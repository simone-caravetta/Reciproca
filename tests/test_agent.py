"""The agent package: settings precedence, provider factory, prompt, and the
real end-to-end of loading tools from the live MCP server over stdio.

The tool-loading test spawns the actual server as a child process (no
browser, no account - just the tools/list handshake), which is exactly the
path the REPL takes at startup.

Requires the agent stack (requirements-agent.txt); the integration class is
skipped when it is not installed.
"""
import os
import tempfile
import unittest
from unittest import mock

import _stubs  # noqa: F401

from reciproca.agent import agent as agent_mod
from reciproca.agent import config as acfg
from reciproca.agent import provider as ap

try:
    from langchain_mcp_adapters.client import MultiServerMCPClient
except ImportError:
    MultiServerMCPClient = None


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._file = acfg.AGENT_CONFIG_FILE
        workdir = tempfile.mkdtemp()
        acfg.AGENT_CONFIG_FILE = os.path.join(workdir, "agent_config.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        acfg.AGENT_CONFIG_FILE = self._file

    def test_defaults_when_no_file(self):
        os.environ.pop("RECIPROCA_AGENT_PROVIDER", None)
        os.environ.pop("RECIPROCA_AGENT_MODEL", None)
        settings = acfg.load_settings()
        self.assertEqual(settings["provider"], "anthropic")
        self.assertEqual(settings["model"], "claude-sonnet-5")

    def test_the_file_overrides_defaults(self):
        with open(acfg.AGENT_CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write('{"provider": "ollama", "ollama": {"model": "llama3"}}')
        os.environ.pop("RECIPROCA_AGENT_PROVIDER", None)
        settings = acfg.load_settings()
        self.assertEqual(settings["provider"], "ollama")
        self.assertEqual(settings["ollama"]["model"], "llama3")
        # The untouched sections keep their defaults.
        self.assertEqual(settings["openai_compatible"]["base_url"],
                         acfg.DEFAULTS["openai_compatible"]["base_url"])

    def test_env_overrides_the_file(self):
        with open(acfg.AGENT_CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write('{"provider": "ollama"}')
        os.environ["RECIPROCA_AGENT_PROVIDER"] = "openai"
        os.environ["RECIPROCA_AGENT_MODEL"] = "my-model"
        os.environ["RECIPROCA_AGENT_BASE_URL"] = "http://localhost:9999/v1"
        settings = acfg.load_settings()
        self.assertEqual(settings["provider"], "openai")
        self.assertEqual(settings["openai_compatible"]["model"], "my-model")
        self.assertEqual(settings["openai_compatible"]["base_url"],
                         "http://localhost:9999/v1")
        self.assertEqual(settings["ollama"]["base_url"], "http://localhost:9999/v1")

    def test_a_malformed_file_falls_back_to_defaults(self):
        with open(acfg.AGENT_CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("not json")
        settings = acfg.load_settings()
        self.assertEqual(settings["provider"], "anthropic")


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_an_unknown_provider_is_a_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            ap.make_llm({"provider": "sideways"})
        self.assertIn("unknown provider", str(ctx.exception))

    def test_anthropic_without_a_key_is_a_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            ap.make_llm({"provider": "anthropic", "model": "claude-sonnet-5",
                         "temperature": 0.2})
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_openai_constructs_lazily_with_a_local_endpoint(self):
        llm = ap.make_llm({
            "provider": "openai", "temperature": 0.2,
            "openai_compatible": {"base_url": "http://localhost:8000/v1",
                                  "model": "m", "api_key": "EMPTY"},
        })
        self.assertEqual(llm.openai_api_base, "http://localhost:8000/v1")

    def test_ollama_constructs_lazily(self):
        llm = ap.make_llm({
            "provider": "ollama", "temperature": 0.2,
            "ollama": {"base_url": "http://localhost:11434", "model": "llama3.1"},
        })
        self.assertEqual(llm.model, "llama3.1")


class AgentAssemblyTest(unittest.TestCase):
    def test_the_prompt_carries_the_engine_and_checkpoint_rules(self):
        self.assertIn("task_id", agent_mod.SYSTEM_PROMPT)
        self.assertIn("confirm with the user", agent_mod.SYSTEM_PROMPT)
        self.assertIn("never fabricate", agent_mod.SYSTEM_PROMPT)

    def test_the_autonomous_variant_swaps_the_checkpoint_rule(self):
        self.assertNotEqual(agent_mod.SYSTEM_PROMPT, agent_mod.SYSTEM_PROMPT_AUTONOMOUS)
        self.assertIn("pre-authorized", agent_mod.SYSTEM_PROMPT_AUTONOMOUS)
        self.assertNotIn("confirm with the user",
                         agent_mod.SYSTEM_PROMPT_AUTONOMOUS.split("pre-authorized")[0])

    def test_mcp_connections_pass_the_display_through(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False):
            conn = agent_mod.mcp_connections()["reciproca"]
        self.assertEqual(conn["transport"], "stdio")
        self.assertEqual(conn["args"], ["-m", "reciproca.mcp_server"])
        self.assertIn("DISPLAY", conn["env"])
        self.assertEqual(conn["env"]["DISPLAY"], ":0")

    def test_the_repl_parser_accepts_the_overrides(self):
        from reciproca.agent.__main__ import build_parser
        args = build_parser().parse_args(
            ["--provider", "ollama", "--model", "llama3", "--say", "ciao"])
        self.assertEqual(args.provider, "ollama")
        self.assertEqual(args.say, "ciao")
        self.assertFalse(args.autonomous)


@unittest.skipIf(MultiServerMCPClient is None, "agent stack not installed")
class AgentIntegrationTest(unittest.TestCase):
    """The REPL's startup path: tools loaded from the live server over stdio."""

    def test_tools_load_from_the_live_server(self):
        import asyncio

        async def go():
            # Same startup path the REPL takes: construct, get_tools().
            client = MultiServerMCPClient(agent_mod.mcp_connections())
            tools = await client.get_tools()
            return {t.name for t in tools}

        names = asyncio.run(go())
        self.assertIn("follow_cycle", names)
        self.assertIn("cycle_status", names)
        self.assertIn("config_reload", names)
        self.assertGreater(len(names), 25, "the full tool surface is exposed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
