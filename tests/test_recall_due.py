"""Checks that a `recall` search whose day range reaches past today also returns the still-open lines filed with a due date in that
range, complete, with no ranking and no model (due.ahead, due.ranged, due.asked), while every other call answers as before, byte for
byte. This is what is due on a day past the week (build 1c of the owner's build list of Wed Nov 25). Every name, line and day is made up,
off any day the suite runs on, at an hour where UTC is already on the next day."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from yerk_ai import daemon, due, memindex, memory, words
from yerk_ai.daemon import Yerkd

from test_room_scope import install

TZ = ZoneInfo("America/Vancouver")
NOW = datetime(2019, 3, 12, 20, 0, tzinfo=TZ)           # 2019-03-13 03:00 UTC: UTC is on the next day, the install is not
TODAY = "2019-03-12"
ROOM, ASK = "example-room", "example kettle"
FILED_IN = "(example-room#rid, 2019-03-01 09:00:00+00:00)"


def on(n: int) -> str:
    """The install's day `n` days from NOW's."""
    return (NOW.date() + timedelta(days=n)).isoformat()


def clock(monkeypatch: pytest.MonkeyPatch, at: datetime = NOW) -> None:
    """The daemon's clock stopped at `at`: the day `recall` takes as today and the hour its list is read at."""
    class At(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)
    monkeypatch.setattr(daemon, "datetime", At)


def yerkd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: bool) -> Yerkd:
    """The example layer's daemon in TZ, its clock at NOW."""
    y = install(tmp_path, **kw)
    y.memory = replace(y.memory, tz=TZ)
    clock(monkeypatch)
    return y


def dated(y: Yerkd, *lines: str, name: str = "example-dates", head: str = "") -> list[dict]:
    """A fact file of dated lines (each `words | mark`, the mark `YYYY-MM-DD` or with `THH:MM`), and the list yerk-index writes."""
    rows = [f"- {w} {FILED_IN} [due {m}] [m:m-{name}-{i}]" for i, (w, _, m) in enumerate(x.partition(" | ") for x in lines)]
    (y.memory.root / "facts").mkdir(exist_ok=True)
    (y.memory.root / "facts" / f"{name}.md").write_text(f"{head}# Example dates\n\n" + "\n".join(rows) + "\n")
    due.Builder(y.memory).refresh(y.memory.scope.files(), NOW)
    return y.owed.load()


def ask(y: Yerkd, since: str = "", until: str = "", room: str = ROOM, query: str = ASK) -> dict:
    return asyncio.run(y.tool_recall(room, query, 12, since, until))


def texts(r: dict) -> list[str]:
    """The words of each line of `due`, its date, time and cite off."""
    return [re.sub(r"^- [0-9-]+(?: [0-9:]+)?: (.*) \(memory/[^()]+\)$", r"\1", x) for x in r["due"]]


# D1: due.ahead, the one rule of when the list is asked
@pytest.mark.parametrize("since, until, past", [
    ("", "2019-03-13", True), ("", TODAY, False), ("", "2019-03-11", False),            # until after today, today, before it
    ("2019-03-13", "", True), ("2019-03-13", "2019-03-20", True),                    # since after today, alone and with until
    (TODAY, "", False), ("2019-03-01", "", False), ("", "", False),                  # since today, since before it alone, no range
    ("2019-03-01", "2019-03-13", True), ("2019-03-01", TODAY, False)])                # a range from the past that ends tomorrow, today
def test_a_range_reaches_past_today_when_until_or_since_is_after_it(since, until, past):
    assert due.ahead(since, until, date(2019, 3, 12)) is past


def test_today_is_the_installs_own_day_while_utc_is_already_on_the_next(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    dated(y, f"Example tomorrow | {on(1)}", f"Example today | {on(0)}")
    assert datetime(2019, 3, 13, 3, 0, tzinfo=timezone.utc) == NOW and on(1) == "2019-03-13"
    tomorrow = ask(y, "", on(1))                                                    # UTC's today, the install's tomorrow: asked
    assert texts(tomorrow) == ["Example today", "Example tomorrow"]
    plain = ask(y, "", on(0))                                                       # the install's today: never asked
    assert "due" not in plain and "due_line" not in plain


# D1: due.ranged, what the range holds
def test_the_range_holds_both_its_ends_and_nothing_a_day_outside_them(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    items = dated(y, f"Example day before | {on(4)}", f"Example since | {on(5)}", f"Example until | {on(9)}", f"Example day after | {on(10)}",
                  f"Example overdue | {on(-3)}", f"Example today | {on(0)}")
    s, rd = y.memory, y.reader(ROOM)

    def got(since: str, until: str) -> list[str]:
        return [x["text"] for x in due.ranged(s, items, rd, since, until, NOW)]
    assert got(on(5), on(9)) == ["Example since", "Example until"]
    assert got(on(5), "") == ["Example since", "Example until", "Example day after"]                     # since alone: from then on
    assert got("", on(9)) == ["Example overdue", "Example today", "Example day before", "Example since", "Example until"]   # the overdue in
    assert got(on(7), on(7)) == [] and got(on(5), on(5)) == ["Example since"]
    assert [x["cite"] for x in due.ranged(s, items, rd, "", "", NOW)] == [x["cite"] for x in due.listed(s, items, rd, NOW)]


def test_a_day_with_two_gives_them_in_the_lists_order_the_one_with_no_time_first(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    dated(y, f"Example at nine | {on(20)}T09:00", f"Example all day | {on(20)}", f"Example next day | {on(21)}")
    r = ask(y, on(20), on(20))
    assert r["due"] == [f"- {on(20)}: Example all day (memory/facts/example-dates.md:4)",
                        f"- {on(20)} 09:00: Example at nine (memory/facts/example-dates.md:3)"]


# D1: the reader's own rule, and what is closed
def test_the_list_is_what_the_room_may_recall_another_rooms_line_for_an_owner_room_and_never_for_a_sealed_one(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch, sealed=True, family=True)
    hers = y.reader("family-room").rid
    dated(y, f"Example club fee | {on(12)}", name="example-club", head=f"---\nclass: room\nroom: {hers}\n---\n")
    dated(y, f"Example owner errand | {on(12)}")
    assert texts(ask(y, on(10), on(14))) == ["Example club fee", "Example owner errand"]                 # an owner room reads both
    assert texts(ask(y, on(10), on(14), "family-room")) == ["Example club fee"]                         # her room its own line alone
    assert texts(ask(y, on(10), on(14), "frozen-room")) == []                                            # a sealed room neither


def test_a_line_marked_do_not_surface_is_in_the_asked_list_and_still_out_of_the_part(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    items = dated(y, "⛔ Example renewal, never raised. | " + on(3), f"Example deposit, ask-only. | {on(3)}", f"Example form. | {on(3)}")
    assert texts(ask(y, on(1), on(5))) == ["⛔ Example renewal, never raised.", "Example deposit, ask-only.", "Example form."]
    part = due.part(y.memory, items, y.reader(ROOM), 10 ** 6, NOW)
    assert "Example form." in part and "Example renewal" not in part and "Example deposit" not in part


def test_a_cleared_a_superseded_and_a_line_fifteen_days_past_are_out_and_one_fourteen_days_past_is_in(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    f = y.memory.root / "facts" / "example-dates.md"
    dated(y, f"Example cleared | {on(2)}", f"Example superseded | {on(2)}", f"Example later | {on(2)}",
          f"Example fourteen days past | {on(-14)}", f"Example fifteen days past | {on(-15)}", f"Example open | {on(2)}")
    f.write_text(f.read_text().replace("[m:m-example-dates-1]", "[superseded by memory/facts/example-boiler.md:3] [m:m-example-dates-1]"))
    (y.memory.root / memory.LATER).write_text("memory/facts/example-dates.md:5\tmemory/facts/example-boiler.md:4\n")
    out = memory.remember(y.memory, y.daily, ROOM, "m-9", "Example cleared: done.", "example boiler", old=("memory/facts/example-dates.md:3",),
                          reader=y.reader(ROOM), now=NOW)
    assert out["ok"] and memory.mentions.OLD in f.read_text().splitlines()[2]
    due.Builder(y.memory).refresh(y.memory.scope.files(), NOW)
    assert texts(ask(y, "", on(3))) == ["Example fourteen days past", "Example open"]


def test_a_dated_line_whose_until_day_is_past_stays_listed_while_its_date_is_open_and_is_not_ended(tmp_path, monkeypatch):
    """PREMISE 2, left as it is: `listed` goes by the date alone (KEEP days past it), never by an `until` mark, and memory.ended
    keeps such a line for every reader while it is open (due.is_open); once its date is past KEEP it leaves the list and is ended."""
    y = yerkd(tmp_path, monkeypatch)
    items = dated(y, f"Example offer, good until the 5th [until {on(-7)}] | {on(2)}", f"Example voucher [until {on(-7)}] | {on(-15)}")
    raw = (y.memory.root / "facts" / "example-dates.md").read_text().splitlines()
    assert [x["text"] for x in due.listed(y.memory, items, y.reader(ROOM), NOW)] == [f"Example offer, good until the 5th [until {on(-7)}]"]
    assert memory.ended(raw[2], NOW.date()) is False and memory.ended(raw[3], NOW.date()) is True
    assert texts(ask(y, on(1), on(5))) == [f"Example offer, good until the 5th [until {on(-7)}]"]


# D1: the answer, its keys and its line
def test_a_range_past_today_returns_due_line_then_due_right_before_the_hits_which_are_as_they_were(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    (y.memory.root / f"example-notes-{on(20)}.md").write_text(f"# {on(20)}\n\n- the example kettle goes back on the shelf\n")
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    dated(y, f"Example kettle back | {on(20)}")
    found = ask(y, on(20), on(20))
    assert list(found) == ["ok", "status", "due_line", "due", "hits"] and found["hits"]
    assert found["due_line"] == words.RECALL_DUE.format(days=on(20), n=1) and found["due"] == [
        f"- {on(20)}: Example kettle back (memory/facts/example-dates.md:3)"]
    assert json.dumps(found["hits"]) == json.dumps(y.search(ROOM, ASK, 12, since=on(20), until=on(20))["hits"])
    wide = ask(y, on(30), on(31))                                                    # nothing named in it: widened, its line first
    assert list(wide) == ["ok", "status", "widened", "line", "due_line", "due", "hits"] and wide["due"] == []
    assert wide["line"] == words.RECALL_WIDENED.format(days=f"from {on(30)} until {on(31)}")
    assert json.dumps(wide["hits"]) == json.dumps(y.search(ROOM, ASK, 12, since=on(30), until=on(31))["hits"]) and wide["hits"]


def test_twelve_are_shown_whole_and_thirteen_give_the_first_twelve_and_say_how_many(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    dated(y, *(f"Example thing {i:02d} | {on(10 + i)}" for i in range(13)))
    twelve = ask(y, on(10), on(21))
    assert len(twelve["due"]) == 12 and twelve["due_line"] == words.RECALL_DUE.format(days=f"from {on(10)} until {on(21)}", n=12)
    r = ask(y, on(10), on(22))
    assert texts(r) == [f"Example thing {i:02d}" for i in range(12)]                    # the soonest first, the latest left out
    assert r["due_line"] == words.RECALL_DUE_MORE.format(days=f"from {on(10)} until {on(22)}", shown=12, n=13)
    assert ask(y, on(10), "")["due_line"] == words.RECALL_DUE_MORE.format(days=f"from {on(10)}", shown=12, n=13)
    assert ask(y, "", on(21))["due_line"] == words.RECALL_DUE.format(days=f"until {on(21)}", n=12)
    none = ask(y, on(40), on(40))
    assert none["due"] == [] and none["due_line"] == words.RECALL_DUE_NONE.format(days=on(40))


def test_the_retired_marks_are_dropped_and_a_long_line_cut_as_the_part_shows_it(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    y.gates = replace(y.gates, retired=("\U0001f7e1",))
    items = dated(y, f"\U0001f7e1 Example retired mark | {on(9)}", f"{'Example long words ' * 20}| {on(9)}")
    r = ask(y, on(9), on(9))
    assert r["due"] == [due.line(x, y.gates.retired) for x in due.listed(y.memory, items, y.reader(ROOM), NOW)]
    assert "\U0001f7e1" not in r["due"][0] and "…" in r["due"][1]


# D1: every other call, byte for byte
def test_no_range_a_range_ending_today_since_alone_before_today_and_a_past_range_answer_as_before(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    dated(y, f"Example overdue | {on(-2)}", f"Example today | {on(0)}", f"Example next week | {on(5)}", f"Example far | {on(30)}")
    calls = (("", ""), ("", on(0)), (on(-5), on(0)), (on(0), on(0)), (on(-5), ""), (on(0), ""), (on(-30), on(-20)), ("", on(-1)))
    plain = {c: ask(y, *c) for c in calls}

    def boom(*a: object, **k: object) -> list:
        raise AssertionError("due.ranged was asked")
    monkeypatch.setattr(due, "ranged", boom)
    for c in calls:
        assert json.dumps(ask(y, *c)) == json.dumps(plain[c]) == json.dumps(y.search(ROOM, ASK, 12, since=c[0], until=c[1])), c
        assert not {"due", "due_line"} & set(plain[c]), c
    with pytest.raises(AssertionError, match="due.ranged was asked"):
        ask(y, on(1), on(1))


def test_a_refusal_and_a_search_that_did_not_finish_carry_neither_key(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    dated(y, f"Example far | {on(30)}")
    for since, until in (("Friday", ""), ("", "2019-02-30"), (on(31), on(30))):
        r = ask(y, since, until)
        assert r["ok"] is False and r["status"] == "refused" and not {"due", "due_line"} & set(r), (since, until)
    late = {"ok": False, "status": "timeout", "error": "the index was busy"}
    monkeypatch.setattr(y, "search", lambda *a, **k: dict(late))
    assert ask(y, on(30), on(30)) == late


def test_the_journal_says_how_many_were_due_when_asked_and_never_a_day_or_a_word(tmp_path, monkeypatch, caplog):
    y = yerkd(tmp_path, monkeypatch)
    dated(y, *(f"Example thing {i:02d} | {on(10 + i)}" for i in range(13)))
    with caplog.at_level(logging.INFO, logger="yerk.daemon"):
        ask(y, on(10), on(22))
        ask(y, on(40), on(40))
        ask(y)
        ask(y, "", on(0))
        ask(y, "Friday", "")
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith("recall: ")]
    assert len(said) == 5 and said[0].endswith(", widened, 13 due") and said[1].endswith(", widened, 0 due")
    assert all(" due" not in x for x in said[2:]) and said[4] == "recall: example-room, 0 hit(s), refused"
    assert not any(re.search(r"\d{4}-\d\d-\d\d|Example|thing", x) for x in said), said


# D2: the words, as written
def test_the_tool_says_a_range_past_today_returns_due_and_the_three_lines_say_what_the_list_is(tmp_path, monkeypatch):
    from test_recall_day import DUE_SAID
    from yerk_ai import mcp_server
    assert DUE_SAID == ("A range that reaches past today also returns `due`: the open dated lines due in it that this room may read, the "
                        "soonest twelve, whatever the query's words. That list is what answers \"what is on that day\" or \"what do I have "
                        "that week\", and an empty one means nothing on file is due then.")
    y = yerkd(tmp_path, monkeypatch)
    monkeypatch.setattr(mcp_server, "caller_room", lambda ctx: ROOM)
    for tools in (mcp_server.build(y, y.port, y.hook), mcp_server.worker_server(tmp_path, ROOM, ["recall"])):
        said = " ".join(str(next(t for t in asyncio.run(tools.list_tools()) if t.name == "recall").description).split())
        assert said.endswith("and leave them out when it names no time. " + DUE_SAID)
    assert words.RECALL_DUE.format(days="2019-04-01", n=3) == ("Due 2019-04-01, every open dated line this room may read, the soonest first "
                                                                  "(3). A thing filed with no `due` is not among them.")
    assert words.RECALL_DUE_MORE.format(days="from 2019-04-01 until 2019-04-30", shown=12, n=13) == (
        "Due from 2019-04-01 until 2019-04-30, the first 12 of 13 open dated lines this room may read, the soonest first; a narrower "
        "range shows the rest. A thing filed with no `due` is not among them.")
    assert words.RECALL_DUE_NONE.format(days="until 2019-04-30") == ("Due until 2019-04-30: no open dated line this room may read. A "
                                                                     "thing filed with no `due` would not be here.")


# D3: the part says so when everything open is further out
FAR_TWO = ("[due] As of 2019-03-12 20:00: nothing overdue or due in the next 7 days; 2 open further out, which `recall` with `since` and "
           "`until` lists by day.")


def test_the_part_with_nothing_near_and_two_further_out_is_one_line_saying_so_and_with_nothing_open_none(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    s, rd = y.memory, y.reader(ROOM)
    far = dated(y, f"Example library | {on(8)}", f"Example renewal | {on(40)}", f"Example gone | {on(-15)}")
    assert due.part(s, far, rd, 10 ** 6, NOW) == FAR_TWO == due.FAR.format(at="2019-03-12 20:00", days=7, n=2)
    assert due.part(s, far, rd, len(FAR_TWO), NOW) == FAR_TWO and due.part(s, far, rd, len(FAR_TWO) - 1, NOW) == ""   # it fits, or none
    assert due.part(s, dated(y, f"Example gone | {on(-15)}"), rd, 10 ** 6, NOW) == "" and due.part(s, [], rd, 10 ** 6, NOW) == ""
    assert due.part(s, dated(y, "⛔ Example renewal, never raised. | " + on(40)), rd, 10 ** 6, NOW) == ""   # none it may show unasked
    theirs = dated(y, f"Example club fee | {on(40)}", name="example-club", head="---\nclass: room\nroom: her-id\n---\n")
    assert due.part(s, theirs, rd, 10 ** 6, NOW) == "" and "Example club fee" in [x["text"] for x in due.listed(s, theirs, rd, NOW)]


def test_the_part_with_one_near_and_one_further_out_is_the_part_its_last_line_saying_recall_lists_the_rest_by_range(tmp_path, monkeypatch):
    y = yerkd(tmp_path, monkeypatch)
    items = dated(y, f"Example library | {on(3)}", f"Example renewal | {on(40)}")
    rows = due.part(y.memory, items, y.reader(ROOM), 10 ** 6, NOW).split("\n")
    assert rows == [due.HEAD.format(at="2019-03-12 20:00", days=7), f"- {on(3)}: Example library (memory/facts/example-dates.md:3)",
                    "(1 more open, not shown; `recall` with `since` and `until` lists what is due in a range)"]
    assert due.MORE == "({} more open, not shown; `recall` with `since` and `until` lists what is due in a range)"
