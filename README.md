# Yerk AI

**An AI assistant that lives in your chat rooms, runs on your own computer, remembers what you told it, and builds its own upgrades.**

You message it in Discord the way you would message a person. Each channel you give it is a *room*: one long-running assistant with its own voice, tools and privacy level. The thinking is done by [Claude Code](https://code.claude.com/docs/en/overview), Anthropic's AI agent built on its Claude models, which you install and log in to yourself. Yerk AI is the Python program around it, the part people call a *harness*: it carries messages in and out, decides what the model is shown, keeps the memory and keeps the rooms running.

## What it does

- **Answers where you already talk.** Message a room and it shows a live progress card while it works; the reply replaces the card.
- **Remembers.** Every message is kept in a day log on your disk. Each new message reaches the model with your earlier notes that bear on it, and a room can search the rest.
- **Works in the background.** A long job goes to a separate worker. It has a progress card of its own and reports back when it ends.
- **Keeps going when an account runs out.** With more than one Claude account, a turn moves to the next one and carries on.

## It builds its own upgrades

When a room cannot do what you asked, Yerk AI builds that ability into itself:

1. The room writes down what it needed, instead of building a one-off way of its own.
2. A builder writes the ability, with tests.
3. The checks run, and a fresh reviewer reads the change.
4. It is merged, deployed and live in every room.

No person is in that loop: the builder and the reviewer are Claude Code workers, and the reviewer never wrote the change. The riskiest changes still wait for a person (Guardrails, below). The loop needs a GitHub token and checks on your copy of the repository; without them, a person merges and deploys.

## Guardrails

It can reach your files, your mail and your accounts, so its limits are part of the program:

- **Your yes before it acts for you.** An email, a text, a public post, a login, a payment or a delete for good waits until you have seen it and said yes. The one exception is yours to make: a standing yes for texts to one person.
- **Secrets stay unread.** No room can read a token, a key or a login, with its file tool or its shell. Yerk AI never reads the credential files of your Claude Code login either, and a test fails the build if any code tries.
- **Outside text is a record, never an order.** A web page, an email or a quoted message reaches the model marked as a record, with a standing rule never to take orders from it.
- **Other people are walled off.** Someone else can be answered in a room of their own. It sees only its own lines and the files you share, it cannot post into your rooms, and the model's rules say an order in it to act as you is reported, not followed.
- **Risky changes wait for a person.** When it builds its own upgrades (above), a change to texting, email, logins, deletes, deploys or the pipeline itself is held for a human, and a change cannot loosen the list that judges it.
- **It says what it cannot stop.** [SECURITY.md](SECURITY.md) lists it: anyone with root or your login, a stolen token, a model that is simply wrong.

## Questions

**Is this a chatbot?** No. A chatbot answers; this also acts. A room can read and write files in its own folder, run commands in a sandbox, search the web, read PDF and Word files, and, with your approval, email or text as you (texting needs a Mac). You choose which of these each room gets.

**What do I need, and what does it cost?** A Linux machine, Claude Code logged in and a Discord bot you create ([Install](#install) has the list). Yerk AI itself is free under the MIT license and runs on your own Claude Code login, so the model use comes out of your Claude plan. A few optional abilities need a provider key or a paid service of your own; `yerk abilities` names them.

**Where does my data live?** On your disk: settings, persona, memory and secrets sit in one private folder outside this repository, and nothing of yours is kept on a server of the project's. Your messages still pass through Discord and, through Claude Code, to Anthropic's models, as they would without it. If you add a provider key for search by meaning, your memory, the day logs included, is sent to that provider to be indexed.

**Who is it for?** One person per install. Other people get a walled room of their own, and a shell on your machine only if you give them one.

**How big is it, and how do I know it works?** About 37,000 lines of Python, held under a ceiling a test enforces. More than 2,700 tests run on Python 3.12, 3.13 and 3.14 for every change, beside lint, type and dependency checks: the green checks on each commit. Every part Yerk AI builds for the model, at a session's start and with each message, has a declared size limit in `budgets.toml`, and the reference docs are written from the code: a test fails a stale page.

**What is the catch?** It is one person's tool, built in the open: Discord only, Linux only (plus a Mac if you want texting), version 0.1, and you set it up in a terminal. There is no hosted version.

**Can I contribute?** Issues are welcome. Pull requests are by invitation for now, so open an issue first ([CONTRIBUTING.md](CONTRIBUTING.md)).

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

In rooms.toml give `example-room` your channel, `channel = "discord:<channel id>"` (a room per chat; yerkd starts none without one), and in accounts.toml keep a line per Claude Code config folder you have (`one` is `~/.claude`), each room's `accounts` naming only those. Do not copy yerkd.toml.example whole: its `[watch] assistants` placeholder stops yerkd's start. The units in `units/` run yerkd and its nightly jobs as services.

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

## Add-ons

Past Claude Code's own tools and Yerk AI's own ([docs/tools.md](docs/tools.md)), a room's abilities come from add-ons: MCP servers (the standard way to hand a model new tools) named in rooms.toml. One ships here, `plugins/web`: web pages, PDF and Word files, search and research. `examples/addons/hello` is a small one to copy, and [docs/GUIDE.md](docs/GUIDE.md), Add-ons, has the rest.

## The docs

- [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md): who is who, one message from send to reply, and the project's words in plain ones. Read it next.
- [docs/GUIDE.md](docs/GUIDE.md): the long guide: running it, and how each part works inside
- Six reference pages that `yerk docs own` writes from the code: [commands](docs/commands.md), [tools](docs/tools.md), [settings](docs/settings.md), [budgets](docs/budgets.md), [hooks](docs/hooks.md), [units](docs/units.md)
- [docs/STATUS.md](docs/STATUS.md): how it was built, stage by stage; not checked against today's code




- [CHANGELOG.md](CHANGELOG.md) says what each merge changes, [STYLE.md](STYLE.md) how every card and notice reads, and [NOTICE](NOTICE) where the lifted code comes from.

Every folder has a README.md that says what is in it.

## License

MIT. See `LICENSE` and `NOTICE`.
