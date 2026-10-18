# Yerk AI

Yerk AI turns chat rooms into long-lived Claude Code sessions. It is a small Python program that does only what Claude Code does not do by itself: carry messages between a room and a session, build each room's context to a declared size, keep memory across sessions, and move work across the accounts you already have. The agent loop, tools, skills, compaction and resume are Claude Code's own, used through its documented flags, settings and hooks.

## What it is, and what it is not

- A personal tool. You run it on your own machine, for yourself.
- Bring your own installed Claude Code. Yerk AI starts the `claude` command you already have, under the login you already made with it. Each account is just a Claude Code config folder named in your own `accounts.toml`.
- No login handling for anyone else. Yerk AI never offers, brokers or copies a login. Two things touch your own credentials, and neither ever prints or logs one: a one-year token you minted yourself with `claude setup-token` can be kept in your private folder (`secrets/claude-<account>.token`, 0600) and is handed to Claude Code in `CLAUDE_CODE_OAUTH_TOKEN`; and `yerk status` reads a config folder's own sign-in (or that token) only to ask for that account's usage meters, the way Claude Code's `/usage` does. It never writes a config folder.
- Nothing stored for anyone else. Persona, memory and room settings live in a private folder you keep outside this repository; the repository ships only a fake example of it.

## Status

Layer 2, run 1: the accounts as one engine (the failure table with a test per Claude Code error type, cooldowns per account and kind, the hop that continues the same turn, one-year token plumbing, the daily health call, the meters). Layer 1, run 3: one Discord test room answers through Claude Code. Measured there over 50 scripted messages, 10 of them sent mid-turn: none dropped, every mid-turn message answered inside the turn it landed in, first visible line 1.7 s and final reply 3.7 s at the median. Built so far: the room runner (the mid-turn fold, idle close and resume, the empty retry, the account hop), the Discord adapter, Yerk AI's MCP server with `send` and `hold`, the HTTP hook with the clock line, read access to each room's private folder, and the ledgers.

## Use

```
uv sync
uv run yerk map example-room                 # the example room's context, part by part, against budgets.toml
uv run yerkd                                 # every room with a discord:<channel id>, through the Discord adapter
uv run yerkd --console example-room --config-dir ~/.claude   # one room on the terminal: a line in, a JSON reply out
uv run yerk map example-room --wire          # also the sizes as sent, read from the room's Claude Code transcript
uv run yerk setup-room ROOM                  # a Discord room's test webhook; its URL goes to secrets/ (0600), never printed
uv run yerk room close ROOM                  # the running yerkd closes ROOM's process now (never mid-turn); the session stays
uv run yerk accounts                         # per account: order, token age and days left (warns inside 30), rests, last health call
uv run yerk accounts cool NAME MINUTES       # rest an account by hand; `clear NAME` lifts it; a running yerkd sees both at its next pick
uv run yerk accounts mint NAME               # `claude setup-token` for NAME: the browser flow shown, the token stored 0600, never shown
uv run yerk accounts token NAME              # or store a token from stdin (never echoed), 0600, issued today
uv run yerk accounts health                  # one tiny model turn per account and its login identity, a ledger row each
uv run yerk status                           # the daemon, its rooms, and each account's 5-hour and weekly meters with resets
uv run yerk probe hello|fold|resume|blocks|smoke|measure|check|idle --config-dir ~/.claude
uv run pytest
```

- `--private DIR` or `YERK_PRIVATE` points at your private folder (default: the fake one in `examples/private/`).
- `YERK_STATE` sets the state folder (default `~/.local/state/yerk`): the room-to-session table, session locks, the ledgers, cooldowns, logs.

## The private folder

- `rooms.toml`: one entry per room: channel (`discord:<channel id>`), account order, model, tool allowlist, privacy class, time zone, and the context files. `layer` names the folder those files come from (default: the private folder itself).
- `accounts.toml`: one line per account: `name = { config_dir = "...", order = 1 }`, optionally `token = "secrets/..."` (the default is `secrets/claude-<name>.token`). Adding or removing an account is that one line; a room's own `accounts` list only orders the accounts it names, the rest follow.
- `yerkd.toml`: the daemon's settings: the Discord token file, the owner's user id, webhook ids accepted as senders in test mode, the local port. A running `yerkd` writes its pid and port to `yerkd.json` in the state folder.
- `secrets/`: files only you can read (mode 0600), for example the bot token. Nothing in the repository reads them except the daemon at start.

## How a room runs

One `claude -p` process per active room, stream-json in and out, with:

- `--session-id` on a session Yerk AI minted, or `--resume` on the room's own session; one live process per session, by lock file
- `--setting-sources ""` and `--settings`, so only the settings Yerk AI writes apply: a Read rule for the room's folder and the `UserPromptSubmit` hook, `type: http`, posting to the daemon
- `--add-dir` on the room's folder; every part of the appended context starts with `[from <absolute path>]`, so the model can re-read the file a fact came from
- `--mcp-config` naming only Yerk AI's server, with `alwaysLoad: true`, and `--strict-mcp-config`; a `--tools` allowlist; `--model`
- `--append-system-prompt-file` with the assembled context, built to the sizes in `budgets.toml`

The room runner:

- A message for an idle room starts a turn. A message that arrives mid-turn goes straight to the process's stdin and is answered inside the same turn; the replayed copy (`--replay-user-messages`) is the proof it was accepted.
- Only the final text after the last tool call is delivered. A turn owns one progress message, edited with its first visible line, then replaced by the reply, split under the channel's limit and never inside a code block.
- A failed turn takes the action `failures.toml` gives its class: transient reruns the same session (twice at most), overflow compacts then reruns, auth and quota rest that account (doubling per account and kind, kept across restarts) and continue the same turn under the same card on the next account, resuming the same session; a session that cannot be resumed starts fresh with a one-line recap; anything else is a visible notice. An empty reply retries once fresh.
- A turn Claude Code answered on a fallback model after a block stands, and its card and ledger row say so; the room's next turn starts a fresh session on the room's own model.
- After 30 idle minutes the process closes; the next message resumes the session.

The daemon's one local HTTP server carries `/mcp` (the tools `send`: text, or a file from the sending room's own folders, to a room; `hold`: end the turn while waiting on a named event or worker), `/hook` (the clock and metadata lines, keyed on the session id through the room-to-session table) and `/control` (`yerk room close`).

## Ledgers

One SQLite file, `ledger.sqlite`: `turns` (account, models, time to first visible event and to the end, how it ended, which messages it answered), `inbound` (every channel message by its id, written before anything runs; a crash runs it again once, marked), `outbound` (every message posted, with the id the channel returned), `holds`, `health` (the daily call per account: pass or fail, login method, an identity fingerprint, never the email). A turn row also names the failure's source in the table and the hop that brought the turn to its account.

## Files

- `budgets.toml`: every size limit, in characters as Claude Code counts them
- `failures.toml`: failure classes and what each one does next
- `units/`: the daily health call's systemd user timer (copy and enable it yourself)
- `rooms.toml.example`, `accounts.toml.example`: templates for your private folder
- `yerk_ai/lifted/`: code taken from other MIT projects and from the author's own tools, each file with its origin; see `NOTICE`
- `yerk_ai/channels/`: channel adapters (Discord)

## License

MIT. See `LICENSE` and `NOTICE`.
