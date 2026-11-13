"""The replay: the owner's past threads put through the live writer and the live check, as of each burst of their texts (a cut),
each decision beside what went next. A thread is one file of <private>/imports/texts/ as sources.py writes it; each burst
of their texts on or after `since` is a cut, decided by `Writer._decide` and checked by `Check.check` on a scratch ledger.
The writer gets no tool, no place and no held part, and its memory part is the report room's search for their texts with
every hit dated at or after the cut taken out. Nothing is sent and nothing the running install uses is written: a run
writes only under <state>/texting/replay/, its scratch folder (removed after a clean run) and one JSONL file."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any, Callable

from ..accounts import Account, Cooldowns
from ..ledger import Ledger
from ..lifted.classify import FailureTable
from . import check, writer
from .people import Person, norm

HEAD = re.compile(r"^# Texts with .*\(([^()]*)\)\s*$")
MSG = re.compile(r"^- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (.*)$")
DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})")
OLD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (.+?) IN:")
BURST = 120.0               # seconds from a burst's first text to its last
OLD_BEFORE = 900.0          # an old agent's OUT counts from this long before the owner's line up to its own minute
SCORES = ("agree", "more", "less", "asks", "tapback", "auto", "open")


@dataclass
class Line:
    n: int                  # its line number in the file: `<person key>:<n>` is what a `thread:` source names
    at: datetime
    side: str               # them, owner, or auto (the old agent's, by its log)
    text: str


@dataclass
class Cut:
    key: str
    before: list[Line]      # the thread as it stood before the burst
    burst: list[Line]       # their texts
    went: list[Line]        # the owner's lines that followed, before their next line and within catch_up_hours
    open: bool              # no line of theirs follows and the file's last line is inside the answer window
    dropped: int = 0        # memory lines the as-of cut took out

    @property
    def at(self) -> datetime:
        return self.burst[-1].at


def handles(path: Path) -> set[str]:
    """The handles in a one-to-one thread file's first line, as people.py compares them; a group's file has none."""
    try:
        with open(path, errors="replace") as f:
            m = HEAD.match(f.readline().rstrip("\n"))
    except OSError:
        return set()
    return {norm(h) for h in m[1].split(",") if h.strip()} if m else set()


def find(folder: Path, p: Person) -> list[Path]:
    """Every thread file whose first line holds one of the person's handles: one is the person's thread, none or more is a skip."""
    return sorted(f for f in folder.rglob("*.md") if handles(f) & set(p.handles))


def read_thread(path: Path, owner: str, tz: tzinfo) -> list[Line]:
    """The message lines of a thread file (from line 4), the owner's by their label (the reply list's [owner] name), every
    other label theirs; a line that is no message's goes on the one above it."""
    out: list[Line] = []
    for n, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if n <= 3:
            continue
        if m := MSG.match(raw):
            rest, mine = m[2], bool(owner) and m[2].startswith(owner + ": ")
            out.append(Line(n, datetime.strptime(m[1], "%Y-%m-%d %H:%M").replace(tzinfo=tz), "owner" if mine else "them",
                            rest[len(owner) + 2:] if mine else rest.partition(": ")[2]))
        elif out:
            out[-1].text += "\n" + raw
    return out


def bursts(lines: list[Line]) -> list[tuple[int, int]]:
    """Each run of their lines with no owner's line between, the first to the last at most BURST apart: (start, end)."""
    out, i = [], 0
    while i < len(lines):
        if lines[i].side != "them":
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].side == "them" and (lines[j].at - lines[i].at).total_seconds() <= BURST:
            j += 1
        out.append((i, j))
        i = j
    return out


def cuts(key: str, lines: list[Line], since: date, limit: int, hours: float) -> list[Cut]:
    """Each burst whose last minute is on or after `since`, newest first, at most `limit`."""
    out = []
    for i, j in bursts(lines):
        last = lines[j - 1].at
        if last.date() < since:
            continue
        went = []
        for x in lines[j:]:
            if x.side == "them" or (x.at - last).total_seconds() > hours * 3600:
                break
            went.append(x)
        out.append(Cut(key, lines[max(0, i - writer.THREAD):i], lines[i:j], went,
                       all(x.side != "them" for x in lines[j:]) and (lines[-1].at - last).total_seconds() <= hours * 3600))
    return out[::-1][:max(0, limit)]


def flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def old_log(path: Path, tz: tzinfo) -> list[tuple[datetime, str, set[str]]]:
    """The old agent's log: (minute, first name, each text quoted after its last OUT:) a line; `OUT: -` quotes none."""
    out = []
    for raw in path.read_text(errors="replace").splitlines():
        if (m := OLD.match(raw)) and "OUT:" in raw:
            said = {flat(x) for x in re.findall(r'"((?:[^"\\]|\\.)*)"', raw.rpartition("OUT:")[2])}
            out.append((datetime.strptime(m[1], "%Y-%m-%d %H:%M").replace(tzinfo=tz), m[2].strip(), said))
    return out


def mark(lines: list[Line], log: list[tuple[datetime, str, set[str]]], first: str) -> None:
    """An owner's line is the old agent's when its log has an OUT for this first name, from OLD_BEFORE before the line up
    to its own minute, quoting the same text."""
    for x in lines:
        if x.side == "owner" and any(f == first and 0 <= (x.at - at).total_seconds() <= OLD_BEFORE and flat(x.text) in said
                                     for at, f, said in log):
            x.side = "auto"


def score(c: Cut, decision: str, parts: list[str]) -> str:
    """One word a cut (SCORES): what the decision would have done beside what went."""
    if decision == "react":
        return "tapback"
    if decision == "held" and not parts:
        return "asks"
    if c.went and all(x.side == "auto" for x in c.went):
        return "auto"
    if not c.went and c.open:
        return "open"
    return ("agree" if c.went else "more") if decision in ("reply", "held") else ("less" if c.went else "agree")


def as_of(hits: list[dict[str, Any]], at: datetime, root: Path, imports: Path, cache: dict[Path, datetime | None]) -> tuple[list[dict[str, Any]], int]:
    """J31: the hits a cut may see, and how many lines were taken out. A hit from a file under the texts import folder that
    holds a message line dated at or after the cut's minute goes whole; so does a hit from a file whose name begins with a
    date on or after the cut's day. A hit with no file in its cite leaves the whole part out."""
    if any(not str(h.get("cite") or "").rpartition(":")[0] for h in hits):
        return [], sum(len(str(h.get("text") or "").splitlines()) or 1 for h in hits)
    kept, dropped = [], 0
    for h in hits:
        f = Path(str(h["cite"]).rpartition(":")[0])
        f = (f if f.is_absolute() else root / f).resolve()
        if f.is_relative_to(imports):
            if f not in cache:
                try:
                    cache[f] = max((datetime.strptime(m[1], "%Y-%m-%d %H:%M").replace(tzinfo=at.tzinfo)
                                    for m in map(MSG.match, f.read_text(errors="replace").splitlines()) if m), default=None)
                except OSError:
                    cache[f] = at
            late = cache[f] is not None and cache[f] >= at          # type: ignore[operator]
        else:
            late = bool(d := DAY.match(f.name)) and d[1] >= f"{at:%Y-%m-%d}"     # type: ignore[index]
        if late:
            dropped += len(str(h.get("text") or "").splitlines()) or 1
        else:
            kept.append(h)
    return kept, dropped


CUT: ContextVar[Cut] = ContextVar("cut")


def rows(c: Cut) -> list[dict[str, Any]]:
    """The thread as it stood, in the shape of the link's rows: the lines before the burst and the burst (`Writer.last` keeps THREAD)."""
    return [{"guid": f"{c.key}:{x.n}", "is_from_me": x.side != "them", "text": x.text, "created_at": x.at.isoformat()}
            for x in c.before + c.burst]


async def decide(conf: dict[str, Any], file: Path, scratch: Path, report: str, todo: list[tuple[Cut, str]],
                 search: Callable[[str], list[dict[str, Any]]], root: Path, imports: Path, accounts: list[Account],
                 failures: FailureTable, model: str = "", claude: str = "") -> list[dict[str, Any]]:
    """Each cut (with the handle its thread was found by) one `deciding` row of a scratch ledger in `scratch`, decided by
    the live writer with no tool and, a reply or a tapback, checked by the live check; at most `parallel` cuts at once,
    cooldowns in memory only. The rows as they end."""
    led, cache = Ledger(scratch / "ledger.sqlite"), {}  # type: ignore[var-annotated]

    async def thread(key: str, limit: int) -> list[dict[str, Any]]:
        return rows(CUT.get())

    def recall(text: str) -> str:
        c = CUT.get()
        hits, c.dropped = as_of(search(text), c.at, root, imports.resolve(), cache)
        return "\n".join(f"{h['cite']} {h['text']}" for h in hits)

    w = writer.Writer(conf, file, scratch, led, report, thread, recall, lambda now: "", accounts, Cooldowns(), failures, model,
                      claude, lambda: CUT.get().at.timestamp(), tools=[])
    chk, slots = check.Check(conf, w), asyncio.Semaphore(w.parallel)

    async def one(c: Cut, handle: str) -> dict[str, Any]:
        CUT.set(c)
        async with slots:
            for x in c.burst:                       # each text arrived at its own minute: never late, never too old
                led.inbound_add(f"{c.key}:{x.n}", "texting", f"imessage:{c.key}", c.key, len(x.text), created=x.at.timestamp())
                led.inbound_set(f"{c.key}:{x.n}", received=x.at.timestamp())
            rid = led.texting_add(person=c.key, handle=handle, inbound=",".join(f"{c.key}:{x.n}" for x in c.burst),
                                  state="deciding", mode="dry")
            await w._decide(led.texting_get(rid) or {})
            r = led.texting_get(rid) or {}
            if r.get("decision") in ("reply", "react"):
                await chk.check(r)
                r = led.texting_get(rid) or {}
        return r
    return list(await asyncio.gather(*(one(c, h) for c, h in todo)))


def entry(c: Cut, r: dict[str, Any]) -> dict[str, Any]:
    """One line of the run's JSONL: the cut, the decision, the check's verdict, what went and the score."""
    parts = json.loads(r.get("parts") or "[]")
    return {"person": c.key, "at": f"{c.at:%Y-%m-%d %H:%M}", "id": f"{c.key}:{c.burst[0].n}", "texts": [x.text for x in c.burst],
            "decision": r.get("decision") or "", "parts": parts, "react": r.get("react") or "", "reply_to": r.get("reply_to") or "",
            "statements": json.loads(r.get("statements") or "[]"), "reason": r.get("reason") or "",
            "verdict": json.loads(r.get("verdict") or "null"), "state": r.get("state") or "",
            "went": [{"at": f"{x.at:%Y-%m-%d %H:%M}", "side": x.side, "text": x.text} for x in c.went],
            "score": score(c, str(r.get("decision") or ""), parts), "dropped": c.dropped, "seconds": r.get("seconds") or 0,
            "tokens": r.get("tokens") or 0, "account": r.get("account") or ""}


def write(folder: Path, stamp: str, head: dict[str, Any], lines: list[dict[str, Any]]) -> Path:
    """<state>/texting/replay/<stamp>.jsonl (0600): line 1 the run, then one line a cut."""
    f = folder / f"{stamp}.jsonl"
    with os.fdopen(os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
        out.write("".join(json.dumps(x) + "\n" for x in [head, *lines]))
    return f


def go(state: Path, stamp: str, head: dict[str, Any], conf: dict[str, Any], file: Path, report: str, todo: list[tuple[Cut, str]],
       search: Callable[[str], list[dict[str, Any]]], root: Path, imports: Path, accounts: list[Account], failures: FailureTable,
       model: str = "", claude: str = "") -> Path:
    """One run under <state>/texting/replay/ (0700): its scratch folder <stamp>/ for the ledger and the working folders, then
    <stamp>.jsonl; the scratch folder goes after a clean run and stays where it is when the run fails."""
    base = state / "texting" / "replay"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    (scratch := base / stamp).mkdir(mode=0o700)
    got = asyncio.run(decide(conf, file, scratch, report, todo, search, root, imports, accounts, failures, model, claude))
    f = write(base, stamp, head, [entry(c, r) for (c, _), r in zip(todo, got)])
    shutil.rmtree(scratch)
    return f
