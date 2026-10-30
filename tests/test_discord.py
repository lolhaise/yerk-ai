"""Checks the Discord adapter, the part that carries messages between Discord and the rooms,
against a fake client. The cases: who is answered, the ledger before anything runs, the card and
attachment staging."""
import asyncio
import os
import threading
from types import SimpleNamespace as NS

import aiohttp
import discord
import pytest

import yerk_ai.channels.discord as dmod
from yerk_ai import words
from yerk_ai.channels.discord import ACK, DONE, LIMIT, REDELIVERED, DiscordChannel
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.split import fence_state_after, split_message

ROOM_CH, OTHER_CH, OWNER, PERSON, HOOK, BOT = 11, 22, 7, 31, 55, 99     # PERSON: a user the test room lists in `people`


class Msg:
    def __init__(self, ch, mid, content="", author=OWNER, webhook_id=None, attachments=(), name=""):
        self.channel, self.id, self.content, self.webhook_id = ch, mid, content, webhook_id
        self.author, self.attachments, self.edits, self.deleted, self.file = NS(id=author), list(attachments), [], False, None
        if name:
            self.author.name = name

    async def edit(self, content):
        self.edits.append(content)

    async def delete(self):
        self.deleted = True


class Partial:
    def __init__(self, ch, mid):
        self.ch, self.mid = ch, mid

    async def add_reaction(self, e):
        self.ch.reactions.append((self.mid, "+", e))

    async def remove_reaction(self, e, who):
        self.ch.reactions.append((self.mid, "-", e))

    async def delete(self):
        self.ch.deleted.append(self.mid)


class Chan:
    def __init__(self, cid):
        self.id, self.sent, self.reactions, self.guild, self.n, self.old = cid, [], [], NS(filesize_limit=100), 1000, {}
        self.deleted, self.past, self.refuse, self.fail, self.nonces = [], [], None, [], []

    async def send(self, content=None, file=None, nonce=None):
        self.nonces.append(nonce)                                    # every attempt's, a failed one's too
        if self.fail and (e := self.fail.pop(0)) is not None:        # the next sends, in order: raised, or None to go through
            raise e
        if self.refuse and file is not None:                         # Discord refusing the file only
            raise self.refuse
        self.n += 1
        m = Msg(self, self.n, content or "", author=BOT)
        m.file, m.nonce = file, nonce
        if file is not None:
            m.attachments = [NS(filename=file.filename, size=os.fstat(file.fp.fileno()).st_size,
                                data=file.fp.read())]
        self.sent.append(m)
        return m

    def get_partial_message(self, mid):
        return Partial(self, mid)

    async def fetch_message(self, mid):
        return self.old[mid]

    async def history(self, limit=100, **kw):            # as discord.py's: at most `limit` messages, None for all
        for m in self.past[:limit]:
            yield m


class Client:
    def __init__(self):
        self.chans, self.user = {ROOM_CH: Chan(ROOM_CH), OTHER_CH: Chan(OTHER_CH)}, NS(id=BOT)

    def event(self, fn):
        return fn

    def get_channel(self, cid):
        return self.chans.get(cid)


class Room:
    name = "test-room"

    def __init__(self, tmp):
        self.got, self.tmp = [], tmp

    def submit(self, m):
        self.got.append(m)
        return "started"

    def inbox(self, mid):
        d = self.tmp / "inbox" / mid
        d.mkdir(parents=True, exist_ok=True)
        return d


class Attachment:
    def __init__(self, filename, data=b"x", content_type=None, fail=False):
        self.filename, self.data, self.content_type, self.fail = filename, data, content_type, fail
        self.size = len(data)

    async def save(self, dest):
        if self.fail:
            raise OSError("cdn said no")
        dest.write_bytes(self.data)


@pytest.fixture
def dc(tmp_path, monkeypatch):
    monkeypatch.setattr(dmod, "MIN_EDIT", 0.05)
    client = Client()
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path)
    room = Room(tmp_path)
    d.add_room(ROOM_CH, room, people=frozenset({PERSON}))
    return d, client, room


def run(coro):
    return asyncio.run(coro)


def test_split_stays_under_the_limit_and_never_inside_a_code_block():
    text = "intro\n```python\n" + "\n".join(f"x = {i}" for i in range(900)) + "\n```\noutro " + "word " * 300
    chunks = split_message(text, LIMIT)
    assert len(chunks) > 2 and all(len(c) <= LIMIT for c in chunks)
    assert all(not fence_state_after(c)[0] for c in chunks)          # every chunk closes what it opens
    assert chunks[1].startswith("```python\n")                        # a split fence reopens with its tag
    assert split_message("short", LIMIT) == ["short"]
    assert LIMIT + len(REDELIVERED) + 1 <= 2000                       # a labelled part still fits


def test_only_the_room_its_owner_its_people_and_its_webhook_are_answered(dc):
    """The sender gate: the owner in every room, a listed person in the room that lists them, the test webhook. A
    stranger, the bot itself, an unlisted webhook, another channel, and a listed person in a room that does not list
    them get no turn, no reaction and no ledger row."""
    d, client, room = dc
    room_ch, other = client.chans[ROOM_CH], client.chans[OTHER_CH]
    second = Room(room.tmp)                                            # a second room, listing nobody
    second.name = "second-room"
    d.add_room(OTHER_CH, second)
    run(d.on_message(Msg(Chan(33), 1, "hi")))                          # another channel: nothing at all
    run(d.on_message(Msg(room_ch, 2, "hi", author=12345)))             # a stranger
    run(d.on_message(Msg(room_ch, 3, "hi", author=BOT)))               # itself
    run(d.on_message(Msg(room_ch, 4, "hi", webhook_id=777)))           # an unlisted webhook
    run(d.on_message(Msg(other, 9, "hi", author=PERSON)))              # a listed person, in a room that does not list them
    assert room.got == second.got == [] and other.reactions == room_ch.reactions == [] and d.ledger.rows(5, "inbound") == []
    run(d.on_message(Msg(room_ch, 5, "from owner")))
    run(d.on_message(Msg(room_ch, 6, "from hook", webhook_id=HOOK)))
    run(d.on_message(Msg(room_ch, 7, "from a listed person", author=PERSON)))
    run(d.on_message(Msg(other, 8, "the owner, in a room that lists nobody")))    # the owner: every room, listed or not
    run(d.on_message(Msg(room_ch, 5, "from owner")))                   # a duplicate delivery
    assert [(m.id, m.sender) for m in room.got] == [("5", "owner"), ("6", "test-webhook"), ("7", "person")]
    assert [(m.id, m.sender) for m in second.got] == [("8", "owner")]
    assert {r["message_id"]: (r["sender"], r["state"]) for r in d.ledger.rows(5, "inbound")} == {
        "5": ("owner", "received"), "6": ("test-webhook", "received"), "7": ("person", "received"), "8": ("owner", "received")}
    assert room_ch.reactions == [(5, "+", ACK), (6, "+", ACK), (7, "+", ACK)] and other.reactions == [(8, "+", ACK)]


def test_a_listed_person_is_never_held_by_the_webhook_loop_guard(tmp_path):
    """The loop guard counts machine-sent messages: a webhook's burst past the cap is held, a listed person's runs
    like the owner's, since a person is not a script."""
    from yerk_ai.lifted.loopguard import LoopGuard
    client = Client()
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path,
                       guard=LoopGuard(max_events=2))
    room = Room(tmp_path)
    d.add_room(ROOM_CH, room, people=frozenset({PERSON}))
    ch = client.chans[ROOM_CH]
    for i in range(3):
        run(d.on_message(Msg(ch, 10 + i, f"person {i}", author=PERSON)))
        run(d.on_message(Msg(ch, 20 + i, f"hook {i}", webhook_id=HOOK)))
    assert [m.id for m in room.got] == ["10", "20", "11", "21", "12"]           # the third webhook message is held


def test_people_is_a_list_of_discord_user_ids_or_the_start_stops(tmp_path):
    """`people` in rooms.toml: ids (TOML integers, or digits in quotes) on a room whose privacy is family or shared; unset
    or empty is the owner only; anything else is refused when rooms.toml is read, with one plain line, so a typo never
    makes a room that answers nobody, and a listed person never talks to a room that reads the owner's whole memory."""
    from yerk_ai.config import people, rooms
    assert people("r", {}) == people("r", {"people": []}) == people("r", {"people": [], "privacy": "owner"}) == frozenset()
    assert people("r", {"people": [PERSON, "7"], "privacy": "family"}) == frozenset({PERSON, 7})    # an int, digits in quotes
    for bad in (str(PERSON), [0], [1, "x"], [1.5], [True]):                                           # one string is not a list
        with pytest.raises(SystemExit, match="people must be a list of Discord user ids"):
            people("r", {"people": bad, "privacy": "family"})
    for conf in ({"people": [7]}, {"people": [7], "privacy": "owner"}):
        with pytest.raises(SystemExit, match='people needs privacy = "family" or "shared"'):
            people("r", conf)
    for more in ({"tools": ["Read", "Bash"]}, {"read": ["/srv/notes"]}):              # a shell or a read folder: past the classes
        with pytest.raises(SystemExit, match='people needs tools without "Bash" and no `read`'):
            people("r", {"people": [7], "privacy": "family", **more})
    assert people("r", {"people": [7], "privacy": "family", "tools": ["Read", "Grep", "Glob"]}) == frozenset({7})
    (tmp_path / "rooms.toml").write_text('[rooms.a]\nchannel = "discord:1"\nprivacy = "family"\npeople = "7"\n')
    with pytest.raises(SystemExit, match="rooms.toml: a: people must be a list"):
        rooms(tmp_path)


def test_card_is_edited_then_replaced_by_the_split_reply(dc):
    d, client, _ = dc
    ch = client.chans[ROOM_CH]

    async def go():
        await d._open("test-room", "working on it", "turn")
        d.want["test-room"] = "Checking the file"
        await d._edit("test-room")                                     # the first edit goes at once
        for text in ("Checking the file\n⏳ read a.md", "Checking the file\n✅ read a.md"):
            d.want["test-room"] = text                                 # later edits coalesce: the latest wins
        await asyncio.gather(d._edit("test-room"), d._edit("test-room"))
        return await d._finish("test-room", "word " * 900, ["5"], DONE, key="t1")
    ids = run(go())
    card = ch.sent[0]
    assert card.edits == ["Checking the file", "Checking the file\n✅ read a.md"] and card.deleted
    assert len(ids) == 3 and all(len(m.content) <= LIMIT for m in ch.sent[1:])
    assert ch.reactions == [(5, "-", ACK), (5, "+", DONE)]
    edits = [r["sent"] for r in sorted(d.ledger.rows(10, "outbound"), key=lambda r: r["id"]) if r["kind"] == "progress-edit"]
    assert edits[1] - edits[0] >= dmod.MIN_EDIT * 0.9                 # spaced, however many were waiting
    kinds = [(r["kind"], r["message_id"]) for r in sorted(d.ledger.rows(10, "outbound"), key=lambda r: r["id"])]
    assert kinds[:3] == [("progress", str(card.id)), ("progress-edit", str(card.id)), ("progress-edit", str(card.id))]
    assert [k for k, _ in kinds[3:]] == ["reply"] * 3
    assert [r["key"] for r in d.ledger.deliveries("test-room", "sent")] == ["t1:0", "t1:1", "t1:2"]
    assert d.ledger.cards_open("test-room") == []


def test_an_edit_that_arrives_before_the_card_is_not_lost(dc):
    d, client, _ = dc
    d.want["test-room"] = "Reading the notes"                         # the stream beat the card's post
    run(d._open("test-room", "working on it", "turn"))
    assert client.chans[ROOM_CH].sent[0].edits == ["Reading the notes"]


def test_a_key_is_posted_once_however_often_it_is_delivered(dc):
    d, client, _ = dc
    first = run(d._finish("test-room", "hello", ["5"], DONE, key="t2"))
    again = run(d._finish("test-room", "hello", ["5"], DONE, key="t2"))     # a retry of the same turn
    assert first == again and len(client.chans[ROOM_CH].sent) == 1


def test_a_crash_before_the_post_redelivers_once_labelled(dc):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    d.ledger.inbound_add("5", "test-room", str(ROOM_CH), "owner", 5)
    d.ledger.inbound_set("5", state="running", attempts=1)
    d.ledger.card_open("900", "test-room", "turn")                     # the card the old process left
    for i, part in enumerate(["part one", "part two"]):                 # planned, never posted
        d.ledger.delivery_plan(f"t3:{i}", "test-room", "reply", part, "5")
    tid = d.ledger.record(room="test-room", outcome="ok", turn_key="t3")
    run(d.recover(ROOM_CH, room))
    assert d.ledger.inbound_get("5")["turn_id"] == tid
    assert [m.content for m in ch.sent] == [f"{REDELIVERED}\npart one", f"{REDELIVERED}\npart two"]
    assert d.ledger.inbound_get("5")["state"] == "answered" and room.got == []      # answered, not run again
    assert ch.deleted == [900] and d.ledger.cards_open("test-room") == []
    run(d.recover(ROOM_CH, room))                                      # a second restart posts nothing
    assert len(ch.sent) == 2


def test_a_crash_after_the_post_finds_the_reply_and_posts_nothing(dc):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    d.ledger.inbound_add("5", "test-room", str(ROOM_CH), "owner", 5)
    d.ledger.inbound_set("5", state="running", attempts=1)
    d.ledger.delivery_plan("t4:0", "test-room", "reply", "the answer", "5")
    ch.past = [Msg(ch, 777, "the answer", author=BOT)]                  # Discord took it; the ledger never heard
    run(d.recover(ROOM_CH, room))
    assert ch.sent == [] and d.ledger.deliveries("test-room")[0]["message_id"] == "777"
    assert d.ledger.inbound_get("5")["reply_ids"] == "777"


def lost():
    """What a reply's post raised, seen live in an owner's room, in the TLS read of Discord's answer."""
    return aiohttp.ClientOSError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac (_ssl.c:2711)")


def test_a_post_that_dies_on_the_way_back_is_retried_once_with_the_same_nonce(dc):
    """A post lost on the connection, no HTTPException, goes once more inside the turn under the same nonce: Discord
    answers a nonce it already took with that message (discord.py sends enforce_nonce), so the room gets one post."""
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    run(d._open("test-room", "working on it", "turn"))
    ch.fail = [lost()]
    ids = run(d._finish("test-room", "the answer", ["5"], DONE, key="t20"))
    card, reply = ch.sent
    assert ids == [str(reply.id)] and reply.content == "the answer" and ch.nonces == [None, dmod.nonce_for("t20:0"), reply.nonce]
    [row] = d.ledger.deliveries("test-room")
    assert (row["state"], row["message_id"], row["attempts"]) == ("sent", str(reply.id), 2)
    assert card.deleted and d.ledger.cards_open("test-room") == [] and ch.reactions == [(5, "-", ACK), (5, "+", DONE)]


def test_a_post_discord_never_took_after_a_retry_fails_the_row_and_the_card_goes(dc):
    """Lost twice: DeliveryFailed, from the error; the row failed with it; the turn's card deleted and closed all the same,
    never left at its working line; nothing posted and no done mark."""
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    run(d._open("test-room", "working on it", "turn"))
    ch.fail = [lost(), lost()]
    with pytest.raises(dmod.DeliveryFailed) as raised:
        run(d._finish("test-room", "the answer", ["5"], DONE, key="t21"))
    assert isinstance(raised.value.__cause__, aiohttp.ClientOSError)
    [card], [row] = ch.sent, d.ledger.deliveries("test-room")
    assert (row["state"], row["attempts"], row["message_id"]) == ("failed", 2, None) and "SSLV3_ALERT_BAD_RECORD_MAC" in row["error"]
    assert card.deleted and card_hows(d) == {str(card.id): "delivery failed"} and ch.reactions == []


def test_a_failed_row_is_not_sent_again_by_the_turns_notice(dc):
    """room._end's notice comes on the turn's key, so its flush finds the reply's row (its own plan is ignored): a failed
    row is never posted again, and the room gets the notice's card alone, no reply under "not answered"."""
    d, client, _ = dc
    ch, card_up = client.chans[ROOM_CH], on_a_loop(d)
    card_up()
    ch.fail = [lost(), lost()]
    with pytest.raises(dmod.DeliveryFailed):
        d.reply("test-room", "the answer", ["5"], key="t22")
    owed = f"{words.NOT_DONE} {words.NOT_ANSWERED}\n{words.WHY['undelivered']}"
    assert d.notice("test-room", f"{words.NOT_DONE} {words.NOT_ANSWERED}", ["5"], card=owed, key="t22") == []
    working, notice = ch.sent
    assert working.deleted and notice.content == owed and "the answer" not in [m.content for m in ch.sent]
    assert [(r["key"], r["state"]) for r in d.ledger.deliveries("test-room")] == [("t22:0", "failed")]
    assert ch.reactions == [(5, "-", ACK), (5, "+", dmod.FAILED)]
    d.loop.call_soon_threadsafe(d.loop.stop)


def test_the_parts_after_a_failed_one_are_never_posted(dc):
    """A long reply whose second part is lost twice: the first stands, the second and third are failed, so the turn's
    notice posts its card and never the rest of a reply without its middle."""
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    ch.fail = [None, lost(), lost()]
    with pytest.raises(dmod.DeliveryFailed):
        run(d._finish("test-room", "word " * 900, ["5"], DONE, key="t24"))
    assert [(r["key"], r["state"]) for r in d.ledger.deliveries("test-room")] == [("t24:0", "sent"), ("t24:1", "failed"), ("t24:2", "failed")]
    run(d._finish("test-room", "not answered", ["5"], dmod.FAILED, kind="notice", card="the owed card", key="t24", instead=True))
    assert [m.content.split()[0] for m in ch.sent] == ["word", "the"] and len(ch.nonces) == 4


def test_a_definitive_http_error_still_fails_at_once(dc):
    """Discord's own answer (an HTTPException: it said no, nothing was lost) is not asked again: one attempt, the row failed
    with Discord's words, the error raised as it came; the card goes as on any failed delivery."""
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    run(d._open("test-room", "working on it", "turn"))
    ch.fail = [discord.Forbidden(NS(status=403, reason="Forbidden"), {"code": 50013, "message": "Missing Permissions"})]
    with pytest.raises(discord.Forbidden):
        run(d._finish("test-room", "the answer", ["5"], DONE, key="t23"))
    [card], [row] = ch.sent, d.ledger.deliveries("test-room")
    assert ch.nonces == [None, dmod.nonce_for("t23:0")]
    assert (row["state"], row["attempts"]) == ("failed", 1) and "Missing Permissions" in row["error"]
    assert card.deleted and d.ledger.cards_open("test-room") == [] and ch.reactions == []


def test_open_messages_run_again_once_then_give_up_visibly(dc):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    for mid, state, attempts in (("1", "received", 0), ("2", "running", 1), ("3", "running", 2)):
        d.ledger.inbound_add(mid, "test-room", str(ROOM_CH), "owner", 5)
        d.ledger.inbound_set(mid, state=state, attempts=attempts)
        ch.old[int(mid)] = Msg(ch, int(mid), f"message {mid}")
    run(d.recover(ROOM_CH, room))
    assert [(m.id, m.text) for m in room.got] == [("1", "message 1"), ("2", "message 2\n[redelivered after a restart]")]
    assert d.ledger.inbound_get("3")["state"] == "abandoned" and "not answered" in ch.sent[0].content


def test_a_message_sent_during_the_outage_is_run(dc):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    d.ledger.inbound_add("1", "test-room", str(ROOM_CH), "owner", 5)
    d.ledger.inbound_set("1", state="answered")
    ch.past = [Msg(ch, 2, "sent while down")]
    run(d.recover(ROOM_CH, room))
    assert [(m.id, m.text) for m in room.got] == [("2", "sent while down")]


def test_a_message_sent_during_the_outage_is_filed_once_before_it_runs(tmp_path):
    """recover's history read files each message as on_message does, then takes it: one line however often it is
    read (filed live before a crash, then read again by two restarts); from a watched channel, the owner's only."""
    from datetime import datetime, timezone

    from yerk_ai.memory import MARK, DailyLog
    log = DailyLog(tmp_path / "daily", timezone.utc)
    client = Client()
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path,
                       capture=log.capture)
    room = Room(tmp_path)
    d.add_room(ROOM_CH, room, watch=OTHER_CH)
    own, watched = client.chans[ROOM_CH], client.chans[OTHER_CH]
    for ch, mid in ((own, "1"), (watched, "2")):                       # the last message each channel ledgered
        d.ledger.inbound_add(mid, "test-room", str(ch.id), "owner", 5)
        d.ledger.inbound_set(mid, state="answered")
    own.past = [Msg(own, 10, "the owner, while it was down"), Msg(own, 11, "someone else", author=12345)]
    watched.past = [Msg(watched, 20, "the owner, in the watched channel"),
                    Msg(watched, 21, "the other side's answer", author=12345)]
    for m in own.past + watched.past:
        m.created_at = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    d.file("test-room", own.past[0])                                  # filed live, then a crash before its ledger row
    for _ in range(2):                                                 # two restarts read the same history
        for cid in (ROOM_CH, OTHER_CH):
            run(d.recover(cid, room))
        for mid in ("10", "20"):
            d.ledger.inbound_set(mid, state="answered")
    text = "".join(p.read_text() for p in (tmp_path / "daily").glob("*.md"))
    assert MARK.findall(text) == ["10", "11", "20"] and "the other side's answer" not in text
    sent = words.SENT_THERE.format(who=words.OTHER_ASSISTANT, channel=OTHER_CH, quiet=words.NO_REPLY)   # read there: marked
    assert [(m.id, m.text) for m in room.got] == [("10", "the owner, while it was down"),
                                                  ("20", f"{sent}\nthe owner, in the watched channel")]


def test_a_busy_watched_channel_never_pushes_the_owners_missed_messages_past_the_cap(tmp_path):
    """After a restart the history read is capped on the owner's missed messages, 50, not on every message: in a
    watched channel where the other side answered 60 times after the last message the room ledgered, the owner's 50
    that follow are still filed and run, in order, and a 51st is not (the cap still holds)."""
    from datetime import datetime, timezone

    from yerk_ai.memory import MARK, DailyLog
    log = DailyLog(tmp_path / "daily", timezone.utc)
    client = Client()
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path,
                       capture=log.capture)
    room = Room(tmp_path)
    d.add_room(ROOM_CH, room, watch=OTHER_CH)
    watched = client.chans[OTHER_CH]
    d.ledger.inbound_add("1", "test-room", str(OTHER_CH), "owner", 5)   # the last message it ledgered there
    d.ledger.inbound_set("1", state="answered")
    watched.past = ([Msg(watched, 100 + i, f"the other side's answer {i}", author=12345) for i in range(60)]
                    + [Msg(watched, 200 + i, f"the owner's message {i}") for i in range(51)])
    for m in watched.past:
        m.created_at = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    run(d.recover(OTHER_CH, room))
    assert [m.id for m in room.got] == [str(200 + i) for i in range(50)]
    text = "".join(p.read_text() for p in (tmp_path / "daily").glob("*.md"))
    assert MARK.findall(text) == [str(200 + i) for i in range(50)]


def test_a_channel_it_cannot_read_never_stops_the_other_rooms_recovery(tmp_path, caplog):
    """on_ready with the first room's channel answering 403 (the bot cannot see it): that room is passed over with
    one line naming it and its channel, the next room still runs what was sent while it was down, and the steps
    after recovery (the workers' and the jobs' reattach) still run."""
    client = Client()
    del client.chans[ROOM_CH]                                          # not in the cache, so fetched: Discord says 403

    async def fetch_channel(cid):
        raise discord.Forbidden(NS(status=403, reason="Forbidden"), {"code": 50001, "message": "Missing Access"})
    client.fetch_channel = fetch_channel
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path)
    first, second = Room(tmp_path), Room(tmp_path)
    second.name = "second-room"
    d.add_room(ROOM_CH, first)
    d.add_room(OTHER_CH, second)
    d.ledger.inbound_add("1", "second-room", str(OTHER_CH), "owner", 5)
    d.ledger.inbound_set("1", state="answered")
    client.chans[OTHER_CH].past = [Msg(client.chans[OTHER_CH], 2, "sent while down")]
    ran = []

    async def reattach():
        ran.append("reattach")
    d.after_recover.append(reattach)
    with caplog.at_level("WARNING", logger="yerk.discord"):
        run(d.on_ready())
    assert [(m.id, m.text) for m in second.got] == [("2", "sent while down")] and ran == ["reattach"]
    assert [r.getMessage() for r in caplog.records] == [f"room test-room: channel {ROOM_CH} not recovered after the "
                                                        "restart: 403 Forbidden (error code: 50001): Missing Access"]


def test_a_network_error_in_one_channels_recovery_never_stops_the_other_rooms(tmp_path, caplog):
    """Cold read 33 W1: discord.py 2.6.4 re-raises an OSError unwrapped (only a connection reset is retried), and a
    sqlite error or a bad ledger id is no HTTPException either, so the per-channel guard takes any Exception: the
    next room still runs what was sent while it was down, and the steps after recovery still run."""
    client = Client()
    del client.chans[ROOM_CH]

    async def fetch_channel(cid):
        raise OSError(101, "Network is unreachable")
    client.fetch_channel = fetch_channel
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path)
    first, second = Room(tmp_path), Room(tmp_path)
    second.name = "second-room"
    d.add_room(ROOM_CH, first)
    d.add_room(OTHER_CH, second)
    d.ledger.inbound_add("1", "second-room", str(OTHER_CH), "owner", 5)
    d.ledger.inbound_set("1", state="answered")
    client.chans[OTHER_CH].past = [Msg(client.chans[OTHER_CH], 2, "sent while down")]
    ran = []

    async def reattach():
        ran.append("reattach")
    d.after_recover.append(reattach)
    with caplog.at_level("WARNING", logger="yerk.discord"):
        run(d.on_ready())
    assert [(g.id, g.text) for g in second.got] == [("2", "sent while down")] and ran == ["reattach"]
    assert [r.getMessage() for r in caplog.records] == [f"room test-room: channel {ROOM_CH} not recovered after the "
                                                        "restart: [Errno 101] Network is unreachable"]


def test_files_take_one_path_checked_with_the_id_recorded(dc, tmp_path, monkeypatch):
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    monkeypatch.setattr(dmod, "BOT_UPLOAD", 100)                      # a room that takes 100 bytes
    small, big, exe = tmp_path / "a.txt", tmp_path / "b.txt", tmp_path / "c.exe"
    small.write_text("x" * 10)
    big.write_text("x" * 500)
    exe.write_text("MZ")
    ids = run(d.send_file("test-room", small, "caption", key="t5-s1"))
    assert ids == [str(ch.sent[0].id)]
    row = d.ledger.deliveries("test-room", "sent")[0]
    assert (row["key"], row["kind"], row["file"], row["message_id"], row["size"]) == ("t5-s1:0", "file", "a.txt", ids[0], 10)
    assert len(list((tmp_path / "rooms" / "test-room" / "outbox").glob("*-t5-s1-a.txt"))) == 1   # sent from a snapshot
    for path, why in ((big, "it is 500 bytes and this room takes 100 bytes"), (exe, "this room does not take .exe files"),
                      (tmp_path / "gone.txt", "there is no such file")):
        with pytest.raises(ValueError, match=why):
            run(d.send_file("test-room", path))
        assert ch.sent[-1].content == f"{words.NOT_DONE} not sent: {path.name}\n{why}"
    failed = [r for r in d.ledger.deliveries("test-room", "failed") if r["kind"] == "file"]
    assert sorted(r["file"] for r in failed) == ["b.txt", "c.exe", "gone.txt"]


def test_attachments_are_staged_and_a_missing_one_is_said_out_loud(dc):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    files = [Attachment("photo one.png", b"\x89PNG" + b"0" * 20, "image/png"), Attachment("bad.pdf", fail=True)]
    run(d.on_message(Msg(ch, 5, "look", attachments=files)))
    [got] = room.got
    staged = room.tmp / "inbox" / "5" / "photo one.png"
    assert staged.read_bytes().startswith(b"\x89PNG") and f"saved at {staged}" in got.text
    assert "bad.pdf: could not be fetched (OSError: cdn said no)" in got.text
    assert [f["ok"] for f in got.files] == [True, False] and got.files[0]["type"] == "image/png"
    rows = d.ledger._select("SELECT * FROM attachments WHERE message_id = ? ORDER BY id", ("5",))
    assert [(r["name"], r["state"], r["size"]) for r in rows] == [("photo one.png", "staged", 24), ("bad.pdf", "missing", 1)]
    assert ch.sent[0].content == f"{words.NOT_DONE} could not fetch bad.pdf\nsend it again"


def test_two_attachments_with_one_name_in_one_message_are_both_staged(dc):
    """The live shape: two pasted screenshots in one message, both named image.png by
    Discord (one webp, one png); the second was staged over the first in inbox/<message>/ and the room read one. The
    second takes a numbered name, the message names both, and the ledger has both paths."""
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    files = [Attachment("image.png", b"\x89PNG" + b"1" * 20, "image/png"), Attachment("image.png", b"RIFF" + b"2" * 30, "image/webp")]
    run(d.on_message(Msg(ch, 6, "two shots", attachments=files)))
    [got] = room.got
    first, second = room.tmp / "inbox" / "6" / "image.png", room.tmp / "inbox" / "6" / "1-image.png"
    assert first.read_bytes().startswith(b"\x89PNG") and second.read_bytes().startswith(b"RIFF")
    assert [f["path"] for f in got.files] == [str(first), str(second)] and f"saved at {second}" in got.text
    rows = d.ledger._select("SELECT * FROM attachments WHERE message_id = ? ORDER BY id", ("6",))
    assert [(r["name"], r["path"]) for r in rows] == [("image.png", str(first)), ("image.png", str(second))]


def test_two_names_that_stage_as_one_name_are_both_kept(dc):
    """safe_name makes a#.png and a!.png both a_.png: the second is numbered like a repeated name, never staged over it."""
    d, client, room = dc
    files = [Attachment("a#.png", b"1" * 10), Attachment("a!.png", b"2" * 20), Attachment("a#.png", b"3" * 30)]
    run(d.on_message(Msg(client.chans[ROOM_CH], 7, "three", attachments=files)))
    [got] = room.got
    inbox = room.tmp / "inbox" / "7"
    assert [f["path"] for f in got.files] == [str(inbox / "a_.png"), str(inbox / "1-a_.png"), str(inbox / "2-a_.png")]
    assert [(inbox / n).read_bytes()[:1] for n in ("a_.png", "1-a_.png", "2-a_.png")] == [b"1", b"2", b"3"]


def test_a_failed_turn_keeps_its_card_saying_what_was_owed(dc):
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    run(d._open("test-room", "working on it", "turn"))
    ids = run(d._finish("test-room", "No reply", ["5"], dmod.FAILED, kind="notice", card="❌ No reply to \"hi\": auth",
                        key="t6", instead=True))
    card = ch.sent[0]
    assert ids == [str(card.id)] and not card.deleted and card.edits == ["❌ No reply to \"hi\": auth"]
    assert len(ch.sent) == 1 and d.ledger.deliveries("test-room", "sent")[0]["key"] == "t6:0"


def test_a_silent_turn_keeps_its_card_saying_so_posts_nothing_and_is_ledgered_silent(dc):
    """watch-note: a final of exactly NO_REPLY (room.py) comes to the channel as `silent`: nothing is posted, the turn's card is
    kept with the line it is handed, the message gets its done mark, and the part's row is kind silent with the card's id; with
    no card up, that line is the one post (the card the turn never got), never the reply text."""
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    d.loop, line = loop, f"{words.DONE} {words.QUIET}"
    asyncio.run_coroutine_threadsafe(d._open("test-room", "working on it", "turn"), loop).result(5)
    ids = d.silent("test-room", ["5"], line, key="t7")
    card = ch.sent[0]
    assert ids == [str(card.id)] and len(ch.sent) == 1 and card.edits == [line] and not card.deleted
    assert ch.reactions == [(5, "-", ACK), (5, "+", DONE)] and d.ledger.cards_open("test-room") == []
    assert [(r["key"], r["kind"], r["state"], r["message_id"]) for r in d.ledger.deliveries("test-room")] == [
        ("t7:0", "silent", "sent", str(card.id))]
    assert d.silent("test-room", ["6"], line, key="t8") == [str(ch.sent[1].id)] and [m.content for m in ch.sent] == ["working on it", line]
    loop.call_soon_threadsafe(loop.stop)


def on_a_loop(d):
    """`d` on a loop of its own, as the daemon runs it (silent and reply wait on it from a room's thread); returns the
    call that opens a turn's card there."""
    d.loop = asyncio.new_event_loop()
    threading.Thread(target=d.loop.run_forever, daemon=True).start()
    return lambda: asyncio.run_coroutine_threadsafe(d._open("test-room", "working on it", "turn"), d.loop).result(5)


def card_hows(d):
    return {r["message_id"]: r["how"] for r in d.ledger._select("SELECT message_id, how FROM cards", ())}


def test_a_quiet_card_goes_when_the_next_one_stands(dc):
    """quiet-card: two watched messages in a row, each read with nothing to add, leave one quiet line in the room, the
    newest. The one before goes once the new one stands (deleted, its card row closed as superseded); both turns' rows
    stay as they were, kind silent and sent. Its first line is what counts: a quiet card shows its rows under it."""
    d, client, _ = dc
    ch, line, card_up = client.chans[ROOM_CH], f"{words.DONE} {words.QUIET}", on_a_loop(d)
    card_up()
    d.silent("test-room", ["5"], f"{line}\n{words.DONE} filing a note", key="t7")
    [kept] = d.ledger.deliveries("test-room")
    card_up()
    assert d.silent("test-room", ["6"], line, key="t8") == [str(ch.sent[1].id)]
    first, second = ch.sent
    assert ch.deleted == [first.id] and second.edits == [line] and not second.deleted
    assert card_hows(d) == {str(first.id): "superseded by the next quiet card", str(second.id): f"kept: {line}"}
    assert d.ledger.deliveries("test-room")[0] == kept                # the first turn's row, as it was
    assert [(r["key"], r["kind"], r["state"], r["message_id"]) for r in d.ledger.deliveries("test-room")] == [
        ("t7:0", "silent", "sent", str(first.id)), ("t8:0", "silent", "sent", str(second.id))]
    assert ch.reactions[-2:] == [(6, "-", ACK), (6, "+", DONE)]
    d.loop.call_soon_threadsafe(d.loop.stop)


def test_a_quiet_card_stays_once_the_room_said_anything_after_it(dc):
    """quiet-card: a reply between two quiet lines makes the older one history, and so does a card kept to say a turn
    went unanswered (a card-note too, its first line not the quiet one): nothing is deleted but the turn's own card the
    reply replaced, as before."""
    d, client, _ = dc
    ch, line, card_up = client.chans[ROOM_CH], f"{words.DONE} {words.QUIET}", on_a_loop(d)
    card_up()
    d.silent("test-room", ["5"], line, key="t7")
    card_up()
    d.reply("test-room", "an answer", ["6"], key="t8")
    card_up()
    d.silent("test-room", ["7"], line, key="t9")
    quiet, working, answer, last = ch.sent
    assert ch.deleted == [] and working.deleted and answer.content == "an answer" and last.edits == [line]
    card_up()
    d.notice("test-room", "No reply", ["8"], card=f"{words.NOT_DONE} {words.NOT_ANSWERED}", key="t10")
    card_up()
    d.silent("test-room", ["9"], line, key="t11")
    assert ch.deleted == [] and len(ch.sent) == 6 and not quiet.deleted and not last.deleted
    assert card_hows(d)[str(quiet.id)] == card_hows(d)[str(last.id)] == f"kept: {line}"
    d.loop.call_soon_threadsafe(d.loop.stop)


def test_a_quiet_card_from_before_a_restart_goes_when_the_next_one_stands(dc, tmp_path):
    """quiet-card: the line before is read from the ledger, never from memory: a new adapter over the same ledger and
    client (a restart) leaves the closed quiet card standing at its start, then deletes it once its own stands."""
    d, client, room = dc
    ch, line, card_up = client.chans[ROOM_CH], f"{words.DONE} {words.QUIET}", on_a_loop(d)
    card_up()
    d.silent("test-room", ["5"], line, key="t7")
    d.loop.call_soon_threadsafe(d.loop.stop)
    again = DiscordChannel("token", OWNER, [HOOK], d.ledger, client=client, state=tmp_path)
    again.add_room(ROOM_CH, room, people=frozenset({PERSON}))
    run(again.recover(ROOM_CH, room))
    assert ch.deleted == []                                            # a start removes open cards only
    card_up = on_a_loop(again)
    card_up()
    assert again.silent("test-room", ["6"], line, key="t8") == [str(ch.sent[1].id)]
    before, after = ch.sent
    assert ch.deleted == [before.id] and after.edits == [line]
    assert card_hows(d) == {str(before.id): "superseded by the next quiet card", str(after.id): f"kept: {line}"}
    again.loop.call_soon_threadsafe(again.loop.stop)


def test_a_quiet_card_already_gone_still_closes_and_one_that_will_not_go_stays_kept(dc, monkeypatch):
    """quiet-card: the delete answering NotFound (the line was removed first) ends the turn clean and closes the older
    card's row as superseded; any other refusal is a warning, the turn ends clean all the same, and the card that could
    not go keeps its row as kept, since it still stands."""
    d, client, _ = dc
    ch, line, card_up = client.chans[ROOM_CH], f"{words.DONE} {words.QUIET}", on_a_loop(d)
    answers = [discord.NotFound(NS(status=404, reason="Not Found"), {"code": 10008, "message": "Unknown Message"}),
               discord.Forbidden(NS(status=403, reason="Forbidden"), {"code": 50013, "message": "Missing Permissions"})]

    async def refused(self):
        raise answers.pop(0)
    monkeypatch.setattr(Partial, "delete", refused)
    for n in (5, 6, 7):
        card_up()
        assert d.silent("test-room", [str(n)], line, key=f"t{n}") == [str(ch.sent[-1].id)]
        assert ch.reactions[-2:] == [(n, "-", ACK), (n, "+", DONE)]
    first, second, third = ch.sent
    assert answers == [] and card_hows(d) == {str(first.id): "superseded by the next quiet card",
                                              str(second.id): f"kept: {line}", str(third.id): f"kept: {line}"}
    assert [(r["kind"], r["state"]) for r in d.ledger.deliveries("test-room")] == [("silent", "sent")] * 3
    d.loop.call_soon_threadsafe(d.loop.stop)


def plan_file(d, tmp_path, key, name="report.pdf", data=b"%PDF-1.4 drill", inbound="5", snapshot=True):
    """A file send a crash cut short: its row planned and, unless told not to, its outbox snapshot."""
    d.ledger.delivery_plan(f"{key}:0", "test-room", "file", "the report", inbound, file=name, size=len(data),
                           type="application/pdf")
    if snapshot:
        outbox = tmp_path / "rooms" / "test-room" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"20261017-231500-{key}-{name}").write_bytes(data)


def test_a_file_a_crash_left_unposted_is_sent_once_from_its_snapshot_labelled(dc, tmp_path):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    plan_file(d, tmp_path, "t7-s1")
    run(d.recover(ROOM_CH, room))
    [m] = ch.sent
    assert m.content == f"{REDELIVERED}\nthe report" and m.attachments[0].filename == "report.pdf"
    assert m.attachments[0].data == b"%PDF-1.4 drill" and m.nonce == "t7-s1:0"     # the snapshot, keyed
    row = d.ledger.deliveries("test-room")[0]
    assert (row["state"], row["message_id"], row["note"]) == ("sent", str(m.id), REDELIVERED)
    run(d.recover(ROOM_CH, room))                                      # a second restart sends nothing
    assert len(ch.sent) == 1


def test_a_file_discord_took_before_the_crash_is_found_by_name_and_size_not_sent_again(dc, tmp_path):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    plan_file(d, tmp_path, "t8-s1")
    plan_file(d, tmp_path, "t8-s2")                                    # the same file sent twice in one turn
    d.ledger.delivery_plan("t0:0", "test-room", "reply", "older", "4")
    d.ledger.delivery_set("t0:0", state="sent", message_id="776")
    posted = Msg(ch, 777, "the report", author=BOT)
    posted.attachments = [NS(filename="report.pdf", size=len(b"%PDF-1.4 drill"))]
    claimed = Msg(ch, 776, "the report", author=BOT)                   # another delivery's message: not this one
    claimed.attachments = [NS(filename="report.pdf", size=len(b"%PDF-1.4 drill"))]
    ch.past = [claimed, posted]
    run(d.recover(ROOM_CH, room))
    rows = {r["key"]: r for r in d.ledger.deliveries("test-room")}
    assert (rows["t8-s1:0"]["message_id"], rows["t8-s1:0"]["note"]) == ("777", "found in the room after a restart")
    assert [m.content for m in ch.sent] == [f"{REDELIVERED}\nthe report"]         # only the second goes again
    assert rows["t8-s2:0"]["message_id"] == str(ch.sent[0].id)


def test_a_file_whose_snapshot_is_gone_is_said_out_loud(dc, tmp_path):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    plan_file(d, tmp_path, "t9-s1", snapshot=False)
    run(d.recover(ROOM_CH, room))
    [m] = ch.sent
    assert m.attachments == [] and m.content.startswith(f"{words.NOT_DONE} not resent: report.pdf\n")
    assert "its saved copy is gone" in m.content
    assert d.ledger.deliveries("test-room", "failed")[0]["key"] == "t9-s1:0"


def test_a_message_run_again_is_told_its_file_is_in_the_room_and_the_file_is_not_sent_twice(dc, tmp_path):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    d.ledger.inbound_add("5", "test-room", str(ROOM_CH), "owner", 5)
    d.ledger.inbound_set("5", state="running", attempts=1)
    ch.old[5] = Msg(ch, 5, "send me the report")
    plan_file(d, tmp_path, "t10-s1")
    run(d.recover(ROOM_CH, room))
    sent_id = str(ch.sent[0].id)
    [again] = room.got
    assert again.text.endswith(f"[already in the room from before the restart, not to be sent again: "
                               f"file report.pdf (message {sent_id})]")
    src = tmp_path / "report.pdf"
    src.write_bytes(b"%PDF-1.4 drill")
    ids = run(d.send_file("test-room", src, "the report", key="t11-s1", inbound="5"))   # the run again sends it
    assert ids == [sent_id] and len(ch.sent) == 1


def test_the_upload_limit_is_what_the_bot_was_measured_to_send(dc, tmp_path):
    """The source of the number: the live probe of 2026-10-17 (20,971,520 bytes sent through the
    bot, 20,971,521 refused 413 / 40005), not discord.py's boost table (10,485,760 at tier 1)."""
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    assert dmod.BOT_UPLOAD == 20_971_520
    ch.guild = NS(filesize_limit=discord.Guild._PREMIUM_GUILD_LIMITS[1].filesize)
    assert ch.guild.filesize_limit == 10_485_760 and dmod.upload_limit(ch) == 20_971_520
    assert dmod.upload_limit(NS(guild=NS(filesize_limit=52_428_800))) == 52_428_800     # a tier 2 guild: the table
    over, edge = tmp_path / "over.bin", tmp_path / "edge.bin"
    with open(over, "wb") as f:
        f.truncate(20_971_521)
    with open(edge, "wb") as f:
        f.truncate(20_971_520)
    assert d.file_problem(ch, edge) is None
    assert d.file_problem(ch, over) == "it is 20,971,521 bytes and this room takes 20,971,520 bytes"
    small = tmp_path / "small.txt"
    small.write_text("x")
    ch.refuse = discord.HTTPException(NS(status=413, reason="Payload Too Large"),
                                      {"code": 40005, "message": "Request entity too large"})
    with pytest.raises(ValueError, match="too large"):
        run(d.send_file("test-room", small, key="t12-s1"))
    [notice] = ch.sent
    assert notice.content == f"{words.NOT_DONE} not sent: small.txt\nDiscord said too large (limit 20 MB)"
    assert "413" not in notice.content and "40005" not in notice.content          # the status code is the log's


def test_every_message_in_the_room_is_filed_in_the_days_log_once(tmp_path, monkeypatch):
    from datetime import timezone

    from yerk_ai.memory import DailyLog
    monkeypatch.setattr(dmod, "MIN_EDIT", 0.05)
    log = DailyLog(tmp_path / "memory" / "daily", timezone.utc)
    client = Client()
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path,
                       capture=log.capture)
    room = Room(tmp_path)
    d.add_room(ROOM_CH, room, people=frozenset({PERSON}))
    ch = client.chans[ROOM_CH]
    run(d.on_message(Msg(ch, 31, "a longer line from the owner", name="owner_handle")))
    run(d.on_message(Msg(ch, 32, "", webhook_id=HOOK, attachments=[Attachment("photo.png", b"x" * 1234, "image/png")])))
    run(d.on_message(Msg(ch, 33, "k")))
    run(d.on_message(Msg(ch, 34, "a person who is not the owner", author=12345)))
    run(d.on_message(Msg(ch, 36, "a listed person, named by handle as the owner is", author=PERSON, name="mom_handle")))
    run(d.on_message(Msg(ch, 35, "the bot's own post, filed where it is sent", author=BOT)))
    run(d.on_message(Msg(ch, 31, "a longer line from the owner")))                 # delivered twice
    run(d.send_text("test-room", "a reply from Yerk"))
    text = "".join(p.read_text() for p in (tmp_path / "memory" / "daily").glob("*.md"))
    lines = [x for x in text.splitlines() if x.startswith("- ")]
    assert [x.split(": ", 1)[0].split(" ", 2)[2] for x in lines] == [
        "test-room owner@owner_handle", "test-room test-webhook", "test-room owner", "test-room other",
        "test-room person@mom_handle", "test-room yerk"]
    assert text.count("[m:31]") == 1 and "[m:35]" not in text and [m.id for m in room.got] == ["31", "32", "33", "36"]
    assert "(attachment only) [c:private] [m:32]\n  - attachment: photo.png, image/png, 1,234 bytes" in text
    assert " owner: k [c:private] [m:33]" in text and " yerk: a reply from Yerk [c:private] [m:" in text
