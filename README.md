# Yerk AI

Yerk AI turns chat rooms into long-lived Claude Code sessions. It is a small Python program that does only what Claude Code does not do by itself: carry messages between a room and a session, build each room's context to a declared size, keep memory across sessions, and move work across the accounts you already have. The agent loop, tools, skills, compaction and resume are Claude Code's own, used through its documented flags, settings and hooks.

## What it is, and what it is not

- A personal tool. You run it on your own machine, for yourself.
- Bring your own installed Claude Code. Yerk AI starts the `claude` command you already have, under the login you already made with it. Each account is just a Claude Code config folder named in your own `accounts.toml`.
- No login handling. Yerk AI never offers, brokers, copies or stores a login, a token or a password. It never reads the credential files in a config folder.
- Nothing stored for anyone else. Persona, memory and room settings live in a private folder you keep outside this repository; the repository ships only a fake example of it.

## Status

Layer 1, run 1: the engine adapter, the context assembler and `yerk map`, two contract probes, the budget and scrub tests. The chat adapter comes next.

## Use

```
uv sync
uv run yerk map example-room          # the example room's context, part by part, against budgets.toml
uv run yerk probe hello --config-dir ~/.claude
uv run yerk probe fold --config-dir ~/.claude
uv run yerk probe resume --config-dir ~/.claude --to-config-dir ~/.claude-two
uv run yerkd --room example-room --config-dir ~/.claude   # a console room: type a line, get a reply
uv run pytest
```

- `--private DIR` or `YERK_PRIVATE` points at your private folder (default: the fake one in `examples/private/`).
- `YERK_STATE` sets the state folder (default `~/.local/state/yerk`): the room-to-session table, session locks, the run ledger, probe logs.

## How a room runs

One `claude -p` process per active room, stream-json in and out, with:

- `--session-id` on a session Yerk AI minted, or `--resume` on the room's own session; one live process per session, by lock file
- `--setting-sources ""` and `--settings`, so only the settings Yerk AI passes apply
- `--strict-mcp-config`, a `--tools` allowlist, `--model`
- `--append-system-prompt-file` with the assembled context, built to the sizes in `budgets.toml`
- a run ledger row per turn: the account that ran it, the models that ran, time to the first visible event, time to the end, how it ended

Failures are classified from one table, `failures.toml`, keyed on Claude Code's own error types.

## Files

- `budgets.toml`: every size limit, in characters as Claude Code counts them
- `failures.toml`: failure classes and what each one does next
- `rooms.toml.example`, `accounts.toml.example`: templates for your private folder
- `yerk_ai/lifted/`: code taken from other MIT projects and from the author's own tools, each file with its origin; see `NOTICE`

## License

MIT. See `LICENSE` and `NOTICE`.
