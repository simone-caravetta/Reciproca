"""
Reciproca - the follow queue: persistence, ranking, scoring shortlist.
Pure logic over the JSON files; no browser and no tkinter here.
"""

import hashlib
import json
import os
from datetime import datetime

from reciproca import config, state
from reciproca.logging_sink import log, logger
from reciproca.persistence import (
    is_already_followed,
    load_account_username,
    load_frequencies,
)
from reciproca.utils import brief_error


def load_queue():
    """Load the follow queue from file with backup recovery."""
    try:
        if os.path.exists(config.QUEUE_FILE):
            with open(config.QUEUE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Ensure it's a list
                if isinstance(data, list):
                    return data
                return []
    except Exception as e:
        logger.error(f"Error loading queue: {e}")
        # Try to recover from backup
        backup_file = config.QUEUE_FILE + ".backup"
        if os.path.exists(backup_file):
            try:
                logger.info(f"Attempting to recover queue from backup...")
                with open(backup_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        logger.info(f"Successfully recovered queue from backup")
                        # Restore the backup to main file
                        save_queue(data)
                        return data
            except Exception as backup_error:
                logger.error(f"Error loading backup queue: {backup_error}")
    return []

def save_queue(queue):
    """Save the follow queue to file with backup."""
    try:
        # Create backup before saving
        if os.path.exists(config.QUEUE_FILE):
            backup_file = config.QUEUE_FILE + ".backup"
            try:
                import shutil
                shutil.copy2(config.QUEUE_FILE, backup_file)
            except:
                pass

        with open(config.QUEUE_FILE, 'w', encoding='utf-8') as f:
            json.dump(queue, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error saving queue: {e}")
        return False

def queue_username(item):
    """Username of a queue entry, whichever format it is stored in.

    Entries are dicts carrying metadata, but queues written by older versions are
    plain username strings. One accessor instead of an isinstance check at every
    call site.
    """
    if isinstance(item, dict):
        return item.get('username')
    return item


def ranking_frequencies():
    """Frequencies the queue is ranked by, read from disk on first use.

    A user's frequency is how many of the scanned hashtag authors that user
    follows: a frequency of 6 means this candidate already follows 6 accounts
    posting under the searched tags, which is why it predicts a follow back.
    The count accumulates across scraping sessions, so it is a property of the
    candidate and not of the session that happened to find them.
    """
    if not state.last_scrape_frequencies:
        state.last_scrape_frequencies = load_frequencies()
    return state.last_scrape_frequencies


# Where the halfway point of the sighting scale falls: a candidate seen this many
# times scores 0.5. Two says that being seen twice rather than once is the step that
# matters, and that the difference between twenty and twenty-one is not.
FREQUENCY_HALFWAY = 2


def frequency_score(frequency):
    """A sighting count as a number between 0 and 1, as f / (f + FREQUENCY_HALFWAY).

    Three things this shape has to do. It has to keep the order the count already
    gave, so that weighing affinity at zero leaves the queue exactly as it is today.
    It has to fit between 0 and 1, so it can be mixed with an affinity at all. And
    the steps have to shrink: 1 to 2 is worth a lot, 20 to 21 is worth nothing, which
    is how the count behaves in fact.

    Fixed rather than measured against the batch it arrived in. Scaling a candidate
    against the 200 around it would give the same person a different number in the
    next search, and the queue outlives any one search.
    """
    frequency = max(0, frequency or 0)
    return frequency / (frequency + FREQUENCY_HALFWAY)


def queue_affinity(item):
    """How close this candidate's profile read to the niche, or None if unscored."""
    if isinstance(item, dict):
        value = item.get('affinity')
        return value if isinstance(value, (int, float)) else None
    return None


def combined_rank(frequency, affinity, weight):
    """The one number a candidate is ordered by, between 0 and 1.

    `weight` is how much of it the affinity is worth, 0 to 100:

        rank = (1 - weight/100) * frequency_score + (weight/100) * affinity

    At 0 it is the sighting count and nothing else, so scoring can be switched on
    and watched for a while without it moving anybody. At 100 the count stops
    counting.

    Most of the queue sits on the same count, since most candidates are seen once,
    and any weight above zero is enough to order all of those: the affinity is the
    only thing telling them apart. What the weight really sets is something else -
    how far up an affinity can carry somebody past a candidate seen more often.

    That is where the number is decided, and it is not where it looks. The affinity
    gap needed to climb one step of the count:

                            at 30      at 60
        seen 2 over 1        0.39       0.11
        seen 3 over 1        0.62       0.18
        seen 6 over 1        0.97       0.28

    Real affinities sit within a few hundredths of each other, not within four
    tenths, so at 30 the steps are effectively sealed: the affinity would sort
    inside each of them and never move anybody between them, which is most of what
    it was brought in to do. At 60 the two genuinely mix, and it still takes a real
    gap rather than noise.

    So 60 is the default: a little more to the affinity than to the count, rather
    than a takeover. If the scores come back all within a hundredth of each other,
    that is the model failing to tell people apart, and the answer is a better
    description of the niche before it is a smaller weight.

    A candidate with no affinity keeps the sighting count as their whole score.
    Not zero: the queue holds people from before any of this existed, and a scoring
    pass can be stopped half way, and scoring them zero would bury them under
    anyone who happened to be measured. An unknown is not a bad result, which is
    the rule the bot filter already goes by.
    """
    counted = frequency_score(frequency)
    if affinity is None:
        return counted

    share = min(max(weight or 0, 0), 100) / 100
    return (1 - share) * counted + share * affinity


def tie_breaker(username):
    """A stable, name-independent order for candidates on the same score.

    Most candidates are seen exactly once, so most of the queue is tied, and ties
    used to break on the username. That sorted the same names to the top of every
    batch, which does not matter while the order is only shown - and matters a lot
    once the top of the queue is the part that gets scored, since the sample would
    have been an alphabetical slice rather than a fair draw from the tie.

    Drawn from the name rather than stored, so it is the same on every run and
    entries written by older versions have one too. Not drawn afresh per sort: the
    list would reshuffle under the cursor every time the window redrew.
    """
    return hashlib.sha1((username or "").encode('utf-8')).hexdigest()


def rank_queue(queue, frequencies=None):
    """Queue entries ordered by rank, highest first.

    Returns a list of (username, rank, item) triples, where rank is the one number
    a candidate is judged on: their sighting count and, once they have been scored,
    how close their profile read to the niche. See combined_rank().

    This is the single definition of the queue's order. The listbox draws it and
    follow_from_queue consumes it, so the account followed next is always the
    top one on screen. They used to order the queue separately - the display
    sorted by rank while the follow loop walked the file in insertion order -
    which made the ranking decorative: it was shown but never acted on.

    Ties break on a number drawn from the username rather than on the username
    itself, so the order is stable between redraws without being alphabetical.
    """
    if frequencies is None:
        frequencies = ranking_frequencies()

    weight = config.CONFIG["SEMANTIC_WEIGHT"]

    ranked = []
    for item in queue:
        username = queue_username(item)
        if username:
            rank = combined_rank(
                frequencies.get(username, 0), queue_affinity(item), weight
            )
            ranked.append((username, rank, item))

    ranked.sort(key=lambda entry: (-entry[1], tie_breaker(entry[0])))
    return ranked


def add_to_queue(usernames):
    """Add usernames to queue, avoiding duplicates and already followed users."""
    queue = load_queue()
    # Add only new usernames (not already in queue and not already followed)
    existing_in_queue = {queue_username(item) for item in queue}

    new_users = []

    # The login account itself can slip into the queue from any extraction
    # path, so the final funnel refuses it by name as well.
    own_username = load_account_username()
    for username in usernames:
        if own_username and username.lower() == own_username.lower():
            logger.debug(f"Skipping {username} - the login account itself")
        elif username not in existing_in_queue and not is_already_followed(username):
            new_users.append(username)
        elif is_already_followed(username):
            logger.debug(f"Skipping {username} - already in followed history")

    # Add new users with metadata
    for username in new_users:
        queue_item = {
            'username': username,
            'added_at': datetime.now().isoformat(),
            'source': 'extraction'  # Could be 'manual', 'import', etc.
        }
        queue.append(queue_item)

    # Persist in rank order, so the file matches what the listbox shows and the
    # order the follow loop will consume. Plain appending buried a high-ranking
    # user from this batch behind every lower-ranked user already queued.
    save_queue([item for _, _, item in rank_queue(queue)])
    return len(new_users), len(queue)

def remove_from_queue(username):
    """Remove a username from queue."""
    queue = load_queue()
    if not queue:
        return False

    save_queue([item for item in queue if queue_username(item) != username])
    return True

def clear_queue():
    """Clear the entire queue."""
    save_queue([])
    return True


def validate_queue():
    """Validate queue by removing already followed users and duplicates."""
    queue = load_queue()
    if not queue:
        return 0, 0

    # Remove duplicates while preserving order
    seen = set()
    unique_queue = []
    for item in queue:
        username = queue_username(item)
        if username not in seen:
            seen.add(username)
            unique_queue.append(item)

    # Remove already followed users
    validated_queue = []
    removed_count = 0
    for item in unique_queue:
        username = queue_username(item)
        if is_already_followed(username):
            removed_count += 1
            logger.debug(f"Queue validation: Removed already followed user {username}")
        else:
            validated_queue.append(item)

    if len(validated_queue) != len(queue):
        save_queue(validated_queue)
        logger.info(f"Queue validated: Removed {len(queue) - len(validated_queue)} entries ({removed_count} already followed, {len(queue) - len(unique_queue)} duplicates)")

    return len(queue) - len(validated_queue), len(validated_queue)


# ---------------------------
# SCORING PASS
# ---------------------------
# What a search leaves behind is a queue far longer than anyone will ever follow
# through: thousands of candidates, most of them seen exactly once, which is to say
# most of them in a tie the sighting count has no opinion about. Reading a profile
# is what breaks that tie, and reading a profile costs a page load, so the pass runs
# over the strongest few hundred rather than over everything.

def scoring_shortlist(queue, limit, frequencies=None):
    """The candidates a scoring pass should visit, best first.

    The top `limit` by rank, less anyone already carrying an affinity. A score is
    kept with the candidate, so a second pass over the same queue pays only for
    whoever has arrived since the first.

    Candidates below the cut are not scored and not judged: they keep their place
    and their count, and a later search that pushes them up gets them scored then.
    """
    ranked = rank_queue(queue, frequencies)[:max(0, limit)]
    return [(username, item) for username, _, item in ranked if queue_affinity(item) is None]


def with_affinity(item, username, affinity):
    """The queue entry, carrying its affinity.

    Entries written by older versions are plain usernames with nowhere to put a
    score, so those become dicts here rather than being skipped.
    """
    entry = dict(item) if isinstance(item, dict) else {'username': username}
    entry['affinity'] = affinity
    return entry


def score_queue(scorer, limit=None, frequencies=None, on_progress=None, stop_event=None):
    """Give an affinity to the best unscored candidates. Returns how many got one.

    `scorer` takes a username and returns a number between 0 and 1, or None where
    the profile could not be read. None leaves the candidate unscored rather than
    scoring them zero: an unknown is not a bad result, and scoring it as one would
    bury somebody for a page that failed to load.

    The queue is written after every candidate, so stopping the pass keeps what it
    has already done and the next one starts from there. Stopping is not a way of
    discarding anybody either: whoever is left unscored stays in the queue, ranked
    on their count as before.
    """
    if limit is None:
        limit = config.CONFIG["SEMANTIC_TOP_K"]
    if stop_event is None:
        stop_event = state.scoring_stop
    # A stop flag left set by the pass it interrupted must not kill the next
    # one: stopping is one-shot, and a resumed pass starts clear (the follow
    # path clears its own flag before this same call; the standalone frontends
    # share stop_requested, which nobody else would ever clear again).
    stop_event.clear()

    queue = load_queue()
    shortlist = scoring_shortlist(queue, limit, frequencies)
    if not shortlist:
        return 0

    log(f"🧭 Reading {len(shortlist)} profiles to score them against your niche", 'info')

    positions = {}
    for index, item in enumerate(queue):
        username = queue_username(item)
        if username is not None and username not in positions:
            positions[username] = index

    scored = 0
    for number, (username, item) in enumerate(shortlist, 1):
        if stop_event.is_set():
            log(f"⏹️ Scoring stopped after {scored} of {len(shortlist)}", 'warning')
            break

        if on_progress:
            on_progress(number, len(shortlist), username)

        try:
            affinity = scorer(username)
        except Exception as e:
            log(f"❌ Could not score {username}: {brief_error(e)}", 'error')
            logger.debug(f"Scoring error on {username}", exc_info=True)
            continue

        if affinity is None:
            logger.info(f"No affinity for {username}: nothing readable on the profile")
            continue

        index = positions.get(username)
        if index is None:
            continue

        queue[index] = with_affinity(item, username, affinity)
        save_queue(queue)
        scored += 1
        logger.info(f"Affinity for {username}: {affinity:.2f}")

    return scored


def trim_queue(limit=None, frequencies=None):
    """Keep the best `limit` candidates, drop the rest. Returns how many were let go.

    The sighting counts are deliberately left alone. A candidate dropped here is out
    of the queue, not out of the record: the count that got them there stays on
    file, so a later search that runs into them again finds them where they were
    rather than back at the beginning, and they may well come back above the cut.

    Without this the queue only grows. A search adds thousands and a session
    follows a few dozen, so the far end of the list is candidates nobody will reach
    this year, slowing every redraw and every ranking on the way past.

    How long the queue is kept is a different question from how many profiles are
    worth reading, and they used to share one setting. That made reading fewer of
    them a way of throwing candidates away, which is not a trade anybody asked for
    and not one that announces itself.
    """
    if limit is None:
        limit = config.CONFIG["SEMANTIC_TOP_K"]

    queue = load_queue()
    ranked = rank_queue(queue, frequencies)
    if len(ranked) <= limit:
        return 0

    save_queue([item for _, _, item in ranked[:limit]])
    return len(ranked) - limit
