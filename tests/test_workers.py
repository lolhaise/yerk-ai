"""Checks a worker's life on a fake launcher in place of systemd: start, its progress card from
TASKS.md at most once a second, completion and the relay of its report or the skip, death, timeout
against the ledger, and the environment a worker gets."""
import asyncio
import json
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
from yerk_ai import workers
from yerk_ai import words as W
from yerk_ai.accounts import Account, Cooldowns, store_token
from yerk_ai.config import table
from yerk_ai.engine import RoomSpec
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.room import Inbound, Room, fit

FAKE = Path(__file__).with_name("fake_worker_claude.py")
ROOT = Path(__file__).resolve().parents[1]
TOKEN_ONE, TOKEN_TWO = "sk-" + "ant-oat01-" + "One" * 30, "sk-" + "ant-oat01-" + "Two" * 30
MANAGER_ENV = {"HOME": "/nonexistent-home", "USER": "someone", "LANG": "C.UTF-8",   # what a user manager adds,
               "SSH_AUTH_SOCK": "/tmp/planted-agent.sock", "GPG_AGENT_INFO": "planted"}  # two the worker must not get


class Units:
    """systemd-run's part, as the code relies on it: argv runs with exactly the given environment on
    top of the manager's, in its folder, output appended to the files; the cap kills it (result
    timeout); a stop kills its process group (result success, killed TERM, as systemd reports a
    stopped service); ExecStopPost runs `post` with SERVICE_RESULT, EXIT_CODE and EXIT_STATUS."""

    def __init__(self):
        self.calls, self.procs, self.stopped = [], {}, set()

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
    """A room that is never idle: the relay is skipped."""

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

    def make(rooms, channel=None, launcher=None):
        conf = workers.Settings(root=tmp_path / "workers", idle_seconds=60.0)
        return workers.Workers(ledger, conf, accounts, Cooldowns(None), rooms,
                               {n: channel or chan for n in rooms}, {n: {"model": "m"} for n in rooms},
                               launcher=launcher or units)
    return NS(ledger=ledger, accounts=accounts, chan=chan, units=units, room=room, make=make, tmp=tmp_path)


async def ended(ledger, wid, relay=True, timeout=20.0):
    """The worker's row once it has finished (and its relay is settled, unless relay=False)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = ledger.worker_get(wid)
        if r and r["finished"] and (not relay or r["relay"] in ("relayed", "skipped", "failed")):
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
def test_completion_when_the_room_is_not_idle_skips_the_relay_and_ledgers_why(setup, why):
    s = setup
    room = s.room() if why == "human" else Busy()
    if why == "human":
        s.ledger.inbound_add("h1", "r", "c", "owner", 2)
        room.submit(Inbound("h1", "owner", "hi"))
        while room.busy:
            time.sleep(0.05)

    async def go():
        w = s.make({"r": room})
        w.begin(asyncio.get_running_loop())
        out = await w.start("r", 'FAKE report="Short one"')
        return await ended(s.ledger, out["id"])
    row = asyncio.run(go())
    assert row["exit"] == "done" and row["relay"] == "skipped"
    assert row["relay_note"].startswith("a human message" if why == "human" else "a turn was running")
    assert "Short one" in s.chan.settled[0][1] and f"yerk worker report {row['id']}" in s.chan.settled[0][1]   # the card
    assert row["report"] not in s.chan.settled[0][1]                                                      # carries it
    assert len(s.chan.replies) == (1 if why == "human" else 0)                              # only the human's answer


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
        return [await ended(s.ledger, x["id"]) for x in (died, slow, held)]
    died, slow, held = asyncio.run(go())
    assert (died["exit"], died["exit_detail"], died["result"]) == ("died", "exit-code, exited 3", "half way")
    assert (slow["exit"], slow["exit_detail"]) == ("timed out", "timeout, killed TERM")
    assert held["exit"] == "stopped" and held["stop_requested"] and held["exit_detail"] == "success, killed TERM"
    heads = {k.removeprefix("worker:"): t.splitlines()[0] for k, t, _, _ in s.chan.settled}
    assert heads[died["id"]] == f"{W.NOT_DONE} worker died: it ended with an error"
    assert heads[slow["id"]].startswith(f"{W.NOT_DONE} worker stopped: timed out at ")
    assert heads[held["id"]] == f"{W.NOT_DONE} worker stopped by the owner"
    assert all(W.width(x) <= W.PHONE for _, t, _, _ in s.chan.settled for x in t.splitlines())
    assert all(r["relay"] == "skipped" for r in (died, slow, held))
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
        gone = s.tmp / "workers" / "0ld0ne"           # a row whose unit vanished with no exit record
        lost = dict(s.ledger.worker_get(out["id"]), id="0ld0ne", unit="yerk-worker-0ld0ne", room="q", card=None,
                    staging=str(gone), report=str(gone / "REPORT.md"), tasks=str(gone / "TASKS.md"))
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
    assert room.submit(Inbound("h2", "owner", "and?")) == "folded"
    while room.busy:
        time.sleep(0.05)
    (text, inbound), = s.chan.replies
    assert inbound == ["relay-ccc333", "h2"] and text == "echo: SLOW [worker ccc333 finished] done + and?"
    assert seen == [("answered", ["m1"])] and s.ledger.inbound_get("h2")["state"] == "answered"
    assert room.last_human is not None and s.ledger.last_received("r") == s.ledger.inbound_get("h2")["received"]
