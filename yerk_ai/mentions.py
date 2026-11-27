# The key rules began as those of the workspace recall-index tool this harness replaces.
"""Mention recall: when a message names one of your people, places or things, the matching lines of
your fact files are put in front of the model before it starts, with no tool call.

Keys come from a fact file's name (car, gym, appliances), its headings and the leading name of each
bullet, nested or not; a one-word key in a line's first words keys it too ("Ada: ..." gives "Ada Example"
its first name). An entry is the bullet or the start of its section, at most ENTRY_CHARS, with the file and line it
came from; yerk-index writes them as one JSON file beside the search index. An entry carrying a
do-not-surface mark (⛔, "ask-only"), or under a heading or bullet that carries one, never enters, and the
private yerkd.toml can keep whole files out (`mentions_exclude`).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, tzinfo
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

from .memindex import STOP

ENTRY_CHARS = 300
KEY_MIN, KEY_MAX, KEY_WORDS = 3, 44, 6
MATCH_MAX = 6
DROP = re.compile(r"⛔|ask[- ]only", re.I)
OLD = "[superseded"                 # a corrected line (memory.remember): never an entry, its line read as blank
TOKEN = re.compile(r"[a-z0-9][a-z0-9'.\-]*")
BOLD = re.compile(r"\*\*(.+?)\*\*")
SPLIT = re.compile(r"\s+[—–-]\s+|\s*[(,:=;]|\s+→\s+")
HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
SENTENCEY = frozenset("is are was were be he she it they we i you my his her their our this that these "
                      "those has have had do does did will would should can could not no if when".split())
KEY_STOP = frozenset((  # a word that names nothing, never a one-word key (with STOP): a file or section label, the calendar,
    "the and people home house work money rule rules note notes decisions identity purchases health family today things "
    "misc general other current status open loops daily facts log summary overview history index todo plan plans memory "
    "pinned january february march april may june july august september october november december jan feb mar apr jun "
    "jul aug sep sept oct nov dec monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur fri "
    "sat sun day days week month year date time tonight tomorrow yesterday morning afternoon evening night weekend hour "
    "ago soon later early late back next last past first still already ever never always maybe really even only well "
    "away around down off else sure right left new old good bad single whole ask asks call come know like look make "
    "need run runs say says see sees send show start stop take tell try use uses want watch keep let put give find feel "
    "leave mean help talk turn play move live read write wait stay buy pay").split())    # and the everyday adverbs and verbs


def tokens(s: str, fold: bool = False) -> list[str]:
    """Lowercase words, the possessive folded away ("the kettle's lid" meets "kettle"); `fold`, keys and messages only: a plural too."""
    words = (re.sub(r"'s$|'$", "", t).strip(".-") for t in TOKEN.findall(str(s or "").lower().replace("’", "'")))
    return [t[:-1] if fold and len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t for t in words if t]


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
    if any(w.lower().strip(".,'") in SENTENCEY for w in words) or len(t := tokens(head)) == 1 and {*t, *tokens(head, True)} & (KEY_STOP | STOP):
        return None                                       # a sentence, or one word that names nothing ("Back", "Who", "August")
    return head if re.search(r"[A-Za-z]", head) else None


def entries_of(path: Path, cite: str, curated: bool, cls: str = "private", room: str = "") -> tuple[list[dict[str, Any]], int]:
    """(entries, dropped) for one file: headings always; for a fact file also its name and bullets; each with the file's class
    and room (memory.file_class), which the daemon keeps a room that is not an owner room to. The front matter is no entry. A bullet
    past its until (memory.ended) stays one until the sweep blanks it: the index is built when a file changes, not when a day passes."""
    lines = ["" if OLD in x else x for x in path.read_text(errors="replace").splitlines()]
    if lines[:1] == ["---"] and "---" in lines[1:]:
        lines = [""] * (end := lines.index("---", 1) + 1) + lines[end:]
    out: list[dict[str, Any]] = []
    dropped, marked, up = 0, [False] * len(lines), [(0, False)]      # up: the headings and bullets open above, by depth, and their mark
    for i, ln in enumerate(lines):                                       # a line's mark: its own, or that of one it sits under
        d = len(h.group(1)) if (h := HEADING.match(ln)) else 9 + len(ln) - len(ln.lstrip()) if ln.strip() else 999
        up = [u for u in up if u[0] < d]
        marked[i] = bool(DROP.search(ln)) or up[-1][1]
        up.append((d, marked[i]))

    def add(key: str | None, text: str, line: int) -> None:          # no key of its own: the Builder lists it under a name, or drops it
        nonlocal dropped
        if marked[line - 1] or DROP.search(text) or DROP.search(key or ""):
            dropped += 1
            return
        body = re.sub(r"\s+", " ", strip_md(text))
        out.append({"key": key or "", "text": body[:ENTRY_CHARS - 1] + "…" if len(body) > ENTRY_CHARS else body, "cite": f"{cite}:{line}",
                    "class": cls, "room": room})

    def section(i: int) -> str:
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
        body = [(i, ln.strip()) for i, ln in enumerate(lines) if ln.strip() and not HEADING.match(ln) and not marked[i]][:6]
        if body and (name := key_of(path.stem.replace("-", " ").replace("_", " "))):       # a file name that is no key: none
            add(name, " ".join(t for _, t in body), body[0][0] + 1)
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

    def __init__(self, out: Path, roots: tuple[Path, ...], curated: tuple[str, ...], exclude: tuple[str, ...], tz: tzinfo | None = None):
        self.out, self.roots, self.curated, self.exclude, self.tz = out, roots, curated, exclude, tz    # tz: the day a card is live on
        self.sig: list[Any] | None = None

    def is_source(self, path: Path) -> bool:
        if not (path.name in self.curated or (path.parent.name == "facts" and path.suffix == ".md")):
            return False                                  # most paths: no stat, no cite (a pass asks of every one)
        rel = short_cite(path, self.roots)
        return not any(fnmatch(rel, p) or fnmatch(path.name, p) for p in self.exclude)

    def refresh(self, disk: dict[str, tuple[int, int]]) -> tuple[int, int, int] | None:
        from .config import write_private
        from .memory import card_names, file_class, moves
        srcs = sorted((p, *st) for p, st in disk.items() if self.is_source(Path(p)))
        if srcs == self.sig and self.out.is_file():
            return None
        entries: list[dict[str, Any]] = []
        src: list[tuple[str, str]] = []                        # each entry's file, and that file as a key if a fact file ("ada example")
        akas: dict[str, list[str]] = {}                        # a card's other name (memory.card_names): the own names of the cards saying it
        dropped = 0
        for path in (Path(p) for p, _, _ in srcs):
            try:
                e, d = entries_of(path, short_cite(path, self.roots), path.name in self.curated, *file_class(path))
            except OSError:
                continue
            entries += e
            src += [(str(path), " ".join(tokens(path.stem.replace("-", " ").replace("_", " "), True)) if path.parent.name == "facts" else "")] * len(e)
            said = [" ".join(tokens(x, True)) for x in ([] if path.name in self.curated else card_names(path))]
            for a in dict.fromkeys(x for x in said[1:] if x != said[0]):
                akas.setdefault(a, []).append(said[0])
            dropped += d
        keys: dict[str, list[int]] = {}
        for n, entry in enumerate(entries):
            if k := " ".join(tokens(entry["key"], True)):
                keys.setdefault(k, []).append(n)
        names = {k for k in keys if " " not in k}       # a name: a one-word key (key_of lets no word that names nothing be one)
        for n, entry in enumerate(entries):
            t = tokens(entry["key"], True)
            if len(t) != 1:                                    # no key of its own, or a longer one: listed under each name its first words
                for name in names.intersection(tokens(" ".join(x for x in entry["text"].split()[:KEY_WORDS] if x[:1].isupper()), True)):
                    keys[name].append(n)                       # write as a name ("Ada lives" under ada, never "Ada's car" under car)

        def once(order: list[int]) -> list[int]:               # within one file, a text that starts another listed one, or starts with it, once
            kept: list[int] = []
            for n in order:
                if not any(src[m][0] == src[n][0] and entries[m]["text"][:len(entries[n]["text"])] == entries[n]["text"][:len(entries[m]["text"])] for m in kept):
                    kept.append(n)
            return kept
        lists = {k: once(sorted(set(ids), key=lambda n: (src[n][1] != k, src[n][1] != k and " ".join(tokens(entries[n]["key"], True)) != k, n)))
                 for k, ids in keys.items()}                   # the key's own fact file in its order, then lines keyed by it, then file order
        for a, owns in akas.items():                           # an aka: what its card's own name lists, in that order, after a card named it
            here = lists.get(a, [])
            if got := once([*(n for n in here if src[n][1] == a), *(n for o in owns for n in lists.get(o, [])), *here]):
                lists[a] = got
        for m, to in moves(self.roots[0], datetime.now(self.tz).date()).items() if self.roots else ():    # a moved card's names: its
            own = " ".join(tokens(card_names(to)[0], True))                     # target's own name's list, then what else they key
            for a in {" ".join(tokens(x, True)) for x in card_names(m)} - {own}:
                if got := once([*lists.get(own, []), *(n for n in lists.get(a, []) if Path(src[n][0]).resolve() != m)]):
                    lists[a] = got
                else:
                    lists.pop(a, None)
        at: dict[int, int] = {}                                # an entry's place in the file written: only what a key lists goes in
        keys = {k: [at.setdefault(n, len(at)) for n in kept] for k, kept in lists.items()}
        entries = [entries[n] for n in at]
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

    def match(self, text: str, limit: int = MATCH_MAX, keep: Callable[[dict[str, Any]], bool] | None = None, strict: bool = False) -> list[dict[str, Any]]:
        """Entries for the names in `text`, in the order named, each once and at most `limit` a name, each with the name it came
        under (`named`: the recall part shares its budget by name); only those `keep` takes, if given; `strict`: a key with none it takes is no match."""
        words = tokens(text, True)
        out: list[dict[str, Any]] = []
        seen: set[int] = set()
        i = 0
        while i < len(words):
            step = 1
            for key, ids in self.first.get(words[i], []):
                if words[i:i + len(key)] == key and not (strict and keep and not any(keep(self.entries[n]) for n in ids)):
                    for n in [n for n in ids if n not in seen and (keep is None or keep(self.entries[n]))][:limit]:
                        seen.add(n)
                        out.append({**self.entries[n], "named": " ".join(key)})
                    step = len(key)
                    break
            i += step
        return out
