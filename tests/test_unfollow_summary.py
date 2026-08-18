"""Checks the unfollow tab reports work already done, from the first launch.

Progress is saved so unfollowing can be spread over sessions, but the summary
label used to show only the list's total until a round finished or Stop was
pressed - so on startup a half-finished job looked untouched.

    python3 tests/test_unfollow_summary.py
"""
import json
import os
import tempfile
import unittest

import _stubs  # noqa: F401  - installs the Selenium/Tkinter stubs
import reciproca as R  # noqa: E402


class LiveDriver:
    window_handles = ["window-1"]


class UnfollowSummaryTest(unittest.TestCase):
    def setUp(self):
        _stubs.install_fake_ui(R)
        R.UNFOLLOW_PROGRESS_FILE = os.path.join(tempfile.mkdtemp(), "unfollow_progress.json")
        R.uf_non_followers = [f"user{n}" for n in range(600)]
        R.uf_progress = {"processed": [], "unfollowed": [], "skipped": []}
        R.login_completed = True  # a live browser means the manual login went through

    def save_progress(self, processed, unfollowed, skipped=()):
        with open(R.UNFOLLOW_PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "processed": list(processed),
                "unfollowed": list(unfollowed),
                "skipped": list(skipped),
            }, f)

    @property
    def label(self):
        return R.uf_data_label.settings.get('text', '')

    def test_counts_split_the_list_into_done_and_left(self):
        R.uf_progress = {
            "processed": [f"user{n}" for n in range(558)],
            "unfollowed": [f"user{n}" for n in range(550)],
            "skipped": [f"user{n}" for n in range(550, 558)],
        }
        self.assertEqual(R.unfollow_progress_counts(), (600, 42, 550))

    def test_nothing_done_yet(self):
        self.assertEqual(R.unfollow_progress_counts(), (600, 600, 0))

    def test_the_summary_shows_progress_before_the_browser_is_open(self):
        """The reported bug: at startup this said only "600 non-followers"."""
        self.save_progress(
            processed=[f"user{n}" for n in range(558)],
            unfollowed=[f"user{n}" for n in range(550)],
        )

        R.update_unfollow_ui_state()

        self.assertIn("600 non-followers", self.label)
        self.assertIn("42 to process", self.label)
        self.assertIn("550 already removed", self.label)
        # No browser yet, so Start cannot work - but the counts are shown anyway.
        self.assertEqual(R.uf_start_btn.state, 'disabled')
        self.assertIn("open the browser", self.label)

    def test_an_open_browser_enables_start_and_drops_the_hint(self):
        self.save_progress(processed=["user0"], unfollowed=["user0"])
        R.driver = LiveDriver()

        R.update_unfollow_ui_state()

        self.assertEqual(R.uf_start_btn.state, 'normal')
        self.assertNotIn("open the browser", self.label)
        self.assertIn("599 to process", self.label)

    def test_without_a_loaded_list_it_asks_for_the_files(self):
        R.uf_non_followers = []
        R.driver = LiveDriver()

        R.update_unfollow_ui_state()

        self.assertIn("Load followers.json", self.label)
        self.assertEqual(
            R.uf_start_btn.state, 'disabled',
            "Start must stay off with nothing to process, browser or not",
        )

    def test_progress_for_users_no_longer_in_the_list_is_ignored(self):
        """A freshly exported following.json can drop users already processed.

        Those must not be counted against the current list, which would report
        fewer remaining than there are.
        """
        R.uf_non_followers = ["still_here", "also_here"]
        self.save_progress(
            processed=["gone_from_export", "still_here"],
            unfollowed=["gone_from_export", "still_here"],
        )

        R.update_unfollow_ui_state()

        total, remaining, removed = R.unfollow_progress_counts()
        self.assertEqual((total, remaining), (2, 1))
        self.assertEqual(removed, 2, "history keeps everything ever unfollowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
