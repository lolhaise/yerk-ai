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

import fcntl
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Callable

from .accounts import Account, account_env
from .ledger import Ledger
from .lifted.classify import Failure, FailureTable

STREAM_FLAGS = ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--replay-user-messages"]


def claude_bin() -> str:
    return os.environ.get("YERK_CLAUDE", "claude")


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


class SessionTable:
    """room -> {session_id, account, bound}: a JSON file only Yerk AI writes."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            return dict(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            return {}

    def get(self, room: str) -> dict[str, Any] | None:
        return self.load().get(room)

    def bind(self, room: str, session_id: str, account: str) -> None:
        data = self.load()
        data[room] = {"session_id": session_id, "account": account, "bound": round(time.time(), 3)}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.path)


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
    tools: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    failure: Failure | None = None

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
        elif kind == "system" and ev.get("subtype") == "model_refusal_fallback":
            self.fallback_model = ev.get("fallback_model")
        elif kind == "result":
            self.result, self.ended = ev, t
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
                 failures: FailureTable | None = None, log: Path | None = None):
        self.spec, self.account, self.state = spec, account, state
        self.ledger, self.failures, self.log_path = ledger, failures, log
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

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def argv(self) -> list[str]:
        s = self.spec
        a = [claude_bin(), *STREAM_FLAGS, "--model", s.model, "--setting-sources", "",
             "--strict-mcp-config", "--tools", ",".join(s.tools)]
        a += ["--resume", self.resume_arg] if self.resume_arg else ["--session-id", self.session_id]
        if s.mcp_config:
            a += ["--mcp-config", str(s.mcp_config)]
        for d in s.add_dirs:
            a += ["--add-dir", str(d)]
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
            self.resume_arg = self.session_id = str(row["session_id"])
        else:
            self.resume_arg, self.session_id = None, str(uuid.uuid4())
        (self.state / "locks").mkdir(parents=True, exist_ok=True)
        self.room_dir.mkdir(parents=True, exist_ok=True)
        self.lock = SessionLock(self.state / "locks" / f"{self.session_id}.lock").acquire()
        self.sessions.bind(self.spec.room, self.session_id, self.account.name)
        if self.spec.settings:
            (self.room_dir / "settings.json").write_text(json.dumps(self.spec.settings, indent=1))
        if self.log_path:
            self._log = open(self.log_path, "a")
            self._note({"argv": self.argv(), "account": self.account.name, "session_id": self.session_id})
        err = open(self.stderr_path, "ab")
        self.proc = subprocess.Popen(self.argv(), cwd=self.spec.cwd, env=account_env(self.account),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
        err.close()
        threading.Thread(target=self._read, name=f"read-{self.spec.room}", daemon=True).start()
        return self.session_id

    def _note(self, obj: dict[str, Any]) -> None:
        if self._log:
            with self._log_lock:
                self._log.write(json.dumps({"t": round(time.time(), 3), **obj}) + "\n")
                self._log.flush()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            t = time.time()
            try:
                ev = json.loads(raw)
            except ValueError:
                ev = {"type": "_raw", "text": raw.decode(errors="replace")[:2000]}
            self._note({"e": ev})
            self.events.put((t, ev))
        self.events.put((time.time(), None))

    def send(self, text: str) -> float:
        """Write one user message on stdin; returns the time it was written."""
        assert self.proc and self.proc.stdin
        line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
        with self._stdin_lock:
            t = time.time()
            self.proc.stdin.write(line.encode())
            self.proc.stdin.flush()
        self._note({"sent": text})
        return t

    def next_event(self, timeout: float) -> tuple[float, dict[str, Any] | None] | None:
        try:
            return self.events.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    def read_turn(self, started: float, timeout: float = 600.0, record: bool = True,
                  on_event: Callable[[Turn, dict[str, Any]], None] | None = None) -> Turn:
        """Consume events until the turn's result, the timeout, or the process exiting.
        `on_event` sees every event after the turn has observed it (the progress card's feed)."""
        turn = Turn(started=started)
        deadline = time.monotonic() + timeout
        while turn.ended is None:
            item = self.next_event(deadline - time.monotonic())
            if item is None:
                turn.outcome = "timeout"
                break
            t, ev = item
            if ev is None:
                turn.outcome = "exited"
                break
            turn.observe(t, ev)
            if on_event:
                on_event(turn, ev)
        if turn.ended is None:
            turn.ended = time.time()
        if turn.outcome == "ok" and not turn.text.strip():
            turn.outcome = "empty"      # an empty reply is a failure to retry fresh, never a silence
        if turn.outcome not in ("ok", "empty") and self.failures:
            text = " ".join([turn.text, *turn.errors, self.stderr_tail()])
            turn.failure = self.failures.classify(turn.error_type, text)
        if record:
            self.record(turn)
        return turn

    def ask(self, text: str, timeout: float = 600.0) -> Turn:
        return self.read_turn(self.send(text), timeout)

    def record(self, turn: Turn, inbound: list[str] | None = None) -> int:
        if not self.ledger:
            return 0
        f = turn.failure
        return self.ledger.record(
            room=self.spec.room, account=self.account.name, config_dir=str(self.account.config_dir),
            session_id=self.session_id, started=turn.started, first_visible=turn.first_visible,
            ended=turn.ended, outcome=turn.outcome, failure=f.cls if f else None,
            action=f.action if f else None, output_chars=len(turn.text), models=",".join(turn.models),
            fallback_model=turn.fallback_model, accepted=len(turn.accepted),
            inbound=",".join(inbound) if inbound else None)

    def stderr_tail(self, n: int = 2000) -> str:
        try:
            with open(self.stderr_path, "rb") as f:
                f.seek(max(f.seek(0, os.SEEK_END) - n, 0))
                return f.read().decode(errors="replace")
        except OSError:
            return ""

    def close(self, grace: float = 15.0) -> int | None:
        """End the process (EOF on stdin, then terminate, then kill) and free the session."""
        rc = None
        if self.proc:
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
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
        if self._log:
            self._log.close()
            self._log = None
        return rc

    def __enter__(self) -> "RoomProcess":
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None) -> None:
        self.close()
