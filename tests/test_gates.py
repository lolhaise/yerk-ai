"""Checks the rule-based gates, checks in code, never a model, that block or hold a room's risky commands and send back a reply that
falls short. The cases (DESIGN 8, decision 18): each gate's control set passes untouched and its blocked case is blocked; the parser
reads quotes and heredocs the way the shell does; the approval gate holds and releases once; the tamper row; the silent-ending
bounce on both of 2026-10-21's filed-instead-of-answered turns; the decision 25 rule's fixture; the hooks'
own time measured over a real local HTTP round trip."""
import asyncio
import json
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone

import pytest

from yerk_ai import evidence, gates, hooks, mcp_server, memindex, memory, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import ConsoleChannel, Yerkd
from yerk_ai.engine import SessionTable
from yerk_ai.room import Inbound

S = gates.Settings(trash=("trash",))
CONTROL_SHELL = [                                   # known-good commands: no gate may touch one
    "ls -la", "git status", "git rm --cached old.txt", "npm rm left-pad", "grep -rn 'rm -rf' .", 'echo "rm a.txt"',
    "echo rm is a word", 'git commit -m "rm the old file, mkfs notes"', "cat > notes.md <<'EOF'\nrm a.txt\nmkfs.ext4 x\nEOF",
    "python -m pytest -q", "sed -n 1,20p yerk_ai/gates.py", "trash -- old.txt", "find . -name '*.pyc'",
    "printf '%s' 'a > /dev/sda'", "kill -9 12345", "systemctl --user status yerkd-test", "git push --dry-run",
    "curl -s http://127.0.0.1:8080/health", 'bash -c "echo ok"', "uv run mypy yerk_ai",
]


def test_the_control_commands_pass_every_shell_gate_untouched(tmp_path):
    priv = tmp_path / "private"
    blocks = [c for c in CONTROL_SHELL if gates.check_shell(c, str(tmp_path), S)
              or gates.restart_hit("Bash", {"command": c}, priv)
              or (gates.external_hit("Bash", {"command": c}, "r", gates.Settings()) and c != "git push --dry-run")]
    assert blocks == []                              # zero blocks on the control set (decision 18)
    assert gates.external_hit("Bash", {"command": "git push --dry-run"}, "r", gates.Settings())  # named, documented


def test_quote_aware_parsing_a_quoted_rm_a_heredoc_rm_and_rm_as_a_word_are_not_deletes():
    for text in ['echo "rm -rf build"', "grep -n 'rm -f' x.py", "cat > f.sh <<EOF\nrm -rf build\nEOF",
                 "cat <<-'END'\n\trm a\n\tEND", "echo the rm command deletes", 'git commit -m "rm x; rm y"']:
        assert not any(gates.is_delete(a) for _, a, _ in gates.shell(text)), text
    assert [a[0] for _, a, _ in gates.shell("cd /tmp && rm a; echo 'x;y' | wc -l")] == ["cd", "rm", "echo", "wc"]
    assert gates.shell("sudo -u me rm -f a")[0][1] == ["rm", "-f", "a"]


def test_a_plain_delete_is_rewritten_to_the_trash_with_the_same_paths(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b c.txt").write_text("x")
    (tmp_path / "d").mkdir()
    v = gates.check_shell("rm a.txt 'b c.txt'", str(tmp_path), S)
    assert v and v.decision == "rewrite" and v.new_input == {"command": "trash -- a.txt 'b c.txt'"}
    assert v.output()["hookSpecificOutput"]["updatedInput"] == {"command": "trash -- a.txt 'b c.txt'"}
    assert "permissionDecision" not in v.output()["hookSpecificOutput"]      # the room's permissions still decide
    assert gates.check_shell("rm -r d", str(tmp_path), S).new_input == {"command": "trash -- d"}
    assert gates.check_shell("unlink a.txt", str(tmp_path), S).new_input == {"command": "trash -- a.txt"}
    assert gates.check_shell("rm -f a.txt gone.txt", str(tmp_path), S).new_input == {"command": "trash -- a.txt"}
    assert gates.check_shell("rm -f gone.txt", str(tmp_path), S).new_input == {"command": "true"}
    for shape in ("rm d", "rm *.txt", "cd /tmp && rm a.txt", "rm $F", "rm gone.txt"):
        assert gates.check_shell(shape, str(tmp_path), S).decision == "deny", shape     # never run as rm
    assert v.line == ""                                                  # the room is not told


def test_hardline_commands_never_run_and_their_quotes_are_prose(tmp_path):
    for c in ("rm -rf ~", "sudo rm -rf /", "rm -rf /etc", "mkfs.ext4 /dev/sda1", "dd if=x of=/dev/sda",
              "cat x > /dev/nvme0n1", ":(){ :|:& };:", "kill -9 -1", "shutdown -h now"):
        v = gates.check_shell(c, str(tmp_path), S)
        assert v and v.gate == "hardline" and v.decision == "deny", c
    for c in ('echo "rm -rf ~"', "echo 'mkfs is scary'", "printf '%s' 'x > /dev/sda'", "echo ':(){ :|:& };:'"):
        assert gates.check_shell(c, str(tmp_path), S) is None, c


class Rec(ConsoleChannel):
    def __init__(self) -> None:
        super().__init__()
        self.notes: list[str] = []

    async def notify(self, room: str, text: str) -> list[str]:
        self.notes.append(text)
        return ["n"]


def setup(tmp_path, conf=None):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    if conf is not None:
        y.gates = gates.settings(conf)
    y.gates.trash = ("trash",)
    ch = Rec()
    room = y.add_room("example-room", ch)
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-g", "one")
    return y, room, ch


def call(y, payload):
    async def go():
        out = await y.hook({"session_id": "sid-g", **payload})
        await asyncio.sleep(0.02)                     # the notice is posted beside the answer
        y.flush_times()                               # and the timing rows are written in batches
        return out
    return asyncio.run(go())


def human_turn(room, text="what does the parser do?", sender="owner"):
    room.busy, room.turn_key = True, "tk-1"
    room.inflight = [Inbound("m1", sender, text)]


def test_the_trash_rewrite_through_the_hook_says_nothing_to_the_room_unless_the_trash_fails(tmp_path):
    y, room, ch = setup(tmp_path)
    work = tmp_path / "rooms" / "example-room" / "work"
    work.mkdir(parents=True)
    (work / "old.txt").write_text("x")
    out = call(y, {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "tu1", "cwd": str(work),
                   "tool_input": {"command": "rm old.txt", "description": "delete it"}})
    assert out["hookSpecificOutput"]["updatedInput"] == {"command": "trash -- old.txt", "description": "delete it"}
    assert ch.notes == []
    call(y, {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_use_id": "tu1", "duration_ms": 12,
             "error": "Exit code 1\ntrash: cannot trash 'old.txt'", "tool_input": {"command": "trash -- old.txt"}})
    assert len(ch.notes) == 1 and ch.notes[0].startswith(f"{words.NOT_DONE} not deleted: old.txt")
    rows = y.ledger.rows(10, "gates")
    assert [(r["gate"], r["decision"]) for r in rows] == [("trash", "failed"), ("trash", "rewrite")]


def test_a_restart_causing_write_is_refused_during_a_human_turn_or_a_rebuild_and_passes_otherwise(tmp_path):
    y, room, ch = setup(tmp_path)
    write = {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_use_id": "w1",
             "tool_input": {"file_path": str(EXAMPLE_PRIVATE / "yerkd.toml"), "content": "[http]\nport = 1\n"}}
    assert call(y, write) == {}                                   # nothing running: it goes ahead
    human_turn(room)
    out = call(y, write)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "would restart Yerk AI" in out["permissionDecisionReason"]
    assert ch.notes == [f"{words.NOT_DONE} not done: a write to yerkd.toml\nit would restart Yerk AI\n"
                        f"{words.IN_PROGRESS}"]
    room.busy, room.inflight = False, []
    y.rebuild_running = lambda: True
    assert call(y, write)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ch.notes[-1].endswith(words.REBUILDING)
    for control in ({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "notes.toml")}},
                    {"tool_name": "Bash", "tool_input": {"command": f"grep -n port {EXAMPLE_PRIVATE}/yerkd.toml"}},
                    {"tool_name": "Bash", "tool_input": {"command": "systemctl --user status yerkd"}}):
        assert call(y, {"hook_event_name": "PreToolUse", **control}) == {}
    assert call(y, {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_input": {"command": "systemctl --user restart yerkd"}})["hookSpecificOutput"]
    for line in ch.notes:
        assert all(words.width(x) <= words.PHONE for x in line.splitlines()), line


def test_the_approval_gate_holds_then_the_owners_word_releases_it_once_ledgered_either_way(tmp_path):
    y, room, ch = setup(tmp_path)
    push = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git push origin main"}}
    out = call(y, push)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"].startswith("Held: a public post")
    assert ch.notes == [f"{words.WORKING} held: a public post\nsay approve here to let it run once\n"
                        "```\ngit push origin main\n```"]                # the exact call the word would release
    room.busy, room.inflight = True, [Inbound("m2", "test-webhook", "approve")]    # not an approver by default
    call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "approve"})
    assert call(y, push)["hookSpecificOutput"]["permissionDecision"] == "deny"     # still held
    room.inflight = [Inbound("m3", "owner", "Approve.")]                    # the owner's word, in the running turn
    ctx = call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "Approve."})["hookSpecificOutput"]
    assert "[approval] owner said approve" in ctx["additionalContext"]
    assert call(y, push) == {}                                            # released, once
    assert call(y, push)["hookSpecificOutput"]["permissionDecision"] == "deny"     # the next one is held again
    got = [(r["gate"], r["decision"]) for r in reversed(y.ledger.rows(20, "gates"))]
    assert got == [("approval", "hold"), ("approval", "hold"), ("approval", "approved"), ("approval", "release"),
                   ("approval", "hold")]
    for control in ("git status", "git commit -m x", "echo 'git push'"):
        assert call(y, {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": control}}) == {}
    assert call(y, {"hook_event_name": "PreToolUse", "tool_name": "mcp__yerk__send",
                    "tool_input": {"room": "example-room", "text": "hi"}}) == {}       # its own room: not external
    assert call(y, {"hook_event_name": "PreToolUse", "tool_name": "mcp__yerk__send",
                    "tool_input": {"room": "family", "text": "hi"}})["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_an_approval_releases_exactly_the_held_call_once_and_a_word_without_the_rooms_binding_counts_for_nothing(
        tmp_path):
    y, room, ch = setup(tmp_path)
    work = tmp_path / "rooms" / "example-room" / "work"
    work.mkdir(parents=True)

    def pre(command, cwd=None):
        p = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command, "description": "x"}}
        return call(y, {**p, "cwd": cwd} if cwd else p)

    def held(out):
        return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def word(sid="sid-g", **extra):
        call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "approve", "session_id": sid, **extra})
    assert held(pre("git push origin main"))
    room.busy, room.inflight = True, [Inbound("m9", "owner", "approve")]     # the owner said it in this turn
    word("sid-posted", cwd=str(work))      # a posted word: the room found by its folder, not by its bound session
    assert held(pre("git push origin main"))                              # nothing was approved
    word()                                 # the room's own session: approved
    word()                                 # the same message again: no second approval
    assert held(pre("git push origin feature"))                           # another call, same rule: held
    assert held(pre("git push origin main", cwd="/tmp"))                  # the same text from another folder: held
    assert pre("git  push 'origin' main") == {}                           # the held call, normalised: released once
    assert held(pre("git push origin main"))                              # and not again
    room.busy, room.inflight = False, []                                  # the turn is over
    word()
    assert held(pre("git push origin feature"))
    rows = y.ledger._select("SELECT * FROM approvals ORDER BY id", ())
    main, feature = "git push origin main", "git push origin feature"
    assert [(r["call"], r["cwd"] == str(work), bool(r["approved"]), bool(r["released"])) for r in rows] == [
        (main, True, False, False), (main, True, True, True), (feature, True, False, False),
        (main, False, False, False), (main, True, False, False), (feature, True, False, False)]
    assert [r["decision"] for r in y.ledger.rows(30, "gates")].count("approved") == 1


def test_approvers_and_external_rules_come_from_yerkd_toml(tmp_path):
    s = gates.settings({"approvers": ["owner", "test-webhook"], "word": "Go", "off": ["public-post"],
                        "external": {"deploy": {"tool": "^Bash$", "command": r"^fly\s+deploy\b", "what": "a deploy"}}})
    assert gates.is_word("go!", "test-webhook", s) and not gates.is_word("go now", "owner", s)
    assert gates.external_hit("Bash", {"command": "git push"}, "r", s) is None
    assert gates.external_hit("Bash", {"command": "fly deploy --now"}, "r", s)[:2] == ("deploy", "a deploy")
    d = gates.Settings()
    for c in ("git -C repo push origin main", "cd repo && git --git-dir=.git push", "gh -R me/x pr create -t y"):
        assert gates.external_hit("Bash", {"command": c}, "r", d)[0] == "public-post", c
    for c in ("git -C repo status", "git log --format=push", "gh pr list"):
        assert gates.external_hit("Bash", {"command": c}, "r", d) is None, c


def test_config_change_writes_a_tamper_row_and_never_blocks(tmp_path):
    y, room, ch = setup(tmp_path)
    f = tmp_path / "settings.json"
    f.write_text('{"hooks": {"Stop": []}}\n')
    ev = {"hook_event_name": "ConfigChange", "source": "project_settings", "file_path": str(f)}
    assert call(y, ev) == {}
    f.write_text('{"hooks": {}}\n{"x": 1}\n')
    assert call(y, ev) == {}
    first, second = reversed(y.ledger.rows(5, "tamper"))
    assert first["sha_before"] is None and first["diff_lines"] == 1 and first["who"] == "example-room:?"
    assert second["sha_before"] and second["sha_after"] != first["sha_after"] and second["diff_lines"] == 3
    assert second["source"] == "project_settings" and second["file"] == str(f) and len(second["diff_sha"]) == 64
    assert "hooks" not in (tmp_path / "tamper").iterdir().__next__().read_text()     # line hashes, never text


def test_a_silent_working_turn_bounces_once_and_only_once_and_a_landed_reply_is_not_sent_again(tmp_path):
    y, room, ch = setup(tmp_path)
    human_turn(room, "did the deploy finish?")
    room.evidence.observe({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "systemctl --user status x"}}]}})
    stop = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": ""}
    out = call(y, stop)
    assert out["decision"] == "block" and '"did the deploy finish?"' in out["reason"]
    assert call(y, {**stop, "stop_hook_active": True}) == {}                  # Claude Code's own once-guard
    assert call(y, stop) == {}                                                # and ours: once per result
    assert [r["rule"] for r in y.ledger.rows(5, "gates")] == ["silent-ending"]
    room._new_card()                                                          # the next result of the turn
    human_turn(room, "and the backup?")
    room.turn_key = "tk-2"
    y.ledger.delivery_plan("tk-2:0", "example-room", "reply", "it finished at noon")
    y.ledger.delivery_set("tk-2:0", state="sent", message_id="900")
    assert call(y, stop) == {}                                                # something landed: not silent
    assert y.ledger.delivery_plan("tk-2:0", "example-room", "reply", "it finished at noon")["state"] == "sent"
    room.turn_key, room.silent_bounced = "tk-3", False
    assert call(y, {"hook_event_name": "Stop", "stop_hook_active": False}) == {}   # key absent: unknown, allow
    assert call(y, {**stop, "last_assistant_message": "Done, it finished."}) == {}
    room.inflight = [Inbound("r1", "yerk", "relay: worker 1 finished")]
    assert call(y, stop) == {}                                                # the harness's own turn


# The stub rule's two turns, invented, each on the same side of every line the rule draws (STUB_CHARS, twice the final's
# length, words.FILED) as the turns it was built on: first a turn is sent a run's result, reads it, works it out, files it
# and ends on "Filed." (what it worked out never reaches the room); then, asked why, it writes the answer above the call,
# files the rule and ends on the rule line.
REPORT_STEPS = [("say", "The export is back with two rows fewer than last week's. Looking at which two before saying more."),
                      ("call", "Read"), ("call", "Bash"), ("call", "Bash"), ("call", "mcp__yerk__remember")]
FILED_ONLY = "Filed. The rest is done too: the invite is sent, the slides are drafted, and only the export job is still running."
              ANSWER_ABOVE = [("say", "Because the summary came first and the table never did. Both times the full table was open, and both "
"times a line on what it meant went out in its place, and that line missed the column that mattered. "
                      "The table was the answer, and it took a second ask to get it.\n\nFrom here a result goes out as its "
                      "table first, then one line on it if it needs one. Filing that as the rule."), ("call", "mcp__yerk__remember")]
                      RULE_LINE = "From now on every room puts the table first and its reading after, in one line."
                      



def stream(room, *steps):
    """One result's stream as the room reads it: the previous result ends, then ("say", text) a text block, ("fold", text)
    a message folded in and replayed, and ("call", name) a tool call with its result, in order."""
    room.evidence.observe({"type": "result", "subtype": "success"})
    for i, (kind, value) in enumerate(steps):
        if kind in ("say", "fold"):
            room.evidence.observe({"type": "assistant", "message": {"content": [{"type": "text", "text": value}]}}
                                  if kind == "say" else {"type": "user", "isReplay": True, "message": {"content": value}})
            continue
        room.evidence.observe({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"s{i}", "name": value, "input": {}}]}})
        room.evidence.observe({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"s{i}", "content": '{"ok": true}'}]}})


def test_a_final_that_only_reports_a_filing_bounces_once_and_so_does_an_answer_left_above_the_call(tmp_path):
    """The stub rule: both turns bounce, each once, each bounce a shadow row `stub` the morning diff counts; a plain
    short reply after a filing call stands, and so does a final whose result's last call ran something."""
    y, room, ch = setup(tmp_path)
    stop = {"hook_event_name": "Stop", "stop_hook_active": False}
    human_turn(room)
    stream(room, *REPORT_STEPS)
    out = call(y, {**stop, "last_assistant_message": FILED_ONLY})
    assert out["decision"] == "block" and '"Filed. The rest is done too: the invite is sent' in out["reason"]
    assert "Write the whole answer as your final message now" in out["reason"]
    assert call(y, {**stop, "stop_hook_active": True, "last_assistant_message": FILED_ONLY}) == {}   # Claude Code's guard
    assert call(y, {**stop, "last_assistant_message": FILED_ONLY}) == {}                             # and ours: once
    room._new_card()                                                          # the next result
    stream(room, *ANSWER_ABOVE)
    assert call(y, {**stop, "last_assistant_message": RULE_LINE})["decision"] == "block"
    rows = [r["detail"] for r in reversed(y.ledger.rows(20, "shadow")) if r["rule"] == "stub"]
    assert rows == [f"{len(FILED_ONLY)} characters after the remember call, opening as a filing",
                    f"{len(ANSWER_ABOVE[0][1])} characters above the remember call, {len(RULE_LINE)} after it"]
    assert y.ledger.shadow_rules(0, time.time() + 1)["stub"] == 2             # the morning diff's count
    for steps, final in (([("say", "Checking the calendar."), ("call", "mcp__yerk__remember")], "Got it, 9:30 then."),
                         ([("say", "Filing it, then the build."), ("call", "mcp__yerk__remember"), ("call", "Bash")],
                          "Filed, and the build is green.")):
        room._new_card()
        stream(room, *steps)
        assert call(y, {**stop, "last_assistant_message": final}) == {}, final
    room._new_card()                                  # an answer above a message folded in is lost all the same
    stream(room, ANSWER_ABOVE[0], ("fold", "and keep it to one line"), ("call", "mcp__yerk__remember"))
    assert call(y, {**stop, "last_assistant_message": "Got it, one line from now on."})["decision"] == "block"


def test_session_start_posts_a_reset_line_only_when_a_new_session_replaced_one(tmp_path):
    y, room, ch = setup(tmp_path)
    work = str(tmp_path / "rooms" / "example-room" / "work")
    call(y, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1", "cwd": work})
    assert ch.notes == []                                                     # the first session ever
    call(y, {"hook_event_name": "SessionStart", "source": "resume", "session_id": "s1", "cwd": work})
    assert ch.notes == []
    call(y, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s2", "cwd": work})
    assert ch.notes == []                                                     # s1 never answered: nothing lost
    y.ledger.record(room="example-room", session_id="s2", outcome="ok")
    call(y, {"hook_event_name": "SessionStart", "source": "clear", "session_id": "s3", "cwd": work})
    assert ch.notes == [f"{words.NOTHING} {words.RESET}"]
    assert [(r["gate"], r["decision"]) for r in reversed(y.ledger.rows(5, "gates"))] == [
        ("session", "reset-quiet"), ("session", "reset")]
    hk = hooks.settings_for("http://127.0.0.1:9/hook")
    assert {k for k, v in hk.items() if v[0]["hooks"][0]["type"] == "command"} == {     # never http: a sandboxed
        "UserPromptSubmit", "Stop", "PreToolUse", "PostToolUse", "PostToolUseFailure", "ConfigChange", "SessionStart"}


def test_post_tool_use_adds_each_calls_time_and_failure_to_the_evidence_ledger(tmp_path):
    y, room, ch = setup(tmp_path)
    human_turn(room)
    room.evidence.observe({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x/a.md"}},
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}]}})
    call(y, {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_use_id": "t1", "duration_ms": 7})
    call(y, {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_use_id": "t2", "duration_ms": 4187,
             "error": "Exit code 1\n1 failed"})
    room.evidence.observe({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "text"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "1 failed"}]}})
    rows = y.ledger._select("SELECT e.*, t.ms, t.failed, t.error FROM evidence e LEFT JOIN tool_times t "   # each call's time
                            "ON e.tool_use_id = t.tool_use_id WHERE e.turn = ? ORDER BY e.id", ("tk-1",))  # and failure
    assert [(r["tool"], r["ms"], r["failed"]) for r in rows] == [("Read", 7, 0), ("Bash", 4187, 1)]
    assert rows[1]["error"].startswith("Exit code 1")


# decision 25: the check is the read
GREPS = [("Grep", {"pattern": "approval", "path": "yerk_ai"}), ("Bash", {"command": "grep -rn approve yerk_ai"}),
         ("Grep", {"pattern": "hold"})]


def turn_of(calls):
    ev = evidence.Evidence()
    for i, (tool, ti) in enumerate(calls):
        ev.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"c{i}", "name": tool,
                                                                  "input": ti}]}})
        ev.observe({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"c{i}",
                                                             "content": "out"}]}})
    return ev


def test_the_first_fixture_nothing_implements_it_with_only_greps_is_flagged_and_with_a_test_run_is_not():
    reply = "I looked through the code for the approval gate. Nothing implements it."
    flagged = evidence.check_claims(reply, turn_of(GREPS), ())
    assert flagged == [("claim", 'behaviour: "Nothing implements it."; tools this turn: Grep x2, Bash(grep) x1')]
    ran = turn_of([*GREPS, ("Bash", {"command": "cd /repo && python -m pytest -q tests/test_gates.py"})])
    assert evidence.check_claims(reply, ran, ()) == []


def test_the_claim_rule_through_shadow_stop_is_ledgered_and_never_blocks(tmp_path):
    y, room, ch = setup(tmp_path)
    human_turn(room, "is the approval gate built?")
    room.evidence = turn_of(GREPS)
    out = call(y, {"hook_event_name": "Stop", "stop_hook_active": False,
                   "last_assistant_message": "Checked: nothing implements it."})
    assert out == {}                                                          # shadow: never a block
    rows = [r for r in y.ledger.rows(10, "shadow") if r["rule"] == "claim"]
    assert len(rows) == 1 and 'behaviour: "Checked: nothing implements it."' in rows[0]["detail"]
    assert "tools this turn: Grep x2, Bash(grep) x1" in rows[0]["detail"]


def test_a_figure_the_room_posted_earlier_in_its_session_counts_as_read_and_what_the_check_never_read_does_not(tmp_path):
    """Step and the: test-reads-it back as 0000-00-00 00:00 plain 00.00% text 000,000 with one more 00:00 line, for
    its turns repeated 99.99% into the next morning, flagged each time as on file in another notes folder or another room's reply.
    A figure the room posted in a reply earlier in the same session counts as read: the Stop check read that reply. What
    the check never read does not count, so its first saying after is checked: narration written before a tool call (in
    the transcript, never posted), the room's note, its send and a failed turn's notice (filed as its own day-log lines, on
    file there and nowhere else), this turn's own reply (posted before its check runs), a reply of an earlier session, and
    a new session's first quote."""
    y, room, ch = setup(tmp_path)
    at = datetime.fromisoformat

    def posted(key, sid, t, text, kind="reply"):  # a part the channel posted: its ledger row, its day-log line, its turn
        if "-s" not in key:                       # a send's key is its turn's, then -s
            y.ledger.record(room="example-room", session_id=sid, turn_key=key, started=t - 30)
        y.ledger.delivery_plan(f"{key}:0", "example-room", kind, text)
        y.ledger.delivery_set(f"{key}:0", state="sent", created=t)
        y.daily.capture("example-room", key, "yerk", text, when=datetime.fromtimestamp(t, timezone.utc))
    posted("t0", "sid-f", at("2026-10-19T21:00:00-04:00").timestamp(), "Run one came in at 4,015 kg.")
    posted("t1", "sid-g", at("2026-10-19T22:21:01-04:00").timestamp(), "Scale two is the best run yet: 148,290 kg against "
           "the weighbridge's 148,304.83, so 99.99% of it.")
    posted("t1-s1", "sid-g", at("2026-10-19T22:21:30-04:00").timestamp(), "The shelf is 2,390 mm.", kind="send")
    posted("t2", "sid-g", at("2026-10-19T22:26:00-04:00").timestamp(), "Not answered: run two stopped at 1,725 rows.", kind="notice")
    y.daily.capture("example-room", "n1", memory.NOTE, "the long shelf is 1,105 mm", when=at("2026-10-19T22:22:00-04:00"))
    y.daily.capture("other-room", "o1", "yerk", "3,260 and 1,472 here too", when=at("2026-10-19T22:30:00-04:00"))   # no line of its own
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    tool = {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "/x/run-two.csv"}}
    (tmp_path / "sid-g.jsonl").write_text("".join(json.dumps(   # Claude Code's rows, stamped in UTC, the last one being written
        {"type": kind, "timestamp": f"{at(t).astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S.000Z}",
         "message": {"role": kind, "content": [{"type": "text", "text": text}, *calls] if kind == "assistant" else text}}) + "\n"
        for t, kind, text, calls in [("2026-10-19T21:53:55-04:00", "user", "how did fable v2 do?", []),
                                     ("2026-10-19T22:20:40-04:00", "assistant", "Checking 3,260 first.", [tool]),
                                     ("2026-10-19T22:21:00-04:00", "assistant", "Scale two: 148,290 kg, 99.99% of the weighbridge.", [])])
        + '{"type": "assistant", "timest')

    def flagged(reply, since, key, sid="sid-g"):
        human_turn(room, "and how close is it?")
        room.turn_key, room.evidence = key, evidence.Evidence()
        room.evidence.started = at(since).timestamp()
        posted(key, sid, room.evidence.started + 20, reply)          # the reply is in the room before its Stop check runs
        last = max([r["id"] for r in y.ledger.rows(1, "shadow")], default=0)
        call(y, {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": reply, "session_id": sid,
                 "transcript_path": str(tmp_path / f"{sid}.jsonl")})
        return sorted(re.match(r"quoted (.+?), on file at", r["detail"])[1] for r in y.ledger.rows(50, "shadow")
                      if r["id"] > last and r["rule"] == "figure")
    assert flagged("Still 99.99% and 148,290 kg. The long shelf is 1,105 mm, then 3,260 and 1,472 and 2,390 and 4,015 and 1,725.",
                   "2026-10-19T22:44:30-04:00", "tk-a") == ["1,105", "1,472", "1,725", "2,390", "3,260", "4,015"]
    assert flagged("99.99% still, the long shelf 1,105 mm, and 3,260 and 2,390.", "2026-10-20T10:34:30-04:00", "tk-b") == []
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-h", "one")
    assert flagged("99.99% still.", "2026-10-20T11:00:00-04:00", "tk-c", "sid-h") == ["99.99%"]   # a new session's first saying


CONTROL_REPLIES = [   # known-good replies with the turn that made them: none may be flagged
    ("The tests pass: 12 passed in 1.4 s.", [("Bash", {"command": "python -m pytest -q"})]),
    ("The daemon is running, up since 14:02.", [("Bash", {"command": "yerk status"})]),
    ("We decided to ship the gates in shadow first.", [("mcp__yerk__recall", {"query": "gates decision"})]),
    ("The file says the port is 8471.", [("Read", {"file_path": "/x/yerkd.toml"})]),
    ("It could be the cache; I would check the ledger first.", []),
    ("Want me to run the probe?", []),
    ("Morning. Three things are due today, the first at 10:00.", [("mcp__yerk__recall", {"query": "due"})]),
    ("Sure, here is a shorter version of that line.", []),
    ("The gate blocks it: the test above shows the refusal.", [("Bash", {"command": "pytest -q -k refusal"})]),
    ("Nothing to do here; the room is quiet.", []),
    ("Not implemented yet, from memory, not checked.", []),
    ("Probably still running; I have not looked.", []),
    ("Answer questions, write, plan, and do code work in the working folder.", []),   # the live control's one
]                                                                                     # false alarm, now fixed


def test_the_claim_rule_raises_no_false_alarm_on_the_control_replies():
    hits = [(r, f) for r, calls in CONTROL_REPLIES for f in evidence.check_claims(r, turn_of(calls), ())]
    assert hits == []


# the hooks' own time: a fake Claude Code posting a turn's events to the real endpoint over local HTTP
@pytest.fixture
def served(tmp_path):
    import uvicorn
    y, room, ch = setup(tmp_path)
    app = mcp_server.build(y, y.port, y.hook).streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=y.port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield y, room
    server.should_exit = True
    t.join(5)


def post(port, payload):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/hook", json.dumps(payload).encode(),
                                 {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_hook_overhead_per_turn_is_under_150_ms_at_the_95th_percentile(served):
    y, room = served
    work = y.state / "rooms" / "example-room" / "work"
    per_turn = []
    for n in range(20):
        human_turn(room, f"question {n}")
        room.turn_key = f"turn-{n}"
        room.seen[f"question {n}"] = room.inflight[0]
        t0 = time.monotonic()
        post(y.port, {"hook_event_name": "UserPromptSubmit", "session_id": "sid-g", "prompt": f"question {n}"})
        for i, cmd in enumerate(("ls", "git status", "grep -rn x .")):
            tid = f"{n}-{i}"
            post(y.port, {"hook_event_name": "PreToolUse", "session_id": "sid-g", "tool_name": "Bash", "cwd": str(work),
                          "tool_use_id": tid, "tool_input": {"command": cmd}})
            post(y.port, {"hook_event_name": "PostToolUse", "session_id": "sid-g", "tool_name": "Bash",
                          "tool_use_id": tid, "duration_ms": 3})
        post(y.port, {"hook_event_name": "Stop", "session_id": "sid-g", "stop_hook_active": False,
                      "last_assistant_message": f"Answer {n}."})
        per_turn.append((time.monotonic() - t0) * 1000)
    per_turn.sort()
    p95 = per_turn[18]                                    # nearest rank of 20
    time.sleep(0.2)
    y.flush_times()
    per = [ms for _, ms in y.ledger.db.execute("SELECT turn, sum(ms) FROM hook_calls WHERE turn != '' GROUP BY turn ORDER BY 2")]
    assert len(per) == 20 and y.ledger.db.execute("SELECT count(*) FROM hook_calls").fetchone()[0] == 160
    assert p95 < 150 and per[18] < 150, (p95, per)        # the hooks' own time per turn, every event summed
