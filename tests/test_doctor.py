"""Checks `yerk doctor` (doctor.py), the daily check of the install, which repairs what a crash left and says each
problem in a plain line. Its cases, layer 11: the crash repair of a zeroed line in a day log (backed up, then blank,
every line number held), in an assembled log and a bound transcript (dropped), and of a line whose text outlived its
NULs (kept); a room with a turn in flight left alone; each check a plain line (a rooms.toml key no loader reads, a
tree; a dry run that changes nothing; the morning line; yerkd's repair before any room opens; and every key the
loaders read from a room in config.ROOM_KEYS."""
import json
import re
import shutil
import time
from datetime import date, datetime, timedelta

import pytest

from yerk_ai import config, context, doctor, hooks, memindex, memory, morning, status, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE, ROOM_KEYS, budgets
from yerk_ai.daemon import ConsoleChannel, Yerkd
from yerk_ai.ledger import Ledger

DAY = 86400.0
URL = "http://127.0.0.1:4321/hook"


def day_line(i):
    at = datetime(2026, 10, 20, 11, 10, i).astimezone()
    return memory.format_line(at, "example-room", "", "owner", "", f"message {i}", "private", f"m{i}")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A healthy install: the example layer with a day log in today's shape, a room's assembled log and its bound
    transcript, a mention index newer than its sources, yerkd answering with no room open, the index fresh."""
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    shutil.rmtree(priv / "memory" / "daily")                 # the example's invented logs predate today's line shape
    (day := priv / "memory" / "daily" / "2026-10-20.md").parent.mkdir()
    day.write_text("# 2026-10-20\n\n" + "".join(day_line(i) + "\n" for i in range(5)))
    (state / "rooms" / "example-room" / "work").mkdir(parents=True)
    (assembled := state / "rooms" / "example-room" / "assembled.jsonl").write_text("".join(json.dumps({"n": i}) + "\n" for i in range(4)))
    (sid := tmp_path / "home" / "projects" / "p" / "s1.jsonl").parent.mkdir(parents=True)
    sid.write_text("".join(json.dumps({"turn": i}) + "\n" for i in range(4)))
    (state / "sessions.json").write_text(json.dumps({"example-room": {"session_id": "s1", "transcript": str(sid)}}))
    (state / "memory").mkdir()
    (state / "memory" / "mentions.json").write_text(json.dumps({"built": time.time() + 60}))
    (state / "yerkd.json").write_text(json.dumps({"pid": 1, "port": 4321}))
    monkeypatch.setattr(memindex, "check", lambda db, scope, *a: {"status": "fresh"})
    monkeypatch.setattr(memory, "meaning_state", lambda s: "ok")
    monkeypatch.setattr(status, "alive", lambda s: True)
    monkeypatch.setattr(status, "ask", lambda s, *a: {"rooms": [], "units": [{"name": "yerkd"}]})
    return priv, state, day, assembled, sid


def test_a_healthy_tree_prints_nothing_and_its_run_is_kept(tree, capsys):
    priv, state, *_ = tree
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == ""
    d = json.loads((state / "doctor.json").read_text())
    assert d["findings"] == [] and d["repaired"] == [] and d["sizes"]["state folder"] > 0
    assert doctor.line(state, time.time()) == ("", f"{words.PLAIN} doctor: ok")


def test_a_zeroed_day_log_line_is_backed_up_then_blank_so_every_line_number_holds(tree, capsys):
    """Seen live after a blue screen: one line was a run of NULs, another kept its text with 141 NULs after it."""
    priv, state, day, *_ = tree
    lines = day.read_text().split("\n")
    zeroed = [*lines[:3], "\0" * 120, *lines[4:5], lines[5] + "\0" * 141, *lines[6:]]
    day.write_text("\n".join(zeroed))
    before = day.read_bytes()
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == ["day log 2026-10-20.md: mended lines 4, 6"]
    after = day.read_text().split("\n")
    assert len(after) == len(lines) and after[3] == "" and after[:3] + after[4:] == lines[:3] + lines[4:]
    backup, = (priv / "backups").glob("doctor-*/daily.2026-10-20.md")
    assert backup.read_bytes() == before and backup.stat().st_mode & 0o777 == 0o600
    assert doctor.line(state, time.time())[1] == words.line(f"{words.NOT_DONE} doctor: 1 found, day log 2026-10-20.md: mended lines 4, 6")


def test_a_zeroed_jsonl_line_is_dropped_and_a_line_that_outlived_its_nuls_is_kept(tree, capsys):
    priv, state, _, assembled, sid = tree
    rows = assembled.read_text().splitlines()
    assembled.write_text("\n".join([rows[0], "\0" * 200, rows[1], "\0" * 50 + rows[2], rows[3]]) + "\n")
    sid.write_bytes(sid.read_bytes() + b"\0" * 193)          # the last write zeroed, no newline after it
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == ["example-room assembled log: mended lines 2, 4", "example-room transcript: mended line 5"]
    assert assembled.read_text().splitlines() == rows
    assert sid.read_text() == "".join(json.dumps({"turn": i}) + "\n" for i in range(4))
    assert sorted(p.name for p in (state / "trash").glob("*/doctor/*")) == ["example-room.assembled.jsonl", "p.s1.jsonl"]


def test_the_line_glued_on_after_a_partly_written_one_is_kept_whole(tree, capsys):
    """A long write whose first page outlived the rest, then the next append glued on after its NULs (cold read F1):
    the glued line is whole and is kept, the surviving fragment goes (it is in the backup), in a log and a day log."""
    priv, state, day, assembled, _ = tree
    rows = assembled.read_text().splitlines()
    assembled.write_text("\n".join([rows[0], rows[1][:5] + "\0" * 30 + rows[2], rows[3]]) + "\n")
    lines = day.read_text().split("\n")
    day.write_text("\n".join([*lines[:3], lines[3][:20] + "\0" * 40 + lines[4], *lines[5:]]))
    assert doctor.main(priv, state) == 1
    assert assembled.read_text().splitlines() == [rows[0], rows[2], rows[3]]
    assert day.read_text().split("\n") == [*lines[:3], lines[4], *lines[5:]]


@pytest.mark.parametrize("how", ["waiting in the ledger", "busy on /status"])
def test_a_room_with_a_turn_in_flight_or_a_message_waiting_is_never_mended(tree, capsys, monkeypatch, how):
    priv, state, _, assembled, sid = tree
    for p in (assembled, sid):
        p.write_bytes(p.read_bytes() + b"\0" * 40 + b"\n")
    if how == "busy on /status":
        monkeypatch.setattr(status, "ask", lambda s, *a: {"rooms": [{"room": "example-room", "busy": True}], "units": [{"name": "yerkd"}]})
    else:
        Ledger(state / "ledger.sqlite").inbound_add("m9", "example-room", "discord:1", "owner", 5)   # received, never answered
    before = assembled.read_bytes(), sid.read_bytes()
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == ""
    assert (assembled.read_bytes(), sid.read_bytes()) == before and not (state / "trash").exists()


def test_a_rooms_toml_key_no_loader_reads_is_named_with_its_room(tree, capsys):
    priv, state, *_ = tree
    rooms = priv / "rooms.toml"
    rooms.write_text(rooms.read_text().replace("recall_days = 2", "recall_days = 2\nrecal_days = 3", 1))
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == ["room example-room: rooms.toml key recal_days is read by nothing"]


def test_a_stale_index_an_old_mention_index_and_no_meaning_search_are_each_a_line(tree, capsys, monkeypatch):
    priv, state, *_ = tree
    monkeypatch.setattr(memindex, "check", lambda db, scope, *a: {"status": "stale"})
    monkeypatch.setattr(memory, "meaning_state", lambda s: "no-index")
    (priv / "memory" / "facts").mkdir()
    (priv / "memory" / "facts" / "Alex.md").write_text("# And\n- note says the\n")
    (state / "memory" / "mentions.json").write_text(json.dumps({"built": time.time() - 3600}))    # the next Run.starts
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == ["and the: test", "for the: check while a second entry",
                                                    "as plain: text-with"]


def test_each_other_check_says_one_plain_line(tree, capsys, monkeypatch):
    priv, state, day, *_ = tree
    (priv / "memory" / "note.md").write_text("---\nclass: secret\n---\nbody\n")
    (priv / "memory" / "pinned.md.class").write_text("public\n")
    day.write_text(day.read_text() + "a hand edit\n")
    (tok := priv / "secrets" / "claude-one.token").parent.mkdir()
    tok.write_text(json.dumps({"token": "sk-ant-x", "issued": (date.today() - timedelta(days=355)).isoformat()}))
    tok.chmod(0o644)
    (priv / "accounts.toml").write_text('[accounts]\none = { config_dir = "~/.claude", token = "secrets/claude-one.token" }\n')
    (priv / "yerkd.toml").write_text("[doctor]\ngrowth_mb = 0.000001\n")
    for room, url in (("example-room", "http://127.0.0.1:9/hook"), ("example-quiet", URL)):    # an old port; yerkd's own
        (state / "rooms" / room).mkdir(parents=True, exist_ok=True)
        (state / "rooms" / room / "settings.json").write_text(json.dumps({"hooks": hooks.settings_for(url, "t")}))
    monkeypatch.setattr(status, "ask", lambda s, *a: {"rooms": [], "units": [{"name": n} for n in ("yerkd", "example-room", "example-quiet")]})
    (state / "doctor.json").write_text(json.dumps({"at": time.time() - DAY, "sizes": {"state folder": 0}, "sized": time.time() - DAY}))
    assert doctor.main(priv, state) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[:4] + out[5:6] == ["secret claude-one.token: mode 644, not 600", "note.md: class secret is not one of private, room, shared",
                                  "pinned.md.class: not one of private, room, shared", "day log 2026-10-20.md: line 8 does not parse",
                                  "room example-room: hooks not as yerkd puts them"], out
    assert re.fullmatch(r"token one: (9|10) days left", out[4]) and len(out) == 7, out      # 10 days, less the hours of today
    assert re.fullmatch(r"state folder: grew \d[\d.]* (KB|MB|bytes) in 1 day", out[6]), out


def test_a_dry_run_says_what_it_would_mend_and_changes_nothing(tree, capsys):
    priv, state, day, assembled, sid = tree
    day.write_bytes(day.read_bytes() + b"\0" * 64)
    assembled.write_bytes(b"\0" * 9 + b"\n" + assembled.read_bytes())
    before = {p: p.read_bytes() for p in (day, assembled, sid)}
    assert doctor.main(priv, state, dry=True) == 1
    assert capsys.readouterr().out.splitlines() == ["day log 2026-10-20.md: would mend line 8",
                                                    "example-room assembled log: would mend line 1"]
    assert {p: p.read_bytes() for p in before} == before
    assert not (priv / "backups").exists() and not (state / "trash").exists() and not (state / "doctor.json").exists()


def test_a_doctor_that_cannot_run_says_why_exits_2_and_the_morning_line_says_it(tree, capsys):
    priv, state, *_ = tree
    (priv / "yerkd.toml").write_text("[doctor\n")
    assert doctor.main(priv, state) == 2 and capsys.readouterr().out.startswith("doctor failed: TOMLDecodeError: ")
    assert doctor.line(state, time.time())[1].startswith(f"{words.NOT_DONE} doctor: 1 found, doctor failed")


def test_the_morning_diff_carries_one_doctor_line_and_says_unknown_with_no_run(tree):
    priv, state, day, *_ = tree
    accounts, cool, led = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"), Ledger(state / "ledger.sqlite")
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": time.time() - DAY, "units": []}, time.time())
    assert words.line(f"{words.PLAIN} doctor: not run yet, runs each night") in lines and not [k for k in stamp["findings"] if "doctor" in k]
    day.write_bytes(day.read_bytes() + b"\0" * 9)
    doctor.main(priv, state)
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": time.time() - DAY, "units": []}, time.time())
    assert words.line(f"{words.NOT_DONE} doctor: 1 found, day log 2026-10-20.md: mended line 8") in lines
    assert "doctor day log 2026-10-20.md: mended line 8" in stamp["findings"]
    doctor.main(priv, state)
    assert doctor.line(state, time.time() + DAY + 60)[0] == "unknown doctor"           # a day later: no run since


def test_yerkd_mends_a_zeroed_line_before_any_room_opens_and_logs_the_line(tree, caplog):
    priv, state, day, assembled, _ = tree
    day.write_bytes(day.read_bytes() + b"\0" * 30 + b"\n")
    assembled.write_bytes(b"\0" * 30 + b"\n" + assembled.read_bytes())
    caplog.set_level("INFO", logger="yerk.daemon")
    y = Yerkd(priv, state)
    assert b"\0" not in day.read_bytes() + assembled.read_bytes() and not y.rooms
    d = json.loads((state / "doctor.json").read_text())
    assert [r["line"] for r in d["repaired"]] == ["day log 2026-10-20.md: mended line 8", "example-room assembled log: mended line 1"]
    assert "at" not in d                                     # the checks have not run: the morning line says not run yet
    said = [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("how", ["a zeroed sessions.json", "a list in sessions.json", "a backups folder it cannot write",
                                 "a doctor.json row without its time"])
def test_yerkd_starts_when_its_crash_repair_cannot_run(tree, caplog, how):
    """The start hook never stops yerkd (cold read F2): a crash can zero sessions.json itself, which the session table
    reads as empty; a full disk or a folder it cannot write; a doctor.json another version wrote."""
    priv, state, day, *_ = tree
    if how == "a zeroed sessions.json":
        (state / "sessions.json").write_bytes(b"\0" * 300)
    elif how == "a list in sessions.json":
        (state / "sessions.json").write_text("[]")
    elif how == "a backups folder it cannot write":
        day.write_bytes(day.read_bytes() + b"\0" * 30)
        (priv / "backups").mkdir(mode=0o500)
    else:
        (state / "doctor.json").write_text(json.dumps({"repaired": [{"line": "x"}]}))
    caplog.set_level("INFO", logger="yerk.daemon")
    try:
        y = Yerkd(priv, state)
    finally:
        if (priv / "backups").exists():
            (priv / "backups").chmod(0o700)
    assert not y.rooms and any("doctor: crash repair failed, " in r.getMessage() for r in caplog.records)


class Seen(dict):
    """A room's table from rooms.toml that notes every key a loader reads from it."""
    read: set[str] = set()

    def get(self, k, default=None):
        Seen.read.add(k)
        return super().get(k, default)

    def __getitem__(self, k):
        Seen.read.add(k)
        return super().__getitem__(k)

    def __contains__(self, k):
        Seen.read.add(k)
        return super().__contains__(k)


def test_every_key_a_loader_reads_from_a_room_is_in_room_keys(tmp_path, monkeypatch):
    """`yerk doctor` names a rooms.toml key no loader reads, from config.ROOM_KEYS. Every room of the example layer goes
    through the loaders (the daemon's start, its spec, reader and recall, the room itself, the session start, the
    per-message parts) as a table that notes each key read: a loader that reads a key the set lacks fails here."""
    real = config.load_toml

    def noted(path):
        d = real(path)
        return {**d, "rooms": {n: Seen(c) for n, c in d.get("rooms", {}).items()}} if path.name == "rooms.toml" else d
    monkeypatch.setattr(config, "load_toml", noted)
    Seen.read.clear()
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    for name, conf in y.room_confs.items():
        assert isinstance(conf, Seen)
        y.add_room(name, ConsoleChannel())
        y.spec_for(name), y.reader(name), y.recall_for(name, "starts the Example note Says")
        rules = y.memory.root / memory.RULES
        context.session_start(EXAMPLE_PRIVATE, conf, budgets(), rules=rules)
        context.per_message(name, conf, "owner", datetime.now().astimezone(), None, budgets())
        config.room_settings(conf, tmp_path, name, y.models), memory.room_class(conf), config.room_id(name, conf)
    assert Seen.read - ROOM_KEYS == set(), f"read by a loader, not in config.ROOM_KEYS: {sorted(Seen.read - ROOM_KEYS)}"
    assert len(Seen.read) >= 20, sorted(Seen.read)          # the tables were read through: the test sees the loaders


def test_the_four_index_checks_each_fire_on_their_case_and_are_quiet_when_all_is_well(tree, monkeypatch, tmp_path):
    """2026-10-21: the index lived at a 450M cap with 861,965 vectors (a), its backfill retried one batch the provider
    refused, cycle after cycle (b), two roots named `memory` shared a cite (c), and yerk-index ran older code than yerkd
    for four hours (d). The doctor's dry run said nothing of it; each is now a line, and a well index is quiet."""
    import os
    from dataclasses import replace
    from yerk_ai import retention
    priv, state, *_ = tree
    leaf = "user.slice/app.slice/yerk-index.service"
    (proc := tmp_path / "proc" / str(os.getpid())).mkdir(parents=True)
    (proc / "cgroup").write_text(f"0::/{leaf}\n")
    (cap := tmp_path / "cgroup" / leaf / "memory.high").parent.mkdir(parents=True)
    monkeypatch.setattr(retention, "PROC", tmp_path / "proc")
    monkeypatch.setattr(doctor, "CGROUP", tmp_path / "cgroup")
    monkeypatch.setattr(status, "ask", lambda s, *a: {"rooms": [], "units": [{"name": "yerkd"}], "commit": "b" * 40})

    def index(high, **emb):
        cap.write_text(f"{high}\n")
        (state / "memory" / "index.json").write_text(json.dumps({"pid": os.getpid(), "port": 1, "commit": emb.pop("commit", "b" * 40),
                                                                 "embeddings": {"vectors": 861965, "width": 256, **emb}}))
        return [x for x in doctor.run(priv, state, dry=True) if x.startswith(("memory index", "meaning search", "memory roots"))]
    assert index(1 << 30, paused=False, streak=0) == [] and index("max", paused=True, streak=2) == []
    assert index(450 << 20) == ["memory index: memory capped at 450M, under the 696M its unit's rule gives 861,965 vectors"]
    stuck = 'HTTP 400 from the provider: {\n  "error": {\n    "message": "Invalid \'input\': maximum request size is 300000 tokens'
    assert index("max", paused=True, streak=3, failed=9, last_error=stuck) == [
        "meaning search: backfill paused after 3 failures: " + ' '.join(stuck.split())[:100]]
    assert index("max", paused=True, failed=138, last_error=stuck)[0].startswith("meaning search: backfill paused after 138 failures")   # before streak
    assert index("max", commit="a" * 40) == ["memory index: runs aaaaaaa, yerkd bbbbbbb: restart yerk-index"]
    # (c): two roots named `memory` and no cite of their own each, as every cite was before 2026-10-21
    for n in ("one", "two"):
        (tmp_path / n / "memory").mkdir(parents=True)
    (priv / "yerkd.toml").write_text(f'[memory]\nalso = ["{tmp_path / "one" / "memory"}", "{tmp_path / "two" / "memory"}"]\n')
    assert index("max") == []                                            # each its own cite: quiet
    real = memory.settings
    monkeypatch.setattr(memory, "settings", lambda *a, **k: replace(real(*a, **k), cites=()))
    assert index("max") == ["memory roots: 3 share the cite memory"]
