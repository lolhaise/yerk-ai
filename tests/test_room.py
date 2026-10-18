"""Checks the room runner on a fake Claude Code: a mid-turn message folds into the turn, idle close and resume, the account hop, the empty-reply retry."""
import json
import threading
import time
from pathlib import Path

import pytest

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

    def progress(self, room, text):
        self.cards.append(text)

    def reply(self, room, text, inbound):
        self.replies.append((text, list(inbound)))
        self.done.set()
        return [f"m{len(self.replies)}"]

    def notice(self, room, text, inbound):
        self.notices.append((text, list(inbound)))
        self.done.set()
        return ["n1"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "empty.mark"))
    ledger = Ledger(tmp_path / "ledger.sqlite")
    accounts = [Account("one", tmp_path / "cfg1"), Account("two", tmp_path / "cfg2")]

    def make(chan, idle=1800.0):
        spec = lambda: RoomSpec(room="r", model="m", cwd=tmp_path, tools=["Read"])  # noqa: E731
        return Room("r", {}, spec, accounts, chan, tmp_path, ledger, FailureTable.load(table("failures.toml")),
                    Cooldowns(tmp_path / "cooldowns.json"), idle_seconds=idle)
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
    assert chan.cards[0] == "working on it" and "echo" in chan.cards[-1]
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
    assert chan.replies == [("echo: hello", ["1"])] and chan.cards.count("working on it") == 1
    rows = sorted(ledger.rows(5), key=lambda r: r["id"])
    assert [(r["account"], r["outcome"], r["failure"]) for r in rows] == [("one", "error", "quota"), ("two", "ok", None)]
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
