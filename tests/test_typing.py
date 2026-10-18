"""The Discord typing signal: from the moment a message is taken until the first card post or the
reply, refreshed while nothing is visible, never once a card exists; ledgered as nothing."""
import asyncio
import time

import yerk_ai.channels.discord as dmod
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.ledger import Ledger

from test_discord import OWNER, ROOM_CH, Chan, Client, Msg, Room


class TypingChan(Chan):
    def __init__(self, cid):
        super().__init__(cid)
        self.typed = []

    async def typing(self):
        self.typed.append(time.monotonic())


def test_typing_starts_when_the_message_is_taken_refreshes_and_stops_at_the_card_or_the_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(dmod, "TYPING_EVERY", 0.05)
    client = Client()
    ch = client.chans[ROOM_CH] = TypingChan(ROOM_CH)
    ledger = Ledger(tmp_path / "l.sqlite")
    d = DiscordChannel("token", OWNER, [], ledger, client=client, state=tmp_path)
    room = Room(tmp_path)
    d.add_room(ROOM_CH, room)

    async def go():
        d.loop = asyncio.get_running_loop()
        await d.on_message(Msg(ch, 1, "hi"))              # taken: the first typing event at once
        assert len(ch.typed) == 1 and "test-room" in d.typing and room.got
        await asyncio.sleep(0.18)                          # refreshed while nothing is visible
        n = len(ch.typed)
        assert n >= 3 and all(0.04 <= b - a <= 0.2 for a, b in zip(ch.typed, ch.typed[1:]))
        await d._open("test-room", "working on it", "turn")   # the card: the signal stops here
        assert "test-room" not in d.typing
        await asyncio.sleep(0.15)
        assert len(ch.typed) == n                          # never after the card exists
        await d.on_message(Msg(ch, 2, "and?"))             # a message while the card is up: no signal at all
        await asyncio.sleep(0.12)
        assert len(ch.typed) == n and "test-room" not in d.typing
        d.cards.pop("test-room")                           # the turn ended and its card went with the reply
        await d.on_message(Msg(ch, 3, "again"))
        await asyncio.sleep(0.07)
        m = len(ch.typed)
        assert m > n and "test-room" in d.typing
        await d._finish("test-room", "the reply", ["3"], dmod.DONE)   # the reply: the signal stops here too
        assert "test-room" not in d.typing
        await asyncio.sleep(0.12)
        assert len(ch.typed) == m
        return m
    m = asyncio.run(go())
    assert m >= 4
    # ledgered as nothing: the outbound rows are the card and the reply, no row for the typing
    assert [r["kind"] for r in ledger.rows(50, "outbound")] == ["reply", "progress"]
    assert [m.content for m in ch.sent] == ["working on it", "the reply"]


def test_a_channel_without_typing_is_left_alone(tmp_path):
    """The fake channels of the other tests have no typing(); the adapter never assumes it."""
    client = Client()
    ch = client.chans[ROOM_CH]
    d = DiscordChannel("token", OWNER, [], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path)
    d.add_room(ROOM_CH, Room(tmp_path))

    async def go():
        d.loop = asyncio.get_running_loop()
        await d.on_message(Msg(ch, 1, "hi"))
        assert d.typing == {}
    asyncio.run(go())
