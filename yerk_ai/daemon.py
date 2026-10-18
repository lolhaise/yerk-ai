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
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import context, evidence, hooks, mcp_server, memory, mentions, workers
from .accounts import Account, Cooldowns, from_dir, load_accounts
from .config import budgets, load_toml, private_dir, rooms, state_dir, table
from .engine import RoomSpec, SessionTable
from .ledger import Ledger
from .lifted.classify import FailureTable
from .lifted.loopguard import LoopGuard
from .meters import Meters
from .room import Channel, Inbound, Room

log = logging.getLogger("yerk.daemon")
YERK_TOOLS = ["mcp__yerk__send", "mcp__yerk__hold", "mcp__yerk__recall", "mcp__yerk__read_memory",
              "mcp__yerk__worker"]
STALL = 0.25            # seconds the event loop may lag before the log says so


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

    def open_card(self, room: str, text: str, kind: str = "turn") -> None:
        self.progress(room, text)

    def progress(self, room: str, text: str) -> None:
        print(f"[card] {text}", file=sys.stderr, flush=True)

    def reply(self, room: str, text: str, inbound: list[str], card: str | None = None,
              key: str | None = None) -> list[str]:
        if card:
            self.progress(room, card)
        return self._out({"room": room, "reply": text, "inbound": inbound, **({"card": card} if card else {})})

    def notice(self, room: str, text: str, inbound: list[str], card: str | None = None,
               key: str | None = None) -> list[str]:
        if card:
            self.progress(room, card)
        return self._out({"room": room, "notice": text, "inbound": inbound})

    async def send_text(self, room: str, text: str, key: str | None = None) -> list[str]:
        return self._out({"room": room, "send": text})

    async def notify(self, room: str, text: str) -> list[str]:
        return self._out({"room": room, "notice": text, "inbound": []})

    async def send_file(self, room: str, path: Path, caption: str = "", key: str | None = None,
                        inbound: str = "") -> list[str]:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return self._out({"room": room, "file": str(path), "caption": caption})

    async def worker_card(self, room: str, key: str, text: str, message_id: str | None = None) -> str | None:
        print(f"[{key}] {text}", file=sys.stderr, flush=True)
        return message_id or f"console-{key}"

    async def worker_settle(self, room: str, key: str, text: str, how: str,
                            message_id: str | None = None) -> str | None:
        return await self.worker_card(room, key, f"{text}\n({how})", message_id)


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
        self.meters = Meters(state / "meters.json")
        self.memory = memory.settings(private, state, dict(self.conf.get("memory", {})))
        self.daily = memory.DailyLog(self.memory.daily, self.memory.tz)
        self.names = mentions.Index(self.memory.mentions)      # yerk-index writes it; reloaded when it changes
        self.idle_seconds, self.log_dir = idle_minutes * 60, log_dir
        self.rooms: dict[str, Room] = {}
        self.channels: dict[str, Any] = {}       # room -> its channel adapter
        self.http: Any = None
        self.sends = 0
        self.workers = workers.Workers(self.ledger, workers.settings(state, dict(self.conf.get("workers", {})),
                                                                     self.budgets),
                                       self.accounts, self.cooldowns, self.rooms, self.channels, self.room_confs)

    # a room's Claude Code process, rebuilt at every process start
    def spec_for(self, name: str) -> RoomSpec:
        conf = self.room_confs[name]
        rdir = self.state / "rooms" / name
        work = rdir / "work"
        work.mkdir(parents=True, exist_ok=True)
        layer = context.layer_dir(self.private, conf)
        ctx = rdir / "context.md"
        parts = context.session_start(self.private, conf, self.budgets, pack=lambda left: self.room_pack(name, left))
        ctx.write_text(context.appended_file(parts))
        hooks.record(self.state, name, "session_start", "", {p.name: p.size for p in parts},
                     next((p.text for p in parts if p.name == "recall_pack"), ""), file=str(ctx))
        settings = {"permissions": {"allow": [f"Read(/{layer}/**)", *YERK_TOOLS]},
                    "hooks": hooks.settings_for(f"{self.url}/hook")}
        mcp = mcp_server.room_config(f"{self.url}/mcp", name, rdir / "mcp.json")
        extra = ["--effort", str(conf["effort"])] if conf.get("effort") else []
        if conf.get("thinking_panel") and conf.get("privacy", "owner") == "owner":
            extra += ["--thinking-display", "summarized"]      # the panel's text; off: the stream says nothing
        return RoomSpec(room=name, model=str(conf["model"]), cwd=work, tools=list(conf.get("tools", [])),
                        settings=settings, context_file=ctx, mcp_config=mcp, add_dirs=[layer], extra_args=extra)

    def add_room(self, name: str, channel: Channel) -> Room:
        conf = self.room_confs[name]
        # the room's list orders the accounts it names; every other account follows in
        # accounts.toml order, so adding or removing an account is one line in accounts.toml
        named = [a for n in conf.get("accounts") or [] for a in self.accounts if a.name == n]
        accts = named + [a for a in self.accounts if a not in named]
        room = Room(name, conf, lambda: self.spec_for(name), accts, channel, self.state, self.ledger,
                    self.failures, self.cooldowns, idle_seconds=self.idle_seconds, log_dir=self.log_dir,
                    meters=self.meters.seen)
        self.rooms[name], self.channels[name] = room, channel
        return room

    # the MCP tools
    def may_send(self, room: str, path: Path) -> bool:
        """A file goes out only from the sending room's own folders (its layer, its work folder),
        never from the private folder's secrets/, whatever the daemon itself can read."""
        p = path.expanduser().resolve()
        roots = [context.layer_dir(self.private, self.room_confs[room]), (self.state / "rooms" / room / "work").resolve()]
        return any(p.is_relative_to(r) for r in roots) and not p.is_relative_to((self.private / "secrets").resolve())

    async def tool_send(self, room: str, text: str, file: str, caller: str) -> dict[str, Any]:
        if room not in self.rooms:
            return {"ok": False, "error": f"no room {room!r}; rooms: {', '.join(self.rooms)}"}
        if not text and not file:
            return {"ok": False, "error": "nothing to send: give text, a file, or both"}
        ch = self.channels[room]
        if file and not self.may_send(caller if caller in self.rooms else room, Path(file)):
            await ch.notify(room, f"Could not send {Path(file).name}: it is outside this room's folders.")
            return {"ok": False, "error": f"{file} is outside this room's folders; only files from them can be sent; "
                                          "the room was told"}
        src = self.rooms.get(caller) or self.rooms[room]
        self.sends += 1
        with src.lock:                        # in a turn: its key and the messages it answers
            turn = src.turn_key if src.busy and src.turn_key else f"x{uuid.uuid4().hex[:11]}"
            inbound = ",".join(m.id for m in src.inflight) if src.busy else ""
        key = f"{turn}-s{self.sends}"                         # and this send's number: unique across restarts
        try:
            ids = await (ch.send_file(room, Path(file), text, key=key, inbound=inbound) if file
                         else ch.send_text(room, text, key=key))
        except (OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        log.info("send: %s -> %s, %d message(s)", caller or "?", room, len(ids))
        return {"ok": True, "message_ids": ids}

    async def tool_recall(self, room: str, query: str, k: int) -> dict[str, Any]:
        """Off the loop: the index is yerk-index's file, read here, never built here (L47)."""
        r = await asyncio.to_thread(memory.recall, self.memory, query, k)
        log.info("recall: %s, %d hit(s)%s", room or "?", len(r.get("hits", [])), "" if r["ok"] else f", {r['status']}")
        return r

    async def tool_read_memory(self, room: str, path: str, start: int, lines: int) -> dict[str, Any]:
        return await asyncio.to_thread(memory.read, self.memory, path, start, lines)

    async def tool_worker(self, room: str, req: dict[str, Any]) -> dict[str, Any]:
        """The `worker` tool (rooms only; a worker has no MCP server) and `yerk worker` (via /control)."""
        return await self.workers.handle(room, req)

    async def tool_hold(self, room: str, wait_for: str, reason: str) -> dict[str, Any]:
        if room not in self.rooms:
            return {"ok": False, "error": "hold is for a room's own session"}
        hid = self.ledger.hold(room, wait_for, reason)
        return {"ok": True, "hold": hid, "next": "End your turn now with one short line; no more tool calls."}

    # recall before the first word (DESIGN 5)
    def room_pack(self, room: str, left: int = 6000) -> tuple[str, str]:
        r = self.rooms.get(room)
        text, _ = memory.room_pack(self.memory, room, left, exclude=r.current_ids() if r else set())
        return text, str(self.memory.daily)

    def compact_pack(self, room: str) -> str:
        """The recall pack as session start builds it, for the re-add after a compaction."""
        conf = self.room_confs[room]
        limit = int(self.budgets["session_start"]["appended"]["recall_pack"])
        text, _ = context.recall_pack(context.layer_dir(self.private, conf), conf, limit,
                                      lambda left: self.room_pack(room, left))
        return text

    def recall_for(self, room: str, prompt: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Names on file (owner-privacy rooms only) and the room's own lines on the topic. Off the loop."""
        conf = self.room_confs.get(room, {})
        names: list[dict[str, Any]] = []
        if conf.get("privacy", "owner") == "owner" and conf.get("mention_recall", True):
            try:
                names = self.names.load().match(prompt)
            except (OSError, ValueError, KeyError) as e:
                log.warning("mention recall skipped: %s", e)
        r = self.rooms.get(room)
        topics = memory.topic_hits(self.memory, room, prompt, r.current_ids() if r else set()) \
            if conf.get("topic_recall", True) else []
        return names, topics

    def shadow_stop(self, name: str, room: Room, reply: str, sid: str) -> list[tuple[str, str]]:
        """The evidence rules after a reply: ledgered and logged, never a block (shadow, DESIGN 8)."""
        figure, known = memory.on_file(self.memory)
        found = evidence.check(reply, room.prompts(), room.evidence, self.memory.scope.roots, figure, known)
        new = [f for f in found if f not in room.evidence.logged]
        for rule, detail in new:
            room.evidence.logged.add((rule, detail))
            self.ledger.shadow(t=round(time.time(), 3), room=name, session_id=sid, turn=room.turn_key, rule=rule,
                               detail=detail, reply_chars=len(reply))
            log.info("room %s: shadow %s: %s (not blocked)", name, rule, detail)
        return new

    # the hooks
    async def hook(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = payload.get("hook_event_name")
        sid = str(payload.get("session_id", ""))
        if event == "Stop":
            name = hooks.room_for_session(SessionTable(self.state / "sessions.json"), sid)
            room = self.rooms.get(name or "")
            reason = room.stop_check(bool(payload.get("stop_hook_active"))) if room else None
            if reason:
                log.info("room %s: Stop bounced once: an image was never read", name)
            if room and name:
                try:
                    await asyncio.to_thread(self.shadow_stop, name, room, str(payload.get("last_assistant_message")
                                                                              or ""), sid)
                except Exception:
                    log.exception("room %s: the shadow Stop check failed (nothing blocked)", name)
            return {"decision": "block", "reason": reason} if reason else {}
        if event == "SessionStart":
            return await asyncio.to_thread(hooks.session_start, payload, self.state, self.compact_pack)
        if event != "UserPromptSubmit":
            return {}
        out = await asyncio.to_thread(hooks.user_prompt_submit, payload, self.state, self.room_confs,
                                      lambda r, p: self.rooms[r].meta_for(p), self.budgets, None, self.recall_for)
        name = hooks.room_for_session(SessionTable(self.state / "sessions.json"), sid)
        if out and name in self.rooms:
            added = str(out["hookSpecificOutput"]["additionalContext"])
            self.rooms[name].evidence.give(f"{payload.get('prompt', '')}\n{added}",
                                           recall="[on file, no lookup needed" in added or "[this room, last" in added)
        return out

    async def control(self, payload: dict[str, Any]) -> dict[str, Any]:
        """`yerk room close ROOM`: close the room's process now unless a turn runs; the session stays.
        `yerk room compact ROOM`: the overflow path's /compact, asked for, when no turn runs.
        `yerk worker ...`: start, list, stop, report."""
        if isinstance(payload.get("worker"), dict):
            req = dict(payload["worker"])
            return await self.workers.handle(str(req.get("room") or ""), req)
        if payload.get("compact"):
            target = self.rooms.get(str(payload["compact"]))
            if target is None:
                return {"ok": False, "error": f"no room {payload['compact']!r}; rooms: {', '.join(self.rooms)}"}
            outcome = await asyncio.to_thread(target.compact_now)
            return {"ok": outcome == "ok", "room": target.name, "compact": outcome}
        room = self.rooms.get(str(payload.get("close", "")))
        if room is None:
            return {"ok": False, "error": f"no room {payload.get('close')!r}; rooms: {', '.join(self.rooms)}"}
        closed = await asyncio.to_thread(room.reap_idle, None, True)
        return {"ok": True, "room": room.name, "closed": closed, "busy": room.busy}

    def write_status(self, mode: str) -> None:
        """state/yerkd.json: what is running, for `yerk room close` and the probes."""
        path = self.state / "yerkd.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "port": self.port, "mode": mode, "started": round(time.time(), 3),
                       "rooms": sorted(self.rooms)}, f)
        os.replace(tmp, path)

    async def serve_http(self) -> None:
        import uvicorn
        app = mcp_server.build(self, self.port, self.hook, self.control).streamable_http_app()
        self.http = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        await self.http.serve()

    async def watch_loop(self, every: float = 0.5) -> None:
        """The stall line: the event loop that serves every room woke late by more than STALL."""
        while True:
            t = time.monotonic()
            await asyncio.sleep(every)
            lag = time.monotonic() - t - every
            if lag > STALL:
                log.warning("stall: the event loop was blocked for %.0f ms", lag * 1000)

    async def reap(self, every: float = 60.0) -> None:
        while True:
            await asyncio.sleep(every)
            for room in self.rooms.values():
                await asyncio.to_thread(room.reap_idle)
            try:
                await self.workers.reconcile()         # the backstop for a missed watch event
            except Exception:
                log.exception("workers: the reconcile pass failed")

    async def run_discord(self) -> None:
        from .channels.discord import DiscordChannel
        d = self.conf.get("discord", {})
        token = (self.private / d["token_file"]).read_text().strip()
        guard = LoopGuard(max_events=int(d.get("webhook_burst", 100)))   # per 5 minutes, then 10 off
        dc = DiscordChannel(token, int(d["owner"]), [int(x) for x in d.get("test_senders", [])], self.ledger,
                            guard=guard, state=self.state, capture=self.daily.capture)
        for name, conf in self.room_confs.items():
            kind, _, cid = str(conf.get("channel", "")).partition(":")
            if kind == "discord" and cid.isdigit():
                dc.add_room(int(cid), self.add_room(name, dc))
        if not dc.rooms:
            raise SystemExit("no room has a discord:<channel id> in rooms.toml")
        self.workers.begin(asyncio.get_running_loop())
        dc.after_recover.append(self.workers.reattach)   # their cards are edited, so after the connect
        self.write_status("discord")
        await asyncio.gather(self.serve_http(), self.reap(min(60.0, self.idle_seconds / 2)), self.watch_loop(),
                             dc.start())

    async def run_console(self, name: str) -> None:
        room = self.add_room(name, ConsoleChannel())
        self.workers.begin(asyncio.get_running_loop())
        await self.workers.reattach()
        self.write_status("console")
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
            self.daily.capture(name, mid, "owner", line.strip())
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
