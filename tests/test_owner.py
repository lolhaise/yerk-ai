"""Checks your own messages as Discord really delivers them: raw gateway payloads for a human author
through discord.py's own parsers (the objects the daemon's client dispatches), then into the
adapter. A plain message, an attachment, a mention of the bot, a reply, an edit and a delete."""
import asyncio
from types import SimpleNamespace as NS

import discord
import pytest

from yerk_ai.channels.discord import ACK, DiscordChannel
from yerk_ai.ledger import Ledger
from yerk_ai.room import Inbound, Room

from test_discord import BOT, OWNER, ROOM_CH, Client

GUILD = 33
PILOT = 10 ** 15 + BOT          # the bot: an id long enough for discord.py's mention pattern (15 to 20 digits)
MEMBER = {"roles": [], "joined_at": "2026-01-25T00:00:00+00:00", "deaf": False, "mute": False, "flags": 0}
OWNER_USER = {"id": str(OWNER), "username": "owner", "global_name": "Owner", "discriminator": "0", "avatar": None,
              "public_flags": 0}
BOT_USER = {"id": str(PILOT), "username": "Pilot", "global_name": None, "discriminator": "4321", "avatar": None,
            "bot": True}


def payload(mid, content, author=OWNER_USER, **extra):
    """A MESSAGE_CREATE (or, with edited_timestamp, MESSAGE_UPDATE) as the gateway sends it."""
    return {"type": 0, "tts": False, "timestamp": "2026-10-17T23:20:00.000000+00:00", "pinned": False,
            "mentions": [], "mention_roles": [], "mention_everyone": False, "member": MEMBER, "id": str(mid),
            "flags": 0, "embeds": [], "edited_timestamp": None, "content": content, "components": [],
            "channel_id": str(ROOM_CH), "author": author, "attachments": [], "guild_id": str(GUILD), **extra}


class Parsed:
    """discord.py's client state, fed raw events; what it dispatches is what the daemon gets."""

    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.message_content = True
        client = discord.Client(intents=intents, max_messages=None, chunk_guilds_at_startup=False,
                                member_cache_flags=discord.MemberCacheFlags.none())
        self.state, self.events = client._connection, []
        self.state.dispatch = lambda name, *args: self.events.append((name, args))
        self.state.user = discord.ClientUser(state=self.state, data=BOT_USER)
        self.state._add_guild_from_data({"id": str(GUILD), "name": "home", "features": [], "roles": [],
                                         "emojis": [], "stickers": [], "premium_tier": 1, "member_count": 2,
                                         "channels": [{"id": str(ROOM_CH), "type": 0, "name": "yerk-test",
                                                       "guild_id": str(GUILD), "position": 0,
                                                       "permission_overwrites": []}]})

    def _last(self, name):
        got = [args[0] for n, args in self.events if n == name]
        assert got, f"discord.py dispatched no {name}"
        return got[-1]

    def create(self, data):
        self.state.parse_message_create(data)
        return self._last("message")

    def update(self, data):
        self.state.parse_message_update(data)
        return self._last("raw_message_edit")

    def delete(self, mid):
        self.state.parse_message_delete({"id": str(mid), "channel_id": str(ROOM_CH), "guild_id": str(GUILD)})
        return self._last("raw_message_delete")


class FakeRoom:
    name = "test-room"

    def __init__(self, tmp):
        self.got, self.waiting, self.tmp = [], [], tmp

    def submit(self, m):
        self.got.append(m)
        return "started"

    def revise(self, mid, text):
        hit = next((m for m in self.waiting if m.id == mid), None)
        if hit:
            hit.text = text
        return hit is not None

    def withdraw(self, mid):
        hit = next((m for m in self.waiting if m.id == mid), None)
        if hit:
            self.waiting.remove(hit)
        return hit is not None

    def inbox(self, mid):
        d = self.tmp / "inbox" / mid
        d.mkdir(parents=True, exist_ok=True)
        return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    async def read(self, *, use_cached=False):          # the CDN fetch, without the network
        return b"\x89PNG" + b"0" * (self.size - 4)
    monkeypatch.setattr(discord.Attachment, "read", read)
    client = Client()
    client.user = NS(id=PILOT)
    d = DiscordChannel("token", OWNER, [], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path)
    room = FakeRoom(tmp_path)
    d.add_room(ROOM_CH, room)
    return d, client, room, Parsed()


def run(coro):
    return asyncio.run(coro)


def test_a_plain_owner_message_is_ledgered_acked_and_run(env):
    d, client, room, p = env
    m = p.create(payload(501, "what's on tomorrow?"))
    assert isinstance(m, discord.Message) and m.webhook_id is None and m.author.id == OWNER
    run(d.on_message(m))
    [got] = room.got
    assert (got.id, got.sender, got.text) == ("501", "owner", "what's on tomorrow?")
    row = d.ledger.inbound_get("501")
    assert (row["sender"], row["state"], row["chars"]) == ("owner", "received", 19)
    assert client.chans[ROOM_CH].reactions == [(501, "+", ACK)]


def test_an_owner_attachment_is_staged_under_its_own_name(env):
    d, _, room, p = env
    att = {"id": "601", "filename": "shelf photo.png", "size": 24, "content_type": "image/png", "width": 2,
           "height": 2, "url": "https://cdn.example/601/shelf.png", "proxy_url": "https://media.example/601/shelf.png"}
    run(d.on_message(p.create(payload(502, "look at this", attachments=[att]))))
    [got] = room.got
    staged = room.tmp / "inbox" / "502" / "shelf photo.png"
    assert staged.stat().st_size == 24 and got.files[0]["type"] == "image/png"
    assert got.text == f"look at this\n[attachment] shelf photo.png, image/png, 24 bytes, saved at {staged}"
    assert [(r["name"], r["state"]) for r in d.ledger.attachments("502")] == [("shelf photo.png", "staged")]


def test_a_mention_of_the_bot_reads_as_its_name_not_an_id(env):
    d, _, room, p = env
    run(d.on_message(p.create(payload(503, f"<@{PILOT}> are you there", mentions=[{**BOT_USER, "member": MEMBER}]))))
    assert room.got[0].text == "@Pilot are you there"           # was "<@id> are you there" before this run


def test_a_reply_says_which_message_it_answers(env):
    d, _, room, p = env
    earlier = payload(400, "The report is ready, 3 pages.", author=BOT_USER)
    run(d.on_message(p.create(payload(504, "send it as a pdf", type=19, mentions=[BOT_USER],
                                      message_reference={"type": 0, "message_id": "400", "channel_id": str(ROOM_CH),
                                                         "guild_id": str(GUILD)},
                                      referenced_message=earlier))))
    assert room.got[0].text == '[a reply to your message: "The report is ready, 3 pages."]\nsend it as a pdf'
    gone = payload(505, "and this?", type=19, message_reference={"type": 0, "message_id": "401",
                                                                   "channel_id": str(ROOM_CH)},
                   referenced_message=None)
    run(d.on_message(p.create(gone)))
    assert room.got[1].text.startswith("[a reply to an earlier message (401) that is no longer there]")


def test_an_edit_runs_the_new_text_once_and_an_unfurl_is_not_an_edit(env):
    d, client, room, p = env
    run(d.on_message(p.create(payload(506, "send the deck"))))
    d.ledger.inbound_set("506", state="answered")
    run(d.on_raw_message_edit(p.update(payload(506, "send the deck", embeds=[{"type": "link"}]))))   # an unfurl
    assert len(room.got) == 1
    edit = payload(506, "send the deck as a pdf", edited_timestamp="2026-10-17T23:21:00.000000+00:00")
    raw = p.update(edit)
    assert isinstance(raw, discord.RawMessageUpdateEvent) and raw.message.edited_at is not None
    run(d.on_raw_message_edit(raw))
    run(d.on_raw_message_edit(p.update(edit)))                        # the same edit again: once
    assert [(m.id, m.text) for m in room.got[1:]] == [
        ("506", "[an earlier message was edited; it now reads:]\nsend the deck as a pdf")]
    row = d.ledger.inbound_get("506")
    assert (row["state"], row["note"]) == ("received", "edited after it ran (answered)")
    assert client.chans[ROOM_CH].reactions.count((506, "+", ACK)) == 2


def test_an_edit_of_a_message_still_waiting_revises_it_before_it_runs(env):
    d, _, room, p = env
    run(d.on_message(p.create(payload(507, "book the 6pm"))))
    room.waiting.append(room.got[0])
    run(d.on_raw_message_edit(p.update(payload(507, "book the 7pm",
                                               edited_timestamp="2026-10-17T23:22:00.000000+00:00"))))
    assert len(room.got) == 1 and room.got[0].text == "book the 7pm"
    assert d.ledger.inbound_get("507")["note"] == "edited before it ran"


def test_a_delete_drops_a_waiting_message_and_keeps_an_answered_one(env):
    d, _, room, p = env
    run(d.on_message(p.create(payload(508, "ignore that"))))
    run(d.on_message(p.create(payload(509, "already answered"))))
    room.waiting.append(room.got[0])
    d.ledger.inbound_set("509", state="answered")
    raw = p.delete(508)
    assert isinstance(raw, discord.RawMessageDeleteEvent)
    run(d.on_raw_message_delete(raw))
    run(d.on_raw_message_delete(p.delete(509)))
    run(d.on_raw_message_delete(p.delete(999)))                      # never taken: nothing
    assert room.waiting == []
    assert (d.ledger.inbound_get("508")["state"], d.ledger.inbound_get("508")["note"]) == (
        "ignored", "deleted by its sender before it ran")
    assert (d.ledger.inbound_get("509")["state"], d.ledger.inbound_get("509")["note"]) == (
        "answered", "deleted by its sender after it ran (answered)")


def test_the_room_revises_and_withdraws_only_messages_still_waiting(tmp_path):
    room = Room("r", {"model": "m"}, lambda: None, [], NS(), tmp_path, Ledger(tmp_path / "l.sqlite"), None, None)
    room.queue.extend([Inbound("1", "owner", "first"), Inbound("2", "owner", "second")])
    assert room.revise("1", "first, edited") and room.queue[0].text == "first, edited"
    assert room.withdraw("2") and [m.id for m in room.queue] == ["1"]
    room.queue.clear()
    assert not room.revise("1", "late") and not room.withdraw("1")
