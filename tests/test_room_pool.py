"""Checks that a room that may not read everything (a room for another person, a sealed room, a room with its own `also`) searches
only the chunks of memory it may read: where your notes name a person in many chunks, the room's own lines on that person still reach
search, and nothing it may not read reaches it. An owner room's search is the one it was. Every value here is a placeholder."""
import time
from datetime import datetime, timezone

from yerk_ai import memindex, memory

AT = datetime(2026, 1, 20, 10, 0, tzinfo=timezone.utc)
OWNER, PERSON = "discord:OWNER_ID", "discord:PERSON_ID"
SEALED = memory.Reader("person-room", PERSON, sealed=True)      # sealed, with a person in it: its own lines only
PLAIN = memory.Reader("person-room", PERSON)                    # a person room: its own lines and shared files
OWNS = memory.Reader("owner-room", OWNER, owner=True)


def tree(tmp_path, crowd: int = 200, word: str = "Alex") -> memory.Settings:
    """The owner's notes, an `also` root no room lists, name `word` in `crowd` short chunks, each above any other in bm25;
    person-room's own fact file and its day-log line name Alex, as do an owner line and a line in `mine`, the root owner-room
    lists as its own `also`. The index is built."""
    private, other, mine = tmp_path / "private", tmp_path / "other", tmp_path / "mine"
    for d in (private / "memory" / "facts", private / "memory" / "daily", other, mine):
        d.mkdir(parents=True)
    (other / "notes.md").write_text("".join(f"## {word} {n}\n\n{word}, {word}: the owner's note {n}.\n\n" for n in range(crowd)))
    (mine / "notes.md").write_text("# Notes\n\n- Alex keeps the example ladder in the blue shed by the gate.\n")
    (private / "memory" / "facts" / "beta.md").write_text(
        f"---\nclass: room\nroom: {PERSON}\n---\n# Beta\n\n- Beta's brother Alex lives by the example lake.\n")
    (private / "memory" / "daily" / "2026-01-20.md").write_text("# 2026-01-20\n\n" + "\n".join([
        memory.format_line(AT, "person-room", PERSON, "person", "beta", "Alex comes to the example picnic on Sunday", "room", "p1"),
        memory.format_line(AT, "owner-room", OWNER, "owner", "", "Alex owes me the example kayak", "private", "o1")]) + "\n")
    s = memory.settings(private, tmp_path / "state", {"timezone": "UTC", "also": [str(other), str(mine)]})
    memindex.Index(s.index_db, s.scope).sync()
    return s


def texts(r: dict) -> str:
    assert r["ok"], r
    return "\n".join(h["text"] for h in r["hits"])


def test_a_sealed_room_finds_its_own_lines_on_a_name_the_owners_notes_hold_in_200_chunks(tmp_path):
    """Nov 12: a sealed room with a person in it searched for a relative's name and was told nothing was on file, though its own
    file names her: its 80 candidates were the owner's, drawn from the whole index, and its filter left none."""
    got = texts(memory.recall(tree(tmp_path), "Alex", 6, reader=SEALED))
    assert "example lake" in got and "example picnic" in got


def test_a_person_room_and_an_owner_room_with_its_own_also_find_their_own_lines_the_same_way(tmp_path):
    s = tree(tmp_path)
    got = texts(memory.recall(s, "Alex", 6, reader=PLAIN))
    assert "example lake" in got and "example picnic" in got
    own = s.only(tmp_path / "private", "owner-room", [str(tmp_path / "mine")])
    got = texts(memory.recall(own, "Alex", 6, reader=OWNS))
    assert "blue shed" in got and "owner's note" not in got


def test_the_room_still_gets_nothing_it_may_not_read_nor_a_line_of_a_chunk_it_reads_in_part(tmp_path):
    """The filters stay the gate, applied before the pool: no hit, no snippet, no line of a chunk that holds a line of the
    room's beside one it may not read (the chunker keeps a day log's rooms apart; the index is held to it all the same)."""
    s = tree(tmp_path)
    conn = memindex.connect(s.index_db, readonly=False)
    conn.execute("INSERT INTO chunks (path, lo, hi, sha, body) VALUES (?, 9, 10, 'mixed', ?)", (str(s.daily / "2026-01-20.md"), "\n".join([
        memory.format_line(AT, "person-room", PERSON, "person", "beta", "Alex and the example canoe", "room", "p2"),
        memory.format_line(AT, "owner-room", OWNER, "owner", "", "Alex and the example vault code", "private", "o2")])))
    conn.close()
    for rd in (SEALED, PLAIN):
        for got in (texts(memory.recall(s, "Alex", 20, reader=rd)), texts(memory.ranked(s, "Alex", 20, reader=rd))):
            assert "example lake" in got and "example picnic" in got
            assert not [w for w in ("owner's note", "kayak", "canoe", "vault") if w in got]


def test_past_the_bound_the_read_stops_inside_its_deadline_as_a_search_that_found_nothing(tmp_path, monkeypatch):
    """More matches than the bound, none of them the room's: the read stops at the bound, inside its deadline, and answers no
    hit; a read its deadline cuts answers a timeout, never no hit (lines_on reads that as no absence)."""
    s = tree(tmp_path, memory.POOL * 2 + memory.READ_ON + 40, "kayak")
    seen, real = [], memory.allowed_text
    monkeypatch.setattr(memory, "allowed_text", lambda *a, **k: seen.append(1) or real(*a, **k))
    t = time.monotonic()
    assert memory.ranked(s, "kayak", 8, 2.0, PLAIN, wide=False) == {"ok": True, "status": "ok", "hits": []}
    assert time.monotonic() - t < 2.0 and len(seen) == memory.POOL * 2 + memory.READ_ON     # the bound, and not a row past it
    monkeypatch.setattr(memory, "allowed_text", lambda *a, **k: time.sleep(0.002) or real(*a, **k))
    t = time.monotonic()
    assert memory.ranked(s, "kayak", 8, 0.3, PLAIN, wide=False)["status"] == "timeout" and time.monotonic() - t < 1.5


MAIN = {"Alex": [f"other/notes.md:{n}" for n in (1, 5, 9, 13, 17, 21)],       # the owner's hits on origin/main (c705590f)
        "Alex example picnic lake kayak": ["memory/facts/beta.md:5", "memory/daily/2026-01-20.md:4", "memory/daily/2026-01-20.md:1",
                                           "mine/notes.md:1", "other/notes.md:1", "other/notes.md:5"]}


def test_an_owner_rooms_search_is_the_read_it_was_with_the_same_hits_in_the_same_order(tmp_path, monkeypatch):
    """The owner reads everything: no filter, no row past the pool, the hits main gives; and a room's read with a filter that keeps
    every row gives the owner's candidates in the owner's order."""
    s = tree(tmp_path)
    conn = memindex.connect(s.index_db, readonly=True)
    for q in ("Alex", "example lake picnic", "owner's note 7"):
        assert memindex.search(conn, q, memory.POOL, memory.POOL * 2) == memindex.search(
            conn, q, memory.POOL, memory.POOL * 2, keep=lambda p, t: True, most=memory.READ_ON)
    conn.close()
    asked, real = [], memindex.search
    monkeypatch.setattr(memindex, "search", lambda *a, **k: asked.append(k.get("keep")) or real(*a, **k))
    for q, main in MAIN.items():              # as origin/main ranks them, for an owner room and for no room alike
        assert [h["cite"] for h in memory.recall(s, q, 6, reader=OWNS)["hits"]] == [h["cite"] for h in memory.recall(s, q, 6)["hits"]] == main
    assert asked == [None] * 4                 # no filter, so the read origin/main makes: LIMIT the pool, no row past it


def test_a_room_asks_the_meaning_search_for_its_most_rows_and_keeps_only_its_own(tmp_path, monkeypatch):
    """The meaning half as far as yerk-index serves (400 rows): a room that may not read everything asks for that many and keeps
    those it may read; an owner room asks for the pool, as before."""
    s = tree(tmp_path)
    conn = memindex.connect(s.index_db, readonly=True)
    rows = conn.execute("SELECT id, path, lo, body FROM chunks ORDER BY instr(body, 'example lake') > 0, id").fetchall()
    conn.close()
    near = [{"id": i, "path": p, "line": lo, "text": b, "score": 0.9 - 0.0001 * n} for n, (i, p, lo, b) in enumerate(rows)]
    asked = []
    monkeypatch.setattr(memory, "meaning", lambda s, q, pool=memory.POOL, **k: asked.append(pool) or ("ok", near[:pool]))
    assert "example lake" in texts(memory.recall(s, "a relative near the shore", 6, reader=PLAIN))      # no keyword of its line
    assert "example lake" not in texts(memory.recall(s, "a relative near the shore", 6, reader=OWNS))   # the owner's 80 are its notes
    assert asked == [memory.MEANING_MOST, memory.POOL]
