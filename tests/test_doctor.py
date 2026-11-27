"""Checks `yerk doctor` (doctor.py), the daily check of the install, which repairs what a crash left and says each
problem in a plain line. Its cases, layer 11: the crash repair of a zeroed line in a day log (backed up, then blank,
every line number held), in an assembled log and a bound transcript (dropped), and of a line whose text outlived its
NULs (kept); a room with a turn in flight left alone; each check a plain line (a rooms.toml key no loader reads, a
stale index, a bad class, a token near its end, a secret's mode, hooks not as yerkd puts them, disk growth) and
nothing on a healthy tree; a check that cannot run named apart from every other; a dry run that changes nothing; the
morning line; yerkd's repair before any room opens; and every key the loaders read from a room in config.ROOM_KEYS."""
import gc
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from yerk_ai import config, context, doctor, files, hooks, logins, memindex, memory, morning, pictures, retention, shape, status, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE, ROOM_KEYS, budgets
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
from yerk_ai.ledger import Ledger

DAY = 86400.0
URL = "http://127.0.0.1:4321/hook"
CLEAN = f"{words.DONE} doctor: clean\n"                 # what a run with nothing to say prints


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
    priv.chmod(0o700)                                        # as yerkd makes its state folder and the README says the
    state.chmod(0o700)                                       # private one is kept: nothing in either for another user
    monkeypatch.setattr(memindex, "check", lambda db, scope, *a: {"status": "fresh"})
    monkeypatch.setattr(memory, "meaning_state", lambda s: "ok")
    monkeypatch.setattr(status, "alive", lambda s: True)
    monkeypatch.setattr(status, "ask", lambda s, *a: {"rooms": [], "units": [{"name": "yerkd"}]})
    return priv, state, day, assembled, sid


@pytest.fixture
def collector():
    """The cyclic collector, for a test that turns it off: back on after the test, however it ends."""
    yield gc
    gc.enable()


def test_a_healthy_tree_prints_one_green_line_and_its_run_is_kept(tree, capsys):
    priv, state, *_ = tree
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN
    d = json.loads((state / "doctor.json").read_text())
    assert d["findings"] == [] and d["repaired"] == [] and d["sizes"]["state folder"] > 0
    assert doctor.line(state, time.time()) == ("", f"{words.DONE} doctor: clean")


def test_a_file_another_user_can_read_is_named_and_a_0700_folder_hides_what_is_in_it(tree, capsys, tmp_path, collector):
    """The modes check: under the 0700 state folder a 0644 context file is no one else's to read; opened to the group, the
    folder is named with the count. In a planted tree a user passing through a 0711 folder reads a 0755 folder and a 0644
    file in it; a 0600 file, anything under a 0700 folder and the target of a link are never named. The count a run says
    is the count it leaves: the doctor closes its own ledger connection before the check (its -wal and -shm files, at the
    umask's mode, exist until then), never leaving it to the collector, whose pass could fall anywhere (3.13, 2026-10-21)."""
    priv, state, *_ = tree
    fresh = config.state_dir(str(tmp_path / "fresh" / "yerk"))
    assert oct(fresh.stat().st_mode & 0o777) == "0o700"     # as the first start makes it
    fresh.chmod(0o775)                                       # an install from before: the next start closes it, no deploy step
    assert oct(config.state_dir(str(fresh)).stat().st_mode & 0o777) == "0o700"
    (ctx := state / "rooms" / "example-room" / "context.md").write_text("the owner's lines")
    ctx.chmod(0o644)
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN
    state.chmod(0o750)
    collector.collect()                                      # whatever an earlier run left to the collector is gone now,
    seen = doctor.exposed(state)                             # and nothing the next run leaves to it goes before its check
    collector.disable()
    assert doctor.main(priv, state) == 1 and ("rooms/example-room/context.md", 0o644) in seen
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} state folder: other users can read {len(seen)} of its files and "
                                                    f"folders, first the folder itself (750); `chmod 700 {state}`"]
    assert doctor.exposed(state) == seen and not [p for p, _ in seen if p.startswith("ledger.sqlite-")]
    t = tmp_path / "t"
    (t / "open" / "shut").mkdir(parents=True)
    for name, mode in (("open/a", 0o644), ("open/b", 0o600), ("open/shut/c", 0o644)):
        (t / name).write_text("x")
        (t / name).chmod(mode)
    (t / "open" / "link").symlink_to("/etc/passwd")
    for d, mode in ((t / "open" / "shut", 0o700), (t, 0o711), (t / "open", 0o755)):
        d.chmod(mode)
    assert doctor.exposed(t) == [("open", 0o755), ("open/a", 0o644)]


def test_a_secret_shaped_line_in_memory_or_an_import_is_named_by_file_and_line_never_by_value(tree, capsys):
    """Claim 4's scan: each shape planted once (a key, a bot token, a one-time code, a password line, a token line) across
    a day log, a fact file and a file an import saved is counted and the first named by file:line and kind, the values
    nowhere in what the doctor says; a password that is only "wrong" and an area code are not secrets. The code, on a
    fact file's undated line, is its own orange line; the day log's spent code (a month old) is none."""
    priv, state, day, *_ = tree
    key, bot = "sk-ant-" + "a" * 24, "M" + "a" * 24 + "." + "b" * 6 + "." + "c" * 27
    said = ["your verification code is 482913", "the example wifi password is Exa-mple9!", "the password is wrong; area code 416"]
    at = datetime(2026, 10, 20, 11, 20).astimezone()
    planted = {"attachments/discord-1/note.txt": "token: " + "abcd1234" * 3, "imports/mail/2026-10.md": "# mail\n\n- api key: " + "q" * 20,
               "memory/facts/example-thing.md": f"# example thing\n\n- the example key {key}\n- the bot's {bot}\n- {said[0]}\n",
               "memory/daily/2026-10-20.md": day.read_text() + "".join(
                   memory.format_line(at, "example-room", "", "owner", "", t, "private", f"s{i}") + "\n" for i, t in enumerate(said))}
    for rel, text in planted.items():
        (priv / rel).parent.mkdir(parents=True, exist_ok=True)
        (priv / rel).write_text(text)
    assert doctor.main(priv, state) == 1
    out = capsys.readouterr().out
    assert out.splitlines() == [f"{words.NOT_DONE} secret-shaped lines: 5 in 4 files, first attachments/discord-1/note.txt:1 (token line); "
                                "`yerk doctor --scrub`, then rotate the key",
                                f"{words.WORKING} one-time codes: 1 in 1 file, first memory/facts/example-thing.md:5; a code works "
                                "once and soon expires, so there is nothing to change, and `yerk doctor --scrub` takes it out if you want it gone"]
    assert not [v for v in (key, bot, "482913", "Exa-mple9!", "abcd1234", "q" * 20) if v in out]
    assert {k for k, rx in doctor.SECRETS.items() for t in planted.values() if rx.search(t)} == set(doctor.SECRETS)


def test_a_one_time_code_in_an_import_two_days_past_its_own_lines_date_is_dead_and_not_said(tree, capsys):
    """PR 66's cold read: 194 of the live tree's 213 secret-shaped lines were verification codes in old mail. Under
    imports/, a code on a line more than two days past its own date (its entry's stamp, else its month file's end) is
    dead and not said; a fresh one is, so is one with no date, and a dead code's line still says the password it holds."""
    priv, state, *_ = tree
    code, fresh = "your verification code is 482913", datetime.now() - timedelta(hours=1)
    planted = {"imports/mail/2019-02.md": f"# Mail 2019-02\n\n  {code}\n- 2019-02-14 09:12 From: Example Bank | Subject: sign in\n"
                                          f"  {code}\n- 2019-02-15 10:00 From: Example Shop | Subject: login code 551234, the password is Exa-mple9!\n",
               "imports/texts/example-bank.md": f"# example bank\n\n- 2019-02-14 09:12 bank: {code}\n",
               f"imports/mail/{fresh:%Y-%m}.md": f"# Mail {fresh:%Y-%m}\n\n- {fresh:%Y-%m-%d %H:%M} From: Example Bank | Subject: sign in\n  {code}\n",
               "imports/mail/undated.md": f"# Mail undated\n\n- undated From: Example Bank | Subject: sign in\n  {code}\n"}
    for rel, text in planted.items():
        (priv / rel).parent.mkdir(parents=True, exist_ok=True)
        (priv / rel).write_text(text)
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} secret-shaped lines: 1 in 1 file, first imports/mail/2019-02.md:6 (password line); "
                                                    "`yerk doctor --scrub`, then rotate the key", f"{words.WORKING} " + words.ONE_TIME_CODES.format(
                                                        n=2, files="2 files", first=f"imports/mail/{fresh:%Y-%m}.md:4")]
    assert doctor.scan(priv / f"imports/mail/{fresh:%Y-%m}.md", time.time()) == [(4, "one-time code")]
    assert doctor.scan(priv / "imports/mail/2019-02.md", time.time()) == [(6, "password line")]
    assert doctor.scan(priv / "imports/mail/2019-02.md") == [(3, "one-time code"), (5, "one-time code"), (6, "password line")]   # a code beside a password: the password's


REFERENCE = "- 2 payments made, card paid, confirmation code starting Q4821"      # a reference's tail after a cue word
CUED = [f"the {w} {k} is 482913" for w in ("verification", "security", "one-time", "one time", "login", "sign-in", "sign in", "2fa",
                                           "confirmation") for k in ("code", "pin")] + [
    "your confirmation code is 482913", "verification code: 482913", "security code 4821", "otp: 48213", "passcode 4821",
    "your code is 482913", "your pin is 4821"]


def plant(priv: Path, **files: str) -> None:
    """Each file given (memory__facts__example_thing = text) at its place in the private folder, memory/facts/example-thing.md."""
    for rel, text in files.items():
        (f := priv / (rel.replace("__", "/").replace("_", "-") + ".md")).parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)


def test_a_payment_reference_after_a_cue_is_no_code_and_a_scrub_leaves_its_file_byte_for_byte(tree, capsys):
    """2026-11-14 on: a fact file's undated payment line, "confirmation code starting" and then a reference cut off after
    a letter and four digits, was red every day, the window after the cue word taking the letter and the digits after it
    read as the code. Its digits are no whole word, so no hit; the scrub leaves its file, and its line beside a key and a
    code in another file, as they were, byte for byte."""
    priv, state, *_ = tree
    key = "sk-ant-" + "a" * 24
    plant(priv, memory__facts__example_card="# example card\n\n" + REFERENCE + "\n",
          memory__facts__example_mixed=f"# example mixed\n\n{REFERENCE}\n- the example key {key}\n- {CUED[0]}\n")
    card, mixed = priv / "memory/facts/example-card.md", priv / "memory/facts/example-mixed.md"
    was = card.read_bytes()
    assert doctor.scan(card, time.time()) == [] and doctor.scan(card) == []
    assert doctor.scan(mixed, time.time()) == [(4, "key"), (5, "one-time code")]
    assert doctor.main(priv, state, scrubbing=True) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith(f"{words.DONE} memory/facts/example-mixed.md: scrubbed lines 4, 5 (key, one-time code)")
    assert card.read_bytes() == was
    assert mixed.read_bytes() == f"# example mixed\n\n{REFERENCE}\n- the example key [scrubbed]\n- the verification code is [scrubbed]\n".encode()


def test_every_cue_still_hits_with_a_number_that_stands_alone(tree):
    priv, *_ = tree
    plant(priv, memory__facts__example_codes="".join(f"- {x}\n" for x in CUED))
    assert doctor.scan(priv / "memory/facts/example-codes.md", time.time()) == [(n, "one-time code") for n in range(1, len(CUED) + 1)]


def test_digits_with_a_letter_or_a_digit_directly_before_them_are_no_code(tree):
    priv, *_ = tree
    tails = ["confirmation code starting Q4821", "the security code A1234567", "a login code x48213 was sent",
             "verification code 123456789", "your code is 1234567890", "otp 2fa9999"]
    plant(priv, memory__facts__example_refs="".join(f"- {x}\n" for x in tails))
    assert doctor.scan(priv / "memory/facts/example-refs.md", time.time()) == []
    assert [doctor.SECRETS["one-time code"].search(x)["v"] for x in ("your code is Q 4821", "verification code: (482913)")] == ["4821", "482913"]


def test_a_code_in_the_memory_root_is_dead_two_days_past_its_own_date_and_said_with_none(tree, capsys):
    """A spent code is no hit wherever it sits: a day log's line is dated by its day (daily/YYYY-MM-DD.md), an entry
    anywhere by its own `- YYYY-MM-DD HH:MM`, the lines under it sharing it; an undated line is still said."""
    priv, state, *_ = tree
    line = memory.format_line(datetime(2026, 11, 16, 9, 30).astimezone(), "example-room", "", "owner", "", CUED[0], "private", "s1")
    old, new = datetime.now() - timedelta(days=4), datetime.now() - timedelta(days=1)    # on the clock: four, so a clock change
    plant(priv, **{f"memory__daily__{d:%Y_%m_%d}": f"# {d:%Y-%m-%d}\n\n{line}\n" for d in (old, new)})   # never brings it inside two
    assert doctor.main(priv, state) == 1                                         # days; three exactly with a fixed now below
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} " + words.ONE_TIME_CODES.format(
        n=1, files="1 file", first=f"memory/daily/{new:%Y-%m-%d}.md:3")]
    now = datetime(2026, 11, 19, 12, 0).timestamp()
    plant(priv, memory__daily__2026_11_16=f"# 2026-11-16\n\n{line}\n", memory__daily__2026_11_18=f"# 2026-11-18\n\n{line}\n",
          memory__notes__example_log=f"# example log\n\n- 2026-11-16 09:30 sign in\n  {CUED[0]}\n- 2026-11-18 09:30 sign in\n  {CUED[0]}\n",
          memory__facts__example_bare=f"# example bare\n\n- {CUED[0]}\n")
    scans = {p: doctor.scan(priv / f"memory/{p}.md", now) for p in ("daily/2026-11-16", "daily/2026-11-18", "notes/example-log", "facts/example-bare")}
    assert scans == {"daily/2026-11-16": [], "daily/2026-11-18": [(3, "one-time code")], "notes/example-log": [(6, "one-time code")],
                     "facts/example-bare": [(3, "one-time code")]}


def test_an_undated_code_in_a_fact_file_is_its_own_orange_line_that_never_says_rotate(tree, capsys):
    priv, state, *_ = tree
    plant(priv, memory__facts__example_login=f"# example login\n\n- {CUED[1]}\n")
    assert doctor.main(priv, state) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [f"{words.WORKING} one-time codes: 1 in 1 file, first memory/facts/example-login.md:3; a code works once and soon "
                   "expires, so there is nothing to change, and `yerk doctor --scrub` takes it out if you want it gone"]
    assert "rotate" not in out[0] and "482913" not in out[0]


def test_a_key_beside_a_code_gives_two_lines_the_red_one_worded_as_today(tree, capsys):
    priv, state, *_ = tree
    plant(priv, memory__facts__example_both=f"# example both\n\n- {CUED[2]}\n- the example key sk-ant-{'b' * 24}\n",
          memory__facts__example_pass="# example pass\n\n- the example wifi password is Exa-mple9! and its login code 551234\n")
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{words.NOT_DONE} secret-shaped lines: 2 in 2 files, first memory/facts/example-both.md:4 (key); `yerk doctor --scrub`, then rotate the key",
        f"{words.WORKING} " + words.ONE_TIME_CODES.format(n=1, files="1 file", first="memory/facts/example-both.md:3")]


def test_the_red_line_comes_alone_when_no_code_is_found(tree, capsys):
    priv, state, *_ = tree
    plant(priv, memory__facts__example_key=f"# example key\n\n- the example key sk-ant-{'c' * 24}\n- {REFERENCE[2:]}\n")
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{words.NOT_DONE} secret-shaped lines: 1 in 1 file, first memory/facts/example-key.md:3 (key); `yerk doctor --scrub`, then rotate the key"]


def test_the_doctors_key_shapes_are_the_scrub_tests(tree):
    from tests.test_scrub import PATTERNS
    assert PATTERNS["secret"].pattern == doctor.SECRETS["key"].pattern


def test_a_zeroed_day_log_line_is_backed_up_then_blank_so_every_line_number_holds(tree, capsys):
    """Seen live after a blue screen: one line was a run of NULs, another kept its text with 141 NULs after it."""
    priv, state, day, *_ = tree
    lines = day.read_text().split("\n")
    zeroed = [*lines[:3], "\0" * 120, *lines[4:5], lines[5] + "\0" * 141, *lines[6:]]
    day.write_text("\n".join(zeroed))
    before = day.read_bytes()
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} day log 2026-10-20.md: mended lines 4, 6"]
    after = day.read_text().split("\n")
    assert len(after) == len(lines) and after[3] == "" and after[:3] + after[4:] == lines[:3] + lines[4:]
    backup, = (priv / "backups").glob("doctor-*/daily.2026-10-20.md")
    assert backup.read_bytes() == before and backup.stat().st_mode & 0o777 == 0o600
    assert doctor.line(state, time.time())[1] == words.line(f"{words.WORKING} doctor: 1 found, day log 2026-10-20.md: mended lines 4, 6")


def test_a_zeroed_jsonl_line_is_dropped_and_a_line_that_outlived_its_nuls_is_kept(tree, capsys):
    priv, state, _, assembled, sid = tree
    rows = assembled.read_text().splitlines()
    assembled.write_text("\n".join([rows[0], "\0" * 200, rows[1], "\0" * 50 + rows[2], rows[3]]) + "\n")
    sid.write_bytes(sid.read_bytes() + b"\0" * 193)          # the last write zeroed, no newline after it
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} example-room assembled log: mended lines 2, 4", f"{words.WORKING} example-room transcript: mended line 5"]
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
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN
    assert (assembled.read_bytes(), sid.read_bytes()) == before and not (state / "trash").exists()


def test_a_rooms_toml_key_no_loader_reads_is_named_with_its_room(tree, capsys):
    priv, state, *_ = tree
    rooms = priv / "rooms.toml"
    rooms.write_text(rooms.read_text().replace("recall_days = 2", "recall_days = 2\nrecal_days = 3", 1))
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} room example-room: rooms.toml key recal_days is read by nothing; fix its name or delete it in rooms.toml"]


def test_a_rooms_toml_key_an_installed_add_on_reads_is_a_known_key(tree, capsys):
    """addons-1: hello's manifest names hello_name among its room_keys, so while it is installed the key is read by it."""
    priv, state, *_ = tree
    rooms = priv / "rooms.toml"
    rooms.write_text(rooms.read_text().replace("recall_days = 2", 'recall_days = 2\nhello_name = "Nora"', 1))
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} room example-room: rooms.toml key hello_name is read by nothing; fix its name or delete it in rooms.toml"]
    (state / "addons" / "hello").mkdir(parents=True)
    shutil.copy(Path(__file__).resolve().parents[1] / "examples" / "addons" / "hello" / "addon.toml", state / "addons" / "hello")
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN


def test_the_files_a_moved_room_brought_with_no_sidecar_are_one_orange_line_with_the_two_backfills(tree, capsys):
    """attachments-root (2026-10-24): an owner room's <layer>/NAME-attachments holding pictures or documents with no sidecar
    is one line with the counts and the two backfills, which says to list the folder first while it is not; quiet once
    each has its sidecar."""
    priv, state, *_ = tree
    (f := priv / "example-room-attachments").mkdir()
    for name in ("1-shot.png", "2-shot.jpg", "3-plan.pdf", "4-clip.mov"):
        (f / name).write_bytes(b"\0")
    f = f.resolve()
    said = (f"{words.WORKING} example-room: 2 picture(s) and 1 workbook(s) or PDF(s) moved in with it, in {f}, have no sidecar, so "
            f"no recall finds what is in them: yerk pictures backfill example-room --root {f}; yerk files backfill example-room --root {f}")
    assert doctor.main(priv, state) == 1 and capsys.readouterr().out.splitlines() == [said + words.MOVED_UNLISTED]
    rooms = priv / "rooms.toml"
    rooms.write_text(rooms.read_text().replace("recall_days = 2", 'recall_days = 2\nalso = ["example-room-attachments"]', 1))
    (priv / "yerkd.toml").write_text('[memory]\nalso = ["example-room-attachments"]\n')
    assert doctor.main(priv, state) == 1 and capsys.readouterr().out.splitlines() == [said]
    for side in (pictures.sidecar(f / "1-shot.png"), pictures.sidecar(f / "2-shot.jpg"), files.sidecar(f / "3-plan.pdf")):
        side.write_text("filed\n")
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN


def test_a_cron_line_left_in_the_jobs_file_is_one_line_with_the_calendar_line_that_replaces_it(tree, capsys):
    """shrink-1: `schedule` is a systemd calendar line; the doctor never rewrites the owner's file, it says the swap."""
    priv, state, *_ = tree
    (priv / "jobs.toml").write_text('[jobs.example-weekly]\nkind = "note"\nschedule = "0 10 * * 1"\ntz = "UTC"\n'
                                    'room = "example-room"\nmessage = "the example check"\n')
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f'{words.NOT_DONE} job example-weekly: `schedule` takes a systemd calendar line now, not cron: '
                                                    'replace schedule = "0 10 * * 1" with schedule = "Mon *-*-* 10:0:00"']
    assert "0 10 * * 1" in (priv / "jobs.toml").read_text()


def test_a_stale_index_an_old_mention_index_and_no_meaning_search_are_each_a_line(tree, capsys, monkeypatch):
    priv, state, *_ = tree
    monkeypatch.setattr(memindex, "check", lambda db, scope, *a: {"status": "stale"})
    monkeypatch.setattr(memory, "meaning_state", lambda s: "no-index")
    (priv / "memory" / "facts").mkdir()
    (priv / "memory" / "facts" / "nora.md").write_text("# Nora\n- the club treasurer\n")
    (state / "memory" / "mentions.json").write_text(json.dumps({"built": time.time() - 3600}))    # older than nora.md
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} memory index: stale; {doctor.INDEX}", f"{words.WORKING} mention index: older than a fact file; {doctor.INDEX}",
                                                    f"{words.WORKING} meaning search: no-index; {doctor.INDEX}"]


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
    assert out[:4] + out[5:6] == [f"{words.NOT_DONE} secret claude-one.token: mode 644, not 600; `chmod 600 {tok}`",
                                  f"{words.WORKING} note.md: class secret is not one of private, room, shared; set its class: to one",
                                  f"{words.WORKING} pinned.md.class: not one of private, room, shared; write one of them in it",
                                  f"{words.WORKING} day log 2026-10-20.md: line 8 does not parse; rewrite it in the day-log line shape",
                                  f"{words.NOT_DONE} room example-room: hooks not as yerkd puts them; `systemctl --user restart yerkd` writes them again"], out
    assert re.fullmatch(rf"{words.WORKING} token one: (9|10) days left; `yerk accounts mint one`", out[4]) and len(out) == 7, out      # 10 days, less the hours of today
    assert re.fullmatch(rf"{words.WORKING} state folder: grew \d[\d.]* (KB|MB|bytes) in 1 day; `yerk retention --dry-run` says what the sweep would move", out[6]), out


def test_an_install_that_emails_with_no_gmail_name_is_one_orange_line_with_the_line_to_add(tree, capsys):
    """need-2026-11-16-102, the wall: with no [gmail] name every email went under the bare address, the owner's own mail
    under the owner's name, and nothing said so. An install that emails with none set is one orange line naming the line
    to add; one with a name, or with no Gmail to send from, says nothing of it."""
    priv, state, *_ = tree
    (pw := priv / "secrets" / "gmail.password").parent.mkdir()
    pw.write_text("abcd efgh ijkl mnop\n")
    pw.chmod(0o600)
    gmail = f'[gmail]\nuser = "{"@".join(["owner", "example.test"])}"\npassword_file = "secrets/gmail.password"\n'
    (priv / "yerkd.toml").write_text(gmail)
    assert doctor.main(priv, state) == 1 and capsys.readouterr().out == f"{words.WORKING} {words.EMAIL_NAME_MISSING}\n"
    assert 'set name = "<your name>" under [gmail] in yerkd.toml' in words.EMAIL_NAME_MISSING
    for quiet in (gmail + 'name = "Example Owner"\n', gmail.replace('password_file = "secrets/gmail.password"\n', ""), ""):
        (priv / "yerkd.toml").write_text(quiet)
        assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN, quiet


def test_the_folder_check_and_the_rule_check_name_themselves_apart_when_neither_can_run(tree):
    """`layout` is the private folder's check and `rule shape` the rule files'; both were once read as "shape"."""
    priv, state, *_ = tree
    (priv / "rooms.toml").write_text("[rooms\n")
    said = [x[2:].split(",")[0] for x in doctor.run(priv, state) if "not checked" in x]
    assert said == ["layout: not checked", "jobs: not checked", "rule shape: not checked", "fit: not checked", "moved in: not checked"], said


def lesson_lines(priv, state) -> list[str]:
    """The doctor's lesson-shape lines for the store as it is (shape.store_findings), each orange, in the order printed."""
    from yerk_ai.config import load_toml, models, rooms
    m, limits = memory.settings(priv, state), budgets()["shape"]
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    read = shape.pages(shape.run_by(rooms(priv), state, models(conf)))      # the pages of every model a room runs, as the doctor reads
    return [f"{words.WORKING} {x}" for x in shape.store_findings(shape.store(m, limits, state / "docs", read), limits, state / "docs")]


def test_a_part_past_its_line_reads_red_with_the_overflow_and_one_that_fits_reads_green(tree, capsys):
    """2026-10-22: the shared rules file sat over its line for a day and every session start cut rules from its middle,
    said to no one. Every room's parts are measured as session start builds them; a part past its line is one finding per
    file, with the overflow in characters and the rooms it is cut in."""
    priv, state, *_ = tree
    rules, cap = priv / "memory" / memory.RULES, budgets()["session_start"]["appended"]["rules"]
    rules.write_text("".join(f"- example job {i}: done the example way, step by step, since it works.\n" for i in range(300)))
    raw = context.chars(context.rules_text({}, rules)[0])
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [*lesson_lines(priv, state),          # 300 lines: past the index's 200 too
                                                    f"{words.NOT_DONE} rules.md: rules over its line by {raw - cap:,} characters ({raw:,} of {cap:,}), "
                                                    "cut at every session start in example-room, example-quiet; raise its line in "
                                                    "budgets.toml or trim the file"]
    assert doctor.line(state, time.time())[1].startswith(f"{words.NOT_DONE} ")
    rules.write_text("- example job: done the example way, since it works.\n")
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN


def test_the_fit_check_reads_the_topic_rules_and_an_overflow_not_moved_yet(tree, capsys):
    """rules-on-demand: the fit check reads rules-topics.md where it read the overflow. A topic rule longer than the per-message
    rules part, or kept to no room on file, never loads: orange, with the count and the first; an overflow yerkd has not moved
    yet: orange, until its next start moves it."""
    priv, state, *_ = tree
    tier, most = priv / "memory" / memory.TOPIC_RULES, budgets()["per_message"]["on_hit"]["rules"]
    fine, at = "- example job 1: done the example way, since it works. (example-room, 2026-10-22 16:54:00-04:00) [m:m-1]", "(example-room, 2026-10-22 16:54:00-04:00)"
    tier.write_text("\n".join([*memory.TOPIC_HEAD, fine, f"- example job 2: done the example way, since it works. [rooms: example-gone] {at} [m:m-2]",
                               f"- example job 3: done the {'example ' * (most // 8)}way, since it works. {at} [m:m-3]"]) + "\n")
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [*lesson_lines(priv, state),          # job 3: one sentence past the lesson shape
                                                    f"{words.WORKING} rules-topics.md: 2 topic rules never load, first \"example job 2\": "
                                                    f"longer than the per-message rules part's {most:,} or kept to no room in rooms.toml; "
                                                    "shorten it or fix its [rooms: ...]"]
    tier.write_text("\n".join([*memory.TOPIC_HEAD, fine]) + "\n")
    (held := priv / "memory" / memory.OVERFLOW).write_text(f"# Rules held: past the rules file's cap, in no room\n\n{fine}\n")
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} rules-overflow.md: 1 rule in no room yet; yerkd moves it to "
                                                    "rules-topics.md at its next start"]
    held.unlink()
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN


def test_a_dry_run_says_what_it_would_mend_and_changes_nothing(tree, capsys):
    priv, state, day, assembled, sid = tree
    day.write_bytes(day.read_bytes() + b"\0" * 64)
    assembled.write_bytes(b"\0" * 9 + b"\n" + assembled.read_bytes())
    before = {p: p.read_bytes() for p in (day, assembled, sid)}
    assert doctor.main(priv, state, dry=True) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} day log 2026-10-20.md: would mend line 8",
                                                    f"{words.WORKING} example-room assembled log: would mend line 1"]
    assert {p: p.read_bytes() for p in before} == before
    assert not (priv / "backups").exists() and not (state / "trash").exists() and not (state / "doctor.json").exists()


def test_a_doctor_that_cannot_run_says_why_exits_2_and_the_morning_line_says_it(tree, capsys):
    priv, state, *_ = tree
    (priv / "yerkd.toml").write_text("[doctor\n")
    assert doctor.main(priv, state) == 2 and capsys.readouterr().out.startswith(f"{words.NOT_DONE} doctor failed: TOMLDecodeError: ")
    assert doctor.line(state, time.time())[1].startswith(f"{words.NOT_DONE} doctor: 1 found, doctor failed")


def test_the_morning_diff_carries_one_doctor_line_and_says_unknown_with_no_run(tree):
    priv, state, day, *_ = tree
    accounts, cool, led = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"), Ledger(state / "ledger.sqlite")
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": time.time() - DAY, "units": []}, time.time())
    assert words.line(f"{words.PLAIN} doctor: not run yet, runs each night") in lines and not [k for k in stamp["findings"] if "doctor" in k]
    day.write_bytes(day.read_bytes() + b"\0" * 9)
    doctor.main(priv, state)
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": time.time() - DAY, "units": []}, time.time())
    assert words.line(f"{words.WORKING} doctor: 1 found, day log 2026-10-20.md: mended line 8") in lines
    assert "doctor day log 2026-10-20.md: mended line 8" in stamp["findings"]
    doctor.main(priv, state)
    assert doctor.line(state, time.time() + DAY + 60)[0] == "unknown doctor"           # a day later: no run since


def test_a_doctor_that_never_ran_is_a_plain_line_and_one_that_ran_then_missed_a_day_stays_red(tmp_path):
    """A new install's first start (no full run in doctor.json, with its crash repair's rows or with none) reads not run
    yet with the plain mark, never the red one; once a run is on file, a day with none is the red unknown line, word for word."""
    state, now = tmp_path / "state", time.time()
    state.mkdir()
    red = ("unknown doctor", words.line(f"{words.NOT_DONE} doctor: unknown, no run in 24h"))
    plain = ("", words.line(f"{words.PLAIN} doctor: not run yet, runs each night"))
    assert doctor.line(state, now) == plain and not plain[1].startswith(words.NOT_DONE)      # no doctor.json at all
    doctor.save(state, now, [f"{words.WORKING} day log 2026-10-20.md: mended line 8"])        # the start's crash repair alone
    assert doctor.line(state, now) == doctor.line(state, now + 2 * DAY) == plain
    doctor.save(state, now, [], {"at": now, "findings": []})                                 # a full run
    assert doctor.line(state, now + DAY - 60)[0].startswith("doctor ") and doctor.line(state, now + DAY + 60) == red
    doctor.save(state, now + DAY + 60, [], {"at": now + DAY + 60, "findings": []})
    assert doctor.line(state, now + DAY + 120) == ("", words.line(f"{words.DONE} {words.DOCTOR_OK}")) != plain
    assert doctor.line(state, now + 3 * DAY) == red                                          # and every missed day after


def test_yerkd_mends_a_zeroed_line_before_any_room_opens_and_logs_the_line(tree, caplog):
    priv, state, day, assembled, _ = tree
    day.write_bytes(day.read_bytes() + b"\0" * 30 + b"\n")
    assembled.write_bytes(b"\0" * 30 + b"\n" + assembled.read_bytes())
    caplog.set_level("INFO", logger="yerk.daemon")
    y = Yerkd(priv, state)
    assert b"\0" not in day.read_bytes() + assembled.read_bytes() and not y.rooms
    d = json.loads((state / "doctor.json").read_text())
    assert [r["line"] for r in d["repaired"]] == [f"{words.WORKING} day log 2026-10-20.md: mended line 8", f"{words.WORKING} example-room assembled log: mended line 1"]
    assert "at" not in d                                     # the checks have not run: the morning line says not run yet
    said = [r.getMessage() for r in caplog.records]
    assert f"{words.PLAIN} doctor: not run yet, runs each night" in said and not [x for x in said if words.NO_RUN in x or "curate" in x]


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
        y.spec_for(name), y.reader(name), y.recall_for(name, "lunch with Nora on Monday")
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

    def index(high, top=None, **emb):
        cap.write_text(f"{high}\n")
        (state / "memory" / "index.json").write_text(json.dumps({"pid": os.getpid(), "port": 1, "commit": emb.pop("commit", "b" * 40),
                                                                 "embeddings": {"vectors": 861965, "width": 256, **emb}, **(top or {})}))
        return [x for x in doctor.run(priv, state, dry=True) if x[2:].startswith(("memory index", "meaning search", "memory roots"))]
    CAP = "raise MemoryHigh with `systemctl --user edit yerk-index`"
    assert index(1 << 30, paused=False, streak=0) == [] and index("max", paused=True, streak=2) == []
    assert index(450 << 20) == [f"{words.WORKING} memory index: memory capped at 450M, under the 696M its unit's rule gives 861,965 vectors; {CAP}"]
    # recall-warm (2026-10-22): the keyword tables' pages the index holds mapped count too
    held = {"pin": {"bytes": 414 << 20}}
    assert index(1 << 30, held) == [f"{words.WORKING} memory index: memory capped at 1024M, under the 1110M its unit's rule gives 861,965 vectors "
                                    f"and 414M of pinned keyword pages; {CAP}"]
    assert index(1400 << 20, held) == []
    stuck = 'HTTP 400 from the provider: {\n  "error": {\n    "message": "Invalid \'input\': maximum request size is 300000 tokens'
    assert index("max", paused=True, streak=3, failed=9, last_error=stuck) == [
        f"{words.WORKING} meaning search: backfill paused after 3 failures: " + ' '.join(stuck.split())[:100] + f"; {doctor.INDEX} once the cause is fixed"]
    assert index("max", paused=True, failed=138, last_error=stuck)[0].startswith(f"{words.WORKING} meaning search: backfill paused after 138 failures")   # before streak
    assert index("max", commit="a" * 40) == [f"{words.WORKING} memory index: runs aaaaaaa, yerkd bbbbbbb; {doctor.INDEX}"]
    # (c): two roots named `memory` and no cite of their own each, as every cite was before 2026-10-21
    for n in ("one", "two"):
        (tmp_path / n / "memory").mkdir(parents=True)
    (priv / "yerkd.toml").write_text(f'[memory]\nalso = ["{tmp_path / "one" / "memory"}", "{tmp_path / "two" / "memory"}"]\n')
    assert index("max") == []                                            # each its own cite: quiet
    real = memory.settings
    monkeypatch.setattr(memory, "settings", lambda *a, **k: replace(real(*a, **k), cites=()))
    assert index("max") == [f"{words.WORKING} memory roots: 2 share the cite inbox; rename one of the folders",
                              f"{words.WORKING} memory roots: 3 share the cite memory; rename one of the folders"]   # the owner rooms' inboxes too


def test_a_scope_change_is_said_once_and_again_only_after_the_index_ran_on_the_new_roots(tree, capsys, monkeypatch):
    """quiet-doctor (2026-10-24): "memory index: scope-changed" stood on every run after a room came. It is said on the first
    run that sees the doctor's scope key unindexed, then quiet until the index has run on the new roots (its meta key moves:
    check says anything else), which clears it, so a later change is said again."""
    priv, state, *_ = tree
    st = {"status": "scope-changed"}
    monkeypatch.setattr(memindex, "check", lambda db, scope, *a: dict(st))
    said = f"{words.WORKING} memory index: scope-changed, still on the roots before a room came or went; {doctor.INDEX}\n"
    assert doctor.main(priv, state) == 1 and capsys.readouterr().out == said
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN          # said once
    st["status"] = "fresh"                                                              # the index ran on the new roots
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN
    assert json.loads((state / "doctor.json").read_text())["scope"] == ""
    st["status"] = "scope-changed"                                                      # a room later: said again
    assert doctor.main(priv, state) == 1 and capsys.readouterr().out == said


def test_a_timer_in_units_the_box_never_installed_or_turned_off_is_one_orange_line_with_its_command(tree, monkeypatch):
    """quiet-doctor (2026-10-24): yerk-curate.timer sat in units/ and was never installed, so the night's curation never ran
    on the box. A timer in units/ that `systemctl --user is-enabled` does not call enabled is orange with its fix; a systemd
    that does not answer is one line; the example layer, a public checkout with no box of its own, is never asked."""
    priv, state, *_ = tree
    states, asked = {"yerk-curate.timer": "not-found", "yerk-recall-bench.timer": "disabled"}, []

    def fake(*a, **k):
        asked.append(a)
        return subprocess.CompletedProcess(a, 0, "".join(f"{states.get(x, 'enabled')}\n" for x in a[1:]), "")
    monkeypatch.setattr(doctor, "systemctl", fake)

    def timers(private=priv):
        return [x for x in doctor.run(private, state, dry=True) if x[2:].startswith(("timer ", "timers:"))]
    assert timers() == [f"{words.WORKING} timer yerk-curate.timer: not-found; `yerk deploy --timers` installs and enables it",
                        f"{words.WORKING} timer yerk-recall-bench.timer: disabled; `systemctl --user enable --now yerk-recall-bench.timer`"]
    assert asked == [("is-enabled", *sorted(p.name for p in (config.REPO / "units").glob("*.timer")))] and len(asked[0]) == 8
    states.clear()
    assert timers() == []
    asked.clear()
    assert timers(EXAMPLE_PRIVATE) == [] and asked == []
    monkeypatch.setattr(doctor, "systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "Failed to connect to bus"))
    assert timers() == [f"{words.WORKING} timers: not checked, OSError: systemctl --user is-enabled did not answer for every timer"]


def test_the_scrub_swaps_each_planted_value_keeps_every_line_backs_up_first_and_a_second_run_finds_nothing(tree, capsys):
    """quiet-doctor (2026-10-24): `yerk doctor --scrub` on the shapes PR 66's scan finds (example values only, decision 22):
    a dry run says each file and changes nothing; the scrub copies each file into <private>/backups/scrub-<day>/ first, swaps
    each value for [scrubbed] with the label and every line kept, never says a value, leaves a dead code (no hit), an
    import's or a day log's a month old, as it is, and a second run finds nothing to scrub."""
    priv, state, day, *_ = tree
    key, bot = "sk-ant-" + "a" * 24, "M" + "a" * 24 + "." + "b" * 6 + "." + "c" * 27
    at = datetime(2026, 10, 20, 11, 20).astimezone()
    lines = ["your verification code is 482913", "the example wifi password is Exa-mple9!", "an example line with nothing in it"]
    planted = {"attachments/discord-1/note.txt": "token: " + "abcd1234" * 3 + " and after it\n",
               "memory/facts/example-thing.md": f"# example thing\n\n- the example key {key}\n- the bot's {bot}\n- a plain line\n",
               "imports/mail/2019-02.md": "# Mail 2019-02\n\n- 2019-02-14 09:12 From: Example Bank | Subject: sign in\n"
                                          "  your verification code is 551234\n- 2019-02-15 10:00 the password is Exa-mple9!\n",
               "memory/daily/2026-10-20.md": day.read_text() + "".join(
                   memory.format_line(at, "example-room", "", "owner", "", x, "private", f"s{i}") + "\n" for i, x in enumerate(lines))}
    for rel, text in planted.items():
        (priv / rel).parent.mkdir(parents=True, exist_ok=True)
        (priv / rel).write_text(text)
    before = {rel: (priv / rel).read_bytes() for rel in planted}
    values = (key, bot, "abcd1234" * 3, "Exa-mple9!")
    assert doctor.main(priv, state, dry=True, scrubbing=True) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [f"{words.WORKING} attachments/discord-1/note.txt: would scrub line 1 (token line)",
                                f"{words.WORKING} imports/mail/2019-02.md: would scrub line 5 (password line)",
                                f"{words.WORKING} memory/daily/2026-10-20.md: would scrub line 9 (password line)",
                                f"{words.WORKING} memory/facts/example-thing.md: would scrub lines 3, 4 (key)"]
    assert {rel: (priv / rel).read_bytes() for rel in planted} == before and not (priv / "backups").exists()
    assert doctor.main(priv, state, scrubbing=True) == 0
    out = capsys.readouterr().out
    kept = list((priv / "backups").glob("scrub-*"))
    assert len(kept) == 1 and out.splitlines()[0] == (f"{words.DONE} attachments/discord-1/note.txt: scrubbed line 1 (token line), "
                                                      f"its copy in backups/{kept[0].name}/") and len(out.splitlines()) == 4
    assert not [v for v in (*values, "482913") if v in out]
    assert sorted(p.read_bytes() for p in kept[0].iterdir()) == sorted(before.values())          # each file copied first
    for rel, was in before.items():
        now = (priv / rel).read_bytes()
        assert now.count(b"\n") == was.count(b"\n") and not [v for v in values if v.encode() in now], rel
    assert (priv / "attachments/discord-1/note.txt").read_text() == "token: [scrubbed] and after it\n"
    assert "- the example key [scrubbed]\n- the bot's [scrubbed]\n- a plain line\n" in (priv / "memory/facts/example-thing.md").read_text()
    assert "your verification code is 551234" in (priv / "imports/mail/2019-02.md").read_text()      # dead: not a hit, not touched
    assert (priv / "memory/daily/2026-10-20.md").read_text().split("\n")[7] == before["memory/daily/2026-10-20.md"].decode().split("\n")[7]
    after = {rel: (priv / rel).read_bytes() for rel in planted}
    assert doctor.main(priv, state, scrubbing=True) == 0
    assert capsys.readouterr().out == f"{words.DONE} scrub: no secret-shaped line\n"
    assert {rel: (priv / rel).read_bytes() for rel in planted} == after and len(list(kept[0].iterdir())) == 4
    assert not [x for x in doctor.run(priv, state, dry=True) if "secret-shaped" in x]


def test_disk_growth_is_what_the_disk_holds_never_a_sparse_file_or_a_second_link(tree):
    """2026-11-10: a finished worker's sparse test files, 31 GB long and 1.7 GB on disk, read as the state folder's growth."""
    priv, state, *_ = tree
    (big := state / "workers" / "w1" / "edge.bin").parent.mkdir(parents=True)
    with big.open("wb") as f:
        f.truncate(2 * 10**9)                               # 2 GB long, no block written
    (one := big.with_name("one.bin")).write_bytes(b"x" * 8192)
    for n in range(3):
        os.link(one, one.with_name(f"link{n}.bin"))
    assert doctor.used(big.parent) == big.stat().st_blocks * 512 + one.stat().st_blocks * 512
    assert retention.size(retention.tree(state)) > 2 * 10**9 > doctor.used(state)
    (state / "doctor.json").write_text(json.dumps({"at": time.time() - DAY, "sizes": {"state folder": 0}, "sized": time.time() - DAY}))
    assert not [x for x in doctor.run(priv, state, dry=True) if "state folder: grew" in x]


# logins-notes-doctor (2026-11-20, need 127's deeper check): the notes a room files on a picture sit in its inbox under the
# state folder, which the check never read, and a saved login's password has no shape. Every value is a placeholder.
NOTE = "rooms/example-room/work/inbox/m-1/photo.seen.txt"            # under the state folder
SAVED = "pw-Fake-9-Qx7Zk2Wd"                                          # test_pictures' saved password
PASSWORD_LINE = "the example wifi password is Exa-mple9!"


def note(state: Path, *lines: str, at: float | None = None) -> Path:
    """A note a room filed with `seen` (pictures.file's shape: the header by code, the words, what it shows), `at` its own time."""
    (f := state / NOTE).parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(["seen in example-room, message m-1, 2026-11-20 10:00 UTC: photo.png", *lines,
                            "what it shows: a sign-in page"]) + "\n")
    if at is not None:
        os.utime(f, (at, at))
    return f


def y5_plant(priv: Path, state: Path, day: Path) -> dict[str, bytes]:
    """A saved login, and its password with no shape around it on a day log's line (8), a note's (3) and an import's (4);
    each file's bytes before."""
    assert logins.save(priv, "example", "example.com", "example-user", SAVED)["ok"]
    at = datetime(2026, 10, 20, 11, 20).astimezone()
    day.write_text(day.read_text() + memory.format_line(at, "example-room", "", "owner", "", f"the new one is {SAVED} for now",
                                                        "private", "s9") + "\n")
    note(state, "EX-42", f"signed in with{SAVED}today")
    plant(priv, imports__mail__2026_10=f"# Mail 2026-10\n\n- 2026-10-14 09:12 From: Example Shop | Subject: welcome\n  keep {SAVED} safe\n")
    return {f: p.read_bytes() for f, p in (("imports/mail/2026-10.md", priv / "imports/mail/2026-10.md"),
                                           ("memory/daily/2026-10-20.md", day), (NOTE, state / NOTE))}


def test_y4_a_password_line_in_a_note_a_room_filed_is_red_by_its_path_under_the_state_folder_and_its_line(tree, capsys):
    """Y4: the doctor reads every room's inbox for the text kinds the index reads there, so a password line in a picture's
    note is a red finding by the note's path under the state folder and its line. The code before this walked only the
    memory tree and the private layer's attachments and imports, none of which holds the note: it was not found."""
    priv, state, *_ = tree
    f = note(state, "EX-42", PASSWORD_LINE)
    m = memory.settings(priv, state)
    assert not any(f.resolve().is_relative_to(r.resolve()) for r in (m.root, priv / "attachments", priv / "imports"))
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} secret-shaped lines: 1 in 1 file, first {NOTE}:3 (password line); "
                                                    "`yerk doctor --scrub`, then rotate the key"]


def test_y5_a_saved_logins_password_with_no_shape_is_red_in_a_day_log_a_note_and_an_import_and_the_scrub_takes_each(tree, capsys):
    """Y5: a line holding a saved login's password is a hit of its own kind, `saved password`, by file and line as the
    shapes are, with what to do after: the password is out, so it is changed and the new one saved with /login. The
    scrub says "would scrub" for each on a dry run, then leaves [scrubbed] in the value's place, every other byte as it
    was, each file's copy in the scrub folder, and a second scrub finds nothing."""
    priv, state, day, *_ = tree
    before = y5_plant(priv, state, day)
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} " + words.SAVED_PASSWORDS.format(
        n=3, files="3 files", first="imports/mail/2026-10.md:4")]
    assert "change it on its site and save the new one with /login" in words.SAVED_PASSWORDS
    m = memory.settings(priv, state)
    assert [h for hs in doctor.leaked(priv, m, time.time()).values() for h in hs] == [(4, "saved password"), (8, "saved password"),
                                                                                       (3, "saved password")]
    assert doctor.main(priv, state, dry=True, scrubbing=True) == 0
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} {f}: would scrub line {n} (saved password)"
                                                    for f, n in zip(before, (4, 8, 3))]
    assert {f: (state if f == NOTE else priv).joinpath(f).read_bytes() for f in before} == before and not (priv / "backups").exists()
    assert doctor.main(priv, state, scrubbing=True) == 0
    kept = list((priv / "backups").glob("scrub-*"))
    assert capsys.readouterr().out.splitlines() == [f"{words.DONE} {f}: scrubbed line {n} (saved password), its copy in backups/{kept[0].name}/"
                                                    for f, n in zip(before, (4, 8, 3))]
    for f, was in before.items():
        assert (state if f == NOTE else priv).joinpath(f).read_bytes() == was.replace(SAVED.encode(), b"[scrubbed]"), f
    assert sorted(p.read_bytes() for p in kept[0].iterdir()) == sorted(before.values())          # each file copied first
    assert doctor.main(priv, state, scrubbing=True) == 0
    assert capsys.readouterr().out == f"{words.DONE} scrub: no secret-shaped line\n"


def test_y6_a_one_time_code_in_a_note_is_said_for_two_days_after_the_notes_own_time_and_not_after(tree, capsys):
    """Y6: a note's one-time code is dated by the note's own time (a note has no entry dates), as a day log's is by its day,
    and is dead two days after it: said up to then, never after."""
    priv, state, *_ = tree
    at = datetime(2026, 11, 20, 10, 0).timestamp()
    f = note(state, "EX-42", CUED[0], at=at)
    m = memory.settings(priv, state)
    said = {after: doctor.leaked(priv, m, at + after).get(f.resolve(), []) for after in (0, DAY, 2 * DAY - 60, 2 * DAY + 60, 9 * DAY)}
    assert said == {0: [(3, "one-time code")], DAY: [(3, "one-time code")], 2 * DAY - 60: [(3, "one-time code")], 2 * DAY + 60: [],
                    9 * DAY: []}
    now = time.time()
    os.utime(f, (now - 3600, now - 3600))                                       # an hour old by the clock: said, orange
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.WORKING} " + words.ONE_TIME_CODES.format(n=1, files="1 file", first=f"{NOTE}:3")]
    os.utime(f, (now - 3 * DAY, now - 3 * DAY))                                 # three days old: dead, the doctor is clean
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == CLEAN


def test_y8_no_answer_log_line_or_finding_in_y2_to_y7_holds_a_saved_password_or_a_refused_value(tree, capsys, caplog, monkeypatch, tmp_path):
    """Y8: what `seen` answers (Y2, Y3, and /login's own lines), every log line at DEBUG, and every line the doctor and its
    scrub say (Y4 to Y6) hold no saved password nor a value refused, nor any ten characters in a row of one; nor does any
    log line while the reader runs (Y7)."""
    from test_logins import ELSEWHERE, TWO, y7_plant
    from test_pictures import PLAIN, SHAPED, SHORT, daemon, picture, signed, y2_run, y3_run
    caplog.set_level(logging.DEBUG)
    priv, state, day, *_ = tree
    for d in ("y2", "y3"):
        (tmp_path / d).mkdir()
    y = daemon(tmp_path / "y2")
    said = [json.dumps(r) for r, _ in y2_run(y, picture(tmp_path / "y2", "example-room", "m-1"))]
    y3, logged = signed(tmp_path / "y3", monkeypatch, SAVED, SHORT, PLAIN)
    said += [json.dumps(logged)] + [json.dumps(r) for r, _ in y3_run(y3, picture(tmp_path / "y3", "example-room", "m-1"))]
    y5_plant(priv, state, day)
    note(state, "EX-42", f"signed in with{SAVED}today", PASSWORD_LINE, CUED[0])   # Y5's note with Y4's password line, Y6's code
    capsys.readouterr()
    said += doctor.run(priv, state, dry=True) + doctor.scrub(priv, state, dry=True) + doctor.scrub(priv, state)
    y7_plant(tmp_path / "y7" / "private")
    assert logins.passwords(tmp_path / "y7" / "private") == TWO
    values = [SAVED, SHORT, PLAIN, *TWO, ELSEWHERE, *(line.rsplit(" ", 1)[1] for line in SHAPED.values())]
    pieces = {v[i:i + 10] for v in values for i in range(max(1, len(v) - 9))}
    out = "\n".join(said) + "\n" + caplog.text + capsys.readouterr().out
    assert len(said) > 12 and "saved password" in out and "line 2 of" in out
    assert not [p for p in pieces if p in out]
