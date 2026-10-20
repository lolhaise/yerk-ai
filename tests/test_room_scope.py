"""Checks that a room given its own `also` in rooms.toml, naming which extra read-only notes folders (the `[memory] also` roots of yerkd.toml) it may read,
keeps to its own tree and those. Its recall (keywords and meaning), read_memory and mention recall serve its own tree and the roots it is given, never
another root; a room without one sees every root as before; a root the install does not index, or an `also` that is not a list, stops the start with one
is a placeholder: `live` and `snap` stand for a live notes folder and a frozen copy of it, which share their file names."""
import asyncio
import json
import shutil

import pytest

from yerk_ai import memindex, memory, mentions
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd


MOM = 31        # the one person of the family room; a stand-in, since a real id (17 to 20 digits) never goes in this repository


def install(tmp_path, frozen: object = None, live: object = None, sealed: bool = False, family: bool = False) -> Yerkd:
    """Two read-only roots, each a curated file and a memory folder holding the same file names with their own words:
    `frozen-room` gets `frozen` as its `also` (default the snap pair), `live-room` gets `live` (default the live pair,
    written relative to the private folder), `example-room` sets none; `family` adds `Alex`, a `privacy = "family"`
    room with one listed person and no `also`. The index and the name index are built."""
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    for side, word, more in (("live", "vermilion", " the shed key, the example shed key by the door"), ("snap", "periwinkle", "")):
        (tmp_path / side / "memory" / "facts").mkdir(parents=True)
        (tmp_path / side / "MEMORY.md").write_text(f"# Example boat\n\nThe example boat is painted {word}.\n")
        (tmp_path / side / "memory" / "facts" / "example-kettle.md").write_text(
            f"# example kettle\n\n- Example kettle: the {word} lid, bought on the 3rd.\n")
        (tmp_path / side / "memory" / "2026-10-19.md").write_text(f"# 2026-10-19\n\n- the example shed key hangs by the {word} door{more}\n")
    pair = {s: [str(tmp_path / s / "MEMORY.md"), str(tmp_path / s / "memory")] for s in ("live", "snap")}
    (private / "yerkd.toml").write_text(f'[memory]\nroot = "memory"\nalso = {json.dumps(pair["live"] + pair["snap"])}\n')
    base = (private / "rooms.toml").read_text()
    block = base.split("[rooms.example-room]\n", 1)[1].split("\n[rooms.", 1)[0]
    frozen = pair["snap"] if frozen is None else frozen
    live = ["../live/MEMORY.md", "../live/memory"] if live is None else live
    sealing = block.replace("CHANNEL_ID", "FROZEN_ID") + "sealed = true\n" if sealed else block    # its own room id, as live
    mom = block.replace("CHANNEL_ID", "MOM_ID").replace('turn = "and"', 'the = "example"') + f"the = [{MOM}]\n"
    (private / "rooms.toml").write_text(base + f"\n[rooms.frozen-room]\n{sealing}also = {json.dumps(frozen)}\n"
                                        f"\n[rooms.live-room]\n{block}also = {json.dumps(live)}\n"
                                        + (f"\n[rooms.example-two]\n{mom}" if family else ""))
    y = Yerkd(private, state)
    s = y.memory
    memindex.Index(s.index_db, s.scope).sync()
    mentions.Builder(s.mentions, s.roots_for_mentions, s.curated, s.mentions_exclude).refresh(s.scope.files())
    return y


def texts(y: Yerkd, room: str, query: str, k: int = 6) -> str:
    r = asyncio.run(y.tool_recall(room, query, k))
    assert r["ok"], r
    return "\n".join(h["text"] for h in r["hits"])


def test_a_scoped_rooms_recall_never_returns_a_line_from_a_root_outside_its_also(tmp_path):
    y = install(tmp_path)
    q = "where does the example shed key hang, by which door?"
    assert "periwinkle" in texts(y, "frozen-room", q) and "vermilion" not in texts(y, "frozen-room", q)
    assert "vermilion" in texts(y, "live-room", q) and "periwinkle" not in texts(y, "live-room", q)
    assert "vermilion" in texts(y, "example-room", q) and "periwinkle" in texts(y, "example-room", q)    # no `also`: every root
    assert "vermilion" in texts(y, "example-room", q, 1)          # the live line ranks first overall, yet a scoped room's
    assert "periwinkle" in texts(y, "frozen-room", q, 1)          # one hit is its own: kept to its roots before the cut to k
    assert "vermilion" not in texts(y, "frozen-room", "vermilion boat")


def test_a_scoped_rooms_meaning_hits_are_kept_to_its_roots_too(tmp_path, monkeypatch):
    """yerk-index's meaning search covers every root, so its answer passes the same filter as the keywords'."""
    y = install(tmp_path)
    conn = memindex.connect(y.memory.index_db, readonly=True)
    rows = conn.execute("SELECT id, path, lo, body FROM chunks").fetchall()
    conn.close()
    every = [{"id": i, "path": p, "line": lo, "text": b, "score": 0.9} for i, p, lo, b in rows]
    monkeypatch.setattr(memory, "meaning", lambda s, q: ("ok", [dict(h) for h in every]))
    assert "vermilion" not in texts(y, "frozen-room", "vermilion", 20)
    assert "periwinkle" not in texts(y, "live-room", "periwinkle", 20)
    assert "vermilion" in texts(y, "example-room", "vermilion", 20) and "periwinkle" in texts(y, "example-room", "periwinkle", 20)


def test_a_scoped_rooms_read_memory_reads_its_own_roots_and_refuses_the_others(tmp_path):
    y = install(tmp_path)

    def read(room: str, path: str) -> dict:
        return asyncio.run(y.tool_read_memory(room, path, 1, 3))
    for cite in ("MEMORY.md:3", "memory/2026-10-19.md:3", "memory/facts/example-kettle.md:3"):   # the same name in both roots
        assert "periwinkle" in read("frozen-room", cite)["text"] and "vermilion" in read("live-room", cite)["text"]
        assert "vermilion" in read("example-room", cite)["text"]                  # no `also`: the first root, as before
    for side, room in (("live", "frozen-room"), ("snap", "live-room")):
        for path in (tmp_path / side / "MEMORY.md", tmp_path / side / "memory" / "2026-10-19.md"):
            r = read(room, str(path))
            assert not r["ok"] and r["error"] == f"{path} is outside the memory tree; read_memory reads only the private memory tree"
            assert read("example-room", str(path))["ok"]


def test_a_scoped_rooms_mention_entries_come_from_its_own_roots_only(tmp_path):
    y = install(tmp_path)
    ask = "what colour is the example kettle lid, and the example boat?"

    def named(room: str) -> list[str]:
        return [e["text"] for e in y.recall_for(room, ask)[0] if str(tmp_path) in e["cite"]]
    frozen, live, every = named("frozen-room"), named("live-room"), named("example-room")
    assert len(frozen) == 3 and all("periwinkle" in t for t in frozen)          # the kettle file's name and bullet, the boat
    assert len(live) == 3 and all("tangerine" in t for t in live)
    assert len(every) == 6                                                       # no `also`: both roots' entries, as before


def test_an_also_the_install_does_not_index_stops_the_start_with_one_plain_line(tmp_path):
    with pytest.raises(SystemExit) as e:
        install(tmp_path / "a", frozen=[str(tmp_path / "a" / "elsewhere")])
    assert str(e.value) == f"room frozen-room: also {tmp_path / 'a' / 'elsewhere'} is not one of the [memory] also roots in yerkd.toml"
    with pytest.raises(SystemExit) as e:          # a sub-folder of a root is indexed, as part of its root (cold read A4)
        install(tmp_path / "d", frozen=[str(tmp_path / "d" / "snap" / "memory" / "facts")])
    assert str(e.value) == f"room frozen-room: also {tmp_path / 'd' / 'snap' / 'memory' / 'facts'} is not one of the [memory] also roots in yerkd.toml"
    with pytest.raises(SystemExit) as e:                                         # Yerk's own tree is no read-only root
        install(tmp_path / "b", frozen=["memory"])
    assert str(e.value).startswith(f"room frozen-room: also {tmp_path / 'b' / 'private' / 'memory'} is not one of the [memory] also roots")
    with pytest.raises(SystemExit) as e:                                         # one string, not a list
        install(tmp_path / "c", frozen=str(tmp_path / "c" / "snap" / "memory"))
    assert str(e.value) == (f'room frozen-room: also must be a list of folders, like also = ["~/notes"], '
                            f"not {str(tmp_path / 'c' / 'snap' / 'memory')!r}")


def sync(y: Yerkd) -> None:
    s = y.memory
    memindex.Index(s.index_db, s.scope).sync()
    mentions.Builder(s.mentions, s.roots_for_mentions, s.curated, s.mentions_exclude).refresh(s.scope.files())


def test_a_correction_in_the_live_room_never_shows_in_the_frozen_room(tmp_path):
    """Cold read A1: remember filed a read-only line's pair in superseded.tsv under its short cite, which both copies share
    (memory/2026-10-19.md:3), so live-room's correction of its copy was read in above the snapshot's line in frozen-room's
    recall, at rank 1 even for a query sharing no word with it. The pair now names the old line's real file, and recall
    reads a correction in only above a hit in that very file, as the room's own scope resolves the hit's cite."""
    y = install(tmp_path)
    r = memory.remember(y.scoped["live-room"], y.daily, "live-room", "m1", "Moved to the crimson hook in the hall", "",
                        ("memory/2026-10-19.md:3 | example shed key",), y.reader("live-room"))
    assert r["ok"] and r["not_edited"] and "(supersedes memory/2026-10-19.md:3)" in (y.memory.daily.parent / r["cite"].split("/", 1)[1].rpartition(":")[0]).read_text()
    assert (y.memory.root / memory.LATER).read_text().startswith(f"{tmp_path / 'live' / 'memory' / '2026-10-19.md'}:3\t")
    sync(y)
    q = "where does the example shed key hang, by which door?"
    live = texts(y, "live-room", q)
    assert "crimson" in live and live.index("crimson") < live.index("vermilion")        # its own copy: read in above the line
    for query in (q, "periwinkle door"):
        assert "crimson" not in texts(y, "frozen-room", query) and "periwinkle" in texts(y, "frozen-room", query)


def test_a_sealed_room_reads_its_own_lines_and_its_also_roots_only(tmp_path):
    """`sealed = true` (frozen-room here): in Yerk's own tree the room reads its own room's lines only, whatever their
    class: never another room's day-log line, fact file or note, through recall, read_memory, mention recall or a
    correction recall reads in; its `also` roots stay whole, and its own notes go to its own day log, so it reads them
    back. An unsealed owner room (example-room) still reads every room's lines."""
    y = install(tmp_path, sealed=True)
    snap = tmp_path / "snap" / "memory" / "2026-10-19.md"
    y.daily.capture("live-room", "m2", "user", "the example shed key moved to the saffron shelf")
    y.daily.capture("frozen-room", "m3", "user", "the example shed key question from the frozen room itself")
    fact = memory.remember(y.memory, y.daily, "live-room", "m4", "The example ladder is saffron", "example ladder",
                           (), y.reader("live-room"))
    fix = memory.remember(y.memory, y.daily, "example-room", "m5", "Now on the saffron peg", "",
                          (f"{snap}:3 | example shed key",), y.reader("example-room"))
    own = memory.remember(y.scoped["frozen-room"], y.daily, "frozen-room", "m6", "The example ladder is teal", "example ladder",
                          (), y.reader("frozen-room"))
    assert fact["cite"].startswith("memory/facts/") and fix["ok"] and own["cite"].startswith("memory/daily/")
    sync(y)
    for query in ("example shed key saffron shelf", "example ladder", "periwinkle door"):
        assert "saffron" not in texts(y, "frozen-room", query, 20)
        assert "saffron" in texts(y, "example-room", query, 20) or query == "periwinkle door"
    assert "frozen room itself" in texts(y, "frozen-room", "example shed key question") and "periwinkle" in texts(y, "frozen-room", "periwinkle door")
    assert "teal" in texts(y, "frozen-room", "example ladder")
    day = fact["cite"] if fact["cite"].startswith("memory/daily/") else own["cite"]
    log = asyncio.run(y.tool_read_memory("frozen-room", day.rpartition(":")[0], 1, 50))
    assert log["ok"] and "frozen room itself" in log["text"] and "saffron" not in log["text"] and "(a line of another room)" in log["text"]
    assert not asyncio.run(y.tool_read_memory("frozen-room", fact["cite"], 1, 5))["ok"]
    assert asyncio.run(y.tool_read_memory("example-room", fact["cite"], 1, 5))["ok"]
    assert "periwinkle" in asyncio.run(y.tool_read_memory("frozen-room", "memory/2026-10-19.md:3", 1, 1))["text"]
    assert not [e for e in y.recall_for("frozen-room", "what is the example ladder?")[0] if "saffron" in e["text"]]
    assert [e for e in y.recall_for("example-room", "what is the example ladder?")[0] if "saffron" in e["text"]]


def test_a_family_room_with_one_person_reads_its_own_lines_and_shared_files_only(tmp_path):
    """The room for the owner's mother, end to end, on the block the brief names (`privacy = "family"`, one listed
    person, no `also`): its lines carry class room; recall and read_memory from it return nothing from the owner's
    private files, another room's lines or the read-only roots; the shared rules file reads; its `remember` files a
    the sample item moved to the next step and, the test reads it; back as plain text with one; more, line,
    one more line for the. Check while a second entry waits."""
    from yerk_ai import words
    from yerk_ai.config import people
    from yerk_ai.daemon import ConsoleChannel
    from yerk_ai.room import Inbound
    y, mom = install(tmp_path, family=True), "Alex"
    assert people(mom, y.room_confs[mom]) == frozenset({MOM}) and not y.owner(mom) and y.daily.rooms[mom] == ("discord:MOM_ID", "room")
    y.daily.capture("example-room", "o1", "owner", "the owner's private line about the example kettle")
    y.daily.capture(mom, "p1", "person", "the mother's line about the example kettle", who="mom")
    memory.remember(y.memory, y.daily, "example-room", "o2", "The example kettle is teal", "example kettle", (), y.reader("example-room"))
    memory.rule(y.memory, "example-room", "o3", "tea", "sample item moved to the next")
    sync(y)
    got = texts(y, mom, "example kettle", 20)
    assert "the mother's line" in got and "owner's private line" not in got and "teal" not in got and "periwinkle" not in got
    assert "four minutes" in texts(y, mom, "steep the example tea", 20)                      # rules.md is shared
    assert "teal" in texts(y, "example-room", "example kettle", 20)                        # the owner's room: everything
    day = asyncio.run(y.tool_read_memory(mom, f"daily/{memory.window(y.memory)[0]}.md", 1, 50))
    assert "the mother's line" in day["text"] and "owner's private" not in day["text"] and "(a line of another room)" in day["text"]
    assert not asyncio.run(y.tool_read_memory(mom, "facts/example-kettle.md", 1, 5))["ok"]
    assert "four minutes" in asyncio.run(y.tool_read_memory(mom, "rules.md", 1, 10))["text"]
    ask = "what colour is the example kettle?"
    assert not [e for e in y.recall_for(mom, ask)[0] if "teal" in e["text"]] and [e for e in y.recall_for("example-room", ask)[0] if "teal" in e["text"]]
    y.add_room(mom, ConsoleChannel()).inflight = [Inbound("p2", "person", "a placeholder message")]
    note = asyncio.run(y.tool_remember(mom, "Example dinner: Sunday at six", "example dinner", []))
    assert note["ok"] and note["cite"].startswith("memory/daily/") and not (y.memory.root / "facts" / "example-dinner.md").exists()
    line = asyncio.run(y.tool_read_memory(mom, note["cite"], 1, 1))["text"]
    assert " The#check:While_A second: Entry waits: In the queue [for:its] [turn:and0." in line
    assert asyncio.run(y.tool_settings(mom, "opus", "")) == {"ok": False, "line": words.OWNER_ONLY}
    assert y.owner_only(mom, "") == {"ok": False, "line": words.WORKERS_OWNER_ONLY}
    assert y.owner_only(mom, "", words.JOBS_OWNER_ONLY) == {"ok": False, "line": words.JOBS_OWNER_ONLY}
    assert asyncio.run(y.tool_rule(mom, "steep it longer", "tea")) == {"ok": False, "line": words.RULES_OWNER_ONLY}


def test_a_family_room_never_learns_what_it_may_not_read(tmp_path):
    """The last part closes the case before the next run starts the example note, says the sample item moved to the next
    run; starts the-example note says the sample item moved to the next step and the test, reads it_back as plain;
    one more line for, the check while a; second entry waits in the queue for; its turn and The Last
    process refuses file reads outside its working folders in every mode. The owner's room keeps all of it."""
    from yerk_ai import words
    from yerk_ai.daemon import ConsoleChannel
    y, mom = install(tmp_path, family=True), "Alex"
    y.daily.capture("example-room", "o5", "owner", "zanzibar quartz is the owner's private code word")
    y.daily.capture(mom, "p5", "person", "see you at dinner on sunday", who="mom")
    with (y.memory.daily / f"{memory.window(y.memory)[0]}.md").open("a") as f:
        f.write(f"- 08:00:00-04:00 {mom}#discord:MOM_ID owner@owner: a private-class line on the example anchor [c:private] [m:zz1]\n")
    sync(y)
    assert asyncio.run(y.tool_recall(mom, "zanzibar quartz", 20))["hits"] == [] and "zanzibar" in texts(y, "example-room", "zanzibar quartz", 20)
    assert "note says the" in texts(y, mom, "reads it back", 20)                  # starts the example, note says the sample
    assert not [t for t in y.recall_for(mom, "the example anchor?")[1] if "anchor" in t["text"]] and "anchor" not in y.room_pack(mom)[0]
    for req in ({"action": "list"}, {"action": "remove", "name": "example-heartbeat"}, {"action": "sync"}):
        assert asyncio.run(y.tool_schedule(mom, req)) == {"ok": False, "line": words.JOBS_OWNER_ONLY}
    y.add_room(mom, ConsoleChannel())
    y.add_room("example-room", ConsoleChannel())
    assert not asyncio.run(y.tool_send("example-room", "a line into the owner's room", "", mom))["ok"]
    assert y.spec_for(mom).settings["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert "blockReadsOutsideWorkingDirectories" not in y.spec_for("example-room").settings["permissions"]
