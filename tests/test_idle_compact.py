"""Checks idle compaction, where yerkd summarizes a room's long session while the room is idle. yerkd's reaper compacts a room
idle `idle_compact_after` minutes whose session holds `idle_compact_at` of its window or more, the way `yerk room compact`
does, so the compaction never lands in a person's turn (seen live: a one-line question waited 5 min 20 s for Claude
Code's own); a turn that ends within `idle_compact_within` of Claude Code's own line compacts the same way at once
(a busy room: never idle 25 minutes near its line). Each test ends on a case that compacts or answers,
missing name."""
import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from yerk_ai import words
from yerk_ai.accounts import Account
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import SessionTable
from yerk_ai.room import Inbound

FAKE = Path(__file__).with_name("fake_claude.py")
ROOM = "example-room"            # the example layer's room: the budget's 250,000 window, so 0.7 of it is 175,000, Claude
                                 # Code's line is 217,000 and 0.05 of the window under it is 204,500
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


class Cards(Chan):
    """Each card post and edit, in order."""
    def __init__(self):
        super().__init__()
        self.cards = []

    def open_card(self, room, text, kind="turn"):
        self.cards.append(("open", text))

    def progress(self, room, text):
        self.cards.append(("edit", text))


@pytest.fixture
def y(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "mark"))
    d = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))
    d.budgets["session"]["precompact_within"] = 0       # the copy's band off but in its own tests (band), so these test one rule each
    return d


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
    say(y, room, "1", "CONTEXT:200000 hello")               # past 175,000, under the turn's end line
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
    room = y.add_room(ROOM, chan := Cards())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        reap(y, room, IDLE)                                  # the compaction starts, and runs on for 1.5 s
        at = len(chan.cards)
        assert say(y, room, "2", "are you there?") == "queued"
        assert chan.cards[at:] == [("open", f"{words.WORKING} {words.COMPACTING}")]   # its card, from the moment it came
        wait_idle(room)
    assert compactions(tmp_path) == 1 and chan.replies[-1] == "echo: are you there?"
    assert chan.cards[at:] == [("open", f"{words.WORKING} {words.COMPACTING}"), ("edit", f"{words.WORKING} {words.START}"),
                               ("edit", f"{words.WORKING} echo: are you there?")]          # one card: its turn took it over
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


def started(tmp_path, timeout=10):
    """Waits for a /compact to reach the fake claude (its mark is written before its slow wait)."""
    end = time.time() + timeout
    while not compactions(tmp_path) and time.time() < end:
        time.sleep(0.01)
    assert compactions(tmp_path)


def test_a_turn_that_ends_near_claude_codes_line_compacts_then_once_and_the_next_message_finds_it_small(y, tmp_path, caplog):
    """Seen live in a busy room: six turns began within 51,000 of Claude Code's line, the room never idle 25 minutes,
    and the next message waited 2 min inside its own turn for Claude Code's compaction."""
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:204000 hello")               # under 204,500: nothing
    wait_idle(room)
    assert compactions(tmp_path) == 0
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "2", "CONTEXT:205000 more")             # past it: compacted at the turn's end, no idle wait
        wait_idle(room)
    assert compactions(tmp_path) == 1
    rows = y.ledger.rows(2)
    assert [(r["outcome"], r["context"]) for r in rows] == [("compacted", 20000), ("ok", 205000)]
    assert f"room {ROOM}: a turn ended at 205000 of its 250000 tokens, Claude Code's line 217000, compacting" in caplog.messages
    say(y, room, "3", "and now?")                            # once per growth: the small session is not compacted again
    wait_idle(room)
    reap(y, room, IDLE)
    wait_idle(room)
    assert compactions(tmp_path) == 1 and chan.replies[-1] == "echo: and now?" and room.rp.session_id == rows[0]["session_id"]
    room.rp.close()


def test_a_message_waiting_when_the_turn_ends_runs_first_and_one_sent_during_the_compaction_has_its_card_at_once(y, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_SLOW", "1.5")
    room = y.add_room(ROOM, chan := Cards())
    say(y, room, "1", "SLOW CONTEXT:205000 a long job")
    say(y, room, "2", "and one more", alone=True)           # waiting when the turn ends: it runs at once, nothing added
    assert room.busy and room.queue
    started(tmp_path)
    assert [r["outcome"] for r in y.ledger.rows(2)] == ["ok", "ok"] and compactions(tmp_path) == 1   # both answered first
    at = len(chan.cards)
    assert say(y, room, "3", "are you there?") == "queued"
    assert chan.cards[at:] == [("open", f"{words.WORKING} {words.COMPACTING}")]   # from the moment it came
    wait_idle(room)
    assert chan.replies == ["echo: SLOW CONTEXT:205000 a long job", "echo: and one more", "echo: are you there?"]
    assert chan.cards[at:] == [("open", f"{words.WORKING} {words.COMPACTING}"), ("edit", f"{words.WORKING} {words.START}"),
                               ("edit", f"{words.WORKING} echo: are you there?")]
    assert [r["outcome"] for r in y.ledger.rows(4)] == ["ok", "compacted", "ok", "ok"] and compactions(tmp_path) == 1
    room.rp.close()


def test_a_failed_turn_end_compaction_is_said_once_and_waits_for_the_next_turn(y, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_FAIL", "1")
    room = y.add_room(ROOM, chan := Chan())
    with caplog.at_level(logging.WARNING, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:205000 hello")
        wait_idle(room)
        reap(y, room, IDLE)                                  # nor does the idle one try before a turn grows it
        wait_idle(room)
    assert compactions(tmp_path) == 1 and chan.replies == ["echo: CONTEXT:205000 hello"]   # nothing said in the room
    assert len([m for m in caplog.messages if "the end-of-turn compaction did not compact" in m]) == 1
    say(y, room, "2", "CONTEXT:206000 more")
    wait_idle(room)
    assert compactions(tmp_path) == 2                        # the turn grew it: one more try, at its end
    room.rp.close()


def test_the_turn_end_line_is_the_budgets_share_and_a_room_may_move_it_or_turn_it_off(y, tmp_path):
    base = dict(y.room_confs[ROOM])
    for n, keys in enumerate(({"idle_compact_within": 0}, {"compact_window": 0})):
        y.room_confs[ROOM] = {**base, **keys}
        room = y.add_room(ROOM, Chan())
        say(y, room, str(n), "CONTEXT:216000 hello")         # 1,000 under Claude Code's line
        wait_idle(room)
        assert compactions(tmp_path) == 0, keys
        room.rp.close()
    y.room_confs[ROOM] = {**base, "idle_compact_within": 0.2}   # the room's own share: 167,000 and up
    room = y.add_room(ROOM, Chan())
    say(y, room, "2", "CONTEXT:170000 again")
    wait_idle(room)
    assert compactions(tmp_path) == 1
    room.rp.close()


def band(y):
    """The copy's band on: 0.1 of the 250,000 window, so a turn that ends from 192,000 up to the 204,500 above compacts a copy."""
    y.budgets["session"]["precompact_within"] = 0.1


def until(test, timeout=10):
    end = time.time() + timeout
    while not test() and time.time() < end:
        time.sleep(0.02)
    assert test()


def bound(tmp_path):
    return SessionTable(tmp_path / "sessions.json").get(ROOM)["session_id"]


def test_a_turn_ending_in_the_copys_band_compacts_a_copy_off_to_the_side_and_the_room_takes_it_in_one_step(y, tmp_path, caplog):
    """Seen live in a busy room: a turn ended 37,362 under its line and the next message waited 126 s behind the
    206 s compaction; a turn 20 minutes earlier had ended 61,754 under it with 305 s of quiet after. Ended in the band, the
    turn leaves the room free at once, the copy (--fork-session) compacts, and with no turn since it becomes the room's
    session: bound, ledgered, the next message answered on it with no compaction of its own."""
    band(y)
    room = y.add_room(ROOM, chan := Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")             # 22,000 under Claude Code's line of 217,000: in the band
        wait_idle(room)                                       # free at once, never held by the copy's compaction
        until(lambda: any("compacted on a copy" in m for m in caplog.messages))
    live, copy = y.ledger.rows(2)[1]["session_id"], room.rp.session_id          # the turn's row, then the copy's
    assert live != copy and compactions(tmp_path) == 1 and room.context == 20000 and room.fork is None and bound(tmp_path) == copy
    argv = [json.loads(x)["argv"] for x in (tmp_path / "argv.jsonl").read_text().splitlines()]
    forked = [a for a in argv if "--fork-session" in a]
    assert len(forked) == 1 and forked[0][forked[0].index("--resume") + 1] == live and forked[0][forked[0].index("--session-id") + 1] == copy
    row = y.ledger.rows(1)[0]
    assert (row["outcome"], row["session_id"], row["context"]) == ("compacted", copy, 20000)
    assert any("compacted on a copy" in m and copy in m for m in caplog.messages)
    say(y, room, "2", "and now?")
    wait_idle(room)
    assert chan.replies == ["echo: CONTEXT:195000 hello", "echo: and now?"] and room.rp.session_id == copy == bound(tmp_path)
    assert compactions(tmp_path) == 1 and len([a for a in argv if "--fork-session" in a]) == 1


def test_a_message_during_the_copys_compaction_runs_at_once_on_the_live_session_the_copy_dropped_and_tried_again(y, tmp_path, monkeypatch, caplog):
    """The wait itself: a message that comes in while the copy compacts is never queued behind it. It runs at once on the
    live session, which is what the room would do with no compaction at all; the copy is stale (Claude Code's own
    prefix_changed rule) and dropped, its transcript copy removed, and the turn's end copies the session again."""
    band(y)
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_SLOW", "4")
    room = y.add_room(ROOM, chan := Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")
        wait_idle(room)
        live = room.rp.session_id
        started(tmp_path)                                     # the copy's /compact is running
        first = room.fork.session_id
        (copied := room.fork.projects / f"{first}.jsonl").parent.mkdir(parents=True, exist_ok=True)
        copied.write_text("{}\n")                             # what Claude Code writes for the copy
        sent = time.time()
        assert say(y, room, "2", "CONTEXT:199000 and now?") == "started"
        until(lambda: len(chan.replies) == 2, timeout=3)      # never behind the copy's 4 s
        assert time.time() - sent < 3 and room.rp.session_id == live
        until(lambda: any("compaction on a copy is dropped" in m and "overtook" in m for m in caplog.messages))
        assert not copied.exists() and bound(tmp_path) == live
        until(lambda: room.rp.session_id not in (live, first))   # the second turn's end copied it again, and no turn overtook that
    assert chan.replies == ["echo: CONTEXT:195000 hello", "echo: CONTEXT:199000 and now?"]
    assert compactions(tmp_path) == 2 and bound(tmp_path) == room.rp.session_id and room.context == 20000


def test_a_turn_within_the_turn_end_line_still_compacts_the_live_session_and_a_band_of_0_never_copies(y, tmp_path):
    """Within idle_compact_within of the line nothing changes: the live session compacts at the turn's end, the room held (its
    own tests above). A band of 0, a room's own key, turns the copy off."""
    band(y)
    room = y.add_room(ROOM, Chan())
    say(y, room, "1", "CONTEXT:205000 hello")              # 12,000 under the line: within 0.05 of the window
    wait_idle(room)
    argv = (tmp_path / "argv.jsonl").read_text()
    assert compactions(tmp_path) == 1 and "--fork-session" not in argv and room.context == 20000
    y.room_confs[ROOM]["precompact_within"] = 0
    other = y.add_room(ROOM, Chan())
    say(y, other, "2", "CONTEXT:195000 again")
    wait_idle(other)
    time.sleep(0.3)
    assert compactions(tmp_path) == 1 and "--fork-session" not in (tmp_path / "argv.jsonl").read_text() and other.fork is None
