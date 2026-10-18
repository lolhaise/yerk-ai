"""Checks the Discord adapter, the part that carries messages between Discord and the rooms,
the card and the split reply, redelivery after a crash, the one file path."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from yerk_ai.channels.discord import ACK, DONE, LIMIT, DiscordChannel
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.split import fence_state_after, split_message

ROOM_CH, OTHER_CH, OWNER, HOOK, BOT = 11, 22, 7, 55, 99


class Msg:
    def __init__(self, ch, mid, content="", author=OWNER, webhook_id=None):
        self.channel, self.id, self.content, self.webhook_id = ch, mid, content, webhook_id
        self.author, self.attachments, self.edits, self.deleted, self.file = NS(id=author), [], [], False, None

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


class Chan:
    def __init__(self, cid):
        self.id, self.sent, self.reactions, self.guild, self.n, self.old = cid, [], [], NS(filesize_limit=100), 1000, {}

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
        for m in []:
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

    def __init__(self):
        self.got = []

    def submit(self, m):
        self.got.append(m)
        return "started"


@pytest.fixture
def dc(tmp_path):
    client = Client()
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client)
    room = Room()
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
    run(d._progress("test-room", "working on it"))
    run(d._progress("test-room", "Checking the file"))
    card = ch.sent[0]
    ids = run(d._finish("test-room", "word " * 900, ["5"], DONE))
    assert card.edits == ["Checking the file"] and card.deleted
    assert len(ids) == 3 and all(len(m.content) <= LIMIT for m in ch.sent[1:])
    assert ch.reactions == [(5, "-", ACK), (5, "+", DONE)]
    kinds = [(r["kind"], r["message_id"]) for r in sorted(d.ledger.rows(10, "outbound"), key=lambda r: r["id"])]
    assert kinds[0] == ("progress", str(card.id)) and [k for k, _ in kinds[1:]] == ["reply"] * 3


def test_a_crash_redelivers_once_then_gives_up_visibly(dc):
    d, client, room = dc
    ch = client.chans[ROOM_CH]
    for mid, state, attempts in (("1", "received", 0), ("2", "running", 1), ("3", "running", 2)):
        d.ledger.inbound_add(mid, "test-room", str(ROOM_CH), "owner", 5)
        d.ledger.inbound_set(mid, state=state, attempts=attempts)
        ch.old[int(mid)] = Msg(ch, int(mid), f"message {mid}")
    run(d.recover(ROOM_CH, room))
    assert [(m.id, m.text) for m in room.got] == [("1", "message 1"), ("2", "message 2\n[redelivered after a restart]")]
    assert d.ledger.inbound_get("3")["state"] == "abandoned" and "not answered" in ch.sent[0].content


def test_files_take_one_path_with_the_id_recorded(dc, tmp_path):
    d, client, _ = dc
    small, big = tmp_path / "a.txt", tmp_path / "b.txt"
    small.write_text("x" * 10)
    big.write_text("x" * 500)
    ids = run(d.send_file("test-room", small, "caption"))
    assert ids == [str(client.chans[ROOM_CH].sent[0].id)]
    row = d.ledger.rows(1, "outbound")[0]
    assert (row["kind"], row["file"], row["message_id"]) == ("file", "a.txt", ids[0])
    with pytest.raises(ValueError):
        run(d.send_file("test-room", big))
    with pytest.raises(FileNotFoundError):
        run(d.send_file("test-room", tmp_path / "missing.txt"))
