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
