"""Checks unwrap (unwrap.py), which reads a shell command line for the simple commands it would really run, and where a delete points.

The program under test is the stand-in yerk-banned-tool and the file to delete
is delete-target, with every folder under pytest's tmp_path. Nothing here runs
a command: the module only reads text and resolves paths.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from yerk_ai.unwrap import delete_targets, inside, unwrap

TOOL = "yerk-banned-tool"
RUN = [[TOOL, "--flag"]]


def nest(levels: int) -> str:
    """The tool behind that many layers of sh -c."""
    command = TOOL
    for _ in range(levels):
        command = "sh -c " + shlex.quote(command)
    return command


def real(*parts: str) -> str:
    return os.path.realpath(os.path.join(*parts))


# What it finds


def test_bare() -> None:
    assert unwrap(f"{TOOL} --flag") == RUN


WRAPPERS = [
    "timeout 5", "timeout -s TERM -k 2 5", "nice", "nice -n 10", "env", "env FOO=1 BAR=2", "env -i FOO=1",
    "command", "command -p", "builtin", "sudo", "sudo -u nobody -E", "doas", "exec", "nohup", "time -p",
    "FOO=1", "sudo timeout 5 nice -n 5 env FOO=1 command",
]


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_behind_each_wrapper(wrapper: str) -> None:
    assert unwrap(f"{wrapper} {TOOL} --flag") == RUN


@pytest.mark.parametrize("wrapper", ["xargs", "xargs -0", "xargs -n 1 -P 4", "xargs -I {}", "xargs sudo"])
def test_behind_xargs_the_inner_command_has_no_args(wrapper: str) -> None:
    assert unwrap(f"{wrapper} {TOOL} --flag") == [[TOOL]]


@pytest.mark.parametrize("shell", ["sh -c", "bash -c", "zsh -c", "bash -lc", "bash --norc -c", "dash -c"])
def test_inside_each_c_string(shell: str) -> None:
    assert unwrap(f"{shell} '{TOOL} --flag'") == RUN
    assert unwrap(f'{shell} "{TOOL} --flag"') == RUN


def test_nested_two_deep() -> None:
    assert unwrap(f"bash -c \"sh -c '{TOOL} --flag'\"") == RUN
    assert unwrap(nest(2)) == [[TOOL]]


def test_the_third_command_in_a_chain() -> None:
    commands = unwrap(f"ls; git status; {TOOL} --flag")
    assert commands == [["ls"], ["git", "status"], [TOOL, "--flag"]]
    assert commands[2] == [TOOL, "--flag"]


@pytest.mark.parametrize("joiner", [";", " && ", " || ", " | ", "\n", " & ", " |& "])
def test_every_separator(joiner: str) -> None:
    assert unwrap(f"ls{joiner}{TOOL} --flag") == [["ls"], [TOOL, "--flag"]]


def test_inside_eval() -> None:
    assert unwrap(f"eval '{TOOL} --flag'") == RUN
    assert unwrap(f"eval {TOOL} --flag") == RUN
    assert unwrap(f'eval "ls; {TOOL} --flag"') == [["ls"], [TOOL, "--flag"]]


def test_a_command_substitution_runs() -> None:
    assert unwrap(f'echo "$({TOOL} --flag)"') == [[TOOL, "--flag"], ["echo", f"$({TOOL} --flag)"]]
    assert unwrap(f"echo `{TOOL} --flag`") == [[TOOL, "--flag"], ["echo", f"`{TOOL} --flag`"]]
    assert unwrap(f"diff <({TOOL} --flag) notes.txt") == [[TOOL, "--flag"], ["diff", f"<({TOOL} --flag)", "notes.txt"]]
    assert unwrap(f"NOW=$({TOOL} --flag) ls") == [[TOOL, "--flag"], ["ls"]]


def test_a_single_quoted_substitution_is_text() -> None:
    assert unwrap(f"echo '$({TOOL})'") == [["echo", f"$({TOOL})"]]


def test_a_commit_message_from_a_heredoc() -> None:
    text = f"git commit -m \"$(cat <<'EOF'\nfix: {TOOL} is named here as text\nEOF\n)\""
    commands = unwrap(text)
    assert commands is not None
    assert [argv[0] for argv in commands] == ["cat", "git"]


@pytest.mark.parametrize(
    "text",
    [
        f"if true; then {TOOL} --flag; fi",
        f"{{ {TOOL} --flag; }}",
        f"({TOOL} --flag)",
        f"! {TOOL} --flag",
        f"for f in a b; do {TOOL} --flag; done",
        f"while true; do {TOOL} --flag; done",
        f"function f {{ {TOOL} --flag; }}",
    ],
)
def test_shell_grammar_words_are_not_programs(text: str) -> None:
    commands = unwrap(text)
    assert commands is not None and [TOOL, "--flag"] in commands
    assert all(argv[0] not in {"if", "then", "do", "{", "!", "for", "function"} for argv in commands)


def test_find_exec_runs_its_command() -> None:
    commands = unwrap(f"find . -name '*.log' -exec {TOOL} {{}} \\;")
    assert commands is not None and commands[1] == [TOOL, "{}"]


def test_a_comment_runs_nothing() -> None:
    assert unwrap(f"ls  # {TOOL} --flag") == [["ls"]]
    assert unwrap(f"ls # a note\n{TOOL} --flag") == [["ls"], [TOOL, "--flag"]]
    assert unwrap("echo a#b") == [["echo", "a#b"]]


def test_a_shell_running_a_script_file_is_itself() -> None:
    assert unwrap("bash build.sh") == [["bash", "build.sh"]]
    assert unwrap(f"command -v {TOOL}") == [["command", "-v", TOOL]]


# What it leaves alone


@pytest.mark.parametrize("opener", ["<<EOF", "<<'EOF'", '<<"EOF"', "<< EOF", "<<\\EOF"])
def test_a_heredoc_body_is_data(opener: str) -> None:
    assert unwrap(f"cat {opener}\n{TOOL} --flag\nEOF\nls") == [["cat"], ["ls"]]


def test_a_tab_stripped_heredoc_body_is_data() -> None:
    assert unwrap(f"cat <<-EOF\n\t{TOOL} --flag\n\tEOF\nls") == [["cat"], ["ls"]]


CONTROL = [
    ("ls -la", [["ls", "-la"]]),
    ("git status", [["git", "status"]]),
    ("git log --oneline -5", [["git", "log", "--oneline", "-5"]]),
    ('echo "hello world"', [["echo", "hello world"]]),
    ("echo 'a; b && c | d'", [["echo", "a; b && c | d"]]),
    ('echo "it\'s fine"', [["echo", "it's fine"]]),
    ("python -m pytest -q", [["python", "-m", "pytest", "-q"]]),
    ("pytest tests -k unwrap", [["pytest", "tests", "-k", "unwrap"]]),
    ("sed -n '1,20p' notes.txt", [["sed", "-n", "1,20p", "notes.txt"]]),
    ('grep -rn "def main" src', [["grep", "-rn", "def main", "src"]]),
    ("cat <<EOF\nplain text, nothing to run\nEOF", [["cat"]]),
    ("ls | wc -l", [["ls"], ["wc", "-l"]]),
    ("env PYTHONPATH=src pytest -q", [["pytest", "-q"]]),
    ("timeout 30 pytest -q", [["pytest", "-q"]]),
    ("nice -n 19 python -m mypy", [["python", "-m", "mypy"]]),
    ("mkdir -p build && ls build", [["mkdir", "-p", "build"], ["ls", "build"]]),
    ('python3 -c "print(1)"', [["python3", "-c", "print(1)"]]),
    ("wc -l < notes.txt", [["wc", "-l"]]),
    ("git diff --stat 2>&1", [["git", "diff", "--stat"]]),
    ("echo done  # a trailing note", [["echo", "done"]]),
]


def test_the_control_list_has_twenty() -> None:
    assert len(CONTROL) == 20


@pytest.mark.parametrize(("text", "expected"), CONTROL)
def test_an_ordinary_command_is_its_own_argv(text: str, expected: list[list[str]]) -> None:
    assert unwrap(text) == expected


# What it cannot read


@pytest.mark.parametrize("text", [f"echo '{TOOL}", f'echo "{TOOL}', f"echo $({TOOL}", f"echo `{TOOL}", "echo done \\"])
def test_an_unclosed_quote_is_none(text: str) -> None:
    assert unwrap(text) is None


def test_four_levels_of_nesting_are_read() -> None:
    assert unwrap(nest(4)) == [[TOOL]]


def test_five_levels_of_nesting_are_none() -> None:
    assert unwrap(nest(5)) is None
    assert unwrap(nest(2), depth=1) is None


@pytest.mark.parametrize(
    "text",
    [
        'eval "$x"',
        "eval $x",
        'sh -c "$x"',
        "$TOOL_NAME --flag",
        '"$(which ls)" -la',
        f"echo {TOOL} | sh",
        "bash -s",
        "env -S 'ls -la'",
        f"env -C sub {TOOL}",
        f"sudo -i {TOOL}",
        f"su -c '{TOOL}'  -",
    ],
)
def test_a_command_known_only_at_run_time_is_none(text: str) -> None:
    assert unwrap(text) is None


def test_the_command_behind_su_c_and_a_nested_sudo_is_read() -> None:
    assert unwrap(f"su -c '{TOOL} --flag'") == RUN
    assert unwrap(f"ls && bash -c 'sudo {TOOL} --flag'") == [["ls"], *RUN]


# Where a delete points


@pytest.fixture
def folders(tmp_path: Path) -> tuple[str, str]:
    work, sibling = tmp_path / "work", tmp_path / "sibling"
    work.mkdir()
    sibling.mkdir()
    (work / "delete-target").write_text("x")
    (work / "link-to-sibling").symlink_to(sibling)
    return str(work), str(sibling)


@pytest.mark.parametrize(
    "argv",
    [["rm", "delete-target"], ["rm", "-f", "--", "delete-target"], ["rmdir", "delete-target"],
     ["unlink", "delete-target"], ["shred", "-u", "delete-target"], ["trash", "delete-target"]],
)
def test_a_delete_in_the_work_folder_stays_inside(folders: tuple[str, str], argv: list[str]) -> None:
    work, _ = folders
    targets = delete_targets(argv, work)
    assert targets == [real(work, "delete-target")]
    assert inside(targets, work)


def test_a_delete_of_the_parent_is_outside(folders: tuple[str, str]) -> None:
    work, _ = folders
    targets = delete_targets(["rm", "-r", ".."], work)
    assert targets == [os.path.dirname(real(work))]
    assert not inside(targets, work)


def test_a_delete_through_a_symlink_resolves_to_the_sibling(folders: tuple[str, str]) -> None:
    work, sibling = folders
    assert delete_targets(["rm", "-r", "link-to-sibling"], work) == [real(sibling)]
    targets = delete_targets(["rm", "link-to-sibling/delete-target"], work)
    assert targets == [real(sibling, "delete-target")]
    assert not inside(targets, work)


def test_find_delete_resolves_to_its_start_path(folders: tuple[str, str]) -> None:
    work, _ = folders
    assert delete_targets(["find", ".", "-delete"], work) == [real(work)]
    assert delete_targets(["find", "-name", "delete-*", "-delete"], work) == [real(work)]
    assert delete_targets(["find", "-L", ".", "-delete"], work) is None


def test_git_clean_resolves_to_the_work_folder(folders: tuple[str, str]) -> None:
    work, _ = folders
    assert delete_targets(["git", "clean", "-fd"], work) == [real(work)]
    assert delete_targets(["git", "-C", "sub", "clean", "-f"], work) == [real(work, "sub")]
    assert delete_targets(["git", "clean", "-nd"], work) == []


@pytest.mark.parametrize("target", ["delete-*", "delete-?", "$TARGET_DIR", "~/delete-target", "`pwd`", "x{1,2}"])
def test_a_glob_or_a_variable_is_none(folders: tuple[str, str], target: str) -> None:
    assert delete_targets(["rm", target], folders[0]) is None


def test_a_delete_with_no_path_is_none(folders: tuple[str, str]) -> None:
    assert delete_targets(["rm", "-f"], folders[0]) is None  # as behind xargs


def test_a_non_delete_is_an_empty_list(folders: tuple[str, str]) -> None:
    work, _ = folders
    assert delete_targets(["ls", "-la"], work) == []
    assert delete_targets(["git", "status"], work) == []
    assert delete_targets(["find", ".", "-name", "delete-target"], work) == []
    assert delete_targets([], work) == []


def test_inside(folders: tuple[str, str]) -> None:
    work, sibling = folders
    assert not inside([], work)
    assert inside([work], work)
    assert not inside([real(work, "delete-target"), real(sibling)], work)


def test_unwrap_then_delete_targets(folders: tuple[str, str]) -> None:
    work, sibling = folders
    commands = unwrap("timeout 5 sh -c 'unlink ../sibling/delete-target'")
    assert commands == [["unlink", "../sibling/delete-target"]]
    targets = delete_targets(commands[0], work)
    assert targets == [real(sibling, "delete-target")]
    assert not inside(targets, work)


# From the cold read of the first version


def test_git_rm_removes_the_files_too(folders: tuple[str, str]) -> None:
    work, _ = folders
    assert delete_targets(["git", "rm", "-r", "--", "delete-target"], work) == [real(work, "delete-target")]
    assert delete_targets(["git", "-C", "sub", "rm", "delete-target"], work) == [real(work, "sub", "delete-target")]
    assert delete_targets(["git", "rm", "--cached", "delete-target"], work) == []
    assert delete_targets(["git", "rm", "-n", "delete-target"], work) == []
    assert delete_targets(["git", "rm", "-r"], work) is None


def test_arithmetic_is_a_number_not_a_command() -> None:
    assert unwrap("echo $((1+1))") == [["echo", "$((1+1))"]]
    assert unwrap("echo $(( (2+3)*4 ))") == [["echo", "$(( (2+3)*4 ))"]]
    assert unwrap(f"(( n = n + 1 )); {TOOL} --flag") == RUN
    assert unwrap("echo $((1+1") is None


def test_an_empty_path_is_none(folders: tuple[str, str]) -> None:
    work, _ = folders
    assert delete_targets(["rm", "-r", ""], work) is None


def test_rsync_delete_zip_m_and_tar_remove_files(folders: tuple[str, str]) -> None:
    work, sibling = folders
    assert delete_targets(["rsync", "-a", "--delete", "delete-target", "../sibling/"], work) == [real(sibling)]
    assert delete_targets(["rsync", "-a", "--delete", "src/", "host:/x"], work) is None
    assert delete_targets(["rsync", "-a", "src/", "dst/"], work) == []
    assert delete_targets(["zip", "-rm", "a.zip", "delete-target"], work) == [real(work, "delete-target")]
    assert delete_targets(["zip", "-r", "a.zip", "delete-target"], work) == []
    assert delete_targets(["tar", "--remove-files", "-cf", "a.tar", "delete-target"], work) is None
