"""Reads and writes the memory folder: the day log every message is written to, exact reads of its files, recall of earlier notes, and recall's test bench.

Files are the truth (DESIGN 5). The tree lives in the private layer: `[memory] root` in yerkd.toml
(default `memory/` beside it) holds `daily/YYYY-MM-DD.md`, where every message of every room is
written as it lands (L55), and the fact files, the curated file and open loops. `[memory] also` names
roots the index reads but nothing here writes. The index itself is yerk-index (memindex.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any
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

    @property
    def daily(self) -> Path:
        return self.root / "daily"


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
                    bench=(private / Path(str(conf.get("bench", "evals/recall-questions.json")))).resolve())


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


def recall(s: Settings, query: str, k: int = 6, deadline_s: float = 5.0) -> dict[str, Any]:
    """The recall tool: ranked chunks with path and line. A timeout and a failure read differently (L54)."""
    import sqlite3
    import time
    k = max(1, min(int(k), 20))
    try:
        conn = memindex.connect(s.index_db, readonly=True, deadline=time.monotonic() + deadline_s)
        try:
            hits = memindex.search(conn, query, k)
        finally:
            conn.close()
    except FileNotFoundError as e:
        return {"ok": False, "status": "failure", "error": str(e)}
    except sqlite3.Error as e:
        if memindex.is_timeout(e):
            return {"ok": False, "status": "timeout", "error": f"the index was busy for {deadline_s:.0f} s; "
                    "memory is not down, try once more"}
        return {"ok": False, "status": "failure", "error": f"the index could not be read: {e}"}
    for h in hits:
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
