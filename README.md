# Yerk AI

Yerk AI turns chat rooms into long-lived Claude Code sessions. It is a small Python program doing only what Claude Code does not: carry messages between a room and a session, build each room's context to a declared size, keep memory across sessions, and move work across the accounts you have. The agent loop, tools, skills, compaction and resume are Claude Code's own, used through its documented flags, settings and hooks.

## What it is, and what it is not

- A personal tool. You run it on your own machine, for yourself.
- Bring your own Claude Code. Yerk AI starts your `claude` command under the login you made with it. Each account is a Claude Code config folder named in your `accounts.toml`.
- No login handling for anyone else. Yerk AI never offers, brokers or copies a login, nor reads the credential files in a config folder (a test greps the package for any read under one). One thing touches your credentials: a one-year token you minted with `claude setup-token` can be kept in your private folder (`secrets/claude-<account>.token`, 0600) and is handed to Claude Code in `CLAUDE_CODE_OAUTH_TOKEN`. The usage meters in `yerk status` come from what Claude Code prints: `claude -p /usage` on the account's config folder when the card asks and every 15 minutes, ending a rest once usage is back (`usage-back`; no model call; a one-year token cannot read them, so it is left out), else the usage events in each room's stream. It never writes a config folder.
- Nothing stored for anyone else. Persona, memory and room settings live in a private folder you keep outside this repository; the repository ships only a fake one.
- Who it trusts. One person per install: it runs as your user and serves you; a room's people (`room-people`) are answered there, never trusted with your memory or machine. The machine's other users are not trusted: yerkd makes its state folder 0700, writes every file holding a credential or your words 0600, hands a room's token to its hooks through a 0600 file, never on a command line (any local user reads those in /proc), and `yerk doctor` names any file or folder in your private or state folder another user can read; keep the private folder 0700 too. A room's shell runs in Claude Code's sandbox, and no room may read a secret (a token, the bot token, a provider key, the daemon's own secrets, an account folder's login) with its Read tool or its shell, because a web page, a mail or a pasted message can steer a model. What a room recalls or imports, and what anyone but you writes (mail, a web page, a quoted message), reaches it marked as a record, never an instruction; a person's own message in their room is an ask to act on there, and an order in it to act as you, in another room or against a rule is reported, not followed. An outward action (a send to another room, a public post, a login, money, a text in your name, a message out through another tool) and a delete outside a room's work folder wait for your word in the room; a person's room can never post into yours. No credential is printed or logged, and the doctor names any secret-shaped line in your memory or imports by file and line, never by value, for you to scrub and rotate. It does not defend against root or anyone who can log in as you, a stolen token, a model that is wrong or that you tell to do harm, or a room you give an unsandboxed shell.

## Install

You need Linux with systemd, Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), Claude Code logged in on each account you name, and a Discord bot (below); `yerk abilities` names what some add (ffmpeg, yt-dlp, LibreOffice, TeX Live, node).

```sh
git clone <this repository's URL> yerk-ai && cd yerk-ai
uv sync
uv run yerk map example-room  # the example room's context against budgets.toml
cp -r examples/private ~/yerk-private && chmod 700 ~/yerk-private && mkdir -m 700 ~/yerk-private/secrets
YERK_PRIVATE=~/yerk-private uv run yerkd  # once yerkd.toml (below) names the bot's token; one line says so until then
```

Between the last two, make the folder yours. The bot: make an application in Discord's Developer Portal, turn on its bot's Message Content intent (or it reads nothing) and copy its token; invite it to your server with the `bot` and `applications.commands` scopes and permissions `274878008384` (View Channels, Send Messages, Send Messages in Threads, Read Message History, Add Reactions, Attach Files); [docs/GUIDE.md](docs/GUIDE.md), The private folder, has the rest. The token goes in `~/yerk-private/secrets/discord.token`, 0600. An id comes from Discord's Developer Mode (User Settings, Advanced): right-click the channel and Copy Channel ID, your name and Copy User ID. A first room's `~/yerk-private/yerkd.toml` is only this:

```toml
[discord]
token_file = "secrets/discord.token"
owner = "<your user id>"
```

In rooms.toml give `example-room` your channel, `channel = "discord:<channel id>"` (a room per chat; yerkd starts none without one), and in accounts.toml keep a line per Claude Code config folder you have (`one` is `~/.claude`), each room's `accounts` naming only those. Do not copy yerkd.toml.example whole: its `[watch] assistants` placeholder stops yerkd's start. `uv run pytest` runs the tests. The units in `units/` run yerkd and its nightly jobs as services.

## The private folder

Everything of yours lives in one folder outside the repository, named by `--private` or `YERK_PRIVATE`; `examples/private/` is a fake one the tests use. What runs keeps its state in a second folder, `YERK_STATE` (default `~/.local/state/yerk`).

```
~/yerk-private/                0700, never committed
├── rooms.toml                 a [rooms.NAME] table a room: channel, accounts, model, tools, privacy, its files
├── yerkd.toml                 the daemon: the bot's token file, your user id, memory, workers, gates
├── accounts.toml              a line an account: its Claude Code config folder and its order
├── jobs.toml                  scheduled jobs, a timer each (yerk jobs sync), an agent job's effort and model
├── persona.md, rulebook.md    what each room that names them is handed at its session start
├── rooms/NAME.md              a room's own voice
├── memory/                    day logs, fact files, the pinned memory, open loops, the rules file
└── secrets/                   0600 files: the bot token, provider keys, account tokens; no room reads them, and
                               a worker sees none but its own account's token
~/.local/state/yerk/           0700: the session table, the ledger, the index, logs, workers, the trash
```

Every key of those files, with its default, is in [docs/settings.md](docs/settings.md).

## A room's turn

Each active room is one `claude -p` process, stream-json in and out, on a session Yerk AI minted or the room's own resumed, under only the settings Yerk AI writes (`--setting-sources ""`): a read rule for the room's folder (an owner room's also for Yerk's running checkout, a worker's folder, fetched docs and doctor.json, as the mechanics say, to check a claim there; rules, never a folder its shell could write), read denies on every secret, a command hook on every event, and Claude Code's sandbox when the room has a shell. Its session start is one assembled file passed with `--append-system-prompt-file`, each part capped by budgets.toml ([docs/budgets.md](docs/budgets.md)). Each message gets the clock, metadata, changed rules and recall parts from the UserPromptSubmit hook ([docs/hooks.md](docs/hooks.md)), and Yerk AI's own tools (send, recall, remember, GitHub and the rest) come from its local MCP server ([docs/tools.md](docs/tools.md)). One, `text`, texts a person as you from your Mac, from your own rooms only, once you see its words and who it goes to and say the word, wherever the shell is; `texts` reads them, yours whole, while it sleeps; `email` emails from your Gmail under your name, files attached, alike (`email-name`). Another, `delete`, deletes paths outside the room's work folder, for good, from your own rooms only: the daemon does it once you read each path and say the word, never the root, a top folder, your home folder, a mounted disk or Yerk's own. `show` puts a room's file on the PC monitor you name, never one the PC would run. Another, `remember` with a `job`, files how a job is done for every room, in the rules file each session starts with; a rule that file has no room for loads only when a message names its topic, on its job's words and those you choose (`triggers`), and the job's older line is marked replaced. A `worker`'s end on exactly NO_REPLY is left to its card, its job and result (`job-cards`). A job fired as Yerk rewrites jobs.toml still starts (`job-fires`). A mid-turn message on Discord or a live page of code, talk and output (`page = true` or ask, on the Mac or PC monitor you name, painted with no click, answers in bubbles, chat on ask) joins that turn. A file sent to the room is saved in its `work/inbox/<message id>/` and named in the message, as is one another assistant in `[watch] assistants` attaches where the room reads or an old message carries (`attachments`). Only the text after the last tool call goes out, on one progress card the reply replaces, and the Stop hook bounces once on a rule check, a picture unread, a blank reply or a mid-turn message unnamed among them. A failed turn takes the action `failures.toml` gives its class: a rerun, a compaction, or the next account resuming the session. After 30 idle minutes the process closes; the next message resumes it; a session compacts between turns, never inside one, and the recall pack returns after it; every line is in the day log. [docs/GUIDE.md](docs/GUIDE.md), How a room runs, has every flag and rule.

## Add-ons

What a room does past Claude Code's tools comes as add-ons: MCP servers in rooms.toml, a `[rooms.NAME.mcp.SERVER]` table each or once for all owner rooms under `[defaults]`, tools in `allow`; a fix to one lands in its own repository (`plugin-builds`). One ships here, the web plug-in `plugins/web`: pages (Bookings as a guest, a Commons answer), PDF or Word files (`web-documents`), search and research (`research-ids`), in a worker too (`worker-web`) ([docs/GUIDE.md](docs/GUIDE.md), Web reach). A browser kept outside (`browser`): shoots a tab (`tab-shots`), opens a mail's links (`mail-links`) and files (`mail-files`), on your word books free on checkout pages (`free-bookings`) and orders whose payment method is an invoice (`order-forms`), clicks in apps with an Upgrade (`upsell-pages`), fills forms (`form-typing`), puts files on them (`form-files`), keeps a key unread (`page-keys`), gets age-locked reels (`locked-reels`).

## The docs

- [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md): who is who, one message from send to reply, and the project's words in plain ones. Read it next.
- [docs/GUIDE.md](docs/GUIDE.md): the long guide: running it, and how each part works inside
- Six reference pages that `yerk docs own` writes from the code: [commands](docs/commands.md), [tools](docs/tools.md), [settings](docs/settings.md), [budgets](docs/budgets.md), [hooks](docs/hooks.md), [units](docs/units.md)
- [docs/STATUS.md](docs/STATUS.md): how it was built, stage by stage; not checked against today's code


- [docs/GUIDE.md](docs/GUIDE.md): the long form: using it, moving or importing a chat, the service, backup, how a room runs, what you see, add-ons, voice notes, texting, web reach, the harness, the ledgers

- [CHANGELOG.md](CHANGELOG.md) says what each merge changes, [STYLE.md](STYLE.md) how every card and notice reads, and [NOTICE](NOTICE) where the lifted code comes from.

`yerk docs own` writes the first six from the code; a test fails the build when one is stale. `yerk docs refresh` is the other half: the Claude docs Yerk AI runs on, kept for every room ([docs/GUIDE.md](docs/GUIDE.md), Yerk knows its harness).

## License

MIT. See `LICENSE` and `NOTICE`.
