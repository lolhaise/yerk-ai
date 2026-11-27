# lifted

Code taken from other projects and adapted here. Each file names its source and license at its top, and [NOTICE](../../NOTICE), at the root of the repository, lists them all; every source is under the MIT License.

- `__init__.py`: marks the folder as a Python package.

## Failed turns and accounts
- `classify.py`: sorts why a turn failed, by the table in failures.toml, into what happens next: run it again, compact and run again, move to the next account, start fresh, or stop.
- `cooldowns.py`: which Claude accounts are resting after a failure, and until when.
- `expiry.py`: whether an account's login token is dead, due for renewal, or fine.

## What a room is shown
- `clock.py`: the clock line every turn carries (the local time, and how long since a person last wrote), and the text of a room's voice file.
- `sizemap.py`: names and measures the parts of a block of text without printing it, for `yerk map`.
- `trim.py`: cuts a text that is over its size limit, keeping its start and end, with a marker that says what was cut.

## Commands
- `hardline.py`: the commands that never run, whatever anyone approves: deleting the root, home or a system folder, formatting or overwriting a disk, a fork bomb, killing every process, a shutdown.
- `killtree.py`: stops a command and every process it started.
- `shellguard.py`: reads a shell command the way the shell does, for the checks on each command a room runs, and turns a plain delete into a move to the trash.

## Messages and the service
- `loopguard.py`: caps how many machine-sent messages a room takes in a time window, so a runaway script cannot flood it.
- `split.py`: splits a long reply into pieces under Discord's limit, never leaving a code block open.
- `systemd_notify.py`: tells systemd that yerkd has started and is still alive, when systemd asks for that.
