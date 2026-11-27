"""Checks idle compaction, where yerkd summarizes a room's long session while the room is idle. yerkd's reaper compacts a room
idle `idle_compact_after` minutes whose session holds `idle_compact_at` of its window or more, the way `yerk room compact`
does, so the compaction never lands in a person's turn (seen live: a one-line question waited 5 min 20 s for Claude
Code's own); a turn that ends within `idle_compact_within` of Claude Code's own line compacts the same way at once
(a busy room: never idle 25 minutes near its line). Each test ends on a case that compacts or answers,
so on a daemon without it every one fails on what the room does, never on a missing name."""
import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from yerk_ai import status, words
from yerk_ai.accounts import Account
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import RoomProcess, SessionTable
from yerk_ai.room import TAIL_HEAD, Inbound

FAKE = Path(__file__).with_name("fake_claude.py")
ROOM = "example-room"            # the example layer's room: the budget's 250,000 window, so 0.7 of it is 175,000, Claude
                                 # Code's line is 217,000 and 0.05 of the window under it is 204,500
IDLE = 26 * 60                   # past the budget's 25 minutes, under the 30 a room's process stays open
COMPACT = f"/compact {words.COMPACT_FOCUS}"     # what a room with no compact_focus of its own sends


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


def compacts(sent, which):
    """The /compact lines of `sent` (with words after it or none) whose first part is `which`: a copy's here, a fast process's in
    test_fast. A test that none went asks this, not for the bare command: the line it would miss has the focus after it."""
    return [t for first, t in sent if first is which and t.split()[:1] == ["/compact"]]


@pytest.fixture
def written(monkeypatch):
    """Each line written to a Claude Code process, as (a copy's, the line)."""
    sent, send = [], RoomProcess.send
    monkeypatch.setattr(RoomProcess, "send", lambda self, text: sent.append((bool(self.fork_of), text)) or send(self, text))
    return sent


def test_the_reaper_compacts_a_room_idle_near_its_window_once_and_the_next_message_finds_it_small(y, tmp_path, gate, written):
    """With its process alive, on a copy, the room free (seen live in a busy room: compacted on its own
    session, the room held 150 s, a message 66 s in waited 84 s)."""
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    reap(y)                                                  # just answered: not idle long enough
    assert compactions(tmp_path) == 0 and room.fork is None  # and no copy on its way either: a copy waits at the gate uncounted
    reap(y, room, IDLE)
    started(tmp_path)                                        # the copy's /compact, held at the gate
    assert room.fork is not None and not room.busy and not room.shortening
    gate.set()
    until(lambda: y.ledger.rows(1)[0]["outcome"] == "compacted")
    rows = y.ledger.rows(2)                                  # the copy's row holds the size after it, its session the room's
    assert [r["context"] for r in rows] == [20000, 200000] and room.rp.session_id == rows[0]["session_id"] != rows[1]["session_id"]
    assert rows[0]["outcome"] == "compacted" and y.ledger.turn_summary(0)[1] == []   # never one of the day's failed turns
    assert written == [(False, "CONTEXT:200000 hello"), (True, COMPACT)]          # nothing to the room's own process
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
    until(lambda: compactions(tmp_path) == 1 and room.fork is None)   # on a copy: the room is never held for it
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
    until(lambda: compactions(tmp_path) == 1 and room.fork is None)   # its process started, then a copy
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
    until(lambda: compactions(tmp_path) == 1 and room.fork is None)   # its process started, then a copy
    room.rp.close()


def test_a_compaction_that_leaves_the_session_past_its_line_is_not_repeated_until_a_turn_grows_it(y, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_POST", "190000")        # Claude Code kept most of it
    room = y.add_room(ROOM, Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    reap(y, room, IDLE)
    until(lambda: compactions(tmp_path) == 1 and room.fork is None)
    for _ in range(2):
        reap(y, room, IDLE)
        wait_idle(room)
    assert compactions(tmp_path) == 1                        # never in a loop
    say(y, room, "2", "CONTEXT:210000 more")
    wait_idle(room)
    reap(y, room, IDLE)
    until(lambda: compactions(tmp_path) == 2 and room.fork is None)
    room.rp.close()


def test_a_failed_idle_compaction_is_said_once_and_waits_for_the_next_turn(y, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_FAIL", "1")
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        for _ in range(3):
            reap(y, room, IDLE)
            wait_idle(room)
        until(lambda: any("compaction on a copy is dropped" in m for m in caplog.messages))
    assert compactions(tmp_path) == 1
    assert len([m for m in caplog.messages if "is dropped" in m and "its compaction failed" in m]) == 1
    assert chan.replies == ["echo: CONTEXT:200000 hello"]   # nothing said in the room
    say(y, room, "2", "CONTEXT:201000 more")
    wait_idle(room)
    reap(y, room, IDLE)
    until(lambda: compactions(tmp_path) == 2 and room.fork is None)   # the turn grew it: one more try
    room.rp.close()


def test_a_message_sent_during_a_held_compaction_is_answered_right_after_it_and_its_wait_logged(y, tmp_path, monkeypatch, caplog):
    """`yerk room compact`'s, as the turn-end and overflow ones (a quiet room's runs on a copy: the next test)."""
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_SLOW", "1.5")
    room = y.add_room(ROOM, chan := Cards())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        threading.Thread(target=room.compact_now, daemon=True).start()   # the compaction starts, and runs on for 1.5 s
        started(tmp_path)
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


def test_a_message_during_a_quiet_rooms_copy_runs_at_once_and_the_copy_is_taken_as_its_turn_ends(y, tmp_path, gate, caplog):
    """Seen live in a busy room: a message 66 s into the idle compaction waited 84 s under the card."""
    room = y.add_room(ROOM, chan := Cards())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    live = room.rp.session_id
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        reap(y, room, IDLE)
        started(tmp_path)                                    # the copy's /compact, held at the gate
        assert room.fork is not None and say(y, room, "2", "CONTEXT:21000 SLOW meanwhile") == "started"
        until(lambda: room.reading)                          # its turn, on the live session
        gate.set()
        until(lambda: room.ready is not None)                # compacted while that turn runs: taken as it ends
        wait_idle(room)
    assert chan.replies[1] == "echo: CONTEXT:21000 SLOW meanwhile" and room.rp.session_id == bound(tmp_path) != live
    assert not any(words.COMPACTING in text for _, text in chan.cards) and not any("waited up to" in m for m in caplog.messages)
    say(y, room, "3", "and now?")
    say(y, room, "4", "once more", alone=True)
    wait_idle(room)
    assert echoed(chan, 2).startswith(TAIL_HEAD) and "owner: CONTEXT:21000 SLOW meanwhile" in echoed(chan, 2)
    assert echoed(chan, 2).endswith("\n\nand now?") and chan.replies[3] == "echo: once more"     # the tail, once
    room.rp.close()


def test_with_no_process_alive_the_room_is_held_for_its_start_only_and_then_compacts_a_copy(y, tmp_path, gate, written, monkeypatch, caplog):
    """After a daemon restart no room has a process until its first message: a message during the idle compaction's start
    of one waits for that start alone, is answered on the live session while the copy compacts, and rides to the copy."""
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    assert room.reap_idle(force=True)
    up, start = threading.Event(), RoomProcess.start
    monkeypatch.setattr(RoomProcess, "start", lambda self, *a, **k: (k.get("fork") or up.wait(20)) and start(self, *a, **k))
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        reap(y, room, IDLE)                                  # the room's own process starting, held until `up`
        assert room.busy and not room.shortening and say(y, room, "2", "CONTEXT:21000 meanwhile") == "queued"
        up.set()
        until(lambda: len(chan.replies) == 2)
        assert room.fork is not None and chan.replies[1] == "echo: CONTEXT:21000 meanwhile"   # the copy still at the gate
        gate.set()
        until(lambda: any("compacted on a copy" in m for m in caplog.messages))
        wait_idle(room)
    assert any("1 message(s) waited up to" in m and "to start (idle)" in m for m in caplog.messages)
    say(y, room, "3", "and now?")
    wait_idle(room)
    assert echoed(chan, 2).startswith(TAIL_HEAD) and "CONTEXT:21000 meanwhile" in echoed(chan, 2) and room.rp.session_id == bound(tmp_path)
    assert not compacts(written, False) and compacts(written, True) == [COMPACT]
    room.rp.close()


def test_with_no_process_alive_and_nothing_waiting_the_room_is_free_after_the_start_and_answers_the_next_message_at_once(y, tmp_path, gate):
    """The start holds the room for itself alone: with no message in it, the room must come back free, or the next message
    would sit queued behind a hold nothing ends."""
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    assert room.reap_idle(force=True)                        # no process alive, as after a daemon restart
    reap(y, room, IDLE)
    started(tmp_path)                                        # the start is over and the copy's /compact is at the gate
    assert room.fork is not None and not room.busy and not room.shortening
    say(y, room, "2", "CONTEXT:21000 meanwhile")
    until(lambda: len(chan.replies) == 2)
    assert chan.replies[1] == "echo: CONTEXT:21000 meanwhile" and room.fork is not None    # answered while the copy is still held
    gate.set()
    until(lambda: y.ledger.rows(1)[0]["outcome"] == "compacted")
    wait_idle(room)
    room.rp.close()


def test_a_start_that_fails_frees_the_room_is_said_once_and_is_not_tried_again_before_the_next_turn(y, tmp_path, monkeypatch, caplog):
    """A process that died without being closed keeps its session's lock, so the start of a new one can fail: the room
    must come back free, never stay held, and the reaper must not try again every minute."""
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    assert room.reap_idle(force=True)
    tries, start = [], room._start

    def failing(*a, **k):
        tries.append(1)
        raise RuntimeError("the session is busy")
    monkeypatch.setattr(room, "_start", failing)
    with caplog.at_level(logging.WARNING, logger="yerk.room"):
        for _ in range(3):
            reap(y, room, IDLE)
            wait_idle(room, timeout=5)
    assert len(tries) == 1 and room.fork is None and compactions(tmp_path) == 0
    assert sum("could not start the room's process" in m for m in caplog.messages) == 1
    monkeypatch.setattr(room, "_start", start)
    say(y, room, "2", "and now?")
    wait_idle(room)
    assert chan.replies[-1] == "echo: and now?"
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
    until(lambda: compactions(tmp_path) == 1 and room.fork is None)   # its process started, then a copy
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


@pytest.fixture
def gate(monkeypatch):
    """The copy's /compact held until the test opens the gate: a copy's two minutes, at the test's own pace."""
    opened = threading.Event()
    real = RoomProcess.read_turn

    def read_turn(self, *a, **k):
        t = real(self, *a, **k)
        if self.fork_of:                                      # the copy, before the room takes it
            opened.wait(20)
        return t
    monkeypatch.setattr(RoomProcess, "read_turn", read_turn)
    return opened


def owners_window(y):
    """A room with the long window: 1,000,000, so Claude Code's line is 967,000, the copy's band starts 100,000 under it
    and the turn-end compaction's 50,000 under it (917,000)."""
    band(y)
    y.room_confs[ROOM]["compact_window"] = 1000000


def echoed(chan, i):
    return chan.replies[i].removeprefix("echo: ")


def test_nine_quick_turns_keep_one_copy_which_the_room_takes_at_a_turns_end_its_tail_given_once_and_nothing_waits(y, tmp_path, gate, caplog):
    """Nine minutes of a busy room, replayed: nine turns ended in the copy's band (874,665 to 915,021 of
    a 1,000,000 window), each message sent within a minute of the last answer, while a copy's compaction takes about two
    minutes. Every copy was dropped and the tenth turn's end compacted the live session with the room held. Here exactly
    one copy starts, goes on through the turns that overtake it, and becomes the room's session as the first turn that ends
    after its compaction ends; the copy's first message carries those turns' exchanges once; no message waits and the
    live session is never compacted."""
    owners_window(y)
    room = y.add_room(ROOM, chan := Cards())
    sizes = [874665, 880423, 882850, 886954, 896346, 903385, 906439, 910801, 915021]
    times = []
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        for i, n in enumerate(sizes):
            if i == 4:                                        # the fifth turn runs: the copy's compaction ends inside it
                say(y, room, str(i), f"CONTEXT:{n} SLOW turn {i}")
                until(lambda: room.reading)
                gate.set()
                until(lambda: room.ready is not None)         # compacted while the turn runs: held for its end
                live = room.rp.session_id
            else:
                taken = gate.is_set() and room.fork is None and room.ready is None
                say(y, room, str(i), f"CONTEXT:{24000 + i if taken else n} turn {i}")
            sent = time.time()
            wait_idle(room)
            times.append(time.time() - sent)
            if i == 0:
                live, copy = room.rp.session_id, room.fork.session_id
    argv = (tmp_path / "argv.jsonl").read_text().splitlines()
    assert len([a for a in argv if "--fork-session" in a]) == 1 and compactions(tmp_path) == 1     # one copy, no held compaction
    assert room.rp.session_id == copy == bound(tmp_path) != live and room.fork is None and room.ready is None
    assert not any("waited" in m or "compact (end-of-turn)" in m for m in caplog.messages)
    assert not any(words.COMPACTING in text for _, text in chan.cards) and max(times) < 5
    took = next(m for m in caplog.messages if "compacted on a copy in" in m and "now the room's" in m)
    assert "4 turn(s) overtook it" in took and copy in took
    first = echoed(chan, 5)                                   # the copy's first message: the tail, then turn 5's own text
    assert first.count(TAIL_HEAD) == 1 and first.endswith("turn 5")
    for i in (1, 2, 3, 4):                                    # the turns between the copy and its take, each once, in order
        assert first.count(f"owner: CONTEXT:{sizes[i]}") == 1 and first.count(f"the room: echo: CONTEXT:{sizes[i]}") == 1
    assert [first.index(f"turn {i}\n") for i in (1, 2, 3)] == sorted(first.index(f"turn {i}\n") for i in (1, 2, 3))
    assert f"CONTEXT:{sizes[0]}" not in first                 # made before the copy: in its summary, never in the tail
    assert all(TAIL_HEAD not in echoed(chan, i) for i in range(6, 9)) and not (tmp_path / "rooms" / ROOM / "copy-tail").exists()
    row = next(r for r in y.ledger.rows(20) if r["session_id"] == copy and r["outcome"] == "compacted")
    assert "4 turn(s) overtook it" in row["note"] and "tail" in row["note"]


def test_a_copy_that_compacts_while_a_turn_runs_is_taken_as_it_ends_before_the_message_waiting_runs(y, tmp_path, gate, caplog):
    """Item 2: the copy's compaction ends while a turn is being answered and a message waits behind it; the turn ends on
    the live session, then the room takes the copy in one step, then the waiting message runs on the copy, the exchange
    it overtook in front of it. The swap makes no model call: only the copy's /compact was ever sent."""
    band(y)
    room = y.add_room(ROOM, chan := Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")
        wait_idle(room)
        live, copy = room.rp.session_id, room.fork.session_id
        say(y, room, "2", "CONTEXT:196000 SLOW still here")
        until(lambda: room.reading)
        assert say(y, room, "3", "CONTEXT:21000 queued behind it", alone=True) == "queued"
        gate.set()
        until(lambda: room.ready is not None)
        assert room.rp.session_id == live
        wait_idle(room)
    assert chan.replies[1] == "echo: CONTEXT:196000 SLOW still here" and room.rp.session_id == copy == bound(tmp_path)
    last = echoed(chan, 2)
    assert last.startswith(TAIL_HEAD) and "owner: CONTEXT:196000 SLOW still here" in last and last.endswith("\n\nCONTEXT:21000 queued behind it")
    assert compactions(tmp_path) == 1 and any("taken as it ends" in m for m in caplog.messages)


@pytest.mark.parametrize("end", ["respec", "renew", "fresh-next", "held", "fail"])
def test_a_copy_still_ends_on_a_change_of_the_live_session_or_its_own_failure_and_says_why(y, tmp_path, gate, monkeypatch, caplog, end):
    """Item 1: what still ends a copy, each said in the log and the ledger, its transcript copy removed, the room's bound
    session the live one: a model, effort or account change; /new or a fresh-next file; a held compaction starting; its own
    compaction failing, after which the next turn's end copies again (item 6)."""
    band(y)
    if end == "fail":
        monkeypatch.setenv("FAKE_CLAUDE_COMPACT_FAIL", "1")
    room = y.add_room(ROOM, Chan())
    why = {"respec": "model, effort or account changed", "renew": "/new", "fresh-next": "fresh session",
           "held": "asked compaction of the live session started", "fail": "its compaction failed"}[end]
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")
        wait_idle(room)
        live, fork = room.rp.session_id, room.fork
        (copied := fork.projects / f"{fork.session_id}.jsonl").parent.mkdir(parents=True, exist_ok=True)
        copied.write_text("{}\n")                            # what Claude Code writes for the copy
        if end == "respec":
            room.respec()
        elif end == "renew":
            room.renew()
        elif end == "fresh-next":
            (tmp_path / "rooms" / ROOM / "fresh-next").write_text("(a recap)")
        elif end == "held":
            threading.Thread(target=room.compact_now, daemon=True).start()
        gate.set()
        until(lambda: any("compaction on a copy is dropped" in m and why in m for m in caplog.messages))
    wait_idle(room)
    assert not copied.exists() and room.fork is None and room.ready is None and (room.rp is None or room.rp.session_id != fork.session_id)
    if end in ("respec", "fail"):
        assert bound(tmp_path) == live
    if end == "fail":                                         # tried again at the next turn's end, as before
        gate.clear()
        say(y, room, "2", "CONTEXT:196000 again")
        wait_idle(room)
        until(lambda: room.fork is not None and room.fork is not fork)
        gate.set()


def test_the_turn_end_compaction_waits_for_a_healthy_copy_while_a_turn_still_fits_and_runs_as_before_past_that(y, tmp_path, gate, caplog):
    """Item 5: at 917,284, 49,716 under the line, a copy compacting leaves the room free (30,000 or more left); at 940,000,
    27,000 under it, the turn-end compaction runs as before and the copy is dropped. With no copy at all it runs at once."""
    owners_window(y)
    room = y.add_room(ROOM, Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:874665 hello")
        wait_idle(room)
        fork = room.fork
        say(y, room, "2", "CONTEXT:917284 and")
        wait_idle(room)
        assert compactions(tmp_path) == 1 and room.fork is fork and not room.busy      # only the copy's /compact
        assert any("a turn still fits, so the session waits for it" in m for m in caplog.messages)
        say(y, room, "3", "CONTEXT:940000 then")
        until(lambda: any("a turn ended at 940000" in m and "compacting" in m for m in caplog.messages))
        gate.set()                                            # the copy's thread sees it was ended
        until(lambda: any("compaction on a copy is dropped" in m and "end-of-turn" in m for m in caplog.messages))
        wait_idle(room)
        until(lambda: compactions(tmp_path) == 2)
    assert room.context == 20000 and room.fork is None and bound(tmp_path) == room.rp.session_id


def test_a_daemon_restart_with_a_copy_in_flight_loses_the_copy_and_nothing_else(y, tmp_path, gate, caplog):
    """Item 8: the daemon stops while a copy compacts: the copy ends with it, its transcript copy removed and never bound;
    after the restart the room resumes its live session, every message answered, with no tail and no compaction."""
    band(y)
    room = y.add_room(ROOM, chan := Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")
        wait_idle(room)
        live, fork = room.rp.session_id, room.fork
        (copied := fork.projects / f"{fork.session_id}.jsonl").parent.mkdir(parents=True, exist_ok=True)
        copied.write_text("{}\n")
        started(tmp_path)                                     # its /compact is running
        room.shutdown()
        gate.set()
        until(lambda: any("compaction on a copy is dropped" in m and "daemon is stopping" in m for m in caplog.messages))
    assert not copied.exists() and bound(tmp_path) == live
    again = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))   # the restart
    again.budgets["session"]["precompact_within"] = 0
    room = again.add_room(ROOM, chan := Chan())
    say(again, room, "2", "after the restart")
    wait_idle(room)
    assert chan.replies == ["echo: after the restart"] and room.rp.session_id == live == bound(tmp_path)
    assert compactions(tmp_path) == 1 and not (tmp_path / "rooms" / ROOM / "copy-tail").exists()   # the copy's own, cut
    room.rp.close()


def test_a_taken_copys_tail_waits_through_a_restart_and_rides_the_next_message_once(y, tmp_path, gate, caplog):
    """The room takes a copy at a turn's end with nothing waiting, so the tail waits in the room's folder. The daemon restarts
    before the next message, as an idle room's process closes: the room resumes the copy, the next message carries the tail
    once and the one after it nothing."""
    band(y)
    room = y.add_room(ROOM, chan := Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")
        wait_idle(room)
        copy = room.fork.session_id
        say(y, room, "2", "CONTEXT:196000 SLOW still here")
        until(lambda: room.reading)
        gate.set()
        until(lambda: room.ready is not None)
        wait_idle(room)
    kept = tmp_path / "rooms" / ROOM / "copy-tail"
    assert room.rp.session_id == copy == bound(tmp_path) and kept.exists()
    room.shutdown()
    again = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))   # the restart
    again.budgets["session"]["precompact_within"] = 0
    room = again.add_room(ROOM, chan := Chan())
    say(again, room, "3", "CONTEXT:21000 after the restart")
    wait_idle(room)
    first = echoed(chan, 0)
    assert room.rp.session_id == copy == bound(tmp_path) and not kept.exists()
    assert first.startswith(TAIL_HEAD) and "owner: CONTEXT:196000 SLOW still here" in first and first.endswith("\n\nCONTEXT:21000 after the restart")
    say(again, room, "4", "CONTEXT:22000 and once more")
    wait_idle(room)
    assert TAIL_HEAD not in echoed(chan, 1)
    room.rp.close()


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


def copy_row(y, tmp_path):
    """The room's `copy` in the /status answer as the daemon builds it (status.collect), no systemd asked."""
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, y.accounts, y.cooldowns, y.ledger, y.rooms, units=set, timers=SimpleNamespace(timers=dict))
    return next(r["copy"] for r in s["rooms"] if r["room"] == ROOM)


def test_the_status_row_says_a_copys_seconds_while_it_compacts_or_waits_for_the_turns_end_and_null_with_none(y, tmp_path):
    """Seen live: a deploy restarted the daemon 62 s into a room's copy, which the /status answer
    never named, so nothing could wait for it. Each room's row says how long its copy in flight has been going, from its
    start (Room.forked, while Room.fork compacts; the third of Room.ready, compacted and held for the running turn's end),
    and null when the room has none."""
    room = y.add_room(ROOM, Chan())
    assert copy_row(y, tmp_path) is None
    room.fork, room.forked = SimpleNamespace(), time.time() - 30      # compacting, 30 s in
    assert 30 <= copy_row(y, tmp_path) < 32
    room.fork = room.forked = None
    room.ready = (SimpleNamespace(), SimpleNamespace(), time.time() - 45)   # compacted, held for the turn's end
    assert 45 <= copy_row(y, tmp_path) < 47
    room.ready = None
    assert copy_row(y, tmp_path) is None


def test_the_room_keeps_its_copys_start_from_fork_off_to_the_take_and_the_row_reads_it(y, tmp_path, gate, caplog):
    """The start is set as a copy starts (_fork_off) and cleared as the room takes it, whether at once or held for the turn
    that ran while it compacted; the row follows it to null."""
    band(y)
    room = y.add_room(ROOM, Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")            # in the band: a copy compacts, held at the gate
        wait_idle(room)
        assert room.fork is not None and 0 <= time.time() - room.forked < 10 and 0 <= copy_row(y, tmp_path) < 10
        gate.set()                                           # no turn runs: taken at once
        until(lambda: any("compacted on a copy" in m for m in caplog.messages))
    assert room.fork is None and room.forked is None and room.ready is None and copy_row(y, tmp_path) is None
    gate.clear()
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "2", "CONTEXT:195000 again")            # the next copy, from the taken one
        wait_idle(room)
        fork = room.fork
        assert fork is not None and room.forked is not None
        say(y, room, "3", "CONTEXT:196000 SLOW still here")  # a turn runs while it compacts
        until(lambda: room.reading)
        gate.set()
        until(lambda: room.ready is not None or room.rp is fork)
        assert room.forked is None                           # held for the turn's end, its start in `ready`
        wait_idle(room)
    assert room.rp is fork and room.ready is None and room.forked is None and copy_row(y, tmp_path) is None


@pytest.mark.parametrize("end", ["respec", "fail", "stop"])
def test_the_copys_start_is_cleared_on_each_kind_of_drop_and_the_row_says_null(y, tmp_path, gate, monkeypatch, caplog, end):
    """A drop by a change of the live session (_drop_fork), by its own compaction failing, and by the daemon stopping
    (Room.shutdown, the deploy's case): the room keeps no start and the row says null."""
    band(y)
    if end == "fail":
        monkeypatch.setenv("FAKE_CLAUDE_COMPACT_FAIL", "1")
    room = y.add_room(ROOM, Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")
        wait_idle(room)
        assert room.fork is not None and room.forked is not None and copy_row(y, tmp_path) is not None
        if end == "respec":
            room.respec()
        elif end == "stop":
            started(tmp_path)                                # its /compact is running
            room.shutdown()
        if end != "fail":                                    # ended at once, before its thread sees it
            assert room.fork is None and room.forked is None and copy_row(y, tmp_path) is None
        gate.set()
        until(lambda: any("compaction on a copy is dropped" in m for m in caplog.messages))
    assert room.fork is None and room.forked is None and room.ready is None and copy_row(y, tmp_path) is None


@pytest.mark.parametrize("key,line", [(None, COMPACT), ("  keep   the\n map ", "/compact keep the map"), ("", "/compact"), (False, "/compact")])
def test_every_compaction_says_on_one_line_what_its_summary_keeps(y, tmp_path, written, key, line):
    """The one before an overflow's rerun, the held one (`yerk room compact`) and the copy's: the room's own compact_focus,
    else Yerk AI's; empty or false, /compact alone and Claude Code's own summary."""
    if key is not None:
        y.room_confs[ROOM]["compact_focus"] = key
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "TOO_LONG_ONCE hello")
    wait_idle(room)
    assert compacts(written, False) == [line] and chan.replies == ["echo: TOO_LONG_ONCE hello"]
    say(y, room, "2", "CONTEXT:200000 hello")
    wait_idle(room)
    assert room.compact_now() == "ok" and compacts(written, False) == [line, line] and compacts(written, True) == []
    say(y, room, "3", "CONTEXT:200000 and now?")
    wait_idle(room)
    reap(y, room, IDLE)
    until(lambda: room.fork is None and compactions(tmp_path) == 3)
    assert compacts(written, True) == [line] and compacts(written, False) == [line, line]
    wait_idle(room)
    room.rp.close()


def test_yerks_own_words_for_a_compaction_are_one_short_line_and_ask_for_guesses_to_be_marked():
    """A slash command ends at its line, and the words ride every compaction of every room. What they are for, off the
    2026-11-23 reading: orders and what is owed survive, and a guess comes back marked as one."""
    text = words.COMPACT_FOCUS
    assert text == " ".join(text.split()) and not text.startswith("/") and len(text) <= 500
    assert all(w in text for w in ("by default", "standing order", "still owed", "UNVERIFIED:"))
