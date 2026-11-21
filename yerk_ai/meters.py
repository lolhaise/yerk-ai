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


def rest_until(resets: float | None, meters: Meters | None, account: str, kind: str, now: float | None = None) -> float | None:
    """When a quota rest ends: the reset the limit named; for a rate limit that named none, the account's weekly reset from
    its last reading, an hour away at the least; None: the kind's backoff (failures.toml) stands."""
    week = ((meters.load().get(account) or {}).get("seven_day") or {}).get("resets") if meters and kind == "rate_limit" else None
    return resets or (max(float(week), (time.time() if now is None else now) + 3600) if week else None)


def meter_line(name: str, m: dict[str, Any], now: float | None = None) -> str:
    """'two: 5h 7% (11:40p) · wk 3% (Fri 10a)' (words.short_when; '?' for a reset not read), plus the reading's age when it came from a stream."""
    if "error" in m:
        return f"{name}: no meter ({m['error']})"
    now = time.time() if now is None else now
    parts = [f"{label} {round(m[w]['pct'])}% ({words.short_when(m[w]['resets'], now) if m[w].get('resets') else '?'})"
             for w, label in (("five_hour", "5h"), ("seven_day", "wk")) if w in m]
    age = f", seen {int((now - m['at']) // 60)} min ago"
    return f"{name}: " + (" · ".join(parts) or "no windows") + age


def meter_short(name: str, m: dict[str, Any], rests_until: float | None = None, now: float | None = None) -> str:
    """The morning diff's account line (morning.py): one phone-width line, state first, each reset the card's short way
    (words.short_when). Resting or a window at 100%: red, with when it comes back; a window at 90% or more: orange;
    else a dash (no colour: nothing to see), as with no reading yet. Where the resets do not fit beside the meters (a
    5-hour and a weekly window both read), they take a second line, the card's own, so a reset is never cut off."""
    if rests_until:
        return words.named(f"{words.NOT_DONE} {words.RESTS}", name, until=words.short_when(rests_until, now))
    if "error" in m:
        return words.named(f"{words.PLAIN} {words.NO_READING}", name)
    now = time.time() if now is None else now
    wins = [(label, m[w]) for w, label in (("five_hour", "5h"), ("seven_day", "wk")) if w in m]
    pct = [f"{label} {round(x['pct'])}%" for label, x in wins]
    at = [words.short_when(float(r), now) if (r := x.get("resets")) else "" for _, x in wins]
    top = max((m[w]["pct"] for w in WINDOWS if w in m), default=0)
    head = f"{words.NOT_DONE if top >= 100 else words.WORKING if top >= 90 else words.PLAIN} {{name}}: "
    one = head + (" · ".join(p + f" ({a})" * bool(a) for p, a in zip(pct, at)) or "no windows")
    if not any(at) or words.width(one.format(name=name)) <= words.PHONE:
        return words.named(one, name)
    resets = " · ".join(f"{label} {a}" for (label, _), a in zip(wins, at) if a)
    return "\n".join([words.named(head + " · ".join(pct), name), words.line(f"{words.PLAIN} {words.RESETS.format(parts=resets)}")])
