"""Checks that the real daemon runs offline and that every task it starts does its first piece of work. tests/verify_daemon.py (the /verify
recipe's run) starts yerkd's own main() once on a scratch copy of the example layer with only the outside world replaced, each long-lived
task run_discord starts seen doing its first piece of work, driven from outside as a person or a program does. A unit test that swaps the
ones that return at once passed with the meters' keeper never started (2026-11-17); this one fails, naming the step."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_daemon_runs_offline_and_every_task_it_starts_does_its_first_piece_of_work(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("YERK_", "FAKE_CLAUDE_"))} | {"TMPDIR": str(tmp_path)}
    r = subprocess.run([sys.executable, str(ROOT / "tests" / "verify_daemon.py")], capture_output=True, text=True,
                       cwd=ROOT, env=env, timeout=120)
    lines = r.stdout.splitlines()
    assert r.returncode == 0 and lines and "checks passed" in lines[-1], r.stdout + r.stderr[-3000:]
    assert not [x for x in lines if x.startswith("\N{LARGE RED CIRCLE}")], r.stdout
    assert not list(tmp_path.iterdir()), "the run left its scratch folder behind"
