"""yerkd: the daemon.

Rooms come from the private folder's rooms.toml and accounts.toml; daemon settings (the Discord
token file, owner, test senders, port) from its yerkd.toml. One local HTTP server carries Yerk
AI's MCP tools (/mcp) and the Claude Code hooks (/hook). Rooms with a Discord channel run through
the Discord adapter; `--console ROOM` runs one room on the terminal instead, a line in, a reply out.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
import time
from pathlib import Path
from typing import Any

from . import context, hooks, mcp_server
from .accounts import Account, Cooldowns, from_dir, load_accounts
from .config import budgets, load_toml, private_dir, rooms, state_dir, table
from .engine import RoomSpec
from .ledger import Ledger
from .lifted.classify import FailureTable
from .lifted.loopguard import LoopGuard
from .room import Channel, Inbound, Room

log = logging.getLogger("yerk.daemon")
YERK_TOOLS = ["mcp__yerk__send", "mcp__yerk__hold"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ConsoleChannel:
    """A room on the terminal: replies on stdout as JSON lines, the card on stderr."""

    def __init__(self) -> None:
        self.n = 0

    def _out(self, obj: dict[str, Any]) -> list[str]:
        self.n += 1
        print(json.dumps({"t": round(time.time(), 3), **obj}), flush=True)
        return [f"console-{self.n}"]

    def progress(self, room: str, text: str) -> None:
        print(f"[card] {text}", file=sys.stderr, flush=True)

    def reply(self, room: str, text: str, inbound: list[str]) -> list[str]:
        return self._out({"room": room, "reply": text, "inbound": inbound})

    def notice(self, room: str, text: str, inbound: list[str]) -> list[str]:
        return self._out({"room": room, "notice": text, "inbound": inbound})

    async def send_text(self, room: str, text: str) -> list[str]:
        return self._out({"room": room, "send": text})

    async def send_file(self, room: str, path: Path, caption: str = "") -> list[str]:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return self._out({"room": room, "file": str(path), "caption": caption})


class Yerkd:
    def __init__(self, private: Path, state: Path, *, port: int = 0, idle_minutes: float = 30.0,
                 only_account: Account | None = None, log_dir: Path | None = None):
        self.private, self.state = private, state
        self.conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
        self.port = port or int(self.conf.get("http", {}).get("port", 0)) or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.budgets, self.room_confs = budgets(), rooms(private)
        self.accounts = [only_account] if only_account else load_accounts(private / "accounts.toml")
        self.ledger = Ledger(state / "ledger.sqlite")
        self.failures = FailureTable.load(table("failures.toml"))
        self.cooldowns = Cooldowns(state / "cooldowns.json")
        self.idle_seconds, self.log_dir = idle_minutes * 60, log_dir
        self.rooms: dict[str, Room] = {}
        self.channels: dict[str, Any] = {}       # room -> its channel adapter
        self.http: Any = None

    # a room's Claude Code process, rebuilt at every process start
    def spec_for(self, name: str) -> RoomSpec:
        conf = self.room_confs[name]
        rdir = self.state / "rooms" / name
        work = rdir / "work"
        work.mkdir(parents=True, exist_ok=True)
        layer = context.layer_dir(self.private, conf)
        ctx = rdir / "context.md"
        ctx.write_text(context.appended_file(context.session_start(self.private, conf, self.budgets)))
        settings = {"permissions": {"allow": [f"Read(/{layer}/**)", *YERK_TOOLS]},
                    "hooks": hooks.settings_for(f"{self.url}/hook")}
        mcp = mcp_server.room_config(f"{self.url}/mcp", name, rdir / "mcp.json")
        return RoomSpec(room=name, model=str(conf["model"]), cwd=work, tools=list(conf.get("tools", [])),
                        settings=settings, context_file=ctx, mcp_config=mcp, add_dirs=[layer])

    def add_room(self, name: str, channel: Channel) -> Room:
        conf = self.room_confs[name]
        order = conf.get("accounts")
        accts = [a for n in order or [] for a in self.accounts if a.name == n] or self.accounts
        room = Room(name, conf, lambda: self.spec_for(name), accts, channel, self.state, self.ledger,
                    self.failures, self.cooldowns, idle_seconds=self.idle_seconds, log_dir=self.log_dir)
        self.rooms[name], self.channels[name] = room, channel
        return room

    # the MCP tools
    async def tool_send(self, room: str, text: str, file: str, caller: str) -> dict[str, Any]:
        if room not in self.rooms:
            return {"ok": False, "error": f"no room {room!r}; rooms: {', '.join(self.rooms)}"}
        if not text and not file:
            return {"ok": False, "error": "nothing to send: give text, a file, or both"}
        ch = self.channels[room]
        try:
            ids = await (ch.send_file(room, Path(file), text) if file else ch.send_text(room, text))
        except (OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        log.info("send: %s -> %s, %d message(s)", caller or "?", room, len(ids))
        return {"ok": True, "message_ids": ids}

    async def tool_hold(self, room: str, wait_for: str, reason: str) -> dict[str, Any]:
        if room not in self.rooms:
            return {"ok": False, "error": "hold is for a room's own session"}
        hid = self.ledger.hold(room, wait_for, reason)
        return {"ok": True, "hold": hid, "next": "End your turn now with one short line; no more tool calls."}

    # the hooks
    async def hook(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("hook_event_name") != "UserPromptSubmit":
            return {}
        return hooks.user_prompt_submit(payload, self.state, self.room_confs,
                                        lambda r, p: self.rooms[r].meta_for(p), self.budgets)

    async def serve_http(self) -> None:
        import uvicorn
        app = mcp_server.build(self, self.port, self.hook).streamable_http_app()
        self.http = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        await self.http.serve()

    async def reap(self, every: float = 60.0) -> None:
        while True:
            await asyncio.sleep(every)
            for room in self.rooms.values():
                await asyncio.to_thread(room.reap_idle)

    async def run_discord(self) -> None:
        from .channels.discord import DiscordChannel
        d = self.conf.get("discord", {})
        token = (self.private / d["token_file"]).read_text().strip()
        guard = LoopGuard(max_events=int(d.get("webhook_burst", 100)))   # per 5 minutes, then 10 off
        dc = DiscordChannel(token, int(d["owner"]), [int(x) for x in d.get("test_senders", [])], self.ledger,
                            guard=guard)
        for name, conf in self.room_confs.items():
            kind, _, cid = str(conf.get("channel", "")).partition(":")
            if kind == "discord" and cid.isdigit():
                dc.add_room(int(cid), self.add_room(name, dc))
        if not dc.rooms:
            raise SystemExit("no room has a discord:<channel id> in rooms.toml")
        await asyncio.gather(self.serve_http(), self.reap(min(60.0, self.idle_seconds / 2)), dc.start())

    async def run_console(self, name: str) -> None:
        room = self.add_room(name, ConsoleChannel())
        http = asyncio.create_task(self.serve_http())
        reaper = asyncio.create_task(self.reap(min(60.0, self.idle_seconds / 2)))
        await asyncio.sleep(0.5)
        n = 0
        while line := await asyncio.to_thread(sys.stdin.readline):
            if not line.strip():
                continue
            n += 1
            mid = f"console-in-{int(time.time())}-{n}"
            self.ledger.inbound_add(mid, name, "console", "owner", len(line.strip()))
            room.submit(Inbound(mid, "owner", line.strip()))
        while room.busy:
            await asyncio.sleep(0.2)
        if room.rp:
            await asyncio.to_thread(room.rp.close)
        reaper.cancel()
        self.http.should_exit = True
        await http


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="yerkd", description="The Yerk AI daemon.")
    p.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    p.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    p.add_argument("--port", type=int, default=0, help="local port for /mcp and /hook (default: yerkd.toml, else free)")
    p.add_argument("--idle-minutes", type=float, default=30.0, help="close a room's process after this long idle")
    p.add_argument("--console", metavar="ROOM", help="run ROOM on the terminal instead of Discord")
    p.add_argument("--config-dir", help="console: use only this account's Claude Code config folder")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    priv, state = private_dir(a.private), state_dir(a.state)
    y = Yerkd(priv, state, port=a.port, idle_minutes=a.idle_minutes,
              only_account=from_dir(a.config_dir) if a.config_dir else None, log_dir=state / "logs")
    (state / "logs").mkdir(exist_ok=True)
    asyncio.run(y.run_console(a.console) if a.console else y.run_discord())
    return 0


if __name__ == "__main__":
    sys.exit(main())
