import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from yerk_ai import mcp_server
from yerk_ai.accounts import Account
from yerk_ai.config import table
from yerk_ai.engine import STREAM_FLAGS, RoomProcess, RoomSpec, SessionBusy, Turn, project_dir
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


def test_a_rooms_claude_runs_its_stdio_servers_on_a_baseline_env(make, tmp_path):
    """Claude Code hands a stdio server in a room's `mcp` table the room process's whole environment (the messaging
    socket and token of the room's session, SSH_AUTH_SOCK, YERK_STATE, any key the daemon started with) unless
    CLAUDE_CODE_MCP_ALLOWLIST_ENV=1, which gives it HOME, PATH, PWD and Claude Code's own session variables plus the
    `env` its table names (Claude Code's docs, code-5.md:26; measured on 2.1.301 in the cold read of pull request 37)."""
    with make() as rp:
        rp.start()
    assert "CLAUDE_CODE_MCP_ALLOWLIST_ENV" in records(tmp_path)[0]["env_keys"]


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("YERK_LIVE") != "1" or not shutil.which("claude"), reason="needs the real Claude Code")
def test_a_plug_in_server_sees_none_of_the_daemons_environment(tmp_path, monkeypatch):
    """The real Claude Code with a made-up token (its one request is refused) and a stdio server in the room's mcp.json
    that writes the names of its environment and exits: a variable the daemon was started with never reaches it."""
    seen, server = tmp_path / "seen.json", tmp_path / "server.py"
    server.write_text(f"import json, os\nopen({str(seen)!r}, 'w').write(json.dumps(sorted(os.environ)))\n")
    (tmp_path / "t.token").write_text("not-a-real-token\n")
    monkeypatch.setenv("YERK_CLAUDE", str(shutil.which("claude")))
    monkeypatch.setenv("YERK_DAEMON_ONLY", "x")       # stands in for SSH_AUTH_SOCK, YERK_STATE or a key in the daemon's env
    cfg = mcp_server.room_config("http://127.0.0.1:1/mcp", "r1", tmp_path / "m.json", "t",
                                 {"rec": {"type": "stdio", "command": sys.executable, "args": [str(server)]}})
    spec = RoomSpec(room="r1", model="claude-fable-5-1", cwd=tmp_path, tools=["Read"], mcp_config=cfg)
    with RoomProcess(spec, Account("one", tmp_path / "cfg", token_file=tmp_path / "t.token"), tmp_path,
                     ledger=Ledger(tmp_path / "l.sqlite"), failures=FailureTable.load(table("failures.toml"))) as rp:
        rp.start()
        rp.ask("hi", timeout=90)
    names = json.loads(seen.read_text())
    assert "PATH" in names and "YERK_DAEMON_ONLY" not in names and not any(k.startswith("SSH_") for k in names)


def test_the_room_reads_its_own_sessions_folder_where_a_big_tool_output_is_saved(make, tmp_path):
    """Claude Code saves a tool output too big to return under <config dir>/projects/<the cwd as it names it>/<session>/
    tool-results/ and refuses a Read there, outside the working folders (the work room copy's eval, q5). The room's process
    gets that one folder as one more --add-dir, never the whole projects folder, and it exists 0700 before the start:
    Claude Code drops an --add-dir that is missing when it starts."""
    with make() as rp:
        rp.start()
    argv = records(tmp_path)[0]["argv"]
    own = tmp_path / "cfg" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(tmp_path.resolve()))   # the docs' rule
    assert [argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"] == [str(own)]
    assert own.is_dir() and own.stat().st_mode & 0o777 == 0o700


def test_the_sessions_folder_is_named_as_claude_code_names_it():
    """engine.project_dir against what Claude Code 2.1.301 did: every "/" a "-" (the eval's refused path), and the
    folder it made, run live, for a working directory past 200 characters with an accent and an emoji: each UTF-16 unit
    outside [a-zA-Z0-9] a "-" (the emoji is two), the first 200, then "-" and a hash of the whole path."""
    from yerk_ai.engine import project_dir
    assert project_dir(Path("/home/u/state-q5/rooms/work-room/work")) == "-home-u-state-q5-rooms-work-room-work"
    assert project_dir(Path("/tmp/" + "x" * 195)) == "-tmp-" + "x" * 195                       # 200: no hash yet
    long = Path("/tmp/yerk-long-cwd/café-\U0001F600/" + "z" * 230)
    assert project_dir(long) == "-tmp-yerk-long-cwd-caf-----" + "z" * 173 + "-gkig5r"


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


def test_a_rejected_rate_limit_event_names_when_its_limit_resets():
    t = Turn(started=100.0)
    t.observe(101.0, {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "resetsAt": 5,
                                                                        "unifiedWindows": {"seven_day": {"utilization": 0.9, "resetsAt": 5}}}})
    assert t.resets is None and t.flags == []                                        # a warning rests nothing
    t.observe(102.0, {"type": "rate_limit_event", "rate_limit_info": {               # the Fable limit's shape, as seen live
        "status": "rejected", "rateLimitType": "seven_day_overage_included", "resetsAt": 9, "unifiedWindows": {
            "seven_day": {"utilization": 0.68, "resetsAt": 9}, "seven_day_overage_included": {"utilization": 1, "resetsAt": 9}}}})
    assert (t.resets, t.flags) == (9, ["rate_limit_rejected"])
    t.observe(103.0, {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour",
                                                                        "unifiedWindows": {"five_hour": {"utilization": 1, "resetsAt": 7}}}})
    assert t.resets == 7                                                             # no top-level reset: the window it names


def test_a_task_notification_result_never_ends_the_turn_or_sets_its_text():
    """Seen live: on the resume after a crash Claude Code 2.1.301 first reported two background commands the
    dead process orphaned, in a turn of its own whose result (origin kind task-notification, result "") landed 1.2 s in.
    Taken as the end of the owner's turn, it read as an empty reply and the room went fresh mid-answer."""
    t = Turn(started=100.0)
    t.observe(101.0, {"type": "system", "subtype": "task_notification", "task_id": "bg1", "status": "stopped"})
    t.observe(101.2, {"type": "result", "subtype": "success", "is_error": False, "result": "", "num_turns": 0,
                      "origin": {"kind": "task-notification"}})
    assert (t.ended, t.outcome, t.text, t.result) == (None, "running", "", None)
    t.observe(105.0, {"type": "result", "subtype": "success", "result": "the answer key", "modelUsage": {"m": {}}})
    assert (t.ended, t.outcome, t.text) == (105.0, "ok", "the answer key")


def test_a_notice_turn_while_idle_never_ends_the_next_turn_or_lends_it_its_text(make):
    """A background command that finishes after its turn ended gets a turn of its own while nothing is asked; its events
    wait in the queue, and the next turn reads them first."""
    with make() as rp:
        rp.start()
        assert rp.ask("BACKGROUND render the plans", timeout=20).text == "echo: BACKGROUND render the plans"
        turn = rp.ask("is it done?", timeout=20)
    assert (turn.outcome, turn.text, len(turn.accepted)) == ("ok", "echo: is it done?", 1)


def test_turn_keeps_claude_codes_hook_error_notification_as_a_flag():
    t = Turn(started=100.0)
    t.observe(101.0, {"type": "system", "subtype": "notification", "key": "stop-hook-error", "text": "Stop hook error occurred"})
    t.observe(102.0, {"type": "system", "subtype": "notification", "key": "other", "text": "x"})
    assert t.flags == ["hook_error:Stop hook error occurred"]


# the session table (restart test R3) and the hop by transcript path (R1)
def test_two_rooms_binding_together_both_keep_their_rows(tmp_path):
    import threading
    from yerk_ai.engine import SessionTable, token_hash
    t = SessionTable(tmp_path / "sessions.json")
    threads = [threading.Thread(target=t.bind, args=(f"room-{i}", f"sid-{i}", "one", f"tok-{i}")) for i in range(12)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    rows = t.load()
    assert sorted(rows) == sorted(f"room-{i}" for i in range(12)) and "token" not in rows["room-3"]
    assert rows["room-3"]["token_sha256"] == token_hash("tok-3") and "tok-" not in (tmp_path / "sessions.json").read_text()
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


def test_the_table_keeps_token_hashes_and_converts_a_table_with_plain_tokens(tmp_path):
    """sessions.json from before the hashes: the rooms are found by their tokens, the file is rewritten
    with hashes on load, and no plain token is written again, even one handed to update."""
    from yerk_ai.engine import SessionTable, token_hash
    path = tmp_path / "sessions.json"
    old = {"room-a": {"session_id": "s-a", "account": "one", "token": "tok-a"}, "room-b": {"session_id": "s-b", "token": "tok-b"}}
    path.write_text(json.dumps(old))
    path.parent.chmod(0o500)                                          # a reader that cannot write still finds them
    try:
        assert SessionTable(path).by_token("tok-a") == "room-a" and json.loads(path.read_text()) == old
    finally:
        path.parent.chmod(0o700)
    t = SessionTable(path)
    assert t.by_token("tok-b") == "room-b" and t.by_token("tok-c") is None and t.by_token("tok-a ") is None
    text = path.read_text()
    assert "tok-a" not in text and "tok-b" not in text and '"token"' not in text
    assert t.get("room-a") == {"session_id": "s-a", "account": "one", "token_sha256": token_hash("tok-a")}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    t.bind("room-c", "s-c", "one", "tok-c")
    t.update("room-c", token="tok-planted")
    assert "tok-c" not in path.read_text() and "tok-planted" not in path.read_text()
    assert t.by_token("tok-planted") == "room-c" and t.by_token("tok-c") is None


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
    with same:                                                        # bound to two now, its transcript still in one's folder:
        same.start()                                                  # by path again, since two's id search never reaches it
    assert records(tmp_path)[-1]["argv"][records(tmp_path)[-1]["argv"].index("--resume") + 1] == str(transcript)
    own = tmp_path / "cfg" / "projects" / project_dir(tmp_path.resolve()) / f"{sid}.jsonl"
    own.parent.mkdir(parents=True, exist_ok=True)                      # the first start made it
    own.write_text("{}\n")
    SessionTable(tmp_path / "sessions.json").update("r1", transcript=str(own))
    with make() as home:                                              # a transcript in the account's own folder: by id
        home.start()
    assert records(tmp_path)[-1]["argv"][records(tmp_path)[-1]["argv"].index("--resume") + 1] == sid
    own.unlink()
    transcript.unlink()
    with make() as gone:                                              # a hop with no file: the id, and the resume rule decides
        gone.start()
    assert records(tmp_path)[-1]["argv"][records(tmp_path)[-1]["argv"].index("--resume") + 1] == sid


def test_the_hook_error_flag_drops_claude_codes_key_hint():
    t = Turn(started=100.0)
    t.observe(101.0, {"type": "system", "subtype": "notification", "key": "stop-hook-error",
                      "text": "Stop hook error occurred · ctrl+o to see"})
    assert t.flags == ["hook_error:Stop hook error occurred"]


def test_a_turn_ends_on_silence_never_on_length(make):
    """A working turn was once cut at 15 minutes, and the owner wanted no such limit on a turn that
    is still working. The timeout is a silence window: a turn still streaming runs past it; a silent one ends."""
    with make() as rp:
        rp.start()
        talking = rp.ask("CHATTY measure every roof", timeout=1.0)
        assert talking.outcome == "ok" and "CHATTY" in talking.text      # about 2 s long, never 1 s quiet
        silent = rp.ask("HANG", timeout=1.0)
        assert silent.outcome == "timeout"
        rp.end_now()                       # the fake sleeps 60 s: no 15 s close() grace in teardown
