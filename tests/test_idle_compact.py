"""Checks idle compaction, where yerkd summarizes a room's long session while the room is idle. yerkd's reaper compacts a room
idle `idle_compact_after` minutes whose session holds `idle_compact_at` of its window or more, the way `yerk room compact`
a one-line question waited 5 min 20 s for Claude Code's own). Each test ends on a case that compacts or answers, so on a
daemon without it every one fails on what the room does, never on a missing name."""
import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest

from yerk_ai.accounts import Account
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.room import Inbound

FAKE = Path(__file__).with_name("fake_claude.py")
ROOM = "example-room"            # the example layer's room: the budget's 250,000 window, so 0.7 of it is 175,000
IDLE = 26 * 60                   # past the budget's 25 minutes, under the 30 a room's process stays open


class Chan:
    def __init__(self):
        self.replies = []

    def open_card(self, room, text, kind="turn"):
        pass

    def progress(self, room, text):
        pass

    def reply(self, room, text, inbound, card=None, key=None):
        self.replies.append(text)
        return [f"m{len(self.replies)}"]

    def notice(self, room, text, inbound, card=None, key=None):
        self.replies.append(text)
        return ["n1"]


@pytest.fixture
def y(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "mark"))
    return Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))


def say(y, room, mid, text, alone=False):
    y.ledger.inbound_add(mid, ROOM, "console", "owner", len(text))
    return room.submit(Inbound(mid, "owner", text, alone=alone))


def wait_idle(room, timeout=20):
    end = time.time() + timeout
    while (room.busy or room.queue) and time.time() < end:
        time.sleep(0.05)
    assert not room.busy


def reap(y, room=None, idle=0.0):
    """Two whole passes of the daemon's own reaper (Yerkd.reap, its minute cut to 0.01 s; a pass ends with the workers'
    reconcile, counted here), `room` idle `idle` seconds first. A compaction a pass starts may still be running after."""
    if room is not None:
        room.last_active = time.time() - idle

    async def passes():
        ended = []
        two = asyncio.Event()

        async def counted():
            ended.append(1)
            if len(ended) >= 2:
                two.set()
        y.workers.reconcile = counted
        task = asyncio.create_task(y.reap(0.01))
        await asyncio.wait_for(two.wait(), 30)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(passes())


def compactions(tmp_path):
    """Each /compact the fake claude was sent."""
    f = tmp_path / "mark.compacted"
    return len(f.read_text().splitlines()) if f.is_file() else 0


def test_the_reaper_compacts_a_room_idle_near_its_window_once_and_the_next_message_finds_it_small(y, tmp_path):
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    reap(y)                                                  # just answered: not idle long enough
    assert compactions(tmp_path) == 0
    reap(y, room, IDLE)
    wait_idle(room)
    assert compactions(tmp_path) == 1
    rows = y.ledger.rows(2)                                  # the /compact's own row holds the size after it
    assert [r["context"] for r in rows] == [20000, 200000] and rows[0]["session_id"] == rows[1]["session_id"]
    assert rows[0]["outcome"] == "compacted" and y.ledger.turn_summary(0)[1] == []   # never one of the day's failed turns
    reap(y, room, IDLE)                                      # once per growth: not again at this size
    wait_idle(room)
    assert compactions(tmp_path) == 1
    say(y, room, "2", "and now?")
    wait_idle(room)
    assert chan.replies == ["echo: CONTEXT:200000 hello", "echo: and now?"] and room.rp.session_id == rows[0]["session_id"]
    room.rp.close()


def test_no_idle_compaction_while_a_turn_runs_or_a_message_waits_and_one_once_they_are_answered(y, tmp_path):
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    room.last_active = time.time() - IDLE                   # past its line and idle long enough, then:
    say(y, room, "2", "SLOW a long job")                    # a turn running,
    say(y, room, "3", "and one more", alone=True)           # and a message waiting behind it
    assert room.busy and room.queue
    reap(y)
    assert compactions(tmp_path) == 0
    wait_idle(room)
    assert chan.replies[-2:] == ["echo: SLOW a long job", "echo: and one more"]
    reap(y, room, IDLE)
    wait_idle(room)
    assert compactions(tmp_path) == 1
    room.rp.close()


def test_a_room_under_its_line_is_left_alone_and_rooms_toml_may_move_the_line(y, tmp_path):
    base = dict(y.room_confs[ROOM])
    room = y.add_room(ROOM, Chan())
    say(y, room, "1", "CONTEXT:150000 hello")               # under 175,000
    wait_idle(room)
    reap(y, room, IDLE)
    assert compactions(tmp_path) == 0
    room.rp.close()
    y.room_confs[ROOM] = {**base, "idle_compact_at": 0.5, "idle_compact_after": 10}   # the room's own keys, read at start
    room = y.add_room(ROOM, Chan())
    reap(y, room, 9 * 60)                                    # under its 10 minutes
    assert compactions(tmp_path) == 0
    reap(y, room, 11 * 60)                                   # 150,000 is past 0.5 of 250,000
    wait_idle(room)
    assert compactions(tmp_path) == 1
    room.rp.close()


def test_a_room_that_turns_it_off_or_leaves_its_window_to_the_model_is_never_compacted(y, tmp_path):
    base = dict(y.room_confs[ROOM])
    room = y.add_room(ROOM, Chan())
    say(y, room, "1", "CONTEXT:240000 hello")
    wait_idle(room)
    room.rp.close()
    for keys in ({"idle_compact_at": 0}, {"compact_window": 0}):
        y.room_confs[ROOM] = {**base, **keys}
        room = y.add_room(ROOM, Chan())
        reap(y, room, IDLE)
        assert compactions(tmp_path) == 0, keys
    y.room_confs[ROOM] = base                                # the budget's line again: the same session compacts
    room = y.add_room(ROOM, Chan())
    reap(y, room, IDLE)
    wait_idle(room)
    assert compactions(tmp_path) == 1
    room.rp.close()


def test_a_compaction_that_leaves_the_session_past_its_line_is_not_repeated_until_a_turn_grows_it(y, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_POST", "190000")        # Claude Code kept most of it
    room = y.add_room(ROOM, Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    reap(y, room, IDLE)
    wait_idle(room)
    assert compactions(tmp_path) == 1
    for _ in range(2):
        reap(y, room, IDLE)
        wait_idle(room)
    assert compactions(tmp_path) == 1                        # never in a loop
    say(y, room, "2", "CONTEXT:210000 more")
    wait_idle(room)
    reap(y, room, IDLE)
    wait_idle(room)
    assert compactions(tmp_path) == 2
    room.rp.close()


def test_a_failed_idle_compaction_is_said_once_and_waits_for_the_next_turn(y, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_FAIL", "1")
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    with caplog.at_level(logging.WARNING, logger="yerk.room"):
        for _ in range(3):
            reap(y, room, IDLE)
            wait_idle(room)
    assert compactions(tmp_path) == 1
    assert len([r for r in caplog.records if "idle compaction did not compact" in r.getMessage()]) == 1
    assert chan.replies == ["echo: CONTEXT:200000 hello"]   # nothing said in the room
    say(y, room, "2", "CONTEXT:201000 more")
    wait_idle(room)
    reap(y, room, IDLE)
    wait_idle(room)
    assert compactions(tmp_path) == 2                        # the turn grew it: one more try
    room.rp.close()


def test_a_message_sent_during_an_idle_compaction_is_answered_right_after_it_and_its_wait_logged(y, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_SLOW", "1.5")
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        reap(y, room, IDLE)                                  # the compaction starts, and runs on for 1.5 s
        assert say(y, room, "2", "are you there?") == "queued"
        wait_idle(room)
    assert compactions(tmp_path) == 1 and chan.replies[-1] == "echo: are you there?"
    assert [r.getMessage() for r in caplog.records if "waited up to" in r.getMessage()][0].startswith(
        f"room {ROOM}: 1 message(s) waited up to ")
    room.rp.close()


def test_a_message_sent_during_yerk_room_compact_is_answered_right_after_it(y, tmp_path, monkeypatch):
    """It was queued and then left until the next message came in: compact_now cleared `busy` and never ran the queue."""
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_SLOW", "1.5")
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "hello")
    wait_idle(room)
    done = []
    t = threading.Thread(target=lambda: done.append(room.compact_now()))
    t.start()
    time.sleep(0.5)
    assert say(y, room, "2", "are you there?") == "queued"
    t.join(10)
    wait_idle(room, timeout=10)
    assert done == ["ok"] and chan.replies == ["echo: hello", "echo: are you there?"]
    room.rp.close()


def test_the_sessions_size_comes_from_the_rooms_own_calls_and_a_restart_keeps_it(y, tmp_path):
    room = y.add_room(ROOM, Chan())
    say(y, room, "1", "CONTEXT:200000 SUBAGENT:900000 hello")
    wait_idle(room)
    assert y.ledger.rows(1)[0]["context"] == 200000        # a subagent's own call is not the room's session
    room.rp.close()
    again = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))   # a restart or a deploy
    room = again.add_room(ROOM, Chan())
    reap(again, room, IDLE)
    wait_idle(room)
    assert compactions(tmp_path) == 1
    room.rp.close()
