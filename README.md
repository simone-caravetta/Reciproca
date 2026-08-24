<img width="645" height="176" alt="Instagram Insights: 10,263 followers, +75.2% over 90 days" src="https://github.com/user-attachments/assets/9b85f505-a886-4b1f-ba0a-33323665f4e8" />

# Reciproca

Python tool for **testing growth strategies** on your own Instagram account
and **managing** the follow relationships they produce. A deterministic core
(Selenium drives an ordinary Chrome window) sits under five interchangeable
frontends: a command-line interface, a Tkinter desktop GUI, an MCP server,
a natural-language agent built on it, and a Telegram bot that talks to that
same agent from your phone.

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
change one thing at a time and see what that changed.

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
pip install -r requirements.txt            # core: CLI, GUI
pip install -r requirements-agent.txt      # MCP server + agent + Telegram (optional)
```

Google Chrome must be installed; `webdriver-manager` downloads the matching
ChromeDriver on first run.

All frontends share the same core, the same files and the same Chrome
profile, and can be used interchangeably:

- **Command line** - `python -m reciproca` starts the interactive shell:
  the browser stays open across commands, sessions run in the background
  and the prompt stays usable while they run. Every command also works on
  its own in one process: `python -m reciproca status`.
- **Graphical interface** - `python -m reciproca.gui` (or `python gui.py`)
  opens the five-tab desktop app.
- **Agent** - `python -m reciproca.agent` opens a REPL where you give goals
  in natural language and a LangChain agent drives the same tools.
- **Telegram** - `python -m reciproca.telegram_bot` is the same agent
  behind a chat bot: goals and monitoring from your phone, log lines
  streamed in throttled batches.
- **MCP server** - `python -m reciproca.mcp_server` exposes the same
  operations over the Model Context Protocol, for external orchestrators.

They read and write the same queue, history, settings, login profile and
logs, so a session started from one can be inspected or continued from the
other. A Chrome window left open by a one-shot command locks the profile
until the window is closed.

Reciproca downloads a little Embedding Model at runtime the first time you
execute the Semantic Evaluation. You can find the same model under:
"Xenova/paraphrase-multilingual-MiniLM-L12-v2"

## Command line

`python -m reciproca` with no arguments opens the shell; `help` lists the
commands and `help <command>` explains one in full (start with `help follow`,
the most complex one):

    browser open / close / status   manage Chrome (log in with your account)
    follow                          extract from your hashtags, then follow
    unfollow                        unfollow accounts that do not follow you back
    queue, hashtags, config         manage the queue, hashtags and settings
    status, logs, stop              watch and control what is running

Each command also runs as a one-shot: `python -m reciproca follow --mode
search --limit 10` runs one session and releases the browser when it ends,
so it can be scheduled or chained from scripts.

**First use:** `follow` and `unfollow run` open a visible Chrome window and
wait for you to log in (`--login-timeout` seconds, default 300). The login
persists in the browser profile, so later runs skip the wait.

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

The same flows exist in the CLI: `follow` is the Auto Follow tab
(`--mode search` for Deep Search, `--mode queue` for Follow from Queue) and
`unfollow run` is the Unfollow tab - `help follow` and `help unfollow` walk
through each one.

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

`weight` is the `SEMANTIC_WEIGHT` setting (default 60: a little more to the
affinity than to the count). A candidate with no affinity keeps the sighting
count as their whole rank.

Affinity is given by comparing your prompt of a desired kind of user and the profile
description found in user page on instagram. The comparison is given by a little 
embedding multi language model that runs on your ram and cpu (150-200mb). 

## Headless

All browser-facing commands accept `--headless`: no window, for unattended
runs. The first login must be visible (headless refuses to start without a
saved session in `chrome_profile/`), and the login persists afterwards.
Headless is experimental - behavior on Instagram can differ from a real
window.

## MCP server

`python -m reciproca.mcp_server` exposes the core as an MCP server over
**stdio**, for external orchestrators (Claude, Cursor, OpenClaw, your own
scripts - anything that speaks MCP). The tool surface mirrors the CLI:
`browser_open`, `follow_cycle`, `cycle_status`, `unfollow_run`, `queue_*`,
`hashtags_*`, `config_*`, `status`, `logs_tail`, `stop`.

Two rules by design:

- **One session at a time.** There is one browser and one session; starting a
  second cycle while one runs is refused cleanly, and a Chrome window opened
  elsewhere locks the profile until it closes.
- **No per-account primitive.** The server deliberately exposes no
  "follow this username" tool: the engine decides every individual follow,
  unfollow, delay and skip. An orchestrator decides *what* to run, never
  *how* an individual action is performed.

Long cycles are asynchronous: `follow_cycle` returns a `task_id` immediately
and `cycle_status` reports progress until done, so a tool call never pins an
orchestrator for the length of a session.

## Agent

`python -m reciproca.agent` opens a REPL where you give goals in natural
language - "avvia un follow di 10 dalla coda", "come va la sessione?" - and a
LangChain agent (REACT loop) decides which MCP tools to call, polls the
running cycle, narrates progress and reports back in your language.

```bash
pip install -r requirements-agent.txt
python -m reciproca.agent
```

The provider is configured in `agent_config.json` next to the app (template:
`agent_config.example.json`):

```json
{
  "provider": "openai",
  "model": "deepseek-v4-flash",
  "temperature": 0.2,
  "openai_compatible": {"base_url": "https://api.deepseek.com",
                        "model": "deepseek-v4-flash", "api_key": ""},
  "ollama": {"base_url": "http://localhost:11434", "model": "llama3.1"}
}
```

- **`provider`**: `anthropic` (default model `claude-sonnet-5`),
  `openai` (any OpenAI-compatible endpoint: DeepSeek, vLLM, unsloth, ...) or
  `ollama`.
- **Precedence**: environment overrides the file overrides defaults -
  `RECIPROCA_AGENT_PROVIDER`, `RECIPROCA_AGENT_MODEL`,
  `RECIPROCA_AGENT_BASE_URL`, `RECIPROCA_AGENT_TEMPERATURE`, plus the
  provider's standard key env (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`).
  A missing key fails with a clear error naming the variable.
- **Flags**: `--provider X --model Y --base-url URL` override the file;
  `--autonomous` pre-authorizes cycles and destructive changes (an explicit
  operator choice - the default is a confirmation checkpoint before long
  sessions and destructive changes); `--say "goal"` answers one goal and
  exits, for scripts.

A typical exchange:

    > segui 5 dalla coda
    🤖 La coda ha 288 candidati, ordinati per rank (60% affinità, 40%
    frequenza). Parto con i primi 5 - confermi?
    > si
    🤖 Ok, avvio. Apro il browser...
    🔧 follow_cycle({"mode": "queue", "limit": 5}) -> task a1b2
    🤖 Task avviato. Seguo i primi 5, ti aggiorno...
    > fermati
    🤖 Ok, interrompo subito. Ho seguito 2 dei 5.

The dialogue has memory, so it works on small local models as well as on
large API ones: each turn opens with a compact block summarising the
previous turns (last request, outcome, open question), built mechanically
from the turn's own messages - no AI summarisation, so it is as reliable
as the smallest model. The conversation works in English and Italian; a
bare confirmation in either language ("si", "ok", "certo", "yes", "sure",
"yeah", ...) is resolved explicitly against the pending question, never
left to inference. Asking for a recap ("riassumi la chat", "summarize the
conversation") widens the memory to the whole kept conversation, so the
answer is not cut off at the last few exchanges. The `wait` tool doubles
as the monitor: it reports the running task's status in its result and
ends early on every change, so progress narrates itself. A wait - or a
silent read-only poll - that keeps returning "nothing new" is capped, and
past the cap the runner closes the turn itself with the current status
line: the conversation never hangs on a model that polls without
narrating, and the running cycle is untouched either way (it lives in the
server). If the model falls silent while a task runs, the status line is
pushed directly; the status monitor itself is guarded, so a bad poll is
skipped instead of killing the updates.

(`🔧` tool calls are streamed into `follow_bot.log`, not the terminal, so
your typing is never interleaved with payloads.)

## Telegram

`python -m reciproca.telegram_bot` is the same agent runner behind a chat
bot (long polling): you send goals in natural language from your phone and
the agent's narration comes back as chat messages, while the session's log
lines are forwarded in throttled batches every few seconds. One process is
enough - the bot is the agent, and the browser stays on the machine that
runs it, exactly like every other frontend.

```bash
pip install -r requirements-agent.txt
python -m reciproca.telegram_bot
```

The bot's own settings live in `telegram_config.json` next to the app
(template: `telegram_config.example.json`):

```json
{
  "token": "123456789:ABC...",
  "allowed_chat_ids": [123456789]
}
```

- **Token**: create the bot with @BotFather and paste the token. The file is
  git-ignored (the token is a secret); `RECIPROCA_TELEGRAM_TOKEN` overrides it.
- **Chat id**: anyone not in `allowed_chat_ids` gets a reply telling them
  their chat id - write to the bot once, copy the number it answers, restart.
  An empty allowlist lets nobody in.
- The LLM provider is `agent_config.json`, the same file the REPL uses; the
  flags `--provider/--model/--base-url` and `--autonomous` work too.

While the agent sleeps (the `wait` tool), a chat message interrupts the wait
exactly like a typed command does in the REPL and is handled inside the
running turn; messages sent while it is not waiting are queued and become
the next turn, one at a time.

The wait also monitors: it reports the running task's status and wakes on
every change, so the agent narrates progress on its own (no need to ask
"come va?"). If the agent falls silent while a task runs, the bot sends the
status line directly; and if a model keeps polling without ever narrating,
the bot closes that turn with the current status line, so the chat never
hangs. Each chat keeps its own conversation memory, so a bare "si"/"yes"
resolves the previous turn's question instead of being forgotten.

## Coming soon

- [DONE] Semantic ranking of candidates during Deep Search, powered by AI
- [DONE] Telegram frontend: the same agent runner behind a chat bot, with
  throttled log forwarding
- More Instagram interface languages (see below)

## Instagram language support

Instagram renders its interface in the account's own language, so Reciproca matches
button and warning text per locale. **Currently supported: English and Italian.**
Every locale string lives in one block, `reciproca/markers.py`, so adding a
language means editing that block and nothing else.

## Windows executable

> Not yet ported: `build.bat`/`reciproca.spec` still target the old
> single-file `reciproca.py` and do not work with the package layout yet.

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
pip install -r requirements-agent.txt   # for the MCP/agent/Telegram test modules
python -m unittest discover -s tests -t tests   # queue, authors, bot filter, MCP tools, agent, Telegram bot
npm install jsdom && node tests/test_extraction.js   # followers-dialog extraction
```

The Python tests need no browser and no real account; the agent-stack tests
are skipped when the optional dependencies are missing. Node and jsdom are
only for that one JS test, not to run the app.

When a count in the log looks wrong for a particular account, this reads that
profile in your own logged-in browser and shows where each number came from:

```bash
python check_profile.py <username>   # close Reciproca first
```

## Generated files (git-ignored)

`chrome_profile/` and the app's own JSON state and logs, all written next to the
app. `agent_config.json` (your API keys) and `telegram_config.json` (your bot
token) are ignored too - copy `agent_config.example.json` and
`telegram_config.example.json` to create them. See `.gitignore`.

## License

GPL-3.0, see [LICENSE](LICENSE).
