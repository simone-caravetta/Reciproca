"""Checks which links the scraper treats as posts to open.

A grid tile links straight to a post. An open post also carries a link per comment,
one for its likes and its own permalink, all of which contain "/p/" without being
tiles. Collecting those queued up addresses that stop existing the moment the post
closes, which filled the log with "no such element" and wasted a pass over the grid.

The pattern is read out of POST_LINKS_JS in reciproca.py, so this cannot drift from
the shipped code.

    python3 tests/test_post_links.py
"""
import re
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
