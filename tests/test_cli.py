import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def yerk(*args):
    env = {k: v for k, v in os.environ.items() if k != "YERK_PRIVATE"}
    return subprocess.run([sys.executable, "-m", "yerk_ai.cli", *args], capture_output=True, text=True,
                          cwd=ROOT, env=env, timeout=60)


def test_help_lists_the_commands():
    r = yerk("--help")
    assert r.returncode == 0 and "map" in r.stdout and "probe" in r.stdout


def test_map_example_room_is_within_budget():
    r = yerk("map", "example-room")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "total" in r.stdout and "OVER" not in r.stdout


def test_map_fails_on_the_planted_file():
    r = yerk("map", "planted-room", "--private", "tests/fixtures/over-budget")
    assert r.returncode == 1 and "OVER BUDGET: persona" in r.stdout


def test_map_unknown_room():
    assert yerk("map", "no-such-room").returncode == 2
