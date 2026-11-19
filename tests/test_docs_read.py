"""Checks that a room whose privacy level is owner can read the docs pages Yerk AI fetched and the findings of `yerk doctor`,
its daily check, with Claude Code's Read, Grep and Glob tools, and that other rooms get the pages alone. On 2026-11-19 (need
124), a room asked to check a worker's re-pinned docs sentences against the fetched page, with that day's doctor findings
read first, Grepped the page and doctor.json, Claude Code refused both reads, and the pins were checked against another log's
quotes. The daemon writes the docs root and doctor.json into every owner room's settings (a sealed room's: the fetched pages
harness from the files."""
import json
from pathlib import Path

from test_trusted_room import layout
from test_worktree_read import reaches

from yerk_ai import abilities, context, docs, doctor, hooks, memory, shape
from yerk_ai.config import EXAMPLE_PRIVATE, budgets
from yerk_ai.daemon import Yerkd

SLUG = "prompting-example"                                  # a placeholder page
PIN = "Ask for what to do, and say why."                    # a placeholder sentence on it
SEALED = "sealed-room"


def kept(state: Path) -> dict[str, Path]:
    """What `yerk docs` and the doctor leave in the state folder, at the paths their writers use: a fetched page, a room's
    shape report, the store's lesson report, the refresh stamp and the last run's findings."""
    root = docs.root(state)
    for d in (root / "claude", root / "shape"):
        d.mkdir(parents=True, exist_ok=True)
    files = {"page": root / "claude" / f"{SLUG}.md", "report": root / "shape" / "example-room.md",
             "lessons": root / "shape" / "lessons.md", "stamp": root / docs.STAMP, "findings": state / doctor.LAST}
    files["page"].write_text(f"---\nclass: shared\nsource: https://docs.example.org/{SLUG}\nfetched: x\nsha256: {'0' * 64}\n---\n{PIN}\n")
    files["report"].write_text("---\nclass: room\nroom: example-room\n---\n# rule shape: example-room\n")
    files["lessons"].write_text("---\nclass: private\n---\n# lessons\n")
    files["stamp"].write_text("{}")
    doctor.save(state, 0.0, [], {"at": 0.0, "findings": ["docs: an example finding"]})
    return files


def test_an_owner_rooms_read_and_grep_reach_the_fetched_docs_and_the_doctors_findings(tmp_path, monkeypatch):
    """The wall: an owner room's settings opened its layer, the checkout that runs and the workers' worktrees, and none of
    those reach a fetched page or doctor.json, so its Grep of both was refused. Every owner room, with a shell or without,
    now carries a rule on the docs root and one on doctor.json beside them: the pages, the shape reports and the findings
    are reached; the rest of the state folder is not (the secrets, a room's token and context, the ledger, the trash), and
    never as an --add-dir or in the sandbox."""
    home, private, state = layout(tmp_path, monkeypatch)
    y = Yerkd(private, state)
    files, root = kept(state), docs.root(state).resolve()
    assert y.docs_reads("example-room") == [f"Read(/{root}/**)", f"Read(/{(state / 'doctor.json').resolve()})"]
    for name in ("work-room", "example-room", "example-quiet"):
        spec = y.spec_for(name)
        perms, code = spec.settings["permissions"], [*y.code_reads(), *y.worktree_reads()]
        assert not any(reaches(p.resolve(), [perms["allow"][0], *code]) for p in files.values()), name      # the wall
        assert perms["allow"][1:1 + len(code) + 2] == [*code, *y.docs_reads(name)], name
        assert all(reaches(p.resolve(), perms["allow"]) for p in files.values()), name
        for p in (state / "secrets" / "yerk-http.secret", state / "rooms" / name / "hook-header", state / "rooms" / name / "context.md",
                  state / "ledger.sqlite", state / "sessions.json", state / "trash" / "x.md", state / "cut-said.json"):
            assert not reaches(p.resolve(), y.docs_reads(name)), (name, p)
        assert not any(a.is_relative_to(root) or root.is_relative_to(a) for a in spec.add_dirs), name
        assert perms["deny"] == y.never_read() and "blockReadsOutsideWorkingDirectories" not in perms, name
    box = y.spec_for("work-room").settings["sandbox"]                    # the shell room's sandbox: nothing of the docs
    assert box["enabled"] is True and str(root) not in json.dumps(box) and "doctor.json" not in json.dumps(box)


def test_a_sealed_room_reads_the_fetched_pages_and_never_a_shape_report_or_a_finding(tmp_path, monkeypatch):
    """A sealed room is kept blind to the other rooms' rules and lines: a shape report quotes a room's rule files and a
    finding names other rooms' files, so its rule opens the fetched pages alone, which are of class shared."""
    home, private, state = layout(tmp_path, monkeypatch)
    block = (private / "rooms.toml").read_text().split("[rooms.example-room]\n", 1)[1].split("\n[rooms.", 1)[0]
    with (private / "rooms.toml").open("a") as f:
        f.write(f"\n[rooms.{SEALED}]\n{block.replace('CHANNEL_ID', 'SEALED_ID')}sealed = true\n")
    y = Yerkd(private, state)
    files = kept(state)
    assert y.docs_reads(SEALED) == [f"Read(/{docs.root(state).resolve()}/claude/**)"]
    allow = y.spec_for(SEALED).settings["permissions"]["allow"]
    assert reaches(files["page"].resolve(), allow)
    assert not any(reaches(files[k].resolve(), allow) for k in ("report", "lessons", "stamp", "findings"))
    unsealed = y.spec_for("example-room").settings["permissions"]["allow"]          # the owner's room beside it
    assert all(reaches(p.resolve(), unsealed) for p in files.values())


def test_a_room_whose_privacy_is_not_owner_reads_the_pages_by_read_memory_and_never_the_findings(tmp_path, monkeypatch):
    """A room with people, its shell trusted or no shell at all, keeps to its own folders for Read and Grep, as before: a
    fetched page reaches it through read_memory, being of class shared, and the store's lessons and the findings never."""
    for sub, mom in (("trusted", None), ("no-shell", "")):
        home, private, state = layout(tmp_path / sub, monkeypatch, **({} if mom is None else {"mom": mom}))
        y = Yerkd(private, state)
        files = kept(state)
        perms, reader = y.spec_for("family-room").settings["permissions"], y.reader("family-room")
        assert not set(y.docs_reads("family-room")) & set(perms["allow"]) and perms["blockReadsOutsideWorkingDirectories"] is True, sub
        assert not any(reaches(p.resolve(), perms["allow"]) for p in files.values()), sub
        assert memory.read(y.memory, f"docs/claude/{SLUG}.md", reader=reader)["text"].endswith(PIN), sub
        assert not memory.read(y.memory, "docs/shape/lessons.md", reader=reader)["ok"], sub
        assert not memory.read(y.memory, str(files["findings"]), reader=reader)["ok"], sub
        assert set(y.docs_reads("work-room")) <= set(y.spec_for("work-room").settings["permissions"]["allow"]), sub


def test_every_room_is_told_where_the_docs_and_the_findings_are_in_the_mechanics(tmp_path, monkeypatch):
    """A rule is no use to a room that does not know where the files are (the room looked in its work folder and in the
    checkout): the docs line names both docs folders and doctor.json, the folder that holds them by a room's own work
    folder, and who reads them how, inside the mechanics' budget and the rule-shape check; that folder is the one the
    rules open, from every room's work folder."""
    line = next(x for x in context.MECHANICS.read_text().splitlines() if "`doctor.json`" in x)
    assert all(w in line for w in ("`docs/claude/`", "`docs/shape/`", "last findings", "Yerk's state folder",
                                   "your work folder's path before `rooms/`", "`read_memory` reads the docs",
                                   "an owner room's Read and Grep all three", "a sealed room's: the Claude docs", "since"))
    assert shape.offenses(line) == [] and line in context.mechanics().splitlines()
    assert context.chars(context.MECHANICS.read_text().strip().replace(context.CODE, "x" * 130)) <= budgets()["session_start"]["appended"]["mechanics"]
    home, private, state = layout(tmp_path, monkeypatch)
    y = Yerkd(private, state)
    files = kept(state)
    for name in ("work-room", "example-room", "family-room"):
        there = Path(str(y.spec_for(name).cwd).rpartition("/rooms/")[0])       # the line's way, from the room's own work folder
        assert (there / "docs" / "claude" / f"{SLUG}.md").resolve() == files["page"].resolve(), name
        assert (there / "doctor.json").resolve() == files["findings"].resolve(), name


def test_a_room_asked_to_check_the_docs_page_and_the_doctor_findings_answers_from_the_entry(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("check the new pins against the docs page, with today's doctor findings read first", y.memory)
    assert [e["key"] for e in found] == ["harness-docs"] and "Read, Grep and Glob" in found[0]["text"]
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    said = sorted(e["key"] for e in abilities.asked("are your rules written the way the claude docs say?", y.memory))
    assert said == ["claude-docs", "rule-shape"]
