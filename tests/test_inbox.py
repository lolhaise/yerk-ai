"""Checks the `texts` tool (inbox.py), which lets a room read the newest text messages on your Mac. Need 2026-11-04-53: a
room asked whether a text had come in could not say, because the Mac was asleep and the read of its Messages over
ssh timed out. The newest texts are kept as a private copy on the machine Yerk AI runs on,
refreshed by a light check whenever the Mac answers, its own short wakes included; a room asked while the Mac sleeps
answers from the last copy and says that copy's time. Nothing here keeps the Mac awake or wakes it, and reading never
sends. Need 2026-11-09-83: a room asked to confirm the words of a text the owner had just sent found its text field
empty, as a sent text's can be, while a received one carried its words; Messages kept those words only in the
message's attributedBody, which the read now brings and reads, and a message with no words anywhere says why. No real Mac
is reached: a fake runner stands in for ssh, and every number and address is a placeholder."""
from __future__ import annotations

import asyncio
import sqlite3
import stat
import time
from pathlib import Path

from yerk_ai import inbox, mcp_server, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import YERK_TOOLS, Yerkd
from yerk_ai.engine import SessionTable

# a placeholder other party, as chat.db joins message to handle; no real contact is here
THEM = "+1 555 0100200"
HOST = "macbook-test"                        # a placeholder ssh host; no real machine is reached
ROWS = [(1_762_300_200.0, 0, THEM, "running late, there by 5"),
        (1_762_300_100.0, 1, THEM, "on my way\nfive minutes out"),
        (1_762_300_000.0, 0, THEM, "where are you?")]


def output(rows: list[tuple]) -> bytes:
    """chat.db rows as sqlite writes them for inbox.script: fields on US, rows on RS, a trailing RS as sqlite's -newline; a
    row's fifth value is its attributedBody, bytes written as sqlite's hex() writes them or a str as given, else ""."""
    def ab(r: tuple) -> str:
        return "" if len(r) < 5 else r[4] if isinstance(r[4], str) else r[4].hex().upper()
    return "".join(inbox.US.join([f"{r[0]}", str(r[1]), r[2], r[3], ab(r)]) + inbox.RS for r in rows).encode()


def typedstream(text: str) -> bytes:
    """An attributedBody as Messages archives one: the NSString's words after '+' and their length, one byte under 128,
    else 0x81 and two bytes little-endian (test_sources.typedstream, the longer length too)."""
    raw = text.encode()
    n = bytes([len(raw)]) if len(raw) < 0x80 else b"\x81" + len(raw).to_bytes(2, "little")
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + n + raw + b"\x86\x84"


SENT = "sounds good, see you at 7 \U0001f44d"     # a placeholder text the owner sent, its words in its attributedBody alone


def runner(mode: str = "awake", rows: list[tuple] | None = None):
    """A stand-in for ssh: `awake` returns the sqlite output, `asleep` exits 255 as ssh does when the Mac is unreachable,
    `slow` times out, `locked` answers but the read failed. It records every command it was handed."""
    seen: list[tuple[list[str], bytes]] = []

    def run(cmd: list[str], stdin: bytes, timeout: float) -> tuple[int | None, bytes, bool]:
        seen.append((cmd, stdin))
        if mode == "asleep":
            return 255, b"", False
        if mode == "slow":
            return None, b"", True
        if mode == "locked":
            return 1, b"Error: database is locked", False
        return 0, output(ROWS if rows is None else rows), False

    run.seen = seen                                      # type: ignore[attr-defined]
    return run


# ---- the module: the read, the copy and the light check ----------------------------------------------------------

def test_the_read_is_a_read_only_sqlite_of_the_messages_database_over_ssh():
    cmd, script = inbox.argv(HOST), inbox.script(5)
    assert cmd[0] == "ssh" and cmd[-3:] == [HOST, "/bin/bash", "-s"]
    assert "BatchMode=yes" in cmd and f"ConnectTimeout={inbox.CONNECT}" in cmd
    assert "-readonly" in script and "Library/Messages/chat.db" in script and "LIMIT 5" in script
    assert "insert" not in script.lower() and "update" not in script.lower() and "delete" not in script.lower()


def test_parse_keeps_who_said_what_and_a_text_with_newlines_survives_the_split():
    msgs = inbox.parse(output(ROWS).decode())
    assert [m["text"] for m in msgs] == ["running late, there by 5", "on my way\nfive minutes out", "where are you?"]
    assert msgs[0] == {"at": 1_762_300_200.0, "from_me": False, "who": THEM, "text": "running late, there by 5"}
    assert msgs[1]["from_me"] is True                    # a line the owner sent
    assert inbox.parse("") == [] and inbox.parse(inbox.RS + inbox.RS) == []     # blank rows are skipped


def test_parse_caps_a_long_text_so_one_message_cannot_bloat_the_copy():
    [msg] = inbox.parse(output([(1.0, 0, THEM, "y" * (inbox.TEXT_MAX + 500))]).decode())
    assert len(msg["text"]) == inbox.TEXT_MAX


def test_read_awake_returns_the_messages_and_asleep_is_told_apart_from_a_failed_read():
    assert inbox.read(HOST, run=runner("awake"))["messages"][0]["text"] == "running late, there by 5"
    assert inbox.read(HOST, run=runner("asleep")) == {"ok": False, "asleep": True}
    assert inbox.read(HOST, run=runner("slow")) == {"ok": False, "asleep": True}
    locked = inbox.read(HOST, run=runner("locked"))
    assert locked["ok"] is False and locked["asleep"] is False and "locked" in locked["error"]
    assert inbox.read("", run=runner("awake")) == {"ok": False, "asleep": True}      # no host: nothing is run


def test_the_copy_roundtrips_and_is_private(tmp_path: Path):
    assert inbox.read_copy(tmp_path) is None                                   # none yet
    data = inbox.write_copy(tmp_path, HOST, inbox.parse(output(ROWS).decode()))
    got = inbox.read_copy(tmp_path)
    assert got is not None and got["messages"] == data["messages"] and got["host"] == HOST
    assert stat.S_IMODE((p := inbox.path(tmp_path)).stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_the_light_check_copies_when_awake_and_a_failed_check_leaves_the_last_copy(tmp_path: Path):
    assert inbox.refresh(tmp_path, HOST, run=runner("awake")) is not None
    first = inbox.read_copy(tmp_path)
    assert first is not None and len(first["messages"]) == len(ROWS)
    for mode in ("asleep", "slow", "locked"):                                  # a failed read never clobbers the good copy
        assert inbox.refresh(tmp_path, HOST, run=runner(mode)) is None
        assert inbox.read_copy(tmp_path) == first
    assert inbox.refresh(tmp_path, "", run=runner("awake")) is None            # no host: nothing read, nothing written


def test_the_wall_a_text_the_owner_sent_with_its_text_column_blank_is_read_from_its_attributed_body():
    # the night of the ask: the owner's own texts came back with an empty text field, those the owner got with their words
    msgs = inbox.parse(output([(1_762_300_300.0, 1, THEM, "", typedstream(SENT)), *ROWS]).decode())
    assert msgs[0] == {"at": 1_762_300_300.0, "from_me": True, "who": THEM, "text": SENT}
    assert [m["text"] for m in msgs[1:]] == [r[3] for r in ROWS]                # a text column with words is kept as it is
    long = "x" * 300 + "\nthe end"                                             # past 127 bytes: its length in two bytes
    assert inbox.parse(output([(1.0, 1, THEM, "", typedstream(long))]).decode())[0]["text"] == long


def test_the_query_brings_the_attributed_body_as_hex_only_for_a_row_whose_text_is_blank(tmp_path: Path):
    db = sqlite3.connect(tmp_path / "chat.db")                                 # chat.db's columns the read touches
    db.executescript("create table handle (ROWID integer primary key, id text); create table message (ROWID integer "
                     "primary key, text text, attributedBody blob, handle_id int, is_from_me int, date int);")
    db.execute("insert into handle values (1, ?)", (THEM,))
    rows = [(None, typedstream(SENT), 1, 3), ("", typedstream(SENT), 1, 2), ("where are you?", typedstream("where are you?"), 0, 1)]
    db.executemany("insert into message (text, attributedBody, handle_id, is_from_me, date) values (?, ?, 1, ?, ?)",
                   [(t, ab, fm, 784_000_000_000_000_000 + d * 10**9) for t, ab, fm, d in rows])
    sql = inbox.script(5).rstrip("\n").split('" "', 1)[1][:-1]                 # the SQL as the bash line hands it to sqlite3
    got = db.execute(sql).fetchall()
    assert [r[4] for r in got] == [typedstream(SENT).hex().upper()] * 2 + [""]  # never for a text that has its words
    msgs = inbox.parse("".join(inbox.US.join(str(v) for v in r) + inbox.RS for r in got))
    assert [(m["from_me"], m["text"]) for m in msgs] == [(True, SENT), (True, SENT), (False, "where are you?")]
    assert msgs[0]["at"] == 784_000_003 + inbox.APPLE                           # nanoseconds since 2001, as before


def test_a_message_with_no_words_says_why_so_a_blank_is_never_taken_for_what_was_said():
    rows = [(6.0, 1, THEM, "\ufffc"),                    # a picture alone: its text is only the mark where it sits
            (5.0, 1, THEM, "", typedstream("\ufffc")),   # the same, kept in the rich copy only
            (4.0, 1, THEM, "", "04ZZ"),                  # a rich copy that is not hex
            (3.0, 1, THEM, "", "040B"),                  # hex, with no words in it to find
            (2.0, 0, THEM, ""),                          # nothing stored at all: an unsent text, a group change
            (1.0, 1, THEM, "", typedstream(""))]         # a rich copy holding no words
    why = words.TEXTS_BLANK
    assert [(m["text"], m.get("blank")) for m in inbox.parse(output(rows).decode())] == [
        ("", why["file"]), ("", why["file"]), ("", why["unread"]), ("", why["unread"]), ("", why["none"]), ("", why["none"])]
    [msg] = inbox.parse(output([(7.0, 1, THEM, "the dog \ufffc at the park")]).decode())
    assert msg["text"] == "the dog \ufffc at the park" and "blank" not in msg    # words beside a picture are words


# ---- the daemon tool: owner rooms only, live when awake, the last copy when asleep -------------------------------

def setup(tmp_path: Path, mode: str = "awake", host: str = HOST, people: bool = False,
          rows: list[tuple] | None = None, monkeypatch=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    if host:
        y.conf["imessage"] = {"host": host}
    if people:
        y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    run = runner(mode, rows)
    if monkeypatch is not None:
        monkeypatch.setattr(inbox, "_run", run)
    y.add_room("example-room", object())
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-i", "one")
    return y, run


def texts(y: Yerkd, room: str = "example-room", limit: int = 0) -> dict:
    return asyncio.run(y.tool_texts(room, limit))


def test_the_tool_is_listed_in_every_room():
    assert "mcp__yerk__texts" in YERK_TOOLS


def test_a_person_room_and_an_install_with_no_mac_are_refused(tmp_path, monkeypatch):
    y, _ = setup(tmp_path, people=True, monkeypatch=monkeypatch)
    out = texts(y)
    assert out["ok"] is False and out["error"] == words.TEXTS_OWNER_ROOMS
    y2, _ = setup(tmp_path / "b", host="", monkeypatch=monkeypatch)             # no [imessage] host and no mac add-on
    out2 = texts(y2)
    assert out2["ok"] is False and out2["error"] == words.TEXTS_NO_SETUP


def test_with_the_mac_awake_the_answer_is_live_and_the_copy_is_refreshed(tmp_path, monkeypatch):
    y, run = setup(tmp_path, "awake", monkeypatch=monkeypatch)
    out = texts(y)
    assert out["ok"] and out["awake"] is True and out["as_of"] == words.TEXTS_LIVE
    assert out["messages"][0]["text"] == "running late, there by 5"
    assert run.seen and run.seen[0][0] == inbox.argv(HOST)       # it read over ssh
    assert inbox.read_copy(y.state) is not None                                 # and copied to the box


def test_the_wall_the_mac_is_asleep_so_the_answer_is_the_last_copy_and_says_its_time(tmp_path, monkeypatch):
    # the night of the ask: a copy was made while the Mac was last awake, then the lid shut and ssh died
    inbox.write_copy(tmp_path, HOST, inbox.parse(output(ROWS).decode()), at=time.time() - 360)
    y, run = setup(tmp_path, "asleep", monkeypatch=monkeypatch)
    out = texts(y)
    assert out["ok"] is True and out["awake"] is False
    assert out["messages"][0]["text"] == "running late, there by 5"  # answered, though the Mac is asleep
    assert "6 min ago" in out["as_of"] and "asleep" in out["as_of"]            # and says the copy's time, not now


def test_the_tool_answers_the_words_of_a_text_the_owner_just_sent_and_says_how_it_reads_them(tmp_path, monkeypatch):
    y, _ = setup(tmp_path, "awake", rows=[(1_762_300_300.0, 1, THEM, "", typedstream(SENT)), *ROWS], monkeypatch=monkeypatch)
    out = texts(y)
    assert out["ok"] and out["messages"][0] == {"at": 1_762_300_300.0, "from_me": True, "who": THEM, "text": SENT}
    copy = inbox.read_copy(y.state)                       # kept with its words, so a read while the Mac sleeps has them
    assert copy is not None and copy["messages"][0]["text"] == SENT
    doc = str({t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}["texts"].description)
    assert "`blank`" in doc and "never that it was empty" in doc


def test_with_the_mac_asleep_and_no_copy_yet_it_says_so_rather_than_guess(tmp_path, monkeypatch):
    y, _ = setup(tmp_path, "asleep", monkeypatch=monkeypatch)
    out = texts(y)
    assert out["ok"] is False and out["awake"] is False and out["error"] == words.TEXTS_NONE


def test_limit_defaults_to_show_and_is_capped_at_the_copy_size(tmp_path, monkeypatch):
    many = [(1_762_300_000.0 + i, i % 2, THEM, f"message {i}") for i in range(inbox.LIMIT + 20)]
    y, _ = setup(tmp_path, "awake", rows=many, monkeypatch=monkeypatch)
    assert len(texts(y)["messages"]) == inbox.SHOW                              # the default
    assert len(texts(y, limit=3)["messages"]) == 3
    assert len(texts(y, limit=999)["messages"]) == inbox.LIMIT                  # never more than the copy holds


def test_the_mac_host_is_the_imessage_host_else_the_add_on_mac(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.conf["imessage"] = {"host": HOST}
    assert y.mac_host() == HOST
    y.conf["imessage"] = {}
    assert y.mac_host() == ""                                                   # the example layer names no mac add-on
