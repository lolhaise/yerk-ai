"""Checks how the engine, the only code that talks to Claude Code, turns on fast mode (Claude Code's `/fast`): fast mode reaches a room's Claude
Code only when the room asked for it AND runs on Fable 5.2 by its full id. Such a process launches with "fastMode" in its --settings file and is
given `/fast on` before its first line, read to the end of its result and never recorded; every other process launches exactly as before."""
import asyncio
import json
import logging
import threading
from pathlib import Path

import pytest
from test_idle_compact import COMPACT, IDLE, band, compactions, compacts, echoed, reap, say, until, wait_idle

from yerk_ai import words
from yerk_ai.accounts import Account
from yerk_ai.config import override, table
from yerk_ai.engine import RoomProcess, RoomSpec
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.room import TAIL_HEAD

FAKE = Path(__file__).with_name("fake_claude.py")
SETTINGS = {"permissions": {"allow": ["Read(/x/**)"]}, "hooks": {}}


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_STDIN", str(tmp_path / "stdin.jsonl"))
    ledger = Ledger(tmp_path / "ledger.sqlite")
    failures = FailureTable.load(table("failures.toml"))

    def _make(model="claude-fable-5-2", fast=True, settings=None, account="one"):
        spec = RoomSpec(room="r1", model=model, cwd=tmp_path, tools=["Read"], settings=dict(settings or SETTINGS))
        spec.fast = fast                       # set as an attribute: the same test runs on main, where RoomSpec has no field
        return RoomProcess(spec, Account(account, tmp_path / f"cfg-{account}"), tmp_path, ledger=ledger, failures=failures)
    return _make


def lines(tmp_path):
    p = tmp_path / "stdin.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.is_file() else []


def launched(tmp_path):
    return [json.loads(x)["argv"] for x in (tmp_path / "argv.jsonl").read_text().splitlines()]


def test_a_fast_fable_room_launches_with_the_key_and_says_fast_on_first(make, tmp_path):
    with make() as rp:
        rp.start()
        written = json.loads((rp.room_dir / "settings.json").read_text())
        turn = rp.ask("hi", timeout=20)
    assert written == {**SETTINGS, "fastMode": True}
    assert lines(tmp_path) == ["/fast on", "hi"]
    assert turn.outcome == "ok" and turn.text == "echo: hi" and len(turn.accepted) == 1   # the reply is the prompt's alone
    assert len(rp.ledger.rows(10)) == 1                                                  # one turn: the exchange is no row
    assert (rp.fast_state, rp.fast_said) == ("on", "↯ Fast mode ON · $16/$80 per Mtok (this session only)")


@pytest.mark.parametrize("fast", [False, None])
def test_a_room_with_fast_off_or_unset_launches_exactly_as_before(make, tmp_path, fast):
    with make(fast=fast) as rp:
        if fast is None:
            del rp.spec.fast                    # unset: a spec built without it
        rp.start()
        assert (rp.room_dir / "settings.json").read_text() == json.dumps(SETTINGS, indent=1)
        rp.ask("hi", timeout=20)
    assert lines(tmp_path) == ["hi"]
    assert not any("fastMode" in a or a.startswith("/fast") for argv in launched(tmp_path) for a in argv)


def test_an_opus_room_with_fast_forced_on_everywhere_gets_no_key_and_no_fast_line(make, tmp_path):
    with make(model="claude-opus-5-5", settings={**SETTINGS, "fastMode": True}) as rp:   # the spec and its settings both
        rp.start()
        written = json.loads((rp.room_dir / "settings.json").read_text())
        rp.ask("hi", timeout=20)
    assert written == SETTINGS and lines(tmp_path) == ["hi"]
    assert rp.fast_said == "" and rp.fast_state == "off"          # no exchange; the reply's result said off


@pytest.mark.parametrize("model", ["fable", "claude-fable-5-1", "claude-fable-5-2[1m]", "CLAUDE-FABLE-5-2"])
def test_only_the_full_fable_5_2_id_opens_the_gate(make, tmp_path, model):
    with make(model=model) as rp:
        rp.start()
        written = json.loads((rp.room_dir / "settings.json").read_text())
        rp.ask("hi", timeout=20)
    assert "fastMode" not in written and lines(tmp_path) == ["hi"]


def test_every_new_process_of_a_fast_room_says_fast_on_again(make, tmp_path):
    with make() as rp:                          # fresh
        rp.start()
        rp.ask("one", timeout=20)
    with make() as rp:                          # the room's next process resumes its bound session
        rp.start()
        assert rp.resume_arg
        rp.ask("two", timeout=20)
    with make(account="two") as rp:             # a hop: the same session on another account
        rp.start()
        rp.ask("three", timeout=20)
    assert lines(tmp_path) == ["/fast on", "one", "/fast on", "two", "/fast on", "three"]
    assert all("--settings" in a for a in launched(tmp_path))


def test_a_refused_fast_on_leaves_the_turn_at_normal_speed(make, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_CREDITS_DIRS", str(tmp_path / "cfg-one"))
    with make() as rp:
        rp.start()
        turn = rp.ask("hi", timeout=20)
    assert rp.fast_state == "off" and "requires usage credits" in rp.fast_said
    assert turn.outcome == "ok" and turn.text == "echo: hi" and len(rp.ledger.rows(10)) == 1


# the room's half: /fast from the room's own channel, its true answers, and where fast mode never reaches
ROOM = "example-room"                           # examples/private: an owner room on Fable 5.1


@pytest.fixture
def y(tmp_path, monkeypatch):
    from yerk_ai.config import EXAMPLE_PRIVATE
    from yerk_ai.daemon import Yerkd
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    return Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))


def fast(y, choice=""):
    return asyncio.run(y.fast_command(ROOM, choice))


def on_fable(y):
    assert asyncio.run(y.tool_settings(ROOM, "fable", ""))["ok"]      # /model fable: the override holds the full id


def test_fast_on_in_a_fable_5_2_room_asks_claude_code_and_keeps_it_when_accepted(y, tmp_path):
    on_fable(y)
    assert fast(y, "on") == {"ok": True, "line": words.FAST_ON_NEXT}
    assert override(tmp_path, ROOM)["fast"] is True and y.spec_for(ROOM).fast is True
    probe = launched(tmp_path)[-1]                # a local command under the room's account: no model request
    assert probe[probe.index("-p") + 1] == "/fast on" and "--no-session-persistence" in probe
    assert json.loads(probe[probe.index("--settings") + 1])["fastMode"] is True
    assert fast(y) == {"ok": True, "line": words.FAST_NOW.format(setting="on")}
    assert fast(y, "off") == {"ok": True, "line": words.FAST_OFF}
    assert "fast" not in override(tmp_path, ROOM) and y.spec_for(ROOM).fast is False


def test_fast_on_refused_for_usage_credits_keeps_nothing_and_says_which_account(y, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_NO_CREDITS_DIRS", str(tmp_path / "cfg1"))
    on_fable(y)
    assert fast(y, "on") == {"ok": False, "line": words.FAST_NOT_ON.format(why=words.FAST_NO_CREDITS.format(account="one"))}
    assert "fast" not in override(tmp_path, ROOM) and y.spec_for(ROOM).fast is False


def test_fast_on_in_a_room_on_another_model_keeps_nothing(y, tmp_path):
    assert fast(y, "on") == {"ok": False, "line": words.FAST_ONLY.format(model="Fable 5.1")}
    assert "fast" not in override(tmp_path, ROOM) and not launched_or_none(tmp_path)
    assert fast(y, "sideways")["ok"] is False


def test_an_opus_room_with_the_setting_planted_gets_no_key_and_no_fast_line(y, tmp_path):
    (tmp_path / "rooms" / ROOM).mkdir(parents=True, exist_ok=True)
    (tmp_path / "rooms" / ROOM / "override.json").write_text(json.dumps({"model": "claude-opus-5-5", "fast": True}))
    spec = y.spec_for(ROOM)
    rp = RoomProcess(spec, Account("one", tmp_path / "cfg1"), tmp_path)
    assert spec.model == "claude-opus-5-5" and "fastMode" not in rp.launch_settings()
    assert fast(y, "on") == {"ok": False, "line": words.FAST_ONLY.format(model="Opus 5.5")}


def test_switching_a_fast_room_off_fable_5_2_drops_its_fast_mode(y, tmp_path):
    on_fable(y)
    assert fast(y, "on")["ok"]
    r = asyncio.run(y.tool_settings(ROOM, "opus", ""))
    assert r["ok"] and r["line"].endswith(words.FAST_OFF) and "fast" not in override(tmp_path, ROOM)


def test_no_worker_job_or_other_launch_can_carry_fast_mode(y, tmp_path):
    from yerk_ai import workers
    on_fable(y)
    assert fast(y, "on")["ok"] and y.spec_for(ROOM).fast is True       # the room's own process: forced on
    d = tmp_path / "w"
    d.mkdir()
    assert "fastMode" not in workers.gate_settings(d).read_text()         # a worker's and a job's whole --settings
    pkg = Path(workers.__file__).parent
    names = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*.py") if "fastMode" in p.read_text() or '"/fast on"' in p.read_text()}
    assert names == {"engine.py"}                 # the gate's one writer: no worker, job, curate, beat, health, judge or need


def test_slash_fast_is_offered_with_its_two_choices():
    from yerk_ai.channels import discord as dmod
    c = {x["name"]: x for x in dmod.commands()}
    assert c["fast"]["description"] == words.COMMANDS["fast"] == "fast mode for this room, on or off, from the next reply"
    assert [o["value"] for o in c["fast"]["options"][0]["choices"]] == ["on", "off"]


def launched_or_none(tmp_path):
    return launched(tmp_path) if (tmp_path / "argv.jsonl").is_file() else []


class Chan:
    def __init__(self):
        self.replies, self.notices = [], []

    def open_card(self, room, text, kind="turn"):
        pass

    def progress(self, room, text):
        pass

    def reply(self, room, text, inbound, card=None, key=None):
        self.replies.append(text)
        return [f"m{len(self.replies)}"]

    def notice(self, room, text, inbound, card=None, key=None):
        self.notices.append(text)
        return ["n1"]

    def silent(self, room, inbound, card, key=None):
        return ["c1"]


@pytest.fixture
def room(tmp_path, monkeypatch):
    import time

    from yerk_ai.accounts import Cooldowns
    from yerk_ai.room import Inbound, Room
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_STDIN", str(tmp_path / "stdin.jsonl"))
    ledger, chan = Ledger(tmp_path / "ledger.sqlite"), Chan()
    accounts = [Account("one", tmp_path / "cfg1"), Account("two", tmp_path / "cfg2")]
    conf = {"model": "claude-fable-5-2"}
    spec = lambda: RoomSpec(room="r", model=conf["model"], cwd=tmp_path, tools=["Read"], settings=dict(SETTINGS), fast=True)  # noqa: E731
    r = Room("r", conf, spec, accounts, chan, tmp_path, ledger, FailureTable.load(table("failures.toml")), Cooldowns(tmp_path / "cd.json"))

    def say(mid, text):
        ledger.inbound_add(mid, "r", "c", "owner", len(text))
        r.submit(Inbound(mid, "owner", text))
        end = time.time() + 20
        while (r.busy or r.queue) and time.time() < end:
            time.sleep(0.05)
        assert not r.busy
    return r, chan, ledger, say


def test_a_fast_rooms_reply_is_its_prompts_alone_and_one_turn_is_ledgered(room, tmp_path):
    r, chan, ledger, say = room
    say("1", "hi")
    assert chan.replies == ["echo: hi"] and chan.notices == [] and r.fast_state == "on"
    assert lines(tmp_path) == ["/fast on", "hi"] and len(ledger.rows(10)) == 1
    assert (ledger.inbound_get("1") or {})["state"] == "answered"


def test_a_hop_to_an_account_without_credits_is_said_once_and_the_reply_still_comes(room, tmp_path, monkeypatch):
    r, chan, ledger, say = room
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", str(tmp_path / "cfg1"))          # account one: rate limited, the turn hops
    monkeypatch.setenv("FAKE_CLAUDE_NO_CREDITS_DIRS", str(tmp_path / "cfg2"))    # account two: usage credits off
    say("1", "hi")
    say("2", "again")
    assert lines(tmp_path) == ["/fast on", "hi", "/fast on", "hi", "again"]       # the hop's new process said it again
    assert chan.replies == ["echo: hi", "echo: again"]
    assert chan.notices == [f"{words.WORKING} {words.FAST_LOST.format(why=words.FAST_NO_CREDITS.format(account='two'))}"]
    assert r.fast_state == "off"


def test_out_of_usage_credits_mid_session_is_said_once(room, tmp_path):
    r, chan, ledger, say = room
    say("1", "OUT_OF_CREDITS one")
    say("2", "OUT_OF_CREDITS two")
    assert chan.replies == ["echo: OUT_OF_CREDITS one", "echo: OUT_OF_CREDITS two"]
    assert chan.notices == [f"{words.WORKING} {words.FAST_LOST.format(why=words.FAST_OUT.format(account='one'))}"]


def test_status_shows_fast_mode_only_while_it_is_on(room, tmp_path):
    from yerk_ai import status
    r, chan, ledger, say = room
    s = {"at": 0.0, "running": True, "rooms": [{"room": "r", "busy": False, "waiting": 0, "watch": None, "since": None, "account": "",
                                                 "fast": True}], "accounts": [], "workers": [], "jobs": [], "units": [], "stalls": []}
    assert f"{words.PLAIN} {words.FAST_ROOMS.format(rooms='r')}" in status.card(s)
    s["rooms"][0]["fast"] = False
    assert not [x for x in status.card(s) if "fast mode" in x]
    assert not status.fast_shown(tmp_path, "r", r)
    (tmp_path / "rooms" / "r").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rooms" / "r" / "override.json").write_text(json.dumps({"fast": True}))
    assert status.fast_shown(tmp_path, "r", r)
    r.fast_state = "off"                         # Claude Code refused it: never "on"
    assert not status.fast_shown(tmp_path, "r", r)





def test_the_compaction_copy_never_runs_in_fast_mode(tmp_path, monkeypatch):
    """The lead's decision of 2026-11-11: the copy a fast room compacts off to the side launches with no fast setting and
    is never sent /fast on, whatever the room's setting: fast replies were asked for, not a fast compaction billed to credits."""
    import logging
    import time

    from yerk_ai.config import EXAMPLE_PRIVATE
    from yerk_ai.daemon import Yerkd
    from yerk_ai.room import Inbound
    for k, v in (("YERK_CLAUDE", FAKE), ("FAKE_CLAUDE_RECORD", tmp_path / "argv.jsonl"), ("FAKE_CLAUDE_MARK", tmp_path / "mark"),
                 ("FAKE_CLAUDE_STDIN", tmp_path / "stdin.jsonl")):
        monkeypatch.setenv(k, str(v))
    launches, real = [], RoomProcess.start
    monkeypatch.setattr(RoomProcess, "start", lambda self, *a, **k: launches.append((bool(k.get("fork")), self.launch_settings())) or real(self, *a, **k))
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))
    y.budgets["session"]["precompact_within"] = 0.1         # the copy's band (test_idle_compact.band)
    on_fable(y)
    assert fast(y, "on")["ok"]
    room = y.add_room(ROOM, Chan())
    y.ledger.inbound_add("1", ROOM, "console", "owner", 20)
    taken = []
    logging.getLogger("yerk.room").addFilter(lambda r: taken.append(r.getMessage()) or True)
    room.submit(Inbound("1", "owner", "CONTEXT:195000 hello"))   # ends in the copy's band: a copy compacts
    end = time.time() + 20
    while not any("compacted on a copy" in m for m in taken) and time.time() < end:
        time.sleep(0.05)
    assert [fork for fork, _ in launches] == [False, True]         # the room's process, then the copy
    assert "fastMode" in launches[0][1] and "fastMode" not in launches[1][1]
    assert lines(tmp_path) == ["/fast on", "CONTEXT:195000 hello", COMPACT]   # no /fast on ahead of the copy's /compact


@pytest.fixture
def take(y, tmp_path, monkeypatch):
    """A room that takes a compacted copy (test_idle_compact's band, its gate here), on `setting`: "on" (/fast on, Fable
    5.2), "off" (Fable 5.2 alone) or "opus" (Opus 5.5, the setting planted). A turn ends in the band and one more runs on
    the live session while the copy's /compact is held (the copy's tail); the copy is taken with no turn running, or
    (`waiting`) as a running turn ends with a message waiting. Each launch is recorded as (a copy, its key)."""
    monkeypatch.setenv("FAKE_CLAUDE_STDIN", str(tmp_path / "stdin.jsonl"))
    launches, opened, rooms, start, read = [], threading.Event(), [], RoomProcess.start, RoomProcess.read_turn

    def held(self, *a, **k):
        t = read(self, *a, **k)
        if self.fork_of:                          # the copy, before the room takes it
            opened.wait(20)
        return t
    monkeypatch.setattr(RoomProcess, "start", lambda self, *a, **k: launches.append((bool(k.get("fork")), "fastMode" in self.launch_settings()))
                        or start(self, *a, **k))
    monkeypatch.setattr(RoomProcess, "read_turn", held)
    band(y)

    def taken(setting="on", waiting=False):
        if setting == "opus":
            (tmp_path / "rooms" / ROOM).mkdir(parents=True, exist_ok=True)
            (tmp_path / "rooms" / ROOM / "override.json").write_text(json.dumps({"model": "claude-opus-5-5", "fast": True}))
        else:
            on_fable(y)
        assert setting != "on" or fast(y, "on")["ok"]
        rooms.append(room := y.add_room(ROOM, chan := Chan()))
        say(y, room, "1", "CONTEXT:195000 hello")
        wait_idle(room)
        say(y, room, "2", f"CONTEXT:196000 {'SLOW ' * waiting}still here")
        if waiting:
            until(lambda: room.reading)
            assert say(y, room, "3", "CONTEXT:21000 queued behind it", alone=True) == "queued"
        else:
            wait_idle(room)
        opened.set()
        until(lambda: any("copy taken" in str(r["note"]) for r in y.ledger.rows(5)))
        wait_idle(room)
        return room, chan, next(r["session_id"] for r in y.ledger.rows(9) if "copy taken" in str(r["note"])), launches
    yield taken
    for r in rooms:
        r.shutdown()


def test_a_fast_room_is_fast_again_from_the_first_reply_after_it_takes_a_compacted_copy(take, y, tmp_path):
    """The copy, launched without fast mode, ends as the room takes it; the next message resumes its session on a new
    process launched with the key and /fast on first; the tail rides once and the reply is the prompt's own, one row."""
    room, chan, copy, launches = take()
    say(y, room, "3", "CONTEXT:21000 and now?")
    wait_idle(room)
    assert launches == [(False, True), (True, False), (False, True)] and room.rp.session_id == copy   # the room's, the copy, a new one
    assert lines(tmp_path)[-2:] == ["/fast on", echoed(chan, 2)] and lines(tmp_path).count("/fast on") == 2
    assert echoed(chan, 2).startswith(TAIL_HEAD) and echoed(chan, 2).endswith("\n\nCONTEXT:21000 and now?") and chan.notices == []
    assert [r["outcome"] for r in y.ledger.rows(9) if r["session_id"] == copy] == ["ok", "compacted"]
    assert not (tmp_path / "rooms" / ROOM / "copy-tail").exists()


def test_a_fast_room_taking_its_copy_as_a_turn_ends_answers_the_message_waiting_on_a_fast_process(take, y, tmp_path):
    room, chan, copy, launches = take(waiting=True)
    assert launches == [(False, True), (True, False), (False, True)] and room.rp.session_id == copy
    assert chan.replies[1] == "echo: CONTEXT:196000 SLOW still here" and lines(tmp_path)[-2:] == ["/fast on", echoed(chan, 2)]
    assert echoed(chan, 2).startswith(TAIL_HEAD) and echoed(chan, 2).endswith("\n\nCONTEXT:21000 queued behind it")
    assert [r["outcome"] for r in y.ledger.rows(9) if r["session_id"] == copy] == ["ok", "compacted"]


@pytest.mark.parametrize("setting", ["off", "opus"])
def test_a_room_fast_mode_does_not_reach_goes_on_on_the_copy_it_took_as_on_main(take, y, tmp_path, setting):
    room, chan, copy, launches = take(setting)
    taken = room.rp
    say(y, room, "3", "CONTEXT:21000 and now?")
    wait_idle(room)
    assert room.rp is taken and taken.session_id == copy and launches == [(False, False), (True, False)]
    assert "/fast on" not in lines(tmp_path) and chan.notices == [] and echoed(chan, 2).startswith(TAIL_HEAD)


def test_status_names_a_fast_room_as_fast_after_it_takes_a_copy_and_after_the_next_reply(take, y, tmp_path):
    from yerk_ai import status
    room, chan, copy, launches = take()
    on = "\n".join([words.FAST_NOW.format(setting="on"), words.FAST_SAID.format(state="on")])   # /fast alone
    assert status.fast_shown(tmp_path, ROOM, room) and fast(y)["line"] == on
    say(y, room, "3", "CONTEXT:21000 and now?")
    wait_idle(room)
    assert status.fast_shown(tmp_path, ROOM, room) and fast(y)["line"] == on


def test_a_refused_fast_on_after_a_take_is_said_once_and_the_replies_still_come(take, y, tmp_path, monkeypatch):
    room, chan, copy, launches = take()
    monkeypatch.setenv("FAKE_CLAUDE_NO_CREDITS_DIRS", str(tmp_path / "cfg1"))   # the account's usage credits went off
    for mid in ("3", "4"):
        say(y, room, mid, f"CONTEXT:2{mid}000 turn {mid}")
        wait_idle(room)
    assert [echoed(chan, i)[-6:] for i in (2, 3)] == ["turn 3", "turn 4"]
    assert chan.notices == [f"{words.WORKING} {words.FAST_LOST.format(why=words.FAST_NO_CREDITS.format(account='one'))}"]


def test_a_message_that_comes_in_while_the_taken_copy_still_closes_waits_for_it_and_is_answered_on_a_fast_process(take, y, tmp_path, monkeypatch):
    """The take's close is the idle close's: while the copy ends the room has no process and the copy still holds its
    session's lock, so a message in that gap waits for the close (never a turn crashed on a session still locked)."""
    closing, release, close = threading.Event(), threading.Event(), RoomProcess.close

    def held(self, *a, **k):
        if not self.spec.fast:                    # the copy: a fast room's one process launched without fast mode
            closing.set()
            release.wait(10)
        return close(self, *a, **k)
    monkeypatch.setattr(RoomProcess, "close", held)
    room, chan, copy, launches = take()
    assert closing.wait(5) and room.rp is None
    say(y, room, "3", "CONTEXT:21000 and now?")
    threading.Event().wait(0.3)                   # its start waits on the close: nothing launched, nothing said
    assert room.busy and len(launches) == 2 and len(chan.replies) == 2 and chan.notices == []
    release.set()
    wait_idle(room)
    assert launches == [(False, True), (True, False), (False, True)] and room.rp.session_id == copy
    assert chan.notices == [] and echoed(chan, 2).endswith("\n\nCONTEXT:21000 and now?")
    assert y.ledger.inbound_get("3")["state"] == "answered"


def test_a_take_that_fails_after_its_row_still_closes_the_copy_and_the_next_message_is_answered_on_a_fast_process(take, y, tmp_path, monkeypatch):
    """Whatever fails between the take and the copy's close (here the ledger, the row in) costs the message it crashed, as
    on main, never the room: the copy ends all the same, so the next message finds its session's lock free."""
    record = RoomProcess.record

    def refused(self, turn, *a, **k):
        row = record(self, turn, *a, **k)
        if str(k.get("note") or "").startswith("copy taken"):
            raise OSError(28, "No space left on device")
        return row
    monkeypatch.setattr(RoomProcess, "record", refused)
    room, chan, copy, launches = take(waiting=True)
    until(lambda: not any(t.name == f"room-{ROOM}" and t.is_alive() for t in threading.enumerate()))   # the crashed run is over
    assert y.ledger.inbound_get("3")["state"] == "failed" and room.rp is None and len(chan.notices) == 1
    say(y, room, "4", "CONTEXT:22000 and now?")
    wait_idle(room)
    assert launches == [(False, True), (True, False), (False, True)] and room.rp.session_id == copy
    assert y.ledger.inbound_get("4")["state"] == "answered" and chan.replies[-1].endswith("\n\nCONTEXT:22000 and now?")
    assert chan.replies[-1].startswith(f"echo: {TAIL_HEAD}") and len(chan.notices) == 1     # the tail kept for it, nothing more said


@pytest.mark.parametrize("how", ["hangs", "exits non-zero and prints nothing"])
def test_a_check_that_gives_no_answer_keeps_nothing_says_so_and_leaves_nothing(y, tmp_path, monkeypatch, how):
    import yerk_ai.daemon as dm
    from yerk_ai import engine
    monkeypatch.setenv("FAKE_CLAUDE_FAST_SLOW" if how == "hangs" else "FAKE_CLAUDE_FAST_EXIT", "5" if how == "hangs" else "3")
    monkeypatch.setattr(dm, "fast_probe", lambda acct, model: engine.fast_probe(acct, model, timeout=1.0))
    on_fable(y)
    assert fast(y, "on") == {"ok": False, "line": words.FAST_NOT_ON.format(why=words.FAST_NO_ANSWER)}
    assert "fast" not in override(tmp_path, ROOM) and y.spec_for(ROOM).fast is False
    assert not list((tmp_path / "cfg1" / "projects").glob("*yerk-fast*"))      # nothing left under the account's projects/


def test_a_late_answer_to_fast_on_never_becomes_the_reply(room, tmp_path, monkeypatch):
    """/fast on answered after the engine stopped waiting for it: the room's reply is still its message's own."""
    from yerk_ai import engine
    monkeypatch.setattr(engine, "FAST_WAIT", 0.3)
    monkeypatch.setenv("FAKE_CLAUDE_FAST_SLOW", "1.0")
    r, chan, ledger, say = room
    say("1", "hi")
    say("2", "again")
    assert chan.replies == ["echo: hi", "echo: again"] and lines(tmp_path) == ["/fast on", "hi", "again"]   # that process ended
    assert chan.notices == [f"{words.WORKING} {words.FAST_LOST.format(why=words.FAST_NO_ANSWER)}"] and len(ledger.rows(10)) == 2



def test_status_never_shows_fast_mode_for_a_room_off_fable_5_2(room, tmp_path):
    from yerk_ai import status
    r, chan, ledger, say = room
    (tmp_path / "rooms" / "r").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rooms" / "r" / "override.json").write_text(json.dumps({"fast": True, "model": "claude-opus-5-5"}))
    assert not status.fast_shown(tmp_path, "r", r)


def test_a_cooldown_after_fast_mode_turned_on_is_never_said_as_a_refusal(room, tmp_path):
    """fast_mode_state cooldown (fast mode's own rate limit, standard speed for a while) in a later result is no refusal: the
    room is never told "not running" with the words that turned it on."""
    r, chan, ledger, say = room
    say("1", "hi")
    say("2", "COOLDOWN then")
    assert chan.replies == ["echo: hi", "echo: COOLDOWN then"] and chan.notices == [] and r.fast_state == "cooldown"


def test_an_exception_inside_the_fast_on_exchange_leaves_the_turn_as_on_main(room, tmp_path, monkeypatch):
    def boom(self):
        raise RuntimeError("boom")
    monkeypatch.setattr(RoomProcess, "_fast_on", boom)
    r, chan, ledger, say = room
    say("1", "hi")
    assert chan.replies == ["echo: hi"] and lines(tmp_path) == ["hi"] and len(ledger.rows(10)) == 1
    assert chan.notices == [f"{words.WORKING} {words.FAST_LOST.format(why=words.FAST_NO_ANSWER)}"]


def test_a_fable_model_not_found_takes_the_rooms_fast_mode_with_it(y, tmp_path):
    """Off Fable 5.2 by a model-not-found failure (Room._off_model) as by /model: fast mode goes too, so the room's own
    settings tool, which never takes fast, cannot bring it back by switching to Fable 5.2 again."""
    from types import SimpleNamespace
    on_fable(y)
    assert fast(y, "on")["ok"]
    room = y.add_room(ROOM, Chan())
    room.rp = SimpleNamespace(spec=SimpleNamespace(model="claude-fable-5-2"))
    line = room._off_model(SimpleNamespace(source="type:model_not_found"))
    room.rp = None
    assert line.endswith(f"\n{words.FAST_OFF}") and "fast" not in override(tmp_path, ROOM)
    assert asyncio.run(y.tool_settings(ROOM, "fable", ""))["ok"] and y.spec_for(ROOM).fast is False


@pytest.fixture
def idle(y, tmp_path, monkeypatch, caplog):
    """A room that answered at 200,000 of its 250,000 window (test_idle_compact's, the copy's band off), on `setting` as
    `take`'s. Each launch is recorded as (a copy, its key) and each line written as (its process's key, the line)."""
    for k, v in (("FAKE_CLAUDE_STDIN", "stdin.jsonl"), ("FAKE_CLAUDE_MARK", "mark")):
        monkeypatch.setenv(k, str(tmp_path / v))
    caplog.set_level(logging.INFO, logger="yerk.room")
    y.budgets["session"]["precompact_within"] = 0
    launches, sent, rooms, start, send = [], [], [], RoomProcess.start, RoomProcess.send
    monkeypatch.setattr(RoomProcess, "start", lambda self, *a, **k: launches.append((bool(k.get("fork")), "fastMode" in self.launch_settings()))
                        or start(self, *a, **k))
    monkeypatch.setattr(RoomProcess, "send", lambda self, text: sent.append(("fastMode" in self.launch_settings(), text)) or send(self, text))

    def idled(setting="on"):
        if setting == "opus":
            (tmp_path / "rooms" / ROOM).mkdir(parents=True, exist_ok=True)
            (tmp_path / "rooms" / ROOM / "override.json").write_text(json.dumps({"model": "claude-opus-5-5", "fast": True}))
        else:
            on_fable(y)
        assert setting != "on" or fast(y, "on")["ok"]
        rooms.append(room := y.add_room(ROOM, chan := Chan()))
        say(y, room, "1", "CONTEXT:200000 hello")
        wait_idle(room)
        return room, chan, launches, sent
    yield idled
    for r in rooms:
        r.shutdown()


def copied(y):
    until(lambda: any("copy taken" in str(r["note"]) for r in y.ledger.rows(5)))
    return next(r["session_id"] for r in y.ledger.rows(5) if "copy taken" in str(r["note"]))


def test_a_fast_room_idle_near_its_window_compacts_on_a_copy_without_fast_mode_and_answers_next_on_a_fast_process(idle, y, tmp_path):
    room, chan, launches, sent = idle()
    reap(y, room, IDLE)
    until(lambda: compactions(tmp_path) == 1)
    assert not compacts(sent, True)                       # never written to a process launched with the key
    copy = copied(y)
    wait_idle(room)
    say(y, room, "2", "CONTEXT:21000 and now?")
    wait_idle(room)
    assert launches == [(False, True), (True, False), (False, True)] and room.rp.session_id == copy
    assert sent[-2:] == [(True, "/fast on"), (True, "CONTEXT:21000 and now?")] and chan.replies[-1] == "echo: CONTEXT:21000 and now?"
    assert [r["outcome"] for r in y.ledger.rows(9) if r["session_id"] == copy] == ["ok", "compacted"] and chan.notices == []


@pytest.mark.parametrize("how", ["closed", "restarted"])
def test_a_fast_room_with_no_process_alive_launches_nothing_to_compact_while_idle(idle, y, tmp_path, caplog, how):
    """No process to copy (the reaper closed it, or a restart): one started for the compaction would run with fast mode,
    so it waits for a turn, said once; the next message is answered on a fast process."""
    from yerk_ai.config import EXAMPLE_PRIVATE
    from yerk_ai.daemon import Yerkd
    room, chan, launches, sent = idle()
    if how == "restarted":
        room.shutdown()
        y = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))
        room = y.add_room(ROOM, Chan())
    else:
        assert room.reap_idle(force=True)
    for _ in range(2):
        reap(y, room, IDLE)
        wait_idle(room)
    assert launches == [(False, True)] and compactions(tmp_path) == 0
    assert sum("waits for a turn" in m for m in caplog.messages) == 1
    say(y, room, "2", "CONTEXT:21000 and now?")
    wait_idle(room)
    assert launches == [(False, True)] * 2 and sent[-2:] == [(True, "/fast on"), (True, "CONTEXT:21000 and now?")]
    room.shutdown()


@pytest.mark.parametrize("setting", ["off", "opus"])
def test_a_room_fast_mode_does_not_reach_compacts_while_idle_on_a_copy_it_keeps_as_its_process(idle, y, tmp_path, setting):
    room, chan, launches, sent = idle(setting)
    reap(y, room, IDLE)
    assert copied(y) == room.rp.session_id                    # taken and kept: no fast mode to give back
    assert launches == [(False, False), (True, False)] and sent == [(False, "CONTEXT:200000 hello"), (False, COMPACT)] and compactions(tmp_path) == 1


def test_a_failed_idle_copy_in_a_fast_room_is_not_tried_again_at_the_next_minute(idle, y, tmp_path, monkeypatch, caplog):
    room, chan, launches, sent = idle()
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_FAIL", "1")         # the copy's /compact fails (the room's process predates it)
    reap(y, room, IDLE)
    until(lambda: compactions(tmp_path) == 1)
    wait_idle(room)
    assert not compacts(sent, True)
    until(lambda: any("is dropped" in m for m in caplog.messages))
    reap(y, room, IDLE)
    wait_idle(room)
    assert launches == [(False, True), (True, False)] and compactions(tmp_path) == 1 and not compacts(sent, True)


def test_a_message_during_a_fast_rooms_idle_copy_is_answered_at_once_on_its_fast_session_and_rides_to_the_copy(idle, y, tmp_path, monkeypatch):
    opened, read = threading.Event(), RoomProcess.read_turn

    def held(self, *a, **k):
        t = read(self, *a, **k)
        if self.fork_of:                          # the copy's /compact, held until the message is answered
            opened.wait(20)
        return t
    monkeypatch.setattr(RoomProcess, "read_turn", held)
    room, chan, launches, sent = idle()
    live = room.rp
    reap(y, room, IDLE)
    until(lambda: compactions(tmp_path) == 1)
    assert not compacts(sent, True)
    say(y, room, "2", "CONTEXT:21000 meanwhile")
    until(lambda: len(chan.replies) == 2)                       # the copy still held
    assert room.rp is live and chan.replies[1] == "echo: CONTEXT:21000 meanwhile" and sent[-1] == (True, "CONTEXT:21000 meanwhile")
    opened.set()
    copy = copied(y)
    wait_idle(room)
    say(y, room, "3", "CONTEXT:22000 and now?")
    wait_idle(room)
    assert launches == [(False, True), (True, False), (False, True)] and room.rp.session_id == copy and chan.notices == []
    assert echoed(chan, 2).startswith(TAIL_HEAD) and "CONTEXT:21000 meanwhile" in echoed(chan, 2)   # the tail: nothing lost
    assert y.ledger.inbound_get("2")["state"] == y.ledger.inbound_get("3")["state"] == "answered"
