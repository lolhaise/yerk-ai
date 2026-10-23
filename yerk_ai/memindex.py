# The FTS5 capability checks and the orphan shadow-table repair are adapted from Hermes Agent v0.22.2,
# hermes_state_fts.py (_is_fts5_unavailable_error, _drop_orphan_fts_shadow_tables),
# https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
# The scope rule (named roots, temp and backup names skipped, an empty scope refused) follows the
# workspace memory-index tool this harness replaces.
"""yerk-index, the memory search program: in its own process it keeps a keyword index of the memory folder, and a meaning index when an embeddings key is set.

One SQLite file in the state folder (DESIGN 5; L47) holds an FTS5 keyword index over the private memory
the daily logs, fact files, the curated file and open loops, plus any read-only roots the private
yerkd.toml adds. The process stats the tree when inotify says a file in it moved; a file whose size or mtime
moved is chunked again and only the chunks whose text changed are written, one short transaction
so a line appended to a day's log rewrites one chunk (L48, L49). A full rebuild runs only on `--rebuild`.
`--check` compares the index with the disk, never with the index's own report (L53), and a timeout reads
differently from a failure (L54). The daemon only reads the file.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import logging
import math
import os
import re
import select
import signal
import sqlite3
import struct
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("yerk.index")

CHUNK_MAX = 900                        # characters per chunk; a heading always starts a new one
VERSION = "2"                          # the chunker and tokenizer: a change here chunks every file again on the next pass (sync)
TOKENIZER = "porter unicode61 remove_diacritics 2"
SKIP_DIRS = {"node_modules", "__pycache__", "overwrite-backups"}     # and every dot-folder
SKIP_NAMES = ("*.tmp", "*.bak", "*.swp", "*~", ".#*", "*.part", "*.pre-*")   # temp and backup copies (L52)
GRACE = 30.0                           # a change younger than this is pending, not stale
FTS_TABLES = ("chunks_fts_data", "chunks_fts_idx", "chunks_fts_docsize", "chunks_fts_config")   # what one MATCH reads
PIN_TABLES = (*FTS_TABLES, "meta", "chunks")   # and what the keyword rank joins: the rows it ranks, the chunk count
PIN_EVERY = 600.0                      # seconds between pin passes: pages written since the last one are mapped then

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                                  chunks INTEGER, indexed REAL);
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT NOT NULL, lo INTEGER, hi INTEGER,
                                   sha TEXT, body TEXT);
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


def units_of(body: str) -> list[tuple[str, str]]:
    """(sha, text) per line of a chunk that gets a vector of its own (a day-log line, a bullet of a
    fact or topic file): every line whose text past its bullet or heading marks is UNIT_MIN
    characters or more, once per distinct text. A chunk of one line yields that line, whose sha is
    the chunk's own, so the two share one vector."""
    texts = (line.strip() for line in body.splitlines())
    return list({hashlib.sha1(t.encode()).hexdigest(): t for t in texts if len(t.lstrip("-*#> ").strip()) >= UNIT_MIN}.items())

STOP = frozenset("""a about after again all also am an and any are as at be because been before being both
but by can could did do does doing done for from get got had has have he her here him his how i if in into
is it its just me more most my no not now of on or our out over own she should so some such than that the
their them then there these they this those through to too under until up us very was we were what when
where which while who whom why will with would you your""".split())
WORD = re.compile(r"\w+")


class Timeout(Exception):
    """The walk or a database call ran past its deadline: says nothing about the store's health."""


def norm(s: str) -> str:
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).lower()


def is_timeout(e: BaseException) -> bool:
    """A locked, busy or interrupted database is a timeout; anything else from SQLite is a failure."""
    return isinstance(e, Timeout) or any(w in str(e).lower() for w in ("locked", "busy", "interrupted"))


def fts5_unavailable(e: sqlite3.OperationalError) -> bool:
    return ("no such module" in (m := str(e).lower()) and "fts5" in m) or "no such tokenizer" in m


def drop_orphan_shadow_tables(cur: sqlite3.Cursor, families: list[str]) -> list[str]:
    """Shadow tables left without their virtual table (a recovered file) make every later CREATE
    VIRTUAL TABLE fail, so yerk-index would never start again; drop them by exact name and the
    keyword index is rebuilt from `chunks`, with no one asked to run anything."""
    repaired = []
    for fam in families:
        if cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? "
                       "AND sql LIKE 'CREATE VIRTUAL TABLE%'", (fam,)).fetchone():
            continue
        shadows = [f"{fam}_{s}" for s in ("content", "data", "docsize", "idx", "config")]
        orphans = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                                             f"({','.join('?' * len(shadows))})", shadows)]
        for name in orphans:
            cur.execute(f'DROP TABLE "{name}"')
        if orphans:
            log.warning("dropped orphan FTS5 shadow tables of %s (%s)", fam, ", ".join(orphans))
            repaired.append(fam)
    return repaired


UPLOADS = (".md", ".txt", ".csv", ".json", ".vtt", ".srt")    # a room inbox's text kinds: the transcribe tool's NAME.transcript.txt too
SEEN = ".seen.txt"           # and a picture's NAME.seen.txt, what the room read on it (pictures.py): a text kind by its .txt
SEEN_HEAD = "seen in "       # how a sidecar's first line, written by code, starts (pictures.header)


def kinds(root: Path) -> tuple[str, ...]:
    """The file kinds a root is indexed for: Markdown and a picture's sidecar (a `--root` backfill files one beside a picture
    in a listed folder), and in a room's upload inbox (<room>/work/inbox) its text kinds too."""
    return UPLOADS if root.parts[-2:] == ("work", "inbox") else (".md", SEEN)


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
        on the way, not a temp or backup name; under a room's inbox, one of its text kinds only (UPLOADS)."""
        for root in self.roots:
            if path == root:
                return not self.skipped(path)
            if root.is_dir() and path.is_relative_to(root):
                parts = path.relative_to(root).parts[:-1]
                return not any(p.startswith(".") or p in SKIP_DIRS for p in parts) and not self.skipped(path) and (
                    kinds(root) != UPLOADS or path.name.endswith(UPLOADS))
        return False

    def files(self, deadline: float | None = None, folders: list[str] | None = None) -> dict[str, tuple[int, int]]:
        """path -> (size, mtime_ns) for every file in scope, from the disk; `folders` gets each folder read."""
        if not self.roots:
            raise ValueError("the memory index has no roots: refusing to index nothing as if it were fresh")
        out: dict[str, tuple[int, int]] = {}
        folders = [] if folders is None else folders
        for root in self.roots:
            if root.is_file():
                st = root.stat()
                out[str(root)] = (st.st_size, st.st_mtime_ns)
                folders.append(str(root.parent))
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                if deadline is not None and time.monotonic() > deadline:
                    raise Timeout(f"walking {root} ran past the deadline")
                folders.append(dirpath)
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
                for n in filenames:
                    p = Path(dirpath, n)
                    if not n.endswith(kinds(root)) or self.skipped(p) or p.is_symlink():
                        continue
                    try:
                        st = p.stat()
                    except OSError:                    # gone between the listing and the stat: next pass
                        continue
                    out[str(p)] = (st.st_size, st.st_mtime_ns)
        return out


# the day-log line: `- HH:MM:SS+hh:mm label#id role@who: body [c:class] [m:id]`, every field after the
# time optional, so the line written before the offset, the id, the author and the class still reads
LINE = re.compile(r"^- (\d\d:\d\d:\d\d)([+-]\d\d:\d\d)? (\S+?)(?:#(\S+))? ([\w-]+)(?:@(\S+))?: (.*?)"
                  r"(?: \[c:(\w+)\])?(?: \[m:([^\]\s]+)\])?$")
DAY_HEAD = re.compile(r"^# \d{4}-\d{2}-\d{2}\b")      # the day log's first line


def lane(line: str) -> tuple[str, str] | None:
    """Who may read a line, as memory.allowed_text reads it: (room id, class) of a day-log line; ("", "") for a free-form
    line, the owner's alone; None for a blank, the day header or an attachment line, which go with the line above."""
    if (m := LINE.match(line)):
        return m.group(4) or m.group(3), m.group(8) or ""
    return None if not line.strip() or DAY_HEAD.match(line) or line.startswith("  - attachment: ") else ("", "")


def chunks_of(text: str) -> list[tuple[int, int, str]]:
    """(first line, last line, text) per chunk. A heading starts a chunk, a blank line past half the
    size ends one, lines gather up to CHUNK_MAX and a longer line is cut in pieces. A chunk depends
    only on the lines up to the one after it, so appending to a file changes its last chunk only.
    A day log is chunked by room id and class too (`lane`): a line of another lane than the chunk's starts a new one, and an
    attachment line stays with its line (after a line cut in pieces it is a chunk of its own), so a room that may read a chunk reads all of it."""
    out: list[tuple[int, int, str]] = []
    buf: list[str] = []
    start = size = n = 0
    key: tuple[str, str] | None = None                 # the chunk's lane, from its first line that has one

    def flush(end: int) -> None:
        nonlocal buf, size, key
        if (body := "\n".join(buf).strip()):
            out.append((start, end, body))
        buf, size, key = [], 0, None

    for n, line in enumerate(text.splitlines(), 1):
        who = lane(line)
        if buf and (line.startswith("#") or who is not None and (size + len(line) + 1 > CHUNK_MAX or key and who != key)):
            flush(n - 1)
        if len(line) > CHUNK_MAX or not buf and line.startswith("  - attachment: "):      # cut in pieces; an attachment line after one
            out.extend((n, n, ("… " if (w := lane(p)) and w[0] and (i or w[1] not in ("", who and who[1])) else "") + p)   # a piece that reads
                       for i in range(0, len(line), CHUNK_MAX) if (p := line[i:i + CHUNK_MAX].strip()))     # as a line it is not: never one
            continue
        if not buf:
            if not line.strip():
                continue
            start = n
        buf.append(line)
        size += len(line) + 1
        key = key or who
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

    def __init__(self, db: Path, scope: Scope, on_insert: Callable[[list[str], list[str]], None] | None = None):
        self.db, self.scope = db, scope
        self.real = live(db)
        self.disk: dict[str, tuple[int, int]] = {}      # the last pass's view of the disk
        self.folders: list[str] = []                      # the folders that pass read: the ones the watch covers
        self.on_insert = on_insert                        # (chunk hashes, unit hashes) just written: the embedding thread
        self.conn = connect(db, readonly=False)
        cur = self.conn.cursor()
        repaired = drop_orphan_shadow_tables(cur, ["chunks_fts"])
        try:
            cur.executescript(SCHEMA)
        except sqlite3.OperationalError as e:
            if fts5_unavailable(e):
                raise SystemExit("this Python's sqlite3 has no FTS5 module; yerk-index needs it") from e
            raise
        if repaired:
            self.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        if (old := self.meta("scope")) and old != scope.key():
            log.warning("index version or scope changed (%s -> %s): every file is chunked again this pass", old, scope.key())

    def meta(self, key: str) -> str | None:
        return str(row[0]) if (row := self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()) else None

    def set_meta(self, **kv: Any) -> None:
        self.conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                              "value = excluded.value", [(k, str(v)) for k, v in kv.items()])

    def update(self, path: str, stat: tuple[int, int]) -> tuple[int, int]:
        """Chunk one file again; write only the chunks whose text changed. (inserted, deleted)."""
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0, 0                                         # gone or unreadable: the next pass sees it
        old: dict[str, list[tuple[int, int, int]]] = {}
        for cid, lo, hi, sha in self.conn.execute("SELECT id, lo, hi, sha FROM chunks WHERE path = ?", (path,)):
            old.setdefault(sha, []).append((cid, lo, hi))
        new = chunks_of(text)
        ins, moves = [], []
        for lo, hi, body in new:
            sha = hashlib.sha1(body.encode()).hexdigest()
            if old.get(sha):
                cid, olo, ohi = old[sha].pop(0)
                if (olo, ohi) != (lo, hi):
                    moves.append((lo, hi, cid))
            else:
                ins.append((path, lo, hi, sha, body))
        dels = [(cid,) for kept in old.values() for cid, _, _ in kept]
        gone = [sha for sha, kept in old.items() if kept]
        units = [(u, sha, t) for _, _, _, sha, body in ins for u, t in units_of(body)]
        self.conn.execute("BEGIN IMMEDIATE")
        with self.conn:                                         # COMMIT, or ROLLBACK on any exception
            self.conn.executemany("DELETE FROM chunks WHERE id = ?", dels)
            self.conn.executemany("UPDATE chunks SET lo = ?, hi = ? WHERE id = ?", moves)
            self.conn.executemany("INSERT INTO chunks(path, lo, hi, sha, body) VALUES (?, ?, ?, ?, ?)", ins)
            self.conn.executemany("INSERT OR IGNORE INTO units(sha, chunk, body) VALUES (?, ?, ?)", units)
            self._drop_units(gone)
            self.conn.execute("INSERT INTO files(path, size, mtime_ns, chunks, indexed) VALUES (?, ?, ?, ?, ?) "
                              "ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns, "
                              "chunks = excluded.chunks, indexed = excluded.indexed",
                              (path, stat[0], stat[1], len(new), time.time()))
        if ins and self.on_insert:
            self.on_insert([sha for _, _, _, sha, _ in ins], [u for u, _, _ in units])
        return len(ins), len(dels)

    def _drop_units(self, shas: list[str]) -> None:
        """The units of chunk texts no file holds any more (inside the caller's transaction)."""
        self.conn.executemany("DELETE FROM units WHERE chunk = ? AND NOT EXISTS (SELECT 1 FROM chunks WHERE sha = ?)",
                              [(sha, sha) for sha in shas])

    def forget(self, path: str) -> int:
        shas = [str(r[0]) for r in self.conn.execute("SELECT DISTINCT sha FROM chunks WHERE path = ?", (path,))]
        self.conn.execute("BEGIN IMMEDIATE")
        with self.conn:
            n = self.conn.execute("DELETE FROM chunks WHERE path = ?", (path,)).rowcount
            self.conn.execute("DELETE FROM files WHERE path = ?", (path,))
            self._drop_units(shas)
        return n

    def sync(self) -> dict[str, int]:
        """One pass: every file whose size or mtime moved is updated (every file, once, after a VERSION or scope change),
        every file gone is forgotten. A rebuild elsewhere swapped the link: the writer moves to the new generation first."""
        if live(self.db) != self.real:
            self.conn.close()
            self.real, self.conn = live(self.db), connect(self.db, readonly=False)
            log.info("the index was rebuilt: writing to %s now", self.real.name)
        self.folders = []
        disk = self.disk = self.scope.files(folders=self.folders)
        indexed = {p: (s, m) for p, s, m in self.conn.execute("SELECT path, size, mtime_ns FROM files")}
        again = self.meta("scope") != self.scope.key()       # a new chunker (VERSION) or scope: every file again, its changed chunks written
        st = {"files": len(disk), "changed": 0, "gone": 0, "inserted": 0, "deleted": 0}
        for path, stat in disk.items():
            if again or indexed.get(path) != stat:
                i, d = self.update(path, stat)
                st.update(changed=st["changed"] + 1, inserted=st["inserted"] + i, deleted=st["deleted"] + d)
        for path in indexed.keys() - disk.keys():
            st["deleted"] += self.forget(path)
            st["gone"] += 1
        st["chunks"] = int(self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
        self.set_meta(scope=self.scope.key(), synced=round(time.time(), 3), chunks=st["chunks"])
        return st

    def close(self) -> None:
        self.conn.close()


SWEEP_GRACE = 60.0       # seconds an old generation stays after a swap (a reader may hold it)


def rebuild(db: Path, scope: Scope) -> Path:
    """The full reindex, on request only (L49). It builds a new generation beside the live one,
    folds its WAL in, then swaps the link in one rename: a recall during the build reads the old
    index, one after the swap the new, and none finds no file (run 1 deleted first: ~10 s of
    failures). The old generation is swept after SWEEP_GRACE."""
    gen = db.with_name(f"{db.stem}-{time.time_ns()}{db.suffix}")
    old = live(db)
    idx = Index(gen, scope)
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
    """A rebuild never embeds twice: every vector of the old generation whose chunk or line hash is
    still in the new one comes across."""
    conn.execute("ATTACH DATABASE ? AS old", (str(old),))
    try:
        return int(conn.execute("INSERT OR IGNORE INTO vectors(sha, model, dim, vec) SELECT o.sha, o.model, o.dim, o.vec "
                                "FROM old.vectors o WHERE o.sha IN (SELECT sha FROM chunks) "
                                "OR o.sha IN (SELECT sha FROM units)").rowcount)
    finally:
        conn.execute("DETACH DATABASE old")


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
    return list(dict.fromkeys(w for w in WORD.findall(norm(query)) if len(w) > 1 and w not in STOP))[:24]


def stems(text: str, every: bool = False) -> set[str]:
    """What topic recall matches on, a word's first five letters: of `text`'s terms, or with `every` of each word it holds."""
    return {w[:5] for w in (WORD.findall(norm(text)) if every else terms(text))}


def rarity(total: int, df: int) -> float:
    """A word's weight by how rare it is: said in `df` of `total` chunks (search), or of `total` rules (memory.on_topic)."""
    return math.log(1 + (max(total, df) - df + 0.5) / (df + 0.5))


def search(conn: sqlite3.Connection, query: str, k: int = 6, pool: int = 80,
           weights: dict[str, float] | None = None, skip: Path | None = None) -> list[dict[str, Any]]:
    """Ranked chunks for a query: any of its words (FTS5, stemmed), ordered by how much of the
    query's weight a chunk covers (each distinct word weighted by its rarity), then by bm25.
    `weights`, when given, gets each word's weight (recall's snippet cut reads them). No chunk under the
    folder `skip` is a candidate, so none of them takes a place in the pool (a filter after it could leave none)."""
    if not (ts := terms(query)):
        return []
    out, args = "", [" OR ".join(f'"{t}"' for t in ts)]
    if skip:
        out, args = " AND instr(c.path, ?) != 1", [*args, f"{skip}/"]
    rows = conn.execute("SELECT c.id, c.path, c.lo, c.hi, c.body, bm25(chunks_fts) FROM chunks_fts "
                        f"JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?{out} "
                        "ORDER BY bm25(chunks_fts) LIMIT ?", (*args, pool)).fetchall()
    if not rows:
        return []
    total = int((conn.execute("SELECT value FROM meta WHERE key = 'chunks'").fetchone() or [len(rows)])[0])
    ids = [r[0] for r in rows]
    marks = ",".join("?" * len(ids))
    cover: dict[int, float] = {}
    for t in ts:
        df = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (f'"{t}"',)).fetchone()[0]
        w = rarity(total, df)
        if weights is not None:
            weights[t] = w
        for (rid,) in conn.execute(f"SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid IN ({marks})", (f'"{t}"', *ids)):
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
    status = "scope-changed" if key != scope.key() else "stale" if old or gone else "pending" if changed else "fresh"
    return {"status": status, "files": len(disk), "indexed": len(indexed), "changed": len(changed),
            "stale": len(old), "gone": len(gone), "lag_s": round(lag, 1), "synced_ago_s": round(now - synced, 1), "examples": (old or gone or changed)[:3]}


CHECK_EXIT = {"fresh": 0, "pending": 0, "stale": 1, "scope-changed": 1, "failure": 2, "timeout": 3}
BACKSTOP = 60.0          # seconds a watched loop waits with no event before it walks anyway (a lost event)


class Wake:
    """inotify on the folders a pass read (the approach of workers.Watch): the loop sleeps until a Markdown file there,
    a root that is one file, or a folder moves. With no inotify, or past the watch limit, it walks every `interval` as
    it did; BACKSTOP catches what the watch cannot see (a network or Windows mount, an edit through another hard link)."""
    MASK = 0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x800   # MODIFY ATTRIB CLOSE_WRITE MOVED_FROM MOVED_TO CREATE DELETE MOVE_SELF

    def __init__(self, names: tuple[str, ...] = ()):
        import ctypes
        self.libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if (fd := int(self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC))) < 0:
            log.warning("no inotify (%s): a pass every interval", os.strerror(ctypes.get_errno()))
        self.fd, self.names, self.wds = fd, {os.fsencode(n) for n in names}, set[int]()

    def watch(self, folder: str) -> bool:
        """True for a folder new to the watch: the caller walks once more, for a file made before the watch."""
        wd = int(self.libc.inotify_add_watch(self.fd, os.fsencode(folder), self.MASK)) if self.fd >= 0 else -1
        if wd < 0 <= self.fd and os.path.isdir(folder):          # past the watch limit (a folder gone is no matter)
            log.warning("no watch for %s: a pass every interval from now", folder)
            os.close(self.fd)
            self.fd = -1
        new = wd >= 0 and wd not in self.wds
        self.wds.add(wd)
        return new

    def wait(self, timeout: float) -> bool:
        """True when a Markdown file or a folder moved within `timeout` seconds; with no watch, a sleep."""
        if not select.select([self.fd] if self.fd >= 0 else [], [], [], max(0.0, timeout))[0]:
            return False
        data, i, hit = os.read(self.fd, 65536), 0, False
        while i + 16 <= len(data):
            _, mask, _, n = struct.unpack_from("iIII", data, i)
            name, i = data[i + 16:i + 16 + n].rstrip(b"\0"), i + 16 + n
            hit = hit or bool(mask & 0x4000C800) or name.endswith(b".md") or name in self.names   # OVERFLOW IGNORED MOVE_SELF ISDIR
        return hit


def _libc() -> Any:
    """mmap through libc, not the mmap module: that one keeps a dup of the descriptor and closes it with the map (Pin)."""
    lib = ctypes.CDLL(None, use_errno=True)
    lib.mmap.restype = ctypes.c_void_p
    lib.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    lib.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.madvise.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    return lib


# Linux's own numbers, hard-coded for ctypes (asm-generic/mman-common.h); where madvise answers EINVAL to 22 (a kernel
# before 5.14) the pin goes off with one log line
PROT_READ, MAP_SHARED, MAP_FAILED, MADV_DONTNEED = 1, 1, ctypes.c_void_p(-1).value, 4
MADV_POPULATE_READ = 22                # Linux 5.14: fault a range in and map it, unlocked; a page past the end fails, never SIGBUS


class Pin:
    """The keyword tables' pages, held in RAM from inside this process. A box may drop its page cache on a clock
    (`echo 1 > /proc/sys/vm/drop_caches`; the trimmer on the author's box does, every 2 minutes), and a keyword rank read
    off the disk after that took 3.8 to 4.5 s against 0.15 to 0.8 s from RAM (measured 2026-10-22, PR recall-warm). A drop
    evicts no page a process has mapped. So a pass maps the index file read-only and faults the pages of PIN_TABLES
    (dbstat) into the mapping, and every reader of the file finds them in RAM from then on, the daemon's recall among
    them. The writer's commits land in those same page-cache pages, so nothing held goes
    stale. Mapped, never locked: under real memory pressure the kernel may still take a page, and the next pass maps it
    again, where a locked page stays charged, unevictable, to whichever unit read it first (a room's, often). A
    connection's own page cache (PRAGMA cache_size) cannot do this job: SQLite drops it whole whenever another connection
    has committed, and the writer commits every pass. A pass runs at start, within 5 s of a writer pass that changed
    chunks (`due`), every PIN_EVERY seconds and after a rebuild; it maps afresh and lets the last mapping go after, so a
    page that left the tables is let go and none is unmapped in between. The descriptor stays open while this process
    may hold a connection to the file: closing any descriptor of a file drops every POSIX lock the process holds on it,
    SQLite's among them (sqlite.org/howtocorrupt.html 2.2). A kernel before 5.14, or a SQLite without dbstat, pins
    nothing; one log line says which."""

    def __init__(self, db: Path, tables: tuple[str, ...] = PIN_TABLES):
        self.db, self.tables = db, tables
        self.real: Path | None = None
        self.fd = -1                                          # the live generation's, open until close()
        self.retired: list[int] = []                          # one a rebuild moved off: closed at the next pass
        self.addr = self.size = 0                             # the last pass's mapping
        self.mode = "map"                                     # map, or off once a pass found it cannot
        self.pages = self.bytes = self.passes = 0             # what the last pass mapped
        self.took = self.at = 0.0
        self.stop = threading.Event()
        self.due = threading.Event()                          # main sets it after a writer pass that changed chunks
        self.thread: threading.Thread | None = None
        self.lib = _libc()

    def _unmap(self) -> None:
        if self.addr:
            self.lib.munmap(self.addr, self.size)
        self.addr = self.size = 0

    def _off(self, why: str) -> dict[str, Any]:
        log.warning("pin: nothing pinned, %s", why)
        self.mode = "off"
        self._unmap()
        return self.status()

    def pass_(self) -> dict[str, Any]:
        """One pass: the tables' page numbers, a fresh mapping of the live generation with those pages faulted in, then
        the last pass's mapping let go."""
        t0 = time.monotonic()
        for fd in self.retired:                  # a generation retired a pass ago: the writer left it within its own pass
            os.close(fd)
        self.retired = []
        if (real := live(self.db)) != self.real:                              # the first pass, or a rebuild swapped the link
            fd = os.open(real, os.O_RDONLY | os.O_CLOEXEC)                    # first: a failed open retires nothing
            self.retired += [self.fd] if self.fd >= 0 else []
            self.fd, self.real = fd, real
        size = os.fstat(self.fd).st_size
        conn = connect(real, readonly=True)
        try:
            page = int(conn.execute("PRAGMA page_size").fetchone()[0])
            nos = sorted({int(r[0]) for t in self.tables for r in conn.execute("SELECT pageno FROM dbstat WHERE name = ?", (t,))})
        except sqlite3.OperationalError as e:
            if "dbstat" not in str(e):           # busy or unreadable this once: run() logs the skip and a later pass tries again
                raise
            return self._off(f"this SQLite has no dbstat ({e})")
        finally:
            conn.close()
        runs: list[tuple[int, int]] = []                                      # (first page, pages): consecutive pages as one
        for no in nos:
            if runs and runs[-1][0] + runs[-1][1] == no:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((no, 1))
        addr = self.lib.mmap(None, size, PROT_READ, MAP_SHARED, self.fd, 0)
        if not addr or addr == MAP_FAILED:
            raise OSError(ctypes.get_errno(), f"mmap of {real.name} failed")
        osp, held, end = os.sysconf("SC_PAGE_SIZE"), 0, 0
        for first, n in runs:
            lo, hi = (first - 1) * page, min((first - 1 + n) * page, size)  # a page past the file is still in the WAL
            lo -= lo % osp                                                    # madvise takes whole pages of the OS's own
            if hi <= lo:
                continue
            if self.lib.madvise(addr + lo, hi - lo, MADV_POPULATE_READ) == 0:
                held += hi - lo
            elif ctypes.get_errno() == errno.EINVAL:                          # a kernel before 5.14
                self.lib.munmap(addr, size)
                return self._off("this kernel cannot fault a mapping in (MADV_POPULATE_READ, Linux 5.14)")
        # a fault maps the cached pages around it too (fault-around: the mapping held 648 MB for the tables' 414 on the
        # author's box), so the gaps between the runs are unmapped again and the mapping holds the tables' pages, no more
        for first, n in [*runs, (size // page + 1, 0)]:
            lo = min((first - 1) * page // osp * osp, size)
            if end < lo:
                self.lib.madvise(addr + end, lo - end, MADV_DONTNEED)
            end = max(end, -(-min((first - 1 + n) * page, size) // osp) * osp)
        self._unmap()                                                         # the last pass's: its pages are mapped here now
        self.addr, self.size = int(addr), size
        self.pages, self.bytes, self.passes = held // page, held, self.passes + 1
        self.took, self.at = time.monotonic() - t0, time.time()
        return self.status()

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "pages": self.pages, "bytes": self.bytes, "took_s": round(self.took, 2),
                "at": round(self.at, 3), "passes": self.passes, "tables": list(self.tables)}

    def run(self) -> None:
        last = -PIN_EVERY
        while not self.stop.is_set():
            if self.due.is_set() or time.monotonic() - last >= PIN_EVERY or live(self.db) != self.real:
                self.due.clear()                                              # a write during the pass sets it again
                try:
                    st = self.pass_()
                    if st["passes"] == 1:                                     # the first; each after is in index.json
                        log.info("pin: %d pages (%.0f MB) of %s mapped in %.1f s", st["pages"], st["bytes"] / 1e6,
                                 ", ".join(self.tables), st["took_s"])
                except (OSError, sqlite3.Error) as e:
                    log.warning("pin pass skipped: %s", e)
                last = time.monotonic()
                if self.mode == "off":
                    return
            self.stop.wait(5.0)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="yerk-index-pin", daemon=True)
        self.thread.start()

    def close(self) -> None:
        """The pass thread stopped, the mapping let go, the descriptors closed: main calls this after the writer and the
        server are closed, so closing them drops no lock a connection of this process still needs."""
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=30.0)
        self._unmap()
        for fd in [*self.retired, self.fd]:
            if fd >= 0:
                os.close(fd)
        self.retired, self.fd, self.real = [], -1, None


def main(argv: list[str] | None = None) -> int:
    from . import mentions
    from .config import REPO, head, private_dir, state_dir
    from .memory import settings
    p = argparse.ArgumentParser(prog="yerk-index", description="The memory index: FTS5 over the private "
                                "memory tree, updated per changed chunk, in its own process.")
    p.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    p.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    p.add_argument("--interval", type=float, help="the least seconds between passes (default: [memory] interval, else 2)")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--rebuild", action="store_true", help="drop the index and build it again (the only full reindex)")
    p.add_argument("--check", action="store_true", help="compare the index with the disk; exit 0 fresh or "
                   "pending, 1 stale or scope changed, 2 failure, 3 timeout")
    p.add_argument("--deadline", type=float, default=10.0, help="--check: seconds before it reads as a timeout")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    m = settings(private_dir(a.private), state_dir(a.state))
    if a.check:
        print(json.dumps(r := check(m.index_db, m.scope, a.deadline)))
        return CHECK_EXIT[r["status"]]
    if a.rebuild:
        rebuild(m.index_db, m.scope)
    from . import embeddings
    embedder = embeddings.Embedder(m.embeddings_key, m.embeddings_model, m.embeddings_dim)
    store = embeddings.Vectors(m.embeddings_dim, m.embeddings_width)
    back = embeddings.Backfill(m.index_db, live, store, embedder, units=m.embeddings_units)
    idx = Index(m.index_db, m.scope, on_insert=back.note)
    names = mentions.Builder(m.mentions, m.roots_for_mentions, m.curated, m.mentions_exclude)
    server = embeddings.Server(store, embedder, m.index_db, live)
    pin = Pin(m.index_db)
    if not a.once:
        embeddings.write_status(m.index_status, commit=(commit := head(REPO)))     # the code that runs, before the load (deploy, doctor)
        loaded = store.load(idx.conn, m.embeddings_model, m.embeddings_units)
        port = server.start()
        pin.start()
        back.start()
        log.info("meaning search: %s; %d vectors loaded (%s, %d of %d dimensions kept, %.1f MB); serving on 127.0.0.1:%d",
                 "key present" if embedder.available() else "no key, keywords only", loaded, m.embeddings_units,
                 store.width, m.embeddings_dim, store.bytes() / 1e6, port)
    stop: list[int] = []                                     # the signal that ends the loop, once one came
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda n, _: stop.append(n))
    interval = a.interval or m.interval
    wake = Wake(tuple(r.name for r in m.scope.roots if r.is_file()))
    first, chunks = True, 0
    while not stop:
        t0 = time.monotonic()
        try:
            st = idx.sync()
            chunks = st["chunks"]
            if st.get("inserted") or st.get("deleted"):      # the pin maps the new chunks' pages within 5 s, not 10 min
                pin.due.set()
        except sqlite3.OperationalError as e:
            log.warning("pass skipped (%s): %s", "timeout" if is_timeout(e) else "failure", e)
            st = {}
        try:
            if (built := names.refresh(idx.disk)) is not None:
                log.info("mention index: %d keys, %d entries, %d dropped as ask-only", *built)
            for gone in sweep(m.index_db):
                log.info("swept the old generation file %s", gone)
            if not a.once:
                embeddings.write_status(m.index_status, port=server.port, chunks=chunks, commit=commit, embeddings={
                    "key": embedder.available(), "model": m.embeddings_model, "dim": m.embeddings_dim, "vectors": store.n,
                    "units": m.embeddings_units, "width": store.width, **back.status(), "requests": embedder.requests,
                    "tokens": embedder.tokens, "rss_kb": embeddings.rss_kb(), "store_bytes": store.bytes()},
                    pin=pin.status())
        except OSError as e:
            log.warning("mention index, sweep or status skipped: %s", e)
        took = time.monotonic() - t0
        if first or st.get("changed") or st.get("gone"):
            log.info("pass: %d files, %d changed, %d gone, %d chunks in, %d out, %.2f s", st.get("files", 0),
                     st.get("changed", 0), st.get("gone", 0), st.get("inserted", 0), st.get("deleted", 0), took)
            first = False
        if a.once:
            break
        again = any([wake.watch(f) for f in idx.folders]) or not st      # a new folder, or a skipped pass: walk again
        soon = time.monotonic() + max(0.2, interval - took)              # never two passes closer than `interval`;
        late, woke = soon + BACKSTOP, again or wake.fd < 0               # past it, a file in scope that moves starts one
        while not stop and time.monotonic() < (soon if woke else late):
            woke = wake.wait(min(1.0, (soon if woke else late) - time.monotonic())) or woke
    back.stop.set()
    server.stop()
    idx.close()
    pin.close()                                              # last: its descriptors close after the connections (Pin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
