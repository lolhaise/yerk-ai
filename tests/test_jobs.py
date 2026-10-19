"""Checks scheduled jobs, the timed tasks in jobs.toml, on a fake systemd (units and timers as files).
The cases: the three schedule kinds compute the right next fire, sync is idempotent and shows the right
diff, a removed job is cleaned, a script job's three exit cases deliver, stay silent and alert, an agent
account and cap and relays, NO_REPLY is silent, the schedule tool round-trips through the file."""
import asyncio
import hashlib
import io
import subprocess
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from yerk_ai import cli, jobs
from yerk_ai import words as W
from yerk_ai.jobs import Env, JobError, make_job
from yerk_ai.ledger import Ledger

from test_workers import Chan, ended, setup  # noqa: F401  (the worker fixture: fake units, fake claude, a room)

UTC, TOR, TOK = ZoneInfo("UTC"), ZoneInfo("America/Toronto"), ZoneInfo("Asia/Tokyo")
ENV = Env(python="/opt/yerk/bin/python", repo=Path("/opt/yerk"), private=Path("/opt/private"), state=Path("/opt/state"))
HOUR = 3600


class FakeSystemd:
    """systemd as sync and the runner rely on it: unit files in a folder, every call recorded, a timer
    known once enabled; its next and last fire times are whatever the test puts there."""

    def __init__(self, folder):
        self.folder, self.calls, self.enabled, self.next, self.last = folder, [], {}, {}, {}
        folder.mkdir(parents=True, exist_ok=True)

    def read(self, unit):
        p = self.folder / unit
        return p.read_text() if p.is_file() else None

    def write(self, unit, text):
        (self.folder / unit).write_text(text)
        self.calls.append(("write", unit))

    def remove(self, unit):
        (self.folder / unit).unlink(missing_ok=True)
        self.calls.append(("remove", unit))

    def ours(self):
        return sorted({p.name.removeprefix(jobs.UNIT).removesuffix(".timer").removesuffix(".service")
                       for p in self.folder.glob(f"{jobs.UNIT}*") if p.read_text().startswith(jobs.HEADER)})

    def active(self, unit):
        return "active" if self.enabled.get(unit) else "inactive"

    def reload(self):
        self.calls.append(("reload",))

    def enable_now(self, timer):
        self.calls.append(("enable", timer))
        self.enabled[timer] = True
        return ""

    def restart(self, timer):
        self.calls.append(("restart", timer))
        return ""

    def disable_now(self, timer):
        self.calls.append(("disable", timer))
        self.enabled.pop(timer, None)
        return ""

    def check(self, spec):
        return f"systemd does not take {spec!r}" if "Mars/" in spec else ""

    def timers(self):
        return {t: {"next": self.next.get(t, 2.0e9), "last": self.last.get(t)} for t in self.enabled if t.endswith(".timer")}


class Rec(Chan):
    """The worker fixture's channel plus the two sends a job uses."""

    def __init__(self):
        super().__init__()
        self.sent, self.notified = [], []

    async def send_text(self, room, text, key=None):
        self.sent.append((room, text, key))
        return [f"s{len(self.sent)}"]

    async def notify(self, room, text):
        self.notified.append((room, text))
        return [f"n{len(self.notified)}"]


def write_jobs(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def epoch(*a, tz):
    return datetime(*a, tzinfo=tz).timestamp()


# the schedules
def test_cron_every_and_at_compute_the_right_next_fire():
    # cron, across the spring-forward night in Toronto: Friday 08:00 EST fires at 09:00 EST the same day,
    # Friday 10:00 EST fires Monday 09:00 EDT (13:00 UTC, an hour earlier in UTC than the Friday one)
    weekdays = make_job("brief", {"kind": "agent", "schedule": "0 9 * * 1-5", "tz": "America/Toronto", "room": "r",
                                  "message": "m"})
    assert weekdays.next_fire(epoch(2026, 3, 6, 8, 0, tz=TOR)) == epoch(2026, 3, 6, 9, 0, tz=TOR) == epoch(2026, 3, 6, 14, 0, tz=UTC)
    assert weekdays.next_fire(epoch(2026, 3, 6, 10, 0, tz=TOR)) == epoch(2026, 3, 9, 9, 0, tz=TOR) == epoch(2026, 3, 9, 13, 0, tz=UTC)
    assert weekdays.calendar() == ["Mon,Tue,Wed,Thu,Fri *-*-* 9:0:00 America/Toronto"]
    assert weekdays.planned(epoch(2026, 3, 9, 9, 0, 4, tz=TOR)) == epoch(2026, 3, 9, 9, 0, tz=TOR)      # the instant it was meant for
    # names, steps and the cron rule that a restricted day of month OR a restricted weekday fires
    assert make_job("x", {"kind": "script", "schedule": "*/15 8-10 * jan,jul mon-fri", "tz": "UTC", "room": "r",
                          "command": "true"}).calendar() == ["Mon,Tue,Wed,Thu,Fri *-1,7-* 8,9,10:0/15:00 UTC"]
    either = make_job("x", {"kind": "script", "schedule": "0 0 1 * 1", "tz": "UTC", "room": "r", "command": "true"})
    assert either.calendar() == ["Mon *-*-* 0:0:00 UTC", "*-*-1 0:0:00 UTC"]
    assert either.next_fire(epoch(2026, 10, 18, 12, 0, tz=UTC)) == epoch(2026, 10, 19, 0, 0, tz=UTC)    # the Monday, not Nov 1
    # every: an interval off the clock's boundaries runs one interval after the last fire, else after now
    odd = make_job("h", {"kind": "script", "every": "1h30m", "room": "r", "command": "true"})
    assert odd.every_seconds == 5400 and odd.next_fire(1000.0) == 6400.0 and odd.next_fire(1000.0, last=500.0) == 5900.0
    assert odd.planned(7000.0, last_trigger=6400.0) == 6400.0 and odd.describe() == "every 1h30m" and odd.calendar() == []
    # an interval that divides an hour or a day runs on the clock's boundaries, in its zone, like a cron line
    two = make_job("t", {"kind": "script", "every": "2m", "room": "r", "command": "true"})
    assert two.calendar() == ["*-*-* *:0/2:00 UTC"] and two.next_fire(epoch(2026, 10, 18, 12, 1, 30, tz=UTC)) == epoch(2026, 10, 18, 12, 2, tz=UTC)
    assert two.planned(epoch(2026, 10, 18, 12, 2, 0, 190000, tz=UTC)) == epoch(2026, 10, 18, 12, 2, tz=UTC)
    six = make_job("s", {"kind": "script", "every": "6h", "tz": "Asia/Tokyo", "room": "r", "command": "true"})
    assert six.calendar() == ["*-*-* 0/6:0:00 Asia/Tokyo"] and six.next_fire(epoch(2026, 10, 18, 7, 0, tz=TOK)) == epoch(2026, 10, 18, 12, 0, tz=TOK)
    assert make_job("d", {"kind": "script", "every": "1d", "room": "r", "command": "true"}).calendar() == ["*-*-* 0:0:00 UTC"]
    assert make_job("m", {"kind": "script", "every": "1h", "room": "r", "command": "true"}).calendar() == ["*-*-* *:0:00 UTC"]
    assert jobs.every_cron(7 * 60) is None and jobs.every_cron(90) is None and jobs.every_cron(5 * 3600) is None
    # at: a naive time is read in tz, one with an offset keeps its instant, a past one is refused
    at = make_job("a", {"kind": "script", "at": "2099-01-02 09:00", "tz": "Asia/Tokyo", "room": "r", "command": "true"})
    assert at.at_epoch == epoch(2099, 1, 2, 9, 0, tz=TOK) and at.calendar() == ["2099-01-02 09:00:00 Asia/Tokyo"]
    assert at.next_fire(time.time()) == at.at_epoch and at.planned(at.at_epoch + 2) == at.at_epoch
    offs = make_job("a", {"kind": "script", "at": "2099-01-02T09:00:00+09:00", "tz": "America/Toronto", "room": "r",
                          "command": "true"})
    assert offs.at_epoch == at.at_epoch and offs.calendar() == ["2099-01-01 19:00:00 America/Toronto"]
    with pytest.raises(JobError, match="in the past"):
        make_job("a", {"kind": "script", "at": "2020-01-02 09:00", "tz": "UTC", "room": "r", "command": "true"})


@pytest.mark.parametrize("raw, why", [
    ({"kind": "cron", "schedule": "* * * * *", "tz": "UTC", "room": "r", "command": "x"}, '`kind` must be "script", "agent" or "watcher"'),
    ({"kind": "script", "schedule": "* * * * *", "every": "1h", "tz": "UTC", "room": "r", "command": "x"}, "exactly one of"),
    ({"kind": "script", "schedule": "0 9 * * *", "room": "r", "command": "x"}, "needs `tz`"),
    ({"kind": "script", "schedule": "0 25 * * *", "tz": "UTC", "room": "r", "command": "x"}, "25 is outside 0-23"),
    ({"kind": "script", "schedule": "0 9 * *", "tz": "UTC", "room": "r", "command": "x"}, "five fields"),
    ({"kind": "script", "schedule": "0 9 * * *", "tz": "Mars/Olympus", "room": "r", "command": "x"}, "not a time zone name"),
    ({"kind": "script", "every": "soon", "room": "r", "command": "x"}, "not a duration"),
    ({"kind": "script", "every": "5s", "room": "r", "command": "x"}, "under 30 seconds"),
    ({"kind": "script", "every": "1h", "command": "x"}, "`room` is required"),
    ({"kind": "script", "every": "1h", "room": "r", "message": "x"}, "has `command`, not `message`"),
    ({"kind": "agent", "every": "1h", "room": "r", "command": "x"}, "has `message`, not `command`"),
    ({"kind": "agent", "every": "1h", "room": "r"}, "needs `message`"),
    ({"kind": "script", "every": "1h", "room": "r", "command": "x", "account": "one"}, "runs with no account"),
    ({"kind": "script", "every": "1h", "room": "r", "command": "x", "colour": "red"}, "unknown key colour"),
    ({"kind": "script", "every": "1h", "room": "r", "command": "x", "deliver": "loud"}, '"announce" or "silent"'),
    ({"kind": "script", "every": "1h", "room": "r", "command": "x", "cap": 0}, "above 0"),
])
def test_a_job_that_cannot_be_scheduled_is_named_with_why(raw, why):
    with pytest.raises(JobError, match=why):
        make_job("ok-name", raw)
    with pytest.raises(JobError, match="lowercase"):
        make_job("Not OK", {"kind": "script", "every": "1h", "room": "r", "command": "x"})


# the sync
def test_sync_is_idempotent_shows_the_diff_and_cleans_a_removed_job(tmp_path):
    sd = FakeSystemd(tmp_path / "units")
    path = write_jobs(tmp_path / "private" / "jobs.toml", "# my jobs\n\n"
                      '[jobs.brief]\nkind = "agent"\nschedule = "0 9 * * 1-5"\ntz = "America/Toronto"\nroom = "r"\n'
                      'account = "two"\ncap = 20\nmessage = "the brief"\n\n'
                      '[jobs.beat]\nkind = "script"\nevery = "2m"\nroom = "r"\ndeliver = "silent"\ncommand = "true"\n')
    p, back = jobs.sync(path, sd, ENV)
    assert (p.added, p.changed, p.unchanged, p.removed, p.errors) == (["brief", "beat"], [], [], [], {})
    assert [c for c in sd.calls if c[0] != "write"] == [("reload",), ("enable", "yerk-job-brief.timer"),
                                                         ("enable", "yerk-job-beat.timer")]
    timer, service = sd.read("yerk-job-brief.timer"), sd.read("yerk-job-brief.service")
    assert timer.startswith(jobs.HEADER + "brief.") and "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 9:0:00 America/Toronto" in timer
    assert "Persistent=true" in timer and "AccuracySec=1s" in timer and "Unit=yerk-job-brief.service" in timer
    assert "ExecStart=/opt/yerk/bin/python -P -m yerk_ai.jobs run brief" in service and "TimeoutStartSec=120" in service
    assert 'Environment="YERK_PRIVATE=/opt/private"' in service and "WorkingDirectory=/opt/yerk" in service
    beat = sd.read("yerk-job-beat.timer")
    assert "OnCalendar=*-*-* *:0/2:00 UTC" in beat and "Persistent=true" in beat     # on the clock: even minutes
    assert "TimeoutStartSec=720" in sd.read("yerk-job-beat.service")          # the 10-minute default cap plus 120 s
    assert set(back) == {"brief", "beat"} and all(r["scheduled"] for r in back.values())
    assert jobs.diff_lines(p)[0] == "+ brief (new): cron `0 9 * * 1-5` America/Toronto"
    # the same file again: nothing written, nothing enabled, nothing restarted
    n = len(sd.calls)
    p2, _ = jobs.sync(path, sd, ENV)
    assert (p2.added, p2.changed, p2.unchanged, p2.removed) == ([], [], ["brief", "beat"], []) and len(sd.calls) == n
    assert jobs.diff_lines(p2) == ["= 2 unchanged: brief, beat"]
    # one job changed: its units rewritten and its timer restarted; the other untouched
    path.write_text(path.read_text().replace('every = "2m"', 'every = "7m"'))       # off the clock: monotonic
    p3, _ = jobs.sync(path, sd, ENV)
    assert (p3.changed, p3.unchanged) == (["beat"], ["brief"])
    beat = sd.read("yerk-job-beat.timer")
    assert "OnActiveSec=420s" in beat and "OnUnitActiveSec=420s" in beat and "Persistent" not in beat
    assert sd.calls[n:] == [("write", "yerk-job-beat.service"), ("write", "yerk-job-beat.timer"), ("reload",),
                            ("enable", "yerk-job-beat.timer"), ("restart", "yerk-job-beat.timer")]
    # a job removed from the file: disabled, both files gone; a broken one named with why, its unit never written
    path.write_text(path.read_text().split("[jobs.beat]")[0] + '[jobs.broken]\nkind = "script"\nschedule = "0 25 * * *"\n'
                    'tz = "UTC"\nroom = "r"\ncommand = "true"\n')
    n = len(sd.calls)
    p4, back4 = jobs.sync(path, sd, ENV)
    assert p4.removed == ["beat"] and list(p4.errors) == ["broken"] and "25 is outside 0-23" in p4.errors["broken"]
    assert sd.calls[n:] == [("disable", "yerk-job-beat.timer"), ("remove", "yerk-job-beat.timer"),
                            ("remove", "yerk-job-beat.service"), ("reload",)]
    assert sd.ours() == ["brief"] and set(back4) == {"brief"}
    assert jobs.diff_lines(p4) == ["- beat (removed: not in the jobs file)", "= 1 unchanged: brief",
                                   "! broken: cannot be scheduled: in the cron expression '0 25 * * *': 25 is outside 0-23"]
    # a calendar systemd itself refuses is named too, and a stray timer is named in the read-back
    path.write_text(path.read_text().replace('tz = "UTC"', 'tz = "UTC"').replace("[jobs.broken]", "[jobs.mars]")
                    .replace('schedule = "0 25 * * *"', 'schedule = "0 9 * * *"'))
    sd.check = lambda spec: "systemd does not take it"                                           # every spec refused
    p5, _ = jobs.sync(path, sd, ENV)
    assert set(p5.errors) == {"brief", "mars"} and p5.removed == ["brief"] and sd.ours() == []
    assert jobs.diff_lines(p5)[-1] == "! mars: cannot be scheduled: systemd does not take it"
    sd.enabled["yerk-job-stray.timer"] = True
    lines = jobs.status_lines(path, sd)
    assert lines[:2] == [f"{jobs.NOTHING} brief: not scheduled", f"{jobs.NOTHING} mars: not scheduled"]
    assert f"{jobs.NOT_DONE} timer stray: not in the jobs file" in lines
    path.write_text(path.read_text().replace('schedule = "0 9 * * *"', 'schedule = "0 25 * * *"'))
    lines = jobs.status_lines(path, sd)
    assert lines[-1] == f"{jobs.NOT_DONE} mars: cannot be scheduled"
    assert all(x[0] in (jobs.DONE, jobs.NOT_DONE, jobs.NOTHING) and len(x) <= jobs.PHONE - 2 for x in lines)   # state first, phone width
    assert jobs.phone("x" * 60) == "x" * 37 + "\N{HORIZONTAL ELLIPSIS}" and jobs.phone("short") == "short"


def test_the_read_back_comes_from_systemd_in_the_jobs_zone(tmp_path):
    sd = FakeSystemd(tmp_path / "units")
    path = write_jobs(tmp_path / "jobs.toml", '[jobs.tokyo]\nkind = "script"\nschedule = "0 9 * * *"\ntz = "Asia/Tokyo"\n'
                                              'room = "r"\ncommand = "true"\n')
    p, back = jobs.sync(path, sd, ENV)
    sd.next["yerk-job-tokyo.timer"] = epoch(2026, 10, 19, 9, 0, tz=TOK)
    sd.last["yerk-job-tokyo.timer"] = epoch(2026, 10, 18, 9, 0, tz=TOK)
    now = epoch(2026, 10, 19, 8, 30, tz=TOK)
    back = jobs.readback(p.jobs, sd, now)
    line, = jobs.read_lines(back, p.jobs, now)
    assert line.startswith("tokyo: next Mon 2026-10-19 09:00:00 JST (in 30m 00s), last Sun 2026-10-18 09:00:00 JST; "
                           "cron `0 9 * * *` Asia/Tokyo, room r, script")
    assert jobs.status_lines(path, sd, now) == [f"{jobs.DONE} tokyo: in 30m 00s"]      # the detail is `yerk jobs list`
    sd.enabled.clear()
    assert jobs.status_lines(path, sd, now) == [f"{jobs.NOTHING} tokyo: not scheduled"]

    def broken():
        raise OSError("systemctl --user list-timers exited 1: no manager")
    sd.timers = broken
    assert jobs.status_lines(path, sd, now) == [f"{jobs.NOT_DONE} jobs: the timer list could not be read"]


def test_the_real_adapter_writes_reads_and_removes_unit_files_and_a_removed_timers_stamp(tmp_path):
    sd = jobs.Systemd(folder=tmp_path / "units", stamps=tmp_path / "stamps")
    sd.write("yerk-job-x.timer", jobs.HEADER + "x.\n[Timer]\n")
    sd.write("yerk-job-x.service", jobs.HEADER + "x.\n[Service]\n")
    (tmp_path / "units" / "yerk-job-hand.timer").write_text("[Timer]\n")          # not ours: never listed, never removed
    (tmp_path / "stamps").mkdir()
    (tmp_path / "stamps" / "stamp-yerk-job-x.timer").touch()                         # systemd's last-trigger stamp
    assert sd.read("yerk-job-x.timer") == jobs.HEADER + "x.\n[Timer]\n" and sd.read("nothing.timer") is None
    assert sd.ours() == ["x"]
    sd.remove("yerk-job-x.timer")
    sd.remove("yerk-job-x.service")
    sd.remove("yerk-job-x.service")                                                  # gone already: no error
    assert sorted(p.name for p in (tmp_path / "units").iterdir()) == ["yerk-job-hand.timer"]
    assert list((tmp_path / "stamps").iterdir()) == []                              # the stamp went with the timer


def test_a_systemctl_that_cannot_start_raises_so_the_tool_keeps_the_job_and_a_removal_keeps_its_units(tmp_path, monkeypatch):
    fail = {"enable": OSError(12, "Cannot allocate memory")}               # verb -> raised (a failed fork) or its exit

    def run(cmd, **k):
        why = fail.get(cmd[2]) if cmd[0] == "systemctl" else None
        if isinstance(why, OSError):
            raise why
        return subprocess.CompletedProcess(cmd, why or 0, "[]", "boom\n")
    monkeypatch.setattr(jobs.subprocess, "run", run)
    sd = jobs.Systemd(folder=tmp_path / "units", stamps=tmp_path / "stamps")
    with pytest.raises(OSError, match="Cannot allocate memory"):
        sd.enable_now("yerk-job-x.timer")
    fail["restart"] = 1                                                             # only a nonzero exit is a refusal
    assert sd.restart("yerk-job-x.timer") == "systemctl --user restart yerk-job-x.timer exited 1: boom"
    path = write_jobs(tmp_path / "jobs.toml", "")
    assert jobs.schedule_add(path, sd, ENV, "x", "script", "every 1h", "true", "r", "UTC") == \
        {"ok": False, "error": "the sync failed: [Errno 12] Cannot allocate memory"}
    assert "[jobs.x]" in path.read_text()                                          # kept for the next sync
    fail.clear()
    jobs.sync(path, sd, ENV)
    jobs.remove(path, "x")
    fail["disable"] = OSError(12, "Cannot allocate memory")
    with pytest.raises(OSError, match="Cannot allocate memory"):
        jobs.sync(path, sd, ENV)
    assert sd.ours() == ["x"]                                                       # the error comes before the files go


# the runs
def manager(setup, tmp_path, rooms, channels, conf=None, room_confs=None, daemon_conf=None):
    s = setup
    w = s.make(rooms, channel=channels[next(iter(rooms))])
    priv, state = tmp_path / "private", tmp_path / "state"
    priv.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    sd = FakeSystemd(tmp_path / "units")
    j = jobs.Jobs(priv, state, s.ledger, conf or {}, rooms, channels, room_confs or {n: {"model": "m", "timezone": "UTC"}
                                                                                 for n in rooms},
                  daemon_conf or {}, w, units=sd)
    return j, w, sd, priv, state


def test_a_script_jobs_three_exit_cases_deliver_stay_silent_and_alert(setup, tmp_path):
    s = setup
    rec, ops = Rec(), Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room(), "ops": s.room("ops")}, {"r": rec, "ops": ops},
                                    conf={"ops_room": "ops"})
    s.ledger.__init__(state / "ledger.sqlite")          # the runner opens the ledger at <state>/ledger.sqlite
    j.ledger = w.ledger = s.ledger
    write_jobs(priv / "jobs.toml", "\n".join(
        f'[jobs.{n}]\nkind = "script"\nevery = "1h"\ntz = "UTC"\nroom = "r"\n{extra}command = {c!r}\n'.replace("'", '"')
        for n, c, extra in (("out", "echo hello from the job", ""), ("quiet", "true", ""),
                            ("bad", "echo oops >&2; exit 3", ""), ("envy", "env | sort", 'deliver = "silent"\n'),
                            ("slow", "sleep 30", "cap = 0.02\n"))))
    jobs.sync(priv / "jobs.toml", sd, ENV)
    handoff = lambda payload: asyncio.run(j.handle("", payload["job"]))  # noqa: E731  (the daemon's /control)
    codes = {n: jobs.run(n, priv, state, units=sd, handoff=handoff) for n in ("out", "quiet", "bad", "envy", "slow")}
    runs = {r["job"]: r for r in s.ledger.job_runs()}
    assert codes == {"out": 0, "quiet": 0, "bad": 1, "envy": 0, "slow": 1}
    # exit 0 with output: the output, as is, in the job's room
    assert (runs["out"]["exit"], runs["out"]["result"], runs["out"]["delivery"], runs["out"]["message_ids"]) == \
        ("exited 0", "output", "delivered", "s1")
    assert rec.sent == [("r", "hello from the job", f"job-{runs['out']['id']}-out")]
    # exit 0 with nothing: nothing
    assert (runs["quiet"]["result"], runs["quiet"]["delivery"]) == ("nothing", "silent (no output)")
    # a failure: one alert line in the ops room, with the stderr tail; a timeout alerts too
    assert (runs["bad"]["exit"], runs["bad"]["result"], runs["bad"]["delivery"]) == ("exited 3", "exit 3", "alert")
    assert (runs["slow"]["exit"], runs["slow"]["result"], runs["slow"]["delivery"]) == ("timed out", "timed out", "alert")
    # the room line in plain words through words.py; the exit code and the stderr tail stay in the run's note
    assert ops.notified == [("ops", f"{W.NOT_DONE} job bad failed: it ended with an error"),
                            ("ops", W.job_line("slow", "timed out", 0.02 * 60))]
    assert ops.notified[1][1].startswith(f"{W.NOT_DONE} job slow stopped: timed out at ")
    assert not [t for _, t in ops.notified if "exit" in t or "oops" in t]
    assert (runs["bad"]["note"], runs["slow"]["note"]) == ("exit 3: oops", "timed out after 0.02 min")
    assert ops.sent == [] and rec.notified == []
    # deliver = silent: the output is kept, nothing posted; the environment is account-free, with the job's zone
    assert (runs["envy"]["result"], runs["envy"]["delivery"]) == ("output", "silent (deliver = silent)")
    env = (Path(runs["envy"]["folder"]) / "stdout").read_text()
    assert "TZ=UTC" in env and "YERK_JOB=envy" in env and "YERK_ROOM=r" in env and "PATH=" in env
    assert not any(x.startswith(("CLAUDE", "ANTHROPIC", "YERK_TOKEN", "SSH_AUTH")) for x in env.splitlines())
    for r in runs.values():
        assert r["fired"] >= r["planned"] and r["ended"] >= r["fired"] and (Path(r["folder"]) / "stderr").is_file()


def test_a_script_run_with_no_daemon_is_ledgered_undelivered_and_delivered_late_at_the_next_start(setup, tmp_path):
    s = setup
    rec = Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": rec})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    write_jobs(priv / "jobs.toml", '[jobs.late]\nkind = "script"\nevery = "1h"\ntz = "UTC"\nroom = "r"\ncommand = "echo while you were out"\n')

    def down(payload):
        raise OSError("connection refused")
    assert jobs.run("late", priv, state, units=sd, handoff=down) == 1
    r, = s.ledger.job_runs()
    assert (r["exit"], r["result"], r["delivery"]) == ("exited 0", "output", "undelivered: the daemon is not running (OSError)")
    assert rec.sent == []
    asyncio.run(j.reattach())
    r, = s.ledger.job_runs()
    assert r["delivery"] == "delivered late" and r["message_ids"] == "s1"
    assert rec.sent[0][1].startswith("(late: the daemon was not running when job late ran at ") and \
        rec.sent[0][1].endswith("\nwhile you were out")


def test_an_agent_job_becomes_a_worker_with_the_right_account_and_cap_and_relays(setup, tmp_path):
    s = setup
    rec = Rec()
    room = s.room()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": room}, {"r": rec})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    write_jobs(priv / "jobs.toml",
               '[jobs.brief]\nkind = "agent"\nschedule = "0 9 * * *"\ntz = "UTC"\nroom = "r"\naccount = "two"\ncap = 7\n'
               'message = "FAKE report=\\"Three things happened\\" result=\\"Three things happened today\\""\n\n'
               '[jobs.hush]\nkind = "agent"\nevery = "1h"\nroom = "r"\n'
               'message = "FAKE report=\\"NO_REPLY\\" result=\\"NO_REPLY\\""\n\n'
               '[jobs.mute]\nkind = "agent"\nevery = "1h"\nroom = "r"\ndeliver = "silent"\n'
               'message = "FAKE report=\\"kept\\" result=\\"kept to the ledger\\""\n\n'
               '[jobs.crash]\nkind = "agent"\nevery = "1h"\nroom = "r"\nmessage = "FAKE report=\\"half\\" exit=3"\n')
    jobs.sync(priv / "jobs.toml", sd, ENV)

    async def go():
        loop = asyncio.get_running_loop()
        w.begin(loop)
        handoff = lambda p: asyncio.run_coroutine_threadsafe(j.handle("", p["job"]), loop).result(30)  # noqa: E731
        out = []
        for name in ("brief", "hush", "mute", "crash"):
            rc = await asyncio.to_thread(jobs.run, name, priv, state, sd, handoff)
            run = s.ledger.job_runs(job=name)[0]
            assert rc == 0 and run["worker"], (name, rc, run)
            out.append((run["id"], await ended(s.ledger, run["worker"], relay=name == "brief")))
            await asyncio.sleep(0.2)                     # the hook's ledger writes after the worker row
        return out
    (run_brief, brief), (run_hush, hush), (run_mute, mute), (run_crash, crash) = asyncio.run(go())
    # the worker: the job's account and cap, the job named on its row, its brief is the message
    call = s.units.calls[0]
    assert call["cap"] == 7 * 60 and call["env"]["CLAUDE_CONFIG_DIR"].endswith("cfg2")
    assert (brief["account"], brief["job"], brief["exit"], brief["result"]) == ("two", "brief", "done", "Three things happened")
    assert "FAKE report=" in Path(brief["brief"]).read_text() and "scheduled job brief" in (Path(brief["staging"]) / "PROMPT.md").read_text()
    # its last message relayed by the room's own session; the run row says delivered
    assert brief["relay"] == "relayed"
    (reply, inbound), = s.chan.replies
    assert inbound == [f"relay-{brief['id']}"] and "Three things happened today" in reply and "[job brief finished]" in reply
    r = s.ledger.job_run_get(run_brief)
    assert (r["kind"], r["worker"], r["exit"], r["result"], r["delivery"], r["message_ids"]) == \
        ("agent", brief["id"], "done", "done", "delivered", "m1")
    # NO_REPLY: nothing in the room, ledgered as silent; deliver = silent likewise; a death alerts
    assert (hush["relay"], hush["relay_note"]) == ("skipped", "the job answered NO_REPLY")
    assert (s.ledger.job_run_get(run_hush)["result"], s.ledger.job_run_get(run_hush)["delivery"]) == ("no_reply", "silent (NO_REPLY)")
    assert (mute["relay"], s.ledger.job_run_get(run_mute)["delivery"]) == ("skipped", "silent (deliver = silent)")
    assert crash["exit"] == "died" and s.ledger.job_run_get(run_crash)["delivery"] == "alert"
    assert rec.notified == [("r", f"{W.NOT_DONE} job crash failed: it ended with an error")]
    assert s.ledger.job_run_get(run_crash)["note"] == f"worker {crash['id']}: exit-code, exited 3"
    assert len(s.chan.replies) == 1                      # the one relay; the other three posted nothing


def test_a_one_shot_is_removed_from_the_file_and_its_units_go_after_it_fires(setup, tmp_path):
    s = setup
    rec = Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": rec})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    path = write_jobs(priv / "jobs.toml", "# keep this comment\n\n[jobs.keep]\nkind = \"script\"\nevery = \"1h\"\nroom = \"r\"\n"
                      "command = \"true\"\n\n[jobs.once]  # the reminder\nkind = \"script\"\nat = \"2099-01-02 09:00\"\n"
                      "tz = \"UTC\"\nroom = \"r\"\ncommand = \"echo Reminder: it is time\"\n")
    jobs.sync(path, sd, ENV)
    assert sd.ours() == ["keep", "once"]
    rc = jobs.run("once", priv, state, units=sd, handoff=lambda p: asyncio.run(j.handle("", p["job"])))
    assert rc == 0 and rec.sent[0][1] == "Reminder: it is time"
    assert path.read_text() == "# keep this comment\n\n[jobs.keep]\nkind = \"script\"\nevery = \"1h\"\nroom = \"r\"\ncommand = \"true\"\n"
    assert sd.ours() == ["keep"] and ("disable", "yerk-job-once.timer") in sd.calls and sd.read("yerk-job-once.service") is None
    r, = s.ledger.job_runs(job="once")
    assert r["planned"] == epoch(2099, 1, 2, 9, 0, tz=UTC) and r["delivery"] == "delivered"
    # a job whose timer fires after it left the file: said, ledgered, exit 2
    assert jobs.run("gone", priv, state, units=sd, handoff=lambda p: {"ok": True}) == 2
    assert s.ledger.job_runs(job="gone")[0]["result"] == "no such job: not in the jobs file"


# the schedule tool
def test_the_schedule_tool_round_trips_through_the_file(setup, tmp_path):
    s = setup
    rec = Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room(), "q": s.room("q")}, {"r": rec, "q": rec},
                                    room_confs={"r": {"timezone": "Asia/Tokyo"}, "q": {}},
                                    daemon_conf={"memory": {"timezone": "America/Toronto"}})
    path = priv / "jobs.toml"
    write_jobs(path, "# the owner's comment stays\n")
    add = lambda **k: asyncio.run(j.handle("r", {"action": "add", **k}))  # noqa: E731
    out = add(name="remind", when="2099-01-02 09:00", what="echo hi", kind="script")
    assert out["ok"] and out["tz"] == "Asia/Tokyo" and out["room"] == "r" and out["unit"] == "yerk-job-remind.timer"
    assert out["schedule"] == "one-shot Fri 2099-01-02 09:00:00 JST" and out["diff"][0].startswith("+ remind (new)")
    loaded = jobs.load(path)[0]["remind"]
    assert loaded.at_epoch == epoch(2099, 1, 2, 9, 0, tz=TOK) and loaded.payload == "echo hi" and loaded.kind == "script"
    assert path.read_text().startswith("# the owner's comment stays\n\n[jobs.remind]\nkind = \"script\"\nat = \"2099-01-02 09:00\"\n"
                                       "tz = \"Asia/Tokyo\"\nroom = \"r\"\ncommand = \"echo hi\"\n")
    # the room's zone, the daemon's zone, an explicit zone; never UTC by accident
    assert add(name="cron", when="0 9 * * 1-5", what="the brief", room="q")["tz"] == "America/Toronto"
    assert add(name="paris", when="every 2h", what="the brief", tz="Europe/Paris", deliver="silent", cap_minutes=3)["tz"] == "Europe/Paris"
    j.daemon_conf = {}
    assert "give `tz`" in add(name="nozone", when="0 9 * * *", what="x", room="q")["error"]
    assert "nozone" not in path.read_text()
    paris = jobs.load(path)[0]["paris"]
    assert (paris.when, paris.every_seconds, paris.deliver, paris.cap, paris.kind) == ("every", 7200, "silent", 3.0, "agent")
    # list reads the timers back; a repeated name and a bad `when` leave the file as it was
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert "already in the file" in add(name="remind", when="2099-05-05 10:00", what="x", kind="script")["error"]
    assert "five fields" in add(name="odd", when="tomorrowish", what="x")["error"]
    assert "not a time zone" in add(name="odd", when="0 9 * * *", what="x", tz="Mars/Olympus")["error"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    listed = asyncio.run(j.handle("r", {"action": "list"}))
    assert [x["name"] for x in listed["jobs"]] == ["remind", "cron", "paris"] and all(x["scheduled"] for x in listed["jobs"])
    assert listed["lines"][0].startswith("remind: next ") and "JST" in listed["lines"][0]
    # remove: the block goes, the rest of the file byte for byte
    kept = path.read_text()
    out = asyncio.run(j.handle("r", {"action": "remove", "name": "cron"}))
    assert out["ok"] and "- cron (removed" in out["diff"][0] and sd.ours() == ["paris", "remind"]
    assert path.read_text() == kept.replace(kept[kept.index("\n[jobs.cron]"):kept.index("\n[jobs.paris]")], "")
    assert not asyncio.run(j.handle("r", {"action": "remove", "name": "cron"}))["ok"]
    # the same three actions from the command line, on the same file and timers
    monkey = pytest.MonkeyPatch()
    monkey.setattr(jobs, "Systemd", lambda: sd)
    monkey.setattr(cli, "rooms", lambda p: {"r": {"timezone": "Asia/Tokyo"}})
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["schedule", "add", "cli-job", "--when", "every 45m", "--what", "echo cli", "--kind", "script",
                           "--room", "r", "--private", str(priv), "--state", str(state)])
            rc2 = cli.main(["schedule", "list", "--private", str(priv), "--state", str(state)])
            rc3 = cli.main(["jobs", "sync", "--private", str(priv), "--state", str(state)])
            rc4 = cli.main(["schedule", "remove", "cli-job", "--private", str(priv), "--state", str(state)])
    finally:
        monkey.undo()
    text = buf.getvalue()
    assert (rc, rc2, rc3, rc4) == (0, 0, 0, 0)
    assert text.startswith("cli-job: every 45m, script job, room r, deliver announce; next ") and "cli-job: next" in text
    assert "= 3 unchanged: remind, paris, cli-job" in text and "cli-job: removed" in text and "cli-job" not in path.read_text()
