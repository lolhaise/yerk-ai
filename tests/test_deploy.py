"""Checks `yerk deploy`, which moves the running yerkd onto new code, restarts it at a quiet moment and rolls back if
the move onto units/yerkd.service, READY=1 only once the port listens, the stall row's hold, dead
card), the move onto units/yerkd.service, READY=1 only once the port listens, the stall row's hold, dead turns found
from the message, the card that cuts the name and never the fact, job alerts to the machine room, and the upgrade drill.
systemd, the channel and the daemon's /status are fakes; git is a real repository in tmp_path. Layer 7, run 2."""
import asyncio
import functools
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from types import SimpleNamespace

from yerk_ai import beat, cli, deploy, gates, jobs, morning, probes, status, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.room import Inbound

NOW = 1792303200.0
NAP = 0.01


def nap(_s):
    time.sleep(NAP)


def repo(tmp_path):
    """Three commits in a row; a bare origin whose main is the second; the tree on main at the first, one
    behind origin/main, as the merge lane leaves the daemon's checkout."""
    t = tmp_path / "tree"
    t.mkdir()

    def g(*a):
        return subprocess.run(["git", "-C", str(t), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    ids = []
    for i in range(3):
        (t / "f.txt").write_text(f"{i}\n")
        g("add", "f.txt")
        g("-c", "user.email=nobody", "-c", "user.name=t", "commit", "-qm", f"c{i}")
        ids.append(g("rev-parse", "HEAD"))
    g("init", "-q", "--bare", str(tmp_path / "origin.git"))
    g("remote", "add", "origin", str(tmp_path / "origin.git"))
    g("push", "-q", "origin", f"{ids[1]}:refs/heads/main")
    g("fetch", "-q", "origin")
    g("branch", "-q", "--set-upstream-to=origin/main", "main")
    g("reset", "-q", "--hard", ids[0])
    return t, ids


def git(tree, *a):
    return subprocess.run(["git", "-C", str(tree), "-c", "core.hooksPath=/dev/null", *a], capture_output=True, text=True)


def where(tree):
    """(the branch checked out, "" when detached; the commit)"""
    return git(tree, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip(), git(tree, "rev-parse", "HEAD").stdout.strip()


def layer(tmp_path, tree, **deploy_conf):
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    state.mkdir()
    rt = priv / "rooms.toml"
    rt.write_text(rt.read_text().replace("[rooms.example-quiet]\n", "[rooms.example-quiet]\nrole = \"machine\"\n"))
    conf = {"tree": str(tree), "unit": "yerkd-fake", "proof_seconds": 0.3, "cap_minutes": 0.005, **deploy_conf}
    (priv / "yerkd.toml").write_text("[deploy]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in conf.items()))
    (state / "yerkd.json").write_text(json.dumps({"pid": os.getpid(), "port": 1}))
    return priv, state


class Fake:
    """systemd and the daemon's /status: a restart loads the tree's commit, as a real one does."""
    def __init__(self, tree, connected=lambda commit: True):
        self.tree, self.connected, self.unit = tree, connected, "yerkd-fake"
        self.commit, self.restarts, self.busy = deploy.head(tree), [], False

    def restart(self):
        self.commit = deploy.head(self.tree)
        self.restarts.append(self.commit)
        return True

    def active(self):
        return True

    def show(self, prop):
        return ""

    def ask(self, state):
        return {"rooms": [{"room": "example-room", "busy": self.busy, "waiting": 0, "since": None}],
                "commit": self.commit, "connected": self.connected(self.commit)}


def rows(state):
    return [(r["kind"], r["before"], r["after"], r["outcome"], r["cut"])
            for r in sorted(Ledger(state / "ledger.sqlite").rows(10, "deploys"), key=lambda r: r["id"])]


def test_a_deploy_that_fast_forwards_waits_for_quiet_restarts_proves_and_ledgers_it(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f, posts = Fake(tree), []
    code = deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask,
                      post=lambda p: posts.append(p["machine"]) or {"ok": True}, sleep=nap)
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and deploy.head(tree) == ids[1] and f.restarts == [ids[1]]
    assert out == [f"{words.DONE} deployed {ids[1][:7]}", f"{words.PLAIN} fast tests skipped",
                   f"{words.DONE} yerkd up", f"{words.DONE} channel connected", f"{words.DONE} status answers",
                   f"{words.DONE} running {ids[1][:7]}", f"{words.DONE} rooms served: 1 of 1", f"{words.PLAIN} no turn cut"]
    assert rows(state) == [("deploy", ids[0], ids[1], "proven", 0)]
    assert posts == [{"room": "example-quiet", "text": "\n".join(out), "key": posts[0]["key"]}]   # the machine room
    assert where(tree) == ("main", ids[1])                                 # the branch moved, nothing detached


def test_a_planted_failing_proof_rolls_back_the_same_way_and_the_card_says_both(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree, connected=lambda commit: commit != ids[2])          # the planted commit never connects
    code = deploy.run(priv, state, ids[2], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap)
    out = capsys.readouterr().out.splitlines()
    assert code == 1 and deploy.head(tree) == ids[0] and f.restarts == [ids[2], ids[0]]
    assert out[0] == f"{words.NOT_DONE} {ids[2][:7]} failed its proof" and f"{words.NOT_DONE} channel connected" in out
    back = out.index(f"{words.DONE} rolled back to {ids[0][:7]}")
    assert f"{words.DONE} channel connected" in out[back:] and f"{words.DONE} running {ids[0][:7]}" in out[back:]
    assert rows(state) == [("deploy", ids[0], ids[2], "proof failed", 0), ("rollback", ids[2], ids[0], "proven", 0)]
    assert all(words.width(x) <= words.PHONE for x in out)


def test_no_restart_while_a_turn_runs_or_a_person_wrote_lately_and_it_gives_up_at_the_cap(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree)
    f.busy = True
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [f"{words.NOT_DONE} not deployed", f"{words.PLAIN} never quiet in {words.span(0.3)}",
                       f"{words.PLAIN} example-room has a turn"]
    assert f.restarts == [] and deploy.head(tree) == ids[0]                  # the tree back where it was
    f.busy = False
    led = Ledger(state / "ledger.sqlite")
    led.inbound_add("m1", "example-room", "discord", "owner", 2)            # a person, just now
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    assert any(x.startswith(f"{words.PLAIN} example-room had a message ") for x in capsys.readouterr().out.splitlines())
    assert f.restarts == [] and [r[3] for r in rows(state)] == ["gave up", "gave up"]


def test_it_refuses_what_does_not_fast_forward_and_a_tree_with_changes(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree)
    f.commit = ids[2]                                                    # the daemon runs the newest
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed",
                                                    f"{words.PLAIN} {ids[1][:7]} is not ahead of {ids[2][:7]}"]
    f.commit = ids[0]
    (tree / "f.txt").write_text("changed\n")
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    assert capsys.readouterr().out.splitlines()[1] == f"{words.PLAIN} the daemon's tree has changes"
    assert f.restarts == [] and [r[3] for r in rows(state)] == ["refused", "refused"]


def test_a_person_s_turn_left_open_by_the_restart_is_counted_as_cut(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree, quiet_seconds=0)
    f, led = Fake(tree), Ledger(state / "ledger.sqlite")
    real = f.restart

    def restart():                     # a message lands and is taken between the quiet check and the stop
        led.inbound_add("m2", "example-room", "discord", "owner", 2)
        led.inbound_set("m2", state="running", received=time.time() - 1)
        return real()
    f.restart = restart
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 0
    assert f"{words.NOT_DONE} turns cut: 1" in capsys.readouterr().out.splitlines()
    assert rows(state) == [("deploy", ids[0], ids[1], "proven", 1)]


def test_the_fast_tests_run_in_the_tree_and_a_failure_deploys_nothing(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    assert open('f.txt').read() != '1\\n'\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    f = Fake(tree)
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    assert capsys.readouterr().out.splitlines()[1] == f"{words.PLAIN} the fast tests failed"
    assert f.restarts == [] and deploy.head(tree) == ids[0]
    assert deploy.run(priv, state, ids[2], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 0
    assert f"{words.DONE} fast tests passed" in capsys.readouterr().out.splitlines()


def test_the_move_onto_the_unit_file_keeps_the_command_folder_and_environment_and_can_go_back(tmp_path, monkeypatch):
    calls = []
    shown = {"WorkingDirectory": "/code", "Type": "notify", "WatchdogUSec": "30s"}
    bus = {"ExecStart": 'a(sasbttttuii) 1 "/v/bin/python" 5 "/v/bin/python" "-m" "yerk_ai.daemon" "--state" "/s" '
                        'false 0 0 0 0 0 0 0\n',                      # as busctl prints them
           "Environment": r'as 5 "YERK_PRIVATE=/p" "PATH=/v/bin:/mnt/c/Program Files/x" "PCT=50%off" "DOLLAR=a$b" '
                          r'"BS=a\\b"' + "\n"}

    class Unit(deploy.Systemd):
        def busctl(self, prop):
            return bus[prop]

        def ctl(self, *args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, shown.get(args[2], "") if args[0] == "show" else "", "")
    u = Unit("yerkd-fake")
    was = u.move(deploy.REPO, tmp_path / "units")
    path = tmp_path / "units" / "yerkd-fake.service"
    text = path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and "Type=notify" in text and "WatchdogSec=30" in text
    assert "MemoryMax=" in text and "WorkingDirectory=/code\n" in text
    assert [x for x in text.splitlines() if x.startswith("Environment=")] == [   # % a specifier, \\ and " escapes
        'Environment="YERK_PRIVATE=/p"', 'Environment="PATH=/v/bin:/mnt/c/Program Files/x"', 'Environment="PCT=50%%off"',
        'Environment="DOLLAR=a$b"', 'Environment="BS=a\\\\b"']
    assert "ExecStart=/v/bin/python -m yerk_ai.daemon --state /s\n" in text and "YERK_STATE=%h" not in text
    assert [c[0] for c in calls if c[0] != "show"] == ["stop", "daemon-reload", "start"]
    ran = []
    monkeypatch.setattr(deploy.subprocess, "run", lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    assert u.back(was) and not path.exists() and (tmp_path / "units" / "yerkd-fake.service.failed").exists()
    assert [c[0] for c in calls if c[0] != "show"][3:] == ["stop", "daemon-reload", "reset-failed"]   # then systemd-run
    assert ran[0][:3] == ["systemd-run", "--user", "--unit=yerkd-fake"] and ran[0][-5:] == \
        ["/v/bin/python", "-m", "yerk_ai.daemon", "--state", "/s"]
    assert {"--setenv=PATH=/v/bin:/mnt/c/Program Files/x", "--setenv=PCT=50%off", "--setenv=DOLLAR=a$b",
            "--setenv=BS=a\\b"} <= set(ran[0])
    assert ran[0][ran[0].index("Type=notify") - 1] == "-p" and ran[0][ran[0].index("WatchdogSec=30s") - 1] == "-p"


def test_a_deploy_moves_the_branch_forward_a_rollback_moves_it_back_and_the_next_pull_still_works(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f, ok = Fake(tree, connected=lambda commit: commit != ids[2]), (lambda p: {"ok": True})   # the next merge fails
    assert deploy.run(priv, state, "origin/main", tests=False, sd=f, ask=f.ask, post=ok, sleep=nap) == 0
    assert where(tree) == ("main", ids[1]) and "Your branch is up to date with 'origin/main'" in git(tree, "status").stdout
    assert git(tree, "pull", "-q", "--ff-only").returncode == 0
    git(tree, "push", "-q", "origin", f"{ids[2]}:refs/heads/main")             # the merge lane's next merge, pulled
    assert git(tree, "pull", "-q", "--ff-only").returncode == 0 and where(tree) == ("main", ids[2])
    capsys.readouterr()
    assert deploy.run(priv, state, tests=False, sd=f, ask=f.ask, post=ok, sleep=nap) == 1
    out = capsys.readouterr().out.splitlines()
    assert where(tree) == ("main", ids[1]) and f.commit == ids[1]
    assert out[-3:] == [f"- main moved back to {ids[1][:7]}", "- origin/main is 1 ahead",
                        f"- a pull brings {ids[2][:7]} back to the tree"] and all(words.width(x) <= words.PHONE for x in out)
    assert "Your branch is behind 'origin/main' by 1 commit" in git(tree, "status").stdout
    assert git(tree, "pull", "-q", "--ff-only").returncode == 0 and where(tree) == ("main", ids[2])
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=ok, sleep=nap) == 1   # behind main
    assert capsys.readouterr().out.splitlines()[1] == f"- {ids[1][:7]} is not ahead of main"
    assert where(tree) == ("main", ids[2]) and len(f.restarts) == 3                   # refused: as it was found
    git(tree, "checkout", "-q", "--detach", ids[1])                                   # found detached: kept detached
    f.connected = lambda commit: True
    assert deploy.run(priv, state, ids[2], tests=False, sd=f, ask=f.ask, post=ok, sleep=nap) == 0
    assert where(tree) == ("", ids[2]) and capsys.readouterr().out.splitlines()[-1] == f"- detached at {ids[2][:7]}"


def test_every_way_out_that_deploys_nothing_leaves_the_branch_where_it_was(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    assert open('f.txt').read() != '1\\n'\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    f = Fake(tree)
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1   # tests fail
    assert where(tree) == ("main", ids[0])
    f.busy = True
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    assert where(tree) == ("main", ids[0]) and f.restarts == []                       # gave up at the cap
    assert "Your branch is behind 'origin/main' by 1 commit" in git(tree, "status").stdout


def test_after_the_move_the_proof_checks_the_unit_runs_from_its_file(tmp_path):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree)
    props = {"FragmentPath": "/run/user/1000/systemd/transient/yerkd-fake.service", "Type": "simple"}
    f.show = lambda prop: props.get(prop, "")                       # a transient unit still loaded
    ok, lines = deploy.prove(state, f, f.ask, {"example-room"}, ids[0], 0, nap, "/u/yerkd-fake.service")
    assert not ok and lines[-1] == f"{words.NOT_DONE} unit runs from its file"
    props.update(FragmentPath="/u/yerkd-fake.service", Type="notify")
    ok, lines = deploy.prove(state, f, f.ask, {"example-room"}, ids[0], 0, nap, "/u/yerkd-fake.service")
    assert ok and lines[-1] == f"{words.DONE} unit runs from its file"


def test_a_rollback_that_never_gets_quiet_says_why_writes_gave_up_and_leaves_the_tree_on_what_runs(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree, connected=lambda commit: commit != ids[1])          # the planted commit never connects
    real = f.restart

    def restart():                     # a person writes while it runs: the rollback never gets a quiet moment
        Ledger(state / "ledger.sqlite").inbound_add("m3", "example-room", "discord", "owner", 2)
        return real()
    f.restart = restart
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 2
    out = capsys.readouterr().out.splitlines()                       # 2: what runs is a commit no proof passed
    assert f.restarts == [ids[1]] and where(tree) == ("main", ids[1])               # the tree on what runs
    never = out.index(f"{words.NOT_DONE} rollback to {ids[0][:7]} never ran")
    assert out[never + 1] == f"{words.PLAIN} never quiet in {words.span(0.3)}"
    assert out[never + 2].startswith(f"{words.PLAIN} example-room had a message ") and len(out) == never + 3
    assert not [x for x in out if "rollback" in x and "failed its proof" in x]
    # m3 landed during the restart's own call and is still open after it: a turn the restart left open (fix 6)
    assert rows(state) == [("deploy", ids[0], ids[1], "proof failed", 1), ("rollback", ids[1], ids[0], "gave up", 0)]


def test_no_restart_while_the_memory_index_rebuilds_and_status_says_so(tmp_path, capsys):
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    (proc / "4242" / "cmdline").write_bytes(b"/v/bin/python\x00-m\x00yerk_ai.memindex\x00--rebuild\x00")
    (tmp_path / "ystate").mkdir()
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path / "ystate")
    y.rebuild_running = functools.partial(gates.rebuild_running, proc)   # a planted rebuild, as the restart gate reads
    assert asyncio.run(y.status_now({}))["rebuilding"] is True
    (proc / "4242" / "cmdline").write_bytes(b"/v/bin/python\x00-m\x00yerk_ai.memindex\x00")
    assert asyncio.run(y.status_now({}))["rebuilding"] is False
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree)
    plain = f.ask
    f.ask = lambda st: {**plain(st), "rebuilding": True}
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [f"{words.NOT_DONE} not deployed", f"{words.PLAIN} never quiet in {words.span(0.3)}",
                       f"{words.PLAIN} {words.REBUILDING}"]
    assert f.restarts == [] and where(tree) == ("main", ids[0]) and [r[3] for r in rows(state)] == ["gave up"]


def planted_proc(tmp_path, cgroup, cmdline=b"/v/bin/python\x00-m\x00yerk_ai.daemon\x00--console\x00r\x00"):
    """A /proc with this process's pid (the one yerkd.json names in `layer`) in `cgroup`."""
    p = tmp_path / "proc" / str(os.getpid())
    p.mkdir(parents=True, exist_ok=True)
    (p / "cgroup").write_text(cgroup)
    (p / "cmdline").write_bytes(cmdline)
    return tmp_path / "proc"


def test_with_no_deploy_unit_the_unit_comes_from_the_pid_and_the_card_names_it(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree, unit="")                       # no [deploy] unit, as tonight
    f, made = Fake(tree), []

    def make(name):
        made.append(name)
        f.unit = name
        return f
    manager = "/user.slice/user-1000.slice/user" + chr(64) + "1000.service"   # spelled out: test_scrub reads x@y.z as an email
    leaf = f"0::{manager}/app.slice/yerkd-found.service\n"
    proc = planted_proc(tmp_path, f"4:memory:{manager}\n" + leaf)     # a hybrid v1 line above the unit: skipped
    code = deploy.run(priv, state, ids[1], tests=False, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, proc=proc,
                      systemd=make)
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and made == ["yerkd-found"] and out[1] == f"{words.PLAIN} unit yerkd-found, from its pid"
    assert f.restarts == [ids[1]] and Ledger(state / "ledger.sqlite").rows(1, "deploys")[0]["unit"] == "yerkd-found"
    for cgroup, cmdline in (("0::/user.slice/user-1000.slice/session-2.scope\n", None),          # a terminal's
                            (leaf, b"/usr/bin/sleep\x00infinity\x00")):                          # a reused pid
        proc = planted_proc(tmp_path, cgroup, *([cmdline] if cmdline else []))
        assert deploy.run(priv, state, ids[2], tests=False, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap,
                          proc=proc, systemd=make) == 1
        assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed",
                                                        f"{words.PLAIN} no unit found: set [deploy] unit"]
    assert made == ["yerkd-found"] and f.restarts == [ids[1]] and where(tree) == ("main", ids[1])   # nothing touched
    assert [(r[3], r[0]) for r in rows(state)][1:] == [("refused", "deploy")] * 2


class Unit(deploy.Systemd):
    """systemd for `--unit`: the bus as busctl prints it, a stop that stops, a start that loads the
    tree's commit from the file, back() that puts the transient unit back; `fault` is the step that
    fails once ("argv": the bus has no such unit, as tonight; else a systemctl verb that times out)."""
    def __init__(self, tree, home, fault=""):
        super().__init__("yerkd-fake")
        self.tree, self.home, self.fault, self.up, self.file, self.log = tree, home, fault, True, "", []
        self.commit = deploy.head(tree)

    def busctl(self, prop):
        return "" if self.fault == "argv" else {
            "ExecStart": 'a(sasbttttuii) 1 "/v/bin/python" 3 "/v/bin/python" "-m" "yerk_ai.daemon" false 0 0 0 0 0 0 0\n',
            "Environment": 'as 1 "YERK_STATE=/s"\n'}[prop]

    def ctl(self, *args):
        self.log.append(args[0])
        if self.fault == args[0]:
            self.fault = ""
            raise subprocess.TimeoutExpired(["systemctl", *args], 180)
        if args[0] == "stop":
            self.up = False
        if args[0] in ("start", "restart"):
            self.up, self.commit = True, deploy.head(self.tree)
            self.file = str(self.home / "yerkd-fake.service") if args[0] == "start" else self.file
        shown = {"FragmentPath": self.file, "Type": "notify" if self.file else "simple", "WorkingDirectory": "/code"}
        out = shown.get(args[2], "") if args[0] == "show" else ("active" if self.up else "inactive")
        return subprocess.CompletedProcess(args, 0, out, "")

    def back(self, was):
        self.log.append("back")
        os.rename(was["file"], was["file"] + ".failed")
        self.up, self.commit, self.file = True, deploy.head(self.tree), ""
        return True

    def ask(self, state):
        return {"rooms": [{"room": "example-room", "busy": False, "waiting": 0, "since": None}], "commit": self.commit,
                "connected": True} if self.up else None


def test_a_fault_before_the_stop_refuses_and_one_after_it_or_in_the_proof_rolls_back(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    (tree / "units").mkdir()                                           # untracked: the tree stays clean
    shutil.copy(deploy.REPO / "units" / "yerkd.service", tree / "units")
    home, ok = tmp_path / "units", (lambda p: {"ok": True})
    u = Unit(tree, home, "argv")                                       # tonight's IndexError, before any stop
    assert deploy.run(priv, state, ids[1], tests=False, unit_file=True, sd=u, ask=u.ask, post=ok, sleep=nap, home=home) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed", f"{words.PLAIN} IndexError in argv",
                                                    f"{words.PLAIN} nothing was stopped", f"{words.PLAIN} fast tests skipped"]
    assert "stop" not in u.log and (u.up, u.commit) == (True, ids[0]) and where(tree) == ("main", ids[0])
    assert rows(state)[-1] == ("deploy", ids[0], ids[1], "fault", 0) and not list(home.glob("*.service"))
    u = Unit(tree, home, "daemon-reload")                              # the move, after its stop
    assert deploy.run(priv, state, ids[1], tests=False, unit_file=True, sd=u, ask=u.ask, post=ok, sleep=nap, home=home) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [f"{words.NOT_DONE} {ids[1][:7]} broke off, not proven", f"{words.PLAIN} fast tests skipped",
                       f"{words.NOT_DONE} TimeoutExpired in ctl"] and f"{words.DONE} rolled back to {ids[0][:7]}" in out
    assert u.log.count("back") == 1 and (u.up, u.commit, u.file) == (True, ids[0], "") and where(tree) == ("main", ids[0])
    assert rows(state)[-2:] == [("deploy", ids[0], ids[1], "fault", 0), ("rollback", ids[1], ids[0], "proven", 0)]
    u = Unit(tree, home, "is-active")                                  # the proof, after the move
    assert deploy.run(priv, state, ids[1], tests=False, unit_file=True, sd=u, ask=u.ask, post=ok, sleep=nap, home=home) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[2] == f"{words.NOT_DONE} TimeoutExpired in ctl" and f"{words.DONE} running {ids[0][:7]}" in out
    assert u.log.count("back") == 1 and (u.up, u.commit) == (True, ids[0]) and where(tree) == ("main", ids[0])
    assert rows(state)[-2:] == [("deploy", ids[0], ids[1], "fault", 0), ("rollback", ids[1], ids[0], "proven", 0)]
    f = Fake(tree)                                                     # a plain restart that raises: maybe stopped
    real, calls = f.restart, []

    def restart():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the bus did not answer")
        return real()
    f.restart = restart
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=ok, sleep=nap) == 1
    assert f"{words.NOT_DONE} OSError in restart" in capsys.readouterr().out.splitlines()
    assert f.commit == ids[0] and rows(state)[-2:] == [("deploy", ids[0], ids[1], "fault", 0),
                                                       ("rollback", ids[1], ids[0], "proven", 0)]


def test_a_row_left_open_long_before_the_deploy_is_not_a_turn_it_cut(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    led, f = Ledger(state / "ledger.sqlite"), Fake(tree)
    led.inbound_add("left", "example-quiet", "discord", "owner", 2)      # a crash's leftover, 30 min old
    led.inbound_set("left", state="running", received=time.time() - 1800)
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 0
    assert f"{words.PLAIN} no turn cut" in capsys.readouterr().out.splitlines()
    assert rows(state) == [("deploy", ids[0], ids[1], "proven", 0)]


def test_a_failed_rollback_exits_2_and_the_morning_diff_names_every_deploy_not_proven(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree, connected=lambda commit: False)                     # neither commit ever connects
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": False}, sleep=nap) == 2
    assert f"{words.NOT_DONE} rollback to {ids[0][:7]} failed its proof" in capsys.readouterr().out.splitlines()
    led, accounts, cool = Ledger(state / "ledger.sqlite"), load_accounts(priv / "accounts.toml"), Cooldowns(state / "c.json")
    led.deploy(kind="deploy", unit="u", before=ids[0], after=ids[2], outcome="proven", cut=0)
    live, now = {"units": [], "started": time.time() - 86400}, time.time() + 60
    lines, stamp = morning.build(priv, state, accounts, cool, led, live, now)
    at = [words.clock(float(d["at"]), now) for d in led.deploys(0)]
    want = [f"{words.NOT_DONE} deploy {ids[1][:7]}: proof failed, {at[0]}",
            f"{words.NOT_DONE} rollback {ids[0][:7]}: proof failed, {at[1]}"]
    assert [x for x in lines if " deploy " in x or " rollback " in x] == want and lines[1:3] == want   # new: on top
    assert lines[0] == f"{words.NOT_DONE} morning: 2 new since the last diff" and all(words.width(x) <= words.PHONE for x in want)
    (state / "morning").mkdir()
    (state / "morning" / "last.json").write_text(json.dumps(stamp))
    again, _ = morning.build(priv, state, accounts, cool, led, live, now + 60)
    assert again[0] == f"{words.PLAIN} morning: nothing new, 2 known" and sum(x in want for x in again) == 2   # said once
    later, _ = morning.build(priv, state, accounts, cool, led, live, now + 86400)
    assert not [x for x in later if " rollback " in x]                                    # the last 24 hours only


def test_with_no_status_before_the_deploy_the_proof_wants_every_room_in_rooms_toml(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    rt = priv / "rooms.toml"
    rt.write_text(rt.read_text().replace("discord:CHANNEL_ID", "discord:1001", 1).replace("discord:CHANNEL_ID", "discord:1002"))
    (state / "yerkd.json").write_text(json.dumps({"pid": 2 ** 30, "port": 1}))     # stopped: nothing answers
    f = Fake(tree)
    real = f.restart

    def restart():                                                    # a start writes its pid, as yerkd does
        (state / "yerkd.json").write_text(json.dumps({"pid": os.getpid(), "port": 1}))
        return real()
    f.restart = restart
    serves = lambda commit: ["example-room"] + ([] if commit == ids[1] else ["example-quiet"])   # noqa: E731
    f.ask = lambda st: {"rooms": [{"room": r, "busy": False, "waiting": 0, "since": None} for r in serves(f.commit)],
                        "commit": f.commit, "connected": True}
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    out = capsys.readouterr().out.splitlines()                        # ids[1] serves one room fewer
    assert out[0] == f"{words.NOT_DONE} {ids[1][:7]} failed its proof" and f"{words.NOT_DONE} rooms served: 1 of 2" in out
    assert f"{words.DONE} rooms served: 2 of 2" in out and f.commit == ids[0]


def test_the_unit_file_slows_the_rooms_before_any_ceiling_kills_one():
    text = (deploy.REPO / "units" / "yerkd.service").read_text()
    unit = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}

    def size(key):
        v = re.search(rf"(?m)^{key}=(\d+)([KMG])$", text)
        return int(v[1]) * unit[v[2]] if v else 0
    assert size("MemoryHigh") >= (100 + 5 * 307) << 20                 # yerkd idle plus five rooms at the most read here
    assert size("MemoryMax") >= 2 * size("MemoryHigh") > 0             # the hard ceiling is for a leak only


def test_ready_goes_out_only_once_the_port_listens(tmp_path):
    sent, flag = [], {"up": False}
    b = beat.Beat(Ledger(tmp_path / "l.sqlite"), tmp_path / "stalls", send=lambda m: sent.append((time.monotonic(), m)) or True)

    async def go():
        t0 = time.monotonic()
        ticker = asyncio.create_task(b.tick(lambda: flag["up"]))
        await asyncio.sleep(0.3)
        flag["up"], flag["at"] = True, time.monotonic()
        await asyncio.sleep(0.2)
        ticker.cancel()
        return t0
    asyncio.run(go())
    assert [m for _, m in sent] == ["READY=1"] and sent[0][0] >= flag["at"]


def test_the_stall_row_says_how_long_the_loop_was_held_and_one_log_line_says_it(tmp_path, caplog):
    b = beat.Beat(Ledger(tmp_path / "l.sqlite"), tmp_path / "stalls")

    async def go():
        ticker = asyncio.create_task(b.tick())
        await asyncio.sleep(0.5)
        time.sleep(0.4)                                   # under the stall limit: a log line, no row
        await asyncio.sleep(0.5)
        time.sleep(3)                                     # the cold read's hold, written 2.5 before
        await asyncio.sleep(0.5)
        ticker.cancel()
    with caplog.at_level(logging.WARNING, logger="yerk.beat"):
        asyncio.run(go())
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stall:")]
    rows_ = b.ledger.stalls(0)
    assert len(rows_) == 1 and 2.85 <= rows_[0]["seconds"] <= 3.2, rows_
    assert len(said) == 2 and " in go" in said[1] and " in " not in said[0]       # a hold under 2 s: no dump
    assert 300 <= int(said[0].split(" for ")[1].split()[0]) <= 400                # within the probe's tenth


def test_the_card_cuts_the_name_and_never_the_fact():
    room, now = "a-very-long-room-name-for-the-card-39ch", 10000.0
    assert len(room) == 39
    s = {"at": now, "running": True, "rooms": [{"room": room, "busy": True, "waiting": 12, "since": now - 900}],
         "units": [{"name": room, "rss_mb": 312.4, "cpu_pct": 1.25}], "workers": [],
         "jobs": [{"name": "a-job-with-a-very-long-name-indeed-yes", "next": now + 1800}],
         "accounts": [{"name": "account-number-0-with-long-name-here", "meter": {"five_hour": {"pct": 91}, "at": now - 7200},
                       "rests_until": None}], "stalls": []}
    lines = status.card(s)
    assert all(words.width(x) <= words.PHONE for x in lines), lines
    assert any(x.startswith(f"{words.NOT_DONE} a-very-") and x.endswith("...: busy 15 min, 12 waiting") for x in lines)
    assert any(x.endswith(": 312 MB, 1.2% cpu") for x in lines) and any(x.endswith(": in 30 min") for x in lines)
    assert any(x.startswith(f"{words.WORKING} account-") and x.endswith(": 5h 91%") for x in lines)
    assert any(x.endswith(f": last reading {words.ago(7200)}") for x in lines) and lines[0] == "- yerkd running: 1 room"


def dead_layer(tmp_path):
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    state.mkdir()
    rt = priv / "rooms.toml"
    rt.write_text(rt.read_text().replace("[rooms.example-quiet]\n", "[rooms.example-quiet]\nrole = \"machine\"\n"))
    return priv, state, load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json")


def message(led, mid, at, room="example-room", sender="owner", state="answered"):
    led.inbound_add(mid, room, "discord", sender, 2)
    led.inbound_set(mid, received=at, state=state)


def test_dead_turns_are_found_from_the_message_and_a_changed_set_alerts_once(tmp_path):
    priv, state, accounts, cool = dead_layer(tmp_path)
    led, t = Ledger(state / "ledger.sqlite"), NOW - 3600
    message(led, "a", t)                                              # answered: a reply part under its turn's key
    tid = led.record(room="example-room", outcome="ok", turn_key="k1", inbound="a")
    led.inbound_set("a", turn_id=tid)
    led.delivery_plan("k1:0", "example-room", "reply", "hi", "a")
    led.delivery_set("k1:0", state="sent", message_id="r1")
    message(led, "b", t + 60)                                         # a visible notice for it, no turn
    led.outbound(room="example-room", channel="c", kind="notice", message_id="n1", chars=5, inbound="b")
    message(led, "c", t + 120, state="failed")                        # a turn that posted nothing
    led.inbound_set("c", turn_id=led.record(room="example-room", outcome="failed", turn_key="k2", inbound="c"))
    message(led, "d", t + 180, room="example-quiet", state="received")   # never taken
    message(led, "e", t + 240, sender="yerk")                        # the harness's own
    message(led, "f", t + 300, state="ignored")                      # deleted by its sender
    message(led, "g", NOW - 60, state="running")                     # still inside its ten minutes
    message(led, "h", NOW - 2 * 86400)                               # older than a day
    led.outbound(room="example-room", channel="c", kind="progress", message_id="p1", chars=5, inbound="c")
    assert [d["message_id"] for d in led.dead_turns(NOW - 86400, NOW - 600)] == ["c", "d"]
    live = {"units": [], "started": NOW - 3 * 86400}
    lines, stamp = morning.build(priv, state, accounts, cool, led, live, NOW)
    want = f"{words.NOT_DONE} dead turns: 2, 2 rooms, last {words.clock(t + 180, NOW)}"
    assert want in lines and lines[0] == f"{words.NOT_DONE} morning: 1 new since the last diff"
    (state / "morning").mkdir()
    (state / "morning" / "last.json").write_text(json.dumps(stamp))
    again, stamp = morning.build(priv, state, accounts, cool, led, live, NOW + 60)
    assert again[0] == f"{words.PLAIN} morning: nothing new, 1 known"   # the same set: said once
    message(led, "i", NOW - 900)
    third, _ = morning.build(priv, state, accounts, cool, led, live, NOW + 120)
    assert third[0] == f"{words.NOT_DONE} morning: 1 new since the last diff"
    blind, _ = morning.build(priv, state, accounts, cool, Ledger(tmp_path / "empty.sqlite"), None, NOW)
    assert f"{words.NOT_DONE} dead turns: unknown, no yerkd answers" in blind   # blind, never clean
    clean, _ = morning.build(priv, state, accounts, cool, Ledger(tmp_path / "empty.sqlite"), live, NOW)
    assert f"{words.PLAIN} dead turns: none in 24h" in clean


def test_a_job_failure_alerts_the_machine_room_before_the_ops_room(tmp_path):
    said = []
    ch = SimpleNamespace(notify=lambda room, line: asyncio.sleep(0, [said.append(room) or "m1"]))
    confs = {"work": {}, "ops": {}, "machine": {"role": "machine"}}
    j = jobs.Jobs(tmp_path, tmp_path, Ledger(tmp_path / "l.sqlite"), {"ops_room": "ops"}, {}, {n: ch for n in confs},
                  confs, {}, SimpleNamespace(hooks=[]))
    asyncio.run(j.alert(None, "job x: failed", 0, "work"))
    del confs["machine"]
    asyncio.run(j.alert(None, "job x: failed", 0, "work"))
    assert said == ["machine", "ops"]


def test_the_upgrade_drill_records_the_version_runs_three_probes_and_upgrades_nothing(tmp_path, monkeypatch, capsys):
    ran = []
    real = subprocess.run
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "2.1.301 (Claude Code)\n", "")
                        if cmd[:2] == ["claude", "--version"] else real(cmd, **k))
    for name, ok in (("hello", True), ("fold", True), ("resume", False)):
        monkeypatch.setattr(probes, name, lambda a, n=name, ok=ok: ran.append((n, a.config_dir, a.to_config_dir)) or {"ok": ok})
    assert cli.main(["drill", "--config-dir", "/acct/four", "--state", str(tmp_path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [f"{words.PLAIN} claude 2.1.301 (Claude Code)", f"{words.DONE} probe hello: passed",
                   f"{words.DONE} probe fold: passed", f"{words.NOT_DONE} probe resume: failed",
                   f"{words.NOT_DONE} verdict: stay, resume failed"]
    assert ran == [(n, "/acct/four", "/acct/four") for n in ("hello", "fold", "resume")]   # one account
    line = json.loads((tmp_path / "drills.jsonl").read_text())
    assert line["version"] == "2.1.301 (Claude Code)" and line["probes"] == {"hello": True, "fold": True, "resume": False}
