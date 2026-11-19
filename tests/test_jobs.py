"""Checks scheduled jobs, the timed tasks in jobs.toml, on a fake systemd (units and timers as files).
The cases: the three schedule kinds compute the right next fire, sync is idempotent and shows the right
diff, a removed job is cleaned, a script job's three exit cases deliver, stay silent and alert, an agent
job becomes a worker with the right account and cap and relays, NO_REPLY is silent, a note is told by
its room's own session (late after a restart, never twice), the schedule tool round-trips through the
owner's keeps its own notes and nothing else, under its caps."""
import asyncio
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from yerk_ai import cli, jobs
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai import words as W
from yerk_ai.jobs import Env, JobError, make_job

from test_workers import Chan, ended, setup  # noqa: F401  (the worker fixture: fake units, fake claude, a room)

UTC, TOR, TOK = ZoneInfo("UTC"), ZoneInfo("America/Toronto"), ZoneInfo("Asia/Tokyo")
ENV = Env(python="/opt/yerk/bin/python", repo=Path("/opt/yerk"), private=Path("/opt/private"), state=Path("/opt/state"))
HOUR = 3600


class FakeSystemd:
    """systemd as sync and the runner rely on it: unit files in a folder, every call recorded, a timer
    known once enabled; its next and last fire times are whatever the test puts there, and a service
    runs while the test has it in `active`."""

    def __init__(self, folder):
        self.folder, self.calls, self.enabled, self.next, self.last, self.active = folder, [], {}, {}, {}, set()
        folder.mkdir(parents=True, exist_ok=True)

    def running(self, unit):
        return unit in self.active

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
def test_a_calendar_line_and_a_time_are_the_two_schedules_and_a_cron_line_is_refused_with_its_calendar_line():
    weekdays = make_job("brief", {"kind": "agent", "schedule": "Mon..Fri 09:00", "tz": "America/Toronto", "room": "r",
                                  "message": "m"})
    assert weekdays.calendar() == ["Mon..Fri 09:00 America/Toronto"] and weekdays.when == "calendar"
    assert weekdays.describe() == "calendar `Mon..Fri 09:00` America/Toronto"
    assert weekdays.planned(epoch(2026, 3, 9, 9, 0, 4, tz=TOR)) == epoch(2026, 3, 9, 9, 0, 4, tz=TOR)     # planned when it fired
    # plain lines first: the replacement is the OnCalendar text the old parser wrote for each, so a swapped unit is unchanged
    for cron, line in (("0 10 * * 1", "Mon *-*-* 10:0:00"), ("45 22 * * *", "*-*-* 22:45:00"), ("30 4 * * *", "*-*-* 4:30:00"),
                       ("*/15 8-10 * * 1-5", "Mon..Fri *-*-* 8..10:0/15:00"), ("0 0 */2 * *", "*-*-1/2 0:0:00"),
                       ("0 9 * * 5-7", "Fri..Sun *-*-* 9:0:00")):
        with pytest.raises(JobError) as e:
            make_job("x", {"kind": "note", "schedule": cron, "tz": "UTC", "room": "r", "message": "m"})
        assert str(e.value) == f'`schedule` takes a systemd calendar line now, not cron: replace schedule = "{cron}" with schedule = "{line}"'
    with pytest.raises(JobError, match=r'replace schedule = "0 9 \* \* 0-4" with a calendar line \(`man systemd.time`'):
        make_job("x", {"kind": "note", "schedule": "0 9 * * 0-4", "tz": "UTC", "room": "r", "message": "m"})   # Sun..Thu: systemd refuses it
    with pytest.raises(JobError, match=r'replace schedule = "0 0 1 \* 1" with a calendar line \(`man systemd.time`'):
        make_job("x", {"kind": "note", "schedule": "0 0 1 * 1", "tz": "UTC", "room": "r", "message": "m"})   # both day fields
    assert jobs.cron_refusal("Mon 10:00") == jobs.cron_refusal("*-*-* 04:30:00 UTC") == jobs.cron_refusal("hourly") == ""
    # at: a naive time is read in tz, one with an offset keeps its instant, a past one is refused
    at = make_job("a", {"kind": "script", "at": "2099-01-02 09:00", "tz": "Asia/Tokyo", "room": "r", "command": "true"})
    assert at.at_epoch == epoch(2099, 1, 2, 9, 0, tz=TOK) and at.calendar() == ["2099-01-02 09:00:00 Asia/Tokyo"]
    assert at.planned(at.at_epoch + 2) == at.at_epoch
    offs = make_job("a", {"kind": "script", "at": "2099-01-02T09:00:00+09:00", "tz": "America/Toronto", "room": "r",
                          "command": "true"})
    assert offs.at_epoch == at.at_epoch and offs.calendar() == ["2099-01-01 19:00:00 America/Toronto"]
    with pytest.raises(JobError, match="in the past"):
        make_job("a", {"kind": "script", "at": "2020-01-02 09:00", "tz": "UTC", "room": "r", "command": "true"})


@pytest.mark.parametrize("raw, why", [
    ({"kind": "cron", "schedule": "* * * * *", "tz": "UTC", "room": "r", "command": "x"}, '`kind` must be "script", "agent" or "note"'),
    ({"kind": "script", "schedule": "*:00", "at": "2099-01-01 00:00", "tz": "UTC", "room": "r", "command": "x"}, "exactly one of"),
    ({"kind": "script", "schedule": "*-*-* 09:00", "room": "r", "command": "x"}, "needs `tz`"),
    ({"kind": "script", "schedule": "0 9 * * *", "tz": "UTC", "room": "r", "command": "x"}, "takes a systemd calendar line now, not cron"),
    ({"kind": "script", "every": "1h", "room": "r", "command": "x"}, "unknown key every"),
    ({"kind": "script", "schedule": "*-*-* 09:00", "tz": "Mars/Olympus", "room": "r", "command": "x"}, "not a time zone name"),
    ({"kind": "script", "schedule": "*:00", "tz": "UTC", "command": "x"}, "`room` is required"),
    ({"kind": "script", "schedule": "*:00", "tz": "UTC", "room": "r", "message": "x"}, "has `command`, not `message`"),
    ({"kind": "agent", "schedule": "*:00", "tz": "UTC", "room": "r", "command": "x"}, "has `message`, not `command`"),
    ({"kind": "agent", "schedule": "*:00", "tz": "UTC", "room": "r"}, "needs `message`"),
    ({"kind": "script", "schedule": "*:00", "tz": "UTC", "room": "r", "command": "x", "account": "one"}, "runs with no account"),
    ({"kind": "script", "schedule": "*:00", "tz": "UTC", "room": "r", "command": "x", "colour": "red"}, "unknown key colour"),
    ({"kind": "script", "schedule": "*:00", "tz": "UTC", "room": "r", "command": "x", "deliver": "loud"}, '"announce" or "silent"'),
    ({"kind": "script", "schedule": "*:00", "tz": "UTC", "room": "r", "command": "x", "cap": 0}, "above 0"),
    ({"kind": "note", "schedule": "*:00", "tz": "UTC", "room": "r", "message": "x", "command": "echo x"}, "runs nothing, so it takes no `command`"),
    ({"kind": "note", "schedule": "*:00", "tz": "UTC", "room": "r", "message": "x", "account": "one"}, "takes no `account`"),
    ({"kind": "note", "schedule": "*:00", "tz": "UTC", "room": "r", "message": "x", "cap": 5}, "takes no `cap`"),
    ({"kind": "note", "schedule": "*:00", "tz": "UTC", "room": "r", "message": "x", "effort": "high"}, "takes no `effort`"),
    ({"kind": "note", "schedule": "*:00", "tz": "UTC", "room": "r", "message": "x", "model": "sonnet"}, "takes no `model`"),
    ({"kind": "script", "schedule": "*:00", "tz": "UTC", "room": "r", "command": "x", "effort": "low"}, "runs no model; `effort` is for agent"),
    ({"kind": "script", "schedule": "*:00", "tz": "UTC", "room": "r", "command": "x", "model": "opus"}, "runs no model; `model` is for agent"),
    ({"kind": "agent", "schedule": "*:00", "tz": "UTC", "room": "r", "message": "x", "effort": "turbo"},
     "`effort` must be one of low, medium, high, xhigh, max, or left out for the room's level"),
    ({"kind": "note", "schedule": "*:00", "tz": "UTC", "room": "r"}, "a note needs `message`, the text its room is told"),
])
def test_a_job_that_cannot_be_scheduled_is_named_with_why(raw, why):
    with pytest.raises(JobError, match=why):
        make_job("ok-name", raw)
    with pytest.raises(JobError, match="lowercase"):
        make_job("Not OK", {"kind": "script", "schedule": "*:00", "tz": "UTC", "room": "r", "command": "x"})


# the sync
def test_sync_is_idempotent_shows_the_diff_and_cleans_a_removed_job(tmp_path):
    sd = FakeSystemd(tmp_path / "units")
    path = write_jobs(tmp_path / "private" / "jobs.toml", "# my jobs\n\n"
                      '[jobs.brief]\nkind = "agent"\nschedule = "Mon..Fri 09:00"\ntz = "America/Toronto"\nroom = "r"\n'
                      'account = "two"\ncap = 20\nmessage = "the brief"\n\n'
                      '[jobs.beat]\nkind = "script"\nschedule = "*:0/2"\ntz = "UTC"\nroom = "r"\ndeliver = "silent"\ncommand = "true"\n')
    p, back = jobs.sync(path, sd, ENV)
    assert (p.added, p.changed, p.unchanged, p.removed, p.errors) == (["brief", "beat"], [], [], [], {})
    assert [c for c in sd.calls if c[0] != "write"] == [("reload",), ("enable", "yerk-job-brief.timer"),
                                                         ("enable", "yerk-job-beat.timer")]
    timer, service = sd.read("yerk-job-brief.timer"), sd.read("yerk-job-brief.service")
    assert timer.startswith(jobs.HEADER + "brief.") and "OnCalendar=Mon..Fri 09:00 America/Toronto" in timer
    assert "Persistent=true" in timer and "AccuracySec=1s" in timer and "Unit=yerk-job-brief.service" in timer
    assert "ExecStart=/opt/yerk/bin/python -P -m yerk_ai.jobs run brief" in service and "TimeoutStartSec=120" in service
    assert 'Environment="YERK_PRIVATE=/opt/private"' in service and "WorkingDirectory=/opt/yerk" in service
    beat = sd.read("yerk-job-beat.timer")
    assert "OnCalendar=*:0/2 UTC" in beat and "Persistent=true" in beat     # the line as written, in its zone
    assert "TimeoutStartSec=720" in sd.read("yerk-job-beat.service")          # the 10-minute default cap plus 120 s
    assert set(back) == {"brief", "beat"} and all(r["scheduled"] for r in back.values())
    assert jobs.diff_lines(p)[0] == "+ brief (new): calendar `Mon..Fri 09:00` America/Toronto"
    # the same file again: nothing written, nothing enabled, nothing restarted
    n = len(sd.calls)
    p2, _ = jobs.sync(path, sd, ENV)
    assert (p2.added, p2.changed, p2.unchanged, p2.removed) == ([], [], ["brief", "beat"], []) and len(sd.calls) == n
    assert jobs.diff_lines(p2) == ["= 2 unchanged: brief, beat"]
    # one job changed: its units rewritten and its timer restarted; the other untouched
    path.write_text(path.read_text().replace('schedule = "*:0/2"', 'schedule = "*:0/7"'))
    p3, _ = jobs.sync(path, sd, ENV)
    assert (p3.changed, p3.unchanged) == (["beat"], ["brief"])
    beat = sd.read("yerk-job-beat.timer")
    assert "OnCalendar=*:0/7 UTC" in beat and "Persistent=true" in beat
    assert sd.calls[n:] == [("write", "yerk-job-beat.service"), ("write", "yerk-job-beat.timer"), ("reload",),
                            ("enable", "yerk-job-beat.timer"), ("restart", "yerk-job-beat.timer")]
    # a job removed from the file: disabled, both files gone; a broken one named with why, its unit never written
    path.write_text(path.read_text().split("[jobs.beat]")[0] + '[jobs.broken]\nkind = "script"\nschedule = "0 9 * * *"\n'
                    'tz = "UTC"\nroom = "r"\ncommand = "true"\n')
    n = len(sd.calls)
    p4, back4 = jobs.sync(path, sd, ENV)
    assert p4.removed == ["beat"] and list(p4.errors) == ["broken"] and "not cron" in p4.errors["broken"]
    assert sd.calls[n:] == [("disable", "yerk-job-beat.timer"), ("remove", "yerk-job-beat.timer"),
                            ("remove", "yerk-job-beat.service"), ("reload",)]
    assert sd.ours() == ["brief"] and set(back4) == {"brief"}
    assert jobs.diff_lines(p4) == ["- beat (removed: not in the jobs file)", "= 1 unchanged: brief",
                                   '! broken: cannot be scheduled: `schedule` takes a systemd calendar line now, not cron: replace '
                                   'schedule = "0 9 * * *" with schedule = "*-*-* 9:0:00"']
    # a calendar systemd itself refuses is named too, and a stray timer is named in the read-back
    path.write_text(path.read_text().replace('tz = "UTC"', 'tz = "UTC"').replace("[jobs.broken]", "[jobs.mars]")
                    .replace('schedule = "0 9 * * *"', 'schedule = "*-*-* 09:00"'))
    sd.check = lambda spec: "systemd does not take it"                                           # every spec refused
    p5, _ = jobs.sync(path, sd, ENV)
    assert set(p5.errors) == {"brief", "mars"} and p5.removed == ["brief"] and sd.ours() == []
    assert jobs.diff_lines(p5)[-1] == "! mars: cannot be scheduled: systemd does not take it"
    sd.enabled["yerk-job-stray.timer"] = True
    lines = jobs.status_lines(path, sd)
    assert lines[:2] == [f"{jobs.NOTHING} brief: not scheduled", f"{jobs.NOTHING} mars: not scheduled"]
    assert f"{jobs.NOT_DONE} timer stray: not in the jobs file" in lines
    path.write_text(path.read_text().replace('schedule = "*-*-* 09:00"', 'schedule = "0 9 * * *"'))
    lines = jobs.status_lines(path, sd)
    assert lines[-1] == f"{jobs.NOT_DONE} mars: cannot be scheduled"
    assert all(x[0] in (jobs.DONE, jobs.NOT_DONE, jobs.NOTHING) and len(x) <= jobs.PHONE - 2 for x in lines)   # state first, phone width
    assert jobs.phone("x" * 60) == "x" * 37 + "\N{HORIZONTAL ELLIPSIS}" and jobs.phone("short") == "short"


def test_the_read_back_comes_from_systemd_in_the_jobs_zone(tmp_path):
    sd = FakeSystemd(tmp_path / "units")
    path = write_jobs(tmp_path / "jobs.toml", '[jobs.tokyo]\nkind = "script"\nschedule = "*-*-* 09:00"\ntz = "Asia/Tokyo"\n'
                                              'room = "r"\ncommand = "true"\n')
    p, back = jobs.sync(path, sd, ENV)
    sd.next["yerk-job-tokyo.timer"] = epoch(2026, 10, 19, 9, 0, tz=TOK)
    sd.last["yerk-job-tokyo.timer"] = epoch(2026, 10, 18, 9, 0, tz=TOK)
    now = epoch(2026, 10, 19, 8, 30, tz=TOK)
    back = jobs.readback(p.jobs, sd, now)
    line, = jobs.read_lines(back, p.jobs, now)
    assert line.startswith("tokyo: next Mon 2026-10-19 09:00:00 JST (in 30m 00s), last Sun 2026-10-18 09:00:00 JST; "
                           "calendar `*-*-* 09:00` Asia/Tokyo, room r, script")
    assert jobs.status_lines(path, sd, now) == [f"{jobs.DONE} tokyo: in 30m 00s"]      # the detail is `yerk jobs list`
    sd.enabled.clear()
    assert jobs.status_lines(path, sd, now) == [f"{jobs.NOTHING} tokyo: not scheduled"]

    def broken():
        raise OSError("systemctl --user list-timers exited 1: no manager")
    sd.timers = broken
    assert jobs.status_lines(path, sd, now) == [f"{jobs.NOT_DONE} jobs: the timer list could not be read"]


def test_the_real_adapter_writes_reads_and_removes_unit_files_and_a_removed_timers_stamp(tmp_path, monkeypatch):
    sd = jobs.Systemd(folder=tmp_path / "units", stamps=tmp_path / "stamps")
    states = {f"yerk-job-{s}.service": s for s in ("active", "activating", "deactivating", "inactive", "failed")}
    monkeypatch.setattr(jobs.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 0 if cmd[2:] == ["is-active", "yerk-job-active.service"] else 3, states[cmd[3]] + "\n", ""))
    assert [u for u in states if sd.running(u)] == ["yerk-job-active.service", "yerk-job-activating.service"]
    monkeypatch.undo()
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
    assert jobs.schedule_add(path, sd, ENV, "x", "script", "*:00", "true", "r", "UTC") == \
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
        f'[jobs.{n}]\nkind = "script"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\n{extra}command = {c!r}\n'.replace("'", '"')
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
    write_jobs(priv / "jobs.toml", '[jobs.late]\nkind = "script"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\ncommand = "echo while you were out"\n')

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


def test_a_job_fired_before_the_daemon_listens_is_handed_off_once_it_does_until_the_units_own_limit(setup, tmp_path, monkeypatch):
    """At boot the job timers start a few seconds before yerkd. An agent job's hand-off that finds no daemon listening
    (nothing on the port, or the pid of a yerkd.json from before the box went down) is tried again every few seconds, the
    one-shot still in the file, until the unit's own limit less a margin; then it gives up as before. A daemon that answered
    is not asked again. A script and a note give up at once, as before: the start sweep delivers them late."""
    s = setup
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": Rec()})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    shot = '[jobs.boot]\nkind = "agent"\nat = "2099-01-02 09:00"\ntz = "UTC"\nroom = "r"\nmessage = "the brief"\n'
    path = write_jobs(priv / "jobs.toml", shot)
    jobs.sync(path, sd, ENV)
    monkeypatch.setattr(jobs, "RETRY_EVERY", 0.0, raising=False)          # raising=False: main has none
    calls = []

    def boot(payload):
        calls.append(payload)
        assert "[jobs.boot]" in path.read_text() and sd.ours() == ["boot"]          # not removed while it waits
        if len(calls) == 1:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        if len(calls) == 2:
            raise ProcessLookupError(3, "No such process")
        return {"ok": True}
    assert (jobs.run("boot", priv, state, units=sd, handoff=boot), len(calls)) == (0, 3)
    r, = s.ledger.job_runs(job="boot")
    assert (r["exit"], r["delivery"]) == (None, None) and sd.ours() == [] and "[jobs.boot]" not in path.read_text()
    limit = jobs.start_limit(jobs.make_job("boot", {"kind": "agent", "at": "2099-01-02 09:00", "tz": "UTC", "room": "r",
                                                    "message": "m"}))
    assert limit == 120                                    # the TimeoutStartSec the unit is written with

    def refused(payload):
        calls.append(payload)
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    for ago, waits in ((limit - jobs.RETRY_SPARE + 1, False), (limit - jobs.RETRY_SPARE - 2, True)):
        write_jobs(path, shot)
        jobs.sync(path, sd, ENV)
        calls.clear()
        monkeypatch.setattr(jobs, "RETRY_EVERY", 0.5)
        t0 = time.time()
        assert jobs.run("boot", priv, state, units=sd, handoff=refused, now=t0 - ago) == 1
        assert (len(calls) > 1, time.time() - t0 < 5) == (waits, True) and sd.ours() == []
        r = s.ledger.job_runs(job="boot")[0]
        assert (r["exit"], r["result"], r["delivery"]) == ("not run", "did not start",
                                                           "undelivered: the daemon is not running (URLError)")
    for answered in (urllib.error.HTTPError("http://127.0.0.1:1/control", 500, "boom", {}, None), TimeoutError("slow")):
        write_jobs(path, shot)                              # it answered, or took the request: never asked twice
        calls.clear()

        def took(payload, e=answered):
            calls.append(payload)
            raise e
        assert jobs.run("boot", priv, state, units=sd, handoff=took) == 1 and len(calls) == 1
    for kind, what in (("script", 'command = "echo while you were out"'), ("note", 'message = "the note"')):
        write_jobs(path, f'[jobs.boot]\nkind = "{kind}"\nat = "2099-01-02 09:00"\ntz = "UTC"\nroom = "r"\n{what}\n')
        jobs.sync(path, sd, ENV)
        calls.clear()
        ago = jobs.start_limit(jobs.load(path, 0)[0]["boot"]) - jobs.RETRY_SPARE - 2      # time left to wait, were it an agent
        assert jobs.run("boot", priv, state, units=sd, handoff=refused, now=time.time() - ago) == 1 and len(calls) == 1
        r = s.ledger.job_runs(job="boot")[0]
        assert (r["kind"], r["delivery"], sd.ours()) == (kind, "undelivered: the daemon is not running (URLError)", [])


def test_a_script_run_its_unit_cut_mid_run_is_closed_at_the_next_start_with_the_failed_run_alert(setup, tmp_path):
    """A script run whose unit was stopped mid-run (its start limit, or any stop) never writes its end. The daemon's sweep
    after a restart closes it once its fired time plus its cap plus the margin has passed: the row gets its end and says it
    was cut, and the failed-run alert goes out, once. A run of a job no longer in the file is closed the same way on the
    default script cap, and nothing is sent for it: nobody is left to tell. A run that may still be going and an agent run
    are left as they are."""
    s = setup
    rec, ops = Rec(), Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room(), "ops": s.room("ops")}, {"r": rec, "ops": ops},
                                    conf={"ops_room": "ops"})
    write_jobs(priv / "jobs.toml", '[jobs.slow]\nkind = "script"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\ncap = 1\ncommand = "sleep 600"\n'
                                   '[jobs.think]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\nmessage = "the brief"\n')
    now = time.time()

    def started(job, kind, ago):                          # the row a run writes as it starts (jobs.run), and nothing after
        return s.ledger.job_run_add(job=job, kind=kind, room="r", unit=f"{jobs.UNIT}{job}", planned=now - ago, fired=now - ago)
    cut, going, agent, gone, recent = started("slow", "script", 181), started("slow", "script", 120), \
        started("think", "agent", 999), started("old", "script", 9999), started("old", "script", 700)
    asyncio.run(j.reattach())
    r = s.ledger.job_run_get(cut)
    assert (r["ended"], r["exit"], r["result"], r["delivery"], r["message_ids"]) == (now - 181 + 180, "cut", "cut", "alert", "n1")
    assert ops.notified == [("ops", W.job_line("slow", "failed", result="signal"))]
    assert ops.notified[0][1] == f"{W.NOT_DONE} job slow failed: it was killed"
    r = s.ledger.job_run_get(gone)                          # its job is gone: the default script cap, 10 min plus the margin
    assert (r["ended"], r["exit"], r["result"], r["delivery"], r["message_ids"]) == (now - 9999 + 720, "cut", "cut", "none", None)
    assert [s.ledger.job_run_get(i)["ended"] for i in (going, agent, recent)] == [None, None, None]
    asyncio.run(j.reattach())                                                     # closed once, told once
    assert len(ops.notified) == 1 and rec.notified == rec.sent == []


def test_an_agent_job_becomes_a_worker_with_the_right_account_and_cap_and_relays(setup, tmp_path):
    s = setup
    rec = Rec()
    room = s.room()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": room}, {"r": rec})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    write_jobs(priv / "jobs.toml",
               '[jobs.brief]\nkind = "agent"\nschedule = "*-*-* 09:00"\ntz = "UTC"\nroom = "r"\naccount = "two"\ncap = 7\n'
               'message = "FAKE report=\\"Three things happened\\" result=\\"Three things happened today\\""\n\n'
               '[jobs.hush]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\n'
               'message = "FAKE report=\\"NO_REPLY\\" result=\\"NO_REPLY\\""\n\n'
               '[jobs.mute]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\ndeliver = "silent"\n'
               'message = "FAKE report=\\"kept\\" result=\\"kept to the ledger\\""\n\n'
               '[jobs.crash]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\nmessage = "FAKE report=\\"half\\" exit=3"\n')
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
    card = {k.removeprefix("worker:"): t for k, t, _, _ in rec.settled}     # its card settles quiet, never the token
    assert card[hush["id"]].splitlines()[0] == f"{W.DONE} worker {W.QUIET_WORKER}" and W.NO_REPLY not in card[hush["id"]]
    assert card[brief["id"]].splitlines()[:2] == [f"{W.DONE} worker finished clean", "Three things happened"]   # as before
    assert (mute["relay"], s.ledger.job_run_get(run_mute)["delivery"]) == ("skipped", "silent (deliver = silent)")
    assert crash["exit"] == "died" and s.ledger.job_run_get(run_crash)["delivery"] == "alert"
    assert rec.notified == [("r", f"{W.NOT_DONE} job crash failed: it ended with an error")]
    assert s.ledger.job_run_get(run_crash)["note"] == f"worker {crash['id']}: exit-code, exited 3"
    assert len(s.chan.replies) == 1                      # the one relay; the other three posted nothing


def test_quiet_job_cards_name_their_job_and_show_the_reports_line_so_each_run_is_told_apart(setup, tmp_path, capsys):
    """need-2026-11-16-100: a room's six quiet job cards read alike ("worker finished clean, nothing to add", the time,
    "report saved"): four runs of a forecast job whose reports each held its one line, two of another job with nothing, all
    answering NO_REPLY. The job brief says the card shows REPORT.md's line, yet a quiet card hid it and named no job, and
    on a phone the report command is out of reach. Fired as jobs, each card now names its job, a quiet run with a line in
    its report shows that line whole, and one with nothing says so as before; neither asks its room for a turn, and the
    worker list and report name the job, in the tool's answer and `yerk worker list|report`. The job's brief asks every run
    for that line, a NO_REPLY run's too, where it said to write the same text as the last message: the token itself."""
    s = setup
    rec = Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": rec})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    write_jobs(priv / "jobs.toml",
               '[jobs.rain-odds]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\n'
               'message = "FAKE report=\\"Rain at the coast on Saturday: median 12 mm (5-95: 3 to 30)\\" result=\\"NO_REPLY\\""\n\n'
               '[jobs.calm-check]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\n'
               'message = "FAKE report=\\"NO_REPLY\\" result=\\"NO_REPLY\\""\n')
    jobs.sync(priv / "jobs.toml", sd, ENV)

    async def go():
        loop = asyncio.get_running_loop()
        w.begin(loop)
        handoff = lambda p: asyncio.run_coroutine_threadsafe(j.handle("", p["job"]), loop).result(30)  # noqa: E731
        out = {}
        for name in ("rain-odds", "calm-check"):
            assert await asyncio.to_thread(jobs.run, name, priv, state, sd, handoff) == 0
            out[name] = await ended(s.ledger, s.ledger.job_runs(job=name)[0]["worker"])
            await asyncio.sleep(0.2)                     # the hook's ledger writes after the worker row
        return out
    rows = asyncio.run(go())
    rain, calm = rows["rain-odds"]["id"], rows["calm-check"]["id"]
    prompt = (Path(rows["calm-check"]["staging"]) / "PROMPT.md").read_text()   # a line on every run, a quiet one's too
    assert f"one-line result (what it found or did) as the first line of REPORT.md in your folder, even when your last " \
           f"message is {W.NO_REPLY}: the card shows it with the job's name." in prompt
    card = {k.removeprefix("worker:"): t.splitlines() for k, t, _, _ in rec.settled}
    assert card[rain][:3] == [f"{W.DONE} worker finished clean", "Rain at the coast on Saturday: median 12", "mm (5-95: 3 to 30)"]
    assert card[rain][3].startswith("-# job rain-odds, done in ") and card[rain][-1] == f"-# report saved: `yerk worker report {rain}`"
    assert card[calm][0] == f"{W.DONE} worker {W.QUIET_WORKER}" and card[calm][1].startswith("-# job calm-check, done in ")
    assert all(W.width(x) <= W.PHONE and x != W.NO_REPLY for t in card.values() for x in t)
    assert s.chan.replies == []                          # neither asked its room for a turn: silence, as before
    capsys.readouterr()
    for name, wid in (("rain-odds", rain), ("calm-check", calm)):
        assert (rows[name]["job"], rows[name]["relay"], rows[name]["relay_note"]) == (name, "skipped", "the job answered NO_REPLY")
        assert s.ledger.job_runs(job=name)[0]["delivery"] == "silent (NO_REPLY)"
        assert asyncio.run(w.handle("r", {"action": "report", "id": wid}))["job"] == name          # the worker tool's report
        assert cli.main(["worker", "report", wid, "--state", str(state)]) == 0
        assert capsys.readouterr().out.startswith(f"worker {wid} done, job {name}, account ")
    listed = asyncio.run(w.handle("r", {"action": "list"}))["workers"]                             # the worker tool's list
    assert {x["id"]: x["job"] for x in listed} == {rain: "rain-odds", calm: "calm-check"}
    assert cli.main(["worker", "list", "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert f"{rain} done for job rain-odds in r on " in out and f"{calm} done for job calm-check in r on " in out


def test_a_job_workers_end_for_memory_reads_as_its_card_reads_it(setup, tmp_path):
    rec, told = Rec(), []
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": setup.room()}, {"r": rec})

    async def relay(row, end, text=None, after=None):
        told.append(text)
        return ""
    w.relay = relay
    ends = [{"exit": "died", "result_code": "signal", "status": "KILL", "oom_kill": 1},       # the main process, for memory
            {"exit": "timed out", "result_code": "timeout", "status": "TERM", "oom_kill": 2},
            {"exit": "done", "result_code": "success", "status": "0", "oom_kill": 1}]
    for i, end in enumerate(ends):
        (tmp_path / f"w{i}").mkdir()
        asyncio.run(j.worker_done({"id": f"w{i}", "job": "nightly", "room": "r", "staging": str(tmp_path / f"w{i}"), "started": 1000.0,
                                   "cap": 600.0}, {**end, "finished": 1060.0, "result": "", "detail": ""}))
    assert rec.notified == [("r", f"{W.NOT_DONE} job nightly failed: it ran out of memory"),
                            ("r", W.job_line("nightly", "timed out", 600.0) + "\n" + W.OOM_LOST[1].format(n=2))]
    assert f"ended done after 1m 00s; {W.OOM_LOST[0]}. Its message for the room" in told[0]


def test_an_agent_jobs_worker_runs_at_the_jobs_own_model_and_thinking_level_not_the_rooms(setup, tmp_path, capsys):
    """need-2026-11-09-79: a room on max thinking started a bot's research runs as standing agent jobs (deliver silent), and
    every job's worker took the room's level, since neither the schedule tool nor `yerk schedule add` had a model or a
    thinking level: a run at max cost about eight times the same run at high. An agent job now sets its worker's `effort` and
    `model` (an alias or a full id on the install's list; an alias is kept by its name, so the job follows the list, and the
    worker gets its full id); a job that sets neither runs on the room's live ones as before. A model off the list, a level
    that is not one, and either on a note are refused, the file left as it was; the list says what each job sets."""
    s = setup
    rec = Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": rec})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    w.models = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5"}
    (s.tmp / "rooms" / "r").mkdir(parents=True)
    (s.tmp / "rooms" / "r" / "override.json").write_text(json.dumps({"effort": "max"}))      # the room's own level: max
    path = write_jobs(priv / "jobs.toml", "# the owner's comment stays\n")
    add = lambda **k: asyncio.run(j.handle("r", {"action": "add", "kind": "agent", "when": "Mon..Fri 09:00", **k}))  # noqa: E731
    lean = add(name="lean", what='FAKE report="NO_REPLY" result="NO_REPLY"', deliver="silent", model=" Sonnet", effort="HIGH ")
    assert lean["ok"] and (lean["model"], lean["effort"]) == ("sonnet", "high"), lean
    plain = add(name="plain", what='FAKE report="NO_REPLY" result="NO_REPLY"', deliver="silent")
    assert plain["ok"] and "model" not in plain and "effort" not in plain
    assert ('[jobs.lean]\nkind = "agent"\nschedule = "Mon..Fri 09:00"\ntz = "UTC"\nroom = "r"\nmodel = "sonnet"\neffort = "high"\n'
            'deliver = "silent"\n') in path.read_text() and "[jobs.plain]" in path.read_text()
    before = path.read_bytes()
    assert add(name="odd", what="x", model="gpt-9") == {"ok": False, "error": W.NOT_LISTED.format(
        model="gpt-9", models="opus (Opus 5.5), sonnet (Sonnet 5.5)")}
    assert add(name="odd", what="x", effort="turbo")["error"].startswith("`effort` must be one of low, medium, high, xhigh, max")
    assert add(name="odd", kind="note", what="x", effort="low")["error"].startswith("a note runs nothing, so it takes no `effort`")
    assert path.read_bytes() == before and sd.ours() == ["lean", "plain"]
    listed = asyncio.run(j.handle("r", {"action": "list"}))
    assert [x.split("; ", 1)[1] for x in listed["lines"]] == ["calendar `Mon..Fri 09:00` UTC, room r, agent, model sonnet, effort high",
                                                              "calendar `Mon..Fri 09:00` UTC, room r, agent"]
    assert [(x["name"], x.get("model"), x.get("effort")) for x in listed["jobs"]] == [("lean", "sonnet", "high"), ("plain", None, None)]

    async def go():
        loop = asyncio.get_running_loop()
        w.begin(loop)
        handoff = lambda p: asyncio.run_coroutine_threadsafe(j.handle("", p["job"]), loop).result(30)  # noqa: E731
        rows = {}
        for name in ("lean", "plain"):
            assert await asyncio.to_thread(jobs.run, name, priv, state, sd, handoff) == 0
            rows[name] = await ended(s.ledger, s.ledger.job_runs(job=name)[0]["worker"])
        return rows
    rows = asyncio.run(go())
    for name, model, level in (("lean", "claude-sonnet-5-5", "high"), ("plain", "m", "max")):     # the job's own, else the room's
        argv = json.loads((Path(rows[name]["staging"]) / "fake-record.json").read_text())["argv"]
        assert (argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]) == (model, level), (name, argv)
        assert (rows[name]["model"], rows[name]["job"], rows[name]["relay"]) == (model, name, "skipped")
        assert json.loads((Path(rows[name]["staging"]) / "worker.json").read_text())["effort"] == level
    # `yerk schedule add` takes the same two, checked against the install's list
    capsys.readouterr()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(jobs, "Systemd", lambda: sd)
    monkey.setattr(cli, "rooms", lambda p: {"r": {"timezone": "UTC"}})
    try:
        rc = cli.main(["schedule", "add", "cli-lean", "--when", "*:0/45", "--what", "look", "--room", "r", "--model", "opus",
                       "--effort", "low", "--private", str(priv), "--state", str(state)])
        odd = cli.main(["schedule", "add", "cli-odd", "--when", "*:0/45", "--what", "look", "--room", "r", "--model", "gpt-9",
                        "--private", str(priv), "--state", str(state)])
    finally:
        monkey.undo()
    out, err = capsys.readouterr()
    assert (rc, odd) == (0, 1)
    assert out.startswith("cli-lean: calendar `*:0/45` UTC, agent job, room r, deliver announce, model opus, effort low; next ")
    assert err.startswith("not on the list: gpt-9\nthe list: opus (") and "cli-odd" not in path.read_text()
    assert jobs.load(path)[0]["cli-lean"].runs_on() == {"model": "opus", "effort": "low"}


def test_a_one_shot_is_removed_from_the_file_and_its_units_go_after_it_fires(setup, tmp_path):
    s = setup
    rec = Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": rec})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    path = write_jobs(priv / "jobs.toml", "# keep this comment\n\n[jobs.keep]\nkind = \"script\"\nschedule = \"*:00\"\ntz = \"UTC\"\nroom = \"r\"\n"
                      "command = \"true\"\n\n[jobs.once]  # the reminder\nkind = \"script\"\nat = \"2099-01-02 09:00\"\n"
                      "tz = \"UTC\"\nroom = \"r\"\ncommand = \"echo Reminder: it is time\"\n")
    jobs.sync(path, sd, ENV)
    assert sd.ours() == ["keep", "once"]
    rc = jobs.run("once", priv, state, units=sd, handoff=lambda p: asyncio.run(j.handle("", p["job"])))
    assert rc == 0 and rec.sent[0][1] == "Reminder: it is time"
    assert path.read_text() == "# keep this comment\n\n[jobs.keep]\nkind = \"script\"\nschedule = \"*:00\"\ntz = \"UTC\"\nroom = \"r\"\ncommand = \"true\"\n"
    assert sd.ours() == ["keep"] and ("disable", "yerk-job-once.timer") in sd.calls and sd.read("yerk-job-once.service") is None
    r, = s.ledger.job_runs(job="once")
    assert r["planned"] == epoch(2099, 1, 2, 9, 0, tz=UTC) and r["delivery"] == "delivered"
    # a job whose timer fires after it left the file: said, ledgered, exit 2
    assert jobs.run("gone", priv, state, units=sd, handoff=lambda p: {"ok": True}) == 2
    assert s.ledger.job_runs(job="gone")[0]["result"] == "no such job: not in the jobs file"


def test_a_sync_while_a_one_shot_runs_keeps_it_and_its_units_until_its_own_run_takes_them_out(setup, tmp_path):
    """Two one-shots at one minute: the first to finish syncs while the other still runs. That one keeps its place in the
    file and both its units (counted unchanged, never checked for a next fire), so systemd does not stop it; its own run
    takes it out. A past one-shot whose own unit is not running (its run was cut) is refused and its units go, as before
    this fix; so is one with no units."""
    s = setup
    rec = Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": rec})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    at = datetime.fromtimestamp(time.time() - 120, UTC).strftime("%Y-%m-%d %H:%M")
    shot = '[jobs.{n}]\nkind = "script"\nat = "' + at + '"\ntz = "UTC"\nroom = "r"\ncommand = "echo {n}"\n'
    path = write_jobs(priv / "jobs.toml", shot.format(n="first") + "\n" + shot.format(n="second"))
    jobs.sync(path, sd, ENV, now=time.time() - HOUR)              # written while both were still to come
    second, n = (sd.read("yerk-job-second.timer"), sd.read("yerk-job-second.service")), len(sd.calls)
    checked = []
    sd.check = lambda spec: checked.append(spec) or f"{spec!r} never fires again (it is in the past)"
    handoff = lambda payload: asyncio.run(j.handle("", payload["job"]))  # noqa: E731  (the daemon's /control)
    sd.active = {"yerk-job-first.service", "yerk-job-second.service"}           # both units running
    assert jobs.run("first", priv, state, units=sd, handoff=handoff) == 0
    assert sd.ours() == ["second"] and (sd.read("yerk-job-second.timer"), sd.read("yerk-job-second.service")) == second
    assert "[jobs.second]" in path.read_text() and "[jobs.first]" not in path.read_text()
    assert not [c for c in sd.calls[n:] if "second" in str(c)] and checked == []      # not disabled, removed or rewritten
    sd.next["yerk-job-second.timer"], sd.last["yerk-job-second.timer"] = None, time.time() - 120     # fired: no next
    assert jobs.status_lines(path, sd) == [f"{W.WORKING} second: running"]             # yerk status reads it as the sync
    listed, told = jobs.schedule_list(path, sd), asyncio.run(j.handle("", {"action": "list"}))     # the command, the tool
    assert listed["lines"] == told["lines"] == [f"second: running; {jobs.load(path, 0)[0]['second'].describe()}, room r, script"]
    assert listed["errors"] == told["errors"] == {} and [r["name"] for r in listed["jobs"] if not r.get("stray")] == ["second"]
    p, _ = jobs.sync(path, sd, ENV)                                # any other sync keeps it too
    assert (p.unchanged, p.removed, p.errors, checked) == (["second"], [], {}, [])
    assert jobs.run("second", priv, state, units=sd, handoff=handoff) == 0
    assert sd.ours() == [] and "[jobs.second]" not in path.read_text() and [t for _, t, _ in rec.sent] == ["first", "second"]
    write_jobs(path, shot.format(n="cut"))                         # its run was cut (a box shutdown): its unit is not running
    sd.check = lambda spec: ""                                     # written while it was still to come
    jobs.sync(path, sd, ENV, now=time.time() - HOUR)
    assert sd.ours() == ["cut"]
    assert jobs.status_lines(path, sd) == [f"{W.NOT_DONE} timer cut: not in the jobs file", f"{W.NOT_DONE} cut: cannot be scheduled"]
    n = len(sd.calls)
    p, _ = jobs.sync(path, sd, ENV)                                # the next sync removes it as before
    assert (p.removed, p.unchanged, sd.ours()) == (["cut"], [], []) and p.errors["cut"].endswith("is in the past")
    assert sd.calls[n:] == [("disable", "yerk-job-cut.timer"), ("remove", "yerk-job-cut.timer"),
                            ("remove", "yerk-job-cut.service"), ("reload",)]
    write_jobs(path, shot.format(n="late"))                        # never scheduled: refused as before
    p, _ = jobs.sync(path, sd, ENV)
    assert p.errors["late"].endswith("is in the past") and (p.unchanged, sd.ours()) == ([], [])


def test_a_job_fired_in_the_instant_a_one_shot_takes_itself_out_of_the_jobs_file_still_starts(setup, tmp_path, monkeypatch):
    """need-2026-11-16-101: a one-shot's run ended and took itself out of the jobs file in the second a script started
    another job's service. That job's runner found it and ledgered its run, but the daemon's fire answered "no job": add
    and remove wrote the file in place, and load, which fire calls, takes no lock, so it read the file empty or cut, every
    job gone. Each new text is now swapped in whole: a fire that lands while the one-shot's run rewrites the file finds
    its job in the file as it was and starts the worker, and the one-shot still leaves the file and its units go."""
    s = setup
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": Rec()})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    path = write_jobs(priv / "jobs.toml", '[jobs.steady]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\n'
                      'message = "FAKE report=\\"checked\\" result=\\"NO_REPLY\\""\n\n'
                      '[jobs.once]\nkind = "script"\nat = "2099-01-02 09:00"\ntz = "UTC"\nroom = "r"\ncommand = "true"\n')
    jobs.sync(path, sd, ENV)
    swap, fired = os.replace, []

    async def go():
        loop = asyncio.get_running_loop()
        w.begin(loop)
        handoff = lambda p: asyncio.run_coroutine_threadsafe(j.handle("", p["job"]), loop).result(30)  # noqa: E731

        def mid_rewrite(src, dst):               # the one-shot's run takes it out of the file: steady fires in that instant
            if Path(dst) == path.resolve() and not fired:
                fired.append((jobs.run("steady", priv, state, sd, handoff), "[jobs.once]" in path.read_text()))
            return swap(src, dst)
        monkeypatch.setattr(os, "replace", mid_rewrite)
        assert await asyncio.to_thread(jobs.run, "once", priv, state, sd, handoff) == 0
        assert fired == [(0, True)]                       # it fired mid-rewrite, the file as it was until the swap
        run, = s.ledger.job_runs(job="steady")
        assert (run["exit"], run["result"], run["delivery"]) == (None, None, None) and run["worker"]   # started, not "no job"
        return run, await ended(s.ledger, run["worker"])
    run, worker = asyncio.run(go())
    assert (worker["id"], worker["job"], worker["exit"]) == (run["worker"], "steady", "done")
    assert "[jobs.once]" not in path.read_text() and "[jobs.steady]" in path.read_text() and sd.ours() == ["steady"]


def test_each_rewrite_of_the_jobs_file_is_swapped_in_whole_so_a_read_at_any_instant_finds_every_job(tmp_path):
    """need-2026-11-16-101: load, which the daemon's fire, a job's runner and a sync each call, takes no lock, and add and
    remove wrote the file in place, so a read in the instant of a rewrite found it empty or cut. Each new text is now
    written beside the file and swapped in with one rename: a read that had the file open still reads it whole, a reader
    beside a hundred rewrites never misses a job, a rewrite that fails leaves the file as it was with no temp file beside
    it, the file is 0600 as every private file, and a jobs file that is a symlink keeps its link."""
    keep = '[jobs.steady]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\nmessage = "m"\n'
    path = write_jobs(tmp_path / "private" / "jobs.toml", keep)
    path.chmod(0o644)
    shot = jobs.fields_for("script", "2099-01-02 09:00", "true", "r", "UTC")
    for change in (lambda: jobs.add(path, "once", shot), lambda: jobs.remove(path, "once")):
        before = path.read_text()
        with open(path) as held:                         # a read that had the file open when the rewrite began
            change()
            assert held.read() == before                 # never emptied or cut under it
    assert path.read_text() == keep and stat.S_IMODE(path.stat().st_mode) == 0o600
    stop, ready, reads, missed = threading.Event(), threading.Event(), [0], []

    def read():                                          # as the daemon's fire, a runner and a sync read it
        while not stop.is_set():
            reads[0] += 1
            if "steady" not in jobs.load(path, now=0)[0]:
                missed.append(reads[0])
            ready.set()
    reader = threading.Thread(target=read)
    reader.start()
    try:
        assert ready.wait(10)
        for _ in range(100):
            jobs.add(path, "once", shot)
            jobs.remove(path, "once")
    finally:
        stop.set()
        reader.join()
    assert (missed, path.read_text()) == ([], keep), f"{len(missed)} of {reads[0]} reads found no job"
    with pytest.raises(UnicodeEncodeError):              # a rewrite that fails partway: the file as it was, no temp file
        jobs.rewrite(path, keep + 'note = "\ud800"\n')
    assert path.read_text() == keep and sorted(p.name for p in path.parent.iterdir()) == [".jobs.toml.lock", "jobs.toml"]
    real = write_jobs(tmp_path / "kept" / "jobs.toml", keep)       # a symlinked jobs file: the file it points at is swapped
    (link := tmp_path / "linked" / "jobs.toml").parent.mkdir()
    link.symlink_to(real)
    jobs.add(link, "once", shot)
    assert link.is_symlink() and "[jobs.once]" in real.read_text() and link.read_text() == real.read_text()


# notes: a reminder its room's own session tells
NOTE = 'kind = "note"\nat = "2099-01-03 10:00"\ntz = "UTC"\nroom = "r"\nmessage = "Remind them to call the clinic."\n'


async def settled(ledger, run_id, timeout=20.0):
    """The run's row once its told turn has ended, delivered or failed."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if (r := ledger.job_run_get(run_id)) and r["delivery"] in ("delivered", "failed"):
            return r
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} was not told: {ledger.job_run_get(run_id)}")


def test_a_message_toml_cannot_hold_raw_is_written_so_the_file_still_parses_and_no_other_timer_goes(tmp_path):
    """Any room writes a note's message now. A DEL in it made the jobs file unparseable, so the add's own sync took
    every other job's timer away; a lone surrogate truncated the file to nothing. Both are written so the file parses."""
    keep = '[jobs.keep]\nkind = "script"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\ncommand = "true"\n'
    path, sd = write_jobs(tmp_path / "jobs.toml", keep), FakeSystemd(tmp_path / "units")
    jobs.sync(path, sd, ENV)
    for name, text in (("del", "call\x7f the clinic"), ("half", "call \ud800 the clinic")):
        assert jobs.schedule_add(path, sd, ENV, name, "note", "2099-01-03 10:00", text, "r", "UTC")["ok"], name
    assert sd.ours() == ["del", "half", "keep"] and path.read_text().startswith(keep)
    assert [j.payload for j in jobs.load(path)[0].values()] == ["true", "call\x7f the clinic", "call ? the clinic"]


def test_a_note_builds_syncs_and_reads_back_like_any_timed_job(tmp_path):
    note = make_job("call-clinic", {"kind": "note", "at": "2099-01-03 10:00", "tz": "UTC", "room": "r",
                                    "message": "Remind them to call the clinic."})
    assert (note.kind, note.payload, note.cap, note.account) == ("note", "Remind them to call the clinic.", 0.0, "")
    path, sd = write_jobs(tmp_path / "jobs.toml", f"[jobs.call-clinic]\n{NOTE}"), FakeSystemd(tmp_path / "units")
    p, back = jobs.sync(path, sd, ENV)
    assert p.added == ["call-clinic"] and back["call-clinic"]["scheduled"]
    assert "OnCalendar=2099-01-03 10:00:00 UTC" in sd.read("yerk-job-call-clinic.timer")
    assert "TimeoutStartSec=120" in sd.read("yerk-job-call-clinic.service")          # the step only hands it on
    assert jobs.read_lines(back, p.jobs)[0].endswith("; one-shot Sat 2099-01-03 10:00:00 UTC, room r, note")
    assert jobs.sync(path, sd, ENV)[0].unchanged == ["call-clinic"]
    assert 'or "note" (a reminder' in jobs.KEYS["kind"] and "Notes: the text" in jobs.KEYS["message"]   # yerk explain jobs


def test_a_note_is_told_by_its_rooms_own_session_and_then_leaves_the_file(setup, tmp_path):
    """No worker, no shell, no post as is: the runner keeps the text in the run's folder and hands the run on, and
    the daemon asks one harness turn of the room's own session; that turn's end is the run's delivery. A one-shot
    then leaves the file like any one-shot."""
    s = setup
    rec = Rec()
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": rec})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    path = write_jobs(priv / "jobs.toml", f'[jobs.keep]\nkind = "script"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\ncommand = "true"\n\n'
                      f"[jobs.call-clinic]\n{NOTE}")
    jobs.sync(path, sd, ENV)

    async def go():
        loop = asyncio.get_running_loop()
        w.begin(loop)
        handoff = lambda p: asyncio.run_coroutine_threadsafe(j.handle("", p["job"]), loop).result(30)  # noqa: E731
        rc = await asyncio.to_thread(jobs.run, "call-clinic", priv, state, sd, handoff)
        return rc, await settled(s.ledger, s.ledger.job_runs(job="call-clinic")[0]["id"])
    rc, r = asyncio.run(go())
    assert rc == 0 and (r["kind"], r["exit"], r["result"], r["delivery"], r["message_ids"]) == ("note", "done", "note", "delivered", "m1")
    (reply, inbound), = s.chan.replies                               # the fake room says back what it was told
    assert inbound == [f"note-{r['id']}"] and reply.startswith("echo: [reminder call-clinic due] Remind them to call the clinic.\n")
    assert "do what it says now and tell the room in your own voice" in reply
    assert (Path(r["folder"]) / "message").read_text() == "Remind them to call the clinic."      # the receipt
    assert rec.sent == rec.notified == s.units.calls == []          # nothing posted as is, no alert, no worker
    assert "call-clinic" not in path.read_text() and sd.ours() == ["keep"]


def test_a_note_the_daemon_missed_is_told_at_the_next_start_and_one_already_said_is_not_said_again(setup, tmp_path):
    """A note that fires with no daemon up waits in the ledger as undelivered with its text in the run's folder, as a
    script's output does, and is told at the next start; one whose reply reached the room before a restart cut its
    turn's end is marked delivered there, and one fired after the start is this process's, so never said twice."""
    s = setup
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": Rec()})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    write_jobs(priv / "jobs.toml", '[jobs.water]\nkind = "note"\nschedule = "0/2:00"\ntz = "UTC"\nroom = "r"\nmessage = "Water the plants."\n')
    jobs.sync(priv / "jobs.toml", sd, ENV)

    def down(payload):
        raise OSError("connection refused")
    assert jobs.run("water", priv, state, units=sd, handoff=down) == 1
    rid = s.ledger.job_runs(job="water")[0]["id"]
    assert s.ledger.job_run_get(rid)["delivery"] == "undelivered: the daemon is not running (OSError)"

    async def go():
        w.begin(asyncio.get_running_loop())
        j.started = time.time()                                     # a daemon start
        await j.reattach()
        first = await settled(s.ledger, rid)
        while w.asked.get("r"):                                     # the told turn hands the room back
            await asyncio.sleep(0.01)
        turn = s.ledger.inbound_get(f"note-{rid}")["turn_id"]        # its reply posted, then a restart before the
        key = s.ledger.db.execute("SELECT turn_key FROM turns WHERE id = ?", (turn,)).fetchone()[0]    # end was ledgered
        s.ledger.delivery_plan(f"{key}:0", "r", "reply", "said", f"note-{rid}")
        s.ledger.delivery_set(f"{key}:0", state="sent", message_id="d1")
        s.ledger.job_run_set(rid, delivery="undelivered: waiting for the room's turn")
        j.started = time.time()                                     # another start
        await j.reattach()
        again = s.ledger.job_run_get(rid)
        assert jobs.run("water", priv, state, units=sd, handoff=lambda p: {"ok": True}) == 0     # fired after the start:
        mine = max(r["id"] for r in s.ledger.job_runs(job="water"))                               # its turn is in hand
        s.ledger.job_run_set(mine, delivery="undelivered: waiting for the room's turn")
        await j.reattach()
        assert not w.waiting.get("r") and "r" not in w.asked and len(s.chan.replies) == 1   # no turn queued, running or said
        return first, again
    first, again = asyncio.run(go())
    assert first["delivery"] == "delivered" and s.chan.replies[0][0].startswith("echo: [reminder water due] Water the plants.")
    assert (again["delivery"], again["message_ids"]) == ("delivered", "d1") and len(s.chan.replies) == 1


def test_a_note_whose_hand_off_failed_after_the_start_is_told_by_that_start(setup, tmp_path):
    """Jobs.started is stamped in the daemon's __init__, before /control answers, so a note that fires in between fails
    its hand-off with a fire time after the start (a reboot fires Persistent timers as the daemon starts). No process
    has it in hand, so that start's reattach tells it; only a note this process told waits on its own turn."""
    s = setup
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": Rec()})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    write_jobs(priv / "jobs.toml", '[jobs.water]\nkind = "note"\nschedule = "0/2:00"\ntz = "UTC"\nroom = "r"\nmessage = "Water the plants."\n')
    jobs.sync(priv / "jobs.toml", sd, ENV)
    j.started = time.time() - 1                                     # the start, before /control answers

    def down(payload):
        raise OSError("connection refused")
    assert jobs.run("water", priv, state, units=sd, handoff=down) == 1
    rid = s.ledger.job_runs(job="water")[0]["id"]

    async def go():
        w.begin(asyncio.get_running_loop())
        await j.reattach()
        return await settled(s.ledger, rid)
    assert asyncio.run(go())["delivery"] == "delivered"
    assert s.chan.replies[0][0].startswith("echo: [reminder water due] Water the plants.")


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
    assert add(name="weekdays", when="Mon..Fri 09:00", what="the brief", room="q")["tz"] == "America/Toronto"
    assert add(name="paris", when="0/2:00", what="the brief", tz="Europe/Paris", deliver="silent", cap_minutes=3)["tz"] == "Europe/Paris"
    j.daemon_conf = {}
    assert "give `tz`" in add(name="nozone", when="*-*-* 09:00", what="x", room="q")["error"]
    assert "nozone" not in path.read_text()
    paris = jobs.load(path)[0]["paris"]
    assert (paris.when, paris.spec, paris.deliver, paris.cap, paris.kind) == ("calendar", "0/2:00", "silent", 3.0, "agent")
    # list reads the timers back; a repeated name and a bad `when` leave the file as it was
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert "already in the file" in add(name="remind", when="2099-05-05 10:00", what="x", kind="script")["error"]
    assert add(name="odd", when="0 9 * * 1-5", what="x")["error"] == ('`schedule` takes a systemd calendar line now, not cron: '
                                                                    'replace schedule = "0 9 * * 1-5" with schedule = "Mon..Fri *-*-* 9:0:00"')
    assert "not a time zone" in add(name="odd", when="*-*-* 09:00", what="x", tz="Mars/Olympus")["error"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    listed = asyncio.run(j.handle("r", {"action": "list"}))
    assert [x["name"] for x in listed["jobs"]] == ["remind", "weekdays", "paris"] and all(x["scheduled"] for x in listed["jobs"])
    assert listed["lines"][0].startswith("remind: next ") and "JST" in listed["lines"][0]
    # remove: the block goes, the rest of the file byte for byte
    kept = path.read_text()
    out = asyncio.run(j.handle("r", {"action": "remove", "name": "weekdays"}))
    assert out["ok"] and "- weekdays (removed" in out["diff"][0] and sd.ours() == ["paris", "remind"]
    assert path.read_text() == kept.replace(kept[kept.index("\n[jobs.weekdays]"):kept.index("\n[jobs.paris]")], "")
    assert not asyncio.run(j.handle("r", {"action": "remove", "name": "weekdays"}))["ok"]
    # the same three actions from the command line, on the same file and timers
    monkey = pytest.MonkeyPatch()
    monkey.setattr(jobs, "Systemd", lambda: sd)
    monkey.setattr(cli, "rooms", lambda p: {"r": {"timezone": "Asia/Tokyo"}})
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["schedule", "add", "cli-job", "--when", "*:0/45", "--what", "echo cli", "--kind", "script",
                           "--room", "r", "--private", str(priv), "--state", str(state)])
            rc2 = cli.main(["schedule", "list", "--private", str(priv), "--state", str(state)])
            rc3 = cli.main(["jobs", "sync", "--private", str(priv), "--state", str(state)])
            rc4 = cli.main(["schedule", "remove", "cli-job", "--private", str(priv), "--state", str(state)])
    finally:
        monkey.undo()
    text = buf.getvalue()
    assert (rc, rc2, rc3, rc4) == (0, 0, 0, 0)
    assert text.startswith("cli-job: calendar `*:0/45` Asia/Tokyo, script job, room r, deliver announce; next ") and "cli-job: next" in text
    assert "= 3 unchanged: remind, paris, cli-job" in text and "cli-job: removed" in text and "cli-job" not in path.read_text()


def test_a_room_with_people_keeps_its_own_notes_and_every_other_job_stays_the_owners(tmp_path):
    """Seen live: a person's room could not set the reminder they asked for, since every job ran as the owner. A
    room that is not the owner's now adds, lists and removes its own notes, and no answer names another job; an agent
    or script job, or a note for another room, gets the line that says what it can do instead; the owner's
    rooms and `yerk schedule` keep every form."""
    from test_room_scope import install
    y, mom = install(tmp_path, family=True), "family-room"
    y.jobs.units = sd = FakeSystemd(tmp_path / "units")
    path = y.jobs.path

    def ask(caller, **req):
        return asyncio.run(y.tool_schedule(caller, req))
    note = {"action": "add", "name": "trip-deals", "kind": "note", "when": "2099-01-03 10:00",
            "what": "Tell them what was signed on the example trip, and whether any of it ties to the example fund."}
    out = ask(mom, **note)
    assert out["ok"] and (out["kind"], out["room"], out["tz"]) == ("note", mom, "UTC") and "diff" not in out
    assert jobs.load(path)[0]["trip-deals"].room == mom and sd.read("yerk-job-trip-deals.timer")
    for req in ({**note, "name": "x", "kind": "agent"}, {**note, "name": "x", "kind": "script", "what": "echo hi"},
                {**note, "name": "x", "room": "example-room"}, {"action": "sync"}):
        assert ask(mom, **req) == {"ok": False, "line": W.JOBS_OWNER_ONLY}, req
    assert "[jobs.x]" not in path.read_text()
    assert W.JOBS_OWNER_ONLY.splitlines()[0] == "not scheduled: only this room's notes"        # state first, what it can do
    monkey = pytest.MonkeyPatch()
    monkey.setattr(jobs, "Systemd", lambda: sd)
    try:
        with redirect_stdout(io.StringIO()):
            rc = cli.main(["schedule", "add", "nightly", "--kind", "agent", "--when", "0/2:00", "--what", "look",
                           "--room", mom, "--private", str(path.parent), "--state", str(tmp_path / "state")])
    finally:
        monkey.undo()
    assert rc == 0 and jobs.load(path)[0]["nightly"].kind == "agent"                # `yerk schedule`: every kind, any room
    listed = ask(mom, action="list")                    # its notes alone: no job of the owner's, not even one for this room
    assert [x["name"] for x in listed["jobs"]] == ["trip-deals"] and listed["errors"] == {}
    assert not [x for x in listed["lines"] if "example" in x or "nightly" in x]
    assert ask(mom, action="remove", name="nightly") == {"ok": False, "error": "no note 'nightly' in this room; its notes: trip-deals"}
    assert ask(mom, action="remove", name="Trip-Deals ") == {"ok": True, "name": "trip-deals", "removed": True}
    assert "trip-deals" not in path.read_text() and "nightly" in path.read_text() and not sd.read("yerk-job-trip-deals.timer")
    assert {"example-heartbeat", "nightly"} <= {x["name"] for x in ask("example-room", action="list")["jobs"]}   # the owner's: all


def test_a_room_that_is_not_the_owners_keeps_room_notes_at_most_and_adds_one_per_room_note_minutes(tmp_path):
    """Cold read C7 (2026-10-21): a person's room could hold any number of notes and add one every 30 seconds, each a
    model turn on the owner's account and a post in her channel. [jobs] room_notes (20) and room_note_minutes (5) cap
    it; the refusal says the cap and when the next one fits; a refused add does not count; the owner's rooms have none."""
    from test_room_scope import install
    y, mom = install(tmp_path, family=True), "family-room"
    y.jobs.units = FakeSystemd(tmp_path / "units")
    y.jobs.conf.update(room_note_burst=1)                                            # one per window, as it was

    def ask(caller, name, when="2099-01-03 10:00"):
        return asyncio.run(y.tool_schedule(caller, {"action": "add", "kind": "note", "name": name, "when": when,
                                                    "what": "Remind them to call the clinic."}))
    assert not ask(mom, "bad", when="0 9 * * *")["ok"] and ask(mom, "one")["ok"]          # the refused add did not count
    wait = ask(mom, "two")
    assert wait == {"ok": False, "line": W.NOTES_WAIT.format(what="one note", every="5", wait="5 min")}
    assert wait["line"].splitlines() == ["not scheduled: one note per 5 min here", "the next in 5 min"]
    y.jobs.conf.update(room_notes=2, room_note_minutes=0)
    assert ask(mom, "two")["ok"]
    full = ask(mom, "three")
    assert full == {"ok": False, "line": W.NOTES_FULL.format(most=2)} and "[jobs.three]" not in y.jobs.path.read_text()
    assert full["line"].splitlines()[0] == "not scheduled: this room keeps 2 notes at most"
    assert asyncio.run(y.tool_schedule(mom, {"action": "remove", "name": "one"}))["ok"] and ask(mom, "three")["ok"]
    y.jobs.conf.update(room_notes=1, room_note_minutes=5)
    assert all(ask("example-room", f"owner-{i}")["ok"] for i in range(3))            # the owner's rooms: no cap
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["explain", "jobs", "--private", str(y.jobs.path.parent), "--state", str(tmp_path / "state")]) == 0
    said = out.getvalue()
    assert "keys of [jobs] in the private yerkd.toml:" in said and "  ops_room: " in said
    assert all(f"  {k}: " in said and f"(default {d})" in said for k, d in (("room_notes", 20), ("room_note_minutes", 5),
                                                                           ("room_note_burst", 3), ("note_chars", 600)))


def test_a_room_with_people_adds_a_reminder_its_check_and_a_watch_in_one_turn(tmp_path):
    """2026-10-21: one note per five minutes refused a person's second note twice (a booking reminder's "is it booked?"
    check), and the room promised it for the person's next turn. [jobs] room_note_burst (3) notes go in one room_note_minutes
    window; the fourth waits for the oldest to leave it, a refused add does not count, and the window still bounds a loop."""
    from test_room_scope import install
    y, mom = install(tmp_path, family=True), "family-room"
    y.jobs.units = FakeSystemd(tmp_path / "units")

    def ask(name):
        return asyncio.run(y.tool_schedule(mom, {"action": "add", "kind": "note", "name": name, "when": "2099-01-03 10:00",
                                                 "what": "Remind them to book the dinner."}))
    assert [ask(n)["ok"] for n in ("book", "booked-check", "weekly")] == [True, True, True]
    wait = ask("fourth")
    assert wait["line"].splitlines() == ["not scheduled: 3 notes per 5 min here", "the next in 5 min"]
    assert len(y.jobs.added[mom]) == 3                                                # the refusal did not count
    y.jobs.added[mom] = [y.jobs.added[mom][0] - 301] + y.jobs.added[mom][1:]          # the oldest leaves the window
    assert ask("fourth")["ok"] and not ask("fifth")["ok"]

    async def at_once(*adds):                                                         # one turn's calls in parallel
        return await asyncio.gather(*(y.tool_schedule(mom, {"action": "add", "kind": "note", "name": n, "when": w,
                                                            "what": "Remind them to book the dinner."}) for n, w in adds))
    y.jobs.added[mom] = []
    bad, good = asyncio.run(at_once(("bad", "0 9 * * *"), ("sixth", "2099-01-03 10:00")))
    assert not bad["ok"] and good["ok"] and len(y.jobs.added[mom]) == 1              # the refused add gives back its own place only


def test_a_notes_message_over_note_chars_is_refused_with_the_cap_and_never_cut(tmp_path):
    """Cold read A2: a note's text had no ceiling anywhere, and the tell cuts nothing. A note's message is at most
    [jobs] note_chars (600) characters in every room and from `yerk schedule`; a longer one is refused with the cap in
    the line and nothing is written."""
    from test_room_scope import install
    y, mom = install(tmp_path, family=True), "family-room"
    y.jobs.units = sd = FakeSystemd(tmp_path / "units")
    note = {"action": "add", "kind": "note", "when": "2099-01-03 10:00"}
    long = asyncio.run(y.tool_schedule(mom, {**note, "name": "long", "what": "x" * 601}))
    assert long == {"ok": False, "error": W.NOTE_LONG.format(most=600, n=601)}
    assert long["error"].splitlines() == ["not scheduled: a note is at most 600 characters", "this one is 601"]
    assert asyncio.run(y.tool_schedule("example-room", {**note, "name": "long", "what": "x" * 601})) == long
    assert "[jobs.long]" not in y.jobs.path.read_text()
    assert asyncio.run(y.tool_schedule(mom, {**note, "name": "fits", "what": "x" * 600}))["ok"]
    priv = y.jobs.path.parent
    (priv / "yerkd.toml").write_text((priv / "yerkd.toml").read_text() + "\n[jobs]\nnote_chars = 10\n")
    monkey, err = pytest.MonkeyPatch(), io.StringIO()
    monkey.setattr(jobs, "Systemd", lambda: sd)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = cli.main(["schedule", "add", "short", "--kind", "note", "--when", "2099-01-03 10:00", "--what", "x" * 11,
                           "--room", mom, "--private", str(priv), "--state", str(tmp_path / "state")])
    finally:
        monkey.undo()
    assert rc == 1 and "a note is at most 10 characters" in err.getvalue() and "[jobs.short]" not in y.jobs.path.read_text()


def test_the_schedule_tool_sets_a_note_by_default_in_a_room_that_is_not_the_owners(tmp_path, monkeypatch):
    """Cold read C3: the tool's `kind` defaulted to "agent", the one kind a person's room can never set, so her call
    with no kind was refused with a line that never says `kind "note"`. In a room that is not the owner's a missing
    kind is a note; the owner's rooms keep "agent"."""
    from test_room_scope import install

    from yerk_ai import mcp_server
    y, mom = install(tmp_path, family=True), "family-room"
    y.jobs.units = FakeSystemd(tmp_path / "units")
    server = mcp_server.build(y, y.port, y.hook)
    for room, kind in ((mom, "note"), ("example-room", "agent")):
        monkeypatch.setattr(mcp_server, "caller_room", lambda ctx, room=room: room)
        got = asyncio.run(server.call_tool("schedule", {"action": "add", "name": f"call-{kind}", "when": "2099-01-03 10:00",
                                                        "what": "Remind them to call the clinic."}))
        out = json.loads((got[0] if isinstance(got, tuple) else got)[0].text)
        assert out["ok"] and (out["kind"], out["room"]) == (kind, room), out


def test_a_script_job_runs_this_installs_yerk_on_this_installs_folders_and_never_an_account(tmp_path, monkeypatch):
    """A `yerk` line in a script job (the nightly `yerk curate --rules`): the unit's YERK_PRIVATE and YERK_STATE reach it and this
    install's bin is first on PATH, so it never falls back to the fake example layer; an account token still stays out."""
    monkeypatch.setenv("YERK_PRIVATE", str(tmp_path / "private"))
    monkeypatch.setenv("YERK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-example")
    job = make_job("probe", {"kind": "script", "schedule": "*:00", "tz": "UTC", "room": "r", "command":
                             'printf "%s|%s|%s|%s" "$YERK_PRIVATE" "$YERK_STATE" "${PATH%%:*}" "$CLAUDE_CODE_OAUTH_TOKEN"'})
    (folder := tmp_path / "run").mkdir()
    out = jobs.run_script(job, folder)
    assert out["exit"] == 0 and out["output"] == f"{tmp_path / 'private'}|{tmp_path / 'state'}|{Path(sys.executable).parent}|"


def test_the_example_nightly_rules_job_reads_exit_1_as_proposals_to_read_and_2_as_a_failure(tmp_path):
    job = jobs.load(EXAMPLE_PRIVATE / jobs.FILE)[0]["example-curate-rules"]
    (b := tmp_path / "bin").mkdir()
    for code, rc in ((0, 0), (1, 0), (2, 1)):
        (y := b / "yerk").write_text(f"#!/bin/sh\n[ \"$*\" = \"curate --rules\" ] || exit 9\nexit {code}\n")
        y.chmod(0o755)
        assert subprocess.run(["/bin/sh", "-c", job.payload], env={"PATH": f"{b}:/usr/bin:/bin"}).returncode == rc


def test_an_agent_job_names_add_ons_and_a_note_or_a_script_naming_them_is_refused_by_the_key():
    base = {"schedule": "Mon 10:00", "tz": "UTC", "room": "r"}
    job = make_job("look", {**base, "kind": "agent", "message": "look", "addons": ["mcp__hands__browser", "mailbox"]})
    assert job.addons == ("mcp__hands__browser", "mailbox") and job.runs_on() == {"addons": "mcp__hands__browser mailbox"}
    assert make_job("plain", {**base, "kind": "agent", "message": "look"}).addons == () and "addons" in jobs.KEYS
    for kind, what, said in (("note", {"message": "x"}, "a note runs nothing, so it takes no `addons`"),
                             ("script", {"command": "true"}, "a script job runs no worker; `addons` is for agent jobs"),
                             ("agent", {"message": "x"}, "`addons` must be a list of the room's add-on tools or servers")):
        with pytest.raises(JobError) as e:
            make_job("x", {**base, "kind": kind, **what, "addons": ["mailbox"] if kind != "agent" else "mailbox"})
        assert str(e.value).startswith(said)


MAILBOX = {"model": "m", "timezone": "UTC", "allow": ["mcp__mailbox__search", "mcp__abacus__sum"],
           "mcp": {"mailbox": {"type": "stdio", "command": "mailbox"}, "abacus": {"type": "stdio", "command": "abacus"}}}
QUIET = 'FAKE report="NO_REPLY" result="NO_REPLY"'


def fired(j, w, s, sd, priv, state, name):
    """The job fired as its unit fires it, and its worker's row once it ended."""
    async def go():
        loop = asyncio.get_running_loop()
        w.begin(loop)
        handoff = lambda p: asyncio.run_coroutine_threadsafe(j.handle("", p["job"]), loop).result(30)  # noqa: E731
        assert await asyncio.to_thread(jobs.run, name, priv, state, sd, handoff) == 0
        return await ended(s.ledger, s.ledger.job_runs(job=name)[0]["worker"])
    return asyncio.run(go())


def test_a_fired_agent_job_whose_brief_names_a_path_its_unit_hides_tells_its_worker_in_the_prompt(setup, tmp_path):
    """A job's run reads no start answer (Jobs.fire keeps the worker's id): its worker hears of the path in PROMPT.md."""
    s = setup
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": Rec()})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    s.units.keep(w.state, (w.state / "rooms",))                    # the daemon's launcher: every room's folder hidden
    other = w.state / "rooms" / "other" / "work"
    write_jobs(priv / "jobs.toml", '[jobs.peek]\nkind = "agent"\nschedule = "*:00"\ntz = "UTC"\nroom = "r"\n'
               f'message = "Read {other}/notes.md. FAKE report=\\"NO_REPLY\\" result=\\"NO_REPLY\\""\n')
    jobs.sync(priv / "jobs.toml", sd, ENV)
    row = fired(j, w, s, sd, priv, state, "peek")
    prompt = (Path(row["staging"]) / "PROMPT.md").read_text()
    assert f"- Not there for you, since your unit hides them: {other}/notes.md. " in prompt


def test_a_booked_agent_job_names_add_ons_its_room_has_and_its_fired_worker_gets_them(setup, tmp_path, capsys):
    """The schedule tool and `yerk schedule add` take `addons`, checked against the job's room when it is booked with
    room_addons' own sentence, nothing written when refused; the list names them and the fired worker has them."""
    s = setup
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": Rec()}, room_confs={"r": MAILBOX})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    w.room_confs["r"] = MAILBOX
    path = write_jobs(priv / "jobs.toml", "# the owner's comment stays\n")
    add = lambda **k: asyncio.run(j.handle("r", {"action": "add", "kind": "agent", "when": "Mon..Fri 09:00", "what": QUIET,  # noqa: E731
                                                 "deliver": "silent", **k}))
    booked = add(name="inbox", addons=["mcp__mailbox__search"])
    assert booked["ok"] and booked["addons"] == "mcp__mailbox__search", booked
    assert 'room = "r"\naddons = ["mcp__mailbox__search"]\ndeliver = "silent"\n' in path.read_text()
    before = path.read_bytes()
    assert add(name="odd", addons=["mcp__mailbox__send"]) == {"ok": False, "error": "this room has no add-on tool "
                                                              "'mcp__mailbox__send'; its add-on tools: mcp__mailbox__search, mcp__abacus__sum"}
    assert add(name="odd", kind="note", what="x", addons=["mailbox"])["error"].startswith("a note runs nothing, so it takes no `addons`")
    assert path.read_bytes() == before and sd.ours() == ["inbox"]
    listed = asyncio.run(j.handle("r", {"action": "list"}))
    assert listed["lines"][0].endswith("room r, agent, addons mcp__mailbox__search") and listed["jobs"][0]["addons"] == "mcp__mailbox__search"
    row = fired(j, w, s, sd, priv, state, "inbox")
    spec = json.loads((Path(row["staging"]) / "worker.json").read_text())
    assert spec["addon_tools"] == ["mcp__mailbox__search"] and row["job"] == "inbox"
    assert list(json.loads(Path(spec["mcp_config"]).read_text())["mcpServers"]) == ["mailbox"]
    capsys.readouterr()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(jobs, "Systemd", lambda: sd)
    monkey.setattr(cli, "rooms", lambda p: {"r": MAILBOX})
    try:
        rc = cli.main(["schedule", "add", "cli-inbox", "--when", "*:0/45", "--what", "look", "--room", "r", "--addon", "mailbox",
                       "--private", str(priv), "--state", str(state)])
        odd = cli.main(["schedule", "add", "cli-odd", "--when", "*:0/45", "--what", "look", "--room", "r", "--addon", "mcp__web__fetch",
                        "--private", str(priv), "--state", str(state)])
    finally:
        monkey.undo()
    out, err = capsys.readouterr()
    assert (rc, odd) == (0, 1) and ", addons mailbox; next " in out and err.startswith("this room has no add-on tool 'mcp__web__fetch'")
    assert jobs.load(path)[0]["cli-inbox"].addons == ("mailbox",) and "cli-odd" not in path.read_text()


def test_a_hand_edited_jobs_add_on_its_room_has_not_is_named_at_the_sync_and_never_scheduled(tmp_path):
    priv = tmp_path / "private"
    priv.mkdir()
    (priv / "rooms.toml").write_text('[rooms.r]\nchannel = "discord:1"\nallow = ["mcp__mailbox__search"]\n\n'
                                     '[rooms.r.mcp.mailbox]\ntype = "stdio"\ncommand = "mailbox"\n')
    path = write_jobs(priv / "jobs.toml", '[jobs.good]\nkind = "agent"\nschedule = "Mon 09:00"\ntz = "UTC"\nroom = "r"\n'
                      'addons = ["mailbox"]\nmessage = "look"\n\n[jobs.odd]\nkind = "agent"\nschedule = "Mon 09:00"\ntz = "UTC"\n'
                      'room = "r"\naddons = ["mcp__mailbox__send"]\nmessage = "look"\n')
    sd = FakeSystemd(tmp_path / "units")
    p, _ = jobs.sync(path, sd, Env(ENV.python, ENV.repo, priv, ENV.state))
    assert p.added == ["good"] and sd.ours() == ["good"]
    assert p.errors == {"odd": "this room has no add-on tool 'mcp__mailbox__send'; its add-on tools: mcp__mailbox__search"}
    p, _ = jobs.sync(path, sd, ENV)                                    # where the rooms file cannot be read: left to the fire
    assert p.errors == {} and sorted(sd.ours()) == ["good", "odd"]


def test_a_rooms_file_that_refuses_to_load_never_ends_a_sync_and_the_jobs_add_ons_wait_for_the_fire(tmp_path):
    """Found by the judge's breaks. A rooms file the reader refuses stops a start with SystemExit, and a sync also runs
    inside the running daemon (the schedule tool's add and remove), where that exit would end the daemon. The add-ons'
    check is skipped instead and left to the fire."""
    priv = tmp_path / "private"
    priv.mkdir()
    (priv / "rooms.toml").write_text('[defaults]\nbogus = 1\n\n[rooms.r]\nchannel = "discord:1"\n')
    path = write_jobs(priv / "jobs.toml", '[jobs.inbox]\nkind = "agent"\nschedule = "Mon 09:00"\ntz = "UTC"\nroom = "r"\n'
                      'addons = ["mailbox"]\nmessage = "look"\n')
    sd = FakeSystemd(tmp_path / "units")
    p, _ = jobs.sync(path, sd, Env(ENV.python, ENV.repo, priv, ENV.state))
    assert p.errors == {} and sd.ours() == ["inbox"]


def test_a_jobs_add_on_name_that_is_not_an_add_ons_shape_is_refused_by_its_own_table():
    """Found by the judge's breaks. A name with a space or a dot, or an empty one, is refused when the job is read, before
    any room is asked: where the rooms file cannot be read nothing else would refuse it until the fire."""
    base = {"kind": "agent", "schedule": "Mon 10:00", "tz": "UTC", "room": "r", "message": "look"}
    for bad in ("not a tool", "mcp__mailbox.search", ""):
        with pytest.raises(JobError) as e:
            make_job("x", {**base, "addons": [bad]})
        assert str(e.value).startswith("`addons` must be a list of the room's add-on tools or servers")


def test_a_job_naming_no_add_on_in_a_room_giving_every_worker_one_starts_a_worker_that_has_it(setup, tmp_path):
    """The case itself, seen live: a booked agent job named no add-on (none could), its brief needed the room's mail
    tool, and its worker started without one. Its room's worker_addons now reach the job's worker with nothing named."""
    s = setup
    conf = {**MAILBOX, "worker_addons": ["mailbox"]}
    j, w, sd, priv, state = manager(setup, tmp_path, {"r": s.room()}, {"r": Rec()}, room_confs={"r": conf})
    s.ledger.__init__(state / "ledger.sqlite")
    j.ledger = w.ledger = s.ledger
    w.room_confs["r"] = conf
    path = write_jobs(priv / "jobs.toml", '[jobs.inbox]\nkind = "agent"\nschedule = "Mon..Fri 09:00"\ntz = "UTC"\nroom = "r"\n'
                      f"deliver = \"silent\"\nmessage = '{QUIET}'\n")
    jobs.sync(path, sd, j.env())
    row = fired(j, w, s, sd, priv, state, "inbox")
    folder = Path(row["staging"])
    spec = json.loads((folder / "worker.json").read_text())
    assert spec["addon_tools"] == ["mcp__mailbox__search"] and row["job"] == "inbox"
    assert list(json.loads(Path(spec["mcp_config"]).read_text())["mcpServers"]) == ["mailbox"]
    assert "- You have the room's own mcp__mailbox__search too." in (folder / "PROMPT.md").read_text()


def test_the_schedule_and_worker_tools_hand_addons_on_and_say_what_a_worker_has(monkeypatch, tmp_path):
    """The schedule tool's `addons` reaches the daemon as the worker tool's does, and both tools say what a worker has: a
    shell, files and the web, of the room's tools only the add-ons the room gives every worker, those named and recall and
    read_memory where the install gives workers those (2026-11-18: a worker given both was told it had none)."""
    from yerk_ai import mcp_server
    asked: list[dict] = []

    class Daemon:
        async def tool_schedule(self, room, req):
            return asked.append(req) or {"ok": True}
        tool_worker = tool_schedule

    async def none(*a, **k):
        return {}
    server = mcp_server.build(Daemon(), 0, none)
    monkeypatch.setattr(mcp_server, "caller_room", lambda ctx: "r")
    asyncio.run(server.call_tool("schedule", {"action": "add", "name": "inbox", "when": "Mon 09:00", "what": "look", "addons": ["mailbox"]}))
    asyncio.run(server.call_tool("worker", {"action": "start", "brief": "look"}))
    assert asked[0]["addons"] == ["mailbox"] and asked[1]["addons"] == []
    said = {t.name: " ".join(str(t.description).split()) for t in asyncio.run(server.list_tools())}
    assert ("it has a shell, files and the web, of your tools only the add-ons this room gives every worker, those `addons` "
            "names and recall and read_memory where the install gives workers those, never send, text or email, so a light "
            "timed check needing your tools is a note") in said["schedule"]
    assert '<example>"remind me Saturday at 10 to call the clinic"' in said["schedule"]                    # the example kept
    assert ("unnamed it has a shell, files, the web and the add-ons this room gives every worker, and reads this room's memory "
            "with recall and read_memory where the install gives workers those; a step needing `confirmed` it leaves ready for "
            "you") in said["worker"]
    monkeypatch.setattr(cli, "_control", lambda state, payload, timeout: asked.append(payload["worker"]) or {"ok": True})
    (brief := tmp_path / "brief.md").write_text("look")
    assert cli.main(["worker", "start", "--brief", str(brief), "--room", "r", "--addon", "mailbox", "--addon", "mcp__abacus__sum"]) == 0
    assert asked[2]["addons"] == ["mailbox", "mcp__abacus__sum"]
