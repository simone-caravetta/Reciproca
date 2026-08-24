"""The Telegram frontend: settings precedence, chat-id allowlist, the
markdown-to-HTML conversion, message chunking, the log-line filter and
the wait redirect.

Pure parts only - nothing here talks to Telegram or the network. The
asyncio machinery (turn worker, log forwarder, polling loop) is the same
shape as the REPL loop and is exercised by running the bot itself.

Requires the agent stack (requirements-agent.txt); the module import is
skipped when it is not installed.
"""
import asyncio
import os
import queue
import tempfile
import unittest
from unittest import mock

import _stubs  # noqa: F401

from reciproca.agent import agent as agent_mod

try:
    import reciproca.telegram_bot as tb
except ImportError:
    tb = None


@unittest.skipIf(tb is None, "agent stack not installed")
class TelegramSettingsTest(unittest.TestCase):
    """load_settings: env wins over the file, the file over defaults."""

    def setUp(self):
        self._env = dict(os.environ)
        self._file = tb.TELEGRAM_CONFIG_FILE
        workdir = tempfile.mkdtemp()
        tb.TELEGRAM_CONFIG_FILE = os.path.join(workdir, "telegram_config.json")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        tb.TELEGRAM_CONFIG_FILE = self._file

    def test_defaults_without_a_file(self):
        self.assertEqual(tb.load_settings(),
                         {"token": "", "allowed_chat_ids": []})

    def test_the_file_is_read(self):
        with open(tb.TELEGRAM_CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write('{"token": "123:abc", "allowed_chat_ids": [42, 43]}')
        settings = tb.load_settings()
        self.assertEqual(settings["token"], "123:abc")
        self.assertEqual(settings["allowed_chat_ids"], [42, 43])

    def test_the_env_token_wins_over_the_file(self):
        with open(tb.TELEGRAM_CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write('{"token": "file-token", "allowed_chat_ids": [42]}')
        with mock.patch.dict(os.environ,
                             {"RECIPROCA_TELEGRAM_TOKEN": "env-token"}):
            settings = tb.load_settings()
        self.assertEqual(settings["token"], "env-token")
        self.assertEqual(settings["allowed_chat_ids"], [42])

    def test_a_missing_file_does_not_raise(self):
        self.assertEqual(tb.load_settings()["token"], "")


@unittest.skipIf(tb is None, "agent stack not installed")
class AllowlistTest(unittest.TestCase):
    def test_the_allowlist_opens_only_the_listed_chats(self):
        settings = {"token": "t", "allowed_chat_ids": [42, 43]}
        self.assertTrue(tb.is_allowed(42, settings))
        self.assertTrue(tb.is_allowed(43, settings))
        self.assertFalse(tb.is_allowed(7, settings))

    def test_an_empty_allowlist_lets_nobody_in(self):
        settings = {"token": "t", "allowed_chat_ids": []}
        self.assertFalse(tb.is_allowed(42, settings))

    def test_the_unauthorized_reply_shows_the_way_in(self):
        message = tb.unauthorized_message(987)
        self.assertIn("987", message)
        self.assertIn("allowed_chat_ids", message)


@unittest.skipIf(tb is None, "agent stack not installed")
class ChatTextTest(unittest.TestCase):
    def test_to_html_escapes_and_bolds(self):
        self.assertEqual(tb._to_html("**Ciao**!"), "<b>Ciao</b>!")
        self.assertEqual(tb._to_html("a < b & c"), "a &lt; b &amp; c")
        self.assertEqual(
            tb._to_html("**a < b**"),
            "<b>a &lt; b</b>",
        )

    def test_split_keeps_lines_together_and_loses_nothing(self):
        # Three long lines: each becomes its own chunk (a line is never cut
        # in half when the chunks around it would fit) and nothing is lost.
        text = "\n".join(f"riga {i} " * 500 for i in range(3))
        chunks = tb._split(text)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 3900 for c in chunks))
        self.assertEqual("".join(chunks), text)

    def test_split_hard_cuts_a_single_overlong_line(self):
        text = "a" * 5000 + " b"
        chunks = tb._split(text)
        self.assertTrue(all(len(c) <= 3900 for c in chunks))
        self.assertEqual("".join(chunks), text)

    def test_split_rebalances_a_bold_span_cut_in_half(self):
        # No spaces before the cut, and a <b> span straddling position 3900:
        # the hard cut must close the tag and reopen it on the next chunk,
        # or Telegram's HTML parser would reject the message.
        text = "a" * 3890 + "<b>X</b>" + "a" * 1000
        chunks = tb._split(text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 3900)
            self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))


@unittest.skipIf(tb is None, "agent stack not installed")
class LogForwarderTest(unittest.TestCase):
    def test_new_lines_starts_at_the_end_of_the_file(self):
        with tempfile.TemporaryDirectory() as workdir:
            path = os.path.join(workdir, "follow_bot.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("old line\n")
            forwarder = tb.LogForwarder(path)
            self.assertEqual(forwarder._new_lines(), [])
            with open(path, "a", encoding="utf-8") as f:
                f.write("new line\n")
            self.assertEqual(forwarder._new_lines(), ["new line"])

    def test_the_filter_skips_the_tool_noise_only(self):
        self.assertTrue(tb._forwardable("followed @user"))
        self.assertFalse(tb._forwardable("🔧 follow_cycle({...})"))
        self.assertFalse(tb._forwardable("📦 {ok: true}"))

    def test_prettify_strips_the_file_timestamp(self):
        line = "2026-08-23 14:22:01,123 - INFO - followed @user"
        self.assertEqual(tb._prettify(line), "followed @user")

    def test_a_batch_is_capped_in_lines_and_chars(self):
        batch = tb._format_batch([f"line {i}" for i in range(100)])
        self.assertEqual(batch.count("\n") + 1, 30)
        self.assertLessEqual(len(batch), 3800)


@unittest.skipIf(tb is None, "agent stack not installed")
class WaitRedirectTest(unittest.TestCase):
    """The wait tool reads the chat inbox instead of stdin in the bot."""

    def setUp(self):
        self._original = agent_mod._read_pending_input
        agent_mod.reset_wait_state()
        agent_mod.set_status_provider(None)
        tb._pending = queue.SimpleQueue()

    def tearDown(self):
        agent_mod._read_pending_input = self._original
        agent_mod.reset_wait_state()
        agent_mod.set_status_provider(None)
        tb._pending = queue.SimpleQueue()

    def test_a_chat_message_ends_the_wait_early(self):
        tb._install_telegram_wait()
        tb._pending.put((42, "fermati"))
        with mock.patch("reciproca.agent.agent.time.sleep") as sleep:
            result = agent_mod.wait.invoke({"seconds": 120})
        self.assertEqual(result, "Wait ended early - the user typed: fermati")
        sleep.assert_not_called()

    def test_an_empty_inbox_leaves_the_wait_alone(self):
        tb._install_telegram_wait()
        with mock.patch("reciproca.agent.agent.time.sleep") as sleep:
            result = agent_mod.wait.invoke({"seconds": 1})
        self.assertEqual(result, "Waited without interruption.")
        sleep.assert_called()

    def test_a_message_consumed_by_the_wait_is_gone_for_the_worker(self):
        tb._install_telegram_wait()
        tb._pending.put((42, "fermati"))
        agent_mod._read_pending_input()  # the wait's pop
        self.assertTrue(tb._pending.empty())

    def test_run_turn_closes_a_silent_polling_turn_with_the_status_line(self):
        # Same backstop as the REPL: a model that keeps polling without
        # narrating gets the turn closed with the current status line, so
        # the chat stays responsive and the running cycle is untouched.
        from langchain_core.messages import AIMessage
        from reciproca.agent.memory import SessionMemory

        class FakeAgent:
            def __init__(self, messages):
                self._messages = messages

            async def astream(self, config, stream_mode=None):
                for m in self._messages:
                    yield {"messages": [m]}

        polls = [
            AIMessage(id=f"m{i}", content="", tool_calls=[
                {"name": "cycle_status", "args": {}, "id": f"c{i}",
                 "type": "tool_call"}])
            for i in range(5)
        ]
        watch = agent_mod.StatusWatch()
        watch.update("Task a1b2 (follow_cycle): 2/5")
        app = mock.MagicMock()
        app.bot_data = {
            "agent": FakeAgent(polls),
            "watch": watch,
            "last_narration_at": 0.0,
        }
        agent_mod.reset_wait_state()
        with mock.patch("reciproca.telegram_bot._render") as render:
            turn = asyncio.run(tb._run_turn(app, 42, "come va?", SessionMemory()))
        self.assertEqual(len(turn), 6)  # 5 polls + the forced closing line
        closing = turn[-1]
        self.assertIsInstance(closing, AIMessage)
        self.assertIn("2/5", closing.content)

    def test_the_redirected_wait_still_reports_the_task_status(self):
        # The bot installs the status watcher the same way the REPL does:
        # the redirected wait must wake on a status change too, with the
        # user message and the status line both visible.
        tb._install_telegram_wait()
        watch = agent_mod.StatusWatch()
        watch.update("Task a1b2 (follow_cycle): 2/5")
        agent_mod.set_status_provider(watch.read)
        try:
            with mock.patch("reciproca.agent.agent.time.sleep") as sleep:
                result = agent_mod.wait.invoke({"seconds": 1})
        finally:
            agent_mod.set_status_provider(None)
        self.assertIn("Task a1b2", result)
        self.assertIn("2/5", result)
        sleep.assert_called()


if __name__ == "__main__":
    unittest.main()
