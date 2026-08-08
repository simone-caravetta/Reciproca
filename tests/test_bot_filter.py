"""Checks the profile counts are read correctly and the bot verdict follows.

Both halves are pure: parsing what Instagram renders into numbers, and deciding
from three numbers whether a profile is worth following. The browser side - finding
those numbers in the header - is not covered here.

    python3 tests/test_bot_filter.py
"""
import unittest

import _stubs  # noqa: F401  - installs the Selenium/Tkinter stubs

import reciproca as R  # noqa: E402


class ParseCountTest(unittest.TestCase):
    def test_plain_digits(self):
        self.assertEqual(R.parse_count("0"), 0)
        self.assertEqual(R.parse_count("847"), 847)

    def test_thousands_separated_either_way(self):
        self.assertEqual(R.parse_count("1,234"), 1234)
        self.assertEqual(R.parse_count("1.234"), 1234)
        self.assertEqual(R.parse_count("1,234,567"), 1234567)

    def test_abbreviations_treat_the_separator_as_a_decimal_point(self):
        """"1.234" is twelve hundred more than a thousand; "1.2K" is twelve hundred.

        The multiplier is what tells them apart, so it decides how to read the
        separator.
        """
        self.assertEqual(R.parse_count("1.2K"), 1200)
        self.assertEqual(R.parse_count("1,2K"), 1200)
        self.assertEqual(R.parse_count("12.3k"), 12300)
        self.assertEqual(R.parse_count("5M"), 5000000)
        self.assertEqual(R.parse_count("1.234"), 1234)

    def test_thousands_separated_by_a_space(self):
        """Understating a count is the harmful direction - it rejects a real
        account - so "1 234" must not read as 1."""
        self.assertEqual(R.parse_count("1 234"), 1234)
        self.assertEqual(R.parse_count("1 234 followers"), 1234)

    def test_a_count_embedded_in_surrounding_text(self):
        self.assertEqual(R.parse_count("1,234 followers"), 1234)
        self.assertEqual(R.parse_count("follower\n12.3K"), 12300)

    def test_nothing_to_read(self):
        for value in (None, "", "   ", "followers", "no digits here"):
            self.assertIsNone(R.parse_count(value), repr(value))


class ParsePostsCountTest(unittest.TestCase):
    def test_finds_the_count_beside_the_word(self):
        header = "mario.rossi\n847 posts\n1,234 followers\n567 following\nPhotographer"
        self.assertEqual(R.parse_posts_count(header), 847)

    def test_italian_header(self):
        header = "mario.rossi\n847 post\n1.234 follower\n567 seguiti"
        self.assertEqual(R.parse_posts_count(header), 847)

    def test_not_taken_from_position(self):
        """The display name can start with digits, and the bio holds numbers too."""
        header = "24k.studio\n0 posts\n9 followers\n2,500 following"
        self.assertEqual(R.parse_posts_count(header), 0)

    def test_a_bio_mentioning_posting_is_not_a_count(self):
        header = "mario.rossi\nI post every day\n12 followers"
        self.assertIsNone(R.parse_posts_count(header))

    def test_no_header(self):
        self.assertIsNone(R.parse_posts_count(None))
        self.assertIsNone(R.parse_posts_count(""))


class BotVerdictTest(unittest.TestCase):
    THRESHOLDS = (
        "BOT_MIN_POSTS", "BOT_MIN_FOLLOWERS", "BOT_MAX_FOLLOWING",
        "BOT_MAX_FOLLOWING_RATIO",
    )

    def setUp(self):
        # The thresholds are settings, so pin them and put them back afterwards:
        # CONFIG is module state shared with every other test file.
        self.saved = {key: R.CONFIG[key] for key in self.THRESHOLDS}
        R.CONFIG.update({
            "BOT_MIN_POSTS": 1,
            "BOT_MIN_FOLLOWERS": 10,
            "BOT_MAX_FOLLOWING": 3000,
            "BOT_MAX_FOLLOWING_RATIO": 5,
        })

    def tearDown(self):
        R.CONFIG.update(self.saved)

    def test_a_plausible_account_passes(self):
        self.assertIsNone(R.bot_rejection_reason(posts=120, followers=800, following=450))

    def test_an_empty_gallery_is_rejected(self):
        self.assertIn("0 posts", R.bot_rejection_reason(posts=0, followers=500, following=300))

    def test_almost_nobody_following_it_is_rejected(self):
        self.assertIn("3 followers", R.bot_rejection_reason(posts=10, followers=3, following=40))

    def test_following_thousands_is_rejected(self):
        reason = R.bot_rejection_reason(posts=10, followers=4000, following=7500)
        self.assertIn("7500", reason)

    def test_the_ratio_catches_what_the_absolute_limits_miss(self):
        """Under every individual limit, but following 40x its own followers."""
        self.assertIsNone(
            R.bot_rejection_reason(posts=10, followers=50, following=200),
            "4x is within the allowance",
        )
        reason = R.bot_rejection_reason(posts=10, followers=50, following=2000)
        self.assertIn("2000", reason)
        self.assertIn("50", reason)

    def test_an_unreadable_count_never_counts_against_a_profile(self):
        """Instagram's markup shifts, and a reading that fails must let people
        through rather than reject everyone."""
        self.assertIsNone(R.bot_rejection_reason(None, None, None))
        self.assertIsNone(
            R.bot_rejection_reason(posts=None, followers=800, following=450),
            "posts unknown, the rest fine",
        )
        self.assertIsNone(
            R.bot_rejection_reason(posts=120, followers=None, following=200),
            "the ratio cannot be judged without followers",
        )

    def test_a_partial_reading_still_rejects_on_what_it_did_read(self):
        self.assertIn(
            "0 posts", R.bot_rejection_reason(posts=0, followers=None, following=None)
        )

    def test_thresholds_are_settings(self):
        R.CONFIG["BOT_MIN_POSTS"] = 0
        self.assertIsNone(
            R.bot_rejection_reason(posts=0, followers=500, following=300),
            "an empty gallery is allowed once the threshold says so",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
