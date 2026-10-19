"""yerk: the command line."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__, context, probes
from .config import EXAMPLE_PRIVATE, budgets, private_dir, rooms


def cmd_map(a: argparse.Namespace) -> int:
    priv = private_dir(a.private)
    rs = rooms(priv)
    if a.room not in rs:
        print(f"no room {a.room!r} in {priv / 'rooms.toml'}; rooms: {', '.join(rs) or 'none'}", file=sys.stderr)
        return 2
    b = budgets()
    start = context.session_start(priv, rs[a.room], b)
    now = datetime.now(timezone.utc)
    msg = context.per_message(a.room, rs[a.room], "owner", now, now - timedelta(minutes=27), b)
    label = "the fake example layer" if priv == EXAMPLE_PRIVATE else str(priv)
    print(context.render(a.room, label, start, msg, b))
    if rs[a.room].get("watch"):
        print(f"\nWatches {rs[a.room]['watch']}: read as its own channel, never written to; it answers in {rs[a.room].get('channel')}.")
    bad = context.violations(start, msg, b)
    for v in bad:
        print("OVER BUDGET: " + v)
    if a.wire:
        from .accounts import load_accounts
        from .config import state_dir
        sid = json.loads((state_dir(a.state) / "sessions.json").read_text())[a.room]["session_id"]
        dirs = [x.config_dir for x in load_accounts(priv / "accounts.toml")] if (priv / "accounts.toml").is_file() else []
        found = [f for d in dirs for f in sorted(d.glob(f"projects/*/{sid}.jsonl"))]
        if not found:
            print(f"no transcript for session {sid} in {', '.join(map(str, dirs))}", file=sys.stderr)
            return 2
        print()
        print(context.render_wire(a.room, context.wire(found[0]), context.chars(context.appended_file(start)), b))
    return 1 if bad else 0


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
    from .config import state_dir
    from .jobs import post_control
    return post_control(state_dir(state_arg), payload, timeout)


def cmd_room(a: argparse.Namespace) -> int:
    out = _control(a.state, {a.action: a.room}, 900 if a.action == "compact" else 60)
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


def cmd_worker(a: argparse.Namespace) -> int:
    """start and stop go through the running yerkd, which owns the cards; list and report read the
    ledger, so they answer with the daemon down too."""
    from pathlib import Path
    from . import workers
    from .config import state_dir
    from .ledger import Ledger
    if a.action in ("start", "stop"):
        if a.action == "start" and not a.brief:
            print("start needs --brief FILE", file=sys.stderr)
            return 2
        if a.action == "stop" and not a.id:
            print("stop needs the worker's id", file=sys.stderr)
            return 2
        req = {"action": a.action, "id": a.id or "", "room": a.room or "", "tasks": a.task, "cap_minutes": a.cap,
               "account": a.account or "", "model": a.model or "",
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


def cmd_jobs(a: argparse.Namespace) -> int:
    """sync: the jobs file to one timer and service per job (idempotent), the diff, then every next
    fire read back from systemd; list: the read-back alone. Both work with the daemon down."""
    from . import jobs
    from .config import state_dir
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
    from .config import load_toml, state_dir
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
                                a.tz or jobs.default_tz(rs, room, conf), a.deliver, a.cap, a.account or "")
    if a.json:
        print(json.dumps(out, default=str))
    elif not out["ok"]:
        print(out["error"], file=sys.stderr)
    elif a.action == "list":
        print("\n".join(out["lines"]))
    elif a.action == "remove":
        print("\n".join([f"{a.name}: removed"] + out["diff"]))
    else:
        print(f"{out['name']}: {out['schedule']}, {out['kind']} job, room {out['room']}, deliver {out['deliver']}; "
              f"next {out['next_text']}; unit {out['unit']}")
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
    from .accounts import WARN_DAYS, store_token, token_info
    from .ledger import Ledger
    priv, state, accounts, cool = _engine(a)
    by = {x.name: x for x in accounts}
    if a.action in ("cool", "clear", "token", "mint") and a.name not in by:
        print(f"no account {a.name!r} in {priv / 'accounts.toml'}; accounts: {', '.join(by)}", file=sys.stderr)
        return 2
    if a.action == "cool":
        cool.set(a.name, a.kind, time.time() + a.minutes * 60)
        print(f"{a.name} rests ({a.kind}) until {_clock(time.time() + a.minutes * 60)}")
        return 0
    if a.action == "clear":
        cool.clear(a.name)
        print(f"{a.name}: no rest")
        return 0
    if a.action == "mint":
        from .accounts import mint
        from .engine import claude_bin
        status, stored = mint(by[a.name], claude_bin())
        print(f"{a.name}: " + (f"token stored in {by[a.name].token_file} (0600)" if stored else
                               "no token in the output, nothing stored"))
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
        rest = ", ".join(f"rests ({r['kind']}, x{r['repeats']}) until {_clock(r['until'])}" for r in rests) or "ready"
        h = ledger.health_last(x.name)
        hl = "no health call yet" if not h else (
            f"health {'pass' if h['ok'] else 'FAIL'} {_clock(h['at'])}"
            + {1: ", identity ok", 0: ", IDENTITY FAIL", None: ""}[h["identity_ok"]])
        out.append({"name": x.name, "order": x.order, "config_dir": str(x.config_dir), **t, "rests": rests,
                    "health": h})
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
        s = status.ask(state) if status.alive(state) else None
        for line in status.card(s or {**status.collect(priv, state, accounts, cool, Ledger(state / "ledger.sqlite")),
                                      "alive": status.alive(state)}):
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
        print(meter_line(x.name, meters.reading(x)) + (f"; rests until {_clock(rest[0]['until'])}" if rest else ""))
    from . import workers
    try:
        units: set[str] | None = workers.Systemd().running()
    except (OSError, subprocess.SubprocessError):
        units = None                          # no systemd here: the ledger alone, unchecked
    lines = workers.status_lines(Ledger(state / "ledger.sqlite"), units, full=True)
    for line in lines or ["workers: none running"]:
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


def cmd_deploy(a: argparse.Namespace) -> int:
    """`yerk deploy [REF]` (deploy.py): fast-forward, fast tests, a quiet room, the restart, the proof,
    else the rollback; the card to the machine room. --unit: onto units/yerkd.service as well."""
    from . import deploy
    from .config import state_dir
    return deploy.run(private_dir(a.private), state_dir(a.state), a.ref or "", tests=not a.no_tests, unit_file=a.unit)


def cmd_drill(a: argparse.Namespace) -> int:
    """The Claude Code upgrade drill (DESIGN 11, L225): the installed version, the contract probes on
    one account (resume onto itself), a verdict, a line in <state>/drills.jsonl. It upgrades nothing."""
    import time
    from .config import state_dir
    from . import words
    try:
        version = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = ""
    print(f"{words.PLAIN} claude {version or 'not found'}")
    a.to_config_dir, passed = a.config_dir, {}
    for name in ("hello", "fold", "resume"):
        out = getattr(probes, name)(a) if version else {"ok": False}
        passed[name] = bool(out.get("ok"))
        print(f"{words.DONE if passed[name] else words.NOT_DONE} probe {name}: {'passed' if passed[name] else 'failed'}")
    failed = [n for n, ok in passed.items() if not ok]
    print(f"{words.NOT_DONE} verdict: stay, {', '.join(failed)} failed" if failed else
          f"{words.DONE} verdict: {version} keeps the contract")
    state = state_dir(a.state)
    with open(state / "drills.jsonl", "a") as f:
        f.write(json.dumps({"at": round(time.time()), "version": version, "probes": passed, "ok": not failed}) + "\n")
    return 1 if failed else 0


def cmd_probe(a: argparse.Namespace) -> int:
    if a.name != "imessage" and not a.config_dir:
        print(f"{a.name} needs --config-dir", file=sys.stderr)
        return 2
    if a.name == "resume" and not a.to_config_dir:
        print("resume needs --to-config-dir", file=sys.stderr)
        return 2
    out: dict[str, Any] = getattr(probes, a.name)(a)
    print(json.dumps(out, indent=1, default=str))
    return 0 if out.get("ok") else 1


def cmd_recall_bench(a: argparse.Namespace) -> int:
    """The fixed question set against the memory index (L63). Prints ids, ranks and rates, never a
    question's text or a snippet (the questions are about the owner's life); one JSON line per run
    goes to <state>/memory/recall-bench.jsonl, the weekly series."""
    from pathlib import Path

    from . import memory
    from .config import state_dir
    state = state_dir(a.state)
    s = memory.settings(private_dir(a.private), state)
    qfile = Path(a.questions).expanduser() if a.questions else s.bench
    if not qfile.is_file():
        print(f"no question set at {qfile} ([memory] bench in yerkd.toml names it)", file=sys.stderr)
        return 2
    r = memory.bench(s, qfile, a.k)
    if not r["ok"]:
        print(f"recall-bench: the index {'timed out (busy, not down)' if r['status'] == 'timeout' else 'failed'}: "
              f"{r['error']}", file=sys.stderr)
        return 3 if r["status"] == "timeout" else 1
    keep = ("questions", "controls", "k", "top_k", "top_1", "top_k_rate", "top_1_rate", "blind", "kept", "lost")
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **{x: r[x] for x in keep},
           "misses": [x["id"] for x in r["rows"] if not x["rank"]]}
    (state / "memory").mkdir(parents=True, exist_ok=True)
    with open(state / "memory" / "recall-bench.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    if a.json:
        print(json.dumps({**row, "rows": r["rows"]}, indent=1))
    else:
        print(f"top {r['k']}: {r['top_k']}/{r['questions']} ({r['top_k_rate']:.1%})   "
              f"top one: {r['top_1']}/{r['questions']} ({r['top_1_rate']:.1%})   controls: {r['controls']}   "
              f"answer in the snippet: {r['kept']}/{r['top_k']}")
        print("misses: " + (" ".join(row["misses"]) or "none"))
        if r["blind"]:
            print(f"BLIND: control question(s) missed: {', '.join(r['blind'])}", file=sys.stderr)
    return 2 if r["blind"] else 0


def cmd_abilities(a: argparse.Namespace) -> int:
    """Every ability in the manifest, one line each, state first: on, needs <provider>, runs on the
    free path, known but not built."""
    from . import abilities, memory
    from .config import state_dir
    s = memory.settings(private_dir(a.private), state_dir(a.state))
    tab = abilities.manifest()
    for name, ab in sorted(tab.items(), key=lambda kv: kv[1].get("state", "built") == "described"):
        print(abilities.line(name, ab, tab, s))
    print("yerk explain <ability>: what it does, needs and costs")
    return 0


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
    one would be built and why it is not), else one setting; state first, plain words, one state line."""
    from . import abilities, memory
    from .config import state_dir
    from .words import NOT_DONE
    table_ = abilities.manifest()
    ab = table_.get(a.ability)
    if ab is None:
        hit = _pick(_settings(a), a.ability)
        if hit:
            print("\n".join(_setting_lines(hit[0])))
            return 0
        print(f"{NOT_DONE} no ability or setting {a.ability!r}; abilities: {', '.join(table_)}", file=sys.stderr)
        return 2
    s = memory.settings(private_dir(a.private), state_dir(a.state))
    print("\n".join(abilities.explain(a.ability, ab, table_, s)))     # one state line: the first (STYLE 1)
    if a.ability in ("jobs", "watchers"):     # the jobs file's keys, from the code (decision 16)
        from . import jobs
        print("keys of a [jobs.NAME] table in the private jobs.toml:")
        for key, words in jobs.KEYS.items():
            print(f"  {key}: {words}")
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
        print(json.dumps(got) if a.json else "\n".join(f"{r['key']} = {r['value'] or 'unset'} ({r['source']})"
                                                       for r in got))
        return 0
    rows = _settings(a)
    pick = _pick(rows, a.key, sections=True)
    if not pick:
        print(f"no setting {a.key!r}; settings: {', '.join(r['key'] for r in rows)}", file=sys.stderr)
        return 2
    print(json.dumps(pick) if a.json else "\n".join(x for r in pick for x in _setting_lines(r)))
    return 0


def cmd_memory(a: argparse.Namespace) -> int:
    """`yerk memory migrate-daylogs`: the rewrite of day-log lines written before offsets, room ids,
    authors and classes (restart test R5, R2), every file that changes copied to the private layer's
    backups folder first. The daemon runs the same rewrite at its start; by hand it is safe beside a
    running daemon, since the rewrite and the daemon's capture hold one lock (cold read F3)."""
    import time
    from pathlib import Path
    from . import memory
    from .config import load_toml, room_id, state_dir
    from .config import rooms as load_rooms
    priv, state = private_dir(a.private), state_dir(a.state)
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    s = memory.settings(priv, state, dict(conf.get("memory", {})))
    rooms = {n: (room_id(n, c), memory.room_class(c)) for n, c in load_rooms(priv).items()}
    backup = Path(a.backup) if a.backup else priv / "backups" / f"daily-before-{time.strftime('%Y-%m-%d')}"
    print(json.dumps(memory.migrate_daylogs(s.daily, s.tz, rooms, backup)))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="yerk", description="Yerk AI: chat rooms as long-lived Claude Code sessions.")
    p.add_argument("--version", action="version", version=f"yerk {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map", help="print a room's context part by part against budgets.toml")
    m.add_argument("room")
    m.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    m.add_argument("--wire", action="store_true", help="also the sizes as sent, from the room's session transcript")
    m.add_argument("--state", help="--wire: state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    s = sub.add_parser("setup-room", help="find or make a Discord room's test webhook (URL to a 0600 file)")
    s.add_argument("room")
    s.add_argument("--private", help="private folder (default: $YERK_PRIVATE)")
    s.add_argument("--channel", help="the channel id, when rooms.toml has none yet")
    s.add_argument("--webhook", default="yerk-test-sender")
    r = sub.add_parser("room", help="act on a running yerkd's room")
    r.add_argument("action", choices=["close", "compact"])
    r.add_argument("room")
    r.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    ac = sub.add_parser("accounts", help="tokens, rests and health per account; cool, clear, token, mint, health")
    ac.add_argument("action", nargs="?", default="list", choices=["list", "cool", "clear", "token", "mint", "health"])
    ac.add_argument("name", nargs="?", help="cool, clear, token: the account")
    ac.add_argument("minutes", nargs="?", type=float, default=30.0, help="cool: minutes to rest")
    ac.add_argument("--kind", default="manual", help="cool: the rest's kind (default manual)")
    ac.add_argument("--issued", help="token: the issue date, YYYY-MM-DD (default today)")
    ac.add_argument("--model", default="haiku", help="health: the model for the one tiny turn")
    ac.add_argument("--names", nargs="*", default=[], help="health: only these accounts")
    ac.add_argument("--json", action="store_true")
    st = sub.add_parser("status", help="the daemon, its rooms, each account's 5-hour and weekly meters, and the "
                        "running workers (from the ledger, checked against the unit list)")
    st.add_argument("--full", action="store_true", help="the developer detail: pid, unit names, reading ages")
    mo = sub.add_parser("morning", help="the morning diff (payload changes, yesterday's waits, stalls, shadow flags, "
                        "memory and CPU, meters) to the room with role = \"machine\", else a file under the state folder")
    wk = sub.add_parser("worker", help="workers: start (needs a running yerkd), list, stop, report")
    wk.add_argument("action", choices=["start", "list", "stop", "report"])
    wk.add_argument("id", nargs="?", help="stop, report: the worker's id")
    wk.add_argument("--brief", help="start: the brief file")
    wk.add_argument("--room", help="start: the room for its card and relay (default: the only room, else required)")
    wk.add_argument("--task", action="append", default=[], help="start: a TASKS.md row (repeat for each)")
    wk.add_argument("--cap", type=float, default=0.0, help="start: minutes before it is stopped (default [workers], 65)")
    wk.add_argument("--account", help="start: the account to run on (default [workers], else the first not resting)")
    wk.add_argument("--model", help="start: the model (default [workers], else the room's)")
    wk.add_argument("--limit", type=int, default=20, help="list: how many")
    wk.add_argument("--json", action="store_true")
    wk.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    jb = sub.add_parser("jobs", help="scheduled jobs from the private jobs.toml: sync (one systemd user timer and "
                        "service per job, the diff, then every next fire time read back) or list (the read-back)")
    jb.add_argument("action", choices=["sync", "list"])
    jb.add_argument("--json", action="store_true")
    sc = sub.add_parser("schedule", help="add a one-shot or a recurring job, remove one, or list them (the room's "
                        "`schedule` tool, from the command line)")
    sc.add_argument("action", choices=["add", "remove", "list"])
    sc.add_argument("name", nargs="?", help="the job's name: lowercase letters, digits, hyphens")
    sc.add_argument("--when", help='add: a date and time ("2026-03-09 09:00", a one-shot), "every 2h", or a cron '
                    'expression ("0 9 * * 1-5")')
    sc.add_argument("--what", help="add: the worker's brief (kind agent) or the shell line (kind script)")
    sc.add_argument("--kind", choices=["agent", "script"], default="agent")
    sc.add_argument("--tz", help="add: an IANA zone (default: the room's timezone, else [memory] timezone)")
    sc.add_argument("--room", help="add: the room it lands in (default: the only room)")
    sc.add_argument("--deliver", choices=["announce", "silent"], default="announce")
    sc.add_argument("--cap", type=float, default=0.0, help="add: minutes before the run is stopped")
    sc.add_argument("--account", help="add: agent jobs, the account to run on")
    sc.add_argument("--json", action="store_true")
    rb = sub.add_parser("recall-bench", help="the fixed question set against the memory index: top six and top one "
                        "hit rates (exit 2 when a control misses, 1 failure, 3 timeout)")
    rb.add_argument("--questions", help="question file (default: [memory] bench, else <private>/evals/recall-questions.json)")
    rb.add_argument("-k", type=int, default=6, help="results per question (default 6, the recall tool's default)")
    rb.add_argument("--json", action="store_true", help="the run as JSON, per-question ranks included")
    ab = sub.add_parser("abilities", help="every ability and its state on this install, one line each")
    ex = sub.add_parser("explain", help="one ability (what it does, needs and costs, its free path, its state here) "
                        "or one setting")
    ex.add_argument("ability")
    for x in (ac, st, mo, rb, ab, ex, jb, sc):
        x.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
        x.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    mm = sub.add_parser("memory", help="the memory tree's one-time migrations: migrate-daylogs (offsets, room ids, "
                        "authors and classes on every day-log line written before them; files copied to backups/ first)")
    mm.add_argument("action", choices=["migrate-daylogs"])
    mm.add_argument("--backup", help="where the copies go (default: <private>/backups/daily-before-<today>)")
    mm.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    mm.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
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
    dp.add_argument("--no-tests", action="store_true", help="skip the fast tests (the card says so)")
    dp.add_argument("--unit", action="store_true", help="also move a systemd-run daemon onto units/yerkd.service")
    dp.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    dp.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    dr = sub.add_parser("drill", help="the Claude Code upgrade drill: the version, the hello, fold and resume "
                        "probes on one account, a verdict; upgrades nothing")
    dr.add_argument("--config-dir", required=True, help="Claude Code config folder of the account to use")
    dr.add_argument("--model", default="claude-fable-5-1")
    dr.add_argument("--reps", type=int, default=1, help="fold: repeats")
    dr.add_argument("--delay", type=float, default=3.0, help="fold: seconds before the second message")
    dr.add_argument("--trailing-wait", type=float, default=30.0, help="fold: seconds to wait for a trailing turn")
    dr.add_argument("--logs", help="folder for raw event logs (default: <state>/probes)")
    dr.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    dr.set_defaults(on_second=None, fold_settings=None)
    pr = sub.add_parser("probe", help="contract probes against a real Claude Code login; imessage: the inbound "
                        "check against the Mac's bridge, read only")
    pr.add_argument("name", choices=["hello", "fold", "resume", "blocks", "smoke", "measure", "short", "think", "check",
                                     "idle", "imessage"])
    pr.add_argument("--config-dir", help="Claude Code config folder of the account to use (every probe but imessage)")
    pr.add_argument("--rows", type=int, default=20, help="imessage: the newest rows read per room's chat")
    pr.add_argument("--grace", type=float, default=120.0, help="imessage: seconds a new row may still be in flight")
    pr.add_argument("--to-config-dir", help="resume: the second account's config folder")
    pr.add_argument("--model", default="claude-fable-5-1")
    pr.add_argument("--reps", type=int, default=3, help="fold: repeats")
    pr.add_argument("--delay", type=float, default=3.0, help="fold: seconds before the second message")
    pr.add_argument("--trailing-wait", type=float, default=30.0, help="fold: seconds to wait for a trailing turn")
    pr.add_argument("--logs", help="folder for raw event logs (default: <state>/probes)")
    pr.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    pr.add_argument("--room", default="yerk-test", help="smoke: the room to drive")
    pr.add_argument("--private", help="smoke: private folder (default: $YERK_PRIVATE)")
    pr.add_argument("--only-account", action="store_true", help="smoke: run on --config-dir only, no hop")
    pr.add_argument("--via", choices=["console", "discord"], default="console", help="smoke, measure: the channel")
    pr.add_argument("--minutes", type=float, default=10.0, help="idle: minutes with the room's process open")
    pr.add_argument("--alone-minutes", type=float, default=5.0, help="idle: minutes of the daemon alone after the close")
    pr.add_argument("--unit", help="idle: the systemd user unit running yerkd, for its cgroup totals")
    pr.add_argument("--compare-unit", help="idle: another user unit sampled over the same window")
    pr.add_argument("--only", help="blocks: setups to run, joined with |")
    pr.add_argument("--message", help="blocks: the first message (default: the hello line)")
    pr.add_argument("--since", type=float, default=0.0, help="check: messages ledgered since this epoch time")
    pr.add_argument("--attach", action="store_true", help="smoke, measure: drive the yerkd already running on --state")
    pr.set_defaults(on_second=None, fold_settings=None)
    a = p.parse_args(argv)
    if a.cmd == "config":
        return cmd_config(a)
    if a.cmd == "worker":
        return cmd_worker(a)
    if a.cmd == "jobs":
        return cmd_jobs(a)
    if a.cmd == "schedule":
        return cmd_schedule(a)
    return int({"map": cmd_map, "probe": cmd_probe, "setup-room": cmd_setup_room, "room": cmd_room,
                "accounts": cmd_accounts, "status": cmd_status, "morning": cmd_morning, "recall-bench": cmd_recall_bench,
                "abilities": cmd_abilities, "explain": cmd_explain, "memory": cmd_memory, "deploy": cmd_deploy,
                "drill": cmd_drill}[a.cmd](a))


if __name__ == "__main__":
    sys.exit(main())
