"""Your ratings of a room's replies: a message of yours that praises or faults the room's last reply is kept as one
row in taste.jsonl, and every message to one of your own rooms carries the rated replies, your words beside what
the room had said. The format is the one an earlier setup of the maintainer's used, so replies rated there carry
the owner's files).

The classifier is that setup's too, its three lists unchanged: a verdict is a phrase about the reply (praise, its length, a
repeat, the wrong register), never about the room (an insult aimed at the room, even seconds after praise for its reply, rates
nothing), praise with a complaint is the complaint, and anything else rates nothing. The file is the switch: a layer without
no row and no part, and a room that is not the owner's gets neither.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from . import words
from .context import chars, layer_dir

FILE = "taste.jsonl"
HEAD = "[taste] Rated by the owner, newest first (their words → what you had said):"

POS = [
    r"\b(that|this)('?s| was| is|s)? (actually |lowkey |so |really |way |a )?(good|great|hype|fire|clean|hard|funny|dope|better|perfect|nice one|the one)\b",
    r"\bway better\b", r"\bgood one\b", r"\bfunny guy\b", r"\blove (it|that)\b", r"\bperfect\b",
    r"\b(lmao|lmfao|lool+|hahah+a?)\b", r"😂|🤣", r"\bbig w\b", r"^\s*w\s*$", r"\bthat'?s it\b",
    r"\b(dope|fire|hype)\b(?! (guy|man{dialect}) (i|you))",     # {dialect}: the owner's own words (words.dialect_alt) at classify
]
NEG = [
    r"(ain'?t|not|aint) reading (all )?(that|this)", r"\btoo long\b", r"\btl;?dr\b",
    r"\byou (have |had )?said (that|this|it)\b", r"\blike \d+ times\b", r"\bagain\?", r"\byou keep\b",
    r"\bnot what i (asked|meant|said)\b", r"\bwhy (did|do|are|would) you\b", r"\bstop (saying|doing|with)\b",
    r"\bi want(ed)? (to know )?your opinion", r"\bguess what\b", r"\bwrite it properly\b", r"\bshorter\b", r"\b(didn['’]?t|did not) (even )?hype me( up)?\b",
    r"\bdon'?t (say|do) that\b", r"\bthat'?s (not|wrong)\b", r"\bnah (man|bro|that)\b", r"\bnot long enough\b",
]
# The box's register verdicts (2026-08-29): a negated appraisal the reply should have held ("feel like you don't think
# I did anything good") or how it should sound ("you gotta be more energetic", "don't make your responses so formal").
REGISTER = [
    r"\bfeel(s)? like (you|u)\b[^.?!]{0,30}?"
    r"\b(don['’]?t|dont|did ?n['’]?t|didnt|never|are ?n['’]?t|arent|ain['’]?t)\s+"
    r"(even\s+|really\s+|actually\s+|fully\s+)?"
    r"(think|get\b|care|realiz|understand|see (how|what|that)|believe|hyped|excited|"
    r"happy|impressed|proud|appreciat)",
    r"(\b(you|u)\b|\b(your|ur) responses?\b)[^.?!]{0,60}?"
    r"(\b(gotta|need to|needa|should|have to) be more \w+"
    r"|\bdon['’]?t (make|be) (your|ur|so)\b[^.?!]{0,30}?"
    r"\b(formal|robotic|dry|long|serious|corporate|stiff|boring)\b)",
]
NEG = NEG + REGISTER


def classify(text: str) -> str | None:
    """"pos", "neg" or None, as the box's classify()."""
    t = (text or "").strip().lower()
    if not t:
        return None
    neg = any(re.search(p, t) for p in NEG)
    pos = any(re.search(p.replace("{dialect}", words.dialect_alt()), t) for p in POS)
    if neg and not pos:
        return "neg"
    if pos and not neg:
        return "pos"
    if pos and neg:
        return "neg"  # "that was good but way too long": the complaint is the actionable half
    return None


def kept(private: Path, room: dict[str, Any]) -> Path | None:
    """The layer's taste.jsonl when the room is an owner room and its layer keeps one, else None."""
    p = layer_dir(private, room) / FILE
    return p if str(room.get("privacy", "owner")) == "owner" and p.is_file() else None


def load(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if isinstance(r := json.loads(line), dict):
                rows.append(r)
        except ValueError:
            continue
    return rows


def record(path: Path, room: str, channel: str, text: str, reply: dict[str, Any] | None, tz: tzinfo) -> dict[str, Any] | None:
    """The owner's message `text`: on a verdict, one row on `reply` (the room's last reply: id, sent, text) appended,
    unless that reply already has a row of that sign. The row, or None."""
    if (verdict := classify(text)) is None or reply is None:
        return None
    if any(r.get("reply_id") == reply["id"] and r.get("verdict") == verdict for r in load(path)):
        return None
    row = {"ts": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S"), "channel": channel, "verdict": verdict,
           "signal": text.strip()[:300], "reply_id": reply["id"], "reply_at": datetime.fromtimestamp(reply["sent"], tz).strftime("%H:%M"),
           "reply": reply["text"][:4000], "room": room}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def excerpt(s: str, n: int | None) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    return s if n is None or len(s) <= n else s[: n - 1].rstrip() + "…"


def render(rows: list[dict[str, Any]], room: str, budget: int, pos_n: int = 3, neg_n: int = 2, each: int = 260, whole: bool = False) -> str:
    """The box's `taste-ledger examples` for `room` as one part: up to `pos_n` praised and `neg_n` faulted replies, the room's
    own newest first, then the other rooms' newest; each reply cut 40 shorter, down to 80, until the part fits `budget`;
    `whole` (an uncapped install): each row's words and reply whole, the part never fitted, the counts the same."""
    seen: set[tuple[Any, Any]] = set()
    pos: list[dict[str, Any]] = []
    neg: list[dict[str, Any]] = []
    for r in [r for r in reversed(rows) if r.get("room") == room] + [r for r in reversed(rows) if r.get("room") != room]:
        if (key := (r.get("reply_id"), r.get("verdict"))) not in seen:
            seen.add(key)
            (pos if r.get("verdict") == "pos" else neg).append(r)
    picked = pos[:pos_n] + neg[:neg_n]
    if not picked:
        return ""

    def text(n: int | None) -> str:
        return "\n".join([HEAD] + [f"{'👍' if r.get('verdict') == 'pos' else '👎'} \"{excerpt(str(r.get('signal', '')), n and 80)}\" → "
                                   f"\"{excerpt(str(r.get('reply', '')), n)}\"" for r in picked])
    if whole:
        return text(None)
    out = text(each)
    while chars(out) > budget and each > 80:
        each -= 40
        out = text(each)
    while chars(out) > budget:                   # still over at 80 each: the last reply goes whole, never part of a line
        out = out.rpartition("\n")[0]
    return out
