"""Checks the room runner on a fake Claude Code: a mid-turn message folds into the turn, idle close and resume, the account hop, the empty-reply retry."""
import json
import threading
import time
import tomllib
from pathlib import Path

import pytest

from yerk_ai import words
from yerk_ai.accounts import Account, Cooldowns
from yerk_ai.config import override, table
from yerk_ai.engine import RoomSpec
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.room import Inbound, Room, covered_by

FAKE = Path(__file__).with_name("fake_claude.py")


class Chan:
    def __init__(self):
        self.cards, self.replies, self.notices, self.silent_ones = [], [], [], []
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

    def silent(self, room, inbound, card, key=None):
        self.silent_ones.append(list(inbound))
        self.cards.append(card)
        self.done.set()
        return ["c1"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "empty.mark"))
    ledger = Ledger(tmp_path / "ledger.sqlite")
    accounts = [Account("one", tmp_path / "cfg1"), Account("two", tmp_path / "cfg2")]

    data = tomllib.loads(table("failures.toml").read_text())
    data["rerun"]["wait"] = [0.0]                       # the reruns without the waits

    def make(chan, idle=1800.0, meters=None, conf=None):
        conf = {"model": "m", **(conf or {})}      # rooms.toml's model, under the settings tool's override, as the daemon's spec reads them
        spec = lambda: RoomSpec(room="r", model=str(override(tmp_path, "r").get("model") or conf["model"]), cwd=tmp_path, tools=["Read"])  # noqa: E731
        return Room("r", conf, spec, accounts, chan, tmp_path, ledger, FailureTable(data),
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
    one, two = room.seen["SLOW first"], room.seen["second"]
    assert one.sent == "SLOW first" and two.sent == f"{words.FOLDED.format(n=round(two.received - one.received))}\n\nsecond"
    assert chan.replies == [(f"echo: SLOW first + {two.sent}", ["1", "2"])]      # the stdin line: the fold's line, then the text
    a, b = ledger.inbound_get("1"), ledger.inbound_get("2")
    assert a["state"] == b["state"] == "answered" and a["turn_id"] == b["turn_id"] and b["folded"] == 1
    assert chan.cards[0] == f"{words.WORKING} working on it" and "echo" in chan.cards[-1] and not room.folds
    room.rp.close()


def test_a_message_from_the_watched_channel_folds_with_no_line_ahead_since_its_own_line_says_how_it_is_answered(env):
    make, ledger, _ = env
    chan = Chan()
    room = make(chan)
    there = words.SENT_THERE.format(who="the other assistant", channel="elsewhere", quiet=words.NO_REPLY) + "\nwhat about EX-41?"
    assert send(room, ledger, "1", "SLOW first") == "started"
    time.sleep(0.4)
    assert send(room, ledger, "2", there) == "folded"
    wait_idle(room)
    assert chan.replies == [(f"echo: SLOW first + {there}", ["1", "2"])] and ledger.inbound_get("2")["folded"] == 1
    room.rp.close()


def test_messages_queued_during_a_turn_and_merged_by_claude_code_are_all_covered(env):
    # seen live on 2.1.316: the lines waiting when a turn ended came back as ONE replay; one
    # message counted per replay left two pending, so every later result went to a message two back and the turn never ended
    make, ledger, _ = env
    chan = Chan()
    room = make(chan)
    assert send(room, ledger, "1", "MERGE first") == "started"
    time.sleep(0.2)
    assert send(room, ledger, "2", "second") == "folded"
    assert send(room, ledger, "3", "third") == "folded"
    wait_idle(room)
    two, three = room.seen["second"].sent, room.seen["third"].sent     # each written with the fold's line ahead of it
    assert two.startswith("[sent ") and two.endswith("\n\nsecond") and three.startswith("[sent ") and three.endswith("\n\nthird")
    assert chan.replies == [("echo: MERGE first", ["1"]), (f"echo: {two}\n{three}", ["2", "3"])]
    rows = [ledger.inbound_get(i) for i in ("1", "2", "3")]
    assert [r["state"] for r in rows] == ["answered"] * 3
    assert rows[1]["turn_id"] == rows[2]["turn_id"] != rows[0]["turn_id"] and rows[1]["folded"] == rows[2]["folded"] == 1
    assert not room.pending and not room.inflight and not room.reading and not room.busy and not room.folds
    assert send(room, ledger, "4", "fourth") == "started"
    wait_idle(room)
    assert chan.replies[-1] == ("echo: fourth", ["4"])
    room.rp.close()


def test_covered_by_counts_each_message_a_replay_carries():
    assert covered_by(["a"], ["a"]) == 1
    assert covered_by(["a\nb\nc"], ["a", "b", "c", "d"]) == 3       # d was written after the replay: it stays pending
    assert covered_by(["x"], ["a", "b"]) == 1                       # matches nothing at the cursor: one, as before
    assert covered_by(["a", "b\nc"], ["a", "b", "c"]) == 3
    assert covered_by([], ["a"]) == 0


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


def test_a_hop_in_a_room_that_is_not_the_owners_names_no_account(env, monkeypatch):
    make, ledger, tmp_path = env
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", str(tmp_path / "cfg1"))
    chan = Chan()
    room = make(chan, conf={"privacy": "family"})              # the wall: such a room never learns an account name
    send(room, ledger, "1", "hello")
    wait_idle(room)
    rows = sorted(ledger.rows(5), key=lambda r: r["id"])
    assert chan.replies == [("echo: hello", ["1"])] and [r["hop"] for r in rows] == [None, "quota:one"]   # it did hop
    assert f"{words.WORKING} {words.MOVED_ON}" in chan.cards
    assert not any("account" in c or "two" in c.split() for c in chan.cards)
    room.rp.close()
    (tmp_path / "cooldowns.json").unlink()                     # one rests after that hop; the owner's room gets it fresh
    owner = Chan()
    room = make(owner)                                         # privacy unset: the owner's room, its note names the account
    send(room, ledger, "2", "again")
    wait_idle(room)
    assert owner.replies == [("echo: again", ["2"])] and f"{words.WORKING} still on it, moved to account two" in owner.cards
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


def test_the_account_the_room_is_put_on_starts_first_and_a_failure_on_it_still_hops_to_the_next(env, monkeypatch):
    make, ledger, tmp_path = env
    (tmp_path / "rooms" / "r").mkdir(parents=True)
    (tmp_path / "rooms" / "r" / "override.json").write_text(json.dumps({"account": "two"}))   # the settings tool's file
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    assert argvs(tmp_path)[-1]["config_dir"] == str(tmp_path / "cfg2")          # the pin, ahead of the order's first
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", str(tmp_path / "cfg2"))         # then the pinned account hits its limit
    room.rp.close()
    send(room, ledger, "2", "again")
    wait_idle(room)
    rows = rows_of(ledger)[1:]
    assert [(r["account"], r["outcome"], r["failure"]) for r in rows] == [("two", "error", "quota"), ("one", "ok", None)]
    assert rows[-1]["hop"] == "quota:two" and chan.replies[-1] == ("echo: again", ["2"])
    assert f"{words.WORKING} still on it, moved to account one" in chan.cards
    room.rp.close()
    send(room, ledger, "3", "still resting?")                                     # the pin rests: the next one starts
    wait_idle(room)
    assert argvs(tmp_path)[-1]["config_dir"] == str(tmp_path / "cfg1") and len(rows_of(ledger)) == 4
    monkeypatch.delenv("FAKE_CLAUDE_FAIL_DIRS")
    Cooldowns(tmp_path / "cooldowns.json").clear("two")                          # its rest is over
    send(room, ledger, "4", "back?")
    wait_idle(room)
    assert argvs(tmp_path)[-1]["config_dir"] == str(tmp_path / "cfg2") and chan.replies[-1] == ("echo: back?", ["4"])
    room.rp.close()


def test_a_pinned_account_resting_for_the_model_falls_through_in_order_and_is_tried_first_again(env, monkeypatch):
    """A pin holds the room to the account the owner set, and when that account has no usage left the room does what it
    did before the pin. Put on three of one, two, three: three out of the model goes to one, then two, in accounts.toml's
    order; the pin stays, so once its rest ends the next turn is on three again."""
    make, ledger, tmp_path = env
    f = tmp_path / "rooms" / "r" / "override.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"account": "three", "model": "m"}))                            # /model three m
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", f"{tmp_path / 'cfg3'},{tmp_path / 'cfg1'}")
    monkeypatch.setenv("FAKE_CLAUDE_RESETS", str(int(time.time()) + 3600))
    chan = Chan()
    room = make(chan)
    room.accounts.append(Account("three", tmp_path / "cfg3"))
    send(room, ledger, "1", "hello")
    wait_idle(room)
    assert [(r["account"], r["outcome"], r["hop"]) for r in rows_of(ledger)] == [
        ("three", "error", None), ("one", "error", "quota:three"), ("two", "ok", "quota:three,quota:one")]
    assert chan.replies == [("echo: hello", ["1"])] and json.loads(f.read_text()) == {"account": "three", "model": "m"}
    room.rp.close()
    monkeypatch.delenv("FAKE_CLAUDE_FAIL_DIRS")
    Cooldowns(tmp_path / "cooldowns.json").clear("three")                                    # its rest is over
    send(room, ledger, "2", "back?")
    wait_idle(room)
    assert argvs(tmp_path)[-1]["config_dir"] == str(tmp_path / "cfg3") and chan.replies[-1] == ("echo: back?", ["2"])
    room.rp.close()


def test_every_account_out_of_the_pinned_model_runs_on_the_rooms_own_on_the_pinned_account_and_both_pins_stay(env, monkeypatch):
    make, ledger, tmp_path = env
    f = tmp_path / "rooms" / "r" / "override.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"model": "m", "account": "two"}))                              # /model two m
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", f"{tmp_path / 'cfg1'},{tmp_path / 'cfg2'}")
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_MODEL", "m")                                        # both out of m; usual answers
    reset = int(time.time()) + 3600
    monkeypatch.setenv("FAKE_CLAUDE_RESETS", str(reset))
    chan = Chan()
    room = make(chan, conf={"model": "usual"})
    send(room, ledger, "1", "hello")
    wait_idle(room)
    assert [(r["account"], r["outcome"], r["model"], r["out_of"]) for r in rows_of(ledger)] == [
        ("two", "error", "m", None), ("one", "error", "m", None), ("two", "ok", "usual", "m")]
    assert chan.replies == [("echo: hello", ["1"])] and f"{words.WORKING} on usual, m back {words.clock(reset)}" in chan.cards
    assert json.loads(f.read_text()) == {"model": "m", "account": "two"}                    # no fallback drops a pin
    room.rp.close()


def test_an_empty_reply_resumes_the_same_session_once_more(env):
    """The bound session holds the day's context and a second resume costs seconds (seen live: the empty reply
    went straight to a new session)."""
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    sid = room.rp.session_id
    send(room, ledger, "2", "EMPTY_ONCE please")
    wait_idle(room)
    assert [(r["outcome"], r["session_id"], r["recap"]) for r in rows_of(ledger)] == [
        ("ok", sid, None), ("empty", sid, None), ("ok", sid, None)]
    last = argvs(tmp_path)[-1]["argv"]
    assert len(argvs(tmp_path)) == 2 and last[last.index("--resume") + 1] == sid       # a new process, the same session
    assert chan.replies[-1] == ("echo: EMPTY_ONCE please", ["2"]) and f"{words.WORKING} {words.EMPTY_AGAIN}" in chan.cards
    room.rp.close()


def test_an_empty_reply_twice_starts_fresh_through_the_recap(env):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    sid = room.rp.session_id
    send(room, ledger, "2", "EMPTY_TWICE please")
    wait_idle(room)
    assert [(r["outcome"], r["session_id"] == sid, r["recap"]) for r in rows_of(ledger)[1:]] == [
        ("empty", True, None), ("empty", True, None), ("ok", False, 1)]
    text, ids = chan.replies[-1]
    assert ids == ["2"] and text.startswith("echo: (Yerk AI: the earlier session") and "the day log holds what was said" in text
    assert 'The last reply here began: "echo: hello")' in text and text.endswith("\n\nEMPTY_TWICE please")
    assert f"{words.WORKING} {words.EMPTY_AGAIN}" in chan.cards and f"{words.WORKING} {words.NEW_SESSION}" in chan.cards
    room.rp.close()


def test_a_final_of_exactly_no_reply_posts_nothing_its_card_says_so_and_it_is_ledgered_silent(env):
    """watch-note (a room reading along where the owner talks to the other assistant only reads):
    a final that is NO_REPLY once stripped posts nothing, is never bounced or asked again, and its card says the room
    read the message and had nothing to add; the message is answered, noted silent. One that only holds NO_REPLY is posted as
    it is, and an empty reply still goes back to the session once, as above."""
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "READ_ALONG the owner, to the other assistant")
    wait_idle(room)
    assert chan.replies == [] and chan.notices == [] and chan.silent_ones == [["1"]]
    assert chan.cards[-1].split("\n")[0] == f"{words.DONE} {words.QUIET}" and room.last_reply == ""
    assert {k: ledger.inbound_get("1")[k] for k in ("state", "note", "reply_ids")} == {"state": "answered", "note": "silent", "reply_ids": "c1"}
    assert [r["outcome"] for r in rows_of(ledger)] == ["ok"] and len(argvs(tmp_path)) == 1    # one run: no bounce, no retry
    send(room, ledger, "2", "is NO_REPLY a word")
    wait_idle(room)
    send(room, ledger, "3", "EMPTY_ONCE then")
    wait_idle(room)
    assert chan.replies == [("echo: is NO_REPLY a word", ["2"]), ("echo: EMPTY_ONCE then", ["3"])] and chan.silent_ones == [["1"]]
    assert [r["outcome"] for r in rows_of(ledger)] == ["ok", "ok", "empty", "ok"] and f"{words.WORKING} {words.EMPTY_AGAIN}" in chan.cards
    assert ledger.inbound_get("2")["note"] is None
    room.rp.close()


def test_a_resume_that_first_reports_orphaned_commands_answers_on_the_same_session(env, monkeypatch):
    """The live shape (ledger turns 369 and 372): the process died with two background commands
    running; on the next message's resume Claude Code first reported them in a turn of its own, whose empty result
    ended the owner's turn, and the room killed the process mid-answer and went fresh with no recap."""
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "move the split pages to the trash")
    wait_idle(room)
    sid = room.rp.session_id
    room.rp.close()                                            # the machine went down; the next message resumes
    monkeypatch.setenv("FAKE_CLAUDE_ORPHANS", "1")
    send(room, ledger, "2", "yea try to get the answer key")
    wait_idle(room)
    assert [(r["outcome"], r["session_id"], r["accepted"]) for r in rows_of(ledger)] == [("ok", sid, 1), ("ok", sid, 1)]
    assert chan.replies[-1] == ("echo: yea try to get the answer key", ["2"]) and len(argvs(tmp_path)) == 2
    room.rp.close()


def test_a_message_folded_into_a_turn_that_dies_gets_its_own_turn(env):
    """Seen live (ledger turn 372): the owner's greeting folded into a 36-step job's turn, was never replayed,
    and was marked failed with it when the turn ended timeout, fatal."""
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    room.turn_timeout = 1.0                                    # its silence window, short
    assert send(room, ledger, "1", "STALL work through every markup") == "started"
    time.sleep(0.4)
    assert send(room, ledger, "2", "hello") == "folded"
    wait_idle(room)
    assert [ids for _, ids in chan.notices] == [["1"]] and chan.replies == [("echo: hello", ["2"])]   # its own turn: no line ahead
    folded = room.seen["hello"].sent                         # as it folded (the message its own turn ran is a copy, written bare)
    assert folded.startswith("[sent ") and folded.endswith("\n\nhello") and room.seen[folded] is room.seen["hello"]
    assert (ledger.inbound_get("1")["state"], ledger.inbound_get("2")["state"]) == ("failed", "answered")
    rows = rows_of(ledger)
    assert [(r["outcome"], r["failure"]) for r in rows] == [("timeout", "fatal"), ("ok", None)]
    assert rows[0]["session_id"] == rows[1]["session_id"] and f"{words.WORKING} {words.AFTER_FAILED}" in chan.cards
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
    assert [(r["outcome"], r["failure"]) for r in rows] == [("error", "overflow"), ("compacted", None), ("ok", None)]
    assert len({r["session_id"] for r in rows}) == 1 and f"{words.WORKING} {words.COMPACTING}" in chan.cards
    assert chan.replies == [("echo: TOO_LONG_ONCE hello", ["1"])]
    room.rp.close()


def test_claude_codes_own_compaction_in_a_turn_is_on_its_card_until_it_ends(env):
    """2026-11-05: Claude Code compacted 2 min inside the owner's turn and the card said only that it was working on it. Its
    status event now puts words.COMPACTING on the card until the compaction's end, at the turn's start (the fake's
    AUTO_COMPACT) and in its middle (a fixture: words, a tool row, the compaction, a silent thinking block, the next words);
    then the card says what comes next: the start line, thinking while the model thinks, its words."""
    make, ledger, _ = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "AUTO_COMPACT hello")
    wait_idle(room)
    shortening = f"{words.WORKING} {words.COMPACTING}"
    assert chan.cards == [f"{words.WORKING} {words.START}", shortening, f"{words.WORKING} {words.START}",
                          f"{words.WORKING} echo: AUTO_COMPACT hello"]
    assert chan.replies == [("echo: AUTO_COMPACT hello", ["1"])] and ledger.rows(1)[0]["context"] == 20000
    chan.cards.clear()
    send(room, ledger, "2", "FIXTURE:compact_mid_turn")
    wait_idle(room)
    row = f"{words.DONE} reading the pinned notes"
    at = chan.cards.index(f"{shortening}\n{row}")
    assert chan.cards[at - 1:] == [f"{words.WORKING} Checking the notes first\n{row}", f"{shortening}\n{row}",
                                   *(f"{words.WORKING} {x}\n{row}" for x in (words.START, words.THINKS, words.START)),
                                   f"{words.WORKING} The pinned notes say yes.\n{row}"]
    assert chan.replies[-1] == ("The pinned notes say yes.", ["2"]) and room.context == 16003
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


def test_a_refusal_fallback_starts_the_next_turn_fresh_only_when_failures_toml_says_fresh_next_turn(env):
    """failures.toml decides what the room does after a refusal, as it does for every other class (cleanup F1-03): with
    another action the answer stands and is marked, and the next turn goes on in the same session, no recap."""
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    room.failures.actions["refusal"] = "stop"
    send(room, ledger, "1", "FIXTURE:refusal_fallback hi")
    wait_idle(room)
    mark, first = chan.cards[-1], rows_of(ledger)[-1]
    assert chan.replies[0] == ("answered on the fallback model", ["1"])
    assert (first["outcome"], first["failure"], first["action"]) == ("ok", "refusal", "stop")
    send(room, ledger, "2", "again")
    wait_idle(room)
    second = rows_of(ledger)[-1]
    assert second["session_id"] == first["session_id"] and second["recap"] is None and len(argvs(tmp_path)) == 1
    assert chan.replies[1] == ("echo: again", ["2"])
    assert mark == f"{words.WORKING} answered by a backup model"                   # the class's mark, no fresh start named
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


def test_a_process_gone_before_its_first_message_is_an_exit_never_a_crashed_turn(env, monkeypatch):
    """The race the test above lost once under load (the fix-22 run): `--resume` of a session that is
    gone exits at once, and when that exit beat the write, the write's BrokenPipeError crashed the turn
    and left message 2 running with no reply. Here every resume waits for its process to end first."""
    from yerk_ai.engine import RoomProcess
    real = RoomProcess.start

    def start(self, **k):
        sid = real(self, **k)
        if self.resume_arg:
            self.proc.wait(10)
        return sid
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    room.rp.close()
    monkeypatch.setattr(RoomProcess, "start", start)
    monkeypatch.setenv("FAKE_CLAUDE_MISSING", "1")
    send(room, ledger, "2", "again")
    wait_idle(room)
    assert [(r["outcome"], r["failure"], r["action"]) for r in rows_of(ledger)[1:]] == [
        ("exited", "fatal", "fresh_with_recap"), ("ok", None, None)]
    assert chan.replies[-1][1] == ["2"] and ledger.inbound_get("2")["state"] == "answered"
    room.rp.close()


def test_meters_come_from_the_stream_per_account(env, monkeypatch):
    from yerk_ai.meters import Meters, meter_line
    make, ledger, tmp_path = env
    monkeypatch.setenv("FAKE_CLAUDE_WINDOWS", "0.07,0.03")
    meters = Meters(tmp_path / "meters.json")
    chan = Chan()
    room = make(chan, meters=meters)
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


def test_a_quoted_false_turns_the_thinking_panel_off(env):
    """A yes-or-no setting is read strictly (cold read of PR 10): true, false, or those two words."""
    from yerk_ai.config import flag
    make, ledger, tmp_path = env
    room = make(Chan())
    shown = {}
    for value in (True, "true", " TRUE ", False, "false", "False", "no", "yes", 1, None):
        room.conf = {"thinking_panel": value}
        room._new_card()
        shown[repr(value)] = room.card.thinking_on
    assert [k for k, on in shown.items() if on] == ["True", "'true'", "' TRUE '"]
    assert flag("off", default=True) and not flag("false", default=True)


def test_a_process_whose_hooks_never_call_back_says_so_once(env):
    """Claude Code treats a failed hook as non-blocking, so a room whose hooks never reach the daemon
    (2026-10-19: a sandboxed room's proxy refused every http hook with 403) ran with no clock, no recall
    and no gates, silently. An answered turn with no hook call at the daemon is said once per process."""
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "m1", "HOOK_ERROR hello")            # the fake says what Claude Code says: a Stop hook error
    wait_idle(room)
    send(room, ledger, "m2", "HOOK_ERROR again")
    wait_idle(room)
    assert [t for t, _ in chan.notices] == [f"{words.NOT_DONE} {words.HOOKS_DEAD}"] and len(chan.replies) == 2
    rows = ledger._select("SELECT room, gate, decision, rule FROM gates", ())
    assert [(r["room"], r["gate"], r["decision"], r["rule"]) for r in rows] == [("r", "hooks", "dead", "no-hook-call")]


def test_a_dead_hook_notice_comes_back_on_a_new_process(env):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "m1", "HOOK_ERROR hello")
    wait_idle(room)
    assert room.reap_idle(force=True) and room.rp is None       # the idle close; the next message resumes
    send(room, ledger, "m2", "HOOK_ERROR again")
    wait_idle(room)
    assert [t for t, _ in chan.notices] == [f"{words.NOT_DONE} {words.HOOKS_DEAD}"] * 2 and len(chan.replies) == 2


def test_a_hook_error_with_a_hook_call_at_the_daemon_is_quiet(env):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    start = room._start

    def started(account, fresh=False):
        start(account, fresh=fresh)
        room.hooked = True                      # what daemon.hook does on the process's first bound call

    room._start = started
    send(room, ledger, "m1", "HOOK_ERROR hello")
    wait_idle(room)
    assert chan.notices == [] and len(chan.replies) == 1
    assert ledger._select("SELECT count(*) AS n FROM gates", ())[0]["n"] == 0


def test_a_rate_limit_rests_the_account_for_that_model_until_the_reset_the_stream_names(env, monkeypatch):
    """The walk this ends (seen live: a room put on a model three of four accounts were out of for the week):
    every message hopped one, four, two before landing on three, each rest a minute's backoff. The rejected event names
    when the limit resets, so the rest ends there, for that model alone, and the next message goes straight to the
    account that answered."""
    make, ledger, tmp_path = env
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", str(tmp_path / "cfg1"))
    monkeypatch.setenv("FAKE_CLAUDE_RESETS", "2000300000")
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    cool = Cooldowns(tmp_path / "cooldowns.json")
    assert cool.until("one", model="m") == 2000300000 and cool.until("one", model="other") is None   # keyed to the model it hit
    assert cool.until("one") == 2000300000 and cool.show("one")[0]["kind"] == "rate_limit:m"
    assert [r["hop"] for r in rows_of(ledger)] == [None, "quota:one"] and chan.replies == [("echo: hello", ["1"])]
    room.rp.close()                                            # the next message picks its account again
    send(room, ledger, "2", "again")
    wait_idle(room)
    rows = rows_of(ledger)
    assert [(r["account"], r["outcome"], r["hop"], r["model"]) for r in rows[2:]] == [("two", "ok", None, "m")]   # no walk
    assert [a["config_dir"] for a in argvs(tmp_path)].count(str(tmp_path / "cfg1")) == 1
    assert chan.cards.count(f"{words.WORKING} still on it, moved to account two") == 1
    room.rp.close()


def test_a_rate_limit_naming_no_reset_rests_until_the_weekly_reset_on_file_an_hour_at_the_least(env, monkeypatch):
    from yerk_ai.meters import Meters, rest_until
    make, ledger, tmp_path = env
    meters, now = Meters(tmp_path / "meters.json"), time.time()
    meters.seen("one", {"seven_day": {"utilization": 0.9, "resetsAt": now + 7200}})
    assert rest_until(None, meters, "one", "rate_limit", now=now) == now + 7200       # the account's weekly reset
    assert rest_until(now + 5, meters, "one", "rate_limit", now=now) == now + 5         # the reset the stream named wins
    assert rest_until(None, meters, "two", "rate_limit", now=now) is None             # no reading: the backoff stands
    assert rest_until(None, meters, "one", "auth", now=now) is None                   # a login has no reset
    meters.seen("one", {"seven_day": {"utilization": 0.9, "resetsAt": now + 60}})
    assert rest_until(None, meters, "one", "rate_limit", now=now) == now + 3600       # the floor
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", str(tmp_path / "cfg1"))                 # an error with no event: no reset named
    chan = Chan()
    room = make(chan, meters=meters)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    until = Cooldowns(tmp_path / "cooldowns.json").until("one", model="m")
    assert until is not None and now + 3590 < until < now + 3660 and chan.replies == [("echo: hello", ["1"])]
    room.rp.close()


def test_every_account_out_of_the_pinned_model_runs_the_turn_on_the_rooms_own_model_and_says_so(env, monkeypatch):
    """The rule the owner asked for: an account with usage left for the pinned model is kept until it runs out, then the
    next one in order, and with none left the turn runs on the room's own model."""
    make, ledger, tmp_path = env
    (tmp_path / "rooms" / "r").mkdir(parents=True)
    (tmp_path / "rooms" / "r" / "override.json").write_text(json.dumps({"model": "m"}))      # put on m; rooms.toml says usual
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", f"{tmp_path / 'cfg1'},{tmp_path / 'cfg2'}")
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_MODEL", "m")                                        # both out of m; usual answers
    reset = int(time.time()) + 6
    monkeypatch.setenv("FAKE_CLAUDE_RESETS", str(reset))
    chan = Chan()
    room = make(chan, conf={"model": "usual"})
    send(room, ledger, "1", "hello")
    wait_idle(room)
    rows = rows_of(ledger)
    assert [(r["account"], r["outcome"], r["model"], r["out_of"], r["hop"]) for r in rows] == [
        ("one", "error", "m", None, None), ("two", "error", "m", None, "quota:one"), ("one", "ok", "usual", "m", "quota:one,quota:two")]
    assert chan.replies == [("echo: hello", ["1"])] and not chan.notices
    assert f"{words.WORKING} the sample, item moved {words.clock(reset)}" in chan.cards
    cool = Cooldowns(tmp_path / "cooldowns.json")
    assert cool.until("one", model="m") == reset and cool.until("one", model="usual") is None   # the reply on usual left m's rest
    send(room, ledger, "2", "still out")                                                    # inside the rest: usual at once, no walk
    wait_idle(room)
    row = rows_of(ledger)[-1]
    assert (row["account"], row["model"], row["out_of"], row["hop"]) == ("one", "usual", "m", None) and len(argvs(tmp_path)) == 3
    time.sleep(max(0.0, reset + 0.5 - time.time()))
    monkeypatch.delenv("FAKE_CLAUDE_FAIL_DIRS")                                              # the limit lifted with its reset
    send(room, ledger, "3", "back?")
    wait_idle(room)
    last, row = argvs(tmp_path)[-1], rows_of(ledger)[-1]
    assert last["argv"][last["argv"].index("--model") + 1] == "m" and "--resume" in last["argv"]   # the pinned model, same session
    assert (row["account"], row["model"], row["out_of"], row["hop"]) == ("one", "m", None, None) and chan.replies[-1] == ("echo: back?", ["3"])
    room.rp.close()


def test_a_room_that_is_not_the_owners_falls_back_naming_no_model_or_account(env, monkeypatch):
    make, ledger, tmp_path = env
    (tmp_path / "rooms" / "r").mkdir(parents=True)
    (tmp_path / "rooms" / "r" / "override.json").write_text(json.dumps({"model": "m"}))
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", f"{tmp_path / 'cfg1'},{tmp_path / 'cfg2'}")
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_MODEL", "m")
    monkeypatch.setenv("FAKE_CLAUDE_RESETS", "2000300000")
    chan = Chan()
    room = make(chan, conf={"model": "usual", "privacy": "family"})
    send(room, ledger, "1", "hello")
    wait_idle(room)
    row = rows_of(ledger)[-1]
    assert chan.replies == [("echo: hello", ["1"])] and (row["model"], row["out_of"], row["hop"]) == ("usual", "m", "quota:one,quota:two")
    assert f"{words.WORKING} {words.MOVED_ON}" in chan.cards
    assert not any("usual" in c or "back" in c or "account" in c or "two" in c.split() for c in chan.cards)
    room.rp.close()


def test_a_failed_turn_is_read_by_its_own_process_stderr_never_an_earlier_one(env):
    make, ledger, tmp_path = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    room.reap_idle(force=True)
    (tmp_path / "rooms" / "r" / "stderr.log").write_bytes(b"No conversation found with session ID: 0ld\n")   # an earlier process's
    room.turn_timeout = 1.0                                    # its silence window, short
    send(room, ledger, "2", "STALL now")
    wait_idle(room)
    row = dict(ledger._select("SELECT outcome, action, source, session_id FROM turns ORDER BY id", ())[1])
    assert (row["outcome"], row["action"], row["source"]) == ("timeout", "stop", "none")      # a silence, never a lost session
    assert row["session_id"] == room.ledger._select("SELECT session_id FROM turns ORDER BY id", ())[0]["session_id"]
    assert ledger.inbound_get("2")["state"] == "failed" and len(chan.notices) == 1


def test_a_message_that_comes_in_while_the_idle_close_waits_for_the_old_process_is_answered(env, monkeypatch):
    make, ledger, tmp_path = env
    slow = tmp_path / "slow-exit.sh"                 # an old process that takes 2 s to exit after its stdin closes
    slow.write_text(f'#!/bin/sh\n"{FAKE}" "$@"\nsleep 2\n')
    slow.chmod(0o700)
    monkeypatch.setenv("YERK_CLAUDE", str(slow))
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    closer = threading.Thread(target=room.reap_idle, kwargs={"force": True})
    closer.start()
    time.sleep(0.5)                                  # the close has the process and waits for it, its session still locked
    assert room.rp is None and closer.is_alive()
    send(room, ledger, "2", "hello again")
    closer.join(10)
    wait_idle(room)
    assert chan.notices == [] and chan.replies[-1] == ("echo: hello again", ["2"])
    assert ledger.inbound_get("2")["state"] == "answered"
    room.rp.close()
