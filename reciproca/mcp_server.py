"""
Reciproca - MCP server (stdio).

Exposes the deterministic operations of the core as MCP tools, so an agent
(or any MCP client) can run and watch sessions on top of the same engine the
CLI and the GUI use. Run with:

    python -m reciproca.mcp_server

Every tool returns the same envelope - {"ok": true/false, "error": ...} plus
tool-specific payload - and raw exceptions never cross the wire: a failing
core call comes back as {"ok": false, "error": "..."} instead of a crash.

Long-running cycles (follow, unfollow, queue scoring) do not block the tool
call. They run on daemon worker threads registered in TASKS; the tool returns
immediately with a task_id and the caller polls cycle_status until it is
done. Short tools run synchronously.

Mutual exclusion: follow and unfollow drive the same Selenium session and the
same Chrome window, so only one may run at a time. follow_cycle and
unfollow_run are refused while a session is running, and Chrome's own profile
lock refuses a second browser while the first is open - both come back as
clean {"ok": false, "error": ...} results.

The browser is long-lived here (the server process is long-lived), and login
is manual and persists in chrome_profile/. The agent flow is: browser_open
(visible, once) -> login_wait (or ask the user to log in) -> follow_cycle ->
poll cycle_status -> status / logs_tail when done.

Nothing here performs a per-account action on its own: every individual
follow decision stays in the deterministic core (queue ranking, bot filter,
delays, rate limits). The tools only start, watch and stop sessions.
"""

import functools
import json
import os
import subprocess
import threading
import time
import uuid
import warnings

# pydantic-settings (a transitive dep of the mcp SDK) warns once about a
# forward reference inside mcp's own settings model. It is not ours to fix
# and it clutters every run of the server and its clients.
warnings.filterwarnings(
    "ignore", message=r"Field 'lifespan' has an incomplete definition.*")

from mcp.server.fastmcp import FastMCP

from reciproca import browser, config, cycles, follow, persistence, semantic, state
from reciproca import queue as queue_mod
from reciproca import unfollow
from reciproca.logging_sink import RECENT, log
from reciproca.utils import brief_error

mcp = FastMCP(
    name="reciproca",
    instructions=(
        "Control a Reciproca Instagram growth-testing session: browser, "
        "follow and unfollow cycles, queue, hashtags, config, status. "
        "One browser, one session at a time. Long cycles are asynchronous: "
        "start them with follow_cycle / unfollow_run and poll cycle_status "
        "until it reports done. Individual follow decisions are made by the "
        "core engine, never by the caller. Destructive tools (queue_clear, "
        "queue_trim, unfollow_reset, config_set) should be confirmed with "
        "the user first."
    ),
)


# ---------------------------------------------------------------------------
# envelope helpers
# ---------------------------------------------------------------------------

def _ok(**payload):
    """A successful tool result: {"ok": true, "error": null, ...payload}."""
    return {"ok": True, "error": None, **payload}


def _fail(error, **payload):
    """A failed tool result: {"ok": false, "error": "...", ...payload}."""
    return {"ok": False, "error": error, **payload}


def _safe(fn):
    """Turn any uncaught exception into the error envelope.

    Every tool is wrapped with this: the core raises on its own terms (a
    Selenium failure, a corrupt file), and those must surface to the agent as
    data, not as a crashed tool call.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            log(f"❌ MCP tool {fn.__name__} failed: {brief_error(e)}", 'error')
            return _fail(brief_error(e))
    return wrapper


def _chrome_window_on_profile():
    """True when a Chrome window is running on the app's profile.

    A browser opened by an earlier process survives that process, but the
    driver - and with it the login flag - dies with it, so this process
    cannot attach. Reported separately from the driver, like the CLI does.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={config.CHROME_PROFILE_DIR}"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def _ensure_browser(headless, login_timeout):
    """Open the browser if needed and wait for the login to be in place.

    Returns (ok, error). Mirrors the CLI's ensure_browser: an already-open
    browser is presumed logged in (the login form would have blocked the
    session otherwise), and a headless open only works with a login already
    saved in chrome_profile/.
    """
    if state.driver is not None:
        return True, None
    if not browser.can_open_browser():
        return False, "Another instance is already opening Chrome - the profile is single-use."
    if headless:
        log("Opening headless Chrome - this only works with a login already saved in chrome_profile/")
    browser.open_browser(headless=headless)
    if state.driver is None:
        if _chrome_window_on_profile():
            return False, (
                "A Chrome window from another command is still open and holds the profile.\n"
                "Close it, then retry."
            )
        return False, (
            "Chrome did not open - see follow_bot.log.\n"
            "If a Chrome window is still open from another command, close it and retry."
        )
    if state.login_completed:
        return True, None
    if headless:
        return False, (
            "No login in the headless profile. Run a visible session once to log in:\n"
            "    python -m reciproca browser open"
        )
    log("Log in to Instagram in the Chrome window - waiting…")
    end = time.monotonic() + login_timeout
    while time.monotonic() < end:
        if state.login_completed:
            return True, None
        time.sleep(1)
    return False, "Login did not complete in time."


# ---------------------------------------------------------------------------
# async task registry
# ---------------------------------------------------------------------------

# Long-running cycles run on daemon worker threads so a tool call returns
# instantly with a task_id and the caller polls cycle_status. The core's
# session_running event is the one-at-a-time guard: only one cycle may own
# the browser, and follow_cycle / unfollow_run refuse to spawn while it is
# set. TASKS: {task_id: {"kind", "params", "started", "thread", "result",
# "error", "progress"}}.
TASKS = {}
_tasks_lock = threading.Lock()


def _new_task(kind, **params):
    task_id = uuid.uuid4().hex[:8]
    with _tasks_lock:
        TASKS[task_id] = {
            "kind": kind,
            "params": params,
            "started": time.time(),
            "thread": None,
            "result": None,
            "error": None,
            "progress": None,
        }
    return task_id


def _run_task(task_id, fn):
    """Worker body: run fn, record its result or a caught failure."""
    try:
        result = fn()
        with _tasks_lock:
            TASKS[task_id]["result"] = result
    except Exception as e:
        log(f"❌ Task {task_id} failed: {brief_error(e)}", 'error')
        with _tasks_lock:
            TASKS[task_id]["error"] = brief_error(e)


def _launch(task_id, fn):
    thread = threading.Thread(target=_run_task, args=(task_id, fn), daemon=True)
    with _tasks_lock:
        TASKS[task_id]["thread"] = thread
    thread.start()
    return thread


def _task_snapshot(task_id):
    with _tasks_lock:
        task = TASKS.get(task_id)
        if task is None:
            return None
        return {
            "task_id": task_id,
            "kind": task["kind"],
            "state": "running" if task["result"] is None and task["error"] is None else "done",
            "started": task["started"],
            "elapsed_seconds": round(time.time() - task["started"], 1),
            "progress": task["progress"],
            "result": task["result"],
            "error": task["error"],
        }


def _tasks_summary():
    with _tasks_lock:
        return [
            {
                "task_id": task_id,
                "kind": task["kind"],
                "state": "running" if task["result"] is None and task["error"] is None else "done",
            }
            for task_id, task in TASKS.items()
        ]


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------

@mcp.tool()
@_safe
def browser_open(headless: bool = False) -> dict:
    """Open Chrome on the app's persistent profile (blocking).

    Visible by default - log in by hand the first time, the session is
    saved in chrome_profile/ and reused. `headless` only works with a login
    already saved there. Returns quickly; poll `login_wait` or `browser_status`
    for the login to be detected.
    """
    if state.driver is not None:
        return _ok(already_open=True, logged_in=state.login_completed)
    if not browser.can_open_browser():
        return _fail("Another instance is already opening Chrome - the profile is single-use.")
    browser.open_browser(headless=headless)
    if state.driver is None:
        if _chrome_window_on_profile():
            return _fail(
                "A Chrome window from another command is still open and holds the profile.\n"
                "Close it, then retry."
            )
        return _fail(
            "Chrome did not open - see follow_bot.log.\n"
            "If a Chrome window is still open from another command, close it and retry."
        )
    # The login watcher reports asynchronously from its own thread; give it a
    # few seconds to probe the cookie before reporting the state.
    for _ in range(20):
        if state.login_completed:
            break
        time.sleep(0.5)
    return _ok(already_open=False, logged_in=state.login_completed)


@mcp.tool()
@_safe
def browser_close() -> dict:
    """Close the browser this process controls (refused mid-session)."""
    if state.driver is None:
        return _ok(message="no browser open")
    if state.session_running.is_set():
        return _fail("A session is using the browser - `stop` it first.")
    browser.handle_browser_closed()
    return _ok()


@mcp.tool()
@_safe
def browser_status() -> dict:
    """Report whether the browser is open, logged in, and rate-limited."""
    status = {
        "browser_open": state.driver is not None and browser.browser_is_open(),
        "logged_in": state.login_completed,
        "session_running": state.session_running.is_set(),
        "rate_limited": browser.check_rate_limit(state.driver) if state.driver is not None else False,
    }
    status["browser_window_open"] = state.driver is None and _chrome_window_on_profile()
    return _ok(**status)


@mcp.tool()
@_safe
def login_wait(timeout_seconds: int = 300) -> dict:
    """Wait until the manual login in the browser is detected.

    Polls the login detection once a second, up to timeout_seconds. Fails
    with browser_not_open when no browser is open, and with a timeout error
    when the user has not logged in in time - tell them what to do and call
    again.
    """
    if state.driver is None:
        return _fail("browser_not_open")
    if state.login_completed:
        return _ok(logged_in=True)
    end = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < end:
        if state.login_completed:
            return _ok(logged_in=True)
        time.sleep(1)
    return _fail("Login did not complete in time - log in to Instagram in the Chrome window.", logged_in=False)


# ---------------------------------------------------------------------------
# follow / unfollow cycles (async)
# ---------------------------------------------------------------------------

@mcp.tool()
@_safe
def follow_cycle(mode: str = "search", hashtags: list = None,
                 delay_min: int = None, delay_max: int = None,
                 limit: int = None, semantic_weight: int = None,
                 after_search: str = "follow", headless: bool = False,
                 auto_open: bool = False, login_timeout: int = 300,
                 score_after_search: bool = True) -> dict:
    """Start one follow session on a worker thread; returns a task_id.

    mode "search" scrapes the hashtags first (new candidates), "queue"
    follows from the saved queue. `hashtags` overrides the saved list in
    search mode; delay_min/delay_max/limit default to the saved settings.
    after_search decides what happens once the scrape is done: "follow"
    (start following now), "save_stop" (save to queue and stop),
    "discard" (drop the results). `semantic_weight` overrides the saved
    ranking weight (0-100) when given.

    score_after_search runs the semantic scoring pass on the saved results
    inside this session (default True). Prefer False and a separate
    `queue_score` call when the scoring is wanted: the follow session then
    stays short, and the pass is its own observable task.

    With auto_open the browser is opened first if it is not already (and the
    login waited for, up to login_timeout seconds); without it, the session
    fails with browser_not_open. Long-running: poll `cycle_status` with the
    returned task_id.
    """
    if mode not in ("search", "queue"):
        return _fail(f"invalid mode: {mode} - use 'search' or 'queue'")
    if after_search not in ("follow", "save_stop", "discard"):
        return _fail(f"invalid after_search: {after_search} - use 'follow', 'save_stop' or 'discard'")
    if state.session_running.is_set():
        return _fail("session_busy - a session is already running, `stop` it before starting another")

    if semantic_weight is not None:
        config.CONFIG["SEMANTIC_WEIGHT"] = semantic_weight
        if not config.save_config(config.CONFIG):
            return _fail("Could not write the config file.")

    if auto_open:
        ok, error = _ensure_browser(headless, login_timeout)
        if not ok:
            return _fail(error)
    elif state.driver is None:
        return _fail("browser_not_open - call browser_open first, or pass auto_open=True")

    task_id = _new_task("follow_cycle", mode=mode, limit=limit)
    _launch(task_id, lambda: cycles.follow_cycle(
        mode=mode, delay_min=delay_min, delay_max=delay_max,
        limit=limit, hashtags=hashtags, after_search=after_search,
        score_after_search=score_after_search,
    ))
    log(f"🚀 Follow session started as task {task_id} ({mode} mode)")
    return _ok(task_id=task_id, kind="follow_cycle")


@mcp.tool()
@_safe
def cycle_status(task_id: str) -> dict:
    """Poll a running task: running/done, progress, and the result when done."""
    snapshot = _task_snapshot(task_id)
    if snapshot is None:
        return _fail("no such task", task_id=task_id)
    # During a run the recent log lines are that session's output.
    snapshot["last_logs"] = [
        {"time": t, "level": level, "message": msg}
        for t, level, msg in list(RECENT)[-15:]
    ]
    return _ok(**snapshot)


@mcp.tool()
@_safe
def unfollow_load(followers_path: str, following_path: str) -> dict:
    """Load the Instagram data-export JSON pair and compute non-followers.

    Points at the downloaded followers_1.json and following.json. Requires
    the browser to be open (the account is cross-checked against it).
    """
    result = unfollow.uf_load_json_pair(followers_path, following_path)
    result["count"] = len(result.get("non_followers", []))
    return result


@mcp.tool()
@_safe
def unfollow_run(delay_min: int = None, delay_max: int = None,
                 limit: int = None, headless: bool = False,
                 auto_open: bool = False, login_timeout: int = 300) -> dict:
    """Start one unfollow session on a worker thread; returns a task_id.

    Unfollows the loaded non-followers that are still unprocessed (progress
    is saved, so a stopped session resumes where it left off). Refused
    unless the JSON pair was loaded first (see unfollow_load). Long-running:
    poll `cycle_status` with the returned task_id.
    """
    if state.session_running.is_set():
        return _fail("session_busy - a session is already running, `stop` it before starting another")

    if auto_open:
        ok, error = _ensure_browser(headless, login_timeout)
        if not ok:
            return _fail(error)
    elif state.driver is None:
        return _fail("browser_not_open - call browser_open first, or pass auto_open=True")

    task_id = _new_task("unfollow_cycle", limit=limit)
    _launch(task_id, lambda: cycles.unfollow_cycle(
        delay_min=delay_min, delay_max=delay_max, limit=limit,
    ))
    log(f"🚀 Unfollow session started as task {task_id}")
    return _ok(task_id=task_id, kind="unfollow_cycle")


@mcp.tool()
@_safe
def unfollow_status() -> dict:
    """Report the loaded non-followers and how many are left to process."""
    total, remaining, removed = unfollow.unfollow_progress_counts()
    return _ok(
        non_followers=len(state.uf_non_followers),
        total=total,
        remaining=remaining,
        removed=removed,
    )


@mcp.tool()
@_safe
def unfollow_reset() -> dict:
    """Discard the unfollow progress and session (not the follow queue).

    Destructive: everyone already unfollowed on Instagram stays unfollowed,
    only the app's memory of the progress goes. Confirm with the user first.
    """
    unfollow.reset_unfollow_state()
    return _ok()


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

@mcp.tool()
@_safe
def queue_list(limit: int = None) -> dict:
    """The queue in rank order: username and affinity for the top `limit`."""
    items = queue_mod.load_queue()
    ranked = queue_mod.rank_queue(items, queue_mod.ranking_frequencies())
    entries = [
        {"username": user, "affinity": queue_mod.queue_affinity(item)}
        for user, _, item in ranked[:limit]
    ]
    return _ok(queue=entries, count=len(entries), total=len(items))


@mcp.tool()
@_safe
def queue_add(usernames: list) -> dict:
    """Add usernames to the queue (duplicates and already-followed are skipped)."""
    if not usernames:
        return _fail("no usernames given")
    new, total = queue_mod.add_to_queue([str(u) for u in usernames])
    return _ok(added=new, total=total)


@mcp.tool()
@_safe
def queue_remove(username: str) -> dict:
    """Remove one username from the queue."""
    removed = queue_mod.remove_from_queue(username)
    return _ok(removed=removed, message=None if removed else f"{username} is not in the queue")


@mcp.tool()
@_safe
def queue_clear() -> dict:
    """Empty the queue. Destructive: confirm with the user first."""
    queue_mod.clear_queue()
    return _ok()


@mcp.tool()
@_safe
def queue_score(limit: int = None, niche: str = None) -> dict:
    """Score the top unscored candidates against the niche (async task).

    Reads profiles from the open browser, so the browser must be open. The
    niche defaults to the saved SEMANTIC_NICHE setting. Returns a task_id;
    poll `cycle_status` for progress ({done, total, current}) and the final
    count of profiles scored.
    """
    if state.driver is None:
        return _fail("browser_not_open - the browser must be open to read profiles")
    if niche is None:
        niche = str(config.CONFIG.get("SEMANTIC_NICHE") or "").strip()
    if not niche:
        return _fail("no niche - set one with config_set SEMANTIC_NICHE or pass niche=")
    scorer = semantic.make_affinity_scorer(follow.read_candidate_profile, semantic.semantic_model.embed, niche)
    if scorer is None:
        return _fail("Could not build the scorer - see follow_bot.log.")

    task_id = _new_task("queue_score", limit=limit)

    def on_progress(number, total, username):
        with _tasks_lock:
            TASKS[task_id]["progress"] = {"done": number, "total": total, "current": username}

    _launch(task_id, lambda: queue_mod.score_queue(
        scorer, limit=limit, on_progress=on_progress,
        stop_event=state.stop_requested,
    ))
    log(f"🧭 Queue scoring started as task {task_id}")
    return _ok(task_id=task_id, kind="queue_score")


@mcp.tool()
@_safe
def queue_trim(limit: int = None) -> dict:
    """Keep the best `limit` candidates, drop the rest.

    Destructive: the dropped entries leave the queue (their sighting counts
    stay on file). Confirm with the user first.
    """
    removed = queue_mod.trim_queue(limit=limit)
    return _ok(removed=removed)


@mcp.tool()
@_safe
def queue_import(file: str) -> dict:
    """Add usernames from a file: a JSON list (or dict keys), or plain lines."""
    usernames = []
    if file.endswith('.json'):
        with open(file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                usernames = [u for u in (queue_mod.queue_username(item) for item in data) if u]
            elif isinstance(data, dict):
                usernames = list(data.keys())
    else:
        with open(file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    usernames.append(line)
    if not usernames:
        return _fail(f"no usernames found in {file}")
    new, total = queue_mod.add_to_queue(usernames)
    return _ok(imported=len(usernames), added=new, total=total)


@mcp.tool()
@_safe
def queue_export(file: str) -> dict:
    """Write the whole queue to a JSON file (what queue_import reads back)."""
    queue = queue_mod.load_queue()
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(queue, f, indent=2, ensure_ascii=False)
    return _ok(exported=len(queue))


# ---------------------------------------------------------------------------
# hashtags
# ---------------------------------------------------------------------------

def _hashtags():
    """load_hashtags() is None when the file is missing; tools want a list."""
    return persistence.load_hashtags() or []


@mcp.tool()
@_safe
def hashtags_list() -> dict:
    """The saved hashtags the search mode scrapes."""
    tags = _hashtags()
    return _ok(hashtags=tags, count=len(tags))


@mcp.tool()
@_safe
def hashtags_add(hashtags: list) -> dict:
    """Add hashtags to the saved list (duplicates are kept, like the CLI)."""
    if not hashtags:
        return _fail("no hashtags given")
    tags = _hashtags()
    tags.extend(str(h) for h in hashtags)
    persistence.save_hashtags(tags)
    return _ok(hashtags=tags, count=len(tags))


@mcp.tool()
@_safe
def hashtags_remove(hashtag: str) -> dict:
    """Remove one hashtag from the saved list."""
    tags = _hashtags()
    if hashtag not in tags:
        return _fail(f"{hashtag} is not in the list.")
    tags.remove(hashtag)
    persistence.save_hashtags(tags)
    return _ok(hashtags=tags, count=len(tags))


@mcp.tool()
@_safe
def hashtags_clear() -> dict:
    """Remove every saved hashtag. Destructive: confirm with the user first."""
    persistence.save_hashtags([])
    return _ok()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@mcp.tool()
@_safe
def config_get(key: str = None) -> dict:
    """One setting by key, or the whole settings table without a key."""
    if key is None:
        return _ok(config=config.CONFIG)
    if key not in config.CONFIG:
        return _fail(f"no such key: {key}")
    return _ok(config={key: config.CONFIG[key]})


@mcp.tool()
@_safe
def config_set(key: str, value: str) -> dict:
    """Change one setting and save it. The value is typed by the current one:
    booleans accept 1/true/yes/on, numbers are parsed, everything else is
    kept as text. Destructive-ish: confirm with the user first.
    """
    if key not in config.CONFIG:
        return _fail(f"no such key: {key}")
    current = config.CONFIG[key]
    if isinstance(current, bool):
        parsed = value.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(current, int):
        try:
            parsed = int(value)
        except ValueError:
            return _fail(f"{key} expects a number.")
    else:
        parsed = value
    config.CONFIG[key] = parsed
    if not config.save_config(config.CONFIG):
        return _fail("Could not write the config file.")
    return _ok(key=key, value=parsed)


@mcp.tool()
@_safe
def config_reset() -> dict:
    """Restore every setting to its default. Destructive: confirm first."""
    # Mutated in place, not rebound: other modules hold the same dict object
    # (the façade's CONFIG is bound at import), and a rebind would quietly
    # leave them looking at the old settings.
    config.CONFIG.clear()
    config.CONFIG.update(config.DEFAULT_CONFIG)
    config.save_config(config.CONFIG)
    return _ok()


@mcp.tool()
@_safe
def config_reload() -> dict:
    """Re-read the config file into this process, in place.

    This server snapshots the settings at startup, so changes made through
    the GUI, the CLI or an external editor are invisible here until this
    tool runs - and the next config_set would otherwise overwrite them with
    the stale snapshot. Returns what changed, if anything.
    """
    fresh = config.load_config()
    changed = {k: fresh[k] for k in fresh if fresh[k] != config.CONFIG.get(k)}
    config.CONFIG.clear()
    config.CONFIG.update(fresh)
    return _ok(changed=changed, reloaded=len(changed))


# ---------------------------------------------------------------------------
# status / logs / stop
# ---------------------------------------------------------------------------

@mcp.tool()
@_safe
def status() -> dict:
    """Full snapshot: browser, session, queue, hashtags, unfollow, tasks."""
    info = {
        "browser_open": state.driver is not None and browser.browser_is_open(),
        "logged_in": state.login_completed,
        "session_running": state.session_running.is_set(),
        "queue": len(queue_mod.load_queue()),
        "hashtags": _hashtags(),
        "unfollow": {
            "non_followers": len(state.uf_non_followers),
            "remaining": unfollow.unfollow_progress_counts()[1],
            "removed": unfollow.unfollow_progress_counts()[2],
        },
    }
    info["browser_window_open"] = state.driver is None and _chrome_window_on_profile()
    if state.stats.attempted:
        info["last_follow_stats"] = {
            "attempted": state.stats.attempted,
            "succeeded": state.stats.succeeded,
            "errors": state.stats.errors,
        }
    if state.uf_stats.attempted:
        info["last_unfollow_stats"] = {
            "attempted": state.uf_stats.attempted,
            "succeeded": state.uf_stats.succeeded,
            "errors": state.uf_stats.errors,
        }
    info["tasks"] = _tasks_summary()
    return _ok(**info)


@mcp.tool()
@_safe
def logs_tail(lines: int = 50) -> dict:
    """The last `lines` log entries (newest first), each with time and level."""
    if lines < 1:
        lines = 1
    if lines > 1000:
        lines = 1000
    return _ok(logs=[
        {"time": t, "level": level, "message": msg}
        for t, level, msg in list(RECENT)[-lines:][::-1]
    ])


@mcp.tool()
@_safe
def stop() -> dict:
    """Ask any running session to stop at its next safe point.

    Works across processes (also flags a session run by the CLI or GUI from
    another terminal). The session finishes its current step, then halts.
    """
    state.stop_requested.set()
    state.scoring_stop.set()
    with open(config.STOP_FLAG_FILE, 'w', encoding='utf-8') as f:
        f.write("stop\n")
    return _ok(message="Stop requested - the running session will halt at its next safe point.")


def main():
    # The stdio transport returns when the client goes away (EOF) - the mcp
    # SDK v2 process did not, which orphaned the server holding Chrome and
    # the profile lock. The finally makes sure the exit also releases the
    # browser, so a dead client cannot leave an orphaned Chrome behind.
    try:
        mcp.run()
    finally:
        browser.handle_browser_closed()


if __name__ == "__main__":
    main()
