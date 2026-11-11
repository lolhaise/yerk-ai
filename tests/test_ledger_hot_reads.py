"""Checks that the reads of the ledger, the database of what ran, on every turn cost what they return, never the room's history. Audit of 2026-11-09.

Measured on the live ledger that day: Ledger.said, run by the shadow Stop check on every Stop under the ledger's one lock,
took 1,017 ms in a room 2,564 turns into its session (every reply of the room against every turn of the session, a LIKE
per pair), and 911 of the 953 loop stalls logged in 7.7 days overlap a Stop hook; discord._deliver read the room's whole
deliveries (4,394 rows, 14.65 ms on the event loop) to post one reply. The tests count sqlite's own steps (its progress
handler), never seconds, so a slow box never fails them and a fast one never hides a scan. Every value is a placeholder."""
from yerk_ai.ledger import Ledger

EVERY = 50      # sqlite virtual-machine steps per tick of the counter


def steps(led, call):
    """How much work sqlite did for `call`, in ticks of EVERY steps, and what it returned."""
    n = [0]

    def tick():
        n[0] += 1
        return 0
    led.db.set_progress_handler(tick, EVERY)
    try:
        out = call()
    finally:
        led.db.set_progress_handler(None, EVERY)
    return n[0], out


def room_with(tmp_path, turns, name="led.sqlite"):
    """A room `turns` turns into one session, a reply posted for each, after an older session just as long."""
    led = Ledger(tmp_path / name)
    led.db.execute("BEGIN")
    for sid, base in (("old", 0), ("now", 100000)):
        for i in range(turns):
            key = f"{sid}{i:09d}"                     # twelve characters, as a turn's key is
            led.db.execute("INSERT INTO turns (room, session_id, started, outcome, turn_key) VALUES ('r', ?, ?, 'ok', ?)", (sid, base + i, key))
            led.db.execute("INSERT INTO deliveries (key, room, kind, body, state, created) VALUES (?, 'r', 'reply', ?, 'sent', ?)",
                           (f"{key}:0", f"reply {sid} {i}", base + i + 0.5))
    led.db.execute("COMMIT")
    return led


def test_said_is_the_sessions_replies_in_the_order_posted_and_nothing_else(tmp_path):
    led = room_with(tmp_path, 3)
    led.db.execute("INSERT INTO turns (room, session_id, started, outcome, turn_key) VALUES ('r', 'now', 100001, 'error', 'now000000001')")   # a rerun: the key twice
    led.db.execute("INSERT INTO turns (room, session_id, started, outcome, turn_key) VALUES ('r', 'now', 100009, 'compacted', NULL)")       # a row with no key
    for key, room, kind, state, created in (("now000000001:1", "r", "reply", "sent", 100001.6),       # the second part of a reply
                                            ("now000000001-s1:0", "r", "send", "sent", 100001.7),      # a send: never read
                                            ("now000000001:hooks:0", "r", "notice", "sent", 100001.8),  # a notice: never read
                                            ("now000000002:1", "r", "reply", "failed", 100002.6),      # never landed
                                            ("now000000000:0x", "other", "reply", "sent", 100000.7)):   # another room's
        led.db.execute("INSERT INTO deliveries (key, room, kind, body, state, created) VALUES (?, ?, ?, ?, ?, ?)", (key, room, kind, key, state, created))
    assert led.said("r", "now", 10**9).split("\n") == ["reply now 0", "reply now 1", "now000000001:1", "reply now 2"]
    assert led.said("r", "now", 100001.55).split("\n") == ["reply now 0", "reply now 1"]      # before the turn's start only
    assert led.said("r", "old", 10**9).split("\n") == ["reply old 0", "reply old 1", "reply old 2"]
    assert led.said("r", "none", 10**9) == "" and led.said("other", "now", 10**9) == ""


def test_said_costs_the_sessions_replies_not_the_rooms_replies_times_the_sessions_turns(tmp_path):
    few, a = steps(led1 := room_with(tmp_path, 100, "b.sqlite"), lambda: led1.said("r", "now", 10**9))
    many, b = steps(led4 := room_with(tmp_path, 400, "c.sqlite"), lambda: led4.said("r", "now", 10**9))
    assert len(a.split("\n")) == 100 and len(b.split("\n")) == 400
    assert many <= 8 * max(few, 1), f"four times the session cost {many / max(few, 1):.1f} times the work ({few} -> {many} ticks): the read grows with the square"


def test_landed_and_one_replys_parts_never_read_the_rooms_history(tmp_path):
    led1, led2 = room_with(tmp_path, 50, "a.sqlite"), room_with(tmp_path, 2000, "b.sqlite")
    for led in (led1, led2):
        led.delivery_plan("k1:0", "r", "reply", "one")
        led.delivery_plan("k1:1", "r", "reply", "two")
        led.delivery_plan("k1:hooks:0", "r", "notice", "the hooks line")      # a longer key under the same start: another delivery
        led.delivery_plan("k1-s1:0", "r", "send", "a send")
        led.delivery_plan("k1:0x", "elsewhere", "reply", "another room's")
        for key in ("k1:0", "k1-s1:0"):
            led.delivery_set(key, state="sent")
    a, n1 = steps(led1, lambda: led1.landed("k1"))
    b, n2 = steps(led2, lambda: led2.landed("k1"))
    assert n1 == n2 == 2 and led1.landed("") == 0 and led1.landed("nokey") == 0
    assert b <= a + 2, f"landed() did {a} ticks of work in a room of 100 replies and {b} in one of 4,000"
    a, rows1 = steps(led1, lambda: led1.deliveries_of("r", "k1"))
    b, rows2 = steps(led2, lambda: led2.deliveries_of("r", "k1"))
    assert [r["key"] for r in rows1] == [r["key"] for r in rows2] == ["k1:0", "k1:1"]
    assert [r["body"] for r in rows1] == ["one", "two"] and led1.deliveries_of("r", "nokey") == []
    assert b <= a + 2, f"one reply's parts took {a} ticks in a room of 100 replies and {b} in one of 4,000"


def test_last_reply_reads_its_own_parts_never_the_other_rooms_replies(tmp_path):
    """Ledger.last_reply runs on the event loop for each message the owner sends in a room that keeps taste (taste.py): its
    parts are read by key range, so the other rooms' sent replies cost it nothing (a LIKE on the key read every sent row)."""
    def ledger(name, others):
        led = Ledger(tmp_path / name)
        led.db.execute("BEGIN")
        for i in range(others):
            led.db.execute("INSERT INTO deliveries (key, room, kind, body, state, created, sent) VALUES (?, 'other', 'reply', 'x', 'sent', ?, ?)",
                           (f"o{i:011d}:0", i, i))
        for key, kind, body, at in (("k0:0", "reply", "older", 1.0), ("k1:0", "reply", "one", 5.0), ("k1:1", "reply", "two", 6.0),
                                    ("k1:hooks:0", "notice", "the hooks line", 7.0)):     # a longer key under the same start: not a part
            led.db.execute("INSERT INTO deliveries (key, room, kind, body, state, created, sent, message_id) VALUES (?, 'r', ?, ?, 'sent', ?, ?, ?)",
                           (key, kind, body, at, at, f"m-{key}"))
        led.db.execute("COMMIT")
        return led
    led1, led2 = ledger("a.sqlite", 0), ledger("b.sqlite", 4000)
    a, r1 = steps(led1, lambda: led1.last_reply("r"))
    b, r2 = steps(led2, lambda: led2.last_reply("r"))
    assert r1 == r2 == {"id": "m-k1:0", "sent": 5.0, "text": "one\ntwo"} and led1.last_reply("none") is None
    assert b <= a + 2, f"the room's last reply took {a} ticks beside no other room's replies and {b} beside 4,000"
