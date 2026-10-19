"""Checks the Discord adapter, the part that carries messages between Discord and the rooms,
against a fake client. The cases: who is answered, the ledger before anything runs, the card and
attachment staging."""
import asyncio
import os
from types import SimpleNamespace as NS

import discord
import pytest

import yerk_ai.channels.discord as dmod
from yerk_ai import words
from yerk_ai.channels.discord import ACK, DONE, LIMIT, REDELIVERED, DiscordChannel
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.split import fence_state_after, split_message

ROOM_CH, OTHER_CH, OWNER, HOOK, BOT = 11, 22, 7, 55, 99


class Msg:
    def __init__(self, ch, mid, content="", author=OWNER, webhook_id=None, attachments=()):
        self.channel, self.id, self.content, self.webhook_id = ch, mid, content, webhook_id
        self.author, self.attachments, self.edits, self.deleted, self.file = NS(id=author), list(attachments), [], False, None

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
        self.deleted, self.past, self.refuse = [], [], None

    async def send(self, content=None, file=None, nonce=None):
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
    d.add_room(ROOM_CH, room)
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


def test_only_the_room_its_owner_and_its_webhook_are_answered(dc):
    d, client, room = dc
    room_ch, other = client.chans[ROOM_CH], client.chans[OTHER_CH]
    run(d.on_message(Msg(other, 1, "hi")))                            # another channel: nothing at all
    run(d.on_message(Msg(room_ch, 2, "hi", author=12345)))             # a stranger
    run(d.on_message(Msg(room_ch, 3, "hi", author=BOT)))               # itself
    run(d.on_message(Msg(room_ch, 4, "hi", webhook_id=777)))           # an unlisted webhook
    assert room.got == [] and other.reactions == room_ch.reactions == [] and d.ledger.rows(5, "inbound") == []
    run(d.on_message(Msg(room_ch, 5, "from owner")))
    run(d.on_message(Msg(room_ch, 6, "from hook", webhook_id=HOOK)))
    run(d.on_message(Msg(room_ch, 5, "from owner")))                   # a duplicate delivery
    assert [(m.id, m.sender) for m in room.got] == [("5", "owner"), ("6", "test-webhook")]
    assert {r["message_id"]: r["state"] for r in d.ledger.rows(5, "inbound")} == {"5": "received", "6": "received"}
    assert room_ch.reactions == [(5, "+", ACK), (6, "+", ACK)]


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
    assert [(m.id, m.text) for m in room.got] == [("10", "the owner, while it was down"),
                                                  ("20", "the owner, in the watched channel")]


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
    rows = d.ledger.attachments("5")
    assert [(r["name"], r["state"], r["size"]) for r in rows] == [("photo one.png", "staged", 24), ("bad.pdf", "missing", 1)]
    assert ch.sent[0].content == f"{words.NOT_DONE} could not fetch bad.pdf\nsend it again"


def test_a_failed_turn_keeps_its_card_saying_what_was_owed(dc):
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    run(d._open("test-room", "working on it", "turn"))
    ids = run(d._finish("test-room", "No reply", ["5"], dmod.FAILED, kind="notice", card="❌ No reply to \"hi\": auth",
                        key="t6", instead=True))
    card = ch.sent[0]
    assert ids == [str(card.id)] and not card.deleted and card.edits == ["❌ No reply to \"hi\": auth"]
    assert len(ch.sent) == 1 and d.ledger.deliveries("test-room", "sent")[0]["key"] == "t6:0"


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
    d.add_room(ROOM_CH, room)
    ch = client.chans[ROOM_CH]
    run(d.on_message(Msg(ch, 31, "a longer line from the owner")))
    run(d.on_message(Msg(ch, 32, "", webhook_id=HOOK, attachments=[Attachment("photo.png", b"x" * 1234, "image/png")])))
    run(d.on_message(Msg(ch, 33, "k")))
    run(d.on_message(Msg(ch, 34, "a person who is not the owner", author=12345)))
    run(d.on_message(Msg(ch, 35, "the bot's own post, filed where it is sent", author=BOT)))
    run(d.on_message(Msg(ch, 31, "a longer line from the owner")))                 # delivered twice
    run(d.send_text("test-room", "a reply from Yerk"))
    text = "".join(p.read_text() for p in (tmp_path / "memory" / "daily").glob("*.md"))
    lines = [x for x in text.splitlines() if x.startswith("- ")]
    assert [x.split(": ", 1)[0].split(" ", 2)[2] for x in lines] == [
        "test-room owner", "test-room test-webhook", "test-room owner", "test-room other", "test-room yerk"]
    assert text.count("[m:31]") == 1 and "[m:35]" not in text
    assert "(attachment only) [c:private] [m:32]\n  - attachment: photo.png, image/png, 1,234 bytes" in text
    assert " owner: k [c:private] [m:33]" in text and " yerk: a reply from Yerk [c:private] [m:" in text
