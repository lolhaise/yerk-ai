"""Claude Code hooks: every event a command hook (curl posting its JSON to the daemon, see settings_for).

UserPromptSubmit adds the per-message parts: the clock line, the metadata line, the rules changed since the session
last looked (context.news, capped at `rules`) and the recall part (the owner's people and things the message names,
then this room's lines of the last 14 days on its topic, capped at `recall`); every assembly goes to the room's
assembled.jsonl. SessionStart `compact` re-adds the recall pack; `startup` or `clear` after another session posts the reset line.
PreToolUse runs the gates (gates.py), PostToolUse and PostToolUseFailure time each call, ConfigChange
writes the tamper row, Stop runs the rule-based checks (an unread image or a turn ending with nothing
visible bounces once; the evidence rules only log). The room comes from the call's token, else from the
session id through the room-to-session table; an unbound call gets the marker and one loud log line.
"""
from __future__ import annotations

import json
import logging
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
log = logging.getLogger("yerk.hooks")
UNBOUND = {"yerk": "unbound"}          # the answer to a hook call no room binding explains (R3)


def room_for_session(sessions: SessionTable, session_id: str) -> str | None:
    return sessions.by_session(session_id)


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
            if used + cost + (c := context.chars(line) + 1) <= budget:
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
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), "event": event, "session_id": session_id, "parts": parts,
                            "chars": context.chars(text), **extra, "text": text}) + "\n")


def user_prompt_submit(payload: dict[str, Any], state: Path, rooms: dict[str, dict[str, Any]], meta: Meta,
                       budgets: dict[str, Any], now: datetime | None = None,
                       recall: Recall | None = None, room: str | None = None, news: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    sid = str(payload.get("session_id", ""))
    room = room or room_for_session(SessionTable(state / "sessions.json"), sid)
    if room is None or room not in rooms:
        log.warning("hook: UserPromptSubmit for session %s matches no room binding; answered unbound", sid[:8] or "?")
        return dict(UNBOUND)
    prompt = str(payload.get("prompt", ""))
    sender, prev = meta(room, prompt)
    now = now or datetime.now(timezone.utc)
    last = datetime.fromtimestamp(prev, timezone.utc) if prev else None
    parts = context.per_message(room, rooms[room], sender, now, last, budgets)
    if news and (told := news(room, sid)):                     # (room, session) -> the rules block, or ""
        parts.append(context.Part("rules", told, int(budgets["per_message"]["on_hit"]["rules"]), context.chars(told)))
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


def session_start(payload: dict[str, Any], state: Path, pack: Callable[[str], str],
                  room: str | None = None) -> dict[str, Any]:
    """After a compaction the recall pack comes back, fresh from the day logs (DESIGN 3)."""
    if payload.get("source") != "compact":
        return {}
    sid = str(payload.get("session_id", ""))
    if (room := room or room_for_session(SessionTable(state / "sessions.json"), sid)) is None:
        log.warning("hook: SessionStart (compact) for session %s matches no room binding; answered unbound", sid[:8] or "?")
        return dict(UNBOUND)
    text = pack(room)
    record(state, room, "compact", sid, {"recall_pack": context.chars(text)}, text)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}} if text else {}


def settings_for(url: str, token: str = "") -> dict[str, Any]:
    """The hooks block of a room's --settings file (written 0600): every event a command hook, curl posting
    the event's JSON to the daemon with the process's token as its secret header. Never `type: http`: Claude
    Code 2.1.301 sends those through a sandboxed room's network proxy, which refuses the daemon's loopback port
    with 403 (2026-10-19, 389 silent refusals in two rooms). Resume is out of SessionStart's matcher: no reset."""
    from .mcp_server import HEADER
    post = (f"curl -sS -f --max-time 10 -H 'content-type: application/json' -H '{HEADER}: {token}' "
            f"--data-binary @- {url}")          # -f: a non-2xx answer is a hook error in the transcript, as over http
    one = [{"hooks": [{"type": "command", "command": post, "timeout": 10}]}]
    return {"UserPromptSubmit": one, "Stop": one, "PreToolUse": one, "PostToolUse": one, "PostToolUseFailure": one,
            "ConfigChange": one, "SessionStart": [{"matcher": "startup|clear|compact",
                                                   "hooks": [{"type": "command", "command": post, "timeout": 15}]}]}
