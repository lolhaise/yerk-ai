"""Yerk AI in Discord, through the discord.py library: one connection that reads only the channels rooms.toml names and posts their cards and replies.

- Every other channel is ignored before anything is read: no reply, no reaction, no history.
- Only the owner, and in test mode the listed webhooks, are answered. Webhook senders pass a
  loop guard (lifted/loopguard.py) so a runaway script cannot run the room.
- A message is ledgered by its Discord id before anything runs, then acked with a reaction. Its
  files are staged in the room's inbox under their own names, a ledger row each; a file that
  cannot be fetched gets a visible "could not fetch NAME" and the turn still runs.
- A turn owns one card: posted when the turn starts, edited from the stream (the first edit at
  once, then at most one a second, the latest text winning), replaced by the reply, or kept and
  edited to say what was owed when the turn ends with nothing to show.
- Every outbound part has an idempotency key (the turn's key and the part's index) and its row in
  the delivery ledger before it is posted. On start, parts a crash left unconfirmed are posted
  once, labelled, unless the room already shows them; then messages a crash left open run again
  once, marked, and messages that arrived while the daemon was down are read from the room's own
  history and run.
- Replies: the final text only, split under 2,000 characters, never inside a code block.
- Files: one path (`send_file`): a snapshot under a new name in the room's outbox, size and type
  checked against the room's limits, the message id recorded, a visible notice on any miss.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Coroutine, TypeVar

import discord

from ..ledger import Ledger
from ..lifted.loopguard import LoopGuard
from ..lifted.split import split_message
from ..room import Inbound, Room

log = logging.getLogger("yerk.discord")
T = TypeVar("T")
LIMIT = 1950          # Discord's 2,000 less room for the redelivery label
MAX = 2000
ACK, DONE, FAILED = "\N{EYES}", "\N{WHITE HEAVY CHECK MARK}", "\N{WARNING SIGN}"
REDELIVERED = "(redelivered after a restart)"
MIN_EDIT = 1.0        # seconds between card edits after the first
FORBIDDEN = {".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".dll", ".ps1", ".vbs", ".jar", ".apk", ".lnk",
             ".reg", ".hta", ".cpl", ".pif"}
IMAGE = ("image/png", "image/jpeg", "image/gif", "image/webp")


def safe_name(name: str) -> str:
    """A file name that stays one path segment."""
    name = re.sub(r"[^\w.\- ]+", "_", Path(name).name).strip(" .") or "file"
    return name[:120]


def file_type(name: str, given: str | None = None) -> str:
    return (given or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0]


def message_text(m: Any, files: list[dict[str, Any]] | None = None) -> str:
    """The message body, with its files listed (staged path, or why it is missing) so none is
    dropped unseen. Without `files`, the attachments as Discord lists them."""
    lines = [m.content or ""]
    if files is None:
        files = [{"name": a.filename, "size": a.size, "type": file_type(a.filename, getattr(a, "content_type", None)),
                  "ok": False, "error": "not staged"} for a in getattr(m, "attachments", []) or []]
    for f in files:
        if f.get("ok"):
            lines.append(f"[attachment] {f['name']}, {f['type']}, {f['size']:,} bytes, saved at {f['path']}")
        else:
            lines.append(f"[attachment] {f['name']}: could not be fetched ({f.get('error', 'unknown')})")
    return "\n".join(x for x in lines if x).strip()


class DiscordChannel:
    def __init__(self, token: str, owner: int, test_senders: list[int], ledger: Ledger,
                 client: Any = None, guard: LoopGuard | None = None, state: Path | None = None):
        self.token, self.owner, self.test_senders, self.ledger = token, owner, set(test_senders), ledger
        self.state = state
        self.rooms: dict[int, Room] = {}          # channel id -> room
        self.names: dict[str, int] = {}           # room name -> channel id
        self.cards: dict[str, Any] = {}           # room name -> its open card
        self.want: dict[str, str] = {}            # room name -> the card text waiting to be shown
        self.shown: dict[str, str] = {}
        self.edited: dict[str, float] = {}        # room name -> when its card was last edited
        self.locks: dict[str, asyncio.Lock] = {}
        self.guard = guard or LoopGuard()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.recovered = False
        if client is None:
            intents = discord.Intents.none()
            intents.guilds = intents.guild_messages = intents.message_content = True
            client = discord.Client(intents=intents, max_messages=None, chunk_guilds_at_startup=False,
                                    member_cache_flags=discord.MemberCacheFlags.none())
        self.client = client
        client.event(self.on_ready)
        client.event(self.on_message)

    def add_room(self, channel_id: int, room: Room) -> None:
        self.rooms[channel_id], self.names[room.name] = room, channel_id

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.client.start(self.token)

    # inbound
    def sender_of(self, m: Any) -> str | None:
        if getattr(m, "webhook_id", None):
            return "test-webhook" if m.webhook_id in self.test_senders else None
        if self.client.user is not None and m.author.id == self.client.user.id:
            return None
        return "owner" if m.author.id == self.owner else None

    async def on_message(self, m: Any) -> None:
        room = self.rooms.get(m.channel.id)
        if room is None:
            return
        await self.take(room, m)

    async def take(self, room: Room, m: Any) -> None:
        sender = self.sender_of(m)
        if sender is None:
            return
        if sender != "owner" and not self.guard.admit(m.channel.id)[0]:
            log.warning("room %s: loop guard holding webhook messages", room.name)
            return
        if not (m.content or getattr(m, "attachments", None)):
            return
        if not self.ledger.inbound_add(str(m.id), room.name, str(m.channel.id), sender, len(m.content or "")):
            return
        await self._react(m.channel.id, m.id, ACK)
        files = await self.stage(room, m)
        room.submit(Inbound(str(m.id), sender, message_text(m, files), files=files))

    async def stage(self, room: Room, m: Any) -> list[dict[str, Any]]:
        """Each attachment saved under the room's inbox with its own name, a ledger row each; a
        visible notice for any that cannot be fetched."""
        files = []
        for a in getattr(m, "attachments", []) or []:
            f = {"name": a.filename, "size": int(a.size or 0), "type": file_type(a.filename, a.content_type)}
            try:
                dest = room.inbox(str(m.id)) / safe_name(a.filename)
                if not dest.is_file() or dest.stat().st_size != f["size"]:
                    await a.save(dest)
                f.update(ok=True, path=str(dest), size=dest.stat().st_size)
                self.ledger.attachment(message_id=str(m.id), room=room.name, name=a.filename, size=f["size"],
                                       type=f["type"], path=str(dest), state="staged")
            except (discord.HTTPException, OSError) as e:
                f.update(ok=False, error=f"{type(e).__name__}: {str(e)[:120]}")
                self.ledger.attachment(message_id=str(m.id), room=room.name, name=a.filename, size=f["size"],
                                       type=f["type"], state="missing", error=f["error"])
                await self._post(room.name, f"could not fetch {a.filename}", "notice", [str(m.id)])
            files.append(f)
        return files

    async def on_ready(self) -> None:
        self.loop = asyncio.get_running_loop()
        log.info("discord: connected as %s, serving %d room(s)", self.client.user, len(self.rooms))
        if not self.recovered:              # once per process: a reconnect must not re-run live turns
            self.recovered = True
            for cid, room in self.rooms.items():
                await self.recover(cid, room)

    async def recover(self, cid: int, room: Room) -> None:
        ch = self.client.get_channel(cid) or await self.client.fetch_channel(cid)
        for c in self.ledger.cards_open(room.name):        # their turns ended with the old process
            try:
                await ch.get_partial_message(int(c["message_id"])).delete()
            except discord.HTTPException:
                pass
            self.ledger.card_close(c["message_id"], "removed after a restart")
        await self.redeliver(ch, room)
        for row in self.ledger.inbound_open(room.name):
            attempts = int(row["attempts"] or 0)
            if attempts >= 2:
                self.ledger.inbound_set(row["message_id"], state="abandoned", note="redelivered once already")
                await self._post(room.name, "A message from before a restart was not answered and was not run again.",
                                 "notice", [row["message_id"]])
                continue
            try:
                m = await ch.fetch_message(int(row["message_id"]))
            except discord.NotFound:
                self.ledger.inbound_set(row["message_id"], state="abandoned", note="message deleted")
                continue
            files = await self.stage(room, m)
            text = message_text(m, files) + ("\n[redelivered after a restart]" if attempts else "")
            room.submit(Inbound(str(m.id), str(row["sender"]), text, attempt=attempts, files=files))
        last = self.ledger.last_inbound(str(cid))
        if last:
            async for m in ch.history(after=discord.Object(int(last)), limit=50, oldest_first=True):
                await self.take(room, m)

    async def redeliver(self, ch: Any, room: Room) -> None:
        """Parts a crash left unconfirmed: posted once, labelled, unless the room already shows
        them (posted before the crash, never confirmed); their messages are then answered."""
        pending = self.ledger.deliveries(room.name, "pending")
        keys = list(dict.fromkeys(r["key"].rsplit(":", 1)[0] for r in pending))
        if not keys:
            return
        mine: dict[str, str] = {}
        oldest = min(r["created"] for r in pending) - 5
        after = discord.Object(int((oldest * 1000 - 1420070400000)) << 22)
        async for m in ch.history(after=after, limit=100, oldest_first=True):
            if self.client.user is not None and m.author.id == self.client.user.id:
                mine.setdefault((m.content or "").strip(), str(m.id))
        for key in keys:
            rows = sorted((r for r in self.ledger.deliveries(room.name) if r["key"].rsplit(":", 1)[0] == key),
                          key=lambda r: int(r["key"].rsplit(":", 1)[1]) if r["key"].rsplit(":", 1)[1].isdigit() else 0)
            if rows[0]["kind"] == "file":
                for r in rows:
                    self.ledger.delivery_set(r["key"], state="failed", error="not confirmed before a restart")
                await self._post(room.name, f"A file ({rows[0]['file']}) was not confirmed sent before a restart; "
                                 "ask again if it is not above.", "notice", [])
                continue
            for r in rows:
                found = mine.get((r["body"] or "").strip())
                if r["state"] == "pending" and found:
                    self.ledger.delivery_set(r["key"], state="sent", message_id=found, sent=time.time(),
                                             note="found in the room after a restart")
            async with self._lock(room.name):
                ids = await self._deliver(room.name, key, label=True)
            inbound = [x for x in str(rows[0]["inbound"] or "").split(",") if x]
            turn_id = self.ledger.turn_for_key(key.split("-")[0])
            for mid in inbound:
                self.ledger.inbound_set(mid, state="answered", answered=time.time(), reply_ids=",".join(ids),
                                        turn_id=turn_id, note="reply redelivered after a restart")
                await self._react(ch.id, mid, DONE, remove=ACK)
            log.info("room %s: %s redelivered after a restart (%d part(s))", room.name, key, len(ids))

    # outbound: called from room threads
    def _call(self, coro: Coroutine[Any, Any, T], wait: bool = True) -> T | None:
        assert self.loop
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(180) if wait else None

    def open_card(self, room: str, text: str, kind: str = "turn") -> None:
        self._call(self._open(room, text, kind), wait=False)

    def progress(self, room: str, text: str) -> None:
        self.want[room] = text
        self._call(self._edit(room), wait=False)

    def reply(self, room: str, text: str, inbound: list[str], card: str | None = None,
              key: str | None = None) -> list[str]:
        """The reply; the card goes, unless `card` is given: then the card keeps that line."""
        return self._call(self._finish(room, text, inbound, DONE, card=card, key=key)) or []

    def notice(self, room: str, text: str, inbound: list[str], card: str | None = None,
               key: str | None = None) -> list[str]:
        """A visible failure: the turn's card kept and edited to `card` (what was owed), else `text` posted."""
        return self._call(self._finish(room, text, inbound, FAILED, kind="notice", card=card, key=key,
                                       instead=card is not None)) or []

    # outbound: on the loop
    def _lock(self, room: str) -> asyncio.Lock:
        return self.locks.setdefault(room, asyncio.Lock())

    async def _channel(self, room: str) -> Any:
        cid = self.names[room]
        return self.client.get_channel(cid) or await self.client.fetch_channel(cid)

    async def _react(self, cid: int, mid: int | str, emoji: str, remove: str | None = None) -> None:
        ch = self.client.get_channel(cid) or await self.client.fetch_channel(cid)
        pm = ch.get_partial_message(int(mid))
        try:
            if remove:
                await pm.remove_reaction(remove, self.client.user)
            await pm.add_reaction(emoji)
        except discord.HTTPException as e:
            log.warning("reaction on %s failed: %s", mid, e)

    async def _open(self, room: str, text: str, kind: str) -> None:
        async with self._lock(room):              # a stream edit already waiting stays: it is this turn's
            self.edited.pop(room, None)
            try:
                card = await (await self._channel(room)).send(text[:MAX])
            except discord.HTTPException as e:
                log.warning("card in %s failed: %s", room, e)
                return
            self.cards[room], self.shown[room] = card, text
            self.ledger.outbound(room=room, channel=str(card.channel.id), kind="progress", message_id=str(card.id),
                                 chars=len(text))
            self.ledger.card_open(str(card.id), room, kind)
        if room in self.want:                     # it arrived before the card did: show it now
            await self._edit(room)

    def _wait(self, room: str) -> float:
        last = self.edited.get(room)
        return 0.0 if last is None else MIN_EDIT - (time.monotonic() - last)

    async def _edit(self, room: str) -> None:
        while True:
            while (wait := self._wait(room)) > 0:
                await asyncio.sleep(wait)
            async with self._lock(room):
                if self._wait(room) > 0:          # another edit went out while this one waited for the lock
                    continue
                text, card = self.want.pop(room, None), self.cards.get(room)
                if text is None or card is None or text == self.shown.get(room):
                    return
                try:
                    await card.edit(content=text[:MAX])
                except discord.HTTPException as e:
                    log.warning("progress in %s failed: %s", room, e)
                    return
                self.edited[room], self.shown[room] = time.monotonic(), text
                self.ledger.outbound(room=room, channel=str(card.channel.id), kind="progress-edit",
                                     message_id=str(card.id), chars=len(text))
                return

    async def _finish(self, room: str, text: str, inbound: list[str], mark: str, kind: str = "reply",
                      card: str | None = None, key: str | None = None, instead: bool = False) -> list[str]:
        key = key or f"x{uuid.uuid4().hex[:12]}"
        if not instead:                           # every part on the ledger before the first post
            for i, part in enumerate(split_message(text.strip() or "(empty reply)", LIMIT)):
                self.ledger.delivery_plan(f"{key}:{i}", room, kind, part, ",".join(inbound))
        async with self._lock(room):
            self.want.pop(room, None)
            msg = self.cards.pop(room, None)
            ids: list[str] = []
            if instead and msg is None:           # no card to keep: the notice is posted
                instead = False
                for i, part in enumerate(split_message(text.strip(), LIMIT)):
                    self.ledger.delivery_plan(f"{key}:{i}", room, kind, part, ",".join(inbound))
            if not instead:
                ids = await self._deliver(room, key)
            try:
                if card is not None:
                    msg = msg or await (await self._channel(room)).send(card[:MAX])
                    await msg.edit(content=card[:MAX])
                    self.ledger.outbound(room=room, channel=str(msg.channel.id), kind="card-note",
                                         message_id=str(msg.id), chars=len(card), inbound=",".join(inbound))
                    self.ledger.card_close(str(msg.id), "kept: " + card.split("\n")[0][:80])
                    if instead:
                        self.ledger.delivery_plan(f"{key}:0", room, kind, card, ",".join(inbound))
                        self.ledger.delivery_set(f"{key}:0", state="sent", message_id=str(msg.id), sent=time.time(),
                                                 note="the turn's card, kept")
                        ids = [str(msg.id)]
                elif msg is not None:
                    await msg.delete()
                    self.ledger.card_close(str(msg.id), "replaced by the reply")
            except discord.HTTPException as e:
                log.warning("card in %s failed: %s", room, e)
        cid = self.names[room]
        for mid in inbound:
            await self._react(cid, mid, mark, remove=ACK)
        return ids

    async def _debug_pause(self, point: str) -> None:
        """A test hook for the crash drill: <state>/debug/pause-delivery holding "before-post 60"
        or "after-post 60" holds every delivery there for that many seconds. Absent: nothing."""
        f = self.state / "debug" / "pause-delivery" if self.state else None
        if f is None or not f.is_file():
            return
        words = f.read_text().split()
        if words and words[0] == point:
            secs = float(words[1]) if len(words) > 1 else 60.0
            log.warning("debug pause at %s for %.0f s", point, secs)
            await asyncio.sleep(secs)

    async def _deliver(self, room: str, key: str, label: bool = False) -> list[str]:
        """Post every part of `key` not yet in the room, in order; the caller holds the room's lock."""
        ch, ids = await self._channel(room), []
        rows = sorted((r for r in self.ledger.deliveries(room) if r["key"].rsplit(":", 1)[0] == key),
                      key=lambda r: int(r["key"].rsplit(":", 1)[1]))
        for r in rows:
            if r["state"] == "sent":
                ids.append(str(r["message_id"]))
                continue
            body = f"{REDELIVERED} {r['body']}" if label else str(r["body"])
            await self._debug_pause("before-post")
            try:
                m = await ch.send(body[:MAX])
            except discord.HTTPException as e:
                self.ledger.delivery_set(r["key"], state="failed", error=str(e)[:300],
                                         attempts=int(r["attempts"] or 0) + 1)
                self.ledger.outbound(room=room, channel=str(ch.id), kind=r["kind"], chars=len(body), error=str(e)[:300],
                                     inbound=r["inbound"])
                raise
            await self._debug_pause("after-post")
            self.ledger.delivery_set(r["key"], state="sent", message_id=str(m.id), sent=time.time(),
                                     attempts=int(r["attempts"] or 0) + 1, note=REDELIVERED if label else None)
            self.ledger.outbound(room=room, channel=str(ch.id), kind=r["kind"], message_id=str(m.id), chars=len(body),
                                 inbound=r["inbound"])
            ids.append(str(m.id))
        return ids

    async def _post(self, room: str, text: str, kind: str, inbound: list[str], key: str | None = None) -> list[str]:
        key = key or f"{kind[0]}{uuid.uuid4().hex[:12]}"
        for i, part in enumerate(split_message(text.strip() or "(empty)", LIMIT)):
            self.ledger.delivery_plan(f"{key}:{i}", room, kind, part, ",".join(inbound))
        async with self._lock(room):
            return await self._deliver(room, key)

    async def send_text(self, room: str, text: str, key: str | None = None) -> list[str]:
        return await self._post(room, text, "send", [], key)

    async def notify(self, room: str, text: str) -> list[str]:
        """A visible notice that answers no message (a refused send)."""
        return await self._post(room, text, "notice", [])

    def file_problem(self, ch: Any, path: Path) -> str | None:
        """Why a file cannot go to this room, or None."""
        limit = int(getattr(getattr(ch, "guild", None), "filesize_limit", 10 * 1024 * 1024))
        if not path.is_file():
            return "there is no such file"
        if path.suffix.lower() in FORBIDDEN:
            return f"{path.suffix.lower()} files are not sent (a type Yerk AI never posts)"
        size = path.stat().st_size
        if size > limit:
            return f"it is {size:,} bytes and this room takes {limit:,}"
        if size == 0:
            return "it is empty"
        return None

    async def send_file(self, room: str, path: Path, caption: str = "", key: str | None = None) -> list[str]:
        """The one file path: checked, sent from a snapshot under a new name, its message id
        recorded; any miss is a visible notice in the room and a ValueError for the caller."""
        key = key or f"f{uuid.uuid4().hex[:12]}"
        ch, name = await self._channel(room), safe_name(path.name)
        size = path.stat().st_size if path.is_file() else None
        self.ledger.delivery_plan(f"{key}:0", room, "file", caption, file=name, size=size, type=file_type(name))
        why = self.file_problem(ch, path)
        snap = None
        if why is None and self.state:
            outbox = self.state / "rooms" / room / "outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            snap = outbox / f"{time.strftime('%Y%m%d-%H%M%S')}-{key}-{name}"
            shutil.copyfile(path, snap)           # a later rewrite of the source cannot change what goes out
        m = None
        if why is None:
            try:
                async with self._lock(room):
                    m = await ch.send(content=caption[:MAX] or None, file=discord.File(str(snap or path), filename=name))
            except discord.HTTPException as e:
                why = f"Discord refused it ({e.status}: {str(e.text or e)[:120]})"
        if m is None:
            self.ledger.delivery_set(f"{key}:0", state="failed", error=why)
            self.ledger.outbound(room=room, channel=str(ch.id), kind="file", chars=len(caption), file=name,
                                 error=str(why)[:300])
            await self._post(room, f"Could not send {name}: {why}.", "notice", [], key=f"{key}-notice")
            raise ValueError(f"{name} was not sent: {why}; the room was told")
        self.ledger.delivery_set(f"{key}:0", state="sent", message_id=str(m.id), sent=time.time(), attempts=1)
        self.ledger.outbound(room=room, channel=str(ch.id), kind="file", message_id=str(m.id), chars=len(caption),
                             file=name)
        return [str(m.id)]


async def setup_room(token: str, guild_id: int, name: str, webhook_name: str,
                     channel_id: int | None = None) -> dict[str, Any]:
    """Find the text channel (by `channel_id` when given, which never creates one; else by `name`,
    created when absent) and its webhook `webhook_name` (created when absent). REST only."""
    client = discord.Client(intents=discord.Intents.none())
    await client.login(token)
    try:
        guild = await client.fetch_guild(guild_id)
        ch: discord.TextChannel | None
        if channel_id:
            got = await client.fetch_channel(channel_id)
            if not isinstance(got, discord.TextChannel) or got.guild.id != guild_id or got.name != name:
                raise ValueError(f"channel {channel_id} is not the text channel {name!r} in guild {guild_id}")
            ch = got
        else:
            ch = next((c for c in await guild.fetch_channels() if c.name == name and isinstance(c, discord.TextChannel)),
                      None)
        created = ch is None
        if ch is None:
            ch = await guild.create_text_channel(name, reason="Yerk AI test room")
        hook = next((w for w in await ch.webhooks() if w.name == webhook_name), None)
        made_hook = hook is None
        if hook is None:
            hook = await ch.create_webhook(name=webhook_name, reason="Yerk AI scripted test sender")
        return {"channel_id": ch.id, "channel_created": created, "webhook_id": hook.id,
                "webhook_created": made_hook, "webhook_url": hook.url}
    finally:
        await client.close()
