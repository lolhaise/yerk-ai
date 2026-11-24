# Settings

Written by `yerk docs own` from yerk_ai/config.py (ROOM_WORDS, DEFAULT_KEYS, YERKD_WORDS) and the tables beside the code that reads them (words.SETTINGS, memory.described, jobs.CONF, jobs.KEYS, retention.DAYS and CAP); change those and run it again, never this page.

Every key the loaders take from the private folder's rooms.toml, yerkd.toml and jobs.toml, in plain words with its default. rooms.toml.example and yerkd.toml.example are commented copies with example values.

## rooms.toml

One `[rooms.NAME]` table a room; `yerk doctor` names any other key as a typo.

- `accounts`: the accounts the room is tried on, in this order, then the rest of accounts.toml; the settings tool or /model can put it on one first
- `allow`: permission rules beyond Read (the tool goes in `tools` too); the gates run first. An owner room carries [defaults] allow unless it sets its own
- `allow_remove`: tools an owner room does without, out of the allow it inherits from [defaults]
- `also`: which of yerkd.toml's [memory] also roots reach the room's recall, read_memory and names (unset: all of them)
- `card`: count: a room with people shows its newest holding line and a step count, never a step's words (unset, steps or any other value: the owner rooms' card, the model's newest line and one row per step, done or not done)
- `channel`: the chat the room is, discord:<channel id>
- `class`: the class the room's day-log lines carry: private, room or shared (default: private in an owner room, room in any other)
- `compact_focus`: what the room's compactions are told to keep, the text after /compact (unset: Yerk AI's own; empty or false: none)
- `compact_window`: tokens the session keeps before Claude Code compacts (unset: budgets.toml [session]; 0: the model's own)
- `daily_notes`: the room's day-log folder (default memory/daily)
- `effort`: passed as --effort: low, medium, high, xhigh or max (unset: the model's own default)
- `id`: the room's id in the day logs and the ledgers (default: its channel, else its name)
- `idle_compact_after`: minutes with no turn before a session at idle_compact_at or more is compacted (unset: budgets.toml [session])
- `idle_compact_at`: the share of compact_window past which an idle session is compacted between turns (unset: budgets.toml [session]; 0: never)
- `idle_compact_within`: the share of compact_window under Claude Code's own line (the window less 33,000) within which a session is compacted as soon as a turn ends with nothing waiting, not idle_compact_after later (unset: budgets.toml [session]; 0: never)
- `layer`: the folder the room's files come from, in the private folder (default: the private folder itself)
- `mcp`: the room's own MCP servers, one [rooms.NAME.mcp.SERVER] table each; an owner room carries each [defaults] server it does not set
- `mention_recall`: the people and things a message names, with their best lines, in its recall part (default true)
- `model`: the model the room runs on, an alias or id from the install's model list; also what a turn runs on while every account is out of the one the settings tool put it on
- `open_loops`: the open commitments file, kept whole in the recall pack at a session start
- `page`: true: a live page for the room, a browser tab at the address `yerk room page NAME` prints, where the owner's typing, drawing and test runs reach the room as the owner's messages and its replies and the page tool render (default false; the page tool's open and off switch it with no restart, kept in the room's override over this)
- `people`: Discord user ids answered in the room besides the owner, as sender person (unset: the owner only); needs privacy family or shared
- `persona`: the persona file, the session start's persona part
- `pinned_memory`: the pinned memory file, the session start's pinned_memory part
- `precompact_midturn`: on: a turn that reaches precompact_within's band mid-turn starts that copy at the call that reached it, once a turn, its exchange given to the copy as one request already answered; log: one line saying it would have, nothing started; off, the default, also for any other value (said once at the start)
- `precompact_within`: the share of compact_window under Claude Code's own line within which a turn's end compacts a copy of the session, the room left free, and the copy becomes its session once compacted, at a turn's end (unset: budgets.toml [session]; 0: never)
- `privacy`: owner (the default): the owner's room, which reads the whole memory; any other value walls the room: its own lines and the shared files only
- `read`: folders the room may read besides its own, from its work folder; a room with people takes none, trusted or not
- `recall_days`: how many of the newest day logs the recall pack reads at a session start (default 2)
- `role`: machine: the machine room, where the morning diff, the deploy card and the ops lines land
- `rulebook`: the rulebook file, the session start's rulebook part
- `sandbox`: [rooms.NAME.sandbox]: Claude Code's sandbox for the room's shell (default: Yerk AI's own when a shell tool is listed); a trusted room may add filesystem.denyRead, filesystem.denyWrite and the network allowlist, no other key
- `sealed`: true: in Yerk's own tree, the room's own lines only, no other room's lines, notes, facts or rules
- `shell`: trusted: a room with people that runs the owner rooms' tools, its shell inside the daemon's wall (`yerk explain trusted`), Write and Edit on its layer and work folder only
- `thinking_panel`: owner-only rooms: a thinking line on the card, about 5 s a reply (default false)
- `timezone`: the room's IANA zone: its clock line (default UTC) and its jobs (else [memory] timezone)
- `tools`: the built-in tools the room's Claude Code gets (--tools); a shell tool brings the sandbox
- `topic_recall`: the room's own lines on the message's topic, and the search of a question about the past, in its recall part (default true)
- `voice`: the room's voice file, the session start's room_voice part
- `watch`: discord:<channel id> of a channel the room watches: [watch] assistants' messages there go in its day log, never answered
- `watch_assistant`: the assistant the owner talks to in the watched channel, by name: a message read from there comes in marked as sent to it, not to this room (default: the other assistant)
- `watch_audit`: true: the room grades the watched assistant's answer to every watched message, a verdict line (🟢 looks good, 🟠 only when something is actually wrong and it changes what the person would say or do, 🔴 wrong) then the fix, never silent (default false: read along, answer only when asked)
- `watch_wait`: seconds the room waits for the watched assistant's answer after a watched message, counted again from that assistant's progress card's last edit while the card is up, at most three times this (default 150)
- `worker_addons`: the room's own add-on tools every worker started from it gets, each named as a worker start's addons names one (a tool as allow has it, or a server's name for each of its tools allow names); one allow has not, or on a server whose table passes a credential, is skipped and the worker starts. An owner room carries [defaults] worker_addons unless it sets its own; [] gives none

### [defaults]

Keys: allow, mcp, worker_addons. An owner room under [defaults] (`base`), in place: the default allow less the room's allow_remove, unless the room has its own allow, the default worker_addons unless the room has its own ([] for none), and each default mcp server, filled, unless the room has its own mcp.<name> table of that name; a key of the room's own replaces the default whole, its other servers kept after the defaults'.

## yerkd.toml

The daemon's settings, in the private folder beside rooms.toml, read when yerkd starts.

### [discord]

- `token_file`: the bot's token, a file in the private folder only you can read (0600)
- `owner`: the owner's Discord user id, answered in every room
- `test_senders`: webhook ids accepted as senders in test mode (`yerk setup-room` makes one a room)
- `guild`: the Discord server's id, where `yerk setup-room` finds or makes a room's channel and its test webhook (that command only)
- `webhook_burst`: webhook events per 5 minutes before 10 minutes off, the loop guard (default 100)
- `commands`: the slash commands (default true)

### [http]

- `port`: the local port of /mcp, /hook and /control (default: a free one; yerkd --port wins)

### [page]

- `open`: the command that opens a room's live page on the owner's screen, a list, the address added last (default: the box's own: wslview or Windows' url.dll under WSL, open on a Mac, xdg-open with a display)
- `screen`: the PC's monitor the page tool's open puts the page on under WSL, maximized, the screen woken with no click: left, middle, right, primary or its number from the left (default: none, where the browser puts it; the tool's `screen` wins)
- `mac`: the ssh host of the owner's Mac, where the page tool's open with where mac runs `open` on the page's one-time address, handed on ssh's stdin (default: [imessage] host, else `ssh` in a mac.json beside an owner room's add-on's server, the hands plug-in's, a file of the owner's no one else writes)
- `address`: yerkd as the owner's Mac reaches it, the box's tailnet or LAN host and its port (http://box.example.ts.net:8787; no port: yerkd's), the address an open on the Mac carries and `yerk room page` prints (none: listen's, else no page opens on the Mac)
- `listen`: the box's tailnet or LAN address (100.64.0.1; `tailscale ip -4` prints the tailnet's) where yerkd also serves the page's routes, and nothing else, on its port, so the owner's Mac and phone open the page (default: the box's own tailnet address, as `tailscale ip -4` prints it at yerkd's start, else loopback alone; 127.0.0.1 keeps it on loopback alone); a wildcard, a public address or a host name is refused

### [style]

- `dialect`: the owner's own filler words (their slang), joined after the neutral ones to the pushback, lead, trailing and name-cut word lists; the package ships none

### [watch]

- `assistants`: other assistants' Discord user ids: in a channel a room watches, their messages go in its day log under their own label once they stop changing, never answered and never an instruction; their files, there or in the room's own channel, are saved in its work/inbox as a person's are

### [memory]

- `root`: the memory tree, in the private folder (default: its memory/)
- `also`: more roots the owner's rooms recall, each in the private folder or absolute (a room's own `also` narrows them)
- `exclude`: paths the index leaves out
- `interval`: the least seconds between the index's passes (default 2)
- `timezone`: the install's IANA zone: the day logs' days, `yerk location day`, a job's default zone (default: the system's)
- `bench`: the recall bench's question file (default evals/recall-questions.json)
- `curated`: the curated files, as the recall reads them (default MEMORY.md)
- `mentions_exclude`: files kept out of mention recall whole
- `embeddings_key`: the provider key file for meaning search (default secrets/openai.key; none there: keywords only)
- `embeddings_model`: the provider model every line, chunk and query is embedded with; a change re-embeds the tree once (each model's vectors are kept apart in the index, so switching back pays nothing); small costs $0.02 and large $0.13 per 1M tokens (OpenAI, read 2026-10-18): a 3,800-file tree's lines (10.4M tokens) $0.21 or $1.35, its chunks (16.7M) $0.33 or $2.17; measured on the 164-question set at 256 kept, large brings paraphrase golds closer but loses on the top six (110 against 117), so small is the default (default text-embedding-3-small; choices: text-embedding-3-small, text-embedding-3-large; takes effect when yerk-index restarts; the backfill then embeds what is missing, in the background)
- `embeddings_dim`: dimensions asked of the provider and stored in the index (2 bytes each a vector): at most 1536 for text-embedding-3-small and 3072 for text-embedding-3-large (OpenAI's embeddings guide, read 2026-10-18); a change re-embeds the tree once; more dimensions buy a finer vector, not a cheaper one (default 256; choices: 256, 512, 1024, 1536, 3072; takes effect when yerk-index restarts; the backfill then embeds what is missing, in the background)
- `embeddings_width`: dimensions kept in memory: the first width of dim, renormalised (the provider's own shortening rule), so a narrower store needs no re-embedding; the index process holds about vectors times (2 times width plus 20) bytes; 192 and 128 were measured and lost (default the dim; choices: 256, 384, 512, the dim; takes effect when yerk-index restarts; the backfill then embeds what is missing, in the background)
- `embeddings_units`: what gets a vector: every line and bullet and every chunk (both, measured best), the lines alone (a smaller store), or the chunks alone (the fewest vectors, a paraphrase then rarely found) (default both; choices: both, lines, chunks; takes effect when yerk-index restarts; the backfill then embeds what is missing, in the background)
- `recall_chars`: characters one recall call hands the room for its first six hits, six hits' worth, their snippets together (each hit at most 750: the lines that carry the match, the cite reads on); each hit past the sixth adds its best line, at most 160; measured on the 164-question set by the recall diet's cold read, 4500 kept 116 of 116 answers found with the key and 84 of 84 keywords only, for about 155 characters a call more than 700 and 4200, still about 21% under whole chunks; less shrinks each of the first six (default 4500; choices: 4500, any number from 100; takes effect when yerkd restarts)

### [location]

- `file`: the phone's fixes, one JSON fix a line (ts_iso, lat, lon, speed_mps), only read
- `places`: the named places, {"places": [{"label", "lat", "lon", "radius_m"}]}, only read
- `stale_after`: no place part when the newest fix is older than this (default 3h)

### [workers]

- `root`: where workers' folders go (default <state>/workers)
- `cap_minutes`: minutes before a worker is stopped (default 65)
- `memory_max`: a worker's memory ceiling, bytes or a whole number with K, M, G or T after it, with no swap past it: the kernel kills the process inside the worker using the most memory and the worker goes on; anything else is not a size and the default is used (default 4G)
- `model`: a worker's model (default: the room's)
- `account`: the account workers run on (default: the first not resting)
- `effort`: a worker's thinking level, one of low, medium, high, xhigh, max (default: the room's live level)
- `tools`: a worker's built-in tools (default Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, TodoWrite)
- `permission_mode`: a worker's --permission-mode (default bypassPermissions)
- `idle_seconds`: seconds a room goes without a human message before a worker's end is told there (default 60)
- `per_account`: workers at once on one account (default 1)
- `yerk_tools`: Yerk AI tools a worker gets on its own server: recall and read_memory only (default none)
- `home`: the owner's home as a worker's unit sees it: open (all of it but the hidden secrets), hidden (empty but its own folder, its account, Yerk's checkout and each plug-in's, the toolchains and the PR gate) or needs (hidden for a need's builder, cold read and fix, open for the rest; default open)

### [gates]

- `approvers`: the senders whose word releases a held action (default owner)
- `word`: the word that does (default approve)
- `approve_minutes`: a word older than this releases nothing (default 30)
- `trash`: the trash command a delete is rewritten into (default: one found on the machine)
- `off`: the built-in outward-action rules turned off, by name
- `external`: [gates.external.NAME] tables, outward-action rules of your own: tool, command and what
- `retired_marks`: marks a reply may not hold in a room without people (an owner room, the machine room), like ["☐", "🟦"]: a final message holding one anywhere in a line outside a fenced block is turned back once at Stop, asked for a plain numbered list instead, and the session start's room pack quotes the room's earlier lines without them, a colour key line left out whole (default none: nothing is turned back or changed; a room with people keeps its own board rule)

### [import]

- `me`: the name your own texts and mail are filed under
- `classes`: a chat id's class or room, "shared" or "room:<a room's id>" (default private)
- `skip`: chat ids a server import leaves out

### [gmail]

- `user`: the Gmail address `yerk import gmail` reads and the `email` tool sends from (none: no room emails)
- `password_file`: a file holding its app password, never printed
- `name`: the name an email goes out under beside the address, as your own Gmail sends mail (default none: the bare address, which `yerk doctor` names each day)
- `smtp_host`: the mail server the `email` tool sends through (default smtp.gmail.com)
- `smtp_port`: that server's port (default 465)

### [imessage]

- `host`: the ssh host of the owner's Mac, the `text` tool's way to it (none, with socket: `ssh` and `imessage_socket` in a mac.json beside an owner room's add-on's server, the hands plug-in's, else no room texts)
- `socket`: the Mac's bridge socket there, serving imsg's JSON-RPC
- `socat`: socat on the Mac (default /opt/homebrew/bin/socat)
- `command`: a whole command whose stdin and stdout are the bridge, in place of host and socket
- `service`: imessage, sms or auto (default auto: iMessage where the person has it, else SMS)
- `send_transport`: imsg's own way to drive Messages, auto, bridge or applescript (default: imsg's)
- `send_timeout`: seconds a text waits on Messages before it may have gone out (default 60)
- `poll_seconds`: seconds between the light checks that copy the owner's newest texts to the box whenever the Mac answers, so the `texts` tool can read them while the Mac sleeps (default 120; 0 turns the copy off and the tool answers only when the Mac is awake)

### [texting]

- `mode`: off, dry or live (default off, as with no table: nothing connects); dry reads the reply list's 1:1 threads on the Mac's bridge, reached as [imessage] reaches it, and does all live does but send: each text that would go waits its time, is checked against the thread again and is written as a job file; live hands it to the Mac's runner over ssh to that Mac, once
- `report`: the owner room (no people) that is told what texting must tell the owner, the missed texts and a card for each text that waits for the owner's word, which is read there; never while dry
- `dry_report`: dry only: an owner room (no people) that is told it in report's place, the cards marked DRY, where send only says it would (default none: nothing is posted while dry)
- `people`: the reply list, a file under the private folder only the owner can read (default texting/people.toml)
- `burst_quiet`: seconds of quiet that close a burst of one person's texts into one decision (default 20)
- `burst_cap`: seconds after a burst's first text that it is closed anyway (default 120)
- `catch_up_hours`: a text older than this when its burst closes is missed: listed once, not answered (default 6)
- `model`: the model that decides what to answer as the owner (default: the report room's model)
- `effort`: its thinking effort (default high)
- `parallel`: decisions that run at once, one person each (default 2)
- `decide_seconds`: seconds one decision may take; past it the text is held with no clean answer (default 240)
- `check_model`: the model of the check, which reads only the thread, the person's sheet, their texts and the draft before a reply or tapback may go (default: the writer's model)
- `check_seconds`: seconds one check may take; past it the draft is held with no clean answer (default 120)
- `fallback`: a second model the writer and the check run on when every account refuses theirs, the row then naming it beside the account (default none: the text is held)
- `outbox`: live only: the Mac runner's outbox, a whole path on the Mac each text's job is written into (default none: live sends nothing)
- `first_shot`: [low, high] seconds a text waits when the owner's own text is their side's newest and younger than first_shot_window (default [150, 240])
- `first_shot_window`: seconds the owner's own text counts as just sent (default 300)
- `send_delay`: [low, high] seconds after the owner's send on a card before its text goes, any hour (default [0, 30])
- `quiet`: [start, end] quiet hours, local HH:MM: a text due inside them to a person whose sheet holds at night waits for their end and a morning draw (default ["23:30", "07:30"])
- `morning`: [low, high] minutes after quiet hours end that such a text goes (default [10, 90])
- `claim_seconds`: seconds after a job is handed in which a text of the owner's side on the thread is taken as that job sent; none by then, unseen and never sent again (default 180)
- `stale_minutes`: minutes past its time a text that could not be handed is dropped and listed once, never sent late (default 30)
- `never_say`: names an assistant goes by on this install: a draft that holds one, as whole words in any case, is held before any model reads it (default none)
- `hold_hours`: hours a held text's card waits for the owner's word; past it the card says expired, not sent, and nothing can send it (default 6)

### [needs]

- `account`: the account `yerk needs build` runs on (default [curate] account, else the workers' pick)
- `cap_minutes`: a builder's minutes before it is stopped (default 60)
- `auto`: the pipeline: a filed need is built, checked, read cold, merged, deployed and live with no person, one at a time, and one filed before it and never built is queued on its next tick (default true; false: `yerk needs build` by hand, as before)
- `fix_cap`: fix workers a need's PR gets for red checks or a cold read's fix first before it is held (default 2)
- `checks_minutes`: minutes a PR's head waits for all its checks before the need is stuck (default 40)
- `contexts`: how many CI checks a head gets before they count as all in (default 7)
- `repo`: the GitHub OWNER/NAME of the pipeline's PRs (default: from the daemon tree's origin)
- `pr_check`: the install's PR checker every need worker runs before its last commit (default: the suite, ruff, mypy)
- `ci_log`: the folder the box's CI keeps its record of each commit it tests in, <full sha>.txt: github_state reads a red head's failing tests and the lines that say what failed there (default none: GitHub's words for each check)
- `sensitive`: what a need's change may not touch and still merge on the cold read alone: a path, a folder/ or plugin:NAME; one touched holds the need for a deeper check, a person merges (default pipeline.SENSITIVE, replaced whole; [] none, the pipeline's road as before)

### [curate]

- `account`: the one account the night's curation runs on
- `logs`: more files in the private folder read as the owner's own stream
- `model`: the model its worker calls run on, the conversion's too (default opus)
- `cap_minutes`: the minutes one worker call may run (default 45)
- `owner_words`: the pronouns the owner's rules use for them, a list: with the owner, owner_names and "in reply to", the words that give a quote of a rule as theirs to the conversion's picker (default ["he", "him", "his"]; rules that say she or they set theirs)

### [docs]

- `fetch`: the doctor's daily fetch of the Claude docs (default true)
- `refresh_days`: days between fetches (default 1)
- `pages`: the pages kept, slug = URL (default config.DOCS_PAGES)
- `topics`: the install's own topics the rule-shape check reads, name = "regex", beside shape.TOPICS
- `acronyms`: words in capitals the rule-shape check and a rule's filing read as names, not shouting

### [deploy]

- `unit`: the systemd unit `yerk deploy` restarts (default: the one the running yerkd is in)
- `tree`: the daemon's checkout (default: the unit's working folder)
- `tests`: the fast tests run before a restart (default test_scrub, test_budget, test_card and test_style)
- `quiet_seconds`: how long every room must have had no message either way, a person's or its own, before the restart (default 240)
- `copy_seconds`: how long the restart waits for a room's copy that is compacting, which a restart drops (default 300; 0 never waits)
- `cap_minutes`: the longest a deploy waits for that quiet (default 60)
- `proof_seconds`: how long the proof may take (default 60)

### [retention]

- `logs`: days the daemon's logs keep (default 14)
- `assembled`: days the rooms' assembled.jsonl keep (default 14)
- `transcripts`: days Claude Code's transcripts keep (default 30)
- `ledger`: days of the ledger's timing rows (default 90)
- `jobs`: days a job run's folder keeps after the run ended; a run still undelivered, or a watch's finding held, keeps it (default 14)
- `workers`: days a finished worker's folder keeps (default 30)
- `pytest`: days a finished worker's pytest temp folders (pytest-of-*) keep; the rest of its folder keeps `workers` days (default 1)
- `built`: days what a finished worker's run built keeps in its folder: each virtual environment (a folder holding pyvenv.cfg) and each folder named __pycache__, .mypy_cache, .ruff_cache, .pytest_cache or node_modules; the rest of its folder keeps `workers` days (default 1)
- `scratch`: days a room's scratch, its work folder but inbox/ and any top folder holding a file named KEEP, keeps (default 2)
- `scratch_mb`: megabytes a room's scratch keeps, its oldest files out first (default 500)
- `trash`: days the trash keeps what left (default 7)
- `scratch_trash`: days the trash keeps a room's scratch (default 1)

### [doctor]

- `growth_mb`: the disk growth a day past which the doctor says so (default 500)

### [limits]

- `uncapped`: true: every character budget in budgets.toml reads as unlimited for what is sent, its number kept as a measurement `yerk map` reports (docs/budgets.md, Uncapped); compact_window and idle_compact_at are not touched by it (default false)

### [backup]

- `path`: the folder `yerk backup` writes to, in the private folder or absolute (default yerk-backups beside the private folder)
- `keep`: how many archives stay, the newest (default 7)
- `units`: the systemd units folder backed up and restored (default ~/.config/systemd/user)

### [models]

- `ALIAS`: one line a model, alias = full id (default: the package's models.toml)

### [presentation]

- `footer`: What the last lines of a worker card show. minimal: how long it ran and the command that opens the report. full: also the account and the model it ran on. (default minimal; choices: minimal, full; read when yerkd starts; restart it to apply a change)
- `paths`: Whether a file path may appear in a room. never: a report shows as 'report saved' with the command that opens it. owner: a worker card in a room whose privacy is owner also shows the report's path. (default never; choices: never, owner; read when yerkd starts; restart it to apply a change)
- `times`: How a card says when. relative: '3 min of 15', 'done in 3 min'. clock: the time of day, 'started 13:05, cap 15 min'. (default relative; choices: relative, clock; read when yerkd starts; restart it to apply a change)

### [jobs]

- `ops_room`: the room a job's failure alert goes to when rooms.toml has no machine room; unset, the job's own room
- `room_notes`: the most notes a room that is not the owner's keeps at once (default 20)
- `room_note_minutes`: such a room adds room_note_burst notes at most in this many minutes (default 5); a refused add does not count
- `room_note_burst`: how many notes such a room adds in one room_note_minutes window (default 3), so a reminder and its follow-up check go in the same turn
- `note_chars`: the most characters a note's message may have, in any room and from `yerk schedule` (default 600); a longer one is refused with the cap, never cut

## jobs.toml

One `[jobs.NAME]` table a scheduled job; `yerk jobs sync` makes its systemd timer.

- `kind`: what runs: "script" (a shell command, no model call, its output posted as is), "agent" (a worker: a headless Claude Code run with the message as its brief, its last message told by the room) or "note" (a reminder: when it is due the room's own session is told its message and says it in its voice; no worker, no shell, no account, no cap, so any room may set one for itself)
- `schedule`: a systemd calendar line (OnCalendar, `man systemd.time`: "Mon 10:00", "Mon..Fri *-*-* 07:30", "*:0/30" every half hour), read in `tz`; a missed run fires once on return. One of schedule or at
- `at`: one date and time ("2026-03-09 09:00", or with an offset), read in `tz` when it has none: a one-shot, removed from this file after it fires; a time missed while the box was down fires once on return; an agent job no daemon took in time is kept in its run's folder (job.json) and fired once at the next start when under a day old and unchanged, else the machine room is told why not
- `tz`: the IANA time zone the schedule or the time is read in (such as Europe/Paris); required with schedule, and with at when the time carries no offset
- `room`: the room the output, the relay or the note lands in (a name from rooms.toml); an agent job's card is there too. A room that is not the owner's adds, lists and removes its own notes and no other job
- `account`: agent jobs: the account (accounts.toml) the worker runs on; unset, the first one not resting
- `model`: agent jobs: the worker's model, an alias or a full id on the install's list (models.toml, or [models] in yerkd.toml); unset, [workers] model, else the room's live one
- `effort`: agent jobs: the worker's thinking level, one of low, medium, high, xhigh, max; unset, [workers] effort, else the room's live level
- `addons`: agent jobs: the room's own add-on tools the worker gets beside the ones the room gives every worker (rooms.toml worker_addons), each a tool as the room's allow names it or a server's name for all of its tools there, such as ["mcp__web__web_fetch"]; one the room has not refuses the job when it is booked
- `cap`: minutes the run may take before it is stopped (default 10 for a script, 15 for an agent); a stopped run alerts
- `deliver`: "announce" (default): the output or the relay goes to the room; "silent": the run and its ledger row happen, nothing is posted, a failure still alerts the ops room; "tell", script jobs only (a watch): output is told to the room's own session, which says it in its own voice as a due note is said, output cut at 4,000 characters with the path of the whole of it; no output tells nothing and costs no turn
- `wakes`: script jobs with deliver = "tell": the most turns the watch wakes its room in any hour, 1 to 60 (default 6); past it each finding waits in its run's folder, and once the hour allows the newest is told as one turn naming how many were held back, so none is lost and none is told twice
- `command`: script jobs: the shell line to run (/bin/sh -c), with an account-free environment, TZ set to the job's zone, YERK_JOB and YERK_JOB_DIR set (a watch's YERK_WATCH_STATE too: a folder of the job's own, 0700, the same every run, where its script keeps what it has seen; Yerk never reads it), YERK_PRIVATE and YERK_STATE as the unit has them and this install's `yerk` first on PATH, so a `yerk` line runs on this install's folders; exit 0 with output posts it (a watch: tells it), exit 0 with nothing posts nothing, anything else alerts
- `message`: agent jobs: the brief the worker gets, written as the whole job; its last message is what the room hears, and exactly NO_REPLY means nothing is posted. Notes: the text the room is told when it is due
