"""`yerk curate --lessons`: rewrites the rules files as lesson files, each a one-line summary, then its reason and examples.
It turns an existing rules.md and rules-topics.md into lesson files and their index (lessons.py), as prompting-fable-5-2 says of
rules that built up: "rewrite absolute rules with their scope and reason", with the originals copied first. The dry run writes one
proposals file, <memory>/rules-proposals/lessons-<day>.md, and changes nothing else: for each original rule by its cite, its words,
the lessons made of it with each one's gate (the filing's own, memory.unshaped, on READ), and each thing done to it as a listed item;
the JSON block at its end is what an apply reads. Meaning never changes: a rule already in the shape is split as it stands (its first
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
from datetime import datetime, timedelta
from functools import lru_cache
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
WHOLE = "his quote kept whole"       # a listed item: a lesson keeping a quote of the rule whole where the gate asks a cite in its place
OWN = "his own sentence kept"        # a listed item: a lesson keeping the owner's sentence that holds "landed" or "bounced" beside a quote
UNCITED = "an example kept as its words"   # a listed item: an example with no line passing, its cite refused, or the rule's own cite with its long gloss
PAST = "his words kept whole, past the length"   # a listed item: a long body whose every sentence is the owner's as they wrote it
EXCUSES = {UNLIFTED: "no_exception", WHOLE: "whole", OWN: "own", UNCITED: "uncited", PAST: "long"}   # each kind and its list in the record
WHYS = {UNLIFTED: "its absolute carried as he wrote it and named, since no exception is ever added to a rule of his; not counted in the verdict",
        WHOLE: "his quote carried whole, word for word, where the gate asks a cite in its place, and named; not counted in the verdict",
        OWN: "his own sentence carried word for word where the gate reads an exchange outside example tags, and named; not counted in the verdict",
        UNCITED: "an example of his kept as its words, since no day-log line passes for it or its cite was refused, where the gate asks "
                 "a cite, or the rule's own cite with its gloss whole, where the gate asks a shorter gloss, and named, since no wrong "
                 "line is written and no gloss of the rule's cut to pass it; not counted in the verdict",
        PAST: "his sentences carried word for word where the gate asks a shorter body, and named, since a body of his own sentences is "
              "never refused for its length; not counted in the verdict"}
HEADS = {UNLIFTED: "no exception, as the rule gives none, carried as he wrote it", WHOLE: "his quote kept whole, carried as he wrote it where the "
         "gate asks a cite in its place", OWN: "his own sentence kept, carried word for word where the gate reads an exchange outside "
         "example tags", UNCITED: "an example kept as its words, since no day-log line passes or its cite was refused, where the gate asks "
         "a cite (no wrong line is written to pass it), or the rule's own cite with its gloss whole, where the gate asks a shorter "
         "gloss", PAST: "his words kept whole, past the length, carried word for word where the gate "
         "asks a shorter body (every sentence of it his, as he wrote it)"}
REFUSED_CITE = "cite refused"        # a listed item: the line code cites an example to, refused by the model with its why
RECORDS = "what the day log already records: "    # the gate's on-record finding (words.LESSON_NAMES), its head
OWN_HINT = (" (code's note: never put his sentence in example tags; keep each sentence of his that holds \"landed\" or \"bounced\" "
            "or his quote whole and word for word, write no sentence of your own that holds them, and the lesson is listed as his)")
UNTAGGED = "an example outside <example> tags: "   # the gate's exchange finding (shape.NAMES), its head
LONG = "a long body: "             # the gate's long-body finding (words.LESSON_NAMES), its head
SEALS = re.compile(r"\b(?:no (?:case|exception|one|go-ahead)[^.;]{0,24}lifts? (?:it|this|that)|nothing lifts (?:it|this|that)|lifts? it\b|"
                   r"even when asked|with no exceptions?|no exceptions?\b)", re.I)    # words that lift or seal a never: never added
FINDINGS = "findings.tsv"          # in <memory>/rules-proposals/: cite<TAB>a finding of the owner's side on an earlier conversion
NAMED = re.compile(r"\b(?:(\d{4})-(\d{2})-(\d{2})|(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* (\d{1,2})(?:,? (\d{4}))?)"
                   r"(?:,? (?:at )?(\d{1,2}):(\d)([\dx]))?\b", re.I)        # a day a passage names, and its time: Nov 9 2026 14:05, Oct 28
HIS = ("he", "him", "his")               # [curate] OWNER_WORDS unset: the rules' pronouns for the owner, read with "the owner" and their names
OWNER_WORDS = "owner_words"        # [curate] in yerkd.toml: the pronouns the rules use for the owner, in HIS's place (unset: HIS)
OWNER_NAMES = "owner_names"        # [curate] in yerkd.toml: the names the private layer gives for the owner, read beside OWNER_WORDS (unset: those alone)
FILING = 120                       # seconds after a rule's trailer time that a day-log line may be stamped and count as its filing's
NO_DAY = "no such day"             # the day `named` gives for one that is none (Nov 31): it names no line, so it cites nothing
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
    last one not after the filing; and its time as minutes of the day (14:4x as 14:45), or None. ("", None) when it names none,
    (NO_DAY, None) when the day it names is none."""
    if not (m := NAMED.search(shape.QUOTED.sub(" ", text))):
        return "", None
    if m[1]:
        y, mon, d = int(m[1]), int(m[2]), int(m[3])
    else:
        mon, d = "jan feb mar apr may jun jul aug sep oct nov dec".split().index(m[4][:3].lower()) + 1, int(m[5])
        y = int(m[6]) if m[6] else (filed or datetime.now()).year - bool(filed and (mon, d) > (filed.month, filed.day))
    try:
        day = datetime(y, mon, d).date().isoformat()
    except ValueError:                   # Nov 31: a day that is none names no line
        return NO_DAY, None
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
    never one written after the rule was filed (its trailer's time, a line stamped FILING seconds after it being the filing's
    own); on the day `when` names, when it names one (none, when that day is none), and with a time the line nearest it; for
    words given as the owner's, a line of their own; else the earliest. shape.on_record, the gate's, is not this."""
    want, cut = flat(words), filed_at(filed)
    day, near = place or named(when, cut)
    end = cut + timedelta(seconds=FILING) if cut else None    # the trailer carries the message's time, its day-log line a later stamp
    best: tuple[int, str, int] | None = None
    for p in sorted(daily.glob("*.md")) if want and daily.is_dir() and day != NO_DAY else ():
        if not memory.DAYLOG.search(p.name) or (day and p.stem != day) or (end and p.stem > end.date().isoformat()):
            continue
        for i, (text, role, at) in enumerate(log_lines(p), 1):
            if want not in text or (his and role not in OWNER) or (end and at and at > end):
                continue
            if near is None:
                return f"daily/{p.name}:{i}"
            gap = abs(at.hour * 60 + at.minute - near) if at else 10 ** 6
            best = (gap, p.name, i) if best is None or gap < best[0] else best
    return f"daily/{best[1]}:{best[2]}" if best else ""


@lru_cache(maxsize=16)
def his_words(names: tuple[str, ...] = (), said_by: tuple[str, ...] = HIS) -> re.Pattern[str]:
    """The words that give a quote as the owner's, a few words before it in its sentence: `said_by` (OWNER_WORDS, else HIS), the
    owner, each of their names the install gives (OWNER_NAMES), or "in reply to"."""
    said = "|".join(re.escape(" ".join(w.split())) for w in (*said_by, "the owner", *names) if w.strip())
    return re.compile(rf"(?:\b(?:{said})(?:['’]s)?\b(?:\W+\w+){{0,3}}|\bin reply to)\W*$", re.I)


def owner_names(conf: dict[str, Any]) -> tuple[str, ...]:
    """[curate] OWNER_NAMES of a yerkd.toml: the names the private layer gives for the owner, a list or one name; none when unset."""
    v = dict(conf.get("curate") or {}).get(OWNER_NAMES) or ()
    return tuple(str(x).strip() for x in ([v] if isinstance(v, str) else v) if str(x).strip())


def owner_words(conf: dict[str, Any]) -> tuple[str, ...]:
    """[curate] OWNER_WORDS of a yerkd.toml: the pronouns the rules use for the owner, a list or one word; HIS when unset."""
    v = dict(conf.get("curate") or {}).get(OWNER_WORDS, HIS)
    return tuple(str(x).strip() for x in ([v] if isinstance(v, str) else v) if str(x).strip())


def given_his(before: str, names: tuple[str, ...] = (), said_by: tuple[str, ...] = HIS) -> bool:
    """Words given as the owner's: their pronouns (`said_by`, OWNER_WORDS, else HIS), the owner or a name of theirs (`names`, OWNER_NAMES)
    a few words before them in their sentence, or "in reply to"."""
    return bool(his_words(tuple(names), tuple(said_by)).search(re.split(r"[.!?]\s", before)[-1][-48:]))


def said_as(rule: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A rule's names for the owner and the pronouns its words use for them, as given_his takes them (yerkd.toml's, set on the rule)."""
    return tuple(rule.get("his") or ()), tuple(rule.get("said_by", HIS))


def words_of(text: str) -> str:
    """Words as a quote or a sentence is matched word for word: lower case, each run of letters and digits, the rest aside."""
    return " ".join(re.findall(r"[^\W_]+", text.lower()))


def quoted(rule: dict[str, Any], made: list[dict[str, Any]], daily: Path, most: int, also: list[str] | None = None) -> tuple[list[dict[str, str]], list[str]]:
    """The owner's quotes in the rule's lessons, each whole: each quoted run of a lesson (its examples and code's scope sentence aside)
    is, word for word, a whole quote of the rule or a whole reason on file (`also`, REASONS: the owner's words too); a part of one (cut,
    split into pieces, joined across an ellipsis) is a fault, and so is a quote that is neither. A quote of the rule past `most`
    words that a lesson keeps is a listed item with the line `pick` finds for it (its words given as the owner's when the rule gives
    them so, the day and time the rule names before it), or kept; never a pointer. A reason on file goes in whole, in quote
    marks, given as the owner's (he said, his words, his order): one whose words stand in a lesson outside quote marks (the rule's own
    words aside), or in quote marks not given as the owner's, is a fault too."""
    said, (names, by) = rule["said"], said_as(rule)
    theirs = [(m[1], m.start()) for m in shape.QUOTED.finditer(said)] + [(x, len(said)) for x in also or []]
    done, faults, seen_ = list[dict[str, str]](), list[str](), set[str]()
    own = f" {words_of(shape.QUOTED.sub(' ', said))} "
    for i, x in enumerate(made, 1):
        text = shape.EXAMPLE_TAGS.sub(" ", f"{x['summary']} {x['body']}").replace(SCOPED.format(job=rule["job"]), " ")
        bare = f" {words_of(shape.QUOTED.sub(' ', text))} "
        faults += [f'lesson {i} gives his reason on file as its own words: "{w}" goes in whole, in quote marks, given as his' for w in also or []
                   if words_of(w) and f" {words_of(w)} " in bare and f" {words_of(w)} " not in own]
        for m in shape.QUOTED.finditer(text):
            if not (have := words_of(m[1])):
                continue
            if not (of := [(q, at) for q, at in theirs if words_of(q) == have]):
                parts = [y for y in (words_of(y) for y in ELIDED.split(m[1])) if y]
                whole = next((q for q, _ in theirs if all(f" {y} " in f" {words_of(q)} " for y in parts)), "")
                faults.append(f'lesson {i} cuts his quote: "{m[1]}" is part of "{whole}", which goes in whole, word for word' if whole
                              else f'lesson {i} quotes words that are no whole quote of the rule: "{m[1]}"')
                continue
            q, at = of[0]
            if at == len(said) and not given_his(text[:m.start()], names, by):
                faults.append(f'lesson {i} quotes his reason on file but not as his: "{m[1]}" goes in given as his (he said, his words, his order)')
            if len(q.split()) > most and q not in seen_ and at < len(said):
                seen_.add(q)
                line = pick(q, daily, rule.get("filed", ""), given_his(said[:at], names, by),
                            place=before(said[:at], filed_at(rule.get("filed", ""))))
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


def sealed(rule: dict[str, Any], text: str) -> bool:
    """A lesson holding a never of the rule's whose own sentence in the rule (own_sentence) gives it no exception (the gate's
    EXCEPTION, read in that sentence alone, its quotes out): one it must carry as the owner wrote it, with no exception added (S2b)."""
    return any((y := own_sentence(rule, w, mine)) and not shape.EXCEPTION.search(shape.QUOTED.sub(" ", y)) for w, mine in nevers(text))


def strong(said: str) -> set[str]:
    """The absolute words of a passage, lower case, its quotes and examples out."""
    return {w.lower() for w in shape.ABSOLUTE.findall(shape.QUOTED.sub(" ", shape.EXAMPLE_TAGS.sub(" ", said)))}


def nevers(text: str) -> list[tuple[str, str]]:
    """(absolute word, the lesson's sentence holding it) for each absolute of a lesson."""
    return [(w, y) for y in shape.SENTENCES.split(shape.EXAMPLE_TAGS.sub(" ", text)) for w in sorted(strong(y))]


def own_sentence(rule: dict[str, Any], w: str, mine: str) -> str:
    """The sentence of the rule a lesson's absolute `w` comes from: of the rule's sentences holding the word (outside quotes and
    examples), the one whose words are nearest those of the lesson's sentence `mine`; "" when the rule has no such word."""
    have = [y for y in shape.SENTENCES.split(WRAP.sub(" ", shape.EXAMPLE_TAGS.sub(" ", rule["said"]))) if w in strong(y)]
    near = set(words_of(mine).split())
    return max(have, key=lambda y: len(near & set(words_of(y).split())) / max(1, len(near | set(words_of(y).split()))), default="")


def unlifted(rule: dict[str, Any], text: str, read: Any) -> bool:
    """A lesson's absolute with no exception because its rule gives none where it says it: each absolute word of the lesson is
    one the rule says (outside its quotes and examples), and the sentence of the rule it comes from (own_sentence) gives no
    exception, as the gate reads that sentence alone. An exception in another sentence of the rule changes nothing, one that
    holds the same word too; a never whose own sentence gives the exception, and an absolute the rule does not have, still count."""
    return bool(pairs := nevers(text)) and all((y := own_sentence(rule, w, mine)) and any(
        "no exception" in g for g in shape.lesson(rule["job"], y, None, {}, read=read).get("absolute") or []) for w, mine in pairs)


def open_finding(g: str) -> bool:
    """The gate's absolute finding for an exception alone, its scope written in."""
    return g.startswith(LIFTED) and "with no exception written in" in g.split(";")[0] and "no scope" not in g.split(";")[0]


def own_sentences(rule: dict[str, Any], text: str) -> bool:
    """A lesson in which each sentence holding what the gate's exchange finding names (a quote of three words or more, "landed" or
    "bounced") is a sentence of the rule, word for word, one of them holding "landed" or "bounced": the owner's own sentence kept, never
    bent to pass the gate. A sentence the model wrote holding one (one that opens with someone asks, says or wrote and holds a
    quote) is not this, nor a lesson holding a thumbs mark outside its examples. A sentence giving the owner's reason on file as theirs
    (reason_given) counts as theirs there."""
    def sentences(said: str) -> list[str]:
        return shape.SENTENCES.split(WRAP.sub(" ", shape.EXAMPLE_TAGS.sub(" ", said)).replace(SCOPED.format(job=rule["job"]), " ").strip())
    theirs = {words_of(y) for y in sentences(rule["said"])}
    held = [y for y in sentences(text) if shape.MARKED.search(y) or any(len(m[1].split()) >= 3 for m in shape.QUOTED.finditer(y))]
    return (any(shape.MARKED.search(y) for y in held) and all(words_of(y) in theirs or reason_given(rule, y) for y in held)
            and not shape.THUMBS.search(shape.EXAMPLE_TAGS.sub(" ", text)))


def reason_given(rule: dict[str, Any], y: str) -> bool:
    """A lesson's sentence giving the owner's reason on file as theirs: it holds no "landed" or "bounced", and each quote of three words or
    more in it is, word for word, a whole reason on file for the rule (its "on_file", REASONS), given as the owner's (given_his)."""
    have, (names, by) = {words_of(w) for w in rule.get("on_file") or ()}, said_as(rule)
    quotes = [m for m in shape.QUOTED.finditer(y) if len(m[1].split()) >= 3]
    return bool(quotes) and not shape.MARKED.search(y) and all(words_of(m[1]) in have and given_his(y[:m.start()], names, by) for m in quotes)


def past(rule: dict[str, Any], text: str) -> bool:
    """A lesson whose long body is the owner's own words, never refused for its length: each sentence of its body (after its summary, as
    the gate splits it; its examples and code's scope sentence aside) is a sentence of the rule as they wrote it, its capitals and
    stops too."""

    def sentences(said: str) -> list[str]:          # each as the gate reads a lesson: its code a word, its trailer out
        bare = shape.TRAILER.sub("", WRAP.sub(" ", shape.EXAMPLE_TAGS.sub(" ", re.sub(r"`[^`]*`", "code", shape.one(said)))))
        return [y for y in (" ".join(y.split()) for y in shape.SENTENCES.split(bare.replace(SCOPED.format(job=rule["job"]), " ").strip())) if y]
    his, mine = sentences(rule["said"]), sentences(text)
    return bool(body := mine[1:]) and all(y in his for y in body)


def recorded(rule: dict[str, Any], text: str, daily: Path | None, seen: dict[Path, str] | None = None) -> set[str]:
    """What the gate's on-record finding on a lesson names, when each passage it names is the owner's own: WHOLE for a quote past the gate's
    words that the day log holds being, word for word, a whole quote of the rule or a whole reason on file for it (its "on_file",
    REASONS: the owner's words too); UNCITED for an example being one of the rule's own kept as its words, its own cite and gloss too. None
    when it names anything else (a quote of the model's own choosing, a part of the owner's quote or of a reason on file, another gloss past its words)."""
    lim = dict(budgets().get("shape") or {})
    cap, gloss = shape.threshold(lim, "lesson_quote_words", 8), shape.threshold(lim, *shape.GLOSS)
    said, his = (re.sub(r"`[^`]*`", "code", shape.one(y)) for y in (text, rule["said"]))     # each as the gate reads a lesson
    theirs = {words_of(m[1]) for m in shape.QUOTED.finditer(his)} | {words_of(y) for y in rule.get("on_file") or ()}
    mine = {y.strip() for y in shape.INSIDE.findall(his)}
    out = set[str]()
    for ex in shape.INSIDE.findall(said):
        if m := shape.SHOWN.match(ex.strip()):
            if len(m[2].split()) > gloss and ex.strip() not in mine:      # a gloss of the conversion's own past the gate's words counts
                return set()
            out |= {UNCITED} if len(m[2].split()) > gloss else set()     # the rule's own cite, its gloss whole as the rule has it
            continue
        quote = re.sub(r"^\s*(?:landed|bounced)\b[,:]?\s*", "", ex.strip(), flags=re.I)
        if len(quote.split()) > cap and shape.on_record(quote.strip(shape.QUOTES_ALL), daily, seen):
            if ex.strip() not in mine:
                return set()
            out.add(UNCITED)
    for m in shape.QUOTED.finditer(shape.TRAILER.sub("", shape.EXAMPLE_TAGS.sub(" ", said)).strip()):
        if len(m[1].split()) > cap and shape.on_record(m[1], daily, seen):
            if words_of(m[1]) not in theirs:
                return set()
            out.add(WHOLE)
    return out


def excused(rule: dict[str, Any], x: dict[str, Any], read: Any, daily: Path | None = None, seen: dict[Path, str] | None = None) -> dict[str, list[str]]:
    """Each gate finding on lesson `x` that only a change to a sentence or a quote of the owner's could meet -> the kinds it is listed as:
    UNLIFTED (a never whose rule gives no exception where it says it), WHOLE (the owner's quote kept whole), OWN (their own sentence kept),
    UNCITED (an example of theirs kept as its words), PAST (their words kept whole, past the length). Never sent to the model; each
    such lesson is carried as they wrote it and listed for the owner's side, and every other finding on it still counts."""
    text, out = f"{x['summary']} {x['body']}", dict[str, list[str]]()
    for g in x["gate"]:
        if open_finding(g) and unlifted(rule, text, read):
            out[g] = [UNLIFTED]
        elif g.startswith(RECORDS) and (k := recorded(rule, text, daily, seen)):
            out[g] = sorted(k)
        elif g.startswith(UNTAGGED) and own_sentences(rule, text):
            out[g] = [OWN]
        elif g.startswith(LONG) and past(rule, text):
            out[g] = [PAST]
    return out


def of_record(m: dict[str, Any], supplied: dict[str, list[tuple[str, str]]] | None = None) -> dict[str, Any]:
    """A rule as a proposals file's record holds it: its cite, job, words (its line's, trailer and marks out) and filing, so the
    apply works each excuse out again from the rule's own words; and its reasons on file, each one REASONS supplies for its
    cite today (`supplied`) that the record's rows for it hold too."""
    head, colon, said = m["text"][2:].partition(": ")
    tm = memory.TRAILER.search(m["text"])
    rows = {tuple(y.strip() for y in row.split("\t")[:2]) for row in m.get("reasons") or []}
    return {"cite": m["cite"], "job": m["job"], "text": m["text"], "filed": f"{tm[1]}{tm[2] or ''}" if tm else "",
            "said": re.sub(r" \[(?:rooms|on): [^\]]*\]", "", shape.TRAILER.sub("", said if colon else m["text"][2:])).strip(),
            "on_file": [w for at, w in (supplied or {}).get(m["cite"], []) if (at, w) in rows]}


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
    its words after its mark and day (an example given as the owner's, on a line of their own), else each quoted piece of it in order (a piece
    under four words only on the day it names); one no line passes, or that describes a shape, keeps its words, a cite of the rule's its gloss. Code's."""
    out: list[dict[str, str]] = []
    for ex in shape.INSIDE.findall(rule["said"]):
        mark, text = (m[0].capitalize() + ", ") if (m := shape.MARKED.search(ex)) else "", ex.strip()
        bare = HEAD.sub("", text).strip(shape.QUOTES_ALL + ",:;.")
        day = named(text, filed_at(rule.get("filed", "")))[0]
        pieces = [(y, his) for y, his in [(bare, False), *((y, given_his(text[:m.start()], *said_as(rule))) for m in shape.QUOTED.finditer(text)
                                                         for y in ELIDED.split(m[1]))] if len(y.split()) >= 4 or (day and y.strip())]
        if shape.SHOWN.match(text) or not bare:
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
         "an example left out for length", "a quote of his cut", "a fault the code finds", "another cause")    # why `--from` redoes a rule, the head's count
LOOPED = "the owner's side's finding comes back"   # a listed item: a rule redone with its findings shown and still dropped, kept
ASKED = "asked again by name"      # why `--from` redoes a rule the owner's side names (`--redo`): never kept, looped or not


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
    """What an earlier file's conversion of a rule holds that the code no longer writes, or a findings row it was made before. A
    cited example is old when the line it is cited to is not the line `pick` gives today, never for the gloss the model gave it
    (one whose cite is the rule's own, with no line, takes no gloss: by its form)."""
    shown_, text = examples(r, s.daily), " ".join(f"{x['summary']} {x['body']}" for x in m["lessons"])
    was = {d.get("said"): d for d in m["done"] if str(d["kind"]).startswith("example")}
    most = shape.threshold(dict(budgets().get("shape") or {}), "lesson_quote_words", 8)
    cut = quoted(r, m["lessons"], s.daily, most, [w for _, w in supplied_reasons(s).get(r["cite"], [])])[1]

    def moved(x: dict[str, str]) -> bool:     # cited then to a line `pick` no longer gives; one with no line, by its form
        d = was.get(x["said"]) or {}
        return d.get("kind") == "example cited" and (d.get("at") != x.get("at") or not x.get("at") and d["as"] != x["as"])
    return [y for y, hit in zip(STALE, (
        bool(found.get(r["cite"])) and m.get("found") != found[r["cite"]],
        any(d["kind"] == "exception added" for d in m["done"]),
        any(x not in r["text"] for x in re.findall(r"the words at daily/\S+", text)),
        any(moved(x) for x in shown_) or any(x not in {y["said"] for y in shown_} for x in was if (was[x] or {}).get("kind") == "example cited"),
        any(d["kind"] == "example left out" and "would pass its length" in str(d.get("why")) for d in m["done"]),
        any(" cuts his quote: " in f for f in cut),
        bool(checks(r, m["lessons"], m["done"], [{**x, "as": f"<example>{x['said']}</example>"} if x["said"] in refusals(m["done"]) else x
                                                 for x in shown_]) or [f for f in cut if " cuts his quote: " not in f]))) if hit]


def refusals(done: list[dict[str, Any]]) -> set[str]:
    """The examples whose cite the model refused (REFUSED_CITE), by their words: each placed as its words."""
    return {str(d.get("said")) for d in done if d["kind"] == REFUSED_CITE}


def carried(s: memory.Settings, rules: list[dict[str, Any]], path: Path, gate: Callable[[dict[str, Any]], list[str]],
            rows: dict[str, list[str]], found: dict[str, list[str]] | None = None, read: Any = None,
            why: dict[str, list[str]] | None = None, seen: dict[Path, str] | None = None, redo: Any = ()) -> dict[str, dict[str, Any]]:
    """cite -> the rule as the earlier proposals file `path` made it (a dry run's, from any folder or store), kept word for word
    with no model call: when that file has it under the same cite with the same words, each lesson of it passes the gate as an
    apply reads it today (`gate`), every sentence is accounted for, it lost no trigger, nothing of it was still failing after the
    last round (the reviewer's drop among them), no slug of it is retired, REASONS holds the same rows for its cite as then and
    it holds nothing `stale` names. A rule FINDINGS has rows for that was redone with them shown and still came back dropped is
    kept and listed, never redone again. `why`, filled: cite -> what made each other rule be redone (STALE). `seen`: the dry run's
    day logs as read once (shape.on_record), so the excuses read each day log once, not once for each quote. `redo`: the cites the
    owner's side names, each never kept, looped or not (ASKED), so a rule worked by hand is asked again."""
    rec, gone, found, why = loaded(path), set(lessons.retired(s.root)), found or {}, {} if why is None else why
    old, out = {m["cite"]: m for m in rec["rules"]}, dict[str, dict[str, Any]]()
    for r in rules:
        m = old.get(r["cite"])
        if r["cite"] in redo:
            why[r["cite"]] = [ASKED]
            continue
        if not m or m["text"] != r["text"] or m.get("reasons") != rows.get(r["cite"], []) or r["cite"] in rec["lost"] or any(
                x["slug"] in gone for x in m["lessons"]):
            why[r["cite"]] = [STALE[-1]]
            continue
        made = [{**x, "gate": gate(x)} for x in m["lessons"]]
        bad = stale(s, r, m, found)
        looped = bool(found.get(r["cite"])) and m.get("found") == found[r["cite"]] and any(   # the reviewer: a finding of theirs holds
            d["kind"] == "reviewer dropped" and re.search(r"\bfindings?\b", str(d.get("why"))) for d in m["done"])
        if not made or any(refused(x["gate"], NONE in rows.get(r["cite"], []), excused(r, x, read, s.daily, seen)) for x in made) \
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
    made, names, glosses, nocite = list[dict[str, Any]](), dict[int, list[int]](), dict[int, str](), dict[int, str]()
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
            if isinstance(e, dict) and e.get("cite") is False:      # the model refuses the line code cites it to: placed as its words
                nocite[n] = str(e.get("why") or "") or "no why given"
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
        if n in nocite and x.get("at"):
            done.append({"kind": REFUSED_CITE, "said": x["said"], "at": x["at"], "why": nocite[n]})
            x = shown_[n - 1] = {"kind": "example kept", "said": x["said"], "as": f"<example>{x['said']}</example>", "why": "its cite refused"}
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
                 and all(g.startswith(WANTING) or g in excused(rule, x, read, s.daily, seen) for g in x["gate"])
                 and any(g.startswith(WANTING) for g in x["gate"]) and not (isinstance(w, dict) and w.get("said"))]
    return {**{k: rule[k] for k in ("cite", "file", "line", "text", "job")}, "lessons": made, "done": done,
            "missing": accounted(rule, made, done), "faults": faults + wrong + checks(rule, made, done, shown_)}


def checks(rule: dict[str, Any], made: list[dict[str, Any]], done: list[dict[str, Any]], shown_: list[dict[str, str]]) -> list[str]:
    """What code finds wrong in a rule's lessons, each a finding sent back: an <example> no example of the rule is, or one of them
    a second time; an example of the rule in no lesson (left out with the model's why aside), each example counted once, by its
    place, so two with the same cited form are two; the caution the model named not the rule's own sentence, or in no lesson (a
    rewritten item never accounts for it); an exception or a seal (SEALS) given to a never whose own sentence in the rule gives
    none, or a seal anywhere, that the rule does not say."""
    out, placed = list[str](), set[int]()
    like = [re.compile(re.escape(x["as"][:-len("</example>")]) + (r"(?:, [^<]*)?</example>" if x.get("at") else "</example>")) for x in shown_]
    for i, x in enumerate(made, 1):
        for m in shape.INSIDE.finditer(x["body"]):
            if (n := next((n for n, rx in enumerate(like) if n not in placed and rx.fullmatch(m[0])), None)) is not None:
                placed.add(n)
            else:
                out.append(f'lesson {i} holds an example of the rule a second time: "{m[1].strip()}"' if any(rx.fullmatch(m[0]) for rx in like)
                           else f'lesson {i} holds an example the rule does not give: "{m[1].strip()}"')
    gone = {d["said"] for d in done if d["kind"] == "example left out" and "would pass its length" not in str(d.get("why"))}
    out += [f'example {n} of the rule is in no lesson: "{x["said"]}"' for n, x in enumerate(shown_, 1) if n - 1 not in placed and x["said"] not in gone]
    his = f" {words_of(shape.QUOTED.sub(' ', WRAP.sub(' ', shape.EXAMPLE_TAGS.sub(' ', rule['said']))))} "
    for i, x in enumerate(made, 1):     # S2b: an exception, or a seal, that the owner's never does not give in its own sentence
        bare = shape.QUOTED.sub(" ", shape.EXAMPLE_TAGS.sub(" ", f"{x['summary']} {x['body']}"))
        for y in shape.SENTENCES.split(bare):       # one the rule's own words, in their order, is the owner's, kept
            if (said := [*SEALS.finditer(y), *(shape.EXCEPTION.finditer(y) if sealed(rule, bare) else ())]) and f" {words_of(y)} " not in his:
                out.append(f'lesson {i} gives its never an exception the rule does not: carry it as he wrote it (its words: "{said[0][0]}")')
    for d in (d for d in done if d["kind"] == "caution to review"):
        if flat(d.get("said", "")) not in flat(rule["said"]):
            out.append(f'the caution is not the rule\'s own sentence, word for word: "{d.get("said", "")}"')
        elif not any(flat(d["said"]) in flat(f"{x['summary']} {x['body']}") for x in made):
            out.append(f'the rule\'s caution is in no lesson: "{d["said"]}"')
    return out


def refused(gate: list[str], none: bool, ok: Any = ()) -> bool:
    """A lesson's gate findings refuse it: any finding, but for a rule whose cite has a NONE row, a finding besides a reason's, and
    a finding besides each one `ok` holds (excused: one only a change to the owner's words could meet, listed); `ok` True, as run 4 gave
    it, is a lesson whose never is unlifted: its exception finding alone."""
    ok = {g for g in gate if open_finding(g)} if ok is True else () if ok is False else ok
    return bool(gate) and not all((none and g.startswith(WANTING)) or g in ok for g in gate)


def findings(m: dict[str, Any], none: bool = False, rule: dict[str, Any] | None = None, read: Any = None, daily: Path | None = None,
             seen: dict[Path, str] | None = None) -> list[str]:
    """What still fails in one rule's lessons, word for word: each lesson's gate refusal, each sentence of the rule not accounted
    for and each reason refused. A refusal for no reason goes back too, since a reason a reader sees ("which slows the page")
    is none to the gate; the prompt says to give none where none is on file. `none`: the rule has a NONE row, so a lesson
    refused for a reason alone stays as it is. A finding only a change to the owner's words could meet (excused) is never sent."""
    ok = [excused(rule, x, read, daily, seen) if rule else {} for x in m["lessons"]]
    return [f'lesson {i} ("{x["summary"]}"): {g}' + (OWN_HINT if g.startswith(UNTAGGED) else "") for i, (x, o) in enumerate(zip(m["lessons"], ok), 1)
            if refused(x["gate"], none, o) for g in x["gate"] if g not in o] + [f'not accounted for: "{x}"' for x in m["missing"]] + \
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
           *([f"Asked again by name, never kept: {len(rec['from']['asked'])} rule{'s' * (len(rec['from']['asked']) != 1)}, "
              f"{', '.join(rec['from']['asked'])}."] if (rec.get("from") or {}).get("asked") else []),
           *(f"Listed as {HEADS[k]} and not counted in the verdict: {len(rec[key])} lesson{'s' * (len(rec[key]) != 1)}, "
             f"{', '.join(f'{lessons.DIR}/{x}.md' for x in rec[key])}." for k, key in EXCUSES.items() if rec.get(key)),
           *(f"Cites the model refused, each example placed as its words and listed with the model's why: {n}, in "
             f"{', '.join(r['cite'] for r in rec['rules'] if any(d['kind'] == REFUSED_CITE for d in r['done']))}."
             for n in [sum(d["kind"] == REFUSED_CITE for r in rec["rules"] for d in r["done"])] if n),
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
            model: Callable[[str, str], str] | None = None, read: Any = None, earlier: Path | None = None, redo: Any = ()) -> Path:
    """The dry run: the proposals file written, nothing else. Its path. `read`: the pages the gate reads, else READ's. `earlier`
    (`--from FILE`): each rule that passed there kept as it made it (carried), every other rule proposed as in a fresh run, its
    lessons taking no slug a kept one holds; the trigger test and the verdict read all of them. `redo` (`--redo CITE`, with
    `earlier`): the rules the owner's side asks again by name, never kept; a name that is no rule's cite is refused first."""
    now = time.time() if now is None else now
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    read = shape.pages(READ) if read is None else read
    acronyms = shape.install(dict(conf.get("docs") or {}))[1]
    rules, seen, taken = originals(s), dict[Path, str](), set(lessons.retired(s.root))
    if redo and earlier is None:
        raise RuntimeError("a rule is asked again by name only of an earlier file: give --from FILE; nothing run")
    if bad := [c for c in dict.fromkeys(redo) if c not in {r["cite"] for r in rules}]:
        raise RuntimeError(f"no live rule has the cite {', '.join(bad)}: name each rule by its cite in the store; nothing run")
    supplied, weights, rows = supplied_reasons(s), memory.rarities([r["text"] for r in rules]), reason_rows(s)
    for r in rules:
        r["keeps"], r["his"], r["on_file"] = keeps(r["text"], weights), owner_names(conf), [w for _, w in supplied.get(r["cite"], [])]
        r["said_by"] = owner_words(conf)
    found, redone = finding_rows(s), dict[str, list[str]]()
    kept = carried(s, rules, earlier, lambda x: (memory.unshaped(x["job"], f"{x['summary']} {x['body']}".strip(), acronyms, s, read,
                                                                 seen) or {}).get("fix", []), rows, found, read, redone, seen, set(redo)) if earlier else {}
    taken |= {x["slug"] for m in kept.values() for x in m["lessons"]}
    off = [r for r in rules if r["cite"] not in kept and (memory.unshaped(r["job"], r["said"], acronyms, s, read, seen) or found.get(r["cite"]))]
    props = model_pass(off, model or (lambda p, tools: curate.ask(private, state, dict(conf.get("curate", {})), p, tools)),
                       lambda r, x: findings(build(s, r, x, read, seen, set(taken), acronyms, supplied), NONE in rows.get(r["cite"], []), r, read,
                                             s.daily, seen),
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
    listed, kinds = dict[str, str](), {k: dict[str, str]() for k in EXCUSES}   # slug -> its rule's cite: no reason on file, as the
    bare = list[dict[str, Any]]()                           # owner's side says; each excused kind; refused only for a reason, counted
    for r, m in zip(rules, made):
        m["done"], none = [d for d in m["done"] if d["kind"] not in (LISTED, *EXCUSES)], NONE in rows.get(m["cite"], [])
        for x in (x for x in m["lessons"] if x["gate"]):
            if refused(x["gate"], none, ok := excused(r, x, read, s.daily, seen)):
                bare += [x] * (all(g.startswith(WANTING) or g in ok for g in x["gate"]) and any(
                    d["kind"] == "no reason on file" and d.get("said", x["summary"]) == x["summary"] for d in m["done"]))
                continue
            if none and any(g.startswith(WANTING) for g in x["gate"]):
                listed[x["slug"]] = m["cite"]
                m["done"].append({"kind": LISTED, "said": x["summary"], "why": f"{lessons.DIR}/{x['slug']}.md, a {NONE} row in {curate.PROPOSALS}/"
                                  f"{REASONS}: carried as it stands and named, since a reason is never invented; not counted in the verdict"})
            for k in dict.fromkeys(k for g in x["gate"] for k in ok.get(g, [])):
                kinds[k][x["slug"]] = m["cite"]
                m["done"].append({"kind": k, "said": x["summary"], "why": f"{lessons.DIR}/{x['slug']}.md: " + WHYS[k]})
    why = [f"{n} lesson{'s' * (n != 1)} the gate refuses" for n in [sum(bool(x["gate"]) for m in made for x in m["lessons"]) - len(bare)
                                                                    - len(set(listed).union(*kinds.values()))] if n]
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
           **{key: kinds[k] for k, key in EXCUSES.items()}, **({"from": {"path": str(earlier), "kept": len(kept), "redone": len(rules) - len(kept), "why": {
               k: n for k in (*STALE, ASKED) if (n := sum(k in v for c, v in redone.items() if c not in kept))}, **({"asked": [
               r["cite"] for r in rules if r["cite"] in set(redo)]} if redo else {})}} if earlier else {})}
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
    seen, rows, listed, have = dict[Path, str](), reason_rows(s), dict(rec.get("no_reason") or {}), supplied_reasons(s)  # a listed lesson passes for its
    lists = {k: dict(rec.get(key) or {}) for k, key in EXCUSES.items()}    # reason alone while its rule's NONE row is there, and for
    for m in rec["rules"]:                             # each excused finding alone, worked out again from the rule's own words
        for x in m["lessons"]:
            gate = (memory.unshaped(x["job"], f"{x['summary']} {x['body']}".strip(), acronyms, s, rec["read"], seen) or {}).get("fix", [])
            ok = excused({**of_record(m, have), "his": owner_names(conf), "said_by": owner_words(conf)}, {**x, "gate": gate}, rec["read"], s.daily, seen)
            if bad := [k for k, v in lists.items() if x["slug"] in v and k not in {k for y in ok.values() for k in y}]:
                raise RuntimeError(f"{x['slug']} is listed as {bad[0]}, which its rule's words do not bear: run the dry run again; nothing applied")
            if refused(gate, x["slug"] in listed and NONE in rows.get(listed[x["slug"]], []),
                       {g for g, y in ok.items() if any(x["slug"] in lists[k] for k in y)}):
                raise RuntimeError(f"{x['slug']} no longer passes the gate: run the dry run again; nothing applied")
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
        if every := dict.fromkeys([*listed, *(x for v in lists.values() for x in v)]):     # one list, each row saying what is missing
            write_private(s.root / lessons.NO_REASON, "".join(f"{x}\t{rec['day']}\t{listed.get(x) or next(v[x] for v in lists.values() if x in v)}\t"
                                                              + " and ".join(["a reason"] * (x in listed) + [lessons.MISSING[key] for k, key in
                                                                                                             EXCUSES.items() if x in lists[k]]) + "\n" for x in every))
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


def main(private: Path, state: Path, apply_to: str = "", undo_day: str = "", start_from: str = "", redo: tuple[str, ...] = ()) -> int:
    """`yerk curate --lessons` (the dry run; `--from FILE`, from an earlier proposals file, and `--redo CITE`, a rule of it asked
    again by name), `--lessons-apply FILE`, `--lessons-undo DAY`; exits 1 on a refusal, 2 when it could not run."""
    try:
        conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
        s, more = memory.settings(private, state, dict(conf.get("memory", {}))), dict[str, Any]({"redo": tuple(redo)} if redo else {})
        out = undo(s, undo_day) if undo_day else apply(private, state, s, Path(apply_to)) if apply_to else [
            f"proposals: {dry_run(private, state, s, earlier=Path(start_from) if start_from else None, **more)}; nothing else written"]
    except RuntimeError as e:
        print(f"refused: {e}")
        return 1
    except (Exception, SystemExit) as e:
        print(f"curate --lessons failed: {type(e).__name__}: {str(e)[:160]}")
        return 2
    print("\n".join(out))
    return 0
