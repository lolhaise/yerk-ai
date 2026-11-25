"""Checks searching memory by day: the `recall` tool's `since` and `until` keep a search to the files named for a day in that range.
The range is applied before either half of the search (keywords and meaning) cuts its pool, an empty range is searched once more
is a placeholder."""
import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from yerk_ai import cli, jobs, mcp_server, memindex, memory, words
from yerk_ai.config import EXAMPLE_PRIVATE

from test_room_scope import install, sync

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


# R5: the reader's rule, in the first search and the widened one
def test_a_range_never_lets_a_room_read_what_it_may_not(tmp_path):
    y = install(tmp_path, family=True)
    mom, today = "family-room", memory.window(y.memory)[0]
    y.daily.capture("example-room", "o1", "owner", "the owner's private line about the example kettle")
    y.daily.capture(mom, "p1", "person", "the mother's line about the example kettle", who="mom")
    sync(y)

    def ask(room: str, query: str, *days: str) -> dict:
        return asyncio.run(y.tool_recall(room, query, 20, *days))
    got = ask(mom, "example kettle", today, today)
    assert "the mother's line" in str(got["hits"]) and "owner's private" not in str(got["hits"]) and len(got["hits"]) == 1
    assert "periwinkle" not in str(got) and "widened" not in got
    for query in ("owner's private", "vermilion"):            # what she may not read is in that day, or in no day she reads: as nothing
        r = ask(mom, query, today, today)
        assert r["ok"] and r["widened"] is True and r["hits"] == []
    for days in (("2001-01-01", "2001-01-01"), ("2026-10-19", "2026-10-19")):              # the widened one is hers too: the live day
        r = ask(mom, "example kettle shed key", *days)                                     # of the read-only roots is in the range
        assert r["widened"] is True and "the mother's line" in str(r["hits"]) and "owner's private" not in str(r["hits"])
        assert not [w for w in ("vermilion", "periwinkle") if w in str(r["hits"])]
        assert r["hits"] == ask(mom, "example kettle shed key")["hits"]
    r = ask("example-room", "example kettle", today, today)                                # an owner room: both
    assert "the mother's line" in str(r["hits"]) and "owner's private" in str(r["hits"])


def test_a_range_never_lets_a_sealed_or_a_scoped_room_out_of_its_roots(tmp_path):
    y = install(tmp_path, sealed=True)
    today = memory.window(y.memory)[0]
    y.daily.capture("live-room", "m2", "user", "the example shed key moved to the saffron shelf")
    y.daily.capture("frozen-room", "m3", "user", "the example shed key question from the frozen room itself")
    sync(y)

    def ask(room: str, *days: str) -> dict:
        return asyncio.run(y.tool_recall(room, "the example shed key by which door, on which shelf", 20, *days))
    r = ask("frozen-room", "2026-10-19", "2026-10-19")
    assert "periwinkle" in str(r["hits"]) and "vermilion" not in str(r["hits"]) and "widened" not in r
    r = ask("frozen-room", today, today)
    assert "frozen room itself" in str(r["hits"]) and "saffron" not in str(r["hits"]) and "widened" not in r
    r = ask("frozen-room", "2001-01-01", "2001-01-01")
    assert r["widened"] is True and "frozen room itself" in str(r["hits"]) and "periwinkle" in str(r["hits"])
    assert "saffron" not in str(r["hits"]) and "vermilion" not in str(r["hits"])
    r = ask("live-room", "2026-10-19", "2026-10-19")                                       # scoped and not sealed: its own roots
    assert "vermilion" in str(r["hits"]) and "periwinkle" not in str(r["hits"])
    assert "periwinkle" in str(r := ask("example-room", "2026-10-19", "2026-10-19")) and "vermilion" in str(r)


# the surfaces: the room's tool, a worker's server and its call to the daemon
def test_the_room_and_its_workers_hand_the_range_on(tmp_path, monkeypatch):
    y = install(tmp_path)
    got = asyncio.run(y.control({"recall": {"room": "example-room", "query": "example shed key", "k": 20,
                                            "since": "2026-10-19", "until": "2026-10-19"}}, {"kind": "worker"}))
    assert got["ok"] and cites(got) == ["2026-10-19.md"]
    assert asyncio.run(y.control({"recall": {"room": "example-room", "query": "x", "since": "Friday"}}))["status"] == "refused"
    assert cites(asyncio.run(y.control({"recall": {"room": "example-room", "query": "example kettle"}}))) != ["2026-10-19.md"]
    asked: list[tuple] = []

    async def heard(*a: object) -> dict:
        asked.append(a)
        return {"ok": True}
    monkeypatch.setattr(y, "tool_recall", heard)
    monkeypatch.setattr(mcp_server, "caller_room", lambda ctx: "example-room")
    server = mcp_server.build(y, y.port, y.hook)
    asyncio.run(server.call_tool("recall", {"query": "kettle", "since": DAY, "until": "2026-01-06"}))
    asyncio.run(server.call_tool("recall", {"query": "kettle", "until": DAY}))
    asyncio.run(server.call_tool("recall", {"query": "kettle"}))
    assert asked == [("example-room", "kettle", 6, DAY, "2026-01-06"), ("example-room", "kettle", 6, "", DAY), ("example-room", "kettle", 6, "", "")]
    sent: list[dict] = []
    monkeypatch.setattr(jobs, "post_control", lambda state, payload, *a: sent.append(payload) or {"ok": True})
    worker = mcp_server.worker_server(tmp_path, "example-room", ["recall"])
    asyncio.run(worker.call_tool("recall", {"query": "kettle", "since": DAY, "until": DAY}))
    assert sent == [{"recall": {"room": "example-room", "query": "kettle", "k": 6, "since": DAY, "until": DAY}}]
    for tools in (server, worker):
        tool = next(t for t in asyncio.run(tools.list_tools()) if t.name == "recall")
        assert list(tool.inputSchema["properties"]) == ["query", "k", "since", "until"] and tool.inputSchema["required"] == ["query"]
        said = " ".join(str(tool.description).split())
        assert said.endswith("\"timeout\" status means busy, not down. `since` and `until` (each YYYY-MM-DD, either alone) keep the search to "
                             "files named for a day in that range, both ends in it. Work them out from the clock line when the question names a "
                             "day, a week, a month or a phrase like \"last Sunday\", and leave them out when it names no time.")


def test_the_prompt_hooks_own_search_is_as_it_was(tmp_path, monkeypatch):
    """The search a message starts takes no range, whatever day it names, and hands the same hits at the same sizes: the pinned
    ones, and byte for byte those of the recall it made before."""
    y, seen = install(tmp_path), []
    real = memory.recall
    ask = "where did the example shed key hang on 2026-10-19?"
    before = memory.live_first(y.memory, real(y.memory, ask, 4, 3.0, y.reader("example-room"), 1500, 2.0, False), y.reader("example-room"))
    monkeypatch.setattr(memory, "recall", lambda *a, **k: seen.append((a, k)) or real(*a, **k))
    got = y.search("example-room", ask, 4, 1500, 2.0, 3.0, docs=False)
    assert json.dumps(got) == json.dumps(before) and sorted(got) == ["hits", "ok", "status"]
    day, pinned = "memory/daily/2026-01-0", [("snap/memory/2026-10-19.md:1", 64), ("live/memory/2026-10-19.md:1", 110)]      # main 66377a20's own
    pinned += [(f"{day}7.md:5", 267), (f"{day}7.md:1", 168), (f"{day}6.md:1", 169), (f"{day}5.md:1", 186)]
    assert [(h["cite"], len(h["text"])) for h in got["hits"]] == pinned[:4]
    assert [(h["cite"], len(h["text"])) for h in y.recall_for("example-room", ask)[2]] == pinned
    assert seen and all((a[8:] in ((), ("", ""))) and not (k.get("since") or k.get("until")) for a, k in seen)


# R6: the bench
def shipped_bench(s: memory.Settings, qfile: Path, k: int = 6) -> dict:
    """memory.bench as it was before the range (main 66377a20), kept here to hold the plain numbers to it byte for byte."""
    rows = []
    for kind, f in ((x, qfile if x == "recall" else qfile.with_name(f"{x}-questions.json")) for x in memory.KINDS):
        if kind != "recall" and not f.is_file():
            continue
        data = json.loads(f.read_text())
        stale = re.compile(str(data.get("stale") or r"(^|/)overwrite-backups/"))
        for q in data["questions"]:
            weights: dict[str, float] = {}
            r = memory.ranked(s, str(q["q"]), k, deadline_s=30.0, weights=weights)
            rank = memory.rank_of(r["hits"], q, stale)
            cut = memory.snippets(r["hits"], weights, s.recall_chars) if rank else []
            rows.append({"id": q["id"], "kind": kind, "control": bool(q.get("control")), "rank": rank,
                         "kept": memory.has_all(cut[rank - 1]["text"], list(q["answer_tokens"])) if rank else None,
                         "top": r["hits"][0]["path"] if r["hits"] else None})
    scored = [r for r in rows if not r["control"]]
    return {"ok": True, **memory.score(rows), "controls": len(rows) - len(scored), "k": k,
            "blind": [r["id"] for r in rows if r["control"] and not r["rank"]],
            "kept": sum(1 for r in scored if r["kept"]), "lost": [r["id"] for r in scored if r["rank"] and not r["kept"]],
            "kinds": {x: memory.score(xs) for x in memory.KINDS if (xs := [r for r in rows if r["kind"] == x])}, "rows": rows, "file": str(qfile)}


def questions(tmp_path: Path) -> tuple[memory.Settings, Path]:
    """Two day logs' lines that 200 chunks outrank, one that ranks plainly, and facts; six questions, one a control."""
    crowd = "\n".join(f"## note {n}\n\nkettle kettle kettle {n}" for n in range(200))
    filler = "some other words of the day, " * 12
    s = layer(tmp_path, {"2026-01-04": crowd, DAY: f"- 09:00 a person: {filler}a kettle was teal", "2026-01-06": "- 09:00 a person: the lamp is red"},
              facts__kettle="# Kettle\n\n- The kettle holds a litre.\n")
    at = s.private / "evals"
    at.mkdir()
    (at / "recall-questions.json").write_text(json.dumps({"questions": [
        {"id": "c1", "q": "which lamp", "answer_tokens": ["lamp"], "control": True, "gold": ["daily/2026-01-06.md:3"]},
        {"id": "q1", "q": "the kettle", "answer_tokens": ["teal"], "gold": [f"memory/{DAY}.md:3"]},                    # only with its day
        {"id": "q2", "q": "what colour is the lamp", "answer_tokens": ["red"], "gold": ["daily/2026-01-06.md:3"]},      # both ways
        {"id": "q3", "q": "the kettle", "answer_tokens": ["nowhere"], "gold": ["facts/kettle.md:3", f"/abs/{DAY}.md:3"]},  # neither
        {"id": "q4", "q": "litre", "answer_tokens": ["litre"], "gold": ["facts/kettle.md:3"]},                          # no day
        {"id": "q5", "q": "the lamp", "answer_tokens": ["lamp"]}]}))                                                    # no gold
    (at / "episodic-questions.json").write_text(json.dumps({"questions": [
        {"id": "e1", "kind": "episodic", "q": "kettle", "answer_tokens": ["teal"], "gold": [f"{DAY}-notes.md:1", "2026-01-06.md:3"]}]}))
    return s, at / "recall-questions.json"


def test_the_bench_runs_the_dated_questions_both_ways_and_its_plain_numbers_are_as_they_were(tmp_path, monkeypatch, capsys):
    s, qfile = questions(tmp_path)
    was, got = shipped_bench(s, qfile), memory.bench(s, qfile, days=True)
    assert got["dated"] == {"questions": 4, "top_k": 1, "top_k_day": 3}
    assert json.dumps({k: v for k, v in got.items() if k != "dated"}) == json.dumps(was) == json.dumps(memory.bench(s, qfile))
    assert (was["questions"], was["top_k"]) == (6, 3)                           # q1, q3 and e1 plainly missed
    args = ["recall-bench", "--private", str(s.private), "--state", str(tmp_path / "state"), "--questions", str(qfile)]
    assert cli.main(args) == 0
    raw = (tmp_path / "state" / "memory" / "recall-bench.jsonl").read_text().splitlines()[-1]
    keep = ("questions", "controls", "k", "top_k", "top_1", "top_k_rate", "top_1_rate", "blind", "kept", "lost", "kinds")     # as it was
    plain = json.dumps({"at": json.loads(raw)["at"], **{x: was[x] for x in keep}, "misses": [x["id"] for x in was["rows"] if not x["rank"]]})
    assert raw == plain[:-1] + ', "dated": {"questions": 4, "top_k": 1, "top_k_day": 3}}'
    assert "dated: top 6 1/4 without the day, 3/4 with it" in capsys.readouterr().out.splitlines()
    real = memory.ranked                                                         # a dated search that did not finish is no miss, and
    monkeypatch.setattr(memory, "ranked", lambda *a, **k: {"ok": False, "status": "timeout", "error": "busy"} if k.get("since") else real(*a, **k))
    late = memory.bench(s, qfile, days=True)                                     # the plain numbers stand
    assert late["dated"] == {"status": "timeout"} and json.dumps({k: v for k, v in late.items() if k != "dated"}) == json.dumps(was)
    assert cli.main(args) == 0 and "dated: timeout" in capsys.readouterr().out.splitlines()


def test_the_ratchet_is_not_asked_about_the_dated_object(tmp_path):
    n = {"questions": 209, "top_k": 126, "top_1": 69, "top_k_rate": 0.6, "top_1_rate": 0.3}
    rows = [{"k": 6, **n, "kinds": {"recall": n}, "dated": {"questions": 148, "top_k": 90, "top_k_day": x}} for x in (120, 120, 120, 120, 0)]
    assert memory.ratchet(rows, []) is None and memory.ratchet([{k: v for k, v in r.items() if k != "dated"} for r in rows], []) is None


def test_the_example_layers_question_files_name_no_gold_and_the_object_counts_none(tmp_path):
    s = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state", {"timezone": "UTC"})
    reindex(s)
    assert memory.bench(s, EXAMPLE_PRIVATE / "evals" / "recall-questions.json", days=True)["dated"] == {"questions": 0, "top_k": 0, "top_k_day": 0}
