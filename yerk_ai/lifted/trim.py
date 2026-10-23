# Adapted from Hermes Agent v0.22.2, agent/prompt_builder.py (_truncate_content and
# _omitted_headings), https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
# Changes: no logger or warning queue (the caller reports the cut), limits passed in
# from budgets.toml, sizes counted by the caller's own counter, a `tail` mode for rule files.
"""Head-and-tail trimming (the head alone for a rule file) with a visible marker naming what was cut."""
from __future__ import annotations

import re

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


def truncate(content: str, name: str, max_chars: int, read_path: str | None = None, mode: str = "middle") -> tuple[str, int]:
    """Return (text, chars_cut). Under the limit the text is unchanged and the cut is 0. Mode "tail", for a file of one rule
    a line with the newest last, keeps the same share from the start, whole lines only, and drops the rest: the newest
    lines go, never one from the middle."""
    if len(content) <= max_chars:
        return content, 0
    head_chars = int(max_chars * HEAD_RATIO)
    tail_chars = int(max_chars * TAIL_RATIO)
    if mode == "tail":
        keep = content.rfind("\n", 0, head_chars + tail_chars + 1)
        tags = list(EXAMPLE.finditer(content))
        for a, b in reversed(list(zip(tags, tags[1:]))):     # an example over several lines open across the cut: it goes whole
            if not a.group(1) and b.group(1) and a.start() < keep < b.end():
                keep = content.rfind("\n", 0, a.start())
        keep = keep if keep > 0 else head_chars + tail_chars
        omitted = omitted_headings(content, keep, len(content))
        sections = f" Omitted sections: {'; '.join(omitted)}." if omitted else ""
        return content[:keep] + (f"\n\n[...trimmed {name}: kept the first {keep} of {len(content)} chars. Its last lines, the "
                                 f"newest, are omitted.{sections} The full file stays readable with the Read tool: "
                                 f"{read_path or name}]"), len(content) - keep
    omitted = omitted_headings(content, head_chars, len(content) - tail_chars)
    sections = f" Omitted sections: {'; '.join(omitted)}." if omitted else ""
    marker = (
        f"\n\n[...trimmed {name}: kept {head_chars}+{tail_chars} of {len(content)} chars. The middle is "
        f"omitted.{sections} The full file stays readable with the Read tool: {read_path or name}]\n\n"
    )
    out = content[:head_chars] + marker + content[-tail_chars:]
    return out, len(content) - head_chars - tail_chars
