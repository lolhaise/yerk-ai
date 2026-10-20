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

from test_discord import BOT, HOOK, OTHER_CH, OWNER, PERSON, ROOM_CH, Client

GUILD = 33
PILOT = 10 ** 15 + BOT          # the bot: an id long enough for discord.py's mention pattern (15 to 20 digits)
MEMBER = {"roles": [], "joined_at": "2026-01-25T00:00:00+00:00", "deaf": False, "mute": False, "flags": 0}
OWNER_USER = {"id": str(OWNER), "username": "owner", "global_name": "Owner", "discriminator": "0", "avatar": None,
              "public_flags": 0}
PERSON_USER = {**OWNER_USER, "id": str(PERSON), "username": "mom", "global_name": "Mom"}     # listed in the room's `people`
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
                                         "channels": [{"id": str(ch), "type": 0, "name": name, "guild_id": str(GUILD),
                                                       "position": 0, "permission_overwrites": []}
                                                      for ch, name in ((ROOM_CH, "yerk-test"), (OTHER_CH, "watched"))]})

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
    d.add_room(ROOM_CH, room, people=frozenset({PERSON}))
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
    assert [(r["name"], r["state"]) for r in d.ledger._select("SELECT * FROM attachments WHERE message_id = ? ORDER BY id", ("502",))] == [("shelf photo.png", "staged")]


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


def test_a_reply_in_a_watched_channel_keeps_that_it_is_one_and_drops_the_other_sides_words(tmp_path):
    """In the queue, for its: turn and the last (#part-closes, the Case Before the next) run starts
    reply to the other side's message says it is a reply and never quotes it, in the turn, in its edit and in the
    day's log; a reply there to the owner's own message or a test sender's still quotes it, and the room's own
    channel quotes whoever it answers, as before."""
    from datetime import timezone

    from yerk_ai.memory import DailyLog
    client = Client()
    client.user = NS(id=PILOT)
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path,
                       capture=DailyLog(tmp_path / "daily", timezone.utc).capture)
    room, p = FakeRoom(tmp_path), Parsed()
    d.add_room(ROOM_CH, room, watch=OTHER_CH)
    ben = {"id": "4321", "username": "Ben", "global_name": "Ben", "discriminator": "0", "avatar": None, "bot": True}
    hook = {"id": str(HOOK), "username": "test sender", "discriminator": "0000", "avatar": None, "bot": True}

    def reply(mid, text, to, ch=OTHER_CH, **extra):
        ref = {"type": 0, "message_id": to["id"], "channel_id": str(ch), "guild_id": str(GUILD)}
        return payload(mid, text, type=19, channel_id=str(ch), message_reference=ref, referenced_message=to, **extra)
    theirs = payload(600, "Drop to 4 sets and add a back-off set", author=ben, channel_id=str(OTHER_CH))
    mine = payload(602, "squats felt heavy", channel_id=str(OTHER_CH))
    tester = payload(604, "a test sender's line", author=hook, webhook_id=str(HOOK), channel_id=str(OTHER_CH))
    run(d.on_message(p.create(reply(601, "ok but why", theirs))))
    run(d.on_message(p.create(reply(603, "and the bench?", mine))))
    run(d.on_message(p.create(reply(605, "noted", tester))))
    run(d.on_message(p.create(reply(607, "why that one", payload(606, "the deck is ready", author=ben), ch=ROOM_CH))))
    run(d.on_raw_message_edit(p.update(reply(601, "ok but why that many", theirs,
                                             edited_timestamp="2026-10-17T23:21:00.000000+00:00"))))
    blind = "[a reply to an earlier message]"
    assert [(m.id, m.text) for m in room.got] == [
        ("601", f"{blind}\nok but why"),
        ("603", '[a reply to Owner\'s message: "squats felt heavy"]\nand the bench?'),
        ("605", '[a reply to test sender\'s message: "a test sender\'s line"]\nnoted'),
        ("607", '[a reply to Ben\'s message: "the deck is ready"]\nwhy that one'),
        ("601", f"[an earlier message was edited; it now reads:]\n{blind}\nok but why that many")]
    days = "".join(f.read_text() for f in (tmp_path / "daily").glob("*.md"))
    assert [x.split(" ", 3)[3].rsplit(" [c:", 1)[0] for x in days.splitlines() if x.startswith("- ")] == [
        f"owner@owner: {blind} / ok but why",
        'owner@owner: [a reply to Owner\'s message: "squats felt heavy"] / and the bench?',
        'owner@owner: [a reply to test sender\'s message: "a test sender\'s line"] / noted',
        'owner@owner: [a reply to Ben\'s message: "the deck is ready"] / why that one',
        f"owner@owner: (edited) {blind} / ok but why that many"]


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


def test_a_listed_person_is_answered_and_their_edits_and_deletes_reach_only_their_own_messages(env):
    """A person in the room's `people`, on the real inbound shape: their message is ledgered as sender person, acked
    and run; their edit revises their own waiting message; an edit event for the owner's waiting message whose author
    is the person (a shape Discord never sends, since only an author edits) changes nothing; a delete drops the waiting
    message it names. Discord's delete event carries no deleter, so whose hand it was is left to Discord's permissions."""
    d, client, room, p = env
    run(d.on_message(p.create(payload(701, "mom here", author=PERSON_USER))))
    run(d.on_message(p.create(payload(702, "the owner's, waiting"))))
    assert [(m.id, m.sender) for m in room.got] == [("701", "person"), ("702", "owner")]
    assert d.ledger.inbound_get("701")["sender"] == "person" and client.chans[ROOM_CH].reactions == [(701, "+", ACK), (702, "+", ACK)]
    room.waiting.extend(room.got)
    run(d.on_raw_message_edit(p.update(payload(701, "mom here, edited", author=PERSON_USER,
                                               edited_timestamp="2026-10-17T23:22:00.000000+00:00"))))
    assert room.got[0].text == "mom here, edited" and d.ledger.inbound_get("701")["note"] == "edited before it ran"
    run(d.on_raw_message_edit(p.update(payload(702, "not the owner's words", author=PERSON_USER,
                                               edited_timestamp="2026-10-17T23:23:00.000000+00:00"))))
    assert len(room.got) == 2 and room.got[1].text == "the owner's, waiting" and not d.ledger.inbound_get("702")["note"]
    run(d.on_raw_message_delete(p.delete(701)))
    assert [m.id for m in room.waiting] == ["702"] and d.ledger.inbound_get("701")["state"] == "ignored"


def test_the_room_revises_and_withdraws_only_messages_still_waiting(tmp_path):
    room = Room("r", {"model": "m"}, lambda: None, [], NS(), tmp_path, Ledger(tmp_path / "l.sqlite"), None, None)
    room.queue.extend([Inbound("1", "owner", "first"), Inbound("2", "owner", "second")])
    assert room.revise("1", "first, edited") and room.queue[0].text == "first, edited"
    assert room.withdraw("2") and [m.id for m in room.queue] == ["1"]
    room.queue.clear()
    assert not room.revise("1", "late") and not room.withdraw("1")
