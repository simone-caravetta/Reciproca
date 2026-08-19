"""
Reciproca - configuration and application paths.

Holds the CONFIG defaults, the on-disk config round trip, and every path the app
reads and writes. Imported first by the package, so it must not import anything
from reciproca itself.
"""

import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

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

    # Author follow - follow scraped authors whose following/followers ratio
    # looks like they would follow back. Checked on the author's page, before
    # their followers are extracted.
    "AUTHOR_FOLLOW_ENABLED": 1,             # 1 follows the author of a scraped post
    "AUTHOR_MAX_FOLLOWERS_RATIO": 5,        # Reject followers > this many x following

    # Semantic ranking - how close a candidate's profile reads to the niche you
    # describe, scored after a search on the strongest candidates it found.
    "SEMANTIC_ENABLED": 1,                  # 0 skips the scoring pass entirely
    "SEMANTIC_WEIGHT": 60,                  # 0-100. See combined_rank() for what it buys
    "SEMANTIC_TOP_K": 200,                  # Candidates kept after a search, and read
    "SEMANTIC_READ_DELAY": 1,               # Seconds between profiles, 0 for none
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
    anchors to the folder holding this package, which is the parent of the folder
    this file lives in. Both give a stable location that matches where the user
    thinks the app lives.
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
ACCOUNT_USERNAME_FILE = data_path("account_username.json")
LOG_FILE = data_path("follow_bot.log")
CHROME_PROFILE_DIR = data_path("chrome_profile")

# Written by the `stop` command of another process, so a running session stops at
# its next pause() checkpoint. begin_session() deletes a stale flag.
STOP_FLAG_FILE = data_path("stop.flag")


DEFAULT_CONFIG = {
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
    "AUTHOR_FOLLOW_ENABLED": 1,
    "AUTHOR_MAX_FOLLOWERS_RATIO": 5,
    "SEMANTIC_ENABLED": 1,
    "SEMANTIC_WEIGHT": 60,
    "SEMANTIC_TOP_K": 200,
    "SEMANTIC_READ_DELAY": 1,
    "SEMANTIC_NICHE": "",
    "UNFOLLOW_DELAY_MIN": 15,
    "UNFOLLOW_DELAY_MAX": 30,
    "UNFOLLOW_DAILY_LIMIT": 20,
}


def load_config():
    """Load config from file, fall back to defaults if not found."""
    default_config = DEFAULT_CONFIG

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
CONFIG = load_config()
