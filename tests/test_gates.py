"""Checks the rule-based gates, checks in code, never a model, that block or hold a room's risky commands and send back a reply that
falls short. The cases (DESIGN 8, decision 18): each gate's control set passes untouched and its blocked case is blocked; the parser
reads quotes and heredocs the way the shell does; the approval gate holds and releases once; the tamper row; the silent-ending
bounce once and only once; the stub bounce on both of 2026-10-21's filed-instead-of-answered turns; the note bounce on 2026-10-29's
reply; the decision 25 rule's fixture; the hooks' own time measured over a real local HTTP round trip."""
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
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
from yerk_ai.engine import SessionTable
from yerk_ai.room import DIRECTIVE, NOTE_CHARS, SHAPE, YOU, Inbound

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


def test_a_hardline_command_is_refused_in_every_shape_a_shell_runs_it(tmp_path):
    # cleanup F5-15: the hardline rules read what would really run too (runs), not only the segments the old splitter finds
    for c in ("timeout 5 kill -9 -1", "bash -c 'kill -9 -1'", "/sbin/reboot", "echo $(shutdown -h now)", "xargs -a pids kill -9 -1",
              "doas mkfs.ext4 /dev/sdb", "sh -c 'rm -rf /'", "timeout 9 rm -rf ~"):
        v = gates.check_shell(c, str(tmp_path), S)
        assert v and v.gate == "hardline" and v.decision == "deny", c
    for c in ('bash -c "echo kill -9 -1"', "timeout 5 kill -9 12345", "sh -c 'git log --format=%s | grep reboot'",
              "git commit -q -m \"$(cat <<'EOF'\nshutdown now waits for the turn\nkill -9 -1 is refused\nEOF\n)\""):
        v = gates.check_shell(c, str(tmp_path), S)
        assert not (v and v.gate == "hardline"), c


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


def test_a_shell_write_to_a_start_up_file_in_a_nested_shell_or_by_its_relative_name_is_refused_during_a_human_turn(tmp_path):
    y, room, ch = setup(tmp_path)                 # cleanup F5-09: the restart rule reads the write as it would run, in its folder
    human_turn(room)
    for command, cwd in ((f'bash -c "echo x >> {EXAMPLE_PRIVATE}/rooms.toml"', tmp_path), ("sed -i s/a/b/ yerkd.toml", EXAMPLE_PRIVATE)):
        out = call(y, {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", command
    assert len(ch.notes) == 2 and all(words.IN_PROGRESS in line for line in ch.notes)


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


def test_a_bare_approve_never_approves_a_text_hold_and_the_rooms_other_hold_still_is(tmp_path):
    """texting-5: a held text's hold is filed in the report room's name (rule text-thread) and only a word on its card releases
    it; a bare `approve` there approves the room's newest other hold as before, and never a text, older or newer."""
    y, room, ch = setup(tmp_path)
    text = y.ledger.hold_add("example-room", "text-thread", "a text to Alex", 0, call="mcp__yerk__text {}")
    room.busy, room.inflight = True, [Inbound("m3", "owner", "approve")]
    call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "approve"})
    assert y.ledger._select("SELECT approved FROM approvals WHERE id = ?", (text,)) == [{"approved": None}]
    push = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git push origin main"}}
    assert call(y, push)["hookSpecificOutput"]["permissionDecision"] == "deny"
    y.ledger.hold_add("example-room", "text-thread", "a text to Beta", 0, call='mcp__yerk__text {"x": 1}')   # newer than the push
    room.inflight = [Inbound("m4", "owner", "approve")]
    call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "approve"})
    assert call(y, push) == {}                                            # the push, released once
    assert [r["approved"] for r in y.ledger._select("SELECT * FROM approvals WHERE rule = 'text-thread'", ())] == [None, None]


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
        (main, True, True, True), (main, True, False, False), (feature, True, False, False),     # the word approves the hold that
        (main, False, False, False), (main, True, False, False), (feature, True, False, False)]  # was there when it was written
    assert [r["decision"] for r in y.ledger.rows(30, "gates")].count("approved") == 1


def test_a_tools_approval_holds_whatever_folder_the_shell_is_in_and_a_shell_commands_only_in_its_own(tmp_path):
    """need 78 of 2026-11-09: a call held with the shell in one folder and tried again from another, as a session resumed
    after a /compact is, back in its work folder. A tool's input is its whole call, so the word releases it there; a shell
    command's words do another thing in another folder (gates.bound_to), so its word holds only where it was held."""
    y, room, ch = setup(tmp_path)
    work = tmp_path / "rooms" / "example-room" / "work"
    send = {"tool_name": "mcp__yerk__send", "tool_input": {"room": "family", "text": "hi"}}
    push = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}

    def pre(c, cwd):
        return call(y, {"hook_event_name": "PreToolUse", **c, "cwd": str(cwd)})

    def held(out):
        return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def word(mid):
        room.busy, room.inflight = True, [Inbound(mid, "owner", "approve")]
        call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "approve"})
    assert held(pre(send, work / "repo"))
    word("m1")
    assert pre(send, work) == {} and held(pre(send, work / "repo"))       # released from another folder, once
    assert held(pre(push, work / "repo"))
    word("m2")
    assert held(pre(push, work)) and pre(push, work / "repo") == {}        # a command: only from its own folder
    assert gates.bound_to("Bash", "/x") == "/x" and gates.bound_to("mcp__yerk__text", "/x") is None
    assert [r["decision"] for r in reversed(y.ledger.rows(20, "gates"))] == [
        "hold", "approved", "release", "hold", "hold", "approved", "hold", "release"]


def test_approvers_and_external_rules_come_from_yerkd_toml(tmp_path):
    s = gates.settings({"approvers": ["owner", "test-webhook"], "word": "Go", "off": ["public-post"],
                        "external": {"deploy": {"tool": "^Bash$", "command": r"^fly\s+deploy\b", "what": "a deploy"}}})
    assert gates.is_word("go!", "test-webhook", s) and gates.is_word("go now", "owner", s)     # the word leads
    assert not gates.is_word("now go", "owner", s) and not gates.is_word("go", "stranger", s)
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
    short reply after a filing call stands, so does one opening "Noted" (a whole answer, bounced that day while
    "noted" was an opener word), and so does a final whose result's last call ran something."""
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
                         ([("say", "Logging that."), ("call", "mcp__yerk__remember")],
                          "Noted, and that moves the review to Thursday with the same three people on it."),
                         ([("say", "Filing it, then the build."), ("call", "mcp__yerk__remember"), ("call", "Bash")],
                          "The build is green.")):
        room._new_card()
        stream(room, *steps)
        assert call(y, {**stop, "last_assistant_message": final}) == {}, final
    room._new_card()                                  # an answer above a message folded in is lost all the same
    stream(room, ANSWER_ABOVE[0], ("fold", "and keep it to one line"), ("call", "mcp__yerk__remember"))
    assert call(y, {**stop, "last_assistant_message": "Got it, one line from now on."})["decision"] == "block"


def test_a_no_reply_final_is_never_bounced_and_an_empty_one_still_is(tmp_path):
    """watch-note: NO_REPLY is the whole answer of a room reading along with nothing to add (room.py posts nothing), so no Stop
    rule sends it back: not the silent-ending rule, which still bounces an empty final once, and not the stub rule after a
    filing, which still bounces an answer written above the call (the second turn)."""
    y, room, ch = setup(tmp_path)
    stop = {"hook_event_name": "Stop", "stop_hook_active": False}
    human_turn(room, "squats felt heavy today")
    stream(room, *ANSWER_ABOVE)                         # a long text above a filing call, then the final
    assert call(y, {**stop, "last_assistant_message": "NO_REPLY\n"}) == {}
    out = call(y, {**stop, "last_assistant_message": ""})
    assert out["decision"] == "block" and '"squats felt heavy today"' in out["reason"]
    room._new_card()
    stream(room, *ANSWER_ABOVE)
    assert call(y, {**stop, "last_assistant_message": RULE_LINE})["decision"] == "block"
    assert [r["rule"] for r in y.ledger.rows(5, "gates")] == ["silent-ending"]


# The note rule's fixtures, invented in the shapes measured over 2,571 finals: a reply with its plan above the answer, the
# correction after it, an answer about a colleague (its first two paragraphs), advice to the owner on their own message and a
# rulebook tag alone. A name is only ever in a second paragraph, and the rule reads the first.
PLAN_ABOVE = ("They are reading the plan back to check it. Confirm in one line and fix the one slip: the meeting moved to "
             "the east hall, the time stays. Landed: a yes and the fix, nothing more.\n\n"
             "Nearly. The time is the same, but the meeting is in the east hall now, not the west one, since the west hall "
             "is booked for the fair that week. So the plan reads right with the hall changed.")
             OWN_CORRECTION = ("The line above was my own working, sent by mistake; please skip it.\n\n"
             "The plan is right with one change: the meeting is in the east hall, at the same time.")
ABOUT_A_COLLEAGUE = ("They want the parcel sent on Monday to land before Friday. That is all they asked for, start to end.\n\n"
              "From the shop file: the shop has 300 example boxes and 12 are out.")
              

                   
ADVICE_FINAL = "Keep your reply to two lines: the date and the ask, then stop.\n\nHe reads it on his phone between meetings."
TAG_FINAL = "\"Landed: 'Yes. Save.'\" style: confirm and move."


def test_a_note_to_itself_before_the_answer_bounces_once_and_an_answer_that_only_looks_like_one_stands(tmp_path):
    """The note rule (seen live: a turn with no thinking wrote its plan for the reply above the answer, and the room
    posted both): the reply bounces once, a shadow row `note` the morning diff counts, and so does a rulebook tag alone; the
    model's own correction, an answer about a colleague, advice to the owner on their own message and NO_REPLY stand."""
    y, room, ch = setup(tmp_path)
    stop = {"hook_event_name": "Stop", "stop_hook_active": False}
    human_turn(room)
    assert len(PLAN_ABOVE.split("\n\n")[0]) <= NOTE_CHARS and "\n\n" in PLAN_ABOVE      # a note the rule reads, an answer after it
    assert call(y, {**stop, "stop_hook_active": True, "last_assistant_message": PLAN_ABOVE}) == {}   # Claude Code's guard
    out = call(y, {**stop, "last_assistant_message": PLAN_ABOVE})
    note = PLAN_ABOVE.split("\n\n")[0]
    assert out["decision"] == "block" and f'("{" ".join(note.split())[:80]}")' in out["reason"]
    assert "Thinking is where that goes" in out["reason"] and "Write the answer alone as your final message now" in out["reason"]
    assert call(y, {**stop, "last_assistant_message": PLAN_ABOVE}) == {}                            # and ours: once
    room._new_card()                                                          # the next result: one paragraph, a tag
    assert call(y, {**stop, "last_assistant_message": TAG_FINAL})["decision"] == "block"
    rows = [r["detail"] for r in reversed(y.ledger.rows(20, "shadow")) if r["rule"] == "note"]
    assert rows == [f"a {len(note)}-character note before the answer: a directive and a shape word, and a rulebook tag",
                    f"a {len(TAG_FINAL)}-character note before the answer: a rulebook tag"]
    assert y.ledger.shadow_rules(0, time.time() + 1)["note"] == 2             # the morning diff's count
    advice = ADVICE_FINAL.split("\n\n")[0]
    assert DIRECTIVE.search(advice) and SHAPE.search(advice) and YOU.search(advice)   # it stands on "your" alone
    for steps, final in (([], OWN_CORRECTION), ([("call", "Read")], ABOUT_A_COLLEAGUE),   # that turn read the file it cites
                         ([], ADVICE_FINAL), ([], words.NO_REPLY), ([], TAG_FINAL + " " + "x" * NOTE_CHARS)):
        room._new_card()
        stream(room, *steps)
        assert call(y, {**stop, "last_assistant_message": final}) == {}, final
    assert y.ledger.shadow_rules(0, time.time() + 1)["note"] == 2


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
    """Queue for its: turn and the last 00.00% part 000,000 closes the case, before
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
    posted("t0", "sid-f", at("2026-10-19T21:00:00-04:00").timestamp(), "In the queue for its 0,000 turn.")
    posted("t1", "sid-g", at("2026-10-19T22:21:01-04:00").timestamp(), "Entry waits in the queue for its: 000,000 turn and "
           "text with 000,000.00, one 00.00% more line.")
    posted("t1-s1", "sid-g", at("2026-10-19T22:21:30-04:00").timestamp(), "Text with one 0,000 more.", kind="send")
    posted("t2", "sid-g", at("2026-10-19T22:26:00-04:00").timestamp(), "Not answered: run two stopped at 1,725 rows.", kind="notice")
    y.daily.capture("example-room", "n1", memory.NOTE, "says the sample item 0,000 moved", when=at("2026-10-19T22:22:00-04:00"))
    y.daily.capture("other-room", "o1", "yerk", "3,260 and 1,472 here too", when=at("2026-10-19T22:30:00-04:00"))   # no line of its own
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    tool = {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "/x/run-two.csv"}}
    (tmp_path / "sid-g.jsonl").write_text("".join(json.dumps(   # Claude Code's rows, stamped in UTC, the last one being written
        {"type": kind, "timestamp": f"{at(t).astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S.000Z}",
         "message": {"role": kind, "content": [{"type": "text", "text": text}, *calls] if kind == "assistant" else text}}) + "\n"
        for t, kind, text, calls in [("2026-10-19T21:53:55-04:00", "user", "how did fable v2 do?", []),
                                     ("2026-10-19T22:20:40-04:00", "assistant", "Checking 3,260 first.", [tool]),
                                     ("2026-10-19T22:21:00-04:00", "assistant", "Starts the: 000,000 example, 00.00% note says the.", [])])
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
    assert flagged("Note 00.00% says 000,000 the. Sample item moved to 0,000 the, next 0,000 step 0,000 and 0,000 the 0,000 test 0,000.",
                   "2026-10-19T22:44:30-04:00", "tk-a") == ["1,105", "1,472", "1,725", "2,390", "3,260", "4,015"]
    assert flagged("00.00% line, for the check 0,000 while, a 0,000 second 0,000.", "2026-10-20T10:34:30-04:00", "tk-b") == []
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


# the board rule (2026-10-21: the owner rooms' status board went out in a person's room); a synthetic board, no one's words
BOARD_FINAL = "\n".join(["Two of the three are set; the third waits on this room.",
                         "☑ The library reminder is set for Monday at 10am",
                         "☐ 🟦 The follow-up check goes in on my next turn",
                         "⛔ 🟨 **Photo album:** this room can't make files yet. Unblocks: allow files here",
                         "> The prints are saved in your notes",
                         "-# 🟦 me · 🟨 you"])
PLAIN_FINAL = ("Two of the three are set: the library reminder comes Monday at 10am, and the follow-up check goes in "
               "next. The photo album has to wait until this room can make files.")


def test_a_status_board_in_a_room_with_people_bounces_once_and_never_in_an_owner_room_while_the_list_is_empty(tmp_path):
    """The board rule: a final with the board's marks in a room with people bounces once, as a shadow row `board`; plain
    sentences stand, and an owner room's board never bounces while [gates] retired_marks is empty, its default."""
    y, room, ch = setup(tmp_path)
    assert y.gates.retired == ()
    stop = {"hook_event_name": "Stop", "stop_hook_active": False}
    human_turn(room)
    assert call(y, {**stop, "last_assistant_message": BOARD_FINAL}) == {}                       # the owner's room, no list
    room.conf = {**room.conf, "people": [7]}                    # a person besides the owner
    room._new_card()
    assert room.card.people == ""                                             # its card the owner rooms' shape (card-default)
    human_turn(room, "is the library one set?", sender="person")
    out = call(y, {**stop, "last_assistant_message": BOARD_FINAL})
    assert out["decision"] == "block" and "two or three plain sentences" in out["reason"]
    assert "4 lines with a checklist mark, the colour key or what unblocks it" in out["reason"]
    assert call(y, {**stop, "stop_hook_active": True, "last_assistant_message": BOARD_FINAL}) == {}   # Claude Code's guard
    assert call(y, {**stop, "last_assistant_message": BOARD_FINAL}) == {}                            # and ours: once
    rows = [r["detail"] for r in reversed(y.ledger.rows(20, "shadow")) if r["rule"] == "board"]
    assert rows == ["4 lines with a checklist mark, the colour key or what unblocks it"]
    assert y.ledger.shadow_rules(0, time.time() + 1)["board"] == 1             # the morning diff's count
    room._new_card()                                                          # the next result
    human_turn(room, "and the album?", sender="person")
    assert call(y, {**stop, "last_assistant_message": PLAIN_FINAL}) == {}
    for line in BOARD_FINAL.splitlines()[1:4] + ["- ☐ a dashed item", "it unblocks: the files"]:
        assert words.BOARD_LINE.search(line), line
    for line in ("> The prints are saved in your notes", "Book it in mid-July, about two months ahead.", "A 🟦 blue door"):
        assert not words.BOARD_LINE.search(line), line


# the retired-marks rule ([gates] retired_marks), in every room without people; a synthetic list, no one's words: the marks
# after a list number, where BOARD_LINE never looks, and a colour key under the list
MARKS = {"retired_marks": ["☐", "☑", "🟦", "🟨"]}
SQUARES_FINAL = "\n".join(["Where the move stands:",
                           "1. 🟦 **Van booked.** It comes Saturday at 9am",
                           "2. 🟨 **Keys back.** The old keys go to the office by Friday",
                           "-# 🟦 me · 🟨 you"])
NUMBERED_FINAL = "\n".join(SQUARES_FINAL.splitlines()[:3])                     # the same list with no key line
LIST_FINAL = "1. **Van booked.** I booked it for Saturday at 9am.\n2. **Keys back.** You take the old keys to the office by Friday."


def test_a_retired_mark_bounces_once_in_an_owner_room_and_once_in_the_machine_room_when_the_list_holds_one(tmp_path):
    """The retired-marks rule: with [gates] retired_marks set, a final holding one of them in a room without people bounces
    once before it is posted, as a shadow row `retired`; the second attempt goes out as written, and a plain list stands."""
    y, room, ch = setup(tmp_path, MARKS)
    assert y.gates.retired == ("☐", "☑", "🟦", "🟨")
    stop = {"hook_event_name": "Stop", "stop_hook_active": False}
    for conf in (room.conf, {**room.conf, "role": "machine"}):                  # an owner room, then the machine room
        room.conf = conf
        room._new_card()
        human_turn(room)
        out = call(y, {**stop, "last_assistant_message": SQUARES_FINAL})
        assert out["decision"] == "block" and out["reason"] == words.RETIRED.format(found="3 lines with 🟦 🟨")
        assert "plain numbered list" in out["reason"] and out["reason"].endswith("write that as your final message now.")
        assert call(y, {**stop, "stop_hook_active": True, "last_assistant_message": SQUARES_FINAL}) == {}   # Claude Code's guard
        assert call(y, {**stop, "last_assistant_message": SQUARES_FINAL}) == {}                            # and ours: once
    rows = [r["detail"] for r in reversed(y.ledger.rows(20, "shadow")) if r["rule"] == "retired"]
    assert rows == ["3 lines with 🟦 🟨"] * 2
    assert y.ledger.shadow_rules(0, time.time() + 1)["retired"] == 2             # the morning diff's count
    room._new_card()                                                            # the next result
    human_turn(room)
    assert call(y, {**stop, "last_assistant_message": LIST_FINAL}) == {}         # what it asks for stands


def test_the_numbered_squares_with_no_key_line_bounce_and_the_bounce_says_how_many_lines(tmp_path):
    """A mark counts anywhere in a line: after a list number (the shape the board's BOARD_LINE never sees), after a dash, mid
    line, with or without its emoji variation selector; each line holding one counts once, and the bounce says how many."""
    y, room, ch = setup(tmp_path, MARKS)
    assert not any(words.BOARD_LINE.search(x) for x in NUMBERED_FINAL.splitlines())
    human_turn(room)
    out = call(y, {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": NUMBERED_FINAL})
    assert out["decision"] == "block" and "(2 lines with 🟦 🟨)" in out["reason"]
    for text, found in (("- ☐ a dashed item", "1 line with ☐"), ("Done ☑ and next ☐ on one line", "1 line with ☐ ☑"),
                        ("☑\ufe0f with its selector\nplain\n🟨 and a third", "2 lines with ☑ 🟨")):
        room._new_card()
        assert room.retired_check(False, text, y.gates.retired) == (words.RETIRED.format(found=found), found), text


def test_a_retired_mark_inside_a_fenced_block_does_not_count(tmp_path):
    """A fenced block (an inline one, and an unclosed one running to the end, as memory.fenced reads it) is never counted;
    a mark outside it still is."""
    y, room, ch = setup(tmp_path, MARKS)
    logged = "The queue reads:\n```log\n🟦 job 4 queued\n☑ job 3 done\n```\nNothing else changed."
    human_turn(room)
    assert call(y, {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": logged}) == {}
    for text in ("Inline ```🟦 here``` only", "An open block:\n```\n🟦 runs to the end"):
        room._new_card()
        assert room.retired_check(False, text, y.gates.retired) is None, text
    room._new_card()
    assert room.retired_check(False, f"{logged}\n🟨 one outside", y.gates.retired)[1] == "1 line with 🟨"


def test_an_empty_list_means_no_bounce_and_the_list_must_be_a_list_of_marks(tmp_path):
    y, room, ch = setup(tmp_path, {"retired_marks": []})
    assert y.gates.retired == () == gates.settings({}).retired == gates.settings(None).retired
    human_turn(room)
    assert call(y, {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": SQUARES_FINAL}) == {}
    assert room.retired_check(False, SQUARES_FINAL, ()) is None
    assert not [r for r in y.ledger.rows(20, "shadow") if r["rule"] == "retired"]
    assert gates.settings({"retired_marks": ["☑\ufe0f", " ", ""]}).retired == ("☑",)      # its selector off, a blank dropped
    with pytest.raises(SystemExit, match="must be a list of marks"):
        gates.settings({"retired_marks": "🟦 me"})                              # one string would be read letter by letter


def test_a_room_with_people_keeps_the_board_rule_unchanged_with_the_list_set(tmp_path):
    y, room, ch = setup(tmp_path, MARKS)
    room.conf = {**room.conf, "people": [7]}
    room._new_card()
    stop = {"hook_event_name": "Stop", "stop_hook_active": False}
    human_turn(room, "is the van set?", sender="person")
    assert call(y, {**stop, "last_assistant_message": NUMBERED_FINAL}) == {}       # the board's marks miss it, as before
    room._new_card()
    human_turn(room, "and the album?", sender="person")
    out = call(y, {**stop, "last_assistant_message": BOARD_FINAL})
    assert out["reason"] == words.BOARD.format(found="4 lines with a checklist mark, the colour key or what unblocks it")
    assert {r["rule"] for r in y.ledger.rows(20, "shadow")} & {"board", "retired"} == {"board"}
    room._new_card()
    assert room.retired_check(False, SQUARES_FINAL, y.gates.retired) is None      # never in a room with people


def test_the_owners_word_counts_alone_or_as_the_first_word_and_never_inside_a_sentence():
    s = gates.settings({})
    yes = ("approve", "Approve.", "APPROVE!", "approve the post", "Approve, go ahead")
    no = ("", "I approve", "don't approve", "approved", "approves it", "the approve")
    assert all(gates.is_word(p, "owner", s) for p in yes)
    assert not any(gates.is_word(p, "owner", s) for p in no)
    assert not gates.is_word("approve the post", "test-webhook", s)      # not an approver


def test_an_owner_rooms_send_to_the_machine_room_needs_no_word_and_a_family_rooms_still_does():
    d = gates.Settings()
    rooms = {"main": {"privacy": "owner"}, "family-room": {"privacy": "family"}, "ops": {"role": "machine"}, "other": {}}

    def send(frm, to, r=rooms):
        return gates.external_hit("mcp__yerk__send", {"room": to, "text": "x"}, frm, d, r)

    assert send("main", "ops") is None                          # the owner's own line to the machine room
    assert send("family-room", "ops")[0] == "send-elsewhere"              # a family room's still holds
    assert send("main", "other")[0] == "send-elsewhere"         # any other room still holds
    assert send("main", "ops", None)[0] == "send-elsewhere"     # no rooms.toml in hand: unchanged


def test_a_final_holding_a_filing_report_bounces_once_with_the_readability_sentence_and_the_rest_kept(tmp_path):
    """The filing rule (2026-11-09, the owner: a rule rewritten, then "Filed: ..." told back): after a filing call, a final
    holding a filing report bounces once, quoting the Claude docs' readability sentence and asking for the rest of the answer;
    a final with no report stands, and so does one in a result that filed nothing."""
    y, room, ch = setup(tmp_path)
    stop = {"hook_event_name": "Stop", "stop_hook_active": False}
    told = "The build is green and the review moved to Thursday.\n\nFiled: rules.md:115, the rule now says so."
    for final in (told, "The build is green. I've filed it under the release job.", "Logged, and the build is green."):
        room._new_card()
        stream(room, ("say", "Filing it, then the build."), ("call", "mcp__yerk__remember"), ("call", "Bash"))
        out = call(y, {**stop, "last_assistant_message": final})
        assert out["decision"] == "block" and "for a reader who didn't see any of that" in out["reason"], final
        assert "every word of it but the filing report" in out["reason"]
        assert call(y, {**stop, "last_assistant_message": final}) == {}                             # once
    assert sorted(r["detail"] for r in y.ledger.rows(20, "shadow") if r["rule"] == "filing") == [
        "a filing report: Filed: rules.md:115, the rule now says so.", "a filing report: Logged, and the build is green.",
        "a filing report: The build is green. I've filed it under the release job."]
    room._new_card()
    stream(room, ("say", "Checking."), ("call", "Bash"))
    assert call(y, {**stop, "last_assistant_message": "Filed under taxes, the 2025 receipts are in the blue folder."}) == {}
    room._new_card()
    stream(room, ("say", "Filing it."), ("call", "mcp__yerk__remember"))
    assert call(y, {**stop, "last_assistant_message": "The review is Thursday at 9:30 with the same three people."}) == {}
