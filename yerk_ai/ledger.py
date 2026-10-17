"""The run ledger: one row per turn, with the account that executed it."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

COLUMNS = ("room", "account", "config_dir", "session_id", "started", "first_visible", "ended",
           "outcome", "failure", "action", "output_chars", "models", "fallback_model", "accepted")

SCHEMA = """CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  room TEXT, account TEXT, config_dir TEXT, session_id TEXT,
  started REAL, first_visible REAL, ended REAL,
  outcome TEXT, failure TEXT, action TEXT,
  output_chars INTEGER, models TEXT, fallback_model TEXT, accepted INTEGER)"""


class Ledger:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute(SCHEMA)

    def record(self, **row: Any) -> int:
        bad = set(row) - set(COLUMNS)
        if bad:
            raise ValueError(f"unknown ledger columns: {sorted(bad)}")
        cols = ", ".join(row)
        cur = self.db.execute(f"INSERT INTO turns ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        return int(cur.lastrowid or 0)

    def rows(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self.db.execute("SELECT * FROM turns ORDER BY id DESC LIMIT ?", (limit,))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
