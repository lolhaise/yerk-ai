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
from . import lessons
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
    """One file: the lines read, each count's offending lines as (line number, text), and the topics it says. A converted
    store's index is scored as its lessons, each whole on a line (lessons.Lesson.whole), since a summary's reason is in its body."""
    rx = {k: re.compile(v, re.I) for k, v in (topics or TOPICS).items()}
    out: dict[str, Any] = {"lines": 0, "topics": set(), **{k: [] for k in COUNTS}}
    have = lessons.store(path.parent) if rules and lessons.converted(path.parent) else None
    own = lessons.listed_for(path.parent, "own") if have else {}      # the owner's own sentence kept: no exchange count, named apart
    text = "\n".join(x.whole for x in have) if have is not None else path.read_text(encoding="utf-8", errors="replace")
    for n, raw, ex in lines(text, rules):
        if ex:
            continue
        out["lines"] += 1
        for k in offenses(raw, rules, acronyms):
            if not (k == "untagged" and have and n <= len(have) and have[n - 1].slug in own):
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
    if lessons.converted(m.root):                 # each lesson filed in the last day, by its file
        for x in lessons.store(m.root):
            at = STAMP.search(x.line)
            if at and now - datetime.fromisoformat(at.group(2)).timestamp() <= 86400 and (o := offenses(x.line, True, acronyms)):
                fresh.append((f"{cite(m, m.root / x.cite)}:{x.front().count(chr(10)) + 1}", o))
    elif rules.is_file():
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
# the fetched page (`sentence`). A pin the page no longer holds turns tests/test_lesson_shape.py red and the doctor's daily
# run files a need naming it. Thresholds: budgets.toml [shape], the lesson_* keys.
FABLE, FABLE1, FABLE2, CODE_MEMORY, GUIDE = "prompting-fable-5", "prompting-fable-5-1", "prompting-fable-5-2", "code-memory", "prompting-guide"
# The pages every room's checks read whatever model it runs: the general guide, the Fable family page (the memory recipe
# lives only there; the 5.1 and 5.2 pages do not restate it) and code-memory. A model's own prompting page (page_of) is
# read only for the rooms that run that model, so the density pin, on the 5.1 page that 5.2 dropped, binds a 5.1 room only.
EVERY = (GUIDE, FABLE, CODE_MEMORY)
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
    "index": (CODE_MEMORY, "While auto memory is on, the first 200 lines or 25KB of `MEMORY.md`, whichever comes first, load at "
                           "session start."),                             # its "How it works" section, reworded by 2026-11-21
    # Claude Fable 5.2's page (2026-11-09, on the owner's word: everything in Yerk the way its docs say)
    "caution": (FABLE2, "Cautious instructions can build up from past sessions through memory, notes, and instruction files."),
    "no-approval": (FABLE2, "Name the steps that need no approval."),
    "exception": (FABLE2, "Be explicit about exceptions."),                              # a bold lead, a sentence itself
    "approvals": (FABLE2, "If you also write approval rules into your system prompt, be specific about the actions you want "
                          "double-checked."),
    "style-limit": (FABLE2, "Style rules for writing: Claude Fable 5.2 follows them closely, so a hard limit such as “one "
                            "instruction per sentence, in 20 words or fewer” can make its prose choppy."),   # its bold lead with it
    "built-up": (FABLE2, "Before you migrate, delete cautions whose reason no longer holds, and rewrite absolute rules with "
                         "their scope and reason."),
    "think-less": (FABLE2, "In Anthropic’s testing, an instruction to answer directly without deliberating didn’t stop the model "
                           "from thinking at `medium` and above."),
    "reasoning": (FABLE2, "If your prompts, skills, or tool descriptions ask the model to write out its reasoning in the response, "
                          "remove those instructions."),
    "new-conversations": (FABLE2, "Make these changes in new conversations only."),
    "newest-turn": (FABLE2, "To change instructions partway through a conversation, put them in the newest turn."),
    "append-only": (FABLE2, "Apart from compaction and context editing, keep the history append-only."),
    "clearly": (FABLE2, "Write for an intelligent but low-context reader."),               # the "# Communicating clearly" block
    "plain-words": (FABLE2, "Use plain words, and write in clear, understandable full sentences."),
    "finishing": (FABLE2, "Please avoid stopping while you still owe work the user asked for."),   # the "# Finishing work" block
    "recap": (FABLE2, "Before you start, say in a line what you’re about to do; brief updates while you work help the user "
                      "follow along."),                                                 # the opening line, updates and recap
}
# Each lesson check, the pin it enforces, its threshold key in [shape] and its default (a findings count's is zero).
LESSON = {"enumerated": ("brief", "lesson_cases", 3), "summary": ("lesson", "lesson_summary_chars", 140),
          "no reason": ("reason", "lesson_why", 0), "long": ("brief", "lesson_body_chars", 600),
          "on record": ("on record", "lesson_quote_words", 8), "dense": ("density", "lesson_sentence_words", 40),
          # Claude Fable 5.2's (2026-11-09): read for an install where a room runs claude-fable-5-2, no number but the words
          "absolute": (("built-up", "exception"), "", 0), "approval": ("approvals", "", 0), "think less": ("think-less", "", 0),
          "reasoning": ("reasoning", "", 0), "style limit": ("style-limit", "", 0)}
# Never a refusal at the write gate: the 40-word sentence (Fable 5.2's page says a hard sentence limit makes prose choppy,
# and the 5.1 density note it rests on binds a 5.1 room only, in the store). A style limit is a count in the store, often the
# owner's own preference, and the gate asks of it only its reason.
NOT_AT_GATE, COUNT_ONLY = frozenset({"dense"}), frozenset({"style limit"})
ABSOLUTE = re.compile(r"\b(?:never|always|must not|mustn['’]t|under no circumstances|at all times|at no time|no matter what|"
                      r"without exception|in no case|not ever)\b", re.I)
SCOPE = re.compile(r"\b(?:when|whenever|while|where|wherever|before|after|during|until|once|unless)\b|\b(?:in|on|for|at|inside|"
                   r"outside|within)\s+(?:a|an|the|any|every|each|this|that|these|those|his|her|their|my|your|our|its|one|another|other|all)\b", re.I)
EXCEPTION = re.compile(r"\b(?:unless|except|only (?:if|when|after|with)|until|even (?:if|when|asked)|no exceptions?|go-ahead|"
                       r"approv(?:al|ed|es)|say-so|(?:his|her|their|the owner's) (?:ok|okay|word)|(?:when|if) (?:he|she|they|the owner) "
                       r"(?:asks?|says?|approves?|tells?))\b", re.I)
ASK_FIRST = re.compile(r"\b(?:ask(?:s|ing)? (?:him |her |them |the owner |me )?(?:first|before)|(?:get|getting|needs?|wait for|"
                       r"with(?:out)?) (?:a |an |his |her |their |the owner's |my |your )?(?:go-ahead|ok|okay|approval|sign-off|say-so|"
                       r"confirmation)|confirm (?:first|before)|check with (?:him|her|them|the owner) (?:first|before))\b", re.I)
VAGUE = re.compile(r"\b(?:anything (?:outward|external|outside|public|risky|irreversible|important|big)|(?:sending )?messages? to "
                   r"(?:people|others|anyone|someone)|(?:any|every) (?:outward|external|outside) (?:actions?|steps?|things?)|"
                   r"outward actions?|external actions?|any action|any step|anything|everything)\b", re.I)
NAMED = re.compile(r"\b(?:e-?mail|text|sms|post|tweet|push|merg|delet|pay|buy|order|deploy|publish|sign|book|transfer|invoic|"
                   r"refund|cancel|upload|releas|call|invit|repl|comment|commit|pull request|purchas|spend|charg|forward|install|"
                   r"overwrit|rebas|money)\w*", re.I)
THINK_LESS = re.compile(r"\b(?:without (?:thinking|deliberat\w*|overthinking)|(?:think|deliberate) less|(?:do not|don['’]t|never|no "
                        r"need to) (?:think|deliberate|overthink)\b(?! (?:that|he|she|they|it|so|of|about whether))|(?:skip|minimi[sz]e|"
                        r"limit|less|minimal) (?:the |your )?(?:thinking|deliberation|deliberating)|answer (?:directly|immediately|"
                        r"straight away|right away|at once) without|no extended thinking|stop thinking|keep (?:the |your )?thinking "
                        r"(?:short|brief|minimal))\b", re.I)
REASONING = re.compile(r"\b(?:show|write|explain|include|give|lay out|walk (?:me |him |them )?through|spell out|share|print|post|"
                       r"narrate|put)\s+(?:out\s+)?(?:all\s+)?(?:of\s+)?(?:your|its|the|his|my)?\s*(?:reasoning|thinking|thought "
                       r"process|chain[- ]of[- ]thought)\b|\bthink (?:out loud|aloud)\b|\bstep[- ]by[- ]step reasoning\b|"
                       r"\b(?:reasoning|thinking) (?:in|into) (?:the|your|each) (?:reply|response|answer|message)\b", re.I)
STYLE_LIMIT = re.compile(r"\b(?:\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|"
                         r"forty|fifty)\s+(?:or (?:fewer|less|more)\s+)?(?:short\s+)?(?:lines?|sentences?|words?|paragraphs?|"
                         r"bullets?|characters?|chars)\b", re.I)
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
    """Text as a pin is matched: one space for any run of white space, straight quotes for curly ones, and a Markdown link
    as its words and bold as its words, since a page's sentence reads the same with a link in it (5.2's "put them in the
    newest turn" is one) and a bold lead is quoted with the sentence that leans on it (5.2's "Style rules for writing:")."""
    return " ".join(LINK.sub(r"\1", text).replace("**", "").translate(str.maketrans("‘’“”", "''\"\"")).split())


LINK = re.compile(r"\[([^\]\n]*)\]\([^)\s]*\)")
DATED = re.compile(r"-\d{8}$")


def page_of(model: str) -> str:
    """A model's own prompting page, by its id: claude-fable-5-2 is prompting-fable-5-2, claude-opus-5-5 prompting-opus-5-5
    (a dated id's date dropped), so a model shipped tomorrow names its page with no table to edit."""
    return "prompting-" + DATED.sub("", model.strip().lower()).removeprefix("claude-")


def run_by(rooms: dict[str, dict[str, Any]], state: Path, listed: dict[str, str]) -> dict[str, list[str]]:
    """model id -> the rooms that run it: each room's override over rooms.toml (config.room_settings), an alias read through
    the install's model list; a room on the model's own default names none, so it reads the pages every room reads."""
    from .config import room_settings
    out: dict[str, list[str]] = {}
    for name, conf in rooms.items():
        if model := room_settings(conf, state, name, listed)["model"][0]:
            out.setdefault(listed.get(model.lower(), model), []).append(name)
    return out


def pages(models: Any = ()) -> tuple[str, ...]:
    """The pages the checks read for rooms running `models`: EVERY, then each model's own page; a shared store's, every
    model any room runs."""
    return tuple(dict.fromkeys([*EVERY, *(page_of(m) for m in models)]))


def unpaged(by: dict[str, list[str]], kept: Any) -> list[tuple[str, str, list[str]]]:
    """(model, the page to add, its rooms) for each model a room runs whose prompting page is not among the pages `yerk docs
    refresh` fetches (`kept`, docs.pages), so a model shipped later cannot leave the checks behind in silence."""
    return [(model, page_of(model), names) for model, names in sorted(by.items()) if page_of(model) not in set(kept)]


def bound(check: str, read: Any) -> bool:
    """A LESSON check binds where every page its pins sit on is read."""
    return all(PINS[p][0] in read for p in pins_of(check))


def pins_of(check: str) -> tuple[str, ...]:
    pin = LESSON[check][0]
    return pin if isinstance(pin, tuple) else (pin,)


def said(check: str, docs: Path | None = None) -> str:
    """A check's docs sentences, each with its cite, as its refusal and its finding quote them."""
    return " and ".join(sentence(p, docs) for p in pins_of(check))


def pinned(docs: Path | None, read: Any = None) -> list[tuple[str, str, str]]:
    """(pin, slug, sentence) of each pin its fetched page no longer holds word for word; a page not fetched yet is none, and with
    `read` (pages()) only the pins on a page some room reads, so the 5.1 density note moving is no finding once no room runs 5.1."""
    return [(k, slug, said) for k, (slug, said) in PINS.items()
            if (read is None or slug in read) and (text := page(docs, slug)) and flat(said) not in flat(text)]


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
    v = limits.get(key, default) if key else default
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
           seen: dict[Path, str] | None = None, read: Any = None) -> dict[str, list[str]]:
    """One rule as one lesson, as the Claude docs shape it: each LESSON check it misses and the words that miss it.
    enumerated: lesson_cases or more case heads (when, whenever, if or for opening a clause) or numbered cases (1. (2) (a));
    summary: the first sentence, the summary, over lesson_summary_chars (a body after it is the lesson's detail, when it has any);
    no reason: no because, since, so that, so or why anywhere in it;
    long: the body after the summary over lesson_body_chars, examples' cites and file:line cites not counted;
    on record: a quoted passage or an example of more than lesson_quote_words words that a day log holds (cite that line);
               an example of the cited form (Landed or Bounced, a cite) with a gloss over lesson_gloss_words words;
    dense: a sentence over lesson_sentence_words words (a 5.1 room's: its page's density note);
    absolute: never, always, must not or a like word, with no scope (a when, where or before clause, or in, on, for or at a
    named thing) or no exception written in (unless, except, only if, until, a go-ahead, an approval, or even when asked
    saying that nothing lifts it);
    approval: asking first required (ask first, get the owner's go-ahead, needs approval) of only a vague class (anything outward,
    messages to people, anything) with no named action (an email, a payment, a push, a delete and the like);
    think less: a line telling the model to answer without thinking or to think less;
    reasoning: a line asking for reasoning or thinking written into the reply;
    style limit: a hard number of lines, sentences or words (a count, never a refusal; the gate asks it only its reason).
    Code in backticks is no sentence and no case; quoted text is no instruction. `read`: the pages the checks read (pages());
    a check binds only where its pins' page is read, EVERY when not given."""
    lim, read = limits or {}, EVERY if read is None else read
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
    bare = QUOTED.sub(" ", prose)
    if ab := ABSOLUTE.findall(bare):
        miss = [x for x, rx in (("no scope", SCOPE), ("no exception written in", EXCEPTION)) if not rx.search(bare)]
        out |= {"absolute": [f'"{ab[0].lower()}" with {" and ".join(miss)}']} if miss else {}
    if ASK_FIRST.search(bare) and (vague := VAGUE.search(bare)) and not NAMED.search(bare):
        out["approval"] = [f'asking first of "{vague[0]}" with no action named']
    out |= {k: [f'"{m[0]}"' for m in rx.finditer(bare)][:3] for k, rx in (("think less", THINK_LESS), ("reasoning", REASONING),
                                                                       ("style limit", STYLE_LIMIT)) if rx.search(bare)}
    return {k: v for k, v in out.items() if bound(k, read)}


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


def rows_of(m: Settings) -> list[tuple[str, str, str, dict[str, list[str]]]]:
    """(cite, job, words, the layout's own misses) of each live rule: a converted store's lessons, each cited at its summary, a
    summary line past lessons.SUMMARY_MAX, or a front matter past lessons.FRONT_MAX or with no class first, a "summary" miss
    (the lesson pin's "one-line summary at the top"); else each rule line of RULES and TOPIC_RULES."""
    from .memory import TOPIC_RULES
    if not lessons.converted(m.root):
        return [(f"{cite(m, p)}:{n}", job, text, {}) for p in (m.root / RULES, m.root / TOPIC_RULES) for n, job, text in lessons_of(p)]
    out = []
    for x in lessons.store(m.root):
        head = (m.root / x.cite).read_text(encoding="utf-8", errors="replace")[:lessons.FRONT_MAX]
        layout = {"summary": [f"a summary line of {len(x.summary)} characters"]} if len(x.summary) > lessons.SUMMARY_MAX else {}
        if not (FRONT.match(head) and head.startswith("---\nclass: ")):       # the summary not at the top a room's class read finds
            layout["summary"] = [*layout.get("summary", []), "a front matter past 400 characters or with no class first"]
        out.append((f"{cite(m, m.root / x.cite)}:{x.front().count(chr(10)) + 1}", x.job, f"{x.summary} {x.body}".strip(), layout))
    return out


SIZED = "rule index size:"        # an uncapped install's daily line for its index's size: a measurement, it files no need


def store(m: Settings, limits: dict[str, Any], docs: Path | None = None, read: Any = None) -> dict[str, Any]:
    """The whole rule store against the lesson shape: each live rule of RULES and TOPIC_RULES that misses a LESSON check, by
    cite with its checks, each style limit counted apart (COUNT_ONLY), and RULES, the index a session start loads, against the
    code-memory limits (lines, bytes). `read`: the pages of every model any room runs (pages()), since every room loads the store.
    A lesson lessons.NO_REASON lists has no reason on file, as the owner's side says, or no exception, as its rule gives none:
    that missing reason or exception (its scope written in) is no finding, and it is named apart; so is a long body of one
    listed as his words kept whole, past the length."""
    found, counted, seen = list[tuple[str, list[str]]](), list[str](), dict[Path, str]()   # seen: each day log read once
    listed, named, unlifted, open_ = lessons.no_reason(m.root), list[str](), list[str](), lessons.no_exception(m.root)
    kept = {k: lessons.listed_for(m.root, k) for k in ("whole", "own", "uncited", "long")}    # the owner's words carried as they wrote them
    apart: dict[str, list[str]] = {k: [] for k in kept}
    for at, job, text, layout in rows_of(m):
        missed = lesson(job, text, m.daily, limits, seen, read) | layout
        if (c := lessons.CITE.search(at)) and c[1] in listed:
            missed.pop("no reason", None)
            named.append(at)
        if c and c[1] in open_:
            if all("no scope" not in x for x in missed.get("absolute", [])):
                missed.pop("absolute", None)
            unlifted.append(at)
        if c and (got := [k for k in kept if c[1] in kept[k]]):     # an on-record finding naming the owner's quote or example alone is none
            heads = tuple(h for k, h in (("whole", "a quote the day log holds"), ("uncited", "an example the day log holds"),
                                         ("uncited", "a gloss of")) if k in got)    # the rule's own cite, its gloss whole
            if heads and all(x.startswith(heads) for x in missed.get("on record", [])):
                missed.pop("on record", None)
            if "long" in got:                                       # the owner's words past the length: no finding, named apart
                missed.pop("long", None)
            for k in got:
                apart[k].append(at)
        counted += [at] * any(k in COUNT_ONLY for k in missed)
        if bad := [k for k in missed if k not in COUNT_ONLY]:
            found.append((at, bad))
    p, (most, size) = m.root / RULES, index_limits(docs)
    rows, have = (p.read_text(encoding="utf-8", errors="replace").splitlines(), p.stat().st_size) if p.is_file() else ([], 0)
    over = [f"{len(rows)} lines, past {most}"] * (len(rows) > most) + [f"{have:,} bytes, past {size:,}"] * (have > size)
    return {"lessons": found, "index": over, "size": [len(rows), have, most, size], "moved": pinned(docs, read), "style limits": counted,
            "read": list(read or EVERY), "listed": named, **({"unlifted": unlifted} if unlifted else {}), **{k: v for k, v in apart.items() if v}}


def store_findings(r: dict[str, Any], limits: dict[str, Any], docs: Path | None = None, whole: bool = False) -> list[str]:
    """The doctor's daily lines for the store: a pin its page moved off, the index past code-memory's limits, and the lessons that
    miss the shape past lesson_findings (zero), a line per page whose checks they miss, the first named with its checks and the
    sentence of that page its first check enforces. A style limit is counted in the report, never a line here. `whole`, an
    uncapped install: the index's size is a measurement, never a finding, said last as a line of its own (SIZED) from
    lessons.NEAR of a limit, since nothing of the owner's is trimmed to fit."""
    out = [f"docs moved: {slug} no longer says the sentence the {k} check enforces: \"{said}\"" for k, slug, said in r["moved"]]
    if r["index"] and not whole:
        out.append(f"rule index {RULES}: {' and '.join(r['index'])}, the code-memory limit a session start loads, {sentence('index', docs)}")
    if len(r["lessons"]) > threshold(limits, "lesson_findings", 0):
        on: dict[str, list[tuple[str, list[str]]]] = {}
        for at, bad in r["lessons"]:
            for slug in dict.fromkeys(PINS[pins_of(k)[0]][0] for k in bad):
                on.setdefault(slug, []).append((at, [k for k in bad if PINS[pins_of(k)[0]][0] == slug]))
        for slug, rows in on.items():
            at, bad = rows[0]
            out.append(f"rule shape: {len(rows)} lesson{'s' * (len(rows) != 1)} out of {slug}'s shape, first {at}: "
                       f"{', '.join(bad)}; {said(bad[0], docs)}")
    lines, have, most, size = r.get("size") or [0, 0, 1, 1]
    if whole and (lines >= lessons.NEAR * most or have >= lessons.NEAR * size):
        out.append(f"{SIZED} {RULES} is {lines} lines of {most} and {have:,} bytes of {size:,}, code-memory's limits for a capped "
                   "start; a measurement, since this install is uncapped and nothing is trimmed to fit")
    return out


def unpaged_lines(rows: list[tuple[str, str, list[str]]]) -> list[str]:
    """The doctor's line for each model a room runs with no prompting page fetched, naming the page to add."""
    from .config import _PE
    return [f"docs: no prompting page fetched for {model}, which {len(names)} room{'s' * (len(names) != 1)} run{'s' * (len(names) == 1)}; "
            f"add {slug} ({_PE}{slug.replace('prompting-', 'prompting-claude-', 1)}) to the pages `yerk docs refresh` fetches"
            for model, slug, names in rows]


def store_report(r: dict[str, Any], found: list[str], at: str) -> str:
    """The store's lesson report, Markdown, docs/shape/lessons.md: the doctor's lines, then every lesson out of the shape by cite."""
    out = [f"# Lesson shape: the rule store, {at}", "",
           "The doctor's daily check of every live rule against the Claude docs' lesson shape (shape.LESSON), each check's "
           "sentence pinned to its fetched page (shape.PINS). `yerk curate --rules` proposes the fixes.", ""]
    out += [f"- {x}" for x in found] or ["- in the shape: every pinned sentence on its page, every lesson in the shape, the index under its limits"]
    if n := len(r.get("listed") or ()):
        out += ["", f"Listed as no reason on file, as the owner's side says ({lessons.NO_REASON}), its missing reason no finding: {n} "
                f"lesson{'s' * (n != 1)}, {', '.join(r['listed'])}."]
    if n := len(r.get("unlifted") or ()):
        out += ["", f"Listed as no exception, as its rule gives none ({lessons.NO_REASON}), its missing exception no finding: {n} "
                f"lesson{'s' * (n != 1)}, {', '.join(r['unlifted'])}."]
    for k, what in (("whole", "his quote kept whole, as he wrote it, the cite the gate asks in its place"), ("own", "his own sentence kept, word "
                    "for word, the example tags the gate asks round it"), ("uncited", "an example kept as its words, the cite the gate asks of it"),
                    ("long", "his words kept whole, past the length, the shorter body the gate asks")):
        if n := len(r.get(k) or ()):
            out += ["", f"Listed as {what} no finding ({lessons.NO_REASON}): {n} lesson{'s' * (n != 1)}, {', '.join(r[k])}."]
    out += ["", f"Pages read, those of every model a room runs: {', '.join(r.get('read') or EVERY)}."]
    if r["lessons"]:
        out += ["", f"## Lessons out of the shape: {len(r['lessons'])}", "", *[f"- {c}: {', '.join(k)}" for c, k in r["lessons"]]]
    if FABLE2 in (r.get("read") or ()) and (st := r.get("style limits")):
        out += ["", f"## Style limits, counted, never refused: {len(st)}", "",
                f"A hard number of lines, sentences or words is often the owner's own call; {sentence('style-limit')}", "",
                *[f"- {c}" for c in st]]
    return "\n".join(out) + "\n"
