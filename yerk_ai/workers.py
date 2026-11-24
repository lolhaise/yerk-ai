"""Workers: a heavy job moves out of the room into a worker, a separate Claude Code run with its own progress
card that reports back when it ends. A worker is a headless `claude -p` run owned by Yerk AI (DESIGN 6,
daemon restarts.

- Start: a staging folder <root>/<id> (BRIEF.md, PROMPT.md, worker.json, TASKS.md when the call
  gives the items), a `workers` ledger row, the card in the room, then the unit: `claude -p` under one
  named account with the prompt on stdin, stdout and stderr to files in the folder, a cap
  (RuntimeMaxSec, 65 minutes unless the call or [workers] says otherwise) and its own cgroup, so a
  stop takes every process with it. A memory ceiling too (MemoryMax, 4G unless [workers] memory_max
  says otherwise, and no swap): past it the kernel kills the process using the most memory inside
  the unit, never one of the box's, and systemd does not stop the unit for it (OOMPolicy=continue),
  so a command of the worker's that runs away dies alone and the worker goes on.
  The unit's environment names its account's token file and no
  other; its first step (`python -m yerk_ai.workers run`) reads it the way a room does
  (accounts.account_env) and starts claude with an allowlisted environment. A worker gets no room
  session, no `worker` or `schedule` tool on any MCP server it is given (so no workers spawning workers) and no subagent tool.
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
  EXIT_STATUS, and how many of its processes the kernel killed for memory) and the same watch sees it. The card settles
  with the outcome in plain words (finished clean, stopped: timed out, stopped by the owner, died: why, lost; a death
  for memory says memory, and one that lost a process to memory and went on says so in one line), the report's first
  line and "report saved" with `yerk worker report <id>`, where the path and the unit's exit live;
  then ONE relay turn in the room's own session tells the outcome under 1,500 characters ending with
  that line. The relay waits until the room is idle (no human message for 60 s and no turn running;
  the settled card carries the result meanwhile), runs alone (never folded into another turn, never
  merged with another relay; two completions are two relays, in order), and in a room whose process
  is closed it resumes the room's own session, as any message does. The raw report never goes to
  the room. A last message of exactly NO_REPLY settles the card quiet, live or at a re-attach: the
  done mark and "finished clean" over the report's first line, or "finished clean, nothing to add" when
  that line is the token too, no rows, the footer as ever; and no relay turn, whoever started it (the
  tool, `yerk worker start` in a script), the ledger saying why. A job's worker names its job in its footer.
- One heavy worker per account ([workers] per_account, default 1): a worker whose account is busy
  waits in line, its card saying "waiting for an account" and its place; the daemon starts it when
  the account frees. A worker that moves account mid-run is not built.
- Tools: a shell, files and the web (TOOLS); none from Yerk AI by default. [workers] yerk_tools may give it the read-only
  ones (recall, read_memory, nothing else) through its own stdio MCP server, which has no send, hold, worker or
  schedule tool to give. Of the room's own add-on tools it gets the ones the room gives every worker started from it
  (rooms.toml worker_addons, an owner room's carried from [defaults]; a job's worker too, with no one to remember them) and
  the ones a start's `addons` names (need 83 of 2026-11-10: a room could not hand a worker the browser), one list with no
  repeat: each one the room's allow list has, its server's table as the room runs it plus YERK_WORKER and YERK_WORK_DIR;
  never one whose table hands it a credential, which the worker's shell could read. A named one the room cannot give
  refuses the start; a default it cannot give is skipped (a credential's with a warning) and the worker starts. Its gate
  (gates.worker_check) refuses any other MCP tool, and every call that carries the owner's word (`confirmed`).
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
import site
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatch
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
# a worker's git worktrees in its folder, by the names every need brief gives them (needs.py): `wt`, Yerk's, and
# `<plug-in>-wt`, a plug-in's; an owner room's Read, Grep and Glob reach them, and the worker's own files too except in a sealed room (Yerkd.worker_reads)
WORKTREES = ("wt", "*-wt")
COMMITS = 10                                   # the newest commits the report reads on a worktree's branch (branch)
TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite"]
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR",
            "XDG_RUNTIME_DIR", "UV_CACHE_DIR")  # what claude gets from the unit's environment; the rest stays out
TICK = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.+?)\s*$")
WORKER_TOOLS = ("recall", "read_memory")       # the only Yerk AI tools a worker may get: read-only
ADDON = re.compile(r"^mcp__([A-Za-z0-9_-]+?)(?:__([A-Za-z0-9_-]+))?$")   # a room's add-on tool, or all of its server's
# a path a brief names: absolute or ~/, at a word's start or after an opening bracket, a quote, a backtick or "="; a closing
# bracket, a quote or a backtick ends it, and the sentence's punctuation after it is not part of it (NAMED_END)
NAMED, NAMED_END, WILD = re.compile(r"(?<![^\s(\[{<'\"`=])~?/[^\s'\"`()\[\]{}<>]*"), ".,;:!?", re.compile(r"[*?]")
CREDENTIAL = re.compile(r"key|token|secret|pass|auth|cookie|credential", re.I)   # an env name or header that hands one over
PUMP_EVERY = 5.0                               # seconds between looks for an idle room with a relay waiting
YOUNG = 120.0                                  # seconds after its start a worker missing from the unit list is not lost yet
MEMORY_MAX = "4G"                              # a worker unit's memory ceiling (MemoryMax), no swap past it
CGROUP, OWN_CGROUP = Path("/sys/fs/cgroup"), Path("/proc/self/cgroup")   # cgroup v2: where a unit's memory.events is
SIZE, SUFFIXES = re.compile(r"([1-9][0-9]*)([KMGT]?)"), ("", "K", "M", "G", "T")   # a size as systemd reads it, base 1024
HOMES = ("open", "needs", "hidden")            # [workers] home: the owner's home as a worker's unit sees it (Systemd.sandbox)
GIT_RW = ("objects", "refs", "logs", "FETCH_HEAD")   # all of .git a commit and a fetch write; the list of worktrees is the unit's own
# outside the home, the doors a hidden home shuts with it (2026-11-24, each measured in a throwaway unit): /run empty and
# read-only but for the unit's own runtime folder (the user bus, rootless Docker, the SSH agent, the system bus, every service's
# socket, WSL's interop, tailscaled's: what nobody listed is shut too); /mnt empty (Windows' drives, and the whole disk again
# under WSLg); /dev/shm the unit's own; the journals (every other program's log) not there. Each only when it is there; /tmp is
# the unit's own too (PrivateTmp)
DOORS = ((Path("/run"), "empty"), (Path(f"/run/user/{os.getuid()}"), "own"), (Path("/mnt"), "empty"), (Path("/dev/shm"), "own"),
         (Path("/var/log"), "gone"), (Path("/dev/vsock"), "gone"), (Path("/dev/fuse"), "gone"), (Path("/dev/dxg"), "gone"))
# what else a hidden unit may not do: sockets past FAMILIES (WSL's Windows side answers any process on the virtual machine's, the
# drives' file server too, with no path and no bus involved), by the other ABI or by io_uring either; a setuid program's rights;
# the owner's other processes (its list of them is its own, so none is seen, signalled or traced); the kernel's log, the cgroup
# tree; a write anywhere but its own folders. Not PrivateDevices (it leaves no pseudo-terminal, and a suite opens them), nor
# RestrictNamespaces (a run in bubblewrap makes them; what it inherits stays locked). Not PrivateIPC: under a user manager the unit's stop step
# cannot join its namespace ("Failed at step NAMESPACE"), so no exit.json is written and the worker never ends (seen 2026-11-24)
FAMILIES, NO_CALLS = "AF_UNIX AF_INET AF_INET6 AF_NETLINK", "~io_uring_setup io_uring_enter io_uring_register"
WALLS = ("PrivateTmp=yes", "PrivatePIDs=yes", "ProtectSystem=strict", "ProtectControlGroups=yes",
         "ProtectKernelTunables=yes", "ProtectKernelLogs=yes", "NoNewPrivileges=yes", "RestrictSUIDSGID=yes",
         "LockPersonality=yes", f"RestrictAddressFamilies={FAMILIES}", "SystemCallArchitectures=native", f"SystemCallFilter={NO_CALLS}",
         "SystemCallErrorNumber=ENOSYS")
OWN_CONFIG = ".claude-home"     # a hidden worker's Claude config folder, in its own folder (Systemd.own)
UNIT_GIT = ".git-unit"          # a hidden worker's worktree's .git from its end on (needs.tidy): out here it names another's record


@dataclass
class Settings:
    """[workers] in the private yerkd.toml; every key optional."""
    root: Path
    cap_minutes: float = 65.0
    memory_max: str = MEMORY_MAX        # the unit's memory ceiling, no swap past it (Systemd.start); a size, see memory_max()
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
    home: str = "open"                  # one of HOMES; "hidden": the home empty but what a worker's job reaches; "needs": a need's workers only


def settings(state: Path, conf: dict[str, Any], budgets: dict[str, Any] | None = None) -> Settings:
    relay, whole = (budgets or {}).get("relay", {}), uncapped(budgets or {})
    return Settings(root=Path(str(conf.get("root", state / "workers"))).expanduser(),
                    cap_minutes=float(conf.get("cap_minutes", 65.0)), model=str(conf.get("model", "")),
                    account=str(conf.get("account", "")), effort=str(conf.get("effort", "")), tools=list(conf.get("tools", TOOLS)),
                    permission_mode=str(conf.get("permission_mode", "bypassPermissions")),
                    idle_seconds=float(conf.get("idle_seconds", 60.0)),
                    relay_message=0 if whole else int(relay.get("message", 1500)), relay_reply=0 if whole else int(relay.get("reply", 1500)),
                    per_account=max(1, int(conf.get("per_account", 1))), yerk_tools=yerk_tools(conf), memory_max=memory_max(conf),
                    home=home(conf))


def home(conf: dict[str, Any]) -> str:
    """[workers] home, one of HOMES; any other value stops yerkd at its start, since a wall misspelt is no wall."""
    if (v := str(conf.get("home", "open"))) not in HOMES:
        raise SystemExit(f"yerkd.toml: [workers] home must be one of {', '.join(HOMES)}, not {v!r}")
    return v


def reach(repos: Sequence[Path], gate: str = "", trash: Sequence[str] = ()) -> list[tuple[Path, str]]:
    """What a hidden home gives back of itself besides the worker's own folder and account (Systemd.sandbox), as (path, "ro" |
    "rw" | "repo"), each measured over the need workers' folders (2026-11-10: 156; 2026-11-24: 262): every repository a need
    is built in, a "repo" (its working tree read-only, of its .git only GIT_RW written: every need worker); uv and uvx, uv's
    pythons and tools read-only and a cache of the workers' own written (the builders' and fixers' `uv run`, `uv sync`; uv_cache); each python's user
    site-packages, read-only (the trash a delete becomes runs from the system python's, whatever python the daemon runs
    on); the browsers Playwright keeps, read-only (a page's
    screenshot); and the pull request gate, read-only (every brief names it)."""
    data, cache = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"), uv_cache().parents[1]
    out = [(Path(r), "repo") for r in repos] + [(Path(t), "ro") for x in ("uv", "uvx", *trash[:1]) if (t := shutil.which(x))]
    sites = sorted({Path(site.getusersitepackages()), *(Path(site.getuserbase()) / "lib").glob("python3*/site-packages")})   # each python's
    out += [(uv_cache(), "rw"), (data / "uv", "ro"), *((p, "ro") for p in sites),
            (Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or cache / "ms-playwright"), "ro")]
    return out + ([(Path(gate).expanduser(), "ro")] if gate else [])


def uv_cache() -> Path:
    """The uv cache hidden workers share, never the owner's: uv links what it installs to its cache, so the owner's holds the
    running daemon's own files (2026-11-24: 3,382 of its venv), and a worker that wrote there would write what runs outside."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "yerk" / "uv"


def resolver(at: Path = Path("/etc/resolv.conf")) -> list[Path]:
    """The file that says where DNS is, when it lives under one of the DOORS (systemd-resolved's in /run, WSL's in /mnt): bound
    back read-only, or a hidden worker finds no host."""
    real = at.resolve()
    return [real] if real.is_file() and any(real.is_relative_to(p) for p, _ in DOORS) else []


def memory_max(conf: dict[str, Any]) -> str:
    """[workers] memory_max, a worker unit's memory ceiling: a whole number of bytes, or one with K, M, G or T after it (any
    case, base 1024, as systemd reads MemoryMax). Anything else (a word, a fraction, a percentage, infinity, zero, past 64
    bits) never reaches systemd: dropped with a warning, as a wrong yerk_tools name is, and the default used."""
    value = conf.get("memory_max", MEMORY_MAX)
    text = str(value).strip().upper() if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    if (m := SIZE.fullmatch(text)) and int(m[1]) * 1024 ** SUFFIXES.index(m[2]) < 2 ** 64:
        return text
    log.warning("workers: memory_max %r is not a size; a worker gets %s", value, MEMORY_MAX)
    return MEMORY_MAX


def yerk_tools(conf: dict[str, Any]) -> list[str]:
    """[workers] yerk_tools, kept to WORKER_TOOLS: any other name (send, hold, worker, schedule) is
    dropped with a warning, never given."""
    want = [str(t).removeprefix("mcp__yerk__") for t in conf.get("yerk_tools", [])]
    for t in want:
        if t not in WORKER_TOOLS:
            log.warning("workers: yerk_tools %r is not given to a worker; only %s are", t, ", ".join(WORKER_TOOLS))
    return [t for t in WORKER_TOOLS if t in want]


def walled_trash(gate: dict[str, Any], folder: Path) -> dict[str, Any]:
    """A hidden home's gate: the owner's Trash is not there and the state folder is not either, so trash-cli's `trash` puts a
    worker's deletes in its own folder's .trash (in a throwaway unit of 2026-11-10: exit 74 without it, 0 with it)."""
    t = list(gate.get("trash") or [])
    return {**gate, "trash": [*t, "--trash-dir", str(folder / ".trash")]} if t and os.path.basename(t[0]) in ("trash", "trash-put") else gate


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


def mcp_config(state: Path, room: str, tools: list[str], path: Path, servers: dict[str, Any] | None = None) -> Path:
    """A worker's --mcp-config: its own stdio server (mcp_server.py `worker`) with only `tools`, when there are any, and the
    room's add-on servers it was given (`servers`, from room_addons)."""
    cfg: dict[str, Any] = dict(servers or {})
    if tools:
        args = ["-P", "-m", "yerk_ai.mcp_server", "worker", str(state), room, ",".join(tools)]
        cfg["yerk"] = {"type": "stdio", "command": sys.executable, "args": args, "env": {"PYTHONPATH": str(PACKAGE.parent)}}
    path.write_text(json.dumps({"mcpServers": cfg}, indent=1))
    return path


def room_addons(conf: dict[str, Any], want: Sequence[str], wid: str, folder: Path) -> tuple[dict[str, Any], list[str]] | str:
    """The room's own add-on tools a worker asks for, from its rooms.toml table (`conf`, [defaults] in it): each a tool's
    name as the room has it (mcp__hands__browser) or a server's name (hands) for every one of its tools the room's allow
    names. (its servers, each as the room runs it, its env with YERK_WORKER, YERK_WORK_DIR and YERK_TOOL_TIMEOUT; the tools), or why not:
    a tool the room has not, or a server whose table hands over a credential (an env name or header saying key, token,
    secret, pass, auth, cookie or credential), since an add-on's process and its table are the worker's shell's to read."""
    servers = {str(k): v for k, v in dict(conf.get("mcp") or {}).items() if isinstance(v, dict)}
    have = [str(t) for t in conf.get("allow") or [] if (m := ADDON.match(str(t))) and m.group(1) in servers]
    tools: list[str] = []
    for w in (str(x).strip() for x in want if str(x).strip()):
        hit = [t for t in have if t == w or ADDON.match(t).group(1) == w.removeprefix("mcp__")]  # type: ignore[union-attr]
        if not hit and (m := ADDON.match(w)) and m.group(2) and f"mcp__{m.group(1)}" in have:   # the room allows all of it
            hit = [w]
        if not hit:
            return f"this room has no add-on tool {w!r}; its add-on tools: {', '.join(have) or 'none'}"
        tools += [t for t in hit if t not in tools]
    out: dict[str, Any] = {}
    for name in dict.fromkeys(ADDON.match(t).group(1) for t in tools):  # type: ignore[union-attr]
        table = servers[name]
        if any(CREDENTIAL.search(str(k)) for part in ("env", "headers") for k in dict(table.get(part) or {})):
            return f"the add-on {name} is not given to a worker: its rooms.toml table passes a credential, which a worker's shell could read"
        env = {**dict(table.get("env") or {}), "YERK_WORKER": wid, "YERK_WORK_DIR": str(folder)}
        if isinstance(table.get("timeout"), int):
            env["YERK_TOOL_TIMEOUT"] = str(table["timeout"])
        out[name] = {**table, "env": env}
    return out, tools


def with_defaults(conf: dict[str, Any], given: tuple[dict[str, Any], list[str]], wid: str,
                  folder: Path) -> tuple[dict[str, Any], list[str]]:
    """A start's own add-ons (`given`, from room_addons) and the room's worker_addons after them, one list with no repeat. A
    default is never a refusal: one the room's allow has not is skipped, one whose server's table passes a credential
    skipped with one warning."""
    servers, tools, said = dict(given[0]), list(given[1]), set()
    for w in conf.get("worker_addons") or []:
        if isinstance(got := room_addons(conf, [str(w)], wid, folder), str):
            if got not in said and not got.startswith("this room has no"):
                log.warning("worker %s: worker_addons %s is skipped: %s", wid, w, got)
            said.add(got)
            continue
        servers |= {k: v for k, v in got[0].items() if k not in servers}
        tools += [t for t in got[1] if t not in tools]
    return servers, tools


def named(brief: str) -> list[str]:
    """The paths a brief names, each once in its order: every absolute path, and every ~/ one as the daemon's user's."""
    out: list[str] = []
    for m in NAMED.finditer(brief):
        if (raw := m[0].rstrip(NAMED_END)) and (s := str(Path(os.path.expanduser(raw)))) not in out:
            out.append(s)
    return out


def own_token(acct: Account) -> str:
    """The token file a worker's unit gets as YERK_TOKEN_FILE (Systemd.sandbox binds it back): its account's, when there."""
    return str(acct.token_file) if acct.token_file and acct.token_file.is_file() else ""


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
    namespacing (`sandbox`), so no tool or shell shape gets around it. Every unit gets `memory_max`
    as its memory ceiling with no swap past it, and outlives a kill for memory (OOMPolicy=continue)."""

    def __init__(self, state: Path | None = None, hidden: Sequence[Path] = (), memory_max: str = MEMORY_MAX, home: str = "open",
                 reach: Callable[[], Sequence[tuple[Path, str]]] | None = None):
        self.state, self.hidden, self.memory_max = state, tuple(hidden), memory_max
        self.home, self.reach = home, reach or (lambda: ())

    def hides(self, spec: dict[str, Any]) -> bool:
        """[workers] home: "hidden" hides the home from every worker, "needs" from a need's builder, cold read and fix (their
        spec carries the need: Workers.start), "open" from none."""
        return self.home == "hidden" or (self.home == "needs" and bool(spec.get("need")))

    def own(self, folder: Path, env: dict[str, str]) -> dict[str, str]:
        """A hidden worker's environment, its two folders made: a Claude config folder of its own and the workers' uv cache. Its
        account's config folder is not there for it: that one holds every room's transcripts on the account, and the settings and
        wrapper other programs run. Its login is its account's token file alone, so without one it does not start (OSError)."""
        if not env.get("YERK_TOKEN_FILE"):
            raise OSError("its account has no token file (accounts.toml, token), and a worker whose home is hidden has no other login")
        (folder / OWN_CONFIG).mkdir(mode=0o700, exist_ok=True)
        uv_cache().mkdir(mode=0o700, parents=True, exist_ok=True)
        return {**env, "CLAUDE_CONFIG_DIR": str(folder / OWN_CONFIG), "UV_CACHE_DIR": str(uv_cache())}

    def sandbox(self, folder: Path, env: dict[str, str]) -> list[str]:
        """The state folder read-only; the worker's own folder writable, and its account's config
        folder when that lives under the state folder (Claude Code writes its transcript there); the
        worker's own gate files (gates.GATE_FILES) read-only inside it; the
        install's secret, the private layer's secrets/ (every provider key and account token but the
        worker's own token, bound back read-only for its first step) and every room's folder (its
        token, settings and override) not there at all. The worker secret stays readable: a worker's
        read-only memory tools reach the daemon with it. PrivateUsers is what lets a user manager set
        up the mounts. A worker the home is hidden from (hides) gets `walled` instead."""
        if self.state is None:
            return []
        if self.hides(spec := self._spec(folder)):
            return [x for p in self.walled(spec, folder, env) for x in ("-p", p)]
        config = Path(env.get("CLAUDE_CONFIG_DIR") or folder)
        rw = [folder, *([config] if config != folder and config.is_relative_to(self.state) else [])]
        props = ["PrivateUsers=yes", f"ReadOnlyPaths={self.state}", *(f"ReadWritePaths={p}" for p in rw)]
        # its own gate (the spec the hook reads on every call, the hook, its tools) stays as the daemon wrote it: each file
        # read-only inside the writable folder, so no tool, shell shape or program of the worker's rewrites, moves or removes it
        props += [f"ReadOnlyPaths={folder / n}" for n in GATE_FILES if (folder / n).is_file()]
        rooms, token = self.state / "rooms", env.get("YERK_TOKEN_FILE", "")
        for p, back in self._fence(self._room(folder), token):
            if p != rooms and back:
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
            if back:
                props.append(f"BindPaths=-{back}")
        return [x for p in props for x in ("-p", p)]

    def walled(self, spec: dict[str, Any], folder: Path, env: dict[str, str]) -> list[str]:
        """A hidden home's unit: the home an empty read-only filesystem (systemd.exec's TemporaryFileSystem), the state folder
        too where it lives outside it (_roots), with only what the worker's job reaches bound back (back); its own gate files
        read-only, and a hidden path outside both (a private secrets/ elsewhere), as an open unit has them; then the DOORS
        that are there shut (the resolver's file back when it lives under one) and the WALLS."""
        roots, token = self._roots(), env.get("YERK_TOKEN_FILE", "")
        props = ["PrivateUsers=yes", *(f"TemporaryFileSystem={r}:ro" for r in roots)]
        for p, how in self.back(spec, folder, env):
            if how != "repo":
                props.append(f"{'BindReadOnlyPaths' if how.startswith('ro') else 'BindPaths'}={'-' if how.endswith('-') else ''}{p}")
                continue
            if (p / ".git").is_dir():         # git makes these two on first use, and .git is read-only in here
                with contextlib.suppress(OSError):
                    (p / ".git" / "worktrees").mkdir(exist_ok=True)
                    if not (p / ".git" / "FETCH_HEAD").exists():
                        (p / ".git" / "FETCH_HEAD").touch()
            # its list of worktrees is the unit's own: a worktree whose folder the unit hides reads as gone, so a `git worktree
            # prune` in here (a probe's model ran one by itself, 2026-11-24) would have dropped every other one's record
            props += [f"BindReadOnlyPaths={p}", *(f"BindPaths={p / '.git' / n}" for n in GIT_RW if (p / ".git" / n).exists()),
                      *([f"TemporaryFileSystem={p / '.git' / 'worktrees'}:mode=0700"] if (p / ".git" / "worktrees").is_dir() else [])]
        props += [f"ReadOnlyPaths={folder / n}" for n in GATE_FILES if (folder / n).is_file()]
        for p in (h for h in self.hidden if not any(h.is_relative_to(r) for r in roots)):
            mine = bool(token) and Path(token).is_relative_to(p)
            props += [f"TemporaryFileSystem={p}:ro", f"BindReadOnlyPaths=-{token}"] if mine else [f"InaccessiblePaths=-{p}"]
        shut = {"own": "TemporaryFileSystem={}:mode=0700", "gone": "InaccessiblePaths={}", "empty": "TemporaryFileSystem={}:ro"}
        return [*props, *(shut[how].format(p) for p, how in DOORS if p.exists()), *(f"BindReadOnlyPaths={p}" for p in resolver()), *WALLS]

    def back(self, spec: dict[str, Any], folder: Path, env: dict[str, str]) -> list[tuple[Path, str]]:
        """What a hidden home binds back, as (path, "rw" | "ro" | "repo", "-" after one the daemon names that may be missing),
        each once and only when it is there and inside what is hidden (_roots): the worker's own folder read and write (its
        Claude config folder in it: own), its account's token read-only, and for a worker that is not a need's its room's work
        folder as an open unit has it; of the state folder only `docs` (the public docs mirror), the same need's other workers'
        folders (_kin) and, for a worker with yerk_tools, the two files its memory server reads (_server), all read-only; and
        what `reach` names. Never ~/.ssh, ~/.netrc, an account's folder (its own too), the owner's uv cache, the private folder,
        a workspace of the owner's, or the state's ledger, memory, texts, logs, web cache, trash, rooms or another need's or a
        room's worker."""
        room, token, roots, state = self._bound(spec), env.get("YERK_TOKEN_FILE", ""), self._roots(), self.state or folder
        work = [(state / "rooms" / room / "work", "rw-")] if room and "/" not in room and room not in (".", "..") and not spec.get("need") else []
        want = [(folder, "rw"), *([(Path(token), "ro-")] if token else []),
                *work, (state / "docs", "ro"), *((folder.parent / w, "ro") for w in self._kin(spec) if w != folder.name),
                *(self._server() if spec.get("yerk_tools") else ()), *self.reach()]
        out: list[tuple[Path, str]] = []
        for p, how in want:
            if all(p != q for q, _ in out) and any(p.is_relative_to(r) for r in roots) and (how.endswith("-") or p.exists()):
                out.append((p, how))
        return out

    def _kin(self, spec: dict[str, Any]) -> list[str]:
        """The same need's workers (its builder, cold reads and fixes) by id, as the need's record keeps them (needs.update):
        one file read at a start, never a scan of the workers' folders; none for a worker that is not a need's."""
        from . import needs                            # late: needs is the pipeline's, and a room's worker never asks
        need = spec.get("need")
        if not isinstance(need, dict) or self.state is None:
            return []
        try:
            row = needs.find(needs.load(self.state), str(need.get("day")), int(need.get("n") or 0)) or {}
        except (OSError, ValueError, TypeError, needs.Unreadable):
            return []
        return [w for w in (str(x) for x in row.get("workers") or []) if w.isalnum()]

    def _server(self) -> list[tuple[Path, str]]:
        """What a worker's own memory server reads to reach the daemon (jobs.post_control): yerkd.json and the worker secret
        it names, read-only; nothing when the daemon has not written it."""
        try:
            named = json.loads((self.state / "yerkd.json").read_text()).get("worker_secret_file") if self.state else ""
        except (OSError, ValueError, AttributeError):
            return []
        return [(self.state / "yerkd.json", "ro"), (Path(named), "ro")] if self.state and named else []

    def _roots(self) -> list[Path]:
        """What a hidden home hides whole: the home, and the state folder where it lives outside it."""
        home = Path.home()
        return [home, *([self.state] if self.state and not self.state.is_relative_to(home) else [])]

    def _fence(self, room: str, token: str) -> list[tuple[Path, Path | None]]:
        """Each hidden path with what comes back inside it, for a worker of `room` (_room's) whose own token file is `token`:
        rooms/ its room's work folder, a secrets folder holding the token that file, any other nothing. The one reading of
        `hidden` the fence (sandbox) and the question (unseen) share, so the two cannot drift apart."""
        rooms, out = self.state / "rooms" if self.state else None, list[tuple[Path, Path | None]]()
        for p in self.hidden:
            if p != rooms and token and Path(token).is_relative_to(p):
                out.append((p, Path(token)))
            elif p != rooms:
                out.append((p, None))
            else:                                   # rooms/ itself: the one room folder a brief may name comes back
                out.append((p, p / room / "work" if room and "/" not in room and room not in (".", "..") else None))
        return out

    def walls(self, spec: dict[str, Any], folder: Path, env: dict[str, str]) -> list[tuple[Path, tuple[Path, ...]]]:
        """A hidden home's fence, as _fence's for an open one: what is hidden whole (_roots) with all it binds back (back),
        each of the DOORS that is there with nothing, and a hidden path outside both with its token. The one reading the
        unit (walled) and the question (unseen) share for a hidden worker."""
        roots, token = self._roots(), env.get("YERK_TOKEN_FILE", "")
        backs = tuple(p for p, _ in self.back(spec, folder, env))
        out = [(r, backs) for r in roots] + [(p, ()) for p, _ in DOORS if p.exists()]
        return out + [(h, (Path(token),) if token and Path(token).is_relative_to(h) else ()) for h in self.hidden
                      if not any(h.is_relative_to(r) for r in roots)]

    def unseen(self, room: str, bind: str, token: str, paths: Sequence[str], spec: dict[str, Any] | None = None,
               folder: Path | None = None) -> list[str]:
        """Of `paths`, those this unit hides from a worker of `room` (of `bind`, when the daemon's caller gave one; the calling
        room's own work folder is then hidden like any other room's) whose own token file is `token`: one of the hidden paths
        or under one, and not what comes back inside it (_fence). The rooms' root itself is hidden: a worker sees it holding
        one room. Each path as written and resolved, so a link into a hidden folder is hidden too, and a glob by the part
        before its first wildcard; with no state folder there is no fence and nothing is hidden. Each once, in order. For a
        worker the home is hidden from (hides: `spec`, the keys its worker.json will carry; `folder`, its folder), the fence is
        that unit's (walls): a path is hidden when nothing binds it back."""
        if self.state is None:
            return []
        s, out = {**(spec or {}), "room": room, "bind": bind}, list[str]()
        fence = (self.walls(s, folder, {"YERK_TOKEN_FILE": token}) if folder is not None and self.hides(s)
                 else [(p, (b,) if b else ()) for p, b in self._fence(self._bound(s), token)])
        for w in paths:
            here = [Path(WILD.split(w, 1)[0])]                  # a glob: the part before its first wildcard
            with contextlib.suppress(OSError, RuntimeError):
                here.append(here[0].resolve())
            if w not in out and any(q.is_relative_to(p) and not any(q.is_relative_to(b) for b in backs) for q in here for p, backs in fence):
                out.append(w)
        return out

    @staticmethod
    def _bound(spec: dict[str, Any]) -> str:
        """The room whose work folder comes back: the `bind` the daemon gave, else the worker's own room."""
        return str(spec.get("bind") or spec.get("room") or "")

    @classmethod
    def _room(cls, folder: Path) -> str:
        """The room whose work folder the worker gets, from the spec Workers.start wrote before the launch: its own, or the
        `bind` the daemon gave it (a pictures backfill: its card in the machine room, the pictures in the owner room's inbox)."""
        return cls._bound(cls._spec(folder))

    @staticmethod
    def _spec(folder: Path) -> dict[str, Any]:
        """The spec Workers.start wrote before the launch; {} when there is none or it is not one."""
        try:
            spec = json.loads((folder / "worker.json").read_text())
        except (OSError, ValueError):
            return {}
        return spec if isinstance(spec, dict) else {}

    def start(self, unit: str, argv: list[str], env: dict[str, str], cwd: Path, out: Path, err: Path, cap: float, post: list[str]) -> None:
        cmd = ["systemd-run", "--user", "--unit", unit, "--collect", "--quiet", "--working-directory", str(cwd),
               "--description", f"Yerk AI worker {unit.removeprefix(UNIT)}",
               "-p", f"RuntimeMaxSec={max(1, int(cap))}", "-p", f"StandardOutput=append:{out}",
               "-p", f"StandardError=append:{err}", "-p", f"ExecStopPost={unit_line(*post)}",
               "-p", "KillMode=control-group", "-p", "TimeoutStopSec=20",
               # past the ceiling the kernel kills the unit's biggest process, never the box's; the unit goes on without it
               "-p", f"MemoryMax={self.memory_max}", "-p", "MemorySwapMax=0", "-p", "OOMPolicy=continue", *self.sandbox(cwd, env),
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
    granted = [*(f"mcp__yerk__{t}" for t in spec.get("yerk_tools") or [] if t in WORKER_TOOLS), *spec.get("addon_tools", [])]
    if spec.get("mcp_config") and granted:     # the read-only tools from its own server, the room's add-on tools it was given
        argv += ["--mcp-config", str(spec["mcp_config"]), "--allowedTools", ",".join(granted)]
    if spec.get("settings"):                                   # the gates' hook (gate_settings)
        argv += ["--settings", str(spec["settings"])]
    os.dup2(os.open(spec["prompt"], os.O_RDONLY), 0)         # the prompt on stdin; the opened fd closes on exec
    os.execvpe(argv[0], argv, env)


def post(folder: Path) -> int:
    """ExecStopPost: how the unit ended, as systemd tells it, and how many of its processes the kernel killed for memory
    (oom_kills; left out when it cannot be read), in exit.json (written whole, then renamed)."""
    rec: dict[str, Any] = {"result": os.environ.get("SERVICE_RESULT", ""), "code": os.environ.get("EXIT_CODE", ""),
                           "status": os.environ.get("EXIT_STATUS", ""), "ended": round(time.time(), 3)}
    if (n := oom_kills()) is not None:
        rec["oom_kill"] = n
    tmp = folder / f"{EXIT}.tmp"
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, folder / EXIT)
    return 0


def oom_kills() -> int | None:
    """memory.events' oom_kill of the cgroup this process runs in: the processes any OOM killer killed there, its children's
    included; None when it cannot be read. Only inside a unit (systemd's INVOCATION_ID): ExecStopPost runs in the unit's own
    cgroup, which still exists then (seen on systemd 259, 2026-11-11), and anywhere else the count is another cgroup's."""
    if not os.environ.get("INVOCATION_ID"):
        return None
    try:
        own = next(x[3:] for x in OWN_CGROUP.read_text().splitlines() if x.startswith("0::"))
        events = (CGROUP / own.lstrip("/") / "memory.events").read_text().split()       # "low 0 high 0 ... oom_kill 1 ..."
        return int(events[events.index("oom_kill") + 1])
    except (OSError, ValueError, IndexError, StopIteration):
        return None


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
    why = words.died("oom-kill" if for_memory(end) else str(end.get("result_code") or "")) if state == "died" else ""
    return words.outcome(state, cap=words.span(float(row["cap"])), why=why, ticked=ticked, rows=len(items))


def for_memory(end: dict[str, Any]) -> bool:
    """A death for memory: systemd says oom-kill (a unit under OOMPolicy=stop exits 143), or the main
    process was killed with KILL in a unit whose cgroup counted a kill for memory (OOMPolicy=continue says signal)."""
    return end.get("exit") == "died" and (end.get("result_code") == "oom-kill"
                                          or (str(end.get("status") or "") == "KILL" and int(end.get("oom_kill") or 0) > 0))


def oom_note(end: dict[str, Any]) -> str:
    """The line of a worker that lost a process to memory and ended any other way; "" when none was or it died for memory."""
    n = int(end.get("oom_kill") or 0)
    return words.OOM_LOST[n > 1].format(n=n) if n > 0 and not for_memory(end) else ""


def cut_off(end: dict[str, Any]) -> bool:
    """A worker that died from a stop nobody asked for, a box shutdown (seen live on need 83's fix): settled "died",
    which a row with a stop request never is ("stopped"), and its exit the termination signal, exited 143 or killed by
    TERM (exit.json's status is systemd's EXIT_STATUS, the signal's name once killed); never a death for memory, which
    would only run out again (seen live: oom-kill, exited 143, started again)."""
    return end.get("exit") == "died" and str(end.get("status") or "") in ("143", "TERM") and not for_memory(end)


def footer(parts: Sequence[str]) -> list[str]:
    """A card's small lines: its parts joined by commas, a part that would pass phone width starting a line."""
    foot: list[str] = []
    for x in (x for x in parts if x):
        if foot and words.width(f"{foot[-1]}, {x}") <= words.PHONE:
            foot[-1] += f", {x}"
        else:
            foot.append(x)
    return [words.SUB + x for x in foot]


def card_text(row: dict[str, Any], items: list[tuple[bool, str]], end: dict[str, Any] | None = None,
              accounts: int = 1, privacy: str = "owner", look: words.Look | None = None,
              now: float | None = None) -> str:
    """The worker's card, state first (STYLE.md). Running: its state and its rows, then the time so
    far against the cap. Settled: the outcome in plain words, the report's first line (two phone lines at most), the
    rows as they were, then how long it ran and "report saved" with the command that opens it; settled quiet
    (end["quiet"]), the done mark and no rows: "finished clean" over the report's first line, or QUIET_WORKER when that
    line is the token itself or there is no report. A scheduled job's worker names its job first in the footer (need
    100 of 2026-11-16: six quiet job cards read alike, whatever each found). The account shows only when the install
    has more than one; the path only under presentation.paths = owner, in an owner room."""
    look = look or words.LOOK
    now = time.time() if now is None else now
    ticked = sum(1 for t, _ in items if t)
    started, cap = float(row["started"]), float(row["cap"])
    who, said = words.WORKER.format(id=row["id"]), ""
    job = words.named(words.WORKER_JOB, str(row["job"])) if row.get("job") else ""
    if end is None and row.get("waiting"):               # in line for its account: no time against the cap yet
        lines = [words.line(f"{words.WORKING} {words.WORKER_HEAD.format(state=words.WAITING_ACCOUNT)}"),
                 words.line(f"{words.WAITING} " + words.IN_LINE.format(nth=words.nth(int(row.get("line") or 1)),
                                                                        name=row["account"]))]
        lines += [words.line(f"{words.WAITING} {text}") for _, text in items[-ROWS_SHOWN:]]
        return "\n".join([*lines, *footer([job, who])])
    if end is None:
        mark, state = words.outcome("ticking" if items else "running", ticked=ticked, rows=len(items))
        when = (words.of_cap(now - started, cap) if look.times == "relative"
                else f"started {words.clock(started, now)}, cap {words.span(cap)}")
    else:
        mark, state = settled_state(row, items, end)
        said = str(end.get("result") or "") if end.get("has_report") else ""
        if end.get("quiet"):                             # its last message was exactly NO_REPLY: no token, no rows
            said = "" if said.strip() == words.NO_REPLY else said
            mark, state, items = words.DONE, words.outcome("done")[1] if said else words.QUIET_WORKER, []
        ran = float(end["finished"]) - started
        when = ((f"done in {words.span(ran)}" if end["exit"] == "done" else f"ran {words.of_cap(ran, cap)}")
                if look.times == "relative" else f"ended {words.clock(float(end['finished']), now)}, "
                                                  f"after {words.span(ran)}")
    lines = [words.line(f"{mark} {words.WORKER_HEAD.format(state=state)}")]
    if end is not None and (note := oom_note(end)):
        lines.append(words.line(note))
    lines += words.wrap(said)
    current = next((i for i, (t, _) in enumerate(items) if not t), None)
    stuck = words.WORKING if end is None else words.NOT_DONE if mark == words.NOT_DONE else words.WAITING
    rows = [words.line(f"{words.DONE if t else stuck if i == current else words.WAITING} {text}") for i, (t, text) in enumerate(items)]
    if len(rows) > ROWS_SHOWN:
        lines.append(words.SUB + words.EARLIER_ROWS.format(n=len(rows) - ROWS_SHOWN))
    lines += rows[-ROWS_SHOWN:]
    extra = [f"account {row['account']}"] if accounts > 1 or look.footer == "full" else []
    if end is not None and end["exit"] == "did not start":
        when = ""                                        # it never ran: no time to tell
    lines += footer([job, *([who] if end is None else []), when, *extra])
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
    head = (f"[worker {row['id']} finished] {state}, {words.of_cap(ran, float(row['cap']))}"
            + (f"; {note}" if (note := oom_note(end)) else "") + "\n" + (f"first line: \"{end['result']}\"\n" if end.get("result") else ""))
    try:
        body = Path(row["report"]).read_text(errors="replace").strip()
    except OSError:
        body = "(no report file)"
    saved = relay_line(row)
    whole = ("the report, all of it:\n", words.RELAY_ASK.format(under=words.under(cap), saved=saved, where="it is all here, so "
                                                                "there is no file to open"))
    part = ("the report begins:\n", words.RELAY_ASK.format(under=words.under(cap), saved=saved, where="the rest is in the report"))
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
                 launcher: Systemd | None = None, state: Path | None = None, hidden: Sequence[Path] = (), models: dict[str, str] | None = None,
                 reach: Callable[[], Sequence[tuple[Path, str]]] | None = None):
        self.ledger, self.conf, self.accounts, self.cooldowns = ledger, conf, accounts, cooldowns
        self.state = state or conf.root.parent             # the daemon's state folder (a worker's tools reach it)
        self.rooms, self.channels, self.room_confs, self.models = rooms, channels, room_confs, models or {}
        # a unit sees the state folder read-only, its own folder writable and `hidden` not at all
        # (the daemon's: the install's secret, every room's folder, the private layer's secrets/); Systemd.sandbox;
        # and every unit gets [workers] memory_max as its memory ceiling; with [workers] home hidden, the home is not there
        # but what `reach` names (the daemon's: its repositories, the toolchains, the gate)
        self.launcher: Systemd = launcher or Systemd(self.state, hidden, conf.memory_max, conf.home, reach)
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
                                    list(req.get("tasks") or []), effort=str(req.get("effort") or ""),
                                    addons=[str(a) for a in req.get("addons") or []])
        if action == "stop":
            return await self.stop(str(req.get("id", "")))
        if action == "list":
            return {"ok": True, "workers": listing(self.ledger)}
        if action == "report":                           # its git reads off the loop
            return await asyncio.to_thread(report, self.ledger, str(req.get("id", "")))
        return {"ok": False, "error": f"no action {action!r}; actions: start, list, stop, report"}

    def _account(self, name: str, model: str = "") -> Account | None:
        if (want := name or self.conf.account):
            return next((a for a in self.accounts if a.name == want), None)
        return pick(self.accounts, self.cooldowns, model=model) if self.accounts else None

    async def start(self, room: str, brief: str = "", brief_file: str = "", account: str = "", model: str = "",
                    cap_minutes: float = 0.0, tasks: list[str] | None = None, job: str = "",
                    more: dict[str, Any] | None = None, effort: str = "", addons: list[str] | None = None) -> dict[str, Any]:
        """`more`: keys the daemon's own callers add to worker.json (never the tool's or the command line's), none replacing one.
        `addons`: the room's own add-on tools the worker gets (room_addons), refused whole when one is not given; the room's
        worker_addons come with every start, whoever calls it (with_defaults)."""
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
        given = room_addons(self.room_confs.get(room, {}), addons, wid, d) if addons else ({}, [])
        if isinstance(given, str):
            return {"ok": False, "error": given}
        given = with_defaults(self.room_confs.get(room, {}), given, wid, d)
        d.mkdir(mode=0o700, parents=True)
        cap = (cap_minutes or self.conf.cap_minutes) * 60
        report, tasks_md = d / REPORT, d / TASKS
        bind, yerk = str((more or {}).get("bind") or ""), [t for t in WORKER_TOOLS if t in self.conf.yerk_tools]   # one list: words, worker.json, server
        hide = self.launcher.hides({**(more or {}), "room": room})            # [workers] home: as the unit will read it
        if (unseen := self.launcher.unseen(room, bind, own_token(acct), named(brief), {**(more or {}), "yerk_tools": yerk}, d)):   # told,
            log.info("worker %s: its brief names %d path(s) its unit hides", wid, len(unseen))   # never refused: a brief may quote what it never reads
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
            + (f"- You have no room session and no subagents. Of Yerk AI's tools you have {' and '.join(f'mcp__yerk__{t}' for t in yerk)} and no other: "
               f"{'they read' if yerk[1:] else 'it reads'} the memory of the room that started you, as the room does. Where the brief depends on what "
               "the owner or the room said, decided or keeps on file, look there first. " if yerk else
               "- You have no room session, no Yerk AI tools and no subagents. ") + f"Your cap is {cap / 60:g} minutes; past it the run is stopped.\n"
            + (f"- You have the room's own {', '.join(given[1])} too. A step that needs the owner's word (`confirmed`) is "
               "refused, since nobody is here to give it: leave that step ready, and say in your report what waits for "
               "the word, for the room to do once the owner says it.\n" if given[1] else "")
            + "- Your tool calls pass Yerk AI's gates: a plain delete in your folder goes to the trash; a delete "
            "elsewhere or after a cd, an external action (a post, a login, money, a message out) and a change to "
            "Yerk AI's own config are refused. Put what you could not do in your report.\n"
            + (f"- Not there for you, since your unit hides them: {', '.join(unseen)}. The brief names them; name them in your "
               "report's first lines, with what you did without them.\n" if unseen else "")
            + "\n"
            f"The brief (also in {d / 'BRIEF.md'}):\n\n{brief.strip()}\n")
        sid = str(uuid.uuid4())
        spec = {"id": wid, "room": room, "account": acct.name, "model": model, "effort": effort, "session_id": sid,
                "claude": shutil.which(claude_bin()) or claude_bin(), "tools": self.conf.tools,
                "permission_mode": self.conf.permission_mode, "prompt": str(prompt), "report": str(report),
                "tasks": str(tasks_md), "cap": cap, "gate": walled_trash(self.gate, d) if hide else self.gate,
                "settings": str(gate_settings(d))}
        spec.update({k: v for k, v in (more or {}).items() if k not in spec})
        if yerk:
            spec["yerk_tools"] = yerk
        if given[1]:
            spec["addon_tools"] = given[1]
        if yerk or given[1]:
            spec["mcp_config"] = str(mcp_config(self.state, room, yerk, d / "mcp.json", given[0]))
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
               "staging": str(d), "report": str(report), "tasks": str(tasks_md), "cap_minutes": cap / 60,
               **({"addons": given[1]} if given[1] else {}),
               **({"unseen": unseen, "unseen_note": words.WORKER_UNSEEN.format(
                   paths=words.few(unseen), work=self.state / "rooms" / (bind or room) / "work")}
                  if unseen else {})}
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
        if (token := own_token(acct)):
            env["YERK_TOKEN_FILE"] = token
        me = [sys.executable, "-P", "-m", "yerk_ai.workers"]    # -P: nothing in the folder can shadow an import
        try:
            if self.launcher.hides(spec := self.launcher._spec(d)):
                env = self.launcher.own(d, env)
                # said in its spec, here and never at the start: a worker in line launches under the setting of this moment, and
                # the daemon runs no git in a folder a hidden unit wrote (hid)
                (d / "worker.json").write_text(json.dumps({**spec, "hidden": True}, indent=1))
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
               "has_report": report.is_file(), "result_code": x.get("result") or "", "status": str(x.get("status") or ""),
               "oom_kill": x["oom_kill"] if isinstance(x.get("oom_kill"), int) else 0}
        if said := words.died("oom-kill") if for_memory(end) else oom_note(end):      # memory, in plain words, in the ledger too
            detail = end["detail"] = f"{detail}; {said}"
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
    return [{"id": r["id"], "room": r["room"], "account": r["account"], "unit": r["unit"], "job": r.get("job") or "",
             "state": r["exit"] or (words.WAITING_ACCOUNT if r.get("waiting") else "running"), "elapsed": took((r["finished"] or now) - r["started"]),
             "cap_minutes": r["cap"] / 60, "result": r["result"], "report": r["report"],
             "relay": r["relay"], "relay_note": r["relay_note"]} for r in ledger.workers(limit=limit)]


def worktrees(folder: Path) -> list[str]:
    """Each git worktree in a worker's folder that an owner room's Read reaches (WORKTREES; a linked one's .git is a file),
    by its path, so a room checks the worker's claim about its change there; none once needs.tidy or the sweep took them."""
    return [str(p.parent) for p in sorted(folder.glob("*/.git")) if p.is_file() and any(fnmatch(p.parent.name, w) for w in WORKTREES)]


def pointer(tree: Path) -> Path | None:
    """Where a worktree's .git file points, read as git reads it: one path, absolute, or relative to the worktree (git's
    `worktree add --relative-paths`); None for anything else."""
    try:
        m = re.fullmatch(r"gitdir: (\S(?:[^\n]*\S)?)\n?", (tree / ".git").read_text())
        return (Path(m[1]) if m[1].startswith("/") else Path(os.path.normpath(tree.resolve() / m[1]))) if m else None
    except (OSError, ValueError, RuntimeError):
        return None


def hid(folder: Path) -> bool:
    """Did this worker's unit hide the home? Its worker.json says so from the launch (Workers._launch), and that file the worker
    cannot write. All else in such a folder is its own to write, a worktree's .git and whatever that names too, and nothing in
    it is in any repository's list (the unit's began empty), so the daemon runs no git there (branch, needs.tidy)."""
    return Systemd._spec(folder).get("hidden") is True


def linked(tree: Path) -> bool:
    """Is this a worktree its repository knows: its .git names a folder of that repository's .git/worktrees, which names it back
    and no other common folder? Git runs out here only on one that is. A hidden worker's never is (its unit's list of worktrees
    is its own), and a .git it wrote to name a repository of its making (in the workers' folders, the workers' uv cache or inside
    a .git) would have git out here run what that repository's config names."""
    if tree.is_symlink() or (admin := pointer(tree)) is None:      # a link to another's worktree is that one's, never this worker's
        return False
    try:
        real, back = admin.resolve(), (admin / "gitdir").read_text().strip()
        return (real == admin and real.parts.count(".git") == 1 and real.parts[-3:-1] == (".git", "worktrees")
                and not any(real.is_relative_to(d.resolve()) for d in (tree.parent.parent, uv_cache()))
                and (Path(back) if back.startswith("/") else Path(os.path.normpath(admin / back))) == tree.resolve() / ".git"
                and (admin / "commondir").read_text().strip() == "../..")
    except (OSError, ValueError, RuntimeError):
        return False


def branch(tree: Path, now: float | None = None) -> dict[str, Any]:
    """A worktree's branch as git has it, so a room checks a worker's claim on its commits (need 134 of 2026-11-21: a plug-in
    worktree's .git is a pointer into the plug-in's own .git, which no room reads, and a commit there is compressed, so a
    relay read neither its head nor its commits): its repository's own checkout (`repo`) and the branch that is on (`base`),
    its branch (None when detached) and head, how many commits each side has that the other lacks, the newest COMMITS of
    those its base lacks (of its own history when that checkout is on no branch, and when its base holds its head, `on_base`:
    a branch landed since), each sha, subject and age, and how many of its files are not committed. Read only: no fetch, no ref, lock or
    index written, no fsmonitor or signature program run (a clean filter its config names still can); a worktree git cannot read is git's first line of why."""
    now = time.time() if now is None else now

    def git(*a: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "log.showSignature=false",
                                   "-C", str(tree), *a], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            return subprocess.CompletedProcess([], 127, "", str(e))
    if hid(tree.parent) or not linked(tree) and ((at := pointer(tree)) is None or at.parent.exists()):   # a pointer to nowhere: git says so itself
        return {"worktree": str(tree), "error": words.BRANCH_UNREAD.format(why=words.BRANCH_UNKNOWN)}
    if (h := git("rev-parse", "--verify", "HEAD^{commit}")).returncode:
        return {"worktree": str(tree), "error": words.BRANCH_UNREAD.format(why=(h.stderr.strip().splitlines() or ["?"])[0][:160])}
    head, first = h.stdout.strip(), git("worktree", "list", "--porcelain").stdout.split("\n\n", 1)[0].splitlines()
    base = next((x.removeprefix("branch refs/heads/") for x in first if x.startswith("branch refs/heads/")), "")
    out: dict[str, Any] = {"worktree": str(tree), "repo": next((x[9:] for x in first if x.startswith("worktree ")), ""), "base": base,
                           "branch": git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or None, "head": head[:7]}
    if base and len(n := git("rev-list", "--left-right", "--count", f"refs/heads/{base}...{head}", "--").stdout.split()) == 2:
        out["only_on_base"], out["not_on_base"], out["on_base"] = int(n[0]), int(n[1]), n[1] == "0"
    log_ = git("log", "-n", str(COMMITS), "--format=%H%x1f%ct%x1f%s", f"refs/heads/{base}..{head}" if base and not out.get("on_base") else head, "--")
    out["commits"] = [{"sha": c[0][:7], "subject": words.line(c[2], 120), "when": words.since(now - int(c[1]))}
                      for c in (x.split("\x1f", 2) for x in log_.stdout.splitlines()) if len(c) == 3 and c[1].isdigit()]
    st = git("status", "--porcelain", "--untracked-files=normal")
    out["not_committed"] = len(st.stdout.splitlines()) if st.returncode == 0 else None
    return out


def report(ledger: Ledger, wid: str, limit: int = 6000) -> dict[str, Any]:
    """A worker's report: its path, first line and text (at most `limit` characters), its worktrees and each one's branch
    as git has it (branch)."""
    if (row := ledger.worker_get(wid)) is None:
        return {"ok": False, "error": f"no worker {wid!r}"}
    try:
        text = Path(row["report"]).read_text(errors="replace")
    except OSError:
        text = ""
    trees = worktrees(Path(row["staging"])) if row["staging"] else []
    return {"ok": True, "id": wid, "state": row["exit"] or "running", "report": row["report"], "job": row.get("job") or "",
            "first_line": first_line(Path(row["report"])), "text": text[:limit], "trimmed": len(text) > limit,
            "unit": row["unit"], "exit_detail": row["exit_detail"], "staging": row["staging"], "account": row["account"],
            "model": row["model"], "worktrees": trees, "branches": [branch(Path(t)) for t in trees]}


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
