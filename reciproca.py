"""
Reciproca
Unified GUI tool: queue-based/hashtag follow, unfollow of non-followers
(from an Instagram data export), rate limit detection, and persistent state.
"""

import json
import logging
import functools
import os
import re
import threading
import time
import random
import tkinter as tk
from collections import Counter
from datetime import datetime
from tkinter import ttk, scrolledtext, messagebox, filedialog

from selenium import webdriver
from selenium.common.exceptions import (
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------------
# CONFIGURATION
# ---------------------------
CONFIG = {
    # Extraction settings - CONSERVATIVE for background operation
    "TARGET_AUTHORS_PER_HASHTAG": 10,      # Low: Prevents detection from rapid profile switching
    "MAX_SCROLLS": 3,                       # Low: Limits scroll actions per hashtag
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

    # Unfollow delays - same conservative philosophy as follow
    "UNFOLLOW_DELAY_MIN": 15,               # Minimum seconds between unfollows
    "UNFOLLOW_DELAY_MAX": 30,               # Maximum seconds between unfollows
    "UNFOLLOW_DAILY_LIMIT": 20,             # Soft daily target for unfollows
}

# Files for persistence
QUEUE_FILE = "follow_queue.json"
FOLLOWED_FILE = "followed_history.json"
FREQUENCIES_FILE = "user_frequencies.json"
HASHTAGS_FILE = "hashtags.json"
CONFIG_FILE = "bot_config.json"
UNFOLLOW_PROGRESS_FILE = "unfollow_progress.json"
UNFOLLOW_SESSION_FILE = "unfollow_last_session.json"

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

# Labels on a post dialog's close button. Matched via XPath contains(), which is
# case-sensitive, so these keep their original capitalization.
CLOSE_BUTTON_LABELS = ("Close", "Chiudi")

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

# ---------------------------
# CONFIG FILE MANAGEMENT
# ---------------------------
def load_config():
    """Load config from file, fall back to defaults if not found."""
    default_config = {
        "TARGET_AUTHORS_PER_HASHTAG": 10,
        "MAX_SCROLLS": 3,
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

def add_to_queue(usernames):
    """Add usernames to queue, avoiding duplicates and already followed users."""
    queue = load_queue()
    # Add only new usernames (not already in queue and not already followed)
    existing_in_queue = set()

    # Extract usernames from queue (handle both list and dict formats)
    if queue and isinstance(queue[0], dict):
        # New format with metadata
        existing_in_queue = {item['username'] for item in queue if isinstance(item, dict) and 'username' in item}
    else:
        # Old format - just usernames
        existing_in_queue = set(queue)

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

    save_queue(queue)
    return len(new_users), len(queue)

def remove_from_queue(username):
    """Remove a username from queue."""
    queue = load_queue()
    if not queue:
        return False

    # Handle both old format (list of usernames) and new format (list of dicts)
    if queue and isinstance(queue[0], dict):
        # New format
        queue = [item for item in queue if item.get('username') != username]
    else:
        # Old format
        if username in queue:
            queue.remove(username)

    save_queue(queue)
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

    # Handle both old format (list of usernames) and new format (list of dicts)
    is_new_format = queue and isinstance(queue[0], dict)

    # Remove duplicates while preserving order
    seen = set()
    unique_queue = []
    for item in queue:
        username = item['username'] if is_new_format else item
        if username not in seen:
            seen.add(username)
            unique_queue.append(item)

    # Remove already followed users
    validated_queue = []
    removed_count = 0
    for item in unique_queue:
        username = item['username'] if is_new_format else item
        if is_already_followed(username):
            removed_count += 1
            logger.debug(f"Queue validation: Removed already followed user {username}")
        else:
            validated_queue.append(item)

    if len(validated_queue) != len(queue):
        save_queue(validated_queue)
        logger.info(f"Queue validated: Removed {len(queue) - len(validated_queue)} entries ({removed_count} already followed, {len(queue) - len(unique_queue)} duplicates)")

    return len(queue) - len(validated_queue), len(validated_queue)

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

        with open(UNFOLLOW_SESSION_FILE, 'w', encoding='utf-8') as f:
            json.dump({"followers_file": f1, "following_file": f2}, f)

        uf_load_progress()
        update_unfollow_ui_state()
        refresh_unfollow_display()

    except Exception as e:
        messagebox.showerror("Error", f"Invalid files:\n{e}")
        logger.exception("Error loading unfollow JSON files")

def uf_auto_load_last_session():
    """Auto-reload the last followers/following.json pair on startup."""
    global uf_followers, uf_following, uf_non_followers

    if not os.path.exists(UNFOLLOW_SESSION_FILE):
        return

    try:
        with open(UNFOLLOW_SESSION_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        f1 = data.get("followers_file")
        f2 = data.get("following_file")

        if not f1 or not f2 or not os.path.exists(f1) or not os.path.exists(f2):
            log("⚠️ Saved unfollow session found but JSON files are missing, reload them manually", 'warning')
            return

        uf_followers = uf_load_followers(f1)
        uf_following = uf_load_following(f2)
        uf_non_followers = list(uf_following - uf_followers)

        log(f"🔄 Unfollow session reloaded: {len(uf_non_followers)} non-followers", 'info')
        uf_load_progress()
        update_unfollow_ui_state()

    except Exception as e:
        logger.debug(f"Auto-load unfollow session error: {e}")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("follow_bot.log"),
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
stop_requested = threading.Event()
active_threads = []
# Live extraction tracking
live_extracted_users = []  # Track users as they're extracted
live_frequencies = Counter()  # Track frequencies in real-time

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

def open_browser():
    """Open Chrome browser with persistent profile."""
    global driver

    try:
        log("🌐 Initializing Chrome...", 'info')

        options = webdriver.ChromeOptions()
        profile_path = os.path.abspath("chrome_profile")
        options.add_argument(f"--user-data-dir={profile_path}")
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

        # Enable start button
        start_btn.config(state='normal')
        browser_btn.config(state='disabled')
        update_unfollow_ui_state()

    except WebDriverException as e:
        log(f"❌ Browser error: {e}", 'error')
        messagebox.showerror("Browser Error", str(e))

def stop_bot():
    """Request graceful stop."""
    stop_requested.set()
    log("⏹️ Stop requested, finishing current operation...", 'warning')
    stop_btn.config(state='disabled')

    # Save any already extracted users to queue if scraping was in progress
    # Only do this if we're actually in extraction mode, not during follow
    try:
        # Check if we have live extraction data (indicating extraction was in progress)
        global live_extracted_users
        if live_extracted_users:
            # Reload frequencies from file to get the latest incremental data
            current_freqs = load_frequencies()
            if current_freqs:
                ranked_users = [u for u, _ in current_freqs.most_common()]
                if ranked_users:
                    new_count, total_count = add_to_queue(ranked_users)
                    log(f"💾 Saved {new_count} users to queue (stop detected during extraction)", 'success')
                    # Update global frequencies
                    global last_scrape_frequencies
                    last_scrape_frequencies = current_freqs
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

def close_post():
    """Close post dialog."""
    try:
        # Try localized labels first, then locale-independent fallbacks
        selectors = [
            f"//div[@role='dialog']//button[contains(., '{label}')]"
            for label in CLOSE_BUTTON_LABELS
        ] + [
            "//div[@role='dialog']//button//*[name()='svg']",
            "//div[@role='dialog']//button[@aria-label='Close']",
        ]

        for selector in selectors:
            buttons = driver.find_elements(By.XPATH, selector)
            for btn in buttons:
                try:
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.5)
                    return
                except:
                    continue

    except Exception as e:
        logger.debug(f"Close post error: {e}")

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
                count_text = parent.text.split()[0].replace(",", "")
                log(f"👥 Opening followers popup ({count_text} followers)...")
            except:
                log(f"👥 Opening followers popup...")

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

        # Final extraction with improved filtering
        try:
            usernames = driver.execute_script("""
                let dialog = document.querySelector("div[role='dialog']");
                if (!dialog) return [];

                let links = dialog.querySelectorAll('a[href^="/"]');
                let usernames = [];

                links.forEach(link => {
                    let href = link.getAttribute('href');
                    if (href) {
                        let match = href.match(/^\\/([^/]+)\\/?$/);
                        if (match) {
                            let username = match[1];
                            // Enhanced filtering
                            if (username &&
                                username.length > 1 &&
                                !['p', 'explore', 'accounts', 'direct', 'emails', 'reels', 'stories', 'help', 'about', 'blog', 'jobs', 'privacy', 'terms', 'locations', 'language', 'developers', 'settings'].includes(username) &&
                                !username.includes('.') &&
                                !username.includes('?') &&
                                !username.startsWith('__') &&
                                !username.startsWith('dm_')) {
                                usernames.push(username);
                            }
                        }
                    }
                });
                return usernames;
            """)

            # Deduplicate while preserving order
            seen = set()
            unique_users = []
            for u in usernames:
                if u not in seen:
                    seen.add(u)
                    unique_users.append(u)

            log(f"📊 Extracted {len(unique_users)} unique users")

            if len(unique_users) < 5:
                logger.warning(f"Low user count: {len(unique_users)}")

            # Filter out already followed users BEFORE returning
            filtered_users = []
            for user in unique_users:
                if not is_already_followed(user):
                    filtered_users.append(user)
                else:
                    logger.debug(f"Filtered out already followed user: {user}")

            logger.info(f"Filtered {len(unique_users) - len(filtered_users)} already followed users")
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

    # Extract usernames from queue items (handle both formats)
    usernames = []
    for item in users_to_follow:
        if isinstance(item, dict):
            usernames.append(item['username'])
        else:
            usernames.append(item)

    log(f"📋 Following from queue: {len(usernames)} users available")
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

        if driver is None:
            log("❌ Browser not open!", 'error')
            messagebox.showerror("Error", "Please open the browser first (Auto Follow tab)")
            return

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

        uf_start_btn.config(state='disabled')
        uf_stop_btn.config(state='normal')
        start_btn.config(state='disabled')  # Prevent concurrent follow session on same driver
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
        uf_start_btn.config(state='normal')
        uf_stop_btn.config(state='disabled')
        start_btn.config(state='normal')
        refresh_unfollow_display()
        if not stop_requested.is_set():
            reset_unfollow_progress()


def run_unfollow():
    """Start unfollow in background thread."""
    thread = threading.Thread(target=unfollow_logic, daemon=True)
    thread.start()
    active_threads.append(thread)


def update_unfollow_ui_state():
    """Enable/disable the unfollow Start button based on browser + data readiness."""
    try:
        ready = driver is not None and (len(uf_non_followers) > 0 or os.path.exists(UNFOLLOW_PROGRESS_FILE))
        uf_start_btn.config(state='normal' if ready else 'disabled')
        if ready:
            uf_data_label.config(text=f"🟢 {len(uf_non_followers)} non-followers ready")
        else:
            uf_data_label.config(text="🟡 Open the browser and load the JSON files")
    except Exception as e:
        logger.debug(f"update_unfollow_ui_state error: {e}")


def refresh_unfollow_display():
    """Refresh the unfollow tab's summary info."""
    try:
        uf_load_progress()
        remaining = len([u for u in uf_non_followers if u not in uf_progress.get("processed", [])])
        uf_data_label.config(
            text=f"🟢 {len(uf_non_followers)} non-followers | {remaining} to process | "
                 f"{len(uf_progress.get('unfollowed', []))} already removed"
        )
    except Exception as e:
        logger.debug(f"refresh_unfollow_display error: {e}")


def reset_unfollow_app():
    """Reset unfollow progress/session (does not touch follow queue/history)."""
    global uf_followers, uf_following, uf_non_followers, uf_progress

    if messagebox.askyesno("Confirm", "Reset unfollow progress and session?"):
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
        log("🔄 Unfollow reset complete", 'info')


# Global variable to store user frequencies from last scrape - load from file on startup
last_scrape_frequencies = load_frequencies()

def scrape_and_fill_queue(hashtags, add_to_queue_limit=0):
    """Scrape users from hashtags and optionally add to queue."""
    global last_scrape_frequencies, live_extracted_users, live_frequencies

    # Reset live extraction tracking for new scrape session
    live_extracted_users = []
    live_frequencies = Counter()

    all_users = []
    current_frequencies = Counter()  # Track frequencies incrementally
    total_authors_processed = 0
    throttle_cooldown_count = 0  # Track consecutive low-extraction authors

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

        visited_authors = set()
        visited_posts = set()
        scroll_count = 0
        author_count = 0

        driver.get(f"https://www.instagram.com/explore/tags/{kw}/")
        time.sleep(random.uniform(2.5, 4.0))  # Randomized initial wait

        if check_rate_limit(driver):
            log("⚠️ Rate limit detected during scraping - continuing anyway", 'warning')
            # Don't break - just warn and continue

        while (len(visited_authors) < CONFIG["TARGET_AUTHORS_PER_HASHTAG"]
               and scroll_count < CONFIG["MAX_SCROLLS"]
               and not stop_requested.is_set()):

            posts = driver.find_elements(
                By.XPATH,
                "//a[contains(@href, '/p/')]"
            )

            for post in posts[:6]:
                # FIX: Check if we've reached target BEFORE processing each post
                if len(visited_authors) >= CONFIG["TARGET_AUTHORS_PER_HASHTAG"]:
                    break

                if stop_requested.is_set():
                    break

                try:
                    href = post.get_attribute("href")
                    if not href or href in visited_posts:
                        continue

                    visited_posts.add(href)

                    driver.execute_script(
                        "arguments[0].scrollIntoView();",
                        post
                    )
                    time.sleep(random.uniform(0.5, 1.5))  # Randomized scroll delay

                    driver.execute_script("arguments[0].click();", post)
                    time.sleep(random.uniform(1.5, 2.5))  # Randomized after-click wait

                    profile_url = get_author_profile()
                    if not profile_url:
                        close_post()
                        continue

                    username = profile_url.rstrip("/").split("/")[-1]

                    if username in visited_authors:
                        log(f"⏭️ Skip duplicate: {username}", 'warning')
                        close_post()
                        continue

                    visited_authors.add(username)
                    author_count += 1
                    total_authors_processed += 1
                    log(f"👤 Author {author_count}/{CONFIG['TARGET_AUTHORS_PER_HASHTAG']}: {username}")

                    driver.execute_script(
                        "window.open(arguments[0]);",
                        profile_url
                    )
                    driver.switch_to.window(driver.window_handles[-1])
                    time.sleep(random.uniform(2.0, 3.5))  # Randomized window switch delay

                    users = []
                    if open_followers_popup():
                        users = extract_users_from_followers(
                            current_hashtag=kw,
                            author_num=author_count,
                            total_authors=CONFIG["TARGET_AUTHORS_PER_HASHTAG"],
                            author_name=username
                        )
                        all_users.extend(users)

                        # Update frequencies incrementally for this author
                        current_frequencies.update(users)
                        last_scrape_frequencies = current_frequencies
                        save_frequencies(current_frequencies)

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

                    driver.close()
                    driver.switch_to.window(driver.window_handles[0])
                    close_post()

                    # Anti-throttling: frequent cooldowns for safety
                    if author_count % CONFIG["AUTHORS_BEFORE_COOLDOWN"] == 0:
                        cooldown = CONFIG["COOLDOWN_DURATION"] + random.uniform(0, 5)
                        log(f"🛡️ Safety cooldown: {cooldown:.0f}s after {author_count} authors...", 'info')
                        time.sleep(cooldown)

                    # Random delay between authors - safer range
                    delay = random.uniform(4, 8)
                    time.sleep(delay)

                except Exception as e:
                    log(f"❌ Post error: {e}", 'error')

            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(random.uniform(2, 3.5))  # Randomized scroll delay
            scroll_count += 1

        log(f"🎯 Authors collected: {len(visited_authors)}")

        # Anti-throttling: longer break between hashtags
        if hashtag_idx < total_hashtags:
            break_time = CONFIG["HASHTAG_BREAK_DURATION"] + random.uniform(0, 10)
            log(f"☕ Safety break between hashtags: {break_time:.0f}s...", 'info')
            time.sleep(break_time)

    # Rank users by frequency (use the incrementally built counter)
    ranked_users = [u for u, _ in current_frequencies.most_common()]

    # Ensure global frequencies are up to date
    last_scrape_frequencies = current_frequencies
    save_frequencies(current_frequencies)

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

        if driver is None:
            log("❌ Browser not open!", 'error')
            messagebox.showerror("Error", "Please open browser first")
            return

        # Update UI
        start_btn.config(state='disabled')
        stop_btn.config(state='normal')
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
            top_freq = last_scrape_frequencies.get(top_score, 0) if top_score else 0

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
        start_btn.config(state='normal')
        stop_btn.config(state='disabled')
        refresh_queue_display()  # Update queue display
        update_live_extraction_display()  # Final update of live extraction
        if not stop_requested.is_set():
            reset_progress()

def run_follow():
    """Start follow in background thread."""
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
    queue_listbox.delete(0, tk.END)
    # Validate queue before displaying to ensure consistency
    validate_queue()
    queue = load_queue()

    # Extract usernames from queue (handle both formats)
    if queue and isinstance(queue[0], dict):
        # New format
        queue_usernames = [item['username'] for item in queue]
    else:
        # Old format
        queue_usernames = queue

    # Get frequencies from last scrape if available, otherwise load from file
    global last_scrape_frequencies
    frequencies = last_scrape_frequencies
    if not frequencies:
        frequencies = load_frequencies()
        last_scrape_frequencies = frequencies

    # Sort queue by frequency (highest first) if frequencies exist
    if frequencies and queue_usernames:
        # Create list of (user, frequency) tuples
        queue_with_freq = [(user, frequencies.get(user, 0)) for user in queue_usernames]
        # Sort by frequency descending, then by username
        queue_with_freq.sort(key=lambda x: (-x[1], x[0]))
    else:
        queue_with_freq = [(user, 0) for user in queue_usernames]

    # Display users with their rank/frequency
    displayed_count = 0
    for user, freq in queue_with_freq[:100]:  # Show first 100
        if freq > 0:
            display_text = f"[{freq}] {user}"
        else:
            display_text = user
        queue_listbox.insert(tk.END, display_text)
        displayed_count += 1

    if len(queue_with_freq) > 100:
        queue_listbox.insert(tk.END, f"... and {len(queue_with_freq) - 100} more")

    queue_count_label.config(text=f"Queue: {len(queue_usernames)} users")

    # Also update main tab info
    try:
        main_queue_info.config(text=f"Queue: {len(queue_usernames)} users waiting")
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

        # Get the sorted queue with frequencies
        queue = load_queue()
        global last_scrape_frequencies
        frequencies = last_scrape_frequencies
        if not frequencies:
            frequencies = load_frequencies()
            last_scrape_frequencies = frequencies

        # Extract usernames from queue (handle both formats)
        if queue and isinstance(queue[0], dict):
            queue_usernames = [item['username'] for item in queue]
        else:
            queue_usernames = queue

        if frequencies and queue_usernames:
            queue_with_freq = [(user, frequencies.get(user, 0)) for user in queue_usernames]
            queue_with_freq.sort(key=lambda x: (-x[1], x[0]))
        else:
            queue_with_freq = [(user, 0) for user in queue_usernames]

        if idx < len(queue_with_freq):
            username = queue_with_freq[idx][0]
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
                    # Handle both old format (list of usernames) and new format (list of dicts)
                    if data and isinstance(data[0], dict):
                        usernames = [item['username'] for item in data if isinstance(item, dict) and 'username' in item]
                    else:
                        usernames = data
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
        # Extract usernames for export (handle both formats)
        if queue and isinstance(queue[0], dict):
            usernames = [item['username'] for item in queue]
        else:
            usernames = queue

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
        filename = f"follow_logs_{datetime.now():%Y%m%d_%H%M%S}.txt"
        with open(filename, 'w') as f:
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

    # Tab 1: Main
    main_tab = ttk.Frame(notebook, padding=10)
    notebook.add(main_tab, text='🎯 Auto Follow')

    # Tab 2: Unfollow
    unfollow_tab = ttk.Frame(notebook, padding=10)
    notebook.add(unfollow_tab, text='🚫 Unfollow')

    # Tab 3: Queue
    queue_tab = ttk.Frame(notebook, padding=10)
    notebook.add(queue_tab, text='📋 Follow Queue')

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

    mode_var = tk.StringVar(value='queue')  # Default to queue mode

    ttk.Radiobutton(
        mode_frame,
        text='📋 Follow from Queue (safe - uses saved list)',
        variable=mode_var,
        value='queue'
    ).pack(anchor='w', pady=2)

    ttk.Radiobutton(
        mode_frame,
        text='🔍 Deep Search (find new users via hashtags)',
        variable=mode_var,
        value='search'
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
        command=open_browser,
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
    ).pack(side='left', padx=(0, 10))

    uf_data_label = ttk.Label(
        uf_data_btn_frame,
        text="🟡 Open the browser and load the JSON files",
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

    ttk.Button(
        uf_control_frame,
        text='🔄 Reset',
        command=reset_unfollow_app,
        width=15
    ).pack(side='left', padx=5)

    ttk.Label(
        unfollow_tab,
        text="Note: uses the same browser/login as the 'Auto Follow' tab. Open the browser there before starting.",
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

    # Queue info
    queue_info_frame = ttk.LabelFrame(queue_tab, text='Queue Info', padding=10)
    queue_info_frame.pack(fill='x', pady=(0, 10))

    ttk.Label(
        queue_info_frame,
        text='The queue stores users to follow across sessions.\n'
             '• Users are removed from queue after being followed\n'
             '• Use "Deep Search" mode to find users and add them to queue\n'
             '• Use "Follow from Queue" mode to safely follow users over time',
        justify='left'
    ).pack(anchor='w')

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

    # ─── EXTRACTION SETTINGS ───
    extraction_frame = ttk.LabelFrame(settings_scrollable_frame, text='🔍 Extraction Settings', padding=10)
    extraction_frame.pack(fill='x', pady=(0, 10), padx=5, expand=True)

    create_config_row(extraction_frame, 0, "TARGET_AUTHORS_PER_HASHTAG", "TARGET_AUTHORS_PER_HASHTAG",
                      "← Number of unique authors to process per hashtag")
    create_config_row(extraction_frame, 1, "MAX_SCROLLS", "MAX_SCROLLS",
                      "← Maximum scroll actions per hashtag page")
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
                elif key in ["TARGET_AUTHORS_PER_HASHTAG", "MAX_SCROLLS", "FOLLOWER_SCROLL_COUNT",
                             "AUTHORS_BEFORE_COOLDOWN", "FOLLOW_BATCH_SIZE", "MAX_FOLLOWS_PER_SESSION",
                             "RETRY_ATTEMPTS", "RETRY_BACKOFF"]:
                    CONFIG[key] = int(value)
                else:
                    CONFIG[key] = int(value)
            except ValueError:
                messagebox.showerror("Invalid Value", f"Could not convert '{entry.get()}' to a number for {key}")
                return

        save_config(CONFIG)
        log("✅ Configuration saved!", 'success')
        messagebox.showinfo("Saved", "Configuration has been saved. Changes will take effect on next session.")

    def reset_config():
        """Reset all config entries to saved config."""
        for key, entry in config_entries.items():
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
• MAX_SCROLLS: Maximum scroll actions per hashtag page
• FOLLOWER_SCROLL_COUNT: How many scroll actions per profile's followers list
• AUTHORS_BEFORE_COOLDOWN: After how many authors to trigger a short cooldown
• COOLDOWN_DURATION: Seconds of cooldown between author groups
• HASHTAG_BREAK_DURATION: Seconds to wait between different hashtags

FOLLOW SETTINGS:
• DEFAULT_DELAY_MIN/MAX: Randomized seconds between follow actions (keeps it human-like)
• FOLLOW_BATCH_SIZE: How many follows before a batch cooldown
• FOLLOW_BATCH_COOLDOWN: Seconds of cooldown after each batch
• MAX_FOLLOWS_PER_SESSION: Soft target (not enforced) for follows per session

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
    update_unfollow_ui_state()

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
