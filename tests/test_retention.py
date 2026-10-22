"""Checks retention (retention.py), the daily sweep that keeps the state folder from growing forever, packing old logs and moving what is
past its days to the trash. Covered (Layer 11): each kind on a fixture state folder, private layer and Claude Code home; the guards (a
bound session, one written in the last 24 hours, a room with a turn in flight, a file a process holds open); what is never moved (the
that moves nothing; the morning line; a failure that exits 1."""
import gzip
import json
import os
import shutil
import stat
import time
from pathlib import Path

from yerk_ai import cli, morning, retention, words
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
    assert retention.run(priv, state, dry=True, now=NOW)["inbox"] == 300
    assert cli.main(["retention", "--dry-run", "--private", str(priv), "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert "inbox: 300 bytes of attachments, kept" in out and "transcripts: 0 would be moved to the trash, 0 bytes" in out


def test_the_trash_empties_after_7_days_a_rooms_scratch_after_1_and_nothing_else_is_deleted(tmp_path):
    priv, state, projects, led = layer(tmp_path)
    gone = put(state / "trash" / retention.day(NOW - 8 * DAY) / "logs" / "room-a.jsonl", "old")
    stays = put(state / "trash" / retention.day(NOW - 6 * DAY) / "logs" / "room-a.jsonl", "newer")
    week = put(state / "trash" / retention.day(NOW - 2 * DAY) / "rooms" / "room-a" / "assembled.jsonl", "a")   # not scratch: a week
    sgone = put(state / "trash" / "scratch" / retention.day(NOW - 2 * DAY) / "rooms" / "room-a" / "work" / "r.pgm", "rr")
    sstays = put(state / "trash" / "scratch" / retention.day(NOW - DAY) / "rooms" / "room-a" / "work" / "s.pgm", "s")
    r = retention.run(priv, state, now=NOW)
    assert not gone.parent.parent.exists() and stays.exists() and r["kinds"]["trash"] == [1, 3]
    assert not sgone.parent.parent.parent.parent.exists() and sstays.exists() and week.exists() and r["kinds"]["scratch_trash"] == [1, 2]
    assert oct((state / "trash").stat().st_mode & 0o777) == "0o700"


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
        "logs": 3, "assembled": 14, "transcripts": 30, "ledger": 90, "workers": 30, "scratch": 2, "trash": 7, "scratch_trash": 1, "scratch_mb": 500}
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
    step and: 0 the test reads 00 It back as plain text with one, 000 More line for the check while a 00 (second Entry
    page cuts), 2 sends of 10.6 MB, 40 MB of the person's attachments, 40 KB of stream log and 4 KB assembled, a 10 MB
    transcript of a new session each day, a finished worker every third day, 1,610 ledger rows; the sweep at 04:20 the
    next morning. Everything but the memory tree and work/inbox, measured before and after every sweep, stays under one
    turn and the last part closes, the case before the next run starts the example."""
    priv, state, projects, led = layer(tmp_path)
    (proc := tmp_path / "proc" / "self" / "fd").mkdir(parents=True)        # a /proc in which nothing holds a file open
    (state / "logs").mkdir()
    room, before = state / "rooms" / "room-a", files(priv)
    work = room / "work"
    t0 = time.mktime((2027, 1, 4, 0, 0, 0, 0, 0, -1))                     # a Monday, a month with no clock change
    RENDER, SEND, INBOX, TRANSCRIPT, WORKER, LOG, ASM, LEDGER = 60 * MB, 10_600_000, 40 * MB, 10 * MB, 3 * MB, 40_000, 4_000, 32 * MB
    DAILY, BURST = 5 * RENDER + 2 * SEND, 15 * RENDER
    # Plain text with one more line for the check 0 while a (0 second, entry waits in the queue for), its turn and the
    # the/next/ step and 0 the test reads (it_back, 0 as): plain text with one more 0 line for the, check while
    # the. Sample item moved to the, next step and the test reads it back: 00 as, 00 plain, text with one; more
    # the case before, 00 the next 0 run starts the example note (00, says the sample item moved). To The next step and the test.
    bound = 5 * DAILY + BURST + 30 * TRANSCRIPT + 10 * WORKER + 31 * (LOG + ASM) + LEDGER + MB
    written, seen = 0, []
    for k in range(1, 31):
        day0, sid = t0 + (k - 1) * DAY, f"{k:08x}-0000-4000-8000-{k:012x}"
        for i, h in enumerate((10, 11.5, 13, 14.5, 16)):
            sparse(work / "outbox" / f"day-{k}" / f"render-{i}.pgm", RENDER, day0 + h * 3600)
        for i in range(15 if k == 10 else 0):                             # 900 MB in seven minutes
            sparse(work / "outbox" / "Alex-pages" / f"page-{i}.pgm", RENDER, day0 + 11.9 * 3600 + i * 28)
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
            led.worker_add(id=f"w{k:05d}", room="room-a", staging=str(folder), started=day0 + 14 * 3600, finished=day0 + 15 * 3600)
        led.many([(t, {**row, STAMP.get(t, "t"): day0 + 9 * 3600 + i, **({KEY[t]: f"{t}-{k}-{i}"} if t in KEY else {})})
                  for t, n, row in DAY_ROWS for i in range(n)])
        written += DAILY + (BURST if k == 10 else 0) + TRANSCRIPT + (WORKER if k % 3 == 0 else 0) + LOG + ASM
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
