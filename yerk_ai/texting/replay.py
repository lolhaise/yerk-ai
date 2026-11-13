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
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any, Callable

from .. import words
from ..accounts import Account, Cooldowns
from ..ledger import Ledger
from ..lifted.classify import FailureTable
from . import check, writer
from .people import Person, load, norm, sheet_file

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
    kept: int = 0           # memory hits that reached the writer
    then: int | None = None  # minutes to their next line when nothing of the owner's came between

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
                       all(x.side != "them" for x in lines[j:]) and (lines[-1].at - last).total_seconds() <= hours * 3600,
                       then=int((lines[j].at - last).total_seconds() // 60) if j < len(lines) and lines[j].side == "them" else None))
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


def located(name: str, cites: tuple[tuple[Path, str], ...], root: Path) -> Path:
    """The file a cite names, as memory.cite names one: its root's cite name, the longest that fits, then the path under it."""
    return next((r / name[len(n):].lstrip("/") for r, n in sorted(cites, key=lambda x: -len(x[1]))
                 if name == n or name.startswith(n + "/")), root / name)


def as_of(hits: list[dict[str, Any]], at: datetime, where: Callable[[str], Path], imports: Path) -> tuple[list[dict[str, Any]], int]:
    """J31: the hits a cut may see, and how many lines were taken out. A hit from a file under the texts import folder is
    kept only when every line of its text but a gap mark is a message line that begins with its own minute, before the
    cut's (a snippet's lines are picked, not a span, and a long one is cut around its match); a hit from a file whose name
    begins with a date on or after the cut's day goes whole, as does one from any file that holds a message line at or after
    the cut, and one whose cite names no file on disk. A hit with no file in its cite leaves the whole part out."""
    if any(not str(h.get("cite") or "").rpartition(":")[0] for h in hits):
        return [], sum(len(str(h.get("text") or "").splitlines()) or 1 for h in hits)
    kept, dropped, cut = [], 0, f"{at:%Y-%m-%d %H:%M}"
    for h in hits:
        f, text = where(str(h["cite"]).rpartition(":")[0]).resolve(), str(h.get("text") or "").splitlines()
        if not f.is_file():                     # a cite read back to no file: where it is from cannot be told, so it goes
            late = True
        elif f.is_relative_to(imports):
            late = any(x.strip() not in ("", "…") and not ((m := MSG.match(x)) and m[1] < cut) for x in text)
        else:
            late = bool((d := DAY.match(f.name)) and d[1] >= cut[:10]) or any((m := MSG.match(x)) and m[1] >= cut for x in text)
        if late:
            dropped += len(str(h.get("text") or "").splitlines()) or 1
        else:
            kept.append(h)
    return kept, dropped


CUT: ContextVar[Cut] = ContextVar("cut")


def unanswered(d: dict[str, Any]) -> bool:
    """A call no model answered (writer.hop's own holds: refused or timed out, or no account free): a failed cut, never a
    decision. A hold the code itself made names its rule, and one a model made carries its tokens."""
    why = str(d.get("reason") or "")
    return not d.get("tokens") and (why == words.TEXTING_NO_CLEAN or why.startswith(words.TEXTING_NO_ACCOUNT.partition("{")[0]))


def rows(c: Cut) -> list[dict[str, Any]]:
    """The thread as it stood, in the shape of the link's rows: the lines before the burst and the burst (`Writer.last` keeps THREAD)."""
    return [{"guid": f"{c.key}:{x.n}", "is_from_me": x.side != "them", "text": x.text, "created_at": x.at.isoformat()}
            for x in c.before + c.burst]


async def decide(conf: dict[str, Any], file: Path, scratch: Path, report: str, todo: list[tuple[Cut, str]],
                 search: Callable[[str], list[dict[str, Any]]], where: Callable[[str], Path], imports: Path, accounts: list[Account],
                 failures: FailureTable, add: Callable[[dict[str, Any]], None], model: str = "", claude: str = "") -> int:
    """Each cut (with the handle its thread was found by) one `deciding` row of a scratch ledger in `scratch`, decided by
    the live writer with no tool and, a reply or a tapback, checked by the live check; at most `parallel` cuts at once,
    cooldowns in memory only. Each cut's line goes to `add` as it ends; a cut that raises is written with its `error` (the
    exception's name and first line, never a text) and the run goes on. How many cuts raised."""
    led = Ledger(scratch / "ledger.sqlite")

    async def thread(key: str, limit: int) -> list[dict[str, Any]]:
        return rows(CUT.get())

    def recall(text: str) -> str:
        c = CUT.get()
        hits, c.dropped = as_of(search(text), c.at, where, imports.resolve())
        c.kept = len(hits)
        return "\n".join(f"{h['cite']} {h['text']}" for h in hits)

    w = writer.Writer(conf, file, scratch, led, report, thread, recall, lambda now: "", accounts, Cooldowns(), failures, model,
                      claude, lambda: CUT.get().at.timestamp(), tools=[])
    chk, slots = check.Check(conf, w), asyncio.Semaphore(w.parallel)

    async def one(c: Cut, handle: str) -> bool:
        CUT.set(c)
        rid, error = 0, ""
        async with slots:
            try:
                for x in c.burst:                   # each text arrived at its own minute: never late, never too old
                    led.inbound_add(f"{c.key}:{x.n}", "texting", f"imessage:{c.key}", c.key, len(x.text), created=x.at.timestamp())
                    led.inbound_set(f"{c.key}:{x.n}", received=x.at.timestamp())
                rid = led.texting_add(person=c.key, handle=handle, inbound=",".join(f"{c.key}:{x.n}" for x in c.burst),
                                      state="deciding", mode="dry")
                await w._decide(led.texting_get(rid) or {})
                if unanswered(r := led.texting_get(rid) or {}):
                    raise LookupError(words.REPLAY_UNANSWERED.format(why=r.get("reason")))
                if r.get("decision") in ("reply", "react"):
                    await chk.check(r)
                    if unanswered(v := json.loads((led.texting_get(rid) or {}).get("verdict") or "{}")):
                        raise LookupError(words.REPLAY_UNANSWERED.format(why=v.get("reason")))
            except Exception as e:
                error = f"{type(e).__name__}: {(str(e).splitlines() or [''])[0]}"
        line = entry(c, (led.texting_get(rid) or {}) if rid else {})
        add({**line, "score": "", "error": error} if error else line)
        return bool(error)
    return sum(await asyncio.gather(*(one(c, h) for c, h in todo)))


def entry(c: Cut, r: dict[str, Any]) -> dict[str, Any]:
    """One line of the run's JSONL: the cut, the decision, the check's verdict, what went and the score."""
    parts = json.loads(r.get("parts") or "[]")
    return {"person": c.key, "at": f"{c.at:%Y-%m-%d %H:%M}", "id": f"{c.key}:{c.burst[0].n}", "texts": [x.text for x in c.burst],
            "decision": r.get("decision") or "", "parts": parts, "react": r.get("react") or "", "reply_to": r.get("reply_to") or "",
            "statements": json.loads(r.get("statements") or "[]"), "reason": r.get("reason") or "",
            "verdict": json.loads(r.get("verdict") or "null"), "state": r.get("state") or "",
            "went": [{"at": f"{x.at:%Y-%m-%d %H:%M}", "side": x.side, "text": x.text} for x in c.went], "then": c.then,
            "score": score(c, str(r.get("decision") or ""), parts), "dropped": c.dropped, "kept": c.kept, "seconds": r.get("seconds") or 0,
            "tokens": r.get("tokens") or 0, "account": r.get("account") or ""}


def go(state: Path, stamp: str, head: dict[str, Any], conf: dict[str, Any], file: Path, report: str, todo: list[tuple[Cut, str]],
       search: Callable[[str], list[dict[str, Any]]], where: Callable[[str], Path], imports: Path, accounts: list[Account],
       failures: FailureTable, model: str = "", claude: str = "") -> Path:
    """One run under <state>/texting/replay/ (0700): <stamp>.jsonl (0600, refused if it is there) made at the start with
    line 1, the run, and one line added as each cut ends; its scratch folder <stamp>/ for the ledger and the working
    folders goes when no cut raised and stays where it is when one did, or when the run dies."""
    base = state / "texting" / "replay"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    f = base / f"{stamp}.jsonl"
    with os.fdopen(os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
        def add(line: dict[str, Any]) -> None:
            out.write(json.dumps(line) + "\n")
            out.flush()
        add(head)
        (scratch := base / stamp).mkdir(mode=0o700)
        failed = asyncio.run(decide(conf, file, scratch, report, todo, search, where, imports, accounts, failures, add, model, claude))
    if not failed:
        shutil.rmtree(scratch)
    return f


def plan(file: Path, folder: Path, since: date, limit: int, hours: float, tz: tzinfo, only: str = "",
         log: Path | None = None) -> tuple[list[tuple[Cut, str]], list[str]]:
    """A run's cuts, each with the handle its thread was found by, and a line a person: the thread file found, by its name,
    and its cuts, or why they are skipped (no sheet, no file, two or more: never a guess). Everyone on the reply list, or the
    one `only` names. Nothing is called and nothing is searched."""
    who, marks, todo = load(file, sheets=False), old_log(log, tz) if log else [], list[tuple[Cut, str]]()
    said = [] if not only or only in who.people else [words.TEXTING_NO_ONE.format(key=only, keys=", ".join(who.people))]
    for p in (p for p in who.people.values() if not only or p.key == only):
        found = find(folder, p)
        why = (words.REPLAY_NO_SHEET if not sheet_file(file, p).is_file() else words.REPLAY_NO_FILE if not found
               else words.REPLAY_FILES if len(found) > 1 else "")
        if why:
            said.append(why.format(key=p.key, n=len(found), files=", ".join(f.name for f in found)))
            continue
        mark(lines := read_thread(found[0], who.owner, tz), marks, p.first)
        cs = cuts(p.key, lines, since, limit, hours)
        todo += [(c, next(h for h in p.handles if h in handles(found[0]))) for c in cs]
        said.append(words.REPLAY_FOUND.format(key=p.key, file=found[0].name, cuts=len(cs)))
    return todo, said


def table(lines: list[dict[str, Any]]) -> list[str]:
    """The run's counts from its file's cut lines, a row a person and the total (words.REPLAY_COLUMNS): never a text or a
    draft. A cut that raised counts as cuts and failed only; seconds and tokens are the writer's and the check's."""
    per: dict[str, Counter[str]] = {}
    for x in lines:
        n, v = per.setdefault(x["person"], Counter()), x.get("verdict") or {}
        n["cuts"] += 1
        if x.get("error"):
            n["failed"] += 1
            continue
        n.update([x["decision"], x["score"], *(["check-ok" if v.get("release") else "check-held"] if v else [])])
        n["seconds"] += round(float(x.get("seconds") or 0) + float(v.get("seconds") or 0))
        n["tokens"] += int(x.get("tokens") or 0) + int(v.get("tokens") or 0)
    rows, w = [*per.items(), (words.REPLAY_TOTAL, sum(per.values(), Counter[str]()))], max(len(k) for k in [*per, words.REPLAY_TOTAL])
    return [" ".join([" " * w, *words.REPLAY_COLUMNS]),
            *(" ".join([k.ljust(w), *(str(c[col]).rjust(len(col)) for col in words.REPLAY_COLUMNS)]) for k, c in rows)]
