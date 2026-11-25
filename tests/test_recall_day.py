"""Checks searching memory by day: the `recall` tool's `since` and `until` keep a search to the files named for a day in that range.
The range is applied before either half of the search (keywords and meaning) cuts its pool, an empty range is searched once more
is a placeholder."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from yerk_ai import memindex, memory, words

from test_room_scope import sync

UTC = timezone.utc
DAY = "2026-01-05"


def layer(tmp_path: Path, daily: dict[str, str] | None = None, also: tuple[Path, ...] = (), **files: str) -> memory.Settings:
    """A private layer with the day logs in `daily` (day: its lines), the other `files` (path, with __ for a slash, no .md), indexed."""
    private = tmp_path / "private"
    (private / "memory" / "facts").mkdir(parents=True)
    (private / "memory" / "daily").mkdir()
    for day, text in (daily or {}).items():
        (private / "memory" / "daily" / f"{day}.md").write_text(f"# {day}\n\n{text}\n")
    for name, text in files.items():
        (private / "memory" / f"{name.replace('__', '/')}.md").write_text(text)
    s = memory.settings(private, tmp_path / "state", {"timezone": "UTC", "also": [str(p) for p in also]})
    reindex(s)
    return s


def reindex(s: memory.Settings) -> None:
    idx = memindex.Index(s.index_db, s.scope)
    idx.sync()
    idx.close()


def cites(r: dict) -> list[str]:
    """Each hit's file, sorted."""
    assert r["ok"], r
    return sorted({h["cite"].rpartition(":")[0].rpartition("/")[2] for h in r["hits"]})


def three_days(tmp_path: Path) -> memory.Settings:
    return layer(tmp_path, {d: f"- 09:00 a person: the kettle on the {w} shelf"
                            for d, w in (("2026-01-04", "low"), (DAY, "middle"), ("2026-01-06", "high"))})


# R2, R3: the candidates, in SQL, before the pool
def test_a_days_line_is_found_under_its_day_when_200_chunks_outrank_it(tmp_path):
    """A filter after the pool (160 by bm25) would find nothing of the day's: 200 short chunks said the word more often."""
    crowd = "\n".join(f"## note {n}\n\nkettle kettle kettle {n}" for n in range(200))
    s = layer(tmp_path, {"2026-01-04": crowd, DAY: "- 09:00 a person: " + "some other words of the day, " * 12 + "a kettle was teal"})
    assert DAY + ".md" not in cites(memory.recall(s, "kettle", 20))                        # as it ranks with no range
    r = memory.recall(s, "kettle", 6, since=DAY, until=DAY)
    assert cites(r) == [f"{DAY}.md"] and "teal" in r["hits"][0]["text"] and "widened" not in r
    conn = memindex.connect(s.index_db, readonly=True)
    try:                                                                                  # the pool of one is the day's own
        got = memindex.search(conn, "kettle", 1, 1, only=lambda p: memory.in_range(p, DAY, DAY))
        assert [Path(h["path"]).name for h in got] == [f"{DAY}.md"]
        assert memindex.search(conn, "kettle", 1, 1, only=lambda p: False) == []
    finally:
        conn.close()


@pytest.mark.parametrize("since, until, found", [
    (DAY, "", [DAY, "2026-01-06"]), ("", DAY, ["2026-01-04", DAY]), (DAY, DAY, [DAY]),
    ("2026-01-04", DAY, ["2026-01-04", DAY]), (DAY, "2026-01-06", [DAY, "2026-01-06"]),
    ("2026-01-04", "2026-01-06", ["2026-01-04", DAY, "2026-01-06"]), ("", "", ["2026-01-04", DAY, "2026-01-06"])])
def test_since_and_until_come_alone_or_together_and_both_ends_are_in_the_range(tmp_path, since, until, found):
    r = memory.recall(three_days(tmp_path), "kettle shelf", since=since, until=until)
    assert cites(r) == [f"{d}.md" for d in found] and "widened" not in r


def test_a_file_with_no_day_in_its_name_is_out_of_a_range_and_in_without_one(tmp_path):
    s = layer(tmp_path, {DAY: "- 09:00 a person: the kettle of the day"}, facts__kettle="# Kettle\n\n- The kettle is teal.\n",
              pinned="# Pinned\n\nThe kettle is pinned.\n")
    (s.docs / "claude").mkdir()
    (s.docs / "claude" / "guide.md").write_text("---\nclass: shared\n---\n# Guide\n\nA kettle in the docs.\n")
    reindex(s)
    assert cites(memory.recall(s, "kettle")) == [f"{DAY}.md", "guide.md", "kettle.md", "pinned.md"]
    for since, until in ((DAY, DAY), ("2000-01-01", ""), ("", "9999-12-31"), ("2000-01-01", "9999-12-31")):
        assert cites(memory.recall(s, "kettle", since=since, until=until)) == [f"{DAY}.md"]


def test_the_day_is_read_from_the_files_own_name_never_its_time_on_disk_or_its_folder(tmp_path):
    also = tmp_path / "notes"
    (also / "2026-01-05").mkdir(parents=True)
    names = {f"{DAY}.md": "in", f"{DAY}-standup.md": "in", f"standup-{DAY}T09.md": "in", f"week-2026-01-01-to-{DAY}.md": "in",
             "undated.md": "out", "2026-01-05/inside.md": "out", "2026-01-06.md": "out", "12026-01-05.md": "out",
             "2026-01-050.md": "out", "2026-13-05.md": "out", "2026-01.md": "out", "20260105.md": "out"}
    at = datetime(2026, 1, 5, 12, tzinfo=UTC).timestamp()
    for name in names:
        (also / name).write_text(f"# Note\n\nThe kettle in {name}.\n")
        os.utime(also / name, (at, at))                               # every one last changed on the day asked for
    s = layer(tmp_path, {"2026-01-04": "- 09:00 a person: the kettle, the day before"}, also=(also,))
    os.utime(s.daily / "2026-01-04.md", (at, at))
    reindex(s)
    found = cites(memory.recall(s, "kettle", 20, since=DAY, until=DAY))
    assert found == sorted(n for n, where in names.items() if where == "in")
    assert len(cites(memory.recall(s, "kettle", 20))) == len(names) + 1                  # with no range, every one
    assert memory.named_days(f"/a/2026-01-01/week-2026-01-02-to-{DAY}.md") == ["2026-01-02", DAY]
    assert memory.named_days("/a/2026-02-30.md") == memory.named_days(f"/a/{DAY}/plain.md") == []


# R1: what is taken and what is refused
@pytest.mark.parametrize("given", ["Friday", "2026-13-40", "2026-12-2", "2026-12-02T09:00", "2026-12-02 09:00", " 2026-12-02", "2026-12-02\n",
                                   "2026-02-30", "20261202", "2026-W48-3", "0000-01-01", "２０２６-12-02", "12026-12-02", "2026-12-02\nand more"])
def test_any_other_shape_is_refused_in_one_line_naming_the_shape_and_nothing_is_searched(tmp_path, monkeypatch, given):
    s = three_days(tmp_path)
    monkeypatch.setattr(memindex, "search", lambda *a, **k: pytest.fail("a refused range was searched"))
    monkeypatch.setattr(memory, "meaning", lambda *a, **k: pytest.fail("a refused range was searched"))
    for name, kw in (("since", {"since": given}), ("until", {"until": given}), ("since", {"since": given, "until": DAY}),
                     ("until", {"since": DAY, "until": given})):
        r = memory.recall(s, "kettle", **kw)
        assert not r["ok"] and r["status"] == "refused" and "hits" not in r and "widened" not in r
        assert r["error"].startswith(f"{name} is YYYY-MM-DD, one real day, not \"") and len(r["error"].splitlines()) == 1
    assert "Friday" in memory.recall(s, "kettle", since="Friday")["error"] or given != "Friday"


def test_since_after_until_is_refused_in_one_line_naming_the_shape(tmp_path):
    s = three_days(tmp_path)
    r = memory.recall(s, "kettle", since="2026-01-06", until=DAY)
    assert r == {"ok": False, "status": "refused",
                 "error": f"since 2026-01-06 is after until {DAY}: each is YYYY-MM-DD, since on or before until"}


# R4: an empty range widens once and says so
def test_an_empty_range_is_searched_once_more_without_it_and_says_so(tmp_path, monkeypatch):
    s, runs = three_days(tmp_path), []
    real = memory.ranked
    monkeypatch.setattr(memory, "ranked", lambda *a, **k: runs.append((k.get("since", ""), k.get("until", ""))) or real(*a, **k))
    plain = memory.recall(s, "kettle shelf")
    assert runs == [("", "")] and "widened" not in plain and "line" not in plain
    for since, until, said in (("2025-01-01", "2025-01-01", "(2025-01-01)"), ("2025-01-01", "2025-01-31", "(from 2025-01-01 until 2025-01-31)"),
                               ("2027-01-01", "", "(from 2027-01-01)"), ("", "2025-01-31", "(until 2025-01-31)")):
        runs.clear()
        r = memory.recall(s, "kettle shelf", since=since, until=until)
        assert runs == [(since, until), ("", "")]                                # once, and without the range
        assert r["hits"] == plain["hits"] and r["widened"] is True and r["ok"] and r["status"] == "ok"
        assert r["line"] == words.RECALL_WIDENED.format(days=said.strip("()")) and said in r["line"] and len(r["line"].splitlines()) == 1
        assert "Nothing was found in that range" in r["line"] and "from any time" in r["line"]
        assert list(r).index("line") < list(r).index("hits")                     # read before the hits it is about
    runs.clear()
    none = memory.recall(s, "zeppelin", since="2025-01-01", until="2025-01-01")        # nor anything at any time: once, and said
    assert none["hits"] == [] and none["widened"] is True and len(runs) == 2


def test_a_range_with_one_hit_never_widens(tmp_path, monkeypatch):
    s, runs = three_days(tmp_path), []
    real = memory.ranked
    monkeypatch.setattr(memory, "ranked", lambda *a, **k: runs.append(1) or real(*a, **k))
    r = memory.recall(s, "kettle shelf", since=DAY, until=DAY)
    assert len(r["hits"]) == 1 and len(runs) == 1 and sorted(r) == ["hits", "ok", "status"]


def test_a_search_that_did_not_finish_is_no_empty_range(tmp_path, monkeypatch):
    s, runs = three_days(tmp_path), []
    real, late = memory.ranked, {"ok": False, "status": "timeout", "error": "busy"}
    monkeypatch.setattr(memory, "ranked", lambda *a, **k: runs.append(1) or late)
    assert memory.recall(s, "kettle", since="2025-01-01", until="2025-01-01") == late and len(runs) == 1
    monkeypatch.setattr(memory, "ranked", lambda *a, **k: real(*a, **k) if k.get("since") else late)      # the widened one did not
    assert memory.recall(s, "kettle", since="2025-01-01", until="2025-01-01") == late


# R3: the meaning half
def test_the_meaning_half_fuses_only_rows_in_the_range_from_its_larger_pool(tmp_path, monkeypatch):
    s, asked, fused = three_days(tmp_path), [], []
    (s.root / "facts" / "kettle.md").write_text("# Kettle\n\n- A vessel.\n")
    reindex(s)
    conn = sqlite3.connect(s.index_db)
    rows = [{"id": i, "path": p, "line": lo, "text": b, "score": 0.9} for i, p, lo, b in conn.execute("SELECT id, path, lo, body FROM chunks")]
    conn.close()
    monkeypatch.setattr(memory, "meaning", lambda s, q, pool=memory.POOL, deadline_s=0: asked.append(pool) or ("ok", [dict(h) for h in rows]))
    real = memory.fuse
    monkeypatch.setattr(memory, "fuse", lambda kw, vec, *a, **k: fused.append([Path(h["path"]).name for h in vec]) or real(kw, vec, *a, **k))
    r = memory.recall(s, "an unrelated ask", since=DAY, until=DAY)                  # no keyword: the meaning half alone
    assert cites(r) == [f"{DAY}.md"] and set(fused[-1]) == {f"{DAY}.md"} and asked == [memory.MEANING_MOST] and "widened" not in r
    assert len(cites(memory.recall(s, "an unrelated ask"))) == 4 and asked[-1] == memory.POOL and len(set(fused[-1])) == 4
    r = memory.recall(s, "an unrelated ask", since="2025-01-01", until="2025-01-01")             # none in range: widened, and whole
    assert r["widened"] and len(cites(r)) == 4 and fused[-2] == [] and asked[-2:] == [memory.MEANING_MOST, memory.POOL]
