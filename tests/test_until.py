"""Checks end dates on facts: `remember` takes `until`, the last day a state holds, and files the curation's own mark ([until
YYYY-MM-DD]) at the end of the note's words. One rule says when such a line has ended (memory.ended): the sweep blanks a card
line by it, and the readers of a line per message (deep recall, the room's topic lines, the pack) leave one out by it from the
day after, with no sweep or index run between. recall and read_memory still give the line as filed."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from yerk_ai import curate, due, mcp_server, memindex, memory, words
from test_remember import daemon

TZ = ZoneInfo("America/Toronto")
ROOM = "owner-room"
TODAY = datetime(2026, 11, 25, 12, 0, tzinfo=TZ)
LAST = "2026-11-30"                       # the last day the line holds
SRC = r"\(owner-room, 2026-11-25 12:00:00-05:00\)"


def clock(monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
    """memory's clock stopped at `at`: what `remember` stamps and the day its readers and its refusals take as today."""
    class At(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)
    monkeypatch.setattr(memory, "datetime", At)


def install(tmp_path: Path) -> tuple[memory.Settings, memory.DailyLog]:
    private = tmp_path / "private"
    for d in ("facts", "daily"):
        (private / "memory" / d).mkdir(parents=True)
    s = memory.settings(private, tmp_path / "state", {"timezone": "America/Toronto"})
    return s, memory.DailyLog(s.daily, s.tz)


def file(s: memory.Settings, log: memory.DailyLog, text: str, about: str = "", mid: str = "m-1", **kw: str) -> dict:
    return memory.remember(s, log, ROOM, mid, text, about, **kw)


def line_at(s: memory.Settings, cite: str) -> str:
    path, _, n = cite.rpartition(":")
    return (s.root / path.removeprefix("memory/")).read_text().splitlines()[int(n) - 1]


# U1: remember takes until

def test_until_ends_a_fact_files_words_before_the_stamp_reads_as_the_rooms_note_and_a_retry_is_one_line(tmp_path, monkeypatch):
    s, log = install(tmp_path)
    clock(monkeypatch, TODAY)
    r = file(s, log, "Example Neighbour is away at the lake.", "example neighbour", until=LAST)
    assert r["ok"] and r["until"] == LAST and r["cite"] == "memory/facts/example-neighbour.md:3"
    line = line_at(s, r["cite"])
    assert re.fullmatch(r"- Example Neighbour is away at the lake\. \[until 2026-11-30\] " + SRC + r" \[m:m-1\]", line)
    assert memory.FILED.match(line) and memory.due_of(line) is None and line.count("[until") == 1
    assert memory.marked(line, True).startswith(f"- {memory.MARKS[memory.NOTE]} Example Neighbour is away at the lake. [until 2026-11-30] (")
    memindex.Index(s.index_db, s.scope).sync()
    hit = next(h for h in memory.recall(s, "neighbour away lake")["hits"] if h["cite"].startswith("memory/facts/example-neighbour.md"))
    assert f"{memory.MARKS[memory.NOTE]} Example Neighbour is away at the lake. [until 2026-11-30]" in hit["text"]
    assert file(s, log, "Example Neighbour is away at the lake.", "example neighbour", until=LAST)["cite"] == r["cite"]   # a retry
    other = file(s, log, "Example Neighbour is away at the lake.", "example neighbour", until="2026-12-01")
    rows = (s.root / "facts" / "example-neighbour.md").read_text().splitlines()
    assert other["cite"] == "memory/facts/example-neighbour.md:4" and rows[2] == line and "[until 2026-12-01] (" in rows[3] and len(rows) == 4
    assert "until" not in file(s, log, "Example Neighbour has a red door.", "example neighbour")             # none given: none said


def test_until_with_due_keeps_the_line_dated_as_before_and_the_due_view_lists_it_by_its_due_date(tmp_path, monkeypatch):
    s, log = install(tmp_path)
    clock(monkeypatch, TODAY)
    r = file(s, log, "Example Neighbour returns the ladder.", "example neighbour", due="2026-11-28", owes="Example Neighbour", until=LAST)
    assert (r["due"], r["owes"], r["until"]) == ("2026-11-28", "Example Neighbour", LAST)
    line = line_at(s, r["cite"])
    assert re.fullmatch(r"- Example Neighbour returns the ladder\. \[until 2026-11-30\] " + SRC + r" \[due 2026-11-28 owes Example Neighbour\] \[m:m-1\]", line)
    got = memory.due_of(line)
    assert got and {k: got[k] for k in ("date", "time", "owes", "text", "role")} == {
        "date": "2026-11-28", "time": "", "owes": "Example Neighbour", "text": "Example Neighbour returns the ladder. [until 2026-11-30]", "role": "note"}
    items = due.items_of(s.root / "facts" / "example-neighbour.md", "facts/example-neighbour.md")
    assert [(x["date"], x["cite"]) for x in items] == [("2026-11-28", "facts/example-neighbour.md:3")]
    assert due.is_open(line, date(2026, 12, 12)) and not due.is_open(line, date(2026, 12, 13))        # open: by its due mark alone


def test_a_correction_with_until_puts_the_mark_before_its_supersedes_clause(tmp_path, monkeypatch):
    s, log = install(tmp_path)
    clock(monkeypatch, TODAY)
    first = file(s, log, "Example Neighbour is at home.", "example neighbour")
    r = file(s, log, "Example Neighbour is away at the lake.", "example neighbour", "m-2", until=LAST,
             old=(f"{first['cite'].removeprefix('tree/')} | Example Neighbour is at home",))
    assert r["superseded"] and re.fullmatch(r"- Example Neighbour is away at the lake\. \[until 2026-11-30\] \(supersedes memory/facts/example-neighbour\.md:3\) "
                                            r"\(owner-room, [^)]+\) \[m:m-2\]", line_at(s, r["cite"]))


def test_a_day_log_note_with_until_ends_its_words_with_the_mark_and_the_due_mark_after_it(tmp_path, monkeypatch):
    s, log = install(tmp_path)
    clock(monkeypatch, TODAY)
    plain = file(s, log, "Send the example form back.", until=LAST)
    dated = file(s, log, "Send the example quote back.", "", "m-2", due="2026-11-28", until=LAST)
    head = r"- 12:00:00-05:00 owner-room note: "
    assert re.fullmatch(head + r"Send the example form back\. \[until 2026-11-30\] \[c:private\] \[m:m-1\.n[0-9a-f]{8}\]", line_at(s, plain["cite"]))
    assert re.fullmatch(head + r"Send the example quote back\. \[until 2026-11-30\] \[due 2026-11-28\] \[c:private\] \[m:m-2\.d[0-9a-f]{8}\]",
                        line_at(s, dated["cite"]))
    assert plain["until"] == dated["until"] == LAST and memory.parse_line(line_at(s, plain["cite"])).role == memory.NOTE
    assert memory.due_of(line_at(s, dated["cite"]), "2026-11-25")["text"] == "Send the example quote back. [until 2026-11-30]"
    assert file(s, log, "Send the example form back.", until=LAST)["cite"] == plain["cite"]                  # a retry: one line
    assert file(s, log, "Send the example form back.", until="2026-12-01")["cite"] != plain["cite"]


def test_what_until_refuses_each_in_one_line_and_files_nothing_and_today_is_taken(tmp_path, monkeypatch):
    s, log = install(tmp_path)
    clock(monkeypatch, TODAY)
    for bad in ("2026-02-30", "2026-11-30T10:00", "2026-11-30 10:00", "２０２６-11-30", "٢٠٢٦-11-30",
                "2026-11-30\n", " 2026-11-30", "2026-11-3", "Monday", "20261130"):
        assert file(s, log, "Example Neighbour is away.", "example neighbour", until=bad) == {
            "ok": False, "error": words.UNTIL_SHAPE.format(until=memory.one_line(bad))}, bad
    past = {"ok": False, "error": words.UNTIL_PAST.format(until="2026-11-24", today="2026-11-25")}
    assert file(s, log, "Example Neighbour is away.", "example neighbour", until="2026-11-24") == past
    assert file(s, log, "Example Neighbour is away [until 2026-11-24].", "example neighbour") == past              # typed: the same rule
    assert file(s, log, "Example Neighbour is away [until 2026-02-30].", "example neighbour")["error"] == words.UNTIL_SHAPE.format(until="2026-02-30")
    assert file(s, log, "Example Neighbour is away [until 2026-11-30T10:00].")["error"] == words.UNTIL_SHAPE.format(until="2026-11-30T10:00")
    assert file(s, log, "Example Neighbour is away [until 2026-11-30].", "example neighbour", until=LAST) == {"ok": False, "error": words.UNTIL_TWICE}
    assert file(s, log, "I am away till the 30th", until=LAST, who="Dana", source="message m-9") == {"ok": False, "error": words.UNTIL_SAID}
    assert not any((s.root / "facts").iterdir()) and not any(s.daily.iterdir())
    ok = file(s, log, "Example Neighbour is away.", "example neighbour", until="2026-11-25")                   # today: taken
    typed = file(s, log, "Example Neighbour is away [until 2026-11-25] at the lake.", "example neighbour")      # a typed mark that holds
    said = file(s, log, 'Away [until 2026-11-01], said in jest', who="Dana", source="message m-9")              # their words, as said
    assert ok["ok"] and ok["until"] == "2026-11-25" and typed["ok"] and "until" not in typed and said["ok"] and "until" not in said
    assert "[until 2026-11-25] at the lake. (" in line_at(s, typed["cite"]) and '"Away [until 2026-11-01], said in jest"' in line_at(s, said["cite"])
    assert file(s, log, "Example Neighbour is away [until further notice].", "example neighbour")["ok"]       # no day: words, no mark


def test_until_with_job_or_retire_is_refused_by_the_tool_and_files_nothing(tmp_path, monkeypatch):
    y = daemon(tmp_path)
    monkeypatch.setattr(mcp_server, "caller_room", lambda ctx: "example-room")
    server = mcp_server.build(y, y.port, y.hook)

    def call(**args: str) -> dict:
        got = asyncio.run(server.call_tool("remember", args))
        return json.loads((got[0] if isinstance(got, tuple) else got)[0].text)
    for args in ({"job": "example job"}, {"retire": "tree/rules.md:6"}):
        assert call(text="Send the example form back.", until="2099-12-31", **args) == {"ok": False, "error": words.UNTIL_JOB}, args
    assert not y.memory.daily.exists() and not (y.memory.root / "facts").exists() and not (y.memory.root / memory.RULES).exists()
    r = call(text="Send the example form back.", until="2099-12-31")
    assert r["ok"] and r["until"] == "2099-12-31" and "[until 2099-12-31] [c:" in (y.memory.root / r["cite"].removeprefix("tree/").rpartition(":")[0]).read_text()


# U2: one rule for ended

def note(at: str, body: str, mid: str, role: str = "note") -> str:
    return f"- {at}:00-05:00 owner-room {role}: {body} [c:private] [m:{mid}]"


def test_ended_is_the_one_rule_past_its_last_day_a_real_day_its_commitment_closed_a_rooms_note_only():
    day, src = "2026-11-25", "(owner-room, 2026-11-25 10:00:00-05:00)"
    fact = f"- Ada is away [until 2026-11-30] {src} [m:a]"
    assert [memory.ended(fact, d) for d in (date(2026, 11, 29), date(2026, 11, 30), date(2026, 12, 1))] == [False, False, True]
    assert memory.ended(f"- Ada is away [until 2026-11-30] and back [until 2026-12-05] {src} [m:a]", date(2026, 12, 1))   # any mark past
    for kept in (f"- Ada is away [until 2026-02-30] {src} [m:a]", f"- Ada is away [until 2026-11-30T10:00] {src} [m:a]",
                 f"- Ada is away [until 2026-11-300] {src} [m:a]", f"- Ada is away [until 2026-11-3] {src} [m:a]",
                 "- Ada is away [until ２０２６-11-30] (x, y) [m:a]", "Ada is away [until 2026-11-30] as a heading's words",
                 f"- Ada is away until 2026-11-30 {src} [m:a]", f"- Ada is away {src} [m:a]"):
        assert not memory.ended(kept, date(2026, 12, 9)), kept                    # no real day in the mark, no mark, no bullet
    open_ = f"- Ada returns it [until 2026-11-30] {src} [due 2026-11-28] [m:a]"
    assert not memory.ended(open_, date(2026, 12, 12)) and memory.ended(open_, date(2026, 12, 13))      # open while its mark keeps it
    assert memory.ended(note("10:00", "Away [until 2026-11-30]", "m-1.n12345678"), date(2026, 12, 1), day)
    assert not memory.ended(note("10:00", "Away [until 2026-11-30]", "m-1.n12345678"), date(2026, 11, 30), day)
    assert not memory.ended(note("10:00", "Away [until 2026-11-30]", "m-1", "owner"), date(2026, 12, 1), day)       # a person's line
    assert not memory.ended(note("10:00", 'Dana: "Away [until 2026-11-30]" (from m-9)', "m-1.n1", "said"), date(2026, 12, 1), day)
    assert not memory.ended(note("10:00", "Back [until 2026-11-30] [due 2026-11-28]", "m-1.d12345678"), date(2026, 12, 1), day)
    assert memory.ended(note("10:00", "Back [until 2026-11-30] [due 2026-11-05]", "m-1.d12345678"), date(2026, 12, 1), day)  # closed


def test_the_sweep_blanks_a_line_remember_filed_the_day_after_its_day_never_on_it_nor_while_its_commitment_is_open(tmp_path, monkeypatch):
    s, log = install(tmp_path)
    clock(monkeypatch, TODAY)
    away = file(s, log, "Example Neighbour is away at the lake.", "example neighbour", until=LAST)
    owed = file(s, log, "Example Neighbour returns the ladder.", "example neighbour", "m-2", due="2026-11-28", until=LAST)
    card = s.root / "facts" / "example-neighbour.md"
    before = card.read_text()
    for n, today in enumerate((date(2026, 11, 30), date(2026, 12, 1), date(2026, 12, 12), date(2026, 12, 13))):
        curate.sweep(s, s.root.parent, [], tmp_path / "backups" / str(n), False, today)
        rows = card.read_text().split("\n")
        assert (rows[2] == line_at(s, away["cite"]) == "") == (today > date(2026, 11, 30)), today
        assert (rows[3] == "") == (today > date(2026, 12, 12)), today                   # open by its due mark to 14 days past
    assert (tmp_path / "backups" / "1" / "facts.example-neighbour.md").read_text() == before
    assert len(card.read_text().split("\n")) == len(before.split("\n"))


def test_the_sweep_and_every_reader_ask_the_one_rule(tmp_path, monkeypatch):
    """memory.ended swapped for a probe: a line it calls ended goes from the card and from each reader, so none keeps a rule of its own."""
    s, log = install(tmp_path)
    (s.root / "facts" / "example-ladder.md").write_text("# Example ladder\n- Example ladder: PROBE lent (owner-room, 2026-11-25 10:00:00-05:00) [m:a]\n"
                                                        "- Example ladder: kept (owner-room, 2026-11-25 10:00:00-05:00) [m:b]\n")
    (s.daily / "2026-11-25.md").write_text("# 2026-11-25\n\n" + note("10:00", "Example ladder PROBE decided lent", "m-1.n12345678") + "\n"
                                           + note("10:01", "Example ladder decided kept", "m-2.n12345678") + "\n")
    memindex.Index(s.index_db, s.scope).sync()
    monkeypatch.setattr(memory, "ended", lambda raw, today, day="": "PROBE" in raw)
    now = datetime(2026, 11, 25, 15, 0, tzinfo=TZ)
    deep = [x["cite"] for x in memory.lines_on(s, "example ladder", now=now)]
    topics = [x["cite"] for x in memory.topic_hits(s, ROOM, "the example ladder decided?", now=now)]
    items = [x["cite"] for x in memory.room_items(s, ROOM, now)]
    assert sorted(deep) == ["memory/daily/2026-11-25.md:4", "memory/facts/example-ladder.md:1", "memory/facts/example-ladder.md:3"], deep
    assert topics == ["daily/2026-11-25.md:4"] and items == ["daily/2026-11-25.md:4"], (topics, items)
    curate.sweep(s, s.root.parent, [], tmp_path / "b", False, date(2026, 11, 25))
    assert (s.root / "facts" / "example-ladder.md").read_text().split("\n")[1:3] == ["", "- Example ladder: kept (owner-room, 2026-11-25 10:00:00-05:00) [m:b]"]


# U3: left out on the day, where a line is read per message

LINES = [note("09:00", "Example ladder decided: lent to the neighbour [until 2026-11-30]", "m-1.n00000001"),          # 3: ends
         note("09:01", "Example ladder decided lent [until 2026-11-20]", "m-2", "owner"),                               # 4: a person's
         note("09:02", 'Dana: "Example ladder decided lent [until 2026-11-20]" (from m-9)', "m-3.n00000003", "said"),  # 5: said
         note("09:03", "Example ladder decided: back by Friday [until 2026-11-20] [due 2026-11-28]", "m-4.d00000004"),   # 6: open
         note("09:04", "Example ladder decided: return it [until 2026-11-10] [due 2026-11-05]", "m-5.d00000005"),       # 7: closed
         note("09:05", "Example ladder decided: old plan [until 2026-12-30] [superseded]", "m-6.n00000006"),            # 8: superseded
         note("09:06", "Example ladder decided: painted green", "m-7.n00000007")]                                         # 9: no mark
FACTS = ["- Example ladder: lent to the neighbour [until 2026-11-30] (owner-room, 2026-11-25 09:00:00-05:00) [m:f1]",      # 3: ends
         "- Example ladder: back by Friday [until 2026-11-20] (owner-room, 2026-11-25 09:00:00-05:00) [due 2026-11-28] [m:f2]",  # 4: open
         "- Example ladder: grey aluminium (owner-room, 2026-11-25 09:00:00-05:00) [m:f3]"]                              # 5: no mark


def readers_tree(tmp_path: Path) -> memory.Settings:
    s, _ = install(tmp_path)
    (s.root / "facts" / "example-ladder.md").write_text("# Example ladder\n\n" + "\n".join(FACTS) + "\n")
    (s.daily / "2026-11-25.md").write_text("# 2026-11-25\n\n" + "\n".join(LINES) + "\n")
    memindex.Index(s.index_db, s.scope).sync()
    return s


DAYS = {"the day before": datetime(2026, 11, 29, 12, 0, tzinfo=TZ), "the day itself": datetime(2026, 11, 30, 23, 30, tzinfo=TZ),
        "the day after": datetime(2026, 12, 1, 0, 10, tzinfo=TZ)}        # 23:30 there is 04:30 the next day in UTC


@pytest.mark.parametrize("when", list(DAYS))
def test_deep_recall_leaves_out_an_ended_bullet_and_note_from_the_day_after_and_hands_over_the_rest(tmp_path, when):
    s = readers_tree(tmp_path)
    got = {x["cite"] for x in memory.lines_on(s, "example ladder", None, 8, None, 20, now=DAYS[when])}
    after = when == "the day after"
    day_lines = {f"memory/daily/2026-11-25.md:{n}" for n in (3, 4, 5, 6, 9)} - ({"memory/daily/2026-11-25.md:3"} if after else set())
    facts = {f"memory/facts/example-ladder.md:{n}" for n in (1, 3, 4, 5)} - ({"memory/facts/example-ladder.md:3"} if after else set())
    assert got == day_lines | facts, sorted(got)          # 7 closed past its until, 8 superseded: out on every day, as before


@pytest.mark.parametrize("when", list(DAYS))
def test_the_rooms_topic_lines_and_the_pack_leave_out_an_ended_note_from_the_day_after(tmp_path, when):
    s = readers_tree(tmp_path)
    want = [f"daily/2026-11-25.md:{n}" for n in (9, 6, 5, 4) + (() if when == "the day after" else (3,))]
    topics = [x["cite"] for x in memory.topic_hits(s, ROOM, "what was decided on the example ladder?", now=DAYS[when], limit=20)]
    items = [x["cite"] for x in memory.room_items(s, ROOM, DAYS[when])]
    assert sorted(topics) == sorted(want) and items == want, (topics, items)
    pack = memory.room_pack(s, ROOM, 6000, DAYS[when])[0]
    assert ("lent to the neighbour [until 2026-11-30]" in pack) == (when != "the day after") and "painted green" in pack


def test_a_tree_with_no_mark_reads_the_same_on_any_day(tmp_path):
    s = readers_tree(tmp_path)
    (s.daily / "2026-11-25.md").write_text("# 2026-11-25\n\n" + "\n".join(re.sub(r" \[until [^\]]*\]", "", x) for x in LINES) + "\n")
    (s.root / "facts" / "example-ladder.md").write_text("# Example ladder\n\n" + "\n".join(re.sub(r" \[until [^\]]*\]", "", x) for x in FACTS) + "\n")
    memindex.Index(s.index_db, s.scope).sync()
    got = [(memory.room_pack(s, ROOM, 6000, t), memory.topic_hits(s, ROOM, "the example ladder decided?", now=t),
            memory.lines_on(s, "example ladder", now=t)) for t in DAYS.values()]
    assert got[0] == got[1] == got[2] and got[0][0][1] == 6


# U4: recall and read_memory give an ended line as filed

def test_recall_and_read_memory_give_an_ended_line_as_filed_in_its_place_on_any_day(tmp_path, monkeypatch):
    s = readers_tree(tmp_path)
    seen = []
    for at in DAYS.values():
        clock(monkeypatch, at)
        r = memory.recall(s, "example ladder lent neighbour", 12)
        seen.append(([(h["cite"], h["text"]) for h in r["hits"]], memory.read(s, "memory/facts/example-ladder.md")["text"],
                     memory.read(s, "memory/daily/2026-11-25.md:3", lines=1)["text"]))
    assert seen[0] == seen[1] == seen[2]
    hits, card, day = seen[2]
    assert any("lent to the neighbour [until 2026-11-30] (" in t for _, t in hits) and "lent to the neighbour [until 2026-11-30]" in day
    assert f"- {memory.MARKS[memory.NOTE]} Example ladder: lent to the neighbour [until 2026-11-30] (owner-room, " in card


def test_at_half_past_eleven_on_the_last_day_the_line_still_holds_and_remember_takes_that_day(tmp_path, monkeypatch):
    s, log = install(tmp_path)
    late, next_day = datetime(2026, 11, 30, 23, 30, tzinfo=TZ), datetime(2026, 12, 1, 0, 10, tzinfo=TZ)
    assert late.astimezone(ZoneInfo("UTC")).date() == date(2026, 12, 1) and memory.local_day(s, late) == date(2026, 11, 30)
    clock(monkeypatch, late)
    r = file(s, log, "Example ladder decided: lent to the neighbour.", until="2026-11-30")
    assert r["ok"] and r["cite"].startswith("memory/daily/2026-11-30.md:")
    assert [x["cite"] for x in memory.room_items(s, ROOM, late)] == [r["cite"].removeprefix("memory/")]
    assert [x["cite"] for x in memory.room_items(s, ROOM, late + timedelta(minutes=29))] == [r["cite"].removeprefix("memory/")]      # 23:59
    assert memory.room_items(s, ROOM, next_day) == [] and memory.topic_hits(s, ROOM, "example ladder lent?", now=next_day) == []
    clock(monkeypatch, next_day)
    assert file(s, log, "Example ladder decided: lent again.", until="2026-11-30")["error"] == words.UNTIL_PAST.format(
        until="2026-11-30", today="2026-12-01")
