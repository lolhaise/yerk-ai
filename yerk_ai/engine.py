"""The only code that speaks Claude Code's headless protocol.

One long-lived `claude -p` per active room, stream-json both ways:
- stdin: one JSON line per user message
- stdout: one JSON event per line (system, replayed user lines, stream events, assistant, result)

Yerk AI mints every session id and keeps the room-to-session table, so no other run can rebind
a room, and a lock file lets one live process own a session at a time. Settings come only from
`--settings` (`--setting-sources ""`), MCP servers only from `--mcp-config` (`--strict-mcp-config`),
tools only from the `--tools` allowlist, and the assembled context from
`--append-system-prompt-file`.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import queue
import secrets
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Callable

from .accounts import Account, account_env
from .config import write_private
from .ledger import Ledger
from .lifted.classify import Failure, FailureTable
from .transcribe import is_audio

log = logging.getLogger("yerk.engine")
STREAM_FLAGS = ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--replay-user-messages"]
CONTEXT = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")   # a call's usage: the session's size


def claude_bin() -> str:
    return os.environ.get("YERK_CLAUDE", "claude")


def project_dir(cwd: Path) -> str:
    """Claude Code's folder for a working directory under <config dir>/projects/ (its 2.1.301 sanitizer; the docs agree):
    each UTF-16 unit outside [a-zA-Z0-9] a "-"; past 200, the first 200, "-" and |the path's string hash| in base 36."""
    units, h, tail = memoryview(str(cwd).encode("utf-16", "surrogatepass")[2:]).cast("H"), 0, ""
    for u in units:                               # JavaScript's 32-bit string hash: h * 31 + unit
        h = (h * 31 + u) % 2 ** 32
    name, h = "".join(chr(u) if chr(u).isascii() and chr(u).isalnum() else "-" for u in units), min(h, 2 ** 32 - h)
    while h or not tail:                          # h is now the signed 32-bit value's absolute value
        h, tail = h // 36, "0123456789abcdefghijklmnopqrstuvwxyz"[h % 36] + tail
    return name if len(name) <= 200 else f"{name[:200]}-{tail}"


class SessionBusy(RuntimeError):
    """Another live process holds this session."""


@dataclass
class RoomSpec:
    room: str
    model: str
    cwd: Path
    tools: list[str] = field(default_factory=list)           # --tools allowlist; [] means none
    allow: list[str] = field(default_factory=list)           # --allowedTools permission rules
    settings: dict[str, Any] = field(default_factory=dict)   # written to a file for --settings
    context_file: Path | None = None                         # --append-system-prompt-file
    mcp_config: Path | None = None                           # --mcp-config
    add_dirs: list[Path] = field(default_factory=list)       # --add-dir: folders the room may read
    extra_args: list[str] = field(default_factory=list)
    token: str = ""                                          # this process's credential for /hook and /mcp (bound with the session)


def token_hash(token: str) -> str:
    """A room token as the session table keeps it: its SHA-256, so the file holds no live token."""
    return hashlib.sha256(token.encode()).hexdigest()


def hashed(rows: dict[str, Any]) -> dict[str, Any]:
    """The rows with each plain `token` (a table from before the hashes) moved to `token_sha256`."""
    out: dict[str, Any] = {}
    for room, row in rows.items():
        if isinstance(row, dict) and "token" in row:
            row = dict(row)
            tok = str(row.pop("token") or "")
            row["token_sha256"] = token_hash(tok) if tok else ""
        out[room] = row
    return out


class SessionTable:
    """room -> {session_id, account, bound, token_sha256, transcript}: a JSON file only Yerk AI writes.
    `token_sha256` is the hash of the credential the room's process sends on every hook and MCP call
    (the daemon knows the room by it: identity from the binding, never from a header a caller picks);
    the token itself is never written, so a copy of the file opens nothing. `transcript` is the
    session's transcript path, learnt from the first hook call, so a hop resumes by path.
    Every write holds the table's lock file (flock) and lands through a temp file of the writer's own,
    so two rooms binding together both keep their rows (restart test R3)."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            return dict(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            return {}

    def load(self) -> dict[str, dict[str, Any]]:
        """The rows; a table that still holds plain tokens is rewritten with their hashes, once."""
        data = self._read()
        if not any(isinstance(r, dict) and "token" in r for r in data.values()):
            return data
        try:
            return self._write(None, {})
        except OSError:                          # a reader that cannot write still finds rooms by the hash
            return hashed(data)

    def get(self, room: str) -> dict[str, Any] | None:
        return self.load().get(room)

    def by_token(self, token: str) -> str | None:
        """The room whose live process holds `token`, or None (an unbound call): the caller's value is
        hashed and each row's hash compared in constant time, like the install's secret (cold read F7)."""
        if not token:
            return None
        want = token_hash(token).encode()
        return next((room for room, row in self.load().items()
                     if hmac.compare_digest(str(row.get("token_sha256") or "").encode(), want)), None)

    def by_session(self, session_id: str) -> str | None:
        if not session_id:
            return None
        return next((room for room, row in self.load().items() if row.get("session_id") == session_id), None)

    def update(self, room: str, **fields: Any) -> dict[str, Any]:
        """Set fields of a room's row under the lock; the row is created when missing."""
        return dict(self._write(room, fields)[room])

    def _write(self, room: str | None, fields: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Under the lock: the table read again, `fields` set on `room`'s row (None: no change), every
        plain token hashed, the whole table written."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._read()
            if room is not None:
                data[room] = {**dict(data.get(room) or {}), **fields}
            data = hashed(data)
            write_private(self.path, json.dumps(data, indent=1))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return data

    def bind(self, room: str, session_id: str, account: str, token: str = "", transcript: str = "") -> None:
        """A process start: the session, the account, the hash of this process's token; the transcript
        path is kept when the session is the same one (a hop) and cleared when it is new."""
        prev = self.get(room) or {}
        keep = transcript or (str(prev.get("transcript") or "") if prev.get("session_id") == session_id else "")
        self.update(room, session_id=session_id, account=account, bound=round(time.time(), 3),
                    token_sha256=token_hash(token or secrets.token_hex(16)), transcript=keep)


class SessionLock:
    """An exclusive, non-blocking flock on <state>/locks/<session>.lock."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> "SessionLock":
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SessionBusy(self.path.stem) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd
        return self

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def visible(e: dict[str, Any]) -> bool:
    """A stream event a progress card could show: the first words, or a tool starting."""
    if e.get("type") == "content_block_delta":
        return (e.get("delta") or {}).get("type") == "text_delta"
    if e.get("type") == "content_block_start":
        return (e.get("content_block") or {}).get("type") == "tool_use"
    return False


@dataclass
class Turn:
    started: float
    first_visible: float | None = None
    ended: float | None = None
    accepted: list[float] = field(default_factory=list)      # times our user lines were replayed
    text: str = ""
    subtype: str = ""
    is_error: bool = False
    error_type: str | None = None
    outcome: str = "running"                                  # ok, error, empty, timeout, exited
    models: list[str] = field(default_factory=list)          # the models that executed
    fallback_model: str | None = None
    flags: list[str] = field(default_factory=list)            # stream events the failure table reads
    windows: dict[str, Any] | None = None                     # the last rate_limit_event's usage windows
    resets: float | None = None                               # when the limit a rejected rate_limit_event hit resets (epoch)
    tools: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    failure: Failure | None = None
    context: int = 0                                          # the session's tokens at its last call (0: none said)
    compacted: dict[str, Any] | None = None                   # a compaction's compact_metadata (pre_tokens, post_tokens)

    def observe(self, t: float, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        if kind == "user" and ev.get("isReplay"):
            self.accepted.append(t)
        elif kind == "stream_event":
            if self.first_visible is None and visible(ev.get("event") or {}):
                self.first_visible = t
        elif kind == "assistant":
            if ev.get("error"):
                self.error_type = str(ev["error"])
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    self.tools.append(str(c.get("name")))
            u = (ev.get("message") or {}).get("usage") or {}       # a subagent's calls (parent_tool_use_id) are not the room's
            if not ev.get("parent_tool_use_id") and (n := sum(int(u.get(k) or 0) for k in CONTEXT)):
                self.context = n
        elif kind == "system" and ev.get("subtype") == "compact_boundary":
            self.compacted = dict(ev.get("compact_metadata") or {})
            self.context = int(self.compacted.get("post_tokens") or 0) or self.context
        elif kind == "system" and ev.get("subtype") == "model_refusal_fallback":
            self.fallback_model = ev.get("fallback_model") or "another model"
        elif kind == "system" and ev.get("subtype") == "notification" and str(ev.get("key") or "").endswith("-hook-error"):
            self.flags.append(f"hook_error:{str(ev.get('text') or '').split(' · ')[0][:120]}")   # its words, minus the key hint
        elif kind == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            self.windows = info.get("unifiedWindows") or self.windows
            if info.get("status") == "rejected":
                self.flags.append("credits_required" if info.get("errorCode") == "credits_required" else "rate_limit_rejected")
                self.resets = info.get("resetsAt") or ((self.windows or {}).get(str(info.get("rateLimitType"))) or {}).get("resetsAt")
        elif kind == "result" and (ev.get("origin") or {}).get("kind") != "task-notification":   # a result of Claude Code's
            self.result, self.ended = ev, t        # own turn for a background command's notice (a resume reports the orphans) ends nothing
            self.text = str(ev.get("result") or "")
            self.subtype = str(ev.get("subtype") or "")
            self.is_error = bool(ev.get("is_error"))
            self.models = list((ev.get("modelUsage") or {}).keys())
            self.errors = [str(x) for x in ev.get("errors") or []]
            self.outcome = "error" if self.is_error or self.subtype != "success" else "ok"

    def ms(self, t: float | None) -> int | None:
        return None if t is None else int((t - self.started) * 1000)


class RoomProcess:
    """One Claude Code process for one room's session."""

    def __init__(self, spec: RoomSpec, account: Account, state: Path, *, ledger: Ledger | None = None,
                 failures: FailureTable | None = None, log: Path | None = None,
                 meters: Callable[[str, dict[str, Any]], None] | None = None, evidence: Any = None):
        self.spec, self.account, self.state = spec, account, state
        self.projects = account.config_dir / "projects" / project_dir(spec.cwd.resolve())   # its own sessions
        self.evidence = evidence               # the room's evidence ledger: every tool call and what it read
        self.ledger, self.failures, self.log_path, self.meters = ledger, failures, log, meters
        self.sessions = SessionTable(state / "sessions.json")
        self.events: queue.Queue[tuple[float, dict[str, Any] | None]] = queue.Queue()
        self.proc: subprocess.Popen[bytes] | None = None
        self.session_id = ""
        self.resume_arg: str | None = None
        self.lock: SessionLock | None = None
        self.room_dir = state / "rooms" / spec.room
        self.stderr_path = self.room_dir / "stderr.log"
        self._log: IO[str] | None = None
        self._log_lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        self.read_paths: set[str] = set()      # every file the process asked to Read (the Stop check reads it)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def argv(self) -> list[str]:
        s = self.spec
        a = [claude_bin(), *STREAM_FLAGS, "--model", s.model, "--setting-sources", "", "--strict-mcp-config", "--tools", ",".join(s.tools)]
        a += ["--resume", self.resume_arg] if self.resume_arg else ["--session-id", self.session_id]
        if s.mcp_config:
            a += ["--mcp-config", str(s.mcp_config)]
        for d in [*s.add_dirs, self.projects]:    # its own sessions' folder only, where a tool output too big to return
            a += ["--add-dir", str(d)]           # is saved (<session>/tool-results/), so it can Read it back
        if s.allow:
            a += ["--allowedTools", ",".join(s.allow)]
        if s.settings:
            a += ["--settings", str(self.room_dir / "settings.json")]
        if s.context_file:
            a += ["--append-system-prompt-file", str(s.context_file)]
        return a + s.extra_args

    def start(self, *, fresh: bool = False, resume: str | None = None) -> str:
        """Start the room's process: resume `resume` (a session id or a transcript path), else
        the room's bound session, else a newly minted one (always when `fresh`)."""
        row = None if fresh else self.sessions.get(self.spec.room)
        if resume:
            self.resume_arg, self.session_id = resume, Path(resume).stem if resume.endswith(".jsonl") else resume
        elif row:
            self.session_id = str(row["session_id"])
            # Claude Code searches only its own account's projects folder for an id, and a session resumed by path goes
            # on writing where its transcript was: a transcript outside this account's folder (a hop, a room put on another
            # account, and every start after) resumes by its absolute path (restart test R1); one inside it, by id.
            path = str(row.get("transcript") or "")
            self.resume_arg = path if path and Path(path).parent != self.projects and os.path.isfile(path) else self.session_id
        else:
            self.resume_arg, self.session_id = None, str(uuid.uuid4())
        (self.state / "locks").mkdir(parents=True, exist_ok=True)
        self.room_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):        # an --add-dir missing at start is dropped; 0700 as Claude Code's own
            self.projects.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = SessionLock(self.state / "locks" / f"{self.session_id}.lock").acquire()
        self.sessions.bind(self.spec.room, self.session_id, self.account.name, self.spec.token)
        if self.spec.settings:
            write_private(self.room_dir / "settings.json", json.dumps(self.spec.settings, indent=1))
        if self.log_path:
            try:
                self._log = open(self.log_path, "a")
            except OSError as e:                  # the stream log is a record of the turn, never the turn (disk-full drill c)
                log.warning("room %s: no stream log this process: %s", self.spec.room, e)
            self._note({"argv": self.argv(), "account": self.account.name, "session_id": self.session_id})
        err = open(self.stderr_path, "ab")
        self.proc = subprocess.Popen(self.argv(), cwd=self.spec.cwd, env={**account_env(self.account), "CLAUDE_CODE_MCP_ALLOWLIST_ENV": "1"},
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)   # a stdio server: a baseline env, not ours
        err.close()
        threading.Thread(target=self._read, name=f"read-{self.spec.room}", daemon=True).start()
        return self.session_id

    def _note(self, obj: dict[str, Any]) -> None:
        """One line in the stream log. A write that fails (the disk full, the file past a size limit) ends the log
        for this process and is said once; the turn goes on, since a reader thread that died here would leave the
        turn waiting out its whole silence timeout (disk-full drill c)."""
        if self._log:
            with self._log_lock:
                if self._log is None:             # closed under a shutdown while this line waited for the lock
                    return
                try:
                    self._log.write(json.dumps({"t": round(time.time(), 3), **obj}) + "\n")
                    self._log.flush()
                except (OSError, ValueError) as e:
                    log.warning("room %s: the stream log cannot be written, the rest of this process goes unlogged: %s", self.spec.room, e)
                    with contextlib.suppress(OSError):
                        self._log.close()
                    self._log = None

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            t = time.time()
            try:
                ev = json.loads(raw)
            except ValueError:
                ev = {"type": "_raw", "text": raw.decode(errors="replace")[:2000]}
            self._note({"e": ev})
            if ev.get("type") == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use" and c.get("name") in ("Read", "mcp__yerk__transcribe"):   # opened, or heard
                        p = str((c.get("input") or {}).get("file_path") or (c.get("input") or {}).get("path") or "")
                        if c.get("name") != "Read" or not is_audio(p):      # a recording is heard only by transcribing it
                            self.read_paths.add(p)
            if self.evidence is not None:
                with contextlib.suppress(Exception):   # the ledger never stops the stream
                    self.evidence.observe(ev)
            self.events.put((t, ev))
        self.events.put((time.time(), None))

    def send(self, text: str) -> float:
        """Write one user message on stdin; returns the time it was written. A process already gone
        (`--resume` of a session that is gone exits at once, before its first message under load) takes
        nothing: the turn reads its exit and stderr like any other, never a crashed turn."""
        assert self.proc and self.proc.stdin
        line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
        with self._stdin_lock:
            t = time.time()
            try:
                self.proc.stdin.write(line.encode())
                self.proc.stdin.flush()
            except BrokenPipeError:
                self._note({"unsent": text})
                return t
        self._note({"sent": text})
        return t

    def next_event(self, timeout: float) -> tuple[float, dict[str, Any] | None] | None:
        try:
            return self.events.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def read_turn(self, started: float, timeout: float = 600.0, record: bool = True,
                  on_event: Callable[[Turn, dict[str, Any]], None] | None = None) -> Turn:
        """Consume events until the turn's result, `timeout` seconds of silence, the 8-hour ceiling, or the process exiting.
        `on_event` sees every event after the turn has observed it (the progress card's feed)."""
        turn = Turn(started=started)
        deadline, ceiling = time.monotonic() + timeout, time.monotonic() + 8 * 3600     # silence ends a turn, never its length
        while turn.ended is None:
            if (item := self.next_event(min(deadline, ceiling) - time.monotonic())) is None:
                turn.outcome = "timeout"
                break
            (t, ev), deadline = item, time.monotonic() + timeout     # every event restarts the silence window
            if ev is None:
                turn.outcome = "exited"
                break
            turn.observe(t, ev)
            if self.meters and ev.get("type") == "rate_limit_event" and turn.windows:
                self.meters(self.account.name, turn.windows)
            if on_event:
                on_event(turn, ev)
        if turn.ended is None:
            turn.ended = time.time()
        if turn.outcome == "ok" and not turn.text.strip():
            turn.outcome = "empty"      # an empty reply is a failure to retry fresh, never a silence
        if turn.outcome not in ("ok", "empty") and self.failures:
            text = " ".join([turn.text, *turn.errors, self.stderr_tail()])
            turn.failure = self.failures.classify(turn.error_type, text, turn.flags)
        elif turn.outcome == "ok" and turn.fallback_model and self.failures:
            turn.failure = self.failures.event("model_refusal_fallback")   # the answer stands, marked
        if record:
            self.record(turn)
        return turn

    def ask(self, text: str, timeout: float = 600.0) -> Turn:
        return self.read_turn(self.send(text), timeout)

    def record(self, turn: Turn, inbound: list[str] | None = None, **extra: Any) -> int:
        """One ledger row for the turn; `extra` carries the room's hop, recap and out_of columns."""
        if not self.ledger:
            return 0
        f = turn.failure
        return self.ledger.record(
            room=self.spec.room, account=self.account.name, config_dir=str(self.account.config_dir),
            session_id=self.session_id, started=turn.started, first_visible=turn.first_visible,
            ended=turn.ended, outcome=turn.outcome, failure=f.cls if f else None,
            action=f.action if f else None, output_chars=len(turn.text), models=",".join(turn.models),
            fallback_model=turn.fallback_model, accepted=len(turn.accepted), model=self.spec.model,
            inbound=",".join(inbound) if inbound else None, source=f.source if f else None, context=turn.context or None, **extra)

    def stderr_tail(self, n: int = 2000) -> str:
        try:
            with open(self.stderr_path, "rb") as f:
                f.seek(max(f.seek(0, os.SEEK_END) - n, 0))
                return f.read().decode(errors="replace")
        except OSError:
            return ""

    def end_now(self, grace: float = 5.0) -> None:
        """/stop: TERM the process mid-turn, KILL it if it lingers; its reader then sees the end, and
        close() still frees the session."""
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def close(self, grace: float = 15.0) -> int | None:
        """End the process (EOF on stdin, then terminate, then kill) and free the session."""
        rc = None
        if self.proc:
            if self.proc.stdin:
                with contextlib.suppress(OSError):
                    self.proc.stdin.close()
            try:
                rc = self.proc.wait(grace)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    rc = self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    rc = self.proc.wait()
            self._note({"exit": rc})
        if self.lock:
            self.lock.release()
        with self._log_lock:                      # never under a line the reader thread is writing
            if self._log:
                self._log.close()
                self._log = None
        return rc

    def __enter__(self) -> "RoomProcess":
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None) -> None:
        self.close()
