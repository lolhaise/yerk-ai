"""Checks the Discord slash commands (/model, /stop, /new and the rest) on the rooms' own bot, against fake interactions: answered
for you only and in rooms only, each the daemon's own method (a switch lands between turns, /stop ends a running turn for good,
/new starts a fresh session), every answer ephemeral (seen by you alone) inside Discord's three seconds, and registration of
exactly this install's commands on exactly its rooms' guilds (Discord servers). No live registration, nothing posted."""
import asyncio
import json
import logging
import shutil
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import discord
import pytest

import yerk_ai.channels.discord as dmod
from yerk_ai import words
from yerk_ai.accounts import Account
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import EFFORTS, EXAMPLE_PRIVATE, discord_id
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.room import NEW_RECAP, Inbound, Room

FAKE = Path(__file__).with_name("fake_claude.py")
ROOM, ROOM_CH, OTHER_CH, OWNER, STRANGER = "example-room", 11, 22, 7, 8


class Chan:
    """The room's side of the adapter: what the room said, and the cards it kept."""
    def __init__(self):
        self.replies, self.cards, self.notified = [], [], []

    def open_card(self, room, text, kind="turn"):
        pass

    async def notify(self, room, text):                       # a hook's one line to the room
        self.notified.append(text)
        return ["x1"]

    def progress(self, room, text):
        pass

    def reply(self, room, text, inbound, card=None, key=None):
        self.replies.append(text)
        return [f"m{len(self.replies)}"]

    def notice(self, room, text, inbound, card=None, key=None):
        self.replies.append(text)
        self.cards.append(card)
        return ["n1"]


class It:
    """A slash-command interaction: what was answered, how, and whether only its sender saw it."""
    def __init__(self, name, user=OWNER, channel=ROOM_CH, **options):
        self.type, self.user, self.channel_id = discord.InteractionType.application_command, NS(id=user), channel
        self.data = {"name": name, "options": [{"name": k, "type": 3, "value": v} for k, v in options.items()]}
        self.said = []
        self.response = NS(send_message=self._say("message"), defer=self._defer)
        self.followup = NS(send=self._say("followup"))

    def _say(self, how):
        async def say(text, ephemeral=False):
            self.said.append((how, text, ephemeral))
        return say

    async def _defer(self, ephemeral=False, thinking=False):
        self.said.append(("defer", "", ephemeral))


class Bot:
    """A discord.Client stand-in: channels in guilds, and every call to Discord's command endpoints. It
    has no global endpoint, so a global registration fails the test."""
    def __init__(self, guild_of):
        self.chans = {cid: NS(id=cid, guild=NS(id=g)) for cid, g in guild_of.items()}
        self.user, self.application_id, self.puts = NS(id=99), 4242, []
        self.http = NS(bulk_upsert_guild_commands=self._put)

    async def _put(self, app, guild, payload):
        self.puts.append((app, guild, payload))

    def event(self, fn):
        return fn

    def get_channel(self, cid):
        return self.chans.get(cid)


@pytest.fixture
def y(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    return Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))


def wire(y):
    """The daemon's room behind the adapter, as run_discord wires them."""
    chan = Chan()
    room = y.add_room(ROOM, chan)
    dc = DiscordChannel("token", OWNER, [], y.ledger, client=Bot({ROOM_CH: 1}), daemon=y)
    dc.add_room(ROOM_CH, room)
    return dc, room, chan


def slash(dc, name, **kw):
    it = It(name, **kw)
    asyncio.run(dc.on_interaction(it))
    return it.said


def say(y, room, mid, text):
    y.ledger.inbound_add(mid, ROOM, "console", "owner", len(text))
    room.submit(Inbound(mid, "owner", text))


def wait(cond, timeout=20):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        time.sleep(0.05)
    assert cond()


def rows(tmp_path):
    """Every start of the fake: its argv and config folder."""
    return [json.loads(x) for x in (tmp_path / "argv.jsonl").read_text().splitlines()]


def starts(tmp_path):
    """The argv of every room process the fake was started as."""
    return [r["argv"] for r in rows(tmp_path) if "--model" in r.get("argv", [])]


def run_discord(tmp_path, monkeypatch, discord_table):
    """Yerkd.run_discord as yerkd runs it, on a copy of the example layer whose room is in channel ROOM_CH and
    a yerkd.toml with this [discord] table; the client is a Bot (room channel in guild 1), and the gateway
    connection, the HTTP server, the reaper, the beat and the workers' pump return at once. The daemon and
    the adapter it built."""
    priv = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    rooms = priv / "rooms.toml"
    rooms.write_text(rooms.read_text().replace('"discord:CHANNEL_ID"', f'"discord:{ROOM_CH}"', 1))
    (priv / "discord.token").write_text("not-a-token\n")
    (priv / "yerkd.toml").write_text('[discord]\ntoken_file = "discord.token"\n' + discord_table)
    (tmp_path / "state").mkdir()
    d = Yerkd(priv, tmp_path / "state", only_account=Account("one", tmp_path / "cfg1"))
    built = []

    async def start(self):
        built.append(self)

    async def at_once(*a, **kw):
        return None
    monkeypatch.setattr(dmod.discord, "Client", lambda **kw: Bot({ROOM_CH: 1}))
    monkeypatch.setattr(DiscordChannel, "start", start)
    monkeypatch.setattr(d, "serve_http", at_once)
    monkeypatch.setattr(d, "reap", at_once)
    monkeypatch.setattr(d.beat, "tick", at_once)
    monkeypatch.setattr(d.workers, "begin", lambda loop: None)
    asyncio.run(d.run_discord())
    return d, built[0]


def test_only_the_owner_is_answered_and_only_in_a_room(y):
    dc, room, _ = wire(y)
    asked = []
    y.room_stop = lambda r: asked.append(r)                  # reaching the daemon would record the room
    assert slash(dc, "stop", user=STRANGER) == [("message", f"{words.NOT_DONE} {words.NOT_OWNER}", True)]
    assert slash(dc, "stop", channel=OTHER_CH) == [("message", f"{words.NOT_DONE} {words.NOT_A_ROOM}", True)]
    assert asked == [] and not room.busy


def test_model_and_effort_land_between_turns_through_the_settings_tool(y, tmp_path):
    dc, room, chan = wire(y)
    say(y, room, "1", "SLOW hello")
    wait(lambda: room.reading)
    first = room.rp
    said = slash(dc, "model", model="sonnet")
    assert said[0][0] == "message" and said[0][2] and said[0][1].startswith(f"{words.DONE} next reply: Sonnet 5.5")
    assert slash(dc, "effort", effort="low")[0][1] == f"{words.DONE} next reply: Sonnet 5.5, low thinking"
    assert room.rp is first and first.alive()                # never inside the running turn
    wait(lambda: not room.busy)
    assert room.rp is None and chan.replies == ["echo: SLOW hello"]
    say(y, room, "2", "and now?")
    wait(lambda: not room.busy)
    a = starts(tmp_path)[-1]
    assert a[a.index("--resume") + 1] == first.session_id    # the same session, on the new settings
    assert a[a.index("--model") + 1] == "claude-sonnet-5-5" and a[a.index("--effort") + 1] == "low"
    assert slash(dc, "model") == [("message", f"{words.PLAIN} this room: Sonnet 5.5, low thinking", True)]
    room.rp.close()


def test_stop_ends_the_running_turn_now_and_for_good(y, tmp_path):
    dc, room, chan = wire(y)
    say(y, room, "1", "HANG build the thing")
    wait(lambda: room.reading and room.rp is not None)
    wait(lambda: (tmp_path / "argv.jsonl").exists() and bool(starts(tmp_path)))   # the fake is up: a stop before its first line leaves no row
    proc, t = room.rp.proc, time.time()
    assert slash(dc, "stop") == [("message", f"{words.DONE} {words.STOP_DONE}", True)]
    wait(lambda: not room.busy)
    assert proc.poll() is not None and time.time() - t < 10   # ended now, not at the fake's 60 s answer
    assert len(starts(tmp_path)) == 1                         # never rerun, hopped or retried fresh
    assert y.ledger.inbound_get("1")["state"] == "stopped" and words.STOPPED in chan.cards[-1]
    assert Ledger(tmp_path / "ledger.sqlite").inbound_open(ROOM) == []    # a restart runs nothing again
    assert slash(dc, "stop") == [("message", f"{words.PLAIN} {words.NOTHING_TO_STOP}", True)]
    say(y, room, "2", "hello again")                          # the room goes on, on the same session
    wait(lambda: not room.busy)
    assert chan.replies[-1] == "echo: hello again"
    room.rp.close()


def test_stop_during_an_account_hop_starts_nothing_on_the_next_account(y, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", str(tmp_path / "cfg1"))   # account one is at its limit: a hop
    y.accounts.append(Account("two", tmp_path / "cfg2"))
    dc, room, chan = wire(y)
    started, real_start, real_note = [], room._start, room._note

    def start(account, fresh=False):                          # counted here: a process killed at once never
        started.append(account.name)                          # writes the fake's argv row
        real_start(account, fresh=fresh)

    def note(line):                                           # the hop's card line: /stop lands between the runs
        assert room.stop()
        real_note(line)
    monkeypatch.setattr(room, "_start", start)
    monkeypatch.setattr(room, "_note", note)
    say(y, room, "1", "build the thing")
    wait(lambda: not room.busy)
    assert started == ["one"] and str(tmp_path / "cfg2") not in [r["config_dir"] for r in rows(tmp_path)]
    assert [(r["account"], r["outcome"]) for r in y.ledger.rows()] == [("one", "error")]   # one run in all
    assert y.ledger.inbound_get("1")["state"] == "stopped" and words.STOPPED in chan.cards[-1]


def test_stop_during_a_rerun_wait_ends_the_turn_at_once(y, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "mark"))
    waiting, real_wait = threading.Event(), y.failures.rerun_wait

    def rerun_wait(n):                                        # an overloaded reply: rerun in 5 s
        waiting.set()
        return 5.0 if n == 0 else real_wait(n)
    monkeypatch.setattr(y.failures, "rerun_wait", rerun_wait)
    dc, room, chan = wire(y)
    written, real_write = [], room._write
    monkeypatch.setattr(room, "_write", lambda m, recap=None: (written.append(m.id), real_write(m, recap)))
    say(y, room, "1", "FIXTURE:overloaded hello")
    assert waiting.wait(20)
    time.sleep(0.3)                                           # inside the wait
    t = time.time()
    assert slash(dc, "stop") == [("message", f"{words.DONE} {words.STOP_DONE}", True)]
    wait(lambda: not room.busy)
    assert time.time() - t < 1                                # not at the end of the 5 s
    assert written == ["1"] and y.ledger.inbound_get("1")["state"] == "stopped"   # never written again
    assert words.STOPPED in chan.cards[-1] and [r["outcome"] for r in y.ledger.rows()] == ["error"]


def test_new_starts_a_fresh_session_with_the_room_s_recap_line(y, tmp_path):
    dc, room, chan = wire(y)
    say(y, room, "1", "hello")
    wait(lambda: not room.busy)
    old = room.rp.session_id
    assert slash(dc, "new") == [("message", f"{words.DONE} {words.NEW_NEXT}", True)]
    say(y, room, "2", "who are you")
    wait(lambda: not room.busy)
    a = starts(tmp_path)[-1]
    assert "--resume" not in a and a[a.index("--session-id") + 1] == room.rp.session_id != old
    assert "--append-system-prompt-file" in a                  # memory stays: the start carries the room's pack
    assert chan.replies[-1] == "echo: " + NEW_RECAP.format(last="echo: hello") + "\n\nwho are you"
    room.rp.close()


def test_new_survives_a_restart_before_the_next_message(y, tmp_path):
    dc, room, _ = wire(y)
    say(y, room, "1", "hello")
    wait(lambda: not room.busy)
    old = room.rp.session_id
    assert slash(dc, "new") == [("message", f"{words.DONE} {words.NEW_NEXT}", True)]
    kept = tmp_path / "rooms" / ROOM / "fresh-next"
    mode = stat.S_IMODE(kept.stat().st_mode) if kept.exists() else None
    room.rp.close()                                            # the daemon goes (a restart, a yerk deploy) ...
    y2 = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))   # ... and comes back
    chan2 = Chan()
    room2 = y2.add_room(ROOM, chan2)
    say(y2, room2, "2", "who are you")
    wait(lambda: not room2.busy)
    a = starts(tmp_path)[-1]
    assert "--resume" not in a and a[a.index("--session-id") + 1] == room2.rp.session_id != old
    assert chan2.replies == ["echo: " + NEW_RECAP.format(last="echo: hello") + "\n\nwho are you"]
    assert mode == 0o600 and not kept.exists()                 # the daemon's own file, private from its creation,
                                                               # taken once
    say(y2, room2, "3", "and then")
    wait(lambda: not room2.busy)
    assert chan2.replies[-1] == "echo: and then" and len(starts(tmp_path)) == 2   # the new session goes on
    room2.rp.close()


def test_new_posts_no_could_not_go_on(y, tmp_path):
    dc, room, chan = wire(y)

    async def session_start(sid):                             # the SessionStart hook a fresh start sends
        await y._hook("SessionStart", ROOM, {"session_id": sid, "source": "startup"})
        await asyncio.gather(*y.later)
    say(y, room, "1", "hello")
    wait(lambda: not room.busy)
    old = room.rp.session_id
    asyncio.run(session_start(old))
    mark = tmp_path / "rooms" / ROOM / "last-session"
    assert mark.read_text() == old and chan.notified == []
    assert slash(dc, "new") == [("message", f"{words.DONE} {words.NEW_NEXT}", True)]
    gone = not mark.exists()
    say(y, room, "2", "who are you")
    wait(lambda: not room.busy)
    new = room.rp.session_id
    asyncio.run(session_start(new))                            # the new session's start reads the mark /new left
    assert chan.notified == []                                 # a session asked for is not a session lost
    assert gone and new != old and mark.read_text() == new
    asyncio.run(session_start("a-session-that-replaced-it"))   # the control: a lost one is still said
    assert chan.notified == [f"{words.NOTHING} {words.RESET}"]
    room.rp.close()


def test_a_slow_call_is_deferred_first_and_a_quick_one_answered_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(dmod, "QUICK", 0.05)

    class Daemon:
        models: dict[str, str] = {}

        async def room_stop(self, room):
            await asyncio.sleep(0.3)
            return {"ok": True, "line": words.STOP_DONE}

        async def room_new(self, room):
            return {"ok": True, "line": words.NEW_NEXT}

    dc = DiscordChannel("t", OWNER, [], Ledger(tmp_path / "l.sqlite"), client=Bot({ROOM_CH: 1}), daemon=Daemon())
    dc.add_room(ROOM_CH, NS(name=ROOM))
    assert slash(dc, "stop") == [("defer", "", True), ("followup", f"{words.DONE} {words.STOP_DONE}", True)]
    assert slash(dc, "new") == [("message", f"{words.DONE} {words.NEW_NEXT}", True)]


def test_the_real_methods_keep_their_blocking_work_off_the_loop(y, monkeypatch):
    monkeypatch.setattr(dmod, "QUICK", 0.1)
    monkeypatch.setattr(Room, "stop", lambda self: (time.sleep(0.5), True)[1])   # a TERM that takes its time
    monkeypatch.setattr(Room, "respec", lambda self: time.sleep(0.5))            # an idle process's slow close
    dc, _, _ = wire(y)                    # on the loop, either would hold it, and the answer could not be deferred
    assert slash(dc, "stop") == [("defer", "", True), ("followup", f"{words.DONE} {words.STOP_DONE}", True)]
    said = slash(dc, "model", model="sonnet")
    assert [how for how, _, _ in said] == ["defer", "followup"] and all(e for _, _, e in said)
    assert said[1][1].startswith(f"{words.DONE} next reply: Sonnet 5.5")


def test_status_is_the_phone_width_card(y):
    dc, _, _ = wire(y)
    said = slash(dc, "status")
    lines = said[-1][1].splitlines()
    assert said[-1][2] and lines[0] == f"{words.PLAIN} {words.RUNNING_ROOMS.format(rooms=ROOM)}"
    assert all(words.width(x) <= words.PHONE for x in lines)


def test_registration_is_exactly_these_commands_on_exactly_the_rooms_guilds(y, tmp_path):
    bot = Bot({ROOM_CH: 1, OTHER_CH: 2, 33: 1, 44: 3})       # 44: a channel in a guild no room lives in
    dc = DiscordChannel("t", OWNER, [], y.ledger, client=bot, daemon=y)
    for cid in (ROOM_CH, OTHER_CH, 33):
        dc.add_room(cid, NS(name=f"r{cid}"))
    asyncio.run(dc.register())
    assert [(app, g) for app, g, _ in bot.puts] == [(4242, 1), (4242, 2)]   # one overwrite per guild
    sent = bot.puts[0][2]
    assert bot.puts[1][2] == sent and [c["name"] for c in sent] == ["model", "effort", "new", "stop", "status"]
    assert [c["value"] for c in sent[0]["options"][0]["choices"]] == [*y.models, "default"]
    assert [c["value"] for c in sent[1]["options"][0]["choices"]] == [*EFFORTS, "default"]
    assert all(len(c["description"]) <= 100 and c["type"] == 1 for c in sent)
    off = Bot({ROOM_CH: 1})                                    # [discord] commands = false
    dc = DiscordChannel("t", OWNER, [], y.ledger, client=off, daemon=None)
    dc.add_room(ROOM_CH, NS(name="r"))
    asyncio.run(dc.register())
    it = It("stop")
    asyncio.run(dc.on_interaction(it))
    assert off.puts == [] and it.said == []


@pytest.mark.parametrize("line, on", [("commands = false\n", False), ('commands = "false"\n', False), ("", True)],
                         ids=["false", "quoted-false", "absent"])
def test_the_commands_line_in_yerkd_toml(tmp_path, monkeypatch, line, on):
    d, dc = run_discord(tmp_path, monkeypatch, f"owner = {OWNER}\n{line}")
    assert dc.daemon is (d if on else None) and dc.owner == OWNER and dc.rooms[ROOM_CH] is d.rooms[ROOM]
    asyncio.run(dc.register())                                 # on_ready's step, once the gateway says ready
    assert [(app, g) for app, g, _ in dc.client.puts] == ([(4242, 1)] if on else [])
    said = slash(dc, "stop")
    assert said == ([("message", f"{words.PLAIN} {words.NOTHING_TO_STOP}", True)] if on else [])


@pytest.mark.parametrize("owner", ["", "owner = true\n", "owner = 7.0\n"], ids=["absent", "bool", "float"])
def test_a_missing_or_malformed_owner_still_starts_and_every_command_names_it(tmp_path, monkeypatch, caplog, owner):
    caplog.set_level(logging.INFO, logger="yerk.daemon")
    d, dc = run_discord(tmp_path, monkeypatch, owner)
    assert dc.owner is None and d.link is dc and dc.rooms[ROOM_CH] is d.rooms[ROOM]   # started, serving its room
    told = [r.getMessage() for r in caplog.records if "[discord] owner" in r.getMessage()]
    assert len(told) == 1 and ("is missing" if not owner else "not a Discord user id") in told[0]   # once, at start
    asked = []
    d.room_stop = d.room_new = d.tool_settings = d.status_now = lambda *a: asked.append(a)
    for name in words.COMMANDS:
        for user in (OWNER, 1, None):                          # 7.0 and true read as 7 and 1 before; no id at all
            assert slash(dc, name, user=user) == [("message", f"{words.NOT_DONE} {words.NO_OWNER}", True)]
    assert asked == [] and words.width(f"{words.NOT_DONE} {words.NO_OWNER}") <= words.PHONE
    assert dc.sender_of(NS(webhook_id=None, author=NS(id=OWNER))) is None          # no person's message runs
    assert [discord_id(v) for v in (7, "7", " 7 ", True, False, 7.0, 0, -7, "7x", "", None)] == [7, 7, 7] + [None] * 8
