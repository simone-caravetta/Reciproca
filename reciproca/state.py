"""
Reciproca - shared runtime state.

Every module that needs the browser, the session flags or the stats reads and
writes these through `from reciproca import state` + `state.<name>`. Never
`from reciproca.state import driver` - that would bind the value at import time
and quietly diverge when the attribute is rebound.

The stats objects are created with no on_update callback; hooks.attach() wires
them to whatever UI is present (GUI handlers, or nothing headless).
"""

import threading
import time
from collections import Counter

from reciproca.logging_sink import logger


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
stats = SessionStats()
driver = None
# Set while the browser is being opened. Both tabs have an Open Browser button,
# and without this a click on each would start two Chrome instances.
browser_opening = threading.Event()
# Set while a follow or unfollow session owns the browser. Both drive the same
# Selenium session and the same window, so only one may run at a time.
session_running = threading.Event()
stop_requested = threading.Event()

# Stopping the search does not mean skipping the scoring that follows it, so the
# scoring watches a flag of its own. Stop sets both; the scoring pass clears its own
# as it starts, which is what lets it run on a search that was cut short. A second
# press then stops the scoring too, and stop_requested is never cleared behind the
# user's back - so a search stopped by hand stays stopped, and so does the following
# that would otherwise have come after it.
scoring_stop = threading.Event()
active_threads = []
# True once the browser holds a session cookie, i.e. the manual login went
# through. Start Following stays disabled until then - a browser on the login
# page cannot follow anyone.
login_completed = False
# Live extraction tracking
live_extracted_users = []  # Track users as they're extracted
live_frequencies = Counter()  # Track frequencies in real-time
# Frequencies the queue is ranked by. Read lazily via ranking_frequencies() so
# startup does not touch the disk before the GUI exists.
last_scrape_frequencies = None
# Usernames currently drawn in the queue listbox, row by row
displayed_queue_usernames = []

# Unfollow state
uf_stats = SessionStats()
uf_followers = set()
uf_following = set()
uf_non_followers = []
uf_progress = {}
