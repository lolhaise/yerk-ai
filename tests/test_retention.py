"""Checks retention (retention.py), the daily sweep that keeps the state folder from growing forever, packing old logs and moving what is
past its days to the trash. Covered (Layer 11): each kind on a fixture state folder, private layer and Claude Code home; the guards (a
bound session, one written in the last 24 hours, a room with a turn in flight, a file a process holds open); what is never moved (the
memory tree, the private layer, attachments); the trash's week; a dry run that moves nothing; the morning line; a failure that exits 1."""
import gzip
import json
import os
import shutil
import stat
import time
from pathlib import Path

from yerk_ai import cli, context, curate, hooks, morning, retention, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.engine import project_dir
from yerk_ai.ledger import Ledger

DAY = 86400.0
NOW = time.mktime((2026, 10, 21, 4, 30, 0, 0, 0, -1))      # 04:30 local, after the timer
BOUND, OLD, FRESH, BUSY = (f"{c * 8}-{c * 4}-{c * 4}-{c * 4}-{c * 12}" for c in "abcd")


def ago(path, days):
    os.utime(path, (NOW - days * DAY, NOW - days * DAY))
    return path


def put(path, text="x", days=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return ago(path, days)


def layer(tmp_path, conf=""):
    """A private layer (the example's, with two accounts sharing one projects folder), a state folder with two rooms
    (room-a bound to BOUND, room-b with a turn in flight) and an attachment, and the rooms' project folders."""
    priv, state, home = tmp_path / "private", tmp_path / "state", tmp_path / "home"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    (priv / "accounts.toml").write_text(f'[accounts]\none = {{ config_dir = "{home}/.claude", order = 1 }}\n'
                                        f'two = {{ config_dir = "{home}/.claude-two", order = 2 }}\n')
    (priv / "yerkd.toml").write_text(conf)
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".claude-two").mkdir()
    (home / ".claude-two" / "projects").symlink_to(home / ".claude" / "projects")
    for room in ("room-a", "room-b"):
        (state / "rooms" / room / "work").mkdir(parents=True)
    put(state / "rooms" / "room-a" / "work" / "inbox" / "m1" / "photo.png", "p" * 300, days=400)
    (state / "sessions.json").write_text(json.dumps({"room-a": {"session_id": BOUND, "account": "one"}}))
    led = Ledger(state / "ledger.sqlite")
    led.inbound_add("m9", "room-b", "discord:2", "owner", 5)           # received, never answered: a turn in flight
    return priv, state, home / ".claude" / "projects", led


def project(projects, state, room):
    return projects / project_dir((state / "rooms" / room / "work").resolve())


def files(*roots):
    """Every file and folder under the roots with its size and time, the ledger's own files aside."""
    return {str(p): (p.lstat().st_size, p.lstat().st_mtime) for r in roots for p in r.rglob("*")
            if not p.name.startswith("ledger.sqlite")}


def test_a_stream_log_and_an_assembled_log_are_cut_into_their_day_and_a_day_past_its_days_goes(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    log = put(state / "logs" / "room-a.jsonl", '{"t": 1, "type": "a"}\n{"t": 2, "type": "b"}\n', days=1)
    put(state / "logs" / "room-b.jsonl", '{"t": 3}\n', days=1)                     # its room has a turn in flight
    asm = put(state / "rooms" / "room-a" / "assembled.jsonl", '{"t": 4, "text": "hi"}\n', days=1)
    os.chmod(asm, 0o600)
    yesterday, today = retention.day(NOW - DAY), retention.day(NOW)
    old = put(state / "logs" / f"room-a.{retention.day(NOW - 20 * DAY)}.jsonl.gz", days=20)
    kept = put(state / "logs" / f"room-a.{retention.day(NOW - 10 * DAY)}.jsonl.gz", days=10)
    with open(put(state / "logs" / "room-c.jsonl", '{"t": 5}\n', days=1), "a"):     # a process holds it open
        r = retention.run(priv, state, now=NOW)
    assert "error" not in r and not log.exists() and not asm.exists() and not old.exists() and kept.exists()
    cut = state / "logs" / f"room-a.{yesterday}.jsonl.gz"
    assert gzip.decompress(cut.read_bytes()) == b'{"t": 1, "type": "a"}\n{"t": 2, "type": "b"}\n'
    assert (state / "trash" / today / "logs" / "room-a.jsonl").read_text().count("\n") == 2   # the original waits a week
    acut = state / "rooms" / "room-a" / f"assembled.{yesterday}.jsonl.gz"
    assert gzip.decompress(acut.read_bytes()) == b'{"t": 4, "text": "hi"}\n' and acut.stat().st_mode & 0o777 == 0o600
    assert (state / "trash" / today / "logs" / old.name).exists()
    assert (state / "logs" / "room-b.jsonl").exists() and (state / "logs" / "room-c.jsonl").exists()
    assert r["kinds"]["logs"][0] == 2 and r["kinds"]["assembled"][0] == 1
    put(state / "logs" / "room-a.jsonl", '{"t": 6}\n', days=1)                     # a second cut the same day adds to it
    retention.run(priv, state, now=NOW + 60)
    assert gzip.decompress(cut.read_bytes()).endswith(b'{"t": 2, "type": "b"}\n{"t": 6}\n')


def test_a_rolled_assembled_log_loses_nothing_and_curate_reads_the_weeks_parts_in_order(tmp_path, monkeypatch):
    """F3-05: past RECORD_MAX the log rolled to .1, the next roll overwrote it and the sweep never cut a .1 (seen live: a busy
    room's archive held only 46 minutes of 367 turns), and curate's weekly count of the rules that rode read the live file
    alone. Three rolls in a day: every row stays, the sweep cuts each part into that day's archive in order, and curate
    reads the week's archives, then the parts, then the live file."""
    priv, state, projects, led = layer(tmp_path)
    room = state / "rooms" / "room-a"
    monkeypatch.setattr(hooks, "RECORD_MAX", 600)                                  # three rows to a part
    with monkeypatch.context() as m:
        for i in range(12):
            m.setattr(hooks.time, "time", lambda i=i: NOW - DAY + 60 * i)
            hooks.record(state, "room-a", "message", f"s{i}", {"p": 1}, "w" * 150, topic_rules=[f"job{i % 2}"])
    assert {json.loads(x)["session_id"] for p in room.glob("assembled.jsonl*") for x in p.read_text().splitlines()} == {f"s{i}" for i in range(12)}
    assert curate.rides(state, ["room-a"], NOW - 7 * DAY) == (12, {"job0": 6, "job1": 6})
    for p in room.glob("assembled.jsonl*"):
        ago(p, 1)
    assert retention.main(priv, state, now=NOW) == 0 and not list(room.glob("assembled.jsonl*"))
    cut = gzip.decompress((room / f"assembled.{retention.day(NOW - DAY)}.jsonl.gz").read_bytes()).decode().splitlines()
    assert [json.loads(x)["session_id"] for x in cut] == [f"s{i}" for i in range(12)]
    hooks.record(state, "room-a", "message", "s12", {"p": 1}, "w", topic_rules=["job0"])
    assert curate.rides(state, ["room-a"], NOW - 7 * DAY) == (13, {"job0": 7, "job1": 6})
    assert curate.rides(state, ["room-a"], NOW - DAY + 60 * 6) == (7, {"job0": 4, "job1": 3})     # a row before `since` is not counted


def test_a_bound_session_survives_and_an_unbound_one_past_its_window_moves_with_its_folder(tmp_path):
    priv, state, projects, led = layer(tmp_path, "[retention]\ntranscripts = 1\n")
    a = project(projects, state, "room-a")
    bound = put(a / f"{BOUND}.jsonl", days=40)
    old = put(a / f"{OLD}.jsonl", days=25 / 24)                                   # 25 hours, unbound
    put(a / OLD / "tool-results" / "r.txt", days=25 / 24)
    ago(a / OLD / "tool-results", 25 / 24), ago(a / OLD, 25 / 24)
    rotated = put(a / "rotated" / f"{OLD}.1792437160.jsonl", days=40)              # a copy the box once moved aside
    other = put(a / "memory" / "notes.md", days=400)                               # not a session's: never touched
    busy = put(project(projects, state, "room-b") / f"{BUSY}.jsonl", days=40)      # a room with a turn in flight
    r = retention.run(priv, state, now=NOW)
    assert bound.exists() and other.exists() and busy.exists()
    assert not old.exists() and not (a / OLD).exists() and not rotated.exists()
    trash = state / "trash" / retention.day(NOW) / "projects" / a.name
    assert (trash / old.name).exists() and (trash / OLD / "tool-results" / "r.txt").exists() and (trash / "rotated" / rotated.name).exists()
    assert r["kinds"]["transcripts"][0] == 3                                       # two accounts, one folder: once


def test_a_transcript_written_in_the_last_24_hours_stays_whatever_the_setting(tmp_path):
    priv, state, projects, led = layer(tmp_path, "[retention]\ntranscripts = 0.01\n")
    fresh = put(project(projects, state, "room-a") / f"{FRESH}.jsonl", days=23 / 24)
    with open(put(project(projects, state, "room-a") / f"{OLD}.jsonl", days=3)):  # held open: never moved
        retention.run(priv, state, now=NOW)
    assert fresh.exists() and (project(projects, state, "room-a") / f"{OLD}.jsonl").exists()


def test_an_attachment_never_moves_and_its_size_is_said(tmp_path, capsys):
    priv, state, projects, led = layer(tmp_path, "[retention]\nlogs = 1\nassembled = 1\ntranscripts = 1\nworkers = 1\ntrash = 1\n")
    photo = state / "rooms" / "room-a" / "work" / "inbox" / "m1" / "photo.png"
    mem = put(state / "memory" / "daily" / "2025-09-01.md", "- a line\n", days=400)          # the memory tree: never a kind
    assert retention.main(priv, state, now=NOW) == 0
    assert photo.read_text() == "p" * 300 and mem.read_text() == "- a line\n"
    assert retention.run(priv, state, dry=True, now=NOW)["kept"] == 300
    assert cli.main(["retention", "--dry-run", "--private", str(priv), "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert "inbox and KEEP folders: 300 bytes, kept" in out and "transcripts: 0 would be moved to the trash, 0 bytes" in out


def test_the_trash_empties_after_7_days_a_rooms_scratch_after_1_and_nothing_else_is_deleted(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    gone = put(state / "trash" / retention.day(NOW - 8 * DAY) / "logs" / "room-a.jsonl", "old")
    stays = put(state / "trash" / retention.day(NOW - 6 * DAY) / "logs" / "room-a.jsonl", "newer")
    week = put(state / "trash" / retention.day(NOW - 2 * DAY) / "rooms" / "room-a" / "assembled.jsonl", "a")   # not scratch: a week
    sgone = put(state / "trash" / "scratch" / retention.day(NOW - 2 * DAY) / "rooms" / "room-a" / "work" / "r.pgm", "rr")
    sstays = put(state / "trash" / "scratch" / retention.day(NOW - DAY) / "rooms" / "room-a" / "work" / "s.pgm", "s")
    disk = [p.stat().st_blocks * 512 for p in (gone, sgone)]                     # what each delete gives back (FB-01)
    r = retention.run(priv, state, now=NOW)
    assert not gone.parent.parent.exists() and stays.exists() and r["kinds"]["trash"] == [1, disk[0]]
    assert not sgone.parent.parent.parent.parent.exists() and sstays.exists() and week.exists() and r["kinds"]["scratch_trash"] == [1, disk[1]]
    assert oct((state / "trash").stat().st_mode & 0o777) == "0o700"


def test_the_trash_says_the_disk_its_delete_gave_back_never_the_files_lengths(tmp_path, capsys):
    """FB-01: a finished test run's sparse file 1 GB long holds no block, and a file linked from outside the trash keeps its
    blocks, so neither is disk the delete gave back; the line and retention.json say the report's blocks alone."""
    priv, state, projects, led = layer(tmp_path)
    old = state / "trash" / retention.day(NOW - 8 * DAY) / "workers" / "aaaaaa"
    os.truncate(put(old / "runs" / "sparse.img", ""), 1 << 30)
    report = put(old / "REPORT.md", "r" * 5000)
    os.link(put(tmp_path / "elsewhere" / "kept.txt", "k" * 9000), old / "linked.txt")
    freed = report.stat().st_blocks * 512
    assert retention.main(priv, state, dry=True, now=NOW) == 0 and old.exists()
    assert f"trash: 1 would be deleted, {words.size(freed)}" in capsys.readouterr().out
    assert retention.main(priv, state, now=NOW) == 0 and not old.exists() and (tmp_path / "elsewhere" / "kept.txt").exists()
    assert f"trash: 1 deleted, {words.size(freed)}" in capsys.readouterr().out
    assert json.loads((state / retention.LAST).read_text())["kinds"]["trash"] == [1, freed]


def test_old_timing_rows_go_to_the_trash_then_leave_the_ledger_and_the_wal_is_checkpointed(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    old = led.outbound(sent=NOW - 100 * DAY, room="room-a", kind="reply", message_id="o1", inbound="m1")
    new = led.outbound(sent=NOW - 10 * DAY, room="room-a", kind="reply", message_id="o2", inbound="m2")
    led.many([("hook_calls", {"t": NOW - 100 * DAY, "room": "room-a", "event": "Stop", "ms": 3.0, "outcome": "ok"}),
              ("hook_calls", {"t": NOW - DAY, "room": "room-a", "event": "Stop", "ms": 4.0, "outcome": "ok"})])
    turn = led.record(room="room-a", started=NOW - 200 * DAY, outcome="ok")       # not a timing table: kept
    r = retention.run(priv, state, now=NOW)
    assert [x["id"] for x in led.rows(10, "outbound")] == [new] and len(led.rows(10, "hook_calls")) == 1
    assert led.rows(10)[0]["id"] == turn and led.inbound_get("m9")
    links = led.older("message_links", "pos", 99)
    assert [(x["row_key"], x["message_id"]) for x in links if x["tbl"] == "outbound"] == [(str(new), "m2")]
    kept = gzip.decompress((state / "trash" / retention.day(NOW) / "ledger" / "outbound.jsonl.gz").read_bytes())
    assert json.loads(kept)["id"] == old and r["kinds"]["ledger"][0] == 2
    assert (state / "ledger.sqlite-wal").stat().st_size == 0


def test_a_finished_workers_folder_and_its_transcript_go_after_their_days_and_no_other(tmp_path, monkeypatch):
    priv, state, projects, led = layer(tmp_path)
    for wid, finished in (("aaaaaa", NOW - 31 * DAY), ("bbbbbb", NOW - 29 * DAY), ("cccccc", None)):
        put(state / "workers" / wid / "REPORT.md", days=40)
        put(projects / project_dir((state / "workers" / wid).resolve()) / f"{OLD}.jsonl", days=40)
        led.worker_add(id=wid, room="room-a", staging=str(state / "workers" / wid), started=NOW - 40 * DAY, finished=finished)
    put(tmp_path / "elsewhere" / "dddddd" / "REPORT.md", days=40)                  # outside the workers root
    led.worker_add(id="dddddd", room="room-a", staging=str(tmp_path / "elsewhere" / "dddddd"), finished=NOW - 99 * DAY)
    led.worker_add(id="eeeeee", room="room-a", staging="bbbbbb", finished=NOW - 99 * DAY)   # not a path of its own
    monkeypatch.chdir(state / "workers")                                         # where it would resolve to bbbbbb
    r = retention.run(priv, state, now=NOW)
    assert not (state / "workers" / "aaaaaa").exists() and not (projects / project_dir((state / "workers" / "aaaaaa").resolve())).exists()
    assert (state / "workers" / "bbbbbb").exists() and (state / "workers" / "cccccc").exists()
    assert (tmp_path / "elsewhere" / "dddddd").exists() and r["kinds"]["workers"][0] == 2 and "error" not in r
    assert (state / "workers").is_dir()
    assert (state / "trash" / retention.day(NOW) / "workers" / "aaaaaa" / "REPORT.md").exists()


def test_a_finished_workers_pytest_trees_go_a_day_after_it_finished_and_the_rest_waits_its_days(tmp_path):
    """F10-04: a finished worker's pytest temp trees (pytest-of-*, in its tmp/ or deeper, as runs/<name>/tmp/) held 9.9 GB on
    disk for the folder's whole 30 days. A worker not finished, or finished less than a day ago, keeps them; a link is never
    followed or moved."""
    priv, state, projects, led = layer(tmp_path)
    for wid, finished in (("aaaaaa", NOW - 2 * DAY), ("bbbbbb", NOW - DAY / 2), ("cccccc", None)):
        w = state / "workers" / wid
        put(w / "REPORT.md", days=3)
        put(w / "tmp" / "pytest-of-owner" / "pytest-1" / "test_x0" / "state.db", "d" * 10, days=3)
        put(w / "runs" / "suite-a" / "tmp" / "pytest-of-owner" / "pytest-0" / "f.txt", "f" * 5, days=3)
        led.worker_add(id=wid, room="room-a", staging=str(w), started=NOW - 3 * DAY, finished=finished)
    a = state / "workers" / "aaaaaa"
    (a / "pytest-of-link").symlink_to(state / "workers" / "cccccc" / "tmp")
    r = retention.run(priv, state, now=NOW)
    assert not (a / "tmp" / "pytest-of-owner").exists() and not (a / "runs" / "suite-a" / "tmp" / "pytest-of-owner").exists()
    assert (a / "REPORT.md").exists() and (a / "tmp").is_dir() and (a / "pytest-of-link").is_symlink()
    assert all((state / "workers" / w / "tmp" / "pytest-of-owner").exists() for w in ("bbbbbb", "cccccc"))
    trash = state / "trash" / retention.day(NOW) / "workers" / "aaaaaa"
    assert (trash / "runs" / "suite-a" / "tmp" / "pytest-of-owner" / "pytest-0" / "f.txt").read_text() == "fffff"
    assert r["kinds"]["pytest"] == [2, 15] and "error" not in r


TREES = ("venv-3.12", "runs/suite-a/wt/.venv", "runs/suite-a/wt/yerk_ai/__pycache__", "runs/suite-a/wt/.mypy_cache",
         "runs/suite-a/wt/.ruff_cache", "runs/suite-a/wt/.pytest_cache", "runs/suite-a/wt/web/node_modules")


def builder(w, led=None, finished=NOW - 2 * DAY):
    """A builder's folder: its record, a worktree's sources and a folder merely named venv, and what its run built (TREES): an
    environment at the top and one three folders down in the worktree, each with its own cache, the four kinds of cache folder
    and a node_modules. {path in the folder: bytes} of each built tree; with `led`, the worker's row, finished at `finished`."""
    for name in ("BRIEF.md", "PROMPT.md", "REPORT.md", "TASKS.md", "worker.json", "exit.json", "stdout.jsonl"):
        put(w / name, "r" * 3, days=3)
    put(w / "runs" / "suite-a" / "wt" / ".git", "gitdir: /repo/.git/worktrees/wt\n", days=3)
    put(w / "runs" / "suite-a" / "wt" / "yerk_ai" / "retention.py", "s" * 40, days=3)
    put(w / "runs" / "suite-a" / "wt" / "venv" / "requirements.txt", "pytest\n", days=3)      # named venv, no pyvenv.cfg
    for i, rel in enumerate(TREES):
        if "venv" in rel:
            put(w / rel / "pyvenv.cfg", "home = /usr/bin\n", days=3)
            put(w / rel / "lib" / "python3.13" / "site-packages" / "pkg" / "__pycache__" / "m.pyc", "e" * (100 + i), days=3)
        else:
            put(w / rel / "sub" / "f.bin", "c" * (10 + i), days=3)
    if led is not None:
        led.worker_add(id=w.name, room="room-a", staging=str(w), started=NOW - 3 * DAY, finished=finished)
    return {rel: retention.size(retention.tree(w / rel)) for rel in TREES}


def test_a_finished_workers_environments_caches_and_node_modules_go_a_day_after_it_finished_and_its_record_and_sources_stay(tmp_path):
    """43 GB of the workers folder's 53 GB on Nov 20 was what a run built to test (130 environments, 2,494 cache folders),
    each waiting the folder's 30 days. They go to the trash under workers/<id>/ at their own paths a day after the worker
    finished; its record, its worktree's sources and a folder merely named venv stay."""
    priv, state, projects, led = layer(tmp_path)
    w = state / "workers" / "aaaaaa"
    trees = builder(w, led)
    r = retention.run(priv, state, now=NOW)
    trash = state / "trash" / retention.day(NOW) / "workers" / "aaaaaa"
    assert "error" not in r and not any((w / rel).exists() for rel in TREES)
    assert all((trash / rel).is_dir() for rel in TREES) and (trash / "runs/suite-a/wt/.venv/pyvenv.cfg").read_text() == "home = /usr/bin\n"
    assert all((w / n).exists() for n in ("BRIEF.md", "PROMPT.md", "REPORT.md", "TASKS.md", "worker.json", "exit.json", "stdout.jsonl"))
    assert [(w / "runs/suite-a/wt" / p).read_text() for p in (".git", "yerk_ai/retention.py", "venv/requirements.txt")] == [
        "gitdir: /repo/.git/worktrees/wt\n", "s" * 40, "pytest\n"]
    assert r["kinds"]["built"] == [7, sum(trees.values())] and "pytest" not in r["kinds"] and "workers" not in r["kinds"]


def test_a_worker_not_finished_and_one_finished_half_a_day_ago_keep_what_their_runs_built(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    builder(state / "workers" / "bbbbbb", led, finished=NOW - DAY / 2)
    builder(state / "workers" / "cccccc", led, finished=None)
    before = files(state / "workers")
    r = retention.run(priv, state, now=NOW)
    assert files(state / "workers") == before and "built" not in r["kinds"] and "error" not in r


def test_a_link_to_an_environment_or_named_like_a_cache_stays_unfollowed_and_a_pyvenv_cfg_folder_or_link_marks_nothing(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    w, out = state / "workers" / "aaaaaa", tmp_path / "elsewhere"
    put(out / "env" / "pyvenv.cfg", "home = /usr/bin\n")
    put(out / "env" / "lib" / "x.py", "x")
    put(out / "cache" / "__pycache__" / "y.pyc", "y")
    put(w / "REPORT.md")
    (w / "env-link").symlink_to(out / "env", target_is_directory=True)              # a link to an environment
    (w / "wt").mkdir()
    (w / "wt" / "__pycache__").symlink_to(out / "cache", target_is_directory=True)  # a link named like a cache
    (w / "wt" / "node_modules").symlink_to(out / "env", target_is_directory=True)
    put(w / "dir-cfg" / "pyvenv.cfg" / "inside.txt", "d")                         # a pyvenv.cfg that is a folder
    put(w / "link-cfg" / "lib" / "z.py", "z")
    (w / "link-cfg" / "pyvenv.cfg").symlink_to(out / "env" / "pyvenv.cfg")          # and one that is a link
    led.worker_add(id="aaaaaa", room="room-a", staging=str(w), started=NOW - 3 * DAY, finished=NOW - 2 * DAY)
    before = files(w, out)
    r = retention.run(priv, state, now=NOW)
    assert files(w, out) == before and "built" not in r["kinds"] and "error" not in r
    assert all((w / p).is_symlink() for p in ("env-link", "wt/__pycache__", "wt/node_modules", "link-cfg/pyvenv.cfg"))


def test_an_environment_holding_caches_and_a_cache_folder_holding_an_environment_move_once_and_count_once(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    w = state / "workers" / "aaaaaa"
    put(w / "venv-3.13" / "pyvenv.cfg", "home = /usr/bin\n")
    for rel in ("lib/python3.13/site-packages/a/__pycache__/a.pyc", ".mypy_cache/3.13/a.json", "node_modules/x/i.js", ".pytest_cache/v/n"):
        put(w / "venv-3.13" / rel, "e" * 10)
    put(w / "wt" / ".mypy_cache" / "env" / "pyvenv.cfg", "home = /usr/bin\n")
    put(w / "wt" / ".mypy_cache" / "env" / "__pycache__" / "b.pyc", "m" * 5)
    led.worker_add(id="aaaaaa", room="room-a", staging=str(w), started=NOW - 3 * DAY, finished=NOW - 2 * DAY)
    sizes = [retention.size(retention.tree(p)) for p in (w / "venv-3.13", w / "wt" / ".mypy_cache")]
    dry = retention.run(priv, state, dry=True, now=NOW)
    r = retention.run(priv, state, now=NOW)
    trash = state / "trash" / retention.day(NOW) / "workers" / "aaaaaa"
    assert dry["kinds"]["built"] == r["kinds"]["built"] == [2, sum(sizes)] == [2, 16 + 40 + 16 + 5]
    assert (trash / "venv-3.13" / "node_modules" / "x" / "i.js").exists() and (trash / "wt" / ".mypy_cache" / "env" / "__pycache__" / "b.pyc").exists()
    assert sorted(p.name for p in trash.iterdir()) == ["venv-3.13", "wt"] and [p.name for p in (trash / "wt").iterdir()] == [".mypy_cache"]


def test_a_pyvenv_cfg_at_the_top_of_a_workers_folder_moves_nothing_and_the_record_beside_it_stays(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    w = state / "workers" / "aaaaaa"
    for rel in ("pyvenv.cfg", "REPORT.md", "TASKS.md", "stdout.jsonl", "bin/python", "lib/python3.13/site-packages/x.py"):
        put(w / rel, "t")
    led.worker_add(id="aaaaaa", room="room-a", staging=str(w), started=NOW - 3 * DAY, finished=NOW - 2 * DAY)
    before = files(w)
    r = retention.run(priv, state, now=NOW)
    assert files(w) == before and "built" not in r["kinds"] and "error" not in r


def test_a_built_tree_a_process_holds_stays_that_sweep_and_goes_on_the_next_once_it_is_free(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    w = state / "workers" / "aaaaaa"
    so = put(w / "wt" / ".venv" / "lib" / "x.so", "s" * 4)
    put(w / "wt" / ".venv" / "pyvenv.cfg", "home = /usr/bin\n")
    runs_in = put(w / "wt" / ".mypy_cache" / "3.13" / "m.json", "m" * 2).parent
    free = put(w / "wt" / "yerk_ai" / "__pycache__" / "r.pyc", "r")
    led.worker_add(id="aaaaaa", room="room-a", staging=str(w), started=NOW - 3 * DAY, finished=NOW - 2 * DAY)
    proc = tmp_path / "proc"
    for d in (proc / "self" / "fd", proc / "4242" / "fd"):
        d.mkdir(parents=True)
    (proc / "4242" / "fd" / "3").symlink_to(so)                                   # an open file inside the environment
    (proc / "4242" / "cwd").symlink_to(runs_in)                                    # a process running in a cache folder
    r = retention.run(priv, state, now=NOW, proc=proc)
    assert so.exists() and runs_in.is_dir() and not free.exists() and r["kinds"]["built"] == [1, 1]
    for p in (proc / "4242" / "fd" / "3", proc / "4242" / "cwd"):
        p.unlink()                                                                 # the process ended
    r = retention.run(priv, state, now=NOW + DAY, proc=proc)
    assert not (w / "wt" / ".venv").exists() and not (w / "wt" / ".mypy_cache").exists() and r["kinds"]["built"] == [2, 4 + 16 + 2]
    assert (state / "trash" / retention.day(NOW + DAY) / "workers" / "aaaaaa" / "wt" / ".venv" / "lib" / "x.so").read_text() == "ssss"


def test_built_days_come_from_yerkd_toml_and_a_value_not_a_positive_number_is_the_default(tmp_path):
    priv, state, projects, led = layer(tmp_path, "[retention]\nbuilt = 3\n")
    w = state / "workers" / "aaaaaa"
    trees = builder(w, led)                                                        # finished two days ago
    r = retention.run(priv, state, now=NOW)
    assert all((w / rel).is_dir() for rel in TREES) and "built" not in r["kinds"]
    r = retention.run(priv, state, now=NOW + 1.5 * DAY)                            # three and a half days
    assert not any((w / rel).exists() for rel in TREES) and r["kinds"]["built"] == [7, sum(trees.values())]
    for bad in ("x", True, 0, -2):
        assert retention.keep({"retention": {"built": bad}})["built"] == 1, bad
    (priv / "yerkd.toml").write_text("[retention]\nbuilt = -1\n")
    v = state / "workers" / "bbbbbb"
    builder(v, led, finished=NOW - 1.2 * DAY)
    assert retention.run(priv, state, now=NOW)["kinds"]["built"][0] == 7 and not (v / "venv-3.12").exists()


def test_a_dry_run_names_built_with_what_the_run_then_moves_and_retention_json_and_the_morning_line_carry_it(tmp_path, capsys):
    priv, state, projects, led = layer(tmp_path)
    nbytes = sum(builder(state / "workers" / "aaaaaa", led).values())
    accounts, cool = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json")
    before = files(tmp_path)
    assert retention.main(priv, state, dry=True, now=NOW) == 0 and files(tmp_path) == before
    assert f"built: 7 would be moved to the trash, {words.size(nbytes)}" in capsys.readouterr().out
    assert retention.main(priv, state, now=NOW) == 0
    assert f"built: 7 moved to the trash, {words.size(nbytes)}" in capsys.readouterr().out
    assert json.loads((state / retention.LAST).read_text())["kinds"]["built"] == [7, nbytes]
    lines, _ = morning.build(priv, state, accounts, cool, led, {"started": NOW - DAY, "units": []}, NOW + 7200)
    assert retention.line(state, NOW + 60) == ("", f"{words.PLAIN} swept: built {words.size(nbytes)}") and f"{words.PLAIN} swept: built {words.size(nbytes)}" in lines


def test_past_its_30_days_the_whole_folder_goes_with_what_its_run_built_and_a_worker_outside_the_root_is_never_touched(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    trees = builder(state / "workers" / "aaaaaa", led, finished=NOW - 31 * DAY)
    out = tmp_path / "elsewhere"
    for wid, finished in (("dddddd", NOW - 2 * DAY), ("eeeeee", NOW - 99 * DAY)):    # outside the workers root
        builder(out / wid, led, finished=finished)
    (state / "workers" / "ffffff").symlink_to(out / "dddddd", target_is_directory=True)   # in the root, a link out of it
    led.worker_add(id="ffffff", room="room-a", staging=str(state / "workers" / "ffffff"), finished=NOW - 2 * DAY)
    before = files(out)
    r = retention.run(priv, state, now=NOW)
    gone = state / "trash" / retention.day(NOW) / "workers" / "aaaaaa"
    assert not (state / "workers" / "aaaaaa").exists() and all(retention.size(retention.tree(gone / rel)) == n for rel, n in trees.items())
    assert (gone / "REPORT.md").exists() and r["kinds"]["workers"][0] == 1 and "built" not in r["kinds"] and "error" not in r
    assert files(out) == before and (state / "workers" / "ffffff").is_symlink()


def test_pytest_trees_still_go_as_pytest_counted_apart_from_built(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    w = state / "workers" / "aaaaaa"
    put(w / "tmp" / "pytest-of-owner" / "pytest-1" / "test_x0" / "copy" / ".venv" / "pyvenv.cfg", "home = /usr/bin\n")
    put(w / "tmp" / "pytest-of-owner" / "pytest-1" / "test_x0" / "copy" / "__pycache__" / "a.pyc", "a" * 9)
    put(w / "wt" / "__pycache__" / "b.pyc", "b" * 7)
    led.worker_add(id="aaaaaa", room="room-a", staging=str(w), started=NOW - 3 * DAY, finished=NOW - 2 * DAY)
    r = retention.run(priv, state, now=NOW)
    trash = state / "trash" / retention.day(NOW) / "workers" / "aaaaaa"
    assert r["kinds"]["pytest"] == [1, 16 + 9] and r["kinds"]["built"] == [1, 7] and not (w / "tmp" / "pytest-of-owner").exists()
    assert (trash / "tmp" / "pytest-of-owner" / "pytest-1" / "test_x0" / "copy" / ".venv" / "pyvenv.cfg").exists() and (trash / "wt" / "__pycache__" / "b.pyc").exists()


def test_only_the_five_names_are_built_trees_each_matched_whole_and_a_folder_named_for_real_work_stays(tmp_path):
    """The list is five names and a name is matched whole: a folder a person names for work (scratch, build, dist, tmp, data),
    one that only starts, ends or is cased like a cache folder, and one named like an environment that holds no pyvenv.cfg
    stay, with everything in them."""
    priv, state, projects, led = layer(tmp_path)
    w = state / "workers" / "aaaaaa"
    put(w / "REPORT.md")
    for name in ("scratch", "build", "dist", "tmp", "data", "node_modules_old", "my__pycache__", "__pycache__x", ".mypy_cache.keep",
                 ".ruff_cache2", "Node_Modules", "venv", ".venv"):
        put(w / "wt" / name / "f.bin", "k" * 5)
    led.worker_add(id="aaaaaa", room="room-a", staging=str(w), started=NOW - 3 * DAY, finished=NOW - 2 * DAY)
    before = files(w)
    r = retention.run(priv, state, now=NOW)
    assert retention.BUILT == ("__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "node_modules")
    assert files(w) == before and "built" not in r["kinds"] and "error" not in r


def test_a_pytest_tree_waits_its_own_days_when_they_are_longer_than_builts_and_is_not_walked_meanwhile(tmp_path):
    """`pytest = 3` beside built's 1: a worker finished two days ago loses what its run built, and its pytest tree waits its
    third day whole, the cache folder inside it too, then goes as pytest."""
    priv, state, projects, led = layer(tmp_path, "[retention]\npytest = 3\n")
    w = state / "workers" / "aaaaaa"
    inner = put(w / "tmp" / "pytest-of-owner" / "pytest-1" / "test_x0" / "__pycache__" / "a.pyc", "a" * 9)
    put(w / "wt" / "__pycache__" / "b.pyc", "b" * 7)
    led.worker_add(id="aaaaaa", room="room-a", staging=str(w), started=NOW - 3 * DAY, finished=NOW - 2 * DAY)
    r = retention.run(priv, state, now=NOW)
    assert r["kinds"]["built"] == [1, 7] and "pytest" not in r["kinds"] and inner.read_text() == "a" * 9 and "error" not in r
    r = retention.run(priv, state, now=NOW + 1.5 * DAY)                            # three and a half days
    assert r["kinds"]["pytest"] == [1, 9] and "built" not in r["kinds"] and not (w / "tmp" / "pytest-of-owner").exists() and "error" not in r


def test_a_job_runs_folder_goes_after_its_days_once_ended_and_delivered_and_never_while_undelivered(tmp_path):
    """F10-14: every job run leaves <state>/jobs/<name>/<stamp>/ and nothing pruned them, about 145 a day. A run the ledger
    still marks undelivered stays whatever its age (the daemon's restart sweep reads its stdout), so does one never ended."""
    priv, state, projects, led = layer(tmp_path)
    runs = {}
    for stamp, ended, delivery in (("20261001-040000", NOW - 20 * DAY, "silent (no output)"), ("20261002-040000", NOW - 19 * DAY, "delivered"),
                                   ("20261003-040000", NOW - 18 * DAY, "undelivered: the daemon was not running"),
                                   ("20261004-040000", None, None), ("20261015-040000", NOW - 6 * DAY, "delivered")):
        runs[stamp] = put(state / "jobs" / "example-job" / stamp / "stdout", "out", days=30).parent
        led.job_run_add(job="example-job", kind="script", room="room-a", fired=NOW - 30 * DAY, ended=ended, delivery=delivery, folder=str(runs[stamp]))
    stray = put(state / "jobs" / "example-job" / "20260901-040000" / "stdout", days=60).parent             # no row: never touched
    out = put(tmp_path / "elsewhere" / "x" / "20261001-040000" / "stdout").parent                          # not under the jobs folder
    led.job_run_add(job="x", kind="script", room="room-a", fired=NOW - 99 * DAY, ended=NOW - 99 * DAY, delivery="delivered", folder=str(out))
    r = retention.run(priv, state, now=NOW)
    assert not runs["20261001-040000"].exists() and not runs["20261002-040000"].exists() and stray.exists() and out.exists()
    assert all(runs[s].exists() for s in ("20261003-040000", "20261004-040000", "20261015-040000"))
    assert (state / "trash" / retention.day(NOW) / "jobs" / "example-job" / "20261001-040000" / "stdout").read_text() == "out"
    assert r["kinds"]["jobs"] == [2, 6] and "error" not in r


def test_a_watchs_held_finding_keeps_its_folder_past_its_days_and_a_batched_or_delivered_one_goes(tmp_path):
    """J4: a held finding's output is what its room is told when the hour allows, so its folder stays whatever its age; one
    told with a newer run (batched) and a delivered one go after their days, as any run's."""
    from yerk_ai.jobs import BATCHED, HELD
    priv, state, projects, led = layer(tmp_path)
    runs = {}
    for stamp, delivery in (("20261001-040000", HELD), ("20261002-040000", f"{BATCHED}9"), ("20261003-040000", "delivered")):
        runs[stamp] = put(state / "jobs" / "shop" / stamp / "stdout", "found", days=30).parent
        led.job_run_add(job="shop", kind="script", room="room-a", fired=NOW - 30 * DAY, ended=NOW - 30 * DAY, delivery=delivery,
                        folder=str(runs[stamp]))
    r = retention.run(priv, state, now=NOW)
    assert runs["20261001-040000"].exists() and (runs["20261001-040000"] / "stdout").read_text() == "found"
    assert not runs["20261002-040000"].exists() and not runs["20261003-040000"].exists() and r["kinds"]["jobs"][0] == 2


def test_a_dry_run_moves_nothing_and_counts_what_the_run_then_moves(tmp_path):
    priv, state, projects, led = layer(tmp_path, "[retention]\ntranscripts = 1\n")
    put(state / "logs" / "room-a.jsonl", '{"t": 1}\n', days=1)
    put(project(projects, state, "room-a") / f"{OLD}.jsonl", days=3)
    put(state / "trash" / retention.day(NOW - 9 * DAY) / "x", "old")
    led.outbound(sent=NOW - 100 * DAY, room="room-a", kind="reply", message_id="o1")
    before = files(tmp_path)
    dry = retention.run(priv, state, dry=True, now=NOW)
    assert files(tmp_path) == before and not (state / retention.LAST).exists()
    real = retention.run(priv, state, now=NOW)
    assert dry["kinds"] == real["kinds"] and set(real["kinds"]) == {"logs", "transcripts", "ledger", "trash"}
    assert files(priv) == {k: v for k, v in before.items() if k.startswith(f"{priv}/")}        # the private layer: as it was


def test_the_morning_diff_says_what_the_sweep_moved_by_kind_and_nothing_when_it_moved_nothing(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    accounts, cool = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json")
    assert retention.main(priv, state, now=NOW) == 0 and retention.line(state, NOW) is None   # nothing moved: no line
    put(state / "logs" / "room-a.jsonl", "y" * 2048, days=1)
    assert retention.main(priv, state, now=NOW) == 0
    lines, _ = morning.build(priv, state, accounts, cool, led, {"started": NOW - DAY, "units": []}, NOW + 7200)
    assert f"{words.PLAIN} swept: logs 2 KB" in lines
    assert retention.line(state, NOW + 2 * DAY) is None                           # a day later it is old news


def test_a_failure_exits_1_and_the_morning_diff_opens_with_it(tmp_path, capsys):
    priv, state, projects, led = layer(tmp_path)
    accounts, cool = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json")
    assert retention.main(priv, state, now=NOW, proc=tmp_path / "no-proc") == 1     # cannot tell what is open: refuses
    assert "no /proc here to say which files are open" in capsys.readouterr().err
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": NOW - DAY, "units": []}, NOW + 60)
    said = [x for x in lines if x.startswith(f"{words.NOT_DONE} retention failed: no /proc")]
    assert said and words.width(said[0]) <= words.PHONE and "retention failed" in stamp["findings"]
    assert lines[0].startswith(f"{words.NOT_DONE} morning:")
    (priv / "yerkd.toml").write_text("[retention\n")                            # a yerkd.toml that does not parse
    assert retention.main(priv, state, now=NOW + 120) == 1
    assert retention.line(state, NOW + 180) == ("retention failed", f"{words.NOT_DONE} retention failed: TOMLDecodeError")


def test_the_days_come_from_yerkd_toml_and_explain_says_them(tmp_path, capsys):
    priv, state, projects, led = layer(tmp_path, '[retention]\nlogs = 3\nledger = "x"\nworkers = -1\ntrash = true\n')
    assert retention.keep({"retention": {"logs": 3, "ledger": "x", "workers": -1, "trash": True, "scratch_mb": 0}}) == {
        "logs": 3, "assembled": 14, "transcripts": 30, "ledger": 90, "jobs": 14, "workers": 30, "pytest": 1, "built": 1, "scratch": 2, "trash": 7,
        "scratch_trash": 1, "scratch_mb": 500}
    assert cli.main(["explain", "retention", "--private", str(priv), "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{words.DONE} retention: ") and "Here: logs 3 days, assembled 14 days, transcripts 30 days" in out
    assert "scratch 2 days, trash 7 days, scratch_trash 1 days, a room's scratch at most 500 MB ([retention] in yerkd.toml)" in out


def test_a_rooms_scratch_goes_after_its_days_then_the_oldest_past_the_cap_and_never_the_persons_files(tmp_path):
    priv, state, projects, led = layer(tmp_path, "[retention]\nscratch_mb = 0.0005\n")             # 500 bytes
    room = state / "rooms" / "room-a"
    old = put(room / "work" / "outbox" / "old.pdf", "o" * 300, days=3)                      # past its 2 days
    sent = put(room / "outbox" / "20261018-090000-k1-s1-report.pdf", "s" * 200, days=2.5)   # the copy of a file it sent
    p1, p2 = (put(room / "work" / "outbox" / "pages" / f"p{i}.pgm", "p" * 400, days=d) for i, d in ((1, 1.6), (2, 1.5)))
    notes = put(room / "work" / "notes.txt", "n" * 300, days=1)                             # under the cap once p1 and p2 left
    fresh = put(room / "work" / "outbox" / "fresh.png", "f" * 100, days=0.5 / 24)
    own = [put(room / "work" / ".claude" / "settings.json", "{}", days=10), put(room / "work" / ".bashrc", "", days=10)]
    photo = room / "work" / "inbox" / "m1" / "photo.png"                                     # the person's, 400 days old
    target = put(tmp_path / "elsewhere" / "keep.txt", "k", days=10)
    (link := room / "work" / "outbox" / "link.txt").symlink_to(target)
    os.utime(link, (NOW - 3 * DAY, NOW - 3 * DAY), follow_symlinks=False)
    (room / "work" / "linked-folder").symlink_to(target.parent, target_is_directory=True)   # never walked into
    mid = put(state / "rooms" / "room-b" / "work" / "outbox" / "old.bin", "b" * 50, days=5)   # its room has a turn in flight
    render = put(room / "work" / "renders" / "r1.pgm", "r", days=4)                          # its folder empties and stays
    tiny, young = put(state / "rooms" / "room-d" / "work" / "tiny.txt", "t", days=2.1), put(state / "rooms" / "room-d" / "work" / "young.txt", "y", days=1.9)
    with open(put(room / "work" / "outbox" / "held.log", "h", days=3), "a"):                  # a process holds it open
        dry = retention.run(priv, state, dry=True, now=NOW)
        r = retention.run(priv, state, now=NOW)
    assert "error" not in r and dry["kinds"]["scratch"] == r["kinds"]["scratch"] == [7, 1302]   # a link moves as itself: 0 bytes
    assert not any(p.exists() or p.is_symlink() for p in (old, sent, p1, p2, link, render, tiny))   # tiny: past its days, under the cap
    assert not (room / "work" / "outbox" / "pages").exists() and (room / "work" / "renders").is_dir()
    assert all(p.exists() for p in (notes, fresh, *own, photo, target, mid, young, room / "work" / "outbox" / "held.log"))
    gone = state / "trash" / "scratch" / retention.day(NOW) / "rooms" / "room-a"
    assert (gone / "outbox" / sent.name).read_text() == "s" * 200 and (gone / "work" / "outbox" / "pages" / "p2.pgm").exists()
    assert (gone / "work" / "outbox" / "link.txt").is_symlink() and (room / "work" / "linked-folder").is_symlink()


def test_a_room_yerkd_says_is_busy_or_waiting_keeps_all_and_a_yerkd_that_does_not_answer_keeps_every_room(tmp_path, monkeypatch):
    priv, state, projects, led = layer(tmp_path)
    led.inbound_set("m9", state="answered")                                       # no open row: yerkd's word alone
    render = put(state / "rooms" / "room-a" / "work" / "render.pgm", "r", days=5)
    log = put(state / "logs" / "room-a.jsonl", '{"t": 1}\n', days=1)
    monkeypatch.setattr(retention.status, "alive", lambda s: True)
    for said in ({"rooms": [{"room": "room-a", "busy": True, "waiting": 0}]}, {"rooms": [{"room": "room-a", "busy": False, "waiting": 2}]},
                 None):                                                          # running, and not answering
        monkeypatch.setattr(retention.status, "ask", lambda s, said=said: said)
        assert "error" not in retention.run(priv, state, now=NOW) and render.exists() and log.exists(), said
    monkeypatch.setattr(retention.status, "ask", lambda s: {"rooms": [{"room": "room-a", "busy": False, "waiting": 0}]})
    retention.run(priv, state, now=NOW)
    assert not render.exists() and not log.exists()                               # quiet: swept


def test_a_trash_folder_holding_an_open_file_and_a_folder_a_process_runs_in_stay(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    held = put(state / "trash" / retention.day(NOW - 9 * DAY) / "logs" / "room-a.jsonl", "h")
    deep = put(state / "rooms" / "room-a" / "work" / "renders" / "run1" / "r.pgm", "r", days=5)
    proc = tmp_path / "proc"
    for d in (proc / "self" / "fd", proc / "4242" / "fd"):
        d.mkdir(parents=True)
    (proc / "4242" / "cwd").symlink_to(deep.parent)                               # a process runs in the folder the sweep empties
    (proc / "4242" / "fd" / "3").symlink_to(held)                                  # and holds a file in a week-old trash folder
    r = retention.run(priv, state, now=NOW, proc=proc)
    assert held.exists() and "trash" not in r["kinds"] and not deep.exists() and deep.parent.is_dir()


def test_a_file_written_in_the_last_hour_stays_whatever_the_cap(tmp_path):
    priv, state, projects, led = layer(tmp_path, "[retention]\nscratch_mb = 0.0005\n")
    work = state / "rooms" / "room-c" / "work"
    older = put(work / "render-1.pgm", "r" * 100, days=2 / 24)
    newest = put(work / "render-2.pgm", "r" * 2000, days=10 / 1440)                         # ten minutes old, over the cap alone
    r = retention.run(priv, state, now=NOW)
    assert not older.exists() and newest.exists() and r["kinds"]["scratch"] == [1, 100]


def test_a_folder_with_a_keep_file_never_moves_by_age_or_cap_and_its_size_is_said(tmp_path, capsys):
    """Seen live: the sweep took the eleven offer drafts a room wrote and cited in its
    facts, past their two days, and later the room told the owner they were held. A top folder of work/ holding a file named
    KEEP is the room's own work: never moved by age or by the cap, and not counted toward the cap."""
    priv, state, projects, led = layer(tmp_path, "[retention]\nscratch_mb = 0.0005\n")             # 500 bytes
    work = state / "rooms" / "room-a" / "work"
    why = put(work / "offer-2026-10-28" / "KEEP", "the drafts the owner holds\n", days=3)
    drafts = [put(work / "offer-2026-10-28" / "FIVERR.md", "f" * 400, days=3),                # 727 bytes kept: past the cap alone
              put(work / "offer-2026-10-28" / "replies" / "github.txt", "g" * 300, days=5)]
    old = put(work / "old.bin", "o" * 200, days=3)                                          # past its days: moves as before
    deeper = [put(work / "renders" / "run1" / "KEEP", "k", days=3), put(work / "renders" / "run1" / "r.pgm", "r" * 100, days=3)]
    linked = [put(work / "linked-keep" / "a.txt", "a" * 50, days=3), work / "linked-keep" / "KEEP"]
    linked[1].symlink_to(put(tmp_path / "elsewhere" / "KEEP", "k"))                          # a KEEP that is a link keeps nothing
    (folder := work / "linked-folder").symlink_to(tmp_path / "elsewhere", target_is_directory=True)   # and a linked folder
    for link in (linked[1], folder):
        os.utime(link, (NOW - 3 * DAY, NOW - 3 * DAY), follow_symlinks=False)
    notes = put(work / "notes.txt", "n" * 300, days=1)        # under the cap once the old ones left, the kept bytes not counted
    dry = retention.run(priv, state, dry=True, now=NOW)
    r = retention.run(priv, state, now=NOW)
    assert "error" not in r and dry["kinds"]["scratch"] == r["kinds"]["scratch"] == [6, 351]
    assert [p.read_text() for p in (why, *drafts)] == ["the drafts the owner holds\n", "f" * 400, "g" * 300] and notes.exists()
    assert not any(p.exists() or p.is_symlink() for p in (old, *deeper, *linked, folder))
    assert r["kept"] == dry["kept"] == 300 + 27 + 400 + 300                              # the inbox's photo and the KEEP folder
    assert cli.main(["retention", "--dry-run", "--private", str(priv), "--state", str(state)]) == 0
    assert "inbox and KEEP folders: 1 KB, kept" in capsys.readouterr().out


def test_the_morning_line_names_the_rooms_and_top_folders_the_sweep_took(tmp_path):
    """The same morning's diff said "swept: scratch 11 MB" and no more: which room lost which folder is now on the line,
    the biggest first, six top folders at most; a sweep kept before that says its kinds as it did."""
    priv, state, projects, led = layer(tmp_path)
    a, c = (state / "rooms" / name / "work" for name in ("room-a", "room-c"))
    for i in range(11):
        put(a / "offer-2026-10-28" / f"draft-{i}.md", "d" * 100, days=3)
    for i in range(3):
        put(a / "videos" / "reel" / f"frame-{i}.jpg", "v" * 1000, days=3)
    for i in range(31):
        put(c / "voice" / f"note-{i}.ogg", "o" * 10, days=3)
    put(c / "young.txt", "y", days=1)
    assert retention.main(priv, state, now=NOW) == 0
    assert json.loads((state / retention.LAST).read_text())["scratch"] == {
        "room-a": {"offer-2026-10-28": [11, 1100], "videos": [3, 3000]}, "room-c": {"voice": [31, 310]}}
    said = f"{words.PLAIN} swept: scratch 4 KB; scratch: room-a videos (3 files), offer-2026-10-28 (11); room-c voice (31)"
    accounts, cool = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json")
    lines, _ = morning.build(priv, state, accounts, cool, led, {"started": NOW - DAY, "units": []}, NOW + 7200)
    assert retention.line(state, NOW + 60) == ("", said) and said in lines
    many = {f"room-{k}": {f"folder-{k}{j}": [j + 1, 1000 * (k + 1) + j] for j in range(2)} for k in range(4)}   # eight top folders
    (state / retention.LAST).write_text(json.dumps({"at": NOW, "dry": False, "kinds": {"scratch": [12, 9000]}, "scratch": many}))
    assert retention.line(state, NOW + 60) == ("", f"{words.PLAIN} swept: scratch 9 KB; scratch: room-3 folder-31 (2 files), folder-30 (1); "
                                                   "room-2 folder-21 (2), folder-20 (1); room-1 folder-11 (2), folder-10 (1), and 2 more")
    (state / retention.LAST).write_text(json.dumps({"at": NOW, "dry": False, "kinds": {"logs": [1, 2048], "scratch": [11, 11 << 20]}, "inbox": 300}))
    assert retention.line(state, NOW + 60) == ("", f"{words.PLAIN} swept: scratch 11 MB, logs 2 KB")       # as before the rooms
    (state / retention.LAST).write_text(json.dumps({"at": NOW, "dry": False, "kinds": {k: [1, 1 << 30] for k in retention.DAYS}, "inbox": 0}))
    assert words.width(retention.line(state, NOW + 60)[1]) <= words.PHONE                                  # and held to a phone line


def test_a_swept_room_finds_retention_md_in_its_work_folder_and_an_untouched_room_does_not(tmp_path):
    """Nothing the room read said what the sweep took, so it told the owner the drafts were held: the room's own work
    folder now says what went, where it waits and for how long, and how to keep a folder."""
    priv, state, projects, led = layer(tmp_path, "[retention]\nscratch_trash = 3\n")
    room, work = state / "rooms" / "room-a", state / "rooms" / "room-a" / "work"
    put(work / "offer-2026-10-28" / "FIVERR.md", "f" * 100, days=3)
    sent = put(room / "outbox" / "20261018-090000-k1-s1-offer.pdf", "s" * 50, days=3)       # the copy of a file it sent
    put(state / "rooms" / "room-c" / "work" / "young.txt", "y", days=1)                     # nothing past its days
    busy = put(state / "rooms" / "room-b" / "work" / "old.txt", "b", days=5)               # a turn in flight: never swept
    retention.run(priv, state, dry=True, now=NOW)
    assert not list(state.glob("rooms/*/work/RETENTION.md"))                                # a dry run writes none
    retention.run(priv, state, now=NOW)
    note, today = work / "RETENTION.md", retention.day(NOW)
    text = note.read_text()
    assert list(state.glob("rooms/*/work/RETENTION.md")) == [note] and stat.S_IMODE(note.stat().st_mode) == 0o600
    assert f"The sweep of {time.strftime('%a %Y-%m-%d %H:%M', time.localtime(NOW))} moved these" in text
    assert f"\n- offer-2026-10-28/FIVERR.md -> trash/scratch/{today}/rooms/room-a/work/offer-2026-10-28/FIVERR.md\n" in text
    assert f"\n- ../outbox/{sent.name} -> trash/scratch/{today}/rooms/room-a/outbox/{sent.name}\n" in text
    assert "\nThe trash keeps them 3 days after that day" in text and text.endswith("\nA folder under work/ with a file named KEEP is never swept.\n")
    assert (state / "trash" / "scratch" / today / "rooms" / "room-a" / "work" / "offer-2026-10-28" / "FIVERR.md").read_text() == "f" * 100
    before = ago(note, 10).read_text(), note.stat().st_mtime                                # ten days old, and never scratch
    r = retention.run(priv, state, now=NOW + 60)
    assert "scratch" not in r["kinds"] and (note.read_text(), note.stat().st_mtime) == before   # moved nothing: left alone
    put(work / "renders" / "r1.pgm", "r", days=3)
    retention.run(priv, state, now=NOW + DAY)
    assert "- renders/r1.pgm -> " in note.read_text() and "FIVERR" not in note.read_text() and busy.exists()   # rewritten whole
    told = [x for x in context.MECHANICS.read_text().splitlines() if "KEEP" in x]                # and every room is told both
    assert len(told) == 1 and "a folder under work/ with a file named `KEEP` is never swept until its `KEEP` is removed, so" in told[0] and "RETENTION.md at the top of work/" in told[0]


MB = 1_000_000
DAY_ROWS = (("inbound", 100, {"room": "room-a", "state": "answered"}),            # a busy room's day in the ledger: 1,610 rows
            ("turns", 100, {"room": "room-a", "outcome": "ok", "models": "claude-opus-5-5"}),
            ("deliveries", 200, {"room": "room-a", "kind": "reply", "state": "sent"}),
            ("outbound", 200, {"room": "room-a", "kind": "reply", "chars": 900}),
            ("hook_calls", 400, {"room": "room-a", "event": "PostToolUse", "ms": 3.0}),
            ("tool_times", 300, {"room": "room-a", "tool": "Bash", "ms": 410}),
            ("evidence", 300, {"room": "room-a", "tool": "Read", "target": "plans/roof.pdf"}),
            ("shadow", 10, {"room": "room-a", "rule": "figure", "detail": "57 MB"}))
STAMP = {"inbound": "received", "turns": "started", "deliveries": "created", "outbound": "sent"}     # else "t"
KEY = {"inbound": "message_id", "deliveries": "key", "tool_times": "tool_use_id"}


def sparse(path, nbytes, at):
    """A file of `nbytes` that takes no disk (one hole), last written at `at`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(nbytes)
    os.utime(path, (at, at))
    return path


def on_disk(*roots, skip=()):
    """The size of every file under the roots, links never followed, the `skip` folders left out."""
    n = 0
    for root in roots:
        for d, ds, fs in os.walk(root):
            ds[:] = [x for x in ds if Path(d, x) not in skip]
            n += sum(s.st_size for f in fs if stat.S_ISREG((s := Path(d, f).lstat()).st_mode))
    return n


def test_a_busy_month_stays_under_one_fixed_bound_from_day_3_to_day_30(tmp_path):
    """The owner's pass mark (a room should run every day for a month and be as clean as it was on
    day 1): 30 days of a busy room at the shipped defaults, the shape a work room had that day at full size, in
    sparse files: 5 page renders of 60 MB a day in the work folder, 900 MB more in seven minutes on day 10 (one PDF's
    page cuts), 2 sends of 10.6 MB, 40 MB of the person's attachments, 40 KB of stream log and 4 KB assembled, a 10 MB
    transcript of a new session each day, a finished worker every third day, 1,610 ledger rows; the sweep at 04:20 the
    next morning. Everything but the memory tree and work/inbox, measured before and after every sweep, stays under one
    bound fixed before the month starts, while the month writes more than twice that bound. Each worker is a builder and
    leaves what a builder's run builds, at the box's size on Nov 20 (31.8 GB in 130 environments, 11.4 GB of cache folders
    beside them): a 245 MB environment and 88 MB of cache folders in its worktree; on_disk counts a file's length, so sparse."""
    priv, state, projects, led = layer(tmp_path)
    (proc := tmp_path / "proc" / "self" / "fd").mkdir(parents=True)        # a /proc in which nothing holds a file open
    (state / "logs").mkdir()
    room, before = state / "rooms" / "room-a", files(priv)
    work = room / "work"
    t0 = time.mktime((2027, 1, 4, 0, 0, 0, 0, 0, -1))                     # a Monday, a month with no clock change
    RENDER, SEND, INBOX, TRANSCRIPT, WORKER, LOG, ASM, LEDGER = 60 * MB, 10_600_000, 40 * MB, 10 * MB, 3 * MB, 40_000, 4_000, 32 * MB
    DAILY, BURST = 5 * RENDER + 2 * SEND, 15 * RENDER
    BUILT = {".venv/pyvenv.cfg": 200, ".venv/lib/python3.13/site-packages/numpy/_core/_multiarray_umath.so": 245 * MB - 200,
             ".mypy_cache/3.13/yerk_ai/daemon.data.json": 60 * MB, "yerk_ai/__pycache__/daemon.cpython-313.pyc": 20 * MB,
             ".ruff_cache/0.17.1/package-cache": 4 * MB, ".pytest_cache/v/cache/nodeids": 4 * MB}
    BUILD = sum(BUILT.values())                                           # 333 MB a builder
    # The bound, one reason a term:
    # 5 * DAILY: a day's scratch leaves its room by the sweep 3 days on (2 days, and the first sweep past them), and its
    #   folder in trash/scratch/ is deleted 2 sweeps after that (scratch_trash, 1 day): on the disk 5 days at most, wherever it sits.
    # BURST: day 10's page cuts are scratch too, on the disk those 5 days, once in the month.
    # 30 * TRANSCRIPT: a session's transcript keeps 30 days, as long as the month: every one of them.
    # 10 * WORKER: a worker's record and its transcript keep 30 days, as long as the month: all 10.
    # 4 * BUILD: a builder's environment and caches leave its folder at the second sweep after its afternoon (built, 1 day),
    #   and that sweep's trash folder is deleted 8 sweeps on (trash, 7 days): 10 days on the disk, so 4 of the workers of every
    #   third day at once, wherever they sit.
    # 31 * (LOG + ASM): a day's log raw, 14 gzipped and 8 of each in the trash (31, at most raw size each).
    # LEDGER + MB: the ledger's rows, kept 90 days, longer than the month, and one MB for the tables beside them.
    bound = 5 * DAILY + BURST + 30 * TRANSCRIPT + 10 * WORKER + 4 * BUILD + 31 * (LOG + ASM) + LEDGER + MB
    written, seen = 0, []
    for k in range(1, 31):
        day0, sid = t0 + (k - 1) * DAY, f"{k:08x}-0000-4000-8000-{k:012x}"
        for i, h in enumerate((10, 11.5, 13, 14.5, 16)):
            sparse(work / "outbox" / f"day-{k}" / f"render-{i}.pgm", RENDER, day0 + h * 3600)
        for i in range(15 if k == 10 else 0):                             # 900 MB in seven minutes
            sparse(work / "outbox" / "spec-pages" / f"page-{i}.pgm", RENDER, day0 + 11.9 * 3600 + i * 28)
        for i, h in enumerate((12, 15)):
            sparse(room / "outbox" / f"{k:02d}{h}0000-k{k}-s{i}-plan.pdf", SEND, day0 + h * 3600)
        sparse(work / "inbox" / f"m{k}" / "drawing.pdf", INBOX, day0 + 9 * 3600)
        for path, n in ((state / "logs" / "room-a.jsonl", LOG), (room / "assembled.jsonl", ASM)):
            with open(path, "a") as f:
                f.write((json.dumps({"t": day0, "type": "stream_event", "text": "a line the room streamed"}) + "\n") * (n // 80))
            os.utime(path, (day0 + 17 * 3600, day0 + 17 * 3600))
        sparse(project(projects, state, "room-a") / f"{sid}.jsonl", TRANSCRIPT, day0 + 17 * 3600)
        (state / "sessions.json").write_text(json.dumps({"room-a": {"session_id": sid, "account": "one"}}))
        if k % 3 == 0:                                                     # a worker, finished the same afternoon
            folder = sparse(state / "workers" / f"w{k:05d}" / "REPORT.md", 2 * MB, day0 + 15 * 3600).parent
            sparse(projects / project_dir(folder.resolve()) / f"{sid}.jsonl", MB, day0 + 15 * 3600)
            for rel, n in BUILT.items():                                   # what its run built, in its worktree
                sparse(folder / "wt" / rel, n, day0 + 15 * 3600)
            led.worker_add(id=f"w{k:05d}", room="room-a", staging=str(folder), started=day0 + 14 * 3600, finished=day0 + 15 * 3600)
        led.many([(t, {**row, STAMP.get(t, "t"): day0 + 9 * 3600 + i, **({KEY[t]: f"{t}-{k}-{i}"} if t in KEY else {})})
                  for t, n, row in DAY_ROWS for i in range(n)])
        written += DAILY + (BURST if k == 10 else 0) + TRANSCRIPT + (WORKER + BUILD if k % 3 == 0 else 0) + LOG + ASM
        late = on_disk(state, projects, skip={state / "memory", work / "inbox"})       # the day's end, before its sweep
        r = retention.run(priv, state, now=(sweep := day0 + DAY + 4 * 3600 + 20 * 60), proc=proc.parent.parent)
        early = on_disk(state, projects, skip={state / "memory", work / "inbox"})
        seen.append((k, late, early))
        assert "error" not in r, r
        left = retention.scratch(room)                                    # the sweep's own rules, held every morning
        assert sum(s.st_size for _, s in left) <= 500 * MB and all(s.st_mtime >= sweep - 2 * DAY for _, s in left), k
        assert len(list((work / "inbox").glob("m*/drawing.pdf"))) == k and len(list((work / "outbox").iterdir())) <= 3, k
        assert all(d.name >= retention.day(sweep - 7 * DAY) for d in (state / "trash").glob("????-??-??")), k
        assert all(d.name >= retention.day(sweep - DAY) for d in (state / "trash" / "scratch").glob("????-??-??")), k
        assert k < 3 or max(late, early) <= bound, (k, late, early, bound, seen)
    assert written > 2 * bound and files(priv) == before, (written, bound)             # the bound means something; the layer as it was
    assert on_disk(work / "inbox") == 30 * INBOX + 300                                   # every attachment, kept (the fixture's photo too)
