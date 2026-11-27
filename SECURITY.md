# Security

How to report a security problem in Yerk AI, what this policy covers, and what Yerk AI trusts and defends against. It is a personal tool with one maintainer: it runs on your own machine, as your user, with your own Claude Code login. Read "What it is, and what it is not", below, before you install it: it says who it trusts, what it defends against and what it does not.

## Reporting a problem

Report a security problem privately, through GitHub's private vulnerability reporting on this repository: the Security tab, then "Report a vulnerability". Do not open a public issue for it.

Say what you found, the commit it is on and how to reproduce it. There is one maintainer, so an answer can take a few days.

## What it covers

The code in this repository: the daemon (`yerkd`), the `yerk` command, the hooks, the MCP server, the units in `units/` and the web plug-in in `plugins/web`. Claude Code, Discord and the services an add-on calls are not; report those to their makers.

## What it is, and what it is not

- A personal tool. You run it on your own machine, for yourself.
- Bring your own Claude Code. Yerk AI starts your `claude` command under the login you made with it. Each account is a Claude Code config folder named in your `accounts.toml`.
- No login handling for anyone else. Yerk AI never offers, brokers or copies a login, nor reads the credential files in a config folder (a test greps the package for any read under one). One thing touches your credentials: a one-year token you minted with `claude setup-token` can be kept in your private folder (`secrets/claude-<account>.token`, 0600) and is handed to Claude Code in `CLAUDE_CODE_OAUTH_TOKEN`. The usage meters in `yerk status` come from what Claude Code prints: `claude -p /usage` on the account's config folder when the card asks and every 15 minutes, ending a rest once usage is back (`usage-back`; no model call; a one-year token cannot read them, so it is left out), else the usage events in each room's stream. It never writes a config folder.
- Nothing stored for anyone else. Persona, memory and room settings live in your private folder.
- Who it trusts. One person per install: it runs as your user and serves you; a room's people (`room-people`) are answered there, never trusted with your memory or machine. The machine's other users are not trusted: yerkd makes its state folder 0700, writes every file holding a credential or your words 0600, hands a room's token to its hooks through a 0600 file, never on a command line (any local user reads those in /proc), and `yerk doctor` names any file or folder in your private or state folder another user can read; keep the private folder 0700 too. A room's shell runs in Claude Code's sandbox, and no room may read a secret (a token, the bot token, a provider key, the daemon's own secrets, an account folder's login) with its Read tool or its shell, because a web page, a mail or a pasted message can steer a model. What a room recalls or imports, and what anyone but you writes (mail, a web page, a quoted message), reaches it marked as a record, never an instruction; a person's own message in their room is an ask to act on there, and an order in it to act as you, in another room or against a rule is reported, not followed. An outward action (a send to another room, a public post, a login, money, a text in your name, a message out through another tool) and a delete outside a room's work folder wait for your word in the room; a person's room can never post into yours. No credential is printed or logged; the doctor names a secret-shaped line or a saved password in memory, imports or inboxes by file:line, never by value, to scrub and rotate. It does not defend against root or anyone who can log in as you, a stolen token, a model that is wrong or that you tell to do harm, or a room you give an unsandboxed shell.
