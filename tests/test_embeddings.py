"""Checks meaning search, which finds memory lines by what they mean as well as by their words. The cases: only
changed chunks are embedded (a touch embeds none), a missing key keeps the keyword path and the room hears it
once, a provider timeout and a provider error each fall back and read apart, fusion ranks a paraphrase above a
keyword-only near miss, the index stays queryable during a backfill, the bench's output shape is unchanged, and
show the ability both ways. No provider is ever called: a stub stands in. Every value here is a
placeholder."""
import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from yerk_ai import abilities, cli, embeddings, memindex, memory
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import NO_KEY_LINE, ConsoleChannel, Yerkd

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


def serve(s: memory.Settings, stub: StubEmbedder):
    """yerk-index's pieces in-process: the writer, the vector store, the search server on a free port,
    the backfill (stepped by hand) and a `status` that writes index.json as the process would."""
    store = embeddings.Vectors(stub.dim)
    back = embeddings.Backfill(s.index_db, memindex.live, store, stub, gap=0.0)
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


# 1. only changed chunks are embedded
def test_only_changed_chunks_are_embedded_and_a_touch_embeds_none(tmp_path):
    s = tree(tmp_path)
    stub = StubEmbedder(s.embeddings_key)
    idx, store, server, back, status = serve(s, stub)
    while back.step():
        pass
    chunks = {r[0] for r in idx.conn.execute("SELECT DISTINCT sha FROM chunks")}
    embedded = [t for call in stub.calls for t in call]
    assert len(embedded) == len(chunks) == store.n == vectors_in(s) > 2
    day = s.root / "daily" / "2026-01-05.md"
    calls = len(stub.calls)
    os.utime(day, None)                                              # a touch: the chunker finds the same text
    idx.sync()
    assert back.step() == 0 and len(stub.calls) == calls and store.n == len(chunks)
    with day.open("a") as f:
        f.write("- 10:00 a person: the forecast says rain\n")       # one line: one chunk changes
    idx.sync()
    assert back.step() == 1 and len(stub.calls) == calls + 1 and len(stub.calls[-1]) == 1
    assert "forecast says rain" in stub.calls[-1][0] and store.n == len(chunks) + 1
    assert back.step() == 0 and len(stub.calls) == calls + 1         # nothing else changed
    server.stop()
    idx.close()


# 2. a missing key: the keyword path, and the room hears it once
def test_a_missing_key_keeps_the_keyword_path_and_the_room_hears_it_once(tmp_path, capsys):
    s = tree(tmp_path, key=False)
    stub = StubEmbedder(s.embeddings_key)
    idx, store, server, back, status = serve(s, stub)
    assert not stub.available() and memory.meaning_state(s) == "no-key"
    assert memory.meaning(s, "the example kettle") == ("no-key", [])
    r = memory.recall(s, "What colour is the example kettle in the example room?")
    assert r["ok"] and r["hits"][0]["path"].endswith("facts/kettle.md") and r["hits"][0]["score"] > 1   # the keyword cover, as before
    assert sorted(r["hits"][0]) == ["cite", "line", "lines", "path", "score", "text"]
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
        r = memory.recall(s, "What colour is the example kettle in the example room?")
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
    r = memory.recall(s, "what colour is the example kettle")
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
    assert sorted(r) == ["blind", "controls", "file", "k", "ok", "questions", "rows", "top_1", "top_1_rate", "top_k", "top_k_rate"]
    assert all(sorted(row) == ["control", "id", "rank", "top"] for row in r["rows"])
    assert r["ok"] and r["blind"] == [] and r["controls"] == 1 and r["top_k"] >= 6
    assert cli.main(["recall-bench", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path / "state"),
                     "--questions", str(qfile)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"top 6: \d+/\d+ \(\d+\.\d%\)   top one: \d+/\d+ \(\d+\.\d%\)   controls: \d+", out[0]), out[0]
    assert out[1].startswith("misses: ")
    assert sorted(json.loads((tmp_path / "state" / "memory" / "recall-bench.jsonl").read_text().splitlines()[-1])) == [
        "at", "blind", "controls", "k", "misses", "questions", "top_1", "top_1_rate", "top_k", "top_k_rate"]


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
