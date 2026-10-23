# Adapted from Hermes Agent v0.22.2, agent/prompt_builder.py (_truncate_content and
# _omitted_headings), https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
# Changes: no logger or warning queue (the caller reports the cut), limits passed in
# from budgets.toml, sizes counted by the caller's own counter, whole lines only and the marker inside the limit, a `tail`
# mode for rule files.
"""Head-and-tail trimming (the head alone for a rule file) with a visible marker naming what was cut."""
from __future__ import annotations

import re
from typing import Callable

HEAD_RATIO = 0.7
TAIL_RATIO = 0.2
EXAMPLE = re.compile(r"<(/?)example(?:\s[^>]*)?>", re.I)      # a rule's example tags, as memory.EXAMPLE reads them


def omitted_headings(content: str, start: int, end: int, limit: int = 15) -> list[str]:
    """Markdown headings whose line starts inside content[start:end], so the marker tells the
    model what it lost; `#` lines inside fenced code blocks are comments, not headings."""
    headings: list[str] = []
    offset, fenced = 0, False
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and start <= offset < end and re.match(r"#{1,6} \S", stripped):
            headings.append(stripped.lstrip("#").strip())
        offset += len(line)
    return headings[:limit] + (["..."] if len(headings) > limit else [])


def truncate(content: str, name: str, max_chars: int, read_path: str | None = None, mode: str = "middle",
             count: Callable[[str], int] = len) -> tuple[str, int]:
    """Return (text, chars_cut). Under the limit the text is unchanged and the cut is 0. Over it whole lines go, never part of
    one, and the text returned, its marker included, measures at most `max_chars` by `count` ("" when not even the marker
    fits). Mode "tail", for a file of one rule a line with the newest last, keeps lines from the start and drops the rest:
    the newest lines go, never one from the middle."""
    if count(content) <= max_chars:
        return content, 0
    room = max_chars
    while room > 0:                  # what is kept shrinks by what the whole text, its marker included, is over
        out, kept = _cut(content, name, room, read_path, mode, count)
        if (over := count(out) - max_chars) <= 0:
            return out, len(content) - kept
        room -= over
    return "", len(content)


def _cut(content: str, name: str, room: int, read_path: str | None, mode: str, count: Callable[[str], int]) -> tuple[str, int]:
    """The text with about `room` characters of it kept, cut at lines' ends, and how many characters of it are kept."""
    head_chars = int(room * HEAD_RATIO)
    tail_chars = int(room * TAIL_RATIO)
    if mode == "tail":
        keep = content.rfind("\n", 0, head_chars + tail_chars + 1)
        tags = list(EXAMPLE.finditer(content))
        for a, b in reversed(list(zip(tags, tags[1:]))):     # an example over several lines open across the cut: it goes whole
            if not a.group(1) and b.group(1) and a.start() < keep < b.end():
                keep = content.rfind("\n", 0, a.start())
        keep = max(keep, 0)
        omitted = omitted_headings(content, keep, len(content))
        sections = f" Omitted sections: {'; '.join(omitted)}." if omitted else ""
        return content[:keep] + (f"\n\n[...trimmed {name}: kept the first {count(content[:keep])} of {count(content)} chars. Its "
                                 f"last lines, the newest, are omitted.{sections} The full file stays readable with the Read "
                                 f"tool: {read_path or name}]"), keep
    h = max(content.rfind("\n", 0, head_chars + 1), 0)                       # the head ends at a line's end
    t = content.find("\n", max(len(content) - tail_chars - 1, h))            # the tail starts at the next line's start
    t = len(content) if t < 0 else t + 1
    omitted = omitted_headings(content, h, t)
    sections = f" Omitted sections: {'; '.join(omitted)}." if omitted else ""
    marker = (
        f"\n\n[...trimmed {name}: kept {count(content[:h])}+{count(content[t:])} of {count(content)} chars. The middle is "
        f"omitted.{sections} The full file stays readable with the Read tool: {read_path or name}]\n\n"
    )
    return content[:h] + marker + content[t:], h + len(content) - t
