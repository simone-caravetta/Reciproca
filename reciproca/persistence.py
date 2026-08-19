"""
Reciproca - JSON persistence: follow history, frequencies, hashtags, account
name, and the unfollow session/progress files.
"""

import json
import os
import re
from collections import Counter
from datetime import datetime

from selenium.common.exceptions import WebDriverException

from reciproca import config, state, hooks
from reciproca.logging_sink import log, logger


def log_followed_user(username, status="success"):
    """Log a followed user to history."""
    try:
        history = {}
        if os.path.exists(config.FOLLOWED_FILE):
            with open(config.FOLLOWED_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)

        history[username] = {
            "date": datetime.now().isoformat(),
            "status": status
        }

        with open(config.FOLLOWED_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error logging followed user: {e}")

def is_already_followed(username):
    """Check if user is in followed history."""
    try:
        if os.path.exists(config.FOLLOWED_FILE):
            with open(config.FOLLOWED_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
                if isinstance(history, dict):
                    return username in history
    except Exception as e:
        logger.debug(f"Error checking followed history for {username}: {e}")
    return False

def load_frequencies():
    """Load user frequencies from file."""
    try:
        if os.path.exists(config.FREQUENCIES_FILE):
            with open(config.FREQUENCIES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return Counter(data)
    except Exception as e:
        logger.error(f"Error loading frequencies: {e}")
    return Counter()

def save_frequencies(frequencies):
    """Save user frequencies to file."""
    try:
        with open(config.FREQUENCIES_FILE, 'w', encoding='utf-8') as f:
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
        if os.path.exists(config.AUTHORS_FILE):
            with open(config.AUTHORS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.error(f"Error loading author history: {e}")
    return {}


def save_author_history(history):
    """Save the scraped-authors history."""
    try:
        with open(config.AUTHORS_FILE, 'w', encoding='utf-8') as f:
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
        if os.path.exists(config.HASHTAGS_FILE):
            with open(config.HASHTAGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        logger.error(f"Error loading hashtags: {e}")
    return None

def save_hashtags(hashtags):
    """Save hashtags to file."""
    try:
        with open(config.HASHTAGS_FILE, 'w', encoding='utf-8') as f:
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
    if not os.path.exists(config.UNFOLLOW_PROGRESS_FILE):
        state.uf_progress = {"processed": [], "unfollowed": [], "skipped": []}
    else:
        try:
            with open(config.UNFOLLOW_PROGRESS_FILE, 'r', encoding='utf-8') as f:
                state.uf_progress = json.load(f)
        except Exception as e:
            logger.error(f"Error loading unfollow progress: {e}")
            state.uf_progress = {"processed": [], "unfollowed": [], "skipped": []}

def uf_save_progress():
    """Save unfollow progress to file."""
    try:
        with open(config.UNFOLLOW_PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(state.uf_progress, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving unfollow progress: {e}")

def uf_load_session():
    """The saved unfollow session: which export files, and whose account."""
    try:
        if os.path.exists(config.UNFOLLOW_SESSION_FILE):
            with open(config.UNFOLLOW_SESSION_FILE, 'r', encoding='utf-8') as f:
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
        with open(config.UNFOLLOW_SESSION_FILE, 'w', encoding='utf-8') as f:
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
    if state.driver is None:
        return None
    try:
        cookie = state.driver.get_cookie('ds_user_id')
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
    return config.data_path(f"unfollow_progress_{safe_id}.json")


def save_login_username(username):
    """Remember which account logged into the browser."""
    try:
        with open(config.ACCOUNT_USERNAME_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "username": username,
                "saved_at": datetime.now().isoformat(timespec='seconds'),
            }, f, indent=2, ensure_ascii=False)
        log(f"👤 Username captured from the login: {username}", 'success')
        hooks.update_account_label()
    except Exception as e:
        logger.error(f"Error saving username: {e}")


def load_account_username():
    """The username saved from the last manual login, or None."""
    try:
        with open(config.ACCOUNT_USERNAME_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get("username")
    except Exception:
        return None
