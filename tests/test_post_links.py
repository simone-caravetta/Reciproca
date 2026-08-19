"""Checks which links the scraper treats as posts to open.

A grid tile links straight to a post. An open post also carries a link per comment,
one for its likes and its own permalink, all of which contain "/p/" without being
tiles. Collecting those queued up addresses that stop existing the moment the post
closes, which filled the log with "no such element" and wasted a pass over the grid.

The pattern is read out of POST_LINKS_JS in reciproca/selectors.py, so this cannot drift from
the shipped code.

    python3 tests/test_post_links.py
"""
import ast
import inspect
import re
import textwrap
import unittest

import _stubs  # noqa: F401  - installs the Selenium/Tkinter stubs

import reciproca as R  # noqa: E402


def tile_pattern():
    """The TILE regex from POST_LINKS_JS, as a Python one.

    The source is a Python string holding JavaScript, so its backslashes are doubled;
    JavaScript needs "\\/" where Python needs "/".
    """
    literal = re.search(r"const TILE = /(.+?)/;", R.POST_LINKS_JS).group(1)
    return re.compile(literal.replace("\\/", "/"))


class PostLinkFilterTest(unittest.TestCase):
    def setUp(self):
        self.tile = tile_pattern()

    def keeps(self, href):
        return bool(self.tile.match(href))

    def test_a_grid_tile_is_opened(self):
        self.assertTrue(self.keeps("/p/DKRv5a4RwXE/"))
        self.assertTrue(self.keeps("/p/DKRv5a4RwXE"), "trailing slash is optional")

    def test_a_tile_with_a_query_string_is_still_opened(self):
        """Dropping a real tile would look like a hashtag with no posts, which is a
        worse way to be wrong than opening one extra link."""
        self.assertTrue(self.keeps("/p/DKRv5a4RwXE/?igsh=abc123"))

    def test_the_links_inside_an_open_post_are_not(self):
        """Every one of these came out of a real run, all from the same post."""
        for href in (
            "/p/DKRv5a4RwXE/liked_by/",
            "/p/DKRv5a4RwXE/c/18084689342654807/",
            "/p/DKRv5a4RwXE/c/17867310294414369/",
            "/thenoir.film/p/DKRv5a4RwXE/",
        ):
            self.assertFalse(self.keeps(href), href)

    def test_other_links_on_the_page_are_not(self):
        for href in ("/explore/tags/photography/", "/thenoir.film/", "/reels/audio/123/"):
            self.assertFalse(self.keeps(href), href)

    def test_an_open_dialog_stops_collection_altogether(self):
        """Belt and braces: even with the pattern right, a page showing an open post
        is not the grid, so the script asks the caller to close it instead."""
        self.assertIn("div[role='dialog']", R.POST_LINKS_JS)
        self.assertIn("return null", R.POST_LINKS_JS)


class ClosePostTest(unittest.TestCase):
    """Reading the source rather than driving a browser, which these tests cannot do.
    Both checks guard a specific regression seen in a real run."""

    def setUp(self):
        self.source = inspect.getsource(R.close_post)

    def test_it_does_not_click_any_icon_it_finds(self):
        """It used to fall back to the first svg inside any button in the dialog. That
        matches the like, comment and save controls as readily as the close X, so a
        failed close could act on somebody's post instead."""
        self.assertNotIn("name()='svg'", self.source)

    def test_it_presses_escape_before_hunting_for_a_button(self):
        """Locale-independent, and it cannot press the wrong control."""
        self.assertIn("Keys.ESCAPE", self.source)

    def test_it_checks_that_the_post_actually_closed(self):
        """It used to return as soon as a click went through, reporting success while
        the post was still open, which is how the grid got read with a post over it."""
        self.assertGreaterEqual(
            self.source.count("post_dialog_open()"), 3,
            "expected a check before, after Escape, and after each button clicked",
        )


class FakeDriver:
    """Enough of a browser to watch which window gets closed and switched to."""

    def __init__(self, handles, current, fail_on=()):
        self.window_handles = list(handles)
        self.current_window_handle = current
        self.fail_on = fail_on
        self.closed = []
        self.switch_to = self

    def close(self):
        if "close" in self.fail_on:
            raise _stubs.WebDriverException("window already gone")
        self.closed.append(self.current_window_handle)
        self.window_handles.remove(self.current_window_handle)
        self.current_window_handle = None

    def window(self, handle):  # reached as driver.switch_to.window(...)
        if "switch" in self.fail_on:
            raise _stubs.WebDriverException("no such window")
        self.current_window_handle = handle


class LeaveExtraWindowTest(unittest.TestCase):
    """Getting back to the grid after reading an author's profile in its own window.

    It runs while a failure may be on its way out, so nothing it does may raise: an
    error here would replace the one being reported.
    """

    def leave(self, driver):
        R.state.driver = driver
        R.leave_extra_window("grid")
        return driver

    def test_it_closes_the_author_window_and_goes_back(self):
        driver = self.leave(FakeDriver(["grid", "author"], "author"))
        self.assertEqual(driver.closed, ["author"])
        self.assertEqual(driver.current_window_handle, "grid")

    def test_it_does_not_close_the_grid_it_is_returning_to(self):
        """Called from the grid window already, there is nothing to tidy up."""
        driver = self.leave(FakeDriver(["grid"], "grid"))
        self.assertEqual(driver.closed, [])
        self.assertEqual(driver.current_window_handle, "grid")

    def test_a_window_that_will_not_close_still_leaves_us_on_the_grid(self):
        driver = self.leave(FakeDriver(["grid", "author"], "author", fail_on=("close",)))
        self.assertEqual(driver.current_window_handle, "grid")

    def test_it_never_raises_over_the_failure_being_reported(self):
        self.leave(FakeDriver(["grid", "author"], "author", fail_on=("close", "switch")))


class TidyUpAfterAFailureTest(unittest.TestCase):
    """Every way out of a post has to leave the browser ready for the next one.

    Reading the shipped source, since neither path can be driven without a browser.
    """

    def setUp(self):
        self.source = textwrap.dedent(inspect.getsource(R.scrape_and_fill_queue))
        self.tree = ast.parse(self.source)

    def finally_calls(self):
        """Every function called in the finally of any try in the scraping loop."""
        return {
            child.func.id
            for node in ast.walk(self.tree) if isinstance(node, ast.Try)
            for stmt in node.finalbody
            for child in ast.walk(stmt)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }

    def calls_to(self, name):
        return sum(
            1 for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        )

    def test_the_post_is_closed_however_the_loop_leaves_it(self):
        self.assertIn("close_post", self.finally_calls())

    def test_closing_is_not_spelled_out_once_per_way_out(self):
        """It used to be a line on each path, and the path nobody wrote down - the
        error - left the post open over the grid for the next tile to be clicked
        through."""
        self.assertEqual(self.calls_to("close_post"), 1)

    def test_the_author_window_is_left_however_the_scrape_ends(self):
        """An extraction that raised used to leave the browser on the author's
        profile, where the next post's tile does not exist, so every post after it
        failed too."""
        self.assertIn("leave_extra_window", self.finally_calls())

    def test_the_grid_window_is_not_assumed_to_be_the_first_one(self):
        self.assertNotIn("window_handles[0]", self.source)


class BriefErrorTest(unittest.TestCase):
    """Selenium appends a chromedriver stacktrace to every message. The on-screen log
    takes the first line; the full text goes to the file through logger."""

    def test_takes_the_first_line(self):
        selenium_style = (
            "Message: no such element: Unable to locate element\n"
            "  (Session info: chrome=151.0.7922.76)\n"
            "Stacktrace:\n\tchromedriver!GetHandleVerifier [0x7ff7d7c88935+14ce5]"
        )
        self.assertEqual(
            R.brief_error(Exception(selenium_style)),
            "Message: no such element: Unable to locate element",
        )

    def test_falls_back_to_the_exception_type(self):
        self.assertEqual(R.brief_error(TimeoutError()), "TimeoutError")
        self.assertEqual(R.brief_error(ValueError("   ")), "ValueError")


if __name__ == "__main__":
    unittest.main(verbosity=2)
