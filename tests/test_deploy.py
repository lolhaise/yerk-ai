"""Checks `yerk deploy`, which moves the running yerkd onto new code, restarts it at a quiet moment and rolls back if
the proof after fails. The cases: the deploy itself (the quiet wait, the proof, the rollback, the ledger rows, the
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
import signal
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from yerk_ai import beat, deploy, gates, jobs, memory, morning, status, words, workers
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE, REPO, budgets, private_dir
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.texting import intake

NOW = 1792303200.0
NAP = 0.01


def nap(_s):
    time.sleep(NAP)


@pytest.fixture(autouse=True)
def no_unit(monkeypatch):
    """No test here starts a real unit: a deploy's check that reaches the wall's own run (workers.checked) fails the test,
    so each one that runs the checks gives the deploy its Fake's stand-in (wall=f.wall)."""
    def refused(*_a, **_k):
        raise AssertionError("a deploy test reached the real wall: give it wall=")
    monkeypatch.setattr(workers, "checked", refused)


def noon(monkeypatch):
    """The wall clock pinned: from here on it reads local noon of a fixed day and runs at its real pace, so deploy rows
    written now and a morning diff read a minute later fall on one day at any hour. Unpinned, a run in a day's last
    minute had the diff name its row 'Wed 23:59', too wide for the phone, so the line dropped the time the test wants."""
    off = time.mktime((2026, 10, 21, 12, 0, 0, 0, 0, -1)) - time.time()
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + off)


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
        self.commit, self.restarts, self.busy, self.checks = deploy.head(tree), [], False, []

    def wall(self, argv, **k):
        """The wall's stand-in: each check's command and folder kept, the check run as a plain child (deploy.plain)."""
        self.checks.append((list(argv), k["cwd"]))
        return deploy.plain(argv, **k)

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


def test_the_quiet_window_counts_the_rooms_own_post_as_it_counts_a_persons_message(tmp_path):
    """Seen live: the owner wrote, the room's reply landed 8.6 minutes later and the restart went two seconds after it, the
    window counted from the person's message alone; the owner's next message came six seconds later, into the restart."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "yerkd.json").write_text(json.dumps({"pid": os.getpid(), "port": 1}))
    led = Ledger(state / "ledger.sqlite")
    led.inbound_add("m1", "example-room", "discord", "owner", 2)
    led.inbound_set("m1", received=NOW - 600)                                # the person, 10 minutes ago
    led.outbound(room="example-room", channel="1", kind="reply", message_id="r1", chars=9, sent=NOW - 5)   # its reply, 5 s ago
    led.outbound(room="example-room", channel="1", kind="progress-edit", message_id="c1", chars=9, sent=NOW)   # a card's: not
    led.outbound(room="example-room", channel="1", kind="notice", chars=9, error="503", sent=NOW)            # never posted: not
    assert led.last_said("example-room") == NOW - 5 and led.last_said("example-quiet") is None

    def ask(_state):
        return {"rooms": [{"room": "example-room", "busy": False, "waiting": 0, "since": None}]}
    assert deploy.quiet(state, ask, 240, NOW) == (False, f"{words.PLAIN} example-room had a message 5 s ago")
    assert deploy.quiet(state, ask, 240, NOW + 234) == (False, f"{words.PLAIN} example-room had a message 4 min ago")
    assert deploy.quiet(state, ask, 240, NOW + 235) == (True, "")             # both older than the window: quiet


def copying(f, *ages):
    """The Fake's /status with its room's `copy`, the next of `ages` at each ask and the last one kept: seconds into a copy
    in flight, None once it is taken or dropped, "no key" for a row from a yerkd older than the key."""
    left = list(ages)

    def ask(state):
        age = left.pop(0) if len(left) > 1 else left[0]
        s = Fake.ask(f, state)
        return {**s, "rooms": [r if age == "no key" else {**r, "copy": age} for r in s["rooms"]]}
    return ask


TODAY = [f"{words.PLAIN} fast tests skipped", f"{words.DONE} yerkd up", f"{words.DONE} channel connected",
         f"{words.DONE} status answers"]                 # a deploy's card under its first line, to the running line


def test_a_rooms_copy_holds_the_restart_until_copy_seconds_and_a_copy_ended_past_it_or_unsaid_holds_nothing(tmp_path):
    """Seen live: a deploy restarted the daemon 62 s into a busy room's copy (quiet_seconds 60, the
    copies of the 8 days before 90 to 171 s) and the room ran past 820,000 through three turns until the next copy. A copy
    holds the restart under `copy` seconds, its room and its time the reason; one taken or dropped (null), one past the
    bound, the bound at 0 and a row with no `copy` (a yerkd older than the key) hold nothing, and quiet, a copy still in
    flight is a card line. A yerkd that does not answer holds a rollback no more than before: it cannot say a copy."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "yerkd.json").write_text(json.dumps({"pid": os.getpid(), "port": 1}))
    f, over = Fake(tmp_path), []
    f.commit = ""
    assert deploy.quiet(state, copying(f, 30.0), 60, NOW, copy=300, over=over) == (False, f"{words.PLAIN} example-room compacts a copy, 30 s in")
    assert deploy.quiet(state, copying(f, 299.6), 60, NOW, copy=300, over=over) == (False, f"{words.PLAIN} example-room compacts a copy, 5 min in")
    for ended in (None, "no key"):
        assert deploy.quiet(state, copying(f, ended), 60, NOW, copy=300, over=over) == (True, "") and over == []
    assert deploy.quiet(state, copying(f, 300.0), 60, NOW, copy=300, over=over) == (True, "")
    assert over == [f"{words.NOT_DONE} example-room's copy dropped, 5 min in"]
    over.clear()
    assert deploy.quiet(state, copying(f, 30.0), 60, NOW, copy=0, over=over) == (True, "")    # 0: a copy never holds it
    assert over == [f"{words.NOT_DONE} example-room's copy dropped, 30 s in"]
    over.clear()
    f.busy = True                                         # the turn check first, as before
    assert deploy.quiet(state, copying(f, 30.0), 60, NOW, copy=300, over=over) == (False, f"{words.PLAIN} example-room has a turn")
    dead = (True, f"{words.PLAIN} {words.NOT_ANSWERING_WHY}")
    assert deploy.quiet(state, lambda _s: None, 60, NOW, blind_ok=True, copy=300, over=over) == dead and over == []


def test_a_copy_in_any_room_holds_the_restart_and_each_copy_the_restart_goes_over_gets_its_own_line(tmp_path):
    """Found by the judge's breaks: every copy test had one room, so a check that read the first row alone passed them all.
    Here the copy is the second room's: it holds the restart under its own name, and past the bound each room whose copy
    the restart goes over is a card line of its own."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "yerkd.json").write_text(json.dumps({"pid": os.getpid(), "port": 1}))
    f, over = Fake(tmp_path), []
    f.commit = ""

    def two(first, second):
        def ask(state):
            s = Fake.ask(f, state)
            return {**s, "rooms": [{**s["rooms"][0], "copy": first}, {**s["rooms"][0], "room": "quiet-room", "copy": second}]}
        return ask
    assert deploy.quiet(state, two(None, 30.0), 60, NOW, copy=300, over=over) == (False, f"{words.PLAIN} quiet-room compacts a copy, 30 s in")
    assert over == []
    assert deploy.quiet(state, two(400.0, 420.0), 60, NOW, copy=300, over=over) == (True, "")
    assert over == [f"{words.NOT_DONE} example-room's copy dropped, {words.span(400.0)} in",
                    f"{words.NOT_DONE} quiet-room's copy dropped, {words.span(420.0)} in"]


def test_a_deploy_waits_for_a_copy_then_restarts_as_today_and_one_past_copy_seconds_or_at_0_is_named_on_the_card(tmp_path, capsys):
    """The default copy_seconds, 300: a copy 30 s in holds the restart to the cap, the card naming the room and its time;
    taken (or dropped) the deploy goes and its card is today's to the line. A copy past the bound, or any copy with the
    bound at 0, is restarted over and the card says so in one line, the room and how long the copy had been going."""
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree)
    f.ask = copying(f, 30.0)
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    assert capsys.readouterr().out.splitlines()[:3] == [f"{words.NOT_DONE} not deployed", f"{words.PLAIN} never quiet in {words.span(0.3)}",
                                                        f"{words.PLAIN} example-room compacts a copy, 30 s in"]
    assert f.restarts == [] and where(tree) == ("main", ids[0]) and [r[3] for r in rows(state)] == ["gave up"]
    f.ask = copying(f, 30.0, 35.0, None)                  # the deploy's first ask, a wait, then taken
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [f"{words.DONE} deployed {ids[1][:7]}", *TODAY, f"{words.DONE} running {ids[1][:7]}",
                   f"{words.DONE} rooms served: 1 of 1", f"{words.PLAIN} no turn cut"] and f.restarts == [ids[1]]
    for conf, age, said in (({}, 400.0, "7 min"), ({"copy_seconds": 0}, 30.0, "30 s")):
        (sub := tmp_path / f"at-{age:.0f}").mkdir()
        tree, ids = repo(sub)
        priv, state = layer(sub, tree, **conf)
        f = Fake(tree)
        f.ask = copying(f, age, age, None)                # gone after the restart, as the restart drops it
        assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[-2:] == [f"{words.PLAIN} no turn cut", f"{words.NOT_DONE} example-room's copy dropped, {said} in"]
        assert f.restarts == [ids[1]] and rows(state) == [("deploy", ids[0], ids[1], "proven", 0)]


def test_a_rollback_after_a_failed_proof_is_held_by_no_copy_a_dead_daemon_cannot_say(tmp_path, capsys):
    """The planted commit never answers /status: its proof fails and the rollback goes at once, as before, though the
    daemon before it had a copy in flight (past the bound, restarted over: the card names it once, in the deploy's lines)."""
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree)
    before = copying(f, 400.0)
    f.ask = lambda st: None if f.commit == ids[2] else before(st)
    assert deploy.run(priv, state, ids[2], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    out = capsys.readouterr().out.splitlines()
    assert f.restarts == [ids[2], ids[0]] and deploy.head(tree) == ids[0]
    assert rows(state) == [("deploy", ids[0], ids[2], "proof failed", 0), ("rollback", ids[2], ids[0], "proven", 0)]
    back = out.index(f"{words.DONE} rolled back to {ids[0][:7]}")
    assert out[:back].count(f"{words.NOT_DONE} example-room's copy dropped, 7 min in") == 1
    assert not [x for x in out[back:] if "copy" in x]


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
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=f.wall) == 1
    assert capsys.readouterr().out.splitlines()[1] == f"{words.PLAIN} the fast tests failed"
    assert f.restarts == [] and deploy.head(tree) == ids[0]
    assert deploy.run(priv, state, ids[2], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=f.wall) == 0
    out = capsys.readouterr().out.splitlines()                         # the real recall check, in the tree: this layer has no index
    assert f"{words.DONE} fast tests passed" in out and f"{words.PLAIN} recall: not checked, no index" in out


def test_each_installed_add_on_s_fast_tests_run_beside_the_core_s_and_one_failing_deploys_nothing(tmp_path, capsys):
    """addons-1 (2026-10-24): an add-on installed under the state folder brings its own fast tests, and the deploy runs
    them after the core's, in its folder, on the tree under test; one failing refuses the deploy with the add-on named."""
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    (a := state / "addons" / "hi" / "tests").mkdir(parents=True)
    (a.parent / "addon.toml").write_text('[abilities.hi]\nversion = "1"\n[abilities.hi.install]\ntests = ["tests"]\n')
    (a / "test_hi.py").write_text("def test_hi():\n    assert False, 'hi is broken'\n")
    f = Fake(tree)
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=f.wall) == 1
    assert capsys.readouterr().out.splitlines()[1:3] == [f"{words.PLAIN} the fast tests of add-on hi failed", f"{words.PLAIN} hi is broken"]
    assert f.restarts == [] and deploy.head(tree) == ids[0]
    (a / "test_hi.py").write_text("def test_hi():\n    pass\n")
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=f.wall) == 0
    assert f"{words.DONE} fast tests passed" in capsys.readouterr().out.splitlines()


def test_the_fast_tests_read_the_layer_deployed_for_and_the_card_names_the_room_and_part(tmp_path, capsys, monkeypatch):
    """The fast tests run with the deploy's --private and --state, so the real-layer budget test is the gate (it skipped
    without YERK_PRIVATE, so a room over budget passed). A planted over-budget part fails the deploy's own fast tests
    (FAST, untracked in the tree), the card names the room and the part, and nothing restarts."""
    monkeypatch.delenv("YERK_PRIVATE", raising=False)
    monkeypatch.delenv("YERK_STATE", raising=False)
    tree, ids = repo(tmp_path)
    for name in ("pyproject.toml", "yerk_ai", "tests"):
        (tree / name).symlink_to(REPO / name)
    priv, state = layer(tmp_path, tree)
    (priv / "memory" / "rules.md").write_text("- a job: " + "x" * 17000 + "\n")       # every room's rules part past its budget
    envs, run = [], subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: envs.append(k.get("env")) or run(*a, **k))
    f = Fake(tree)
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=f.wall) == 1
    out = capsys.readouterr().out.splitlines()
    rules = budgets()["session_start"]["appended"]["rules"]
    assert out[1] == f"{words.PLAIN} the fast tests failed" and re.fullmatch(rf"- example-room rules: 17,\d{{3}} >(?: {rules:,})?\.\.\.", out[2])
    assert all(words.width(x) <= words.PHONE for x in out) and f.restarts == [] and deploy.head(tree) == ids[0]
    assert [(e["YERK_PRIVATE"], e["YERK_STATE"]) for e in envs if e] == [(str(priv), str(state))]
    assert rows(state) == [("deploy", ids[0], ids[1], "tests failed", 0)]


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
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=f.wall) == 1   # tests fail
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
    f.show = lambda prop: str(os.getpid()) if prop == "MainPID" else ""   # yerkd is the unit's main process

    def make(name):
        made.append(name)
        f.unit = name
        return f
    uid = os.getuid()
    manager = f"/user.slice/user-{uid}.slice/user" + chr(64) + f"{uid}.service"   # spelled out: test_scrub reads x@y.z as an email
    leaf = f"0::{manager}/app.slice/yerkd-found.service\n"
    proc = planted_proc(tmp_path, f"4:memory:{manager}\n" + leaf)     # a hybrid v1 line above the unit: skipped
    code = deploy.run(priv, state, ids[1], tests=False, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, proc=proc,
                      systemd=make)
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and made == ["yerkd-found"] * 2 and out[1] == f"{words.PLAIN} unit yerkd-found, from its pid"   # its MainPID, then the deploy
    assert f.restarts == [ids[1]] and Ledger(state / "ledger.sqlite").rows(1, "deploys")[0]["unit"] == "yerkd-found"
    for cgroup, cmdline in (("0::/user.slice/user-1000.slice/session-2.scope\n", None),          # a terminal's
                            (leaf, b"/usr/bin/sleep\x00infinity\x00")):                          # a reused pid
        proc = planted_proc(tmp_path, cgroup, *([cmdline] if cmdline else []))
        assert deploy.run(priv, state, ids[2], tests=False, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap,
                          proc=proc, systemd=make) == 1
        assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed",
                                                        f"{words.PLAIN} no unit found: set [deploy] unit"]
    assert made == ["yerkd-found"] * 2 and f.restarts == [ids[1]] and where(tree) == ("main", ids[1])   # nothing touched
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
    assert rows(state)[-2:] == [("deploy", ids[0], ids[1], "fault", None), ("rollback", ids[1], ids[0], "proven", 0)]   # not counted
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
    assert f.commit == ids[0] and rows(state)[-2:] == [("deploy", ids[0], ids[1], "fault", None),   # not counted
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


def test_a_failed_rollback_exits_2_and_the_morning_diff_names_every_deploy_not_proven(tmp_path, capsys, monkeypatch):
    noon(monkeypatch)                                                  # the diff a minute on stays on the rows' day
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
    assert lines[0] == f"{words.NOT_DONE} morning: 3 new since the last diff" and all(words.width(x) <= words.PHONE for x in want)   # no curate run yet; a doctor never run is none
    (state / "morning").mkdir()
    (state / "morning" / "last.json").write_text(json.dumps(stamp))
    again, _ = morning.build(priv, state, accounts, cool, led, live, now + 60)
    assert again[0] == f"{words.PLAIN} morning: nothing new, 3 known" and sum(x in want for x in again) == 2   # said once
    later, _ = morning.build(priv, state, accounts, cool, led, live, now + 86400)
    assert not [x for x in later if " rollback " in x]                                    # the last 24 hours only


def test_a_unit_whose_main_process_is_not_yerkd_is_refused_and_a_named_unit_wins_over_the_pid(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree, unit="")
    f, made, main = Fake(tree), [], {"pid": "1"}                       # the host unit's own program: a tmux, a gateway
    f.show = lambda prop: main["pid"] if prop == "MainPID" else ""

    def make(name):
        made.append(name)
        f.unit = name
        return f
    uid, ok = os.getuid(), (lambda p: {"ok": True})
    manager = f"/user.slice/user-{uid}.slice/user" + chr(64) + f"{uid}.service"
    proc = planted_proc(tmp_path, f"0::{manager}/app.slice/host.service\n")      # yerkd started inside it
    assert deploy.run(priv, state, ids[1], tests=False, ask=f.ask, post=ok, sleep=nap, proc=proc, systemd=make) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed",
                                                    f"{words.PLAIN} no unit found: set [deploy] unit"]
    assert made == ["host"] and f.restarts == [] and where(tree) == ("main", ids[0])   # its MainPID asked, nothing restarted
    main["pid"] = str(os.getpid())                                     # yerkd the main process, of the system manager's unit
    proc = planted_proc(tmp_path, "0::/system.slice/host.service\n")
    assert deploy.run(priv, state, ids[1], tests=False, ask=f.ask, post=ok, sleep=nap, proc=proc, systemd=make) == 1
    assert capsys.readouterr().out.splitlines()[1] == f"{words.PLAIN} no unit found: set [deploy] unit"
    assert made == ["host"] and f.restarts == []
    conf = priv / "yerkd.toml"                                         # a named unit, the pid in another findable one
    conf.write_text(conf.read_text().replace('unit = ""', 'unit = "yerkd-named"'))
    proc = planted_proc(tmp_path, f"0::{manager}/app.slice/yerkd-other.service\n")
    assert deploy.run(priv, state, ids[1], tests=False, ask=f.ask, post=ok, sleep=nap, proc=proc, systemd=make) == 0
    out = capsys.readouterr().out.splitlines()
    assert made == ["host", "yerkd-named"] and f.restarts == [ids[1]] and not [x for x in out if "from its pid" in x]
    assert Ledger(state / "ledger.sqlite").rows(1, "deploys")[0]["unit"] == "yerkd-named"


def test_a_fault_in_the_quiet_wait_or_the_open_rows_read_refuses_and_puts_the_tree_back(tmp_path, capsys, monkeypatch):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree)
    for name in ("last_received", "inbound_open"):                     # the quiet check's read, then settle's own
        real, once = getattr(Ledger, name), []

        def locked(self, *a, real=real, once=once, **k):
            if not once:
                once.append(1)
                raise sqlite3.OperationalError("database is locked")
            return real(self, *a, **k)
        with monkeypatch.context() as m:
            m.setattr(Ledger, name, locked)
            assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
        assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed",
                                                        f"{words.PLAIN} OperationalError in locked",
                                                        f"{words.PLAIN} nothing was stopped", f"{words.PLAIN} fast tests skipped"]
        assert once and f.restarts == [] and where(tree) == ("main", ids[0])          # the checkout put back
    assert rows(state) == [("deploy", ids[0], ids[1], "fault", 0)] * 2


def test_ctrl_c_in_the_quiet_wait_stops_nothing_and_in_the_proof_rolls_back_then_exits_130(tmp_path, capsys, monkeypatch):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree)
    f.busy = True                                                      # a turn runs: Ctrl-C lands in the wait

    def ctrl_c(_s):
        raise KeyboardInterrupt
    try:
        code = deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=ctrl_c)
    except KeyboardInterrupt:
        code = "a traceback"
    assert code == 130 and f.restarts == [] and where(tree) == ("main", ids[0])
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed", f"{words.PLAIN} KeyboardInterrupt in ctrl_c",
                                                    f"{words.PLAIN} nothing was stopped", f"{words.PLAIN} fast tests skipped"]
    f.busy, real, calls = False, deploy.prove, []

    def prove(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt                                    # after the restart, before any check
        return real(*a, **k)
    monkeypatch.setattr(deploy, "prove", prove)
    try:
        code = deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap)
    except KeyboardInterrupt:
        code = "a traceback"
    out = capsys.readouterr().out.splitlines()
    assert code == 130 and f.restarts == [ids[1], ids[0]] and f.commit == ids[0] and where(tree) == ("main", ids[0])
    assert out[:4] == [f"{words.NOT_DONE} {ids[1][:7]} broke off, not proven", f"{words.PLAIN} fast tests skipped",
                       f"{words.NOT_DONE} KeyboardInterrupt in prove", f"{words.PLAIN} no turn cut"]
    assert f"{words.DONE} rolled back to {ids[0][:7]}" in out
    assert rows(state)[-2:] == [("deploy", ids[0], ids[1], "fault", 0), ("rollback", ids[1], ids[0], "proven", 0)]


def test_sigterm_or_sighup_in_the_proof_rolls_back_as_ctrl_c_does_and_the_handlers_come_back(tmp_path, capsys):
    for sig in (signal.SIGTERM, signal.SIGHUP):
        (tmp_path / sig.name).mkdir()
        tree, ids = repo(tmp_path / sig.name)
        priv, state = layer(tmp_path / sig.name, tree)
        f, hits, sent = Fake(tree), [], []
        plain = f.ask

        def ask(st, f=f, plain=plain, sent=sent, sig=sig):            # the first check after the restart: the signal
            if f.restarts and not sent:
                sent.append(sig)
                os.kill(os.getpid(), sig)
            return plain(st)
        f.ask = ask

        def mine(*a, hits=hits):                                       # the test's own: the old code left it the signal
            hits.append(a)
        before = signal.signal(sig, mine)
        try:
            code = deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap)
            assert signal.getsignal(sig) is mine                       # put back after the deploy
        finally:
            signal.signal(sig, before)
        out = capsys.readouterr().out.splitlines()
        assert sent == [sig] and hits == [] and code == 130 and f.restarts == [ids[1], ids[0]] and f.commit == ids[0]
        assert out[2].startswith(f"{words.NOT_DONE} {sig.name} in ") and f"{words.DONE} rolled back to {ids[0][:7]}" in out
        assert rows(state) == [("deploy", ids[0], ids[1], "fault", 0), ("rollback", ids[1], ids[0], "proven", 0)]


def test_the_row_is_written_before_the_restart_so_a_sigkill_there_leaves_one_the_morning_diff_names(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f, seen = Fake(tree), []
    led, accounts, cool = Ledger(state / "ledger.sqlite"), load_accounts(priv / "accounts.toml"), Cooldowns(state / "c.json")
    live, real = {"units": [], "started": time.time() - 86400}, f.restart

    def restart():                                                     # a SIGKILL here leaves what the ledger holds now
        seen.append((rows(state), morning.build(priv, state, accounts, cool, led, live, time.time() + 60)[0]))
        return real()
    f.restart = restart
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 0
    (during, diff), = seen
    assert during == [("deploy", ids[0], ids[1], "begun", None)]
    assert [x for x in diff if x.startswith(f"{words.NOT_DONE} deploy {ids[1][:7]}: begun")]
    assert rows(state) == [("deploy", ids[0], ids[1], "proven", 0)]                # the same row, set after the proof


def test_a_row_that_cannot_be_written_never_skips_the_rollback_and_a_deploy_without_one_never_restarts(
        tmp_path, capsys, monkeypatch):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f, real, lock = Fake(tree, connected=lambda commit: commit != ids[1]), Ledger.deploy, {"outcome": "proof failed"}

    def deploy_row(self, *a, **k):                                     # the ledger locked when that row is written
        if k.get("outcome") == lock["outcome"]:
            raise sqlite3.OperationalError("database is locked")
        return real(self, *a, **k)
    monkeypatch.setattr(Ledger, "deploy", deploy_row)
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    got = capsys.readouterr()
    out = got.out.splitlines()
    assert f.restarts == [ids[1], ids[0]] and f.commit == ids[0] and where(tree) == ("main", ids[0])   # rolled back
    assert f"{words.NOT_DONE} deploy outcome not in the ledger" in out and f"{words.DONE} rolled back to {ids[0][:7]}" in out
    assert got.err.splitlines() == ["deploy: the deploy row was not written: OperationalError in deploy_row"]
    assert rows(state) == [("deploy", ids[0], ids[1], "begun", None), ("rollback", ids[1], ids[0], "proven", 0)]
    lock["outcome"] = "begun"                                          # no row before the restart: no restart
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed",
                                                    f"{words.PLAIN} OperationalError in deploy_row",
                                                    f"{words.PLAIN} nothing was stopped", f"{words.PLAIN} fast tests skipped"]
    assert f.restarts == [ids[1], ids[0]] and where(tree) == ("main", ids[0]) and rows(state)[-1][3] == "fault"


def test_when_the_restart_or_the_cut_count_raised_the_card_says_the_cut_was_not_counted(tmp_path, capsys, monkeypatch):
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    f = Fake(tree)
    real, calls = f.restart, []

    def restart():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the bus did not answer")
        return real()
    f.restart = restart
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[2:4] == [f"{words.NOT_DONE} OSError in restart", f"{words.NOT_DONE} turns cut: not counted"]
    assert out.count(f"{words.PLAIN} no turn cut") == 1 and out.index(f"{words.PLAIN} no turn cut") > 4   # the rollback's
    assert rows(state) == [("deploy", ids[0], ids[1], "fault", None), ("rollback", ids[1], ids[0], "proven", 0)]
    real_cut, counts = deploy.cut, []

    def cut(*a, **k):                                                  # the ledger locked when the cut is counted
        counts.append(1)
        if len(counts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_cut(*a, **k)
    monkeypatch.setattr(deploy, "cut", cut)
    assert deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[2:4] == [f"{words.NOT_DONE} OperationalError in cut", f"{words.NOT_DONE} turns cut: not counted"]
    assert rows(state)[-2:] == [("deploy", ids[0], ids[1], "fault", None), ("rollback", ids[1], ids[0], "proven", 0)]


def test_the_morning_diff_names_a_proven_deploy_that_cut_a_turn(tmp_path, monkeypatch):
    noon(monkeypatch)                                                  # the diff a minute on stays on the rows' day
    priv, state, accounts, cool = dead_layer(tmp_path)
    led = Ledger(state / "ledger.sqlite")
    led.deploy(kind="deploy", unit="u", before="a" * 40, after="b" * 40, outcome="proven", cut=2)   # its card may be lost
    led.deploy(kind="deploy", unit="u", before="b" * 40, after="c" * 40, outcome="proof failed", cut=1)
    led.deploy(kind="rollback", unit="u", before="c" * 40, after="b" * 40, outcome="proven", cut=0)  # clean: not named
    live, now = {"units": [], "started": time.time() - 86400}, time.time() + 60
    lines, _ = morning.build(priv, state, accounts, cool, led, live, now)
    at = words.clock(float(led.deploys(0)[0]["at"]), now)
    assert [x for x in lines if " deploy " in x or " rollback " in x] == [
        f"{words.NOT_DONE} deploy bbbbbbb: proven, 2 cut, {at}", f"{words.NOT_DONE} deploy ccccccc: proof failed, 1 cut"]
    assert all(words.width(x) <= words.PHONE for x in lines)


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
    assert any(x.startswith(f"{words.WORKING} account-") and x.endswith(f"... 5h {words.WARN}91%") for x in lines)
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
    assert want in lines and lines[0] == f"{words.NOT_DONE} morning: 2 new since the last diff"   # no curate run yet; a doctor never run is none
    (state / "morning").mkdir()
    (state / "morning" / "last.json").write_text(json.dumps(stamp))
    again, stamp = morning.build(priv, state, accounts, cool, led, live, NOW + 60)
    assert again[0] == f"{words.PLAIN} morning: nothing new, 2 known"   # the same set: said once
    message(led, "i", NOW - 900)
    third, _ = morning.build(priv, state, accounts, cool, led, live, NOW + 120)
    assert third[0] == f"{words.NOT_DONE} morning: 1 new since the last diff"
    blind, _ = morning.build(priv, state, accounts, cool, Ledger(tmp_path / "empty.sqlite"), None, NOW)
    assert f"{words.NOT_DONE} dead turns: unknown, no yerkd answers" in blind   # blind, never clean
    clean, _ = morning.build(priv, state, accounts, cool, Ledger(tmp_path / "empty.sqlite"), live, NOW)
    assert f"{words.PLAIN} dead turns: none in 24h" in clean


def test_a_text_in_a_watched_chat_is_never_a_dead_turn_nor_a_turn_a_restart_cut(tmp_path):
    led, t = Ledger(tmp_path / "ledger.sqlite"), NOW - 3600
    message(led, "t1", t, room=intake.ROOM, sender="alex", state="decided")         # theirs: it gets a decision, never a turn
    message(led, "t2", t + 30, room=intake.ROOM, state="filed")                     # the owner's own text in that thread
    message(led, "t3", t + 60, room=intake.ROOM, sender="alex", state="received")   # a burst a stop left open
    message(led, "d", t + 90, room="example-quiet", state="received")               # a room's message nobody took
    assert [d["message_id"] for d in led.dead_turns(NOW - 86400, NOW - 600)] == ["d"]
    assert [r["message_id"] for r in led.inbound_open()] == ["d"] and deploy.cut(led, set(), NOW) == 1
    assert [r["message_id"] for r in led.inbound_open(intake.ROOM)] == ["t3"]       # the intake still finds its own


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



def test_a_deploy_restarts_yerk_index_after_yerkd_and_the_running_line_wants_both_on_the_commit(tmp_path, capsys):
    """yerk-index ran an older mentions.py than yerkd for about four hours on 2026-10-20: a deploy never restarted it. Its
    unit now comes from its pid (index.json), it restarts right after yerkd, and the running line holds only once it runs
    the commit too; an index that comes back on the old code fails the proof, and the rollback restarts both again."""
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    (ix := state / "memory" / "index.json").parent.mkdir()
    ix.write_text(json.dumps({"pid": os.getpid(), "commit": ids[0]}))
    uid = os.getuid()
    manager = f"/user.slice/user-{uid}.slice/user" + chr(64) + f"{uid}.service"
    proc = planted_proc(tmp_path, f"0::{manager}/app.slice/yerk-index.service\n", b"/v/bin/python\x00-m\x00yerk_ai.memindex\x00")
    order, takes, made, f = [], [True], [], Fake(tree)
    f.restart = lambda: order.append("yerkd") or Fake.restart(f)

    class Index(Fake):
        def restart(self):
            order.append("yerk-index")
            if takes[0]:                                 # else it comes back on the code it ran before
                ix.write_text(json.dumps({"pid": os.getpid(), "commit": deploy.head(tree)}))
            return True

        def show(self, prop):
            return str(os.getpid()) if prop == "MainPID" else ""
    run = functools.partial(deploy.run, priv, state, tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap,
                            proc=proc, systemd=lambda name: made.append(name) or Index(tree))
    assert run(ids[1]) == 0 and order == ["yerkd", "yerk-index"] and set(made) == {"yerk-index"}
    assert f"{words.DONE} running {ids[1][:7]}" in capsys.readouterr().out.splitlines()
    takes[0], order[:] = False, []
    assert run(ids[2]) == 1 and order == ["yerkd", "yerk-index"] * 2 and deploy.head(tree) == ids[1]
    out = capsys.readouterr().out.splitlines()
    assert f"{words.NOT_DONE} running {ids[2][:7]}" in out and f"{words.DONE} rolled back to {ids[1][:7]}" in out
    assert [r[3] for r in rows(state)] == ["proven", "proof failed", "proven"]


def test_with_no_private_given_the_fast_tests_read_the_layer_on_the_daemon_s_command_line_and_a_given_one_wins(
        tmp_path, capsys, monkeypatch):
    """No --private and no $YERK_PRIVATE was the example layer, so two of the lane's deploys ran its
    question set against the live index in the fast tests and refused: the private and state folders are now the ones on
    the command line of the unit the pid is in (`--private P` under its folder, `--state=S`), the card says so and goes
    to that layer's machine room, and a --private given is never replaced."""
    monkeypatch.delenv("YERK_PRIVATE", raising=False)
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])     # the daemon's: its [deploy] unit, a machine room
    lane = tmp_path / "lane"                                           # the deploy's own state folder, the daemon's pid file in it
    lane.mkdir()
    (lane / "yerkd.json").write_text((state / "yerkd.json").read_text())
    f, made, posts = Fake(tree), [], []
    f.show = lambda prop: {"MainPID": str(os.getpid()), "WorkingDirectory": str(tmp_path)}.get(prop, "")
    f.argv = lambda: made.append("argv") or ["/v/bin/python", "-m", "yerk_ai.daemon", "--private", "private", f"--state={state}"]

    def make(name):
        made.append(name)
        f.unit = name
        return f

    def post(p):
        posts.append(p["machine"]["room"])
        return {"ok": True}
    uid = os.getuid()
    manager = f"/user.slice/user-{uid}.slice/user" + chr(64) + f"{uid}.service"
    proc = planted_proc(tmp_path, f"0::{manager}/app.slice/yerkd-test.service\n")
    envs, run = [], subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: envs.append(k.get("env")) or run(*a, **k))
    assert deploy.run(private_dir(), lane, ids[1], ask=f.ask, post=post, sleep=nap, proc=proc, systemd=make, wall=f.wall) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [f"{words.DONE} deployed {ids[1][:7]}", f"{words.PLAIN} private folder: the daemon's", f"{words.DONE} fast tests passed"]
    assert [(e["YERK_PRIVATE"], e["YERK_STATE"]) for e in envs if e] == [(str(priv), str(state))]
    assert made == ["yerkd-test", "yerkd-test", "argv", "yerkd-fake"] and posts == ["example-quiet"]   # its [deploy] unit, its machine room
    assert rows(state) == [("deploy", ids[0], ids[1], "proven", 0)] and not (lane / "ledger.sqlite").exists()
    other = tmp_path / "given"                                         # a --private given: the unit's command is never read
    shutil.copytree(priv, other)
    envs[:], made[:] = [], []
    assert deploy.run(other, state, ids[2], ask=f.ask, post=post, sleep=nap, proc=proc, systemd=make, wall=f.wall) == 0
    out = capsys.readouterr().out.splitlines()
    assert [(e["YERK_PRIVATE"], e["YERK_STATE"]) for e in envs if e] == [(str(other), str(state))]
    assert made == ["yerkd-fake"] and not [x for x in out if "private layer" in x]


def test_with_no_private_given_and_none_on_the_unit_s_command_line_the_deploy_refuses_and_touches_nothing(tmp_path, capsys, monkeypatch):
    """The example layer is for tests and the README, never a deploy: with no --private and no $YERK_PRIVATE, no unit found
    (a yerkd in a terminal) or a unit whose command carries no --private (units/yerkd.service sets its layer in
    Environment=) is one line, before the checkout, the tests or systemd are touched, and the card is the terminal's."""
    monkeypatch.delenv("YERK_PRIVATE", raising=False)
    tree, ids = repo(tmp_path)
    _, state = layer(tmp_path, tree)
    f, posts = Fake(tree), []
    f.show = lambda prop: str(os.getpid()) if prop == "MainPID" else ""
    f.argv = lambda: ["/v/bin/yerkd"]
    uid = os.getuid()
    manager = f"/user.slice/user-{uid}.slice/user" + chr(64) + f"{uid}.service"
    for cgroup in ("0::/user.slice/user-1000.slice/session-2.scope\n", f"0::{manager}/app.slice/yerkd-test.service\n"):
        proc = planted_proc(tmp_path, cgroup)
        assert deploy.run(private_dir(), state, ids[1], ask=f.ask, post=lambda p: posts.append(p) or {"ok": True}, sleep=nap, wall=f.wall,
                          proc=proc, systemd=lambda name: f) == 1
        assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed", f"{words.PLAIN} no private folder: pass --private"]
    assert f.restarts == [] and posts == [] and where(tree) == ("main", ids[0]) and [r[3] for r in rows(state)] == ["refused"] * 2


def test_the_recall_step_searches_each_control_on_the_exact_half_and_a_miss_there_once_more_with_the_meaning_half(tmp_path):
    """memory.controls, `yerk deploy`'s recall step: a control the exact half finds is one search, with the meaning half off;
    one it misses is searched once more as a room's recall searches, meaning half and all, and is a hit through it; one
    neither finds is missed. The scored questions are not its business, and with no index or no controls it holds nothing."""
    s = memory.settings(tmp_path / "private", tmp_path / "state", {"timezone": "UTC"})
    s.index_db.parent.mkdir(parents=True)
    s.index_db.write_text("")                                          # an index there; the fake search never opens it
    qs = [{"id": f"ctrl-{i}", "q": f"where is example thing {i}", "answer_tokens": [f"example{i}"], "control": True} for i in range(13)]
    (qfile := tmp_path / "questions.json").write_text(json.dumps({"questions": [*qs, {"id": "q-1", "q": "a scored one", "answer_tokens": ["x"]}]}))
    calls: list = []

    def search(meaning_only=(), nowhere=()):
        def find(_s, query, k, deadline_s=5.0, wide=True):
            cid = "ctrl-" + query.rsplit(" ", 1)[1]
            calls.append((cid, wide))
            hit = cid not in nowhere and (wide or cid not in meaning_only)
            return {"ok": True, "hits": [{"path": "/x/memory/notes.md", "text": f"example{cid[5:]} is here" if hit else "else"}]}
        return find
    assert memory.controls(s, qfile, find=search()) == {"of": 13, "hit": 13, "meaning": [], "missed": [], "none": ""}
    assert calls == [(f"ctrl-{i}", False) for i in range(13)]          # the exact half alone, once each
    calls.clear()
    assert memory.controls(s, qfile, find=search(meaning_only={"ctrl-3"})) == {"of": 13, "hit": 13, "meaning": ["ctrl-3"], "missed": [], "none": ""}
    assert len(calls) == 14 and [c for c in calls if c[1]] == [("ctrl-3", True)]     # the retry: once, and for that control only
    calls.clear()
    r = memory.controls(s, qfile, find=search(nowhere={"ctrl-5"}))
    assert (r["hit"], r["meaning"], r["missed"], calls.count(("ctrl-5", True)), len(calls)) == (12, [], ["ctrl-5"], 1, 14)
    calls.clear()
    assert memory.controls(s, tmp_path / "none.json", find=search())["none"] == "no controls"
    s.index_db.unlink()
    assert memory.controls(s, qfile, find=search()) == {"of": 13, "hit": 0, "meaning": [], "missed": [], "none": "no index"} and calls == []


def test_a_deploy_holds_the_live_controls_after_the_fast_tests_and_one_missed_restarts_nothing(tmp_path, capsys):
    """After the fast tests and before the restart the deploy runs the layer's controls in the tree (`yerk recall-bench
    --controls`, its check): the card says how many hit and how many only through the meaning half, or names the ones
    missed, and then nothing restarts and the tree goes back; a check that never ran holds nothing. --no-tests skips it too."""
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    f, seen, ok = Fake(tree), [], lambda p: {"ok": True}

    def check(r):
        return lambda *a: seen.append(a) or r
    miss = {"of": 13, "hit": 12, "meaning": [], "missed": ["ctrl-example"], "none": ""}
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=ok, sleep=nap, wall=f.wall, check=check(miss)) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed", f"{words.DONE} fast tests passed",
                                                    f"{words.PLAIN} recall: 12 of 13 hit", f"{words.PLAIN} missed: ctrl-example"]
    assert seen == [(tree, priv, state)] and f.restarts == [] and where(tree) == ("main", ids[0])
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=ok, sleep=nap, wall=f.wall, check=check({"broke": True})) == 1
    assert capsys.readouterr().out.splitlines()[2:] == [f"{words.PLAIN} the recall check did not run"] and f.restarts == []
    held = {"of": 13, "hit": 13, "meaning": ["ctrl-a", "ctrl-b", "ctrl-c"], "missed": [], "none": ""}
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=ok, sleep=nap, wall=f.wall, check=check(held)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1:3] == [f"{words.DONE} fast tests passed", f"{words.DONE} recall: 13 of 13 hit (3 by meaning)"] and f.restarts == [ids[1]]
    assert all(words.width(x) <= words.PHONE for x in out)
    assert deploy.run(priv, state, ids[2], tests=False, sd=f, ask=f.ask, post=ok, sleep=nap, check=check(miss)) == 0 and len(seen) == 3
    assert [r[3] for r in rows(state)] == ["recall failed", "recall failed", "proven", "proven"]


def test_a_proven_deploy_installs_and_turns_on_each_timer_in_units_the_box_does_not_have(tmp_path, capsys):
    """quiet-doctor (2026-10-24): yerk-curate.timer sat in units/ and was never copied, so the night's curation never ran
    on the box. A proven deploy copies each timer systemd calls not-found, with its service, into the user's unit folder
    (0600), reloads once and turns it on, a card line each; one already there, on or off, is left as it is."""
    tree, ids = repo(tmp_path)
    priv, state = layer(tmp_path, tree)
    (tree / "units").mkdir()                                           # untracked: the tree stays clean
    for name in ("yerk-curate.timer", "yerk-curate.service", "yerk-doctor.timer", "yerk-doctor.service"):
        shutil.copy(deploy.REPO / "units" / name, tree / "units")
    home, calls, f = tmp_path / "user-units", [], Fake(tree)
    states = {"yerk-curate.timer": "not-found", "yerk-doctor.timer": "disabled"}

    def ctl(*a, **k):
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, "".join(f"{states[t]}\n" for t in a[1:]) if a[0] == "is-enabled" else "", "")
    code = deploy.run(priv, state, ids[1], tests=False, sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, home=home, ctl=ctl)
    assert code == 0 and capsys.readouterr().out.splitlines()[-1] == f"{words.DONE} yerk-curate.timer installed, on"
    assert sorted(p.name for p in home.iterdir()) == ["yerk-curate.service", "yerk-curate.timer"]
    assert (home / "yerk-curate.timer").read_bytes() == (deploy.REPO / "units" / "yerk-curate.timer").read_bytes()
    assert stat.S_IMODE((home / "yerk-curate.service").stat().st_mode) == 0o600
    assert calls == [("is-enabled", "yerk-curate.timer", "yerk-doctor.timer"), ("daemon-reload",), ("enable", "--now", "yerk-curate.timer")]
    states["yerk-curate.timer"], calls[:] = "enabled", []
    assert deploy.timers(tree, home, ctl) == [] and calls == [("is-enabled", "yerk-curate.timer", "yerk-doctor.timer")]
    states["yerk-curate.timer"] = "not-found"                            # an enable that fails is red on the card
    assert deploy.timers(tree, tmp_path / "other", lambda *a, **k: subprocess.CompletedProcess(
        a, 1 if a[0] == "enable" else 0, "not-found\nenabled\n" if a[0] == "is-enabled" else "", "")) == [f"{words.NOT_DONE} yerk-curate.timer not enabled"]


# the step behind the wall (deploy.step, workers.checked): the core's fast tests, each add-on's and the recall controls
def ran(f):
    """Each check the stand-in saw: (its command, its folder)."""
    return [(argv, cwd) for argv, cwd in f.checks]


def addon(state, test="def test_hi():\n    pass\n"):
    (a := state / "addons" / "hi" / "tests").mkdir(parents=True)
    (a.parent / "addon.toml").write_text('[abilities.hi]\nversion = "1"\n[abilities.hi.install]\ntests = ["tests"]\n')
    (a / "test_hi.py").write_text(test)
    return a.parent


def test_the_core_s_tests_an_add_on_s_and_the_controls_each_run_once_through_the_wall_with_today_s_command(tmp_path, capsys, monkeypatch):
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    folder, f = addon(state), Fake(tree)
    controls = '{"of": 2, "hit": 2, "meaning": [], "missed": [], "none": ""}\n'
    seen = []

    def wall(argv, **k):
        seen.append((argv, k))
        return subprocess.CompletedProcess(argv, 0, controls if "recall-bench" in argv else "1 passed\n", "")
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=wall) == 0
    py = deploy.sys.executable
    assert [(a, k["cwd"]) for a, k in seen] == [([py, "-m", "pytest", "-q", "-x", "--tb=line", "tests/test_x.py"], tree),
                                               ([py, "-m", "pytest", "-q", "-x", "--tb=line", "tests"], folder),
                                               ([py, "-m", "yerk_ai.cli", "recall-bench", "--controls", "--private", str(priv), "--state", str(state)], tree)]
    assert all((k["tree"], k["private"], k["state"]) == (tree, priv, state) for _, k in seen) and seen[2][1]["timeout"] == 180
    assert capsys.readouterr().out.splitlines()[:3] == [f"{words.DONE} deployed {ids[1][:7]}", f"{words.DONE} fast tests passed",
                                                        f"{words.DONE} recall: 2 of 2 hit"]
    seen.clear()                                                      # with no stand-in, the wall's own run (workers.checked)
    monkeypatch.setattr(workers, "checked", wall)
    assert deploy.run(priv, state, ids[2], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 0 and len(seen) == 3


def test_a_check_that_fails_behind_the_wall_deploys_nothing_and_the_card_is_today_s(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    addon(state)
    f = Fake(tree)

    def wall(fails):
        def go(argv, **k):
            failed = fails in argv
            return subprocess.CompletedProcess(argv, int(failed), f"{k['cwd']}/t.py:2: AssertionError: {fails} is broken\n" if failed else "", "")
        return go
    for fails, said in [("tests/test_x.py", "the fast tests failed"), ("tests", "the fast tests of add-on hi failed")]:
        assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=wall(fails)) == 1
        assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} not deployed", f"{words.PLAIN} {said}",
                                                        f"{words.PLAIN} {fails} is broken"]
        assert f.restarts == [] and where(tree) == ("main", ids[0])
    assert [r[3] for r in rows(state)] == ["tests failed", "tests failed"]


def test_wall_false_runs_the_three_plainly_and_the_card_says_so(tmp_path, capsys):
    """[deploy] wall = false: the checks as before the wall, children of the deploy (the wall's own run is refused in this
    file, so a walled one fails the test), and a line on the card, on a pass and on a failure."""
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    assert open('f.txt').read() != '1\\n'\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"], wall=False)
    f = Fake(tree)
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == [f"{words.NOT_DONE} not deployed", f"{words.PLAIN} the fast tests failed"] and out[-1] == f"{words.PLAIN} {words.WALL_OFF}"
    assert deploy.run(priv, state, ids[2], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1:4] == [f"{words.DONE} fast tests passed", f"{words.PLAIN} {words.WALL_OFF}", f"{words.PLAIN} recall: not checked, no index"]
    assert all(words.width(x) <= words.PHONE for x in out)


def test_a_wall_that_cannot_be_built_deploys_nothing_and_names_the_setting(tmp_path, capsys):
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    f = Fake(tree)

    def wall(at):
        def go(argv, **k):
            if at in argv:
                raise workers.NoWall("systemd-run exited 1: Failed to connect to bus: No medium found /run/user/1000/bus")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return go
    for at in ("tests/test_x.py", "recall-bench"):                     # the tests' unit, or the controls'
        assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=wall(at)) == 1
        out = capsys.readouterr().out.splitlines()
        assert out[:2] == [f"{words.NOT_DONE} not deployed", f"{words.PLAIN} {words.NO_WALL}"]
        assert out[2].startswith(f"{words.PLAIN} systemd-run exited 1: Failed") and "/" not in out[2] and len(out) == 3
        assert f.restarts == [] and where(tree) == ("main", ids[0])
    assert [r[3] for r in rows(state)] == ["no wall", "no wall"]


def test_the_live_index_is_held_open_while_the_checks_run(tmp_path, capsys):
    """Behind the wall the state folder is read-only and a WAL index's reader there needs its -shm, which a closed index has
    not: the deploy holds the index open read-only out here meanwhile, and closes it after."""
    from yerk_ai import memindex
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    s = memory.settings(priv, state)
    idx = memindex.Index(s.index_db, s.scope)
    idx.sync()
    idx.close()
    shm = Path(str(memindex.live(s.index_db)) + "-shm")
    assert not shm.exists()
    held = []

    def wall(argv, **k):
        held.append(shm.exists())
        return subprocess.CompletedProcess(argv, 0, '{"of": 0, "hit": 0, "meaning": [], "missed": [], "none": "no controls"}\n', "")
    f = Fake(tree)
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=wall) == 0
    assert held == [True, True]


def test_the_hold_says_the_pid_it_saw_the_index_alive_under_and_takes_it_back(tmp_path, monkeypatch):
    """A walled check cannot see yerk-index's pid (its process list is its own), so the deploy's hold says it in
    YERK_INDEX_SEEN while the checks run: only a pid alive out here, gone again after, a caller's own value put back."""
    (state := tmp_path / "state" / "memory").mkdir(parents=True)
    monkeypatch.delenv(memory.INDEX_SEEN, raising=False)
    with deploy.index_held(state.parent):                              # no status file: nothing said
        assert memory.INDEX_SEEN not in os.environ
    (state / "index.json").write_text(json.dumps({"pid": os.getpid(), "port": 1}))
    with deploy.index_held(state.parent):
        assert os.environ[memory.INDEX_SEEN] == str(os.getpid())
    assert memory.INDEX_SEEN not in os.environ
    gone = subprocess.Popen([deploy.sys.executable, "-c", "pass"])
    gone.wait()
    (state / "index.json").write_text(json.dumps({"pid": gone.pid, "port": 1}))
    with deploy.index_held(state.parent):                              # a pid that is gone: never vouched for
        assert memory.INDEX_SEEN not in os.environ
    monkeypatch.setenv(memory.INDEX_SEEN, "7")                         # one its caller set: as it was, after
    (state / "index.json").write_text(json.dumps({"pid": os.getpid(), "port": 1}))
    with deploy.index_held(state.parent):
        assert os.environ[memory.INDEX_SEEN] == str(os.getpid())
    assert os.environ[memory.INDEX_SEEN] == "7"


def test_the_walled_tests_are_given_each_room_s_newest_transcript_and_nothing_else_of_the_home(tmp_path):
    """The budget test reads Claude Code's parts as each room last sent them, from its newest transcript, in an account's
    folder in the home the wall hides: unread, each part counts at its line and nothing is measured. The deploy names
    those files for its tests' run, and only a file that lies in an account's projects folder."""
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    acct = tmp_path / "acct"
    (priv / "accounts.toml").write_text(f'[accounts.one]\nconfig_dir = "{acct}"\n')
    (said := acct / "projects" / "p" / "s.jsonl").parent.mkdir(parents=True)
    said.write_text("{}\n")
    (key := acct / ".credentials.json").write_text("not a key\n")     # the account's own login, beside its projects folder
    (state / "sessions.json").write_text(json.dumps({"example-room": {"transcript": str(said)}, "example-quiet": {"transcript": str(key)}}))
    assert deploy.transcripts(priv, state) == [said]                   # never a file outside an account's projects folder
    (said.parent / "link.jsonl").symlink_to(key)                       # nor one in it that leads out
    (state / "sessions.json").write_text(json.dumps({"example-room": {"transcript": str(said)}, "example-quiet": {"transcript": str(said.parent / "link.jsonl")}}))
    assert deploy.transcripts(priv, state) == [said]
    f, seen = Fake(tree), []

    def wall(argv, **k):
        seen.append((argv, k.get("reads")))
        return f.wall(argv, **k)
    assert deploy.run(priv, state, ids[1], sd=f, ask=f.ask, post=lambda p: {"ok": True}, sleep=nap, wall=wall) == 0
    assert [r for a, r in seen if "pytest" in a] == [[said]] and [r for a, r in seen if "recall-bench" in a] == [None]
    (state / "sessions.json").unlink()
    assert deploy.transcripts(priv, state) == [] and deploy.transcripts(tmp_path / "none", state) == []


def test_check_runs_the_step_alone_and_touches_nothing(tmp_path, capsys, monkeypatch):
    """`yerk deploy --check`: the step as a deploy runs it, on the checkout as it stands; nothing moved or restarted, no
    ledger row; 0 when all held, 1 when not."""
    tree, ids = repo(tmp_path)
    (tree / "tests").mkdir()
    (tree / "tests" / "test_x.py").write_text("def test_x():\n    assert open('f.txt').read() != '1\\n'\n")
    priv, state = layer(tmp_path, tree, tests=["tests/test_x.py"])
    f = Fake(tree)
    f.argv = lambda: ["/v/bin/yerkd"]                                  # a unit's command with no --private in it
    assert deploy.check_step(priv, state, sd=f, wall=f.wall) == 0
    assert capsys.readouterr().out.splitlines() == [f"{words.DONE} fast tests passed", f"{words.PLAIN} recall: not checked, no index"]
    assert [a[-1] for a, _ in ran(f)] == ["tests/test_x.py", str(state)] and ran(f)[0][1] == tree
    (tree / "f.txt").write_text("1\n")                                 # the checkout as it stands: a failing test
    assert deploy.check_step(priv, state, sd=f, wall=f.wall) == 1
    assert capsys.readouterr().out.splitlines()[0] == f"{words.PLAIN} the fast tests failed"
    assert f.restarts == [] and where(tree) == ("main", ids[0]) and (tree / "f.txt").read_text() == "1\n"
    assert not (state / "ledger.sqlite").exists() and len(f.checks) == 3
    assert deploy.check_step(EXAMPLE_PRIVATE, state, sd=f, wall=f.wall) == 1 and len(f.checks) == 3   # the example layer: refused
    assert capsys.readouterr().out.splitlines() == [f"{words.PLAIN} no private folder: pass --private"]
    from yerk_ai import cli
    got = []
    monkeypatch.setattr(deploy, "check_step", lambda p, s: got.append((p, s)) or 0)
    assert cli.main(["deploy", "--check", "--private", str(priv), "--state", str(state)]) == 0 and got == [(priv, state)]
