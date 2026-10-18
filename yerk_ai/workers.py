"""Workers: a heavy job moves out of the room into a worker, a separate Claude Code run with its own progress
card that reports back when it ends. A worker is a headless `claude -p` run owned by Yerk AI (DESIGN 6,
daemon restarts.

- Start: a staging folder <root>/<id> (BRIEF.md, PROMPT.md, worker.json, TASKS.md when the call
  gives the items), a `workers` ledger row, the card in the room, then the unit: `claude -p` under one
  named account with the prompt on stdin, stdout and stderr to files in the folder, a cap
  (RuntimeMaxSec, 65 minutes unless the call or [workers] says otherwise) and its own cgroup, so a
  stop takes every process with it. The unit's environment names its account's token file and no
  other; its first step (`python -m yerk_ai.workers run`) reads it the way a room does
  (accounts.account_env) and starts claude with an allowlisted environment. A worker gets no room
  session, no MCP server (so no `worker` tool: no workers spawning workers) and no subagent tool.
- The card: one per worker, in the room that started it or the room named in the call: its state,
  the time so far against the cap ("3 min of 15", refreshed each minute), then one row per TASKS.md
  line, ticked as the worker ticks them. An inotify watch on the folder wakes the daemon when a file
  there is written; the adapter edits at most once a second, the latest text winning, as it does
  for turn cards. Its words come from words.py (STYLE.md): no path and no exit code in a room.
- Completion: the unit's ExecStopPost writes exit.json (systemd's SERVICE_RESULT, EXIT_CODE and
  EXIT_STATUS) and the same watch sees it. The card settles with the outcome in plain words
  (finished clean, stopped: timed out, stopped by the owner, died: why, lost), the report's first
  line and "report saved" with `yerk worker report <id>`, where the path and the unit's exit live;
  then, when the room is idle (no human message for 60 s and no turn running), ONE relay turn in the
  room's own session tells the outcome under 1,500 characters ending with that line; otherwise the
  relay is skipped and ledgered with why. The raw report never goes to the room.
- A daemon start re-attaches from the ledger and the unit list: a running unit gets its watch and its
  card back, one that ended while the daemon was away completes, one that vanished settles as lost.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from . import words
from .accounts import Account, Cooldowns, account_env, pick
from .card import ROWS_SHOWN, short
from .config import PACKAGE
from .engine import claude_bin
from .ledger import Ledger
from .room import Inbound

log = logging.getLogger("yerk.workers")
UNIT = "yerk-worker-"
EXIT, TASKS, REPORT = "exit.json", "TASKS.md", "REPORT.md"
TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite"]
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR",
            "XDG_RUNTIME_DIR")                 # what claude gets from the unit's environment; the rest stays out
TICK = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.+?)\s*$")


@dataclass
class Settings:
    """[workers] in the private yerkd.toml; every key optional."""
    root: Path
    cap_minutes: float = 65.0
    model: str = ""                     # "" = the room's own model
    account: str = ""                   # "" = the first account in order that is not resting
    tools: list[str] = field(default_factory=lambda: list(TOOLS))
    permission_mode: str = "bypassPermissions"     # headless: nobody is there to answer a prompt
    idle_seconds: float = 60.0          # the relay runs only after this long without a human message
    relay_message: int = 1500           # budgets.toml [relay]
    relay_reply: int = 1500


def settings(state: Path, conf: dict[str, Any], budgets: dict[str, Any] | None = None) -> Settings:
    relay = (budgets or {}).get("relay", {})
    return Settings(root=Path(str(conf.get("root", state / "workers"))).expanduser(),
                    cap_minutes=float(conf.get("cap_minutes", 65.0)), model=str(conf.get("model", "")),
                    account=str(conf.get("account", "")), tools=list(conf.get("tools", TOOLS)),
                    permission_mode=str(conf.get("permission_mode", "bypassPermissions")),
                    idle_seconds=float(conf.get("idle_seconds", 60.0)),
                    relay_message=int(relay.get("message", 1500)), relay_reply=int(relay.get("reply", 1500)))


# the unit
class Launcher(Protocol):
    def start(self, unit: str, argv: list[str], env: dict[str, str], cwd: Path, out: Path, err: Path,
              cap: float, post: list[str]) -> None: ...
    def stop(self, unit: str) -> None: ...
    def running(self) -> set[str]: ...


def _exec_line(argv: list[str]) -> str:
    """argv as one unit-file command line: quoted, with systemd's % and $ escaped."""
    return " ".join(shlex.quote(a).replace("%", "%%").replace("$", "$$") for a in argv)


class Systemd:
    """systemd-run --user: the unit outlives the daemon; --collect unloads it once it has ended
    (exit.json is the record)."""

    def start(self, unit: str, argv: list[str], env: dict[str, str], cwd: Path, out: Path, err: Path,
              cap: float, post: list[str]) -> None:
        cmd = ["systemd-run", "--user", "--unit", unit, "--collect", "--quiet", "--working-directory", str(cwd),
               "--description", f"Yerk AI worker {unit.removeprefix(UNIT)}",
               "-p", f"RuntimeMaxSec={max(1, int(cap))}", "-p", f"StandardOutput=append:{out}",
               "-p", f"StandardError=append:{err}", "-p", f"ExecStopPost={_exec_line(post)}",
               "-p", "KillMode=control-group", "-p", "TimeoutStopSec=20",
               *[f"--setenv={k}={v}" for k, v in env.items()], "--", *argv]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if p.returncode:
            raise OSError(f"systemd-run exited {p.returncode}: {p.stderr.strip()[:300]}")

    def stop(self, unit: str) -> None:
        subprocess.run(["systemctl", "--user", "stop", "--no-block", f"{unit}.service"], capture_output=True,
                       timeout=60)

    def running(self) -> set[str]:
        p = subprocess.run(["systemctl", "--user", "list-units", f"{UNIT}*", "--all", "--plain", "--no-legend",
                            "--no-pager"], capture_output=True, text=True, timeout=60)
        if p.returncode:                    # unreadable is not empty: nobody is declared lost on it
            raise OSError(f"systemctl --user list-units exited {p.returncode}: {p.stderr.strip()[:200]}")
        rows = [x.split() for x in p.stdout.splitlines() if x.strip()]
        return {r[0].removesuffix(".service") for r in rows if len(r) > 2 and r[2] not in ("inactive", "failed")}


def run(spec_path: Path) -> int:
    """Inside the unit: claude under the account the environment names, the prompt on stdin."""
    spec = json.loads(spec_path.read_text())
    tok = os.environ.get("YERK_TOKEN_FILE")
    account = Account(str(spec["account"]), Path(os.environ["CLAUDE_CONFIG_DIR"]), token_file=Path(tok) if tok else None)
    env = account_env(account, {k: v for k, v in os.environ.items() if k in ENV_KEEP})
    argv = [str(spec["claude"]), "-p", "--output-format", "stream-json", "--verbose", "--model", str(spec["model"]),
            "--session-id", str(spec["session_id"]), "--setting-sources", "", "--strict-mcp-config",
            "--tools", ",".join(spec["tools"]), "--disallowedTools", "Agent", "--permission-mode",
            str(spec["permission_mode"])]
    fd = os.open(spec["prompt"], os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)
    os.execvpe(argv[0], argv, env)


def post(folder: Path) -> int:
    """ExecStopPost: how the unit ended, as systemd tells it, in exit.json (written whole, then renamed)."""
    rec = {"result": os.environ.get("SERVICE_RESULT", ""), "code": os.environ.get("EXIT_CODE", ""),
           "status": os.environ.get("EXIT_STATUS", ""), "ended": round(time.time(), 3)}
    tmp = folder / f"{EXIT}.tmp"
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, folder / EXIT)
    return 0


# the watch
class Watch:
    """inotify on each running worker's folder, read on the event loop: the daemon wakes when a file
    there is written or moved in (TASKS.md ticked, exit.json written), never on a timer."""
    EVENT = struct.Struct("iIII")
    MASK = 0x8 | 0x80                   # IN_CLOSE_WRITE | IN_MOVED_TO

    def __init__(self, loop: asyncio.AbstractEventLoop, on_file: Callable[[Path, str], None]):
        import ctypes
        self.libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self.fd = int(self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC))
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.dirs: dict[int, Path] = {}
        self.on_file = on_file
        loop.add_reader(self.fd, self._read)

    def add(self, d: Path) -> None:
        wd = int(self.libc.inotify_add_watch(self.fd, os.fsencode(d), self.MASK))
        if wd < 0:
            raise OSError(f"inotify_add_watch failed for {d}")
        self.dirs[wd] = d

    def drop(self, d: Path) -> None:
        for wd, x in list(self.dirs.items()):
            if x == d:
                self.libc.inotify_rm_watch(self.fd, wd)
                del self.dirs[wd]

    def _read(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        i = 0
        while i + self.EVENT.size <= len(data):
            wd, _, _, n = self.EVENT.unpack_from(data, i)
            name = data[i + self.EVENT.size:i + self.EVENT.size + n].split(b"\0", 1)[0].decode(errors="replace")
            i += self.EVENT.size + n
            if wd in self.dirs and name:
                self.on_file(self.dirs[wd], name)


# the card
def tasks_of(path: Path) -> list[tuple[bool, str]]:
    """TASKS.md's checklist lines: (ticked, text)."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [(m.group(1) != " ", m.group(2)) for x in lines if (m := TICK.match(x))]


def first_line(path: Path) -> str:
    """The report's first non-empty line, heading marks dropped; "" when there is no report."""
    try:
        with open(path, errors="replace") as f:
            for x in f:
                if x.strip():
                    return short(x.strip().lstrip("#").strip(), 200)
    except OSError:
        pass
    return ""


def took(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def settled_state(row: dict[str, Any], items: list[tuple[bool, str]], end: dict[str, Any]) -> tuple[str, str]:
    """(mark, words) of a worker that ended, from its exit and its rows."""
    state, ticked = str(end["exit"]), sum(1 for t, _ in items if t)
    if state == "done" and not end.get("has_report"):
        state = "no report"
    elif state == "done" and items and ticked < len(items):
        state = "partly"
    why = words.died(str(end.get("result_code") or "")) if state == "died" else ""
    return words.outcome(state, cap=words.span(float(row["cap"])), why=why, ticked=ticked, rows=len(items))


def card_text(row: dict[str, Any], items: list[tuple[bool, str]], end: dict[str, Any] | None = None,
              accounts: int = 1, privacy: str = "owner", look: words.Look | None = None,
              now: float | None = None) -> str:
    """The worker's card, state first (STYLE.md). Running: its state and its rows, then the time so
    far against the cap. Settled: the outcome in plain words, the report's first line, the rows as
    they were, then how long it ran and "report saved" with the command that opens it. The account
    shows only when the install has more than one; the path only under presentation.paths = owner,
    in an owner room."""
    look = look or words.LOOK
    now = time.time() if now is None else now
    ticked = sum(1 for t, _ in items if t)
    started, cap = float(row["started"]), float(row["cap"])
    who = words.WORKER.format(id=row["id"])
    if end is None:
        mark, state = words.outcome("ticking" if items else "running", ticked=ticked, rows=len(items))
        when = (words.of_cap(now - started, cap) if look.times == "relative"
                else f"started {words.clock(started, now)}, cap {words.span(cap)}")
    else:
        mark, state = settled_state(row, items, end)
        ran = float(end["finished"]) - started
        when = ((f"done in {words.span(ran)}" if end["exit"] == "done" else f"ran {words.of_cap(ran, cap)}")
                if look.times == "relative" else f"ended {words.clock(float(end['finished']), now)}, "
                                                  f"after {words.span(ran)}")
    lines = [words.line(f"{mark} {words.WORKER_HEAD.format(state=state)}")]
    if end is not None and end.get("result") and end.get("has_report"):
        lines.append(words.line(str(end["result"])))
    current = next((i for i, (t, _) in enumerate(items) if not t), None)
    stuck = words.WORKING if end is None else words.NOT_DONE if mark == words.NOT_DONE else words.WAITING
    rows = [words.line(f"{words.DONE if t else stuck if i == current else words.WAITING} {text}")
            for i, (t, text) in enumerate(items)]
    if len(rows) > ROWS_SHOWN:
        lines.append(words.SUB + words.EARLIER_ROWS.format(n=len(rows) - ROWS_SHOWN))
    lines += rows[-ROWS_SHOWN:]
    extra = [f"account {row['account']}"] if accounts > 1 or look.footer == "full" else []
    if end is not None and end["exit"] == "did not start":
        when = ""                                        # it never ran: no time to tell
    foot = ", ".join(x for x in [*([who] if end is None else []), when, *extra] if x)
    if foot:
        lines.append(words.SUB + foot)
    if look.footer == "full" and row.get("model"):
        lines.append(words.SUB + f"model {row['model']}")
    if end is not None:
        lines.append(words.SUB + (words.REPORT_SAVED if end.get("has_report") else words.NO_REPORT).format(id=row["id"]))
        if end.get("has_report") and look.paths == "owner" and privacy == "owner":
            lines.append(words.SUB + str(row["report"]))
    return "\n".join(lines)


# the relay
def relay_text(row: dict[str, Any], end: dict[str, Any], cap: int) -> str:
    """The one harness line the room's session gets: the outcome in plain words, the report's
    first line and start, and the line the reply ends with ("report saved" and the command); the
    room says it in its own voice. Never the path, never the exit (STYLE.md). At most `cap` characters."""
    _, state = settled_state(row, tasks_of(Path(row["tasks"])), end)
    ran = float(end["finished"]) - float(row["started"])
    head = (f"[worker {row['id']} finished] {state}, {words.of_cap(ran, float(row['cap']))}\n"
            + (f"first line: \"{end['result']}\"\n" if end.get("result") else ""))
    try:
        body = Path(row["report"]).read_text(errors="replace").strip()
    except OSError:
        body = "(no report file)"
    saved = relay_line(row)
    whole = ("the report, all of it:\n", words.RELAY_ASK.format(cap=cap, saved=saved, where="it is all here, so "
                                                                "there is no file to open"))
    part = ("the report begins:\n", words.RELAY_ASK.format(cap=cap, saved=saved, where="the rest is in the report, "
                                                           "which this room does not open"))
    label, ask = whole if len(head) + len(whole[0]) + len(body) + 1 + len(whole[1]) <= cap else part
    room = cap - len(head) - len(label) - 1 - len(ask)
    if len(body) > room:
        body = body[:max(room - 4, 0)]
        body = (body[:body.rfind("\n")] if "\n" in body else body).rstrip() + "\n..."
    return f"{head}{label}{body}\n{ask}"[:cap]


def relay_line(row: dict[str, Any]) -> str:
    """The line a relay reply always carries: the report is saved, and the command that opens it.
    The relay names the report's path as the line its reply must carry, and room.fit says it this way."""
    return words.carried(str(row["report"]))


class Workers:
    """The daemon's workers: start, stop, the watch, the cards, completion, the relay, re-attach.
    Lives on the daemon's event loop; `rooms` and `channels` are the daemon's own tables."""

    def __init__(self, ledger: Ledger, conf: Settings, accounts: list[Account], cooldowns: Cooldowns,
                 rooms: dict[str, Any], channels: dict[str, Any], room_confs: dict[str, dict[str, Any]],
                 launcher: Launcher | None = None):
        self.ledger, self.conf, self.accounts, self.cooldowns = ledger, conf, accounts, cooldowns
        self.rooms, self.channels, self.room_confs = rooms, channels, room_confs
        self.launcher: Launcher = launcher or Systemd()
        self.watch: Watch | None = None
        self.live: dict[str, dict[str, Any]] = {}      # id -> its ledger row, while its unit runs
        self.tasks: set[asyncio.Future[Any]] = set()

    def begin(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            self.watch = Watch(loop, self._changed)
        except OSError as e:                             # no inotify: the minute backstop still completes them
            log.warning("workers: no file watch (%s); completion falls back to the reap pass", e)

    def _spawn(self, coro: Any) -> None:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def _changed(self, d: Path, name: str) -> None:
        row = self.live.get(d.name)
        if row is None:
            return
        if name == TASKS:
            self._spawn(self._show(row))
        elif name == EXIT:
            self._spawn(self.complete(d.name))

    async def _show(self, row: dict[str, Any], end: dict[str, Any] | None = None) -> None:
        ch = self.channels.get(row["room"])
        if ch is None:
            return
        text = card_text(row, tasks_of(Path(row["tasks"])), end, accounts=len(self.accounts),
                         privacy=str(self.room_confs.get(row["room"], {}).get("privacy", "owner")))
        key = f"worker:{row['id']}"
        try:
            if end is None:
                mid = await ch.worker_card(row["room"], key, text, row.get("card"))
            else:
                mid = await ch.worker_settle(row["room"], key, text, f"settled: {end['exit']}", row.get("card"))
        except Exception:                                # a card that fails never stops the worker
            log.exception("worker %s: card update failed", row["id"])
            return
        if mid and mid != row.get("card"):
            row["card"] = mid
            self.ledger.worker_set(row["id"], card=mid)

    # the tool and the command line
    async def handle(self, caller: str, req: dict[str, Any]) -> dict[str, Any]:
        action = str(req.get("action", ""))
        if action == "start":
            room = str(req.get("room") or caller) or (next(iter(self.rooms)) if len(self.rooms) == 1 else "")
            return await self.start(room, str(req.get("brief") or ""),
                                    str(req.get("brief_file") or ""), str(req.get("account") or ""),
                                    str(req.get("model") or ""), float(req.get("cap_minutes") or 0),
                                    list(req.get("tasks") or []))
        if action == "stop":
            return await self.stop(str(req.get("id", "")))
        if action == "list":
            return {"ok": True, "workers": listing(self.ledger)}
        if action == "report":
            return report(self.ledger, str(req.get("id", "")))
        return {"ok": False, "error": f"no action {action!r}; actions: start, list, stop, report"}

    def _account(self, name: str) -> Account | None:
        want = name or self.conf.account
        if want:
            return next((a for a in self.accounts if a.name == want), None)
        return pick(self.accounts, self.cooldowns) if self.accounts else None

    async def start(self, room: str, brief: str = "", brief_file: str = "", account: str = "", model: str = "",
                    cap_minutes: float = 0.0, tasks: list[str] | None = None) -> dict[str, Any]:
        if room not in self.rooms:
            return {"ok": False, "error": f"no room {room!r}; rooms: {', '.join(self.rooms)}"}
        if brief_file and not brief:
            try:
                brief = Path(brief_file).expanduser().read_text()
            except OSError as e:
                return {"ok": False, "error": f"cannot read the brief file: {e}"}
        if not brief.strip():
            return {"ok": False, "error": "a worker needs a brief: give `brief` (text) or `brief_file` (a path)"}
        acct = self._account(account)
        if acct is None:
            return {"ok": False, "error": f"no account {account or self.conf.account!r}; accounts: "
                                          f"{', '.join(a.name for a in self.accounts)}"}
        wid = uuid.uuid4().hex[:6]
        while self.ledger.worker_get(wid):
            wid = uuid.uuid4().hex[:6]
        d = self.conf.root / wid
        d.mkdir(mode=0o700, parents=True)
        cap = (cap_minutes or self.conf.cap_minutes) * 60
        model = model or self.conf.model or str(self.room_confs.get(room, {}).get("model", ""))
        report, tasks_md = d / REPORT, d / TASKS
        (d / "BRIEF.md").write_text(brief)
        if tasks:
            tasks_md.write_text("".join(f"- [ ] {t}\n" for t in tasks))
        prompt = d / "PROMPT.md"
        prompt.write_text(
            f"You are a Yerk AI worker: a headless Claude Code run with one brief, started from room {room}.\n"
            f"- Your folder is {d}. Write your files there unless the brief names another place.\n"
            f"- Keep {tasks_md} as your checklist: one \"- [ ] step\" line per step, ticked \"- [x]\" the moment "
            "it is done. The room's card shows these lines as you tick them.\n"
            f"- Write your report to {report}. Its first line is the one-line result; the card shows it.\n"
            "- Your last message is the deliverable: end with it, no tool call after it.\n"
            f"- You have no room session, no Yerk AI tools and no subagents. Your cap is {cap / 60:g} minutes; "
            "past it the run is stopped.\n\n"
            f"The brief (also in {d / 'BRIEF.md'}):\n\n{brief.strip()}\n")
        sid = str(uuid.uuid4())
        spec = {"id": wid, "room": room, "account": acct.name, "model": model, "session_id": sid,
                "claude": shutil.which(claude_bin()) or claude_bin(), "tools": self.conf.tools,
                "permission_mode": self.conf.permission_mode, "prompt": str(prompt), "report": str(report),
                "tasks": str(tasks_md), "cap": cap}
        (d / "worker.json").write_text(json.dumps(spec, indent=1))
        row: dict[str, Any] = {"id": wid, "room": room, "account": acct.name, "unit": f"{UNIT}{wid}", "model": model,
               "session_id": sid, "brief": brief_file or str(d / "BRIEF.md"), "staging": str(d),
               "report": str(report), "tasks": str(tasks_md), "cap": cap, "started": round(time.time(), 3)}
        self.ledger.worker_add(**row)
        row["card"] = None
        self.live[wid] = row
        if self.watch:
            self.watch.add(d)
        await self._show(row)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(PACKAGE.parent),
               "CLAUDE_CONFIG_DIR": str(acct.config_dir)}
        if acct.token_file and acct.token_file.is_file():
            env["YERK_TOKEN_FILE"] = str(acct.token_file)
        me = [sys.executable, "-P", "-m", "yerk_ai.workers"]    # -P: nothing in the folder can shadow an import
        try:
            await asyncio.to_thread(self.launcher.start, row["unit"], [*me, "run", str(d / "worker.json")], env, d,
                                    d / "stdout.jsonl", d / "stderr.log", cap, [*me, "post", str(d)])
        except (OSError, subprocess.SubprocessError) as e:
            await self.complete(wid, failed=str(e))
            return {"ok": False, "id": wid, "error": f"the unit did not start: {e}"}
        log.info("worker %s: started in %s on %s (%s), cap %g min", wid, room, acct.name, model, cap / 60)
        return {"ok": True, "id": wid, "unit": row["unit"], "room": room, "account": acct.name, "model": model,
                "staging": str(d), "report": str(report), "tasks": str(tasks_md), "cap_minutes": cap / 60,
                "card": row["card"]}

    async def stop(self, wid: str) -> dict[str, Any]:
        row = self.ledger.worker_get(wid)
        if row is None:
            return {"ok": False, "error": f"no worker {wid!r}"}
        if row["finished"]:
            return {"ok": False, "error": f"worker {wid} already ended: {row['exit']}"}
        self.ledger.worker_set(wid, stop_requested=round(time.time(), 3))
        await asyncio.to_thread(self.launcher.stop, row["unit"])
        log.info("worker %s: stop asked", wid)
        return {"ok": True, "id": wid, "stopping": row["unit"]}

    # completion
    async def complete(self, wid: str, lost: bool = False, failed: str = "") -> None:
        """Settle the worker once: the card, the ledger row, then the relay or the skip."""
        row = self.live.pop(wid, None) or self.ledger.worker_get(wid)
        if row is None or (self.ledger.worker_get(wid) or {}).get("finished"):
            return
        if self.watch:
            self.watch.drop(Path(row["staging"]))
        fresh = self.ledger.worker_get(wid) or row
        try:
            x = json.loads((Path(row["staging"]) / EXIT).read_text())
        except (OSError, ValueError):
            x = {}
        if failed:
            state, detail = "did not start", short(failed, 200)
        elif fresh.get("stop_requested"):
            state = "stopped"
        elif lost or not x:
            state = "lost"
        elif x.get("result") == "timeout":
            state = "timed out"
        elif x.get("result") == "success" and x.get("code") in ("exited", "") and x.get("status") in ("0", ""):
            state = "done"
        else:
            state = "died"
        if not failed:
            detail = (f"{x.get('result')}, {x.get('code') or 'exited'} {x.get('status') or ''}".strip() if x
                      else "the unit is gone and left no exit record")
        report = Path(row["report"])
        end: dict[str, Any] = {"exit": state, "detail": detail, "finished": float(x.get("ended") or time.time()),
               "result": first_line(report) or ("no report file" if state == "done" else ""),
               "has_report": report.is_file(), "result_code": x.get("result") or ""}
        self.ledger.worker_set(wid, finished=round(end["finished"], 3), exit=state, exit_detail=detail,
                               result=end["result"])
        row["card"] = row.get("card") or fresh.get("card")
        await self._show(row, end)
        log.info("worker %s: %s (%s)", wid, state, detail)
        await self.relay(row, end)

    async def relay(self, row: dict[str, Any], end: dict[str, Any]) -> None:
        """ONE short turn in the room's own session when the room is idle; else skipped, with why."""
        room = self.rooms.get(row["room"])
        why = "no such room in this daemon" if room is None else room.idle_reason(self.conf.idle_seconds)
        if end["exit"] == "did not start":
            why = "the unit never started; the card says so"
        if why:
            self.ledger.worker_set(row["id"], relay="skipped", relay_note=why)
            log.info("worker %s: relay skipped: %s", row["id"], why)
            return
        assert room is not None
        text = relay_text(row, end, self.conf.relay_message)
        mid = f"relay-{row['id']}"
        self.ledger.inbound_add(mid, row["room"], "worker", "yerk", len(text))
        self.ledger.worker_set(row["id"], relay="sent", relay_note=f"inbound {mid}")

        def then(state: str, ids: list[str], chars: int) -> None:
            self.ledger.worker_set(row["id"], relay="relayed" if state == "answered" else "failed", relay_chars=chars,
                                   relay_note=f"inbound {mid}, {state}, message(s) {','.join(ids)}")
        room.submit(Inbound(mid, "yerk", text, cap=self.conf.relay_reply, must=str(row["report"]), then=then))
        log.info("worker %s: relay turn asked in %s", row["id"], row["room"])

    # after a daemon start, and the minute backstop
    async def reattach(self) -> None:
        """Every worker the ledger has running: its watch and its card back when its unit still runs,
        completion when it ended while the daemon was away, settled as lost when it vanished."""
        for r in self.ledger.inbound_open():           # a relay turn the old process left open
            if r["channel"] != "worker":
                continue
            mid, wid = str(r["message_id"]), str(r["message_id"]).removeprefix("relay-")
            posted = self.ledger.delivered_for(mid)
            if posted:                                   # its reply was in the room before the cut
                self.ledger.inbound_set(mid, state="answered", reply_ids=",".join(posted),
                                        note="its reply was found delivered after a restart")
                self.ledger.worker_set(wid, relay="relayed", relay_note=f"inbound {mid}, found delivered after a "
                                                                        f"restart, message(s) {','.join(posted)}")
            else:
                self.ledger.inbound_set(mid, state="abandoned", note="cut by a daemon restart")
                self.ledger.worker_set(wid, relay="failed",
                                       relay_note="the relay turn was cut by a daemon restart; the card has the result")
        try:
            units: set[str] | None = await asyncio.to_thread(self.launcher.running)
        except (OSError, subprocess.SubprocessError) as e:
            units = None                                 # unknown: every row without an exit file is watched
            log.warning("workers: the unit list could not be read (%s); re-attaching every running row", e)
        rows = self.ledger.workers(running=True)
        for row in rows:
            d = Path(row["staging"])
            if (units is None or row["unit"] in units) and not (d / EXIT).is_file():
                self.live[row["id"]] = row
                if self.watch:
                    self.watch.add(d)
                await self._show(row)
                log.info("worker %s: re-attached (unit %s still running)", row["id"], row["unit"])
                if (d / EXIT).is_file():                 # it ended between the look and the watch
                    await self.complete(row["id"])
            else:
                await self.complete(row["id"], lost=not (d / EXIT).is_file())
        for u in sorted((units or set()) - {r["unit"] for r in rows}):
            log.warning("workers: unit %s is not in the ledger; left alone", u)

    async def reconcile(self) -> None:
        """A missed watch event never strands a worker: an exit file that is there completes it, a unit
        that is gone with none settles it as lost. Units are listed first, so an exit written in
        between is seen."""
        if not self.live:
            return
        for row in list(self.live.values()):            # "3 min of 15" moves on; an unchanged card is not edited
            await self._show(row)
        try:
            units = await asyncio.to_thread(self.launcher.running)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("workers: the unit list could not be read (%s); the next pass tries again", e)
            return
        for wid, row in list(self.live.items()):
            d = Path(row["staging"])
            if (d / EXIT).is_file():
                await self.complete(wid)
            elif row["unit"] not in units:
                await self.complete(wid, lost=True)


def listing(ledger: Ledger, limit: int = 20) -> list[dict[str, Any]]:
    """The newest workers, running or ended: what `worker list` and `yerk worker list` show."""
    now = time.time()
    return [{"id": r["id"], "room": r["room"], "account": r["account"], "unit": r["unit"],
             "state": r["exit"] or "running", "elapsed": took((r["finished"] or now) - r["started"]),
             "cap_minutes": r["cap"] / 60, "result": r["result"], "report": r["report"],
             "relay": r["relay"], "relay_note": r["relay_note"]} for r in ledger.workers(limit=limit)]


def report(ledger: Ledger, wid: str, limit: int = 6000) -> dict[str, Any]:
    """A worker's report: its path, first line and text (at most `limit` characters)."""
    row = ledger.worker_get(wid)
    if row is None:
        return {"ok": False, "error": f"no worker {wid!r}"}
    try:
        text = Path(row["report"]).read_text(errors="replace")
    except OSError:
        text = ""
    return {"ok": True, "id": wid, "state": row["exit"] or "running", "report": row["report"],
            "first_line": first_line(Path(row["report"])), "text": text[:limit], "trimmed": len(text) > limit,
            "unit": row["unit"], "exit_detail": row["exit_detail"], "staging": row["staging"], "account": row["account"],
            "model": row["model"]}


def status_lines(ledger: Ledger, units: set[str] | None, now: float | None = None, full: bool = False) -> list[str]:
    """`yerk status`: every worker the ledger has running, state first with its time against the cap,
    and any disagreement with the unit list (None: the unit list could not be read). `full`: the
    developer detail (room, account, unit names)."""
    now = time.time() if now is None else now
    rows = ledger.workers(running=True)
    out = []
    for r in rows:
        gone = units is not None and r["unit"] not in units
        if full:
            out.append(f"worker {r['id']}: room {r['room']}, account {r['account']}, {took(now - r['started'])} of "
                       f"{r['cap'] / 60:g} min, unit {r['unit']}" + (", NO UNIT RUNNING" if gone else ""))
        else:
            out.append(words.line(f"{words.NOT_DONE if gone else words.WORKING} {words.WORKER.format(id=r['id'])}: "
                                  + (words.GONE if gone else words.of_cap(now - r["started"], r["cap"]))))
    for u in sorted((units or set()) - {r["unit"] for r in rows}):
        out.append(f"unit {u}: running, NOT IN THE LEDGER" if full else
                   words.line(f"{words.NOT_DONE} {words.STRAY.format(id=u.removeprefix(UNIT))}"))
    return out or [words.NONE_RUNNING if full else f"{words.NOTHING} {words.NONE_RUNNING}"]


def main(argv: list[str] | None = None) -> int:
    a = sys.argv[1:] if argv is None else argv
    if len(a) == 2 and a[0] == "run":
        return run(Path(a[1]))
    if len(a) == 2 and a[0] == "post":
        return post(Path(a[1]))
    print("usage: python -m yerk_ai.workers run SPEC | post FOLDER (the unit's own steps)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
