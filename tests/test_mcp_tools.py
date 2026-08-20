"""Exercises the MCP server's tools against the reciproca package.

The tools are the contract the LangChain agent (milestone M4) will call, so
what is under test is the envelope - {"ok": ..., "error": ...} - and the
parameter mapping, not the core itself: the core's own behavior has its own
tests. The async cycle tools are driven with stubbed executors, so no test
here can open a browser or touch a real account.

Requires the mcp SDK (requirements-agent.txt); the class is skipped when it
is not installed, so a bare `pip install -r requirements.txt` test run still
passes.

    python3 tests/test_mcp_tools.py
"""
import json
import os
import tempfile
import time
import unittest

import _stubs  # noqa: F401  - installs the Selenium/Tkinter stubs

import reciproca as R  # noqa: E402

try:
    from reciproca import mcp_server as ms  # noqa: E402
except ImportError:
    ms = None  # mcp SDK not installed: the whole test class is skipped below

EXPECTED_TOOLS = {
    "browser_open", "browser_close", "browser_status", "login_wait",
    "follow_cycle", "cycle_status",
    "unfollow_load", "unfollow_run", "unfollow_status", "unfollow_reset",
    "queue_list", "queue_add", "queue_remove", "queue_clear", "queue_score",
    "queue_trim", "queue_import", "queue_export",
    "hashtags_list", "hashtags_add", "hashtags_remove", "hashtags_clear",
    "config_get", "config_set", "config_reset",
    "status", "logs_tail", "stop",
}


class FakeDriver:
    """Just enough of a driver for the liveness probes: window_handles only."""

    window_handles = ["window"]


@unittest.skipIf(ms is None, "mcp SDK not installed")
class MCPToolTest(unittest.TestCase):
    def reset_config(self):
        # In place, not rebound: the façade's R.CONFIG is the same dict object
        # bound at import, and a rebind would silently leave it stale.
        R.config.CONFIG.clear()
        R.config.CONFIG.update(R.config.DEFAULT_CONFIG)

    def setUp(self):
        # Redirect every file the tools can write into a scratch directory, so
        # a test run cannot touch a real queue, hashtag list, config or stop
        # flag. install_fake_ui resets the runtime state (driver, events).
        workdir = tempfile.mkdtemp()
        R.config.QUEUE_FILE = os.path.join(workdir, "follow_queue.json")
        R.config.FREQUENCIES_FILE = os.path.join(workdir, "user_frequencies.json")
        R.config.FOLLOWED_FILE = os.path.join(workdir, "followed_history.json")
        R.config.HASHTAGS_FILE = os.path.join(workdir, "hashtags.json")
        R.config.CONFIG_FILE = os.path.join(workdir, "bot_config.json")
        R.config.STOP_FLAG_FILE = os.path.join(workdir, "stop.flag")
        R.config.ACCOUNT_USERNAME_FILE = os.path.join(workdir, "account_username.json")
        _stubs.install_fake_ui()
        R.state.uf_non_followers = []
        R.state.uf_progress = {}
        self.reset_config()

    def tearDown(self):
        R.state.stop_requested.clear()
        R.state.scoring_stop.clear()
        self.reset_config()

    # ------------------------------------------------------------- registry

    def test_all_expected_tools_are_registered(self):
        import asyncio
        names = {t.name for t in asyncio.run(ms.mcp.list_tools())}
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_tool_descriptions_come_from_the_docstrings(self):
        """The agent reads these descriptions to decide which tool to call."""
        import asyncio
        tools = {t.name: t for t in asyncio.run(ms.mcp.list_tools())}
        self.assertIn("task_id", tools["follow_cycle"].description)
        self.assertIn("poll", tools["cycle_status"].description.lower())

    def test_follow_cycle_schema_exposes_the_cycle_parameters(self):
        import asyncio
        tools = {t.name: t for t in asyncio.run(ms.mcp.list_tools())}
        props = set(tools["follow_cycle"].input_schema.get("properties", {}))
        self.assertLessEqual(
            {"mode", "hashtags", "delay_min", "delay_max", "limit",
             "semantic_weight", "after_search", "headless", "auto_open",
             "score_after_search"},
            props,
        )

    # ---------------------------------------------------------- browser

    def test_browser_status_reports_a_closed_browser(self):
        result = ms.browser_status()
        self.assertTrue(result["ok"])
        self.assertFalse(result["browser_open"])
        self.assertFalse(result["logged_in"])
        self.assertFalse(result["session_running"])

    def test_browser_status_with_a_driver(self):
        R.state.driver = FakeDriver()
        result = ms.browser_status()
        self.assertTrue(result["ok"])
        self.assertTrue(result["browser_open"])
        self.assertFalse(result["rate_limited"])

    def test_browser_close_with_nothing_open(self):
        result = ms.browser_close()
        self.assertTrue(result["ok"])

    def test_browser_close_is_refused_mid_session(self):
        R.state.driver = FakeDriver()
        R.state.session_running.set()
        try:
            result = ms.browser_close()
            self.assertFalse(result["ok"])
            self.assertIn("session", result["error"].lower())
        finally:
            R.state.session_running.clear()

    def test_login_wait_without_a_browser(self):
        result = ms.login_wait(timeout_seconds=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "browser_not_open")

    def test_login_wait_returns_immediately_when_logged_in(self):
        R.state.driver = FakeDriver()
        R.state.login_completed = True
        try:
            started = time.monotonic()
            result = ms.login_wait(timeout_seconds=60)
            self.assertTrue(result["ok"])
            self.assertLess(time.monotonic() - started, 1)
        finally:
            R.state.login_completed = False

    def test_login_wait_times_out(self):
        R.state.driver = FakeDriver()
        started = time.monotonic()
        result = ms.login_wait(timeout_seconds=1)
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    # ------------------------------------------------------------ hashtags

    def test_hashtags_roundtrip(self):
        self.assertEqual(ms.hashtags_list(), {"ok": True, "error": None, "hashtags": [], "count": 0})

        added = ms.hashtags_add(["#art", "#photo"])
        self.assertTrue(added["ok"])
        self.assertEqual(added["hashtags"], ["#art", "#photo"])

        listed = ms.hashtags_list()
        self.assertEqual(listed["hashtags"], ["#art", "#photo"])

        removed = ms.hashtags_remove("#art")
        self.assertTrue(removed["ok"])
        self.assertEqual(ms.hashtags_list()["hashtags"], ["#photo"])

        cleared = ms.hashtags_clear()
        self.assertTrue(cleared["ok"])
        self.assertEqual(ms.hashtags_list()["hashtags"], [])

    def test_hashtags_remove_something_not_there(self):
        result = ms.hashtags_remove("#missing")
        self.assertFalse(result["ok"])

    def test_hashtags_add_nothing(self):
        result = ms.hashtags_add([])
        self.assertFalse(result["ok"])

    # --------------------------------------------------------------- queue

    def test_queue_roundtrip(self):
        empty = ms.queue_list()
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["total"], 0)

        added = ms.queue_add(["alice", "bob", "carol"])
        self.assertTrue(added["ok"])
        self.assertEqual(added["added"], 3)

        listed = ms.queue_list()
        self.assertEqual(listed["total"], 3)
        usernames = {entry["username"] for entry in listed["queue"]}
        self.assertEqual(usernames, {"alice", "bob", "carol"})

        removed = ms.queue_remove("alice")
        self.assertTrue(removed["ok"])
        self.assertTrue(removed["removed"])
        self.assertEqual(ms.queue_list()["total"], 2)

        ms.queue_clear()
        self.assertEqual(ms.queue_list()["total"], 0)

    def test_queue_remove_something_not_there(self):
        result = ms.queue_remove("ghost")
        self.assertTrue(result["ok"])
        self.assertFalse(result["removed"])

    def test_queue_import_and_export(self):
        workdir = tempfile.mkdtemp()
        source = os.path.join(workdir, "list.json")
        with open(source, 'w', encoding='utf-8') as f:
            json.dump(["carol", "dave"], f)

        imported = ms.queue_import(source)
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["imported"], 2)

        target = os.path.join(workdir, "out.json")
        exported = ms.queue_export(target)
        self.assertTrue(exported["ok"])
        self.assertEqual(exported["exported"], 2)
        with open(target, 'r', encoding='utf-8') as f:
            self.assertEqual({R.queue_username(i) for i in json.load(f)}, {"carol", "dave"})

    def test_queue_import_a_missing_file_fails_cleanly(self):
        result = ms.queue_import(os.path.join(tempfile.mkdtemp(), "nope.json"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_queue_trim_keeps_the_best(self):
        R.add_to_queue(["a", "b", "c"])
        R.save_frequencies(R.Counter({"a": 9, "b": 7, "c": 5}))
        R.state.last_scrape_frequencies = None
        result = ms.queue_trim(limit=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 2)

    # --------------------------------------------------------------- config

    def test_config_get_whole_table_and_one_key(self):
        whole = ms.config_get()
        self.assertTrue(whole["ok"])
        self.assertIn("DEFAULT_DELAY_MIN", whole["config"])

        one = ms.config_get(key="DEFAULT_DELAY_MIN")
        self.assertTrue(one["ok"])
        self.assertIn("DEFAULT_DELAY_MIN", one["config"])

    def test_config_get_an_unknown_key(self):
        result = ms.config_get(key="NOPE")
        self.assertFalse(result["ok"])

    def test_config_set_coerces_by_the_current_type(self):
        R.config.CONFIG["MAX_FOLLOWS_PER_SESSION"] = 50
        result = ms.config_set("MAX_FOLLOWS_PER_SESSION", "70")
        self.assertTrue(result["ok"])
        self.assertEqual(result["value"], 70)
        self.assertEqual(R.config.CONFIG["MAX_FOLLOWS_PER_SESSION"], 70)
        self.assertTrue(os.path.exists(R.config.CONFIG_FILE))

    def test_config_set_an_unknown_key(self):
        result = ms.config_set("NOPE", "x")
        self.assertFalse(result["ok"])

    def test_config_set_a_number_key_with_text(self):
        result = ms.config_set("MAX_FOLLOWS_PER_SESSION", "many")
        self.assertFalse(result["ok"])

    def test_config_reset_restores_defaults(self):
        R.config.CONFIG["DEFAULT_DELAY_MIN"] = 99
        result = ms.config_reset()
        self.assertTrue(result["ok"])
        self.assertEqual(
            R.config.CONFIG["DEFAULT_DELAY_MIN"],
            R.config.DEFAULT_CONFIG["DEFAULT_DELAY_MIN"],
        )

    # ------------------------------------------------------------ unfollow

    def test_unfollow_status_with_nothing_loaded(self):
        result = ms.unfollow_status()
        self.assertTrue(result["ok"])
        self.assertEqual(result["non_followers"], 0)
        self.assertEqual(result["remaining"], 0)

    def test_unfollow_load_bad_paths_fails_cleanly(self):
        workdir = tempfile.mkdtemp()
        result = ms.unfollow_load(
            os.path.join(workdir, "followers_1.json"),
            os.path.join(workdir, "following.json"),
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_unfollow_reset_does_not_touch_the_queue(self):
        R.add_to_queue(["alice"])
        result = ms.unfollow_reset()
        self.assertTrue(result["ok"])
        self.assertEqual(ms.queue_list()["total"], 1, "the follow queue is untouched")

    # ------------------------------------------------------------ envelope

    def test_status_aggregates_everything(self):
        R.add_to_queue(["alice"])
        R.save_hashtags(["#art"])
        result = ms.status()
        self.assertTrue(result["ok"])
        self.assertEqual(result["queue"], 1)
        self.assertEqual(result["hashtags"], ["#art"])
        self.assertEqual(result["unfollow"]["non_followers"], 0)
        self.assertIn("tasks", result)
        self.assertFalse(result["session_running"])

    def test_logs_tail_returns_recent_entries_newest_first(self):
        from reciproca.logging_sink import RECENT, clear_sinks, log
        clear_sinks()
        RECENT.clear()
        try:
            log("first line", 'info')
            log("second line", 'warning')
            result = ms.logs_tail(lines=10)
            self.assertTrue(result["ok"])
            messages = [entry["message"] for entry in result["logs"]]
            self.assertEqual(messages, ["second line", "first line"])
        finally:
            RECENT.clear()

    def test_stop_sets_the_events_and_writes_the_flag(self):
        result = ms.stop()
        self.assertTrue(result["ok"])
        self.assertTrue(R.state.stop_requested.is_set())
        self.assertTrue(R.state.scoring_stop.is_set())
        self.assertTrue(os.path.exists(R.config.STOP_FLAG_FILE))

    # ---------------------------------------------------------- validation

    def test_follow_cycle_without_a_browser(self):
        result = ms.follow_cycle(auto_open=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "browser_not_open - call browser_open first, or pass auto_open=True")

    def test_follow_cycle_rejects_a_bad_mode(self):
        result = ms.follow_cycle(mode="sideways")
        self.assertFalse(result["ok"])

    def test_follow_cycle_rejects_a_bad_after_search(self):
        result = ms.follow_cycle(after_search="maybe")
        self.assertFalse(result["ok"])

    def test_unfollow_run_without_a_browser(self):
        result = ms.unfollow_run(auto_open=False)
        self.assertFalse(result["ok"])

    def test_queue_score_without_a_browser(self):
        result = ms.queue_score()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "browser_not_open - the browser must be open to read profiles")


@unittest.skipIf(ms is None, "mcp SDK not installed")
class MCPAsyncTaskTest(unittest.TestCase):
    """The long cycles run on worker threads and report through cycle_status.

    The executors are stubbed - what is under test is the registry plumbing:
    a tool call returns a task_id, the worker runs, cycle_status tracks it to
    done with the result, and the status aggregate lists it.
    """

    def setUp(self):
        workdir = tempfile.mkdtemp()
        R.config.QUEUE_FILE = os.path.join(workdir, "follow_queue.json")
        R.config.FREQUENCIES_FILE = os.path.join(workdir, "user_frequencies.json")
        R.config.FOLLOWED_FILE = os.path.join(workdir, "followed_history.json")
        R.config.STOP_FLAG_FILE = os.path.join(workdir, "stop.flag")
        R.config.ACCOUNT_USERNAME_FILE = os.path.join(workdir, "account_username.json")
        _stubs.install_fake_ui()
        R.state.driver = FakeDriver()
        R.config.CONFIG.clear()
        R.config.CONFIG.update(R.config.DEFAULT_CONFIG)
        self._orig = {
            "follow_cycle": ms.cycles.follow_cycle,
            "unfollow_cycle": ms.cycles.unfollow_cycle,
            "make_affinity_scorer": ms.semantic.make_affinity_scorer,
        }
        with ms._tasks_lock:
            ms.TASKS.clear()

    def tearDown(self):
        ms.cycles.follow_cycle = self._orig["follow_cycle"]
        ms.cycles.unfollow_cycle = self._orig["unfollow_cycle"]
        ms.semantic.make_affinity_scorer = self._orig["make_affinity_scorer"]
        with ms._tasks_lock:
            ms.TASKS.clear()
        R.state.stop_requested.clear()
        R.state.scoring_stop.clear()
        R.config.CONFIG.clear()
        R.config.CONFIG.update(R.config.DEFAULT_CONFIG)

    def wait_done(self, task_id, timeout=10):
        """Poll cycle_status until the task is done, returning the snapshot."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = ms.cycle_status(task_id)
            self.assertTrue(snapshot["ok"])
            if snapshot["state"] == "done":
                return snapshot
            time.sleep(0.05)
        self.fail(f"task {task_id} did not finish in {timeout}s")

    def test_follow_cycle_runs_the_executor_and_reports_through_cycle_status(self):
        captured = {}

        def stub(mode="search", **kwargs):
            captured["mode"] = mode
            captured["score_after_search"] = kwargs.get("score_after_search")
            return {"ok": True, "error": None, "report": "done", "mode": mode,
                    "ranked_count": 3, "top_freq": 0.5, "followed": 2,
                    "queue_remaining": 5, "added": 3, "branch": "follow"}

        ms.cycles.follow_cycle = stub

        started = ms.follow_cycle(mode="queue", limit=10, score_after_search=False)
        self.assertTrue(started["ok"])
        task_id = started["task_id"]

        snapshot = self.wait_done(task_id)
        self.assertEqual(captured["mode"], "queue")
        self.assertFalse(captured["score_after_search"])
        self.assertEqual(snapshot["result"]["followed"], 2)
        self.assertEqual(snapshot["result"]["branch"], "follow")
        self.assertIsInstance(snapshot["last_logs"], list)

    def test_unfollow_run_runs_the_executor(self):
        def stub(delay_min=None, delay_max=None, limit=None):
            return {"ok": True, "error": None, "report": "done",
                    "processed": 4, "unfollowed": 3}

        ms.cycles.unfollow_cycle = stub

        started = ms.unfollow_run(limit=10)
        self.assertTrue(started["ok"])
        snapshot = self.wait_done(started["task_id"])
        self.assertEqual(snapshot["result"]["unfollowed"], 3)

    def test_a_failing_executor_reports_the_error_not_a_crash(self):
        def stub(**kwargs):
            raise RuntimeError("boom")

        ms.cycles.follow_cycle = stub
        started = ms.follow_cycle()
        self.assertTrue(started["ok"])
        snapshot = self.wait_done(started["task_id"])
        self.assertEqual(snapshot["state"], "done")
        self.assertIn("boom", snapshot["error"])

    def test_cycle_status_for_an_unknown_task(self):
        result = ms.cycle_status("doesnotexist")
        self.assertFalse(result["ok"])

    def test_running_tasks_show_up_in_the_status_aggregate(self):
        ms.cycles.follow_cycle = lambda **kw: {"ok": True, "error": None, "report": "done"}
        started = ms.follow_cycle()
        info = ms.status()
        task_ids = {t["task_id"] for t in info["tasks"]}
        self.assertIn(started["task_id"], task_ids)
        self.wait_done(started["task_id"])

    def test_queue_score_scores_with_a_stubbed_scorer(self):
        R.save_frequencies(R.Counter({"a": 9, "b": 7}))
        R.state.last_scrape_frequencies = None
        R.add_to_queue(["a", "b"])

        def fake_scorer(read_profile, embed, niche):
            self.assertEqual(niche, "my niche")
            return lambda username: 0.5

        ms.semantic.make_affinity_scorer = fake_scorer

        started = ms.queue_score(niche="my niche")
        self.assertTrue(started["ok"])
        snapshot = self.wait_done(started["task_id"])
        self.assertEqual(snapshot["result"], 2, "both candidates get an affinity")
        self.assertEqual(snapshot["progress"]["total"], 2)

    def test_queue_score_without_a_niche(self):
        R.state.driver = FakeDriver()
        result = ms.queue_score()
        self.assertFalse(result["ok"])
        self.assertIn("niche", result["error"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
