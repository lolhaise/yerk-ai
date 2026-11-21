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
from yerk_ai import status, usage, words
from yerk_ai.accounts import Account
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import EFFORTS, EXAMPLE_PRIVATE, discord_id
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.room import NEW_RECAP, Inbound, Room

FAKE = Path(__file__).with_name("fake_claude.py")
ROOM, ROOM_CH, OTHER_CH, OWNER, STRANGER, PERSON = "example-room", 11, 22, 7, 8, 31    # PERSON: listed in the room's `people`
MODELS = "models: opus (Opus 5.5), sonnet (Sonnet 5.5), haiku (Haiku 5.5), fable (Fable 5.2), fable51 (Fable 5.1)"   # bare /model's last line


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

    async def edit_original_response(self, *, content=None):     # the first answer edited in place, ephemeral as it was
        self.said.append(("edit", content, None))


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
    dc.add_room(ROOM_CH, room, people=frozenset({PERSON}))
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
    connection, the HTTP server, the reaper, the beat, the meters' keeper and the workers' pump return at once,
    the keeper marking that it started (`keeper_started`). The daemon and the adapter it built."""
    priv = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    rooms = priv / "rooms.toml"
    rooms.write_text(rooms.read_text().replace('"discord:CHANNEL_ID"', f'"discord:{ROOM_CH}"\npeople = [{PERSON}]\nlayer = "family"', 1)
                     .replace('privacy = "owner"', 'privacy = "family"', 1))      # a room that lists people is not the owner's alone
    (priv / "discord.token").write_text("not-a-token\n")
    (priv / "yerkd.toml").write_text('[discord]\ntoken_file = "discord.token"\n' + discord_table)
    (tmp_path / "state").mkdir(exist_ok=True)            # a test may put its state there first
    d = Yerkd(priv, tmp_path / "state", only_account=Account("one", tmp_path / "cfg1"))
    built = []

    async def start(self):
        built.append(self)

    async def at_once(*a, **kw):
        return None

    async def keeper(*a, **kw):
        d.keeper_started = True
    monkeypatch.setattr(dmod.discord, "Client", lambda **kw: Bot({ROOM_CH: 1}))
    monkeypatch.setattr(DiscordChannel, "start", start)
    monkeypatch.setattr(d, "serve_http", at_once)
    monkeypatch.setattr(d, "reap", at_once)
    monkeypatch.setattr(d, "keep_meters", keeper)
    monkeypatch.setattr(d.beat, "tick", at_once)
    monkeypatch.setattr(d.workers, "begin", lambda loop: None)
    asyncio.run(d.run_discord())
    return d, built[0]


def test_only_the_owner_is_answered_and_only_in_a_room(y):
    dc, room, _ = wire(y)
    asked = []
    y.room_stop = lambda r: asked.append(r)                  # reaching the daemon would record the room
    assert slash(dc, "stop", user=STRANGER) == [("message", f"{words.NOT_DONE} {words.NOT_OWNER}", True)]
    assert slash(dc, "stop", user=PERSON) == [("message", f"{words.NOT_DONE} {words.NOT_OWNER}", True)]   # listed, not the owner
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
    assert slash(dc, "model") == [("message", f"{words.PLAIN} this room: Sonnet 5.5, low thinking\n{MODELS}", True)]   # one account: no list
    room.rp.close()


def test_model_puts_the_room_on_an_account_between_turns_through_the_settings_tool(y, tmp_path):
    y.accounts.append(Account("two", tmp_path / "cfg2"))
    dc, room, chan = wire(y)
    say(y, room, "1", "SLOW hello")
    wait(lambda: room.reading)
    first = room.rp
    t = tmp_path / "projects" / f"{first.session_id}.jsonl"                 # the transcript the hooks named
    t.parent.mkdir()
    t.write_text("{}\n")
    y.learn_transcript(ROOM, {"session_id": first.session_id, "transcript_path": str(t)})
    assert slash(dc, "model", account="two") == [
        ("message", f"{words.DONE} next reply: Fable 5.1, default thinking, on account two\n{words.CARRIED}", True)]
    assert room.rp is first and first.alive()                # never inside the running turn
    wait(lambda: not room.busy)
    say(y, room, "2", "and now?")
    wait(lambda: not room.busy)
    r = rows(tmp_path)[-1]                                   # the same session, carried to the pinned account
    assert r["config_dir"] == str(tmp_path / "cfg2") and r["argv"][r["argv"].index("--resume") + 1] == str(t)
    assert slash(dc, "model") == [("message", "\n".join([f"{words.PLAIN} this room: Fable 5.1, default thinking, on account two",
                                                          "accounts, in the order tried for Fable 5.1:", "two: order 0, free, pinned",
                                                          "one: order 0, free", MODELS]), True)]
    assert slash(dc, "model", account="five") == [("message", f"{words.NOT_DONE} not an account: five\n"
                                                              "the accounts: one, two, default", True)]
    assert slash(dc, "model", user=STRANGER, account="one") == [("message", f"{words.NOT_DONE} {words.NOT_OWNER}", True)]
    assert json.loads((tmp_path / "rooms" / ROOM / "override.json").read_text()) == {"account": "two"}   # untouched
    room.rp.close()


def test_the_owners_model_command_works_in_a_family_room_where_the_rooms_own_tool_does_not(y, tmp_path):
    """Seen live in a people room: the owner's own /model got "only the owner's rooms can change this". The interaction had
    already proven the owner; the settings tool then judged the room (family). The owner's slash command is theirs in any of
    their rooms; a room's own `settings` call from a people room still is not."""
    y.room_confs[ROOM]["privacy"] = "family"
    dc, room, chan = wire(y)
    assert slash(dc, "model") == [("message", f"{words.PLAIN} this room: Fable 5.1, default thinking\n{MODELS}", True)]
    assert slash(dc, "effort", effort="low")[0][1] == f"{words.DONE} next reply: Fable 5.1, low thinking"
    assert slash(dc, "model", user=STRANGER, effort="max") == [("message", f"{words.NOT_DONE} {words.NOT_OWNER}", True)]
    assert slash(dc, "model", user=PERSON, effort="max") == [("message", f"{words.NOT_DONE} {words.NOT_OWNER}", True)]   # in its people
    assert asyncio.run(y.tool_settings(ROOM, "", "max")) == {"ok": False, "line": words.OWNER_ONLY}
    assert json.loads((tmp_path / "rooms" / ROOM / "override.json").read_text()) == {"effort": "low"}


def test_model_takes_an_account_a_model_or_both_by_name_or_digit_and_default_clears_both(y, tmp_path):
    """/model names an account as well as a model, and the room stays pinned to what the owner set.
    One argument: an account, a model or both; default clears both; bare, the accounts and the models."""
    y.accounts += [Account("two", tmp_path / "cfg2", order=2), Account("three", tmp_path / "cfg3", order=3)]
    dc, room, chan = wire(y)
    f = tmp_path / "rooms" / ROOM / "override.json"
    assert slash(dc, "model", model="three") == [("message", f"{words.DONE} next reply: Fable 5.1, default thinking, on account three", True)]
    assert json.loads(f.read_text()) == {"account": "three"}                               # the account alone
    assert slash(dc, "model", model="2 sonnet")[0][1] == f"{words.DONE} next reply: Sonnet 5.5, default thinking, on account two"
    assert slash(dc, "model", model="three/claude-opus-5-5")[0][1] == f"{words.DONE} next reply: Opus 5.5, default thinking, on account three"
    assert json.loads(f.read_text()) == {"model": "claude-opus-5-5", "account": "three"}
    assert slash(dc, "model", model="haiku")[0][1] == f"{words.DONE} next reply: Haiku 5.5, default thinking, on account three"   # the pin stays
    until = time.time() + 3600
    y.cooldowns.set("two", "rate_limit:claude-haiku-5-5", until)                          # two is out of haiku
    assert slash(dc, "model") == [("message", "\n".join([
        f"{words.PLAIN} this room: Haiku 5.5, default thinking, on account three", "accounts, in the order tried for Haiku 5.5:",
        "three: order 3, free, pinned", "one: order 0, free", f"two: order 2, resting until {words.short_when(until)}", MODELS]), True)]
    assert slash(dc, "model", model="default")[0][1] == f"{words.DONE} next reply: Fable 5.1, default thinking"
    assert json.loads(f.read_text()) == {}                                                 # both pins gone
    assert "two: order 2, free" in slash(dc, "model")[0][1].split("\n")                    # on Fable 5.1 now: a rest for haiku is not one for it
    assert slash(dc, "model", model="six fable")[0][1].startswith(f"{words.NOT_DONE} not on the list: six fable")
    assert slash(dc, "model", model="two", account="three")[0][1].startswith(f"{words.NOT_DONE} not on the list: two")   # typed: a model
    assert slash(dc, "model", user=STRANGER, model="two") == [("message", f"{words.NOT_DONE} {words.NOT_OWNER}", True)]
    assert slash(dc, "model", user=PERSON, model="1 opus") == [("message", f"{words.NOT_DONE} {words.NOT_OWNER}", True)]
    assert json.loads(f.read_text()) == {}                                                 # a refusal writes nothing


def suggest(dc, typed, user=OWNER, channel=ROOM_CH):
    """Discord's autocomplete request for /model's argument as `user` types `typed`: what the bot offered, (label, value)."""
    it, got = It("model", user=user, channel=channel), []
    it.type, it.data["options"] = discord.InteractionType.autocomplete, [{"name": "model", "type": 3, "value": typed, "focused": True}]

    async def autocomplete(choices):
        got.extend((c.name, c.value) for c in choices)
    it.response.autocomplete = autocomplete
    asyncio.run(dc.on_interaction(it))
    return got


def test_the_model_picker_offers_every_account_and_model_to_the_owner_alone(y, tmp_path):
    y.accounts.append(Account("two", tmp_path / "cfg2", order=2))
    dc, room, chan = wire(y)
    until = time.time() + 3600
    y.cooldowns.set("two", "rate_limit:claude-fable-5-1", until)                          # two is out of the room's model
    picks = suggest(dc, "")
    assert picks[:2] == [("one: order 0, free", "one"), (f"two: order 2, resting until {words.short_when(until)}", "two")]
    assert [v for _, v in picks[2:]] == [*y.models, "default"] and picks[-1] == (words.PICK_DEFAULT, "default")
    assert suggest(dc, "2 fa") == [("two fable: Fable 5.2 on account two, free", "two fable"),
                                   (f"two fable51: Fable 5.1 on account two, resting until {words.short_when(until)}", "two fable51")]
    assert ("two opus: Opus 5.5 on account two, free", "two opus") in suggest(dc, "two")       # a rest for fable is not one for opus
    assert suggest(dc, "", user=STRANGER) == suggest(dc, "", user=PERSON) == suggest(dc, "", channel=OTHER_CH) == []
    dc.add_room(ROOM_CH, room, watch=33, people=frozenset({PERSON}))                       # 33: a channel the room watches
    assert suggest(dc, "", channel=33) == [] and slash(dc, "model", channel=33)[0][1].startswith(words.NOT_DONE)


def test_the_model_menu_and_picker_list_the_accounts_in_the_rooms_own_order(y, tmp_path):
    """The order tried is the room's own (Yerkd.add_room: the accounts its rooms.toml line names first, then accounts.toml's)."""
    y.accounts += [Account("two", tmp_path / "cfg2", order=2), Account("three", tmp_path / "cfg3", order=3)]
    y.room_confs[ROOM]["accounts"] = ["three"]
    dc, room, chan = wire(y)
    assert [a.name for a in room.accounts] == ["three", "one", "two"]
    assert slash(dc, "model")[0][1].split("\n")[2:5] == ["three: order 3, free", "one: order 0, free", "two: order 2, free"]
    assert [v for _, v in suggest(dc, "")[:3]] == ["three", "one", "two"]


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


def test_status_is_the_phone_width_card(y, monkeypatch):
    asked, real = [], y.status_now
    monkeypatch.setattr(y, "status_now", lambda payload, *a: asked.append(payload) or real(payload, *a))
    dc, _, _ = wire(y)
    said = slash(dc, "status")
    assert asked == [{}, {"live": True}]                      # the room's /status: what is on hand, then the meters asked now
    lines = said[-1][1].splitlines()
    assert [how for how, _, _ in said] == ["message", "edit"] and said[0][2]          # one ephemeral message, edited once
    assert lines[0] == words.ROOM_HEAD.format(parts="claude-fable-5-1 · ①")   # the room's card (status.room_card)
    assert all(words.width(x) <= words.PHONE for x in lines + said[0][1].splitlines())


def test_status_in_a_room_is_that_rooms_card_and_another_room_gets_its_own_head(y, monkeypatch):
    dc, room, _ = wire(y)
    other = y.add_room("example-quiet", Chan())
    dc.add_room(OTHER_CH, other, people=frozenset())
    (y.state / "rooms" / "example-quiet").mkdir(parents=True, exist_ok=True)
    (y.state / "rooms" / "example-quiet" / "override.json").write_text(json.dumps({"model": "claude-opus-5-5", "effort": "max"}))
    here, there = (slash(dc, "status", channel=ch)[-1][1].splitlines() for ch in (ROOM_CH, OTHER_CH))
    assert here[:3] == [words.ROOM_HEAD.format(parts="claude-fable-5-1 · ①"), words.ROOM_NEW, ""]       # no level named: no line
    assert there[:3] == [words.ROOM_HEAD.format(parts="claude-opus-5-5 · ①"), words.ROOM_NEW, words.ROOM_THINK.format(effort="max")]
    assert here[3:] == there[4:] and not any(x.startswith(f"{words.PLAIN} ") for x in here + there)   # one tank, one daemon
    assert words.ALL_QUIET in here and "yerkd running" not in "\n".join(here)


LIVE = {"five_hour": {"pct": 7.0, "resets": None}, "seven_day": {"pct": 1.0, "resets": None}}    # what the /usage call reads


def held_status(y, monkeypatch, it=None, answer=LIVE):
    """/status in the room while the account's /usage call (usage.read) waits on an event, then reads `answer`: the
    interaction, what it had said when the call began, each status_now payload with its answer, and the accounts the
    call was made for."""
    gate, got, reads, real = threading.Event(), [], [], y.status_now

    async def spy(payload, *a):
        got.append((payload, s := await real(payload, *a)))
        return s

    def read(account, cwd, timeout=None):
        reads.append(account.name)
        assert gate.wait(20)
        return dict(answer)
    monkeypatch.setattr(usage, "read", read)
    monkeypatch.setattr(y, "status_now", spy)
    dc, _, _ = wire(y)
    it, seen = it or It("status"), []

    async def run():
        task = asyncio.ensure_future(dc.on_interaction(it))
        while not reads and not task.done():
            await asyncio.sleep(0.01)
        seen.extend(it.said)                     # the call is held here: nothing said after this line can be in it
        gate.set()
        await task
    asyncio.run(run())
    return it, seen, got, reads


def test_status_answers_at_once_from_what_is_on_hand_while_the_meters_are_asked(y, monkeypatch):
    """(a) The first answer is out before the /usage call ends, none started for it: the card from what is on hand, with
    one more line under the tank saying the meters are being asked."""
    it, seen, got, reads = held_status(y, monkeypatch)
    assert reads == ["one"] and [p for p, _ in got] == [{}, {"live": True}]
    assert len(seen) == 1 and seen[0][0] == "message" and seen[0][2]              # sent, ephemeral, while the call was held
    first = seen[0][1].splitlines()
    at = first.index(words.METERS_ASKED)
    assert first[at - 1] == words.TANK and first[:at] + first[at + 1:] == status.room_card(got[0][1], ROOM)
    assert words.width(words.METERS_ASKED) <= words.PHONE and not words.METERS_ASKED.startswith(words.PLAIN)


def test_the_meters_asked_now_edit_that_message_once_into_the_card_status_gave_until_now(y, monkeypatch):
    """(b) Once the call ends, the same ephemeral message is edited once, to exactly the card /status answered with until
    now (status.room_card of status_now with live), the line saying the meters are being asked gone."""
    it, seen, got, _ = held_status(y, monkeypatch)
    today = "\n".join(status.room_card(got[1][1], ROOM))
    assert got[1][0] == {"live": True} and it.said == [seen[0], ("edit", today, None)]
    assert words.METERS_ASKED not in today.splitlines() and "5h 7% · wk 1%" in today and "5h 7%" not in seen[0][1]


def test_every_reading_under_a_minute_old_is_one_answer_no_edit_and_no_call(y, monkeypatch):
    """(c) Every account read under a minute ago: the first answer is already the live card; one message, no line saying
    the meters are being asked, no edit, no second status_now and no /usage call."""
    (y.state / usage.FILE).write_text(json.dumps({"one": {**LIVE, "at": time.time(), "source": "live"}}))
    it, _, got, reads = held_status(y, monkeypatch)
    assert reads == [] and [p for p, _ in got] == [{}] and it.said == [("message", "\n".join(status.room_card(got[0][1], ROOM)), True)]
    assert words.METERS_ASKED not in it.said[0][1] and "5h 7% · wk 1%" in it.said[0][1]
    assert it.said[0][1] == "\n".join(status.room_card(asyncio.run(y.status_now({"live": True})), ROOM)) and reads == []


def test_the_first_answer_has_the_models_own_week_from_the_last_live_reading_and_the_edit_still_comes_once(y, monkeypatch):
    """(e) Every account read live ten minutes ago, its stream's reading newer: the first answer has the model's own week
    from that live reading on the account's line beside the stream's week, and the line saying the meters are being asked,
    since that is no live reading under a minute old; the meters are asked and the message is edited once."""
    now = time.time()
    (y.state / "meters.json").write_text(json.dumps({"one": {"at": now - 120, "source": "stream",
                                                             "seven_day": {"pct": 44.0, "resets": now + 4 * 86400}}}))
    (y.state / usage.FILE).write_text(json.dumps({"one": {**LIVE, "model": {"name": "Fable", "pct": 83.0, "resets": now + 3 * 86400},
                                                          "at": now - 600, "source": "live"}}))
    it, seen, got, reads = held_status(y, monkeypatch)
    first = seen[0][1].splitlines()
    assert reads == ["one"] and [p for p, _ in got] == [{}, {"live": True}] and got[0][1]["accounts"][0]["meter"]["source"] == "stream"
    assert first[first.index(words.TANK) + 1] == words.METERS_ASKED and f"{words.ON_THIS}① wk 44% · Fable ⚠️83%" in first
    assert it.said == [seen[0], ("edit", "\n".join(status.room_card(got[1][1], ROOM)), None)]


def test_kept_fresh_d_the_first_answer_has_the_kept_readings_week_and_model_and_the_edit_still_comes_once(y, monkeypatch):
    """kept-fresh (d) Every account as the owner found it: its stream's reading 8 hours old (57%), its live reading kept 10
    minutes ago by the daemon's keeper (59%). The first answer shows the kept week and model on the account's line, no age
    line, and still says the meters are being asked, since that reading is over a minute old; the edit comes once."""
    now = time.time()
    (y.state / "meters.json").write_text(json.dumps({"one": {"at": now - 8 * 3600, "source": "stream",
                                                             "seven_day": {"pct": 57.0, "resets": now + 4 * 86400}}}))
    (y.state / usage.FILE).write_text(json.dumps({"one": {"seven_day": {"pct": 59.0, "resets": now + 4 * 86400}, "at": now - 600,
                                                          "model": {"name": "Fable", "pct": 83.0, "resets": now + 3 * 86400}, "source": "live"}}))
    it, seen, got, reads = held_status(y, monkeypatch)
    first = seen[0][1].splitlines()
    assert first[first.index(words.TANK) + 1] == words.METERS_ASKED and f"{words.ON_THIS}① wk 59% · Fable ⚠️83%" in first
    assert not any("57%" in x or "last reading" in x for x in first)
    assert reads == ["one"] and [p for p, _ in got] == [{}, {"live": True}] and got[0][1]["accounts"][0]["meter"]["source"] == "kept"
    assert it.said == [seen[0], ("edit", "\n".join(status.room_card(got[1][1], ROOM)), None)]


def test_failed_read_i_a_failed_ask_edits_in_the_kept_reading_not_the_streams_older_one_once(y, monkeypatch):
    """failed-read (i) The stream's reading 8 hours old (57%), the live reading kept 10 minutes ago (59%), and the meters'
    /usage call times out: the first answer shows the kept reading and says the meters are being asked; the edit, once,
    still carries the kept week and model with one age line, 10 min, never the stream's 57% (main edited in the stream's
    week with "last reading 8 h 0 min ago"), and usage.json keeps that reading."""
    now = time.time()
    (y.state / "meters.json").write_text(json.dumps({"one": {"at": now - 8 * 3600, "source": "stream",
                                                             "seven_day": {"pct": 57.0, "resets": now + 4 * 86400}}}))
    good = {"seven_day": {"pct": 59.0, "resets": now + 4 * 86400}, "model": {"name": "Fable", "pct": 83.0, "resets": now + 3 * 86400},
            "at": now - 600, "source": "live"}
    (y.state / usage.FILE).write_text(json.dumps({"one": good}))
    it, seen, got, reads = held_status(y, monkeypatch, answer={"error": "no answer in 15 s"})
    first, edit = seen[0][1].splitlines(), it.said[-1][1].splitlines()
    assert first[first.index(words.TANK) + 1] == words.METERS_ASKED and f"{words.ON_THIS}① wk 59% · Fable ⚠️83%" in first
    assert reads == ["one"] and [p for p, _ in got] == [{}, {"live": True}]
    assert it.said == [seen[0], ("edit", "\n".join(status.room_card(got[1][1], ROOM)), None)]
    assert f"{words.ON_THIS}① wk 59% · Fable ⚠️83%" in edit and not any("57%" in x for x in edit)
    assert [x for x in edit if "last reading" in x] == ["①: last reading 10 min ago"] and words.METERS_ASKED not in edit
    assert got[1][1]["accounts"][0]["meter"]["live_error"] == "no answer in 15 s"
    assert json.loads((y.state / usage.FILE).read_text()) == {"one": good}


def test_an_edit_discord_refuses_is_one_log_line_and_the_first_answer_stands(y, monkeypatch, caplog):
    """(d) The edit refused (its token gone, or Discord's no): nothing leaves the handler, the first answer stands, and
    the log has one line for it, no traceback."""
    caplog.set_level(logging.INFO, logger="yerk.discord")
    it = It("status")

    async def refused(*, content=None):
        it.said.append(("refused", content, None))
        raise discord.HTTPException(NS(status=404, reason="Not Found"), {"code": 10015, "message": "Unknown Webhook"})
    it.edit_original_response = refused
    _, seen, _, _ = held_status(y, monkeypatch, it)                  # an exception out of the handler fails the test here
    assert [how for how, _, _ in it.said] == ["message", "refused"] and it.said[0] == seen[0]
    told = [r for r in caplog.records if r.name == "yerk.discord" and r.levelno >= logging.WARNING]
    assert len(told) == 1 and told[0].exc_info is None and "Unknown Webhook" in told[0].getMessage()


def test_registration_is_exactly_these_commands_on_exactly_the_rooms_guilds(y, tmp_path):
    bot = Bot({ROOM_CH: 1, OTHER_CH: 2, 33: 1, 44: 3})       # 44: a channel in a guild no room lives in
    dc = DiscordChannel("t", OWNER, [], y.ledger, client=bot, daemon=y)
    for cid in (ROOM_CH, OTHER_CH, 33):
        dc.add_room(cid, NS(name=f"r{cid}"))
    y.accounts += [Account("two", tmp_path / "cfg2"), Account("five", tmp_path / "cfg5")]
    asyncio.run(dc.register())
    assert [(app, g) for app, g, _ in bot.puts] == [(4242, 1), (4242, 2)]   # one overwrite per guild
    sent = bot.puts[0][2]
    assert bot.puts[1][2] == sent and [c["name"] for c in sent] == ["model", "effort", "new", "stop", "status", "fast", "login"]
    assert [c["value"] for c in sent[5]["options"][0]["choices"]] == ["on", "off"]          # /fast: its two choices
    assert sent[0]["options"][0]["autocomplete"] is True and "choices" not in sent[0]["options"][0]   # suggested to the owner alone
    assert [c["value"] for c in sent[1]["options"][0]["choices"]] == [*EFFORTS, "default"]
    assert [o["name"] for o in sent[0]["options"]] == ["model", "account"] and len(sent[1]["options"]) == 1
    assert "choices" not in sent[0]["options"][1] and "two" not in json.dumps(sent) and "five" not in json.dumps(sent)   # no account name for the guild
    assert all(len(c["description"]) <= 100 and c["type"] == 1 for c in sent)
    assert all(len(o["description"]) <= 100 and o["required"] == (c["name"] == "login") for c in sent for o in c["options"])   # /login's four alone
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
    assert dc.people[ROOM_CH] == frozenset({PERSON})          # rooms.toml's `people`, through run_discord's wiring
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
    assert dc.sender_of(NS(webhook_id=None, author=NS(id=OWNER), channel=NS(id=ROOM_CH))) is None    # no owner's message runs
    assert dc.sender_of(NS(webhook_id=None, author=NS(id=PERSON), channel=NS(id=ROOM_CH))) == "person"   # a listed person's still does
    assert [discord_id(v) for v in (7, "7", " 7 ", True, False, 7.0, 0, -7, "7x", "", None)] == [7, 7, 7] + [None] * 8
