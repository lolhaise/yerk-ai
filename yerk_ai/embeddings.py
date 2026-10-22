"""Search by meaning beside the keyword search, so a room's memory search finds a note that says the same thing in other words.

yerk-index turns every chunk into a vector once, from an outside provider, and stores it next to
the chunk's hash: a chunk whose text did not change is never embedded again, a touch embeds
nothing, an appended line embeds one chunk. The vectors live in the index file (table `vectors`,
keyed by the chunk hash, so a rebuild copies them and never pays twice) and in this process's
memory as one float16 matrix. A local HTTP endpoint answers a query with the nearest chunks; the
daemon's recall fuses that rank with the keyword rank (DESIGN 5; decisions 10 and 23).

The key is a path in the private layer (`[memory] embeddings_key`, default secrets/openai.key),
read by this process alone when a request needs it, never logged, printed or kept in a report.
Without it nothing here runs and recall is keywords only, exactly as before. A provider error or
timeout is told apart from a failure of the index itself: the query answers keywords only and the
reason is logged.
"""
from __future__ import annotations

import base64
import contextlib
import itertools
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

from . import memindex
from .config import write_private

log = logging.getLogger("yerk.embeddings")

MODEL = "text-embedding-3-small"
DIM = 256
URL = "https://api.openai.com/v1/embeddings"
BATCH = 1024             # texts per request at most (the provider takes up to 2,048); BYTES cuts it first when the texts are long
BYTES = 250_000          # UTF-8 bytes per request at most: the provider caps a request at 300,000 tokens, and its tokenizer (byte-level
                         # BPE, cl100k_base) never makes more tokens than bytes; the rest is margin (1,015 chunks of texts, 2026-10-21)
TOO_BIG = re.compile(r"HTTP 400 .*(?:request size|tokens per request)", re.S)     # the provider's 400 for a request past its cap
TIMEOUT = 30.0           # seconds per backfill request
QUERY_TIMEOUT = 4.0      # seconds for a query's one vector: a recall waits at most this, no retry
RETRIES = 3              # the retry budget per backfill request, 1, 2 and 4 s apart
PAUSE = 60.0             # seconds the backfill rests after a request spends its budget
GAP = 0.25               # seconds between backfill requests: the provider and the CPU are shared
BLOCK = 16384            # rows per block of the store, and per step of the scan (float16 rows widened to float32 a block at a time)
HIGH_BASE = 256 << 20    # bytes yerk-index holds beside its store: its unit's MemoryHigh rule is this and a block per BLOCK vectors
LOAD_ROWS = 4096         # rows fetched per step of the load: the temporary copies stay small, so the load peak stays near the steady size
KEY_SHAPE = re.compile(r"sk-[A-Za-z0-9_-]{6,}")


class ProviderError(Exception):
    """The provider said no (a bad key, a bad request, a 5xx past the retry budget)."""


class ProviderTimeout(ProviderError):
    """No answer inside the timeout, past the retry budget: says nothing about the index."""


def scrub(text: object) -> str:
    """Error text with anything key-shaped removed, cut short. Every log line here passes through it."""
    return KEY_SHAPE.sub("sk-[removed]", str(text))[:240]


class Embedder:
    """The provider client. The key file is read per request and never held between them."""

    def __init__(self, key_file: Path, model: str = MODEL, dim: int = DIM, timeout: float = TIMEOUT,
                 retries: int = RETRIES, url: str = URL):
        self.key_file, self.model, self.dim, self.timeout, self.retries, self.url = key_file, model, dim, timeout, retries, url
        self.requests = self.tokens = 0

    def available(self) -> bool:
        """A key file is there and not empty. Its content is never looked at here."""
        try:
            return self.key_file.stat().st_size > 0
        except OSError:
            return False

    def _key(self) -> str | None:
        try:
            k = self.key_file.read_text().strip()
        except OSError:
            return None
        return k or None

    def embed(self, texts: list[str], timeout: float | None = None, retries: int | None = None) -> np.ndarray:
        """(len(texts), dim) float32 unit vectors, in order. ProviderTimeout or ProviderError past the budget."""
        if (key := self._key()) is None:
            raise ProviderError("no provider key")
        body = json.dumps({"model": self.model, "input": texts, "dimensions": self.dim, "encoding_format": "base64"}).encode()
        timeout = self.timeout if timeout is None else timeout
        retries = self.retries if retries is None else retries
        wait = 1.0
        for attempt in range(retries + 1):
            req = urllib.request.Request(self.url, data=body, headers={"Authorization": f"Bearer {key}",
                                                                       "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                try:
                    detail = scrub(e.read().decode(errors="replace"))
                except OSError:
                    detail = ""
                if e.code in (408, 429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(wait)
                    wait *= 2
                    continue
                raise ProviderError(f"HTTP {e.code} from the provider: {detail}") from None
            except (urllib.error.URLError, OSError, ValueError) as e:
                if attempt < retries:
                    time.sleep(wait)
                    wait *= 2
                    continue
                if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError) or "timed out" in str(e).lower():
                    raise ProviderTimeout(f"no answer from the provider in {timeout:.0f} s") from None
                raise ProviderError(f"the provider could not be reached: {scrub(e)}") from None
        del key
        rows = sorted(data["data"], key=lambda d: int(d["index"]))
        if len(rows) != len(texts):
            raise ProviderError(f"the provider returned {len(rows)} vectors for {len(texts)} texts")
        out = np.stack([np.frombuffer(base64.b64decode(d["embedding"]), dtype="<f4") for d in rows]).astype(np.float32)
        if out.shape[1] != self.dim:
            raise ProviderError(f"the provider returned {out.shape[1]} dimensions, not {self.dim}")
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        out /= np.where(norms == 0, 1.0, norms)
        self.requests += 1
        self.tokens += int(data.get("usage", {}).get("total_tokens", 0))
        return out


UNITS = ("lines", "chunks", "both")     # what gets a vector in memory: the lines (and bullets), the chunks, or both
UNIT_WHERE = {"lines": "EXISTS (SELECT 1 FROM units u WHERE u.sha = v.sha)",
              "chunks": "EXISTS (SELECT 1 FROM chunks c WHERE c.sha = v.sha)"}


def unit_where(units: str) -> str:
    if units not in UNITS:
        raise ValueError(f"embeddings_units must be one of {', '.join(UNITS)}, not {units!r}")
    return UNIT_WHERE.get(units) or f"({UNIT_WHERE['lines']} OR {UNIT_WHERE['chunks']})"


class Vectors:
    """Every served vector in memory, float16, in blocks of BLOCK rows with the hashes beside them.
    Blocks are never reallocated: growth adds a block, so the store's resident size never doubles
    while it grows (one matrix copied on growth did). Readers take the row count under the lock and
    scan up to it; a writer only fills rows past it, so a scan never sees a half-written row.
    `width` keeps the first `width` of each vector's `dim` dimensions, renormalised, which is the
    provider's own rule for a shorter vector: a narrower store from the same vectors, no re-embedding."""

    def __init__(self, dim: int = DIM, width: int | None = None):
        self.dim = dim
        self.width = min(dim, width or dim)
        self.lock = threading.Lock()
        self.blocks: list[np.ndarray] = []
        self.shas: list[np.ndarray] = []
        self.n = 0

    def narrow(self, vecs: np.ndarray) -> np.ndarray:
        """(rows, width) float32 unit vectors from (rows, dim) ones."""
        v = np.asarray(vecs, dtype=np.float32)[:, :self.width]
        if self.width < self.dim:
            norms = np.linalg.norm(v, axis=1, keepdims=True)
            v = v / np.where(norms == 0, 1.0, norms)
        return v

    def _put(self, shas: list[str], vecs: np.ndarray) -> None:
        v16 = self.narrow(vecs).astype(np.float16)
        sh = np.frombuffer(b"".join(bytes.fromhex(s) for s in shas), dtype=np.uint8).reshape(len(shas), 20)
        i = 0
        while i < len(shas):
            b, off = divmod(self.n, BLOCK)
            if b == len(self.blocks):
                self.blocks.append(np.zeros((BLOCK, self.width), dtype=np.float16))
                self.shas.append(np.zeros((BLOCK, 20), dtype=np.uint8))
            take = min(BLOCK - off, len(shas) - i)
            self.blocks[b][off:off + take] = v16[i:i + take]
            self.shas[b][off:off + take] = sh[i:i + take]
            self.n += take
            i += take

    def load(self, conn: sqlite3.Connection, model: str, units: str = "both") -> int:
        """Every stored vector of the chosen units, a block at a time (never the whole table at once)."""
        cur = conn.execute(f"SELECT v.sha, v.vec FROM vectors v WHERE v.model = ? AND v.dim = ? AND {unit_where(units)}", (model, self.dim))
        fresh = Vectors(self.dim, self.width)
        while rows := cur.fetchmany(LOAD_ROWS):
            fresh._put([s for s, _ in rows], np.stack([np.frombuffer(v, dtype=np.float16) for _, v in rows]))
        with self.lock:
            self.blocks, self.shas, self.n = fresh.blocks, fresh.shas, fresh.n
        return self.n

    def add(self, shas: list[str], vecs: np.ndarray) -> None:
        with self.lock:
            self._put(shas, vecs)

    def search(self, q: np.ndarray, k: int) -> list[tuple[str, float]]:
        """The k nearest stored vectors to the unit vector q: (hash, cosine), best first."""
        with self.lock:
            n, blocks, shas = self.n, self.blocks, self.shas
        if n == 0 or k <= 0:
            return []
        qf = self.narrow(np.asarray(q, dtype=np.float32)[None, :])[0]
        scores = np.empty(n, dtype=np.float32)
        for b, block in enumerate(blocks):
            if (lo := b * BLOCK) >= n:
                break
            hi = min(lo + BLOCK, n)
            scores[lo:hi] = block[:hi - lo].astype(np.float32) @ qf
        k = min(k, n)
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [(shas[i // BLOCK][i % BLOCK].tobytes().hex(), float(scores[i])) for i in top]

    def bytes(self) -> int:
        return sum(int(b.nbytes) for b in self.blocks) + sum(int(s.nbytes) for s in self.shas)


def rss_kb() -> int:
    """This process's resident size from /proc, in kB (0 where /proc is not there)."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


class Server:
    """The meaning-search endpoint of yerk-index, local only. POST /search {"query", "pool", "near"} answers
    the nearest chunks (rows as the keyword search returns them, plus the cosine as `score`, and with
    `near: true` the matched line as `near`, which a daemon that does not ask never receives), or
    `ok: false` with a status told apart by kind: no-key, provider-timeout, provider-error, empty.
    GET /status answers the counts. Each request runs on its own thread with its own read-only
    connection, so a scan never waits on the writer. POST /keywords {"query", "k", "pool", "skip", "deadline"} answers
    the keyword rank (`keywords` below) on this process's one long-lived connection, one request at a time."""

    def __init__(self, store: Vectors, embedder: Embedder, db: Path, live: Any, cache_kib: int = 16_000):
        self.store, self.embedder, self.db, self.live = store, embedder, db, live
        self.port = 0
        self.httpd: ThreadingHTTPServer | None = None
        self.queries = self.keyword_queries = 0
        self.cache_kib = int(cache_kib)                 # the keyword connection's page cache, sized to the FTS tables by main
        self.kw: sqlite3.Connection | None = None
        self.kw_real: Path | None = None
        self.kw_lock = threading.Lock()

    def _keyword_conn(self) -> sqlite3.Connection:
        """This process's one long-lived read connection for the keyword rank, its page cache sized to the FTS tables
        (cache_kib: 16 MB is memindex.connect's own); opened again after a rebuild swapped the link."""
        real = self.live(self.db)
        if self.kw is None or real != self.kw_real:
            if self.kw is not None:
                self.kw.close()
            self.kw = memindex.connect(self.db, readonly=True, shared=True)      # each request runs on its own thread
            self.kw.execute(f"PRAGMA cache_size=-{self.cache_kib}")
            self.kw_real = real
        return self.kw

    def keywords(self, query: str, k: int, pool: int, skip: str | None = None, deadline_s: float = 4.0) -> dict[str, Any]:
        """The keyword rank, memindex.search as the daemon runs it on the index file, on this process's one long-lived
        connection, one request at a time: the rows in the same shape and order, plus the words' weights (recall's snippet
        cut reads them). The deadline counts from the request, the wait for the connection included: a request still
        queued at it, or a statement past it (interrupted), reads as a timeout, told apart from a failure; a statement's
        timeout or failure closes the connection, so the next request opens a fresh one. The pin (memindex.Pin) is what
        keeps this fast across the box's page-cache drops: the connection's own cache is dropped by SQLite at the writer's
        every commit, and the writer commits every pass."""
        self.keyword_queries += 1
        end = time.monotonic() + max(0.05, float(deadline_s))
        if not self.kw_lock.acquire(timeout=max(0.0, end - time.monotonic())):
            return {"ok": False, "status": "timeout", "error": "the keyword connection was busy past the deadline"}
        try:
            conn = self._keyword_conn()
            conn.set_progress_handler(lambda: 1 if time.monotonic() > end else 0, 2000)
            try:
                weights: dict[str, float] = {}
                hits = memindex.search(conn, query, k, pool, weights, Path(skip) if skip else None)
            finally:
                conn.set_progress_handler(None, 0)
        except (sqlite3.Error, OSError) as e:
            if self.kw is not None:
                self.kw.close()
            self.kw = None
            return {"ok": False, "status": "timeout" if isinstance(e, sqlite3.Error) and memindex.is_timeout(e) else "failure",
                    "error": scrub(e)}
        finally:
            self.kw_lock.release()
        return {"ok": True, "status": "ok", "hits": hits, "weights": weights}

    def search(self, query: str, pool: int, near: bool = False) -> dict[str, Any]:
        self.queries += 1
        if not self.embedder.available():
            return {"ok": False, "status": "no-key", "error": "no provider key: keywords only"}
        if self.store.n == 0:
            return {"ok": False, "status": "empty", "error": "no vectors yet: the backfill has not started or has nothing"}
        try:
            q = self.embedder.embed([query], timeout=QUERY_TIMEOUT, retries=0)[0]
        except ProviderTimeout as e:
            return {"ok": False, "status": "provider-timeout", "error": str(e)}
        except ProviderError as e:
            return {"ok": False, "status": "provider-error", "error": scrub(e)}
        pool = max(1, min(int(pool), 400))
        score = dict(self.store.search(q, pool))
        marks = ",".join("?" * len(score))
        conn = sqlite3.connect(f"file:{self.live(self.db)}?mode=ro", uri=True, timeout=2.0)
        try:
            # a hash is a line's (through units) or a chunk's own; a chunk keeps its best line's cosine, and,
            # when the request asks, that line's text as `near` (recall's snippet keeps the line the meaning
            # matched; an older daemon never asks, and its room filter would pass the line on unfiltered)
            rows = conn.execute(f"SELECT c.id, c.path, c.lo, c.hi, c.body, u.sha, u.body FROM units u JOIN chunks c ON "
                                f"c.sha = u.chunk WHERE u.sha IN ({marks}) UNION ALL SELECT id, path, lo, hi, body, sha, NULL "
                                f"FROM chunks WHERE sha IN ({marks})", list(score) * 2).fetchall()
        finally:
            conn.close()
        best: dict[int, dict[str, Any]] = {}
        for cid, p, lo, hi, body, sha, line in rows:
            if cid not in best or score[sha] > best[cid]["score"]:
                best[cid] = {"id": cid, "path": p, "line": lo, "lines": f"{lo}-{hi}", "text": body, "score": round(score[sha], 4),
                             **({"near": line} if near and line else {})}
        hits = sorted(best.values(), key=lambda h: -h["score"])[:pool]
        return {"ok": True, "status": "ok", "hits": hits, "vectors": self.store.n}

    def status(self) -> dict[str, Any]:
        return {"vectors": self.store.n, "model": self.embedder.model, "dim": self.embedder.dim, "width": self.store.width,
                "key": self.embedder.available(), "queries": self.queries, "requests": self.embedder.requests,
                "tokens": self.embedder.tokens, "rss_kb": rss_kb(), "store_bytes": self.store.bytes(),
                "keyword_queries": self.keyword_queries, "cache_kib": self.cache_kib}

    def start(self) -> int:
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:     # quiet: the index log is for passes
                pass

            def _send(self, obj: dict[str, Any], code: int = 200) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._send(srv.status() if self.path == "/status" else {"ok": False, "error": "no such path"}, 200)

            def do_POST(self) -> None:
                if self.path not in ("/search", "/keywords"):
                    self._send({"ok": False, "status": "failure", "error": "no such path"}, 404)
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(n) or b"{}")
                    if self.path == "/keywords":
                        out = srv.keywords(str(payload.get("query", "")), int(payload.get("k", 80)), int(payload.get("pool", 160)),
                                           str(payload["skip"]) if payload.get("skip") else None, float(payload.get("deadline", 4.0)))
                    else:
                        out = srv.search(str(payload.get("query", "")), int(payload.get("pool", 80)), payload.get("near") is True)
                except (ValueError, sqlite3.Error, OSError) as e:
                    out = {"ok": False, "status": "failure", "error": scrub(e)}
                self._send(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, name="yerk-index-search", daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        with self.kw_lock:
            if self.kw is not None:
                self.kw.close()
            self.kw = None


class Backfill:
    """The embedding thread of yerk-index: chunks without a vector, newest first, in batches, with a
    gap between requests so the provider and the box are never hammered; resumable, since every
    batch is committed as it lands and a restart asks the index what is still missing. Shared with
    the writer through `pending` (hashes of chunks just inserted) and `dirty` (scan again)."""

    def __init__(self, db: Path, live: Any, store: Vectors, embedder: Embedder, batch: int = BATCH, gap: float = GAP, units: str = "both"):
        self.db, self.live, self.store, self.embedder, self.batch, self.gap = db, live, store, embedder, batch, gap
        self.units, self.cap = units, BYTES
        unit_where(units)
        self.pending: list[str] = []
        self.lock = threading.Lock()
        self.dirty = threading.Event()
        self.dirty.set()
        self.stop = threading.Event()
        self.paused_until = 0.0
        self.done = self.failed = self.streak = 0       # streak: pauses in a row, none embedded between (the doctor reads it)
        self.last_error = ""
        self.thread: threading.Thread | None = None
        self.conn: sqlite3.Connection | None = None
        self.real: Path | None = None

    def note(self, chunks: list[str], units: list[str] | None = None) -> None:
        """The writer's new chunk and unit hashes: the ones this store serves are embedded next, before any scan."""
        want = (units or [] if self.units != "chunks" else []) + (chunks if self.units != "lines" else [])
        with self.lock:
            self.pending.extend(want)

    def _conn(self) -> sqlite3.Connection:
        """This thread's own writer connection; after a rebuild swapped the link it moves to the new
        generation and loads the vectors the rebuild copied there."""
        real = self.live(self.db)
        if self.conn is None or real != self.real:
            if self.conn is not None:
                self.conn.close()
            self.conn = sqlite3.connect(real, timeout=5.0, isolation_level=None)
            if self.real is not None:
                log.info("the index was rebuilt: %d vectors loaded from %s", self.store.load(self.conn, self.embedder.model), real.name)
                self.dirty.set()
            self.real = real
        return self.conn

    def missing(self, conn: sqlite3.Connection, limit: int) -> list[str]:
        """Hashes the store serves that have no vector yet, newest chunk first."""
        none = "NOT EXISTS (SELECT 1 FROM vectors v WHERE v.sha = {sha} AND v.model = ? AND v.dim = ?)"
        lines = ("SELECT u.sha AS sha, max(c.id) AS o FROM units u JOIN chunks c ON c.sha = u.chunk WHERE "
                 + none.format(sha="u.sha") + " GROUP BY u.sha")
        chunks = "SELECT c.sha AS sha, max(c.id) AS o FROM chunks c WHERE " + none.format(sha="c.sha") + " GROUP BY c.sha"
        parts = {"lines": [lines], "chunks": [chunks], "both": [lines, chunks]}[self.units]
        args = (self.embedder.model, self.embedder.dim) * len(parts)
        return [r[0] for r in conn.execute("SELECT sha FROM (" + " UNION ALL ".join(parts) + ") ORDER BY o DESC LIMIT ?",
                                           (*args, limit)).fetchall()]

    def take(self, conn: sqlite3.Connection) -> list[str]:
        with self.lock:
            batch, self.pending = self.pending[:self.batch], self.pending[self.batch:]
        if not batch and self.dirty.is_set():
            found = self.missing(conn, self.batch * 8)        # one scan feeds several batches
            batch = found[:self.batch]
            with self.lock:
                self.pending.extend(found[self.batch:])
            if not batch:
                self.dirty.clear()
        return list(dict.fromkeys(batch))

    def step(self) -> int:
        """One batch: its texts from the index, one request of at most `cap` bytes (the rest goes back to the front of
        the queue), one transaction. Chunks already embedded (a hash queued twice) are skipped. A 400 that names the
        request size halves `cap` and puts the batch back, so the next request is smaller and never the same one (F2,
        2026-10-21). Returns the number embedded."""
        conn = self._conn()
        if not (shas := self.take(conn)):
            return 0
        marks = ",".join("?" * len(shas))
        have = {r[0] for r in conn.execute(f"SELECT sha FROM vectors WHERE sha IN ({marks}) AND model = ? AND dim = ?",
                                           (*shas, self.embedder.model, self.embedder.dim))}
        texts: dict[str, str] = {}
        for s, b in conn.execute(f"SELECT sha, min(body) FROM units WHERE sha IN ({marks}) GROUP BY sha UNION ALL "
                                 f"SELECT sha, min(body) FROM chunks WHERE sha IN ({marks}) GROUP BY sha", shas * 2):
            texts.setdefault(s, b)
        if not (rows := [(s, b) for s, b in texts.items() if s not in have]):
            return 0
        n = max(1, sum(e <= self.cap for e in itertools.accumulate(len(b.encode()) for _, b in rows)))   # the most that fit, one at least
        rows, rest = rows[:n], rows[n:]
        try:
            vecs = self.embedder.embed([b for _, b in rows])
        except ProviderError as e:
            if n == 1 or not TOO_BIG.match(str(e)):
                raise                                   # run() pauses, then scans again
            self.cap, self.failed, self.last_error, rest = sum(len(b.encode()) for _, b in rows) // 2, self.failed + 1, scrub(e), rows + rest
            log.warning("backfill: %d texts were one request too large; at most %d bytes a request from now", n, self.cap)
            return 0
        finally:
            with self.lock:
                self.pending[:0] = [s for s, _ in rest]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("INSERT OR REPLACE INTO vectors(sha, model, dim, vec) VALUES (?, ?, ?, ?)",
                             [(s, self.embedder.model, self.embedder.dim, np.asarray(v, dtype=np.float16).tobytes())
                              for (s, _), v in zip(rows, vecs)])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        self.store.add([s for s, _ in rows], vecs)
        self.done, self.streak = self.done + len(rows), 0
        return len(rows)

    def run(self) -> None:
        while not self.stop.is_set():
            if not self.embedder.available() or time.monotonic() < self.paused_until:
                self.stop.wait(2.0)
                continue
            try:
                n = self.step()
            except ProviderError as e:
                self.failed, self.streak = self.failed + 1, self.streak + 1
                self.last_error = scrub(e)
                log.warning("backfill paused %.0f s (%s): %s", PAUSE, "timeout" if isinstance(e, ProviderTimeout)
                            else "provider error", self.last_error)
                self.paused_until = time.monotonic() + PAUSE
                self.dirty.set()
                continue
            except sqlite3.Error as e:
                self.failed += 1
                self.last_error = scrub(e)
                log.warning("backfill skipped a batch (index busy or failed): %s", self.last_error)
                self.stop.wait(5.0)
                self.dirty.set()
                continue
            if n == 0:
                self.stop.wait(1.0)
            else:
                self.stop.wait(self.gap)
        if self.conn is not None:
            self.conn.close()

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="yerk-index-embed", daemon=True)
        self.thread.start()

    def status(self) -> dict[str, Any]:
        with self.lock:
            pending = len(self.pending)
        return {"done": self.done, "failed": self.failed, "streak": self.streak, "pending": pending, "scanning": self.dirty.is_set(),
                "paused": time.monotonic() < self.paused_until, "last_error": self.last_error}


def write_status(path: Path, **fields: Any) -> None:
    """<state>/memory/index.json, 0600: what yerk-index is serving (pid, port, the embeddings' state)."""
    write_private(path, json.dumps({"pid": os.getpid(), "at": round(time.time(), 3), **fields}))
