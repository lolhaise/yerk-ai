#!/usr/bin/env python3
"""Runs the real daemon offline, the way it runs on an install, and checks from outside that every task it starts does
its first piece of work. yerkd's own main() runs Yerkd.run_discord on a scratch copy of the example layer and a scratch
state folder, every long-lived task it starts running as on an install, and only the outside world replaced: Claude Code
by fake_worker_claude.py (fake_claude.py for a room and the meters), Discord's client by an in-process one built on the
tests' (test_discord.Chan, test_slash.Bot and It), systemd by the worker tests' Units, a notify socket of the run's own
and stand-ins on PATH for systemctl, systemd-run and ssh, no bwrap for a meter reading, and no network (a connect past
loopback is refused and counted). It drives the daemon the way a person or a program does, prints one line a check,
state first, and exits 0 only when every check passed. `.claude/skills/verify/SKILL.md` is the recipe around it.

    uv run python tests/verify_daemon.py                  # the fixed checks
    uv run python tests/verify_daemon.py --drive FILE.py  # and FILE's drive(run) before the stop (one changed feature)
    uv run python tests/verify_daemon.py --keep           # leave the scratch folder, its path printed

Never the live install: the scratch folder is a new temporary one (removed at the end unless --keep), the run refuses
to start when its state folder or private layer is the live install's (units/: ~/.local/state/yerk, ~/yerk-private,
and $YERK_STATE, $YERK_PRIVATE as the shell has them), its port is one the kernel hands out free, and HOME, YERK_STATE
and YERK_PRIVATE point into the scratch folder while it runs. Named here, not a test file: pytest collects
test_verify_daemon.py, which runs this once."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import ipaddress
import json
import logging
import os
import runpy
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):                 # the package from this tree, and the tests' fakes beside this file
    if p not in sys.path:
        sys.path.insert(0, p)

import yerk_ai.channels.discord as dmod          # noqa: E402
from yerk_ai import daemon, doctor, jobs, pipeline, usage, words, workers   # noqa: E402
from yerk_ai.channels import page                # noqa: E402
from yerk_ai.config import EXAMPLE_PRIVATE       # noqa: E402
from yerk_ai.daemon import Yerkd                 # noqa: E402

from test_discord import Chan, Msg, Partial      # noqa: E402
from test_slash import Bot, It                   # noqa: E402
from test_workers import Units                   # noqa: E402

FAKE = HERE / "fake_worker_claude.py"            # a worker's claude; a room's and the meters' it hands to fake_claude.py
LIVE = (Path.home() / ".local" / "state" / "yerk", Path.home() / "yerk-private")   # the live install, as units/ names it
OWNER, BOT, GUILD = 7, 99, 1
ROOM, QUIET = "example-room", "example-quiet"    # the example layer's two rooms, given channels 11 and 12
CHANNEL = {ROOM: 11, QUIET: 12}
IDLE_MINUTES = 0.03                              # yerkd --idle-minutes: a room's process closes 1.8 s idle, reaped every 0.9 s
RELAY_IDLE = 6.0                                 # [workers] idle_seconds: a worker's result waits for the room this quiet
INBOX_POLL = 0.5                                 # [imessage] poll_seconds: the inbox check's period
SHORTER = (                                      # (what, how it is set, its own value, the run's): named in the output
    ("Yerkd.keep_meters every", "its parameter's default", 30.0, 0.3),
    ("Pipeline.watch every", "its parameter's default", pipeline.EVERY, 0.5),
    ("workers.PUMP_EVERY", "module constant", workers.PUMP_EVERY, 0.2),
    ("usage.FRESH", "module constant: a reading a second old is a kept one, as one a minute old is on an install",
     usage.FRESH, 1.0),
)
# each task run_discord starts (its coroutine's name) and the check that sees its first piece of work; a task it starts
# that is in neither table fails the run, so a task added later is never left unseen
CHECKS = {"Yerkd.serve_http": "http", "Yerkd.reap": "reaper", "Beat.tick": "beat", "Yerkd.keep_meters": "keeper",
          "Pipeline.watch": "pipeline", "Yerkd.refresh_inbox": "inbox", "DiscordChannel.start": "gateway",
          "Workers._pumper": "pump"}
NOT_CHECKED: dict[str, str] = {}                 # name -> why its first piece of work cannot be seen without a package change
DEADLINE = 20.0                                  # seconds any one wait may take; a run that is well is far under it
LIST_TIMERS = 'case "$*" in *list-timers*) echo "[]"; exit 0;; esac\nexit 1'   # systemd with no job timers installed
READS = ("list-timers", "list-units", "is-active", "show", "status", "is-enabled")   # a systemctl call that changes nothing


def n(count: int, what: str) -> str:
    return f"{count} {what}{'' if count == 1 else 's'}"


def said(epoch: float) -> str:
    """A reset the way Claude Code 2.1.304's /usage says it (test_observe.said)."""
    t = datetime.fromtimestamp(epoch, ZoneInfo("America/Toronto"))
    return f"{t:%b} {t.day}, {t.hour % 12 or 12}{f':{t.minute:02d}' if t.minute else ''}{'am' if t.hour < 12 else 'pm'} (America/Toronto)"


def live_folders() -> list[tuple[Path, str]]:
    """The live install's folders and their kind (a stat of each path, never a read of what is in it)."""
    named = [(Path(os.environ[k]).expanduser(), kind) for k, kind in (("YERK_STATE", "state"), ("YERK_PRIVATE", "private"))
             if os.environ.get(k)]
    return [(p.resolve(), kind) for p, kind in ((LIVE[0], "state"), (LIVE[1], "private"), *named)]


def refusal(state: Path, private: Path) -> str:
    """Why the run must not start on these folders ("" when it may): one of them is a live install's folder, holds one
    (removing the scratch folder would remove it) or is inside one, but for a worker's own folder in a live state folder
    (<state>/workers/<id>/, where a worker keeps its scratch)."""
    for mine, what in ((state.resolve(), "state folder"), (private.resolve(), "private layer")):
        for live, kind in live_folders():
            if mine == live:
                return f"its {what} {mine} is the live install's {kind} folder"
            if live.is_relative_to(mine):
                return f"its {what} {mine} holds the live install's {kind} folder {live}"
            inside = mine.relative_to(live).parts if mine.is_relative_to(live) else ()
            if inside and not (kind == "state" and len(inside) > 2 and inside[0] == "workers"):
                return f"its {what} {mine} is inside the live install's {kind} folder {live}"
    return ""


class Room(Chan):
    """A room's channel as Discord shows it: test_discord's, in guild 1, with the typing signal and an edit by id."""
    def __init__(self, cid: int):
        super().__init__(cid)
        self.guild = NS(id=GUILD, filesize_limit=25 << 20)

    async def typing(self) -> None:
        pass

    def get_partial_message(self, mid: int) -> Partial:
        return Edited(self, mid)

    def text(self, mid: str) -> str:
        """A message as a person sees it now: its last edit, else what was posted."""
        m = next((m for m in self.sent if str(m.id) == str(mid)), None)
        return "" if m is None else (m.edits[-1] if m.edits else m.content)


class Edited(Partial):
    async def edit(self, content: str | None = None) -> None:
        for m in self.ch.sent:
            if m.id == int(self.mid):
                m.edits.append(content or "")


class Gateway(Bot):
    """discord.Client's stand-in: test_slash's Bot with the rooms' channels and the gateway connection start makes (ready
    once, then up until close)."""
    def __init__(self) -> None:
        super().__init__({cid: GUILD for cid in CHANNEL.values()})
        self.chans = {cid: Room(cid) for cid in CHANNEL.values()}
        self.user, self.handlers, self.closed = NS(id=BOT, name="bot"), {}, None

    def event(self, fn):
        self.handlers[fn.__name__] = fn
        return fn

    async def fetch_channel(self, cid: int) -> Room:
        return self.chans[cid]

    async def start(self, token: str) -> None:
        self.closed = asyncio.Event()
        await self.handlers["on_ready"]()
        await self.closed.wait()

    async def close(self) -> None:
        if self.closed is not None:
            self.closed.set()


class Run:
    def __init__(self, root: Path, verbose: bool = False, drive: str = ""):
        self.root, self.verbose, self.drive_file = root, verbose, drive
        self.private, self.state = root / "private", root / "state"
        self.lines: list[tuple[bool | None, str]] = []
        self.up, self.ended = threading.Event(), threading.Event()
        self.box: dict = {}
        self.ticks: list[str] = []
        self.pumps: list[str] = []
        self.reached: list[str] = []
        self.notes: list[str] = []
        self.exit: object = None
        self.began = time.monotonic()
        self.gateway = Gateway()
        self.units = Units()

    # what the run prints: one line a check, state first
    def say(self, ok: bool | None, what: str, saw: str = "") -> bool | None:
        mark = words.DONE if ok is True else words.NOT_DONE if ok is False else words.NOTHING
        self.lines.append((ok, what))
        print(f"{mark} {what}" + (f": {saw}" if saw else ""), flush=True)
        return ok

    def note(self, text: str) -> None:
        print(f"· {text}", flush=True)

    def tail(self, path: Path, n: int = 6) -> str:
        try:
            got = path.read_text(errors="replace").splitlines()[-n:]
        except OSError as e:
            return f"({path.name} unreadable: {e})"
        return "\n    " + "\n    ".join(got) if got else f"({path.name} empty)"

    def wait(self, cond, deadline: float = DEADLINE, every: float = 0.05, after: bool = False):
        """cond() until it is truthy or the deadline passes (or, before the stop, the daemon ends); its last value either
        way, never a fixed sleep."""
        end = time.monotonic() + deadline
        while True:
            got = cond()
            if got or time.monotonic() > end or self.ended.is_set() and not after:
                return got
            self.read_notify()
            time.sleep(every)

    # the scratch install
    def build(self) -> str:
        if why := refusal(self.state, self.private):
            return why
        shutil.copytree(EXAMPLE_PRIVATE, self.private)
        rooms = (self.private / "rooms.toml").read_text()
        for name in (ROOM, QUIET):
            rooms = rooms.replace('"discord:CHANNEL_ID"', f'"discord:{CHANNEL[name]}"', 1)
        (self.private / "rooms.toml").write_text(rooms)
        accounts = tomllib.loads((self.private / "accounts.toml").read_text())["accounts"]
        self.accounts = list(accounts)
        (self.private / "accounts.toml").write_text("[accounts]\n" + "".join(
            f'{n} = {{ config_dir = "{self.root / f"cfg-{n}"}", order = {a.get("order", i + 1)} }}\n'
            for i, (n, a) in enumerate(accounts.items())))
        (self.root / "usage").mkdir()
        soon, week = (time.time() // 600) * 600 + 3 * 3600, (time.time() // 3600) * 3600 + 80 * 3600
        for i, n in enumerate(self.accounts):
            (self.root / f"cfg-{n}").mkdir()
            (self.root / "usage" / f"cfg-{n}.txt").write_text(
                f"Current session: {20 + i}% used · resets {said(soon)}\n"
                f"Current week (all models): {40 + i}% used · resets {said(week)}\n")
        token = self.private / "discord.token"
        token.write_text("not-a-token\n")
        token.chmod(0o600)
        (self.private / "yerkd.toml").write_text(
            f'[discord]\ntoken_file = "discord.token"\nowner = {OWNER}\n\n[workers]\nidle_seconds = {RELAY_IDLE}\n\n'
            f'[imessage]\nhost = "mac.example.test"\npoll_seconds = {INBOX_POLL}\n')
        (self.root / "home").mkdir()
        self.state.mkdir()
        bin_ = self.root / "bin"
        bin_.mkdir()
        self.outside = self.root / "outside.log"
        for tool, then in (("systemctl", LIST_TIMERS), ("systemd-run", "exit 1"), ("systemd-analyze", "exit 1"),
                           ("ssh", "cat >/dev/null; exit 0")):     # ssh: the Mac, answering with no texts
            (bin_ / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{self.outside}"\n{then}\n')
            (bin_ / tool).chmod(0o755)
        return ""

    def patch(self) -> None:
        """The outside world swapped for the tests' stand-ins, and the periods SHORTER names; undone by unpatch."""
        self.saved_env = dict(os.environ)
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        name = f"yerk-verify-{os.getpid()}-{self.root.name}"      # abstract, as systemd's own: no path, so no length cap
        self.notify.bind("\0" + name)
        self.notify.setblocking(False)
        self.said_notify: list[str] = []
        os.environ.update({
            "YERK_CLAUDE": str(FAKE), "FAKE_CLAUDE_USAGE_DIR": str(self.root / "usage"),
            "FAKE_CLAUDE_RECORD": str(self.root / "fake-claude.jsonl"), "FAKE_CLAUDE_MARK": str(self.root / "mark"),
            "NOTIFY_SOCKET": "@" + name, "WATCHDOG_USEC": "2000000",
            "YERK_STATE": str(self.state), "YERK_PRIVATE": str(self.private), "HOME": str(self.root / "home"),
            "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"})
        self.undo: list[tuple[object, str, object]] = []

        def put(obj, name, value):
            self.undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)
        put(dmod.discord, "Client", lambda **kw: self.gateway)
        put(workers, "Systemd", self.units.keep)           # the stand-in keeps the daemon's state folder and hidden paths
        put(doctor, "systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 0, "enabled\n" * (len(a) - 1), ""))
        put(page, "tailnet", lambda: "")
        put(page, "wsl", lambda: False)
        put(page, "POWERSHELL", str(self.root / "no-powershell"))
        put(usage, "BWRAP", str(self.root / "no-bwrap"))          # a meter reading runs no bwrap of the box's (conftest's no_bwrap)
        put(Yerkd.keep_meters, "__defaults__", (SHORTER[0][3],))
        put(pipeline.Pipeline.watch, "__defaults__", (SHORTER[1][3],))
        put(workers, "PUMP_EVERY", SHORTER[2][3])
        put(usage, "FRESH", SHORTER[3][3])
        run, real_until, real_tick = self, Yerkd.until_stopped, pipeline.Pipeline.tick

        async def until_stopped(self, tasks, close):             # watched only: the daemon's own stop path runs
            run.box.update(d=self, tasks=list(tasks), loop=asyncio.get_running_loop(),
                           pump=[t for t in self.workers.tasks if not t.done()])
            names = [t.get_coro().cr_code.co_qualname for t in tasks]
            run.up.set()
            try:
                return await real_until(self, tasks, close)
            finally:                                             # when its wait ended, and which tasks had ended by then
                run.box.update(returned=time.monotonic(), done_then=[n for n, t in zip(names, tasks) if t.done()])

        def tick(self):                                          # who asked the pipeline's round: its watch, or the connect
            run.ticks.append(sys._getframe(1).f_code.co_qualname)
            return real_tick(self)
        real_pump = workers.Workers.pump

        def pump(self, name):                                    # who looks for a quiet room while a relay waits there
            if self.waiting.get(name):
                run.pumps.append(sys._getframe(1).f_code.co_qualname)
            return real_pump(self, name)
        put(Yerkd, "until_stopped", until_stopped)
        put(pipeline.Pipeline, "tick", tick)
        put(workers.Workers, "pump", pump)
        real_connect, real_gai = socket.socket.connect, socket.getaddrinfo

        def near(host) -> bool:
            try:
                return host in (None, "localhost") or ipaddress.ip_address(str(host).split("%")[0]).is_loopback
            except ValueError:
                return False

        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6) and not near(address[0]):
                run.reached.append(f"connect {address[0]}:{address[1]}")
                raise OSError(errno.ENETUNREACH, "no network in the offline run")
            return real_connect(sock, address)

        def connect_ex(sock, address):
            try:
                connect(sock, address)
                return 0
            except OSError as e:
                return e.errno or errno.ECONNREFUSED

        def getaddrinfo(host, *a, **kw):
            if not near(host):
                run.reached.append(f"resolve {host}")
                raise socket.gaierror(socket.EAI_NONAME, "no network in the offline run")
            return real_gai(host, *a, **kw)
        put(socket.socket, "connect", connect)
        put(socket.socket, "connect_ex", connect_ex)
        put(socket, "getaddrinfo", getaddrinfo)
        self.log = self.root / "yerkd.log"
        handler = logging.FileHandler(self.log)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
        logging.getLogger().addHandler(handler)          # before main()'s basicConfig, so the journal is this file
        logging.getLogger().setLevel(logging.INFO)
        if self.verbose:
            logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))
        self.handler = handler

    def unpatch(self) -> None:
        for obj, name, value in reversed(self.undo):
            setattr(obj, name, value)
        logging.getLogger().removeHandler(self.handler)
        self.handler.close()
        os.environ.clear()
        os.environ.update(self.saved_env)
        self.notify.close()

    def read_notify(self) -> list[str]:
        while True:
            try:
                self.said_notify += self.notify.recv(4096).decode(errors="replace").split("\n")
            except (BlockingIOError, OSError):
                return self.said_notify

    # the daemon, on this thread (its signal handlers want the main one); the driver beside it
    def go(self) -> int:
        if why := self.build():
            self.say(False, "refused to start", why)
            return 2
        self.note(f"scratch install: {self.root} (state, private layer, Claude Code config folders, HOME), none of them "
                  f"the live install's ({', '.join(str(p) for p, _ in live_folders())})")
        self.patch()
        self.note("outside world: Claude Code fake_worker_claude.py and fake_claude.py, Discord an in-process gateway, "
                  "systemd the worker tests' Units and a notify socket, systemctl, systemd-run and ssh stand-ins on PATH")
        for what, how, was, now in SHORTER:
            self.note(f"shortened: {what} {was:g} s to {now:g} s ({how})")
        self.note(f"shortened: yerkd --idle-minutes {IDLE_MINUTES:g} (a room's process closes {IDLE_MINUTES * 60:g} s "
                  f"idle), [workers] idle_seconds = {RELAY_IDLE:g}, [imessage] poll_seconds = {INBOX_POLL:g}")
        threads = set(threading.enumerate())
        driver = threading.Thread(target=self.driver, name="verify-driver", daemon=True)
        driver.start()
        try:
            self.exit = daemon.main(["--private", str(self.private), "--state", str(self.state),
                                     "--idle-minutes", str(IDLE_MINUTES)])
        except BaseException as e:                       # SystemExit at the start, or a task's error ending it
            self.exit = e
            self.daemon_error = "".join(traceback.format_exception_only(type(e), e)).strip()
        self.ended.set()
        self.stopped_at = time.monotonic()
        driver.join(DEADLINE)
        try:
            self.after(threads)
        finally:
            self.unpatch()
        bad = [w for ok, w in self.lines if ok is False]
        took = time.monotonic() - self.began
        if bad:
            self.say(False, f"{len(bad)} of {len(self.lines)} checks failed in {took:.1f} s", ", ".join(bad))
            return 1
        unseen = sum(ok is None for ok, _ in self.lines)
        self.say(True, f"all {len(self.lines) - unseen} checks passed in {took:.1f} s"
                 + (f", {unseen} not checked" if unseen else ""))
        return 0

    def driver(self) -> None:
        try:
            if not self.wait(lambda: self.up.is_set(), 30):
                err = getattr(self, "daemon_error", "") or "it never reached its stop wait in 30 s"
                self.say(False, "the daemon started", f"{err}; its log:{self.tail(self.log)}")
                if not self.ended.is_set():              # stuck in its start, no handler for a SIGTERM: end here
                    shutil.rmtree(self.root, ignore_errors=True)
                    os._exit(1)
                return
            self.d, self.loop, self.dc = self.box["d"], self.box["loop"], self.box["d"].link
            self.checks()
            if self.drive_file:
                self.drive()
        except Exception:
            self.say(False, "the run's own driver", traceback.format_exc(limit=4).strip().replace("\n", " | "))
        finally:
            self.stop()

    def on_loop(self, coro, timeout: float = DEADLINE):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # what a person or a program does, and what each sees
    def message(self, name: str, text: str, mid: int):
        """The owner's message in a room's channel, as the gateway hands it over; the room's posts after it."""
        ch = self.gateway.chans[CHANNEL[name]]
        before = len(ch.sent)
        self.on_loop(self.dc.on_message(Msg(ch, mid, text, author=OWNER)))
        return ch, before

    def slash(self, name: str, command: str, **options) -> It:
        it = It(command, user=OWNER, channel=CHANNEL[name], **options)
        self.on_loop(self.dc.on_interaction(it))
        return it

    def worker(self, name: str, brief: str, tasks: list[str]) -> dict:
        """A worker started the way `yerk worker start` asks the daemon (/control); its answer, or why there was none.
        The brief's FAKE line says what fake_worker_claude.py does (ticks, report, exit)."""
        f = self.root / f"brief-{time.monotonic_ns()}.md"
        f.write_text(brief)
        try:
            return jobs.post_control(self.state, {"worker": {
                "action": "start", "id": "", "room": name, "tasks": tasks, "cap_minutes": 2, "account": "", "model": "",
                "effort": "", "brief_file": str(f)}}, 120)
        except OSError as e:
            return {"ok": False, "error": f"no answer: {e}"}

    def ledger(self, sql: str, *args) -> list[dict]:
        db = sqlite3.connect(f"file:{self.state / 'ledger.sqlite'}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(sql, args)]
        finally:
            db.close()

    def http(self, path: str, payload: dict) -> tuple[int, str]:
        import urllib.error
        import urllib.request
        conf = json.loads((self.state / "yerkd.json").read_text())
        secret = Path(conf["secret_file"]).read_text().strip()
        req = urllib.request.Request(f"http://127.0.0.1:{conf['port']}{path}", data=json.dumps(payload).encode(),
                                     headers={"x-yerk-secret": secret, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")
        except OSError as e:
            return 0, f"no answer on port {conf['port']}: {e}"

    def checks(self) -> None:
        d, seen = self.d, {}
        started = [t.get_coro().cr_code.co_qualname for t in self.box["tasks"]]
        pumps = [t.get_coro().cr_code.co_qualname for t in self.box["pump"]]
        self.started = started + [p for p in pumps if p == "Workers._pumper"][:1]
        self.say(True, f"run_discord started {len(started)} long-lived tasks", ", ".join(started)
                 + (", and workers.begin the workers' pump" if "Workers._pumper" in pumps else ""))
        for name in started:
            if name not in CHECKS and name not in NOT_CHECKED:
                self.say(False, f"task {name}", "run_discord starts it and this run has no check of its first piece of "
                         "work: add one to CHECKS in tests/verify_daemon.py, or name it in NOT_CHECKED with why")
        for name, why in NOT_CHECKED.items():
            if name in self.started:
                self.say(None, f"not checked: {name}", why)
        for name in CHECKS:                              # its check below still runs, and says what it saw
            if name not in self.started:
                self.say(False, f"task {name}", "not among the tasks the daemon started")

        # the gateway: ready, and this install's slash commands registered
        ok = self.wait(lambda: self.dc.connected and self.gateway.puts)
        if seen.get("gateway") is None:
            self.say(bool(ok), "the gateway connection: ready, slash commands registered",
                     f"{len(self.gateway.puts[0][2])} commands in guild {self.gateway.puts[0][1]}" if ok
                     else f"connected {self.dc.connected}, registrations {self.gateway.puts}; log:{self.tail(self.log)}")

        # a program: /status over the HTTP port
        if seen.get("http") is None:
            code, body = self.wait(lambda: (r := self.http("/status", {}))[0] == 200 and r, 5) or self.http("/status", {})
            try:
                got = json.loads(body) if code == 200 else {}
            except ValueError:
                got = {}
            names = sorted(r.get("room") or r.get("name") or "" for r in got.get("rooms", []))
            ok = code == 200 and set(CHANNEL) <= set(names)
            self.say(ok, "/status over the daemon's HTTP port answers with its rooms",
                     f"{code}, rooms {', '.join(names)}, connected {got.get('connected')}" if ok
                     else f"answer {code}: {body[:300]}")

        # a person: a message in a room is answered there, and the turn is on the ledger
        t0 = time.monotonic()
        ch, before = self.message(ROOM, "hello from the offline run", 5000000001)
        other = self.gateway.chans[CHANNEL[QUIET]]
        want = "echo: hello from the offline run"
        got = self.wait(lambda: next((m for m in ch.sent[before:] if want in (m.edits[-1] if m.edits else m.content)), None))
        row = (self.ledger("SELECT * FROM inbound WHERE message_id = ?", "5000000001") or [{}])[0]
        turn = (self.ledger("SELECT * FROM turns WHERE id = ?", row.get("turn_id") or -1) or [{}])[0]
        if got and not (row.get("state") == "answered" and turn):
            row = self.wait(lambda: (r := (self.ledger("SELECT * FROM inbound WHERE message_id = ?", "5000000001") or [{}])[0])
                            .get("state") == "answered" and r, 5) or row
            turn = (self.ledger("SELECT * FROM turns WHERE id = ?", row.get("turn_id") or -1) or [{}])[0]
        ok = bool(got) and row.get("state") == "answered" and bool(turn) and not other.sent
        self.say(ok, f"a message in {ROOM} is answered there, the turn on the ledger",
                 f"{want!r} in {time.monotonic() - t0:.1f} s, inbound {row.get('state')}, turn {turn.get('id')} "
                 f"{turn.get('outcome')}, nothing in {QUIET}" if ok else
                 f"posted there {[m.edits[-1] if m.edits else m.content for m in ch.sent[before:]][-3:]}, in {QUIET} "
                 f"{len(other.sent)}, inbound row {row}, turn {turn}; the fake's starts:{self.tail(self.root / 'fake-claude.jsonl', 3)}"
                 f"; log:{self.tail(self.log)}")
        room = d.rooms.get(ROOM)
        self.pids = {room.rp.proc.pid} if room is not None and room.rp is not None and room.rp.proc is not None else set()

        # a worker, started through the daemon the way `yerk worker start` asks (its result will wait for the room)
        asked = self.worker(ROOM, 'FAKE ticks=2 gap=0.1 report="both steps done in the offline run"\nTick both steps, then report.\n',
                            ["first step", "second step"])
        wid = str(asked.get("id") or "")
        if asked.get("tasks"):                           # the owner again as the worker ticks: its result waits for the room
            self.wait(lambda: "- [x]" in Path(asked["tasks"]).read_text() if Path(asked["tasks"]).is_file() else False, 15)
            self.message(ROOM, "still here while the worker runs", 5000000002)
        self.relay_seen: list[str] = []
        watcher = threading.Thread(target=self.watch_relay, args=(wid,), name="verify-relay", daemon=True)
        if wid:
            watcher.start()

        # the meters' keeper: a reading filed for each account
        if seen.get("keeper") is None:
            kept = lambda: usage.last(self.state)       # noqa: E731
            ok = self.wait(lambda: all(n in kept() for n in self.accounts), 10)
            have = kept()
            self.say(bool(ok) and all("error" not in have[n] for n in self.accounts),
                     "the meters' keeper filed a reading for each account in usage.json",
                     ", ".join(f"{n} {have[n].get('five_hour', {}).get('pct')}% of 5 h" for n in self.accounts) if ok
                     else f"usage.json: {json.dumps(have)[:300] or 'none'}; log:{self.tail(self.log)}")

        # the beat: READY=1 once the port listened, then the watchdog's beats
        if seen.get("beat") is None:
            ok = self.wait(lambda: "READY=1" in self.read_notify() and "WATCHDOG=1" in self.read_notify(), 5)
            self.say(bool(ok), "the beat: READY=1 to systemd, then WATCHDOG=1",
                     f"{n(self.said_notify.count('WATCHDOG=1'), 'beat')} so far" if ok
                     else f"the notify socket got {sorted(set(x for x in self.said_notify if x)) or 'nothing'}")

        # the pipeline's watch: its own round, not only the one the connect asks
        if seen.get("pipeline") is None:
            ok = self.wait(lambda: "Pipeline.watch" in self.ticks, 5)
            self.say(bool(ok), "the pipeline's watch ran its first round",
                     f"Pipeline.tick asked by {', '.join(dict.fromkeys(self.ticks))}" if ok
                     else f"Pipeline.tick asked only by {self.ticks or 'nobody'}")

        # the inbox check: the Mac's newest texts copied to the box (the stand-in Mac answers with none)
        if seen.get("inbox") is None:
            copy = self.state / "texts" / "inbox.json"
            ok = self.wait(lambda: copy.is_file(), 5)
            self.say(bool(ok), "the inbox check copied the Mac's texts to the box",
                     f"texts/inbox.json, {n(len(json.loads(copy.read_text())['messages']), 'message')}, ssh asked "
                     f"{n(self.tail(self.outside, 50).count('ssh '), 'time')}" if ok
                     else f"no texts/inbox.json; ssh asked:{self.tail(self.outside)}")

        # the slash /status in a room: the first answer from what is on hand, then the live card edited in once
        self.wait(lambda: all(time.time() - float(usage.last(self.state).get(n, {}).get("at") or 0) > usage.FRESH
                              for n in self.accounts), 5)
        self.status_it = it = self.slash(ROOM, "status")
        ok = self.wait(lambda: [s for s in it.said if s[0] == "edit"], 15)
        first = [s for s in it.said if s[0] in ("message", "followup")]
        good = bool(ok) and len(first) == 1 and words.METERS_ASKED in first[0][1] and first[0][2] is True \
            and words.METERS_ASKED not in ok[0][1]
        self.say(good, f"slash /status in {ROOM}: the first answer, then one edit",
                 f"first {first[0][1].splitlines()[0]!r} with {words.METERS_ASKED!r}, then the live card" if good
                 else f"answers {it.said}")

        # the worker ends on the fakes, with its card and its exit record
        if not asked.get("ok"):
            self.say(False, "a worker started through the daemon", f"/control answered {asked}")
        else:
            end = self.wait(lambda: (r := (self.ledger("SELECT * FROM workers WHERE id = ?", wid) or [{}])[0]).get("finished")
                            and (Path(r["staging"]) / workers.EXIT).is_file() and r, 20) or \
                (self.ledger("SELECT * FROM workers WHERE id = ?", wid) or [{}])[0]
            self.wait(lambda: ch.text((self.ledger("SELECT card FROM workers WHERE id = ?", wid) or [{}])[0].get("card") or "")
                      .startswith(words.DONE), 10)        # the card settles just after the exit record
            end = (self.ledger("SELECT * FROM workers WHERE id = ?", wid) or [{}])[0] or end
            card = ch.text(end.get("card") or "") if end else ""
            record = (Path(end["staging"]) / workers.EXIT) if end.get("staging") else None
            ok = bool(end.get("finished")) and record is not None and record.is_file() and card.startswith(words.DONE)
            self.say(ok, "a worker started through the daemon ends with its card and its exit record",
                     f"{wid} {end.get('exit')}, card {card.splitlines()[0]!r}, exit.json "
                     f"{json.loads(record.read_text()).get('result') if ok else ''}" if ok else
                     f"row {dict((k, end.get(k)) for k in ('finished', 'exit', 'exit_detail', 'card', 'relay'))}, card "
                     f"{card!r}, exit record {'there' if record and record.is_file() else 'missing'}; the worker's "
                     f"stderr:{self.tail(Path(end.get('staging') or self.root) / 'stderr.log')}")

            # the workers' pump: the result waited for the room to go quiet, then was relayed into it
            if seen.get("pump") is None:
                ok = self.wait(lambda: self.relay_seen and self.relay_seen[-1].startswith("relayed"), 20)
                watcher.join(DEADLINE)
                notes = self.relay_seen
                waited = any(x.startswith("waiting") for x in notes)
                inbound = (self.ledger("SELECT * FROM inbound WHERE message_id = ?", f"relay-{wid}") or [{}])[0]
                posted = [ch.text(i) for i in str(inbound.get("reply_ids") or "").split(",") if i]
                looked = self.pumps.count("Workers._pumper")
                good = bool(ok) and waited and bool(posted) and all(posted) and looked > 0
                self.say(good, "the workers' pump looked for a quiet room while the worker's result waited, and it was relayed",
                         f"it waited, {next(x for x in notes if 'waiting' in x).split('waiting: ')[-1]}, the pump looked "
                         f"{n(looked, 'time')}, then {n(len(posted), 'message')} in {ROOM}" if good else
                         f"relay seen as {list(dict.fromkeys(notes))[-4:]}, posted {posted}, the pump looked {n(looked, 'time')} "
                         f"(a waiting relay was looked at by {sorted(set(self.pumps)) or 'nobody'})")

        # the reaper: a room's process closed once idle
        if seen.get("reaper") is None:
            closed = "idle process closed"
            ok = self.wait(lambda: room.rp is None and f"room {ROOM}: {closed}" in self.log.read_text()
                           and all(not alive(p) for p in self.pids), 15)
            self.say(bool(ok), f"the reaper closed {ROOM}'s process once idle",
                     f"process {', '.join(map(str, sorted(self.pids))) or '?'} ended, the journal says {closed!r}" if ok
                     else f"process {self.pids} alive {[alive(p) for p in self.pids]}, room process "
                          f"{'open' if room.rp else 'closed'}; log:{self.tail(self.log)}")

    def watch_relay(self, wid: str) -> None:
        """Every state the worker's relay passes through, from its start until it is relayed (the ledger keeps the last)."""
        while not self.ended.is_set():
            r = (self.ledger("SELECT relay, relay_note FROM workers WHERE id = ?", wid) or [{}])[0]
            x = f"{r.get('relay')}: {r.get('relay_note')}"
            if not self.relay_seen or self.relay_seen[-1] != x:
                self.relay_seen.append(x)
            if r.get("relay") == "relayed" or self.ended.wait(0.02):
                return

    def drive(self) -> None:
        """--drive FILE: its drive(run) against this daemon after the fixed checks, before the stop. It has run.message,
        run.slash, run.worker, run.http, run.ledger, run.wait, run.say, run.state, run.private, run.d (the Yerkd) and
        run.gateway.chans (each room's channel: sent, text(id))."""
        ns = runpy.run_path(self.drive_file)
        ns["drive"](self)

    def stop(self) -> None:
        """The daemon's own stop path: SIGTERM, as systemd sends it, to this process, whose loop took the signal."""
        if self.ended.is_set():
            return
        self.stop_sent = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)

    def after(self, threads: set[threading.Thread]) -> None:
        if not self.up.is_set():
            return
        took = self.stopped_at - getattr(self, "stop_sent", self.stopped_at)
        if self.box.get("returned", self.stopped_at) < getattr(self, "stop_sent", float("inf")):
            self.say(False, "a long-lived task ended by itself, which ends the daemon",       # until_stopped: the first to end
                     f"{', '.join(self.box.get('done_then') or ['none seen'])} returned or raised before any stop was sent")
        tasks = self.box["tasks"] + self.box["pump"]
        left = [t.get_coro().cr_code.co_qualname for t in tasks if not t.done()]
        said = self.read_notify()
        ok = self.exit == 0 and not left and "STOPPING=1" in said
        self.say(ok, "the stop: SIGTERM ends every task through the daemon's own path",
                 f"main() returned 0 {took:.1f} s after SIGTERM, STOPPING=1 to systemd, {len(tasks)} tasks ended" if ok
                 else f"main() gave {self.exit!r} {getattr(self, 'daemon_error', '')}, tasks still running {left}, "
                      f"STOPPING=1 {'sent' if 'STOPPING=1' in said else 'never sent'}; log:{self.tail(self.log)}")
        if hasattr(self, "status_it"):
            edits = [s for s in self.status_it.said if s[0] == "edit"]
            if len(edits) != 1:
                self.say(False, "slash /status: exactly one edit, counted after the stop", f"{len(edits)} edits")
        units = list(self.units.procs)                  # a worker's unit outlives the daemon, as under systemd: waited for
        if not self.wait(lambda: not self.units.procs, DEADLINE, after=True):
            for unit in list(self.units.procs):
                with contextlib.suppress(OSError):
                    self.units.stop(unit)
            self.say(False, "every worker unit ended", f"still running after {DEADLINE:g} s, stopped: {', '.join(units)}")
        mine = lambda: [t.name for t in threading.enumerate() if t not in threads and t.is_alive()   # noqa: E731
                        and t is not threading.current_thread()]
        pids = lambda: processes(self.root)             # noqa: E731
        self.wait(lambda: not mine() and not pids(), 10, after=True)
        threads_left, procs_left = mine(), pids()
        self.say(not threads_left and not procs_left, "nothing of the run is left: no thread, no process",
                 "" if not threads_left and not procs_left else f"threads {threads_left}, processes {procs_left}")
        calls = self.outside.read_text().splitlines() if self.outside.is_file() else []
        asked = [c for c in calls if c.startswith("systemctl ")]
        unwanted = [c for c in calls if c.startswith(("systemd-run", "systemd-analyze"))
                    or c.startswith("systemctl ") and not any(f" {r} " in f"{c} " for r in READS)]
        reads = sorted({next((r for r in READS if f" {r} " in f"{c} "), c) for c in asked} - {""})
        self.say(not self.reached and not unwanted, "the outside world stayed out: no network, systemd and the Mac stand-ins only",
                 f"no connection past loopback; systemctl {', '.join(reads) or 'never'} asked {n(len(asked), 'time')}, "
                 f"the stand-in answering as systemd with no timers; ssh to the stand-in Mac {n(sum(c.startswith('ssh ') for c in calls), 'time')}"
                 if not self.reached and not unwanted else f"network {self.reached[:5]}, a change asked of systemd {unwanted[:5]}")
        for left in procs_left:                          # said above; never left behind
            with contextlib.suppress(OSError):
                os.kill(int(left.split()[0]), signal.SIGKILL)


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def processes(root: Path) -> list[str]:
    """Every live process of the run: a child of this one, or one whose working folder or command line is in the
    scratch folder (a worker's unit is its own session, and an orphan is the init's)."""
    me, out = os.getpid(), []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            stat = (d / "stat").read_text()
            parent, state = int(stat.rsplit(")", 1)[1].split()[1]), stat.rsplit(")", 1)[1].split()[0]
            cwd = os.readlink(d / "cwd")
            argv = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, ValueError, IndexError):
            continue
        if state != "Z" and (parent == me or cwd.startswith(str(root)) or str(root) in argv):
            out.append(f"{d.name} {argv[:80]}")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="verify_daemon", description=__doc__.split("\n\n")[0])
    p.add_argument("--drive", default="", help="a Python file whose drive(run) runs against the daemon before the stop")
    p.add_argument("--keep", action="store_true", help="leave the scratch folder (its path is printed)")
    p.add_argument("--verbose", action="store_true", help="the daemon's journal on stderr too")
    a = p.parse_args(argv)
    parent = Path(tempfile.gettempdir())             # checked before anything is made there
    if why := refusal(parent / "yerk-verify-" / "state", parent / "yerk-verify-" / "private"):
        print(f"{words.NOT_DONE} refused to start: {why}", flush=True)
        return 2
    root = Path(tempfile.mkdtemp(prefix="yerk-verify-")).resolve()
    try:
        return Run(root, a.verbose, a.drive).go()
    finally:
        if a.keep:
            print(f"· kept: {root}", flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
