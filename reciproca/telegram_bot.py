"""The Telegram frontend: python -m reciproca.telegram_bot

The same agent runner the REPL uses, behind a Telegram chat bot (long
polling): goals arrive as chat messages, the agent's narration streams
back into the chat, and the session's log lines are forwarded as a
throttled digest. One process is enough - the bot IS the agent, and the
browser stays on this machine like every other frontend.

    python -m reciproca.telegram_bot
    python -m reciproca.telegram_bot --autonomous

The token and the allowed chat ids live in telegram_config.json next to
the app (template: telegram_config.example.json); the token can also come
from RECIPROCA_TELEGRAM_TOKEN. Anyone else who writes to the bot gets the
hint that their chat id must be added to the allowlist - no other reply.

While the agent sleeps (the wait tool), a chat message interrupts the
wait exactly like a typed command does in the REPL: the wait returns the
text as "the user typed", the agent acts on it in the running turn, and
the message is not re-processed as a separate turn. Messages that arrive
while the agent is not waiting are queued and become the next turn.

The wait doubles as the monitor: it reports the running task's status and
wakes up when the status changes, so the agent narrates progress on its
own. If it falls silent while a task runs, the bot sends the status line
directly - the user never has to ask for an update. Each chat has its own
conversation memory, so a bare "si"/"ok" resolves the pending question of
the previous turn instead of being forgotten.
"""

import argparse
import asyncio
import html
import json
import logging
import os
import queue
import re
import sys
import time
import warnings

# pydantic-settings (a transitive dep of the mcp SDK) warns once about a
# forward reference inside mcp's own settings model; not ours to fix.
warnings.filterwarnings(
    "ignore", message=r"Field 'lifespan' has an incomplete definition.*")

# Same quieting as the REPL: the OpenAI-compatible client logs every HTTP
# round-trip at INFO, which would flood the chat forwarder's file reads.
for _quiet_logger in ("httpx", "httpx2", "httpcore", "openai"):
    logging.getLogger(_quiet_logger).setLevel(logging.WARNING)

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from reciproca import config
from reciproca.agent import agent as agent_mod
from reciproca.agent.__main__ import WELCOME, _tool_logger
from reciproca.agent.config import load_settings as load_agent_settings
from reciproca.agent.memory import SessionMemory
from reciproca.agent.provider import make_llm
from reciproca.config import data_path

TELEGRAM_CONFIG_FILE = data_path("telegram_config.json")

DEFAULTS = {"token": "", "allowed_chat_ids": []}


def load_settings():
    """telegram_config.json next to the app, token overridable by env.

    Same precedence as the agent settings: environment > file > defaults.
    The token is a secret, so it must never be committed - the file is
    git-ignored and the env var is the alternative.
    """
    settings = json.loads(json.dumps(DEFAULTS))
    try:
        with open(TELEGRAM_CONFIG_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
        for key, value in loaded.items():
            settings[key] = value
    except FileNotFoundError:
        pass  # first run: the defaults are the whole story
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠️  Could not read {TELEGRAM_CONFIG_FILE}: {e}", file=sys.stderr)
    if os.environ.get("RECIPROCA_TELEGRAM_TOKEN"):
        settings["token"] = os.environ["RECIPROCA_TELEGRAM_TOKEN"]
    return settings


def is_allowed(chat_id, settings):
    """The allowlist: an empty list allows nobody (token alone is not a door)."""
    return chat_id in settings["allowed_chat_ids"]


def unauthorized_message(chat_id):
    """The only reply an unauthorized chat ever gets - with the way in."""
    return (
        "Non sei autorizzato a usare questo bot. 🤖\n\n"
        f"Il tuo chat id è {chat_id}: aggiungilo a `allowed_chat_ids` in "
        f"{TELEGRAM_CONFIG_FILE} (template: telegram_config.example.json) "
        "e riavvia il bot."
    )


# --- Chat text helpers -----------------------------------------------------

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_TAG_RE = re.compile(r"<b>|</b>")
_ASCTIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - \w+ - ")


def _to_html(text):
    """**bold** markers to Telegram HTML; everything else is escaped.

    The agent narrates in the REPL's markdown-ish style; in a chat the
    literal asterisks would look broken, so the spans become <b> and the
    rest of the text is HTML-escaped before the conversion (the asterisks
    survive the escape, the < > & do not).
    """
    return _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", html.escape(text))


def _balance(chunk):
    """Close any <b> a hard split left open; returns (chunk, reopen_prefix)."""
    depth = sum(1 if m.group() == "<b>" else -1 for m in _TAG_RE.finditer(chunk))
    if depth <= 0:
        return chunk, ""
    return chunk + "</b>" * depth, "<b>" * depth


def _split(text, limit=3900):
    """Chunks for Telegram messages, preferring line and word boundaries.

    Telegram's cap is 4096 characters; a message that long breaks the send
    (and an HTML chunk cut inside a <b> span breaks the parse). Long lines
    are cut at the last space before the limit, and a cut that still lands
    inside a bold span closes the tag and reopens it on the next chunk, so
    every chunk renders on its own.
    """
    lines = text.split("\n")
    chunks, cur = [], ""
    for line in lines:
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit and cur:
            chunks.append(cur + "\n")  # the split line keeps its newline
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)

    out = []
    for chunk in chunks:
        while len(chunk) > limit:
            cut = chunk.rfind(" ", 0, limit + 1)
            if cut <= limit // 2:
                cut = limit
            piece, rest = chunk[:cut], chunk[cut:].lstrip()
            piece, prefix = _balance(piece)
            out.append(piece)
            chunk = prefix + rest
        out.append(chunk)
    return out


async def _safe_send(app, chat_id, text, parse_mode=None):
    """Send, chunked; a failed send never takes the bot down with it."""
    try:
        for chunk in _split(text):
            await app.bot.send_message(
                chat_id=chat_id, text=chunk, parse_mode=parse_mode)
    except Exception as e:
        _tool_logger().warning("telegram send failed: %s", e)


# --- Log forwarding ---------------------------------------------------------
#
# The cycles run in the MCP server child process, so register_sink (which
# only catches log() calls in this process) would see almost nothing here.
# Both processes write the same follow_bot.log, so the file is the one
# stream that sees everything - the forwarder tails it and streams new
# lines into the chat in throttled batches.

class LogForwarder:
    """Tails follow_bot.log from the end; new lines go in .buffer."""

    def __init__(self, path, interval=4.0):
        self.path = path
        self.interval = interval
        self.buffer = []
        self._offset = None

    def _new_lines(self):
        if self._offset is None:
            # Start from the end: the pre-bot history is not replayed.
            self._offset = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                lines = f.readlines()
                self._offset = f.tell()
        except OSError:
            return []
        return [line.rstrip("\n") for line in lines]


def _forwardable(line):
    """A log line worth streaming to the chat.

    The 🔧/📦 tool payloads are the REPL's internal noise - the agent's
    narration already tells the user what is happening - and the file's
    own framing lines are empty once prettified.
    """
    return "🔧" not in line and "📦" not in line


def _prettify(line):
    """Drop the file's "2026-08-23 14:22:01,123 - INFO - " prefix."""
    return _ASCTIME_RE.sub("", line)


def _format_batch(lines, limit=30, max_chars=3800):
    """One chat message from a burst of log lines: the last `limit`, cut."""
    lines = lines[-limit:]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "…" + text[-(max_chars - 1):]
    return text


async def _forward_loop(app):
    """Read the log file every second, flush a batch to the chat every few."""
    S = app.bot_data
    forwarder = S["log_forwarder"]
    last_flush = 0.0
    while True:
        await asyncio.sleep(1.0)
        for line in forwarder._new_lines():
            if _forwardable(line):
                forwarder.buffer.append(_prettify(line))
        if forwarder.buffer and time.monotonic() - last_flush >= forwarder.interval:
            batch = forwarder.buffer[:]
            forwarder.buffer.clear()
            last_flush = time.monotonic()
            text = _format_batch(batch)
            for chat_id in S["bot_settings"]["allowed_chat_ids"]:
                await _safe_send(app, chat_id, text)


# --- The agent turn runner --------------------------------------------------

# Chat messages waiting to become turns. Two consumers pop from here: the
# turn worker (when no turn runs) and the wait tool (when one does - via
# the patched _read_pending_input, see _install_telegram_wait). Because a
# message is consumed by whichever reaches it first, a message sent while
# the agent sleeps interrupts the wait and is handled inside the running
# turn - the REPL behaviour, not a queued duplicate.
_pending = queue.SimpleQueue()


def _install_telegram_wait():
    """Point the wait tool at the chat inbox instead of stdin.

    The wait tool reads agent_mod._read_pending_input at call time, so
    swapping the module function redirects it. The bot runs in a terminal
    and stdin would be a tty, which the REPL's isatty gate would treat as
    a real user typing at the keyboard - wrong here, where the user types
    in Telegram. Returns the original, for tests to restore.
    """
    original = agent_mod._read_pending_input

    def _read():
        try:
            return _pending.get_nowait()[1]
        except queue.Empty:
            return None

    agent_mod._read_pending_input = _read
    return original


async def _render(app, message):
    """One line of the conversation as it streams in.

    Tool calls (🔧) and raw results (📦) stay in the file-only tool log,
    exactly like the REPL: the chat gets the agent's narration, converted
    to Telegram HTML.
    """
    if isinstance(message, AIMessage):
        for call in message.tool_calls:
            _tool_logger().info("🔧 %s(%s)", call["name"],
                                json.dumps(call["args"], ensure_ascii=False))
        if message.content:
            await _safe_send(app, app.bot_data["turn_chat_id"],
                             _to_html(str(message.content)), parse_mode="HTML")
    elif isinstance(message, ToolMessage):
        content = str(message.content)
        if len(content) > 500:
            content = content[:500] + "…"
        _tool_logger().info("📦 %s", content)


async def _run_turn(app, chat_id, text, memory):
    """Stream one goal through the agent, sending narration to the chat.

    Same shape as the REPL's _run: stream_mode="values", each new message
    rendered once. The turn opens with the conversation memory (so a "si"
    resolves its pending question) and returns the turn's message list for
    the memory to record.
    """
    S = app.bot_data
    S["turn_chat_id"] = chat_id
    printed = set()
    turn_messages = []
    forced_end = False
    messages = memory.context_messages(text)
    async for step in S["agent"].astream({"messages": messages},
                                         stream_mode="values"):
        message = step["messages"][-1]
        turn_messages.append(message)
        if message.id not in printed:
            printed.add(message.id)
            if isinstance(message, AIMessage):
                if message.content:
                    # A narration breaks a stale sequence and refreshes the
                    # push monitor's silence clock.
                    agent_mod.reset_wait_state()
                    S["last_narration_at"] = time.monotonic()
                elif message.tool_calls and agent_mod.note_stale_tool_calls(
                        message.tool_calls):
                    # Past the stall cap: close the turn ourselves with the
                    # current status line, so the chat stays responsive.
                    # The running cycle lives in the server - it is untouched.
                    forced_end = True
                    break
            await _render(app, message)
    if forced_end:
        line = S["watch"].read()[1]
        closing = AIMessage(content=line or agent_mod.FALLBACK_CLOSING)
        turn_messages.append(closing)
        S["last_narration_at"] = time.monotonic()
        await _render(app, closing)
    return turn_messages


async def _turn_worker(app):
    """One turn at a time: the queue is drained serially, never in parallel.

    The MCP session, the browser and the cycle registry are all single, so
    two turns at once could never work; the queue serializes them. While a
    turn runs, new messages stay queued - or interrupt the wait, if the
    agent is sleeping. Each chat has its own conversation memory.
    """
    S = app.bot_data
    while True:
        chat_id, text = await asyncio.to_thread(_pending.get)
        memory = S["memories"].get(chat_id) or SessionMemory()
        S["memories"][chat_id] = memory
        agent_mod.reset_wait_state()
        try:
            turn = await _run_turn(app, chat_id, text, memory)
            memory.record_turn(text, turn)
        except Exception as e:
            _tool_logger().warning("turn failed: %s", e)
            await _safe_send(app, chat_id, f"⚠️ La richiesta è fallita: {e}")


async def _push_loop(app):
    """The guarantee: status lines reach the chat even when the model is
    silent. When the agent narrates nothing for a while while a task runs,
    the running task's status line is sent directly - throttled to one per
    30 seconds, like the log forwarder.
    """
    S = app.bot_data
    last_push = 0.0
    warned = False
    while True:
        await asyncio.sleep(10)
        # The push must never die either: a hiccup here is exactly the
        # silence this loop exists to prevent.
        try:
            _, line = S["watch"].read()
            if not line:
                continue
            now = time.monotonic()
            if now - S["last_narration_at"] > 45 and now - last_push > 30:
                last_push = now
                chat_id = S.get("turn_chat_id") or (S["bot_settings"]["allowed_chat_ids"] or [None])[0]
                if chat_id is not None:
                    await _safe_send(app, chat_id, f"📊 {line}")
        except Exception as e:
            if not warned:
                warned = True
                _tool_logger().warning("push loop hiccup: %s", e)


# --- Telegram handlers ------------------------------------------------------

def _welcome_text():
    return WELCOME.replace(
        "(scrivi `quit` per uscire)", "(i comandi si scrivono qui in chat)")


async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id, app.bot_data["bot_settings"]):
        await _safe_send(app, chat_id, unauthorized_message(chat_id))
        return
    await _safe_send(app, chat_id, _to_html(_welcome_text()), parse_mode="HTML")


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id, app.bot_data["bot_settings"]):
        await _safe_send(app, chat_id, unauthorized_message(chat_id))
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    _pending.put((chat_id, text))


async def _on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id, app.bot_data["bot_settings"]):
        await _safe_send(app, chat_id, unauthorized_message(chat_id))
        return
    await _safe_send(
        app, chat_id,
        "Scrivi il tuo obiettivo in linguaggio naturale "
        "(es. «come va la sessione?»).")


# --- Lifecycle --------------------------------------------------------------

async def _hold_session(app):
    """Own the MCP session for the bot's whole life.

    The session must span post_init..post_shutdown, but the REPL's `async
    with client.session(...)` cannot be entered in post_init and exited in
    post_shutdown: entering the adapter's async-generator context manager
    manually leaves its cancel scopes tied to the entering task and the
    session's streams die (the tools/list handshake hangs). So a dedicated
    task owns the `async with` - it enters, builds the agent, starts the
    worker and the forwarder, then sleeps until the shutdown event, when
    it exits the block (and the session) in the same task that entered it.
    """
    S = app.bot_data
    client = MultiServerMCPClient(agent_mod.mcp_connections())
    try:
        async with client.session("reciproca") as session:
            tools = await load_mcp_tools(session)
            S["session"] = session
            S["agent"] = agent_mod.build_agent(
                S["llm"], tools, autonomous=S["autonomous"])
            S["log_forwarder"] = LogForwarder(config.LOG_FILE)
            _install_telegram_wait()
            # The task-status watcher: the poller keeps the wait tool's
            # snapshot fresh (so a wait wakes on status changes) and the
            # pusher sends status lines when the model narrates nothing.
            agent_mod.set_status_provider(S["watch"].read)
            S["monitor"] = asyncio.create_task(
                agent_mod.status_monitor(session, S["watch"]))
            S["pusher"] = asyncio.create_task(_push_loop(app))
            S["worker"] = asyncio.create_task(_turn_worker(app))
            S["forwarder"] = asyncio.create_task(_forward_loop(app))
            for chat_id in S["bot_settings"]["allowed_chat_ids"]:
                await _safe_send(app, chat_id,
                                 "🤖 Bot online - scrivi una richiesta in "
                                 "linguaggio naturale o /start per il benvenuto.")
            S["ready"].set()
            await S["shutdown"].wait()
    except Exception as e:
        S["error"] = e
        S["ready"].set()
        raise
    finally:
        agent_mod.set_status_provider(None)


async def _post_init(app):
    """Start the session holder and wait until the agent is ready."""
    S = app.bot_data
    S["ready"] = asyncio.Event()
    S["shutdown"] = asyncio.Event()
    S["error"] = None
    S["holder"] = asyncio.create_task(_hold_session(app))
    await S["ready"].wait()
    if S["error"]:
        raise S["error"]


async def _post_shutdown(app):
    """Stop the background tasks and wake the holder to close the session."""
    S = app.bot_data
    for key in ("worker", "forwarder", "monitor", "pusher"):
        if S.get(key):
            S[key].cancel()
    if S.get("shutdown"):
        S["shutdown"].set()
    if S.get("holder"):
        await asyncio.wait([S["holder"]], timeout=10)


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m reciproca.telegram_bot",
        description="The Telegram frontend: the same agent runner, in a chat.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--provider", choices=("anthropic", "openai", "ollama"),
                   help="override the provider in agent_config.json")
    p.add_argument("--model", help="override the model")
    p.add_argument("--base-url", help="override the openai/ollama endpoint")
    p.add_argument("--autonomous", action="store_true",
                   help="skip the confirmation checkpoints (explicit choice, not the default)")
    return p


def main():
    args = build_parser().parse_args()
    settings = load_settings()
    if not settings["token"]:
        print("✗ nessun token: metti il token del bot in telegram_config.json "
              "(vedi telegram_config.example.json) o in RECIPROCA_TELEGRAM_TOKEN",
              file=sys.stderr)
        sys.exit(1)
    if not settings["allowed_chat_ids"]:
        print("⚠️  allowed_chat_ids è vuoto: il bot rifiuterà tutti. Avvialo, "
              "mandagli un messaggio e aggiungi il chat id che ti risponde in "
              "telegram_config.json, poi riavvialo.", file=sys.stderr)

    agent_settings = load_agent_settings()
    if args.provider:
        agent_settings["provider"] = args.provider
    if args.model:
        agent_settings["model"] = args.model
        agent_settings["openai_compatible"]["model"] = args.model
        agent_settings["ollama"]["model"] = args.model
    if args.base_url:
        agent_settings["openai_compatible"]["base_url"] = args.base_url
        agent_settings["ollama"]["base_url"] = args.base_url

    # Built here, not in post_init: a missing API key must fail with the
    # same clear error the REPL gives, before the bot starts polling.
    try:
        llm = make_llm(agent_settings)
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)

    app = (Application.builder()
           .token(settings["token"])
           .post_init(_post_init)
           .post_shutdown(_post_shutdown)
           .build())
    S = app.bot_data
    S["bot_settings"] = settings
    S["autonomous"] = args.autonomous
    S["llm"] = llm
    S["session"] = None
    S["agent"] = None
    S["log_forwarder"] = None
    S["worker"] = None
    S["forwarder"] = None
    S["monitor"] = None
    S["pusher"] = None
    S["holder"] = None
    S["watch"] = agent_mod.StatusWatch()
    S["memories"] = {}
    S["last_narration_at"] = 0.0
    S["turn_chat_id"] = None

    app.add_handler(CommandHandler("start", _on_start))
    app.add_handler(CommandHandler("help", _on_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_handler(MessageHandler(filters.COMMAND, _on_unknown_command))

    print("🤖 Bot in ascolto... Ctrl+C per fermare.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
