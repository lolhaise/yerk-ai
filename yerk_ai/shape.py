"""The rule-shape check (`yerk doctor`, `yerk docs shape`): it scores every rule file a room loads (yerk_ai/mechanics.md, its
persona, rulebook and voice, the install's rules file) against Anthropic's prompting guide for Claude, which `yerk docs`
downloads to docs/claude/prompting-guide.md in the state folder and which asks for what to do with its reason, examples inside
each thing said once and calm wording. Five counts, each one exact rule so a person can redo it with grep:
- no why: a line holding never, no, not, nothing, cannot or an n't word, and none of because, since, so that, so
  (bar "say so", "do so", "or so") or why;
- loud: a line holding a word of four or more capitals that is no known acronym or name (ACRONYMS, plus [docs]
  acronyms in yerkd.toml) and no file name (CLAUDE.md);
- untagged: a line outside <example> tags holding a thumbs emoji, a lead like `He says ...:` or `Asked "...":` (a says,
  asks or writes word in its first five, then a quote or a closing colon), or a quoted sentence of three words or more
  beside the word landed or bounced;
- alarm: a line holding 🔴 or ⛔, unless it also holds a checklist or state mark (☑ ☐ ✅ 🟢 🟠 ⚪), where ⛔ is the
  blocked mark;
- twice: a topic of TOPICS (plus [docs] topics) whose words are in two of the files one room loads, once per topic.
A line is any line but a blank, a heading, an HTML comment, front matter and a fenced block. A line inside <example>
tags is an example and no count reads it. Code in backticks, quoted text and a rules line's own trailer (its room, time
and message id) are dropped before the counts read a line, except that `untagged` reads the quotes; a superseded rules
line is never loaded, so never read. Thresholds: budgets.toml [shape], per file, and for twice per install."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import REPO, flag
from .context import MECHANICS, layer_dir
from .memory import EXAMPLE, FRONT, RULES, Settings, cite
from .mentions import OLD

COUNTS = ("no why", "loud", "untagged", "alarm")      # per line; "twice" is per room
KEYS = {"no why": "negatives_without_why", "loud": "shouted", "untagged": "untagged_examples", "alarm": "alarm_marks",
        "twice": "duplicated_topics"}                 # budgets.toml [shape]
NAMES = {"no why": "a negative with no why", "loud": "a shouted word", "untagged": "an example outside <example> tags",
         "alarm": "an alarm mark"}
NEG = re.compile(r"\b(?:never|no|not|nothing|cannot)\b|\b\w+n['’]t\b", re.I)
WHY = re.compile(r"\b(?:because|since|why)\b|\bso that\b|(?<!\bsay )(?<!\bdo )(?<!\bor )\bso\b", re.I)
LOUD = re.compile(r"(?<![\w./-])[A-Z]{4,}(?![\w-]|\.[A-Za-z])")
ACRONYMS = frozenset("""ASCII ASAP CLAUDE HTML HTTP HTTPS IMAP JSON JSONL NULL OAUTH POSIX README SIGHUP SIGINT SIGKILL
SIGTERM SMTP STDERR STDOUT TODO TOML UNIX UUID YAML AGENTS MEMORY SOUL USER TOOLS""".split())
THUMBS = re.compile("[\U0001F44D\U0001F44E]")
LEAD = re.compile(r"^(?:[-*>]\s*)?(?:\*\*)?(?:[\w'’]+\s+){0,4}?(?:says|said|asks|asked|writes|wrote)\b", re.I)
QUOTED = re.compile(r"[\"“]([^\"”]+)[\"”]")
MARKED = re.compile(r"\b(?:landed|bounced)\b", re.I)
ALARM = re.compile("\U0001F534|⛔")
LEGEND = re.compile("[☑☐✅\U0001F7E2\U0001F7E0⚪]")
OPEN, CLOSE = re.compile(r"<example(?:\s[^>]*)?>", re.I), re.compile(r"</example>", re.I)
TRAILER = re.compile(r"\s*\([^()]*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[^()]*\)\s*(?:\[m:[^\]]*\])?\s*$")
STAMP = re.compile(r"\(([^()]*), (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})\)\s*(?:\[m:[^\]]*\])?\s*$")
# A topic a rule file may say, and the words that say it: one topic in two files a room loads is said twice, and the
# two copies drift. Small on purpose; [docs] topics in yerkd.toml adds an install's own (name = "regex").
TOPICS = {
    "answer first": r"\bthe answer first\b|\banswer (?:the question )?first\b|\bbefore anything else\b|\bfirst (?:line|sentence) answers\b",
    "clock and dates": r"\[clock\]|\bclock line\b|\brelative times?\b|\braw timestamps?\b",
    "file paths in replies": r"\bfile paths?\b",
    "tables": r"\btables?\b",
    "em dashes": r"\bem[ -]?dash",
    "read before a claim": r"\bread (?:it |the file )?(?:first|before)\b|\bbefore you quote\b|\bneeds a (?:run or )?read\b",
    "promises": r"\bpromises?\b|\bopen commitments\b",
    "background work": r"\bworkers?\b|\bbackground work\b",
    "attachments": r"\battachments?\b",
    "not on file": r"\bnot on file\b|\b(?:do not|don't) know\b",
    "state first": r"\bstate first\b|\bglanceable\b",
    "memory before a fact": r"\brecall\b|\bdaily notes\b|\bearlier conversation\b|\bremembered fact\b",
    "drafts for others": r"\bdrafts?\b.*\bshown\b|\bsent to another person\b|\bin (?:his|her|their|the owner's) name\b",
}


def lines(text: str, rules: bool = False) -> list[tuple[int, str, bool]]:
    """(line number, text, inside <example> tags) of each line the counts read."""
    out, inside, fence, comment = [], False, False, False
    rows = text.split("\n")
    skip = m.group(0).count("\n") if (m := FRONT.match(text)) else 0
    for n, raw in enumerate(rows, 1):
        s = raw.strip()
        if n <= skip:
            continue
        if s.startswith(("```", "~~~")):
            fence = not fence
            continue
        if comment or s.startswith("<!--"):
            comment = "-->" not in s
            continue
        bare = re.sub(r"`[^`]*`", " ", s)                     # a tag named in backticks is a name, not a tag
        ex = inside or bool(OPEN.search(bare))
        shut = [m.start() for m in CLOSE.finditer(bare)]
        inside = ex and not (shut and shut[-1] > max((m.start() for m in OPEN.finditer(bare)), default=-1))
        if fence or not s or s.startswith("#") or (rules and OLD in s):
            continue
        out.append((n, raw.rstrip(), ex))
    return out


def plain(line: str, rules: bool = False, quotes: bool = False) -> str:
    """A line as the counts read it: no code in backticks, no rules trailer, and no quoted text unless `quotes`."""
    line = re.sub(r"`[^`]*`", " ", TRAILER.sub("", line) if rules else line)
    return line if quotes else QUOTED.sub(" ", line)


def tripped(line: str, rules: bool = False, acronyms: frozenset[str] = ACRONYMS) -> dict[str, list[str]]:
    """Per count, in COUNTS' order, the words of one line of text that put it under that count (none: it is not)."""
    p, q = plain(line, rules), plain(line, rules, quotes=True)
    said = [m.group(0) for m in QUOTED.finditer(q) if len(m.group(1).split()) >= 3]
    lead = LEAD.search(q.strip()) if QUOTED.search(q) or q.rstrip().endswith(":") else None
    marked = MARKED.findall(q) if said else []
    return {"no why": [] if WHY.search(p) else NEG.findall(p), "loud": [w for w in LOUD.findall(p) if w not in acronyms],
            "untagged": THUMBS.findall(q) + ([lead.group(0)] if lead else []) + (said + marked if marked else []),
            "alarm": [] if LEGEND.search(line) else ALARM.findall(p)}


def offenses(line: str, rules: bool = False, acronyms: frozenset[str] = ACRONYMS) -> list[str]:
    """The per-line counts one line of text falls under, in COUNTS' order."""
    return [k for k, v in tripped(line, rules, acronyms).items() if v]


def filing(line: str, acronyms: frozenset[str] = ACRONYMS) -> dict[str, list[str]]:
    """A rule line about to be filed, read as the counts read a rules line with its examples out (a line holding one is
    never read by score): each count it falls under and the words that put it there. memory.rule files none that has one."""
    return {k: list(dict.fromkeys(v)) for k, v in tripped(EXAMPLE.sub("", line), True, acronyms).items() if v}


def install(docs: dict[str, Any]) -> tuple[dict[str, str], frozenset[str]]:
    """The topics and acronyms the counts read on an install: TOPICS and ACRONYMS with yerkd.toml's [docs] topics and acronyms."""
    return ({**TOPICS, **{str(k): str(v) for k, v in (docs.get("topics") or {}).items()}},
            ACRONYMS | {str(x).upper() for x in docs.get("acronyms") or []})


def score(path: Path, rules: bool = False, topics: dict[str, str] | None = None,
          acronyms: frozenset[str] = ACRONYMS) -> dict[str, Any]:
    """One file: the lines read, each count's offending lines as (line number, text), and the topics it says."""
    rx = {k: re.compile(v, re.I) for k, v in (topics or TOPICS).items()}
    out: dict[str, Any] = {"lines": 0, "topics": set(), **{k: [] for k in COUNTS}}
    for n, raw, ex in lines(path.read_text(encoding="utf-8", errors="replace"), rules):
        if ex:
            continue
        out["lines"] += 1
        for k in offenses(raw, rules, acronyms):
            out[k].append((n, raw.strip()))
        said = QUOTED.sub(" ", TRAILER.sub("", raw) if rules else raw)          # code in backticks names a topic too
        out["topics"] |= {t for t, r in rx.items() if r.search(said)}
    return out


def files(private: Path, room: dict[str, Any], rules: Path) -> list[tuple[str, Path]]:
    """(kind, path) of each rule file the room loads that is there: the mechanics, persona, rulebook, voice, rules file."""
    base = layer_dir(private, room)
    out = [("mechanics", MECHANICS), *((k, (base / str(room[k])).resolve()) for k in ("persona", "rulebook", "voice") if room.get(k))]
    return [(k, p) for k, p in out + ([] if flag(room.get("sealed")) else [("rules", rules)]) if p.is_file()]


def label(private: Path, m: Settings, kind: str, p: Path) -> str:
    """A rule file as a finding names it: the mechanics and the rules file by name and cite, any other under its folder."""
    if kind == "mechanics":
        return "mechanics.md"
    if kind == "rules":
        return cite(m, p)
    return next((p.relative_to(b).as_posix() for b in (private.resolve(), REPO) if p.is_relative_to(b)), p.name)


def check(private: Path, m: Settings, rooms: dict[str, dict[str, Any]], docs: dict[str, Any], now: float) -> dict[str, Any]:
    """Every room's rule files scored: per file its counts, per room the topics said twice, and the rules file's lines of
    the last day that miss the shape, by cite."""
    topics, acronyms = install(docs)
    rules, scored, per_room, twice = m.root / RULES, dict[str, dict[str, Any]](), dict[str, dict[str, Any]](), dict[str, list[str]]()
    for name, conf in rooms.items():
        mine = []
        for kind, p in files(private, conf, rules):
            if str(p) not in scored:                          # a file once, however many rooms load it; a label once too
                lab, taken = label(private, m, kind, p), {f["label"] for f in scored.values()}
                lab = next((x for x in [lab, *("/".join(p.parts[-n:]) for n in range(2, len(p.parts)))] if x not in taken), str(p))
                scored[str(p)] = {"kind": kind, "label": lab, **score(p, kind == "rules", topics, acronyms)}
            mine.append(str(p))
        said = {t: [scored[f]["label"] for f in mine if t in scored[f]["topics"]] for t in topics}
        per_room[name] = {"files": mine, "twice": {t: fs for t, fs in said.items() if len(fs) > 1}}
        for t, fs in per_room[name]["twice"].items():
            twice.setdefault(t, fs)
    fresh = []
    if rules.is_file():
        for n, raw, ex in lines(rules.read_text(encoding="utf-8", errors="replace"), rules=True):
            at = STAMP.search(raw)
            if not ex and at and now - datetime.fromisoformat(at.group(2)).timestamp() <= 86400 and (o := offenses(raw, True, acronyms)):
                fresh.append((f"{cite(m, rules)}:{n}", o))
    return {"files": scored, "rooms": per_room, "twice": twice, "fresh": fresh}


def totals(r: dict[str, Any]) -> dict[str, int]:
    return {**{k: sum(len(f[k]) for f in r["files"].values()) for k in COUNTS}, "twice": len(r["twice"]),
            "lines": sum(f["lines"] for f in r["files"].values())}


def findings(r: dict[str, Any], limits: dict[str, Any]) -> list[str]:
    """The doctor's lines: a file whose count passes its [shape] threshold, the topics said twice past theirs, and each
    rules line of the last day that misses the shape (a room may file any line; this says which to rewrite)."""
    def cap(k: str) -> int:
        v = limits.get(KEYS[k], 0)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0
    out = []
    for f in r["files"].values():
        if over := [k for k in COUNTS if len(f[k]) > cap(k)]:
            first = min(n for k in over for n, _ in f[k])
            out.append(f"rule shape {f['label']}: {', '.join(f'{len(f[k])} {k}' for k in over)}, first line {first}")
    if len(r["twice"]) > cap("twice"):
        t, fs = next(iter(r["twice"].items()))
        out.append(f"rule shape: {len(r['twice'])} topic{'s' * (len(r['twice']) > 1)} said twice, first {t} in {' and '.join(fs[:2])}")
    out += [f"rule shape {c}: filed in the last day with {' and '.join(NAMES[k] for k in o)}; file it again in the shape"
            for c, o in r["fresh"]]
    return out


def report(name: str, r: dict[str, Any], limits: dict[str, Any], at: str) -> str:
    """One room's shape report, Markdown: its files' counts and first three lines each, its topics said twice."""
    out = [f"# Rule shape: {name}, {at}", "",
           "What the doctor's rule-shape check found in the rule files this room loads, scored against "
           "docs/claude/prompting-guide.md. Each count's rule: `yerk docs shape --rules`. "
           f"Thresholds (budgets.toml [shape]): {', '.join(f'{k} {limits.get(v, 0)}' for k, v in KEYS.items())}.", ""]
    for key in r["rooms"][name]["files"]:
        f = r["files"][key]
        out += [f"## {f['label']}: {f['lines']} line{'s' * (f['lines'] != 1)}, " + ", ".join(f"{len(f[k])} {k}" for k in COUNTS), ""]
        out += [f"- {k}, line {n}: {t[:200]}" for k in COUNTS for n, t in f[k][:3]] + ([""] if any(f[k] for k in COUNTS) else [])
    tw = r["rooms"][name]["twice"]
    out += [f"## Topics said twice: {len(tw)}", "", *[f"- {t}: {', '.join(fs)}" for t, fs in tw.items()]]
    return "\n".join(out).rstrip() + "\n"


def rules_text() -> str:
    """Each count's exact rule, from this module's own description, for `yerk docs shape --rules`."""
    return (__doc__ or "").split("grep:\n", 1)[-1]


# A lesson in the Claude docs' shape (2026-11-09, on the owner's word: always written the way the Claude docs say). Each check
# derives from one sentence of a page `yerk docs` fetches to <state>/docs/claude/<slug>.md: PINS holds the page's slug and
# the sentence as the page says it, so `pinned` can tell when the page moved, and every refusal quotes the sentence off
# the test reads (`it`). Back as plain text with one more line for/the_check_while.a second entry waits in the
# run files a need naming it. Thresholds: budgets.toml [shape], the lesson_* keys.
FABLE, FABLE1, CODE_MEMORY = "prompting-fable-5", "prompting-fable-5-1", "code-memory"
PINS = {
    "lesson": (FABLE, "Store one lesson per file with a one-line summary at the top."),
    "why": (FABLE, "Record corrections and confirmed approaches alike, including why they mattered."),
    "on record": (FABLE, "Don't save what the repo or chat history already records; update an existing note rather than "
                         "creating a duplicate; delete notes that turn out to be wrong."),
    "brief": (FABLE, "Instruction-following is improved enough that you can steer most behaviors with a brief instruction "
                     "rather than enumerating each behavior by name."),
    "reason": (FABLE, "Claude Fable 5 tends to perform better when the prompt states the intent behind a request: context lets "
                      "it connect the task to relevant information rather than inferring intent on its own."),
    "reader": (FABLE, "Your final summary is different: it's for a reader who didn't see any of that."),
    "density": (FABLE1, "In some cases, though, its prose is denser than Claude Fable 5’s: sentences run longer and there are "
                        "fewer paragraph breaks."),
    "index": (CODE_MEMORY, "Example note 000 says the `Sample.item`, moved to the 00Next, step and the, test reads it back "
                           "starts the example note."),
}
# Each lesson check, the pin it enforces, its threshold key in [shape] and its default (a findings count's is zero).
LESSON = {"enumerated": ("brief", "lesson_cases", 3), "summary": ("lesson", "lesson_summary_chars", 140),
          "no reason": ("reason", "lesson_why", 0), "long": ("brief", "lesson_body_chars", 600),
          "on record": ("on record", "lesson_quote_words", 8), "dense": ("density", "lesson_sentence_words", 40)}
GLOSS = ("lesson_gloss_words", 6)          # a cited example's gloss, at most
CASE = re.compile(r"(?:^|[.;:!?]\s+|\(\w\)\s*|\b\d[.)]\s+)(?:when|whenever|if|for)\b", re.I)    # a case's head, a clause's first word
NUMBERED = re.compile(r"(?:^|\s)(?:\(\d{1,2}\)|\d{1,2}[.)]|\([a-h]\))\s")                          # (1) 1. 1) (a)
CITED = re.compile(r"\b[\w.-]+(?:/[\w.-]+)*\.md:\d+\b")                                            # daily/2026-11-09.md:41
SHOWN = re.compile(r"^\s*(?:landed|bounced)\b[,:]?\s*(" + CITED.pattern + r")[\s,;:.-]*(.*)$", re.I | re.S)
SENTENCES = re.compile(r"(?<=[.!?])[\"”)]?\s+(?=[\"“(<A-Z0-9])")
INSIDE = re.compile(r"<example(?:\s[^>]*)?>(.*?)</example>", re.I | re.S)


def page(docs: Path | None, slug: str) -> str:
    """A fetched page's text, docs.py's <state>/docs/claude/<slug>.md; "" when it is not fetched."""
    p = docs / "claude" / f"{slug}.md" if docs else None
    return p.read_text(encoding="utf-8", errors="replace") if p and p.is_file() else ""


def flat(text: str) -> str:
    """Text as a pin is matched: one space for any run of white space, straight quotes for curly ones."""
    return " ".join(text.translate(str.maketrans("‘’“”", "''\"\"")).split())


def pinned(docs: Path | None) -> list[tuple[str, str, str]]:
    """(pin, slug, sentence) of each pin its fetched page no longer holds word for word; a page not fetched yet is none."""
    return [(k, slug, said) for k, (slug, said) in PINS.items() if (text := page(docs, slug)) and flat(said) not in flat(text)]


def sentence(pin: str, docs: Path | None = None) -> str:
    """A pin's sentence as its fetched page says it, quoted with its cite (docs/claude/<slug>.md); the pin's words before a fetch."""
    slug, said = PINS[pin]
    text, want = flat(page(docs, slug)), flat(said)
    return f'"{text[i:i + len(want)] if (i := text.find(want)) >= 0 else said}" (docs/claude/{slug}.md)'


def index_limits(docs: Path | None = None) -> tuple[int, int]:
    """The index's limits, lines and bytes, read off the code-memory sentence a session start loads by (200 lines, 25KB)."""
    said = sentence("index", docs)
    n, kb = re.search(r"(\d+) lines", said), re.search(r"(\d+)\s*KB", said)
    return int(n[1]) if n else 200, int(kb[1]) * 1000 if kb else 25_000


def threshold(limits: dict[str, Any], key: str, default: int) -> int:
    v = limits.get(key, default)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else default


def on_record(passage: str, daily: Path | None, seen: dict[Path, str] | None = None) -> str:
    """The day-log line holding `passage` word for word (spaces and curly quotes aside), as daily/<day>.md:<line>; "" for none.
    `seen`: each day log as read once, its lines flattened, kept for the next passage (the doctor's whole-store run)."""
    want = flat(passage).lower()
    for p in sorted(daily.glob("*.md"), reverse=True) if daily and daily.is_dir() and want else ():
        text = seen.get(p) if seen is not None else None
        if text is None:
            text = "\n".join(flat(x).lower() for x in p.read_text(encoding="utf-8", errors="replace").splitlines())
            if seen is not None:
                seen[p] = text
        if (i := text.find(want)) >= 0:
            return f"daily/{p.name}:{text.count(chr(10), 0, i) + 1}"
    return ""


def lesson(job: str, text: str, daily: Path | None = None, limits: dict[str, Any] | None = None,
           seen: dict[Path, str] | None = None) -> dict[str, list[str]]:
    """One rule as one lesson, as the Claude docs shape it: each LESSON check it misses and the words that miss it.
    enumerated: lesson_cases or more case heads (when, whenever, if or for opening a clause) or numbered cases (1. (2) (a));
    summary: the first sentence, the summary, over lesson_summary_chars (a body after it is the lesson's detail, when it has any);
    no reason: no because, since, so that, so or why anywhere in it;
    long: the body after the summary over lesson_body_chars, examples' cites and file:line cites not counted;
    on record: a quoted passage or an example of more than lesson_quote_words words that a day log holds (cite that line);
               an example of the cited form (Landed or Bounced, a cite) with a gloss over lesson_gloss_words words;
    dense: a sentence over lesson_sentence_words words. Code in backticks is no sentence and no case."""
    lim = limits or {}
    cap = {k: threshold(lim, key, d) for k, (_, key, d) in LESSON.items()}
    said = re.sub(r"`[^`]*`", "code", one(text))
    prose = TRAILER.sub("", EXAMPLE_TAGS.sub(" ", said)).strip()
    out: dict[str, list[str]] = {}
    heads = [m.group(0).strip(" .;:") for m in CASE.finditer(prose)] + [m.group(0).strip() for m in NUMBERED.finditer(prose)]
    if len(heads) >= max(cap["enumerated"], 2):
        out["enumerated"] = [f"{len(heads)} cases ({', '.join(dict.fromkeys(h.split()[-1] for h in heads))})"]
    first, *rest = SENTENCES.split(prose, maxsplit=1) or [""]
    body = rest[0] if rest else ""             # none: the summary is the whole lesson, as a one-sentence rule with its reason is
    if len(first) > cap["summary"]:
        out["summary"] = [f"a first sentence of {len(first)} characters"]
    if not WHY.search(QUOTED.sub(" ", prose)):
        out["no reason"] = ["no because, since, so that or why"]
    if len(CITED.sub("", body)) > cap["long"]:
        out["long"] = [f"a body of {len(CITED.sub('', body))} characters"]
    record = []
    for ex in INSIDE.findall(said):
        if m := SHOWN.match(ex.strip()):
            if len(m[2].split()) > threshold(lim, *GLOSS):
                record.append(f"a gloss of {len(m[2].split())} words beside {m[1]}")
            continue
        quote = re.sub(r"^\s*(?:landed|bounced)\b[,:]?\s*", "", ex.strip(), flags=re.I)
        if len(quote.split()) > cap["on record"] and (at := on_record(quote.strip(QUOTES_ALL), daily, seen)):
            record.append(f"an example the day log holds at {at}")
    for m in QUOTED.finditer(prose):
        if len(m[1].split()) > cap["on record"] and (at := on_record(m[1], daily, seen)):
            record.append(f"a quote the day log holds at {at}")
    if record:
        out["on record"] = record
    long_ones = [s for s in SENTENCES.split(CITED.sub("cite", prose)) if len(s.split()) > cap["dense"]]
    if long_ones:
        out["dense"] = [f"a sentence of {len(s.split())} words" for s in long_ones[:3]]
    return out


def one(text: str) -> str:
    return " ".join(text.split())


EXAMPLE_TAGS = re.compile(r"\s*<example(?:\s[^>]*)?>.*?</example>", re.I | re.S)
QUOTES_ALL = "\"“”'‘’ "


def lessons_of(path: Path) -> list[tuple[int, str, str]]:
    """(line number, job, text) of each live rule line of a rules file (RULES, TOPIC_RULES): a bullet `- job: text`, its trailer
    and scope marks out, none superseded."""
    out = []
    for n, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else [], 1):
        if raw.startswith("- ") and OLD not in raw:
            head, colon, said = raw[2:].partition(": ")
            said = re.sub(r" \[(?:rooms|on): [^\]]*\]", "", TRAILER.sub("", said if colon else raw[2:]))
            out.append((n, head if colon else "", said))
    return out


def store(m: Settings, limits: dict[str, Any], docs: Path | None = None) -> dict[str, Any]:
    """The whole rule store against the lesson shape: each live rule of RULES and TOPIC_RULES that misses a LESSON check, by
    cite with its checks, and RULES, the index a session start loads, against the code-memory limits (lines, bytes)."""
    from .memory import TOPIC_RULES
    found, seen = list[tuple[str, list[str]]](), dict[Path, str]()      # seen: each day log read once for every rule
    for name in (RULES, TOPIC_RULES):
        p = m.root / name
        for n, job, text in lessons_of(p):
            if bad := lesson(job, text, m.daily, limits, seen):
                found.append((f"{cite(m, p)}:{n}", list(bad)))
    p, (most, size) = m.root / RULES, index_limits(docs)
    rows, have = (p.read_text(encoding="utf-8", errors="replace").splitlines(), p.stat().st_size) if p.is_file() else ([], 0)
    over = [f"{len(rows)} lines, past {most}"] * (len(rows) > most) + [f"{have:,} bytes, past {size:,}"] * (have > size)
    return {"lessons": found, "index": over, "moved": pinned(docs)}


def store_findings(r: dict[str, Any], limits: dict[str, Any], docs: Path | None = None) -> list[str]:
    """The doctor's daily lines for the store: a pin its page moved off, the index past code-memory's limits, and the lessons that
    miss the shape past lesson_findings (zero), the first named with its checks and the docs sentence the first check enforces."""
    out = [f"docs moved: {slug} no longer says the sentence the {k} check enforces: \"{said}\"" for k, slug, said in r["moved"]]
    if r["index"]:
        out.append(f"rule index {RULES}: {' and '.join(r['index'])}, the code-memory limit a session start loads, {sentence('index', docs)}")
    if len(r["lessons"]) > threshold(limits, "lesson_findings", 0):
        at, bad = r["lessons"][0]
        out.append(f"rule shape: {len(r['lessons'])} lesson{'s' * (len(r['lessons']) != 1)} out of the docs' shape, first {at}: "
                   f"{', '.join(bad)}; {sentence(LESSON[bad[0]][0], docs)}")
    return out


def store_report(r: dict[str, Any], found: list[str], at: str) -> str:
    """The store's lesson report, Markdown, docs/shape/lessons.md: the doctor's lines, then every lesson out of the shape by cite."""
    out = [f"# Lesson shape: the rule store, {at}", "",
           "The doctor's daily check of every live rule against the Claude docs' lesson shape (shape.LESSON), each check's "
           "sentence pinned to its fetched page (shape.PINS). `yerk curate --rules` proposes the fixes.", ""]
    out += [f"- {x}" for x in found] or ["- in the shape: every pinned sentence on its page, every lesson in the shape, the index under its limits"]
    if r["lessons"]:
        out += ["", f"## Lessons out of the shape: {len(r['lessons'])}", "", *[f"- {c}: {', '.join(k)}" for c, k in r["lessons"]]]
    return "\n".join(out) + "\n"
