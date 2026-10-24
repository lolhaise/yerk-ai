"""yerkd: the daemon.

Rooms come from the private folder's rooms.toml and accounts.toml; daemon settings (the Discord
token file, owner, test senders, port) from its yerkd.toml. One local HTTP server carries Yerk
AI's MCP tools (/mcp) and the Claude Code hooks (/hook). Rooms with a Discord channel run through
the Discord adapter.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import fcntl
import hashlib
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from . import (beat, context, deploy, doctor, evidence, gates, hooks, jobs, location, mcp_server, memory, mentions, needs, pictures, status,
               taste, transcribe, voice_note, workers)
from .accounts import Account, Cooldowns, from_dir, load_accounts, pick
from .config import (EFFORTS, REPO, SHELLS, account_and_model, account_named, assistants, budgets, discord_id, flag, load_toml, models,
                     override, people, pinned, private_dir, read_secret, room_id, room_settings, rooms, state_dir, table, trusted, write_private)
from .engine import RoomSpec, SessionTable
from .ledger import Ledger
from .lifted.classify import FailureTable
from .lifted.loopguard import LoopGuard
from .lifted.systemd_notify import notify
from .meters import Meters
from .room import Channel, Inbound, Room
from . import abilities, words   # STYLE.md: what a room reads

log = logging.getLogger("yerk.daemon")
YERK_TOOLS = ["mcp__yerk__send", "mcp__yerk__hold", "mcp__yerk__recall", "mcp__yerk__read_memory",
              "mcp__yerk__remember", "mcp__yerk__worker", "mcp__yerk__schedule", "mcp__yerk__settings", "mcp__yerk__transcribe",
              "mcp__yerk__voice_note", "mcp__yerk__seen", "mcp__yerk__need"]
# A shell room's sandbox when rooms.toml gives it no [rooms.NAME.sandbox] table: unsandboxed, a Read deny stops `cat` and
# the Read tool, but a script, grep or redirect read every secret (Claude Code 2.1.304, PR 66's cold read, 2026-10-21)
SANDBOX = {"enabled": True, "autoAllowBashIfSandboxed": True}
# A trusted room's (shell = "trusted"): no command retried outside it, no start without it (Claude Code would run unsandboxed)
WALL = {**SANDBOX, "allowUnsandboxedCommands": False, "failIfUnavailable": True}
PAST_HITS = 6          # a question about the past: the recall tool's own six hits (its default), cut to the prompt hook's part
NO_KEY_LINE = words.needs("meaning search", "an OpenAI key", "embeddings", "recall matches words only")


def around(root: Path, own: list[Path]) -> list[Path]:
    """`root` whole when it holds none of `own`, else each of its entries the same way (a link by its target); nothing
    that is one of `own`, inside one or holding one (Claude Code's sandbox would deny the room its own work folder)."""
    if any(root.is_relative_to(o) for o in own):
        return []
    if not any(o.is_relative_to(root) for o in own):
        return [root]
    try:
        kids = sorted(root.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for c in kids:
        t = c.resolve()
        if not c.is_symlink():
            out += around(c, own)
        elif not any(o.is_relative_to(t) or t.is_relative_to(o) for o in own):
            out.append(t)
    return out


def site_packages() -> list[Path]:
    """The owner's Python user site-packages (the libraries installed with pip --user, python-docx among them): a
    trusted room's shell reads them, since under blockReadsOutsideWorkingDirectories it reads nothing else in the home."""
    home = Path.home()
    return sorted({p.resolve() for g in (".local/lib/python3*/site-packages", "Library/Python/3*/lib/python/site-packages")
                   for p in home.glob(g) if p.is_dir()})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Yerkd:
    def __init__(self, private: Path, state: Path, *, port: int = 0, idle_minutes: float = 30.0,
                 only_account: Account | None = None, log_dir: Path | None = None):
        self.private, self.state = private, state
        self.conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
        words.configure(self.conf.get("presentation"))          # what every card and notice shows (STYLE.md)
        self.port = port or int(self.conf.get("http", {}).get("port", 0)) or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.budgets, self.room_confs, self.models = budgets(), rooms(private), models(self.conf)
        for n, c in self.room_confs.items():               # one string would be read as one folder per letter
            for key in (k for k in ("read", "also") if not isinstance(c.get(k, []), list)):
                raise SystemExit(f'room {n}: {key} must be a list of folders, like {key} = ["~/notes"], not {c[key]!r}')
        self.read_dirs = {n: [(state / "rooms" / n / "work" / Path(x).expanduser()).resolve() for x in c.get("read", [])]
                          for n, c in self.room_confs.items()}   # from the work folder, as Claude Code lands one
        for n, p in ((n, p) for n, ps in self.read_dirs.items() for p in ps):
            if any(q.is_relative_to(p) or p.is_relative_to(q) for q in (private.resolve(), state.resolve())):   # F4
                raise SystemExit(f"room {n}: read {p} reaches Yerk's own folders; name one outside {private} and {state}")
        for n, m in ((n, c.get("mcp", {})) for n, c in self.room_confs.items()):   # never the values: an env may hold a key
            if not isinstance(m, dict) or not all(isinstance(v, dict) for v in m.values()):
                raise SystemExit(f"room {n}: mcp must be a table of servers, like [rooms.{n}.mcp.web], each one a table")
            for s in (s for s in m if s.lower().startswith("yerk") or "__" in s):
                raise SystemExit(f"room {n}: mcp server {s} could pass for Yerk's own tools; name it without yerk at the start or __")
        self.accounts =[only_account] if only_account else load_accounts(private / "accounts.toml")
        self.ledger, self.failures = Ledger(state / "ledger.sqlite"), FailureTable.load(table("failures.toml"))
        self.cooldowns = Cooldowns(state / "cooldowns.json")
        self.meters = Meters(state / "meters.json")
        self.memory = memory.settings(private, state, dict(self.conf.get("memory", {})))
        self.location = location.settings(self.conf.get("location"), private)     # None: no room gets a place part
        me = str(self.conf.get("import", {}).get("me") or "")       # the name the owner's own words go by (decision 22: never in code)
        self.owner_names = (me,) if me and me != "me" else ()
        self.scoped = {n: self.memory.only(private, n, c["also"]) for n, c in self.room_confs.items() if "also" in c}
        context.walls(private, state, self.memory.scope.roots, self.room_confs)
        for n in (n for n, c in self.room_confs.items() if trusted(c)):
            self.walled(n)                         # a trusted room's wall that cannot be built stops the start here
        self.daily = memory.DailyLog(self.memory.daily, self.memory.tz,
                                     {n: (room_id(n, c), memory.room_class(c)) for n, c in self.room_confs.items()})
        # crash repair before anything reads a day log, assembled log or bound transcript (doctor.py): a hard power-off
        # zeroes the line a file was writing; a room the crash left with a message waiting is left for the daily run
        log.info("%s", doctor.start(private, state, self.memory, {str(r["room"]) for r in self.ledger.inbound_open()}))
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
        self.pastes: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {}   # room -> (today, its earlier days' blocks)
        self.paste_lock = threading.Lock()
        # a worker's unit never sees the install's secret or any room's folder (each room's token, settings
        # and override): both exist before the first unit, since a hidden path that is missing fails its start
        (state / "rooms").mkdir(parents=True, exist_ok=True)
        self.workers = workers.Workers(self.ledger, workers.settings(state, dict(self.conf.get("workers", {})),
                                                                     self.budgets),
                                       self.accounts, self.cooldowns, self.rooms, self.channels, self.room_confs,
                                       state=state, hidden=(self.secret_file, state / "rooms"))
        self.jobs = jobs.Jobs(private, state, self.ledger, dict(self.conf.get("jobs", {})), self.rooms, self.channels,
                              self.room_confs, self.conf, self.workers)
        self.workers.hooks.append(self.pictures_done)            # a pictures backfill batch: its line, then the next batch
        self.workers.hooks.append(self.need_built)               # a need's builder: its state and its line (needs.py)
        self.gates = gates.settings(self.conf.get("gates"))      # the rule-based gates (DESIGN 8)
        self.workers.gate = {"trash": list(self.gates.trash), "private": str(self.private),   # workers pass them too
                             "external": self.gates.external}
        self.rebuild_running = gates.rebuild_running
        self.rewrites: dict[str, tuple[str, str]] = {}           # tool_use id -> (room, the trash command)
        self.worded: set[str] = set()                            # inbound ids whose approval word was used
        self.later: set[asyncio.Task[Any]] = set()               # a gate's notice, posted beside the hook's answer
        self.held_files: dict[str, Path] = {}                    # room -> a held call's exact text too long to show
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
        (work := rdir / "work").mkdir(parents=True, exist_ok=True)
        layer = context.layer_dir(self.private, conf)
        self.rules_news(name)          # first, the look a new session starts with: a rule filed meanwhile is told twice, never lost
        parts = context.session_start(self.private, conf, self.budgets, lambda left: self.room_pack(name, left), self.memory.root / memory.RULES)
        self.cut_said(name, parts)
        ctx = write_private(rdir / "context.md", context.appended_file(parts))      # the owner's lines: 0600, as every private file
        hooks.record(self.state, name, "session_start", "", {p.name: p.size for p in parts},
                     next((p.text for p in parts if p.name == "recall_pack"), ""), file=str(ctx))
        token = secrets.token_hex(16)                      # this process's credential, bound with its session
        header = write_private(rdir / "hook-header", f"{mcp_server.HEADER}: {token}\n")    # curl reads it: never in an argv
        settings: dict[str, Any] = {"permissions": {"allow": [f"Read(/{layer}/**)", *YERK_TOOLS, *conf.get("allow", [])], "deny": self.never_read()},
                                    "hooks": hooks.settings_for(f"{self.url}/hook", header)}
        shell = conf.get("sandbox", dict(SANDBOX) if SHELLS & set(conf.get("tools") or []) else None)   # Claude Code's own
        dirs = [layer, *self.read_dirs.get(name, [])]                       # `read`, as F4 judged it
        if trusted(conf):                    # the daemon's wall; Write and Edit on the room's own folders; its outbox read only
            shell = self.wall(name)
            settings["permissions"]["allow"] += [f"Edit(/{p}/**)" for p in (layer, work.resolve())]
            (outbox := rdir / "outbox").mkdir(parents=True, exist_ok=True)
            dirs.append(outbox.resolve())
        settings.update({"sandbox": shell} if shell is not None else {})    # shell sandbox: a shell room's is on unless its table says
        settings.update({"autoCompactWindow": w} if (w := self.window(name)) else {})
        settings["permissions"].update({} if self.owner(name) else {"blockReadsOutsideWorkingDirectories": True})   # file tools, every mode
        now = room_settings(conf, self.state, name, self.models)    # the room's override over rooms.toml
        extra = ["--effort", now["effort"][0]] if now["effort"][0] else []
        if flag(conf.get("thinking_panel")) and self.owner(name):
            extra += ["--thinking-display", "summarized"]      # the panel's text; off: the stream says nothing
        return RoomSpec(room=name, model=now["model"][0], cwd=work, tools=list(conf.get("tools", [])), settings=settings,
                        context_file=ctx, mcp_config=mcp_server.room_config(f"{self.url}/mcp", name, rdir / "mcp.json", token, conf.get("mcp")),
                        extra_args=extra, token=token, add_dirs=dirs)

    def cut_said(self, name: str, parts: list[context.Part]) -> None:
        """Each part a session start sent with lines of its own dropped to fit its key (whole lines, never a blank, a `[from`
        line or the pack's own oldest items, which it leaves out by construction): one machine-room notice, the part and how many,
        then the key and the room, once a day per part and its file, said by the first room to start (cut-said.json in the state
        folder), so no budget cuts in silence (claim 8); `yerk doctor` names every room, red until the part fits. Never stops
        the start."""
        mark, today = self.state / "cut-said.json", time.strftime("%Y-%m-%d")
        try:
            said = dict(json.loads(mark.read_text()))
        except (OSError, ValueError, TypeError):
            said = {}
        if not (new := [p for p in parts if p.cut and said.get(f"{p.name} {p.source}") != today]):
            return
        write_private(mark, json.dumps({**said, **{f"{p.name} {p.source}": today for p in new}}))
        for p in new:
            self.to_machine(f"{words.WORKING} " + words.PART_CUT.format(n=len(p.cut), s="s" * (len(p.cut) != 1), part=p.name, room=name,
                                                                       cap=p.budget))

    def to_machine(self, text: str) -> None:
        """One line to the machine room (rooms.toml role = "machine") from a room's thread, where a process start runs: on the
        daemon's loop while it runs, else here (a test); logged, never raised, when it cannot go."""
        machine = next((n for n, c in self.room_confs.items() if c.get("role") == "machine" and n in self.channels), None)
        if machine is None:
            log.warning("no machine room runs here; not said there: %s", text)
            return

        async def post(to: str) -> None:
            try:
                await self.channels[to].notify(to, text)
            except Exception:
                log.exception("not said in %s: %s", to, text)
        if (loop := self.workers.loop) is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(post(machine), loop)
        else:
            asyncio.run(post(machine))

    def walled(self, name: str) -> list[Path]:
        """What a trusted room's shell never reads or writes, from what the daemon knows, at each process start: every entry
        of the private and state folders but the room's own (the other layers, the memory folder, rooms.toml, imports and
        backups; every other room's folder, the ledger, the logs, the index), every memory root, every other room's layer
        and `read` folders, projects/ under every account's config folder (each room's transcripts), and each folder of the
        owner's home that holds a memory root, a layer or a `read` folder (on the box, its workspace and its staging).
        One of those inside the room's own folders stops the start with one line."""
        conf, home = self.room_confs[name], Path.home().resolve()
        own = [context.layer_dir(self.private, conf), (self.state / "rooms" / name).resolve()]
        inbox = (self.state / "rooms" / name / "work" / "inbox").resolve()     # its own uploads, a memory root of its own
        theirs = [*(r for r in self.memory.scope.roots if r != inbox), *(context.layer_dir(self.private, c) for m, c in self.room_confs.items() if m != name),
                  *(p for m, ps in self.read_dirs.items() if m != name for p in ps)]
        known = [self.private.resolve(), self.state.resolve(), *theirs, *((self.state / "rooms" / m).resolve() for m in self.room_confs if m != name),
                 *(a.config_dir.expanduser().resolve() / "projects" for a in self.accounts)]
        if (inside := next((p for p in known if any(p.is_relative_to(o) for o in own)), None)) is not None:
            raise SystemExit(f'room {name}: shell = "trusted" cannot wall {inside}: it is inside the room\'s own folders')
        tops = sorted({(home / p.relative_to(home).parts[0]).resolve() for p in theirs if p.is_relative_to(home) and p != home})
        out: list[Path] = []
        for p in (q for root in (*known, *tops) for q in around(root, own)):
            if not any(p.is_relative_to(q) for q in out):
                out = [q for q in out if not q.is_relative_to(p)] + [p]
        return out

    def wall(self, name: str) -> dict[str, Any]:
        """A trusted room's sandbox: the daemon's (WALL), its rooms.toml table adding denies and the network allowlist only
        (config.unwalled refused the rest). The walled folders are denied the shell for reads and writes, the room's outbox
        for writes (what went out stays as it went), and the owner's Python libraries re-opened to it read only."""
        own = dict(self.room_confs[name].get("sandbox") or {})
        fs = {k: [str(x) for x in v] for k, v in dict(own.get("filesystem") or {}).items()}
        walled = [str(p) for p in self.walled(name)]
        outbox = str((self.state / "rooms" / name / "outbox").resolve())
        fs["denyRead"] = list(dict.fromkeys([*fs.get("denyRead", []), *walled]))
        fs["denyWrite"] = list(dict.fromkeys([*fs.get("denyWrite", []), *walled, outbox]))
        fs["allowRead"] = [str(p) for p in site_packages()]
        return {**own, **WALL, "autoAllowBashIfSandboxed": own.get("autoAllowBashIfSandboxed", True), "filesystem": fs}

    def window(self, name: str) -> int:
        """The tokens a room's session keeps before Claude Code compacts it: rooms.toml `compact_window`, else budgets.toml
        [session]'s; 0 leaves it to the model. Clamped to 100,000 to 1,000,000: Claude Code drops a value outside, silently."""
        w = int(self.room_confs[name].get("compact_window", self.budgets.get("session", {}).get("compact_window", 0)) or 0)
        return min(max(w, 100000), 1000000) if w else 0

    def secrets_at(self) -> tuple[list[Path], list[Path]]:
        """(folders, files) no room reads or sends: both secrets folders; every room's token files, each account's token
        file and the secret files yerkd.toml names, wherever they are."""
        files = [self.state / "rooms" / n / f for n in self.room_confs for f in ("hook-header", "mcp.json")]
        files += [a.token_file for a in self.accounts if a.token_file] + [self.private / v for t in self.conf.values() if isinstance(t, dict)
                                                                           for k, v in t.items() if k.endswith(("_file", "_key")) and isinstance(v, str)]
        return [(d / "secrets").resolve() for d in (self.private, self.state)], [f.expanduser().resolve() for f in files]

    def never_read(self) -> list[str]:
        """What no room reads, by its Read tool or its shell (Claude Code puts a Read deny in the sandbox too; deny beats the
        layer's allow): the secrets, and each account config folder's own dot files (its login). Proven on Claude Code
        2.1.304 (scratch probe, 2026-10-21)."""
        folders, files = self.secrets_at()
        return ([f"Read(/{d}/**)" for d in folders] + [f"Read(/{f})" for f in files]
                + [f"Read(/{a.config_dir.expanduser().resolve()}/.*)" for a in self.accounts])

    def owner(self, room: str) -> bool:
        """An owner room (rooms.toml privacy, "owner" when unset); a name that is no room is not one."""
        return room in self.room_confs and str(self.room_confs[room].get("privacy", "owner")) == "owner"

    def taste_heard(self, room: str, text: str) -> None:
        """The owner's message in a room's own channel: a verdict on the room's last reply joins the layer's taste.jsonl
        when the room is an owner room whose layer keeps one (taste.py)."""
        if (conf := self.room_confs.get(room)) is not None and (path := taste.kept(self.private, conf)) is not None:
            taste.record(path, room, str(conf.get("channel", "")).partition(":")[2], text, self.ledger.last_reply(room),
                         ZoneInfo(str(conf.get("timezone", "UTC"))))

    def taste_for(self, room: str) -> str:
        """The taste part of an owner room whose layer keeps taste.jsonl, else ""."""
        if (conf := self.room_confs.get(room)) is None or (path := taste.kept(self.private, conf)) is None:
            return ""
        try:
            return taste.render(taste.load(path), room, int(self.budgets["per_message"]["on_hit"]["taste"]))
        except OSError as e:                      # the message goes on without it
            log.warning("room %s: no taste part, %s could not be read: %s", room, path, e)
            return ""

    def place_for(self, room: str, now: datetime) -> str:
        """The place part of an owner room's message (location.py), else ""; a name that is no room gets none."""
        return location.part(self.location, conf, now) if (conf := self.room_confs.get(room)) is not None else ""

    def reader(self, room: str) -> memory.Reader:
        """Who a memory read is for: an owner room reads everything, any other room its own lines and
        shared files, a caller that is no room at all shared files only (R2), and a sealed room its own lines in Yerk's tree."""
        return memory.Reader(room, room_id(room, self.room_confs[room]) if room in self.room_confs else "",
                             self.owner(room), flag(self.room_confs.get(room, {}).get("sealed")))

    def check(self) -> mcp_server.Check:
        return mcp_server.checker(self.secret, self.worker_secret, self.sessions.by_token)

    def add_room(self, name: str, channel: Channel) -> Room:
        conf = self.room_confs[name]
        # the room's list orders the accounts it names; every other account follows in
        # accounts.toml order, so adding or removing an account is one line in accounts.toml
        named = [a for n in conf.get("accounts") or [] for a in self.accounts if a.name == n]
        accts = named + [a for a in self.accounts if a not in named]
        s = {**self.budgets.get("session", {}), **conf}          # the room's idle compaction keys over the budget's
        room = Room(name, conf, lambda: self.spec_for(name), accts, channel, self.state, self.ledger,
                    self.failures, self.cooldowns, idle_seconds=self.idle_seconds, log_dir=self.log_dir, meters=self.meters,
                    compact=(self.window(name), float(s.get("idle_compact_at", 0)), 60 * float(s.get("idle_compact_after", 0))))
        self.rooms[name], self.channels[name] = room, channel
        return room

    # the MCP tools
    def may_send(self, room: str, path: Path) -> bool:
        """A file goes out only from the sending room's own folders (its layer, its work folder), never a secret
        (secrets_at: a token file kept in the layer too), whatever the daemon itself can read."""
        p, (folders, files) = path.expanduser().resolve(), self.secrets_at()
        roots = [context.layer_dir(self.private, self.room_confs[room]), (self.state / "rooms" / room / "work").resolve()]
        return any(p.is_relative_to(r) for r in roots) and not any(p.is_relative_to(d) for d in folders) and p not in files

    def earlier_blocks(self, room: str) -> dict[str, tuple[str, str]]:
        """The paste rule's view of `room` (memory.earlier_blocks, read as the room's own lines are for its recall pack): built on
        the first fenced text that asks for it each day and kept until the date changes, since a block posted today never
        enters it; a restart builds it again from the day logs. Called off the loop."""
        today = memory.window(self.memory, days=1)[0]
        with self.paste_lock:
            if (kept := self.pastes.get(room)) and kept[0] == today:
                return kept[1]
        view = memory.earlier_blocks(self.memory, room, rid=room_id(room, self.room_confs.get(room, {})), owner=self.owner(room))
        with self.paste_lock:
            self.pastes[room] = (today, view)
        return view

    async def paste(self, room: Room, text: str, active: bool, at: str) -> tuple[str, str] | None:
        """The paste rule for a room's text bound for `at` (Room.paste_check), off the loop since its first call of the day reads
        the day logs; a text with no three backticks never leaves this line."""
        if memory.FENCE not in text:
            return None
        return await asyncio.to_thread(room.paste_check, active, text, lambda: self.earlier_blocks(at))

    async def tool_send(self, room: str, text: str, file: str, caller: str) -> dict[str, Any]:
        room = gates.room_named(room, self.room_confs) or room       # a room's name, or its channel id (the gate reads it so)
        if caller in self.rooms and not self.owner(caller) and room != caller:     # a room that is not the owner's posts to itself only
            return {"ok": False, "error": f"room {caller} posts to itself only; it is not one of the owner's rooms"}
        if room not in self.rooms:
            return {"ok": False, "error": f"no room {room!r}; rooms: {', '.join(self.rooms)}"}
        if not text and not file:
            return {"ok": False, "error": "nothing to send: give text, a file, or both"}
        ch = self.channels[room]
        if file and not self.may_send(caller if caller in self.rooms else room, Path(file)):
            await ch.notify(room, f"{words.NOT_DONE} {words.NOT_SENT.format(name=Path(file).name, why=words.file_why('outside'))}")
            return {"ok": False, "error": f"{file} is outside this room's folders; only files from them can be sent; " "the room was told"}
        src = self.rooms.get(caller) or self.rooms[room]
        if caller in self.rooms and text and (stale := await self.paste(src, text, False, room)):   # filed under `room`
            bounce, found = stale             # the Stop rule's words, flag and shadow row: the room's next send goes out
            self.ledger.shadow(t=round(time.time(), 3), room=caller, session_id=src.rp.session_id if src.rp else "",
                               turn=src.turn_key, rule="paste", detail=found, reply_chars=len(text))
            log.info("send: %s -> %s turned back once: a fenced block posted on an earlier day (%s)", caller, room, found)
            return {"ok": False, "error": bounce}
        if caller in self.rooms and src.busy and (sheet := src.sheet_check(False, text, file)):     # a room's own turn
            bounce, found = sheet             # the same way: its words, its flag, its shadow row
            self.ledger.shadow(t=round(time.time(), 3), room=caller, session_id=src.rp.session_id if src.rp else "",
                               turn=src.turn_key, rule="sheet", detail=found, reply_chars=len(text))
            log.info("send: %s -> %s turned back once: %s", caller, room, found)
            return {"ok": False, "error": bounce}
        sid = src.rp.session_id if src.rp else ""
        if caller in self.rooms and src.busy and (hit := src.said_check(False, text, lambda: self.ledger.said(caller, sid, src.evidence.started),
                                                                         self.owner_names)):
            bounce, rule, found = hit         # the same way: its words, its flag, its shadow row
            self.ledger.shadow(t=round(time.time(), 3), room=caller, session_id=sid, turn=src.turn_key, rule=rule, detail=found,
                               reply_chars=len(text))
            log.info("send: %s -> %s turned back once: %s", caller, room, found)
            return {"ok": False, "error": bounce}
        self.sends += 1
        with src.lock:                        # in a turn: its key and the messages it answers
            turn = src.turn_key if src.busy and src.turn_key else f"x{uuid.uuid4().hex[:11]}"
            inbound = ",".join(m.id for m in src.inflight) if src.busy else ""
        key = f"{turn}-s{self.sends}"                         # and this send's number: unique across restarts
        voice, why = voice_note.meta(Path(file)) if file else None, ""     # a note voice_note made goes as a voice message
        try:
            if voice:                                         # Discord takes no words beside one, so they follow it
                ids, why = await ch.send_voice(room, Path(file), voice, key=key, inbound=inbound)
                ids += await ch.send_text(room, text, key=f"{key}-t") if text else []
            else:
                ids = await (ch.send_file(room, Path(file), text, key=key, inbound=inbound) if file else ch.send_text(room, text, key=key))
        except (OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        log.info("send: %s -> %s, %d message(s)%s", caller or "?", room, len(ids), f", the voice note as a plain file: {why}" if why else "")
        return {"ok": True, "message_ids": ids, **({"voice_message": not why} if voice else {}),
                **({"note": words.VOICE_AS_FILE.format(why=why)} if why else {})}

    async def tool_transcribe(self, room: str, path: str, owner_yes: bool = False) -> dict[str, Any]:
        """The `transcribe` tool: an audio file from the calling room's own folders (may_send's rule; its inbox holds what
        was sent) made text off the loop (transcribe.run) on the install's one OpenAI key, each paid call a spend row."""
        p = Path(path).expanduser().resolve()
        if room not in self.rooms or not self.may_send(room, p):
            return {"ok": False, "error": f"{p.name} is outside this room's folders; transcribe reads the files sent to it"}
        if not p.is_file():                           # not audio: ffmpeg says so before anything is paid
            return {"ok": False, "error": f"there is no file {p.name} in this room's folders"}
        r = await asyncio.to_thread(transcribe.run, p, self.memory.embeddings_key, self.private / transcribe.GROQ_KEY,
                                    self.booker(room, "transcribe"), owner_yes)
        log.info("transcribe: %s, %s", room, f"{r['segments']} segments, {r['uncertain']} uncertain" if r["ok"] else r["error"][:160])
        return r

    async def tool_seen(self, room: str, path: str, text: str, shows: str) -> dict[str, Any]:
        """The `seen` tool: what a room read on a picture it was sent, filed beside it (pictures.file) for its recall, in any
        room (a person's: class room, memory.INBOXES). Only a picture in the calling room's own inbox, the header by code."""
        p, inbox = Path(path).expanduser().resolve(), (self.state / "rooms" / room / "work" / "inbox").resolve()
        if room not in self.rooms or p.parent.parent != inbox or not pictures.is_picture(p) or not p.is_file():
            return {"ok": False, "error": words.SEEN_NOT_HERE.format(name=p.name)}
        if not shows.strip():
            return {"ok": False, "error": words.SEEN_NO_SHOWS}
        out = await asyncio.to_thread(pictures.file, room, p, text, shows, self.memory.tz)
        log.info("seen: %s, %s, %d characters of its words", room, p.name, len(text))
        return {"ok": True, "file": str(out), "next": words.FILED_NEXT.format(clause=words.FILED_AT_MOST)}

    async def tool_need(self, room: str, asked: str, tried: str, needed: str, shadow: str = "") -> dict[str, Any]:
        """The `need` tool: what a room could not do, filed as one line (needs.file), folded into an open need of the same
        ability, and one line in the machine room; a person's room names only its own files (needs.foreign)."""
        if room not in self.rooms:
            return {"ok": False, "error": "need is for a room's own session"}
        others = [r for r in self.room_confs if r != room]
        if not self.owner(room) and (bad := needs.foreign([asked, tried, needed, shadow], self.state / "rooms" / room, others)):
            return {"ok": False, "line": words.NEED_NOT_YOURS.format(path=words.no_paths(bad))}
        with self.rooms[room].lock:
            mid = ",".join(m.id for m in self.rooms[room].inflight) or f"x{self.rooms[room].turn_key}"
        r = await asyncio.to_thread(needs.file, self.memory, self.state, room, mid, asked, tried, needed, shadow)
        if not r["ok"]:
            return r
        if not r["again"]:                                # a retried call is said once
            await self.jobs.alert(None, (words.NEED_AGAIN if r["folded"] else words.NEED_FILED).format(
                n=r["n"], room=room, ability=words.line(r["ability"], 80), day=r["day"], count=r["count"]), 0)
        log.info("need: %s, %s %s, %s", room, r["day"], r["n"], "folded" if r["folded"] else "filed")
        return {**r, "next": words.FILED_NEXT.format(clause=words.NEED_CLAUSE)}

    async def pictures(self, req: dict[str, Any]) -> dict[str, Any]:
        """`yerk pictures backfill ROOM`: the owner room's pictures with no sidecar (at most `limit`), BATCH to a Yerk worker on the
        room's own account and model, one batch at a time (pictures_done starts the next), so the account's other workers wait
        for one batch at most. The card is in the machine room, the worker's folder bound to the room's work folder. `root`: the
        pictures under a folder the room lists (`read` or `also`, pictures.root_of), each sidecar beside its picture; no bind."""
        room = str(req.get("room") or "")
        if room not in self.rooms or not self.owner(room):
            return {"ok": False, "error": words.PICTURES_PERSON.format(room=room)}
        root: Path | None = None
        if req.get("root") and (root := pictures.root_of(self.private, self.state, room, self.room_confs[room], str(req["root"]))) is None:
            return {"ok": False, "error": words.PICTURES_NOT_LISTED.format(room=room, folder=req["root"])}
        if any(self.backfill_of(row) == room for row in self.workers.live.values()):       # running or in line: one chain a room
            return {"ok": False, "error": words.PICTURES_RUNNING.format(room=room)}
        todo = pictures.pending(root or self.state / "rooms" / room / "work" / "inbox", root is not None)[:int(req.get("limit") or 0) or None]
        if not todo:
            return {"ok": True, "room": room, "pictures": 0, "line": words.PICTURES_NONE.format(room=room, where=root or "its inbox")}
        out = await self.picture_batch(room, pictures.batches(todo), 1, str(root or ""))
        return {**out, "pictures": len(todo), "batches": -(-len(todo) // pictures.BATCH)}

    async def picture_batch(self, room: str, rest: list[list[str]], n: int, root: str = "") -> dict[str, Any]:
        model = room_settings(self.room_confs[room], self.state, room, self.models)["model"][0]
        acct = self.rooms[room]._account(model)
        card = next((r for r, c in self.room_confs.items() if c.get("role") == "machine" and r in self.channels), room)
        pics, of = [Path(p) for p in rest[0]], n - 1 + len(rest)
        job = {"room": room, "n": n, "of": of, "rest": rest, **({"root": root} if root else {})}
        return await self.workers.start(card, pictures.brief(room, pics, self.memory.tz, n, of, Path(root) if root else None),
                                        account=acct.name, model=model, tasks=[f"{p.parent.name}/{p.name}" for p in pics],
                                        more={"pictures": job, **({} if root else {"bind": room})})

    @staticmethod
    def backfill(row: dict[str, Any]) -> dict[str, Any] | None:
        """A worker's pictures backfill batch, from the worker.json the daemon wrote; None for any other worker."""
        try:
            job = json.loads((Path(row["staging"]) / "worker.json").read_text()).get("pictures")
        except (OSError, ValueError):
            return None
        return job if isinstance(job, dict) else None

    def backfill_of(self, row: dict[str, Any]) -> str:
        return str((self.backfill(row) or {}).get("room") or "")

    async def pictures_done(self, row: dict[str, Any], end: dict[str, Any]) -> bool:
        """A backfill batch's end (workers.hooks): one line to the machine room, then the next batch when this one finished
        clean; never a relay turn. False for any other worker."""
        if (job := self.backfill(row)) is None:
            return False
        room, n, of, rest = str(job["room"]), int(job["n"]), int(job["of"]), list(job["rest"])
        mine = [Path(p) for p in rest[0]]
        for side in (pictures.sidecar(p) for p in mine if pictures.sidecar(p).is_file()):
            with contextlib.suppress(OSError):           # the worker's Write leaves its umask's mode; a private file is 0600
                side.chmod(0o600)
        filed, left = sum(pictures.sidecar(p).is_file() for p in mine), rest[1:]
        then = words.PICTURES_DONE
        if left and end["exit"] == "done" and room in self.rooms:
            started = await self.picture_batch(room, left, n + 1, str(job.get("root") or ""))
            then = words.PICTURES_NEXT.format(n=n + 1) if started.get("ok") else words.PICTURES_STOP.format(left=sum(map(len, left)))
        elif left:
            then = words.PICTURES_STOP.format(left=sum(map(len, left)))
        how = workers.settled_state(row, workers.tasks_of(Path(row["tasks"])), end)[1]
        line = words.PICTURES_BATCH.format(room=room, n=n, of=of, how=how, filed=filed, count=len(mine), then=then)
        if (to := str(row["room"])) in self.channels:
            with contextlib.suppress(Exception):
                await self.channels[to].notify(to, line)
        self.ledger.worker_set(str(row["id"]), relay="skipped", relay_note="pictures backfill: its line went to the machine room")
        log.info("%s", line)
        return True

    async def need_build(self, req: dict[str, Any]) -> dict[str, Any]:
        """`yerk needs build DAY N`: the need's brief (needs.brief) to a builder worker on [needs] account, else [curate]
        account, else the workers' own pick, its card in the machine room. One builder a need: the need is claimed under the
        lock, so one that is building, has a PR or a branch, or is merged starts nothing."""
        day, n = str(req.get("day") or ""), int(req.get("n") or 0)
        if await asyncio.to_thread(needs.update, self.memory, self.state, day, n, {"state": "building", "worker": ""},
                                   lambda r: r.get("state", "filed") == "filed") is None:
            row = needs.find(needs.load(self.state), day, n)
            return {"ok": False, "error": words.NEED_NONE.format(day=day, n=n) if row is None else
                    words.NEED_BUSY.format(n=n, state=needs.said(row))}
        row = needs.find(needs.load(self.state), day, n) or {}
        card = next((r for r, c in self.room_confs.items() if c.get("role") == "machine" and r in self.channels),
                    next((r for r in self.rooms if self.owner(r)), ""))
        conf = dict(self.conf.get("needs", {}))
        out = await self.workers.start(card, needs.brief(row, REPO), account=str(conf.get("account") or dict(
            self.conf.get("curate", {})).get("account") or ""), cap_minutes=float(conf.get("cap_minutes") or needs.CAP),
            tasks=list(needs.TASKS), more={"need": {"day": day, "n": n}})
        await asyncio.to_thread(needs.update, self.memory, self.state, day, n, {"worker": out["id"]} if out.get("ok") else {"state": "filed"})
        log.info("needs: build %s %s: %s", day, n, out.get("id") or out.get("error"))
        return {**out, "day": day, "n": n}

    async def need_built(self, row: dict[str, Any], end: dict[str, Any]) -> bool:
        """A need's builder ended (workers.hooks): the need's state from its report's first line (needs.ended) and one line to
        the machine room, never a relay turn; False for any other worker."""
        try:
            job = json.loads((Path(row["staging"]) / "worker.json").read_text()).get("need")
        except (OSError, ValueError):
            return False
        if not isinstance(job, dict):
            return False
        day, n = str(job.get("day")), int(job.get("n") or 0)
        how = workers.settled_state(row, workers.tasks_of(Path(row["tasks"])), end)[1]
        r = await asyncio.to_thread(needs.ended, self.memory, self.state, day, n, str(row["id"]), how,
                                    workers.first_line(Path(row["report"])), end["exit"] == "done")
        if r is not None:
            ab, st = words.line(str(r["ability"]), 80), r.get("state")
            line = (words.NEED_PR.format(n=n, pr=r.get("pr"), ability=ab) if st == "pr" else words.NEED_BRANCH.format(
                n=n, branch=r.get("branch"), ability=ab) if st == "built" else words.NEED_FAILED.format(n=n, how=how, ability=ab))
            if (to := str(row["room"])) in self.channels:
                with contextlib.suppress(Exception):
                    await self.channels[to].notify(to, line)
            log.info("%s", line)
        self.ledger.worker_set(str(row["id"]), relay="skipped", relay_note="need build: its line went to the machine room")
        return True

    async def tool_voice_note(self, room: str, script: str, name: str = "", owner_yes: bool = False) -> dict[str, Any]:
        """The `voice_note` tool: a script made a voice note off the loop (voice_note.run) in the calling room's own work
        folder, where `send` takes it from, each paid call a spend row, as transcribe's are."""
        if room not in self.rooms:
            return {"ok": False, "error": f"no room {room!r}; rooms: {', '.join(self.rooms)}"}
        r = await asyncio.to_thread(voice_note.run, script, name, self.state / "rooms" / room / "work" / voice_note.FOLDER, self.private,
                                    self.memory.embeddings_key, self.booker(room, "voice_note"), owner_yes)
        log.info("voice_note: %s, %s", room, f"{r['engine']}, {r['duration_s']} s, US${r['usd']:.4f}" if r["ok"] else r["error"][:160])
        return r

    def booker(self, room: str, tool: str) -> transcribe.Book:
        """A paid tool's spend writer: one ledger row per paid call (decision 32), the morning diff's day total."""
        def book(engine: str, usd: float, note: str) -> None:
            try:
                self.ledger.spend(room=room, tool=tool, engine=engine, usd=round(usd, 6), note=note)
            except Exception as e:                    # the ledger never stops a paid call's answer reaching the room
                log.warning("%s: a spend row was not written (%s, US$%.4f): %s", tool, engine, usd, e)
        return book

    async def tool_recall(self, room: str, query: str, k: int) -> dict[str, Any]:
        """Off the loop: the index is yerk-index's file, read here, never built here (L47). The first
        recall in a room while yerk-index runs without a provider key tells the room so, once. A superseded
        line never comes above its replacement (memory.live_first)."""
        r = await asyncio.to_thread(self.search, room, query, k)
        log.info("recall: %s, %d hit(s)%s", room or "?", len(r.get("hits", [])), "" if r["ok"] else f", {r['status']}")
        if r["ok"] and room in self.rooms and memory.meaning_state(self.memory) == "no-key":
            await self.tell_once(room, "embeddings", NO_KEY_LINE)
        return r

    def search(self, room: str, query: str, k: int = 6, chars: int = 0, wait: float = memory.MEANING_WAIT, deadline: float = 5.0,
               docs: bool = True) -> dict[str, Any]:
        """The recall tool's search, for the tool and the prompt hook alike (which waits less, inside its 10 s): the room's reader in its own
        scope; the hook's without the docs root (`docs`), which a room reaches by its own recall and read_memory."""
        rd, s = self.reader(room), self.scoped.get(room, self.memory)
        return memory.live_first(s, memory.recall(s, query, k, deadline, rd, chars, wait, docs), rd)

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
        return await asyncio.to_thread(memory.read, self.scoped.get(room, self.memory), path, start, lines, self.reader(room))

    async def tool_remember(self, room: str, text: str, about: str, replaces: list[str], said: str = "", source: str = "") -> dict[str, Any]:
        """The `remember` tool: a room's own note into Yerk's own tree (memory.remember), stamped with
        the ids of the messages its turn answers, or with `said` and `source` a person's words; a filed one ends with `next`."""
        if room not in self.rooms:
            return {"ok": False, "error": "remember is for a room's own session"}
        with self.rooms[room].lock:
            mid = ",".join(m.id for m in self.rooms[room].inflight) or f"x{self.rooms[room].turn_key}"
        r = await asyncio.to_thread(memory.remember, self.scoped.get(room, self.memory), self.daily, room, mid, text, about,
                                    tuple(replaces), self.reader(room), said, source)
        return {**r, "next": words.FILED_NEXT.format(clause=words.FILED_AT_MOST)} if r["ok"] else r

    def owner_only(self, room: str, named: str) -> dict[str, Any] | None:
        """A room's own call that would start or read a worker: the owner's rooms only (a worker runs as
        the owner's user), and for the calling room only. None means go on; else the one line."""
        if not room:                                   # the install's own callers keep every form
            return None
        if not self.owner(room):
            log.warning("workers: room %s is not an owner room; its worker call was refused", room)
            return {"ok": False, "line": words.WORKERS_OWNER_ONLY}
        if named and named != room:
            return {"ok": False, "line": words.WORKER_OWN_ROOM.format(room=named)}
        return None

    async def tool_rule(self, room: str, text: str, job: str) -> dict[str, Any]:
        """`remember` with a `job`: an owner's room files how it is done from now on (memory.rule) for every room, fitted under
        the rules part's budget; one with no room even with no examples is filed as a topic rule in rules-topics.md and said
        (`topic`: the reply's clause and one machine-room notice, each naming the words it loads on)."""
        if room not in self.rooms or not self.owner(room):
            return {"ok": False, "line": words.RULES_OWNER_ONLY}
        if self.reader(room).sealed:        # its teaching stays its own day-log note, the job its label, never the shared rules
            return await self.tool_remember(room, text, job.strip(" :"), [])
        with self.rooms[room].lock:
            mid = ",".join(m.id for m in self.rooms[room].inflight) or f"x{self.rooms[room].turn_key}"
        cap = int(self.budgets["session_start"]["appended"]["rules"])
        r = await asyncio.to_thread(memory.rule, self.memory, room, mid, job, text, cap, self.daily, context.books(self.private, self.room_confs))
        if not r["ok"]:
            return r
        on = words.either(r.get("triggers", []))
        if r.get("topic") and not r.pop("again", False):       # a retried call: filed and said already
            where = words.TOPIC_WHERE[1].format(rooms=", ".join(r["rooms"])) if r["rooms"] else words.TOPIC_WHERE[0]
            await self.jobs.alert(None, f"{words.DONE} " + words.RULE_TOPIC.format(triggers=words.line(on, words.PHONE - 8), job=words.line(
                r["job"], words.PHONE - 2), room=room, where=words.line(where), cap=cap), 0)
        return {**r, "next": words.FILED_NEXT.format(clause=words.TOPIC_CLAUSE.format(triggers=on) if r.get("topic") else words.RULE_CLAUSE.format(job=r["job"]))}

    async def tool_worker(self, room: str, req: dict[str, Any]) -> dict[str, Any]:
        """The `worker` tool (rooms only; a worker has no MCP server). `yerk worker` comes through
        /control, which keeps every form, for any room."""
        named = str(req.get("room") or "") if req.get("action") == "start" else ""
        return self.owner_only(room, named) or await self.workers.handle(room, req)

    async def tool_schedule(self, room: str, req: dict[str, Any]) -> dict[str, Any]:
        """The `schedule` tool (add, remove, list); the job runner's hand-offs come through /control. An agent,
        script job runs as the owner's user (a worker; a shell command with no sandbox at all), so an
        owner's room adds one for itself by the worker rule and lists or removes any. A note runs nothing (its room
        is told its text when due), so a room that is not the owner's keeps its own notes and nothing else."""
        if room and not self.owner(room):
            return await self.jobs.notes(room, req)
        refused = self.owner_only(room, str(req.get("room") or "") if req.get("action") == "add" else "")
        return refused or await self.jobs.handle(room, req)

    async def tool_hold(self, room: str, wait_for: str, reason: str) -> dict[str, Any]:
        if room not in self.rooms:
            return {"ok": False, "error": "hold is for a room's own session"}
        hid = self.ledger.hold(room, wait_for, reason)
        return {"ok": True, "hold": hid, "next": "End your turn now with one short line; no more tool calls."}

    async def tool_settings(self, room: str, model: str, effort: str, account: str = "", by_owner: bool = False) -> dict[str, Any]:
        """The `settings` tool: an owner room's model, thinking level and account (tried first, the room's order behind
        it), the daemon's override over rooms.toml (which is never written). The running turn ends as it began; then the
        room's process closes and the next message resumes the same session on the new ones (Room.respec), on another
        account as a hop does; after an account change the line's second line says which (`carried`)."""
        if not (self.owner(room) or by_owner):      # by_owner: the owner's own slash command, any of their rooms
            return {"ok": False, "line": words.OWNER_ONLY}
        conf, names = self.room_confs[room], {a.name.lower(): a.name for a in self.accounts}
        want, level, acct = model.strip(), effort.strip().lower(), account.strip()
        full = self.models.get(want.lower()) or (want if want in self.models.values() else want.lower())
        put = "default" if acct.lower() == "default" else account_named(acct, [*names.values()])
        if want and full != "default" and full not in self.models.values():
            listed = ", ".join(f"{a} ({words.model_name(m)})" for a, m in self.models.items())
            return {"ok": False, "line": words.NOT_LISTED.format(model=want, models=listed)}
        if level and level != "default" and level not in EFFORTS:
            return {"ok": False, "line": words.NOT_A_LEVEL.format(effort=level, levels=", ".join([*EFFORTS, "default"]))}
        if acct and not put:
            return {"ok": False, "line": words.NOT_AN_ACCOUNT.format(account=acct, accounts=", ".join([*names.values(), "default"]))}
        before, over = room_settings(conf, self.state, room, self.models), override(self.state, room)
        pin = pinned(self.state, room, [*names.values()])
        for key, value in (("model", full if want else ""), ("effort", level), ("account", put)):
            if value == "default":
                over.pop(key, None)
            elif value:
                over[key] = value
        if want or level or acct:
            (self.state / "rooms" / room).mkdir(parents=True, exist_ok=True)
            write_private(self.state / "rooms" / room / "override.json", json.dumps(over))
        now, on = room_settings(conf, self.state, room, self.models), pinned(self.state, room, [*names.values()])
        if ([v for v, _ in now.values()], on) != ([v for v, _ in before.values()], pin) and room in self.rooms:
            await asyncio.to_thread(self.rooms[room].respec)        # an idle process's close, off the loop
        log.info("settings: room %s on %s, effort %s, account %s", room, now["model"][0], now["effort"][0] or "default", on or "by order")
        line = (words.NEXT_REPLY if want or level or acct else words.THIS_ROOM).format(
            model=words.model_name(now["model"][0]), effort=now["effort"][0] or "default", on=words.ON.format(account=on) if on else "")
        return {"ok": True, "line": line + (self.carried(room, on) if on != pin else "")}

    async def model_command(self, room: str, choice: str, account: str = "") -> dict[str, Any]:
        """/model: its one argument an account, a model or both (config.account_and_model), default clearing both, through the
        settings tool (a typed `account` option keeps the argument a model alone, as before); nothing, the room's line, then
        its accounts in the order tried and the models (model_menu)."""
        acct, model = ("", choice) if account.strip() else account_and_model(choice, self.models, [a.name for a in self.accounts])
        r = await self.tool_settings(room, model, "", account.strip() or acct or ("default" if model.strip().lower() == "default" else ""), by_owner=True)
        return {**r, "line": "\n".join([r["line"], *self.model_menu(room)])} if r["ok"] and not (choice.strip() or account.strip()) else r

    def tried(self, room: str, model: str) -> list[tuple[str, str]]:
        """The room's accounts in the order a turn tries them (Room._account: the room's own list, rooms.toml's `accounts` first),
        the one it is put on first, as (line, name): its order and its rest for `model`, the one it is put on marked."""
        pin, order = pinned(self.state, room, [a.name for a in self.accounts]), (self.rooms[room].accounts if room in self.rooms else self.accounts)
        return [(words.MENU_ACCOUNT.format(name=a.name, order=a.order, state=words.rest(self.cooldowns.until(a.name, model=model)))
                 + (words.PINNED_MARK if a.name == pin else ""), a.name) for a in sorted(order, key=lambda a: a.name != pin)]

    def model_menu(self, room: str) -> list[str]:
        """Bare /model's lines under the room's own: the accounts in the order tried (when there is more than one), the models."""
        model = room_settings(self.room_confs[room], self.state, room, self.models)["model"][0]
        head = [words.MENU_ACCOUNTS.format(model=words.model_name(model) if model else words.USUAL), *(line for line, _ in self.tried(room, model))]
        return (head if len(self.accounts) > 1 else []) + [words.MENU_MODELS.format(models=", ".join(f"{k} ({words.model_name(v)})" for k, v in self.models.items()))]

    def model_picks(self, room: str, typed: str) -> list[tuple[str, str]]:
        """/model's suggestions as the owner types, (label, value), at most Discord's 25: the accounts in the order tried, the
        models and default, then each account with each model and its rest for it; once they type, the ones that match."""
        tried = self.tried(room, room_settings(self.room_confs[room], self.state, room, self.models)["model"][0])
        picks = [*tried, *((f"{k}: {words.model_name(v)}", k) for k, v in self.models.items()), (words.PICK_DEFAULT, "default")]
        both = [(f"{n} {k}: " + words.PICK_ON.format(model=words.model_name(v), name=n, state=words.rest(self.cooldowns.until(n, model=v))), f"{n} {k}")
                for _, n in tried for k, v in self.models.items()]
        t = " ".join(filter(None, account_and_model(typed, self.models, [n for _, n in tried]))).lower()
        return [p for p in picks + both if t in p[1] or t in p[0].lower()][:25] if t else picks[:25]

    def carried(self, room: str, pin: str) -> str:
        """After an account change, the line's second line: what the next message does where it starts (RoomProcess.start).
        The session goes on, by id on its own account, by the transcript's path on another; with no path on file there is
        none to find, so the turn starts fresh with a recap (session_missing). Nothing if no session or a fresh start is due."""
        row, r = self.sessions.get(room) or {}, self.rooms.get(room)
        if not row or r is None or r.fresh_next or (self.state / "rooms" / room / "fresh-next").is_file():
            return ""
        model = room_settings(r.conf, self.state, room, self.models)["model"][0]      # a rest keyed to another model does not count
        same = row.get("account") == pick(r.accounts, self.cooldowns, first=pin, model=model).name
        return "\n" + (words.CARRIED if same or os.path.isfile(str(row.get("transcript") or "")) else words.RECAPPED)

    async def room_new(self, room: str) -> dict[str, Any]:
        """/new: the room's next message starts a fresh session with its recap line (Room.renew). The
        "could not go on" notice is for a session lost, not one asked for, so its mark goes too."""
        if room not in self.rooms:
            return {"ok": False, "line": words.NOT_A_ROOM}
        self.rooms[room].renew()
        (self.state / "rooms" / room / "last-session").unlink(missing_ok=True)
        return {"ok": True, "line": words.NEW_NEXT}

    async def room_stop(self, room: str) -> dict[str, Any]:
        """/stop: the running turn ends now and for good (Room.stop, L82); ok is False when none runs."""
        if room not in self.rooms:
            return {"ok": False, "line": words.NOT_A_ROOM}
        stopped = await asyncio.to_thread(self.rooms[room].stop)
        return {"ok": stopped, "line": words.STOP_DONE if stopped else words.NOTHING_TO_STOP}

    # recall before the first word (DESIGN 5)
    def room_pack(self, room: str, left: int = 6000) -> tuple[str, str]:
        r = self.rooms.get(room)
        text, _ = memory.room_pack(self.memory, room, left, exclude=r.current_ids() if r else set(),
                                   rid=room_id(room, self.room_confs.get(room, {})), owner=self.owner(room))
        return text, str(self.memory.daily)

    def compact_pack(self, room: str) -> str:
        """The recall pack as session start builds and fits it, for the re-add after a compaction."""
        conf = self.room_confs[room]
        limit = int(self.budgets["session_start"]["appended"]["recall_pack"])
        text, src = context.recall_pack(context.layer_dir(self.private, conf), conf, limit, lambda left: self.room_pack(room, left))
        return context.fit("recall_pack", text, limit, src)[0]

    def recall_for(self, room: str, prompt: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """(names, topics, past, absent), each search decided by code (decision 42), read as the room's reader in its scope: names
        on file (every one for an owner room; for any other, those in files it may read now, R2), each with its best lines from
        the room's whole scope (memory.lines_on), as each name or topic the message asks about gets (words.asked), or the absence
        line when it has none; the room's own lines on the topic; a question about the past (words.PAST): the recall tool's own search.
        None of them reads the docs root: a room reaches the docs by its own recall and read_memory. Off the loop."""
        conf, s, rd = self.room_confs.get(room, {}), self.scoped.get(room, self.memory), self.reader(room)
        names: list[dict[str, Any]] = []
        deep: list[dict[str, Any]] = []
        absent: list[str] = []
        r = self.rooms.get(room)
        mine = r.current_ids() if r else set()            # the messages this turn answers: filed already, never their own answer
        back = conf.get("topic_recall", True) and words.PAST.search(prompt)     # beside it a card shows its first line: no deep search
        if conf.get("mention_recall", True):
            try:            # a room's own `also`: the entries from its roots only (a cite is relative in Yerk's own tree, which sealed skips)
                names = self.names.load().match(prompt, keep=lambda e: (rd.owner or memory.may_read(rd, *memory.file_class(s.root / e["cite"].rpartition(":")[0])))
                                                and (not (s.scoped or rd.sealed) or s.scope.contains(s.root / e["cite"].rpartition(":")[0])
                                                     and (Path(e["cite"].rpartition(":")[0]).is_absolute() or not rd.sealed)), strict=not rd.owner)
            except (OSError, ValueError, KeyError) as e:
                log.warning("mention recall skipped: %s", e)
            keyed, cards = list(dict.fromkeys(e["named"] for e in names)), {e["cite"] if e["cite"].startswith("/") else f"{s.root}/{e['cite']}" for e in names}
            for what in ([] if back else keyed) + [a for a in words.asked(prompt) if " ".join(mentions.tokens(a, True)) not in keyed]:
                try:            # a read that did not finish proves no absence; when eight chunks hold none, twenty are read once
                    got = memory.lines_on(s, what, rd, exclude=mine) or memory.lines_on(s, what, rd, 20, exclude=mine)
                except LookupError:
                    continue
                deep += [{"key": "", "text": x["text"], "cite": x["cite"], "named": what} for x in got if x["at"] not in cards]
                absent += [] if got or what in keyed else [what]
        topics = memory.topic_hits(self.memory, room, prompt, mine, rid=room_id(room, conf),
                                   owner=self.owner(room)) if conf.get("topic_recall", True) else []
        past = [h for h in self.search(room, prompt, PAST_HITS, int(self.budgets["per_message"]["on_hit"]["recall"]), 2.0, 3.0, docs=False)
                .get("hits", []) if not any(f"[m:{m}" in h["text"] for m in mine)] if back else []
        return abilities.asked(prompt, self.memory) + names + deep, topics, past, absent     # "can you do voice": the manifest's answer

    def rules_news(self, room: str, sid: str = "") -> str:
        """The rules block for session `sid` (context.news), under budgets.toml's `rules`; "" records a process start."""
        if self.reader(room).sealed:        # none, and no rules-seen.json with the rules' text in its folder: no other room's rule
            return ""
        return context.news(self.state / "rooms" / room / "rules-seen.json", sid, self.private, self.room_confs[room],
                            self.memory.root / memory.RULES, int(self.budgets["per_message"]["on_hit"]["rules"]))

    def rules_topic(self, room: str, sid: str, prompt: str, budget: int) -> tuple[str, list[str]]:
        """The topic rules this message names, the room's own and every room's, each once per session (context.topics); none in a sealed room."""
        if self.reader(room).sealed:
            return "", []
        return context.topics(self.state / "rooms" / room / context.TOLD, sid, room, self.memory.root / memory.RULES, prompt, budget)

    def move_held(self) -> str:
        """At a start: rules-overflow.md's rules into rules-topics.md, once (memory.move_held), and the machine-room line saying so."""
        try:
            n = memory.move_held(self.memory, context.books(self.private, self.room_confs))
        except OSError:
            log.exception("rules: %s did not move to %s; it stays as it was", memory.OVERFLOW, memory.TOPIC_RULES)
            return ""
        return f"{words.DONE} " + words.TOPICS_MOVED.format(n=n, s="s" * (n != 1)) if n else ""

    def shadow_stop(self, name: str, room: Room, reply: str, sid: str, refused: bool = False, went: bool = False) -> list[tuple[str, str]]:
        """The evidence rules after a reply: ledgered and logged, never a block (shadow, DESIGN 8). `refused`: the said check
        turned this reply back, its own row written, so its person and quote rows are not written twice. `went`: no Stop rule
        turned it back, so each call it makes is a `calls` row with the cites its turn read (claim 19), a person's room's
        for its people, never the owner."""
        figure = memory.on_file(self.memory)
        said = self.ledger.said(name, sid, room.evidence.started)     # its replies posted earlier in this session
        found = evidence.check(reply, room.evidence, figure, said)
        found += evidence.check_claims(reply, room.evidence, self.memory.scope.roots)     # decision 25
        found += [] if room.in_owner_name() else evidence.check_sheet(reply, room.evidence)   # an owner's room refuses instead
        if not refused:                                   # a claim about a person needs a read, a quote its source
            found += evidence.check_person(reply, room.evidence, self.owner_names)
            found += evidence.check_quote(reply, room.evidence, said, self.owner_names)
        with room.lock:                                   # claim 3: the owner pushing back on a reply posted earlier
            pushed = [m.received for m in room.inflight if m.sender == "owner" and words.PUSHBACK.search(m.text)]
        found += evidence.stance(reply, pushed, room.evidence) if pushed and said and reply.strip() else []
        new = [f for f in found if f not in room.evidence.logged]
        for rule, detail in new:
            room.evidence.logged.add((rule, detail))
            self.ledger.shadow(t=round(time.time(), 3), room=name, session_id=sid, turn=room.turn_key, rule=rule,
                               detail=detail, reply_chars=len(reply))
            log.info("room %s: shadow %s: %s (not blocked)", name, rule, detail)
        if went:
            s = self.scoped.get(name, self.memory)
            for row in evidence.receipt_rows(reply, room.evidence, lambda p: memory.cite(s, Path(p).expanduser()) if p.startswith(("/", "~")) else p):
                if ("call", row["call"]) not in room.evidence.logged:
                    room.evidence.logged.add(("call", row["call"]))
                    self.ledger.call(t=round(time.time(), 3), room=name, session_id=sid, turn=room.turn_key,
                                     whom="owner" if room.in_owner_name() else name, **row)
        return new

    # the hooks
    def room_of(self, payload: dict[str, Any], auth: dict[str, str] | None = None) -> str | None:
        """The room a hook call belongs to: the binding that holds the call's token; for the install's
        own callers, by the payload's session id, else by its cwd (a room's work folder)."""
        if auth and auth.get("kind") == "room" and auth.get("room") in self.rooms:
            return str(auth["room"])
        name = self.sessions.by_session(str(payload.get("session_id", "")))
        if name in self.rooms:
            return name
        cwd = Path(str(payload.get("cwd") or "/nonexistent"))
        for n in self.rooms:
            work = self.state / "rooms" / n / "work"
            if cwd == work or work in cwd.parents:
                return n
        return None

    def notify_later(self, room: str, text: str, file: Path | None = None) -> None:
        """A gate's one line to the room, posted beside the hook's answer so the hook never waits on it;
        a held call's exact text too long to show goes right after it as a file."""
        task = asyncio.get_running_loop().create_task(self._notice(room, text, file))
        self.later.add(task)
        task.add_done_callback(self.later.discard)

    async def _notice(self, room: str, text: str, file: Path | None) -> None:
        await self.channels[room].notify(room, text)
        if file is not None:
            try:
                await self.channels[room].send_file(room, file, caption=words.HELD_FILE)
            except (OSError, ValueError) as e:            # send_file has already posted a visible notice
                log.warning("room %s: the held call's text file did not go out: %s", room, e)

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
        the overhead per turn is a query on that table, never a guess. A call no binding
        explains is answered with the unbound marker and logged, never with a silent {}."""
        t0 = time.monotonic()
        event = str(payload.get("hook_event_name") or "")
        name = self.room_of(payload, auth)
        out: dict[str, Any] = {}
        try:
            if name is None:
                log.warning("hook: %s from session %s (%s caller) matches no room binding; answered unbound",
                            event or "?", str(payload.get("session_id", ""))[:8] or "?", (auth or {}).get("kind") or "no")
                return (out := dict(hooks.UNBOUND))
            self.learn_transcript(name, payload)
            return (out := await self._hook(event, name, payload))
        finally:
            if (room := self.rooms.get(name or "")) and event != "SessionStart":   # SessionStart alone proved nothing
                room.hooked = True        # the process's hooks are alive (room._results says so when none came)
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
                self.notify_later(name, line, self.held_files.pop(name, None))
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
            active, refused = bool(payload.get("stop_hook_active")), False
            reason = room.stop_check(active) if room else None
            if reason:
                log.info("room %s: Stop bounced once: an image was never read, or a picture never filed", name)
            elif room and name:
                said = payload["last_assistant_message"] if "last_assistant_message" in payload else None
                reason = room.silent_check(active, None if said is None else str(said or ""), self.ledger.landed(room.turn_key))
                if reason:
                    self.ledger.gate(room=name, session_id=sid, turn=room.turn_key, gate="silent", decision="bounce",
                                     tool="", rule="silent-ending", detail=reason[:300])
                    log.info("room %s: Stop bounced once: the turn ended with nothing visible", name)
                elif (stub := room.stub_check(active, None if said is None else str(said))):
                    reason, found = stub              # a shadow row so the morning diff counts every bounce
                    self.ledger.shadow(t=round(time.time(), 3), room=name, session_id=sid, turn=room.turn_key, rule="stub",
                                       detail=found, reply_chars=len(str(said)))
                    log.info("room %s: Stop bounced once: the final text only reports a filing (%s)", name, found)
                elif (board := room.board_check(active, None if said is None else str(said))):
                    reason, found = board             # counted the same way as a stub
                    self.ledger.shadow(t=round(time.time(), 3), room=name, session_id=sid, turn=room.turn_key, rule="board",
                                       detail=found, reply_chars=len(str(said)))
                    log.info("room %s: Stop bounced once: a status board in a room with people (%s)", name, found)
                elif said and (paste := await self.paste(room, str(said), active, name)):
                    reason, found = paste             # the send tool's words and flag: one bounce per result in either place
                    self.ledger.shadow(t=round(time.time(), 3), room=name, session_id=sid, turn=room.turn_key, rule="paste",
                                       detail=found, reply_chars=len(str(said)))
                    log.info("room %s: Stop bounced once: a fenced block posted on an earlier day (%s)", name, found)
                elif said and (sheet := room.sheet_check(active, str(said), stop=True)):
                    reason, found = sheet             # the send tool's words and flag: one ask per result in either place
                    self.ledger.shadow(t=round(time.time(), 3), room=name, session_id=sid, turn=room.turn_key, rule="sheet",
                                       detail=found, reply_chars=len(str(said)))
                    log.info("room %s: Stop bounced once: %s", name, found)
                elif said and (hit := room.said_check(active, str(said), lambda: self.ledger.said(name, sid, room.evidence.started), self.owner_names)):
                    reason, rule, found = hit         # the send tool's words and flag: one refusal per result in either place
                    self.ledger.shadow(t=round(time.time(), 3), room=name, session_id=sid, turn=room.turn_key, rule=rule,
                                       detail=found, reply_chars=len(str(said)))
                    log.info("room %s: Stop bounced once: %s", name, found)
                    refused = True
            if room and name:
                try:
                    await asyncio.to_thread(self.shadow_stop, name, room, str(payload.get("last_assistant_message") or ""), sid, refused, not reason)
                except Exception:
                    log.exception("room %s: the shadow Stop check failed (nothing blocked)", name)
            return {"decision": "block", "reason": reason} if reason else {}
        if event == "SessionStart":
            if payload.get("source") == "compact":
                if name:                          # the topic rules ride again after it, each once (context.topics)
                    (self.state / "rooms" / name / context.TOLD).unlink(missing_ok=True)
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
                                      lambda r, p: self.rooms[r].meta_for(p), self.budgets, None, self.recall_for, name, self.rules_news,
                                      self.taste_for, self.place_for, self.rules_topic)
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
            self.rooms[name].evidence.give(f"{prompt}\n{added}", recall=any(h in added for h in hooks.HEADS))
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
        key, shown, cwd = "", "", str(payload.get("cwd") or self.state / "rooms" / name / "work")
        if v is None and (hit := gates.external_hit(tool, ti, name, self.gates, self.room_confs)):
            rule, what, call = hit
            key = gates.call_key(tool, ti)
            shown = gates.plain(tool, ti) if self.room_confs[name].get("people") else key     # a person reads it: no JSON
            rel = self.ledger.release(name, rule, key, cwd, self.gates.window_s)
            if rel:
                v = gates.Verdict("approval", "release", detail=f"{what}: {call}; approved by {rel['approved_by']}", rule=rule)
            else:
                model, line = gates.held(what, call, self.gates.word, exact=shown)
                v = gates.Verdict("approval", "hold", model, line, f"{what}: {call}", rule=rule)
        if v is None:
            return {}, ""
        gid = self.ledger.gate(room=name, session_id=sid, turn=room.turn_key, gate=v.gate, decision=v.decision,
                               tool=tool, rule=v.rule, detail=v.detail)
        if v.decision == "hold":
            self.ledger.hold_add(name, v.rule, v.detail.split(":")[0], gid, key, cwd)    # the exact call: the receipt
            if len(shown) > gates.SHOW:                   # too long for one message: the text shown, as a file
                path = self.state / "rooms" / name / "held" / f"{gid}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                write_private(path, shown + "\n")
                self.held_files[name] = path
        if v.decision == "rewrite" and v.new_input:
            self.rewrites[str(payload.get("tool_use_id") or "")] = (name, str(v.new_input.get("command")))
        log.info("room %s: gate %s %s: %s", name, v.gate, v.decision, v.detail[:160])
        return v.output(), v.line

    def said_in_turn(self, name: str, sid: str, prompt: str) -> Inbound | None:
        """The message behind a prompt, for the approval word: the hook call must carry the room's bound
        session id (the cwd fallback is not enough), and the prompt must be a message the room itself
        wrote into its running turn. /hook has no secret yet (restart test R3), so this binding is what
        keeps a posted "approve" from counting."""
        if self.sessions.by_session(sid) != name:
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
        write_private(keep, "\n".join(lines))
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
        if isinstance(payload.get("pictures"), dict):   # `yerk pictures backfill ROOM`
            return await self.pictures(dict(payload["pictures"]))
        if isinstance(payload.get("needs"), dict):      # `yerk needs build DAY N`
            return await self.need_build(dict(payload["needs"]))
        if isinstance(payload.get("recall"), dict):     # a worker's read-only tools, from its own stdio server
            req = dict(payload["recall"])
            return await self.tool_recall(str(req.get("room", "")), str(req.get("query", "")), int(req.get("k") or 6))
        if isinstance(payload.get("read_memory"), dict):
            req = dict(payload["read_memory"])
            return await self.tool_read_memory(str(req.get("room", "")), str(req.get("path", "")),
                                               int(req.get("start") or 1), int(req.get("lines") or memory.READ_LINES))
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
        """/status: what runs now, for `yerk status` and any program that asks (status.py); {"live": true} asks
        each account's meters now (the card's), else only a reading under a minute old is used."""
        out = await asyncio.to_thread(status.collect, self.private, self.state, self.accounts, self.cooldowns, self.ledger,
                                      self.rooms, live=bool((payload or {}).get("live")))
        return {**out, "started": self.started, "commit": self.commit,
                "connected": bool(getattr(self.link, "connected", False)),
                "rebuilding": await asyncio.to_thread(self.rebuild_running)}   # deploy waits it out, as the gate does

    def write_status(self, mode: str) -> None:
        """state/yerkd.json: what is running, for `yerk room close` and the probes."""
        write_private(self.state / "yerkd.json", json.dumps({
            "pid": os.getpid(), "port": self.port, "mode": mode, "started": round(time.time(), 3),
            "rooms": sorted(self.rooms), "secret_file": str(self.secret_file),      # paths, never values
            "worker_secret_file": str(self.worker_secret_file)}))

    async def serve_http(self) -> None:
        import uvicorn
        app = mcp_server.app(mcp_server.build(self, self.port, self.hook, self.control, self.status_now), self.check())
        self.http = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        await self.http.serve()

    def listening(self) -> bool:
        """The HTTP port is bound: READY=1 may go out (a start counts only when it can answer)."""
        return self.http is not None and bool(self.http.started)

    async def until_stopped(self, tasks: list[asyncio.Task[Any]], close: Callable[[], Awaitable[Any]]) -> None:
        """The daemon runs until one of its tasks ends (the channel's start raising at boot ends it as before: that error
        is the exit) or SIGTERM or SIGINT comes. On a signal: STOPPING=1 to systemd, every room's process ended (a turn
        mid-run is cut and its messages stay open in the ledger, so the next start runs them once more, marked), the
        channel closed clean, the HTTP server ended, exit 0; measured 1 to 2 s with a turn running (signal drill)."""
        asked = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, asked.set)
        waiter = asyncio.create_task(asked.wait())
        done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
        if waiter not in done:
            waiter.cancel()
            for t in done:
                t.result()                                # a task that raised: its error ends the daemon
            return
        notify("STOPPING=1")
        cut = await asyncio.gather(*(asyncio.to_thread(r.shutdown) for r in self.rooms.values()))
        log.info("stopping: %d room(s) closed, %d turn(s) cut; a cut turn's messages run again once at the next start",
                 len(cut), sum(cut))
        with contextlib.suppress(Exception):
            await close()
        if self.http is not None:                 # and its own end awaited (tasks[0] is serve_http): a cancel mid-serve
            self.http.should_exit = True          # cut uvicorn's lifespan and logged a CancelledError traceback per stop
            await asyncio.wait(tasks[:1], timeout=5)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def reap(self, every: float = 60.0) -> None:
        while True:
            await asyncio.sleep(every)
            for room in self.rooms.values():
                await asyncio.to_thread(room.idle_compact)     # first: a room it starts is busy, so the reap leaves it open
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
        owner = discord_id(d.get("owner"))
        if owner is None:                        # the rooms still start; every command names the setting
            log.warning("discord: [discord] owner in yerkd.toml is %s, so no person's message is answered and every "
                        "slash command says to set it", "missing" if "owner" not in d
                        else f"a {type(d['owner']).__name__}, not a Discord user id")
        dc = DiscordChannel(token, owner, [int(x) for x in d.get("test_senders", [])], self.ledger,
                            guard=guard, state=self.state, capture=self.daily.capture,
                            daemon=self if flag(d.get("commands"), True) else None, taste=self.taste_heard,   # the slash commands
                            assistants=assistants(self.conf, frozenset({owner, *(i for n, c in self.room_confs.items() for i in people(n, c))})))
        for name, conf in self.room_confs.items():
            kind, _, cid = str(conf.get("channel", "")).partition(":")
            wk, _, wid = str(conf.get("watch", "")).partition(":")     # read as the room's own, never written to
            if kind == "discord" and cid.isdigit():
                dc.add_room(int(cid), self.add_room(name, dc), int(wid) if wk == "discord" and wid.isdigit() else None, people(name, conf))
        if not dc.rooms:
            raise SystemExit("no room has a discord:<channel id> in rooms.toml")
        self.workers.begin(asyncio.get_running_loop())
        moved = self.move_held()                         # before any room starts; said after the connect

        async def say_moved() -> None:
            if moved:
                await self.jobs.alert(None, moved, 0)
        dc.after_recover.append(say_moved)
        dc.after_recover.append(self.workers.reattach)   # their cards are edited, so after the connect
        dc.after_recover.append(self.jobs.reattach)      # a script run that ended with no daemon up: delivered late
        self.write_status("discord")
        self.link = dc
        tasks = [asyncio.create_task(c) for c in (self.serve_http(), self.reap(min(60.0, self.idle_seconds / 2)),
                                                   self.beat.tick(self.listening), dc.start())]
        await self.until_stopped(tasks, dc.close)


def claim(state: Path) -> int:
    """One daemon per state folder: an exclusive flock on <state>/yerkd.lock, held for the process's life (the kernel
    drops it with the process, so a kill -9 leaves nothing to clear) and taken before the ledger opens, so a second
    yerkd on the same folder stops with one line before any migration, crash repair or day-log rewrite."""
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / "yerkd.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit(f"another yerkd runs on {state} (yerkd.json names its pid); stop it first") from None
    return fd


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="yerkd", description="The Yerk AI daemon.")
    p.add_argument("--private", help="private folder (default: $YERK_PRIVATE, else the fake example layer)")
    p.add_argument("--state", help="state folder (default: $YERK_STATE, else ~/.local/state/yerk)")
    p.add_argument("--port", type=int, default=0, help="local port for /mcp and /hook (default: yerkd.toml, else free)")
    p.add_argument("--idle-minutes", type=float, default=30.0, help="close a room's process after this long idle")
    p.add_argument("--config-dir", help="use only this account's Claude Code config folder (the contract probes' smoke)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    priv, state = private_dir(a.private), state_dir(a.state)
    claim(state)                                 # before the ledger opens: a second daemon writes nothing
    y = Yerkd(priv, state, port=a.port, idle_minutes=a.idle_minutes,
              only_account=from_dir(a.config_dir) if a.config_dir else None, log_dir=state / "logs")
    (state / "logs").mkdir(exist_ok=True)
    asyncio.run(y.run_discord())
    return 0


if __name__ == "__main__":
    sys.exit(main())
