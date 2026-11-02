"""Checks that a room whose privacy level is owner can read Yerk's own code, the checkout that runs, with Claude Code's Read, Grep and Glob
tools, and that no other room can. On 2026-10-25 an owner room asked to tell a worker's code-review outcome Grepped the checkout for the
worker's finding, Claude Code refused the read, and the room relayed the claim unchecked. The daemon writes the rule into every owner room's
mechanics name the checkout's path, so every owner room checks a claim about Yerk the same way."""
import json
import shutil
from pathlib import Path

from test_trusted_room import TOOLS, layout

from yerk_ai import abilities, context, daemon, memory, shape
from yerk_ai.config import EXAMPLE_PRIVATE, REPO, budgets
from yerk_ai.daemon import Yerkd

FILES = ("yerk_ai/workers.py", "yerk_ai/daemon.py", "tests/test_budget.py", "README.md")


def reaches(path: Path, rules: list[str]) -> bool:
    """A Read rule as Claude Code reads one: `Read(//<folder>/**)` every file under the folder, `Read(//<file>)` the file."""
    for r in (r for r in rules if r.startswith("Read(/") and r.endswith(")")):
        body = r[len("Read(/"):-1]
        if (body.endswith("/**") and path.is_relative_to(Path(body[:-3]))) or path == Path(body):
            return True
    return False


def test_an_owner_rooms_read_and_grep_reach_the_checkout_that_runs_and_its_shell_is_never_handed_it_to_write(tmp_path, monkeypatch):
    """The wall: the room's settings opened its own layer to the file tools and nothing else, so its Grep of workers.py
    and daemon.py in the checkout was refused. Every owner room, with a shell or without, now carries one Read rule on the
    checkout: never an --add-dir, since Claude Code's sandbox opens an added folder to the room's shell for writing, and
    the secrets' denies are as they were (deny beats allow)."""
    home, private, state = layout(tmp_path, monkeypatch)
    y, repo = Yerkd(private, state), REPO.resolve()
    assert y.code_reads() == [f"Read(/{repo}/**)"]
    for name in ("work-room", "example-room", "example-quiet"):
        spec = y.spec_for(name)
        perms = spec.settings["permissions"]
        assert perms["allow"][1] == f"Read(/{repo}/**)" and all(reaches(repo / f, perms["allow"]) for f in FILES), name
        assert not any(d.is_relative_to(repo) or repo.is_relative_to(d) for d in spec.add_dirs), name
        assert perms["deny"] == y.never_read() and "blockReadsOutsideWorkingDirectories" not in perms, name
    box = y.spec_for("work-room").settings["sandbox"]                   # the shell room's sandbox: nothing of the checkout
    assert box["enabled"] is True and str(repo) not in json.dumps(box)


def test_a_room_whose_privacy_is_not_owner_never_reads_the_checkout(tmp_path, monkeypatch):
    """A room with people, its shell trusted or no shell at all, keeps to its own folders: the checkout is an owner room's,
    as a `read` folder is (a room with people takes none)."""
    for sub, mom in (("trusted", f'shell = "trusted"\ntools = {TOOLS}\n'), ("no-shell", "")):
        home, private, state = layout(tmp_path / sub, monkeypatch, mom=mom)
        y = Yerkd(private, state)
        perms = y.spec_for("family-room").settings["permissions"]
        assert not any(reaches(REPO.resolve() / f, perms["allow"]) for f in FILES), sub
        assert not set(y.code_reads()) & set(perms["allow"]) and perms["blockReadsOutsideWorkingDirectories"] is True, sub
        assert y.code_reads()[0] in y.spec_for("work-room").settings["permissions"]["allow"], sub      # the owner's room beside it


def test_a_checkout_that_holds_the_private_or_the_state_folder_is_opened_around_them(tmp_path, monkeypatch):
    """A fresh clone runs on the example layer inside it, and a state folder may sit in the checkout too: the rules then open
    each of the checkout's entries around them (a top file by a rule of its own), never the private or the state folder
    (every room's layer and memory, the ledger, each room's token); a checkout inside one of them is opened nowhere."""
    code = tmp_path / "yerk-ai"
    for f in (*FILES, "examples/rooms/owner.toml"):
        (code / f).parent.mkdir(parents=True, exist_ok=True)
        (code / f).write_text("x\n")
    private, state = code / "examples" / "private", code / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    monkeypatch.setattr(daemon, "REPO", code)
    y = Yerkd(private, state)
    rules = y.code_reads()
    assert rules == y.spec_for("example-room").settings["permissions"]["allow"][1:1 + len(rules)]
    assert f"Read(/{(code / 'README.md').resolve()})" in rules
    for f in (*FILES, "examples/rooms/owner.toml"):
        assert reaches((code / f).resolve(), rules), f
    for p in (private / "rooms.toml", private / "memory" / "pinned.md", state / "secrets" / "yerk-http.secret", state / "rooms" / "example-room"):
        assert not reaches(p.resolve(), rules), p
    monkeypatch.setattr(daemon, "REPO", private / "code")
    assert y.code_reads() == []


def test_every_room_is_handed_the_checkouts_path_in_the_mechanics_and_never_the_mark(tmp_path):
    """A rule is no use to a room that does not know where the code is: mechanics.md carries the mark once, and a room is
    handed the line with the checkout's path, at its session start and in the news of a changed line, inside the budget."""
    raw = context.MECHANICS.read_text()
    line = next(x for x in raw.splitlines() if context.CODE in x)
    said = line.replace(context.CODE, str(REPO))
    assert raw.count(context.CODE) == 1 and all(w in line for w in ("Read and Grep", "owner room", "worker's claim", "since"))
    assert said in context.mechanics().splitlines() and context.CODE not in context.mechanics(True) and shape.offenses(line) == []
    assert context.chars(context.mechanics()) < budgets()["session_start"]["appended"]["mechanics"]
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    y = Yerkd(private, state)
    y.spec_for("example-room")
    start = (state / "rooms" / "example-room" / "context.md").read_text()
    assert said in start.splitlines() and context.CODE not in start
    seen, room, rules = tmp_path / "seen.json", y.room_confs["example-room"], y.memory.root / memory.RULES
    context.news(seen, "", private, room, rules, 2000)                   # a session that looked before the line came in
    look = json.loads(seen.read_text())
    look["start"]["mechanics"] = look["start"]["mechanics"].replace("\n" + said, "")
    seen.write_text(json.dumps(look))
    assert context.news(seen, "s1", private, room, rules, 2000) == f"{context.NEWS}\nnew in mechanics.md: {said.removeprefix('- ')}"


def test_a_room_asked_whether_it_can_read_yerks_code_answers_from_the_entry(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("can you read the harness code and check a worker's claim?", y.memory)
    assert [e["key"] for e in found] == ["harness-code"] and "Read, Grep and Glob" in found[0]["text"]
