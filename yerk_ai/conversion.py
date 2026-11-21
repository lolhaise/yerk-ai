"""`yerk curate --lessons`: rewrites the rules files as lesson files, each a one-line summary, then its reason and examples.
It turns an existing rules.md and rules-topics.md into lesson files and their index (lessons.py), as prompting-fable-5-2 says of
rules that built up: "rewrite absolute rules with their scope and reason", with the originals copied first. The dry run writes one
proposals file, <memory>/rules-proposals/lessons-<day>.md, and changes nothing else: for each original rule by its cite, its words,
the lessons made of it with each one's gate (the filing's own, memory.unshaped, on READ), and each thing done to it as a listed item;
block at its end is what an apply reads. Meaning never changes: a rule already in the shape is split as it stands (its first
sentence the summary); one out of it goes to curate's model pass ("Lessons", then "Knock down lessons" in curate.md); code alone
cites a quote or an example on the day-log line it means (`pick`) and never adds an exception the rule does not give, and checks
that every sentence of the original lands in a lesson or is listed, each example in the lesson the model names, the rule's caution
in a lesson word for word. The frozen trigger test refuses a split that loses an owner message of the last 14 days that
loads its rule today. Apply takes that exact file and refuses unless every lesson passes the gate, every original is accounted
for, the trigger test passes and the store is byte for byte the one the dry run read; it copies the originals to
<memory>/rules-backup-<day>/ first, and undo puts them back."""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import curate, lessons, memory, shape
from .config import budgets, load_toml, write_private
from .mentions import OLD

FILE = "lessons-{day}.md"          # in <memory>/rules-proposals/
APPLIED = "applied.json"           # in the backup folder: what an apply wrote, each file's sha256, for its undo
DAYS = 14                          # the trigger test's window of owner messages
OWNER = ("owner", "test-webhook")  # whose messages the trigger test reads (the webhook stands in for the owner)
WORD = re.compile(r"[\w'’-]+")      # a sentence is accounted for by its words, the punctuation a cited quote left aside
STOPS = (".", "!", "?")
BRACKET = re.compile(r"\([^()\n]*\)")  # a bracket, innermost first: the accounting never ends a sentence inside one ("(Ex. B)")
HELD = re.compile("\x02(\\d+)\x03([.!?]?)")  # a held bracket, its own last stop written after it so a sentence still ends there
LIFTED = "an absolute rule missing its scope or exception: "    # the gate's absolute finding (words.LESSON_NAMES), its head
SCOPED = 'It holds for the job "{job}".'   # the scope code writes into an absolute that has none, from the rule's job
UNLIFTED = "no exception, as the rule gives none"   # a listed item: a lesson whose absolute has no exception since its rule has none
FINDINGS = "findings.tsv"          # in <memory>/rules-proposals/: cite<TAB>a finding of the owner's side on an earlier conversion
NAMED = re.compile(r"\b(?:(\d{4})-(\d{2})-(\d{2})|(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{1,2})(?:,? (\d{4}))?)"
                   r"(?:,? (?:at )?(\d{1,2}):(\d)([\dx]))?\b", re.I)        # a day a passage names, and its time: Nov 9 2026 14:05, Oct 28
HIS = re.compile(r"(?:\its(?:turn|and|the|last part)(?:['’]closes)?\the(?:\Case+\before+){0,3}|\the example note)\Says*$", re.I)    # the queue for its
CLOCK = re.compile(r"^- (\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}) ")
STAMPED = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}(?::\d{2})?)([+-]\d{2}:\d{2})?")
SHAPE = re.compile(r"^\s*(?:landed|bounced)\s+shape\b", re.I)     # an example that describes a shape, never a message of the log
HEAD = re.compile(r"^\s*(?:landed|bounced)\b[^:\"“]*:?\s*", re.I)  # an example's mark and the day it names, before its words
ELIDED = re.compile(r"\s*(?:\.\.\.|…)\s*")
LOGS: dict[Path, tuple[tuple[int, int], list[tuple[str, str, datetime | None]]]] = {}   # each day log as the picker read it
ROUNDS = 2                         # the times a rule still failing goes back to the model with its findings, after its first
CHUNK = 8                          # rules a call of the first proposal, a knock-down or a round back reads; the calls run side by
                                   # side (16 to a call ran 9 minutes on a store of the owner's size; a round's rules in one call, 11)
REASONS = "reasons.tsv"            # in <memory>/rules-proposals/: cite, the daily/<day>.md:<line> holding it, the reason, a row
# The conversion's pages: the ones every room reads and the Claude Fable 5.2 page, whatever model the rooms run that minute,
# since the owner's order of 2026-11-09 names that page; the doctor's daily run follows the rooms (shape.run_by).
READ = ("claude-fable-5-2",)
WANTING = ("no reason given", "a negative with no why", "a hard style limit with no reason")   # what a reason on file would meet
WRAP = re.compile(r"\s*</?examples(?:\s[^>]*)?>", re.I)    # the wrapper left round a rule's examples once code takes them out
DROPPED = "the reviewer dropped it: "   # a finding of the knock-down: its proposal says other than the rule says
KEPT = "kept from an earlier file"      # a listed item: a rule `--from FILE` kept word for word, with no model call
NONE = "none"                           # REASONS's `cite<TAB>none`: the owner's side looked, and no file holds a reason for it
LISTED = "no reason on file, as the owner's side says"   # a listed item: a lesson of a rule with a NONE row refused for a reason alone


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def flat(text: str) -> str:
    return " ".join(shape.flat(text).lower().split())


def originals(s: memory.Settings) -> list[dict[str, Any]]:
    """Each live rule of RULES and TOPIC_RULES, by its cite: its line, job, words (trailer and marks out), rooms, triggers, filing
    and class (the file's: rules.md shared, the topic rules private unless their front matter says)."""
    out = []
    for name in (memory.RULES, memory.TOPIC_RULES):
        p = s.root / name
        rows = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []
        for n, job, said in shape.lessons_of(p):
            raw, tm = rows[n - 1], memory.TRAILER.search(rows[n - 1])
            out.append({"cite": f"{memory.cite(s, p)}:{n}", "file": name, "line": n, "text": raw, "job": job.strip().lower(),
                        "said": said.strip(), "rooms": sorted(memory.scope(raw) or ()), "on": memory.picked(raw),
                        "filed": f"{tm[1]}{tm[2] or ''}" if tm else "", "cls": memory.file_class(p)[0]})
    return out


def ends(s: memory.Settings) -> dict[str, str]:
    """Each line of the store, live or replaced, by cite -> the cite of the live rule its replacements end at (by its mark, else
    LATER), lessons.HOPS at most and a cite met twice ending it; a line that ends nowhere live (emptied, a note) is none."""
    rows = {f"{memory.cite(s, s.root / n)}:{i}": x for n in (memory.RULES, memory.TOPIC_RULES) if (s.root / n).is_file()
            for i, x in enumerate((s.root / n).read_text(encoding="utf-8", errors="replace").splitlines(), 1) if x.startswith("- ")}
    kept, out = memory.later(s), dict[str, str]()
    for c in rows:
        at, been = c, set[str]()
        while at in rows and at not in been and len(been) < lessons.HOPS:
            been.add(at)
            if OLD not in rows[at]:
                out[c] = at
                break
            at = (memory.BY.findall(rows[at]) or [kept.get(at, "")])[0]
    return out


def owner_messages(s: memory.Settings, now: float, days: int = DAYS) -> list[tuple[str, str]]:
    """(cite, words) of each owner message of the last `days` day logs, the trigger test's frozen set."""
    first = datetime.fromtimestamp(now - days * 86400, s.tz).date().isoformat()
    out = []
    for p in sorted(s.daily.glob("*.md")) if s.daily.is_dir() else []:
        if memory.DAYLOG.search(p.name) and p.stem >= first:
            for i, raw in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if (ln := memory.parse_line(raw)) and ln.role in OWNER:
                    out.append((f"daily/{p.name}:{i}", ln.body))
    return out


def filed_at(filed: str) -> datetime | None:
    """When the rule was filed, from its trailer's time; None for a rule with none."""
    m = STAMPED.search(filed or "")
    return datetime.fromisoformat(f"{m[1]}T{m[2]}{m[3] or '+00:00'}") if m else None


def named(text: str, filed: datetime | None) -> tuple[str, int | None]:
    """The day a passage names outside its quotes (Nov 9 2026 14:05, Oct 28, 2026-11-09), as YYYY-MM-DD, a year it leaves out the
    last one not after the filing; and its time as minutes of the day (14:4x as 14:45), or None. ("", None) when it names none."""
    if not (m := NAMED.search(shape.QUOTED.sub(" ", text))):
        return "", None
    if m[1]:
        y, mon, d = int(m[1]), int(m[2]), int(m[3])
    else:
        mon, d = "jan feb mar apr may jun jul aug sep oct nov dec".split().index(m[4][:3].lower()) + 1, int(m[5])
        y = int(m[6]) if m[6] else (filed or datetime.now()).year - bool(filed and (mon, d) > (filed.month, filed.day))
    try:
        day = datetime(y, mon, d).date().isoformat()
    except ValueError:
        return "", None
    return day, (int(m[7]) * 60 + int(m[8]) * 10 + (5 if m[9].lower() == "x" else int(m[9]))) if m[7] else None


def log_lines(p: Path) -> list[tuple[str, str, datetime | None]]:
    """A day log's lines as the picker reads them: each one's words flattened, its speaker's role and its time; read once."""
    st = p.stat()
    if (have := LOGS.get(p)) and have[0] == (st.st_mtime_ns, st.st_size):
        return have[1]
    rows = []
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln, at = memory.parse_line(raw), CLOCK.match(raw)
        rows.append((flat(ln.body if ln else raw), ln.role if ln else "", datetime.fromisoformat(f"{p.stem}T{at[1]}") if at else None))
    LOGS[p] = ((st.st_mtime_ns, st.st_size), rows)
    return rows


def before(text: str, filed: datetime | None) -> tuple[str, int | None]:
    """The day and time the rule names last before a quote of it: its last day named (outside quotes) and the last time its
    clause names (13:25, on a line ...), as `named` gives them; no time without a day."""
    bare = shape.QUOTED.sub(lambda m: " " * len(m[0]), text)
    if not (days := list(NAMED.finditer(bare))):
        return "", None
    day, near = named(days[-1][0], filed)
    times = re.findall(r"\b(\d{1,2}):(\d)([\dx])\b", re.split(r"[;.!?]\s", bare)[-1])
    return day, (int(times[-1][0]) * 60 + int(times[-1][1]) * 10 + (5 if times[-1][2] == "x" else int(times[-1][2]))) if times else near


def pick(words: str, daily: Path, filed: str = "", his: bool = False, when: str = "", place: tuple[str, int | None] | None = None) -> str:
    """The day-log line a passage means, as daily/<day>.md:<line>; "" when no line passes. Of the lines that hold its words:
    never one written after the rule was filed (its trailer's time); on the day `when` names, when it names one, and with a time
    the line nearest it; for words given as the owner's, a line of their own; else the earliest. shape.on_record, the gate's, is not this."""
    want, cut = flat(words), filed_at(filed)
    day, near = place or named(when, cut)
    best: tuple[int, str, int] | None = None
    for p in sorted(daily.glob("*.md")) if want and daily.is_dir() else ():
        if not memory.DAYLOG.search(p.name) or (day and p.stem != day) or (cut and p.stem > cut.date().isoformat()):
            continue
        for i, (text, role, at) in enumerate(log_lines(p), 1):
            if want not in text or (his and role not in OWNER) or (cut and at and at > cut):
                continue
            if near is None:
                return f"daily/{p.name}:{i}"
            gap = abs(at.hour * 60 + at.minute - near) if at else 10 ** 6
            best = (gap, p.name, i) if best is None or gap < best[0] else best
    return f"daily/{best[1]}:{best[2]}" if best else ""


def given_his(before: str) -> bool:
    """Next run starts the: example, note, says, the sample item moved to the next step and the test reads it, back "as plain text"."""
    return bool(HIS.search(re.split(r"[.!?]\s", before)[-1][-48:]))


def quoted(rule: dict[str, Any], made: list[dict[str, Any]], daily: Path, most: int, also: list[str] | None = None) -> tuple[list[dict[str, str]], list[str]]:
    """The rule's quotes in its lessons, never swapped for a pointer: each quoted run of a lesson must be a run of a quote of the
    rule (a fault otherwise), and a quote of the rule past `most` words that a lesson keeps a run of is a listed item with the
    line `pick` finds for it (its words given as his when the rule gives them so, the day and time the rule names before it), or kept.
    `also`: the reasons on file for it (REASONS), his words too, a run of which a lesson may quote."""
    said = rule["said"]
    theirs = [(m[1], m.start()) for m in shape.QUOTED.finditer(said)] + [(x, len(said)) for x in also or []]
    done, faults, seen_ = list[dict[str, str]](), list[str](), set[str]()
    for i, x in enumerate(made, 1):
        for m in shape.QUOTED.finditer(shape.EXAMPLE_TAGS.sub(" ", f"{x['summary']} {x['body']}").replace(SCOPED.format(job=rule["job"]), " ")):
            parts = [flat(y).strip(" .,;:!?") for y in ELIDED.split(m[1]) if y.strip(" .,;:!?")]
            of = [(q, at) for q, at in theirs if parts and all(y in flat(q) for y in parts)]
            if not of:
                faults.append(f'lesson {i} quotes words that are no run of a quote of the rule: "{m[1]}"')
                continue
            q, at = of[0]
            if len(q.split()) > most and q not in seen_ and at < len(said):
                seen_.add(q)
                line = pick(q, daily, rule.get("filed", ""), given_his(said[:at]), place=before(said[:at], filed_at(rule.get("filed", ""))))
                done.append({"kind": "quote cited", "said": q, "as": m[1], "at": line} if line else {
                    "kind": "quote kept", "said": q, "as": m[1], "why": "no day-log line passes: none before the rule was filed, on the day "
                    "it names, of his own where the words are his"})
    return done, faults


def absolute(job: str, text: str, read: Any) -> tuple[str, list[dict[str, str]]]:
    """An absolute with no scope written in: its scope from its own job, a listed item. No exception is ever added: an absolute
    whose rule gives none is carried as they wrote it and listed for the owner's side (unlifted)."""
    miss = shape.lesson(job, text, None, {}, read=read).get("absolute")
    if not miss or "no scope" not in miss[0]:
        return text, []
    said = SCOPED.format(job=job)
    return f"{text} {said}", [{"kind": "scope added", "said": said, "from": "its job"}]


def unlifted(rule: dict[str, Any], text: str, read: Any) -> bool:
    """A lesson's absolute with no exception because its rule has none: the rule's own absolute has no exception written in and
    every absolute word of the lesson is one the rule says. A lesson missing an exception its rule gives is not this."""
    miss = shape.lesson(rule["job"], rule["said"], None, {}, read=read).get("absolute") or []

    def strong(said: str) -> set[str]:
        return {w.lower() for w in shape.ABSOLUTE.findall(shape.QUOTED.sub(" ", shape.EXAMPLE_TAGS.sub(" ", said)))}
    return any("no exception" in x for x in miss) and strong(text) <= strong(rule["said"])


def open_finding(g: str) -> bool:
    """The gate's absolute finding for an exception alone, its scope written in."""
    return g.startswith(LIFTED) and "with no exception written in" in g.split(";")[0] and "no scope" not in g.split(";")[0]


def accounted(rule: dict[str, Any], made: list[dict[str, Any]], listed: list[dict[str, str]]) -> list[str]:
    """The original's sentences that land in no lesson (word for word, a cited quote aside) and are not listed as left out or
    rewritten: none, when every sentence is accounted for."""
    have = flat(" ".join(f"{x['summary']} {x['body']}" for x in made))

    def uncited(text: str) -> str:      # a cited quote is accounted for by its cite, a quote across two sentences too
        for x in listed:
            text = text.replace(x["said"], " ") if x["kind"] == "quote cited" else text
        return text
    said = [y for y in {flat(uncited(x["said"])) for x in listed if x["kind"] in ("left out", "rewritten")} if y.strip(" .;:,")]
    out, text = [], uncited(WRAP.sub(" ", shape.EXAMPLE_TAGS.sub(" ", rule["said"])))   # an example is code's, each listed
    held = [m[0] for m in shape.QUOTED.finditer(text)]       # a quote is never split at a sentence end inside it
    text = shape.QUOTED.sub(lambda m: f"\u201c\x00{held.index(m[0])}\x00\u201d", text)
    brackets: list[str] = []                                 # nor a bracket at an abbreviation in it, a bracket in it held first

    def hold(m: re.Match[str]) -> str:
        brackets.append(m[0])
        return f"\x02{len(brackets) - 1}\x03{m[0][-2] if m[0][-2:-1] in STOPS else ''}"
    while (n := BRACKET.subn(hold, text))[1]:
        text = n[0]
    for sent in shape.SENTENCES.split(text):
        while HELD.search(sent):
            sent = HELD.sub(lambda m: brackets[int(m[1])] + ("" if brackets[int(m[1])][-2:-1] in STOPS else m[2]), sent)
        sent = re.sub("\u201c\x00(\\d+)\x00\u201d", lambda m: held[int(m[1])], sent)
        rest = flat(sent)
        cut = {i for y in said for m in re.finditer(re.escape(y), rest) for i in range(m.start(), m.end())}
        rest = "".join(" " if i in cut else c for i, c in enumerate(rest))    # each part the model listed, found in the
        bare = flat(shape.MARKED.sub(" ", shape.QUOTED.sub(" ", rest)))      # sentence as it stands (the parts may overlap); a
        # mark (landed, bounced) is the store's, never a lesson's: the prompt asks it written nowhere
        if rest.strip(" .;:,") and flat(sent) not in have and not set(WORD.findall(bare)) <= set(WORD.findall(have)):
            out.append(sent.strip())
    return out


def examples(rule: dict[str, Any], daily: Path) -> list[dict[str, str]]:
    """Each <example> of the original, in the rule's order, in the cited form the gate takes when `pick` finds the line it means:
    its words after its mark and day (an example given as his, on a line of his own), else each quoted piece of it in order (a
    piece under four words only on the day it names); one no line passes, or that describes a shape, keeps its words. Code's."""
    out, most = [], shape.threshold(dict(budgets().get("shape") or {}), *shape.GLOSS)
    for ex in shape.INSIDE.findall(rule["said"]):
        mark, text = (m[0].capitalize() + ", ") if (m := shape.MARKED.search(ex)) else "", ex.strip()
        bare = HEAD.sub("", text).strip(shape.QUOTES_ALL + ",:;.")
        day = named(text, filed_at(rule.get("filed", "")))[0]
        pieces = [(y, his) for y, his in [(bare, False), *((y, given_his(text[:m.start()])) for m in shape.QUOTED.finditer(text)
                                                         for y in ELIDED.split(m[1]))] if len(y.split()) >= 4 or (day and y.strip())]
        if (m := shape.SHOWN.match(text)) and len(m[2].split()) > most:      # cited, its gloss past the gate's room
            out.append({"kind": "example cited", "said": text, "as": f"<example>{mark}{m[1]}</example>",
                        "why": f"its gloss of {len(m[2].split())} words left out, since the gate takes {most} at most beside a cite"})
        elif shape.SHOWN.match(text) or not bare:
            out.append({"kind": "example cited", "said": text, "as": f"<example>{text}</example>"})
        elif not SHAPE.match(text) and (at := next((x for q, his in pieces if (x := pick(q, daily, rule.get("filed", ""), his, text))), "")):
            out.append({"kind": "example cited", "said": text, "at": at, "as": f"<example>{mark}{at}</example>"})
        else:
            out.append({"kind": "example kept", "said": text, "as": f"<example>{text}</example>", "why": "a shape, not a message"
                        if SHAPE.match(text) else "no day-log line passes: none before the rule was filed holds it on the day it names"})
    return out


def reason_of(s: memory.Settings, rule: dict[str, Any], why: dict[str, Any], supplied: dict[str, list[tuple[str, str]]]) -> dict[str, str]:
    """A proposed reason as a listed item: added when the rule's own words or the day-log line it names hold it word for word,
    supplied when it is the owner's side's for this cite (REASONS), else refused; a reason is never invented."""
    said, src = str(why.get("said") or ""), str(why.get("from") or "")
    if mine := next((x for x in supplied.get(rule["cite"], []) if flat(said) and flat(said) in flat(x[1])), None):
        return {"kind": "reason supplied", "said": said, "from": f"{mine[0]}, named in {curate.PROPOSALS}/{REASONS}"}
    line = (s.daily / src.removeprefix("daily/").rpartition(":")[0]) if src.startswith("daily/") else None
    n = int(src.rpartition(":")[2]) if line and src.rpartition(":")[2].isdigit() else 0
    rows = line.read_text(errors="replace").splitlines() if line and line.is_file() else []
    held = flat(rule["said"]) if not line else flat(rows[n - 1]) if 0 < n <= len(rows) else ""
    return ({"kind": "reason added", "said": said, "from": src or "the rule's own words"} if flat(said) and flat(said) in held
            else {"kind": "reason refused", "said": said, "why": "neither the rule's own words nor the line it names hold it"})


def supplied_reasons(s: memory.Settings) -> dict[str, list[tuple[str, str]]]:
    """cite -> each (its day-log line, the reason) row of <memory>/rules-proposals/REASONS that the line it names holds word for
    word (`cite<TAB>daily/<day>.md:<line><TAB>reason`, # a comment), the owner's side's reason for a rule with none on file, a
    row a reason, so a rule split in parts may have one for each part that has none."""
    p, out = s.root / curate.PROPOSALS / REASONS, dict[str, list[tuple[str, str]]]()
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []:
        cite, at, why = ([*raw.split("\t"), "", ""])[:3]
        f, _, n = at.strip().removeprefix("daily/").rpartition(":")
        rows = (s.daily / f).read_text(errors="replace").splitlines() if f and (s.daily / f).is_file() else []
        if not raw.startswith("#") and why.strip() and n.isdigit() and 0 < int(n) <= len(rows) and flat(why) in flat(rows[int(n) - 1]):
            out.setdefault(cite.strip(), []).append((at.strip(), why.strip()))
    return out


def reason_rows(s: memory.Settings) -> dict[str, list[str]]:
    """cite -> each row of REASONS for it as written, its fields after the cite (a `none` row too), comments aside: kept in the
    record, so a dry run from an earlier file can tell a rule whose rows changed since."""
    p, out = s.root / curate.PROPOSALS / REASONS, dict[str, list[str]]()
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []:
        if raw.strip() and not raw.startswith("#"):
            out.setdefault(raw.partition("\t")[0].strip(), []).append(raw.partition("\t")[2].strip())
    return out


STALE = ("a findings row", "an exception added", "a quote swapped for a pointer", "an example cited by the old search",
         "an example left out for length", "a fault the code finds", "another cause")    # why `--from` redoes a rule, the head's count
LOOPED = "the owner's side's finding comes back"   # a listed item: a rule redone with its findings shown and still dropped, kept


def finding_rows(s: memory.Settings) -> dict[str, list[str]]:
    """cite -> each finding of <memory>/rules-proposals/FINDINGS for it, word for word (`cite<TAB>the finding's words`, # a comment):
    what the owner's side found reading an earlier conversion of that rule, so `--from` redoes it and the model is shown them."""
    p, out = s.root / curate.PROPOSALS / FINDINGS, dict[str, list[str]]()
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []:
        cite, _, said = raw.partition("\t")
        if not raw.startswith("#") and cite.strip() and said.strip():
            out.setdefault(cite.strip(), []).append(said.strip())
    return out


def stale(s: memory.Settings, r: dict[str, Any], m: dict[str, Any], found: dict[str, list[str]]) -> list[str]:
    """What an earlier file's conversion of a rule holds that the code no longer writes, or a findings row it was made before."""
    shown_, text = examples(r, s.daily), " ".join(f"{x['summary']} {x['body']}" for x in m["lessons"])
    was = {d.get("said"): d for d in m["done"] if str(d["kind"]).startswith("example")}
    most = shape.threshold(dict(budgets().get("shape") or {}), "lesson_quote_words", 8)
    return [y for y, hit in zip(STALE, (
        bool(found.get(r["cite"])) and m.get("found") != found[r["cite"]],
        any(d["kind"] == "exception added" for d in m["done"]),
        any(x not in r["text"] for x in re.findall(r"the words at daily/\S+", text)),
        any((was.get(x["said"]) or {}).get("kind") == "example cited" and (was[x["said"]].get("at"), was[x["said"]]["as"]) != (x.get("at"), x["as"])
            for x in shown_) or any(x not in {y["said"] for y in shown_} for x in was if (was[x] or {}).get("kind") == "example cited"),
        any(d["kind"] == "example left out" and "would pass its length" in str(d.get("why")) for d in m["done"]),
        bool(checks(r, m["lessons"], m["done"], shown_) or quoted(r, m["lessons"], s.daily, most, [w for _, w in supplied_reasons(s).get(r["cite"], [])])[1]))) if hit]


def carried(s: memory.Settings, rules: list[dict[str, Any]], path: Path, gate: Callable[[dict[str, Any]], list[str]],
            rows: dict[str, list[str]], found: dict[str, list[str]] | None = None, read: Any = None,
            why: dict[str, list[str]] | None = None) -> dict[str, dict[str, Any]]:
    """cite -> the rule as the earlier proposals file `path` made it (a dry run's, from any folder or store), kept word for word
    with no model call: when that file has it under the same cite with the same words, each lesson of it passes the gate as an
    apply reads it today (`gate`), every sentence is accounted for, it lost no trigger, nothing of it was still failing after the
    last round (the reviewer's drop among them), no slug of it is retired, REASONS holds the same rows for its cite as then and
    it holds nothing `stale` names. A rule FINDINGS has rows for that was redone with them shown and still came back dropped is
    kept and listed, never redone again. `why`, filled: cite -> what made each other rule be redone (STALE)."""
    rec, gone, found, why = loaded(path), set(lessons.retired(s.root)), found or {}, {} if why is None else why
    old, out = {m["cite"]: m for m in rec["rules"]}, dict[str, dict[str, Any]]()
    for r in rules:
        m = old.get(r["cite"])
        if not m or m["text"] != r["text"] or m.get("reasons") != rows.get(r["cite"], []) or r["cite"] in rec["lost"] or any(
                x["slug"] in gone for x in m["lessons"]):
            why[r["cite"]] = [STALE[-1]]
            continue
        made = [{**x, "gate": gate(x)} for x in m["lessons"]]
        bad = stale(s, r, m, found)
        looped = bool(found.get(r["cite"])) and m.get("found") == found[r["cite"]] and any(   # the reviewer: a finding of theirs holds
            d["kind"] == "reviewer dropped" and re.search(r"\bfindings?\b", str(d.get("why"))) for d in m["done"])
        if not made or any(refused(x["gate"], NONE in rows.get(r["cite"], []), unlifted(r, f"{x['summary']} {x['body']}", read)) for x in made) \
                or accounted(r, made, m["done"]) or (any(d["kind"] in ("still failing", "reviewer dropped") for d in m["done"]) and not looped):
            bad = bad or [STALE[-1]]
        if bad:
            why[r["cite"]] = bad
            continue
        out[r["cite"]] = {**m, "lessons": made, "missing": [], "faults": [], "done": [*(d for d in m["done"] if d["kind"] not in (KEPT, LOOPED)), {
            "kind": KEPT, "why": f"{path.name}, as it made it: its lessons word for word, no model call"}, *([{"kind": LOOPED, "why": "redone with "
            "the owner's side's findings shown and dropped again: listed for the owner's side, not redone again"}] if looped else [])]}
    return out


def keeps(line: str, weights: tuple[dict[str, float], float]) -> list[str]:
    """The words a rule loads on today as the `on` words each lesson made of it keeps (on_topic weighs an `on` phrase held as a
    word only one rule says, so it always loads): each smallest set of its trigger words whose weight among the live rules
    reaches memory.RIDE, a set one phrase, three words at most. A rule a set of more words could load keeps each trigger word
    alone, which loads more often but never less, so a message that loads it today always loads a lesson made of it."""
    weight, one = weights
    tr, picked, ride = memory.triggers(line), {memory.wants(t) for t in memory.picked(line)}, memory.RIDE * weights[1]
    w = {k: one if k in picked else weight.get(k, one) for k in tr}
    light = sorted((k for k in w if w[k] < ride), key=lambda k: w[k])
    if next((n for n in range(1, len(light) + 1) if sum(w[k] for k in light[:n]) >= ride), 0) > 3:
        return list(dict.fromkeys(tr.values()))
    out: list[tuple[str, ...]] = [(k,) for k in w if w[k] >= ride]
    for c in (c for n in (2, 3) for c in itertools.combinations(sorted(light, key=lambda k: -w[k]), n)):
        out += [c] if sum(w[k] for k in c) >= ride and not any(set(o) <= set(c) for o in out) else []
    return [" ".join(tr[k] for k in c) for c in out]


def build(s: memory.Settings, rule: dict[str, Any], prop: dict[str, Any] | None, read: Any, seen: dict[Path, str],
          taken: set[str], acronyms: frozenset[str] | None, supplied: dict[str, list[tuple[str, str]]] | None = None) -> dict[str, Any]:
    """The lessons made of one rule, each with its gate, and what was done to it. Each lesson keeps the words its rule loads on
    (keeps; else memory.triggers' every word: its job's, its picked ones, what its words name) as its `on`, so no split loses a
    trigger. Each example of the rule goes, in the cited form or as its words, into the lesson the model names for it (a rule
    made one lesson: that one), never counted against the body's length; `faults`, what code finds wrong (checks)."""
    most, supplied = shape.threshold(dict(budgets().get("shape") or {}), "lesson_quote_words", 8), supplied or {}
    gloss = shape.threshold(dict(budgets().get("shape") or {}), *shape.GLOSS)
    parts = [(str(x.get("summary", "")), str(x.get("body", "")), x.get("reason"), x.get("examples") if isinstance(x.get("examples"), list)
              else []) for x in (prop or {}).get("lessons", []) if isinstance(x, dict)] \
        or [(*memory.split_summary(WRAP.sub("", shape.EXAMPLE_TAGS.sub("", rule["said"])).strip()), None, [])]
    done = [{"kind": k, **x} for k in ("left out", "rewritten") for x in (prop or {}).get(k.replace(" ", "_"), [])
            if isinstance(x, dict) and "example" not in x]
    whys = [w for _, _, w, _ in parts if isinstance(w, dict) and w.get("said")] + [w for w in [(prop or {}).get("reason")] if isinstance(w, dict) and w.get("said")]
    done += list({json.dumps(x, sort_keys=True): x for x in (reason_of(s, rule, w, supplied) for w in whys)}.values())
    every = list(dict.fromkeys(memory.triggers(rule["text"]).values()))
    on = rule.get("keeps") or every
    shown_, faults = examples(rule, s.daily), list[str]()
    out_ = {n: str(x.get("why") or "") for x in (prop or {}).get("left_out", []) if isinstance(x, dict) and isinstance(n := x.get("example"), int)}
    made, names, glosses = list[dict[str, Any]](), dict[int, list[int]](), dict[int, str]()
    for i, (summary, body, _, exs) in enumerate(parts):
        def theirs(m: re.Match[str], i: int = i) -> str:    # an <example> the model wrote that is one of the rule's: named
            words = re.findall(r"[a-z0-9]+", m[1].lower())     # its words, or the line code cites it to, whatever its mark
            if n := next((k for k, y in enumerate(shown_, 1) if words in (re.findall(r"[a-z0-9]+", y["said"].lower()), re.findall(
                    r"[a-z0-9]+", shape.INSIDE.sub(r"\1", y["as"]).lower())) or (y.get("at") and (c := shape.CITED.search(m[1])) and c[0] == y["at"])), 0):
                names.setdefault(n, []).append(i)
            return "" if n else m[0]
        body = " ".join(new.split()) if (new := shape.INSIDE.sub(theirs, body)) != body else body
        for e in exs or []:
            n, g = (e, "") if isinstance(e, int) else (e.get("n"), str(e.get("gloss") or "")) if isinstance(e, dict) else (None, "")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(shown_):
                faults.append(f"lesson {i + 1} names example {n}, which the rule does not have: it has {len(shown_)}")
                continue
            names.setdefault(n, []).append(i)
            glosses[n] = g or glosses.get(n, "")
        body2, added = absolute(rule["job"], f"{summary} {body}".strip(), read)
        body = body2[len(summary):].strip()
        slug = lessons.slug_of(rule["job"], taken)
        taken.add(slug)
        made.append({"slug": slug, "job": rule["job"], "summary": summary, "body": body, "cls": rule["cls"], "rooms": rule["rooms"],
                     "on": on, "filed": rule["filed"]})
        if len(lesson_of(made[-1]).front()) > lessons.FRONT_MAX:      # the sets past the front matter's room: each word alone
            made[-1]["on"] = on = every
        while made[-1]["on"] and len(lesson_of(made[-1]).front()) > lessons.FRONT_MAX:
            made[-1]["on"] = made[-1]["on"][:-1]
        done += added
    if len(made) == 1 and not names and not out_:
        names = {n: [0] for n in range(1, len(shown_) + 1)}
    for n, x in enumerate(shown_, 1):
        at = list(dict.fromkeys(names.get(n, [])))
        if len(at) > 1:
            faults.append(f"example {n} is named in {len(at)} lessons ({', '.join(str(k + 1) for k in at)}): name it in the one whose step it shows")
        if at:
            g = glosses.get(n, "") if x.get("at") else ""
            if len(g.split()) > gloss:
                faults.append(f'example {n}\'s gloss "{g}" has {len(g.split())} words, past the {gloss} the gate takes beside a cite')
            x = {**x, "as": x["as"].replace("</example>", f", {g}</example>")} if g else x
            made[at[0]]["body"] = f"{made[at[0]]['body']} {x['as']}".strip()
        elif out_.get(n, "").strip():
            x = {**x, "kind": "example left out", "why": out_[n]}
        else:
            x = {**x, "kind": "example in no lesson"}
        done.append(x)
    cited, wrong = quoted(rule, made, s.daily, most, [w for _, w in supplied.get(rule["cite"], [])])
    done += cited
    if (c := (prop or {}).get("caution")):
        held = next((x for x in made if flat(str(c)) in flat(f"{x['summary']} {x['body']}")), None)
        done.append({"kind": "caution to review", "said": str(c), "why": f"left in place in {lessons.DIR}/{held['slug']}.md: deleting one is "
                     "the owner's side's call" if held else "in no lesson"})
    for x in made:
        x["gate"] = (memory.unshaped(rule["job"], f"{x['summary']} {x['body']}".strip(), acronyms, s, read, seen) or {}).get("fix", [])
        if len(x["on"]) < len(on):
            done.append({"kind": "triggers trimmed", "said": ", ".join(on[len(x["on"]):]), "why": f"the front matter holds {lessons.FRONT_MAX} characters"})
    row = f"{curate.PROPOSALS}/{REASONS} names one for it" if rule["cite"] in supplied else f"a row in {curate.PROPOSALS}/{REASONS} supplies one"
    if not any(x["kind"] in ("reason added", "reason supplied") for x in done) and "no reason" in shape.lesson(rule["job"], rule["said"], None, {}, read=read):
        done.append({"kind": "no reason on file", "why": "listed for the owner's side: a reason is never invented; " + row})
    elif prop:                          # a split rule's lesson refused only for a reason, whose part the rule gives none
        done += [{"kind": "no reason on file", "said": x["summary"], "why": "listed for the owner's side: the rule gives this lesson's part no "
                  "reason and a reason is never invented; " + row} for x, (_, _, w, _) in zip(made, parts) if x["gate"]
                 and all(g.startswith(WANTING) or (open_finding(g) and unlifted(rule, f"{x['summary']} {x['body']}", read)) for g in x["gate"])
                 and any(g.startswith(WANTING) for g in x["gate"]) and not (isinstance(w, dict) and w.get("said"))]
    return {**{k: rule[k] for k in ("cite", "file", "line", "text", "job")}, "lessons": made, "done": done,
            "missing": accounted(rule, made, done), "faults": faults + wrong + checks(rule, made, done, shown_)}


def checks(rule: dict[str, Any], made: list[dict[str, Any]], done: list[dict[str, Any]], shown_: list[dict[str, str]]) -> list[str]:
    """What code finds wrong in a rule's lessons, each a finding sent back: an <example> no example of the rule is; an example of
    the rule in no lesson (left out with the model's why aside) or in two; the caution the model named not the rule's own sentence,
    or in no lesson (a rewritten item never accounts for it)."""
    out, tags = list[str](), [[m[0] for m in shape.INSIDE.finditer(x["body"])] for x in made]
    like = [re.compile(re.escape(x["as"][:-len("</example>")]) + (r"(?:, [^<]*)?</example>" if x.get("at") else "</example>")) for x in shown_]
    for i, have in enumerate(tags, 1):
        out += [f'lesson {i} holds an example the rule does not give: "{m[1].strip()}"' for y in have
                if not any(rx.fullmatch(y) for rx in like) and (m := shape.INSIDE.match(y))]
    gone = {d["said"] for d in done if d["kind"] == "example left out" and "would pass its length" not in str(d.get("why"))}
    for n, (x, rx) in enumerate(zip(shown_, like), 1):
        k = sum(any(rx.fullmatch(y) for y in have) for have in tags)
        out += [f'example {n} of the rule is in no lesson: "{x["said"]}"'] if not k and x["said"] not in gone else []
        out += [f"example {n} of the rule is in {k} lessons: it goes in the one whose step it shows"] if k > 1 else []
    for d in (d for d in done if d["kind"] == "caution to review"):
        if flat(d.get("said", "")) not in flat(rule["said"]):
            out.append(f'the caution is not the rule\'s own sentence, word for word: "{d.get("said", "")}"')
        elif not any(flat(d["said"]) in flat(f"{x['summary']} {x['body']}") for x in made):
            out.append(f'the rule\'s caution is in no lesson: "{d["said"]}"')
    return out


def refused(gate: list[str], none: bool, open_: bool = False) -> bool:
    """A lesson's gate findings refuse it: any finding, but for a rule whose cite has a NONE row, a finding besides a reason's, and
    for a lesson whose absolute has no exception since its rule gives none (`open_`, unlifted), a finding besides that one."""
    return bool(gate) and not all((none and g.startswith(WANTING)) or (open_ and open_finding(g)) for g in gate)


def findings(m: dict[str, Any], none: bool = False, rule: dict[str, Any] | None = None, read: Any = None) -> list[str]:
    """What still fails in one rule's lessons, word for word: each lesson's gate refusal, each sentence of the rule not accounted
    for and each reason refused. A refusal for no reason goes back too, since a reason a reader sees ("which slows the page")
    is none to the gate; the prompt says to give none where none is on file. `none`: the rule has a NONE row, so a lesson
    refused for a reason alone stays as it is."""
    open_ = [bool(rule) and unlifted(rule or {}, f"{x['summary']} {x['body']}", read) for x in m["lessons"]]   # never asked to add one
    return [f'lesson {i} ("{x["summary"]}"): {g}' for i, (x, o) in enumerate(zip(m["lessons"], open_), 1) if refused(x["gate"], none, o)
            for g in x["gate"] if not (o and open_finding(g))] + [f'not accounted for: "{x}"' for x in m["missing"]] + \
        [f'reason refused: "{d["said"]}" ({d["why"]})' for d in m["done"] if d["kind"] == "reason refused"] + list(m.get("faults") or [])


def lesson_of(x: dict[str, Any]) -> lessons.Lesson:
    return lessons.Lesson(x["slug"], x["job"], x["summary"], x["body"], x["cls"], tuple(x["rooms"]), tuple(x["on"]), x["filed"])


def trigger_test(rules: list[dict[str, Any]], made: list[dict[str, Any]],
                 messages: list[tuple[str, str]]) -> tuple[dict[str, list[str]], dict[str, int]]:
    """The frozen trigger test: cite -> each owner message that loads the rule today (memory.on_topic among every live rule)
    and loads no lesson made of it (on_topic among every lesson made), none lost no key; and cite -> how many load it."""
    old = [r["text"] for r in rules]
    new = {r["cite"]: [lesson_of(x).line for x in m["lessons"]] for r, m in zip(rules, made)}
    corpus = [x for v in new.values() for x in v]
    w_old, w_new, lost, loads = memory.rarities(old), memory.rarities(corpus), dict[str, list[str]](), dict[str, int]()
    for at, said in messages:
        if not (hit := {x for _, x in memory.on_topic(said, old, old, w_old)}):
            continue
        now = {x for _, x in memory.on_topic(said, corpus, corpus, w_new)}
        for r in rules:
            if r["text"] in hit:
                loads[r["cite"]] = loads.get(r["cite"], 0) + 1
                if not now & set(new[r["cite"]]):
                    lost.setdefault(r["cite"], []).append(at)
    return lost, loads


def proposed(said: str, cites: set[str]) -> dict[str, dict[str, Any]]:
    """cite -> the JSON object a model's answer gives for it, one a line."""
    out = {}
    for raw in said.splitlines():
        try:
            if isinstance(x := json.loads(raw.strip()), dict) and x.get("cite") in cites:
                out[x["cite"]] = x
        except ValueError:
            continue
    return out


def notes(s: memory.Settings, r: dict[str, Any], found: dict[str, list[str]]) -> str:
    """What the model is shown after a rule's words: its examples numbered, each with the line `pick` cites it to, and the owner's
    side's findings on an earlier conversion of it (FINDINGS)."""
    def line(at: str) -> str:
        f, _, n = at.removeprefix("daily/").rpartition(":")
        rows = (s.daily / f).read_text(encoding="utf-8", errors="replace").splitlines() if (s.daily / f).is_file() else []
        return " ".join(((ln.body if (ln := memory.parse_line(rows[int(n) - 1])) else rows[int(n) - 1]) if 0 < int(n) <= len(rows) else "").split())[:300]
    return "".join(f"\nExample {n}: {x['said']}" + (f" (code cites it as {x['at']}, which reads: {line(x['at'])})" if x.get("at") else "")
                   for n, x in enumerate(examples(r, s.daily), 1)) + ("\nThe owner's side read an earlier conversion of this rule and found:" + "".join(
                       f"\n- {f}" for f in found[r["cite"]]) if found.get(r["cite"]) else "")


def model_pass(rules: list[dict[str, Any]], model: Callable[[str, str], str], check: Callable[[dict[str, Any], dict[str, Any]], list[str]],
               supplied: dict[str, list[tuple[str, str]]] | None = None, notes: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """curate's propose and knock-down machinery on the rules out of the shape: curate.md's "Lessons" proposes each rule's lessons
    as one JSON line (CHUNK rules a call, side by side), "Knock down lessons" keeps or drops each; a rule whose lessons `check`
    still finds failing (the gate's refusals, its words not all accounted for) or that was dropped goes back with its proposal and
    the exact findings ("Lessons again", the round's failing rules CHUNK a call, side by side), ROUNDS times at most. cite -> {"prop": its last
    proposal (none: no answer came for it), "findings": what still fails after the last round, "rounds": the times it went back}."""
    if not rules:
        return {}
    have = supplied or {}
    shown_ = {r["cite"]: f"=== {r['cite']} (job: {r['job']})\n{WRAP.sub('', shape.EXAMPLE_TAGS.sub('', r['said'])).strip()}" + "".join(
        f"\nReason on file, from {at}: {why}" for at, why in have.get(r["cite"], [])) + (notes or {}).get(r["cite"], "") for r in rules}

    def side(part: str, group: list[dict[str, Any]], extra: Callable[[str], str], tools: str) -> list[str]:
        chunks = [group[i:i + CHUNK] for i in range(0, len(group), CHUNK)]
        with ThreadPoolExecutor(max(1, len(chunks))) as ex:
            return list(ex.map(lambda c: model(curate.prompt(part, {}, "\n\n".join(shown_[r["cite"]] + extra(r["cite"]) for r in c)), tools), chunks))

    props: dict[str, dict[str, Any]] = {}
    for said in side("Lessons", rules, lambda c: "", ""):
        props |= proposed(said, {r["cite"] for r in rules})
    todo, out = list(rules), dict[str, dict[str, Any]]()
    for n in range(ROUNDS + 1):
        asked = [r for r in todo if r["cite"] in props]
        said = "\n".join(side("Knock down lessons", asked, lambda c: "\n=== proposed\n" + json.dumps(props[c]), "Read,Grep,Glob")) if asked else ""
        kept = {m[1] for m in re.finditer(r"^KEEP (\S+)\s*$", said, re.M)}
        dropped = {m[1]: m[2].strip() for m in re.finditer(r"^DROP (\S+)\s*(?:\|\s*(.*))?$", said, re.M)}
        for r in todo:
            c = r["cite"]
            found = ["no proposal came back for it"] if c not in props else [*check(r, props[c]), *(
                [] if c in kept else [f"{DROPPED}{dropped.get(c) or 'no verdict'}"])]
            out[c] = {"prop": props.get(c), "findings": found, "rounds": n}
        todo = [r for r in todo if out[r["cite"]]["findings"]]
        if not todo or n == ROUNDS:
            break
        for said in side("Lessons again", todo, lambda c: "\n=== proposed\n" + json.dumps(props.get(c) or {}) + "\n=== findings\n"
                         + "\n".join(f"- {f}" for f in out[c]["findings"]), ""):
            props |= proposed(said, {r["cite"] for r in todo})
    return out


def render(rec: dict[str, Any]) -> str:
    """The proposals file: each original by its cite, its lessons with their gates, and each thing done as a listed item, then
    the JSON block an apply reads; apply renders it again and refuses a file that differs."""
    out = [f"# Lessons proposed from the rule store, {rec['day']}", "",
           f"Store read: {', '.join(f'{k} {v[:12]}' for k, v in sorted(rec['store'].items()))}. Pages read: {', '.join(rec['read'])}.",
           f"Apply: `yerk curate --lessons-apply {rec['path']}`. Undo after it: `yerk curate --lessons-undo {rec['day']}`.",
           f"Ready to apply: {'yes' if rec['ready'] else 'no: ' + '; '.join(rec['why'])}.",
           *([f"Listed as no reason on file, as the owner's side says (a {NONE} row in {curate.PROPOSALS}/{REASONS}), not counted in the verdict: "
              f"{len(rec['no_reason'])} lesson{'s' * (len(rec['no_reason']) != 1)}, {', '.join(f'{lessons.DIR}/{x}.md' for x in rec['no_reason'])}."]
             if rec.get("no_reason") else []),
           *([f"Kept from {rec['from']['path']}: {rec['from']['kept']:,} rule{'s' * (rec['from']['kept'] != 1)} word for word, "
              f"with no model call; {rec['from']['redone']:,} redone."] if rec.get("from") else []),
           *([f"Redone, by why (a rule may have more than one): {', '.join(f'{k} {v:,}' for k, v in rec['from']['why'].items())}."]
             if (rec.get("from") or {}).get("why") else []),
           *([f"Listed as no exception, as the rule gives none, carried as he wrote it and not counted in the verdict: "
              f"{len(rec['no_exception'])} lesson{'s' * (len(rec['no_exception']) != 1)}, {', '.join(f'{lessons.DIR}/{x}.md' for x in rec['no_exception'])}."]
             if rec.get("no_exception") else []),
           *([f"Examples kept as their words, since no day-log line passes, that the gate asks a cite of (no wrong line is written to pass "
              f"it): {len(rec['uncited'])}, in {', '.join(f'{lessons.DIR}/{x}.md' for x in rec['uncited'])}; listed for the owner's side."]
             if rec.get("uncited") else []),
           *([f"The index it would write: {rec['index'][0]:,} lines and {rec['index'][1]:,} bytes, against code-memory's "
              f"{rec['index'][2]:,} and {rec['index'][3]:,}."] if rec.get("index") else []), ""]
    for r in rec["rules"]:
        out += [f"## {r['cite']}", "", f"Original: {r['text']}", ""]
        for x in r["lessons"]:
            out.append(f"- lesson {lessons.DIR}/{x['slug']}.md: {x['summary']}" + (f" | {x['body']}" if x["body"] else ""))
            out.append("  - gate: passes" if not x["gate"] else "  - gate: refuses: " + " / ".join(x["gate"]))
        for d in r["done"]:
            out.append(f"- {d['kind']}" + (f": \"{d['said']}\"" if d.get("said") else "") + (f" -> {d['at']}" if d.get("at") else "")
                       + (f" as \"{d['as']}\"" if d.get("as") else "") + (f", from {d['from']}" if d.get("from") else "")
                       + (f" ({d['why']})" if d.get("why") else ""))
        out += [f"- not accounted for: \"{x}\"" for x in r["missing"]] + [f"- fault: {x}" for x in r.get("faults") or []]
        lost = rec["lost"].get(r["cite"], [])
        out += [f"- triggers: {r['loads']} owner message{'s' * (r['loads'] != 1)} of the last {DAYS} days load it; "
                + (f"lost by the split: {', '.join(lost)}" if lost else "each still loads a lesson made of it"), ""]
    return "\n".join(out) + "\n```json\n" + json.dumps(rec, sort_keys=True) + "\n```\n"


def dry_run(private: Path, state: Path, s: memory.Settings, now: float | None = None,
            model: Callable[[str, str], str] | None = None, read: Any = None, earlier: Path | None = None) -> Path:
    """The dry run: the proposals file written, nothing else. Its path. `read`: the pages the gate reads, else READ's. `earlier`
    (`--from FILE`): each rule that passed there kept as it made it (carried), every other rule proposed as in a fresh run, its
    lessons taking no slug a kept one holds; the trigger test and the verdict read all of them."""
    now = time.time() if now is None else now
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    read = shape.pages(READ) if read is None else read
    acronyms = shape.install(dict(conf.get("docs") or {}))[1]
    rules, seen, taken = originals(s), dict[Path, str](), set(lessons.retired(s.root))
    supplied, weights, rows = supplied_reasons(s), memory.rarities([r["text"] for r in rules]), reason_rows(s)
    for r in rules:
        r["keeps"] = keeps(r["text"], weights)
    found, redone = finding_rows(s), dict[str, list[str]]()
    kept = carried(s, rules, earlier, lambda x: (memory.unshaped(x["job"], f"{x['summary']} {x['body']}".strip(), acronyms, s, read,
                                                                 seen) or {}).get("fix", []), rows, found, read, redone) if earlier else {}
    taken |= {x["slug"] for m in kept.values() for x in m["lessons"]}
    off = [r for r in rules if r["cite"] not in kept and (memory.unshaped(r["job"], r["said"], acronyms, s, read, seen) or found.get(r["cite"]))]
    props = model_pass(off, model or (lambda p, tools: curate.ask(private, state, dict(conf.get("curate", {})), p, tools)),
                       lambda r, x: findings(build(s, r, x, read, seen, set(taken), acronyms, supplied), NONE in rows.get(r["cite"], []), r, read),
                       supplied, {r["cite"]: notes(s, r, found) for r in off})
    made = []
    for r in rules:
        if r["cite"] in kept:
            made.append(kept[r["cite"]])
            continue
        made.append(build(s, r, (props.get(r["cite"]) or {}).get("prop"), read, seen, taken, acronyms, supplied))
        if (p := props.get(r["cite"])) and p["findings"]:
            made[-1]["done"].append({"kind": "still failing", "why": f"after {p['rounds']} round{'s' * (p['rounds'] != 1)} back to the model: "
                                     + " / ".join(p["findings"]) + ("" if p["prop"] else "; split as it stands")})
            if drop := next((f for f in p["findings"] if f.startswith(DROPPED)), ""):    # counted: its lessons may all pass the gate
                made[-1]["done"].append({"kind": "reviewer dropped", "why": f"after the last round: {drop.removeprefix(DROPPED)}"})
    lost, loads = trigger_test(rules, made, owner_messages(s, now))
    for r, m in zip(rules, made):
        m["loads"], m["reasons"], m["found"] = loads.get(r["cite"], 0), rows.get(r["cite"], []), found.get(r["cite"], [])
    day = datetime.fromtimestamp(now, s.tz).date().isoformat()
    path = s.root / curate.PROPOSALS / FILE.format(day=day)
    listed, unlift = dict[str, str](), dict[str, str]()   # slug -> its rule's cite: no reason on file, as the owner's side says; no
    bare = list[dict[str, Any]]()                           # exception, as the rule gives none; refused only for a reason, counted
    for r, m in zip(rules, made):
        m["done"], none = [d for d in m["done"] if d["kind"] not in (LISTED, UNLIFTED)], NONE in rows.get(m["cite"], [])
        for x in (x for x in m["lessons"] if x["gate"]):
            if refused(x["gate"], none, open_ := unlifted(r, f"{x['summary']} {x['body']}", read)):
                bare += [x] * (all(g.startswith(WANTING) or (open_ and open_finding(g)) for g in x["gate"]) and any(
                    d["kind"] == "no reason on file" and d.get("said", x["summary"]) == x["summary"] for d in m["done"]))
                continue
            if none and any(g.startswith(WANTING) for g in x["gate"]):
                listed[x["slug"]] = m["cite"]
                m["done"].append({"kind": LISTED, "said": x["summary"], "why": f"{lessons.DIR}/{x['slug']}.md, a {NONE} row in {curate.PROPOSALS}/"
                                  f"{REASONS}: carried as it stands and named, since a reason is never invented; not counted in the verdict"})
            if open_ and any(open_finding(g) for g in x["gate"]):
                unlift[x["slug"]] = m["cite"]
                m["done"].append({"kind": UNLIFTED, "said": x["summary"], "why": f"{lessons.DIR}/{x['slug']}.md: its absolute carried as he wrote "
                                  "it and named, since no exception is ever added to a rule of his; not counted in the verdict"})
    uncited = {x["slug"]: m["cite"] for m in made for x in m["lessons"] if any("an example the day log holds at" in g for g in x["gate"])}
    why = [f"{n} lesson{'s' * (n != 1)} the gate refuses" for n in [sum(bool(x["gate"]) for m in made for x in m["lessons"]) - len(bare)
                                                                    - len(set(listed) | set(unlift))] if n]
    why += [f"{n} lesson{'s' * (n != 1)} refused only for a reason no file holds, each listed (a row in {curate.PROPOSALS}/{REASONS} "
            "supplies one)" for n in [len(bare)] if n]
    why += [f"{n} original{'s' * (n != 1)} not accounted for" for n in [sum(bool(m["missing"]) for m in made)] if n]
    why += [f"{n} rule{'s' * (n != 1)} with a fault the code finds, each listed" for n in [sum(bool(m.get("faults")) for m in made)] if n]
    why += [f"{n} rule{'s' * (n != 1)} the reviewer dropped after the last round, each listed" for n in
            [sum(any(d["kind"] == "reviewer dropped" for d in m["done"]) for m in made)] if n]
    why += [f"{n} original{'s' * (n != 1)} losing a trigger" for n in [len(lost)] if n]
    index, (most, size) = lessons.index_text([lesson_of(x) for m in made for x in m["lessons"]]), shape.index_limits(s.docs)
    lines, have = len(index.splitlines()), len(index.encode())
    over = [f"{n:,} {unit}, {n - cap:,} past {cap:,}" for n, cap, unit in ((lines, most, "lines"), (have, size, "bytes")) if n > cap]
    why += [f"the index it would write is past code-memory's limits: {' and '.join(over)}"] if over else []
    rec = {"day": day, "path": str(path), "store": {n: sha(s.root / n) for n in (memory.RULES, memory.TOPIC_RULES)}, "read": list(read),
           "ends": ends(s), "rules": made, "lost": lost, "ready": not why, "why": why, "index": [lines, have, most, size], "no_reason": listed,
           "no_exception": unlift, "uncited": uncited, **({"from": {"path": str(earlier), "kept": len(kept), "redone": len(rules) - len(kept), "why": {
               k: n for k in STALE if (n := sum(k in v for c, v in redone.items() if c not in kept))}}} if earlier else {})}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, render(json.loads(json.dumps(rec, sort_keys=True))))      # as an apply will read it back
    return path


def loaded(path: Path) -> dict[str, Any]:
    """The proposals file's record, refused unless the file is exactly what the dry run wrote."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"\n```json\n(.+)\n```\n\Z", text, re.S)
    rec = json.loads(m[1]) if m else {}
    if not rec or render(rec) != text:
        raise RuntimeError(f"{path.name} is not the file a dry run wrote, word for word: run the dry run again")
    return dict(rec)


def apply(private: Path, state: Path, s: memory.Settings, path: Path) -> list[str]:
    """The conversion, from the exact proposals file: refused unless it is ready, every lesson still passes the gate and the
    store is byte for byte the one it read; the originals copied first, then each lesson, the index, the old cites' map and the
    snapshot news reads written, and rules-topics.md moved into the copy."""
    rec = loaded(path)
    if not rec["ready"]:
        raise RuntimeError(f"not ready: {'; '.join(rec['why'])}: nothing applied")
    if moved := [n for n, h in rec["store"].items() if sha(s.root / n) != h]:
        raise RuntimeError(f"{', '.join(moved)} changed since the dry run read it: run the dry run again; nothing applied")
    if lessons.converted(s.root):
        raise RuntimeError(f"the store is converted already ({lessons.DIR}/ is there): nothing applied")
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    acronyms = shape.install(dict(conf.get("docs") or {}))[1]
    seen, rows, listed = dict[Path, str](), reason_rows(s), dict(rec.get("no_reason") or {})    # a listed lesson passes for its
    unlift = dict(rec.get("no_exception") or {})       # reason alone while its rule's NONE row is there, for its exception alone
    if off := [x["slug"] for m in rec["rules"] for x in m["lessons"] if refused(
               (memory.unshaped(x["job"], f"{x['summary']} {x['body']}".strip(), acronyms, s, rec["read"], seen) or {}).get("fix", []),
               x["slug"] in listed and NONE in rows.get(listed[x["slug"]], []), x["slug"] in unlift)]:
        raise RuntimeError(f"{off[0]} no longer passes the gate: run the dry run again; nothing applied")
    backup = s.root / f"{lessons.BACKUP}{rec['day']}"
    with memory.daily_lock(s.daily):
        backup.mkdir(mode=0o700, parents=True, exist_ok=True)
        for n in (memory.RULES, memory.TOPIC_RULES, memory.LATER):
            if (s.root / n).is_file():
                shutil.copy2(s.root / n, backup / n)
        made = [(lesson_of(x), m) for m in rec["rules"] for x in m["lessons"]]
        first = {m["cite"]: lesson_of(m["lessons"][0]).slug for m in rec["rules"] if m["lessons"]}
        (s.root / lessons.DIR).mkdir(mode=0o700, exist_ok=True)
        for x, _ in made:
            write_private(s.root / lessons.DIR / f"{x.slug}.md", x.text())
        write_private(s.root / lessons.CITES, "".join(f"{a}\t{first[b]}\n" for a, b in rec["ends"].items() if b in first))
        if listed or unlift:           # one list, each row saying what is missing
            write_private(s.root / lessons.NO_REASON, "".join(f"{x}\t{rec['day']}\t{listed.get(x) or unlift[x]}\t" + " and ".join(
                ["a reason"] * (x in listed) + ["an exception"] * (x in unlift)) + "\n" for x in dict.fromkeys([*listed, *unlift])))
        lessons.keep(s.root, rec["day"], [(x, [m["text"]]) for x, m in made])
        write_private(s.root / memory.RULES, lessons.index_text([x for x, _ in made]))
        (s.root / memory.TOPIC_RULES).unlink(missing_ok=True)
        write_private(backup / APPLIED, json.dumps({"files": {f"{lessons.DIR}/{x.slug}.md": hashlib.sha256(x.text().encode()).hexdigest()
                                                              for x, _ in made}, "index": sha(s.root / memory.RULES)}))
    return [f"{len(made)} lessons from {len(rec['rules'])} rules written to {lessons.DIR}/, the index to {memory.RULES}",
            f"originals kept in {backup.name}/; undo: yerk curate --lessons-undo {rec['day']}"]


def undo(s: memory.Settings, day: str) -> list[str]:
    """The originals put back from rules-backup-<day>/ and the lessons, the old cites' map and the tombstones gone, refused when
    a lesson was filed or changed since the apply (re-file it after the undo, or retire it first)."""
    backup = s.root / f"{lessons.BACKUP}{day}"
    try:
        rec = json.loads((backup / APPLIED).read_text())
    except (OSError, ValueError):
        raise RuntimeError(f"no apply of {day} to undo: {backup.name}/{APPLIED} is not there") from None
    have = {f"{lessons.DIR}/{p.name}": sha(p) for p in (s.root / lessons.DIR).glob("*.md")}
    if changed := sorted(f for f, h in have.items() if rec["files"].get(f) != h):
        raise RuntimeError(f"{changed[0]} was filed or changed since the apply: retire it or keep its words, then undo; nothing put back")
    with memory.daily_lock(s.daily):
        for n in (memory.RULES, memory.TOPIC_RULES, memory.LATER):
            if (backup / n).is_file():
                write_private(s.root / n, (backup / n).read_bytes())
        for f in have:
            (s.root / f).unlink()
        (s.root / lessons.DIR).rmdir()
        for n in (lessons.CITES, lessons.RETIRED, lessons.NO_REASON):
            (s.root / n).unlink(missing_ok=True)
        (backup / APPLIED).rename(backup / f"{APPLIED}.undone")
    return [f"{len(have)} lessons removed; {memory.RULES} and {memory.TOPIC_RULES} put back from {backup.name}/"]


def main(private: Path, state: Path, apply_to: str = "", undo_day: str = "", start_from: str = "") -> int:
    """`yerk curate --lessons` (the dry run; `--from FILE`, from an earlier proposals file), `--lessons-apply FILE`, `--lessons-undo
    DAY`; exits 1 on a refusal, 2 when it could not run."""
    try:
        conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
        s = memory.settings(private, state, dict(conf.get("memory", {})))
        out = undo(s, undo_day) if undo_day else apply(private, state, s, Path(apply_to)) if apply_to else [
            f"proposals: {dry_run(private, state, s, earlier=Path(start_from) if start_from else None)}; nothing else written"]
    except RuntimeError as e:
        print(f"refused: {e}")
        return 1
    except (Exception, SystemExit) as e:
        print(f"curate --lessons failed: {type(e).__name__}: {str(e)[:160]}")
        return 2
    print("\n".join(out))
    return 0
