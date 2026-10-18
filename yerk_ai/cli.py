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
    """One POST to the running yerkd's /control (its port is in <state>/yerkd.json)."""
    import urllib.request
    from .config import state_dir
    st = json.loads((state_dir(state_arg) / "yerkd.json").read_text())
    req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/control", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return dict(json.loads(r.read()))


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
    """What runs (the daemon and its rooms), each account's 5-hour and weekly meters and the running
    workers: one short line each, state first (STYLE.md). --full: the developer detail (pid, units,
    how old each meter reading is)."""
    import os
    from . import words
    from .meters import Meters, meter_line, meter_short
    priv, state, accounts, cool = _engine(a)
    full = bool(getattr(a, "full", False))
    try:
        st = json.loads((state / "yerkd.json").read_text())
        os.kill(int(st["pid"]), 0)
        print(f"yerkd: running ({st['mode']}), pid {st['pid']}, rooms {', '.join(st['rooms'])}" if full
              else words.line(f"{words.DONE} {words.RUNNING_ROOMS.format(rooms=', '.join(st['rooms']))}"))
    except (OSError, ValueError, KeyError):
        print(words.NOT_RUNNING if full else f"{words.NOT_DONE} {words.NOT_RUNNING}")
    meters = Meters(state / "meters.json")
    for x in accounts:
        rest = cool.show(x.name)
        print(meter_line(x.name, meters.reading(x)) + (f"; rests until {_clock(rest[0]['until'])}" if rest else "")
              if full else meter_short(x.name, meters.reading(x), rest[0]["until"] if rest else None))
    from . import workers
    from .ledger import Ledger
    try:
        units: set[str] | None = workers.Systemd().running()
    except (OSError, subprocess.SubprocessError):
        units = None                          # no systemd here: the ledger alone, unchecked
    lines = workers.status_lines(Ledger(state / "ledger.sqlite"), units, full=full)
    for line in lines or ["workers: none running"]:
        print(line)
    return 0


def cmd_probe(a: argparse.Namespace) -> int:
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
    keep = ("questions", "controls", "k", "top_k", "top_1", "top_k_rate", "top_1_rate", "blind")
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **{x: r[x] for x in keep},
           "misses": [x["id"] for x in r["rows"] if not x["rank"]]}
    (state / "memory").mkdir(parents=True, exist_ok=True)
    with open(state / "memory" / "recall-bench.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    if a.json:
        print(json.dumps({**row, "rows": r["rows"]}, indent=1))
    else:
        print(f"top {r['k']}: {r['top_k']}/{r['questions']} ({r['top_k_rate']:.1%})   "
              f"top one: {r['top_1']}/{r['questions']} ({r['top_1_rate']:.1%})   controls: {r['controls']}")
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
    from . import words
    from .config import load_toml
    priv = private_dir(a.private)
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    return words.described(conf.get("presentation"))


def _setting_lines(r: dict[str, Any]) -> list[str]:
    return [f"{r['key']} = {r['value']} ({'set in yerkd.toml' if r['source'] != 'default' else 'the default'})",
            f"  {r['what']}", f"  choices: {', '.join(r['choices'])}; {r['reloads']}"]


def cmd_explain(a: argparse.Namespace) -> int:
    """One ability from the manifest (what it does, needs and costs, its free path; how a described
    one would be built and why it is not), else one setting; state first, plain words."""
    from . import abilities, memory, words
    from .config import state_dir
    tab = abilities.manifest()
    ab = tab.get(a.ability)
    if ab is None:
        hit = [r for r in _settings(a) if r["key"] in (a.ability, f"presentation.{a.ability}")]
        if hit:
            print("\n".join(_setting_lines(hit[0])))
            return 0
        print(f"{words.NOT_DONE} no ability or setting {a.ability!r}; abilities: {', '.join(tab)}", file=sys.stderr)
        return 2
    s = memory.settings(private_dir(a.private), state_dir(a.state))
    print("\n".join(abilities.explain(a.ability, ab, tab, s)))
    return 0


def cmd_config(a: argparse.Namespace) -> int:
    """`yerk config get [KEY]`: the settings Yerk AI describes in code (decisions 16 and 27), each with
    its value here, where it came from, its choices and its plain words. Only these are printed."""
    rows = _settings(a)
    want = a.key.removeprefix("presentation").lstrip(".")
    pick = [r for r in rows if not want or r["key"] == f"presentation.{want}"]
    if not pick:
        print(f"no setting {a.key!r}; settings: {', '.join(r['key'] for r in rows)}", file=sys.stderr)
        return 2
    print(json.dumps(pick) if a.json else "\n".join(x for r in pick for x in _setting_lines(r)))
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
    rb = sub.add_parser("recall-bench", help="the fixed question set against the memory index: top six and top one "
                        "hit rates (exit 2 when a control misses, 1 failure, 3 timeout)")
    rb.add_argument("--questions", help="question file (default: [memory] bench, else <private>/evals/recall-questions.json)")
    rb.add_argument("-k", type=int, default=6, help="results per question (default 6, the recall tool's default)")
    rb.add_argument("--json", action="store_true", help="the run as JSON, per-question ranks included")
    ab = sub.add_parser("abilities", help="every ability and its state on this install, one line each")
    ex = sub.add_parser("explain", help="one ability (what it does, needs and costs, its free path, its state here) "
                        "or one setting")
    ex.add_argument("ability")
    cf = sub.add_parser("config", help="the settings Yerk AI describes, with their values here and plain words")
    cf.add_argument("action", choices=["get"])
    cf.add_argument("key", nargs="?", default="presentation", help="a setting, or presentation for all of them")
    cf.add_argument("--json", action="store_true")
    cf.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    for x in (ac, st, rb, ab, ex):
        x.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
        x.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    pr = sub.add_parser("probe", help="contract probes against a real Claude Code login")
    pr.add_argument("name", choices=["hello", "fold", "resume", "blocks", "smoke", "measure", "short", "think", "check",
                                     "idle"])
    pr.add_argument("--config-dir", required=True, help="Claude Code config folder of the account to use")
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
    return int({"map": cmd_map, "probe": cmd_probe, "setup-room": cmd_setup_room, "room": cmd_room,
                "accounts": cmd_accounts, "status": cmd_status, "recall-bench": cmd_recall_bench,
                "abilities": cmd_abilities, "explain": cmd_explain}[a.cmd](a))


if __name__ == "__main__":
    sys.exit(main())
