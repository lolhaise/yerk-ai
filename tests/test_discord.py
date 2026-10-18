"""Checks the Discord adapter, the part that carries messages between Discord and the rooms,
against a fake client. The cases: who is answered, the ledger before anything runs, the card and
attachment staging."""
import asyncio
from types import SimpleNamespace as NS

import pytest

import yerk_ai.channels.discord as dmod
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
        self.deleted, self.past = [], []

    async def send(self, content=None, file=None):
        self.n += 1
        m = Msg(self, self.n, content or "", author=BOT)
        m.file = file
        self.sent.append(m)
        return m

    def get_partial_message(self, mid):
        return Partial(self, mid)

    async def fetch_message(self, mid):
        return self.old[mid]

    async def history(self, **kw):
        for m in self.past:
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
    kinds = [(r["kind"], r["message_id"]) for r in sorted(d.ledger.rows(10, "outbound"), key=lambda r: r["id"])]
    assert kinds[:3] == [("progress", str(card.id)), ("progress-edit", str(card.id)), ("progress-edit", str(card.id))]
    assert [k for k, _ in kinds[3:]] == ["reply"] * 3
    assert [r["key"] for r in d.ledger.deliveries("test-room", "sent")] == ["t1:0", "t1:1", "t1:2"]
    assert d.ledger.cards_open("test-room") == []


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
    run(d.recover(ROOM_CH, room))
    assert [m.content for m in ch.sent] == [f"{REDELIVERED} part one", f"{REDELIVERED} part two"]
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


def test_files_take_one_path_checked_with_the_id_recorded(dc, tmp_path):
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    small, big, exe = tmp_path / "a.txt", tmp_path / "b.txt", tmp_path / "c.exe"
    small.write_text("x" * 10)
    big.write_text("x" * 500)
    exe.write_text("MZ")
    ids = run(d.send_file("test-room", small, "caption", key="t5-s1"))
    assert ids == [str(ch.sent[0].id)]
    row = d.ledger.deliveries("test-room", "sent")[0]
    assert (row["key"], row["kind"], row["file"], row["message_id"], row["size"]) == ("t5-s1:0", "file", "a.txt", ids[0], 10)
    assert len(list((tmp_path / "rooms" / "test-room" / "outbox").glob("*-t5-s1-a.txt"))) == 1   # sent from a snapshot
    for path, why in ((big, "this room takes 100"), (exe, ".exe files are not sent"), (tmp_path / "gone.txt", "no such file")):
        with pytest.raises(ValueError, match=why):
            run(d.send_file("test-room", path))
        assert ch.sent[-1].content.startswith(f"Could not send {path.name}: ") and why in ch.sent[-1].content
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
    assert ch.sent[0].content == "could not fetch bad.pdf"


def test_a_failed_turn_keeps_its_card_saying_what_was_owed(dc):
    d, client, _ = dc
    ch = client.chans[ROOM_CH]
    run(d._open("test-room", "working on it", "turn"))
    ids = run(d._finish("test-room", "No reply", ["5"], dmod.FAILED, kind="notice", card="❌ No reply to \"hi\": auth",
                        key="t6", instead=True))
    card = ch.sent[0]
    assert ids == [str(card.id)] and not card.deleted and card.edits == ["❌ No reply to \"hi\": auth"]
    assert len(ch.sent) == 1 and d.ledger.deliveries("test-room", "sent")[0]["key"] == "t6:0"
