"""The ledger, one SQLite database that records each message in and out, each turn, worker and approval, so that
why the turn moved there), inbound (every channel message by its id, before anything runs),
executed it and why the turn moved there), inbound (every channel message by its id, before anything runs), outbound
(every message Yerk AI posted, with the id the channel returned, and a card's text at each post and edit, so a card
the reply replaced can still be read back), deliveries (every outbound part by its idempotency key, written before
the post: a retry after a crash posts a part only if it is not already in the room), attachments (every inbound
missing), cards (each turn's progress message, open until its turn ends; a worker's until it
settles), holds, workers (one row per worker unit: where it runs, how it ended, whether its result
skipped and why), health (the daily call per account: pass or fail and the login's identity), and the gates' own
(gates.py): every gate decision, the approval holds, each hook call's time, each tool call's time and failure, and
the tamper rows of a changed setting; stalls (beat.py: each time the event loop was held more than 2 s, with its
stalls (beat.py: each time the event loop ran more than 2 s late, with its dump).

Inbound states, after Hermes' delivery ledger (gateway/delivery_ledger.py, MIT): `received`
(ledgered, nothing ran: run it plainly after a crash), `running` (sent to Claude Code: after a
crash run it once more, marked as a redelivery), `answered`, `failed`, `abandoned` (redelivered
once already), `ignored`.
"""
from __future__ import annotations

import math
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
         "outbound": {"body": "TEXT"},                      # a card's text, each post and edit
         "workers": {"job": "TEXT"},                        # the scheduled job a worker runs for (jobs.py)
         "evidence": {"tool_use_id": "TEXT"},               # joins a call to its time (tool_times)
         "approvals": {"call": "TEXT", "cwd": "TEXT"}}      # the held call an approval releases, exactly

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
  exit_detail TEXT, result TEXT, stop_requested REAL, relay TEXT, relay_note TEXT, relay_chars INTEGER, job TEXT);
CREATE TABLE IF NOT EXISTS job_runs (
  id INTEGER PRIMARY KEY, job TEXT, kind TEXT, room TEXT, unit TEXT, planned REAL, fired REAL, ended REAL,
  exit TEXT, result TEXT, delivery TEXT, message_ids TEXT, worker TEXT, chars INTEGER, folder TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS watch_events (
  id INTEGER PRIMARY KEY, job TEXT, room TEXT, wake TEXT, state TEXT, lines TEXT, keys TEXT, received REAL,
  attempts INTEGER DEFAULT 0, woke REAL, ended REAL, worker TEXT, inbound TEXT, reply_ids TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS health (
  id INTEGER PRIMARY KEY, at REAL, account TEXT, config_dir TEXT, ok INTEGER, failure TEXT, source TEXT,
  login TEXT, identity TEXT, identity_ok INTEGER, model TEXT, ms INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY, t REAL, room TEXT, session_id TEXT, turn TEXT, tool TEXT, target TEXT, chars INTEGER);
CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY, t REAL, room TEXT, session_id TEXT, turn TEXT, rule TEXT, detail TEXT,
  reply_chars INTEGER);
CREATE TABLE IF NOT EXISTS gates (
  id INTEGER PRIMARY KEY, t REAL, room TEXT, session_id TEXT, turn TEXT, gate TEXT, decision TEXT, tool TEXT,
  rule TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY, room TEXT, rule TEXT, what TEXT, held REAL, approved REAL, approved_by TEXT,
  released REAL, gate_id INTEGER, call TEXT, cwd TEXT);
CREATE TABLE IF NOT EXISTS hook_calls (
  id INTEGER PRIMARY KEY, t REAL, room TEXT, session_id TEXT, turn TEXT, event TEXT, ms REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS tool_times (
  tool_use_id TEXT PRIMARY KEY, t REAL, room TEXT, session_id TEXT, turn TEXT, tool TEXT, ms INTEGER,
  failed INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS tamper (
  id INTEGER PRIMARY KEY, t REAL, who TEXT, room TEXT, session_id TEXT, source TEXT, file TEXT, sha_before TEXT,
  sha_after TEXT, diff_sha TEXT, diff_lines INTEGER);
CREATE TABLE IF NOT EXISTS stalls (
  id INTEGER PRIMARY KEY, began REAL, seconds REAL, doing TEXT, dump TEXT)"""
HEALTH = ("at", "account", "config_dir", "ok", "failure", "source", "login", "identity", "identity_ok", "model",
          "ms", "note")

WORKERS = ("room", "account", "unit", "model", "session_id", "brief", "staging", "report", "tasks", "cap", "card",
           "started", "finished", "exit", "exit_detail", "result", "stop_requested", "relay", "relay_note",
           "relay_chars", "job", "queued", "waiting", "watch")
JOB_RUNS = ("job", "kind", "room", "unit", "planned", "fired", "ended", "exit", "result", "delivery", "message_ids",
            "worker", "chars", "folder", "note")
ADDED["workers"] = {**ADDED["workers"], "queued": "REAL", "waiting": "INTEGER",   # in line for its account
                    "watch": "INTEGER"}                                          # the watcher event it woke for
WATCH_EVENTS = ("job", "room", "wake", "state", "lines", "keys", "received", "attempts", "woke", "ended", "worker",
                "inbound", "reply_ids", "note")
INBOUND = ("room", "channel", "sender", "received", "chars", "state", "attempts", "folded", "accepted",
           "answered", "turn_id", "reply_ids", "note")
OPEN = ("received", "running")


class Ledger:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        # WAL with NORMAL sync: a commit no longer waits on a disk sync (10 to 400 ms each on this box
        # under load, paid by every hook call); a crash still leaves the file consistent
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
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

    # the gates (gates.py)
    def gate(self, **row: Any) -> int:
        """One gate decision: a rewrite, a refusal, a hold or a release."""
        return self._insert("gates", {"t": time.time(), **row})

    def hold_add(self, room: str, rule: str, what: str, gate_id: int, call: str = "", cwd: str = "") -> int:
        return self._insert("approvals", {"room": room, "rule": rule, "what": what, "held": time.time(),
                                          "gate_id": gate_id, "call": call, "cwd": cwd})

    def approve(self, room: str, by: str, window_s: float) -> dict[str, Any] | None:
        """The owner's word: the room's newest hold not yet approved (inside the window) is approved."""
        r = self._select("SELECT * FROM approvals WHERE room = ? AND approved IS NULL AND held > ? ORDER BY id DESC "
                         "LIMIT 1", (room, time.time() - window_s))
        if not r:
            return None
        with self.lock:
            self.db.execute("UPDATE approvals SET approved = ?, approved_by = ? WHERE id = ?",
                            (time.time(), by, r[0]["id"]))
        return r[0]

    def release(self, room: str, rule: str, call: str, cwd: str, window_s: float) -> dict[str, Any] | None:
        """The approved hold of exactly this call (its normalised text and working folder), not yet used,
        takes it through once; another call under the same rule is not released by it."""
        r = self._select("SELECT * FROM approvals WHERE room = ? AND rule = ? AND call = ? AND cwd = ? AND approved > ? "
                         "AND released IS NULL ORDER BY id DESC LIMIT 1", (room, rule, call, cwd, time.time() - window_s))
        if not r:
            return None
        with self.lock:
            self.db.execute("UPDATE approvals SET released = ? WHERE id = ?", (time.time(), r[0]["id"]))
        return r[0]

    def many(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        """Timing rows for hook_calls and tool_times in one transaction: one disk sync per batch, never
        one per hook call (a commit each cost 10 to 80 ms here under load)."""
        if not rows:
            return
        with self.lock:
            self.db.execute("BEGIN")
            try:
                for table, row in rows:
                    verb = "INSERT OR REPLACE" if table == "tool_times" else "INSERT"
                    self.db.execute(f"{verb} INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                                    tuple(row.values()))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def tamper(self, **row: Any) -> int:
        """A setting file changed under a running session: who, what, when and the diff's hash."""
        return self._insert("tamper", row)

    def answered_in(self, session_id: str) -> int:
        """Turns a session answered (a session that never answered loses nothing when it is replaced)."""
        r = self._select("SELECT count(*) AS n FROM turns WHERE session_id = ? AND outcome = 'ok'", (session_id,))
        return int(r[0]["n"])

    def landed(self, turn_key: str) -> int:
        """Parts already in the room under a turn's key (its reply parts, its sends)."""
        if not turn_key:
            return 0
        r = self._select("SELECT count(*) AS n FROM deliveries WHERE state = 'sent' AND (key LIKE ? OR key LIKE ?)",
                         (f"{turn_key}:%", f"{turn_key}-s%"))
        return int(r[0]["n"])

    def evidence_timed(self, turn: str) -> list[dict[str, Any]]:
        """The turn's evidence rows with each call's time and failure (None until its PostToolUse)."""
        return self._select("SELECT e.*, t.ms, t.failed, t.error FROM evidence e LEFT JOIN tool_times t "
                            "ON e.tool_use_id = t.tool_use_id WHERE e.turn = ? ORDER BY e.id", (turn,))

    def hook_overhead(self, since: float = 0.0) -> dict[str, Any]:
        """The hooks' own time per turn (every event of the turn summed): turns, calls, p50, p95, max."""
        per = [float(r["ms"]) for r in self._select(
            "SELECT turn, sum(ms) AS ms FROM hook_calls WHERE t >= ? AND turn != '' GROUP BY turn ORDER BY ms",
            (since,))]
        calls = self._select("SELECT count(*) AS n FROM hook_calls WHERE t >= ?", (since,))[0]["n"]

        def pct(q: float) -> float | None:                  # nearest rank
            return round(per[max(0, math.ceil(q * len(per)) - 1)], 1) if per else None
        return {"turns": len(per), "calls": int(calls), "p50_ms": pct(0.5), "p95_ms": pct(0.95),
                "max_ms": round(per[-1], 1) if per else None}

    # observability (beat.py, status.py, morning.py)
    def stall(self, **row: Any) -> int:
        return self._insert("stalls", row)

    def stalls(self, since: float, until: float = math.inf) -> list[dict[str, Any]]:
        return self._select("SELECT * FROM stalls WHERE began >= ? AND began < ? ORDER BY began", (since, until))

    def waits(self, since: float, until: float) -> list[dict[str, Any]]:
        """Each person's message in the window joined to the turn that answered it: `wait`, the turn's
        start after the message (0 when it folded into a running turn), and `first`, the turn's first
        visible line after the message (None when there was none, or it came before the message)."""
        return self._select("SELECT max(0, t.started - i.received) AS wait, CASE WHEN t.first_visible >= i.received "
                            "THEN t.first_visible - i.received END AS first FROM inbound i JOIN turns t ON "
                            "i.turn_id = t.id WHERE i.received >= ? AND i.received < ? AND i.sender != 'yerk'",
                            (since, until))

    def shadow_rules(self, since: float, until: float) -> dict[str, int]:
        rows = self._select("SELECT rule, count(*) AS n FROM shadow WHERE t >= ? AND t < ? GROUP BY rule", (since, until))
        return {str(r["rule"]): int(r["n"]) for r in rows}

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

    # job runs (jobs.py): one row per fire of a scheduled job
    def job_run_add(self, **row: Any) -> int:
        bad = set(row) - set(JOB_RUNS)
        if bad:
            raise ValueError(f"unknown job_runs columns: {sorted(bad)}")
        return self._insert("job_runs", row)

    def job_run_set(self, run_id: int, **fields: Any) -> None:
        bad = set(fields) - set(JOB_RUNS)
        if bad:
            raise ValueError(f"unknown job_runs columns: {sorted(bad)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE job_runs SET {sets} WHERE id = ?", (*fields.values(), run_id))

    def job_run_get(self, run_id: int) -> dict[str, Any] | None:
        r = self._select("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        return r[0] if r else None

    # watcher events (jobs.py): one row per event a watcher's command printed, and one "primed" row
    # holding the lines its first start absorbed; state pending, waking, handled, failed, stopped (its
    # worker stopped by the owner: final) or primed
    def watch_add(self, **row: Any) -> int:
        bad = set(row) - set(WATCH_EVENTS)
        if bad:
            raise ValueError(f"unknown watch_events columns: {sorted(bad)}")
        return self._insert("watch_events", row)

    def watch_set(self, event_id: int, **fields: Any) -> None:
        bad = set(fields) - set(WATCH_EVENTS)
        if bad:
            raise ValueError(f"unknown watch_events columns: {sorted(bad)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE watch_events SET {sets} WHERE id = ?", (*fields.values(), event_id))

    def watch_get(self, event_id: int) -> dict[str, Any] | None:
        r = self._select("SELECT * FROM watch_events WHERE id = ?", (event_id,))
        return r[0] if r else None

    def watch_events(self, job: str | None = None, states: tuple[str, ...] = (), limit: int = 2000) -> list[dict[str, Any]]:
        """Oldest first: one watcher's events (all when `job` is None), only `states` when given."""
        sql, args = "SELECT * FROM watch_events WHERE 1", tuple[Any, ...]()
        if job:
            sql, args = sql + " AND job = ?", (*args, job)
        if states:
            sql, args = sql + f" AND state IN ({', '.join('?' * len(states))})", (*args, *states)
        rows = self._select(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return rows[::-1]

    def job_run_for_worker(self, worker_id: str) -> dict[str, Any] | None:
        r = self._select("SELECT * FROM job_runs WHERE worker = ? ORDER BY id DESC LIMIT 1", (worker_id,))
        return r[0] if r else None

    def job_runs(self, job: str | None = None, limit: int = 50, undelivered: bool = False) -> list[dict[str, Any]]:
        """The newest runs (of one job when named); `undelivered`: runs whose delivery waits on a daemon."""
        sql, args = "SELECT * FROM job_runs WHERE 1", tuple[Any, ...]()
        if job:
            sql, args = sql + " AND job = ?", (*args, job)
        if undelivered:
            sql = sql + " AND delivery LIKE 'undelivered:%'"
        return self._select(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))

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
