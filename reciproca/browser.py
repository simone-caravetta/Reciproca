"""
Reciproca - browser lifecycle: opening and closing Chrome, the login-username
watcher, the session claim (begin/end), the rate-limit probe, and the browser
watcher's probe body. GUI-facing calls go through hooks; this module never
touches tkinter.
"""

import os
import threading
import time

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from reciproca import config, hooks, state
from reciproca.logging_sink import log, logger
from reciproca.utils import brief_error
from reciproca.markers import RATE_LIMIT_MARKERS
from reciproca.persistence import (
    current_account_id,
    load_frequencies,
    save_login_username,
)
from reciproca.queue import add_to_queue, rank_queue, validate_queue
from reciproca.selectors import LOGIN_USERNAME_SELECTORS, LOGIN_USERNAME_XPATHS
from reciproca.unfollow import uf_check_account

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

def chrome_options(headless=False):
    """Chrome options for the persistent profile.

    The headless delta: --headless=new and an explicit window size, and no
    --start-maximized (there is no window to maximize). Everything else - the
    profile directory, the anti-detection switches - is shared, so a headless
    run sees the same logged-in profile as a visible one.
    """
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={config.CHROME_PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")

    # Anti-detection measures
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    return options


def start_browser():
    """Open the browser on a worker thread.

    ChromeDriverManager().install() can download a driver, which takes long enough
    to freeze the GUI if it runs on the Tk callback thread - the window stops
    repainting and the app looks hung.

    Reached from either tab's Open Browser button, so it has to be safe to call
    when a browser is already open or on its way.
    """
    if state.driver is not None or state.browser_opening.is_set():
        return

    state.browser_opening.set()
    hooks.update_follow_ui_state()
    hooks.update_unfollow_ui_state()

    thread = threading.Thread(target=open_browser, daemon=True)
    thread.start()
    state.active_threads.append(thread)


def open_browser(headless=False):
    """Open Chrome browser with persistent profile."""
    try:
        log("🌐 Initializing Chrome...", 'info')

        service = Service(ChromeDriverManager().install())
        state.driver = webdriver.Chrome(service=service, options=chrome_options(headless))

        # Override navigator.webdriver
        state.driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        state.driver.get("https://www.instagram.com/")
        log("✅ Browser opened! Please login manually.", 'success')

        # If the saved profile has logged out, the login form is up and the user
        # types the account name by hand - the one chance to learn it.
        threading.Thread(target=watch_login_username, daemon=True).start()

        # The browser is the only place that knows which account is logged in, so
        # this is the first chance to tell whether the loaded export still matches.
        uf_check_account()

    except Exception as e:
        # Deliberately broad. A frozen build has no console, so anything not caught
        # here vanishes silently and the log just stops after "Initializing Chrome"
        # with no indication of why. Record the full traceback in follow_bot.log,
        # which sits next to the executable, and surface the error in the GUI.
        logger.exception("Failed to open browser")
        log(f"❌ Browser error: {brief_error(e)}", 'error')
        log("   Full traceback written to follow_bot.log", 'error')
        hooks.notify_user(
            "Browser Error",
            f"{brief_error(e)}\n\nSee follow_bot.log next to the app for details.",
            'error',
        )
    finally:
        state.browser_opening.clear()
        refresh_browser_state()


def login_username_field():
    """The login form's username input, or None when the login page is not up."""
    if state.driver is None:
        return None
    for selector in LOGIN_USERNAME_SELECTORS:
        try:
            return state.driver.find_element(By.CSS_SELECTOR, selector)
        except NoSuchElementException:
            continue
        except WebDriverException:
            return None
    for xpath in LOGIN_USERNAME_XPATHS:
        try:
            return state.driver.find_element(By.XPATH, xpath)
        except NoSuchElementException:
            continue
        except WebDriverException:
            return None
    return None


def read_login_username():
    """Username typed into the login form, "" while it is still empty.

    None means the login page is not up: the login went through, the user
    navigated away, or the browser is gone.
    """
    field = login_username_field()
    if field is None:
        return None
    try:
        return (field.get_attribute("value") or "").strip()
    except StaleElementReferenceException:
        return ""
    except WebDriverException:
        # The browser is not answering - treat it as not up and let the
        # watcher keep polling rather than die on a transient failure.
        return None


def watch_login_username():
    """Save the username typed into Instagram's login page, whenever it appears.

    The browser's saved profile is not always logged in: when it is not, the
    login page comes up and the user logs in by hand, and nothing else in the
    app can learn the account name. This thread reads the login form's username
    field and saves the value as soon as the user has typed it.

    It runs for as long as the browser is open, rather than stopping when no
    login form is found: the page needs a moment to appear after the browser
    opens, and a session can expire mid-run and show the login page again.

    It steps aside while a session is driving the browser, like watch_browser.
    """
    last_username = None
    login_form_noticed = False
    while True:
        if state.driver is None or not browser_is_open():
            return
        if state.session_running.is_set() or state.active_threads:
            time.sleep(1)
            continue
        username = read_login_username()
        if username is not None and not login_form_noticed:
            login_form_noticed = True
            log("👤 Login form detected - waiting for the username", 'info')
        if username and username != last_username:
            last_username = username
            save_login_username(username)
        # The session cookie only exists after a successful login, so it is the
        # signal that Start Following can be let out. It also catches the case
        # where the profile was already logged in when the browser opened.
        # A stale cookie can outlive the session though - the profile keeps
        # ds_user_id after Instagram stops accepting it - so the login form
        # being up means logged out, cookie or not.
        completed = username is None and current_account_id() is not None
        if completed != state.login_completed:
            state.login_completed = completed
            if completed:
                log("✅ Login detected - Start Following enabled", 'success')
            hooks.update_follow_ui_state()
            hooks.update_unfollow_ui_state()
        time.sleep(1)


# How often to check that the browser is still there, in milliseconds.
BROWSER_WATCH_INTERVAL = 2000


def browser_is_open():
    """True if the browser is still there, asked of the browser itself.

    The `driver` state is not evidence: closing the Chrome window leaves it
    holding a dead session that looks perfectly valid until something tries to
    use it.

    Never call this from the GUI thread while a worker thread is driving the same
    session - one Selenium session commanded from two threads interleaves badly.
    """
    if state.driver is None:
        return False
    try:
        return bool(state.driver.window_handles)
    except WebDriverException as e:
        logger.info(f"Browser probe failed, treating the browser as closed: {type(e).__name__}")
        return False


def can_open_browser():
    """True when clicking Open Browser would actually do something."""
    return state.driver is None and not state.browser_opening.is_set()


# Sessions are claimed from whatever thread runs a cycle - the GUI's worker
# thread, the CLI's main thread, an MCP worker thread. The check-then-set
# below must not let two callers both see the flag clear, so the claim is
# serialized.
_session_lock = threading.Lock()


def begin_session():
    """Claim the browser for one session, or refuse if another already has it.

    Follow and unfollow drive the same Selenium session and the same window. Two
    at once would interleave commands on one browser: each would navigate the page
    out from under the other, and both would then act on whatever happened to be
    loaded - unfollowing an account the follow session had just opened, or
    following one the unfollow session was on.

    Both tabs disable their Start buttons while a session runs; this is the guard
    that does not depend on a button state being right.
    """
    with _session_lock:
        if state.session_running.is_set():
            log("⚠️ A session is already running - stop it before starting another", 'warning')
            return False

        state.session_running.set()
        state.stop_requested.clear()

    # A stop.flag left behind by a `stop` command from a previous run must not
    # kill the new session at its first pause.
    try:
        if os.path.exists(config.STOP_FLAG_FILE):
            os.remove(config.STOP_FLAG_FILE)
    except Exception as e:
        logger.debug(f"Could not remove stale stop flag: {e}")

    hooks.update_follow_ui_state()
    hooks.update_unfollow_ui_state()
    return True


def end_session():
    """Release the browser when a session finishes and settle both tabs."""
    state.session_running.clear()
    refresh_browser_state()


def handle_browser_closed():
    """Forget a browser that is gone and let the user open a new one."""
    if state.driver is not None:
        try:
            state.driver.quit()  # Release chromedriver; the browser itself is already gone
        except Exception as e:
            logger.debug(f"Error quitting the closed browser: {e}")
        state.driver = None
        state.login_completed = False
        log("🌐 Browser closed - click 'Open Browser' to start a new session", 'warning')

    hooks.update_follow_ui_state()
    hooks.update_unfollow_ui_state()


def refresh_browser_state():
    """Re-check the browser and bring both tabs' controls in line with it."""
    if state.driver is not None and not browser_is_open():
        handle_browser_closed()
    else:
        hooks.update_follow_ui_state()
        hooks.update_unfollow_ui_state()


def poll_browser():
    """The watch_browser probe body, without the GUI reschedule.

    Closing the Chrome window used to go unremarked: Start Following stayed
    enabled while every click failed, and Open Browser stayed disabled, so there
    was no way back without restarting the app.

    Only probes while no worker thread is running, to keep two threads off the
    same Selenium session. A browser closed mid-session is caught by the worker
    failing, and by the check when the session finishes.

    The GUI calls this from its own watch_browser loop; the CLI calls it once
    per status probe.
    """
    try:
        state.active_threads[:] = [t for t in state.active_threads if t.is_alive()]
        if state.driver is not None and not state.active_threads and not browser_is_open():
            handle_browser_closed()
    except Exception as e:
        logger.debug(f"watch_browser error: {e}")


def stop_bot():
    """Request graceful stop."""
    state.stop_requested.set()
    state.scoring_stop.set()
    log("⏹️ Stop requested, finishing current operation...", 'warning')
    # Both tabs' Stop buttons come here, so both acknowledge the click. The session
    # itself keeps running until it reaches a checkpoint.
    hooks.on_stop_clicked()

    # Save any already extracted users to queue if scraping was in progress
    # Only do this if we're actually in extraction mode, not during follow
    try:
        # Check if we have live extraction data (indicating extraction was in progress)
        if state.live_extracted_users:
            # Only this session's finds, best first. The frequencies file accumulates
            # across sessions, so ranking it whole would queue the entire history.
            state.last_scrape_frequencies = load_frequencies()
            ranked_users = [
                username for username, _, _ in
                rank_queue(list(dict.fromkeys(state.live_extracted_users)), state.last_scrape_frequencies)
            ]
            if ranked_users:
                new_count, total_count = add_to_queue(ranked_users)
                log(f"💾 Saved {new_count} users to queue (stop detected during extraction)", 'success')
                hooks.refresh_queue_display()
        else:
            # If we're stopping during follow (not extraction), just validate the queue
            log("🔄 Validating queue consistency after stop...", 'info')
            removed, remaining = validate_queue()
            if removed > 0:
                log(f"🗑️ Cleaned up {removed} invalid entries from queue", 'warning')
            hooks.refresh_queue_display()
    except Exception as e:
        logger.debug(f"Error during stop cleanup: {e}")
