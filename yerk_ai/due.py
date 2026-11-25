"""What is due, for the open-loops ability: the notes a room filed with a date through its `remember` tool (memory.due_of), and which are still open."""
from __future__ import annotations

from datetime import date, timedelta

from . import memory, mentions

KEEP = 14            # days past its date a dated line is still open


def kept(day: str, today: date) -> bool:
    """A date no more than KEEP days past `today`."""
    return day >= (today - timedelta(days=KEEP)).isoformat()


def is_open(raw: str, today: date, day: str = "") -> bool:
    """A fact file's line, or with `day` a day log's, that is dated and open by its own words: no superseded mark on it, and its
    date no more than KEEP days past. The list drops a line that memory.LATER names as replaced too (Builder)."""
    x = None if mentions.OLD in raw else memory.due_of(raw, day)
    return x is not None and kept(x["date"], today)
