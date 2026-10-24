"""What is running now (rooms mid-turn, workers, jobs due, account usage, memory and CPU), for `yerk status` and
any program that asks. The running yerkd answers POST /status (DESIGN 9, L195, L206), local only like /hook and
flight and since when, the workers and their caps, the jobs due within the hour (from systemd), each
within the hour (from systemd), each account's meters with reset times (usage.py live, else meters.py with the
and CPU of the daemon and of each open room's process tree (from /proc), and today's stalls
(beat.py). With no daemon answering, `yerk status` collects the same from the files, rooms aside.

`card` renders it at phone width, state first, the colour only where it means something (an
account at or near its limit, a blocked room, a stall today, a daemon down), else a dash."""
from __future__ import annotations

import calendar
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import __version__, config, jobs, usage, words, workers
from .accounts import Account, Cooldowns
from .ledger import Ledger
from .meters import Meters

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
            units: Callable[[], set[str]] | None = None, timers: Any = None, live: bool = False,
            read_one: Callable[[Account], dict[str, Any]] | None = None) -> dict[str, Any]:
    """The /status answer. `rooms`: the daemon's live rooms (None: collected by `yerk status` with no
    daemon answering, so nothing about rooms or units). `live`: the card asks each account's meters now
    (usage.py; a reading under a minute old is used again), else only a reading still that young is used;
    an account with neither shows the stream's reading."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"at": now, "running": rooms is not None, "rooms": [], "units": []}
    if rooms is not None:
        out["units"].append(use("yerkd", [r for r in [stat(os.getpid(), proc)] if r]))   # its own, not its children
    for name, room in (rooms or {}).items():
        with room.lock:
            msgs, waiting, busy, rp = [*room.inflight, *room.pending, *room.queue], len(room.queue), room.busy, room.rp
        out["rooms"].append({"room": name, "busy": busy, "waiting": waiting, "watch": room.conf.get("watch"),
                             "since": min((m.received for m in msgs), default=None) if busy else None,
                             "account": config.pinned(state, name, [a.name for a in accounts])})   # the one it is put on
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
    meters, fresh = Meters(state / "meters.json"), usage.readings(accounts, state, live, now, read_one)
    out["accounts"] = [{"name": a.name, "meter": reading(meters.reading(a), fresh.get(a.name)),
                        "rests_until": (cooldowns.show(a.name, now) or [{}])[0].get("until"),
                        "bill": next_bill(a.bill_day, now)} for a in accounts]
    out["live"] = live
    out["stalls"] = ledger.stalls(midnight(now))
    return out


def reading(stream: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any]:
    """The live reading when there is one, else the stream's, with why the live one failed (`--json`)."""
    if live and "error" not in live:
        return live
    return {**stream, **({"live_error": live["error"]} if live else {})}


def next_bill(day: int | None, now: float) -> float | None:
    """The next bill on `day` of the month, today included, at local midnight; a shorter month bills on its last day."""
    if not day:
        return None
    t = datetime.fromtimestamp(now)
    y, m = t.year, t.month
    for _ in range(2):
        d = min(day, calendar.monthrange(y, m)[1])
        if (y, m, d) >= (t.year, t.month, t.day):
            return datetime(y, m, d).timestamp()
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return None


def document(private: Path, state: Path, s: dict[str, Any], ledger: Ledger, now: float | None = None) -> dict[str, Any]:
    """`yerk status --json`: one document a monitoring tool reads. The /status answer (rooms, accounts, workers, jobs,
    units, stalls, commit, connected, rebuilding) plus the version, each room's last answered turn, the turns that failed
    and the person's messages left unanswered in the last day, the index's state (yerk-index's own status file and its
    age) and the newest backup's age when `[backup] path` names a folder."""
    from . import memory
    now = time.time() if now is None else now
    conf = config.load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    ok, failed = ledger.turn_summary(now - 86400.0)
    out = {**s, "version": __version__, "last_turn": ok, "failed_turns": failed,
           "dead_messages": len(ledger.dead_turns(now - 86400.0, now - BLOCKED))}
    try:
        index = memory.index_status(memory.settings(private, state, dict(conf.get("memory", {}))))
        out["index"] = {**index, "age": round(now - float(index.get("at") or 0), 1)} if index else None
    except (OSError, ValueError, KeyError):
        out["index"] = None
    folder = Path(str((conf.get("backup") or {}).get("path") or "")).expanduser()
    newest = max((p.stat().st_mtime for p in folder.glob("yerk-*") if p.is_file()), default=None) if folder.is_dir() else None
    out["backup_age"] = None if newest is None else round(now - newest, 1)
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


def ask(state: Path, timeout: float = 10.0, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The running yerkd's /status under the install's secret (jobs.post_control); None when none answers. A live
    card's meters can take two rounds of usage.TIMEOUT, so it waits that much longer."""
    if (payload or {}).get("live"):
        timeout += 2 * usage.TIMEOUT
    try:
        return jobs.post_control(state, payload or {}, timeout, path="status")
    except (OSError, ValueError, KeyError, TypeError):
        return None


def stall_line(stalls: list[dict[str, Any]], day: str, since: str = "") -> str:
    if not stalls:
        return words.line(f"{words.PLAIN} {words.NO_STALLS.format(day=day)}{since}")
    took = words.secs(max(float(x["seconds"]) for x in stalls))
    return words.line(f"{words.NOT_DONE} {words.STALLS.format(day=day, n=len(stalls), took=took)}")


def account_lines(accounts: list[dict[str, Any]], now: float, live: bool = False) -> list[str]:
    """Each account in two lines, the box card's shape: `- ① 5h 7% · wk 1% · Fable 2%`, then `- ↻ 5h 6:10p ·
    wk Sun 3a · 💳 Oct 25`. Red first when it rests (its second line says until when) or a 5-hour or weekly window
    is full, orange when the model's week is full or any window is at 90% or more; a warning sign on each number at
    words.HOT or more. A reading that is not live says how old it is when the live one was asked for, or once it is
    an hour old. With two accounts or more, the soonest weekly reset wears the green mark on its tag, and two lines
    say the next two resets of each window, and one the account the next turn tries first."""
    W = words

    def ahead(w: str) -> dict[str, float]:
        return {a["name"]: float(r) for a in accounts if (r := (a["meter"].get(w) or {}).get("resets")) and float(r) > now}
    week, five, out = ahead("seven_day"), ahead("five_hour"), []
    soonest = min(week, key=lambda n: (week[n], n)) if len(accounts) > 1 and week else None
    for a in accounts:
        m, rest, tag = a["meter"], a.get("rests_until"), W.tag(a["name"])
        head = (W.DONE if a["name"] == soonest else "") + tag
        wins = [(label, m[w]) for w, label in (("five_hour", "5h"), ("seven_day", "wk"), ("model", "")) if w in m]
        pct = [round(float(x["pct"])) for _, x in wins]
        full = max((p for (label, _), p in zip(wins, pct) if label), default=0) >= 100
        mark = W.NOT_DONE if rest or full else W.WORKING if max(pct, default=0) >= 90 else W.PLAIN
        if wins:                                                  # a long name is cut, never a meter
            out.append(W.named(f"{mark} {head[:len(head) - len(tag)]}{{name}} {{meters}}", tag, meters=" · ".join(
                f"{label or x['name']} {W.WARN if p >= W.HOT else ''}{p}%" for (label, x), p in zip(wins, pct))))
        else:
            out.append(W.named(f"{mark} {W.NO_READING}", head))
        when = [f"{label or x['name']} {W.short_when(float(x['resets']), now)}" for label, x in wins
                if x.get("resets") and float(x["resets"]) > now and (label or abs(float(x["resets"]) - week.get(a["name"], 0)) > 60)]
        bill = [W.BILL.format(day=W.short_day(a["bill"]))] if a.get("bill") else []
        parts = [W.BACK_AT.format(until=W.short_when(rest, now))] if rest else when
        text = W.RESETS.format(parts=" · ".join(parts + bill))
        if parts[2:] and W.width(f"{W.PLAIN} {text}") > W.PHONE:      # the model's own reset only where it fits
            text = W.RESETS.format(parts=" · ".join(parts[:2] + bill))
        if parts or bill:
            out.append(W.line(f"{W.PLAIN} {text}"))
        if m.get("source") != "live" and "at" in m and (live or now - m["at"] >= OLD):
            out.append(W.named(f"{W.PLAIN} {W.OLD_READING}", tag, ago=W.ago(now - m["at"])))
    for label, due in (("wk", week), ("5h", five)):
        seq = sorted((r, n) for n, r in due.items())[:2] if len(accounts) > 1 else []
        if seq:
            first = (W.DONE if label == "wk" else "") + f"{W.tag(seq[0][1])} {W.short_when(seq[0][0], now)}"
            then = W.THEN.format(tag=W.tag(seq[1][1]), when=W.short_when(seq[1][0], now)) if seq[1:] else ""
            out.append(W.line(f"{W.PLAIN} {W.NEXT.format(label=label, first=first)}{then}"))
    free = next((a["name"] for a in accounts if not a.get("rests_until")), None)     # accounts.pick, no room's own
    if len(accounts) > 1 and free:
        out.append(W.line(f"{W.PLAIN} {W.FIRST_TRY.format(tag=W.tag(free))}"))
    return out


def card(s: dict[str, Any], now: float | None = None) -> list[str]:
    """`yerk status`: every line state first and at most words.PHONE columns (STYLE.md)."""
    now = float(s["at"]) if now is None else now
    W = words

    def row(mark: str, text: str) -> str:
        return W.line(f"{mark} {text}")
    head = W.RUNNING_ROOMS.format(rooms=", ".join(r["room"] for r in s["rooms"]) or W.ROOMS_N.format(n=0))
    if W.width(f"{W.PLAIN} {head}") > W.PHONE:              # names that do not fit: how many
        head = W.RUNNING_ROOMS.format(rooms=W.ROOM_1 if len(s["rooms"]) == 1 else W.ROOMS_N.format(n=len(s["rooms"])))
    out = [row(W.PLAIN, head) if s["running"] else row(W.NOT_DONE, W.NOT_ANSWERING if s.get("alive") else W.NOT_RUNNING)]
    out += account_lines(s["accounts"], now, bool(s.get("live")))
    for r in s["rooms"]:
        if r["since"] is not None:
            took = now - r["since"]
            out.append(W.named(f"{W.NOT_DONE if took > BLOCKED else W.PLAIN} {W.TURN_FOR}"
                               + (W.QUEUED.format(n=r["waiting"]) if r["waiting"] else ""), r["room"], took=W.span(took)))
    out += [W.named(f"{W.PLAIN} {W.WATCHES}", r["room"], channel=str(r["watch"]).partition(":")[2]) for r in s["rooms"] if r.get("watch")]
    pins: dict[str, list[str]] = {}
    for r in s["rooms"]:
        if r.get("account"):
            pins.setdefault(r["account"], []).append(r["room"])
    for acct, names in pins.items():                    # one room: its name; more: their names, else how many
        if len(names) == 1:
            out.append(W.named(f"{W.PLAIN} {W.PINNED}", names[0], account=W.tag(acct)))
            continue
        text = W.ROOMS_ON.format(account=W.tag(acct), rooms=", ".join(names))
        out.append(row(W.PLAIN, text if W.width(f"{W.PLAIN} {text}") <= W.PHONE
                       else W.ROOMS_ON.format(account=W.tag(acct), rooms=W.ROOMS_N.format(n=len(names)))))
    out.append(row(W.PLAIN, W.VERSION_AT.format(version=s.get("version") or __version__,   # what runs: an upgrade's receipt
                                                 commit=str(s.get("commit") or "")[:7] or "?")))
    if s.get("addons") is not None:                     # `yerk status` names the installed add-ons (addons.py)
        out.append(row(W.PLAIN, W.ADDONS.format(names=", ".join(s["addons"]) or W.NO_ADDONS)))
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
    out += [W.named(f"{W.PLAIN} {W.JOB_DUE}", j["name"], left=W.span(j["next"] - now)) for j in s["jobs"] or []]
    if s["jobs"] == []:
        out.append(row(W.PLAIN, W.NO_JOB_DUE))
    out += [W.named(f"{W.PLAIN} {W.UNIT_USE}", u["name"], mb=round(u["rss_mb"]), cpu=u["cpu_pct"]) for u in s["units"] if not u.get("gone")]
    return out + [stall_line(s["stalls"], W.TODAY)]
