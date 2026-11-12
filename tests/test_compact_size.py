"""Checks that after a compaction, when Claude Code summarizes a long session to make room, a room's size is the session's whole prompt as Claude
Code counts it (the get_context_usage control request, no model call), not a compact_boundary's post_tokens, which counts only the messages kept.
One night an owner's room read 12k on its card while its next call carried about 328k. No answer, today's numbers."""

import json
import logging
import time

import pytest
from test_idle_compact import FAKE, IDLE, ROOM, Chan, band, compactions, reap, say, until, wait_idle
from test_observe import Timers

from yerk_ai import engine, status
from yerk_ai.accounts import Account
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import RoomProcess, RoomSpec


@pytest.fixture
def y(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "mark"))
    d = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))
    d.budgets["session"]["precompact_within"] = 0
    return d


def shown(d, tmp_path):
    """The room card's 📚 line, as /status in the room gives it."""
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, d.accounts, d.cooldowns, d.ledger, d.rooms, now=time.time(), units=set, timers=Timers({}))
    return status.room_card(s, ROOM)[1]


def idle_compacted(y, chan, caplog, tmp_path):
    room = y.add_room(ROOM, chan)
    say(y, room, "1", "CONTEXT:200000 hello")             # past 0.7 of the 250,000 window: the reaper compacts it
    wait_idle(room)
    with caplog.at_level(logging.INFO):
        reap(y, room, IDLE)
        wait_idle(room)
    assert compactions(tmp_path) == 1
    return room


def test_a_held_compaction_leaves_the_room_its_row_and_its_card_at_the_size_claude_code_answers(y, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_CLAUDE_CONTEXT", "180000")
    room = idle_compacted(y, Chan(), caplog, tmp_path)
    row = y.ledger.rows(1)[0]
    assert (room.context, row["context"], row["outcome"]) == (180000, 180000, "compacted")
    assert shown(y, tmp_path).startswith("📚 180k/250k (72%)")
    assert any("compact (idle) ended compacted" in m and "200000 to 180000 tokens" in m for m in caplog.messages)
    room.rp.close()


def test_a_daemon_restart_after_a_compaction_reads_the_answered_size_from_the_ledger(y, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_CLAUDE_CONTEXT", "180000")
    idle_compacted(y, Chan(), caplog, tmp_path).rp.close()
    again = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))
    assert again.add_room(ROOM, Chan()).context == 180000 and shown(again, tmp_path).startswith("📚 180k/250k")


def test_a_copy_taken_gives_the_room_the_copys_answered_size(y, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_CLAUDE_CONTEXT", "150000")
    band(y)
    room = y.add_room(ROOM, Chan())
    with caplog.at_level(logging.INFO, logger="yerk.room"):
        say(y, room, "1", "CONTEXT:195000 hello")             # in the copy's band: a copy compacts and is taken
        wait_idle(room)
        until(lambda: any("compacted on a copy" in m for m in caplog.messages))
    row = y.ledger.rows(1)[0]
    assert (room.context, row["context"], row["outcome"], row["session_id"]) == (150000, 150000, "compacted", room.rp.session_id)
    assert shown(y, tmp_path).startswith("📚 150k/250k (60%)")
    room.rp.close()


def test_the_overflow_compactions_row_carries_the_answered_size(y, tmp_path, monkeypatch):
    """The compaction before a rerun: its row says the answered size; the room's own number waits for the rerun's turn, as
    on main (set there, a rerun with no usage would skip the turn-end compaction the old number starts)."""
    monkeypatch.setenv("FAKE_CLAUDE_CONTEXT", "120000")
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "TOO_LONG_ONCE hello")
    wait_idle(room)
    assert [r["context"] for r in y.ledger.rows(10) if r["outcome"] == "compacted"] == [120000] and chan.replies[-1].startswith("echo:")
    room.rp.close()


@pytest.mark.parametrize("how,why", [("", "answered get_context_usage is not supported"), ("silent", "no answer in 0 s"),
                                     ("exit", "the process ended")])
def test_no_answer_keeps_todays_numbers_and_the_room_goes_on(y, tmp_path, monkeypatch, caplog, how, why):
    monkeypatch.setenv("FAKE_CLAUDE_CONTEXT", how)
    monkeypatch.setattr(engine, "SIZE_WAIT", 0.4, raising=False)
    room = idle_compacted(y, chan := Chan(), caplog, tmp_path)
    assert room.context == 20000 == y.ledger.rows(1)[0]["context"]          # post_tokens, as today
    said = [m for m in caplog.messages if "did not say the session's size" in m]
    assert len(said) == 1 and why in said[0]
    if how == "exit":                                         # its end stays in the queue for the next reader
        assert room.rp.events.queue[-1][1] is None
    say(y, room, "2", "and now?")
    wait_idle(room)
    assert chan.replies == ["echo: CONTEXT:200000 hello", "echo: and now?"]
    assert [r["outcome"] for r in reversed(y.ledger.rows(10))] == ["ok", "compacted", "ok"]
    room.rp.close()


def test_a_message_sent_during_a_compaction_is_answered_after_it_in_order_with_one_row(y, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_CLAUDE_CONTEXT", "180000")
    monkeypatch.setenv("FAKE_CLAUDE_COMPACT_SLOW", "0.5")
    room = y.add_room(ROOM, chan := Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    reap(y, room, IDLE)
    until(lambda: compactions(tmp_path) == 1)
    say(y, room, "2", "and now?")                             # waits for the compaction and its ask, then runs
    wait_idle(room)
    assert chan.replies == ["echo: CONTEXT:200000 hello", "echo: and now?"]
    assert [(r["outcome"], r["context"]) for r in reversed(y.ledger.rows(10))][:2] == [("ok", 200000), ("compacted", 180000)]
    assert len(y.ledger.rows(10)) == 3
    room.rp.close()


def test_the_ask_is_claude_codes_own_request_and_a_recorded_answer_reads_as_its_total(y, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_CLAUDE_CONTEXT", "recorded")      # fixtures/context_usage.json: 2.1.321's answer after /compact
    monkeypatch.setenv("FAKE_CLAUDE_ASKS", str(asks := tmp_path / "asks.jsonl"))
    room = idle_compacted(y, Chan(), caplog, tmp_path)
    [line] = asks.read_text().splitlines(keepends=True)
    rid = json.loads(line)["request_id"]
    assert line == json.dumps({"type": "control_request", "request_id": rid, "request": {"subtype": "get_context_usage"}}) + "\n"
    assert room.context == 125775 == y.ledger.rows(1)[0]["context"]
    room.rp.close()


def test_an_ask_never_takes_a_turns_events(tmp_path, monkeypatch):
    """A background command's notice turn streams while nothing is asked (test_engine's): asked right then, the size
    comes back and the notice's events still wait for the next turn, which reads them first as before."""
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_CONTEXT", "5000")
    with RoomProcess(RoomSpec(room="r1", model="m", cwd=tmp_path, tools=["Read"]), Account("one", tmp_path / "cfg"), tmp_path) as rp:
        rp.start()
        assert rp.ask("BACKGROUND render the plans", timeout=20).text == "echo: BACKGROUND render the plans"
        assert rp.size() == 5000
        waiting = [e for _, e in list(rp.events.queue)]
        assert [e["type"] for e in waiting if e][-1] == "result" and "control_response" not in [e["type"] for e in waiting if e]
        turn = rp.ask("is it done?", timeout=20)
    assert (turn.outcome, turn.text, len(turn.accepted)) == ("ok", "echo: is it done?", 1)


def test_a_late_answer_to_an_earlier_ask_is_never_read_as_this_asks_size(tmp_path, monkeypatch):
    """An ask that gave up leaves its answer still to come: the next ask skips it by its request id, so a room never
    shows the size its session had at an earlier compaction."""
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_CONTEXT", "5000")
    with RoomProcess(RoomSpec(room="r1", model="m", cwd=tmp_path, tools=["Read"]), Account("one", tmp_path / "cfg"), tmp_path) as rp:
        rp.start()
        rp.answers.put({"type": "control_response", "response": {"subtype": "success", "request_id": "size-000000000000",
                                                                 "response": {"totalTokens": 999999}}})
        assert rp.size() == 5000
