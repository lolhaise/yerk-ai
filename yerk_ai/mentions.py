# The key rules (a bullet's leading name run, the possessive fold, sentence-like runs refused,
# entries carrying a do-not-surface mark dropped) follow the workspace recall-index tool this
# harness replaces.
"""Mention recall: when a message names one of your people, places or things, the matching lines of
front of the model before it starts, with no tool call.

yerk-index builds the index from the fact files (any `facts/*.md` under a memory root) and the
curated file's headings, again whenever one of those files changes, and writes it as one JSON file
beside the search index. The daemon loads it when its mtime moves and matches the words of each
message against its keys. Keys come from a fact file's name (car, dates, appliances), its headings
and the leading name of each bullet; an entry is the bullet or the start of its section, at most
ENTRY_CHARS, with the file and line it came from. An entry carrying a do-not-surface mark (⛔,
"ask-only") never enters, and the private yerkd.toml can keep whole files out
(`mentions_exclude`).
"""
from __future__ import annotations

import json
import re
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

ENTRY_CHARS = 300
KEY_MIN, KEY_MAX, KEY_WORDS = 3, 44, 6
MATCH_MAX = 6
DROP = re.compile(r"⛔|ask[- ]only", re.I)
OLD = "[superseded"                 # a corrected line (memory.remember): never an entry, its line read as blank
TOKEN = re.compile(r"[a-z0-9][a-z0-9'.\-]*")
BOLD = re.compile(r"\*\*(.+?)\*\*")
SPLIT = re.compile(r"\s+[—–-]\s+|\s*[(,:=;]|\s+→\s+")
HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
BULLET = re.compile(r"^[-*]\s+(.*\S)\s*$")
SENTENCEY = frozenset("is are was were be he she it they we i you my his her their our this that these "
                      "those has have had do does did will would should can could not no if when".split())
KEY_STOP = frozenset("the and people home house work money rule rules note notes decisions identity purchases "
                     "health family today things misc general other current status open loops daily facts log "
                     "summary overview history index todo plan plans memory pinned".split())


def tokens(s: str) -> list[str]:
    """Lowercase words, the possessive folded away, so "the kettle's lid" matches the key "kettle"."""
    words = (re.sub(r"'s$|'$", "", t).strip(".-") for t in TOKEN.findall(str(s or "").lower().replace("’", "'")))
    return [t for t in words if t]


def strip_md(s: str) -> str:
    s = re.sub(r"\*\*|__|`", "", s)
    return re.sub(r"^[^\w\"']+", "", s).strip()


def key_of(text: str) -> str | None:
    """A bullet's or heading's leading name run, or None when it reads as a sentence or is too long."""
    bold = BOLD.match(text.strip())
    raw = strip_md(bold.group(1) if bold else text)
    head = SPLIT.split(raw, 1)[0].strip().rstrip(".;:!?")
    words = head.split()
    if not KEY_MIN <= len(head) <= KEY_MAX or not words or len(words) > KEY_WORDS:
        return None
    if any(w.lower().strip(".,'") in SENTENCEY for w in words) or " ".join(tokens(head)) in KEY_STOP:
        return None
    return head if re.search(r"[A-Za-z]", head) else None


def entries_of(path: Path, cite: str, curated: bool, cls: str = "private", room: str = "") -> tuple[list[dict[str, Any]], int]:
    """(entries, dropped) for one file: headings always; for a fact file also its name and bullets; each with the file's class
    and room (memory.file_class), which the daemon keeps a room that is not an owner room to. The front matter is no entry."""
    lines = ["" if OLD in x else x for x in path.read_text(errors="replace").splitlines()]
    if lines[:1] == ["---"] and "---" in lines[1:]:
        lines = [""] * (end := lines.index("---", 1) + 1) + lines[end:]
    out: list[dict[str, Any]] = []
    dropped = 0

    def add(key: str | None, text: str, line: int) -> None:
        nonlocal dropped
        if not key:
            return
        if DROP.search(text) or DROP.search(key):
            dropped += 1
            return
        body = re.sub(r"\s+", " ", strip_md(text))
        out.append({"key": key, "text": body[:ENTRY_CHARS - 1] + "…" if len(body) > ENTRY_CHARS else body, "cite": f"{cite}:{line}",
                    "class": cls, "room": room})

    def section(i: int) -> str:
        """A heading's text and the lines under it up to the next heading, as one run."""
        run = [HEADING.match(lines[i]).group(2)]            # type: ignore[union-attr]
        for ln in lines[i + 1:]:
            if HEADING.match(ln):
                break
            if ln.strip():
                run.append(ln.strip())
            if sum(len(x) for x in run) > ENTRY_CHARS:
                break
        return " ".join(run)

    if not curated:                               # the file's own name, over its first unmarked lines
        body = [(i, ln.strip()) for i, ln in enumerate(lines) if ln.strip() and not HEADING.match(ln) and not DROP.search(ln)][:6]
        if body:
            add(key_of(path.stem.replace("-", " ").replace("_", " ")), " ".join(t for _, t in body), body[0][0] + 1)
    for i, ln in enumerate(lines):
        if (h := HEADING.match(ln)) and (curated or len(h.group(1)) >= 2):
            add(key_of(h.group(2)), section(i), i + 1)
        b = None if curated else BULLET.match(ln)
        if b:
            add(key_of(b.group(1)), b.group(1), i + 1)
    return out, dropped


def short_cite(path: Path, roots: tuple[Path, ...]) -> str:
    """The path read_memory takes: relative inside the install's own tree (the first root),
    absolute in a read-only root."""
    if roots and roots[0].is_dir() and path.is_relative_to(roots[0]):
        return path.relative_to(roots[0]).as_posix()
    return str(path)


class Builder:
    """yerk-index's half: rebuild the JSON when a source file's size or mtime moved."""

    def __init__(self, out: Path, roots: tuple[Path, ...], curated: tuple[str, ...], exclude: tuple[str, ...]):
        self.out, self.roots, self.curated, self.exclude = out, roots, curated, exclude
        self.sig: list[Any] | None = None

    def is_source(self, path: Path) -> bool:
        if not (path.name in self.curated or (path.parent.name == "facts" and path.suffix == ".md")):
            return False                                  # most paths: no stat, no cite (a pass asks of every one)
        rel = short_cite(path, self.roots)
        return not any(fnmatch(rel, p) or fnmatch(path.name, p) for p in self.exclude)

    def refresh(self, disk: dict[str, tuple[int, int]]) -> tuple[int, int, int] | None:
        from .config import write_private
        from .memory import file_class
        srcs = sorted((p, *st) for p, st in disk.items() if self.is_source(Path(p)))
        if srcs == self.sig and self.out.is_file():
            return None
        entries: list[dict[str, Any]] = []
        dropped = 0
        for path in (Path(p) for p, _, _ in srcs):
            try:
                e, d = entries_of(path, short_cite(path, self.roots), path.name in self.curated, *file_class(path))
            except OSError:
                continue
            entries += e
            dropped += d
        keys: dict[str, list[int]] = {}
        for n, entry in enumerate(entries):                    # each n once, so a key never lists it twice
            if k := " ".join(tokens(entry["key"])):
                keys.setdefault(k, []).append(n)
        self.out.parent.mkdir(parents=True, exist_ok=True)
        write_private(self.out, json.dumps({"built": round(time.time(), 3), "sources": len(srcs), "entries": entries, "keys": keys}))
        self.sig = srcs
        return len(keys), len(entries), dropped


class Index:
    """The daemon's half: the JSON, loaded again only when its file changed."""

    def __init__(self, path: Path):
        self.path = path
        self.stamp: tuple[int, int] | None = None
        self.entries: list[dict[str, Any]] = []
        self.first: dict[str, list[tuple[list[str], list[int]]]] = {}

    def load(self) -> "Index":
        try:
            st = self.path.stat()
        except OSError:
            self.stamp, self.entries, self.first = None, [], {}
            return self
        if (st.st_mtime_ns, st.st_size) != self.stamp:
            data = json.loads(self.path.read_text())
            self.entries, self.first = data["entries"], {}
            for k, ids in data["keys"].items():
                words = k.split()
                self.first.setdefault(words[0], []).append((words, ids))
            for v in self.first.values():
                v.sort(key=lambda kv: -len(kv[0]))          # the longest key at a position wins
            self.stamp = (st.st_mtime_ns, st.st_size)
        return self

    def match(self, text: str, limit: int = MATCH_MAX, keep: Callable[[dict[str, Any]], bool] | None = None) -> list[dict[str, Any]]:
        """Entries for the names in `text`, in the order they are named, each once; only those `keep` takes (by cite, class and room), if given."""
        words = tokens(text)
        out: list[dict[str, Any]] = []
        seen: set[int] = set()
        i = 0
        while i < len(words) and len(out) < limit:
            step = 1
            for key, ids in self.first.get(words[i], []):
                if words[i:i + len(key)] == key:
                    for n in ids:
                        if n not in seen and len(out) < limit and (keep is None or keep(self.entries[n])):
                            seen.add(n)
                            out.append(self.entries[n])
                    step = len(key)
                    break
            i += step
        return out
