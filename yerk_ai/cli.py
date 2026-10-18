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
    pr = sub.add_parser("probe", help="contract probes against a real Claude Code login")
    pr.add_argument("name", choices=["hello", "fold", "resume", "blocks"])
    pr.add_argument("--config-dir", required=True, help="Claude Code config folder of the account to use")
    pr.add_argument("--to-config-dir", help="resume: the second account's config folder")
    pr.add_argument("--model", default="claude-fable-5-1")
    pr.add_argument("--reps", type=int, default=3, help="fold: repeats")
    pr.add_argument("--delay", type=float, default=3.0, help="fold: seconds before the second message")
    pr.add_argument("--trailing-wait", type=float, default=30.0, help="fold: seconds to wait for a trailing turn")
    pr.add_argument("--logs", help="folder for raw event logs (default: <state>/probes)")
    pr.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    pr.set_defaults(on_second=None, fold_settings=None)
    a = p.parse_args(argv)
    return int({"map": cmd_map, "probe": cmd_probe}[a.cmd](a))


if __name__ == "__main__":
    sys.exit(main())
