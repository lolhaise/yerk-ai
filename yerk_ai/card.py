"""Builds the live progress card a room shows while it works, from Claude Code's stream events. One card per turn of every kind: the state and the
waits in (the queue for its), turn and the last (part-closes the case before the next; run
start of the summary, then "..."), then one row per tool call saying what the step is FOR in plain words
(words.STEP), marked done or not done by its result, two or more failures in a row folded into one. Every
line holds to phone width (words.PHONE); the newest rows win. The room swaps the card for the reply at the
end, or edits it to say what was owed when a turn ends with nothing to show. (Ideas from OpenClaw's cards.)
Waits in the queue for its turn and: the last part closes the (case before the next run starts) the example note
count, never a step'the example, note says-the sample item moved to's narration (2026-10-21: a person's card listed
the steps of a file build; the owner: "The test reads it back as").
"""
from __future__ import annotations

import calendar
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import words
from .words import DONE as OK, NOT_DONE as FAILED, START, WORKING as RUNNING

ROW_MAX, THINKING_MAX, ROWS_SHOWN = words.PHONE, 2 * words.PHONE, 12
DAY = re.compile(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\.md")           # a day log is named by its day
FILE = re.compile(r"(?<![\w:/.])~?/[\w.@+-]+(?:/[\w.@+-]+)*|(?<![\w/.-])[\w@+/-]+\.(?:md|json|py|sh|txt|toml|log|pdf)\b")
# a search pattern's syntax, read out by terms(): group and flag openers, escapes (an escaped character stays),
# classes, repeats, wildcard runs, alternations, stray marks and quotes
SYNTAX = re.compile(r"\(\?[a-zA-Z]*[:)]|\\[bBAZdDwWsS]|\\(.)|\[\^?[^\]]*\]|\{\d*,?\d*\}|\.[*+?]|\s*\|\s*|[()^$*+?\"']")


def short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def tool_name(name: str) -> str:
    """mcp__yerk__send -> send; Read -> read."""
    return (name.split("__")[-1] if name.startswith("mcp__") else name).lower()


def refused(content: Any) -> bool:
    """A Yerk tool's result that says it did nothing (`"ok": false`: a send the paste rule turned back, a file from outside
    the room's folders). Claude Code hands an MCP tool's answer over as `{"result": "<its JSON>"}`, never as an error."""
    text = content if isinstance(content, str) else "".join(
        str(b.get("text") or "") for b in content if isinstance(b, dict)) if isinstance(content, list) else ""
    for _ in range(2):
        try:
            got = json.loads(text)
        except (TypeError, ValueError):
            return False
        if not isinstance(got, dict):
            return False
        if not isinstance(got.get("result"), str):
            return got.get("ok") is False
        text = got["result"]
    return False


def plain(path: str) -> str:
    """A file as a person names it (STYLE.md rule 4): a day log "the Oct 6 notes", open-loops.md "your open items",
    MEMORY.md "the pinned notes", rules.md "the rules", facts/<name>.md "<name>'s file", else its bare name."""
    parts = re.sub(r":\d+$", "", path).rstrip("/").split("/")
    name = parts[-1]
    if m := DAY.fullmatch(name):
        return words.DAY_NOTES.format(day=f"{calendar.month_abbr[int(m[1])]} {int(m[2])}")
    if name in words.FILES:
        return words.FILES[name]
    stem = os.path.splitext(name)[0]
    return words.FACT_FILE.format(name=stem) if parts[-2:-1] == ["facts"] else stem


def terms(pattern: str) -> str:
    """A search pattern in words: `a|b|c` reads "a, b, c"; anchors, groups, classes, repeats and quotes go."""
    out = SYNTAX.sub(lambda m: m[1] or (", " if "|" in m[0] else " " if m[0][0] == "." else ""), pattern)
    return " ".join(out.split()).strip(", ")


def step(name: str, args: dict[str, Any]) -> str:
    """A row's words, what the step is for and never the call (STYLE.md rule 4): a Bash call's own description,
    a search's folder ("your notes" in the memory tree) and terms, a file's plain name, a page's host, a query."""
    n = tool_name(name)
    path, where = str(args.get("file_path") or args.get("notebook_path") or args.get("path") or args.get("file") or ""), ""
    if n == "bash":                           # in the words the model wrote, a path as its plain name, no code marks
        what = FILE.sub(lambda m: plain(m[0]), " ".join(str(args.get("description") or "").replace("`", "").split()))
        return what[:1].lower() + what[1:] if what[1:2].islower() else what or words.STEP[n]   # "PDF" stays
    if n in ("worker", "schedule"):
        act, table = str(args.get("action") or ""), words.WORKER_STEP if n == "worker" else words.JOB_STEP
        return table.get(act, n).format(id=args.get("id") or "", name=args.get("name") or "").strip()
    if n == "settings":                       # neither a model nor a level: the call only reads what the room runs
        model, effort = words.model_name(str(args.get("model") or "")), str(args.get("effort") or "")
        what = " at ".join(x for x in (model, effort and f"{effort} thinking") if x)
        return words.STEP[n].format(what=what) if what else words.ROOM_NOW
    if n == "grep":
        where = words.NOTES if set(path.split("/")) & {"memory", "daily", "facts"} else plain(path)
        what = terms(str(args.get("pattern") or ""))
    elif n == "glob":
        what = " ".join(re.findall(r"\w+", os.path.splitext(str(args.get("pattern") or ""))[1]))   # **/*.md: md
    elif n in ("webfetch", "web_fetch", "fetch"):
        what = urlparse(str(args.get("url") or "")).hostname or str(args.get("url") or "")
    elif n == "send":
        what = os.path.basename(path) if path else words.MESSAGE_TO.format(room=args.get("room") or "")
    elif n == "hold":
        what = str(args.get("wait_for") or "")
    else:
        what = plain(path) if path else next((v for v in args.values() if isinstance(v, str) and v), "")
    return words.STEP.get(n, n.replace("_", " ") + " {what}").format(what=what, where=where)


@dataclass
class Row:
    id: str
    name: str
    text: str = ""
    state: str = RUNNING

    def line(self) -> str:
        text = self.text or words.STEP.get(tool_name(self.name), tool_name(self.name)).split("{")[0]   # no input yet
        return words.line(words.no_paths(f"{self.state} {text}".rstrip()), ROW_MAX)


class Card:
    def __init__(self, kind: str = "turn", thinking: bool = False, people: str = ""):
        self.kind, self.thinking_on = kind, thinking
        self.people = people                  # a room with people: its name, and the card's shape is _people()
        self.holding = ""                     # that room's newest holding line
        self.narration = ""
        self.text = ""                        # the text block streaming now
        self.thinking = ""
        self.thinking_at: int | None = None   # the open thinking block's index: its line waits for the block
        self.rows: list[Row] = []
        self.by_id: dict[str, Row] = {}
        self.by_index: dict[int, Row] = {}
        self.partial: dict[int, str] = {}     # input_json_delta per block index

    def feed(self, ev: dict[str, Any]) -> bool:
        """Take one stream event; True when the card's text changed."""
        before = self.render()
        if (kind := ev.get("type")) == "stream_event":
            self._stream(ev.get("event") or {})
        elif kind == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    row = self._row(str(c.get("id")), str(c.get("name")))
                    row.text = step(row.name, c.get("input") or {}) or row.text
                    self._said(row.name, c.get("input") or {})
        elif kind == "user":
            content = (ev.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if c.get("type") == "tool_result" and (hit := self.by_id.get(str(c.get("tool_use_id")))):
                    hit.state = FAILED if c.get("is_error") or tool_name(hit.name) == "send" and refused(c.get("content")) else OK
        return self.render() != before

    def _row(self, rid: str, name: str) -> Row:
        if rid not in self.by_id:
            self.by_id[rid] = Row(rid, name)
            self.rows.append(self.by_id[rid])
        return self.by_id[rid]

    def _stream(self, e: dict[str, Any]) -> None:
        t, i = e.get("type"), int(e.get("index") or 0)
        block = e.get("content_block") or {}
        delta = e.get("delta") or {}
        if t == "content_block_start" and block.get("type") == "text":
            self.text = ""
        elif t == "content_block_start" and block.get("type") == "thinking":
            self.thinking_at = i
        elif t == "content_block_stop" and i == self.thinking_at:
            self.thinking_at = None
        elif t == "content_block_start" and block.get("type") == "tool_use":
            self.by_index[i] = self._row(str(block.get("id")), str(block.get("name")))
            self.partial[i] = ""
        elif t == "content_block_delta" and delta.get("type") == "text_delta":
            self.text += str(delta.get("text") or "")
            if not self.people and (line := next((x for x in self.text.splitlines() if x.strip()), "")):
                self.narration = line
        elif t == "content_block_delta" and delta.get("type") == "thinking_delta":
            self.thinking += str(delta.get("thinking") or "")
        elif t == "content_block_delta" and delta.get("type") == "input_json_delta":
            self.partial[i] = self.partial.get(i, "") + str(delta.get("partial_json") or "")
        elif t == "content_block_stop" and i in self.by_index:
            row = self.by_index.pop(i)
            try:
                args = json.loads(self.partial.pop(i, "") or "{}")
            except ValueError:
                args = {}
            if isinstance(args, dict) and not row.text:
                row.text = step(row.name, args)
            if isinstance(args, dict):
                self._said(row.name, args)

    def _said(self, name: str, args: dict[str, Any]) -> None:
        """A room with people: a text send to itself (a room with people posts to itself only) is its holding line."""
        if self.people and tool_name(name) == "send" and str(args.get("room") or "") == self.people and not args.get("file"):
            if said := next((x for x in str(args.get("text") or "").splitlines() if x.strip()), ""):
                self.holding = said

    def render(self, head: str | None = None) -> str:
        if self.people:
            return self._people(head)
        lines = [head or words.line(f"{RUNNING} {self.narration or START}")]
        whole = self.thinking_at is None or len(self.thinking) >= THINKING_MAX   # never a fragment of a summary
        if self.thinking_on and self.thinking.strip() and whole:
            text = " ".join(self.thinking.split())[:THINKING_MAX]
            room = words.PHONE - len(words.THINKING)
            cut = -1 if len(text) <= room else text.rfind(" ", 0, room)
            first, rest = (text[:cut], text[cut + 1:]) if cut > 0 else (text, "")
            lines.append(words.SUB + words.line(words.THINKING + first))
            if rest:
                lines.append(words.SUB + words.line(rest + "..."))
        rows: list[Row | None] = []               # None: two or more failed steps in a row, folded into one
        for i, r in enumerate(self.rows):         # a line only for a row that shows: a long turn stays cheap
            if r.state == FAILED and i and self.rows[i - 1].state == FAILED:
                rows[-1] = None
            else:
                rows.append(r)
        if len(rows) > ROWS_SHOWN:
            lines.append(words.SUB + words.EARLIER.format(n=len(rows) - ROWS_SHOWN))
        fold = words.line(f"{FAILED} {words.ANOTHER_WAY}", ROW_MAX)
        return "\n".join(lines + [r.line() if r else fold for r in rows[-ROWS_SHOWN:]])

    def _people(self, head: str | None) -> str:
        """A room with people: its newest holding line, else the harness line or "working on it", in two phone lines at
        most, then the step count once a step has run (or `head`, an owed card's, then the count)."""
        said = " ".join((self.holding or self.narration or START).split())
        cut = said.rfind(" ", 0, words.PHONE - 2) if words.width(f"{RUNNING} {said}") > words.PHONE else -1
        first, rest = (said[:cut], said[cut + 1:]) if cut > 0 else (said, "")
        lines = [head] if head else [words.line(f"{RUNNING} {first}")] + ([words.line(rest)] if rest else [])
        return "\n".join(lines + ([words.SUB + words.STEP_N.format(n=len(self.rows))] if self.rows else []))

    def owed(self, why: str, asked: str) -> str:
        """The card of a turn that ends with nothing visible: not answered, why in plain words, what
        was asked, then the rows of what it did."""
        asked = words.line(words.no_paths(" ".join(asked.split())), words.PHONE - len(words.YOU_ASKED) + 7)
        asked = asked.replace('"', "'")
        return self.render(head="\n".join([f"{FAILED} {words.NOT_ANSWERED}", words.line(why),
                                            words.SUB + words.YOU_ASKED.format(asked=asked)]))
