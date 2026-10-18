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


# the session table (restart test R3) and the hop by transcript path (R1)
def test_two_rooms_binding_together_both_keep_their_rows(tmp_path):
    import threading
    from yerk_ai.engine import SessionTable
    t = SessionTable(tmp_path / "sessions.json")
    threads = [threading.Thread(target=t.bind, args=(f"room-{i}", f"sid-{i}", "one", f"tok-{i}")) for i in range(12)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    rows = t.load()
    assert sorted(rows) == sorted(f"room-{i}" for i in range(12)) and rows["room-3"]["token"] == "tok-3"
    assert t.by_token("tok-7") == "room-7" and t.by_token("") is None and t.by_token("nope") is None
    assert t.by_token("tok-8 ") is None and t.by_token("tok-") is None and t.by_token("tok-70") is None   # one byte off
    import inspect
    assert "hmac.compare_digest" in inspect.getsource(SessionTable.by_token)     # constant time (cold read F7)
    assert t.by_session("sid-2") == "room-2" and not list(tmp_path.glob(".sessions.json.*.tmp"))
    assert oct((tmp_path / "sessions.json").stat().st_mode & 0o777) == "0o600"
    t.update("room-1", transcript="/x/one.jsonl")
    t.bind("room-1", "sid-1", "two", "tok-1b")                        # the same session on another account keeps it
    assert t.get("room-1")["transcript"] == "/x/one.jsonl" and t.get("room-1")["account"] == "two"
    t.bind("room-1", "sid-new", "two", "tok-1c")                      # a new session starts with none
    assert t.get("room-1")["transcript"] == ""


def test_a_hop_resumes_by_the_transcripts_path_when_the_folders_are_not_shared(make, tmp_path, monkeypatch):
    """Two config folders with their own projects folders: the id alone would be searched in the wrong one."""
    from yerk_ai.engine import SessionTable
    with make() as rp:
        sid = rp.start()
        rp.ask("one", timeout=20)
    transcript = tmp_path / "cfg" / "projects" / "-work" / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    from yerk_ai.config import EXAMPLE_PRIVATE
    from yerk_ai.daemon import Yerkd
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)                              # the daemon learns the path from the hook payload
    y.learn_transcript("r1", {"hook_event_name": "Stop", "session_id": sid, "transcript_path": str(transcript)})
    assert SessionTable(tmp_path / "sessions.json").get("r1")["transcript"] == str(transcript)
    spec = RoomSpec(room="r1", model="m", cwd=tmp_path, tools=["Read"])
    other = RoomProcess(spec, Account("two", tmp_path / "cfg2"), tmp_path, ledger=make().ledger)
    with other:
        assert other.start() == sid                                   # the same session, continued
        assert other.ask("two", timeout=20).outcome == "ok"
    argv = records(tmp_path)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == str(transcript) and records(tmp_path)[-1]["config_dir"] == str(tmp_path / "cfg2")
    same = RoomProcess(spec, Account("two", tmp_path / "cfg2"), tmp_path, ledger=make().ledger)
    with same:                                                        # the bound account resumes by id
        same.start()
    assert records(tmp_path)[-1]["argv"][records(tmp_path)[-1]["argv"].index("--resume") + 1] == sid
    transcript.unlink()
    with make() as gone:                                              # a hop with no file: the id, and the resume rule decides
        gone.start()
    assert records(tmp_path)[-1]["argv"][records(tmp_path)[-1]["argv"].index("--resume") + 1] == sid
