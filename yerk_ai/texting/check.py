"""The check: each drafted reply or tapback (a `decided` row whose decision is reply or react) is read once, before it may go, by a
that can reach nothing of the owner's memory. The code's own pre-check runs first, with no model call: one hit holds the draft
with its reason. Then one `claude -p` with no tool, no MCP server, no settings and an empty folder is handed on stdin the
thread, the person's sheet as it is on disk now, their texts and the draft with its numbered statements, each fenced. The code
reads its answer and releases only what it can confirm itself: the model said release, failed nothing, left nothing out, and
gave every statement a basis that is a row of the owner's side in the thread it was handed, a key of the sheet, or a topic of
the owner's. Anything else, an error, a timeout or a missing field included, is a hold. The row gets the verdict and state
`release-ready`, or decision `held` with the reason, and one line of the dry file; nothing is sent, shown or posted."""
from __future__ import annotations

import re

from .. import words

PARTS, CHARS = 3, 400              # (e): more texts than this, or a longer one, is held by the code


def whole(word: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + r"[\s-]+".join(map(re.escape, word.split())) + r"(?!\w)", re.I)


AI = [(w, whole(w)) for w in words.TEXTING_AI_WORDS]
CLASS = {c: [whole(w) for w in ws] for c, ws in words.TEXTING_KEYWORDS.items()}
AMOUNT, BULLET = re.compile(words.TEXTING_AMOUNT, re.I), re.compile(r"(?m)^\s*- ")


def precheck(parts: list[str], open_: tuple[str, ...]) -> str:
    """The code's own holds, before any model call: (d) a word that names an assistant, (e) markdown or a shape past the cap,
    (c) a word of an always-held class the sheet does not open (money: an amount too). The reason, or "" when none hits."""
    text = "\n".join(parts)
    if said := next((w for w, rx in AI if rx.search(text)), ""):
        return words.TEXTING_CHECK_SAYS.format(word=said)
    if mark := next((m for m in words.TEXTING_MARKDOWN if m in text), "- " if BULLET.search(text) else ""):
        return words.TEXTING_CHECK_MARK.format(mark=mark)
    if len(parts) > PARTS or any(len(p) > CHARS for p in parts):
        return words.TEXTING_CHECK_PARTS.format(n=PARTS) if len(parts) > PARTS else words.TEXTING_CHECK_LONG.format(chars=CHARS)
    shut = [c for c, rxs in CLASS.items() if c not in open_ and (any(rx.search(text) for rx in rxs) or c == "money" and AMOUNT.search(text))]
    return words.TEXTING_HELD_BY.format(classes=", ".join(shut)) if shut else ""
