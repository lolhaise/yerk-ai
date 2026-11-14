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

from . import __version__, backup, config, jobs, latency, usage, words, workers
from .accounts import Account, Cooldowns, pick
from .engine import FAST_MODELS
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


def fast_shown(state: Path, name: str, room: Any) -> bool:
    """/status says fast mode is on for a room only while its own /fast on stands, its model is Fable 5.2 and Claude Code
    has not reported it otherwise since (Room.fast_state): never "on" when Claude Code refused it."""
    model = str(config.override(state, name).get("model") or room.conf.get("model") or "")
    return config.fast_on(state, name) and model in FAST_MODELS and room.fast_state in ("", "on")


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
    conf = config.load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    listed = config.models(conf) if rooms else {}
    if rooms is not None:
        out["units"].append(use("yerkd", [r for r in [stat(os.getpid(), proc)] if r]))   # its own, not its children
    compacted = ledger.compactions(midnight(now)) if rooms else {}   # each room's today, the ledger's: a restart keeps them
    for name, room in (rooms or {}).items():
        with room.lock:
            msgs, waiting, busy, rp = [*room.inflight, *room.pending, *room.queue], len(room.queue), room.busy, room.rp
            held = (getattr(room, "context", 0), getattr(room, "window", 0), compacted.get(name, 0),
                    getattr(room, "fresh_next", None) is None)
        out["rooms"].append({"room": name, "busy": busy, "waiting": waiting, "watch": room.conf.get("watch"),
                             "since": min((m.received for m in msgs), default=None) if busy else None,
                             "account": config.pinned(state, name, [a.name for a in accounts]),   # the one it is put on
                             "fast": fast_shown(state, name, room), "head": head(state, name, room, rp, held, accounts, cooldowns, listed, now)})
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
    out["speed"] = latency.medians(ledger, midnight(now), now)    # today's median first line and turn
    out["backup"] = backup.newest(private, state)
    if (mode := str(dict(conf.get("texting") or {}).get("mode") or "off")) != "off":    # texting/link.py writes it as it runs
        try:
            out["texting"] = json.loads((state / "texting" / "status.json").read_text())
        except (OSError, ValueError):
            out["texting"] = {"mode": mode}
    if config.uncapped(config.budgets(conf)):           # the money gates lifted: the receipt, today and the week to it
        out["spend"] = spend(ledger, midnight(now), now, midnight(now, 6))
    return out


def head(state: Path, name: str, room: Any, rp: Any, held: tuple[int, int, int, bool], accounts: list[Account],
         cooldowns: Cooldowns, listed: dict[str, str], now: float) -> dict[str, Any]:
    """The room card's head (room_card) for room `name`, its session's numbers as collect read them under the room's lock
    (`held`: its tokens, its compaction window, its compactions today, and no new session waiting): the model its next reply
    launches with (its /model choice over rooms.toml, as config.room_settings reads them for spec_for, an alias through
    the install's list; "" when neither names one, since Claude Code's own default has no id Yerk knows), its thinking
    level ("" alike), the account its session is on (the live process's, else the one its next turn would try, as Room._account
    picks: the one it is put on unless that one rests), its tokens (None: no session yet, or a /new waiting), its window (0: left
    to the model) and its compactions today (Ledger.compactions, read once a card for every room)."""
    tokens, window, compactions, same = held
    settings = config.room_settings(dict(room.conf), state, name, listed)
    model = settings["model"][0]
    own = rp.account.name if rp is not None and hasattr(rp, "account") else ""
    account = own or (pick(accounts, cooldowns, now, config.pinned(state, name, [a.name for a in accounts]), model).name if accounts else "")
    return {"model": listed.get(model.lower(), model), "effort": settings["effort"][0], "account": account,
            "context": tokens if tokens and same else None, "window": int(window or 0), "compactions": compactions}


def spend(ledger: Ledger, since: float, until: float, week: float) -> dict[str, Any]:
    """{n, usd, week}: the paid calls booked from `since` (ledger.spent) and the dollars from `week`, both to `until`."""
    day, wk = ledger.spent(since, until), ledger.spent(week, until)
    return {"n": int(day.get("n") or 0), "usd": float(day.get("usd") or 0), "week": float(wk.get("usd") or 0)}


def spend_line(sp: dict[str, Any], day: str) -> str:
    """words.SPEND in a card's PHONE columns: less the call count while wider, then less "spend ", so both dollar figures stay."""
    for f in (words.SPEND, bare := words.SPEND.replace(" ({n} call{s})", ""), bare.removeprefix("spend ")):
        if words.width(f"{words.PLAIN} {(t := f.format(day=day, s='s' * (sp['n'] != 1), **sp))}") <= words.PHONE:
            break
    return t


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
    age) and the newest backup's age (backup.py's folder), null when there is none."""
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
    newest = backup.newest(private, state)
    out["backup_age"] = None if newest is None else round(now - newest["at"], 1)
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


def tank(accounts: list[dict[str, Any]], now: float, live: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """The numbers both cards print (account_lines, room_card): per account its tag, `green` when its week resets soonest
    (two accounts or more), its state mark, its meters in words (None: no reading yet), the resets its second line says
    (the model's own only when it is not the week's, within a minute), its bill, until when it rests, its weekly reset,
    and the age of a reading not live where that is said (None: not said); then each window's `next` line, in words."""
    W = words

    def ahead(w: str) -> dict[str, float]:
        return {a["name"]: float(r) for a in accounts if (r := (a["meter"].get(w) or {}).get("resets")) and float(r) > now}
    week, five, rows, nexts = ahead("seven_day"), ahead("five_hour"), [], []
    soonest = min(week, key=lambda n: (week[n], n)) if len(accounts) > 1 and week else None
    for a in accounts:
        m, rest = a["meter"], a.get("rests_until")
        wins = [(label, m[w]) for w, label in (("five_hour", "5h"), ("seven_day", "wk"), ("model", "")) if w in m]
        pct = [round(float(x["pct"])) for _, x in wins]
        full = max((p for (label, _), p in zip(wins, pct) if label), default=0) >= 100
        rows.append({"name": a["name"], "tag": W.tag(a["name"]), "green": a["name"] == soonest, "rest": rest, "week": week.get(a["name"]),
                     "mark": W.NOT_DONE if rest or full else W.WORKING if max(pct, default=0) >= 90 else W.PLAIN,
                     "meters": " · ".join(f"{label or x['name']} {W.WARN if p >= W.HOT else ''}{p}%"
                                          for (label, x), p in zip(wins, pct)) if wins else None,
                     "when": [f"{label or x['name']} {W.short_when(float(x['resets']), now)}" for label, x in wins if x.get("resets")
                              and float(x["resets"]) > now and (label or abs(float(x["resets"]) - week.get(a["name"], 0)) > 60)],
                     "bill": [W.BILL.format(day=W.short_day(a["bill"]))] if a.get("bill") else [],
                     "old": W.ago(now - m["at"]) if m.get("source") != "live" and "at" in m and (live or now - m["at"] >= OLD) else None})
    for label, due in (("wk", week), ("5h", five)):
        seq = sorted((r, n) for n, r in due.items())[:2] if len(accounts) > 1 else []
        if seq:
            first = (W.DONE if label == "wk" else "") + f"{W.tag(seq[0][1])} {W.short_when(seq[0][0], now)}"
            then = W.THEN.format(tag=W.tag(seq[1][1]), when=W.short_when(seq[1][0], now)) if seq[1:] else ""
            nexts.append(f"{W.NEXT.format(label=label, first=first)}{then}")
    return rows, nexts


def resets(parts: list[str], bill: list[str], lead: int = 0) -> str:
    """An account's second line (words.RESETS): its resets, then its bill; the model's own reset only where the line fits
    words.PHONE with `lead` columns before it."""
    text = words.RESETS.format(parts=" · ".join(parts + bill))
    if parts[2:] and words.width(text) + lead > words.PHONE:
        text = words.RESETS.format(parts=" · ".join(parts[:2] + bill))
    return text


def account_lines(accounts: list[dict[str, Any]], now: float, live: bool = False) -> list[str]:
    """Each account in two lines, the box card's shape: `- ① 5h 7% · wk 1% · Fable 2%`, then `- ↻ 5h 6:10p ·
    wk Sun 3a · 💳 Oct 25`. Red first when it rests (its second line says until when) or a 5-hour or weekly window
    is full, orange when the model's week is full or any window is at 90% or more; a warning sign on each number at
    words.HOT or more. A reading that is not live says how old it is when the live one was asked for, or once it is
    an hour old. With two accounts or more, the soonest weekly reset wears the green mark on its tag, and two lines
    say the next two resets of each window, and one the account the next turn tries first."""
    W, out = words, []
    rows, nexts = tank(accounts, now, live)
    for r in rows:
        head = (W.DONE if r["green"] else "") + r["tag"]
        if r["meters"] is not None:                               # a long name is cut, never a meter
            out.append(W.named(f"{r['mark']} {head[:len(head) - len(r['tag'])]}{{name}} {{meters}}", r["tag"], meters=r["meters"]))
        else:
            out.append(W.named(f"{r['mark']} {W.NO_READING}", head))
        parts = [W.BACK_AT.format(until=W.short_when(r["rest"], now))] if r["rest"] else r["when"]
        if parts or r["bill"]:
            out.append(W.line(f"{W.PLAIN} {resets(parts, r['bill'], W.width(W.PLAIN + ' '))}"))
        if r["old"] is not None:
            out.append(W.named(f"{W.PLAIN} {W.OLD_READING}", r["tag"], ago=r["old"]))
    out += [W.line(f"{W.PLAIN} {n}") for n in nexts]
    free = next((a["name"] for a in accounts if not a.get("rests_until")), None)     # accounts.pick, no room's own
    if len(accounts) > 1 and free:
        out.append(W.line(f"{W.PLAIN} {W.FIRST_TRY.format(tag=W.tag(free))}"))
    return out


def card(s: dict[str, Any], now: float | None = None) -> list[str]:
    """`yerk status`: every line state first and at most words.PHONE columns (STYLE.md), its parts in the order of words.STATUS_PARTS."""
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
    out += [W.named(f"{W.PLAIN} {W.WATCHES}", r["room"], channel=W.CHANNEL.format(name=r["watch_name"]) if r.get("watch_name")
                    else str(r["watch"]).partition(":")[2]) for r in s["rooms"] if r.get("watch")]      # its id only with no name known
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
    if fast := [r["room"] for r in s["rooms"] if r.get("fast")]:      # only while it is on: it bills usage credits
        out.append(row(W.PLAIN, W.FAST_ROOMS.format(rooms=", ".join(fast))))
    out.append(row(W.PLAIN, W.VERSION_AT.format(version=s.get("version") or __version__,   # what runs: an upgrade's receipt
                                                 commit=str(s.get("commit") or "")[:7] or "?")))
    if s.get("addons") is not None:                     # `yerk status` names the installed add-ons (addons.py)
        out.append(row(W.PLAIN, W.ADDONS.format(names=", ".join(s["addons"]) or W.NO_ADDONS)))
    if (t := s.get("texting")) is not None:
        out.append(row(W.PLAIN if t.get("up") else W.NOT_DONE, W.TEXTING_STATUS.format(
            mode=t.get("mode"), state=W.TEXTING_STATES[bool(t.get("up"))], since=W.short_clock(t["since"]) if t.get("since") else "?",
            last=W.TEXTING_IN.format(at=W.short_clock(t["last_in"])) if t.get("last_in") else W.TEXTING_NONE_IN)))
        n = {"held": int(t.get("held") or 0), "unseen": int(t.get("unseen") or 0)}       # texting/sender.py's
        out.append(row(W.NOT_DONE if n["unseen"] else W.PLAIN, W.TEXTING_OUT.format(at=W.short_clock(t["last_out"]), **n)
                       if t.get("last_out") else W.TEXTING_NONE_OUT.format(**n)))
    out += [row(W.NOT_DONE if gone else W.PLAIN, text) for gone, text in said(s["workers"], now)]
    if not s["workers"]:
        out.append(row(W.PLAIN, W.NONE_RUNNING))
    if s["jobs"] is None:
        out.append(row(W.NOT_DONE, W.NO_TIMERS))
    out += [W.named(f"{W.PLAIN} {W.JOB_DUE}", j["name"], left=W.span(j["next"] - now)) for j in s["jobs"] or []]
    if s["jobs"] == []:
        out.append(row(W.PLAIN, W.NO_JOB_DUE))
    out += [W.named(f"{W.PLAIN} {W.UNIT_USE}", u["name"], mb=round(u["rss_mb"]), cpu=u["cpu_pct"]) for u in s["units"] if not u.get("gone")]
    if (sp := s.get("speed")) and any(v is not None for v in sp.values()):     # none before the day's first message
        out.append(row(W.PLAIN, W.SPEED.format(day=W.TODAY, **{k: "-" if v is None else W.secs(v) for k, v in sp.items()})))
    if "spend" in s:                                    # an uncapped install's receipt line (collect)
        out.append(row(W.PLAIN, spend_line(s["spend"], W.TODAY)))
    if "backup" in s:
        out.append(backup_line(s["backup"], now))
    return out + [stall_line(s["stalls"], W.TODAY)]


def room_card(s: dict[str, Any], room: str, now: float | None = None) -> list[str]:
    """/status typed in `room`: the box card's shape, every line at most words.PHONE columns opening with its own icon,
    never a dash (STYLE.md). The room in three lines (the model its next reply launches with and its account; its session's
    tokens over its window and its compactions today; its thinking level, and fast mode while on), the tank (tank's numbers, two
    lines an account in number order, the room's own marked, a rest other than the account's weekly reset said), what runs
    (this room's turn first, at most four lines), today's speed and spend, then a red line only for trouble: a stall today,
    no backup or a stale one, a timer list not read."""
    now = float(s["at"]) if now is None else now
    W = words
    mine: dict[str, Any] = next((r for r in s["rooms"] if r["room"] == room), {})
    h = mine.get("head") or {}
    out = [W.line(W.ROOM_HEAD.format(parts=" · ".join(x for x in (h.get("model"), W.tag(h.get("account") or "")) if x)))]
    if held := h.get("context"):
        out.append(W.line(W.ROOM_HELD.format(held=W.tokens(held)) + (
            W.ROOM_OF.format(window=W.tokens(h["window"]), pct=round(100 * held / h["window"])) if h.get("window") else "") + (
            W.ROOM_COMPACTED.format(n=h["compactions"]) if h.get("compactions") is not None else "")))
    else:
        out.append(W.ROOM_NEW)
    fast = W.ROOM_FAST if mine.get("fast") else ""
    if h.get("effort"):
        out.append(W.line(W.ROOM_THINK.format(effort=h["effort"]) + fast))
    elif fast:
        out.append(W.ROOM_FAST_ONLY)
    out += ["", W.TANK]
    rows, nexts = tank(s["accounts"], now, bool(s.get("live")))
    for r in sorted(rows, key=lambda r: (W.number(r["name"]) or 10**6, r["name"])):     # by number, not the order tried
        lead = (W.ON_THIS if r["name"] == h.get("account") else "") + (W.DONE if r["green"] else "")
        asleep = W.ASLEEP.format(until=W.short_when(r["rest"], now)) if r["rest"] and abs(r["rest"] - (r["week"] or 0)) > 60 else ""
        first = f"{lead}{{name}} {{meters}}" if r["meters"] is not None else f"{lead}{W.NO_READING}"
        if W.width(" ".join(first.format(name=r["tag"], meters=r["meters"]).split()) + asleep) > W.PHONE:
            asleep = asleep.removeprefix(" · ")             # too wide with its rest: the rest on a line of its own
            out.append(W.named(first, r["tag"], meters=r["meters"]))
        else:
            out.append(W.named(first + asleep, r["tag"], meters=r["meters"]))
            asleep = ""
        if r["when"] or r["bill"]:
            out.append(W.line(resets(r["when"], r["bill"])))
        out += [W.line(asleep)] if asleep else []
        if r["old"] is not None:
            out.append(W.named(W.OLD_READING, r["tag"], ago=r["old"]))
    out += [W.line(n) for n in nexts] + [""]
    busy = []
    for r in sorted(s["rooms"], key=lambda r: r["room"] != room):          # this room's own turn first
        if r["since"] is not None:
            took = now - r["since"]
            busy.append(W.named(f"{W.NOT_DONE if took > BLOCKED else W.BUSY} {W.TURN_OF}" + (W.QUEUED.format(n=r["waiting"]) if r["waiting"] else ""),
                                W.HERE if r["room"] == room else r["room"], took=W.span(took)))
    busy += [W.line(f"{W.NOT_DONE if gone else W.BUSY} {text}") for gone, text in said(s["workers"], now)]
    out += (busy if len(busy) <= 4 else [*busy[:3], W.line(f"{W.BUSY} {W.AND_MORE.format(n=len(busy) - 3)}")]) or [W.ALL_QUIET]
    if (sp := s.get("speed")) and any(v is not None for v in sp.values()):     # none before the day's first message
        out.append(W.line(W.ROOM_SPEED.format(day=W.TODAY, **{k: "-" if v is None else W.secs(v) for k, v in sp.items()})))
    if "spend" in s:                                    # an uncapped install's (collect)
        out.append(W.line(W.ROOM_SPEND.format(day=W.TODAY, **s["spend"])))
    if s["stalls"]:
        out.append(stall_line(s["stalls"], W.TODAY))
    if "backup" in s and (b := backup_line(s["backup"], now)).startswith(W.NOT_DONE):
        out.append(b)
    if s.get("jobs", []) is None:
        out.append(W.line(f"{W.NOT_DONE} {W.NO_TIMERS}"))
    return out


def said(workers: list[dict[str, Any]], now: float) -> list[tuple[bool, str]]:
    """Each worker as both cards say it, the running first and the waiting in their place in line: (gone, its words)."""
    nth: dict[str, int] = {}
    out = []
    for w in sorted(workers, key=lambda w: (bool(w["waiting"]), float(w["queued"] or 0))):
        if w["waiting"]:
            nth[w["account"]] = nth.get(w["account"], 0) + 1
            say = words.IN_LINE_SHORT.format(nth=words.nth(nth[w["account"]]))
        else:
            say = words.GONE if w["gone"] else words.of_cap(now - w["started"], w["cap"])
        out.append((bool(w["gone"]), f"{words.WORKER.format(id=w['id'])}: {say}"))
    return out


def backup_line(b: dict[str, Any] | None, now: float) -> str:
    """The newest archive (backup.py), state first: red when there is none, or past backup.STALE."""
    text = words.BACKUP_AT.format(ago=words.span(now - b["at"]), size=words.size(int(b["bytes"]))) if b else words.NO_BACKUP
    return words.line(f"{words.NOT_DONE if not b or now - b['at'] > backup.STALE else words.PLAIN} {text}")
