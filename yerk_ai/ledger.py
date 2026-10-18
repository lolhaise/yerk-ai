"""The ledgers, one SQLite file: turns (one row per turn, with the account that executed it),
inbound (every channel message by its id, before anything runs), outbound (every message Yerk AI
posted, with the id the channel returned) and holds.

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
           "outcome", "failure", "action", "output_chars", "models", "fallback_model", "accepted", "inbound")

SCHEMA = """CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  room TEXT, account TEXT, config_dir TEXT, session_id TEXT,
  started REAL, first_visible REAL, ended REAL,
  outcome TEXT, failure TEXT, action TEXT,
  output_chars INTEGER, models TEXT, fallback_model TEXT, accepted INTEGER, inbound TEXT);
CREATE TABLE IF NOT EXISTS inbound (
  message_id TEXT PRIMARY KEY, room TEXT, channel TEXT, sender TEXT,
  received REAL, chars INTEGER, state TEXT, attempts INTEGER DEFAULT 0,
  folded INTEGER DEFAULT 0, accepted REAL, answered REAL, turn_id INTEGER, reply_ids TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS outbound (
  id INTEGER PRIMARY KEY, room TEXT, channel TEXT, kind TEXT, message_id TEXT,
  chars INTEGER, file TEXT, inbound TEXT, sent REAL, error TEXT);
CREATE TABLE IF NOT EXISTS holds (
  id INTEGER PRIMARY KEY, room TEXT, wait_for TEXT, reason TEXT, at REAL)"""

INBOUND = ("room", "channel", "sender", "received", "chars", "state", "attempts", "folded", "accepted",
           "answered", "turn_id", "reply_ids", "note")
OPEN = ("received", "running")


class Ledger:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(turns)")}
        if "inbound" not in have:                      # a run 1 ledger
            self.db.execute("ALTER TABLE turns ADD COLUMN inbound TEXT")

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        with self.lock:
            cur = self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                                  tuple(row.values()))
        return int(cur.lastrowid or 0)

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

    # outbound and holds
    def outbound(self, **row: Any) -> int:
        return self._insert("outbound", {"sent": time.time(), **row})

    def hold(self, room: str, wait_for: str, reason: str) -> int:
        return self._insert("holds", {"room": room, "wait_for": wait_for, "reason": reason, "at": time.time()})
