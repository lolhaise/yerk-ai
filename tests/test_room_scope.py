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


def install(tmp_path, frozen: object = None, live: object = None) -> Yerkd:
    """Two read-only roots, each a curated file and a memory folder holding the same file names with their own words:
    `frozen-room` gets `frozen` as its `also` (default the snap pair), `live-room` gets `live` (default the live pair,
    written relative to the private folder), `example-room` sets none. The index and the name index are built."""
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
    (private / "rooms.toml").write_text(base + f"\n[rooms.frozen-room]\n{block}also = {json.dumps(frozen)}\n"
                                        f"\n[rooms.live-room]\n{block}also = {json.dumps(live)}\n")
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
    assert str(e.value) == (f"room frozen-room: also {tmp_path / 'a' / 'elsewhere'} is not in [memory] also in yerkd.toml, "
                            "so the install never indexes it")
    with pytest.raises(SystemExit) as e:                                         # Yerk's own tree is no read-only root
        install(tmp_path / "b", frozen=["memory"])
    assert str(e.value).startswith(f"room frozen-room: also {tmp_path / 'b' / 'private' / 'memory'} is not in [memory] also")
    with pytest.raises(SystemExit) as e:                                         # one string, not a list
        install(tmp_path / "c", frozen=str(tmp_path / "c" / "snap" / "memory"))
    assert str(e.value) == (f'room frozen-room: also must be a list of folders, like also = ["~/notes"], '
                            f"not {str(tmp_path / 'c' / 'snap' / 'memory')!r}")
