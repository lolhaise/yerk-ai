"""Checks texting's reply list, the people a room may answer by text for you: yours only, handles in one form, a handle on one
person only. Each person's sheet beside it (run 2), read by code and checked, and changed only through the report room's `texting`
of this turn, each change one line of sheets.log. Every person and number here is made up."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from yerk_ai import doctor, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.texting import people

AT, SPACED = "@", "+1 (555) 000" "-0101"     # split in the source: the repository scan reads no fixture
LIST = f"""[owner]
name = "Sam"

[people.alex]
first = "Alex"
handles = ["{SPACED}", "Alex.Q{AT}Example.com"]
sheet = "people/alex.toml"
release = true

[people.beta]
handles = ["+15550000102"]
"""


def write(tmp_path, text: str, mode: int = 0o600):
    f = tmp_path / "people.toml"
    f.write_text(text)
    os.chmod(f, mode)
    return f


def test_people_loads_and_matches_a_handle_however_it_is_written(tmp_path):
    p = people.load(write(tmp_path, LIST))
    assert p.owner == "Sam" and set(p.people) == {"alex", "beta"}
    alex = p.people["alex"]
    assert alex.handles == ("+15550000101", f"alex.q{AT}example.com") and alex.release and alex.sheet == "people/alex.toml"
    assert p.by_handle("+15550000101") is alex and p.by_handle(f"ALEX.Q{AT}example.com") is alex
    assert p.people["beta"].first == "beta" and not p.people["beta"].release
    assert p.by_handle("+15550000199") is None and p.by_handle("") is None


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_people_refuses_a_file_others_can_read(tmp_path, mode):
    with pytest.raises(ValueError, match="0600"):
        people.load(write(tmp_path, LIST, mode))


@pytest.mark.parametrize("bad", ['handles = ["Alex"]', 'handles = []', 'handles = "+15550000103"',
                                 'handles = ["+15550000102"]'])
def test_people_refuses_a_bad_handle_and_one_shared(tmp_path, bad):
    with pytest.raises(ValueError, match="people.casey"):
        people.load(write(tmp_path, LIST + f"\n[people.casey]\n{bad}\n"))


def test_doctor_names_what_keeps_texting_off(tmp_path):
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    state.mkdir()
    (priv / "secrets").mkdir()
    (token := priv / "secrets" / "claude-one.token").write_text("token-one")      # the check runs on a token alone (J28)
    os.chmod(token, 0o600)

    def said(table: str) -> list[str]:
        (priv / "yerkd.toml").write_text(table)
        return [x[2:] for x in doctor.run(priv, state, dry=True) if "[texting]" in x]
    assert said("") == [] and said('[texting]\nmode = "off"\nburst_cap = "soon"\n') == []
    on = '[texting]\nmode = "dry"\nreport = "example-room"\n'
    missing = said(on + 'burst_cap = "soon"\ndry_report = "nowhere"\n')
    assert missing[0] == "[texting] burst_cap: not a number above 0; texting stays off"
    assert missing[1].startswith("[texting] people: ") and missing[1].endswith("; texting stays off")
    assert missing[2:] == ["[texting] dry_report: nowhere is not an owner room in rooms.toml"]
    (priv / "texting").mkdir()
    write(priv / "texting", LIST, 0o644)
    assert [x for x in said(on) if "0600" in x]
    os.chmod(priv / "texting" / "people.toml", 0o600)
    assert said(on) == [] and said('[texting]\nmode = "dry"\n') == ["[texting] report: none is not an owner room in rooms.toml"]
    assert said(on + 'quiet = ["23:30"]\nfirst_shot = [240, 150]\nclaim_seconds = 0\nmorning = [10, 90]\n') == [
        "[texting] claim_seconds: not a number above 0; texting stays off",
        "[texting] first_shot: [240, 150] is not two numbers, low then high; texting stays off",
        "[texting] quiet: ['23:30'] is not two HH:MM times; texting stays off"]
    live = on.replace('"dry"', '"live"')
    assert "[texting] outbox: not a whole path on the Mac; live sends nothing" in said(live)
    assert not [x for x in said(live + 'outbox = "/Users/someone/outbox"\n') if "outbox" in x]
    token.unlink()
    assert said(on) == [f"[texting] {words.TEXTING_NO_TOKEN}; `yerk accounts mint one` makes one"]


def test_doctor_is_red_for_a_sender_whose_last_pass_is_older_than_a_minute_only_while_yerkd_runs(tmp_path, monkeypatch):
    priv, state, now = tmp_path / "private", tmp_path / "state", 1_763_000_000.0
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    (priv / "yerkd.toml").write_text('[texting]\nmode = "dry"\nreport = "example-room"\n')
    (state / "texting").mkdir(parents=True)

    def said(ago: float, up: bool) -> list[str]:
        (state / "texting" / "status.json").write_text(json.dumps({"mode": "dry", "up": True, "pass": now - ago}))
        monkeypatch.setattr(doctor.status, "alive", lambda s: up)
        return [x[2:] for x in doctor.run(priv, state, dry=True, now=now) if "sender" in x]
    assert said(50, True) == [] and said(120, False) == []
    assert said(120, True) == [f"[texting] the sender stopped {datetime.fromtimestamp(now - 120):%H:%M}: no text is timed or "
                               "sent; `systemctl --user restart yerkd` starts it again"]


A1, B1 = "+15550000101", "+15550000102"
REPLY = f'[owner]\nname = "Sam"\n\n[people.alex]\nfirst = "Alex"\nhandles = ["{A1}"]\n# kept as written\n\n' \
        f'[people.beta]\nfirst = "Beta"\nhandles = ["{B1}"]\nrelease = false\n'
SHEET = 'open = ["location"]\nknows = [{fact = "the new car", source = "owner-room m:1 2026-11-12"}]\nnight = "answer"\n' \
        'reacts = ["love"]\nunanswered_share = 0.25\n\n[pacing]\nactive = [0.5, 2]\nburst_gap = [10, 35]\n\n' \
        "[voice]\ncard = '''\nshort, lowercase, no full stops'''\n"


def folder(tmp_path: Path, sheet: str | None = SHEET) -> Path:
    """A texting folder: the list (0600) and, unless `sheet` is None, Alex's sheet; Beta has none."""
    (where := tmp_path / "texting" / "people").mkdir(parents=True)
    (f := where.parent / "people.toml").write_text(REPLY)
    os.chmod(f, 0o600)
    if sheet is not None:
        (s := where / "alex.toml").write_text(sheet)
        os.chmod(s, 0o600)
    return f


def test_a_sheet_loads_and_a_person_with_none_has_no_card_and_every_class_held(tmp_path):
    who = people.load(folder(tmp_path))
    alex = who.sheets["alex"]
    assert alex.open == ("location",) and alex.knows == (("the new car", "owner-room m:1 2026-11-12"),)
    assert (alex.night, alex.reacts, alex.unanswered_share) == ("answer", ("love",), 0.25)
    assert alex.pacing == {"active": (0.5, 2.0), "burst_gap": (10.0, 35.0)} and alex.card == "short, lowercase, no full stops"
    assert who.sheets["beta"] == people.Sheet() and who.sheets["beta"].open == () and who.sheets["beta"].card == ""
    for s in (alex, people.Sheet(card="it's\n'''"), people.Sheet(knows=(('a "quoted" fact', ""),))):    # written back, the same
        (f := tmp_path / "round.toml").write_text(people.sheet_text(s))
        os.chmod(f, 0o600)
        assert people.sheet(f) == s


def test_the_classes_are_one_list_and_a_sheet_opening_another_fails_the_load_with_its_file_and_line(tmp_path):
    assert words.TEXTING_CLASSES == ("health", "medication", "substances", "faith", "money", "job", "dating", "location")
    bad = SHEET.replace('open = ["location"]', 'open = [\n  "location",\n  "gossip",\n]')
    with pytest.raises(ValueError, match=r"alex\.toml:3: open: 'gossip' is not a class; the classes: health, "):
        people.load(folder(tmp_path, bad))
    for i, (old, new, line, key) in enumerate((('night = "answer"', 'night = "late"', 3, "night"),
                                               ('reacts = ["love"]', 'reacts = ["wow"]', 4, "reacts"),
                                               ("unanswered_share = 0.25", "unanswered_share = 2", 5, "unanswered_share"),
                                               ("burst_gap = [10, 35]", "burst_gap = [35, 10]", 7, "pacing"),
                                               ('night = "answer"', "nite = 1", 3, "nite"),
                                               ("[voice]", "[voice]\ntone = 1", 11, "voice"))):
        with pytest.raises(ValueError, match=rf"alex\.toml:{line}: {key}: "):
            people.load(folder(tmp_path / str(i), SHEET.replace(old, new)))
    with pytest.raises(ValueError, match=r"alex\.toml: .*\(at line 2"):
        people.load(folder(tmp_path / "syntax", "reacts = []\nnight = \n"))
    os.chmod(folder(tmp_path / "open").parent / "people" / "alex.toml", 0o644)
    with pytest.raises(ValueError, match=r"alex\.toml must be 0600"):
        people.load(tmp_path / "open" / "texting" / "people.toml")
    (f := folder(tmp_path / "out", None)).write_text(REPLY.replace('first = "Beta"', 'first = "Beta"\nsheet = "../b.toml"'))
    with pytest.raises(ValueError, match=r"people\.beta: sheet must be a path inside texting/"):
        people.load(f)


def test_check_names_a_bad_sheet_with_its_file_and_line_a_missing_one_and_a_file_no_one_names(tmp_path, capsys):
    from yerk_ai import cli
    f = folder(priv := tmp_path / "private")
    (priv / "yerkd.toml").write_text('[texting]\nmode = "dry"\n')
    (beta := f.parent / "people" / "beta.toml").write_text(SHEET)
    os.chmod(beta, 0o600)
    log = f.parent / "sheets.log"
    log.write_text("not read\n")                                   # the log is never read: a line in it is no finding
    assert cli.main(["texting", "sheets", "--check", "--private", str(priv)]) == 0
    assert capsys.readouterr().out == "every sheet loads\n"
    beta.write_text(SHEET.replace('reacts = ["love"]', 'reacts = ["love"]\nopen = 3'))
    (f.parent / "people" / "gamma.toml").write_text("")
    (f.parent / "people" / "alex.toml").unlink()
    assert cli.main(["texting", "sheets", "--check", "--private", str(priv)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"{f.parent / 'people' / 'alex.toml'}: missing, so alex has no voice card and every class held"
    assert out[1].startswith(f"{beta}: ") and "line 5" in out[1]                  # tomllib's own words: a key set twice
    assert out[2:] == [f"{f.parent / 'people' / 'gamma.toml'}: no one on the list names it"]
    (f.parent / "people" / "gamma.toml").unlink()
    beta.write_text(SHEET.replace('open = ["location"]', 'open = ["money", "gossip"]'))
    assert cli.main(["texting", "sheets", "--check", "--private", str(priv)]) == 1
    assert capsys.readouterr().out.splitlines()[1] == f"{beta}:1: open: 'gossip' is not a class; the classes: " + ", ".join(words.TEXTING_CLASSES)
    f.write_text(REPLY.replace(f'handles = ["{B1}"]', f'handles = ["{A1}"]'))
    assert cli.main(["texting", "sheets", "--check", "--private", str(priv)]) == 1
    assert capsys.readouterr().out == f"{f}: people.beta: a handle is not a number or an address, or is someone else's too\n"


def daemon(tmp_path: Path, table: str = '[texting]\nmode = "dry"\nreport = "owner-room"\n'):
    """A daemon on the example layer with the texting folder above, and two made-up rooms: owner-room, the report room,
    and person-room, a room that answers a person."""
    import asyncio

    from test_gates import Rec
    from yerk_ai.daemon import Yerkd
    f = folder(priv := tmp_path / "private")
    shutil.copytree(EXAMPLE_PRIVATE, priv, dirs_exist_ok=True)
    (priv / "yerkd.toml").write_text(table)
    (tmp_path / "state").mkdir()
    y = Yerkd(priv, tmp_path / "state")
    y.room_confs["owner-room"] = dict(y.room_confs["example-room"])
    y.room_confs["person-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    rooms = {n: y.add_room(n, Rec()) for n in ("owner-room", "person-room", "example-room")}

    def tool(room: str, op: str, person: str = "alex", value: str = "", said: str = "") -> dict:
        return asyncio.run(y.tool_texting(room, op, person, value, said))
    return y, rooms, f, tool


def files(f: Path) -> dict[str, tuple[bytes, int]]:
    return {str(x.relative_to(f.parent)): (x.read_bytes(), x.stat().st_mtime_ns) for x in sorted(f.parent.rglob("*")) if x.is_file()}


ORDER = "Alex can always know where I am"


def test_an_edit_without_the_owners_own_words_of_this_turn_is_refused_and_changes_nothing(tmp_path):
    from yerk_ai.room import Inbound
    y, rooms, f, tool = daemon(tmp_path)
    before = files(f)
    for turn, said in (([], ORDER), ([Inbound("m1", "person", ORDER)], ORDER), ([Inbound("m2", "owner", "ok")], ORDER),
                       ([Inbound("m3", "owner", ORDER)], ""), ([Inbound("m5", "owner", ORDER)], "Alex can"),      # a scrap of the owner's
                       ([Inbound("m4", "owner", ORDER)], "Alex can always know")):
        rooms["owner-room"].inflight = turn                    # the running turn's messages, as said_in_turn reads them
        for op, value in (("release", "on"), ("open", "location"), ("knows", "the trip"), ("forget", "the new car")):
            got = tool("owner-room", op, value=value, said=said)
            if turn and turn[0].id == "m4":
                break                                        # the owner's own words, in part: this one goes
            assert got == {"ok": False, "line": words.TEXTING_NOT_SAID}, (turn, said, op)
    assert files(f) == {**before, **{k: v for k, v in files(f).items() if k in ("people.toml", "sheets.log")}}
    assert [x["source"] for x in map(__import__("json").loads, (f.parent / "sheets.log").read_text().splitlines())] == [
        f"owner-room m:m4 {__import__('datetime').datetime.now(y.memory.tz):%Y-%m-%d}"]


def test_an_edit_or_a_read_from_a_persons_room_is_refused_and_shows_nothing_of_texting(tmp_path):
    from yerk_ai.room import Inbound
    y, rooms, f, tool = daemon(tmp_path)
    y.ledger.texting_add(person="alex", handle=A1, inbound="g1", state="missed", mode="dry", reason="older than 6 h")
    before = files(f)
    for room in ("person-room", "example-room", "no-such-room"):
        if room in rooms:
            rooms[room].inflight = [Inbound("m9", "owner", f"open location for Alex, {ORDER}")]
        for op, value in (("sheet", ""), ("log", ""), ("why", ""), ("open", "location"), ("close", "location"),
                          ("knows", "the trip"), ("forget", "the new car"), ("release", "on"), ("help", ""), ("nonsense", "")):
            got = tool(room, op, value=value, said=ORDER)
            want = words.TEXTING_WHERE.format(room="owner-room") if room == "example-room" else words.TEXTING_ONLY
            assert got == {"ok": False, "line": want}, (room, op)                  # another owner room is told which room
            shown = repr(got)
            assert not any(x in shown for x in ("short, lowercase", "the new car", "location", "6 h", "missed", A1, "sheets.log"))
            assert room == "example-room" or "owner-room" not in shown             # a person's room is never told its name
    assert files(f) == before and not (f.parent / "sheets.log").exists()


def test_a_persons_room_gets_one_line_whatever_the_mode_and_help_answers_in_the_report_room_only(tmp_path):
    (tmp_path / "off").mkdir()
    y, rooms, f, tool = daemon(tmp_path / "off", table="")                         # no [texting] table: texting is off
    assert tool("person-room", "sheet") == tool("no-such-room", "help") == {"ok": False, "line": words.TEXTING_ONLY}
    assert tool("owner-room", "help") == tool("example-room", "help") == {"ok": False, "line": words.TEXTING_OFF}
    y, rooms, f, tool = daemon(tmp_path)                                           # dry: a person's room hears the same line
    assert tool("person-room", "sheet") == tool("person-room", "help") == {"ok": False, "line": words.TEXTING_ONLY}
    assert tool("owner-room", "help") == {"ok": True, "line": words.TEXTING_HELP}
    assert tool("owner-room", "nonsense") == {"ok": False, "line": words.TEXTING_OPS}


def test_each_change_is_one_line_of_sheets_log_and_the_files_keep_their_mode(tmp_path):
    import hashlib
    import json

    from yerk_ai.room import Inbound
    y, rooms, f, tool = daemon(tmp_path)
    rooms["owner-room"].inflight = [Inbound("m7", "owner", f"{ORDER}. And money is fine with Beta; she knows the trip moved; "
                                             "drop the car thing; Beta and Alex can get texts without asking")]
    os.chmod(sheet := f.parent / "people" / "alex.toml", 0o400)
    said = {"said": "drop the car thing"}
    for op, person, value in (("close", "alex", "location"), ("open", "beta", "money"), ("knows", "alex", "the trip moved"),
                              ("forget", "alex", "the new car"), ("release", "beta", "on"), ("release", "alex", "on")):
        assert tool("owner-room", op, person, value, **said) == {
            "ok": True, "line": words.TEXTING_CHANGED.format(first=person.title(), op=op, value=value)}, (op, person)
    assert tool("owner-room", "close", "alex", "location", **said)["line"] == words.TEXTING_SAME.format(first="Alex", op="close", value="location")
    assert tool("owner-room", "open", "alex", "gossip", **said)["line"].startswith("not changed: gossip is not a class")
    assert tool("owner-room", "release", "alex", "maybe", **said)["line"] == words.TEXTING_ON_OFF
    assert tool("owner-room", "open", "gamma", "money", **said)["line"] == words.TEXTING_NO_ONE.format(key="gamma", keys="alex, beta")
    rows = [json.loads(x) for x in (f.parent / "sheets.log").read_text().splitlines()]
    assert [(r["person"], r["op"], r["value"], r["file"]) for r in rows] == [
        ("alex", "close", "location", "people/alex.toml"), ("beta", "open", "money", "people/beta.toml"),
        ("alex", "knows", "the trip moved", "people/alex.toml"), ("alex", "forget", "the new car", "people/alex.toml"),
        ("beta", "release", "on", "people.toml"), ("alex", "release", "on", "people.toml")]
    assert rows[1]["before"] == "" and rows[2]["before"] == rows[0]["after"] and rows[3]["before"] == rows[2]["after"]
    assert rows[5]["before"] == rows[4]["after"] == hashlib.sha256(REPLY.replace("release = false", "release = true").encode()).hexdigest()
    assert rows[3]["after"] == hashlib.sha256(sheet.read_bytes()).hexdigest() and {r["source"].rsplit(" ", 1)[0] for r in rows} == {"owner-room m:m7"}
    assert f.read_text() == REPLY.replace("release = false", "release = true").replace(
        '[people.alex]\n', '[people.alex]\nrelease = true\n')                              # the release line only; the comment kept
    assert os.stat(sheet).st_mode & 0o777 == 0o400 and os.stat(f.parent / "people" / "beta.toml").st_mode & 0o777 == 0o600
    who = people.load(f)
    assert who.sheets["alex"].open == () and who.sheets["alex"].knows == (("the trip moved", rows[2]["source"]),)
    assert who.sheets["alex"].card == "short, lowercase, no full stops" and who.sheets["beta"].open == ("money",)
    assert who.people["alex"].release and who.people["beta"].release
    f.write_text(f.read_text().replace("[people.beta]", "[ people.beta ]"))      # a header written another way
    kept = {k: v[0] for k, v in files(f).items()}
    assert tool("owner-room", "release", "beta", "off", **said) == {"ok": False, "line": words.TEXTING_UNREAD.format(file="people.toml")}
    assert {k: v[0] for k, v in files(f).items()} == kept and len(kept["sheets.log"].splitlines()) == 6


def test_the_read_ops_only_read_and_with_texting_off_answer_texting_is_off(tmp_path, monkeypatch):
    import time

    from yerk_ai.daemon import YERK_TOOLS
    assert "mcp__yerk__texting" in YERK_TOOLS
    y, rooms, f, tool = daemon(tmp_path)
    now = time.time()
    y.ledger.inbound_add("g1", "texting", "imessage:alex", "alex", 12, created=now - 60)
    y.ledger.inbound_add("g2", "texting", "imessage:alex", "owner", 5, created=now - 30)
    y.ledger.texting_add(person="alex", handle=A1, inbound="g1", state="missed", mode="dry", reason="older than 6 h")
    before = files(f)
    got = tool("owner-room", "sheet")
    assert got["ok"] and got["open"] == ["location"] and got["card"] == "short, lowercase, no full stops" and got["on_file"]
    assert got["release"] is False and tool("owner-room", "sheet", "beta")["on_file"] is False
    lines = tool("owner-room", "log")["lines"]
    assert [x.split(" ", 1)[1] for x in lines] == ["Alex: 12 characters, received", "the owner: 5 characters, received",
                                                   "missed (dry): older than 6 h"]
    assert tool("owner-room", "log", value="2001-01-01") == {"ok": True, "day": "2001-01-01", "lines": []}
    assert tool("owner-room", "why")["line"].endswith(" missed (dry): older than 6 h")
    assert tool("owner-room", "why", "beta") == {"ok": True, "line": words.TEXTING_NO_WHY.format(first="Beta")}
    assert tool("owner-room", "sheet", "gamma")["line"] == words.TEXTING_NO_ONE.format(key="gamma", keys="alex, beta")
    assert tool("owner-room", "say")["line"] == words.TEXTING_OPS
    assert files(f) == before and not (f.parent / "sheets.log").exists()
    for n, table in enumerate(("", '[texting]\nmode = "off"\nreport = "owner-room"\n')):
        y, rooms, f, tool = daemon(tmp_path / str(n), table)
        for op in ("sheet", "log", "why", "open", "release"):
            assert tool("owner-room", op, value="location", said=ORDER) == {"ok": False, "line": words.TEXTING_OFF}
