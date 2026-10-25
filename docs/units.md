# Units

Written by `yerk docs own` from units/; change those and run it again, never this page.

The systemd user units Yerk AI ships, each service with when it runs, then each file line by line; an Environment line shows its variable's name only. Copy a unit to ~/.config/systemd/user/ to install it.

- yerk-backup.service: yerk-backup.timer, OnCalendar=*-*-* 04:10:00
- yerk-curate.service: yerk-curate.timer, OnCalendar=*-*-* 05:00:00
- yerk-doctor.service: yerk-doctor.timer, OnCalendar=*-*-* 04:40:00
- yerk-health.service: yerk-health.timer, OnCalendar=*-*-* 07:10:00
- yerk-index.service: no timer, it runs while it is enabled
- yerk-morning.service: yerk-morning.timer, OnCalendar=*-*-* 06:30:00
- yerk-recall-bench.service: yerk-recall-bench.timer, OnCalendar=Sun 06:00
- yerk-retention.service: yerk-retention.timer, OnCalendar=*-*-* 04:20:00
- yerkd.service: no timer, it runs while it is enabled

## yerk-backup.service

The nightly backup (yerk backup): the private folder, the state's ledger, sessions and index metadata, and these units, as one dated archive with a manifest under [backup] path in yerkd.toml (else yerk-backups beside the private folder), 0600; the last [backup] keep (7) stay. One line a run; a failure exits 1 and fails the unit, and `yerk status` and the doctor say a backup older than 36 h. Installed with yerk-backup.timer: copy both to ~/.config/systemd/user/ and `systemctl --user enable --now yerk-backup.timer`.

[Unit]

- Description: `Yerk AI backup`
- Before: `yerk-retention.service`

[Service]

- Type: `oneshot`
- Environment: YERK_PRIVATE
- Environment: YERK_STATE
- ExecStart: `%h/yerk-ai/.venv/bin/yerk backup`
- Nice: `10`
- IOSchedulingClass: `idle`
- TimeoutStartSec: `1800`

## yerk-backup.timer

Every day at 04:10 local time, after the rooms' processes have gone idle and before retention's 04:20 sweep moves anything into the trash; a night the box was off runs once when it is back.

[Unit]

- Description: `Yerk AI backup at 04:10`

[Timer]

- OnCalendar: `*-*-* 04:10:00`
- Persistent: `true`

[Install]

- WantedBy: `timers.target`

## yerk-curate.service

The night's card curation (yerk curate): the sweep of the fact cards (a line said twice, a superseded line, a line `until` a date now past, each blanked so cites into a card hold; a cite moved only when its quoted words sit on one line of its file, every other drift a finding), then yesterday's facts: a pack per card, a worker proposes, a second knocks down by yerk_ai/curate.md, code checks, and the lines land with a backup only when the recall bench does not fall. Corrections are only ever proposed. A finding exits 1, which is not a unit failure (the morning diff says it); a run that could not run exits 2 and fails the unit. Installed with yerk-curate.timer: copy both to ~/.config/systemd/user/ and `systemctl --user enable --now yerk-curate.timer`.

[Unit]

- Description: `Yerk AI curate`
- After: `yerk-doctor.service`

[Service]

- Type: `oneshot`
- Environment: YERK_PRIVATE
- Environment: YERK_STATE
- ExecStart: `%h/yerk-ai/.venv/bin/yerk curate`
- SuccessExitStatus: `1`
- TimeoutStartSec: `7200`

## yerk-curate.timer

Every day at 05:00 local time, after the doctor's 04:40 check and before the 06:30 morning diff that carries its line; a night the box was off runs once when it is back, after the doctor's own catch-up run.

[Unit]

- Description: `Yerk AI curate at 05:00`

[Timer]

- OnCalendar: `*-*-* 05:00:00`
- Persistent: `true`

[Install]

- WantedBy: `timers.target`

## yerk-doctor.service

The daily check (yerk doctor): the private folder's shape, rooms.toml keys against the loaders, memory classes and day-log lines, token expiry, the index, each running room's hooks, disk growth past [doctor] growth_mb in yerkd.toml (500 MB a day), and the crash repair of NUL lines in day logs, assembled logs and bound transcripts, each file backed up first. Prints nothing when all is well; a finding exits 1, which is not a unit failure (the morning diff says it); a run that could not run exits 2 and fails the unit. Installed with yerk-doctor.timer: copy both to ~/.config/systemd/user/ and `systemctl --user enable --now yerk-doctor.timer`.

[Unit]

- Description: `Yerk AI doctor`
- After: `yerk-retention.service`

[Service]

- Type: `oneshot`
- Environment: YERK_PRIVATE
- Environment: YERK_STATE
- ExecStart: `%h/yerk-ai/.venv/bin/yerk doctor`
- SuccessExitStatus: `1`
- TimeoutStartSec: `600`

## yerk-doctor.timer

Every day at 04:40 local time, after retention's 04:20 sweep and before the 06:30 morning diff that carries its line; a night the box was off runs once when it is back, after retention's own catch-up run.

[Unit]

- Description: `Yerk AI doctor at 04:40`

[Timer]

- OnCalendar: `*-*-* 04:40:00`
- Persistent: `true`

[Install]

- WantedBy: `timers.target`

## yerk-health.service

The daily health call: one tiny model turn per account, the login's identity, a ledger row each (yerk accounts health). Installed by copying both files to ~/.config/systemd/user/ and `systemctl --user enable --now yerk-health.timer`. Set YERK_PRIVATE and YERK_STATE below.

[Unit]

- Description: `Yerk AI daily health call per account`

[Service]

- Type: `oneshot`
- Environment: YERK_PRIVATE
- Environment: YERK_STATE
- ExecStart: `%h/yerk-ai/.venv/bin/yerk accounts health`
- TimeoutStartSec: `900`

## yerk-health.timer

[Unit]

- Description: `Yerk AI daily health call per account (07:10, and at boot when a day was missed)`

[Timer]

- OnCalendar: `*-*-* 07:10:00`
- Persistent: `true`
- RandomizedDelaySec: `120`

[Install]

- WantedBy: `timers.target`

## yerk-index.service

yerk-index: the memory index in its own process (DESIGN 5). Installed by copying this file to ~/.config/systemd/user/ and `systemctl --user enable --now yerk-index.service`. It reads [memory] in the private yerkd.toml for its roots; the index file is <state>/memory/index.sqlite. Low CPU and idle IO priority so index work never competes with the daemon serving rooms.  Memory is mostly the vectors meaning search keeps here: one float16 matrix grown in blocks of 16,384 vectors, 8.3M a block at 256 dimensions (16,384 x (2 x 256 + 20 hash) bytes = 8,716,288). On 2026-10-21, after five new roots, it held 862,135 vectors in 53 blocks, 441M; beside them 109M stayed resident and 86M sat cold in swap (the interpreter, numpy, SQLite, load leftovers; MALLOC_ARENA_MAX=2 keeps them to two glibc arenas), and the kernel held 9M. A 450M cap under that kept the unit reclaiming and swapping (600M in swap; meaning search took 3.5 to 8.2 s, past the recall's 5 s wait); 600M served warm searches but the first after each backfill scan (300M of file pages) took 3.5 to 4.7 s; 1G never reclaimed. The rule: MemoryHigh = 256M + 8.3M per block of 16,384 vectors, the 256M being the 109M, the 86M (a new process holds it resident until reclaimed), the 9M, 25M of file pages and 25M for searches at once (embeddings.HIGH_BASE); `yerk doctor` says when the vector count passes it. Since recall-warm (2026-10-22) the rule adds the keyword tables' pages this process keeps mapped against page-cache drops (pin.bytes in index.json, 394M at 314,010 chunks); the doctor counts them too. 1400M covers 65 blocks (1,064,960 vectors) and 600M of those pages; past MemoryHigh the kernel reclaims and slows the unit, and kills nothing.

[Unit]

- Description: `Yerk AI memory index (FTS5 over the private memory tree)`

[Service]

- Type: `simple`
- Environment: YERK_PRIVATE
- Environment: YERK_STATE
- Environment: PYTHONUNBUFFERED
- Environment: MALLOC_ARENA_MAX
- WorkingDirectory: `%h/yerk-ai`
- ExecStart: `%h/yerk-ai/.venv/bin/python -m yerk_ai.memindex`
- Restart: `on-failure`
- RestartSec: `10s`
- Nice: `10`
- IOSchedulingClass: `idle`
- MemoryHigh: `1400M`

A stop ends the loop after the pass that runs (SIGTERM is handled); a pass that is a full rebuild can run past the 90 s default, and the WAL keeps the index consistent under the SIGKILL that follows. The daemon's recall reads the index file itself and treats a missing meaning search as keywords only for that query, so a restart here costs the vectors and the pinned pages until the first pass, never an answer. No setuid gain and a /tmp of its own (proven on a scratch install under systemd-run).

- NoNewPrivileges: `yes`
- PrivateTmp: `yes`

[Install]

- WantedBy: `default.target`

## yerk-morning.service

The morning diff (yerk morning): payload changes per room, yesterday's waits, stalls and shadow flags, memory and CPU, the meters; posted to the room with role = "machine" in rooms.toml, else written under the state folder. Installed with yerk-morning.timer: copy both to ~/.config/systemd/user/ and `systemctl --user enable --now yerk-morning.timer`.

[Unit]

- Description: `Yerk AI morning diff`

[Service]

- Type: `oneshot`
- Environment: YERK_PRIVATE
- Environment: YERK_STATE
- ExecStart: `%h/yerk-ai/.venv/bin/yerk morning`
- TimeoutStartSec: `300`

## yerk-morning.timer

Every day at 06:30 local time; a morning the box was off runs once when it is back.

[Unit]

- Description: `Yerk AI morning diff at 06:30`

[Timer]

- OnCalendar: `*-*-* 06:30:00`
- Persistent: `true`

[Install]

- WantedBy: `timers.target`

## yerk-recall-bench.service

The weekly retrieval measurement (L63): the fixed question set against the memory index; one JSON line per run in <state>/memory/recall-bench.jsonl. Installed with yerk-recall-bench.timer: copy both to ~/.config/systemd/user/ and `systemctl --user enable --now yerk-recall-bench.timer`.

[Unit]

- Description: `Yerk AI weekly recall bench`

[Service]

- Type: `oneshot`
- Environment: YERK_PRIVATE
- Environment: YERK_STATE
- ExecStart: `%h/yerk-ai/.venv/bin/yerk recall-bench`
- SuccessExitStatus: `2`
- TimeoutStartSec: `600`

## yerk-recall-bench.timer

[Unit]

- Description: `Yerk AI weekly recall bench`

[Timer]

- OnCalendar: `Sun 06:00`
- Persistent: `true`

[Install]

- WantedBy: `timers.target`

## yerk-retention.service

The retention sweep (yerk retention): stream and assembled logs gzipped by day, old transcripts, timing rows, finished workers' folders and rooms' scratch past their days ([retention] in yerkd.toml), and a room's scratch past 500 MB oldest first, moved to the state folder's trash, the trash past 7 days deleted (a room's scratch past 1). Prints nothing when nothing moved; a failure exits non-zero and opens the next morning diff. Installed with yerk-retention.timer: copy both to ~/.config/systemd/user/ and `systemctl --user enable --now yerk-retention.timer`.

[Unit]

- Description: `Yerk AI retention sweep`

[Service]

- Type: `oneshot`
- Environment: YERK_PRIVATE
- Environment: YERK_STATE
- ExecStart: `%h/yerk-ai/.venv/bin/yerk retention`
- TimeoutStartSec: `900`

## yerk-retention.timer

Every day at 04:20 local time, after the rooms' processes have gone idle and before the 06:30 morning diff that says what it moved; a night the box was off runs once when it is back.

[Unit]

- Description: `Yerk AI retention sweep at 04:20`

[Timer]

- OnCalendar: `*-*-* 04:20:00`
- Persistent: `true`

[Install]

- WantedBy: `timers.target`

## yerkd.service

The daemon: every room, its hooks and tools, under systemd. Installed by copying it to ~/.config/systemd/user/ and `systemctl --user enable --now yerkd.service`. Set YERK_PRIVATE and YERK_STATE below.  Type=notify: yerkd says READY=1 once its event loop runs, so a start counts only when it can answer. WatchdogSec=30: yerkd beats the watchdog only while its event loop makes progress (a mark set every second, yerk_ai/beat.py); 30 s with no beat and systemd stops it with SIGABRT and Restart=on-failure starts it again. Without them a frozen yerkd stays "active" while no room answers (freezes of up to 61 s went unseen that way), and nothing but a person notices.  Memory covers yerkd and the room processes it starts (workers are units of their own). yerkd idles at 89 MB (/status, 2026-10-18); each open room's Claude Code read 201 to 307 MB here (2026-10-18, before its MCP child), so a room is sized at 350M and yerkd at 100M. MemoryHigh=2G is five rooms open at once: past it the kernel reclaims and slows the unit, and kills nothing. MemoryMax=4G is the hard ceiling for a leak only: there the OOM killer takes the largest process, a room's Claude Code, mid-turn, so it sits far above what the rooms need. For each room kept open past five, raise MemoryHigh by 350M and keep MemoryMax at twice it.  `yerk deploy --unit` moves a daemon started by systemd-run onto this file under its own name, with its command, folder and environment; `yerk deploy` restarts it after that (docs/GUIDE.md, Install as a service).

[Unit]

- Description: `Yerk AI daemon (yerkd)`
- After: `network-online.target`

[Service]

- Type: `notify`
- NotifyAccess: `main`
- WatchdogSec: `30`
- Restart: `on-failure`
- RestartSec: `5`
- TimeoutStartSec: `120`

A stop: yerkd gets SIGTERM alone (KillMode=mixed), says STOPPING=1, ends each room's Claude Code itself (a turn mid-run is cut and its messages run again once at the next start, marked), closes the gateway clean and exits 0; anything still alive when it has exited gets SIGKILL. Measured 1.3 s with a turn running (signal drill); 30 s is the ceiling before systemd's SIGKILL. NoNewPrivileges and PrivateTmp: no setuid gain for any room's shell, a /tmp of its own (Claude Code's sandbox, bwrap, needs neither; proven on a scratch install under systemd-run).

- KillMode: `mixed`
- TimeoutStopSec: `30`
- NoNewPrivileges: `yes`
- PrivateTmp: `yes`
- MemoryHigh: `2G`
- MemoryMax: `4G`
- Environment: YERK_PRIVATE
- Environment: YERK_STATE
- ExecStart: `%h/yerk-ai/.venv/bin/yerkd`

[Install]

- WantedBy: `default.target`
