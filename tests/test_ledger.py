"""Checks the schema versions and migrations of the ledger, the small database of what ran: a ledger from before versions moves to the current one
to the current one in order, ids become rows, order is by the stored time, the indexes exist, a rename
is one idempotent step. Every value here is a placeholder."""
import sqlite3

from yerk_ai import ledger as L


def old_ledger(path):
    """A ledger as the code before versions wrote it: the tables, comma text, user_version 0."""
    db = sqlite3.connect(path, isolation_level=None)
    db.executescript(L.SCHEMA.split("CREATE TABLE IF NOT EXISTS message_links")[0].rstrip().rstrip(";"))
    L.add_columns(db)
    db.execute("INSERT INTO inbound (message_id, room, channel, sender, received, state, reply_ids) VALUES "
               "('m1', 'r', 'c', 'owner', 100.0, 'answered', 'a,b')")
    db.execute("INSERT INTO inbound (message_id, room, channel, sender, received, state) VALUES ('9', 'r', 'c', 'owner', 200.0, 'answered')")
    db.execute("INSERT INTO inbound (message_id, room, channel, sender, received, state) VALUES ('10', 'r', 'c', 'owner', 150.0, 'answered')")
    db.execute("INSERT INTO turns (room, inbound, outcome, turn_key) VALUES ('r', 'm1', 'ok', 'k1')")
    db.execute("INSERT INTO deliveries (key, room, kind, inbound, state, message_id, created) VALUES "
               "('k1:1', 'r', 'file', 'm1', 'sent', 'o1', 1.0)")
    db.execute("INSERT INTO job_runs (job, message_ids) VALUES ('j', 'x,y,z')")
    db.close()


def test_a_ledger_from_before_versions_migrates_in_order_once(tmp_path):
    old_ledger(tmp_path / "l.sqlite")
    assert sqlite3.connect(tmp_path / "l.sqlite").execute("PRAGMA user_version").fetchone()[0] == 0
    led = L.Ledger(tmp_path / "l.sqlite")
    assert led.migrated == [1, 2, 3] and led.version() == L.VERSION == 3
    assert led.links("inbound", "m1", "reply_ids") == ["a", "b"] and led.links("job_runs", 1, "message_ids") == ["x", "y", "z"]
    assert led.delivered_for("m1") == ["o1"] and [r["key"] for r in led.files_sent_for(["m1"])] == ["k1:1"]
    assert led.files_sent_for([]) == [] and led.delivered_for("nobody") == []
    again = L.Ledger(tmp_path / "l.sqlite")
    assert again.migrated == [] and again.version() == 3
    names = {r[0] for r in again.db.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")}
    assert {"inbound_room_received", "deliveries_state_key", "message_links_message", "turns_turn_key"} <= names
    assert again.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"             # pull request 12's change stays


def test_order_is_by_the_stored_time_never_by_casting_an_id(tmp_path):
    old_ledger(tmp_path / "l.sqlite")
    led = L.Ledger(tmp_path / "l.sqlite")
    assert led.last_inbound("c") == "9"                      # received 200 > 150, though 10 > 9 as a number
    led.inbound_add("im-abc", "r", "c", "owner", 1, created=300.0)   # a channel whose ids are not numbers
    assert led.last_inbound("c") == "im-abc"
    led.inbound_add("late-replay", "r", "c", "owner", 1, created=50.0)  # received now, made long ago
    assert led.last_inbound("c") == "im-abc"


def test_every_write_of_an_id_list_writes_its_rows(tmp_path):
    led = L.Ledger(tmp_path / "l.sqlite")
    led.inbound_add("m2", "r", "c", "owner", 1)
    led.inbound_set("m2", reply_ids="p,q")
    assert led.links("inbound", "m2", "reply_ids") == ["p", "q"]
    led.inbound_set("m2", reply_ids="q")
    assert led.links("inbound", "m2", "reply_ids") == ["q"]
    tid = led.record(room="r", inbound="m2,m3", outcome="ok", turn_key="k2")
    assert led.links("turns", tid, "inbound") == ["m2", "m3"]
    led.delivery_plan("k2:1", "r", "text", inbound="m2")
    led.delivery_set("k2:1", state="sent", message_id="o2")
    led.delivery_plan("k2:2", "r", "text", inbound="m3")
    led.delivery_set("k2:2", state="sent", message_id="o3")
    led.delivery_plan("k2:1", "r", "text", inbound="other")          # a second plan changes nothing
    assert led.links("deliveries", "k2:1", "inbound") == ["m2"] and led.delivered_for("m3") == ["o2", "o3"]
    rid = led.job_run_add(job="j", kind="script")
    led.job_run_set(rid, message_ids="a,b")
    assert led.links("job_runs", rid, "message_ids") == ["a", "b"]
    eid = led.watch_add(job="w", room="r", state="pending")
    led.watch_set(eid, reply_ids="c", inbound="m9")
    assert led.links("watch_events", eid, "reply_ids") == ["c"] and led.links("watch_events", eid, "inbound") == ["m9"]
