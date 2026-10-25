# The rule shapes (a figure grounded only by a user line or a tool result of the session, every
# tool that can carry evidence counted)
# follow the workspace hooks this harness replaces; the code is new.
"""Records what each turn read (the evidence ledger), and after each reply checks what it claims against what it read (the Stop check).

Every tool call of a turn is recorded from the room's own stream (the tool_use and its
tool_result): what it read (a path, a query, a command) and the text that came back. The turn's
prompts, the context the prompt hook added and the session-start file count as read too. The
store is bounded by characters and drops the OLDEST result text first, never a later hit (DESIGN 5 and 8; L185, L175).

After each reply the Stop check runs one rule and LOGS what it would bounce, never blocks
(shadow mode, until 100 cases show under 1 in 10 false alarms):
- figure: the reply quotes a figure that is on file in the memory tree, that no read of the turn
  carries and that the room did not post in a reply earlier in its session (the check read that reply).
A second, decision 25 ("the check is the read"), reads the reply sentence by sentence: a claim
about what code does or lacks needs a test or probe run in the turn, a claim about what is running
needs a status read, a claim about what was decided needs a memory read, a claim about a file's
contents needs a read of a file. A claim with none is ledgered with the sentence and the tools the
turn ran. A question, a hedge or a proposal ("would", "could", "might", "I think") is not a claim.
A third, the stance rule (claim 3), ledgers each reply to the owner's pushback: held, conceded or folded.
A fourth, the sheet rule, ledgers once a turn that sent a document or pasted a block with no claims
sheet written or named in the turn (check_sheet), in a person's room; an owner's room is refused once instead.
A fifth, the person rule, ledgers a sentence telling what someone said, wants, knows, decided or asked, or where they
were, from a turn with no reading call (check_person); a sixth, the quote rule, a quoted span given as someone's or a
document's words that nothing the turn read, was given or posted earlier carries (check_quote). Neither counts a sentence about
the owner or the room itself, or one written as the room's own note ("from my notes, not their words"); since 2026-10-23 an
owner's room refuses the rest once a result when the turn made no reading call (Room.said_check), a person's room logs.
The taste judge at the end (taste_prompt, taste_runner, taste_judge) is no rule of this check: nothing calls it after a
reply; `yerk eval taste` (taste_eval.py) replays it over the owner's complaints and the replies they accepted.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .words import CALL, no_paths

KEEP_CHARS = 400_000
FIGURE = re.compile(r"(?<![\w.,$])(?:\$\s?\d[\d,]*(?:\.\d+)?[kKmM]?|\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|\d+\.\d+%?|\d+%)" r"(?![\w,]|\.\d)")
MEMORY_TOOLS = ("recall", "read_memory")


def target(tool: str, ti: dict[str, Any]) -> str:
    """What a call read, in one short line."""
    for k in ("file_path", "path", "url", "query", "pattern", "command", "notebook_path"):
        if ti.get(k):
            extra = f" in {ti['path']}" if k == "pattern" and ti.get("path") else ""
            return f"{k}={str(ti[k])[:200]}{extra}"
    return json.dumps(ti)[:200]


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


class Evidence:
    """One room's turn: the calls and what came back, plus what the turn was given."""

    def __init__(self, on_call: Callable[[dict[str, Any]], None] | None = None):
        self.lock = threading.Lock()
        self.on_call = on_call
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.calls: dict[str, dict[str, Any]] = {}
            self.given: list[str] = []          # prompts, the hook's added context, the session-start file
            self.recalled: list[str] = []       # of those, the prompt hook's that carried recall: a call's receipt reads these only
            self.injected_recall = False
            self.logged: set[tuple[str, str]] = set()
            self.kept = 0
            self.started = time.time()          # a reply posted before this came from an earlier turn
            self.trail: list[tuple[str, str]] = []   # this result's ("text", its words) and ("tool", name), in order

    def give(self, text: str, recall: bool = False) -> None:
        with self.lock:
            if text:
                self.given.append(text)
                self.recalled += [text] if recall else []
            self.injected_recall = self.injected_recall or recall

    def observe(self, ev: dict[str, Any]) -> None:
        """The stream's tool_use blocks open a call; its tool_result closes it with the text. The trail keeps the text
        blocks and calls of the result being written, in order (Room.stub_check reads it at Stop, before the result lands)."""
        if (kind := ev.get("type")) not in ("assistant", "user", "result"):
            return
        done = []
        with self.lock:
            if kind == "result":
                self.trail = []
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if kind == "assistant" and c.get("type") == "text":
                    self.trail.append(("text", str(c.get("text") or "")))
                elif kind == "assistant" and c.get("type") == "tool_use":
                    ti = c.get("input") or {}
                    self.calls[str(c.get("id"))] = {"tool": str(c.get("name")), "input": ti, "id": str(c.get("id")),
                                                    "target": target(str(c.get("name")), ti), "result": "", "t": time.time()}
                    self.trail.append(("tool", str(c.get("name"))))
                elif kind == "user" and c.get("type") == "tool_result":
                    if (call := self.calls.get(str(c.get("tool_use_id")))) is not None:
                        call["result"] = result_text(c.get("content"))
                        self.kept += len(call["result"])
                        done.append(dict(call, chars=len(call["result"])))
            for old in self.calls.values():             # bounded: the oldest text goes first
                if self.kept <= KEEP_CHARS:
                    break
                self.kept -= len(old["result"])
                old["result"] = ""
        for call in done:
            if self.on_call:
                self.on_call(call)

    def read_text(self) -> str:
        with self.lock:
            return "\n".join([*self.given, *(f"{c['target']}\n{c['result']}" for c in self.calls.values())])

    def memory_read(self, roots: tuple[Path, ...]) -> bool:
        with self.lock:
            return self.injected_recall or any(c["tool"].rsplit("__", 1)[-1] in MEMORY_TOOLS
                                               or any(str(r) in json.dumps(c["input"]) for r in roots) for c in self.calls.values())


def has_figure(text: str, fig: str) -> bool:
    return re.search(r"(?<![\w$,.\-])" + re.escape(fig) + r"(?![\w,]|\.\d)", text) is not None


# Not quotes of a line, so not looked up on file (matching their digits found unrelated lines: three
# false alarms in one conversation, 2026-10-18): a version after a name ("Opus 5.5", "Python 3.12"),
# a figure the reply marks as approximate ("about 22,000"), and a round figure with a number within
# NEAR of it read this turn ("22,000" from a read 21,822).
VERSION = re.compile(r"\b(?:opus|sonnet|haiku|fable|mythos|claude(?:\s+code)?|gpt|gemini|llama|qwen|python|node|"
                     r"version|release|v)\s*$", re.I)
APPROX = re.compile(r"(?:\b(?:about|around|roughly|nearly|almost|over|under|more\s+than|less\s+than|fewer\s+than|"
                    r"close\s+to|upwards\s+of|approximately)\s+|~\s*)$", re.I)
NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.,])\d+(?:\.\d+)?")
NEAR = 0.1


def rounded_near(fig: str, seen: str) -> bool:
    """A round figure (1,000 or more, ending in 00) with a number within NEAR of it in `seen`."""
    digits = fig.lstrip("$").replace(",", "")
    if not digits.isdigit() or int(digits) < 1000 or not digits.endswith("00"):
        return False
    v = int(digits)
    return any(abs(float(n.replace(",", "")) - v) <= NEAR * v for n in NUMBER.findall(seen))


def forms(fig: str) -> tuple[str, ...]:
    """The figure, without its dollar sign, and the same number with and without thousands separators: a read that
    holds 293477.15 carries a reply's 293,477.15."""
    bare = fig.lstrip("$")
    whole, dot, rest = bare.partition(".")
    plain = whole.replace(",", "")
    if not plain.isdigit() or (len(plain) > 1 and plain.startswith("0")):
        return tuple(dict.fromkeys((fig, bare)))
    return tuple(dict.fromkeys((fig, bare, plain + dot + rest, f"{int(plain):,}" + dot + rest)))


# A figure's source on file is a line about the same thing, not any line that shares its digits (the evidence replay of
# 2026-10-21: 13 of 74 accepted replies flagged, and all 4 wrong-fact hits on an unrelated line, a product's 6,071
# reviews for a shed's 6,071 sf). A line counts when the label words beside the number in the reply (Z8, girth,
# worksheet) are beside it on the line too: two of them, or one that holds a digit (Z8, J6, v2), or the reply's only
# one; when its unit agrees (1.4 ft is not 1.4 s, 0.11 ft is not 0.11%); and when it is not the reply itself (the day
# log records a reply as it is posted, and another lane may quote it: a line holding the reply's own words around the
# figure is a copy, never its source). A figure with no label word beside it, in the reply or on the line, is matched
# by its digits, as before. A dollar figure is looked up with its sign: has_figure never matches digits right after
# a "$", so the bare digits of "$500" could only ever be found where 500 was not a dollar amount; and a read carries
# a figure in either written form, with or without thousands separators (forms, above).
LABEL = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:'[A-Za-z]+)?")
PLAIN = frozenset("""a an the and or but nor of to in on at by for with from into onto as than then that this these those
it its is are was were be been being has have had do does did not no so if vs per via all each every any some one two three
four five six seven eight nine ten about over under above below up down out off more less most least just only also still
very much many few same other came come comes get got gets put puts you he she we they me him her us them my your his
our their who what when where which while there here now new old own like way too can could would should will may might
must""".split())
DAYLOG = re.compile(r"^- \d\d:\d\d:\d\d[+-]\d\d:\d\d \S+ [^\s:]+: |(?: \[[cm]:[^\]]*\])+$")   # memory.format_line's frame
UNIT = re.compile(r" ?(%|ft|feet|sf|sq|inch|inches|s|sec|ms|min|h|hr|hrs|kb|mb|gb|tb|m|km|kg|lb|lbs|mm|cm|x)"
                  r"(?![A-Za-z0-9])", re.I)


def labels(text: str, start: int, end: int) -> frozenset[str]:
    """The label words beside a number at text[start:end]: up to six words before it and four after, on its own line,
    lower case, a plural or possessive s dropped, function words out (Z8 and J6 stay)."""
    lo, hi = text.rfind("\n", 0, start) + 1, text.find("\n", end)
    near = LABEL.findall(text[lo:start])[-6:] + LABEL.findall(text[end:hi if hi >= 0 else len(text)])[:4]
    out = set()
    for w in near:
        w = re.sub(r"'s?$", "", w.lower())
        w = w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
        if w not in PLAIN and (len(w) > 2 or any(c.isdigit() for c in w)):
            out.add(w)
    return frozenset(out)


def unit(text: str, end: int) -> str:
    """The unit right after a number ending at `end` ("%", "ft", "sf", "ms"...), or ""."""
    return m.group(1).lower() if (m := UNIT.match(text, end)) else ""


def squash(text: str) -> str:
    """Text with a day log's " / " line breaks and runs of white space made one space, markdown stars dropped."""
    return re.sub(r"\s+", " ", text.replace(" / ", " ").replace("*", "")).strip()


class Quoted(str):
    """A figure to look up on file, carrying from the reply the label words beside it, its unit and its own words
    around it. memory.on_file asks `about(line)` of each line that holds the figure; a plain str is matched by its
    digits alone."""
    words: frozenset[str]
    unit: str
    own: str

    def __new__(cls, fig: str, words: frozenset[str] = frozenset(), unit: str = "", own: str = "") -> Quoted:
        q = super().__new__(cls, fig)
        q.words, q.unit, q.own = words, unit, own
        return q

    def about(self, line: str) -> bool:
        if self.own and self.own in squash(line):
            return False
        line = DAYLOG.sub("", line)                  # a day-log line's time, room and author are not its words
        for m in re.finditer(r"(?<![\w$,.\-])" + re.escape(str(self)) + r"(?![\w,]|\.\d)", line):
            u = "%" if self.endswith("%") else unit(line, m.end())
            if (u and self.unit and u != self.unit) or (u == "%") != (self.unit == "%"):
                continue
            theirs = labels(line, m.start(), m.end())
            shared = self.words & theirs
            if not theirs or len(shared) >= min(2, len(self.words)) or any(c.isdigit() for w in shared for c in w):
                return True
        return False


def check(reply: str, ev: Evidence, filed: Callable[[str], str | None],
          said: str = "") -> list[tuple[str, str]]:
    """(rule, detail) for each thing the reply leans on that the turn never read. Logs only. A figure in what the room
    posted in its session's earlier replies, `said` (Ledger.said), counts as read: the check ran on those replies."""
    out: list[tuple[str, str]] = []
    seen = ev.read_text()
    figures: dict[str, tuple[str, Quoted]] = {}     # each figure once, with the words just before it and as looked up
    for m in FIGURE.finditer(reply):
        fig = m.group(0).replace("$ ", "$")
        own = squash(reply[max(0, m.start() - 30):m.end() + 30])          # too short to tell a copy: no copy test
        figures.setdefault(fig, (reply[max(0, m.start() - 24):m.start()], Quoted(
            fig, labels(reply, m.start(), m.end()), "%" if fig.endswith("%") else unit(reply, m.end()),
            own if len(own) >= len(fig) + 40 else "")))
    for fig, (before, quoted) in figures.items():
        bare = fig.lstrip("$")
        if any(has_figure(text, f) for text in (seen, said) for f in forms(fig)):
            continue
        if VERSION.search(before) or APPROX.search(before) or rounded_near(bare, seen):
            continue
        if where := filed(quoted):
            out.append(("figure", f"quoted {fig}, on file at {where}; no read this turn carries it "
                                  "(missing read: recall or read_memory of that file)"))
    return out


# decision 25: the claim classes and what backs each (a run, a status read, a memory read, a file read)
CLAIMS = {
    "behaviour": re.compile(
        r"\b(?:nothing|no\s+code|no\s+(?:function|module|test|hook|gate|rule)|none\s+of\s+(?:it|them|the\s+code))\s+"
        r"(?:in\s+\S+\s+)?(?:implements?|handles?|does|calls?|checks?|wires?|covers?|catch(?:es)?|blocks?|tests?)\b"
        r"|\b(?:is|are|isn't|aren't|was|wasn't|is\s+not|are\s+not)\s+(?:yet\s+|already\s+|fully\s+)?"
        r"(?:implemented|wired(?:\s+up)?|hooked\s+up|unimplemented|broken|covered\s+by\s+(?:a\s+)?tests?)\b"
        r"|\b(?:the\s+)?(?:code|function|module|hook|gate|daemon|harness|test|parser|script)\s+"
        r"(?:already\s+|still\s+|never\s+)?(?:does(?:n't|\s+not)?|handles|implements|checks|calls|works|"
        r"fails|passes|breaks|catches|blocks|lacks)\b", re.I),        # third person: "do code work" is no claim
    "state": re.compile(
        r"\b(?:is|are|isn't|aren't|is\s+not|are\s+not)\s+(?:now\s+|still\s+|currently\s+)?"
        r"(?:running|up|down|live|stopped|active|inactive|dead|connected|online|offline|crashed)\b", re.I),
    "decision": re.compile(
        r"\bwe\s+(?:decided|agreed|settled|chose|picked|went\s+with)\b|\bthe\s+(?:decision|call)\s+(?:was|is)\b"
        r"|\b(?:it\s+)?was\s+decided\b", re.I),
    "file": re.compile(
        r"\b(?:the\s+file|[\w./-]+\.(?:py|md|toml|json|txt|ya?ml|sh|ts|js|cfg|ini))\s+"
        r"(?:says|contains|has|holds|lists|reads|defines|sets|shows)\b", re.I),
}
HEDGE = re.compile(r"\b(?:would|could|might|may|maybe|perhaps|probably|likely|i\s+think|i\s+guess|i\s+believe|"
                   r"if|unless|not\s+checked|unverified|from\s+memory|guess)\b", re.I)
RUNS = re.compile(r"^(?:pytest|py\.test|mypy|ruff|tox|nox|unittest|uv|uvx|python[\d.]*|node|npm|pnpm|yarn|npx|cargo|"
                  r"go|make|bash|sh|zsh|yerk|yerkd|curl|wget|\./\S+|\S+\.sh|\S+\.py)$")
READS = re.compile(r"^(?:grep|rg|ag|cat|sed|awk|head|tail|less|more|ls|find|wc|git|diff|file|stat|jq|tree)$")
STATUS = re.compile(r"^(?:yerk|systemctl|journalctl|ps|pgrep|pidof|ss|netstat|lsof|curl|sqlite3|docker|kubectl|"
                    r"uptime|free|df|top|htop)$")
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(reply: str) -> list[str]:
    text = re.sub(r"```.*?```", " ", reply, flags=re.S)
    return [s.strip(" -*>`") for s in SENTENCE.split(text) if s.strip(" -*>`")]


def claims(reply: str) -> list[tuple[str, str]]:
    """(class, sentence) for each sentence that claims something code must back."""
    out = []
    for s in sentences(reply):
        if s.endswith("?") or HEDGE.search(s):
            continue
        for cls, rx in CLAIMS.items():
            if rx.search(s):
                out.append((cls, s[:300]))
                break
    return out


def words_run(calls: list[dict[str, Any]]) -> list[str]:
    """The command word of every shell command the turn ran, in order."""
    from .lifted.shellguard import command_words
    out: list[str] = []
    for c in calls:
        if c["tool"] == "Bash":
            out += command_words(str(c["input"].get("command") or ""))
    return out


def backed(cls: str, calls: list[dict[str, Any]], memory_read: bool) -> bool:
    words = words_run(calls)
    names = [c["tool"].rsplit("__", 1)[-1] for c in calls]
    if cls == "behaviour":
        return any(RUNS.match(w) for w in words)
    if cls == "state":
        return any(STATUS.match(w) or RUNS.match(w) for w in words)
    if cls == "decision":
        return memory_read
    return any(n in ("Read", "Grep", "Glob", "read_memory", "NotebookRead") for n in names) or any(READS.match(w) for w in words)  # file


def tools_line(calls: list[dict[str, Any]]) -> str:
    """The tools the turn ran, shell commands by their command word: "Grep x2, Bash(grep) x1"."""
    seen: dict[str, int] = {}
    for c in calls:
        if c["tool"] == "Bash":
            from .lifted.shellguard import command_words
            key = f"Bash({','.join(command_words(str(c['input'].get('command') or ''))) or '?'})"
        else:
            key = c["tool"]
        seen[key] = seen.get(key, 0) + 1
    return ", ".join(f"{k} x{n}" for k, n in seen.items()) or "none"


def check_claims(reply: str, ev: Evidence, roots: tuple[Path, ...]) -> list[tuple[str, str]]:
    """Decision 25 in shadow: ("claim", detail) for each claim no run of the turn backs. Logs only."""
    with ev.lock:
        calls = list(ev.calls.values())
    mem = ev.memory_read(roots)
    return [("claim", f"{cls}: \"{s}\"; tools this turn: {tools_line(calls)}") for cls, s in claims(reply) if not backed(cls, calls, mem)]


# the stance rule (claim 3): how a reply to the owner's pushback answers it (the pushback: words.PUSHBACK)
AGREE = re.compile(r"^\W*(?:(?:yeah|yep|yes|ok(?:ay)?|oh|honestly|lol|haha)\b\W*)?(?:fair|true|agreed?|good\s+(?:point|call|catch)"
                   r"|makes\s+sense|you['’]?re\s+(?:right|correct)|you\s+are\s+right)\b(?![^.!?\n]*\b(?:but|though|however|except)\b)", re.I)
STANDS = re.compile(r"\b(?:still\s+(?:stands?|holds?|true|right)|i\s+(?:still\s+)?stand\s+by|both\s+are\s+true|nothing\s+(?:i\s+said\s+)?"
                    r"was\s+wrong|what\s+(?:still\s+)?stands)\b|:[*_\s]*(?:holds?|stands?)\b|\b(?:holds?|stands?)\s*[:(]", re.I)
OWN_WRONG = re.compile(r"\bi\s+(?:was\s+wrong|got\s+(?:\w+\s+){0,3}?wrong|overstated|misread|miscounted|misspoke)\b"
                       r"|\b(?:my\s+(?:mistake|bad)|i\s+stand\s+corrected)\b", re.I)
NO_READ = ("Write", "Edit", "MultiEdit", "NotebookEdit", "TodoWrite", "ToolSearch", "send", "hold", "remember", "rule", "worker", "schedule", "settings", "voice_note")


def stance(reply: str, pushed: list[float], ev: Evidence) -> list[tuple[str, str]]:
    """Held: it says what still stands with a source read after the pushback (`pushed`: when each came); conceded: it
    says what it got wrong, with that read; folded: it agrees or concedes without either. Saying neither holds ground."""
    with ev.lock:
        read = any(c["t"] >= min(pushed) and c["tool"].rsplit("__", 1)[-1] not in NO_READ for c in ev.calls.values())
    wrong = OWN_WRONG.search(reply)
    return [("stance", "held" if STANDS.search(reply) and read else "conceded" if wrong and read else
             "folded" if wrong or AGREE.search(reply) else "held")]


# the sheet rule: a turn that ships something in the owner's name, a document through the send tool or a pasted block
# in the reply or in a send's text, with no claims sheet (claim, source line, quote) written or named in the turn.
# The morning diff counts how often a room sends without checking its claims against source first. Since 2026-10-22 an
# owner's room is refused once a turn instead (Room.sheet_check, a shadow row per refusal); a person's room still logs.
DOCUMENT = (".docx", ".pdf", ".xlsx", ".pptx", ".txt", ".md", ".csv")
PASTE_TAGS = ("", "text", "plain", "none")              # a fence with one of these opens a paste; a language tag opens code
SHEET = re.compile(r"claims[\w.-]*\.(?:md|txt|csv)\b", re.I)    # CLAIMS.md, example-report/claims.md, ex905-claims.md
WRITES = ("Write", "Edit", "NotebookEdit")


def pasted(text: str) -> bool:
    """A fenced block (three backticks) that is a paste, not code: no tag after the fence, or text, plain or none."""
    inside = False
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("```"):
            continue
        if inside:
            inside = False
        elif s[3:].strip().split(" ")[0].lower() in PASTE_TAGS:
            return True
        else:
            inside = True
    return False


def sheet_written(ev: Evidence, beside: Path | None = None) -> bool:
    """A claims sheet written or named this turn, the shadow rule's test and the refusal's alike: a call of the turn that
    writes a file whose name holds claims (SHEET), or a shell command naming one; or, given `beside`, such a file in that
    folder changed since the turn began (a sheet a call wrote counts before its stream line is read)."""
    with ev.lock:
        calls, began = [(c["tool"].rsplit("__", 1)[-1], c["input"]) for c in ev.calls.values()], ev.started
    if any(SHEET.search(str(ti.get("file_path") or "")) if name in WRITES else name == "Bash" and SHEET.search(str(ti.get("command") or ""))
           for name, ti in calls):
        return True
    try:
        return beside is not None and any(SHEET.search(p.name) and p.stat().st_mtime >= began for p in beside.iterdir() if p.is_file())
    except OSError:
        return False


def shipped(sent: list[str], paste: bool) -> str:
    """What a turn ships in the owner's name, as the sheet rows say it: "sent a.pdf", "pasted a block" or both."""
    return " and ".join(x for x in (f"sent {', '.join(sent)}" if sent else "", "pasted a block" if paste else "") if x)


def check_sheet(reply: str, ev: Evidence) -> list[tuple[str, str]]:
    """The sheet rule in shadow, a person's room's (an owner's room refuses instead, Room.sheet_check): ("sheet", detail)
    once when the turn sent a document or pasted a block and no call of the turn wrote or named a claims sheet. Logs only."""
    with ev.lock:
        sends = [c["input"] for c in ev.calls.values() if c["tool"].rsplit("__", 1)[-1] == "send"]
    sent = [Path(f).name for ti in sends if (f := str(ti.get("file") or "")).lower().endswith(DOCUMENT)]
    paste = any(pasted(t) for t in [reply, *(str(ti.get("text") or "") for ti in sends)])
    if not sent and not paste or sheet_written(ev):
        return []
    return [("sheet", f"{shipped(sent, paste)} with no claims sheet written this turn")]


# the person rule: what someone said, wants, knows, decided or asked, or where they were, told in a turn that made no
# call that reads (any call outside NO_READ, the stance rule's test: a recall, read_memory, Read, Grep, web or shell
# read). The prompt hook's recall block is not a call: the replay's misses of 2026-10-21 (who said to go ahead, where
# the owner was) all had one. A question, a hedge or a proposal is no claim, and since 2026-10-23 (said-or-noted) neither is a
# sentence about the owner ("you", the name the install gives the owner's words) or the room, or one the room marks as its own note:
# eight of the first day's twelve rows told the owner what they had said or wanted, their words being the prompt. Nor is a
# bare "told", only , . ; ) or the end after it ("286 posted, Sam told, 288 named"): the owner's own act in the passive, no one's words.
PERSON = re.compile(r"\b([Yy]ou|[Hh]e|[Ss]he|[A-Z][a-z]+)\s+(?:(?:never|already|actually|just|also|still|even)\s+)?"
                    r"(?:said|says|told(?!\s*(?:[,.;)]|$))|tells|asked|asks|decided|wants?|wanted|knows?|knew|went\s+to|(?:was|were)\s+(?:on|in|at)"
                    r"|(?:does|did|do)(?:n['’]?t|\s+not)\s+(?:want|know|say|ask|decide))\b")
NOT_PEOPLE = frozenset("the this that these those it its there here what which who whoever nobody everyone everybody someone "
                       "somebody anyone anybody one each both all most some none nothing something everything anything we they "
                       "i then so and but or if when once since as yes yeah fair right good ok okay also still just even now".split())
OWNER = frozenset("you your yours i me my mine we our".split())      # the owner, or the room speaking of itself
NOTED = re.compile(r"\bfrom\s+my\s+notes\b|\bnot\s+their\s+words\b|\bproposed,?\s+not\s+confirmed\b", re.I)


def read_any(ev: Evidence) -> bool:
    """A call of the turn that reads: any outside NO_READ, whatever its target (the person rule's test)."""
    with ev.lock:
        return any(c["tool"].rsplit("__", 1)[-1] not in NO_READ for c in ev.calls.values())


def people(reply: str, ev: Evidence, owner: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """(name, detail) for each sentence telling what a third person said, wants, knows, decided or asked, from a turn with no
    reading call: never one whose subject (the first such name) is the owner (OWNER, their `owner` names) or the room, nor one
    marked as the room's note (NOTED)."""
    with ev.lock:
        calls = list(ev.calls.values())
    if any(c["tool"].rsplit("__", 1)[-1] not in NO_READ for c in calls):
        return []
    out, mine = [], OWNER | {n.lower() for n in owner}
    for s in sentences(reply):
        names = [m.group(1) for m in PERSON.finditer(s) if m.group(1).lower() not in NOT_PEOPLE and not VERSION.search(m.group(1))]
        if names and names[0].lower() not in mine and not s.endswith("?") and not HEDGE.search(s) and not NOTED.search(s):
            out.append((names[0], f"{names[0]}: \"{s[:300]}\"; tools this turn: {tools_line(calls)}"))
    return out


def check_person(reply: str, ev: Evidence, owner: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """The person rule in shadow: ("person", detail) for each such sentence, with the name and the tools line."""
    return [("person", detail) for _, detail in people(reply, ev, owner)]


# the quote rule: a quoted span of three or more words that its lead-in gives as someone's or a document's words (said,
# wrote, the note says, her message, the email) and that no read, prompt or earlier reply of the session carries, looked
# up in the evidence the way `figure` is. The lead-in is the clause before the quote, cut at an earlier quote; its cue
# ends within three words of the quote, or the clause ends in a colon ("wrote a note right on that detail:"). Logs only.
QUOTED = re.compile(r"[\"“]([^\"“”\n]{3,400})[\"”]")
NOUN = (r"\b(?:[Hh]er|[Hh]is|[Tt]heir|[Yy]our|[Mm]y|[Oo]ur|[Tt]he|[Tt]hat|[Tt]his|[A-Z][a-z]+['’]s)\s+(?:own\s+)?"
        r"(?:note|notes|message|email|e-mail|comment|text|ticket|page|doc|document|post|reply|memo|letter|words)")
LEAD = re.compile(r"(?:\b(?:[Ss]aid|says|saying|wrote|writes|told|tells|asked|replied|answered|put\s+it|quoted?|reads)|" + NOUN + r")\b")
INTRO = re.compile(LEAD.pattern + r"\W*(?:\w+\W+){0,3}$")
SAID_AFTER = re.compile(r"^\W{0,3}(you|he|she|they|[A-Z][a-z]+)\s+(?:said|says|wrote|writes|asked|told)\b")
DETERMINER = frozenset("the this that a an her his their its".split())


def speaker(lead: str, after: re.Match[str] | None) -> str:
    """Whose words a quote is given as: the name after it ("..., she said"), its lead-in's owned noun ("Her reply", "Sam's
    note") or the subject of its verb ("She says", "the recurrence says"), its determiner kept."""
    cue = [*LEAD.finditer(lead)][-1:]
    if after or not cue:
        return after.group(1) if after else ""
    if re.match(NOUN, cue[0].group(0)):
        return cue[0].group(0)
    before = re.findall(r"[\w'’]+", lead[:cue[0].start()])[-2:]
    return " ".join(before if len(before) == 2 and before[0].lower() in DETERMINER else before[-1:])


def flat(text: str) -> str:
    """Words only, lower case, apostrophes dropped: the form a quote and the evidence are compared in."""
    return " " + " ".join(re.findall(r"[\w%$]+", text.lower().replace("'", "").replace("’", ""))) + " "


def quotes(reply: str, ev: Evidence, said: str = "", owner: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """(whose, detail) for each quoted span given as someone's or a document's words with a part (split at an ellipsis) that
    the turn's reads and prompts and the session's earlier replies, `said`, do not hold: never the owner's or the room's own
    words (OWNER, their `owner` names), nor a span in a sentence marked as the room's note (NOTED); detail: the span, its lead-in."""
    out, pool, mine = [], "", OWNER | {n.lower() for n in owner}
    for m in QUOTED.finditer(reply):
        span, lead = m.group(1).strip(), re.split(r"[.!?]\s|\n|[\"“”]", reply[max(0, m.start() - 80):m.start()])[-1]
        after, lead = SAID_AFTER.match(reply[m.end():m.end() + 60]), lead.strip()
        introduced = INTRO.search(lead) or (LEAD.search(lead) and lead.rstrip(" *_").endswith(":"))
        if len(re.findall(r"\w+", span)) < 3 or not (introduced or after):
            continue
        who, rest = speaker(lead, after), re.split(r"(?<=[.!?])\s|\n", reply[m.end():], maxsplit=1)[0]
        if re.sub(r"['’]s$", "", who.split(" ")[0].lower()) in mine or NOTED.search(f"{lead} {span} {rest}"):
            continue
        pool = pool or flat(ev.read_text() + "\n" + said)
        if any(flat(part) not in pool for part in re.split(r"\.\.\.|…", span) if flat(part).strip()):
            lead = lead if LEAD.search(lead) else str(after.group(0) if after else "").strip(" ,.")
            out.append((who, f"\"{span[:200]}\" after \"{lead[-80:]}\"; no read, prompt or earlier reply of the session carries it"))
    return out


def check_quote(reply: str, ev: Evidence, said: str = "", owner: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """The quote rule in shadow: ("quote", detail) for each such span (quotes)."""
    return [("quote", detail) for _, detail in quotes(reply, ev, said, owner)]


# decision receipts (claim 19): each call a reply makes (words.CALL, code's word, no model) with what its turn stood on, the
# recall hits the prompt hook handed it and the cites of every call that reads (outside NO_READ), one `calls` row a call
# (Ledger.call) for the next morning diff; a call with no reads behind it is flagged
LIST = re.compile(r"^(?:step\s+)?\d{1,2}[.):]\s+", re.I)       # a numbered line's own number, where no sentence ends
END = re.compile(r"[.!?](?=\s|$)")
CITED = re.compile(r"\(([^()\s]+:\d{1,7})\)$", re.M)           # a recall line's cite, at its end (hooks.render_recall)
AT = re.compile(r"(?<![\w/.~-])((?:~|/)?(?:[\w.@+-]+/)*[\w.@+-]+\.[A-Za-z]\w*):(\d{1,7})(?!\d)")    # file:line in a result
OPENS = ("Read", "NotebookRead", "read_memory")                # one file from a line: cited by its input


def calls(reply: str) -> list[str]:
    """Each sentence of a reply that makes a call, once, in order, its stars out and cut to 300 characters: never a question,
    a shape inside quotes (someone else's words) or one in a code block."""
    out: list[str] = []
    for line in re.sub(r"```.*?```", " ", reply, flags=re.S).splitlines():
        line = line.strip(" -*>`#")
        bare = QUOTED.sub(lambda m: " " * len(m.group(0)), line)
        skip = n.end() if (n := LIST.match(bare)) else 0
        for m in CALL.finditer(bare):
            lo = 0 if m.start() < skip else max([skip, *(e.end() for e in END.finditer(bare, skip, m.start()))])
            hi = next((e.end() for e in END.finditer(bare, m.end())), len(bare))
            if (s := re.sub(r"\*+", "", line[lo:hi]).strip()[:300]) and not s.endswith("?") and s not in out:
                out.append(s)
    return out


def receipts(ev: Evidence, short: Callable[[str], str] = str, most: int = 20) -> list[str]:
    """The cites a turn's calls stand on, each once, in order, `most` at most: the recall hits the prompt hook handed it (never
    the session-start file's, which every turn of a session carries), then each call that reads: a Read or read_memory as its
    file and first line, any other by the file:line cites in what it returned (recall's hits, grep's lines), five a call, else
    by what it read (a query, a url, a command). `short` names a path as a room cites it (memory.cite)."""
    with ev.lock:
        given, made = list(ev.recalled), list(ev.calls.values())
    out = [m.group(1) for g in given for m in CITED.finditer(g)]
    for c in made:
        name, ti = c["tool"].rsplit("__", 1)[-1], c["input"]
        if name in NO_READ:
            continue
        if name in OPENS and (p := str(ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or "")):
            f, _, n = p.rpartition(":") if re.search(r":\d+$", p) else (p, "", str(ti.get("offset") or ti.get("start") or 1))
            out.append(f"{short(f)}:{n if n.isdigit() else 1}")
        else:
            out += [f"{short(m.group(1))}:{m.group(2)}" for m in AT.finditer(c["result"])][:5] or [no_paths(c["target"].partition("=")[2])[:80]]
    return list(dict.fromkeys(x for x in out if x))[:most]


def receipt_rows(reply: str, ev: Evidence, short: Callable[[str], str] = str) -> list[dict[str, Any]]:
    """A `calls` row's own part for each call the reply makes: its sentence, the turn's cites (JSON) and the flag when none."""
    reads = receipts(ev, short) if (said := calls(reply)) else []
    return [{"call": s, "reads": json.dumps(reads), "no_reads": int(not reads)} for s in said]


# the taste judge (the taste eval's phase 2, from PR 93 without its shadow thread): a judge model reads a reply to the
# owner the way phase 1's did (the messages before it, the reply marked, never what they wrote after), plus what the turn
# READ: each call's tool, target and characters back, a read's first words when the row carries them, and the recall the
# prompt hook gave with the message. It answers phase 1's ten kinds; the answer says the verdict they fold into, the why
# and the read or message it leaned on. One `claude -p` call a row, hooks, settings and tools off, the rubric as its whole
# system prompt, `timeout` alone at the cap. Only `yerk eval taste` (taste_eval.py) calls it: no reply is judged as it is
# posted, and nothing here is a rule of the Stop check. The bar for a gate, printed by `yerk eval taste` and enforced
# nowhere: recall at least 5 of 7 on the owner's complaints, false alarms under 1 in 10 on the replies they accepted.
TASTE_KINDS = ("wrong fact", "wrong register", "missed the ask", "too long", "no check before claiming", "filed instead of answering",
               "slow or no answer", "repeated itself", "overstepped", "other")
TASTE_VERDICTS = {"wrong fact": "unchecked-claim", "no check before claiming": "unchecked-claim", "missed the ask": "missed-the-ask",
                  "filed instead of answering": "missed-the-ask", "overstepped": "missed-the-ask", "other": "missed-the-ask",
                  "wrong register": "not-his-voice", "too long": "not-his-voice", "repeated itself": "not-his-voice",
                  "slow or no answer": "slow-or-no-answer"}
TASTE_SCHEMA = {"type": "object", "properties": {"would_complain": {"type": "boolean"}, "kind": {"type": "string", "enum": [*TASTE_KINDS, "none"]},
                                                 "why": {"type": "string"}, "cite": {"type": "string"}},
                "required": ["would_complain", "kind", "why", "cite"]}
TASTE_CAP, TASTE_RECALL = 20.0, 4000        # seconds a judge call may take; characters of recall
TASTE_RUBRIC = Path(__file__).with_name("taste-rubric.md")
MARK = "  <-- THE REPLY UNDER JUDGMENT"


def taste_prompt(row: dict[str, Any]) -> str:
    """The judge's input from a row (room, before, reply_id, reply, reads, recall): the messages before the owner's next one,
    oldest first, the reply marked, then what the turn read and the recall it was given. A row with no `reads` (no ledger
    to join it to) is judged on the chat alone, as phase 1 was."""
    out, rid, marked = [f"Room: {row.get('room', '')}", "", "Messages before the owner's next message, oldest first:"], row.get("reply_id"), False
    for b in row.get("before") or []:
        mark = MARK if rid and b.get("message_id") == rid else ""
        marked, atts = marked or bool(mark), f" [attachments: {', '.join(b['attachments'])}]" if b.get("attachments") else ""
        out.append(f"[{'owner' if b.get('author') == 'owner' else 'room'} {b.get('at', '')}]{mark} {b.get('text', '')}{atts}")
    if not marked:                                  # a reply outside `before` keeps its own files, as phase 1 shows them
        atts = f" [attachments: {', '.join(row['reply_attachments'])}]" if row.get("reply_attachments") else ""
        out.append(f"[room {row.get('reply_at', '')}]{MARK} {row.get('reply', '')}{atts}")
    if (reads := row.get("reads")) is not None:
        out += ["", "What the turn read before it replied, in order (tool, what it read, characters back, the first words of a read):"]
        out += [f"- {r['tool']} {r['target']} ({r['chars']} chars)" + (f": {r['head']}" if r.get("head") else "") for r in reads]
        out += [] if reads else ["- nothing: the turn made no call"]
    if row.get("recall"):
        out += ["", "Recall the room was given with the message:", str(row["recall"])[:TASTE_RECALL]]
    out += ["", ("A claim of the reply that no read above and no message carries was not checked. " if reads is not None else "")
            + "Would the owner complain about, correct or push back on the reply marked above? Answer with the JSON; cite is the read or "
            "message your answer leans on, or none."]
    return "\n".join(out)


def taste_runner(rubric: Path, model: str = "", effort: str = "medium", cwd: Path | None = None,
                 claude: str = "") -> Callable[[str, float], str]:
    """run(prompt, cap) -> the judge's stdout: one `claude -p` call (json out, the schema forced, the rubric as the system
    prompt, no tools, hooks, settings, MCP or session kept) on whatever account the environment gives it; TimeoutExpired
    past `cap`."""
    from .engine import claude_bin
    argv = [claude or claude_bin(), "-p", "--output-format", "json", "--json-schema", json.dumps(TASTE_SCHEMA), "--system-prompt-file",
            str(rubric), "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
            *(["--model", model] if model else []), *(["--effort", effort] if effort else [])]

    def run(prompt: str, cap: float) -> str:
        return subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=cap, cwd=cwd).stdout
    return run


def taste_judge(prompt: str, run: Callable[[str, float], str], cap: float = TASTE_CAP) -> dict[str, Any]:
    """One judge call: {verdict, kind, why, cite, seconds, tokens}, {error, seconds} or {timeout, seconds}."""
    t0 = time.time()
    try:
        out = run(prompt, cap)
    except subprocess.TimeoutExpired:
        return {"timeout": True, "seconds": round(time.time() - t0, 2)}
    rec: dict[str, Any] = {"seconds": round(time.time() - t0, 2)}
    try:
        d = json.loads(out)
        assert isinstance(d, dict)
    except (ValueError, AssertionError):
        return {**rec, "error": f"no JSON: {str(out)[-120:]}"}
    ans = d.get("structured_output")
    if not isinstance(ans, dict) and not d.get("is_error"):       # an error's result is its reason (a usage limit), never an answer
        try:
            ans = json.loads(d.get("result") or "")
        except ValueError:
            ans = None
    rec.update(seconds=round(float(d.get("duration_ms") or rec["seconds"] * 1000) / 1000, 2), tokens=sum(
        int((d.get("usage") or {}).get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")))
    if d.get("is_error") or not isinstance(ans, dict) or "would_complain" not in ans:
        return {**rec, "error": str(d.get("result") or "no answer")[:120]}
    kind = str(ans.get("kind") or "none")
    verdict = TASTE_VERDICTS.get(kind, "missed-the-ask") if ans["would_complain"] else "ok"
    return {**rec, "verdict": verdict, "kind": kind, "why": " ".join(str(ans.get("why") or "").split())[:300],
            "cite": " ".join(str(ans.get("cite") or "none").split())[:160]}
