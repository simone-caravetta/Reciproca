"""
Reciproca - the unfollow side: per-user unfollow, the list loop with resumable
progress, the account-switch guard, loading the JSON export pair, and the
reset. Core logic only - dialogs stay in the GUI, decisions stay here.
"""

import os
import random
import time

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

from reciproca import config, hooks, state
from reciproca.logging_sink import log, logger
from reciproca.markers import FOLLOWING_BUTTON_MARKERS, UNFOLLOW_CONFIRM_MARKERS
from reciproca.persistence import (
    current_account_id,
    uf_load_followers,
    uf_load_following,
    uf_load_progress,
    uf_load_session,
    uf_save_progress,
    uf_save_session,
    uf_progress_archive,
)
from reciproca.utils import has_marker


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
        state.uf_stats.increment('attempted')

        state.driver.get(f"https://www.instagram.com/{username}/")
        time.sleep(random.uniform(1, 2))

        buttons = state.driver.find_elements(By.TAG_NAME, "button")

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

        state.driver.execute_script("arguments[0].click();", follow_btn)
        time.sleep(random.uniform(1, 2))

        elements = state.driver.find_elements(By.XPATH, "//button | //div[@role='button']")

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

        state.driver.execute_script("arguments[0].click();", unfollow_btn)

        state.uf_stats.increment('succeeded')
        return True, None

    except Exception as e:
        state.uf_stats.increment('errors')
        return False, str(e)


def unfollow_from_list(users_to_process, delay_min, delay_max, limit):
    """Unfollow users from the non-followers list, tracking progress for resumability."""
    successful = 0

    log(f"📋 Non-followers to process: {len(users_to_process)}")
    log(f"🎯 Target: {limit} unfollows this session")

    for i, user in enumerate(users_to_process):
        if state.stop_requested.is_set():
            log("⏹️ Stopped by user", 'warning')
            break

        if successful >= limit:
            log(f"✅ Reached target of {limit} unfollows", 'success')
            break

        hooks.update_unfollow_progress(i, len(users_to_process))

        log(f"\n➡️ {user}")
        result, reason = unfollow_user(user)

        state.uf_progress["processed"].append(user)

        if result:
            log(f"✅ Unfollow {user} | {i+1}/{len(users_to_process)}", 'success')
            successful += 1
            state.uf_progress["unfollowed"].append(user)
        else:
            log(f"⚠️ Skip {user} | {reason}", 'warning')
            state.uf_progress["skipped"].append(user)

        uf_save_progress()

        if state.stop_requested.is_set():
            log("🛑 Stop detected, breaking...", 'warning')
            break

        delay = random.uniform(delay_min, delay_max)
        log(f"⏱️ Waiting {delay:.1f}s...", 'info')
        # Chunked so Stop stays responsive during long delays
        for _ in range(int(delay)):
            if state.stop_requested.is_set():
                break
            time.sleep(1)
        time.sleep(delay % 1)

    hooks.update_unfollow_progress(successful, max(successful, 1))
    return successful


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
        if os.path.exists(config.UNFOLLOW_PROGRESS_FILE):
            os.replace(config.UNFOLLOW_PROGRESS_FILE, uf_progress_archive(previous_id))
        returning = uf_progress_archive(account_id)
        if os.path.exists(returning):
            os.replace(returning, config.UNFOLLOW_PROGRESS_FILE)
    except Exception as e:
        logger.error(f"Error switching unfollow progress between accounts: {e}")

    state.uf_followers = set()
    state.uf_following = set()
    state.uf_non_followers = []
    uf_save_session(account_id=account_id, followers_file=None, following_file=None)
    uf_load_progress()

    already_removed = len(state.uf_progress.get("unfollowed", []))
    log("👥 Different Instagram account detected - unfollow progress switched", 'warning')
    hooks.update_unfollow_ui_state()
    hooks.notify_user(
        "Account changed",
        "The browser is logged into a different Instagram account than the one the "
        "unfollow progress belongs to.\n\n"
        "That progress has been set aside under the previous account and will come "
        "back if you log into it again - nothing was deleted.\n\n"
        f"This account's own progress is now active ({already_removed} already "
        "unfollowed). Load its followers.json and following.json to continue.",
        'info',
    )


def uf_load_json_pair(f1, f2):
    """Load followers.json and following.json exports and compute non-followers.

    Silent on failure: returns {"ok": False, "error": ...} and lets the caller
    (filedialog in the GUI, paths from the CLI) decide how to surface it.
    """
    try:
        state.uf_followers = uf_load_followers(f1)
        state.uf_following = uf_load_following(f2)
        state.uf_non_followers = list(state.uf_following - state.uf_followers)

        if len(state.uf_non_followers) == 0:
            return {"ok": False, "error": "No users found (no non-followers)", "non_followers": []}

        log(f"✔ JSON files loaded: {len(state.uf_non_followers)} non-followers found", 'success')

        uf_save_session(
            followers_file=f1,
            following_file=f2,
            account_id=current_account_id() or uf_load_session().get("account_id"),
        )

        hooks.update_unfollow_ui_state()
        return {"ok": True, "error": None, "non_followers": list(state.uf_non_followers)}

    except Exception as e:
        logger.exception("Error loading unfollow JSON files")
        return {"ok": False, "error": f"Invalid files:\n{e}", "non_followers": []}


def uf_auto_load_last_session():
    """Auto-reload the last followers/following.json pair on startup."""
    if not os.path.exists(config.UNFOLLOW_SESSION_FILE):
        return

    try:
        data = uf_load_session()

        f1 = data.get("followers_file")
        f2 = data.get("following_file")

        if not f1 or not f2 or not os.path.exists(f1) or not os.path.exists(f2):
            log("⚠️ Saved unfollow session found but JSON files are missing, reload them manually", 'warning')
            return

        state.uf_followers = uf_load_followers(f1)
        state.uf_following = uf_load_following(f2)
        state.uf_non_followers = list(state.uf_following - state.uf_followers)

        uf_load_progress()
        total, remaining, removed = unfollow_progress_counts()
        log(
            f"🔄 Unfollow session reloaded: {total} non-followers, "
            f"{remaining} still to process ({removed} already removed)",
            'info'
        )
        hooks.update_unfollow_ui_state()

    except Exception as e:
        logger.debug(f"Auto-load unfollow session error: {e}")


def unfollow_progress_counts():
    """(total, remaining, removed) for the loaded non-followers list.

    Read from the saved progress, so it survives restarts: the work already done
    in earlier sessions is the whole point of keeping that file.
    """
    processed = set(state.uf_progress.get("processed", []))
    remaining = sum(1 for u in state.uf_non_followers if u not in processed)
    return len(state.uf_non_followers), remaining, len(state.uf_progress.get("unfollowed", []))


def reset_unfollow_state():
    """Discard the unfollow progress and session (leaves the follow queue alone).

    Unconditional - the caller is expected to have asked for confirmation first
    (the GUI's reset_unfollow_app does, with a dialog that says exactly what is
    about to be lost).

    Unlike the automatic account switch, this throws the record away for good.
    Everyone already unfollowed on Instagram stays unfollowed - only the app's
    memory of it goes, which means those accounts can be processed again if they
    turn up in a future export.
    """
    uf_load_progress()
    processed = len(state.uf_progress.get("processed", []))

    if os.path.exists(config.UNFOLLOW_PROGRESS_FILE):
        os.remove(config.UNFOLLOW_PROGRESS_FILE)
    if os.path.exists(config.UNFOLLOW_SESSION_FILE):
        os.remove(config.UNFOLLOW_SESSION_FILE)

    state.uf_followers = set()
    state.uf_following = set()
    state.uf_non_followers = []
    state.uf_progress = {"processed": [], "unfollowed": [], "skipped": []}

    hooks.update_unfollow_ui_state()
    hooks.reset_unfollow_progress()
    log(f"🔄 Unfollow reset complete ({processed} processed entries discarded)", 'info')
