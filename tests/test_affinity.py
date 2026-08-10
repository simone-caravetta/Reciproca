"""Checks how close a profile reads to the niche you described.

Two vectors in, one number out, plus the rules about what happens when there is
nothing to compare. Turning text into a vector is not covered here: that needs the
model, and this file is meant to run on the standard library alone.

    python3 tests/test_affinity.py
"""
import os
import unittest

import _stubs  # noqa: F401  - installs the Selenium/Tkinter stubs

import reciproca as R  # noqa: E402


class CosineTest(unittest.TestCase):
    def test_the_same_direction(self):
        self.assertAlmostEqual(R.cosine([1, 0, 0], [1, 0, 0]), 1.0)
        self.assertAlmostEqual(R.cosine([1, 2, 3], [2, 4, 6]), 1.0, msg="length is not direction")

    def test_at_right_angles(self):
        self.assertAlmostEqual(R.cosine([1, 0], [0, 1]), 0.0)

    def test_opposite(self):
        self.assertAlmostEqual(R.cosine([1, 0], [-1, 0]), -1.0)

    def test_closer_is_a_higher_number(self):
        niche = [1, 1, 0]
        near, far = [1, 0.9, 0], [0, 0.2, 1]
        self.assertGreater(R.cosine(near, niche), R.cosine(far, niche))

    def test_nothing_to_compare(self):
        for left, right in (([], [1]), ([1], []), (None, [1]), ([1], None), ([0, 0], [1, 1])):
            self.assertIsNone(R.cosine(left, right), (left, right))

    def test_vectors_of_different_lengths(self):
        """A mismatch means two different models, or a half-written file. Either
        way it is not a small number, it is no answer."""
        self.assertIsNone(R.cosine([1, 0, 0], [1, 0]))


class AffinityBetweenTest(unittest.TestCase):
    def test_it_lands_between_zero_and_one(self):
        self.assertAlmostEqual(R.affinity_between([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(R.affinity_between([1, 0], [0, 1]), 0.0)

    def test_pointing_away_is_floored_at_zero(self):
        """Below zero the number stops saying "less like the niche" and starts
        saying something about the model's own geometry."""
        self.assertEqual(R.affinity_between([1, 0], [-1, 0]), 0.0)

    def test_no_answer_stays_no_answer(self):
        self.assertIsNone(R.affinity_between([], [1, 0]))


class ProfileDescriptionTest(unittest.TestCase):
    """What a profile says about itself, taken out of the header's run of text.

    REAL is the header of a real profile, printed by check_profile.py, which is
    what settled where the bio sits: after the last count, before the buttons, with
    the names of the highlight covers trailing along behind them.
    """

    REAL = "\n".join([
        "iinkandlight",
        "Elif Aslan",
        "60 post",
        "975 follower",
        "929 seguiti",
        "Fotografo",
        "canon eos rp 7",
        "\u3147\u3145\u3147",
        "film \u2022 photo \u2022 drawing",
        "Account seguito da shotsby.eagles, ellajam.media + altri 34",
        "Segui",
        "Messaggio",
        "\U0001f338\U0001f469",
        "M O V \u0130 E",
        "my drawings",
        "kamera arkas\u0131",
    ])

    def test_the_real_profile(self):
        described = R.profile_description(self.REAL)
        self.assertEqual(
            described,
            "Fotografo\ncanon eos rp 7\n\u3147\u3145\u3147\nfilm \u2022 photo \u2022 drawing",
        )

    def test_the_username_and_the_counts_are_left_out(self):
        described = R.profile_description(self.REAL)
        for unwanted in ("iinkandlight", "60 post", "975 follower", "929 seguiti"):
            self.assertNotIn(unwanted, described)

    def test_the_people_in_common_are_left_out(self):
        """Other people's names say nothing about this profile, and the line carries
        a number that would read as part of the bio."""
        self.assertNotIn("shotsby.eagles", R.profile_description(self.REAL))

    def test_the_highlight_covers_are_left_out(self):
        """They sit past the buttons: a list of holiday names would drown the bio."""
        for cover in ("M O V", "my drawings", "kamera"):
            self.assertNotIn(cover, R.profile_description(self.REAL))

    def test_a_profile_with_no_people_in_common(self):
        header = "someone\n12 posts\n300 followers\n250 following\nPhotographer\nfilm only\nFollow\nMessage"
        self.assertEqual(R.profile_description(header), "Photographer\nfilm only")

    def test_a_profile_you_already_follow(self):
        """The buttons read differently, and the bio still has to end at them."""
        header = "someone\n12 posts\n300 followers\n250 following\nfilm only\nFollowing\nMessage"
        self.assertEqual(R.profile_description(header), "film only")

    def test_a_bio_that_says_follow_me(self):
        """Whole lines are matched against the buttons, not searched for inside
        them, or this bio would stop at its first word."""
        header = "someone\n12 posts\n300 followers\n250 following\nseguimi su youtube\nvideo ogni giorno\nSegui"
        self.assertEqual(
            R.profile_description(header), "seguimi su youtube\nvideo ogni giorno"
        )

    def test_the_counts_on_a_single_line(self):
        header = "someone\n12 posts 300 followers 250 following\nPhotographer\nFollow"
        self.assertEqual(R.profile_description(header), "Photographer")

    def test_a_profile_with_nothing_written_on_it(self):
        header = "someone\n12 posts\n300 followers\n250 following\nFollow\nMessage"
        self.assertIsNone(R.profile_description(header))

    def test_a_header_this_code_does_not_understand(self):
        """No counts in it means it is not the header this expects. That is a thing
        to say nothing about, not a thing to guess at."""
        for header in ("", None, "just some words\nand some more"):
            self.assertIsNone(R.profile_description(header), repr(header))


class ProfileTextTest(unittest.TestCase):
    def test_everything_readable_goes_in(self):
        text = R.profile_text(name="Elif Aslan", category="Fotografo", bio="canon eos rp")
        for part in ("Elif Aslan", "Fotografo", "canon eos rp"):
            self.assertIn(part, text)

    def test_the_category_comes_first(self):
        """Instagram's own label, picked from a fixed list, so it says what somebody
        does in the same words every time. The bio says it in theirs."""
        text = R.profile_text(name="Elif", category="Fotografo", bio="o x o")
        self.assertLess(text.index("Fotografo"), text.index("o x o"))

    def test_a_profile_with_only_a_bio(self):
        self.assertEqual(R.profile_text(bio="film photography"), "film photography")

    def test_a_profile_with_nothing_written_on_it(self):
        """Common, and perfectly good. It has to come back unscored rather than
        scored badly: nobody can judge it, which is not the same as judging it."""
        for empty in ((None, None, None), ("", "", ""), ("  ", None, "\n")):
            self.assertIsNone(R.profile_text(*empty), empty)


class AffinityScorerTest(unittest.TestCase):
    """The function score_queue() calls, with the browser and the model handed in."""

    PROFILES = {
        "photographer": ("Elif", "Fotografo", "pellicola, ritratti, analogico"),
        "dropshipper": ("Deals", "Negozio", "sconti, offerte, link in bio"),
        "empty": (None, None, None),
    }

    def read_profile(self, username):
        return self.PROFILES[username]

    def embed(self, text):
        """A stand-in model: two words in common is a small angle.

        Enough to check the wiring, and it makes what the scorer is doing legible
        without a hundred megabytes on disk.
        """
        vocabulary = ["pellicola", "ritratti", "analogico", "sconti", "offerte", "negozio"]
        lowered = (text or "").lower()
        vector = [1.0 if word in lowered else 0.0 for word in vocabulary]
        return vector if any(vector) else None

    def scorer(self, niche="fotografi che scattano su pellicola e fanno ritratti"):
        return R.make_affinity_scorer(self.read_profile, self.embed, niche)

    def test_a_profile_in_the_niche_scores_above_one_outside_it(self):
        score = self.scorer()
        self.assertGreater(score("photographer"), score("dropshipper"))

    def test_a_profile_with_nothing_written_on_it_is_not_scored(self):
        self.assertIsNone(self.scorer()("empty"))

    def test_no_niche_written_means_nobody_is_scored(self):
        """Not everybody scored zero: without a niche there is no question being
        asked, so there is no answer to record against anyone."""
        for niche in (None, "", "   "):
            self.assertIsNone(R.make_affinity_scorer(self.read_profile, self.embed, niche))

    def test_a_model_that_cannot_read_the_niche_means_nobody_is_scored(self):
        self.assertIsNone(
            R.make_affinity_scorer(self.read_profile, lambda text: None, "una nicchia")
        )

    def test_the_niche_is_read_once_and_not_once_per_candidate(self):
        """It does not change while a pass runs, and it costs what a profile costs."""
        embedded = []

        def counting_embed(text):
            embedded.append(text)
            return self.embed(text)

        score = R.make_affinity_scorer(self.read_profile, counting_embed, "pellicola")
        score("photographer")
        score("dropshipper")
        self.assertEqual(embedded.count("pellicola"), 1)



class MeanPoolingTest(unittest.TestCase):
    """The model gives a vector per token; what is wanted is one for the text."""

    def test_it_averages_the_tokens(self):
        rows = [[2.0, 0.0], [0.0, 4.0]]
        self.assertEqual(R.mean_pooled(rows, [1, 1]), [1.0, 2.0])

    def test_padding_is_left_out(self):
        """A batch is padded to a fixed length. Averaging the padding in would drag
        every short bio towards the same place, and hardest where there is least
        written - the shorter the text, the more padding there is."""
        rows = [[2.0, 0.0], [0.0, 4.0], [100.0, 100.0]]
        self.assertEqual(R.mean_pooled(rows, [1, 1, 0]), [1.0, 2.0])

    def test_a_text_that_is_all_padding(self):
        self.assertIsNone(R.mean_pooled([[1.0, 2.0]], [0]))

    def test_nothing_at_all(self):
        self.assertIsNone(R.mean_pooled([], [1]))
        self.assertIsNone(R.mean_pooled(None, None))


class NormalizeTest(unittest.TestCase):
    def test_it_comes_out_at_length_one(self):
        vector = R.normalized([3.0, 4.0])
        self.assertAlmostEqual(sum(v * v for v in vector) ** 0.5, 1.0)
        self.assertAlmostEqual(vector[0], 0.6)

    def test_direction_is_kept(self):
        """Which is the whole point: length is what is being thrown away."""
        self.assertAlmostEqual(R.cosine(R.normalized([3.0, 4.0]), [3.0, 4.0]), 1.0)

    def test_a_vector_with_no_length(self):
        self.assertIsNone(R.normalized([0.0, 0.0]))
        self.assertIsNone(R.normalized([]))
        self.assertIsNone(R.normalized(None))


class ModelAbsentTest(unittest.TestCase):
    """Absent is a normal state. The packages are not installed here, which is the
    same position a user is in before the first download, so this is the real
    behaviour rather than a simulation of it."""

    def setUp(self):
        _stubs.install_fake_ui(R)
        self.model = R.SemanticModel()

    def test_it_says_it_is_not_available(self):
        self.assertFalse(self.model.available())

    def test_embedding_comes_back_empty_rather_than_raising(self):
        self.assertIsNone(self.model.embed("fotografia analogica"))

    def test_it_gives_up_once_and_does_not_retry_every_candidate(self):
        """A pass over 200 profiles must not try to import a missing package 200
        times, nor log the same line 200 times."""
        self.model.available()
        self.assertTrue(self.model.failed)

    def test_the_scorer_is_not_built_at_all_without_a_model(self):
        """So a pass with no model scores nobody, rather than scoring everybody the
        same and reordering the queue by nothing."""
        self.assertIsNone(
            R.make_affinity_scorer(lambda u: (None, None, "bio"), self.model.embed, "nicchia")
        )


class NicheSettingTest(unittest.TestCase):
    """The niche is a sentence, and every other setting is a number.

    Saving converts each entry with int(), so the sentence is kept in a separate
    registry and written straight through. This checks the file end of that: a
    setting that is words has to survive being saved and read back.
    """

    def setUp(self):
        import tempfile
        R.CONFIG_FILE = os.path.join(tempfile.mkdtemp(), "bot_config.json")

    def test_a_sentence_survives_a_save_and_a_load(self):
        niche = "fotografi che scattano su pellicola e mostrano il loro lavoro"
        R.save_config({**R.CONFIG, "SEMANTIC_NICHE": niche})
        self.assertEqual(R.load_config()["SEMANTIC_NICHE"], niche)

    def test_the_settings_have_defaults_before_anything_is_saved(self):
        """A config file written by an older version has none of these keys in it,
        and the merge with the defaults is what keeps that from being a crash."""
        R.save_config({"BOT_MIN_POSTS": 3})
        loaded = R.load_config()
        for key in ("SEMANTIC_NICHE", "SEMANTIC_ENABLED", "SEMANTIC_WEIGHT", "SEMANTIC_TOP_K"):
            self.assertIn(key, loaded, key)
        self.assertEqual(loaded["BOT_MIN_POSTS"], 3, "what was saved is still there")

if __name__ == "__main__":
    unittest.main(verbosity=2)
