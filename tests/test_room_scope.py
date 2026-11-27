"""Checks that a room given its own `also` in rooms.toml, naming which extra read-only notes folders (the `[memory] also` roots of yerkd.toml) it may read,
keeps to its own tree and those. Its recall (keywords and meaning), read_memory and mention recall serve its own tree and the roots it is given, never
another root; a room without one sees every root as before; a root the install does not index, or an `also` that is not a list, stops the start with one
plain line. Every value here is a placeholder: `live` and `snap` stand for a live notes folder and a frozen copy of it, which share their file names."""
import asyncio
import json
import shutil

import pytest

from yerk_ai import memindex, memory, mentions
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd

from test_jobs import FakeSystemd


MOM = 31        # the one person of the family room; a stand-in, since a real id (17 to 20 digits) never goes in this repository


def install(tmp_path, frozen: object = None, live: object = None, sealed: bool = False, family: bool = False) -> Yerkd:
    """Two read-only roots, each a curated file and a memory folder holding the same file names with their own words:
    `frozen-room` gets `frozen` as its `also` (default the snap pair), `live-room` gets `live` (default the live pair,
    written relative to the private folder), `example-room` sets none; `family` adds `family-room`, a `privacy = "family"`
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
    mom = block.replace("CHANNEL_ID", "MOM_ID").replace('privacy = "owner"', 'privacy = "family"') + f"people = [{MOM}]\nlayer = \"mom\"\n"
    (private / "rooms.toml").write_text(base + f"\n[rooms.frozen-room]\n{sealing}also = {json.dumps(frozen)}\n"
                                        f"\n[rooms.live-room]\n{block}also = {json.dumps(live)}\n"
                                        + (f"\n[rooms.family-room]\n{mom}" if family else ""))
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
    monkeypatch.setattr(memory, "meaning", lambda s, q, **wait: ("ok", [dict(h) for h in every]))
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
    assert len(frozen) == 2 and all("periwinkle" in t for t in frozen)          # the kettle file (its name and bullet, one text), the boat
    assert len(live) == 2 and all("vermilion" in t for t in live)
    assert len(every) == 4                                                       # no `also`: both roots' entries, as before


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
    assert r["ok"] and r["not_edited"] and "(supersedes live/memory/2026-10-19.md:3)" in (y.memory.daily.parent / r["cite"].split("/", 1)[1].rpartition(":")[0]).read_text()   # its own cite since F3
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
    assert asyncio.run(y.tool_recall("frozen-room", "saffron shelf", 20))["hits"] == []        # the hidden words: no hit; its own lines
    assert "frozen room itself" in texts(y, "frozen-room", "example shed key question", 20)       # beside them are chunks of their own
    assert "teal" in texts(y, "frozen-room", "example ladder", 20) and "periwinkle" in texts(y, "frozen-room", "periwinkle door")  # (lane),
    assert [t for t in y.recall_for("frozen-room", "the example shed key question?")[1] if "frozen room itself" in t["text"]]  # so recall
    assert [t for t in y.recall_for("frozen-room", "what is the example ladder?")[1] if "teal" in t["text"]]   # finds them, as topic hits do
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
    note line of class room in the day log, never a fact file; mention recall gives it the names in the files it may read, a
    class-room file of its own and a shared one, and none from an owner-class file; settings, workers, jobs and rules are
    refused. The owner's room still reads everything."""
    from yerk_ai import words
    from yerk_ai.config import people
    from fake_channel import ConsoleChannel
    from yerk_ai.room import Inbound
    y, mom = install(tmp_path, family=True), "family-room"
    assert people(mom, y.room_confs[mom]) == frozenset({MOM}) and not y.owner(mom) and y.daily.rooms[mom] == ("discord:MOM_ID", "room")
    y.daily.capture("example-room", "o1", "owner", "the owner's private line about the example kettle")
    y.daily.capture(mom, "p1", "person", "the mother's line about the example kettle", who="mom")
    memory.remember(y.memory, y.daily, "example-room", "o2", "The example kettle is teal", "example kettle", (), y.reader("example-room"))
    memory.rule(y.memory, "example-room", "o3", "tea", "steep the example tea four minutes, since longer turns it bitter")
    sync(y)
    got = texts(y, mom, "example kettle", 20)
    assert "the mother's line" in got and "owner's private line" not in got and "teal" not in got and "periwinkle" not in got
    assert asyncio.run(y.tool_recall(mom, "owner's private", 20))["hits"] == []    # the hidden words: no hit; her own line beside them is
    assert "the mother's line" in y.room_pack(mom)[0]                             # a chunk of its own (lane): recall, her pack, read_memory
    assert "four minutes" in texts(y, mom, "steep the example tea", 20)                      # rules.md is shared
    assert "teal" in texts(y, "example-room", "example kettle", 20)                        # the owner's room: everything
    day = asyncio.run(y.tool_read_memory(mom, f"daily/{memory.window(y.memory)[0]}.md", 1, 50))
    assert "the mother's line" in day["text"] and "owner's private" not in day["text"] and "(a line of another room)" in day["text"]
    assert not asyncio.run(y.tool_read_memory(mom, "facts/example-kettle.md", 1, 5))["ok"]
    assert "four minutes" in asyncio.run(y.tool_read_memory(mom, "rules.md", 1, 10))["text"]
    ask = "what colour is the example kettle?"
    assert not [e for e in y.recall_for(mom, ask)[0] if "teal" in e["text"]] and [e for e in y.recall_for("example-room", ask)[0] if "teal" in e["text"]]
    (y.memory.root / "facts" / "example-friend.md").write_text("---\nclass: room\nroom: discord:MOM_ID\n---\n# example friend\n\n- Example Friend: knits on Example Lane.\n")
    (y.memory.root / "facts" / "example-club.md").write_text("---\nclass: shared\n---\n# example club\n\n- Example Club: meets on Tuesdays.\n")
    sync(y)                 # her own class-room file and a shared one name people to her; the owner's private kettle file never does
    hers, his = ([e["text"] for e in y.recall_for(r, "is example friend at example club, and what colour is the example kettle?")[0]]
                 for r in (mom, "example-room"))
    assert [t for t in hers if "knits" in t] and [t for t in hers if "Tuesdays" in t] and not [t for t in hers if "teal" in t or "class:" in t]
    assert [t for t in his if "knits" in t] and [t for t in his if "Tuesdays" in t]                # the owner's room: every name, as before
    y.add_room(mom, ConsoleChannel()).inflight = [Inbound("p2", "person", "a placeholder message")]
    note = asyncio.run(y.tool_remember(mom, "Example dinner: Sunday at six", "example dinner", []))
    assert note["ok"] and note["cite"].startswith("memory/daily/") and not (y.memory.root / "facts" / "example-dinner.md").exists()
    line = asyncio.run(y.tool_read_memory(mom, note["cite"], 1, 1))["text"]
    assert f" family-room#discord:MOM_ID note: {memory.MARKS[memory.NOTE]} Example dinner: Sunday at six [c:room] [m:p2." in line
    assert asyncio.run(y.tool_settings(mom, "opus", "")) == {"ok": False, "line": words.OWNER_ONLY}
    assert asyncio.run(y.tool_settings(mom, "", "", by_owner=True))["line"].startswith("this room: ")   # the owner's own slash command
    assert y.owner_only(mom, "") == {"ok": False, "line": words.WORKERS_OWNER_ONLY}
    assert asyncio.run(y.tool_rule(mom, "steep it longer", "tea")) == {"ok": False, "line": words.RULES_OWNER_ONLY}


def test_a_family_rooms_names_follow_each_files_class_as_it_is_now(tmp_path):
    """Cold read of PR 46 (D): yerk-index stamps each entry with its file's class when it builds the name index, and a `.class`
    sidecar edit moves no Markdown file, so the stamp stayed until yerk-index restarted and the family room kept getting the
    names of a file made private, which its recall and read_memory already refused. The daemon reads the class as it is now."""
    y = install(tmp_path, family=True)
    s, mom, ask = y.memory, "family-room", "when does example club meet?"
    (s.root / "facts").mkdir(exist_ok=True)
    (s.root / "facts" / "example-club.md").write_text("# example club\n\n- Example Club: meets on Tuesdays.\n")
    (s.root / "facts" / "example-club.md.class").write_text("shared\n")
    idx, names = memindex.Index(s.index_db, s.scope), mentions.Builder(s.mentions, s.roots_for_mentions, s.curated, s.mentions_exclude)
    idx.sync()
    names.refresh(idx.disk)                       # one Index and one Builder across passes, as yerk-index holds them
    assert [e for e in y.recall_for(mom, ask)[0] if "Tuesdays" in e["text"]]
    (s.root / "facts" / "example-club.md.class").write_text("private\n")
    idx.sync()
    assert names.refresh(idx.disk) is None        # no Markdown file moved: the name index is not built again
    assert not [e for e in y.recall_for(mom, ask)[0] if "Tuesdays" in e["text"]]
    assert not asyncio.run(y.tool_read_memory(mom, "facts/example-club.md", 1, 5))["ok"]
    assert [e for e in y.recall_for("example-room", ask)[0] if "Tuesdays" in e["text"]]     # the owner's room: every name
    idx.close()


def test_a_hidden_longer_name_never_takes_the_words_of_a_family_rooms_own(tmp_path):
    """Cold read of PR 46 (B): the longest key at a position took its words even when the room could read none of its entries,
    so a name in an owner-class file ("Example Aunt Mary") hid the room's own ("Example Aunt"), and the gap said the hidden
    file held the words. For a room that is not an owner room such a key is no match; an owner room keeps the longest key."""
    y = install(tmp_path, family=True)
    facts = y.memory.root / "facts"
    facts.mkdir(exist_ok=True)
    (facts / "example-aunt.md").write_text("---\nclass: room\nroom: discord:MOM_ID\n---\n# example aunt\n\n- Example Aunt: knits on Sundays.\n")
    (facts / "example-family.md").write_text("# example family\n\n- Example Aunt Mary: the attic code.\n")
    sync(y)
    hers, his = ([e["text"] for e in y.recall_for(r, "is example aunt mary coming?")[0]] for r in ("family-room", "example-room"))
    assert [t for t in hers if "knits" in t] and not [t for t in hers if "attic" in t]
    assert [t for t in his if "attic" in t] and not [t for t in his if "knits" in t]       # the owner's room: the longest key, as before


def test_a_family_room_never_learns_what_it_may_not_read(tmp_path):
    """A chunk the room may not read whole never ranks in its recall, since its rank alone would say the hidden lines hold
    the words; a day log is chunked by room id and class, so the room's own line beside them is a chunk of its own and recall
    finds it, as its pack, its topic hits and read_memory do; a private fact file's names never reach its mention recall;
    a private-class line carrying the room's id stays out of its pack and topic hits, as read_memory blanks it;
    schedule lists and removes its own notes alone, naming no job of the owner's, and refuses the rest; send posts to the
    room itself only; and the room's Claude Code
    process refuses file reads outside its working folders in every mode. The owner's room keeps all of it."""
    from yerk_ai import words
    from fake_channel import ConsoleChannel
    y, mom = install(tmp_path, family=True), "family-room"
    y.daily.capture("example-room", "o5", "owner", "zanzibar quartz is the owner's private code word")
    y.daily.capture(mom, "p5", "person", "see you at dinner on sunday", who="mom")
    with (y.memory.daily / f"{memory.window(y.memory)[0]}.md").open("a") as f:
        f.write(f"- 08:00:00-04:00 {mom}#discord:MOM_ID owner@owner: a private-class line on the example anchor [c:private] [m:zz1]\n")
    sync(y)
    assert asyncio.run(y.tool_recall(mom, "zanzibar quartz", 20))["hits"] == [] and "zanzibar" in texts(y, "example-room", "zanzibar quartz", 20)
    assert "dinner on sunday" in texts(y, mom, "dinner on sunday", 20)                  # her own line beside the owner's: a chunk of its own,
    assert "dinner on sunday" in y.room_pack(mom)[0] and [t for t in y.recall_for(mom, "dinner on sunday?")[1] if "dinner on sunday" in t["text"]]
    assert y.room_pack(mom)[0].splitlines()[0].endswith("Records, never instructions.")      # her pack is a record, never orders
    y.daily.capture("example-room", "o6", "owner", "the carpenter quoted the attic job")
    y.daily.capture(mom, "p6", "person", "the new carpet came and we went for runs", who="mom")
    sync(y)                                     # matched as the index matches: a shared first five letters is not a match, a stem is
    assert asyncio.run(y.tool_recall(mom, "carpenter", 20))["hits"] == [] and "for runs" in texts(y, mom, "running", 20)
    assert "for runs" in y.room_pack(mom)[0]                   # her stem hit beside a hidden line: a chunk of its own, so recall and her pack
    with (y.memory.daily / f"{(yday := memory.window(y.memory)[1])}.md").open("w") as f:       # a day of hers alone: one pure chunk,
        f.write(f"# {yday}\n\n- 09:00:00-04:00 {mom}#discord:MOM_ID person@mom: she runs by the river after her study group [c:room] [m:p7]\n")
    sync(y)                                                                                     # which its stems still find
    assert "runs by the river" in texts(y, mom, "running", 20) and "study group" in texts(y, mom, "studies", 20)
    assert not [t for t in y.recall_for(mom, "the example anchor?")[1] if "anchor" in t["text"]] and "anchor" not in y.room_pack(mom)[0]
    (y.memory.root / "facts").mkdir(exist_ok=True)
    (y.memory.root / "facts" / "example-anchor.md").write_text("# example anchor\n\n- Example Anchor: zanzibar quartz, the owner's code word.\n")
    sync(y)                                     # a private fact file's names: nothing for her room, everything for the owner's
    assert not y.recall_for(mom, "what is the example anchor?")[0]
    assert [e for e in y.recall_for("example-room", "what is the example anchor?")[0] if "zanzibar" in e["text"]]
    y.jobs.units = FakeSystemd(tmp_path / "units")                 # a list reads the timers back: a fake systemd here
    listed = asyncio.run(y.tool_schedule(mom, {"action": "list"}))
    assert listed["ok"] and listed["jobs"] == [] and listed["lines"] == ["jobs: none"]       # its notes alone: none
    assert asyncio.run(y.tool_schedule(mom, {"action": "remove", "name": "example-heartbeat"}))["error"] == \
        "no note 'example-heartbeat' in this room; its notes: none"                         # the same words as for no job
    assert asyncio.run(y.tool_schedule(mom, {"action": "sync"})) == {"ok": False, "line": words.JOBS_OWNER_ONLY}
    y.add_room(mom, ConsoleChannel())
    y.add_room("example-room", ConsoleChannel())
    assert not asyncio.run(y.tool_send("example-room", "a line into the owner's room", "", mom))["ok"]
    assert y.spec_for(mom).settings["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert "blockReadsOutsideWorkingDirectories" not in y.spec_for("example-room").settings["permissions"]


def test_a_room_that_lists_people_needs_a_layer_of_its_own(tmp_path):
    """Its layer is a working folder and its files are its prompt: unset (the private folder itself), the memory tree, or a
    context file outside it stops the start; a folder of its own starts."""
    for i, extra in enumerate(("", 'layer = "."\n', 'layer = "memory"\n', 'layer = "mom"\npersona = "../memory/pinned.md"\n', 'layer = "mom"\n')):
        private, state = tmp_path / f"p{i}", tmp_path / f"s{i}"
        shutil.copytree(EXAMPLE_PRIVATE, private)
        state.mkdir()
        block = (private / "rooms.toml").read_text().split("[rooms.example-room]\n", 1)[1].split("\n[rooms.", 1)[0]
        block = "".join(x + "\n" for x in block.replace('privacy = "owner"', 'privacy = "family"').replace("CHANNEL_ID", "MOM_ID").splitlines()
                        if not x.startswith(("persona", "pinned_memory", "open_loops")))
        with (private / "rooms.toml").open("a") as f:
            f.write(f"\n[rooms.family-room]\n{block}people = [{MOM}]\n{extra}")
        if i < 4:
            with pytest.raises(SystemExit, match="people needs a `layer` of its own"):
                Yerkd(private, state)
        else:
            Yerkd(private, state)


def test_no_new_recall_path_brings_a_family_room_an_owner_line_and_a_scoped_room_stays_in_its_roots(tmp_path):
    """Rail run 2's paths (the past-tense search, deep name recall, the asked-about search, the absence line) read as the
    room's reader in its own scope (decisions 22, 23, 44): the family room gets its own lines and shared files, never an
    owner line; a name only the owner's files hold is not on file for her; a scoped owner room gets its own `also` root only."""
    from yerk_ai import hooks
    y, mom = install(tmp_path, family=True), "family-room"
    y.daily.capture("example-room", "o1", "owner", "we ate at the example diner last friday with Examplia, the owner's private night")
    y.daily.capture(mom, "p1", "person", "we ate at the example diner last friday, the mother's own night", who="mom")
    (y.memory.root / "facts").mkdir(exist_ok=True)
    (y.memory.root / "facts" / "examplia.md").write_text("# examplia\n\n- Examplia: the owner's private friend from the example diner.\n")
    (y.memory.root / "facts" / "example-club.md").write_text("---\nclass: shared\n---\n# example club\n\n- Example Club: meets at the example diner.\n")
    sync(y)
    asks = ["what did we eat at the example diner last friday?", "who is Examplia?", "what do you know about the example diner",
            "tell me about example club", "do you remember the example kettle"]
    for ask in asks:
        names, topics, past, absent = got = y.recall_for(mom, ask)
        text = hooks.render_recall(names, topics, 1500, past, absent)
        assert "owner's private" not in text + json.dumps(got) and "periwinkle" not in text and "vermilion" not in text, ask
    hers = y.recall_for(mom, asks[0])
    assert hers[2] and any("the mother's own night" in h["text"] for h in hers[2])        # her own line comes in through the past search
    assert y.recall_for(mom, asks[1])[3] == ["Examplia"]                                  # only the owner's files hold it: not on file for her
    assert any("meets at the example diner" in e["text"] for e in y.recall_for(mom, asks[3])[0])     # a shared file reaches her
    his = y.recall_for("example-room", asks[1])
    assert his[3] == [] and any("owner's private friend" in e["text"] for e in his[0])    # the owner's room: their card, nothing absent
    frozen = hooks.render_recall(*y.recall_for("frozen-room", "what do you know about the example shed key")[:2], 1500,
                                 *y.recall_for("frozen-room", "what do you know about the example shed key")[2:])
    assert "periwinkle" in frozen and "vermilion" not in frozen                           # its own `also` root, never the other


def test_a_name_brings_its_best_lines_newest_first_and_a_name_with_none_gets_the_absence_line(tmp_path):
    y = install(tmp_path)
    live = tmp_path / "live" / "memory"
    (live / "facts" / "examplia.md").write_text("- Examplia: the example neighbour, two doors down.\n")
    (live / "2026-10-01.md").write_text("# 2026-10-01\n\n- Examplia lent the example ladder.\n- who is Examplia?\n")
    (live / "2026-10-12.md").write_text("# 2026-10-12\n\n- Examplia came back from the example trip.\n")
    y.daily.capture("example-room", "now1", "owner", "tell me about Examplia")
    sync(y)
    lines = memory.lines_on(y.scoped["live-room"], "Examplia", y.reader("live-room"), exclude={"now1"})
    assert [x["text"] for x in lines] == ["Examplia: the example neighbour, two doors down.", "10-12 Examplia came back from the example trip.",
                                          "10-01 Examplia lent the example ladder."]     # standing first, then newest first; no ask, no own message
    names, topics, past, absent = y.recall_for("live-room", "who is Examplia? and who is Zorblaxia?")
    assert absent == ["Zorblaxia"] and [e["cite"] for e in names if e.get("named") == "examplia"][:2] == [
        f"{live}/facts/examplia.md:1", "live/memory/2026-10-12.md:3"]          # the card, then its newest line, cited as recall cites it
    from yerk_ai import hooks
    assert hooks.render_recall(names, topics, 1500, past, absent).startswith("[named, nothing on file: Zorblaxia]\n" + hooks.HEADS[0])


def test_an_unfinished_keyword_read_is_no_absence_and_eight_crowded_chunks_are_looked_past(tmp_path, monkeypatch):
    """2026-10-21 cold read: ranked() answers a busy or broken index read with ok False and no hits, and lines_on read
    that as no lines, so a room said "nothing on file" about a name it holds; and in a room reading a snapshot beside the
    live notes, the copies took all eight chunks and the line holding every word ranked ninth."""
    y = install(tmp_path)
    (tmp_path / "live" / "memory" / "2026-10-01.md").write_text("# 2026-10-01\n\n- Zorblaxia lent the example ladder.\n")
    sync(y)
    real = memory.ranked
    monkeypatch.setattr(memory, "ranked", lambda *a, **k: {"ok": False, "status": "timeout", "error": "busy"})
    assert y.recall_for("live-room", "who is Zorblaxia?")[3] == []                  # busy: no absence line
    def crowded(s, q, k=6, *a, **kw):
        r = real(s, q, k, *a, **kw)
        return r if k > 8 else {**r, "hits": [{**h, "text": "- the example ladder."} for h in r.get("hits", [])]}
    monkeypatch.setattr(memory, "ranked", crowded)
    names, topics, past, absent = y.recall_for("live-room", "who is Zorblaxia?")
    assert absent == [] and any("Zorblaxia lent" in e["text"] for e in names)       # the deeper read found it
