"""Reads each account's usage meters live, for `yerk status` and the room's /status: Claude Code's own `/usage`, run as
`claude -p /usage` on the account's config folder, prints the session (5-hour) meter, the week's and the week's for a
model, each with its reset, and makes no model call. Claude Code reads its own login, the config folder's: a one-year
token cannot read the meters (Claude Code then prints the session's cost instead), so the token is left out of this
call. Yerk AI reads only what Claude Code prints.

A reading is kept FRESH seconds in <state>/usage.json, so a card asked twice in a minute starts no second Claude Code,
and at most AT_ONCE run at a time (each holds about 350 MB for about 5 s, inside yerkd's unit). Anything that fails is
{"error": why}, kept as long (in memory where the file holds a good reading, which it keeps), and the card shows the
newest reading it holds instead: the kept one or the stream's (meters.py), with its age. Between cards
the daemon reads again, one account at a time and through nice, each account whose kept reading is EVERY old (keep),
so a card that asks nothing shows that reading where it is newer than the stream's, else the stream's with the model's
own week, which no stream reads, from the last reading whatever its age (status.reading)."""
from __future__ import annotations

import contextlib
import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .accounts import TOKEN_ENV, Account, account_env
from .config import write_private
from .engine import claude_bin

FILE = "usage.json"
FRESH = 60.0            # a reading younger than this is used again
TIMEOUT = 15.0          # one call, Claude Code's own start included (about 5 s on a busy box)
AT_ONCE = 3
EVERY = 900.0           # a kept reading this old is read again in the background (keep), one account at a time
ARGV = ["-p", "/usage", "--output-format", "json", "--no-session-persistence", "--setting-sources", "",
        "--strict-mcp-config", "--tools", ""]
# Claude Code 2.1.304: "Current session: 30% used · resets Oct 23, 5:10pm (America/Toronto)", then
# "Current week (all models): ..." and "Current week (Fable): ..."
LINE = re.compile(r"^\s*Current (session|week \(([^)]*)\))\s*:\s*(\d+(?:\.\d+)?)%\s*used(?:\s*·\s*resets\s+(.+?))?\s*$")
WHEN = re.compile(r"^(?:([A-Za-z]{3}) (\d{1,2}), )?(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([^)]+)\))?$", re.I)
_lock = threading.Lock()
_failed: dict[tuple[str, str], dict[str, Any]] = {}   # (state folder, account): a failed ask over a good kept reading, under _lock


def reset_at(text: str, now: float) -> float | None:
    """'Oct 23, 5:10pm (America/Toronto)', or '5:10pm' alone, as epoch seconds: that time next from `now`. Every reset
    seen sits on a 10-minute mark, and Claude Code drops the seconds, so one a moment before its mark prints a minute
    early ('2:59am' at 13:45 read '3am'): a minute ending in 9 is read as the next."""
    m = WHEN.match(text.strip())
    if not m:
        return None
    mon, day, hour, minute, half, zone = m.groups()
    try:
        tz = ZoneInfo(zone) if zone else None
    except (KeyError, ValueError):                       # an unknown zone: local time
        tz = None
    base = datetime.fromtimestamp(now, tz)
    h = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    try:
        if mon:
            t = datetime.strptime(f"{base.year} {mon} {day}", "%Y %b %d").replace(hour=h, minute=int(minute or 0),
                                                                                   tzinfo=base.tzinfo)
            if t.timestamp() < now - 86400:              # read in December, it resets in January
                t = t.replace(year=base.year + 1)
        else:
            t = base.replace(hour=h, minute=int(minute or 0), second=0, microsecond=0)
            t += timedelta(days=1) if t.timestamp() < now - 60 else timedelta(0)
    except ValueError:
        return None
    return t.timestamp() + (60 if int(minute or 0) % 10 == 9 else 0)


def parse(text: str, now: float) -> dict[str, Any]:
    """The meters in /usage's text: {five_hour, seven_day: {pct, resets}, model: {name, pct, resets}}, each when
    printed (the first model week only); {} when it printed none."""
    out: dict[str, Any] = {}
    for raw in text.splitlines():
        if not (m := LINE.match(raw)):
            continue
        which, scope, pct, when = m.groups()
        r = {"pct": float(pct), "resets": reset_at(when, now) if when else None}
        if which == "session":
            out.setdefault("five_hour", r)
        elif scope.strip().lower() == "all models":
            out.setdefault("seven_day", r)
        else:
            out.setdefault("model", {"name": scope.strip(), **r})
    return out


def read(account: Account, cwd: Path, timeout: float = TIMEOUT, nice: bool = False) -> dict[str, Any]:
    """One account's meters from `claude -p /usage` on its config folder's own login, or {"error": why}; with `nice`
    (the background read, keep) through `nice -n 19`, so it never competes with a room's turn."""
    if not account.config_dir.is_dir():
        return {"error": "no config folder"}                 # Claude Code would make one; it has no login to read
    env = {k: v for k, v in account_env(account).items() if k != TOKEN_ENV}
    now, argv = time.time(), ["nice", "-n", "19", claude_bin(), *ARGV] if nice else [claude_bin(), *ARGV]
    try:
        p = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        meters = parse(str(json.loads(p.stdout or "{}").get("result") or ""), now)
    except subprocess.TimeoutExpired:
        return {"error": f"no answer in {timeout:g} s"}
    except (OSError, ValueError, AttributeError) as e:
        return {"error": f"unreadable ({type(e).__name__})"}
    if "five_hour" not in meters and "seven_day" not in meters:
        return {"error": "no meters in what Claude Code printed"}
    return meters


def readings(accounts: list[Account], state: Path, refresh: bool = True, now: float | None = None,
             read_one: Callable[[Account], dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Each account's live reading ({"error": why} when its call failed), younger than FRESH: from the file, else
    (`refresh`) asked again, AT_ONCE at a time. An account with neither is left out. A failed call never replaces a good
    reading in the file: it is held in memory (_failed) instead, where it counts as the try while it is the newer."""
    now = time.time() if now is None else now
    # two cards at once: the second reads the first's answers. A read that does not ask (the deploy's proof, `--json`)
    # never waits on one that does: the file is only ever replaced whole (write_private)
    with _lock if refresh else contextlib.nullcontext():
        kept = last(state)
        seen = {**kept, **{a.name: f for a in accounts if refresh and (f := _failed.get((str(state), a.name)))
                           and float(f["at"]) > float(kept.get(a.name, {}).get("at") or 0)}}
        stale = [a for a in accounts if not 0 <= now - float(seen.get(a.name, {}).get("at") or 0) < FRESH]
        if refresh and stale:
            ask = read_one or (lambda a: read(a, state))
            with ThreadPoolExecutor(max_workers=min(AT_ONCE, len(stale))) as pool:
                for a, r in zip(stale, pool.map(ask, stale)):
                    seen[a.name] = {**r, "at": now, "source": "live"}
                    if "error" in r and a.name in kept and "error" not in kept[a.name]:
                        _failed[(str(state), a.name)] = seen[a.name]     # the file keeps its good reading
                    else:
                        kept[a.name] = seen[a.name]
                        _failed.pop((str(state), a.name), None)
            try:
                write_private(state / FILE, json.dumps(kept))
            except OSError:
                pass                                     # the card still shows them; the next one asks again
    return {a.name: seen[a.name] for a in accounts if 0 <= now - float(seen.get(a.name, {}).get("at") or -FRESH) < FRESH}


def keep(accounts: list[Account], state: Path, tried: dict[str, float], clock: Callable[[], float] = time.time,
         read_one: Callable[[Account], dict[str, Any]] | None = None) -> None:
    """One step of the daemon's keeper (Yerkd.keep_meters): of the accounts whose kept reading is EVERY old (none kept:
    the oldest) and not tried here within EVERY (`tried`, the keeper's own, in memory), the oldest is read once through
    nice and filed as `readings` files one, unless a newer one came meanwhile (a card's live ask) that is good or this
    read failed too: a good reading replaces an error whatever its time. A failed read never replaces a good reading: it
    only counts as the try. One account a step, so never two background reads at once."""
    now = clock()
    kept = last(state)
    due = [a for a in accounts if not 0 <= now - float(kept.get(a.name, {}).get("at") or 0) < EVERY
           and not 0 <= now - tried.get(a.name, -EVERY) < EVERY]
    if not due:
        return
    a = min(due, key=lambda a: float(kept.get(a.name, {}).get("at") or 0))
    tried[a.name] = now
    r = (read_one or (lambda a: read(a, state, nice=True)))(a)
    ended = clock()
    with _lock:                                          # a card's live ask files under it too: its newer reading stays
        kept = last(state)
        had = kept.get(a.name)
        if had and ("error" in r or "error" not in had) and (float(had.get("at") or 0) > now or "error" in r and "error" not in had):
            return
        kept[a.name] = {**r, "at": ended, "source": "live"}
        write_private(state / FILE, json.dumps(kept))


def last(state: Path) -> dict[str, dict[str, Any]]:
    """Each account's last live reading in <state>/usage.json whatever its age, the file read whole, asking nothing."""
    try:
        return {k: v for k, v in json.loads((state / FILE).read_text()).items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}
