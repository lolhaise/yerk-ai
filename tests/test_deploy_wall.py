"""Checks the wall a deploy's checks run behind (workers.checked: its fast tests, each add-on's and the recall controls, one
command in a unit of its own, waited for): the unit's properties as composed from a hidden worker's parts, its fixed
environment, the command, the refusals when no unit starts, a worker's own unit as it was, and one real unit (a canary)
where a user bus answers. Every path is made up under tmp_path; no test but the last starts a unit."""
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from yerk_ai import deploy, memory, words, workers
from yerk_ai.config import EXAMPLE_PRIVATE, REPO


def made_up(tmp_path, monkeypatch, state_outside=False):
    """A made-up home holding a tree, an interpreter's two folders, a private layer with secrets/ and a state folder."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    tree, venv, py, private = home / "tree", home / "venv", home / "python", home / "private"
    state = tmp_path / "state" if state_outside else home / "state"
    for d in (tree, venv, py, private / "secrets", state):
        d.mkdir(parents=True)
    (private / "secrets" / "denylist.txt").write_text("a-made-up-name\n")
    (private / "secrets" / "example.key").write_text("not a key\n")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(py))
    monkeypatch.delenv("YERK_DENYLIST_FILE", raising=False)
    return home, tree, private, state


def binds(props):
    """What the unit binds back read-only, the resolver's file (a door's: workers.doors) left out."""
    return [b for x in props if x.startswith("BindReadOnlyPaths=") and (b := x.split("=", 1)[1]) not in map(str, workers.resolver())]


def test_a_deploy_check_s_unit_hides_the_home_and_binds_back_read_only_what_the_check_reads(tmp_path, monkeypatch):
    home, tree, private, state = made_up(tmp_path, monkeypatch)
    props = workers.check_props(tree, private, state, tree)
    assert props[:2] == ["PrivateUsers=yes", f"TemporaryFileSystem={home}:ro"]
    assert binds(props) == [str(tree), sys.prefix, sys.base_prefix, str(private), str(state)]     # each once, read-only
    assert not [x for x in props if x.startswith(("BindPaths=", "ReadWritePaths="))]
    assert f"InaccessiblePaths=-{private / 'secrets'}" in props and not [x for x in props if "denylist" in x]
    assert f"InaccessiblePaths=-{state / 'secrets'}" in props          # nor the state folder's own: the install's secret
    assert all(d in props for d in workers.doors()) and all(w in props for w in workers.WALLS)
    for p, _ in workers.DOORS:                                          # every door that is there, shut
        assert not p.exists() or [x for x in props if x.split("=", 1)[1].split(":")[0] == str(p)], p
    assert not [x for x in props if x.startswith("PrivateNetwork")]   # the network stays the deploy's own
    elsewhere = home / "addon"                                          # the folder it runs in, when it is none of those
    elsewhere.mkdir()
    assert binds(workers.check_props(tree, private, state, elsewhere))[-1] == str(elsewhere)
    assert binds(workers.check_props(tree, private, state, state / "x")) == binds(props)   # one not there: nothing to bind
    home, tree, private, state = made_up(tmp_path / "b", monkeypatch, state_outside=True)
    props = workers.check_props(tree, private, state, tree)
    assert props[1:3] == [f"TemporaryFileSystem={home}:ro", f"TemporaryFileSystem={state}:ro"] and str(state) in binds(props)
    assert f"InaccessiblePaths=-{state / 'secrets'}" in props


def test_the_denylist_file_comes_back_alone_and_only_from_the_private_layer(tmp_path, monkeypatch):
    home, tree, private, state = made_up(tmp_path, monkeypatch)
    deny, secrets = private / "secrets" / "denylist.txt", private / "secrets"
    props = workers.check_props(tree, private, state, tree, str(deny))
    assert f"TemporaryFileSystem={secrets}:ro" in props and binds(props)[-1] == str(deny) and binds(props).count(str(deny)) == 1
    assert not [x for x in props if x.startswith("InaccessiblePaths=") and str(secrets) in x]
    outside = home / "elsewhere" / "denylist.txt"
    outside.parent.mkdir()
    outside.write_text("a-made-up-name\n")
    (secrets / "link.txt").symlink_to(outside)
    for named in ("", str(outside), str(secrets / "link.txt"), str(secrets / "missing.txt")):
        props = workers.check_props(tree, private, state, tree, named)
        assert f"InaccessiblePaths=-{secrets}" in props and not [x for x in binds(props) if x.startswith(str(secrets)) or x == str(outside)], named


def test_a_file_its_caller_s_check_reads_comes_back_read_only_and_never_one_out_of_a_secrets_folder(tmp_path, monkeypatch):
    """`reads`: the one more thing a check reads (a deploy's fast tests: each room's newest transcript, deploy.transcripts)."""
    home, tree, private, state = made_up(tmp_path, monkeypatch)
    seen = home / "acct" / "projects" / "p" / "s.jsonl"
    seen.parent.mkdir(parents=True)
    seen.write_text("{}\n")
    (state / "secrets").mkdir()
    (state / "secrets" / "k").write_text("not a key\n")
    hid = [private / "secrets" / "example.key", state / "secrets" / "k", seen.with_name("missing.jsonl"), seen.parent]
    props = workers.check_props(tree, private, state, tree, "", [seen, *hid])
    assert binds(props)[-1] == str(seen) and binds(props).count(str(seen)) == 1
    assert not [b for b in binds(props) if b in map(str, hid)] and not [x for x in props if x.startswith("BindPaths=")]
    assert binds(workers.check_props(tree, private, state, tree)) == binds(props)[:-1]      # none given: none bound
    ran = []
    workers.checked(["python"], cwd=tree, tree=tree, private=private, state=state, reads=[seen],
                    run=lambda cmd, **_k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert f"BindReadOnlyPaths={seen}" in ran[0]


def test_a_wall_added_to_the_walls_is_in_the_check_with_no_other_change(tmp_path, monkeypatch):
    _, tree, private, state = made_up(tmp_path, monkeypatch)
    monkeypatch.setattr(workers, "WALLS", (*workers.WALLS, "ProtectHostname=yes"))
    assert workers.check_props(tree, private, state, tree)[-1] == "ProtectHostname=yes"


def test_the_check_s_environment_is_a_fixed_list_and_never_the_deploy_s_own(tmp_path, monkeypatch):
    home, tree, private, state = made_up(tmp_path, monkeypatch)
    for k, v in {"USER": "someone", "LOGNAME": "someone", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin", "YERK_PLANTED_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("YERK_DENYLIST_FILE", raising=False)
    monkeypatch.delenv(memory.INDEX_SEEN, raising=False)
    env = workers.check_env(tree, private, state)
    assert env == {"HOME": str(home), "USER": "someone", "LOGNAME": "someone", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin",
                   "TMPDIR": "/tmp", "YERK_PRIVATE": str(private), "YERK_STATE": str(state), "PYTHONPATH": str(tree),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    monkeypatch.setenv("YERK_DENYLIST_FILE", str(private / "secrets" / "denylist.txt"))
    assert workers.check_env(tree, private, state) == {**env, "YERK_DENYLIST_FILE": str(private / "secrets" / "denylist.txt")}
    monkeypatch.setenv(memory.INDEX_SEEN, "4242")                      # the pid the deploy saw the index alive under
    assert workers.check_env(tree, private, state)[memory.INDEX_SEEN] == "4242"
    assert workers.CHECK_ENV == ("HOME", "USER", "LOGNAME", "LANG", "PATH")   # the two lists whole: a name added is a door
    assert workers.CHECK_PASS == ("YERK_DENYLIST_FILE", memory.INDEX_SEEN)


def test_a_recall_behind_the_wall_takes_the_deploy_s_word_for_the_index_s_pid_and_for_no_other(tmp_path, monkeypatch):
    """A walled check's process list is its own, so yerk-index's pid is never in it: the recall there reads the pid the deploy
    saw alive (YERK_INDEX_SEEN) and skips its own look for that pid alone."""
    s = memory.settings(EXAMPLE_PRIVATE, tmp_path)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()                                                        # a pid this process cannot see, as behind the wall
    s.index_status.parent.mkdir(parents=True, exist_ok=True)
    s.index_status.write_text(json.dumps({"pid": gone.pid, "port": 1, "embeddings": {"key": True, "vectors": 3}}))
    monkeypatch.delenv(memory.INDEX_SEEN, raising=False)
    assert memory.meaning_state(s) == "no-index"
    monkeypatch.setenv(memory.INDEX_SEEN, str(gone.pid + 1))           # the deploy saw another pid: not this one
    assert memory.meaning_state(s) == "no-index"
    monkeypatch.setenv(memory.INDEX_SEEN, str(gone.pid))
    assert memory.meaning_state(s) == "ok"


def test_checked_runs_one_command_in_a_unit_of_its_own_and_returns_what_subprocess_run_does(tmp_path, monkeypatch):
    _, tree, private, state = made_up(tmp_path, monkeypatch)
    ran = []

    def run(cmd, **k):
        ran.append((cmd, k))
        return subprocess.CompletedProcess(cmd, 1, "1 failed\n", "a warning\n")
    argv = [sys.executable, "-m", "pytest", "-q", "-x", "--tb=line", "tests/test_x.py"]
    r = workers.checked(argv, cwd=tree, tree=tree, private=private, state=state, timeout=120, run=run)
    assert (r.args, r.returncode, r.stdout, r.stderr) == ([*argv[:3], *workers.PYTEST_WALLED, *argv[3:]], 1, "1 failed\n", "a warning\n")
    cmd, k = ran[0]
    assert cmd[:6] == ["systemd-run", "--user", "--wait", "--pipe", "--collect", "--quiet"] and k["stdin"] == subprocess.DEVNULL
    unit = cmd[6].removeprefix("--unit=")
    assert re.fullmatch(rf"yerk-deploy-check-{os.getpid()}-\d+", unit) and not unit.startswith(workers.UNIT)
    head = cmd[:cmd.index("--")]
    props = [head[i + 1] for i, x in enumerate(head) if x == "-p"]
    assert props == ["RuntimeMaxSec=120", "TimeoutStopSec=10", f"MemoryMax={workers.MEMORY_MAX}", "MemorySwapMax=0",
                     *workers.check_props(tree, private, state, tree)]
    assert [x.removeprefix("--setenv=") for x in cmd if x.startswith("--setenv=")] == [f"{a}={b}" for a, b in workers.check_env(tree, private, state).items()]
    assert cmd[cmd.index("--") + 1:] == r.args and f"--working-directory={tree}" in cmd
    controls = [sys.executable, "-m", "yerk_ai.cli", "recall-bench", "--controls"]
    assert workers.checked(controls, cwd=tree, tree=tree, private=private, state=state, run=run).args == controls   # not pytest: as given
    assert ran[1][0][6] != ran[0][0][6] and f"RuntimeMaxSec={workers.CHECK_SECONDS}" in ran[1][0]


@pytest.mark.parametrize("answer", [FileNotFoundError(2, "No such file or directory"), (1, "", "Failed to connect to bus: No medium found\n"),
                                    (1, "", "Unknown assignment: Made=up\n"), (226, "", ""), subprocess.TimeoutExpired("systemd-run", 1)])
def test_no_unit_started_is_no_wall_and_never_a_run_outside_it(tmp_path, monkeypatch, answer):
    _, tree, private, state = made_up(tmp_path, monkeypatch)
    stopped = []
    monkeypatch.setattr(workers, "systemctl", lambda *a, **k: stopped.append(a))

    def run(cmd, **_k):
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(cmd, *answer)
    with pytest.raises(workers.NoWall, match=r"^systemd-run"):
        workers.checked([sys.executable, "-m", "pytest"], cwd=tree, tree=tree, private=private, state=state, run=run)
    assert stopped == ([] if isinstance(answer, tuple) else [("stop", stopped[0][1])])   # a run that raised: its unit stopped


def test_a_check_that_ran_and_failed_is_its_own_result_and_ctrl_c_stops_its_unit(tmp_path, monkeypatch):
    _, tree, private, state = made_up(tmp_path, monkeypatch)
    stopped = []
    monkeypatch.setattr(workers, "systemctl", lambda *a, **k: stopped.append(a))
    for code, out, err in [(1, "", "Traceback (most recent call last):\n"), (2, '{"missed": ["c1"]}\n', ""), (0, "", "")]:
        r = workers.checked(["python"], cwd=tree, tree=tree, private=private, state=state,
                            run=lambda cmd, **_k: subprocess.CompletedProcess(cmd, code, out, err))
        assert (r.returncode, r.stdout, r.stderr) == (code, out, err)

    def ctrl_c(cmd, **_k):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        workers.checked(["python"], cwd=tree, tree=tree, private=private, state=state, run=ctrl_c)
    assert len(stopped) == 1 and stopped[0][0] == "stop" and stopped[0][1].startswith(workers.CHECK_UNIT)


def test_a_worker_s_own_walled_unit_is_as_it_was(tmp_path, monkeypatch):
    """This build shares the doors with a hidden worker's unit (workers.doors) and changes nothing of it: its answer, whole."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    state = home / "state"
    folder = state / "workers" / "abc123"
    folder.mkdir(parents=True)
    (state / "docs").mkdir()
    shut = {"own": "TemporaryFileSystem={}:mode=0700", "gone": "InaccessiblePaths={}", "empty": "TemporaryFileSystem={}:ro"}
    assert workers.Systemd(state, home="hidden").walled({}, folder, {}) == [
        "PrivateUsers=yes", f"TemporaryFileSystem={home}:ro", f"BindPaths={folder}", f"BindReadOnlyPaths={state / 'docs'}",
        *(shut[how].format(p) for p, how in workers.DOORS if p.exists()), *(f"BindReadOnlyPaths={p}" for p in workers.resolver()),
        *workers.WALLS]


def in_worker(pid: int = 0) -> bool:
    """This process or one it runs under is in a Yerk worker's unit: a run through a scope of its own (a heavy-run) is in
    another cgroup, but its caller is not, and the unit it would start sees neither the worker's /tmp nor its home."""
    pid = pid or os.getpid()
    while pid > 1:
        try:
            if "/yerk-worker-" in Path(f"/proc/{pid}/cgroup").read_text():
                return True
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def no_real_unit() -> str:
    """Why no real unit starts here, or "" when one can: a user bus that answers, systemd-run there, and not a Yerk worker's own
    unit (its gate refuses systemd-run, so a worker's suite never starts one; the box's CI has no bus)."""
    if not shutil.which("systemd-run"):
        return "no systemd-run here"
    if in_worker():
        return "a Yerk worker's own unit: its gate refuses systemd-run; a person's run of this test starts the unit"
    p = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=20)
    return "" if p.stdout.strip() in ("running", "degraded", "starting", "initializing") else "no user bus answers here"


CANARY = '''import json, os, socket
from pathlib import Path


def tried(f):
    try:
        f()
        return True
    except OSError:
        return False


def meaning(private, state):
    from yerk_ai import memory
    return memory.meaning_state(memory.settings(private, state))


def test_canary():
    """What this process can see, read and write, one JSON line on stdout."""
    home, tree, private, state = Path.home(), Path.cwd(), Path(os.environ["YERK_PRIVATE"]), Path(os.environ["YERK_STATE"])
    seen = {
        "home": sorted(os.listdir(home)),
        "planted": tried(lambda: (Path(__file__).parent.parent / "planted.key").read_text()),
        "tree_read": tried(lambda: (tree / "f.txt").read_text()),
        "writes": [tried(lambda p=p: (p / "canary-wrote").write_text("x")) for p in (tree, private, state)],
        "secret": tried(lambda: (private / "secrets" / "example.key").read_text()),
        "state_secret": tried(lambda: (state / "secrets" / "yerk-http.secret").read_text()),
        "given": [tried(lambda n=n: (Path(__file__).parent.parent / "acct" / "projects" / "p" / n).read_text()) for n in ("s.jsonl", "other.jsonl")],
        "meaning": meaning(private, state),
        "denylist": tried(lambda: Path(os.environ["YERK_DENYLIST_FILE"]).read_text()),
        "imports": tried(lambda: __import__("yerk_ai.words")) and Path(__import__("yerk_ai").__file__).is_relative_to(tree),
        "server": tried(lambda: socket.create_connection(("127.0.0.1", int((tree / "port").read_text())), timeout=5).close()),
        "env": sorted(os.environ),
    }
    print("CANARY " + json.dumps(seen))
'''


@pytest.mark.skipif(bool(why := no_real_unit()), reason=why or "-")
def test_the_real_wall_hides_the_home_refuses_writes_and_keeps_the_loopback(tmp_path, monkeypatch):
    """The one real unit: a canary test file in a made-up tree, run through workers.checked as a deploy's fast tests are, then
    the same canary plainly, which sees the home: the wall is proven, not a broken canary. Every unit it starts is stopped."""
    tree, private, state = tmp_path / "tree", tmp_path / "private", tmp_path / "state"
    shutil.copytree(REPO / "yerk_ai", tree / "yerk_ai", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "secrets").mkdir()
    (private / "secrets" / "example.key").write_text("not a key\n")
    (private / "secrets" / "denylist.txt").write_text("a-made-up-name\n")
    state.mkdir(mode=0o700)
    (state / "secrets").mkdir()
    (state / "secrets" / "yerk-http.secret").write_text("not a secret\n")
    (state / "memory").mkdir()                                         # an index alive out here, under this test's own pid
    (state / "memory" / "index.json").write_text(json.dumps({"pid": os.getpid(), "port": 1, "embeddings": {"key": True, "vectors": 3}}))
    (tree / "f.txt").write_text("the tree\n")
    (tree / "test_canary.py").write_text(CANARY)
    (tmp_path / "planted.key").write_text("not a key either\n")        # beside the tree, never bound back
    (said := tmp_path / "acct" / "projects" / "p" / "s.jsonl").parent.mkdir(parents=True)
    said.write_text("{}\n")                                            # a room's transcript: the one file given to read
    said.with_name("other.jsonl").write_text("{}\n")                   # its neighbour: not given
    monkeypatch.setenv("YERK_DENYLIST_FILE", str(private / "secrets" / "denylist.txt"))
    monkeypatch.setenv("YERK_PLANTED_KEY", "k")                       # the deploy's own environment: never the unit's
    monkeypatch.setenv(memory.INDEX_SEEN, str(os.getpid()))           # as the deploy's hold says it (deploy.index_held)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (tree / "port").write_text(str(server.server_address[1]))
    argv = [sys.executable, "-m", "pytest", "-q", "-s", "test_canary.py"]
    before = set(workers.systemctl("list-units", f"{workers.CHECK_UNIT}*", "--all", "--plain", "--no-legend").stdout.split())
    try:
        walled = workers.checked(argv, cwd=tree, tree=tree, private=private, state=state, timeout=120, reads=[said])
        wrote = [p.name for p in (tree, private, state) if (p / "canary-wrote").exists()]
        monkeypatch.delenv(memory.INDEX_SEEN)                           # with no word from the deploy: the index reads as gone
        blind = workers.checked(argv, cwd=tree, tree=tree, private=private, state=state, timeout=120)
        plain = subprocess.run(argv, cwd=tree, capture_output=True, text=True, timeout=120,
                               env={**os.environ, "YERK_PRIVATE": str(private), "YERK_STATE": str(state), "PYTHONPATH": str(tree)})
    finally:
        server.shutdown()
        for u in set(workers.systemctl("list-units", f"{workers.CHECK_UNIT}*", "--all", "--plain", "--no-legend").stdout.split()) - before:
            if u.startswith(workers.CHECK_UNIT):
                workers.systemctl("stop", u)
    inside, outside, unseen = (json.loads(next(x for x in r.stdout.splitlines() if x.startswith("CANARY "))[7:]) for r in (walled, plain, blind))
    home = sorted(os.listdir(Path.home()))
    assert walled.returncode == 0 and outside["home"] == home and outside["planted"]        # plainly: the whole home, the file
    assert len(inside["home"]) < len(home), f"{len(inside['home'])} of {len(home)} home entries seen behind the wall"
    assert not inside["planted"] and inside["writes"] == [False, False, False] and not inside["secret"]
    assert not inside["state_secret"] and outside["state_secret"]      # the install's own secret: gone behind the wall
    assert (inside["given"], unseen["given"], outside["given"]) == ([True, False], [False, False], [True, True])   # the file given, alone
    assert (inside["meaning"], unseen["meaning"], outside["meaning"]) == ("ok", "no-index", "ok")   # its pid: by the deploy's word
    assert inside["denylist"] and inside["tree_read"] and inside["imports"] and inside["server"]
    assert set(workers.check_env(tree, private, state)) <= set(inside["env"]) and wrote == []
    assert "YERK_PLANTED_KEY" not in inside["env"] and "YERK_PLANTED_KEY" in outside["env"] and outside["writes"] == [True] * 3


def test_what_a_check_may_read_is_read_from_its_own_unit_with_links_followed(tmp_path, monkeypatch):
    """workers.unseen: a check's folder and test paths against what check_props gives that check, never a list of its own."""
    home, tree, private, state = made_up(tmp_path, monkeypatch)
    (out := home / "elsewhere").mkdir()
    (tree / "in").symlink_to(state)                             # a link that stays in what is bound back
    (tree / "out").symlink_to(out)                              # one that leads out of it
    (state / "addons").mkdir()
    (state / "addons" / "gone").symlink_to(out)                 # a folder under the state folder that is a link out

    def seen(*paths, cwd=tree):
        return workers.unseen(cwd, list(paths), tree, private, state)
    assert seen("tests/test_x.py", "tests/test_x.py::test_y", "-k", "in/x.py", str(state / "x"), "/usr/lib/x.py") is None
    assert seen("out/test_x.py") == tree / "out" / "test_x.py" and seen(str(out / "t.py")) == out / "t.py"
    assert seen(str(tmp_path / "t.py")) == tmp_path / "t.py"                       # the deploy's /tmp: the unit has its own
    assert seen(str(private / "secrets" / "t.py")) and seen(str(state / "secrets" / "t.py"))   # as the unit has them: not there
    assert seen("tests", cwd=state / "addons" / "gone") == state / "addons" / "gone" and seen(cwd=state / "addons") is None
    props = workers.check_props

    def one_more(*a, **k):                                      # a path bound back tomorrow
        return [*props(*a, **k), f"BindReadOnlyPaths={out}"]
    monkeypatch.setattr(workers, "check_props", one_more)
    assert seen(str(out / "t.py")) is None and seen("out/test_x.py") is None and seen(str(private / "secrets" / "t.py"))


def kept(monkeypatch):
    """workers.checked as deploy.step calls it: each (argv, cwd) kept, each check passing."""
    seen = []

    def checked(argv, cwd, **_k):
        seen.append((argv, cwd))
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")
    monkeypatch.setattr(workers, "checked", checked)
    return seen


def held(*_a):
    return {"hit": 1, "of": 1, "meaning": [], "missed": []}


def hello(state, where):
    """The example add-on installed under `state` from `where`, its folder there a link to `where` when they differ."""
    shutil.copytree(REPO / "examples" / "addons" / "hello", where, ignore=shutil.ignore_patterns("__pycache__"))
    (state / "addons").mkdir(exist_ok=True)
    if where != state / "addons" / "hello":
        (state / "addons" / "hello").symlink_to(where)


@pytest.mark.parametrize("where", ["outside", "inside"])
def test_an_add_on_s_folder_that_is_a_link_out_is_refused_by_name_and_one_that_stays_in_runs(tmp_path, monkeypatch, where):
    home, tree, private, state = made_up(tmp_path, monkeypatch)
    hello(state, home / "elsewhere" / "hello" if where == "outside" else state / "kept" / "hello")
    seen = kept(monkeypatch)
    outcome, lines = deploy.step(tree, private, state, {"tests": ["tests/test_x.py"]}, check=held)
    if where == "outside":
        assert (outcome, lines) == ("path hidden", [f"{words.PLAIN} add-on hello's folder", f"{words.PLAIN} lies outside what a check may read"])
        assert [c for _, c in seen] == [tree]                    # the core's ran; the add-on's never started
    else:
        assert outcome == "" and [c for _, c in seen] == [tree, state / "addons" / "hello"]


@pytest.mark.parametrize("path", ["inside", "outside", "../tree-not/test_x.py", "secrets", "state secrets"])
def test_a_deploy_tests_path_the_wall_would_hide_is_refused_as_one_and_one_inside_runs(tmp_path, monkeypatch, path):
    home, tree, private, state = made_up(tmp_path, monkeypatch)
    given = {"inside": "tests/test_x.py", "outside": str(home / "elsewhere" / "test_x.py"),
             "secrets": str(private / "secrets" / "test_x.py"), "state secrets": str(state / "secrets" / "test_x.py")}.get(path, path)
    seen = kept(monkeypatch)
    outcome, lines = deploy.step(tree, private, state, {"tests": [given]}, check=held)
    if path == "inside":
        assert outcome == "" and seen[0][0][-1] == given
    else:
        assert (outcome, lines, seen) == ("path hidden", [f"{words.PLAIN} [deploy] tests path test_x.py", f"{words.PLAIN} {words.HIDDEN}"], [])
        assert all("/" not in x for x in lines)                  # the path's last part at most, never the whole path


def test_an_add_on_s_test_path_led_out_by_a_link_made_after_the_add_is_refused_and_a_bound_one_runs(tmp_path, monkeypatch):
    home, tree, private, state = made_up(tmp_path, monkeypatch)
    hello(state, state / "addons" / "hello")
    shutil.move(state / "addons" / "hello" / "tests", out := home / "elsewhere" / "tests")
    (state / "addons" / "hello" / "tests").symlink_to(out)
    seen = kept(monkeypatch)
    said = deploy.step(tree, private, state, {"tests": ["tests/test_x.py"]}, check=held)
    assert said == ("path hidden", [f"{words.PLAIN} add-on hello's test path tests", f"{words.PLAIN} lies outside what a check may read"])
    assert len(seen) == 1 and all(str(tmp_path) not in x for x in said[1])
    props = workers.check_props
    monkeypatch.setattr(workers, "check_props", lambda *a, **k: [*props(*a, **k), f"BindReadOnlyPaths={out}"])
    assert deploy.step(tree, private, state, {"tests": ["tests/test_x.py"]}, check=held)[0] == "" and len(seen) == 3
    assert deploy.step(tree, private, state, {"tests": ["tests/test_x.py"]}, go=lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""),
                       check=held)[0] == ""                       # a stand-in for the wall is no wall: nothing of it to hide
