"""The agent REPL: python -m reciproca.agent

The agent drives the Reciproca MCP server the way the CLI does - it is one
more frontend, with the same deterministic core underneath. Each line is a
goal in natural language; the agent decides which tools to call, and the
session's progress streams back as it goes.

    python -m reciproca.agent                          interactive
    python -m reciproca.agent --say "report the status"   one-shot
    python -m reciproca.agent --provider ollama --model llama3.1
    python -m reciproca.agent --autonomous   (no confirmation checkpoints)

Ctrl+C stops the agent, not a running cycle: a long follow session keeps
running headless inside the server until it finishes or the `stop` tool is
called.
"""

import argparse
import asyncio
import json
import sys
import warnings

# pydantic-settings (a transitive dep of the mcp SDK) warns once about a
# forward reference inside mcp's own settings model; not ours to fix.
warnings.filterwarnings(
    "ignore", message=r"Field 'lifespan' has an incomplete definition.*")

from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

from reciproca.agent import agent as agent_mod
from reciproca.agent.config import load_settings
from reciproca.agent.provider import make_llm


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m reciproca.agent",
        description="The agent frontend: talk to Reciproca in natural language.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--provider", choices=("anthropic", "openai", "ollama"),
                   help="override the provider in agent_config.json")
    p.add_argument("--model", help="override the model")
    p.add_argument("--base-url", help="override the openai/ollama endpoint")
    p.add_argument("--autonomous", action="store_true",
                   help="skip the confirmation checkpoints (explicit choice, not the default)")
    p.add_argument("--say", metavar="GOAL",
                   help="answer this one goal and exit (no prompt loop)")
    return p


_last_printed = {}


def render(message):
    """One line of the conversation as it streams in."""
    if isinstance(message, AIMessage):
        for call in message.tool_calls:
            print(f"\n🔧 {call['name']}({json.dumps(call['args'], ensure_ascii=False)})",
                  flush=True)
        if message.content:
            print(f"\n🤖 {message.content}", flush=True)
    elif isinstance(message, ToolMessage):
        content = str(message.content)
        if len(content) > 500:
            content = content[:500] + "…"
        print(f"\n📦 {content}", flush=True)


async def _run(messages, agent):
    """Stream one goal through the agent, printing each new message.

    Must run inside the caller's event loop: the MCP tools are bound to the
    session's loop, and a fresh asyncio.run() loop could never reach them.
    """
    printed = set()
    async for step in agent.astream({"messages": messages}, stream_mode="values"):
        message = step["messages"][-1]
        if message.id not in printed:
            printed.add(message.id)
            render(message)


async def _amain():
    args = build_parser().parse_args()
    settings = load_settings()
    if args.provider:
        settings["provider"] = args.provider
    if args.model:
        settings["model"] = args.model
        settings["openai_compatible"]["model"] = args.model
        settings["ollama"]["model"] = args.model
    if args.base_url:
        settings["openai_compatible"]["base_url"] = args.base_url
        settings["ollama"]["base_url"] = args.base_url

    llm = make_llm(settings)
    # The model that matters depends on the provider: the top-level one is
    # anthropic's, the sections carry openai/ollama's.
    section_model = {
        "anthropic": settings["model"],
        "openai": settings["openai_compatible"]["model"],
        "ollama": settings["ollama"]["model"],
    }[settings["provider"]]
    print(f"🤖 Provider: {settings['provider']} · model: {section_model}")

    # Not a context manager since adapter 0.1: construct, get the tools, and
    # keep the object alive - the tools open a stdio session per call.
    client = MultiServerMCPClient(agent_mod.mcp_connections())
    tools = await client.get_tools()
    print(f"🧰 {len(tools)} tools from the Reciproca MCP server\n")
    agent = agent_mod.build_agent(llm, tools, autonomous=args.autonomous)

    messages = []
    if args.say:
        messages.append(("user", args.say))
        await _run(messages, agent)
        return

    print("Parla in italiano o in inglese; `quit` per uscire, Ctrl+C per fermare l'agente.\n")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye - a running cycle is unaffected; `python -m reciproca stop` lo ferma.")
            return
        if not line:
            continue
        if line.lower() in ("quit", "exit"):
            return
        messages.append(("user", line))
        await _run(messages, agent)


def main():
    try:
        asyncio.run(_amain())
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nBye - a running cycle is unaffected; `python -m reciproca stop` lo ferma.")


if __name__ == "__main__":
    main()
