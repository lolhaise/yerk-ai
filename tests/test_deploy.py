"""Checks `yerk deploy`, which moves the running yerkd onto new code, restarts it at a quiet moment and rolls back if
the move onto units/yerkd.service, READY=1 only once the port listens, the stall row's hold, dead
card), the move onto units/yerkd.service, READY=1 only once the port listens, the stall row's hold, dead turns found
from the message, the card that cuts the name and never the fact, job alerts to the machine room, and the upgrade drill.
systemd, the channel and the daemon's /status are fakes; git is a real repository in tmp_path. Layer 7, run 2."""
import asyncio
import json
import logging
import os
import shutil
import stat
import subprocess
import threading
import time
from types import SimpleNamespace

from yerk_ai import beat, cli, deploy, jobs, morning, probes, status, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.ledger import Ledger
from yerk_ai.room import Inbound

NOW = 1792303200.0
NAP = 0.01


def nap(_s):
    time.sleep(NAP)


def repo(tmp_path):
    """Three commits in a row, the tree checked out detached at the first."""
    t = tmp_path / "tree"
    t.mkdir()

    def g(*a):
        return subprocess.run(["git", "-C", str(t), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q")
    ids = []
    for i in range(3):
        (t / "f.txt").write_text(f"{i}\n")
        g("add", "f.txt")
        g("-c", "user.email=nobody", "-c", "user.name=t", "commit", "-qm", f"c{i}")
        ids.append(g("rev-parse", "HEAD"))
    g("checkout", "-q", "--detach", ids[0])
    return t, ids


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
    assert subprocess.run(["git", "-C", str(tree), "symbolic-ref", "-q", "HEAD"]).returncode == 1  # detached: no branch moved


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
    shown = {"WorkingDirectory": "/code", "Environment": "YERK_PRIVATE=/p PATH=/v/bin:/usr/bin"}

    class Unit(deploy.Systemd):
        def argv(self):
            return ["/v/bin/python", "-m", "yerk_ai.daemon", "--state", "/s"]

        def ctl(self, *args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, shown.get(args[2], "") if args[0] == "show" else "", "")
    u = Unit("yerkd-fake")
    was = u.move(deploy.REPO, tmp_path / "units")
    path = tmp_path / "units" / "yerkd-fake.service"
    text = path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and "Type=notify" in text and "WatchdogSec=30" in text
    assert "MemoryMax=" in text and "WorkingDirectory=/code\n" in text and "Environment=YERK_PRIVATE=/p\n" in text
    assert "ExecStart=/v/bin/python -m yerk_ai.daemon --state /s\n" in text and "YERK_STATE=%h" not in text
    assert [c[0] for c in calls if c[0] != "show"] == ["stop", "daemon-reload", "start"]
    ran = []
    monkeypatch.setattr(deploy.subprocess, "run", lambda cmd, **k: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    assert u.back(was) and not path.exists() and (tmp_path / "units" / "yerkd-fake.service.failed").exists()
    assert ran[0][:3] == ["systemd-run", "--user", "--unit=yerkd-fake"] and ran[0][-5:] == \
        ["/v/bin/python", "-m", "yerk_ai.daemon", "--state", "/s"] and "--setenv=PATH=/v/bin:/usr/bin" in ran[0]


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
