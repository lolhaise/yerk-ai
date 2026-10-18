"""Checks the room runner on a fake Claude Code: a mid-turn message folds into the turn, idle close and resume, the account hop, the empty-reply retry."""
import json
import threading
import time
import tomllib
from pathlib import Path

import pytest

from yerk_ai import words
from yerk_ai.accounts import Account, Cooldowns
from yerk_ai.config import table
from yerk_ai.engine import RoomSpec
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.room import Inbound, Room

FAKE = Path(__file__).with_name("fake_claude.py")


class Chan:
    def __init__(self):
        self.cards, self.replies, self.notices = [], [], []
        self.done = threading.Event()

    def open_card(self, room, text, kind="turn"):
        self.cards.append(text)

    def progress(self, room, text):
        self.cards.append(text)

    def reply(self, room, text, inbound, card=None, key=None):
        self.replies.append((text, list(inbound)))
        self.keys = getattr(self, "keys", []) + [key]
        if card:
            self.cards.append(card)
        self.done.set()
        return [f"m{len(self.replies)}"]

    def notice(self, room, text, inbound, card=None, key=None):
        self.notices.append((text, list(inbound)))
        if card:
            self.cards.append(card)
        self.done.set()
        return ["n1"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "empty.mark"))
    ledger = Ledger(tmp_path / "ledger.sqlite")
    accounts = [Account("one", tmp_path / "cfg1"), Account("two", tmp_path / "cfg2")]

    data = tomllib.loads(table("failures.toml").read_text())
    data["rerun"]["wait"] = [0.0]                       # the reruns without the waits

    def make(chan, idle=1800.0, meters=None):
        spec = lambda: RoomSpec(room="r", model="m", cwd=tmp_path, tools=["Read"])  # noqa: E731
        return Room("r", {}, spec, accounts, chan, tmp_path, ledger, FailureTable(data),
                    Cooldowns(tmp_path / "cooldowns.json"), idle_seconds=idle, meters=meters)
    return make, ledger, tmp_path


def send(room, ledger, mid, text):
    ledger.inbound_add(mid, "r", "c", "owner", len(text))
    return room.submit(Inbound(mid, "owner", text))


def wait_idle(room, timeout=20):
    end = time.time() + timeout
    while (room.busy or room.queue) and time.time() < end:
        time.sleep(0.05)
    assert not room.busy


def argvs(tmp_path):
    return [json.loads(x) for x in (tmp_path / "argv.jsonl").read_text().splitlines()]


def test_mid_turn_message_folds_into_the_running_turn(env):
    make, ledger, _ = env
    chan = Chan()
    room = make(chan)
    assert send(room, ledger, "1", "SLOW first") == "started"
    time.sleep(0.4)
    assert send(room, ledger, "2", "second") == "folded"
    wait_idle(room)
    assert chan.replies == [("echo: SLOW first + second", ["1", "2"])]
    a, b = ledger.inbound_get("1"), ledger.inbound_get("2")
    assert a["state"] == b["state"] == "answered" and a["turn_id"] == b["turn_id"] and b["folded"] == 1
    assert chan.cards[0] == f"{words.WORKING} working on it" and "echo" in chan.cards[-1]
    room.rp.close()


def test_idle_process_closes_and_the_next_message_resumes(env):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan, idle=60)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    sid = room.rp.session_id
    assert not room.reap_idle(time.time() + 30)          # not idle long enough
    assert room.reap_idle(time.time() + 61) and room.rp is None
    send(room, ledger, "2", "again")
    wait_idle(room)
    last = argvs(tmp_path)[-1]["argv"]
    assert last[last.index("--resume") + 1] == sid and len(chan.replies) == 2
    room.rp.close()
    assert make(Chan()).last_human == ledger.inbound_get("2")["received"]   # the clock's gap survives a restart


def test_quota_failure_hops_to_the_next_account_in_the_same_turn(env, monkeypatch):
    make, ledger, tmp_path = env
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", str(tmp_path / "cfg1"))
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    assert chan.replies == [("echo: hello", ["1"])] and chan.cards.count(f"{words.WORKING} working on it") == 1
    rows = sorted(ledger.rows(5), key=lambda r: r["id"])
    assert [(r["account"], r["outcome"], r["failure"]) for r in rows] == [("one", "error", "quota"), ("two", "ok", None)]
    assert [r["hop"] for r in rows] == [None, "quota:one"] and rows[0]["source"] == "type:rate_limit"
    assert f"{words.WORKING} still on it, moved to account two" in chan.cards
    assert Cooldowns(tmp_path / "cooldowns.json").until("one") is not None
    second = argvs(tmp_path)[-1]
    assert second["config_dir"] == str(tmp_path / "cfg2") and "--session-id" in second["argv"]  # never answered: fresh
    room.rp.close()


def test_a_session_that_answered_before_resumes_on_the_next_account(env, monkeypatch):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    sid = room.rp.session_id
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", str(tmp_path / "cfg1"))
    room.rp.close()                                            # the next process sees account one failing
    send(room, ledger, "2", "again")
    wait_idle(room)
    last = argvs(tmp_path)[-1]
    assert last["config_dir"] == str(tmp_path / "cfg2") and last["argv"][last["argv"].index("--resume") + 1] == sid
    assert chan.replies[-1] == ("echo: again", ["2"])
    room.rp.close()


def test_empty_reply_retries_once_in_a_fresh_session(env):
    make, ledger, _ = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "EMPTY_ONCE please")
    wait_idle(room)
    rows = sorted(ledger.rows(5), key=lambda r: r["id"])
    assert [r["outcome"] for r in rows] == ["empty", "ok"] and rows[0]["session_id"] != rows[1]["session_id"]
    assert chan.replies == [("echo: EMPTY_ONCE please", ["1"])]
    room.rp.close()


def test_a_failure_with_no_way_forward_is_a_visible_notice(env, monkeypatch):
    make, ledger, tmp_path = env
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", f"{tmp_path / 'cfg1'},{tmp_path / 'cfg2'}")
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    assert not chan.replies and chan.notices and chan.notices[0][1] == ["1"]
    assert ledger.inbound_get("1")["state"] == "failed"


def rows_of(ledger, n=10):
    return sorted(ledger.rows(n), key=lambda r: r["id"])


def test_an_overloaded_reply_replayed_from_a_fixture_reruns_the_same_session(env):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "FIXTURE:overloaded hello")
    wait_idle(room)
    rows = rows_of(ledger)
    assert [(r["account"], r["outcome"], r["failure"], r["action"]) for r in rows] == [
        ("one", "error", "transient", "rerun"), ("one", "ok", None, None)]
    assert rows[0]["session_id"] == rows[1]["session_id"] and len(argvs(tmp_path)) == 1   # same process
    assert chan.replies == [("echo: FIXTURE:overloaded hello", ["1"])] and not chan.notices
    room.rp.close()


def test_an_overflow_compacts_then_runs_the_turn_again(env):
    make, ledger, _ = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "TOO_LONG_ONCE hello")
    wait_idle(room)
    rows = rows_of(ledger)
    assert [(r["outcome"], r["failure"]) for r in rows] == [("error", "overflow"), ("ok", None), ("ok", None)]
    assert len({r["session_id"] for r in rows}) == 1 and f"{words.WORKING} {words.COMPACTING}" in chan.cards
    assert chan.replies == [("echo: TOO_LONG_ONCE hello", ["1"])]
    room.rp.close()


def test_a_rejected_rate_limit_event_hops_like_a_quota_failure(env):
    make, ledger, _ = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "FIXTURE:rate_limit_rejected hello")
    wait_idle(room)
    rows = rows_of(ledger)
    assert [(r["account"], r["failure"], r["source"]) for r in rows] == [
        ("one", "quota", "event:rate_limit_rejected"), ("two", None, None)]
    room.rp.close()


def test_a_refusal_fallback_answer_stands_marked_and_the_next_turn_starts_fresh(env):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "FIXTURE:refusal_fallback hi")
    wait_idle(room)
    assert chan.replies[0] == ("answered on the fallback model", ["1"])
    assert chan.cards[-1] == f"{words.WORKING} answered by a backup model\n-# the next message starts fresh"
    first = rows_of(ledger)[-1]
    assert (first["outcome"], first["failure"], first["action"], first["fallback_model"]) == (
        "ok", "refusal", "fresh_next_turn", "claude-opus-4-8")
    send(room, ledger, "2", "again")
    wait_idle(room)
    second = rows_of(ledger)[-1]
    last = argvs(tmp_path)[-1]["argv"]
    assert "--session-id" in last and second["session_id"] != first["session_id"] and second["recap"] == 1
    assert chan.replies[1][0].startswith("echo: (Yerk AI: this is a new session.") and chan.replies[1][1] == ["2"]
    assert second["fallback_model"] is None
    room.rp.close()


def test_a_session_that_cannot_be_resumed_starts_fresh_with_a_one_line_recap(env, monkeypatch):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    sid = room.rp.session_id
    room.rp.close()
    monkeypatch.setenv("FAKE_CLAUDE_MISSING", "1")              # the transcript is gone
    send(room, ledger, "2", "again")
    wait_idle(room)
    rows = rows_of(ledger)
    assert [(r["outcome"], r["failure"], r["action"]) for r in rows[1:]] == [
        ("exited", "fatal", "fresh_with_recap"), ("ok", None, None)]
    assert rows[-1]["session_id"] != sid and rows[-1]["recap"] == 1
    text, ids = chan.replies[-1]
    assert ids == ["2"] and 'The last reply here began: "echo: hello")' in text
    assert text.startswith("echo: (Yerk AI: the earlier session") and text.endswith("\n\nagain")
    room.rp.close()


def test_meters_come_from_the_stream_per_account(env, monkeypatch):
    from yerk_ai.meters import Meters, meter_line
    make, ledger, tmp_path = env
    monkeypatch.setenv("FAKE_CLAUDE_WINDOWS", "0.07,0.03")
    meters = Meters(tmp_path / "meters.json")
    chan = Chan()
    room = make(chan, meters=meters.seen)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    m = meters.load()["one"]
    assert (m["five_hour"]["pct"], m["seven_day"]["pct"], m["source"]) == (7.0, 3.0, "stream")
    assert meter_line("one", m, now=m["at"] + 120).startswith("one: 5h 7% (") and "seen 2 min ago" in meter_line(
        "one", m, now=m["at"] + 120)
    room.rp.close()


def test_a_recap_survives_a_hop_that_has_to_start_fresh(env, monkeypatch):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    room.rp.close()
    monkeypatch.setenv("FAKE_CLAUDE_MISSING", "1")
    monkeypatch.setenv("FAKE_CLAUDE_AUTH_DIRS", str(tmp_path / "cfg1"))   # and account one's login is refused
    send(room, ledger, "2", "again")
    wait_idle(room)
    rows = rows_of(ledger)[1:]
    assert [(r["account"], r["failure"]) for r in rows] == [("one", "fatal"), ("one", "auth"), ("two", None)]
    assert rows[-1]["hop"] == "auth:one" and rows[-1]["recap"] == 1
    assert chan.replies[-1][0].startswith("echo: (Yerk AI: the earlier session") and chan.cards.count(f"{words.WORKING} working on it") == 2
    room.rp.close()
