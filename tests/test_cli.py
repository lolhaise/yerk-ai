import os
import re
import subprocess
import sys
from pathlib import Path

from yerk_ai.config import budgets

ROOT = Path(__file__).resolve().parents[1]


def yerk(*args):
    env = {k: v for k, v in os.environ.items() if k != "YERK_PRIVATE"}
    return subprocess.run([sys.executable, "-m", "yerk_ai.cli", *args], capture_output=True, text=True,
                          cwd=ROOT, env=env, timeout=60)


def test_help_lists_the_commands():
    r = yerk("--help")
    assert r.returncode == 0 and "map" in r.stdout and "retention" in r.stdout and "probe" not in r.stdout   # tests/contract/


def test_map_example_room_is_within_budget():
    r = yerk("map", "example-room")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "total" in r.stdout and "OVER" not in r.stdout


def test_map_fails_on_the_planted_file():
    r = yerk("map", "planted-room", "--private", "tests/fixtures/over-budget")
    assert r.returncode == 1 and "OVER BUDGET: persona" in r.stdout


def test_map_shows_the_rules_file_as_a_part_of_its_own(tmp_path):
    (tmp_path / "memory" / "tree").mkdir(parents=True)          # the example layer's own tree sits in the state folder
    (tmp_path / "memory" / "tree" / "rules.md").write_text("# Rules\n\n- a job: done this way.\n")
    r = yerk("map", "example-room", "--state", str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(rf"^  rules +\d+ +{budgets()['session_start']['appended']['rules']:,}  ok  \[\S+/rules\.md\]$", r.stdout, re.M)
    assert r.stdout.index("  pinned_memory ") < r.stdout.index("  rules ") < r.stdout.index("  recall_pack ")
    assert "rules.md" not in next(x for x in r.stdout.splitlines() if x.startswith("  rulebook "))


def test_map_unknown_room():
    assert yerk("map", "no-such-room").returncode == 2


def test_a_command_on_the_example_layer_says_so_once_on_stderr(tmp_path):
    """F9-12: with no --private and no $YERK_PRIVATE a command answers about the example layer in the repo against the real
    state folder; one line on stderr says so, state first, with the flag that names the real folder, once however often the
    command reads the folder (`yerk explain retention` reads it twice), and the answer itself is unchanged."""
    from yerk_ai import words
    r = yerk("explain", "retention", "--state", str(tmp_path))
    said = r.stderr.splitlines()
    assert r.returncode == 0 and len(said) == 1 and said[0].startswith(words.WORKING), r.stderr
    assert "example layer" in said[0] and "--private" in said[0] and words.width(said[0]) <= words.PHONE
    q = yerk("explain", "retention", "--private", "examples/private", "--state", str(tmp_path))
    assert q.returncode == 0 and q.stdout == r.stdout and q.stderr == ""


def test_add_and_deploy_never_say_the_example_layer(tmp_path, monkeypatch, capsys):
    """F9-12: the two commands that never answer on the example layer say nothing of it: `yerk add` refuses there with its
    own line, and `yerk deploy` takes the running daemon's own layer, else refuses."""
    from yerk_ai import cli, config, deploy
    r = yerk("add", "anything", "--state", str(tmp_path))
    assert r.returncode == 2 and "example layer" not in r.stderr and "--private" in r.stderr, r.stderr
    monkeypatch.delenv("YERK_PRIVATE", raising=False)
    monkeypatch.setattr(config, "_EXAMPLE_SAID", False, raising=False)
    monkeypatch.setattr(deploy, "run", lambda private, *a, **k: 0 if private == config.EXAMPLE_PRIVATE else 1)
    assert cli.main(["deploy", "--state", str(tmp_path)]) == 0
    assert "example layer" not in capsys.readouterr().err
