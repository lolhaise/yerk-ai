"""Yerk AI in Discord, through the discord.py library: one connection that reads only the channels rooms.toml names and posts their cards and replies.

- Every other channel is ignored before anything is read: no reply, no reaction, no history.
- A room's `watch` channel is read as its own and never written to: what it sends, its card, its typing
  signal and its reactions go to its own channel only; a slash command there gets one ephemeral line. A message
  taken there waits (Hold) for the answer of the assistant there (`watch_assistant`), counted again from its progress
  card's newest edit while that card is up (CARD_PATIENCE waits at most), then reaches the room in one turn with it,
  under a line saying it was sent to that assistant. Another assistant's message there (`[watch] assistants` in
  yerkd.toml) is filed under its own role once it has settled (SETTLE), its progress card never, and never answered;
  its files, there or in the room's own channel, are saved to the room's inbox as a person's are (_keep).
- One read past those channels: the message whose link `watch` or `attachments` is handed, in an owner's room anywhere
  in its own server, read once for the files attached to it (pull, files), which are saved as a person's files are.
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
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Coroutine, TypeVar
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.http import handle_message_parameters

from .. import addons, files as extracted, pictures, status, transcribe, watch, words
from ..config import EFFORTS, uncapped
from ..ledger import Ledger
from ..lifted.loopguard import LoopGuard
from ..lifted.split import split_message
from ..memory import ASSISTANT
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
VOICE_FLAG = 1 << 13  # IS_VOICE_MESSAGE: one audio attachment, no content (Discord's Message Resource, read 2026-10-22)
REPLY_MAX = 300
QUICK = 1.0           # seconds a slash command may take before its answer is deferred (Discord gives three)
RETRY = 60.0          # seconds before a channel is read again after a message the ledger could not take (disk-full drill b)
SETTLE = 30.0         # seconds another assistant's message sits with no edit or delete before it is filed (its card never is)
ANSWER_SETTLE = 6.0   # seconds its message sits with no edit before it is the answer a held message waits for
WATCH_WAIT = 150.0    # rooms.toml `watch_wait` unset: the most seconds a held message waits for that answer
CARD_PATIENCE = 3     # while that assistant's progress card is up, a held message waits at most this many `watch_wait`s
CARD_GONE = 30.0      # seconds a held message still waits once that card is deleted with no answer: its turn has ended
CARD_ROWS = tuple(e.replace("\ufe0f", "") for e in words.OPENCLAW_ROWS.split())   # U+FE0F dropped: Discord may not keep it
CARD_STEPS = re.compile(re.escape(words.SUB + words.STEP_N.format(n="")) + r"\d+")   # Yerk AI's own card in a room with people
ANSWER_MAX = 2500     # characters of the answer the room reads, its whitespace folded
SAID = (discord.MessageType.default, discord.MessageType.reply)   # what a person types; the rest are Discord's lines
LOST = (aiohttp.ClientError, OSError, asyncio.TimeoutError)        # a post that died on the connection, taken or not


class DeliveryFailed(RuntimeError):
    """A part Discord did not take: its post lost on the connection, then the one retry under its nonce failed too."""


def commands() -> list[dict[str, Any]]:
    """Exactly this install's slash commands, in the shape Discord's bulk overwrite takes. /model's argument (an account, a
    model or both) is suggested as the owner types (autocomplete, DiscordChannel._suggest), never offered as choices: a
    command's choices show to everyone in its guild, a people room's people too, a suggestion only to the one typing."""
    def pick(name: str, values: list[str] | None) -> list[dict[str, Any]]:
        return [{"type": 3, "name": name, "description": words.CHOICE[name], "required": False,
                 **({"choices": [{"name": v, "value": v} for v in values][:25]} if values else {"autocomplete": True} if values is None else {})}]
    opts = {"model": pick("model", None) + pick("account", []), "effort": pick("effort", [*EFFORTS, "default"])}
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


class VoiceFile(discord.File):
    """A voice message's one attachment with the duration_secs and waveform Discord reads beside it (its Message Resource,
    Voice Messages, read 2026-10-22); discord.py's File carries neither."""

    def __init__(self, path: str, name: str, meta: dict[str, Any]) -> None:
        super().__init__(path, filename=name)
        self.meta = meta

    def to_dict(self, index: int) -> dict[str, Any]:
        return {**super().to_dict(index), "duration_secs": self.meta["duration"], "waveform": self.meta["waveform"]}


def safe_name(name: str) -> str:
    """A file name that stays one path segment."""
    name = re.sub(r"[^\w.\- ]+", "_", Path(name).name).strip(" .") or "file"
    return name[:120]


def file_type(name: str, given: str | None = None) -> str:
    return (given or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0]


def message_text(m: Any, files: list[dict[str, Any]] | None = None, reply: str = "", reads: dict[str, str] | None = None) -> str:
    """The message body (mentions as names), with what it replies to and its files listed (staged path, or why it is missing)
    so none is dropped unseen, a voice note with the line that it is heard only through the transcribe tool, a video with the
    line that it is seen only through the watch tool, a type an installed add-on reads with the line naming its tool (`reads`,
    addons.reads). Without `files`, the attachments as Discord lists them."""
    lines = [reply, str(getattr(m, "clean_content", None) or m.content or "")]
    if files is None:
        files = [{"name": a.filename, "size": a.size, "type": file_type(a.filename, getattr(a, "content_type", None)),
                  "ok": False, "error": "not staged"} for a in getattr(m, "attachments", []) or []]
    for f in files:
        if f.get("ok"):
            lines.append(f"[attachment] {f['name']}, {f['type']}, {f['size']:,} bytes, saved at {f['path']}{shrunk(Path(f['path']))}")
            lines += [words.VOICE_NOTE.format(name=f["name"])] if transcribe.is_audio(f["name"], f["type"]) else []
            lines += [words.VIDEO_FILE.format(name=f["name"])] if watch.is_video(f["name"], f["type"]) else []
            lines += [words.ADDON_READS.format(name=f["name"], tool=t) for k, t in (reads or {}).items() if fnmatchcase(f["type"], k)][:1]
            lines += [words.FILE_TEXT.format(path=f["text"])] if f.get("text") else []
        else:
            lines.append(f"[attachment] {f['name']}: could not be fetched ({f.get('error', 'unknown')})")
    return "\n".join(x for x in lines if x).strip()


def shrunk(path: Path) -> str:
    """The [attachment] line's clause on a picture past what Read shows, pictures.EDGE pixels on its long side: its size
    off its header (pictures.size), and that zoom reads its small print; "" for any other file."""
    dims = pictures.size(path) if pictures.is_picture(path) else None
    return words.SHRUNK.format(w=dims[0], h=dims[1], edge=pictures.EDGE) if dims and max(dims) > pictures.EDGE else ""


def shown(user: Any) -> str:
    """A person's name as the server shows it: their display name, else their handle."""
    return str(getattr(user, "display_name", None) or getattr(user, "name", None) or words.A_PERSON)


def stamp(m: Any, edit: bool = False) -> float:
    """A message's own time (with `edit`, its last edit's) in epoch seconds; now, for one that carries none."""
    at: Any = (getattr(m, "edited_at", None) if edit else None) or getattr(m, "created_at", None)
    return at.timestamp() if hasattr(at, "timestamp") else time.time()


def is_card(text: str) -> bool:
    """A progress card, which says its sender is still at work: a line that starts as an OpenClaw tool row does (CARD_ROWS),
    or Yerk AI's own card in a room with people (card.py): a state mark and its holding line first, "-# step N" last."""
    lines = [x.strip() for x in str(text or "").splitlines() if x.strip()]
    if any(x.replace("\ufe0f", "").startswith(CARD_ROWS) for x in lines):
        return True
    mark = lines[0].startswith((words.WORKING, words.DONE, words.NOT_DONE)) if lines else False
    return mark and len(lines) > 1 and bool(CARD_STEPS.fullmatch(lines[-1]))


class Hold:
    """The messages taken in a room's watched channel while one waits for the answer of the assistant there: one turn
    with that answer once it settles, or with the line that it had not come once `watch_wait` (rooms.toml) runs out
    after the newest of them. Outside the room's `order` chain until then, so its own channel never waits on it."""

    def __init__(self, room: Room) -> None:
        self.room, self.bar, self.at = room, 0, 0.0          # the newest message's id and its own time: the answer comes after
        self.msgs: dict[str, tuple[Any, str, str, int]] = {}  # message id -> (message, sender, what recover adds, attempts)
        self.answer: Any = None                               # the first of the assistant's messages after `bar` to settle
        wait = getattr(room, "conf", {}).get("watch_wait", WATCH_WAIT)
        self.wait = float(wait) if isinstance(wait, (int, float)) else WATCH_WAIT
        self.timer: asyncio.TimerHandle | None = None
        self.judging: dict[str, asyncio.TimerHandle] = {}    # the assistant's message id -> its ANSWER_SETTLE wait
        self.cards: dict[str, float] = {}                     # its progress card's id -> that card's newest edit, while up
        self.gone: float | None = None                        # the wait's end once its last card went with no answer

    def due(self) -> float:
        """When the wait runs out: `wait` after the newest message, or after the newest edit of the assistant's card while one
        is up, CARD_PATIENCE waits after that message at most; once the card has gone, `gone`."""
        if self.gone is not None:
            return self.gone
        return min(max([self.at, *self.cards.values()]) + self.wait, self.at + CARD_PATIENCE * self.wait)


class DiscordChannel:
    def __init__(self, token: str, owner: int | None, test_senders: list[int], ledger: Ledger,
                 client: Any = None, guard: LoopGuard | None = None, state: Path | None = None,
                 capture: Callable[..., bool] | None = None, daemon: Any = None, taste: Callable[[str, str], Any] | None = None,
                 assistants: frozenset[int] = frozenset()):
        self.token, self.owner, self.test_senders, self.ledger = token, owner, set(test_senders), ledger
        self.assistants = assistants              # other assistants' user ids: filed where a room watches, never answered
        self.settling: dict[str, asyncio.TimerHandle] = {}   # message id -> an assistant's message waiting to settle
        self.kept: dict[str, asyncio.Future[list[dict[str, Any]]]] = {}   # an assistant's message id -> its files saved (_keep)
        self.held: dict[str, Hold] = {}           # room name -> what its watched channel's messages wait on
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
        self.order: dict[str, asyncio.Future[None]] = {}   # room name -> the last message taken, until it is in the room
        self.guard = guard or LoopGuard()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False                   # /status: the gateway said ready (yerk deploy's proof)
        self.recovered = False
        self.edits: dict[str, str] = {}           # message id -> the edit already taken (its edited_at)
        self.people: dict[int, frozenset[int]] = {}   # channel id -> the user ids answered there besides the owner
        self.known: dict[int, str] = {}           # user id -> the name the server shows them by (_meet), once fetched
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
        if self.client.user is not None and m.author.id == self.client.user.id or m.author.id in self.assistants:
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

    def inbound_text(self, room: Room, m: Any, files: list[dict[str, Any]]) -> str:
        """What the room reads of a message it takes: message_text, and from the channel it watches, a line above it naming who
        the message was sent to there (`watch_assistant`, the channel's name as the client sees it, else its id), so the room
        reads along and answers NO_REPLY unless the sender's words ask it something; with `watch_audit` true, so it grades that
        assistant's answer, never NO_REPLY, naming the sender and who reads the room (_readers). Never in its own channel."""
        text = message_text(m, files, self.reply_line(room.name, m), addons.reads(self.state))
        if m.channel.id == self.names[room.name]:
            return text
        (who, where), audit = self._there(room, m.channel), getattr(room, "conf", {}).get("watch_audit") is True
        line = (words.SENT_THERE_AUDIT if audit else words.SENT_THERE).format(who=who, channel=where, quiet=words.NO_REPLY,
                                                                             sender=shown(m.author), readers=self._readers(m.channel))
        return f"{line}\n{text}"

    def _readers(self, ch: Any) -> str:
        """Who reads the room that watches `ch`: the owner alone, else the owner and its people, each by the name the server
        shows them by once fetched (`known`, _meet), else in words."""
        if not (ids := sorted(self.people.get(ch.id, ()))):
            return words.OWNER
        names = [self.known.get(self.owner or 0, words.OWNER), *(self.known.get(i, words.A_PERSON) for i in ids)]
        return f"{', '.join(names[:-1])} and {names[-1]}"

    async def _meet(self, room: Room, ch: Any) -> None:
        """In a `watch_audit` room with people, the owner's and their names as the server shows them, each fetched once (there
        is no member cache), so its audit line says who reads the verdict; one that cannot be fetched is tried again next turn."""
        if getattr(room, "conf", {}).get("watch_audit") is not True or not self.people.get(ch.id):
            return
        for i in (x for x in (self.owner, *sorted(self.people[ch.id])) if x is not None and x not in self.known):
            try:
                self.known[i] = shown(await asyncio.wait_for(ch.guild.fetch_member(i), 5))
            except Exception as e:                # a name, never a reason to hold the turn: said in words until fetched
                log.debug("room %s: no name for %s yet: %s", room.name, i, e)

    def _there(self, room: Room, ch: Any) -> tuple[str, Any]:
        """Who the owner talks to in the channel `room` watches (`watch_assistant`), and that channel's name, else its id."""
        return (str(getattr(room, "conf", {}).get("watch_assistant") or words.OTHER_ASSISTANT),
                getattr(self.client.get_channel(ch.id) or ch, "name", None) or ch.id)

    async def on_message(self, m: Any) -> None:
        if (room := self.rooms.get(m.channel.id)) is not None:
            self.file(room.name, m)
            await self.take(room, m)

    def file(self, room: str, m: Any, edited: bool = False) -> None:
        """Every message in a room's own channel into the day's log as it lands (L55), whoever sent it, taken or not;
        in a watched one, the owner's and its people's, and another assistant's once settled. Yerk AI's own posts are filed
        where they are sent; cards never."""
        if m.author.id in self.assistants and m.channel.id != self.names[room]:
            self._offer(room, m)                  # the answer a held message waits for, once it settles
            return self._assistant(room, m)
        if m.author.id in self.assistants:        # in the room's own channel: its files saved as it lands
            self._keep(m)
        role = self.sender_of(m)              # watched: blind to anyone else there, so recall never shows their words
        if self.capture is None or m.author.id == self._me() or (role is None and m.channel.id != self.names[room]):
            return
        role = role or ("webhook" if getattr(m, "webhook_id", None) else "other")
        text = "\n".join(x for x in (self.reply_line(room, m), str(getattr(m, "clean_content", None) or m.content or "")) if x)
        files = [(a.filename, file_type(a.filename, getattr(a, "content_type", None)), int(a.size or 0))
                 for a in getattr(m, "attachments", []) or []]
        who = str(getattr(getattr(m, "author", None), "name", "") or "")      # the handle, beside the role
        self._file(room, str(m.id), role, text, files, getattr(m, "created_at", None), edited, who)

    def _assistant(self, room: str, m: Any, settled: bool = False) -> None:
        """Another assistant's message where `room` watches, filed under ASSISTANT with its handle once it has gone SETTLE
        seconds with no edit and no delete: an edit starts the wait again, a delete ends it (on_raw_message_delete). Its
        progress card (is_card) never is, settled or not, live or read from history; one edited into a plain message is, once
        that settles. Filed once by its id, an edit after that is not; read from history, at once. Its files are saved as it is
        filed (_keep)."""
        mid, at = str(m.id), getattr(m, "edited_at", None) or getattr(m, "created_at", None)
        if (old := self.settling.pop(mid, None)) is not None:
            old.cancel()
        if is_card(str(getattr(m, "content", None) or "")):
            return
        left = SETTLE - (time.time() - at.timestamp()) if at is not None and hasattr(at, "timestamp") else 0.0
        if not settled and left > 0 and self.loop is not None:
            self.settling[mid] = self.loop.call_later(left, self._assistant, room, m, True)
            return
        text = "\n".join(x for x in (self.reply_line(room, m), str(getattr(m, "clean_content", None) or m.content or "")) if x)
        files = [(a.filename, file_type(a.filename, getattr(a, "content_type", None)), int(a.size or 0))
                 for a in getattr(m, "attachments", []) or []]
        self._keep(m)
        self._file(room, mid, ASSISTANT, text, files, getattr(m, "created_at", None), False, str(getattr(m.author, "name", "") or ""))

    def _keep(self, m: Any) -> asyncio.Future[list[dict[str, Any]]] | None:
        """Another assistant's files where a room reads, saved to its inbox as a person's are (stage), once a message: a held
        group's answer before its turn reads them (_release), any other as it is filed. A miss is its ledger row and a line in
        that turn, never a notice in the room, and none is a file the room must open (Inbound.files). None: it has no files."""
        if not getattr(m, "attachments", None) or (room := self.rooms.get(m.channel.id)) is None:
            return None
        if (got := self.kept.get(mid := str(m.id))) is None:
            async def keep() -> list[dict[str, Any]]:
                try:
                    return await self.stage(room, m, say=False)
                except Exception as e:            # a record, never a reason to hold the turn it answers; the turn
                    log.exception("room %s: the files of %s's message %s not saved", room.name, getattr(m.author, "name", "?"), mid)
                    return [{"name": a.filename, "size": int(a.size or 0),   # still gets each file by name, and why
                             "type": file_type(a.filename, getattr(a, "content_type", None)),
                             "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
                            for a in getattr(m, "attachments", []) or []]
            got = self.kept[mid] = asyncio.ensure_future(keep())
            while len(self.kept) > 200:
                self.kept.pop(next(iter(self.kept)))
        return got

    def _file(self, room: str, mid: str, role: str, text: str, files: list[tuple[str, str, int]] | None = None,
              when: Any = None, edited: bool = False, who: str = "") -> None:
        if self.capture is None:
            return
        try:
            self.capture(room, mid, role, text, files or [], when=when, edited=edited, who=who)
        except OSError as e:
            log.warning("room %s: message %s not filed in the day's log: %s", room, mid, e)

    async def take(self, room: Room, m: Any, live: bool = True) -> None:
        """`live` False: read from history (catch_up), whose reader starts a held message's wait once it has read on."""
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
        if m.channel.id != self.names[room.name] and self.assistants:   # watched, an assistant there to wait for
            return self._hold(room, m, sender, live=live)
        ahead, mine = self.order.get(room.name), asyncio.get_running_loop().create_future()
        self.order[room.name] = mine              # in the room in the order taken: staging waits on a file's text, and the
        try:                                      # message sent after it must not overtake it
            await self._typing_begin(room.name, await self._channel(room.name))   # its own channel, never the watched one
            await self._react(m.channel.id, m.id, ACK)
            files = await self.stage(room, m)
            if ahead is not None:
                await asyncio.shield(ahead)
            room.submit(Inbound(str(m.id), sender, self.inbound_text(room, m, files), files=files))
        finally:
            mine.set_result(None)
            if self.order.get(room.name) is mine:
                del self.order[room.name]

    # a watched message waits for the answer of the assistant there (Hold)
    def _hold(self, room: Room, m: Any, sender: str, extra: str = "", attempt: int = 0, live: bool = True) -> None:
        """A message taken in the watched channel joins the room's open group, else opens one. The newest moves the bar the
        answer must come after, and the wait starts again from it: now when live, once history is read when not."""
        h = self.held.setdefault(room.name, Hold(room))
        h.msgs[str(m.id)] = (m, sender, extra, attempt)
        if int(m.id) > h.bar:
            h.bar, h.at, h.gone = int(m.id), stamp(m), None
            for mid in [x for x in h.judging if int(x) < h.bar]:   # said before this message: not the answer to it
                h.judging.pop(mid).cancel()
        if live:
            self._arm(room)

    def _arm(self, room: Room) -> None:
        """The open group's wait, to its end (Hold.due): one read from history after a restart waits what is left of it,
        none once it has passed."""
        if (h := self.held.get(room.name)) is not None:
            if h.timer is not None:
                h.timer.cancel()
            h.timer = asyncio.get_running_loop().call_later(max(0.0, h.due() - time.time()), self._close, h)

    def _offer(self, room: str, m: Any) -> None:
        """The watched assistant's message, new or edited, made after the open group's newest: its answer once it has gone
        ANSWER_SETTLE seconds with no edit and no delete (on_raw_message_delete), whichever settles first. Its progress card
        (is_card), however long it sits still, never is: while it is up the wait runs from its newest edit, an edit that makes
        a message one ends its judging, and one edited into a plain message is judged from that edit. Nor is a line of
        Discord's in its name (SAID); read from history, it has settled already."""
        if (h := self.held.get(room)) is None or getattr(m, "type", discord.MessageType.default) not in SAID:
            return
        if (old := h.judging.pop(mid := str(m.id), None)) is not None:
            old.cancel()
        if is_card(str(getattr(m, "content", None) or "")):
            if int(m.id) > h.bar or mid in h.cards:    # after the newest message, or up before it came: at work on the group
                h.cards[mid], h.gone = stamp(m, edit=True), None
                if h.timer is not None:                # unarmed while history is read: catch_up arms it after
                    self._arm(h.room)
            return
        self._card_gone(h, mid)
        if int(m.id) <= h.bar:
            return
        if (left := ANSWER_SETTLE - (time.time() - stamp(m, edit=True))) > 0:
            h.judging[mid] = asyncio.get_running_loop().call_later(left, self._close, h, m)
        else:
            self._close(h, m)

    def _card_gone(self, h: Hold, mid: str) -> None:
        """The assistant's card `mid` deleted, or edited into a plain message: with no other card up its turn has ended, so
        the wait is what is left of it, CARD_GONE at most, in which a reply posted around then is still judged."""
        if mid not in h.cards:
            return
        left = h.due() - time.time()
        del h.cards[mid]
        if not h.cards:
            h.gone = time.time() + max(0.0, min(left, CARD_GONE))
        if h.timer is not None:
            self._arm(h.room)

    def _close(self, h: Hold, answer: Any = None) -> None:
        """The answer settled (`answer`), the wait ran out, or the group was left empty: it leaves the hold, and one with a
        message in it joins the room's order chain as one turn."""
        if self.held.get(name := h.room.name) is not h:
            return
        del self.held[name]
        h.answer = answer
        for t in (h.timer, *h.judging.values()):
            if t is not None:
                t.cancel()
        if h.msgs:
            ahead, mine = self.order.get(name), asyncio.get_running_loop().create_future()
            self.order[name] = mine
            asyncio.ensure_future(self._release(h, ahead, mine))

    async def _release(self, h: Hold, ahead: asyncio.Future[None] | None, mine: asyncio.Future[None]) -> None:
        """The group as one turn, in its place in the order chain: the sent-to line once, each message as message_text
        renders it, then the answer line. The turn's id is its first message's; the rest follow that row (`then`)."""
        room = h.room
        try:
            await self._typing_begin(room.name, await self._channel(room.name))
            group = sorted(h.msgs.values(), key=lambda x: int(x[0].id))
            staged = [await self.stage(room, m) for m, *_ in group]
            kept = await got if h.answer is not None and (got := self._keep(h.answer)) is not None else []   # the answer's files
            if ahead is not None:
                await asyncio.shield(ahead)
            (first, _, extra, _), ids = group[0], [str(m.id) for m, *_ in group]
            await self._meet(room, first.channel)            # a watch_audit room's readers, by name, before its line is written
            text = "\n".join([self.inbound_text(room, first, staged[0]) + extra,
                              *(message_text(m, f, self.reply_line(room.name, m), addons.reads(self.state)) + x
                                for (m, _, x, _), f in zip(group[1:], staged[1:])), self._answer_line(h, first.channel, kept)])
            note = f"one turn with {', '.join(ids)}" if len(ids) > 1 else ""

            def then(state: str, reply: list[str], chars: int) -> None:   # the room's thread, as the turn ends: the rest as its first
                row = self.ledger.inbound_get(ids[0]) or {}
                for mid in ids[1:]:
                    self.ledger.inbound_set(mid, state=row.get("state") or state, answered=row.get("answered"),
                                            turn_id=row.get("turn_id"), reply_ids=",".join(reply))
                self.ledger.inbound_set(ids[0], note="; ".join(dict.fromkeys(x for x in (row.get("note"), note) if x)))   # and the room's
            for mid in ids if note else []:
                self.ledger.inbound_set(mid, note=note)
            senders = {s for _, s, _, _ in group}               # mixed: the least trusted, so no one's words ride as the owner's
            room.submit(Inbound(ids[0], next(s for s in ("person", "test-webhook", "owner") if s in senders), text,
                                attempt=max(a for *_, a in group), files=[f for fs in staged for f in fs], then=then if note else None))
        except Exception:
            log.exception("room %s: a held message from the watched channel did not reach it", room.name)
        finally:
            mine.set_result(None)
            if self.order.get(room.name) is mine:
                del self.order[room.name]

    def _answer_line(self, h: Hold, ch: Any, kept: list[dict[str, Any]] | None = None) -> str:
        """What the assistant there answered, its whitespace folded and cut at ANSWER_MAX, at its own time in the room's zone,
        with a line per file it attached (`kept`, _keep: where it was saved and its text beside it, or why it could not be
        fetched); else that it had not answered within the wait."""
        who, where = self._there(h.room, ch)
        if (a := h.answer) is None:
            return words.WATCH_UNANSWERED.format(who=who, channel=where, wait=f"{h.wait:g}")
        said = " ".join(str(getattr(a, "clean_content", None) or a.content or "").split()) or "(no text)"
        at = datetime.fromtimestamp(stamp(a), ZoneInfo(str(getattr(h.room, "conf", {}).get("timezone") or "UTC")))
        return "\n".join([words.WATCH_ANSWERED.format(who=who, at=at.strftime("%H:%M"), channel=where, text=said if len(said) <= ANSWER_MAX
                                                      else said[:ANSWER_MAX - 3] + "..."),
                          *(words.WATCH_ATTACHED.format(who=who, **f) + (f"\n{words.FILE_TEXT.format(path=f['text'])}" if f.get("text") else "")
                            if f.get("ok") else words.WATCH_NOT_KEPT.format(who=who, name=f["name"], why=f.get("error", "unknown"))
                            for f in kept or [])])

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
        if (h := self.held.get(room.name)) is not None and mid in h.msgs:     # held: its group reads it as edited
            h.msgs[mid] = (m, *h.msgs[mid][1:])
            return self.ledger.inbound_set(mid, note="edited before it ran")
        files = await self.stage(room, m)
        text = self.inbound_text(room, m, files)
        if room.revise(mid, text):
            self.ledger.inbound_set(mid, note="edited before it ran")
            return
        self.ledger.inbound_set(mid, state="received", attempts=0, note=f"edited after it ran ({row['state']})")
        await self._react(m.channel.id, m.id, ACK)
        room.submit(Inbound(mid, sender, f"[an earlier message was edited; it now reads:]\n{text}", files=files))

    async def on_raw_message_delete(self, p: Any) -> None:
        """A message deleted while it still waits (held too) never runs; one already run keeps its answer. Another assistant's,
        still settling, is never filed, nor the answer a held message waits for; once its card goes, CARD_GONE more at most."""
        room, mid = self.rooms.get(int(p.channel_id)), str(p.message_id)
        if (wait := self.settling.pop(mid, None)) is not None:
            wait.cancel()
        if (h := self.held.get(room.name) if room else None) is not None and (judged := h.judging.pop(mid, None)) is not None:
            judged.cancel()
        if h is not None:                         # its card: the wait CARD_GONE at most from now
            self._card_gone(h, mid)
        row = self.ledger.inbound_get(mid) if room else None
        if room is None or row is None:
            return
        if (h is not None and h.msgs.pop(mid, None) is not None) or room.withdraw(mid):
            self.ledger.inbound_set(mid, state="ignored", note="deleted by its sender before it ran")
            if h is not None and not h.msgs:      # held, its group left empty: no turn
                self._close(h)
        else:
            self.ledger.inbound_set(mid, note=f"deleted by its sender after it ran ({row['state']})")

    async def stage(self, room: Room, m: Any, say: bool = True) -> list[dict[str, Any]]:
        """Each attachment saved under the room's inbox with its own name, a ledger row each; a
        visible notice for any that cannot be fetched (`say` False, another assistant's: none, _keep)."""
        files: list[dict[str, Any]] = []
        texts: list[tuple[dict[str, Any], asyncio.Task[Path | None]]] = []    # each workbook's or PDF's text, read side by side
        for a in getattr(m, "attachments", []) or []:
            f, name = {"name": a.filename, "size": int(a.size or 0), "type": file_type(a.filename, a.content_type)}, safe_name(a.filename)
            try:
                dest = room.inbox(str(m.id)) / (name if all(safe_name(x["name"]) != name for x in files) else f"{len(files)}-{name}")   # a name twice: 1-image.png
                if not dest.is_file() or dest.stat().st_size != f["size"]:
                    await a.save(dest)
                f.update(ok=True, path=str(dest), size=dest.stat().st_size)
                self.ledger.attachment(message_id=str(m.id), room=room.name, name=a.filename, size=f["size"],
                                       type=f["type"], path=str(dest), state="staged")
                if extracted.kind(dest) and str(getattr(room, "conf", {}).get("privacy", "owner")) == "owner":   # a person's: never
                    texts.append((f, asyncio.ensure_future(extracted.beside(room.name, dest, self.daemon.memory.tz if self.daemon else None,
                                                                            bool(self.daemon) and uncapped(self.daemon.budgets)))))
            except (discord.HTTPException, OSError) as e:
                f.update(ok=False, error=f"{type(e).__name__}: {str(e)[:120]}")
                self.ledger.attachment(message_id=str(m.id), room=room.name, name=a.filename, size=f["size"],
                                       type=f["type"], state="missing", error=f["error"])
                if say:
                    await self._post(room.name, f"{words.NOT_DONE} {words.NOT_FETCHED.format(name=a.filename)}", "notice", [str(m.id)])
            files.append(f)
        for f, job in texts:                      # so the message waits files.WAIT once, never once a file
            if side := await job:
                f["text"] = str(side)
        return files

    async def _read(self, room: str, cid: int, mid: int, wide: bool, outside: str, gone: bool = True) -> Any:
        """Message `mid` in channel `cid`, read only where `room` reads (its own channel, the one it watches) or, `wide` (an
        owner's room), in its own server; else watch.Failed in `outside`'s words. A message not there is watch.Failed too, or
        None with `gone` False."""
        m, mine = None, getattr(self.rooms.get(cid), "name", None) == room
        try:
            if mine or wide:
                ch = self.client.get_channel(cid) or await self.client.fetch_channel(cid)
                if mine or getattr(getattr(ch, "guild", None), "id", None) == getattr((await self._channel(room)).guild, "id", 0):
                    m = await ch.fetch_message(mid)
        except discord.NotFound:
            if not gone:
                return None
            raise watch.Failed(words.WATCH_NO_MESSAGE) from None
        except discord.Forbidden:
            raise watch.Failed(words.WATCH_NOT_READABLE) from None
        except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError):
            raise watch.Failed(words.WATCH_DISCORD_DOWN) from None
        if m is None:
            raise watch.Failed(outside)
        return m

    async def files(self, room: str, at: tuple[int | None, int] | None, name: str, wide: bool) -> tuple[str, list[dict[str, Any]], str]:
        """The files an earlier message carries, for `attachments`: message `at` (its channel and id; no channel, the room's own
        channel, then the one it watches), or with none the newest message `room` posted file `name` in (the ledger's outbound
        rows); `name` keeps the one of that name. Read where _read lets the room read, each saved to the room's inbox as a
        person's files are (stage), but one past watch.MOST_BYTES. Returns the message's id, what stage gave and the names too big."""
        if at is None:
            if (row := self.ledger.posted(room, safe_name(name))) is None:
                raise watch.Failed(words.ATTACH_NOT_POSTED.format(name=name))
            at = (int(row["channel"]), int(row["message_id"]))
        cid, mid = at
        if cid is not None:
            m = await self._read(room, cid, mid, wide, words.ATTACH_NOT_ITS_CHANNEL)
        else:
            for c in sorted((c for c, r in self.rooms.items() if r.name == room), key=lambda c: c != self.names[room]):
                if (m := await self._read(room, c, mid, wide, words.ATTACH_NOT_ITS_CHANNEL, gone=False)) is not None:
                    break
            else:
                raise watch.Failed(words.ATTACH_NOT_FOUND)
        if not (given := list(getattr(m, "attachments", None) or [])):
            raise watch.Failed(words.ATTACH_NO_FILES)
        want = [a for a in given if not name or a.filename.casefold() in (name.casefold(), safe_name(name).casefold())]
        if not want:
            raise watch.Failed(words.ATTACH_NO_NAME.format(name=name, files=", ".join(a.filename for a in given)))
        big = ", ".join(a.filename for a in want if int(a.size or 0) > watch.MOST_BYTES)
        fit = [a for a in want if int(a.size or 0) <= watch.MOST_BYTES]
        kept = await self.stage(self.rooms[self.names[room]], SimpleNamespace(id=m.id, attachments=fit), say=False) if fit else []
        return str(m.id), kept, big

    async def pull(self, room: str, cid: int, mid: int, wide: bool) -> tuple[Path, str]:
        """The first video attached to message `mid` in channel `cid`, for `watch` handed its link, and the note on the rest:
        each saved to `room`'s inbox as a person's files are (stage), but one past watch.MOST_BYTES. Read only where the room
        reads (its own channel, the one it watches) or, `wide` (an owner's room), in its own server; else watch.Failed."""
        m = await self._read(room, cid, mid, wide, words.WATCH_NOT_ITS_CHANNEL)
        given = list(getattr(m, "attachments", None) or [])
        if not (videos := [a for a in given if watch.is_video(a.filename, getattr(a, "content_type", None) or "")]):
            raise watch.Failed(words.WATCH_NO_VIDEO.format(files=", ".join(a.filename for a in given)) if given else words.WATCH_NO_FILES)
        big = ", ".join(a.filename for a in videos if int(a.size or 0) > watch.MOST_BYTES)
        fit = [a for a in videos if int(a.size or 0) <= watch.MOST_BYTES]      # a save cut off is stage's miss, as a person's
        kept = await self.stage(self.rooms[self.names[room]], SimpleNamespace(id=m.id, attachments=fit), say=False) if fit else []
        if not (got := [str(f["path"]) for f in kept if f.get("ok")]):
            raise watch.Failed(words.WATCH_NOT_SAVED.format(name=kept[0]["name"], why=kept[0].get("error")) if kept else words.WATCH_TOO_BIG.format(names=big))
        more = (words.WATCH_MORE.format(n=len(got) - 1, paths=", ".join(got[1:])) if got[1:] else "", words.WATCH_TOO_BIG.format(names=big) if big else "")
        return Path(got[0]), "; ".join(x for x in more if x)

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
        try:
            async for m in ch.history(after=discord.Object(int(last)), limit=None, oldest_first=True):
                if (left := left - (self.sender_of(m) is not None)) < 0:
                    break
                self.file(room.name, m)                # as on_message: into the day's log, then taken
                await self.take(room, m, live=False)
        finally:                                       # a group read here, its answer not in what came after it: what is
            self._arm(room)                            # left of its wait

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
                await self.client.http.bulk_upsert_guild_commands(self.client.application_id, g, commands())
                log.info("discord: %d slash command(s) registered in guild %s", len(words.COMMANDS), g)
            except discord.HTTPException as e:
                log.warning("discord: slash commands not registered in guild %s: %s", g, e)

    async def on_interaction(self, it: Any) -> None:
        """A slash command: the owner's only, in a room only; anyone else, or any other channel, gets one
        ephemeral line. Off (`daemon` None): left to whatever else answers."""
        if self.daemon is not None and it.type == discord.InteractionType.autocomplete:
            return await self._suggest(it)
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

    async def _suggest(self, it: Any) -> None:
        """/model's picker as the owner types in a room's own channel (Yerkd.model_picks); anyone else, anywhere else, is offered nothing."""
        data, room = it.data or {}, self.rooms.get(int(it.channel_id or 0))
        typed = next((str(o.get("value", "")) for o in data.get("options") or [] if o.get("focused")), "")
        mine = self.owner is not None and getattr(it.user, "id", None) == self.owner and data.get("name") == "model"
        try:
            picks = self.daemon.model_picks(room.name, typed) if room and mine and self.names[room.name] == int(it.channel_id) else []
        except Exception:
            log.exception("discord: the /model suggestions failed")
            picks = []
        await it.response.autocomplete([discord.app_commands.Choice(name=n[:100], value=v[:100]) for n, v in picks])

    async def _command(self, name: str, room: str, args: dict[str, str]) -> str:
        """Each command is the daemon method the room's tool or `yerk` command calls; no second path."""
        d = self.daemon
        if name == "status":
            return "\n".join(status.card(await d.status_now({"live": True})))     # the meters asked now (usage.py)
        if name in ("model", "effort"):
            r = await (d.model_command(room, args.get("model", ""), args.get("account", "")) if name == "model"
                       else d.tool_settings(room, "", args.get("effort", ""), by_owner=True))
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
            mine = {str(m.id) for m in self.cards.values()}    # posted by this process: a turn begun before this ran
            for c in self.ledger.cards_open(room.name):        # their turns ended with the old process
                if c["kind"] == "worker" or c["message_id"] in mine:   # a worker outlives the daemon: its card stays
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
            extra = "\n[redelivered after a restart]" if attempts else ""
            if sent := self.ledger.files_sent_for([str(row["message_id"])]):   # the cut-short turn's files: in the room already
                extra += ("\n[already in the room from before the restart, not to be sent again: "
                          + "; ".join(f"file {r['file']} (message {r['message_id']})" for r in sent) + "]")
            if cid != self.names[room.name] and self.assistants:   # watched: held, its answer read from what came after it
                self._hold(room, m, str(row["sender"]), extra, attempts, live=False)
                async for x in ch.history(after=m, limit=50, oldest_first=True):
                    if self.sender_of(x) is not None:  # the sender's next message: its own row, or no part of this group
                        break
                    if x.author.id in self.assistants:
                        self._offer(room.name, x)
                continue
            files = await self.stage(room, m)
            room.submit(Inbound(str(m.id), str(row["sender"]), self.inbound_text(room, m, files) + extra, attempt=attempts, files=files))
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

    def silent(self, room: str, inbound: list[str], card: str, key: str | None = None) -> list[str]:
        """A final of exactly NO_REPLY: nothing posted; the turn's card kept and edited to `card`, its row kind `silent`."""
        return self._call(self._finish(room, "", inbound, DONE, kind="silent", card=card, key=key, instead=True)) or []

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
                    if isinstance(e, discord.NotFound):   # deleted by anyone: forgotten, never edited again (2026-11-07)
                        self.cards.pop(key, None)
                        for d in (self.shown, self.edited, self.want):
                            d.pop(key, None)
                        self.ledger.card_close(str(card.id), "found deleted at an edit")
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
            if instead and msg is None and kind == "notice":   # no card to keep: the notice is posted (a silent turn's
                instead = False                                 # line is posted as its card)
                for i, part in enumerate(split_message(text.strip(), LIMIT)):
                    self.ledger.delivery_plan(f"{key}:{i}", room, kind, part, ",".join(inbound))
            if not instead:
                try:
                    ids = await self._deliver(room, key)
                except Exception:                 # no reply: the card goes all the same, never left at its working line
                    if msg is not None:           # (seen live); a delete that fails leaves it open for the next start
                        with contextlib.suppress(discord.HTTPException, *LOST):
                            await msg.delete()
                            self.ledger.card_close(str(msg.id), "delivery failed")
                    raise
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
                    if kind == "silent" and card.split("\n")[0] == f"{words.DONE} {words.QUIET}":   # kept first, then the quiet
                        await self._supersede(room, str(msg.id))   # line before it goes: one stands; a NO_VERDICT card never
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

    async def _supersede(self, room: str, kept: str) -> None:
        """The room's quiet card before `kept`, found in the ledger (so a restart changes nothing), is deleted when the room
        said nothing after it; once anything else was said, it stays as history. The caller holds the room's lock."""
        row = self.ledger.last_outbound(room, besides=kept)
        if row is None or row["kind"] != "card-note" or str(row["body"] or "").split("\n")[0] != f"{words.DONE} {words.QUIET}":
            return
        mid = str(row["message_id"])
        try:
            await (await self._channel(room)).get_partial_message(int(mid)).delete()
        except discord.NotFound:
            pass                                  # gone already
        except discord.HTTPException as e:
            log.warning("quiet card %s in %s not removed: %s", mid, room, e)
            return                                # it still stands: its row stays kept
        self.ledger.card_close(mid, "superseded by the next quiet card")

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
        """Post every part of `key` not yet in the room, in order; the caller holds the room's lock. A post that dies on the
        connection is sent once more under the same nonce: Discord answers a nonce it already took with that message, never
        a second one (discord.py sends enforce_nonce). A part that failed, and the parts after it, are never posted again."""
        ch, ids = await self._channel(room), []
        rows = sorted((r for r in self.ledger.deliveries(room) if r["key"].rsplit(":", 1)[0] == key),
                      key=lambda r: int(r["key"].rsplit(":", 1)[1]))
        for i, r in enumerate(rows):
            if r["state"] == "sent":
                ids.append(str(r["message_id"]))
                continue
            if r["state"] == "failed":            # the turn's notice on this key posts its card, never this under it
                continue
            body = f"{REDELIVERED}\n{r['body']}" if label else str(r["body"])
            await self._debug_pause("before-post")
            for tries in (1, 2):
                try:
                    m = await ch.send(body[:MAX], nonce=nonce_for(str(r["key"])))
                    break
                except (discord.HTTPException, *LOST) as e:
                    if tries == 1 and not isinstance(e, discord.HTTPException):
                        log.warning("room %s: %s lost on the connection (%s), posting it once more under its nonce", room, r["key"], e)
                        continue
                    err = (str(e) if isinstance(e, discord.HTTPException) else f"{type(e).__name__}: {e}")[:300]
                    self.ledger.delivery_set(r["key"], state="failed", error=err, attempts=int(r["attempts"] or 0) + tries)
                    self.ledger.outbound(room=room, channel=str(ch.id), kind=r["kind"], chars=len(body), error=err, inbound=r["inbound"])
                    for x in rows[i + 1:]:        # a later call never posts the rest of a reply without this part
                        if x["state"] == "pending":
                            self.ledger.delivery_set(x["key"], state="failed", error=f"{r['key']} before it failed")
                    if tries == 1:
                        raise                     # Discord's own answer: not asked again
                    raise DeliveryFailed(f"{r['key']}: {err}") from e
            await self._debug_pause("after-post")
            self.ledger.delivery_set(r["key"], state="sent", message_id=str(m.id), sent=time.time(),
                                     attempts=int(r["attempts"] or 0) + tries, note=REDELIVERED if label else None)
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

    async def send_text(self, room: str, text: str, key: str | None = None, inbound: str = "") -> list[str]:
        """A send's rows answer no message (a redelivery marks none answered); `inbound`, its turn's, is for the page (page.Mirror)."""
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

    async def send_voice(self, room: str, path: Path, meta: dict[str, Any], key: str | None = None,
                         inbound: str = "") -> tuple[list[str], str]:
        """A note voice_note.py made, as one Discord voice message: send_file's path with the voice form, and why it went as
        the plain audio file instead when Discord refused that form or dropped its flag ("" when it went as voice)."""
        plain: list[str] = []
        ids = await self.send_file(room, path, key=key or f"v{uuid.uuid4().hex[:12]}", inbound=inbound, voice=(meta, plain))
        return ids, "; ".join(plain)

    async def _voice(self, ch: Any, path: Path, name: str, meta: dict[str, Any], nonce: str, plain: list[str]) -> Any:
        """One voice message: flag 8192, no content, the file as audio/ogg with its duration and waveform, through the bot's
        own HTTP client, since discord.py's send posts a file as application/octet-stream and Discord reads the voice fields
        only beside an audio/ type. Refused (but for size), it goes as the plain file and `plain` says why."""
        try:
            with handle_message_parameters(file=VoiceFile(str(path), name, meta), flags=discord.MessageFlags(voice=True),
                                           nonce=nonce) as params:
                for part in params.multipart or []:
                    if str(part.get("name", "")).startswith("files["):
                        part["content_type"] = "audio/ogg"
                data = await self.client.http.send_message(ch.id, params=params)
        except discord.HTTPException as e:
            if e.status == 413 or getattr(e, "code", None) == 40005:
                raise                                 # too large as voice is too large as a file: send_file says so
            plain.append(refusal(e, upload_limit(ch)))
            return await ch.send(file=discord.File(str(path), filename=name), nonce=nonce)
        if not int(data.get("flags") or 0) & VOICE_FLAG:
            plain.append(words.VOICE_PLAIN)
        return SimpleNamespace(id=int(data["id"]))

    async def send_file(self, room: str, path: Path, caption: str = "", key: str | None = None, inbound: str = "",
                        voice: tuple[dict[str, Any], list[str]] | None = None) -> list[str]:
        """The one file path: checked, sent from a snapshot under a new name, its message id
        recorded; any miss is a visible notice in the room and a ValueError for the caller. A file
        the same message already had in the room (a turn a crash cut short, run again) is not sent
        twice: its message id comes back. With `voice` (send_voice's), it is posted as a voice message."""
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
                    m = await (self._voice(ch, snap or path, name, voice[0], nonce_for(f"{key}:0"), voice[1]) if voice else
                               ch.send(content=caption[:MAX] or None, file=discord.File(str(snap or path), filename=name),
                                       nonce=nonce_for(f"{key}:0")))
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
