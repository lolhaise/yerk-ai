"""yerk: the command line."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__, context, memory, taste
from .config import EXAMPLE_PRIVATE, budgets, private_dir, rooms, state_dir, uncapped


def cmd_map(a: argparse.Namespace) -> int:
    priv = private_dir(a.private)
    rs = rooms(priv)
    if a.room not in rs:
        print(f"no room {a.room!r} in {priv / 'rooms.toml'}; rooms: {', '.join(rs) or 'none'}", file=sys.stderr)
        return 2
    from . import addons, location
    from .config import load_toml
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    b, root = addons.with_lines(budgets(conf), state_dir(a.state)), memory.settings(priv, state_dir(a.state)).root
    start, whole = context.session_start(priv, rs[a.room], b, rules=root / memory.RULES, room_name=a.room), context.uncapped(b)
    now = datetime.now(timezone.utc)
    msg = context.per_message(a.room, rs[a.room], "owner", now, now - timedelta(minutes=27), b,
                              location.part(location.settings(conf.get("location"), priv), rs[a.room], now))
    hit = []
    if str(rs[a.room].get("privacy", "owner")) == "owner":         # the taste part: an owner room's, from its layer's file
        cap, path = int(b["per_message"]["on_hit"]["taste"]), taste.kept(priv, rs[a.room])
        rated = taste.render(taste.load(path), a.room, cap, whole=whole) if path else ""
        hit = [context.Part("taste", rated, cap, context.chars(rated), str(path) if path else f"no {taste.FILE} in the layer")]
    label = "the fake example layer" if priv == EXAMPLE_PRIVATE else str(priv)
    sent, src = context.sent(priv, state_dir(a.state), a.room, b)        # Claude Code's parts as last sent, else at their line
    print(context.render(a.room, label, start, msg, b, hit, sent, src))
    tier = memory.live_rules(root / memory.TOPIC_RULES)
    mine, (seen, jobs) = [x for x in tier if (sc := memory.scope(x)) is None or a.room in sc], context.rode(state_dir(a.state), a.room)
    print(f"\nTopic rules ({memory.TOPIC_RULES}): {len(tier)}, {sum(context.chars(x) + 1 for x in tier):,} characters, {len(mine)} of them "
          f"this room's; " + ("each rides whole in the session start's `rules` part, a message only one filed since the session started"
                              if whole else "each rides in a message's `rules` part when the message names its topic, never at session start"))
    print(f"  on its last {seen} messages: " + (", ".join(f"{j} ({k})" for j, k in sorted(jobs.items(), key=lambda kv: -kv[1])) or "none rode"))
    if rs[a.room].get("watch"):
        print(f"\nWatches {rs[a.room]['watch']}: read as its own channel, never written to; it answers in {rs[a.room].get('channel')}.")
    bad = context.violations(start, msg, b, sent, src)
    for v in bad:
        print(("uncapped, past its line: " if whole else "OVER BUDGET: ") + v)
    if a.wire:
        if not (t := context.transcript(priv, state_dir(a.state), a.room)):
            print(f"no transcript for room {a.room} (sessions.json in {state_dir(a.state)})", file=sys.stderr)
            return 2
        print()
        print(context.render_wire(a.room, context.wire(t), context.chars(context.appended_file(start)), b,
                                  str(rs[a.room].get("timezone", "UTC"))))
    return 1 if bad and not whole else 0


def cmd_setup_room(a: argparse.Namespace) -> int:
    """The test room's webhook: found or made with the bot's token; its URL goes to a 0600 file
    in the private folder and is never printed. The channel comes from rooms.toml when it has one."""
    import asyncio
    import os
    from .channels.discord import setup_room
    from .config import load_toml
    priv = private_dir(a.private)
    d = load_toml(priv / "yerkd.toml")["discord"]
    room = rooms(priv).get(a.room, {})
    kind, _, cid = str(room.get("channel", "")).partition(":")
    channel_id = int(a.channel or (cid if kind == "discord" and cid.isdigit() else 0)) or None
    token = (priv / d["token_file"]).read_text().strip()
    out = asyncio.run(setup_room(token, int(d["guild"]), a.room, a.webhook, channel_id))
    path = priv / "secrets" / "discord-test-webhook.url"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(out.pop("webhook_url") + "\n")
    print(json.dumps({**out, "webhook_url_file": str(path)}, default=str))
    return 0


def _control(state_arg: str | None, payload: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
    """One POST to the running yerkd's /control (its port and the secret file's path are in
    <state>/yerkd.json), under the install's secret."""
    from .jobs import post_control
    return post_control(state_dir(state_arg), payload, timeout)


def cmd_room(a: argparse.Namespace) -> int:
    if a.cmd == "import" or a.action == "add":  # a Discord chat moves in, or its history only (move.py); the daemon is never touched
        import asyncio
        from . import move
        return asyncio.run(move.imports(a) if a.cmd == "import" else move.add(a))
    if a.action == "page":                      # the room's live page: its address, the key in the fragment (channels/page.py)
        from .channels.page import address
        print(address(state_dir(a.state), a.room, a.here))
        return 0
    out = _control(a.state, {a.action: a.room}, 900 if a.action == "compact" else 60)
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


def cmd_worker(a: argparse.Namespace) -> int:
    """start and stop go through the running yerkd, which owns the cards; list and report read the
    ledger, so they answer with the daemon down too."""
    from pathlib import Path
    from . import workers
    from .ledger import Ledger
    if a.action in ("start", "stop"):
        if a.action == "start" and not a.brief:
            print("start needs --brief FILE", file=sys.stderr)
            return 2
        if a.action == "stop" and not a.id:
            print("stop needs the worker's id", file=sys.stderr)
            return 2
        req = {"action": a.action, "id": a.id or "", "room": a.room or "", "tasks": a.task, "cap_minutes": a.cap,
               "account": a.account or "", "model": a.model or "", "effort": a.effort or "",
               "brief_file": str(Path(a.brief).expanduser().resolve()) if a.brief else ""}
        out = _control(a.state, {"worker": req}, 120)
        print(json.dumps(out))
        return 0 if out.get("ok") else 1
    ledger = Ledger(state_dir(a.state) / "ledger.sqlite")
    if a.action == "report":
        r = workers.report(ledger, a.id or "")
        if not r["ok"]:
            print(r["error"], file=sys.stderr)
            return 2
        print(json.dumps(r) if a.json else f"worker {r['id']} {r['state']}, account {r['account']}, model {r['model']}\n"
              f"report: {r['report']}\nfolder: {r['staging']}\nunit: {r['unit']}, {r['exit_detail'] or 'still running'}"
              f"\n\n{r['text']}")
        return 0
    rows = workers.listing(ledger, a.limit)
    if a.json:
        print(json.dumps(rows))
    for w in [] if a.json else rows:
        print(f"{w['id']} {w['state']} in {w['room']} on {w['account']}, {w['elapsed']} of {w['cap_minutes']:g} min"
              + (f": {w['result']}" if w["result"] else "") + (f"; relay {w['relay']}" if w["relay"] else "")
              + (f" ({w['relay_note']})" if w["relay"] == "skipped" else ""))
    return 0


def cmd_pictures(a: argparse.Namespace) -> int:
    """backfill: an owner room's pictures with no sidecar get one through Yerk workers the running yerkd starts, a batch at a
    time; --dry-run lists them (each sidecar it would write) and starts nothing. --root: the pictures under a folder the room
    lists in rooms.toml (`read` or `also`) in place of its inbox; any other folder is refused."""
    from . import pictures, words
    priv, state = private_dir(a.private), state_dir(a.state)
    if (conf := rooms(priv).get(a.room)) is None or str(conf.get("privacy", "owner")) != "owner":
        print(words.PICTURES_PERSON.format(room=a.room) if conf is not None else f"no room {a.room!r} in rooms.toml", file=sys.stderr)
        return 2
    folder = state / "rooms" / a.room / "work" / "inbox"
    if a.root:
        if (root := pictures.root_of(priv, state, a.room, conf, a.root)) is None:
            print(words.PICTURES_NOT_LISTED.format(room=a.room, folder=a.root), file=sys.stderr)
            return 2
        folder = root
    todo = pictures.pending(folder, bool(a.root))[:a.limit or None]
    if a.dry_run:
        for p in todo:
            print(pictures.sidecar(p))
        print(f"{len(todo)} picture(s) in {folder if a.root else a.room} with no sidecar, {-(-len(todo) // pictures.BATCH)} "
              f"batch(es) of {pictures.BATCH}; nothing started (--dry-run)")
        return 0
    out = _control(a.state, {"pictures": {"room": a.room, "limit": a.limit, **({"root": str(folder)} if a.root else {})}}, 120)
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


def cmd_files(a: argparse.Namespace) -> int:
    """backfill: the text beside each workbook and PDF in an owner room's inbox with none, or an older one, written here by
    code (files.write), no model and no yerkd; --dry-run lists each it would write; --root: those under a folder the room
    lists in rooms.toml (`read` or `also`) in place of its inbox. A person room's files are never read: refused."""
    from . import files, memory, pictures, words
    from .config import load_toml
    priv, state = private_dir(a.private), state_dir(a.state)
    if (conf := rooms(priv).get(a.room)) is None or str(conf.get("privacy", "owner")) != "owner":
        print(words.FILES_PERSON.format(room=a.room) if conf is not None else f"no room {a.room!r} in rooms.toml", file=sys.stderr)
        return 2
    root = pictures.root_of(priv, state, a.room, conf, a.root) if a.root else None
    if a.root and root is None:
        print(words.PICTURES_NOT_LISTED.format(room=a.room, folder=a.root), file=sys.stderr)
        return 2
    todo = files.pending(root or state / "rooms" / a.room / "work" / "inbox", root is not None)
    tz = None if a.dry_run or not todo else memory.settings(priv, state).tz     # the zone the daemon writes a header in
    whole = uncapped(budgets(load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}))     # [limits] uncapped: no cut
    for p in todo:
        print(files.sidecar(p) if tz is None else files.write(a.room, p, tz, whole=whole))
    print(words.FILES_BACKFILL.format(n=len(todo), where=root or a.room, done=words.FILES_DRY if a.dry_run else words.FILES_WRITTEN))
    return 0


def cmd_needs(a: argparse.Namespace) -> int:
    """list and done read and write <state>/needs.json and the day's page, so they answer with the daemon down (done lands the
    need's plug-in branches, needs.land); build goes through the running yerkd, which owns the workers and their cards."""
    from . import memory, needs, words
    if a.action == "list":
        print("\n".join(needs.listing(state_dir(a.state))) or "no needs filed")
        return 0
    if not (a.day and a.n) or a.action == "done" and not a.pr:
        print(f"{a.action} needs DAY N" + " PR" * (a.action == "done"), file=sys.stderr)
        return 2
    if a.action == "build":
        out = _control(a.state, {"needs": {"action": "build", "day": a.day, "n": a.n}}, 120)
        print(json.dumps(out))
        return 0 if out.get("ok") else 1
    priv, state = private_dir(a.private), state_dir(a.state)
    r = needs.update(memory.settings(priv, state), state, a.day, a.n, {"state": "merged", "pr": a.pr, "merged_at": time.time()})
    print(f"need {a.n} of {a.day}: {needs.said(r)}" if r else words.NEED_NONE.format(day=a.day, n=a.n))
    whys = {k: needs.land(k, p) for k, p in dict((r or {}).get("plugins") or {}).items()}       # its plug-in branches land too
    print("".join(f"{w or f'the {k} plug-in landed'}\n" for k, w in whys.items()), end="")
    return 1 if any(whys.values()) else 0 if r else 2


def cmd_calls(a: argparse.Namespace) -> int:
    """`yerk calls fix ID PATH:LINE NOTE` (claim 19): the correction for a call that stood on a wrong line, filed the way
    `remember` files one (a note in the call's room's day log naming the line it supersedes, that line marked), citing the
    call; never into a persona or a rule file. The next morning diff shows it."""
    from . import words
    from .config import load_toml, room_id
    from .ledger import Ledger
    priv, state = private_dir(a.private), state_dir(a.state)
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    s, confs, led = memory.settings(priv, state, dict(conf.get("memory", {}))), rooms(priv), Ledger(state / "ledger.sqlite")
    kept = {str((priv / str(v)).resolve()) for c in confs.values() for k in ("persona", "rulebook", "voice") if (v := c.get(k))}
    kept |= {str((s.root / f).resolve()) for f in (memory.RULES, memory.OVERFLOW, memory.TOPIC_RULES)}
    got: dict[str, Any] = memory.read(s, a.source, lines=1) if a.source.rpartition(":")[2].isdigit() else {"ok": False, "error": "give the line as path:line"}
    if not (c := led.call_row(a.id) or {}):
        got = {"ok": False, "error": f"no call {a.id}"}
    elif got["ok"] and got["path"] in kept:
        got = {"ok": False, "error": f"{got['path'].rsplit('/', 1)[-1]} is a persona or a rule file: fix the line the call stood on"}
    elif got["ok"] and (not got["text"].strip() or got["text"].lstrip().startswith("#")):
        got = {"ok": False, "error": f"{a.source} is a heading or a blank"}
    if not got["ok"]:
        print(f"not fixed: {got['error']}", file=sys.stderr)
        return 2
    log = memory.DailyLog(s.daily, s.tz, {n: (room_id(n, x), memory.room_class(x)) for n, x in confs.items()})
    note = words.CALL_FIX.format(note=a.note, id=a.id, room=c["room"], call=words.line(str(c["call"]), 80))
    if not (r := memory.remember(s, log, str(c["room"]), f"call-{a.id}", note, old=(a.source,)))["ok"]:
        print(f"not fixed: {r['error']}", file=sys.stderr)
        return 1
    led.call_fix(a.id, fixed=datetime.now().timestamp(), source=a.source, correction=r["cite"], note=a.note)
    print("\n".join([words.CALL_FIXED.format(id=a.id, source=a.source), words.FIXED_BY.format(cite=r["cite"]), *(f"superseded: {x}" for x in r["superseded"]),
                     *r["refused"], *r["not_edited"]]))
    return 0


def cmd_jobs(a: argparse.Namespace) -> int:
    """sync: the jobs file to one timer and service per job (idempotent), the diff, then every next
    fire read back from systemd; list: the read-back alone. Both work with the daemon down."""
    from . import jobs
    priv, state = private_dir(a.private), state_dir(a.state)
    units, path = jobs.Systemd(), private_dir(a.private) / jobs.FILE
    try:
        if a.action == "sync":
            p, back = jobs.sync(path, units, jobs.env_for(priv, state))
            lines = jobs.diff_lines(p) + ["next fire, read back from systemd:"] + jobs.read_lines(back, p.jobs)
            bad = bool(p.errors or p.failed)
        else:
            out = jobs.schedule_list(path, units)
            if not out["ok"]:
                raise OSError(out["error"])
            lines, back, bad = out["lines"], {r["name"]: r for r in out["jobs"]}, bool(out["errors"])
    except (OSError, subprocess.SubprocessError) as e:
        print(f"systemd could not be read or written: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"lines": lines, "jobs": back}, default=str) if a.json else "\n".join(lines))
    return 1 if bad else 0


def cmd_schedule(a: argparse.Namespace) -> int:
    """add, remove, list: the same three actions as the room's `schedule` tool, on the jobs file and
    the timers, with or without a running daemon. The zone defaults to the room's."""
    from . import jobs
    from .config import load_toml, models
    priv, state = private_dir(a.private), state_dir(a.state)
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    rs, units, env, path = rooms(priv), jobs.Systemd(), jobs.env_for(priv, state), priv / jobs.FILE
    if a.action == "list":
        out = jobs.schedule_list(path, units)
    elif a.action == "remove":
        out = jobs.schedule_remove(path, units, env, a.name or "")
    else:
        if not a.name or not a.when or not a.what:
            print("add needs NAME, --when and --what", file=sys.stderr)
            return 2
        room = a.room or (next(iter(rs)) if len(rs) == 1 else "")
        if room not in rs:
            print(f"--room: no room {room!r}; rooms: {', '.join(rs) or 'none'}", file=sys.stderr)
            return 2
        out = jobs.schedule_add(path, units, env, a.name, a.kind, a.when, a.what, room,
                                a.tz or jobs.default_tz(rs, room, conf), a.deliver, a.cap, a.account or "",
                                int(conf.get("jobs", {}).get("note_chars", jobs.NOTE_CHARS)), a.model or "", a.effort or "", models(conf))
    if a.json:
        print(json.dumps(out, default=str))
    elif not out["ok"]:
        print(out["error"], file=sys.stderr)
    elif a.action == "list":
        print("\n".join(out["lines"]))
    elif a.action == "remove":
        print("\n".join([f"{a.name}: removed"] + out["diff"]))
    else:
        print(f"{out['name']}: {out['schedule']}, {out['kind']} job, room {out['room']}, deliver {out['deliver']}"
              + "".join(f", {k} {out[k]}" for k in ("model", "effort") if k in out) + f"; next {out['next_text']}; unit {out['unit']}")
    return 0 if out["ok"] else 1


def _engine(a: argparse.Namespace) -> tuple[Any, ...]:
    from .accounts import Cooldowns, load_accounts
    from .config import state_dir
    priv, state = private_dir(a.private), state_dir(a.state)
    return priv, state, load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json")


def _clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%a %H:%M")


def cmd_accounts(a: argparse.Namespace) -> int:
    """Each account's order, token age and days to expiry (warning inside 30 days), rests and last
    health call; and the hand controls: cool, clear, token, health."""
    import time
    from . import words
    from .accounts import WARN_DAYS, store_token, token_info
    from .ledger import Ledger
    priv, state, accounts, cool = _engine(a)
    by = {x.name: x for x in accounts}
    if a.action in ("cool", "clear", "token", "mint") and a.name not in by:
        print(f"no account {a.name!r} in {priv / 'accounts.toml'}; accounts: {', '.join(by)}", file=sys.stderr)
        return 2
    if a.action == "cool":
        cool.set(a.name, a.kind, time.time() + a.minutes * 60)
        print(f"{a.name} rests ({a.kind}) until {words.short_when(time.time() + a.minutes * 60)}")
        return 0
    if a.action == "clear":
        cool.clear(a.name)
        print(f"{a.name}: no rest")
        return 0
    if a.action == "mint":
        from .accounts import mint
        from .engine import claude_bin
        status, stored = mint(by[a.name], claude_bin())
        print(f"{a.name}: " + (f"token stored in {by[a.name].token_file} (0600)" if stored else "no token in the output, nothing stored"))
        return 0 if stored else 1
    if a.action == "token":
        import getpass
        tok = getpass.getpass("token (not shown): ") if sys.stdin.isatty() else sys.stdin.read()
        if not tok.strip():
            print("no token read", file=sys.stderr)
            return 2
        issued = datetime.fromisoformat(a.issued).date() if a.issued else None
        print(f"{a.name}: token stored in {store_token(by[a.name], tok, issued)} (0600)")
        return 0
    ledger = Ledger(state / "ledger.sqlite")
    if a.action == "health":
        from . import health
        from .config import table
        from .lifted.classify import FailureTable
        from .meters import Meters
        pick = [by[n] for n in a.names if n in by] if a.names else accounts
        rows = health.check(pick, FailureTable.load(table("failures.toml")), ledger, cool, state, model=a.model,
                            meters=Meters(state / "meters.json"))
        for r in rows:
            ident = {1: "identity ok", 0: "IDENTITY FAIL", None: "identity unverified"}[r["identity_ok"]]
            print(f"{r['account']}: {'pass' if r['ok'] else 'FAIL ' + str(r['failure'])} in {r['ms']} ms, "
                  f"{r['login']} login, {ident} [{r['identity'] or '-'}]" + (f"; {r['note']}" if r["note"] else ""))
        return 0 if all(r["ok"] and r["identity_ok"] != 0 for r in rows) else 1
    warn = 0
    out = []
    for x in accounts:
        t = token_info(x)
        if t["token"] == "none":
            tok = "token: none, the config folder's own login"
        else:
            tok = f"token: {t['age_days']} days old, {t['days_left']} days left"
            if t["token"] != "ok":
                tok += f" (RENEW: {t['token']}, inside {WARN_DAYS} days)"
                warn += 1
        rests = cool.show(x.name)
        rest = ", ".join(f"rests ({r['kind']}, x{r['repeats']}) until {words.short_when(r['until'])}" for r in rests) or "ready"
        h = ledger.health_last(x.name)
        hl = "no health call yet" if not h else (
            f"health {'pass' if h['ok'] else 'FAIL'} {_clock(h['at'])}"
            + {1: ", identity ok", 0: ", IDENTITY FAIL", None: ""}[h["identity_ok"]])
        out.append({"name": x.name, "order": x.order, "config_dir": str(x.config_dir), **t, "rests": rests, "health": h})
        if not a.json:
            print(f"{x.name} (order {x.order}): {tok}; {rest}; {hl}")
    if a.json:
        print(json.dumps(out, default=str))
    return 1 if warn else 0


def cmd_status(a: argparse.Namespace) -> int:
    """The card (status.py): the running yerkd's /status, else what the files say, at phone width and
    state first (STYLE.md). --full: the developer detail (pid, units, how old each meter reading is,
    today's stall rows and their dumps)."""
    import os
    import time
    from . import status, words
    from .ledger import Ledger
    from .meters import Meters, meter_line
    priv, state, accounts, cool = _engine(a)
    if not getattr(a, "full", False):
        from . import deploy
        from .config import REPO
        ledger = Ledger(state / "ledger.sqlite")
        live = not getattr(a, "json", False)      # the card asks the meters now; a monitoring tool's poll never does
        s = status.ask(state, payload={"live": live}) if status.alive(state) else None
        s = s or {**status.collect(priv, state, accounts, cool, ledger, live=live), "alive": status.alive(state),
                  "commit": deploy.head(REPO)}
        from . import addons
        s["addons"] = [f"{n} {ab.get('version', '')}".strip() for n, ab in addons.installed(state).items()]
        if getattr(a, "json", False):             # one document for a monitoring tool (status.document)
            print(json.dumps(status.document(priv, state, s, ledger), default=str))
            return 0
        for line in status.card(s):
            print(line)
        return 0
    try:
        st = json.loads((state / "yerkd.json").read_text())
        os.kill(int(st["pid"]), 0)
        print(f"yerkd: running ({st['mode']}), pid {st['pid']}, rooms {', '.join(st['rooms'])}")
    except (OSError, ValueError, KeyError):
        print(words.NOT_RUNNING)
    meters = Meters(state / "meters.json")
    for x in accounts:
        rest = cool.show(x.name)
        print(meter_line(x.name, meters.reading(x)) + (f"; rests until {words.short_when(rest[0]['until'])}" if rest else ""))
    from . import workers
    try:
        units: set[str] | None = workers.Systemd().running()
    except (OSError, subprocess.SubprocessError):
        units = None                          # no systemd here: the ledger alone, unchecked
    lines = workers.status_lines(Ledger(state / "ledger.sqlite"), units, full=True)
    for line in lines or [words.NONE_RUNNING]:
        print(line)
    from . import jobs
    for line in jobs.status_lines(priv / jobs.FILE, jobs.Systemd()) or [f"{jobs.NOTHING} jobs: none scheduled"]:
        print(line)                           # every job's next fire, from systemd, never from memory
    for r in [r for r in Ledger(state / "ledger.sqlite").job_runs(limit=50) if r["delivery"] == "alert"][:5]:
        print(f"job {r['job']} run {r['id']}: {r['exit'] or 'no exit'}, {r['result']}; {r['note'] or 'no note'}")
    for r in Ledger(state / "ledger.sqlite").stalls(status.midnight(time.time())):   # the detail a card leaves out
        print(f"stall {_clock(r['began'])}: {r['seconds']} s late, in {r['doing']}; dump {r['dump'] or 'none'}")
    return 0


def cmd_morning(a: argparse.Namespace) -> int:
    """The morning diff (morning.py): to the room with role = "machine", else a file under the state folder."""
    from . import morning
    priv, state, accounts, cool = _engine(a)
    return morning.run(priv, state, accounts, cool)


def cmd_retention(a: argparse.Namespace) -> int:
    """The daily sweep (retention.py): a line per kind it moved, none when nothing did; --dry-run moves nothing."""
    from . import retention
    return retention.main(private_dir(a.private), state_dir(a.state), a.dry_run)


def cmd_backup(a: argparse.Namespace) -> int:
    """`yerk backup` (backup.py): one archive with its manifest, one line; exit 1 when none was written."""
    from . import backup
    return backup.main(private_dir(a.private), state_dir(a.state), a.to, a.with_index)


def cmd_restore(a: argparse.Namespace) -> int:
    """`yerk restore ARCHIVE` (backup.py): read against its manifest first, then a line a step; exit 1 when refused."""
    from . import backup
    return backup.restore_main(a.archive, private_dir(a.private), state_dir(a.state), a.to, a.dry_run, a.force)


def cmd_location(a: argparse.Namespace) -> int:
    """`yerk location day [DATE]`: the day's places as one line in its day log (location.py), an earlier run's line replaced;
    prints the line, or why there is none; exit 1 with no [location] table, no located place in its places table or no readable fixes file."""
    from datetime import date
    from . import location
    from .config import load_toml
    priv, state = private_dir(a.private), state_dir(a.state)
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    if (loc := location.settings(conf.get("location"), priv)) is None or not location.places(loc.places):     # a silent night is a failure
        print("not written: yerkd.toml has no [location] table with a file and a places table that names a place", file=sys.stderr)
        return 1
    s = memory.settings(priv, state, dict(conf.get("memory", {})))
    on = date.fromisoformat(a.date) if a.date else datetime.now(s.tz).date()
    try:
        line = location.day(loc, s.daily, s.tz, on, a.dry_run)
    except OSError as e:
        print(f"not written: {loc.file.name} could not be read: {e.strerror}", file=sys.stderr)
        return 1
    print(line or f"nothing written: no fix at a named place on {on}")
    return 0


def cmd_doctor(a: argparse.Namespace) -> int:
    """The daily check and the crash repair (doctor.py): a line per finding, one green line when all is well; --dry-run
    changes nothing; --scrub swaps the value on each secret-shaped line the check names, each file backed up first."""
    from . import doctor
    return doctor.main(private_dir(a.private), state_dir(a.state), a.dry_run, refresh=not a.scrub, scrubbing=a.scrub)


def cmd_docs(a: argparse.Namespace) -> int:
    """The Claude docs Yerk keeps (docs.py): refresh them now, list them, show one; or `shape`, the rule-shape report the
    doctor writes for each room (shape.py), printed here without a doctor run. Exit 1 on a page not fetched or a count past
    budgets.toml [shape], 2 on a page that is not on the list. `own`: Yerk AI's own pages, written from the code alone
    (owndocs.py); --dry-run names each stale one and writes nothing, exit 1 when one is."""
    if a.action == "own":
        from . import owndocs
        return owndocs.main(a.dry_run)
    from . import docs, shape
    priv, state = private_dir(a.private), state_dir(a.state)
    conf = docs.settings(priv)
    if a.action == "refresh":
        r = docs.refresh(state, conf)
        print(f"docs: {len(r['new'])} new, {len(r['changed'])} changed, {len(r['same'])} the same" +
              "".join(f"; changed: {x}" for x in r["changed"]))
        for x in docs.failed(r):
            print(x)
        return 1 if r["failed"] else 0
    if a.action == "list":
        print("\n".join(docs.listing(state, conf)))
        return 0
    if a.action == "show":
        path = docs.root(state) / "claude" / f"{a.name}.md"
        if a.name not in docs.pages(conf) or not docs.SLUG.fullmatch(a.name) or not path.is_file():
            print(f"no page {a.name!r} kept; pages: {', '.join(docs.pages(conf))}", file=sys.stderr)
            return 2
        print(path.read_text(), end="")
        return 0
    if a.rules:
        print(shape.rules_text(), end="")
        return 0
    rs, limits = rooms(priv), dict(budgets().get("shape") or {})
    if a.name and a.name not in rs:
        print(f"no room {a.name!r} in rooms.toml; rooms: {', '.join(rs)}", file=sys.stderr)
        return 2
    r = shape.check(priv, memory.settings(priv, state), rs, conf, datetime.now().timestamp())
    for name in [a.name] if a.name else r["rooms"]:
        print(shape.report(name, r, limits, datetime.now().strftime("%Y-%m-%d %H:%M")))
    print("\n".join(found := shape.findings(r, limits)) or "rule shape: every count is within budgets.toml [shape]")
    return 1 if found else 0


def cmd_curate(a: argparse.Namespace) -> int:
    """The night's card curation (curate.py): the sweep, then yesterday's facts, or each day from --since, proposed,
    knocked down, checked and landed past the bench, then the rule files' proposals; --dry-run changes nothing. --rules:
    the proposals alone; --apply DAY N[,N...]: the owner's word, those proposals written (or `undo`, the last apply back)."""
    from . import curate
    if a.apply:
        return curate.apply_main(private_dir(a.private), state_dir(a.state), *a.apply)
    if a.rules:
        return curate.rules_main(private_dir(a.private), state_dir(a.state), a.dry_run)
    return curate.main(private_dir(a.private), state_dir(a.state), a.since, a.dry_run)


def cmd_deploy(a: argparse.Namespace) -> int:
    """`yerk deploy [REF]` (deploy.py): fast-forward, fast tests, a quiet room, the restart, the proof,
    else the rollback; the card to the machine room. --unit: onto units/yerkd.service as well. --timers: only each timer
    in units/ the box has not installed, installed and on (the doctor's fix for one), nothing restarted."""
    from . import deploy, words
    from .config import REPO, state_dir
    if a.timers:
        print("\n".join(out := deploy.timers(REPO, deploy.USER_UNITS)) or f"{words.DONE} every timer in units/ is installed")
        return int(any(x.startswith(words.NOT_DONE) for x in out))
    return deploy.run(private_dir(a.private, quiet=True), state_dir(a.state), a.ref or "", tests=not a.no_tests, unit_file=a.unit)


def cmd_recall_bench(a: argparse.Namespace) -> int:
    """The fixed question set against the memory index (L63). Prints ids, ranks and rates, never a
    question's text or a snippet (the questions are about the owner's life); one JSON line per run
    goes to <state>/memory/recall-bench.jsonl, the weekly series, and the run is held to it (memory.ratchet)."""
    from pathlib import Path

    from . import memory
    from .config import state_dir
    state = state_dir(a.state)
    s = memory.settings(private_dir(a.private), state)
    qfile = Path(a.questions).expanduser() if a.questions else s.bench
    if a.controls:                                      # `yerk deploy`'s recall step: one JSON line, nothing written
        print(json.dumps(r := memory.controls(s, qfile, a.k)))
        return 2 if r["missed"] else 0
    if not qfile.is_file():
        print(f"no question set at {qfile} ([memory] bench in yerkd.toml names it)", file=sys.stderr)
        return 2
    r = memory.bench(s, qfile, a.k)
    if not r["ok"]:
        print(f"recall-bench: the index {'timed out (busy, not down)' if r['status'] == 'timeout' else 'failed'}: "
              f"{r['error']}", file=sys.stderr)
        return 3 if r["status"] == "timeout" else 1
    keep = ("questions", "controls", "k", "top_k", "top_1", "top_k_rate", "top_1_rate", "blind", "kept", "lost", "kinds")
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **{x: r[x] for x in keep},
           "misses": [x["id"] for x in r["rows"] if not x["rank"]]}
    (state / "memory").mkdir(parents=True, exist_ok=True)
    with open(state / "memory" / "recall-bench.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    rec = budgets().get("recall") or {}
    series = [json.loads(x) for x in (state / "memory" / "recall-bench.jsonl").read_text().splitlines() if x.strip()]
    drops = [d for x in r["kinds"] if (d := memory.ratchet(series, list(rec.get("drops", [])), int(rec.get("slack", 1)), x))]
    if a.json:
        print(json.dumps({**row, "rows": r["rows"]}, indent=1))
    else:
        print(f"top {r['k']}: {r['top_k']}/{r['questions']} ({r['top_k_rate']:.1%})   "
              f"top one: {r['top_1']}/{r['questions']} ({r['top_1_rate']:.1%})   controls: {r['controls']}   "
              f"answer in the snippet: {r['kept']}/{r['top_k']}")
        print("misses: " + (" ".join(row["misses"]) or "none"))
        for x, n in r["kinds"].items():
            print(f"{x}: top {r['k']} {n['top_k']}/{n['questions']} ({n['top_k_rate']:.1%})   top one {n['top_1']}/{n['questions']} "
                  f"({n['top_1_rate']:.1%})")
        if r["blind"]:
            print(f"BLIND: control question(s) missed: {', '.join(r['blind'])}", file=sys.stderr)
    for d in drops:
        print(f"RATCHET: {d}", file=sys.stderr)
    return 5 if r["blind"] else 4 if drops else 0      # 5: blind, its own code, so the weekly unit fails it (2 is no question set)


def cmd_bench(a: argparse.Namespace) -> int:
    """`yerk bench latency` (latency.py): the table, and the file it went to on stderr; the ledger read only."""
    from pathlib import Path

    from . import latency
    from .config import state_dir
    lines, out = latency.run(state_dir(a.state), a.days, Path(a.out).expanduser() if a.out else None)
    print("\n".join(lines))
    print(f"written to {out}", file=sys.stderr)
    return 0


def cmd_eval(a: argparse.Namespace) -> int:
    """`yerk eval taste` (taste_eval.py): the taste judge over the owner's complaints and the replies they accepted, phase 2
    beside phase 1; the bar printed, never enforced (exit 0 whatever the numbers; 2 when the case file is missing)."""
    from pathlib import Path

    from . import evidence, taste_eval
    here = private_dir(a.private) / "evals" / "taste"
    cases, controls = (Path(x).expanduser() if x else here / name for x, name in ((a.cases, "cases.jsonl"), (a.controls, "controls.jsonl")))
    rubric = Path(a.rubric).expanduser() if a.rubric else here / "rubric.md" if (here / "rubric.md").is_file() else evidence.TASTE_RUBRIC
    for f in (cases, controls, rubric):
        if not f.is_file():
            print(f"no file at {f} (--cases, --controls and --rubric name them; else <private>/evals/taste/)", file=sys.stderr)
            return 2
    out = Path(a.out).expanduser() if a.out else cases.with_name("scores-taste.json")
    lines, _ = taste_eval.run(cases, controls, rubric, Path(a.ledger).expanduser() if a.ledger else state_dir(a.state) / "ledger.sqlite",
                              out, a.model, a.effort, a.jobs, a.cap, a.sample)
    print("\n".join(lines))
    print(f"every row: {out}")
    return 0


def cmd_texting(a: argparse.Namespace) -> int:
    """`yerk texting sheets --check`: the reply list and each person's sheet read (texting/people.check), nothing written;
    each that does not load, is missing or is named by no one, one line with its file; exit 1 then. `replay`: texting_replay."""
    from . import words
    from .config import load_toml
    from .texting import people
    if not (a.check if a.action == "sheets" else a.since):
        print(words.REPLAY_NEEDS.format(action=a.action, flag="--check" if a.action == "sheets" else "--since YYYY-MM-DD"), file=sys.stderr)
        return 2
    priv = private_dir(a.private)
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    if a.action == "replay":
        return texting_replay(a, priv, conf)
    bad = people.check(people.path(priv, dict(conf.get("texting") or {})))
    print("\n".join(bad) or "every sheet loads")
    return 1 if bad else 0


def texting_replay(a: argparse.Namespace, priv: Any, conf: dict[str, Any]) -> int:
    """`yerk texting replay` (texting/replay.py, J30 to J32): the plan, which calls and searches nothing; with --run the header,
    each cut through the live writer and check on the install's accounts in the report room's order, the table, the file.
    The memory part is the report room's search as the daemon runs it, borrowed with no daemon made (its constructor opens
    the install's ledger); the index is read, never built or refreshed. The recall part is left out whole (J31)."""
    from datetime import date
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from . import words
    from .accounts import load_accounts
    from .config import models, room_settings, table
    from .daemon import Yerkd
    from .lifted.classify import FailureTable
    from .texting import people, replay
    t, room_confs = dict(conf.get("texting") or {}), rooms(priv)
    if (report := str(t.get("report") or "")) not in room_confs:
        print(words.REPLAY_OFF)
        return 1
    tz = ZoneInfo(str(dict(conf.get("memory") or {}).get("timezone") or memory.system_zone()))   # the import's own, memory.settings
    texts, file = priv / "imports" / "texts", people.path(priv, t)
    todo, said = replay.plan(file, texts, date.fromisoformat(a.since), a.limit, float(t.get("catch_up_hours", 6)), tz,
                             a.person or "", Path(a.old_log).expanduser() if a.old_log else None)
    print("\n".join([*said, words.REPLAY_PLAN.format(cuts=len(todo), since=a.since, limit=a.limit)]), flush=True)
    if not a.run:
        print(words.REPLAY_DRY)
        return 0
    if not ((state := state_dir(a.state)) / "docs").is_dir():         # memory.settings would make it; an install has it
        print(words.REPLAY_NO_DOCS.format(docs=state / "docs"))
        return 1
    print("\n".join([words.REPLAY_AS_OF, words.REPLAY_LEFT_OUT, *words.REPLAY_LIMITS]), flush=True)

    class Search:
        search, reader, owner = Yerkd.search, Yerkd.reader, Yerkd.owner

        def __init__(self) -> None:                  # as Yerkd.__init__ makes them
            self.room_confs, self.memory = room_confs, memory.settings(priv, state, dict(conf.get("memory", {})))
            self.scoped = {n: self.memory.only(priv, n, c["also"]) for n, c in room_confs.items() if "also" in c}
    d: Any = Search()
    accts = load_accounts(priv / "accounts.toml")
    named = [x for n in room_confs[report].get("accounts") or [] for x in accts if x.name == n]     # its order, as daemon.texting
    f = replay.go(state, f"{datetime.now(tz):%Y-%m-%d-%H%M%S}", {"since": a.since, "limit": a.limit, "people": sorted({c.key for c, _ in todo}),
                  "left_out": ["recall"]}, t, file, report, todo, lambda text: d.search(report, text, docs=False).get("hits") or [],
                  lambda name: replay.located(name, d.memory.cites, d.memory.root), texts, named + [x for x in accts if x not in named],
                  FailureTable.load(table("failures.toml")), room_settings(room_confs[report], state, report, models(conf))["model"][0])
    lines = [json.loads(x) for x in f.read_text().splitlines()[1:]]
    print("\n".join([*replay.table(lines), words.REPLAY_FILE.format(file=f)]))
    if failed := sum(1 for x in lines if x.get("error")):
        print(words.REPLAY_KEPT.format(n=failed, folder=f.with_suffix("")))
    return 1 if failed else 0


def cmd_abilities(a: argparse.Namespace) -> int:
    """Every ability in the manifest, one line each, state first: on, needs <provider>, runs on the
    free path, known but not built."""
    from . import abilities, memory
    from .config import state_dir
    s = memory.settings(private_dir(a.private), state := state_dir(a.state))
    tab = abilities.manifest(state)
    for name, ab in sorted(tab.items(), key=lambda kv: kv[1].get("state", "built") == "described"):
        print(abilities.line(name, ab, tab, s))
    print("yerk explain <ability>: what it does, needs and costs")
    return 0


def cmd_add(a: argparse.Namespace) -> int:
    """`yerk add SOURCE` and `yerk remove NAME` (addons.py), each step a line; never on the fake example layer."""
    from . import addons, words
    if (priv := private_dir(a.private, quiet=True)).resolve() == EXAMPLE_PRIVATE.resolve():
        print(f"{words.NOT_DONE} {words.NO_LAYER}", file=sys.stderr)
        return 2
    return (addons.add if a.cmd == "add" else addons.remove)(a.source, priv, state_dir(a.state), a.dry_run)


def _settings(a: argparse.Namespace) -> list[dict[str, Any]]:
    from . import memory, words
    from .config import load_toml
    priv = private_dir(a.private)
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    return words.described(conf.get("presentation")) + memory.described(conf.get("memory"))


def _pick(rows: list[dict[str, Any]], key: str, sections: bool = False) -> list[dict[str, Any]]:
    """The described settings a key names: whole, by its last part (`yerk config get embeddings_model`),
    or with `sections` a whole section (`yerk config get memory`)."""
    want = key.strip(".")
    return [r for r in rows if not want or r["key"] in (want, f"presentation.{want}", f"memory.{want}")
            or (sections and r["key"].startswith(f"{want}."))]


def _setting_lines(r: dict[str, Any]) -> list[str]:
    return [f"{r['key']} = {r['value']} ({'set in yerkd.toml' if r['source'] != 'default' else 'the default'})",
            f"  {r['what']}", f"  choices: {', '.join(r['choices'])}; {r['reloads']}"]


def cmd_explain(a: argparse.Namespace) -> int:
    """One ability from the manifest (what it does, needs and costs, its free path; how a described
    one would be built and why it is not), else one setting, else each ability whose entry names the word in its `command`
    (`yerk explain docs`); state first, plain words, one state line each."""
    from . import abilities, memory
    from .config import state_dir
    from .words import NOT_DONE
    table_ = abilities.manifest(state_dir(a.state))
    if (ab := table_.get(a.ability)) is None:
        if (hit := _pick(_settings(a), a.ability)):
            print("\n".join(_setting_lines(hit[0])))
            return 0
        if not (served := [n for n, x in table_.items() if a.ability in str(x.get("command", "")).split()]):
            print(f"{NOT_DONE} no ability, setting or command {a.ability!r}; abilities: {', '.join(table_)}", file=sys.stderr)
            return 2
        s = memory.settings(private_dir(a.private), state_dir(a.state))
        print("\n\n".join("\n".join(abilities.explain(n, table_[n], table_, s)) for n in served))
        return 0
    s = memory.settings(private_dir(a.private), state_dir(a.state))
    print("\n".join(abilities.explain(a.ability, ab, table_, s)))     # one state line: the first (STYLE 1)
    if a.ability == "jobs":     # the jobs file's keys, from the code (decision 16)
        from . import jobs
        for head, table in (("a [jobs.NAME] table in the private jobs.toml", jobs.KEYS), ("[jobs] in the private yerkd.toml", jobs.CONF)):
            print(f"keys of {head}:")
            for key, words in table.items():
                print(f"  {key}: {words}")
    if a.ability == "retention":              # the days this install keeps, from its yerkd.toml
        from . import retention
        print(retention.here(private_dir(a.private)))
    return 0


def cmd_config(a: argparse.Namespace) -> int:
    """`yerk config get [KEY]`: the settings Yerk AI describes in code (decisions 16 and 27), each with
    its value here, where it came from, its choices and its plain words. Only these are printed.
    `yerk config get room NAME`: the room's model and effort, each from rooms.toml or the room's override."""
    if a.key == "room":
        from .config import load_toml, models, room_settings, state_dir
        confs = rooms(priv := private_dir(a.private))
        if a.name not in confs:
            print(f"no room {a.name!r}; rooms: {', '.join(confs)}", file=sys.stderr)
            return 2
        listed = models(load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {})
        now = room_settings(confs[a.name], state_dir(a.state), a.name, listed)
        got = [{"key": f"rooms.{a.name}.{k}", "value": v, "source": src} for k, (v, src) in now.items()]
        print(json.dumps(got) if a.json else "\n".join(f"{r['key']} = {r['value'] or 'unset'} ({r['source']})" for r in got))
        return 0
    rows = _settings(a)
    if not (pick := _pick(rows, a.key, sections=True)):
        print(f"no setting {a.key!r}; settings: {', '.join(r['key'] for r in rows)}", file=sys.stderr)
        return 2
    print(json.dumps(pick) if a.json else "\n".join(x for r in pick for x in _setting_lines(r)))
    return 0


def parser() -> argparse.ArgumentParser:
    """Every command and flag of `yerk`, with its help; docs/commands.md is written from it (`yerk docs own`)."""
    from . import words
    p = argparse.ArgumentParser(prog="yerk", description="Yerk AI: chat rooms as long-lived Claude Code sessions.")
    p.add_argument("--version", action="version", version=f"yerk {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map", help="print a room's context part by part against budgets.toml")
    m.add_argument("room")
    m.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    m.add_argument("--wire", action="store_true", help="also the sizes as sent, from the room's session transcript")
    m.add_argument("--state", help="state folder for --wire and the example layer's rules (default: $YERK_STATE, else ~/.local/state/yerk)")
    s = sub.add_parser("setup-room", help="find or make a Discord room's test webhook (URL to a 0600 file)")
    s.add_argument("room")
    s.add_argument("--private", help="private folder (default: $YERK_PRIVATE)")
    s.add_argument("--channel", help="the channel id, when rooms.toml has none yet")
    s.add_argument("--webhook", default="yerk-test-sender")
    r = sub.add_parser("room", help="act on a running yerkd's room (close, compact), add one (a Discord chat moved in), or print "
                       "the address of its live page (page)")
    r.add_argument("action", choices=["close", "compact", "add", "page"])
    r.add_argument("room", help="the room; add: NAME, for a room NAME-on-yerk")
    r.add_argument("--from", dest="source", default="", help="add: discord:<channel or thread id>, the chat that moves in")
    r.add_argument("--person", type=int, default=0, help="add: a person room, for this Discord user id, behind the wall")
    r.add_argument("--voice", help="add: the voice file copied in (default: a one-line placeholder)")
    r.add_argument("--since", help="add: the history from this day on, YYYY-MM-DD (default: all of it)")
    r.add_argument("--like", help="add: the room it is made like (default: the last of its kind in rooms.toml)")
    r.add_argument("--dry-run", action="store_true", help="add: read and print; write and send nothing")
    r.add_argument("--here", action="store_true", help="page: the box's own 127.0.0.1 address, where [page] listen or address "
                   "names the host other screens reach")
    r.add_argument("--private", help="add: private folder (default: $YERK_PRIVATE, else the fake example layer)")
    r.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    im = sub.add_parser("import", help="a chat's whole history into the day logs, no room made: discord:<channel or thread id>, "
                        "or discord-guild:<guild id> for every chat on the server no room covers (--dry-run: the inventory)")
    im.add_argument("source", help="discord:<channel or thread id>, discord-guild:<guild id>, imessage:<a Messages chat.db> "
                    "(texts into imports/texts), gmail (All Mail into imports/mail, by yerkd.toml [gmail]) or openclaw:<an "
                    "OpenClaw workspace's folder> (its notes copied into openclaw/<its name>, the lines that read them printed)")
    im.add_argument("--contacts", help="imessage: a Contacts database, for the names its numbers and addresses have")
    im.add_argument("--since", help="the history from this day on, YYYY-MM-DD (default: all of it; openclaw: the files changed since)")
    im.add_argument("--files", action="store_true", help="save attachments up to 25 MB too (default: each named with its size, none downloaded)")
    im.add_argument("--dry-run", action="store_true", help="read and count, write nothing; on one chat, also print every line it would file")
    im.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    im.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    ac = sub.add_parser("accounts", help="tokens, rests and health per account; cool, clear, token, mint, health")
    ac.add_argument("action", nargs="?", default="list", choices=["list", "cool", "clear", "token", "mint", "health"])
    ac.add_argument("name", nargs="?", help="cool, clear, token, mint: the account")
    ac.add_argument("minutes", nargs="?", type=float, default=30.0, help="cool: minutes to rest")
    ac.add_argument("--kind", default="manual", help="cool: the rest's kind (default manual)")
    ac.add_argument("--issued", help="token: the issue date, YYYY-MM-DD (default today)")
    ac.add_argument("--model", default="haiku", help="health: the model for the one tiny turn")
    ac.add_argument("--names", nargs="*", default=[], help="health: only these accounts")
    ac.add_argument("--json", action="store_true")
    st = sub.add_parser("status", help="the card: " + "; ".join(words.STATUS_PARTS))      # its parts, from the list the card keeps
    st.add_argument("--full", action="store_true", help="the developer detail: pid, unit names, reading ages")
    st.add_argument("--json", action="store_true", help="one JSON document for a monitoring tool: rooms, accounts, last turn "
                    "per room, the last day's failures, the index, the backup's age")
    mo = sub.add_parser("morning", help="the morning diff (payload changes, yesterday's waits, stalls, shadow flags, "
                        "memory and CPU, meters) to the room with role = \"machine\", else a file under the state folder")
    wk = sub.add_parser("worker", help="workers: start (needs a running yerkd), list, stop, report")
    wk.add_argument("action", choices=["start", "list", "stop", "report"])
    wk.add_argument("id", nargs="?", help="stop, report: the worker's id")
    wk.add_argument("--brief", help="start: the brief file; a run whose last message is exactly NO_REPLY settles its card quiet, with no relay")
    wk.add_argument("--room", help="start: the room for its card and relay (default: the only room, else required)")
    wk.add_argument("--task", action="append", default=[], help="start: a TASKS.md row (repeat for each)")
    wk.add_argument("--cap", type=float, default=0.0, help="start: minutes before it is stopped (default [workers], 65)")
    wk.add_argument("--account", help="start: the account to run on (default [workers], else the first not resting)")
    wk.add_argument("--model", help="start: the model (default [workers], else the room's)")
    wk.add_argument("--effort", help="start: the thinking level, one of low, medium, high, xhigh, max (default [workers], else the room's)")
    wk.add_argument("--limit", type=int, default=20, help="list: how many")
    wk.add_argument("--json", action="store_true")
    wk.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    pc = sub.add_parser("pictures", help="backfill: every picture in an owner room's inbox with no NAME.seen.txt gets one, read by "
                        "Yerk workers on the room's own account, 20 a batch, a line to the machine room per batch (needs a running yerkd)")
    pc.add_argument("action", choices=["backfill"])
    pc.add_argument("room", help="the owner room")
    pc.add_argument("--limit", type=int, default=0, help="at most this many pictures, oldest first (default: all)")
    pc.add_argument("--dry-run", action="store_true", help="list each sidecar it would write; start nothing")
    pc.add_argument("--root", help="a folder the room lists in rooms.toml (read or also): its pictures in place of the inbox's")
    fl = sub.add_parser("files", help="backfill: every workbook and PDF in an owner room's inbox with no text beside it (NAME.sheet.txt, "
                        "NAME.pdf.txt), or an older one, gets it now, by code in this process, no model and no yerkd")
    fl.add_argument("action", choices=["backfill"])
    fl.add_argument("room", help="the owner room")
    fl.add_argument("--dry-run", action="store_true", help="list each text file it would write; write nothing")
    fl.add_argument("--root", help="a folder the room lists in rooms.toml (read or also): its files in place of the inbox's")
    nd = sub.add_parser("needs", help="what the rooms could not do and filed with `need`: list (each one's state), build DAY N "
                        "(a builder worker on [needs] account, its card in the machine room; needs a running yerkd), done DAY N PR")
    nd.add_argument("action", choices=["list", "build", "done"])
    nd.add_argument("day", nargs="?", default="", help="build, done: the day it was filed (YYYY-MM-DD), as list shows it")
    nd.add_argument("n", nargs="?", type=int, default=0, help="build, done: its number that day")
    nd.add_argument("pr", nargs="?", type=int, default=0, help="done: the pull request that merged it")
    cl = sub.add_parser("calls", help="fix ID PATH:LINE NOTE: a call (as the morning diff numbers it) that stood on a wrong line gets "
                        "the correction filed beside that line the way remember files one, never into a persona or a rule")
    cl.add_argument("action", choices=["fix"])
    cl.add_argument("id", type=int)
    cl.add_argument("source", help="the wrong line, as path:line (a cite the diff shows)")
    cl.add_argument("note", help="what is true instead")
    jb = sub.add_parser("jobs", help="scheduled jobs from the private jobs.toml: sync (one systemd user timer and "
                        "service per job, the diff, then every next fire time read back) or list (the read-back)")
    jb.add_argument("action", choices=["sync", "list"])
    jb.add_argument("--json", action="store_true")
    sc = sub.add_parser("schedule", help="add a one-shot or a recurring job, remove one, or list them (the room's "
                        "`schedule` tool, from the command line)")
    sc.add_argument("action", choices=["add", "remove", "list"])
    sc.add_argument("name", nargs="?", help="the job's name: lowercase letters, digits, hyphens")
    sc.add_argument("--when", help='add: a date and time ("2026-03-09 09:00", a one-shot) or a systemd calendar line '
                    '("Mon..Fri 09:00", recurring)')
    sc.add_argument("--what", help="add: the worker's brief (kind agent), the shell line (kind script) or the text "
                    "the room is told (kind note)")
    sc.add_argument("--kind", choices=["agent", "script", "note"], default="agent")
    sc.add_argument("--tz", help="add: an IANA zone (default: the room's timezone, else [memory] timezone)")
    sc.add_argument("--room", help="add: the room it lands in (default: the only room)")
    sc.add_argument("--deliver", choices=["announce", "silent"], default="announce")
    sc.add_argument("--cap", type=float, default=0.0, help="add: minutes before the run is stopped")
    sc.add_argument("--account", help="add: agent jobs, the account to run on")
    sc.add_argument("--model", help="add: agent jobs, the worker's model, an alias or a full id on the install's list (default [workers], else the room's)")
    sc.add_argument("--effort", help="add: agent jobs, the worker's thinking level, one of low, medium, high, xhigh, max (default [workers], else the room's)")
    sc.add_argument("--json", action="store_true")
    rb = sub.add_parser("recall-bench", help="the fixed question set against the memory index: top six and top one "
                        "hit rates (exit 5 when a control misses, 2 no question set, 1 failure, 3 timeout, 4 a drop budgets.toml [recall] does not name)")
    rb.add_argument("--questions", help="the recall kind's question file (default: [memory] bench, else <private>/evals/recall-questions"
                    ".json); the episodic and self kinds are episodic-questions.json and self-questions.json beside it")
    rb.add_argument("-k", type=int, default=6, help="results per question (default 6, the recall tool's default)")
    rb.add_argument("--json", action="store_true", help="the run as JSON, per-question ranks included")
    rb.add_argument("--controls", action="store_true", help="the controls alone, as `yerk deploy` holds them: one JSON line, "
                    "nothing written (exit 2 when one misses)")
    bn = sub.add_parser("bench", help="latency: the first visible line, the turn, the hooks, Yerk's tools and the cache hit rate per "
                        "room, from the ledger (read only) and the logs; the table, and <state>/bench/latency-<day>.json to diff")
    bn.add_argument("what", choices=["latency"])
    bn.add_argument("--days", type=float, default=7.0, help="the window, days back from now (default 7)")
    bn.add_argument("--out", help="the file written instead of <state>/bench/latency-<day>.json")
    ev = sub.add_parser("eval", help="taste: the taste judge replayed over the owner's complaints and the replies they accepted, "
                        "with what each turn read; precision, recall, false alarms per verdict, phase 1 beside (the bar printed, not enforced)")
    ev.add_argument("what", choices=["taste"])
    ev.add_argument("--cases", help="the complaints (default: <private>/evals/taste/cases.jsonl)")
    ev.add_argument("--controls", help="the accepted replies (default: <private>/evals/taste/controls.jsonl)")
    ev.add_argument("--rubric", help="the judge's system prompt (default: <private>/evals/taste/rubric.md, else the package's)")
    ev.add_argument("--ledger", help="where each turn's reads come from, read only (default: <state>/ledger.sqlite)")
    ev.add_argument("--out", help="every row and the numbers as JSON (default: scores-taste.json beside the cases)")
    ev.add_argument("--model", default="", help="the judge's model (default: the claude binary's own)")
    ev.add_argument("--effort", default="medium", help="the judge's effort (default: medium)")
    ev.add_argument("--jobs", type=int, default=4, help="judge calls in flight at once")
    ev.add_argument("--cap", type=float, default=20.0, help="seconds a judge call may take (default 20)")
    ev.add_argument("--sample", type=int, default=0, help="a stratified N cases (by verdict) and N controls (by room), not all")
    tx = sub.add_parser("texting", help="sheets --check: the reply list and each person's sheet read, nothing written; one line, "
                        "with its file, for each that does not load, a sheet missing and a file no one names; exit 1 then. "
                        "replay --since DAY: past threads through the writer and the check as of each cut, the plan only without "
                        "--run; counts printed, each cut beside what went in one private file, nothing sent")
    tx.add_argument("action", choices=["sheets", "replay"])
    tx.add_argument("--check", action="store_true", help="sheets: needed")
    tx.add_argument("--since", help="replay: the first day a cut may fall on, YYYY-MM-DD (needed)")
    tx.add_argument("--person", help="replay: one person's key on the reply list, not everyone with a sheet")
    tx.add_argument("--limit", type=int, default=10, help="replay: cuts a person, newest first (default 10)")
    tx.add_argument("--old-log", help="replay: the old agent's log; an answer it quotes is marked auto, not the owner's")
    tx.add_argument("--run", action="store_true", help="replay: make the calls; without it the plan only, nothing called")
    ab = sub.add_parser("abilities", help="every ability and its state on this install, one line each")
    ad = sub.add_parser("add", help="an add-on into this install (a folder, a git URL, or an ability whose entry names its repo): "
                        "fetched, checked against its own size, its package, server, tables and units in, its tests run")
    ad.add_argument("source")
    rm = sub.add_parser("remove", help="an installed add-on out again, each step of yerk add undone")
    rm.add_argument("source", metavar="name")
    for x in (ad, rm):
        x.add_argument("--dry-run", action="store_true", help="say each step it would take, change nothing")
    ex = sub.add_parser("explain", help="one ability (what it does, needs and costs, its free path, its state here) or one setting")
    ex.add_argument("ability")
    lc = sub.add_parser("location", help="day: the day's places (yerkd.toml [location]) as one line in its day log, place by "
                        "place with the first and last fix at each, an earlier run's line replaced; the nightly script job")
    lc.add_argument("action", choices=["day"])
    lc.add_argument("date", nargs="?", default="", help="the day, YYYY-MM-DD (default: today in [memory] timezone)")
    lc.add_argument("--dry-run", action="store_true", help="print the line, write nothing")
    rt = sub.add_parser("retention", help="the daily sweep: logs, transcripts, timing rows, finished workers and rooms' scratch "
                        "past their days ([retention] in yerkd.toml) into the trash, never a work folder holding a file named KEEP, "
                        "each swept room told in its work/RETENTION.md, and the trash past its days deleted")
    rt.add_argument("--dry-run", action="store_true", help="say what would move and how much, move nothing")
    bk = sub.add_parser("backup", help="one dated archive with a manifest: the private folder, the state's ledger, sessions and "
                        "index metadata, the units; into [backup] path (yerkd.toml), the last [backup] keep (7) kept")
    bk.add_argument("--to", help="the folder for it (default: [backup] path, else yerk-backups beside the private folder)")
    bk.add_argument("--with-index", action="store_true", help="the index's vector store too (else yerk-index rebuilds it)")
    rs = sub.add_parser("restore", help="an archive back, read whole against its manifest first: into an empty folder, or "
                        "over the layer with --force after a backup of it")
    rs.add_argument("archive")
    rs.add_argument("--to", help="an empty folder to restore into, as DIR/private, DIR/state and DIR/units")
    rs.add_argument("--dry-run", action="store_true", help="verify and say each step, write nothing")
    rs.add_argument("--force", action="store_true", help="over the layer after a backup of it, and under a running yerkd")
    said = ("the daily check (the private folder's layout, rooms.toml keys, memory classes, tokens, the index, hooks, disk growth, "
            "the rule files' shape) and the crash repair of NUL lines, each file backed up first; once a day it also fetches "
            "the Claude docs from the web ([docs] fetch = false in yerkd.toml turns that off)")
    dc = sub.add_parser("doctor", help=said, description=said)
    dc.add_argument("--dry-run", action="store_true", help="say what would be mended or scrubbed, change nothing")
    dc.add_argument("--scrub", action="store_true", help="only the scrub: each secret-shaped line's value swapped for [scrubbed], "
                    "every line kept, each file copied first into <private>/backups/scrub-<day>/")
    dk = sub.add_parser("docs", help="the Claude docs kept as a memory root every room recalls (refresh, list, show NAME), "
                        "shape: every room's rule files scored against the prompting guide, and own: Yerk AI's own docs "
                        "written into docs/ from the code")
    dk.add_argument("action", choices=["refresh", "list", "show", "shape", "own"])
    dk.add_argument("name", nargs="?", default="", help="show: the page's slug; shape: one room (default: every room)")
    dk.add_argument("--rules", action="store_true", help="shape: print each count's exact rule")
    dk.add_argument("--dry-run", action="store_true", help="own: name each page that differs from the code, write nothing")
    cu = sub.add_parser("curate", help="the night's card curation: the sweep (repeats, superseded and past-date lines, cite "
                        "drift), then the day's facts proposed, knocked down, checked and landed only if the bench holds")
    cu.add_argument("--since", default="", help="each day from this date (YYYY-MM-DD) to yesterday, for a backfill")
    cu.add_argument("--dry-run", action="store_true", help="say what it would change and propose, change nothing")
    cu.add_argument("--rules", action="store_true", help="the rule files alone: merges, cuts, condensations, moves to the topic rules, promotions "
                    "and the (test reads it back, as plain) text with <one>/more-line/<for>.the, check while")
    cu.add_argument("--apply", nargs=2, metavar=("DAY", "N"), help="the owner's word: proposals N[,N...] of DAY written, backed "
                    "back as plain text with one more, line `for`: the check while a second")
    cf = sub.add_parser("config", help="the settings Yerk AI describes, with their values here and plain words")
    cf.add_argument("action", choices=["get"])
    cf.add_argument("key", nargs="?", default="", help="a setting or section (presentation, memory, room); all when left out")
    cf.add_argument("name", nargs="?", default="", help="room: the room's name")
    cf.add_argument("--json", action="store_true")
    cf.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    cf.add_argument("--state", help="room: state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    dp = sub.add_parser("deploy", help="a commit onto the running yerkd: fast-forward only, fast tests, a quiet "
                        "room, the restart, the proof card to the machine room, else the rollback")
    dp.add_argument("ref", nargs="?", help="the commit (default: the daemon's tree's HEAD)")
    dp.add_argument("--no-tests", action="store_true", help="skip the fast tests and the recall check (the card says so)")
    dp.add_argument("--unit", action="store_true", help="also move a systemd-run daemon onto units/yerkd.service")
    dp.add_argument("--timers", action="store_true", help="only install and turn on each timer in units/ not installed yet")
    for x in (ac, st, mo, rb, bn, ev, ab, ex, jb, sc, lc, rt, bk, rs, dc, dk, cu, dp, pc, fl, nd, ad, rm, cl, tx):
        x.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else "
                       + ("the running yerkd's own, from its unit)" if x is dp else "the fake example layer)"))
        x.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    return int({"map": cmd_map, "setup-room": cmd_setup_room, "room": cmd_room, "import": cmd_room, "worker": cmd_worker, "pictures": cmd_pictures, "files": cmd_files,
                "jobs": cmd_jobs, "schedule": cmd_schedule, "accounts": cmd_accounts, "status": cmd_status, "morning": cmd_morning,
                "recall-bench": cmd_recall_bench, "bench": cmd_bench, "eval": cmd_eval, "abilities": cmd_abilities, "explain": cmd_explain, "config": cmd_config,
                "add": cmd_add, "remove": cmd_add,
                "deploy": cmd_deploy, "retention": cmd_retention, "backup": cmd_backup, "restore": cmd_restore, "location": cmd_location,
                "doctor": cmd_doctor,
                "docs": cmd_docs,
                "curate": cmd_curate, "needs": cmd_needs, "calls": cmd_calls, "texting": cmd_texting}[a.cmd](a))


if __name__ == "__main__":
    sys.exit(main())
