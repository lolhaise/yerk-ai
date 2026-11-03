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
import subprocess
import sys
import time
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
