"""A room that watches a channel (rooms.toml `watch`): it reads that channel as its own and never writes to
it. The proof runs the daemon's own wiring and a real room on the fake Claude against fake channels that
record every call that writes; the watched one must end with none. Then the config refusal, and the
`yerk map` and `yerk status` lines."""
import asyncio
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import yerk_ai.channels.discord as dmod
from yerk_ai import cli, status, words
from yerk_ai.accounts import Account, Cooldowns
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import EXAMPLE_PRIVATE, rooms
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.room import Inbound

from test_discord import Attachment, Chan, Msg
from test_observe import Timers, busy_room
from test_slash import It, wait

FAKE = Path(__file__).with_name("fake_claude.py")
ROOM, OWN, WATCHED, OWNER, BOT = "example-room", 11, 33, 7, 99


class Spy(Chan):
    """A channel that records every call that writes to it: a post, the typing signal, and any message it is
    asked for (a reaction, an edit or a delete goes through one). Reads (history, a fetch) are not writes."""
    def __init__(self, cid):
        super().__init__(cid)
        self.guild, self.writes = NS(id=1, filesize_limit=0), []

    async def send(self, content=None, file=None, nonce=None):
        self.writes.append(("send", content))
        return await super().send(content, file, nonce)

    async def typing(self):
        self.writes.append(("typing", None))

    def get_partial_message(self, mid):
        self.writes.append(("message", mid))
        return super().get_partial_message(mid)


class Bot:
    """discord.Client's stand-in: the two channels, and the command endpoint on_ready registers through."""
    def __init__(self, *chans):
        self.chans, self.user, self.application_id, self.puts = {c.id: c for c in chans}, NS(id=BOT), 4242, []
        self.http = NS(bulk_upsert_guild_commands=self._put)

    async def _put(self, app, guild, payload):
        self.puts.append(guild)

    def event(self, fn):
        return fn

    def get_channel(self, cid):
        return self.chans.get(cid)


def layer(tmp_path, watch=f"discord:{WATCHED}"):
    """The example layer with its room in OWN, watching `watch`, and the [discord] table yerkd reads."""
    priv = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    toml = priv / "rooms.toml"
    toml.write_text(toml.read_text().replace('"discord:CHANNEL_ID"', f'"discord:{OWN}"\nwatch = "{watch}"', 1))
    (priv / "discord.token").write_text("not-a-token\n")
    (priv / "yerkd.toml").write_text(f'[discord]\ntoken_file = "discord.token"\nowner = {OWNER}\n')
    return priv


def test_a_watched_channel_is_read_as_the_rooms_own_and_never_written_to(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setattr(dmod, "MIN_EDIT", 0.05)
    own, watched = Spy(OWN), Spy(WATCHED)
    bot = Bot(own, watched)
    (tmp_path / "state").mkdir()
    d = Yerkd(layer(tmp_path), tmp_path / "state", only_account=Account("one", tmp_path / "cfg1"))

    async def at_once(*a, **kw):
        return None
    monkeypatch.setattr(dmod.discord, "Client", lambda **kw: bot)
    monkeypatch.setattr(DiscordChannel, "start", at_once)
    for name in ("serve_http", "reap"):
        monkeypatch.setattr(d, name, at_once)
    monkeypatch.setattr(d.beat, "tick", at_once)
    monkeypatch.setattr(d.workers, "begin", lambda loop: None)
    asyncio.run(d.run_discord())                      # yerkd's own wiring, from rooms.toml
    dc, room = d.link, d.rooms[ROOM]
    wired = dc.rooms.get(WATCHED) is room and dc.names == {ROOM: OWN}
    dc.rooms.setdefault(WATCHED, room)                # a tree with no `watch`: mapped by hand, so the proof shows its leaks
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    dc.loop = loop

    def go(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(30)

    def answered(text):
        wait(lambda: any(text in m.content for m in own.sent) and not room.busy)
    # a turn: the owner's message in the watched channel with a file that cannot be fetched, a second mid-turn
    go(dc.on_message(Msg(watched, 501, "SLOW how was the session", attachments=[Attachment("plan.pdf", fail=True)])))
    wait(lambda: room.reading)
    go(dc.on_message(Msg(watched, 502, "and the bench")))
    answered("echo: SLOW how was the session")
    # a slash command there: one ephemeral line naming the room's own channel
    it = It("status", channel=WATCHED)
    go(dc.on_interaction(it))
    # an edit after it ran comes in again, marked; a delete after it ran keeps its answer
    edited = Msg(watched, 501, "how was the session, really")
    edited.edited_at = "2026-10-19T08:00:00"
    go(dc.on_raw_message_edit(NS(channel_id=WATCHED, message=edited)))
    answered("how was the session, really")
    go(dc.on_raw_message_delete(NS(channel_id=WATCHED, message_id=502)))
    # a file, a notice, a kept card's notice, a worker's card, and a relay
    plan = tmp_path / "plan.txt"
    plan.write_text("squat day")
    go(dc.send_file(ROOM, plan, "the plan", inbound="501"))
    go(dc.notify(ROOM, f"{words.NOT_DONE} not sent: big.zip"))
    dc.notice(ROOM, f"{words.NOT_DONE} not answered", ["502"])
    go(dc.worker_card(ROOM, "wabc123", f"{words.WORKING} worker abc123 running"))
    go(dc.worker_settle(ROOM, "wabc123", f"{words.DONE} worker abc123 finished clean", "settled: done"))
    d.ledger.inbound_add("relay-abc123", ROOM, "worker", "yerk", 30)
    room.submit(Inbound("relay-abc123", "yerk", "worker abc123 finished clean"))
    answered("echo: worker abc123 finished clean")
    # a restart: a card the old process left, a part it left unconfirmed, a message sent while it was down
    d.ledger.card_open("900", ROOM, "turn")
    d.ledger.delivery_plan("t9:0", ROOM, "reply", "the late part", "502")
    watched.past = [Msg(watched, 503, "missed while down")]
    dc.recovered = False
    dc.after_recover.clear()                          # the workers' and jobs' reattach have their own tests
    go(dc.on_ready())
    answered("echo: missed while down")
    room.rp.close()
    loop.call_soon_threadsafe(loop.stop)

    # nothing, of any kind, ever went to the watched channel
    assert watched.writes == [] and watched.reactions == [] and watched.deleted == [] and watched.sent == []
    assert [r for r in d.ledger.rows(500, "outbound") if r["channel"] == str(WATCHED)] == []
    assert it.said == [("message", f"{words.NOT_DONE} {ROOM} answers in <#{OWN}>, not here", True)]
    assert wired and bot.puts == [1]                  # rooms.toml's watch, wired by the daemon; commands in the own guild only
    # ... while the room read it as its own and answered in its own channel
    said = [m.content for m in own.sent]
    assert any("echo: SLOW how was the session" in x and "and the bench" in x for x in said)   # the mid-turn one folded
    assert f"{words.NOT_DONE} could not fetch plan.pdf\nsend it again" in said
    assert "the plan" in said and f"{dmod.REDELIVERED}\nthe late part" in said and own.deleted == [900]
    cards = [m for m in own.sent if m.content == f"{words.WORKING} {words.START}"]
    assert ("typing", None) in own.writes and any(c.edits for c in cards)      # its typing signal, its card's progress
    rows = {r["message_id"]: r for r in d.ledger.rows(50, "inbound")}
    assert {m: (rows[m]["channel"], rows[m]["state"]) for m in ("501", "502", "503")} == {
        m: (str(WATCHED), "answered") for m in ("501", "502", "503")}
    days = "".join(p.read_text() for p in Path(d.memory.daily).rglob("*.md"))
    assert all(f"[m:{m}]" in days for m in (501, 502))           # filed in the room's day log as they landed


def test_a_watch_on_a_rooms_own_channel_or_another_rooms_watch_is_refused(tmp_path):
    def load(text):
        (tmp_path / "rooms.toml").write_text(text)
        return rooms(tmp_path)
    a = '[rooms.a]\nchannel = "discord:1"\nwatch = "discord:3"\n'
    assert load(a + '[rooms.b]\nchannel = "discord:2"\n')["a"]["watch"] == "discord:3"
    for text, line in ((a + '[rooms.b]\nchannel = "discord:3"\n', "a watches discord:3, which is b's own channel"),
                       (a + '[rooms.b]\nchannel = "discord:2"\nwatch = "discord:3"\n', "b watches discord:3, which a watches too"),
                       ('[rooms.a]\nchannel = "discord:1"\nwatch = "discord:1"\n', "a watches discord:1, which is a's own channel")):
        with pytest.raises(SystemExit) as e:
            load(text)
        assert str(e.value) == f"rooms.toml: {line}"


def test_map_and_status_say_which_channel_a_room_watches(tmp_path, capsys):
    assert cli.main(["map", ROOM, "--private", str(layer(tmp_path))]) == 0
    assert (f"Watches discord:{WATCHED}: read as its own channel, never written to; it answers in discord:{OWN}."
            in capsys.readouterr().out)
    r = busy_room()
    r.conf = {"watch": f"discord:{WATCHED}"}
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, [], Cooldowns(tmp_path / "c.json"), Ledger(tmp_path / "l.sqlite"),
                       {ROOM: r}, units=set, timers=Timers({}))
    lines = status.card(s)
    assert f"- {ROOM}: watches {WATCHED}" in lines and all(words.width(x) <= words.PHONE for x in lines)
