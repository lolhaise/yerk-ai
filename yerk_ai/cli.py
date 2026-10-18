"""yerk: the command line."""
from __future__ import annotations

import argparse
import json
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


def cmd_room(a: argparse.Namespace) -> int:
    import urllib.request
    from .config import state_dir
    st = json.loads((state_dir(a.state) / "yerkd.json").read_text())
    req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/control", data=json.dumps({a.action: a.room}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


def cmd_probe(a: argparse.Namespace) -> int:
    if a.name == "resume" and not a.to_config_dir:
        print("resume needs --to-config-dir", file=sys.stderr)
        return 2
    out: dict[str, Any] = getattr(probes, a.name)(a)
    print(json.dumps(out, indent=1, default=str))
    return 0 if out.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="yerk", description="Yerk AI: chat rooms as long-lived Claude Code sessions.")
    p.add_argument("--version", action="version", version=f"yerk {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map", help="print a room's context part by part against budgets.toml")
    m.add_argument("room")
    m.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    s = sub.add_parser("setup-room", help="find or make a Discord room's test webhook (URL to a 0600 file)")
    s.add_argument("room")
    s.add_argument("--private", help="private folder (default: $YERK_PRIVATE)")
    s.add_argument("--channel", help="the channel id, when rooms.toml has none yet")
    s.add_argument("--webhook", default="yerk-test-sender")
    r = sub.add_parser("room", help="act on a running yerkd's room")
    r.add_argument("action", choices=["close"])
    r.add_argument("room")
    r.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    pr = sub.add_parser("probe", help="contract probes against a real Claude Code login")
    pr.add_argument("name", choices=["hello", "fold", "resume", "blocks", "smoke", "measure"])
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
    pr.add_argument("--attach", action="store_true", help="smoke, measure: drive the yerkd already running on --state")
    pr.set_defaults(on_second=None, fold_settings=None)
    a = p.parse_args(argv)
    return int({"map": cmd_map, "probe": cmd_probe, "setup-room": cmd_setup_room, "room": cmd_room}[a.cmd](a))


if __name__ == "__main__":
    sys.exit(main())
