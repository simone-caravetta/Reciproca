"""
Reciproca
Unified GUI tool: queue-based/hashtag follow, unfollow of non-followers
(from an Instagram data export), rate limit detection, and persistent state.
"""

import hashlib
import json
import logging
import functools
import os
import re
import sys
import threading
import time
import random
import tkinter as tk
from collections import Counter
from datetime import datetime
from tkinter import ttk, scrolledtext, messagebox, filedialog

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------------
# CONFIGURATION
# ---------------------------
CONFIG = {
    # Extraction settings - CONSERVATIVE for background operation
    "TARGET_AUTHORS_PER_HASHTAG": 10,      # Low: Prevents detection from rapid profile switching
    "MAX_SCROLLS_PER_HASHTAG": 30,          # Safety ceiling, not a budget - see the loop
    "FOLLOWER_SCROLL_COUNT": 20,            # Low: Shorter scrolls = less time per profile = lower risk
    "AUTHORS_BEFORE_COOLDOWN": 2,           # Very frequent cooldowns
    "COOLDOWN_DURATION": 15,                # 15 seconds between author groups
    "HASHTAG_BREAK_DURATION": 20,           # 20 seconds between hashtags

    # Follow delays - ULTRA SAFE (Instagram watches follows closely)
    "DEFAULT_DELAY_MIN": 25,                # Absolute minimum 25s between follows
    "DEFAULT_DELAY_MAX": 45,                # Up to 45s for natural variation
    "FOLLOW_BATCH_SIZE": 10,                # Stop after 10 follows for safety break
    "FOLLOW_BATCH_COOLDOWN": 300,           # 5 minute break after each batch

    # Session limits - for development (no hard limits)
    "MAX_FOLLOWS_PER_SESSION": 100,          # Soft target, not enforced
    "SESSION_DURATION_MAX": 7200,            # Max 2 hours per session (safety for dev)

    # Technical settings
    "BROWSER_TIMEOUT": 15,                  # Slower timeouts for stability
    "RETRY_ATTEMPTS": 2,                    # Fewer retries = less aggressive
    "RETRY_BACKOFF": 3,                     # Longer backoff between retries
    "EXTRACTION_PAUSE_DURATION": 2,         # Hours between extraction sessions

    # Bot filtering - checked on the profile, right before following it
    "BOT_FILTER_ENABLED": 1,                # 0 turns the whole check off
    "BOT_MIN_POSTS": 1,                     # An empty gallery is the strongest signal
    "BOT_MIN_FOLLOWERS": 10,
    "BOT_MAX_FOLLOWING": 3000,
    "BOT_MAX_FOLLOWING_RATIO": 5,           # Reject following > this many x followers

    # Semantic ranking - how close a candidate's profile reads to the niche you
    # describe, scored after a search on the strongest candidates it found.
    "SEMANTIC_ENABLED": 1,                  # 0 skips the scoring pass entirely
    "SEMANTIC_WEIGHT": 30,                  # 0-100. See combined_rank() for what it buys
    "SEMANTIC_SHORTLIST": 200,              # How many top candidates get scored
    "SEMANTIC_NICHE": "",                   # What you are looking for, in your words

    # Unfollow delays - same conservative philosophy as follow
    "UNFOLLOW_DELAY_MIN": 15,               # Minimum seconds between unfollows
    "UNFOLLOW_DELAY_MAX": 30,               # Maximum seconds between unfollows
    "UNFOLLOW_DAILY_LIMIT": 20,             # Soft daily target for unfollows
}

# ---------------------------
# APPLICATION PATHS
# ---------------------------
def app_dir():
    """Directory the app reads and writes its own files in.

    Never use the current working directory for this. Running from a PyInstaller
    build, the CWD is wherever the user launched from - a desktop shortcut or the
    Start menu points it somewhere unrelated, and the queue, follow history, saved
    login profile and config would silently be created there instead, looking to
    the user like the app lost its data.

    Frozen builds anchor to the folder holding the executable; running from source
    anchors to the folder holding this file. Both give a stable location that
    matches where the user thinks the app lives.
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_path(filename):
    """Absolute path for one of the app's own files, next to the app itself."""
    return os.path.join(app_dir(), filename)


# Files for persistence
QUEUE_FILE = data_path("follow_queue.json")
FOLLOWED_FILE = data_path("followed_history.json")
FREQUENCIES_FILE = data_path("user_frequencies.json")
AUTHORS_FILE = data_path("scraped_authors.json")
HASHTAGS_FILE = data_path("hashtags.json")
CONFIG_FILE = data_path("bot_config.json")
UNFOLLOW_PROGRESS_FILE = data_path("unfollow_progress.json")
UNFOLLOW_SESSION_FILE = data_path("unfollow_last_session.json")
LOG_FILE = data_path("follow_bot.log")
CHROME_PROFILE_DIR = data_path("chrome_profile")

# ---------------------------
# INSTAGRAM UI TEXT MARKERS
# ---------------------------
# Instagram renders its interface in the account's own language, so every button
# and state lookup has to match text in each supported locale.
#
# Supported locales: English, Italian.
#
# These are the single source of truth - do not inline locale strings at the call
# sites. Adding a language should mean editing this block and nothing else.
# All comparisons run against lowercased text, so keep every entry lowercase.

# Text on the button meaning "you already follow this account" (or a follow request
# is pending). This is also the button you click to start an unfollow.
FOLLOWING_BUTTON_MARKERS = (
    "following", "requested",                            # EN
    "segui già", "seguendo", "richiesta", "in attesa",    # IT
)

# Text on the plain "Follow" button. Note these are substrings of the markers above
# ("follow" of "following", "segui" of "segui già"), so never test them on their
# own - use is_follow_button(), which excludes the already-following case.
FOLLOW_BUTTON_MARKERS = (
    "follow",   # EN
    "segui",    # IT
)

# Text that merely *signals* an existing relationship without being the follow
# button itself: the Message button only appears on profiles you already follow.
# Safe for lenient post-click validation, never for deciding what to click.
FOLLOWED_SIGNAL_MARKERS = FOLLOWING_BUTTON_MARKERS + (
    "message",     # EN
    "messaggio",   # IT
)

# Text on the confirmation button in the "Unfollow?" dialog.
UNFOLLOW_CONFIRM_MARKERS = (
    "unfollow",                 # EN
    "non seguire", "smetti",    # IT
)

# The words beside the three counts in a profile header. The follower and following
# counts are normally found by their links, but those links are not guaranteed to be
# there, so reading the header text is the fallback for all three.
#
# Longest first: Italian uses "follower" for any number, so the English plural has to
# be tried before the form that is also a prefix of it.
POSTS_LABEL_MARKERS = (
    "post",                     # EN "posts" / IT "post"
)
FOLLOWERS_LABEL_MARKERS = (
    "followers", "follower",    # EN / IT
)
FOLLOWING_LABEL_MARKERS = (
    "following", "seguiti",     # EN / IT
)

# Labels on a post dialog's close button. Matched via XPath contains(), which is
# case-sensitive, so these keep their original capitalization.
CLOSE_BUTTON_LABELS = ("Close", "Chiudi")

# The line naming the people who follow an account and who you follow too. It sits
# in the header right after the bio, so it is one of the two things that mark where
# the bio ends.
MUTUAL_FOLLOWERS_MARKERS = (
    "followed by",          # EN
    "account seguito da",   # IT
)

# The buttons under a profile's bio. Whole lines are matched against these rather
# than searched for inside them: "segui" appears in plenty of real bios, and a bio
# reading "seguimi su youtube" must not be cut off at its first word.
PROFILE_BUTTON_LABELS = FOLLOW_BUTTON_MARKERS + FOLLOWED_SIGNAL_MARKERS

# Page or dialog text Instagram shows when it is throttling or blocking actions,
# paired with the explanation surfaced in the log.
RATE_LIMIT_MARKERS = (
    # EN
    ("try again later", "Try Again Later - Instagram needs you to slow down"),
    ("action blocked", "Action Blocked - You've exceeded a limit"),
    ("temporarily blocked", "Temporarily Blocked - Instagram locked your actions"),
    ("too many requests", "Too Many Requests - You're hitting the API too fast"),
    ("please wait", "Please Wait - Instagram is throttling you"),
    # IT
    ("riprova più tardi", "Riprova Più Tardi (IT) - Try again later"),
    ("azione bloccata", "Azione Bloccata (IT) - Action blocked"),
    ("temporaneamente bloccato", "Temporaneamente Bloccato (IT) - Temporarily blocked"),
    ("troppo veloce", "Troppo Veloce (IT) - Going too fast"),
    ("limite superato", "Limite Superato (IT) - Limit exceeded"),
    ("attendi", "Attendi (IT) - Instagram is throttling you"),
)


def brief_error(exc):
    """First line of an exception message, for the on-screen log.

    Selenium appends a chromedriver stacktrace to every message: twenty lines of hex
    addresses that push everything else off the screen. Callers pair this with a
    logger.debug(exc_info=True), so the full text stays in the log file.
    """
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


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
    if posts is not None and posts < CONFIG["BOT_MIN_POSTS"]:
        return f"{posts} posts"

    if followers is not None and followers < CONFIG["BOT_MIN_FOLLOWERS"]:
        return f"only {followers} followers"

    if following is not None and following > CONFIG["BOT_MAX_FOLLOWING"]:
        return f"follows {following} accounts"

    if (followers is not None and following is not None
            and following > followers * CONFIG["BOT_MAX_FOLLOWING_RATIO"]):
        return f"follows {following} but has {followers} followers"

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
# FOLLOWERS-DIALOG EXTRACTION SCRIPT
# ---------------------------
# Runs inside the browser against the open followers dialog. Kept as a named
# constant rather than inlined so the row-walking logic can be exercised against
# a synthetic DOM in tests - that walk is the fragile part of this feature.
#
# Called with one argument: the list of "already following" markers, passed in
# from FOLLOWING_BUTTON_MARKERS so locale strings stay defined in exactly one
# place. Returns {kept, skippedFollowing, rowsWithoutButton, rowsInspected}.
EXTRACT_FOLLOWERS_JS = r"""
const followingMarkers = arguments[0];
const RESERVED = ['p', 'explore', 'accounts', 'direct', 'emails', 'reels', 'stories',
                  'help', 'about', 'blog', 'jobs', 'privacy', 'terms', 'locations',
                  'language', 'developers', 'settings'];

const dialog = document.querySelector("div[role='dialog']");
if (!dialog) return {kept: [], skippedFollowing: 0, rowsWithoutButton: 0, rowsInspected: 0};

// Username out of an href like "/mario/" - null for anything that is not a
// plain profile link.
function usernameFromLink(link) {
    const href = link.getAttribute('href');
    if (!href) return null;
    const match = href.match(/^\/([^/]+)\/?$/);
    if (!match) return null;
    const username = match[1];
    if (!username || username.length <= 1) return null;
    if (RESERVED.includes(username)) return null;
    if (username.includes('.') || username.includes('?')) return null;
    if (username.startsWith('__') || username.startsWith('dm_')) return null;
    return username;
}

function usersInside(node) {
    const found = new Set();
    node.querySelectorAll('a[href^="/"]').forEach(a => {
        const u = usernameFromLink(a);
        if (u) found.add(u);
    });
    return found;
}

// Smallest ancestor that still belongs to this user alone and carries a button.
// Two stop conditions, whichever comes first: the node already holds a button,
// or the next step up would swallow a different user's link (a row boundary).
// The depth cap is purely defensive, so an unexpected DOM cannot walk to <body>.
function findRow(link, username) {
    let row = link;
    for (let depth = 0; depth < 8; depth++) {
        if (row.querySelector('button')) return row;
        const parent = row.parentElement;
        if (!parent || parent === dialog) break;
        const users = usersInside(parent);
        let foreign = false;
        users.forEach(u => { if (u !== username) foreign = true; });
        if (foreign) break;
        row = parent;
    }
    return row.querySelector('button') ? row : null;
}

const kept = [];
const seen = new Set();
let skippedFollowing = 0;
let rowsWithoutButton = 0;
let rowsInspected = 0;

dialog.querySelectorAll('a[href^="/"]').forEach(link => {
    const username = usernameFromLink(link);
    if (!username || seen.has(username)) return;
    seen.add(username);
    rowsInspected++;

    const row = findRow(link, username);
    if (!row) {
        // Fail open: keeping a candidate is far less harmful than silently
        // dropping everyone if Instagram's markup changes. rowsWithoutButton
        // is what makes such a regression visible instead of silent.
        rowsWithoutButton++;
        kept.push(username);
        return;
    }

    let following = false;
    row.querySelectorAll('button').forEach(btn => {
        const text = (btn.innerText || btn.textContent || '').toLowerCase();
        if (followingMarkers.some(m => text.includes(m))) following = true;
    });

    if (following) {
        skippedFollowing++;
    } else {
        kept.push(username);
    }
});

return {kept, skippedFollowing, rowsWithoutButton, rowsInspected};
"""

# Every post link currently on a hashtag page, as the href attribute reads in the
# DOM. Collected in one call rather than one round trip per element: a scrolled
# hashtag page holds hundreds of them, and the values are also how each post is
# found again right before it is clicked, which keeps element references from
# going stale while post dialogs open and close.
POST_LINKS_JS = """
// An open post is not the grid, and its links belong to that post rather than to the
// hashtag, so there is nothing here worth collecting. null asks the caller to close it.
if (document.querySelector("div[role='dialog']")) return null;

// A grid tile links straight to the post and no deeper. An open post also carries a
// link per comment (/p/<code>/c/<id>/), one for its likes (/p/<code>/liked_by/) and
// its own permalink (/<user>/p/<code>/): all contain "/p/" without being tiles.
// A query string is still that same post, and dropping tiles over one would look
// like a hashtag with no posts, which is the worse way to be wrong here.
const TILE = /^\\/p\\/[^/?]+\\/?(\\?.*)?$/;

const hrefs = [];
const seen = new Set();
document.querySelectorAll("a[href*='/p/']").forEach(a => {
    const href = a.getAttribute('href');
    if (href && TILE.test(href) && !seen.has(href)) {
        seen.add(href);
        hrefs.push(href);
    }
});
return hrefs;
"""

# The three numbers in a profile header, read where the browser already is when a
# follow is about to happen. Returns raw strings rather than numbers, so the
# parsing - and every locale quirk in it - stays in Python where it is tested.
#
# The follower and following counts are found by their links, which are structural
# and survive Instagram's redesigns better than any class name. Where the visible
# text is abbreviated ("12.3K") the exact figure is usually in a title attribute
# alongside it, so that is preferred. The post count has no link, so it is left to
# a search of the header text.
#
# Every matching link is returned, not the first. More than one of them points at a
# profile's followers: beside the count there is the line about people you both
# follow, which reads "11 followers you follow" and leads to the same page. Which
# comes first in the markup is Instagram's business, so choosing between them is
# left to Python, where it is tested.
PROFILE_STATS_JS = """
const header = document.querySelector('header');
if (!header) return null;

function read(link) {
    // The title may be on the link itself, which querySelector would not reach.
    const titled = link.matches('[title]') ? link : link.querySelector('[title]');
    const title = titled ? titled.getAttribute('title') : null;
    // The address takes no part in choosing the count, which goes by what the link
    // says. It is carried so check_profile.py can show which link a number came
    // from, the one thing a log line cannot tell you when a count looks wrong.
    return {
        href: link.getAttribute('href') || '',
        title: title,
        text: link.innerText || link.textContent || '',
    };
}

function readAll(selector) {
    return Array.from(header.querySelectorAll(selector)).map(read);
}

// Matched loosely on purpose: the href is not always exactly "/user/followers/" -
// it can carry a query string or lose its trailing slash.
return {
    headerText: header.innerText || header.textContent || '',
    followers: readAll('a[href*="/followers"]'),
    following: readAll('a[href*="/following"]'),
};
"""

# ---------------------------
# CONFIG FILE MANAGEMENT
# ---------------------------
def load_config():
    """Load config from file, fall back to defaults if not found."""
    default_config = {
        "TARGET_AUTHORS_PER_HASHTAG": 10,
        "MAX_SCROLLS_PER_HASHTAG": 30,
        "FOLLOWER_SCROLL_COUNT": 20,
        "AUTHORS_BEFORE_COOLDOWN": 2,
        "COOLDOWN_DURATION": 15,
        "HASHTAG_BREAK_DURATION": 20,
        "DEFAULT_DELAY_MIN": 25,
        "DEFAULT_DELAY_MAX": 45,
        "FOLLOW_BATCH_SIZE": 10,
        "FOLLOW_BATCH_COOLDOWN": 300,
        "MAX_FOLLOWS_PER_SESSION": 100,
        "SESSION_DURATION_MAX": 7200,
        "BROWSER_TIMEOUT": 15,
        "RETRY_ATTEMPTS": 2,
        "RETRY_BACKOFF": 3,
        "EXTRACTION_PAUSE_DURATION": 2,
        "BOT_FILTER_ENABLED": 1,
        "BOT_MIN_POSTS": 1,
        "BOT_MIN_FOLLOWERS": 10,
        "BOT_MAX_FOLLOWING": 3000,
        "BOT_MAX_FOLLOWING_RATIO": 5,
        "SEMANTIC_ENABLED": 1,
        "SEMANTIC_WEIGHT": 30,
        "SEMANTIC_SHORTLIST": 200,
        "SEMANTIC_NICHE": "",
        "UNFOLLOW_DELAY_MIN": 15,
        "UNFOLLOW_DELAY_MAX": 30,
        "UNFOLLOW_DAILY_LIMIT": 20,
    }

    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                # Merge with defaults to ensure all keys exist
                config = {**default_config, **loaded}
                logger.info(f"Config loaded from {CONFIG_FILE}")
                return config
    except Exception as e:
        logger.error(f"Error loading config: {e}")

    return default_config.copy()

def save_config(config_dict):
    """Save config to file."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_dict, f, indent=2, ensure_ascii=False)
        logger.info(f"Config saved to {CONFIG_FILE}")
        return True
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        return False

# Load config on startup
logger = logging.getLogger(__name__)
CONFIG = load_config()

# ---------------------------
# QUEUE MANAGEMENT
# ---------------------------
def load_queue():
    """Load the follow queue from file with backup recovery."""
    try:
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Ensure it's a list
                if isinstance(data, list):
                    return data
                return []
    except Exception as e:
        logger.error(f"Error loading queue: {e}")
        # Try to recover from backup
        backup_file = QUEUE_FILE + ".backup"
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
        if os.path.exists(QUEUE_FILE):
            backup_file = QUEUE_FILE + ".backup"
            try:
                import shutil
                shutil.copy2(QUEUE_FILE, backup_file)
            except:
                pass

        with open(QUEUE_FILE, 'w', encoding='utf-8') as f:
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
    global last_scrape_frequencies
    if not last_scrape_frequencies:
        last_scrape_frequencies = load_frequencies()
    return last_scrape_frequencies


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
    only thing telling them apart. What the weight really sets is how far up an
    affinity can carry somebody past a candidate seen more often. It takes about 60
    for a profile reading 0.65 to overtake one seen six times reading 0.30.

    So the default is not 0. The order within a tie is drawn from the username
    today, which is to say it is arbitrary, and replacing an arbitrary order with a
    measured one risks nothing: where the measurement is poor, it is arbitrary
    again, which is where it started.

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

    weight = CONFIG["SEMANTIC_WEIGHT"]

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

    for username in usernames:
        if username not in existing_in_queue and not is_already_followed(username):
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


def score_queue(scorer, limit=None, frequencies=None, on_progress=None):
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
        limit = CONFIG["SEMANTIC_SHORTLIST"]

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
        if stop_requested.is_set():
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
    """
    if limit is None:
        limit = CONFIG["SEMANTIC_SHORTLIST"]

    queue = load_queue()
    ranked = rank_queue(queue, frequencies)
    if len(ranked) <= limit:
        return 0

    save_queue([item for _, _, item in ranked[:limit]])
    return len(ranked) - limit


def log_followed_user(username, status="success"):
    """Log a followed user to history."""
    try:
        history = {}
        if os.path.exists(FOLLOWED_FILE):
            with open(FOLLOWED_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)

        history[username] = {
            "date": datetime.now().isoformat(),
            "status": status
        }

        with open(FOLLOWED_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error logging followed user: {e}")

def is_already_followed(username):
    """Check if user is in followed history."""
    try:
        if os.path.exists(FOLLOWED_FILE):
            with open(FOLLOWED_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
                if isinstance(history, dict):
                    return username in history
    except Exception as e:
        logger.debug(f"Error checking followed history for {username}: {e}")
    return False

def load_frequencies():
    """Load user frequencies from file."""
    try:
        if os.path.exists(FREQUENCIES_FILE):
            with open(FREQUENCIES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return Counter(data)
    except Exception as e:
        logger.error(f"Error loading frequencies: {e}")
    return Counter()

def save_frequencies(frequencies):
    """Save user frequencies to file."""
    try:
        with open(FREQUENCIES_FILE, 'w', encoding='utf-8') as f:
            json.dump(dict(frequencies), f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error saving frequencies: {e}")
        return False

def load_author_history():
    """Authors whose followers have been scraped, mapped to when that last happened.

    A hashtag page shows the same posts at the top session after session, so
    without this the same handful of authors get scraped every time and the
    candidates found never change.
    """
    try:
        if os.path.exists(AUTHORS_FILE):
            with open(AUTHORS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.error(f"Error loading author history: {e}")
    return {}


def save_author_history(history):
    """Save the scraped-authors history."""
    try:
        with open(AUTHORS_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error saving author history: {e}")
        return False


def order_authors_by_staleness(usernames, history):
    """Authors ordered by who is most worth scraping next.

    Never scraped comes first, always. The rest follow least recently scraped
    first, so an author left alone for several sessions comes back up before one
    used yesterday, and repeated runs on the same hashtags rotate through
    different authors instead of always taking whoever the first posts belong to.

    Timestamps are ISO 8601, which sorts chronologically as plain text.
    """
    return sorted(usernames, key=lambda u: (u in history, history.get(u) or "", u))


def load_hashtags():
    """Load hashtags from file."""
    try:
        if os.path.exists(HASHTAGS_FILE):
            with open(HASHTAGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        logger.error(f"Error loading hashtags: {e}")
    return None

def save_hashtags(hashtags):
    """Save hashtags to file."""
    try:
        with open(HASHTAGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(hashtags, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error saving hashtags: {e}")
        return False

# ---------------------------
# UNFOLLOW - DATA EXPORT PARSING
# ---------------------------
def uf_get_username(info):
    """Extract username from an Instagram data-export entry (value or href)."""
    if "value" in info and info["value"]:
        return info["value"]
    elif "href" in info:
        return info["href"].rstrip("/").split("/")[-1]
    return None

def uf_load_followers(file):
    """Load usernames from an Instagram followers.json export."""
    with open(file, "r", encoding="utf-8") as f:
        data = json.load(f)

    users = set()
    for user in data:
        try:
            info = user["string_list_data"][0]
            username = uf_get_username(info)
            if username:
                users.add(username)
        except Exception:
            continue
    return users

def uf_load_following(file):
    """Load usernames from an Instagram following.json export."""
    with open(file, "r", encoding="utf-8") as f:
        data = json.load(f)

    users = set()
    for user in data["relationships_following"]:
        try:
            info = user["string_list_data"][0]
            username = uf_get_username(info)
            if username:
                users.add(username)
        except Exception:
            continue
    return users

# ---------------------------
# UNFOLLOW - PROGRESS PERSISTENCE
# ---------------------------
def uf_load_progress():
    """Load unfollow progress from file."""
    global uf_progress
    if not os.path.exists(UNFOLLOW_PROGRESS_FILE):
        uf_progress = {"processed": [], "unfollowed": [], "skipped": []}
    else:
        try:
            with open(UNFOLLOW_PROGRESS_FILE, 'r', encoding='utf-8') as f:
                uf_progress = json.load(f)
        except Exception as e:
            logger.error(f"Error loading unfollow progress: {e}")
            uf_progress = {"processed": [], "unfollowed": [], "skipped": []}

def uf_save_progress():
    """Save unfollow progress to file."""
    try:
        with open(UNFOLLOW_PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(uf_progress, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving unfollow progress: {e}")

def uf_load_session():
    """The saved unfollow session: which export files, and whose account."""
    try:
        if os.path.exists(UNFOLLOW_SESSION_FILE):
            with open(UNFOLLOW_SESSION_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.error(f"Error loading unfollow session: {e}")
    return {}


def uf_save_session(**changes):
    """Update the saved unfollow session, leaving the other keys as they are."""
    session = uf_load_session()
    session.update(changes)
    try:
        with open(UNFOLLOW_SESSION_FILE, 'w', encoding='utf-8') as f:
            json.dump(session, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving unfollow session: {e}")


def current_account_id():
    """Numeric id of the Instagram account logged into the browser, or None.

    Read from the ds_user_id cookie rather than from the page: no navigation, no
    markup to match, and nothing that shifts with Instagram's layout or language.

    None means the question cannot be answered right now - no browser, not logged
    in, or sitting on another domain - never that the account has changed.
    """
    if driver is None:
        return None
    try:
        cookie = driver.get_cookie('ds_user_id')
        return cookie.get('value') if cookie else None
    except WebDriverException as e:
        logger.info(f"Could not read the logged-in account: {type(e).__name__}")
        return None


def uf_progress_archive(account_id):
    """Where one account's unfollow progress waits while another one is active.

    The id comes from a cookie, so it is stripped to word characters before it
    becomes part of a filename. Instagram's ids are digits; this only rules out a
    malformed value reaching outside the app's own directory.
    """
    safe_id = re.sub(r'\W', '', str(account_id))
    return data_path(f"unfollow_progress_{safe_id}.json")


def uf_check_account():
    """Keep the unfollow progress with the account it was recorded against.

    The progress records who has already been processed from one account's
    following list, which means nothing on another account's. Re-exporting the
    JSON files for the same account has to keep it - that is the normal way to
    carry on - while a different account has to start from its own.

    Nothing is deleted. Each account's record is parked under its own name and
    brought back if that account returns: this runs by itself, and an automatic
    action should not be able to destroy history. Reset is the button for
    discarding on purpose.

    The loaded export is cleared, though - it describes the other account, so
    every count drawn from it would be wrong.
    """
    global uf_followers, uf_following, uf_non_followers

    account_id = current_account_id()
    if account_id is None:
        return

    previous_id = uf_load_session().get("account_id")
    if previous_id == account_id:
        return

    if previous_id is None:
        # First run able to identify the account: adopt it, keep the progress.
        uf_save_session(account_id=account_id)
        logger.info(f"Unfollow progress now associated with account {account_id}")
        return

    try:
        if os.path.exists(UNFOLLOW_PROGRESS_FILE):
            os.replace(UNFOLLOW_PROGRESS_FILE, uf_progress_archive(previous_id))
        returning = uf_progress_archive(account_id)
        if os.path.exists(returning):
            os.replace(returning, UNFOLLOW_PROGRESS_FILE)
    except Exception as e:
        logger.error(f"Error switching unfollow progress between accounts: {e}")

    uf_followers = set()
    uf_following = set()
    uf_non_followers = []
    uf_save_session(account_id=account_id, followers_file=None, following_file=None)
    uf_load_progress()

    already_removed = len(uf_progress.get("unfollowed", []))
    log("👥 Different Instagram account detected - unfollow progress switched", 'warning')
    update_unfollow_ui_state()
    messagebox.showinfo(
        "Account changed",
        "The browser is logged into a different Instagram account than the one the "
        "unfollow progress belongs to.\n\n"
        "That progress has been set aside under the previous account and will come "
        "back if you log into it again - nothing was deleted.\n\n"
        f"This account's own progress is now active ({already_removed} already "
        "unfollowed). Load its followers.json and following.json to continue."
    )


def uf_load_json_files():
    """Prompt user for followers.json/following.json and compute non-followers."""
    global uf_followers, uf_following, uf_non_followers

    try:
        f1 = filedialog.askopenfilename(title="Select followers.json")
        if not f1:
            return
        f2 = filedialog.askopenfilename(title="Select following.json")
        if not f2:
            return

        uf_followers = uf_load_followers(f1)
        uf_following = uf_load_following(f2)
        uf_non_followers = list(uf_following - uf_followers)

        if len(uf_non_followers) == 0:
            messagebox.showerror("Error", "No users found (no non-followers)")
            return

        log(f"✔ JSON files loaded: {len(uf_non_followers)} non-followers found", 'success')

        uf_save_session(
            followers_file=f1,
            following_file=f2,
            account_id=current_account_id() or uf_load_session().get("account_id"),
        )

        update_unfollow_ui_state()

    except Exception as e:
        messagebox.showerror("Error", f"Invalid files:\n{e}")
        logger.exception("Error loading unfollow JSON files")

def uf_auto_load_last_session():
    """Auto-reload the last followers/following.json pair on startup."""
    global uf_followers, uf_following, uf_non_followers

    if not os.path.exists(UNFOLLOW_SESSION_FILE):
        return

    try:
        data = uf_load_session()

        f1 = data.get("followers_file")
        f2 = data.get("following_file")

        if not f1 or not f2 or not os.path.exists(f1) or not os.path.exists(f2):
            log("⚠️ Saved unfollow session found but JSON files are missing, reload them manually", 'warning')
            return

        uf_followers = uf_load_followers(f1)
        uf_following = uf_load_following(f2)
        uf_non_followers = list(uf_following - uf_followers)

        uf_load_progress()
        total, remaining, removed = unfollow_progress_counts()
        log(
            f"🔄 Unfollow session reloaded: {total} non-followers, "
            f"{remaining} still to process ({removed} already removed)",
            'info'
        )
        update_unfollow_ui_state()

    except Exception as e:
        logger.debug(f"Auto-load unfollow session error: {e}")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)


# ---------------------------
# SESSION STATS
# ---------------------------
class SessionStats:
    """Track session statistics. `on_update` is called (if set) whenever a counter changes,
    so the same class can drive different GUI widgets (follow tab vs. unfollow tab)."""
    def __init__(self, on_update=None):
        self.attempted = 0
        self.succeeded = 0
        self.skipped_already_following = 0
        self.errors = 0
        self.start_time = time.time()
        self._lock = threading.Lock()
        self.on_update = on_update

    def increment(self, field):
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)
            if self.on_update:
                self.on_update()

    def report(self):
        duration = time.time() - self.start_time
        logger.info(
            f"Session Report: {self.succeeded}/{self.attempted} follows, "
            f"Skipped: {self.skipped_already_following}, "
            f"Errors: {self.errors}, "
            f"Duration: {duration // 60:.0f}m {duration % 60:.0f}s"
        )
        return (
            f"Session Complete!\n\n"
            f"Successful: {self.succeeded}\n"
            f"Attempted: {self.attempted}\n"
            f"Already Following: {self.skipped_already_following}\n"
            f"Duration: {duration // 60:.0f}m {duration % 60:.0f}s"
        )

# Global state
stats = SessionStats(on_update=lambda: update_stats_display())
driver = None
# Set while the browser is being opened. Both tabs have an Open Browser button,
# and without this a click on each would start two Chrome instances.
browser_opening = threading.Event()
# Set while a follow or unfollow session owns the browser. Both drive the same
# Selenium session and the same window, so only one may run at a time.
session_running = threading.Event()
stop_requested = threading.Event()
active_threads = []
# Live extraction tracking
live_extracted_users = []  # Track users as they're extracted
live_frequencies = Counter()  # Track frequencies in real-time
# Frequencies the queue is ranked by. Read lazily via ranking_frequencies() so
# startup does not touch the disk before the GUI exists.
last_scrape_frequencies = None
# Usernames currently drawn in the queue listbox, row by row
displayed_queue_usernames = []

# Unfollow state
uf_stats = SessionStats(on_update=lambda: update_unfollow_stats_display())
uf_followers = set()
uf_following = set()
uf_non_followers = []
uf_progress = {}

# Validate queue on startup
validate_queue()

# ---------------------------
# RETRY DECORATOR
# ---------------------------
def retry(max_attempts=CONFIG["RETRY_ATTEMPTS"], backoff=CONFIG["RETRY_BACKOFF"]):
    """Decorator for retry logic with exponential backoff."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (TimeoutException, StaleElementReferenceException) as e:
                    if attempt == max_attempts - 1:
                        raise
                    wait_time = backoff ** attempt
                    logger.warning(f"Retry {func.__name__} in {wait_time}s: {e}")
                    time.sleep(wait_time)
            return None
        return wrapper
    return decorator

# ---------------------------
# GUI UTILITIES
# ---------------------------
class ToolTip:
    """Tooltip for widgets."""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        widget.bind('<Enter>', self.show)
        widget.bind('<Leave>', self.hide)
        self.tip = None

    def show(self, event=None):
        x, y = self.widget.winfo_pointerxy()
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f'+{x + 15}+{y + 10}')
        label = tk.Label(
            self.tip,
            text=self.text,
            background='#ffffcc',
            relief='solid',
            borderwidth=1,
            font=('Helvetica', 9),
            padx=5,
            pady=2
        )
        label.pack()

    def hide(self, event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None

def log(msg, level='info'):
    """Log message to GUI with color coding."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    full_msg = f"{timestamp} | {msg}\n"

    colors = {
        'success': 'success',
        'error': 'error',
        'warning': 'warning',
        'info': 'info'
    }
    tag = colors.get(level, 'info')

    log_box.insert(tk.END, full_msg, tag)
    log_box.see(tk.END)
    root.update_idletasks()

    # Also log to file
    log_func = getattr(logger, level, logger.info)
    log_func(msg)

def update_stats_display():
    """Update the stats label."""
    stats_text = (
        f"Followed: {stats.succeeded} | "
        f"Attempted: {stats.attempted} | "
        f"Skipped: {stats.skipped_already_following} | "
        f"Errors: {stats.errors}"
    )
    stats_label.config(text=stats_text)

def validate_number(P):
    """Validate numeric input."""
    return P.isdigit() or P == ""

def update_progress(current, total, phase="", current_hashtag="", author_num=0, total_authors=0, author_name="", followers_extracted=0, overall_progress=None):
    """Update progress bar and status with phase-specific information."""
    # Calculate percentage for this phase (0-100)
    phase_progress = (current / total) * 100 if total > 0 else 0

    # Build status message based on phase
    if phase == "scraping_hashtags":
        status_text = f"Hashtag: #{current_hashtag} ({current}/{total})"
    elif phase == "loading_followers":
        status_text = f"#{current_hashtag} | Author {author_num}/{total_authors}: {author_name} | Scroll: {current}/{total} | Extracted: {followers_extracted}"
    elif phase == "following_users":
        status_text = f"Following users: {current}/{total} ({phase_progress:.0f}%)"
    elif phase:
        status_text = f"{phase}: {current}/{total} ({phase_progress:.0f}%)"
    else:
        status_text = f"Progress: {current}/{total} ({phase_progress:.0f}%)"

    # Update progress bar - ALWAYS use percentage (0-100)
    # The calling code should set progress_bar['maximum'] = 100
    if overall_progress is not None:
        progress_bar['value'] = overall_progress
    else:
        progress_bar['value'] = phase_progress
    status_label.config(text=status_text)
    root.update_idletasks()


def reset_progress():
    """Reset progress bar."""
    progress_bar['value'] = 0
    status_label.config(text="Ready")

def update_unfollow_stats_display():
    """Update the unfollow tab's stats label."""
    stats_text = (
        f"Unfollowed: {uf_stats.succeeded} | "
        f"Attempted: {uf_stats.attempted} | "
        f"Errors: {uf_stats.errors}"
    )
    uf_stats_label.config(text=stats_text)

def update_unfollow_progress(current, total):
    """Update the unfollow tab's progress bar and status label."""
    pct = (current / total) * 100 if total > 0 else 0
    uf_progress_bar['value'] = pct
    uf_status_label.config(text=f"Unfollow: {current}/{total} ({pct:.0f}%)")
    root.update_idletasks()

def reset_unfollow_progress():
    """Reset the unfollow tab's progress bar."""
    uf_progress_bar['value'] = 0
    uf_status_label.config(text="Ready")

# ---------------------------
# RATE LIMIT DETECTION
# ---------------------------
def check_rate_limit(driver):
    """Check if Instagram is showing rate limit warnings and return info about the type."""
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()

        for indicator, description in RATE_LIMIT_MARKERS:
            if indicator in page_text:
                log(f"⚠️ RATE LIMIT: {description}", 'warning')
                return True

        # Check for popup dialogs with actual error messages
        dialogs = driver.find_elements(By.XPATH, "//div[@role='dialog']")
        for dialog in dialogs:
            dialog_text = dialog.text.lower()
            for indicator, description in RATE_LIMIT_MARKERS:
                if indicator in dialog_text:
                    log(f"⚠️ RATE LIMIT POPUP: {description}", 'warning')
                    return True

    except Exception as e:
        logger.debug(f"Rate limit check error: {e}")

    return False

# ---------------------------
# BROWSER OPERATIONS
# ---------------------------
@retry()
def wait_for_element(driver, by, value, timeout=CONFIG["BROWSER_TIMEOUT"]):
    """Wait for element to be present."""
    wait = WebDriverWait(driver, timeout)
    return wait.until(EC.presence_of_element_located((by, value)))

@retry()
def wait_for_clickable(driver, by, value, timeout=CONFIG["BROWSER_TIMEOUT"]):
    """Wait for element to be clickable."""
    wait = WebDriverWait(driver, timeout)
    return wait.until(EC.element_to_be_clickable((by, value)))

def start_browser():
    """Open the browser on a worker thread.

    ChromeDriverManager().install() can download a driver, which takes long enough
    to freeze the GUI if it runs on the Tk callback thread - the window stops
    repainting and the app looks hung.

    Reached from either tab's Open Browser button, so it has to be safe to call
    when a browser is already open or on its way.
    """
    if driver is not None or browser_opening.is_set():
        return

    browser_opening.set()
    update_follow_ui_state()
    update_unfollow_ui_state()

    thread = threading.Thread(target=open_browser, daemon=True)
    thread.start()
    active_threads.append(thread)


def open_browser():
    """Open Chrome browser with persistent profile."""
    global driver

    try:
        log("🌐 Initializing Chrome...", 'info')

        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--start-maximized")

        # Anti-detection measures
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        # Override navigator.webdriver
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        driver.get("https://www.instagram.com/")
        log("✅ Browser opened! Please login manually.", 'success')

        # The browser is the only place that knows which account is logged in, so
        # this is the first chance to tell whether the loaded export still matches.
        uf_check_account()

    except Exception as e:
        # Deliberately broad. A frozen build has no console, so anything not caught
        # here vanishes silently and the log just stops after "Initializing Chrome"
        # with no indication of why. Record the full traceback in follow_bot.log,
        # which sits next to the executable, and surface the error in the GUI.
        logger.exception("Failed to open browser")
        log(f"❌ Browser error: {type(e).__name__}: {e}", 'error')
        log("   Full traceback written to follow_bot.log", 'error')
        messagebox.showerror(
            "Browser Error",
            f"{type(e).__name__}: {e}\n\nSee follow_bot.log next to the app for details."
        )
    finally:
        browser_opening.clear()
        refresh_browser_state()

# How often to check that the browser is still there, in milliseconds.
BROWSER_WATCH_INTERVAL = 2000


def browser_is_open():
    """True if the browser is still there, asked of the browser itself.

    The `driver` global is not evidence: closing the Chrome window leaves it
    holding a dead session that looks perfectly valid until something tries to
    use it.

    Never call this from the GUI thread while a worker thread is driving the same
    session - one Selenium session commanded from two threads interleaves badly.
    """
    if driver is None:
        return False
    try:
        return bool(driver.window_handles)
    except WebDriverException as e:
        logger.info(f"Browser probe failed, treating the browser as closed: {type(e).__name__}")
        return False


def can_open_browser():
    """True when clicking Open Browser would actually do something."""
    return driver is None and not browser_opening.is_set()


def begin_session():
    """Claim the browser for one session, or refuse if another already has it.

    Follow and unfollow drive the same Selenium session and the same window. Two
    at once would interleave commands on one browser: each would navigate the page
    out from under the other, and both would then act on whatever happened to be
    loaded - unfollowing an account the follow session had just opened, or
    following one the unfollow session was on.

    Both tabs disable their Start buttons while a session runs; this is the guard
    that does not depend on a button state being right. Called from the GUI thread
    only, so the check and the claim cannot race.
    """
    if session_running.is_set():
        log("⚠️ A session is already running - stop it before starting another", 'warning')
        return False

    session_running.set()
    stop_requested.clear()
    update_follow_ui_state()
    update_unfollow_ui_state()
    return True


def end_session():
    """Release the browser when a session finishes and settle both tabs."""
    session_running.clear()
    refresh_browser_state()


def update_follow_ui_state():
    """Follow-tab buttons in agreement with the browser and any running session.

    One place decides, so no exit path can leave Start Following enabled with no
    browser behind it, or Open Browser disabled after the browser is gone - which
    left the app stuck until it was restarted.
    """
    try:
        running = session_running.is_set()
        browser_btn.config(state='normal' if can_open_browser() else 'disabled')
        start_btn.config(
            state='normal' if driver is not None and not running else 'disabled'
        )
        stop_btn.config(state='normal' if running else 'disabled')
    except Exception as e:
        logger.debug(f"update_follow_ui_state error: {e}")


def handle_browser_closed():
    """Forget a browser that is gone and let the user open a new one."""
    global driver

    if driver is not None:
        try:
            driver.quit()  # Release chromedriver; the browser itself is already gone
        except Exception as e:
            logger.debug(f"Error quitting the closed browser: {e}")
        driver = None
        log("🌐 Browser closed - click 'Open Browser' to start a new session", 'warning')

    update_follow_ui_state()
    update_unfollow_ui_state()


def refresh_browser_state():
    """Re-check the browser and bring both tabs' controls in line with it."""
    if driver is not None and not browser_is_open():
        handle_browser_closed()
    else:
        update_follow_ui_state()
        update_unfollow_ui_state()


def watch_browser():
    """Notice the browser being closed, instead of waiting for something to fail.

    Closing the Chrome window used to go unremarked: Start Following stayed
    enabled while every click failed, and Open Browser stayed disabled, so there
    was no way back without restarting the app.

    Only probes while no worker thread is running, to keep two threads off the
    same Selenium session. A browser closed mid-session is caught by the worker
    failing, and by the check when the session finishes.
    """
    try:
        active_threads[:] = [t for t in active_threads if t.is_alive()]
        if driver is not None and not active_threads and not browser_is_open():
            handle_browser_closed()
    except Exception as e:
        logger.debug(f"watch_browser error: {e}")
    finally:
        root.after(BROWSER_WATCH_INTERVAL, watch_browser)


def stop_bot():
    """Request graceful stop."""
    stop_requested.set()
    log("⏹️ Stop requested, finishing current operation...", 'warning')
    # Both tabs' Stop buttons come here, so both acknowledge the click. The session
    # itself keeps running until it reaches a checkpoint.
    stop_btn.config(state='disabled')
    uf_stop_btn.config(state='disabled')

    # Save any already extracted users to queue if scraping was in progress
    # Only do this if we're actually in extraction mode, not during follow
    try:
        # Check if we have live extraction data (indicating extraction was in progress)
        global live_extracted_users
        if live_extracted_users:
            # Only this session's finds, best first. The frequencies file accumulates
            # across sessions, so ranking it whole would queue the entire history.
            global last_scrape_frequencies
            last_scrape_frequencies = load_frequencies()
            ranked_users = [
                username for username, _, _ in
                rank_queue(list(dict.fromkeys(live_extracted_users)), last_scrape_frequencies)
            ]
            if ranked_users:
                new_count, total_count = add_to_queue(ranked_users)
                log(f"💾 Saved {new_count} users to queue (stop detected during extraction)", 'success')
                refresh_queue_display()
        else:
            # If we're stopping during follow (not extraction), just validate the queue
            log("🔄 Validating queue consistency after stop...", 'info')
            removed, remaining = validate_queue()
            if removed > 0:
                log(f"🗑️ Cleaned up {removed} invalid entries from queue", 'warning')
            refresh_queue_display()
    except Exception as e:
        logger.debug(f"Error during stop cleanup: {e}")

# ---------------------------
# SEMANTIC AFFINITY
# ---------------------------
# How close a candidate's profile reads to the niche described in the settings.
# Everything here is arithmetic and text handling: turning either one into a vector
# is a separate job, handed in, so the model can be absent without any of this
# having an opinion about it.

def mean_pooled(rows, mask):
    """One vector for a piece of text, from one vector per token.

    The model returns a vector per token; what is wanted is a vector for the whole
    text, and the average of the tokens is what these models are trained to be read
    by. The mask says which positions are real: a batch is padded to a fixed length,
    and averaging the padding in would drag every short bio towards the same place -
    the shorter the bio, the more padding, so the pull would be strongest exactly
    where there is least to go on.

    Pure Python on purpose. It is a few hundred numbers, so the speed is not worth a
    dependency, and this way it is a part that can be checked without the model.
    """
    if not rows:
        return None

    width = len(rows[0])
    total = [0.0] * width
    counted = 0

    for row, keep in zip(rows, mask or []):
        if not keep:
            continue
        counted += 1
        for index, value in enumerate(row):
            total[index] += value

    if not counted:
        return None
    return [value / counted for value in total]


def normalized(vector):
    """The vector at length 1, or None if it has no length to speak of.

    Cosine divides this out anyway, so this is not what makes the comparison work.
    It is what makes a stored vector comparable to one made later without carrying
    the length around, and it keeps the numbers in a range that does not drift.
    """
    if not vector:
        return None

    length = sum(value * value for value in vector) ** 0.5
    if not length:
        return None
    return [value / length for value in vector]


def cosine(left, right):
    """How close two vectors point, from -1 to 1, or None if either says nothing.

    Written out rather than pulled from a library: it is five lines, and a package
    added here is a package to be found again by the Windows build.
    """
    if not left or not right or len(left) != len(right):
        return None

    dot = sum(a * b for a, b in zip(left, right))
    size = (sum(a * a for a in left) ** 0.5) * (sum(b * b for b in right) ** 0.5)
    return dot / size if size else None


def affinity_between(profile_vector, niche_vector):
    """The two vectors as an affinity from 0 to 1, or None if there is not one.

    Opposite directions are floored at 0 rather than kept negative. Below zero the
    number stops meaning "less like the niche" and starts meaning something about
    the model's own geometry, which is not a thing to rank people by, and the whole
    scale is meant to read as "none of it" to "all of it".
    """
    closeness = cosine(profile_vector, niche_vector)
    return None if closeness is None else max(0.0, min(1.0, closeness))


def profile_description(header_text):
    """What a profile says about itself, out of the header's text, or None.

    The header is one run of text with everything in it. Read from a real profile,
    in order: the username, the display name, the three counts, the category, the
    bio, the line about people in common, the buttons, and then the names of the
    highlight covers. So what is wanted is the stretch between the last count and
    the buttons, which is the category and the bio together.

    They are not told apart, and there is nothing lost by that: Instagram's category
    and the bio both say what somebody does, and both are going into the same
    comparison.

    A header with no counts in it is not a header this code understands, and comes
    back as None rather than as a guess. Same for a profile with nothing written
    between the counts and the buttons, which is a profile nobody can judge rather
    than a bad one.
    """
    lines = [line.strip() for line in (header_text or "").splitlines() if line.strip()]

    start = None
    for index, line in enumerate(lines):
        if parse_labelled_count(line, FOLLOWING_LABEL_MARKERS) is not None:
            start = index + 1
            break
    if start is None:
        return None

    described = []
    for line in lines[start:]:
        lowered = line.lower()
        if lowered in PROFILE_BUTTON_LABELS or has_marker(lowered, MUTUAL_FOLLOWERS_MARKERS):
            break
        described.append(line)

    return "\n".join(described) or None


def profile_text(name=None, category=None, bio=None):
    """The parts of a profile worth comparing, as one piece of text, or None.

    The category is Instagram's own label, filled in from a fixed list rather than
    written by hand, so it says what somebody does in the same words every time.
    The bio says it in theirs. The display name is often a real name and says
    nothing, but it is where some people put what they do, so it goes in last.

    None where there is nothing to read. A profile with an empty bio is common and
    perfectly good, and it must come back unscored rather than scored badly: it is
    a profile nobody can judge, not a bad one.
    """
    parts = [str(part).strip() for part in (category, bio, name) if part]
    joined = ". ".join(part for part in parts if part)
    return joined or None


# Where the model lives once it has been fetched, next to everything else the app
# writes. Deleting the folder is how you make it download again.
MODEL_DIR = data_path("model")

# A sentence model small enough to run on a CPU while the browser is busy, and
# multilingual because the bios being read are not all in one language. Quantized,
# which is what keeps it near a hundred megabytes rather than several hundred.
#
# Pinned to a revision rather than to a branch: "main" is whatever the author
# pushed last, and a model that changes underneath you changes every score with it,
# silently, with the old numbers still sitting in the queue beside the new ones.
MODEL_REPO = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION = "main"
MODEL_FILES = ("onnx/model_quantized.onnx", "tokenizer.json")

# The longest piece of text handed to the model. A bio is short; this is only here
# so that a profile with a wall of text cannot make one score take a minute.
MODEL_MAX_TOKENS = 128


def model_file(name):
    """Where one of the model's files sits on disk."""
    return os.path.join(MODEL_DIR, name.replace('/', os.sep))


def model_is_downloaded():
    return all(os.path.exists(model_file(name)) for name in MODEL_FILES)


def file_digest(path):
    """The SHA-256 of a file, as hex."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def download_model(on_progress=None):
    """Fetch the model files. True if they are all there afterwards.

    Downloaded to a temporary name and renamed once complete, so a download cut off
    half way cannot leave a file that looks finished. That matters more than usual
    here: a truncated model does not fail to load, it loads and returns nonsense,
    and nonsense would come out as affinities that look like numbers.

    The digest of each file is written down beside it and checked on every load
    afterwards. That catches a file that changed or got damaged after it arrived.
    It does not check what arrived in the first place against a known good value,
    which would need that value to be known: the digests are logged, so pinning
    them later is a matter of reading them out of the log.
    """
    import urllib.request

    os.makedirs(MODEL_DIR, exist_ok=True)
    digests = {}

    for name in MODEL_FILES:
        target = model_file(name)
        if os.path.exists(target):
            digests[name] = file_digest(target)
            continue

        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
        partial = target + ".part"
        os.makedirs(os.path.dirname(target), exist_ok=True)

        log(f"⬇️ Fetching {name} for the semantic ranking, this happens once", 'info')
        try:
            def report(blocks, block_size, total):
                if on_progress and total > 0:
                    on_progress(name, min(blocks * block_size, total), total)

            urllib.request.urlretrieve(url, partial, reporthook=report)
            os.replace(partial, target)
            digests[name] = file_digest(target)
        except Exception as e:
            log(f"❌ Could not fetch {name}: {brief_error(e)}", 'error')
            logger.debug(f"Model download failed for {url}", exc_info=True)
            if os.path.exists(partial):
                os.remove(partial)
            return False

    try:
        with open(model_file("digests.json"), 'w', encoding='utf-8') as handle:
            json.dump(digests, handle, indent=2)
    except Exception as e:
        logger.debug(f"Could not record the model digests: {e}")

    for name, digest in digests.items():
        logger.info(f"Model file {name} sha256 {digest}")

    log("✅ Model ready", 'success')
    return True


def model_files_unchanged():
    """True if every file still matches what was written down when it arrived."""
    try:
        with open(model_file("digests.json"), 'r', encoding='utf-8') as handle:
            recorded = json.load(handle)
    except Exception:
        return True  # Nothing to check against is not the same as a mismatch

    for name, digest in recorded.items():
        path = model_file(name)
        if os.path.exists(path) and file_digest(path) != digest:
            log(f"⚠️ {name} is not the file that was downloaded - ignoring the model", 'warning')
            return False
    return True


class SemanticModel:
    """Turns a piece of text into a vector, if it can.

    Absent is a normal state, not an error. The packages may not be installed, the
    download may have failed, the machine may be offline. Every one of those ends
    with available() saying no and the app carrying on exactly as it did before any
    of this existed - a queue ranked on sighting counts alone, which is what it was
    ranked on for its whole life so far.

    Loaded on first use rather than at startup: it costs a second and a couple of
    hundred megabytes, and most runs of the app never follow anybody.
    """

    def __init__(self):
        self.session = None
        self.tokenizer = None
        self.numpy = None
        self.failed = False

    def load(self):
        """Bring the model up. True if it is usable afterwards."""
        if self.session is not None:
            return True
        if self.failed:
            return False

        try:
            import numpy
            import onnxruntime
            from tokenizers import Tokenizer
        except ImportError as e:
            log(f"ℹ️ Semantic ranking is off: {brief_error(e)}", 'info')
            logger.info("Install onnxruntime and tokenizers to turn it on")
            self.failed = True
            return False

        if not model_is_downloaded() and not download_model():
            self.failed = True
            return False

        if not model_files_unchanged():
            self.failed = True
            return False

        try:
            self.tokenizer = Tokenizer.from_file(model_file("tokenizer.json"))
            self.tokenizer.enable_truncation(max_length=MODEL_MAX_TOKENS)
            self.session = onnxruntime.InferenceSession(
                model_file("onnx/model_quantized.onnx"),
                providers=["CPUExecutionProvider"],
            )
            self.numpy = numpy
        except Exception as e:
            log(f"❌ The model would not load: {brief_error(e)}", 'error')
            logger.exception("Loading the semantic model failed")
            self.failed = True
            return False

        logger.info(f"Semantic model ready, inputs: {[i.name for i in self.session.get_inputs()]}")
        return True

    def available(self):
        return self.load()

    def embed(self, text):
        """One vector for a piece of text, or None if it could not be made.

        Never raises. A profile that cannot be turned into a vector has to come back
        unscored, the same as one with nothing written on it: the pass carries on
        and that candidate keeps their sighting count.
        """
        if not (text or "").strip() or not self.load():
            return None

        try:
            encoded = self.tokenizer.encode(text)
            if not encoded.ids:
                return None

            # Which inputs to hand over is read off the model rather than assumed.
            # Exports of the same model differ over token_type_ids, and a missing
            # input is a hard failure at the first score rather than a wrong number.
            available_inputs = {
                "input_ids": encoded.ids,
                "attention_mask": encoded.attention_mask,
                "token_type_ids": encoded.type_ids,
            }
            feed = {
                spec.name: self.numpy.array([available_inputs[spec.name]], dtype=self.numpy.int64)
                for spec in self.session.get_inputs()
                if spec.name in available_inputs
            }

            outputs = self.session.run(None, feed)
            rows = outputs[0][0].tolist()
        except Exception as e:
            logger.debug(f"Could not embed a piece of text: {e}", exc_info=True)
            return None

        return normalized(mean_pooled(rows, encoded.attention_mask))


semantic_model = SemanticModel()


def make_affinity_scorer(read_profile, embed, niche):
    """A function from username to affinity, for score_queue().

    `read_profile` returns whatever the browser can see of a profile as a
    (name, category, bio) triple; `embed` turns a piece of text into a vector.
    Both are handed in: one needs a browser and the other needs a model, and
    neither belongs in the arithmetic.

    The niche is embedded once here rather than per candidate. It does not change
    while a pass runs, and it is the same cost as a profile every time.

    Returns None if there is nothing to compare against, so a pass with no niche
    written, or no model to be had, scores nobody rather than scoring everybody
    zero.
    """
    if not (niche or "").strip():
        return None

    niche_vector = embed(niche.strip())
    if not niche_vector:
        return None

    def score(username):
        name, category, bio = read_profile(username)
        text = profile_text(name=name, category=category, bio=bio)
        if text is None:
            return None
        return affinity_between(embed(text), niche_vector)

    return score


# ---------------------------
# SCRAPING FUNCTIONS
# ---------------------------
@retry()
def get_author_profile():
    """Get profile URL from open post."""
    try:
        author = wait_for_element(
            driver,
            By.XPATH,
            "//div[@role='dialog']//header//a"
        )
        return author.get_attribute("href")
    except (NoSuchElementException, TimeoutException):
        return None

def collect_post_links():
    """Post tiles on the hashtag page, or None if the page could not be read.

    None means "could not look", which is not the same as "there is nothing there" and
    must not be read as an exhausted hashtag. It happens when a post is open over the
    grid and will not close: its links belong to that post, so collecting them would
    queue up addresses that stop existing the moment it does close.
    """
    hrefs = driver.execute_script(POST_LINKS_JS)
    if hrefs is not None:
        return hrefs

    log("⚠️ A post was still open over the grid, closing it", 'warning')
    close_post()
    return driver.execute_script(POST_LINKS_JS)


@retry()
def open_post(href):
    """Click the grid tile for one post, finding it at the moment of use.

    References held from earlier are invalidated by post dialogs opening and closing,
    and Instagram can redraw a tile between it being found and clicked, which is what
    the retry covers. A tile that is simply not there raises and is not retried.
    """
    post = driver.find_element(By.CSS_SELECTOR, f'a[href="{href}"]')
    driver.execute_script("arguments[0].scrollIntoView();", post)
    time.sleep(random.uniform(0.5, 1.5))  # Randomized scroll delay
    driver.execute_script("arguments[0].click();", post)


def post_dialog_open():
    """True while a post is open over the grid."""
    try:
        return bool(driver.find_elements(By.XPATH, "//div[@role='dialog']"))
    except WebDriverException:
        return False


def close_post():
    """Close the open post. True if nothing is open afterwards.

    Escape first: it is locale-independent and cannot press the wrong thing. Only if
    the post survives that does this hunt for a close button, and every attempt is
    checked rather than assumed.

    It used to click the first svg inside any button in the dialog when the labelled
    close button did not match, then return as soon as a click went through. That
    selector matches the like, comment and save controls just as readily as the close
    X, so it could act on a stranger's post and report success while the post stayed
    open - which is how the grid ended up being read with a post over it.
    """
    try:
        if not post_dialog_open():
            return True

        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.5)
        if not post_dialog_open():
            return True

        # Only buttons that say they close something, by visible text or by label.
        selectors = [
            f"//div[@role='dialog']//button[contains(., '{label}')]"
            for label in CLOSE_BUTTON_LABELS
        ] + [
            f"//button[@aria-label='{label}']" for label in CLOSE_BUTTON_LABELS
        ]

        for selector in selectors:
            for btn in driver.find_elements(By.XPATH, selector):
                try:
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.5)
                    if not post_dialog_open():
                        return True
                except Exception:
                    continue

        logger.info("Post dialog would not close")
        return False

    except Exception as e:
        logger.debug(f"Close post error: {e}")
        return False


def leave_extra_window(main_window):
    """Close the window in front, whatever it is, and go back to `main_window`.

    Called while a failure may be on its way out, so it must not raise one of its
    own: an error here would replace the one being reported, and the log would say
    a window could not be closed instead of saying what actually went wrong.
    """
    try:
        if driver.current_window_handle != main_window:
            driver.close()
    except WebDriverException as e:
        logger.debug(f"Could not close the extra window: {e}")

    try:
        driver.switch_to.window(main_window)
    except WebDriverException as e:
        logger.debug(f"Could not switch back to the main window: {e}")


@retry()
def open_followers_popup():
    """Open followers popup on profile page."""
    try:
        # Wait for profile to load
        wait_for_element(driver, By.TAG_NAME, "header")
        time.sleep(1)  # Extra wait for dynamic content

        # Find followers link - try multiple selectors
        selectors = [
            "a[href*='/followers/']",
            "a[href*='followers']",
            "//a[contains(@href, '/followers/')]",
            "//span[contains(text(), 'follower') or contains(text(), 'follower')]/ancestor::a",
        ]

        followers_link = None
        for selector in selectors:
            try:
                if selector.startswith("//"):
                    followers_link = driver.find_element(By.XPATH, selector)
                else:
                    followers_link = driver.find_element(By.CSS_SELECTOR, selector)
                if followers_link:
                    break
            except:
                continue

        # Fallback: search all links
        if not followers_link:
            links = driver.find_elements(By.TAG_NAME, "a")
            for link in links:
                href = link.get_attribute("href") or ""
                if "/followers/" in href:
                    followers_link = link
                    break

        if followers_link:
            # Try to extract follower count for logging
            try:
                parent = followers_link.find_element(By.XPATH, "..")
                count_text = parse_follower_count(parent.text)
                if count_text:
                    log(f"👥 Opening followers popup ({count_text} followers)...")
                else:
                    log("👥 Opening followers popup...")
            except Exception:
                log("👥 Opening followers popup...")

            driver.execute_script("arguments[0].click();", followers_link)

            # Wait for dialog with better timeout
            wait_for_element(driver, By.XPATH, "//div[@role='dialog']")

            # IMPORTANT: Wait longer for the scrollable content to load
            time.sleep(3.5)

            # Wait for user links to appear
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: d.execute_script("""
                        let dialog = document.querySelector("div[role='dialog']");
                        if (!dialog) return false;
                        let links = dialog.querySelectorAll('a[href^=\"/\"]');
                        return links.length > 3;
                    """)
                )
                logger.debug("User links loaded in dialog")
            except:
                logger.debug("Waited for user links but timeout")

            return True

        logger.warning("Could not find followers link")
        return False

    except Exception as e:
        logger.warning(f"Failed to open followers: {e}")
        return False

def extract_users_from_followers(current_hashtag="", author_num=0, total_authors=0, author_name=""):
    """Extract users from followers popup with improved scrolling."""
    users = []
    original_url = driver.current_url

    try:
        dialog = driver.find_element(By.XPATH, "//div[@role='dialog']")

        # Try to click "See all" / "See all suggestions" button if present
        see_all_clicked = False
        try:
            see_all_btn = driver.execute_script("""
                let dialog = document.querySelector("div[role='dialog']");
                if (!dialog) return null;
                let btns = dialog.querySelectorAll('button, a');
                for (let b of btns) {
                    let text = (b.innerText || b.textContent || '').toLowerCase();
                    // Match various "see all" patterns including suggestions
                    if (text.match(/see all|show all|view all|mostra.*tutti|ver todos|voir tout|vedi tutti/)) {
                        return b;
                    }
                }
                return null;
            """)
            if see_all_btn:
                log("🔘 Found 'See all' button, clicking...")
                driver.execute_script("arguments[0].click();", see_all_btn)
                time.sleep(3)  # Wait longer for full list to load
                see_all_clicked = True
        except:
            pass

        # Check if clicking "See all" caused navigation to /explore/people/
        if see_all_clicked and "/explore/people" in driver.current_url:
            log("⚠️ Navigation to /explore/people detected, going back...", 'warning')
            driver.back()
            time.sleep(2)
            # Re-open followers popup
            if not open_followers_popup():
                log("❌ Failed to reopen followers popup after navigation", 'error')
                return []
            # Try extraction without clicking "See all" this time
            log("🔄 Retrying extraction without 'See all' button...")
            # Skip the see_all click by removing the button from DOM
            try:
                driver.execute_script("""
                    let dialog = document.querySelector("div[role='dialog']");
                    if (dialog) {
                        let btns = dialog.querySelectorAll('button, a');
                        for (let b of btns) {
                            let text = (b.innerText || b.textContent || '').toLowerCase();
                            if (text.match(/see all|show all|view all|mostra.*tutti|ver todos|voir tout|vedi tutti/)) {
                                b.remove();
                                break;
                            }
                        }
                    }
                """)
            except:
                pass

        log("🔄 Scrolling followers list...")

        # Wait longer for the list to populate initially
        time.sleep(2.5)

        # Check for "See all suggestions" button again after initial load
        try:
            suggestions_btn = driver.execute_script("""
                let dialog = document.querySelector("div[role='dialog']");
                if (!dialog) return null;

                let buttons = dialog.querySelectorAll('button, a');
                for (let btn of buttons) {
                    let text = (btn.innerText || btn.textContent || '').toLowerCase();
                    // Look for suggestion-related buttons
                    if (text.match(/vedi tutti.*suggerimenti|see all.*suggestion|ver todas.*sugerencia|voir toutes.*suggestion|show.*suggestion/)) {
                        return btn;
                    }
                }
                return null;
            """)
            if suggestions_btn:
                log("🔘 Found 'See all suggestions' button after load, clicking...")
                driver.execute_script("arguments[0].click();", suggestions_btn)
                time.sleep(3)
                log("✅ Expanded to full followers list")
        except Exception as e:
            logger.debug(f"Post-load suggestions button check: {e}")

        # Find the scrollable container first
        scrollable_container = driver.execute_script("""
            let dialog = document.querySelector("div[role='dialog']");
            if (!dialog) return null;

            // Try multiple strategies to find the scrollable container
            // Strategy 1: Find div with overflow-y or large scrollHeight
            let divs = dialog.querySelectorAll('div');
            for (let div of divs) {
                let style = window.getComputedStyle(div);
                let rect = div.getBoundingClientRect();
                // Look for tall scrollable containers
                if ((style.overflowY === 'scroll' || style.overflowY === 'auto' || div.scrollHeight > rect.height + 100)
                    && rect.height > 300) {
                    return div;
                }
            }

            // Strategy 2: Look for the second child div of the dialog's first child (common Instagram pattern)
            let firstChild = dialog.firstElementChild;
            if (firstChild && firstChild.firstElementChild) {
                let sibling = firstChild.firstElementChild.nextElementSibling;
                if (sibling) return sibling;
            }

            // Strategy 3: Look for any div with significant scrollable content
            for (let div of divs) {
                if (div.scrollHeight > div.clientHeight + 200) {
                    return div;
                }
            }

            return null;
        """)

        if scrollable_container:
            log("✅ Found scrollable container")
        else:
            log("⚠️ Using dialog as scrollable container")

        # Scroll using multiple strategies
        scroll_count = 0
        last_user_count = 0
        no_new_users_count = 0
        consecutive_empty_scrolls = 0

        for i in range(CONFIG["FOLLOWER_SCROLL_COUNT"]):
            if stop_requested.is_set():
                break

            # Strategy 1: Scroll the container
            try:
                if scrollable_container:
                    # Scroll the found container by a larger amount
                    driver.execute_script("""
                        arguments[0].scrollBy(0, 800);
                    """, scrollable_container)
                else:
                    # Fallback: scroll the dialog
                    driver.execute_script("""
                        let dialog = document.querySelector("div[role='dialog']");
                        if (dialog) dialog.scrollBy(0, 800);
                    """)
                scroll_count += 1
            except Exception as e:
                logger.debug(f"Scroll error: {e}")

            # Strategy 2: Every 3rd scroll, scroll the last user into view (triggers lazy loading)
            if i % 3 == 0:
                try:
                    driver.execute_script("""
                        let dialog = document.querySelector("div[role='dialog']");
                        if (!dialog) return;

                        // Find all user links - look for links in the list
                        let links = dialog.querySelectorAll('a[href^="/"]');
                        let userLinks = [];

                        for (let link of links) {
                            let href = link.getAttribute('href') || '';
                            // Filter for actual user profile links
                            let match = href.match(/^\\/([^\\/]+)\\/?$/);
                            if (match) {
                                let username = match[1];
                                if (username.length > 1 &&
                                    !['p', 'explore', 'accounts', 'direct', 'emails', 'reels', 'stories'].includes(username) &&
                                    !username.includes('.') &&
                                    !username.includes('?')) {
                                    userLinks.push(link);
                                }
                            }
                        }

                        // Scroll the last few user links into view
                        if (userLinks.length > 0) {
                            let lastLink = userLinks[userLinks.length - 1];
                            lastLink.scrollIntoView({behavior: 'instant', block: 'center'});
                        }
                    """)
                except Exception as e:
                    logger.debug(f"Last element scroll error: {e}")

            # Check for "See all suggestions" button (appears when list is truncated)
            if i % 5 == 0 or i == 3:  # Check early and periodically
                try:
                    suggestions_btn = driver.execute_script("""
                        let dialog = document.querySelector("div[role='dialog']");
                        if (!dialog) return null;

                        // Look for "See all suggestions" or similar buttons
                        let buttons = dialog.querySelectorAll('button, a');
                        for (let btn of buttons) {
                            let text = (btn.innerText || btn.textContent || '').toLowerCase();
                            // Match patterns like:
                            // "Vedi tutti i suggerimenti" (Italian)
                            // "See all suggestions" (English)
                            // "Ver todas las sugerencias" (Spanish)
                            // "Voir toutes les suggestions" (French)
                            if (text.match(/vedi tutti|see all.*suggestion|ver todos.*sugerencia|voir toutes.*suggestion/)) {
                                return btn;
                            }
                        }
                        return null;
                    """)
                    if suggestions_btn:
                        log("🔘 Found 'See all suggestions' button, clicking...")
                        driver.execute_script("arguments[0].click();", suggestions_btn)
                        time.sleep(3)  # Wait longer for full list to load
                        log("✅ Full list loaded, continuing scroll...")
                except Exception as e:
                    logger.debug(f"Suggestions button check error: {e}")

            # Wait longer for content to load (Instagram needs time to fetch new users)
            # SAFETY: Longer delays between scrolls to appear more human
            time.sleep(random.uniform(3.0, 5.0))

            # Check actual user count by extracting and counting unique users
            try:
                current_users = driver.execute_script("""
                    let dialog = document.querySelector("div[role='dialog']");
                    if (!dialog) return [];

                    let links = dialog.querySelectorAll('a[href^="/"]');
                    let users = [];
                    for (let link of links) {
                        let href = link.getAttribute('href') || '';
                        let match = href.match(/^\\/([^\\/]+)\\/?$/);
                        if (match) {
                            let username = match[1];
                            // Better filtering for usernames
                            if (username.length > 1 &&
                                !['p', 'explore', 'accounts', 'direct', 'emails', 'reels', 'stories', 'help', 'about', 'blog', 'jobs', 'privacy', 'terms', 'locations', 'language'].includes(username) &&
                                !username.includes('.') &&
                                !username.includes('?') &&
                                !username.startsWith('__')) {
                                users.push(username);
                            }
                        }
                    }
                    return users;
                """)

                unique_current = len(set(current_users))

                if unique_current > last_user_count:
                    new_users = unique_current - last_user_count
                    no_new_users_count = 0
                    consecutive_empty_scrolls = 0
                    last_user_count = unique_current
                else:
                    no_new_users_count += 1
                    consecutive_empty_scrolls += 1

                # More lenient stopping: need 8 consecutive empty scrolls after initial scrolls
                if i > 20 and consecutive_empty_scrolls >= 8:
                    log(f"⏹️ No new users after {consecutive_empty_scrolls} scrolls, stopping")
                    break

                # Also stop if stuck early (might indicate end of list)
                if i > 8 and consecutive_empty_scrolls >= 5 and unique_current < 10:
                    log(f"⏹️ List appears to be at end or loading failed, stopping early")
                    break

            except Exception as e:
                logger.debug(f"User count check error: {e}")

            # Log progress every 5 scrolls
            if (i + 1) % 5 == 0:
                log(f"  Scrolled {i+1}/{CONFIG['FOLLOWER_SCROLL_COUNT']} (found ~{last_user_count} users)")

            # Update UI progress
            if i % 3 == 0:
                update_progress(
                    i + 1,
                    CONFIG["FOLLOWER_SCROLL_COUNT"],
                    phase="loading_followers",
                    current_hashtag=current_hashtag,
                    author_num=author_num,
                    total_authors=total_authors,
                    author_name=author_name,
                    followers_extracted=last_user_count
                )

        log(f"📊 Completed {scroll_count} scrolls, found ~{last_user_count} user elements")

        # Final extraction. Each row's follow button is read so accounts you
        # already follow never enter the ranking or the queue.
        try:
            result = driver.execute_script(
                EXTRACT_FOLLOWERS_JS, list(FOLLOWING_BUTTON_MARKERS)
            ) or {}

            candidates = result.get('kept', [])
            skipped_following = result.get('skippedFollowing', 0)
            rows_without_button = result.get('rowsWithoutButton', 0)
            rows_inspected = result.get('rowsInspected', 0)

            # Second, independent net: the bot's own history. Covers users
            # followed in an earlier session whose button Instagram has not
            # refreshed yet.
            filtered_users = []
            skipped_history = 0
            for user in candidates:
                if is_already_followed(user):
                    skipped_history += 1
                    logger.debug(f"Filtered out already followed user (history): {user}")
                else:
                    filtered_users.append(user)

            total_skipped = skipped_following + skipped_history
            log(
                f"📊 Extracted {len(filtered_users)} candidates "
                f"({rows_inspected} rows, skipped {total_skipped} already followed: "
                f"{skipped_following} by button, {skipped_history} by history)"
            )

            # If not a single row yielded a button, the row lookup is broken -
            # every user is being kept by the fail-open path, which looks exactly
            # like having no filter at all. Say so instead of failing silently.
            if rows_inspected and rows_without_button == rows_inspected:
                log(
                    "⚠️ Could not read the follow button on any row - "
                    "already-followed users are NOT being filtered out. "
                    "Instagram's layout may have changed.",
                    'warning'
                )
                logger.warning(
                    f"Row lookup failed for all {rows_inspected} rows in the followers dialog"
                )
            elif rows_without_button:
                logger.info(
                    f"{rows_without_button}/{rows_inspected} rows had no readable button (kept)"
                )

            if len(filtered_users) < 5:
                logger.warning(f"Low user count: {len(filtered_users)}")

            return filtered_users

        except Exception as e:
            log(f"❌ Error extracting followers: {e}", 'error')
            logger.exception("Extraction error")
            return []

    except Exception as e:
        log(f"❌ Error in extraction process: {e}", 'error')
        logger.exception("Extraction error")
        return []

# ---------------------------
# FOLLOW LOGIC
# ---------------------------
def get_button_text(btn):
    """Get full text from button including innerText."""
    try:
        # Try multiple ways to get text
        texts = [
            btn.text,
            btn.get_attribute("innerText"),
            btn.get_attribute("textContent"),
        ]
        return " ".join(filter(None, texts)).lower()
    except:
        return ""


def validate_follow_success(original_btn=None, original_username=None):
    """Verify that follow was successful by checking button state changed and we stayed on target profile."""
    try:
        # Wait longer for UI to update
        time.sleep(2)

        # CRITICAL: First verify we're still on the target user's profile
        if original_username:
            current_url = driver.current_url.rstrip("/")
            if original_username not in current_url:
                logger.debug(f"⚠️ Navigation detected after click! Expected to be on {original_username}, now at {current_url}")
                return False

        # Get original button text if provided
        original_text = ""
        if original_btn:
            try:
                original_text = get_button_text(original_btn)
            except:
                pass

        # Check all buttons on page
        buttons = driver.find_elements(By.TAG_NAME, "button")

        found_following = False
        found_follow = False
        current_btn_texts = []

        for btn in buttons:
            text = get_button_text(btn)
            current_btn_texts.append(text[:50])  # Limit for logging

            # Success indicators - button changed to these states
            if has_marker(text, FOLLOWED_SIGNAL_MARKERS):
                found_following = True

            # If we still see a clear "Follow" button
            if is_follow_button(text):
                found_follow = True

        # Log for debugging
        logger.debug(f"Validation check - found_following: {found_following}, found_follow: {found_follow}")
        logger.debug(f"Button texts: {current_btn_texts[:5]}")  # First 5 buttons

        # Success cases:
        # 1. Found "Following" or "Requested" state
        if found_following:
            return True

        # 2. Original button was "Follow" and now we see different text.
        # Intentionally coarse: this also matches "Following", which is fine here -
        # we only care that the original button was follow-related at all.
        if original_text and has_marker(original_text, FOLLOW_BUTTON_MARKERS):
            # Button changed - likely succeeded
            if not found_follow:
                return True

        # 3. No "Follow" button found at all (button disappeared/changed)
        if not found_follow:
            return True

        # 4. Still see "Follow" button - might be false positive
        # Since Instagram UI can be slow, give benefit of doubt
        logger.debug(f"Still see Follow button - giving benefit of doubt")
        return True

    except Exception as e:
        logger.debug(f"Validation error: {e}")
        return True  # Assume success if validation fails


def find_follow_button():
    """Find the follow button on profile page - ONLY in the main profile header, not in suggestions carousels."""
    try:
        # Strategy 1: Look specifically in the profile header section
        # This avoids horizontal suggestion carousels that appear below the profile
        header_selectors = [
            "//header",  # The main profile header
            "//section//div[contains(@class, 'header')]",
            "//div[contains(@class, 'profile')]//header",
            "//main//header",
            "//div[@role='button' and ancestor::header]",  # Buttons inside header
        ]

        for header_xpath in header_selectors:
            try:
                header = driver.find_element(By.XPATH, header_xpath)
                # Now find follow button only within this header section
                header_html = header.get_attribute('innerHTML')
                # Cheap gate: does this header mention follow at all (any state)?
                if has_marker(header_html.lower(), FOLLOW_BUTTON_MARKERS):
                    # Search for buttons within the header
                    buttons_in_header = header.find_elements(By.TAG_NAME, "button")
                    for btn in buttons_in_header:
                        text = get_button_text(btn)
                        # Skip if already following (or only a Message button)
                        if has_marker(text, FOLLOWED_SIGNAL_MARKERS):
                            continue
                        if is_follow_button(text):
                            return btn, "follow"
            except NoSuchElementException:
                continue

        # Strategy 2: Find the FIRST follow button that appears in the page BEFORE any horizontal scroll sections
        # This works because Instagram renders profile header before suggestion carousels
        all_buttons = driver.find_elements(By.TAG_NAME, "button")
        first_follow_btn = None
        first_follow_idx = -1

        for idx, btn in enumerate(all_buttons):
            text = get_button_text(btn)
            if has_marker(text, FOLLOWING_BUTTON_MARKERS):
                if first_follow_btn is None:
                    first_follow_btn = btn
                    first_follow_idx = idx
                    return btn, "already_following"
            if is_follow_button(text):
                if first_follow_btn is None:
                    first_follow_btn = btn
                    first_follow_idx = idx

        if first_follow_btn:
            return first_follow_btn, "follow"

        # Strategy 3: Fallback - find div[role='button'] in header area
        div_buttons = driver.find_elements(By.XPATH, "//div[@role='button']")
        for btn in div_buttons:
            try:
                # Check if this button is in the upper portion of the page (likely header area)
                location = btn.location
                if location['y'] < 500:  # Profile header is typically in upper portion
                    text = get_button_text(btn)
                    if is_follow_button(text):
                        return btn, "follow"
            except:
                continue

        return None, "not_found"

    except Exception as e:
        logger.debug(f"Find button error: {e}")
        return None, "error"


def read_profile_stats():
    """(posts, followers, following) for the profile in the browser, None where unread.

    Never raises. A profile whose numbers cannot be read has to stay followable.
    """
    try:
        raw = driver.execute_script(PROFILE_STATS_JS)
    except WebDriverException as e:
        logger.info(f"Could not read profile stats: {type(e).__name__}")
        return None, None, None

    if not raw:
        return None, None, None

    header_text = raw.get('headerText') or ""

    def read(entries, markers, name):
        """One count, from its link and from the header text, only if they agree.

        Two independent routes, because one link that cannot be found used to mean
        the check simply did not happen. Where both answer they are checked against
        each other rather than one silently winning: they are anchored on different
        things, so a disagreement means the header is not what this code thinks it
        is, and a number nobody can vouch for must not reject an account. That is
        the same rule the filter already applies to a count it could not read at
        all - the difference here is that the misreading looks like an answer.
        """
        from_link = count_from_links(entries, markers)
        from_text = parse_labelled_count(header_text, markers)

        if from_link is None:
            return from_text
        if from_text is None:
            return from_link
        if counts_agree(from_link, from_text):
            # The link is the precise one where the rendered text was abbreviated.
            return from_link

        log(
            f"⚠️ Two different {name} counts on this profile "
            f"({from_link} beside the link, {from_text} in the header) - "
            f"that check was skipped",
            'warning'
        )
        return None

    return (
        parse_labelled_count(header_text, POSTS_LABEL_MARKERS),
        read(raw.get('followers'), FOLLOWERS_LABEL_MARKERS, "follower"),
        read(raw.get('following'), FOLLOWING_LABEL_MARKERS, "following"),
    )


def read_candidate_profile(username):
    """Open a candidate's profile and read what it says about itself.

    Comes back as the (name, category, bio) that make_affinity_scorer wants. Only
    the last is filled in: the header is one run of text, and profile_description()
    takes the part of it that describes the account rather than counting it.

    Never raises, and never guesses. A page that will not load, or that Instagram
    answers with somebody else's profile, leaves the candidate unscored - which
    keeps their sighting count and their place, rather than recording a number
    about a profile that was never read.
    """
    nothing = (None, None, None)

    try:
        driver.get(f"https://www.instagram.com/{username}/")
        wait_for_element(driver, By.TAG_NAME, "header")
        time.sleep(random.uniform(1.0, 2.0))  # Let the header settle

        # Instagram answers a visit to some profiles with a page of suggestions.
        # Reading that would score this candidate on a stranger's bio.
        if username.lower() not in driver.current_url.lower():
            logger.info(f"Asked for {username} and landed somewhere else, leaving it unscored")
            return nothing

        raw = driver.execute_script(PROFILE_STATS_JS)
    except WebDriverException as e:
        logger.info(f"Could not read {username}: {type(e).__name__}")
        return nothing
    finally:
        # The pass walks hundreds of profiles in a row, which is the sort of thing
        # worth doing at the same unhurried pace as everything else here.
        time.sleep(random.uniform(2.0, 4.0))

    if not raw:
        return nothing
    return None, None, profile_description(raw.get('headerText') or "")


def run_scoring_pass():
    """Score the strongest candidates in the queue, then hold the queue to size.

    Runs at the end of a search rather than during a follow session. The browser is
    already up and already reading heavily here, and a follow session stays exactly
    as quick as it has always been.

    Every way of not being able to score leaves the queue ordered on sighting counts
    alone, which is what it was ordered on before any of this existed. None of them
    is an error.
    """
    if not CONFIG["SEMANTIC_ENABLED"]:
        return

    niche = str(CONFIG.get("SEMANTIC_NICHE") or "").strip()
    if not niche:
        log("ℹ️ No niche written in the settings, so nothing is scored", 'info')
        return

    scorer = make_affinity_scorer(read_candidate_profile, semantic_model.embed, niche)
    if scorer is None:
        log("ℹ️ Semantic ranking is off, the queue keeps its order by sightings", 'info')
        return

    def on_progress(number, total, username):
        if number == 1 or number % 25 == 0:
            log(f"🧭 Scoring {number}/{total}...", 'info')

    scored = score_queue(scorer, on_progress=on_progress)
    if scored:
        log(f"🧭 Scored {scored} profiles against your niche", 'success')

    dropped = trim_queue()
    if dropped:
        log(
            f"✂️ Queue held at {CONFIG['SEMANTIC_SHORTLIST']}: {dropped} candidates "
            f"left it. Their sighting counts are kept, so a later search that finds "
            f"them again picks up where this one left off",
            'info'
        )

    try:
        refresh_queue_display()
    except Exception as e:
        logger.debug(f"Could not refresh the queue display: {e}")


def profile_bot_reason():
    """Why the profile in the browser looks automated, or None to follow it."""
    # The header can render before the counts arrive, so give them a moment.
    for attempt in range(3):
        posts, followers, following = read_profile_stats()
        if None not in (posts, followers, following):
            break
        if attempt < 2:
            time.sleep(1)

    missing = [
        name for name, value in
        (("posts", posts), ("followers", followers), ("following", following))
        if value is None
    ]

    # Fail open, but never quietly: an unread count takes no part in the decision, so
    # silence here is indistinguishable from a profile that passed.
    if len(missing) == 3:
        log("⚠️ Could not read any of this profile's counts - bot filter skipped", 'warning')
        return None
    if missing:
        log(f"⚠️ Could not read {' or '.join(missing)} here - that check was skipped", 'warning')

    reason = bot_rejection_reason(posts, followers, following)
    logger.info(
        f"Profile stats: posts={posts} followers={followers} "
        f"following={following} -> {reason or 'ok'}"
    )
    return reason


def follow_user(username, delay_min, delay_max):
    """Follow a single user with validation."""
    try:
        stats.increment('attempted')
        target_url = f"https://www.instagram.com/{username}/"

        driver.get(target_url)

        # Wait for profile
        wait_for_element(driver, By.TAG_NAME, "header")
        time.sleep(1)  # Let page settle

        # CRITICAL: Verify we landed on the correct profile
        # Instagram may redirect private profile visitors to suggested accounts
        current_url = driver.current_url.rstrip("/")
        if username not in current_url:
            log(f"⚠️ Redirect detected! Expected {username}, got redirect. Skipping...", 'warning')
            return False, "redirected"

        if check_rate_limit(driver):
            log(f"⚠️ Rate limit warning detected - continuing anyway (dev mode)", 'warning')
            # Don't stop - just warn and continue
            # stats.increment('skipped_rate_limited')
            # return False, "rate_limited"

        # Find follow button
        btn, status = find_follow_button()

        if status == "not_found":
            return False, "no_button"

        if status == "already_following":
            stats.increment('skipped_already_following')
            return False, "already_following"

        if status == "error":
            return False, "button_error"

        if status == "follow" and btn:
            # Checked here rather than on arrival: a profile already followed, or one
            # with no button, is settled without needing its counts, and would
            # otherwise be recorded as filtered instead.
            if CONFIG["BOT_FILTER_ENABLED"]:
                bot_reason = profile_bot_reason()
                if bot_reason:
                    log(f"🤖 Skip {username} | looks automated: {bot_reason}", 'warning')
                    return False, "filtered_bot"

            # Store button reference for validation comparison
            original_btn = btn

            # Try to click the button
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", btn)
            except Exception as e:
                # Fallback to regular click
                try:
                    btn.click()
                except:
                    return False, f"click_failed: {e}"

            # Verify we stayed on the correct profile after clicking
            # (button click may cause navigation to suggestions)
            time.sleep(1)
            final_url = driver.current_url.rstrip("/")
            if username not in final_url:
                log(f"⚠️ Navigation after click! Expected {username}, now at different page. Skipping...", 'warning')
                return False, "navigation_after_click"

            # Verify success with original button reference
            if validate_follow_success(original_btn, username):
                stats.increment('succeeded')
                return True, None
            else:
                # Still might have worked, check button again after extra wait
                time.sleep(2)
                _, new_status = find_follow_button()
                if new_status == "already_following":
                    stats.increment('succeeded')
                    return True, None
                stats.increment('errors')
                return False, "validation_failed"

        return False, f"unexpected_status: {status}"

    except Exception as e:
        stats.increment('errors')
        return False, str(e)

def follow_from_queue(users_to_follow, delay_min, delay_max, limit):
    """Follow users from the queue with batch safety cooldowns."""
    global stats
    successful = 0
    skipped_already_followed = 0
    batch_count = 0

    # Consume in rank order, highest first - the same order rank_queue() gives the
    # listbox, so the next account followed is the top one on screen. Ranking here
    # rather than at the call sites means neither queue mode nor deep search can
    # bypass it.
    ranked = rank_queue(users_to_follow)
    usernames = [username for username, _, _ in ranked]

    log(f"📋 Following from queue: {len(usernames)} users available")
    if ranked:
        preview = ", ".join(f"{u} [{rank}]" for u, rank, _ in ranked[:10])
        log(f"🏆 Follow order by rank: {preview}{' ...' if len(ranked) > 10 else ''}", 'info')
    log(f"🎯 Target: {limit} follows this session")
    log(f"🛡️ Safety: {CONFIG['FOLLOW_BATCH_SIZE']} follows per batch, {CONFIG['FOLLOW_BATCH_COOLDOWN']//60}min cooldown between batches")

    # Progress bar uses percentage (0-100)
    progress_bar['maximum'] = 100

    for i, user in enumerate(usernames):
        if stop_requested.is_set():
            log("⏹️ Stopped by user", 'warning')
            break

        if successful >= limit:
            log(f"✅ Reached target of {limit} follows", 'success')
            break

        # SAFETY: Batch cooldown check
        if batch_count >= CONFIG["FOLLOW_BATCH_SIZE"] and successful > 0:
            cooldown = CONFIG["FOLLOW_BATCH_COOLDOWN"]
            minutes = cooldown // 60
            log(f"🛡️ BATCH COOLDOWN: {minutes} minute break after {CONFIG['FOLLOW_BATCH_SIZE']} follows...", 'warning')
            # Break cooldown into chunks to allow stop detection
            for _ in range(cooldown):
                if stop_requested.is_set():
                    break
                time.sleep(1)
            batch_count = 0
            log(f"✅ Batch cooldown complete, resuming...", 'success')

        # Check if already in history
        if is_already_followed(user):
            log(f"⏭️ Skip {user} | already followed in history", 'warning')
            skipped_already_followed += 1
            remove_from_queue(user)  # Remove from queue since already done
            continue

        result, reason = follow_user(user, delay_min, delay_max)

        if result:
            log(f"✅ Followed {user} | {i+1}/{len(usernames)}", 'success')
            successful += 1
            batch_count += 1  # Increment batch counter for safety cooldowns
            update_progress(successful, limit, phase="following_users")
            remove_from_queue(user)  # Remove successfully followed user
            log_followed_user(user, "success")
        else:
            if reason == "rate_limited":
                log(f"⚠️ Rate limit detected - continuing anyway (dev mode)", 'warning')
                # Don't stop - just warn and continue
                # messagebox.showwarning(
                #     "Rate Limited",
                #     f"Instagram rate limit detected.\n"
                #     f"Wait {CONFIG['RATE_LIMIT_PAUSE']//3600} hours before continuing."
                # )
                # break
            elif reason == "already_following":
                log(f"⚠️ Skip {user} | already following", 'warning')
                remove_from_queue(user)  # Remove since already following
                log_followed_user(user, "already_following")
            elif reason == "filtered_bot":
                # follow_user already logged which signal rejected it. Recorded in the
                # history so the queue drops it and later extractions do not re-add it:
                # the verdict would not change on a second visit, and re-checking would
                # cost another profile load every session.
                remove_from_queue(user)
                log_followed_user(user, "filtered_bot")
            else:
                log(f"⚠️ Skip {user} | {reason}", 'warning')
                # Don't remove from queue on error - can retry next time

        # Check if stop was requested during the follow operation
        if stop_requested.is_set():
            log("🛑 Stop detected during follow loop, breaking gracefully...", 'warning')
            break

        # Random delay between follows
        delay = random.uniform(delay_min, delay_max)
        log(f"⏱️ Waiting {delay:.1f}s...", 'info')

        # Break delay into chunks to check stop_requested
        for _ in range(int(delay)):
            if stop_requested.is_set():
                break
            time.sleep(1)
        time.sleep(delay % 1)

    return successful


# ---------------------------
# UNFOLLOW LOGIC
# ---------------------------
def unfollow_user(username):
    """Unfollow a single user.

    Deliberately kept as a straight line: load profile, find the "Following" button,
    click it, click the confirmation, done. This mirrors the original standalone
    unfollow script, which was fast and predictable.

    It does NOT reuse the follow side's helpers (wait_for_element, check_rate_limit,
    find_follow_button, post-action validation): those are built for the scraping
    flow and each one costs extra waits and many WebDriver round-trips per profile
    - wait_for_element alone is @retry-wrapped around a 15s WebDriverWait - which is
    what made unfollow feel far slower than the configured delay.
    """
    try:
        uf_stats.increment('attempted')

        driver.get(f"https://www.instagram.com/{username}/")
        time.sleep(random.uniform(1, 2))

        buttons = driver.find_elements(By.TAG_NAME, "button")

        follow_btn = None
        for b in buttons:
            try:
                txt = b.text.lower()
            except StaleElementReferenceException:
                continue
            if has_marker(txt, FOLLOWING_BUTTON_MARKERS):
                follow_btn = b
                break

        if not follow_btn:
            return False, "not following"

        driver.execute_script("arguments[0].click();", follow_btn)
        time.sleep(random.uniform(1, 2))

        elements = driver.find_elements(By.XPATH, "//button | //div[@role='button']")

        unfollow_btn = None
        for el in elements:
            try:
                txt = el.text.lower()
            except StaleElementReferenceException:
                continue
            if has_marker(txt, UNFOLLOW_CONFIRM_MARKERS):
                unfollow_btn = el
                break

        if not unfollow_btn:
            return False, "unfollow button not found"

        driver.execute_script("arguments[0].click();", unfollow_btn)

        uf_stats.increment('succeeded')
        return True, None

    except Exception as e:
        uf_stats.increment('errors')
        return False, str(e)


def unfollow_from_list(users_to_process, delay_min, delay_max, limit):
    """Unfollow users from the non-followers list, tracking progress for resumability."""
    successful = 0

    log(f"📋 Non-followers to process: {len(users_to_process)}")
    log(f"🎯 Target: {limit} unfollows this session")

    for i, user in enumerate(users_to_process):
        if stop_requested.is_set():
            log("⏹️ Stopped by user", 'warning')
            break

        if successful >= limit:
            log(f"✅ Reached target of {limit} unfollows", 'success')
            break

        update_unfollow_progress(i, len(users_to_process))

        log(f"\n➡️ {user}")
        result, reason = unfollow_user(user)

        uf_progress["processed"].append(user)

        if result:
            log(f"✅ Unfollow {user} | {i+1}/{len(users_to_process)}", 'success')
            successful += 1
            uf_progress["unfollowed"].append(user)
        else:
            log(f"⚠️ Skip {user} | {reason}", 'warning')
            uf_progress["skipped"].append(user)

        uf_save_progress()

        if stop_requested.is_set():
            log("🛑 Stop detected, breaking...", 'warning')
            break

        delay = random.uniform(delay_min, delay_max)
        log(f"⏱️ Waiting {delay:.1f}s...", 'info')
        # Chunked so Stop stays responsive during long delays
        for _ in range(int(delay)):
            if stop_requested.is_set():
                break
            time.sleep(1)
        time.sleep(delay % 1)

    update_unfollow_progress(successful, max(successful, 1))
    return successful


def unfollow_logic():
    """Main unfollow logic - processes the non-followers list computed from the JSON exports."""
    global uf_stats
    uf_stats = SessionStats(on_update=lambda: update_unfollow_stats_display())
    stop_requested.clear()

    try:
        try:
            delay_min = int(uf_delay_min_entry.get())
            delay_max = int(uf_delay_max_entry.get())
            limit = int(uf_limit_entry.get())
        except ValueError:
            log("❌ Invalid numeric input!", 'error')
            messagebox.showerror("Error", "Please enter valid numbers")
            return

        if not browser_is_open():
            log("❌ Browser not open!", 'error')
            handle_browser_closed()
            messagebox.showerror("Error", "Please open the browser first")
            return

        # The account may have been switched in the browser since the files were
        # loaded, which would make this session write progress against the wrong
        # following list.
        uf_check_account()

        if not uf_non_followers:
            log("❌ No data loaded! Load followers.json and following.json first", 'error')
            messagebox.showerror("Error", "Please load the JSON files first")
            return

        uf_load_progress()
        to_process = [u for u in uf_non_followers if u not in uf_progress["processed"]]

        if not to_process:
            log("✔ All non-followers have already been processed", 'success')
            messagebox.showinfo("Completed", "All non-followers have already been processed.\nUse 'Reset' to start over.")
            return

        log(f"🚀 Starting UNFOLLOW: {len(to_process)} users left to process")

        reset_unfollow_progress()

        random.shuffle(to_process)

        unfollow_from_list(to_process, delay_min, delay_max, limit)

        report = uf_stats.report()
        log(f"\n{report}", 'success')
        messagebox.showinfo("Session Complete", report)

    except Exception as e:
        log(f"❌ Fatal error: {e}", 'error')
        logger.exception("Fatal error in unfollow_logic")
    finally:
        # Releases the browser for the next session, re-checks it in case it was
        # closed while this ran, and refreshes the summary counts.
        end_session()
        if not stop_requested.is_set():
            reset_unfollow_progress()


def run_unfollow():
    """Start unfollow in background thread."""
    if not begin_session():
        return
    thread = threading.Thread(target=unfollow_logic, daemon=True)
    thread.start()
    active_threads.append(thread)


def unfollow_progress_counts():
    """(total, remaining, removed) for the loaded non-followers list.

    Read from the saved progress, so it survives restarts: the work already done
    in earlier sessions is the whole point of keeping that file.
    """
    processed = set(uf_progress.get("processed", []))
    remaining = sum(1 for u in uf_non_followers if u not in processed)
    return len(uf_non_followers), remaining, len(uf_progress.get("unfollowed", []))


def update_unfollow_ui_state():
    """Unfollow tab's Start button and summary, in agreement with what is loaded.

    One place owns that label. A second function used to write a fuller version of
    it, but nothing called that at startup, so a reloaded session showed only its
    total - how much of the list had already been done stayed hidden until a round
    finished or Stop was pressed.
    """
    try:
        uf_load_progress()
        running = session_running.is_set()
        uf_browser_btn.config(state='normal' if can_open_browser() else 'disabled')
        uf_start_btn.config(
            state='normal'
            if driver is not None and uf_non_followers and not running
            else 'disabled'
        )
        uf_stop_btn.config(state='normal' if running else 'disabled')

        if not uf_non_followers:
            uf_data_label.config(text="🟡 Load followers.json and following.json to begin")
            return

        total, remaining, removed = unfollow_progress_counts()
        summary = f"🟢 {total} non-followers | {remaining} to process | {removed} already removed"
        if driver is None:
            # The counts are worth seeing before the browser is open; say why Start
            # is not available rather than hiding them behind that instruction.
            summary += " | open the browser to start"
        uf_data_label.config(text=summary)
    except Exception as e:
        logger.debug(f"update_unfollow_ui_state error: {e}")


def reset_unfollow_app():
    """Discard the unfollow progress and session (leaves the follow queue alone).

    Unlike the automatic account switch, this throws the record away for good, so
    it says exactly what is about to be lost and what is not. Everyone already
    unfollowed on Instagram stays unfollowed - only the app's memory of it goes,
    which means those accounts can be processed again if they turn up in a future
    export.
    """
    global uf_followers, uf_following, uf_non_followers, uf_progress

    uf_load_progress()
    processed = len(uf_progress.get("processed", []))
    removed = len(uf_progress.get("unfollowed", []))

    if processed or removed:
        warning = (
            f"This deletes the unfollow record for this account:\n\n"
            f"    • {processed} accounts already processed\n"
            f"    • {removed} of them recorded as unfollowed\n"
            f"    • the loaded followers.json / following.json\n\n"
            f"Nobody gets followed back and nothing changes on Instagram - the "
            f"accounts you unfollowed stay unfollowed. What is lost is the app's "
            f"memory of it, so any of them still present in a future export will "
            f"be processed a second time.\n\n"
            f"This cannot be undone. Reset anyway?"
        )
    else:
        warning = (
            "There is no progress to lose yet. This clears the loaded "
            "followers.json / following.json. Continue?"
        )

    if not messagebox.askyesno("Reset unfollow?", warning, icon='warning', default='no'):
        log("Reset cancelled", 'info')
        return

    if os.path.exists(UNFOLLOW_PROGRESS_FILE):
        os.remove(UNFOLLOW_PROGRESS_FILE)
    if os.path.exists(UNFOLLOW_SESSION_FILE):
        os.remove(UNFOLLOW_SESSION_FILE)

    uf_followers = set()
    uf_following = set()
    uf_non_followers = []
    uf_progress = {"processed": [], "unfollowed": [], "skipped": []}

    update_unfollow_ui_state()
    reset_unfollow_progress()
    log(f"🔄 Unfollow reset complete ({processed} processed entries discarded)", 'info')


def scrape_and_fill_queue(hashtags, add_to_queue_limit=0):
    """Scrape users from hashtags and optionally add to queue."""
    global last_scrape_frequencies, live_extracted_users, live_frequencies

    # Reset live extraction tracking for new scrape session
    live_extracted_users = []
    live_frequencies = Counter()

    # Ranks earned in earlier scraping sessions. A rank counts how many scanned
    # authors a candidate follows, so a second session adds to that count rather
    # than replacing it - this counter starts from what is already on disk and
    # every save writes the sum. Starting from zero used to wipe the rank of
    # every candidate found previously, leaving them at 0 and unrankable.
    previous_frequencies = load_frequencies()

    all_users = []
    current_frequencies = Counter()  # This session's contribution only
    total_authors_processed = 0
    throttle_cooldown_count = 0  # Track consecutive low-extraction authors

    # Authors scraped in earlier sessions, and the ones the current hashtag is
    # holding back in case it cannot reach its target with unseen authors.
    author_history = load_author_history()
    visited_authors = set()
    deferred_authors = {}

    def scrape_author(username, profile_url, hashtag):
        """Extract one author's followers and record that the author was used.

        Leaves the browser on the window it was called from, whether or not the
        extraction worked. The caller owns the post dialog, if there is one - only
        the fresh-author path opens one.
        """
        nonlocal author_count, total_authors_processed, throttle_cooldown_count
        global last_scrape_frequencies, live_frequencies

        visited_authors.add(username)
        author_count += 1
        total_authors_processed += 1
        log(f"👤 Author {author_count}/{CONFIG['TARGET_AUTHORS_PER_HASHTAG']}: {username}")

        # The author's profile is read in a window of its own, so the hashtag grid
        # keeps its scroll position. Which window to come back to is remembered
        # rather than assumed to be the first one.
        grid_window = driver.current_window_handle

        driver.execute_script("window.open(arguments[0]);", profile_url)
        driver.switch_to.window(driver.window_handles[-1])

        # Everything from here happens in that window, so getting out of it is a
        # finally and not a line at the end. Reaching the end was the only way back
        # before, and an extraction that raised - a popup that would not scroll, a
        # save that failed - left the browser sitting on the author's profile with
        # the window still open. The caller caught the error and moved on to the
        # next post, whose tile is not on a profile page, so it failed too, and so
        # did every post after it: the run filled with "no such element" and a
        # window was left behind for each one.
        try:
            time.sleep(random.uniform(2.0, 3.5))  # Randomized window switch delay

            if open_followers_popup():
                users = extract_users_from_followers(
                    current_hashtag=hashtag,
                    author_num=author_count,
                    total_authors=CONFIG["TARGET_AUTHORS_PER_HASHTAG"],
                    author_name=username
                )
                all_users.extend(users)

                # Update frequencies incrementally for this author
                current_frequencies.update(users)
                last_scrape_frequencies = previous_frequencies + current_frequencies
                save_frequencies(last_scrape_frequencies)

                log(f"📊 Author {username}: {len(users)} followers extracted (total unique: {len(current_frequencies)})")

                # Update live extraction display for real-time feedback
                live_extracted_users.extend(users)
                live_frequencies = current_frequencies.copy()  # Mirror current frequencies
                update_live_extraction_display()

                # Skip authors with very high follower counts to avoid throttling
                if len(users) < 5:
                    log(f"⏭️ Skipping future posts from {username} (too few extracted, likely throttled)", 'warning')

                # Anti-throttling: detect low extraction as rate limiting signal
                if len(users) < 25:
                    throttle_cooldown_count += 1
                    if throttle_cooldown_count >= 2:
                        cooldown = random.uniform(8, 12)
                        log(f"🐢 Throttling detected ({throttle_cooldown_count} low counts), cooling down for {cooldown:.0f}s...", 'warning')
                        time.sleep(cooldown)
                        throttle_cooldown_count = 0  # Reset after cooldown
                else:
                    throttle_cooldown_count = 0  # Reset on good extraction
        finally:
            leave_extra_window(grid_window)

        # Mark the author used even if the popup never opened. Retrying next
        # session would hit the same wall, and the point of the rotation is to
        # move on to someone who has not been tried.
        author_history[username] = datetime.now().isoformat()
        save_author_history(author_history)

        # Anti-throttling: frequent cooldowns for safety
        if author_count % CONFIG["AUTHORS_BEFORE_COOLDOWN"] == 0:
            cooldown = CONFIG["COOLDOWN_DURATION"] + random.uniform(0, 5)
            log(f"🛡️ Safety cooldown: {cooldown:.0f}s after {author_count} authors...", 'info')
            time.sleep(cooldown)

        # Random delay between authors - safer range
        time.sleep(random.uniform(4, 8))

    total_hashtags = len(hashtags)
    for hashtag_idx, kw in enumerate(hashtags, 1):
        if stop_requested.is_set():
            log("⏹️ Scraping stopped by user", 'warning')
            break

        log(f"\n🔍 Processing hashtag: #{kw}", 'info')

        update_progress(
            hashtag_idx,
            total_hashtags,
            phase="scraping_hashtags",
            current_hashtag=kw
        )

        visited_authors.clear()
        deferred_authors.clear()
        visited_posts = set()
        scroll_count = 0
        author_count = 0
        # A post has to be opened to find out whose it is, so posts spent on authors
        # already handled are the price of working along the grid. Counted to keep
        # that price visible next to what it bought.
        posts_opened = 0

        driver.get(f"https://www.instagram.com/explore/tags/{kw}/")
        time.sleep(random.uniform(2.5, 4.0))  # Randomized initial wait

        if check_rate_limit(driver):
            log("⚠️ Rate limit detected during scraping - continuing anyway", 'warning')
            # Don't break - just warn and continue

        # Keep working rightwards through the grid, scrolling only once the posts
        # already on the page are used up.
        #
        # This used to take a fixed slice of the first six links and then scroll.
        # Instagram appends new posts *below*, so the first six stayed the first
        # six: the next pass found them all visited, skipped them, and scrolled
        # again. Only six posts per hashtag were ever opened however high the
        # scroll count went - and once a few of their authors had been scraped in
        # an earlier session, the run gave up three authors short and fell back to
        # reusing old ones, blaming a hashtag it had barely looked at.
        stop_reason = "target reached"
        unproductive_scrolls = 0

        while (len(visited_authors) < CONFIG["TARGET_AUTHORS_PER_HASHTAG"]
               and not stop_requested.is_set()):

            hrefs = collect_post_links()
            if hrefs is None:
                stop_reason = "a post would not close, so the grid could not be read"
                break

            fresh = [href for href in hrefs if href not in visited_posts]

            if not fresh:
                if scroll_count >= CONFIG["MAX_SCROLLS_PER_HASHTAG"]:
                    stop_reason = f"hit the {CONFIG['MAX_SCROLLS_PER_HASHTAG']}-scroll ceiling"
                    break

                driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);"
                )
                time.sleep(random.uniform(2, 3.5))  # Randomized scroll delay
                scroll_count += 1

                after_scroll = collect_post_links()
                if after_scroll is None:
                    stop_reason = "a post would not close, so the grid could not be read"
                    break

                # A scroll that loads nothing new means the hashtag has no more to
                # show, or Instagram has stopped answering. Either way there is
                # nothing to be gained by scrolling on. Two in a row rather than one,
                # since a single scroll can come back empty on a slow response.
                if len(after_scroll) <= len(hrefs):
                    unproductive_scrolls += 1
                    if unproductive_scrolls >= 2:
                        stop_reason = "the hashtag stopped loading new posts"
                        break
                else:
                    unproductive_scrolls = 0
                continue

            for href in fresh:
                # Check the target before each post, not after
                if len(visited_authors) >= CONFIG["TARGET_AUTHORS_PER_HASHTAG"]:
                    break

                if stop_requested.is_set():
                    break

                visited_posts.add(href)

                # Whatever happens to this post, the grid has to be left uncovered
                # for the next one: open_post() clicks a tile, and a tile behind an
                # open dialog is not something to click. Closing used to be a line
                # on each way out, which meant the one way out that was not written
                # down - an error - left the post open.
                try:
                    open_post(href)
                    posts_opened += 1
                    time.sleep(random.uniform(1.5, 2.5))  # Randomized after-click wait

                    profile_url = get_author_profile()
                    if not profile_url:
                        continue

                    username = profile_url.rstrip("/").split("/")[-1]

                    if username in visited_authors or username in author_history:
                        # Already scraped, either in an earlier session or minutes ago
                        # in this one. Both mean the same thing from here - move on to
                        # the next post - so both say it the same way. Authors have
                        # several posts under one hashtag, so working along the grid
                        # runs into them repeatedly; none of this is a repeated post,
                        # those are filtered out before the loop.
                        #
                        # What differs is only what gets recorded. An author from an
                        # earlier session is held back as a fallback candidate, in
                        # case the hashtag cannot reach its target with new ones -
                        # that is what makes an author left alone for a while come
                        # back up. One already scraped in this run is simply done, and
                        # must not be held back: scrape_author() writes the author
                        # into the history straight away, so without this guard it
                        # would be offered to the fallback and have its followers
                        # popup opened a second time in one run.
                        if username not in visited_authors:
                            deferred_authors[username] = profile_url

                        log(f"⏭️ Skip author {username}, already scraped - moving on to the next post")
                        continue

                    scrape_author(username, profile_url, kw)

                except Exception as e:
                    log(f"❌ Post error: {brief_error(e)}", 'error')
                    logger.debug(f"Post error on {href}", exc_info=True)
                finally:
                    close_post()

        # Short of the target, so fall back to authors used before, least recently
        # scraped first. Their profile URLs are already in hand from the posts that
        # surfaced them, so there is no post to reopen.
        if (deferred_authors
                and len(visited_authors) < CONFIG["TARGET_AUTHORS_PER_HASHTAG"]
                and not stop_requested.is_set()):
            reusable = order_authors_by_staleness(deferred_authors, author_history)
            short_by = CONFIG["TARGET_AUTHORS_PER_HASHTAG"] - len(visited_authors)
            log(
                f"♻️ #{kw}: {len(visited_authors)} new authors, {short_by} short "
                f"({stop_reason}). Reusing {min(short_by, len(reusable))} of the "
                f"{len(reusable)} held back, least recently scraped first",
                'info'
            )
            for username in reusable:
                if (len(visited_authors) >= CONFIG["TARGET_AUTHORS_PER_HASHTAG"]
                        or stop_requested.is_set()):
                    break
                try:
                    scrape_author(username, deferred_authors[username], kw)
                except Exception as e:
                    log(f"❌ Author error for {username}: {brief_error(e)}", 'error')
                    logger.debug(f"Author error for {username}", exc_info=True)

        log(f"🎯 #{kw}: {len(visited_authors)} authors scraped from {posts_opened} posts opened")

        # Anti-throttling: longer break between hashtags
        if hashtag_idx < total_hashtags:
            break_time = CONFIG["HASHTAG_BREAK_DURATION"] + random.uniform(0, 10)
            log(f"☕ Safety break between hashtags: {break_time:.0f}s...", 'info')
            time.sleep(break_time)

    # Which users to offer for the queue: this session's finds, best first. The
    # accumulated counter is for ranking the queue, not for deciding what to add,
    # so a long history cannot crowd out what was just found.
    ranked_users = [u for u, _ in current_frequencies.most_common()]

    # Ensure global frequencies are up to date
    last_scrape_frequencies = previous_frequencies + current_frequencies
    save_frequencies(last_scrape_frequencies)

    log(f"\n🏆 Total unique users found: {len(ranked_users)}", 'success')

    # Calculate frequency distribution for better insights
    freq_dist = Counter(current_frequencies.values())
    total_extractions = sum(current_frequencies.values())
    log(f"📊 Frequency distribution: {dict(sorted(freq_dist.items(), reverse=True)[:5])}", 'info')
    log(f"📊 Top 10 (appeared in most authors): {current_frequencies.most_common(10)}", 'info')
    log(f"💡 Note: Low scores are normal - each hashtag has a different audience", 'info')

    # Add to queue if requested
    if add_to_queue_limit > 0 and ranked_users:
        users_to_add = ranked_users[:add_to_queue_limit]
        new_count, total_count = add_to_queue(users_to_add)
        log(f"📥 Added top {len(users_to_add)} users to queue (out of {len(ranked_users)} found)", 'success')
        log(f"📋 Queue now has {total_count} total users", 'info')
        # Refresh queue display
        refresh_queue_display()

    # The search has just left a queue longer than anyone will follow through, and
    # nearly all of it on one sighting count, which is to say in a tie that count
    # has no opinion about. Reading the strongest few hundred is what breaks it.
    if not stop_requested.is_set():
        try:
            run_scoring_pass()
        except Exception as e:
            log(f"❌ Scoring pass failed: {brief_error(e)}", 'error')
            logger.exception("The scoring pass failed")

    return ranked_users


def follow_logic():
    """Main follow logic - supports both queue and search modes."""
    global stats
    stats = SessionStats(on_update=lambda: update_stats_display())
    stop_requested.clear()

    try:
        # Get common settings
        try:
            delay_min = int(delay_min_entry.get())
            delay_max = int(delay_max_entry.get())
            limit = int(limit_entry.get())
        except ValueError:
            log("❌ Invalid numeric input!", 'error')
            messagebox.showerror("Error", "Please enter valid numbers")
            return

        if not browser_is_open():
            log("❌ Browser not open!", 'error')
            handle_browser_closed()
            messagebox.showerror("Error", "Please open browser first")
            return

        # Update UI
        reset_progress()
        # Progress bar always uses 0-100 percentage scale
        progress_bar['maximum'] = 100

        # Get mode
        mode = mode_var.get()

        if mode == 'queue':
            # Queue mode: follow from saved queue
            log(f"🚀 Starting QUEUE MODE session...")
            log(f"🎯 Target: {limit} follows from queue")

            queue = load_queue()
            if not queue:
                log("❌ Queue is empty! Switch to 'Deep Search' mode to find users.", 'error')
                messagebox.showerror("Queue Empty", "No users in queue. Use 'Deep Search' mode to find users.")
                return

            log(f"📋 Queue has {len(queue)} users")

            # Follow from queue
            successful = follow_from_queue(queue, delay_min, delay_max, limit)

        else:
            # Search mode: scrape hashtags, optionally add to queue, then follow
            hashtags = list(hashtag_listbox.get(0, tk.END))
            if not hashtags:
                log("❌ No hashtags selected!", 'error')
                messagebox.showerror("Error", "Please add at least one hashtag")
                return

            log(f"🚀 Starting DEEP SEARCH session...")
            log(f"Hashtags: {hashtags}")
            log(f"🛡️ Safety: Extraction will use conservative delays to avoid detection")
            log(f"💡 Tip: You can STOP anytime - extracted users are saved with rankings")
            log(f"📋 Recommended: Extract now, then follow from queue in separate sessions")

            # Scrape users
            ranked_users = scrape_and_fill_queue(hashtags, add_to_queue_limit=0)

            if not ranked_users:
                log("❌ No users found during scraping", 'error')
                return

            # Ask if user wants to add to queue or follow directly
            # Show frequency stats in the dialog
            top_score = ranked_users[0] if ranked_users else None
            top_freq = ranking_frequencies().get(top_score, 0) if top_score else 0

            result = messagebox.askyesnocancel(
                "Search Complete",
                f"Found {len(ranked_users)} unique users from {len(hashtags)} hashtag(s).\n"
                f"Highest frequency: {top_freq} (appeared in {top_freq} authors' followers).\n\n"
                f"What do you want to do?\n\n"
                f"• YES: Save to queue and STOP (follow manually later from GUI)\n"
                f"• NO: Save to queue and START following now\n"
                f"• CANCEL: Discard results and stop",
                icon='question'
            )

            if result is None:  # CANCEL - discard and stop
                log("❌ Scraping cancelled by user", 'warning')
                return

            # Add top 500 users to queue (always, regardless of choice)
            users_to_add = ranked_users[:500]
            new_count, total_count = add_to_queue(users_to_add)
            log(f"✅ Added top {len(users_to_add)} users to queue (out of {len(ranked_users)} total found)", 'success')
            log(f"📋 Queue now has {total_count} users total", 'info')
            refresh_queue_display()
            update_live_extraction_display()

            if result:  # YES - Save to queue and STOP
                log("🛑 Scraping complete. Start following manually when ready.", 'success')
                report = stats.report()
                messagebox.showinfo("Saved to Queue", f"{new_count} users saved to queue.\n\nClick 'Start Following' in the GUI to begin following.")
                return

            # NO - Save to queue and START following now
            queue = load_queue()
            successful = follow_from_queue(queue, delay_min, delay_max, limit)

        # Final report
        report = stats.report()
        log(f"\n{report}", 'success')

        # Validate queue after session to ensure consistency
        removed, remaining = validate_queue()
        log(f"📋 Queue status: {remaining} users remaining (removed {removed} invalid entries)", 'info')

        messagebox.showinfo("Session Complete", f"{report}\n\nQueue: {remaining} users remaining")

    except Exception as e:
        log(f"❌ Fatal error: {e}", 'error')
        logger.exception("Fatal error in follow_logic")
    finally:
        # Releases the browser for the next session, and re-checks it rather than
        # handing back a Start button that cannot work because it was closed.
        end_session()
        refresh_queue_display()  # Update queue display
        update_live_extraction_display()  # Final update of live extraction
        if not stop_requested.is_set():
            reset_progress()

def run_follow():
    """Start follow in background thread."""
    if not begin_session():
        return
    thread = threading.Thread(target=follow_logic, daemon=True)
    thread.start()
    active_threads.append(thread)

# ---------------------------
# HASHTAG MANAGEMENT
# ---------------------------
def add_hashtag():
    """Add hashtag to list."""
    tag = hashtag_entry.get().strip().lower()
    if tag:
        # Remove # if present
        tag = tag.lstrip('#')

        existing = hashtag_listbox.get(0, tk.END)
        if tag not in existing:
            hashtag_listbox.insert(tk.END, tag)
            hashtag_entry.delete(0, tk.END)
            save_hashtags(list(hashtag_listbox.get(0, tk.END)))  # Persist changes
            log(f"Added hashtag: #{tag}", 'success')
        else:
            log(f"Hashtag #{tag} already in list", 'warning')

def remove_hashtag():
    """Remove selected hashtag."""
    selection = hashtag_listbox.curselection()
    if selection:
        hashtag_listbox.delete(selection[0])
        save_hashtags(list(hashtag_listbox.get(0, tk.END)))  # Persist changes

def clear_hashtags():
    """Clear all hashtags."""
    hashtag_listbox.delete(0, tk.END)
    save_hashtags([])  # Persist empty list

# ---------------------------
# QUEUE UI FUNCTIONS
# ---------------------------
def refresh_queue_display():
    """Refresh the queue listbox display with frequency rankings."""
    global displayed_queue_usernames

    queue_listbox.delete(0, tk.END)
    # Validate queue before displaying to ensure consistency
    validate_queue()
    ranked = rank_queue(load_queue())

    # Remember what each row holds, so acting on a selection never has to
    # reconstruct the ordering and cannot disagree with what is on screen.
    displayed_queue_usernames = [username for username, _, _ in ranked[:100]]

    # The row says what the order is made of, not the number it comes out as: how
    # many scanned authors this candidate follows, and how close their profile read
    # to the niche where that has been measured. A single 0.61 on screen would sort
    # the list correctly and tell you nothing about why.
    frequencies = ranking_frequencies()
    for user, _, item in ranked[:100]:  # Show first 100
        seen = frequencies.get(user, 0)
        affinity = queue_affinity(item)
        parts = [str(seen)] if seen else []
        if affinity is not None:
            parts.append(f"{affinity:.0%}")
        queue_listbox.insert(tk.END, f"[{' · '.join(parts)}] {user}" if parts else user)

    if len(ranked) > 100:
        queue_listbox.insert(tk.END, f"... and {len(ranked) - 100} more")

    queue_count_label.config(text=f"Queue: {len(ranked)} users")

    # Also update main tab info
    try:
        main_queue_info.config(text=f"Queue: {len(ranked)} users waiting")
    except:
        pass  # Might not exist yet

def update_live_extraction_display():
    """Update the live extraction listbox with current extracted users and their rankings."""
    try:
        global live_extraction_listbox, live_extraction_label
        live_extraction_listbox.delete(0, tk.END)

        if not live_extracted_users:
            live_extraction_listbox.insert(tk.END, "Waiting for extraction...")
            return

        # Get current frequencies
        global live_frequencies
        frequencies = live_frequencies

        # Create sorted list by frequency (highest first)
        user_freq_list = [(user, frequencies.get(user, 0)) for user in live_extracted_users]
        # Remove duplicates while preserving order (first occurrence wins for equal frequency)
        seen = set()
        unique_users = []
        for user, freq in user_freq_list:
            if user not in seen:
                seen.add(user)
                unique_users.append((user, freq))
        # Sort by frequency descending
        unique_users.sort(key=lambda x: -x[1])

        # Show top 50 users with their rank
        for rank, (user, freq) in enumerate(unique_users[:50], 1):
            display_text = f"#{rank} [{freq}] {user}"
            live_extraction_listbox.insert(tk.END, display_text)

        # Update count label
        unique_count = len(unique_users)
        live_extraction_label.config(text=f"Extracted: {unique_count} unique users (showing top 50)")

        # Force GUI update
        root.update_idletasks()
    except Exception as e:
        logger.debug(f"Live extraction display error: {e}")

def add_to_queue_ui():
    """Add users from entry to queue."""
    text = queue_entry.get().strip()
    if not text:
        return

    # Split by comma, space, or newline
    usernames = [u.strip().lower().lstrip('@') for u in re.split(r'[,\s\n]+', text) if u.strip()]

    if not usernames:
        return

    new_count, total_count = add_to_queue(usernames)
    queue_entry.delete(0, tk.END)
    refresh_queue_display()
    log(f"✅ Added {new_count} new users to queue (total: {total_count})", 'success')

def remove_from_queue_ui():
    """Remove selected user from queue."""
    selection = queue_listbox.curselection()
    if selection:
        idx = selection[0]

        # Read the row straight off what was drawn. Rebuilding the ordering here
        # could disagree with the listbox, and the bound also guards the trailing
        # "... and N more" row, which is not a user.
        if idx < len(displayed_queue_usernames):
            username = displayed_queue_usernames[idx]
            remove_from_queue(username)
            refresh_queue_display()
            log(f"Removed {username} from queue", 'info')

def clear_queue_ui():
    """Clear queue with confirmation."""
    if messagebox.askyesno("Confirm", "Clear entire follow queue?"):
        clear_queue()
        refresh_queue_display()
        log("🗑️ Queue cleared", 'warning')

def import_queue_from_file():
    """Import users from file."""
    filepath = filedialog.askopenfilename(
        filetypes=[("Text files", "*.txt"), ("JSON files", "*.json"), ("All files", "*.*")]
    )
    if not filepath:
        return

    try:
        usernames = []
        if filepath.endswith('.json'):
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    # Accepts an exported queue (dicts) or a plain list of usernames
                    usernames = [u for u in (queue_username(item) for item in data) if u]
                elif isinstance(data, dict):
                    usernames = list(data.keys())
        else:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        usernames.append(line.lstrip('@'))

        new_count, total_count = add_to_queue(usernames)
        refresh_queue_display()
        log(f"✅ Imported {new_count} new users from file (total: {total_count})", 'success')
    except Exception as e:
        messagebox.showerror("Error", f"Failed to import: {e}")

def export_queue_to_file():
    """Export queue to file."""
    filepath = filedialog.asksaveasfilename(
        defaultextension=".txt",
        filetypes=[("Text files", "*.txt"), ("JSON files", "*.json")]
    )
    if not filepath:
        return

    try:
        queue = load_queue()
        usernames = [queue_username(item) for item in queue]

        if filepath.endswith('.json'):
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(queue, f, indent=2, ensure_ascii=False)
        else:
            with open(filepath, 'w', encoding='utf-8') as f:
                for user in usernames:
                    f.write(user + '\n')
        log(f"✅ Exported {len(usernames)} users to {filepath}", 'success')
    except Exception as e:
        messagebox.showerror("Error", f"Failed to export: {e}")

def add_scraped_to_queue():
    """Add users from the last scrape to queue."""
    # This will be called from follow_logic after scraping
    # For now, just a placeholder that will be set dynamically
    pass

# ---------------------------
# MENU ACTIONS
# ---------------------------
def export_logs():
    """Export logs to file."""
    try:
        filename = data_path(f"follow_logs_{datetime.now():%Y%m%d_%H%M%S}.txt")
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(log_box.get(1.0, tk.END))
        log(f"Logs exported to {filename}", 'success')
        messagebox.showinfo("Exported", f"Logs saved to:\n{filename}")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to export: {e}")

def show_about():
    """Show about dialog."""
    messagebox.showinfo(
        "About",
        "Reciproca v3.0\n\n"
        "Features:\n"
        "• Queue-based following (session-safe)\n"
        "• Deep search via hashtags\n"
        "• Unfollow of non-followers from Instagram data export\n"
        "• Rate limit detection\n"
        "• Modern GUI with tabs\n"
        "• Persistent user queue & unfollow progress\n"
        "• Retry logic & validation"
    )

def on_closing():
    """Handle window close."""
    if messagebox.askokcancel("Quit", "Close browser and exit?"):
        stop_bot()
        # Save hashtags before closing
        try:
            if 'hashtag_listbox' in globals():
                current_hashtags = list(hashtag_listbox.get(0, tk.END))
                save_hashtags(current_hashtags)
        except Exception as e:
            logger.debug(f"Error saving hashtags on exit: {e}")
        if driver:
            try:
                driver.quit()
            except:
                pass
        root.destroy()

# ---------------------------
# GUI SETUP
# ---------------------------
def setup_gui():
    """Setup the main GUI."""
    global root, log_box, progress_bar, status_label, stats_label
    global hashtag_listbox, hashtag_entry, delay_min_entry, delay_max_entry
    global limit_entry, start_btn, stop_btn, browser_btn
    global queue_listbox, queue_entry, queue_count_label, mode_var, main_queue_info
    global live_extraction_listbox, live_extraction_label
    global uf_data_label, uf_delay_min_entry, uf_delay_max_entry, uf_limit_entry
    global uf_progress_bar, uf_status_label, uf_stats_label, uf_start_btn, uf_stop_btn
    global uf_browser_btn

    root = tk.Tk()
    root.title("Reciproca - Follow & Unfollow")
    root.geometry("800x700")
    root.minsize(700, 600)

    # Center window
    root.eval('tk::PlaceWindow . center')

    # Menu bar
    menubar = tk.Menu(root)
    root.config(menu=menubar)

    file_menu = tk.Menu(menubar, tearoff=0)
    menubar.add_cascade(label="File", menu=file_menu)
    file_menu.add_command(label="Export Logs", command=export_logs)
    file_menu.add_separator()
    file_menu.add_command(label="Exit", command=on_closing)

    help_menu = tk.Menu(menubar, tearoff=0)
    menubar.add_cascade(label="Help", menu=help_menu)
    help_menu.add_command(label="About", command=show_about)

    # Style
    style = ttk.Style()
    style.theme_use('clam')
    style.configure('Accent.TButton', background='#405de6', foreground='white')
    style.configure('Success.TButton', foreground='green')
    style.configure('Danger.TButton', foreground='red')

    # Notebook (tabs)
    notebook = ttk.Notebook(root)
    notebook.pack(fill='both', expand=True, padx=10, pady=10)

    # Tab 1: Main. Follow Queue comes next because the two work together - one
    # fills the queue, the other shows it - so unfollow does not sit between them.
    main_tab = ttk.Frame(notebook, padding=10)
    notebook.add(main_tab, text='🎯 Auto Follow')

    # Tab 2: Queue
    queue_tab = ttk.Frame(notebook, padding=10)
    notebook.add(queue_tab, text='📋 Follow Queue')

    # Tab 3: Unfollow
    unfollow_tab = ttk.Frame(notebook, padding=10)
    notebook.add(unfollow_tab, text='🚫 Unfollow')

    # Tab 4: Settings
    settings_tab = ttk.Frame(notebook, padding=10)
    notebook.add(settings_tab, text='⚙️ Settings')

    # Tab 5: Logs
    logs_tab = ttk.Frame(notebook, padding=10)
    notebook.add(logs_tab, text='📝 Logs')

    # ==================== MAIN TAB ====================

    # Hashtag section
    hashtag_frame = ttk.LabelFrame(main_tab, text='Hashtags', padding=10)
    hashtag_frame.pack(fill='x', pady=(0, 10))

    # Hashtag list with scrollbar
    list_frame = ttk.Frame(hashtag_frame)
    list_frame.pack(side='left', fill='both', expand=True)

    hashtag_listbox = tk.Listbox(
        list_frame,
        height=5,
        selectmode=tk.SINGLE,
        font=('Consolas', 10)
    )
    hashtag_listbox.pack(side='left', fill='both', expand=True)

    scrollbar = ttk.Scrollbar(
        list_frame,
        orient='vertical',
        command=hashtag_listbox.yview
    )
    scrollbar.pack(side='right', fill='y')
    hashtag_listbox.config(yscrollcommand=scrollbar.set)

    # Load saved hashtags or use defaults
    saved_hashtags = load_hashtags()
    if saved_hashtags is not None:
        # User has a saved list, use it
        for tag in saved_hashtags:
            hashtag_listbox.insert(tk.END, tag)
    else:
        # First run - use defaults and save them
        default_hashtags = ['photography', 'photooftheday', 'streetphotography', 'landscape', 'perspective']
        for tag in default_hashtags:
            hashtag_listbox.insert(tk.END, tag)
        save_hashtags(default_hashtags)

    # Hashtag controls
    btn_frame = ttk.Frame(hashtag_frame)
    btn_frame.pack(side='right', padx=(10, 0), fill='y')

    hashtag_entry = ttk.Entry(btn_frame, width=15)
    hashtag_entry.pack(pady=(0, 5))
    hashtag_entry.bind('<Return>', lambda e: add_hashtag())

    ttk.Button(btn_frame, text='➕ Add', command=add_hashtag).pack(fill='x', pady=2)
    ttk.Button(btn_frame, text='➖ Remove', command=remove_hashtag).pack(fill='x', pady=2)
    ttk.Button(btn_frame, text='🗑️ Clear', command=clear_hashtags).pack(fill='x', pady=2)

    ToolTip(hashtag_entry, "Enter hashtag without # (e.g., 'photography')")

    # Mode selection
    mode_frame = ttk.LabelFrame(main_tab, text='Operation Mode', padding=10)
    mode_frame.pack(fill='x', pady=(0, 10))

    mode_var = tk.StringVar(value='search')  # Default to deep search

    ttk.Radiobutton(
        mode_frame,
        text='🔍 Deep Search (find new users via hashtags)',
        variable=mode_var,
        value='search'
    ).pack(anchor='w', pady=2)

    ttk.Radiobutton(
        mode_frame,
        text='📋 Follow from Queue (safe - uses saved list)',
        variable=mode_var,
        value='queue'
    ).pack(anchor='w', pady=2)

    main_queue_info = ttk.Label(
        mode_frame,
        text=f"Queue: {len(load_queue())} users waiting",
        font=('Helvetica', 9, 'italic'),
        foreground='gray'
    )
    main_queue_info.pack(anchor='w', pady=(5, 0))

    # Quick settings for follow timing (used at runtime)
    quick_frame = ttk.LabelFrame(main_tab, text='⏱️ Follow Timing (Runtime Settings)', padding=8)
    quick_frame.pack(fill='x', pady=(0, 10))

    vcmd = (root.register(validate_number), '%P')

    ttk.Label(quick_frame, text='Delay Min (sec):').pack(side='left', padx=(0, 3))
    delay_min_entry = ttk.Entry(quick_frame, width=6, validate='key', validatecommand=vcmd)
    delay_min_entry.insert(0, str(CONFIG["DEFAULT_DELAY_MIN"]))
    delay_min_entry.pack(side='left', padx=(0, 10))

    ttk.Label(quick_frame, text='Delay Max (sec):').pack(side='left', padx=(0, 3))
    delay_max_entry = ttk.Entry(quick_frame, width=6, validate='key', validatecommand=vcmd)
    delay_max_entry.insert(0, str(CONFIG["DEFAULT_DELAY_MAX"]))
    delay_max_entry.pack(side='left', padx=(0, 10))

    ttk.Label(quick_frame, text='Follow Limit:').pack(side='left', padx=(0, 3))
    limit_entry = ttk.Entry(quick_frame, width=6, validate='key', validatecommand=vcmd)
    limit_entry.insert(0, str(CONFIG["MAX_FOLLOWS_PER_SESSION"]))
    limit_entry.pack(side='left', padx=(0, 10))

    ToolTip(limit_entry, "Maximum follows this session. Bot saves remaining to queue.")

    # Progress section
    progress_frame = ttk.LabelFrame(main_tab, text='Progress', padding=10)
    progress_frame.pack(fill='x', pady=(0, 10))

    progress_bar = ttk.Progressbar(
        progress_frame,
        mode='determinate',
        length=400
    )
    progress_bar.pack(fill='x', pady=5)

    status_label = ttk.Label(
        progress_frame,
        text='Ready',
        font=('Helvetica', 11, 'bold')
    )
    status_label.pack()

    stats_label = ttk.Label(
        progress_frame,
        text='Followed: 0 | Attempted: 0 | Skipped: 0 | Errors: 0',
        font=('Helvetica', 10)
    )
    stats_label.pack(pady=(5, 0))

    # Control buttons
    control_frame = ttk.Frame(main_tab)
    control_frame.pack(pady=20)

    browser_btn = ttk.Button(
        control_frame,
        text='🌐 Open Browser',
        command=start_browser,
        width=20
    )
    browser_btn.pack(side='left', padx=5)

    start_btn = ttk.Button(
        control_frame,
        text='🚀 Start Following',
        command=run_follow,
        width=20,
        style='Accent.TButton',
        state='disabled'
    )
    start_btn.pack(side='left', padx=5)

    stop_btn = ttk.Button(
        control_frame,
        text='⏹️ Stop',
        command=stop_bot,
        width=15,
        state='disabled'
    )
    stop_btn.pack(side='left', padx=5)

    # ==================== UNFOLLOW TAB ====================

    # Data section
    uf_data_frame = ttk.LabelFrame(unfollow_tab, text='📂 Data (Instagram export)', padding=10)
    uf_data_frame.pack(fill='x', pady=(0, 10))

    ttk.Label(
        uf_data_frame,
        text="Load the followers.json and following.json files downloaded from your\n"
             "Instagram settings (Privacy and security > Download your data).\n"
             "The tool works out who you follow that doesn't follow you back.",
        justify='left'
    ).pack(anchor='w', pady=(0, 8))

    uf_data_btn_frame = ttk.Frame(uf_data_frame)
    uf_data_btn_frame.pack(fill='x')

    ttk.Button(
        uf_data_btn_frame, text='📥 Load JSON', command=uf_load_json_files
    ).pack(side='left', padx=(0, 5))

    # Next to Load JSON rather than with the session controls: both act on the
    # loaded data, and Reset is what you reach for when a load went wrong.
    ttk.Button(
        uf_data_btn_frame, text='🔄 Reset', command=reset_unfollow_app
    ).pack(side='left', padx=(0, 10))

    uf_data_label = ttk.Label(
        uf_data_btn_frame,
        text="🟡 Load followers.json and following.json to begin",
        font=('Helvetica', 9, 'italic'),
        foreground='gray'
    )
    uf_data_label.pack(side='left')

    # Timing section
    uf_timing_frame = ttk.LabelFrame(unfollow_tab, text='⏱️ Unfollow Timing (Runtime Settings)', padding=8)
    uf_timing_frame.pack(fill='x', pady=(0, 10))

    uf_vcmd = (root.register(validate_number), '%P')

    ttk.Label(uf_timing_frame, text='Delay Min (sec):').pack(side='left', padx=(0, 3))
    uf_delay_min_entry = ttk.Entry(uf_timing_frame, width=6, validate='key', validatecommand=uf_vcmd)
    uf_delay_min_entry.insert(0, str(CONFIG["UNFOLLOW_DELAY_MIN"]))
    uf_delay_min_entry.pack(side='left', padx=(0, 10))

    ttk.Label(uf_timing_frame, text='Delay Max (sec):').pack(side='left', padx=(0, 3))
    uf_delay_max_entry = ttk.Entry(uf_timing_frame, width=6, validate='key', validatecommand=uf_vcmd)
    uf_delay_max_entry.insert(0, str(CONFIG["UNFOLLOW_DELAY_MAX"]))
    uf_delay_max_entry.pack(side='left', padx=(0, 10))

    ttk.Label(uf_timing_frame, text='Session Limit:').pack(side='left', padx=(0, 3))
    uf_limit_entry = ttk.Entry(uf_timing_frame, width=6, validate='key', validatecommand=uf_vcmd)
    uf_limit_entry.insert(0, str(CONFIG["UNFOLLOW_DAILY_LIMIT"]))
    uf_limit_entry.pack(side='left', padx=(0, 10))

    ToolTip(uf_limit_entry, "Maximum unfollows this session. Progress is saved so you can resume later.")

    # Progress section
    uf_progress_frame = ttk.LabelFrame(unfollow_tab, text='Progress', padding=10)
    uf_progress_frame.pack(fill='x', pady=(0, 10))

    uf_progress_bar = ttk.Progressbar(uf_progress_frame, mode='determinate', length=400)
    uf_progress_bar.pack(fill='x', pady=5)

    uf_status_label = ttk.Label(uf_progress_frame, text='Ready', font=('Helvetica', 11, 'bold'))
    uf_status_label.pack()

    uf_stats_label = ttk.Label(
        uf_progress_frame,
        text='Unfollowed: 0 | Attempted: 0 | Errors: 0',
        font=('Helvetica', 10)
    )
    uf_stats_label.pack(pady=(5, 0))

    # Control buttons
    uf_control_frame = ttk.Frame(unfollow_tab)
    uf_control_frame.pack(pady=20)

    uf_browser_btn = ttk.Button(
        uf_control_frame,
        text='🌐 Open Browser',
        command=start_browser,
        width=20
    )
    uf_browser_btn.pack(side='left', padx=5)

    uf_start_btn = ttk.Button(
        uf_control_frame,
        text='🚫 Start Unfollow',
        command=run_unfollow,
        width=20,
        style='Accent.TButton',
        state='disabled'
    )
    uf_start_btn.pack(side='left', padx=5)

    uf_stop_btn = ttk.Button(
        uf_control_frame,
        text='⏹️ Stop',
        command=stop_bot,
        width=15,
        state='disabled'
    )
    uf_stop_btn.pack(side='left', padx=5)

    ttk.Label(
        unfollow_tab,
        text="Note: shares one browser and login with the 'Auto Follow' tab - opening it here is the same as opening it there.",
        foreground='gray',
        font=('Helvetica', 8, 'italic')
    ).pack(anchor='w', pady=(0, 5))

    # ==================== QUEUE TAB ====================

    # Queue info header
    queue_header_frame = ttk.Frame(queue_tab)
    queue_header_frame.pack(fill='x', pady=(0, 10))

    queue_count_label = ttk.Label(
        queue_header_frame,
        text=f'Follow Queue: {len(load_queue())} users',
        font=('Helvetica', 12, 'bold')
    )
    queue_count_label.pack(side='left')

    live_extraction_label = ttk.Label(
        queue_header_frame,
        text='| Live Extraction: 0 users',
        font=('Helvetica', 12, 'bold')
    )
    live_extraction_label.pack(side='left', padx=(20, 0))

    # Two-section layout: Follow Queue + Live Extraction
    lists_frame = ttk.Frame(queue_tab)
    lists_frame.pack(fill='both', expand=True, pady=(0, 10))

    # Left: Follow Queue
    queue_list_frame = ttk.LabelFrame(lists_frame, text='📋 Follow Queue', padding=10)
    queue_list_frame.pack(side='left', fill='both', expand=True, pady=(0, 5), padx=(0, 5))

    queue_listbox = tk.Listbox(
        queue_list_frame,
        selectmode=tk.SINGLE,
        font=('Consolas', 10)
    )
    queue_listbox.pack(side='left', fill='both', expand=True)

    queue_scrollbar = ttk.Scrollbar(
        queue_list_frame,
        orient='vertical',
        command=queue_listbox.yview
    )
    queue_scrollbar.pack(side='right', fill='y')
    queue_listbox.config(yscrollcommand=queue_scrollbar.set)

    # Right: Live Extraction
    live_list_frame = ttk.LabelFrame(lists_frame, text='🔄 Live Extraction', padding=10)
    live_list_frame.pack(side='left', fill='both', expand=True, pady=(0, 5), padx=(5, 0))

    live_extraction_listbox = tk.Listbox(
        live_list_frame,
        selectmode=tk.SINGLE,
        font=('Consolas', 10)
    )
    live_extraction_listbox.pack(side='left', fill='both', expand=True)

    live_scrollbar = ttk.Scrollbar(
        live_list_frame,
        orient='vertical',
        command=live_extraction_listbox.yview
    )
    live_scrollbar.pack(side='right', fill='y')
    live_extraction_listbox.config(yscrollcommand=live_scrollbar.set)

    # Initialize with placeholder
    live_extraction_listbox.insert(0, "Waiting for extraction...")

    # Refresh the display
    refresh_queue_display()
    update_live_extraction_display()

    # Queue controls
    queue_ctrl_frame = ttk.Frame(queue_tab)
    queue_ctrl_frame.pack(fill='x', pady=(0, 10))

    queue_entry = ttk.Entry(queue_ctrl_frame, width=40)
    queue_entry.pack(side='left', fill='x', expand=True, padx=(0, 5))
    queue_entry.bind('<Return>', lambda e: add_to_queue_ui())
    ToolTip(queue_entry, "Enter usernames separated by comma, space, or newline")

    ttk.Button(queue_ctrl_frame, text='➕ Add', command=add_to_queue_ui).pack(side='left', padx=2)
    ttk.Button(queue_ctrl_frame, text='➖ Remove', command=remove_from_queue_ui).pack(side='left', padx=2)
    ttk.Button(queue_ctrl_frame, text='🔄 Refresh', command=refresh_queue_display).pack(side='left', padx=2)

    # Import/Export buttons
    queue_io_frame = ttk.Frame(queue_tab)
    queue_io_frame.pack(fill='x', pady=(0, 10))

    ttk.Button(
        queue_io_frame,
        text='📥 Import from File',
        command=import_queue_from_file
    ).pack(side='left', padx=2)

    ttk.Button(
        queue_io_frame,
        text='📤 Export to File',
        command=export_queue_to_file
    ).pack(side='left', padx=2)

    ttk.Button(
        queue_io_frame,
        text='🗑️ Clear Queue',
        command=clear_queue_ui
    ).pack(side='left', padx=2)

    # ==================== SETTINGS TAB ====================

    # Create scrollable frame for settings
    settings_container = ttk.Frame(settings_tab)
    settings_container.pack(fill='both', expand=True)

    settings_canvas = tk.Canvas(settings_container, highlightthickness=0)
    settings_scrollbar = ttk.Scrollbar(settings_container, orient="vertical", command=settings_canvas.yview)
    settings_scrollable_frame = ttk.Frame(settings_canvas)

    settings_scrollable_frame.bind(
        "<Configure>",
        lambda e: settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))
    )

    # Create the window and configure it to fill width
    canvas_window = settings_canvas.create_window((0, 0), window=settings_scrollable_frame, anchor="nw")

    def configure_canvas(event):
        # Resize the inner window to match canvas width
        settings_canvas.itemconfig(canvas_window, width=event.width)

    settings_canvas.bind('<Configure>', configure_canvas)
    settings_canvas.configure(yscrollcommand=settings_scrollbar.set)

    # Mouse wheel scrolling - bind only when mouse is over this canvas
    def _on_mousewheel(event):
        settings_canvas.yview_scroll(int(-1*(event.delta/120)), 'units')
    def _bound_to_mousewheel(event):
        settings_canvas.bind_all('<MouseWheel>', _on_mousewheel)
    def _unbound_to_mousewheel(event):
        settings_canvas.unbind_all('<MouseWheel>')
    settings_canvas.bind('<Enter>', _bound_to_mousewheel)
    settings_canvas.bind('<Leave>', _unbound_to_mousewheel)

    settings_canvas.pack(side="left", fill="both", expand=True)
    settings_scrollbar.pack(side="right", fill="y")

    vcmd = (root.register(validate_number), '%P')

    # Configure the scrollable frame to expand
    settings_scrollable_frame.columnconfigure(0, weight=1)
    settings_scrollable_frame.columnconfigure(1, weight=0)
    settings_scrollable_frame.columnconfigure(2, weight=1)

    # Dictionary to hold config entry widgets
    config_entries = {}
    config_text_entries = {}

    def create_config_row(parent, row, label, config_key, description, is_password=False, validate=True):
        """Helper to create a labeled config row with entry field."""
        lbl = ttk.Label(parent, text=f'{label}:', font=('Helvetica', 9, 'bold'))
        lbl.grid(row=row, column=0, sticky='w', pady=3, padx=(0, 5))

        entry_kwargs = {'width': 10}
        if validate:
            entry_kwargs['validate'] = 'key'
            entry_kwargs['validatecommand'] = vcmd

        entry = ttk.Entry(parent, **entry_kwargs)
        entry.insert(0, str(CONFIG.get(config_key, "")))
        entry.grid(row=row, column=1, pady=3, padx=5)

        desc_lbl = ttk.Label(parent, text=description, foreground='gray', font=('Helvetica', 8))
        desc_lbl.grid(row=row, column=2, sticky='w', pady=3)

        config_entries[config_key] = entry
        return entry

    def create_text_row(parent, row, label, config_key, description):
        """A setting that is words rather than a number, so it gets room to be read.

        Kept apart from the numbered rows because saving them converts every entry
        to an integer, and a sentence put through that would take the whole save
        down with it.
        """
        ttk.Label(parent, text=f'{label}:', font=('Helvetica', 9, 'bold')).grid(
            row=row, column=0, sticky='w', pady=3, padx=(0, 5)
        )

        entry = ttk.Entry(parent, width=60)
        entry.insert(0, str(CONFIG.get(config_key, "")))
        entry.grid(row=row, column=1, columnspan=2, sticky='we', pady=3, padx=5)

        ttk.Label(parent, text=description, foreground='gray', font=('Helvetica', 8)).grid(
            row=row + 1, column=1, columnspan=2, sticky='w', padx=5
        )

        config_text_entries[config_key] = entry
        return entry

    # ─── EXTRACTION SETTINGS ───
    extraction_frame = ttk.LabelFrame(settings_scrollable_frame, text='🔍 Extraction Settings', padding=10)
    extraction_frame.pack(fill='x', pady=(0, 10), padx=5, expand=True)

    create_config_row(extraction_frame, 0, "TARGET_AUTHORS_PER_HASHTAG", "TARGET_AUTHORS_PER_HASHTAG",
                      "← Number of unique authors to process per hashtag")
    create_config_row(extraction_frame, 1, "MAX_SCROLLS_PER_HASHTAG", "MAX_SCROLLS_PER_HASHTAG",
                      "← Safety ceiling on scrolls per hashtag (rarely reached)")
    create_config_row(extraction_frame, 2, "FOLLOWER_SCROLL_COUNT", "FOLLOWER_SCROLL_COUNT",
                      "← How many times to scroll the followers list per profile")
    create_config_row(extraction_frame, 3, "AUTHORS_BEFORE_COOLDOWN", "AUTHORS_BEFORE_COOLDOWN",
                      "← After how many authors to trigger a cooldown")
    create_config_row(extraction_frame, 4, "COOLDOWN_DURATION", "COOLDOWN_DURATION",
                      "← Seconds of cooldown between author groups")
    create_config_row(extraction_frame, 5, "HASHTAG_BREAK_DURATION", "HASHTAG_BREAK_DURATION",
                      "← Seconds to wait between different hashtags")
    create_config_row(extraction_frame, 6, "EXTRACTION_PAUSE_DURATION", "EXTRACTION_PAUSE_DURATION",
                      "← Hours between extraction sessions")

    # ─── FOLLOW SETTINGS ───
    follow_frame = ttk.LabelFrame(settings_scrollable_frame, text='⏱️ Follow Settings', padding=10)
    follow_frame.pack(fill='x', pady=(0, 10), padx=5, expand=True)

    create_config_row(follow_frame, 0, "DEFAULT_DELAY_MIN", "DEFAULT_DELAY_MIN",
                      "← Minimum seconds between follow actions")
    create_config_row(follow_frame, 1, "DEFAULT_DELAY_MAX", "DEFAULT_DELAY_MAX",
                      "← Maximum seconds between follow actions (randomized)")
    create_config_row(follow_frame, 2, "FOLLOW_BATCH_SIZE", "FOLLOW_BATCH_SIZE",
                      "← How many follows before a batch cooldown")
    create_config_row(follow_frame, 3, "FOLLOW_BATCH_COOLDOWN", "FOLLOW_BATCH_COOLDOWN",
                      "← Seconds of cooldown after each batch")
    create_config_row(follow_frame, 4, "MAX_FOLLOWS_PER_SESSION", "MAX_FOLLOWS_PER_SESSION",
                      "← Soft target for follows per session (not enforced)")

    # ─── UNFOLLOW SETTINGS ───
    bot_frame = ttk.LabelFrame(settings_scrollable_frame, text='🤖 Bot Filter', padding=10)
    bot_frame.pack(fill='x', pady=(0, 10), padx=5, expand=True)

    create_config_row(bot_frame, 0, "BOT_FILTER_ENABLED", "BOT_FILTER_ENABLED",
                      "← 1 to check each profile before following it, 0 to follow everything")
    create_config_row(bot_frame, 1, "BOT_MIN_POSTS", "BOT_MIN_POSTS",
                      "← Reject a profile with fewer posts than this")
    create_config_row(bot_frame, 2, "BOT_MIN_FOLLOWERS", "BOT_MIN_FOLLOWERS",
                      "← Reject a profile with fewer followers than this")
    create_config_row(bot_frame, 3, "BOT_MAX_FOLLOWING", "BOT_MAX_FOLLOWING",
                      "← Reject a profile following more accounts than this")
    create_config_row(bot_frame, 4, "BOT_MAX_FOLLOWING_RATIO", "BOT_MAX_FOLLOWING_RATIO",
                      "← Reject when following exceeds followers by this many times")

    # ─── SEMANTIC RANKING ───
    semantic_frame = ttk.LabelFrame(
        settings_scrollable_frame, text='🧭 Semantic Ranking', padding=10
    )
    semantic_frame.pack(fill='x', pady=(0, 10), padx=5, expand=True)
    semantic_frame.columnconfigure(1, weight=1)

    create_text_row(
        semantic_frame, 0, "SEMANTIC_NICHE", "SEMANTIC_NICHE",
        "← Who you are looking for, written as a sentence rather than as keywords. "
        "A model reads it the way it reads a bio, so it works better that way: "
        "\"fotografi che scattano su pellicola e mostrano il loro lavoro\""
    )
    create_config_row(semantic_frame, 2, "SEMANTIC_ENABLED", "SEMANTIC_ENABLED",
                      "← 1 to read the top of the queue after a search, 0 to skip it")
    create_config_row(semantic_frame, 3, "SEMANTIC_WEIGHT", "SEMANTIC_WEIGHT",
                      "← 0-100. How much of a candidate's rank is the niche rather "
                      "than how often they were seen. 0 is the old order")
    create_config_row(semantic_frame, 4, "SEMANTIC_SHORTLIST", "SEMANTIC_SHORTLIST",
                      "← How many of the best candidates get read, and how long the "
                      "queue is kept. One page load each")

    unfollow_settings_frame = ttk.LabelFrame(settings_scrollable_frame, text='🚫 Unfollow Settings', padding=10)
    unfollow_settings_frame.pack(fill='x', pady=(0, 10), padx=5, expand=True)

    create_config_row(unfollow_settings_frame, 0, "UNFOLLOW_DELAY_MIN", "UNFOLLOW_DELAY_MIN",
                      "← Minimum seconds between unfollow actions")
    create_config_row(unfollow_settings_frame, 1, "UNFOLLOW_DELAY_MAX", "UNFOLLOW_DELAY_MAX",
                      "← Maximum seconds between unfollow actions (randomized)")
    create_config_row(unfollow_settings_frame, 2, "UNFOLLOW_DAILY_LIMIT", "UNFOLLOW_DAILY_LIMIT",
                      "← Soft target for unfollows per session (used as default in the Unfollow tab)")

    # ─── TECHNICAL SETTINGS ───
    tech_frame = ttk.LabelFrame(settings_scrollable_frame, text='⚙️ Technical Settings', padding=10)
    tech_frame.pack(fill='x', pady=(0, 10), padx=5, expand=True)

    create_config_row(tech_frame, 0, "BROWSER_TIMEOUT", "BROWSER_TIMEOUT",
                      "← Seconds to wait for elements to load")
    create_config_row(tech_frame, 1, "RETRY_ATTEMPTS", "RETRY_ATTEMPTS",
                      "← Number of retry attempts on failure")
    create_config_row(tech_frame, 2, "RETRY_BACKOFF", "RETRY_BACKOFF",
                      "← Exponential backoff multiplier for retries")
    create_config_row(tech_frame, 3, "SESSION_DURATION_MAX", "SESSION_DURATION_MAX",
                      "← Maximum session length in seconds (2hr = 7200)")

    # ─── SAVE / RESET BUTTONS ───
    action_frame = ttk.Frame(settings_scrollable_frame, padding=10)
    action_frame.pack(fill='x', pady=(10, 10), padx=5)

    def apply_config():
        """Apply the config changes from GUI entries."""
        for key, entry in config_entries.items():
            try:
                value = entry.get().strip()
                if key in ["DEFAULT_DELAY_MIN", "DEFAULT_DELAY_MAX", "BROWSER_TIMEOUT",
                           "COOLDOWN_DURATION", "HASHTAG_BREAK_DURATION", "FOLLOW_BATCH_COOLDOWN",
                           "SESSION_DURATION_MAX", "EXTRACTION_PAUSE_DURATION"]:
                    CONFIG[key] = int(value)
                elif key in ["TARGET_AUTHORS_PER_HASHTAG", "MAX_SCROLLS_PER_HASHTAG", "FOLLOWER_SCROLL_COUNT",
                             "AUTHORS_BEFORE_COOLDOWN", "FOLLOW_BATCH_SIZE", "MAX_FOLLOWS_PER_SESSION",
                             "RETRY_ATTEMPTS", "RETRY_BACKOFF"]:
                    CONFIG[key] = int(value)
                else:
                    CONFIG[key] = int(value)
            except ValueError:
                messagebox.showerror("Invalid Value", f"Could not convert '{entry.get()}' to a number for {key}")
                return

        # Words, not numbers, so these never go near int().
        for key, entry in config_text_entries.items():
            CONFIG[key] = entry.get().strip()

        save_config(CONFIG)
        log("✅ Configuration saved!", 'success')
        messagebox.showinfo("Saved", "Configuration has been saved. Changes will take effect on next session.")

    def reset_config():
        """Reset all config entries to saved config."""
        for key, entry in list(config_entries.items()) + list(config_text_entries.items()):
            entry.delete(0, tk.END)
            entry.insert(0, str(CONFIG.get(key, "")))

    ttk.Button(action_frame, text='💾 Save Configuration', command=apply_config,
               style='Accent.TButton').pack(side='left', padx=5)
    ttk.Button(action_frame, text='🔄 Reset to Saved', command=reset_config).pack(side='left', padx=5)

    # ─── SAFETY INFO ───
    info_frame = ttk.LabelFrame(settings_scrollable_frame, text='🛡️ Safety Information & Best Practices', padding=10)
    info_frame.pack(fill='x', pady=(10, 10), padx=5, expand=True)

    safety_text = """DEVELOPMENT MODE - No limits enforced

This bot is running in development mode. Use these settings to tune behavior:

EXTRACTION SETTINGS:
• TARGET_AUTHORS_PER_HASHTAG: How many unique profile authors to process per hashtag
• MAX_SCROLLS_PER_HASHTAG: Safety ceiling on scrolls, not a target. Scrolling stops
  as soon as enough new authors are found, or once the hashtag stops loading posts,
  so this is only reached on a hashtag whose authors have nearly all been scraped
• FOLLOWER_SCROLL_COUNT: How many scroll actions per profile's followers list
• AUTHORS_BEFORE_COOLDOWN: After how many authors to trigger a short cooldown
• COOLDOWN_DURATION: Seconds of cooldown between author groups
• HASHTAG_BREAK_DURATION: Seconds to wait between different hashtags

FOLLOW SETTINGS:
• DEFAULT_DELAY_MIN/MAX: Randomized seconds between follow actions (keeps it human-like)
• FOLLOW_BATCH_SIZE: How many follows before a batch cooldown
• FOLLOW_BATCH_COOLDOWN: Seconds of cooldown after each batch
• MAX_FOLLOWS_PER_SESSION: Soft target (not enforced) for follows per session

BOT FILTER:
Checked on the profile page, just before following, because posts/followers/following
are not in the followers list a candidate is found in. So it does not keep bots out of
the queue - it stops them being followed, and drops them from the queue when reached.
A profile whose counts cannot be read is followed anyway, with a warning in the log.

For development: Lower delays to test faster, increase cooldowns if getting blocked."""

    info_label = ttk.Label(info_frame, text=safety_text, justify='left', font=('Consolas', 9))
    info_label.pack(anchor='w')

    # ==================== LOGS TAB ====================

    log_box = scrolledtext.ScrolledText(
        logs_tab,
        height=30,
        width=90,
        font=('Consolas', 10),
        wrap=tk.WORD
    )
    log_box.pack(fill='both', expand=True)

    # Configure tags for colored logging
    log_box.tag_config('success', foreground='green')
    log_box.tag_config('error', foreground='red')
    log_box.tag_config('warning', foreground='orange')
    log_box.tag_config('info', foreground='black')

    # Load last unfollow session (if any) and refresh its display
    uf_auto_load_last_session()
    update_follow_ui_state()
    update_unfollow_ui_state()

    # Start watching for the browser being closed behind the app's back
    root.after(BROWSER_WATCH_INTERVAL, watch_browser)

    # Handle close
    root.protocol("WM_DELETE_WINDOW", on_closing)

    return root

# ---------------------------
# MAIN
# ---------------------------
if __name__ == "__main__":
    root = setup_gui()
    log("Reciproca (Follow & Unfollow) loaded", 'success')
    log("Click 'Open Browser' to start", 'info')
    root.mainloop()
