"""Checks the paste rule, which keeps a room from posting an old block again as if it were new: a fenced block (text between ``` lines) equal, in the
day log's own shape, to one the same room posted on an earlier day of the last 14 is turned back once per result, in the `send` tool before it posts
and in the final text at Stop, with one view, one set of words, one `paste` shadow row and one flag. The case behind it (2026-10-22): a block posted
the evening before went out again through `send`, word for word, its "tomorrow" and its times that day's. A synthetic plan, no one's words."""
import asyncio
import calendar
import json
import time
from datetime import datetime, timedelta

import test_gates
from test_gates import call, human_turn
from test_observe import NOW, layer

from yerk_ai import memory, morning, status, words
from yerk_ai.card import FAILED, OK, Card
from yerk_ai.config import EXAMPLE_PRIVATE, REPO
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger

BLOCK = ["Tomorrow (Thursday)", "09:30  the team meets the vendor at the north office",
         "11:00  the vendor call, the lead runs it", "14:00  the draft review, both teams"]
PLAN = "\n".join(["Here is tomorrow's plan as it stands:", "```text", *BLOCK, "```", "Say if anything moved."])
# the same block as the log files it: another tag, blank lines, tabs, indents and runs of spaces, other words around it
AGAIN = "\n".join(["Morning. The plan again:", "```", "", f"  {BLOCK[0]}", BLOCK[1].replace("  ", "\t"), "",
                   BLOCK[2].replace(" ", "   "), f"{BLOCK[3]}   ", "```"])
STOP = {"hook_event_name": "Stop", "stop_hook_active": False}


def setup(tmp_path):
    """test_gates' owner room with a claims sheet written in its turn, so the sheet rule (test_sheet_hard.py), which turns
    back a pasted block with none, stays out of the paste rule's tests."""
    y, room, ch = test_gates.setup(tmp_path)
    room.evidence.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "s0", "name": "Write",
                                                                         "input": {"file_path": "/w/claims.md"}}]}})
    return y, room, ch


def posted(y, room, text, days_ago, at=(19, 22, 19), mid=None):
    """`text` filed as the room's own post `days_ago` days back (as the Discord adapter files every part it posts); the
    day as the bounce names it."""
    day = datetime.now(y.memory.tz) - timedelta(days=days_ago)
    when = day.replace(hour=at[0], minute=at[1], second=at[2], microsecond=0)
    y.daily.capture(room, mid or f"p{days_ago}{at[0]}{len(text)}", "yerk", text, when=when)
    return f"{calendar.month_abbr[when.month]} {when.day}"


def send(y, text, to="example-room", caller="example-room"):
    return asyncio.run(y.tool_send(to, text, "", caller))


def test_1_2_5_the_real_pairs_shape_is_turned_back_once_in_send_and_once_at_stop_in_the_same_words(tmp_path):
    """1: yesterday's block sent again today through `send` is refused once, naming the day and the time; the room's next
    send in the turn goes out. 5: the final of that same result is not bounced again (one flag). 2: the next result's
    final holding it bounces once at Stop, in the very words the send got."""
    y, room, ch = setup(tmp_path)
    day = posted(y, "example-room", PLAN, 1)
    human_turn(room)
    out = send(y, AGAIN)
    assert out["ok"] is False and out["error"] == words.PASTE.format(when=f"{day} at 19:22")
    assert "line by line against today (relative dates, clock times, who meets whom and when)" in out["error"]
    assert ch.n == 0                                                           # nothing reached the room
    assert send(y, AGAIN)["ok"] is True and ch.n == 1                          # the next send goes out, whatever it holds
    assert call(y, {**STOP, "last_assistant_message": AGAIN}) == {}            # 5: the same result's final, the same flag
    room._new_card()                                                          # the next result
    human_turn(room)
    stop = call(y, {**STOP, "last_assistant_message": PLAN})
    assert stop == {"decision": "block", "reason": out["error"]}              # 2: one set of words
    assert call(y, {**STOP, "stop_hook_active": True, "last_assistant_message": PLAN}) == {}   # Claude Code's guard
    assert call(y, {**STOP, "last_assistant_message": PLAN}) == {}                             # and ours: once


def test_3_4_a_fresh_block_and_one_already_posted_today_go_out(tmp_path):
    y, room, ch = setup(tmp_path)
    posted(y, "example-room", PLAN, 1)
    human_turn(room)
    fresh = PLAN.replace("north office", "south office")
    assert send(y, fresh)["ok"] is True                                        # 3: not refused
    assert call(y, {**STOP, "last_assistant_message": fresh}) == {}            # 3: not bounced
    (tmp_path / "today").mkdir()
    y2, room2, ch2 = setup(tmp_path / "today")
    posted(y2, "example-room", PLAN, 0, at=(8, 0, 0))                          # 4: posted this morning
    human_turn(room2)
    assert send(y2, AGAIN)["ok"] is True
    assert call(y2, {**STOP, "last_assistant_message": AGAIN}) == {}


def test_6_one_character_is_a_new_block_and_whitespace_or_blank_lines_are_the_same_one(tmp_path):
    (one,) = memory.fenced(PLAN)
    assert memory.fenced(AGAIN) == [one] and one == " / ".join(" ".join(x.split()) for x in BLOCK)
    assert memory.fenced(PLAN.replace("```text", "```")) == [one]              # the tag is not the block
    assert memory.fenced(PLAN.replace("09:30", "09:31")) != [one]
    assert memory.fenced(PLAN.replace("vendor at", "vendor  at\t")) == [one]
    assert memory.fenced("a\n```\nx  y\n\n```\nb\n```one line```") == ["x y", "one line"]
    assert memory.fenced("```\nno end") == ["no end"] and memory.fenced("no fence at all") == []
    y, room, ch = setup(tmp_path)
    posted(y, "example-room", PLAN, 2)
    human_turn(room)
    assert send(y, PLAN.replace("09:30", "09:31"))["ok"] is True               # one character: not refused
    room._new_card()
    human_turn(room)
    assert send(y, PLAN.replace("\n", "\n\n").replace("  ", " \t "))["ok"] is False   # blank lines and whitespace: refused


def test_7_a_text_with_no_fence_reads_no_file_and_the_view_is_read_once_a_day(tmp_path, monkeypatch):
    y, room, ch = setup(tmp_path)
    posted(y, "example-room", PLAN, 1)
    reads = []
    real = memory.room_lines
    monkeypatch.setattr(memory, "room_lines", lambda *a, **k: reads.append(1) or real(*a, **k))
    human_turn(room)
    assert send(y, "The plan holds, nothing moved.")["ok"] is True
    assert call(y, {**STOP, "last_assistant_message": "Nothing moved, the plan holds."}) == {}
    assert reads == []                                                         # no fence: no day file read
    assert send(y, AGAIN)["ok"] is False and reads == [1]                      # the first fenced text builds the view
    room._new_card()
    human_turn(room)
    assert send(y, "```\nanother block\n```")["ok"] is True and reads == [1]  # kept until the date changes


def test_8_a_restart_builds_the_view_again_from_a_two_day_log(tmp_path):
    y, room, ch = setup(tmp_path)
    day = posted(y, "example-room", PLAN, 2)
    posted(y, "example-room", "```\nyesterday's own block\n```", 1)
    days = memory.window(y.memory)
    assert y.earlier_blocks("example-room") == {memory.fenced(PLAN)[0]: (days[2], "19:22:19"),
                                                "yesterday's own block": (days[1], "19:22:19")}
    y2 = Yerkd(EXAMPLE_PRIVATE, tmp_path)                                      # the daemon restarts: nothing kept in memory
    room2 = y2.add_room("example-room", ch)
    assert y2.pastes == {}
    human_turn(room2)
    assert asyncio.run(y2.tool_send("example-room", AGAIN, "", "example-room"))["error"] == words.PASTE.format(when=f"{day} at 19:22")


def test_9_a_send_to_another_room_is_compared_with_that_rooms_log(tmp_path):
    y, room, ch = setup(tmp_path)
    y.room_confs["example-quiet"] = {**y.room_confs["example-quiet"], "id": "quiet-1"}   # its own id, as rooms.toml gives one
    y.daily.rooms["example-quiet"] = ("quiet-1", "private")
    y.add_room("example-quiet", ch)
    day = posted(y, "example-quiet", PLAN, 1)                                  # filed where it went: the other room
    human_turn(room)
    assert send(y, AGAIN)["ok"] is True                                        # this room never posted it
    room._new_card()
    human_turn(room)
    out = send(y, AGAIN, to="example-quiet")
    assert out == {"ok": False, "error": words.PASTE.format(when=f"{day} at 19:22")}


def test_10_each_bounce_is_one_paste_row_and_the_morning_diff_counts_it(tmp_path):
    y, room, ch = setup(tmp_path)
    day = posted(y, "example-room", PLAN, 1)
    human_turn(room)
    send(y, AGAIN)
    send(y, AGAIN)                                                            # went out: no row
    room._new_card()
    human_turn(room)
    call(y, {**STOP, "last_assistant_message": PLAN})
    rows = [r for r in reversed(y.ledger.rows(20, "shadow")) if r["rule"] == "paste"]
    assert [r["detail"] for r in rows] == [f"a block of 4 lines first posted {day} at 19:22"] * 2
    assert [r["reply_chars"] for r in rows] == [len(AGAIN), len(PLAN)] and all("vendor" not in r["detail"] for r in rows)
    assert y.ledger.shadow_rules(0, time.time() + 1)["paste"] == 2
    priv, state, accounts, cool = layer(tmp_path / "m", machine=None)
    led = Ledger(state / "ledger.sqlite")
    y0 = status.midnight(NOW, 1)
    for i in range(2):
        led.shadow(t=y0 + 100 + i, room="example-room", rule="paste", detail="a block of 4 lines", reply_chars=10)
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": y0 - 10, "units": []}, NOW)
    assert f"{words.WORKING} shadow flag paste: 2" in lines and "shadow paste" in stamp["findings"]   # as stub and board are


def test_11_the_card_row_of_a_turned_back_send_reads_not_done_and_the_next_one_done():
    c = Card()
    for i, ok in enumerate((False, True)):
        tid = f"s{i}"
        c.feed({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": "mcp__yerk__send",
                                                                  "input": {"room": "example-room", "text": PLAN}}]}})
        said = json.dumps({"result": json.dumps({"ok": ok} if ok else {"ok": False, "error": words.PASTE})})   # Claude Code's shape
        c.feed({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": said}]}})
    assert c.render().splitlines()[1:] == [f"{FAILED} sending a message to example-room", f"{OK} sending a message to example-room"]
    m = (REPO / "yerk_ai" / "mechanics.md").read_text()
    assert "A fenced block this room posted on an earlier day, sent again word for word, is refused once" in m
