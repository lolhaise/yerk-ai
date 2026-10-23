"""A room that watches a channel (rooms.toml `watch`): it reads that channel as its own and never writes to
it. The proof runs the daemon's own wiring and a real room on the fake Claude against fake channels that
record every call that writes; the watched one must end with none. Then the blind day log (only the owner's
lines from the watched channel), the config refusal, and the `yerk map` and `yerk status` lines."""
import asyncio
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import discord
import pytest

import yerk_ai.channels.discord as dmod
from yerk_ai import cli, config, memory, status, words
from yerk_ai.accounts import Account, Cooldowns
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import EXAMPLE_PRIVATE, rooms
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.memory import DailyLog
from yerk_ai.room import Inbound

from test_discord import HOOK, Attachment, Chan, Msg, Room
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


def test_a_watched_channel_files_only_the_owners_lines_so_the_room_never_reads_the_other_side(tmp_path):
    """The blind shadow: in the watched channel (#example-chat, where another assistant answers too) only the owner's lines, new
    or edited, and a test sender's go in the day's log, so the room's recall pack and `recall` never read the other
    side's answers; the room's own channel still files everyone's."""
    class Shadow(Room):
        name = ROOM

        def revise(self, mid, text):                  # the owner's edit, taken while the message still waits
            return True
    other = 4321                                      # OpenClaw's bot, or anyone else who is not the owner
    log = DailyLog(tmp_path / "daily", timezone.utc)
    own, watched = Spy(OWN), Spy(WATCHED)
    dc = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=Bot(own, watched),
                        state=tmp_path, capture=log.capture)
    dc.add_room(OWN, Shadow(tmp_path), watch=WATCHED)

    def edit(ch, mid, text, author=OWNER):
        m = Msg(ch, mid, text, author=author)
        m.edited_at = "2026-10-19T08:00:00"
        asyncio.run(dc.on_raw_message_edit(NS(channel_id=ch.id, message=m)))
    for m in (Msg(watched, 601, "squats felt heavy"), Msg(watched, 602, "OpenClaw: drop to 4 sets", author=other),
              Msg(watched, 603, "a bot's take", webhook_id=777), Msg(watched, 604, "a test sender", webhook_id=HOOK),
              Msg(own, 701, "the owner here"), Msg(own, 702, "someone else here", author=other)):
        asyncio.run(dc.on_message(m))
    edit(watched, 601, "squats felt heavier")
    edit(watched, 602, "OpenClaw: drop to 4 sets, then a back-off set", author=other)    # it streams by editing
    edit(own, 702, "someone else here, edited", author=other)
    text = "".join(p.read_text() for p in (tmp_path / "daily").glob("*.md"))
    assert "OpenClaw" not in text and "a bot's take" not in text           # the other side, new or edited: never filed
    said = [x.split(" ", 3)[3].rsplit(" [c:", 1)[0] for x in text.splitlines() if x.startswith("- ")]
    assert said == ["owner: squats felt heavy", "test-webhook: a test sender", "owner: the owner here",
                    "other: someone else here", "owner: (edited) squats felt heavier",
                    "other: (edited) someone else here, edited"]


def test_another_assistant_where_a_room_watches_is_filed_as_its_own_once_settled_and_never_answered(tmp_path, monkeypatch):
    """person-room-parity: `[watch] assistants` in yerkd.toml names the other assistant (its bot's user id); its message in the
    watched channel goes in the day log under its own role and handle once it has sat with no edit or delete, so the room can
    read what was already said there; a card it edits and then deletes never does; none is ever run. Its id in the room's
    own channel, and any other bot in the watched one, stay as they were."""
    monkeypatch.setattr(dmod, "SETTLE", 0.05)
    other, ben = 4321, 5555                                # example ids
    log = DailyLog(tmp_path / "daily", timezone.utc, {ROOM: ("room-id", "room")})
    own, watched = Spy(OWN), Spy(WATCHED)
    room = Room(tmp_path)
    room.name = ROOM
    dc = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=Bot(own, watched), state=tmp_path,
                        capture=log.capture, assistants=frozenset({ben}))
    dc.add_room(OWN, room, watch=WATCHED)

    async def run():
        dc.loop = asyncio.get_running_loop()
        reply, card = (Msg(watched, mid, text, author=ben, name="Ben") for mid, text in ((901, "Three blocks, ending on the angle."),
                                                                                       (902, "Reading the page")))
        reply.created_at = card.created_at = datetime.now(timezone.utc)
        await dc.on_message(Msg(watched, 900, "explain this very briefly"))
        await dc.on_message(reply)
        await dc.on_message(card)
        card.content, card.edited_at = "Reading the page, then the figures", datetime.now(timezone.utc)
        await dc.on_raw_message_edit(NS(channel_id=WATCHED, message=card))       # it streams by editing, then deletes
        await dc.on_raw_message_delete(NS(channel_id=WATCHED, message_id=902))
        await dc.on_message(Msg(watched, 903, "a bot's take", author=other))
        await dc.on_message(Msg(own, 904, "here too", author=ben, name="Ben"))
        assert "Three blocks" not in "".join(p.read_text() for p in (tmp_path / "daily").glob("*.md"))   # not yet settled
        await asyncio.sleep(0.3)
    asyncio.run(run())
    text = "".join(p.read_text() for p in (tmp_path / "daily").glob("*.md"))
    said = [x.split(" ", 3)[3].rsplit(" [c:", 1)[0] for x in text.splitlines() if x.startswith("- ")]
    assert said == ["owner: explain this very briefly", "other@Ben: here too", "assistant@Ben: Three blocks, ending on the angle."]
    assert "Reading the page" not in text and "a bot's take" not in text and dc.settling == {}
    assert [g.text for g in room.got] == ["explain this very briefly"]                    # the assistant's: never run
    assert dc.sender_of(Msg(watched, 905, "x", author=ben)) is None
    raw = next(x for x in text.splitlines() if " assistant@Ben: " in x)
    assert memory.marked(raw).split(" assistant@Ben: ", 1)[1].startswith(f"{memory.MARKS[memory.ASSISTANT]} Three blocks")


def test_an_assistants_lines_never_fill_the_recall_pack_and_the_setting_takes_ids_only(tmp_path):
    (priv := tmp_path / "private").mkdir()
    (priv / "yerkd.toml").write_text('[memory]\nroot = "memory"\n')
    s = memory.settings(priv, tmp_path / "state")
    at = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    s.daily.mkdir(parents=True)
    (s.daily / "2026-01-05.md").write_text("# 2026-01-05\n\n" + "\n".join(
        memory.format_line(at, ROOM, ROOM, role, who, body, "private", mid) for role, who, body, mid in (
            ("owner", "", "explain this very briefly", "1"), (memory.ASSISTANT, "Ben", "We decided: go with option 3.", "2"))) + "\n")
    items = memory.room_items(s, ROOM, now=at)
    assert [i["role"] for i in items] == ["owner"]                                        # a decision of its: still not the owner's
    assert config.assistants({}) == frozenset() and config.assistants({"watch": {"assistants": ["5555", 6666]}}) == {5555, 6666}
    for bad in ("5555", ["Ben"], [5.5]):
        with pytest.raises(SystemExit, match=r"\[watch\] assistants must be a list of Discord user ids"):
            config.assistants({"watch": {"assistants": bad}})
    assert config.assistants({"watch": {"assistants": [5555]}}, frozenset({OWNER, 1001, None})) == {5555}   # not a human's id


def test_the_owners_or_a_persons_id_listed_as_another_assistant_stops_the_start(tmp_path):
    """Cold read (2026-10-23): an id in `[watch] assistants` is never answered, so the owner's id there, or one a room's
    `people` lists (the person's id pasted where the bot's goes), would leave that person unanswered in every room: the start
    stops with one line instead, before Discord is reached."""
    from test_person_wall import PERSON
    priv = layer(tmp_path)
    (lay := priv / "layers" / "her-room").mkdir(parents=True)
    for f in ("persona.md", "rulebook.md"):
        shutil.copy(priv / f, lay / f)
    with (priv / "rooms.toml").open("a") as f:
        f.write(PERSON.format(name="her-room", who=1001, extra=""))
    base = (priv / "yerkd.toml").read_text()
    for listed in (OWNER, 1001):
        (priv / "yerkd.toml").write_text(base + f'\n[watch]\nassistants = ["5555", "{listed}"]\n')
        (state := tmp_path / f"state-{listed}").mkdir()
        d = Yerkd(priv, state, only_account=Account("one", tmp_path / "cfg1"))
        with pytest.raises(SystemExit, match=f"assistants lists {listed}, the owner or a person a room answers"):
            asyncio.run(d.run_discord())


def test_the_other_assistants_wait_is_thirty_seconds_from_its_own_last_edit_and_none_from_history(tmp_path):
    """Cold read (2026-10-23): the wait is SETTLE, 30 s, counted from the message's own time (its last edit, else its post), so a
    reply left alone is filed 30 s after it went up, one edited 10 s ago waits 20 s more, and one already older than 30 s (read
    from history after a restart) is filed at once. The test above shrinks SETTLE to run fast; this one keeps it."""
    assert dmod.SETTLE == 30.0
    log = DailyLog(tmp_path / "daily", timezone.utc, {ROOM: ("room-id", "room")})
    own, watched = Spy(OWN), Spy(WATCHED)
    room = Room(tmp_path)
    room.name = ROOM
    dc = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=Bot(own, watched), state=tmp_path,
                        capture=log.capture, assistants=frozenset({5555}))       # an example id
    dc.add_room(OWN, room, watch=WATCHED)
    waits: list[tuple[int, int]] = []
    dc.loop = NS(call_later=lambda delay, fn, *a: waits.append((a[1].id, round(delay))) or NS(cancel=lambda: None))
    now = datetime.now(timezone.utc)
    fresh, edited, old = (Msg(watched, mid, f"reply {mid}", author=5555, name="helper-bot") for mid in (911, 912, 913))
    fresh.created_at = now
    edited.created_at, edited.edited_at = now - timedelta(seconds=120), now - timedelta(seconds=10)
    old.created_at = now - timedelta(seconds=31)
    for m in (fresh, edited, old):
        dc.file(ROOM, m)
    assert waits == [(911, 30), (912, 20)] and set(dc.settling) == {"911", "912"}
    text = "".join(p.read_text() for p in (tmp_path / "daily").glob("*.md"))
    assert "reply 913" in text and "reply 911" not in text and "reply 912" not in text

def test_a_thread_started_in_the_watched_channel_never_brings_the_other_sides_words(tmp_path):
    """Cold read 33 W2: in discord.py 2.6.4 a thread_created line is in the name of whoever started the thread and its
    text is the thread's name, which can be the other side's words; a rename's text is the new name. In a watched
    channel only what a person types, a message or a reply, is filed in the day log or run."""
    log = DailyLog(tmp_path / "daily", timezone.utc)
    own, watched = Spy(OWN), Spy(WATCHED)
    dc = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=Bot(own, watched),
                        state=tmp_path, capture=log.capture)
    room = Room(tmp_path)
    room.name = ROOM
    dc.add_room(OWN, room, watch=WATCHED)
    for mid, kind, text in ((801, "thread_created", "Drop to 4 sets and add a back-off set"),
                            (802, "channel_name_change", "gym-back-off-sets"), (803, "default", "ok but why"),
                            (804, "reply", "and the deload")):
        m = Msg(watched, mid, text)                    # all in the owner's name
        m.type = getattr(discord.MessageType, kind)
        asyncio.run(dc.on_message(m))
    text = "".join(p.read_text() for p in (tmp_path / "daily").glob("*.md"))
    assert "Drop to 4 sets" not in text and "back-off" not in text and "ok but why" in text and "and the deload" in text
    assert [g.text for g in room.got] == ["ok but why", "and the deload"]


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
