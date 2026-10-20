"""Reads and writes the memory folder: the day log every message is written to, exact reads of its files, recall of earlier notes, and recall's test bench.

Files are the truth (DESIGN 5). The tree lives in the private layer: `[memory] root` in yerkd.toml
(default `memory/` beside it) holds `daily/YYYY-MM-DD.md`, where every message of every room is
written as it lands (L55), and the fact files, the curated file and open loops. `[memory] also` names
roots the index reads but nothing here writes. The index itself is yerk-index (memindex.py).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import memindex, mentions
from .config import REPO, load_toml, write_private

MARK = re.compile(r"\[m:([^\]\s]+)\]$", re.M)        # the message id that ends each captured line
READ_MAX = 400                                       # read_memory: lines per call
READ_CHARS = 20_000
READ_LINES = 12          # read_memory's default window: a whole hit chunk from its first line for 98 in 100 hits (measured)
# recall: characters per hit at most. 750 with RECALL_CHARS 4500 kept 84 of 84 answers keywords only and 116 of 116 with
# the key, the PR 30 cold read's measurement (700 kept 83 of 84 and held four answers on a 0 to 40 character margin)
SNIPPET = 750
RECALL_CHARS = 4500      # recall: the first six hits' snippets together ([memory] recall_chars), six hits of SNIPPET
RECALL_HITS = 6          # recall_chars is six hits' worth: a call of more cuts its first six as a call of six does
EXTRA = 160              # recall: each hit past the sixth, its best line cut around the match, on top of recall_chars


@dataclass(frozen=True)
class Settings:
    root: Path                  # the install's own tree; capture writes root/daily
    scope: memindex.Scope       # what the index covers: root and the `also` roots
    index_db: Path
    tz: tzinfo
    interval: float
    bench: Path
    mentions: Path                  # the mention index yerk-index writes
    curated: tuple[str, ...]        # file names whose headings are mention keys
    mentions_exclude: tuple[str, ...]   # globs kept out of mention recall
    embeddings_key: Path            # the provider key, a path in the private layer, never read here
    embeddings_model: str
    embeddings_dim: int
    embeddings_units: str           # what gets a vector in memory: both (lines and chunks, measured best), lines, or chunks
    embeddings_width: int           # dimensions kept in memory (the first `width` of `dim`, renormalised)
    index_status: Path              # what yerk-index serves: its port and the embeddings' state
    recall_chars: int               # one recall's first six snippets together, at most
    scoped: bool = False            # a room's own `also` (only): index hits and names kept to the roots in `scope`

    @property
    def daily(self) -> Path:
        return self.root / "daily"

    @property
    def roots_for_mentions(self) -> tuple[Path, ...]:
        return self.scope.roots

    def only(self, private: Path, room: str, also: list[Any]) -> Settings:
        """A room's `also` in rooms.toml, resolved as `[memory] also` is: these settings with the read-only roots cut to those,
        so its recall, read_memory and mention recall serve its own tree and them; a root the install lacks stops the start."""
        want = [(private / Path(str(x)).expanduser()).resolve() for x in also]
        for p in (p for p in want if p not in self.scope.roots[1:]):
            raise SystemExit(f"room {room}: also {p} is not one of the [memory] also roots in yerkd.toml")
        return replace(self, scoped=True, scope=replace(self.scope, roots=tuple(r for r in self.scope.roots if r in (self.root, *want))))


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
    tz: tzinfo = ZoneInfo(str(conf.get("timezone") or system_zone()))   # a zone, never one start-time offset (R5)
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
                    index_status=state / "memory" / "index.json",
                    recall_chars=max(100, int(conf.get("recall_chars", RECALL_CHARS))))


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
RECALL_SETTINGS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "recall_chars": ((str(RECALL_CHARS), "any number from 100"),
                     f"characters one recall call hands the room for its first six hits, six hits' worth, their snippets "
                     f"together (each hit at most {SNIPPET}: the lines that carry the match, the cite reads on); each hit "
                     f"past the sixth adds its best line, at most {EXTRA}; measured on the 164-question set by the recall "
                     f"diet's cold read, {RECALL_CHARS} kept 116 of 116 answers found with the key and 84 of 84 keywords "
                     "only, for about 155 characters a call more than 700 and 4200, still about 21% under whole chunks; "
                     "less shrinks each of the first six", str(RECALL_CHARS)),
}


def described(conf: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The [memory] embedding and recall settings with their value here, where it came from, choices and
    plain words (the shape `yerk config` prints for every described setting)."""
    conf = conf or {}
    out = []
    for table, reloads in ((EMBEDDING_SETTINGS, EMBEDDING_RELOADS), (RECALL_SETTINGS, "takes effect when yerkd restarts")):
        for key, (choices, what, default) in table.items():
            value = conf.get(key, default)
            out.append({"key": f"memory.{key}", "value": str(value),
                        "default": default, "source": "yerkd.toml" if key in conf else "default", "choices": list(choices),
                        "what": what, "reloads": reloads})
    return out


def system_zone() -> str:
    """The box's IANA zone name: $TZ, else where /etc/localtime points, else UTC. A name resolves
    each line's offset for its own instant; a fixed offset read at start is an hour off after every
    clock change until a restart (restart test R5)."""
    if not (name := os.environ.get("TZ", "").lstrip(":")):
        try:
            target = os.readlink("/etc/localtime")
            name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
        except OSError:
            name = ""
    try:
        ZoneInfo(name or "UTC")
    except (KeyError, ValueError):
        return "UTC"
    return name or "UTC"


def one_line(text: str) -> str:
    return " / ".join(x.strip() for x in text.splitlines() if x.strip())


CLASSES = ("private", "room", "shared")           # who may read a memory file or a day-log line
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)   # a memory file's front matter


def room_class(conf: dict[str, Any]) -> str:
    """The class a room's lines carry: `class` in rooms.toml, else private for an owner room (the
    owner's rooms read everything) and room for any other (its own lines and shared files only)."""
    if (c := str(conf.get("class") or "")) in CLASSES:
        return c
    return "private" if str(conf.get("privacy", "owner")) == "owner" else "room"


@dataclass(frozen=True)
class Reader:
    """Who is asking: the room's name, its stable id, whether it is an owner room, and whether `sealed` keeps it, in Yerk's
    own tree, to its own room's lines (its `also` roots stay as its class lets it read them)."""
    name: str = ""
    rid: str = ""
    owner: bool = False
    sealed: bool = False


def file_class(path: Path) -> tuple[str, str]:
    """(class, room id) of a memory file: its front matter (`class: shared`, `room: <id>`), else a
    sidecar `<file>.class` (one line: the class, then optionally the room id), else private. The
    `also` roots are indexed read-only, so a sidecar is how a file nobody edits gets a class."""
    cls, room = "private", ""
    with suppress(OSError):
        head = path.open(encoding="utf-8", errors="replace").read(400)
        if (m := FRONT.match(head)):
            for line in m.group(1).splitlines():
                k, _, v = line.partition(":")
                if k.strip() == "class" and v.strip() in CLASSES:
                    cls = v.strip()
                elif k.strip() == "room":
                    room = v.strip()
            return cls, room
        side = Path(str(path) + ".class")
        if side.is_file():
            words = side.read_text().split()
            if words and words[0] in CLASSES:
                cls = words[0]
            room = words[1] if len(words) > 1 else ""
    return cls, room


def may_read(reader: Reader | None, cls: str, room: str) -> bool:
    """An owner room reads everything; any other room reads shared files and its own room's lines; a sealed one its own lines only."""
    return reader is None or ((reader.owner or cls == "shared") and not reader.sealed) or (
        (cls == "room" or reader.sealed) and bool(room) and room == reader.rid)


def allowed_text(reader: Reader | None, path: Path, text: str, blank: str = "", whole: bool = False,
                 daily: Path | None = None) -> str | None:
    """`text` of a memory file as `reader` may see it: a day log keeps only the lines it may read; the
    others, and every line that is not a day-log line (a free-form note, a hand edit, an import),
    become `blank`, so line numbers hold; the day header, a blank line and an attachment line under a
    kept line stay. None when nothing is left unless `whole`, an exact read that must keep its line
    count. A day log is a date-named file, under `daily` when given: a date-named file anywhere else
    is a plain file, whole or None by its class, like the rest."""
    if reader is None or reader.owner and not (reader.sealed and (daily is None or Path(path).resolve().is_relative_to(daily.resolve().parent))):
        return text
    if is_daylog(str(path)) and (daily is None or Path(path).resolve().parent == daily.resolve()):
        out = []
        kept = False
        for raw in text.splitlines():
            if (ln := parse_line(raw)) is None:
                shown = (kept and raw.startswith("  - attachment: ")) or not raw.strip() or bool(memindex.DAY_HEAD.match(raw))
                out.append(raw if shown else blank)
                kept = kept and raw.startswith("  - attachment: ")
                continue
            kept = may_read(reader, ln.cls, ln.rid)
            out.append(raw if kept else blank)
        return "\n".join(out) if whole or any(parse_line(x) for x in out) else None
    cls, room = file_class(path)
    return text if may_read(reader, cls, room) else None


@dataclass(frozen=True)
class Line:
    time: str                 # HH:MM:SS
    offset: str               # +hh:mm, or "" on a line written before the change
    label: str                # the room's name as it was when the line was written
    room_id: str              # the stable id, or "" when the line carries the label only
    role: str
    who: str                  # a person's handle, or ""
    body: str
    cls: str                  # private, room or shared ("" on an old line: read as the room's class now)
    mid: str

    @property
    def rid(self) -> str:
        return self.room_id or self.label

    def of_room(self, name: str, rid: str) -> bool:
        """Written by room `name` (stable id `rid`): by id, or by label on a line with no id."""
        return self.rid == rid or (not self.room_id and self.label == name)


def parse_line(raw: str) -> Line | None:
    if not (m := memindex.LINE.match(raw)):          # the grammar sits in memindex, whose chunker reads it too
        return None
    return Line(m.group(1), m.group(2) or "", m.group(3), m.group(4) or "", m.group(5), m.group(6) or "",
                m.group(7), m.group(8) or "", m.group(9) or "")


def _token(word: str, drop: str = r"[\s:@#\[\]]+") -> str:
    return re.sub(drop, "_", word.strip())


def format_line(at: datetime, label: str, rid: str, role: str, who: str, body: str, cls: str, mid: str) -> str:
    off = f"{at:%z}"
    room = _token(label, r"[\s#]+") if rid == label or not rid else f"{_token(label, r'[\s#]+')}#{_token(rid, r'[\s#]+')}"
    author = f"{role}@{_token(who)}" if who else role
    return f"- {at:%H:%M:%S}{off[:3]}:{off[3:]} {room} {author}: {body} [c:{cls}] [m:{mid}]"


@contextmanager
def daily_lock(daily: Path) -> Iterator[None]:
    """The one lock every writer of the day logs holds, a flock on `.daily.lock` beside the folder
    (the daemon's capture and the migration's rewrite, in their own processes): an append that arrives
    while a file is being rewritten waits for the rename and lands in the new file, never in the file
    the rename drops. The lock file sits outside `daily/`, so a listing of the day files stays clean."""
    daily.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(daily.parent / f".{daily.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class DailyLog:
    """The day's log: one line per message, written as it lands, short and attachment-only ones
    included, each attachment on a line of its own under it. The line ends with the message id, so
    a message captured twice (a replay, a restart, a rerun) is written once. The day and the time
    are the message's own, in the install's time zone."""

    def __init__(self, daily: Path, tz: tzinfo, rooms: dict[str, tuple[str, str]] | None = None):
        self.daily, self.tz = daily, tz
        self.rooms = rooms or {}                 # room name -> (stable id, class); unlisted: the name, private
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
                when: datetime | None = None, edited: bool = False, who: str = "") -> bool:
        """One message into the log; False when its line is already there. The time carries its own
        UTC offset (the zone resolved for that instant), the room its stable id with the name as the
        label, the author a role and, when known, who (a handle), and the line its room's class."""
        files = files or []
        at = (when or datetime.now(timezone.utc)).astimezone(self.tz)
        day = at.strftime("%Y-%m-%d")
        body = one_line(text) or ("(attachment only)" if files else "(no text)")
        key = f"{mid}.e{hashlib.sha1(body.encode()).hexdigest()[:8]}" if edited else mid
        rid, cls = self.rooms.get(room, (room, "private"))
        lines = [format_line(at, room, rid, role, who, f"{'(edited) ' if edited else ''}{body}", cls, key)]
        lines += [f"  - attachment: {name}, {kind}, {size:,} bytes" for name, kind, size in files]
        with self.lock, daily_lock(self.daily):
            ids = self._ids(day)
            if key in ids:
                return False
            path = self.daily / f"{day}.md"
            head = "" if path.exists() else f"# {day}\n\n"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(head + "\n".join(lines) + "\n")
            ids.add(key)
        return True


NOTE = "note"                # the role of a day-log line a room files with `remember` (DESIGN 5; L58)
BY = re.compile(re.escape(mentions.OLD) + r" by (\S+?:\d+)\]")     # a mark naming its replacement; `[superseded]` names none
LATER = "superseded.tsv"     # in the tree, `old cite<TAB>new cite`: a line a correction named but could not mark with its cite
RULES = "rules.md"           # in the tree: the install's one rules file, how each job is done in every room (decision 15)


def later(s: Settings) -> dict[str, str]:
    f = s.root / LATER                                               # old cite -> new cite, the newest last
    return {a: b for a, _, b in (x.partition("\t") for x in f.read_text().splitlines()) if b} if f.is_file() else {}


def remember(s: Settings, log: DailyLog, room: str, mid: str, text: str, about: str = "",
             old: tuple[str, ...] = (), reader: Reader | None = None) -> dict[str, Any]:
    """A fact, decision or correction into the install's own tree, with its room, message and time. An owner
    room's fact goes to facts/<about>.md, named by a key mention recall keys (private); anything else, or a fact
    with no such key or no folder inside the tree, to the room's day log as a `note` line of its class. Each line in
    `old` (`file:line | its words`: the one line holding them, from there through the next 12) it replaces: the
    room's own line in this tree (any, for an owner room) is marked superseded, by the new cite where all its readers
    may read it, else in LATER as a line it may only read is; a heading or a blank is refused."""
    body, about = one_line(text), about.strip()
    if not body:
        return {"ok": False, "error": "nothing to remember: give the fact, decision or correction as text"}
    rid, cls = log.rooms.get(room, (room, "private"))
    owner, refused, olds = reader is None or reader.owner and not reader.sealed, [], {b: list[tuple[Path, int]]() for b in (True, False)}
    for c in old:
        c, _, said = (x.strip() for x in c.partition("|"))
        r, want = read(s, c, lines=READ_LINES + 1 if said else 1), set(memindex.WORD.findall(memindex.norm(said)))
        rows = (allowed_text(reader, Path(r["path"]), r["text"], whole=True, daily=s.daily) or "").split("\n") if r["ok"] else [""]
        hit = [i for i, x in enumerate(rows) if want <= set(memindex.WORD.findall(memindex.norm(x)))]
        line = rows[hit[0]] if len(hit) == 1 else ""
        if not re.search(r":\d*[1-9]\d*$", c) or not re.search(r"\w", line) or line.lstrip().startswith("#"):
            refused.append(f"{c}: " + (r.get("error") or (f"{len(hit)} lines from there to the next {READ_LINES} hold those words"
                           if said and len(hit) != 1 else "a heading, a blank or not a line this room may read")))
            continue
        p, ln, k = Path(r["path"]), parse_line(line), r["from"] + hit[0]
        olds[p.is_relative_to(s.root) and (owner or (ln is not None and (ln.cls, ln.rid) == (cls, rid)))].append((p, k))
    edit, named = ([f"{cite(s, p)}:{k}" for p, k in olds[b]] for b in (True, False))
    note = body + (f" (supersedes {', '.join(edit + named)})" if edit or named else "")
    at = datetime.now(timezone.utc).astimezone(s.tz)
    words = [w for w in "-".join(mentions.tokens(about)).split("-") if w][:mentions.KEY_WORDS]
    while len("-".join(words)) > mentions.KEY_MAX:          # a name mention recall keys, never too long to file (F7)
        words.pop()
    path = (s.root / "facts" / f"{'-'.join(words)}.md").resolve()       # never through a link out of the tree (F8)
    if owner and mentions.key_of(" ".join(words)) and path.is_relative_to(s.root) and file_class(path)[0] == "private" and not any(
            path.is_relative_to(a) and not s.root.is_relative_to(a) for a in s.scope.roots[1:]):
        with daily_lock(s.daily):                  # the one writers' lock, a fact file's too
            have = path.read_text().splitlines() if path.is_file() else [f"# {about}", ""]
            n = next((i + 1 for i, x in enumerate(have) if x.startswith(f"- {note} (")), 0)     # a retried call
            if not n:
                src = f"{room if rid in ('', room) else f'{room}#{rid}'}, {at:%Y-%m-%d %H:%M:%S%:z}"
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_private(path, "\n".join(have := have + [f"- {note} ({src}) [m:{mid}]"]) + "\n")
                n = len(have)
    else:
        key = f"{mid}.n{hashlib.sha1(note.encode()).hexdigest()[:8]}"     # one line per note, a retry once
        log.capture(room, key, NOTE, f"{about}: {note}" if about and not note.lower().startswith(about.lower()) else note, when=at)
        path = s.daily / f"{at:%Y-%m-%d}.md"
        n = next((i + 1 for i, x in enumerate(path.read_text().splitlines()) if x.endswith(f"[m:{key}]")), 0)
    new, nc = f"{cite(s, path)}:{n}", (cls, rid) if path.parent == s.daily else ("private", "")    # the new line's class
    taken, pairs = dict[str, str](), [(c if p.is_relative_to(s.root) else f"{p}:{k}", new) for (p, k), c in zip(olds[False], named)]
    for (p, k), c in zip(olds[True], edit):
        with daily_lock(s.daily):
            lines = p.read_text().split("\n")
            by = BY.findall(lines[k - 1]) or ([later(s).get(c, "")] if mentions.OLD in lines[k - 1] else [])
            if by and by[0] != new:         # another correction marked it first (F6)
                taken[c] = f"{c}: already superseded" + (f" by {by[0]}" if BY.search(lines[k - 1]) else "")
            elif not by:                    # the mark goes before a day-log line's class and id
                ln = parse_line(lines[k - 1])
                oc, orid = (ln.cls, ln.rid) if ln and p.parent == s.daily.resolve() else file_class(p)
                shown = oc not in ("room", "shared") or nc[0] == "shared" or nc == (oc, orid)    # no private cite on it (F1)
                lines[k - 1] = mark(lines[k - 1], new if shown else "")
                write_private(p, "\n".join(lines))
                pairs += [] if shown else [(c, new)]
    if pairs:
        with daily_lock(s.daily), os.fdopen(os.open(s.root / LATER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
            f.write("".join(f"{a}\t{b}\n" for a, b in pairs))
    return {"ok": True, "cite": new, "superseded": [c for c in edit if c not in taken], "refused": refused, "not_edited":
            [*taken.values(), *(f"{c}: a read-only root or not this room's own line; the new line names it" for c in named)]}


def mark(line: str, by: str = "") -> str:
    """`line` marked superseded, by `by` (its replacement's cite) when given; the mark goes before a line's class and id."""
    return re.sub(r"^(.*?)((?: \[c:\w+\])?(?: \[m:[^\]\s]+\])?)$", lambda m: f"{m[1]} {mentions.OLD}{' by ' + by if by else ''}]{m[2]}",
                  line, count=1)


def rule(s: Settings, room: str, mid: str, job: str, text: str) -> dict[str, Any]:
    """How a job is done from now on, from an owner's room, into RULES: the one file every room starts with and hears
    from on its next message when it changes (context.news), shared so read_memory reads it anywhere. One line with its
    room, message and time; the job's live line before it is marked superseded by the new one, never deleted."""
    job, body = one_line(job).strip(" :").lower(), one_line(text)
    if not job or not body:
        return {"ok": False, "error": "a rule needs the job's short name and how it is done, both as text"}
    path, head, at = s.root / RULES, f"- {job}: ", datetime.now(timezone.utc).astimezone(s.tz)
    with daily_lock(s.daily):                  # the one writers' lock
        lines = path.read_text().splitlines() if path.is_file() else ["---", "class: shared", "---", "# Rules: one live line per job, in every room", ""]
        live = [i for i, x in enumerate(lines) if x.startswith(head) and mentions.OLD not in x]
        n = next((i + 1 for i in live if lines[i].startswith(f"{head}{body} (")), 0)       # a retried call
        gone = [] if n else live
        if not n:
            n = len(lines) + 1
            lines = [mark(x, f"{cite(s, path)}:{n}") if i in gone else x for i, x in enumerate(lines)]
            write_private(path, "\n".join([*lines, f"{head}{body} ({room}, {at:%Y-%m-%d %H:%M:%S%:z}) [m:{mid}]"]) + "\n")
    return {"ok": True, "job": job, "cite": f"{cite(s, path)}:{n}", "replaced": [f"{cite(s, path)}:{i + 1}" for i in gone]}


def live_first(s: Settings, r: dict[str, Any], reader: Reader | None = None) -> dict[str, Any]:
    """Recall's answer with no superseded line above its replacement: a hit that a later hit names as replaced
    gets that hit right above it, and a hit showing a marked line, or whose window (its line and the next 12)
    holds a line in LATER, gets the live end of that line's replacements (five hops at most) read in right above
    it, as the reader may see it, unless a hit above already shows it."""
    hits, kept = list(r.get("hits") or []), later(s)

    def above(j: int) -> int:
        names = {c.strip().rpartition(":")[0] for m in re.findall(r"\(supersedes ([^)]+)\)", hits[j]["text"]) for c in m.split(",")}
        return next((i for i in range(j) if hits[i]["cite"].rpartition(":")[0] in names), j)
    out: list[dict[str, Any]] = []
    for h in [hits[j] for j in sorted(range(len(hits)), key=lambda j: (above(j), above(j) == j, j))]:
        f, _, at = h["cite"].rpartition(":")         # a read-only root's pair names its real file: two copies share a cite (A1)
        mine = (f, read(s, h["cite"], lines=1).get("path") if kept else f)
        for c in BY.findall(h["text"]) + [b for a, b in kept.items() if a.rpartition(":")[0] in mine
                                           and 0 <= int(a.rpartition(":")[2]) - int(at) <= READ_LINES]:
            for _ in range(5):          # a replacement replaced in turn: on to the live end (F4)
                got = read(s, c, lines=1)
                line = (allowed_text(reader, Path(got["path"]), got["text"], daily=s.daily) or "").strip() if got["ok"] else ""
                if not (c := (BY.findall(line) or [kept.get(f"{cite(s, Path(got['path']))}:{got['from']}", "")])[0] if line else ""):
                    break
            if line and not any(line in x["text"] for x in [*out, h]):
                out.append({"cite": f"{cite(s, Path(got['path']))}:{got['from']}", "text": line})
        out.append(h)
    return {**r, "hits": out} if r.get("ok") else r


def read(s: Settings, path: str, start: int = 1, lines: int = READ_LINES, reader: Reader | None = None) -> dict[str, Any]:
    """An exact excerpt of a file in the memory tree. A relative path is taken from the tree's root, else
    as a cite names it (a root's name, then the path under it); a recall `cite` (file:line) starts at its
    line unless `start` is past 1. Anything that resolves outside the scope (a `..`, an absolute path
    elsewhere, a link out) is refused.
    `reader` (the asking room) sees only what its class allows: a private file is refused outright, a
    day log keeps its own room's lines and blanks the others, so the line numbers still cite."""
    m = re.fullmatch(r"(.+?):(\d{1,9})", path)     # nine digits at most: int() refuses a line of 4,300 or more
    if m:
        path, start = m.group(1), start if int(start) > 1 else int(m.group(2))
    try:            # a NUL, or a name too long (3.12 and 3.13 raise that from exists()), is no such file
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = next((c for c in (s.root / p, *(r.parent / p for r in s.scope.roots if p.parts[:1] == (r.name,)))
                      if c.exists()), s.root / p)
        real = p.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return {"ok": False, "error": f"no such file in the memory tree: {path}"}
    if not s.scope.contains(real):
        return {"ok": False, "error": f"{path} is outside the memory tree; read_memory reads only the private memory tree"}
    if not real.is_file():
        return {"ok": False, "error": f"{path} is a folder, not a file"}
    start, lines = max(1, int(start)), max(1, min(int(lines), READ_MAX))
    daylog = is_daylog(str(real)) and real.parent == s.daily.resolve()   # date-named elsewhere: a plain file
    if reader and (not reader.owner or reader.sealed and real.is_relative_to(s.root)) and not daylog and not may_read(reader, *file_class(real)):
        return {"ok": False, "error": f"{path} is not readable from room {reader.name or '?'}: its class keeps it to " "the owner's rooms"}
    out: list[str] = []
    total = 0
    with real.open(encoding="utf-8", errors="replace") as f:
        for total, line in enumerate(f, 1):
            if start <= total < start + lines:
                out.append(line.rstrip("\n"))
    text = "\n".join(out)
    if reader and (not reader.owner or reader.sealed) and daylog:
        text = allowed_text(reader, real, text, blank="(a line of another room)", whole=True, daily=s.daily) or ""
    cut = len(text) > READ_CHARS
    return {"ok": True, "path": str(real), "from": start, "to": start + len(out) - 1, "total_lines": total,
            "text": text[:READ_CHARS] + ("\n[cut at 20,000 characters; read fewer lines]" if cut else "")}


POOL = 80            # candidates from each search (keywords, meaning) before fusion
RRF_K = 20           # reciprocal rank fusion's constant: 1 / (RRF_K + rank); 60 before run 4, 20 measured on the 164-question set
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
    if (state := meaning_state(s)) != "ok":
        return state, []
    port = int(index_status(s)["port"])
    # `near` asks for the line each hit's vector matched; yerk-index adds it only when asked, so an older daemon,
    # whose room filter knows nothing of it, never receives it
    req = urllib.request.Request(f"http://127.0.0.1:{port}/search",
                                 data=json.dumps({"query": query, "pool": pool, "near": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=deadline_s) as r:
            out = json.loads(r.read())
    except OSError as e:                            # TimeoutError is one
        return ("index-timeout" if isinstance(e, TimeoutError) or "timed out" in str(e).lower() else "no-index"), []
    except ValueError:
        return "failure", []
    if not isinstance(out, dict) or not out.get("ok"):
        return str((out or {}).get("status", "failure")) if isinstance(out, dict) else "failure", []
    return "ok", list(out.get("hits", []))


def fuse(keywords: list[dict[str, Any]], vectors: list[dict[str, Any]], k: int, keep: int = KEEP) -> list[dict[str, Any]]:
    """Reciprocal rank fusion of the two ranked lists: each chunk scores the sum over the lists it is in
    of 1 / (RRF_K + its rank there); a tie breaks on the cosine. No calibration between a bm25 cover
    and a cosine is needed, and a chunk both searches put first outranks either search's lone top hit.
    Vector hits under half the best cosine are noise and leave the fusion first. `keep` holds each
    search's own first `keep` hits in the top k (in fused order), so a lone keyword hit at rank two is
    not pushed out by chunks both searches place low."""
    if not vectors:
        return keywords[:k]
    top = float(vectors[0]["score"])
    vectors = [h for h in vectors if float(h["score"]) >= top / 2] if top > 0 else []
    if not vectors:
        return keywords[:k]
    score: dict[int, float] = {}
    rows: dict[int, dict[str, Any]] = {}
    for ranked in (keywords, vectors):
        for rank, h in enumerate(ranked, 1):
            score[h["id"]] = score.get(h["id"], 0.0) + 1.0 / (RRF_K + rank)
            rows.setdefault(h["id"], h)
    cos = {h["id"]: float(h["score"]) for h in vectors}
    near = {h["id"]: h["near"] for h in vectors if h.get("near")}       # the line the meaning matched
    order = sorted(score, key=lambda i: (-score[i], -cos.get(i, 0.0)))
    held = list(dict.fromkeys([h["id"] for h in keywords[:keep]] + [h["id"] for h in vectors[:keep]]))[:k]
    chosen = set(held) | set([cid for cid in order if cid not in held][:k - len(held)])
    out = []
    for cid in [cid for cid in order if cid in chosen]:
        h = dict(rows[cid])
        h["score"] = round(score[cid], 4)
        if cid in near:
            h["near"] = near[cid]
        out.append(h)
    return out


def ranked(s: Settings, query: str, k: int = 6, deadline_s: float = 5.0, reader: Reader | None = None,
           weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Ranked chunks, whole, with path and line, the keyword rank fused with the meaning rank when
    yerk-index serves one (the bench's rank reads these; the recall tool cuts them). A timeout and a
    failure of the index read differently (L54); a provider timeout or error reads differently from
    both: keywords only for this query, logged. `weights` gets the query words' weights."""
    k = max(1, min(int(k), 20))
    try:
        conn = memindex.connect(s.index_db, readonly=True, deadline=time.monotonic() + deadline_s)
        try:
            # twice the pool by bm25, re-ranked by cover down to the pool: the cover re-rank needs
            # the candidates bm25 alone ranks low (measured on the fixed question set: 2 more top-six hits)
            keywords = memindex.search(conn, query, POOL, POOL * 2, weights)
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
    if s.scoped:             # a room's own `also`: its roots' candidates only, before fusion keeps k (every root is indexed)
        keywords, vectors = ([h for h in x if s.scope.contains(Path(h["path"]))] for x in (keywords, vectors))
    if reader and (not reader.owner or reader.sealed):   # a room that is not an owner room, or is sealed, ranks and gets only chunks it can
        keywords, vectors = ([h for h in x if allowed_text(reader, Path(h["path"]), str(h.get("text", "")), daily=s.daily) == h.get("text")]
                             for x in (keywords, vectors))     # read whole (R2): its own line beside a hidden one reaches it through its pack,
    hits = fuse(keywords, vectors, k)       # its topic hits and read_memory, never recall: a chunk read in part would rank on words it may not see
    for h in hits:
        h.pop("id", None)
        h["cite"] = f"{cite(s, Path(h['path']))}:{h['line']}"
    return {"ok": True, "status": "ok", "hits": hits}


def recall(s: Settings, query: str, k: int = 6, deadline_s: float = 5.0, reader: Reader | None = None) -> dict[str, Any]:
    """The recall tool: the hits `ranked` gives, in its order, each cut to the lines that carry the match
    with a cite (file:line of its first line) that read_memory reads on from; the first six snippets
    together hold at most `recall_chars` characters, each at most SNIPPET, and each hit past the sixth
    adds its best line, at most EXTRA."""
    weights: dict[str, float] = {}
    r = ranked(s, query, k, deadline_s, reader, weights)
    return {**r, "hits": snippets(r["hits"], weights, s.recall_chars)} if r["ok"] else r


def snippets(hits: list[dict[str, Any]], weights: dict[str, float], budget: int) -> list[dict[str, Any]]:
    """Each ranked hit as {cite, text}. The first RECALL_HITS share `budget`, each a share of what is left
    (at most SNIPPET), so a short snippet leaves more for the hits after it; each hit past them gets its
    best line on top, cut around the match to at most EXTRA, so a call of twenty hits shows its first six
    exactly as a call of six does."""
    out = []
    shared = min(len(hits), RECALL_HITS)
    for n, h in enumerate(hits):
        if n < shared:
            first, text = snippet(str(h.get("text", "")), int(h["line"]), weights, h.get("near"), min(SNIPPET, budget // (shared - n)))
            budget -= len(text)
        else:
            first, text = snippet(str(h.get("text", "")), int(h["line"]), weights, h.get("near"), EXTRA, one=True)
        out.append({"cite": f"{h['cite'].rpartition(':')[0]}:{first}", "text": text})
    return out


def _root(word: str) -> str:
    """A word's match key: its first five letters, a short word less a plural s."""
    return word[:5] if len(word) > 5 else word[:-1] if len(word) > 3 and word.endswith("s") else word


def snippet(text: str, lo: int, weights: dict[str, float], near: str | None = None, cap: int = SNIPPET,
            one: bool = False) -> tuple[int, str]:
    """(first line, text): the lines of a hit that carry the match, at most about `cap` characters, kept in
    file order. Taken in this order while they fit: the best line (the most query weight; `near`, the line
    the meaning search matched, counts as a match), every other line holding a query word, heaviest first,
    then the lines nearest the best one. A line too long for what is left is cut around its matches (the
    best line always, another matching line when 80 or more are left); a gap between kept lines reads ….
    `one`: the best line alone (a blank one gives way to the next in that order), at most `cap` with its marks."""
    lines = text.splitlines() or [""]
    want = {_root(t): w for t, w in weights.items()}
    top = max(want.values(), default=1.0)
    score = [sum(w for r, w in want.items() if r in {_root(x) for x in memindex.WORD.findall(memindex.norm(ln))})
             + (top if near and ln.strip() == near.strip() else 0.0) for ln in lines]
    best = max(range(len(lines)), key=lambda i: (score[i], -i))
    order = [best, *sorted((i for i in range(len(lines)) if score[i] > 0 and i != best), key=lambda i: (-score[i], i)),
             *(j for d in range(1, len(lines)) for j in (best + d, best - d) if 0 <= j < len(lines) and score[j] <= 0)]
    if one:
        i = next((j for j in order if lines[j].strip()), best)
        ln = lines[i].strip()
        return lo + i, ln if len(ln) <= cap else _cut(ln, want, cap - 2)      # _cut adds at most two …
    took: dict[int, str] = {}
    left = cap
    for i in order:
        if not (ln := lines[i].strip()):
            continue
        if len(ln) <= left:
            took[i], left = ln, left - len(ln) - 1
        elif not took or (left >= 80 and score[i] > 0):
            took[i], left = _cut(ln, want, left), 0
        if left < 40:
            break
    keys = sorted(took) or [best]
    parts: list[str] = []
    for n, i in enumerate(keys):            # a gap of blank lines is no gap
        parts += (["…"] if n and any(x.strip() for x in lines[keys[n - 1] + 1:i]) else []) + [took.get(i, "")]
    return lo + keys[0], "\n".join(parts)


def _cut(line: str, want: dict[str, float], room: int) -> str:
    """The `room` characters of a long line that hold the most query weight, cut at spaces, marked with …."""
    at = [(m.start(), r, w) for r, w in want.items() for m in re.finditer(r"(?<!\w)" + re.escape(r), line, re.I)]
    starts = sorted({max(0, min(p - room // 4, len(line) - room)) for p, _, _ in at}) or [0]
    a = max(starts, key=lambda a0: (sum({r: w for p, r, w in at if a0 <= p < a0 + room - len(r)}.values()), -a0))
    b = min(len(line), a + room)
    if a > 0:
        sp = line.find(" ", a, a + 24)
        a = sp + 1 if sp != -1 else a
    if b < len(line):
        sp = line.rfind(" ", b - 24, b)
        b = sp if sp > a else b
    return ("…" if a > 0 else "") + line[a:b] + ("…" if b < len(line) else "")


def cite(s: Settings, path: Path) -> str:
    """The short name a room cites a file by: its root's name and the path under it."""
    for root in s.scope.roots:
        if path == root:
            return root.name
        if path.is_relative_to(root):
            return (Path(root.name) / path.relative_to(root)).as_posix()
    return path.name


# the room's own log: the topic hit per message and the recall pack at session start (decision 20)
PEOPLE_ROLES = frozenset({"owner", "person", "test-webhook"})   # whose messages fill the pack; the webhook stands in for the owner
DECIDED = re.compile(r"\b(?:decided|decision|agreed|settled on|going with|go with|draft(?:ed)?)\b", re.I)
WINDOW_DAYS = 14
ITEM_CHARS = 280


def window(s: Settings, now: datetime | None = None, days: int = WINDOW_DAYS) -> list[str]:
    """The last `days` day names in the install's time zone, newest first."""
    today = (now or datetime.now(timezone.utc)).astimezone(s.tz).date()
    return [(today - timedelta(days=n)).isoformat() for n in range(days)]


def room_lines(s: Settings, room: str, now: datetime | None = None,
               rid: str | None = None, owner: bool = True) -> Iterator[tuple[str, int, Line, list[str]]]:
    """(day, line number, the parsed line, the lines of its file) for this room's lines of the last
    14 days, newest day first: by the room's stable id (`rid`, default its name), and, for an owner
    room (`owner`, the view every caller but the daemon's non-owner rooms has), by name on a line
    written before ids. A room that is not an owner room gets only its id's lines of class room or
    shared, as read_memory shows them: a former room's name is not its history (R2). The room's own day
    files are small, so they are read straight from disk: fresher than the index (which trails by a
    pass) and a few milliseconds per message."""
    rid = rid or room
    for day in window(s, now):
        f = s.daily / f"{day}.md"
        if not f.is_file():
            continue
        lines = f.read_text(errors="replace").splitlines()
        for n, raw in enumerate(lines):
            ln = parse_line(raw)
            if ln and (ln.of_room(room, rid) if owner else ln.cls in ("room", "shared") and ln.rid == rid):
                yield day, n, ln, lines


def topic_hits(s: Settings, room: str, text: str, exclude: set[str] | None = None,
               now: datetime | None = None, limit: int = 6, rid: str | None = None,
               owner: bool = True) -> list[dict[str, Any]]:
    """The message's key terms against this room's own lines of the last 14 days, best first:
    ranked by how many of the terms a line holds, newer first on a tie. The message itself
    (filed before the turn) and lines sharing too few terms never come back."""
    if not (ts := memindex.terms(text)):
        return []
    want = {t[:5] for t in ts}                                            # a term's stem: its first five letters
    need = 1 if len(want) <= 2 else max(2, -(-len(want) * 3 // 10))     # a long message needs 30% of its terms
    body = one_line(text)
    hits = []
    for day, n, ln, _ in room_lines(s, room, now, rid, owner):
        if ln.mid.split(".e")[0] in (exclude or set()) or ln.body.strip() == body or mentions.OLD in ln.body:
            continue
        cover = len(want & {w[:5] for w in memindex.WORD.findall(memindex.norm(ln.body))})
        if cover >= need:
            hits.append({"day": day, "time": ln.time[:5], "role": ln.role, "who": ln.who, "text": ln.body,
                         "cite": f"daily/{day}.md:{n + 1}", "cover": cover})
    return sorted(hits, key=lambda h: (h["cover"], h["day"], h["time"]), reverse=True)[:limit]


def room_items(s: Settings, room: str, now: datetime | None = None,
               exclude: set[str] | None = None, rid: str | None = None, owner: bool = True) -> list[dict[str, Any]]:
    """The room's filed items of the last 14 days, newest first: every message its people sent (the owner,
    a listed person; its attachments with it) and every line that reads as a decision or a draft. The messages
    the turn is answering (`exclude`) are the prompt itself and stay out."""
    items: list[dict[str, Any]] = []
    found: list[dict[str, Any]] = []
    last_day = None
    for day, n, ln, lines in room_lines(s, room, now, rid, owner):
        if day != last_day:
            items += reversed(found)
            found, last_day = [], day
        if ln.mid.split(".e")[0] in (exclude or set()):
            continue
        if (ln.role not in (*PEOPLE_ROLES, NOTE) and not DECIDED.search(ln.body)) or mentions.OLD in ln.body:
            continue
        files = []
        for nxt in lines[n + 1:]:
            if not nxt.startswith("  - attachment: "):
                break
            files.append(nxt.strip()[2:])
        found.append({"day": day, "time": ln.time[:5], "role": ln.role, "who": ln.who, "text": ln.body,
                      "files": files, "cite": f"daily/{day}.md:{n + 1}"})
    items += reversed(found)
    return items


def room_pack(s: Settings, room: str, budget: int = 6000, now: datetime | None = None,
              exclude: set[str] | None = None, rid: str | None = None, owner: bool = True) -> tuple[str, int]:
    """(text, items shown): the recall pack's room part, newest first, cut to `budget` characters."""
    head = (f"[recall pack] Room {room}, last {WINDOW_DAYS} days of filed items, newest first: what the room "
            "was sent, decisions and drafts. Each ends with its file and line; read_memory gives the whole line.")
    out, used, shown = [head], len(head), 0
    items = room_items(s, room, now, exclude, rid, owner)
    for it in items:
        text = it["text"] if len(it["text"]) <= ITEM_CHARS else it["text"][:ITEM_CHARS - 1] + "…"
        extra = "".join(f" [{f}]" for f in it["files"])
        line = f"- {it['day'][5:]} {it['time']} {it['role']}: {text}{extra} ({it['cite']})"
        if used + 1 + len(line) > budget - 60:
            break
        out.append(line)
        used += 1 + len(line)
        shown += 1
    if shown < len(items):
        out.append(f"({len(items) - shown} older items not shown; recall or read_memory reaches them)")
    return ("\n".join(out), shown) if items else ("", 0)


def migrate_daylogs(daily: Path, tz: tzinfo, rooms: dict[str, tuple[str, str]], backup: Path) -> dict[str, Any]:
    """One-time: every day-log line written before offsets, ids, authors and classes is rewritten in
    today's shape, its offset resolved for its own day and time in `tz` (the repeated hour of a clock
    change takes the first offset, the only ambiguity the old shape left), its room's id and class from
    `rooms` (name -> (id, class); an unknown name keeps the name, class private). A file that changes
    is copied to `backup` first (a copy already there is kept); a line already in the new shape is
    left as it is, and a file with nothing to rewrite is left untouched. Each file is read and
    replaced under the lock capture holds, so a line the daemon files meanwhile waits for the rename
    and lands in the new file (cold read F3): the daemon runs this at its start, and `yerk memory
    migrate-daylogs` runs it by hand, beside a running daemon or not."""
    stats: dict[str, Any] = {"files": 0, "rewritten": 0, "kept": 0, "other": 0, "backup": str(backup)}
    for f in sorted(daily.glob("????-??-??.md")):
        with daily_lock(daily):
            text = f.read_text(errors="replace")
            day = date.fromisoformat(f.stem)
            out = []
            for raw in text.splitlines():
                if (ln := parse_line(raw)) is None:
                    stats["other"] += 1
                    out.append(raw)
                    continue
                if ln.offset and ln.cls:
                    stats["kept"] += 1
                    out.append(raw)
                    continue
                h, m_, sec = (int(x) for x in ln.time.split(":"))
                at = datetime(day.year, day.month, day.day, h, m_, sec, tzinfo=tz)
                rid, cls = rooms.get(ln.label, (ln.rid, "private")) if not ln.room_id else (ln.rid, ln.cls or "private")
                out.append(format_line(at, ln.label, rid, ln.role, ln.who, ln.body, ln.cls or cls, ln.mid))
                stats["rewritten"] += 1
            new = "\n".join(out) + ("\n" if text.endswith("\n") else "")
            if new != text:
                backup.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not (backup / f.name).exists():
                    write_private(backup / f.name, text)
                tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as w:
                    w.write(new)
                os.replace(tmp, f)
        stats["files"] += 1
    return stats


def on_file(s: Settings, rid: str = "", deadline_s: float = 1.5) -> tuple[Any, Any]:
    """(figure lookup, file lookup) for the shadow Stop check, each answering the cite or None. A day-log line room `rid`
    filed itself, its reply or its note, is not where a figure is on file: quoting it, the room quotes its own words."""
    from .evidence import has_figure

    def rows(sql: str, args: tuple[str, ...]) -> list[Any]:
        try:
            conn = memindex.connect(s.index_db, readonly=True, deadline=time.monotonic() + deadline_s)
            try:
                return conn.execute(sql, args).fetchall()
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return []

    def figure(fig: str) -> str | None:
        found = []                  # a fact file first, then the install's own tree, then a read-only root
        for path, lo, body, head in rows("SELECT c.path, c.lo, c.body, (SELECT h.body FROM chunks h WHERE h.path = c.path "
                                         "AND h.lo = c.lo ORDER BY h.id LIMIT 1) FROM chunks_fts JOIN chunks c ON c.id = "
                                         "chunks_fts.rowid WHERE chunks_fts MATCH ? LIMIT 60",
                                         ('"' + " ".join(re.findall(r"\d+", fig)) + '"',)):
            for n, line in enumerate(body.splitlines()):    # a line past CHUNK_MAX comes in pieces: the first names who wrote it
                x = parse_line(line if n else head.split("\n", 1)[0]) if rid and is_daylog(path) else None
                if has_figure(line, fig) and not (x and x.role in ("yerk", NOTE) and x.rid == rid):
                    p = Path(path)
                    rank = 0 if p.parent.name == "facts" else 1 if p.is_relative_to(s.root) else 2
                    found.append((rank, str(p.relative_to(s.root)) if p.is_relative_to(s.root) else path, lo + n))
                    break
        return f"{min(found)[1]}:{min(found)[2]}" if found else None

    def known(name: str) -> str | None:
        hit = rows("SELECT path FROM files WHERE path = ? OR path LIKE ? LIMIT 1", (name, "%/" + name.lstrip("./")))
        return cite(s, Path(hit[0][0])) if hit else None

    return figure, known


# the recall bench: a fixed question set, run against the index (L63)
def has_all(text: str, tokens: list[str]) -> bool:
    t = memindex.norm(text)
    return all(memindex.norm(tok) in t for tok in tokens)


def bench(s: Settings, questions: Path | None = None, k: int = 6) -> dict[str, Any]:
    """Each question's search; a hit is a result in the top k whose whole chunk holds every answer token
    and whose path is not a stale copy. Controls must hit, or the instrument is blind. `kept`: the hits
    whose snippet, as recall hands it to the room, still holds every answer token."""
    qfile = questions or s.bench
    data = json.loads(qfile.read_text())
    stale = re.compile(str(data.get("stale") or r"(^|/)overwrite-backups/"))
    rows = []
    for q in data["questions"]:
        weights: dict[str, float] = {}
        r = ranked(s, str(q["q"]), k, deadline_s=30.0, weights=weights)
        if not r["ok"]:
            return {"ok": False, "status": r["status"], "error": r["error"]}
        rank = next((i + 1 for i, h in enumerate(r["hits"])
                     if not stale.search(h["path"]) and has_all(h["text"], list(q["answer_tokens"]))), None)
        cut = snippets(r["hits"], weights, s.recall_chars) if rank else []
        rows.append({"id": q["id"], "control": bool(q.get("control")), "rank": rank,
                     "kept": has_all(cut[rank - 1]["text"], list(q["answer_tokens"])) if rank else None,
                     "top": r["hits"][0]["path"] if r["hits"] else None})
    scored = [r for r in rows if not r["control"]]
    n = len(scored) or 1
    top_k = sum(1 for r in scored if r["rank"])
    top_1 = sum(1 for r in scored if r["rank"] == 1)
    return {"ok": True, "questions": len(scored), "controls": len(rows) - len(scored),
            "blind": [r["id"] for r in rows if r["control"] and not r["rank"]],
            "top_k": top_k, "top_1": top_1, "k": k, "top_k_rate": round(top_k / n, 3), "top_1_rate": round(top_1 / n, 3),
            "kept": sum(1 for r in scored if r["kept"]), "lost": [r["id"] for r in scored if r["rank"] and not r["kept"]],
            "rows": rows, "file": str(qfile)}
