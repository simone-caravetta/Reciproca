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
2. Pick the mode: **Follow from Queue** (follows users already queued) or **Deep Search** (finds new users via hashtags and adds them to the queue).
3. Set delay min/max and the follow limit for the session, then **Start Following**.

### Unfollow
1. In Instagram: **Settings → Privacy and security → Download your data**, request the export in JSON format and download `followers_1.json` (or similar) and `following.json`.
2. **Auto Follow** tab → **Open Browser** (if not already open) and log in.
3. **🚫 Unfollow** tab → **Carica JSON**, select the two files. The tool automatically computes who you follow that doesn't follow you back.
4. Set delay min/max and the session limit, then **Start Unfollow**. Progress is saved to `unfollow_progress.json`, so you can stop and resume in later sessions without starting over.

### Coming Soon...
- Semantic ranking of users during the Deep Search phase, powered by AI
- Multi-language support (see below)

## Instagram language support

Instagram renders its interface in the account's own language, so Reciproca has to
match button and warning text per locale. **Currently supported: English and Italian.**

All locale strings live in one block at the top of `reciproca.py`
(`FOLLOWING_BUTTON_MARKERS`, `FOLLOW_BUTTON_MARKERS`, `UNFOLLOW_CONFIRM_MARKERS`,
`CLOSE_BUTTON_LABELS`, `RATE_LIMIT_MARKERS`), so adding a language means editing
that block only — no call site needs to change.

## Building a standalone Windows executable

PyInstaller cannot cross-compile, so a Windows `.exe` has to be built **on Windows**.

```bat
build.bat
```

That installs the dependencies plus PyInstaller and produces `dist\Reciproca\Reciproca.exe`.
To build by hand instead: `pip install -r requirements.txt pyinstaller` then
`pyinstaller reciproca.spec`.

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

## Generated files (git-ignored)

`chrome_profile/`, `follow_queue.json`, `followed_history.json`, `user_frequencies.json`, `hashtags.json`, `bot_config.json`, `unfollow_progress.json`, `unfollow_last_session.json`, and various logs — see `.gitignore`.

## License

GPL-3.0, see [LICENSE](LICENSE).
