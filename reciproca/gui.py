"""
Reciproca - the tkinter GUI.

This is a frontend onto the core package: setup_gui() builds the window,
attaches its handlers to the core via hooks.attach() and registers the log
sink, and every action is a thin wrapper that reads widgets, calls a core
function, and renders the structured result as the dialogs the app always
showed. Nothing about running a session lives here anymore - cycles.py owns
that, so the CLI and the MCP server run identical sessions without any GUI.
"""

import json
import os
import random
import re
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, ttk

from reciproca import config, hooks, state
from reciproca.browser import (
    BROWSER_WATCH_INTERVAL,
    begin_session,
    browser_is_open,
    can_open_browser,
    end_session,
    handle_browser_closed,
    poll_browser,
    start_browser,
    stop_bot,
)
from reciproca.cycles import follow_cycle, unfollow_cycle
from reciproca.follow import run_scoring_pass
from reciproca.logging_sink import log, logger, register_sink
from reciproca.persistence import load_hashtags, save_hashtags
from reciproca.queue import (
    add_to_queue,
    clear_queue,
    load_queue,
    queue_affinity,
    queue_username,
    rank_queue,
    remove_from_queue,
    validate_queue,
)
from reciproca.unfollow import (
    reset_unfollow_state,
    uf_auto_load_last_session,
    uf_load_json_pair,
    unfollow_progress_counts,
)

# Widgets, created by setup_gui().
root = None
log_box = None
progress_bar = None
status_label = None
stats_label = None
hashtag_listbox = None
hashtag_entry = None
delay_min_entry = None
delay_max_entry = None
limit_entry = None
start_btn = None
stop_btn = None
browser_btn = None
queue_listbox = None
queue_entry = None
queue_count_label = None
queue_score_label = None
score_queue_btn = None
mode_var = None
main_queue_info = None
live_extraction_listbox = None
live_extraction_label = None
uf_data_label = None
uf_delay_min_entry = None
uf_delay_max_entry = None
uf_limit_entry = None
uf_progress_bar = None
uf_status_label = None
uf_stats_label = None
uf_start_btn = None
uf_stop_btn = None
uf_browser_btn = None
account_label = None


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


def _gui_log_sink(full_msg, level):
    """Write a log line into the Logs tab, colored by level.

    Registered via register_sink() when the window is built; the core's log()
    appends to its own buffer and writes the file logger, so this only touches
    the widget.
    """
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


def update_stats_display():
    """Update the stats label."""
    stats_text = (
        f"Followed: {state.stats.succeeded} | "
        f"Attempted: {state.stats.attempted} | "
        f"Skipped: {state.stats.skipped_already_following} | "
        f"Errors: {state.stats.errors}"
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
        f"Unfollowed: {state.uf_stats.succeeded} | "
        f"Attempted: {state.uf_stats.attempted} | "
        f"Errors: {state.uf_stats.errors}"
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


def update_account_label():
    """Show the last account that logged into the browser."""
    from reciproca.persistence import load_account_username
    username = load_account_username()
    text = f"👤 Account: {username}" if username else "👤 Account: —"
    try:
        account_label.config(text=text)
    except Exception as e:
        logger.debug(f"update_account_label error: {e}")


def update_follow_ui_state():
    """Follow-tab buttons in agreement with the browser and any running session.

    One place decides, so no exit path can leave Start Following enabled with no
    browser behind it, or Open Browser disabled after the browser is gone - which
    left the app stuck until it was restarted.
    """
    try:
        running = state.session_running.is_set()
        browser_btn.config(state='normal' if can_open_browser() else 'disabled')
        start_btn.config(
            state='normal' if state.driver is not None and not running and state.login_completed else 'disabled'
        )
        stop_btn.config(state='normal' if running else 'disabled')
    except Exception as e:
        logger.debug(f"update_follow_ui_state error: {e}")


def update_unfollow_ui_state():
    """Unfollow tab's Start button and summary, in agreement with what is loaded.

    One place owns that label. A second function used to write a fuller version of
    it, but nothing called that at startup, so a reloaded session showed only its
    total - how much of the list had already been done stayed hidden until a round
    finished or Stop was pressed.
    """
    try:
        from reciproca.persistence import uf_load_progress
        uf_load_progress()
        running = state.session_running.is_set()
        uf_browser_btn.config(state='normal' if can_open_browser() else 'disabled')
        uf_start_btn.config(
            state='normal'
            if state.driver is not None and state.uf_non_followers and not running and state.login_completed
            else 'disabled'
        )
        uf_stop_btn.config(state='normal' if running else 'disabled')

        if not state.uf_non_followers:
            uf_data_label.config(text="🟡 Load followers.json and following.json to begin")
            return

        total, remaining, removed = unfollow_progress_counts()
        summary = f"🟢 {total} non-followers | {remaining} to process | {removed} already removed"
        if state.driver is None:
            # The counts are worth seeing before the browser is open; say why Start
            # is not available rather than hiding them behind that instruction.
            summary += " | open the browser to start"
        uf_data_label.config(text=summary)
    except Exception as e:
        logger.debug(f"update_unfollow_ui_state error: {e}")


def set_progress_maximum(maximum):
    """The progress bar's scale, always 100 (a percentage)."""
    progress_bar['maximum'] = maximum


def on_stop_clicked():
    """Both Stop buttons acknowledge the click; the session stops at a checkpoint."""
    stop_btn.config(state='disabled')
    uf_stop_btn.config(state='disabled')


def notify_user(title, message, kind='info'):
    """Surface a dialog for a core notification."""
    if kind == 'error':
        messagebox.showerror(title, message)
    elif kind == 'warning':
        messagebox.showwarning(title, message)
    else:
        messagebox.showinfo(title, message)


def watch_browser():
    """Notice the browser being closed, instead of waiting for something to fail.

    Only probes while no worker thread is running, to keep two threads off the
    same Selenium session. A browser closed mid-session is caught by the worker
    failing, and by the check when the session finishes.
    """
    try:
        poll_browser()
    except Exception as e:
        logger.debug(f"watch_browser error: {e}")
    finally:
        root.after(BROWSER_WATCH_INTERVAL, watch_browser)


def uf_load_json_files():
    """Prompt user for followers.json/following.json and compute non-followers."""
    try:
        f1 = filedialog.askopenfilename(title="Select followers.json")
        if not f1:
            return
        f2 = filedialog.askopenfilename(title="Select following.json")
        if not f2:
            return

        result = uf_load_json_pair(f1, f2)
        if not result.get("ok"):
            messagebox.showerror("Error", result.get("error", "Failed to load files"))

    except Exception as e:
        messagebox.showerror("Error", f"Invalid files:\n{e}")
        logger.exception("Error loading unfollow JSON files")


def reset_unfollow_app():
    """Discard the unfollow progress and session (leaves the follow queue alone).

    Unlike the automatic account switch, this throws the record away for good, so
    it says exactly what is about to be lost and what is not. Everyone already
    unfollowed on Instagram stays unfollowed - only the app's memory of it goes,
    which means those accounts can be processed again if they turn up in a future
    export.
    """
    from reciproca.persistence import uf_load_progress

    uf_load_progress()
    processed = len(state.uf_progress.get("processed", []))
    removed = len(state.uf_progress.get("unfollowed", []))

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

    reset_unfollow_state()


def _search_decision(info):
    """The dialog after a search, as a follow_cycle decision hook.

    Three named buttons rather than a yes/no/cancel: the choices are actions,
    not answers, so they are labeled as such. The semantic scoring of the top
    candidates is the common next step and is announced here, because with the
    scoring decoupled from the follow it is now a phase the user is told about
    instead of a hidden tail of the session.

    Built on the worker thread follow_cycle calls the hook from, the same
    place the old askyesnocancel was; wait_window pumps the event loop the
    same way. Closing the window counts as Discard.
    """
    decision = {"value": "discard"}

    def choose(value):
        decision["value"] = value
        dialog.destroy()

    dialog = tk.Toplevel(root)
    dialog.title("Search Complete")
    dialog.resizable(True, True)
    dialog.minsize(520, 230)
    dialog.transient(root)
    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("discard"))

    # The text wraps at a fixed width instead of stretching the window to its
    # longest line: on odd font metrics the window would otherwise size itself
    # so wide that the buttons below got pushed off the visible area.
    tk.Label(
        dialog,
        text=(
            f"Found {info['ranked_count']} unique users from {info['hashtag_count']} hashtag(s).\n"
            f"Highest frequency: {info['top_freq']} (appeared in {info['top_freq']} authors' followers).\n\n"
            f"Unless you choose Discard, the results are saved to the queue\n"
            f"and the semantic scoring of the top candidates runs automatically\n"
            f"(visible on the Follow tab)."
        ),
        justify='left',
        wraplength=460,
        anchor='w',
        padx=18,
        pady=14,
    ).pack()

    buttons = ttk.Frame(dialog, padding=(18, 4, 18, 14))
    buttons.pack()
    ttk.Button(buttons, text="💾 Save to queue", command=lambda: choose("save_stop")).pack(side='left', padx=4)
    ttk.Button(buttons, text="🚀 Save & follow now", command=lambda: choose("follow")).pack(side='left', padx=4)
    ttk.Button(buttons, text="🗑️ Discard", command=lambda: choose("discard")).pack(side='left', padx=4)

    dialog.grab_set()
    dialog.wait_window()
    return decision["value"]


def _render_follow_result(result):
    """Turn follow_cycle's result into the dialogs the GUI always showed."""
    if not result.get("ok"):
        errors = {
            "session_busy": ("Error", "A session is already running - stop it before starting another"),
            "browser_not_open": ("Error", "Please open browser first"),
            "queue_empty": ("Queue Empty", "No users in queue. Use 'Deep Search' mode to find users."),
            "no_hashtags": ("Error", "Please add at least one hashtag"),
        }
        if result.get("error") in errors:
            title, message = errors[result["error"]]
            messagebox.showerror(title, message)
        return

    if result.get("branch") == "save_stop":
        messagebox.showinfo(
            "Saved to Queue",
            f"{result.get('added', 0)} users saved to queue.\n\n"
            f"Click 'Start Following' in the GUI to begin following."
        )
        return

    if result.get("branch") in ("follow", "queue"):
        messagebox.showinfo(
            "Session Complete",
            f"{result.get('report', '')}\n\nQueue: {result.get('queue_remaining', 0)} users remaining"
        )


def follow_logic():
    """Main follow logic - supports both queue and search modes.

    Reads the timing entries, hands the session to follow_cycle() (which owns
    the browser session and every check), and renders the structured result as
    the dialogs this app always showed.
    """
    try:
        try:
            delay_min = int(delay_min_entry.get())
            delay_max = int(delay_max_entry.get())
            limit = int(limit_entry.get())
        except ValueError:
            log("❌ Invalid numeric input!", 'error')
            messagebox.showerror("Error", "Please enter valid numbers")
            return

        mode = mode_var.get()

        if mode == 'queue':
            result = follow_cycle(mode="queue", delay_min=delay_min, delay_max=delay_max, limit=limit)
        else:
            hashtags = list(hashtag_listbox.get(0, tk.END))
            result = follow_cycle(
                mode="search", delay_min=delay_min, delay_max=delay_max, limit=limit,
                hashtags=hashtags, decision_hook=_search_decision,
            )

        _render_follow_result(result)

    except Exception as e:
        log(f"❌ Fatal error: {e}", 'error')
        logger.exception("Fatal error in follow_logic")


def run_follow():
    """Start follow in background thread.

    Peek at the session flag so a busy session spawns nothing - but the claim
    itself is follow_cycle()'s, end to end. Claiming it here too (as the
    monolith's run_follow did) would double the claim: follow_cycle would
    refuse with session_busy, and the claim it could not take would never be
    released, so every later Start click would die silently.
    """
    if state.session_running.is_set():
        return
    thread = threading.Thread(target=follow_logic, daemon=True)
    thread.start()
    state.active_threads.append(thread)


def unfollow_logic():
    """Main unfollow logic - processes the non-followers list computed from the JSON exports."""
    try:
        try:
            delay_min = int(uf_delay_min_entry.get())
            delay_max = int(uf_delay_max_entry.get())
            limit = int(uf_limit_entry.get())
        except ValueError:
            log("❌ Invalid numeric input!", 'error')
            messagebox.showerror("Error", "Please enter valid numbers")
            return

        result = unfollow_cycle(delay_min=delay_min, delay_max=delay_max, limit=limit)

        if not result.get("ok"):
            errors = {
                "browser_not_open": ("Error", "Please open the browser first"),
                "no_data": ("Error", "Please load the JSON files first"),
            }
            if result.get("error") == "all_processed":
                messagebox.showinfo(
                    "Completed",
                    "All non-followers have already been processed.\nUse 'Reset' to start over."
                )
            elif result.get("error") in errors:
                title, message = errors[result["error"]]
                messagebox.showerror(title, message)
            return

        messagebox.showinfo("Session Complete", result.get("report", ""))

    except Exception as e:
        log(f"❌ Fatal error: {e}", 'error')
        logger.exception("Fatal error in unfollow_logic")


def run_unfollow():
    """Start unfollow in background thread.

    Like run_follow: peek at the flag, but let unfollow_cycle() claim the
    session.
    """
    if state.session_running.is_set():
        return
    thread = threading.Thread(target=unfollow_logic, daemon=True)
    thread.start()
    state.active_threads.append(thread)


# ---------------------------
# HASHTAG FUNCTIONS
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
    ranked = rank_queue(load_queue())

    # Remember what each row holds, so acting on a selection never has to
    # reconstruct the ordering and cannot disagree with what is on screen.
    state.displayed_queue_usernames = [username for username, _, _ in ranked[:100]]

    # The row says what the order is made of, not the number it comes out as: how
    # many scanned authors this candidate follows, and how close their profile read
    # to the niche where that has been measured. A single 0.61 on screen would sort
    # the list correctly and tell you nothing about why.
    from reciproca.queue import ranking_frequencies
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
        live_extraction_listbox.delete(0, tk.END)

        if not state.live_extracted_users:
            live_extraction_listbox.insert(tk.END, "Waiting for extraction...")
            return

        # Get current frequencies
        frequencies = state.live_frequencies

        # Create sorted list by frequency (highest first)
        user_freq_list = [(user, frequencies.get(user, 0)) for user in state.live_extracted_users]
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
        if idx < len(state.displayed_queue_usernames):
            username = state.displayed_queue_usernames[idx]
            remove_from_queue(username)
            refresh_queue_display()
            log(f"Removed {username} from queue", 'info')


def clear_queue_ui():
    """Clear queue with confirmation."""
    if messagebox.askyesno("Confirm", "Clear entire follow queue?"):
        clear_queue()
        refresh_queue_display()
        log("🗑️ Queue cleared", 'warning')


def score_queue_ui():
    """Run the semantic scoring pass from the Queue tab, standalone.

    The decoupled entry point: scoring no longer has to ride along on a
    search session - the queue can be scored any time the browser is open and
    no session is running. The pass claims the session like any cycle does,
    so the browser is safe and Stop works; progress shows on the label next
    to the button.
    """
    if state.session_running.is_set():
        messagebox.showwarning("Session running", "Stop the current session before scoring the queue.")
        return
    if state.driver is None:
        messagebox.showerror("Browser closed", "Open the browser first - scoring reads the candidates' profiles.")
        return
    if not begin_session():
        return

    def worker():
        try:
            def on_progress(number, total, username):
                queue_score_label.config(text=f"🧠 Scoring {number}/{total}: {username}")
                root.update_idletasks()
            run_scoring_pass(on_progress=on_progress)
        except Exception as e:
            log(f"❌ Scoring error: {e}", 'error')
            logger.exception("Scoring failed")
        finally:
            end_session()
            queue_score_label.config(text="")
            score_queue_btn.config(state='normal')
            refresh_queue_display()

    score_queue_btn.config(state='disabled')
    queue_score_label.config(text="🧠 Scoring…")
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    state.active_threads.append(thread)


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
        filename = config.data_path(f"follow_logs_{datetime.now():%Y%m%d_%H%M%S}.txt")
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
        if state.driver:
            try:
                state.driver.quit()
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
    global queue_score_label, score_queue_btn
    global live_extraction_listbox, live_extraction_label
    global uf_data_label, uf_delay_min_entry, uf_delay_max_entry, uf_limit_entry
    global uf_progress_bar, uf_status_label, uf_stats_label, uf_start_btn, uf_stop_btn
    global uf_browser_btn
    global account_label

    root = tk.Tk()
    root.title("Reciproca - Follow & Unfollow")
    root.geometry("800x800")
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

    # Scoring is a phase between the search and the following, and nothing either
    # side of it depends on having happened. So it gets a switch here rather than a
    # number in the settings: it is the sort of thing to change your mind about
    # while looking at the tab you start a search from.
    semantic_var = tk.IntVar(value=1 if config.CONFIG["SEMANTIC_ENABLED"] else 0)

    # The niche belongs here rather than in the settings: it is the question the
    # scoring asks, so it is read and changed at the same moment as the switch that
    # turns the scoring on, not two tabs away among the numbers.
    niche_frame = ttk.Frame(mode_frame)
    niche_label = ttk.Label(niche_frame, text='Niche:')
    niche_entry = ttk.Entry(niche_frame)
    niche_entry.insert(0, str(config.CONFIG.get("SEMANTIC_NICHE") or ""))

    def save_niche(event=None):
        """Keep what was typed. Bound to leaving the field and to pressing Enter."""
        typed = niche_entry.get().strip()
        if typed != str(config.CONFIG.get("SEMANTIC_NICHE") or ""):
            config.CONFIG["SEMANTIC_NICHE"] = typed
            config.save_config(config.CONFIG)
            log(f"🧭 Niche set to: {typed}" if typed else "🧭 Niche cleared", 'info')

    niche_entry.bind('<FocusOut>', save_niche)
    niche_entry.bind('<Return>', save_niche)

    def toggle_semantic():
        on = bool(semantic_var.get())
        config.CONFIG["SEMANTIC_ENABLED"] = 1 if on else 0
        config.save_config(config.CONFIG)

        # Nothing to describe while the scoring is off, so the field says so by
        # being unusable rather than by sitting there inviting an answer to a
        # question nobody is going to ask.
        niche_entry.config(state='normal' if on else 'disabled')
        niche_label.config(foreground='' if on else 'gray')

        if on:
            log("🧭 Profiles will be scored against your niche after a search", 'info')
            if not str(config.CONFIG.get("SEMANTIC_NICHE") or "").strip():
                log("   Describe who you are looking for in the Niche box", 'info')
        else:
            log("🧭 Scoring off - the queue keeps its order by sightings", 'info')

    ttk.Checkbutton(
        mode_frame,
        text='🧭 Score profiles against my niche after the search',
        variable=semantic_var,
        command=toggle_semantic
    ).pack(anchor='w', pady=(6, 0))

    niche_frame.pack(fill='x', pady=(2, 0), padx=(20, 0))
    niche_label.pack(side='left', padx=(0, 5))
    niche_entry.pack(side='left', fill='x', expand=True)
    ToolTip(
        niche_entry,
        "Who you are looking for, as a sentence rather than keywords - the model "
        "reads it the way it reads a bio.\n"
        "Example: fotografi che scattano su pellicola e mostrano il loro lavoro"
    )

    # Match the field to the switch as it is drawn, not only when it is clicked.
    if not semantic_var.get():
        niche_entry.config(state='disabled')
        niche_label.config(foreground='gray')

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
    delay_min_entry.insert(0, str(config.CONFIG["DEFAULT_DELAY_MIN"]))
    delay_min_entry.pack(side='left', padx=(0, 10))

    ttk.Label(quick_frame, text='Delay Max (sec):').pack(side='left', padx=(0, 3))
    delay_max_entry = ttk.Entry(quick_frame, width=6, validate='key', validatecommand=vcmd)
    delay_max_entry.insert(0, str(config.CONFIG["DEFAULT_DELAY_MAX"]))
    delay_max_entry.pack(side='left', padx=(0, 10))

    ttk.Label(quick_frame, text='Follow Limit:').pack(side='left', padx=(0, 3))
    limit_entry = ttk.Entry(quick_frame, width=6, validate='key', validatecommand=vcmd)
    limit_entry.insert(0, str(config.CONFIG["MAX_FOLLOWS_PER_SESSION"]))
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

    account_label = ttk.Label(
        control_frame,
        text='👤 Account: —',
        font=('Helvetica', 10, 'bold')
    )
    account_label.pack(side='left', padx=5)

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
    uf_delay_min_entry.insert(0, str(config.CONFIG["UNFOLLOW_DELAY_MIN"]))
    uf_delay_min_entry.pack(side='left', padx=(0, 10))

    ttk.Label(uf_timing_frame, text='Delay Max (sec):').pack(side='left', padx=(0, 3))
    uf_delay_max_entry = ttk.Entry(uf_timing_frame, width=6, validate='key', validatecommand=uf_vcmd)
    uf_delay_max_entry.insert(0, str(config.CONFIG["UNFOLLOW_DELAY_MAX"]))
    uf_delay_max_entry.pack(side='left', padx=(0, 10))

    ttk.Label(uf_timing_frame, text='Session Limit:').pack(side='left', padx=(0, 3))
    uf_limit_entry = ttk.Entry(uf_timing_frame, width=6, validate='key', validatecommand=uf_vcmd)
    uf_limit_entry.insert(0, str(config.CONFIG["UNFOLLOW_DAILY_LIMIT"]))
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
    score_queue_btn = ttk.Button(queue_ctrl_frame, text='🧠 Score Queue', command=score_queue_ui)
    score_queue_btn.pack(side='left', padx=(12, 2))
    ToolTip(score_queue_btn, "Score the best candidates against your niche (semantic ranking)")

    # Scoring status line - the Queue tab's own place for the scoring phase,
    # so a pass started from here is visible without switching tabs.
    queue_score_frame = ttk.Frame(queue_tab)
    queue_score_frame.pack(fill='x', pady=(0, 10))
    queue_score_label = ttk.Label(queue_score_frame, text="")
    queue_score_label.pack(side='left')

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

    def create_config_row(parent, row, label, config_key, description, is_password=False, validate=True):
        """Helper to create a labeled config row with entry field."""
        lbl = ttk.Label(parent, text=f'{label}:', font=('Helvetica', 9, 'bold'))
        lbl.grid(row=row, column=0, sticky='w', pady=3, padx=(0, 5))

        entry_kwargs = {'width': 10}
        if validate:
            entry_kwargs['validate'] = 'key'
            entry_kwargs['validatecommand'] = vcmd

        entry = ttk.Entry(parent, **entry_kwargs)
        entry.insert(0, str(config.CONFIG.get(config_key, "")))
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

    # ─── AUTHOR FOLLOW ───
    author_frame = ttk.LabelFrame(settings_scrollable_frame, text='👤 Author Follow', padding=10)
    author_frame.pack(fill='x', pady=(0, 10), padx=5, expand=True)

    create_config_row(author_frame, 0, "AUTHOR_FOLLOW_ENABLED", "AUTHOR_FOLLOW_ENABLED",
                      "← 1 to follow scraped authors before extracting their followers")
    create_config_row(author_frame, 1, "AUTHOR_MAX_FOLLOWERS_RATIO", "AUTHOR_MAX_FOLLOWERS_RATIO",
                      "← Skip an author whose followers exceed their following by this many times")

    # ─── SEMANTIC RANKING ───
    semantic_frame = ttk.LabelFrame(
        settings_scrollable_frame, text='🧭 Semantic Ranking', padding=10
    )
    semantic_frame.pack(fill='x', pady=(0, 10), padx=5, expand=True)

    create_config_row(semantic_frame, 0, "SEMANTIC_WEIGHT", "SEMANTIC_WEIGHT",
                      "← 0-100. How much of a candidate's rank is the niche rather "
                      "than how often they were seen. 0 is the order without it")
    create_config_row(semantic_frame, 1, "SEMANTIC_TOP_K", "SEMANTIC_TOP_K",
                      "← Candidates kept after a search. Every one of them is read, "
                      "one page load each, so this is what the pass costs")
    create_config_row(semantic_frame, 2, "SEMANTIC_READ_DELAY", "SEMANTIC_READ_DELAY",
                      "← Seconds between profiles while reading. 0 is as fast as the "
                      "pages load")

    ttk.Label(
        semantic_frame,
        text="Switched on, and the niche described, on the Auto Follow tab.",
        foreground='gray', font=('Helvetica', 8)
    ).grid(row=3, column=0, columnspan=3, sticky='w', pady=(8, 0))

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

    def _sync_quick_entries():
        """Refresh the Follow/Unfollow tab quick fields from the config just
        written. Sessions read those entry widgets, so stale values left in
        them are exactly the "runs with a different setting than the file"
        divergence."""
        for widget, key in ((delay_min_entry, "DEFAULT_DELAY_MIN"),
                            (delay_max_entry, "DEFAULT_DELAY_MAX"),
                            (limit_entry, "MAX_FOLLOWS_PER_SESSION"),
                            (uf_delay_min_entry, "UNFOLLOW_DELAY_MIN"),
                            (uf_delay_max_entry, "UNFOLLOW_DELAY_MAX"),
                            (uf_limit_entry, "UNFOLLOW_DAILY_LIMIT")):
            widget.delete(0, tk.END)
            widget.insert(0, str(config.CONFIG[key]))

    def apply_config():
        """Apply the config changes from GUI entries."""
        for key, entry in config_entries.items():
            try:
                # Every settings row is a numeric field, whatever its group.
                config.CONFIG[key] = int(entry.get().strip())
            except ValueError:
                messagebox.showerror("Invalid Value", f"Could not convert '{entry.get()}' to a number for {key}")
                return

        config.save_config(config.CONFIG)
        _sync_quick_entries()
        log("✅ Configuration saved!", 'success')
        messagebox.showinfo("Saved", "Configuration has been saved. Changes will take effect on next session.")

    def reset_config():
        """Reset all config entries to saved config."""
        for key, entry in config_entries.items():
            entry.delete(0, tk.END)
            entry.insert(0, str(config.CONFIG.get(key, "")))

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

    # The core's log() fans out to this widget; the core's hooks dispatch to the
    # handlers defined above. Both attach here, so the same session logic drives
    # the GUI headfully and the CLI/MCP headlessly.
    register_sink(_gui_log_sink)
    hooks.attach(sys.modules[__name__])

    # Load last unfollow session (if any) and refresh its display
    uf_auto_load_last_session()
    update_follow_ui_state()
    update_unfollow_ui_state()
    update_account_label()

    # Start watching for the browser being closed behind the app's back
    root.after(BROWSER_WATCH_INTERVAL, watch_browser)

    # Handle close
    root.protocol("WM_DELETE_WINDOW", on_closing)

    return root


# ---------------------------
# MAIN
# ---------------------------
def main():
    root = setup_gui()
    log("Reciproca (Follow & Unfollow) loaded", 'success')
    log("Click 'Open Browser' to start", 'info')
    root.mainloop()


if __name__ == "__main__":
    main()
