"""Yerk AI in Discord, through the discord.py library: one connection that reads only the channels rooms.toml names and posts their cards and replies.

- Every other channel is ignored before anything is read: no reply, no reaction, no history.
- Only the owner, and in test mode the listed webhooks, are answered. Webhook senders pass a
  loop guard (lifted/loopguard.py) so a runaway script cannot run the room.
- A message is ledgered by its Discord id before anything runs, then acked with a reaction.
  On start, messages a crash left open run again once, marked, and messages that arrived while
  the daemon was down are read from the room's own history and run.
- Replies: the final text only, split under 2,000 characters, never inside a code block. A turn
  owns one progress message, edited with the first visible line, then replaced by the reply.
- Files: one path (`send_file`), size checked against the channel's limit, message id recorded.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Coroutine, TypeVar

import discord

from ..ledger import Ledger
from ..lifted.loopguard import LoopGuard
from ..lifted.split import split_message
from ..room import Inbound, Room

log = logging.getLogger("yerk.discord")
T = TypeVar("T")
LIMIT = 1990          # Discord's 2,000 less a margin
ACK, DONE, FAILED = "\N{EYES}", "\N{WHITE HEAVY CHECK MARK}", "\N{WARNING SIGN}"


def message_text(m: Any) -> str:
    """The message body, with any attachments listed so none is dropped unseen."""
    lines = [m.content or ""]
    for a in getattr(m, "attachments", []) or []:
        lines.append(f"[attachment] {a.filename}, {a.size} bytes, {a.url}")
    return "\n".join(x for x in lines if x).strip()


class DiscordChannel:
    def __init__(self, token: str, owner: int, test_senders: list[int], ledger: Ledger,
                 client: Any = None, guard: LoopGuard | None = None):
        self.token, self.owner, self.test_senders, self.ledger = token, owner, set(test_senders), ledger
        self.rooms: dict[int, Room] = {}          # channel id -> room
        self.names: dict[str, int] = {}           # room name -> channel id
        self.cards: dict[str, Any] = {}           # room name -> progress message
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
        text = message_text(m)
        if not text or not self.ledger.inbound_add(str(m.id), room.name, str(m.channel.id), sender, len(text)):
            return
        await self._react(m.channel.id, m.id, ACK)
        room.submit(Inbound(str(m.id), sender, text))

    async def on_ready(self) -> None:
        self.loop = asyncio.get_running_loop()
        log.info("discord: connected as %s, serving %d room(s)", self.client.user, len(self.rooms))
        if not self.recovered:              # once per process: a reconnect must not re-run live turns
            self.recovered = True
            for cid, room in self.rooms.items():
                await self.recover(cid, room)

    async def recover(self, cid: int, room: Room) -> None:
        ch = self.client.get_channel(cid) or await self.client.fetch_channel(cid)
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
            text = message_text(m) + ("\n[redelivered after a restart]" if attempts else "")
            room.submit(Inbound(str(m.id), str(row["sender"]), text, attempt=attempts))
        last = self.ledger.last_inbound(str(cid))
        if last:
            async for m in ch.history(after=discord.Object(int(last)), limit=50, oldest_first=True):
                await self.take(room, m)

    # outbound: called from room threads
    def _call(self, coro: Coroutine[Any, Any, T], wait: bool = True) -> T | None:
        assert self.loop
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(120) if wait else None

    def progress(self, room: str, text: str) -> None:
        self._call(self._progress(room, text), wait=False)

    def reply(self, room: str, text: str, inbound: list[str]) -> list[str]:
        return self._call(self._finish(room, text, inbound, DONE)) or []

    def notice(self, room: str, text: str, inbound: list[str]) -> list[str]:
        return self._call(self._finish(room, text, inbound, FAILED, kind="notice")) or []

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

    async def _progress(self, room: str, text: str) -> None:
        async with self._lock(room):
            card = self.cards.get(room)
            try:
                if card is None:
                    self.cards[room] = card = await (await self._channel(room)).send(text[:LIMIT])
                    self.ledger.outbound(room=room, channel=str(card.channel.id), kind="progress",
                                         message_id=str(card.id), chars=len(text))
                else:
                    await card.edit(content=text[:LIMIT])
            except discord.HTTPException as e:
                log.warning("progress in %s failed: %s", room, e)

    async def _finish(self, room: str, text: str, inbound: list[str], mark: str, kind: str = "reply") -> list[str]:
        async with self._lock(room):
            card = self.cards.pop(room, None)
            if card is not None:
                try:
                    await card.delete()
                except discord.HTTPException as e:
                    log.warning("card delete in %s failed: %s", room, e)
            ids = await self._post_locked(room, text, kind, inbound)
        cid = self.names[room]
        for mid in inbound:
            await self._react(cid, mid, mark, remove=ACK)
        return ids

    async def _post(self, room: str, text: str, kind: str, inbound: list[str]) -> list[str]:
        async with self._lock(room):
            return await self._post_locked(room, text, kind, inbound)

    async def _post_locked(self, room: str, text: str, kind: str, inbound: list[str]) -> list[str]:
        ch, ids = await self._channel(room), []
        for chunk in split_message(text.strip() or "(empty reply)", LIMIT):
            try:
                m = await ch.send(chunk)
                ids.append(str(m.id))
                self.ledger.outbound(room=room, channel=str(ch.id), kind=kind, message_id=str(m.id), chars=len(chunk),
                                     inbound=",".join(inbound))
            except discord.HTTPException as e:
                self.ledger.outbound(room=room, channel=str(ch.id), kind=kind, chars=len(chunk), error=str(e)[:300],
                                     inbound=",".join(inbound))
                raise
        return ids

    async def send_text(self, room: str, text: str) -> list[str]:
        return await self._post(room, text, "send", [])

    async def send_file(self, room: str, path: Path, caption: str = "") -> list[str]:
        """The one file path: a real file under the channel's limit, its message id recorded."""
        ch = await self._channel(room)
        limit = int(getattr(getattr(ch, "guild", None), "filesize_limit", 10 * 1024 * 1024))
        if not path.is_file():
            raise FileNotFoundError(str(path))
        size = path.stat().st_size
        if size > limit:
            raise ValueError(f"{path.name} is {size:,} bytes; this channel takes {limit:,}")
        async with self._lock(room):
            m = await ch.send(content=caption[:LIMIT] or None, file=discord.File(str(path), filename=path.name))
        self.ledger.outbound(room=room, channel=str(ch.id), kind="file", message_id=str(m.id), chars=len(caption),
                             file=path.name)
        return [str(m.id)]


async def setup_room(token: str, guild_id: int, name: str, webhook_name: str) -> dict[str, Any]:
    """Find the text channel `name` in the guild (create it when absent) and its webhook
    `webhook_name` (create it when absent). REST only, no gateway connection."""
    client = discord.Client(intents=discord.Intents.none())
    await client.login(token)
    try:
        guild = await client.fetch_guild(guild_id)
        ch = next((c for c in await guild.fetch_channels() if c.name == name and isinstance(c, discord.TextChannel)), None)
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
