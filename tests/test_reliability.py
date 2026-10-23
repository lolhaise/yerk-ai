"""Checks what the crash drills guarantee when yerkd, the program that runs the rooms, is stopped or fails mid-work. A daemon
stop mid-turn leaves the turn's messages for the next start and nothing else, a turn that crashes is said in the room, a stream
log that cannot be written never stalls a turn, a message the ledger (the database of what ran) could not take is read again once
it can, a ready after a lost gateway session catches the channel up, a ledger newer than the code is refused untouched, a second
names the version and the commit, as text and as one JSON document."""
import asyncio
import json
import os
import signal
import sqlite3
import time

import pytest

import yerk_ai.channels.discord as dmod
from test_discord import HOOK, OWNER, ROOM_CH, Client, Msg, Room, run
from test_room import Chan, env, send, wait_idle  # noqa: F401  (env: the room fixture)
from yerk_ai import __version__, cli, words
from yerk_ai.accounts import Account
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd, claim, main as dmain
from yerk_ai.engine import RoomProcess, RoomSpec
from yerk_ai.ledger import MIGRATIONS, Ledger
from yerk_ai.room import Inbound


def test_a_stop_mid_turn_leaves_the_messages_open_and_the_next_start_runs_them_once_marked(env):
    make, ledger, tmp = env
    chan = Chan()
    room = make(chan)
    send(room, ledger, "1", "SLOW please")
    for _ in range(200):
        if room.reading and room.rp is not None and room.rp.alive():
            break
        time.sleep(0.02)
    t0 = time.time()
    assert room.shutdown() is True                    # a turn was cut
    wait_idle(room)
    assert time.time() - t0 < 10
    assert ledger.inbound_get("1")["state"] == "running" and chan.notices == [] and chan.replies == []
    assert room.rp is None or not room.rp.alive()
    again = make(chan)                                # the next start: the adapter's recovery pass runs it once, marked
    again.submit(Inbound("1", "owner", "SLOW please\n[redelivered after a restart]", attempt=1))
    wait_idle(again)
    assert ledger.inbound_get("1")["state"] == "answered" and chan.replies[-1][1] == ["1"]


def test_sigterm_ends_the_daemon_clean_telling_systemd_and_a_task_that_raises_still_ends_it(tmp_path, monkeypatch):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))
    told, closed = [], []
    monkeypatch.setattr("yerk_ai.daemon.notify", told.append)

    async def close():
        closed.append(True)

    async def forever():
        await asyncio.Event().wait()

    async def stopped():
        tasks = [asyncio.create_task(forever())]
        asyncio.get_running_loop().call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)
        await y.until_stopped(tasks, close)
        return tasks
    tasks = asyncio.run(stopped())
    assert told == ["STOPPING=1"] and closed == [True] and all(t.cancelled() for t in tasks)

    async def boom():
        raise OSError("the gateway is unreachable")

    async def raised():
        await y.until_stopped([asyncio.create_task(boom())], close)
    with pytest.raises(OSError):
        asyncio.run(raised())
    assert closed == [True]                            # no stop ran for an error


def test_a_turn_that_crashes_is_said_in_the_room_and_its_rows_are_closed(env, monkeypatch):
    make, ledger, tmp = env
    chan = Chan()
    room = make(chan)

    def no_space(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(room, "_start", no_space)
    send(room, ledger, "1", "hello")
    wait_idle(room)
    row = ledger.inbound_get("1")
    assert row["state"] == "failed" and row["note"] == "crashed"
    assert chan.notices and words.WHY["crashed"] in chan.notices[-1][0] and chan.notices[-1][1] == ["1"]


def test_a_stream_log_that_cannot_be_written_never_stalls_the_turn(env, tmp_path):
    make, ledger, tmp = env
    chan = Chan()
    room = make(chan)
    room.log_dir = tmp_path / "logs"
    (room.log_dir / "r.jsonl").mkdir(parents=True)   # a folder where the file should be: open() fails, the turn runs
    send(room, ledger, "1", "hello")
    wait_idle(room, 15)
    assert chan.replies and ledger.inbound_get("1")["state"] == "answered"

    class Broken:                                     # a write that fails mid-stream (the disk full): the log ends, not the turn
        def write(self, s):
            raise OSError(28, "No space left on device")

        def flush(self):
            pass

        def close(self):
            pass
    rp = RoomProcess(RoomSpec(room="q", model="m", cwd=tmp_path, tools=["Read"]), Account("one", tmp_path / "cfg1"),
                     tmp_path, log=tmp_path / "stream.jsonl")
    rp.start()
    rp._log = Broken()
    try:
        t = rp.ask("hello", timeout=15)
    finally:
        rp.close()
    assert t.outcome == "ok" and rp._log is None


def adapter(tmp_path):
    client = Client()
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path)
    room = Room(tmp_path)
    d.add_room(ROOM_CH, room)
    d.ledger.inbound_add("1", "test-room", str(ROOM_CH), "owner", 5)   # the last message the channel ledgered
    d.ledger.inbound_set("1", state="answered")
    return d, client.chans[ROOM_CH], room


def test_a_message_the_ledger_cannot_take_is_read_again_once_it_can(tmp_path, monkeypatch):
    monkeypatch.setattr(dmod, "RETRY", 0.2)
    d, ch, room = adapter(tmp_path)
    real, full = d.ledger.inbound_add, [True]

    def add(*a, **k):
        if full[0]:
            raise sqlite3.OperationalError("database or disk is full")
        return real(*a, **k)
    monkeypatch.setattr(d.ledger, "inbound_add", add)
    m = Msg(ch, 2, "while the disk was full")
    ch.past = [m]

    async def go():
        await d.on_message(m)                         # not taken, not lost: the channel is read again
        assert room.got == [] and ch.reactions == [] and ROOM_CH in d.retry
        await asyncio.sleep(0.3)                      # still full: read again later
        assert room.got == [] and ROOM_CH in d.retry
        full[0] = False
        await asyncio.sleep(0.4)
    run(go())
    assert [(x.id, x.text) for x in room.got] == [("2", "while the disk was full")] and d.retry == set()


def test_a_ready_after_a_lost_gateway_session_catches_the_channel_up(tmp_path):
    d, ch, room = adapter(tmp_path)

    async def go():
        await d.on_ready()                            # the first ready of the process: the recovery pass
        ch.past = [Msg(ch, 2, "sent while the gateway was down")]
        await d.on_ready()                            # after the re-identify: no recovery, the catch-up
        await d.on_ready()                            # read once however many readies
    run(go())
    assert [(m.id, m.text) for m in room.got] == [("2", "sent while the gateway was down")]


def test_a_ledger_newer_than_the_code_is_refused_with_a_line_and_left_as_it_is(tmp_path):
    p = tmp_path / "l.sqlite"
    Ledger(p)
    newer = MIGRATIONS[-1][0] + 1
    db = sqlite3.connect(p)
    db.execute(f"PRAGMA user_version = {newer}")
    db.commit()
    db.close()
    with pytest.raises(SystemExit) as e:
        Ledger(p)
    assert "newer than this code" in str(e.value) and "nothing was changed" in str(e.value)
    assert sqlite3.connect(p).execute("PRAGMA user_version").fetchone()[0] == newer


def test_a_second_daemon_on_one_state_folder_is_refused_before_it_writes_anything(tmp_path):
    held = claim(tmp_path)
    with pytest.raises(SystemExit) as e:
        dmain(["--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path), "--config-dir", str(tmp_path / "cfg1")])
    assert "another yerkd runs" in str(e.value) and not (tmp_path / "ledger.sqlite").exists()   # refused before the ledger
    os.close(held)                                     # the lock goes with its holder, as at a kill -9
    os.close(claim(tmp_path))


def test_status_names_the_version_and_the_commit_as_text_and_as_one_json_document(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", os.path.join(os.path.dirname(__file__), "fake_claude.py"))   # the card's /usage, never the real one
    assert cli.main(["status", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    assert f"{words.PLAIN} yerk {__version__} at " in capsys.readouterr().out
    assert cli.main(["status", "--json", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == __version__ and doc["running"] is False
    assert set(doc) >= {"rooms", "accounts", "last_turn", "failed_turns", "dead_messages", "index", "backup_age", "commit"}
