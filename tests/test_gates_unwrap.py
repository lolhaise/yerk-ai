"""Checks that the shell gate, the check on a room's shell commands, judges what would really run: wrappers peeled, paths resolved, the work folder kept.

Every folder is under pytest's tmp_path; the file to delete is delete-target. Nothing here runs a
command: the gate only reads text and resolves paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yerk_ai import gates

S = gates.Settings(trash=("trash",))


@pytest.fixture
def work(tmp_path: Path) -> Path:
    w = tmp_path / "work"
    w.mkdir()
    (w / "delete-target").write_text("x")
    (tmp_path / "delete-target").write_text("x")
    (w / "link-out").symlink_to(tmp_path)
    return w


def test_a_plain_delete_inside_is_still_rewritten_to_the_trash(work: Path) -> None:
    v = gates.check_shell("rm delete-target", str(work), S, root=str(work))
    assert v is not None and v.decision == "rewrite" and v.new_input == {"command": "trash -- delete-target"}


@pytest.mark.parametrize("text", ["timeout 5 rm delete-target", "sh -c 'rm delete-target'", "nice -n 5 rm delete-target",
                                  "sudo rm delete-target", "echo x && rm delete-target", "eval rm delete-target"])
def test_a_wrapped_or_chained_delete_is_seen_and_held_for_the_plain_form(work: Path, text: str) -> None:
    v = gates.check_shell(text, str(work), S, root=str(work))
    assert v is not None and v.decision == "deny" and "trash" in v.reason


@pytest.mark.parametrize("text", ["rm ../delete-target", "rm -r link-out/delete-target", "rm -r link-out",
                                  "find .. -name delete-target -delete"])
def test_a_delete_that_resolves_outside_the_work_folder_is_refused(work: Path, text: str) -> None:
    v = gates.check_shell(text, str(work), S, root=str(work))
    assert v is not None and v.decision == "deny" and "outside the work folder" in v.reason


def test_the_work_folder_is_the_root_even_when_the_shell_has_moved(work: Path, tmp_path: Path) -> None:
    v = gates.check_shell("rm delete-target", str(tmp_path), S, root=str(work))   # cwd is the parent now
    assert v is not None and v.decision == "deny" and "outside the work folder" in v.reason


def test_a_delete_after_a_cd_is_refused(work: Path) -> None:
    v = gates.check_shell("cd .. && rm delete-target", str(work), S, root=str(work))
    assert v is not None and v.decision == "deny" and "cd" in v.reason


@pytest.mark.parametrize("text", ['rm "$HOME/delete-target"', "rm *", "rm ~/delete-target", "xargs rm", "rm -f"])
def test_a_delete_the_gate_cannot_resolve_is_refused(work: Path, text: str) -> None:
    v = gates.check_shell(text, str(work), S, root=str(work))
    assert v is not None and v.decision == "deny" and "cannot resolve" in v.reason


@pytest.mark.parametrize("text", ["taskset -c 1 ls", "flock lock ls", "script -qc ls /dev/null"])
def test_a_runner_the_gate_cannot_see_through_is_refused(work: Path, text: str) -> None:
    v = gates.check_shell(text, str(work), S, root=str(work))
    assert v is not None and v.decision == "deny" and "without it" in v.reason


@pytest.mark.parametrize("text", ["echo 'rm delete-target", "echo $(rm delete-target"])
def test_an_unreadable_command_is_refused(work: Path, text: str) -> None:
    v = gates.check_shell(text, str(work), S, root=str(work))
    assert v is not None and v.decision == "deny" and "could not be read" in v.reason


@pytest.mark.parametrize("text", ["git rm delete-target", "git clean -fd", "rmdir sub", "trash -- delete-target",
                                  "rsync -a --delete src/ dst/", "zip -m a.zip delete-target"])
def test_ordinary_deletes_inside_the_work_folder_run_as_written(work: Path, text: str) -> None:
    (work / "sub").mkdir(exist_ok=True)
    assert gates.check_shell(text, str(work), S, root=str(work)) is None


@pytest.mark.parametrize("text", ["echo rm delete-target", "grep -n 'rm -rf' x.py", "git rm --cached delete-target",
                                  "cat <<'EOF'\nrm delete-target\nEOF", "ls -la", 'bash -c "echo ok"', "find . -name '*.pyc'"])
def test_what_deletes_nothing_passes_untouched(work: Path, text: str) -> None:
    assert gates.check_shell(text, str(work), S, root=str(work)) is None


def test_root_defaults_to_cwd(work: Path) -> None:
    assert gates.check_shell("rm ../delete-target", str(work), S) is not None
    assert gates.check_shell("rm delete-target", str(work), S).new_input == {"command": "trash -- delete-target"}
