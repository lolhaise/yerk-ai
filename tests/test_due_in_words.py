"""Checks that a `due` says only what the line's words say. memory.due_unsaid refuses a filing whose text does not name its due's
day in an explicit form (memory.days_in), or, for a due with a time, holds no such clock time (memory.times_in), before anything
is written, and the journal counts each refusal with the room alone. What files with its day and time in its words files as it
did, and build 1b's reader and note are as they were. Every name, line and day here is made up."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from yerk_ai import mcp_server, memindex, memory, words
from test_remember import NOTED, daemon, dated, line_at
from test_until import clock

TODAY = date(2026, 11, 27)
DAY_LINE = words.DUE_DAY_UNSAID.format(day="2026-11-30")
TIME_LINE = words.DUE_TIME_UNSAID.format(time="09:30", day="2026-11-30")
JOURNAL = {DAY_LINE: "remember: a due in example-room refused, its day not in the words",
           TIME_LINE: "remember: a due in example-room refused, its time not in the words"}


def days(text: str, today: date = TODAY) -> set[str]:
    return memory.days_in(text, today)


def times(text: str) -> set[str]:
    return memory.times_in(text)


def noon(y) -> datetime:
    """A stopped clock's time: noon of TODAY in the example install's own zone."""
    return datetime(2026, 11, 27, 12, 0, tzinfo=y.memory.tz)


# the day reader: the forms, and only these

def test_each_day_form_names_its_day_and_no_other():
    for text in ("2026-11-30", "Nov 30", "November 30th", "Nov 30, 2026", "30 Nov", "30th November", "30th Nov 2026", "30th of November, 2026",
                 "Mon Nov 30", "Monday, so Nov 30", "on 30 November."):
        assert days(f"The example visit: {text}") == {"2026-11-30"}, text
    assert days("The example visit: Dec 1, 2 Dec or 2026-12-03.") == {"2026-12-01", "2026-12-02", "2026-12-03"}
    assert days("The example visit, with no day in it.") == set()


def test_today_and_a_past_day_are_named_here_and_build_1bs_reader_still_names_neither():
    assert days("The example form went back Nov 27.") == days("today, 2026-11-27") == days("27 Nov") == {"2026-11-27"}
    assert days("The example form went back Nov 20.") == days("20th November 2026") == {"2026-11-20"}
    assert memory.days_named("The example form went back Nov 27, then Nov 20 and 2026-11-26.", TODAY) == []


def test_a_form_with_no_year_is_the_nearest_such_day_on_both_sides_of_a_years_end():
    dec30, jan2 = date(2026, 12, 30), date(2027, 1, 2)
    assert days("Jan 2", dec30) == days("2 Jan", dec30) == {"2027-01-02"}                    # three days on, never a year behind
    assert days("Dec 30", jan2) == days("30th December", jan2) == {"2026-12-30"}             # three days behind, never a year on
    assert days("Jan 2, 2026", dec30) == {"2026-01-02"} and days("30 Dec 2027", jan2) == {"2027-12-30"}    # a year written is itself
    assert days("May 28") == {"2027-05-28"} and days("May 29") == {"2026-05-29"}            # 182 days on, 182 behind: the nearer


def test_a_weekday_alone_a_relative_word_alone_and_a_numeric_date_never_name_a_day():
    for text in ("Monday", "on Monday", "Mon", "tomorrow", "the day after tomorrow", "next week", "tonight", "12/02", "12/02/2026", "30/11"):
        assert days(f"The example visit: {text}.") == set(), text


def test_a_day_inside_a_file_name_a_path_or_an_id_never_names_one():
    for text in ("notes-2026-11-30.md", "daily/2026-11-30.md:14", "x2026-11-30", "id=2026-11-30", "2026-11-30-notes", "2026-11-30T09:30:00+00:00",
                 "x30 Nov", "id-30 Nov", "v1.30 Nov", "#30 Nov"):
        assert days(f"See {text} for it") == set(), text


def test_the_day_then_month_forms_never_read_a_clock_time_or_a_count_as_a_day():
    assert days("30 November") == {"2026-11-30"} and days("30 new lines") == set()
    assert days("the example call at 9:30 Dec 2") == {"2026-12-02"}                         # never Dec 30
    assert days("the example call at 10.30 Nov 2") == {"2026-11-02"}
    for text in ("3 Mayfair rooms", "12 Decimals", "the 4 Junes", "5 mar", "15 nov", "130 Nov"):
        assert days(f"The example list: {text}.") == set(), text


# the time reader: the forms, and only these

def test_each_time_form_names_its_time():
    for text, want in (("3:00 PM", "15:00"), ("3:00pm", "15:00"), ("3:00 p.m.", "15:00"), ("15:00", "15:00"), ("3 p.m.", "15:00"),
                       ("3 P.M.", "15:00"), ("9 PM", "21:00"), ("9pm", "21:00"), ("9 am", "09:00"), ("7:05 AM", "07:05"), ("noon", "12:00"),
                       ("Noon", "12:00"), ("midnight", "00:00"), ("12 PM", "12:00"), ("12 AM", "00:00"), ("00:30", "00:30")):
        assert times(f"The example call is at {text}, then") == {want}, text


def test_an_hour_1_to_12_with_no_am_or_pm_names_both_halves_of_the_day():
    assert times("at 9:30") == {"09:30", "21:30"} and times("at 12:15") == {"00:15", "12:15"} and times("at 13:15") == {"13:15"}


def test_a_range_names_both_of_its_ends():
    assert {"11:00", "11:30"} <= times("11:00 to 11:30 AM") and "23:30" not in times("11:00 to 11:30 AM")
    assert times("9 to 11 AM") == {"09:00", "11:00"} and times("3pm-4pm") == {"15:00", "16:00"} and times("11 to 1 PM") == {"11:00", "13:00"}
    assert times("between 2 and 3 PM") == {"14:00", "15:00"} and times("8:00 \u2013 9:30 PM") == {"08:00", "20:00", "21:30"}


def test_a_bare_number_is_never_an_hour_and_a_minute_off_by_one_is_not_named():
    for text in ("at 9", "the 3 lines", "9 to 11", "room 12", "30 minutes", "9.30"):
        assert times(f"The example call: {text}.") == set(), text
    assert "09:31" not in times("at 9:30") and "09:29" not in times("at 9:30") and "21:31" not in times("at 9:30")


def test_a_time_inside_a_file_name_or_an_id_is_not_read():
    for text in ("notes-09:30.md", "run_09:30", "id=09:30", "x09:30", "2026-11-30T09:30", "2026-11-30T09:30:00+00:00", "12:30:45", "log/09:30",
                 "#9:30", "4pmx"):
        assert times(f"See {text} for it") == set(), text


# the refusals: their lines, nothing written, the journal

def files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("kw", [{}, {"about": "example boiler"}, {"owes": "Example Roofers"}, {"said": "Dana", "source": "message m-9"}],
                         ids=["note", "card", "owes", "said"])
def test_a_due_whose_day_or_time_the_words_do_not_give_is_refused_in_its_line_and_nothing_is_written(tmp_path, monkeypatch, caplog, kw):
    y = daemon(tmp_path)
    clock(monkeypatch, noon(y))
    for about in ("", "example boiler"):                                                 # a day log and a card with lines, and their index
        assert dated(y, "example-room", "The example boiler check is on Nov 30.", about, due="2026-11-30")["ok"]
    index = memindex.Index(y.memory.index_db, y.memory.scope)
    index.sync()
    index.conn.close()                                                               # its log folded into the file now, not whenever the object dies
    before = files(tmp_path)
    caplog.set_level(logging.INFO, logger="yerk")
    for text, due, line in (("The example boiler check is on Monday.", "2026-11-30", DAY_LINE),
                            ("The example boiler check is on Nov 30.", "2026-11-30T09:30", TIME_LINE),
                            ("The example boiler check is tomorrow morning.", "2026-11-30T09:30", DAY_LINE),        # both: the day's
                            ("The example boiler check is on Nov 30 at 9:00 AM.", "2026-11-30T09:30", TIME_LINE),   # a minute of its own
                            ("The example boiler check is on Nov 30 at 9:29 AM.", "2026-11-30T09:30", TIME_LINE),   # one minute off
                            ("The example boiler check is Nov 30, 2027 at 9:30.", "2026-11-30T09:30", DAY_LINE)):
        about = kw.get("about", "")
        assert dated(y, "example-room", text, about, **{k: v for k, v in kw.items() if k != "about"}, due=due) == {"ok": False, "error": line}, text
    assert files(tmp_path) == before                                                     # the day log, the card, the index: as they were
    said = [x.getMessage() for x in caplog.records if x.getMessage().startswith("remember: ")]
    assert said == [JOURNAL[x] for x in (DAY_LINE, TIME_LINE, DAY_LINE, TIME_LINE, TIME_LINE, DAY_LINE)]     # and no line of build 1b's
    assert not any(w in caplog.text for w in ("boiler", "Monday", "tomorrow", "9:30", "Nov 30", "2026-11-30", "Dana", "Roofers"))


def test_the_refusals_words_are_these_word_for_word():
    assert words.DUE_DAY_UNSAID == ("these words do not name {day}: write the day in the line as the words gave it or as you worked it out from them "
                                    "(\"Monday, so Nov 30\"), or file it without `due`")
    assert words.DUE_TIME_UNSAID == "these words give no {time}: file it by its day alone ({day}), or write the time in the line as the words gave it"
    assert DAY_LINE.startswith("these words do not name 2026-11-30: ") and TIME_LINE.startswith("these words give no 09:30: file it by its day alone (2026-11-30)")


def test_a_shape_refusal_comes_before_the_words_and_is_not_counted_as_theirs(tmp_path, caplog):
    y = daemon(tmp_path)
    caplog.set_level(logging.INFO, logger="yerk")
    assert dated(y, "example-room", "The example visit is on Monday.", due="Monday") == {"ok": False, "error": words.DUE_SHAPE.format(due="Monday")}
    assert dated(y, "example-room", "The example visit.", due="2026-11-30", owes="Dana]")["error"] == words.DUE_NAME.format(owes="Dana]")
    assert "remember: " not in caplog.text


def test_a_line_that_replaces_another_is_checked_and_a_refusal_leaves_the_old_line_as_it_was(tmp_path, monkeypatch):
    y = daemon(tmp_path)
    clock(monkeypatch, noon(y))
    old = dated(y, "example-room", "The example boiler check is on Nov 30.", due="2026-11-30")
    was = line_at(y, old["cite"])
    moved = dated(y, "example-room", "The example boiler check moved to Wednesday.", "", (old["cite"],), due="2026-12-02")
    assert moved == {"ok": False, "error": words.DUE_DAY_UNSAID.format(day="2026-12-02")} and line_at(y, old["cite"]) == was
    moved = dated(y, "example-room", "The example boiler check moved to Wednesday, so Dec 2.", "", (old["cite"],), due="2026-12-02")
    assert moved["ok"] and moved["superseded"] == [old["cite"]] and "[superseded" in line_at(y, old["cite"])


def test_a_due_of_today_or_of_a_day_behind_is_checked_like_a_coming_one(tmp_path, monkeypatch):
    y = daemon(tmp_path)
    clock(monkeypatch, noon(y))
    for due in ("2026-11-27", "2026-11-20"):
        refused = {"ok": False, "error": words.DUE_DAY_UNSAID.format(day=due)}
        assert dated(y, "example-room", "The example form goes back this week.", due=due) == refused, due
    assert dated(y, "example-room", "The example form goes back today, Nov 27.", due="2026-11-27")["ok"]
    assert dated(y, "example-room", "The example form went back on Nov 20.", due="2026-11-20")["ok"]


def test_the_day_and_the_time_are_read_on_every_line_of_the_words(tmp_path, monkeypatch):
    y = daemon(tmp_path)
    clock(monkeypatch, noon(y))
    said = "The example boiler check is booked.\nIt is on Nov 30.\nThe engineer comes at 9:30 AM."
    r = dated(y, "example-room", said, due="2026-11-30T09:30")
    assert r["ok"] and r["due"] == "2026-11-30T09:30" and line_at(y, r["cite"]).count(" / ") == 2


# what still files: main's line and answer, the same bytes as with no check at all

def card(said: str, mark: str = "") -> str:
    return re.escape(f"- {said}") + r" \(example-room#discord:CHANNEL_ID, [^)]+\)" + re.escape(mark) + r" \[m:m-1\]"


def noted(said: str, mark: str = "", kind: str = "d") -> str:
    return NOTED + re.escape(f"{said}{mark}") + r" \[c:private\] \[m:m-1\." + kind + r"[0-9a-f]{8}\]"


@pytest.mark.parametrize("text, about, kw, line", [
    ("The example boiler check is on Nov 30.", "", {"due": "2026-11-30"},                                   # a day in the words
     noted("note: The example boiler check is on Nov 30.", " [due 2026-11-30]")),
    ("The example boiler check is on 30 November.", "example boiler", {"due": "2026-11-30"},
     card("The example boiler check is on 30 November.", " [due 2026-11-30]")),
    ("The example call is Monday, so Nov 30, at 3:00 PM.", "", {"due": "2026-11-30T15:00"},                 # its time in the words
     noted("note: The example call is Monday, so Nov 30, at 3:00 PM.", " [due 2026-11-30T15:00]")),
    ("The example call is Nov 30, 11:00 to 11:30 AM.", "", {"due": "2026-11-30T11:30"},
     noted("note: The example call is Nov 30, 11:00 to 11:30 AM.", " [due 2026-11-30T11:30]")),
    ("The example quote, by 2026-11-30.", "", {"due": "2026-11-30", "owes": "Example Roofers"},             # owes
     noted("note: The example quote, by 2026-11-30.", " [due 2026-11-30 owes Example Roofers]")),
    ("I will bring the key on the second (so Dec 2)", "", {"due": "2026-12-02", "said": "Dana", "source": "message m-9"},
     noted('said: Dana: "I will bring the key on the second (so Dec 2)" (from message m-9)', " [due 2026-12-02]")),
    ("Example Neighbour is away at the lake.", "example neighbour", {"until": "2026-11-30"},                 # until
     card("Example Neighbour is away at the lake. [until 2026-11-30]")),
    ("Example Neighbour returns the ladder on Nov 28.", "example neighbour", {"due": "2026-11-28", "until": "2026-11-30"},
     card("Example Neighbour returns the ladder on Nov 28. [until 2026-11-30]", " [due 2026-11-28]")),
    ("The example piano tuner comes Dec 2 at 9 AM.", "", {}, noted("note: The example piano tuner comes Dec 2 at 9 AM.", kind="n")),  # 1b's note
    ("The example piano was tuned on Nov 20.", "", {}, noted("note: The example piano was tuned on Nov 20.", kind="n")),
], ids=["day", "day-card", "time", "range", "owes", "said", "until", "until-due", "no-due-coming", "no-due-past"])
def test_what_files_with_its_day_and_time_in_its_words_files_mains_line_and_answer(tmp_path, monkeypatch, caplog, text, about, kw, line):
    for d in ("checked", "plain"):
        (tmp_path / d).mkdir()
    a, b = daemon(tmp_path / "checked"), daemon(tmp_path / "plain")
    clock(monkeypatch, noon(a))
    caplog.set_level(logging.INFO, logger="yerk")
    got = dated(a, "example-room", text, about, **kw)
    monkeypatch.setattr(memory, "due_unsaid", lambda *args: "")                          # remember with no check, as on main
    was = dated(b, "example-room", text, about, **kw)
    assert got["ok"] and got == was and line_at(a, got["cite"]) == line_at(b, was["cite"]) and re.fullmatch(line, line_at(a, got["cite"])), got
    assert ("day" in got) == (not kw and "Dec 2" in text) and "refused" not in caplog.text
    assert (got.get("due"), got.get("owes"), got.get("until")) == (kw.get("due"), kw.get("owes"), kw.get("until"))


def test_build_1bs_note_and_its_journal_line_are_as_they_were(tmp_path, monkeypatch, caplog):
    y = daemon(tmp_path)
    clock(monkeypatch, noon(y))
    caplog.set_level(logging.INFO, logger="yerk")
    r = dated(y, "example-room", "The example piano tuner comes Dec 2 and 3 Dec.")         # the day-then-month form is not 1b's
    assert r["day"] == words.DAY_NAMED.format(days="2026-12-02")
    assert [x.getMessage() for x in caplog.records if x.getMessage().startswith("remember: ")] == [
        "remember: a filing in example-room named 1 coming day(s) with no due"]


# the tool's words

def test_the_tools_words_say_the_day_goes_in_the_line_and_a_minute_is_never_chosen(tmp_path):
    y = daemon(tmp_path)
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    said = " ".join(str(tools["remember"].description).split())
    assert ("`owes`, with it: who owes it to the owner, empty when the owner does. Give a day only when the words give one or the clock line "
            "lets you work it out, and write that day in the line (\"Monday, so Nov 30\"): a `due` whose day the line does not name is refused. A "
            "time goes in only when the words give the hour (a meeting's, a booking's): a thing with a day and no hour is filed by its day, and a "
            "minute of your own choosing is never one. What might happen then (a call someone may make) is not a thing due: it stays in the "
            "line's words. Your own plan to act at a set time is a `schedule` job. Clear a thing by filing its outcome with `replaces` naming its "
            "line. `until`: ") in said
    assert "Give a date only when the words give one or the clock line lets you work it out, never as a guess." not in said
    assert "never as a guess" not in said and said.count("`schedule`") == 1
