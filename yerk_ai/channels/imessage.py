"""iMessage, through the owner's Mac: the bridge's socket (BenBridge's rpc.sock), reached by ssh
plus socat as OpenClaw's imsg-ssh does, speaking imsg's JSON-RPC, one request or notification a line.

- One connection, reopened with backoff (1 s doubling to 60 s). On each connect: `chats.list` maps
  every room's chat identifier to its chat id, then one `watch.subscribe` per chat from the highest
  row id Yerk AI has seen there (<state>/imessage.json), so what landed while the Mac was away is
  replayed; every row is deduplicated by its guid in the inbound ledger. A first start never replays.
- Identity: everything sent from the Mac carries the owner's own Apple ID, so a from-me row is Yerk
  AI's send or the owner typing on another device. Every part Yerk AI sends is keyed in the delivery
  ledger before the send and its guid recorded from the bridge's answer (or, when the answer carries
  none, the first new from-me row of that chat after the send, by row order). A from-me row whose
  guid is in the ledger is Yerk AI's own and never answered; any other from-me row is the owner. A
  from-me row that lands while a send to its chat is still out waits until that send is settled.
  Text is never compared. The loop guard holds a burst of from-me rows as a second line.
- Inbound: only the configured chats. The owner's messages run as turns; anyone else's are filed in
  the day's log and ledgered as ignored (a reply to other people is the texting layer's, with its
  preview). Attachments get a ledger row each and are listed in the turn as not fetched: the Mac's
  attachment folder needs a grant plain ssh does not hold. Tapbacks are not turns.
- Outbound: each part is on the ledger before it goes; while the bridge does not answer the parts
  wait as pending and go out in order when it answers again. No room is ever told about the outage
  (DESIGN A60): the machine room gets one line once the Mac has been away `away_after` seconds and
  one when it has been back `back_after` seconds, so a dark wake posts nothing.
- What iMessage cannot do degrades on purpose: no card, no edit, no typing signal; a turn sends its
  reply only; a worker sends one line when it starts and its relay when it ends; long replies split
  at LIMIT with the shared splitter; files are refused with a notice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, TypeVar

from .. import words
from ..ledger import Ledger
from ..lifted.loopguard import LoopGuard
from ..lifted.split import split_message
from ..room import Inbound, Room

log = logging.getLogger("yerk.imessage")
T = TypeVar("T")
LIMIT = 4000                 # a part's characters: imsg's own send takes more, a phone shows this much well
LINE = 16 * 1024 * 1024      # the longest line the bridge may send (a history with attachments)
TIMEOUT = 30.0               # seconds a request waits for its answer
SEND_TIMEOUT = 60.0          # a send waits on Messages itself
CLAIM_WINDOW = 120.0         # seconds a send with no guid in its answer claims the chat's next from-me row
GUID_KEYS = ("messageGuid", "guid", "messageId", "message_id", "id")   # where a send's answer may carry it
READS = frozenset({"chats.list", "messages.history"})                   # all a read-only connection may ask


class BridgeDown(Exception):
    """The bridge did not answer: the Mac asleep, its lid shut, its network changing."""


class BridgeError(Exception):
    """The bridge answered with a JSON-RPC error."""


def transport(conf: dict[str, Any]) -> list[str]:
    """The command whose stdin and stdout are the bridge: `command` as given, else ssh to `host` and
    socat to `socket` there (`rpc` through the socket, never a bare `imsg rpc`, as imsg-ssh does)."""
    if conf.get("command"):
        return [str(x) for x in conf["command"]]
    host, sock = conf.get("host"), conf.get("socket")
    if not host or not sock:
        raise ValueError("[imessage] in yerkd.toml needs host and socket, or command")
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(conf.get('connect_timeout', 10))}",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", str(host),
            str(conf.get("socat", "/opt/homebrew/bin/socat")), "-", f"UNIX-CONNECT:{sock}"]


def guid_of(answer: Any) -> str | int | None:
    """A send's guid from its answer; an all-digit value is a row id (int), to be looked up."""
    if not isinstance(answer, dict):
        return None
    for k in GUID_KEYS:
        v = answer.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip():
            return int(v) if v.strip().isdigit() else v.strip()
    return None


def when_of(msg: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(msg.get("created_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


class Bridge:
    """One JSON-RPC connection to the bridge: requests matched by id, notifications to `on_note`.
    `unix` opens a local socket instead of the command (the tests' fake bridge); `read_only` refuses
    every method but the reads, so a probe can never send."""

    def __init__(self, argv: list[str] | None = None, unix: str | None = None,
                 on_note: Callable[[str, dict[str, Any]], None] | None = None, read_only: bool = False,
                 timeout: float = TIMEOUT):
        self.argv, self.unix, self.on_note, self.read_only, self.timeout = argv, unix, on_note, read_only, timeout
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
                    log.warning("imessage: a line from the bridge was not JSON (%d bytes)", len(line))
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") is not None and ("result" in msg or "error" in msg):
                    fut = self.pending.pop(int(msg["id"]), None)
                    if fut is not None and not fut.done():
                        err = msg.get("error")
                        if err:
                            what = err.get("message") if isinstance(err, dict) else err
                            fut.set_exception(BridgeError(str(what)[:300]))
                        else:
                            fut.set_result(msg.get("result"))
                elif msg.get("method") and self.on_note is not None:
                    try:
                        self.on_note(str(msg["method"]), dict(msg.get("params") or {}))
                    except Exception:
                        log.exception("imessage: a notification from the bridge failed")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("imessage: the bridge connection broke: %s", type(e).__name__)
        finally:
            self._fail("the bridge connection closed")

    def _fail(self, why: str) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(BridgeDown(why))
        self.pending.clear()

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        if self.read_only and method not in READS:
            raise PermissionError(f"{method} is not a read; this connection only reads")
        if not self.up or self.writer is None:
            raise BridgeDown("not connected")
        self.n += 1
        rid, wait = self.n, timeout or self.timeout
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            self.writer.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
                               + "\n").encode())
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
            try:
                self.writer.close()
            except (OSError, RuntimeError):
                pass
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
                await self.proc.wait()
            except ProcessLookupError:
                pass
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self._fail("closed")
        self.writer = None


class IMessageChannel:
    def __init__(self, conf: dict[str, Any], ledger: Ledger, state: Path | None = None,
                 capture: Callable[..., bool] | None = None,
                 machine: Callable[[str, str], Awaitable[Any]] | None = None, guard: LoopGuard | None = None):
        self.conf, self.ledger, self.state, self.capture, self.machine = conf, ledger, state, capture, machine
        self.argv = None if conf.get("unix") else transport(conf)
        self.unix = str(conf["unix"]) if conf.get("unix") else None
        self.away_after = float(conf.get("away_after", 300))
        self.back_after = float(conf.get("back_after", 120))
        self.guard = guard or LoopGuard(max_events=int(conf.get("from_me_burst", 20)))
        self.rooms: dict[str, Room] = {}          # chat identifier -> room
        self.names: dict[str, str] = {}           # room name -> chat identifier
        self.chat_ids: dict[str, int] = {}        # chat identifier -> the bridge's chat id
        self.bridge: Bridge | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.locks: dict[str, asyncio.Lock] = {}
        self.sending: dict[str, int] = {}         # chat -> sends out and not settled
        self.deferred: list[dict[str, Any]] = []  # from-me rows that landed while a send to their chat was out
        self.unclaimed: dict[str, deque[tuple[str, int, float]]] = {}   # chat -> (key, row id before, deadline)
        self.away_since: float | None = None
        self.away_said = False
        self.back_task: asyncio.Task[None] | None = None
        self.away_task: asyncio.Task[None] | None = None
        self.held_sent = 0                        # parts that waited for the Mac and went out after
        self.connected_at = 0.0
        self.cursor: dict[str, int] = self._load()

    def add_room(self, ident: str, room: Room) -> None:
        self.rooms[ident], self.names[room.name] = room, ident

    # the row cursor: the highest row id seen per chat, so a reconnect replays only what is new
    def _load(self) -> dict[str, int]:
        try:
            got = json.loads((self.state / "imessage.json").read_text()) if self.state else {}
            return {str(k): int(v) for k, v in dict(got.get("cursor", {})).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self) -> None:
        if self.state is None:
            return
        path = self.state / "imessage.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"cursor": self.cursor}, f)
        os.replace(tmp, path)

    def _advance(self, ident: str, rowid: int) -> None:
        if rowid > self.cursor.get(ident, 0):
            self.cursor[ident] = rowid
            self._save()

    # the connection
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        delay = 1.0
        while True:
            try:
                await self._connect()
                delay = 1.0
                assert self.bridge is not None and self.bridge.task is not None
                await asyncio.shield(self.bridge.task)
            except asyncio.CancelledError:
                if self.bridge is not None:
                    await self.bridge.close()
                raise
            except (BridgeDown, BridgeError, OSError) as e:
                log.info("imessage: the bridge did not answer: %s", e)
            await self._lost()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _connect(self) -> None:
        b = Bridge(self.argv, self.unix, on_note=self._note)
        await b.open()
        self.bridge = b
        try:
            chats = (await b.request("chats.list", {"limit": int(self.conf.get("chat_limit", 200))}) or {}).get("chats", [])
            for c in chats if isinstance(chats, list) else []:
                ident = str(c.get("identifier") or "")
                if ident in self.rooms and c.get("id") is not None:
                    self.chat_ids[ident] = int(c["id"])
            for ident in self.rooms:
                cid = self.chat_ids.get(ident)
                if cid is None:
                    log.warning("imessage: room %s: its chat is not in the bridge's chat list", self.rooms[ident].name)
                    continue
                if ident not in self.cursor:      # a first start: from the newest row, never a replay (the
                    # history comes newest by date, not by row id, so the highest id of a page)
                    rows = (await b.request("messages.history", {"chat_id": cid, "limit": 20}) or {}).get("messages", [])
                    self._advance(ident, max([int(r.get("id") or 0) for r in rows] or [0]))
                params: dict[str, Any] = {"chat_id": cid, "attachments": True}
                if self.cursor.get(ident):
                    params["since_rowid"] = self.cursor[ident]
                await b.request("watch.subscribe", params)
        except BaseException:
            await b.close()
            raise
        self.connected, self.connected_at = True, time.time()
        log.info("imessage: connected, serving %d room(s)", len(self.rooms))
        await self._back()

    async def _lost(self) -> None:
        self.connected = False
        if self.bridge is not None:
            await self.bridge.close()
        if self.back_task is not None:
            self.back_task.cancel()
            self.back_task = None
        if self.away_since is None:
            self.away_since = time.time()
            if not self.away_said:
                self.away_task = asyncio.ensure_future(self._say_away())

    async def _say_away(self) -> None:
        await asyncio.sleep(self.away_after)
        if not self.connected and not self.away_said:
            self.away_said = True
            await self._tell(words.MAC_AWAY, f"imsg-away-{int(self.away_since or time.time())}")

    async def _back(self) -> None:
        """Connected: held parts go out now; the machine room hears it once the link has held."""
        if self.away_task is not None:
            self.away_task.cancel()
            self.away_task = None
        since, self.away_since = self.away_since, None
        for room in list(self.names):
            await self._flush(room)
        if self.away_said and since is not None:
            self.back_task = asyncio.ensure_future(self._say_back(since))

    async def _say_back(self, since: float) -> None:
        await asyncio.sleep(self.back_after)
        if self.connected and self.away_said:
            self.away_said = False
            n, self.held_sent = self.held_sent, 0
            await self._tell(words.MAC_BACK.format(n=n, s="" if n == 1 else "s"), f"imsg-back-{int(since)}")

    async def _tell(self, text: str, key: str) -> None:
        """One line to the machine room, never to a human one; its key keeps it to one post."""
        if self.machine is None:
            log.info("imessage: no machine room for: %s", text)
            return
        try:
            await self.machine(words.line(text), key)
        except Exception:
            log.exception("imessage: the machine room line did not go")

    # inbound
    def _note(self, method: str, params: dict[str, Any]) -> None:
        if method == "message":
            msg = params.get("message", params)
            if isinstance(msg, dict):
                self.take(msg)
        elif method == "error":
            log.warning("imessage: the bridge's watch reported an error")

    def ident_of(self, msg: dict[str, Any]) -> str | None:
        ident = str(msg.get("chat_identifier") or "")
        if ident in self.rooms:
            return ident
        cid = msg.get("chat_id")
        return next((i for i, c in self.chat_ids.items() if cid is not None and c == int(cid)), None)

    def mine(self, guid: str) -> bool:
        return self.ledger.delivery_by_message(guid) is not None

    def take(self, msg: dict[str, Any]) -> str:
        """One row from the bridge: what it was taken as (for the tests and the log)."""
        guid, ident = str(msg.get("guid") or ""), self.ident_of(msg)
        if not guid or ident is None:
            return "not ours"
        room, rowid, from_me = self.rooms[ident], int(msg.get("id") or 0), msg.get("is_from_me") is True
        self._advance(ident, rowid)
        if msg.get("is_reaction") or msg.get("is_tapback"):
            return "tapback"
        if self.mine(guid):
            return "own"                          # Yerk AI's own send: never answered (filed when it went)
        if from_me and self.sending.get(ident):
            self.deferred.append(msg)             # a send to this chat is out: wait for its guid first
            return "deferred"
        if from_me and self._claim_row(ident, guid, rowid):
            return "own"
        text = str(msg.get("text") or "")
        sender = "owner" if from_me else "other"
        if not self.ledger.inbound_add(guid, room.name, f"imessage:{ident}", sender, len(text),
                                       created=(w.timestamp() if (w := when_of(msg)) else None)):
            return "seen"                         # deduplicated by guid: a replay or a second notification
        atts = [a for a in msg.get("attachments") or [] if isinstance(a, dict)]
        self._file(room.name, guid, sender, text, [(self._name(a), str(a.get("mime_type") or ""),
                                                    int(a.get("total_bytes") or 0)) for a in atts], when_of(msg))
        if sender != "owner":
            self.ledger.inbound_set(guid, state="ignored", note="not the owner")
            return "other"
        if not self.guard.admit(ident)[0]:
            self.ledger.inbound_set(guid, state="ignored", note="loop guard: a burst of from-me rows")
            log.warning("room %s: loop guard holding from-me rows", room.name)
            return "held"
        if not (text.strip() or atts):
            self.ledger.inbound_set(guid, state="ignored", note="no text")
            return "empty"
        files = self.stage(room, guid, atts)
        room.submit(Inbound(guid, "owner", body(text, files), files=files))
        return "owner"

    def _claim_row(self, ident: str, guid: str, rowid: int) -> bool:
        """A send whose answer had no guid takes the chat's first new from-me row (by row order)."""
        q, now = self.unclaimed.get(ident), time.time()
        while q and q[0][2] < now:
            q.popleft()
        if not q or rowid <= q[0][1]:
            return False
        key, _, _ = q.popleft()
        self.ledger.delivery_set(key, state="sent", message_id=guid, sent=time.time(),
                                 note="guid from the chat's next from-me row")
        log.info("imessage: %s claimed its guid from the watch", key)
        return True

    @staticmethod
    def _name(a: dict[str, Any]) -> str:
        return Path(str(a.get("transfer_name") or a.get("filename") or "file")).name

    def stage(self, room: Room, guid: str, atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each attachment ledgered as Discord's are; not fetched yet (the Mac's attachment folder needs
        a grant ssh does not hold), so the turn is told it is missing."""
        files = []
        for a in atts:
            f = {"name": self._name(a), "size": int(a.get("total_bytes") or 0),
                 "type": str(a.get("mime_type") or "application/octet-stream"), "ok": False,
                 "error": words.NOT_FETCHED_MAC}
            self.ledger.attachment(message_id=guid, room=room.name, name=f["name"], size=f["size"], type=f["type"],
                                   state="missing", error=f["error"])
            files.append(f)
        return files

    def _file(self, room: str, mid: str, role: str, text: str, files: list[tuple[str, str, int]] | None = None,
              when: Any = None) -> None:
        if self.capture is None:
            return
        try:
            self.capture(room, mid, role, text, files or [], when=when)
        except OSError as e:
            log.warning("room %s: message %s not filed in the day's log: %s", room, mid, e)

    # outbound: called from room threads
    def _call(self, coro: Coroutine[Any, Any, T]) -> T | None:
        assert self.loop
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(180)

    def open_card(self, room: str, text: str, kind: str = "turn") -> None:
        return None                               # no cards on iMessage: the reply is what shows

    def progress(self, room: str, text: str) -> None:
        return None                               # no edits on iMessage

    def reply(self, room: str, text: str, inbound: list[str], card: str | None = None,
              key: str | None = None) -> list[str]:
        """The turn's reply, in parts; a card line has nowhere to go and is dropped."""
        return self._call(self._post(room, text, "reply", inbound, key)) or []

    def notice(self, room: str, text: str, inbound: list[str], card: str | None = None,
               key: str | None = None) -> list[str]:
        return self._call(self._post(room, text, "notice", inbound, key)) or []

    # outbound: on the loop
    def _lock(self, room: str) -> asyncio.Lock:
        return self.locks.setdefault(room, asyncio.Lock())

    async def _post(self, room: str, text: str, kind: str, inbound: list[str], key: str | None = None) -> list[str]:
        """Every part on the ledger first, then everything pending for the room in order; the ids of
        this key's parts that went (none while the Mac is away: they wait)."""
        key = key or f"{kind[0]}{uuid.uuid4().hex[:12]}"
        for i, part in enumerate(split_message(text.strip() or "(empty)", LIMIT)):
            self.ledger.delivery_plan(f"{key}:{i}", room, kind, part, ",".join(inbound))
        await self._flush(room)
        rows = sorted((r for r in self.ledger.deliveries(room) if r["key"].rsplit(":", 1)[0] == key),
                      key=lambda r: int(r["key"].rsplit(":", 1)[1]))
        return [str(r["message_id"] or r["key"]) for r in rows if r["state"] == "sent"]

    async def _flush(self, room: str) -> None:
        """Pending parts of the room, oldest first, while the bridge answers."""
        async with self._lock(room):
            rows = sorted(self.ledger.deliveries(room, "pending"),
                          key=lambda r: (r["created"], r["key"].rsplit(":", 1)[0],
                                         int(r["key"].rsplit(":", 1)[1]) if r["key"].rsplit(":", 1)[1].isdigit() else 0))
            for r in rows:
                if not self.connected or self.bridge is None or not self.bridge.up:
                    return                        # held: they go when the bridge answers again
                try:
                    await self._send(room, r)
                except BridgeDown as e:
                    log.info("imessage: room %s: parts held, the bridge did not answer: %s", room, e)
                    if self.bridge is not None:
                        await self.bridge.close()  # the read loop ends, start() reconnects
                    return

    async def _send(self, room: str, r: dict[str, Any]) -> None:
        assert self.bridge is not None
        ident, body_text, key = self.names[room], str(r["body"] or ""), str(r["key"])
        cid = self.chat_ids.get(ident)
        params: dict[str, Any] = {"text": body_text, "service": str(self.conf.get("service", "auto")),
                                  **({"chat_id": cid} if cid is not None else {"chat_identifier": ident})}
        if self.conf.get("send_transport"):       # imsg's own choice of how Messages sends (applescript, bridge)
            params["transport"] = str(self.conf["send_transport"])
        before, attempts = self.cursor.get(ident, 0), int(r["attempts"] or 0) + 1
        held = float(r["created"] or 0) < self.connected_at      # planned before this connect: it waited
        self.sending[ident] = self.sending.get(ident, 0) + 1
        try:
            last = re.search(r"after row (\d+)", str(r["note"] or ""))
            if last:                              # an earlier try got no answer: it may have gone out
                prior = await self._guid_from_history(ident, cid, int(last.group(1)))
                if prior:
                    self.ledger.delivery_set(key, state="sent", message_id=prior, sent=time.time(),
                                             note="it had gone out before the bridge dropped")
                    self.ledger.outbound(room=room, channel=f"imessage:{ident}", kind=r["kind"], message_id=prior,
                                         chars=len(body_text), inbound=r["inbound"])
                    self._file(room, prior, "yerk", body_text)
                    return
            try:
                answer = await self.bridge.request("send", params, timeout=float(self.conf.get("send_timeout", SEND_TIMEOUT)))
            except BridgeError as e:
                self.ledger.delivery_set(key, state="failed", error=str(e)[:300], attempts=attempts)
                self.ledger.outbound(room=room, channel=f"imessage:{ident}", kind=r["kind"], chars=len(body_text),
                                     error=str(e)[:300], inbound=r["inbound"])
                log.warning("imessage: room %s: the bridge refused %s: %s", room, key, e)
                return
            except BridgeDown:                    # it may have gone out: its row, if it comes, is this part's
                self.ledger.delivery_set(key, attempts=attempts, note=f"no answer after row {before}; held")
                self.unclaimed.setdefault(ident, deque()).append((key, before, time.time() + CLAIM_WINDOW))
                raise
            got = guid_of(answer)
            if isinstance(got, int):              # a row id: its guid from the chat's history
                got = await self._guid_for_row(cid, got)
            note = None
            if got is None:
                got = await self._guid_from_history(ident, cid, before)
                note = "guid from the chat's history" if got else None
            self.ledger.delivery_set(key, state="sent", message_id=got, sent=time.time(), attempts=attempts,
                                     note=note or ("sent after the Mac came back" if held else None))
            if got is None:                       # the next from-me row of this chat is this part's
                self.unclaimed.setdefault(ident, deque()).append((key, before, time.time() + CLAIM_WINDOW))
            if held:
                self.held_sent += 1
            self.ledger.outbound(room=room, channel=f"imessage:{ident}", kind=r["kind"], message_id=got or None,
                                 chars=len(body_text), inbound=r["inbound"])
            self._file(room, str(got or key), "yerk", body_text)
        finally:
            self.sending[ident] -= 1
            if not self.sending[ident]:
                waiting, self.deferred = [m for m in self.deferred if self.ident_of(m) == ident], \
                    [m for m in self.deferred if self.ident_of(m) != ident]
                for m in waiting:
                    self.take(m)

    async def _guid_for_row(self, cid: int | None, rowid: int) -> str | None:
        if cid is None or self.bridge is None:
            return None
        rows = (await self.bridge.request("messages.history", {"chat_id": cid, "limit": 10}) or {}).get("messages", [])
        return next((str(m["guid"]) for m in rows if int(m.get("id") or 0) == rowid and m.get("guid")), None)

    async def _guid_from_history(self, ident: str, cid: int | None, before: int) -> str | None:
        """The answer had no guid: the chat's first from-me row past `before` that is no one's yet,
        taken from a row that already waited, else from the history (a few looks, a second apart)."""
        waiting = sorted((m for m in self.deferred if self.ident_of(m) == ident and m.get("is_from_me") is True
                          and int(m.get("id") or 0) > before and not self.mine(str(m.get("guid") or ""))),
                         key=lambda m: int(m.get("id") or 0))
        if waiting:
            self.deferred.remove(waiting[0])
            return str(waiting[0]["guid"])
        for _ in range(int(self.conf.get("claim_looks", 5))):
            if cid is None or self.bridge is None or not self.bridge.up:
                return None
            rows = (await self.bridge.request("messages.history", {"chat_id": cid, "limit": 10}) or {}).get("messages", [])
            new = sorted((m for m in rows if m.get("is_from_me") is True and int(m.get("id") or 0) > before
                          and m.get("guid") and not self.mine(str(m["guid"]))
                          and self.ledger.inbound_get(str(m["guid"])) is None), key=lambda m: int(m.get("id") or 0))
            if new:
                return str(new[0]["guid"])
            await asyncio.sleep(float(self.conf.get("claim_every", 1.0)))
        return None

    async def send_text(self, room: str, text: str, key: str | None = None) -> list[str]:
        return await self._post(room, text, "send", [], key)

    async def notify(self, room: str, text: str) -> list[str]:
        return await self._post(room, text, "notice", [])

    async def send_file(self, room: str, path: Path, caption: str = "", key: str | None = None,
                        inbound: str = "") -> list[str]:
        why = words.file_why("imessage")
        await self._post(room, f"{words.NOT_DONE} {words.NOT_SENT.format(name=path.name, why=why)}", "notice", [],
                         key=f"{key or 'f' + uuid.uuid4().hex[:12]}-notice")
        raise ValueError(f"{path.name} did not go out: {why}; the room was told")

    async def worker_card(self, room: str, key: str, text: str, message_id: str | None = None) -> str | None:
        """A worker gets one line when it starts; its later card texts have nowhere to go."""
        if message_id:
            return message_id
        first = next((x for x in text.strip().split("\n") if x.strip()), words.START)
        ids = await self._post(room, first, "worker-card", [], key=f"{key}-start")
        return ids[0] if ids else f"imessage-{key}"

    async def worker_settle(self, room: str, key: str, text: str, how: str,
                            message_id: str | None = None) -> str | None:
        return message_id or f"imessage-{key}"    # the relay is the worker's last word, as a turn's reply


def body(text: str, files: list[dict[str, Any]]) -> str:
    """The message, with its files listed and why each is missing, so none is dropped unseen."""
    lines = [text] + [f"[attachment] {f['name']}: could not be fetched ({f.get('error', 'unknown')})" for f in files]
    return "\n".join(x for x in lines if x).strip()


async def probe(rooms: dict[str, str], conf: dict[str, Any], ledger: Ledger, limit: int = 20,
                grace: float = 120.0) -> dict[str, Any]:
    """`yerk probe imessage`: never sends. For each room's chat, the bridge's newest `limit` rows (a
    read-only connection: any other method is refused), each looked up in the inbound ledger and
    the delivery ledger; a row neither holds, older than `grace` seconds, is a lost text."""
    b = Bridge(None if conf.get("unix") else transport(conf), str(conf["unix"]) if conf.get("unix") else None,
               read_only=True)
    out: dict[str, Any] = {"ok": False, "rooms": {}, "gaps": []}
    try:
        await b.open()
        chats = (await b.request("chats.list", {"limit": int(conf.get("chat_limit", 200))}) or {}).get("chats", [])
        ids = {str(c.get("identifier") or ""): int(c["id"]) for c in chats if c.get("id") is not None}
        now = time.time()
        for room, ident in rooms.items():
            cid = ids.get(ident)
            if cid is None:
                out["rooms"][room] = {"read": 0, "missing": 0, "chat": "not in the chat list"}
                continue
            rows = (await b.request("messages.history", {"chat_id": cid, "limit": limit}) or {}).get("messages", [])
            miss = []
            for m in rows:
                guid, w = str(m.get("guid") or ""), when_of(m)
                if not guid or m.get("is_reaction") or (w is not None and now - w.timestamp() < grace):
                    continue
                if ledger.inbound_get(guid) is None and ledger.delivery_by_message(guid) is None:
                    miss.append(guid)
            out["rooms"][room] = {"read": len(rows), "missing": len(miss)}
            out["gaps"] += [{"room": room, "guid": g} for g in miss]
        out["ok"] = not out["gaps"]
    except (BridgeDown, BridgeError, OSError) as e:
        out["error"] = f"the bridge did not answer ({type(e).__name__})"
    finally:
        await b.close()
    return out
