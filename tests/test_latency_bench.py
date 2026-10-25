"""Checks `yerk bench latency` (latency.py), which reports how fast the rooms answered, on a fixture state with example values: the
day's medians on the status card and in the morning diff, and Ledger.dead_turns against the query it replaced. Ledger.dead_turns
was rewritten as lookups on Oct 24 (64 s for a day of messages on the live ledger); example values are decision 22."""
import gzip
import json
import shutil
import sqlite3
import time

import pytest

from yerk_ai import latency, morning, status, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.ledger import Ledger

NOW = 1792303200.0
H = 3600.0

# dead_turns as it was at 30ea8dc: the rewrite must return its rows
OLD = ("SELECT i.message_id, i.room, i.received FROM inbound i WHERE i.received >= ? AND i.received < ? AND "
       "i.sender != 'yerk' AND i.state != 'ignored' AND NOT EXISTS (SELECT 1 FROM turns t JOIN deliveries d ON "
       "d.state = 'sent' AND (d.key LIKE t.turn_key || ':%' OR d.key LIKE t.turn_key || '-s%') WHERE "
       "t.turn_key != '' AND (t.id = i.turn_id OR CAST(t.id AS TEXT) IN (SELECT row_key FROM message_links WHERE "
       "tbl = 'turns' AND col = 'inbound' AND message_id = i.message_id))) AND NOT EXISTS (SELECT 1 FROM "
       "message_links l JOIN outbound o ON l.row_key = CAST(o.id AS TEXT) WHERE l.tbl = 'outbound' AND "
       "l.col = 'inbound' AND l.message_id = i.message_id AND o.message_id IS NOT NULL AND o.kind NOT IN "
       "('progress', 'progress-edit', 'worker-card')) ORDER BY i.received")


def message(led, mid, at, room="example-room", sender="owner", state="answered"):
    led.inbound_add(mid, room, "discord", sender, 2)
    led.inbound_set(mid, received=at, state=state)


def turn(led, key, inbound, outcome="ok", sent=None, **row):
    """A turn that took `inbound` (comma-joined), named on its first message; `sent`: a part's key posted under it."""
    tid = led.record(room="example-room", outcome=outcome, turn_key=key, inbound=inbound, **row)
    led.inbound_set(inbound.split(",")[0], turn_id=tid)
    if sent:
        led.delivery_plan(sent, "example-room", "reply", "hi", inbound)
        led.delivery_set(sent, state="sent", message_id=f"r-{sent}")
    return tid


def dead_fixture(led):
    t = NOW - H
    message(led, "a", t)                                       # a reply part under its turn's key
    turn(led, "aaaaaaaaaaaa", "a", sent="aaaaaaaaaaaa:0")
    message(led, "b", t + 60)                                  # only a side part, `<key>-s1`
    turn(led, "bbbbbbbbbbbb", "b", sent="bbbbbbbbbbbb-s1")
    message(led, "c1", t + 120)                                # one turn took two; c2 reaches it through its link alone
    message(led, "c2", t + 125)
    turn(led, "cccccccccccc", "c1,c2", sent="cccccccccccc:0")
    message(led, "d", t + 180)                                 # its part planned, never sent: dead, between two turns whose
    turn(led, "dddddddddddd", "d", outcome="failed")           # parts were, so a bound that widens past its key reads it answered
    led.delivery_plan("dddddddddddd:0", "example-room", "reply", "hi", "d")
    message(led, "e", t + 240)                                 # a visible notice linked to it, no turn
    led.outbound(room="example-room", channel="c", kind="notice", message_id="n1", chars=5, inbound="e")
    message(led, "g", t + 270)
    turn(led, "eeeeeeeeeeee", "g", sent="eeeeeeeeeeee:0")
    message(led, "f", t + 300)                                 # a progress post only: dead
    turn(led, "ffffffffffff", "f", outcome="failed")
    led.outbound(room="example-room", channel="c", kind="progress", message_id="p1", chars=5, inbound="f")
    message(led, "h", t + 360, sender="yerk")                  # the harness's own
    message(led, "i", t + 420, state="ignored")                # deleted by its sender
    message(led, "j", t + 480, state="received")               # never taken: dead
    message(led, "k", t + 540)                                 # a post linked to it that never got an id: dead
    led.outbound(room="example-room", channel="c", kind="reply", message_id=None, chars=5, inbound="k")


def test_dead_turns_returns_the_replaced_querys_rows(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    dead_fixture(led)
    new = led.dead_turns(NOW - 86400, NOW)
    assert new == led._select(OLD, (NOW - 86400, NOW))
    assert [d["message_id"] for d in new] == ["d", "f", "j", "k"]
    assert led.dead_turns(NOW - H + 150, NOW - H + 290) == led._select(OLD, (NOW - H + 150, NOW - H + 290))   # a window's edges


def test_dead_turns_finds_each_turn_part_and_post_by_an_index(tmp_path):
    """Per message, a turn, its links, a part and a post are each a lookup: only the window's messages and the two-row range
    table are scanned (the old form scanned every turn for every sent part of every message)."""
    led, seen = Ledger(tmp_path / "ledger.sqlite"), []
    led._select = lambda sql, args: seen.append((sql, args)) or []
    led.dead_turns(NOW - 86400, NOW)
    plan = [r[3] for r in led.db.execute("EXPLAIN QUERY PLAN " + seen[0][0], seen[0][1])]
    scans = [p for p in plan if p.startswith("SCAN")]
    assert scans and all(p.split()[1] in ("i", "k") or "CONSTANT ROW" in p for p in scans), plan
    for table in ("t", "d", "o", "l", "message_links"):
        assert not any(p.startswith(f"SCAN {table} ") or p == f"SCAN {table}" for p in plan), plan
    links = [p for p in plan if "message_links" in p or p.startswith("SEARCH l ")]     # by the message, never every link of a table
    assert links and all(p.endswith("(message_id=?)") for p in links), plan


def bench_state(tmp_path):
    state = tmp_path / "state"
    (state / "rooms" / "example-room").mkdir(parents=True)
    (state / "logs").mkdir()
    led = Ledger(state / "ledger.sqlite")
    for mid, at, first, length in (("m1", NOW - 3 * H, 2, 10), ("m2", NOW - 2 * H, 4, 20), ("m3", NOW - H, 8, 40)):
        message(led, mid, at)
        led.inbound_set(mid, turn_id=led.record(room="example-room", outcome="ok", inbound=mid, started=at + 1,
                                                first_visible=at + first, ended=at + 1 + length))
    q = NOW - 1800                                             # a turn that took two messages, counted once
    message(led, "q1", q, room="example-quiet")
    message(led, "q2", q + 5, room="example-quiet")
    tid = led.record(room="example-quiet", outcome="ok", inbound="q1,q2", started=q + 3, first_visible=q + 30, ended=q + 103)
    for mid in ("q1", "q2"):
        led.inbound_set(mid, turn_id=tid)
    message(led, "old", NOW - 8 * 86400)                       # outside the window
    led.inbound_set("old", turn_id=led.record(room="example-room", outcome="ok", inbound="old", started=NOW - 8 * 86400,
                                              first_visible=NOW - 8 * 86400 + 99, ended=NOW - 8 * 86400 + 999))
    for i, (ev, ms, key) in enumerate((("UserPromptSubmit", 100, "k1"), ("UserPromptSubmit", 200, "k2"), ("UserPromptSubmit", 300, "k3"),
                                       ("PostToolUse", 50, "k1"), ("PostToolUse", 50, "k2"))):
        led.db.execute("INSERT INTO hook_calls (t, room, turn, event, ms) VALUES (?, 'example-room', ?, ?, ?)", (NOW - H + i, key, ev, ms))
    for tool, ms in (("mcp__yerk__recall", 16), ("mcp__yerk__send", 30), ("Bash", 900)):
        led.db.execute("INSERT INTO tool_times (tool_use_id, t, room, tool, ms) VALUES (?, ?, 'example-room', ?, ?)", (tool, NOW - H, tool, ms))
    led.db.commit()
    led.close()
    room = state / "rooms" / "example-room"
    (room / "assembled.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
        {"t": NOW - 3 * H, "event": "message", "recall_ms": 120}, {"t": NOW - 2 * H, "event": "message", "recall_ms": 80},
        {"t": NOW - H, "event": "reply", "recall_ms": 999}, {"t": NOW - 9 * 86400, "event": "message", "recall_ms": 999})))
    with gzip.open(room / "assembled.2026-10-17.jsonl.gz", "wt") as f:    # rolled by retention inside the window: read
        f.write(json.dumps({"t": NOW - 4 * H, "event": "message", "recall_ms": 100}) + "\n")
    with gzip.open(room / "assembled.2026-09-01.jsonl.gz", "wt") as f:    # rolled before the window: not opened
        f.write(json.dumps({"t": NOW - 4 * H, "event": "message", "recall_ms": 5000}) + "\n")

    def result(t, inp, read, made):
        return json.dumps({"t": t, "e": {"type": "result", "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                                                     "cache_creation_input_tokens": made}}}) + "\n"
    (state / "logs" / "example-room.jsonl").write_text(result(NOW - 3 * H, 10, 90, 0) + result(NOW - 2 * H, 20, 60, 20) + json.dumps(
        {"t": NOW - H, "e": {"type": "assistant", "usage": {"input_tokens": 1000}}}) + "\n")
    (state / "logs" / "example-quiet.jsonl").write_text(result(q, 0, 100, 0))
    return state


def test_bench_reads_the_fixture_into_one_table_and_diffs_the_earlier_file(tmp_path):
    state = bench_state(tmp_path)
    before = (state / "ledger.sqlite").read_bytes()
    rows = {(r["what"], r["room"]): r for r in latency.bench(state, 7, NOW)["rows"]}
    assert rows["first", "all"] == {"what": "first", "room": "all", "n": 5, "median": 8, "p90": 30, "p95": 30}
    assert (rows["first", "example-room"]["median"], rows["first", "example-quiet"]["median"]) == (4, 27.5)
    assert (rows["wait", "all"]["median"], rows["wait", "example-quiet"]["n"], rows["wait", "example-quiet"]["median"]) == (1, 2, 1.5)    # folded: 0, not -2
    assert rows["turn", "all"] == {"what": "turn", "room": "all", "n": 4, "median": 30, "p90": 100, "p95": 100}
    assert (rows["hook", "example-room"]["median"], rows["daemon", "example-room"]["median"]) == (200, 250)
    assert (rows["tools", "all"]["n"], rows["tools", "all"]["median"]) == (2, 23)
    assert (rows["recall", "example-room"]["n"], rows["recall", "example-room"]["median"]) == (3, 100)
    assert [(rows["cache", r]["n"], rows["cache", r]["median"]) for r in ("all", "example-room", "example-quiet")] == [(3, 83.3), (2, 75.0), (1, 100.0)]
    lines, out = latency.run(state, 7, now=NOW)
    assert out == state / "bench" / time.strftime("latency-%Y-%m-%d.json", time.localtime(NOW)) and json.loads(out.read_text())["was"] is None
    assert lines[2].split() == ["first", "line,", "s", "all", "5", "8.0", "30.0", "30.0", "-"]
    later, out2 = latency.run(state, 7, now=NOW + 86400)
    assert json.loads(out2.read_text())["was"] == out.name and later[0].endswith(f"was: the median in {out.name}")
    assert later[2].split()[-1] == "8.0" and [x for x in later if x.startswith("not measured: ")] == [f"not measured: {x}" for x in latency.UNMEASURED]
    assert (state / "ledger.sqlite").read_bytes() == before        # the ledger only read
    with pytest.raises(sqlite3.OperationalError):
        Ledger.read_only(state / "ledger.sqlite").db.execute("DELETE FROM inbound")


def test_the_card_and_the_morning_carry_the_days_medians(tmp_path):
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    state.mkdir()
    led = Ledger(state / "ledger.sqlite")
    for mid, day, first, length in (("y", 1, 6, 12), ("b", 2, 9, 20), ("t", 0, 3, 5)):    # yesterday, the day before, today
        at = status.midnight(NOW, day) + 600
        message(led, mid, at)
        led.inbound_set(mid, turn_id=led.record(room="example-room", outcome="ok", inbound=mid, started=at,
                                                first_visible=at + first, ended=at + length))
    assert latency.medians(led, status.midnight(NOW), NOW) == {"first": 3, "turn": 5}
    assert latency.medians(led, status.midnight(NOW) - 1, status.midnight(NOW)) == {"first": None, "turn": None}
    s = status.collect(priv, state, load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"), led, now=NOW)
    card = status.card({**s, "alive": False, "commit": "x"}, NOW)
    assert words.line(f"{words.PLAIN} {words.SPEED.format(day=words.TODAY, first=words.secs(3), turn=words.secs(5))}") in card
    assert not any("first line" in x for x in status.card({**s, "alive": False, "commit": "x", "speed": {"first": None, "turn": None}}, NOW))
    lines, _ = morning.build(priv, state, load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"), led, None, NOW)
    for what, now, was in (("first line", 6, 9), ("turn", 12, 20)):
        assert words.line(f"{words.PLAIN} {words.SPEED_WAS.format(what=what, now=words.secs(now), was=words.secs(was))}") in lines
