"""Checks how close a profile reads to the niche you described.

Two vectors in, one number out, plus the rules about what happens when there is
nothing to compare. Turning text into a vector is not covered here: that needs the
model, and this file is meant to run on the standard library alone.

    python3 tests/test_affinity.py
"""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
