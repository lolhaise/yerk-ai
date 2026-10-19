"""yerkd: the daemon.

Rooms come from the private folder's rooms.toml and accounts.toml; daemon settings (the Discord
token file, owner, test senders, port) from its yerkd.toml. One local HTTP server carries Yerk
AI's MCP tools (/mcp) and the Claude Code hooks (/hook). Rooms with a Discord channel run through
the Discord adapter; `--console ROOM` runs one room on the terminal instead, a line in, a reply out.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import beat, context, deploy, evidence, gates, hooks, jobs, mcp_server, memory, mentions, status, workers
from .accounts import Account, Cooldowns, from_dir, load_accounts
from .config import (EFFORTS, REPO, budgets, flag, load_toml, models, override, private_dir, read_secret,
                     room_id, room_settings, rooms, state_dir, table, write_private)
from .engine import RoomSpec, SessionTable
from .ledger import Ledger
from .lifted.classify import FailureTable
from .lifted.loopguard import LoopGuard
from .meters import Meters
from .room import Channel, Inbound, Room
from . import abilities, words   # STYLE.md: what a room reads

log = logging.getLogger("yerk.daemon")
YERK_TOOLS = ["mcp__yerk__send", "mcp__yerk__hold", "mcp__yerk__recall", "mcp__yerk__read_memory",
              "mcp__yerk__worker", "mcp__yerk__schedule", "mcp__yerk__settings"]
NO_KEY_LINE = words.needs("meaning search", "an OpenAI key", "embeddings", "recall matches words only")


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
        print(json.dumps({"t": round(time.time(), 3), **obj}, ensure_ascii=False), flush=True)   # the room's own words
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
        words.configure(self.conf.get("presentation"))          # what every card and notice shows (STYLE.md)
        self.port = port or int(self.conf.get("http", {}).get("port", 0)) or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.budgets, self.room_confs, self.models = budgets(), rooms(private), models(self.conf)
        self.accounts = [only_account] if only_account else load_accounts(private / "accounts.toml")
        self.ledger = Ledger(state / "ledger.sqlite")
        self.failures = FailureTable.load(table("failures.toml"))
        self.cooldowns = Cooldowns(state / "cooldowns.json")
        self.meters = Meters(state / "meters.json")
        self.memory = memory.settings(private, state, dict(self.conf.get("memory", {})))
        self.daily = memory.DailyLog(self.memory.daily, self.memory.tz,
                                     {n: (room_id(n, c), memory.room_class(c)) for n, c in self.room_confs.items()})
        # day-log lines written before offsets, ids, authors and classes are rewritten once, here, before any
        # channel opens and under the lock capture holds, so no second writer ever races the rewrite (cold
        # read F3); the originals go beside the private layer, or under the state folder for the example one
        backup = (state if private.resolve().is_relative_to(REPO) else private) / "backups" / f"daily-before-{time.strftime('%Y-%m-%d')}"
        moved = memory.migrate_daylogs(self.memory.daily, self.memory.tz, self.daily.rooms, backup)
        if moved["rewritten"]:
            log.info("memory: %d day-log line(s) in %d file(s) rewritten in today's shape; the originals are under %s",
                     moved["rewritten"], moved["files"], backup)
        # the install's secrets for /hook, /control and /mcp (R3): under the state folder for every layer, so
        # no room's Read(/<layer>/**) rule or --add-dir reaches them (cold read F4); yerkd.json names the paths
        sdir = state / "secrets"
        self.secret_file, self.worker_secret_file = sdir / "yerk-http.secret", sdir / "yerk-worker.secret"
        self.secret = read_secret(self.secret_file, create=True)
        self.worker_secret = read_secret(self.worker_secret_file, create=True)
        self.sessions = SessionTable(state / "sessions.json")
        self.names = mentions.Index(self.memory.mentions)      # yerk-index writes it; reloaded when it changes
        self.idle_seconds, self.log_dir = idle_minutes * 60, log_dir
        self.rooms: dict[str, Room] = {}
        self.channels: dict[str, Any] = {}       # room -> its channel adapter
        self.http: Any = None
        self.sends = 0
        # a worker's unit never sees the install's secret or any room's folder (each room's token, settings
        # and override): both exist before the first unit, since a hidden path that is missing fails its start
        (state / "rooms").mkdir(parents=True, exist_ok=True)
        self.workers = workers.Workers(self.ledger, workers.settings(state, dict(self.conf.get("workers", {})),
                                                                     self.budgets),
                                       self.accounts, self.cooldowns, self.rooms, self.channels, self.room_confs,
                                       state=state, hidden=(self.secret_file, state / "rooms"))
        self.jobs = jobs.Jobs(private, state, self.ledger, dict(self.conf.get("jobs", {})), self.rooms, self.channels,
                              self.room_confs, self.conf, self.workers)
        self.gates = gates.settings(self.conf.get("gates"))      # the rule-based gates (DESIGN 8)
        self.workers.gate = {"trash": list(self.gates.trash), "private": str(self.private),   # workers pass them too
                             "external": self.gates.external}
        self.rebuild_running = gates.rebuild_running
        self.rewrites: dict[str, tuple[str, str]] = {}           # tool_use id -> (room, the trash command)
        self.worded: set[str] = set()                            # inbound ids whose approval word was used
        self.later: set[asyncio.Task[Any]] = set()               # a gate's notice, posted beside the hook's answer
        self.timing: list[tuple[str, dict[str, Any]]] = []       # hook and tool timings, written in batches
        self.timing_lock = threading.Lock()
        self.flushing = False
        self.beat = beat.Beat(self.ledger, beat.folder(state, self.memory.root), pids=self.room_pids)
        self.started, self.commit = time.time(), deploy.head(REPO)   # what runs, for yerk deploy's proof
        self.link: Any = None                                         # the channel adapter: is it connected

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
        token = secrets.token_hex(16)                      # this process's credential, bound with its session
        settings = {"permissions": {"allow": [f"Read(/{layer}/**)", *YERK_TOOLS, *conf.get("allow", [])]},
                    "hooks": hooks.settings_for(f"{self.url}/hook", token)}
        mcp = mcp_server.room_config(f"{self.url}/mcp", name, rdir / "mcp.json", token)
        now = room_settings(conf, self.state, name, self.models)    # the room's override over rooms.toml
        extra = ["--effort", now["effort"][0]] if now["effort"][0] else []
        if flag(conf.get("thinking_panel")) and conf.get("privacy", "owner") == "owner":
            extra += ["--thinking-display", "summarized"]      # the panel's text; off: the stream says nothing
        return RoomSpec(room=name, model=now["model"][0], cwd=work, tools=list(conf.get("tools", [])),
                        settings=settings, context_file=ctx, mcp_config=mcp, add_dirs=[layer], extra_args=extra,
                        token=token)

    def reader(self, room: str) -> memory.Reader:
        """Who a memory read is for: an owner room reads everything, any other room its own lines and
        shared files, and a caller that is no room at all reads shared files only (R2)."""
        conf = self.room_confs.get(room)
        if conf is None:
            return memory.Reader(room, "", False)
        return memory.Reader(room, room_id(room, conf), str(conf.get("privacy", "owner")) == "owner")

    def check(self) -> mcp_server.Check:
        return mcp_server.checker(self.secret, self.worker_secret, self.sessions.by_token)

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
            await ch.notify(room, f"{words.NOT_DONE} "
                                  f"{words.NOT_SENT.format(name=Path(file).name, why=words.file_why('outside'))}")
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
        """Off the loop: the index is yerk-index's file, read here, never built here (L47). The first
        recall in a room while yerk-index runs without a provider key tells the room so, once."""
        r = await asyncio.to_thread(memory.recall, self.memory, query, k, 5.0, self.reader(room))
        log.info("recall: %s, %d hit(s)%s", room or "?", len(r.get("hits", [])), "" if r["ok"] else f", {r['status']}")
        if r["ok"] and room in self.rooms and memory.meaning_state(self.memory) == "no-key":
            await self.tell_once(room, "embeddings", NO_KEY_LINE)
        return r

    async def tell_once(self, room: str, what: str, text: str) -> bool:
        """One plain line to the room about `what`, the first time only (a marker file remembers it)."""
        mark = self.state / "rooms" / room / f"told-{what}"
        if mark.exists():
            return False
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
        await self.channels[room].notify(room, text)
        return True

    async def tool_read_memory(self, room: str, path: str, start: int, lines: int) -> dict[str, Any]:
        return await asyncio.to_thread(memory.read, self.memory, path, start, lines, self.reader(room))

    def owner_only(self, room: str, named: str) -> dict[str, Any] | None:
        """A room's own call that would start or read a worker: the owner's rooms only (a worker runs as
        the owner's user), and for the calling room only. None means go on; else the one line."""
        if not room:                                   # the install's own callers keep every form
            return None
        if room not in self.room_confs or str(self.room_confs[room].get("privacy", "owner")) != "owner":
            log.warning("workers: room %s is not an owner room; its worker call was refused", room)
            return {"ok": False, "line": words.WORKERS_OWNER_ONLY}
        if named and named != room:
            return {"ok": False, "line": words.WORKER_OWN_ROOM.format(room=named)}
        return None

    async def tool_worker(self, room: str, req: dict[str, Any]) -> dict[str, Any]:
        """The `worker` tool (rooms only; a worker has no MCP server). `yerk worker` comes through
        /control, which keeps every form, for any room."""
        named = str(req.get("room") or "") if req.get("action") == "start" else ""
        return self.owner_only(room, named) or await self.workers.handle(room, req)

    async def tool_schedule(self, room: str, req: dict[str, Any]) -> dict[str, Any]:
        """The `schedule` tool (add, remove, list); the job runner's hand-offs come through /control.
        A job that is not a script starts a worker, so a room adds one by the worker rule."""
        if req.get("action") == "add" and str(req.get("kind") or "agent") != "script":
            refused = self.owner_only(room, str(req.get("room") or ""))
            if refused:
                return refused
        return await self.jobs.handle(room, req)

    async def tool_hold(self, room: str, wait_for: str, reason: str) -> dict[str, Any]:
        if room not in self.rooms:
            return {"ok": False, "error": "hold is for a room's own session"}
        hid = self.ledger.hold(room, wait_for, reason)
        return {"ok": True, "hold": hid, "next": "End your turn now with one short line; no more tool calls."}

    async def tool_settings(self, room: str, model: str, effort: str) -> dict[str, Any]:
        """The `settings` tool: an owner room's model and thinking level, the daemon's override over
        rooms.toml (which is never written). The running turn ends as it began; then the room's process
        closes and the next message resumes the same session on the new ones (Room.respec)."""
        conf = self.room_confs.get(room)
        if conf is None or str(conf.get("privacy", "owner")) != "owner":
            return {"ok": False, "line": words.OWNER_ONLY}
        want, level = model.strip(), effort.strip().lower()
        full = self.models.get(want.lower()) or (want if want in self.models.values() else want.lower())
        if want and full != "default" and full not in self.models.values():
            listed = ", ".join(f"{a} ({words.model_name(m)})" for a, m in self.models.items())
            return {"ok": False, "line": words.NOT_LISTED.format(model=want, models=listed)}
        if level and level != "default" and level not in EFFORTS:
            return {"ok": False, "line": words.NOT_A_LEVEL.format(effort=level, levels=", ".join([*EFFORTS, "default"]))}
        before, over = room_settings(conf, self.state, room, self.models), override(self.state, room)
        for key, value in (("model", full if want else ""), ("effort", level)):
            if value == "default":
                over.pop(key, None)
            elif value:
                over[key] = value
        if want or level:
            (self.state / "rooms" / room).mkdir(parents=True, exist_ok=True)
            write_private(self.state / "rooms" / room / "override.json", json.dumps(over))
        now = room_settings(conf, self.state, room, self.models)
        if [v for v, _ in now.values()] != [v for v, _ in before.values()] and room in self.rooms:
            self.rooms[room].respec()
        log.info("settings: room %s on %s, effort %s", room, now["model"][0], now["effort"][0] or "default")
        return {"ok": True, "line": (words.NEXT_REPLY if want or level else words.THIS_ROOM).format(
            model=words.model_name(now["model"][0]), effort=now["effort"][0] or "default")}

    # recall before the first word (DESIGN 5)
    def room_pack(self, room: str, left: int = 6000) -> tuple[str, str]:
        r = self.rooms.get(room)
        text, _ = memory.room_pack(self.memory, room, left, exclude=r.current_ids() if r else set(),
                                   rid=room_id(room, self.room_confs.get(room, {})), owner=self.reader(room).owner)
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
        topics = memory.topic_hits(self.memory, room, prompt, r.current_ids() if r else set(), rid=room_id(room, conf),
                                   owner=self.reader(room).owner) if conf.get("topic_recall", True) else []
        return abilities.asked(prompt, self.memory) + names, topics      # "can you do voice": the manifest's answer

    def shadow_stop(self, name: str, room: Room, reply: str, sid: str) -> list[tuple[str, str]]:
        """The evidence rules after a reply: ledgered and logged, never a block (shadow, DESIGN 8)."""
        figure, known = memory.on_file(self.memory)
        found = evidence.check(reply, room.prompts(), room.evidence, self.memory.scope.roots, figure, known)
        found += evidence.check_claims(reply, room.evidence, self.memory.scope.roots)     # decision 25
        new = [f for f in found if f not in room.evidence.logged]
        for rule, detail in new:
            room.evidence.logged.add((rule, detail))
            self.ledger.shadow(t=round(time.time(), 3), room=name, session_id=sid, turn=room.turn_key, rule=rule,
                               detail=detail, reply_chars=len(reply))
            log.info("room %s: shadow %s: %s (not blocked)", name, rule, detail)
        return new

    # the hooks
    def room_of(self, payload: dict[str, Any], auth: dict[str, str] | None = None) -> str | None:
        """The room a hook call belongs to: the binding that holds the call's token; for the install's
        own callers, by the payload's session id, else by its cwd (a room's work folder)."""
        if auth and auth.get("kind") == "room" and auth.get("room") in self.rooms:
            return str(auth["room"])
        name = hooks.room_for_session(self.sessions, str(payload.get("session_id", "")))
        if name in self.rooms:
            return name
        cwd = Path(str(payload.get("cwd") or "/nonexistent"))
        for n in self.rooms:
            work = self.state / "rooms" / n / "work"
            if cwd == work or work in cwd.parents:
                return n
        return None

    def notify_later(self, room: str, text: str) -> None:
        """A gate's one line to the room, posted beside the hook's answer so the hook never waits on it."""
        task = asyncio.get_running_loop().create_task(self.channels[room].notify(room, text))
        self.later.add(task)
        task.add_done_callback(self.later.discard)

    def time_row(self, table: str, row: dict[str, Any]) -> None:
        """Queue a timing row; one flush per batch runs on a thread, beside the hook's answer."""
        with self.timing_lock:
            self.timing.append((table, row))
            if self.flushing:
                return
            self.flushing = True
        task = asyncio.get_running_loop().create_task(asyncio.to_thread(self.flush_times))
        self.later.add(task)
        task.add_done_callback(self.later.discard)

    def flush_times(self) -> None:
        with self.timing_lock:
            rows, self.timing, self.flushing = self.timing, [], False
        try:
            self.ledger.many(rows)
        except Exception:
            log.exception("hook: %d timing rows were not written", len(rows))

    async def hook(self, payload: dict[str, Any], auth: dict[str, str] | None = None) -> dict[str, Any]:
        """Every hook event, timed: the daemon's own time per call goes to the hook_calls ledger, so
        the overhead per turn is a query (Ledger.hook_overhead), never a guess. A call no binding
        explains is answered with the unbound marker and logged, never with a silent {}."""
        t0 = time.monotonic()
        event = str(payload.get("hook_event_name") or "")
        name = self.room_of(payload, auth)
        out: dict[str, Any] = {}
        try:
            if name is None:
                log.warning("hook: %s from session %s (%s caller) matches no room binding; answered unbound",
                            event or "?", str(payload.get("session_id", ""))[:8] or "?", (auth or {}).get("kind") or "no")
                out = dict(hooks.UNBOUND)
                return out
            self.learn_transcript(name, payload)
            out = await self._hook(event, name, payload)
            return out
        finally:
            room = self.rooms.get(name or "")
            hso = out.get("hookSpecificOutput") or {}
            outcome = (out.get("decision") or hso.get("permissionDecision")
                       or ("rewrite" if "updatedInput" in hso else "context" if hso else "pass"))
            self.time_row("hook_calls", {"t": round(time.time(), 3), "room": name or "",
                                         "session_id": str(payload.get("session_id", "")),
                                         "turn": room.turn_key if room else "", "event": event,
                                         "ms": round((time.monotonic() - t0) * 1000, 2), "outcome": str(outcome)})

    def learn_transcript(self, name: str, payload: dict[str, Any]) -> None:
        """The session's transcript path, from the first hook call that carries it, into the room's
        binding: an account hop then resumes by that path (R1), which Claude Code's docs promise.
        Only a path Claude Code would name is taken, an absolute `<session id>.jsonl`, so a room's own
        hook call cannot point its binding at another file (cold read F5); anything else is logged
        and ignored."""
        path = str(payload.get("transcript_path") or "")
        sid = str(payload.get("session_id", ""))
        if not path:
            return
        if not (sid and Path(path).is_absolute() and Path(path).name == f"{sid}.jsonl"):
            log.warning("hook: room %s named a transcript that is not its session's own file; ignored", name)
            return
        row = self.sessions.get(name) or {}
        if row.get("session_id") == sid and row.get("transcript") != path:
            self.sessions.update(name, transcript=path)

    async def _hook(self, event: str, name: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        sid = str(payload.get("session_id", ""))
        room = self.rooms.get(name or "")
        if event == "PreToolUse":
            if not room or not name:
                return {}
            out, line = await asyncio.to_thread(self.pre_tool, name, room, sid, payload)
            if line:
                self.notify_later(name, line)
            return out
        if event in ("PostToolUse", "PostToolUseFailure"):
            if room and name:
                failed = event == "PostToolUseFailure"
                err = str(payload.get("error") or "") if failed else ""
                ms = payload.get("duration_ms")
                self.time_row("tool_times", {"tool_use_id": str(payload.get("tool_use_id") or ""), "t": round(time.time(), 3),
                                             "room": name, "session_id": sid, "turn": room.turn_key,
                                             "tool": str(payload.get("tool_name") or ""),
                                             "ms": int(ms) if isinstance(ms, (int, float)) else None,
                                             "failed": int(failed), "error": err[:500] or None})
                if failed and str(payload.get("tool_use_id") or "") in self.rewrites:
                    line = await asyncio.to_thread(self.trash_failed, name, room, sid, payload)
                    self.notify_later(name, line)
                else:
                    self.rewrites.pop(str(payload.get("tool_use_id") or ""), None)
            return {}
        if event == "Stop":
            active = bool(payload.get("stop_hook_active"))
            reason = room.stop_check(active) if room else None
            if reason:
                log.info("room %s: Stop bounced once: an image was never read", name)
            elif room and name:
                said = payload["last_assistant_message"] if "last_assistant_message" in payload else None
                reason = room.silent_check(active, None if said is None else str(said or ""),
                                           self.ledger.landed(room.turn_key))
                if reason:
                    self.ledger.gate(room=name, session_id=sid, turn=room.turn_key, gate="silent", decision="bounce",
                                     tool="", rule="silent-ending", detail=reason[:300])
                    log.info("room %s: Stop bounced once: the turn ended with nothing visible", name)
            if room and name:
                try:
                    await asyncio.to_thread(self.shadow_stop, name, room, str(payload.get("last_assistant_message")
                                                                              or ""), sid)
                except Exception:
                    log.exception("room %s: the shadow Stop check failed (nothing blocked)", name)
            return {"decision": "block", "reason": reason} if reason else {}
        if event == "SessionStart":
            if payload.get("source") == "compact":
                return await asyncio.to_thread(hooks.session_start, payload, self.state, self.compact_pack, name)
            prev = self.was_reset(name, sid) if name and payload.get("source") in ("startup", "clear") else ""
            if name and prev:                 # told only when the replaced session had answered something
                lost = self.ledger.answered_in(prev)
                self.ledger.gate(room=name, session_id=sid, turn="", gate="session", decision="reset" if lost else
                                 "reset-quiet", tool="", rule=str(payload.get("source")),
                                 detail=f"a new session {sid[:8]} replaced {prev[:8]} ({lost} answered turn(s) in it)")
                if lost:
                    self.notify_later(name, f"{words.NOTHING} {words.RESET}")
            return {}
        if event == "ConfigChange":
            await asyncio.to_thread(self.tamper, name, sid, payload)
            return {}                                     # ledgered, never blocked
        if event != "UserPromptSubmit":
            return {}
        out = await asyncio.to_thread(hooks.user_prompt_submit, payload, self.state, self.room_confs,
                                      lambda r, p: self.rooms[r].meta_for(p), self.budgets, None, self.recall_for, name)
        if out and "hookSpecificOutput" in out and name in self.rooms:
            prompt = str(payload.get("prompt", ""))
            said = self.said_in_turn(name, sid, prompt)
            if said and said.id not in self.worded and gates.is_word(prompt, said.sender, self.gates):
                self.worded.add(said.id)                  # one message, one approval
                sender = said.sender
                row = self.ledger.approve(name, sender, self.gates.window_s)
                if row:
                    self.ledger.gate(room=name, session_id=sid, turn=self.rooms[name].turn_key, gate="approval",
                                     decision="approved", tool="", rule=row["rule"], detail=f"{row['what']}; by {sender}")
                    out["hookSpecificOutput"]["additionalContext"] += (
                        f"\n[approval] {sender} said {self.gates.word}: the held {row['what']} may run once now; "
                        "run the same action again.")
            added = str(out["hookSpecificOutput"]["additionalContext"])
            self.rooms[name].evidence.give(f"{prompt}\n{added}",
                                           recall="[on file, no lookup needed" in added or "[this room, last" in added)
        return out

    def pre_tool(self, name: str, room: Room, sid: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The PreToolUse gates in order: hardline, trash, restart, approval. (answer, line for the room)."""
        tool, ti = str(payload.get("tool_name") or ""), dict(payload.get("tool_input") or {})
        v = None
        if tool == "Bash":
            work = self.state / "rooms" / name / "work"
            v = gates.check_shell(str(ti.get("command") or ""), str(payload.get("cwd") or work), self.gates, root=str(work))
            if v and v.new_input is not None:
                v.new_input = {**ti, **v.new_input}
        if v is None and (what := gates.restart_hit(tool, ti, self.private)):
            why = (words.REBUILDING if self.rebuild_running()
                   else words.IN_PROGRESS if any(r.human_turn() for r in self.rooms.values()) else "")
            if why:
                model, line = gates.refused(what, why)
                v = gates.Verdict("restart", "deny", model, line, f"{what}; {why}")
        key, cwd = "", str(payload.get("cwd") or self.state / "rooms" / name / "work")
        if v is None and (hit := gates.external_hit(tool, ti, name, self.gates)):
            rule, what, call = hit
            key = gates.call_key(tool, ti)
            rel = self.ledger.release(name, rule, key, cwd, self.gates.window_s)
            if rel:
                v = gates.Verdict("approval", "release", detail=f"{what}: {call}; approved by {rel['approved_by']}",
                                  rule=rule)
            else:
                model, line = gates.held(what, call, self.gates.word)
                v = gates.Verdict("approval", "hold", model, line, f"{what}: {call}", rule=rule)
        if v is None:
            return {}, ""
        gid = self.ledger.gate(room=name, session_id=sid, turn=room.turn_key, gate=v.gate, decision=v.decision,
                               tool=tool, rule=v.rule, detail=v.detail)
        if v.decision == "hold":
            self.ledger.hold_add(name, v.rule, v.detail.split(":")[0], gid, key, cwd)
        if v.decision == "rewrite" and v.new_input:
            self.rewrites[str(payload.get("tool_use_id") or "")] = (name, str(v.new_input.get("command")))
        log.info("room %s: gate %s %s: %s", name, v.gate, v.decision, v.detail[:160])
        return v.output(), v.line

    def said_in_turn(self, name: str, sid: str, prompt: str) -> Inbound | None:
        """The message behind a prompt, for the approval word: the hook call must carry the room's bound
        session id (the cwd fallback is not enough), and the prompt must be a message the room itself
        wrote into its running turn. /hook has no secret yet (restart test R3), so this binding is what
        keeps a posted "approve" from counting."""
        if hooks.room_for_session(self.sessions, sid) != name:
            return None
        room = self.rooms[name]
        with room.lock:
            return next((m for m in room.inflight if m.text.strip() == prompt.strip()), None)

    def trash_failed(self, name: str, room: Room, sid: str, payload: dict[str, Any]) -> str:
        """A rewritten delete whose trash command failed: ledgered, and one line for the room."""
        _, cmd = self.rewrites.pop(str(payload.get("tool_use_id") or ""))
        err = str(payload.get("error") or "")
        first = next((x for x in err.splitlines()[1:] if x.strip()), err.splitlines()[0] if err else "it failed")
        self.ledger.gate(room=name, session_id=sid, turn=room.turn_key, gate="trash", decision="failed", tool="Bash",
                         rule="", detail=f"{cmd[:160]}: {err[:200]}")
        target = cmd.split(" -- ", 1)[-1][:24]
        return f"{words.NOT_DONE} {words.TRASH_FAILED.format(name=target, why=first.strip()[:22])}"

    def was_reset(self, name: str, sid: str) -> str:
        """The session a new one replaced in this room ("" if none: a resume keeps its id and is not one)."""
        mark = self.state / "rooms" / name / "last-session"
        prev = mark.read_text().strip() if mark.is_file() else ""
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(sid)
        return prev if prev and prev != sid else ""

    def tamper(self, name: str | None, sid: str, payload: dict[str, Any]) -> None:
        """ConfigChange: who (room and account), what (source and file), when, the hashes before and after
        and the diff's hash. Line hashes are kept between calls, never the file's text."""
        f = Path(str(payload.get("file_path") or ""))
        try:
            data = f.read_bytes() if f.is_file() and f.stat().st_size < 4_000_000 else b""
        except OSError:
            data = b""
        lines = [hashlib.sha1(x).hexdigest() for x in data.splitlines()]
        keep = self.state / "tamper" / (hashlib.sha1(str(f).encode()).hexdigest()[:16] + ".lines")
        before = keep.read_text().split() if keep.is_file() else []
        diff = "\n".join(difflib.unified_diff(before, lines, lineterm="", n=0))
        keep.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(keep, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write("\n".join(lines))
        room = self.rooms.get(name or "")
        acct = room.rp.account.name if room and room.rp else ""
        self.ledger.tamper(t=round(time.time(), 3), who=f"{name or '?'}:{acct or '?'}", room=name or "", session_id=sid,
                           source=str(payload.get("source") or ""), file=str(f),
                           sha_before=hashlib.sha256("\n".join(before).encode()).hexdigest() if before else None,
                           sha_after=hashlib.sha256(data).hexdigest() if data else None,
                           diff_sha=hashlib.sha256(diff.encode()).hexdigest(),
                           diff_lines=sum(1 for x in diff.splitlines() if x[:1] in "+-" and x[:3] not in ("+++", "---")))

    async def control(self, payload: dict[str, Any], auth: dict[str, str] | None = None) -> dict[str, Any]:
        """`yerk room close ROOM`: close the room's process now unless a turn runs; the session stays.
        `yerk room compact ROOM`: the overflow path's /compact, asked for, when no turn runs.
        `yerk worker ...`: start, list, stop, report. `{"job": ...}`: the job runner's fire and result.
        Under the worker secret only `recall` and `read_memory` answer: a worker starts nothing."""
        if auth and auth.get("kind") == "worker" and not (isinstance(payload.get("recall"), dict)
                                                          or isinstance(payload.get("read_memory"), dict)):
            log.warning("control: a worker-secret call asked for %s; refused", ", ".join(sorted(payload)) or "nothing")
            return {"ok": False, "error": "the worker secret reads memory only (recall, read_memory)"}
        if isinstance(payload.get("worker"), dict):
            req = dict(payload["worker"])
            return await self.workers.handle(str(req.get("room") or ""), req)
        if isinstance(payload.get("job"), dict):
            req = dict(payload["job"])
            return await self.jobs.handle(str(req.get("room") or ""), req)
        if isinstance(payload.get("recall"), dict):     # a worker's read-only tools, from its own stdio server
            req = dict(payload["recall"])
            return await self.tool_recall(str(req.get("room", "")), str(req.get("query", "")), int(req.get("k") or 6))
        if isinstance(payload.get("read_memory"), dict):
            req = dict(payload["read_memory"])
            return await self.tool_read_memory(str(req.get("room", "")), str(req.get("path", "")),
                                               int(req.get("start") or 1), int(req.get("lines") or 40))
        if isinstance(payload.get("morning") or payload.get("machine"), dict):   # the morning diff, a deploy card:
            req = dict(payload.get("morning") or payload["machine"])                # the machine room only (L197)
            name = str(req.get("room", ""))
            if self.room_confs.get(name, {}).get("role") != "machine" or name not in self.channels:
                return {"ok": False, "error": f"no machine room {name!r} runs here"}
            ids = await self.channels[name].send_text(name, str(req.get("text", "")), key=str(req.get("key") or "") or None)
            return {"ok": bool(ids), "message_ids": ids}
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

    def room_pids(self) -> dict[str, int]:
        """Each open room's Claude Code pid (the stall dump's /proc lines)."""
        return {n: r.rp.proc.pid for n, r in list(self.rooms.items()) if r.rp is not None and r.rp.proc is not None}

    async def status_now(self, payload: dict[str, Any], auth: dict[str, str] | None = None) -> dict[str, Any]:
        """/status: what runs now, for `yerk status` and any program that asks (status.py)."""
        out = await asyncio.to_thread(status.collect, self.private, self.state, self.accounts, self.cooldowns,
                                      self.ledger, self.rooms)
        return {**out, "started": self.started, "commit": self.commit,
                "connected": bool(getattr(self.link, "connected", False)),
                "rebuilding": await asyncio.to_thread(self.rebuild_running)}   # deploy waits it out, as the gate does

    def write_status(self, mode: str) -> None:
        """state/yerkd.json: what is running, for `yerk room close` and the probes."""
        path = self.state / "yerkd.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "port": self.port, "mode": mode, "started": round(time.time(), 3),
                       "rooms": sorted(self.rooms), "secret_file": str(self.secret_file),      # paths, never values
                       "worker_secret_file": str(self.worker_secret_file)}, f)
        os.replace(tmp, path)

    async def serve_http(self) -> None:
        import uvicorn
        app = mcp_server.app(mcp_server.build(self, self.port, self.hook, self.control, self.status_now), self.check())
        self.http = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        await self.http.serve()

    def listening(self) -> bool:
        """The HTTP port is bound: READY=1 may go out (a start counts only when it can answer)."""
        return self.http is not None and bool(self.http.started)

    async def reap(self, every: float = 60.0) -> None:
        while True:
            await asyncio.sleep(every)
            for room in self.rooms.values():
                await asyncio.to_thread(room.reap_idle)
            try:
                await self.workers.reconcile()         # the backstop for a missed watch event
                await self.jobs.events_open()          # ... and for a watcher event the daemon was not told of
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
        dc.after_recover.append(self.jobs.reattach)      # a script run that ended with no daemon up: delivered late
        self.write_status("discord")
        self.link = dc
        await asyncio.gather(self.serve_http(), self.reap(min(60.0, self.idle_seconds / 2)),
                             self.beat.tick(self.listening), dc.start())

    async def run_console(self, name: str) -> None:
        room = self.add_room(name, ConsoleChannel())
        self.workers.begin(asyncio.get_running_loop())
        await self.workers.reattach()
        await self.jobs.reattach()
        self.write_status("console")
        http = asyncio.create_task(self.serve_http())
        reaper = asyncio.create_task(self.reap(min(60.0, self.idle_seconds / 2)))
        ticker = asyncio.create_task(self.beat.tick(self.listening))
        await asyncio.sleep(0.5)
        self.link = SimpleNamespace(connected=True)    # the terminal is the channel: it is there once read
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
        ticker.cancel()
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
