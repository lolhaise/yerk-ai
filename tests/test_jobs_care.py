"""Checks how scheduled jobs are stopped, alerted on and reviewed, on the fakes of the jobs tests. The cases: a job's
run in progress stopped by `yerk jobs stop` or the schedule tool (J1), one alert per unchanged failure, edited in
place (J2), the doctor's review of the jobs (J3), and a held finding's folder kept by the sweep (J4)."""
import asyncio
import hashlib
import io
import time
from contextlib import redirect_stdout

from yerk_ai import cli, jobs
from yerk_ai import words as W

from test_jobs import ENV, FakeSystemd, Rec, manager, write_jobs
from test_workers import ended, setup  # noqa: F401  (the worker fixture: fake units, fake claude, a room)

SCRIPT = '[jobs.{n}]\nkind = "script"\n{key} = "{when}"\ntz = "UTC"\nroom = "r"\ncommand = "sleep 600"\n'


class Units(FakeSystemd):
    """The jobs tests' systemd and its stop: the service is down once it returns."""

    def stop(self, unit):
        self.calls.append(("stop", unit))
        self.active.discard(unit)
        return ""


class Edits(Rec):
    """The machine room's channel with edit_post, as discord.py has it; `fail`: every edit refused (a deleted post)."""

    def __init__(self, fail=False):
        super().__init__()
        self.edits, self.fail = [], fail

    async def edit_post(self, room, message_id, text):
        if self.fail:
            raise RuntimeError("404 Not Found (error code: 10008): Unknown Message")
        self.edits.append((room, message_id, text))


def care(setup, tmp_path, table, conf=None, ops=None, tz="UTC"):
    """A manager with a room and the machine room, the jobs file synced, on a ledger at the runner's path."""
    s = setup
    rec, ops = Rec(), ops or Rec()
    j, w, _, priv, state = manager(setup, tmp_path, {"r": s.room(), "ops": s.room("ops")}, {"r": rec, "ops": ops},
                                   conf=conf or {}, room_confs={"r": {"model": "m", "timezone": "UTC"},
                                                                "ops": {"model": "m", "timezone": tz, "role": "machine"}})
    j.units = sd = Units(tmp_path / "units")
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    write_jobs(priv / "jobs.toml", table)
    jobs.sync(priv / "jobs.toml", sd, ENV)
    return j, w, sd, priv, state, rec, ops


def started(ledger, job, ago, kind="script"):
    """The row jobs.run writes as a run starts, and nothing after it (the runner is killed with its unit)."""
    now = time.time()
    return ledger.job_run_add(job=job, kind=kind, room="r", unit=f"{jobs.UNIT}{job}", planned=now - ago, fired=now - ago)


def stop(j, name, caller=""):
    return asyncio.run(j.handle(caller, {"action": "stop", "name": name}))


# J1: stop a running job
def test_a_running_script_job_is_stopped_its_row_closed_as_stopped_one_line_said_and_its_timer_left(setup, tmp_path):
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path, SCRIPT.format(n="nightly", key="schedule", when="*:00"))
    rid = started(s.ledger, "nightly", 800)                   # past its unit's limit (10 min and 2): what reattach closes
    sd.active.add("yerk-job-nightly.service")
    before = list(sd.calls)
    out = stop(j, "nightly")
    assert out["ok"] and out["line"].startswith(f"stopped nightly's script run {rid} after 13 min; next fire ")
    assert sd.calls[len(before):] == [("stop", "yerk-job-nightly.service")]                # the timer is never touched
    assert sd.enabled == {"yerk-job-nightly.timer": True} and sd.read("yerk-job-nightly.timer")
    r = s.ledger.job_run_get(rid)
    assert (r["exit"], r["result"], r["delivery"], r["note"]) == ("stopped", "stopped", "none", "stopped from the command line")
    assert r["ended"] >= r["fired"]
    assert ops.notified == [("ops", W.job_line("nightly", "stopped"))] and "failed" not in ops.notified[0][1]
    asyncio.run(j.reattach())                                     # an ended row is never closed again, nothing alerts
    assert s.ledger.job_run_get(rid) == {**r, "message_ids": "n1"} and len(ops.notified) == 1 and rec.notified == []
    assert "nightly" in jobs.load(priv / "jobs.toml")[0]


def test_a_one_shot_stopped_leaves_the_file_and_its_units_go(setup, tmp_path):
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path, SCRIPT.format(n="once", key="at", when="2099-01-01 09:00")
                                           + SCRIPT.format(n="stay", key="schedule", when="*:00"))
    started(s.ledger, "once", 30)
    sd.active.add("yerk-job-once.service")
    out = stop(j, "once", "r")
    assert out["ok"] and out["line"].endswith("next fire none: a one-shot, out of the jobs file, its units gone")
    assert list(jobs.load(priv / "jobs.toml")[0]) == ["stay"]
    assert sd.read("yerk-job-once.timer") is None and sd.read("yerk-job-once.service") is None
    assert s.ledger.job_runs(job="once")[0]["note"] == "stopped from room r"


def test_a_row_the_runner_ended_first_is_left_as_it_is(setup, tmp_path):
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path, SCRIPT.format(n="nightly", key="schedule", when="*:00"))
    rid = started(s.ledger, "nightly", 60)
    s.ledger.job_run_set(rid, ended=time.time() - 1, exit="exited 0", result="output", delivery="delivered", message_ids="s9")
    kept = s.ledger.job_run_get(rid)
    sd.active.add("yerk-job-nightly.service")                      # its hand-off still going when the stop came
    assert stop(j, "nightly")["ok"]
    assert s.ledger.job_run_get(rid) == kept and ops.notified == []          # its own end says it


def test_a_stop_with_nothing_running_says_when_it_last_ran_and_an_unknown_name_gets_removes_refusal(setup, tmp_path):
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path, SCRIPT.format(n="nightly", key="schedule", when="*:00")
                                           + '[jobs.nudge]\nkind = "note"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\nmessage = "x"\n')
    rid = started(s.ledger, "nightly", 60)
    s.ledger.job_run_set(rid, ended=time.time(), exit="exited 0", result="nothing")
    out = stop(j, "nightly")
    assert not out["ok"] and out["error"].startswith("nothing of nightly is running; it last ran ") and ", next " in out["error"]
    assert stop(j, "nudge")["error"].startswith("nothing of nudge is running; it last ran never, next ")
    assert ("stop", "yerk-job-nightly.service") not in sd.calls and ops.notified == []
    gone = stop(j, "nope")
    assert gone == {"ok": False, "error": jobs.no_job("nope", ["nightly", "nudge"])}
    assert gone["error"] == jobs.schedule_remove(priv / "jobs.toml", sd, ENV, "nope")["error"]


def test_an_agent_jobs_worker_is_stopped_and_its_run_reads_stopped_with_one_line(setup, tmp_path):
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path,
                                           '[jobs.think]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\nmessage = "FAKE sleep=30"\n')

    async def go():
        loop = asyncio.get_running_loop()
        w.begin(loop)
        handoff = lambda p: asyncio.run_coroutine_threadsafe(j.handle("", p["job"]), loop).result(30)  # noqa: E731
        assert await asyncio.to_thread(jobs.run, "think", priv, state, sd, handoff) == 0
        run = s.ledger.job_runs(job="think")[0]
        await asyncio.sleep(0.3)
        out = await j.handle("r", {"action": "stop", "name": "think"})
        row = await ended(s.ledger, run["worker"], relay=False)
        await asyncio.sleep(0.2)
        return out, run, row
    out, run, row = asyncio.run(go())
    assert out["ok"] and out["line"].startswith(f"stopped think's worker {row['id']} after ") and row["exit"] == "stopped"
    r = s.ledger.job_run_get(run["id"])
    assert (r["exit"], r["result"], r["delivery"]) == ("stopped", "stopped", "none")
    assert ops.notified == [("ops", W.job_line("think", "stopped"))]          # today's path says it, once
    asyncio.run(j.reattach())
    assert len(ops.notified) == 1


def test_systemds_stop_waits_out_the_unit_and_clears_the_failed_state_a_killed_run_leaves(monkeypatch, tmp_path):
    """A oneshot ended by `systemctl stop` reads failed (Result=signal, measured on the box 2026-11-23): the state goes."""
    import subprocess
    sent = []

    def systemctl(*args, timeout=60, check=""):
        sent.append((args, timeout))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(jobs, "systemctl", systemctl)
    assert jobs.Systemd(tmp_path, tmp_path).stop("yerk-job-x.service") == ""
    assert sent == [(("stop", "yerk-job-x.service"), jobs.STOP_WAIT), (("reset-failed", "yerk-job-x.service"), 60)]
    monkeypatch.setattr(jobs, "systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no such unit"))
    assert jobs.Systemd(tmp_path, tmp_path).stop("yerk-job-x.service").startswith("systemctl --user stop yerk-job-x.service exited 1")


def test_a_run_that_ends_by_itself_while_the_unit_stops_keeps_its_own_end_and_says_itself(setup, tmp_path):
    """The script finishes in the second the stop goes out: the runner's row and hand-off win, and no stopped line is posted."""
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path, SCRIPT.format(n="nightly", key="schedule", when="*:00"))
    rid = started(s.ledger, "nightly", 60)
    sd.active.add("yerk-job-nightly.service")
    down = sd.stop

    def stops(unit):
        s.ledger.job_run_set(rid, ended=time.time(), exit="exited 0", chars=5)
        asyncio.run(j.result({"name": "nightly", "run": rid, "exit": 0, "timed_out": False, "output": "done!", "stderr_tail": ""}))
        return down(unit)
    sd.stop = stops
    assert stop(j, "nightly")["ok"]
    r = s.ledger.job_run_get(rid)
    assert (r["exit"], r["result"], r["delivery"]) == ("exited 0", "output", "delivered") and ops.notified == [] and rec.sent


def test_a_stop_before_the_run_is_on_record_names_no_older_run(setup, tmp_path):
    """The runner is up and its row not written yet (its first second): the last run, a day old and done, is not the one stopped."""
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path, SCRIPT.format(n="nightly", key="schedule", when="*:00"))
    old = started(s.ledger, "nightly", 86400)
    s.ledger.job_run_set(old, ended=time.time() - 86000, exit="exited 0", result="nothing", delivery="silent (no output)")
    sd.active.add("yerk-job-nightly.service")
    out = stop(j, "nightly")
    assert out["ok"] and (out["run"], out["closed"]) == (0, []) and out["line"].startswith("stopped nightly's script, its run not on record yet after ")
    assert "24 h" not in out["line"] and f"run {old}" not in out["line"] and s.ledger.job_run_get(old)["result"] == "nothing"


def test_a_stop_ends_the_jobs_running_worker_though_a_newer_run_did_not_start(setup, tmp_path):
    """The newest run's worker is not the only one going: an older run's worker the account was busy with is what the person means."""
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path,
                                           '[jobs.think]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\nmessage = "FAKE sleep=30"\n')

    async def go():
        loop = asyncio.get_running_loop()
        w.begin(loop)
        handoff = lambda p: asyncio.run_coroutine_threadsafe(j.handle("", p["job"]), loop).result(30)  # noqa: E731
        assert await asyncio.to_thread(jobs.run, "think", priv, state, sd, handoff) == 0
        run = s.ledger.job_runs(job="think")[0]
        await asyncio.sleep(0.3)
        now = time.time()
        s.ledger.job_run_add(job="think", kind="agent", room="r", unit="yerk-job-think", planned=now, fired=now, ended=now,
                             exit="not run", result="did not start", delivery="alert", note="its account is busy")
        out = await j.handle("r", {"action": "stop", "name": "think"})
        return out, run, await ended(s.ledger, run["worker"], relay=False)
    out, run, row = asyncio.run(go())
    assert out["ok"] and out["run"] == run["id"] and out["worker"] == row["id"] and row["exit"] == "stopped"


def test_the_command_line_stops_a_script_with_the_daemon_down_and_an_agent_job_not(setup, tmp_path, monkeypatch):
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path, SCRIPT.format(n="nightly", key="schedule", when="*:00")
                                           + '[jobs.think]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\nmessage = "x"\n')
    rid = started(s.ledger, "nightly", 200)
    sd.active.add("yerk-job-nightly.service")
    monkeypatch.setattr(jobs, "Systemd", lambda: sd)
    buf = io.StringIO()
    with redirect_stdout(buf):                                                     # no yerkd.json: no daemon
        rc = cli.main(["jobs", "stop", "nightly", "--private", str(priv), "--state", str(state)])
        rc2 = cli.main(["jobs", "stop", "think", "--private", str(priv), "--state", str(state)])
        rc3 = cli.main(["jobs", "stop", "nightly", "--private", str(priv), "--state", str(state)])
    said = buf.getvalue().splitlines()
    assert (rc, rc2, rc3) == (0, 1, 1)
    assert said[0].startswith(f"stopped nightly's script run {rid} after 3 min; next fire ") and said[0].endswith(W.JOB_STOP_UNTOLD)
    assert said[1] == W.JOB_STOP_DOWN.format(name="think") and said[2].startswith("nothing of nightly is running")
    assert s.ledger.job_run_get(rid)["result"] == "stopped" and ops.notified == []


def test_the_tools_stop_works_in_an_owner_room_and_is_refused_elsewhere_with_todays_line(setup, tmp_path):
    s = setup
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path, SCRIPT.format(n="nightly", key="schedule", when="*:00"))
    rid = started(s.ledger, "nightly", 60)
    sd.active.add("yerk-job-nightly.service")
    out = asyncio.run(j.notes("guests", {"action": "stop", "name": "nightly"}))
    assert out == {"ok": False, "line": W.JOBS_OWNER_ONLY} and sd.active and s.ledger.job_run_get(rid)["ended"] is None
    from yerk_ai.daemon import Yerkd
    d = Yerkd.__new__(Yerkd)
    d.jobs, d.owner, d.owner_only = j, lambda room: room == "r", lambda room, named: None
    assert asyncio.run(d.tool_schedule("guests", {"action": "stop", "name": "nightly"}))["line"] == W.JOBS_OWNER_ONLY
    assert asyncio.run(d.tool_schedule("r", {"action": "stop", "name": "nightly"}))["ok"]
    assert s.ledger.job_run_get(rid)["note"] == "stopped from room r" and not sd.active


# J2: one alert per unchanged failure
def ended_at(j, name, code, ago, timed_out=False):
    """A script run as the runner leaves it (its row and its end, `ago` seconds back), handed to the daemon's result."""
    rid = started(j.ledger, name, ago + 5)
    j.ledger.job_run_set(rid, ended=time.time() - ago, exit="timed out" if timed_out else f"exited {code}")
    asyncio.run(j.handle("", {"action": "result", "name": name, "run": rid, "exit": code, "timed_out": timed_out,
                              "output": "", "stderr_tail": ""}))
    return j.ledger.job_run_get(rid)


def clock(row, tz="Asia/Tokyo"):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(row["ended"], ZoneInfo(tz)).strftime("%H:%M")


FLAKY = SCRIPT.format(n="flaky", key="schedule", when="*:0/10") + SCRIPT.format(n="other", key="schedule", when="*:0/10")
FAILED = f"{W.NOT_DONE} job flaky failed: it ended with an error"


def test_equal_failures_are_one_post_edited_with_the_count_since_the_first_in_the_machine_rooms_clock(setup, tmp_path):
    ops = Edits()
    j, *_ = care(setup, tmp_path, FLAKY, ops=ops, tz="Asia/Tokyo")
    one = ended_at(j, "flaky", 3, 1800)
    theirs = ended_at(j, "other", 3, 1700)                  # another job's equal failures count for it alone
    two = ended_at(j, "flaky", 3, 1200)
    assert ops.notified == [("ops", FAILED), ("ops", FAILED.replace("flaky", "other"))]
    assert ops.edits == [("ops", "n1", f"{FAILED}\n2 times since {clock(one)}, last {clock(two)}")]
    assert (two["message_ids"], j.ledger.job_run_get(two["id"])["message_ids"], theirs["message_ids"]) == ("n1", "n1", "n2")
    three = ended_at(j, "flaky", 3, 600)
    assert ops.edits[-1] == ("ops", "n1", f"{FAILED}\n3 times since {clock(one)}, last {clock(three)}")
    assert len(ops.notified) == 2 and three["delivery"] == "alert" and three["result"] == "exit 3"
    ended_at(j, "other", 3, 500)
    assert ops.edits[-1] == ("ops", "n2", f"{FAILED.replace('flaky', 'other')}\n2 times since {clock(theirs)}, last "
                                          f"{clock(j.ledger.job_runs(job='other')[0])}")
    ended_at(j, "flaky", 3, 400, timed_out=True)            # a time out is another failure: posted anew
    assert ops.notified[-1] == ("ops", W.job_line("flaky", "timed out", jobs.DEFAULT_CAP["script"] * 60)) and len(ops.notified) == 3


def test_a_different_exit_posts_anew_a_good_run_says_ok_again_once_and_the_next_failure_posts_anew(setup, tmp_path):
    ops = Edits()
    j, *_ = care(setup, tmp_path, FLAKY, ops=ops)
    ended_at(j, "flaky", 3, 1800)
    four = ended_at(j, "flaky", 4, 1200)
    assert [t for _, t in ops.notified] == [FAILED, FAILED] and ops.edits == [] and four["message_ids"] == "n2"
    good = ended_at(j, "flaky", 0, 600)
    assert ops.edits == [("ops", "n2", f"{W.DONE} job flaky is ok again at {clock(good, 'UTC')}; it failed at {clock(four, 'UTC')}")]
    assert (good["result"], good["delivery"]) == ("nothing", "silent (no output)")
    ended_at(j, "flaky", 0, 500)
    assert len(ops.edits) == 1                                                       # once
    ended_at(j, "flaky", 4, 400)
    assert len(ops.notified) == 3 and len(ops.edits) == 1                          # after a good run: anew


def test_ok_again_counts_the_failures_it_ends(setup, tmp_path):
    ops = Edits()
    j, *_ = care(setup, tmp_path, FLAKY, ops=ops)
    one, _, three = (ended_at(j, "flaky", 3, ago) for ago in (1800, 1200, 900))
    good = ended_at(j, "flaky", 0, 600)
    assert ops.edits[-1] == ("ops", "n1", f"{W.DONE} job flaky is ok again at {clock(good, 'UTC')}; it failed 3 times since "
                                          f"{clock(one, 'UTC')}, last {clock(three, 'UTC')}")


def test_an_edit_that_fails_or_a_channel_with_none_posts_anew(setup, tmp_path):
    ops = Edits(fail=True)
    j, *_ = care(setup, tmp_path, FLAKY, ops=ops)
    ended_at(j, "flaky", 3, 1800)
    two = ended_at(j, "flaky", 3, 1200)
    assert [t for _, t in ops.notified] == [FAILED, FAILED] and two["message_ids"] == "n2"
    three = ended_at(j, "flaky", 3, 600)                    # counted from the post it can edit: none
    assert len(ops.notified) == 3 and three["message_ids"] == "n3"


def test_a_channel_with_no_edit_posts_as_today(setup, tmp_path):
    j, w, sd, priv, state, rec, ops = care(setup, tmp_path, FLAKY)
    ended_at(j, "flaky", 3, 1800)
    ended_at(j, "flaky", 3, 1200)
    assert [t for _, t in ops.notified] == [FAILED, FAILED]


def test_a_restart_between_the_runs_changes_nothing(setup, tmp_path):
    ops = Edits()
    j, w, sd, priv, state, rec, _ = care(setup, tmp_path, FLAKY, ops=ops)
    one = ended_at(j, "flaky", 3, 1800)
    again = jobs.Jobs(priv, state, j.ledger, {}, j.rooms, j.channels, j.room_confs, {}, w, units=sd)     # a new process
    two = ended_at(again, "flaky", 3, 1200)
    assert ops.edits == [("ops", "n1", f"{FAILED}\n2 times since {clock(one, 'UTC')}, last {clock(two, 'UTC')}")]


def test_a_stopped_run_between_two_failures_is_skipped(setup, tmp_path):
    ops = Edits()
    j, w, sd, priv, state, rec, _ = care(setup, tmp_path, FLAKY, ops=ops)
    one = ended_at(j, "flaky", 3, 1800)
    started(j.ledger, "flaky", 1500)
    sd.active.add("yerk-job-flaky.service")
    assert stop(j, "flaky")["ok"] and ops.notified[-1] == ("ops", W.job_line("flaky", "stopped"))
    two = ended_at(j, "flaky", 3, 1200)
    assert ops.edits == [("ops", "n1", f"{FAILED}\n2 times since {clock(one, 'UTC')}, last {clock(two, 'UTC')}")]
    assert len(ops.notified) == 2


def test_one_shots_and_other_alert_lines_post_as_today(setup, tmp_path):
    ops = Edits()
    j, *_ = care(setup, tmp_path, SCRIPT.format(n="once", key="at", when="2099-01-01 09:00"), ops=ops)
    ended_at(j, "once", 3, 1800)
    ended_at(j, "once", 3, 1200)                            # a one-shot runs once; an equal row before it changes nothing
    assert len(ops.notified) == 2 and ops.edits == []
    asyncio.run(j.alert(None, "a line of another part", 0))
    asyncio.run(j.alert(None, "a line of another part", 0))
    assert ops.notified[-2:] == [("ops", "a line of another part")] * 2 and ops.edits == []


def test_an_agent_jobs_equal_ends_are_one_post_and_did_not_start_posts_as_today(setup, tmp_path):
    ops = Edits()
    j, w, sd, priv, state, rec, _ = care(setup, tmp_path, '[jobs.think]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\n'
                                                       'room = "r"\nmessage = "x"\n', ops=ops)

    def end(exit, ago):
        rid = started(j.ledger, "think", ago + 60, kind="agent")
        j.ledger.job_run_set(rid, worker=f"w{rid}")
        asyncio.run(j.worker_done({"id": f"w{rid}", "job": "think", "staging": str(tmp_path / "none"), "room": "r", "cap": 600,
                                   "started": time.time() - ago - 60}, {"exit": exit, "finished": time.time() - ago,
                                                                        "detail": "exit-code, exited 3", "result_code": "exit-code"}))
        return j.ledger.job_run_get(rid)
    one, two = end("died", 1800), end("died", 1200)
    assert ops.notified == [("ops", f"{W.NOT_DONE} job think failed: it ended with an error")]
    assert ops.edits == [("ops", "n1", f"{ops.notified[0][1]}\n2 times since {clock(one, 'UTC')}, last {clock(two, 'UTC')}")]
    j.ledger.job_run_set(started(j.ledger, "think", 900, kind="agent"), ended=time.time() - 900, exit="not run",
                         result="did not start", delivery="alert", message_ids="n9")
    end("died", 600)                                        # a did-not-start between: not the same failure, posted anew
    assert len(ops.notified) == 2


# J3: the doctor's review of the jobs
def review_tree(tmp_path, table):
    from yerk_ai.ledger import Ledger
    priv, state = tmp_path / "private", tmp_path / "state"
    priv.mkdir()
    state.mkdir()
    (priv / "rooms.toml").write_text('[rooms.r]\nmodel = "m"\ntimezone = "UTC"\n')
    (priv / "accounts.toml").write_text('[accounts]\none = { config_dir = "cfg1" }\n')
    write_jobs(priv / "jobs.toml", table)
    led = Ledger(state / "ledger.sqlite")
    return priv, state, led


def hashes(*folders):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for f in folders for p in sorted(f.rglob("*")) if p.is_file()}


def test_the_doctors_review_names_each_finding_once_and_writes_nothing(tmp_path):
    from yerk_ai import doctor
    now = time.time()
    priv, state, led = review_tree(tmp_path, SCRIPT.format(n="lost", key="schedule", when="*:00").replace('room = "r"', 'room = "gone"')
                                   + '[jobs.who]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\naccount = "nobody"\nmessage = "x"\n'
                                   + SCRIPT.format(n="late", key="at", when="2026-01-01 09:00")
                                   + SCRIPT.format(n="going", key="at", when="2026-01-01 09:00")
                                   + SCRIPT.format(n="flaky", key="schedule", when="*:00")
                                   + SCRIPT.format(n="shop", key="schedule", when="*:00").replace('room = "r"', 'room = "r"\ndeliver = "tell"'))
    sd = Units(tmp_path / "units")
    sd.active.add("yerk-job-going.service")                # its own run takes it out
    row = lambda job, ago, **k: led.job_run_add(job=job, kind="script", room="r", planned=now - ago, fired=now - ago,  # noqa: E731
                                                ended=now - ago + 1, **k)
    row("flaky", 7200, exit="not run", result="did not start", delivery="alert")
    row("flaky", 5400, exit="cut", result="cut", delivery="alert")
    last = row("flaky", 3600, exit="exited 0", result="output", delivery="undelivered: the daemon is not running (OSError)")
    row("flaky", 1800, exit="stopped", result="stopped", delivery="none", note="stopped from the command line")
    row("flaky", 2 * 86400, exit="not run", result="did not start", delivery="alert")          # older than the day
    row("flaky", 60, exit="exited 0", result="output", delivery=jobs.WAITING)                  # told a minute ago, in hand
    row("shop", 90000, exit="exited 0", result="output", delivery=jobs.HELD)
    row("shop", 600, exit="exited 0", result="output", delivery=jobs.HELD)
    led.close()
    before = hashes(priv, state, tmp_path / "units")
    said = doctor.job_review(priv, state, now, units=sd)
    assert said == [
        f"{W.NOT_DONE} job lost: room gone is not in rooms.toml; add it there or give the job a room that is",
        f"{W.NOT_DONE} job who: account nobody is not in accounts.toml; add it there or fix the job's account",
        f"{W.WORKING} job late: a one-shot whose time ({jobs.when_text(1767258000.0, 'UTC')}) has passed is still in the jobs file, not running; "
        "`yerk schedule remove late`, or give it a time to come",
        f"{W.WORKING} job flaky: 3 of its runs in the last day did not start, were cut or are not delivered, the last at "
        f"{W.clock(now - 3600, now)}; `yerk status --full` and the runs' folders say why",
        f"{W.WORKING} watch shop: 1 of its findings held more than a day, the oldest from {W.clock(now - 90000, now)}; a daemon start tells them"]
    assert last and hashes(priv, state, tmp_path / "units") == before              # nothing written, no -wal left behind
    assert sd.calls == [] and sd.active == {"yerk-job-going.service"}


def test_a_clean_install_has_no_job_finding_and_an_unreadable_jobs_file_or_ledger_is_said(tmp_path):
    from yerk_ai import doctor
    now = time.time()
    priv, state, led = review_tree(tmp_path, SCRIPT.format(n="flaky", key="schedule", when="*:00"))
    led.job_run_add(job="flaky", kind="script", room="r", planned=now - 60, fired=now - 60, ended=now - 59, exit="exited 0",
                    result="nothing", delivery="silent (no output)")
    led.close()
    sd = Units(tmp_path / "units")
    assert doctor.job_review(priv, state, now, units=sd) == []
    (state / "ledger.sqlite").write_text("not a ledger")
    assert doctor.job_review(priv, state, now, units=sd) == [
        f"{W.WORKING} jobs: the ledger cannot be read (file is not a database); no run was reviewed"]
    (state / "ledger.sqlite").rename(state / "elsewhere")
    assert doctor.job_review(priv, state, now, units=sd)[0].startswith(f"{W.WORKING} jobs: the ledger cannot be read (")
    assert not (state / "ledger.sqlite").exists()                                  # never made
    (priv / "jobs.toml").write_text("[jobs\n")
    said = doctor.job_review(priv, state, now, units=sd)
    assert len(said) == 1 and said[0].startswith(f"{W.NOT_DONE} jobs: the jobs file cannot be read (") and said[0].endswith(
        "; no job was reviewed")
