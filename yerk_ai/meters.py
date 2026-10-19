"""Account meters: each account's 5-hour and weekly usage with reset times.

One source: the last `rate_limit_event` Claude Code printed in a room's or a health call's
stream for that account (its own usage windows), with its age. It works under any login, the
one-year token included, and needs no credential: Yerk AI never reads the credential files in a
config folder (tests/test_scrub.py greps the package for any read under one)."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import words
from .accounts import Account

WINDOWS = ("five_hour", "seven_day")


class Meters:
    """state/meters.json: {account: {at, five_hour: {pct, resets}, seven_day: {...}}} from streams."""

    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()

    def load(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            return {}

    def seen(self, account: str, windows: dict[str, Any]) -> None:
        """A rate_limit_event's unifiedWindows (utilization 0 to 1, resetsAt epoch seconds)."""
        row: dict[str, Any] = {"at": time.time(), "source": "stream"}
        for w in WINDOWS:
            d = windows.get(w) or {}
            if d.get("utilization") is not None:
                row[w] = {"pct": round(float(d["utilization"]) * 100, 1), "resets": d.get("resetsAt")}
        with self.lock:
            data = self.load()
            data[account] = row
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)

    def reading(self, account: Account) -> dict[str, Any]:
        """The last stream reading for the account, else why there is none."""
        last = self.load().get(account.name)
        return dict(last) if last else {"error": "no stream has reported it yet"}


def when(epoch: float | None, now: float | None = None) -> str:
    """A reset as a clock time inside a day, else a weekday: phone-short."""
    if not epoch:
        return "?"
    t = datetime.fromtimestamp(epoch)
    return t.strftime("%H:%M") if epoch - (now or time.time()) < 86400 else t.strftime("%a %H:%M")


def meter_line(name: str, m: dict[str, Any], now: float | None = None) -> str:
    """'two: 5h 7% (23:40) · wk 3% (Fri 10:00)', plus the reading's age when it came from a stream."""
    if "error" in m:
        return f"{name}: no meter ({m['error']})"
    now = time.time() if now is None else now
    parts = [f"{label} {round(m[w]['pct'])}% ({when(m[w].get('resets'), now)})"
             for w, label in (("five_hour", "5h"), ("seven_day", "wk")) if w in m]
    age = f", seen {int((now - m['at']) // 60)} min ago"
    return f"{name}: " + (" · ".join(parts) or "no windows") + age


def meter_short(name: str, m: dict[str, Any], rests_until: float | None = None, now: float | None = None) -> str:
    """`yerk status`: one phone-width line, state first. Resting or a window at 100%: red, with when it
    comes back; a window at 90% or more: orange; else a dash (no colour: nothing to see), as with no
    reading yet. The reading's age is --full's, and the card's own line once it is an hour old."""
    if rests_until:
        return words.line(f"{words.NOT_DONE} {words.RESTS.format(name=name, until=words.clock(rests_until, now))}")
    if "error" in m:
        return words.line(f"{words.PLAIN} {words.NO_READING.format(name=name)}")
    now = time.time() if now is None else now
    parts = []
    for w, label in (("five_hour", "5h"), ("seven_day", "wk")):
        if w in m:
            r = m[w].get("resets")
            at = "" if not r else f" ({datetime.fromtimestamp(r).strftime('%H:%M' if r - now < 86400 else '%a')})"
            parts.append(f"{label} {round(m[w]['pct'])}%{at}")
    top = max((m[w]["pct"] for w in WINDOWS if w in m), default=0)
    mark = words.NOT_DONE if top >= 100 else words.WORKING if top >= 90 else words.PLAIN
    return words.line(f"{mark} {name}: " + (" · ".join(parts) or "no windows"))
