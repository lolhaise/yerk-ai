"""Checks the live progress card a room shows while it works, built from Claude Code's streamed events: narration as it streams,
target in plain words, ticked or crossed, the thinking line only where a room turns it on."""
from types import SimpleNamespace as NS

from yerk_ai.card import FAILED, OK, ROW_MAX, RUNNING, START, Card
from yerk_ai.room import Room


def se(event):
    return {"type": "stream_event", "event": event}


def tool(i, tid, name, args):
    return [se({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": tid, "name": name,
                                                                             "input": {}}}),
            se({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": ""}}),
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": args}]}}]


def result(tid, error=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": error}]}}


def test_the_card_streams_narration_then_rows_ticked_and_crossed():
    c = Card()
    assert c.render() == START
    assert c.feed(se({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})) is False
    c.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Checking the "}}))
    c.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "notes first"}}))
    assert c.render() == "Checking the notes first"
    for ev in tool(1, "a", "Read", {"file_path": "/x/memory/pinned.md"}) + tool(2, "b", "Bash",
                                                                                {"command": "git status --short\nls"}):
        c.feed(ev)
    for ev in tool(3, "c", "WebFetch", {"url": "https://docs.example.org/a?b=1"}) + tool(
            4, "d", "mcp__yerk__send", {"room": "r", "file": "/w/report.pdf"}) + tool(5, "e", "Grep", {"pattern": "owner",
                                                                                                    "path": "/x/rules.md"}):
        c.feed(ev)
    c.feed(result("a"))
    c.feed(result("b", error=True))
    lines = c.render().splitlines()
    assert lines == ["Checking the notes first", f"{OK} read pinned.md", f"{FAILED} bash git status --short",
                     f"{RUNNING} webfetch docs.example.org", f"{RUNNING} send file report.pdf",
                     f'{RUNNING} grep "owner" in rules.md']


def test_rows_stay_short_and_the_newest_win():
    c = Card()
    for i in range(20):
        for ev in tool(i, f"t{i}", "Bash", {"command": "echo " + "x" * 200}):
            c.feed(ev)
    lines = c.render().splitlines()
    assert all(len(x) <= ROW_MAX for x in lines) and lines[1] == "-# 8 earlier steps" and len(lines) == 14


def test_a_row_gets_its_target_from_the_streamed_input_alone():
    c = Card()
    c.feed(se({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "z", "name": "Glob"}}))
    c.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta",
                                                                    "partial_json": '{"pattern": "**/*.md"}'}}))
    c.feed(se({"type": "content_block_stop", "index": 0}))
    assert c.render().splitlines()[1] == f"{RUNNING} glob **/*.md"


def test_the_thinking_line_shows_only_where_the_room_turns_it_on():
    events = [se({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
              se({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "y" * 300}})]
    on, off = Card(thinking=True), Card()
    for ev in events:
        on.feed(ev)
        off.feed(ev)
    assert on.render().splitlines()[1] == "-# thinking: " + "y" * 200 + "..."
    assert off.render() == START                                       # the default: no thinking line


def test_a_card_with_nothing_to_show_says_what_was_owed():
    c = Card()
    for ev in tool(0, "a", "Read", {"file_path": "/x/a.md"}):
        c.feed(ev)
    first = c.owed("the turn ended error (auth)", "what units does the owner prefer, check the file please")
    assert first.splitlines()[0].startswith(f'{FAILED} No reply to "what units does the owner prefer, check the file ')
    assert first.splitlines()[1] == f"{RUNNING} read a.md"


def test_an_image_never_read_bounces_once_at_stop(tmp_path):
    room = Room.__new__(Room)
    room.lock, room.rp, room.bounced = __import__("threading").Lock(), NS(read_paths=set()), False
    room.images = {"/in/5/photo.png": "photo.png"}
    reason = room.stop_check(active=False)
    assert reason and "photo.png" in reason and "/in/5/photo.png" in reason
    assert room.stop_check(active=False) is None                       # once per result
    room.bounced = False
    assert room.stop_check(active=True) is None                        # Claude Code's own loop guard
    room.rp.read_paths.add("/in/5/photo.png")
    assert room.stop_check(active=False) is None                       # read: nothing to say
