"""Checks meaning search, which finds memory lines by what they mean as well as by their words. The cases: only
changed chunks are embedded (a touch embeds none), a missing key keeps the keyword path and the room hears it
once, a provider timeout and a provider error each fall back and read apart, fusion ranks a paraphrase above a
keyword-only near miss, the index stays queryable during a backfill, the bench's output shape is unchanged, and
the manifest and `yerk abilities` show the ability both ways. The pin maps the keyword tables, keeps every lock
a writer pass's new chunks at its next tick, and goes off only where SQLite has no dbstat. No provider
is ever called: a stub stands in. Every value here is a placeholder."""
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from yerk_ai import abilities, cli, embeddings, memindex, memory
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import NO_KEY_LINE, Yerkd
from fake_channel import ConsoleChannel

CONCEPTS = ["kettle", "colour", "lunch", "weather", "book"]
WORDS = {"kettle": "kettle", "teapot": "kettle", "pot": "kettle", "boil": "kettle", "teal": "colour", "painted": "colour",
         "colour": "colour", "shade": "colour", "hue": "colour", "lunch": "lunch", "ate": "lunch", "meal": "lunch",
         "rain": "weather", "weather": "weather", "forecast": "weather", "book": "book", "novel": "book", "reading": "book"}


class StubEmbedder(embeddings.Embedder):
    """A provider that never leaves the box: a text's vector is its mix of five concepts, from a word
    table, plus a little of everything so no two texts are orthogonal."""

    def __init__(self, key_file: Path, delay: float = 0.0):
        super().__init__(key_file, model="stub", dim=len(CONCEPTS) + 1)
        self.calls: list[list[str]] = []
        self.fail: Exception | None = None
        self.delay = delay

    def embed(self, texts: list[str], timeout: float | None = None, retries: int | None = None) -> np.ndarray:
        if not self.available():
            raise embeddings.ProviderError("no provider key")
        if self.fail:
            raise self.fail
        if self.delay:
            time.sleep(self.delay)
        self.calls.append(list(texts))
        out = np.full((len(texts), self.dim), 0.0, dtype=np.float32)
        for i, t in enumerate(texts):
            for w in memindex.WORD.findall(memindex.norm(t)):
                if w in WORDS:
                    out[i, CONCEPTS.index(WORDS[w])] += 1.0
            out[i, -1] = 0.2
        return out / np.linalg.norm(out, axis=1, keepdims=True)


def tree(tmp_path: Path, key: bool = True) -> memory.Settings:
    private = tmp_path / "private"
    (private / "memory" / "facts").mkdir(parents=True)
    (private / "memory" / "daily").mkdir(parents=True)
    (private / "memory" / "facts" / "kettle.md").write_text(
        "# Kitchen\n\nThe example kettle in an example room is painted teal.\n")
    (private / "memory" / "daily" / "2026-01-05.md").write_text(
        "# 2026-01-05\n\n- 09:00 a person: kettle kettle kettle kettle kettle\n"
        "- 09:01 a person: lunch at example.com with a person\n")
    (private / "memory" / "pinned.md").write_text("# Pinned\n\nA person prefers tea in the morning.\n")
    if key:
        (private / "secrets").mkdir(mode=0o700)
        (private / "secrets" / "openai.key").write_text("placeholder-not-a-key\n")
    return memory.settings(private, tmp_path / "state", {"timezone": "UTC"})


def serve(s: memory.Settings, stub: StubEmbedder, units: str = "lines"):
    """yerk-index's pieces in-process: the writer, the vector store, the search server on a free port,
    the backfill (stepped by hand) and a `status` that writes index.json as the process would."""
    store = embeddings.Vectors(stub.dim)
    back = embeddings.Backfill(s.index_db, memindex.live, store, stub, gap=0.0, units=units)
    idx = memindex.Index(s.index_db, s.scope, on_insert=back.note)
    idx.sync()
    server = embeddings.Server(store, stub, s.index_db, memindex.live)
    port = server.start()

    def status() -> None:
        embeddings.write_status(s.index_status, port=port, chunks=0, embeddings={
            "key": stub.available(), "model": stub.model, "dim": stub.dim, "vectors": store.n})
    status()
    return idx, store, server, back, status


def vectors_in(s: memory.Settings) -> int:
    conn = sqlite3.connect(f"file:{memindex.live(s.index_db)}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT count(*) FROM vectors").fetchone()[0])
    finally:
        conn.close()


# 1. a vector per line and per bullet; only changed lines are embedded
def test_a_line_or_bullet_gets_its_own_vector_and_a_touch_embeds_none(tmp_path):
    s = tree(tmp_path)
    stub = StubEmbedder(s.embeddings_key)
    idx, store, server, back, status = serve(s, stub)
    while back.step():
        pass
    units = {r[0] for r in idx.conn.execute("SELECT DISTINCT sha FROM units")}
    chunks = {r[0] for r in idx.conn.execute("SELECT DISTINCT sha FROM chunks")}
    embedded = [t for call in stub.calls for t in call]
    # the day log is one chunk of two lines: each line has a vector of its own, the chunk none
    assert len(embedded) == len(units) == store.n == vectors_in(s) == len(chunks) + 1 > 3
    assert all("\n" not in t for t in embedded) and any(t.startswith("- 09:01 ") for t in embedded)
    day = s.root / "daily" / "2026-01-05.md"
    calls = len(stub.calls)
    os.utime(day, None)                                              # a touch: the chunker finds the same text
    idx.sync()
    assert back.step() == 0 and len(stub.calls) == calls and store.n == len(units)
    with day.open("a") as f:
        f.write("- 10:00 a person: the forecast says rain\n")       # one line: one new unit, the chunk's old lines keep theirs
    idx.sync()
    assert back.step() == 1 and len(stub.calls) == calls + 1 and len(stub.calls[-1]) == 1
    assert "forecast says rain" in stub.calls[-1][0] and store.n == len(units) + 1
    assert back.step() == 0 and len(stub.calls) == calls + 1         # nothing else changed
    # the meaning search maps a line's vector to its chunk and keeps the best line's cosine
    r = server.search("will it rain", 10)
    assert r["ok"] and r["hits"] and r["hits"][0]["path"].endswith("2026-01-05.md") and "forecast" in r["hits"][0]["text"]
    assert len({h["id"] for h in r["hits"]}) == len(r["hits"])        # one row per chunk
    server.stop()
    idx.close()


# 2. a missing key: the keyword path, and the room hears it once
def test_a_missing_key_keeps_the_keyword_path_and_the_room_hears_it_once(tmp_path, capsys):
    s = tree(tmp_path, key=False)
    stub = StubEmbedder(s.embeddings_key)
    idx, store, server, back, status = serve(s, stub)
    assert not stub.available() and memory.meaning_state(s) == "no-key"
    assert memory.meaning(s, "the example kettle") == ("no-key", [])
    r = memory.ranked(s, "What colour is the example kettle in the example room?")
    assert r["ok"] and r["hits"][0]["path"].endswith("facts/kettle.md") and r["hits"][0]["score"] > 1   # the keyword cover, as before
    assert sorted(r["hits"][0]) == ["cite", "line", "lines", "path", "score", "text"]
    assert sorted(memory.recall(s, "What colour is the example kettle in the example room?")["hits"][0]) == ["cite", "text"]
    assert stub.calls == []
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path / "state")
    y.add_room("example-room", ConsoleChannel())
    a = asyncio.run(y.tool_recall("example-room", "example kettle", 6))
    b = asyncio.run(y.tool_recall("example-room", "example kettle", 6))
    out = capsys.readouterr().out
    assert a["ok"] and b["ok"] and out.count(json.dumps(NO_KEY_LINE, ensure_ascii=False)) == 1   # once, as one notice
    assert (tmp_path / "state" / "rooms" / "example-room" / "told-embeddings").exists()
    server.stop()
    idx.close()


# 3. a provider timeout and a provider error each fall back and read apart from the index's own failure
def test_a_provider_timeout_and_a_provider_error_fall_back_and_read_apart(tmp_path, caplog):
    s = tree(tmp_path)
    stub = StubEmbedder(s.embeddings_key)
    idx, store, server, back, status = serve(s, stub)
    while back.step():
        pass
    status()
    assert memory.meaning_state(s) == "ok"
    stub.fail = embeddings.ProviderTimeout("no answer from the provider in 4 s")
    assert memory.meaning(s, "example kettle")[0] == "provider-timeout"
    with caplog.at_level(logging.WARNING, logger="yerk.memory"):
        r = memory.ranked(s, "What colour is the example kettle in the example room?")
    assert r["ok"] and r["status"] == "ok" and r["hits"][0]["path"].endswith("facts/kettle.md")
    assert "provider-timeout" in caplog.text and "keywords only" in caplog.text
    stub.fail = embeddings.ProviderError("HTTP 401 from the provider: bad key sk-[removed]")
    assert memory.meaning(s, "example kettle")[0] == "provider-error"
    assert memory.recall(s, "example kettle")["ok"]
    stub.fail = None
    assert memory.meaning(s, "example kettle")[0] == "ok"
    server.stop()                                                    # yerk-index gone: not a provider matter
    assert memory.meaning(s, "example kettle")[0] == "no-index"
    assert memory.recall(s, "example kettle")["ok"]
    os.replace(memindex.live(s.index_db), tmp_path / "away.sqlite")  # the index itself gone: a failure, as before
    assert memory.recall(s, "example kettle")["status"] == "failure"
    idx.close()


# 4. fusion ranks a paraphrase above a keyword-only near miss
def test_fusion_ranks_a_paraphrase_above_a_keyword_only_near_miss(tmp_path):
    s = tree(tmp_path)
    (s.root / "facts" / "kettle.md").write_text(
        "# Kitchen\n\nThe teapot we boil water in is painted teal.\n")            # the paraphrase: none of the query's words
    (s.root / "daily" / "2026-01-05.md").write_text(
        "# 2026-01-05\n\n- 09:00 a person: lunch lunch meal ate rain weather forecast book novel reading, "
        "and the example kettle colour\n")                                        # the near miss: the query's words, another topic
    (s.root / "facts" / "paint.md").write_text("# Paint\n\nThe example kettle colour is teal, painted last spring.\n")
    stub = StubEmbedder(s.embeddings_key)
    idx, store, server, back, status = serve(s, stub)
    while back.step():
        pass
    status()
    keywords = memindex.search(sqlite3.connect(f"file:{memindex.live(s.index_db)}?mode=ro", uri=True),
                               "what colour is the example kettle", 6, 80)
    assert [Path(h["path"]).name for h in keywords][:2] == ["paint.md", "2026-01-05.md"]   # keywords alone miss the paraphrase
    r = memory.ranked(s, "what colour is the example kettle")
    assert [Path(h["path"]).name for h in r["hits"]][:3] == ["paint.md", "kettle.md", "2026-01-05.md"]
    assert r["hits"][0]["score"] > r["hits"][1]["score"] >= r["hits"][2]["score"]
    server.stop()
    idx.close()


# 5. the index stays queryable during a backfill
def test_the_index_answers_throughout_a_backfill(tmp_path):
    s = tree(tmp_path)
    for n in range(120):
        (s.root / "facts" / f"f{n}.md").write_text(f"# F{n}\n\n- example teapot number {n} is painted teal\n" * 3)
    stub = StubEmbedder(s.embeddings_key, delay=0.01)
    idx, store, server, back, status = serve(s, stub)
    back.batch = 8
    status()
    back.start()
    results, seen = [], set()
    end = time.monotonic() + 1.0
    while time.monotonic() < end and (store.n < 120 or len(results) < 5):
        results.append(memory.recall(s, "what colour is the example kettle", 3))
        seen.add(store.n)
        status()
    # the second is the proof that recall answers while the store grows; the backfill itself finishes in
    # its own time (a shared runner embedded 84 of 120 in that second, 2026-10-18), so wait for it, still asking
    deadline = time.monotonic() + 30.0
    while store.n < 120 and time.monotonic() < deadline:
        results.append(memory.recall(s, "what colour is the example kettle", 3))
        seen.add(store.n)
        status()
        time.sleep(0.05)
    back.stop.set()
    back.thread.join(5.0)
    assert results and all(r["ok"] for r in results), [r for r in results if not r["ok"]][:1]
    assert len(seen) > 2 and store.n >= 120 and vectors_in(s) == store.n   # it grew while recall kept answering
    assert memory.meaning(s, "the hue of the teapot")[0] == "ok"
    server.stop()
    idx.close()


# 6. the bench's output shape is unchanged, on the example set with its two room-recall kinds
def test_the_bench_shape_is_unchanged_and_the_example_set_has_the_room_kinds(tmp_path, capsys):
    s = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state", {"timezone": "UTC"})
    memindex.Index(s.index_db, s.scope).sync()
    qfile = EXAMPLE_PRIVATE / "evals" / "recall-questions.json"
    kinds = [q["kind"] for q in json.loads(qfile.read_text())["questions"]]
    assert "room-recent" in kinds and "room-day" in kinds
    r = memory.bench(s, qfile)
    assert sorted(r) == ["blind", "controls", "file", "k", "kept", "lost", "ok", "questions", "rows", "top_1", "top_1_rate",
                         "top_k", "top_k_rate"]
    assert all(sorted(row) == ["control", "id", "kept", "rank", "top"] for row in r["rows"])
    assert r["kept"] == sum(1 for row in r["rows"] if row["kept"] and not row["control"]) and r["kept"] + len(r["lost"]) == r["top_k"]
    assert r["ok"] and r["blind"] == [] and r["controls"] == 1 and r["top_k"] >= 6
    assert cli.main(["recall-bench", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path / "state"),
                     "--questions", str(qfile)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"top 6: \d+/\d+ \(\d+\.\d%\)   top one: \d+/\d+ \(\d+\.\d%\)   controls: \d+   "
                        r"answer in the snippet: \d+/\d+", out[0]), out[0]
    assert out[1].startswith("misses: ")
    assert sorted(json.loads((tmp_path / "state" / "memory" / "recall-bench.jsonl").read_text().splitlines()[-1])) == [
        "at", "blind", "controls", "k", "kept", "lost", "misses", "questions", "top_1", "top_1_rate", "top_k", "top_k_rate"]


# 6b. the bench is a ratchet: a planted drop exits 4 and says so; a dated note in budgets.toml naming it lets the run pass
def test_a_planted_drop_fails_the_bench_unless_a_dated_note_names_it(tmp_path, capsys, monkeypatch):
    s = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state", {"timezone": "UTC"})
    memindex.Index(s.index_db, s.scope).sync()
    args = ["recall-bench", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path / "state"),
            "--questions", str(EXAMPLE_PRIVATE / "evals" / "recall-questions.json")]
    assert cli.main(args) == 0
    series = tmp_path / "state" / "memory" / "recall-bench.jsonl"
    row = json.loads(series.read_text().splitlines()[-1])
    best = row["top_k"] + 2                                                 # four runs before, each two over this one
    series.write_text("".join(json.dumps({**row, "top_k": best}) + "\n" for _ in range(4)))
    capsys.readouterr()
    assert cli.main(args) == 4
    assert f"RATCHET: top 6 fell to {row['top_k']} of {row['questions']} from {best} in the 4 runs before" in capsys.readouterr().err
    monkeypatch.setattr(cli, "budgets", lambda: {"recall": {"slack": 1, "drops": [f"2026-10-22: {best} to {row['top_k']}, planted"]}})
    assert cli.main(args) == 0 and "RATCHET" not in capsys.readouterr().err


# 7. the manifest, and `yerk abilities` and `yerk explain` both ways
def test_the_manifest_and_the_abilities_command_show_the_key_tier_both_ways(tmp_path, capsys):
    ab = abilities.manifest()
    e = ab["embeddings"]
    assert e["tier"] == "key" and e["provider"] == "OpenAI" and "$" in e["price"] and "read 2026" in e["price"]
    assert e["free_path"].startswith("keywords") and ab["recall"]["upgrade"] == "embeddings"
    s = tree(tmp_path)
    assert cli.main(["abilities", "--private", str(s.root.parent), "--state", str(tmp_path / "state")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "🟢 embeddings: on" in lines and "🟢 recall: on" in lines and "⚪ voice: known, not built" in lines
    s.embeddings_key.unlink()
    assert cli.main(["abilities", "--private", str(s.root.parent), "--state", str(tmp_path / "state")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "🟠 embeddings: needs OpenAI" in lines and "⚪ recall: runs on the free path" in lines
    assert cli.main(["explain", "embeddings", "--private", str(s.root.parent), "--state", str(tmp_path / "state")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("🟠 embeddings: needs OpenAI\n") and "Cost: $0.02 per 1M tokens" in out
    assert "Free path: keywords" in out and "How to add the key: put the key in the file" in out and "/home/" not in out
    assert cli.main(["explain", "no-such-ability", "--private", str(s.root.parent)]) == 2


# 9. the merge keeps each search's own first hits in the top k
def test_the_merge_keeps_each_searchs_own_top_three():
    def row(cid: int, score: float) -> dict:
        return {"id": cid, "path": f"/t/memory/daily/2026-01-0{cid % 9}.md", "line": 1, "lines": "1-1", "text": f"t{cid}", "score": score}
    shared = [50, 51, 52, 53, 54, 55, 56]                                   # chunks both searches place at ranks 4 to 10
    keywords = [row(1, 3.0), row(2, 2.9), row(3, 2.8)] + [row(c, 2.0) for c in shared]
    vectors = [row(101, 0.9), row(102, 0.89), row(103, 0.88)] + [row(c, 0.8) for c in shared]
    plain = [h["id"] for h in memory.fuse(keywords, vectors, 6, keep=0)]
    assert plain == [50, 51, 52, 53, 54, 55]                                # plain RRF: no search's own top hit survives
    kept = [h["id"] for h in memory.fuse(keywords, vectors, 6, keep=3)]
    assert set(kept) == {1, 2, 3, 101, 102, 103} and kept[0] in (1, 101)            # each search's first three, in fused order
    one = [h["id"] for h in memory.fuse(keywords, vectors, 6, keep=1)]
    assert one == [50, 51, 52, 53, 101, 1]                                  # one slot each; the rest by fused score
    # overlap: a chunk in both top threes is held once and the free slots fill
    vectors2 = [row(1, 0.9), row(102, 0.89), row(103, 0.88)] + [row(c, 0.8) for c in shared]
    two = [h["id"] for h in memory.fuse(keywords, vectors2, 6, keep=3)]
    assert two[0] == 1 and set(two) == {1, 2, 3, 102, 103, 50}
    # without vectors, the keyword list as it is
    assert [h["id"] for h in memory.fuse(keywords, [], 6, keep=3)] == [1, 2, 3, 50, 51, 52]


# 9b. a stored message counts RECORD beside a note: an import's line, a day of the install's own log past the window
def test_a_stored_message_ranks_after_a_note_on_the_same_ranks(tmp_path):
    s = memory.settings(tmp_path / "private", tmp_path / "state", {"timezone": "UTC"})
    weight = memory.record(s, datetime(2026, 10, 22, 12, tzinfo=timezone.utc))

    def hit(cid: int, path: Path) -> dict:
        return {"id": cid, "path": str(path), "line": 1, "lines": "1-1", "text": f"t{cid}", "score": 0.9}
    old, recent = s.daily / "2026-10-01.md", s.daily / "2026-10-20.md"
    mail, note = tmp_path / "private" / "imports" / "mail" / "2026-01.md", s.root / "facts" / "kettle.md"
    assert [weight(hit(1, p)) for p in (old, mail, recent, note)] == [memory.RECORD, memory.RECORD, 1.0, 1.0]
    both = [hit(1, old), hit(2, note)]                                      # the old day first in both lists, the note second
    assert [h["id"] for h in memory.fuse(both, both, 6, keep=0)] == [1, 2]
    assert [h["id"] for h in memory.fuse(both, both, 6, keep=0, weight=weight)] == [2, 1]     # the note leads; the day is kept
    assert [h["id"] for h in memory.fuse(both, [], 6, keep=0, weight=weight)] == [2, 1]       # and so on the exact half alone


# 9c. with the meaning half out (off, down or late) the exact half alone ranks a note before an old record on the same words:
# three of the install's 13 recall controls were found only through the meaning half until it did (gate-live-step)
def test_with_the_meaning_half_out_a_note_still_ranks_before_an_old_day_on_the_same_words(tmp_path):
    s = tree(tmp_path, key=False)
    memindex.Index(s.index_db, s.scope).sync()
    hits = memory.ranked(s, "kettle", 6, wide=False)["hits"]
    assert [Path(h["path"]).name for h in hits] == ["kettle.md", "2026-01-05.md"]     # the day's five kettles led on bm25


# 9d. deep recall (lines_on, decision 42) reads a name on the exact half alone, unweighed: 9c's note-first order is the recall tool's.
# Two old day logs name Examplia six times and a note once, so the two orders keep different lines; deep recall's are main's (ed8044a,
# before gate-live-step) field for field. On the bare example layer every name's lines are the same either way: too few to cut.
def test_deep_recall_keeps_the_index_order_while_the_recall_tool_puts_the_note_first(tmp_path):
    shutil.copytree(EXAMPLE_PRIVATE, priv := tmp_path / "private")
    (mem := priv / "memory").joinpath("facts").mkdir()
    (mem / "facts" / "examplia.md").write_text("# Examplia (example, invented)\n\n- Examplia: the example neighbour two doors down, who keeps the example hedge trimmed.\n")
    for day, verbs in (("2026-01-02", ("lent", "fixed", "returned")), ("2026-01-03", ("borrowed", "painted", "stored"))):
        (mem / "daily" / f"{day}.md").write_text(f"# {day} (example, invented)\n\n" + "".join(f"- 1{i}:00 Examplia {v} the example ladder.\n" for i, v in enumerate(verbs)))
    s = memory.settings(priv, tmp_path / "state", {"timezone": "UTC"})
    memindex.Index(s.index_db, s.scope).sync()
    ladder = [("memory/daily/2026-01-03.md:5", "2026-01-03", 5, "01-03 12:00 Examplia stored the example ladder."),
              ("memory/daily/2026-01-03.md:4", "2026-01-03", 4, "01-03 11:00 Examplia painted the example ladder."),
              ("memory/daily/2026-01-03.md:3", "2026-01-03", 3, "01-03 10:00 Examplia borrowed the example ladder."),
              ("memory/daily/2026-01-02.md:5", "2026-01-02", 5, "01-02 12:00 Examplia returned the example ladder."),
              ("memory/daily/2026-01-02.md:4", "2026-01-02", 4, "01-02 11:00 Examplia fixed the example ladder."),
              ("memory/daily/2026-01-02.md:3", "2026-01-02", 3, "01-02 10:00 Examplia lent the example ladder.")]
    main = {"Examplia": ladder, "example ladder": ladder,                       # the six day lines; weighed, the note's two would come in
            "library card": [("memory/open-loops.md:3", "", 3, "Renew the library card before the 20th. surface: remind on the 15th."),
                             ("memory/daily/2026-01-05.md:4", "2026-01-05", 4, "01-05 14:40 Set a reminder to renew the library card before the 20th.")],
            "reading list": [("memory/open-loops.md:4", "", 4, "Suggest the next book from the reading list. surface: next time the owner asks about reading."),
                             ("memory/daily/2026-01-06.md:4", "2026-01-06", 4, "01-06 18:05 Owner finished the book from the reading list and wants the next one.")],
            "shopping list": [("memory/daily/2026-01-07.md:7", "2026-01-07", 7, "01-07 12:26 yerk: Filed it. The hinge is on the shopping list under batteries."),
                              ("memory/daily/2026-01-07.md:4", "2026-01-07", 4, "01-07 12:20 Drafted a shopping list: rice, lemons, coffee, batteries.")]}
    for what, want in main.items():
        got = memory.lines_on(s, what) or memory.lines_on(s, what, None, 20)       # as the daemon calls it
        assert [(x["cite"], x["day"], x["n"], x["text"]) for x in got] == want
        assert all(list(x) == ["cite", "at", "day", "n", "text"] and x["at"] == f"{priv}/{x['cite']}" for x in got)
    assert [h["cite"] for h in memory.ranked(s, "Examplia", 6, wide=False)["hits"]] == [
        "memory/facts/examplia.md:1", "memory/daily/2026-01-03.md:1", "memory/daily/2026-01-02.md:1"]     # the deploy's controls
    assert memory.recall(s, "Examplia")["hits"][0]["cite"].startswith("memory/facts/examplia.md:")         # and a room's recall


# 9e. the keyword rank's part of the fusion is SHARE, the meaning rank's the rest (0.4: on the install's 164 questions top six 103 to
# 107, top one 59 to 62); at 0.5 both count alike, as before. A lone keyword first hit against a lone meaning second hit:
def test_the_meaning_rank_counts_more_than_the_keyword_rank_in_the_fusion():
    def row(cid: int, score: float) -> dict:
        return {"id": cid, "path": f"/t/memory/facts/f{cid}.md", "line": 1, "lines": "1-1", "text": f"t{cid}", "score": score}
    keywords, vectors = [row(1, 3.0)], [row(101, 0.9), row(102, 0.8)]
    fused = memory.fuse(keywords, vectors, 6, keep=0)
    assert [(h["id"], h["score"]) for h in fused] == [(101, 0.0571), (102, 0.0545), (1, 0.0381)]     # 1.2/21, 1.2/22, 0.8/21
    even = memory.fuse(keywords, vectors, 6, keep=0, share=0.5)
    assert [(h["id"], h["score"]) for h in even] == [(101, 0.0476), (1, 0.0476), (102, 0.0455)]       # 1/21 each, the cosine breaks the tie
    assert memory.fuse(keywords, [], 6, keep=0, share=0.1) == keywords      # the exact half alone (deep recall) has no share to read


# 10. the manifest's embeddings entry records the fit choice: the dimensions, the units, the width
def test_the_manifest_records_the_fit_choice():
    ab = abilities.manifest()["embeddings"]
    assert "256 dimensions" in ab["model"] and "a vector per line" in ab["model"] and "embeddings_width" in ab["model"]
    assert "vectors" in ab["model"] and "every line" in ab["what"]


# 11. a model change re-embeds every served vector once and nothing twice; a narrower width keeps the bench shape
def test_a_model_change_re_embeds_once_and_a_narrower_width_keeps_the_shape(tmp_path):
    s = tree(tmp_path)
    stub = StubEmbedder(s.embeddings_key)
    idx, store, server, back, status = serve(s, stub, units="both")
    while back.step():
        pass
    first = store.n
    assert first == vectors_in(s) > 0
    # a second model on the same index: its backfill embeds every unit and chunk once; the first model's vectors stay
    stub2 = StubEmbedder(s.embeddings_key)
    stub2.model = "stub-large"
    store2 = embeddings.Vectors(stub2.dim, width=3)
    back2 = embeddings.Backfill(s.index_db, memindex.live, store2, stub2, gap=0.0, units="both")
    while back2.step():
        pass
    assert sum(len(c) for c in stub2.calls) == store2.n == first
    assert back2.step() == 0 and sum(len(c) for c in stub2.calls) == first            # nothing twice
    assert vectors_in(s) == 2 * first and store.n == first
    conn = sqlite3.connect(f"file:{memindex.live(s.index_db)}?mode=ro", uri=True)
    assert dict(conn.execute("SELECT model, count(*) FROM vectors GROUP BY model").fetchall()) == {"stub": first, "stub-large": first}
    conn.close()
    # the narrower width: the same stored vectors, the first 3 of 6 dimensions renormalised; search and bench keep their shape
    assert store2.width == 3 and store2.load(idx.conn, "stub-large", "both") == first and store2.blocks[0].shape[1] == 3
    server2 = embeddings.Server(store2, stub2, s.index_db, memindex.live)
    embeddings.write_status(s.index_status, port=server2.start(), chunks=0,
                            embeddings={"key": True, "model": "stub-large", "dim": 6, "vectors": store2.n})
    r = memory.recall(s, "what colour is the example kettle")
    assert r["ok"] and sorted(r["hits"][0]) == ["cite", "text"]
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps({"questions": [
        {"id": "q1", "kind": "fact", "q": "what colour is the example kettle", "answer_tokens": ["teal"]},
        {"id": "c1", "kind": "fact", "control": True, "q": "example kettle", "answer_tokens": ["kettle"]}]}))
    b = memory.bench(s, qfile)
    assert sorted(b) == ["blind", "controls", "file", "k", "kept", "lost", "ok", "questions", "rows", "top_1", "top_1_rate",
                         "top_k", "top_k_rate"]
    assert b["blind"] == [] and b["top_k"] == 1
    server2.stop()
    server.stop()
    idx.close()


# 12. the stall line stays silent while a backfill runs: the embedding thread never holds the loop that serves rooms
def test_the_stall_line_stays_silent_during_a_backfill(tmp_path, caplog):
    from yerk_ai.beat import Beat
    from yerk_ai.ledger import Ledger
    s = tree(tmp_path)
    for n in range(60):
        (s.root / "facts" / f"f{n}.md").write_text(f"# F{n}\n\n- example teapot number {n} is painted teal\n")
    stub = StubEmbedder(s.embeddings_key, delay=0.02)
    idx, store, server, back, status = serve(s, stub)
    back.batch = 4

    async def go():
        watch = asyncio.create_task(Beat(Ledger(tmp_path / "beat.sqlite"), tmp_path / "stalls", every=0.05).tick())
        back.start()
        end = time.monotonic() + 5.0
        while store.n < 60 and time.monotonic() < end:
            await asyncio.sleep(0.05)
        back.stop.set()
        watch.cancel()
    with caplog.at_level(logging.WARNING, logger="yerk.beat"):
        asyncio.run(go())
    back.thread.join(5.0)
    assert store.n >= 60 and len(stub.calls) >= 15
    assert not any("stall" in r.getMessage() for r in caplog.records)
    server.stop()
    idx.close()


# 13. the matched line (`near`) only when the request asks: a daemon from before the diet never asks, and its room
# filter would hand a non-owner room another room's line unfiltered
def test_a_search_carries_the_matched_line_only_when_the_request_asks(tmp_path):
    s = tree(tmp_path)
    stub = StubEmbedder(s.embeddings_key)
    idx, store, server, back, status = serve(s, stub)
    while back.step():
        pass
    status()

    def post(payload: dict) -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}/search", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    line = "- 09:01 a person: lunch at example.com with a person"
    old = post({"query": "the meal", "pool": 10})                             # what a daemon before the diet sends
    assert old["ok"] and old["hits"] and all("near" not in h for h in old["hits"])
    new = post({"query": "the meal", "pool": 10, "near": True})
    assert new["hits"][0]["path"].endswith("2026-01-05.md") and new["hits"][0]["near"] == line
    assert [{k: v for k, v in h.items() if k != "near"} for h in new["hits"]] == old["hits"]     # nothing else moves
    assert all("near" not in h for h in post({"query": "the meal", "pool": 10, "near": "yes"})["hits"])   # true only
    assert all("near" not in h for h in server.search("the meal", 10)["hits"])
    state, rows = memory.meaning(s, "the meal")                               # this daemon asks
    assert state == "ok" and rows[0]["near"] == line
    server.stop()
    idx.close()


class CappedEmbedder(StubEmbedder):
    """A provider with a cap on a request, counted the worst way the bound allows: a token for every UTF-8 byte (the
    real tokenizer, byte-level BPE, makes fewer). Past the cap it answers the 400 the real one gave on 2026-10-21."""

    def __init__(self, key_file: Path, cap: int):
        super().__init__(key_file)
        self.cap, self.sizes, self.rejected = cap, list[int](), list[frozenset[str]]()

    def embed(self, texts: list[str], timeout: float | None = None, retries: int | None = None) -> np.ndarray:
        self.sizes.append(size := sum(len(t.encode()) for t in texts))
        if size > self.cap:
            self.rejected.append(frozenset(texts))
            raise embeddings.ProviderError('HTTP 400 from the provider: {\n  "error": {\n    "message": "Invalid \'input\': '
                                           f'maximum request size is {self.cap} tokens per request.",\n    "type": '
                                           '"invalid_request_error"\n  }\n}')
        return super().embed(texts, timeout, retries)


def drain(back: embeddings.Backfill) -> None:
    for _ in range(500):
        back.step()
        if not back.pending and not back.dirty.is_set():
            return


# 13. a request is cut by size, so a set of long texts drains; a 400 that names the size halves the next one at once
def test_a_batch_past_the_providers_cap_splits_by_size_and_a_stuck_set_drains(tmp_path):
    s = tree(tmp_path)
    for n in range(40):                       # 40 chunks of about 850 bytes, under the count batch: one request of 34 kB
        (s.root / "facts" / f"long{n}.md").write_text(f"# Long {n}\n\n" + " ".join(f"teapot{n}x{i}" for i in range(80)) + "\n")
    stub = CappedEmbedder(s.embeddings_key, cap=8000)
    store = embeddings.Vectors(stub.dim)
    back = embeddings.Backfill(s.index_db, memindex.live, store, stub, gap=0.0, units="both")
    back.cap = 8000                           # the bound at the provider's cap: every request passes, the set drains
    idx = memindex.Index(s.index_db, s.scope, on_insert=back.note)
    idx.sync()
    drain(back)
    conn = sqlite3.connect(f"file:{memindex.live(s.index_db)}?mode=ro", uri=True)
    assert stub.rejected == [] and max(stub.sizes) <= 8000 and len(stub.sizes) >= 5 and back.failed == 0
    assert back.missing(conn, 10**6) == [] and store.n == vectors_in(s) > 80
    # the premise wrong (a cap of 1 MB against a provider that takes 8 kB): each 400 halves the next request at once,
    # with no pause and no scan, never the same batch twice, and the set drains
    stub2 = CappedEmbedder(s.embeddings_key, cap=8000)
    stub2.model = "stub-2"
    back2 = embeddings.Backfill(s.index_db, memindex.live, embeddings.Vectors(stub2.dim), stub2, gap=0.0, units="both")
    back2.cap = 1_000_000
    drain(back2)
    assert stub2.rejected and back2.failed == len(stub2.rejected) and back2.paused_until == 0.0 and back2.streak == 0
    assert all(a != b for a, b in zip(stub2.calls, stub2.calls[1:])) and len(set(stub2.rejected)) == len(stub2.rejected)
    assert back2.cap <= 8000 and back2.missing(conn, 10**6) == [] and "request size" in back2.last_error
    assert sum(len(c) for c in stub2.calls) == store.n                       # every text once, nothing twice
    conn.close()
    idx.close()


# 14. the unit's sizing rule and the doctor's are one rule
def test_the_index_units_memory_rule_is_the_one_the_doctor_checks():
    text = (Path(__file__).resolve().parents[1] / "units" / "yerk-index.service").read_text()
    assert "MemoryHigh = 256M + 8.3M per block of 16,384 vectors" in " ".join(x.lstrip("# ") for x in text.splitlines())
    assert embeddings.HIGH_BASE == 256 << 20 and embeddings.BLOCK == 16384
    assert round(embeddings.BLOCK * (2 * 256 + 20) / 2**20, 1) == 8.3


def shared_lock_held(db: Path) -> bool:
    """Whether a process holds SQLite's shared lock on the file (a read lock on its shared byte range), asked from a child:
    a process's own POSIX locks never conflict with it."""
    code = ("import fcntl, os, struct, sys\nfd = os.open(sys.argv[1], os.O_RDONLY)\n"
            "lk = struct.pack('hhqqixxxx', fcntl.F_WRLCK, 0, 0x40000002, 510, 0)\n"
            "print(struct.unpack('hhqqixxxx', fcntl.fcntl(fd, fcntl.F_GETLK, lk))[0] != fcntl.F_UNLCK)")
    return subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True, text=True, check=True).stdout.strip() == "True"


def mapped_kib(addr: int) -> int:
    """The Rss of this process's mapping that starts at `addr` (/proc/self/smaps): the pages it holds mapped there."""
    lines = Path("/proc/self/smaps").read_text().splitlines()
    at = next((i for i, x in enumerate(lines) if x.split("-", 1)[0] == f"{addr:x}"), None)
    return 0 if at is None else next(int(x.split()[1]) for x in lines[at + 1:] if x.startswith("Rss:"))


# 15. the pin maps the keyword tables' pages into this process, which a page-cache drop passes by, and closes no descriptor
# of the file while a connection of this process may hold a lock on it: closing one drops them all, and SQLite never takes
# its lock again (sqlite.org/howtocorrupt.html 2.2; measured on this box 2026-10-22, PR recall-warm)
def test_the_pin_maps_the_keyword_tables_and_keeps_every_lock_the_process_holds(tmp_path):
    s = tree(tmp_path, key=False)
    idx = memindex.Index(s.index_db, s.scope)
    idx.sync()
    idx.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")       # every page in the file, none left in the WAL
    db = memindex.live(s.index_db)
    reader = memindex.connect(s.index_db, readonly=True)
    reader.execute("SELECT count(*) FROM chunks").fetchone()  # a WAL connection holds SQLite's shared lock from here on
    assert shared_lock_held(db)
    pin = memindex.Pin(s.index_db)
    st = pin.pass_()
    want = [reader.execute("SELECT count(*), coalesce(sum(pgsize), 0) FROM dbstat WHERE name = ?", (t,)).fetchone() for t in memindex.PIN_TABLES]
    assert st["mode"] == "map" and st["pages"] == sum(n for n, _ in want) and st["bytes"] == sum(b for _, b in want)
    assert mapped_kib(pin.addr) * 1024 == st["bytes"] and shared_lock_held(db)   # the tables' pages, none fault-around added
    fd, first = pin.fd, pin.addr
    (tmp_path / "private" / "memory" / "facts" / "more.md").write_text(
        "# More\n\n" + "".join(f"- an example line about the teal kettle, number {i}\n" for i in range(2000)))
    idx.sync()
    idx.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    st2 = pin.pass_()                                         # the file grew: the same descriptor, a fresh mapping, the lock kept
    assert pin.fd == fd and pin.addr and mapped_kib(first) == 0 and st2["pages"] > st["pages"] and shared_lock_held(db)
    gen = memindex.rebuild(s.index_db, s.scope)               # a rebuild swaps the link: the next pass maps the new generation
    pin.pass_()
    assert pin.real == gen and pin.retired == [fd] and pin.fd != fd
    pin.pass_()
    assert pin.retired == [] and pin.passes == 4              # the old generation's descriptor closed a pass later
    reader.close()
    idx.close()
    pin.close()
    assert pin.fd == -1 and pin.addr == 0


# 16. a writer pass that changed chunks has the pin map their pages at its next tick (5 s), not at the next 10-minute
# pass, so a page-cache drop in between never leaves the newest lines cold (cold read of PR 80, E4)
def test_a_pass_asked_for_after_a_write_runs_at_the_pins_next_tick(tmp_path):
    s = tree(tmp_path, key=False)
    idx = memindex.Index(s.index_db, s.scope)
    idx.sync()
    pin = memindex.Pin(s.index_db)
    pin.start()
    end = time.monotonic() + 30
    while pin.passes < 1 and time.monotonic() < end:
        time.sleep(0.05)
    (tmp_path / "private" / "memory" / "facts" / "more.md").write_text("# More\n\n- an example line about the teal kettle\n")
    assert idx.sync()["inserted"] and pin.passes == 1
    pin.due.set()                                             # what main does after a writer pass that changed chunks
    while pin.passes < 2 and time.monotonic() < end:
        time.sleep(0.05)
    assert pin.passes == 2 and not pin.due.is_set() and time.monotonic() < end
    pin.close()
    idx.close()


# 17. a dbstat walk that fails for another reason (a busy or unreadable read) skips that pass and leaves the pin on; only a
# SQLite built without dbstat turns it off (cold read of PR 80, the pin alone)
def test_a_failed_pin_pass_is_skipped_and_only_a_sqlite_without_dbstat_turns_the_pin_off(tmp_path, monkeypatch):
    s = tree(tmp_path, key=False)
    idx = memindex.Index(s.index_db, s.scope)
    pin = memindex.Pin(s.index_db)

    class Fails:                                              # every statement fails with `msg`
        msg = "database is locked"

        def execute(self, *a):
            raise sqlite3.OperationalError(self.msg)

        def close(self):
            pass

    monkeypatch.setattr(memindex, "connect", lambda *a, **k: Fails())
    with pytest.raises(sqlite3.OperationalError):            # run() logs it as a skipped pass; the next one tries again
        pin.pass_()
    Fails.msg = "no such table: dbstat"                      # what a SQLite built without dbstat answers
    assert pin.mode == "map" and pin.pass_()["mode"] == "off"
    pin.close()
    idx.close()
