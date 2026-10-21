# Changelog

One entry per merge to main, newest first: the date, the pull request, and what a person running Yerk AI notices.
A line that changes a file the install keeps (rooms.toml, yerkd.toml, budgets.toml, a unit) says so under
**Upgrading**, with what to do. The ledger migrates itself at the next start (its schema is versioned and a ledger
newer than the code is refused with one line, never changed); the day logs are rewritten once at the daemon's start
with the originals kept under `backups/`; the index chunks every file again when its chunker's version changes.

## 2026-10-21

### harness-expert (#69)

- `yerk docs refresh | list | show NAME | shape [ROOM] [--rules]`: the Claude docs Yerk runs on (the prompting guide,
  each model's prompting page, the Claude Code pages on settings, hooks, memory, sandboxing, MCP, the CLI and the
  system prompt) are fetched as Markdown into `<state>/docs/claude/` and kept as a memory root of class shared, so a
  room's own recall and read_memory reach them; they never ride in what a room is handed per message, and in a room's
  own recall the owner's notes rank above them.
- The doctor refreshes the pages once a week (`[docs] fetch = false` turns that off; `yerk docs refresh` still
  fetches), and the morning diff says `docs changed: <slug>` once per change.
- The doctor scores every rule file a room loads against the prompting guide (no why, loud, untagged examples, alarm
  marks, a topic said twice): a report per room in `docs/shape/<room>.md`, one totals line in the morning diff, and a
  finding for a file past a budgets.toml `[shape]` threshold. mechanics.md and examples/private are rewritten in the
  guide's shape.
- `yerk explain` takes a command's name (`yerk explain docs`), and the built abilities `claude-docs` and `rule-shape`
  answer a room asked whether its rules are written the docs' way.
- **Upgrading:** yerkd.toml takes an optional `[docs]` table (`pages`, `refresh_days` 7, `fetch` true, `topics`,
  `acronyms`); budgets.toml gains `[shape]` (every threshold 0) and [code] lines rises by 452. The memory index takes
  `<state>/docs` as a new root at its next start and chunks every file again once; run `yerk docs refresh` once after
  the deploy so the pages are there before the first weekly run.

### budgets-voice (#68)

- The taste rules (one line each: a rule and the rated replies that prove it) fit a rulebook: its budget rises from
  7,000 to 10,850 characters and a room voice's from 5,000 to 5,500; the session-start total rises by the same 4,350,
  to 84,350.
- **Upgrading:** budgets.toml changed (rulebook, room_voice, total). Nothing to do.

### hardening (#66)

- A room's token reaches its hooks through a 0600 file beside its settings, never on a command line (which every
  local user can read in /proc). The state folder is made 0700, and an existing one is closed to 0700 at the next
  start.
- No room may read or send a secret (both secrets folders, every room's token files, each account's token file and
  its config folder's login, the secret files yerkd.toml names): Read deny rules in every room's settings.
- A room whose tools run a shell (Bash, Monitor, PowerShell or `default`) gets Claude Code's sandbox when rooms.toml
  gives it no `[rooms.NAME.sandbox]` table: unsandboxed, a script, `grep` or a redirect read every secret.
- Recalled and imported lines, and a person's words, reach a room marked as records, never instructions.
- `yerk doctor` names any file or folder another user can read in the private or state folder, and any
  secret-shaped line in memory, attachments or imports by file and line, never by value; a one-time code in an
  import two days past its own date is dead and not said.
- Tests: `YERK_DENYLIST_FILE` names a file of words no tracked file may hold.

**Upgrading**: a room whose tools run a shell and that has no `[rooms.NAME.sandbox]` table now runs sandboxed (on
Linux Claude Code's sandbox needs bubblewrap and socat); `[rooms.NAME.sandbox]` with `enabled = false` keeps its
shell on the host. `budgets.toml` `[code] lines` moved to the new count.

### reliability (#65)

- A stop (`systemctl stop`, `yerk deploy`) ends the daemon clean: STOPPING=1 to systemd, every room's Claude Code
  ended by the daemon, the gateway closed, exit 0, about a second. A turn cut mid-run has its messages run again
  once at the next start, marked, as a crash already had.
- A gateway session Discord let go (an outage past a few minutes) no longer loses the messages sent meanwhile: every
  ready reads each room's channel since the last ledgered message, not only the first ready of the process.
- A message the ledger could not take (the disk full) is read again from the channel a minute later, and again
  until it is taken, instead of waiting for the next restart.
- A stream log that cannot be written (the disk full, a file-size limit) is dropped for that process; the turn
  runs. A turn the harness itself crashes is said in the room ("the harness failed; it is in the log") and
  its messages closed, instead of a card left at "working on it".
- A ledger from a newer version is refused with one line and left as it is. A second yerkd on the same state
  folder is refused with one line.
- `yerk status` names the version and the commit that runs; `yerk status --json` prints one document for a
  monitoring tool.
- CI: `ruff check .` (pyflakes and the syntax-level pycodestyle rules, the config in pyproject.toml), pip-audit
  over uv.lock (fails on a known vulnerability; certifi moved to 2026.10.19, the first run's one finding), and
  property tests with hypothesis for the day-log line grammar.

**Upgrading**: copy `units/yerkd.service` and `units/yerk-index.service` again (KillMode, TimeoutStopSec,
NoNewPrivileges, PrivateTmp) and `systemctl --user daemon-reload`; `budgets.toml` `[code] lines` moved to the new
count; the dev group gained hypothesis, ruff and pip-audit (`pip install -e . pytest mypy hypothesis ruff pip-audit`).

### curate (#63)

- `yerk curate`, nightly at 05:00 after the doctor: a sweep in code blanks a card line said twice, a superseded line
  and a line past its `[until YYYY-MM-DD]`, and moves a cite only when its quoted words sit on exactly one line; any
  other drift is a finding in the morning diff, each card backed up first. Then a worker proposes lines from the
  day's logs for each card, a second knocks them down, code checks each one, and they land only when the recall
  bench does not fall. A correction is only proposed, never applied. `--dry-run` shows the night, `--since DATE`
  runs a range as a backfill.

**Upgrading**: copy `units/yerk-curate.service` and `units/yerk-curate.timer` and enable the timer; `[curate]
account` in yerkd.toml pins the account it runs on, and `[curate] logs` adds logs of the owner's own.

### compact-window (#64)

- A room compacts where Yerk AI says, never at the model's window edge: `budgets.toml` `[session] compact_window`
  (250,000 tokens) is written as every room's `autoCompactWindow`; a room's own `compact_window` in rooms.toml
  overrides it (kept between 100,000 and 1,000,000), and 0 leaves it to the model.

**Upgrading**: `budgets.toml` gained `[session] compact_window`; each room takes it at its next start.

### import-sources (#62)

- `yerk import imessage:<chat.db> [--contacts <db>]` and `yerk import gmail`: a person's texts and mail come in as
  files, `imports/texts/<person or group>.md` and `imports/mail/<YYYY-MM>.md`, one line a message, both sources
  only read. Anything shaped like a key, a token, a password or a one-time code is written as its kind only.

**Upgrading**: for mail, `[gmail] user` and `password_file` (an app password) in yerkd.toml; `[import] me` names
your own texts.

### rail-2 (#59)

- Recall per message reads up to 2,000 characters when it finds something (`[per_message.on_hit]`, the cold read's
  measurement with the index healthy); the recall hook stays inside its 10 s; a keyword search that did not finish no
  longer says nothing was found, and when the best 8 chunks hold nothing, 20 are read once before it says so.

### keys (#60), index-health (#61)

- keys: the one-time codes and the private layer's key files (see the pull request).
- index-health: yerk-index's memory ceiling moved to 800M with a rule (256M plus 8.3M per block of 16,384 vectors;
  `yerk doctor` says when the vector count passes it), the doctor reads the index.

**Upgrading**: copy `units/yerk-index.service` again (MemoryHigh=800M) and `systemctl --user daemon-reload`.

### import-discord (#58), doctor (#55)

- `yerk import discord:<channel or thread id>`: a chat's whole history into the day logs, no room made; a chat
  imported before its room never reaches `yerk room add`.
- `yerk doctor`, daily at 04:40: the private folder's shape, rooms.toml keys against the loaders, memory classes
  and day-log lines, token expiry, the index, each running room's hooks, disk growth, and the crash repair of NUL
  lines in day logs, assembled logs and bound transcripts (each file backed up first). A finding exits 1 and the
  morning diff says it; yerkd's start runs the repair before anything reads a log.

**Upgrading**: copy `units/yerk-doctor.service` and `units/yerk-doctor.timer` and enable the timer.

## 2026-10-20

- name-keys (#54), shrink-imessage (#57), stance (#52), web-research (#53), owner-cmds (#51), nothing-dropped (#50),
  land3 (#49), gates (#47), silence-cap (#48), people-recall (#46), limit-rest (#45), room-account (#44),
  room-people (#43): see each pull request; this file starts with the merges of 2026-10-21.
