"""
Reciproca - scraping: walking a hashtag grid, opening posts, reading the
authors' followers lists, and the whole scrape-and-fill-queue orchestration.
"""

import random
import time
from collections import Counter
from datetime import datetime

from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from reciproca import config, hooks, state
from reciproca.browser import check_rate_limit
from reciproca.follow import follow_author
from reciproca.logging_sink import log, logger
from reciproca.markers import CLOSE_BUTTON_LABELS, FOLLOWING_BUTTON_MARKERS
from reciproca.persistence import (
    is_already_followed,
    load_account_username,
    load_author_history,
    load_frequencies,
    order_authors_by_staleness,
    save_author_history,
    save_frequencies,
)
from reciproca.queue import add_to_queue
from reciproca.selectors import EXTRACT_FOLLOWERS_JS, POST_LINKS_JS
from reciproca.utils import brief_error, parse_follower_count, pause, retry, wait_for_element


@retry()
def get_author_profile():
    """Get profile URL from open post."""
    try:
        author = wait_for_element(
            state.driver,
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
    hrefs = state.driver.execute_script(POST_LINKS_JS)
    if hrefs is not None:
        return hrefs

    log("⚠️ A post was still open over the grid, closing it", 'warning')
    close_post()
    return state.driver.execute_script(POST_LINKS_JS)


@retry()
def open_post(href):
    """Click the grid tile for one post, finding it at the moment of use.

    References held from earlier are invalidated by post dialogs opening and closing,
    and Instagram can redraw a tile between it being found and clicked, which is what
    the retry covers. A tile that is simply not there raises and is not retried.
    """
    post = state.driver.find_element(By.CSS_SELECTOR, f'a[href="{href}"]')
    state.driver.execute_script("arguments[0].scrollIntoView();", post)
    time.sleep(random.uniform(0.5, 1.5))  # Randomized scroll delay
    state.driver.execute_script("arguments[0].click();", post)


def post_dialog_open():
    """True while a post is open over the grid."""
    try:
        return bool(state.driver.find_elements(By.XPATH, "//div[@role='dialog']"))
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

        ActionChains(state.driver).send_keys(Keys.ESCAPE).perform()
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
            for btn in state.driver.find_elements(By.XPATH, selector):
                try:
                    state.driver.execute_script("arguments[0].click();", btn)
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
        if state.driver.current_window_handle != main_window:
            state.driver.close()
    except WebDriverException as e:
        logger.debug(f"Could not close the extra window: {e}")

    try:
        state.driver.switch_to.window(main_window)
    except WebDriverException as e:
        logger.debug(f"Could not switch back to the main window: {e}")


@retry()
def open_followers_popup():
    """Open followers popup on profile page."""
    try:
        # Wait for profile to load
        wait_for_element(state.driver, By.TAG_NAME, "header")
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
                    followers_link = state.driver.find_element(By.XPATH, selector)
                else:
                    followers_link = state.driver.find_element(By.CSS_SELECTOR, selector)
                if followers_link:
                    break
            except:
                continue

        # Fallback: search all links
        if not followers_link:
            links = state.driver.find_elements(By.TAG_NAME, "a")
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

            state.driver.execute_script("arguments[0].click();", followers_link)

            # Wait for dialog with better timeout
            wait_for_element(state.driver, By.XPATH, "//div[@role='dialog']")

            # IMPORTANT: Wait longer for the scrollable content to load
            time.sleep(3.5)

            # Wait for user links to appear
            try:
                WebDriverWait(state.driver, 10).until(
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
    original_url = state.driver.current_url

    try:
        dialog = state.driver.find_element(By.XPATH, "//div[@role='dialog']")

        # Try to click "See all" / "See all suggestions" button if present
        see_all_clicked = False
        try:
            see_all_btn = state.driver.execute_script("""
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
                state.driver.execute_script("arguments[0].click();", see_all_btn)
                time.sleep(3)  # Wait longer for full list to load
                see_all_clicked = True
        except:
            pass

        # Check if clicking "See all" caused navigation to /explore/people/
        if see_all_clicked and "/explore/people" in state.driver.current_url:
            log("⚠️ Navigation to /explore/people detected, going back...", 'warning')
            state.driver.back()
            time.sleep(2)
            # Re-open followers popup
            if not open_followers_popup():
                log("❌ Failed to reopen followers popup after navigation", 'error')
                return []
            # Try extraction without clicking "See all" this time
            log("🔄 Retrying extraction without 'See all' button...")
            # Skip the see_all click by removing the button from DOM
            try:
                state.driver.execute_script("""
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
            suggestions_btn = state.driver.execute_script("""
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
                state.driver.execute_script("arguments[0].click();", suggestions_btn)
                time.sleep(3)
                log("✅ Expanded to full followers list")
        except Exception as e:
            logger.debug(f"Post-load suggestions button check: {e}")

        # Find the scrollable container first
        scrollable_container = state.driver.execute_script("""
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

        for i in range(config.CONFIG["FOLLOWER_SCROLL_COUNT"]):
            if state.stop_requested.is_set():
                break

            # Strategy 1: Scroll the container
            try:
                if scrollable_container:
                    # Scroll the found container by a larger amount
                    state.driver.execute_script("""
                        arguments[0].scrollBy(0, 800);
                    """, scrollable_container)
                else:
                    # Fallback: scroll the dialog
                    state.driver.execute_script("""
                        let dialog = document.querySelector("div[role='dialog']");
                        if (dialog) dialog.scrollBy(0, 800);
                    """)
                scroll_count += 1
            except Exception as e:
                logger.debug(f"Scroll error: {e}")

            # Strategy 2: Every 3rd scroll, scroll the last user into view (triggers lazy loading)
            if i % 3 == 0:
                try:
                    state.driver.execute_script(r"""
                        let dialog = document.querySelector("div[role='dialog']");
                        if (!dialog) return;

                        // Find all user links - look for links in the list
                        let links = dialog.querySelectorAll('a[href^="/"]');
                        let userLinks = [];

                        for (let link of links) {
                            let href = link.getAttribute('href') || '';
                            // Filter for actual user profile links
                            let match = href.match(/^\/([^\/]+)\/?$/);
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
                    suggestions_btn = state.driver.execute_script("""
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
                        state.driver.execute_script("arguments[0].click();", suggestions_btn)
                        time.sleep(3)  # Wait longer for full list to load
                        log("✅ Full list loaded, continuing scroll...")
                except Exception as e:
                    logger.debug(f"Suggestions button check error: {e}")

            # Wait longer for content to load (Instagram needs time to fetch new users)
            # SAFETY: Longer delays between scrolls to appear more human
            time.sleep(random.uniform(3.0, 5.0))

            # Check actual user count by extracting and counting unique users
            try:
                current_users = state.driver.execute_script(r"""
                    let dialog = document.querySelector("div[role='dialog']");
                    if (!dialog) return [];

                    let links = dialog.querySelectorAll('a[href^="/"]');
                    let users = [];
                    for (let link of links) {
                        let href = link.getAttribute('href') || '';
                        let match = href.match(/^\/([^\/]+)\/?$/);
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
                log(f"  Scrolled {i+1}/{config.CONFIG['FOLLOWER_SCROLL_COUNT']} (found ~{last_user_count} users)")

            # Update UI progress
            if i % 3 == 0:
                hooks.update_progress(
                    i + 1,
                    config.CONFIG["FOLLOWER_SCROLL_COUNT"],
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
            result = state.driver.execute_script(
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
            skipped_own = 0
            skipped_history = 0
            # Third net, and the one that has to hold whatever the buttons say:
            # the login account itself. Following an author puts our own row at
            # the top of their followers list, and that row's button does not
            # mark it as ours, so it is compared by name - the username
            # captured from the login form - before anything is returned, so it
            # never reaches the live extraction or the queue.
            own_username = load_account_username()
            for user in candidates:
                if own_username and user.lower() == own_username.lower():
                    skipped_own += 1
                    log(f"⏭️ Skipped own account: {user}", 'info')
                    logger.debug(f"Filtered out the login account itself: {user}")
                elif is_already_followed(user):
                    skipped_history += 1
                    logger.debug(f"Filtered out already followed user (history): {user}")
                else:
                    filtered_users.append(user)

            why = f"{skipped_following} by button, {skipped_history} by history"
            if skipped_own:
                why += f", {skipped_own} own account"
            log(
                f"📊 Extracted {len(filtered_users)} candidates "
                f"({rows_inspected} rows, skipped {skipped_following + skipped_history + skipped_own} "
                f"already followed: {why})"
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


def scrape_and_fill_queue(hashtags, add_to_queue_limit=0):
    """Scrape users from hashtags and optionally add to queue."""
    # Reset live extraction tracking for new scrape session
    state.live_extracted_users = []
    state.live_frequencies = Counter()

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

        visited_authors.add(username)
        author_count += 1
        total_authors_processed += 1
        log(f"👤 Author {author_count}/{config.CONFIG['TARGET_AUTHORS_PER_HASHTAG']}: {username}")

        # The author's profile is read in a window of its own, so the hashtag grid
        # keeps its scroll position. Which window to come back to is remembered
        # rather than assumed to be the first one.
        grid_window = state.driver.current_window_handle

        state.driver.execute_script("window.open(arguments[0]);", profile_url)
        state.driver.switch_to.window(state.driver.window_handles[-1])

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

            # The author's page is open anyway, and we are already on it: follow
            # them before the followers popup takes over the window. The balance
            # check runs here, not at follow time, because the author is never
            # queued - this is the only visit.
            if config.CONFIG["AUTHOR_FOLLOW_ENABLED"]:
                follow_author(username)

            if open_followers_popup():
                users = extract_users_from_followers(
                    current_hashtag=hashtag,
                    author_num=author_count,
                    total_authors=config.CONFIG["TARGET_AUTHORS_PER_HASHTAG"],
                    author_name=username
                )
                all_users.extend(users)

                # Update frequencies incrementally for this author
                current_frequencies.update(users)
                state.last_scrape_frequencies = previous_frequencies + current_frequencies
                save_frequencies(state.last_scrape_frequencies)

                log(f"📊 Author {username}: {len(users)} followers extracted (total unique: {len(current_frequencies)})")

                # Update live extraction display for real-time feedback
                state.live_extracted_users.extend(users)
                state.live_frequencies = current_frequencies.copy()  # Mirror current frequencies
                hooks.update_live_extraction_display()

                # Skip authors with very high follower counts to avoid throttling
                if len(users) < 5:
                    log(f"⏭️ Skipping future posts from {username} (too few extracted, likely throttled)", 'warning')

                # Anti-throttling: detect low extraction as rate limiting signal
                if len(users) < 25:
                    throttle_cooldown_count += 1
                    if throttle_cooldown_count >= 2:
                        cooldown = random.uniform(8, 12)
                        log(f"🐢 Throttling detected ({throttle_cooldown_count} low counts), cooling down for {cooldown:.0f}s...", 'warning')
                        pause(cooldown)
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
        if author_count % config.CONFIG["AUTHORS_BEFORE_COOLDOWN"] == 0:
            cooldown = config.CONFIG["COOLDOWN_DURATION"] + random.uniform(0, 5)
            log(f"🛡️ Safety cooldown: {cooldown:.0f}s after {author_count} authors...", 'info')
            pause(cooldown)

        # Random delay between authors - safer range
        pause(random.uniform(4, 8))

    total_hashtags = len(hashtags)
    for hashtag_idx, kw in enumerate(hashtags, 1):
        if state.stop_requested.is_set():
            log("⏹️ Scraping stopped by user", 'warning')
            break

        log(f"\n🔍 Processing hashtag: #{kw}", 'info')

        hooks.update_progress(
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

        state.driver.get(f"https://www.instagram.com/explore/tags/{kw}/")
        time.sleep(random.uniform(2.5, 4.0))  # Randomized initial wait

        if check_rate_limit(state.driver):
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

        while (len(visited_authors) < config.CONFIG["TARGET_AUTHORS_PER_HASHTAG"]
               and not state.stop_requested.is_set()):

            hrefs = collect_post_links()
            if hrefs is None:
                stop_reason = "a post would not close, so the grid could not be read"
                break

            fresh = [href for href in hrefs if href not in visited_posts]

            if not fresh:
                if scroll_count >= config.CONFIG["MAX_SCROLLS_PER_HASHTAG"]:
                    stop_reason = f"hit the {config.CONFIG['MAX_SCROLLS_PER_HASHTAG']}-scroll ceiling"
                    break

                state.driver.execute_script(
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
                if len(visited_authors) >= config.CONFIG["TARGET_AUTHORS_PER_HASHTAG"]:
                    break

                if state.stop_requested.is_set():
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
                and len(visited_authors) < config.CONFIG["TARGET_AUTHORS_PER_HASHTAG"]
                and not state.stop_requested.is_set()):
            reusable = order_authors_by_staleness(deferred_authors, author_history)
            short_by = config.CONFIG["TARGET_AUTHORS_PER_HASHTAG"] - len(visited_authors)
            log(
                f"♻️ #{kw}: {len(visited_authors)} new authors, {short_by} short "
                f"({stop_reason}). Reusing {min(short_by, len(reusable))} of the "
                f"{len(reusable)} held back, least recently scraped first",
                'info'
            )
            for username in reusable:
                if (len(visited_authors) >= config.CONFIG["TARGET_AUTHORS_PER_HASHTAG"]
                        or state.stop_requested.is_set()):
                    break
                try:
                    scrape_author(username, deferred_authors[username], kw)
                except Exception as e:
                    log(f"❌ Author error for {username}: {brief_error(e)}", 'error')
                    logger.debug(f"Author error for {username}", exc_info=True)

        log(f"🎯 #{kw}: {len(visited_authors)} authors scraped from {posts_opened} posts opened")

        # Anti-throttling: longer break between hashtags
        if hashtag_idx < total_hashtags:
            break_time = config.CONFIG["HASHTAG_BREAK_DURATION"] + random.uniform(0, 10)
            log(f"☕ Safety break between hashtags: {break_time:.0f}s...", 'info')
            pause(break_time)

    # Which users to offer for the queue: this session's finds, best first. The
    # accumulated counter is for ranking the queue, not for deciding what to add,
    # so a long history cannot crowd out what was just found.
    ranked_users = [u for u, _ in current_frequencies.most_common()]

    # Ensure global frequencies are up to date
    state.last_scrape_frequencies = previous_frequencies + current_frequencies
    save_frequencies(state.last_scrape_frequencies)

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
        hooks.refresh_queue_display()

    return ranked_users
