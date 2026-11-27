# tests

The automated tests for Yerk AI. They run the program's pieces with stand-ins in place of Claude Code, Discord, systemd and GitHub, so they need no login; the one exception, `test_live.py`, is skipped unless `YERK_LIVE=1` and `YERK_LIVE_CONFIG_DIR` are set.

## Run them

From the top folder of the repository, after `uv sync`:

```sh
uv run pytest                        # every test
uv run pytest tests/test_backup.py   # one file
```

## How the files are named

pytest runs every `test_*.py` file. Each is named after what it checks: a module (`test_backup.py` checks `yerk_ai/backup.py`), a tool a room is handed (`test_emails.py`) or a behaviour (`test_fold.py`). Files on one subject share a start, as `test_recall_*.py`, `test_texting_*.py` and `test_evidence_*.py` do.

## The groups

- **Rooms and Discord**: how a message becomes a turn and a reply, with its progress card. For example `test_room.py`, `test_discord.py`, `test_fold.py`.
- **Memory**: the day logs, the search index, recall, the cards and the rules files. For example `test_memory.py`, `test_recall.py`, `test_curate.py`.
- **Tools a room is handed**: email, texts, deletes, files, pictures and voice notes. For example `test_emails.py`, `test_deletes.py`, `test_texting_sender.py`.
- **Gates, walls and checks**: what blocks or holds a risky command, what each room may read, and the checks run on a reply. For example `test_gates.py`, `test_person_wall.py`, `test_evidence_sheet.py`.
- **Workers, jobs and upkeep**: background workers, scheduled jobs, the pipeline that builds a missing ability, deploys, backups and the daily check. For example `test_workers.py`, `test_needs_pipeline.py`, `test_deploy.py`.
- **Guards on the repository itself**: sizes against `budgets.toml` and the ceiling on the code, docs written from the code, no personal data or secrets, and the wording rules in `STYLE.md`. For example `test_budget.py`, `test_owndocs.py`, `test_scrub.py`.

## The other files

- `conftest.py`: setup pytest applies to every test, so that none touches the real machine's timers, network address, screen or sandbox.
- `contract/`: the contract probes in `probes.py`, short runs of the real Claude Code that you start by hand before an upgrade, to see it still behaves as Yerk AI expects. pytest does not collect them; `test_contract.py` runs one against the fake.
- `fake_channel.py`, `fake_claude.py`, `fake_worker_claude.py`: stand-ins for a chat channel, for Claude Code in a room and for Claude Code in a worker.
- `fixtures/`: the data the tests read, so it has no README of its own: Claude Code event streams that `fake_claude.py` plays back (`overloaded.jsonl`, `rate_limit_rejected.jsonl`), a private folder with a file over its size budget (`over-budget/`), rule files that each fail one count of the rule-shape check (`shape/`), and small sets of placeholder data (`location.jsonl`, `taste.jsonl`).
- `verify_daemon.py`: runs the whole daemon once, offline, on a scratch copy of the example private folder; `test_verify_daemon.py` starts it.
