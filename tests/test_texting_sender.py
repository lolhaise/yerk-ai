"""Checks texting's sender and its hands, the step that passes a text to the Mac: a text that may go waits a time drawn for it, is
checked against the thread read again at that moment, and is handed once, dry as a job file, live as one job on ssh's stdin into
the Mac runner's outbox. Built in run 6. Nothing here reaches a Mac: the thread is the tests' own, and "ssh" is a fake command
test's tmp folder and prints `queued:<id>`. Time is the tests' own clock, never a sleep."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from test_texting_link import A1, B1
from yerk_ai.texting import hands

LIST = (f'[owner]\nname = "Sam"\n\n[people.alex]\nfirst = "Alex"\nhandles = ["{A1}"]\nsheet = "people/alex.toml"\n\n'
        f'[people.beta]\nfirst = "Beta"\nhandles = ["{B1}"]\nsheet = "people/beta.toml"\n')
SHEET = ('open = []\nnight = "{night}"\n\n[pacing]\nweekday_day = [2.0, 4.0]\nweekday_evening = [2.0, 4.0]\nweekend_day = [2.0, 4.0]\n'
         'weekend_evening = [2.0, 4.0]\nactive = [0.5, 1.0]\nburst_gap = [10, 35]\n\n[voice]\ncard = "short, lower case"\n')
FAKE = """import pathlib, re, sys
box, kind, cmd = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[-1]
name = re.search(r"echo queued:(\\S+)", cmd).group(1)
with open(box.parent / "calls.log", "a") as f:
    f.write(name + "\\n")
data = sys.stdin.read()
if kind == "nolink":
    sys.stderr.write("ssh: connect to host mac port 22: Connection refused\\n")
    sys.exit(255)
(box / (name + ".json")).write_text(data)
print("queued:" + name if kind == "ok" else "Connection to mac closed.")
"""


def people(tmp: Path, night: str = "hold") -> Path:
    priv = tmp / "private" / "texting"
    (priv / "people").mkdir(parents=True, exist_ok=True)
    for f, text in ((priv / "people.toml", LIST), (priv / "people" / "alex.toml", SHEET.replace("{night}", night)),
                    (priv / "people" / "beta.toml", SHEET.replace("{night}", "hold"))):
        f.write_text(text)
        os.chmod(f, 0o600)
    return priv / "people.toml"


def fake(tmp: Path, kind: str = "ok") -> list[str]:
    """The tests' "ssh": writes its stdin as <tmp>/outbox/<id>.json and answers as the Mac would (`kind` ok), or cannot
    connect (`nolink`), or connects and answers something else (`odd`); every call is a line of <tmp>/calls.log."""
    (box := tmp / "outbox").mkdir(exist_ok=True)
    (script := tmp / "fake_ssh.py").write_text(FAKE)
    return [sys.executable, str(script), str(box), kind]


def calls(tmp: Path) -> list[str]:
    return (tmp / "calls.log").read_text().split() if (tmp / "calls.log").exists() else []


def reply(*texts: str, handle: str = A1, reply_to: str = "") -> dict[str, Any]:
    return {"handle": handle, "decision": "reply", "parts": json.dumps(list(texts)), "react": "", "reply_to": reply_to}


def no_spawn(monkeypatch: Any) -> None:
    """Every way to start a process fails the test."""
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError(f"a process was started: {a[:2]}")
    for mod, name in ((asyncio, "create_subprocess_exec"), (asyncio, "create_subprocess_shell"), (subprocess, "Popen"),
                      (subprocess, "run"), (os, "system"), (os, "execv"), (os, "posix_spawn")):
        monkeypatch.setattr(mod, name, boom)


def test_a_jobs_queued_and_since_are_the_newest_answered_text_plus_one_second_and_parts_two_and_three_burst() -> None:
    jobs = [hands.job(reply("on my way", "ten min", "save me a seat", reply_to="where are you"), k, 1700000000.25) for k in (1, 2, 3)]
    assert all(j["to"] == A1 and j["delay"] == 0 and j["queued"] == j["since"] == 1700000001.25 for j in jobs)
    assert "burst" not in jobs[0] and jobs[0]["reply_to"] == "where are you" and jobs[0]["text"] == "on my way"
    assert jobs[1]["burst"] is jobs[2]["burst"] is True and "reply_to" not in jobs[1] and jobs[2]["text"] == "save me a seat"
    assert "reply_to" not in hands.job(reply("ok"), 1, 5.0)
    tap = {**reply(), "decision": "react", "react": "love", "parts": "[]"}
    assert hands.job(tap, 1, 5.0) == {"to": A1, "delay": 0, "queued": 6.0, "since": 6.0, "react": "love"} and hands.parts(tap) == 1


def test_dry_hands_write_the_job_file_and_never_start_a_process(tmp_path: Path, monkeypatch: Any) -> None:
    file = people(tmp_path)
    no_spawn(monkeypatch)
    job = hands.job(reply("on my way"), 1, 100.0)
    assert asyncio.run(hands.Dry(file, tmp_path / "state").hand("yerk-7-1", job)) == hands.PUT
    put = tmp_path / "state" / "texting" / "dry" / "yerk-7-1.json"
    assert json.loads(put.read_text()) == job and put.stat().st_mode & 0o777 == 0o600


def test_live_writes_the_job_on_stdin_and_reads_what_the_mac_answered(tmp_path: Path) -> None:
    file, job = people(tmp_path), hands.job(reply("on my way"), 1, 100.0)
    assert asyncio.run(hands.Live(file, "/Users/someone/outbox", fake(tmp_path)).hand("yerk-7-1", job)) == hands.PUT
    assert json.loads((tmp_path / "outbox" / "yerk-7-1.json").read_text()) == job
    assert asyncio.run(hands.Live(file, "/Users/someone/outbox", fake(tmp_path, "nolink")).hand("yerk-8-1", job)) == hands.DOWN
    assert asyncio.run(hands.Live(file, "/Users/someone/outbox", fake(tmp_path, "odd")).hand("yerk-9-1", job)) == hands.MAYBE
    assert calls(tmp_path) == ["yerk-7-1", "yerk-8-1", "yerk-9-1"]


def test_a_to_not_on_the_reply_list_or_an_outbox_that_is_not_a_whole_path_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    file = people(tmp_path)
    stranger = hands.job(reply("on my way", handle="+15550000199"), 1, 100.0)
    for hand in (hands.Live(file, "/Users/someone/outbox", fake(tmp_path)), hands.Dry(file, tmp_path / "state")):
        assert asyncio.run(hand.hand("yerk-7-1", stranger)) == hands.REFUSED
    ours = hands.job(reply("on my way"), 1, 100.0)
    for box in ("outbox", "/Users/someone/out box", "/Users/x;touch y"):
        assert asyncio.run(hands.Live(file, box, fake(tmp_path)).hand("yerk-7-1", ours)) == hands.REFUSED
    assert calls(tmp_path) == [] and not list((tmp_path / "outbox").iterdir()) and not (tmp_path / "state").exists()
