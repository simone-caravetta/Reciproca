"""The agent package: settings precedence, provider factory, prompt, and the
real end-to-end of loading tools from the live MCP server over stdio.

The tool-loading test spawns the actual server as a child process (no
browser, no account - just the tools/list handshake), which is exactly the
path the REPL takes at startup.

Requires the agent stack (requirements-agent.txt); the integration class is
skipped when it is not installed.
"""
import asyncio
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

import _stubs  # noqa: F401

from langchain_core.messages import AIMessage

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
    def setUp(self):
        # The wait tool's stale-wait counter is module state; a test that
        # calls wait four or five times must start from a clean slate.
        agent_mod.reset_wait_state()
        agent_mod.set_status_provider(None)

    def tearDown(self):
        agent_mod.reset_wait_state()
        agent_mod.set_status_provider(None)

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

    def test_the_prompt_ties_waiting_to_monitoring(self):
        # The wait doubles as the monitor: its result carries the task's
        # status, and the model must narrate changes instead of waiting
        # silently - this is what keeps local models from wait-abusing.
        self.assertIn("reports the running task's status", agent_mod.SYSTEM_PROMPT)
        self.assertIn("status line", agent_mod.SYSTEM_PROMPT)
        self.assertIn("narrate it in one short line", agent_mod.SYSTEM_PROMPT)
        self.assertIn("the user never needs to ask for an update",
                      agent_mod.SYSTEM_PROMPT)

    def test_the_prompt_carries_the_memory_and_confirmation_rules(self):
        # The "Conversation memory" block is background, not a request;
        # a bare confirmation (English or Italian) answers the open
        # question in it.
        self.assertIn("Conversation memory", agent_mod.SYSTEM_PROMPT)
        self.assertIn("never re-run what it marks as done", agent_mod.SYSTEM_PROMPT)
        self.assertIn("bare confirmation", agent_mod.SYSTEM_PROMPT)
        self.assertIn("in English or Italian", agent_mod.SYSTEM_PROMPT)
        self.assertIn("never ask the user to restate it", agent_mod.SYSTEM_PROMPT)

    def test_the_prompt_orders_reporting_done_and_stopping(self):
        self.assertIn("communicate the result and stop polling",
                      agent_mod.SYSTEM_PROMPT)

    def test_the_wait_tool_reports_the_task_status(self):
        from reciproca.agent.agent import wait

        watch = agent_mod.StatusWatch()
        watch.update("Task a1b2 (follow_cycle): 2/5 - last: followed @user")
        agent_mod.set_status_provider(watch.read)
        try:
            with mock.patch("reciproca.agent.agent.time.sleep") as sleep, \
                    mock.patch("reciproca.agent.agent._read_pending_input",
                               return_value=None):
                result = wait.invoke({"seconds": 1})
        finally:
            agent_mod.set_status_provider(None)
        self.assertIn("Task a1b2", result)
        self.assertIn("2/5", result)
        sleep.assert_called()

    def test_the_wait_wakes_on_a_status_change(self):
        from reciproca.agent.agent import wait

        # The poller bumps the generation during the wait: the wait must end
        # at once and hand the new line back, not sleep the whole duration.
        states = iter([(0, None), (1, "Task a1b2: 2/5 - ultimo: followed @user")])
        agent_mod.set_status_provider(lambda: next(states))
        try:
            with mock.patch("reciproca.agent.agent.time.sleep") as sleep, \
                    mock.patch("reciproca.agent.agent._read_pending_input",
                               return_value=None):
                result = wait.invoke({"seconds": 60})
        finally:
            agent_mod.set_status_provider(None)
        self.assertIn("status update", result)
        self.assertIn("2/5", result)
        self.assertLess(len(sleep.call_args_list), 60)

    def test_a_user_command_beats_a_status_change(self):
        from reciproca.agent.agent import wait

        watch = agent_mod.StatusWatch()
        agent_mod.set_status_provider(watch.read)
        try:
            with mock.patch("reciproca.agent.agent.time.sleep") as sleep, \
                    mock.patch("reciproca.agent.agent._read_pending_input",
                               return_value="fermati\n"):
                result = wait.invoke({"seconds": 60})
        finally:
            agent_mod.set_status_provider(None)
        self.assertIn("the user typed: fermati", result)
        sleep.assert_not_called()

    def test_stale_waits_are_capped(self):
        from reciproca.agent.agent import wait

        with mock.patch("reciproca.agent.agent.time.sleep") as sleep, \
                mock.patch("reciproca.agent.agent._read_pending_input",
                           return_value=None):
            for _ in range(4):
                wait.invoke({"seconds": 1})  # nothing changes: allowed
            blocked = wait.invoke({"seconds": 1})  # the fifth is refused
        self.assertIn("already waited 4 times", blocked)
        self.assertIn("Stop waiting", blocked)

    def test_an_informative_return_resets_the_stale_counter(self):
        from reciproca.agent.agent import wait

        watch = agent_mod.StatusWatch()
        agent_mod.set_status_provider(watch.read)
        try:
            with mock.patch("reciproca.agent.agent.time.sleep") as sleep, \
                    mock.patch("reciproca.agent.agent._read_pending_input",
                               return_value="si\n"):
                wait.invoke({"seconds": 1})  # a user message: resets
            with mock.patch("reciproca.agent.agent.time.sleep") as sleep, \
                    mock.patch("reciproca.agent.agent._read_pending_input",
                               return_value=None):
                # Four more no-change waits are allowed again: the counter
                # did not carry over.
                for _ in range(4):
                    wait.invoke({"seconds": 1})
                blocked = wait.invoke({"seconds": 1})
        finally:
            agent_mod.set_status_provider(None)
        self.assertIn("already waited 4 times", blocked)

    def test_silent_read_polls_count_as_stalls_and_a_real_action_resets(self):
        # The wait is not the only way a model can stall: polling with
        # read-only tools without ever narrating must hit the same cap.
        agent_mod.reset_wait_state()
        read = lambda: agent_mod.note_stale_tool_calls(
            [{"name": "cycle_status", "args": {}, "id": "c", "type": "tool_call"}])
        for _ in range(4):
            self.assertFalse(read())   # under the cap: allowed
        self.assertTrue(read())         # past the cap: close the turn

        # A real action breaks the streak and starts the counter over.
        self.assertFalse(agent_mod.note_stale_tool_calls(
            [{"name": "follow_cycle", "args": {}, "id": "a", "type": "tool_call"}]))
        for _ in range(4):
            self.assertFalse(read())
        self.assertTrue(read())

    def test_run_closes_a_silent_polling_turn_with_the_status_line(self):
        # The REPL runner's backstop: five silent read polls in a row end
        # the turn with the current status line, so the user gets the
        # prompt back and a stalled model cannot pin the conversation.
        from reciproca.agent import __main__ as repl

        class FakeAgent:
            def __init__(self, messages):
                self._messages = messages

            async def astream(self, config, stream_mode=None):
                for m in self._messages:
                    yield {"messages": [m]}

        agent_mod.reset_wait_state()
        polls = [
            AIMessage(id=f"m{i}", content="", tool_calls=[
                {"name": "cycle_status", "args": {}, "id": f"c{i}",
                 "type": "tool_call"}])
            for i in range(5)
        ]
        watch = agent_mod.StatusWatch()
        watch.update("Task a1b2 (follow_cycle): 2/5")
        with mock.patch("reciproca.agent.__main__.render"):
            turn = asyncio.run(repl._run(
                [("user", "come va la sessione?")], FakeAgent(polls), watch=watch))
        self.assertEqual(len(turn), 6)  # 5 polls + the forced closing line
        closing = turn[-1]
        self.assertIsInstance(closing, AIMessage)
        self.assertIn("2/5", closing.content)

    def test_a_narration_or_a_real_action_breaks_the_silent_streak(self):
        # The backstop must never fire for a turn that is actually
        # communicating: a narration resets the counter, and so does a
        # real action, so multi-phase turns are never cut off.
        from reciproca.agent import __main__ as repl

        class FakeAgent:
            def __init__(self, messages):
                self._messages = messages

            async def astream(self, config, stream_mode=None):
                for m in self._messages:
                    yield {"messages": [m]}

        def poll(i):
            return AIMessage(id=f"m{i}", content="", tool_calls=[
                {"name": "cycle_status", "args": {}, "id": f"c{i}",
                 "type": "tool_call"}])

        agent_mod.reset_wait_state()
        messages = [poll(0), poll(1),
                    AIMessage(id="m-nar", content="Sono al 40%"),
                    poll(3), poll(4), poll(5), poll(6)]
        with mock.patch("reciproca.agent.__main__.render"):
            turn = asyncio.run(repl._run(
                [("user", "vai")], FakeAgent(messages)))
        self.assertEqual(len(turn), 7)  # nothing forced: no closing line

        agent_mod.reset_wait_state()
        messages = [poll(10), poll(11),
                    AIMessage(id="m-act", content="", tool_calls=[
                        {"name": "follow_cycle", "args": {}, "id": "a1",
                         "type": "tool_call"}]),
                    poll(13), poll(14), poll(15), poll(16)]
        with mock.patch("reciproca.agent.__main__.render"):
            turn = asyncio.run(repl._run(
                [("user", "vai")], FakeAgent(messages)))
        self.assertEqual(len(turn), 7)  # the action reset the counter

    def test_status_line_builds_readable_lines(self):
        line = agent_mod.status_line({
            "task_id": "a1b2", "kind": "follow_cycle", "state": "running",
            "progress": {"done": 2, "total": 5, "current": "@user"},
        })
        self.assertIn("a1b2", line)
        self.assertIn("2/5", line)
        self.assertIn("@user", line)

        done = agent_mod.status_line({
            "task_id": "a1b2", "kind": "follow_cycle", "state": "done",
            "result": {"followed": 5, "skipped": 2, "errors": 0},
        })
        self.assertIn("completed", done)
        self.assertIn("followed=5", done)

        error = agent_mod.status_line({
            "task_id": "a1b2", "kind": "follow_cycle", "state": "running",
            "error": "rate_limited",
        })
        self.assertIn("failed", error)

    def test_status_line_falls_back_to_the_last_log_line(self):
        line = agent_mod.status_line({
            "task_id": "a1b2", "kind": "follow_cycle", "state": "running",
            "progress": None,
            "last_logs": [{"time": 1, "level": "INFO", "message": "followed @user"}],
        })
        self.assertIn("followed @user", line)

    def test_status_line_rejects_garbage(self):
        self.assertIsNone(agent_mod.status_line(None))
        self.assertIsNone(agent_mod.status_line("not a snapshot"))

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
class LogIsolationTest(unittest.TestCase):
    def test_the_test_suite_logs_outside_the_real_app_log(self):
        # _stubs redirects RECIPROCA_LOG_FILE before reciproca is imported:
        # a test run must never write into the log the agent reads through
        # logs_tail. When it did (the "mario.rossi" fixture sessions), the
        # agent reported phantom logins and artificial errors as if they
        # had really happened.
        self.assertEqual(config.LOG_FILE,
                         os.path.join(_stubs.TEST_LOG_DIR, "follow_bot.log"))
        self.assertIn("reciproca-tests-", config.LOG_FILE)


class StatusMonitorResilienceTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_bad_poll_or_snapshot_never_kills_the_monitor(self):
        # The monitor is what wakes the wait on every change: if it dies,
        # the turn hangs silent. A raising poll and a malformed snapshot
        # must be skipped, never fatal.
        watch = agent_mod.StatusWatch()
        calls = {"n": 0}

        async def fake_snapshot(session):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("server hiccup")
            if calls["n"] == 2:
                # last_logs as a list of strings: the old key building
                # would crash on ".get" and kill the monitor here.
                return {"task_id": "a1b2", "kind": "follow_cycle",
                        "state": "running", "progress": None,
                        "last_logs": ["not a dict"]}
            if calls["n"] == 3:
                # A None snapshot is what cycle_status answers with an
                # error looks like: the monitor must skip it, not crash
                # on snapshot.get while building the key.
                return None
            return {"task_id": "a1b2", "kind": "follow_cycle",
                    "state": "running", "progress": {"done": 1, "total": 5},
                    "last_logs": [{"message": "followed @user"}]}

        with mock.patch.object(agent_mod, "_current_task_snapshot",
                               fake_snapshot):
            task = asyncio.create_task(agent_mod.status_monitor(
                session=None, watch=watch, interval=0.001))
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        # The monitor survived both bad ticks and delivered the real status.
        generation, line = watch.read()
        self.assertGreater(generation, 0)
        self.assertIsNotNone(line)
        self.assertIn("1/5", line)


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

    def test_status_snapshot_reads_the_live_server(self):
        import asyncio

        async def go():
            client = MultiServerMCPClient(agent_mod.mcp_connections())
            async with client.session("reciproca") as session:
                # No task runs in the test: the snapshot is None (idle) and
                # the monitor keeps the watch untouched.
                snapshot = await agent_mod._current_task_snapshot(session)
                watch = agent_mod.StatusWatch()
                monitor = asyncio.create_task(
                    agent_mod.status_monitor(session, watch, interval=1.0))
                await asyncio.sleep(2.5)
                monitor.cancel()
                await asyncio.sleep(0)
                return snapshot, watch.read()

        snapshot, (generation, line) = asyncio.run(go())
        self.assertIsNone(snapshot)
        self.assertEqual((generation, line), (0, None),
                         "idle: the watch must stay untouched")


class StatusPusherTest(unittest.TestCase):
    """The shared push logic: a status line is reported at most once, and
    a task finishing while no turn runs wakes the agent up (the bug it
    fixes: the 'completed' line was re-sent every ~30s forever)."""

    def setUp(self):
        self.pusher = agent_mod.StatusPusher()
        self.now = 1000.0

    def test_a_line_is_pushed_once(self):
        line = "Task t1 (follow): 2/5"
        self.assertEqual(self.pusher.tick(line, False, 0.0, self.now), "push")
        # the same line again, however long later: never repeated
        self.assertEqual(self.pusher.tick(line, False, 0.0, self.now + 600), "skip")

    def test_a_final_line_wakes_up_once(self):
        line = "Task t1 (follow) completed - followed=40"
        self.assertEqual(self.pusher.tick(line, False, 0.0, self.now), "wakeup")
        # the wake-up (and the line) are never repeated
        self.assertEqual(self.pusher.tick(line, False, 0.0, self.now + 600),
                         "skip")

    def test_busy_with_fresh_narration_marks_the_line_seen(self):
        line = "Task t1 (follow): 2/5"
        # the model is narrating right now: the line is already covered
        self.assertEqual(self.pusher.tick(line, True, self.now - 10, self.now),
                         "skip")
        # and once the turn ends it is not re-sent either
        self.assertEqual(self.pusher.tick(line, False, 0.0, self.now + 100),
                         "skip")

    def test_busy_but_silent_still_pushes(self):
        # the model has not narrated for over the silence window: the
        # guarantee kicks in even mid-turn
        line = "Task t1 (follow): 2/5"
        self.assertEqual(self.pusher.tick(line, True, self.now - 60, self.now),
                         "push")

    def test_throttle_skips_a_line_pushed_recently(self):
        line = "Task t1 (follow): 2/5"
        self.assertEqual(self.pusher.tick(line, False, 0.0, self.now), "push")
        # a new line within the throttle window: held back
        other = "Task t1 (follow): 3/5"
        self.assertEqual(self.pusher.tick(other, False, 0.0, self.now + 10),
                         "skip")
        # after the window it goes out
        self.assertEqual(self.pusher.tick(other, False, 0.0, self.now + 31),
                         "push")

    def test_final_line_while_idle_wakes_the_agent(self):
        line = "Task t1 (follow) completed - followed=40, skipped=12"
        self.assertEqual(self.pusher.tick(line, False, 0.0, self.now), "wakeup")

    def test_failed_line_while_idle_wakes_the_agent(self):
        line = "Task t1 (follow) failed: rate_limited"
        self.assertEqual(self.pusher.tick(line, False, 0.0, self.now), "wakeup")

    def test_progress_line_while_idle_is_a_plain_push(self):
        line = "Task t1 (follow): 2/5"
        self.assertEqual(self.pusher.tick(line, False, 0.0, self.now), "push")

    def test_final_line_while_busy_is_a_plain_push(self):
        # the turn is running: the model will narrate the result itself,
        # no wake-up needed - but the silent-turn guarantee still applies
        line = "Task t1 (follow) completed"
        self.assertEqual(self.pusher.tick(line, True, self.now - 60, self.now),
                         "push")

    def test_is_final_status_only_for_the_task_tag(self):
        self.assertTrue(agent_mod.is_final_status(
            "Task t1 (follow) completed - followed=40"))
        self.assertTrue(agent_mod.is_final_status(
            "Task t1 (follow) failed: rate_limited"))
        self.assertFalse(agent_mod.is_final_status("Task t1 (follow): 2/5"))
        # a running task whose last log line mentions "completed"
        # must not look final
        self.assertFalse(agent_mod.is_final_status(
            "Task t1 (follow) in progress - last: completed the login"))
        self.assertFalse(agent_mod.is_final_status(None))

    def test_wakeup_message_carries_the_result_line(self):
        msg = agent_mod.wakeup_message("Task t1 (queue_score) completed - 120")
        self.assertIn("Task t1 (queue_score) completed - 120", msg)
        self.assertIn("Riferisci il risultato", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
