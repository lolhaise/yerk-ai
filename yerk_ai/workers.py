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
  The unit sees the state folder read-only and its own folder writable; the install's secret, the
  private layer's secrets/ (its own account's token alone comes back, read-only) and every room's
  folder are not there at all (Systemd.sandbox), so a worker spends no money outside the daemon's
  tools, their gates and the ledger.
- The card: one per worker, in the room that started it or the room named in the call: its state,
  the time so far against the cap ("3 min of 15", refreshed each minute), then one row per TASKS.md
  line, ticked as the worker ticks them. An inotify watch on the folder wakes the daemon when a file
  there is written; the adapter edits at most once a second, the latest text winning, as it does
  for turn cards. Its words come from words.py (STYLE.md): no path and no exit code in a room.
- Completion: the unit's ExecStopPost writes exit.json (systemd's SERVICE_RESULT, EXIT_CODE and
  EXIT_STATUS) and the same watch sees it. The card settles with the outcome in plain words
  (finished clean, stopped: timed out, stopped by the owner, died: why, lost), the report's first
  line and "report saved" with `yerk worker report <id>`, where the path and the unit's exit live;
  then ONE relay turn in the room's own session tells the outcome under 1,500 characters ending with
  that line. The relay waits until the room is idle (no human message for 60 s and no turn running;
  the settled card carries the result meanwhile), runs alone (never folded into another turn, never
  merged with another relay; two completions are two relays, in order), and in a room whose process
  is closed it resumes the room's own session, as any message does. The raw report never goes to
  the room. A last message of exactly NO_REPLY settles the card quiet, live or at a re-attach: the
  done mark and "finished clean, nothing to add", no token, no rows, the footer as ever; and no
  relay turn, whoever started it (the tool, `yerk worker start` in a script), the ledger saying why.
- One heavy worker per account ([workers] per_account, default 1): a worker whose account is busy
  waits in line, its card saying "waiting for an account" and its place; the daemon starts it when
  the account frees. A worker that moves account mid-run is not built.
- Tools: none from Yerk AI by default. [workers] yerk_tools may give it the read-only ones (recall,
  read_memory, nothing else) through its own stdio MCP server, which has no send, hold, worker or
  schedule tool to give.
- A daemon start re-attaches from the ledger and the unit list: a running unit gets its watch and its
  card back, one that ended while the daemon was away completes, one that vanished settles as lost.
"""
from __future__ import annotations

import asyncio
import contextlib
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
from typing import Any, Awaitable, Callable, Sequence

from . import words
from .accounts import Account, Cooldowns, account_env, pick
from .card import ROWS_SHOWN, short
from .config import EFFORTS, PACKAGE, room_settings, uncapped
from .engine import claude_bin
from .gates import GATE_FILES
from .ledger import Ledger
from .room import Inbound

log = logging.getLogger("yerk.workers")
UNIT = "yerk-worker-"
EXIT, TASKS, REPORT = "exit.json", "TASKS.md", "REPORT.md"
TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite"]
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR",
            "XDG_RUNTIME_DIR")                 # what claude gets from the unit's environment; the rest stays out
TICK = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.+?)\s*$")
WORKER_TOOLS = ("recall", "read_memory")       # the only Yerk AI tools a worker may get: read-only
PUMP_EVERY = 5.0                               # seconds between looks for an idle room with a relay waiting
YOUNG = 120.0                                  # seconds after its start a worker missing from the unit list is not lost yet


@dataclass
class Settings:
    """[workers] in the private yerkd.toml; every key optional."""
    root: Path
    cap_minutes: float = 65.0
    model: str = ""                     # "" = the room's own model
    account: str = ""                   # "" = the first account in order that is not resting
    effort: str = ""                    # "" = the room's own effort; one of EFFORTS (config.py)
    tools: list[str] = field(default_factory=lambda: list(TOOLS))
    permission_mode: str = "bypassPermissions"     # headless: nobody is there to answer a prompt
    idle_seconds: float = 60.0          # the relay runs only after this long without a human message
    relay_message: int = 1500           # budgets.toml [relay]; 0, an uncapped install's: none
    relay_reply: int = 1500
    per_account: int = 1                # heavy workers at once on one account; the next waits in line
    yerk_tools: list[str] = field(default_factory=list)   # read-only Yerk AI tools (WORKER_TOOLS); none by default


def settings(state: Path, conf: dict[str, Any], budgets: dict[str, Any] | None = None) -> Settings:
    relay, whole = (budgets or {}).get("relay", {}), uncapped(budgets or {})
    return Settings(root=Path(str(conf.get("root", state / "workers"))).expanduser(),
                    cap_minutes=float(conf.get("cap_minutes", 65.0)), model=str(conf.get("model", "")),
                    account=str(conf.get("account", "")), effort=str(conf.get("effort", "")), tools=list(conf.get("tools", TOOLS)),
                    permission_mode=str(conf.get("permission_mode", "bypassPermissions")),
                    idle_seconds=float(conf.get("idle_seconds", 60.0)),
                    relay_message=0 if whole else int(relay.get("message", 1500)), relay_reply=0 if whole else int(relay.get("reply", 1500)),
                    per_account=max(1, int(conf.get("per_account", 1))), yerk_tools=yerk_tools(conf))


def yerk_tools(conf: dict[str, Any]) -> list[str]:
    """[workers] yerk_tools, kept to WORKER_TOOLS: any other name (send, hold, worker, schedule) is
    dropped with a warning, never given."""
    want = [str(t).removeprefix("mcp__yerk__") for t in conf.get("yerk_tools", [])]
    for t in want:
        if t not in WORKER_TOOLS:
            log.warning("workers: yerk_tools %r is not given to a worker; only %s are", t, ", ".join(WORKER_TOOLS))
    return [t for t in WORKER_TOOLS if t in want]


def gate_settings(folder: Path) -> Path:
    """A worker's --settings: one PreToolUse hook on every tool call, the room's gates run as a local
    process (gates.py worker-hook: no daemon round trip, no secret) with the worker's folder as the root. A hook that
    cannot start, times out or dies (a checkout that does not import mid-deploy) blocks the call (`onFailure`), as a
    hook that cannot read it does: Claude Code lets the call through otherwise."""
    cmd = shlex.join(["env", f"PYTHONPATH={PACKAGE.parent}", sys.executable, "-P", "-m", "yerk_ai.gates", "worker-hook", str(folder)])
    path = folder / "settings.json"
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": cmd, "timeout": 15, "onFailure": "block"}]}]}}, indent=1))
    return path


def mcp_config(state: Path, room: str, tools: list[str], path: Path) -> Path:
    """A worker's --mcp-config: its own stdio server (mcp_server.py `worker`) with only `tools`."""
    args = ["-P", "-m", "yerk_ai.mcp_server", "worker", str(state), room, ",".join(tools)]
    cfg = {"mcpServers": {"yerk": {"type": "stdio", "command": sys.executable, "args": args, "env": {"PYTHONPATH": str(PACKAGE.parent)}}}}
    path.write_text(json.dumps(cfg, indent=1))
    return path


# the unit; systemctl and unit_line are the jobs' and the deploy's too
def systemctl(*args: str, check: str = "", timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """`systemctl --user ARGS`; with `check`, an OSError naming it and the stderr when it fails."""
    p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise OSError(f"systemctl --user {check} exited {p.returncode}: {p.stderr.strip()[:200]}")
    return p


def unit_line(*argv: str) -> str:
    """argv as one unit-file command line: quoted, with systemd's % and $ escaped."""
    return " ".join(shlex.quote(a).replace("%", "%%").replace("$", "$$") for a in argv)


class Systemd:
    """systemd-run --user: the unit outlives the daemon; --collect unloads it once it has ended
    (exit.json is the record). With the daemon's state folder, the unit sees it through systemd's own
    namespacing (`sandbox`), so no tool or shell shape gets around it."""

    def __init__(self, state: Path | None = None, hidden: Sequence[Path] = ()):
        self.state, self.hidden = state, tuple(hidden)

    def sandbox(self, folder: Path, env: dict[str, str]) -> list[str]:
        """The state folder read-only; the worker's own folder writable, and its account's config
        folder when that lives under the state folder (Claude Code writes its transcript there); the
        worker's own gate files (gates.GATE_FILES) read-only inside it; the
        install's secret, the private layer's secrets/ (every provider key and account token but the
        worker's own token, bound back read-only for its first step) and every room's folder (its
        token, settings and override) not there at all. The worker secret stays readable: a worker's
        read-only memory tools reach the daemon with it. PrivateUsers is what lets a user manager set
        up the mounts."""
        if self.state is None:
            return []
        config = Path(env.get("CLAUDE_CONFIG_DIR") or folder)
        rw = [folder, *([config] if config != folder and config.is_relative_to(self.state) else [])]
        props = ["PrivateUsers=yes", f"ReadOnlyPaths={self.state}", *(f"ReadWritePaths={p}" for p in rw)]
        # its own gate (the spec the hook reads on every call, the hook, its tools) stays as the daemon wrote it: each file
        # read-only inside the writable folder, so no tool, shell shape or program of the worker's rewrites, moves or removes it
        props += [f"ReadOnlyPaths={folder / n}" for n in GATE_FILES if (folder / n).is_file()]
        rooms, token = self.state / "rooms", env.get("YERK_TOKEN_FILE", "")
        for p in self.hidden:
            if p != rooms and token and Path(token).is_relative_to(p):
                # secrets/ holding the worker's own token (secrets/claude-<name>.token, the default): an empty read-only
                # filesystem with that one file bound back, since nothing comes back inside an InaccessiblePaths (systemd.exec)
                props += [f"TemporaryFileSystem={p}:ro", f"BindReadOnlyPaths=-{token}"]
                continue
            if p != rooms:      # "-": outside the state folder (the private layer's secrets/) a path may not be there
                props.append(f"InaccessiblePaths={'' if p.is_relative_to(self.state) else '-'}{p}")
                continue
            # every room's folder (its token, settings, conversation) under an empty read-only filesystem,
            # with the worker's own room's work folder bound back at its own path, read and write: the
            # one room folder a brief may name (systemd.exec's TemporaryFileSystem plus BindPaths pattern)
            props.append(f"TemporaryFileSystem={rooms}:ro")
            if (room := str(self._room(folder))) and "/" not in room and room not in (".", ".."):
                props.append(f"BindPaths=-{rooms / room / 'work'}")
        return [x for p in props for x in ("-p", p)]

    @staticmethod
    def _room(folder: Path) -> str:
        """The room whose work folder the worker gets, from the spec Workers.start wrote before the launch: its own, or the
        `bind` the daemon gave it (a pictures backfill: its card in the machine room, the pictures in the owner room's inbox)."""
        try:
            spec = json.loads((folder / "worker.json").read_text())
            return str(spec.get("bind") or spec.get("room") or "")
        except (OSError, ValueError):
            return ""

    def start(self, unit: str, argv: list[str], env: dict[str, str], cwd: Path, out: Path, err: Path, cap: float, post: list[str]) -> None:
        cmd = ["systemd-run", "--user", "--unit", unit, "--collect", "--quiet", "--working-directory", str(cwd),
               "--description", f"Yerk AI worker {unit.removeprefix(UNIT)}",
               "-p", f"RuntimeMaxSec={max(1, int(cap))}", "-p", f"StandardOutput=append:{out}",
               "-p", f"StandardError=append:{err}", "-p", f"ExecStopPost={unit_line(*post)}",
               "-p", "KillMode=control-group", "-p", "TimeoutStopSec=20", *self.sandbox(cwd, env),
               *[f"--setenv={k}={v}" for k, v in env.items()], "--", *argv]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if p.returncode:
            raise OSError(f"systemd-run exited {p.returncode}: {p.stderr.strip()[:300]}")

    def stop(self, unit: str) -> None:
        systemctl("stop", "--no-block", f"{unit}.service")

    def running(self) -> set[str]:          # unreadable is not empty (OSError): nobody is declared lost on it
        p = systemctl("list-units", f"{UNIT}*", "--all", "--plain", "--no-legend", "--no-pager", check="list-units")
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
            str(spec["permission_mode"]), *(["--effort", str(spec["effort"])] if spec.get("effort") else [])]
    if spec.get("mcp_config") and spec.get("yerk_tools"):     # only the read-only tools, from its own server
        argv += ["--mcp-config", str(spec["mcp_config"]),
                 "--allowedTools", ",".join(f"mcp__yerk__{t}" for t in spec["yerk_tools"] if t in WORKER_TOOLS)]
    if spec.get("settings"):                                   # the gates' hook (gate_settings)
        argv += ["--settings", str(spec["settings"])]
    os.dup2(os.open(spec["prompt"], os.O_RDONLY), 0)         # the prompt on stdin; the opened fd closes on exec
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
        if (wd := int(self.libc.inotify_add_watch(self.fd, os.fsencode(d), self.MASK))) < 0:
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
    with contextlib.suppress(OSError), open(path, errors="replace") as f:
        for x in f:
            if x.strip():
                return short(x.strip().lstrip("#").strip(), 200)
    return ""


def last_message(folder: Path) -> str:
    """A worker's last message: the `result` of the last result event in its stdout.jsonl."""
    try:
        lines = (folder / "stdout.jsonl").read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            return str(ev.get("result") or "").strip()
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
    they were, then how long it ran and "report saved" with the command that opens it; settled quiet
    (end["quiet"]), the done mark, QUIET_WORKER and that footer alone. The account shows only when
    the install has more than one; the path only under presentation.paths = owner, in an owner room."""
    look = look or words.LOOK
    now = time.time() if now is None else now
    ticked = sum(1 for t, _ in items if t)
    started, cap = float(row["started"]), float(row["cap"])
    who = words.WORKER.format(id=row["id"])
    if end is None and row.get("waiting"):               # in line for its account: no time against the cap yet
        lines = [words.line(f"{words.WORKING} {words.WORKER_HEAD.format(state=words.WAITING_ACCOUNT)}"),
                 words.line(f"{words.WAITING} " + words.IN_LINE.format(nth=words.nth(int(row.get("line") or 1)),
                                                                        name=row["account"]))]
        lines += [words.line(f"{words.WAITING} {text}") for _, text in items[-ROWS_SHOWN:]]
        return "\n".join([*lines, words.SUB + who])
    if end is None:
        mark, state = words.outcome("ticking" if items else "running", ticked=ticked, rows=len(items))
        when = (words.of_cap(now - started, cap) if look.times == "relative"
                else f"started {words.clock(started, now)}, cap {words.span(cap)}")
    else:
        mark, state = settled_state(row, items, end)
        if end.get("quiet"):                             # its last message was exactly NO_REPLY: no token, no rows
            mark, state, items = words.DONE, words.QUIET_WORKER, []
        ran = float(end["finished"]) - started
        when = ((f"done in {words.span(ran)}" if end["exit"] == "done" else f"ran {words.of_cap(ran, cap)}")
                if look.times == "relative" else f"ended {words.clock(float(end['finished']), now)}, "
                                                  f"after {words.span(ran)}")
    lines = [words.line(f"{mark} {words.WORKER_HEAD.format(state=state)}")]
    if end is not None and end.get("result") and end.get("has_report") and not end.get("quiet"):
        lines.append(words.line(str(end["result"])))
    current = next((i for i, (t, _) in enumerate(items) if not t), None)
    stuck = words.WORKING if end is None else words.NOT_DONE if mark == words.NOT_DONE else words.WAITING
    rows = [words.line(f"{words.DONE if t else stuck if i == current else words.WAITING} {text}") for i, (t, text) in enumerate(items)]
    if len(rows) > ROWS_SHOWN:
        lines.append(words.SUB + words.EARLIER_ROWS.format(n=len(rows) - ROWS_SHOWN))
    lines += rows[-ROWS_SHOWN:]
    extra = [f"account {row['account']}"] if accounts > 1 or look.footer == "full" else []
    if end is not None and end["exit"] == "did not start":
        when = ""                                        # it never ran: no time to tell
    foot: list[str] = []                                 # phone width: a part that does not fit starts a line
    for x in (x for x in [*([who] if end is None else []), when, *extra] if x):
        if foot and words.width(f"{foot[-1]}, {x}") <= words.PHONE:
            foot[-1] += f", {x}"
        else:
            foot.append(x)
    lines += [words.SUB + x for x in foot]
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
    room says it in its own voice. Never the path, never the exit (STYLE.md). At most `cap` characters; 0: the whole report."""
    _, state = settled_state(row, tasks_of(Path(row["tasks"])), end)
    ran = float(end["finished"]) - float(row["started"])
    head = (f"[worker {row['id']} finished] {state}, {words.of_cap(ran, float(row['cap']))}\n"
            + (f"first line: \"{end['result']}\"\n" if end.get("result") else ""))
    try:
        body = Path(row["report"]).read_text(errors="replace").strip()
    except OSError:
        body = "(no report file)"
    saved = relay_line(row)
    whole = ("the report, all of it:\n", words.RELAY_ASK.format(under=words.under(cap), saved=saved, where="it is all here, so "
                                                                "there is no file to open"))
    part = ("the report begins:\n", words.RELAY_ASK.format(under=words.under(cap), saved=saved, where="the rest is in the report, "
                                                           "which this room does not open"))
    label, ask = whole if not cap or len(head) + len(whole[0]) + len(body) + 1 + len(whole[1]) <= cap else part
    room = cap - len(head) - len(label) - 1 - len(ask)
    if cap and len(body) > room:
        body = body[:max(room - 4, 0)]
        body = (body[:body.rfind("\n")] if "\n" in body else body).rstrip() + "\n..."
    return f"{head}{label}{body}\n{ask}"[:cap or None]


def relay_line(row: dict[str, Any]) -> str:
    """The line a relay reply always carries: the report is saved, and the command that opens it, or,
    when the worker left no report file, that there is none. The relay asks for it and names it as the
    line its reply must carry (room.fit adds it when the reply lacks it)."""
    saved = words.REPORT_SAVED if Path(str(row["report"])).is_file() else words.NO_REPORT
    return words.SUB + saved.format(id=row["id"])


class Workers:
    """The daemon's workers: start, stop, the watch, the cards, completion, the relay, re-attach.
    Lives on the daemon's event loop; `rooms` and `channels` are the daemon's own tables."""

    def __init__(self, ledger: Ledger, conf: Settings, accounts: list[Account], cooldowns: Cooldowns,
                 rooms: dict[str, Any], channels: dict[str, Any], room_confs: dict[str, dict[str, Any]],
                 launcher: Systemd | None = None, state: Path | None = None, hidden: Sequence[Path] = (), models: dict[str, str] | None = None):
        self.ledger, self.conf, self.accounts, self.cooldowns = ledger, conf, accounts, cooldowns
        self.state = state or conf.root.parent             # the daemon's state folder (a worker's tools reach it)
        self.rooms, self.channels, self.room_confs, self.models = rooms, channels, room_confs, models or {}
        # a unit sees the state folder read-only, its own folder writable and `hidden` not at all
        # (the daemon's: the install's secret, every room's folder, the private layer's secrets/); Systemd.sandbox
        self.launcher: Systemd = launcher or Systemd(self.state, hidden)
        self.gate: dict[str, Any] = {}       # the daemon's [gates] for a worker's hook: trash, private, external
        self.watch: Watch | None = None
        self.live: dict[str, dict[str, Any]] = {}      # id -> its ledger row, while its unit runs
        self.tasks: set[asyncio.Future[Any]] = set()
        # completion hooks (jobs.py): each gets (row, end) after the card settles; True means it
        # delivered the result itself, so the default relay does not run
        self.hooks: list[Callable[[dict[str, Any], dict[str, Any]], Awaitable[bool]]] = []
        # harness turns waiting for an idle room (relays, notes): in order, one at a time
        self.waiting: dict[str, list[Inbound]] = {}
        self.asked: dict[str, str] = {}                 # room -> the harness turn it is running now
        self.loop: asyncio.AbstractEventLoop | None = None

    def begin(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._spawn(self._pumper())
        try:
            self.watch = Watch(loop, self._changed)
        except OSError as e:                             # no inotify: the minute backstop still completes them
            log.warning("workers: no file watch (%s); completion falls back to the reap pass", e)

    def _spawn(self, coro: Any) -> None:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def _changed(self, d: Path, name: str) -> None:
        if (row := self.live.get(d.name)) is None:
            return
        if name == TASKS:
            self._spawn(self._show(row))
        elif name == EXIT:
            self._spawn(self.complete(d.name))

    async def _show(self, row: dict[str, Any], end: dict[str, Any] | None = None) -> None:
        if (ch := self.channels.get(row["room"])) is None:
            return
        text = card_text(row, tasks_of(Path(row["tasks"])), end, accounts=len(self.accounts),
                         privacy=str(self.room_confs.get(row["room"], {}).get("privacy", "owner")))
        key = f"worker:{row['id']}"
        try:
            mid = await (ch.worker_card(row["room"], key, text, row.get("card")) if end is None
                         else ch.worker_settle(row["room"], key, text, f"settled: {end['exit']}", row.get("card")))
        except Exception:                                # a card that fails never stops the worker
            log.exception("worker %s: card update failed", row["id"])
            return
        if mid and mid != row.get("card"):
            row["card"] = mid
            self.ledger.worker_set(row["id"], card=mid)

    # the tool and the command line
    async def handle(self, caller: str, req: dict[str, Any]) -> dict[str, Any]:
        if (action := str(req.get("action", ""))) == "start":
            room = str(req.get("room") or caller) or (next(iter(self.rooms)) if len(self.rooms) == 1 else "")
            return await self.start(room, str(req.get("brief") or ""),
                                    str(req.get("brief_file") or ""), str(req.get("account") or ""),
                                    str(req.get("model") or ""), float(req.get("cap_minutes") or 0),
                                    list(req.get("tasks") or []), effort=str(req.get("effort") or ""))
        if action == "stop":
            return await self.stop(str(req.get("id", "")))
        if action == "list":
            return {"ok": True, "workers": listing(self.ledger)}
        if action == "report":
            return report(self.ledger, str(req.get("id", "")))
        return {"ok": False, "error": f"no action {action!r}; actions: start, list, stop, report"}

    def _account(self, name: str, model: str = "") -> Account | None:
        if (want := name or self.conf.account):
            return next((a for a in self.accounts if a.name == want), None)
        return pick(self.accounts, self.cooldowns, model=model) if self.accounts else None

    async def start(self, room: str, brief: str = "", brief_file: str = "", account: str = "", model: str = "",
                    cap_minutes: float = 0.0, tasks: list[str] | None = None, job: str = "",
                    more: dict[str, Any] | None = None, effort: str = "") -> dict[str, Any]:
        """`more`: keys the daemon's own callers add to worker.json (never the tool's or the command line's), none replacing one."""
        if room not in self.rooms:
            return {"ok": False, "error": f"no room {room!r}; rooms: {', '.join(self.rooms)}"}
        if brief_file and not brief:
            try:
                brief = Path(brief_file).expanduser().read_text()
            except OSError as e:
                return {"ok": False, "error": f"cannot read the brief file: {e}"}
        if not brief.strip():
            return {"ok": False, "error": "a worker needs a brief: give `brief` (text) or `brief_file` (a path)"}
        live = room_settings(self.room_confs.get(room, {}), self.state, room, self.models)  # the room's live model and level
        model = model or self.conf.model or live["model"][0]
        if (effort := (effort or self.conf.effort or live["effort"][0]).strip().lower()) and effort not in EFFORTS:
            return {"ok": False, "error": f"no effort level {effort!r}; levels: {', '.join(EFFORTS)}"}
        if (acct := self._account(account, model)) is None:
            return {"ok": False, "error": f"no account {account or self.conf.account!r}; accounts: "
                                          f"{', '.join(a.name for a in self.accounts)}"}
        wid = uuid.uuid4().hex[:6]
        while self.ledger.worker_get(wid):
            wid = uuid.uuid4().hex[:6]
        d = self.conf.root / wid
        d.mkdir(mode=0o700, parents=True)
        cap = (cap_minutes or self.conf.cap_minutes) * 60
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
            "past it the run is stopped.\n"
            "- Your tool calls pass Yerk AI's gates: a plain delete in your folder goes to the trash; a delete "
            "elsewhere or after a cd, an external action (a post, a login, money, a message out) and a change to "
            "Yerk AI's own config are refused. Put what you could not do in your report.\n\n"
            f"The brief (also in {d / 'BRIEF.md'}):\n\n{brief.strip()}\n")
        sid = str(uuid.uuid4())
        spec = {"id": wid, "room": room, "account": acct.name, "model": model, "effort": effort, "session_id": sid,
                "claude": shutil.which(claude_bin()) or claude_bin(), "tools": self.conf.tools,
                "permission_mode": self.conf.permission_mode, "prompt": str(prompt), "report": str(report),
                "tasks": str(tasks_md), "cap": cap, "gate": self.gate, "settings": str(gate_settings(d))}
        spec.update({k: v for k, v in (more or {}).items() if k not in spec})
        if self.conf.yerk_tools:
            spec["yerk_tools"] = list(self.conf.yerk_tools)
            spec["mcp_config"] = str(mcp_config(self.state, room, self.conf.yerk_tools, d / "mcp.json"))
        (d / "worker.json").write_text(json.dumps(spec, indent=1))
        now = round(time.time(), 3)
        busy = self.busy(acct.name)
        row: dict[str, Any] = {"id": wid, "room": room, "account": acct.name, "unit": f"{UNIT}{wid}", "model": model,
               "session_id": sid, "brief": brief_file or str(d / "BRIEF.md"), "staging": str(d),
               "report": str(report), "tasks": str(tasks_md), "cap": cap, "started": now,
               "job": job or None}
        if busy >= self.conf.per_account:                   # one heavy worker per account: this one waits
            row.update(queued=now, waiting=1)
        self.ledger.worker_add(**row)
        row["card"] = None
        self.live[wid] = row
        if self.watch:
            self.watch.add(d)
        out = {"ok": True, "id": wid, "unit": row["unit"], "room": room, "account": acct.name, "model": model, "effort": effort,
               "staging": str(d), "report": str(report), "tasks": str(tasks_md), "cap_minutes": cap / 60}
        if row.get("waiting"):
            row["line"] = len(self.in_line(acct.name))
            await self._show(row)
            log.info("worker %s: waiting for account %s (%s in line)", wid, acct.name, words.nth(row["line"]))
            return {**out, "waiting": words.WAITING_ACCOUNT, "line": row["line"], "card": row["card"]}
        await self._show(row)
        if (why := await self._launch(row)):
            return {"ok": False, "id": wid, "error": f"the unit did not start: {why}"}
        return {**out, "card": row["card"]}

    def busy(self, account: str) -> int:
        return sum(1 for r in self.live.values() if r["account"] == account and not r.get("waiting"))

    def in_line(self, account: str) -> list[dict[str, Any]]:
        return sorted((r for r in self.live.values() if r["account"] == account and r.get("waiting")),
                      key=lambda r: (float(r.get("queued") or 0), r["id"]))

    async def _launch(self, row: dict[str, Any]) -> str:
        """The unit, now; "" or why it did not start (then the worker is settled as such)."""
        acct = next((a for a in self.accounts if a.name == row["account"]), None)
        d, wid, cap = Path(row["staging"]), str(row["id"]), float(row["cap"])
        if row.get("waiting"):
            row.update(waiting=0, started=round(time.time(), 3))
            self.ledger.worker_set(wid, waiting=0, started=row["started"])
            await self._show(row)
        if acct is None:
            await self.complete(wid, failed=f"no account {row['account']!r}")
            return f"no account {row['account']!r}"
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
            return str(e)
        log.info("worker %s: started in %s on %s (%s), cap %g min", wid, row["room"], acct.name, row["model"], cap / 60)
        return ""

    async def next_in_line(self) -> None:
        """Start the first worker in line on every account that has room, then tell the rest their place."""
        for acct in {str(r["account"]) for r in self.live.values() if r.get("waiting")}:
            line = self.in_line(acct)
            while line and self.busy(acct) < self.conf.per_account:
                await self._launch(line.pop(0))
            for i, r in enumerate(line, 1):
                if r.get("line") != i:
                    r["line"] = i
                    await self._show(r)

    async def stop(self, wid: str) -> dict[str, Any]:
        if (row := self.ledger.worker_get(wid)) is None:
            return {"ok": False, "error": f"no worker {wid!r}"}
        if row["finished"]:
            return {"ok": False, "error": f"worker {wid} already ended: {row['exit']}"}
        self.ledger.worker_set(wid, stop_requested=round(time.time(), 3))
        if (self.live.get(wid) or row).get("waiting"):      # still in line: no unit to stop
            await self.complete(wid)
            return {"ok": True, "id": wid, "stopped": "it was still waiting for an account"}
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
        # its last message read as jobs.py's worker_done reads it: exactly NO_REPLY settles the card quiet (card_text), no relay
        end["quiet"] = state == "done" and (last_message(Path(row["staging"])) or end["result"]).strip() == words.NO_REPLY
        self.ledger.worker_set(wid, finished=round(end["finished"], 3), exit=state, exit_detail=detail, result=end["result"])
        row["card"] = row.get("card") or fresh.get("card")
        row["job"] = row.get("job") or fresh.get("job")
        await self._show(row, end)
        log.info("worker %s: %s (%s)", wid, state, detail)
        try:
            for hook in self.hooks:
                try:
                    if await hook(row, end):
                        return
                except Exception:                            # a hook that fails never loses the relay
                    log.exception("worker %s: a completion hook failed", wid)
            if end["quiet"]:                                 # nothing to tell: no relay turn, as a job's NO_REPLY is silence
                self.ledger.worker_set(wid, relay="skipped", relay_note="the worker answered NO_REPLY")
                log.info("worker %s: relay skipped: it answered NO_REPLY", wid)
                return
            await self.relay(row, end)
        finally:
            await self.next_in_line()                        # the account is free: the next in line starts

    async def relay(self, row: dict[str, Any], end: dict[str, Any], text: str | None = None,
                    after: Callable[[str, list[str], int], None] | None = None) -> str:
        """ONE short turn in the room's own session, alone, once the room is idle (the settled card
        carries the result meanwhile); skipped, with why, only when it cannot run at all (returned;
        "" when the turn is asked or waiting). `text` replaces the harness line (a job's result);
        `after` hears how the turn ended, after the ledger does."""
        room = self.rooms.get(row["room"])
        why = "no such room in this daemon" if room is None else ""
        if end["exit"] == "did not start":
            why = "the unit never started; the card says so"
        if why:
            self.ledger.worker_set(row["id"], relay="skipped", relay_note=why)
            log.info("worker %s: relay skipped: %s", row["id"], why)
            return why
        text = text or relay_text(row, end, self.conf.relay_message)
        mid = f"relay-{row['id']}"
        self.ledger.inbound_add(mid, row["room"], "worker", "yerk", len(text))

        def then(state: str, ids: list[str], chars: int) -> None:
            self.ledger.worker_set(row["id"], relay="relayed" if state == "answered" else "failed", relay_chars=chars,
                                   relay_note=f"inbound {mid}, {state}, message(s) {','.join(ids)}")
            if after:
                after(state, ids, chars)
        busy = room.idle_reason(self.conf.idle_seconds) if room is not None else ""
        self.ledger.worker_set(row["id"], relay="waiting" if busy or self.waiting.get(row["room"]) else "sent",
                               relay_note=f"inbound {mid}" + (f", waiting: {busy}" if busy else ""))
        self.ask(row["room"], Inbound(mid, "yerk", text, cap=self.conf.relay_reply, must=relay_line(row), then=then, alone=True))
        return ""

    # harness turns: one at a time per room, in order, each alone, when the room is idle
    def ask(self, room: str, msg: Inbound) -> None:
        """Queue a harness turn for `room`; it runs when the room is idle and no other harness turn
        of this daemon is running there."""
        self.waiting.setdefault(room, []).append(msg)
        self.pump(room)

    def pump(self, name: str) -> None:
        q, room = self.waiting.get(name), self.rooms.get(name)
        if not q or room is None or self.asked.get(name):
            return
        if room.idle_reason(self.conf.idle_seconds):
            return
        msg = q.pop(0)
        done = msg.then

        def then(state: str, ids: list[str], chars: int) -> None:
            if done:
                done(state, ids, chars)
            self.asked.pop(name, None)
            if self.loop is not None:                    # the room's thread: the next one from the loop
                self.loop.call_soon_threadsafe(self.pump, name)
        msg.then = then
        self.asked[name] = msg.id
        if msg.id.startswith("relay-"):
            self.ledger.worker_set(msg.id.removeprefix("relay-"), relay="sent", relay_note=f"inbound {msg.id}")
        room.submit(msg)
        log.info("harness turn %s asked in %s", msg.id, name)

    async def _pumper(self) -> None:
        while True:
            await asyncio.sleep(PUMP_EVERY)
            for name in list(self.waiting):
                self.pump(name)

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
            if row.get("waiting"):                       # still in line: no unit yet, back in line
                self.live[row["id"]] = row
                if self.watch:
                    self.watch.add(d)
                log.info("worker %s: back in line for account %s", row["id"], row["account"])
                continue
            if not (d / EXIT).is_file() and (units is None or row["unit"] in units or not await self._gone(row)):
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
        for i, r in enumerate(sorted((r for r in self.live.values() if r.get("waiting")), key=lambda r: float(r.get("queued") or 0)), 1):
            r["line"] = i
            await self._show(r)
        await self.next_in_line()

    async def reconcile(self) -> None:
        """A missed watch event never strands a worker: an exit file that is there completes it, a unit
        that is gone with none settles it as lost. Units are listed first, so an exit written in
        between is seen."""
        for name in list(self.waiting):
            self.pump(name)
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
            if row.get("waiting"):
                continue
            if (d / EXIT).is_file():
                await self.complete(wid)
            elif row["unit"] not in units and await self._gone(row):
                await self.complete(wid, lost=True)
        await self.next_in_line()

    async def _gone(self, row: dict[str, Any]) -> bool:
        """A worker one unit list missed is lost only when a fresh look misses its unit too, it left no exit file and it
        started over YOUNG s ago: a row is in the ledger and live before systemd-run makes its unit, so one stale list
        settled a running cold read as lost a second after its start (seen live)."""
        try:
            listed = row["unit"] in await asyncio.to_thread(self.launcher.running)
        except (OSError, subprocess.SubprocessError):
            listed = True                                # unreadable is not empty: nobody is declared lost on it
        if (Path(row["staging"]) / EXIT).is_file():
            return False                                 # it ended meanwhile: its exit file completes it
        if listed or time.time() - float(row.get("started") or 0) < YOUNG:
            log.info("worker %s: the unit list was stale, re-attached", row["id"])
            return False
        return True


def listing(ledger: Ledger, limit: int = 20) -> list[dict[str, Any]]:
    """The newest workers, running or ended: what `worker list` and `yerk worker list` show."""
    now = time.time()
    return [{"id": r["id"], "room": r["room"], "account": r["account"], "unit": r["unit"],
             "state": r["exit"] or (words.WAITING_ACCOUNT if r.get("waiting") else "running"), "elapsed": took((r["finished"] or now) - r["started"]),
             "cap_minutes": r["cap"] / 60, "result": r["result"], "report": r["report"],
             "relay": r["relay"], "relay_note": r["relay_note"]} for r in ledger.workers(limit=limit)]


def report(ledger: Ledger, wid: str, limit: int = 6000) -> dict[str, Any]:
    """A worker's report: its path, first line and text (at most `limit` characters)."""
    if (row := ledger.worker_get(wid)) is None:
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
    out, waiting = [], []
    line: dict[str, int] = {}
    for r in sorted((r for r in rows if r.get("waiting")), key=lambda r: float(r.get("queued") or 0)):
        line[r["account"]] = line.get(r["account"], 0) + 1
        waiting.append(f"worker {r['id']}: room {r['room']}, waiting for account {r['account']}, "
                   f"{words.nth(line[r['account']])} in line, {took(now - float(r['queued'] or now))} so far" if full else
                   words.line(f"{words.WORKING} {words.WORKER.format(id=r['id'])}: "
                              + words.IN_LINE_SHORT.format(nth=words.nth(line[r['account']]))))
    rows = [r for r in rows if not r.get("waiting")]
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
    out += waiting                                   # the line for each account, after the running ones
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
