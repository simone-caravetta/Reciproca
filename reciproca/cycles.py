"""
Reciproca - session orchestrators.

follow_cycle() and unfollow_cycle() are the parameterized versions of what the
monolith's follow_logic/unfollow_logic did around their calls into the core.
They are headless-safe: every GUI interaction has been lifted out into the
caller, either as a parameter (after_search) or a hook (decision_hook), so the
CLI and the MCP server run the exact same sessions as the GUI does.

Both return a structured result dict instead of raising through the UI, and
both take the browser for themselves with begin_session()/end_session() the
way run_follow/run_unfollow did. The GUI's wrappers still exist (gui.py) and
show the same dialogs; they just call these and render the result.
"""

import random

from reciproca import config, hooks, state
from reciproca.browser import (
    begin_session,
    browser_is_open,
    end_session,
    handle_browser_closed,
)
from reciproca.follow import follow_from_queue, run_scoring_pass
from reciproca.logging_sink import log, logger
from reciproca.persistence import load_hashtags, uf_load_progress
from reciproca.queue import (
    add_to_queue,
    load_queue,
    ranking_frequencies,
    validate_queue,
)
from reciproca.scraping import scrape_and_fill_queue
from reciproca.state import SessionStats
from reciproca.unfollow import uf_check_account, unfollow_from_list
from reciproca.utils import brief_error


def follow_cycle(mode="search", delay_min=None, delay_max=None, limit=None,
                 hashtags=None, after_search="follow", decision_hook=None):
    """Run one follow session, queue or search mode.

    `mode` "queue" follows from the saved queue; "search" scrapes the hashtags
    first. `delay_min`/`delay_max`/`limit` are the follow timing settings;
    when None they come from CONFIG (same defaults the GUI entry fields show).

    In search mode, once the scrape is done the decision of what to do with the
    results is asked: if `decision_hook` is given it is called with
    {"ranked_count", "top_freq", "hashtag_count"} and must return one of
    "follow" | "save_stop" | "discard"; otherwise `after_search` is used
    directly. The GUI passes a hook wrapping its askyesnocancel; the CLI and
    MCP pass the flag.

    Returns {"ok", "error", "report", "mode", "ranked_count", "top_freq",
    "followed", "queue_remaining", "added", "branch"}. "ok" is True whenever
    the session reached a defined end state (including a discard); "error"
    names the precondition that stopped it otherwise.
    """
    if not begin_session():
        return {"ok": False, "error": "session_busy", "report": None,
                "mode": mode, "ranked_count": 0, "top_freq": 0,
                "followed": 0, "queue_remaining": 0, "added": 0, "branch": None}

    state.stats = SessionStats(on_update=hooks.stats_handler())
    state.stop_requested.clear()

    # The same keys the GUI entry fields are prefilled from.
    if delay_min is None:
        delay_min = int(config.CONFIG["DEFAULT_DELAY_MIN"])
    if delay_max is None:
        delay_max = int(config.CONFIG["DEFAULT_DELAY_MAX"])
    if limit is None:
        limit = int(config.CONFIG["MAX_FOLLOWS_PER_SESSION"])

    result = {"ok": True, "error": None, "report": None, "mode": mode,
              "ranked_count": 0, "top_freq": 0, "followed": 0,
              "queue_remaining": 0, "added": 0, "branch": None}

    try:
        if not browser_is_open():
            log("❌ Browser not open!", 'error')
            handle_browser_closed()
            return {**result, "ok": False, "error": "browser_not_open"}

        # Progress bar always uses 0-100 percentage scale
        hooks.reset_progress()
        hooks.set_progress_maximum(100)

        if mode == 'queue':
            # Queue mode: follow from saved queue
            log(f"🚀 Starting QUEUE MODE session...")
            log(f"🎯 Target: {limit} follows from queue")

            queue = load_queue()
            if not queue:
                log("❌ Queue is empty! Switch to 'Deep Search' mode to find users.", 'error')
                return {**result, "ok": False, "error": "queue_empty"}

            log(f"📋 Queue has {len(queue)} users")

            # Follow from queue
            result["followed"] = follow_from_queue(queue, delay_min, delay_max, limit)
            result["branch"] = "queue"

        else:
            # Search mode: scrape hashtags, optionally add to queue, then follow
            if hashtags is None:
                hashtags = list(load_hashtags())
            hashtags = [h for h in hashtags if h]
            if not hashtags:
                log("❌ No hashtags selected!", 'error')
                return {**result, "ok": False, "error": "no_hashtags"}

            log(f"🚀 Starting DEEP SEARCH session...")
            log(f"Hashtags: {hashtags}")
            log(f"🛡️ Safety: Extraction will use conservative delays to avoid detection")
            log(f"💡 Tip: You can STOP anytime - extracted users are saved with rankings")
            log(f"📋 Recommended: Extract now, then follow from queue in separate sessions")

            # Scrape users
            ranked_users = scrape_and_fill_queue(hashtags, add_to_queue_limit=0)
            result["ranked_count"] = len(ranked_users)

            if not ranked_users:
                log("❌ No users found during scraping", 'error')
                return {**result, "ok": False, "error": "no_users_found",
                        "branch": "discard"}

            # Ask if user wants to add to queue or follow directly.
            # The top frequency is read from the session's own frequencies, so
            # the dialog shows what this scrape found rather than the whole
            # history.
            top_score = ranked_users[0]
            top_freq = ranking_frequencies().get(top_score, 0)
            result["top_freq"] = top_freq

            if decision_hook is not None:
                decision = decision_hook({
                    "ranked_count": len(ranked_users),
                    "top_freq": top_freq,
                    "hashtag_count": len(hashtags),
                })
            else:
                decision = after_search

            if decision == "discard":  # CANCEL - discard and stop
                log("❌ Scraping cancelled by user", 'warning')
                return {**result, "branch": "discard"}

            # Add top 500 users to queue (always, regardless of choice)
            users_to_add = ranked_users[:500]
            new_count, total_count = add_to_queue(users_to_add)
            result["added"] = new_count
            log(f"✅ Added top {len(users_to_add)} users to queue (out of {len(ranked_users)} total found)", 'success')
            log(f"📋 Queue now has {total_count} users total", 'info')
            hooks.refresh_queue_display()
            hooks.update_live_extraction_display()

            # Here, and not at the end of the search: the search is called with
            # add_to_queue_limit=0, so until the line above ran, the users it found
            # were not in the queue at all. Scoring before that read an empty queue,
            # loaded the model for nothing and scored no one.
            #
            # Both answers that keep the results get scored, including the one that
            # follows straight away - which is the whole point, since that is the
            # run whose order the scoring changes.
            try:
                run_scoring_pass(after_stop=state.stop_requested.is_set())
            except Exception as e:
                log(f"❌ Scoring pass failed: {brief_error(e)}", 'error')
                logger.exception("The scoring pass failed")

            if decision == "save_stop":  # YES - Save to queue and STOP
                log("🛑 Scraping complete. Start following manually when ready.", 'success')
                result["report"] = state.stats.report()
                return {**result, "branch": "save_stop"}

            # NO - Save to queue and START following now
            queue = load_queue()
            result["followed"] = follow_from_queue(queue, delay_min, delay_max, limit)
            result["branch"] = "follow"

        # Final report
        report = state.stats.report()
        result["report"] = report
        log(f"\n{report}", 'success')

        # Validate queue after session to ensure consistency
        removed, remaining = validate_queue()
        result["queue_remaining"] = remaining
        log(f"📋 Queue status: {remaining} users remaining (removed {removed} invalid entries)", 'info')

        return result

    except Exception as e:
        log(f"❌ Fatal error: {e}", 'error')
        logger.exception("Fatal error in follow_cycle")
        return {**result, "ok": False, "error": brief_error(e)}
    finally:
        # Releases the browser for the next session, and re-checks it rather than
        # handing back a Start button that cannot work because it was closed.
        end_session()
        hooks.refresh_queue_display()  # Update queue display
        hooks.update_live_extraction_display()  # Final update of live extraction
        if not state.stop_requested.is_set():
            hooks.reset_progress()


def unfollow_cycle(delay_min=None, delay_max=None, limit=None):
    """Run one unfollow session over the loaded non-followers list.

    Resumes where the progress file says work stopped: the already-processed
    users are skipped, so a session killed mid-run carries on from where it
    left off the next time it is called.

    Returns {"ok", "error", "report", "processed", "unfollowed"}.
    """
    if not begin_session():
        return {"ok": False, "error": "session_busy", "report": None,
                "processed": 0, "unfollowed": 0}

    state.uf_stats = SessionStats(on_update=hooks.unfollow_stats_handler())
    state.stop_requested.clear()

    # The same keys the GUI entry fields are prefilled from.
    if delay_min is None:
        delay_min = int(config.CONFIG["UNFOLLOW_DELAY_MIN"])
    if delay_max is None:
        delay_max = int(config.CONFIG["UNFOLLOW_DELAY_MAX"])
    if limit is None:
        limit = int(config.CONFIG["UNFOLLOW_DAILY_LIMIT"])

    result = {"ok": True, "error": None, "report": None, "processed": 0,
              "unfollowed": 0}

    try:
        if not browser_is_open():
            log("❌ Browser not open!", 'error')
            handle_browser_closed()
            return {**result, "ok": False, "error": "browser_not_open"}

        # The account may have been switched in the browser since the files were
        # loaded, which would make this session write progress against the wrong
        # following list.
        uf_check_account()

        if not state.uf_non_followers:
            log("❌ No data loaded! Load followers.json and following.json first", 'error')
            return {**result, "ok": False, "error": "no_data"}

        uf_load_progress()
        to_process = [u for u in state.uf_non_followers if u not in state.uf_progress["processed"]]

        if not to_process:
            log("✔ All non-followers have already been processed", 'success')
            return {**result, "ok": False, "error": "all_processed"}

        log(f"🚀 Starting UNFOLLOW: {len(to_process)} users left to process")

        hooks.reset_unfollow_progress()

        random.shuffle(to_process)

        unfollowed = unfollow_from_list(to_process, delay_min, delay_max, limit)

        report = state.uf_stats.report()
        result["report"] = report
        result["unfollowed"] = unfollowed
        result["processed"] = len(to_process)
        log(f"\n{report}", 'success')
        return result

    except Exception as e:
        log(f"❌ Fatal error: {e}", 'error')
        logger.exception("Fatal error in unfollow_cycle")
        return {**result, "ok": False, "error": brief_error(e)}
    finally:
        # Releases the browser for the next session, re-checks it in case it was
        # closed while this ran.
        end_session()
        if not state.stop_requested.is_set():
            hooks.reset_unfollow_progress()
