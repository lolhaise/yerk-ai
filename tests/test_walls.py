"""Checks the walls: the approval, restart and worker rules read a shell command as what would really run, whatever its shape.

G1: the approval and restart rules read a shell command the way the delete rules do, as what would really run (unwrap):
G1: the approval and restart rules read a shell command the way the delete rules do, as what would really run (unwrap): a held command stays held behind
a wrapper, in a nested shell, after a shell keyword, behind xargs or by its full path, and the control set still passes untouched. The live case: worker
need-2026-10-28-24` past the worker gate; the missing GitHub login was what stopped it.
"""
from pathlib import Path

import pytest

from yerk_ai import gates

D = gates.Settings(trash=("trash",))
PUSH_SHAPES = [                                   # one held action, `git push origin main`, in the shapes a shell runs it
    "git push origin main",
    "/usr/bin/git push origin main",
    "timeout 60 git push origin main",
    "nice -n 5 git push origin main",
    'bash -c "git push origin main"',
    "sh -c 'cd repo && git push origin main'",
    'eval "git push origin main"',
    "if true; then git push origin main; fi",
    "! git push origin main",
    "echo main | xargs git push origin",
    "echo $(git push origin main)",
    "cd wt && timeout 60 git push -u origin need-2026-10-28-24 2>&1 | tail -5",     # worker 00c67e's own line
]


@pytest.mark.parametrize("command", PUSH_SHAPES)
def test_a_push_is_held_in_every_shape_a_shell_runs_it(command):
    hit = gates.external_hit("Bash", {"command": command}, "r", D)
    assert hit is not None and hit[0] == "public-post", command


@pytest.mark.parametrize("command,rule", [
    ("/usr/bin/gh pr create -t x -b y", "public-post"),
    ("timeout 30 gh pr merge 12 --squash", "public-post"),
    ("sh -c 'gh auth login'", "login"),
    ("env -u X docker login registry.example", "login"),
    ("timeout 5 stripe charges create --amount 100", "money-cli"),
    ("/usr/local/bin/npm publish", "public-post"),
])
def test_the_other_shell_rules_read_what_really_runs(command, rule):
    hit = gates.external_hit("Bash", {"command": command}, "r", D)
    assert hit is not None and hit[0] == rule, command


@pytest.mark.parametrize("command", [             # a word of a held command in text, never run: no hold (decision 18)
    'echo "git push origin main"', "grep -rn 'git push' docs", 'git commit -m "git push comes later"', "git push --dry-run -h | cat <<'EOF'\ngh pr create\nEOF",
    "cat > notes.md <<'EOF'\ngit push origin main\nstripe charges create\nEOF", "printf '%s\\n' 'gh auth login'", "man git-push",
    "git log --format=push", "gh pr list", "git -C repo status", "ls /usr/bin/git", "which gh stripe",
])
def test_a_held_command_named_in_text_is_not_held(command):
    hit = gates.external_hit("Bash", {"command": command}, "r", D)
    assert hit is None or command.startswith("git push --dry-run"), command


def test_a_worker_is_refused_a_push_in_every_shape(tmp_path):
    spec = {"room": "r", "gate": {"trash": ["trash"], "private": str(tmp_path / "private")}}
    for command in PUSH_SHAPES:
        out = gates.worker_check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}, tmp_path, spec)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", command


RESTART_SHAPES = [
    "systemctl --user restart yerkd-test",
    "/usr/bin/systemctl --user restart yerkd-test",
    "systemctl --user --no-block restart yerkd-test",
    "systemctl --user -q restart yerkd-test.service",
    "systemctl restart --user yerkd-test",
    "bash -c 'systemctl --user restart yerkd-test'",
    "timeout 20 systemctl --user stop yerkd",
    "systemctl --user kill -s KILL yerkd-test",
    "/usr/bin/pkill -f yerkd",
    "timeout 5 killall yerkd",
]


@pytest.mark.parametrize("command", RESTART_SHAPES)
def test_a_restart_of_yerkd_is_seen_in_every_shape(command, tmp_path):
    assert gates.restart_hit("Bash", {"command": command}, tmp_path / "private") == "a restart of yerkd", command


@pytest.mark.parametrize("command", [
    "systemctl --user status yerkd-test", "systemctl --user is-active yerkd-test", "systemctl --user restart yerk-index",
    "systemctl --user list-timers 'yerk-job-*'", "journalctl --user -u yerkd-test -n 50", 'echo "systemctl --user restart yerkd-test"',
    "systemctl --user show yerkd-test -p MainPID", "pgrep -f yerkd", "systemctl --user restart other-unit",
])
def test_a_look_at_yerkd_is_not_a_restart(command, tmp_path):
    assert gates.restart_hit("Bash", {"command": command}, tmp_path / "private") is None, command


def test_a_worker_is_refused_a_restart_in_every_shape(tmp_path):
    spec = {"room": "r", "gate": {"trash": ["trash"], "private": str(tmp_path / "private")}}
    for command in RESTART_SHAPES:
        out = gates.worker_check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}, tmp_path, spec)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", command


def test_runs_reads_a_command_as_the_delete_rules_do():
    assert gates.runs("cd wt && timeout 60 /usr/bin/git push -u origin b 2>&1 | tail -5") == ["cd wt", "git push -u origin b", "tail -5"]
    assert gates.runs("echo x | xargs -n1 git push origin") == ["echo x", "git push origin"]      # behind xargs: its fixed words too
    assert gates.runs('bash -c "a; b c"') == ["a", "b c"]
    assert gates.runs("echo 'unclosed") == []                      # unreadable: the delete gate has refused it already
