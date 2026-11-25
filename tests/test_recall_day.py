"""Checks searching memory by day: the `recall` tool's `since` and `until` keep a search to the files named for a day in that range.
The range is applied before either half of the search (keywords and meaning) cuts its pool, an empty range is searched once more
is a placeholder."""
from datetime import timezone
from pathlib import Path

import pytest

from yerk_ai import memindex, memory

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
