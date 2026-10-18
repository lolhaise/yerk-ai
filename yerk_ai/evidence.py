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
        kind = ev.get("type")
        if kind not in ("assistant", "user"):
            return
        done = []
        with self.lock:
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if kind == "assistant" and c.get("type") == "tool_use":
                    ti = c.get("input") or {}
                    self.calls[str(c.get("id"))] = {"tool": str(c.get("name")), "input": ti,
                                                    "target": target(str(c.get("name")), ti), "result": ""}
                elif kind == "user" and c.get("type") == "tool_result":
                    call = self.calls.get(str(c.get("tool_use_id")))
                    if call is not None:
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


def check(reply: str, prompts: list[str], ev: Evidence, roots: tuple[Path, ...],
          filed: Callable[[str], str | None], known: Callable[[str], str | None]) -> list[tuple[str, str]]:
    """(rule, detail) for each thing the reply leans on that the turn never read. Logs only."""
    out: list[tuple[str, str]] = []
    seen = ev.read_text()
    for fig in dict.fromkeys(m.group(0).replace("$ ", "$") for m in FIGURE.finditer(reply)):
        bare = fig.lstrip("$")
        if has_figure(seen, fig) or has_figure(seen, bare):
            continue
        where = filed(bare)
        if where:
            out.append(("figure", f"quoted {fig}, on file at {where}; no read this turn carries it "
                                  "(missing read: recall or read_memory of that file)"))
    for name in dict.fromkeys(m.group(1) for m in FILE.finditer(reply)):
        if Path(name).name in seen:
            continue
        where = known(name)
        if where:
            out.append(("file", f"named {name} ({where}); the turn never read it"))
    if any(DECIDE.search(p) for p in prompts) and not ev.memory_read(roots):
        out.append(("decide", "answered what was decided with no memory read this turn"))
    return out
