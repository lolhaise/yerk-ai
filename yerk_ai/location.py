"""Says which of your named places you are at, from the location fixes a phone watcher writes to a file (yerkd.toml
[location]): the `place` line a message to one of your own rooms carries, and the day's places as one line in the day log
(`yerk location day`, a nightly script job). The fixes file and the places table are only read, and nothing goes to the
network: a fix is named by the nearest row of the places table whose radius holds it, the way the watcher that writes the
ever said or filed."""
from __future__ import annotations

import json
import math
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from . import memory, words
from .config import write_private
from .jobs import DURATION, UNITS_S

LABEL, ROLE = "location", "place"     # the day-log line's room label and author: filed for the owner's rooms by no room
MOVING_MPS, MOVED_M = 2.0, 150.0      # moving: faster than 2 m/s, or 150 m from the fix before (the watcher's rule)
GAP = timedelta(minutes=30)           # a stay at one place ends at a gap this long between two of its fixes
TAIL = 16384                          # bytes read from the end of the fixes file for the newest two


@dataclass(frozen=True)
class Location:
    file: Path                        # one JSON fix a line: ts_iso, lat, lon, speed_mps (null when the source has none)
    places: Path | None               # {"places": [{label, lat, lon, radius_m, todo}]}, the watcher's own table
    stale: timedelta


@dataclass(frozen=True)
class Fix:
    at: datetime
    lat: float
    lon: float
    speed: float | None


def settings(conf: dict[str, Any] | None, private: Path) -> Location | None:
    """yerkd.toml [location]: `file`, `places` (each relative to the private folder) and `stale_after` (default 3h); None
    with no table or no `file`, and then no room gets a place part."""
    if not conf or not conf.get("file"):
        return None
    text = str(conf.get("stale_after", "3h"))
    if not (found := DURATION.findall(text)) or DURATION.sub("", text).strip():
        raise SystemExit(f"yerkd.toml [location] stale_after {text!r} is not a duration (write it like 3h or 90m)")
    at = {k: (private / Path(str(conf[k])).expanduser()).resolve() for k in ("file", "places") if conf.get(k)}
    return Location(at["file"], at.get("places"), timedelta(seconds=sum(int(n) * UNITS_S[u[0].lower()] for n, u in found)))


def fixes(path: Path, tail: int = 0) -> list[Fix]:
    """The file's fixes in time order, only its last `tail` bytes' worth with `tail`; a line that is no fix is skipped."""
    with path.open("rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - tail) if tail else 0)
        if tail and size > tail:
            f.readline()                  # the line the seek cut
        rows = f.read().decode("utf-8", "replace").splitlines()
    out = []
    for x in rows:
        with suppress(ValueError, KeyError, TypeError, AttributeError):
            d = json.loads(x)
            out.append(Fix(datetime.fromisoformat(str(d["ts_iso"])).astimezone(timezone.utc), float(d["lat"]), float(d["lon"]),
                           None if d.get("speed_mps") is None else float(d["speed_mps"])))
    return sorted(out, key=lambda f: f.at)


def places(path: Path | None) -> list[tuple[str, float, float, float]]:
    """(label, lat, lon, radius in metres) for each located row of the table; a `todo` row or one with no coordinate never
    matches, as in the watcher; no table or an unreadable one, none (it can under-report, never mislabel)."""
    try:
        doc = json.loads(path.read_text()) if path else []
        rows = doc.get("places", []) if isinstance(doc, dict) else doc
        return [(str(p.get("label") or p.get("key") or ""), float(p["lat"]), float(p["lon"]), float(p.get("radius_m") or 150))
                for p in rows if isinstance(p, dict) and not p.get("todo") and p.get("lat") is not None and p.get("lon") is not None]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def metres(a: Fix, lat: float, lon: float) -> float:
    p1, p2 = math.radians(a.lat), math.radians(lat)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon - a.lon) / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))


def name(f: Fix, table: list[tuple[str, float, float, float]]) -> str:
    """The nearest place whose radius holds the fix, else ""."""
    hits = [(d, label) for label, lat, lon, r in table if (d := metres(f, lat, lon)) <= r]
    return min(hits)[1] if hits else ""


def part(loc: Location | None, room: dict[str, Any], now: datetime) -> str:
    """An owner room's `place` line: the newest fix's place, how long ago, moving or not; "" in any other room, with no
    [location] table or no readable file, past `stale_after`, or when the newest fix is at no named place."""
    if loc is None or str(room.get("privacy", "owner")) != "owner":
        return ""
    try:
        got = fixes(loc.file, TAIL)
    except OSError:
        return ""
    if not got or now - got[-1].at > loc.stale or not (here := name(got[-1], places(loc.places))):
        return ""
    f, before = got[-1], got[-2] if len(got) > 1 else None
    moving = (f.speed or 0) > MOVING_MPS or (before is not None and metres(f, before.lat, before.lon) > MOVED_M)
    return words.PLACE.format(place=here, ago=words.ago((now - f.at).total_seconds()), state="moving" if moving else "stationary")


def stays(got: list[Fix], table: list[tuple[str, float, float, float]], on: date, tz: tzinfo) -> list[tuple[datetime, datetime, str]]:
    """The day's stays in time order, each a run of fixes at one named place, its first and last: a run ends at another
    place or at a GAP between two of its fixes; a fix at no named place (the road, a place not in the table) is none."""
    out: list[tuple[datetime, datetime, str]] = []
    for f in got:
        if f.at.astimezone(tz).date() != on or not (here := name(f, table)):
            continue
        if out and out[-1][2] == here and f.at - out[-1][1] < GAP:
            out[-1] = (out[-1][0], f.at, here)
        else:
            out.append((f.at, f.at, here))
    return out


def day(loc: Location, daily: Path, tz: tzinfo, on: date, dry: bool = False) -> str:
    """`yerk location day`: the day's stays as one line of its day log, timed at the last fix it holds, class private (the
    owner's rooms read it, no person's room does); an earlier run's line (its id is the day's) is replaced where it stands,
    so no other line moves and every cite into the file holds, else the line goes at the end. "" when the day has no stay."""
    if not (got := stays(fixes(loc.file), places(loc.places), on, tz)):
        return ""
    clock, mid = [(f"{a.astimezone(tz):%H:%M}", f"{b.astimezone(tz):%H:%M}", n) for a, b, n in got], f"{LABEL}-{on}"
    line = memory.format_line(got[-1][1].astimezone(tz), LABEL, LABEL, ROLE, "", words.PLACES_DAY.format(
        stays="; ".join(f"{a} to {b} {n}" if a != b else f"{a} {n}" for a, b, n in clock)), "private", mid)
    if dry:
        return line
    with memory.daily_lock(daily):
        path = daily / f"{on}.md"
        rows = path.read_text(errors="replace").splitlines() if path.is_file() else [f"# {on}", ""]
        at = next((i for i, x in enumerate(rows) if (ln := memory.parse_line(x)) and ln.mid == mid), len(rows))
        write_private(path, "\n".join(rows[:at] + [line] + rows[at + 1:]) + "\n")
    return line
