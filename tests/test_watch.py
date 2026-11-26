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
from zoneinfo import ZoneInfo

import discord
import pytest

import yerk_ai.channels.discord as dmod
from yerk_ai import cli, config, files, memory, status, words
from yerk_ai.accounts import Account, Cooldowns
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import EXAMPLE_PRIVATE, rooms
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.memory import DailyLog
from yerk_ai.room import Inbound

from test_discord import HOOK, Attachment, Chan, Msg, Room
from test_files import pdf_bytes
from test_observe import Timers, busy_room
from test_slash import It, wait
from conftest import state_read_only

FAKE = Path(__file__).with_name("fake_claude.py")
ROOM, OWN, WATCHED, OWNER, BOT = "example-room", 11, 33, 7, 99
SENT = words.SENT_THERE.format(who=words.OTHER_ASSISTANT, channel=WATCHED, quiet=words.NO_REPLY)   # no watch_assistant, no name


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
    for name in ("serve_http", "reap", "keep_meters"):
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
    # a turn: the owner's message in the watched channel with a file that cannot be fetched, a second mid-turn. The fake
    # holds the turn open until the second line comes (HOLD_OPEN): `reading` is set once the room wrote the first line, maybe
    # before the fake read it, and SLOW's 1.5 s select then missed a second line that landed in the same read (45 of 911 runs
    # under load, 2026-11-17)
    go(dc.on_message(Msg(watched, 501, "HOLD_OPEN how was the session", attachments=[Attachment("plan.pdf", fail=True)])))
    wait(lambda: room.reading)
    go(dc.on_message(Msg(watched, 502, "and the bench")))
    answered(f"echo: {SENT}\nHOLD_OPEN how was the session")
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
    answered(f"echo: {SENT}\nmissed while down")
    # a message the room reads along with and has nothing to add to: NO_REPLY, so nothing is posted and its card says so
    go(dc.on_message(Msg(watched, 504, "READ_ALONG tell Ben the plan")))
    wait(lambda: d.ledger.inbound_get("504")["state"] == "answered" and not room.busy)
    room.rp.close()
    loop.call_soon_threadsafe(loop.stop)

    # nothing, of any kind, ever went to the watched channel
    assert watched.writes == [] and watched.reactions == [] and watched.deleted == [] and watched.sent == []
    assert [r for r in d.ledger.rows(500, "outbound") if r["channel"] == str(WATCHED)] == []
    assert it.said == [("message", f"{words.NOT_DONE} {ROOM} answers in <#{OWN}>, not here", True)]
    assert wired and bot.puts == [1]                  # rooms.toml's watch, wired by the daemon; commands in the own guild only
    # ... while the room read it as its own and answered in its own channel
    said = [m.content for m in own.sent]
    assert any(f"echo: {SENT}\nHOLD_OPEN how was the session" in x and f"{SENT}\nand the bench" in x for x in said)   # folded
    quiet = d.ledger.inbound_get("504")
    card = next(m for m in own.sent if str(m.id) == quiet["reply_ids"])
    assert card.edits[-1].split("\n")[0] == f"{words.DONE} {words.QUIET}" and not card.deleted and quiet["note"] == "silent"
    assert not any(words.NO_REPLY in x and SENT not in x for x in said)            # NO_REPLY itself never went out
    assert [(r["kind"], r["message_id"]) for r in d.ledger.deliveries(ROOM) if r["key"].endswith(":0") and
            r["inbound"] == "504"] == [("silent", str(card.id))]
    assert f"{words.NOT_DONE} could not fetch plan.pdf\nsend it again" in said
    assert "the plan" in said and f"{dmod.REDELIVERED}\nthe late part" in said and own.deleted == [900]
    cards = [m for m in own.sent if m.content == f"{words.WORKING} {words.START}"]
    assert ("typing", None) in own.writes and any(c.edits for c in cards)      # its typing signal, its card's progress
    rows = {r["message_id"]: r for r in d.ledger.rows(50, "inbound")}
    assert {m: (rows[m]["channel"], rows[m]["state"]) for m in ("501", "502", "503")} == {
        m: (str(WATCHED), "answered") for m in ("501", "502", "503")}
    days = "".join(p.read_text() for p in Path(d.memory.daily).rglob("*.md"))
    assert all(f"[m:{m}]" in days for m in (501, 502))           # filed in the room's day log as they landed


def test_a_message_read_in_the_watched_channel_says_who_it_was_sent_to_and_one_in_the_rooms_own_never_does(tmp_path):
    """watch-note (what the owner types in one chat reaches the other too, which knows it was sent to the first):
    the owner's or a listed person's message in the watched channel reaches the room under one line naming the assistant it
    was sent to (`watch_assistant`) and the channel by its name, so the room reads along and answers NO_REPLY unless the sender's
    words ask it something; an edit of one comes in the same way. Unset, it is the other assistant; a channel the client
    cannot name goes by its id. A message in the room's own channel comes in as it was typed."""
    class Reading(Room):
        name, conf = ROOM, {"watch_assistant": "Ben"}

        def revise(self, mid, text):                  # the first edit lands while the message waits, the next once it ran
            self.revised = getattr(self, "revised", []) + [text]
            return len(self.revised) == 1
    listed = 31                                       # a person the room lists in `people` (an example id)
    own, watched = Spy(OWN), Spy(WATCHED)
    watched.name = "example-chat"
    room = Reading(tmp_path)
    dc = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=Bot(own, watched), state=tmp_path)
    dc.add_room(OWN, room, watch=WATCHED, people=frozenset({listed}))
    line = ("[sent to Ben in #example-chat, not to this room: the owner is talking to Ben there. Read along and file what matters. "
            "Answer here only if the owner's words ask this room something, or you have something they would want from this room that "
            "Ben did not give; otherwise answer exactly NO_REPLY.]")
    for m in (Msg(watched, 701, "how was the session"), Msg(watched, 702, "and the bench", author=listed),
              Msg(own, 703, "the owner here")):
        asyncio.run(dc.on_message(m))
    for at, text in (("08:00", "how was the session, really"), ("08:01", "how was the session, honestly")):
        m = Msg(watched, 701, text)
        m.edited_at = f"2026-10-27T{at}:00"
        asyncio.run(dc.on_raw_message_edit(NS(channel_id=WATCHED, message=m)))
    assert [g.text for g in room.got] == [f"{line}\nhow was the session", f"{line}\nand the bench", "the owner here",
                                          f"[an earlier message was edited; it now reads:]\n{line}\nhow was the session, honestly"]
    assert room.revised == [f"{line}\nhow was the session, really", f"{line}\nhow was the session, honestly"]
    del watched.name                                  # no watch_assistant, and a channel the client cannot name
    room.conf = {}
    asyncio.run(dc.on_message(Msg(watched, 704, "and the deload")))
    assert room.got[-1].text == f"{SENT}\nand the deload" and SENT.startswith("[sent to the other assistant in #33, not to this")
    assert watched.writes == []


def test_a_watched_message_run_again_after_a_restart_still_says_who_it_was_sent_to(tmp_path):
    """watch-note: recover runs a message a restart left open once more, marked; one from the watched channel keeps the line
    naming who it was sent to, one from the room's own channel never gets it."""
    own, watched = Spy(OWN), Spy(WATCHED)
    room = Room(tmp_path)
    room.name = ROOM
    dc = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=Bot(own, watched), state=tmp_path)
    dc.add_room(OWN, room, watch=WATCHED)
    for ch, mid in ((watched, 801), (own, 802)):
        dc.ledger.inbound_add(str(mid), ROOM, str(ch.id), "owner", 5)
        dc.ledger.inbound_set(str(mid), state="running", attempts=1)
        ch.old[mid] = Msg(ch, mid, f"message {mid}")
    for cid in (WATCHED, OWN):
        asyncio.run(dc.recover(cid, room))
    again = "\n[redelivered after a restart]"
    assert [(g.id, g.text) for g in room.got] == [("801", f"{SENT}\nmessage 801{again}"), ("802", f"message 802{again}")]
    assert watched.writes == []


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
    text = day_log(tmp_path)
    assert "OpenClaw" not in text and "a bot's take" not in text           # the other side, new or edited: never filed
    said = [x.split(" ", 3)[3].rsplit(" [c:", 1)[0] for x in text.splitlines() if x.startswith("- ")]
    assert said == ["owner: squats felt heavy", "test-webhook: a test sender", "owner: the owner here",
                    "other: someone else here", "owner: (edited) squats felt heavier",
                    "other: (edited) someone else here, edited"]


def test_another_assistant_where_a_room_watches_is_filed_as_its_own_once_settled_and_never_answered(tmp_path, monkeypatch):
    """person-room-parity: `[watch] assistants` in yerkd.toml names the other assistant (its bot's user id); its message in the
    watched channel goes in the day log under its own role and handle once it has sat with no edit or delete, so the room can
    read what was already said there; a card it edits and then deletes never does; none is ever run. Its id in the room's
    own channel, and any other bot in the watched one, stay as they were. The owner's message comes in with its answer."""
    monkeypatch.setattr(dmod, "ANSWER_SETTLE", 0.05)
    other, ben = 4321, 5555                                       # example ids
    at = datetime.now(timezone.utc) - timedelta(seconds=1)        # each message's time: its answer has settled, its SETTLE not
    log = DailyLog(tmp_path / "daily", timezone.utc, {ROOM: ("room-id", "room")})
    own, watched = Spy(OWN), Spy(WATCHED)
    room = Room(tmp_path)
    room.name = ROOM
    dc = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=Bot(own, watched), state=tmp_path,
                        capture=log.capture, assistants=frozenset({ben}))
    dc.add_room(OWN, room, watch=WATCHED)
    waits = []                       # SETTLE's waits as the code arms them, run out here by hand: with SETTLE at 0.05 s a starved
                                     # run filed the reply as it came, before the check below (1 in 4 file runs, 2026-11-25)

    def call_later(delay, fn, *args):
        w = NS(delay=delay, run=lambda: fn(*args), cancelled=False)
        w.cancel = lambda: setattr(w, "cancelled", True)
        return waits.append(w) or w

    async def run():
        dc.loop = NS(call_later=call_later)
        ask, reply, card, here = (Msg(ch, mid, text, author=who, name=name) for ch, mid, text, who, name in (
            (watched, 900, "explain this very briefly", OWNER, ""), (watched, 901, "Three blocks, ending on the angle.", ben, "Ben"),
            (watched, 902, "Reading the page", ben, "Ben"), (own, 904, "here too", ben, "Ben")))
        ask.created_at = reply.created_at = card.created_at = here.created_at = at      # one day's lines, whenever it runs
        await dc.on_message(ask)
        await dc.on_message(reply)
        await dc.on_message(card)
        card.content, card.edited_at = "Reading the page, then the figures", datetime.now(timezone.utc)
        await dc.on_raw_message_edit(NS(channel_id=WATCHED, message=card))       # it streams by editing, then deletes
        await dc.on_raw_message_delete(NS(channel_id=WATCHED, message_id=902))
        await dc.on_message(Msg(watched, 903, "a bot's take", author=other))
        await dc.on_message(here)
        assert "Three blocks" not in day_log(tmp_path)              # not yet settled
        assert [w.cancelled for w in waits] == [False, True, True]  # 901's wait; 902's, then its edit's, ended by the delete
        for w in waits:
            w.cancelled or w.run()                                  # SETTLE ran out with no edit or delete
        for _ in range(200):                                        # the held message's turn: polled, never slept on
            if room.got:
                break
            await asyncio.sleep(0.05)
    asyncio.run(run())
    text = day_log(tmp_path)
    said = [x.split(" ", 3)[3].rsplit(" [c:", 1)[0] for x in text.splitlines() if x.startswith("- ")]
    assert said == ["owner: explain this very briefly", "other@Ben: here too", "assistant@Ben: Three blocks, ending on the angle."]
    assert "Reading the page" not in text and "a bot's take" not in text and dc.settling == {}
    answer = f"[the other assistant answered at {at:%H:%M} in #{WATCHED}: Three blocks, ending on the angle.]"
    assert [g.text for g in room.got] == [f"{SENT}\nexplain this very briefly\n{answer}"]   # the assistant's: never run
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


HELPER = 5555                                             # the other assistant's bot in the watched channel (an example id)
LINE = words.SENT_THERE.format(who="Helper", channel="lounge", quiet=words.NO_REPLY)


class Watching(Room):
    """A room whose rooms.toml names the assistant the owner talks to in the channel it watches."""
    name = ROOM

    def __init__(self, tmp, conf=None):
        super().__init__(tmp)
        self.conf = {"watch_assistant": "Helper", **(conf or {})}


class Past(Spy):
    """A watched channel whose history reads as discord.py's does: only what came after `after`."""
    async def history(self, limit=100, after=None, **kw):
        for m in [m for m in self.past if after is None or m.id > after.id][:limit]:
            yield m


def watching(tmp_path, conf=None):
    """A room in OWN watching #lounge, where `[watch] assistants` lists HELPER, with its day log."""
    own, watched = Spy(OWN), Past(WATCHED)
    watched.name, room = "lounge", Watching(tmp_path, conf)
    dc = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=Bot(own, watched), state=tmp_path,
                        capture=DailyLog(tmp_path / "daily", timezone.utc, {ROOM: ("room-id", "room")}).capture,
                        assistants=frozenset({HELPER}))
    dc.add_room(OWN, room, watch=WATCHED)
    return dc, room, own, watched


def day_log(tmp_path):
    """The day log as one text: its days in date order (the file names), each day's lines in the order filed. A run across
    00:00 UTC files into two days, and a directory lists its files in no set order, so a glob alone may read the new day
    first (three of the CI's reds, each at about 00:01 UTC)."""
    return "".join(p.read_text() for p in sorted((tmp_path / "daily").glob("*.md")))


def lines(tmp_path):
    text = day_log(tmp_path)
    return [x.split(" ", 3)[3].rsplit(" [c:", 1)[0] for x in text.splitlines() if x.startswith("- ")]


def test_a_watched_message_waits_for_the_other_assistants_answer_and_comes_in_one_turn_with_it(tmp_path, monkeypatch):
    """watch-answer (seen live: the room had read the owner's message and found nothing to add while the other assistant
    was still typing its answer): a message taken in the watched channel is held until the other assistant's answer there has
    gone ANSWER_SETTLE with no edit, then comes in once, the answer under it, its file saved. Its card, edited again and again
    and then deleted, is not the answer; the reply after it is. The room's own channel runs meanwhile without waiting, the
    answer is still filed once under its own role, and nothing is posted or typed in the watched channel."""
    monkeypatch.setattr(dmod, "ANSWER_SETTLE", 0.2)
    monkeypatch.setattr(dmod, "SETTLE", 0.3)
    dc, room, own, watched = watching(tmp_path)

    async def run():
        dc.loop = asyncio.get_running_loop()
        await dc.on_message(Msg(watched, 1001, "how did the long run go"))
        await dc.on_message(Msg(own, 1002, "and in here"))
        assert [g.text for g in room.got] == ["and in here"]          # its own channel: never behind the wait
        card = Msg(watched, 1003, "Reading the splits...", author=HELPER, name="helper-bot")
        card.created_at = datetime.now(timezone.utc)
        await dc.on_message(card)
        for n in range(4):                                          # it streams by editing, faster than it settles
            await asyncio.sleep(0.05)
            card.content, card.edited_at = f"Reading the splits, step {n}...", datetime.now(timezone.utc)
            await dc.on_raw_message_edit(NS(channel_id=WATCHED, message=card))
        reply = Msg(watched, 1004, "Even splits,   a faster last mile.", author=HELPER, name="helper-bot",
                    attachments=[Attachment("splits.csv")])
        reply.created_at = datetime.now(timezone.utc)
        await dc.on_message(reply)
        await dc.on_raw_message_delete(NS(channel_id=WATCHED, message_id=1003))   # the card goes once the reply is up
        assert len(room.got) == 1 and dc.held[ROOM].answer is None              # the reply has not settled yet
        await asyncio.sleep(0.6)
        return reply.created_at
    at = asyncio.run(run())
    answer = (f'[Helper answered at {at:%H:%M} in #lounge: Even splits, a faster last mile.]\n'
              f'[Helper attached: "splits.csv", text/csv, 1 bytes, saved at {tmp_path / "inbox" / "1004" / "splits.csv"}]')
    assert [(g.id, g.sender, g.text) for g in room.got] == [("1002", "owner", "and in here"),
                                                            ("1001", "owner", f"{LINE}\nhow did the long run go\n{answer}")]
    assert lines(tmp_path) == ["owner: how did the long run go", "owner: and in here",
                               "assistant@helper-bot: Even splits,   a faster last mile."]     # filed once; the card never
    assert dc.held == {} and dc.settling == {} and ("typing", None) in own.writes
    assert watched.writes == [] and watched.reactions == [] and watched.sent == []


def test_two_watched_messages_inside_the_wait_come_as_one_turn_and_one_deleted_while_held_never_does(tmp_path):
    """Two of the owner's messages inside the wait come as one turn, the first as they edited it, under one line that the other
    assistant had not answered within `watch_wait` (rooms.toml; default 150 s) of their message. One they deleted while it was
    held never comes, and a group left empty goes with no turn. The group's rows name it, and the rest follow its first."""
    dc, room, own, watched = watching(tmp_path, {"watch_wait": 0.3})
    assert dmod.WATCH_WAIT == 150.0 and dmod.Hold(Room(tmp_path)).wait == 150.0

    async def run():
        for mid, text in ((1101, "first"), (1102, "second"), (1103, "third, never mind")):
            await dc.on_message(Msg(watched, mid, text))
        edited = Msg(watched, 1101, "first, edited")
        edited.edited_at = datetime.now(timezone.utc)
        await dc.on_raw_message_edit(NS(channel_id=WATCHED, message=edited))
        await dc.on_raw_message_delete(NS(channel_id=WATCHED, message_id=1103))
        await asyncio.sleep(0.15)
        assert room.got == []                                       # still waiting
        await asyncio.sleep(0.45)
        await dc.on_message(Msg(watched, 1104, "gone before it ran"))
        await dc.on_raw_message_delete(NS(channel_id=WATCHED, message_id=1104))
        await asyncio.sleep(0.5)
    asyncio.run(run())
    late = "[Helper had not answered in #lounge within 0.3 s of the owner's message]"
    assert [(g.id, g.text) for g in room.got] == [("1101", f"{LINE}\nfirst, edited\nsecond\n{late}")]
    row = {m: dc.ledger.inbound_get(m) for m in ("1101", "1102", "1103", "1104")}
    assert [row[m]["note"] for m in ("1101", "1102")] == ["one turn with 1101, 1102"] * 2
    assert [(row[m]["state"], row[m]["note"]) for m in ("1103", "1104")] == [("ignored", "deleted by its sender before it ran")] * 2
    dc.ledger.inbound_set("1101", state="answered", answered=1.0, turn_id=7, reply_ids="2001", note="silent")   # as the room ends it
    room.got[0].then("answered", ["2001"], 0)
    row = {m: dc.ledger.inbound_get(m) for m in ("1101", "1102")}
    assert [(row[m]["state"], row[m]["turn_id"], row[m]["reply_ids"]) for m in row] == [("answered", 7, "2001")] * 2
    assert row["1101"]["note"] == "silent; one turn with 1101, 1102" and dc.held == {} and watched.writes == []


def test_after_a_restart_a_watched_message_with_its_answer_in_history_comes_in_at_once(tmp_path):
    """A restart: a held message it left open (recover) and one sent while it was down (catch_up), each with the other
    assistant's answer already in the channel's history after it, come in at once with that answer, no wait; the answer's
    whitespace folded and cut at ANSWER_MAX, its time in the room's zone, each answer filed once under its own role."""
    dc, room, own, watched = watching(tmp_path, {"timezone": "Asia/Tokyo"})
    now, long = datetime.now(timezone.utc), "Two parts.\n\n" + "word " * 600
    dc.ledger.inbound_add("1301", ROOM, str(WATCHED), "owner", 5, created=(now - timedelta(seconds=180)).timestamp())
    watched.old[1301] = Msg(watched, 1301, "held when it stopped")
    watched.old[1301].created_at = now - timedelta(seconds=180)
    for mid, who, text, ago in ((1302, HELPER, "The first answer.", 120), (1303, OWNER, "sent while it was down", 60),
                                (1304, HELPER, long, 40)):
        watched.past.append(Msg(watched, mid, text, author=who, name="helper-bot" if who == HELPER else "owner"))
        watched.past[-1].created_at = now - timedelta(seconds=ago)

    async def run():
        await dc.recover(WATCHED, room)
        await asyncio.sleep(0.05)
    asyncio.run(run())
    at = {s: f"{(now - timedelta(seconds=s)).astimezone(ZoneInfo('Asia/Tokyo')):%H:%M}" for s in (120, 40)}
    folded = " ".join(long.split())
    assert len(folded) > dmod.ANSWER_MAX and len(f"{folded[:dmod.ANSWER_MAX - 3]}...") == 2500
    assert [(g.id, g.text) for g in room.got] == [
        ("1301", f"{LINE}\nheld when it stopped\n[Helper answered at {at[120]} in #lounge: The first answer.]"),
        ("1303", f"{LINE}\nsent while it was down\n[Helper answered at {at[40]} in #lounge: {folded[:2497]}...]")]
    assert [x.split(":")[0] for x in lines(tmp_path)] == ["assistant@helper-bot", "owner@owner", "assistant@helper-bot"]
    assert dc.held == {} and watched.writes == []


# hold-card: a person's question in a watched channel, and the other assistant's progress card
# there, which sat still 6 s through a web fetch and was read as its answer, then filed as its line once it sat 30 s.
ASKED = "Should we add the late-fee rule to the club handbook or is it in there already?"
CARD = ("The current handbook is v53, and its source has 8 late-fee mentions, so it's likely already covered. "
        "Reading those lines to see what it says.\n"
        "🛠️ Bash · Show late-fee lines and section headings in the handbook\n"
        "🛠️ Bash\n"
        "🧩 Mcp Openclaw Web Search: example club handbook late fee rule wording\n"
        "🧩 Mcp Openclaw Web Fetch: https://example.org/handbooks/...")
QUIET_CARD = "🟢 read it, nothing to add\n-# step 2"          # Yerk AI's own quiet card in the watching room, that turn
REPLY = "It is in there already: section 3 of v53 names the late-fee rule. I checked with Bash and a web search."


def test_a_progress_card_is_told_from_a_reply_by_its_tool_rows_or_by_yerks_own_card_shape():
    """An OpenClaw card has a line that starts with a tool row's emoji (each kind of row on its own, U+FE0F kept or dropped);
    Yerk AI's own card in a room with people is a holding line under a state mark with "-# step N" last. A reply that only
    names a tool is neither, nor is a card's narration before its first row lands (the 6 s judge covers that window)."""
    narration, *rows = CARD.split("\n")
    assert dmod.is_card(CARD) and dmod.is_card(CARD.replace("️", ""))
    assert dmod.is_card(f"{narration}\n{rows[0]}") and dmod.is_card(f"{narration}\n{rows[2]}") and dmod.is_card(rows[1])
    assert QUIET_CARD == f"{words.DONE} {words.QUIET}\n{words.SUB}{words.STEP_N.format(n=2)}" and dmod.is_card(QUIET_CARD)
    assert dmod.is_card(f"{words.WORKING} looking it up\n{words.SUB}{words.STEP_N.format(n=11)}")
    for text in (REPLY, narration, f"{words.DONE} {words.QUIET}", "-# step 2", "", "Bash · Show late-fee lines"):
        assert not dmod.is_card(text), text


def test_the_other_assistants_card_is_never_its_answer_however_long_it_sits_still(tmp_path, monkeypatch):
    """hold-card (a): the card after the held message, edited as its rows come and then still for longer than ANSWER_SETTLE
    (and SETTLE), as during a long fetch, is never the answer and never filed: the hold stays open, nothing judged."""
    monkeypatch.setattr(dmod, "ANSWER_SETTLE", 0.1)
    monkeypatch.setattr(dmod, "SETTLE", 0.15)
    dc, room, own, watched = watching(tmp_path, {"watch_wait": 2.0})
    narration, *rows = CARD.split("\n")

    async def run():
        dc.loop = asyncio.get_running_loop()
        await dc.on_message(Msg(watched, 1401, ASKED))
        card = Msg(watched, 1402, f"{narration}\n{rows[0]}", author=HELPER, name="helper-bot")
        card.created_at = datetime.now(timezone.utc)
        await dc.on_message(card)
        await asyncio.sleep(0.05)
        card.content, card.edited_at = CARD, datetime.now(timezone.utc)
        await dc.on_raw_message_edit(NS(channel_id=WATCHED, message=card))
        await asyncio.sleep(0.4)
        h = dc.held[ROOM]
        assert room.got == [] and h.answer is None and h.judging == {} and set(h.cards) == {"1402"} and dc.settling == {}
    asyncio.run(run())
    assert lines(tmp_path) == [f"owner: {ASKED}"]


def test_the_wait_runs_again_from_the_cards_newest_edit_and_three_waits_at_most(tmp_path, monkeypatch):
    """hold-card (b): while the card is up the wait counts again from its newest edit, so a card that stops moving holds the
    message one wait past its last edit, not past the message; one edited on and on releases it once CARD_PATIENCE waits
    from the message have run, with the line that the assistant had not answered."""
    dc, room, own, watched = watching(tmp_path, {"watch_wait": 0.4})
    assert dmod.CARD_PATIENCE == 3 and dmod.CARD_GONE == 30.0
    late = "[Helper had not answered in #lounge within 0.4 s of the owner's message]"

    async def run():
        dc.loop = asyncio.get_running_loop()
        for first, edits, held, out in ((1501, [0.2] * 2, 0.15, 0.25), (1511, [0.2] * 5 + [0.1], 0.0, 0.2)):
            await dc.on_message(Msg(watched, first, ASKED))         # a card still after two edits; one edited on and on
            card = Msg(watched, first + 1, CARD, author=HELPER, name="helper-bot")
            card.created_at, got = datetime.now(timezone.utc), len(room.got)
            await dc.on_message(card)
            for gap in edits:
                await asyncio.sleep(gap)
                card.edited_at = datetime.now(timezone.utc)
                await dc.on_raw_message_edit(NS(channel_id=WATCHED, message=card))
            await asyncio.sleep(held)                               # 0.55 s and 1.1 s: past the 0.4 s from the message
            assert len(room.got) == got and ROOM in dc.held
            await asyncio.sleep(out)                                # 0.8 s: its last edit's wait; 1.2 s: three waits
            assert len(room.got) == got + 1 and ROOM not in dc.held
    asyncio.run(run())
    assert [(g.id, g.text) for g in room.got] == [("1501", f"{LINE}\n{ASKED}\n{late}"), ("1511", f"{LINE}\n{ASKED}\n{late}")]
    assert lines(tmp_path) == [f"owner: {ASKED}"] * 2


def test_the_reply_after_the_card_is_the_answer_and_comes_in_one_turn_with_the_message(tmp_path, monkeypatch):
    """hold-card (c): the card goes and the final reply lands as a new message (OpenClaw deletes its card as the turn ends), long
    after the wait from the message ran out: the reply settles and is the answer, in one turn with the message, its file saved;
    the reply is filed once and the card never."""
    monkeypatch.setattr(dmod, "ANSWER_SETTLE", 0.1)
    monkeypatch.setattr(dmod, "SETTLE", 0.15)
    dc, room, own, watched = watching(tmp_path, {"watch_wait": 0.5})

    async def run():
        dc.loop = asyncio.get_running_loop()
        await dc.on_message(Msg(watched, 1601, ASKED, attachments=[Attachment("screenshot.png")]))
        card = Msg(watched, 1602, CARD, author=HELPER, name="helper-bot")
        card.created_at = datetime.now(timezone.utc)
        await dc.on_message(card)
        for _ in range(3):
            await asyncio.sleep(0.2)
            card.edited_at = datetime.now(timezone.utc)
            await dc.on_raw_message_edit(NS(channel_id=WATCHED, message=card))
        await asyncio.sleep(0.2)                                    # 0.8 s: still, past ANSWER_SETTLE and the 0.5 s wait
        assert room.got == []
        await dc.on_raw_message_delete(NS(channel_id=WATCHED, message_id=1602))
        reply = Msg(watched, 1603, REPLY, author=HELPER, name="helper-bot", attachments=[Attachment("brief-v54.pdf")])
        reply.created_at = datetime.now(timezone.utc)
        await dc.on_message(reply)
        await asyncio.sleep(0.35)
        return reply.created_at
    at = asyncio.run(run())
    saved = tmp_path / "inbox" / "1603" / "brief-v54.pdf"           # an owner room: its text read beside it
    answer = (f'[Helper answered at {at:%H:%M} in #lounge: {REPLY}]\n[Helper attached: "brief-v54.pdf", application/pdf, 1 bytes, '
              f'saved at {saved}]\n{words.FILE_TEXT.format(path=files.sidecar(saved))}')
    assert [g.id for g in room.got] == ["1601"] and room.got[0].text.startswith(f"{LINE}\n{ASKED}\n[attachment] screenshot.png")
    assert room.got[0].text.endswith(f"\n{answer}") and "late-fee lines" not in room.got[0].text
    assert lines(tmp_path) == [f"owner: {ASKED}", f"assistant@helper-bot: {REPLY}"] and dc.held == {}


def test_the_card_deleted_with_no_reply_releases_the_message_within_card_gone(tmp_path, monkeypatch):
    """hold-card (d): the assistant's turn ends with no reply (it answered NO_REPLY): its card is deleted and nothing follows, so
    the message comes in CARD_GONE later with the line that it had not answered, never the rest of a long wait."""
    monkeypatch.setattr(dmod, "CARD_GONE", 0.2)
    dc, room, own, watched = watching(tmp_path, {"watch_wait": 5})

    async def run():
        dc.loop = asyncio.get_running_loop()
        await dc.on_message(Msg(watched, 1701, ASKED))
        card = Msg(watched, 1702, CARD, author=HELPER, name="helper-bot")
        card.created_at = datetime.now(timezone.utc)
        await dc.on_message(card)
        await asyncio.sleep(0.1)
        await dc.on_raw_message_delete(NS(channel_id=WATCHED, message_id=1702))
        await asyncio.sleep(0.1)
        assert room.got == [] and dc.held[ROOM].cards == {}
        await asyncio.sleep(0.25)
    asyncio.run(run())
    assert [(g.id, g.text) for g in room.got] == [("1701", f"{LINE}\n{ASKED}\n[Helper had not answered in #lounge within 5 s of the owner's message]")]
    assert lines(tmp_path) == [f"owner: {ASKED}"] and dc.held == {}


def test_a_card_is_never_filed_and_the_plain_message_after_it_is(tmp_path, monkeypatch):
    """hold-card (e): with no message held, the assistant's card is never filed however long it sits still, live or read from
    history; a card edited into a plain message is filed once that settles, and its plain message after it is filed too."""
    monkeypatch.setattr(dmod, "SETTLE", 0.1)
    dc, room, own, watched = watching(tmp_path)
    now = datetime.now(timezone.utc)

    async def run():
        dc.loop = asyncio.get_running_loop()
        live, turned = (Msg(watched, mid, CARD, author=HELPER, name="helper-bot") for mid in (1801, 1802))
        live.created_at = turned.created_at = now - timedelta(seconds=120)   # as old as the two below: one day; it settles from its edit
        for m in (live, turned):
            await dc.on_message(m)
        await asyncio.sleep(0.05)
        turned.content, turned.edited_at = "Done: v54 has it on page 2.", datetime.now(timezone.utc)
        await dc.on_raw_message_edit(NS(channel_id=WATCHED, message=turned))
        await asyncio.sleep(0.3)
        assert dc.settling == {}
        old, after = Msg(watched, 1803, CARD, author=HELPER, name="helper-bot"), Msg(watched, 1804, REPLY, author=HELPER, name="helper-bot")
        old.created_at = after.created_at = now - timedelta(seconds=120)    # read from history after a restart: at once
        for m in (old, after):
            dc.file(ROOM, m)
    asyncio.run(run())
    assert lines(tmp_path) == ["assistant@helper-bot: Done: v54 has it on page 2.", f"assistant@helper-bot: {REPLY}"]


def test_after_a_restart_a_card_in_history_is_never_the_answer_and_holds_the_wait_from_its_last_edit(tmp_path):
    """hold-card: a held message left open by a restart, the card after it in history and the reply after that: the reply is
    the answer, the card is not, nor filed. With only the card there (the assistant still at work), the message waits one
    wait past the card's last edit, where without it its wait had run out already."""
    dc, room, own, watched = watching(tmp_path, {"watch_wait": 150})
    now = datetime.now(timezone.utc)

    def held(mid, ago):
        dc.ledger.inbound_add(str(mid), ROOM, str(WATCHED), "owner", 5, created=(now - timedelta(seconds=ago)).timestamp())
        watched.old[mid] = Msg(watched, mid, ASKED)
        watched.old[mid].created_at = now - timedelta(seconds=ago)
    held(1901, 300)
    for mid, text, ago in ((1902, CARD, 290), (1903, REPLY, 280)):
        watched.past.append(Msg(watched, mid, text, author=HELPER, name="helper-bot"))
        watched.past[-1].created_at = now - timedelta(seconds=ago)

    async def run():
        await dc.recover(WATCHED, room)
        await asyncio.sleep(0.05)
        said = f"[Helper answered at {now - timedelta(seconds=280):%H:%M} in #lounge: {REPLY}]"
        assert [(g.id, g.text.splitlines()[-1]) for g in room.got] == [("1901", said)] and dc.held == {}
        dc.ledger.inbound_set("1901", state="answered")
        held(1911, 200)
        watched.past = [Msg(watched, 1912, CARD, author=HELPER, name="helper-bot")]
        watched.past[0].created_at, watched.past[0].edited_at = now - timedelta(seconds=190), now - timedelta(seconds=40)
        await dc.recover(WATCHED, room)
        h = dc.held[ROOM]
        assert set(h.cards) == {"1912"} and abs(h.due() - (now.timestamp() + 110)) < 1 and len(room.got) == 1
        h.timer.cancel()
    asyncio.run(run())
    assert [x.split(":")[0] for x in lines(tmp_path)] == ["assistant@helper-bot"]


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
    assert [g.text for g in room.got] == [f"{SENT}\nok but why", f"{SENT}\nand the deload"]


def test_a_file_the_other_assistant_attaches_is_saved_to_the_inbox_and_its_turn_names_where(tmp_path, monkeypatch):
    """need 26 (2026-10-30): a watching room asked to open a PDF the other assistant had attached in its answer found its name
    and size in the day log and no copy, since only a person's files were saved. Its files are saved in the room's inbox as a
    person's are, under the message's id, once however often the answer is judged and filed: the audit turn names where, the
    PDF's text beside it in an owner room, and the day log's line carries the id the folder is named by. No notice is posted,
    and none is a file the room must open before it may answer (Inbound.files)."""
    monkeypatch.setattr(dmod, "ANSWER_SETTLE", 0.1)
    monkeypatch.setattr(dmod, "SETTLE", 0.2)
    dc, room, own, watched = watching(tmp_path, {"watch_audit": True})
    pdf = pdf_bytes(["page one", "page two: the figure"])

    async def run():
        dc.loop = asyncio.get_running_loop()
        await dc.on_message(Msg(watched, 2001, "is the brief ready"))
        reply = Msg(watched, 2002, "Here it is.", author=HELPER, name="helper-bot",
                    attachments=[Attachment("brief v2.pdf", pdf, "application/pdf")])
        reply.created_at = datetime.now(timezone.utc)
        await dc.on_message(reply)
        for _ in range(200):                                        # its text is read off the loop: polled, never slept on
            if room.got and not dc.settling:
                break
            await asyncio.sleep(0.05)
        return reply.created_at
    at = asyncio.run(run())
    saved = tmp_path / "inbox" / "2002" / "brief v2.pdf"
    assert saved.read_bytes() == pdf and "page two: the figure" in files.sidecar(saved).read_text()
    [got] = room.got
    assert got.text.endswith(f"[Helper answered at {at:%H:%M} in #lounge: Here it is.]\n[Helper attached: \"brief v2.pdf\", "
                             f"application/pdf, {len(pdf):,} bytes, saved at {saved}]\n"
                             + words.FILE_TEXT.format(path=files.sidecar(saved))) and got.files == []
    rows = dc.ledger._select("SELECT * FROM attachments WHERE message_id = ?", ("2002",))
    assert [(r["name"], r["state"], r["path"]) for r in rows] == [("brief v2.pdf", "staged", str(saved))]   # judged, then filed: once
    text = day_log(tmp_path)
    assert memory.MARK.findall(text) == ["2001", "2002"] and f"  - attachment: brief v2.pdf, application/pdf, {len(pdf):,} bytes" in text
    assert own.sent == [] and watched.writes == []


def test_a_file_it_posts_unasked_or_in_the_rooms_own_channel_is_saved_too_and_one_that_will_not_come_is_never_a_notice(tmp_path, monkeypatch):
    """Not only an answer: a file the other assistant posts where no message waits is saved once that message is filed, one in
    the room's own channel as it lands. One that cannot be fetched is a missing row and a line in the turn it answers, never
    the "send it again" notice a person's gets, since no one in the room sent it."""
    monkeypatch.setattr(dmod, "ANSWER_SETTLE", 0.1)
    monkeypatch.setattr(dmod, "SETTLE", 0.2)
    dc, room, own, watched = watching(tmp_path)

    async def run():
        dc.loop = asyncio.get_running_loop()
        unasked = Msg(watched, 2101, "The week's sheet.", author=HELPER, name="helper-bot", attachments=[Attachment("week.csv", b"a,b\n1,2\n")])
        unasked.created_at = datetime.now(timezone.utc)
        await dc.on_message(unasked)
        await dc.on_message(Msg(own, 2102, "", author=HELPER, name="helper-bot", attachments=[Attachment("notes.txt", b"hello")]))
        await asyncio.sleep(0.05)
        assert (tmp_path / "inbox" / "2102" / "notes.txt").read_bytes() == b"hello"        # its own channel: as it lands
        assert not (tmp_path / "inbox" / "2101").exists() and room.got == []               # watched: once it is filed
        await asyncio.sleep(0.3)
        await dc.on_message(Msg(watched, 2103, "and the plan"))
        reply = Msg(watched, 2104, "It would not attach.", author=HELPER, name="helper-bot", attachments=[Attachment("plan.pdf", fail=True)])
        reply.created_at = datetime.now(timezone.utc)
        await dc.on_message(reply)
        await asyncio.sleep(0.4)
    asyncio.run(run())
    assert (tmp_path / "inbox" / "2101" / "week.csv").read_bytes() == b"a,b\n1,2\n"
    assert [g.id for g in room.got] == ["2103"]
    assert room.got[0].text.endswith('[Helper attached: "plan.pdf", which could not be fetched (OSError: cdn said no)]')
    rows = dc.ledger._select("SELECT message_id, name, state FROM attachments WHERE room = ?", (ROOM,))
    assert sorted((r["message_id"], r["name"], r["state"]) for r in rows) == [("2101", "week.csv", "staged"),
                                                                             ("2102", "notes.txt", "staged"), ("2104", "plan.pdf", "missing")]
    assert own.sent == [] and watched.writes == []


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


@state_read_only
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


def test_a_save_that_fails_outside_a_fetch_error_still_names_the_file_and_why_in_the_turn(tmp_path, monkeypatch):
    """A save that raises anything but a fetch error (a ledger write, a payload cut short) is logged, and the answer line
    still names each file and why it is not there, where before the turn saw no file at all."""
    monkeypatch.setattr(dmod, "ANSWER_SETTLE", 0.1)
    monkeypatch.setattr(dmod, "SETTLE", 0.2)
    dc, room, own, watched = watching(tmp_path)

    real = dc.stage

    async def broken(room_, m, say=True):          # only the assistant's message fails; the person's own files still stage
        if str(m.id) == "2202":
            raise RuntimeError("ledger locked")
        return await real(room_, m, say=say)
    monkeypatch.setattr(dc, "stage", broken)

    async def run():
        dc.loop = asyncio.get_running_loop()
        await dc.on_message(Msg(watched, 2201, "and the plan"))
        reply = Msg(watched, 2202, "Here.", author=HELPER, name="helper-bot", attachments=[Attachment("plan.pdf", b"%PDF-1.4")])
        reply.created_at = datetime.now(timezone.utc)
        await dc.on_message(reply)
        for _ in range(200):
            if room.got and not dc.settling:
                break
            await asyncio.sleep(0.05)
    asyncio.run(run())
    [got] = room.got
    assert got.text.endswith('[Helper attached: "plan.pdf", which could not be fetched (RuntimeError: ledger locked)]') and got.files == []
    assert not (tmp_path / "inbox" / "2202").exists() and own.sent == [] and watched.writes == []
