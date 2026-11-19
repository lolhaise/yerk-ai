"""Checks that one of your rooms can read a worker's own folder (Read, Grep and Glob), beyond its worktrees, so it can check what the worker left there.
The case behind it (need 123 of 2026-11-19): a room asked to relay a finished worker's result with its patch checked had its Glob of the worker's folder
and its Read of the patch the worker left there refused, since that worker made no worktree, and the patch went out on the report's word. The
daemon writes one pattern over the workers' root into every owner room's settings, so a worker started after the room's
process is reached too; a sealed room keeps to the worktrees, and a room whose privacy is not owner to its own folders."""
import time
from fnmatch import fnmatchcase
from pathlib import Path

from test_trusted_room import layout

from yerk_ai import abilities, context, hooks, shape, workers
from yerk_ai.config import EXAMPLE_PRIVATE, budgets
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger

WID, LATER = "4f2a91", "7c1e08"                   # placeholder worker ids
OWN = ("example-fix.patch", "REPORT.md", "BRIEF.md", "PROMPT.md", "TASKS.md", "worker.json", "settings.json", "stdout.jsonl",
       "stderr.log", "exit.json", "out/findings.md")


def reads(path: Path, rules: list[str]) -> bool:
    """A Read rule as Claude Code 2.1.321 reads one (its bundle, read 2026-11-19): a trailing `/**` dropped, the rest
    gitignore's, so `Read(//<folder>/**)` is the folder itself (a Glob or a Grep on it) and every file under it, and a `*`
    in it any one name."""
    for r in (r for r in rules if r.startswith("Read(/") and r.endswith(")")):
        head = Path(r[len("Read(/"):-1].removesuffix("/**")).parts
        if len(path.parts) >= len(head) and all(fnmatchcase(p, h) for p, h in zip(path.parts, head)):
            return True
    return False


def worker_folder(root: Path, wid: str, worktree: bool = False) -> Path:
    """A finished worker's folder as the start and the run leave it: its own files, a patch and a report file it wrote,
    and, when `worktree`, Yerk's worktree `wt` (a linked worktree's .git is a file)."""
    d = root / wid
    for f in (*OWN, *(("wt/yerk_ai/deploy.py", "wt/.git") if worktree else ())):
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / f).write_text("gitdir: /elsewhere/.git/worktrees/wt\n" if f.endswith(".git") else "x\n")
    return d


def test_an_owner_rooms_glob_and_read_reach_a_workers_own_folder_and_the_patch_it_left_there(tmp_path, monkeypatch):
    """The wall: an owner room's settings opened the workers' worktrees only, so a worker that made none had its folder's
    Glob and its patch's Read refused. Every owner room, with a shell or without, now carries one pattern over the
    workers' root: the folder itself and every file in it are reached (its patch, report, brief, tasks, stream and the
    files it wrote, its worktree's too), a worker started after the room's process too; the rest of the state folder never,
    the secrets' denies are the same, and never as an --add-dir or in the sandbox."""
    home, private, state = layout(tmp_path, monkeypatch)
    y = Yerkd(private, state)
    root = y.workers.conf.root.resolve()
    d, built = worker_folder(root, WID), worker_folder(root, "9e3b57", worktree=True)
    for name in ("work-room", "example-room", "example-quiet"):
        spec = y.spec_for(name)
        perms, code = spec.settings["permissions"], y.code_reads()
        assert y.worker_reads(name) == [f"Read(/{root}/*/**)"] and perms["allow"][1:2 + len(code)] == [*code, *y.worker_reads(name)], name
        assert not any(reads(p.resolve(), r) for p in (d, d / "example-fix.patch") for r in (y.worktree_reads(), y.code_reads())), name   # the wall
        assert reads(d, perms["allow"]) and all(reads((d / f).resolve(), perms["allow"]) for f in OWN), name
        assert reads((built / "wt" / "yerk_ai" / "deploy.py").resolve(), perms["allow"]), name
        for p in (state / "secrets" / "yerk-http.secret", state / "rooms" / name / "hook-header", state / "rooms" / name / "work",
                  state / "ledger.sqlite", state / "trash" / "x", root):
            assert not reads(p.resolve(), y.worker_reads(name)), (name, p)
        assert not any(a.is_relative_to(root) or root.is_relative_to(a) for a in spec.add_dirs), name
        assert perms["deny"] == y.never_read() and "blockReadsOutsideWorkingDirectories" not in perms, name
    later = worker_folder(root, LATER)                                   # started after the room's process: no rebuild
    assert reads((later / "example-fix.patch").resolve(), y.spec_for("example-room").settings["permissions"]["allow"])
    assert str(root) not in str(y.spec_for("work-room").settings["sandbox"])     # the shell room's sandbox: nothing of the workers


def test_the_worker_report_names_the_folder_the_rule_opens(tmp_path, monkeypatch):
    """A room needs the path: the `worker` tool's report gives the worker's folder (`staging`), the one the rule reaches."""
    home, private, state = layout(tmp_path, monkeypatch)
    y = Yerkd(private, state)
    d = worker_folder(y.workers.conf.root, WID)
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.worker_add(id=WID, room="example-room", account="one", unit=f"yerk-worker-{WID}", model="m", staging=str(d),
                      report=str(d / "REPORT.md"), tasks=str(d / "TASKS.md"), cap=1800.0, started=0.0)
    r = workers.report(ledger, WID)
    assert r["ok"] and r["staging"] == str(d) and r["worktrees"] == []
    assert reads(Path(r["staging"]).resolve(), y.worker_reads("example-room"))
    assert reads((Path(r["staging"]) / "example-fix.patch").resolve(), y.worker_reads("example-room"))


def test_a_sealed_owner_room_keeps_to_the_worktrees(tmp_path, monkeypatch):
    """A sealed room reads only its own room's lines, and a worker's brief and stream carry the lines of the room that
    started it, so its rules are the worktrees' alone: a worktree's files reached, the worker's own files never; the
    unsealed owner room beside it reaches them."""
    home, private, state = layout(tmp_path, monkeypatch)
    rooms = private / "rooms.toml"
    rooms.write_text(rooms.read_text().replace("[rooms.example-quiet]\n", "[rooms.example-quiet]\nsealed = true\n", 1))
    y = Yerkd(private, state)
    d = worker_folder(y.workers.conf.root.resolve(), WID, worktree=True)
    assert y.reader("example-quiet").sealed and not y.reader("example-room").sealed
    sealed = y.spec_for("example-quiet").settings["permissions"]["allow"]
    assert y.worker_reads("example-quiet") == y.worktree_reads() and set(y.worktree_reads()) <= set(sealed)
    assert reads((d / "wt" / "yerk_ai" / "deploy.py").resolve(), sealed)
    assert not reads(d.resolve(), sealed) and not any(reads((d / f).resolve(), sealed) for f in OWN)
    assert all(reads((d / f).resolve(), y.spec_for("example-room").settings["permissions"]["allow"]) for f in OWN)


def test_a_room_whose_privacy_is_not_owner_never_reads_a_workers_folder(tmp_path, monkeypatch):
    """A room with people, its shell trusted or no shell at all, keeps to its own folders: a worker's folder holds the
    owner's work."""
    for sub, mom in (("trusted", None), ("no-shell", "")):
        home, private, state = layout(tmp_path / sub, monkeypatch, **({} if mom is None else {"mom": mom}))
        y = Yerkd(private, state)
        d = worker_folder(y.workers.conf.root.resolve(), WID)
        perms = y.spec_for("family-room").settings["permissions"]
        assert not set(y.worker_reads("work-room")) & set(perms["allow"]) and perms["blockReadsOutsideWorkingDirectories"] is True, sub
        assert not reads(d, perms["allow"]) and not any(reads((d / f).resolve(), perms["allow"]) for f in OWN), sub


def test_the_rule_follows_the_workers_root_and_opens_none_where_it_could_reach_a_rooms_folder_or_the_trash(tmp_path, monkeypatch):
    """[workers] root moves the workers' folders and the rule goes with it, the default inside the state folder too; a
    root that is, holds or sits inside the private folder, holds the state folder, or sits inside it anywhere but its
    workers folder (its rooms, a room's folder, the trash) gets no rule, since each entry under the root is read whole."""
    home, private, state = layout(tmp_path, monkeypatch)
    base = (private / "yerkd.toml").read_text()
    for root, want in ((home / "scratch" / "workers", True), (state / "workers", True), (state / "workers" / "more", True),
                       (state / "rooms", False), (state / "rooms" / "example-room", False), (state / "trash", False),
                       (state, False), (private, False), (home, False)):
        (private / "yerkd.toml").write_text(base + f'\n[workers]\nroot = "{root}"\n')
        y = Yerkd(private, state)
        rules = y.worker_reads("example-room")
        assert bool(rules) is want and bool(y.worktree_reads()) is want, root
        assert rules == ([f"Read(/{root.resolve()}/*/**)"] if want else []), root
        assert (not want) or reads((worker_folder(root.resolve(), WID) / "example-fix.patch").resolve(), rules), root
        assert set(rules) <= set(y.spec_for("example-room").settings["permissions"]["allow"]), root


def test_a_long_reports_relay_no_longer_tells_the_room_it_does_not_open_the_report(tmp_path):
    """The relay of a report past its cap said the rest was in a report "which this room does not open", true when no
    room's Read reached a worker's folder; it says where the rest is, true in every room."""
    (report := tmp_path / WID / "REPORT.md").parent.mkdir()
    report.write_text("# Report\n" + "".join(f"- finding {i}: what was found, said in full.\n" for i in range(200)))
    row = {"id": WID, "cap": 900.0, "started": time.time() - 185, "account": "one", "model": "m", "room": "example-room",
           "report": str(report), "tasks": str(tmp_path / "TASKS.md")}
    end = {"exit": "done", "finished": row["started"] + 181, "result": "done", "has_report": True, "result_code": "success",
           "detail": "success, exited 0"}
    part = workers.relay_text(row, end, 1500)
    assert len(part) <= 1500 and "the report begins:" in part and "the rest is in the report; do not paste it]" in part
    assert "does not open" not in part and "there is no file to open" in workers.relay_text(row, end, 0)


def test_every_room_is_told_a_workers_folder_is_open_in_the_mechanics():
    """A rule is no use to a room that does not know it: the code line says a worker's own work is in its folder (its
    report, a patch, its stream and worktrees) and what a sealed room reads, inside the mechanics' budget and the
    rule-shape check."""
    text = context.MECHANICS.read_text()
    line = next(x for x in text.splitlines() if context.CODE in x)
    assert all(w in line for w in ("in its folder", "a patch", "its stream", "worktrees", "(a sealed room's: the worktrees)"))
    assert shape.offenses(line) == []
    assert context.chars(text.strip().replace(context.CODE, "x" * 130)) <= budgets()["session_start"]["appended"]["mechanics"]


def test_a_room_asked_whether_it_can_read_a_workers_folder_answers_from_the_entry(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("can you read the worker's patch in its folder?", y.memory)
    assert [e["key"] for e in found] == ["worker-folder"] and "Read, Grep and Glob" in found[0]["text"]
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert [e["key"] for e in abilities.asked("can you read the worker's worktree?", y.memory)] == ["worker-code"]
