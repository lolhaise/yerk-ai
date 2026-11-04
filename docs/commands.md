# Commands

Written by `yerk docs own` from the parsers in yerk_ai/cli.py, yerk_ai/daemon.py, yerk_ai/memindex.py; change those and run it again, never this page.

The commands pyproject.toml installs, each command and flag with the help its parser gives it.

## yerk

Yerk AI: chat rooms as long-lived Claude Code sessions.

- `--version`: show program's version number and exit

### yerk map ROOM

print a room's context part by part against budgets.toml

- `ROOM`
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--wire`: also the sizes as sent, from the room's session transcript
- `--state STATE`: state folder for --wire and the example layer's rules (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk setup-room ROOM

find or make a Discord room's test webhook (URL to a 0600 file)

- `ROOM`
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE)
- `--channel CHANNEL`: the channel id, when rooms.toml has none yet
- `--webhook WEBHOOK`: default yerk-test-sender

### yerk room ACTION ROOM

act on a running yerkd's room (close, compact), add one (a Discord chat moved in), or print the address of its live page (page)

- `ACTION`: one of close, compact, add, page
- `ROOM`: the room; add: NAME, for a room NAME-on-yerk
- `--from SOURCE`: add: discord:<channel or thread id>, the chat that moves in
- `--person PERSON`: add: a person room, for this Discord user id, behind the wall
- `--voice VOICE`: add: the voice file copied in (default: a one-line placeholder)
- `--since SINCE`: add: the history from this day on, YYYY-MM-DD (default: all of it)
- `--like LIKE`: add: the room it is made like (default: the last of its kind in rooms.toml)
- `--dry-run`: add: read and print; write and send nothing
- `--private PRIVATE`: add: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk import SOURCE

a chat's whole history into the day logs, no room made: discord:<channel or thread id>, or discord-guild:<guild id> for every chat on the server no room covers (--dry-run: the inventory)

- `SOURCE`: discord:<channel or thread id>, discord-guild:<guild id>, imessage:<a Messages chat.db> (texts into imports/texts), gmail (All Mail into imports/mail, by yerkd.toml [gmail]) or openclaw:<an OpenClaw workspace's folder> (its notes copied into openclaw/<its name>, the lines that read them printed)
- `--contacts CONTACTS`: imessage: a Contacts database, for the names its numbers and addresses have
- `--since SINCE`: the history from this day on, YYYY-MM-DD (default: all of it; openclaw: the files changed since)
- `--files`: save attachments up to 25 MB too (default: each named with its size, none downloaded)
- `--dry-run`: read and count, write nothing; on one chat, also print every line it would file
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk accounts [ACTION] [NAME] [MINUTES]

tokens, rests and health per account; cool, clear, token, mint, health

- `[ACTION]`: one of list, cool, clear, token, mint, health; default list
- `[NAME]`: cool, clear, token: the account
- `[MINUTES]`: cool: minutes to rest; default 30.0
- `--kind KIND`: cool: the rest's kind (default manual)
- `--issued ISSUED`: token: the issue date, YYYY-MM-DD (default today)
- `--model MODEL`: health: the model for the one tiny turn; default haiku
- `--names [NAMES ...]`: health: only these accounts
- `--json`
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk status

the daemon, its rooms, each account's 5-hour and weekly meters, and the running workers (from the ledger, checked against the unit list)

- `--full`: the developer detail: pid, unit names, reading ages
- `--json`: one JSON document for a monitoring tool: rooms, accounts, last turn per room, the last day's failures, the index, the backup's age
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk morning

the morning diff (payload changes, yesterday's waits, stalls, shadow flags, memory and CPU, meters) to the room with role = "machine", else a file under the state folder

- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk worker ACTION [ID]

workers: start (needs a running yerkd), list, stop, report

- `ACTION`: one of start, list, stop, report
- `[ID]`: stop, report: the worker's id
- `--brief BRIEF`: start: the brief file
- `--room ROOM`: start: the room for its card and relay (default: the only room, else required)
- `--task TASK`: start: a TASKS.md row (repeat for each); repeat for each
- `--cap CAP`: start: minutes before it is stopped (default [workers], 65)
- `--account ACCOUNT`: start: the account to run on (default [workers], else the first not resting)
- `--model MODEL`: start: the model (default [workers], else the room's)
- `--effort EFFORT`: start: the thinking level, one of low, medium, high, xhigh, max (default [workers], else the room's)
- `--limit LIMIT`: list: how many; default 20
- `--json`
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk pictures ACTION ROOM

backfill: every picture in an owner room's inbox with no NAME.seen.txt gets one, read by Yerk workers on the room's own account, 20 a batch, a line to the machine room per batch (needs a running yerkd)

- `ACTION`: one of backfill
- `ROOM`: the owner room
- `--limit LIMIT`: at most this many pictures, oldest first (default: all)
- `--dry-run`: list each sidecar it would write; start nothing
- `--root ROOT`: a folder the room lists in rooms.toml (read or also): its pictures in place of the inbox's
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk files ACTION ROOM

backfill: every workbook and PDF in an owner room's inbox with no text beside it (NAME.sheet.txt, NAME.pdf.txt), or an older one, gets it now, by code in this process, no model and no yerkd

- `ACTION`: one of backfill
- `ROOM`: the owner room
- `--dry-run`: list each text file it would write; write nothing
- `--root ROOT`: a folder the room lists in rooms.toml (read or also): its files in place of the inbox's
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk needs ACTION [DAY] [N] [PR]

what the rooms could not do and filed with `need`: list (each one's state), build DAY N (a builder worker on [needs] account, its card in the machine room; needs a running yerkd), done DAY N PR

- `ACTION`: one of list, build, done
- `[DAY]`: build, done: the day it was filed (YYYY-MM-DD), as list shows it
- `[N]`: build, done: its number that day
- `[PR]`: done: the pull request that merged it
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk calls ACTION ID SOURCE NOTE

fix ID PATH:LINE NOTE: a call (as the morning diff numbers it) that stood on a wrong line gets the correction filed beside that line the way remember files one, never into a persona or a rule

- `ACTION`: one of fix
- `ID`
- `SOURCE`: the wrong line, as path:line (a cite the diff shows)
- `NOTE`: what is true instead
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk jobs ACTION

scheduled jobs from the private jobs.toml: sync (one systemd user timer and service per job, the diff, then every next fire time read back) or list (the read-back)

- `ACTION`: one of sync, list
- `--json`
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk schedule ACTION [NAME]

add a one-shot or a recurring job, remove one, or list them (the room's `schedule` tool, from the command line)

- `ACTION`: one of add, remove, list
- `[NAME]`: the job's name: lowercase letters, digits, hyphens
- `--when WHEN`: add: a date and time ("2026-03-09 09:00", a one-shot) or a systemd calendar line ("Mon..Fri 09:00", recurring)
- `--what WHAT`: add: the worker's brief (kind agent), the shell line (kind script) or the text the room is told (kind note)
- `--kind KIND`: one of agent, script, note; default agent
- `--tz TZ`: add: an IANA zone (default: the room's timezone, else [memory] timezone)
- `--room ROOM`: add: the room it lands in (default: the only room)
- `--deliver DELIVER`: one of announce, silent; default announce
- `--cap CAP`: add: minutes before the run is stopped
- `--account ACCOUNT`: add: agent jobs, the account to run on
- `--json`
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk recall-bench

the fixed question set against the memory index: top six and top one hit rates (exit 2 when a control misses, 1 failure, 3 timeout, 4 a drop budgets.toml [recall] does not name)

- `--questions QUESTIONS`: the recall kind's question file (default: [memory] bench, else <private>/evals/recall-questions.json); the episodic and self kinds are episodic-questions.json and self-questions.json beside it
- `-k K`: results per question (default 6, the recall tool's default)
- `--json`: the run as JSON, per-question ranks included
- `--controls`: the controls alone, as `yerk deploy` holds them: one JSON line, nothing written (exit 2 when one misses)
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk bench WHAT

latency: the first visible line, the turn, the hooks, Yerk's tools and the cache hit rate per room, from the ledger (read only) and the logs; the table, and <state>/bench/latency-<day>.json to diff

- `WHAT`: one of latency
- `--days DAYS`: the window, days back from now (default 7)
- `--out OUT`: the file written instead of <state>/bench/latency-<day>.json
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk eval WHAT

taste: the taste judge replayed over the owner's complaints and the replies they accepted, with what each turn read; precision, recall, false alarms per verdict, phase 1 beside (the bar printed, not enforced)

- `WHAT`: one of taste
- `--cases CASES`: the complaints (default: <private>/evals/taste/cases.jsonl)
- `--controls CONTROLS`: the accepted replies (default: <private>/evals/taste/controls.jsonl)
- `--rubric RUBRIC`: the judge's system prompt (default: <private>/evals/taste/rubric.md, else the package's)
- `--ledger LEDGER`: where each turn's reads come from, read only (default: <state>/ledger.sqlite)
- `--out OUT`: every row and the numbers as JSON (default: scores-taste.json beside the cases)
- `--model MODEL`: the judge's model (default: the claude binary's own)
- `--effort EFFORT`: the judge's effort (default: medium)
- `--jobs JOBS`: judge calls in flight at once; default 4
- `--cap CAP`: seconds a judge call may take (default 20)
- `--sample SAMPLE`: a stratified N cases (by verdict) and N controls (by room), not all
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk abilities

every ability and its state on this install, one line each

- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk add SOURCE

an add-on into this install (a folder, a git URL, or an ability whose entry names its repo): fetched, checked against its own size, its package, server, tables and units in, its tests run

- `SOURCE`
- `--dry-run`: say each step it would take, change nothing
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk remove NAME

an installed add-on out again, each step of yerk add undone

- `NAME`
- `--dry-run`: say each step it would take, change nothing
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk explain ABILITY

one ability (what it does, needs and costs, its free path, its state here) or one setting

- `ABILITY`
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk location ACTION [DATE]

day: the day's places (yerkd.toml [location]) as one line in its day log, place by place with the first and last fix at each, an earlier run's line replaced; the nightly script job

- `ACTION`: one of day
- `[DATE]`: the day, YYYY-MM-DD (default: today in [memory] timezone)
- `--dry-run`: print the line, write nothing
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk retention

the daily sweep: logs, transcripts, timing rows, finished workers and rooms' scratch past their days ([retention] in yerkd.toml) into the trash, never a work folder holding a file named KEEP, each swept room told in its work/RETENTION.md, and the trash past its days deleted

- `--dry-run`: say what would move and how much, move nothing
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk backup

one dated archive with a manifest: the private folder, the state's ledger, sessions and index metadata, the units; into [backup] path (yerkd.toml), the last [backup] keep (7) kept

- `--to TO`: the folder for it (default: [backup] path, else yerk-backups beside the private folder)
- `--with-index`: the index's vector store too (else yerk-index rebuilds it)
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk restore ARCHIVE

an archive back, read whole against its manifest first: into an empty folder, or over the layer with --force after a backup of it

- `ARCHIVE`
- `--to TO`: an empty folder to restore into, as DIR/private, DIR/state and DIR/units
- `--dry-run`: verify and say each step, write nothing
- `--force`: over the layer after a backup of it, and under a running yerkd
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk doctor

the daily check (the private folder's layout, rooms.toml keys, memory classes, tokens, the index, hooks, disk growth, the rule files' shape) and the crash repair of NUL lines, each file backed up first; once a week it also fetches the Claude docs from the web ([docs] fetch = false in yerkd.toml turns that off)

- `--dry-run`: say what would be mended or scrubbed, change nothing
- `--scrub`: only the scrub: each secret-shaped line's value swapped for [scrubbed], every line kept, each file copied first into <private>/backups/scrub-<day>/
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk docs ACTION [NAME]

the Claude docs kept as a memory root every room recalls (refresh, list, show NAME), shape: every room's rule files scored against the prompting guide, and own: Yerk AI's own docs written into docs/ from the code

- `ACTION`: one of refresh, list, show, shape, own
- `[NAME]`: show: the page's slug; shape: one room (default: every room)
- `--rules`: shape: print each count's exact rule
- `--dry-run`: own: name each page that differs from the code, write nothing
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk curate

the night's card curation: the sweep (repeats, superseded and past-date lines, cite drift), then the day's facts proposed, knocked down, checked and landed only if the bench holds

- `--since SINCE`: each day from this date (YYYY-MM-DD) to yesterday, for a backfill
- `--dry-run`: say what it would change and propose, change nothing
- `--rules`: the rule files alone: merges, cuts, condensations and promotions proposed into <memory>/rules-proposals/<day>.md, never applied
- `--apply DAY N`: the owner's word: proposals N[,N...] of DAY written, backed up and fitted to each file's line, or `undo`: the last apply put back
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk config ACTION [KEY] [NAME]

the settings Yerk AI describes, with their values here and plain words

- `ACTION`: one of get
- `[KEY]`: a setting or section (presentation, memory, room); all when left out
- `[NAME]`: room: the room's name
- `--json`
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: room: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

### yerk deploy [REF]

a commit onto the running yerkd: fast-forward only, fast tests, a quiet room, the restart, the proof card to the machine room, else the rollback

- `[REF]`: the commit (default: the daemon's tree's HEAD)
- `--no-tests`: skip the fast tests and the recall check (the card says so)
- `--unit`: also move a systemd-run daemon onto units/yerkd.service
- `--timers`: only install and turn on each timer in units/ not installed yet
- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the running yerkd's own, from its unit)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)

## yerkd

The Yerk AI daemon.

- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)
- `--port PORT`: local port for /mcp and /hook (default: yerkd.toml, else free)
- `--idle-minutes IDLE_MINUTES`: close a room's process after this long idle; default 30.0
- `--config-dir CONFIG_DIR`: use only this account's Claude Code config folder (the contract probes' smoke)

## yerk-index

The memory index: FTS5 over the private memory tree, updated per changed chunk, in its own process.

- `--private PRIVATE`: private folder (default: $YERK_PRIVATE, else the fake example layer)
- `--state STATE`: state folder (default: $YERK_STATE, else ~/.local/state/yerk)
- `--interval INTERVAL`: the least seconds between passes (default: [memory] interval, else 2)
- `--once`: one pass, then exit
- `--rebuild`: drop the index and build it again (the only full reindex)
- `--check`: compare the index with the disk; exit 0 fresh or pending, 1 stale or scope changed, 2 failure, 3 timeout
- `--deadline DEADLINE`: --check: seconds before it reads as a timeout; default 10.0
