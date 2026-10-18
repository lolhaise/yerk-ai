"""A room's context: the session-start file built to its budget, and the small per-message parts.

Session start, most stable first so the cached start of the prompt survives: Claude Code's own
prompt, tools and skills, then one appended file holding the mechanics, persona, rulebook, room
voice, pinned memory and the recall pack. Per message: only parts with news, starting with the
clock line and the message metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .lifted.clock import clock_line, room_voice
from .lifted.sizemap import fmt, split_sections
from .lifted.trim import truncate

MECHANICS = Path(__file__).with_name("mechanics.md")
APPENDED = ("mechanics", "persona", "rulebook", "room_voice", "pinned_memory", "recall_pack")
SEP = "\n\n"


def chars(s: str) -> int:
    """Characters as Claude Code counts them: UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


@dataclass
class Part:
    name: str
    text: str            # what is sent, trimmed when over budget
    budget: int | None
    raw: int             # size before trimming
    source: str = ""

    @property
    def size(self) -> int:
        return chars(self.text)

    @property
    def over(self) -> bool:
        return self.budget is not None and self.raw > self.budget


def layer_dir(private: Path, room: dict[str, Any]) -> Path:
    """The folder a room's context files come from: its `layer` (absolute, or relative to the
    private folder), else the private folder itself. The room's process may read it."""
    return (private / str(room.get("layer", "."))).resolve()


def cited(path: Path, text: str) -> str:
    """A file's text under the absolute path it came from, so the model can re-read it."""
    return f"[from {path}]\n{text}" if text else ""


def _read(base: Path, rel: str | None) -> tuple[str, str]:
    if not rel or not (base / rel).is_file():
        return "", rel or ""
    p = (base / rel).resolve()
    return cited(p, p.read_text().strip()), str(p)


def recall_pack(base: Path, room: dict[str, Any]) -> tuple[str, str]:
    """The room's newest daily notes, newest first, then the open commitments."""
    texts, srcs = [], []
    notes = base / str(room.get("daily_notes", "memory/daily"))
    if notes.is_dir():
        for f in sorted(notes.glob("*.md"), reverse=True)[: int(room.get("recall_days", 2))]:
            texts.append(cited(f.resolve(), f.read_text().strip()))
            srcs.append(str(f.resolve()))
    loops, src = _read(base, room.get("open_loops"))
    if loops:
        texts.append(loops)
        srcs.append(src)
    return SEP.join(texts), ", ".join(srcs)


def session_start(private: Path, room: dict[str, Any], budgets: dict[str, Any]) -> list[Part]:
    limits = budgets["session_start"]["appended"]
    base = layer_dir(private, room)
    voice = str(room.get("voice") or "")
    vpath = (base / voice).resolve() if voice else None
    raw = {
        "mechanics": (MECHANICS.read_text().strip(), "yerk_ai/mechanics.md"),
        "persona": _read(base, room.get("persona")),
        "rulebook": _read(base, room.get("rulebook")),
        "room_voice": (cited(vpath, room_voice(vpath) or ""), str(vpath)) if vpath else ("", ""),
        "pinned_memory": _read(base, room.get("pinned_memory")),
        "recall_pack": recall_pack(base, room),
    }
    parts = []
    for name in APPENDED:
        text, src = raw[name]
        limit = int(limits[name])
        sent, _ = truncate(text, name, limit, read_path=src or None)
        parts.append(Part(name, sent, limit, chars(text), src))
    return parts


def appended_file(parts: list[Part]) -> str:
    """The one file passed by --append-system-prompt-file."""
    return SEP.join(p.text for p in parts if p.text) + "\n"


def per_message(room_name: str, room: dict[str, Any], sender: str, now: datetime,
                last_human: datetime | None, budgets: dict[str, Any]) -> list[Part]:
    tz = ZoneInfo(str(room.get("timezone", "UTC")))
    clock = clock_line(now.astimezone(tz), last_human.astimezone(tz) if last_human else None)
    meta = f"[message] room {room_name}, from {sender}, privacy {room.get('privacy', 'owner')}"
    limits = budgets["per_message"]["parts"]
    return [Part("clock", clock, limits.get("clock"), chars(clock)),
            Part("metadata", meta, limits.get("metadata"), chars(meta))]


def message_text(parts: list[Part], body: str) -> str:
    return "\n".join(p.text for p in parts if p.text) + SEP + body


def session_total(parts: list[Part], budgets: dict[str, Any]) -> int:
    """Yerk AI's parts at their untrimmed size, plus Claude Code's parts at their budget."""
    present = [p for p in parts if p.raw]
    return sum(budgets["session_start"]["claude_code"].values()) + sum(p.raw for p in present) \
        + chars(SEP) * max(len(present) - 1, 0)


def violations(start: list[Part], msg: list[Part], budgets: dict[str, Any]) -> list[str]:
    out = [f"{p.name}: {p.raw:,} > {p.budget:,} ({p.source or 'no file'})" for p in start + msg if p.over]
    total, limit = session_total(start, budgets), int(budgets["session_start"]["total"])
    if total > limit:
        out.append(f"session start total: {total:,} > {limit:,}")
    every, typical = sum(p.raw for p in msg), int(budgets["per_message"]["typical"])
    if every > typical:
        out.append(f"every message: {every:,} > {typical:,}")
    return out


def render(room_name: str, private_label: str, start: list[Part], msg: list[Part], budgets: dict[str, Any]) -> str:
    row = "  {:<30}{:>9}{:>9}  {}"
    L = [f"yerk map {room_name}  (private folder: {private_label})",
         "Characters as Claude Code counts them. Untrimmed sizes; an over-budget file is sent trimmed.", "",
         f"{'Session start, in load order':<32}{'chars':>9}{'budget':>9}  status"]
    for name, b in budgets["session_start"]["claude_code"].items():
        L.append(row.format(name, "-", fmt(b), "sent by Claude Code; counted at budget"))
    for p in start:
        status = f"OVER, sent trimmed to {fmt(p.size)}" if p.over else ("ok" if p.raw else "empty")
        L.append(row.format(p.name, fmt(p.raw), fmt(p.budget or 0), status + (f"  [{p.source}]" if p.source else "")))
        sections = split_sections(p.text, chars) if p.text else []
        for sec, n in sections[1:7] if len(sections) > 1 else []:
            L.append(f"      {sec[:26]:<26}{fmt(n):>9}")
    total, limit = session_total(start, budgets), int(budgets["session_start"]["total"])
    L.append(row.format("total", fmt(total), fmt(limit), "ok" if total <= limit else "OVER"))
    L.append(row.format("  of which Yerk AI's file", fmt(chars(appended_file(start))), "", "as sent"))
    L += ["", f"{'Every message':<32}{'chars':>9}{'budget':>9}  status"]
    for p in msg:
        L.append(row.format(p.name, fmt(p.raw), fmt(p.budget or 0), "OVER" if p.over else "ok"))
    every, pm = sum(p.raw for p in msg), budgets["per_message"]
    L.append(row.format("total", fmt(every), fmt(pm["typical"]),
                        f"{'ok' if every <= pm['typical'] else 'OVER'} (cap {fmt(pm['cap'])})"))
    return "\n".join(L)
