"""The agent package: settings precedence, provider factory, prompt, and the
real end-to-end of loading tools from the live MCP server over stdio.

The tool-loading test spawns the actual server as a child process (no
browser, no account - just the tools/list handshake), which is exactly the
path the REPL takes at startup.

Requires the agent stack (requirements-agent.txt); the integration class is
skipped when it is not installed.
"""
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

import _stubs  # noqa: F401

from reciproca import config
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

    def test_the_prompt_forbids_tight_polling_loops(self):
        # The agent polled cycle_status dozens of times in a row in testing:
        # the prompt must impose a floor on the polling cadence, and point at
        # the wait tool instead of re-polling.
        self.assertIn("30 seconds", agent_mod.SYSTEM_PROMPT)
        self.assertIn("30 seconds", agent_mod.SYSTEM_PROMPT_AUTONOMOUS)
        self.assertIn("wait tool", agent_mod.SYSTEM_PROMPT)
        self.assertIn("wait tool", agent_mod.SYSTEM_PROMPT_AUTONOMOUS)
        # Scraping finds profiles steadily, so it gets a livelier cadence and
        # live narration, unlike the slow follow/unfollow cycles.
        self.assertIn("10-15 seconds", agent_mod.SYSTEM_PROMPT)
        self.assertIn("narrate what lands in the queue", agent_mod.SYSTEM_PROMPT)

    def _total_slept(self, sleep_mock):
        return sum(c.args[0] for c in sleep_mock.call_args_list)

    def test_the_wait_tool_sleeps_with_a_safe_clamp(self):
        from reciproca.agent.agent import wait

        with mock.patch("reciproca.agent.agent.time.sleep") as sleep, \
                mock.patch("reciproca.agent.agent._read_pending_input",
                           return_value=None):
            # The requested duration is slept in small ticks so the terminal
            # stays responsive to a typed command.
            wait.invoke({"seconds": 45})
            self.assertAlmostEqual(self._total_slept(sleep), 45.0, places=1)
            sleep.reset_mock()
            # Out-of-range requests are clamped, never free-form.
            wait.invoke({"seconds": 500})
            self.assertAlmostEqual(self._total_slept(sleep), 120.0, places=1)
            sleep.reset_mock()
            wait.invoke({"seconds": -3})
            self.assertAlmostEqual(self._total_slept(sleep), 1.0, places=1)
            sleep.reset_mock()
            # The default paces the 30-second polling floor.
            wait.invoke({})
            self.assertAlmostEqual(self._total_slept(sleep), 30.0, places=1)

    def test_the_wait_tool_ends_early_on_a_user_command(self):
        from reciproca.agent.agent import wait

        with mock.patch("reciproca.agent.agent.time.sleep") as sleep, \
                mock.patch("reciproca.agent.agent._read_pending_input",
                           return_value="fermati\n") as inp:
            result = wait.invoke({"seconds": 60})
        self.assertIn("fermati", result)
        # The command was seen before any sleeping happened.
        sleep.assert_not_called()
        inp.assert_called_once()

    def test_a_blank_line_does_not_interrupt_the_wait(self):
        from reciproca.agent.agent import wait

        with mock.patch("reciproca.agent.agent.time.sleep") as sleep, \
                mock.patch("reciproca.agent.agent._read_pending_input",
                           return_value=None):
            wait.invoke({"seconds": 2})
        self.assertGreater(len(sleep.call_args_list), 0)

    def test_read_pending_input_ignores_a_piped_stdin(self):
        # Only a real terminal can interrupt a wait: with a pipe, commands
        # queued at the start of the turn would look like fresh typing.
        from reciproca.agent.agent import _read_pending_input
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            self.assertIsNone(_read_pending_input())

    def test_the_prompt_hands_a_typed_command_back_as_a_new_request(self):
        self.assertIn("ends early when the user types a command",
                      agent_mod.SYSTEM_PROMPT)
        self.assertIn("ends early when the user types a command",
                      agent_mod.SYSTEM_PROMPT_AUTONOMOUS)

    def test_the_prompt_marks_each_user_message_as_fresh(self):
        # With the full conversation in context the agent re-ran completed
        # requests on every new command; the prompt now states the rule.
        self.assertIn("fresh request", agent_mod.SYSTEM_PROMPT)
        self.assertIn("fresh request", agent_mod.SYSTEM_PROMPT_AUTONOMOUS)

    def test_turn_context_keeps_only_a_pending_question(self):
        from langchain_core.messages import AIMessage, ToolMessage

        pending = AIMessage(content="Confermi che parto con lo scraping?")
        self.assertEqual(agent_mod.turn_context(pending), [pending])
        # Anything else resets the context: no history, no re-runs.
        self.assertEqual(agent_mod.turn_context(AIMessage(content="Fatto.")), [])
        self.assertEqual(
            agent_mod.turn_context(ToolMessage(content="ok", tool_call_id="t")), [])
        self.assertEqual(agent_mod.turn_context(None), [])
        # A question buried in a content list does not count as pending.
        self.assertEqual(agent_mod.turn_context(AIMessage(content=["Confermi?"])), [])

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

    def test_render_logs_tool_noise_instead_of_printing_it(self):
        from langchain_core.messages import AIMessage, ToolMessage
        from reciproca.agent import __main__ as repl

        with mock.patch("reciproca.agent.__main__.print") as print_mock:
            with mock.patch("reciproca.agent.__main__._tool_logger") as lg:
                repl.render(AIMessage(
                    content="Apro il browser",
                    tool_calls=[{"name": "browser_open", "args": {"headless": False},
                                 "id": "c1", "type": "tool_call"}]))
                repl.render(ToolMessage(content='{"ok": true}', tool_call_id="c1"))

        # The terminal only ever sees the agent's plain replies.
        printed = [c.args[0] for c in print_mock.call_args_list]
        self.assertEqual(printed, ["\n🤖 Apro il browser"])
        # The tool call and its result are filed away in the log instead.
        logged = [c.args for c in lg.return_value.info.call_args_list]
        self.assertEqual(logged[0][0], "🔧 %s(%s)")
        self.assertEqual(logged[0][1], "browser_open")
        self.assertEqual(logged[1][0], "📦 %s")
        self.assertEqual(logged[1][1], '{"ok": true}')

    def test_the_welcome_message_opens_the_repl(self):
        from reciproca.agent import __main__ as repl
        self.assertIn("Ciao", repl.WELCOME)
        self.assertIn("**Follow**", repl.WELCOME)
        self.assertIn("**Unfollow**", repl.WELCOME)
        self.assertIn("quit", repl.WELCOME)

    def test_the_tool_logger_only_writes_to_the_file(self):
        from reciproca.agent import __main__ as repl

        logger = repl._tool_logger()
        try:
            self.assertFalse(logger.propagate, "must not reach the console handler")
            self.assertEqual(len(logger.handlers), 1)
            handler = logger.handlers[0]
            self.assertIsInstance(handler, logging.FileHandler)
            self.assertEqual(os.path.abspath(handler.baseFilename),
                             os.path.abspath(config.LOG_FILE))
        finally:
            logger.handlers[0].close()
            repl._tool_logger_instance = None


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
