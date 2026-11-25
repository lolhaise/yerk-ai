"""Checks that a `recall` search that names no number of hits gives twelve: the six a search for six gives, exactly as they are, then six more
as one line each. Those come first from the keyword half's own top six the fusion left out, then the fused order's next, none of them twice
is handed, and the weekly bench's number, stay at six. Every value here is a placeholder."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from yerk_ai import cli, jobs, mcp_server, memindex, memory
from yerk_ai.daemon import PACK_HITS, PAST_HITS, Yerkd

from test_recall_day import DAY, layer, reindex, shipped_bench
from test_room_scope import install, sync

ASK = "where does the kettle stand, on which shelf"


def chunks(s: memory.Settings) -> list[dict]:
    """The index's own rows, as the halves return them (no score)."""
    conn = sqlite3.connect(s.index_db)
    try:
        return [{"id": i, "path": p, "line": lo, "text": b} for i, p, lo, b in conn.execute("SELECT id, path, lo, body FROM chunks")]
    finally:
        conn.close()


def notes(tmp_path: Path, n: int = 30, **lines: str) -> tuple[memory.Settings, list[dict]]:
    """n fact files, each one chunk with the kettle in it (or the line given for it, `n02="..."`), and their rows by number."""
    s = layer(tmp_path, **{f"facts__n{i:02}": f"# Note {i:02}\n\n- {lines.get(f'n{i:02}', f'The kettle of note {i:02} stands on shelf {i:02}.')}\n" for i in range(n)})
    by = {Path(c["path"]).stem: c for c in chunks(s)}
    return s, [by[f"n{i:02}"] for i in range(n)]


def halves(monkeypatch, keywords: list[dict], vectors: list[dict]) -> None:
    """The two halves' rows, in this order, as the index and yerk-index would rank them; the keyword half's filters are applied."""
    def search(conn, query, k=6, pool=80, weights=None, skip=None, keep=None, most=0, only=None):
        if weights is not None:
            weights.update({t: 1.0 for t in memindex.terms(query)})
        return [dict(h, score=1.0 - n / 1000) for n, h in enumerate(keywords)
                if (only is None or only(h["path"])) and (keep is None or keep(h["path"], h["text"]))][:k]
    monkeypatch.setattr(memindex, "search", search)
    monkeypatch.setattr(memory, "meaning", lambda s, q, **wait: ("ok", [dict(h, score=round(0.9 - n / 1000, 4)) for n, h in enumerate(vectors)]))


def stems(r: dict) -> list[str]:
    """Each hit's file stem, in order."""
    assert r["ok"], r
    return [Path(h["cite"].rpartition(":")[0]).stem for h in r["hits"]]


def numbers(rows: list[dict]) -> list[str]:
    return [Path(h["path"]).stem for h in rows]


# T2 and T3: the first six as a call of six gives them, then the tail
def test_the_first_six_of_twelve_are_the_call_of_six_where_fuse_at_twelve_orders_them_otherwise(tmp_path, monkeypatch):
    s, rows = notes(tmp_path)
    halves(monkeypatch, rows[:4], rows[4:24])
    six, at_twelve = (memory.fuse([dict(h, score=1.0) for h in rows[:4]], [dict(h, score=0.9 - n / 1000) for n, h in enumerate(rows[4:24])], k) for k in (6, 12))
    assert numbers(six) == ["n04", "n05", "n06", "n07", "n08", "n00"] != numbers(at_twelve)[:6]            # held: the keyword half's first
    r6, r12, r20 = (memory.recall(s, ASK, k) for k in (6, 12, 20))
    assert json.dumps(r12["hits"][:6]) == json.dumps(r6["hits"]) == json.dumps(r20["hits"][:6]) and len(r6["hits"]) == 6
    assert stems(r6) == numbers(six) and len(r12["hits"]) == 12 and len(r20["hits"]) == 20
    for k in (1, 2, 3, 5):                                                                    # a smaller call is as it was too
        assert stems(memory.recall(s, ASK, k)) == numbers(memory.fuse([dict(h, score=1.0) for h in rows[:4]],
                                                                      [dict(h, score=0.9 - n / 1000) for n, h in enumerate(rows[4:24])], k))


def test_the_tail_is_the_keyword_halfs_own_six_then_the_fused_orders_next(tmp_path, monkeypatch):
    """Keyword rank two, three and four are the answers fusion put past the six; fused rank six, seven and eight come after them."""
    s, rows = notes(tmp_path)
    halves(monkeypatch, rows[:4], rows[4:24])
    r = memory.recall(s, ASK, 12)
    assert stems(r) == ["n04", "n05", "n06", "n07", "n08", "n00", "n01", "n02", "n03", "n09", "n10", "n11"]
    assert "n02" in stems(r) and stems(r).index("n02") in range(6, 9)             # keyword rank three, fused past the six, right in the tail
    assert stems(r)[9:12] == ["n09", "n10", "n11"]                                    # fused rank six, seven and eight, after the keyword half's
    assert stems(memory.recall(s, ASK, 8)) == stems(r)[:8]                            # a call of eight: the same order, cut
    more = memory.recall(s, ASK, 20)
    assert stems(more)[:12] == stems(r) and len(more["hits"]) == 20 and len(set(h["cite"] for h in more["hits"])) == 20


def test_no_hit_is_shown_twice_and_each_one_past_the_sixth_is_one_line_of_at_most_extra(tmp_path, monkeypatch):
    s, rows = notes(tmp_path)
    long = tmp_path / "private" / "memory" / "facts" / "long.md"
    long.write_text("# Long\n\n- " + "a kettle " * 60 + "and on the shelf " * 60 + "\n- a second line of the long one\n")
    reindex(s)
    row = next(c for c in chunks(s) if Path(c["path"]).name == "long.md")
    for kw, vec in ((rows[:4], rows[4:24]), (rows[:3], rows[3:24]), ([rows[9], rows[0], row, rows[3]], [row, *rows[4:24]]),
                    ([row, *rows[:8]], rows[8:20])):
        halves(monkeypatch, kw, vec)
        for k in (12, 20):
            r = memory.recall(s, ASK, k)
            cites = [h["cite"] for h in r["hits"]]
            assert len(set(cites)) == len(cites) == k, (k, cites)
            assert all(len(h["text"]) <= memory.EXTRA and len(h["text"].splitlines()) == 1 for h in r["hits"][6:])
    for k, words in ((6, 6), (12, 12), (20, 20)):                                         # what the call asked for, whole
        assert len(memory.recall(s, ASK, k)["hits"]) == words


# T1: the tool's default, for a room and a worker
def many(y: Yerkd, n: int = 30) -> None:
    facts = y.memory.root / "facts"
    facts.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (facts / f"kettle-{i:02}.md").write_text(f"# Kettle {i:02}\n\n- The example kettle {i:02} stands on the shelf {i:02}.\n")
    sync(y)


def test_a_call_with_no_number_gives_twelve_hits_for_a_room_and_for_a_worker(tmp_path, monkeypatch):
    y = install(tmp_path)
    many(y)

    def worker(**args) -> list[str]:
        got = asyncio.run(y.control({"recall": {"room": "example-room", "query": "example kettle shelf", **args}}, {"kind": "worker"}))
        return [h["cite"] for h in got["hits"]]
    assert [len(worker(**a)) for a in ({}, {"k": 6}, {"k": 20}, {"k": 1}, {"k": 7}, {"k": 99})] == [12, 6, 20, 1, 7, 20]
    assert worker()[:6] == worker(k=6) and worker(k=20)[:6] == worker(k=6)
    monkeypatch.setattr(mcp_server, "caller_room", lambda ctx: "example-room")
    server = mcp_server.build(y, y.port, y.hook)

    def room(**args) -> list[dict]:
        out = asyncio.run(server.call_tool("recall", {"query": "example kettle shelf", **args}))
        text = out[0][0].text if isinstance(out, tuple) else out[0].text
        return json.loads(text)["hits"]
    assert len(room()) == 12 and len(room(k=6)) == 6 and len(room(k=20)) == 20
    assert [h["cite"] for h in room()][:6] == [h["cite"] for h in room(k=6)]
    sent: list[dict] = []

    def post(state, payload, *a):                                             # a worker's server posts to the daemon, as its tool sends it
        sent.append(payload)
        return asyncio.run(y.control(payload, {"kind": "worker"}))
    monkeypatch.setattr(jobs, "post_control", post)
    stdio = mcp_server.worker_server(tmp_path, "example-room", ["recall"])
    out = asyncio.run(stdio.call_tool("recall", {"query": "example kettle shelf"}))
    text = out[0][0].text if isinstance(out, tuple) else out[0].text
    assert sent[-1]["recall"]["k"] == 12 and len(json.loads(text)["hits"]) == 12
    for tools in (server, stdio):
        tool = next(t for t in asyncio.run(tools.list_tools()) if t.name == "recall")
        assert tool.inputSchema["properties"]["k"]["default"] == 12 and tool.inputSchema["required"] == ["query"]
        assert "twelve hits" in str(tool.description)


# T4: what a message is handed does not grow by this
def test_the_prompt_hooks_searches_ask_for_six_hits_whatever_the_tools_default(tmp_path, monkeypatch):
    """The past question's search and the text pack's: the same hits and sizes as six, and as they were, with the tool's own default at any number."""
    y = install(tmp_path)
    many(y)
    monkeypatch.setattr(memory, "RECALL_K", 20)
    monkeypatch.setattr(mcp_server, "RECALL_K", 20)
    real, seen = memory.recall, []
    ask = "what did we say about the example kettle on 2026-10-19, where does it stand?"
    rd, s = y.reader("example-room"), y.scoped.get("example-room", y.memory)
    was_pack = memory.live_first(s, real(s, ask, 6, 5.0, rd, 0, memory.MEANING_WAIT, False), rd)
    cap = int(y.budgets["per_message"]["on_hit"]["recall"])
    was_past = memory.live_first(s, real(s, ask, 6, 3.0, rd, cap, 2.0, False), rd)
    monkeypatch.setattr(memory, "recall", lambda *a, **k: seen.append((a, k)) or real(*a, **k))
    pack = y.texting_recall("example-room", ask)
    assert {a[2] for a, _ in seen} == {6} and len(was_pack["hits"]) == 6 and PACK_HITS == PAST_HITS == 6       # every one of them six
    assert pack.endswith("\n".join(f"{h['cite']} {h['text']}" for h in was_pack["hits"]))
    back = y.recall_for("example-room", ask)
    assert back[2] == [h for h in was_past["hits"] if not any(f"[m:{m}" in h["text"] for m in set())] and len(back[2]) <= 6
    assert {a[2] for a, _ in seen} == {6} and {a[5] for a, _ in seen} <= {0, cap}
    with pytest.raises(TypeError):                                             # no default to reach them: each says its own k
        y.search("example-room", ask)                                           # type: ignore[call-arg]


# T3: what a room may read, and the range
def lanes(y: Yerkd, room: str, n: int, word: str, owner: str) -> None:
    """n lines of `room` and n of the owner's, alternating, each with the kettle: a chunk each."""
    for i in range(n):
        y.daily.capture("example-room", f"o{i}", "owner", f"the owner's private {owner} kettle line {i}")
        y.daily.capture(room, f"r{i}", "person" if room != "example-room" else "owner", f"the {word} kettle line {i}", who="mom")
    sync(y)


def test_a_people_rooms_tail_holds_only_what_it_may_read(tmp_path, monkeypatch):
    y = install(tmp_path, family=True)
    lanes(y, "family-room", 12, "mother's", "vermilion")
    every = [dict(c, score=0.9) for c in chunks(y.memory)]
    monkeypatch.setattr(memory, "meaning", lambda s, q, **wait: ("ok", [dict(h) for h in every]))

    def ask(room: str, k: int = 12) -> list[dict]:
        return asyncio.run(y.tool_recall(room, "mother's kettle line", k))["hits"]
    hits = ask("family-room")
    assert len(hits) == 12 and not any("vermilion" in h["text"] or "private" in h["text"] for h in hits) and "mother's" in str(hits[6:])
    assert not any("tangerine" in h["text"] for h in ask("family-room", 20)) and len(ask("family-room", 20)) > 6
    assert "vermilion" in str(ask("example-room", 20))                       # the owner's rooms read both


def test_a_sealed_rooms_tail_holds_only_what_it_may_read(tmp_path, monkeypatch):
    y = install(tmp_path, sealed=True)
    frozen, every = y.memory, []
    for i in range(14):
        y.daily.capture("frozen-room", f"f{i}", "user", f"the frozen kettle line {i} on the shelf")
        y.daily.capture("live-room", f"l{i}", "user", f"the saffron kettle line {i} on the shelf")
    sync(y)
    every = [dict(c, score=0.9) for c in chunks(y.memory)]
    monkeypatch.setattr(memory, "meaning", lambda s, q, **wait: ("ok", [dict(h) for h in every]))

    def ask(k: int = 12) -> list[dict]:
        return asyncio.run(y.tool_recall("frozen-room", "the frozen kettle line on the shelf", k))["hits"]
    hits = ask()
    assert len(hits) == 12 and not any("saffron" in h["text"] for h in hits) and "frozen" in str(hits[6:])
    assert not any("saffron" in h["text"] or "tangerine" in h["text"] for h in ask(20))
    del frozen


def days(tmp_path: Path, n: int = 24) -> memory.Settings:
    """A day's 16 chunks of the kettle, and the day before's and after's 16 each, better at it."""
    def day(words: str, more: str) -> str:
        return "\n".join(f"## line {i}\n\n- a kettle {words} {i} on the shelf {more}" for i in range(16))
    return layer(tmp_path, {"2026-01-04": day("the day before", "kettle kettle"), DAY: day("of the day", ""), "2026-01-06": day("the day after", "kettle kettle")})


def test_a_ranges_tail_is_inside_the_range_and_a_widened_search_has_its_tail_and_its_line(tmp_path, monkeypatch):
    s = days(tmp_path)
    every = [dict(c, score=0.9) for c in chunks(s)]
    monkeypatch.setattr(memory, "meaning", lambda s, q, **wait: ("ok", [dict(h) for h in every]))
    r6, r12 = (memory.recall(s, "kettle shelf", k, since=DAY, until=DAY) for k in (6, 12))
    assert len(r12["hits"]) == 12 and r12["hits"][:6] == r6["hits"] and "widened" not in r12
    assert all(Path(h["cite"].rpartition(":")[0]).stem == DAY for h in r12["hits"])           # the tail is the day's, all of it
    assert all(Path(h["cite"].rpartition(":")[0]).stem == DAY for h in memory.recall(s, "kettle shelf", 20, since=DAY, until=DAY)["hits"])
    for r in (memory.recall(s, "kettle shelf", 12, since="2026-03-01", until="2026-03-02"),):      # a range with nothing in it
        assert r["widened"] is True and r["line"] and len(r["hits"]) == 12
        assert json.dumps(r["hits"][:6]) == json.dumps(memory.recall(s, "kettle shelf", 6, since="2026-03-01", until="2026-03-02")["hits"])
        assert {Path(h["cite"].rpartition(":")[0]).stem for h in r["hits"][6:]} <= {"2026-01-04", DAY, "2026-01-06"}


# live_first: the head as a call of six gives it, the tail with a replacement above what it replaces
def live(s: memory.Settings, k: int) -> list[dict]:
    return memory.live_first(s, memory.recall(s, ASK, k))["hits"]


def test_a_replacement_stands_above_the_line_it_replaces_in_the_tail_and_is_never_a_hit_twice(tmp_path, monkeypatch):
    """n02 (keyword rank three, in the tail) is marked superseded by n11's line, which ranks last in the tail: n11's line comes above it, once."""
    s, rows = notes(tmp_path, n02="The kettle of note 02 stood on shelf 02. [superseded by memory/facts/n11.md:3]")
    halves(monkeypatch, rows[:4], rows[4:24])
    hits = live(s, 12)
    cites = [h["cite"] for h in hits]
    names = [c.rpartition(":")[0].rpartition("/")[2].removesuffix(".md") for c in cites]
    assert names == ["n04", "n05", "n06", "n07", "n08", "n00", "n01", "n11", "n02", "n03", "n09", "n10"]
    assert len(hits) == len(set(cites)) == 12 and all(len(h["text"].splitlines()) == 1 and len(h["text"]) <= memory.EXTRA for h in hits[6:])
    assert "stands on shelf 11" in hits[7]["text"] and "superseded" in hits[8]["text"]
    (s.root / memory.LATER).write_text("memory/facts/n03.md:3\tmemory/facts/n10.md:3\n")         # the later map, for a line it could not mark
    names = [c["cite"].rpartition(":")[0].rpartition("/")[2].removesuffix(".md") for c in live(s, 12)]
    assert names.index("n10") < names.index("n03") and len(names) == 12 == len(set(names))


def test_a_replaced_line_in_the_first_six_is_as_a_call_of_six_gives_it_and_the_tail_never_moves_into_them(tmp_path, monkeypatch):
    s, rows = notes(tmp_path, n05="The kettle of note 05 stood on shelf 05. [superseded by memory/facts/n10.md:3]",
                    n11="The kettle of note 11 stands on shelf 11 (supersedes memory/facts/n06.md:3).")
    halves(monkeypatch, rows[:4], rows[4:24])
    six, twelve = live(s, 6), live(s, 12)
    assert json.dumps(twelve[:len(six)]) == json.dumps(six) and len(six) == 7                      # n10's line read in above n05, as with six
    names = [h["cite"].rpartition(":")[0].rpartition("/")[2].removesuffix(".md") for h in twelve]
    assert names[1] == "n10" and names[2] == "n05"                                                 # read in above what it replaces: the six's own
    assert names.count("n11") == 1 and names.index("n11") > 6                                      # n11 names n06 and stays in the tail, below it
    assert [x for x in names if x == "n10"] == ["n10"], "n10 is in the tail too: the line shown above, not again"
    assert len(twelve) == 13 - 1 and len({(h["cite"], h["text"]) for h in twelve}) == len(twelve)


# T5: the bench, the six as they are and the twelve beside
def bench_layer(tmp_path: Path) -> tuple[memory.Settings, list[dict], Path]:
    far = "The kettle of note 01 stands on the shelf, " + "some other words of the day, " * 12 + "a kettle was teal"
    s, rows = notes(tmp_path, n01=far)
    at = s.private / "evals"
    at.mkdir()
    q = [{"id": f"q{n}", "q": ASK, "answer_tokens": [f"note {i:02}"]} for n, i in enumerate((2, 4, 11, 20), 1)]
    q.append({"id": "q5", "q": ASK, "answer_tokens": ["a kettle was teal"]})                          # in the tail's chunk, not in its line of 160
    q.append({"id": "c1", "q": ASK, "answer_tokens": ["note 05"], "control": True})                  # in the first six, and no scored question
    (at / "recall-questions.json").write_text(json.dumps({"questions": q}))
    return s, rows, at / "recall-questions.json"


def test_the_bench_measures_twelve_beside_the_six_and_the_plain_row_is_as_it_was(tmp_path, monkeypatch, capsys):
    s, rows, qfile = bench_layer(tmp_path)
    halves(monkeypatch, rows[:4], rows[4:24])
    was, got = shipped_bench(s, qfile), memory.bench(s, qfile, days=True)
    assert (was["top_k"], was["kept"], was["questions"], was["blind"]) == (1, 1, 5, [])             # note 04 alone; the rest past the six
    assert list(got)[-2:] == ["dated", "k12"] and got["k12"] == {"top_k": 4, "kept": 3}               # note 02, 04, 11 and the tail's far answer
    assert json.dumps({k: v for k, v in got.items() if k not in ("dated", "k12")}) == json.dumps(was)
    assert json.dumps(memory.bench(s, qfile)) == json.dumps(was) and "k12" not in memory.bench(s, qfile)       # the night's run: none of it
    args = ["recall-bench", "--private", str(s.private), "--state", str(tmp_path / "state"), "--questions", str(qfile)]
    assert cli.main(args) == 0
    raw = (tmp_path / "state" / "memory" / "recall-bench.jsonl").read_text().splitlines()[-1]
    row = json.loads(raw)
    keep = ("questions", "controls", "k", "top_k", "top_1", "top_k_rate", "top_1_rate", "blind", "kept", "lost", "kinds")
    plain = json.dumps({"at": row["at"], **{x: was[x] for x in keep}, "misses": [x["id"] for x in was["rows"] if not x["rank"]]})
    assert raw == plain[:-1] + ', "dated": {"questions": 0, "top_k": 0, "top_k_day": 0}, "k12": {"top_k": 4, "kept": 3}}'
    assert "k12: top 12 4/5, answer in the snippet 3/4" in capsys.readouterr().out.splitlines()
    assert json.dumps(memory.bench(s, qfile, k=12, days=True)["k12"]) == json.dumps({"top_k": 4, "kept": 3})      # any k asks for the twelve too


def test_the_weekly_run_asks_each_search_once_and_the_nights_runs_pay_for_nothing_new(tmp_path, monkeypatch):
    s, rows, qfile = bench_layer(tmp_path)
    halves(monkeypatch, rows[:4], rows[4:24])
    real, runs = memory.ranked, []
    monkeypatch.setattr(memory, "ranked", lambda *a, **k: runs.append((k.get("since"), a[2])) or real(*a, **k))
    memory.bench(s, qfile)                                                    # the night's two: six, once a question
    assert runs == [(None, 6)] * 6
    runs.clear()
    memory.bench(s, qfile, days=True)                                         # the weekly: one search at twelve, whose first six are the six's
    assert runs == [(None, 12)] * 6
    late = lambda *a, **k: {"ok": False, "status": "timeout", "error": "busy"} if a[2] == 12 else real(*a, **k)  # noqa: E731
    monkeypatch.setattr(memory, "ranked", late)
    assert memory.bench(s, qfile, k=6, days=True) == {"ok": False, "status": "timeout", "error": "busy"}      # the one search is the plain one's


def test_a_drop_in_the_twelve_is_no_drop_for_the_ratchet_which_reads_the_six_alone(tmp_path, monkeypatch, capsys):
    s, rows, qfile = bench_layer(tmp_path)
    halves(monkeypatch, rows[:4], rows[4:24])
    args = ["recall-bench", "--private", str(s.private), "--state", str(tmp_path / "state"), "--questions", str(qfile)]
    assert cli.main(args) == 0
    f = tmp_path / "state" / "memory" / "recall-bench.jsonl"
    row = json.loads(f.read_text().splitlines()[0])
    f.write_text("".join(json.dumps({**row, "k12": {"top_k": 100, "kept": 90}}) + "\n" for _ in range(4)))         # runs before, the twelve far higher
    assert cli.main(args) == 0 and "RATCHET" not in capsys.readouterr().err
    fell = {**row, "kinds": {"recall": {**row["kinds"]["recall"], "top_k": row["top_k"] + 2}}}                  # the six fell by two: a drop
    f.write_text("".join(json.dumps(fell) + "\n" for _ in range(4)))
    assert cli.main(args) == 4 and "RATCHET: top 6 fell" in capsys.readouterr().err


def test_a_long_replaced_lines_mark_is_read_off_the_whole_line_and_what_is_read_in_is_one_line_of_at_most_extra(tmp_path, monkeypatch):
    """The lines are longer than a tail hit shows (160 characters), so n02's mark, n03's and the replacement's own words are past the cut."""
    far = " and the shelf it stands on is the one by the window" * 4
    s, rows = notes(tmp_path, n01="The kettle of note 01 stands on shelf 01," + far, n10="The kettle of note 10 stands on shelf 10," + far,
                    n02="The kettle of note 02 stood on shelf 02," + far + " [superseded by memory/facts/n10.md:3]",
                    n03="The kettle of note 03 stood on shelf 03. [superseded by memory/facts/n01.md:3]")
    halves(monkeypatch, rows[:4], rows[4:24])
    hits = live(s, 12)
    names = [h["cite"].rpartition(":")[0].rpartition("/")[2].removesuffix(".md") for h in hits]
    assert names == ["n04", "n05", "n06", "n07", "n08", "n00", "n01", "n10", "n02", "n03", "n09", "n11"]           # n10 above n02, n01 above n03
    assert len({h["cite"] for h in hits}) == 12 and all(len(h["text"]) <= memory.EXTRA and len(h["text"].splitlines()) == 1 for h in hits[6:])
    assert json.dumps(live(s, 12)[:6]) == json.dumps(live(s, 6))
