import json
from pathlib import Path

import pytest

from yerk_ai.accounts import Account
from yerk_ai.config import table
from yerk_ai.engine import STREAM_FLAGS, RoomProcess, RoomSpec, SessionBusy, Turn
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("ANTHROPIC_SOMETHING", "must-not-reach-the-child")
    ledger = Ledger(tmp_path / "ledger.sqlite")
    failures = FailureTable.load(table("failures.toml"))

    def _make(room="r1"):
        spec = RoomSpec(room=room, model="m", cwd=tmp_path, tools=["Read", "Grep"])
        return RoomProcess(spec, Account("one", tmp_path / "cfg"), tmp_path, ledger=ledger, failures=failures)
    return _make


def records(tmp_path):
    return [json.loads(x) for x in (tmp_path / "argv.jsonl").read_text().splitlines()]


def test_one_turn_round_trip(make, tmp_path):
    with make() as rp:
        sid = rp.start()
        turn = rp.ask("hi", timeout=20)
    assert turn.outcome == "ok" and turn.text == "echo: hi"
    assert turn.accepted and turn.first_visible is not None and turn.models == ["fake-model"]
    row = rp.ledger.rows(1)[0]
    assert (row["account"], row["session_id"], row["outcome"], row["output_chars"]) == ("one", sid, "ok", 8)
    rec = records(tmp_path)[0]
    argv = rec["argv"]
    assert all(f in argv for f in STREAM_FLAGS)
    assert argv[argv.index("--session-id") + 1] == sid
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--tools") + 1] == "Read,Grep"
    assert "--strict-mcp-config" in argv
    assert rec["config_dir"] == str(tmp_path / "cfg")
    assert not any(k.startswith("ANTHROPIC") for k in rec["env_keys"])


def test_second_start_resumes_the_bound_session(make, tmp_path):
    with make() as rp:
        sid = rp.start()
        rp.ask("one", timeout=20)
    with make() as rp2:
        assert rp2.start() == sid
        rp2.ask("two", timeout=20)
    argv = records(tmp_path)[1]["argv"]
    assert argv[argv.index("--resume") + 1] == sid and "--session-id" not in argv


def test_fresh_mints_a_new_session(make):
    with make() as rp:
        a = rp.start()
    with make() as rp:
        assert rp.start(fresh=True) != a


def test_one_live_process_per_session(make):
    with make() as rp:
        rp.start()
        with pytest.raises(SessionBusy):
            make().start()


def test_failure_is_classified_and_ledgered(make):
    with make() as rp:
        rp.start()
        turn = rp.ask("FAIL_RATE", timeout=20)
    assert turn.outcome == "error" and turn.error_type == "rate_limit"
    assert turn.failure and (turn.failure.cls, turn.failure.action) == ("quota", "hop_account")
    assert rp.ledger.rows(1)[0]["failure"] == "quota"


def test_turn_records_fallback_and_first_visible():
    t = Turn(started=100.0)
    t.observe(101.0, {"type": "system", "subtype": "model_refusal_fallback", "fallback_model": "other"})
    t.observe(102.0, {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}})
    t.observe(103.0, {"type": "stream_event", "event": {"type": "content_block_start",
                                                        "content_block": {"type": "tool_use", "name": "Bash"}}})
    t.observe(104.0, {"type": "result", "subtype": "success", "result": "x", "modelUsage": {"a": {}, "other": {}}})
    assert (t.fallback_model, t.ms(t.first_visible), t.outcome, t.models) == ("other", 3000, "ok", ["a", "other"])
