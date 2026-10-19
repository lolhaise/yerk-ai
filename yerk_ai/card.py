"""A turn's progress card, built from Claude Code's stream events. One per turn of every kind.

In order: the state and the narration line (the model's newest text as it streams in), an optional
thinking line (owner-only rooms that turn it on; the start of the summary, then "..."), then one
row per tool call as it starts, a plain verb and its target (a file name, a command's head, a URL's
host), marked done or not done when its result comes back. Every line holds to phone width
(words.PHONE); the newest rows win when there are many. The words come from words.py (STYLE.md).
The room swaps the card for the reply at the end, or edits it to say what was owed when a turn ends
with nothing to show. (Ideas from OpenClaw's session cards; no code.)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import words
from .words import DONE as OK, NOT_DONE as FAILED, START, WORKING as RUNNING

ROW_MAX, THINKING_MAX, ROWS_SHOWN = words.PHONE, 2 * words.PHONE, 12


def short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def tool_name(name: str) -> str:
    """mcp__yerk__send -> send; Read -> read."""
    return (name.split("__")[-1] if name.startswith("mcp__") else name).lower()


def verb(name: str) -> str:
    """The row's verb in plain words: Bash -> run, WebFetch -> fetch, another server's tool by its own name."""
    n = tool_name(name)
    return words.VERBS.get(n, n.replace("_", " "))


def target(name: str, args: dict[str, Any]) -> str:
    """The tool's target in plain words."""
    n = tool_name(name)
    path = args.get("file_path") or args.get("notebook_path") or args.get("file")
    if n in ("read", "write", "edit", "multiedit", "notebookedit") and path:
        return os.path.basename(str(path))
    if n == "bash" and args.get("command"):
        return str(args["command"]).strip().splitlines()[0]
    if n in ("webfetch", "fetch") and args.get("url"):
        return urlparse(str(args["url"])).hostname or str(args["url"])
    if n == "grep" and args.get("pattern"):
        where = os.path.basename(str(args.get("path") or "").rstrip("/"))
        return f'"{args["pattern"]}"' + (f" in {where}" if where else "")
    if n == "glob" and args.get("pattern"):
        return str(args["pattern"])
    if n == "send":
        return f"file {os.path.basename(str(path))}" if path else "a message"
    if n == "hold":
        return str(args.get("wait_for") or "")
    if n == "worker":
        return " ".join(str(args.get(k) or "") for k in ("action", "id")).strip() or "a worker"
    if n in ("recall", "websearch") and args.get("query"):
        return f'"{args["query"]}"'
    first = next((v for v in args.values() if isinstance(v, str) and v), "")
    return str(first)


@dataclass
class Row:
    id: str
    name: str
    target: str = ""
    state: str = RUNNING

    def line(self) -> str:
        return words.line(words.no_paths(f"{self.state} {verb(self.name)} {self.target}".rstrip()), ROW_MAX)


class Card:
    def __init__(self, kind: str = "turn", thinking: bool = False):
        self.kind, self.thinking_on = kind, thinking
        self.narration = ""
        self.text = ""                        # the text block streaming now
        self.thinking = ""
        self.thinking_tokens = 0
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
                    row.target = short(target(row.name, c.get("input") or {}), ROW_MAX) or row.target
        elif kind == "user":
            content = (ev.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if c.get("type") == "tool_result" and (hit := self.by_id.get(str(c.get("tool_use_id")))):
                    hit.state = FAILED if c.get("is_error") else OK
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
            if (line := next((x for x in self.text.splitlines() if x.strip()), "")):
                self.narration = line
        elif t == "content_block_delta" and delta.get("type") == "thinking_delta":
            self.thinking += str(delta.get("thinking") or "")
            self.thinking_tokens += int(delta.get("estimated_tokens") or 0)
        elif t == "content_block_delta" and delta.get("type") == "input_json_delta":
            self.partial[i] = self.partial.get(i, "") + str(delta.get("partial_json") or "")
        elif t == "content_block_stop" and i in self.by_index:
            row = self.by_index.pop(i)
            try:
                args = json.loads(self.partial.pop(i, "") or "{}")
            except ValueError:
                args = {}
            if isinstance(args, dict) and not row.target:
                row.target = short(target(row.name, args), ROW_MAX)

    def render(self, head: str | None = None) -> str:
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
        rows = self.rows[-ROWS_SHOWN:]
        if len(self.rows) > len(rows):
            lines.append(words.SUB + words.EARLIER.format(n=len(self.rows) - len(rows)))
        lines += [r.line() for r in rows]
        return "\n".join(lines)

    def owed(self, why: str, asked: str) -> str:
        """The card of a turn that ends with nothing visible: not answered, why in plain words, what
        was asked, then the rows of what it did."""
        asked = words.line(words.no_paths(" ".join(asked.split())), words.PHONE - len(words.YOU_ASKED) + 7)
        asked = asked.replace('"', "'")
        return self.render(head="\n".join([f"{FAILED} {words.NOT_ANSWERED}", words.line(why),
                                            words.SUB + words.YOU_ASKED.format(asked=asked)]))
