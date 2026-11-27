# units

The files that tell systemd, the part of Linux that starts services and runs them on timers, how to run Yerk AI in the background: yerkd itself, the memory search index, and the jobs that run on a timer. A `.service` file says what runs and the `.timer` of the same name says when; docs/units.md shows every file in full.

## Always running

- `yerk-index.service`: yerk-index, the memory search index, as a low-priority process of its own.
- `yerkd.service`: yerkd, the program that serves every room; it restarts if it fails.

## Every night

- `yerk-backup.service`: `yerk backup`, one dated archive of the private folder, the important state and the units.
- `yerk-backup.timer`: starts the backup every day at 04:10.
- `yerk-curate.service`: `yerk curate`, the cleanup of the memory's fact files and the filing of yesterday's facts.
- `yerk-curate.timer`: starts the curation every day at 05:00.
- `yerk-doctor.service`: `yerk doctor`, the daily check of the install's folders, settings, memory, tokens and index.
- `yerk-doctor.timer`: starts the check every day at 04:40.
- `yerk-retention.service`: `yerk retention`, the sweep that moves old logs and scratch files to the trash.
- `yerk-retention.timer`: starts the sweep every day at 04:20.

## Every morning

- `yerk-health.service`: `yerk accounts health`, one tiny model call per account, to prove each login still works.
- `yerk-health.timer`: starts the health call every day at 07:10, or at boot after a missed day.
- `yerk-morning.service`: `yerk morning`, the summary of what changed, posted to the room marked `role = "machine"`.
- `yerk-morning.timer`: starts the summary every day at 06:30.

## Every week

- `yerk-recall-bench.service`: `yerk recall-bench`, a test of how well memory search finds known answers.
- `yerk-recall-bench.timer`: starts the recall bench every Sunday at 06:00.
