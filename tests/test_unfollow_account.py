"""Checks the unfollow progress stays with the account it was recorded against.

Re-exporting followers.json / following.json for the same account is the normal
way to carry on, so that has to keep the progress. A different account has to
start from its own - the record of who has been processed describes one account's
following list and means nothing on another's.

Nothing is deleted on the way: each account's record is parked under its own name
and comes back if that account returns.

    python3 tests/test_unfollow_account.py
"""
import json
import os
import tempfile
import unittest

import _stubs
from _stubs import messagebox

import reciproca as R  # noqa: E402


class DriverLoggedInAs:
    """A browser session carrying one account's ds_user_id cookie."""

    window_handles = ["window-1"]

    def __init__(self, account_id):
        self.account_id = account_id

    def get_cookie(self, name):
        if name == 'ds_user_id' and self.account_id is not None:
            return {"name": name, "value": self.account_id}
        return None


class UnfollowAccountTest(unittest.TestCase):
    def setUp(self):
        _stubs.install_fake_ui(R)
        self.workdir = tempfile.mkdtemp()
        R.UNFOLLOW_PROGRESS_FILE = os.path.join(self.workdir, "unfollow_progress.json")
        R.UNFOLLOW_SESSION_FILE = os.path.join(self.workdir, "unfollow_last_session.json")
        # uf_progress_archive() builds its path through data_path()
        R.data_path = lambda name: os.path.join(self.workdir, name)
        R.uf_non_followers = ["someone"]
        R.uf_progress = {"processed": [], "unfollowed": [], "skipped": []}

    def write_progress(self, unfollowed):
        with open(R.UNFOLLOW_PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "processed": list(unfollowed),
                "unfollowed": list(unfollowed),
                "skipped": [],
            }, f)

    def read_progress(self, path=None):
        with open(path or R.UNFOLLOW_PROGRESS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_the_same_account_keeps_its_progress(self):
        """The user's actual workflow: finish a list, re-export, carry on."""
        self.write_progress(["gone1", "gone2"])
        R.uf_save_session(account_id="12345")
        R.driver = DriverLoggedInAs("12345")

        R.uf_check_account()

        self.assertEqual(self.read_progress()["unfollowed"], ["gone1", "gone2"])
        self.assertEqual(R.uf_non_followers, ["someone"], "the loaded list stands")
        self.assertEqual(messagebox.shown, [], "nothing to tell the user about")

    def test_an_unidentifiable_account_changes_nothing(self):
        """No browser, not logged in, or another domain: unknown is not 'changed'."""
        self.write_progress(["gone1"])
        R.uf_save_session(account_id="12345")

        R.driver = None
        R.uf_check_account()
        R.driver = DriverLoggedInAs(None)  # logged out: no cookie
        R.uf_check_account()

        self.assertEqual(self.read_progress()["unfollowed"], ["gone1"])
        self.assertEqual(R.uf_load_session().get("account_id"), "12345")
        self.assertEqual(messagebox.shown, [])

    def test_the_first_identified_account_adopts_the_existing_progress(self):
        """Progress recorded before this check existed belongs to whoever is logged
        in now - there is nothing else it could belong to."""
        self.write_progress(["gone1"])
        R.driver = DriverLoggedInAs("12345")

        R.uf_check_account()

        self.assertEqual(R.uf_load_session().get("account_id"), "12345")
        self.assertEqual(self.read_progress()["unfollowed"], ["gone1"])
        self.assertEqual(messagebox.shown, [])

    def test_another_account_parks_the_progress_instead_of_deleting_it(self):
        self.write_progress(["gone1", "gone2"])
        R.uf_save_session(
            account_id="12345", followers_file="/tmp/f.json", following_file="/tmp/g.json"
        )
        R.driver = DriverLoggedInAs("99999")

        R.uf_check_account()

        parked = os.path.join(self.workdir, "unfollow_progress_12345.json")
        self.assertTrue(os.path.exists(parked), "the old record must survive")
        self.assertEqual(self.read_progress(parked)["unfollowed"], ["gone1", "gone2"])
        self.assertFalse(
            os.path.exists(R.UNFOLLOW_PROGRESS_FILE),
            "the new account starts with no progress of its own",
        )

        session = R.uf_load_session()
        self.assertEqual(session.get("account_id"), "99999")
        self.assertIsNone(session.get("followers_file"), "the export was the other account's")
        self.assertEqual(R.uf_non_followers, [], "so the loaded list goes too")
        self.assertEqual([kind for kind, _, _ in messagebox.shown], ["showinfo"])

    def test_going_back_to_an_account_restores_its_progress(self):
        # Work on the first account, then switch away.
        self.write_progress(["first1", "first2"])
        R.uf_save_session(account_id="11111")
        R.driver = DriverLoggedInAs("22222")
        R.uf_check_account()

        # Do some work on the second account, then switch back.
        self.write_progress(["second1"])
        R.driver = DriverLoggedInAs("11111")
        R.uf_check_account()

        self.assertEqual(
            self.read_progress()["unfollowed"], ["first1", "first2"],
            "the first account's own record must come back",
        )
        parked = os.path.join(self.workdir, "unfollow_progress_22222.json")
        self.assertEqual(self.read_progress(parked)["unfollowed"], ["second1"])


class UnfollowResetTest(unittest.TestCase):
    def setUp(self):
        _stubs.install_fake_ui(R)
        workdir = tempfile.mkdtemp()
        R.UNFOLLOW_PROGRESS_FILE = os.path.join(workdir, "unfollow_progress.json")
        R.UNFOLLOW_SESSION_FILE = os.path.join(workdir, "unfollow_last_session.json")
        R.uf_progress_bar = _stubs.FakeWidget()
        R.uf_status_label = _stubs.FakeWidget()
        R.uf_non_followers = ["a", "b", "c"]
        with open(R.UNFOLLOW_PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump({"processed": ["a", "b"], "unfollowed": ["a"], "skipped": ["b"]}, f)

    def test_the_warning_says_what_is_lost(self):
        messagebox.answer = False  # user backs out

        R.reset_unfollow_app()

        kind, title, message = messagebox.shown[0]
        self.assertEqual(kind, "askyesno")
        self.assertIn("2 accounts already processed", message)
        self.assertIn("1 of them recorded as unfollowed", message)
        self.assertIn("cannot be undone", message)

    def test_backing_out_keeps_everything(self):
        messagebox.answer = False

        R.reset_unfollow_app()

        self.assertTrue(os.path.exists(R.UNFOLLOW_PROGRESS_FILE))
        self.assertEqual(R.uf_non_followers, ["a", "b", "c"])

    def test_confirming_clears_progress_and_session(self):
        messagebox.answer = True

        R.reset_unfollow_app()

        self.assertFalse(os.path.exists(R.UNFOLLOW_PROGRESS_FILE))
        self.assertFalse(os.path.exists(R.UNFOLLOW_SESSION_FILE))
        self.assertEqual(R.uf_non_followers, [])
        self.assertEqual(R.uf_progress, {"processed": [], "unfollowed": [], "skipped": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
