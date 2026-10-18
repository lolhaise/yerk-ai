# The FTS5 capability checks and the orphan shadow-table repair are adapted from Hermes Agent v0.22.2,
# hermes_state_fts.py (_is_fts5_unavailable_error, _drop_orphan_fts_shadow_tables),
# https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
# The scope rule (named roots, temp and backup names skipped, an empty scope refused) follows the
# workspace memory-index tool this harness replaces.
"""yerk-index, the memory search program: in its own process it keeps a keyword index of the memory folder, and a meaning index when an embeddings key is set.

One SQLite file in the state folder (DESIGN 5; L47) holds an FTS5 keyword index over the private memory
the daily logs, fact files, the curated file and open loops, plus any read-only roots the private
yerkd.toml adds. Every `interval` seconds the process stats the tree; a file whose size or mtime
moved is chunked again and only the chunks whose text changed are written, one short transaction
so a line appended to a day's log rewrites one chunk (L48, L49). A full rebuild runs only on `--rebuild`.
`--check` compares the index with the disk, never with the index's own report (L53), and a timeout reads
differently from a failure (L54). The daemon only reads the file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("yerk.index")

CHUNK_MAX = 900                        # characters per chunk; a heading always starts a new one
VERSION = "1"                          # the chunker and tokenizer: a change here re-chunks every file
TOKENIZER = "porter unicode61 remove_diacritics 2"
SKIP_DIRS = {"node_modules", "__pycache__", "overwrite-backups"}     # and every dot-folder
SKIP_NAMES = ("*.tmp", "*.bak", "*.swp", "*~", ".#*", "*.part", "*.pre-*")   # temp and backup copies (L52)
GRACE = 30.0                           # a change younger than this is pending, not stale

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                                  chunks INTEGER, indexed REAL);
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT NOT NULL, lo INTEGER, hi INTEGER,
                                   sha TEXT, body TEXT, head TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(body, content='chunks', content_rowid='id',
                                                        tokenize='{TOKENIZER}');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS chunks_sha ON chunks(sha);
CREATE TABLE IF NOT EXISTS vectors (sha TEXT NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL,
                                    PRIMARY KEY (sha, model, dim));
CREATE TABLE IF NOT EXISTS units (sha TEXT NOT NULL, chunk TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (chunk, sha));
CREATE INDEX IF NOT EXISTS units_sha ON units(sha);
"""

UNIT_MIN = 20                          # characters a line needs, past its bullet, to get a vector of its own
UNITS_VERSION = "2"                    # the unit rule ("2": the heading path in front of a line): a change here fills the units table again
HEAD_MAX = 160                         # characters of heading path kept in front of a line
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)")


def units_rule(context: bool) -> str:
    """The unit rule the index was filled under: the version and whether lines carry their heading path."""
    return f"{UNITS_VERSION}:{'context' if context else 'bare'}"


def heading_of(line: str) -> tuple[int, str] | None:
    """(level, text) for a Markdown heading line, else None."""
    m = HEADING.match(line.strip())
    return (len(m.group(1)), m.group(2).strip("# ").strip()[:80]) if m else None


def units_of(body: str, head: str = "", context: bool = False) -> list[tuple[str, str]]:
    """(sha, text) per line of a chunk that gets a vector of its own (a day-log line, a bullet of a
    fact or topic file): every line whose text past its bullet or heading marks is UNIT_MIN
    characters or more, once per distinct text. With `context` the text embedded is the line behind
    its heading path, "<file title> > <section heading>: <line>" (`head` is the chunk's path, the
    heading that opens the chunk included; that heading's own line sits under its parent path), so
    a line that says only "colour: teal" carries what it is about; the sha is the embedded text's,
    and the hit shown stays the chunk. Without it the text is the bare line, whose sha for a chunk
    of one line is the chunk's own, so the two share one vector."""
    out: dict[str, str] = {}
    for line in body.splitlines():
        text = line.strip()
        if len(text.lstrip("-*#> ").strip()) < UNIT_MIN:
            continue
        own = head
        h = heading_of(text)
        if h and head.endswith(h[1]):
            own = head[:-len(h[1])].rstrip(" >")
        unit = f"{own}: {text}" if context and own else text
        out.setdefault(hashlib.sha1(unit.encode()).hexdigest(), unit)
    return list(out.items())

STOP = frozenset("""a about after again all also am an and any are as at be because been before being both
but by can could did do does doing done for from get got had has have he her here him his how i if in into
is it its just me more most my no not now of on or our out over own she should so some such than that the
their them then there these they this those through to too under until up us very was we were what when
where which while who whom why will with would you your""".split())
WORD = re.compile(r"\w+")


class Timeout(Exception):
    """The walk or a database call ran past its deadline: says nothing about the store's health."""


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).lower()


def is_timeout(e: BaseException) -> bool:
    """A locked, busy or interrupted database is a timeout; anything else from SQLite is a failure."""
    m = str(e).lower()
    return isinstance(e, Timeout) or "locked" in m or "busy" in m or "interrupted" in m


def fts5_unavailable(e: sqlite3.OperationalError) -> bool:
    m = str(e).lower()
    return ("no such module" in m and "fts5" in m) or "no such tokenizer" in m


def drop_orphan_shadow_tables(cur: sqlite3.Cursor, families: list[str]) -> list[str]:
    """Shadow tables left without their virtual table (a recovered file) make every later CREATE
    VIRTUAL TABLE fail; drop them by exact name so the index is recreated from `chunks`."""
    repaired = []
    for fam in families:
        if cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? "
                       "AND sql LIKE 'CREATE VIRTUAL TABLE%'", (fam,)).fetchone():
            continue
        shadows = [f"{fam}_{s}" for s in ("content", "data", "docsize", "idx", "config")]
        orphans = [r[0] for r in cur.execute(
            f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({','.join('?' * len(shadows))})",
            shadows)]
        for name in orphans:
            cur.execute(f'DROP TABLE "{name}"')
        if orphans:
            log.warning("dropped orphan FTS5 shadow tables of %s (%s)", fam, ", ".join(orphans))
            repaired.append(fam)
    return repaired


@dataclass(frozen=True)
class Scope:
    """What the index covers: Markdown files under each root (a root may be one file)."""
    roots: tuple[Path, ...]
    exclude: tuple[str, ...] = ()

    def key(self) -> str:
        spec = f"{VERSION}|{CHUNK_MAX}|{TOKENIZER}|{[str(r) for r in self.roots]}|{list(self.exclude)}"
        return hashlib.sha1(spec.encode()).hexdigest()[:16]

    def skipped(self, path: Path) -> bool:
        return any(fnmatch(path.name, p) for p in SKIP_NAMES) or any(
            fnmatch(path.as_posix(), p) or fnmatch(path.name, p) for p in self.exclude)

    def contains(self, path: Path) -> bool:
        """True for a resolved path inside the scope: under a root, no dot-folder or skipped folder
        on the way, not a temp or backup name."""
        for root in self.roots:
            if path == root:
                return not self.skipped(path)
            if root.is_dir() and path.is_relative_to(root):
                parts = path.relative_to(root).parts[:-1]
                return not any(p.startswith(".") or p in SKIP_DIRS for p in parts) and not self.skipped(path)
        return False

    def files(self, deadline: float | None = None) -> dict[str, tuple[int, int]]:
        """path -> (size, mtime_ns) for every file in scope, from the disk."""
        if not self.roots:
            raise ValueError("the memory index has no roots: refusing to index nothing as if it were fresh")
        out: dict[str, tuple[int, int]] = {}
        for root in self.roots:
            if root.is_file():
                st = root.stat()
                out[str(root)] = (st.st_size, st.st_mtime_ns)
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                if deadline is not None and time.monotonic() > deadline:
                    raise Timeout(f"walking {root} ran past the deadline")
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
                for n in filenames:
                    p = Path(dirpath, n)
                    if not n.endswith(".md") or self.skipped(p) or p.is_symlink():
                        continue
                    try:
                        st = p.stat()
                    except OSError:                    # gone between the listing and the stat: next pass
                        continue
                    out[str(p)] = (st.st_size, st.st_mtime_ns)
        return out


def chunks_of(text: str, title: str = "") -> list[tuple[int, int, str, str]]:
    """(first line, last line, text, heading path) per chunk. A heading starts a chunk, a blank line
    past half the size ends one, lines gather up to CHUNK_MAX and a longer line is cut in pieces. A
    chunk depends only on the lines up to the one after it, so appending to a file changes its last
    chunk only. The heading path is the file's title (its first level-one heading, else `title`,
    the file's name) and the headings in force above the chunk's first line, joined by " > "."""
    out: list[tuple[int, int, str, str]] = []
    buf: list[str] = []
    stack: list[tuple[int, str]] = [(1, title)] if title else []
    start = size = n = 0
    head = ""

    def path() -> str:
        return " > ".join(t for _, t in stack)[:HEAD_MAX]

    def flush(end: int) -> None:
        nonlocal buf, size
        body = "\n".join(buf).strip()
        if body:
            out.append((start, end, body, head))
        buf, size = [], 0

    for n, line in enumerate(text.splitlines(), 1):
        if buf and (line.startswith("#") or size + len(line) + 1 > CHUNK_MAX):
            flush(n - 1)
        h = heading_of(line)
        if h:                                   # a heading opens its chunk, so the chunk's path includes it
            stack = [s for s in stack if s[0] < h[0]] + [h]
        if len(line) > CHUNK_MAX:
            out.extend((n, n, line[i:i + CHUNK_MAX].strip(), path()) for i in range(0, len(line), CHUNK_MAX)
                       if line[i:i + CHUNK_MAX].strip())
            continue
        if not buf:
            if not line.strip():
                continue
            start, head = n, path()
        buf.append(line)
        size += len(line) + 1
        if not line.strip() and size > CHUNK_MAX // 2:
            flush(n)
    flush(n)
    return out


def live(db: Path) -> Path:
    """The file the index lives in now: `db` itself, or the generation its link points at (a
    rebuild builds a new generation and swaps the link, so the path a reader opens is always
    a whole index, old or new). SQLite names the -wal and -shm after the path it opens, so
    every connection opens the generation's own path, never the link."""
    return Path(os.path.realpath(db))


def connect(db: Path, *, readonly: bool, deadline: float | None = None) -> sqlite3.Connection:
    """A connection; read-only ones (the daemon, the check, the bench) never write or lock the file.
    With a deadline, a statement still running past it is interrupted (a timeout, L54)."""
    db = live(db)
    if readonly:
        if not db.is_file():
            raise FileNotFoundError(f"no memory index at {db}; is yerk-index running?")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0, isolation_level=None)
    else:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-16000")                  # 16 MB of page cache, the whole budget
    if deadline is not None:
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
    return conn


class Index:
    """The writer. Only yerk-index holds one."""

    def __init__(self, db: Path, scope: Scope, on_insert: Callable[[list[str], list[str]], None] | None = None,
                 context: bool = False):
        self.db, self.scope = db, scope
        self.real = live(db)
        self.disk: dict[str, tuple[int, int]] = {}      # the last pass's view of the disk
        self.on_insert = on_insert                        # (chunk hashes, unit hashes) just written: the embedding thread
        self.context = context                            # lines get vectors with their heading path in front
        self.conn = connect(db, readonly=False)
        cur = self.conn.cursor()
        repaired = drop_orphan_shadow_tables(cur, ["chunks_fts"])
        try:
            cur.executescript(SCHEMA)
            if "head" not in {r[1] for r in cur.execute("PRAGMA table_info(chunks)")}:
                cur.execute("ALTER TABLE chunks ADD COLUMN head TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as e:
            if fts5_unavailable(e):
                raise SystemExit("this Python's sqlite3 has no FTS5 module; yerk-index needs it") from e
            raise
        if repaired:
            self.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        old = self.meta("scope")
        if old and old != scope.key():
            log.warning("index scope changed (%s -> %s): files out of scope go, new ones come in", old, scope.key())
        self.refill = self.refill_units()

    def refill_units(self) -> bool:
        """True when the units table must be filled again (an index from before per-line vectors, or
        a changed unit rule: the heading path turned on or off). The old units go now; the first
        pass then chunks every file again and writes each chunk's units as they land, in one short
        transaction per file, so a reader never waits. A vector already stored for a unit's text is
        found again by its sha and never paid twice."""
        rule = units_rule(self.context)
        if self.meta("units") == rule:
            return False
        n = int(self.conn.execute("SELECT count(*) FROM units").fetchone()[0])
        if n:
            self.conn.execute("DELETE FROM units")
            log.info("the unit rule changed (%s -> %s): %d units dropped; every file is chunked again on the first "
                     "pass and its lines get vectors under the new rule", self.meta("units"), rule, n)
        return True

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_meta(self, **kv: Any) -> None:
        self.conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                              "value = excluded.value", [(k, str(v)) for k, v in kv.items()])

    def update(self, path: str, stat: tuple[int, int]) -> tuple[int, int]:
        """Chunk one file again; write only the chunks whose text changed. (inserted, deleted)."""
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0, 0                                         # gone or unreadable: the next pass sees it
        old: dict[str, list[tuple[int, int, int, str]]] = {}
        for cid, lo, hi, sha, head in self.conn.execute("SELECT id, lo, hi, sha, head FROM chunks WHERE path = ?", (path,)):
            old.setdefault(sha, []).append((cid, lo, hi, head))
        new = chunks_of(text, Path(path).stem)
        ins, moves, heads, kept = [], [], [], []
        for lo, hi, body, head in new:
            sha = hashlib.sha1(body.encode()).hexdigest()
            if old.get(sha):
                cid, olo, ohi, ohead = old[sha].pop(0)
                if (olo, ohi) != (lo, hi):
                    moves.append((lo, hi, cid))
                if ohead != head or self.refill:
                    # the heading above a kept chunk changed (or the table is being filled again): the chunk's
                    # lines get units under the new path; the old path's units stay until the chunk text goes,
                    # since a chunk text held by two files may be under two paths
                    heads.append((head, cid))
                    kept.append((sha, body, head))
            else:
                ins.append((path, lo, hi, sha, body, head))
        dels = [(cid,) for kept_ in old.values() for cid, _, _, _ in kept_]
        gone = [sha for sha, kept_ in old.items() if kept_]
        units = [(u, sha, t) for _, _, _, sha, body, head in ins for u, t in units_of(body, head, self.context)]
        units += [(u, sha, t) for sha, body, head in kept for u, t in units_of(body, head, self.context)]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.executemany("DELETE FROM chunks WHERE id = ?", dels)
            self.conn.executemany("UPDATE chunks SET lo = ?, hi = ? WHERE id = ?", moves)
            self.conn.executemany("UPDATE chunks SET head = ? WHERE id = ?", heads)
            self.conn.executemany("INSERT INTO chunks(path, lo, hi, sha, body, head) VALUES (?, ?, ?, ?, ?, ?)", ins)
            self.conn.executemany("INSERT OR IGNORE INTO units(sha, chunk, body) VALUES (?, ?, ?)", units)
            self._drop_units(gone)
            self.conn.execute("INSERT INTO files(path, size, mtime_ns, chunks, indexed) VALUES (?, ?, ?, ?, ?) "
                              "ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns, "
                              "chunks = excluded.chunks, indexed = excluded.indexed",
                              (path, stat[0], stat[1], len(new), time.time()))
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        if (ins or units) and self.on_insert and not self.refill:   # a refill lets the embedding thread's own scan find them
            self.on_insert([sha for _, _, _, sha, _, _ in ins], [u for u, _, _ in units])
        return len(ins), len(dels)

    def _drop_units(self, shas: list[str]) -> None:
        """The units of chunk texts no file holds any more (inside the caller's transaction)."""
        self.conn.executemany("DELETE FROM units WHERE chunk = ? AND NOT EXISTS (SELECT 1 FROM chunks WHERE sha = ?)",
                              [(sha, sha) for sha in shas])

    def forget(self, path: str) -> int:
        shas = [str(r[0]) for r in self.conn.execute("SELECT DISTINCT sha FROM chunks WHERE path = ?", (path,))]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            n = self.conn.execute("DELETE FROM chunks WHERE path = ?", (path,)).rowcount
            self.conn.execute("DELETE FROM files WHERE path = ?", (path,))
            self._drop_units(shas)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return n

    def sync(self) -> dict[str, int]:
        """One pass: every file whose size or mtime moved is updated, every file gone is forgotten.
        A rebuild elsewhere swapped the link: the writer moves to the new generation first."""
        if live(self.db) != self.real:
            self.conn.close()
            self.real, self.conn = live(self.db), connect(self.db, readonly=False)
            log.info("the index was rebuilt: writing to %s now", self.real.name)
        disk = self.disk = self.scope.files()
        indexed = {p: (s, m) for p, s, m in self.conn.execute("SELECT path, size, mtime_ns FROM files")}
        st = {"files": len(disk), "changed": 0, "gone": 0, "inserted": 0, "deleted": 0}
        for path, stat in disk.items():
            if self.refill or indexed.get(path) != stat:
                i, d = self.update(path, stat)
                st["changed"] += 1
                st["inserted"] += i
                st["deleted"] += d
        for path in indexed.keys() - disk.keys():
            st["deleted"] += self.forget(path)
            st["gone"] += 1
        chunks = self.conn.execute("SELECT max(id), count(*) FROM chunks").fetchone()
        if self.refill:
            self.refill = False
            self.set_meta(units=units_rule(self.context))
            if self.on_insert:
                self.on_insert([], [])                   # the embedding thread scans for the new units
        self.set_meta(scope=self.scope.key(), synced=round(time.time(), 3), chunks=chunks[1] or 0)
        st["chunks"] = int(chunks[1] or 0)
        return st

    def close(self) -> None:
        self.conn.close()


SWEEP_GRACE = 60.0       # seconds an old generation stays after a swap (a reader may hold it)


def open_store(idx: Index, store: Any, model: str, units: str) -> int:
    """The vectors served from the start. After a unit rule change the units are filled again first
    (one pass), so the load finds every vector already paid for: a rule switched back embeds nothing
    and serves its lines at once, instead of only the chunks until the next restart."""
    if idx.refill:
        idx.sync()
    return int(store.load(idx.conn, model, units))


def rebuild(db: Path, scope: Scope, context: bool = False) -> Path:
    """The full reindex, on request only (L49). It builds a new generation beside the live one,
    folds its WAL in, then swaps the link in one rename: a recall during the build reads the old
    index, one after the swap the new, and none finds no file (run 1 deleted first: ~10 s of
    failures). The old generation is swept after SWEEP_GRACE."""
    gen = db.with_name(f"{db.stem}-{time.time_ns()}{db.suffix}")
    old = live(db)
    idx = Index(gen, scope, context=context)
    try:
        st = idx.sync()
        if old.is_file() and old != gen:
            st["vectors"] = copy_vectors(idx.conn, old)
        idx.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        idx.close()
    tmp = db.with_name(f".{db.name}.swap")
    tmp.unlink(missing_ok=True)
    os.symlink(gen.name, tmp)
    os.replace(tmp, db)
    log.info("rebuilt into %s: %d files; the link is swapped", gen.name, st["files"])
    return gen


def copy_vectors(conn: sqlite3.Connection, old: Path) -> int:
    """A rebuild never embeds twice: every vector of the old generation whose chunk hash is still in
    the new one comes across (an old file without the table, from before meaning search, gives 0)."""
    conn.execute("ATTACH DATABASE ? AS old", (str(old),))
    try:
        try:
            n = conn.execute("INSERT OR IGNORE INTO vectors(sha, model, dim, vec) SELECT o.sha, o.model, o.dim, o.vec "
                             "FROM old.vectors o WHERE o.sha IN (SELECT sha FROM chunks) "
                             "OR o.sha IN (SELECT sha FROM units)").rowcount
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):
                raise
            n = 0
    finally:
        conn.execute("DETACH DATABASE old")
    return int(n)


def sweep(db: Path, grace: float = SWEEP_GRACE, now: float | None = None) -> list[str]:
    """Delete generations the link no longer points at, once the swap is older than `grace`."""
    if not db.is_symlink() or (now or time.time()) - os.lstat(db).st_mtime < grace:
        return []
    keep = live(db).name
    gone = []
    for p in [*db.parent.glob(f"{db.stem}-*{db.suffix}*"), *(Path(f"{db}{s}") for s in ("-wal", "-shm"))]:
        base = p.name.removesuffix("-wal").removesuffix("-shm")
        if base != keep and p.exists() and not p.is_symlink():
            p.unlink(missing_ok=True)
            gone.append(p.name)
    return gone


def terms(query: str) -> list[str]:
    out: list[str] = []
    for w in WORD.findall(norm(query)):
        if len(w) > 1 and w not in STOP and w not in out:
            out.append(w)
    return out[:24]


def search(conn: sqlite3.Connection, query: str, k: int = 6, pool: int = 80) -> list[dict[str, Any]]:
    """Ranked chunks for a query: any of its words (FTS5, stemmed), ordered by how much of the
    query's weight a chunk covers (each distinct word weighted by its rarity), then by bm25."""
    ts = terms(query)
    if not ts:
        return []
    rows = conn.execute("SELECT c.id, c.path, c.lo, c.hi, c.body, bm25(chunks_fts) FROM chunks_fts "
                        "JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ? "
                        "ORDER BY bm25(chunks_fts) LIMIT ?", (" OR ".join(f'"{t}"' for t in ts), pool)).fetchall()
    if not rows:
        return []
    total = int((conn.execute("SELECT value FROM meta WHERE key = 'chunks'").fetchone() or [len(rows)])[0])
    ids = [r[0] for r in rows]
    marks = ",".join("?" * len(ids))
    cover: dict[int, float] = {}
    for t in ts:
        df = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (f'"{t}"',)).fetchone()[0]
        w = math.log(1 + (max(total, df) - df + 0.5) / (df + 0.5))
        for (rid,) in conn.execute(f"SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid IN ({marks})",
                                   (f'"{t}"', *ids)):
            cover[rid] = cover.get(rid, 0.0) + w
    ranked = sorted(rows, key=lambda r: (-round(cover.get(r[0], 0.0), 2), r[5]))
    return [{"id": cid, "path": p, "line": lo, "lines": f"{lo}-{hi}", "text": body, "score": round(cover.get(cid, 0.0), 2)}
            for cid, p, lo, hi, body, _ in ranked[:k]]


def check(db: Path, scope: Scope, deadline_s: float = 10.0, grace: float = GRACE) -> dict[str, Any]:
    """The index against the disk. status: fresh; pending (changes younger than `grace`, the next
    pass takes them); stale; scope-changed; timeout (the walk or a locked file ran past the deadline:
    nothing is known about the store); failure (no index, or SQLite could not read it)."""
    end = time.monotonic() + deadline_s
    try:
        conn = connect(db, readonly=True, deadline=end)
        try:
            indexed = {p: (s, m) for p, s, m in conn.execute("SELECT path, size, mtime_ns FROM files")}
            key = (conn.execute("SELECT value FROM meta WHERE key = 'scope'").fetchone() or [""])[0]
            synced = float((conn.execute("SELECT value FROM meta WHERE key = 'synced'").fetchone() or [0])[0])
        finally:
            conn.close()
        disk = scope.files(deadline=end)
    except FileNotFoundError as e:
        return {"status": "failure", "error": str(e)}
    except (Timeout, sqlite3.Error) as e:
        return {"status": "timeout" if is_timeout(e) else "failure", "error": str(e), "deadline_s": deadline_s}
    now = time.time()
    changed = [p for p, st in disk.items() if indexed.get(p) != st]
    gone = [p for p in indexed if p not in disk]
    old = [p for p in changed if now - disk[p][1] / 1e9 > grace]
    lag = max((now - disk[p][1] / 1e9 for p in changed), default=0.0)
    if key != scope.key():
        status = "scope-changed"
    elif old or gone:
        status = "stale"
    else:
        status = "pending" if changed else "fresh"
    return {"status": status, "files": len(disk), "indexed": len(indexed), "changed": len(changed),
            "stale": len(old), "gone": len(gone), "lag_s": round(lag, 1), "synced_ago_s": round(now - synced, 1),
            "examples": (old or gone or changed)[:3]}


CHECK_EXIT = {"fresh": 0, "pending": 0, "stale": 1, "scope-changed": 1, "failure": 2, "timeout": 3}


def main(argv: list[str] | None = None) -> int:
    from . import mentions
    from .config import private_dir, state_dir
    from .memory import settings
    p = argparse.ArgumentParser(prog="yerk-index", description="The memory index: FTS5 over the private "
                                "memory tree, updated per changed chunk, in its own process.")
    p.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    p.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    p.add_argument("--interval", type=float, help="seconds between passes (default: [memory] interval, else 2)")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--rebuild", action="store_true", help="drop the index and build it again (the only full reindex)")
    p.add_argument("--check", action="store_true", help="compare the index with the disk; exit 0 fresh or "
                   "pending, 1 stale or scope changed, 2 failure, 3 timeout")
    p.add_argument("--deadline", type=float, default=10.0, help="--check: seconds before it reads as a timeout")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    m = settings(private_dir(a.private), state_dir(a.state))
    if a.check:
        r = check(m.index_db, m.scope, a.deadline)
        print(json.dumps(r))
        return CHECK_EXIT[r["status"]]
    if a.rebuild:
        rebuild(m.index_db, m.scope, m.embeddings_context)
    from . import embeddings
    embedder = embeddings.Embedder(m.embeddings_key, m.embeddings_model, m.embeddings_dim)
    store = embeddings.Vectors(m.embeddings_dim, m.embeddings_width)
    back = embeddings.Backfill(m.index_db, live, store, embedder, units=m.embeddings_units)
    idx = Index(m.index_db, m.scope, on_insert=back.note, context=m.embeddings_context)
    names = mentions.Builder(m.mentions, m.roots_for_mentions, m.curated, m.mentions_exclude)
    server = embeddings.Server(store, embedder, m.index_db, live)
    if not a.once:
        loaded = open_store(idx, store, m.embeddings_model, m.embeddings_units)
        port = server.start()
        back.start()
        log.info("meaning search: %s; %d vectors loaded (%s, %d of %d dimensions kept, %.1f MB); serving on 127.0.0.1:%d",
                 "key present" if embedder.available() else "no key, keywords only", loaded, m.embeddings_units,
                 store.width, m.embeddings_dim, store.bytes() / 1e6, port)
    stop = False

    def _stop(*_: Any) -> None:
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = a.interval or m.interval
    first = True
    chunks = 0
    told = (0, time.monotonic())                   # the backfill's count and time at its last log line
    while not stop:
        t0 = time.monotonic()
        try:
            st = idx.sync()
            chunks = st["chunks"]
        except sqlite3.OperationalError as e:
            log.warning("pass skipped (%s): %s", "timeout" if is_timeout(e) else "failure", e)
            st = {}
        try:
            built = names.refresh(idx.disk)
            if built is not None:
                log.info("mention index: %d keys, %d entries, %d dropped as ask-only", *built)
            for gone in sweep(m.index_db):
                log.info("swept the old generation file %s", gone)
            if not a.once:
                embeddings.write_status(m.index_status, port=server.port, chunks=chunks,
                                        embeddings={"key": embedder.available(), "model": m.embeddings_model,
                                                    "dim": m.embeddings_dim, "vectors": store.n, "units": m.embeddings_units,
                                                    "width": store.width, "context": m.embeddings_context, **back.status(),
                                                    "requests": embedder.requests, "tokens": embedder.tokens,
                                                    "rss_kb": embeddings.rss_kb(), "store_bytes": store.bytes()})
        except OSError as e:
            log.warning("mention index, sweep or status skipped: %s", e)
        took = time.monotonic() - t0
        if first or st.get("changed") or st.get("gone"):
            log.info("pass: %d files, %d changed, %d gone, %d chunks in, %d out, %.2f s", st.get("files", 0),
                     st.get("changed", 0), st.get("gone", 0), st.get("inserted", 0), st.get("deleted", 0), took)
            first = False
        if back.done != told[0] and time.monotonic() - told[1] >= 30.0:
            log.info("backfill: %d embedded since start (%d vectors of %d chunks), %d requests, %d tokens, "
                     "%d kB resident", back.done, store.n, chunks, embedder.requests, embedder.tokens, embeddings.rss_kb())
            told = (back.done, time.monotonic())
        if a.once:
            break
        end = time.monotonic() + max(0.2, interval - took)
        while not stop and time.monotonic() < end:
            time.sleep(0.1)
    back.stop.set()
    server.stop()
    idx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
