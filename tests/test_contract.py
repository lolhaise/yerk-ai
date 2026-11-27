"""Checks the contract probes in tests/contract/probes.py, short real Claude Code runs that show it still behaves as Yerk AI expects. The cases:
one probe run as a person or a timer runs it, against the fake binary, with its raw event log and ledger row; the upgrade drill with its verdict
and its line in drills.jsonl; and the package's command line, which no longer has either since the probes moved there from `yerk probe`."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from yerk_ai import cli, words
from contract import probes

ROOT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_claude.py")


def test_a_probe_runs_from_its_new_home_against_the_binary_and_keeps_its_raw_event_log(tmp_path):
    state, logs = tmp_path / "state", tmp_path / "raw"
    env = {k: v for k, v in os.environ.items() if not k.startswith("YERK_")} | {"YERK_CLAUDE": str(FAKE)}
    r = subprocess.run([sys.executable, "tests/contract/probes.py", "hello", "--config-dir", str(tmp_path / "acct"),
                        "--state", str(state), "--logs", str(logs), "--model", "claude-opus-5-5"],
                       capture_output=True, text=True, cwd=ROOT, env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] and out["outcome"] == "ok" and out["ledger_row"]["room"] == "probe-hello"
    events = [json.loads(x) for x in (logs / "hello.jsonl").read_text().splitlines()]
    assert any(e.get("sent") == probes.HELLO for e in events) and any((e.get("e") or {}).get("type") == "result" for e in events)
    no = subprocess.run([sys.executable, "tests/contract/probes.py", "resume", "--config-dir", "x"], capture_output=True,
                        text=True, cwd=ROOT, env=env, timeout=60)
    assert no.returncode == 2 and "resume needs --to-config-dir" in no.stderr


def test_the_upgrade_drill_records_the_version_runs_three_probes_and_upgrades_nothing(tmp_path, monkeypatch, capsys):
    ran = []
    real = subprocess.run
    monkeypatch.setattr(probes.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "2.1.301 (Claude Code)\n", "")
                        if cmd[:2] == ["claude", "--version"] else real(cmd, **k))
    for name, ok in (("hello", True), ("fold", True), ("resume", False)):
        monkeypatch.setattr(probes, name, lambda a, n=name, ok=ok: ran.append((n, a.config_dir, a.to_config_dir, a.reps)) or {"ok": ok})
    assert probes.main(["drill", "--config-dir", "/acct/four", "--state", str(tmp_path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [f"{words.PLAIN} claude 2.1.301 (Claude Code)", f"{words.DONE} probe hello: passed",
                   f"{words.DONE} probe fold: passed", f"{words.NOT_DONE} probe resume: failed",
                   f"{words.NOT_DONE} verdict: stay, resume failed"]
    assert ran == [(n, "/acct/four", "/acct/four", 1) for n in ("hello", "fold", "resume")]   # one account, one fold
    line = json.loads((tmp_path / "drills.jsonl").read_text())
    assert line["version"] == "2.1.301 (Claude Code)" and line["probes"] == {"hello": True, "fold": True, "resume": False}


def test_the_package_command_line_has_neither_probe_nor_drill(capsys):
    for argv in (["probe", "hello", "--config-dir", "x"], ["drill", "--config-dir", "x"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2 and "invalid choice" in capsys.readouterr().err
