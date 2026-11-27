"""Checks the copy started in the middle of a long turn ([session] precompact_midturn, off by default), a copy of the session compacted in the
background that becomes the room's session once it is ready. A room learns its session's size only as a turn ends, so one turn that starts under
the copy's band and grows across it ends in the held compaction, and a message then waits about two minutes (the 96 hours to 2026-11-18 00:59: 7
of 1,189 turns grew over 100,000, one ended 7,544 under the held line). On, the copy starts at the call whose message_start first reaches the
band. Every test drives a GROW turn of the fake (tests/fake_claude.py), its calls held after each message_start until the test lets them go on."""
import json
import logging
import threading

import pytest
from test_idle_compact import FAKE, ROOM, Cards, Chan, bound, compactions, echoed, say, until, wait_idle

from yerk_ai import words
from yerk_ai.accounts import Account
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import RoomProcess, Turn
from yerk_ai.room import TAIL_CUT, TAIL_HEAD, TAIL_RAN

MIDTURN = "a turn crossed the copy's band mid-turn"
WOULD = "would have started a copy mid-turn"


@pytest.fixture
def y(tmp_path, monkeypatch):
    """The example room with the copy's band on: 0.1 of its 250,000 window, so a call that starts from 192,000 up is in it."""
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "mark"))
    monkeypatch.setenv("FAKE_CLAUDE_GROW_GATE", str(tmp_path / "grow"))
    d = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))
    d.budgets["session"]["precompact_within"] = 0.1
    return d


@pytest.fixture
def gate(monkeypatch):
    """The copy's /compact held until the test opens the gate (test_idle_compact's)."""
    opened = threading.Event()
    real = RoomProcess.read_turn

    def read_turn(self, *a, **k):
        t = real(self, *a, **k)
        if self.fork_of:
            opened.wait(20)
        return t
    monkeypatch.setattr(RoomProcess, "read_turn", read_turn)
    return opened


def step(tmp_path, n=1):
    """The next `n` calls of a GROW turn go on past their message_start."""
    with open(tmp_path / "grow", "a") as f:
        f.write("go\n" * n)


def forks(tmp_path):
    return [a for a in (json.loads(x)["argv"] for x in (tmp_path / "argv.jsonl").read_text().splitlines()) if "--fork-session" in a]


def room_in(y, mode, chan=None):
    if mode is not None:
        y.room_confs[ROOM]["precompact_midturn"] = mode
    return y.add_room(ROOM, chan if chan is not None else Chan())


def owners(y):
    """A busy room's numbers: a 1,000,000 window, Claude Code's line at 967,000, the copy's band from 817,000
    (0.15), the held compaction from 917,000, and a copy waited for while 30,000 or more is left under the line."""
    y.budgets["session"]["precompact_within"] = 0.15
    y.room_confs[ROOM]["compact_window"] = 1000000


def lines(caplog, text):
    return [m for m in caplog.messages if text in m]


def test_a_turn_that_crosses_the_copys_band_mid_turn_starts_the_copy_at_that_call_and_the_room_takes_it_as_the_turn_ends(y, tmp_path, gate, caplog):
    """The copy starts as the call that reaches the band starts (its message_start, before its assistant event), the turn
    running; it compacts while the turn runs and the room takes it as the turn ends, so the next message runs on it."""
    room = room_in(y, "on", chan := Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "GROW:150000,195000,200000 CONTEXT:201000 the long job")
        until(lambda: room.midturn_at == 150000)
        assert room.fork is None                              # under the band's 192,000
        step(tmp_path)
        until(lambda: room.midturn_at == 195000)              # the second call has started, its assistant event held
        until(lambda: room.fork is not None and len(forks(tmp_path)) == 1)   # its process starts on the copy's thread
        assert room.busy and room.reading
        live, copy = room.rp.session_id, room.fork.session_id
        gate.set()
        until(lambda: room.ready is not None)                 # compacted while the turn runs: held for its end
        assert room.rp.session_id == live
        step(tmp_path, 2)
        wait_idle(room)
    assert room.rp.session_id == copy == bound(tmp_path) and room.fork is None and room.ready is None
    assert compactions(tmp_path) == 1 and room.context == 20000 and len(forks(tmp_path)) == 1
    assert lines(caplog, f"{MIDTURN} at 195000 of its 250000 tokens, 22000 under Claude Code's line, compacting a copy")
    assert "1 turn(s) overtook it" in lines(caplog, "now the room's")[0]
    say(y, room, "2", "CONTEXT:21000 and then?")
    wait_idle(room)
    first = echoed(chan, 1)
    assert first.startswith(f"{TAIL_HEAD}\n\n{TAIL_RAN}\n") and first.endswith("\n\nCONTEXT:21000 and then?")
    assert "owner: GROW:150000,195000,200000 CONTEXT:201000 the long job" in first and first.count(TAIL_RAN) == 1


def test_the_size_mid_turn_comes_from_each_calls_message_start_and_a_subagents_calls_are_not_the_rooms(y, tmp_path, caplog):
    """2.1.321, a real capture with one subagent (2026-11-18): every stream_event carries parent_tool_use_id, null on the
    room's own thread, and a subagent streams no message_start; its calls come as assistant events with the id set. The size
    mid-turn is each of the room's calls' as it starts; a subagent's, or a message_start that names no thread, moves nothing."""
    def start(n, **thread):
        return {"type": "stream_event", **thread, "event": {"type": "message_start", "message": {"usage": {
            "input_tokens": 2, "cache_read_input_tokens": n - 2, "cache_creation_input_tokens": 0}}}}

    def call(n, parent=None):
        return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [], "usage": {"input_tokens": n}}}
    t = Turn(started=0)
    t.observe(1, start(150000, parent_tool_use_id=None))
    assert (t.reached, t.context) == (150000, 0)              # as the call starts, before any assistant event of it
    t.observe(2, call(150000))
    t.observe(3, start(900000, parent_tool_use_id="toolu_sub"))   # a subagent's, were one streamed
    t.observe(4, start(910000))                                   # no thread named: an unattributed figure
    t.observe(5, call(920000, parent="toolu_sub"))
    assert (t.reached, t.context) == (150000, 150000)
    t.observe(6, start(195000, parent_tool_use_id=None))
    assert (t.reached, t.context) == (195000, 150000)
    room = room_in(y, "on")
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:150000 SUBAGENT:900000 hello")   # its subagent's message_start and call say 900,000
        wait_idle(room)
    assert room.fork is None and not forks(tmp_path) and not lines(caplog, "compacting a copy")
    assert room.midturn_at == 150000 and y.ledger.rows(1)[0]["context"] == 150000
    room.rp.close()


def test_the_size_mid_turn_still_moves_on_the_rooms_own_assistant_event_when_no_message_start_is_the_rooms():
    """The other feed. A stream whose message_start the room cannot call its own (none streamed, or one that names no thread,
    as a later Claude Code may send) still moves the size mid-turn, one event later: from the room's own assistant event, the
    one that sets the size a turn records. A subagent's assistant event moves neither."""
    def call(n, parent=None):
        return {"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [], "usage": {"input_tokens": n}}}
    t = Turn(started=0)
    t.observe(1, {"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 830000}}}})
    assert (t.reached, t.context) == (0, 0)                       # no thread named: left out
    t.observe(2, call(830000))
    assert (t.reached, t.context) == (830000, 830000)             # the room's own call, as it is recorded
    t.observe(3, call(900000, parent="toolu_sub"))
    assert (t.reached, t.context) == (830000, 830000)


def test_the_size_a_turn_records_is_the_same_with_message_start_read(y, tmp_path):
    """The ledger's context column and the room's size are fed as before, from the room's assistant events: every turn of
    the fake streams a message_start now, and a call that started and never finished (here a /stop while it ran) is no
    size the turn reached for its row."""
    room = room_in(y, "on")                                   # on, its sizes under the band: nothing copies
    step(tmp_path, 2)
    say(y, room, "1", "CONTEXT:120000 hello")
    wait_idle(room)
    say(y, room, "2", "GROW:130000,140000 CONTEXT:141000 a job")
    wait_idle(room)
    assert [r["context"] for r in y.ledger.rows(2)][::-1] == [120000, 141000] and room.context == 141000
    say(y, room, "3", "GROW:150000,180000 CONTEXT:181000 cut short")
    until(lambda: room.midturn_at == 150000)
    step(tmp_path)
    until(lambda: room.midturn_at == 180000)                  # the second call started: no assistant event of it yet
    assert room.stop()
    wait_idle(room)
    row = y.ledger.rows(1)[0]
    assert (row["outcome"], row["context"], room.context) == ("stopped", 150000, 150000) and not forks(tmp_path)


def test_off_by_default_a_turn_crossing_the_band_mid_turn_starts_no_copy_and_writes_no_line(y, tmp_path, gate, caplog):
    """Unset in the example layer, in rooms.toml.example and in budgets.toml: the room is off, and a turn that crosses the
    band mid-turn copies at its end exactly as before."""
    assert "precompact_midturn" not in y.budgets["session"] and all("precompact_midturn" not in c for c in y.room_confs.values())
    room = room_in(y, None)
    assert room.midturn == "off"
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        step(tmp_path, 3)
        say(y, room, "1", "GROW:150000,195000,200000 CONTEXT:201000 the long job")
        wait_idle(room)
        until(lambda: room.fork is not None and len(forks(tmp_path)) == 1)
    assert lines(caplog, "a turn ended at 201000 of its 250000 tokens")   # its end's copy, as before
    assert not lines(caplog, "mid-turn") and room.midturn_at == 0
    gate.set()
    until(lambda: room.fork is None and compactions(tmp_path) == 1)
    room.rp.close()


def test_log_only_writes_one_would_have_started_line_per_turn_and_the_turns_end_copies_as_before(y, tmp_path, gate, caplog):
    """`log`: the line, once a turn however many of its calls are in the band, and nothing started; the turn's end
    copies as it does today."""
    room = room_in(y, "log")
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        step(tmp_path, 3)
        say(y, room, "1", "GROW:195000,198000,200000 CONTEXT:201000 the long job")
        wait_idle(room)
        until(lambda: room.fork is not None)
        assert len(lines(caplog, WOULD)) == 1 and lines(caplog, "a turn ended at 201000 of its 250000 tokens")
        assert f"room {ROOM}: {WOULD} at 195000 of its 250000 tokens, 22000 under Claude Code's line, " in lines(caplog, WOULD)[0]
        gate.set()
        until(lambda: room.fork is None and compactions(tmp_path) == 1)      # taken: the room's session is the copy now
        step(tmp_path, 2)
        say(y, room, "2", "GROW:150000,196000 CONTEXT:197000 again")
        wait_idle(room)
    assert len(lines(caplog, WOULD)) == 2 and not lines(caplog, MIDTURN)
    assert len(forks(tmp_path)) == 2 and len(lines(caplog, "a turn ended at")) == 2
    room.rp.close()


@pytest.mark.parametrize("mode", ["log", "on"])
def test_one_mid_turn_copy_per_turn_and_none_while_a_copy_compacts_or_waits_to_be_taken(y, tmp_path, gate, monkeypatch, caplog, mode):
    """A copy from the last turn's end still compacting, then compacted and waiting for this turn's end: no copy and no
    line mid-turn, and the copy taken carries no mid-turn line. On, a mid-turn copy whose own compaction failed is not
    made again in the same turn."""
    room = room_in(y, "off", chan := Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")             # ends in the band: its end's copy compacts
        wait_idle(room)
        fork, room.midturn = room.fork, mode
        say(y, room, "2", "GROW:196000,200000,203000 CONTEXT:204000 the long job")
        until(lambda: room.midturn_at == 196000)
        step(tmp_path)
        until(lambda: room.midturn_at == 200000)
        gate.set()
        until(lambda: room.ready is not None)                 # compacted, waiting for this turn's end
        step(tmp_path, 2)
        wait_idle(room)
        assert room.fork is None and room.rp.session_id == fork.session_id and len(forks(tmp_path)) == 1
        assert not lines(caplog, WOULD) and not lines(caplog, MIDTURN)
        say(y, room, "3", "CONTEXT:21000 next")
        wait_idle(room)
        assert echoed(chan, 2).startswith(TAIL_HEAD) and TAIL_RAN not in echoed(chan, 2)
    if mode == "log":
        return
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_FAIL", "1")       # a copy's process reads it; the room's own is running
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "4", "GROW:150000,196000,200000,203000 CONTEXT:204000 once")
        step(tmp_path)
        until(lambda: room.midturn_at == 196000)
        until(lambda: lines(caplog, "its compaction failed"))   # the mid-turn copy is gone, the turn still runs
        step(tmp_path, 3)
        wait_idle(room)                                       # its calls at 200,000 and 203,000 came after the drop
    assert room.midturn_at == 204000 and len(lines(caplog, MIDTURN)) == 1 and lines(caplog, "a turn ended at 204000")


def test_the_turn_running_at_the_fork_reaches_the_copy_once_in_its_tail_with_the_mid_turn_line_and_counts_as_overtaking(y, tmp_path, gate, caplog):
    """The turn ends before the copy compacts and the next turn overtakes it too: the copy's first message carries both
    exchanges once, in order, the line ahead of the first alone; the copy says two turns overtook it."""
    room = room_in(y, "on", chan := Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        step(tmp_path, 2)
        say(y, room, "1", "GROW:150000,195000 CONTEXT:199000 the long job")
        wait_idle(room)
        copy = room.fork.session_id
        say(y, room, "2", "CONTEXT:200000 meanwhile")
        wait_idle(room)
        gate.set()
        until(lambda: room.fork is None and bound(tmp_path) == copy)
        say(y, room, "3", "CONTEXT:21000 after")
        wait_idle(room)
        say(y, room, "4", "CONTEXT:22000 and after that")
        wait_idle(room)
    first = echoed(chan, 2)
    assert first.startswith(f"{TAIL_HEAD}\n\n{TAIL_RAN}\n[") and first.count(TAIL_RAN) == 1 and first.count(TAIL_HEAD) == 1
    assert first.index("owner: GROW:150000,195000") < first.index("the room: echo: GROW:") < first.index("owner: CONTEXT:200000 meanwhile")
    assert first.index(TAIL_RAN) < first.index("owner: GROW:") and first.endswith("\n\nCONTEXT:21000 after")
    assert "2 turn(s) overtook it" in lines(caplog, "now the room's")[0] and TAIL_HEAD not in echoed(chan, 3)


def test_a_mid_turn_copys_first_exchange_keeps_its_reply_whole_past_copy_tail(y, tmp_path, gate, caplog):
    """Past `copy_tail` the cut starts at the exchange after the mid-turn one: the copy holds that turn cut off, so its
    reply is what says it was answered; the newest stays whole, the one between is named by its day-log line."""
    y.budgets["session"]["copy_tail"] = 600
    room = room_in(y, "on", chan := Chan())
    long = "x" * 900
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        step(tmp_path, 2)
        say(y, room, "1", f"GROW:150000,195000 CONTEXT:199000 the long job {long}")
        wait_idle(room)
        say(y, room, "2", "CONTEXT:199500 between")
        wait_idle(room)
        say(y, room, "3", "CONTEXT:200000 newest")
        wait_idle(room)
        gate.set()
        until(lambda: room.fork is None and compactions(tmp_path) == 1)
        say(y, room, "4", "CONTEXT:21000 after")
        wait_idle(room)
    first = echoed(chan, 3)
    assert f"the room: echo: GROW:150000,195000 CONTEXT:199000 the long job {long}\n" in first and len(first) > 1800
    assert TAIL_CUT in first and "owner: CONTEXT:199500 between" not in first and "owner: CONTEXT:200000 newest" in first
    assert first.index(TAIL_RAN) < first.index(TAIL_CUT) < first.index("owner: CONTEXT:200000 newest")


def test_a_copy_that_compacts_before_its_turn_ends_is_taken_as_it_ends_and_nothing_compacts_held(y, tmp_path, gate, caplog):
    """A busy room's case: a turn that starts under the band (817,000) and ends past the held line (917,000). The
    copy starts at 820,000, compacts while the turn runs, and is taken as the turn ends at 945,000, 22,000 under the line,
    where with no copy the live session would compact with the room held; the turn's end reads the copy's size instead."""
    owners(y)
    room = room_in(y, "on", chan := Cards())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "GROW:800000,820000,900000 CONTEXT:945000 the long job")
        step(tmp_path, 2)
        until(lambda: room.fork is not None)
        gate.set()
        until(lambda: room.ready is not None)
        step(tmp_path)
        wait_idle(room)
        say(y, room, "2", "CONTEXT:21000 next")
        wait_idle(room)
    assert lines(caplog, f"{MIDTURN} at 820000 of its 1000000 tokens, 147000 under Claude Code's line")
    assert compactions(tmp_path) == 1 and not lines(caplog, "compact (end-of-turn)") and room.fork is None
    assert not any(words.COMPACTING in text for _, text in chan.cards) and room.context == 21000
    assert bound(tmp_path) == room.rp.session_id and echoed(chan, 1).startswith(f"{TAIL_HEAD}\n\n{TAIL_RAN}\n")
    room.rp.close()


def test_a_turn_that_ends_before_its_mid_turn_copy_waits_for_it_within_copy_room_and_compacts_held_past_it(y, tmp_path, gate, caplog):
    """The copy still compacting as the turn ends at 930,000, 37,000 under the line: the room is left free for it, as for a
    turn-end copy (copy_room). The next turn ends at 940,000, 27,000 under: compacted held as before, the copy dropped."""
    owners(y)
    room = room_in(y, "on")
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        step(tmp_path, 2)
        say(y, room, "1", "GROW:800000,820000 CONTEXT:930000 the long job")
        wait_idle(room)
        fork = room.fork
        until(lambda: compactions(tmp_path) == 1)             # the copy's own /compact, held
        assert fork is not None and lines(caplog, "a turn still fits, so the session waits for it")
        say(y, room, "2", "CONTEXT:940000 then")
        until(lambda: lines(caplog, "a turn ended at 940000") and lines(caplog, "compacting"))
        gate.set()
        until(lambda: lines(caplog, "compaction on a copy is dropped") and lines(caplog, "end-of-turn"))
        wait_idle(room)
        until(lambda: compactions(tmp_path) == 2)
    assert room.context == 20000 and room.fork is None and bound(tmp_path) == room.rp.session_id != fork.session_id
    room.rp.close()


@pytest.mark.parametrize("end", ["stop", "fail"])
def test_a_stopped_or_failed_turn_drops_the_copy_made_in_it(y, tmp_path, gate, caplog, end):
    """/stop, or a failure the turn cannot get past, while the copy made in it compacts: the copy holds the turn cut off under
    a summary that says to pick it up, and no reply says it ended, so it goes; the room stays on its live session."""
    room = room_in(y, "on", chan := Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        step(tmp_path, 1)
        say(y, room, "1", f"GROW:150000,195000 CONTEXT:199000 {'FAIL_RATE ' if end == 'fail' else ''}the long job")
        until(lambda: room.fork is not None)
        live = room.rp.session_id
        if end == "stop":
            assert room.stop()
        else:
            step(tmp_path)
        wait_idle(room)
        gate.set()
        until(lambda: lines(caplog, "compaction on a copy is dropped") and lines(caplog, "the turn the copy was made in ended unanswered"))
    assert room.fork is None and room.ready is None and bound(tmp_path) == live and len(lines(caplog, MIDTURN)) == 1
    say(y, room, "2", "CONTEXT:21000 again")
    wait_idle(room)
    assert chan.replies[-1] == "echo: CONTEXT:21000 again" and bound(tmp_path) == live
    room.rp.close()


def test_claude_codes_own_compaction_inside_the_turn_drops_the_mid_turn_copy(y, tmp_path, gate, caplog):
    """The turn crosses the band, the copy starts, and then Claude Code compacts the live session itself inside the turn:
    the copy is moot and goes, as a turn-end copy does."""
    room = room_in(y, "on")
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        step(tmp_path, 2)
        say(y, room, "1", "GROW:150000,195000 AUTO_COMPACT CONTEXT:21000 the long job")
        until(lambda: room.fork is not None)
        live = room.rp.session_id
        wait_idle(room)
        gate.set()
        until(lambda: lines(caplog, "compaction on a copy is dropped") and lines(caplog, "the live session compacted meanwhile"))
    assert room.fork is None and room.context == 21000 and bound(tmp_path) == live
    room.rp.close()


def test_a_turn_already_in_the_band_at_its_first_call_with_no_copy_starts_one_there(y, tmp_path, gate, caplog):
    """No copy at the turn's start (here a room's first turn; else one dropped at the last turn's end) and the first call
    already in the band: the copy starts at that call, not at the turn's end."""
    room = room_in(y, "on")
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "GROW:196000,200000 CONTEXT:201000 the long job")
        until(lambda: room.midturn_at == 196000)
        until(lambda: room.fork is not None)
        assert room.reading and lines(caplog, f"{MIDTURN} at 196000 of its 250000 tokens")
        step(tmp_path, 2)
        wait_idle(room)
    assert len(forks(tmp_path)) == 1 and not lines(caplog, "a turn ended at")
    gate.set()
    until(lambda: room.fork is None and compactions(tmp_path) == 1)
    room.rp.close()


def test_a_call_that_starts_exactly_at_the_bands_start_is_in_the_band(y, tmp_path, caplog):
    """The band's start is inclusive, as _ended's is for a turn's end: 192,000 of this room's 250,000 (its line 217,000 less
    0.1 of the window). A call that starts one token short says nothing; the call that starts exactly there does."""
    room = room_in(y, "log")
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        step(tmp_path, 2)
        say(y, room, "1", "GROW:191999,192000 CONTEXT:192001 the long job")
        wait_idle(room)
        until(lambda: room.fork is None and compactions(tmp_path) == 1)   # its end is in the band too: that copy, as before
    assert [m.split(" tokens,")[0] for m in lines(caplog, WOULD)] == [f"room {ROOM}: {WOULD} at 192000 of its 250000"]
    room.rp.close()


def test_the_key_reads_from_rooms_toml_over_budgets_session_and_a_bad_value_is_off_and_said_once(y, tmp_path, caplog):
    """The key reads as precompact_within does: budgets.toml [session], a room's own over it. Any value but off, log and on
    is off, said once as the room starts, as a refused page key is."""
    y.budgets["session"]["precompact_midturn"] = "log"
    assert y.add_room(ROOM, Chan()).midturn == "log"
    y.room_confs[ROOM]["precompact_midturn"] = "on"
    assert y.add_room(ROOM, Chan()).midturn == "on"
    for bad in ("yes", True, "ON"):
        caplog.clear()
        y.room_confs[ROOM]["precompact_midturn"] = bad
        with caplog.at_level(logging.WARNING):
            room = y.add_room(ROOM, Chan())
        assert room.midturn == "off" and [m for m in caplog.messages if "precompact_midturn" in m] == [
            f"room {ROOM}: precompact_midturn = {bad!r} is none of off, log and on, so it is off"]
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        step(tmp_path, 3)
        say(y, room, "1", "GROW:150000,195000,200000 CONTEXT:201000 the long job")
        wait_idle(room)
    assert not lines(caplog, "mid-turn") and len(lines(caplog, "precompact_midturn")) == 1
    room.rp.close()


@pytest.mark.parametrize("why", ["no process", "stopping", "fresh next", "stopped", "held"])
def test_no_mid_turn_start_without_a_live_process_or_while_the_daemon_stops_or_a_fresh_session_is_next(y, tmp_path, caplog, why):
    """Every rule of _fork_off and _ended, mid-turn: no live process to copy from, the daemon stopping, a fresh session next
    (the file, as after a restart), the turn stopped, a compaction holding the room: the call in the band starts nothing."""
    room = room_in(y, "on")
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "GROW:150000,195000,200000 CONTEXT:201000 the long job")
        until(lambda: room.midturn_at == 150000)
        if why == "no process":
            room.rp.alive = lambda: False
        elif why == "stopping":
            room.closing = True
        elif why == "fresh next":
            (tmp_path / "rooms" / ROOM / "fresh-next").write_text("(a recap)")
        elif why == "stopped":
            room.stopped = True
        else:
            room.shortening = True
        step(tmp_path)
        until(lambda: room.midturn_at == 195000)
        assert room.fork is None and not forks(tmp_path)
        step(tmp_path, 2)
        until(lambda: room.midturn_at == 201000)              # the answer's own call, the turn's last
        assert not lines(caplog, MIDTURN) and not room.midturn_said
        room.closing = room.shortening = False
        wait_idle(room)
    assert not lines(caplog, MIDTURN)
    if room.rp:
        room.rp.close()
