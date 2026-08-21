"""Assemble the agent: the system prompt + the MCP tools + the model.

The tools are not built here: they come from the live MCP server over stdio
(see mcp_connections), which is what makes this an agent over the real
engine rather than over a reimplementation of it.
"""

import os
import select
import sys
import time

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

# langgraph 1.x renamed create_agent to create_react_agent, and the system
# prompt moved from the system_prompt kwarg to prompt.
from langgraph.prebuilt import create_react_agent as create_agent

# How often wait checks the terminal for a typed command while sleeping.
_POLL_MS = 0.2


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
    every 30 seconds - it just burns API calls and log noise. If the user
    types a command while waiting, the wait ends at once and the command
    is returned here as the user's new request.
    """
    seconds = min(max(seconds, 1), 120)
    slept = 0.0
    while slept < seconds:
        line = _read_pending_input()
        if line:
            return f"Wait ended early - the user typed: {line.strip()}"
        time.sleep(min(_POLL_MS, seconds - slept))
        slept += _POLL_MS
    return "Waited without interruption."

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

Cycles are long and asynchronous. follow_cycle and unfollow_run return a \
task_id immediately; poll cycle_status until it reports done, narrating \
progress as it goes. A follow or unfollow cycle acts a few times per minute \
at best, so wait at least 30 seconds between polls - call the wait tool \
instead of polling repeatedly, which only burns tokens and log noise. \
Scraping is different: it finds new profiles steadily, so poll a little \
more often (every 10-15 seconds) and narrate what lands in the queue as it \
happens - how many candidates so far, which hashtag is being searched - so \
the user can follow the session live. The wait tool ends early when the \
user types a command while you are polling - treat that as their new \
request and act on it at once, dropping the cycle you were waiting on. \
Never start a second cycle while one is running: there is one browser and \
one session at a time.

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
    """
    return create_agent(
        llm,
        [*tools, wait],
        prompt=SYSTEM_PROMPT_AUTONOMOUS if autonomous else SYSTEM_PROMPT,
    )
