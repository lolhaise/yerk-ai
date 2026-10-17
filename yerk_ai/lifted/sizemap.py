# Adapted from claude-code-context-map, context_map/core.py (fmt, label_of,
# split_sections), the author's own MIT tool.
# Copyright (c) 2026 Eric McLaren. MIT License. See NOTICE.
"""Name and size the pieces of a block of text, without printing the text."""
from __future__ import annotations

import re
from typing import Callable

LABEL_MAX = 60
SEP_RE = re.compile(r" \u2014 | -- | - |; |: | \(| \u00b7 | \| ")
HEAD_RE = re.compile(r"#{1,2} \S.*")


def fmt(n: float) -> str:
    return f"{int(round(n)):,}"


def label_of(text: str, limit: int = LABEL_MAX) -> str:
    """A part's name from its own first non-empty line: leading # marks and the brackets of a
    [header] taken off, cut at the first separator and at `limit` characters, digits as N."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = line.lstrip("#").strip()
    if line.startswith("[") and "]" in line:
        line = line[1:line.index("]")]
    line = SEP_RE.split(line, maxsplit=1)[0]
    line = re.sub(r"\d+", "N", line).strip().rstrip(":")
    return line[:limit].rstrip() or "unlabelled"


def split_sections(block: str, size: Callable[[str], int] = len) -> list[tuple[str, int]]:
    """A block cut at its level 1 and 2 markdown headings, skipping headings inside ```
    fences. Returns [(name, size)]; the sizes add up to size(block)."""
    cuts: list[tuple[int, str]] = []
    pos, fence = 0, False
    for line in block.splitlines(keepends=True):
        if line.strip().startswith("```"):
            fence = not fence
        elif not fence and HEAD_RE.fullmatch(line.rstrip("\r\n")):
            cuts.append((pos, label_of(line)))
        pos += len(line)
    if not cuts or cuts[0][0] > 0:
        cuts.insert(0, (0, label_of(block)))
    ends = [c[0] for c in cuts[1:]] + [len(block)]
    return [(name, size(block[p:end])) for (p, name), end in zip(cuts, ends)]
