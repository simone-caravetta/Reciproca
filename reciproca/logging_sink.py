"""
Reciproca - logging sink.

The one place log() lives, decoupled from any GUI: it appends to a recent buffer,
fans out to registered sinks (the GUI registers one that writes the log widget),
and writes to the Python logger. The signature has not changed, so every call
site in the core is unchanged.

Import-time side effects (as in the monolith): logging.basicConfig to
follow_bot.log + stderr, and the urllib3 pool-noise filter.
"""

import logging
import time
from collections import deque
from datetime import datetime

from reciproca import config

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)


class _DropPoolFullWarning(logging.Filter):
    """Drop urllib3's "Connection pool is full" noise.

    Selenium keeps one keep-alive connection to chromedriver (pool size 1).
    When two commands overlap - the browser watcher's probe against a
    command, or two probes at once - the pool discards the busy connection
    and opens a new one, which is harmless. The "Retrying" warnings, which
    mean a real connection failure, stay.
    """

    def filter(self, record):
        return "Connection pool is full" not in record.getMessage()


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_DropPoolFullWarning())

logger = logging.getLogger(__name__)

# The last log lines, for the CLI's `logs` command and the MCP server's
# logs_tail tool. (timestamp, level, message)
RECENT = deque(maxlen=1000)

# GUI / other frontends register a sink; each is called with (full_msg, level).
_sinks = []


def register_sink(fn):
    """Add a sink. Idempotent: the same function is registered once."""
    if fn not in _sinks:
        _sinks.append(fn)


def clear_sinks():
    """Drop every registered sink (used by tests and by UI teardown)."""
    _sinks.clear()


def log(msg, level='info'):
    """Log message to any registered sink and to the logger."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    full_msg = f"{timestamp} | {msg}\n"

    RECENT.append((timestamp, level, msg))

    for sink in list(_sinks):
        try:
            sink(full_msg, level)
        except Exception:
            # A broken sink must never take the session down with it.
            pass

    # Also log to file
    log_func = getattr(logger, level, logger.info)
    log_func(msg)
