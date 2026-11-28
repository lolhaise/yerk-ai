"""Checks add-ons (addons.py), put in with `yerk add` and taken out with `yerk remove`. The cases: `yerk add` from a folder
or a file:// git URL, each step a line, and `yerk remove` leaving the private folder byte-identical; an add-on over its own
ceiling, an entry the checks refuse and its own failing test each refused whole; the state read from the install; its
budget lines, rooms.toml keys and the attachment types it reads counted only while installed."""
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from yerk_ai import abilities, addons, cli, config, memory, shape, words, workers
from yerk_ai.channels.discord import message_text
from yerk_ai.config import EXAMPLE_PRIVATE, REPO

HELLO = Path(__file__).resolve().parents[1] / "examples" / "addons" / "hello"


def snapshot(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def fake(fail: str = ""):
    """The runner `yerk add` is given: a git clone is the real one (file:// is local); pip and pytest are said done, but
    for the command that holds `fail`."""
    def run(cmd, **k):
        run.ran.append(cmd)
        if cmd[0] == "git":
            return subprocess.run(cmd, **k)
        failed = bool(fail) and fail in cmd
        return subprocess.CompletedProcess(cmd, int(failed), "1 failed" if failed else "1 passed", "")
    run.ran = []
    return run


def pips(run) -> list[str]:
    """The pip verbs it ran, uv's pip or this Python's."""
    return [c[c.index("pip") + 1] for c in run.ran if "pip" in c]


def git_repo(tmp_path: Path, change=lambda r: None) -> str:
    shutil.copytree(HELLO, r := tmp_path / "yerk-hello", ignore=shutil.ignore_patterns("__pycache__"))
    change(r)
    for c in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t", "commit", "-qm", "hello"]):
        subprocess.run(["git", "-C", str(r), *c], check=True, capture_output=True)
    return r.as_uri()


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A copy of the example layer, a state folder and a systemd units folder of the test's own."""
    shutil.copytree(EXAMPLE_PRIVATE, priv := tmp_path / "private")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(addons, "importable", lambda m: True)        # the fake pip installed it
    monkeypatch.setattr(workers, "systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))   # no real unit stopped
    return priv, tmp_path / "state"


def test_a_dry_run_says_what_it_would_do_and_writes_nothing(install, capsys):
    priv, state = install
    before = snapshot(priv)
    assert addons.add(str(HELLO), priv, state, dry=True, run=fake()) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == [f"{words.DONE} fetched {HELLO}", f"{words.DONE} checked hello 0.1.0: {addons.size(HELLO)} lines of code, its ceiling 40"]
    assert len(out) == 10 and all(x.startswith(f"{words.PLAIN} would ") for x in out[2:-1]) and out[-1] == f"{words.PLAIN} dry run: nothing written"
    assert snapshot(priv) == before and list((state / "addons").iterdir()) == [] and not addons.units_dir().exists()


@pytest.mark.parametrize("how", ["folder", "file:// URL"])
def test_each_step_is_written_and_said_and_remove_leaves_the_private_folder_byte_identical(install, tmp_path, capsys, how):
    priv, state = install
    before, run = snapshot(priv), fake()
    src = str(HELLO) if how == "folder" else git_repo(tmp_path)
    assert addons.add(src, priv, state, run=run) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 10 and all(x.startswith(words.DONE) for x in out) and out[-1].endswith("added hello 0.1.0: the rooms get it at yerkd's next start")
    rs = config.rooms(priv)                         # the loaders that start yerkd: every owner room has its server and tool
    assert all(c["allow"] == ["mcp__hello__hello"] and c["mcp"]["hello"]["args"][-1] == n for n, c in rs.items()) and len(rs) == 2
    assert config.load_toml(priv / "yerkd.toml")["hello"] == {"greeting": "Hello"}
    link = addons.units_dir() / "yerk-hello.service"
    assert link.resolve() == (state / "addons" / "hello" / "units" / "yerk-hello.service").resolve()
    assert pips(run) == ["install"] and any("pytest" in c for c in run.ran)
    tab = abilities.manifest(state)
    assert abilities.line("hello", tab["hello"], tab, memory.settings(priv, state)) == f"{words.DONE} hello: installed, version 0.1.0"
    assert addons.remove("hello", priv, state, run=run) == 0
    assert snapshot(priv) == before and not link.is_symlink() and not (state / "addons" / "hello").exists()
    assert pips(run) == ["install", "uninstall"] and "hello" not in abilities.manifest(state)


def test_an_add_on_over_its_own_ceiling_is_refused_and_nothing_is_written(install, tmp_path, capsys):
    priv, state = install
    shutil.copytree(HELLO, big := tmp_path / "hello")
    (big / "yerk_hello" / "more.py").write_text("x = 1\n" * (41 - addons.size(HELLO)))     # 41 lines against its ceiling of 40
    before, run = snapshot(priv), fake()
    assert addons.add(str(big), priv, state, run=run) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.DONE} fetched {big}",
                                                    f"{words.NOT_DONE} not added: its code is 41 lines, over its own ceiling of 40"]
    assert snapshot(priv) == before and run.ran == [] and list((state / "addons").iterdir()) == []


@pytest.mark.parametrize("old,new,why", [
    ('allow = ["mcp__hello__hello"]', 'allow = ["Bash"]', "allow Bash is not a tool of its own server (mcp__hello__...)"),
    ('"session_start.claude_code.deferred_tool_names"', '"session_start.total"',
     "budget line session_start.total: an add-on adds session_start and per_message parts, never a total"),
    ('tests = ["tests"]', 'tests = ["tests"]\nhooks = ["Stop"]', f"install hooks: no step reads it ({', '.join(addons.INSTALL)})"),
    ("abilities.hello", "abilities.web", "web is a core ability"),
    ('words = "yerk_hello/words.py"', 'words = "words.toml"', "no words.toml in it"),
    ("abilities.hello", 'abilities."../escape"', "its name ../escape is not one plain word: letters, digits, - and _ only"),
    ("size = 40\n", "", "its entry has no size"),
    ('settings = { hello = { greeting = "Hello" } }', 'settings = ["hello"]', "install settings is not a table"),
    ("size = 40\n\n[abilities.hello.install]", 'size = 40\ninstall = "x"\n\n[abilities.hello.unused]',
     f"install is not a table: no step reads it ({', '.join(addons.INSTALL)})"),
    ('names" = 300', 'names" = "300"', "budget line session_start.claude_code.deferred_tool_names = '300': not a whole number"),
    ('names" = 300', 'names" = -300', "budget line session_start.claude_code.deferred_tool_names = -300: not a whole number"),
    ('names" = 300', 'names" = true', "budget line session_start.claude_code.deferred_tool_names = True: not a whole number"),
    ('words = "yerk_hello/words.py"\n', "", "install names no words file or no tests, and an add-on brings both"),
    ('words = "yerk_hello/words.py"', 'words = "../../outside/words.py"', "../../outside/words.py is outside its folder"),
    ('words = "yerk_hello/words.py"', f'words = "{HELLO / "yerk_hello" / "words.py"}"',
     f"{HELLO / 'yerk_hello' / 'words.py'} is outside its folder"),
    ('tests = ["tests"]', f'tests = ["{HELLO / "tests"}"]', f"{HELLO / 'tests'} is outside its folder"),
    ('units = ["units/yerk-hello.service"]', 'units = ["../evil.service"]', "../evil.service is outside its folder"),
    ('tests = ["tests"]', 'tests = ["tests"]\nreads = { "video/*" = "mcp__hello__frames" }', "reads video/* = mcp__hello__frames: not a tool on its own allow list"),
])
def test_an_entry_the_checks_refuse_writes_nothing(install, tmp_path, capsys, old, new, why):
    priv, state = install
    shutil.copytree(HELLO, bad := tmp_path / "hello")
    (bad / "addon.toml").write_text((bad / "addon.toml").read_text().replace(old, new))
    before = snapshot(priv)
    assert addons.add(str(bad), priv, state, run=fake()) == 1
    assert capsys.readouterr().out.splitlines()[-1] == f"{words.NOT_DONE} not added: {why}" and snapshot(priv) == before


def test_a_path_that_leaves_it_through_a_link_is_refused(install, tmp_path, capsys):
    priv, state = install
    (out := tmp_path / "words.py").write_text('GREETING = "x"\n')

    def link(r):                        # a git clone keeps the link, where a folder's copy takes the file it points to
        (w := r / "yerk_hello" / "words.py").unlink()
        w.symlink_to(out)
    before = snapshot(priv)
    assert addons.add(git_repo(tmp_path, link), priv, state, run=fake()) == 1
    assert capsys.readouterr().out.splitlines()[-1] == f"{words.NOT_DONE} not added: yerk_hello/words.py is outside its folder"
    assert snapshot(priv) == before and list((state / "addons").iterdir()) == []


def test_its_own_failing_test_undoes_every_step(install, capsys):
    priv, state = install
    before, run = snapshot(priv), fake(fail="pytest")
    assert addons.add(str(HELLO), priv, state, run=run) == 1
    assert capsys.readouterr().out.splitlines()[-1] == f"{words.NOT_DONE} not added: its fast tests failed: 1 failed"
    assert snapshot(priv) == before and not (addons.units_dir() / "yerk-hello.service").is_symlink()
    assert not (state / "addons" / "hello").exists() and pips(run) == ["install", "uninstall"]


@pytest.mark.parametrize("error", [ValueError, TypeError, KeyError])
def test_a_late_failure_of_any_kind_undoes_every_step(install, capsys, error):
    priv, state = install
    before, ran = snapshot(priv), fake()

    def run(cmd, **k):                  # every step done, then the last one raises, as a string budget value did at its line
        if "pytest" in cmd:
            raise error("late")
        return ran(cmd, **k)
    assert addons.add(str(HELLO), priv, state, run=run) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith(f"{words.NOT_DONE} not added: ")
    assert snapshot(priv) == before and not (addons.units_dir() / "yerk-hello.service").is_symlink()
    assert not (state / "addons" / "hello").exists() and pips(ran) == ["install", "uninstall"]


ITS_RUN = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--tb=line", "tests"]   # yerk add's own command


def walled(monkeypatch, code=0, out="1 passed"):
    """workers.checked as `yerk add` calls it, each call kept: (argv, cwd, tree, private, state, the rest); `code` and `out` its result."""
    seen = []

    def checked(argv, cwd, tree, private, state, **k):
        seen.append((argv, cwd, tree, private, state, k))
        return subprocess.CompletedProcess(argv, code, out, "")
    monkeypatch.setattr(workers, "checked", checked)
    return seen


def test_its_tests_run_once_behind_the_deploy_s_wall_in_its_folder_on_this_tree_layer_and_state(install, monkeypatch, capsys):
    priv, state = install
    seen, run = walled(monkeypatch), fake()
    assert addons.add(str(HELLO), priv, state, run=run) == 0
    assert seen == [(ITS_RUN, state / "addons" / "hello", REPO, priv, state, {"run": run})]
    assert not any("pytest" in c for c in run.ran) and words.WALL_OFF not in capsys.readouterr().out   # never a plain run too


def test_the_walled_run_reaches_add_s_runner_as_one_systemd_run_of_its_command(install):
    priv, state = install
    assert addons.add(str(HELLO), priv, state, run=(run := fake()), say=lambda _: None) == 0
    [cmd] = [c for c in run.ran if "pytest" in c]
    assert cmd[:2] == ["systemd-run", "--user"] and f"--working-directory={state / 'addons' / 'hello'}" in cmd
    assert cmd[cmd.index("--") + 1:] == [*ITS_RUN[:3], *workers.PYTEST_WALLED, *ITS_RUN[3:]]
    assert {f"--setenv=PYTHONPATH={REPO}", f"--setenv=YERK_PRIVATE={priv}", f"--setenv=YERK_STATE={state}"} <= set(cmd)


def test_deploy_wall_false_runs_them_plain_as_before_and_says_so_in_one_line(install, monkeypatch, capsys):
    priv, state = install
    (priv / "yerkd.toml").write_text("[deploy]\nwall = false\n")
    seen, run = walled(monkeypatch), fake()
    assert addons.add(str(HELLO), priv, state, run=run) == 0
    assert seen == [] and [c for c in run.ran if "pytest" in c] == [ITS_RUN]
    out = capsys.readouterr().out.splitlines()
    assert out[-3:-1] == [f"{words.DONE} ran its fast tests (tests)", f"{words.PLAIN} tests ran with no wall: [deploy] wall"]


def test_no_wall_to_run_behind_adds_nothing_and_its_line_names_the_setting(install, monkeypatch, capsys):
    priv, state = install
    before, run = snapshot(priv), fake()

    def no_wall(*_a, **_k):
        raise workers.NoWall("systemd-run exited 1: Failed to connect to bus: /run/user/1000/bus is not there")
    monkeypatch.setattr(workers, "checked", no_wall)
    assert addons.add(str(HELLO), priv, state, run=run) == 1
    assert capsys.readouterr().out.splitlines()[-1] == (f"{words.NOT_DONE} not added: its fast tests did not run, no wall to run them "
                                                        "behind ([deploy] wall): systemd-run exited 1: Failed to connect to bus: bus is not there")
    assert snapshot(priv) == before and not (addons.units_dir() / "yerk-hello.service").is_symlink()
    assert not (state / "addons" / "hello").exists() and pips(run) == ["install", "uninstall"] and not any("pytest" in c for c in run.ran)


def test_a_failing_test_behind_the_wall_says_today_s_words_and_a_dry_run_runs_nothing(install, monkeypatch, capsys):
    priv, state = install
    before, seen = snapshot(priv), walled(monkeypatch, 1, "tests/test_hello.py:3: assert 1 == 2\n1 failed in 0.01s")
    assert addons.add(str(HELLO), priv, state, run=fake()) == 1
    assert capsys.readouterr().out.splitlines()[-1] == f"{words.NOT_DONE} not added: its fast tests failed: 1 failed in 0.01s"
    assert len(seen) == 1 and snapshot(priv) == before and not (state / "addons" / "hello").exists()
    assert addons.add(str(HELLO), priv, state, dry=True, run=(run := fake()), say=lambda _: None) == 0
    assert len(seen) == 1 and not any("pytest" in c for c in run.ran)


@pytest.mark.parametrize("where", ["out of the state folder", "inside it"])
def test_a_folder_the_wall_would_hide_is_not_added_and_every_step_is_undone(install, tmp_path, capsys, where):
    priv, state = install
    (state / "addons").parent.mkdir(parents=True)
    (state / "addons").symlink_to(away := tmp_path / "away" if where == "out of the state folder" else state / "kept")
    away.mkdir()
    before, run = snapshot(priv), fake()
    assert addons.add(str(HELLO), priv, state, run=run) == (1 if away.parent == tmp_path else 0)
    if away.parent == tmp_path:
        assert capsys.readouterr().out.splitlines()[-1] == f"{words.NOT_DONE} not added: its folder lies outside what a check may read"
        assert snapshot(priv) == before and not (state / "addons" / "hello").exists() and not any("pytest" in c for c in run.ran)
    else:
        assert len([c for c in run.ran if "pytest" in c]) == 1


def test_a_package_that_will_not_uninstall_is_said_and_the_rest_is_removed(install, capsys):
    priv, state = install
    before = snapshot(priv)
    assert addons.add(str(HELLO), priv, state, run=fake(), say=lambda _: None) == 0
    assert addons.remove("hello", priv, state, run=fake(fail="uninstall")) == 0
    assert f"{words.NOT_DONE} not uninstalled: its package yerk-hello; the rest is removed" in capsys.readouterr().out.splitlines()
    assert snapshot(priv) == before and not (state / "addons" / "hello").exists()


def test_a_registry_name_fetches_the_repo_it_names_at_its_version_and_the_state_is_read_from_the_install(install, tmp_path, monkeypatch, capsys):
    priv, state = install
    # an entry that names a repo is read from the install, whatever its state says ("described" is for one with no repo)
    (reg := tmp_path / "abilities.toml").write_text(f'[abilities.hello]\nstate = "described"\nrepo = "{(url := git_repo(tmp_path))}"\nversion = "0.2.0"\n')
    monkeypatch.setattr(addons, "table", lambda name: reg)
    s, ab = memory.settings(priv, state), config.load_toml(reg)["abilities"]["hello"]
    assert abilities.state("hello", ab, {"hello": ab}, s) == words.ability("available", name="hello")
    assert addons.add("hello", priv, state, run=fake()) == 1
    assert capsys.readouterr().out.splitlines()[-1] == f"{words.NOT_DONE} not added: it is version 0.1.0 and the registry names 0.2.0"
    reg.write_text(reg.read_text().replace("0.2.0", "0.1.0"))
    assert addons.add("hello", priv, state, run=fake()) == 0 and capsys.readouterr().out.splitlines()[0] == f"{words.DONE} fetched {url}"
    tab = abilities.manifest(state)
    assert abilities.state("hello", tab["hello"], tab, s) == words.ability("installed", version="0.1.0")
    monkeypatch.setattr(addons, "importable", lambda m: False)
    (addons.units_dir() / "yerk-hello.service").unlink()
    assert abilities.state("hello", tab["hello"], tab, s) == words.ability("broken", what="its package, unit yerk-hello.service")
    assert addons.add("nothing", priv, state) == 2


def test_its_budget_lines_and_rooms_toml_keys_count_only_while_it_is_installed(install):
    priv, state = install
    core = config.budgets()
    assert addons.with_lines(core, state) == core and addons.room_keys(state) == frozenset()
    assert addons.add(str(HELLO), priv, state, run=fake(), say=lambda _: None) == 0
    b, c = addons.with_lines(core, state)["session_start"], core["session_start"]
    assert b["claude_code"]["deferred_tool_names"] == c["claude_code"]["deferred_tool_names"] + 300 and b["total"] == c["total"] + 300
    assert sum(b["claude_code"].values()) + sum(b["appended"].values()) == b["total"] and core == config.budgets()
    assert addons.room_keys(state) == {"hello_name"}
    assert addons.remove("hello", priv, state, run=fake(), say=lambda _: None) == 0
    assert addons.with_lines(core, state) == core and addons.room_keys(state) == frozenset()


def test_an_attachment_type_it_reads_names_its_tool_under_the_attachment_line_only_while_it_is_installed(install, tmp_path):
    priv, state = install
    shutil.copytree(HELLO, a := tmp_path / "hello")
    (a / "addon.toml").write_text((a / "addon.toml").read_text().replace('tests = ["tests"]', 'tests = ["tests"]\nreads = { "video/*" = "mcp__hello__hello" }'))
    files = [{"name": n, "type": t, "size": 10, "path": f"/s/inbox/9/{n}", "ok": True} for n, t in (("clip.mp4", "video/mp4"), ("photo.png", "image/png"))]

    def said() -> list[str]:            # the room's text for a message with a video and a picture, as yerkd builds it
        return message_text(NS(clean_content="?", content="?"), files, "", addons.reads(state)).splitlines()
    clip, photo = (f"[attachment] {n}, {t}, 10 bytes, saved at /s/inbox/9/{n}" for n, t in (("clip.mp4", "video/mp4"), ("photo.png", "image/png")))
    assert addons.reads(state) == {} and said() == ["?", clip, words.VIDEO_FILE.format(name="clip.mp4"), photo]    # watch's own line
    assert addons.add(str(a), priv, state, run=fake(), say=lambda _: None) == 0
    line = words.ADDON_READS.format(name="clip.mp4", tool="mcp__hello__hello")
    assert addons.reads(state) == {"video/*": "mcp__hello__hello"} and said()[3] == line and len(said()) == 5 and shape.offenses(line) == []
    assert addons.remove("hello", priv, state, run=fake(), say=lambda _: None) == 0
    assert addons.reads(state) == {} and len(said()) == 4


def test_yerk_add_refuses_the_example_layer_and_yerk_status_names_the_installed_add_ons(install, monkeypatch, capsys):
    priv, state = install
    monkeypatch.delenv("YERK_PRIVATE", raising=False)
    monkeypatch.setenv("YERK_CLAUDE", str(Path(__file__).with_name("fake_claude.py")))     # the card's /usage call, never the real one
    assert cli.main(["add", str(HELLO), "--state", str(state)]) == 2 and words.NO_LAYER in capsys.readouterr().err
    assert cli.main(["status", "--private", str(priv), "--state", str(state)]) == 0
    assert f"{words.PLAIN} add-ons: none installed" in capsys.readouterr().out.splitlines()
    assert addons.add(str(HELLO), priv, state, run=fake(), say=lambda _: None) == 0
    assert cli.main(["status", "--private", str(priv), "--state", str(state)]) == 0
    assert f"{words.PLAIN} add-ons: hello 0.1.0" in capsys.readouterr().out.splitlines()
