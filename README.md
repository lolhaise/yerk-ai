# Yerk AI

Yerk AI turns chat rooms into long-lived Claude Code sessions. It is a small Python program that does only what Claude Code does not do by itself: carry messages between a room and a session, build each room's context to a declared size, keep memory across sessions, and move work across the accounts you already have. The agent loop, tools, skills, compaction and resume are Claude Code's own, used through its documented flags, settings and hooks.

## What it is, and what it is not

- A personal tool. You run it on your own machine, for yourself.
- Bring your own installed Claude Code. Yerk AI starts the `claude` command you already have, under the login you already made with it. Each account is just a Claude Code config folder named in your own `accounts.toml`.
- No login handling for anyone else. Yerk AI never offers, brokers or copies a login, and it never reads the credential files in a config folder (a test greps the package for any read under one). One thing touches your own credentials, and it never prints or logs one: a one-year token you minted yourself with `claude setup-token` can be kept in your private folder (`secrets/claude-<account>.token`, 0600) and is handed to Claude Code in `CLAUDE_CODE_OAUTH_TOKEN`. The usage meters in `yerk status` come from what Claude Code itself prints: its own `/usage`, run as `claude -p /usage` on the account's config folder when the card is asked for (Claude Code reads its own login there; a one-year token cannot read the meters, so it is left out of that call; no model call is made), else the usage events in each room's stream. It never writes a config folder.
- Nothing stored for anyone else. Persona, memory and room settings live in a private folder you keep outside this repository; the repository ships only a fake example of it.
- Who it trusts. One person per install: it runs as your user and serves you, and the people a room lists are answered there but never trusted with your memory or your machine. The machine's other users are not trusted: yerkd makes its state folder 0700, writes every file that holds a credential or your words 0600, hands a room's token to its hooks through a 0600 file and never on a command line (which every local user can read in /proc), and `yerk doctor` names any file or folder in your private or state folder another user can read; keep the private folder 0700 too. A room's shell runs in Claude Code's sandbox, and no room may read a secret (a token, the bot token, a provider key, the daemon's own secrets, an account folder's login) with its Read tool or its shell, because a web page, a mail or a pasted message can steer a model. What a room recalls or imports, and what anyone but you writes (mail, a web page, a quoted message), reaches it marked as a record, never an instruction; a person's own message in their room is an ask to act on there, and an order in it to act as you, in another room or against a rule is reported, not followed. An outward action (a send to another room, a public post, a login, money, a message out through another tool) waits for your word in the room; a person's room can never post into yours. No credential is printed or logged, and the doctor names any secret-shaped line in your memory or imports by file and line, never by value, for you to scrub and rotate. It does not defend against root or anyone who can log in as you, a stolen token, a model that is wrong or that you tell to do harm, or a room you give an unsandboxed shell.

## Install

You need Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), Claude Code installed and logged in on each account you will name, and a Discord bot (below).

```sh
git clone <this repository's URL> yerk-ai && cd yerk-ai
uv sync
uv run yerk map example-room                  # the fake example room's context, part by part, against budgets.toml
cp -r examples/private ~/yerk-private && chmod 700 ~/yerk-private && mkdir -m 700 ~/yerk-private/secrets
YERK_PRIVATE=~/yerk-private uv run yerkd      # every room with a discord:<channel id>, through the Discord adapter
```

Between the last two, make the folder yours. The bot: make an application in Discord's Developer Portal, turn on its bot's Message Content intent (or it reads nothing) and copy its token; invite it to your server with the `bot` and `applications.commands` scopes and permissions `274878008384` (View Channels, Send Messages, Send Messages in Threads, Read Message History, Add Reactions, Attach Files); [docs/GUIDE.md](docs/GUIDE.md), The private folder, has the rest. The token goes in `~/yerk-private/secrets/discord.token`, 0600. An id comes from Discord's Developer Mode (User Settings, Advanced): right-click the channel and Copy Channel ID, your own name and Copy User ID. A first room's `~/yerk-private/yerkd.toml` is only this:

```toml
[discord]
token_file = "secrets/discord.token"
owner = "<your user id>"
```

In rooms.toml give `example-room` your channel, `channel = "discord:<channel id>"` (a room per chat; yerkd starts none without one), and in accounts.toml keep a line per Claude Code config folder you have (`one` is `~/.claude`), each room's `accounts` naming only those. Do not copy yerkd.toml.example whole: its `[watch] assistants` placeholder stops yerkd's start until it is a real id or deleted. `uv run pytest` runs the tests. To run yerkd and its nightly jobs as services, the units are in `units/`, and [docs/units.md](docs/units.md) says when each one runs.

## The private folder

Everything that is yours lives in one folder outside the repository, named by `--private` or `YERK_PRIVATE`; `examples/private/` is a fake one the tests use. What runs keeps its state in a second folder, `YERK_STATE` (default `~/.local/state/yerk`).

```
~/yerk-private/                0700, never committed
├── rooms.toml                 a [rooms.NAME] table a room: channel, accounts, model, tools, privacy, its files
├── yerkd.toml                 the daemon: the bot's token file, your user id, memory, workers, gates
├── accounts.toml              a line an account: its Claude Code config folder and its order
├── jobs.toml                  scheduled jobs, a systemd timer each (yerk jobs sync)
├── persona.md, rulebook.md    what each room that names them is handed at its session start
├── rooms/NAME.md              a room's own voice
├── memory/                    day logs, fact files, the pinned memory, open loops, the rules file
└── secrets/                   0600 files: the bot token, provider keys, account tokens; no room reads them
~/.local/state/yerk/           0700: the session table, the ledger, the index, logs, workers, the trash
```

Every key of those files, with its default, is in [docs/settings.md](docs/settings.md); the long form is in [docs/GUIDE.md](docs/GUIDE.md), The private folder.

## A room's turn

Each active room is one `claude -p` process, stream-json in and out, on a session Yerk AI minted or the room's own resumed, under only the settings Yerk AI writes (`--setting-sources ""`): a read rule for the room's folder, read denies on every secret, a command hook on every event, and Claude Code's sandbox when the room has a shell. Its session start is one assembled file passed with `--append-system-prompt-file`, each part capped by budgets.toml ([docs/budgets.md](docs/budgets.md)). Each message gets the clock, metadata and recall parts from the UserPromptSubmit hook ([docs/hooks.md](docs/hooks.md)), and Yerk AI's own tools (send, recall, remember and the rest) come from its local MCP server ([docs/tools.md](docs/tools.md)). A message that arrives mid-turn is answered inside the same turn. Only the final text after the last tool call is delivered, on one progress card that is edited and then replaced by the reply, and the Stop hook bounces a turn once on a rule check, a picture it never read or a reply with nothing visible among them. A failed turn takes the action `failures.toml` gives its class: a rerun, a compaction, or the next account resuming the same session. After 30 idle minutes the process closes and the next message resumes it; a session compacts between turns, never inside one, and the recall pack comes back after it, so nothing said is lost: every line is in the day log. [docs/GUIDE.md](docs/GUIDE.md), How a room runs, has every flag and rule.

## Add-ons

What a room can do beyond Claude Code's own tools comes in as add-ons: MCP servers a room attaches in rooms.toml, a `[rooms.NAME.mcp.SERVER]` table each or once for every owner room under `[defaults]`, with their tools in the room's `allow`. One ships here, the web plug-in in `plugins/web`: pages, search and research for a room, kept out of budgets.toml's `[code]` count ([docs/GUIDE.md](docs/GUIDE.md), Web reach).

## The docs

- [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md): who is who, one message from send to reply, and the project's words in plain ones. Read it next., with its help
- [docs/tools.md](docs/tools.md): the tools a room is handed, in the words the model reads
- Six reference pages that `yerk docs own` writes from the code: [commands](docs/commands.md), [tools](docs/tools.md), [settings](docs/settings.md), [budgets](docs/budgets.md), [hooks](docs/hooks.md), [units](docs/units.md)
- [docs/budgets.md](docs/budgets.md): every size limit, with the notes that say why it is that size
- [docs/hooks.md](docs/hooks.md): each Claude Code event Yerk AI handles and what it does

- [docs/GUIDE.md](docs/GUIDE.md): the long form: using it, moving a chat in, importing one, the service, backup and restore, how a room runs, what you see, voice notes, web reach, the harness docs, the ledgers
- [docs/STATUS.md](docs/STATUS.md): the build's history layer by layer as it stood on 2026-10-24, kept as a record
- [CHANGELOG.md](CHANGELOG.md) says what each merge changes, [STYLE.md](STYLE.md) how every card and notice reads, and [NOTICE](NOTICE) where the lifted code comes from.

The first six are written from the code by `yerk docs own`, and a test fails the build when one of them is stale, so they say what the code does. `yerk docs refresh` is the other half: the Claude docs Yerk AI runs on, kept for every room to recall ([docs/GUIDE.md](docs/GUIDE.md), Yerk knows its harness).

## License

MIT. See `LICENSE` and `NOTICE`.
