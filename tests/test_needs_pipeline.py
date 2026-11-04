"""Checks the needs pipeline (pipeline.py), which takes an ability a room filed as missing from build to live with no person. A filed
need goes built, checked, read cold, merged, deployed and live, one need at a time and the rest queued; [needs] auto off is the hand
road. GitHub is a fake `github.api` (routes to answers, every call recorded), the deploy unit a fake systemd-run, the workers the fake
real token file, GitHub or systemd. Every value here is a placeholder (decision 22)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest
from test_needs import ASK, FLYER, listed, need, ops
from test_pictures import Rec

from yerk_ai import cli, git_askpass, github, needs, words
from yerk_ai.config import EXAMPLE_PRIVATE, REPO, head

SLUG, TOK = "example-owner/example-repo", "tok-placeholder-0000"
GREEN = [{"context": f"ci/{c}", "state": "success", "description": "512 passed"} for c in "abcdefg"]


class Hub:
    """GitHub as a dict of (method, path) to (status, answer); every call kept, with its body."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method, path, tok, body=None):
        assert tok == TOK
        self.calls.append((method, path, body))
        return self.routes.get((method, path), (404, {"message": "Not Found"}))


class Deploys:
    """systemd-run for the deploy unit, recorded, nothing run."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.busy: set[str] = set()

    def start(self, argv):
        self.argv.append(argv)
        return ""

    def running(self):
        return set(self.busy)


@pytest.fixture
def pipe(tmp_path, monkeypatch):
    y, rec = ops(tmp_path, "example-room", "her-room")
    y.conf["needs"] = {"repo": SLUG}                      # auto on, as by default
    hub, units = Hub(), Deploys()
    monkeypatch.setattr(github, "api", hub)
    monkeypatch.setattr(github, "token", lambda private: TOK)
    y.pipeline.units = units
    y.pipeline.tree = tmp_path / "no-checkout"            # a need going live never deletes a branch in the checkout under test
    rooms = {r: Rec() for r in ("example-room", "her-room")}
    y.channels.update(rooms)
    return y, rec, hub, units, rooms


def end(y, wid: str, first: str, ok: bool = True) -> None:
    d = Path(y.ledger.worker_get(wid)["staging"])
    (d / "REPORT.md").write_text(first + "\n")
    (d / "exit.json").write_text(json.dumps({"result": "success" if ok else "exit-code", "code": "exited", "status": "0" if ok else "1"}))
    asyncio.run(y.workers.complete(wid))


def row(y, day: str, n: int = 1) -> dict:
    return needs.find(needs.load(y.state), day, n)


def role(y, wid: str) -> dict:
    return json.loads((Path(y.ledger.worker_get(wid)["staging"]) / "worker.json").read_text())["need"]


def to_pr(y, hub, day: str, sha: str = "a" * 40) -> dict:
    """Need 1 built on its branch, and its PR 7 seen at `sha`."""
    end(y, row(y, day)["worker"], f"branch need-{day}-1 at 1234567: it reads pictures")
    assert row(y, day)["state"] == "built"
    hub.routes[("GET", f"/repos/{SLUG}/pulls?head=example-owner:need-{day}-1&state=open")] = (200, [{"number": 7, "head": {"sha": sha}}])
    hub.routes[("GET", f"/repos/{SLUG}/pulls/7")] = (200, {"head": {"sha": sha}, "merged": False, "state": "open"})
    asyncio.run(y.pipeline.tick())
    return row(y, day)


def test_auto_starts_a_builder_queues_the_next_and_starts_it_once_the_first_is_live(pipe):
    y, rec, hub, units, rooms = pipe
    day = need(y, "example-room", *ASK)["day"]
    assert row(y, day)["state"] == "building" and len(y.workers.launcher.calls) == 1
    assert rec.lines[0] == words.NEED_STARTS.format(n=1, room="example-room", ability=ASK[2])
    assert need(y, "her-room", "post a reel", "no upload tool", "upload a video")["n"] == 2
    assert row(y, day, 2)["state"] == "queued" and len(y.workers.launcher.calls) == 1
    assert rec.lines[-1] == words.NEED_QUEUED.format(n=2, room="her-room", ability="upload a video", behind=1)
    assert need(y, "her-room", *FLYER)["folded"] and len(y.workers.launcher.calls) == 1        # folded: no builder, no queue
    sha = head(REPO)
    y.ledger.deploy(kind="deploy", unit="yerkd", before="0" * 7, after=sha, outcome="proven")
    asyncio.run(y.pipeline.set(row(y, day), {"state": "merged", "pr": 7, "merged_at": time.time() - 60, "merge": sha}))
    asyncio.run(y.pipeline.tick())
    assert row(y, day)["state"] == "live" and row(y, day, 2)["state"] == "building" and row(y, day, 2)["worker"]
    assert units.argv == []                              # a proven deploy already holds the merge: no second deploy


def test_auto_off_starts_nothing_and_the_line_carries_the_command(pipe):
    y, rec, hub, units, rooms = pipe
    y.conf["needs"]["auto"] = False
    day = need(y, "example-room", *ASK)["day"]
    assert row(y, day)["state"] == "filed" and y.workers.launcher.calls == []
    assert rec.lines == [words.NEED_FILED.format(n=1, room="example-room", ability=ASK[2], day=day)]
    asyncio.run(y.pipeline.tick())
    assert row(y, day)["state"] == "filed" and hub.calls == []


def test_a_need_filed_before_the_pipeline_is_queued_on_a_tick_one_whose_build_came_back_is_not(pipe):
    y, rec, hub, units, rooms = pipe
    y.conf["needs"]["auto"] = False                      # before the pipeline: filed by the hand road, one built by hand and back
    day = need(y, "example-room", *ASK)["day"]
    need(y, "her-room", "post a reel", "no upload tool", "upload a video")
    end(y, asyncio.run(y.need_build({"day": day, "n": 2}))["id"], "no branch: the suite would not run", ok=False)
    need(y, "her-room", "what does this voice memo say", "no audio tool", "hear a voice memo")
    asyncio.run(y.pipeline.set(row(y, day, 3), {"state": "building", "worker": "w1"}))        # one in flight
    y.conf["needs"]["auto"] = True
    asyncio.run(y.pipeline.tick())
    asyncio.run(y.pipeline.tick())
    picked = words.NEED_PICKED_UP.format(n=1, day=day, room="example-room", ability=ASK[2])
    assert row(y, day)["state"] == "queued" and rec.lines.count(picked) == 1 and len(y.workers.launcher.calls) == 1
    assert row(y, day, 2)["state"] == "filed" and row(y, day, 2)["last"]           # its build came back: a person's call
    asyncio.run(y.pipeline.stop(row(y, day, 3), "held", "a reason"))               # none in flight now: the queued one starts
    assert row(y, day)["state"] == "building" and row(y, day)["worker"] and len(y.workers.launcher.calls) == 2
    assert rec.lines[-1] == words.NEED_STARTS.format(n=1, room="example-room", ability=ASK[2]) and row(y, day, 2)["state"] == "filed"


def test_built_pr_checks_pending_then_green_start_a_cold_read_and_its_merge_uses_the_heads_sha(pipe):
    y, rec, hub, units, rooms = pipe
    day = need(y, "example-room", *ASK)["day"]
    builder = row(y, day)["worker"]
    r = to_pr(y, hub, day)
    assert (r["state"], r["pr"], r["head"]) == ("pr", 7, "a" * 40) and rec.lines[-1] == words.NEED_SEEN.format(n=1, pr=7)
    status = ("GET", f"/repos/{SLUG}/commits/{'a' * 40}/status")
    hub.routes[status] = (200, {"state": "success", "statuses": GREEN[:3]})            # three of seven in: wait
    asyncio.run(y.pipeline.tick())
    assert row(y, day)["state"] == "pr" and needs.said(row(y, day)) == "PR 7, checks 3 of 3 passed"
    hub.routes[status] = (200, {"state": "success", "statuses": GREEN})
    asyncio.run(y.pipeline.tick())
    r = row(y, day)
    assert r["state"] == "reading" and r["worker"] != builder and role(y, r["worker"])["role"] == "read"
    brief = (Path(y.ledger.worker_get(r["worker"])["staging"]) / "BRIEF.md").read_text()
    assert "exactly one of `merge`, `fix first: <why>`, `unsure: <why>`" in brief and "PR 7" in brief
    hub.routes[("PUT", f"/repos/{SLUG}/pulls/7/merge")] = (200, {"merged": True, "sha": "c" * 40})
    end(y, r["worker"], "merge")
    assert hub.calls[-1] == ("PUT", f"/repos/{SLUG}/pulls/7/merge", {"sha": "a" * 40, "merge_method": "merge"})
    assert (row(y, day)["state"], row(y, day)["merge"]) == ("merged", "c" * 40) and rec.lines[-1] == words.NEED_MERGED.format(n=1, pr=7)


def test_red_checks_start_fix_workers_up_to_the_cap_then_held_with_the_contexts_named(pipe):
    y, rec, hub, units, rooms = pipe
    day = need(y, "example-room", *ASK)["day"]
    to_pr(y, hub, day)
    for k, sha in ((1, "a" * 40), (2, "b" * 40), (3, "d" * 40)):
        hub.routes[("GET", f"/repos/{SLUG}/pulls/7")] = (200, {"head": {"sha": sha}, "merged": False, "state": "open"})
        hub.routes[("GET", f"/repos/{SLUG}/commits/{sha}/status")] = (200, {"state": "failure", "statuses": [
            *GREEN[:6], {"context": "ci/py3.14", "state": "failure", "description": "tests/test_x.py::test_y failed"}]})
        asyncio.run(y.pipeline.tick())
        r = row(y, day)
        if k == 3:
            break
        assert (r["state"], r["fixes"]) == ("fixing", k) and role(y, r["worker"])["role"] == "fix"
        assert needs.said(r) == f"fixing PR 7, fix {k} of 2, {r['worker']}"
        assert "ci/py3.14: failure (tests/test_x.py::test_y failed)" in (Path(y.ledger.worker_get(r["worker"])["staging"]) / "BRIEF.md").read_text()
        end(y, r["worker"], f"fixed: {sha[:7]}")
        assert row(y, day)["state"] == "pr"
    assert (r["state"], r["why"]) == ("held", "checks red after 2 fixes: ci/py3.14")
    assert rec.lines[-1] == words.NEED_HELD.format(n=1, ability=ASK[2], why="checks red after 2 fixes: ci/py3.14")


def test_checks_that_never_come_are_stuck_and_a_new_head_restarts_the_clock(pipe):
    y, rec, hub, units, rooms = pipe
    day = need(y, "example-room", *ASK)["day"]
    to_pr(y, hub, day)
    hub.routes[("GET", f"/repos/{SLUG}/commits/{'a' * 40}/status")] = (200, {"state": "pending", "statuses": GREEN[:2]})
    y.pipeline.now = lambda: time.time() + 39 * 60
    asyncio.run(y.pipeline.tick())
    assert row(y, day)["state"] == "pr"
    y.pipeline.now = lambda: time.time() + 41 * 60
    asyncio.run(y.pipeline.tick())
    assert row(y, day)["state"] == "stuck" and row(y, day)["why"].startswith("checks never came: 2 of 7 in ")


def test_the_cold_read_saying_fix_first_or_unsure(pipe):
    y, rec, hub, units, rooms = pipe
    day = need(y, "example-room", *ASK)["day"]
    to_pr(y, hub, day)
    hub.routes[("GET", f"/repos/{SLUG}/commits/{'a' * 40}/status")] = (200, {"state": "success", "statuses": GREEN})
    asyncio.run(y.pipeline.tick())
    end(y, row(y, day)["worker"], "fix first: the new tool has no test for an empty post")
    r = row(y, day)
    assert (r["state"], r["fixes"]) == ("fixing", 1) and "the new tool has no test for an empty post" in (
        Path(y.ledger.worker_get(r["worker"])["staging"]) / "BRIEF.md").read_text()
    end(y, r["worker"], "fixed: abc1234")
    asyncio.run(y.pipeline.tick())                       # the same head, still green: a cold read again
    assert row(y, day)["state"] == "reading"
    end(y, row(y, day)["worker"], "unsure: it changes the gates")
    assert (row(y, day)["state"], row(y, day)["why"]) == ("held", "the cold read was unsure: it changes the gates")


def test_merged_starts_one_deploy_unit_with_the_askpass_and_no_token_in_its_argv(pipe):
    y, rec, hub, units, rooms = pipe
    day = need(y, "example-room", *ASK)["day"]
    units.busy = {"yerk-deploy-0a0a0a"}                  # a deploy running: none starts
    asyncio.run(y.pipeline.set(row(y, day), {"state": "merged", "pr": 7, "merged_at": time.time(), "merge": "c" * 40}))
    asyncio.run(y.pipeline.tick())
    assert units.argv == [] and row(y, day)["state"] == "merged"
    units.busy = set()
    asyncio.run(y.pipeline.tick())
    asyncio.run(y.pipeline.tick())
    assert len(units.argv) == 1 and row(y, day)["state"] == "deploying" and rec.lines[-1] == words.NEED_DEPLOYING.format(n=1, pr=7)
    argv = units.argv[0]
    assert argv[:4] == ["systemd-run", "--user", "--unit", row(y, day)["unit"]] and row(y, day)["unit"].startswith("yerk-deploy-")
    assert f"--setenv=GIT_ASKPASS={y.state / 'askpass'}" in argv and "--setenv=GIT_TERMINAL_PROMPT=0" in argv
    assert f"--setenv=YERK_GITHUB_TOKEN_FILE={y.private / github.TOKEN}" in argv and not any(TOK in a for a in argv)
    assert argv[-6:] == ["deploy", "--private", str(y.private), "--state", str(y.state), "origin/main"] and argv[-7].endswith("/bin/yerk")
    assert argv[-12:-7] == ["--", "/bin/sh", "-c", 'git fetch --quiet origin && exec "$@"', "yerk-deploy"]
    assert "-m yerk_ai.git_askpass" in (y.state / "askpass").read_text() and TOK not in (y.state / "askpass").read_text()


def test_a_proven_deploy_holding_the_merge_is_live_one_line_a_room_and_a_failed_one_is_held(pipe):
    y, rec, hub, units, rooms = pipe
    day = need(y, "example-room", *ASK)["day"]
    need(y, "her-room", *FLYER)
    need(y, "example-room", "what is in this other picture", "web_fetch again", "see a picture in a post")
    sha = head(REPO)
    asyncio.run(y.pipeline.set(row(y, day), {"state": "deploying", "pr": 7, "merged_at": time.time() - 60, "merge": sha,
                                             "deploy_at": time.time() - 30, "unit": "yerk-deploy-0b0b0b"}))
    y.ledger.deploy(kind="deploy", unit="yerkd", before="0" * 7, after=sha, outcome="proven")
    asyncio.run(y.pipeline.tick())
    asyncio.run(y.pipeline.tick())
    assert row(y, day)["state"] == "live" and rec.lines.count(words.NEED_LIVE.format(n=1, ability=ASK[2])) == 1
    for room in ("example-room", "her-room"):
        assert rooms[room].lines == [words.NEED_LIVE_ROOM.format(n=1, ability=ASK[2])]
    n2 = need(y, "her-room", "post a reel", "no upload tool", "upload a video")["n"]
    asyncio.run(y.pipeline.set(row(y, day, n2), {"state": "deploying", "pr": 8, "merged_at": time.time() - 60, "merge": "e" * 40,
                                                 "deploy_at": time.time() - 30, "unit": "yerk-deploy-0c0c0c"}))
    y.ledger.deploy(kind="deploy", unit="yerkd", before=sha, after=sha, outcome="tests failed", note="the fast tests failed")
    asyncio.run(y.pipeline.tick())
    assert (row(y, day, n2)["state"], row(y, day, n2)["why"]) == ("held", "deploy failed: the fast tests failed")


def test_no_token_skips_github_says_so_once_and_done_still_closes_it_by_hand(pipe, monkeypatch, capsys):
    y, rec, hub, units, rooms = pipe
    monkeypatch.setattr(github, "token", lambda private: "")
    day = need(y, "example-room", *ASK)["day"]
    end(y, row(y, day)["worker"], f"branch need-{day}-1 at 1234567: it reads pictures")
    asyncio.run(y.pipeline.tick())
    asyncio.run(y.pipeline.tick())
    assert hub.calls == [] and rec.lines.count(words.NEED_NO_TOKEN.format(n=1)) == 1 and row(y, day)["state"] == "built"
    assert cli.main(["needs", "done", day, "1", "7", "--state", str(y.state), "--private", str(EXAMPLE_PRIVATE)]) == 0
    assert capsys.readouterr().out.strip() == f"need 1 of {day}: merged, PR 7"
    asyncio.run(y.pipeline.tick())
    assert units.argv == [] and row(y, day)["state"] == "merged"        # no token, no fetch: a person deploys, live follows


ID = ("-c", "user.name=Example Builder", "-c", "user.email=builder-placeholder", "-c", "commit.gpgsign=false")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def attach(y, tmp_path: Path) -> Path:
    """A plug-in kept in a local git repository of its own, on main, that example-room runs as an mcp server, beside Yerk's
    own web plug-in (in Yerk's checkout, so never a plug-in repository of its own)."""
    p = tmp_path / "example-plugin"
    p.mkdir()
    git(p, "init", "-q", "-b", "main")
    (p / "server.mjs").write_text("export const gate = 'the address';\n")
    git(p, "add", ".")
    git(p, *ID, "commit", "-q", "-m", "baseline")
    y.room_confs["example-room"]["mcp"] = {
        "example-plugin": {"type": "stdio", "command": "/usr/bin/node", "args": [str(p / "server.mjs")]},
        "web": {"type": "stdio", "command": "/usr/bin/node", "args": [str(REPO / "plugins" / "web" / "server.mjs")]}}
    return p


def commit_on(p: Path, wt: Path, branch: str, text: str) -> str:
    """A worker's commit on `branch` in the plug-in's repository, in a worktree of its own: the checkout the rooms run untouched."""
    if not wt.exists():
        git(p, "worktree", "add", "-q", str(wt), *(["-b", branch, "main"] if not git(p, "branch", "--list", branch) else [branch]))
    (wt / "server.mjs").write_text(text)
    git(wt, *ID, "commit", "-q", "-am", "the page decides")
    return git(wt, "rev-parse", "HEAD")


def staged(y, wid: str) -> str:
    return (Path(y.ledger.worker_get(wid)["staging"]) / "BRIEF.md").read_text()


def test_a_plugin_kept_in_its_own_git_repository_is_named_in_the_builders_brief_from_the_rooms_mcp_tables(pipe, tmp_path):
    """need 36 (2026-11-02): a browser gate in a plug-in's own repository came back twice, since the builder only ever edited
    Yerk's repo and described the rest as out of reach; the brief now names each add-on whose code is a git repository of
    its own, where the fix is built on a branch of the need's name. Yerk's own web plug-in is not one."""
    y, rec, hub, units, rooms = pipe
    p = attach(y, tmp_path)
    assert needs.plugins(y.room_confs, REPO) == {"example-plugin": {"repo": str(p.resolve()), "base": "main"}}
    day = need(y, "example-room", *ASK)["day"]
    brief = staged(y, row(y, day)["worker"])
    assert "\n## Plug-ins\n" in brief and f"`example-plugin` at {p.resolve()} (its rooms run its main)" in brief
    assert f"worktree add <your folder>/<plug-in>-wt -b need-{day}-1 <its base>`" in brief and "\N{EM DASH}" not in brief
    assert "never described in abilities.toml as out of reach" in brief and brief.index("## Plug-ins") < brief.index("## What done")
    assert "## Plug-ins" not in needs.brief(row(y, day), REPO)                     # no plug-in repository: the brief as before


def test_a_fix_built_in_a_plugins_repository_rides_the_pr_through_the_fix_and_the_read_and_the_merge_lands_it(pipe, tmp_path):
    """The wall itself: the fix lives in the plug-in (its gate read the page's address alone). The builder's branch there is
    found when it ends, a fix worker is pointed at it, the cold read reads its newest head, and the merge fast-forwards the
    plug-in's main, the checkout its rooms run, to exactly that head."""
    y, rec, hub, units, rooms = pipe
    p, wt, branch = attach(y, tmp_path), tmp_path / "worker-plugin-wt", f"need-{need(y, 'example-room', *ASK)['day']}-1"
    day = branch[5:-2]
    sha = commit_on(p, wt, branch, "export const gate = 'the page marks';\n")
    r = to_pr(y, hub, day)
    assert r["plugins"] == {"example-plugin": {"repo": str(p.resolve()), "base": "main", "branch": branch, "head": sha}}
    assert words.NEED_BRANCH.format(n=1, branch=branch, ability=ASK[2]) + words.NEED_PLUGIN.format(names="example-plugin") in rec.lines
    assert needs.said(r) == f"PR 7, and the example-plugin plug-in's branch at {sha[:7]}"
    hub.routes[("GET", f"/repos/{SLUG}/commits/{'a' * 40}/status")] = (200, {"state": "success", "statuses": GREEN})
    asyncio.run(y.pipeline.tick())
    end(y, row(y, day)["worker"], "fix first: the gate still calls a look-alike a payment page")
    fixer = row(y, day)["worker"]
    assert f"`example-plugin` at {p.resolve()} (its rooms run its main; its branch `{branch}` is at {sha[:7]}, so build on it)" in staged(y, fixer)
    sha2 = commit_on(p, wt, branch, "export const gate = 'the page marks, a look-alike on confirmed';\n")
    end(y, fixer, f"fixed: {sha2[:7]}")
    asyncio.run(y.pipeline.tick())                       # the same Yerk head, still green: read again, at the plug-in's new head
    r = row(y, day)
    assert r["state"] == "reading" and r["plugins"]["example-plugin"]["head"] == sha2
    read = staged(y, r["worker"])
    assert f"`git -C {p.resolve()} diff main...{sha2}`" in read and "fast-forwards its main to that sha" in read
    hub.routes[("PUT", f"/repos/{SLUG}/pulls/7/merge")] = (200, {"merged": True, "sha": "c" * 40})
    end(y, r["worker"], "merge")
    assert hub.calls[-1] == ("PUT", f"/repos/{SLUG}/pulls/7/merge", {"sha": "a" * 40, "merge_method": "merge"})
    assert git(p, "rev-parse", "main") == sha2 and "a look-alike on confirmed" in (p / "server.mjs").read_text()
    assert row(y, day)["state"] == "merged" and rec.lines[-1] == words.NEED_MERGED.format(n=1, pr=7) + words.NEED_LANDED.format(
        names="example-plugin")


def test_a_plugin_branch_that_cannot_land_holds_the_need_and_nothing_is_merged(pipe, tmp_path):
    y, rec, hub, units, rooms = pipe
    p, wt = attach(y, tmp_path), tmp_path / "worker-plugin-wt"
    day = need(y, "example-room", *ASK)["day"]
    sha = commit_on(p, wt, f"need-{day}-1", "export const gate = 'the page marks';\n")
    to_pr(y, hub, day)
    plug = row(y, day)["plugins"]["example-plugin"]
    (p / "server.mjs").write_text("a change by hand\n")                         # each why in words, nothing landed
    assert needs.land("example-plugin", plug, dry=True) == "the example-plugin plug-in did not land: its checkout has changes not committed"
    git(p, "checkout", "-q", "--", "server.mjs")
    assert needs.land("example-plugin", {**plug, "head": "0" * 40}) == (
        f"the example-plugin plug-in did not land: its branch need-{day}-1 moved off 0000000")
    assert needs.land("example-plugin", plug, dry=True) == "" and git(p, "rev-parse", "main") != sha
    (p / "notes.md").write_text("main moved on\n")                               # its main moved after the branch was made
    git(p, "add", "notes.md")
    git(p, *ID, "commit", "-q", "-m", "a commit on main")
    hub.routes[("GET", f"/repos/{SLUG}/commits/{'a' * 40}/status")] = (200, {"state": "success", "statuses": GREEN})
    hub.routes[("PUT", f"/repos/{SLUG}/pulls/7/merge")] = (200, {"merged": True, "sha": "c" * 40})
    asyncio.run(y.pipeline.tick())
    end(y, row(y, day)["worker"], "merge")
    why = f"the cold read said merge; the example-plugin plug-in did not land: its main moved since need-{day}-1 was made, so it is no fast-forward"
    assert (row(y, day)["state"], row(y, day)["why"]) == ("held", why) and not [c for c in hub.calls if c[0] == "PUT"]
    assert rec.lines[-1] == words.NEED_HELD.format(n=1, ability=ASK[2], why=why) and not git(p, "branch", "--contains", sha, "main")


def test_done_by_hand_lands_the_needs_plugin_branch_too(pipe, tmp_path, monkeypatch, capsys):
    y, rec, hub, units, rooms = pipe
    monkeypatch.setattr(github, "token", lambda private: "")
    p = attach(y, tmp_path)
    day = need(y, "example-room", *ASK)["day"]
    sha = commit_on(p, tmp_path / "worker-plugin-wt", f"need-{day}-1", "export const gate = 'the page marks';\n")
    end(y, row(y, day)["worker"], f"branch need-{day}-1 at 1234567: it reads pictures")
    capsys.readouterr()
    assert cli.main(["needs", "done", day, "1", "7", "--state", str(y.state), "--private", str(EXAMPLE_PRIVATE)]) == 0
    assert capsys.readouterr().out == f"need 1 of {day}: merged, PR 7\nthe example-plugin plug-in landed\n"
    assert git(p, "rev-parse", "main") == sha and "the page marks" in (p / "server.mjs").read_text()


def shipped(y, tmp_path: Path, branch: str, merged: bool = True) -> Path:
    """The checkout a need's merge leaves: on main, the need's branch there with a finished worker's worktree still on it,
    merged as GitHub merges, by a merge commit (or, `merged` False, not)."""
    tree = tmp_path / "checkout"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    (tree / "server.mjs").write_text("baseline\n")
    git(tree, "add", ".")
    git(tree, *ID, "commit", "-q", "-m", "baseline")
    commit_on(tree, tmp_path / "worker-wt", branch, "the need\n")
    if merged:
        git(tree, *ID, "merge", "-q", "--no-ff", "--no-edit", branch)
    y.pipeline.tree = tree
    return tree


def test_live_drops_the_needs_branch_here_then_on_github_and_frees_the_worktrees_its_workers_left_on_it(pipe, tmp_path, monkeypatch):
    y, rec, hub, units, rooms = pipe
    day = need(y, "example-room", *ASK)["day"]
    branch, wt, wt2 = f"need-{day}-1", tmp_path / "worker-wt", tmp_path / "fixer-wt"
    tree = shipped(y, tmp_path, branch)
    git(tree, "worktree", "add", "-q", "--force", str(wt2), branch)    # a fix worker on the same branch: git takes it with --force
    git(tree, "worktree", "add", "-q", str(tmp_path / "next-wt"), "-b", f"{branch}0", "main")    # the next need's builder, a name this one starts
    sha, route, here = git(tree, "rev-parse", branch), f"/repos/{SLUG}/git/refs/heads/{branch}", []
    hub.routes[("DELETE", route)] = (204, None)

    def spy(method, path, tok, body=None):
        here.append(git(tree, "branch", "--list", branch))             # what the checkout has when GitHub is called
        return hub(method, path, tok, body)
    monkeypatch.setattr(github, "api", spy)
    asyncio.run(y.pipeline.live(row(y, day)))
    assert here == [""] and hub.calls == [("DELETE", route, None)]     # gone here first, and GitHub's once
    assert row(y, day)["state"] == "live" and rec.lines[-1] == words.NEED_LIVE.format(n=1, ability=ASK[2])
    for w in (wt, wt2):                                                # each detached where it stood, its files as they were
        assert git(w, "rev-parse", "HEAD") == sha and (w / "server.mjs").read_text() == "the need\n"
        assert subprocess.run(["git", "-C", str(w), "symbolic-ref", "-q", "HEAD"]).returncode == 1
    assert git(tmp_path / "next-wt", "symbolic-ref", "--short", "HEAD") == f"{branch}0"


def test_a_branch_head_does_not_hold_stays_with_its_worktree_and_github_is_not_called(pipe, tmp_path, caplog):
    y, rec, hub, units, rooms = pipe
    caplog.set_level("INFO", logger="yerk.needs")
    day = need(y, "example-room", *ASK)["day"]
    branch = f"need-{day}-1"
    tree = shipped(y, tmp_path, branch, merged=False)
    asyncio.run(y.pipeline.live(row(y, day)))
    assert git(tree, "branch", "--list", branch) and git(tmp_path / "worker-wt", "symbolic-ref", "--short", "HEAD") == branch
    assert f"needs: branch {branch} stays: its HEAD does not hold it" in caplog.messages and hub.calls == []
    assert row(y, day)["state"] == "live" and rec.lines[-1] == words.NEED_LIVE.format(n=1, ability=ASK[2])


def test_git_refusing_the_delete_keeps_githubs_too_and_the_main_checkout_is_never_detached(pipe, tmp_path, caplog):
    y, rec, hub, units, rooms = pipe
    caplog.set_level("INFO", logger="yerk.needs")
    day = need(y, "example-room", *ASK)["day"]
    branch = f"need-{day}-1"
    tree = shipped(y, tmp_path, branch)
    git(tree, "checkout", "-q", "--ignore-other-worktrees", branch)     # the checkout itself on the need's branch: -d refuses
    asyncio.run(y.pipeline.live(row(y, day)))
    assert git(tree, "symbolic-ref", "--short", "HEAD") == branch and git(tree, "branch", "--list", branch) and hub.calls == []
    assert any(m.startswith(f"needs: branch {branch} stays: ") for m in caplog.messages) and row(y, day)["state"] == "live"


def test_a_plugin_branch_goes_in_its_own_repository_and_never_on_github(pipe, tmp_path, caplog):
    y, rec, hub, units, rooms = pipe
    caplog.set_level("INFO", logger="yerk.needs")
    p = attach(y, tmp_path)
    day = need(y, "example-room", *ASK)["day"]
    branch = f"need-{day}-1"
    sha = commit_on(p, tmp_path / "worker-plugin-wt", branch, "export const gate = 'the page marks';\n")
    plugs = asyncio.run(y.pipeline.heads(row(y, day)))
    assert needs.land("example-plugin", plugs["example-plugin"]) == ""
    asyncio.run(y.pipeline.set(row(y, day), {"plugins": plugs}))
    tree = shipped(y, tmp_path, branch)
    asyncio.run(y.pipeline.live(row(y, day)))
    assert not git(p, "branch", "--list", branch) and git(p, "rev-parse", "main") == sha and not git(tree, "branch", "--list", branch)
    assert hub.calls == [("DELETE", f"/repos/{SLUG}/git/refs/heads/{branch}", None)]       # Yerk's own alone
    assert f"needs: branch {branch} on GitHub: GitHub said 404: Not Found" in caplog.messages and row(y, day)["state"] == "live"


def test_the_hand_road_reaches_the_same_drop_and_with_no_token_github_is_never_called(pipe, tmp_path, monkeypatch, caplog):
    y, rec, hub, units, rooms = pipe
    caplog.set_level("INFO", logger="yerk.needs")
    monkeypatch.setattr(github, "token", lambda private: "")
    day = need(y, "example-room", *ASK)["day"]
    tree = shipped(y, tmp_path, f"need-{day}-1")
    end(y, row(y, day)["worker"], f"branch need-{day}-1 at 1234567: it reads pictures")
    assert cli.main(["needs", "done", day, "1", "7", "--state", str(y.state), "--private", str(EXAMPLE_PRIVATE)]) == 0
    sha = head(REPO)
    asyncio.run(y.pipeline.set(row(y, day), {"merge": sha}))
    y.ledger.deploy(kind="deploy", unit="yerkd", before="0" * 7, after=sha, outcome="proven")
    asyncio.run(y.pipeline.tick())
    assert row(y, day)["state"] == "live" and not git(tree, "branch", "--list", f"need-{day}-1") and hub.calls == []
    assert not [m for m in caplog.messages if "branches:" in m]         # the fake GitHub refuses an empty token: never asked, no error


def test_an_error_in_the_drop_is_one_log_line_and_the_need_is_live_with_its_lines(pipe, tmp_path, monkeypatch, caplog):
    y, rec, hub, units, rooms = pipe
    caplog.set_level("INFO", logger="yerk.needs")
    day = need(y, "example-room", *ASK)["day"]
    shipped(y, tmp_path, f"need-{day}-1")
    monkeypatch.setattr(github, "api", lambda *a, **k: 1 / 0)
    asyncio.run(y.pipeline.live(row(y, day)))
    assert [m for m in caplog.messages if "branches:" in m] == ["needs: need 1's branches: ZeroDivisionError('division by zero')"]
    assert row(y, day)["state"] == "live" and rec.lines[-1] == words.NEED_LIVE.format(n=1, ability=ASK[2])
    assert rooms["example-room"].lines == [words.NEED_LIVE_ROOM.format(n=1, ability=ASK[2])]


def test_the_list_words_every_state(tmp_path, capsys):
    base = {"day": "2026-10-31", "ability": "see a picture", "asks": [{}], "pr": 7, "worker": "w1", "branch": "need-x",
            "fixes": 1, "fix_cap": 2, "why": "a reason"}
    said = {s: needs.said({**base, "n": i, "state": s}) for i, s in enumerate(
        ["filed", "queued", "building", "built", "pr", "fixing", "reading", "held", "stuck", "merged", "deploying", "live"], 1)}
    assert said == {"filed": "filed", "queued": "queued, it starts when the need in flight is live, held or stuck",
                    "building": "building w1", "built": "built on branch need-x", "pr": "PR 7", "fixing": "fixing PR 7, fix 1 of 2, w1",
                    "reading": "cold read of PR 7, w1", "held": "held: a reason", "stuck": "stuck: a reason", "merged": "merged, PR 7",
                    "deploying": "merged, PR 7, deploying", "live": "live, PR 7"}
    (tmp_path / needs.STORE).write_text(json.dumps([{**base, "n": i, "state": s} for i, s in enumerate(said, 1)]))
    assert [x.split(": see")[0].split(" ", 2)[2] for x in listed(tmp_path, capsys)] == [
        *[v for s, v in said.items() if s not in ("merged", "live")], said["merged"], said["live"]]


def test_the_askpass_answers_the_username_and_the_password_and_nothing_else(tmp_path, monkeypatch, capsys):
    f = tmp_path / "token"
    f.write_text(TOK + "\n")
    assert git_askpass.answer("Username for 'https://github.com': ", str(f)) == "x-access-token"
    assert git_askpass.answer("Password for 'https://github.com': ", str(f)) == TOK
    assert git_askpass.answer("Are you sure you want to continue connecting (yes/no)? ", str(f)) == ""
    assert git_askpass.answer("Password for 'https://github.com': ", str(tmp_path / "missing")) == ""
    monkeypatch.setenv("YERK_GITHUB_TOKEN_FILE", str(f))
    assert git_askpass.main(["Password for 'https://github.com': "]) == 0 and capsys.readouterr().out == TOK + "\n"
    assert git_askpass.main(["Host key? "]) == 1 and capsys.readouterr().out == ""


def test_github_reads_its_answers_and_says_a_refused_merge_in_words(monkeypatch):
    hub = Hub()
    monkeypatch.setattr(github, "api", hub)
    hub.routes[("GET", f"/repos/{SLUG}/pulls?head=example-owner:b&state=open")] = (200, [])
    assert github.pr_for(SLUG, "b", TOK) == {"ok": True, "number": 0}
    hub.routes[("PUT", f"/repos/{SLUG}/pulls/7/merge")] = (409, {"message": "Head branch was modified. Review and try the merge again."})
    assert github.merge(SLUG, 7, "a" * 40, TOK) == {"ok": False, "code": 409, "error": "GitHub said 409: Head branch was modified. Review and try the merge again."}
    assert github.slug(REPO, "o/n") == "o/n" and github.SLUG.search("ssh://github.com:o/n.git")[1] == "o/n"
    assert github.SLUG.search("https://github.com/o/n.git")[1] == "o/n"
