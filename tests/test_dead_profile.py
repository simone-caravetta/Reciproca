"""The dead-profile page is spotted fast, so a deleted account costs a couple
of seconds instead of two full BROWSER_TIMEOUT retries and a wall of noise.

The marker lives in a heading (h1/h2), which is what keeps a live profile
whose bio happens to contain one of these phrases safe.
"""
import unittest

import _stubs  # noqa: F401

from reciproca.follow import _dead_profile_page


class _El:
    def __init__(self, text):
        self._text = text

    @property
    def text(self):
        return self._text


class _Driver:
    """Fake driver whose h1/h2 headings are the scripted ones."""

    def __init__(self, headings):
        self._headings = headings

    def find_elements(self, by, value):
        if value in ("h1", "h2"):
            return self._headings
        return []


class DeadProfilePageTest(unittest.TestCase):
    def test_the_classic_marker(self):
        driver = _Driver([_El("Sorry, this page isn't available.")])
        self.assertTrue(_dead_profile_page(driver))

    def test_suspended_account(self):
        driver = _Driver([_El("This account has been suspended.")])
        self.assertTrue(_dead_profile_page(driver))

    def test_rate_limit_page(self):
        driver = _Driver([_El("Please wait a few minutes before you try again.")])
        self.assertTrue(_dead_profile_page(driver))

    def test_a_live_profile_is_not_dead(self):
        driver = _Driver([_El("Some Username")])
        self.assertFalse(_dead_profile_page(driver))

    def test_a_live_profile_without_headings(self):
        self.assertFalse(_dead_profile_page(_Driver([])))

    def test_the_marker_is_case_insensitive(self):
        driver = _Driver([_El("SORRY, THIS PAGE ISN'T AVAILABLE.")])
        self.assertTrue(_dead_profile_page(driver))


if __name__ == "__main__":
    unittest.main(verbosity=2)
