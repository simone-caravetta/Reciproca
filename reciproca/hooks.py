"""
Reciproca - UI-neutral delegation hooks.

Every GUI-facing call the core makes goes through a same-name delegator here,
with the same signature it always had, so call sites are unchanged. With no GUI
attached the delegators are no-ops and notify_user falls back to logging.

setup_gui() (and the tests' install_fake_ui) call attach(gui) to register the
real implementations; attach also wires the session stats' on_update callbacks.
"""

from reciproca import state
from reciproca.logging_sink import log, logger

# Registered GUI handlers, set by attach().
_handlers = {}


def _call(name, *args, **kwargs):
    """Run one registered handler, never letting it raise into the core."""
    handler = _handlers.get(name)
    if handler is None:
        return None
    try:
        return handler(*args, **kwargs)
    except Exception:
        logger.debug(f"GUI handler {name} failed", exc_info=True)
        return None


def attach(gui_module):
    """Wire the handlers the core calls to a GUI module's implementations."""
    _handlers.update({
        'update_follow_ui_state': gui_module.update_follow_ui_state,
        'update_unfollow_ui_state': gui_module.update_unfollow_ui_state,
        'update_progress': gui_module.update_progress,
        'reset_progress': gui_module.reset_progress,
        'reset_unfollow_progress': gui_module.reset_unfollow_progress,
        'update_stats_display': gui_module.update_stats_display,
        'update_unfollow_stats_display': gui_module.update_unfollow_stats_display,
        'update_unfollow_progress': gui_module.update_unfollow_progress,
        'refresh_queue_display': gui_module.refresh_queue_display,
        'update_live_extraction_display': gui_module.update_live_extraction_display,
        'update_account_label': gui_module.update_account_label,
        'set_progress_maximum': gui_module.set_progress_maximum,
        'on_stop_clicked': gui_module.on_stop_clicked,
        'notify_user': gui_module.notify_user,
    })
    # The stats objects in state.py are created with no callback; wire them to
    # the GUI's displays so every increment redraws the labels.
    state.stats.on_update = _handlers['update_stats_display']
    state.uf_stats.on_update = _handlers['update_unfollow_stats_display']
    _call('update_stats_display')
    _call('update_unfollow_stats_display')


def detach():
    """Drop every registered handler (used by tests and UI teardown)."""
    _handlers.clear()
    state.stats.on_update = None
    state.uf_stats.on_update = None


def stats_handler():
    """The currently attached follow-stats display, for a fresh SessionStats."""
    return _handlers.get('update_stats_display')


def unfollow_stats_handler():
    """The currently attached unfollow-stats display, for a fresh SessionStats."""
    return _handlers.get('update_unfollow_stats_display')


# ---------------------------
# Delegators - same names and signatures the core always used
# ---------------------------
def update_follow_ui_state():
    """Follow-tab buttons in agreement with the browser and any running session."""
    _call('update_follow_ui_state')


def update_unfollow_ui_state():
    """Unfollow tab's Start button and summary, in agreement with what is loaded."""
    _call('update_unfollow_ui_state')


def update_progress(current, total, phase="", current_hashtag="", author_num=0, total_authors=0, author_name="", followers_extracted=0, overall_progress=None):
    """Update progress bar and status with phase-specific information."""
    _call('update_progress', current, total, phase, current_hashtag, author_num, total_authors, author_name, followers_extracted, overall_progress)


def reset_progress():
    """Reset progress bar."""
    _call('reset_progress')


def reset_unfollow_progress():
    """Reset the unfollow tab's progress bar."""
    _call('reset_unfollow_progress')


def update_stats_display():
    """Update the stats label."""
    _call('update_stats_display')


def update_unfollow_stats_display():
    """Update the unfollow tab's stats label."""
    _call('update_unfollow_stats_display')


def update_unfollow_progress(current, total):
    """Update the unfollow tab's progress bar and status label."""
    _call('update_unfollow_progress', current, total)


def refresh_queue_display():
    """Refresh the queue listbox display with frequency rankings."""
    _call('refresh_queue_display')


def update_live_extraction_display():
    """Update the live extraction listbox with current extracted users."""
    _call('update_live_extraction_display')


def update_account_label():
    """Show the last account that logged into the browser."""
    _call('update_account_label')


def set_progress_maximum(maximum):
    """The progress bar's scale, always 100 (a percentage)."""
    _call('set_progress_maximum', maximum)


def on_stop_clicked():
    """Both Stop buttons acknowledge the click; the session stops at a checkpoint."""
    _call('on_stop_clicked')


def notify_user(title, message, kind='info'):
    """Surface a dialog when a GUI is attached; log the message otherwise.

    Replaces the messagebox calls that used to live in the core paths, so
    headless callers (CLI, MCP) get the same information in the log instead of
    a dialog nobody can see. `kind` is 'info' | 'warning' | 'error'.
    """
    if 'notify_user' in _handlers:
        return _call('notify_user', title, message, kind)
    level = {'info': 'info', 'warning': 'warning', 'error': 'error'}.get(kind, 'info')
    log(message, level)
    return None
