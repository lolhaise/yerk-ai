"""The ledger, one SQLite database that records each message in and out, each turn, worker and approval, so that
why the turn moved there), inbound (every channel message by its id, before anything runs),
executed it and why the turn moved there), inbound (every channel message by its id, before anything runs), outbound
(every message Yerk AI posted, with the id the channel returned, and a card's text at each post and edit, so a card
the reply replaced can still be read back), deliveries (every outbound part by its idempotency key, written before
the post: a retry after a crash posts a part only if it is not already in the room), attachments (every inbound
missing), cards (each turn's progress message, open until its turn ends; a worker's until it
settles), holds, workers (one row per worker unit: where it runs, how it ended, whether its result
was relayed or skipped and why), and health (the daily call per account: pass or fail and the
login's identity).

Inbound states, after Hermes' delivery ledger (gateway/delivery_ledger.py, MIT): `received`
(ledgered, nothing ran: run it plainly after a crash), `running` (sent to Claude Code: after a
crash run it once more, marked as a redelivery), `answered`, `failed`, `abandoned` (redelivered
once already), `ignored`.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

COLUMNS = ("room", "account", "config_dir", "session_id", "started", "first_visible", "ended",
           "outcome", "failure", "action", "output_chars", "models", "fallback_model", "accepted", "inbound",
           "source", "hop", "recap", "turn_key")
ADDED = {"turns": {"inbound": "TEXT", "source": "TEXT", "hop": "TEXT", "recap": "INTEGER",   # newer than run 1
                   "turn_key": "TEXT"},
         "outbound": {"body": "TEXT"}}                      # a card's text, each post and edit

SCHEMA = """CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  room TEXT, account TEXT, config_dir TEXT, session_id TEXT,
  started REAL, first_visible REAL, ended REAL,
  outcome TEXT, failure TEXT, action TEXT,
  output_chars INTEGER, models TEXT, fallback_model TEXT, accepted INTEGER, inbound TEXT,
  source TEXT, hop TEXT, recap INTEGER, turn_key TEXT);
CREATE TABLE IF NOT EXISTS inbound (
  message_id TEXT PRIMARY KEY, room TEXT, channel TEXT, sender TEXT,
  received REAL, chars INTEGER, state TEXT, attempts INTEGER DEFAULT 0,
  folded INTEGER DEFAULT 0, accepted REAL, answered REAL, turn_id INTEGER, reply_ids TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS outbound (
  id INTEGER PRIMARY KEY, room TEXT, channel TEXT, kind TEXT, message_id TEXT,
  chars INTEGER, file TEXT, inbound TEXT, sent REAL, error TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS deliveries (
  key TEXT PRIMARY KEY, room TEXT, kind TEXT, body TEXT, file TEXT, size INTEGER, type TEXT, inbound TEXT,
  state TEXT, message_id TEXT, attempts INTEGER DEFAULT 0, created REAL, sent REAL, error TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY, message_id TEXT, room TEXT, name TEXT, size INTEGER, type TEXT, path TEXT,
  state TEXT, error TEXT, at REAL);
CREATE TABLE IF NOT EXISTS cards (
  message_id TEXT PRIMARY KEY, room TEXT, kind TEXT, opened REAL, closed REAL, how TEXT);
CREATE TABLE IF NOT EXISTS holds (
  id INTEGER PRIMARY KEY, room TEXT, wait_for TEXT, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY, room TEXT, account TEXT, unit TEXT, model TEXT, session_id TEXT, brief TEXT,
  staging TEXT, report TEXT, tasks TEXT, cap REAL, card TEXT, started REAL, finished REAL, exit TEXT,
  exit_detail TEXT, result TEXT, stop_requested REAL, relay TEXT, relay_note TEXT, relay_chars INTEGER);
CREATE TABLE IF NOT EXISTS health (
  id INTEGER PRIMARY KEY, at REAL, account TEXT, config_dir TEXT, ok INTEGER, failure TEXT, source TEXT,
  login TEXT, identity TEXT, identity_ok INTEGER, model TEXT, ms INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY, t REAL, room TEXT, session_id TEXT, turn TEXT, tool TEXT, target TEXT, chars INTEGER);
CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY, t REAL, room TEXT, session_id TEXT, turn TEXT, rule TEXT, detail TEXT,
  reply_chars INTEGER)"""
HEALTH = ("at", "account", "config_dir", "ok", "failure", "source", "login", "identity", "identity_ok", "model",
          "ms", "note")

WORKERS = ("room", "account", "unit", "model", "session_id", "brief", "staging", "report", "tasks", "cap", "card",
           "started", "finished", "exit", "exit_detail", "result", "stop_requested", "relay", "relay_note",
           "relay_chars")
INBOUND = ("room", "channel", "sender", "received", "chars", "state", "attempts", "folded", "accepted",
           "answered", "turn_id", "reply_ids", "note")
OPEN = ("received", "running")


class Ledger:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)
        for table, cols in ADDED.items():              # an older ledger gains the newer columns
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, kind in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        with self.lock:
            cur = self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                                  tuple(row.values()))
        return int(cur.lastrowid or 0)

    def evidence(self, **row: Any) -> int:
        """One tool call of a turn and what it read (L185)."""
        return self._insert("evidence", row)

    def shadow(self, **row: Any) -> int:
        """One thing the shadow Stop check would have bounced; nothing was blocked."""
        return self._insert("shadow", row)

    def record(self, **row: Any) -> int:
        bad = set(row) - set(COLUMNS)
        if bad:
            raise ValueError(f"unknown ledger columns: {sorted(bad)}")
        return self._insert("turns", row)

    def rows(self, limit: int = 20, table: str = "turns") -> list[dict[str, Any]]:
        order = "received" if table == "inbound" else "id"
        return self._select(f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT ?", (limit,))

    def _select(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self.lock:
            cur = self.db.execute(sql, args)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    # inbound
    def inbound_add(self, message_id: str, room: str, channel: str, sender: str, chars: int) -> bool:
        """Ledger a channel message before anything runs. False when it is already ledgered."""
        try:
            self._insert("inbound", {"message_id": message_id, "room": room, "channel": channel, "sender": sender,
                                     "received": time.time(), "chars": chars, "state": "received"})
        except sqlite3.IntegrityError:
            return False
        return True

    def inbound_set(self, message_id: str, **fields: Any) -> None:
        bad = set(fields) - set(INBOUND)
        if bad:
            raise ValueError(f"unknown inbound columns: {sorted(bad)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE inbound SET {sets} WHERE message_id = ?", (*fields.values(), message_id))

    def inbound_get(self, message_id: str) -> dict[str, Any] | None:
        r = self._select("SELECT * FROM inbound WHERE message_id = ?", (message_id,))
        return r[0] if r else None

    def inbound_open(self, room: str | None = None) -> list[dict[str, Any]]:
        """Rows a crash left received or running, oldest first."""
        sql = f"SELECT * FROM inbound WHERE state IN ({', '.join('?' * len(OPEN))})"
        args: tuple[Any, ...] = OPEN
        if room:
            sql, args = sql + " AND room = ?", (*OPEN, room)
        return self._select(sql + " ORDER BY received", args)

    def last_inbound(self, channel: str) -> str | None:
        r = self._select("SELECT message_id FROM inbound WHERE channel = ? ORDER BY CAST(message_id AS INTEGER) DESC "
                         "LIMIT 1", (channel,))
        return str(r[0]["message_id"]) if r else None

    def last_received(self, room: str) -> float | None:
        """The last message a person (or the test webhook) sent this room; the harness's own are not."""
        r = self._select("SELECT max(received) AS t FROM inbound WHERE room = ? AND sender != 'yerk'", (room,))
        return float(r[0]["t"]) if r and r[0]["t"] is not None else None

    # health
    def health(self, **row: Any) -> int:
        bad = set(row) - set(HEALTH)
        if bad:
            raise ValueError(f"unknown health columns: {sorted(bad)}")
        return self._insert("health", {"at": time.time(), **row})

    def health_last(self, account: str) -> dict[str, Any] | None:
        r = self._select("SELECT * FROM health WHERE account = ? ORDER BY id DESC LIMIT 1", (account,))
        return r[0] if r else None

    # workers (workers.py)
    def worker_add(self, **row: Any) -> None:
        bad = set(row) - {"id", *WORKERS}
        if bad:
            raise ValueError(f"unknown worker columns: {sorted(bad)}")
        self._insert("workers", row)

    def worker_set(self, worker_id: str, **fields: Any) -> None:
        bad = set(fields) - set(WORKERS)
        if bad:
            raise ValueError(f"unknown worker columns: {sorted(bad)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE workers SET {sets} WHERE id = ?", (*fields.values(), worker_id))

    def worker_get(self, worker_id: str) -> dict[str, Any] | None:
        r = self._select("SELECT * FROM workers WHERE id = ?", (worker_id,))
        return r[0] if r else None

    def workers(self, running: bool = False, limit: int = 1000) -> list[dict[str, Any]]:
        """Running ones (no `finished`) oldest first; else the newest `limit`."""
        if running:
            return self._select("SELECT * FROM workers WHERE finished IS NULL ORDER BY started", ())
        return self._select("SELECT * FROM workers ORDER BY started DESC LIMIT ?", (limit,))

    # outbound and holds
    def outbound(self, **row: Any) -> int:
        return self._insert("outbound", {"sent": time.time(), **row})

    # deliveries: one row per outbound part, keyed by its turn and part index, before the post
    def delivery_plan(self, key: str, room: str, kind: str, body: str = "", inbound: str = "",
                      **extra: Any) -> dict[str, Any]:
        """The part's row, written once: a second plan for the same key returns the first row."""
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO deliveries (key, room, kind, body, inbound, state, created"
                            f"{''.join(', ' + k for k in extra)}) VALUES (?, ?, ?, ?, ?, 'pending', ?"
                            f"{', ?' * len(extra)})", (key, room, kind, body, inbound, time.time(), *extra.values()))
        return self._select("SELECT * FROM deliveries WHERE key = ?", (key,))[0]

    def delivery_set(self, key: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE deliveries SET {sets} WHERE key = ?", (*fields.values(), key))

    def deliveries(self, room: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM deliveries WHERE 1", tuple[Any, ...]()
        if room:
            sql, args = sql + " AND room = ?", (*args, room)
        if state:
            sql, args = sql + " AND state = ?", (*args, state)
        return self._select(sql + " ORDER BY created, key", args)

    def files_sent_for(self, message_ids: list[str]) -> list[dict[str, Any]]:
        """Files already in the room for any of these inbound messages (a turn a crash cut short)."""
        rows = self._select("SELECT * FROM deliveries WHERE kind = 'file' AND state = 'sent' AND inbound != '' "
                            "ORDER BY created", ())
        want = set(message_ids)
        return [r for r in rows if want & set(str(r["inbound"] or "").split(","))]

    def delivered_for(self, message_id: str) -> list[str]:
        """The message ids a turn answering `message_id` posted, from the delivery ledger ([] if none)."""
        turns = self._select("SELECT turn_key FROM turns WHERE ',' || inbound || ',' LIKE ? AND outcome = 'ok'",
                             (f"%,{message_id},%",))
        keys = {str(t["turn_key"]) for t in turns if t["turn_key"]}
        rows = self._select("SELECT key, message_id FROM deliveries WHERE state = 'sent' ORDER BY created, key", ())
        return [str(r["message_id"]) for r in rows if r["key"].rsplit(":", 1)[0] in keys]

    def turn_for_key(self, turn_key: str) -> int | None:
        r = self._select("SELECT id FROM turns WHERE turn_key = ? ORDER BY id DESC LIMIT 1", (turn_key,))
        return int(r[0]["id"]) if r else None

    # attachments and cards
    def attachment(self, **row: Any) -> int:
        return self._insert("attachments", {"at": time.time(), **row})

    def attachments(self, message_id: str) -> list[dict[str, Any]]:
        return self._select("SELECT * FROM attachments WHERE message_id = ? ORDER BY id", (message_id,))

    def card_open(self, message_id: str, room: str, kind: str) -> None:
        self._insert("cards", {"message_id": message_id, "room": room, "kind": kind, "opened": time.time()})

    def card_close(self, message_id: str, how: str) -> None:
        with self.lock:
            self.db.execute("UPDATE cards SET closed = ?, how = ? WHERE message_id = ?", (time.time(), how, message_id))

    def cards_open(self, room: str) -> list[dict[str, Any]]:
        return self._select("SELECT * FROM cards WHERE room = ? AND closed IS NULL ORDER BY opened", (room,))

    def hold(self, room: str, wait_for: str, reason: str) -> int:
        return self._insert("holds", {"room": room, "wait_for": wait_for, "reason": reason, "at": time.time()})
