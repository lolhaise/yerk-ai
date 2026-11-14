"""One long-lived, read-only connection to the bridge app on your Mac, through which texting reads your Messages threads (imsg's JSON-RPC, one request
or notification a line). It is adapted from an earlier iMessage channel of this project, which is not in the repository. Read-only, with no switch to make it
method but the three reads is refused here before a byte is written, so it can never send, react or change anything on the Mac."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger("yerk.texting")
LINE = 16 * 1024 * 1024      # the longest line the bridge may send (a history with attachments)
TIMEOUT = 30.0               # seconds a request waits for its answer
READS = frozenset({"chats.list", "messages.history", "watch.subscribe"})   # all a read-only connection may ask


class BridgeDown(Exception):
    """The bridge did not answer: the Mac asleep, its lid shut, its network changing."""


class BridgeError(Exception):
    """The bridge answered with a JSON-RPC error."""


def error_class(err: Any) -> str:
    """The bridge's error by its JSON-RPC code, never its text: imsg's message can name a handle."""
    code = err.get("code") if isinstance(err, dict) else None
    return f"bridge error {code}" if isinstance(code, int) and not isinstance(code, bool) else "bridge error"


def when_of(msg: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(msg.get("created_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def is_tapback(msg: dict[str, Any]) -> bool:
    return bool(msg.get("is_reaction") or msg.get("is_tapback"))


def attached(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """A row's files, never a link preview's payload: not a file anyone sent (an import leaves it out too, sources.py)."""
    return [a for a in msg.get("attachments") or [] if isinstance(a, dict)
            and not str(a.get("transfer_name") or a.get("filename") or "").endswith(".pluginPayloadAttachment")]


class Bridge:
    """Requests matched by id, notifications to `on_note`. `unix` opens a local socket instead of the command (the tests'
    stub bridge)."""

    def __init__(self, argv: list[str] | None = None, unix: str | None = None,
                 on_note: Callable[[str, dict[str, Any]], None] | None = None,
                 timeout: float = TIMEOUT):
        self.argv, self.unix, self.on_note, self.timeout = argv, unix, on_note, timeout
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.n = 0
        self.task: asyncio.Task[None] | None = None

    @property
    def up(self) -> bool:
        return self.writer is not None and self.task is not None and not self.task.done()

    async def open(self) -> None:
        try:
            if self.unix:
                self.reader, self.writer = await asyncio.open_unix_connection(self.unix, limit=LINE)
            else:
                assert self.argv
                self.proc = await asyncio.create_subprocess_exec(
                    *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=LINE)
                self.reader, self.writer = self.proc.stdout, self.proc.stdin
        except OSError as e:
            raise BridgeDown(f"could not open the transport ({type(e).__name__})") from e
        self.task = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        assert self.reader is not None
        try:
            while line := await self.reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("texting: a line from the bridge was not JSON (%d bytes)", len(line))
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") is not None and ("result" in msg or "error" in msg):
                    fut = self.pending.pop(int(msg["id"]), None)
                    if fut is not None and not fut.done():
                        if (err := msg.get("error")):
                            fut.set_exception(BridgeError(error_class(err)))
                        else:
                            fut.set_result(msg.get("result"))
                elif msg.get("method") and self.on_note is not None:
                    try:
                        self.on_note(str(msg["method"]), dict(msg.get("params") or {}))
                    except Exception:
                        log.exception("texting: a notification from the bridge failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("texting: the bridge connection broke: %s", type(e).__name__)
        finally:
            self._fail("the bridge connection closed")

    def _fail(self, why: str) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(BridgeDown(why))
        self.pending.clear()

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        if method not in READS:
            raise PermissionError(f"{method} is not a read; this connection only reads")
        if not self.up or self.writer is None:
            raise BridgeDown("not connected")
        self.n += 1
        rid, wait = self.n, timeout or self.timeout
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            self.writer.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n").encode())
            await self.writer.drain()
            return await asyncio.wait_for(fut, wait)
        except asyncio.TimeoutError as e:
            raise BridgeDown(f"{method}: no answer in {wait:.0f} s") from e
        except (OSError, RuntimeError) as e:
            raise BridgeDown(f"{method}: {type(e).__name__}") from e
        finally:
            self.pending.pop(rid, None)

    async def close(self) -> None:
        if self.writer is not None:
            with contextlib.suppress(OSError, RuntimeError):
                self.writer.close()
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
                await self.proc.wait()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self._fail("closed")
        self.writer = None
