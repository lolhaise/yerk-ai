# yerk_ai

The program itself: the Python package behind the `yerk`, `yerkd` and `yerk-index` commands. Each file opens with what it is for; this page groups them by job. [docs/HOW-IT-WORKS.md](../docs/HOW-IT-WORKS.md) says how the parts work together.

## Starting and running the rooms

- `__init__.py`: Marks the folder as the yerk_ai Python package and holds its version number.
- `accounts.py`: Loads the Claude Code accounts from accounts.toml, rests one that fails and picks the next.
- `beat.py`: Tells systemd the daemon is alive while its event loop answers, and records when the loop is held up.
- `cli.py`: The `yerk` command: every subcommand and its flags.
- `config.py`: Where Yerk AI keeps things and how it reads them: its own tables, the private folder, the state folder.
- `daemon.py`: yerkd, the background service that runs every room, its tools and its hooks.
- `engine.py`: Runs each room's long-lived Claude Code process and talks to it in Claude Code's headless protocol.
- `hooks.py`: Connects Claude Code's hooks to the daemon and says what is done on each hook event.
- `mechanics.md`: The working notes every room is handed at session start: its clock and place lines, how to reply, records.
- `room.py`: A room at run time: its Claude Code process, messages that come mid-turn, idle close, failed turns run again.
- `words.py`: Every word a card, notice or status line shows, kept in one place so all rooms read alike (see STYLE.md).
- `workers.py`: Workers: runs a heavy job as a separate Claude Code run with its own card, and reports back when it ends.

## What a room is shown, and what it shows you

- `card.py`: Builds the live progress card a room shows while it works, from Claude Code's stream events.
- `context.py`: Builds what a room's model is shown: one file at its session's start, and small parts with each message.
- `evidence.py`: Records what each turn read, and after each reply checks its claims against those reads.

## Memory

- `cards.py`: `yerk cards`: merges two fact cards (memory files of facts on one subject) by hand, lists like-named ones.
- `conversion.py`: `yerk curate --lessons`: rewrites the rules files as lesson files, each a summary line, a reason, examples.
- `curate.md`: The instructions `yerk curate` gives a model, one section per step.
- `curate.py`: `yerk curate`: each night tidies the fact cards and adds new facts to them from the day's notes.
- `docs.py`: `yerk docs`: keeps local copies of the Claude documentation pages, as files every room can search.
- `due.py`: What is due: the notes filed with a date, and which of them are still open.
- `embeddings.py`: Search by meaning beside the keyword search: turns memory into vectors and finds the nearest notes.
- `files.py`: Writes the text of a spreadsheet or PDF sent to your own room beside it, so a room can search it.
- `lessons.py`: Keeps the rules a room is given as lessons, one small file each, and the index a room loads at session start.
- `location.py`: Says which of your named places you are at, from a phone's location fixes, for your rooms and the day log.
- `memindex.py`: yerk-index, the memory search program: a keyword index of the memory folder, and a meaning index with a key.
- `memory.py`: Reads and writes the memory folder: the day log, exact reads of its files, recall of earlier notes.
- `mentions.py`: When a message names one of your people, places or things, puts the matching fact-file lines before the model.
- `move.py`: `yerk room add`: turns a Discord chat into a room, history included; `yerk import` copies history only.
- `sources.py`: `yerk import imessage` and `yerk import gmail`: copy your texts and mail into the private folder as files.
- `taste.py`: Your ratings of a room's replies, kept as rows and shown to your own rooms beside what the room had said.

## Tools a room can use, and the checks on them

- `abilities.py`: Reads abilities.toml and answers what Yerk AI can do or could build, what each needs, and its state here.
- `addons.py`: `yerk add` and `yerk remove`: installs and removes add-ons, abilities kept in repositories of their own.
- `captions.py`: Takes a video's own captions as its transcript, for free, before any paid transcription of its sound.
- `deletes.py`: The delete tool: on your approval, deletes paths outside a room's work folder, for good.
- `emails.py`: The email tool: sends one email in your name from your own Gmail, once you approve it.
- `gates.py`: The gates: rules in code that check each tool call before it runs and hold, rewrite or refuse risky ones.
- `inbox.py`: Keeps a copy of your recent texts from your Mac, so a room can answer about them while the Mac sleeps.
- `logins.py`: Saves a site login you hand over with /login as a secret file, so no room ever sees the password.
- `mcp_server.py`: Yerk AI's own MCP server: the 25 tools every room gets beyond Claude Code's own, on this machine only.
- `pictures.py`: Makes pictures sent to a room findable later (their words and what they show) and zooms in on part of one.
- `render.py`: The render tool: turns a document in a room's folders into a PDF beside it and counts its pages.
- `texts.py`: Sends one text (iMessage, else SMS) in your name from your Mac, once you have approved it.
- `transcribe.py`: The transcribe tool: turns a voice note or recording into text with a time per segment, saved beside it.
- `unwrap.py`: Works out which commands a shell line would really run and what a delete would touch, for the safety gate.
- `voice_note.py`: The voice_note tool: turns a script a room writes into a spoken voice note it can post to Discord.
- `watch.py`: The watch tool: fetches a linked video's still frames and transcript, so a room answers off the video itself.

## Building its own upgrades

- `deploy.py`: `yerk deploy`: moves the running daemon onto a new commit and proves it is up, else rolls back.
- `git_askpass.py`: Answers git's login prompt for github.com with a token from a file, so the token stays off command lines.
- `github.py`: Talks to GitHub: finds, reads and merges the self-upgrade loop's pull requests, and looks one up for you.
- `needs.py`: Missing abilities: a room files what it could not do, and one worker builds that ability for every room.
- `pipeline.py`: Takes a filed need through build, checks, a fresh review, merge and deploy, one at a time; sensitive ones wait.
- `shape.py`: The rule-shape check: scores each rule file a room loads against Anthropic's prompting guide, by exact counts.
- `taste-rubric.md`: The instructions given to the taste judge: decide whether you would complain about one reply, and why.
- `taste_eval.py`: `yerk eval taste`: measures how well the taste judge agrees with the replies you complained about or accepted.

## Looking after the install

- `backup.py`: `yerk backup` and `yerk restore`: saves what one failed disk would lose in one archive, and puts it back.
- `doctor.py`: `yerk doctor`: checks the install once a day and repairs files a crash or a power cut damaged.
- `health.py`: Checks once a day that each Claude account works, with one tiny model call, and is still the same login.
- `jobs.py`: Scheduled jobs: turns jobs.toml into systemd timers and runs each job when it is due.
- `latency.py`: `yerk bench latency`: how fast the rooms answered over the last days, from what is already recorded.
- `ledger.py`: The ledger: one SQLite database of each message in and out, each turn, worker and approval.
- `meters.py`: Each Claude account's 5-hour and weekly usage and reset times, read from what Claude Code prints.
- `morning.py`: `yerk morning`: a daily report on how the install ran and what changed, posted to the machine room at 06:30.
- `owndocs.py`: `yerk docs own`: writes the reference pages in docs/ from the code, so a test fails a stale page.
- `retention.py`: `yerk retention`: the daily sweep that moves old logs, transcripts and scratch to a trash, deleted days later.
- `status.py`: What is running now (rooms mid-turn, workers, jobs, account usage, memory, CPU), for `yerk status`.
- `usage.py`: Reads each account's usage meters live by running Claude Code's own /usage, for `yerk status`.

## Sub-folders

- `channels/`: the ways messages reach a room and its answers come back: Discord, and the live page, a room you open as a browser tab.
- `lifted/`: small pieces of code taken from other projects, each file saying where it came from; the NOTICE file at the root lists them.
- `texting/`: texting as you: reading your Messages threads through your Mac, deciding and checking a reply, and sending it once it is released.
