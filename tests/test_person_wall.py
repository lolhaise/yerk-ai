"""Checks, through the real search index, that a room you run for another person finds nothing of yours, and that what it files is found by it and your
rooms only. From the person-room-parity cold read (2026-10-23): an owner room's picture sidecar, an upload there that says it is shared, its day-log line
and its fact file hold one placeholder word; a person's room asking for it through recall (keywords, and a meaning half that hands back every chunk), the
per-message recall, the pack, read_memory and `seen` gets nothing of it, while the owner's rooms find it. A person's room's own sidecar is found by it
other person's room, whatever class rooms.toml gives that room's lines. Every value here is a placeholder (decision 22)."""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone

from test_pictures import picture

from yerk_ai import memindex, memory, mentions, pictures, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel

HIS, HERS = "zorblax", "plimsoll"         # the owner's things hold only the first, her room's sidecar only the second
PERSON = ('\n[rooms.{name}]\nchannel = "console:{name}"\naccounts = ["one", "two"]\nmodel = "claude-fable-5-1"\ntools = ["Read"]\n'
          'privacy = "family"\npeople = [{who}]\ntimezone = "UTC"\nlayer = "layers/{name}"\npersona = "persona.md"\n'
          'rulebook = "rulebook.md"\n{extra}')


def install(tmp_path, monkeypatch, extra: str = "") -> Yerkd:
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    for name in ("her-room", "next-room"):               # a person's room has a layer of its own (context.walls)
        (lay := priv / "layers" / name).mkdir(parents=True)
        for f in ("persona.md", "rulebook.md"):
            shutil.copy(priv / f, lay / f)
    with (priv / "rooms.toml").open("a") as f:
        f.write(PERSON.format(name="her-room", who=1001, extra=extra) + PERSON.format(name="next-room", who=1002, extra=extra))
    state.mkdir()
    mine, hers = picture(state, "example-room", "m-1"), picture(state, "her-room", "m-2")
    y = Yerkd(priv, state)
    for name in ("example-room", "her-room", "next-room"):
        y.add_room(name, ConsoleChannel())
    s = y.memory
    pictures.file("example-room", mine, f"{HIS} receipt EX-42", f"a page with the word {HIS}", s.tz)
    (mine.parent / "shared.md").write_text(f"---\nclass: shared\n---\nThe {HIS} plan, sent to his room.\n")   # says it is shared
    asyncio.run(y.tool_seen("her-room", str(hers), f"{HERS} ticket EX-7", f"a ticket with the word {HERS}"))
    (s.root / "facts").mkdir(parents=True, exist_ok=True)
    (s.root / "facts" / f"{HIS}.md").write_text(f"# {HIS}\n\n- The {HIS} code is EX-99.\n")
    at = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    y.daily.capture("example-room", "o-1", "owner", f"the {HIS} deal is on for Friday", when=at)
    memindex.Index(s.index_db, s.scope).sync()
    mentions.Builder(s.mentions, s.roots_for_mentions, s.curated, s.mentions_exclude).refresh(s.scope.files())

    def every_chunk(_s, _q, **_k):            # yerk-index's meaning half has no class filter: it may hand back any chunk
        conn = memindex.connect(s.index_db, readonly=True)
        try:
            rows = conn.execute("SELECT id, path, lo, body FROM chunks").fetchall()
        finally:
            conn.close()
        return "ok", [{"id": i, "path": p, "line": lo, "text": b, "score": 0.9} for i, p, lo, b in rows]
    monkeypatch.setattr(memory, "meaning", every_chunk)
    return y


def seen_by(y: Yerkd, room: str, word: str) -> list[str]:
    """Every surface a room gets memory through, as text: recall (the tool's own search), the per-message recall (names, deep
    lines, topics, the past search), the pack, and a read of each place the word is filed."""
    out = [str(h) for h in y.search(room, f"{word} receipt plan code deal ticket", 20).get("hits", [])]
    names, topics, past, absent = y.recall_for(room, f"what did we say about {word} last week? who is {word}?")
    out += [str(x) for x in (*names, *topics, *past)]
    out += [y.room_pack(room)[0]]
    for cite in ("example-room/work/inbox/m-1/photo.seen.txt", "example-room/work/inbox/m-1/shared.md",
                 "her-room/work/inbox/m-2/photo.seen.txt", f"facts/{word}.md"):
        r = asyncio.run(y.tool_read_memory(room, cite, 1, 50))
        out += [r["text"]] if r["ok"] else []
    return [x for x in out if word in x]


def test_a_persons_room_gets_nothing_of_the_owners_and_its_own_reaches_it_and_his_rooms_only(tmp_path, monkeypatch):
    y = install(tmp_path, monkeypatch)
    assert seen_by(y, "example-room", HIS), "the fixture is live: his room finds his words"
    assert seen_by(y, "her-room", HIS) == [] and seen_by(y, "next-room", HIS) == []      # nothing of the owner's, even "shared"
    assert seen_by(y, "her-room", HERS) and seen_by(y, "example-room", HERS)              # hers: her room and the owner's (decision 44)
    assert seen_by(y, "next-room", HERS) == []                                            # no other person's room
    mine = y.state / "rooms" / "example-room" / "work" / "inbox" / "m-1" / "photo.png"
    before = pictures.sidecar(mine).read_text()
    assert asyncio.run(y.tool_seen("her-room", str(mine), "x", "y")) == {"ok": False, "error": words.SEEN_NOT_HERE.format(name="photo.png")}
    assert pictures.sidecar(mine).read_text() == before                                   # `seen` never writes beside the owner's


def test_a_person_rooms_uploads_stay_its_own_whatever_class_rooms_toml_gives_its_lines(tmp_path, monkeypatch):
    """The inbox is class room with the room's id for every person's room: a `class = "shared"` on the room (its lines for every
    room) never opens what was sent to it to another person's room."""
    y = install(tmp_path, monkeypatch, extra='class = "shared"\n')
    assert memory.file_class(y.state.resolve() / "rooms" / "her-room" / "work" / "inbox" / "m-2" / "photo.seen.txt") == (
        "room", "console:her-room")
    assert seen_by(y, "her-room", HERS) and seen_by(y, "example-room", HERS) and seen_by(y, "next-room", HERS) == []
    assert seen_by(y, "her-room", HIS) == []
