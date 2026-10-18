"""What is running now (rooms mid-turn, workers, jobs due, account usage, memory and CPU), for `yerk status` and
any program that asks. The running yerkd answers POST /status (DESIGN 9, L195, L206), local only like /hook and
flight and since when, the workers and their caps, the jobs due within the hour (from systemd), each
account's 5-hour and weekly meters with reset times and the reading's age (meters.py), the memory
and CPU of the daemon and of each open room's process tree (from /proc), and today's stalls
(beat.py). With no daemon answering, `yerk status` collects the same from the files, rooms aside.

`card` renders it at phone width, state first, the colour only where it means something (an
account at or near its limit, a blocked room, a stall today, a daemon down), else a dash."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import jobs, words, workers
from .accounts import Account, Cooldowns
from .ledger import Ledger
from .meters import Meters, meter_short

PROC = Path("/proc")
TICK, PAGE = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
SOON = 3600.0           # a job due within the hour is on the card
BLOCKED = 600.0         # a turn in flight longer than this: the room is blocked
OLD = 3600.0            # a meter reading older than this gets its age on a line of its own


def stat(pid: int, proc: Path = PROC, base: Path | None = None) -> dict[str, Any] | None:
    """One process (one thread, with base = <pid>/task) from /proc: name, state, parent, CPU seconds,
    age, resident MB and wchan; None when it is gone. An unreadable wchan says so, never "-"."""
    d = (base or proc) / str(pid)
    try:
        name, _, rest = (d / "stat").read_text().partition(" (")[2].rpartition(")")
        f = rest.split()
        rss = int((d / "statm").read_text().split()[1]) * PAGE / 2**20
        up = float((proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    try:
        wchan = (d / "wchan").read_text().strip() or "-"
    except OSError as e:
        wchan = f"unreadable ({e.strerror})"          # a permission result, not "not waiting"
    return {"pid": pid, "name": name, "state": f[0], "ppid": int(f[1]), "cpu_s": (int(f[11]) + int(f[12])) / TICK,
            "age_s": up - int(f[19]) / TICK, "rss_mb": round(rss, 1), "wchan": wchan}


def tree(pid: int, proc: Path = PROC) -> list[dict[str, Any]]:
    """The process and every process under it (a room's claude, its MCP servers, its commands)."""
    rows = [r for p in proc.iterdir() if p.name.isdigit() and (r := stat(int(p.name), proc))]
    out, todo = [], [r for r in rows if r["pid"] == pid]
    while todo:
        out.append(r := todo.pop())
        todo += [c for c in rows if c["ppid"] == r["pid"]]
    return out


def use(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One unit's memory (resident MB over its processes) and CPU (the share of one core they used
    since the first of them started)."""
    if not rows:
        return {"name": name, "gone": True}
    cpu = sum(r["cpu_s"] for r in rows)
    return {"name": name, "pid": rows[0]["pid"], "procs": len(rows), "rss_mb": round(sum(r["rss_mb"] for r in rows), 1),
            "cpu_s": round(cpu, 2), "cpu_pct": round(100 * cpu / max(rows[0]["age_s"], 1.0), 2)}


def collect(private: Path, state: Path, accounts: list[Account], cooldowns: Cooldowns, ledger: Ledger,
            rooms: dict[str, Any] | None = None, now: float | None = None, proc: Path = PROC,
            units: Callable[[], set[str]] | None = None, timers: Any = None) -> dict[str, Any]:
    """The /status answer. `rooms`: the daemon's live rooms (None: collected by `yerk status` with no
    daemon answering, so nothing about rooms or units)."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"at": now, "running": rooms is not None, "rooms": [], "units": []}
    if rooms is not None:
        out["units"].append(use("yerkd", [r for r in [stat(os.getpid(), proc)] if r]))   # its own, not its children
    for name, room in (rooms or {}).items():
        with room.lock:
            msgs, waiting, busy, rp = [*room.inflight, *room.pending, *room.queue], len(room.queue), room.busy, room.rp
        out["rooms"].append({"room": name, "busy": busy, "waiting": waiting,
                             "since": min((m.received for m in msgs), default=None) if busy else None})
        if rp is not None and rp.proc is not None and rp.alive():
            out["units"].append(use(name, tree(rp.proc.pid, proc)))
    try:
        running: set[str] | None = (units or workers.Systemd().running)()
    except (OSError, subprocess.SubprocessError):
        running = None                                 # no unit list: the ledger alone, unchecked
    out["workers"] = [{**{k: w[k] for k in ("id", "room", "account", "started", "cap", "waiting", "queued")},
                       "gone": running is not None and not w["waiting"] and w["unit"] not in running}
                      for w in ledger.workers(running=True)]
    try:
        back = jobs.readback(jobs.load(private / jobs.FILE, now)[0], timers or jobs.Systemd(), now)
        out["jobs"] = sorted(({"name": n, "next": r["next"]} for n, r in back.items()
                              if r.get("next") and 0 <= r["next"] - now <= SOON), key=lambda j: j["next"])
    except (OSError, subprocess.SubprocessError, ValueError):
        out["jobs"] = None                             # the timer list could not be read
    meters = Meters(state / "meters.json")
    out["accounts"] = [{"name": a.name, "meter": meters.reading(a),
                        "rests_until": (cooldowns.show(a.name) or [{}])[0].get("until")} for a in accounts]
    out["stalls"] = ledger.stalls(midnight(now))
    return out


def midnight(now: float, days: int = 0) -> float:
    """The start of the local day `days` before the one `now` falls in."""
    return (datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)).timestamp()


def alive(state: Path) -> bool:
    try:
        os.kill(int(json.loads((state / "yerkd.json").read_text())["pid"]), 0)
        return True
    except (OSError, ValueError, KeyError):
        return False


def ask(state: Path, timeout: float = 10.0) -> dict[str, Any] | None:
    """The running yerkd's /status (its port is in <state>/yerkd.json); None when none answers."""
    try:
        port = json.loads((state / "yerkd.json").read_text())["port"]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/status", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return dict(json.loads(r.read()))
    except (OSError, ValueError, KeyError):
        return None


def stall_line(stalls: list[dict[str, Any]], day: str, since: str = "") -> str:
    if not stalls:
        return words.line(f"{words.PLAIN} {words.NO_STALLS.format(day=day)}{since}")
    took = words.secs(max(float(x["seconds"]) for x in stalls))
    return words.line(f"{words.NOT_DONE} {words.STALLS.format(day=day, n=len(stalls), took=took)}")


def card(s: dict[str, Any], now: float | None = None) -> list[str]:
    """`yerk status`: every line state first and at most words.PHONE columns (STYLE.md)."""
    now = float(s["at"]) if now is None else now
    W = words

    def row(mark: str, text: str) -> str:
        return W.line(f"{mark} {text}")
    head = W.RUNNING_ROOMS.format(rooms=", ".join(r["room"] for r in s["rooms"]) or W.ROOMS_N.format(n=0))
    if W.width(f"{W.PLAIN} {head}") > W.PHONE:              # names that do not fit: how many
        head = W.RUNNING_ROOMS.format(rooms=W.ROOMS_N.format(n=len(s["rooms"])))
    out = [row(W.PLAIN, head) if s["running"] else row(W.NOT_DONE, W.NOT_ANSWERING if s.get("alive") else W.NOT_RUNNING)]
    for a in s["accounts"]:
        out.append(meter_short(a["name"], a["meter"], a["rests_until"], now))
        if now - a["meter"].get("at", now) >= OLD:
            out.append(row(W.PLAIN, W.OLD_READING.format(name=a["name"], ago=W.ago(now - a["meter"]["at"]))))
    for r in s["rooms"]:
        if r["since"] is not None:
            took = now - r["since"]
            out.append(row(W.NOT_DONE if took > BLOCKED else W.PLAIN, W.TURN_FOR.format(room=r["room"], took=W.span(took))
                           + (W.QUEUED.format(n=r["waiting"]) if r["waiting"] else "")))
    nth: dict[str, int] = {}
    for w in sorted(s["workers"], key=lambda w: (bool(w["waiting"]), float(w["queued"] or 0))):
        if w["waiting"]:
            nth[w["account"]] = nth.get(w["account"], 0) + 1
            say = W.IN_LINE_SHORT.format(nth=W.nth(nth[w["account"]]))
        else:
            say = W.GONE if w["gone"] else W.of_cap(now - w["started"], w["cap"])
        out.append(row(W.NOT_DONE if w["gone"] else W.PLAIN, f"{W.WORKER.format(id=w['id'])}: {say}"))
    if not s["workers"]:
        out.append(row(W.PLAIN, W.NONE_RUNNING))
    if s["jobs"] is None:
        out.append(row(W.NOT_DONE, W.NO_TIMERS))
    out += [row(W.PLAIN, W.JOB_DUE.format(name=j["name"], left=W.span(j["next"] - now))) for j in s["jobs"] or []]
    if s["jobs"] == []:
        out.append(row(W.PLAIN, W.NO_JOB_DUE))
    out += [row(W.PLAIN, W.UNIT_USE.format(name=u["name"], mb=round(u["rss_mb"]), cpu=u["cpu_pct"]))
            for u in s["units"] if not u.get("gone")]
    return out + [stall_line(s["stalls"], W.TODAY)]
