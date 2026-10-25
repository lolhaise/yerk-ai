"""`yerk bench latency`: how fast the rooms answered and where the time went, per room and over all of them, for the last N days,
from what is already recorded. It measures (claim 18's judge) the message to the first visible line, the queue wait and the turn's
length (Ledger.waits); the prompt hook and the daemon's own hook handling summed per turn (hook_calls); the recall inside the prompt
hook (each room's assembled.jsonl, recall_ms); Yerk's own tool round trips (tool_times, mcp__yerk__*); the cache hit rate, cache
reads over all input tokens of each turn's result (the stream logs). One table, also written to <state>/bench/latency-<day>.json
beside the medians of the newest earlier file, so a speed lever is kept or cut on a diff. The ledger is opened ?mode=ro and the logs
only read: the one file written is the bench's own. `medians` is the day's pair the status card and the morning diff carry."""
from __future__ import annotations

import contextlib
import gzip
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any, Iterator

from .engine import CONTEXT
from .ledger import Ledger

DAY = 86400.0
WHAT = {"first": "first line, s", "wait": "queue wait, s", "turn": "turn, s", "hook": "prompt hook, ms",
        "recall": "recall in it, ms", "daemon": "hooks per turn, ms", "tools": "yerk tools, ms", "cache": "cache hit, %"}
LOG = re.compile(r"^(?P<name>.+?)(\.(?P<day>\d{4}-\d{2}-\d{2}))?\.jsonl(\.1)?(\.gz)?$")   # a log, live or rolled by retention
UNMEASURED = (
    "the prompt hook as Claude Code waits for it (the curl process and the round trip): hook_calls holds the daemon's own "
    "handling only and the stream logs carry no hook event; the record: the hook command's start time, sent as a header",
    "the first line as the person sees it (the card's first post): a progress post's outbound row names no message; the "
    "record: the message ids in outbound.inbound on a card's first post",
    "the daemon's own time from a message's arrival to its turn's start, apart from the queue: inbound.accepted is never "
    "written; the record: inbound.accepted when the message goes to the room's stdin")


def spread(xs: list[float]) -> dict[str, Any]:
    """n, the median, and p90 and p95 by the nearest rank; None where there are no values."""
    xs = sorted(xs)
    if not xs:
        return {"n": 0, "median": None, "p90": None, "p95": None}
    return {"n": len(xs), "median": round(statistics.median(xs), 3), **{f"p{p}": round(xs[-(-p * len(xs) // 100) - 1], 3) for p in (90, 95)}}


def medians(ledger: Ledger, since: float, until: float) -> dict[str, float | None]:
    """The window's median first visible line after a person's message and median turn, in seconds (None: no message)."""
    rows = ledger.waits(since, until)
    turns = {r["turn_id"]: r["turn"] for r in rows if r["turn"] is not None}          # a turn that took three messages once
    return {"first": spread([r["first"] for r in rows if r["first"] is not None])["median"], "turn": spread(list(turns.values()))["median"]}


def records(folder: Path, pattern: str, since: float, until: float, needle: str) -> Iterator[tuple[Path, dict[str, Any]]]:
    """(file, record) for each line holding `needle` in the logs `pattern` names under `folder`, plain or gzipped, timed inside
    the window; a file retention rolled on a day before the window's first is not opened."""
    first = time.strftime("%Y-%m-%d", time.localtime(since))
    for p in sorted(folder.glob(pattern)):
        if not (m := LOG.match(p.name)) or (m["day"] or first) < first:
            continue
        with gzip.open(p, "rt", errors="replace") if p.suffix == ".gz" else open(p, errors="replace") as f:
            for line in f:
                if needle in line:
                    with contextlib.suppress(ValueError, TypeError, AttributeError):
                        if since <= float((r := json.loads(line)).get("t") or 0) < until:
                            yield p, r


def bench(state: Path, days: float, now: float | None = None) -> dict[str, Any]:
    """{at, since, until, days, rows: [{what, room, n, median, p90, p95}], unmeasured}, the rooms after "all" in each
    measure; the cache row holds the window's hit rate as its median and n is its turns."""
    now = time.time() if now is None else now
    since, vals = now - days * DAY, {w: dict[str, list[float]]() for w in WHAT}

    def put(what: str, room: str, x: Any) -> None:
        if x is not None:
            vals[what].setdefault(room, []).append(float(x))
    led = Ledger.read_only(state / "ledger.sqlite")
    try:
        turns: dict[int, tuple[str, Any]] = {}
        for r in led.waits(since, now):
            put("first", r["room"], r["first"])
            put("wait", r["room"], r["wait"])
            turns[r["turn_id"]] = (r["room"], r["turn"])
        for room, x in turns.values():
            put("turn", room, x)
        per_turn: dict[tuple[str, str], float] = {}
        for r in led.timings("hook_calls", since, now):
            if r["event"] == "UserPromptSubmit":
                put("hook", r["room"], r["ms"])
            if r["turn"]:
                per_turn[(r["room"], r["turn"])] = per_turn.get((r["room"], r["turn"]), 0.0) + float(r["ms"] or 0)
        for (room, _), ms in per_turn.items():
            put("daemon", room, ms)
        for r in led.timings("tool_times", since, now):
            if str(r["tool"]).startswith("mcp__yerk__"):
                put("tools", r["room"], r["ms"])
    finally:
        led.close()
    for p, r in records(state / "rooms", "*/assembled*.jsonl*", since, now, '"recall_ms": '):
        if r.get("event") == "message":
            put("recall", p.parent.name, r.get("recall_ms"))
    cache: dict[str, list[int]] = {}
    for p, r in records(state / "logs", "*.jsonl*", since, now, '"type": "result"'):
        if (e := r.get("e") or {}).get("type") == "result" and (u := e.get("usage") or {}):
            c = cache.setdefault(LOG.match(p.name)["name"], [0, 0, 0])    # type: ignore[index]
            c[0], c[1], c[2] = c[0] + 1, c[1] + int(u.get("cache_read_input_tokens") or 0), c[2] + sum(int(u.get(k) or 0) for k in CONTEXT)
    rows = []
    for what in WHAT:
        if what == "cache":
            for room, (n, hit, of) in [("all", [sum(c[i] for c in cache.values()) for i in range(3)]), *sorted(cache.items())]:
                rows.append({"what": what, "room": room, "n": n, "median": round(100 * hit / of, 1) if of else None, "p90": None, "p95": None})
            continue
        rows.append({"what": what, "room": "all", **spread([x for xs in vals[what].values() for x in xs])})
        rows += [{"what": what, "room": room, **spread(xs)} for room, xs in sorted(vals[what].items())]
    return {"at": now, "since": since, "until": now, "days": days, "rows": rows, "unmeasured": list(UNMEASURED)}


def cell(x: Any, what: str) -> str:
    return "-" if x is None else f"{x:.0f}" if what in ("hook", "recall", "daemon", "tools") else f"{x:.1f}"


def run(state: Path, days: float, out: Path | None = None, now: float | None = None) -> tuple[list[str], Path]:
    """The table's lines and the file it went to (default <state>/bench/latency-<day>.json, the day's earlier run replaced),
    each median beside the one in the newest file of an earlier day."""
    b = bench(state, days, now)
    name = time.strftime("latency-%Y-%m-%d.json", time.localtime(b["until"]))
    earlier = sorted(p for p in (state / "bench").glob("latency-*.json") if p.name < name)
    was: dict[tuple[str, str], Any] = {}
    with contextlib.suppress(OSError, ValueError, KeyError, IndexError):
        was = {(r["what"], r["room"]): r["median"] for r in json.loads(earlier[-1].read_text())["rows"]}
    out = out or state / "bench" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**b, "was": earlier[-1].name if was else None}, indent=1) + "\n")
    lines = [f"latency, the last {days:g} days to {time.strftime('%Y-%m-%d %H:%M', time.localtime(b['until']))}: n, median, p90, "
             f"p95" + (f"; was: the median in {earlier[-1].name}" if was else ""),
             f"{'what':<20}{'room':<24}{'n':>7}{'median':>9}{'p90':>9}{'p95':>9}{'was':>9}"]
    lines += [f"{WHAT[r['what']]:<20}{r['room']:<24}{r['n']:>7}" + "".join(f"{cell(r[k], r['what']):>9}" for k in ("median", "p90", "p95"))
              + f"{cell(was.get((r['what'], r['room'])), r['what']):>9}" for r in b["rows"]]
    return lines + [f"not measured: {x}" for x in b["unmeasured"]], out
