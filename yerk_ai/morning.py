"""`yerk morning`: the morning diff, a daily report on how the install ran and what changed since the last
report, for the 06:30 timer (units/yerk-morning.timer). In plain words, a line each (DESIGN 9, L196, L197,
L204): every room's payload against the last delivered diff (`yerk map`'s numbers, so a planted change shows
up), yesterday's messages, queue wait and time to the first visible line, stalls, dead turns (a person's
message in the last 24 hours with no reply or notice for it) and shadow-gate flags from the run ledger, the
night's sweep, memory and CPU per unit from the running yerkd's /status, and the account meters. It is
posted through the running yerkd to the room whose rooms.toml entry says role = "machine", and to no other
room; with no such room it is only written under the state folder, and one line says where. A finding the
delivered diff did not have is an alert: the diff opens with it. The sizes and findings are stamped
only once the diff is delivered, so a failed post shows the same change again the next morning."""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import context, curate, docs, doctor, jobs, latency, memory, retention, status, words
from .accounts import Account, Cooldowns
from .config import budgets, load_toml, rooms, uncapped
from .ledger import Ledger
from .meters import Meters, meter_short

DAY = 86400.0
CALLS_SHOWN = 20                                         # a day's calls shown a line each, then their count (claim 19)
STATIC = ("mechanics", "persona", "rulebook", "room_voice", "pinned_memory", "rules")   # the parts whose change is
                                                                                        # a finding: the recall pack moves daily


def sizes(private: Path, state: Path) -> dict[str, dict[str, int]]:
    """Each room's session-start parts in characters before any trim (`yerk map`'s raw numbers), so a
    part past its budget still shows its growth; Claude Code's as the room last sent them (context.sent)."""
    b, rules = budgets(), memory.settings(private, state).root / memory.RULES
    return {name: {**context.sent(private, state, name, b)[0], **{p.name: p.raw for p in context.session_start(private, conf, b, rules=rules)}}
            for name, conf in rooms(private).items()}


def calls(ledger: Ledger, since: float, until: float) -> list[tuple[str, str]]:
    """Claim 19: each call a room made for the owner in the window, a line of its room and words and under it its id and the
    cites its turn read, or that it read nothing; the first CALLS_SHOWN, then the count after; a person's room's calls counted,
    never quoted; each fix filed in the window. Receipts, never findings, so a call's two lines stay together."""
    W, made = words, ledger.calls(since, until)
    mine = [c for c in made if since <= float(c["t"]) < until and c["whom"] == "owner"]
    bare = sum(1 for c in mine if c["no_reads"])
    out = [W.line(f"{W.PLAIN} " + (W.CALLS.format(day=W.YESTERDAY, n=len(mine)) + (W.CALLS_BARE.format(m=bare) if bare else "")
                                   if mine else W.CALLS_NONE.format(day=W.YESTERDAY)))]
    for c in mine[:CALLS_SHOWN]:
        reads, k, left = json.loads(c["reads"] or "[]"), 1, W.PHONE - W.width(W.CALL_READS.format(id=c["id"], reads=""))
        while k < len(reads) and W.width(", ".join(reads[:k + 1]) + W.READS_MORE.format(n=len(reads) - k - 1) * (k + 1 < len(reads))) <= left:
            k += 1
        said = W.NO_READS if c["no_reads"] else ", ".join(reads[:k]) + W.READS_MORE.format(n=len(reads) - k) * (k < len(reads))
        out.append("\n".join([W.line(f"{W.WORKING if c['no_reads'] else W.PLAIN} " + W.CALL_SAID.format(room=c["room"], call=c["call"])),
                              W.line(W.SUB + W.CALL_READS.format(id=c["id"], reads=said))]))
    if len(mine) > CALLS_SHOWN:
        out.append(W.line(f"{W.PLAIN} " + W.CALLS_MORE.format(n=len(mine) - CALLS_SHOWN)))
    theirs = Counter(str(c["room"]) for c in made if since <= float(c["t"]) < until and c["whom"] != "owner")
    out += [W.line(f"{W.PLAIN} " + W.CALLS_FOR.format(room=r, n=n, s="s" * (n != 1))) for r, n in sorted(theirs.items())]
    out += ["\n".join([W.line(f"{W.DONE} " + W.CALL_FIXED.format(id=c["id"], source=c["source"])), W.line(W.SUB + W.FIXED_BY.format(cite=c["correction"]))])
            for c in made if since <= float(c["fixed"] or 0) < until]
    return [("", x) for x in out]


def build(private: Path, state: Path, accounts: list[Account], cooldowns: Cooldowns, ledger: Ledger,
          live: dict[str, Any] | None, now: float) -> tuple[list[str], dict[str, Any]]:
    """(the diff's lines, the stamp to keep once it is delivered: sizes and finding keys)."""
    W = words
    try:
        last = json.loads((state / "morning" / "last.json").read_text())
    except (OSError, ValueError):
        last = {}
    since, until = status.midnight(now, 1), status.midnight(now)
    rows: list[tuple[str, str]] = []                     # (finding key or "", line)

    def add(key: str, mark: str, text: str) -> None:
        rows.append((key, W.line(f"{mark} {text}")))
    now_sizes, s = sizes(private, state), budgets()["session_start"]
    for room, parts in now_sizes.items():
        before, cc = last.get("sizes", {}).get(room), s["claude_code"]
        left = int(s["total"]) - (size := context.start_size(parts))       # the session start as sent and its gap to the line
        rows.append(("" if left >= 0 else f"over {room}", W.named(f"{W.PLAIN if left >= 0 else W.NOT_DONE} {W.PAYLOAD}", room, size=size, left=left)))
        rows += [(f"over {room} {k}", W.named(f"{W.NOT_DONE} {W.OVER_PART}", f"{room} {k}", size=parts[k], line=n))
                 for k, n in cc.items() if parts.get(k, 0) > n]
        for part in [*STATIC, *(k for k in cc if k in before)] if before else ():     # Claude Code's too, from its second diff
            if d := parts.get(part, 0) - before.get(part, 0):
                add(f"payload {room} {part} {d:+}", W.WORKING, W.PART.format(room=room, part=part, change=f"{d:+,}"))
    waits = ledger.waits(since, until)
    add("", W.PLAIN, W.ANSWERED.format(day=W.YESTERDAY, n=len(waits)))
    for word, xs in ((W.WAIT, [float(r["wait"]) for r in waits]),
                     (W.FIRST_LINE, [float(r["first"]) for r in waits if r["first"] is not None])):
        if xs:
            add("", W.PLAIN, word.format(mid=W.secs(statistics.median(xs)), worst=W.secs(max(xs))))
    day, before = latency.medians(ledger, since, until), latency.medians(ledger, status.midnight(now, 2), since)
    for k in ("first", "turn"):                          # the day's medians, each against the day before's (claim 18)
        if (x := day[k]) is not None:
            add("", W.PLAIN, W.SPEED_WAS.format(what=W.SPEED_OF[k], now=W.secs(x), was=W.secs(y) if (y := before[k]) is not None else "-"))
    stalls, started = ledger.stalls(since, until), float((live or {}).get("started") or 0)
    if not stalls and live is None:                      # a blind detector says so (L190)
        add("unknown stalls", W.NOT_DONE, W.UNKNOWN.format(what="stalls", why=W.NOT_ANSWERING_WHY))
    else:
        partly = W.SINCE.format(at=W.clock(started, now)) if since < started < until else ""
        rows.append(("stalls" if stalls else "", status.stall_line(stalls, W.YESTERDAY, partly)))
    dead = ledger.dead_turns(now - DAY, now - status.BLOCKED)    # a turn still inside its 10 minutes is not dead
    if dead:
        names = sorted({str(d["room"]) for d in dead})
        rows.append(("dead " + " ".join(sorted(str(d["message_id"]) for d in dead)), W.named(
            f"{W.NOT_DONE} {W.DEAD}", names[0] if len(names) == 1 else W.ROOMS_N.format(n=len(names)), n=len(dead),
            at=W.clock(float(dead[-1]["received"]), now))))
    elif live is None:                                    # messages sent while no yerkd ran were never ledgered
        add("unknown dead", W.NOT_DONE, W.UNKNOWN.format(what="dead turns", why=W.NO_YERKD))
    else:
        add("", W.PLAIN, W.NO_DEAD)
    flags = ledger.shadow_rules(since, until)
    for rule, n in sorted(flags.items()):                # a reply that held or conceded is counted, never a finding
        fine = rule in ("stance held", "stance conceded")
        add("" if fine else f"shadow {rule}", W.PLAIN if fine else W.WORKING, W.SHADOW.format(rule=rule, n=n))
    if not flags:
        add("", W.PLAIN, W.NO_SHADOW)
    rows += calls(ledger, since, until)                  # claim 19: each call's receipt, and a fix the day after it was filed
    if uncapped(budgets(load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {})):    # gates lifted: every day
        add("", W.PLAIN, status.spend_line(status.spend(ledger, since, until, status.midnight(now, 7)), W.YESTERDAY))
    elif (spent := ledger.spent(since, until)).get("n"):   # the paid calls, a spend row each (transcribe, voice_note, plugins/web)
        add("", W.PLAIN, W.SPENT.format(day=W.YESTERDAY, **spent))
    for d in ledger.deploys(now - DAY):                  # its card may never have reached the machine room
        if d["outcome"] != "proven" or (d["cut"] or 0) > 0:      # a proven one that cut a person's turn too
            said = W.UNPROVEN.format(kind=d["kind"], commit=str(d["after"] or d["before"] or "?")[:7], outcome=d["outcome"])
            said += W.UNPROVEN_CUT.format(n=d["cut"]) if d["cut"] else ""
            at = W.UNPROVEN_AT.format(at=W.clock(float(d["at"]), now))
            add(f"deploy {d['id']}", W.NOT_DONE, said + at if W.width(f"{W.NOT_DONE} {said}{at}") <= W.PHONE else said)
    if (swept := retention.line(state, now)):            # the night's sweep: what it moved by kind, or that it failed
        rows.append(swept)
    rows.append(doctor.line(state, now))                 # the daily check: ok, or its count and first finding
    rows += [x for x in [doctor.shape_line(state, now)] if x]           # the rule files' shape totals (shape.py)
    rows += docs.changed(state, float(last.get("at") or now - DAY))    # a docs page whose hash moved since the last diff
    rows.append(curate.line(state, now))                 # the night's curation: what landed, else what to read and the first
    rows += curate.rules_lines(state, now)               # the night's rule proposals by kind and number, for --apply DAY N
    if live is None:
        add("unknown units", W.NOT_DONE, W.UNKNOWN.format(what="memory", why=W.NOT_ANSWERING_WHY))
    for u in (live or {}).get("units", []):
        if not u.get("gone"):
            rows.append(("", W.named(f"{W.PLAIN} {W.UNIT_USE}", u["name"], mb=round(u["rss_mb"]), cpu=u["cpu_pct"])))
    meters = Meters(state / "meters.json")
    for a in accounts:
        rest = cooldowns.show(a.name)
        line = meter_short(a.name, meters.reading(a), rest[0]["until"] if rest else None, now)
        rows.append(("" if line.startswith(W.PLAIN) else f"account {a.name}", line))   # at or near its limit
    found = [k for k, _ in rows if k]
    new = [k for k in found if k not in last.get("findings", [])]
    head = (f"{W.NOT_DONE} " + W.MORNING_NEW.format(n=len(new)) if new else
            f"{W.PLAIN} {W.MORNING_SAME}" + (W.KNOWN.format(n=len(found)) if found else ""))
    lines = [W.line(head), *[x for k, x in rows if k in new], *[x for k, x in rows if k not in new]]
    return lines, {"at": now, "sizes": now_sizes, "findings": found}


def run(private: Path, state: Path, accounts: list[Account], cooldowns: Cooldowns,
        post: Callable[[dict[str, Any]], dict[str, Any]] | None = None, now: float | None = None) -> int:
    """Build the diff, write it under <state>/morning/, post it to the machine room; 1 if not delivered."""
    now = time.time() if now is None else now
    live = status.ask(state) if status.alive(state) else None
    lines, stamp = build(private, state, accounts, cooldowns, Ledger(state / "ledger.sqlite"), live, now)
    text = "\n".join(lines)
    (state / "morning").mkdir(parents=True, exist_ok=True)
    path = state / "morning" / time.strftime("%Y-%m-%d.txt", time.localtime(now))
    path.write_text(text + "\n")
    print(text)
    if not (machine := [n for n, c in rooms(private).items() if c.get("role") == "machine"]):
        print(f"morning: no room in rooms.toml has role = \"machine\"; the diff is at {path}", file=sys.stderr)
    else:
        req = {"morning": {"room": machine[0], "text": text, "key": f"morning-{int(now)}"}}
        try:
            out = (post or (lambda p: jobs.post_control(state, p, 60)))(req)
        except (OSError, ValueError) as e:
            out = {"ok": False, "error": f"yerkd did not answer ({type(e).__name__})"}
        if not out.get("ok"):
            print(f"morning: not posted to {machine[0]}: {out.get('error')}; the diff is at {path}", file=sys.stderr)
            return 1
    (state / "morning" / "last.json").write_text(json.dumps(stamp))     # only now: delivered (L196)
    return 0
