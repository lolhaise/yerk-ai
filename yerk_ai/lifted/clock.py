# Adapted from the author's hook dispatcher in tools/hooks/: the live clock line (turn_clock_part
# and the clock part of prompt_context, 2026-09) and the room-voice part (channel_voice,
# 2026-08-28), without the envelope parsing, kill files and per-agent gates.
# Copyright (c) 2026 Eric McLaren. MIT License. See NOTICE.
"""The clock line on every turn (the local time, and how long since a person last wrote), and the text of a room's voice file."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


def gap(delta: timedelta) -> str:
    if (s := int(delta.total_seconds())) < 60:
        return "under a minute"
    m, h, d = s // 60, s // 3600, s // 86400
    if d:
        return f"{d}d {h % 24}h"
    return f"{h}h {m % 60}m" if h else f"{m}m"


def clock_line(now: datetime, last_human: datetime | None = None) -> str:
    """Local time, weekday and zone, plus the gap since the previous human message."""
    line = "[clock] " + now.strftime("%A %Y-%m-%d %H:%M %Z")
    if last_human is not None:
        line += f", {gap(now - last_human)} since the previous human message"
    return line


def room_voice(path: Path) -> str | None:
    """The room's voice file, stripped, or None when it is missing or empty."""
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return text or None
