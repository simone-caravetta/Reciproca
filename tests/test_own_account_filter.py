"""The login account must never end up in the extraction or the queue.

Following an author puts our own row at the top of their followers list, so
scraping their followers can harvest the account doing the scraping - and that
row's button does not identify it as ours. The username captured from the
login form is therefore compared by name: at the popup stage, before anything
reaches the live extraction, and again at the queue funnel.

    python3 tests/test_own_account_filter.py
"""
import json
import os
import tempfile
import unittest

import _stubs  # noqa: F401  - installs the Selenium/Tkinter stubs

import reciproca as R  # noqa: E402


def save_own_username(username):
    """Write the account username file the way the login watcher does."""
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "account_username.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({"username": username, "saved_at": "now"}, f)
    return path


class PopupDriver:
    """A followers popup whose rows the extraction script reports."""

    current_url = "https://www.instagram.com/some_author/"

    def __init__(self, kept):
        self.kept = kept

    def find_element(self, by, value):
        return object()  # the dialog

    def execute_script(self, script, *args):
        if script == R.EXTRACT_FOLLOWERS_JS:
            return {
                "kept": list(self.kept),
                "skippedFollowing": 0,
                "rowsWithoutButton": 0,
                "rowsInspected": len(self.kept),
            }
        return None


class FollowersPopupFilterTest(unittest.TestCase):
    """The check at the popup stage, before anything reaches the live list."""

    def setUp(self):
        _stubs.install_fake_ui(R)
        self.workdir = tempfile.mkdtemp()
        R.FOLLOWED_FILE = os.path.join(self.workdir, "followed_history.json")
        R.stop_requested.set()  # skip the scroll loop, keep the popup read

    def test_the_login_account_is_not_extracted(self):
        R.ACCOUNT_USERNAME_FILE = save_own_username("mario.rossi")
        R.driver = PopupDriver(kept=["mario.rossi", "someone_else"])

        self.assertEqual(R.extract_users_from_followers(), ["someone_else"])

    def test_the_name_is_compared_case_insensitively(self):
        R.ACCOUNT_USERNAME_FILE = save_own_username("mario.rossi")
        R.driver = PopupDriver(kept=["Mario.Rossi", "someone_else"])

        self.assertEqual(R.extract_users_from_followers(), ["someone_else"])

    def test_nothing_is_filtered_without_a_saved_username(self):
        R.ACCOUNT_USERNAME_FILE = os.path.join(self.workdir, "missing.json")
        R.driver = PopupDriver(kept=["mario.rossi", "someone_else"])

        self.assertEqual(
            R.extract_users_from_followers(), ["mario.rossi", "someone_else"]
        )


class QueueFunnelTest(unittest.TestCase):
    """The final funnel refuses the login account from any path."""

    def setUp(self):
        _stubs.install_fake_ui(R)
        self.workdir = tempfile.mkdtemp()
        R.QUEUE_FILE = os.path.join(self.workdir, "follow_queue.json")
        R.FREQUENCIES_FILE = os.path.join(self.workdir, "user_frequencies.json")
        R.FOLLOWED_FILE = os.path.join(self.workdir, "followed_history.json")
        R.ACCOUNT_USERNAME_FILE = save_own_username("mario.rossi")

    def queued(self):
        return [R.queue_username(item) for item in R.load_queue()]

    def test_the_login_account_is_not_queued(self):
        R.add_to_queue(["mario.rossi", "someone_else"])

        self.assertEqual(self.queued(), ["someone_else"])

    def test_without_a_saved_username_everything_is_added(self):
        R.ACCOUNT_USERNAME_FILE = os.path.join(self.workdir, "missing.json")

        R.add_to_queue(["mario.rossi", "someone_else"])

        self.assertEqual(self.queued(), ["mario.rossi", "someone_else"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
