"""Claude Code hooks, answered over local HTTP (`type: http`, one POST per event).

UserPromptSubmit adds the per-message parts as context: the clock line and the metadata line.
Stop runs the rule-based checks (today one: an image sent in and never read bounces once).
The room comes from the payload's session id through the engine's room-to-session table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import context
from .engine import SessionTable

Meta = Callable[[str, str], tuple[str, float | None]]     # (room, prompt) -> (sender, previous human time)


def room_for_session(sessions: SessionTable, session_id: str) -> str | None:
    for room, row in sessions.load().items():
        if row.get("session_id") == session_id:
            return room
    return None


def user_prompt_submit(payload: dict[str, Any], state: Path, rooms: dict[str, dict[str, Any]], meta: Meta,
                       budgets: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    room = room_for_session(SessionTable(state / "sessions.json"), str(payload.get("session_id", "")))
    if room is None or room not in rooms:
        return {}
    sender, prev = meta(room, str(payload.get("prompt", "")))
    now = now or datetime.now(timezone.utc)
    last = datetime.fromtimestamp(prev, timezone.utc) if prev else None
    parts = context.per_message(room, rooms[room], sender, now, last, budgets)
    text = "\n".join(p.text for p in parts if p.text)[: int(budgets["hook"]["output_cap"]) - 1]
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def settings_for(url: str) -> dict[str, Any]:
    """The hooks block of a room's --settings file."""
    one = [{"hooks": [{"type": "http", "url": url, "timeout": 10}]}]
    return {"UserPromptSubmit": one, "Stop": one}
