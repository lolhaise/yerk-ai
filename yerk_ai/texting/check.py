"""The check: each drafted reply or tapback (a `decided` row whose decision is reply or react) is read once, before it may go, by a
that can reach nothing of the owner's memory. The code's own pre-check runs first, with no model call: one hit holds the draft
with its reason. Then one `claude -p` with no tool, no MCP server, no settings and an empty folder is handed on stdin the
thread, the person's sheet as it is on disk now, their texts and the draft with its numbered statements, each fenced. The code
reads its answer and releases only what it can confirm itself: the model said release, failed nothing, left nothing out, and
gave every statement a basis that is a row of the owner's side in the thread it was handed, a key of the sheet, or a topic of
the owner's. Anything else, an error, a timeout or a missing field included, is a hold. The row gets the verdict and state
`release-ready`, or decision `held` with the reason, and one line of the dry file; nothing is sent, shown or posted."""
from __future__ import annotations

import json
import re
from typing import Any

from .. import words
from .people import Sheet, sheet_text
from .writer import USAGE, fence, line

PARTS, CHARS = 3, 400              # (e): more texts than this, or a longer one, is held by the code
SCHEMA: dict[str, Any] = {"type": "object", "required": ["release", "failed", "statements", "unlisted", "why"], "properties": {
    "release": {"type": "boolean"},
    "failed": {"type": "array", "items": {"enum": ["a", "b", "c", "d", "e"]}},
    "statements": {"type": "array", "items": {"type": "object", "required": ["n", "ok", "basis"], "properties": {
        "n": {"type": "integer"}, "ok": {"type": "boolean"}, "basis": {"type": "string"}}}},
    "unlisted": {"type": "array", "items": {"type": "string"}},
    "why": {"type": "string"}}}


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


def hold(reason: str) -> dict[str, Any]:
    return {"release": False, "failed": [], "statements": [], "unlisted": [], "why": "", "reason": reason}


def norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def keys(s: Sheet) -> set[str]:
    """What `sheet:<key>` may name: a class the sheet opens, or a fact in its `knows` by its words."""
    return {norm(c) for c in s.open} | {norm(f) for f, _ in s.knows}


def real(basis: str, his: set[str], known: set[str]) -> bool:
    """A basis the code confirms: `thread:` or `topic:` a row of the owner's side in the thread handed over, or `sheet:` a key
    of the sheet. A memory cite never is one: the check cannot read memory."""
    kind, _, what = basis.strip().partition(":")
    return kind in ("thread", "topic") and what in his or kind == "sheet" and norm(what) in known


def draft(row: dict[str, Any], his: set[str], known: set[str]) -> str:
    """The draft as the check reads it: the texts or the tapback, the bubble it answers, and each numbered statement with the
    source the writer claimed. One the code finds no basis for is marked claimed, not the owner's side, and a memory cite is shown as
    `memory` alone: its file is the owner's memory."""
    said = json.loads(row.get("statements") or "[]")
    out = [f"tapback: {row.get('react')}"] if row.get("decision") == "react" else [
        f"text {i}: {p}" for i, p in enumerate(json.loads(row.get("parts") or "[]"), 1)]
    out += [f"answers: {row.get('reply_to') or 'their newest text'}", "statements about the owner:" + ("" if said else " none")]
    for s in said:
        src = str(s.get("source") or "")
        shown = "memory" if src.startswith("memory:") else src or "none"
        out.append(f"{s.get('n')}. {s.get('text')} (source: {shown}" + ("" if real(src, his, known) else f"; {words.TEXTING_CHECK_CLAIMED}") + ")")
    return "\n".join(out)


def inputs(row: dict[str, Any], rows: list[dict[str, Any]], s: Sheet, tag: str) -> str:
    """The four inputs and nothing else, each fenced with this call's tag: the thread (each row marked as the writer's pack
    marks it), the sheet, their texts (the row's `inbound`) and the draft."""
    gs, his = set(str(row.get("inbound") or "").split(",")), {str(r.get("guid")) for r in rows if r.get("is_from_me") is True}
    return "\n\n".join([words.TEXTING_CHECK_PACK.format(tag=tag), fence("thread", "\n".join(line(r) for r in rows), tag),
                        fence("sheet", sheet_text(s), tag), fence("their texts", "\n".join(line(r) for r in rows if r.get("guid") in gs), tag),
                        fence("draft", draft(row, his, keys(s)), tag)])


def read(out: str, said: dict[int, str], his: set[str], known: set[str]) -> dict[str, Any]:
    """A call's stdout as a verdict, read as taste_judge reads its judge; `error` when the call failed (its reason, for the
    account hop). The model's release stands only when it also fails nothing, leaves nothing unlisted and gives every
    statement of the draft (`said`, by number) an ok with a basis the code confirms; else a hold, with every reason named."""
    try:
        d = json.loads(out)
        assert isinstance(d, dict)
    except (ValueError, AssertionError):
        return hold(words.TEXTING_NO_CLEAN)
    rec = {"seconds": round(float(d.get("duration_ms") or 0) / 1000, 2), "tokens": sum(int((d.get("usage") or {}).get(k) or 0) for k in USAGE)}
    if d.get("is_error"):
        return {**hold(words.TEXTING_NO_CLEAN), **rec, "error": str(d.get("result") or "no answer")[:300]}
    ans = d.get("structured_output")
    if not isinstance(ans, dict):
        try:
            ans = json.loads(d.get("result") or "")
        except ValueError:
            ans = None
    try:                                    # a field missing or of the wrong kind: no clean answer
        assert isinstance(ans, dict) and isinstance(ans["release"], bool) and isinstance(ans["why"], str)
        assert all(isinstance(ans[k], list) for k in ("failed", "unlisted", "statements"))
        failed = sorted({str(x) for x in ans["failed"]})
        unlisted = [" ".join(str(x).split()) for x in ans["unlisted"] if str(x).strip()]
        got = [{"n": int(s["n"]), "ok": s["ok"] is True, "basis": str(s["basis"]).strip()} for s in ans["statements"]]
    except (AssertionError, KeyError, TypeError, ValueError):
        return {**hold(words.TEXTING_NO_CLEAN), **rec}
    bad = sorted({s["n"] for s in got if not (s["ok"] and real(s["basis"], his, known))} | (set(said) - {s["n"] for s in got}))
    release = ans["release"] and not failed and not unlisted and not bad
    why = " ".join(ans["why"].split())
    reason = "" if release else "; ".join(x for x in (
        words.TEXTING_CHECK_FAILED.format(tests=", ".join(failed)) if failed else "",
        words.TEXTING_CHECK_UNSAID.format(what="; ".join(unlisted)) if unlisted else "",
        words.TEXTING_CHECK_NO_BASIS.format(what="; ".join(said.get(n) or str(n) for n in bad)) if bad else "", why) if x)
    return {"release": release, "failed": failed, "statements": got, "unlisted": unlisted, "why": why,
            "reason": reason or ("" if release else words.TEXTING_CHECK_HELD), **rec}
