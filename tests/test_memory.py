"""Checks the memory, the day logs and the search index over them. The cases: the day's log (capture, dedupe),
the index (per-chunk updates, scope, freshness against the disk, timeout apart from failure), recall ranking on
recall bench and the manifest. Every value here is a placeholder."""
import json
import os
import sqlite3
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yerk_ai import memindex, memory
from yerk_ai.config import EXAMPLE_PRIVATE, REPO

UTC = timezone.utc


def tree(tmp_path: Path) -> memory.Settings:
    private = tmp_path / "private"
    (private / "memory" / "facts").mkdir(parents=True)
    (private / "memory" / "daily").mkdir(parents=True)
    (private / "memory" / "facts" / "kettle.md").write_text(
        "# Kitchen\n\nThe example kettle in an example room is painted teal.\n")
    (private / "memory" / "daily" / "2026-01-05.md").write_text(
        "# 2026-01-05\n\n- 09:00 a person: kettle kettle kettle kettle kettle\n"
        "- 09:01 a person: lunch at example.com with a person\n")
    (private / "memory" / "pinned.md").write_text("# Pinned\n\nA person prefers tea in the morning.\n")
    (private / "memory" / "open-loops.md").write_text("# Open loops\n\n- send a person the example file\n")
    return memory.settings(private, tmp_path / "state", {"timezone": "UTC"})


def build(s: memory.Settings) -> memindex.Index:
    idx = memindex.Index(s.index_db, s.scope)
    idx.sync()
    return idx


# capture
def test_capture_files_text_attachment_only_and_one_character_once_each(tmp_path):
    log = memory.DailyLog(tmp_path / "daily", UTC)
    at = datetime(2026, 1, 5, 9, 30, 15, tzinfo=UTC)
    assert log.capture("an-example-room", "101", "owner", "a plain line\nwith a second line", when=at)
    assert log.capture("an-example-room", "102", "test-webhook", "", [("photo.png", "image/png", 1234)], when=at)
    assert log.capture("an-example-room", "103", "owner", "k", when=at)
    assert not log.capture("an-example-room", "101", "owner", "a plain line\nwith a second line", when=at)
    again = memory.DailyLog(tmp_path / "daily", UTC)                 # a restart, then a replay
    assert not again.capture("an-example-room", "102", "test-webhook", "", [("photo.png", "image/png", 1234)], when=at)
    assert again.capture("an-example-room", "101", "owner", "the new text", when=at, edited=True)
    assert not again.capture("an-example-room", "101", "owner", "the new text", when=at, edited=True)
    text = (tmp_path / "daily" / "2026-01-05.md").read_text()
    assert text.splitlines() == [
        "# 2026-01-05", "",
        "- 09:30:15 an-example-room owner: a plain line / with a second line [m:101]",
        "- 09:30:15 an-example-room test-webhook: (attachment only) [m:102]",
        "  - attachment: photo.png, image/png, 1,234 bytes",
        "- 09:30:15 an-example-room owner: k [m:103]",
        text.splitlines()[-1]]
    assert text.splitlines()[-1].startswith("- 09:30:15 an-example-room owner: (edited) the new text [m:101.e")
    assert oct((tmp_path / "daily" / "2026-01-05.md").stat().st_mode & 0o777) == "0o600"


def test_the_day_is_the_messages_own_in_the_install_time_zone(tmp_path):
    log = memory.DailyLog(tmp_path / "daily", timezone(timedelta(hours=-5)))
    log.capture("an-example-room", "201", "owner", "late", when=datetime(2026, 1, 6, 4, 59, tzinfo=UTC))
    log.capture("an-example-room", "202", "owner", "replayed after midnight", when=datetime(2026, 1, 6, 4, 59, tzinfo=UTC))
    assert sorted(p.name for p in (tmp_path / "daily").iterdir()) == ["2026-01-05.md"]
    assert "- 23:59:00 an-example-room owner: late [m:201]" in (tmp_path / "daily" / "2026-01-05.md").read_text()


def test_the_example_layer_never_captures_into_the_repo(tmp_path):
    s = memory.settings(EXAMPLE_PRIVATE, tmp_path, {})
    assert s.root.is_relative_to(tmp_path) and not s.root.is_relative_to(REPO)
    assert (EXAMPLE_PRIVATE / "memory").resolve() in s.scope.roots


# the index
def test_appending_a_line_rewrites_one_chunk_and_a_touch_writes_none(tmp_path):
    s = tree(tmp_path)
    idx = build(s)
    day = s.root / "daily" / "2026-01-05.md"
    day.write_text("# 2026-01-05\n\n" + "".join(f"- 09:{i:02d} line number {i} of the log\n" for i in range(60)))
    idx.sync()
    before = dict(idx.conn.execute("SELECT id, sha FROM chunks WHERE path = ?", (str(day),)).fetchall())
    assert len(before) > 2
    with day.open("a") as f:
        f.write("- 10:00 one more line\n")
    st = idx.sync()
    assert (st["changed"], st["inserted"], st["deleted"]) == (1, 1, 1)
    after = dict(idx.conn.execute("SELECT id, sha FROM chunks WHERE path = ?", (str(day),)).fetchall())
    assert len(set(before.values()) & set(after.values())) == len(before) - 1   # only the last chunk changed
    os.utime(day, None)
    assert idx.sync()["inserted"] == 0
    day.unlink()
    st = idx.sync()
    assert st["gone"] == 1 and not idx.conn.execute("SELECT 1 FROM chunks WHERE path = ?", (str(day),)).fetchone()


def test_temp_backup_and_hidden_files_stay_out_of_the_index(tmp_path):
    s = tree(tmp_path)
    for rel in ("facts/kettle.pre-move.md", ".drafts/x.md", "overwrite-backups/pinned.md", "notes.md.tmp", "pinned.md.bak"):
        p = s.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("a copy that must not be indexed\n")
    files = s.scope.files()
    assert sorted(Path(p).relative_to(s.root).as_posix() for p in files) == [
        "daily/2026-01-05.md", "facts/kettle.md", "open-loops.md", "pinned.md"]
    with pytest.raises(ValueError):
        memindex.Scope(()).files()


def test_freshness_is_measured_against_the_disk(tmp_path):
    s = tree(tmp_path)
    assert memindex.check(s.index_db, s.scope)["status"] == "failure"          # no index yet
    idx = build(s)
    assert memindex.check(s.index_db, s.scope)["status"] == "fresh"
    pinned = s.root / "pinned.md"
    pinned.write_text("# Pinned\n\nchanged on disk\n")
    assert memindex.check(s.index_db, s.scope)["status"] == "pending"          # younger than the grace
    old = time.time() - 600
    os.utime(pinned, (old, old))
    r = memindex.check(s.index_db, s.scope)
    assert r["status"] == "stale" and r["stale"] == 1 and r["examples"] == [str(pinned)]
    idx.sync()
    assert memindex.check(s.index_db, s.scope)["status"] == "fresh"
    wider = memindex.Scope((*s.scope.roots, tmp_path / "elsewhere"))
    assert memindex.check(s.index_db, wider)["status"] == "scope-changed"
    assert memindex.check(s.index_db, s.scope, deadline_s=-1)["status"] == "timeout"
    idx.close()
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"this is not a database" * 100)
    assert memindex.check(bad, s.scope)["status"] == "failure"
    assert memindex.CHECK_EXIT["timeout"] != memindex.CHECK_EXIT["failure"]


def test_recall_ranks_the_fact_above_a_chunk_that_repeats_one_word(tmp_path):
    s = tree(tmp_path)
    build(s)
    r = memory.recall(s, "What colour is the example kettle in the example room?")
    assert r["ok"] and r["hits"][0]["path"].endswith("facts/kettle.md")
    assert r["hits"][0]["line"] == 1 and r["hits"][0]["cite"] == "memory/facts/kettle.md:1"
    assert "painted teal" in r["hits"][0]["text"]
    assert memory.recall(s, "the of and")["hits"] == []


def test_recall_says_timeout_and_failure_apart(tmp_path, monkeypatch):
    s = tree(tmp_path)
    assert memory.recall(s, "kettle")["status"] == "failure"                   # no index file
    build(s)

    def busy(*a, **k):
        raise sqlite3.OperationalError("interrupted")
    monkeypatch.setattr(memindex, "search", busy)
    r = memory.recall(s, "kettle")
    assert r["status"] == "timeout" and "not down" in r["error"]

    def broken(*a, **k):
        raise sqlite3.DatabaseError("file is not a database")
    monkeypatch.setattr(memindex, "search", broken)
    assert memory.recall(s, "kettle")["status"] == "failure"


# read_memory
def test_read_memory_reads_exact_lines_and_only_inside_the_tree(tmp_path):
    s = tree(tmp_path)
    r = memory.read(s, "facts/kettle.md", 3, 1)
    assert r["ok"] and r["text"] == "The example kettle in an example room is painted teal." and r["total_lines"] == 3
    assert memory.read(s, str(s.root / "pinned.md"), 1, 400)["ok"]
    outside = tmp_path / "private" / "secret.md"
    outside.write_text("not memory\n")
    (s.root / ".hidden").mkdir()
    (s.root / ".hidden" / "x.md").write_text("hidden\n")
    (s.root / "facts" / "link.md").symlink_to(outside)
    for path in ("../secret.md", str(outside), "facts/link.md", ".hidden/x.md", "/etc/hostname"):
        r = memory.read(s, path)
        assert not r["ok"] and ("outside the memory tree" in r["error"] or "no such file" in r["error"]), path
    assert not memory.read(s, "facts")["ok"] and not memory.read(s, "facts/missing.md")["ok"]


# the bench and the manifest
def test_the_bench_scores_top_six_and_top_one_and_flags_a_blind_control(tmp_path):
    s = tree(tmp_path)
    build(s)
    q = tmp_path / "questions.json"
    q.write_text(json.dumps({"questions": [
        {"id": "c1", "q": "what colour is the example kettle", "answer_tokens": ["teal"], "control": True},
        {"id": "q1", "q": "what does a person prefer in the morning", "answer_tokens": ["tea", "morning"]},
        {"id": "q2", "q": "who had lunch at example.com", "answer_tokens": ["lunch", "a person"]},
        {"id": "q3", "q": "what is the example lamp", "answer_tokens": ["lamp", "shelf"]}]}))
    r = memory.bench(s, q)
    assert r["ok"] and r["questions"] == 3 and r["blind"] == []
    assert (r["top_k"], r["top_1"]) == (2, 2) and r["top_k_rate"] == round(2 / 3, 3)
    q.write_text(json.dumps({"questions": [{"id": "c2", "q": "the example lamp", "answer_tokens": ["lamp"], "control": True}]}))
    assert memory.bench(s, q)["blind"] == ["c2"]


def test_the_manifest_declares_every_memory_ability_with_a_tier():
    abilities = tomllib.loads((REPO / "abilities.toml").read_text())["abilities"]
    assert {"capture", "memory-index", "recall", "read-memory", "recall-bench"} <= set(abilities)
    for name, a in abilities.items():
        assert a["tier"] in {"free", "key", "paid"} and a["what"] and a["needs"] and a["free_path"], name


def test_the_stall_line_fires_when_the_loop_is_blocked(caplog):
    """The control for the daemon's stall detector: a blocked loop is said, so a quiet log means quiet."""
    import asyncio
    import logging

    from yerk_ai.daemon import Yerkd

    async def go():
        watch = asyncio.create_task(Yerkd.watch_loop(None, every=0.05))  # type: ignore[arg-type]
        await asyncio.sleep(0.06)
        time.sleep(0.4)                                                  # the loop held, as index work on it would
        await asyncio.sleep(0.1)
        watch.cancel()
    with caplog.at_level(logging.WARNING, logger="yerk.daemon"):
        asyncio.run(go())
    assert any("stall: the event loop was blocked" in r.getMessage() for r in caplog.records)
