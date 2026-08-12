<img width="645" height="176" alt="Instagram Insights: 10,263 followers, +75.2% over 90 days" src="https://github.com/user-attachments/assets/9b85f505-a886-4b1f-ba0a-33323665f4e8" />

# Reciproca

Desktop tool (Python, Tkinter, Selenium) for **testing growth strategies** on your own
Instagram account and **managing** the follow relationships they produce.

Everything runs on your own machine: no account to create, no API key, no third-party
service in the middle. You log into Instagram yourself, in an ordinary Chrome window
the tool drives, and the session stays in a browser profile next to the app. Your
password is never typed into Reciproca and never leaves your computer.

The approach is reciprocal growth. Reciproca looks for accounts that already follow
several people posting under your hashtags, a sign they follow within your niche and
might follow back. It ranks candidates by how strong that signal is and works down the
list at a deliberately human pace. Managing the other half is the **Unfollow** tab: it
reads your Instagram data export and clears the accounts that never reciprocated.

It is built for testing rather than for volume. Every threshold, delay and limit is a
setting, and every decision is written to the log with the reason for it, so you can
change one thing at a time and see what that changed. Five tabs: **Auto Follow**,
**Follow Queue**, **Unfollow**, **Settings**, **Logs**.

## Before you start

Reciproca drives Instagram through a Selenium-controlled browser, which goes against
Instagram's Terms of Service. In practice that may mean a temporary action block, and
in some cases the account could end up restricted.

It is a project for tinkering with browser automation and seeing how far careful
pacing gets you, so try it for fun on an account you would not mind losing, on your
own content and your own audience. The defaults are deliberately slow. Leaving them
alone, or making them slower, keeps the whole thing closer to what somebody could
plausibly do by hand, which is the point.

## Setup

```bash
pip install -r requirements.txt
python reciproca.py
```

Google Chrome must be installed; `webdriver-manager` downloads the matching
ChromeDriver on first run.

## Follow

1. **Auto Follow** → **Open Browser**, then log into Instagram by hand.
2. Choose **Deep Search** (finds new candidates via hashtags, the default) or
   **Follow from Queue**.
3. Set the delays and the session limit, then **Start Following**.
4. Select "Score profile against my Niche", enabling embedding model to add its
   user evaluation score inside ranking calculus (see Semantic Ranking for further
   info).

Candidates are ranked by how many of the scanned hashtag authors they already
follow, a score that adds up across sessions. Following starts from the top of that
list. Profiles that look automated (nothing posted, almost no followers, following
thousands) are skipped and dropped from the queue; the thresholds are under
**Settings → 🤖 Bot Filter**.

Each hashtag prefers authors it has never scraped, so repeated runs on the same tag
keep finding new people instead of the same few.

## Unfollow

1. In Instagram: **Settings → Privacy and security → Download your data**, request
   JSON, and download `followers_1.json` and `following.json`.
2. **Unfollow** → **Open Browser** (one browser is shared with the Follow tab).
3. **Load JSON** and pick the two files. Reciproca works out who doesn't follow
   you back.
4. Set the delays and the session limit, then **Start Unfollow**.

Progress is saved, so a list can be worked through over several sessions. It stays
with the Instagram account it was recorded against: export again and load the new
files to carry on. **Reset** discards it on purpose, and says what it will delete
first.

## Semantic Ranking

* Rank a candidate on one number

Groundwork for scoring a candidate's profile against the niche you describe. This
is the arithmetic only: nothing scores anything yet.

Two things have to be mixed that are not on the same scale. A sighting count is an
integer that grows without limit across searches; an affinity is already between 0
and 1. So the count is squashed by f / (f + 2):

    seen 1 -> 0.33    seen 3 -> 0.60    seen 10 -> 0.83
    seen 2 -> 0.50    seen 6 -> 0.75    seen 20 -> 0.91

The two are then weighed:

    rank = (1 - weight/100) * count_score + (weight/100) * affinity

Affinity is given by comparing your prompt of a desired kind of user and the profile
description found in user page on instagram. The comparison is given by a little 
embedding multi language model that runs on your ram and cpu (150-200mb). 

## Coming soon

- [DONE] Semantic ranking of candidates during Deep Search, powered by AI
- More Instagram interface languages (see below)

## Instagram language support

Instagram renders its interface in the account's own language, so Reciproca matches
button and warning text per locale. **Currently supported: English and Italian.**
Every locale string lives in one block at the top of `reciproca.py`, so adding a
language means editing that block and nothing else.

## Windows executable

PyInstaller cannot cross-compile, so build on Windows:

```bat
build.bat
```

This produces `dist\Reciproca\Reciproca.exe`.

- Needs **Python 3.14.3**, installed **with the "tcl/tk and IDLE" option**.
  `build.bat` prints the Python and Tk versions first: **Tk must read 8.6**. With
  Tk 9.0 the build succeeds and then crashes at startup.
- Keep everything inside `dist\Reciproca\` together, and put the folder somewhere
  **writable**: the queue, history, settings, login profile and logs are stored
  next to the executable.
- Delete `build\` after upgrading anything: PyInstaller caches its analysis there.

## Tests

```bash
python -m unittest discover -s tests -t tests   # queue, authors, bot filter, UI state
npm install jsdom && node tests/test_extraction.js   # followers-dialog extraction
```

The Python tests need no browser and no extra packages. Node and jsdom are only for
that one JS test, not to run the app.

When a count in the log looks wrong for a particular account, this reads that
profile in your own logged-in browser and shows where each number came from:

```bash
python check_profile.py <username>   # close Reciproca first
```

## Generated files (git-ignored)

`chrome_profile/` and the app's own JSON state and logs, all written next to the
app. See `.gitignore`.

## License

GPL-3.0, see [LICENSE](LICENSE).
