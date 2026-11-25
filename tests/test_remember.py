"""Checks the `remember` tool, with which a room files a new fact, decision or correction into
Yerk's own memory tree in the same turn. A correction marks every old line it names, recall never
puts a superseded line above its replacement, and a room that is not an owner room writes its own
placeholder."""
import asyncio
import json
import re
import threading
from datetime import date, datetime, timezone

from yerk_ai import due, mcp_server, memindex, memory, mentions, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
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
    assert r == {"ok": True, "cite": "tree/facts/example-kettle.md:3", "superseded": [], "refused": [], "not_edited": [],
                 "next": words.FILED_NEXT.format(clause=words.FILED_AT_MOST)}
    assert list(r)[-1] == "next" and "first and whole" in r["next"]       # the last thing the room reads before it answers
    assert "next" not in remember(y, "example-room", "  ")                # nothing filed, nothing to say about the answer
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
    again = remember(y, "example-room", "Example plant: water every 3 days.", "example plant", (old,))
    assert again["superseded"] == [] and again["not_edited"] == [f"{old}: already superseded by {r['cite']}"]   # F6
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


# the PR 35 cold read's seven fixes, in its order (each test fails on 709c6b5)
def test_two_racing_corrections_of_one_line_say_which_one_marked_it(tmp_path):
    """F6: both answers said `superseded` while the mark named the first only."""
    y = daemon(tmp_path)
    old = remember(y, "example-room", "Example bike: the lock code is 1234.", "example bike")["cite"]
    got: dict[int, dict] = {}
    bar = threading.Barrier(2)

    def go(n: int) -> None:
        bar.wait()
        got[n] = memory.remember(y.memory, y.daily, "example-room", f"m-{n}", f"Example bike: the lock code is {n * 1111}.",
                                 "example bike", (old,), y.reader("example-room"))
    ts = [threading.Thread(target=go, args=(n,)) for n in (5, 7)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    first, second = sorted(got.values(), key=lambda r: not r["superseded"])
    assert first["superseded"] == [old] and second["superseded"] == []
    assert second["not_edited"] == [f"{old}: already superseded by {first['cite']}"]
    assert (y.memory.root / "facts" / "example-bike.md").read_text().count("[superseded") == 1


def test_a_long_or_wordy_about_files_under_a_name_mention_recall_keys(tmp_path):
    """F7: 300 characters raised `File name too long`; seven words, or two letters, filed under a name no key matches."""
    y = daemon(tmp_path)
    long = "the example grandmother's old apartment building " + "on the north side of the example city " * 7
    assert len(long) >= 300
    assert remember(y, "example-room", "Example lobby: the door code changed.", long)["cite"] == \
        "tree/facts/the-example-grandmother-old-apartment.md:3"
    assert remember(y, "example-room", "Example cousin: moved in May.", "example cousin from the north side branch")["cite"] == \
        "tree/facts/example-cousin-from-the-north-side.md:3"
    keyed = [(e["key"], e["cite"]) for e in names(y).match("the example grandmother's old apartment, the example cousin from the north side")]
    assert ("the example grandmother old apartment", "facts/the-example-grandmother-old-apartment.md:3") in keyed
    assert ("example cousin from the north side", "facts/example-cousin-from-the-north-side.md:3") in keyed
    assert remember(y, "example-room", "Example name: Al.", "Al")["cite"].startswith("tree/daily/")     # under KEY_MIN


def test_a_facts_folder_linked_out_of_the_tree_is_never_written_through(tmp_path):
    """F8: root/facts was never resolved, so a link into an `also` root got the fact line."""
    priv, also = tmp_path / "private", tmp_path / "also"
    (priv / "memory").mkdir(parents=True)
    (also / "facts").mkdir(parents=True)
    boat = also / "facts" / "example-boat.md"
    boat.write_text("# example boat\n\n- Example boat: moored at pier 3.\n")
    (priv / "memory" / "facts").symlink_to(also / "facts")
    s = memory.settings(priv, tmp_path / "state", {"root": "memory", "also": [str(also)]})
    r = memory.remember(s, memory.DailyLog(s.daily, s.tz, {"example-room": ("rid", "private")}), "example-room", "m-1",
                        "Example boat: moored at pier 5 now.", "example boat")
    assert r["ok"] and r["cite"].startswith("memory/daily/") and "pier 5" in (s.daily / r["cite"].split("/")[2].partition(":")[0]).read_text()
    assert boat.read_text() == "# example boat\n\n- Example boat: moored at pier 3.\n"


def test_an_owner_correction_of_a_family_line_leaves_no_private_words_on_it(tmp_path):
    """F1: the mark named the private fact file, whose name is the owner's `about` words, and the family room read it.
    The family room now sees `[superseded]` and nothing of the new line; the owner's recall still reads it in."""
    y = daemon(tmp_path, "example-room", "family-room")
    fam = remember(y, "family-room", "Example dinner: Sunday at six.")
    own = remember(y, "example-room", "Example dinner plan: moved to Saturday, keep it from the cousins.",
                   "example cousins surprise party", (fam["cite"],))
    assert own["superseded"] == [fam["cite"]] and own["cite"] == "tree/facts/example-cousins-surprise-party.md:3"
    seen = memory.read(y.memory, fam["cite"], lines=1, reader=y.reader("family-room"))["text"]
    assert seen.endswith(f"note: {memory.MARKS[memory.NOTE]} Example dinner: Sunday at six. [superseded] [c:room] " + seen.rpartition(" ")[2])
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    hits = asyncio.run(y.tool_recall("family-room", "example dinner Sunday", 6))["hits"]
    assert hits and not any(w in h["text"] for h in hits for w in ("facts/", "surprise", "Saturday", "cousins"))
    hits = asyncio.run(y.tool_recall("example-room", "example dinner Sunday", 6))["hits"]
    assert "moved to Saturday" in hits[0]["text"] and "Sunday at six. [superseded]" in hits[1]["text"]


def test_a_line_corrected_twice_has_the_live_one_read_in_first(tmp_path):
    """F4: live_first read in one hop, so the second line, itself superseded, stood first."""
    y = daemon(tmp_path)
    a = remember(y, "example-room", "Example lamp: the bulb is a warm glowbrick halogen.")
    b = remember(y, "example-room", "Example lamp: switched to an LED bulb.", "example lamp", (a["cite"],))
    c = remember(y, "example-room", "Example lamp: back to a filament bulb.", "example lamp", (b["cite"],))
    assert b["superseded"] == [a["cite"]] and c["superseded"] == [b["cite"]]
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    hits = asyncio.run(y.tool_recall("example-room", "glowbrick", 6))["hits"]
    assert hits[0]["cite"] == c["cite"] and "back to a filament bulb" in hits[0]["text"] and "glowbrick" in hits[1]["text"]
    assert not any("switched to an LED bulb" in h["text"] for h in hits)


def test_a_corrected_read_only_line_comes_back_with_its_replacement(tmp_path):
    """F5: a line in an `also` root is never marked, and here its replacement shares none of its words."""
    y = daemon(tmp_path)
    for n in range(40):                      # other lines, so six hits is a real cut
        remember(y, "example-room", f"Example note {n}: the household plant shelf item {n} is fine.", f"example item {n}")
    r = remember(y, "example-room", "Example plant: twice a week now.", "example plant", ("memory/pinned.md:5",))
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    hits = asyncio.run(y.tool_recall("example-room", "last watered on the 4th", 6))["hits"]
    assert any("twice a week now" in h["text"] for h in hits), [h["text"][:60] for h in hits]    # 709c6b5: the pinned line alone
    at = lambda words: next(i for i, h in enumerate(hits) if words in h["text"])  # noqa: E731
    assert at("twice a week now") < at("last watered on the 4th") and hits[at("twice a week now")]["cite"] == r["cite"]
    real = memory.read(y.memory, "memory/pinned.md:5", lines=1)["path"]      # by its real file: two copies share a cite (A1)
    assert memory.later(y.memory) == {f"{real}:5": r["cite"]}


def test_a_correction_marks_the_line_the_room_meant_never_the_snippets_first_line(tmp_path):
    """F3: recall cites a hit at its snippet's first line, here the file's heading, and remember marked that."""
    y = daemon(tmp_path)
    (y.memory.root / "facts").mkdir(parents=True)
    car = y.memory.root / "facts" / "example-car.md"
    car.write_text("# Example car\n\n- Example car: a blue hatchback with a roof rack.\n- Example car: parked on level 2 of "
                   "the garage.\n- Example car: the spare key is in the kitchen drawer.\n- Example car: insurance renews in March.\n")
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    hit = next(h for h in asyncio.run(y.tool_recall("example-room", "where is the spare key for the example car?", 6))["hits"]
               if "example-car" in h["cite"])
    assert hit["cite"] != "tree/facts/example-car.md:5" and "the spare key is in the kitchen drawer" in hit["text"]
    r = remember(y, "example-room", "Example car: the spare key is now in the hall bowl.", "example car",
                 (hit["cite"], "tree/facts/example-car.md:0", f"{hit['cite']} | Example car",
                  f"{hit['cite']} | the spare key is in the kitchen drawer"))
    assert r["superseded"] == ["tree/facts/example-car.md:5"] and len(r["refused"]) == 3
    assert "5 lines from there to the next 12 hold those words" in r["refused"][2]
    lines = car.read_text().splitlines()
    assert lines[4].endswith(f"[superseded by {r['cite']}]") and sum("[superseded" in x for x in lines) == 1
    assert lines[0] == "# Example car" and lines[6].startswith("- Example car: the spare key is now in the hall bowl. "
                                                               "(supersedes tree/facts/example-car.md:5) (")


def test_a_cite_past_a_vertical_tab_opens_its_own_line_and_a_correction_marks_that_line(tmp_path):
    """The index numbers a file's lines as str.splitlines does, which also breaks at a vertical tab or a form feed; read_memory
    and a correction's edit count the same way, so a cite opens on its own line, the mark lands there and every tab stays."""
    y = daemon(tmp_path)
    (y.memory.root / "facts").mkdir(parents=True)
    car = y.memory.root / "facts" / "example-car.md"
    car.write_text("# Example car\n\n" + "".join(f"- Example car: note {n}\x0bits second half.\n" for n in range(14))
                   + "\n# Keys\n\n- Example car: the spare key is in the kitchen drawer.\n")
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    hit = next(h for h in asyncio.run(y.tool_recall("example-room", "where is the spare key for the example car?", 6))["hits"]
               if "example-car" in h["cite"] and "spare key" in h["text"])
    got = memory.read(y.memory, hit["cite"], lines=1)
    assert got["ok"] and got["text"].strip() == hit["text"].splitlines()[0].strip()
    r = remember(y, "example-room", "The spare key is now in the hall bowl.", "",
                 (f"{hit['cite']} | the spare key is in the kitchen drawer",))
    assert r["superseded"] == ["tree/facts/example-car.md:34"] and r["refused"] == []
    text = car.read_text()
    assert text.splitlines()[33].endswith(f"[superseded by {r['cite']}]") and text.count("\x0b") == 14


# `due` and `owes` (2026-11-25): a filed commitment carries its date as a mark
NOTED = r"- \d\d:\d\d:\d\d[+-]\d\d:\d\d example-room#discord:CHANNEL_ID "


def dated(y: Yerkd, room: str, text: str, about: str = "", replaces: tuple[str, ...] = (), **kw: str) -> dict:
    return asyncio.run(y.tool_remember(room, text, about, list(replaces), **kw))


def line_at(y: Yerkd, cite: str) -> str:
    path, _, n = cite.rpartition(":")
    return (y.memory.root / path.removeprefix("tree/")).read_text().splitlines()[int(n) - 1]


def test_a_fact_filed_with_due_carries_its_mark_before_its_id_and_the_answer_names_the_date(tmp_path):
    y = daemon(tmp_path)
    r = dated(y, "example-room", "Example boiler: the yearly check.", "example boiler", due="2026-12-02")
    assert r["ok"] and r["cite"] == "tree/facts/example-boiler.md:3" and r["due"] == "2026-12-02" and "owes" not in r and list(r)[-1] == "next"
    line = line_at(y, r["cite"])
    assert re.fullmatch(r"- Example boiler: the yearly check\. \(example-room#discord:CHANNEL_ID, [^)]+\) \[due 2026-12-02\] \[m:m-1\]", line)
    got = memory.due_of(line)
    assert got and {k: got[k] for k in ("date", "time", "owes", "text", "role")} == {
        "date": "2026-12-02", "time": "", "owes": "", "text": "Example boiler: the yearly check.", "role": memory.NOTE}
    assert datetime.fromisoformat(got["filed"]).tzinfo is not None                        # when it was filed, its own offset
    assert "- [the room's note, not their words] Example boiler: the yearly check. (" in memory.read(y.memory, r["cite"], lines=1)["text"]
    assert "due" not in remember(y, "example-room", "Example boiler: in the cellar.", "example boiler")    # undated: as today
    assert re.fullmatch(r"- Example boiler: in the cellar\. " + SRC, line_at(y, "tree/facts/example-boiler.md:4"))


def test_the_day_log_path_carries_the_mark_before_the_class_and_id_and_a_said_line_carries_it(tmp_path):
    y = daemon(tmp_path)
    note = dated(y, "example-room", "Send the example form back.", due="2026-12-02T09:30")
    wait = dated(y, "example-room", "The example quote for the roof.", due="2026-12-02", owes="Example Roofers")
    said = dated(y, "example-room", "I will bring the key on the second", said="Dana", source="message m-9", due="2026-12-02", owes="Dana")
    assert (note["due"], wait["due"], wait["owes"], said["owes"]) == ("2026-12-02T09:30", "2026-12-02", "Example Roofers", "Dana")
    tail = r" \[c:private\] \[m:m-1\.d[0-9a-f]{8}\]"
    assert re.fullmatch(NOTED + r"note: Send the example form back\. \[due 2026-12-02T09:30\]" + tail, line_at(y, note["cite"]))
    assert re.fullmatch(NOTED + r"note: The example quote for the roof\. \[due 2026-12-02 owes Example Roofers\]" + tail, line_at(y, wait["cite"]))
    assert re.fullmatch(NOTED + r'said: Dana: "I will bring the key on the second" \(from message m-9\) \[due 2026-12-02 owes Dana\]' + tail,
                        line_at(y, said["cite"]))
    day = note["cite"].rpartition("/")[2].partition(".")[0]
    got = [memory.due_of(line_at(y, r["cite"]), day) for r in (note, wait, said)]
    assert [(x["date"], x["time"], x["owes"], x["text"], x["role"]) for x in got if x] == [
        ("2026-12-02", "09:30", "", "Send the example form back.", "note"),
        ("2026-12-02", "", "Example Roofers", "The example quote for the roof.", "note"),
        ("2026-12-02", "", "Dana", 'Dana: "I will bring the key on the second" (from message m-9)', "said")]
    parsed = memory.parse_line(line_at(y, wait["cite"]))
    assert parsed and (parsed.cls, parsed.rid, parsed.role) == ("private", "discord:CHANNEL_ID", "note")
    assert "[the room's note, not their words] Send the example form back. [due 2026-12-02T09:30]" in y.room_pack("example-room")[0]


def test_what_due_and_owes_refuse_each_in_one_line_and_files_nothing(tmp_path, monkeypatch):
    y = daemon(tmp_path)
    for bad in ("Friday", "2026-13-40", "2026-12-02 09:30", "2026-12-02T24:30", "2026-12-2", "2026-12-02T09:30:00", "2026-12-02\n", " 2026-12-02",
                "\u0662\u0660\u0662\u0666-12-02", "2026-12-02T09:30Z", "20261202"):
        r = dated(y, "example-room", "Send the example form back.", due=bad)
        assert r == {"ok": False, "error": words.DUE_SHAPE.format(due=memory.one_line(bad))}, bad
        assert "YYYY-MM-DD or YYYY-MM-DDTHH:MM" in r["error"] and "\n" not in r["error"], bad
    assert dated(y, "example-room", "A wait.", owes="Dana") == {"ok": False, "error": "owes needs due: a wait needs the day to look at it again"}
    for bad in ("Dana]", "[Dana", "Dana (the roofer)", "Dana\nand Riley"):
        assert dated(y, "example-room", "A wait.", due="2026-12-02", owes=bad) == {
            "ok": False, "error": words.DUE_NAME.format(owes=memory.one_line(bad))}, bad
    monkeypatch.setattr(mcp_server, "caller_room", lambda ctx: "example-room")
    server = mcp_server.build(y, y.port, y.hook)

    def call(**args: str) -> dict:
        got = asyncio.run(server.call_tool("remember", args))
        return json.loads((got[0] if isinstance(got, tuple) else got)[0].text)
    for args in ({"job": "example job"}, {"retire": "tree/rules.md:6"}):
        for more in ({"due": "2026-12-02"}, {"due": "2026-12-02", "owes": "Dana"}, {"owes": "Dana"}):
            assert call(text="Send the example form back.", **args, **more) == {"ok": False, "error": words.DUE_JOB}, (args, more)
    assert not y.memory.daily.exists() and not (y.memory.root / "facts").exists() and not (y.memory.root / memory.RULES).exists()
    ok = call(text="Send the example form back.", due="2020-01-05", owes=" Dana ")        # a thing told late, the name's spaces off
    assert ok["ok"] and ok["due"] == "2020-01-05" and ok["owes"] == "Dana" and "[due 2020-01-05 owes Dana] [c:" in line_at(y, ok["cite"])


def test_a_retried_call_with_the_same_date_files_nothing_twice_and_another_date_is_another_line(tmp_path):
    y = daemon(tmp_path)
    for about in ("example boiler", ""):
        text = "Example boiler: the yearly check."
        first = dated(y, "example-room", text, about, due="2026-12-02")
        assert dated(y, "example-room", text, about, due="2026-12-02")["cite"] == first["cite"]
        f = y.memory.root / first["cite"].rpartition(":")[0].removeprefix("tree/")
        n = len(f.read_text().splitlines())
        moved = dated(y, "example-room", text, about, due="2026-12-09")                       # the date is part of what was filed
        plain = dated(y, "example-room", text, about)
        assert len({first["cite"], moved["cite"], plain["cite"]}) == 3 and len(f.read_text().splitlines()) == n + 2
        assert dated(y, "example-room", text, about)["cite"] == plain["cite"]
        assert dated(y, "example-room", text, about, due="2026-12-09")["cite"] == moved["cite"] and len(f.read_text().splitlines()) == n + 2
        owed = dated(y, "example-room", text, about, due="2026-12-09", owes="Dana")
        assert owed["cite"] not in (first["cite"], moved["cite"], plain["cite"])
    ends = dated(y, "example-room", "Example words that end [due 2026-12-02]")                # words, filed with no date
    same = dated(y, "example-room", "Example words that end", due="2026-12-02")
    assert ends["cite"] != same["cite"] and len(line_at(y, same["cite"]).split("[due ")) == 2


def test_a_dated_fact_closed_since_is_filed_again_and_never_taken_for_a_retry(tmp_path):
    y = daemon(tmp_path)
    first = dated(y, "example-room", "Example boiler: the yearly check.", "example boiler", due="2026-12-02")
    done = dated(y, "example-room", "Example boiler: checked.", "example boiler", (first["cite"],))
    assert done["superseded"] == [first["cite"]]
    again = dated(y, "example-room", "Example boiler: the yearly check.", "example boiler", due="2026-12-02")
    assert again["cite"] == "tree/facts/example-boiler.md:5" and memory.mentions.OLD not in line_at(y, again["cite"])


def test_a_superseded_due_line_keeps_both_marks_parses_and_reads_as_blank_where_one_does_today(tmp_path):
    y = daemon(tmp_path)
    fact = dated(y, "example-room", "Example boiler: the yearly check.", "example boiler", due="2026-12-02")
    note = dated(y, "example-room", "Send the example form back by noon.", due="2026-12-02", owes="Dana")
    r = dated(y, "example-room", "Example boiler: checked, and the form is back.", "example boiler", (fact["cite"], note["cite"]))
    assert r["superseded"] == [fact["cite"], note["cite"]]
    a, b = line_at(y, fact["cite"]), line_at(y, note["cite"])
    assert a.endswith(f") [due 2026-12-02] [superseded by {r['cite']}] [m:m-1]")
    assert re.search(re.escape(f" [due 2026-12-02 owes Dana] [superseded by {r['cite']}] [c:private] [m:m-1.d") + r"[0-9a-f]{8}\]$", b)
    ln = memory.parse_line(b)
    assert ln and ln.role == "note" and ln.cls == "private" and ln.body.endswith(f"[superseded by {r['cite']}]")
    day = note["cite"].rpartition("/")[2].partition(".")[0]
    assert (memory.due_of(a) or {}).get("date") == (memory.due_of(b, day) or {}).get("date") == "2026-12-02"      # both marks read
    assert (memory.due_of(b, day) or {}).get("text") == "Send the example form back by noon."
    entries = [e["text"] for e in names(y).match("the example boiler")]
    assert entries and all("the yearly check" not in t for t in entries)                 # blank to mention recall
    assert "Send the example form back" not in y.room_pack("example-room")[0]            # and to the pack
    assert not y.recall_for("example-room", "send the example form back by noon")[1]     # and to the topic hit


def test_a_dated_line_is_open_until_it_is_marked_superseded_or_its_date_is_more_than_fourteen_days_past():
    src, today = "(example-room, 2026-11-01 09:00:00+00:00)", date(2026, 11, 25)
    line = "- Example form: send it back " + src + " [due {}]{} [m:m-1]"
    assert due.KEEP == 14
    for day, want in (("2026-12-02", True), ("2026-11-25", True), ("2026-11-11", True), ("2026-11-10", False), ("2025-11-25", False),
                      ("2026-11-11T09:00", True), ("2026-11-10T23:59", False), ("2026-11-11 owes Dana", True)):
        assert due.is_open(line.format(day, ""), today) is want, day
        assert due.is_open(line.format(day, " [superseded by tree/facts/example-form.md:4]"), today) is False, day
        assert due.is_open(line.format(day, " [superseded]"), today) is False, day
    note = "- 09:00:00+00:00 example-room#rid note: Send it back [due 2026-11-11]{} [c:private] [m:m-1.d0a1b2c3d]"
    assert due.is_open(note.format(""), today, "2026-11-01") and not due.is_open(note.format(" [superseded]"), today, "2026-11-01")


def test_the_characters_of_a_mark_in_a_lines_own_words_are_words_and_no_date():
    src, mark = "(example-room, 2026-11-01 09:00:00+00:00)", "[due 2026-12-02]"
    for raw in (f"- Example form: {mark} {src} [m:m-1]", f"- {mark} send it {src} [m:m-1]", f"- The line said x {src} {mark} [m:m-0] {src} [m:m-1]",
                f"- Example form: send it back {mark}", f"- Example form {mark} [m:m-1]", f"- Example form {src} [m:m-1] {mark}",
                f"- Example form {src} [due 2026-12-02 owes ] [m:m-1]", f"- Example form {src} [due soon] [m:m-1]"):
        assert memory.due_of(raw) is None and not due.is_open(raw, date(2026, 11, 25)), raw
    at = "- 09:00:00+00:00 example-room#rid "
    for raw in (f"{at}owner: send it {mark} [c:private] [m:1001]", f"{at}note: send it {mark} [c:private] [m:m-1.n0a1b2c3d]",
                f"{at}note: the mark {mark} in it [c:private] [m:m-1.d0a1b2c3d]", f"{at}owner@pat: {mark} [c:private] [m:m-1.d0a1b2c3d]",
                f"{at}note: retired: x {mark} [c:private] [m:m-1.r0a1b2c3d]", f"{at}note: send it [c:private] {mark} [m:m-1.d0a1b2c3d]",
                f"{at}note: send it [c:private] [m:m-1.d0a1b2c3d] {mark}"):
        assert memory.due_of(raw, "2026-11-01") is None and not due.is_open(raw, date(2026, 11, 25), "2026-11-01"), raw
    kept = memory.due_of(f"{at}note: it said [due 2026-12-09] then [due 2026-12-02] [c:private] [m:m-1.d0a1b2c3d]", "2026-11-01")
    assert kept and (kept["date"], kept["text"]) == ("2026-12-02", "it said [due 2026-12-09] then")          # the last is the mark
