"""Speak to the Reciproca MCP server over stdio, from the terminal.

A tiny interactive client for the tools the server exposes - the same wire
protocol the LangChain agent (`python -m reciproca.agent`) will use, so this
is also the place to try a tool before letting an agent at it.

    python mcp_client.py                              # interactive
    echo 'browser_status' | python mcp_client.py      # one-shot from a pipe

Each line is a tool name, optionally followed by its arguments as JSON:

    mcp> browser_status
    mcp> queue_list {"limit": 5}
    mcp> follow_cycle {"mode": "queue", "limit": 10}

`help` lists the tools, `quit` (or Ctrl+D) exits. The server runs as a child
process of this one, so the browser stays open across tool calls - close it
with `browser_close` or by quitting.

Requires the agent stack: pip install -r requirements-agent.txt
"""
import asyncio
import json
import os
import shlex
import sys

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    print("The mcp SDK is not installed - pip install -r requirements-agent.txt")
    sys.exit(1)


def server_env():
    """Environment for the server child process.

    The mcp SDK only lets a safe allowlist through (HOME, PATH, TERM, ...),
    so the server would inherit no DISPLAY and Chrome would die at startup
    with a cryptic "Chrome instance exited". The display variables are handed
    through explicitly, both X11 and Wayland.
    """
    return {
        key: os.environ[key]
        for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
        if os.environ.get(key)
    }


def parse(line):
    """A tool line: "name" or "name {json args}". Returns (name, args_dict).

    The arguments keep their original quoting: shlex.split() would strip the
    double quotes JSON needs around its keys, so only the tool name is split
    off and the rest is handed to json.loads verbatim.
    """
    parts = shlex.split(line)
    if not parts:
        return None, None
    name = parts[0]
    rest = line[len(name):].strip()
    if not rest:
        return name, {}
    args = json.loads(rest)
    if not isinstance(args, dict):
        raise ValueError("arguments must be a JSON object, e.g. {limit: 5}")
    return name, args


async def run_line(session, name, args):
    result = await session.call_tool(name, args)
    text = result.content[0].text if result.content else "(no content)"
    try:
        print(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print(text)


async def main():
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "reciproca.mcp_server"],
        env=server_env(),
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            by_name = {t.name: t for t in tools.tools}
            print(f"Reciproca MCP server - {len(tools.tools)} tools. `help` for the list, `quit` to exit.")

            if not sys.stdin.isatty():
                # Piped: run every line, then exit.
                for line in sys.stdin:
                    line = line.strip()
                    if not line or line in ("quit", "exit"):
                        continue
                    try:
                        name, args = parse(line)
                        if name in by_name:
                            await run_line(session, name, args)
                    except Exception as e:
                        print(f"✗ {e}")
                return

            while True:
                try:
                    line = input("mcp> ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                line = line.strip()
                if not line:
                    continue
                if line in ("quit", "exit"):
                    break
                if line == "help":
                    for name in sorted(by_name):
                        desc = (by_name[name].description or "").split("\n")[0]
                        print(f"  {name:<18} {desc[:70]}")
                    continue
                try:
                    name, args = parse(line)
                    if name not in by_name:
                        print(f"✗ No such tool: {name} - `help` for the list.")
                        continue
                    await run_line(session, name, args)
                except Exception as e:
                    print(f"✗ {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
