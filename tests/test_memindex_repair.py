"""Checks that yerk-index, the program that keeps the memory's search index, heals itself: a recovered index file whose
keyword table lost its virtual table but kept the shadow tables starts again and finds what it found before, and a Python
FTS5 is told so in one line instead of a stack trace. Everything is under pytest's tmp_path."""

from __future__ import annotations

import sqlite3

import pytest

from test_memory import build, tree
from yerk_ai import memindex


def hits(db, word: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (word,)).fetchone()[0])
    finally:
        conn.close()


def test_a_recovered_file_with_orphan_shadow_tables_starts_and_finds_again(tmp_path) -> None:
    s = tree(tmp_path)
    build(s).close()
    before = hits(s.index_db, "kettle")
    assert before > 0
    conn = sqlite3.connect(s.index_db)            # what a recovery leaves: the shadow tables, no virtual table
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("DELETE FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts'")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.OperationalError):  # without the repair nothing can create it again
        conn = sqlite3.connect(s.index_db)
        try:
            conn.executescript("CREATE VIRTUAL TABLE chunks_fts USING fts5(body)")
        finally:
            conn.close()
    idx = memindex.Index(s.index_db, s.scope)      # yerk-index starts: drops the orphans, rebuilds from chunks
    idx.close()
    assert hits(s.index_db, "kettle") == before


@pytest.mark.parametrize(("message", "unavailable"), [("no such module: fts5", True),
                                                      ("no such tokenizer: trigram", True),
                                                      ("database is locked", False),
                                                      ("no such table: chunks", False)])
def test_fts5_unavailable_names_only_a_missing_module_or_tokenizer(message: str, unavailable: bool) -> None:
    assert memindex.fts5_unavailable(sqlite3.OperationalError(message)) is unavailable


def test_a_python_without_fts5_is_told_so_in_one_line(tmp_path, monkeypatch) -> None:
    s = tree(tmp_path)
    monkeypatch.setattr(memindex, "SCHEMA", "CREATE VIRTUAL TABLE IF NOT EXISTS x USING fts5_missing_module(a);")
    with pytest.raises(SystemExit, match="no FTS5 module"):
        memindex.Index(s.index_db, s.scope)
