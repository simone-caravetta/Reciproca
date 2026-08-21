"""Assemble the agent: the system prompt + the MCP tools + the model.

The tools are not built here: they come from the live MCP server over stdio
(see mcp_connections), which is what makes this an agent over the real
engine rather than over a reimplementation of it.
"""

import os
import sys

# langgraph 1.x renamed create_agent to create_react_agent, and the system
# prompt moved from the system_prompt kwarg to prompt.
from langgraph.prebuilt import create_react_agent as create_agent

SYSTEM_PROMPT = """\
You operate Reciproca, a local Instagram growth-testing bot, through its MCP \
tools. The user's goals arrive in natural language; you translate them into \
tool calls and report back in plain language, in the language the user speaks.

The engine decides every individual follow or unfollow: ranking, bot filter, \
delays, rate limits. Never override or second-guess its rules, and never \
fabricate usernames, counts or outcomes - if a tool errors or returns \
nothing, say so plainly and stop.

Cycles are long and asynchronous. follow_cycle and unfollow_run return a \
task_id immediately; poll cycle_status until it reports done, narrating \
progress as it goes. A cycle takes a few actions per minute at best, so \
space your polls at least 30 seconds apart - polling faster just burns \
tokens and noise. Never start a second cycle while one is running: there \
is one browser and one session at a time.

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
    """The agent graph: the model with the tools bound and the prompt set."""
    return create_agent(
        llm,
        tools,
        prompt=SYSTEM_PROMPT_AUTONOMOUS if autonomous else SYSTEM_PROMPT,
    )
