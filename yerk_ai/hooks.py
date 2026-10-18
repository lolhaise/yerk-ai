"""Claude Code hooks, answered over local HTTP (`type: http`, one POST per event).

UserPromptSubmit adds the per-message parts as context: the clock line, the metadata line and the
recall part (the owner's people and things named in the message, then this room's own lines of the
last 14 days on the message's topic), the recall part capped at budgets.toml's `recall`. Every
assembly is written to the room's assembled.jsonl, the daemon's own record of what the model got.
SessionStart with source `compact` re-adds the recall pack. Claude Code takes only command and
mcp_tool hooks on SessionStart, so that one entry is a command that posts the same payload to the
same endpoint with curl; on `startup` or `clear` after another session it posts the reset line.
PreToolUse runs the gates (gates.py), PostToolUse and PostToolUseFailure time each call, ConfigChange
writes the tamper row. Stop runs the rule-based checks: an image sent in and never read bounces
once, a working turn ending with nothing visible bounces once; the evidence rules log what they
would bounce and never block (shadow).
The room comes from the payload's session id through the engine's room-to-session table.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import context
from .engine import SessionTable

Meta = Callable[[str, str], tuple[str, float | None]]     # (room, prompt) -> (sender, previous human time)
Recall = Callable[[str, str], tuple[list[dict[str, Any]], list[dict[str, Any]]]]   # -> (name entries, topic hits)
RECORD_MAX = 5_000_000                                    # assembled.jsonl rolls to .1 past this


def room_for_session(sessions: SessionTable, session_id: str) -> str | None:
    for room, row in sessions.load().items():
        if row.get("session_id") == session_id:
            return room
    return None


def render_recall(names: list[dict[str, Any]], topics: list[dict[str, Any]], budget: int) -> str:
    """Names first, then topic lines, each line whole or not at all, under `budget` characters."""
    blocks = [("[on file, no lookup needed: the people and things this message names]",
               [f"- {e['text'] if e['text'].startswith(e['key']) else e['key'] + ': ' + e['text']} ({e['cite']})"
                for e in names]),
              ("[this room, last 14 days, on this topic]",
               [f"- {h['day'][5:]} {h['time']} {h['role']}: {h['text'][:280]} ({h['cite']})" for h in topics])]
    out: list[str] = []
    used = -1                                   # n lines joined by n - 1 newlines
    for head, lines in blocks:
        take, cost = [], context.chars(head) + 1
        for line in lines:
            c = context.chars(line) + 1
            if used + cost + c <= budget:
                take.append(line)
                cost += c
        if take:
            out += [head, *take]
            used += cost
    return "\n".join(out)


def record(state: Path, room: str, event: str, session_id: str, parts: dict[str, int], text: str,
           **extra: Any) -> None:
    """One line in the room's assembled.jsonl: what was put in front of the model, part by part."""
    path = state / "rooms" / room / "assembled.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > RECORD_MAX:
        os.replace(path, path.with_suffix(".jsonl.1"))
    line = json.dumps({"t": round(time.time(), 3), "event": event, "session_id": session_id, "parts": parts,
                       "chars": context.chars(text), **extra, "text": text})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def user_prompt_submit(payload: dict[str, Any], state: Path, rooms: dict[str, dict[str, Any]], meta: Meta,
                       budgets: dict[str, Any], now: datetime | None = None,
                       recall: Recall | None = None) -> dict[str, Any]:
    sid = str(payload.get("session_id", ""))
    room = room_for_session(SessionTable(state / "sessions.json"), sid)
    if room is None or room not in rooms:
        return {}
    prompt = str(payload.get("prompt", ""))
    sender, prev = meta(room, prompt)
    now = now or datetime.now(timezone.utc)
    last = datetime.fromtimestamp(prev, timezone.utc) if prev else None
    parts = context.per_message(room, rooms[room], sender, now, last, budgets)
    names: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []
    took = None
    if recall:
        t0 = time.monotonic()
        names, topics = recall(room, prompt)
        cap = int(budgets["per_message"].get("on_hit", {}).get("recall", 1500))
        rtext = render_recall(names, topics, cap)
        parts.append(context.Part("recall", rtext, cap, context.chars(rtext)))
        took = round((time.monotonic() - t0) * 1000)
    text = "\n".join(p.text for p in parts if p.text)[: int(budgets["hook"]["output_cap"]) - 1]
    record(state, room, "message", sid, {p.name: p.size for p in parts}, text, prompt_chars=context.chars(prompt),
           names=[e["key"] for e in names], topics=[h["cite"] for h in topics], recall_ms=took)
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def session_start(payload: dict[str, Any], state: Path, pack: Callable[[str], str]) -> dict[str, Any]:
    """After a compaction the recall pack comes back, fresh from the day logs (DESIGN 3)."""
    if payload.get("source") != "compact":
        return {}
    sid = str(payload.get("session_id", ""))
    room = room_for_session(SessionTable(state / "sessions.json"), sid)
    if room is None:
        return {}
    text = pack(room)
    record(state, room, "compact", sid, {"recall_pack": context.chars(text)}, text)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}} if text else {}


def settings_for(url: str) -> dict[str, Any]:
    """The hooks block of a room's --settings file: every event an http post to the daemon, but
    SessionStart, which Claude Code runs only as a command (curl posts the same payload). Resume is
    left out of its matcher: a resumed session is not a reset."""
    one = [{"hooks": [{"type": "http", "url": url, "timeout": 10}]}]
    post = f"curl -sS --max-time 10 -H 'content-type: application/json' --data-binary @- {url}"
    return {"UserPromptSubmit": one, "Stop": one, "PreToolUse": one, "PostToolUse": one, "PostToolUseFailure": one,
            "ConfigChange": one,
            "SessionStart": [{"matcher": "startup|clear|compact",
                              "hooks": [{"type": "command", "command": post, "timeout": 15}]}]}
