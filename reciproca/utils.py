"""
Reciproca - pure helpers: text parsing, locale-aware button detection, pause,
and the Selenium wait/retry utilities. No tkinter, no browser state beyond what
is passed in.
"""

import functools
import os
import re
import time

from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from reciproca import config, state
from reciproca.logging_sink import logger
from reciproca.markers import FOLLOWING_BUTTON_MARKERS, FOLLOW_BUTTON_MARKERS


def brief_error(exc):
    """First line of an exception message, for the on-screen log.

    Selenium appends a chromedriver stacktrace to every message: twenty lines of hex
    addresses that push everything else off the screen. Callers pair this with a
    logger.debug(exc_info=True), so the full text stays in the log file.
    """
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def pause(seconds):
    """Sleep in one-second slices so a stop request cuts the wait short.

    The waits exist so Instagram never sees a machine. Once the user has asked
    to stop, no more traffic comes from this run, so the wait has nothing left
    to protect: the current operation still finishes, only the pause before the
    next one is skipped. Short per-request waits inside an operation are not
    pause() calls - they belong to the operation that must finish.

    A stop requested from another process (the CLI's `stop` command writes a
    stop.flag next to the app) is honoured here too, on the same check.
    """
    end = time.monotonic() + seconds
    while (not state.stop_requested.is_set()
           and not _stop_flag_exists()
           and time.monotonic() < end):
        time.sleep(min(1.0, end - time.monotonic()))


def _stop_flag_exists():
    """True when another process asked the running session to stop."""
    try:
        return os.path.exists(config.STOP_FLAG_FILE)
    except Exception:
        return False


def has_marker(text, markers):
    """True if any marker appears in text. Text must already be lowercased."""
    return any(m in text for m in markers)


def is_follow_button(text):
    """True for a plain "Follow" button.

    Excludes the already-following case explicitly: "follow" is a substring of
    "following" and "segui" of "segui già", so a naive membership test matches the
    very button it is meant to rule out.
    """
    return has_marker(text, FOLLOW_BUTTON_MARKERS) and not has_marker(text, FOLLOWING_BUTTON_MARKERS)


def parse_count(text):
    """A count out of Instagram's profile header as a number, or None.

    Accepts plain digits, thousands separated by , . or a space depending on locale,
    and abbreviations such as 12.3K, 1,2K, 5M. A separator is a decimal point only
    when a multiplier follows: "1.234" is 1234, "1.2K" is 1200.
    """
    if not text:
        return None

    # Some locales separate thousands with a space, so "1 234" has to read as 1234
    # and not as 1. Getting that wrong understates a count, which is the direction
    # that matters: it would reject a real account rather than let one through.
    text = re.sub(r'(?<=\d)[\s  ](?=\d)', '', str(text))

    match = re.search(r'(\d[\d.,]*)\s*([KkMmBb])?', text)
    if not match:
        return None

    digits = match.group(1).rstrip('.,')
    multiplier = match.group(2)

    if multiplier:
        try:
            value = float(digits.replace(',', '.'))
        except ValueError:
            return None
        return int(value * {'k': 1_000, 'm': 1_000_000, 'b': 1_000_000_000}[multiplier.lower()])

    digits = digits.replace('.', '').replace(',', '')
    return int(digits) if digits.isdigit() else None


def bot_rejection_reason(posts, followers, following):
    """Why a profile looks automated, as a phrase for the log, or None to allow it.

    A count that could not be read arrives as None and takes no part in the decision:
    a missing signal must never count as a bad one, or a change in Instagram's markup
    would start rejecting everybody.
    """
    if posts is not None and posts < config.CONFIG["BOT_MIN_POSTS"]:
        return f"{posts} posts"

    if followers is not None and followers < config.CONFIG["BOT_MIN_FOLLOWERS"]:
        return f"only {followers} followers"

    if following is not None and following > config.CONFIG["BOT_MAX_FOLLOWING"]:
        return f"follows {following} accounts"

    if (followers is not None and following is not None
            and following > followers * config.CONFIG["BOT_MAX_FOLLOWING_RATIO"]):
        return f"follows {following} but has {followers} followers"

    return None


def author_rejection_reason(posts, followers, following):
    """Why a scraped author should not be followed back, or None to allow it.

    Authors are followed for the follow-back they can give, so a profile with far
    more followers than accounts it follows is not a candidate: that audience does
    not follow back. Unlike the bot filter this does NOT fail open - an author
    whose counts cannot be read cannot vouch for a follow-back, so it is skipped
    too.
    """
    if followers is None or following is None:
        return "counts unreadable"

    if following == 0:
        return "follows nobody"

    if followers > following * config.CONFIG["AUTHOR_MAX_FOLLOWERS_RATIO"]:
        return f"{followers} followers but only follows {following}"

    return None


# What can appear inside a rendered count: digits, a thousands separator (either ,
# or . by locale, or a space in some), and nothing else. Letters are excluded on
# purpose, so a search for one count's label can never reach across another count.
COUNT_CHARS = r'[\d.,   ]'


def parse_labelled_count(header_text, markers):
    """The count beside one of `markers` in a profile header's text, or None.

    Found by the word next to it rather than by position: the header carries all
    three counts plus the display name and the bio, so none of them is reliably the
    first number in it. Instagram renders each count and its label as separate
    elements, so what sits between them is usually a newline.
    """
    for marker in markers:
        match = re.search(
            r'(\d' + COUNT_CHARS + r'*[KkMmBb]?)\s*' + re.escape(marker),
            header_text or "",
            re.IGNORECASE,
        )
        if match:
            value = parse_count(match.group(1))
            if value is not None:
                return value
    return None


def count_link_value(entry, markers):
    """The count carried by one header link, or None if it carries something else.

    A profile header holds more than one link to the same followers page. Beside the
    count there is the line about the people you both follow, which reads "11
    followers you follow". Taking whichever came first meant reading that eleven as
    the account's whole audience, and the bot filter then rejected real accounts for
    having eleven followers and hundreds of following.

    The count link says the number and, at most, the word for what it counts. So a
    link qualifies only when nothing is left over once those two are removed, which
    is what tells "624 followers" apart from "11 followers you follow".
    """
    if not entry:
        return None

    text = (entry.get('text') or "").strip()
    if parse_count(text) is None:
        return None

    leftover = re.sub(r'\d' + COUNT_CHARS + r'*[KkMmBb]?', ' ', text, count=1)
    for marker in markers:
        leftover = re.sub(re.escape(marker), ' ', leftover, flags=re.IGNORECASE)
    if leftover.strip():
        return None

    # The title attribute holds the exact figure where the text is abbreviated, so
    # prefer it - but only when it parsed, and 0 is a value like any other.
    from_title = parse_count(entry.get('title'))
    return from_title if from_title is not None else parse_count(text)


def count_from_links(entries, markers):
    """The count from the first header link that is a count link, or None."""
    for entry in entries or []:
        value = count_link_value(entry, markers)
        if value is not None:
            return value
    return None


# How far the two routes to a count may differ and still be describing the same
# number. Only abbreviation should separate them: a title of 12345 against a
# rendered "12.3K" is a fraction of a percent out, and "1M" at its widest is 5%.
# Ten percent leaves that alone while catching a misread, which is never close -
# the link that started this read 11 where the header said 624.
COUNT_AGREEMENT_TOLERANCE = 0.10


def counts_agree(a, b):
    """True if two readings of one count differ by no more than abbreviation would."""
    return abs(a - b) <= max(abs(a), abs(b)) * COUNT_AGREEMENT_TOLERANCE


def parse_follower_count(text):
    """First token in `text` that looks like a follower count, or None.

    The count sits next to the followers link, but the surrounding text also
    carries the profile's display name, so the first token is not reliably the
    number - a name starting with an emoji would be logged as the count.

    Accepts the shapes Instagram actually renders: plain digits, thousands
    separated by either , or . depending on locale, and abbreviations such as
    12.3K, 1,2K, 5M. Purely cosmetic: only used to label a log line.
    """
    for token in (text or "").split():
        if re.fullmatch(r'\d[\d.,]*[KkMmBb]?', token):
            return token
    return None


# ---------------------------
# RETRY DECORATOR
# ---------------------------
def retry(max_attempts=None, backoff=None):
    """Decorator for retry logic with exponential backoff.

    The settings are read at call time, not import time: a long-lived process
    honours a RETRY_ATTEMPTS / RETRY_BACKOFF change without a restart.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempts = config.CONFIG["RETRY_ATTEMPTS"] if max_attempts is None else max_attempts
            bf = config.CONFIG["RETRY_BACKOFF"] if backoff is None else backoff
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except (TimeoutException, StaleElementReferenceException) as e:
                    if attempt == attempts - 1:
                        raise
                    wait_time = bf ** attempt
                    # Type only: Selenium's TimeoutException str() carries the
                    # server stacktrace, which is noise at this level - the
                    # detail is in follow_bot.log if it is ever needed.
                    logger.warning(f"Retry {func.__name__} in {wait_time}s: {type(e).__name__}")
                    time.sleep(wait_time)
            return None
        return wrapper
    return decorator


def _resolved_timeout(timeout):
    """The caller's timeout, or the config's - read at call time, not at
    import, so a long-lived process honours a BROWSER_TIMEOUT change."""
    return config.CONFIG["BROWSER_TIMEOUT"] if timeout is None else timeout


@retry()
def wait_for_element(driver, by, value, timeout=None):
    """Wait for element to be present."""
    wait = WebDriverWait(driver, _resolved_timeout(timeout))
    return wait.until(EC.presence_of_element_located((by, value)))


@retry()
def wait_for_clickable(driver, by, value, timeout=None):
    """Wait for element to be clickable."""
    wait = WebDriverWait(driver, _resolved_timeout(timeout))
    return wait.until(EC.element_to_be_clickable((by, value)))


def validate_number(P):
    """Validate numeric input."""
    return P.isdigit() or P == ""
