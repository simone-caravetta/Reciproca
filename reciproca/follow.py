"""
Reciproca - the follow side: reading a profile (stats, bot filter, candidate
description), following one user or one author, and the queue loop. The three
profile readers live here rather than in scraping.py so that follow does not
import scraping; scraping imports follow (for follow_author), never the other
way round.
"""

import random
import time

from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By

from reciproca import config, hooks, state
from reciproca.browser import check_rate_limit
from reciproca.logging_sink import log, logger
from reciproca.markers import (
    FOLLOW_BUTTON_MARKERS,
    FOLLOWED_SIGNAL_MARKERS,
    FOLLOWING_BUTTON_MARKERS,
    FOLLOWERS_LABEL_MARKERS,
    FOLLOWING_LABEL_MARKERS,
    POSTS_LABEL_MARKERS,
)
from reciproca.persistence import is_already_followed, log_followed_user
from reciproca.queue import rank_queue, remove_from_queue, score_queue, trim_queue
from reciproca.selectors import PROFILE_STATS_JS
from reciproca.semantic import make_affinity_scorer, profile_description, semantic_model
from reciproca.utils import (
    author_rejection_reason,
    bot_rejection_reason,
    brief_error,
    count_from_links,
    counts_agree,
    has_marker,
    is_follow_button,
    parse_labelled_count,
    wait_for_element,
)


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
            current_url = state.driver.current_url.rstrip("/")
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
        buttons = state.driver.find_elements(By.TAG_NAME, "button")

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
                header = state.driver.find_element(By.XPATH, header_xpath)
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
        all_buttons = state.driver.find_elements(By.TAG_NAME, "button")
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
        div_buttons = state.driver.find_elements(By.XPATH, "//div[@role='button']")
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


def read_profile_stats():
    """(posts, followers, following) for the profile in the browser, None where unread.

    Never raises. A profile whose numbers cannot be read has to stay followable.
    """
    try:
        raw = state.driver.execute_script(PROFILE_STATS_JS)
    except WebDriverException as e:
        logger.info(f"Could not read profile stats: {type(e).__name__}")
        return None, None, None

    if not raw:
        return None, None, None

    header_text = raw.get('headerText') or ""

    def read(entries, markers, name):
        """One count, from its link and from the header text, only if they agree.

        Two independent routes, because one link that cannot be found used to mean
        the check simply did not happen. Where both answer they are checked against
        each other rather than one silently winning: they are anchored on different
        things, so a disagreement means the header is not what this code thinks it
        is, and a number nobody can vouch for must not reject an account. That is
        the same rule the filter already applies to a count it could not read at
        all - the difference here is that the misreading looks like an answer.
        """
        from_link = count_from_links(entries, markers)
        from_text = parse_labelled_count(header_text, markers)

        if from_link is None:
            return from_text
        if from_text is None:
            return from_link
        if counts_agree(from_link, from_text):
            # The link is the precise one where the rendered text was abbreviated.
            return from_link

        log(
            f"⚠️ Two different {name} counts on this profile "
            f"({from_link} beside the link, {from_text} in the header) - "
            f"that check was skipped",
            'warning'
        )
        return None

    return (
        parse_labelled_count(header_text, POSTS_LABEL_MARKERS),
        read(raw.get('followers'), FOLLOWERS_LABEL_MARKERS, "follower"),
        read(raw.get('following'), FOLLOWING_LABEL_MARKERS, "following"),
    )


def read_profile_stats_retried():
    """Profile counts with a short retry, since the header can render before them.

    Returns (posts, followers, following), None where a count stayed unread.
    """
    for attempt in range(3):
        posts, followers, following = read_profile_stats()
        if None not in (posts, followers, following):
            break
        if attempt < 2:
            time.sleep(1)
    return posts, followers, following


def profile_bot_reason():
    """Why the profile in the browser looks automated, or None to follow it."""
    posts, followers, following = read_profile_stats_retried()

    missing = [
        name for name, value in
        (("posts", posts), ("followers", followers), ("following", following))
        if value is None
    ]

    # Fail open, but never quietly: an unread count takes no part in the decision, so
    # silence here is indistinguishable from a profile that passed.
    if len(missing) == 3:
        log("⚠️ Could not read any of this profile's counts - bot filter skipped", 'warning')
        return None
    if missing:
        log(f"⚠️ Could not read {' or '.join(missing)} here - that check was skipped", 'warning')

    reason = bot_rejection_reason(posts, followers, following)
    logger.info(
        f"Profile stats: posts={posts} followers={followers} "
        f"following={following} -> {reason or 'ok'}"
    )
    return reason


def read_candidate_profile(username):
    """Open a candidate's profile and read what it says about itself.

    Comes back as the (name, category, bio) that make_affinity_scorer wants. Only
    the last is filled in: the header is one run of text, and profile_description()
    takes the part of it that describes the account rather than counting it.

    Never raises, and never guesses. A page that will not load, or that Instagram
    answers with somebody else's profile, leaves the candidate unscored - which
    keeps their sighting count and their place, rather than recording a number
    about a profile that was never read.
    """
    nothing = (None, None, None)
    header_text = None

    try:
        state.driver.get(f"https://www.instagram.com/{username}/")
        wait_for_element(state.driver, By.TAG_NAME, "header")

        # Instagram answers a visit to some profiles with a page of suggestions.
        # Reading that would score this candidate on a stranger's bio.
        if username.lower() not in state.driver.current_url.lower():
            logger.info(f"Asked for {username} and landed somewhere else, leaving it unscored")
            return nothing

        # Asked for repeatedly until the counts are in it, rather than waited out on
        # a timer. The header element exists before its contents do, so something
        # has to give the page a moment - but a fixed pause is the wrong shape for
        # it, since it is both too long on nearly every profile and too short on the
        # slow one. The counts are the sign that the header has filled in.
        for attempt in range(12):
            raw = state.driver.execute_script(PROFILE_STATS_JS) or {}
            header_text = raw.get('headerText') or ""
            if parse_labelled_count(header_text, FOLLOWING_LABEL_MARKERS) is not None:
                break
            time.sleep(0.25)
    except WebDriverException as e:
        logger.info(f"Could not read {username}: {type(e).__name__}")
        return nothing
    finally:
        # Reading a profile is not following one, so this is a fraction of what the
        # follow delays are, and it can be set to nothing. It is not zero by default
        # because several hundred profile views in a row, as fast as a machine can
        # ask for them, is the shape of the thing Instagram is watching for.
        pace = max(0, config.CONFIG["SEMANTIC_READ_DELAY"])
        if pace:
            time.sleep(random.uniform(pace, pace * 2))

    if not header_text:
        # A header that never filled in is where a block would be showing, so this
        # is the one place worth paying for the check - the page text it reads costs
        # more than the pause above, which is why it is not run on every profile.
        if check_rate_limit(state.driver):
            log("⏸️ Instagram is throttling, waiting a minute before carrying on", 'warning')
            time.sleep(60)
        return nothing

    return None, None, profile_description(header_text)


def run_scoring_pass(after_stop=False):
    """Hold the queue to size, then read and score everyone left in it.

    Runs at the end of a search rather than during a follow session. The browser is
    already up and already reading heavily here, and a follow session stays exactly
    as quick as it has always been.

    `after_stop` says the search was cut short by hand. That is not a reason to skip
    this: a stopped search has still put everything it found into the queue, and
    that queue wants sorting as much as a finished one does - more, really, since it
    is the one about to be worked through. So the pass gets its own stop flag and
    clears it here, and the next press of Stop is what ends the scoring as well.

    Every way of not being able to score leaves the queue ordered on sighting counts
    alone, which is what it was ordered on before any of this existed. None of them
    is an error.
    """
    if not config.CONFIG["SEMANTIC_ENABLED"]:
        return

    state.scoring_stop.clear()
    if after_stop:
        log("🧭 Search stopped. Scoring what it found - press Stop again to skip", 'info')

    niche = str(config.CONFIG.get("SEMANTIC_NICHE") or "").strip()
    if not niche:
        log("ℹ️ No niche written in the settings, so nothing is scored", 'info')
        return

    scorer = make_affinity_scorer(read_candidate_profile, semantic_model.embed, niche)
    if scorer is None:
        log("ℹ️ Semantic ranking is off, the queue keeps its order by sightings", 'info')
        return

    # Held to size first, then every one of those read. The other way round - read
    # the best few hundred, then keep more of them than that - leaves a queue where
    # some entries have been read and some have not, and those two are not ordered
    # on the same thing. One number, and everybody in the queue has been looked at.
    dropped = trim_queue()
    if dropped:
        log(
            f"✂️ Kept the top {config.CONFIG['SEMANTIC_TOP_K']}, so {dropped} candidates "
            f"left the queue. Their sighting counts stay on file, so a later search "
            f"that finds them again picks up where this one left off",
            'info'
        )

    def on_progress(number, total, username):
        if number == 1 or number % 25 == 0:
            log(f"🧭 Scoring {number}/{total}...", 'info')

    scored = score_queue(scorer, on_progress=on_progress)
    if scored:
        log(f"🧭 Scored {scored} profiles against your niche", 'success')

    try:
        hooks.refresh_queue_display()
    except Exception as e:
        logger.debug(f"Could not refresh the queue display: {e}")


def follow_author(username):
    """Follow the author whose profile is open in the current window.

    The author's page is visited anyway for their followers, so the follow costs
    no extra page load - it only checks the button and the counts already on
    screen. Followed only when the profile looks like it would follow back: not
    already followed, and with a balanced following/followers ratio. A rejected
    author is not written to the history, so a later session can re-evaluate
    them - ratios change, and the point is the follow-back, not a verdict.

    Never raises: a failure here must not cost the author's follower extraction.
    """
    try:
        state.stats.increment('attempted')

        # Wait for profile
        wait_for_element(state.driver, By.TAG_NAME, "header")
        time.sleep(1)  # Let page settle

        if is_already_followed(username):
            log(f"⏭️ Author {username} already in history - not following", 'warning')
            return False, "already_followed"

        btn, status = find_follow_button()

        if status == "already_following":
            log(f"⏭️ Author {username} already following", 'warning')
            log_followed_user(username, "already_following")
            return False, "already_following"

        if status != "follow" or not btn:
            log(f"⚠️ Author {username} | no follow button ({status})", 'warning')
            return False, status or "no_button"

        posts, followers, following = read_profile_stats_retried()
        author_reason = author_rejection_reason(posts, followers, following)
        if author_reason:
            log(f"🚫 Author {username} skipped | {author_reason}", 'warning')
            return False, "filtered_author"

        # Store button reference for validation comparison
        original_btn = btn

        # Try to click the button
        try:
            state.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
            time.sleep(0.5)
            state.driver.execute_script("arguments[0].click();", btn)
        except Exception as e:
            # Fallback to regular click
            try:
                btn.click()
            except:
                return False, f"click_failed: {e}"

        # Verify success with original button reference
        if validate_follow_success(original_btn, username):
            state.stats.increment('succeeded')
            log_followed_user(username, "success")
            log(f"✅ Followed author {username}", 'success')
            return True, None
        else:
            # Still might have worked, check button again after extra wait
            time.sleep(2)
            _, new_status = find_follow_button()
            if new_status == "already_following":
                state.stats.increment('succeeded')
                log_followed_user(username, "success")
                log(f"✅ Followed author {username}", 'success')
                return True, None
            state.stats.increment('errors')
            return False, "validation_failed"

    except Exception as e:
        state.stats.increment('errors')
        logger.debug(f"follow_author failed for {username}: {e}", exc_info=True)
        return False, str(e)


def follow_user(username, delay_min, delay_max):
    """Follow a single user with validation."""
    try:
        state.stats.increment('attempted')
        target_url = f"https://www.instagram.com/{username}/"

        state.driver.get(target_url)

        # Wait for profile
        wait_for_element(state.driver, By.TAG_NAME, "header")
        time.sleep(1)  # Let page settle

        # CRITICAL: Verify we landed on the correct profile
        # Instagram may redirect private profile visitors to suggested accounts
        current_url = state.driver.current_url.rstrip("/")
        if username not in current_url:
            log(f"⚠️ Redirect detected! Expected {username}, got redirect. Skipping...", 'warning')
            return False, "redirected"

        if check_rate_limit(state.driver):
            log(f"⚠️ Rate limit warning detected - continuing anyway (dev mode)", 'warning')
            # Don't stop - just warn and continue
            # stats.increment('skipped_rate_limited')
            # return False, "rate_limited"

        # Find follow button
        btn, status = find_follow_button()

        if status == "not_found":
            return False, "no_button"

        if status == "already_following":
            state.stats.increment('skipped_already_following')
            return False, "already_following"

        if status == "error":
            return False, "button_error"

        if status == "follow" and btn:
            # Checked here rather than on arrival: a profile already followed, or one
            # with no button, is settled without needing its counts, and would
            # otherwise be recorded as filtered instead.
            if config.CONFIG["BOT_FILTER_ENABLED"]:
                bot_reason = profile_bot_reason()
                if bot_reason:
                    log(f"🤖 Skip {username} | looks automated: {bot_reason}", 'warning')
                    return False, "filtered_bot"

            # Store button reference for validation comparison
            original_btn = btn

            # Try to click the button
            try:
                state.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                time.sleep(0.5)
                state.driver.execute_script("arguments[0].click();", btn)
            except Exception as e:
                # Fallback to regular click
                try:
                    btn.click()
                except:
                    return False, f"click_failed: {e}"

            # Verify we stayed on the correct profile after clicking
            # (button click may cause navigation to suggestions)
            time.sleep(1)
            final_url = state.driver.current_url.rstrip("/")
            if username not in final_url:
                log(f"⚠️ Navigation after click! Expected {username}, now at different page. Skipping...", 'warning')
                return False, "navigation_after_click"

            # Verify success with original button reference
            if validate_follow_success(original_btn, username):
                state.stats.increment('succeeded')
                return True, None
            else:
                # Still might have worked, check button again after extra wait
                time.sleep(2)
                _, new_status = find_follow_button()
                if new_status == "already_following":
                    state.stats.increment('succeeded')
                    return True, None
                state.stats.increment('errors')
                return False, "validation_failed"

        return False, f"unexpected_status: {status}"

    except Exception as e:
        state.stats.increment('errors')
        return False, str(e)


def follow_from_queue(users_to_follow, delay_min, delay_max, limit):
    """Follow users from the queue with batch safety cooldowns."""
    successful = 0
    skipped_already_followed = 0
    batch_count = 0

    # Consume in rank order, highest first - the same order rank_queue() gives the
    # listbox, so the next account followed is the top one on screen. Ranking here
    # rather than at the call sites means neither queue mode nor deep search can
    # bypass it.
    ranked = rank_queue(users_to_follow)
    usernames = [username for username, _, _ in ranked]

    log(f"📋 Following from queue: {len(usernames)} users available")
    if ranked:
        preview = ", ".join(f"{u} [{rank}]" for u, rank, _ in ranked[:10])
        log(f"🏆 Follow order by rank: {preview}{' ...' if len(ranked) > 10 else ''}", 'info')
    log(f"🎯 Target: {limit} follows this session")
    log(f"🛡️ Safety: {config.CONFIG['FOLLOW_BATCH_SIZE']} follows per batch, {config.CONFIG['FOLLOW_BATCH_COOLDOWN']//60}min cooldown between batches")

    # Progress bar uses percentage (0-100)
    hooks.set_progress_maximum(100)

    for i, user in enumerate(usernames):
        if state.stop_requested.is_set():
            log("⏹️ Stopped by user", 'warning')
            break

        if successful >= limit:
            log(f"✅ Reached target of {limit} follows", 'success')
            break

        # SAFETY: Batch cooldown check
        if batch_count >= config.CONFIG["FOLLOW_BATCH_SIZE"] and successful > 0:
            cooldown = config.CONFIG["FOLLOW_BATCH_COOLDOWN"]
            minutes = cooldown // 60
            log(f"🛡️ BATCH COOLDOWN: {minutes} minute break after {config.CONFIG['FOLLOW_BATCH_SIZE']} follows...", 'warning')
            # Break cooldown into chunks to allow stop detection
            for _ in range(cooldown):
                if state.stop_requested.is_set():
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
            hooks.update_progress(successful, limit, phase="following_users")
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
            elif reason == "filtered_bot":
                # follow_user already logged which signal rejected it. Recorded in the
                # history so the queue drops it and later extractions do not re-add it:
                # the verdict would not change on a second visit, and re-checking would
                # cost another profile load every session.
                remove_from_queue(user)
                log_followed_user(user, "filtered_bot")
            else:
                log(f"⚠️ Skip {user} | {reason}", 'warning')
                # Don't remove from queue on error - can retry next time

        # Check if stop was requested during the follow operation
        if state.stop_requested.is_set():
            log("🛑 Stop detected during follow loop, breaking gracefully...", 'warning')
            break

        # Random delay between follows
        delay = random.uniform(delay_min, delay_max)
        log(f"⏱️ Waiting {delay:.1f}s...", 'info')

        # Break delay into chunks to check stop_requested
        for _ in range(int(delay)):
            if state.stop_requested.is_set():
                break
            time.sleep(1)
        time.sleep(delay % 1)

    return successful
