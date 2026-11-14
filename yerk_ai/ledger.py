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
dump); deploys (deploy.py: each deploy and rollback, the commit before and after, and whether a person's turn was
cut); spend (each paid call); calls (each call a room made in a reply, with the cites its turn read, and its fix:
claim 19).

Inbound states, after Hermes' delivery ledger (gateway/delivery_ledger.py, MIT): `received`
(ledgered, nothing ran: run it plainly after a crash), `running` (sent to Claude Code: after a
crash run it once more, marked as a redelivery), `answered`, `failed`, `abandoned` (redelivered
once already), `ignored`, `stopped` (the owner's /stop: never run again).

The schema is versioned (PRAGMA user_version) and moves by the ordered MIGRATIONS list, each step a
function that may add, rename or backfill and is run once, inside one transaction, with the version
written at its end (restart test R4). Lists of message ids live as rows in `message_links` (one row
per id, with its place in the list), written beside the comma text the older readers still split;
the hot lookups read the rows.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

COLUMNS = ("room", "account", "config_dir", "session_id", "started", "first_visible", "ended",
           "outcome", "failure", "action", "output_chars", "models", "fallback_model", "accepted", "inbound",
           "source", "hop", "recap", "turn_key", "model", "out_of", "context", "note")
ADDED = {"turns": {"inbound": "TEXT", "source": "TEXT", "hop": "TEXT", "recap": "INTEGER",   # newer than run 1
                   "turn_key": "TEXT", "model": "TEXT", "out_of": "TEXT",   # the model asked for; the pinned one every account was out of
                   "context": "INTEGER",                    # the session's tokens at the turn's last call (the idle compaction's)
                   "note": "TEXT"},                         # a copy's take or drop, said (Room._precompact); COMPACTED_IN_TURN
         "outbound": {"body": "TEXT"},                      # a card's text, each post and edit
         "workers": {"job": "TEXT"},                        # the scheduled job a worker runs for (jobs.py)
         "evidence": {"tool_use_id": "TEXT"},               # joins a call to its time (tool_times)
         "approvals": {"call": "TEXT", "cwd": "TEXT"},      # the held call an approval releases, exactly
         "inbound": {"created": "REAL"}}                   # the message's own time on its channel (ordering)
COMPACTED_IN_TURN = "compacted in the turn"   # the note on an ordinary turn's row inside which Claude Code compacted the session

SCHEMA = """CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  room TEXT, account TEXT, config_dir TEXT, session_id TEXT,
  started REAL, first_visible REAL, ended REAL,
  outcome TEXT, failure TEXT, action TEXT,
  output_chars INTEGER, models TEXT, fallback_model TEXT, accepted INTEGER, inbound TEXT,
  source TEXT, hop TEXT, recap INTEGER, turn_key TEXT, model TEXT, out_of TEXT, context INTEGER);
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
  id INTEGER PRIMARY KEY, began REAL, seconds REAL, doing TEXT, dump TEXT);
CREATE TABLE IF NOT EXISTS deploys (
  id INTEGER PRIMARY KEY, at REAL, kind TEXT, unit TEXT, before TEXT, after TEXT, outcome TEXT, cut INTEGER,
  proof TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS spend (
  id INTEGER PRIMARY KEY, t REAL, room TEXT, tool TEXT, engine TEXT, usd REAL, note TEXT);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY, t REAL, room TEXT, session_id TEXT, turn TEXT, whom TEXT, call TEXT, reads TEXT, no_reads INTEGER,
  fixed REAL, source TEXT, correction TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS texting (
  id INTEGER PRIMARY KEY, t REAL, person TEXT, handle TEXT, inbound TEXT, decision TEXT, parts TEXT, react TEXT,
  reply_to TEXT, statements TEXT, verdict TEXT, delay REAL, fire_at REAL, state TEXT, reason TEXT,
  approval_id INTEGER, card TEXT, handed REAL, claimed TEXT, account TEXT, seconds REAL, tokens INTEGER, mode TEXT);
CREATE TABLE IF NOT EXISTS message_links (
  tbl TEXT, row_key TEXT, col TEXT, message_id TEXT, pos INTEGER,
  PRIMARY KEY (tbl, row_key, col, message_id))"""
# the comma-joined id columns and the key column of their table: every write of one also writes its rows
LINKED = {("turns", "inbound"): "id", ("inbound", "reply_ids"): "message_id", ("outbound", "inbound"): "id",
          ("deliveries", "inbound"): "key", ("job_runs", "message_ids"): "id", ("texting", "inbound"): "id"}
INDEXES = """CREATE INDEX IF NOT EXISTS inbound_room_received ON inbound (room, received);
CREATE INDEX IF NOT EXISTS inbound_channel_created ON inbound (channel, created, received);
CREATE INDEX IF NOT EXISTS inbound_state ON inbound (state);
CREATE INDEX IF NOT EXISTS turns_session_outcome ON turns (session_id, outcome);
CREATE INDEX IF NOT EXISTS turns_turn_key ON turns (turn_key);
CREATE INDEX IF NOT EXISTS deliveries_state_key ON deliveries (state, key);
CREATE INDEX IF NOT EXISTS deliveries_room_created ON deliveries (room, created);
CREATE INDEX IF NOT EXISTS approvals_room_held ON approvals (room, held, approved, released);
CREATE INDEX IF NOT EXISTS hook_calls_t_turn ON hook_calls (t, turn);
CREATE INDEX IF NOT EXISTS evidence_turn ON evidence (turn);
CREATE INDEX IF NOT EXISTS workers_finished_started ON workers (finished, started);
CREATE INDEX IF NOT EXISTS job_runs_job_id ON job_runs (job, id);
CREATE INDEX IF NOT EXISTS job_runs_worker ON job_runs (worker);
CREATE INDEX IF NOT EXISTS cards_room_closed ON cards (room, closed);
CREATE INDEX IF NOT EXISTS health_account_id ON health (account, id);
CREATE INDEX IF NOT EXISTS message_links_message ON message_links (message_id, tbl)"""
HEALTH = ("at", "account", "config_dir", "ok", "failure", "source", "login", "identity", "identity_ok", "model", "ms", "note")

WORKERS = ("room", "account", "unit", "model", "session_id", "brief", "staging", "report", "tasks", "cap", "card",
           "started", "finished", "exit", "exit_detail", "result", "stop_requested", "relay", "relay_note",
           "relay_chars", "job", "queued", "waiting")
JOB_RUNS = ("job", "kind", "room", "unit", "planned", "fired", "ended", "exit", "result", "delivery", "message_ids",
            "worker", "chars", "folder", "note")
TEXTING = ("person", "handle", "inbound", "decision", "parts", "react", "reply_to", "statements", "verdict", "delay",
           "fire_at", "state", "reason", "approval_id", "card", "handed", "claimed", "account", "seconds", "tokens", "mode",
           "shown")
ADDED["workers"] = {**ADDED["workers"], "queued": "REAL", "waiting": "INTEGER"}   # in line for its account
ADDED["texting"] = {"shown": "TEXT"}       # a held text's card as it was last shown (texting/holds.py)
ADDED["inbound"] = {**ADDED["inbound"], "digest": "TEXT"}   # a text's words, hashed: the sender's recheck (texting/)
INBOUND = ("room", "channel", "sender", "received", "chars", "state", "attempts", "folded", "accepted",
           "answered", "turn_id", "reply_ids", "note", "created", "digest")
OPEN = ("received", "running")


def ids_of(text: Any) -> list[str]:
    """The ids in a comma-joined column, in order, empties dropped."""
    return [x for x in str(text or "").split(",") if x]


def known(cols: dict[str, Any], allowed: tuple[str, ...] | set[str], table: str) -> None:
    """Every column named is one `table` has: the names go into the SQL text, so an unknown one fails loudly."""
    if bad := set(cols) - set(allowed):
        raise ValueError(f"unknown {table} columns: {sorted(bad)}")


# the migrations, in order: (version, what it does). Each runs once, inside one transaction, on a
# ledger whose user_version is below it; the version is written as the step's last statement.
def add_columns(db: sqlite3.Connection) -> None:
    """Every column newer than a table's first shape (add-only, idempotent: the first migration, and
    the one run on every open so a column added in the code is there before anything reads it)."""
    for table, cols in ADDED.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for col, kind in cols.items():
            if col not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], Any]]] = [
    (1, "the run-1 tables and every column added since", add_columns),
    (3, "the indexes the hot queries need", lambda db: [db.execute(x) for x in INDEXES.split(";") if x.strip()]),
]


class Ledger:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        # WAL with NORMAL sync: a commit no longer waits on a disk sync (10 to 400 ms each on this box
        # under load, paid by every hook call); a crash still leaves the file consistent
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")    # the CLI, the units and the daemon all write it
        if (v := self.version()) > MIGRATIONS[-1][0]:   # a downgrade: refused with one line before anything is written
            raise SystemExit(f"the ledger {path} is schema version {v}, newer than this code's {MIGRATIONS[-1][0]}: run the "
                             "version that wrote it, or restore the ledger from a backup; nothing was changed")
        self.db.executescript(SCHEMA)
        add_columns(self.db)                           # a column the code gained is there before any read
        self.migrated = self.migrate()

    def close(self) -> None:
        """The connection closed now, not when the collector gets to it: CPython keeps a connection in a reference
        cycle (its statement cache is bound to it), and a WAL database's -wal and -shm files, made at the umask's mode,
        stay beside it until its last connection closes (the doctor's modes check counted its own, 2026-10-21)."""
        self.db.close()

    @classmethod
    def read_only(cls, path: Path) -> Ledger:
        """The ledger opened ?mode=ro, no schema or migration run: a reader that cannot write (yerk bench latency)."""
        led = cls.__new__(cls)
        led.db, led.lock, led.migrated = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False), threading.Lock(), []
        return led

    def version(self) -> int:
        return int(self.db.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> list[int]:
        """Run every migration above the ledger's version, each in its own transaction; returns the
        versions applied. A ledger from before versions (user_version 0) starts at the first."""
        done = []
        for n, what, step in MIGRATIONS:
            if self.version() >= n:
                continue
            with self.lock:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    step(self.db)
                    self.db.execute(f"PRAGMA user_version = {int(n)}")
                    self.db.execute("COMMIT")
                except Exception:
                    self.db.execute("ROLLBACK")
                    raise
            done.append(n)
        return done

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        with self.lock:
            cur = self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
            rid = int(cur.lastrowid or 0)
            self._link(table, row, rid)
        return rid

    def _link(self, table: str, row: dict[str, Any], rid: int | None = None, key: Any = None) -> None:
        """The rows of every comma-joined id column in `row` (under the lock already)."""
        for (tbl, col), keycol in LINKED.items():
            if tbl != table or col not in row:
                continue
            row_key = key if key is not None else (row.get(keycol) if keycol != "id" else rid)
            self.db.execute("DELETE FROM message_links WHERE tbl = ? AND row_key = ? AND col = ?", (table, str(row_key), col))
            for pos, mid in enumerate(ids_of(row[col])):
                self.db.execute("INSERT OR IGNORE INTO message_links (tbl, row_key, col, message_id, pos) VALUES (?, ?, ?, ?, ?)",
                                (table, str(row_key), col, mid, pos))

    def evidence(self, **row: Any) -> int:
        """One tool call of a turn and what it read (L185)."""
        return self._insert("evidence", row)

    def shadow(self, **row: Any) -> int:
        """One thing the shadow Stop check would have bounced, nothing blocked; or a `stub` bounce, which was."""
        return self._insert("shadow", row)

    # the gates (gates.py)
    def gate(self, **row: Any) -> int:
        """One gate decision: a rewrite, a refusal, a hold or a release."""
        return self._insert("gates", {"t": time.time(), **row})

    def hold_add(self, room: str, rule: str, what: str, gate_id: int, call: str = "", cwd: str = "") -> int:
        return self._insert("approvals", {"room": room, "rule": rule, "what": what, "held": time.time(),
                                          "gate_id": gate_id, "call": call, "cwd": cwd})

    def approve(self, room: str, by: str, window_s: float, before: float | None = None) -> dict[str, Any] | None:
        """The owner's word: the room's newest hold not yet approved (inside the window) is approved. `before`, when the word
        reached Yerk: a hold made after it is never approved by it, since nobody had read that hold when the word was written
        (a word folded into a running turn reaches the gate after the turn's next calls)."""
        r = self._select("SELECT * FROM approvals WHERE room = ? AND approved IS NULL AND held > ? AND held <= ? AND rule != 'text-thread' "
                         "ORDER BY id DESC LIMIT 1", (room, time.time() - window_s, time.time() if before is None else before))
        if not r:
            return None
        self._update("approvals", "id", r[0]["id"], {"approved": time.time(), "approved_by": by})
        return r[0]

    def release(self, room: str, rule: str, call: str, cwd: str | None, window_s: float) -> dict[str, Any] | None:
        """The approved hold of exactly this call (its normalised text, and its working folder unless `cwd` is None:
        gates.bound_to), not yet used, takes it through once; another call under the same rule is not released by it."""
        where, folder = ("AND cwd = ? ", (cwd,)) if cwd is not None else ("", ())
        r = self._select(f"SELECT * FROM approvals WHERE room = ? AND rule = ? AND call = ? {where}AND approved > ? "
                         "AND released IS NULL ORDER BY id DESC LIMIT 1", (room, rule, call, *folder, time.time() - window_s))
        if not r:
            return None
        self._update("approvals", "id", r[0]["id"], {"released": time.time()})
        return r[0]

    def approval(self, hold_id: int) -> dict[str, Any] | None:
        return self._first("SELECT * FROM approvals WHERE id = ?", (hold_id,))

    def hold_approve(self, hold_id: int, by: str) -> None:
        """One hold approved by its id (a text hold, by the owner's word on its card), never the room's newest."""
        self._update("approvals", "id", hold_id, {"approved": time.time(), "approved_by": by})

    def holds_open(self, room: str, since: float) -> list[dict[str, Any]]:
        """The room's holds made after `since` and not approved yet: what a word said there could still approve."""
        return self._select("SELECT * FROM approvals WHERE room = ? AND approved IS NULL AND held > ? ORDER BY id", (room, since))

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
                    self.db.execute(f"{verb} INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
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
        """Parts already in the room under a turn's key (its reply parts, its sends). Read by key range (`key:` up to
        `key;`, `key-s` up to `key-t`), so it costs what the turn posted, never every sent row there is (a LIKE on the key
        cannot use its index: sqlite's LIKE ignores case and the key's index does not)."""
        if not turn_key:
            return 0
        one = "(SELECT count(*) FROM deliveries WHERE key >= ? AND key < ? AND +state = 'sent')"      # +: the key's own index
        r = self._select(f"SELECT {one} + {one} AS n", (f"{turn_key}:", f"{turn_key};", f"{turn_key}-s", f"{turn_key}-t"))
        return int(r[0]["n"])

    def said(self, room: str, session_id: str, before: float) -> str:
        """The replies `room` posted in session `session_id` before `before`, its turn's start: what the Stop check read.
        Its narration before a tool call, its sends, captions and notes were never checked, so they are not here.
        Each turn of the session finds its own parts by key range, so the read costs the session's replies. It was every
        reply of the room against every turn of the session, a LIKE per pair, under the ledger's one lock: 1,017 ms a
        Stop in a room 2,564 turns into its session (measured 2026-11-09 on the live ledger), and the event loop's next
        ledger call waited behind it (911 of 953 logged loop stalls in 7.7 days overlap a Stop hook)."""
        rows = self._select("SELECT d.body AS body, d.created AS created, d.rowid AS n FROM (SELECT DISTINCT turn_key FROM turns "
                            "WHERE session_id = ? AND room = ? AND turn_key IS NOT NULL AND turn_key != '') t CROSS JOIN deliveries d "
                            "ON d.key >= t.turn_key || ':' AND d.key < t.turn_key || ';' WHERE +d.room = ? AND d.kind = 'reply' "
                            "AND +d.state = 'sent' AND +d.created < ?", (session_id, room, room, before))   # +: by the key, never the room's rows
        rows.sort(key=lambda r: (r["created"], r["n"]))        # the order the room posted them in, as before
        return "\n".join(str(r["body"]) for r in rows)

    def deliveries_of(self, room: str, key: str) -> list[dict[str, Any]]:
        """The parts of one delivery (`key:0`, `key:1`, ...), in order, by key range: what a post of one reply needs. A
        room's whole history (deliveries(room): 4,394 rows and 1.9 million characters in a busy room, 14.65 ms
        on the event loop for every reply) is never read for it. A longer key under the same start
        (`key:hooks:0`, a notice's) is another delivery and is left out, as the callers' own test left it out."""
        rows = self._select("SELECT * FROM deliveries WHERE key >= ? AND key < ? AND +room = ?", (f"{key}:", f"{key};", room))
        rows.sort(key=lambda r: (r["created"] or 0, r["key"]))
        return [r for r in rows if r["key"].rsplit(":", 1)[0] == key]

    # observability (beat.py, status.py, morning.py)
    def spend(self, **row: Any) -> int:
        """One paid call (room, tool, engine, usd, note), so the morning diff carries the day's spend (decision 32)."""
        return self._insert("spend", {"t": time.time(), **row})

    def stall(self, **row: Any) -> int:
        return self._insert("stalls", row)

    def stalls(self, since: float, until: float = math.inf) -> list[dict[str, Any]]:
        return self._select("SELECT * FROM stalls WHERE began >= ? AND began < ? ORDER BY began", (since, until))

    def deploy(self, row_id: int = 0, **row: Any) -> int:
        """A deploys row; given one's id, its columns set instead (deploy.py writes the row before the
        restart, outcome "begun", and sets it once the proof is in)."""
        if not row_id:
            return self._insert("deploys", {"at": time.time(), **row})
        known(row, {"kind", "unit", "before", "after", "outcome", "cut", "proof", "note"}, "deploys")
        self._update("deploys", "id", row_id, row)
        return row_id

    def deploys(self, since: float) -> list[dict[str, Any]]:
        return self._select("SELECT * FROM deploys WHERE at >= ? ORDER BY at, id", (since,))

    def dead_turns(self, since: float, until: float) -> list[dict[str, Any]]:
        """A person's messages in the window with no reply, notice or file in the room for them: no part
        sent under the key of a turn that took them, and no visible post linked to them (L192). Every join is a
        lookup, so the cost is per message, not messages times turns times parts (64 s for a day on Oct 24): a turn
        by its id (i.turn_id, or a link's row_key, which _link writes as the id's text), a part by the key range
        under the turn's key (`<key>:` to `<key>;`, `<key>-s` to `<key>-t`, the keys LIKE matched: a turn key is
        lowercase hex), a post by its id; the unary + keeps the planner off the link table's (tbl, ...) key."""
        return self._select(
            "SELECT i.message_id, i.room, i.received FROM inbound i WHERE i.received >= ? AND i.received < ? AND "
            "i.sender != 'yerk' AND i.state != 'ignored' AND NOT EXISTS (SELECT 1 FROM turns t JOIN (SELECT ':' AS lo, "
            "';' AS hi UNION ALL SELECT '-s', '-t') k JOIN deliveries d ON d.state = 'sent' AND d.key >= t.turn_key || k.lo "
            "AND d.key < t.turn_key || k.hi WHERE t.turn_key != '' AND t.id IN (SELECT i.turn_id UNION ALL SELECT "
            "CAST(row_key AS INTEGER) FROM message_links WHERE message_id = i.message_id AND +tbl = 'turns' AND "
            "col = 'inbound')) AND NOT EXISTS (SELECT 1 FROM message_links l JOIN outbound o ON o.id = CAST(l.row_key "
            "AS INTEGER) WHERE l.message_id = i.message_id AND +l.tbl = 'outbound' AND l.col = 'inbound' AND "
            "o.message_id IS NOT NULL AND o.kind NOT IN ('progress', 'progress-edit', 'worker-card')) "
            "ORDER BY i.received", (since, until))

    def waits(self, since: float, until: float) -> list[dict[str, Any]]:
        """Each person's message in the window joined to the turn that answered it: `wait`, the turn's
        start after the message (0 when it folded into a running turn), `first`, the turn's first
        visible line after the message (None when there was none, or it came before the message), and the
        turn's `turn` length (None while it runs), `turn_id` and `room` (a turn that took three messages is one turn)."""
        return self._select("SELECT max(0, t.started - i.received) AS wait, CASE WHEN t.first_visible >= i.received "
                            "THEN t.first_visible - i.received END AS first, t.ended - t.started AS turn, t.id AS turn_id, "
                            "i.room FROM inbound i JOIN turns t ON i.turn_id = t.id WHERE i.received >= ? AND "
                            "i.received < ? AND i.sender != 'yerk'", (since, until))

    def timings(self, table: str, since: float, until: float) -> list[dict[str, Any]]:
        """The hook_calls or tool_times rows timed in the window (yerk bench latency)."""
        known({table: 1}, ("hook_calls", "tool_times"), "timing")
        return self._select(f"SELECT * FROM {table} WHERE t >= ? AND t < ?", (since, until))

    def shadow_rules(self, since: float, until: float) -> dict[str, int]:
        rows = self._select("SELECT rule || CASE rule WHEN 'stance' THEN ' ' || detail ELSE '' END AS rule, count(*) AS n "
                            "FROM shadow WHERE t >= ? AND t < ? GROUP BY 1", (since, until))     # stance by its label
        return {str(r["rule"]): int(r["n"]) for r in rows}

    def call(self, **row: Any) -> int:
        """One call a room made in a reply (claim 19): whom it was for, its sentence, the cites its turn read (JSON), none flagged."""
        return self._insert("calls", row)

    def calls(self, since: float, until: float) -> list[dict[str, Any]]:
        """The calls made in the window and those fixed in it, oldest first."""
        return self._select("SELECT * FROM calls WHERE t >= ? AND t < ? OR fixed >= ? AND fixed < ? ORDER BY id", (since, until, since, until))

    def call_row(self, cid: int) -> dict[str, Any] | None:
        return self._first("SELECT * FROM calls WHERE id = ?", (cid,))

    def call_fix(self, cid: int, **fix: Any) -> None:
        """`yerk calls fix`: when, the source line it names, the correction's cite and its note."""
        known(fix, ("fixed", "source", "correction", "note"), "calls")
        self._update("calls", "id", cid, fix)

    def spent(self, since: float, until: float) -> dict[str, Any]:
        """{n, usd}: the paid calls booked in the window (spend), as billed or at list price (decision 32)."""
        return self._first("SELECT count(*) AS n, coalesce(sum(usd), 0) AS usd FROM spend WHERE t >= ? AND t < ?", (since, until)) or {}

    def record(self, **row: Any) -> int:
        known(row, COLUMNS, "ledger")
        return self._insert("turns", row)

    def last_context(self, session_id: str) -> int:
        """The session's tokens at its newest turn that said them (0: none), so the idle compaction knows them after a restart."""
        r = self._first("SELECT context FROM turns WHERE session_id = ? AND context IS NOT NULL ORDER BY id DESC LIMIT 1", (session_id,))
        return int(r["context"]) if r else 0

    def compactions(self, since: float) -> dict[str, int]:
        """Each room's compactions of its session ended since `since` (the room card's broom): a held or idle /compact and a
        copy taken (outcome 'compacted'), and Claude Code's own inside a turn (that turn's note, COMPACTED_IN_TURN)."""
        rows = self._select("SELECT room, count(*) AS n FROM turns WHERE ended >= ? AND (outcome = 'compacted' OR note = ?) "
                            "GROUP BY room", (since, COMPACTED_IN_TURN))
        return {str(r["room"]): int(r["n"]) for r in rows}

    def turn_summary(self, since: float) -> tuple[dict[str, float], list[dict[str, Any]]]:
        """(each room's last answered turn's end, the failed turns since `since` by room, outcome and class with a count)."""
        ok = self._select("SELECT room, max(ended) AS ended FROM turns WHERE outcome = 'ok' GROUP BY room", ())
        failed = self._select("SELECT room, outcome, failure, count(*) AS n FROM turns WHERE started >= ? AND outcome NOT IN ('ok', 'compacted', 'dropped') "
                              "GROUP BY room, outcome, failure ORDER BY room", (since,))
        return {str(r["room"]): float(r["ended"]) for r in ok if r["ended"] is not None}, failed

    def rows(self, limit: int = 20, table: str = "turns") -> list[dict[str, Any]]:
        order = "received" if table == "inbound" else "id"
        return self._select(f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT ?", (limit,))

    def _select(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self.lock:
            cur = self.db.execute(sql, args)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def _first(self, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        return next(iter(self._select(sql, args)), None)

    def _update(self, table: str, where: str, key: Any, fields: dict[str, Any]) -> None:
        """`fields` (names the caller checked) set on the row whose `where` is `key`, with the ids they link."""
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE {table} SET {sets} WHERE {where} = ?", (*fields.values(), key))
            self._link(table, fields, key=key)

    # inbound
    def inbound_add(self, message_id: str, room: str, channel: str, sender: str, chars: int, created: float | None = None) -> bool:
        """Ledger a channel message before anything runs (`created`: its own time on the channel, the
        order messages are read back in). False when it is already ledgered."""
        try:
            self._insert("inbound", {"message_id": message_id, "room": room, "channel": channel, "sender": sender,
                                     "received": time.time(), "chars": chars, "state": "received",
                                     "created": created})
        except sqlite3.IntegrityError:
            return False
        return True

    def inbound_set(self, message_id: str, **fields: Any) -> None:
        known(fields, INBOUND, "inbound")
        self._update("inbound", "message_id", message_id, fields)

    def inbound_get(self, message_id: str) -> dict[str, Any] | None:
        return self._first("SELECT * FROM inbound WHERE message_id = ?", (message_id,))

    def inbound_open(self, room: str | None = None) -> list[dict[str, Any]]:
        """Rows a crash left received or running, oldest first."""
        sql = f"SELECT * FROM inbound WHERE state IN ({', '.join('?' * len(OPEN))})"
        args: tuple[Any, ...] = OPEN
        if room:
            sql, args = sql + " AND room = ?", (*OPEN, room)
        return self._select(sql + " ORDER BY received", args)

    def last_inbound(self, channel: str) -> str | None:
        """The channel's newest message by its stored time (its own, else when it was received): an id
        is a name, never a number to cast (an iMessage id is not a snowflake)."""
        r = self._select("SELECT message_id FROM inbound WHERE channel = ? ORDER BY COALESCE(created, received) DESC, "
                         "received DESC LIMIT 1", (channel,))
        return str(r[0]["message_id"]) if r else None

    def last_received(self, room: str) -> float | None:
        """The last message a person (or the test webhook) sent this room; the harness's own are not."""
        r = self._select("SELECT max(received) AS t FROM inbound WHERE room = ? AND sender != 'yerk'", (room,))
        return float(r[0]["t"]) if r and r[0]["t"] is not None else None

    def last_said(self, room: str) -> float | None:
        """The room's own last post (a reply, a notice, a file, a card kept): a card's progress and a post that failed are not."""
        r = self._select("SELECT max(sent) AS t FROM outbound WHERE room = ? AND message_id IS NOT NULL AND kind NOT IN "
                         "('progress', 'progress-edit')", (room,))
        return float(r[0]["t"]) if r and r[0]["t"] is not None else None

    # health
    def health(self, **row: Any) -> int:
        known(row, HEALTH, "health")
        return self._insert("health", {"at": time.time(), **row})

    def health_last(self, account: str, source: str | None = None) -> dict[str, Any] | None:
        return self._first(f"SELECT * FROM health WHERE account = ?{'' if source is None else ' AND source = ?'} ORDER BY id DESC "
                           "LIMIT 1", (account,) if source is None else (account, source))

    # workers (workers.py)
    def worker_add(self, **row: Any) -> None:
        known(row, {"id", *WORKERS}, "worker")
        self._insert("workers", row)

    def worker_set(self, worker_id: str, **fields: Any) -> None:
        known(fields, WORKERS, "worker")
        self._update("workers", "id", worker_id, fields)

    def worker_get(self, worker_id: str) -> dict[str, Any] | None:
        return self._first("SELECT * FROM workers WHERE id = ?", (worker_id,))

    def workers(self, running: bool = False, limit: int = 1000) -> list[dict[str, Any]]:
        """Running ones (no `finished`) oldest first; else the newest `limit`."""
        if running:
            return self._select("SELECT * FROM workers WHERE finished IS NULL ORDER BY started", ())
        return self._select("SELECT * FROM workers ORDER BY started DESC LIMIT ?", (limit,))

    # job runs (jobs.py): one row per fire of a scheduled job
    def job_run_add(self, **row: Any) -> int:
        known(row, JOB_RUNS, "job_runs")
        return self._insert("job_runs", row)

    def job_run_set(self, run_id: int, **fields: Any) -> None:
        known(fields, JOB_RUNS, "job_runs")
        self._update("job_runs", "id", run_id, fields)

    def texting_add(self, **row: Any) -> int:
        """One texting decision (texting/intake.py); `inbound`: the comma-joined guids of the texts it answers."""
        known(row, TEXTING, "texting")
        return self._insert("texting", {"t": time.time(), **row})

    def texting_set(self, row_id: int, **fields: Any) -> None:
        known(fields, TEXTING, "texting")
        self._update("texting", "id", row_id, fields)

    def texting_rows(self, person: str, state: str) -> list[dict[str, Any]]:
        return self._select("SELECT * FROM texting WHERE person = ? AND state = ? ORDER BY id", (person, state))

    def texting_day(self, person: str, start: float, end: float) -> list[dict[str, Any]]:
        """A person's texts (inbound rows of room texting, at their own time) and texting rows from `start` to `end`, by time."""
        return sorted([*self._select("SELECT *, COALESCE(created, received) AS t FROM inbound WHERE room = 'texting' AND channel = ? "
                                     "AND COALESCE(created, received) >= ? AND COALESCE(created, received) < ?", (f"imessage:{person}", start, end)),
                       *self._select("SELECT * FROM texting WHERE person = ? AND t >= ? AND t < ?", (person, start, end))], key=lambda r: r["t"])

    def texting_span(self, start: float, end: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Every person's texts (texting_day's inbound rows) and every texting row from `start` to `end`, each by time."""
        return (self._select("SELECT *, COALESCE(created, received) AS t FROM inbound WHERE room = 'texting' AND COALESCE(created, "
                             "received) >= ? AND COALESCE(created, received) < ? ORDER BY t", (start, end)),
                self._select("SELECT * FROM texting WHERE t >= ? AND t < ? ORDER BY t", (start, end)))

    def texting_get(self, row_id: int) -> dict[str, Any] | None:
        return self._first("SELECT * FROM texting WHERE id = ?", (row_id,))

    def texting_card(self, message_id: str) -> dict[str, Any] | None:
        """The newest decision a card shows (a newer burst takes over its card)."""
        return self._first("SELECT * FROM texting WHERE card = ? ORDER BY id DESC LIMIT 1", (message_id,))

    def texting_held(self) -> list[dict[str, Any]]:
        return self.texting_in("held")

    def texting_in(self, state: str) -> list[dict[str, Any]]:
        return self._select("SELECT * FROM texting WHERE state = ? ORDER BY id", (state,))

    def texting_last(self, person: str) -> dict[str, Any] | None:
        return self._first("SELECT * FROM texting WHERE person = ? ORDER BY id DESC LIMIT 1", (person,))

    def job_run_get(self, run_id: int) -> dict[str, Any] | None:
        return self._first("SELECT * FROM job_runs WHERE id = ?", (run_id,))

    def job_run_for_worker(self, worker_id: str) -> dict[str, Any] | None:
        return self._first("SELECT * FROM job_runs WHERE worker = ? ORDER BY id DESC LIMIT 1", (worker_id,))

    def job_runs(self, job: str | None = None, limit: int = 50, undelivered: bool = False, unended: bool = False) -> list[dict[str, Any]]:
        """The newest runs (of one job when named); `undelivered`: runs whose delivery waits on a daemon; `unended`: no end yet."""
        sql, args = "SELECT * FROM job_runs WHERE 1", tuple[Any, ...]()
        if job:
            sql, args = sql + " AND job = ?", (*args, job)
        if undelivered:
            sql = sql + " AND delivery LIKE 'undelivered:%'"
        if unended:
            sql = sql + " AND ended IS NULL"
        return self._select(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))

    # outbound and holds
    def outbound(self, **row: Any) -> int:
        return self._insert("outbound", {"sent": time.time(), **row})

    # deliveries: one row per outbound part, keyed by its turn and part index, before the post
    def delivery_plan(self, key: str, room: str, kind: str, body: str = "", inbound: str = "", **extra: Any) -> dict[str, Any]:
        """The part's row, written once: a second plan for the same key returns the first row."""
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO deliveries (key, room, kind, body, inbound, state, created"
                                  f"{''.join(', ' + k for k in extra)}) VALUES (?, ?, ?, ?, ?, 'pending', ?"
                                  f"{', ?' * len(extra)})", (key, room, kind, body, inbound, time.time(), *extra.values()))
            if cur.rowcount:
                self._link("deliveries", {"inbound": inbound}, key=key)
        return self._select("SELECT * FROM deliveries WHERE key = ?", (key,))[0]

    def delivery_set(self, key: str, **fields: Any) -> None:
        self._update("deliveries", "key", key, fields)

    def deliveries(self, room: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM deliveries WHERE 1", tuple[Any, ...]()
        if room:
            sql, args = sql + " AND room = ?", (*args, room)
        if state:
            sql, args = sql + " AND state = ?", (*args, state)
        return self._select(sql + " ORDER BY created, key", args)

    def files_sent_for(self, message_ids: list[str]) -> list[dict[str, Any]]:
        """Files already in the room for any of these inbound messages (a turn a crash cut short): by
        the id rows, never by loading every sent delivery."""
        if not (want := [m for m in message_ids if m]):
            return []
        return self._select("SELECT DISTINCT d.* FROM deliveries d JOIN message_links l ON l.tbl = 'deliveries' AND "
                            "l.col = 'inbound' AND l.row_key = d.key WHERE d.kind = 'file' AND d.state = 'sent' AND "
                            f"l.message_id IN ({', '.join('?' * len(want))}) ORDER BY d.created", tuple(want))

    def delivered_for(self, message_id: str) -> list[str]:
        """The message ids a turn answering `message_id` posted, from the delivery ledger ([] if none)."""
        turns = self._select("SELECT t.turn_key FROM turns t JOIN message_links l ON l.tbl = 'turns' AND l.col = 'inbound' "
                             "AND l.row_key = CAST(t.id AS TEXT) WHERE l.message_id = ? AND t.outcome = 'ok'", (message_id,))
        keys = [str(t["turn_key"]) for t in turns if t["turn_key"]]
        out: list[str] = []
        for k in dict.fromkeys(keys):
            out += [str(r["message_id"]) for r in self._select(
                "SELECT message_id FROM deliveries WHERE state = 'sent' AND key LIKE ? AND key NOT LIKE ? "
                "ORDER BY created, key", (f"{k}:%", f"{k}:%:%"))]
        return out

    def last_reply(self, room: str) -> dict[str, Any] | None:
        """The room's newest reply as it went out (taste.py): its parts in order, its first part's message id and time."""
        if (r := self._first("SELECT key FROM deliveries WHERE room = ? AND kind = 'reply' AND state = 'sent' "
                             "ORDER BY sent DESC LIMIT 1", (room,))) is None:
            return None
        base = str(r["key"]).rsplit(":", 1)[0]
        parts = sorted((p for p in self._select("SELECT key, message_id, body, sent FROM deliveries WHERE key >= ? AND key < ? "
                                                 "AND +state = 'sent'", (f"{base}:", f"{base};")) if p["key"].rsplit(":", 1)[0] == base),
                       key=lambda p: int(p["key"].rsplit(":", 1)[1]))
        return {"id": str(parts[0]["message_id"]), "sent": float(parts[0]["sent"]), "text": "\n".join(str(p["body"]) for p in parts)}

    def last_outbound(self, room: str, besides: str) -> dict[str, Any] | None:
        """What the room last said, message `besides` left out: its newest outbound row that is not a card's own
        progress (discord.py: a quiet card goes when the next one stands, if nothing came between them)."""
        return self._first("SELECT kind, message_id, body FROM outbound WHERE room = ? AND kind NOT IN ('progress', "
                           "'progress-edit') AND message_id IS NOT ? ORDER BY id DESC LIMIT 1", (room, besides))

    def posted(self, room: str, name: str) -> dict[str, Any] | None:
        """The newest message the room posted a file named `name` in (any case), its channel and message id, else None."""
        return self._first("SELECT channel, message_id FROM outbound WHERE room = ? AND kind = 'file' AND file = ? COLLATE NOCASE "
                           "AND message_id IS NOT NULL ORDER BY id DESC LIMIT 1", (room, name))

    def turn_for_key(self, turn_key: str) -> int | None:
        r = self._select("SELECT id FROM turns WHERE turn_key = ? ORDER BY id DESC LIMIT 1", (turn_key,))
        return int(r[0]["id"]) if r else None

    # attachments and cards
    def attachment(self, **row: Any) -> int:
        return self._insert("attachments", {"at": time.time(), **row})

    def attachment_state(self, message_id: str, n: int, state: str) -> None:
        """The state of a message's `n`th file (0 first, in the order it was ledgered): texting's `seen`, or why not."""
        with self.lock:
            self.db.execute("UPDATE attachments SET state = ? WHERE id = (SELECT id FROM attachments WHERE message_id = ? "
                            "ORDER BY id LIMIT 1 OFFSET ?)", (state, message_id, n))

    def card_open(self, message_id: str, room: str, kind: str) -> None:
        self._insert("cards", {"message_id": message_id, "room": room, "kind": kind, "opened": time.time()})

    def card_close(self, message_id: str, how: str) -> None:
        self._update("cards", "message_id", message_id, {"closed": time.time(), "how": how})

    def cards_open(self, room: str) -> list[dict[str, Any]]:
        return self._select("SELECT * FROM cards WHERE room = ? AND closed IS NULL ORDER BY opened", (room,))

    def hold(self, room: str, wait_for: str, reason: str) -> int:
        return self._insert("holds", {"room": room, "wait_for": wait_for, "reason": reason, "at": time.time()})

    # retention (retention.py): a timing table's rows past their days, then the WAL into the database
    def older(self, table: str, column: str, before: float) -> list[dict[str, Any]]:
        return self._select(f"SELECT * FROM {table} WHERE {column} < ?", (before,))

    def expire(self, table: str, column: str, before: float) -> None:
        """Those rows deleted, the id rows of their list columns first."""
        with self.lock:
            for (tbl, col), key in ((k, v) for k, v in LINKED.items() if k[0] == table):
                self.db.execute(f"DELETE FROM message_links WHERE tbl = ? AND col = ? AND row_key IN (SELECT CAST({key} AS TEXT) "
                                f"FROM {table} WHERE {column} < ?)", (tbl, col, before))
            self.db.execute(f"DELETE FROM {table} WHERE {column} < ?", (before,))

    def checkpoint(self) -> None:
        with self.lock:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
