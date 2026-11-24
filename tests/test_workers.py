"""Checks a worker's life on a fake launcher in place of systemd: start, its progress card from
TASKS.md at most once a second, completion and the relay of its report or the skip, death, timeout
against the ledger, and the environment a worker gets."""
import asyncio
import json
import re
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import yerk_ai.channels.discord as dmod
from yerk_ai import cli, gates, mcp_server, workers
from yerk_ai import words as W
from yerk_ai.accounts import Account, Cooldowns, store_token
from yerk_ai.config import table
from yerk_ai.engine import RoomSpec, SessionTable
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.room import Inbound, Room, fit

FAKE = Path(__file__).with_name("fake_worker_claude.py")
ROOT = Path(__file__).resolve().parents[1]
TOKEN_ONE, TOKEN_TWO = "sk-" + "ant-oat01-" + "One" * 30, "sk-" + "ant-oat01-" + "Two" * 30
MANAGER_ENV = {"HOME": "/nonexistent-home", "USER": "someone", "LANG": "C.UTF-8",   # what a user manager adds,
               "SSH_AUTH_SOCK": "/tmp/planted-agent.sock", "GPG_AGENT_INFO": "planted"}  # two the worker must not get


class Units(workers.Systemd):
    """systemd-run's part, as the code relies on it: argv runs with exactly the given environment on
    top of the manager's, in its folder, output appended to the files; the cap kills it (result
    timeout); a stop kills its process group (result success, killed TERM, as systemd reports a
    stopped service); ExecStopPost runs `post` with SERVICE_RESULT, EXIT_CODE and EXIT_STATUS. The
    rest is the launcher's own: given a state folder and hidden paths, its fence's question (unseen)."""

    def __init__(self, state=None, hidden=()):
        super().__init__(state, hidden)
        self.calls, self.procs, self.stopped = [], {}, set()

    def keep(self, *a, **kw):
        """This stand-in, holding what the daemon gives its launcher (the state folder, the hidden paths); a bare
        Systemd() (`yerk status`'s) keeps what it holds."""
        if a or kw:
            super().__init__(*a, **kw)
        return self

    def start(self, unit, argv, env, cwd, out, err, cap, post):
        self.calls.append({"unit": unit, "argv": list(argv), "env": dict(env), "cwd": cwd, "cap": cap, "post": post})
        full = {**MANAGER_ENV, **env}
        with open(out, "ab") as o, open(err, "ab") as e:
            p = subprocess.Popen(argv, env=full, cwd=cwd, stdout=o, stderr=e, start_new_session=True)
        self.procs[unit] = p
        threading.Thread(target=self._wait, args=(unit, p, cap, post, full, cwd), daemon=True).start()

    def _wait(self, unit, p, cap, post, env, cwd):
        try:
            rc = p.wait(timeout=cap)
            result = "success" if rc == 0 or unit in self.stopped else "exit-code"
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)
            rc, result = p.wait(), "timeout"
        code, status = ("exited", str(rc)) if rc >= 0 else ("killed", signal.Signals(-rc).name.removeprefix("SIG"))
        subprocess.run(post, env={**env, "SERVICE_RESULT": result, "EXIT_CODE": code, "EXIT_STATUS": status}, cwd=cwd,
                       check=True)
        self.procs.pop(unit, None)

    def stop(self, unit):
        self.stopped.add(unit)
        if unit in self.procs:
            os.killpg(self.procs[unit].pid, signal.SIGTERM)

    def running(self):
        return set(self.procs)


class Chan:
    """A room's channel (the room thread's calls) and the worker cards (the loop's), recorded."""

    def __init__(self):
        self.replies, self.notices, self.cards, self.settled = [], [], [], []

    def open_card(self, room, text, kind="turn"):
        pass

    def progress(self, room, text):
        pass

    def reply(self, room, text, inbound, card=None, key=None):
        self.replies.append((text, list(inbound)))
        return [f"m{len(self.replies)}"]

    def notice(self, room, text, inbound, card=None, key=None):
        self.notices.append((text, list(inbound)))
        return ["n1"]

    async def worker_card(self, room, key, text, message_id=None):
        self.cards.append((key, text, message_id))
        return message_id or f"card-{key}"

    async def worker_settle(self, room, key, text, how, message_id=None):
        self.settled.append((key, text, how, message_id))
        return message_id or f"card-{key}"


class Busy:
    """A room that is never idle: the relay waits (the card carries the result)."""

    def idle_reason(self, seconds, now=None):
        return "a turn was running"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "mark"))
    ledger = Ledger(tmp_path / "ledger.sqlite")
    accounts = [Account("one", tmp_path / "cfg1", 1, tmp_path / "secrets" / "claude-one.token"),
                Account("two", tmp_path / "cfg2", 2, tmp_path / "secrets" / "claude-two.token")]
    store_token(accounts[0], TOKEN_ONE)
    store_token(accounts[1], TOKEN_TWO)
    data = tomllib.loads(table("failures.toml").read_text())
    chan, units = Chan(), Units()

    def room(name="r"):
        spec = lambda: RoomSpec(room=name, model="m", cwd=tmp_path, tools=["Read"])  # noqa: E731
        return Room(name, {}, spec, accounts, chan, tmp_path, ledger, FailureTable(data), Cooldowns(None))

    def make(rooms, channel=None, launcher=None, per_account=10, yerk_tools=()):
        conf = workers.Settings(root=tmp_path / "workers", idle_seconds=60.0, per_account=per_account,
                                yerk_tools=list(yerk_tools))
        return workers.Workers(ledger, conf, accounts, Cooldowns(None), rooms,
                               {n: channel or chan for n in rooms}, {n: {"model": "m"} for n in rooms},
                               launcher=launcher or units)
    return NS(ledger=ledger, accounts=accounts, chan=chan, units=units, room=room, make=make, tmp=tmp_path)


async def ended(ledger, wid, relay=True, timeout=20.0):
    """The worker's row once it has finished (and its relay is settled, unless relay=False)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = ledger.worker_get(wid)
        if r and r["finished"] and (not relay or r["relay"] in ("relayed", "skipped", "failed", "waiting", relay)):
            return r
        await asyncio.sleep(0.05)
    raise AssertionError(f"worker {wid} did not end: {ledger.worker_get(wid)}")


def test_start_writes_the_unit_and_the_ledger_row_and_posts_the_card_then_completion_relays_when_idle(setup):
    s = setup
    room = s.room()

    async def go():
        w = s.make({"r": room})
        w.begin(asyncio.get_running_loop())
        out = await w.handle("r", {"action": "start", "brief": 'FAKE ticks=3 gap=0.1 report="All three done" pad=5000',
                                   "tasks": ["plan", "build", "write up"]})
        assert out["ok"], out
        wid = out["id"]
        call = s.units.calls[0]
        assert call["unit"] == f"yerk-worker-{wid}" and call["cap"] == 65 * 60
        assert call["argv"][-2:] == ["run", out["staging"] + "/worker.json"] and call["post"][-2:] == ["post", out["staging"]]
        row = s.ledger.worker_get(wid)
        assert (row["room"], row["account"], row["unit"], row["finished"]) == ("r", "one", call["unit"], None)
        assert row["report"] == out["report"] and row["brief"].endswith("BRIEF.md") and row["started"] > 0
        key, text, mid = s.chan.cards[0]
        assert key == f"worker:{wid}" and mid is None and text.splitlines()[0] == f"{W.WORKING} worker running, 0 of 3 done"
        assert text.splitlines()[1:4] == [f"{W.WORKING} plan", f"{W.WAITING} build", f"{W.WAITING} write up"]
        assert text.splitlines()[-2:] == [f"-# worker {wid}, under 1 min of 65", "-# account one"]   # 2 accounts, phone width
        assert "<t:" not in text and all(W.width(x) <= W.PHONE for x in text.splitlines())
        assert workers.status_lines(s.ledger, s.units.running())[0] == f"{W.WORKING} worker {wid}: under 1 min of 65"
        assert workers.status_lines(s.ledger, s.units.running(), full=True)[0].startswith(f"worker {wid}: room r, account one")
        return wid, await ended(s.ledger, wid)
    wid, row = asyncio.run(go())
    assert (row["exit"], row["exit_detail"], row["result"]) == ("done", "success, exited 0", "All three done")
    key, text, how, mid = s.chan.settled[0]
    assert mid == f"card-worker:{wid}" and how == "settled: done"
    assert text.splitlines()[:2] == [f"{W.DONE} worker finished clean", "All three done"]
    assert [x for x in text.splitlines() if x.startswith(W.DONE + " ")][1:] == [f"{W.DONE} plan", f"{W.DONE} build",
                                                                               f"{W.DONE} write up"]
    assert text.splitlines()[-1] == f"-# report saved: `yerk worker report {wid}`" and text.splitlines()[-2].startswith(
        "-# done in ")
    assert row["report"] not in text and "exited" not in text and "unit" not in text     # the detail is the command's
    # one relay turn in the room's own session, under the cap, ending with the report line, never the whole report
    assert row["relay"] == "relayed" and 0 < row["relay_chars"] <= 1500
    (reply, inbound), = s.chan.replies
    assert inbound == [f"relay-{wid}"] and len(reply) <= 1500 and f"`yerk worker report {wid}`" in reply
    assert row["report"] not in reply and "exited" not in reply
    assert "LAST LINE OF THE REPORT" not in reply
    r = s.ledger.inbound_get(f"relay-{wid}")
    assert (r["state"], r["sender"], r["channel"]) == ("answered", "yerk", "worker")
    assert room.last_human is None                      # the relay is not a human message


@pytest.mark.parametrize("why", ["human", "turn"])
def test_completion_when_the_room_is_not_idle_waits_for_idle_then_relays_once(setup, why):
    """A completion while a human turn runs (or right after a human message): the card carries the
    result at once, the relay waits, then runs once when the room is idle."""
    s = setup
    room = s.room()
    s.ledger.inbound_add("h1", "r", "c", "owner", 2)
    room.submit(Inbound("h1", "owner", "hi"))
    while room.busy:
        time.sleep(0.05)
    if why == "turn":
        room.busy = True                                 # a human turn is running

    async def go():
        w = s.make({"r": room})
        w.begin(asyncio.get_running_loop())
        out = await w.start("r", 'FAKE report="Short one"')
        row = await ended(s.ledger, out["id"], relay="waiting")
        assert row["relay"] == "waiting" and "waiting: " in row["relay_note"]
        assert "Short one" in s.chan.settled[0][1] and f"yerk worker report {row['id']}" in s.chan.settled[0][1]
        assert row["report"] not in s.chan.settled[0][1] and len(s.chan.replies) == 1     # only the human's answer
        if why == "turn":
            room.busy = False
        room.last_human = time.time() - 61               # the minute has passed
        w.pump("r")
        w.pump("r")                                      # asked twice: still one relay
        return await ended(s.ledger, out["id"])
    row = asyncio.run(go())
    assert row["exit"] == "done" and row["relay"] == "relayed"
    assert len(s.chan.replies) == 2 and s.chan.replies[1][1] == [f"relay-{row['id']}"]


def test_a_death_a_timeout_and_a_stop_each_settle_as_such(setup):
    s = setup

    async def go():
        w = s.make({"r": Busy()})
        w.begin(asyncio.get_running_loop())
        died = await w.start("r", 'FAKE report="half way" exit=3')
        slow = await w.start("r", "FAKE sleep=30", cap_minutes=1.5 / 60)
        held = await w.start("r", "FAKE sleep=30")
        await asyncio.sleep(0.3)
        assert (await w.stop(held["id"]))["ok"]
        return [await ended(s.ledger, x["id"], relay="waiting") for x in (died, slow, held)]
    died, slow, held = asyncio.run(go())
    assert (died["exit"], died["exit_detail"], died["result"]) == ("died", "exit-code, exited 3", "half way")
    assert (slow["exit"], slow["exit_detail"]) == ("timed out", "timeout, killed TERM")
    assert held["exit"] == "stopped" and held["stop_requested"] and held["exit_detail"] == "success, killed TERM"
    heads = {k.removeprefix("worker:"): t.splitlines()[0] for k, t, _, _ in s.chan.settled}
    assert heads[died["id"]] == f"{W.NOT_DONE} worker died: it ended with an error"
    assert heads[slow["id"]].startswith(f"{W.NOT_DONE} worker stopped: timed out at ")
    assert heads[held["id"]] == f"{W.NOT_DONE} worker stopped by the owner"
    assert all(W.width(x) <= W.PHONE for _, t, _, _ in s.chan.settled for x in t.splitlines())
    assert all(r["relay"] == "waiting" for r in (died, slow, held))       # a Busy room: the cards carry them
    assert workers.status_lines(s.ledger, set()) == [f"{W.NOTHING} workers: none running"]   # nothing still running


def test_a_restart_reattaches_from_the_ledger_and_completion_still_relays(setup):
    s = setup
    room = s.room()

    async def go():
        loop = asyncio.get_running_loop()
        a = s.make({"r": room})
        a.begin(loop)
        out = await a.start("r", 'FAKE sleep=1.0 ticks=1 report="Survived the restart"', tasks=["only step"])
        card = s.ledger.worker_get(out["id"])["card"]
        loop.remove_reader(a.watch.fd)                  # the old daemon is gone; its unit is not
        os.close(a.watch.fd)
        gone = s.tmp / "workers" / "0ld0ne"           # a row whose unit vanished with no exit record, ten minutes in
        lost = dict(s.ledger.worker_get(out["id"]), id="0ld0ne", unit="yerk-worker-0ld0ne", room="q", card=None,
                    staging=str(gone), report=str(gone / "REPORT.md"), tasks=str(gone / "TASKS.md"), started=time.time() - 600)
        s.ledger.worker_add(**{k: v for k, v in lost.items() if k != "finished"})
        b = s.make({"r": room})
        b.begin(loop)
        await b.reattach()
        return out["id"], card, await ended(s.ledger, out["id"]), await ended(s.ledger, "0ld0ne")
    wid, card, row, lost = asyncio.run(go())
    reattached = [c for c in s.chan.cards if c[0] == f"worker:{wid}"]
    assert reattached[0][2] is None and reattached[-1][2] == card    # the card taken back by its id, not a new one
    assert [x[3] for x in s.chan.settled] == [card]                 # settled once, on the same card
    assert (row["exit"], row["result"], row["relay"]) == ("done", "Survived the restart", "relayed")
    assert lost["exit"] == "lost" and lost["exit_detail"] == "the unit is gone and left no exit record"
    assert lost["relay"] == "skipped" and lost["relay_note"] == "no such room in this daemon"


def need_hook(w):
    """The need pipeline's `ended`, as Yerkd.need_built hands it a worker's end (workers.hooks): each (id, exit) it heard,
    in order; True, so no relay runs."""
    seen = []

    async def hook(row, end):
        seen.append((row["id"], end["exit"]))
        return True
    w.hooks.append(hook)
    return seen


def stale_first(units):
    """The unit list as systemctl gave it in the live case: the first read taken before a unit just started was listed
    (empty), every read after it as it is. Returns each read, in order."""
    reads = []

    def running():
        reads.append(set(units.procs) if reads else set())
        return reads[-1]
    units.running = running
    return reads


def unlisted(s, wid, age, exit=None):
    """A row the ledger has running, `age` seconds after its start, its unit in no list; with `exit`, the exit file its
    unit's ExecStopPost wrote and the report's first line."""
    d = s.tmp / "workers" / wid
    d.mkdir(parents=True)
    s.ledger.worker_add(id=wid, room="r", account="one", unit=f"yerk-worker-{wid}", model="m", staging=str(d),
                        report=str(d / "REPORT.md"), tasks=str(d / "TASKS.md"), cap=1800.0, started=time.time() - age)
    if exit:
        (d / "REPORT.md").write_text("merge\n")
        (d / "exit.json").write_text(json.dumps(exit))


@pytest.mark.parametrize("look", ["reattach", "reconcile"])
def test_a_unit_the_first_list_missed_is_looked_at_again_and_kept_never_settled_as_lost(setup, caplog, look):
    """Seen live: need 22's cold read was settled as lost a second after its start on one unit list read before
    its unit was listed; the need was held and its account given to the next builder while the read ran on to its
    verdict. A start's reattach and the minute's reconcile look again first: the second look finds the unit, the worker
    is kept, and its need hears nothing until it really ends."""
    caplog.set_level("INFO", logger="yerk.workers")
    s = setup

    async def go():
        loop = asyncio.get_running_loop()
        w = s.make({"r": Busy()})
        w.begin(loop)
        wid = (await w.start("r", 'FAKE sleep=1.0 report="merge"'))["id"]
        s.ledger.worker_set(wid, started=time.time() - 600)          # past YOUNG: the second look alone keeps it
        w.live[wid]["started"] = time.time() - 600
        reads = stale_first(s.units)
        if look == "reattach":                                       # a new daemon: the old one's watch is gone, its unit is not
            loop.remove_reader(w.watch.fd)
            os.close(w.watch.fd)
            w = s.make({"r": Busy()})
            w.begin(loop)
        seen = need_hook(w)
        await getattr(w, look)()
        assert wid in w.live and seen == [] and s.ledger.worker_get(wid)["finished"] is None
        assert len(reads) == 2 and f"yerk-worker-{wid}" in reads[1]
        return wid, seen, await ended(s.ledger, wid, relay=False)
    wid, seen, row = asyncio.run(go())
    assert row["exit"] == "done" and seen == [(wid, "done")]          # it ends once, as it really did
    assert f"worker {wid}: the unit list was stale, re-attached" in caplog.text


def test_a_unit_both_looks_miss_with_no_exit_file_is_lost_as_before_once_past_its_first_two_minutes(setup):
    """A unit gone with no exit record still settles as lost, its need told so, when the second look misses it too; a
    worker under YOUNG s old is kept for a later pass instead, since its unit may not be listed yet."""
    s = setup

    async def go():
        unlisted(s, "0ld0ne", 600)
        unlisted(s, "n3w0ne", 1)
        w = s.make({"r": Busy()})
        w.begin(asyncio.get_running_loop())
        seen = need_hook(w)
        await w.reattach()
        assert seen == [("0ld0ne", "lost")] and list(w.live) == ["n3w0ne"]
        w.live["n3w0ne"]["started"] = time.time() - workers.YOUNG    # two minutes on, still in no list
        await w.reconcile()
        return seen, w.live
    seen, live = asyncio.run(go())
    assert seen == [("0ld0ne", "lost"), ("n3w0ne", "lost")] and live == {}
    row = s.ledger.worker_get("0ld0ne")
    assert (row["exit"], row["exit_detail"]) == ("lost", "the unit is gone and left no exit record")


def test_a_unit_that_ended_while_no_list_had_it_completes_from_its_exit_file(setup):
    """A worker whose unit ended and was collected (in no list, its exit file there) completes as it ended, report line
    and all, its need told it is done, though it is under YOUNG s old: the exit file is enough, no second look."""
    s = setup
    reads = stale_first(s.units)

    async def go():
        unlisted(s, "d0ne01", 30, exit={"result": "success", "code": "exited", "status": "0", "ended": time.time()})
        w = s.make({"r": Busy()})
        w.begin(asyncio.get_running_loop())
        seen = need_hook(w)
        await w.reattach()
        return seen
    seen = asyncio.run(go())
    row = s.ledger.worker_get("d0ne01")
    assert seen == [("d0ne01", "done")] and len(reads) == 1
    assert (row["exit"], row["exit_detail"], row["result"]) == ("done", "success, exited 0", "merge")


def test_a_last_message_of_exactly_no_reply_settles_a_quiet_card_live_and_at_a_re_attach(setup):
    """A worker whose last message is exactly NO_REPLY, read as jobs.py's worker_done reads it (its last result event, else
    the report's first line), settles quiet when the daemon sees its exit file and when a restart finds it ended: the done
    mark, never the token, no rows, the footer as ever; QUIET_WORKER when the report's first line is the token too, else
    "finished clean" over that line (need-2026-11-16-100: a quiet card hid it). Any other last message settles as before."""
    s = setup

    def away(wid, report, last=None):                    # it ended while no daemon was up: its exit file, report and rows
        unlisted(s, wid, 30, exit={"result": "success", "code": "exited", "status": "0", "ended": time.time()})
        d = s.tmp / "workers" / wid
        (d / "REPORT.md").write_text(f"{report}\n")
        (d / "TASKS.md").write_text("- [x] look at the feed\n- [ ] decide\n")
        if last is not None:
            (d / "stdout.jsonl").write_text(json.dumps({"type": "result", "result": last}) + "\n")

    async def go():
        w = s.make({"r": Busy()})
        w.begin(asyncio.get_running_loop())
        out = await w.start("r", 'FAKE ticks=1 report="NO_REPLY" result="NO_REPLY"', tasks=["look at the feed", "decide"])
        died = await w.start("r", 'FAKE report="NO_REPLY" result="NO_REPLY" exit=3')   # only a clean end is quiet
        await ended(s.ledger, out["id"], relay="waiting")
        await ended(s.ledger, died["id"], relay="waiting")
        away("qu1e70", "NO_REPLY")                       # no result event: the report's first line is read
        away("qu1e71", "merge", last=" NO_REPLY\n")      # the last message wins over the report, stripped
        away("n0rma1", "merge", last="merged it")
        await w.reattach()
        return out["id"], died["id"]
    live, died = asyncio.run(go())
    cards = {k.removeprefix("worker:"): t for k, t, _, _ in s.chan.settled}
    assert cards[died].splitlines()[:2] == [f"{W.NOT_DONE} worker died: it ended with an error", W.NO_REPLY]
    for wid, result in ((live, W.NO_REPLY), ("qu1e70", W.NO_REPLY), ("qu1e71", "merge")):
        head, *said, done, saved = cards[wid].splitlines()
        assert [head, *said] == ([f"{W.DONE} worker {W.QUIET_WORKER}"] if result == W.NO_REPLY else
                                 [f"{W.DONE} worker finished clean", "merge"])     # the report's own line, never the token
        assert done.startswith("-# done in ") and saved == f"-# report saved: `yerk worker report {wid}`"
        assert W.NO_REPLY not in cards[wid] and "look at the feed" not in cards[wid] and "job" not in cards[wid]
        assert all(W.width(x) <= W.PHONE for x in cards[wid].splitlines())
        assert (s.ledger.worker_get(wid)["exit"], s.ledger.worker_get(wid)["result"]) == ("done", result)   # the row as ever
    assert cards["n0rma1"].splitlines()[:4] == [f"{W.WORKING} worker finished, 1 of 2 ticked", "merge",
                                                f"{W.DONE} look at the feed", f"{W.WAITING} decide"]
    assert cards["n0rma1"].splitlines()[-1] == "-# report saved: `yerk worker report n0rma1`"


def test_a_settled_card_reads_as_before_for_any_other_end_and_the_quiet_one_keeps_its_footer_and_path():
    """card_text on fixed ends: every end but a quiet one renders as main 64b7953 rendered it, with end["quiet"] false or
    absent; a quiet one keeps the footer as ever: the time, the account when there are two, the path under paths = owner."""
    row = {"id": "abc123", "cap": 900.0, "started": 1000.0, "account": "one", "report": "/w/abc123/REPORT.md",
           "tasks": "/w/abc123/TASKS.md", "room": "r"}
    items, saved = [(True, "look at the feed"), (False, "decide")], "-# report saved: `yerk worker report abc123`"

    def end(state="done", result="Three things happened", **more):
        return {"exit": state, "finished": 1181.0, "result": result, "has_report": True,
                "result_code": "exit-code" if state == "died" else "success", **more}
    before = {"done": [f"{W.WORKING} worker finished, 1 of 2 ticked", "Three things happened", f"{W.DONE} look at the feed",
                       f"{W.WAITING} decide", "-# done in 3 min", saved],
              "died": [f"{W.NOT_DONE} worker died: it ended with an error", "Three things happened",
                       f"{W.DONE} look at the feed", f"{W.NOT_DONE} decide", "-# ran 3 min of 15", saved]}
    for state, lines in before.items():
        for more in ({}, {"quiet": False}):
            assert workers.card_text(row, items, end(state, **more), now=2000.0).splitlines() == lines, (state, more)
    quiet = workers.card_text(row, items, end(result=W.NO_REPLY, quiet=True), accounts=2, look=W.Look(paths="owner"))
    assert quiet.splitlines() == [f"{W.DONE} worker finished clean, nothing to add", "-# done in 3 min, account one", saved,
                                  "-# /w/abc123/REPORT.md"]


def test_a_jobs_worker_card_names_its_job_and_a_quiet_one_shows_its_one_line_result_whole():
    """need-2026-11-16-100: six quiet job cards in a room read alike, "worker finished clean, nothing to add", the time and
    "report saved", though four were one job whose reports each held a forecast's one line and two another job's runs with
    nothing: the card hid the report's line whenever the last message was NO_REPLY and never named the job, so on a phone
    they could not be told apart. A job's worker now names its job first in the footer, waiting, running and settled; a
    quiet end shows the report's first line under "finished clean", whole over two phone lines (cut at one, a forecast
    loses its number), and keeps QUIET_WORKER when that line is the token too or there is no report. Every settled card's
    line wraps alike, a longer one cut with "..."; a long name is cut to phone width; a worker no job started has no job."""
    row = {"id": "abc123", "cap": 900.0, "started": 1000.0, "account": "one", "report": "/w/abc123/REPORT.md",
           "tasks": "/w/abc123/TASKS.md", "room": "r", "job": "price-watch"}
    items, saved = [(True, "look at the feed"), (False, "decide")], "-# report saved: `yerk worker report abc123`"
    said = "Rain at the coast on Saturday: median 12 mm (5-95: 3 to 30)"         # 59 columns: two phone lines
    whole = ["Rain at the coast on Saturday: median 12", "mm (5-95: 3 to 30)"]

    def end(result=said, state="done", **more):
        return {"exit": state, "finished": 1181.0, "result": result, "has_report": True,
                "result_code": "exit-code" if state == "died" else "success", **more}

    def card(r=row, e=None, **kw):
        lines = workers.card_text(r, items, e, now=1185.0, **kw).splitlines()
        assert all(W.width(x) <= W.PHONE for x in lines) and W.NO_REPLY not in lines, lines
        return lines
    assert card(e=end(quiet=True)) == [f"{W.DONE} worker finished clean", *whole, "-# job price-watch, done in 3 min", saved]
    assert card(e=end(quiet=True), accounts=2)[3:5] == ["-# job price-watch, done in 3 min", "-# account one"]
    for nothing in (end(W.NO_REPLY, quiet=True), end(quiet=True, has_report=False)):
        assert card(e=nothing)[:2] == [f"{W.DONE} worker {W.QUIET_WORKER}", "-# job price-watch, done in 3 min"]
    assert card(e=end(state="died")) == [f"{W.NOT_DONE} worker died: it ended with an error", *whole, f"{W.DONE} look at the feed",
                                         f"{W.NOT_DONE} decide", "-# job price-watch, ran 3 min of 15", saved]
    longer = card(e=end(f"{said}, then dry until the next front on Thursday", quiet=True))
    assert longer[1] == whole[0] and longer[2].startswith(whole[1]) and longer[2].endswith("...") and len(longer) == 5
    assert card()[-2:] == ["-# job price-watch, worker abc123", "-# 3 min of 15"]                    # running
    assert card({**row, "waiting": 1, "line": 2})[-1] == "-# job price-watch, worker abc123"         # in line
    named = card({**row, "job": "a-" * 29 + "zz"}, end(quiet=True))
    assert named[3].startswith("-# job a-a-a-") and named[3].endswith("...") and named[4] == "-# done in 3 min"
    plain = {k: v for k, v in row.items() if k != "job"}
    assert card(plain, end(quiet=True)) == [f"{W.DONE} worker finished clean", *whole, "-# done in 3 min", saved]
    assert card(plain)[-1] == "-# worker abc123, 3 min of 15" and card({**plain, "job": None})[-1] == card(plain)[-1]


def settle(s, wid, exit):
    """A worker whose unit wrote `exit`, settled through Workers.complete: its card's lines, its ledger exit_detail and the
    first line of its relay."""
    seen = {}

    async def go():
        unlisted(s, wid, 30, exit={**exit, "ended": time.time()})
        w = s.make({"r": Busy()})

        async def hook(row, end):
            seen["end"] = end
            return True
        w.hooks.append(hook)
        await w.complete(wid)
    asyncio.run(go())
    row = s.ledger.worker_get(wid)
    card = next(t for k, t, _, _ in s.chan.settled if k == f"worker:{wid}").splitlines()
    assert all(W.width(x) <= W.PHONE for x in card)
    return card, row["exit_detail"], workers.relay_text(row, seen["end"], 1500).splitlines()[0]


def test_a_death_for_memory_says_memory_on_the_card_the_relay_and_the_ledger_and_a_kill_with_no_count_reads_as_before(setup):
    # 2026-11-11, units on the box: OOMPolicy=continue ends a main process the kernel killed for memory as signal, KILL;
    # OOMPolicy=stop said oom-kill, exited 143, as seen live
    for wid, exit, detail in (("oomkil", {"result": "signal", "code": "killed", "status": "KILL", "oom_kill": 1}, "signal, killed KILL"),
                              ("oomstp", {"result": "oom-kill", "code": "exited", "status": "143"}, "oom-kill, exited 143")):
        card, said, relay = settle(setup, wid, exit)
        assert card[:2] == [f"{W.NOT_DONE} worker died: it ran out of memory", "merge"] and said == f"{detail}; it ran out of memory"
        assert relay.startswith(f"[worker {wid} finished] died: it ran out of memory, ") and relay.count("memory") == 1
    for wid, exit in (("kill00", {"result": "signal", "code": "killed", "status": "KILL"}),
                      ("kill01", {"result": "signal", "code": "killed", "status": "KILL", "oom_kill": 0})):
        card, said, relay = settle(setup, wid, exit)
        assert (card[:2], said) == ([f"{W.NOT_DONE} worker died: it was killed", "merge"], "signal, killed KILL")
        assert relay.startswith(f"[worker {wid} finished] died: it was killed, ") and "memory" not in relay


def test_a_worker_that_lost_a_process_to_memory_and_ended_any_other_way_says_so_in_one_line_and_none_without(setup):
    one, two = W.OOM_LOST[0], W.OOM_LOST[1].format(n=2)
    for wid, exit, head, line in (
            ("lost01", {"result": "success", "code": "exited", "status": "0", "oom_kill": 1}, "finished clean", one),
            ("lost02", {"result": "timeout", "code": "killed", "status": "TERM", "oom_kill": 2}, "stopped: timed out at 30 min", two),
            ("lost03", {"result": "exit-code", "code": "exited", "status": "3", "oom_kill": 1}, "died: it ended with an error", one),
            ("lost04", {"result": "exit-code", "code": "exited", "status": "143", "oom_kill": 1}, "died: it ended with an error", one),
            ("none00", {"result": "success", "code": "exited", "status": "0", "oom_kill": 0}, "finished clean", None)):
        card, said, relay = settle(setup, wid, exit)
        detail = f"{exit['result']}, {exit['code']} {exit['status']}"
        assert card[0].endswith(f"worker {head}") and relay.startswith(f"[worker {wid} finished] {head}, "), wid
        if line is None:
            assert card[1] == "merge" and said == detail and "memory" not in relay
        else:
            assert card[1:3] == [line, "merge"] and said == f"{detail}; {line}" and relay.endswith(f"; {line}"), wid


def test_a_death_for_memory_is_never_a_stop_nobody_asked_for_and_a_real_one_still_is():
    died = lambda **x: {"exit": "died", **x}  # noqa: E731
    assert not workers.cut_off(died(result_code="oom-kill", status="143"))      # seen live: the fix started again
    assert not workers.cut_off(died(result_code="signal", status="KILL", oom_kill=1))
    assert all(workers.cut_off(died(result_code=r, status=st, **more)) for r, st in (("exit-code", "143"), ("signal", "TERM"))
               for more in ({}, {"oom_kill": 1}))       # a process lost to memory earlier, then the TERM: still cut off


def test_a_worker_a_script_starts_that_answers_exactly_no_reply_ends_with_no_relay_turn_live_or_at_a_re_attach(setup, monkeypatch):
    """need-2026-11-09-77: a timer script started each run with `yerk worker start --brief FILE --room ROOM`, the brief
    saying to answer exactly NO_REPLY when there is nothing, and each quiet run still asked the room for a relay turn that
    had to be answered with a posted line: only a job's hook skipped it, and the workaround, a one-shot agent job a run, left
    a job in the jobs file each time and no thinking level. Now a clean end whose last message is exactly NO_REPLY, read as
    the card reads it, asks no turn from any caller: the relay is skipped with why, nothing reaches the room's session, the
    card settles quiet, live and when a restart finds it ended, and the run keeps its --effort. A run with something to say
    still relays, and one that dies after NO_REPLY is still told."""
    s = setup
    room = s.room()
    (brief := s.tmp / "brief.md").write_text('FAKE report="NO_REPLY" result="NO_REPLY"\nAnswer exactly NO_REPLY when nothing opened.\n')
    asked: list[dict] = []
    monkeypatch.setattr(cli, "_control", lambda state, payload, timeout: asked.append(payload["worker"]) or {"ok": True})
    assert cli.main(["worker", "start", "--brief", str(brief), "--room", "r", "--effort", "low"]) == 0

    async def go():
        w = s.make({"r": room})
        w.begin(asyncio.get_running_loop())
        quiet = await w.handle("", asked[0])                  # what /control does with the script's line
        assert quiet["ok"] and quiet["effort"] == "low", quiet
        await ended(s.ledger, quiet["id"])
        said = await w.start("r", 'FAKE report="One question opened" result="filed a forecast"')
        await ended(s.ledger, said["id"])
        died = await w.start("r", 'FAKE report="NO_REPLY" result="NO_REPLY" exit=3')
        await ended(s.ledger, died["id"])
        unlisted(s, "qu1e72", 30, exit={"result": "success", "code": "exited", "status": "0", "ended": time.time()})
        (s.tmp / "workers" / "qu1e72" / "stdout.jsonl").write_text(json.dumps({"type": "result", "result": "NO_REPLY"}) + "\n")
        await w.reattach()                                    # it ended while no daemon was up
        return quiet, said["id"], died["id"]
    quiet, said, died = asyncio.run(go())
    argv = json.loads((Path(quiet["staging"]) / "fake-record.json").read_text())["argv"]
    assert argv[argv.index("--effort") + 1] == "low"
    for wid in (quiet["id"], "qu1e72"):
        row = s.ledger.worker_get(wid)
        assert (row["exit"], row["relay"], row["relay_note"]) == ("done", "skipped", "the worker answered NO_REPLY"), wid
        assert s.ledger.inbound_get(f"relay-{wid}") is None, wid          # no turn was asked of the room's session
        card = next(t for k, t, _, _ in s.chan.settled if k == f"worker:{wid}")
        head = [f"{W.DONE} worker {W.QUIET_WORKER}"] if wid == quiet["id"] else [f"{W.DONE} worker finished clean", "merge"]
        assert card.splitlines()[:len(head)] == head and W.NO_REPLY not in card    # qu1e72's report says merge (unlisted)
    assert [inbound for _, inbound in s.chan.replies] == [[f"relay-{said}"], [f"relay-{died}"]]   # each told, in order
    assert [s.ledger.worker_get(x)["relay"] for x in (said, died)] == ["relayed", "relayed"]
    listed = {r["id"]: (r["relay"], r["relay_note"]) for r in workers.listing(s.ledger)}
    assert listed[quiet["id"]] == ("skipped", "the worker answered NO_REPLY")       # `yerk worker list` says why


def test_a_relay_left_open_by_a_crash_is_ledgered_as_what_happened(setup):
    s = setup
    for wid, posted in (("aaa111", True), ("bbb222", False)):
        s.ledger.worker_add(id=wid, room="r", unit=f"yerk-worker-{wid}", staging=str(s.tmp / wid), report="", cap=60.0,
                            started=1.0, finished=2.0, exit="done", relay="sent")
        s.ledger.inbound_add(f"relay-{wid}", "r", "worker", "yerk", 10)
        s.ledger.inbound_set(f"relay-{wid}", state="running")
        if posted:                                  # the turn ended and its reply went out, then the process died
            s.ledger.record(room="r", outcome="ok", inbound=f"relay-{wid}", turn_key="k1")
            s.ledger.delivery_plan("k1:0", "r", "reply", "the outcome")
            s.ledger.delivery_set("k1:0", state="sent", message_id="77")
    asyncio.run(s.make({"r": Busy()}).reattach())
    a, b = s.ledger.worker_get("aaa111"), s.ledger.worker_get("bbb222")
    assert a["relay"] == "relayed" and "found delivered after a restart, message(s) 77" in a["relay_note"]
    assert s.ledger.inbound_get("relay-aaa111")["state"] == "answered"
    assert b["relay"] == "failed" and s.ledger.inbound_get("relay-bbb222")["state"] == "abandoned"
    assert workers.status_lines(s.ledger, s.units.running()) == [f"{W.NOTHING} workers: none running"]


def test_status_lists_the_running_workers_exactly_as_the_ledger_has_them(setup):
    s = setup

    async def go():
        w = s.make({"r": Busy()})
        w.begin(asyncio.get_running_loop())
        ids = [(await w.start("r", "FAKE sleep=30", cap_minutes=c))["id"] for c in (5, 65)]
        lines = workers.status_lines(s.ledger, s.units.running(), now=time.time())
        rows = s.ledger.workers(running=True)
        assert [x.split(":")[0] for x in lines] == [f"{W.WORKING} worker {r['id']}" for r in rows] == [
            f"{W.WORKING} worker {i}" for i in ids]
        assert lines[0].endswith("under 1 min of 5") and lines[1].endswith("under 1 min of 65")
        assert all(W.width(x) <= W.PHONE for x in lines)
        full = workers.status_lines(s.ledger, s.units.running(), now=time.time(), full=True)
        assert "of 5 min, unit yerk-worker-" in full[0] and "of 65 min" in full[1]
        assert not any("NO UNIT" in x or "NOT IN" in x for x in full)
        drift = workers.status_lines(s.ledger, {f"yerk-worker-{ids[0]}", "yerk-worker-stray1"})
        assert drift[1] == f"{W.NOT_DONE} worker {ids[1]}: gone, no result"
        assert drift[2] == f"{W.NOT_DONE} unknown worker stray1 running"
        drift = workers.status_lines(s.ledger, {f"yerk-worker-{ids[0]}", "yerk-worker-stray1"}, full=True)
        assert drift[1].endswith("NO UNIT RUNNING") and drift[2] == "unit yerk-worker-stray1: running, NOT IN THE LEDGER"
        for i in ids:
            await w.stop(i)
        return [await ended(s.ledger, i) for i in ids]
    rows = asyncio.run(go())
    assert [r["exit"] for r in rows] == ["stopped", "stopped"]
    assert workers.status_lines(s.ledger, set()) == [f"{W.NOTHING} workers: none running"]
    env = {**os.environ, "YERK_STATE": str(s.tmp)}
    out = subprocess.run([sys.executable, "-m", "yerk_ai.cli", "worker", "list", "--json"], capture_output=True,
                         text=True, cwd=ROOT, env=env, timeout=60)
    assert [w["state"] for w in json.loads(out.stdout)] == ["stopped", "stopped"]


def test_a_worker_gets_only_its_own_accounts_token_path_no_mcp_no_subagents_no_room_session(setup):
    s = setup

    async def go():
        w = s.make({"r": Busy()})
        w.begin(asyncio.get_running_loop())
        out = await w.start("r", 'FAKE report="env checked"', account="one")
        return out, await ended(s.ledger, out["id"])
    out, row = asyncio.run(go())
    call = s.units.calls[0]
    launched = json.dumps([call["argv"], call["env"], call["post"]])
    one, two = s.accounts
    assert set(call["env"]) == {"PATH", "PYTHONPATH", "CLAUDE_CONFIG_DIR", "YERK_TOKEN_FILE"}
    assert call["env"]["YERK_TOKEN_FILE"] == str(one.token_file) and launched.count(str(one.token_file)) == 1
    assert str(two.token_file) not in launched and "secrets" not in launched.replace(str(one.token_file), "")
    folder = Path(out["staging"])
    for f in [*folder.iterdir()]:
        assert TOKEN_ONE not in f.read_text(errors="ignore") and TOKEN_TWO not in f.read_text(errors="ignore")
    assert TOKEN_ONE not in launched and TOKEN_TWO not in launched
    rec = json.loads((folder / "fake-record.json").read_text())
    assert "CLAUDE_CODE_OAUTH_TOKEN" in rec["env_keys"] and "CLAUDE_CONFIG_DIR" in rec["env_keys"]
    assert not {"SSH_AUTH_SOCK", "GPG_AGENT_INFO", "YERK_TOKEN_FILE", "PYTHONPATH"} & set(rec["env_keys"])
    argv = rec["argv"]
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv and "--resume" not in argv
    assert argv[argv.index("--disallowedTools") + 1] == "Agent" and "Agent" not in argv[argv.index("--tools") + 1]
    assert argv[argv.index("--session-id") + 1] == row["session_id"] and rec["cwd"] == str(folder)
    assert row["exit"] == "done"


def test_ticks_become_card_rows_at_most_once_a_second_and_a_restart_keeps_the_worker_card(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(dmod, "MIN_EDIT", 1.0)
    edits = []

    class Msg:
        def __init__(self, ch, mid, content):
            self.channel, self.id, self.content = ch, mid, content

        async def edit(self, content):
            edits.append((time.monotonic(), content))

        async def delete(self):
            deleted.append(self.id)

    class DChan:
        id = 11

        def __init__(self):
            self.n = 0

        async def send(self, content=None, nonce=None, file=None):
            self.n += 1
            sent.append(content)
            return Msg(self, self.n, content)

        def get_partial_message(self, mid):
            return Msg(self, mid, "")

        async def history(self, **kw):
            for m in []:
                yield m

    sent, deleted, the = [], [], DChan()
    client = NS(user=NS(id=99), get_channel=lambda cid: the, event=lambda fn: fn)
    dc = dmod.DiscordChannel("token", 7, [], s.ledger, client=client, state=s.tmp)
    dc.add_room(11, NS(name="r"))

    async def go():
        dc.loop = asyncio.get_running_loop()
        w = s.make({"r": Busy()}, channel=dc)
        w.begin(dc.loop)
        out = await w.start("r", 'FAKE sleep=0.2 ticks=6 gap=0.12 report="Six ticks"', tasks=[f"step {i}" for i in range(6)])
        return out["id"], await ended(s.ledger, out["id"])
    wid, row = asyncio.run(go())
    assert sent and "step 0" in sent[0]
    gaps = [b[0] - a[0] for a, b in zip(edits, edits[1:])]
    assert edits and all(g >= 0.95 for g in gaps), gaps          # at most one edit a second
    assert len(edits) < 6 + 1                                    # six ticks and the settle, coalesced
    assert edits[-1][1].startswith(f"{W.DONE} worker finished clean") and edits[-1][1].count(W.DONE) == 7
    assert [c["how"] for c in s.ledger._select("SELECT how FROM cards", ())] == ["settled: done"]
    # a restart's sweep removes a turn's card, never a worker's, and leaves the relay rows to workers.py
    s.ledger.card_open("501", "r", "worker")
    s.ledger.card_open("502", "r", "turn")
    s.ledger.inbound_add("relay-abc123", "r", "worker", "yerk", 10)
    asyncio.run(dc.recover(11, dc.rooms[11]))
    assert deleted == [502] and [c["message_id"] for c in s.ledger.cards_open("r")] == ["501"]
    assert s.ledger.inbound_get("relay-abc123")["state"] == "received"
    # a relay's reply in the room: the answered message has no Discord id, so no reaction is tried
    assert len(asyncio.run(dc._finish("r", "the outcome, in the room's words", ["relay-abc123"], dmod.DONE))) == 1


def test_fit_holds_a_reply_to_its_cap_and_carries_the_report_line():
    long = "First sentence here. " * 200
    out = fit(long, 1500, ["/w/ab12cd/REPORT.md"])                # a worker's report path, as the room reads it
    assert len(out) <= 1500 and out.endswith("\n-# report saved: `yerk worker report ab12cd`") and "..." in out
    assert fit("Done.", 1500, ["/x/notes/plan.md"]) == "Done.\nplan.md"   # any other path: its name
    saved = workers.relay_line({"id": "ab12cd", "report": "/w/ab12cd/REPORT.md"})   # added once, never twice
    assert fit("All done.", 1500, [saved]) == f"All done.\n{saved}"
    assert fit("All done; `yerk worker report ab12cd` has it.", 1500, [saved]) == "All done; `yerk worker report ab12cd` has it."


def test_a_human_message_during_the_relay_turn_is_answered_inside_it(setup):
    s = setup
    room = s.room()
    seen = []
    relay = Inbound("relay-ccc333", "yerk", "SLOW [worker ccc333 finished] done", cap=1500, must="/x/REPORT.md",
                    then=lambda state, ids, n: seen.append((state, ids)))
    s.ledger.inbound_add("relay-ccc333", "r", "worker", "yerk", 10)
    assert room.submit(relay) == "started"
    time.sleep(0.4)
    s.ledger.inbound_add("h2", "r", "c", "owner", 5)
    assert room.submit(h2 := Inbound("h2", "owner", "and?")) == "folded"
    while room.busy:
        time.sleep(0.05)
    (text, inbound), = s.chan.replies
    assert inbound == ["relay-ccc333", "h2"] and text == f"echo: SLOW [worker ccc333 finished] done + {h2.sent}"
    assert h2.sent == f"{W.FOLDED.format(n=round(h2.received - relay.received))}\n\nand?"     # its seconds since the relay above it
    assert seen == [("answered", ["m1"])] and s.ledger.inbound_get("h2")["state"] == "answered"
    assert room.last_human is not None and s.ledger.last_received("r") == s.ledger.inbound_get("h2")["received"]


def test_one_heavy_worker_per_account_the_next_waits_in_line_then_starts(setup):
    s = setup
    room = s.room()

    async def go():
        w = s.make({"r": room}, per_account=1)
        w.begin(asyncio.get_running_loop())
        first = await w.start("r", 'FAKE sleep=0.6 report="First"', account="one")
        second = await w.start("r", 'FAKE report="Second"', account="one")
        other = await w.start("r", 'FAKE report="Other account"', account="two")      # its own account: no wait
        assert "waiting" not in first and second["waiting"] == W.WAITING_ACCOUNT and second["line"] == 1
        assert "waiting" not in other and len(s.units.calls) == 2                       # the second has no unit yet
        key, text, _ = [c for c in s.chan.cards if c[0] == f"worker:{second['id']}"][0]
        assert text.splitlines()[:2] == [f"{W.WORKING} worker waiting for an account",
                                         f"{W.WAITING} 1st in line for account one"]
        lines = workers.status_lines(s.ledger, s.units.running())
        assert f"{W.WORKING} worker {second['id']}: waiting, 1st in line" in lines
        assert all(W.width(x) <= W.PHONE for x in lines) and lines[-1].endswith("1st in line")
        full = workers.status_lines(s.ledger, s.units.running(), full=True)
        assert any(x.startswith(f"worker {second['id']}: room r, waiting for account one, 1st in line") for x in full)
        a = await ended(s.ledger, first["id"], relay=False)
        b = await ended(s.ledger, second["id"], relay=False)
        return a, b
    a, b = asyncio.run(go())
    assert b["started"] >= a["finished"] - 0.05 and b["queued"] and b["waiting"] == 0 and b["exit"] == "done"
    assert [c["unit"] for c in s.units.calls][-1] == f"yerk-worker-{b['id']}"


def test_a_worker_gets_only_the_read_only_yerk_tools_from_config_never_send_worker_or_schedule(setup, caplog):
    s = setup
    conf = workers.yerk_tools({"yerk_tools": ["recall", "read_memory", "send", "mcp__yerk__worker", "schedule"]})
    assert conf == ["recall", "read_memory"] and "send" in caplog.text and "schedule" in caplog.text
    assert workers.Settings(root=s.tmp).yerk_tools == []                                   # default: none

    async def go():
        w = s.make({"r": Busy()}, yerk_tools=conf)
        w.begin(asyncio.get_running_loop())
        out = await w.start("r", 'FAKE report="tools checked"', account="one")
        return out, await ended(s.ledger, out["id"])
    out, row = asyncio.run(go())
    argv = json.loads((Path(out["staging"]) / "fake-record.json").read_text())["argv"]
    launched = " ".join(argv)
    assert argv[argv.index("--allowedTools") + 1] == "mcp__yerk__recall,mcp__yerk__read_memory"
    assert set(re.findall(r"mcp__yerk__\w+", launched)) == {"mcp__yerk__recall", "mcp__yerk__read_memory"}
    cfg = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())["mcpServers"]["yerk"]
    assert cfg["type"] == "stdio" and cfg["args"][-1] == "recall,read_memory" and "--strict-mcp-config" in argv
    for name in ("send", "hold", "worker", "schedule"):
        assert f"mcp__yerk__{name}" not in launched and name not in cfg["args"][-1].split(",")
    srv = mcp_server.worker_server(s.tmp, "r", ["recall", "read_memory", "send", "worker", "schedule", "hold"])
    assert sorted(t.name for t in asyncio.run(srv.list_tools())) == ["read_memory", "recall"]


def preamble(prompt):
    """A PROMPT.md's own lines, before the brief it carries, which is appended after them: what a test of them reads."""
    return prompt[:prompt.index("\nThe brief (also in ")]


BOTH = ("- You have no room session and no subagents. Of Yerk AI's tools you have mcp__yerk__recall and mcp__yerk__read_memory "
        "and no other: they read the memory of the room that started you, as the room does. Where the brief depends on what the "
        "owner or the room said, decided or keeps on file, look there first. Your cap is 65 minutes; past it the run is stopped.")


def test_a_worker_given_no_yerk_ai_tool_reads_todays_line_to_the_letter_at_any_cap_and_its_add_on_line_right_after(setup):
    """With no [workers] yerk_tools the line is the one every worker read before, byte for byte, at the default cap and at
    another, and with an add-on given its line comes right after it."""
    s = setup
    w = s.make({"r": Busy()}, launcher=Record(s.tmp))
    w.room_confs["r"] = COUNTING
    for kw, line in (({}, "- You have no room session, no Yerk AI tools and no subagents. Your cap is 65 minutes; past it the run is stopped.\n"),
                     ({"cap_minutes": 10},
                      "- You have no room session, no Yerk AI tools and no subagents. Your cap is 10 minutes; past it the run is stopped.\n")):
        bare = preamble(started(w, "r", "look", **kw)[1]).splitlines(keepends=True)
        given = preamble(started(w, "r", "look", addons=["mcp__abacus__sum"], **kw)[1]).splitlines(keepends=True)
        assert bare[5] == given[5] == line and "mcp__yerk__" not in "".join(bare + given)
        assert given[6].startswith("- You have the room's own mcp__abacus__sum too. A step that needs the owner's word (`confirmed`)")
        assert bare[6].startswith("- Your tool calls pass Yerk AI's gates") and given[7] == bare[6]


def test_a_worker_given_both_read_only_tools_is_told_them_by_the_names_its_tool_list_shows_and_what_they_are_for(setup):
    """Seen live: two workers given recall and read_memory read in their prompts that they had no Yerk AI tools, so a
    worker read the memory only when its brief named a tool. The line names what it is granted (--allowedTools), no more."""
    s = setup

    async def go():
        w = s.make({"r": Busy()}, yerk_tools=["recall", "read_memory"])
        w.begin(asyncio.get_running_loop())
        out = await w.start("r", 'FAKE report="told"', account="one")
        return out, await ended(s.ledger, out["id"], relay=False)
    out, row = asyncio.run(go())
    folder = Path(out["staging"])
    own = preamble((folder / "PROMPT.md").read_text())
    argv = json.loads((folder / "fake-record.json").read_text())["argv"]
    granted = set(argv[argv.index("--allowedTools") + 1].split(","))
    assert set(re.findall(r"mcp__yerk__\w+", own)) == granted == {"mcp__yerk__recall", "mcp__yerk__read_memory"}
    assert "no Yerk AI tools" not in own and own.splitlines()[5] == BOTH and row["exit"] == "done"
    assert json.loads((folder / "worker.json").read_text())["yerk_tools"] == ["recall", "read_memory"]


def test_a_worker_given_recall_alone_is_told_that_one_in_words_for_one_and_never_read_memory(setup):
    s = setup
    w = s.make({"r": Busy()}, launcher=Record(s.tmp), yerk_tools=["recall"])
    out, prompt = started(w, "r", "look")
    own = preamble(prompt)
    assert own.splitlines()[5] == ("- You have no room session and no subagents. Of Yerk AI's tools you have mcp__yerk__recall and "
                                   "no other: it reads the memory of the room that started you, as the room does. Where the brief "
                                   "depends on what the owner or the room said, decided or keeps on file, look there first. Your cap "
                                   "is 65 minutes; past it the run is stopped.")
    assert "read_memory" not in own and "they read" not in own and set(re.findall(r"mcp__yerk__\w+", own)) == {"mcp__yerk__recall"}
    spec = json.loads((Path(out["staging"]) / "worker.json").read_text())
    assert spec["yerk_tools"] == ["recall"] and json.loads(Path(spec["mcp_config"]).read_text())["mcpServers"]["yerk"]["args"][-1] == "recall"


def test_a_setting_naming_tools_a_worker_may_not_get_puts_none_of_them_in_its_prompt(setup, caplog):
    """The line names what the worker gets, kept to WORKER_TOOLS, never the raw setting: send, worker, schedule and hold
    are dropped with a warning when yerkd.toml is read, and a Settings built by hand with them names none either."""
    s = setup
    raw = ["recall", "send", "mcp__yerk__worker", "schedule", "hold"]
    for given, names in ((workers.settings(s.tmp, {"yerk_tools": raw}).yerk_tools, {"mcp__yerk__recall"}),
                         (raw, {"mcp__yerk__recall"}), (["send", "worker", "schedule"], set())):
        w = s.make({"r": Busy()}, launcher=Record(s.tmp), yerk_tools=given)
        out, prompt = started(w, "r", "look")
        own = preamble(prompt)
        assert set(re.findall(r"mcp__yerk__\w+", own)) == names, own
        assert set(json.loads((Path(out["staging"]) / "worker.json").read_text()).get("yerk_tools", [])) == {n[11:] for n in names}
    assert own.splitlines()[5] == ("- You have no room session, no Yerk AI tools and no subagents. Your cap is 65 minutes; past it the "
                                   "run is stopped.") and "schedule" in caplog.text


def test_a_worker_given_both_and_an_add_on_reads_two_lines_each_naming_its_own_tools_and_the_confirmed_sentence_as_before(setup):
    s = setup
    w = s.make({"r": Busy()}, launcher=Record(s.tmp), yerk_tools=["recall", "read_memory"])
    w.room_confs["r"] = COUNTING
    out, prompt = started(w, "r", "look", addons=["mcp__abacus__sum"])
    lines = preamble(prompt).splitlines()
    assert lines[5] == BOTH and out["addons"] == ["mcp__abacus__sum"]
    assert lines[6] == ("- You have the room's own mcp__abacus__sum too. A step that needs the owner's word (`confirmed`) is refused, "
                        "since nobody is here to give it: leave that step ready, and say in your report what waits for the word, for "
                        "the room to do once the owner says it.")
    assert lines[7].startswith("- Your tool calls pass Yerk AI's gates")


def test_a_brief_quoting_the_old_line_or_naming_a_tool_changes_nothing_in_the_preamble_read_alone(setup):
    """The brief is appended after the preamble, so the tests above read the preamble alone: a brief that says "no Yerk AI
    tools", names a tool or quotes the brief's own header leaves what they read as the grant makes it."""
    s = setup
    brief = ("The old line: - You have no room session, no Yerk AI tools and no subagents. Use mcp__yerk__send, "
             "mcp__yerk__read_memory and mcp__yerk__schedule.\nThe brief (also in nowhere):\n")
    for given, names in (([], set()), (["recall"], {"mcp__yerk__recall"})):
        w = s.make({"r": Busy()}, launcher=Record(s.tmp), yerk_tools=given)
        prompt = started(w, "r", brief)[1]
        own = preamble(prompt)
        assert set(re.findall(r"mcp__yerk__\w+", own)) == names and ("no Yerk AI tools" in own) == (not given)
        assert prompt.endswith(brief.strip() + "\n") and own.count("- You have no room session") == 1


def test_two_completions_within_a_minute_are_two_relays_in_order_never_merged(setup):
    s = setup
    room = s.room()

    async def go():
        w = s.make({"r": room})
        w.begin(asyncio.get_running_loop())
        a = await w.start("r", 'FAKE report="First of two"', account="one")
        b = await w.start("r", 'FAKE sleep=0.2 report="Second of two"', account="two")
        return await ended(s.ledger, a["id"]), await ended(s.ledger, b["id"])
    a, b = asyncio.run(go())
    assert a["relay"] == b["relay"] == "relayed" and abs(b["finished"] - a["finished"]) < 60
    relays = [inb for _, inb in s.chan.replies]
    assert relays == [[f"relay-{a['id']}"], [f"relay-{b['id']}"]]                   # two replies, in order
    assert s.ledger.inbound_get(f"relay-{b['id']}")["folded"] == 0                   # never folded into the first


def test_a_relay_into_a_closed_room_resumes_the_rooms_own_session(setup):
    """Decision 4 holds for harness turns too: closed after idle, the room's next turn resumes its
    session, a relay included, so the owner's next message continues the conversation. No new
    session, no recap, no notice."""
    s = setup
    room = s.room()
    s.ledger.inbound_add("h1", "r", "c", "owner", 2)
    room.submit(Inbound("h1", "owner", "hi"))
    while room.busy:
        time.sleep(0.05)
    old = room.rp.session_id
    assert room.reap_idle(force=True)                      # the room's process is closed; its session is kept

    async def go():
        w = s.make({"r": room})
        w.begin(asyncio.get_running_loop())
        room.last_human = time.time() - 61
        out = await w.start("r", 'FAKE report="Into a closed room"')
        return await ended(s.ledger, out["id"])
    row = asyncio.run(go())
    assert row["relay"] == "relayed"
    turn = s.ledger._select("SELECT * FROM turns WHERE inbound = ?", (f"relay-{row['id']}",))[0]
    assert turn["session_id"] == old and not turn["recap"] and room.rp.resume_arg == old
    assert SessionTable(s.tmp / "sessions.json").get("r")["session_id"] == old       # still the room's session
    reply, inbound = s.chan.replies[-1]
    assert inbound == [f"relay-{row['id']}"] and not reply.startswith("echo: (Yerk AI:")  # no new-session note
    assert s.chan.notices == []
    assert row["report"] not in reply and f"`yerk worker report {row['id']}`" in reply


# the sandbox: what a worker's unit sees of the state folder, and which rooms may use workers
class Record(workers.Systemd):
    """A launcher that only records: nothing runs, no systemd, no model; its fence's question is the launcher's own."""

    def __init__(self, state=None, hidden=()):
        super().__init__(state, hidden)
        self.calls = []

    def start(self, unit, argv, env, cwd, out, err, cap, post):
        self.calls.append({"unit": unit, "cwd": cwd})

    def stop(self, unit):
        pass

    def running(self):
        return set()


def test_the_unit_sees_the_state_folder_read_only_its_own_folder_writable_and_no_secret_or_room(tmp_path, monkeypatch):
    lines = []
    monkeypatch.setattr(workers.subprocess, "run", lambda cmd, **kw: lines.append(cmd) or NS(returncode=0, stderr=""))
    state = tmp_path / "state"
    folder, secret, rooms = state / "workers" / "abc123", state / "secrets" / "yerk-http.secret", state / "rooms"
    unit = workers.Systemd(state, (secret, rooms))
    unit.start("yerk-worker-abc123", ["true"], {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}, folder, folder / "o",
               folder / "e", 60, ["true"])
    unit.start("yerk-worker-abc124", ["true"], {"CLAUDE_CONFIG_DIR": str(state / "accounts" / "one")}, folder,
               folder / "o", folder / "e", 60, ["true"])
    folder.mkdir(parents=True)                                        # a worker of room r: its work folder comes back
    (folder / "worker.json").write_text(json.dumps({"room": "r"}))
    unit.start("yerk-worker-abc125", ["true"], {}, folder, folder / "o", folder / "e", 60, ["true"])
    props = [[c[i + 1] for i, a in enumerate(c[:c.index("--")]) if a == "-p"] for c in lines]
    for p in props:
        assert "PrivateUsers=yes" in p and f"ReadOnlyPaths={state}" in p
        assert f"InaccessiblePaths={secret}" in p and f"TemporaryFileSystem={rooms}:ro" in p
        assert f"InaccessiblePaths={rooms}" not in p
        assert not any("yerk-worker.secret" in x for x in p)          # the worker secret stays readable
    assert not any(x.startswith("BindPaths=") for x in props[0] + props[1])     # no spec: no room folder at all
    assert [x for x in props[2] if x.startswith("BindPaths=")] == [f"BindPaths=-{rooms / 'r' / 'work'}"]
    assert [x for x in props[0] if x.startswith("ReadWritePaths=")] == [f"ReadWritePaths={folder}"]
    assert [x for x in props[1] if x.startswith("ReadWritePaths=")] == [f"ReadWritePaths={folder}",
                                                                        f"ReadWritePaths={state / 'accounts' / 'one'}"]
    from yerk_ai.config import EXAMPLE_PRIVATE
    from yerk_ai.daemon import Yerkd
    monkeypatch.undo()                                                # the daemon runs git: the real subprocess again
    (tmp_path / "d").mkdir()
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path / "d")                         # the daemon hands all four paths to its launcher
    assert isinstance(y.workers.launcher, workers.Systemd) and y.workers.launcher.state == tmp_path / "d"
    assert y.workers.launcher.hidden == (y.secret_file, tmp_path / "d" / "rooms", EXAMPLE_PRIVATE / "secrets",
                                         tmp_path / "d" / "secrets" / "pages")   # the live pages' keys: each opens a room as the owner
    assert (tmp_path / "d" / "rooms").is_dir() and (tmp_path / "d" / "secrets" / "pages").is_dir()   # there before a unit starts


def unit_props(monkeypatch, launcher, folder):
    """The -p properties `launcher` hands systemd-run for one worker in `folder`, nothing run."""
    lines = []
    monkeypatch.setattr(workers.subprocess, "run", lambda cmd, **kw: lines.append(cmd) or NS(returncode=0, stderr=""))
    launcher.start("yerk-worker-abc123", ["true"], {}, folder, folder / "o", folder / "e", 60, ["true"])
    return [lines[0][i + 1] for i, a in enumerate(lines[0][:lines[0].index("--")]) if a == "-p"]


def test_a_worker_unit_has_a_4g_memory_ceiling_no_swap_and_outlives_a_kill_for_memory(tmp_path, monkeypatch):
    # 2026-11-11: a worker reproducing an out-of-memory bug took 16G and 3.2G of swap on a 20G box, and the box froze
    for launcher in (workers.Systemd(), workers.Systemd(tmp_path / "state", (tmp_path / "state" / "rooms",))):
        props = unit_props(monkeypatch, launcher, tmp_path / "state" / "workers" / "abc123")
        assert [p for p in props if p.startswith(("Memory", "OOM"))] == ["MemoryMax=4G", "MemorySwapMax=0", "OOMPolicy=continue"]
        assert "RuntimeMaxSec=60" in props and "KillMode=control-group" in props    # the time cap and the stop as they were


def test_the_exit_record_carries_the_units_kills_for_memory_and_a_count_it_cannot_read_leaves_it_as_before(tmp_path, monkeypatch):
    # ExecStopPost runs in the unit's own cgroup and reads its memory.events there (systemd 259, two units on the box, 2026-11-11)
    unit = "user.slice/app.slice/yerk-worker-abc123.service"
    events, d = tmp_path / "fs" / unit / "memory.events", tmp_path / "w"
    events.parent.mkdir(parents=True)
    d.mkdir()
    (tmp_path / "cgroup").write_text(f"0::/{unit}\n")
    monkeypatch.setattr(workers, "OWN_CGROUP", tmp_path / "cgroup")
    monkeypatch.setattr(workers, "CGROUP", tmp_path / "fs")
    for k, v in {"SERVICE_RESULT": "success", "EXIT_CODE": "exited", "EXIT_STATUS": "0", "INVOCATION_ID": "e3f0"}.items():
        monkeypatch.setenv(k, v)

    def record():
        assert workers.post(d) == 0
        return json.loads((d / "exit.json").read_text())
    events.write_text("low 0\nhigh 0\nmax 63\noom 2\noom_kill 2\noom_group_kill 0\n")
    assert {k: v for k, v in record().items() if k != "ended"} == {"result": "success", "code": "exited", "status": "0", "oom_kill": 2}
    events.write_text("low 0\nhigh 0\n")                       # no count in it
    assert sorted(record()) == ["code", "ended", "result", "status"]
    events.unlink()                                             # the cgroup already gone
    assert sorted(record()) == ["code", "ended", "result", "status"]
    events.write_text("oom_kill 1\n")
    monkeypatch.delenv("INVOCATION_ID")                         # not in a unit: the count would be another cgroup's
    assert sorted(record()) == ["code", "ended", "result", "status"]


def launched_with(s, monkeypatch, conf):
    """The -p properties of a worker's unit on an install whose [workers] is `conf`: its settings, and the launcher Workers builds."""
    w = workers.Workers(s.ledger, workers.settings(s.tmp, conf), s.accounts, Cooldowns(None), {}, {}, {})
    return w.conf, unit_props(monkeypatch, w.launcher, s.tmp / "workers" / "abc123")


def test_an_install_sets_its_own_worker_memory_ceiling_in_workers_memory_max(setup, monkeypatch, caplog):
    for given, sent in (("6G", "6G"), ("512m", "512M"), (" 2t ", "2T"), (3221225472, "3221225472"), ("1048576K", "1048576K")):
        conf, props = launched_with(setup, monkeypatch, {"memory_max": given})
        assert f"MemoryMax={sent}" in props and "MemorySwapMax=0" in props and "OOMPolicy=continue" in props
        assert conf.memory_max == sent and "memory_max" not in caplog.text


def test_a_memory_max_that_is_not_a_size_never_reaches_systemd_the_default_does_with_a_warning(setup, monkeypatch, caplog):
    for bad in ("lots", "4 GB", "4GB", "1.5G", "50%", "infinity", "", "0", 0, -1, 2.5, True, ["4G"], "20000000000000000T"):
        caplog.clear()
        conf, props = launched_with(setup, monkeypatch, {"memory_max": bad})
        assert [p for p in props if p.startswith("MemoryMax=")] == ["MemoryMax=4G"] and conf.memory_max == "4G"
        assert f"memory_max {bad!r} is not a size; a worker gets 4G" in caplog.text
    caplog.clear()
    assert launched_with(setup, monkeypatch, {})[0].memory_max == "4G" and not caplog.text   # unset: the default, said nowhere


def test_the_unit_does_not_see_the_private_layers_secrets_but_its_own_accounts_token(tmp_path):
    # a worker read the install's provider keys from the private layer's secrets/ and spent with them outside the
    # daemon's tools, their gates and the ledger (2026-10-31); the folder holds every account's token too
    state, keys = tmp_path / "state", tmp_path / "private" / "secrets"
    folder, secret, rooms, own = state / "workers" / "abc123", state / "secrets" / "yerk-http.secret", state / "rooms", keys / "claude-one.token"
    was, now = workers.Systemd(state, (secret, rooms)), workers.Systemd(state, (secret, rooms, keys))
    for env in ({}, {"YERK_TOKEN_FILE": str(tmp_path / "tokens" / "claude-one.token")}, {"YERK_TOKEN_FILE": str(own)}):
        p, before = now.sandbox(folder, env)[1::2], was.sandbox(folder, env)[1::2]
        assert f"ReadOnlyPaths={state}" in p and f"ReadWritePaths={folder}" in p and f"InaccessiblePaths={secret}" in p
        assert [x for x in p if str(keys) not in x] == before          # the rest exactly as it was
        if env.get("YERK_TOKEN_FILE") == str(own):    # its token there, the default: an empty folder but that file, read-only
            assert [x for x in p if str(keys) in x] == [f"TemporaryFileSystem={keys}:ro", f"BindReadOnlyPaths=-{own}"]
        else:                                         # no token there: not there at all, and with no secrets/ it still starts
            assert [x for x in p if str(keys) in x] == [f"InaccessiblePaths=-{keys}"]


# what a start says of the paths its brief names that the unit hides (Systemd.unseen): told, never refused
def fence_of(t):
    """The daemon's four hidden paths on the setup's state folder `t`: the install's secret, every room's folder, the private
    layer's secrets/ (the setup's, holding each account's token, as the default does) and the pages' keys."""
    return (t / "yerk" / "yerk-http.secret", t / "rooms", t / "secrets", t / "yerk" / "pages")


def started(w, room, brief, **kw):
    """`start`'s answer and the PROMPT.md it wrote."""
    out = asyncio.run(w.start(room, brief, account="one", **kw))
    return out, (Path(out["staging"]) / "PROMPT.md").read_text()


def test_a_brief_naming_another_rooms_work_folder_starts_and_its_answer_and_prompt_say_the_worker_cannot_see_it(setup, caplog):
    # seen live: a brief's third source was another room's work folder; the start said nothing and the worker ran
    # one source short. The unit is right to hide it; the start now says so, and the worker is told
    s = setup
    caplog.set_level("INFO", logger="yerk.workers")
    units = Record(s.tmp, fence_of(s.tmp))
    w = s.make({"r": Busy(), "other": Busy()}, launcher=units)
    own, other = s.tmp / "rooms" / "r" / "work", s.tmp / "rooms" / "other" / "work"
    brief = (f"Three sources.\n1. {own}/a.md\n2. The checkout {ROOT}.\n"
             f"3. Another room's work folder {other}/: notes.md and the list*.txt reads.\nAgain: {other}.\n")
    out, prompt = started(w, "r", brief)
    assert out["ok"] and out["unseen"] == [str(other)]
    assert out["unseen_note"] == W.WORKER_UNSEEN.format(paths=other, work=own) and str(own) in out["unseen_note"]
    line = (f"- Not there for you, since your unit hides them: {other}. The brief names them; name them in your report's "
            "first lines, with what you did without them.\n")
    assert line in prompt and prompt.index(line) < prompt.index("The brief (also in")
    assert [c["unit"] for c in units.calls] == [out["unit"]]                  # launched as before
    assert (Path(out["staging"]) / "BRIEF.md").read_text() == brief            # the brief as given
    assert f"worker {out['id']}: its brief names 1 path(s) its unit hides" in caplog.text and str(other) not in caplog.text


def test_a_brief_naming_only_what_the_worker_sees_gets_todays_answer_and_prompt(setup, caplog):
    s = setup
    caplog.set_level("INFO", logger="yerk.workers")
    own = s.tmp / "rooms" / "r" / "work"
    brief = (f"Write in {s.tmp / 'workers'}, read {own}, {own / 'notes' / 'a.md'} and `{ROOT / 'yerk_ai'}`, "
             f"then ~/notes/today.md and {Path.home() / 'Documents'}; your token is {s.accounts[0].token_file}.")
    fenced, plain = s.make({"r": Busy()}, launcher=Record(s.tmp, fence_of(s.tmp))), s.make({"r": Busy()}, launcher=Record())
    (out, prompt), (was, before) = started(fenced, "r", brief), started(plain, "r", brief)   # no state folder: no fence
    assert sorted(out) == sorted(was) == ["account", "cap_minutes", "card", "effort", "id", "model", "ok", "report", "room",
                                          "staging", "tasks", "unit"]
    assert prompt.replace(out["id"], "ID") == before.replace(was["id"], "ID") and "Not there for you" not in prompt
    assert "hides" not in caplog.text


def test_unseen_hides_every_rooms_folder_but_the_workers_rooms_work_folder_and_every_secret_but_its_own_token(setup):
    s = setup
    unit, rooms, keys = workers.Systemd(s.tmp, fence_of(s.tmp)), s.tmp / "rooms", s.tmp / "secrets"
    token, other = str(keys / "claude-one.token"), str(keys / "claude-two.token")
    ask = lambda *ps, room="r", bind="", tok=token: unit.unseen(room, bind, tok, [str(p) for p in ps])  # noqa: E731
    assert ask(rooms / "r" / "settings.json", rooms / "r", rooms, rooms / "r" / "work" / "x.md", rooms / "r" / "work") == \
        [str(rooms / "r" / "settings.json"), str(rooms / "r"), str(rooms)]   # its room outside work and the root alone
    assert ask(keys, other, token, keys / "openai.key") == [str(keys), other, str(keys / "openai.key")]
    assert ask(token, tok="") == [token]                                        # no token of its own: none comes back
    assert ask(s.tmp / "yerk" / "yerk-http.secret", s.tmp / "yerk" / "pages" / "r.key", s.tmp / "yerk") == \
        [str(s.tmp / "yerk" / "yerk-http.secret"), str(s.tmp / "yerk" / "pages" / "r.key")]
    # given `bind` (a pictures backfill): the bound room's work folder is seen, the calling room's own is hidden
    assert ask(rooms / "r" / "work" / "inbox", rooms / "ops" / "work", room="ops", bind="r") == [str(rooms / "ops" / "work")]
    w = s.make({"ops": Busy(), "r": Busy()}, launcher=Record(s.tmp, fence_of(s.tmp)))
    out, _ = started(w, "ops", f"Describe {rooms / 'r' / 'work' / 'inbox' / 'p.png'}; not {rooms / 'ops' / 'work'}.",
                     more={"bind": "r"})
    assert out["unseen"] == [str(rooms / "ops" / "work")] and str(rooms / "r" / "work") in out["unseen_note"]
    assert ask(rooms, rooms, rooms / "r") == [str(rooms), str(rooms / "r")]                    # each once
    assert workers.Systemd(None, fence_of(s.tmp)).unseen("r", "", token, [str(rooms / "other")]) == []   # no state folder


def test_a_brief_names_its_paths_in_backticks_quotes_brackets_at_a_sentences_end_and_as_globs_each_once(setup, monkeypatch):
    s = setup
    monkeypatch.setenv("HOME", str(s.tmp / "home"))
    o = s.tmp / "rooms" / "other"
    assert workers.named(f"See `{o}/a`, \"{o}/b\" and '{o}/c' ({o}/d) [{o}/e] {{{o}/f}}. Then {o}/g; {o}/h: {o}/i? "
                         f"{o}/j! ~/k/l. {o}/*.txt and {o}/x?.md. Again {o}/a. https://example.com/x a/b TemporaryFileSystem={o}/m") \
        == [f"{o}/{x}" for x in "abcdefghij"] + [str(s.tmp / "home" / "k" / "l"), f"{o}/*.txt", f"{o}/x?.md", f"{o}/m"]
    unit = workers.Systemd(s.tmp, fence_of(s.tmp))
    home = s.tmp / "rooms" / "r" / "work"
    assert unit.unseen("r", "", "", [f"{home}/list*.txt", f"{s.tmp}/rooms/*/work", f"{s.tmp}/rooms*", f"{s.tmp}/room?"]) == \
        [f"{s.tmp}/rooms/*/work", f"{s.tmp}/rooms*"]                             # a glob by the part before its wildcard
    w = s.make({"r": Busy()}, launcher=Record(s.tmp, fence_of(s.tmp)))
    five = [str(o / n / "work") for n in ("a", "b", "c", "d", "e")]
    out, prompt = started(w, "r", "Read " + ", ".join(five) + f" and ~/notes, then {five[0]} again.")
    assert out["unseen"] == five and out["unseen_note"].startswith(f"this worker cannot see {', '.join(five[:3])} and 2 more, and")
    assert f"hides them: {', '.join(five)}." in prompt                          # the worker is told every one


def test_a_link_in_the_rooms_work_folder_into_a_hidden_folder_is_hidden(setup):
    s = setup
    work, other = s.tmp / "rooms" / "r" / "work", s.tmp / "rooms" / "other" / "work"
    work.mkdir(parents=True)
    other.mkdir(parents=True)
    (work / "theirs").symlink_to(other)
    (work / "mine").symlink_to(work)
    unit = workers.Systemd(s.tmp, fence_of(s.tmp))
    assert unit.unseen("r", "", "", [str(work / "theirs"), str(work / "theirs" / "notes.md"), str(work / "mine")]) == \
        [str(work / "theirs"), str(work / "theirs" / "notes.md")]


def test_a_start_with_no_state_folder_names_nothing_and_a_worker_in_line_gets_the_same_answer(setup):
    s = setup
    brief = f"Read {s.tmp / 'rooms' / 'other' / 'work'}."
    out, prompt = started(s.make({"r": Busy()}, launcher=Record(None, fence_of(s.tmp))), "r", brief)
    assert "unseen" not in out and "Not there for you" not in prompt
    w = s.make({"r": Busy()}, launcher=Record(s.tmp, fence_of(s.tmp)), per_account=1)
    (first, _), (second, told) = started(w, "r", brief), started(w, "r", brief)
    assert "waiting" not in first and second["waiting"] == W.WAITING_ACCOUNT and second["line"] == 1
    assert first["unseen"] == second["unseen"] == [brief[5:-1]] and first["unseen_note"] == second["unseen_note"]
    assert "Not there for you" in told
    (s.tmp / "brief.md").write_text(brief)
    assert asyncio.run(w.start("r", brief_file=str(s.tmp / "brief.md"), account="two"))["unseen"] == first["unseen"]


def test_unseen_and_the_units_properties_agree_on_every_path(setup):
    # the question and the fence read one _fence: for the same hidden paths, each sample is hidden by unseen exactly when
    # it is under a TemporaryFileSystem or an InaccessiblePaths of sandbox's and under no Bind line
    s = setup
    unit, rooms, keys = workers.Systemd(s.tmp, fence_of(s.tmp)), s.tmp / "rooms", s.tmp / "secrets"
    token, folder = keys / "claude-one.token", s.tmp / "workers" / "abc123"
    samples = [rooms, rooms / "r", rooms / "r" / "settings.json", rooms / "r" / "work", rooms / "r" / "work" / "a.md",
               rooms / "ops", rooms / "ops" / "work", rooms / "ops" / "work" / "inbox" / "p.png", rooms / "other" / "work",
               keys, token, keys / "claude-two.token", keys / "deep" / "x", s.tmp / "yerk", s.tmp / "yerk" / "yerk-http.secret",
               s.tmp / "yerk" / "pages", s.tmp / "yerk" / "pages" / "r.key", s.tmp, folder, s.tmp / "ledger.sqlite", ROOT,
               Path.home()]
    folder.mkdir(parents=True)
    for spec, token_env in (({"room": "r"}, str(token)), ({"room": "ops", "bind": "r"}, str(token)), ({"room": "r"}, ""),
                            ({"room": "../r"}, str(token))):
        (folder / "worker.json").write_text(json.dumps(spec))
        props = unit.sandbox(folder, {"YERK_TOKEN_FILE": token_env} if token_env else {})[1::2]
        gone = [Path(re.sub(r"^\w+=-?|:ro$", "", p)) for p in props if p.startswith(("TemporaryFileSystem=", "InaccessiblePaths="))]
        back = [Path(p.split("=-", 1)[1]) for p in props if p.startswith(("BindPaths=", "BindReadOnlyPaths="))]
        by_props = [str(q) for q in samples if any(q.is_relative_to(g) for g in gone) and not any(q.is_relative_to(b) for b in back)]
        assert unit.unseen(spec["room"], spec.get("bind", ""), token_env, [str(q) for q in samples]) == by_props, spec
        assert len(by_props) >= 9, spec                                         # the fixture hides something


def test_a_room_name_that_is_a_path_gets_nothing_back_a_link_out_of_a_hidden_folder_stays_hidden_and_a_token_not_there_is_none(setup):
    # three guards the fence and its question share that no test held: the agreement test above cannot see them, since
    # both of its sides read one _fence
    s = setup
    unit, rooms = workers.Systemd(s.tmp, fence_of(s.tmp)), s.tmp / "rooms"
    folder, theirs = s.tmp / "workers" / "abc123", rooms / "r" / "work" / "a.md"
    folder.mkdir(parents=True)
    # the room whose work folder comes back is a name, never a path: rooms/../r/work is a folder outside rooms/, and a
    # BindPaths line is read and write
    for spec in ({"room": "../r"}, {"room": "a/b"}, {"room": "."}, {"room": ".."}, {"room": "r", "bind": "../b"}, {"room": "r", "bind": "."}):
        (folder / "worker.json").write_text(json.dumps(spec))
        props = unit.sandbox(folder, {})[1::2]
        assert f"TemporaryFileSystem={rooms}:ro" in props and not any(x.startswith("Bind") for x in props), spec
        assert unit.unseen(spec["room"], spec.get("bind", ""), "", [str(s.tmp / "r" / "work"), str(theirs)]) == [str(theirs)], spec
    (folder / "worker.json").write_text(json.dumps({"room": "r"}))
    assert [x for x in unit.sandbox(folder, {})[1::2] if x.startswith("Bind")] == [f"BindPaths=-{rooms / 'r' / 'work'}"]
    # a path is judged as written too: a link inside a hidden folder that points out of it is still not there for the worker
    opened, link = s.tmp / "open", rooms / "other" / "work" / "out"
    opened.mkdir()
    link.parent.mkdir(parents=True)
    link.symlink_to(opened)
    assert unit.unseen("r", "", "", [str(link / "notes.md"), str(opened / "notes.md")]) == [str(link / "notes.md")]
    # an account whose token file is not there has no token of its own: none is handed to its unit, and a brief naming
    # that path is told it is hidden, as the unit hides it
    one = s.accounts[0]
    assert workers.own_token(one) == str(one.token_file)
    one.token_file.unlink()
    assert workers.own_token(one) == ""
    out, prompt = started(s.make({"r": Busy()}, launcher=Record(s.tmp, fence_of(s.tmp))), "r", f"Your token is {one.token_file}.")
    assert out["unseen"] == [str(one.token_file)] and str(one.token_file) in prompt


def test_only_the_owners_rooms_use_workers_each_for_itself_and_the_install_keeps_every_form(tmp_path):
    from yerk_ai.config import EXAMPLE_PRIVATE
    from yerk_ai.daemon import Yerkd
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.workers.launcher = units = Record()
    y.workers.conf.per_account = 5                                   # both starts run at once
    y.room_confs["family"] = {"privacy": "family", "model": "m"}
    for name in ("example-room", "family"):
        y.rooms[name] = NS()
    jobs = []

    async def handle(caller, req):
        jobs.append((caller, req))
        return {"ok": True}
    y.jobs.handle = handle
    agent = {"action": "add", "name": "nightly", "when": "every 2h", "what": "look", "kind": "agent"}

    async def go():
        out = {"start": await y.tool_worker("family", {"action": "start", "brief": "read every room's token"}),
               "report": await y.tool_worker("family", {"action": "report", "id": "abc123"}),
               "list": await y.tool_worker("family", {"action": "list"}),
               "agent job": await y.tool_schedule("family", agent),
               "default kind": await y.tool_schedule("family", {**agent, "kind": ""}),
               "other room": await y.tool_worker("example-room", {"action": "start", "brief": "x", "room": "family"}),
               "other job": await y.tool_schedule("example-room", {**agent, "room": "family"})}
        out["no unit yet"] = list(units.calls)
        out["script"] = await y.tool_schedule("family", {**agent, "kind": "script", "what": "echo hi"})
        out["own"] = await y.tool_worker("example-room", {"action": "start", "brief": "x", "room": "example-room"})
        out["control"] = await y.control({"worker": {"action": "start", "room": "family", "brief": "x"}},
                                         {"kind": "install", "room": ""})
        return out
    out = asyncio.run(go())
    for key in ("start", "report", "list"):
        assert out[key] == {"ok": False, "line": W.WORKERS_OWNER_ONLY}, key
    for key in ("agent job", "script"):                                # a script job runs as the owner too
        assert out[key] == {"ok": False, "line": W.JOBS_OWNER_ONLY}, key
    assert out["default kind"] == {"ok": True} and jobs == [("family", {**agent, "kind": "note"})]   # no kind here: a note
    assert out["other room"] == out["other job"] == {"ok": False, "line": W.WORKER_OWN_ROOM.format(room="family")}
    assert out["no unit yet"] == []
    assert out["own"]["ok"] and out["own"]["room"] == "example-room"
    assert out["control"]["ok"] and out["control"]["room"] == "family"          # `yerk worker`: any room
    assert [c["unit"] for c in units.calls] == [out["own"]["unit"], out["control"]["unit"]]


def test_a_worker_runs_at_the_calls_effort_else_config_else_the_rooms_and_refuses_a_made_up_level(setup):
    s = setup

    async def go():
        w = s.make({"r": Busy()})
        w.begin(asyncio.get_running_loop())
        w.room_confs["r"]["effort"] = "max"
        a = await w.start("r", 'FAKE report="room effort"', account="one")
        await ended(s.ledger, a["id"])
        (s.tmp / "rooms" / "r").mkdir(parents=True)                       # the room's /effort: its override beats rooms.toml
        (s.tmp / "rooms" / "r" / "override.json").write_text(json.dumps({"effort": "medium"}))
        o = await w.start("r", 'FAKE report="live effort"', account="one")
        await ended(s.ledger, o["id"])
        w.conf.effort = "xhigh"
        b = await w.start("r", 'FAKE report="config effort"', account="one")
        await ended(s.ledger, b["id"])
        c = await w.start("r", 'FAKE report="call effort"', account="one", effort="high")
        await ended(s.ledger, c["id"])
        before = sorted(p.name for p in (s.tmp / "workers").iterdir())
        d = await w.start("r", 'FAKE report="bad effort"', account="one", effort="turbo")
        return a, o, b, c, d, before
    a, o, b, c, d, before = asyncio.run(go())
    for out, level in ((a, "max"), (o, "medium"), (b, "xhigh"), (c, "high")):
        argv = json.loads((Path(out["staging"]) / "fake-record.json").read_text())["argv"]
        assert argv[argv.index("--effort") + 1] == level and out["effort"] == level
        assert json.loads((Path(out["staging"]) / "worker.json").read_text())["effort"] == level
    assert d["ok"] is False and "no effort level 'turbo'" in d["error"]
    assert sorted(p.name for p in (s.tmp / "workers").iterdir()) == before      # a refusal leaves no folder behind


HANDS = {"type": "stdio", "command": "/usr/bin/node", "args": ["/opt/example/hands/server.mjs"], "env": {"YERK_ROOM": "r"},
         "timeout": 120000}
ROOM = {"model": "m", "allow": ["mcp__hands__browser", "mcp__hands__desktop", "mcp__web__web_fetch"],
        "mcp": {"hands": HANDS, "web": {"type": "stdio", "command": "web", "env": {"WEB_API_KEY": "placeholder"}}}}


def test_a_worker_gets_the_rooms_own_addon_tools_it_names_its_server_as_the_room_runs_it_and_nothing_else(setup):
    """Need 83 of 2026-11-10: a room asked for a worker to fill web forms in the browser, and the worker it started had no
    browser tool at all. A start's `addons` gives it the room's own add-on tools, by name or by server."""
    s = setup

    async def go():
        w = s.make({"r": Busy()})
        w.room_confs["r"] = ROOM
        w.begin(asyncio.get_running_loop())
        one = await w.start("r", 'FAKE report="browsed"', account="one", addons=["mcp__hands__browser"])
        both = await w.start("r", 'FAKE report="both"', account="one", addons=["hands"])
        return one, both, await ended(s.ledger, one["id"]), await ended(s.ledger, both["id"])
    one, both, row, _ = asyncio.run(go())
    folder = Path(one["staging"])
    argv = json.loads((folder / "fake-record.json").read_text())["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__hands__browser" and "--strict-mcp-config" in argv
    cfg = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())["mcpServers"]
    assert list(cfg) == ["hands"]                                   # no web, and no yerk server: none was configured
    assert cfg["hands"]["args"] == HANDS["args"] and cfg["hands"]["env"] == {
        "YERK_ROOM": "r", "YERK_WORKER": one["id"], "YERK_WORK_DIR": str(folder), "YERK_TOOL_TIMEOUT": "120000"}
    spec = json.loads((folder / "worker.json").read_text())
    assert spec["addon_tools"] == one["addons"] == ["mcp__hands__browser"] and row["exit"] == "done"
    prompt = (folder / "PROMPT.md").read_text()
    assert "the room's own mcp__hands__browser too" in prompt and "`confirmed`) is refused" in prompt
    assert both["addons"] == ["mcp__hands__browser", "mcp__hands__desktop"]
    plain = json.loads((Path(both["staging"]) / "worker.json").read_text())
    assert gates.given("mcp__hands__desktop", plain) and not gates.given("mcp__web__web_fetch", plain)


@pytest.mark.parametrize(("want", "why"), [(["mcp__hands__mail"], "no add-on tool 'mcp__hands__mail'"),
                                           (["mcp__yerk__send"], "no add-on tool 'mcp__yerk__send'"),
                                           (["web"], "passes a credential")])
def test_a_worker_never_gets_an_addon_tool_its_room_has_not_or_one_whose_table_hands_over_a_credential(setup, want, why):
    s = setup

    async def go():
        w = s.make({"r": Busy()})
        w.room_confs["r"] = ROOM
        return await w.start("r", 'FAKE report="x"', account="one", addons=want)
    out = asyncio.run(go())
    assert out["ok"] is False and why in out["error"] and "mcp__hands__browser" in out["error"] + ROOM["allow"][0]
    assert s.units.calls == [] and not (s.tmp / "workers").exists()       # refused whole: no folder, no unit


def test_the_worker_tool_hands_the_rooms_browser_to_a_worker_from_an_owner_room_only(tmp_path):
    from yerk_ai.config import EXAMPLE_PRIVATE
    from yerk_ai.daemon import Yerkd
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.workers.launcher = units = Record()
    y.room_confs["example-room"] = {**ROOM, "privacy": "owner"}
    y.room_confs["family"] = {**ROOM, "privacy": "family"}
    for name in ("example-room", "family"):
        y.rooms[name] = NS()
    ask = {"action": "start", "brief": "fill the example form", "addons": ["mcp__hands__browser"]}

    async def go():
        return await y.tool_worker("example-room", ask), await y.tool_worker("family", ask)
    own, family = asyncio.run(go())
    assert own["ok"] and own["addons"] == ["mcp__hands__browser"] and family == {"ok": False, "line": W.WORKERS_OWNER_ONLY}
    spec = json.loads((Path(own["staging"]) / "worker.json").read_text())
    servers = json.loads(Path(spec["mcp_config"]).read_text())["mcpServers"]
    assert spec["addon_tools"] == ["mcp__hands__browser"] and servers["hands"]["env"]["YERK_WORKER"] == own["id"]
    assert [c["unit"] for c in units.calls] == [own["unit"]]


ABACUS = {"type": "stdio", "command": "/usr/bin/node", "args": ["/opt/example/abacus/server.mjs"], "env": {"YERK_ROOM": "r"}}
COUNTING = {"model": "m", "allow": ["mcp__abacus__sum", "mcp__abacus__chart", "mcp__web__web_fetch"],          # a made-up add-on
            "mcp": {"abacus": ABACUS, "web": {"type": "stdio", "command": "web", "env": {"WEB_API_KEY": "placeholder"}}}}


def test_a_worker_started_naming_no_add_on_gets_the_ones_its_room_gives_every_worker(setup):
    """Seen live: a booked agent job told to search the owner's mail started a worker with no mail tool, since a job
    could name no add-on and no start got one unnamed. A room's worker_addons go to every worker started from it; a room
    with none starts the same worker as before, no add-on and no MCP server."""
    s = setup

    async def go():
        w = s.make({"r": Busy(), "q": Busy()})
        w.room_confs["r"] = {**COUNTING, "worker_addons": ["mcp__abacus__sum"]}
        w.room_confs["q"] = COUNTING
        w.begin(asyncio.get_running_loop())
        out, bare = await w.start("r", 'FAKE report="read"', account="one"), await w.start("q", 'FAKE report="x"', account="one")
        return out, await ended(s.ledger, out["id"]), bare, await ended(s.ledger, bare["id"])
    out, row, bare, _ = asyncio.run(go())
    folder = Path(out["staging"])
    spec = json.loads((folder / "worker.json").read_text())
    assert spec["addon_tools"] == out["addons"] == ["mcp__abacus__sum"] and row["exit"] == "done"
    cfg = json.loads(Path(spec["mcp_config"]).read_text())["mcpServers"]
    assert list(cfg) == ["abacus"] and cfg["abacus"]["env"]["YERK_WORKER"] == out["id"]
    prompt = (folder / "PROMPT.md").read_text()
    assert "- You have the room's own mcp__abacus__sum too." in prompt and "`confirmed`) is refused" in prompt
    argv = json.loads((folder / "fake-record.json").read_text())["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "mcp__abacus__sum"
    plain = json.loads((Path(bare["staging"]) / "worker.json").read_text())
    assert "addon_tools" not in plain and "mcp_config" not in plain and "addons" not in bare
    assert not (Path(bare["staging"]) / "mcp.json").exists() and "the room's own" not in (Path(bare["staging"]) / "PROMPT.md").read_text()


@pytest.mark.parametrize(("defaults", "warned"), [(["mcp__abacus__erase"], False), (["web", "mcp__web__web_fetch"], True)])
def test_a_default_the_room_cannot_give_is_skipped_and_the_worker_starts_but_named_it_refuses_the_start(setup, caplog, defaults,
                                                                                                         warned):
    """A default is never a refusal: one the room's allow has not is skipped, one whose server's table passes a credential
    skipped with one warning, and the worker starts with the rest. The same add-on named by the start is refused whole."""
    s = setup

    async def go():
        w = s.make({"r": Busy()})
        w.room_confs["r"] = {**COUNTING, "worker_addons": [*defaults, "mcp__abacus__sum"]}
        w.begin(asyncio.get_running_loop())
        out = await w.start("r", 'FAKE report="x"', account="one")
        await ended(s.ledger, out["id"])
        return out, await w.start("r", 'FAKE report="x"', account="one", addons=defaults[:1])
    with caplog.at_level("WARNING", logger="yerk.workers"):
        out, named = asyncio.run(go())
    assert out["ok"] and out["addons"] == ["mcp__abacus__sum"]
    assert list(json.loads((Path(out["staging"]) / "mcp.json").read_text())["mcpServers"]) == ["abacus"]   # no key handed over
    said = [r.getMessage() for r in caplog.records if "worker_addons" in r.getMessage()]
    assert len(said) == warned and all("passes a credential" in x for x in said)       # one warning for the server, not one a tool
    assert named["ok"] is False and ("passes a credential" if warned else "no add-on tool 'mcp__abacus__erase'") in named["error"]


def test_the_rooms_add_ons_and_the_ones_a_start_names_are_one_list_with_no_repeat(setup):
    s = setup

    async def go():
        w = s.make({"r": Busy()})
        w.room_confs["r"] = {**COUNTING, "allow": [*COUNTING["allow"], "mcp__slate__note"], "worker_addons": ["abacus", "mcp__abacus__sum", "slate"],
                             "mcp": {**COUNTING["mcp"], "slate": {"type": "stdio", "command": "slate"}}}
        w.begin(asyncio.get_running_loop())
        out = await w.start("r", 'FAKE report="x"', account="one", addons=["mcp__abacus__sum"])
        await ended(s.ledger, out["id"])
        return out
    out = asyncio.run(go())
    folder = Path(out["staging"])
    assert json.loads((folder / "worker.json").read_text())["addon_tools"] == out["addons"] == [
        "mcp__abacus__sum", "mcp__abacus__chart", "mcp__slate__note"]
    assert list(json.loads((folder / "mcp.json").read_text())["mcpServers"]) == ["abacus", "slate"]
    assert "- You have the room's own mcp__abacus__sum, mcp__abacus__chart, mcp__slate__note too." in (folder / "PROMPT.md").read_text()


def _hidden(s, monkeypatch, home="hidden"):
    """A launcher with [workers] home = `home` on the setup's state folder, the setup's folder standing as the home; the
    daemon's reach a checkout and the gate under it."""
    monkeypatch.setenv("HOME", str(s.tmp))
    (s.tmp / "yerk-ai" / ".git" / "objects").mkdir(parents=True, exist_ok=True)
    (s.tmp / "tools").mkdir(exist_ok=True)
    (s.tmp / "tools" / "pr-check").write_text("x")
    unit = Record(s.tmp, fence_of(s.tmp))
    unit.home, unit.reach = home, lambda: [(s.tmp / "yerk-ai", "repo"), (s.tmp / "tools" / "pr-check", "ro")]
    return unit


def test_a_hidden_units_question_and_its_properties_agree_on_every_path(setup, monkeypatch):
    # D7: for a hidden worker the question (unseen) reads the unit's own fence (walls): a path is hidden exactly when it is
    # under a TemporaryFileSystem or an InaccessiblePaths of the unit's and under no Bind line; outside the home only the
    # doors (/mnt, /run/user/<uid>, /run/WSL, /run/tailscale) change anything
    s = setup
    unit, rooms, keys = _hidden(s, monkeypatch), s.tmp / "rooms", s.tmp / "secrets"
    token, folder = keys / "claude-one.token", s.tmp / "workers" / "abc123"
    (s.tmp / "docs").mkdir(exist_ok=True)
    samples = [s.tmp, s.tmp / ".ssh" / "id_ed25519", s.tmp / "ledger.sqlite", s.tmp / "docs" / "GUIDE.md", folder, folder / "wt" / "x.py",
               rooms / "r" / "work" / "a.md", rooms / "r" / "settings.json", rooms / "ops" / "work", keys, token, keys / "claude-two.token",
               s.tmp / "yerk-ai" / "README.md", s.tmp / "yerk-ai" / ".git" / "config", s.tmp / "tools" / "pr-check", s.tmp / "tools" / "other",
               s.tmp / "workers" / "b11111" / "REPORT.md", ROOT, Path("/usr/bin/git"), Path("/tmp/x.log"),
               *(p / "x" for p, _ in workers.DOORS)]
    folder.mkdir(parents=True)
    for spec in ({"room": "r"}, {"room": "ops", "bind": "r"}, {"room": "r", "need": {"day": "2026-11-24", "n": 1}}):
        (folder / "worker.json").write_text(json.dumps(spec))
        env = {"YERK_TOKEN_FILE": str(token), "CLAUDE_CONFIG_DIR": str(s.tmp / "acct")}
        props = unit.sandbox(folder, env)[1::2]
        gone = [Path(re.sub(r"^\w+=-?|:ro$|:mode=0700$", "", p)) for p in props if p.startswith(("TemporaryFileSystem=", "InaccessiblePaths="))]
        back = [Path(p.split("=", 1)[1].lstrip("-")) for p in props if p.startswith(("BindPaths=", "BindReadOnlyPaths="))]
        by_props = [str(q) for q in samples if any(q.is_relative_to(g) for g in gone) and not any(q.is_relative_to(b) for b in back)]
        asked = unit.unseen(spec["room"], spec.get("bind", ""), str(token), [str(q) for q in samples], spec, folder, env["CLAUDE_CONFIG_DIR"])
        assert asked == by_props, spec
        assert str(s.tmp / ".ssh" / "id_ed25519") in asked and str(s.tmp / "docs" / "GUIDE.md") not in asked
        assert str(Path("/tmp/x.log")) not in asked and str(ROOT) not in asked or ROOT.is_relative_to(s.tmp)
    assert unit.unseen("r", "", str(token), [str(s.tmp / ".ssh")]) == []          # no folder given: today's fence, which hides no home


def test_a_need_brief_as_needs_writes_it_gets_no_note_in_a_hidden_unit_and_a_path_it_hides_does(setup, monkeypatch):
    # D7: what the three need briefs name (the checkout, a plug-in's, the gate) is bound back, so a need's worker is told of
    # nothing; a brief that names a hidden path gets today's note and today's answer
    from yerk_ai import needs
    s = setup
    unit = _hidden(s, monkeypatch, "needs")
    w = s.make({"r": Busy()}, launcher=unit)
    repo, gate = s.tmp / "yerk-ai", str(s.tmp / "tools" / "pr-check")
    r = {"day": "2026-11-24", "n": 7, "ability": "an ability", "pr": 5, "branch": "need-2026-11-24-7", "head": "abc1234",
         "asks": [{"room": "r", "at": "2026-11-24 05:00", "mid": "m1", "asked": "a", "tried": "t", "needed": "n"}]}
    for brief in (needs.brief(r, repo, gate), needs.read_brief(r, repo, gate), needs.fix_brief(r, repo, gate, "the suite fails")):
        out, prompt = started(w, "r", brief, more={"need": {"day": "2026-11-24", "n": 7}})
        assert out["ok"] and "unseen" not in out and "Not there for you" not in prompt, brief[:60]
    out, prompt = started(w, "r", f"Read {s.tmp / '.ssh' / 'id_ed25519'} and {s.tmp / 'ledger.sqlite'}.", more={"need": {"day": "2026-11-24", "n": 7}})
    hid = [str(s.tmp / ".ssh" / "id_ed25519"), str(s.tmp / "ledger.sqlite")]
    assert out["unseen"] == hid and f"- Not there for you, since your unit hides them: {', '.join(hid)}. The brief names them" in prompt
    out, prompt = started(w, "r", f"Read {s.tmp / '.ssh' / 'id_ed25519'}.")              # a room's worker under "needs": open
    assert "unseen" not in out and "Not there for you" not in prompt


def test_a_hidden_workers_deletes_go_to_its_own_trash_and_it_keeps_its_memory_tools(setup, monkeypatch):
    # D6: the owner's Trash and the state folder are not there, so trash-cli's `trash x` exits 74 (a throwaway unit of
    # 2026-11-10); with --trash-dir in the worker's own folder it exits 0. D5: the memory tools stay on, through the daemon
    from yerk_ai.lifted import shellguard
    s = setup
    folder = s.tmp / "workers" / "abc123"
    gate = {"trash": ["trash"], "private": "/p", "external": {}}
    walled = workers.walled_trash(gate, folder)
    assert walled == {**gate, "trash": ["trash", "--trash-dir", str(folder / ".trash")]} and gate["trash"] == ["trash"]
    assert workers.walled_trash({"trash": ["gio", "trash"]}, folder) == {"trash": ["gio", "trash"]}   # not trash-cli: as it was
    assert workers.walled_trash({}, folder) == {}
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("x")
    new = shellguard.delete_rewrite("rm notes.md", str(folder), walled["trash"])
    assert new and f"--trash-dir {folder / '.trash'}" in str(new)
    for home, walled_ in (("hidden", True), ("open", False)):
        w = s.make({"r": Busy()}, launcher=_hidden(s, monkeypatch, home), yerk_tools=["recall", "read_memory"])
        w.gate = dict(gate)
        out, _ = started(w, "r", "Do it.")
        spec = json.loads((Path(out["staging"]) / "worker.json").read_text())
        assert ("--trash-dir" in spec["gate"]["trash"]) is walled_ and spec["yerk_tools"] == ["recall", "read_memory"]
        assert json.loads(Path(spec["mcp_config"]).read_text())["mcpServers"]["yerk"]["args"][-1] == "recall,read_memory"
