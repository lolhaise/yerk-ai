"""Checks that one of your rooms can read a worker's change in its worktrees (its git checkouts) with Read, Grep and Glob, so it can check a worker's claim.
The case behind it (need 110 of 2026-11-18): a room asked to relay a finished worker's result with its code claims checked Grepped the worker's worktree,
Claude Code refused the read, and the change went out on the report's word. The daemon writes one pattern per worktree name into every owner room's
settings, over the workers' root, so a worker started after the room's process is reached too; the `worker` tool's report
names the worktrees a worker left, and the mechanics say both, so every owner room checks a worker's claim the same way."""
import json
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from test_trusted_room import layout

from yerk_ai import abilities, context, hooks, shape, workers
from yerk_ai.config import EXAMPLE_PRIVATE, budgets
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger

WID, LATER = "4f2a91", "7c1e08"                   # placeholder worker ids
CHANGED = ("wt/yerk_ai/deploy.py", "wt/tests/test_deploy.py", "wt/README.md", "web-wt/server.py")
OWN = ("REPORT.md", "BRIEF.md", "PROMPT.md", "TASKS.md", "worker.json", "mcp.json", "settings.json", "stdout.jsonl", "drive/notes.md")


def reaches(path: Path, rules: list[str]) -> bool:
    """A Read rule as Claude Code reads one (gitignore's syntax): `Read(//<folder>/**)` every file under the folder, a `*` in
    it any one name, `Read(//<file>)` the file."""
    for r in (r for r in rules if r.startswith("Read(/") and r.endswith(")")):
        body = r[len("Read(/"):-1]
        if body.endswith("/**"):
            head = Path(body[:-3]).parts
            if len(path.parts) > len(head) and all(fnmatchcase(p, h) for p, h in zip(path.parts, head)):
                return True
        elif path == Path(body):
            return True
    return False


def worker_folder(root: Path, wid: str) -> Path:
    """A finished worker's folder as a need brief leaves it: Yerk's worktree `wt`, a plug-in's `web-wt` (a linked
    worktree's .git is a file), a scratch folder, and the worker's own files."""
    d = root / wid
    for f in (*CHANGED, *OWN):
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / f).write_text("x\n")
    for t in ("wt", "web-wt"):
        (d / t / ".git").write_text(f"gitdir: /elsewhere/.git/worktrees/{t}\n")
    return d


def test_an_owner_rooms_read_and_grep_reach_each_workers_worktree_and_never_its_own_files(tmp_path, monkeypatch):
    """The wall: an owner room's settings opened its own layer and the checkout that runs, so its Grep of a worker's
    worktree for the change the worker claimed was refused. Every owner room, with a shell or without, now carries a
    pattern per worktree name over the workers' root: the worktrees' files are reached, the worker's report, brief,
    stream and other folders are not, nor the rest of the state folder, and never as an --add-dir or in the sandbox."""
    home, private, state = layout(tmp_path, monkeypatch)
    y = Yerkd(private, state)
    root = y.workers.conf.root.resolve()
    d = worker_folder(root, WID)
    assert y.worktree_reads() == [f"Read(/{root}/*/wt/**)", f"Read(/{root}/*/*-wt/**)"]
    for name in ("work-room", "example-room", "example-quiet"):
        spec = y.spec_for(name)
        perms, code = spec.settings["permissions"], y.code_reads()
        assert perms["allow"][1:1 + len(code) + 2] == [*code, *y.worktree_reads()], name
        assert all(reaches((d / f).resolve(), perms["allow"]) for f in CHANGED), name
        assert not any(reaches((d / f).resolve(), y.worktree_reads()) for f in OWN), name
        for p in (state / "secrets" / "yerk-http.secret", state / "rooms" / name / "hook-header", state / "ledger.sqlite"):
            assert not reaches(p.resolve(), y.worktree_reads()), (name, p)
        assert not any(a.is_relative_to(root) or root.is_relative_to(a) for a in spec.add_dirs), name
        assert perms["deny"] == y.never_read() and "blockReadsOutsideWorkingDirectories" not in perms, name
    later = worker_folder(root, LATER)                                   # started after the room's process: no rebuild
    assert reaches((later / "wt" / "yerk_ai" / "deploy.py").resolve(), y.spec_for("example-room").settings["permissions"]["allow"])
    box = y.spec_for("work-room").settings["sandbox"]                    # the shell room's sandbox: nothing of the workers
    assert box["enabled"] is True and str(root) not in json.dumps(box)


def test_a_room_whose_privacy_is_not_owner_never_reads_a_workers_worktree(tmp_path, monkeypatch):
    """A room with people, its shell trusted or no shell at all, keeps to its own folders: a worker runs from an owner
    room only, and its worktree is read there."""
    for sub, mom in (("trusted", None), ("no-shell", "")):
        home, private, state = layout(tmp_path / sub, monkeypatch, **({} if mom is None else {"mom": mom}))
        y = Yerkd(private, state)
        d = worker_folder(y.workers.conf.root.resolve(), WID)
        perms = y.spec_for("family-room").settings["permissions"]
        assert not set(y.worktree_reads()) & set(perms["allow"]) and perms["blockReadsOutsideWorkingDirectories"] is True, sub
        assert not any(reaches((d / f).resolve(), perms["allow"]) for f in CHANGED), sub
        assert set(y.worktree_reads()) <= set(y.spec_for("work-room").settings["permissions"]["allow"]), sub   # sample item moved to the


def test_the_rules_follow_the_workers_root_and_open_none_around_the_private_folder(tmp_path, monkeypatch):
    """[workers] root moves the workers' folders, and the patterns go with it; a root that is, holds or sits inside the
    private folder, or holds the state folder, gets no pattern, since one there could reach a layer or a room's folder."""
    home, private, state = layout(tmp_path, monkeypatch)
    base = (private / "yerkd.toml").read_text()
    elsewhere = home / "scratch" / "workers"
    for root, want in ((elsewhere, True), (private, False), (private / "workers", False), (home, False), (state, False)):
        (private / "yerkd.toml").write_text(base + f'\n[workers]\nroot = "{root}"\n')
        y = Yerkd(private, state)
        rules = y.worktree_reads()
        assert bool(rules) is want, root
        assert all(r.startswith(f"Read(/{root.resolve()}/*/") for r in rules), root
        assert (not want) or reaches((worker_folder(root.resolve(), WID) / "wt" / "README.md").resolve(), rules), root
        assert set(rules) <= set(y.spec_for("example-room").settings["permissions"]["allow"]), root


def test_the_worker_report_names_the_worktrees_a_room_reads(tmp_path):
    """A room needs the path: the `worker` tool's report (and `yerk worker report`) names each linked git worktree in the
    worker's folder by the names the rules open, never a plain folder or one by another name; none once needs.tidy took
    them, and none for a row with no folder."""
    ledger = Ledger(tmp_path / "ledger.sqlite")
    d = worker_folder(tmp_path / "workers", WID)
    (d / "scratch").mkdir()
    (d / "scratch" / ".git").write_text("gitdir: /elsewhere/.git/worktrees/scratch\n")    # a worktree by another name
    (d / "plain-wt").mkdir()                                                             # a folder, no worktree
    ledger.worker_add(id=WID, room="r", account="one", unit=f"yerk-worker-{WID}", model="m", staging=str(d),
                      report=str(d / "REPORT.md"), tasks=str(d / "TASKS.md"), cap=1800.0, started=0.0)
    r = workers.report(ledger, WID)
    assert r["ok"] and r["worktrees"] == [str(d / "web-wt"), str(d / "wt")] and r["staging"] == str(d)
    assert all(reaches(Path(w) / "x.py", [f"Read(/{tmp_path / 'workers'}/*/{n}/**)" for n in workers.WORKTREES]) for w in r["worktrees"])
    for t in ("wt", "web-wt"):
        (d / t / ".git").unlink()
    assert workers.report(ledger, WID)["worktrees"] == []
    ledger.worker_add(id=LATER, room="r", account="one", unit=f"yerk-worker-{LATER}", model="m", staging="", report="",
                      tasks="", cap=1800.0, started=0.0)
    assert workers.report(ledger, LATER)["worktrees"] == []


def test_a_real_git_worktree_is_named_and_its_files_reached(tmp_path, monkeypatch):
    """The names the rules open are the ones git leaves: a worktree added under a worker's folder as a need brief says
    (`git worktree add <your folder>/wt -b <branch> main`) is listed by the report and its files are reached."""
    home, private, state = layout(tmp_path, monkeypatch)
    y = Yerkd(private, state)
    repo, d = tmp_path / "repo", y.workers.conf.root / WID
    repo.mkdir()
    d.mkdir(parents=True)

    def git(*a: str, at: Path = repo) -> None:
        subprocess.run(["git", "-C", str(at), "-c", "user.name=Example", "-c", "user.email=nobody", "-c", "commit.gpgsign=false", *a],
                       check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (repo / "deploy.py").write_text("GATE = True\n")
    git("add", ".")
    git("commit", "-q", "-m", "one")
    git("worktree", "add", "-q", str(d / "wt"), "-b", "need-example", "main")
    assert workers.worktrees(d) == [str(d / "wt")]
    assert reaches((d / "wt" / "deploy.py").resolve(), y.spec_for("example-room").settings["permissions"]["allow"])


def test_every_room_is_told_where_a_workers_change_is_in_the_mechanics(tmp_path):
    """A rule is no use to a room that does not know where the change is: the code line names a worker's worktrees and
    the report that names them, beside the checkout, inside the mechanics' budget and the rule-shape check."""
    line = next(x for x in context.MECHANICS.read_text().splitlines() if context.CODE in x)
    assert all(w in line for w in ("a worker's own change is in its worktrees", "`worker` report names them", "Read and Grep",
                                   "worker's claim about Yerk or its change", "since"))
    assert shape.offenses(line) == []
    assert context.chars(context.MECHANICS.read_text().strip().replace(context.CODE, "x" * 130)) <= budgets()["session_start"]["appended"]["mechanics"]


def test_a_room_asked_whether_it_can_read_a_workers_worktree_answers_from_the_entry(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("can you read the worker's worktree and check its claims?", y.memory)
    assert [e["key"] for e in found] == ["worker-code"] and "Read, Grep and Glob" in found[0]["text"]
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert [e["key"] for e in abilities.asked("can you read the harness code and check a worker's claim?", y.memory)] == ["harness-code"]
