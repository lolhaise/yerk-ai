"""The link: one read-only connection to the bridge app on the Mac, watching the 1:1 chats of the people on the reply list (iMessage and SMS
alike), each message handed to the intake and the chat's cursor saved after it, so a reconnect or a restart replays only what is new and a
first start never replays. Adapted from an earlier iMessage channel of this project, which is not in the repository: the
backoff runs 1 s doubling to 60 s and starts over only after a link that held `HELD`; the machine room hears one line
once the bridge has been away `away`, and one once it has been back `back`, so a short dark wake says nothing."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from .. import words
from ..config import write_private
from .bridge import Bridge, BridgeDown, BridgeError
from .people import People, Person, norm

log = logging.getLogger("yerk.texting")
HELD = 60.0                  # seconds the link holds before the backoff starts over at 1 s
CHATS = 1000                 # chats.list's limit: a listed person's chat past it is not watched until it is in it
REREAD = 60.0                # seconds between reads of a cursor file that could not be read, the link held meanwhile


def refusal(chat: dict[str, Any], p: Person) -> str | None:
    """Why a listed person's chat is not watched, or None: a group, or anyone in it but them (and the Mac's own login)."""
    if chat.get("is_group"):
        return "a group"
    ok = set(p.handles) | ({norm(chat["account_login"])} if chat.get("account_login") else set())
    who = chat.get("participants")
    if not isinstance(who, list) or any(not isinstance(x, str) or norm(x) not in ok for x in who):
        return "someone else is in it"
    return None


class Link:
    def __init__(self, people: People, state: Path, take: Callable[[Person, dict[str, Any]], str],
                 machine: Callable[[str], Any] | None = None, argv: list[str] | None = None, unix: str | None = None,
                 mode: str = "dry"):
        self.people, self.take_row, self.machine, self.argv, self.unix, self.mode = people, take, machine, argv, unix, mode
        self.folder = state / "texting"
        self.away, self.back = 300.0, 120.0
        self.chats: dict[int, Person] = {}        # the bridge's chat id -> whose 1:1 chat it is
        self.bridge: Bridge | None = None
        self.connected, self.connected_at, self.since, self.last_in = False, 0.0, time.time(), 0.0
        self.away_since: float | None = None
        self.away_said = False
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.out: dict[str, Any] = {}               # the sender's: the last text out, how many held, how many unseen
        self.reread, self.held_said = REREAD, False
        self.held = self._read()

    def _read(self) -> str:
        """The saved rows into `cursor`; "" when read, or when there is no file yet (a first start). Else why not: a file
        there that cannot be read holds the link (start), never a first start, nothing saved, the file left as it is."""
        self.cursor: dict[str, int] = {}
        try:
            got = json.loads((self.folder / "cursor.json").read_text())
            self.cursor = {str(k): int(v) for k, v in dict(got.get("cursor", {})).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError) as e:
            return f"{self.folder / 'cursor.json'} cannot be read ({type(e).__name__})"
        return ""

    def _save(self) -> None:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(self.folder / "cursor.json", json.dumps({"cursor": self.cursor}))

    def status(self) -> None:
        """<state>/texting/status.json, what `yerk status` shows: the mode, the bridge up or down since when, the last text in."""
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(self.folder / "status.json", json.dumps({"mode": self.mode, "up": self.connected, "since": self.since,
                                                               "last_in": self.last_in, "watched": len(self.chats), **self.out,
                                                               **({"held": self.held} if self.held else {})}))

    def more(self, fields: dict[str, Any]) -> None:
        self.out.update(fields)
        self.status()

    async def start(self) -> None:
        delay = 1.0
        self.status()
        while self.held:                             # said once in the machine room; it connects once the file reads or is gone
            if not self.held_said:
                log.warning("texting: not connected: %s", self.held)
                if self.machine is not None:
                    self.machine(words.TEXTING_HELD.format(why=self.held))
            self.held_said = True
            await asyncio.sleep(self.reread)
            self.held = self._read()
            self.status()
        while True:
            try:
                await self._connect()
                assert self.bridge is not None and self.bridge.task is not None
                await asyncio.wait([self.bridge.task])
                if time.time() - self.connected_at >= HELD:
                    delay = 1.0
            except asyncio.CancelledError:
                if self.bridge is not None:
                    await self.bridge.close()
                raise
            except (BridgeDown, BridgeError, OSError) as e:
                log.info("texting: the bridge did not answer: %s", e)
            await self._lost()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _connect(self) -> None:
        b = Bridge(self.argv, self.unix, on_note=self._note)
        await b.open()
        self.bridge = b
        try:
            got = (await b.request("chats.list", {"limit": CHATS}) or {}).get("chats", [])
            self.chats = {}
            for c in got if isinstance(got, list) else []:
                if (p := self.people.by_handle(c.get("identifier"))) is None or c.get("id") is None:
                    continue
                if (why := refusal(c, p)) is not None:
                    log.warning("texting: a chat of %s is not watched: %s", p.key, why)
                    continue
                self.chats[int(c["id"])] = p
            for cid in self.chats:
                if str(cid) not in self.cursor:        # a first start: from the newest row or tapback on one, never a replay
                    rows = (await b.request("messages.history", {"chat_id": cid, "limit": 20}) or {}).get("messages", [])
                    self.cursor[str(cid)] = max([int(x.get("id") or 0) for r in rows for x in (r, *(r.get("reactions") or []))] or [0])
                    self._save()
                await b.request("watch.subscribe", {"chat_id": cid, "attachments": True, "include_reactions": True,    # none unasked (J97)
                                                    "since_rowid": self.cursor[str(cid)]})
        except BaseException:
            await b.close()
            raise
        self.connected, self.connected_at, self.since = True, time.time(), time.time()
        log.info("texting: connected, watching %d chat(s)", len(self.chats))
        self._cancel("away")
        since, self.away_since = self.away_since, None
        if self.away_said and since is not None:
            self.tasks["back"] = asyncio.ensure_future(self._say(self.back, True, words.TEXTING_BACK))
        self.status()

    async def _lost(self) -> None:
        if self.connected:
            self.since = time.time()
        self.connected = False
        if self.bridge is not None:
            await self.bridge.close()
        self._cancel("back")
        if self.away_since is None:
            self.away_since = time.time()
            if not self.away_said:
                self.tasks["away"] = asyncio.ensure_future(self._say(self.away, False, words.TEXTING_AWAY))
        self.status()

    def _cancel(self, name: str) -> None:
        if (t := self.tasks.pop(name, None)) is not None:
            t.cancel()

    async def _say(self, after: float, up: bool, text: str) -> None:
        """One line to the machine room once the link has stayed down (or back up) `after` seconds."""
        await asyncio.sleep(after)
        if self.connected == up and self.away_said == up:
            self.away_said = not up
            if self.machine is not None:
                self.machine(text)

    def _note(self, method: str, params: dict[str, Any]) -> None:
        if method == "message" and isinstance(msg := params.get("message", params), dict):
            self.take(msg)
        elif method == "error":
            log.warning("texting: the bridge's watch reported an error")

    async def thread(self, key: str, limit: int) -> list[dict[str, Any]]:
        """The newest `limit` rows of each of a person's chats (messages.history, a read), each with its files (the bridge
        leaves them out unless asked), for the writer to merge; BridgeDown while the link is away."""
        if self.bridge is None or not self.connected:
            raise BridgeDown("the bridge is away")
        return [r for cid, p in list(self.chats.items()) if p.key == key for r in (await self.bridge.request(
                "messages.history", {"chat_id": cid, "limit": limit, "attachments": True}) or {}).get("messages", [])]

    def take(self, msg: dict[str, Any]) -> str:
        """One row from the watch: a listed person's 1:1 chat only, by its chat id and, when the row names it, its handle."""
        cid = msg.get("chat_id")
        p = self.chats.get(int(str(cid))) if str(cid).isdigit() else None
        if p is None or (msg.get("chat_identifier") and self.people.by_handle(msg["chat_identifier"]) is not p):
            return "not ours"
        got = self.take_row(p, msg)
        if got == "them":
            self.last_in = time.time()
        if (rowid := int(msg.get("id") or 0)) > self.cursor.get(str(cid), 0):
            self.cursor[str(cid)] = rowid
            self._save()
        if got == "them":
            self.status()
        return got
