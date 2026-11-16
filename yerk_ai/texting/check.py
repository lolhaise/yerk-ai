"""The check: each drafted reply or tapback (a `decided` row whose decision is reply or react) is read once, before it may go, by a
that can reach nothing of the owner's memory. The code's own pre-check runs first, with no model call: one hit holds the draft
with its reason. Then one `claude -p` with no tool, no MCP server, no settings and an empty folder is handed on stdin the
thread, the person's sheet as it is on disk now, their texts and the draft with its numbered statements, each fenced. The code
reads its answer and releases only what it can confirm itself: the model said release, failed nothing, left nothing out, and
gave every statement a basis that is a row of the owner's side in the thread it was handed, a key of the sheet, or a topic of
the owner's. Anything else, an error, a timeout or a missing field included, is a hold. The row gets the verdict and state
`release-ready`, or decision `held` with the reason, and one line of the dry file; nothing is sent, shown or posted."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import words
from .people import Sheet, load, sheet_text
from .writer import Writer, answer, dry, fence, line, taps

log = logging.getLogger("yerk.texting")
PROMPT = Path(__file__).with_name("check.md")
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


def never(names: Any) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """[texting] never_say: the names an assistant goes by on this install, held as the built-in words are."""
    return tuple((w, whole(w)) for w in map(str, names or []) if w.strip())


def precheck(parts: list[str], open_: tuple[str, ...], names: tuple[tuple[str, re.Pattern[str]], ...] = ()) -> str:
    """The code's own holds, before any model call: (d) the writer naming itself or a name of `names`, (e) markdown or a shape
    past the cap, (c) a word of an always-held class the sheet does not open (money: an amount too). The reason, or "" when
    none hits."""
    text = "\n".join(parts)
    if said := next((w for w, rx in (*AI, *names) if rx.search(text)), ""):
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


def inputs(row: dict[str, Any], rows: list[dict[str, Any]], s: Sheet, tag: str, auto: set[str] | None = None) -> str:
    """The four inputs and nothing else, each fenced with this call's tag: the thread (each row marked as the writer's pack
    marks it), the sheet, their texts (the row's `inbound`) and the draft."""
    gs, his = set(str(row.get("inbound") or "").split(",")), {str(r.get("guid")) for r in rows if r.get("is_from_me") is True}
    return "\n\n".join([words.TEXTING_CHECK_PACK.format(tag=tag), fence("thread", "\n".join(line(r, auto=auto) for r in rows), tag),
                        fence("sheet", sheet_text(s), tag), fence("their texts", "\n".join(line(r, auto=auto) for r in rows if r.get("guid") in gs), tag),
                        fence("draft", draft(row, his, keys(s)), tag)])


def read(out: str, said: dict[int, str], his: set[str], known: set[str]) -> dict[str, Any]:
    """A call's stdout as a verdict (`writer.answer`). The model's release stands only when it also fails nothing, leaves
    nothing unlisted and gives every statement of the draft (`said`, by number) an ok with a basis the code confirms; else a
    hold, with every reason named."""
    ans, rec = answer(out, hold)
    if "reason" in rec:
        return rec
    try:                                    # a field missing or of the wrong kind: no clean answer
        assert isinstance(ans, dict) and isinstance(ans["release"], bool) and isinstance(ans["why"], str)
        assert all(isinstance(ans[k], list) for k in ("failed", "unlisted", "statements"))
        failed = sorted({str(x) for x in ans["failed"]})
        unlisted = [" ".join(str(x).split()) for x in ans["unlisted"] if str(x).strip()]
        got: list[dict[str, Any]] = [{"n": int(s["n"]), "ok": s["ok"] is True, "basis": str(s["basis"]).strip()} for s in ans["statements"]]
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


class Check:
    """The check's task beside the writer's: its loop, slots, account hop and dry file are the writer's own."""

    def __init__(self, conf: dict[str, Any], writer: Writer):
        self.w, self.model, self.cap = writer, str(conf.get("check_model") or writer.model), float(conf.get("check_seconds", 120))
        self.names = never(conf.get("never_say"))
        self.cwd = writer.state / "texting" / "check-cwd"            # empty, and nothing is ever written into it
        self.cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.config = writer.state / "texting" / "check-config"      # its own config folder, as empty: no account's settings or name
        self.config.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.argv = [writer.argv[0], "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--system-prompt-file",
                     str(PROMPT), "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
                     *(["--model", self.model] if self.model else []), "--effort", str(conf.get("effort") or "high")]

    async def run(self) -> None:
        """Each decided reply or tapback with no verdict yet, once: a none sends nothing and a held one is held already."""
        await self.w.run("decided", self.check, lambda r: r["decision"] in ("reply", "react") and not r["verdict"])

    async def check(self, row: dict[str, Any]) -> None:
        rid, key, now = int(row["id"]), str(row["person"]), self.w.clock()
        who = await asyncio.to_thread(load, self.w.file)          # the sheet as it is on disk now, never a start's copy
        if key not in who.people:
            v = hold(words.TEXTING_UNLISTED)
        elif why := precheck(json.loads(row["parts"] or "[]"), (s := who.sheets[key]).open, self.names):
            v = hold(why)
        else:
            rows = await self.w.last(key)
            his, said = {str(r.get("guid")) for r in rows if r.get("is_from_me") is True}, {
                int(x["n"]): str(x.get("text") or "") for x in json.loads(row["statements"] or "[]")}
            t0 = time.monotonic()
            v = await asyncio.to_thread(self.w.hop, self.argv, inputs(row, rows, s, secrets.token_hex(4), taps(self.w.ledger, key)), self.cwd, self.cap,
                                        self.model, lambda out: read(out, said, his, keys(s)), hold, config=self.config)
            v.setdefault("seconds", round(time.monotonic() - t0, 2))
        fields = {"state": "release-ready"} if v["release"] else {"decision": "held", "reason": v["reason"]}
        self.w.ledger.texting_set(rid, verdict=json.dumps(v), **fields)
        if self.w.mode == "dry":
            dry(self.w.state, datetime.fromtimestamp(now).astimezone(), {"t": now, "id": rid, "person": key, "verdict": v, **fields})
        log.info("texting: %s checked: %s (%s)", key, fields.get("state") or "held", self.w.mode)
