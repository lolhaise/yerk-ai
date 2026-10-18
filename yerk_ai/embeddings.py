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

log = logging.getLogger("yerk.embeddings")

MODEL = "text-embedding-3-small"
DIM = 256
URL = "https://api.openai.com/v1/embeddings"
BATCH = 256              # chunks per request (the provider takes up to 2,048)
TIMEOUT = 20.0           # seconds per backfill request
QUERY_TIMEOUT = 4.0      # seconds for a query's one vector: a recall waits at most this, no retry
RETRIES = 3              # the retry budget per backfill request, 1, 2 and 4 s apart
PAUSE = 60.0             # seconds the backfill rests after a request spends its budget
GAP = 0.25               # seconds between backfill requests: the provider and the CPU are shared
BLOCK = 16384            # rows scored per step of the scan (float16 rows widened to float32 a block at a time)
KEY_SHAPE = re.compile(r"sk-[A-Za-z0-9_-]{6,}")


class ProviderError(Exception):
    """The provider said no (a bad key, a bad request, a 5xx past the retry budget)."""


class ProviderTimeout(ProviderError):
    """No answer inside the timeout, past the retry budget: says nothing about the index."""


def scrub(text: object) -> str:
    """Error text with anything key-shaped removed, cut short. Every log line here passes through it."""
    return KEY_SHAPE.sub("sk-[removed]", str(text))[:240]


def _timed_out(e: BaseException) -> bool:
    reason = getattr(e, "reason", None)
    return isinstance(e, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(e).lower()


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
        key = self._key()
        if key is None:
            raise ProviderError("no provider key")
        body = json.dumps({"model": self.model, "input": texts, "dimensions": self.dim,
                           "encoding_format": "base64"}).encode()
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
                if _timed_out(e):
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


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float16).tobytes()


class Vectors:
    """Every stored vector in memory as one float16 matrix with its chunk hashes beside it. Readers
    take the row count under the lock and scan up to it; a writer only appends past it (into spare
    capacity, or into a fresh, larger array), so a scan never sees a half-written row."""

    def __init__(self, dim: int = DIM):
        self.dim = dim
        self.lock = threading.Lock()
        self.mat = np.zeros((0, dim), dtype=np.float16)
        self.shas = np.zeros(0, dtype="S40")
        self.n = 0

    def load(self, conn: sqlite3.Connection, model: str) -> int:
        rows = conn.execute("SELECT sha, vec FROM vectors WHERE model = ? AND dim = ?", (model, self.dim)).fetchall()
        if rows:
            mat = np.stack([np.frombuffer(v, dtype=np.float16) for _, v in rows])
            shas = np.array([s for s, _ in rows], dtype="S40")
        else:
            mat, shas = np.zeros((0, self.dim), dtype=np.float16), np.zeros(0, dtype="S40")
        with self.lock:
            self.mat, self.shas, self.n = mat, shas, len(rows)
        return len(rows)

    def add(self, shas: list[str], vecs: np.ndarray) -> None:
        if not shas:
            return
        with self.lock:
            need = self.n + len(shas)
            if need > len(self.mat):
                cap = max(need, len(self.mat) + len(self.mat) // 4 + 4096)
                mat = np.zeros((cap, self.dim), dtype=np.float16)
                mat[:self.n] = self.mat[:self.n]
                sh = np.zeros(cap, dtype="S40")
                sh[:self.n] = self.shas[:self.n]
                self.mat, self.shas = mat, sh
            self.mat[self.n:need] = vecs.astype(np.float16)
            self.shas[self.n:need] = np.array(shas, dtype="S40")
            self.n = need

    def search(self, q: np.ndarray, k: int) -> list[tuple[str, float]]:
        """The k nearest stored vectors to the unit vector q: (chunk hash, cosine), best first."""
        with self.lock:
            n, mat, shas = self.n, self.mat, self.shas
        if n == 0 or k <= 0:
            return []
        qf = np.asarray(q, dtype=np.float32)
        scores = np.empty(n, dtype=np.float32)
        for i in range(0, n, BLOCK):
            scores[i:i + BLOCK] = mat[i:min(i + BLOCK, n)].astype(np.float32) @ qf
        k = min(k, n)
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [(shas[i].decode(), float(scores[i])) for i in top]

    def bytes(self) -> int:
        return int(self.mat.nbytes + self.shas.nbytes)


def rss_kb() -> int:
    """This process's resident size from /proc, in kB (0 where /proc is not there)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


class Server:
    """The meaning-search endpoint of yerk-index, local only. POST /search {"query", "pool"} answers
    the nearest chunks (rows as the keyword search returns them, plus the cosine as `score`), or
    `ok: false` with a status told apart by kind: no-key, provider-timeout, provider-error, empty.
    GET /status answers the counts. Each request runs on its own thread with its own read-only
    connection, so a scan never waits on the writer."""

    def __init__(self, store: Vectors, embedder: Embedder, db: Path, live: Any):
        self.store, self.embedder, self.db, self.live = store, embedder, db, live
        self.port = 0
        self.httpd: ThreadingHTTPServer | None = None
        self.queries = 0

    def search(self, query: str, pool: int) -> dict[str, Any]:
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
        top = self.store.search(q, max(1, min(int(pool), 400)))
        if not top:
            return {"ok": True, "status": "ok", "hits": [], "vectors": self.store.n}
        score = dict(top)
        marks = ",".join("?" * len(score))
        conn = sqlite3.connect(f"file:{self.live(self.db)}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(f"SELECT id, path, lo, hi, body, sha FROM chunks WHERE sha IN ({marks})",
                                list(score)).fetchall()
        finally:
            conn.close()
        hits = [{"id": cid, "path": p, "line": lo, "lines": f"{lo}-{hi}", "text": body, "score": round(score[sha], 4)}
                for cid, p, lo, hi, body, sha in rows]
        hits.sort(key=lambda h: -h["score"])
        return {"ok": True, "status": "ok", "hits": hits, "vectors": self.store.n}

    def status(self) -> dict[str, Any]:
        return {"vectors": self.store.n, "model": self.embedder.model, "dim": self.embedder.dim,
                "key": self.embedder.available(), "queries": self.queries, "requests": self.embedder.requests,
                "tokens": self.embedder.tokens, "rss_kb": rss_kb(), "store_bytes": self.store.bytes()}

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
                if self.path != "/search":
                    self._send({"ok": False, "status": "failure", "error": "no such path"}, 404)
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(n) or b"{}")
                    out = srv.search(str(payload.get("query", "")), int(payload.get("pool", 80)))
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


class Backfill:
    """The embedding thread of yerk-index: chunks without a vector, newest first, in batches, with a
    gap between requests so the provider and the box are never hammered; resumable, since every
    batch is committed as it lands and a restart asks the index what is still missing. Shared with
    the writer through `pending` (hashes of chunks just inserted) and `dirty` (scan again)."""

    def __init__(self, db: Path, live: Any, store: Vectors, embedder: Embedder, batch: int = BATCH, gap: float = GAP):
        self.db, self.live, self.store, self.embedder, self.batch, self.gap = db, live, store, embedder, batch, gap
        self.pending: list[str] = []
        self.lock = threading.Lock()
        self.dirty = threading.Event()
        self.dirty.set()
        self.stop = threading.Event()
        self.paused_until = 0.0
        self.done = self.failed = 0
        self.last_error = ""
        self.started = 0.0
        self.thread: threading.Thread | None = None
        self.conn: sqlite3.Connection | None = None
        self.real: Path | None = None

    def note(self, shas: list[str]) -> None:
        """The writer's new chunk hashes: embedded next, before any scan."""
        with self.lock:
            self.pending.extend(shas)

    def _conn(self) -> sqlite3.Connection:
        """This thread's own writer connection; after a rebuild swapped the link it moves to the new
        generation and loads the vectors the rebuild copied there."""
        real = self.live(self.db)
        if self.conn is None or real != self.real:
            if self.conn is not None:
                self.conn.close()
            self.conn = sqlite3.connect(real, timeout=5.0, isolation_level=None)
            if self.real is not None:
                log.info("the index was rebuilt: %d vectors loaded from %s", self.store.load(self.conn, self.embedder.model),
                         real.name)
                self.dirty.set()
            self.real = real
        return self.conn

    def missing(self, conn: sqlite3.Connection, limit: int) -> list[str]:
        return [r[0] for r in conn.execute(
            "SELECT c.sha FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM vectors v WHERE v.sha = c.sha AND v.model = ? "
            "AND v.dim = ?) GROUP BY c.sha ORDER BY max(c.id) DESC LIMIT ?", (self.embedder.model, self.embedder.dim, limit)).fetchall()]

    def take(self, conn: sqlite3.Connection) -> list[str]:
        with self.lock:
            batch, self.pending = self.pending[:self.batch], self.pending[self.batch:]
        if not batch and self.dirty.is_set():
            batch = self.missing(conn, self.batch)
            if not batch:
                self.dirty.clear()
        return list(dict.fromkeys(batch))

    def step(self) -> int:
        """One batch: its texts from the index, one request, one transaction. Chunks already embedded
        (a hash queued twice) are skipped. Returns the number embedded."""
        conn = self._conn()
        shas = self.take(conn)
        if not shas:
            return 0
        marks = ",".join("?" * len(shas))
        have = {r[0] for r in conn.execute(f"SELECT sha FROM vectors WHERE sha IN ({marks}) AND model = ? AND dim = ?",
                                           (*shas, self.embedder.model, self.embedder.dim))}
        rows = conn.execute(f"SELECT sha, min(body) FROM chunks WHERE sha IN ({marks}) GROUP BY sha", shas).fetchall()
        rows = [(s, b) for s, b in rows if s not in have]
        if not rows:
            return 0
        vecs = self.embedder.embed([b for _, b in rows])
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("INSERT OR REPLACE INTO vectors(sha, model, dim, vec) VALUES (?, ?, ?, ?)",
                             [(s, self.embedder.model, self.embedder.dim, to_blob(v)) for (s, _), v in zip(rows, vecs)])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        self.store.add([s for s, _ in rows], vecs)
        self.done += len(rows)
        return len(rows)

    def run(self) -> None:
        self.started = time.time()
        while not self.stop.is_set():
            if not self.embedder.available() or time.monotonic() < self.paused_until:
                self.stop.wait(2.0)
                continue
            try:
                n = self.step()
            except ProviderError as e:
                self.failed += 1
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
        return {"done": self.done, "failed": self.failed, "pending": pending, "scanning": self.dirty.is_set(),
                "paused": time.monotonic() < self.paused_until, "last_error": self.last_error}


def write_status(path: Path, **fields: Any) -> None:
    """<state>/memory/index.json, 0600: what yerk-index is serving (pid, port, the embeddings' state)."""
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "at": round(time.time(), 3), **fields}, f)
    os.replace(tmp, path)
