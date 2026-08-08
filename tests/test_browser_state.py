"""Checks that the controls agree with whether a browser is actually open.

Closing the Chrome window used to go unnoticed: `driver` kept holding a dead
session, so Start Following stayed enabled while every click failed, and Open
Browser stayed disabled, leaving no way back short of restarting the app.

    python3 tests/test_browser_state.py
"""
import unittest

import _stubs  # noqa: F401  - installs the Selenium/Tkinter stubs
from _stubs import WebDriverException

import reciproca as R  # noqa: E402


class DeadDriver:
    """A closed browser: chromedriver rejects every command."""

    def __init__(self):
        self.quit_called = False

    @property
    def window_handles(self):
        raise WebDriverException("no such window: target window already closed")

    def quit(self):
        self.quit_called = True


class LiveDriver:
    """An open browser with one window."""

    def __init__(self):
        self.quit_called = False
        self.window_handles = ["window-1"]

    def quit(self):
        self.quit_called = True


class BrowserStateTest(unittest.TestCase):
    def setUp(self):
        _stubs.install_fake_ui(R)
        R.uf_non_followers = []

    def assert_no_browser(self):
        self.assertEqual(R.browser_btn.state, 'normal', "Open Browser must be clickable")
        self.assertEqual(R.start_btn.state, 'disabled', "Start Following must be off")

    def assert_browser_ready(self):
        self.assertEqual(R.browser_btn.state, 'disabled')
        self.assertEqual(R.start_btn.state, 'normal')

    def test_probe_reports_a_closed_browser(self):
        self.assertFalse(R.browser_is_open(), "no driver at all")

        R.driver = DeadDriver()
        self.assertFalse(R.browser_is_open(), "driver whose window is gone")

        live = LiveDriver()
        live.window_handles = []
        R.driver = live
        self.assertFalse(R.browser_is_open(), "driver with no windows left")

        R.driver = LiveDriver()
        self.assertTrue(R.browser_is_open())

    def test_controls_follow_the_browser(self):
        R.driver = LiveDriver()
        R.update_follow_ui_state()
        self.assert_browser_ready()

        R.driver = None
        R.update_follow_ui_state()
        self.assert_no_browser()

    def test_closing_the_browser_makes_the_app_usable_again(self):
        dead = DeadDriver()
        R.driver = dead

        R.handle_browser_closed()

        self.assertIsNone(R.driver, "the dead session must not be kept")
        self.assertTrue(dead.quit_called, "chromedriver must be released")
        self.assert_no_browser()

    def test_refresh_notices_a_browser_that_died_during_a_session(self):
        R.driver = DeadDriver()
        R.refresh_browser_state()
        self.assertIsNone(R.driver)
        self.assert_no_browser()

    def test_refresh_leaves_a_working_browser_alone(self):
        live = LiveDriver()
        R.driver = live
        R.refresh_browser_state()
        self.assertIs(R.driver, live)
        self.assertFalse(live.quit_called)
        self.assert_browser_ready()

    def test_the_watcher_reschedules_itself_and_skips_busy_workers(self):
        R.driver = DeadDriver()

        # A worker thread is driving the session, so the GUI must not probe it.
        R.active_threads[:] = [_AliveThread()]
        R.watch_browser()
        self.assertIsNotNone(R.driver, "must not probe while a worker is running")
        self.assertEqual(len(R.root.scheduled), 1, "must keep watching")

        # Worker finished: the next tick prunes it and notices the closed browser.
        R.active_threads[:] = [_FinishedThread()]
        R.watch_browser()
        self.assertIsNone(R.driver)
        self.assertEqual(R.active_threads, [], "finished threads must be pruned")
        self.assert_no_browser()
        self.assertEqual(len(R.root.scheduled), 2)

    def test_the_watcher_keeps_going_after_an_unexpected_error(self):
        """It reschedules from a finally, so one bad tick cannot end the watch."""
        class Exploding:
            @property
            def window_handles(self):
                raise RuntimeError("something unforeseen")

        R.driver = Exploding()
        R.active_threads[:] = []
        R.watch_browser()
        self.assertEqual(len(R.root.scheduled), 1)


class OneSessionAtATimeTest(unittest.TestCase):
    """Follow and unfollow drive the same browser window, so only one may run.

    Deep Search used to leave Start Unfollow clickable: the two sessions would
    then issue commands on one Selenium session, each navigating the page out from
    under the other, and act on whatever was loaded.
    """

    def setUp(self):
        _stubs.install_fake_ui(R)
        R.driver = LiveDriver()
        R.uf_non_followers = ["someone"]  # otherwise Start Unfollow is off anyway

    def test_both_start_buttons_are_off_while_a_session_runs(self):
        R.update_follow_ui_state()
        R.update_unfollow_ui_state()
        self.assertEqual(R.start_btn.state, 'normal', "idle: both are available")
        self.assertEqual(R.uf_start_btn.state, 'normal')

        self.assertTrue(R.begin_session())

        self.assertEqual(R.start_btn.state, 'disabled')
        self.assertEqual(R.uf_start_btn.state, 'disabled')
        self.assertEqual(R.stop_btn.state, 'normal', "Stop is how you get out")
        self.assertEqual(R.uf_stop_btn.state, 'normal')

    def test_a_second_session_is_refused(self):
        self.assertTrue(R.begin_session())
        self.assertFalse(R.begin_session(), "the browser is already claimed")

    def test_starting_unfollow_mid_session_spawns_nothing(self):
        """The guard that does not depend on a button state being right."""
        R.begin_session()

        R.run_unfollow()
        R.run_follow()

        self.assertEqual(R.active_threads, [], "no worker may be started")

    def test_finishing_hands_the_browser_back(self):
        R.begin_session()
        R.end_session()

        self.assertFalse(R.session_running.is_set())
        self.assertEqual(R.start_btn.state, 'normal')
        self.assertEqual(R.uf_start_btn.state, 'normal')
        self.assertEqual(R.stop_btn.state, 'disabled')

    def test_a_session_that_loses_the_browser_ends_with_start_off(self):
        R.begin_session()
        R.driver = DeadDriver()

        R.end_session()

        self.assertIsNone(R.driver)
        self.assertEqual(R.start_btn.state, 'disabled')
        self.assertEqual(R.browser_btn.state, 'normal')


class _AliveThread:
    def is_alive(self):
        return True


class _FinishedThread:
    def is_alive(self):
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
