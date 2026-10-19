"""Checks the `remember` tool, with which a room files a new fact, decision or correction into
Yerk's own memory tree in the same turn. A correction marks every old line it names, recall never
puts a superseded line above its replacement, and a room that is not an owner room writes its own
placeholder."""
import asyncio
import re
from datetime import datetime, timezone

from yerk_ai import memindex, memory, mentions
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import ConsoleChannel, Yerkd
from yerk_ai.room import Inbound

SRC = r"\(example-room#discord:CHANNEL_ID, \d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d\d:\d\d\) \[m:m-1\]"


def daemon(tmp_path, *rooms: str) -> Yerkd:
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    for name in rooms or ("example-room",):
        if name not in y.room_confs:           # a room that is not an owner room, next to the owner's
            y.room_confs[name] = {**y.room_confs["example-room"], "privacy": "family", "id": f"{name}-id"}
            y.daily.rooms[name] = (f"{name}-id", "room")
        y.add_room(name, ConsoleChannel()).inflight = [Inbound("m-1", "owner", "a placeholder message")]
    return y


def remember(y: Yerkd, room: str, text: str, about: str = "", replaces: tuple[str, ...] = ()) -> dict:
    return asyncio.run(y.tool_remember(room, text, about, list(replaces)))


def names(y: Yerkd) -> mentions.Index:
    s = y.memory
    mentions.Builder(s.mentions, s.roots_for_mentions, s.curated, s.mentions_exclude).refresh(s.scope.files())
    return mentions.Index(s.mentions).load()


def test_a_fact_lands_in_its_own_fact_file_with_its_source_and_mention_recall_has_it_next(tmp_path):
    y = daemon(tmp_path)
    r = remember(y, "example-room", "Example kettle: painted teal, bought on the 3rd.", "example kettle")
    assert r == {"ok": True, "cite": "tree/facts/example-kettle.md:3", "superseded": [], "refused": [], "not_edited": []}
    f = y.memory.root / "facts" / "example-kettle.md"
    lines = f.read_text().splitlines()
    assert lines[:2] == ["# example kettle", ""] and oct(f.stat().st_mode & 0o777) == "0o600"
    assert re.fullmatch(r"- Example kettle: painted teal, bought on the 3rd\. " + SRC, lines[2])
    assert remember(y, "example-room", "Example kettle: painted teal, bought on the 3rd.", "example kettle")["cite"] == r["cite"]
    assert len(f.read_text().splitlines()) == 3                       # a retried call files it once
    hit = names(y).match("what colour is the example kettle now?")
    assert [(e["key"], e["cite"]) for e in hit][:1] == [("example kettle", "facts/example-kettle.md:3")]
    assert "painted teal" in hit[0]["text"]
    assert memory.file_class(f) == ("private", "")


def test_a_decision_goes_to_the_rooms_day_log_and_rides_in_the_next_sessions_pack(tmp_path):
    y = daemon(tmp_path)
    r = remember(y, "example-room", "Decided: the example review moves to Mondays.")
    day = datetime.now(timezone.utc).astimezone(y.memory.tz).strftime("%Y-%m-%d")
    assert r["ok"] and r["cite"].startswith(f"tree/daily/{day}.md:")
    line = (y.memory.daily / f"{day}.md").read_text().splitlines()[int(r["cite"].rpartition(":")[2]) - 1]
    assert re.fullmatch(r"- \d\d:\d\d:\d\d[+-]\d\d:\d\d example-room#discord:CHANNEL_ID note: Decided: the example review "
                        r"moves to Mondays\. \[c:private\] \[m:m-1\.n[0-9a-f]{8}\]", line)
    assert "the example review moves to Mondays" in y.room_pack("example-room")[0]
    assert not (y.memory.root / "facts").exists()
    assert not remember(y, "example-room", "  \n ")["ok"]


def test_a_correction_marks_every_old_line_in_yerks_tree_and_names_a_read_only_one(tmp_path):
    y = daemon(tmp_path)
    old = remember(y, "example-room", "Example plant: water every 5 days.", "example plant")["cite"]
    said = remember(y, "example-room", "Decided: water the example plant every 5 days.")["cite"]
    pinned = (EXAMPLE_PRIVATE / "memory" / "pinned.md").read_text()
    ro = "memory/pinned.md:5"                                          # the example layer's own folder: an `also` root
    assert "every 5 days" in memory.read(y.memory, ro, lines=1)["text"]
    r = remember(y, "example-room", "Example plant: water every 3 days.", "example plant", (old, said, ro, "tree/nowhere.md:1"))
    assert r["ok"] and r["cite"] == "tree/facts/example-plant.md:4" and r["superseded"] == [old, said]
    assert r["not_edited"] == [f"{ro}: a read-only root or not this room's own line; the new line names it"]
    assert len(r["refused"]) == 1 and r["refused"][0].startswith("tree/nowhere.md:1: no such file")
    assert (EXAMPLE_PRIVATE / "memory" / "pinned.md").read_text() == pinned            # never edited
    fact = (y.memory.root / "facts" / "example-plant.md").read_text().splitlines()
    assert re.fullmatch(r"- Example plant: water every 5 days\. \(example-room#discord:CHANNEL_ID, [^)]+\) "
                        r"\[superseded by tree/facts/example-plant\.md:4\] \[m:m-1\]", fact[2])
    assert fact[3].startswith(f"- Example plant: water every 3 days. (supersedes {old}, {said}, {ro}) (example-room#")
    day = said.partition(":")[0].removeprefix("tree/")
    marked = (y.memory.root / day).read_text().splitlines()[int(said.rpartition(":")[2]) - 1]
    assert marked.endswith(" [superseded by tree/facts/example-plant.md:4] [c:private] " + marked.rpartition(" ")[2])
    assert memory.parse_line(marked) is not None                      # still a day-log line: id, class, dedupe hold
    assert "every 5 days" not in y.room_pack("example-room")[0]       # the pack and the topic hit skip it
    assert not y.recall_for("example-room", "how often do we water the example plant?")[1]
    entries = [e["text"] for e in names(y).match("the example plant")]
    assert entries and all("every 5 days." not in t.split("(supersedes")[0] for t in entries)
    assert remember(y, "example-room", "Example plant: water every 3 days.", "example plant", (old,))["superseded"] == [old]
    assert (y.memory.root / "facts" / "example-plant.md").read_text().count("[superseded by") == 1   # marked once


def test_recall_never_puts_a_superseded_line_above_its_replacement(tmp_path):
    """The old line ranks first on its words alone (it repeats them); the replacement is read in or moved
    right above it: a marked line in Yerk's tree, and a read-only line a new line names."""
    y = daemon(tmp_path)
    tree = y.memory.root
    (tree / "facts").mkdir(parents=True)
    (tree / "facts" / "example-ladder.md").write_text(
        "# Example ladder\n\n- Example ladder: lent to the example neighbour, example ladder in the example shed "
        "[superseded by tree/facts/example-hinge.md:3]\n")
    (tree / "facts" / "example-hinge.md").write_text("# Hinge\n\n- It came back on the 9th, hung on the hook.\n")
    (tree / "facts" / "example-watering.md").write_text(
        "# Watering\n\n- Every 3 days now (supersedes memory/pinned.md:5) for the plant.\n")
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    assert all("lent to" not in e["text"] for e in names(y).match("the example ladder"))    # never by name either
    plain = memory.recall(y.memory, "where is the example ladder, example shed?", 6)    # recall's own rank
    assert "lent to the example neighbour" in plain["hits"][0]["text"]
    hits = asyncio.run(y.tool_recall("example-room", "where is the example ladder, example shed?", 6))["hits"]
    at = lambda words: next(i for i, h in enumerate(hits) if words in h["text"])  # noqa: E731
    assert at("came back on the 9th") < at("lent to the example neighbour")
    assert hits[at("came back on the 9th")]["cite"] == "tree/facts/example-hinge.md:3"
    hits = asyncio.run(y.tool_recall("example-room", "household plant needs water every days", 6))["hits"]
    assert at("Every 3 days now") < at("needs water every 5 days")


def test_a_room_that_is_not_an_owner_room_writes_its_own_class_and_never_supersedes_what_it_cannot_read(tmp_path):
    y = daemon(tmp_path, "example-room", "family-room")
    mine = remember(y, "example-room", "Example cousin: visiting on the 12th.", "example cousin")["cite"]
    said = remember(y, "example-room", "Decided: the example dinner is at seven.")["cite"]
    fact = (y.memory.root / "facts" / "example-cousin.md").read_text()
    r = remember(y, "family-room", "Example guest: prefers green tea.", "example guest", (mine, said))
    assert r["ok"] and r["superseded"] == [] and r["not_edited"] == [] and len(r["refused"]) == 2
    assert all("not a line this room may read" in x for x in r["refused"])
    assert not (y.memory.root / "facts" / "example-guest.md").exists()          # a room's fact: its day log
    day = y.memory.root / r["cite"].partition(":")[0].removeprefix("tree/")
    assert (y.memory.root / "facts" / "example-cousin.md").read_text() == fact
    own = day.read_text().splitlines()[int(r["cite"].rpartition(":")[2]) - 1]
    assert re.fullmatch(r"- \S+ family-room#family-room-id note: Example guest: prefers green tea\. "
                        r"\[c:room\] \[m:m-1\.n[0-9a-f]{8}\]", own)
    fix = remember(y, "family-room", "Example guest: prefers mint tea.", "example guest", (r["cite"],))
    assert fix["superseded"] == [r["cite"]] and f"[superseded by {fix['cite']}] [c:room]" in day.read_text()
    assert remember(y, "family-room", "x", "", (fix["cite"],))["superseded"] == [fix["cite"]]   # its own lines only
    owner = day.read_text().splitlines()[int(said.rpartition(":")[2]) - 1]
    assert owner.endswith("[c:private] " + owner.rpartition(" ")[2]) and "superseded" not in owner
