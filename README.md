# Reciproca

Desktop tool (Tkinter + Selenium) that unifies everything into a single tabbed GUI:

- **🎯 Auto Follow** — automatic following from a persistent queue or via hashtag search ("Deep Search"), with rate-limit detection, batch cooldowns and retries.
- **🚫 Unfollow** — automatically unfollows people who don't follow you back, computed from the `followers.json` / `following.json` files exported by Instagram. Reuses the same browser/login as the Follow tab.
- **📋 Follow Queue** — management of the queue of users to follow (import/export, manual add/remove).
- **⚙️ Settings** — extraction parameters, follow/unfollow timing and technical settings, persisted to `bot_config.json`.
- **📝 Logs** — real-time colored log, exportable to file.

## ⚠️ Important notice

This tool automates actions on Instagram (profile/hashtag scraping, bulk follow/unfollow) through a Selenium-controlled browser, including measures to reduce bot detection. This kind of automation **violates Instagram's Terms of Service** and can lead to temporary restrictions or a ban of the account used. Use it at your own risk, with an account you are willing to lose, and keep delays/limits conservative.

## Setup

```bash
pip install -r requirements.txt
python reciproca.py
```

Requires Google Chrome to be installed: `webdriver-manager` automatically downloads the matching ChromeDriver.

## Quick start

### Follow
1. **Auto Follow** tab → **Open Browser**, then log into Instagram manually.
2. Pick the mode: **Deep Search** (finds new users via hashtags and adds them to the queue, selected by default) or **Follow from Queue** (follows users already queued).
3. Set delay min/max and the follow limit for the session, then **Start Following**.

### Unfollow
1. In Instagram: **Settings → Privacy and security → Download your data**, request the export in JSON format and download `followers_1.json` (or similar) and `following.json`.
2. **🚫 Unfollow** tab → **Open Browser** (if not already open) and log in. There is one browser shared with the Follow tab, so either tab's button opens the same one.
3. **Load JSON**, select the two files. The tool automatically computes who you follow that doesn't follow you back.
4. Set delay min/max and the session limit, then **Start Unfollow**. Progress is saved to `unfollow_progress.json`, so you can stop and resume in later sessions without starting over — the tab shows how much is left and how much is already done from the moment you open the app.

When the list runs out, request a fresh export and load it: progress is kept as long as the browser is logged into the same account. Log into a different one and Reciproca sets that record aside under the previous account and picks up the new account's own — switching back restores it. **Reset** discards the current account's record on purpose, and says what it is about to delete before doing it.

### Bot filter

Profiles that will not reciprocate — nothing posted, almost no followers, following
thousands — are rejected before being followed and dropped from the queue. The
thresholds are in **Settings → 🤖 Bot Filter**; set `BOT_FILTER_ENABLED` to 0 to turn
the check off.

The counts live on the profile page and not in the followers list a candidate is
found in, so the check runs at follow time, where the browser is already on the
profile and it costs nothing extra. It therefore does not keep bots out of the
queue — it stops them being followed. A profile whose counts cannot be read is
followed anyway, with a warning in the log, so a change in Instagram's markup cannot
quietly block every follow.

### Coming Soon...
- Semantic ranking of users during the Deep Search phase, powered by AI
- Multi-language support (see below)

## Instagram language support

Instagram renders its interface in the account's own language, so Reciproca has to
match button and warning text per locale. **Currently supported: English and Italian.**

All locale strings live in one block at the top of `reciproca.py`
(`FOLLOWING_BUTTON_MARKERS`, `FOLLOW_BUTTON_MARKERS`, `UNFOLLOW_CONFIRM_MARKERS`,
`POSTS_LABEL_MARKERS`, `CLOSE_BUTTON_LABELS`, `RATE_LIMIT_MARKERS`), so adding a
language means editing that block only — no call site needs to change.

## Building a standalone Windows executable

PyInstaller cannot cross-compile, so a Windows `.exe` has to be built **on Windows**.

### Prerequisites

- **Python 3.14.3**, which ships **Tcl/Tk 8.6** — both are reported in the banner
  `build.bat` prints before it starts
- Python installed **with the "tcl/tk and IDLE" option**. It is optional in the
  installer and easy to leave out; without it there is no tkinter and no GUI.
  Already installed without it? Re-run the installer and choose *Modify*.

> **Check the Tk line in the banner: it must read 8.6.**
>
> If it reads 9.0 you are on a different Python, and the build will succeed and
> then fail at startup in `pyi_rth_tkinter` — on that machine only, while your
> other one keeps working, which makes it look like a code problem. Tcl/Tk 9
> changed its directory layout and PyInstaller bundles it differently.
>
> This is the whole reason to keep build machines on the same Python version, and
> the banner turns it into a one-second check.

### Build

```bat
build.bat
```

It prints the interpreter, Python version and Tk version it is about to use, then
installs the dependencies plus PyInstaller and produces `dist\Reciproca\Reciproca.exe`.
Check that banner first whenever a build misbehaves — a machine with more than one
Python is the usual explanation.

To build by hand instead: `pip install -r requirements.txt pyinstaller` then
`pyinstaller reciproca.spec`. Delete `build\` first if you have upgraded anything:
PyInstaller caches its analysis there, and a stale cache silently undoes the upgrade.

This is a **one-folder** build: keep everything inside `dist\Reciproca\` together —
the executable needs the files next to it. Copy that whole folder wherever you like.

Notes:

- **Put it somewhere writable.** The app stores its queue, follow history, settings,
  Chrome login profile and logs *next to the executable*, so `Program Files` is a
  poor choice. A folder under your user directory works well.
- **First run needs internet.** `webdriver-manager` downloads the ChromeDriver
  matching your installed Chrome. Afterwards it is cached.
- **Antivirus.** PyInstaller output is sometimes flagged as a false positive. The
  one-folder build trips this far less often than a one-file build.
- **Debugging a build that won't start.** Set `console=False` to `True` in
  `reciproca.spec` and rebuild to get a console window showing the traceback.
  Errors while opening the browser are also written in full to `follow_bot.log`
  next to the executable, so you can diagnose without rebuilding.
- **A `RequestsDependencyWarning` at startup is harmless** — it means the build
  machine has `urllib3`/`charset_normalizer` versions `requests` doesn't
  recognise. It does not affect the app.

## Tests

The Python tests cover the queue's ranking, the rotation of scraped authors, the
bot filter's parsing and verdict, and the browser-state handling. They need no
browser and no extra packages:

```bash
python -m unittest discover -s tests -t tests
```

`tests/test_extraction.js` checks the followers-dialog extraction: that each row's
follow button is matched to the right user, so accounts you already follow are
excluded. It reads the script straight out of `reciproca.py`, so it cannot drift
from the shipped code.

```bash
npm install jsdom
node tests/test_extraction.js
```

Node and jsdom are only needed for that one test, not to run the app.

## Generated files (git-ignored)

`chrome_profile/`, `follow_queue.json`, `followed_history.json`, `user_frequencies.json`, `scraped_authors.json`, `hashtags.json`, `bot_config.json`, `unfollow_progress.json` (plus one `unfollow_progress_<account>.json` per account you have switched away from), `unfollow_last_session.json`, and various logs — see `.gitignore`.

## License

GPL-3.0, see [LICENSE](LICENSE).
