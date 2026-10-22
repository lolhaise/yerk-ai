"""Yerk AI in Discord, through the discord.py library: one connection that reads only the channels rooms.toml names and posts their cards and replies.

- Every other channel is ignored before anything is read: no reply, no reaction, no history.
- A room's `watch` channel is read as its own and never written to: what it sends, its card, its typing
  signal and its reactions go to its own channel only; a slash command there gets one ephemeral line.
- Only the owner, the people a room lists (`people` in rooms.toml), and in test mode the listed webhooks,
  are answered. Webhook senders pass a loop guard (lifted/loopguard.py) so a runaway script cannot run the room.
- A message is ledgered by its Discord id before anything runs, then acked with a reaction. Its
  files are staged in the room's inbox under their own names, a ledger row each; a file that
  cannot be fetched gets a visible "could not fetch NAME" and the turn still runs. Every notice
  takes its words from words.py (STYLE.md): state first, no path, no status code.
- A turn owns one card: posted when the turn starts, edited from the stream (the first edit at
  once, then at most one a second, the latest text winning), replaced by the reply, or kept and
  edited to say what was owed when the turn ends with nothing to show. A worker owns one too
  (workers.py), on the same cadence, kept through a restart and settled when the worker ends.
- Every outbound part has an idempotency key (the turn's key and the part's index) and its row in
  the delivery ledger before it is posted; the key is also the post's nonce, so Discord itself
  returns the first message for a repeat within its window. On start, parts a crash left
  unconfirmed are posted once, labelled, unless the room already shows them (a file: from its
  outbox snapshot, found by name and size); then messages a crash left open run again once,
  marked, told which files are already in the room, and messages that arrived while the daemon
  was down are read from the room's own history, filed and run.
- A sender's edits and deletes of their own messages: one still waiting is revised or dropped before it runs;
  an edit of one already run comes in again, marked; a reply says which message it answers.
- Replies: the final text only, split under 2,000 characters, never inside a code block.
- Files: one path (`send_file`): a snapshot under a new name in the room's outbox, size and type
  checked against what the bot can send (measured, BOT_UPLOAD), the message id recorded, a
  visible notice on any miss that names the limit in bytes.
- Slash commands (/model, /effort, /new, /stop, /status): on connect exactly this install's, on the
  guilds its rooms live in, never global; the owner's only, in a room only, each the daemon method
  the room's tool or `yerk` command calls; every answer ephemeral, inside Discord's three seconds.
  `[discord] commands = false` in yerkd.toml: none registered, none answered.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import mimetypes
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, TypeVar

import discord

from .. import status, transcribe, words
from ..config import EFFORTS
from ..ledger import Ledger
from ..lifted.loopguard import LoopGuard
from ..lifted.split import split_message
from ..room import Inbound, Room

log = logging.getLogger("yerk.discord")
T = TypeVar("T")
LIMIT = 1950          # Discord's 2,000 less room for the redelivery label
MAX = 2000
ACK, DONE, FAILED = "\N{EYES}", "\N{WHITE HEAVY CHECK MARK}", "\N{WARNING SIGN}"
REDELIVERED = words.SUB + words.SENT_AGAIN    # the line above a part posted again after a restart
MIN_EDIT = 1.0        # seconds between card edits after the first
TYPING_EVERY = 8.0    # the typing signal is refreshed this often while nothing is visible (Discord shows it ~10 s)
FORBIDDEN = {".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".dll", ".ps1", ".vbs", ".jar", ".apk", ".lnk", ".reg", ".hta", ".cpl", ".pif"}
# What the bot can send, measured 2026-10-17 through discord.py's own send with the bot's token in
# the test room's guild (boost tier 1, which discord.py's table puts at 10,485,760): 20,971,520
# bytes sent, 20,971,521 refused with 413 / 40005 "Request entity too large". Discord's answer wins
# over the library's table; a guild the table puts higher (tier 2 and 3) keeps the table's number.
BOT_UPLOAD = 20 * 1024 * 1024
REPLY_MAX = 300
QUICK = 1.0           # seconds a slash command may take before its answer is deferred (Discord gives three)
RETRY = 60.0          # seconds before a channel is read again after a message the ledger could not take (disk-full drill b)
SAID = (discord.MessageType.default, discord.MessageType.reply)   # what a person types; the rest are Discord's lines


def commands(models: dict[str, str]) -> list[dict[str, Any]]:
    """Exactly this install's slash commands, in the shape Discord's bulk overwrite takes; /model takes an account too,
    typed, never offered: a command's choices show to everyone in its guild, a people room's people too."""
    def pick(name: str, values: list[str]) -> list[dict[str, Any]]:
        return [{"type": 3, "name": name, "description": words.CHOICE[name], "required": False,
                 **({"choices": [{"name": v, "value": v} for v in values][:25]} if values else {})}]
    opts = {"model": pick("model", [*models, "default"]) + pick("account", []), "effort": pick("effort", [*EFFORTS, "default"])}
    return [{"type": 1, "name": n, "description": what, "options": opts.get(n, [])} for n, what in words.COMMANDS.items()]


def upload_limit(ch: Any) -> int:
    """The largest file this room takes, in bytes."""
    return max(BOT_UPLOAD, int(getattr(getattr(ch, "guild", None), "filesize_limit", 0) or 0))


def nonce_for(key: str) -> str:
    """A delivery key as the post's nonce (Discord takes 25 characters)."""
    return key if len(key) <= 25 else hashlib.sha256(key.encode()).hexdigest()[:25]


def refusal(e: Any, limit: int) -> str:
    """Why Discord turned a file down, in plain words; its status and text go to the log."""
    log.warning("discord refused a file: %s %s", e.status, str(e.text or e)[:200])
    if e.status == 413 or getattr(e, "code", None) == 40005:
        return words.file_why("too large", limit=words.size(limit))
    return words.file_why("refused")


def safe_name(name: str) -> str:
    """A file name that stays one path segment."""
    name = re.sub(r"[^\w.\- ]+", "_", Path(name).name).strip(" .") or "file"
    return name[:120]


def file_type(name: str, given: str | None = None) -> str:
    return (given or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0]


def message_text(m: Any, files: list[dict[str, Any]] | None = None, reply: str = "") -> str:
    """The message body (mentions as names), with what it replies to and its files listed (staged
    path, or why it is missing) so none is dropped unseen, a voice note with the line that it is heard only through
    the transcribe tool. Without `files`, the attachments as Discord lists them."""
    lines = [reply, str(getattr(m, "clean_content", None) or m.content or "")]
    if files is None:
        files = [{"name": a.filename, "size": a.size, "type": file_type(a.filename, getattr(a, "content_type", None)),
                  "ok": False, "error": "not staged"} for a in getattr(m, "attachments", []) or []]
    for f in files:
        if f.get("ok"):
            lines.append(f"[attachment] {f['name']}, {f['type']}, {f['size']:,} bytes, saved at {f['path']}")
            lines += [words.VOICE_NOTE.format(name=f["name"])] if transcribe.is_audio(f["name"], f["type"]) else []
        else:
            lines.append(f"[attachment] {f['name']}: could not be fetched ({f.get('error', 'unknown')})")
    return "\n".join(x for x in lines if x).strip()


class DiscordChannel:
    def __init__(self, token: str, owner: int | None, test_senders: list[int], ledger: Ledger,
                 client: Any = None, guard: LoopGuard | None = None, state: Path | None = None,
                 capture: Callable[..., bool] | None = None, daemon: Any = None, taste: Callable[[str, str], Any] | None = None):
        self.token, self.owner, self.test_senders, self.ledger = token, owner, set(test_senders), ledger
        self.state = state
        self.daemon = daemon                      # the slash commands' methods (daemon.Yerkd), or None: off
        self.capture = capture                    # the day's log (memory.DailyLog.capture), or None
        self.taste = taste                        # the owner's verdict on a room's last reply (daemon.Yerkd.taste_heard), or None
        self.rooms: dict[int, Room] = {}          # channel id -> room: its own, and the one it watches
        self.names: dict[str, int] = {}           # room name -> its own channel id, where everything goes
        self.cards: dict[str, Any] = {}           # room name -> its open card
        self.want: dict[str, str] = {}            # room name -> the card text waiting to be shown
        self.shown: dict[str, str] = {}
        self.edited: dict[str, float] = {}        # room name -> when its card was last edited
        self.locks: dict[str, asyncio.Lock] = {}
        self.guard = guard or LoopGuard()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False                   # /status: the gateway said ready (yerk deploy's proof)
        self.recovered = False
        self.edits: dict[str, str] = {}           # message id -> the edit already taken (its edited_at)
        self.people: dict[int, frozenset[int]] = {}   # channel id -> the user ids answered there besides the owner
        self.after_recover: list[Callable[[], Awaitable[None]]] = []   # once, after the rooms recovered
        self.typing: dict[str, asyncio.Task[None]] = {}   # room name -> its typing refresh, while nothing is visible
        self.retry: set[int] = set()              # channels read again in RETRY s: a message the ledger could not take
        if client is None:
            intents = discord.Intents.none()
            intents.guilds = intents.guild_messages = intents.message_content = True
            client = discord.Client(intents=intents, max_messages=None, chunk_guilds_at_startup=False,
                                    member_cache_flags=discord.MemberCacheFlags.none())
        self.client = client
        client.event(self.on_ready)
        client.event(self.on_message)
        client.event(self.on_raw_message_edit)    # raw: with no message cache these are the only ones
        client.event(self.on_raw_message_delete)
        client.event(self.on_interaction)

    def add_room(self, channel_id: int, room: Room, watch: int | None = None, people: frozenset[int] = frozenset()) -> None:
        """The room's own channel, the one it watches (read as its own), and the people it answers there besides the owner."""
        self.names[room.name] = channel_id
        for cid in (channel_id, watch) if watch else (channel_id,):
            self.rooms[cid], self.people[cid] = room, people

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.client.start(self.token)

    async def close(self) -> None:
        """A stop: the gateway told and the session closed clean, so the next start identifies fresh."""
        await self.client.close()

    # inbound
    def sender_of(self, m: Any) -> str | None:
        """owner, person (a user id in this room's `people`) or test-webhook; None: not answered, whoever it is."""
        if getattr(m, "type", discord.MessageType.default) not in SAID and m.channel.id not in self.names.values():
            return None                           # watched: a system line (a thread's name) is not the owner's words
        if getattr(m, "webhook_id", None):
            return "test-webhook" if m.webhook_id in self.test_senders else None
        if self.client.user is not None and m.author.id == self.client.user.id:
            return None
        if self.owner is not None and m.author.id == self.owner:
            return "owner"
        return "person" if m.author.id in self.people.get(m.channel.id, ()) else None

    def reply_line(self, room: str, m: Any) -> str:
        """Which message a reply answers, so the model knows what "this" means. In a watched channel a reply to anyone
        but the owner or a test sender only says it is one, so the room never reads the other side's words there."""
        ref = getattr(m, "reference", None)
        if ref is None or getattr(m, "type", None) != discord.MessageType.reply:
            return ""
        got = getattr(ref, "resolved", None)
        if got is None or not hasattr(got, "content"):
            return f"[a reply to an earlier message ({ref.message_id}) that is no longer there]"
        if m.channel.id != self.names[room] and self.sender_of(got) is None:
            return "[a reply to an earlier message]"
        who = "your message" if got.author.id == self._me() else f"{got.author.display_name}'s message"
        said = " ".join(str(getattr(got, "clean_content", got.content) or "").split())
        said = said or ", ".join(f"file {a.filename}" for a in getattr(got, "attachments", []) or []) or "(no text)"
        return f'[a reply to {who}: "{said if len(said) <= REPLY_MAX else said[:REPLY_MAX - 3] + "..."}"]'

    async def on_message(self, m: Any) -> None:
        if (room := self.rooms.get(m.channel.id)) is not None:
            self.file(room.name, m)
            await self.take(room, m)

    def file(self, room: str, m: Any, edited: bool = False) -> None:
        """Every message in a room's own channel into the day's log as it lands (L55), whoever sent it, taken or not;
        in a watched one, the owner's only. Yerk AI's own posts are filed where they are sent; cards never."""
        role = self.sender_of(m)              # watched: blind to the other side, so recall never shows its answers
        if self.capture is None or m.author.id == self._me() or (role is None and m.channel.id != self.names[room]):
            return
        role = role or ("webhook" if getattr(m, "webhook_id", None) else "other")
        text = "\n".join(x for x in (self.reply_line(room, m), str(getattr(m, "clean_content", None) or m.content or "")) if x)
        files = [(a.filename, file_type(a.filename, getattr(a, "content_type", None)), int(a.size or 0))
                 for a in getattr(m, "attachments", []) or []]
        who = str(getattr(getattr(m, "author", None), "name", "") or "")      # the handle, beside the role
        self._file(room, str(m.id), role, text, files, getattr(m, "created_at", None), edited, who)

    def _file(self, room: str, mid: str, role: str, text: str, files: list[tuple[str, str, int]] | None = None,
              when: Any = None, edited: bool = False, who: str = "") -> None:
        if self.capture is None:
            return
        try:
            self.capture(room, mid, role, text, files or [], when=when, edited=edited, who=who)
        except OSError as e:
            log.warning("room %s: message %s not filed in the day's log: %s", room, mid, e)

    async def take(self, room: Room, m: Any) -> None:
        if (sender := self.sender_of(m)) is None:
            return
        if sender == "test-webhook" and not self.guard.admit(m.channel.id)[0]:
            log.warning("room %s: loop guard holding webhook messages", room.name)
            return
        if not (m.content or getattr(m, "attachments", None)):
            return
        made = getattr(m, "created_at", None)
        try:
            if not self.ledger.inbound_add(str(m.id), room.name, str(m.channel.id), sender, len(m.content or ""),
                                           created=made.timestamp() if made is not None and hasattr(made, "timestamp") else None):
                return
        except sqlite3.OperationalError as e:     # the ledger cannot be written (the disk full): not lost, the channel is
            log.error("room %s: message %s not taken, the ledger cannot be written (%s); read again in %.0f s", room.name, m.id, e, RETRY)
            if m.channel.id not in self.retry:    # read again once it can (disk-full drill b), one retry per channel at a time
                self.retry.add(m.channel.id)
                asyncio.ensure_future(self._retry(m.channel.id, room))
            return
        if sender == "owner" and self.taste is not None and m.content and m.channel.id == self.names[room.name]:
            try:                                  # rated before the turn runs: the room's last reply is the one the owner saw
                self.taste(room.name, m.content)
            except Exception:                     # a record, never a reason to hold the message
                log.exception("room %s: the owner's verdict on message %s was not kept", room.name, m.id)
        await self._typing_begin(room.name, await self._channel(room.name))   # its own channel, never the watched one
        await self._react(m.channel.id, m.id, ACK)
        files = await self.stage(room, m)
        room.submit(Inbound(str(m.id), sender, message_text(m, files, self.reply_line(room.name, m)), files=files))

    # the typing signal: "heard you", from the moment a message is taken until something is visible
    async def _typing_begin(self, room: str, ch: Any) -> None:
        """The first typing event at once, then a refresh every TYPING_EVERY seconds, unless a card is
        up (the card is what says what is happening) or one already runs. Ledgered as nothing: it is
        not a delivery."""
        if room in self.cards or room in self.typing or not hasattr(ch, "typing"):
            return
        try:
            await ch.typing()
        except Exception as e:                    # cosmetic: a failed typing event never holds the message
            log.debug("typing in %s failed: %s", room, e)
            return
        self.typing[room] = asyncio.ensure_future(self._typing(room, ch))

    async def _typing(self, room: str, ch: Any) -> None:
        try:
            while True:
                await asyncio.sleep(TYPING_EVERY)
                if room in self.cards:
                    return
                await ch.typing()
        except Exception as e:
            log.debug("typing in %s failed: %s", room, e)
        finally:
            if self.typing.get(room) is asyncio.current_task():
                self.typing.pop(room, None)

    def _typing_end(self, room: str) -> None:
        """The card is posted, or the reply is: nothing more to signal."""
        if (t := self.typing.pop(room, None)) is not None:
            t.cancel()

    def _me(self) -> int | None:
        return self.client.user.id if self.client.user is not None else None

    async def on_raw_message_edit(self, p: Any) -> None:
        """An edit of a message the room took, by its own sender: still waiting, it runs as edited; already run (or
        running), the new text comes in again, marked. Not an edit (an embed unfurl), or another sender's: nothing."""
        m, room = getattr(p, "message", None), self.rooms.get(int(p.channel_id))
        if room is None or m is None or getattr(m, "edited_at", None) is None:
            return
        self.file(room.name, m, edited=True)
        sender, mid = self.sender_of(m), str(m.id)
        row = self.ledger.inbound_get(mid)
        if sender is None or row is None or row["sender"] != sender or self.edits.get(mid) == str(m.edited_at):
            return
        self.edits[mid] = str(m.edited_at)
        files = await self.stage(room, m)
        text = message_text(m, files, self.reply_line(room.name, m))
        if room.revise(mid, text):
            self.ledger.inbound_set(mid, note="edited before it ran")
            return
        self.ledger.inbound_set(mid, state="received", attempts=0, note=f"edited after it ran ({row['state']})")
        await self._react(m.channel.id, m.id, ACK)
        room.submit(Inbound(mid, sender, f"[an earlier message was edited; it now reads:]\n{text}", files=files))

    async def on_raw_message_delete(self, p: Any) -> None:
        """A message deleted while it still waits never runs; one already run keeps its answer."""
        room, mid = self.rooms.get(int(p.channel_id)), str(p.message_id)
        row = self.ledger.inbound_get(mid) if room else None
        if room is None or row is None:
            return
        if room.withdraw(mid):
            self.ledger.inbound_set(mid, state="ignored", note="deleted by its sender before it ran")
        else:
            self.ledger.inbound_set(mid, note=f"deleted by its sender after it ran ({row['state']})")

    async def stage(self, room: Room, m: Any) -> list[dict[str, Any]]:
        """Each attachment saved under the room's inbox with its own name, a ledger row each; a
        visible notice for any that cannot be fetched."""
        files: list[dict[str, Any]] = []
        for a in getattr(m, "attachments", []) or []:
            f, name = {"name": a.filename, "size": int(a.size or 0), "type": file_type(a.filename, a.content_type)}, safe_name(a.filename)
            try:
                dest = room.inbox(str(m.id)) / (name if all(safe_name(x["name"]) != name for x in files) else f"{len(files)}-{name}")   # a name twice: 1-image.png
                if not dest.is_file() or dest.stat().st_size != f["size"]:
                    await a.save(dest)
                f.update(ok=True, path=str(dest), size=dest.stat().st_size)
                self.ledger.attachment(message_id=str(m.id), room=room.name, name=a.filename, size=f["size"],
                                       type=f["type"], path=str(dest), state="staged")
            except (discord.HTTPException, OSError) as e:
                f.update(ok=False, error=f"{type(e).__name__}: {str(e)[:120]}")
                self.ledger.attachment(message_id=str(m.id), room=room.name, name=a.filename, size=f["size"],
                                       type=f["type"], state="missing", error=f["error"])
                await self._post(room.name, f"{words.NOT_DONE} {words.NOT_FETCHED.format(name=a.filename)}", "notice", [str(m.id)])
            files.append(f)
        return files

    async def on_ready(self) -> None:
        self.loop = asyncio.get_running_loop()
        log.info("discord: connected as %s, serving %d room(s)", self.client.user, len(self.names))
        self.connected = True
        first, self.recovered = not self.recovered, True   # the recovery once per process: a reconnect must not re-run
        for cid, room in self.rooms.items():               # live turns; the catch-up on every ready (network drill 3)
            try:
                await (self.recover if first else self.catch_up)(cid, room)
            except Exception as e:    # whatever one channel hits (a 403, a 404, the network) never stops the rest
                log.warning("room %s: channel %s not recovered after the restart: %s", room.name, cid, e)
        if not first:
            return
        for hook in self.after_recover:
            try:
                await hook()
            except Exception:
                log.exception("discord: a start-up step after recovery failed")
        await self.register()

    async def catch_up(self, cid: int, room: Room) -> None:
        """The owner's messages the channel got since the last one ledgered: 50 at most, however many others wrote
        between them, which never count; each filed and taken as on_message does. After every ready, since a gateway
        session Discord let go replays nothing (network drill 3), and again RETRY s after a message the ledger could
        not take (disk-full drill b); a channel with no ledgered message yet has no place to read from."""
        self.retry.discard(cid)
        last, left = self.ledger.last_inbound(str(cid)), 50
        if not last:
            return
        ch = self.client.get_channel(cid) or await self.client.fetch_channel(cid)
        async for m in ch.history(after=discord.Object(int(last)), limit=None, oldest_first=True):
            if (left := left - (self.sender_of(m) is not None)) < 0:
                break
            self.file(room.name, m)                    # as on_message: into the day's log, then taken
            await self.take(room, m)

    async def _retry(self, cid: int, room: Room) -> None:
        await asyncio.sleep(RETRY)
        try:
            await self.catch_up(cid, room)             # a take that fails again schedules the next read itself
        except Exception as e:
            log.warning("room %s: the channel could not be read again: %s", room.name, e)

    # slash commands
    async def register(self) -> None:
        """This install's commands, exactly, on each guild its rooms live in (guild commands show at once;
        never global): Discord's bulk overwrite replaces whatever the application had there."""
        if self.daemon is None:
            return
        guilds = set()
        for cid in self.names.values():
            try:
                guilds.add(int((self.client.get_channel(cid) or await self.client.fetch_channel(cid)).guild.id))
            except (discord.HTTPException, AttributeError) as e:
                log.warning("discord: no guild for channel %s, no commands there: %s", cid, e)
        for g in sorted(guilds):
            try:
                await self.client.http.bulk_upsert_guild_commands(self.client.application_id, g, commands(self.daemon.models))
                log.info("discord: %d slash command(s) registered in guild %s", len(words.COMMANDS), g)
            except discord.HTTPException as e:
                log.warning("discord: slash commands not registered in guild %s: %s", g, e)

    async def on_interaction(self, it: Any) -> None:
        """A slash command: the owner's only, in a room only; anyone else, or any other channel, gets one
        ephemeral line. Off (`daemon` None): left to whatever else answers."""
        if self.daemon is None or it.type != discord.InteractionType.application_command:
            return
        data = it.data or {}
        args = {str(o["name"]): str(o.get("value", "")) for o in data.get("options") or []}
        room = self.rooms.get(int(it.channel_id or 0))
        if self.owner is None:                    # yerkd.toml names no owner: no one is, and the line says so
            await it.response.send_message(f"{words.NOT_DONE} {words.NO_OWNER}", ephemeral=True)
        elif getattr(it.user, "id", None) != self.owner:
            await it.response.send_message(f"{words.NOT_DONE} {words.NOT_OWNER}", ephemeral=True)
        elif room is None:
            await it.response.send_message(f"{words.NOT_DONE} {words.NOT_A_ROOM}", ephemeral=True)
        elif (own := self.names[room.name]) != int(it.channel_id):     # a watched channel: the room answers elsewhere
            await it.response.send_message(f"{words.NOT_DONE} {words.WATCHED.format(room=room.name, own=own)}", ephemeral=True)
        else:
            await self._answer(it, self._command(str(data.get("name", "")), room.name, args))

    async def _command(self, name: str, room: str, args: dict[str, str]) -> str:
        """Each command is the daemon method the room's tool or `yerk` command calls; no second path."""
        d = self.daemon
        if name == "status":
            return "\n".join(status.card(await d.status_now({})))
        if name in ("model", "effort"):
            r = await d.tool_settings(room, args.get("model", ""), args.get("effort", ""), args.get("account", ""), by_owner=True)
            return f"{words.DONE if r['ok'] and args else words.PLAIN if r['ok'] else words.NOT_DONE} {r['line']}"
        if name in ("new", "stop"):
            r = await (d.room_new(room) if name == "new" else d.room_stop(room))
            return f"{words.DONE if r['ok'] else words.PLAIN} {r['line']}"
        return f"{words.NOT_DONE} {words.NOT_RUN}"

    async def _answer(self, it: Any, call: Awaitable[str]) -> None:
        """Ephemeral, inside Discord's three seconds: a call still running after QUICK is deferred first
        (Discord shows it thinking) and answered when it ends."""
        task = asyncio.ensure_future(call)
        if (slow := not (await asyncio.wait({task}, timeout=QUICK))[0]):
            await it.response.defer(ephemeral=True, thinking=True)
        try:
            text = await task
        except Exception:
            log.exception("discord: a slash command failed")
            text = f"{words.NOT_DONE} {words.NOT_RUN}"
        await (it.followup.send if slow else it.response.send_message)(text[:MAX], ephemeral=True)

    async def recover(self, cid: int, room: Room) -> None:
        ch = self.client.get_channel(cid) or await self.client.fetch_channel(cid)
        if cid == self.names[room.name]:                   # its own: what a restart left there; a watched one is only read
            for c in self.ledger.cards_open(room.name):        # their turns ended with the old process
                if c["kind"] == "worker":                      # a worker outlives the daemon: its card stays
                    continue
                with contextlib.suppress(discord.HTTPException):
                    await ch.get_partial_message(int(c["message_id"])).delete()
                self.ledger.card_close(c["message_id"], "removed after a restart")
            await self.redeliver(ch, room)
        for row in self.ledger.inbound_open(room.name):
            if row["channel"] != str(cid):                 # the other channel's, or a relay (workers.py's to redo)
                continue
            if (attempts := int(row["attempts"] or 0)) >= 2:
                self.ledger.inbound_set(row["message_id"], state="abandoned", note="redelivered once already")
                await self._post(room.name, f"{words.NOT_DONE} {words.NOT_RUN_AGAIN}", "notice", [row["message_id"]])
                continue
            try:
                m = await ch.fetch_message(int(row["message_id"]))
            except discord.NotFound:
                self.ledger.inbound_set(row["message_id"], state="abandoned", note="message deleted")
                continue
            files = await self.stage(room, m)
            text = message_text(m, files, self.reply_line(room.name, m)) + ("\n[redelivered after a restart]" if attempts else "")
            sent = self.ledger.files_sent_for([str(row["message_id"])])
            if sent:                              # the cut-short turn's files: in the room already
                text += ("\n[already in the room from before the restart, not to be sent again: "
                         + "; ".join(f"file {r['file']} (message {r['message_id']})" for r in sent) + "]")
            room.submit(Inbound(str(m.id), str(row["sender"]), text, attempt=attempts, files=files))
        await self.catch_up(cid, room)                     # then what arrived while the daemon was down

    async def redeliver(self, ch: Any, room: Room) -> None:
        """Parts a crash left unconfirmed: posted once, labelled, unless the room already shows
        them (posted before the crash, never confirmed); their messages are then answered."""
        pending = self.ledger.deliveries(room.name, "pending")
        if not (keys := list(dict.fromkeys(r["key"].rsplit(":", 1)[0] for r in pending))):
            return
        mine: dict[str, str] = {}
        files: list[tuple[str, str, int]] = []    # (message id, file name, size) of the bot's own posts
        oldest = min(r["created"] for r in pending) - 5
        after = discord.Object(int((oldest * 1000 - 1420070400000)) << 22)
        async for m in ch.history(after=after, limit=100, oldest_first=True):
            if self.client.user is not None and m.author.id == self.client.user.id:
                mine.setdefault((m.content or "").strip(), str(m.id))
                files += [(str(m.id), a.filename, int(a.size or 0)) for a in getattr(m, "attachments", []) or []]
        claimed = {str(r["message_id"]) for r in self.ledger.deliveries(room.name) if r["message_id"]}
        for key in keys:
            rows = sorted((r for r in self.ledger.deliveries(room.name) if r["key"].rsplit(":", 1)[0] == key),
                          key=lambda r: int(r["key"].rsplit(":", 1)[1]) if r["key"].rsplit(":", 1)[1].isdigit() else 0)
            if rows[0]["kind"] == "file":
                await self._redeliver_file(ch, room.name, rows[0], files, claimed)
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

    async def _redeliver_file(self, ch: Any, room: str, r: dict[str, Any], seen: list[tuple[str, str, int]], claimed: set[str]) -> None:
        """A file a crash left unconfirmed: in the room already (a bot post since it was planned with
        an attachment of its name and size that no other delivery holds), it is recorded; absent, it
        is sent once from its outbox snapshot, labelled; with no snapshot, the room is told."""
        key, name, size = str(r["key"]), str(r["file"] or ""), r["size"]
        hit = next((mid for mid, n, s in seen if n == name and (size is None or s == int(size)) and mid not in claimed), None)
        if hit:
            claimed.add(hit)
            self.ledger.delivery_set(key, state="sent", message_id=hit, sent=time.time(), note="found in the room after a restart")
            log.info("room %s: file %s (%s) found in the room after a restart", room, name, key)
            return
        base = key.rsplit(":", 1)[0]
        snaps = sorted((self.state / "rooms" / room / "outbox").glob(f"*-{base}-{name}")) if self.state else []
        snap = snaps[-1] if snaps else None
        why = words.file_why("gone") if snap is None else self.file_problem(ch, snap)
        caption = f"{REDELIVERED}\n{r['body'] or ''}".strip()
        m = None
        if why is None and snap is not None:
            try:
                async with self._lock(room):
                    m = await ch.send(content=caption[:MAX], file=discord.File(str(snap), filename=name), nonce=nonce_for(key))
            except discord.HTTPException as e:
                why = refusal(e, upload_limit(ch))
        if m is None:
            self.ledger.delivery_set(key, state="failed", error=f"not confirmed before a restart; {why}")
            await self._post(room, f"{words.NOT_DONE} {words.NOT_SENT_AGAIN.format(name=name, why=why)}", "notice", [], key=f"{base}-notice")
            return
        self.ledger.delivery_set(key, state="sent", message_id=str(m.id), sent=time.time(),
                                 attempts=int(r["attempts"] or 0) + 1, note=REDELIVERED)
        self.ledger.outbound(room=room, channel=str(ch.id), kind="file", message_id=str(m.id), chars=len(caption),
                             file=name, inbound=r["inbound"])
        log.info("room %s: file %s (%s) sent again from its snapshot after a restart", room, name, key)

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

    def reply(self, room: str, text: str, inbound: list[str], card: str | None = None, key: str | None = None) -> list[str]:
        """The reply; the card goes, unless `card` is given: then the card keeps that line."""
        return self._call(self._finish(room, text, inbound, DONE, card=card, key=key)) or []

    def notice(self, room: str, text: str, inbound: list[str], card: str | None = None, key: str | None = None) -> list[str]:
        """A visible failure: the turn's card kept and edited to `card` (what was owed), else `text` posted."""
        return self._call(self._finish(room, text, inbound, FAILED, kind="notice", card=card, key=key, instead=card is not None)) or []

    # outbound: on the loop
    def _lock(self, room: str) -> asyncio.Lock:
        return self.locks.setdefault(room, asyncio.Lock())

    async def _channel(self, room: str) -> Any:
        cid = self.names[room]
        return self.client.get_channel(cid) or await self.client.fetch_channel(cid)

    async def _react(self, cid: int, mid: int | str, emoji: str, remove: str | None = None) -> None:
        """Only in a room's own channel, on a message the ledger does not place elsewhere (watched, a relay)."""
        row = self.ledger.inbound_get(str(mid))
        if cid not in self.names.values() or (row is not None and row["channel"] != str(cid)):
            return
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
            self._typing_end(room)                # the card exists: the typing signal stops here
            self.ledger.outbound(room=room, channel=str(card.channel.id), kind="progress", message_id=str(card.id),
                                 chars=len(text), body=text[:MAX])
            self.ledger.card_open(str(card.id), room, kind)
        if room in self.want:                     # it arrived before the card did: show it now
            await self._edit(room)

    def _wait(self, key: str) -> float:
        last = self.edited.get(key)
        return 0.0 if last is None else MIN_EDIT - (time.monotonic() - last)

    async def _edit(self, room: str, key: str | None = None) -> None:
        """Show the card's newest wanted text: at most one edit a second per card. `key` is the
        card's own (a worker's); a turn's card is keyed by its room."""
        key = key or room
        while True:
            while (wait := self._wait(key)) > 0:
                await asyncio.sleep(wait)
            async with self._lock(key):
                if self._wait(key) > 0:           # another edit went out while this one waited for the lock
                    continue
                text, card = self.want.pop(key, None), self.cards.get(key)
                if text is None or card is None or text == self.shown.get(key):
                    return
                try:
                    await card.edit(content=text[:MAX])
                except discord.HTTPException as e:
                    log.warning("progress in %s failed: %s", room, e)
                    return
                self.edited[key], self.shown[key] = time.monotonic(), text
                self.ledger.outbound(room=room, channel=str(self.names[room]), kind="progress-edit",
                                     message_id=str(card.id), chars=len(text), body=text[:MAX])
                return

    # worker cards (workers.py), on the event loop
    async def worker_card(self, room: str, key: str, text: str, message_id: str | None = None) -> str | None:
        """A worker's card: posted the first time, or taken back by its message id after a restart,
        then edited on the turn cards' cadence, the latest text winning. Returns its message id."""
        async with self._lock(key):
            if key not in self.cards:
                ch = await self._channel(room)
                if message_id:
                    self.cards[key] = ch.get_partial_message(int(message_id))
                else:
                    try:
                        card = await ch.send(text[:MAX])
                    except discord.HTTPException as e:
                        log.warning("worker card in %s failed: %s", room, e)
                        return None
                    self.cards[key], self.shown[key] = card, text
                    self.ledger.outbound(room=room, channel=str(ch.id), kind="worker-card", message_id=str(card.id),
                                         chars=len(text), body=text[:MAX])
                    self.ledger.card_open(str(card.id), room, "worker")
                    return str(card.id)
            self.want[key] = text
        await self._edit(room, key)
        card = self.cards.get(key)
        return str(card.id) if card is not None else message_id

    async def worker_settle(self, room: str, key: str, text: str, how: str, message_id: str | None = None) -> str | None:
        """The worker's last card text, shown on the same cadence; the card then closes in the
        ledger and is never edited again."""
        mid = await self.worker_card(room, key, text, message_id)
        async with self._lock(key):
            card = self.cards.pop(key, None)
            for d in (self.want, self.shown, self.edited):
                d.pop(key, None)
        if card is not None:
            self.ledger.card_close(str(card.id), how)
        return mid

    async def _finish(self, room: str, text: str, inbound: list[str], mark: str, kind: str = "reply",
                      card: str | None = None, key: str | None = None, instead: bool = False) -> list[str]:
        key = key or f"x{uuid.uuid4().hex[:12]}"
        self._typing_end(room)                    # the reply (or the notice) is what becomes visible now
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
                                         message_id=str(msg.id), chars=len(card), inbound=",".join(inbound),
                                         body=card[:MAX])
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
            if mid.isdigit():                     # a Discord message; the harness's own (a relay) has no reaction
                await self._react(cid, mid, mark, remove=ACK)
        return ids

    async def _debug_pause(self, point: str) -> None:
        """A test hook for the crash drill: <state>/debug/pause-delivery holding "before-post 60"
        or "after-post 60" holds every delivery there for that many seconds. Absent: nothing."""
        f = self.state / "debug" / "pause-delivery" if self.state else None
        if f is None or not f.is_file():
            return
        if (words := f.read_text().split()) and words[0] == point:
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
            body = f"{REDELIVERED}\n{r['body']}" if label else str(r["body"])
            await self._debug_pause("before-post")
            try:
                m = await ch.send(body[:MAX], nonce=nonce_for(str(r["key"])))
            except discord.HTTPException as e:
                self.ledger.delivery_set(r["key"], state="failed", error=str(e)[:300], attempts=int(r["attempts"] or 0) + 1)
                self.ledger.outbound(room=room, channel=str(ch.id), kind=r["kind"], chars=len(body), error=str(e)[:300],
                                     inbound=r["inbound"])
                raise
            await self._debug_pause("after-post")
            self.ledger.delivery_set(r["key"], state="sent", message_id=str(m.id), sent=time.time(),
                                     attempts=int(r["attempts"] or 0) + 1, note=REDELIVERED if label else None)
            self.ledger.outbound(room=room, channel=str(ch.id), kind=r["kind"], message_id=str(m.id), chars=len(body), inbound=r["inbound"])
            self._file(room, str(m.id), "yerk", body)
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
        limit = upload_limit(ch)
        if not path.is_file():
            return words.file_why("missing")
        if path.suffix.lower() in FORBIDDEN:
            return words.file_why("type", ext=path.suffix.lower())
        if (size := path.stat().st_size) > limit:
            said, cap = words.sizes(size, limit)
            return words.file_why("size", size=said, limit=cap)
        return words.file_why("empty") if size == 0 else None

    async def send_file(self, room: str, path: Path, caption: str = "", key: str | None = None, inbound: str = "") -> list[str]:
        """The one file path: checked, sent from a snapshot under a new name, its message id
        recorded; any miss is a visible notice in the room and a ValueError for the caller. A file
        the same message already had in the room (a turn a crash cut short, run again) is not sent
        twice: its message id comes back."""
        key = key or f"f{uuid.uuid4().hex[:12]}"
        ch, name = await self._channel(room), safe_name(path.name)
        size = path.stat().st_size if path.is_file() else None
        if inbound and size is not None:
            for r in self.ledger.files_sent_for(inbound.split(",")):
                if r["file"] == name and r["size"] == size:
                    log.info("room %s: %s is in the room already for this message (%s); not sent again", room, name, r["message_id"])
                    return [str(r["message_id"])]
        row = self.ledger.delivery_plan(f"{key}:0", room, "file", caption, inbound, file=name, size=size, type=file_type(name))
        if row["state"] == "sent":                # this key went out already
            return [str(row["message_id"])]
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
                    await self._debug_pause("before-post")
                    m = await ch.send(content=caption[:MAX] or None, file=discord.File(str(snap or path), filename=name),
                                      nonce=nonce_for(f"{key}:0"))
                    await self._debug_pause("after-post")
            except discord.HTTPException as e:
                why = refusal(e, upload_limit(ch))
        if m is None:
            self.ledger.delivery_set(f"{key}:0", state="failed", error=why)
            self.ledger.outbound(room=room, channel=str(ch.id), kind="file", chars=len(caption), file=name, error=str(why)[:300])
            await self._post(room, f"{words.NOT_DONE} {words.NOT_SENT.format(name=name, why=why)}", "notice", [], key=f"{key}-notice")
            raise ValueError(f"{name} did not go out: {why}; the room was told")
        self.ledger.delivery_set(f"{key}:0", state="sent", message_id=str(m.id), sent=time.time(), attempts=1)
        self.ledger.outbound(room=room, channel=str(ch.id), kind="file", message_id=str(m.id), chars=len(caption),
                             file=name, inbound=inbound)
        self._file(room, str(m.id), "yerk", caption, [(name, file_type(name, None), path.stat().st_size)])
        return [str(m.id)]


async def setup_room(token: str, guild_id: int, name: str, webhook_name: str, channel_id: int | None = None) -> dict[str, Any]:
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
