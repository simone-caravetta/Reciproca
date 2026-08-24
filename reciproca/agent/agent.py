"""Assemble the agent: the system prompt + the MCP tools + the model.

The tools are not built here: they come from the live MCP server over stdio
(see mcp_connections), which is what makes this an agent over the real
engine rather than over a reimplementation of it.
"""

import asyncio
import json
import os
import select
import sys
import time

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

# langgraph 1.x renamed create_agent to create_react_agent, and the system
# prompt moved from the system_prompt kwarg to prompt.
from langgraph.prebuilt import create_react_agent as create_agent

from . import compact

# How often wait checks the terminal for a typed command while sleeping.
_POLL_MS = 0.2

# A wait, or a read-only poll (status, cycle_status, logs_tail), that keeps
# returning "nothing new" more than this many times in a row is a stall: the
# model is polling without learning and without telling the user anything.
# The next call then refuses, and once the cap is truly exceeded the runner
# closes the turn and lets the user back in (the cycle keeps running in the
# server either way - the turn is only an observer). The counter resets on
# every narration, on every informative wait return (a status change, a user
# message), on a real action (a non-read tool call), and at the start of
# every turn - so healthy polling never hits the cap.
_MAX_STALE_WAITS = 4

# Tools that only look at state. A model polling with these and nothing else
# is not doing work: a streak of them without a narration counts as a stall.
READ_TOOL_NAMES = frozenset({"wait", "status", "cycle_status", "logs_tail"})

# The line the runner emits when it has to close a silent polling turn and
# the watch has no status to show (fallback only - normally the current
# status line is used).
FALLBACK_CLOSING = "The task is still running - I'll keep you posted as it goes."

# The wait tool's view of the running task. The runner (REPL or Telegram)
# installs a provider: a callable returning (generation, line) - the current
# status line and how many times it has changed. None means no provider and
# the wait sleeps blindly, like before.
_status_provider = None
_stale_waits = 0


def set_status_provider(provider):
    """Install the task-status watcher the wait tool reads (runner side)."""
    global _status_provider
    _status_provider = provider


def reset_wait_state():
    """Fresh turn, or a new piece of information: the stale counter starts
    over. Called by the runners before each turn, and whenever the model
    narrates or takes a real action."""
    global _stale_waits
    _stale_waits = 0


def note_stale_tool_calls(tool_calls):
    """The runner's view of a silent tool call (no narration in the same
    message). Read-only polls count as a stall; a real action resets the
    counter. Returns True when the cap is exceeded - the runner should end
    the turn so the user can type again.
    """
    global _stale_waits
    names = {call.get("name") for call in tool_calls}
    if names and names <= READ_TOOL_NAMES:
        _stale_waits += 1
        return _stale_waits > _MAX_STALE_WAITS
    reset_wait_state()
    return False


def _read_pending_input():
    """A line the user typed during a wait, if any; never blocks.

    Only a real terminal counts: with a piped stdin (one-shot runs, tests,
    scripts) a command that was queued since the start of the turn would
    look like something typed "right now", so the wait ignores it there.
    """
    if not sys.stdin.isatty():
        return None
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        return None
    if not ready:
        return None
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError):
        return None
    return line or None


@tool
def wait(seconds: int = 30):
    """Sleep without doing anything else. Use it between cycle_status polls
    instead of polling repeatedly: a follow or unfollow cycle acts a few
    times per minute, so nothing is learned by polling more often than
    every 30 seconds - it just burns API calls and log noise. The wait
    doubles as the monitor: it reports the running task's status in its
    result and ends early when the status changes, so waiting and checking
    are the same call. If the user types a command while waiting, the wait
    ends at once and the command is returned here as the user's new
    request.
    """
    global _stale_waits
    seconds = min(max(seconds, 1), 120)
    _stale_waits += 1
    if _stale_waits > _MAX_STALE_WAITS:
        return (
            f"You have already waited {_MAX_STALE_WAITS} times in a row and "
            "nothing changed. Stop waiting: check the task with cycle_status "
            "or status, or report to the user what you know, or end the turn "
            "so the next message can be processed."
        )
    baseline_generation = _status_provider()[0] if _status_provider else 0
    slept = 0.0
    while slept < seconds:
        line = _read_pending_input()
        if line:
            reset_wait_state()
            return f"Wait ended early - the user typed: {line.strip()}"
        if _status_provider:
            generation, status = _status_provider()
            if generation != baseline_generation:
                reset_wait_state()
                if status:
                    return f"Waited {int(slept)}s - status update: {status}"
        time.sleep(min(_POLL_MS, seconds - slept))
        slept += _POLL_MS
    if _status_provider:
        _, status = _status_provider()
        if status:
            return f"Waited {int(seconds)}s. {status}"
    return "Waited without interruption."


class StatusWatch:
    """The shared, thread-safe task snapshot: the poller writes, wait reads.

    The status poller (runner side) calls update() whenever the task state
    changes; the wait tool reads (generation, line) as one tuple, so the two
    sides never race even though the wait runs in a worker thread.
    """

    def __init__(self):
        self._state = (0, None)  # (generation, status line)

    def read(self):
        return self._state

    def update(self, line):
        generation, _ = self._state
        self._state = (generation + 1, line)


def status_line(snapshot):
    """One readable line for a task, from a cycle_status snapshot.

    The raw snapshot is JSON-shaped (kind, state, progress, result, recent
    log lines); the wait tool and the runner's push both hand the model and
    the user this plain-text version, so nobody has to parse the JSON.
    Returns None when the input is not a usable snapshot.
    """
    if not isinstance(snapshot, dict):
        return None
    tag = f"Task {snapshot.get('task_id') or '?'} ({snapshot.get('kind') or 'task'})"
    if snapshot.get("error"):
        return f"{tag} failed: {snapshot['error']}"
    if snapshot.get("state") == "done":
        summary = _result_summary(snapshot.get("result"))
        return f"{tag} completed - {summary}" if summary else f"{tag} completed"
    progress = snapshot.get("progress")
    if isinstance(progress, dict):
        done, total = progress.get("done"), progress.get("total")
        if isinstance(done, int) and isinstance(total, int):
            line = f"{tag}: {done}/{total}"
            if progress.get("current"):
                line += f" ({progress['current']})"
            return line
    for entry in reversed(snapshot.get("last_logs") or []):
        message = entry.get("message") if isinstance(entry, dict) else None
        if message and message.strip():
            return f"{tag} in progress - last: {message.strip()}"
    elapsed = snapshot.get("elapsed_seconds")
    if isinstance(elapsed, (int, float)):
        return f"{tag} in progress for {int(elapsed)}s"
    return f"{tag} in progress"


def _result_summary(result):
    if not isinstance(result, dict):
        return str(result)[:100]
    parts = [
        f"{key}={result[key]}"
        for key in ("followed", "skipped", "errors", "succeeded", "attempted",
                    "added", "removed", "unfollowed", "done")
        if key in result
    ]
    if parts:
        return ", ".join(parts)
    return json.dumps(result, ensure_ascii=False)[:100]


def _tool_result_text(result):
    """The first text block of an MCP CallToolResult, or '{}'."""
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return text
    return "{}"


async def _current_task_snapshot(session):
    """The most recent task's cycle_status snapshot, or None when idle.

    Two calls: status lists the tasks, cycle_status gives the full snapshot
    of the active one (running first, else the latest - whose final result
    is the "completed" line). Both are local stdio calls, so a 4-second
    poll is cheap.
    """
    payload = json.loads(_tool_result_text(await session.call_tool("status", {})))
    tasks = payload.get("tasks") or []
    if not tasks:
        return None
    running = next((t for t in tasks if t.get("state") == "running"), None)
    task_id = (running or tasks[-1]).get("task_id")
    payload = json.loads(_tool_result_text(
        await session.call_tool("cycle_status", {"task_id": task_id})))
    if payload.get("error"):
        return None
    return payload


async def status_monitor(session, watch, interval=4.0):
    """Keep the wait tool's snapshot fresh for the session's whole life.

    Polls the MCP status, and bumps the watch's generation whenever the
    active task visibly changes (progress, last log line, done, error) - a
    wait in progress then wakes up and returns the new line. Never raises:
    a failed poll is skipped and retried on the next tick.
    """
    last_key = object()
    warned = False
    while True:
        await asyncio.sleep(interval)
        # The whole tick is guarded, not just the poll: a malformed snapshot
        # (or anything else) must never kill this task, or the wait would
        # stop waking up and the turn would hang silent. A bad tick is
        # skipped; the first one is reported to stderr for diagnosis.
        try:
            snapshot = await _current_task_snapshot(session)
            if snapshot is None:
                # A None snapshot means cycle_status answered with an error
                # (see _current_task_snapshot): skip the tick entirely - the
                # watch keeps its last good line, nothing to compare or show.
                continue
            key = json.dumps(
                {k: snapshot.get(k)
                 for k in ("task_id", "kind", "state", "progress", "error")},
                default=str, sort_keys=True,
            )
            last_log = (snapshot.get("last_logs") or [{}])[-1].get("message")
            key += f"|{last_log}"
            if key == last_key:
                continue
            last_key = key
            watch.update(status_line(snapshot) if snapshot else None)
        except Exception as e:
            if not warned:
                warned = True
                print(f"(status monitor skipped a bad poll: {e})",
                      file=sys.stderr, flush=True)

SYSTEM_PROMPT = """\
You operate Reciproca, a local Instagram growth-testing bot, through its MCP \
tools. The user's goals arrive in natural language; you translate them into \
tool calls and report back in plain language, in the language the user speaks.

The engine decides every individual follow or unfollow: ranking, bot filter, \
delays, rate limits. Never override or second-guess its rules, and never \
fabricate usernames, counts or outcomes - if a tool errors or returns \
nothing, say so plainly and stop.

Every user message is a fresh request: anything asked before has already \
been executed and completed, so do not re-run it. If a request is ambiguous \
or refers to something earlier, ask the user to restate it.

Before your message a "Conversation memory" block summarises the \
previous turns: what was requested, what was done, and any open question. \
It is background, not a request - never re-run what it marks as done. A \
bare confirmation in English or Italian (si, sì, ok, certo, va bene, yes, \
yeah, sure, alright, fine) answers the open question shown in that block - \
never ask the user to restate it.

Tool arguments are exact JSON: pass plain values. hashtags_add and \
queue_add take plain lists of strings - e.g. ["street", "35mm"] or \
["alice", "bob"] - one tag or username per string. When a tool rejects a \
call, re-read its error and fix the format it asks for; do not retry with \
different key names, the schema does not change.

Cycles are long and asynchronous. follow_cycle and unfollow_run return a \
task_id immediately; poll cycle_status until it reports done, narrating \
progress as it goes. When cycle_status reports done, communicate the result \
and stop polling. A follow or unfollow cycle acts a few times per minute at \
best, so wait at least 30 seconds between polls - call the wait tool \
instead of polling repeatedly, which only burns tokens and log noise. The \
wait tool doubles as the monitor: its result reports the running task's \
status and it ends early when the status changes or the user sends a \
message - when the status line differs from what you last told the user, \
narrate it in one short line; keep doing this until the task is done, the \
user never needs to ask for an update. Scraping is different: it finds new \
profiles steadily, so poll a little more often (every 10-15 seconds) and \
narrate what lands in the queue as it happens - how many candidates so far, \
which hashtag is being searched - so the user can follow the session live. \
The wait tool ends early when the user types a command while you are \
polling - treat that as their new request and act on it at once, dropping \
the cycle you were waiting on; if you cannot act on it at once, end the \
turn so it can be processed. Never start a second cycle while one is \
running: there is one browser and one session at a time.

Watch for anomalies: a spike of errors, a rate_limited flag, a browser that \
will not open. On an anomaly, call stop, then summarise what happened and \
suggest what to do next.

The browser and the login are shared and manual. The login persists in the \
profile, but if login_wait reports a timeout, tell the user exactly what to \
do: log in to the Chrome window, then say they are done.

Instagram's terms: keep the configured delays, never propose raising the \
limits or loosening the bot filter to follow more aggressively.

Before starting a long cycle, or any destructive change (queue_clear, \
queue_trim, unfollow_reset, config_set), confirm with the user first."""

# The autonomous variant: the same prompt with the confirmation rule
# swapped. --autonomous is an explicit operator choice, not a default.
SYSTEM_PROMPT_AUTONOMOUS = SYSTEM_PROMPT.replace(
    "Before starting a long cycle, or any destructive change (queue_clear, "
    "queue_trim, unfollow_reset, config_set), confirm with the user first.",
    "You run in autonomous mode: the operator pre-authorized cycles and "
    "destructive changes (queue_clear, queue_trim, unfollow_reset, "
    "config_set). Still announce what you are about to do before doing it.",
)


def turn_context(last_message):
    """What the next REPL turn may see of the previous one.

    Each turn gets only the user's new request, not the whole conversation:
    with the full history in context, smaller models tend to re-run requests
    that are already completed (the REPL used to feed the accumulated
    messages, and the agent kept repeating earlier commands on every new
    one). The one exception is a pending question from the agent - a
    confirmation checkpoint - because a plain "si"/"ok" needs that question
    to resolve, so it is carried into the next turn.
    """
    content = getattr(last_message, "content", "")
    if isinstance(last_message, AIMessage) and str(content).rstrip().endswith("?"):
        return [last_message]
    return []


def server_env():
    """The display variables the mcp SDK would strip from the child env.

    The SDK's stdio allowlist only lets a safe subset of the parent's
    environment through (HOME, PATH, TERM, ...), so without this the server
    child would inherit no DISPLAY and Chrome would die at startup with a
    cryptic "Chrome instance exited". Handed through explicitly, X11 and
    Wayland both.
    """
    return {
        key: os.environ[key]
        for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
        if os.environ.get(key)
    }


def mcp_connections():
    """The MultiServerMCPClient config for the Reciproca stdio server."""
    return {
        "reciproca": {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "reciproca.mcp_server"],
            "env": server_env(),
        }
    }


def build_agent(llm, tools, autonomous=False):
    """The agent graph: the model with the tools bound and the prompt set.

    The local wait tool is mixed into the MCP tools: it gives the model a
    cheap way to pace its cycle_status polling (the model used to re-poll
    every couple of seconds, flooding the API and the terminal).

    The pre_model_hook (compact.compact_turn_history) hands the model a
    compacted view of the turn: user messages and narrations whole, raw
    tool outputs reduced to their essence once used - so a polling turn
    with dozens of tool calls does not double the prompt at every step.
    The graph state keeps the full history for the render and the turn
    memory.
    """
    return create_agent(
        llm,
        [*tools, wait],
        prompt=SYSTEM_PROMPT_AUTONOMOUS if autonomous else SYSTEM_PROMPT,
        pre_model_hook=compact.compact_turn_history,
    )
