"""The watcher job kind (DESIGN 7): a long-running command whose matching output lines are events.
It primes on its first start, a line seen before is never an event again (so a restart wakes nobody
twice), lines are gathered into one wake, and an event is handled only after its turn ends clean:
a failed or cut turn is replayed once, never twice. Every event is a ledger row."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from yerk_ai import jobs
from yerk_ai import words as W
from yerk_ai.jobs import JobError, Watcher, make_job
from yerk_ai.ledger import Ledger
from yerk_ai.room import Inbound

from test_jobs import FakeSystemd as FakeUnits, Rec
from test_workers import setup  # noqa: F401

WATCH = {"kind": "watcher", "command": "true", "prefix": "tick ", "room": "r"}


@pytest.mark.parametrize("raw, why", [
    ({**WATCH, "every": "1m"}, "runs all the time"),
    ({k: v for k, v in WATCH.items() if k != "prefix"}, "exactly one of `prefix`"),
    ({**WATCH, "regex": "x"}, "exactly one of `prefix`"),
    ({**{k: v for k, v in WATCH.items() if k != "prefix"}, "regex": "("}, "does not compile"),
    ({**WATCH, "wake": "phone"}, '"room" or "worker"'),
    ({**WATCH, "account": "one"}, "wakes a worker"),
    ({**WATCH, "lines": 0}, "at least 1"),
    ({"kind": "script", "every": "1h", "room": "r", "command": "x", "prefix": "a"}, "for watchers"),
])
def test_a_watcher_that_cannot_run_is_named_with_why(raw, why):
    with pytest.raises(JobError, match=why):
        make_job("w", raw)


def test_a_watcher_is_one_always_on_service_with_restart_and_no_timer(tmp_path):
    job = make_job("w", WATCH)
    env = jobs.Env("/py", tmp_path / "repo", tmp_path / "p", tmp_path / "s")
    timer, service = jobs.unit_texts(job, env)
    assert timer == "" and "Type=simple" in service and "Restart=always" in service and "RestartSec=10" in service
    assert "KillMode=control-group" in service and "WantedBy=default.target" in service
    assert "yerk_ai.jobs watch w" in service and "OnCalendar" not in service and "TimeoutStartSec" not in service
    units = FakeUnits(tmp_path / "units")
    path = tmp_path / "jobs.toml"
    path.write_text('[jobs.w]\nkind = "watcher"\ncommand = "true"\nprefix = "tick "\nroom = "r"\n')
    p, back = jobs.sync(path, units, env)
    assert p.added == ["w"] and ("enable", "yerk-job-w.service") in units.calls
    assert units.read("yerk-job-w.timer") is None and units.read("yerk-job-w.service") == service
    assert back["w"]["watcher"] and back["w"]["active"] == "active"
    assert jobs.read_lines(back, p.jobs)[0].startswith("w: watching; watcher on prefix 'tick ', wakes the room")
    assert jobs.status_lines(path, units) == [f"{W.DONE} w: watching"]
    units.calls.clear()
    p2, _ = jobs.sync(path, units, env)                  # idempotent
    assert p2.unchanged == ["w"] and units.calls == []
    path.write_text("")
    p3, _ = jobs.sync(path, units, env)                  # removed: the service is disabled and goes
    assert p3.removed == ["w"] and ("disable", "yerk-job-w.service") in units.calls
    assert units.read("yerk-job-w.service") is None and jobs.status_lines(path, units) == []


class Script:
    """A command's stdout as a list of (seconds after start, line), in real time."""

    def __init__(self, steps):
        self.steps, self.returncode = steps, 0

    def __call__(self, argv, **kw):
        self.argv, self.env = argv, kw.get("env", {})
        start, steps = time.time(), self.steps

        class Out:
            def __iter__(s):
                for at, line in steps:
                    time.sleep(max(0.0, start + at - time.time()))
                    yield line + "\n"
        self.stdout = Out()
        return self

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode


def run_watcher(tmp_path, steps, raw=None):
    private, state = tmp_path / "p", tmp_path / "s"
    private.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    body = "".join(f"{k} = {json.dumps(v)}\n" for k, v in (raw or {**WATCH, "batch": 0.3, "prime": 0.3}).items())
    (private / "jobs.toml").write_text(f"[jobs.w]\n{body}")
    told, said = [], []
    cmd = Script(steps)
    w = Watcher("w", private, state, handoff=told.append, popen=cmd, diag=said.append)
    rc = w.once()
    return w, rc, told, said, cmd


def test_the_first_start_primes_matches_are_events_and_other_lines_go_to_the_log(tmp_path):
    w, rc, told, said, cmd = run_watcher(tmp_path, [(0, "tick 1"), (0.05, "a diagnostic"), (0.8, "tick 2"),
                                                    (0.85, "tick 3"), (1.6, "tick 4")])
    rows = w.ledger.watch_events("w")
    assert [(r["state"], r["lines"]) for r in rows] == [("primed", "tick 1"), ("pending", "tick 2\ntick 3"),
                                                        ("pending", "tick 4")]
    assert [x["job"]["id"] for x in told] == [rows[1]["id"], rows[2]["id"]]           # told after the row exists
    assert all(x["job"]["action"] == "event" and x["job"]["name"] == "w" for x in told)
    assert "a diagnostic" in said and any(x.startswith("primed: 1 line") for x in said)
    assert cmd.argv[:2] == ["/bin/sh", "-c"] and cmd.env["YERK_JOB"] == "w" and "CLAUDE_CONFIG_DIR" not in cmd.env
    # a restart: the command prints old lines again; none is an event, nothing primes again
    w2, _, told2, said2, _ = run_watcher(tmp_path, [(0, "tick 3"), (0.05, "tick 4"), (0.6, "tick 5")])
    rows2 = w2.ledger.watch_events("w")
    assert [(r["state"], r["lines"]) for r in rows2[3:]] == [("pending", "tick 5")] and len(told2) == 1
    assert "seen before, not an event: tick 3" in said2 and not any(x.startswith("primed") for x in said2)


def test_a_regex_key_names_the_event_and_lines_caps_one_wake(tmp_path):
    raw = {"kind": "watcher", "command": "x", "regex": r"^job (?P<key>\d+) ", "room": "r", "batch": 5, "lines": 2,
           "prime": 0}
    w, _, told, _, _ = run_watcher(tmp_path, [(0.1, "job 7 failed"), (0.15, "job 7 failed again"), (0.2, "job 8 failed"),
                                              (0.25, "job 9 failed"), (0.3, "job 10 failed")], raw=raw)
    rows = [r for r in w.ledger.watch_events("w") if r["state"] != "primed"]
    assert [r["lines"] for r in rows] == ["job 7 failed\njob 8 failed", "job 9 failed\njob 10 failed"]
    assert len(told) == 2                                # the same key twice is one event line


class RoomStub:
    def __init__(self):
        self.asked: list[Inbound] = []


def test_an_event_wakes_one_turn_and_is_handled_only_after_it_ends_clean_replayed_once(setup, tmp_path):
    s = setup
    rec = Rec()
    private, state = tmp_path / "priv", tmp_path / "st"
    private.mkdir()
    state.mkdir()
    (private / "jobs.toml").write_text('[jobs.w]\nkind = "watcher"\ncommand = "x"\nprefix = "tick "\nroom = "r"\n'
                                       'message = "Say it."\n')

    async def go():
        room = s.room()
        w = s.make({"r": room}, channel=rec)
        w.begin(asyncio.get_running_loop())
        j = jobs.Jobs(private, state, s.ledger, {}, {"r": room}, {"r": rec}, {"r": {}}, {}, w, FakeUnits(tmp_path / "u"))
        asked: list[Inbound] = []
        w.ask = lambda name, msg: asked.append(msg)       # the turn itself is room.py's; here: what is asked
        ok = s.ledger.watch_add(job="w", room="r", wake="room", state="pending", lines="tick 5", keys="[]",
                                received=time.time(), attempts=0)
        bad = s.ledger.watch_add(job="w", room="r", wake="room", state="pending", lines="tick 6", keys="[]",
                                 received=time.time(), attempts=0)
        await j.event(ok)
        await j.event(ok)                                 # told twice: still one turn
        assert len(asked) == 1 and asked[0].alone and asked[0].sender == "yerk" and asked[0].id == f"watch-{ok}-1"
        assert "tick 5" in asked[0].text and "Say it." in asked[0].text and s.ledger.watch_get(ok)["state"] == "waking"
        await j.ended(ok, "answered", ["m1"])
        assert (s.ledger.watch_get(ok)["state"], s.ledger.watch_get(ok)["reply_ids"]) == ("handled", "m1")
        await j.event(bad)
        await j.ended(bad, "failed", [])                  # the turn failed: one replay
        assert asked[-1].id == f"watch-{bad}-2" and s.ledger.watch_get(bad)["attempts"] == 2
        await j.ended(bad, "failed", [])                  # failed again: never a third time
        assert s.ledger.watch_get(bad)["state"] == "failed" and len(asked) == 3
        assert rec.notified == [("r", f"{W.NOT_DONE} job w failed: it ended with an error")]
        # a restart: a turn cut while waking replays once; one already replayed fails; a delivered one is handled
        cut = s.ledger.watch_add(job="w", room="r", wake="room", state="waking", lines="tick 7", keys="[]",
                                 received=time.time(), attempts=1, inbound="watch-x-1")
        twice = s.ledger.watch_add(job="w", room="r", wake="room", state="waking", lines="tick 8", keys="[]",
                                   received=time.time(), attempts=2, inbound="watch-y-2")
        j2 = jobs.Jobs(private, state, s.ledger, {}, {"r": room}, {"r": rec}, {"r": {}}, {}, w,
                       FakeUnits(tmp_path / "u"))
        await j2.events_open(restarted=True)
        assert asked[-1].id == f"watch-{cut}-2" and s.ledger.watch_get(cut)["state"] == "waking"
        assert s.ledger.watch_get(twice)["state"] == "failed" and len(asked) == 4
    asyncio.run(go())


def test_every_event_is_a_ledger_row_with_its_lines_and_state(tmp_path):
    led = Ledger(tmp_path / "l.sqlite")
    eid = led.watch_add(job="w", room="r", wake="room", state="pending", lines="a\nb", keys='["k1","k2"]',
                        received=1.0, attempts=0)
    led.watch_set(eid, state="handled", ended=2.0)
    (row,) = led.watch_events("w", states=("handled",))
    assert (row["lines"], json.loads(row["keys"]), row["ended"]) == ("a\nb", ["k1", "k2"], 2.0)
    with pytest.raises(ValueError):
        led.watch_set(eid, colour="red")
    assert Path(tmp_path / "l.sqlite").is_file()
