"""Checks how the engine, the only code that talks to Claude Code, turns on fast mode (Claude Code's `/fast`): fast mode reaches a room's Claude
Code only when the room asked for it AND runs on Fable 5.2 by its full id. Such a process launches with "fastMode" in its --settings file and is
line, read to the end of its result and never recorded; every other process launches exactly as before."""
import json
from pathlib import Path

import pytest

from yerk_ai.accounts import Account
from yerk_ai.config import table
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
