"""The waits that keep the app from looking like a machine, and the stop button.

The long pauses - the break between hashtags, the cooldowns - exist so Instagram
never sees a machine. They used to be plain time.sleep() calls, so pressing Stop
during one still left the user waiting out the whole pause before the loop
noticed. The pauses now sleep in one-second slices and give up as soon as Stop
is requested: the current operation still finishes, only the pause before the
next one is skipped.

    python3 tests/test_pause.py
"""
import threading
import time
import unittest

import _stubs  # noqa: F401  - installs the Selenium/Tkinter stubs

import reciproca as R  # noqa: E402


class PauseTest(unittest.TestCase):
    def setUp(self):
        _stubs.install_fake_ui()

    def test_it_waits_the_requested_time(self):
        start = time.monotonic()
        R.pause(0.3)
        self.assertGreaterEqual(time.monotonic() - start, 0.25)

    def test_a_stop_request_skips_the_wait_altogether(self):
        R.state.stop_requested.set()
        start = time.monotonic()
        R.pause(30)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_a_stop_during_the_wait_cuts_it_short(self):
        """Stop pressed while the hashtag break is running, as it usually is."""
        threading.Timer(0.2, R.state.stop_requested.set).start()
        start = time.monotonic()
        R.pause(30)
        self.assertLess(time.monotonic() - start, 5, "must not wait out the pause")

    def test_zero_is_zero(self):
        start = time.monotonic()
        R.pause(0)
        self.assertLess(time.monotonic() - start, 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
