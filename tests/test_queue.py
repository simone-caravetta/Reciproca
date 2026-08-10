"""Exercises the queue's ranking against reciproca.py.

The invariant under test is that the order shown in the listbox is the order the
follow loop consumes: both go through rank_queue(). They used to be computed
separately, so the ranking was displayed but never acted on.

Runs on stdlib only; see _stubs.py for why Selenium and Tkinter are stubbed.

    python3 tests/test_queue.py
"""
import os
import tempfile
import unittest

import _stubs  # noqa: F401  - installs the Selenium/Tkinter stubs

import reciproca as R  # noqa: E402
from reciproca import order_authors_by_staleness  # noqa: E402


class QueueRankingTest(unittest.TestCase):
    def setUp(self):
        # Redirect the app's files into a scratch directory so a test run cannot
        # touch a real queue or follow history.
        workdir = tempfile.mkdtemp()
        R.QUEUE_FILE = os.path.join(workdir, "follow_queue.json")
        R.FREQUENCIES_FILE = os.path.join(workdir, "user_frequencies.json")
        R.FOLLOWED_FILE = os.path.join(workdir, "followed_history.json")
        R.last_scrape_frequencies = None

    def ranks(self, queue, frequencies=None):
        return [username for username, _, _ in R.rank_queue(queue, frequencies)]

    def test_orders_by_rank(self):
        frequencies = R.Counter({"alice": 6, "bob": 4, "carol": 6, "dave": 0})
        queue = [{"username": u} for u in ("dave", "bob", "alice", "carol")]
        order = self.ranks(queue, frequencies)
        self.assertEqual(set(order[:2]), {"alice", "carol"}, "the two on 6 come first")
        self.assertEqual(order[2:], ["bob", "dave"])

    def test_ties_are_not_broken_alphabetically(self):
        """Most candidates are seen once, so most of the queue is tied. Ordering
        those by name would make the top of the queue an alphabetical slice, and the
        top of the queue is the part that gets scored."""
        tied = [{"username": u} for u in ("aaa", "aab", "aac", "aad", "aae", "zzz")]
        frequencies = R.Counter({u["username"]: 1 for u in tied})
        self.assertNotEqual(
            self.ranks(tied, frequencies), sorted(u["username"] for u in tied)
        )

    def test_the_tie_order_does_not_change_between_redraws(self):
        """It is drawn from the name, not afresh each sort: a list that reshuffled
        every time the window redrew would be unusable."""
        tied = [{"username": u} for u in ("aaa", "bbb", "ccc", "ddd")]
        frequencies = R.Counter({u["username"]: 1 for u in tied})
        self.assertEqual(
            self.ranks(tied, frequencies), self.ranks(list(reversed(tied)), frequencies)
        )

    def test_reads_queues_written_by_older_versions(self):
        frequencies = R.Counter({"alice": 6, "bob": 4})
        self.assertEqual(self.ranks(["bob", "alice"], frequencies), ["alice", "bob"])

    def test_keeps_unranked_users_at_the_bottom(self):
        frequencies = R.Counter({"alice": 6})
        queue = [{"username": "zoe"}, {"username": "alice"}]
        self.assertEqual(self.ranks(queue, frequencies), ["alice", "zoe"])

    def test_a_later_batch_is_not_buried_behind_an_earlier_one(self):
        """The original defect: appending sent every new batch to the tail.

        A candidate found in the second scraping session outranks both of the
        first session's, so it has to be followed before them - not after.
        """
        R.save_frequencies(R.Counter({"low1": 1, "low2": 1}))
        R.last_scrape_frequencies = None
        R.add_to_queue(["low1", "low2"])

        R.save_frequencies(R.Counter({"low1": 1, "low2": 1, "high": 9}))
        R.last_scrape_frequencies = None
        R.add_to_queue(["high"])

        on_disk = [R.queue_username(item) for item in R.load_queue()]
        self.assertEqual(on_disk[0], "high")
        self.assertEqual(set(on_disk[1:]), {"low1", "low2"}, "tied, so in either order")

    def test_requeueing_a_known_user_raises_their_rank_and_position(self):
        """Finding a queued user again is more evidence, not a duplicate to drop.

        add_to_queue() refuses to store the same username twice, but the rank
        lives in the frequencies file, so a later session adds to the count it
        already had - and the user climbs past those it now outranks.
        """
        # First session: alice follows 2 of the scanned authors, bob follows 5.
        R.save_frequencies(R.Counter({"alice": 2, "bob": 5}))
        R.add_to_queue(["alice", "bob"])
        self.assertEqual(self.ranks(R.load_queue()), ["bob", "alice"])

        # Second session finds alice under 4 more authors: 2 + 4 beats bob's 5.
        R.save_frequencies(R.load_frequencies() + R.Counter({"alice": 4}))
        R.last_scrape_frequencies = None
        R.add_to_queue(["alice"])

        self.assertEqual(dict(R.load_frequencies()), {"alice": 6, "bob": 5})
        # Still queued once, now ahead of bob, on disk as well as on screen.
        stored = [R.queue_username(item) for item in R.load_queue()]
        self.assertEqual(stored, ["alice", "bob"])
        self.assertEqual(self.ranks(R.load_queue()), ["alice", "bob"])

    def test_stored_order_matches_the_ranked_order(self):
        R.save_frequencies(R.Counter({"alice": 2, "bob": 7, "carol": 5}))
        R.add_to_queue(["alice", "bob", "carol"])
        stored = [R.queue_username(item) for item in R.load_queue()]
        self.assertEqual(stored, self.ranks(R.load_queue()))
        self.assertEqual(stored, ["bob", "carol", "alice"])


class FrequencyAccumulationTest(unittest.TestCase):
    """A rank counts the scanned authors a candidate follows, so a new scraping
    session adds to that count. It used to start from zero and overwrite the
    file, dropping every earlier candidate to rank 0."""

    def setUp(self):
        R.FREQUENCIES_FILE = os.path.join(tempfile.mkdtemp(), "user_frequencies.json")

    def test_sessions_sum_onto_earlier_ranks(self):
        R.save_frequencies(R.Counter({"alice": 4}) + R.Counter(["alice", "bob"]))
        self.assertEqual(dict(R.load_frequencies()), {"alice": 5, "bob": 1})

        previous = R.load_frequencies()
        R.save_frequencies(previous + R.Counter(["alice", "alice", "carol"]))
        self.assertEqual(
            dict(R.load_frequencies()), {"alice": 7, "bob": 1, "carol": 1}
        )


class AuthorRotationTest(unittest.TestCase):
    """A hashtag page shows the same posts at the top every session, so without a
    rotation the same authors get scraped forever and the candidates never
    change. Never-scraped authors win outright; the rest go least recently
    scraped first, so one left alone for a while climbs back up."""

    def test_unseen_authors_come_before_every_seen_one(self):
        history = {"old": "2026-01-01T00:00:00", "fresh": "2026-08-01T00:00:00"}
        self.assertEqual(
            order_authors_by_staleness(["fresh", "never", "old"], history),
            ["never", "old", "fresh"],
        )

    def test_seen_authors_go_oldest_first(self):
        history = {
            "yesterday": "2026-08-07T09:00:00",
            "last_month": "2026-07-05T09:00:00",
            "this_morning": "2026-08-08T07:00:00",
        }
        self.assertEqual(
            order_authors_by_staleness(
                ["this_morning", "yesterday", "last_month"], history
            ),
            ["last_month", "yesterday", "this_morning"],
        )

    def test_repeated_sessions_rotate_instead_of_repeating(self):
        """Two authors, one slot per session: each session takes the other one."""
        history = {}
        picked = []
        for session in range(4):
            chosen = order_authors_by_staleness(["ann", "bea"], history)[0]
            picked.append(chosen)
            history[chosen] = f"2026-08-0{session + 1}T00:00:00"
        self.assertEqual(picked, ["ann", "bea", "ann", "bea"])

    def test_several_unseen_authors_keep_a_stable_order(self):
        self.assertEqual(
            order_authors_by_staleness(["carol", "alice", "bob"], {}),
            ["alice", "bob", "carol"],
        )

    def test_a_missing_timestamp_is_treated_as_the_oldest(self):
        """An entry written by a version that stored no timestamp must still be
        reusable, and go first among the seen rather than being skipped."""
        history = {"undated": None, "dated": "2026-01-01T00:00:00"}
        self.assertEqual(
            order_authors_by_staleness(["dated", "undated"], history),
            ["undated", "dated"],
        )



class RankFormulaTest(unittest.TestCase):
    """The one number a candidate is ordered by.

    Two things have to be mixed that are not on the same scale: a sighting count,
    an integer that grows without limit across searches, and an affinity, already
    between 0 and 1. The count is squashed by f / (f + FREQUENCY_HALFWAY) and the
    two are then weighed against each other.
    """

    def test_the_count_keeps_the_order_it_already_had(self):
        """Squashing must not reorder anybody, or a weight of zero would not leave
        the queue as it is today."""
        counts = [0, 1, 2, 3, 6, 10, 20, 21, 500]
        scores = [R.frequency_score(c) for c in counts]
        self.assertEqual(scores, sorted(scores))

    def test_the_count_lands_between_zero_and_one(self):
        self.assertEqual(R.frequency_score(0), 0)
        for count in (1, 5, 100, 10_000):
            self.assertLess(R.frequency_score(count), 1)

    def test_halfway_is_where_the_constant_says(self):
        self.assertAlmostEqual(R.frequency_score(R.FREQUENCY_HALFWAY), 0.5)

    def test_the_steps_shrink(self):
        """Seen twice rather than once says something. Twenty-one rather than
        twenty says nothing, and the numbers have to agree."""
        early = R.frequency_score(2) - R.frequency_score(1)
        late = R.frequency_score(21) - R.frequency_score(20)
        self.assertGreater(early, late * 10)

    def test_no_weight_is_todays_order(self):
        for affinity in (0.0, 0.5, 1.0, None):
            self.assertEqual(
                R.combined_rank(6, affinity, weight=0), R.frequency_score(6), affinity
            )

    def test_all_the_weight_is_affinity_alone(self):
        self.assertEqual(R.combined_rank(6, 0.3, weight=100), 0.3)

    def test_a_weight_in_between(self):
        """A candidate seen six times with a poor bio against one seen once with a
        good one: 60 is where the affinity takes the lead."""
        seen_often, seen_once = (6, 0.30), (1, 0.65)
        at = lambda weight, c: R.combined_rank(c[0], c[1], weight)

        self.assertGreater(at(50, seen_often), at(50, seen_once))
        self.assertLess(at(60, seen_often), at(60, seen_once))

    def test_any_weight_at_all_orders_a_tie(self):
        """Most candidates are seen once, so most of the queue is on one count. The
        affinity is the only thing telling those apart, and it does not take much
        weight to let it: what the weight really sets is how far an affinity can
        carry somebody past a candidate seen more often."""
        for weight in (1, 5, 30, 100):
            better = R.combined_rank(1, 0.60, weight)
            worse = R.combined_rank(1, 0.30, weight)
            self.assertGreater(better, worse, weight)

    def test_a_real_gap_in_sightings_still_takes_a_real_gap_in_affinity(self):
        """The weight is a little more to the affinity, not a takeover: a candidate
        seen six times is not overtaken by a hundredth of a point."""
        self.assertGreater(
            R.combined_rank(6, 0.45, weight=60), R.combined_rank(1, 0.50, weight=60)
        )

    def test_a_wide_gap_in_affinity_does_climb_a_step(self):
        """Which is the thing the weight was raised for. At 30 this did not happen
        at any affinity a real model produces."""
        self.assertGreater(
            R.combined_rank(1, 0.60, weight=60), R.combined_rank(2, 0.40, weight=60)
        )

    def test_the_shipped_default_is_one_that_orders_ties(self):
        self.assertGreater(R.CONFIG["SEMANTIC_WEIGHT"], 0)

    def test_an_unscored_candidate_keeps_their_count(self):
        """Not zero: the queue holds people from before scoring existed, and a pass
        can be stopped half way. Scoring them zero would bury them under anyone who
        happened to be measured."""
        for weight in (0, 50, 100):
            self.assertEqual(
                R.combined_rank(6, None, weight), R.frequency_score(6), weight
            )

    def test_an_unscored_candidate_still_outranks_a_worse_seen_one(self):
        self.assertGreater(
            R.combined_rank(6, None, weight=50), R.combined_rank(1, None, weight=50)
        )

    def test_a_weight_outside_the_scale_is_pulled_back_onto_it(self):
        self.assertEqual(R.combined_rank(6, 0.3, weight=1000), R.combined_rank(6, 0.3, 100))
        self.assertEqual(R.combined_rank(6, 0.3, weight=-5), R.combined_rank(6, 0.3, 0))

    def test_the_same_candidate_scores_the_same_in_any_batch(self):
        """Nothing here is measured against the group, so a queue that outlives a
        search is not holding numbers that meant something else when written."""
        self.assertEqual(R.combined_rank(3, 0.4, 50), R.combined_rank(3, 0.4, 50))


class QueueAffinityTest(unittest.TestCase):
    def test_a_scored_entry(self):
        self.assertEqual(R.queue_affinity({"username": "x", "affinity": 0.42}), 0.42)

    def test_an_entry_never_scored(self):
        self.assertIsNone(R.queue_affinity({"username": "x"}))

    def test_an_entry_written_by_an_older_version(self):
        self.assertIsNone(R.queue_affinity("x"))

    def test_something_that_is_not_a_number(self):
        """A hand-edited file must not take the ranking down with it."""
        for value in ("0.4", None, [], {}):
            self.assertIsNone(R.queue_affinity({"username": "x", "affinity": value}), value)



class ScoringPassTest(unittest.TestCase):
    """The pass that reads profiles and gives them an affinity.

    Driven with a stand-in scorer: what is under test is which candidates get
    visited, what is kept, and what survives being stopped half way.
    """

    def setUp(self):
        workdir = tempfile.mkdtemp()
        R.QUEUE_FILE = os.path.join(workdir, "follow_queue.json")
        R.FREQUENCIES_FILE = os.path.join(workdir, "user_frequencies.json")
        R.FOLLOWED_FILE = os.path.join(workdir, "followed_history.json")
        R.last_scrape_frequencies = None
        _stubs.install_fake_ui(R)
        self.visited = []

    def scorer(self, scores):
        def score(username):
            self.visited.append(username)
            value = scores.get(username, 0.5)
            if isinstance(value, Exception):
                raise value
            return value
        return score

    def queue_now(self):
        return {R.queue_username(i): R.queue_affinity(i) for i in R.load_queue()}

    def test_it_visits_the_top_of_the_queue_and_stops_there(self):
        R.save_frequencies(R.Counter({"a": 9, "b": 7, "c": 5, "d": 3}))
        R.add_to_queue(["a", "b", "c", "d"])

        R.score_queue(self.scorer({}), limit=2)
        self.assertEqual(self.visited, ["a", "b"])
        self.assertIsNone(self.queue_now()["c"], "below the cut, so left alone")

    def test_a_score_is_kept_so_a_second_pass_is_cheaper(self):
        R.save_frequencies(R.Counter({"a": 9, "b": 7}))
        R.add_to_queue(["a", "b"])

        self.assertEqual(R.score_queue(self.scorer({}), limit=10), 2)
        self.visited.clear()
        self.assertEqual(R.score_queue(self.scorer({}), limit=10), 0)
        self.assertEqual(self.visited, [], "nobody is read twice")

    def test_an_unreadable_profile_leaves_the_candidate_unscored(self):
        """Not scored zero. A page that failed to load is not evidence about
        anybody, and zero would bury them under everyone who happened to load."""
        R.save_frequencies(R.Counter({"a": 9, "b": 7}))
        R.add_to_queue(["a", "b"])

        R.score_queue(self.scorer({"a": None}), limit=10)
        self.assertIsNone(self.queue_now()["a"])
        self.assertEqual(self.queue_now()["b"], 0.5)

    def test_a_scorer_that_raises_does_not_end_the_pass(self):
        R.save_frequencies(R.Counter({"a": 9, "b": 7}))
        R.add_to_queue(["a", "b"])

        R.score_queue(self.scorer({"a": RuntimeError("boom")}), limit=10)
        self.assertIsNone(self.queue_now()["a"])
        self.assertEqual(self.queue_now()["b"], 0.5, "the next candidate is still read")

    def test_stopping_keeps_what_it_has_and_discards_nobody(self):
        R.save_frequencies(R.Counter({"a": 9, "b": 7, "c": 5}))
        R.add_to_queue(["a", "b", "c"])

        def score(username):
            self.visited.append(username)
            R.stop_requested.set()      # as if Stop were pressed during the first read
            return 0.8

        R.score_queue(score, limit=10)
        R.stop_requested.clear()

        queue = self.queue_now()
        self.assertEqual(queue["a"], 0.8, "the one already read is written down")
        self.assertEqual(set(queue), {"a", "b", "c"}, "nobody is dropped for being unread")

    def test_an_entry_written_by_an_older_version_can_still_be_scored(self):
        R.save_queue(["a", "b"])
        R.save_frequencies(R.Counter({"a": 9, "b": 7}))
        R.last_scrape_frequencies = None

        R.score_queue(self.scorer({}), limit=10)
        self.assertEqual(self.queue_now(), {"a": 0.5, "b": 0.5})

    def test_an_empty_queue_reads_nothing(self):
        self.assertEqual(R.score_queue(self.scorer({}), limit=10), 0)
        self.assertEqual(self.visited, [])


class TrimQueueTest(unittest.TestCase):
    """A search adds thousands and a session follows a few dozen, so without a cut
    the far end of the queue is people nobody will reach this year."""

    def setUp(self):
        workdir = tempfile.mkdtemp()
        R.QUEUE_FILE = os.path.join(workdir, "follow_queue.json")
        R.FREQUENCIES_FILE = os.path.join(workdir, "user_frequencies.json")
        R.FOLLOWED_FILE = os.path.join(workdir, "followed_history.json")
        R.last_scrape_frequencies = None
        _stubs.install_fake_ui(R)

    def test_it_keeps_the_best_and_drops_the_rest(self):
        R.save_frequencies(R.Counter({"a": 9, "b": 7, "c": 5, "d": 3}))
        R.add_to_queue(["a", "b", "c", "d"])

        self.assertEqual(R.trim_queue(limit=2), 2)
        self.assertEqual([R.queue_username(i) for i in R.load_queue()], ["a", "b"])

    def test_a_dropped_candidate_keeps_the_count_that_got_them_there(self):
        """Out of the queue is not out of the record. A later search that runs into
        them again finds them where they were, not back at the beginning."""
        R.save_frequencies(R.Counter({"a": 9, "d": 3}))
        R.add_to_queue(["a", "d"])
        R.trim_queue(limit=1)

        self.assertEqual(dict(R.load_frequencies()), {"a": 9, "d": 3})

        R.save_frequencies(R.load_frequencies() + R.Counter({"d": 8}))
        R.last_scrape_frequencies = None
        R.add_to_queue(["d"])
        self.assertEqual([R.queue_username(i) for i in R.load_queue()], ["d", "a"])

    def test_a_queue_shorter_than_the_cut_is_left_alone(self):
        R.save_frequencies(R.Counter({"a": 9, "b": 7}))
        R.add_to_queue(["a", "b"])
        self.assertEqual(R.trim_queue(limit=50), 0)
        self.assertEqual(len(R.load_queue()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
