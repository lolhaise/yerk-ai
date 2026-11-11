"""Checks how the engine, the only code that talks to Claude Code, turns on fast mode (Claude Code's `/fast`): fast mode reaches a room's Claude
Code only when the room asked for it AND runs on Fable 5.2 by its full id. Such a process launches with "fastMode" in its --settings file and is
line, read to the end of its result and never recorded; every other process launches exactly as before."""
import asyncio
import json
from pathlib import Path

import pytest

from yerk_ai import words
from yerk_ai.accounts import Account
from yerk_ai.config import override, table
from yerk_ai.engine import RoomProcess, RoomSpec
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable

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
    assert lines(tmp_path) == ["/fast on", "CONTEXT:195000 hello", "/compact"]   # no /fast on ahead of the copy's /compact


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
