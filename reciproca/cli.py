"""The command-line interface: `python -m reciproca`.

Every command maps onto the same core the GUI drives, so a session can be
started from one and continued from the other - the browser, the queue, the
config and the progress files are all shared. The GUI is only the window; the
CLI is only the terminal; the core does not know which one is calling.

Two modes. With arguments, each invocation is one command in its own process,
which owns the browser for that command and releases it afterwards. With no
arguments it enters the interactive shell: one long-lived process where the
browser survives across commands, so `browser open` followed by `follow`
drives the same Chrome. Piped stdin (`echo "status\\nquit" | python -m
reciproca`) runs the same shell as a script.

Command tree:

    browser open [--headless] | close | status
    follow [--mode queue|search] [--hashtags a,b] [--delay-min M]
           [--delay-max M] [--limit N] [--after-search follow|save-stop|discard]
           [--headless] [--login-timeout N]
    unfollow load F1 F2 | run [--delay-min M] [--delay-max M] [--limit N]
            [--headless] [--login-timeout N] | status | reset [--yes]
    queue list [--limit N] | rank [--limit N] | add USER... | remove USER |
          clear [--yes] | score [--limit N] | trim [--limit N] [--yes] |
          import FILE | export FILE
    hashtags add TAG... | remove TAG | list | clear [--yes]
    config get [KEY] | set KEY VALUE | reset [--yes]
    status [--json]
    logs [--tail N] [--follow]
    stop
    help [topic]
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import time

from reciproca import config, state
from reciproca import browser, queue as queue_mod, unfollow, persistence, cycles, semantic, follow


def _chrome_window_on_profile():
    """True when a Chrome window is running on the app's profile.

    A browser opened by an earlier CLI process survives that process, but the
    driver - and with it the login flag - dies with it, so a fresh process
    cannot attach. The status commands report the leftover window separately
    from the driver, instead of pretending everything is off.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={config.CHROME_PROFILE_DIR}"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def _confirm(args, what):
    """The `--yes` flag stands in for the GUI's confirmation dialogs."""
    if getattr(args, "yes", False):
        return True
    try:
        return input(f"{what} (y/N) ").strip().lower() in ("y", "yes")
    except EOFError:
        return False  # closed stdin counts as "no"


def _print_result(result, json_out=False):
    """Print a cycles result dict; the report is what the GUI would have shown."""
    if json_out:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if not result.get("ok"):
        print(f"✗ {result.get('error', 'failed')}", file=sys.stderr)
        return
    report = result.get("report")
    if report:
        print(report)
    for key, label in (
        ("followed", "Followed"),
        ("added", "Added to queue"),
        ("processed", "Processed"),
        ("unfollowed", "Unfollowed"),
        ("queue_remaining", "Queue remaining"),
    ):
        if key in result:
            print(f"{label}: {result[key]}")


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------

def cmd_browser_open(args):
    if state.driver is not None:
        print("The browser is already open.")
        return 0
    if not browser.can_open_browser():
        print("Another instance is already opening Chrome - the profile is single-use.", file=sys.stderr)
        return 1
    browser.open_browser(headless=args.headless)
    if state.driver is None:
        if _chrome_window_on_profile():
            print("✗ A Chrome window from another command is open and holds the profile.", file=sys.stderr)
            print("  Close it, then retry.", file=sys.stderr)
        else:
            print("Chrome did not open - see follow_bot.log.", file=sys.stderr)
            print("If a Chrome window is still open from another command, close it and retry.", file=sys.stderr)
        return 1
    # The login watcher reports asynchronously from its own thread; give it a
    # few seconds to probe the cookie before saying whether the session is in.
    for _ in range(20):
        if state.login_completed:
            break
        time.sleep(0.5)
    if state.login_completed:
        print("✅ Browser open and logged in.")
        print("   Chrome stays open for interactive use - close the window when done.")
    else:
        # The login is detected by the watcher thread; the shell is still
        # alive to see it, a one-shot process is not. Say what to do for each.
        if _interactive:
            print("✅ Browser open. Log in to Instagram in the Chrome window -")
            print("   this session picks the login up on its own (`status` confirms it).")
        else:
            print("✅ Browser open. Log in to Instagram in the Chrome window.")
            print("⚠️  This window locks the profile until you close it: no other command")
            print("    (shell or CLI) can use the browser. The next command after you close")
            print("    it re-opens Chrome with the login already saved.")
    return 0


def cmd_browser_close(_args):
    if state.driver is None:
        print("No browser is open.")
        return 0
    if state.session_running.is_set():
        print("✗ A session is using the browser - `stop` it first.", file=sys.stderr)
        return 1
    browser.handle_browser_closed()
    print("✅ Browser closed.")
    return 0


def cmd_browser_status(args):
    status = {
        "browser_open": state.driver is not None and browser.browser_is_open(),
        "logged_in": state.login_completed,
        "session_running": state.session_running.is_set(),
        "rate_limited": browser.check_rate_limit(state.driver) if state.driver is not None else False,
    }
    status["browser_window_open"] = state.driver is None and _chrome_window_on_profile()
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        if status["browser_open"]:
            print(f"Browser open: yes")
        elif status["browser_window_open"]:
            print("Browser open: a Chrome window is running, but no CLI process controls it")
            print("               (close the window to release the profile)")
        else:
            print("Browser open: no")
        print(f"Logged in: {'yes' if status['logged_in'] else 'no'}")
        print(f"Session running: {'yes' if status['session_running'] else 'no'}")
        print(f"Rate limited: {'yes' if status['rate_limited'] else 'no'}")
    return 0


def wait_for_login(timeout):
    """Poll for login_completed, one second at a time. Returns True when in."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if state.login_completed:
            return True
        time.sleep(1)
    return state.login_completed


def ensure_browser(headless, login_timeout):
    """Open the browser if needed and wait for the login to be in place.

    Returns a (ok, error) tuple. The login wait matters only when the browser
    was just opened: an already-open browser is presumed logged in, since the
    login form would have disabled the session otherwise.
    """
    if state.driver is not None:
        return True, None
    if not browser.can_open_browser():
        return False, "Another instance is already opening Chrome - the profile is single-use."
    if headless:
        print("Opening headless Chrome - this only works with a login already saved in chrome_profile/.")
    browser.open_browser(headless=headless)
    if state.driver is None:
        if _chrome_window_on_profile():
            return False, (
                "A Chrome window from another command is still open and holds the profile.\n"
                "Close it, then retry this command."
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
    print("Log in to Instagram in the Chrome window - waiting…")
    if not wait_for_login(login_timeout):
        return False, "Login did not complete in time."
    return True, None


# ---------------------------------------------------------------------------
# follow / unfollow
# ---------------------------------------------------------------------------

# Set by repl(): cycles run on a worker thread so the prompt stays responsive,
# and `stop`/`status` keep working while a session runs. The one-shot CLI stays
# synchronous - its exit code must mean the cycle finished.
_interactive = False

# The worker threads repl() spawned, so `quit` can ask their sessions to stop
# before the browser is released.
_repl_threads = []

# Mid-session question handshake. A worker thread that needs an answer from the
# user - the after-search tri-state, mirroring the GUI's messagebox - sets
# _question and waits; the main loop, which owns the terminal, posts the answer.
# While a question is pending the next typed line IS the answer, so typing
# `status` there is not possible - the cycle is paused, exactly like the GUI's
# modal dialog blocks the session.
_question = None
_question_event = None
_question_answer = None
# The worker renders the question itself as soon as it sets it - the main loop
# is usually blocked in input() and would otherwise show it only when the user
# presses Enter, leaving the session paused on an invisible question. The main
# loop re-renders on answer attempts after the first, which it consumed.
_question_rendered = False


def _shell_decision(info):
    """The decision_hook for shell sessions: ask at the prompt, like the GUI.

    Called by the worker after the scrape; returns one of "follow" |
    "save_stop" | "discard". A `stop` request while the question is open is
    treated as "discard" - the user wants out.
    """
    global _question, _question_event, _question_answer, _question_rendered
    question = (
        f"❓ Search finished: {info['ranked_count']} users found"
        f" (best affinity {info['top_freq']:.2f}, {info['hashtag_count']} hashtags).\n"
        "   [f]ollow now   [s]ave to queue and stop   [d]iscard? "
    )
    _question = question
    _question_rendered = True
    _question_event = threading.Event()
    _question_answer = None
    # The main loop may be blocked in input() for minutes; show the question
    # from here so the user sees it the moment the search ends.
    print("\n" + question, end="", flush=True)
    try:
        while not _question_event.wait(timeout=0.5):
            if state.stop_requested.is_set() or os.path.exists(config.STOP_FLAG_FILE):
                return "discard"
        return _question_answer
    finally:
        _question = None


def cmd_follow(args):
    # In the interactive shell the browser may predate this command - leave it
    # for the next one. A one-shot CLI process always opened it just now, so it
    # releases it: open -> run -> quit, or a Chrome left behind holds the
    # profile hostage for the next command.
    had_browser = state.driver is not None
    ok, error = ensure_browser(args.headless, args.login_timeout)
    if not ok:
        print(f"✗ {error}", file=sys.stderr)
        return 1

    if not _interactive:
        # One-shot: the exit code must mean the cycle finished and its outcome.
        try:
            result = cycles.follow_cycle(
                mode=args.mode,
                delay_min=args.delay_min,
                delay_max=args.delay_max,
                limit=args.limit,
                hashtags=args.hashtags,
                after_search=args.after_search,
            )
            _print_result(result, args.json)
            return 0 if result.get("ok") else 1
        finally:
            if not had_browser:
                browser.handle_browser_closed()

    # Interactive shell: the cycle runs on a worker thread so the prompt stays
    # responsive - `status`, `stop` and every other command keep working. The
    # after-search choice is asked at the prompt, mirroring the GUI's dialog.
    def worker():
        try:
            result = cycles.follow_cycle(
                mode=args.mode,
                delay_min=args.delay_min,
                delay_max=args.delay_max,
                limit=args.limit,
                hashtags=args.hashtags,
                after_search=args.after_search,
                decision_hook=_shell_decision,
            )
            _print_result(result, args.json)
        except Exception as e:
            print(f"✗ {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            if not had_browser:
                browser.handle_browser_closed()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _repl_threads.append(thread)
    print("🚀 Follow session started - `status` to watch, `stop` to halt.")
    return 0


def cmd_unfollow_load(args):
    result = unfollow.uf_load_json_pair(args.followers, args.following)
    if not result.get("ok"):
        print(f"✗ {result.get('error')}", file=sys.stderr)
        return 1
    print(f"✅ {len(result['non_followers'])} non-followers loaded. `unfollow status` to see progress, `unfollow run` to start.")
    return 0


def cmd_unfollow_run(args):
    had_browser = state.driver is not None
    ok, error = ensure_browser(args.headless, args.login_timeout)
    if not ok:
        print(f"✗ {error}", file=sys.stderr)
        return 1

    if not _interactive:
        try:
            result = cycles.unfollow_cycle(delay_min=args.delay_min, delay_max=args.delay_max, limit=args.limit)
            _print_result(result, args.json)
            return 0 if result.get("ok") else 1
        finally:
            if not had_browser:
                browser.handle_browser_closed()

    def worker():
        try:
            result = cycles.unfollow_cycle(delay_min=args.delay_min, delay_max=args.delay_max, limit=args.limit)
            _print_result(result, args.json)
        except Exception as e:
            print(f"✗ {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            if not had_browser:
                browser.handle_browser_closed()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _repl_threads.append(thread)
    print("🚀 Unfollow session started - `status` to watch, `stop` to halt.")
    return 0


def cmd_unfollow_status(args):
    total, remaining, removed = unfollow.unfollow_progress_counts()
    status = {
        "non_followers": len(state.uf_non_followers),
        "remaining": remaining,
        "removed": removed,
    }
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        print(f"Non-followers: {status['non_followers']}")
        print(f"Remaining to unfollow: {remaining}")
        print(f"Already unfollowed: {removed}")
    return 0


def cmd_unfollow_reset(args):
    if not _confirm(args, "Discard all unfollow progress?"):
        print("Reset cancelled.")
        return 0
    unfollow.reset_unfollow_state()
    print("✅ Unfollow progress reset.")
    return 0


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

def cmd_queue_list(args):
    items = queue_mod.load_queue()
    ranked = queue_mod.rank_queue(items, queue_mod.ranking_frequencies())
    if args.json:
        print(json.dumps([user for user, _, _ in ranked[: args.limit]], indent=2, ensure_ascii=False))
        return 0
    if not ranked:
        print("Queue is empty.")
        return 0
    for n, (user, _, _) in enumerate(ranked[: args.limit], 1):
        print(f"{n:>4}. {user}")
    if args.limit and len(ranked) > args.limit:
        print(f"… {len(ranked) - args.limit} more")
    return 0


def cmd_queue_rank(args):
    items = queue_mod.load_queue()
    ranked = queue_mod.rank_queue(items, queue_mod.ranking_frequencies())
    if args.json:
        print(json.dumps(
            [{"username": user, "affinity": queue_mod.queue_affinity(item)}
             for user, _, item in ranked[: args.limit]], indent=2, ensure_ascii=False))
        return 0
    if not ranked:
        print("Queue is empty.")
        return 0
    print(f"{'rank':>4}  {'username':<32} affinity")
    for n, (user, _, item) in enumerate(ranked[: args.limit], 1):
        affinity = queue_mod.queue_affinity(item)
        shown = f"{affinity:.2f}" if affinity is not None else "-"
        print(f"{n:>4}  {user:<32} {shown}")
    return 0


def cmd_queue_add(args):
    queue_mod.add_to_queue(args.users)
    print(f"✅ Added {len(args.users)} to the queue.")
    return 0


def cmd_queue_remove(args):
    queue_mod.remove_from_queue(args.user)
    print("✅ Removed.")
    return 0


def cmd_queue_clear(args):
    if not _confirm(args, "Remove everyone from the queue?"):
        print("Clear cancelled.")
        return 0
    queue_mod.clear_queue()
    print("✅ Queue cleared.")
    return 0


def cmd_queue_score(args):
    niche = str(config.CONFIG.get("SEMANTIC_NICHE") or "").strip()
    if not niche:
        print("✗ No niche in the config - set one: `config set SEMANTIC_NICHE 'what you look for'`", file=sys.stderr)
        return 1
    if state.driver is None:
        print("✗ The browser must be open to read profiles: `browser open`", file=sys.stderr)
        return 1
    scorer = semantic.make_affinity_scorer(follow.read_candidate_profile, semantic.semantic_model.embed, niche)
    if scorer is None:
        print("✗ Could not build the scorer - see follow_bot.log.", file=sys.stderr)
        return 1

    def on_progress(done, total):
        print(f"\rScoring {done}/{total}…", end="", flush=True)

    scored = queue_mod.score_queue(scorer, limit=args.limit, on_progress=on_progress,
                                   stop_event=state.stop_requested)
    print("\r" + " " * 40 + "\r", end="")
    print(f"✅ Queue rescored ({len(scored)} entries).")
    return 0


def cmd_queue_trim(args):
    if not _confirm(args, "Trim the queue to the top entries?"):
        print("Trim cancelled.")
        return 0
    queue_mod.trim_queue(limit=args.limit)
    print("✅ Queue trimmed.")
    return 0


def cmd_queue_import(args):
    filepath = args.file
    try:
        usernames = []
        if filepath.endswith('.json'):
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    usernames = [u for u in (queue_mod.queue_username(item) for item in data) if u]
                elif isinstance(data, dict):
                    usernames = list(data.keys())
        else:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        usernames.append(line)
    except Exception as e:
        print(f"✗ Could not read {filepath}: {e}", file=sys.stderr)
        return 1
    queue_mod.add_to_queue(usernames)
    print(f"✅ Imported {len(usernames)} users.")
    return 0


def cmd_queue_export(args):
    queue = queue_mod.load_queue()
    try:
        with open(args.file, 'w', encoding='utf-8') as f:
            json.dump(queue, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"✗ Could not write {args.file}: {e}", file=sys.stderr)
        return 1
    print(f"✅ Exported {len(queue)} users to {args.file}.")
    return 0


# ---------------------------------------------------------------------------
# hashtags
# ---------------------------------------------------------------------------

def cmd_hashtags_add(args):
    current = persistence.load_hashtags()
    current.extend(args.tags)
    persistence.save_hashtags(current)
    print(f"✅ Hashtags: {', '.join(persistence.load_hashtags())}")
    return 0


def cmd_hashtags_remove(args):
    current = persistence.load_hashtags()
    if args.tag not in current:
        print(f"✗ {args.tag} is not in the list.", file=sys.stderr)
        return 1
    current.remove(args.tag)
    persistence.save_hashtags(current)
    print("✅ Removed.")
    return 0


def cmd_hashtags_list(args):
    tags = persistence.load_hashtags()
    if args.json:
        print(json.dumps(tags, indent=2, ensure_ascii=False))
    elif tags:
        print("\n".join(f"#{t}" for t in tags))
    else:
        print("No hashtags saved.")
    return 0


def cmd_hashtags_clear(args):
    if not _confirm(args, "Remove all hashtags?"):
        print("Clear cancelled.")
        return 0
    persistence.save_hashtags([])
    print("✅ Hashtags cleared.")
    return 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def cmd_config_get(args):
    conf = config.CONFIG
    if args.key:
        if args.key not in conf:
            print(f"✗ No such key: {args.key}", file=sys.stderr)
            return 1
        print(json.dumps({args.key: conf[args.key]} if args.json else conf[args.key]))
    else:
        if args.json:
            print(json.dumps(conf, indent=2, ensure_ascii=False))
        else:
            for key in sorted(conf):
                print(f"{key} = {conf[key]}")
    return 0


def cmd_config_set(args):
    if args.key not in config.CONFIG:
        print(f"✗ No such key: {args.key}", file=sys.stderr)
        return 1
    value = args.value
    current = config.CONFIG[args.key]
    if isinstance(current, bool):
        value = value.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(current, int):
        try:
            value = int(value)
        except ValueError:
            print(f"✗ {args.key} expects a number.", file=sys.stderr)
            return 1
    config.CONFIG[args.key] = value
    if config.save_config(config.CONFIG):
        print(f"✅ {args.key} = {value}")
        return 0
    print("✗ Could not write the config file.", file=sys.stderr)
    return 1


def cmd_config_reset(args):
    if not _confirm(args, "Reset every setting to its default?"):
        print("Reset cancelled.")
        return 0
    config.CONFIG = config.DEFAULT_CONFIG.copy()
    config.save_config(config.CONFIG)
    print("✅ Config reset.")
    return 0


# ---------------------------------------------------------------------------
# status / logs / stop
# ---------------------------------------------------------------------------

def cmd_status(args):
    status = {
        "browser_open": state.driver is not None and browser.browser_is_open(),
        "logged_in": state.login_completed,
        "session_running": state.session_running.is_set(),
        "queue": len(queue_mod.load_queue()),
        "hashtags": persistence.load_hashtags(),
        "unfollow": {
            "non_followers": len(state.uf_non_followers),
            "remaining": unfollow.unfollow_progress_counts()[1],
            "removed": unfollow.unfollow_progress_counts()[2],
        },
    }
    status["browser_window_open"] = state.driver is None and _chrome_window_on_profile()
    if state.stats.attempted:
        status["last_follow_stats"] = {
            "attempted": state.stats.attempted,
            "succeeded": state.stats.succeeded,
            "errors": state.stats.errors,
        }
    if state.uf_stats.attempted:
        status["last_unfollow_stats"] = {
            "attempted": state.uf_stats.attempted,
            "succeeded": state.uf_stats.succeeded,
            "errors": state.uf_stats.errors,
        }
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    else:
        if status["browser_open"]:
            browser_line = f"Browser: open - {'logged in' if status['logged_in'] else 'not logged in'}"
        elif status["browser_window_open"]:
            browser_line = "Browser: a Chrome window is running, but no CLI process controls it"
        else:
            browser_line = "Browser: closed"
        print(browser_line)
        print(f"Session running: {'yes' if status['session_running'] else 'no'}")
        print(f"Queue: {status['queue']} users")
        print(f"Hashtags: {', '.join('#' + t for t in status['hashtags']) or 'none'}")
        uf = status["unfollow"]
        print(f"Unfollow: {uf['non_followers']} loaded, {uf['remaining']} left, {uf['removed']} removed")
        # While a session runs, the stats are that session's live progress.
        if "last_follow_stats" in status:
            s = status["last_follow_stats"]
            if status["session_running"]:
                print(f"Follow in progress: {s['succeeded']}/{s['attempted']} followed, {s['errors']} errors")
            else:
                print(f"Last follow session: {s['succeeded']}/{s['attempted']} followed, {s['errors']} errors")
        if "last_unfollow_stats" in status:
            s = status["last_unfollow_stats"]
            if status["session_running"]:
                print(f"Unfollow in progress: {s['succeeded']}/{s['attempted']} unfollowed, {s['errors']} errors")
            else:
                print(f"Last unfollow session: {s['succeeded']}/{s['attempted']} unfollowed, {s['errors']} errors")
    return 0


def cmd_logs(args):
    # The log file, not the RECENT buffer: the buffer is process-local and a
    # fresh process's `logs` would be empty, while the file is the session's
    # history across processes.
    if args.follow:
        with open(config.LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            tail = f.readlines()[-args.tail:]
            print("".join(tail), end="", flush=True)
            f.seek(0, os.SEEK_END)
            try:
                while True:
                    line = f.readline()
                    if line:
                        print(line.rstrip(), flush=True)
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                print()
        return 0
    with open(config.LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
        tail = f.readlines()[-args.tail:]
    print("".join(tail), end="", flush=True)
    return 0


def cmd_stop(_args):
    """Stop any running session: this process or another one, GUI or CLI."""
    state.stop_requested.set()
    state.scoring_stop.set()
    with open(config.STOP_FLAG_FILE, 'w', encoding='utf-8') as f:
        f.write("stop\n")
    print("✅ Stop requested - the running session will finish its current step and halt.")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def _subparser(parser, name):
    """The subparser for a command name, or None when there is no such command."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices.get(name)
    return None


def cmd_help(args):
    """The command list, or one command's full manual."""
    if not args.topic:
        args._parser.print_help()
        return 0
    sub = _subparser(args._parser, args.topic)
    if sub is None:
        print(f"No such command: {args.topic}", file=sys.stderr)
        print("Type `help` for the command list.", file=sys.stderr)
        return 1
    print(sub.format_help())
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="reciproca",
        description="Follow & unfollow bot - the same core the GUI runs, from the terminal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser(
        "browser", help="open, close or inspect the Chrome session",
        description="""The Chrome session on the saved profile. `open` shows Chrome and
waits for the login; `close` ends the session; `status` probes whether it is
open, logged in or rate limited. A one-shot `open` leaves the window running
and locks the profile until the window is closed - the shell keeps the same
browser across commands instead.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bb = b.add_subparsers(dest="browser_command", required=True)
    bo = bb.add_parser("open", help="open Chrome on the saved profile")
    bo.add_argument("--headless", action="store_true", help="no window - needs a login saved in chrome_profile/")
    bo.set_defaults(handler=cmd_browser_open)
    bb.add_parser("close", help="close Chrome and forget the session").set_defaults(handler=cmd_browser_close)
    bs = bb.add_parser("status", help="is the browser open, logged in, rate limited?")
    bs.add_argument("--json", action="store_true")
    bs.set_defaults(handler=cmd_browser_status)

    f = sub.add_parser(
        "follow", help="run one follow session",
        description="""Follow users from the saved queue or from your hashtags, one at a
time, with the configured delays and the bot filter on each profile.

The two --mode values:
  queue    work the saved queue (follow_queue.json), in rank order
  search   scrape the saved hashtags first (or --hashtags), rank the
           authors by niche affinity and sighting frequency, then decide
           what to do with them (see --after-search) and follow

The flow of a search session:
  1. open Chrome (or reuse the shell's) and wait for the login
  2. scrape each hashtag's recent posts and collect the authors
  3. rank the candidates by affinity and frequency
  4. ask the after-search tri-state: in the shell it appears at the
     prompt - [f]ollow now / [s]ave to queue and stop / [d]iscard;
     one-shot runs use --after-search instead
  5. follow the strongest candidates, checking each profile against the
     bot filter first""",
        epilog="""Delays, limits and filters come from bot_config.json unless a flag
overrides them here. In the shell, `status` and `stop` keep working while
the session runs; from another terminal, `python -m reciproca stop` halts
it at its next checkpoint.

Examples:
  reciproca follow --mode queue --limit 5
  reciproca follow --mode search --hashtags photography,street
  reciproca follow --mode search --after-search save_stop""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    f.add_argument("--mode", choices=["queue", "search"], default="search",
                   help="queue = work the saved queue, search = hashtags first (default search)")
    f.add_argument("--hashtags", help="comma-separated list; defaults to the saved hashtags")
    f.add_argument("--delay-min", type=int, help="seconds between follows (default from config)")
    f.add_argument("--delay-max", type=int, help="seconds between follows (default from config)")
    f.add_argument("--limit", type=int, help="how many to follow (default from config)")
    f.add_argument("--after-search", choices=["follow", "save_stop", "discard"], default="follow",
                   help="what to do with the ranked results when the search ends "
                        "(default follow; save_stop = add them to the queue and stop)")
    f.add_argument("--headless", action="store_true")
    f.add_argument("--login-timeout", type=int, default=300, help="seconds to wait for the login (default 300)")
    f.add_argument("--json", action="store_true", help="print the session result as JSON")
    f.set_defaults(handler=cmd_follow)

    u = sub.add_parser(
        "unfollow", help="load exports, run an unfollow session, inspect progress",
        description="""Unfollow accounts that do not follow you back. The account list
comes from the Instagram data download's JSON exports; progress is saved,
so a stopped session resumes where it left off.

  load F1 F2   read the followers.json and following.json exports
  run          walk the non-followers list, one by one, with delays
  status       how many are left
  reset        discard the saved progress""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    uu = u.add_subparsers(dest="unfollow_command", required=True)
    ul = uu.add_parser("load", help="load the followers/following JSON exports")
    ul.add_argument("followers", help="the followers.json export path")
    ul.add_argument("following", help="the following.json export path")
    ul.set_defaults(handler=cmd_unfollow_load)
    ur = uu.add_parser("run", help="run one unfollow session (resumes from saved progress)")
    ur.add_argument("--delay-min", type=int)
    ur.add_argument("--delay-max", type=int)
    ur.add_argument("--limit", type=int)
    ur.add_argument("--headless", action="store_true")
    ur.add_argument("--login-timeout", type=int, default=300)
    ur.add_argument("--json", action="store_true")
    ur.set_defaults(handler=cmd_unfollow_run)
    us = uu.add_parser("status", help="how many are left to unfollow")
    us.add_argument("--json", action="store_true")
    us.set_defaults(handler=cmd_unfollow_status)
    ures = uu.add_parser("reset", help="discard the unfollow progress")
    ures.add_argument("--yes", action="store_true", help="skip the confirmation")
    ures.set_defaults(handler=cmd_unfollow_reset)

    q = sub.add_parser(
        "queue", help="inspect and edit the follow queue",
        description="""The follow queue: who to follow next, in rank order. The same file
the GUI uses.

  list / rank      show it (rank also shows each entry's affinity score)
  add / remove / clear   edit it (your own account is filtered out)
  score            read the candidates' profiles and rescore by niche
                   affinity (slow: a page load per candidate)
  trim             keep only the top entries
  import / export  read or write a .json or .txt file""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    qq = q.add_subparsers(dest="queue_command", required=True)
    ql = qq.add_parser("list", help="the queue in rank order")
    ql.add_argument("--limit", type=int)
    ql.add_argument("--json", action="store_true")
    ql.set_defaults(handler=cmd_queue_list)
    qr = qq.add_parser("rank", help="the queue with its affinity scores")
    qr.add_argument("--limit", type=int)
    qr.add_argument("--json", action="store_true")
    qr.set_defaults(handler=cmd_queue_rank)
    qa = qq.add_parser("add", help="add usernames (own account is filtered out)")
    qa.add_argument("users", nargs="+")
    qa.set_defaults(handler=cmd_queue_add)
    qrm = qq.add_parser("remove", help="remove one username")
    qrm.add_argument("user")
    qrm.set_defaults(handler=cmd_queue_remove)
    qc = qq.add_parser("clear", help="empty the queue")
    qc.add_argument("--yes", action="store_true")
    qc.set_defaults(handler=cmd_queue_clear)
    qs = qq.add_parser("score", help="read the queue's profiles and rescore by affinity")
    qs.add_argument("--limit", type=int)
    qs.set_defaults(handler=cmd_queue_score)
    qt = qq.add_parser("trim", help="trim the queue to its top entries")
    qt.add_argument("--limit", type=int)
    qt.add_argument("--yes", action="store_true")
    qt.set_defaults(handler=cmd_queue_trim)
    qi = qq.add_parser("import", help="add users from a .json or .txt file")
    qi.add_argument("file")
    qi.set_defaults(handler=cmd_queue_import)
    qe = qq.add_parser("export", help="write the queue to a file")
    qe.add_argument("file")
    qe.set_defaults(handler=cmd_queue_export)

    h = sub.add_parser(
        "hashtags", help="manage the saved hashtags",
        description="""The saved hashtag list that follow's search mode scrapes.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hh = h.add_subparsers(dest="hashtags_command", required=True)
    ha = hh.add_parser("add", help="add hashtags (with or without the #)")
    ha.add_argument("tags", nargs="+")
    ha.set_defaults(handler=cmd_hashtags_add)
    hr = hh.add_parser("remove", help="remove one hashtag")
    hr.add_argument("tag")
    hr.set_defaults(handler=cmd_hashtags_remove)
    hl = hh.add_parser("list", help="show the saved hashtags")
    hl.add_argument("--json", action="store_true")
    hl.set_defaults(handler=cmd_hashtags_list)
    hc = hh.add_parser("clear", help="remove all hashtags")
    hc.add_argument("--yes", action="store_true")
    hc.set_defaults(handler=cmd_hashtags_clear)

    c = sub.add_parser(
        "config", help="read and write the settings",
        description="""Read or change bot_config.json (delays, limits, filters). `get`
shows one key or everything, `set` writes one value (numbers and booleans
are converted), `reset` restores the defaults.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cc = c.add_subparsers(dest="config_command", required=True)
    cg = cc.add_parser("get", help="one key, or every key without one")
    cg.add_argument("key", nargs="?")
    cg.add_argument("--json", action="store_true")
    cg.set_defaults(handler=cmd_config_get)
    cs = cc.add_parser("set", help="set one key (numbers and booleans are converted)")
    cs.add_argument("key")
    cs.add_argument("value")
    cs.set_defaults(handler=cmd_config_set)
    cr = cc.add_parser("reset", help="back to the defaults")
    cr.add_argument("--yes", action="store_true")
    cr.set_defaults(handler=cmd_config_reset)

    s = sub.add_parser(
        "status", help="browser, session, queue, hashtags, unfollow, last stats",
        description="""The whole app in one glance: browser, login, running session,
queue, hashtags, unfollow progress and the last session's stats.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(handler=cmd_status)

    lo = sub.add_parser(
        "logs", help="the recent log lines",
        description="""The session history (follow_bot.log). --tail N shows the last N
lines; --follow keeps printing new lines as they are written (tail -f).""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    lo.add_argument("--tail", type=int, default=50, help="how many lines (default 50)")
    lo.add_argument("--follow", action="store_true", help="keep printing new lines until Ctrl+C")
    lo.set_defaults(handler=cmd_logs)

    st = sub.add_parser(
        "stop", help="ask any running session to stop (works across processes)",
        description="""Ask any running session to stop: this process, or another one
through the stop.flag file that every cycle checks at each pause checkpoint.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    st.set_defaults(handler=cmd_stop)

    hp = sub.add_parser(
        "help", help="the command list, or one command's manual",
        description="""The command list without a topic; with a topic, that command's
full manual, e.g. `help follow`.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hp.add_argument("topic", nargs="?", help="a command name (follow, browser, queue, ...)")
    hp.set_defaults(handler=cmd_help)

    return parser


# ANSI colors for the startup banner; empty strings when stdout is not a
# terminal, so piped runs (scripts, tests) stay plain.
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RESET = "\033[0m"
if sys.stdout.isatty():
    _g, _y, _r = _GREEN, _YELLOW, _RESET
else:
    _g = _y = _r = ""

REPL_BANNER = f"""
✦══════════════════════════════════════════════════════════════════════════✦
                       ▛▀▖      ▗
                       ▙▄▘▞▀▖▞▀▖▄ ▛▀▖▙▀▖▞▀▖▞▀▖▝▀▖
                       ▌▚ ▛▀ ▌ ▖▐ ▙▄▘▌  ▌ ▌▌ ▖▞▀▌
                       ▘ ▘▝▀▘▝▀ ▀▘▌  ▘  ▝▀ ▝▀ ▝▀▘
──────────── ✦ The Instagram follow & unfollow assistant. ✦ ────────────

{_y}What you can do here:{_r}
  {_g}browser open / close / status{_r}   manage Chrome (log in with your account)
  {_g}follow{_r}                          extract from your hashtags, then follow
  {_g}unfollow{_r}                        unfollow accounts that do not follow you back
  {_g}queue, hashtags, config{_r}         manage the queue, hashtags and settings
  {_g}status, logs, stop{_r}              watch and control what is running

{_y}How it runs:{_r}
  {_g}python -m reciproca <comando>{_r}   one command in its own process; the browser
                                  is released when the command ends
  {_g}python -m reciproca (this){_r}      the shell: the browser stays open across
                                  commands, sessions run in the background and
                                  the prompt stays usable while they run

{_y}Good to know:{_r}
  {_y}• A one-shot command's Chrome window locks the profile until you close it.{_r}
  {_y}• `help` shows the full command list.{_r}
  {_y}• `help <comando>` shows one command's manual.{_r}
  {_y}• `quit` (or Ctrl+D) exits.{_r}
"""


def repl(commands=None):
    """Interactive shell over the same command tree.

    The one-shot CLI dies after every command, taking the driver - and with it
    the login flag - along. Here the process stays alive, so a browser opened
    by one command is still there for the next: the flow that is impossible
    across separate processes. `quit` closes the browser and returns to the
    shell.

    `commands` is for tests and piped input (`echo "status\\nquit" |
    python -m reciproca`); when None, lines come from input().
    """
    # The question branch below assigns _question_answer; without the global
    # declaration Python would shadow it with a local that dies at each line,
    # and the waiting worker would read None instead of the answer.
    global _interactive, _question, _question_event, _question_answer, _question_rendered
    print(REPL_BANNER)
    parser = build_parser()
    _interactive = True
    try:
        if commands is not None:
            lines = iter(commands)
        else:
            def _input_lines():
                while True:
                    try:
                        yield input("reciproca> ")
                    except (EOFError, KeyboardInterrupt):
                        return
            lines = _input_lines()

        for raw in lines:
            if _question is not None:
                # A worker thread is waiting for an answer; this line is it.
                # The GUI pauses the session on a modal messagebox; the shell
                # pauses it on this question. The worker already rendered the
                # question when it set it; re-render only on retries.
                if _question_rendered:
                    _question_rendered = False
                else:
                    print("\n" + _question, end="", flush=True)
                answer = raw.strip().lower()
                if answer in ("f", "follow", "y", "yes"):
                    _question_answer = "follow"
                elif answer in ("s", "save", "save_stop"):
                    _question_answer = "save_stop"
                elif answer in ("d", "discard"):
                    _question_answer = "discard"
                elif answer in ("quit", "exit"):
                    _question_answer = "discard"
                    _question_event.set()
                    break
                else:
                    print("   Answer with [f], [s] or [d].")
                    continue
                _question_event.set()
                # The worker clears _question in its finally; wait for it, so a
                # line that follows instantly (a piped script) is not mistaken
                # for a second answer.
                for _ in range(100):
                    if _question is None:
                        break
                    time.sleep(0.05)
                continue

            line = raw.strip()
            if not line:
                continue
            if line in ("quit", "exit"):
                break
            if line in ("help", "?"):
                parser.print_help()
                continue
            try:
                args = parser.parse_args(shlex.split(line))
                args._parser = parser
            except SystemExit:
                continue  # argparse already printed the error
            except Exception as e:
                print(f"✗ {e}", file=sys.stderr)
                continue
            try:
                args.handler(args)
            except KeyboardInterrupt:
                print("\nInterrupted.")
            except Exception as e:
                print(f"✗ {type(e).__name__}: {e}", file=sys.stderr)

            # Workers are one session each; finished ones are pruned.
            _repl_threads[:] = [t for t in _repl_threads if t.is_alive()]
    finally:
        _interactive = False

    if any(t.is_alive() for t in _repl_threads):
        print("A session is running - asking it to stop…")
        state.stop_requested.set()
        state.scoring_stop.set()
        with open(config.STOP_FLAG_FILE, 'w', encoding='utf-8') as f:
            f.write("stop\n")
        for t in _repl_threads:
            t.join(timeout=120)

    browser.handle_browser_closed()  # release the profile for other processes
    print("Bye.")
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        # One-shot: each invocation is one command in its own process, which
        # owns the browser for that command and releases it afterwards.
        parser = build_parser()
        args = parser.parse_args(argv)
        args._parser = parser
        try:
            return args.handler(args)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130
        except Exception as e:
            print(f"✗ {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    # No arguments: an interactive shell when on a terminal, a scripted
    # session when stdin is a pipe. Either way one process owns the
    # browser for the whole session.
    if sys.stdin.isatty():
        return repl()
    return repl(commands=iter(sys.stdin))


if __name__ == "__main__":
    sys.exit(main())
