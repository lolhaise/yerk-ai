"""Reads and writes the memory folder: the day log every message is written to, exact reads of its files, recall of earlier notes, and recall's test bench.

Files are the truth (DESIGN 5). The tree lives in the private layer: `[memory] root` in yerkd.toml
(default `memory/` beside it) holds `daily/YYYY-MM-DD.md`, where every message of every room is
written as it lands (L55), and the fact files, the curated file and open loops. `[memory] also` names
roots the index reads but nothing here writes. The index itself is yerk-index (memindex.py).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import memindex
from .config import REPO, load_toml

MARK = re.compile(r"\[m:([^\]\s]+)\]$", re.M)        # the message id that ends each captured line
READ_MAX = 400                                       # read_memory: lines per call
READ_CHARS = 20_000


@dataclass(frozen=True)
class Settings:
    root: Path                  # the install's own tree; capture writes root/daily
    scope: memindex.Scope       # what the index covers: root and the `also` roots
    index_db: Path
    tz: tzinfo
    interval: float
    bench: Path
    mentions: Path = Path("mentions.json")          # the mention index yerk-index writes
    curated: tuple[str, ...] = ("MEMORY.md",)        # file names whose headings are mention keys
    mentions_exclude: tuple[str, ...] = ()          # globs kept out of mention recall
    embeddings_key: Path = Path("secrets/openai.key")   # the provider key, a path in the private layer, never read here
    embeddings_model: str = "text-embedding-3-small"
    embeddings_dim: int = 256
    embeddings_units: str = "both"                  # what gets a vector in memory: both (lines and chunks, measured best), lines, or chunks
    embeddings_width: int = 256                     # dimensions kept in memory (the first `width` of `dim`, renormalised)
    index_status: Path = Path("index.json")         # what yerk-index serves: its port and the embeddings' state

    @property
    def daily(self) -> Path:
        return self.root / "daily"

    @property
    def roots_for_mentions(self) -> tuple[Path, ...]:
        return self.scope.roots


def settings(private: Path, state: Path, conf: dict[str, Any] | None = None) -> Settings:
    if conf is None:
        f = private / "yerkd.toml"
        conf = dict(load_toml(f).get("memory", {})) if f.is_file() else {}
    also = [(private / Path(str(x)).expanduser()).resolve() for x in conf.get("also", [])]
    if conf.get("root"):
        root = (private / Path(str(conf["root"])).expanduser()).resolve()
    elif private.resolve().is_relative_to(REPO):
        # the fake example layer shipped in the repo: capture never writes into the repo; the
        # example's own memory folder is indexed read-only
        root = (state / "memory" / "tree").resolve()
        also = also or [(private / "memory").resolve()]
    else:
        root = (private / "memory").resolve()
    tz: tzinfo = ZoneInfo(str(conf["timezone"])) if conf.get("timezone") else (datetime.now().astimezone().tzinfo
                                                                              or timezone.utc)
    return Settings(root=root, scope=memindex.Scope(tuple([root, *also]), tuple(conf.get("exclude", []))),
                    index_db=state / "memory" / "index.sqlite", tz=tz, interval=float(conf.get("interval", 2.0)),
                    bench=(private / Path(str(conf.get("bench", "evals/recall-questions.json")))).resolve(),
                    mentions=state / "memory" / "mentions.json",
                    curated=tuple(str(x) for x in conf.get("curated", ["MEMORY.md"])),
                    mentions_exclude=tuple(str(x) for x in conf.get("mentions_exclude", [])),
                    embeddings_key=(private / Path(str(conf.get("embeddings_key", "secrets/openai.key"))).expanduser()),
                    embeddings_model=str(conf.get("embeddings_model", "text-embedding-3-small")),
                    embeddings_dim=int(conf.get("embeddings_dim", 256)),
                    embeddings_units=str(conf.get("embeddings_units", "both")),
                    embeddings_width=int(conf.get("embeddings_width") or conf.get("embeddings_dim") or 256),
                    index_status=state / "memory" / "index.json")


# the embedding settings a user with another model or budget can pick, described from the code (run 5):
# key: (choices, what, the default)
EMBEDDING_SETTINGS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "embeddings_model": (("text-embedding-3-small", "text-embedding-3-large"),
                         "the provider model every line, chunk and query is embedded with; a change re-embeds the tree once "
                         "(each model's vectors are kept apart in the index, so switching back pays nothing); small costs "
                         "$0.02 and large $0.13 per 1M tokens (OpenAI, read 2026-10-18): a 3,800-file tree's lines "
                         "(10.4M tokens) $0.21 or $1.35, its chunks (16.7M) $0.33 or $2.17; measured on the 164-question "
                         "set at 256 kept, large brings "
                         "paraphrase golds closer but loses on the top six (110 against 117), so small is the default",
                         "text-embedding-3-small"),
    "embeddings_dim": (("256", "512", "1024", "1536", "3072"),
                       "dimensions asked of the provider and stored in the index (2 bytes each a vector): at most 1536 "
                       "for text-embedding-3-small and 3072 for text-embedding-3-large (OpenAI's embeddings guide, read "
                       "2026-10-18); a change re-embeds the tree once; more dimensions buy a finer vector, not a cheaper one",
                       "256"),
    "embeddings_width": (("256", "384", "512", "the dim"),
                         "dimensions kept in memory: the first width of dim, renormalised (the provider's own shortening "
                         "rule), so a narrower store needs no re-embedding; the index process holds about vectors times "
                         "(2 times width plus 20) bytes; 192 and 128 were measured and lost", "the dim"),
    "embeddings_units": (("both", "lines", "chunks"),
                         "what gets a vector: every line and bullet and every chunk (both, measured best), the lines alone "
                         "(a smaller store), or the chunks alone (the fewest vectors, a paraphrase then rarely found)", "both"),
}
EMBEDDING_RELOADS = "takes effect when yerk-index restarts; the backfill then embeds what is missing, in the background"


def described(conf: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The [memory] embedding settings with their value here, where it came from, choices and plain words
    (the shape `yerk config` prints for every described setting)."""
    conf = conf or {}
    out = []
    for key, (choices, what, default) in EMBEDDING_SETTINGS.items():
        value = conf.get(key, default)
        out.append({"key": f"memory.{key}", "value": str(value),
                    "default": default, "source": "yerkd.toml" if key in conf else "default", "choices": list(choices),
                    "what": what, "reloads": EMBEDDING_RELOADS})
    return out


def one_line(text: str) -> str:
    return " / ".join(x.strip() for x in text.splitlines() if x.strip())


class DailyLog:
    """The day's log: one line per message, written as it lands, short and attachment-only ones
    included, each attachment on a line of its own under it. The line ends with the message id, so
    a message captured twice (a replay, a restart, a rerun) is written once. The day and the time
    are the message's own, in the install's time zone."""

    def __init__(self, daily: Path, tz: tzinfo):
        self.daily, self.tz = daily, tz
        self.seen: dict[str, set[str]] = {}
        self.lock = threading.Lock()

    def _ids(self, day: str) -> set[str]:
        if day not in self.seen:
            if len(self.seen) > 3:
                self.seen.pop(min(self.seen))
            f = self.daily / f"{day}.md"
            self.seen[day] = set(MARK.findall(f.read_text(errors="replace"))) if f.is_file() else set()
        return self.seen[day]

    def capture(self, room: str, mid: str, role: str, text: str, files: list[tuple[str, str, int]] | None = None,
                when: datetime | None = None, edited: bool = False) -> bool:
        """One message into the log; False when its line is already there."""
        files = files or []
        at = (when or datetime.now(timezone.utc)).astimezone(self.tz)
        day = at.strftime("%Y-%m-%d")
        body = one_line(text) or ("(attachment only)" if files else "(no text)")
        key = f"{mid}.e{hashlib.sha1(body.encode()).hexdigest()[:8]}" if edited else mid
        lines = [f"- {at:%H:%M:%S} {room} {role}: {'(edited) ' if edited else ''}{body} [m:{key}]"]
        lines += [f"  - attachment: {name}, {kind}, {size:,} bytes" for name, kind, size in files]
        with self.lock:
            ids = self._ids(day)
            if key in ids:
                return False
            self.daily.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.daily / f"{day}.md"
            head = "" if path.exists() else f"# {day}\n\n"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(head + "\n".join(lines) + "\n")
            ids.add(key)
        return True


def read(s: Settings, path: str, start: int = 1, lines: int = 40) -> dict[str, Any]:
    """An exact excerpt of a file in the memory tree. A relative path is taken from the tree's root;
    anything that resolves outside the scope (a `..`, an absolute path elsewhere, a link out) is refused."""
    p = Path(path).expanduser()
    p = p if p.is_absolute() else s.root / p
    try:
        real = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"ok": False, "error": f"no such file in the memory tree: {path}"}
    if not s.scope.contains(real):
        return {"ok": False, "error": f"{path} is outside the memory tree; read_memory reads only the private memory tree"}
    if not real.is_file():
        return {"ok": False, "error": f"{path} is a folder, not a file"}
    start, lines = max(1, int(start)), max(1, min(int(lines), READ_MAX))
    out: list[str] = []
    total = 0
    with real.open(encoding="utf-8", errors="replace") as f:
        for total, line in enumerate(f, 1):
            if start <= total < start + lines:
                out.append(line.rstrip("\n"))
    text = "\n".join(out)
    cut = len(text) > READ_CHARS
    return {"ok": True, "path": str(real), "from": start, "to": start + len(out) - 1, "total_lines": total,
            "text": text[:READ_CHARS] + ("\n[cut at 20,000 characters; read fewer lines]" if cut else "")}


POOL = 80            # candidates from each search (keywords, meaning) before fusion
RRF_K = 20           # reciprocal rank fusion's constant: 1 / (RRF_K + rank); 60 before run 4, 20 measured on the 164-question set
PRIOR = 0.0          # the source prior: a fact or topic file's fused score times (1 + PRIOR); measured 0.15 to 0.5 on the 164-question set and every value lost, so it stays off
KEEP = 1             # each search's own first KEEP hits stay in the fused top k (measured: 1; 0 was plain fusion)
MEANING_WAIT = 5.0   # seconds a recall waits for yerk-index's meaning search (its own provider wait is shorter)
DAYLOG = re.compile(r"(^|/)\d{4}-\d{2}-\d{2}\.md$")    # a day log is named by its day
log = logging.getLogger("yerk.memory")


def is_daylog(path: str) -> bool:
    return bool(DAYLOG.search(str(path)))


def index_status(s: Settings) -> dict[str, Any]:
    """What yerk-index wrote about itself (index.json), or {} when nothing is serving."""
    try:
        d = json.loads(s.index_status.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def meaning_state(s: Settings) -> str:
    """ok: vectors are served; no-key: yerk-index runs without a provider key; empty: a key but no vector
    yet; no-index: nothing is serving (no status file, or the process behind it is gone)."""
    st = index_status(s)
    emb = st.get("embeddings") or {}
    if not st or not st.get("port"):
        return "no-index"
    try:
        os.kill(int(st.get("pid", 0)), 0)
    except (OSError, ValueError):
        return "no-index"
    if not emb.get("key"):
        return "no-key"
    return "ok" if emb.get("vectors") else "empty"


def meaning(s: Settings, query: str, pool: int = POOL, deadline_s: float = MEANING_WAIT) -> tuple[str, list[dict[str, Any]]]:
    """(status, rows) from yerk-index's meaning search. Every status short of ok means keywords only for
    this query and says why: no-index, no-key, empty, provider-timeout, provider-error, index-timeout
    (yerk-index itself answered late), failure (its answer was broken)."""
    import urllib.request
    state = meaning_state(s)
    if state != "ok":
        return state, []
    port = int(index_status(s)["port"])
    req = urllib.request.Request(f"http://127.0.0.1:{port}/search", data=json.dumps({"query": query, "pool": pool}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=deadline_s) as r:
            out = json.loads(r.read())
    except TimeoutError:
        return "index-timeout", []
    except OSError as e:
        return ("index-timeout" if "timed out" in str(e).lower() else "no-index"), []
    except ValueError:
        return "failure", []
    if not isinstance(out, dict) or not out.get("ok"):
        return str((out or {}).get("status", "failure")) if isinstance(out, dict) else "failure", []
    return "ok", list(out.get("hits", []))


def fuse(keywords: list[dict[str, Any]], vectors: list[dict[str, Any]], k: int,
         rrf_k: int = RRF_K, w_keywords: float = 1.0, w_vectors: float = 1.0,
         prior: float | None = None, keep: int | None = None) -> list[dict[str, Any]]:
    """Reciprocal rank fusion of the two ranked lists: each chunk scores the sum over the lists it is in
    of w / (rrf_k + its rank there); a tie breaks on the cosine. No calibration between a bm25 cover
    and a cosine is needed, and a chunk both searches put first outranks either search's lone top hit.
    Vector hits under half the best cosine are noise and leave the fusion first. Two measured levers:
    `prior` multiplies a fact or topic file's score by (1 + prior), a boost, never a rule, so a day log
    that ranks well ahead still comes first; `keep` holds each search's own first `keep` hits in the
    top k (in fused order), so a lone keyword hit at rank two is not pushed out by chunks both searches
    place low."""
    prior = PRIOR if prior is None else prior
    keep = KEEP if keep is None else keep
    if not vectors:
        return keywords[:k]
    top = float(vectors[0]["score"])
    vectors = [h for h in vectors if float(h["score"]) >= top / 2] if top > 0 else []
    if not vectors:
        return keywords[:k]
    score: dict[int, float] = {}
    rows: dict[int, dict[str, Any]] = {}
    for w, ranked in ((w_keywords, keywords), (w_vectors, vectors)):
        for rank, h in enumerate(ranked, 1):
            score[h["id"]] = score.get(h["id"], 0.0) + w / (rrf_k + rank)
            rows.setdefault(h["id"], h)
    if prior:
        for cid in score:
            if not is_daylog(rows[cid]["path"]):
                score[cid] *= 1 + prior
    cos = {h["id"]: float(h["score"]) for h in vectors}
    order = sorted(score, key=lambda i: (-score[i], -cos.get(i, 0.0)))
    held = list(dict.fromkeys([h["id"] for h in keywords[:keep]] + [h["id"] for h in vectors[:keep]]))[:k]
    chosen = set(held) | set([cid for cid in order if cid not in held][:k - len(held)])
    out = []
    for cid in [cid for cid in order if cid in chosen]:
        h = dict(rows[cid])
        h["score"] = round(score[cid], 4)
        out.append(h)
    return out


def recall(s: Settings, query: str, k: int = 6, deadline_s: float = 5.0) -> dict[str, Any]:
    """The recall tool: ranked chunks with path and line, the keyword rank fused with the meaning rank
    when yerk-index serves one. A timeout and a failure of the index read differently (L54); a
    provider timeout or error reads differently from both: keywords only for this query, logged."""
    import sqlite3
    import time
    k = max(1, min(int(k), 20))
    try:
        conn = memindex.connect(s.index_db, readonly=True, deadline=time.monotonic() + deadline_s)
        try:
            # twice the pool by bm25, re-ranked by cover down to the pool: the cover re-rank needs
            # the candidates bm25 alone ranks low (measured on the fixed question set: 2 more top-six hits)
            keywords = memindex.search(conn, query, POOL, POOL * 2)
        finally:
            conn.close()
    except FileNotFoundError as e:
        return {"ok": False, "status": "failure", "error": str(e)}
    except sqlite3.Error as e:
        if memindex.is_timeout(e):
            return {"ok": False, "status": "timeout", "error": f"the index was busy for {deadline_s:.0f} s; "
                    "memory is not down, try once more"}
        return {"ok": False, "status": "failure", "error": f"the index could not be read: {e}"}
    state, vectors = meaning(s, query)
    if state in ("provider-timeout", "provider-error", "index-timeout", "failure"):
        log.warning("recall: meaning search skipped (%s); keywords only for this query", state)
    hits = fuse(keywords, vectors, k)
    for h in hits:
        h.pop("id", None)
        h["cite"] = f"{cite(s, Path(h['path']))}:{h['line']}"
    return {"ok": True, "status": "ok", "hits": hits}


def cite(s: Settings, path: Path) -> str:
    """The short name a room cites a file by: its root's name and the path under it."""
    for root in s.scope.roots:
        if path == root:
            return root.name
        if path.is_relative_to(root):
            return (Path(root.name) / path.relative_to(root)).as_posix()
    return path.name


# the room's own log: the topic hit per message and the recall pack at session start (decision 20)
LINE = re.compile(r"^- (\d\d:\d\d:\d\d) (\S+) ([\w-]+): (.*?)(?: \[m:([^\]\s]+)\])?$")
OWNER_ROLES = frozenset({"owner", "test-webhook"})   # the test webhook stands in for the owner in a test room
DECIDED = re.compile(r"\b(?:decided|decision|agreed|settled on|going with|go with|draft(?:ed)?)\b", re.I)
WINDOW_DAYS = 14
ITEM_CHARS = 280


def window(s: Settings, now: datetime | None = None, days: int = WINDOW_DAYS) -> list[str]:
    """The last `days` day names in the install's time zone, newest first."""
    from datetime import timedelta
    today = (now or datetime.now(timezone.utc)).astimezone(s.tz).date()
    return [(today - timedelta(days=n)).isoformat() for n in range(days)]


def _short(text: str) -> str:
    return text if len(text) <= ITEM_CHARS else text[:ITEM_CHARS - 1] + "…"


def _stem(w: str) -> str:
    return w[:5] if len(w) >= 5 else w


def room_lines(s: Settings, room: str, now: datetime | None = None) -> Iterator[tuple[str, int, re.Match[str], list[str]]]:
    """(day, line number, match, the lines of its file) for this room's lines of the last 14 days,
    newest day first. The room's own day files are small, so they are read straight from disk:
    fresher than the index (which trails by a pass) and a few milliseconds per message."""
    for day in window(s, now):
        f = s.daily / f"{day}.md"
        if not f.is_file():
            continue
        lines = f.read_text(errors="replace").splitlines()
        for n, line in enumerate(lines):
            m = LINE.match(line)
            if m and m.group(2) == room:
                yield day, n, m, lines


def topic_hits(s: Settings, room: str, text: str, exclude: set[str] | None = None,
               now: datetime | None = None, limit: int = 6) -> list[dict[str, Any]]:
    """The message's key terms against this room's own lines of the last 14 days, best first:
    ranked by how many of the terms a line holds, newer first on a tie. The message itself
    (filed before the turn) and lines sharing too few terms never come back."""
    ts = memindex.terms(text)
    if not ts:
        return []
    want = {_stem(t) for t in ts}
    need = 1 if len(want) <= 2 else max(2, -(-len(want) * 3 // 10))     # a long message needs 30% of its terms
    body = one_line(text)
    hits = []
    for day, n, m, _ in room_lines(s, room, now):
        if (m.group(5) or "").split(".e")[0] in (exclude or set()) or m.group(4).strip() == body:
            continue
        cover = len(want & {_stem(w) for w in memindex.WORD.findall(memindex.norm(m.group(4)))})
        if cover >= need:
            hits.append({"day": day, "time": m.group(1)[:5], "role": m.group(3), "text": m.group(4),
                         "cite": f"daily/{day}.md:{n + 1}", "cover": cover})
    return sorted(hits, key=lambda h: (h["cover"], h["day"], h["time"]), reverse=True)[:limit]


def room_items(s: Settings, room: str, now: datetime | None = None,
               exclude: set[str] | None = None) -> list[dict[str, Any]]:
    """The room's filed items of the last 14 days, newest first: every message the owner sent
    (its attachments with it) and every line that reads as a decision or a draft. The messages
    the turn is answering (`exclude`) are the prompt itself and stay out."""
    items: list[dict[str, Any]] = []
    for day in window(s, now):
        f = s.daily / f"{day}.md"
        if not f.is_file():
            continue
        lines = f.read_text(errors="replace").splitlines()
        found: list[dict[str, Any]] = []
        for n, line in enumerate(lines):
            m = LINE.match(line)
            if not m or m.group(2) != room or (m.group(5) or "").split(".e")[0] in (exclude or set()):
                continue
            owner = m.group(3) in OWNER_ROLES
            if not owner and not DECIDED.search(m.group(4)):
                continue
            files = []
            for nxt in lines[n + 1:]:
                if not nxt.startswith("  - attachment: "):
                    break
                files.append(nxt.strip()[2:])
            found.append({"day": day, "time": m.group(1)[:5], "role": m.group(3), "text": m.group(4),
                          "files": files, "cite": f"daily/{day}.md:{n + 1}"})
        items += reversed(found)
    return items


def room_pack(s: Settings, room: str, budget: int = 6000, now: datetime | None = None,
              exclude: set[str] | None = None) -> tuple[str, int]:
    """(text, items shown): the recall pack's room part, newest first, cut to `budget` characters."""
    head = (f"[recall pack] Room {room}, last {WINDOW_DAYS} days of filed items, newest first: what the owner "
            "sent, decisions and drafts. Each ends with its file and line; read_memory gives the whole line.")
    out, used, shown = [head], len(head), 0
    items = room_items(s, room, now, exclude)
    for it in items:
        extra = "".join(f" [{f}]" for f in it["files"])
        line = f"- {it['day'][5:]} {it['time']} {it['role']}: {_short(it['text'])}{extra} ({it['cite']})"
        if used + 1 + len(line) > budget - 60:
            break
        out.append(line)
        used += 1 + len(line)
        shown += 1
    if shown < len(items):
        out.append(f"({len(items) - shown} older items not shown; recall or read_memory reaches them)")
    return ("\n".join(out), shown) if items else ("", 0)


def on_file(s: Settings, deadline_s: float = 1.5) -> tuple[Any, Any]:
    """(figure lookup, file lookup) for the shadow Stop check, each answering the cite or None."""
    import sqlite3
    import time

    from .evidence import has_figure

    def figure(fig: str) -> str | None:
        digits = re.findall(r"\d+", fig)
        try:
            conn = memindex.connect(s.index_db, readonly=True, deadline=time.monotonic() + deadline_s)
            try:
                rows = conn.execute("SELECT c.path, c.lo, c.body FROM chunks_fts JOIN chunks c ON c.id = "
                                    "chunks_fts.rowid WHERE chunks_fts MATCH ? LIMIT 60",
                                    ('"' + " ".join(digits) + '"',)).fetchall()
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return None
        found = []                  # a fact file first, then the install's own tree, then a read-only root
        for path, lo, body in rows:
            for n, line in enumerate(body.splitlines()):
                if has_figure(line, fig):
                    p = Path(path)
                    rank = 0 if p.parent.name == "facts" else 1 if p.is_relative_to(s.root) else 2
                    found.append((rank, str(p.relative_to(s.root)) if p.is_relative_to(s.root) else path, lo + n))
                    break
        return f"{min(found)[1]}:{min(found)[2]}" if found else None

    def known(name: str) -> str | None:
        try:
            conn = memindex.connect(s.index_db, readonly=True, deadline=time.monotonic() + deadline_s)
            try:
                row = conn.execute("SELECT path FROM files WHERE path = ? OR path LIKE ? LIMIT 1",
                                   (name, "%/" + name.lstrip("./"))).fetchone()
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return None
        return cite(s, Path(row[0])) if row else None

    return figure, known


# the recall bench: a fixed question set, run against the index (L63)
def has_all(text: str, tokens: list[str]) -> bool:
    t = memindex.norm(text)
    return all(memindex.norm(tok) in t for tok in tokens)


def bench(s: Settings, questions: Path | None = None, k: int = 6) -> dict[str, Any]:
    """Each question's search; a hit is a result in the top k whose text holds every answer token
    and whose path is not a stale copy. Controls must hit, or the instrument is blind."""
    qfile = questions or s.bench
    data = json.loads(qfile.read_text())
    stale = re.compile(str(data.get("stale") or r"(^|/)overwrite-backups/"))
    rows = []
    for q in data["questions"]:
        r = recall(s, str(q["q"]), k, deadline_s=30.0)
        if not r["ok"]:
            return {"ok": False, "status": r["status"], "error": r["error"]}
        rank = next((i + 1 for i, h in enumerate(r["hits"])
                     if not stale.search(h["path"]) and has_all(h["text"], list(q["answer_tokens"]))), None)
        rows.append({"id": q["id"], "control": bool(q.get("control")), "rank": rank,
                     "top": r["hits"][0]["path"] if r["hits"] else None})
    scored = [r for r in rows if not r["control"]]
    n = len(scored) or 1
    top_k = sum(1 for r in scored if r["rank"])
    top_1 = sum(1 for r in scored if r["rank"] == 1)
    return {"ok": True, "questions": len(scored), "controls": len(rows) - len(scored),
            "blind": [r["id"] for r in rows if r["control"] and not r["rank"]],
            "top_k": top_k, "top_1": top_1, "k": k, "top_k_rate": round(top_k / n, 3), "top_1_rate": round(top_1 / n, 3),
            "rows": rows, "file": str(qfile)}
