"""Checks that the `worker` tool's report gives a worker's branch, its head and its commits, read with git, so a room can check a worker's
claim. The case behind it (need 134 of 2026-11-21): a room asked to relay a plug-in worker's result, the worker naming its branch, its head
and the commits before it, had its Read of the worktree's HEAD in the plug-in's own .git and its Grep of that .git's refs refused, since a
linked worktree's .git is only a pointer there and no room's rules open a plug-in's .git, and a commit there is compressed anyway. The
`worker` tool's report reads each worktree in the worker's folder with git, read only: its repository's own checkout and
the branch that is on, its branch and head, the commits each side lacks, the newest of those its base lacks, and the files
it has not committed; the plug-in's .git stays shut to the room's Read, and the mechanics say where the branch is read."""
import asyncio
import os
import subprocess
from pathlib import Path

from test_trusted_room import layout
from test_worker_folder_read import reads

from yerk_ai import abilities, context, hooks, shape, words, workers
from yerk_ai.config import EXAMPLE_PRIVATE, budgets
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger

WID = "4f2a91"                                     # a placeholder worker id
AT = 1_700_000_000                                 # the commits' clock: the first one's time, each next a minute on


def git(at: Path, *a: str, when: int = AT) -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": f"@{when} +0000", "GIT_COMMITTER_DATE": f"@{when} +0000"}
    return subprocess.run(["git", "-C", str(at), "-c", "user.name=Example", "-c", "user.email=nobody", "-c", "commit.gpgsign=false",
                           "-c", "core.hooksPath=/dev/null", *a], capture_output=True, text=True, check=True, env=env).stdout.strip()


def repo(at: Path) -> Path:
    """A repository of the test's own on main, one commit: a plug-in's checkout, the one its rooms run."""
    at.mkdir(parents=True)
    git(at, "init", "-q", "-b", "main")
    (at / "README.md").write_text("an example plug-in\n")
    git(at, "add", ".")
    git(at, "commit", "-q", "-m", "base")
    return at


def commits(tree: Path, n: int, start: int = 1) -> list[str]:
    """`n` commits on the worktree's branch, a minute apart from AT; their shas, oldest first."""
    shas = []
    for i in range(start, start + n):
        (tree / f"step-{i}.txt").write_text(f"step {i}\n")
        git(tree, "add", ".")
        git(tree, "commit", "-q", "-m", f"step {i}: an example change", when=AT + 60 * i)
        shas.append(git(tree, "rev-parse", "HEAD"))
    return shas


def row(ledger: Ledger, d: Path) -> None:
    ledger.worker_add(id=WID, room="example-room", account="one", unit=f"yerk-worker-{WID}", model="m", staging=str(d),
                      report=str(d / "REPORT.md"), tasks=str(d / "TASKS.md"), cap=1800.0, started=0.0)


def test_the_wall_a_plug_in_worktrees_git_stays_shut_to_the_room_and_the_report_reads_its_branch_head_and_commits(tmp_path, monkeypatch):
    """The wall: a plug-in's worktree under a worker's folder has a .git that is only a pointer into the plug-in's own
    .git, which no room's rules open, so an owner room read the worktree's files and neither its head nor its commits. The
    pointer's target stays shut (no rule was opened for it); the `worker` tool's report reads the branch with git in its
    place, the plug-in's and Yerk's alike, and writes nothing: no ref, no lock, the index as it was."""
    home, private, state = layout(tmp_path, monkeypatch)
    y = Yerkd(private, state)
    plugin, own = repo(home / ".local" / "share" / "example-plugin"), repo(tmp_path / "checkout")
    d = y.workers.conf.root / WID
    d.mkdir(parents=True)
    git(plugin, "worktree", "add", "-q", str(d / "example-wt"), "-b", "example-branch", "main")
    git(own, "worktree", "add", "-q", str(d / "wt"), "-b", "need-example-1", "main")
    shas, [yours] = commits(d / "example-wt", 4), commits(d / "wt", 1)
    pointer = (d / "example-wt" / ".git").read_text().split(": ", 1)[1].strip()
    allow = y.spec_for("example-room").settings["permissions"]["allow"]
    assert Path(pointer).resolve() == (plugin / ".git" / "worktrees" / "example-wt").resolve()      # only a pointer
    assert reads((d / "example-wt" / "step-4.txt").resolve(), allow)                                  # its files: read
    for shut in (Path(pointer) / "HEAD", plugin / ".git" / "refs" / "heads" / "example-branch", plugin / ".git" / "packed-refs"):
        assert not reads(shut.resolve(), allow), shut                                                 # its git: never
    before = [(plugin / ".git" / "worktrees" / "example-wt" / "index").read_bytes(), git(plugin, "for-each-ref")]
    ledger = Ledger(tmp_path / "ledger.sqlite")
    row(ledger, d)
    r = workers.report(ledger, WID)
    assert r["ok"] and r["worktrees"] == [str(d / "example-wt"), str(d / "wt")]
    got = [{k: v for k, v in b.items() if k != "commits"} for b in r["branches"]]
    assert got == [{"worktree": str(d / "example-wt"), "repo": str(plugin), "base": "main", "branch": "example-branch",
                    "head": shas[3][:7], "only_on_base": 0, "not_on_base": 4, "not_committed": 0},
                   {"worktree": str(d / "wt"), "repo": str(own), "base": "main", "branch": "need-example-1",
                    "head": yours[:7], "only_on_base": 0, "not_on_base": 1, "not_committed": 0}]
    assert [(c["sha"], c["subject"]) for c in r["branches"][0]["commits"]] == [
        (s[:7], f"step {i}: an example change") for i, s in reversed(list(enumerate(shas, 1)))]       # newest first, base's none
    assert workers.branch(d / "example-wt", now=AT + 60 * 4 + 180)["commits"][:2] == [
        {"sha": shas[3][:7], "subject": "step 4: an example change", "when": "3 min ago"},
        {"sha": shas[2][:7], "subject": "step 3: an example change", "when": "4 min ago"}]
    assert [(plugin / ".git" / "worktrees" / "example-wt" / "index").read_bytes(), git(plugin, "for-each-ref")] == before
    assert not list((plugin / ".git").rglob("*.lock"))


def test_a_detached_worktree_a_base_that_moved_files_not_committed_and_a_long_branch_are_each_said(tmp_path):
    """A cold read's worktree is detached at the head it reads: no branch, its commits against the checkout's branch all
    the same. A base that moved on since the branch was made is counted (no fast-forward then), every file changed or new
    and not committed is counted, and a branch past COMMITS commits lists its newest COMMITS with the whole count beside."""
    plugin, d = repo(tmp_path / "example-plugin"), tmp_path / "workers" / WID
    git(plugin, "worktree", "add", "-q", str(d / "example-wt"), "-b", "example-branch", "main")
    shas = commits(d / "example-wt", workers.COMMITS + 2)
    git(plugin, "worktree", "add", "-q", "--detach", str(d / "read-wt"), shas[1])
    (plugin / "MAIN.txt").write_text("moved on\n")
    git(plugin, "add", ".")
    git(plugin, "commit", "-q", "-m", "main moved on")
    (d / "example-wt" / "step-1.txt").write_text("changed, not committed\n")
    (d / "example-wt" / "new.txt").write_text("new, not committed\n")
    read, long = workers.branch(d / "read-wt", now=AT), workers.branch(d / "example-wt", now=AT)
    assert read["branch"] is None and read["head"] == shas[1][:7] and read["base"] == "main"
    assert (read["only_on_base"], read["not_on_base"], read["not_committed"]) == (1, 2, 0)
    assert [c["sha"] for c in read["commits"]] == [shas[1][:7], shas[0][:7]]
    assert (long["only_on_base"], long["not_on_base"], long["not_committed"]) == (1, workers.COMMITS + 2, 2)
    assert [c["sha"] for c in long["commits"]] == [s[:7] for s in reversed(shas)][:workers.COMMITS]


def test_a_worktree_git_cannot_read_is_one_line_and_the_report_still_answers(tmp_path):
    """A worktree whose repository is gone (a pointer to nowhere, a folder the sweep half took) is git's own first line of
    why, never a traceback, and the report answers with the rest."""
    d = tmp_path / "workers" / WID
    (d / "wt").mkdir(parents=True)
    (d / "wt" / ".git").write_text(f"gitdir: {tmp_path / 'elsewhere' / '.git' / 'worktrees' / 'wt'}\n")
    ledger = Ledger(tmp_path / "ledger.sqlite")
    row(ledger, d)
    r = workers.report(ledger, WID)
    assert r["ok"] and r["worktrees"] == [str(d / "wt")] and list(r["branches"][0]) == ["worktree", "error"]
    assert r["branches"][0]["error"].startswith(words.BRANCH_UNREAD.format(why="fatal: not a git repository"))
    assert workers.report(ledger, WID)["branches"] == r["branches"]
    (d / "wt" / ".git").unlink()
    assert workers.report(ledger, WID)["branches"] == []


def test_an_owner_rooms_worker_tool_gives_the_branches_and_a_persons_room_gets_no_report(tmp_path, monkeypatch):
    """Through the tool a room calls, the report comes with the branches, its git read off the daemon's loop; a room whose
    privacy is not owner reads no worker's report, as before."""
    home, private, state = layout(tmp_path, monkeypatch)
    y = Yerkd(private, state)
    plugin, d = repo(home / ".local" / "share" / "example-plugin"), y.workers.conf.root / WID
    git(plugin, "worktree", "add", "-q", str(d / "example-wt"), "-b", "example-branch", "main")
    [sha] = commits(d / "example-wt", 1)
    row(y.ledger, d)
    threads: list[str] = []
    real = asyncio.to_thread

    async def to_thread(f, *a, **k):
        threads.append(f.__name__)
        return await real(f, *a, **k)
    monkeypatch.setattr(workers.asyncio, "to_thread", to_thread)
    r = asyncio.run(y.tool_worker("example-room", {"action": "report", "id": WID}))
    assert r["ok"] and [(b["branch"], b["head"], b["not_on_base"]) for b in r["branches"]] == [("example-branch", sha[:7], 1)]
    assert threads == ["report"]
    family = next(n for n, c in y.room_confs.items() if c.get("privacy", "owner") != "owner")
    assert asyncio.run(y.tool_worker(family, {"action": "report", "id": WID})) == {"ok": False, "line": words.WORKERS_OWNER_ONLY}


def test_every_room_is_told_the_report_reads_a_worktrees_branch_head_and_commits(tmp_path):
    """A way no room knows is no way: the mechanics' code line says the `worker` report reads each worktree's branch, head
    and commits, inside the mechanics' budget and the rule-shape check, and a room asked whether it can read a worker's
    branch gets the entry."""
    line = next(x for x in context.MECHANICS.read_text().splitlines() if context.CODE in x)
    assert "worktrees, which `worker` report names with each one's branch, head and commits" in line
    assert shape.offenses(line) == []
    assert context.chars(context.MECHANICS.read_text().strip().replace(context.CODE, "x" * 130)) <= budgets()["session_start"]["appended"]["mechanics"]
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("can you read the worker's branch and check its commits?", y.memory)
    assert [e["key"] for e in found] == ["worker-branch"] and "git" in found[0]["text"]
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
