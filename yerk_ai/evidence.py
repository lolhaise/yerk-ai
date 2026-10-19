# The rule shapes (a figure grounded only by a user line or a tool result of the session, a
# "what did we decide" answer needing a memory read, every tool that can carry evidence counted)
# follow the workspace hooks this harness replaces; the code is new.
"""Records what each turn read (the evidence ledger), and after each reply checks what it claims against what it read (the Stop check).

Every tool call of a turn is recorded from the room's own stream (the tool_use and its
tool_result): what it read (a path, a query, a command) and the text that came back. The turn's
prompts, the context the prompt hook added and the session-start file count as read too. The
store is bounded by characters and drops the OLDEST result text first, never a later hit (DESIGN 5 and 8; L185, L175).

After each reply the Stop check runs three rules and LOGS what it would bounce, never blocks
(shadow mode, until 100 cases show under 1 in 10 false alarms):
- figure: the reply quotes a figure that is on file in the memory tree and no read of the turn
  carries it;
- file: the reply names a file of the memory tree the turn never read;
- decide: the prompt asks what was decided and the turn made no memory read (a recall or
  read_memory call, a read inside a memory root, or recall the prompt hook put in).
A fourth, decision 25 ("the check is the read"), reads the reply sentence by sentence: a claim
about what code does or lacks needs a test or probe run in the turn, a claim about what is running
needs a status read, a claim about what was decided needs a memory read, a claim about a file's
contents needs a read of a file. A claim with none is ledgered with the sentence and the tools the
turn ran. A question, a hedge or a proposal ("would", "could", "might", "I think") is not a claim.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Callable

KEEP_CHARS = 400_000
FIGURE = re.compile(r"(?<![\w.,$])(?:\$\s?\d[\d,]*(?:\.\d+)?[kKmM]?|\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|\d+\.\d+%?|\d+%)"
                    r"(?![\w,]|\.\d)")
FILE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:md|txt|json|toml|csv|pdf))(?::\d+)?\b")
DECIDE = re.compile(r"\bwhat\s+(?:did|have|had)\s+we\s+(?:decide|agree|settle|choose|pick|land|go\s+with)"
                    r"|\bdid\s+we\s+(?:decide|agree|settle)\b|\bwhat\s+was\s+(?:the|our)\s+(?:decision|call)\b", re.I)
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
            self.injected_recall = False
            self.logged: set[tuple[str, str]] = set()
            self.kept = 0

    def give(self, text: str, recall: bool = False) -> None:
        with self.lock:
            if text:
                self.given.append(text)
            self.injected_recall = self.injected_recall or recall

    def observe(self, ev: dict[str, Any]) -> None:
        """The stream's tool_use blocks open a call; its tool_result closes it with the text."""
        if (kind := ev.get("type")) not in ("assistant", "user"):
            return
        done = []
        with self.lock:
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if kind == "assistant" and c.get("type") == "tool_use":
                    ti = c.get("input") or {}
                    self.calls[str(c.get("id"))] = {"tool": str(c.get("name")), "input": ti, "id": str(c.get("id")),
                                                    "target": target(str(c.get("name")), ti), "result": ""}
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
            if self.injected_recall:
                return True
            for c in self.calls.values():
                name = c["tool"].rsplit("__", 1)[-1]
                if name in MEMORY_TOOLS:
                    return True
                blob = json.dumps(c["input"])
                if any(str(r) in blob for r in roots):
                    return True
        return False


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


def check(reply: str, prompts: list[str], ev: Evidence, roots: tuple[Path, ...],
          filed: Callable[[str], str | None], known: Callable[[str], str | None]) -> list[tuple[str, str]]:
    """(rule, detail) for each thing the reply leans on that the turn never read. Logs only."""
    out: list[tuple[str, str]] = []
    seen = ev.read_text()
    figures: dict[str, str] = {}                    # each figure once, with the words just before it
    for m in FIGURE.finditer(reply):
        figures.setdefault(m.group(0).replace("$ ", "$"), reply[max(0, m.start() - 24):m.start()])
    for fig, before in figures.items():
        bare = fig.lstrip("$")
        if has_figure(seen, fig) or has_figure(seen, bare):
            continue
        if VERSION.search(before) or APPROX.search(before) or rounded_near(bare, seen):
            continue
        if where := filed(bare):
            out.append(("figure", f"quoted {fig}, on file at {where}; no read this turn carries it "
                                  "(missing read: recall or read_memory of that file)"))
    for name in dict.fromkeys(m.group(1) for m in FILE.finditer(reply)):
        if Path(name).name in seen:
            continue
        if where := known(name):
            out.append(("file", f"named {name} ({where}); the turn never read it"))
    if any(DECIDE.search(p) for p in prompts) and not ev.memory_read(roots):
        out.append(("decide", "answered what was decided with no memory read this turn"))
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
    if cls == "file":
        return any(n in ("Read", "Grep", "Glob", "read_memory", "NotebookRead") for n in names) or any(
            READS.match(w) for w in words)
    return True


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
    return [("claim", f"{cls}: \"{s}\"; tools this turn: {tools_line(calls)}") for cls, s in claims(reply)
            if not backed(cls, calls, mem)]
