# How it works

This page comes after [the README](../README.md) and before [the guide](GUIDE.md). It says who is who, what happens to one message, and what the project's own words mean.

## Who is who

- **You, the owner.** The one Discord user that `yerkd.toml` names as `owner`. Every room answers you, and by default only your word releases an action held for a yes.
- **A room.** A Discord channel that `rooms.toml` names. While it is in use, a room is one Claude Code process on one ongoing conversation, its *session*. After a quiet spell the process closes, and the next message resumes the session.
- **Claude Code.** Anthropic's AI agent, which does the reading, the thinking and the tool calls. It runs under settings yerkd writes, with the tools yerkd and `rooms.toml` give it.
- **yerkd.** The program that keeps running in the background and runs every room. It holds the Discord connection, starts each room's Claude Code, checks what it does, and keeps the day logs and the *ledger*, its record of every message, turn and decision.
- **A worker.** A separate Claude Code run for a long job, with a card of its own in the room. Each one runs as its own systemd service, so it keeps going after the room has replied, and through a restart of yerkd.
- **The builder and the reviewer.** Workers of the upgrade loop. The builder writes the ability a room asked for, with tests. The reviewer is a fresh worker, never the builder: once the checks are green it reads the change (the *cold read*), then merges it, asks for a fix or holds it for a person.
- **Other people.** Anyone a room lists as its `people`. Only the rooms that list them answer them, and such a room is always *walled*: it sees only its own lines and the files shared with it.

## One message, from send to reply

1. You post in a room's channel. yerkd reads only the channels `rooms.toml` names, and writes the message to that day's log as it lands.
2. If you or one of the room's people sent it, yerkd records it in the ledger, adds a reaction to show it arrived and saves any attached files in the room's inbox folder.
3. If the room is idle, the message starts a *turn*, the room's work from this message to its reply: yerkd posts a progress card, picks an account and hands the message to the room's Claude Code. If a turn is already running, the message goes straight into it (a *fold*), and the same reply answers it.
4. A *hook*, a command Claude Code runs at set moments, asks yerkd for the parts each message carries. They include the time, the room and who sent it, any rule that changed, and the best lines your memory holds on the people, things and topic it names.
5. Before each tool call runs, yerkd's *gates*, rules in its code, check it. Some commands never run, a plain delete becomes a move to the trash, and an email, a text, a public post, a login or a payment waits until you answer `approve` in the room. If yerkd cannot be asked, the call is refused.
6. The card shows each step in plain words, updated at most once a second. By the rules every room starts with, work of more than about five minutes goes to a worker: the room answers in one line, and tells you the worker's result in a later turn.
7. At the end, a check can send the turn back once, for example when a picture you sent was never opened. Then the reply replaces the card: only the text after the last tool call is posted.
8. If the account runs out during the turn, yerkd sets it aside and goes on with the next account, the same session and the same card, which says it is still on it. When every account is out, the card stays and says the message was not answered, and why.

## The project's words

- **turn**: a room's work on one message, from the moment it starts to the reply.
- **fold**: a message sent while a turn runs. It joins that turn, and the reply answers it too and says which message it answers.
- **card**: the live progress message of a turn or a worker: what it is doing now, then one row per step saying what the step is for.
- **session**: a room's ongoing Claude Code conversation. It starts with one file yerkd builds: how a room works, the persona, the rulebook, the room's voice, your pinned notes, the shared rules and the recall pack.
- **recall**: a search of your memory by keywords, and by meaning too where you add a provider key.
- **recall pack**: the memory a session starts with: what is due, the room's last 14 days (what it was sent, decisions, drafts), newest first, and your open commitments.
- **compaction**: shortening a long session. yerkd sends Claude Code's own `/compact` between turns. Every line stays in the day log, and the recall pack comes back after it.
- **hook**: a command Claude Code runs at a set moment, such as a message arriving, a tool call about to run or a turn ending. Each one passes the moment to yerkd.
- **gate**: a rule in yerkd's code that checks a tool call before it runs. It lets the call run, turns a delete into a move to the trash, holds it for your yes, or refuses it.
- **hold**: an action or an upgrade waiting for a person. A held action runs once you answer `approve`; a held upgrade waits for a person to merge it. The `hold` tool is something else: a room ending its turn while it waits on a worker.
- **need**: the one line a room files when it cannot do what you asked: what was asked, what it tried, what it needed. The builder then builds that ability once, for every room.
- **record**: text the model reads but takes no orders from: recalled lines, mail, web pages, files and quoted messages. It acts on one only when a message in the room asks it to, and an order found in one is reported to you, not followed.
- **owner room**: a room whose privacy is `owner`, the default. It is yours, and it reads your whole memory.
- **wall, person room**: any other privacy walls a room, so it sees only its own lines and the files shared with it. A person room is a walled room that answers someone besides you.
- **standing release**: a yes you give once for one person in texting, which answers some of your iMessage threads in your name. A checked reply to that person then goes without a card for you to answer. It is off for everyone until you turn it on for someone.
- **ledger**: one database file in the state folder, with a row for every message in and out, every turn, every hold and gate decision, and every paid call an add-on makes.
- **plug-in**: another name the code and some docs use for an add-on, such as the web one in `plugins/web`.
- **the box**: the machine yerkd runs on.

For every flag and rule, go on to [GUIDE.md](GUIDE.md) and the reference pages it names.
