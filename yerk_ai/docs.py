"""`yerk docs`: keeps local copies of the Claude documentation pages Yerk AI relies on, as files every room can search.
`refresh` fetches each page of [docs] pages in yerkd.toml (config.DOCS_PAGES when unset: the prompting guide, the prompting
pages of the models in models.toml, the Claude Code pages a room's harness leans on) into <state>/docs/claude/<slug>.md, headed
fetch time and content hash, class shared. memory.settings adds <state>/docs to every install's memory roots, so recall
finds a page in every room and cites it as docs/claude/<slug>.md:line. The sites answer `Accept: text/markdown` with the
page as Markdown (checked 2026-10-21 with a plain GET); anything else is refused and the page's old file kept.
`yerk doctor` refreshes them once [docs] refresh_days (7) have passed since the last clean refresh, and a page whose hash
moved is a line in the next morning diff, since a changed guide means the rules may be stale against it."""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, words
from .config import DOCS_PAGES, load_toml, write_private

REFRESH_DAYS, MAX_BYTES, TIMEOUT, STAMP = 7, 4_000_000, 30, "refresh.json"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
HEAD = re.compile(r"\A---\nclass: shared\nsource: (\S+)\nfetched: (\S+)\nsha256: ([0-9a-f]{64})\n---\n")


def root(state: Path) -> Path:
    """The memory root: <state>/docs, so a page cites as docs/claude/<slug>.md and a room's shape report as docs/shape/."""
    return state / "docs"


def settings(private: Path) -> dict[str, Any]:
    f = private / "yerkd.toml"
    return dict(load_toml(f).get("docs", {})) if f.is_file() else {}


def pages(conf: dict[str, Any]) -> dict[str, str]:
    """slug -> URL: [docs] pages, which replaces DOCS_PAGES whole, as [models] replaces models.toml."""
    return {str(k): str(v) for k, v in (conf.get("pages") or DOCS_PAGES).items()}


def fetch(url: str) -> tuple[str, bytes]:
    req = urllib.request.Request(url, headers={"Accept": "text/markdown", "User-Agent": f"yerk-docs/{__version__}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return str(r.headers.get("Content-Type") or ""), r.read(MAX_BYTES + 1)


def kept(path: Path) -> dict[str, str]:
    """A kept page's head (source, fetched, sha256); {} for no file or another shape."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            m = HEAD.match(f.read(600))
    except OSError:
        return {}
    return {"source": m[1], "fetched": m[2], "sha256": m[3]} if m else {}


def load(state: Path) -> dict[str, Any]:
    try:
        return dict(json.loads((root(state) / STAMP).read_text()))
    except (OSError, ValueError, TypeError):
        return {}


def due(state: Path, conf: dict[str, Any], now: float) -> bool:
    days = conf.get("refresh_days", REFRESH_DAYS)
    days = days if isinstance(days, (int, float)) and not isinstance(days, bool) and days > 0 else REFRESH_DAYS
    return now - float(load(state).get("at", 0)) >= days * 86400


def refresh(state: Path, conf: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Every page fetched: one whose bytes moved is rewritten, a failure keeps its old file and leaves the refresh due, so
    the next doctor run tries again. Returns the slugs new, changed and the same, and each failure's why."""
    now = time.time() if now is None else now
    (folder := root(state) / "claude").mkdir(mode=0o700, parents=True, exist_ok=True)
    last = load(state)
    seen: dict[str, Any] = dict(last.get("pages", {}))
    out: dict[str, Any] = {"new": [], "changed": [], "same": [], "failed": {}}
    for slug, url in pages(conf).items():
        try:
            if not SLUG.fullmatch(slug):
                raise ValueError("a slug is lowercase letters, digits and hyphens")
            ctype, body = fetch(url)
            if "markdown" not in ctype and not ctype.startswith("text/plain"):
                raise ValueError(f"not Markdown ({ctype.split(';')[0] or 'no type'})")
            if len(body) > MAX_BYTES:
                raise ValueError(f"over {MAX_BYTES:,} bytes")
            text = body.decode("utf-8")
        except (OSError, ValueError, http.client.HTTPException) as e:     # URLError, HTTPError, a timeout; a bad decode; a cut read
            out["failed"][slug] = f"{type(e).__name__}: {words.no_paths(str(e))[:120]}"
            continue
        sha, path = hashlib.sha256(body).hexdigest(), folder / f"{slug}.md"
        old = kept(path).get("sha256")
        if old != sha:
            when = datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds")
            write_private(path, f"---\nclass: shared\nsource: {url}\nfetched: {when}\nsha256: {sha}\n---\n{text}")
        kind = "same" if old == sha else "changed" if old else "new"
        out[kind].append(slug)
        seen[slug] = {"url": url, "sha256": sha, "changed": now if kind == "changed" else seen.get(slug, {}).get("changed", 0)}
    at = last.get("at", 0) if out["failed"] else now
    write_private(root(state) / STAMP, json.dumps({"at": at, "tried": now, "pages": seen, "failed": out["failed"]}))
    return out


def failed(r: dict[str, Any]) -> list[str]:
    """A refresh's failures as one doctor line."""
    if not (f := r.get("failed")):
        return []
    slug, why = next(iter(f.items()))
    return [f"docs: {len(f)} page{'s' * (len(f) > 1)} not fetched, first {slug}: {why}"]


def changed(state: Path, since: float) -> list[tuple[str, str]]:
    """The morning diff's lines: each page whose hash moved after `since` (the last delivered diff), new once by its hash."""
    return [(f"docs {slug} {p.get('sha256', '')[:12]}", words.line(f"{words.WORKING} " + words.DOCS_CHANGED.format(slug=slug)))
            for slug, p in sorted(load(state).get("pages", {}).items()) if float(p.get("changed") or 0) > since]


def listing(state: Path, conf: dict[str, Any]) -> list[str]:
    """`yerk docs list`: a line per page, what is kept of it and how long ago it was fetched (STYLE.md 5: no raw timestamp)."""
    out, failures, now = [], load(state).get("failed", {}), time.time()
    for slug, url in pages(conf).items():
        path = root(state) / "claude" / f"{slug}.md"
        k = kept(path)
        said = f"fetched {words.ago(now - path.stat().st_mtime)}, {words.size(path.stat().st_size)}, sha256 {k['sha256'][:12]}" if k else "not fetched yet"
        out.append(f"{slug}: {said}" + (f"; last try failed, {failures[slug]}" if slug in failures else "") + f" ({url})")
    return out
