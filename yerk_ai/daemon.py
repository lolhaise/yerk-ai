"""yerkd: the daemon. Run 1 ships one console room; the chat adapter arrives next."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import context
from .accounts import from_dir
from .config import budgets, private_dir, rooms, state_dir, table
from .engine import RoomProcess, RoomSpec
from .ledger import Ledger
from .lifted.classify import FailureTable


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="yerkd", description="Run one room on the console: a line in, a reply out.")
    p.add_argument("--room", required=True)
    p.add_argument("--config-dir", required=True, help="Claude Code config folder of the account to use")
    p.add_argument("--private")
    p.add_argument("--state")
    a = p.parse_args(argv)
    priv, state, b = private_dir(a.private), state_dir(a.state), budgets()
    room = rooms(priv)[a.room]
    work = state / "rooms" / a.room / "work"
    work.mkdir(parents=True, exist_ok=True)
    ctx = state / "rooms" / a.room / "context.md"
    ctx.write_text(context.appended_file(context.session_start(priv, room, b)))
    spec = RoomSpec(room=a.room, model=str(room["model"]), cwd=work, tools=list(room.get("tools", [])),
                    context_file=ctx)
    last = None
    with RoomProcess(spec, from_dir(a.config_dir), state, ledger=Ledger(state / "ledger.sqlite"),
                     failures=FailureTable.load(table("failures.toml"))) as rp:
        rp.start()
        for line in sys.stdin:
            if not line.strip():
                continue
            now = datetime.now(timezone.utc)
            parts = context.per_message(a.room, room, "owner", now, last, b)
            turn = rp.ask(context.message_text(parts, line.strip()))
            last = now
            print(turn.text if turn.outcome == "ok" else f"[{turn.outcome}: {turn.failure}]", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
