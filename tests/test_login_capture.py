"""The login form is the one place the account username is typed in by hand.

When the browser's saved profile has logged out, Instagram shows the login page
and the user types the username there; nothing else in the app can learn it.
watch_login_username() reads that field and saves the value when the form goes
away, i.e. the login went through.

    python3 tests/test_login_capture.py
"""
import json
import os
import tempfile
import threading
import unittest

import _stubs  # noqa: F401 - installs the Selenium/Tkinter stubs
from _stubs import WebDriverException

import reciproca as R  # noqa: E402


class Field:
    """A login form's username input."""

    def __init__(self, value):
        self._value = value

    def get_attribute(self, name):
        return self._value


# The username field as Instagram renders it today: name="email" (the same box
# holds username, email or phone) and autocomplete="username webauthn".
INSTAGRAM_USERNAME_ATTRS = {
    "name": "email",
    "autocomplete": "username webauthn",
    "type": "text",
}


def _selector_matches(selector, attrs):
    """Minimal CSS attribute matching for the selectors in LOGIN_USERNAME_SELECTORS."""
    if not (selector.startswith("input[") and selector.endswith("]")):
        return False
    expr = selector[len("input["):-1]
    for op in ("^=", "="):
        if op in expr:
            attr, value = expr.split(op, 1)
            actual = attrs.get(attr)
            if actual is None:
                return False
            return actual.startswith(value.strip("'\"")) if op == "^=" else actual == value.strip("'\"")
    return False


class LoginPageDriver:
    """A browser sitting on the Instagram login page.

    `session_cookie` is None while the login form is up, and becomes a value
    once the login has gone through - the fake of the ds_user_id cookie.
    """

    def __init__(self, username="", form_goes_away_after=None, attrs=None,
                 label_for=None, session_cookie=None):
        self.window_handles = ["window-1"]
        self.username = username
        self.reads = 0
        self._form_goes_away_after = form_goes_away_after
        self.attrs = attrs if attrs is not None else dict(INSTAGRAM_USERNAME_ATTRS)
        self.label_for = label_for
        self.session_cookie = session_cookie

    def find_element(self, by, value):
        self.reads += 1
        if self._form_goes_away_after is not None and self.reads > self._form_goes_away_after:
            raise R.NoSuchElementException("login form gone")
        if by == R.By.CSS_SELECTOR:
            if not _selector_matches(value, self.attrs):
                raise R.NoSuchElementException(f"no element for {value}")
            return Field(self.username)
        if by == R.By.XPATH:
            # The login XPath falls back on the label's for->id link. The fake
            # driver mirrors it: an input is found when a login label points at
            # its id.
            if self.label_for and self.attrs.get("id") == self.label_for:
                return Field(self.username)
            raise R.NoSuchElementException(f"no element for {value}")
        raise R.NoSuchElementException(f"unsupported by {by}")

    def get_cookie(self, name):
        if name == 'ds_user_id' and self.session_cookie is not None:
            return {'value': self.session_cookie}
        return None


class NoLoginPageDriver:
    """A browser that is not showing the login page (already logged in)."""

    def __init__(self, session_cookie="59859152535"):
        self.window_handles = ["window-1"]
        self.session_cookie = session_cookie

    def find_element(self, by, value):
        raise R.NoSuchElementException("no login form")

    def get_cookie(self, name):
        if name == 'ds_user_id' and self.session_cookie is not None:
            return {'value': self.session_cookie}
        return None


class LoginCaptureTest(unittest.TestCase):
    def setUp(self):
        _stubs.install_fake_ui(R)
        self.tmpdir = tempfile.TemporaryDirectory()
        R.ACCOUNT_USERNAME_FILE = os.path.join(self.tmpdir.name, "account_username.json")
        R.uf_non_followers = ["someone"]  # otherwise Start Unfollow is off anyway

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_username_is_saved_and_loadable(self):
        R.save_login_username("mario.rossi")

        with open(R.ACCOUNT_USERNAME_FILE, encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data["username"], "mario.rossi")
        self.assertIn("saved_at", data)
        self.assertEqual(R.load_account_username(), "mario.rossi")

    def test_no_saved_username_yet_returns_none(self):
        self.assertIsNone(R.load_account_username())

    def test_saving_updates_the_account_label(self):
        R.update_account_label()
        self.assertEqual(R.account_label.settings['text'], '👤 Account: —')

        R.save_login_username("mario.rossi")
        self.assertEqual(R.account_label.settings['text'], '👤 Account: mario.rossi')

    def test_reading_the_login_field(self):
        R.driver = LoginPageDriver(username="mario.rossi")
        self.assertEqual(R.read_login_username(), "mario.rossi")

    def test_it_finds_the_field_with_instagram_current_markup(self):
        """The real field has name="email" and autocomplete="username webauthn"."""
        R.driver = LoginPageDriver(username="mario.rossi")
        self.assertEqual(R.read_login_username(), "mario.rossi")

    def test_it_still_finds_the_older_markup_with_name_username(self):
        R.driver = LoginPageDriver(
            username="mario.rossi",
            attrs={"name": "username", "autocomplete": "username", "type": "text"},
        )
        self.assertEqual(R.read_login_username(), "mario.rossi")

    def test_it_ignores_a_field_that_is_not_a_login_username(self):
        """A page can contain other text inputs - they must not be captured."""
        R.driver = LoginPageDriver(
            username="mario.rossi",
            attrs={"name": "search", "autocomplete": "off", "type": "text"},
        )
        self.assertIsNone(R.read_login_username())

    def test_it_falls_back_to_the_label_for_link_when_attributes_are_renamed(self):
        """If Instagram renames name/autocomplete, the label still points at the
        input through its for attribute, and the XPath fallback finds it."""
        R.driver = LoginPageDriver(
            username="mario.rossi",
            attrs={"id": "_R_32d9lplcldcpbn6b5ipamH1_", "type": "text"},
            label_for="_R_32d9lplcldcpbn6b5ipamH1_",
        )
        self.assertEqual(R.read_login_username(), "mario.rossi")

    def test_empty_field_is_not_the_form_gone(self):
        R.driver = LoginPageDriver(username="")
        self.assertEqual(R.read_login_username(), "")

    def test_no_login_page_reads_as_none(self):
        R.driver = NoLoginPageDriver()
        self.assertIsNone(R.read_login_username())

    def test_watch_saves_the_username_as_soon_as_it_is_typed(self):
        R.driver = LoginPageDriver(username="mario.rossi", form_goes_away_after=2)

        def close_the_browser():
            R.driver = None

        threading.Timer(2, close_the_browser).start()
        thread = threading.Thread(target=R.watch_login_username, daemon=True)
        thread.start()
        thread.join(10)

        self.assertFalse(thread.is_alive(), "watcher must stop when the browser closes")
        self.assertEqual(R.load_account_username(), "mario.rossi")

    def test_start_stays_disabled_while_the_login_form_is_up(self):
        """A browser sitting on the login page cannot follow anyone."""
        R.driver = LoginPageDriver(username="mario.rossi")  # no session cookie yet
        R.update_follow_ui_state()
        R.update_unfollow_ui_state()
        self.assertEqual(R.start_btn.state, 'disabled')

        # The watcher must not flip the button while the cookie is missing.
        def close_the_browser():
            R.driver = None

        threading.Timer(1, close_the_browser).start()
        thread = threading.Thread(target=R.watch_login_username, daemon=True)
        thread.start()
        thread.join(10)

        self.assertFalse(R.login_completed)
        self.assertEqual(R.start_btn.state, 'disabled')
        self.assertEqual(R.uf_start_btn.state, 'disabled', "unfollow waits for the login too")

    def test_start_enables_only_after_the_login_goes_through(self):
        """The session cookie appears only after a successful login - Start
        Following must wait for it, not for the username being typed."""
        class LoggingInDriver:
            """The form is up at first, then the user logs in and the cookie
            appears while the form goes away."""

            def __init__(self):
                self.window_handles = ["window-1"]
                self.reads = 0

            def find_element(self, by, value):
                self.reads += 1
                if self.reads > 2:
                    raise R.NoSuchElementException("login form gone")
                return Field("mario.rossi")

            def get_cookie(self, name):
                if name == 'ds_user_id' and self.reads > 2:
                    return {'value': '59859152535'}
                return None

        R.driver = LoggingInDriver()
        R.update_follow_ui_state()
        self.assertEqual(R.start_btn.state, 'disabled')

        def close_the_browser():
            R.driver = None

        threading.Timer(3, close_the_browser).start()
        thread = threading.Thread(target=R.watch_login_username, daemon=True)
        thread.start()
        thread.join(10)

        self.assertTrue(R.login_completed)
        self.assertEqual(R.start_btn.state, 'normal')
        self.assertEqual(R.uf_start_btn.state, 'normal', "unfollow enables with the login too")
        self.assertEqual(R.load_account_username(), "mario.rossi")

    def test_a_stale_session_cookie_does_not_enable_start(self):
        """The profile keeps ds_user_id after the session expired server-side:
        the login page showing means logged out, cookie or not."""
        class StaleCookieDriver:
            """The login form is up the whole time, with an old session cookie
            still sitting in the profile."""

            def __init__(self):
                self.window_handles = ["window-1"]
                self.reads = 0

            def find_element(self, by, value):
                self.reads += 1
                return Field("mario.rossi")

            def get_cookie(self, name):
                if name == 'ds_user_id':
                    return {'value': '59859152535'}
                return None

        R.driver = StaleCookieDriver()
        R.update_follow_ui_state()
        R.update_unfollow_ui_state()
        self.assertEqual(R.start_btn.state, 'disabled')

        def close_the_browser():
            R.driver = None

        threading.Timer(1, close_the_browser).start()
        thread = threading.Thread(target=R.watch_login_username, daemon=True)
        thread.start()
        thread.join(10)

        self.assertFalse(R.login_completed)
        self.assertEqual(R.start_btn.state, 'disabled')
        self.assertEqual(R.uf_start_btn.state, 'disabled', "unfollow waits for the login too")

    def test_start_enables_when_the_form_goes_away_even_with_a_stale_cookie(self):
        """The cookie was already there from an old session - what matters is
        the form going away, which is the login going through."""
        class StaleCookieLoggingInDriver:
            def __init__(self):
                self.window_handles = ["window-1"]
                self.reads = 0

            def find_element(self, by, value):
                self.reads += 1
                if self.reads > 2:
                    raise R.NoSuchElementException("login form gone")
                return Field("mario.rossi")

            def get_cookie(self, name):
                if name == 'ds_user_id':
                    return {'value': '59859152535'}
                return None

        R.driver = StaleCookieLoggingInDriver()
        R.update_follow_ui_state()
        self.assertEqual(R.start_btn.state, 'disabled')

        def close_the_browser():
            R.driver = None

        threading.Timer(3, close_the_browser).start()
        thread = threading.Thread(target=R.watch_login_username, daemon=True)
        thread.start()
        thread.join(10)

        self.assertTrue(R.login_completed)
        self.assertEqual(R.start_btn.state, 'normal')
        self.assertEqual(R.uf_start_btn.state, 'normal', "unfollow enables with the login too")
        self.assertEqual(R.load_account_username(), "mario.rossi")

    def test_watch_saves_nothing_when_already_logged_in(self):
        R.driver = NoLoginPageDriver()

        def close_the_browser():
            R.driver = None

        threading.Timer(0.5, close_the_browser).start()
        thread = threading.Thread(target=R.watch_login_username, daemon=True)
        thread.start()
        thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertIsNone(R.load_account_username(), "nothing was typed, nothing is saved")

    def test_watch_stops_when_the_browser_closes(self):
        R.driver = LoginPageDriver(username="mario.rossi")

        def close_the_browser():
            R.driver = None

        threading.Timer(0.5, close_the_browser).start()
        thread = threading.Thread(target=R.watch_login_username, daemon=True)
        thread.start()
        thread.join(10)

        self.assertFalse(thread.is_alive())

    def test_watch_updates_the_username_if_the_user_corrects_it(self):
        """The typed value can be edited before login - the file must follow."""
        class EditableDriver:
            """First reads "mario", later reads "mario.rossi" - like a user
            correcting the field before pressing Accedi. Never logs in."""

            def __init__(self):
                self.window_handles = ["window-1"]
                self.reads = 0

            def find_element(self, by, value):
                self.reads += 1
                return Field("mario" if self.reads < 3 else "mario.rossi")

            def get_cookie(self, name):
                return None

        driver = EditableDriver()
        R.driver = driver
        R.ACCOUNT_USERNAME_FILE = os.path.join(self.tmpdir.name, "account_username.json")

        def close_the_browser():
            R.driver = None

        threading.Timer(3, close_the_browser).start()
        thread = threading.Thread(target=R.watch_login_username, daemon=True)
        thread.start()
        thread.join(10)

        self.assertEqual(R.load_account_username(), "mario.rossi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
