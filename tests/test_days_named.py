"""Checks that a room filing a note that names a coming day but gives no date is told so. memory.days_named reads the days still
to come that a line's words name in an explicit form, with no model, and `remember`'s answer gains `day` after `cite` when the
line was filed with no `due` and no `until`. The room decides: the line is filed byte for byte as it would be without the note,
is dated by code. Every name, line and day here is made up."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from yerk_ai import mcp_server, memory, words
from test_remember import daemon, dated, line_at
from test_until import clock

TODAY = date(2026, 11, 26)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def named(text: str) -> list[str]:
    return memory.days_named(text, TODAY)


def note(*days: str) -> str:
    return words.DAY_NAMED.format(days=", ".join(days))


def noon(y) -> datetime:
    """A stopped clock's time: noon of TODAY in the example install's own zone, whatever zone the machine runs in."""
    return datetime(2026, 11, 26, 12, 0, tzinfo=y.memory.tz)


# days_named: the forms, and only these

def test_each_form_names_its_day_a_weekday_in_front_and_a_comma_before_the_year_too():
    assert named("The example visit is 2026-12-02.") == ["2026-12-02"]
    assert named("The example visit is 2026-12-02T09:30 at the hall") == ["2026-12-02"]
    assert named("The example visit is Nov 27") == ["2026-11-27"]
    assert named("The example visit is Fri Nov 27") == ["2026-11-27"]
    assert named("The example visit is November 27, 2026") == ["2026-11-27"]
    assert named("The example visit is November 27 2026") == ["2026-11-27"]
    assert named("The example visit is Dec 2") == ["2026-12-02"]
    assert named("The example visit is Sept 5") == named("The example visit is Sep 5") == named("on September 5") == ["2027-09-05"]


def test_a_form_with_no_year_is_the_next_such_day_across_the_turn_of_the_year_and_a_year_already_past_is_none():
    assert named("Dec 30, then Jan 5") == ["2026-12-30", "2027-01-05"]
    assert named("the example check of Nov 20") == ["2027-11-20"]           # this year's has passed: the coming one
    assert named("the example check of Jan 5, 2026") == named("2025-12-02") == named("Dec 2, 2025") == []
    assert named("the example check of Jan 5, 2027") == ["2027-01-05"]


def test_today_and_yesterday_are_never_named_and_day_366_is_in_and_day_367_out():
    for text in ("Nov 26", "2026-11-26", "November 26, 2026", "Nov 25, 2026", "2026-11-25"):
        assert named(f"The example form was sent {text}.") == [], text
    assert TODAY + timedelta(days=366) == date(2027, 11, 27)
    assert named("2027-11-27") == named("Nov 27, 2027") == ["2027-11-27"]
    assert named("2027-11-28") == named("Nov 28, 2027") == []


def test_a_form_that_is_no_real_day_is_skipped_and_the_real_day_beside_it_kept():
    assert named("Feb 30 or Mar 2") == ["2027-03-02"]
    assert named("2026-02-30 or 2026-12-32 or 2026-13-02, else 2026-12-03") == ["2026-12-03"]


def test_a_month_in_lower_case_a_month_with_no_day_a_numeric_date_a_relative_word_and_a_bare_weekday_name_none():
    assert named("may 5") == [] and named("May 5") == ["2027-05-05"]
    for text in ("Nov 2026", "12/02", "12/02/2026", "tomorrow", "next week", "Friday", "on Friday", "the 3rd", "nov 27", "NOV 27"):
        assert named(f"The example visit: {text}.") == [], text


def test_a_date_inside_a_file_name_a_path_or_an_id_is_no_day_and_one_ending_a_sentence_is():
    for text in ("notes-2026-12-02.md", "daily/2026-12-02.md:14", "tree/daily/2026-12-02.md", "x2026-12-02", "id=2026-12-02",
                 "2026-12-02-notes", "2026-12-02.md", "2026-12-02:14", "2026-12-02T09:30:00+00:00"):
        assert named(f"See {text} for it") == [], text
    for text in ("on 2026-12-02.", "(on 2026-12-02)", "[2026-12-02]", "on 2026-12-02, then", "2026-12-02: the visit"):
        assert named(text) == ["2026-12-02"], text


def test_one_day_named_twice_is_listed_once_and_five_days_give_the_three_soonest_in_order():
    assert named("Dec 2, that is 2026-12-02, so December 2") == ["2026-12-02"]
    assert named("Dec 9, Dec 2, Dec 5, Dec 1 or Dec 7") == ["2026-12-01", "2026-12-02", "2026-12-05"]


# remember: the note, and the line it never changes

def same_line(tmp_path, monkeypatch, about: str = "", **kw: str) -> tuple[dict, dict, str]:
    """The same call filed twice with memory's clock stopped, in two trees: with days_named, and with it giving none."""
    for d in ("told", "plain"):
        (tmp_path / d).mkdir()
    a, b, text = daemon(tmp_path / "told"), daemon(tmp_path / "plain"), "The example piano tuner comes Dec 2 and 2026-12-03."
    clock(monkeypatch, noon(a))
    told = dated(a, "example-room", text, about, **kw)
    monkeypatch.setattr(memory, "days_named", lambda text, today: [])
    plain = dated(b, "example-room", text, about, **kw)
    assert line_at(a, told["cite"]) == line_at(b, plain["cite"])           # byte for byte, its stamp too
    return told, plain, line_at(a, told["cite"])


@pytest.mark.parametrize("about", ["", "example piano"])
def test_a_coming_day_with_no_due_answers_with_day_after_cite_and_next_last_and_files_the_same_bytes(tmp_path, monkeypatch, about):
    told, plain, line = same_line(tmp_path, monkeypatch, about)
    assert told["day"] == note("2026-12-02", "2026-12-03") and list(told)[:3] == ["ok", "cite", "day"] and list(told)[-1] == "next"
    assert {k: v for k, v in told.items() if k != "day"} == plain and "day" not in plain
    assert told["cite"].startswith("tree/facts/example-piano.md:" if about else "tree/daily/2026-11-26.md:")
    assert "[due" not in line and "The example piano tuner comes Dec 2 and 2026-12-03." in line


def test_a_persons_words_are_told_the_same_way_and_filed_the_same_bytes(tmp_path, monkeypatch):
    told, plain, line = same_line(tmp_path, monkeypatch, said="Dana", source="message m-9")
    assert told["day"] == note("2026-12-02", "2026-12-03") and list(told)[:3] == ["ok", "cite", "day"] and list(told)[-1] == "next"
    assert {k: v for k, v in told.items() if k != "day"} == plain
    assert 'said: Dana: "The example piano tuner comes Dec 2 and 2026-12-03." (from message m-9) [c:private]' in line


def test_with_due_with_until_on_a_refusal_and_with_only_past_or_todays_days_there_is_no_day(tmp_path, monkeypatch):
    y, text = daemon(tmp_path), "The example piano tuner comes Dec 2."
    clock(monkeypatch, noon(y))
    assert dated(y, "example-room", "The example piano tuner comes Dec 3.")["day"] == note("2026-12-03")      # the same words, told
    with_due = dated(y, "example-room", text, due="2026-12-02")
    with_until = dated(y, "example-room", text, until="2026-12-02")
    assert with_due["ok"] and with_until["ok"] and "day" not in with_due and "day" not in with_until
    for refused in (dated(y, "example-room", text, said="Dana"), dated(y, "example-room", text, said="Dana", source="m-9", until="2026-12-02"),
                    dated(y, "example-room", text, due="Friday"), dated(y, "example-room", text, until="2026-11-20")):
        assert not refused["ok"] and set(refused) == {"ok", "error"}, refused
    for past in ("The example form went back Nov 26.", "The example form went back 2026-11-26 and 2026-11-25.", "Sent Dec 2, 2025."):
        r = dated(y, "example-room", past, "example form")
        assert r["ok"] and "day" not in r, past


def test_today_is_the_installs_own_day_never_utcs(tmp_path):
    y = daemon(tmp_path)
    s = replace(y.memory, tz=ZoneInfo("America/New_York"))
    late = datetime(2026, 11, 27, 4, 30, tzinfo=timezone.utc)               # 23:30 on Nov 26 there, UTC already on Nov 27
    assert late.astimezone(s.tz).date() == date(2026, 11, 26) and late.date() == date(2026, 11, 27)
    log = memory.DailyLog(s.daily, s.tz)
    r = memory.remember(s, log, "example-room", "m-1", "The example piano tuner comes Nov 27.", now=late)
    assert r["ok"] and r["day"] == note("2026-11-27")
    r = memory.remember(s, log, "example-room", "m-2", "The example piano was tuned Nov 26.", now=late)
    assert r["ok"] and "day" not in r


def test_the_journal_line_holds_the_room_and_the_count_and_never_a_day_or_a_word_of_the_text(tmp_path, caplog):
    y = daemon(tmp_path)
    soon = memory.local_day(y.memory) + timedelta(days=6)
    later = soon + timedelta(days=2)
    text = f"Zephyrine tuner quartz: {soon.isoformat()} or {MONTHS[later.month - 1]} {later.day}, {later.year}."
    caplog.set_level(logging.INFO, logger="yerk.daemon")
    r = dated(y, "example-room", text)
    assert r["ok"] and r["day"] == note(soon.isoformat(), later.isoformat())
    said = [x.getMessage() for x in caplog.records if x.getMessage().startswith("remember: ")]
    assert said == ["remember: a filing in example-room named 2 coming day(s) with no due"]
    assert not re.search(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", caplog.text) and not any(w in caplog.text for w in ("Zephyrine", "tuner", "quartz"))
    caplog.clear()
    assert "day" not in dated(y, "example-room", "Zephyrine tuner quartz, no day.") and "remember: " not in caplog.text


def test_a_rule_and_its_retiring_never_answer_with_day(tmp_path, monkeypatch):
    y = daemon(tmp_path)
    soon = memory.local_day(y.memory) + timedelta(days=6)
    monkeypatch.setattr(mcp_server, "caller_room", lambda ctx: "example-room")
    server = mcp_server.build(y, y.port, y.hook)

    def call(**args: str) -> dict:
        got = asyncio.run(server.call_tool("remember", args))
        return json.loads((got[0] if isinstance(got, tuple) else got)[0].text)
    rule = call(text=f"Open web pages with the example browser from {soon.isoformat()}, since it draws what a page draws.", job="open web pages")
    assert rule["ok"] and rule["job"] == "open web pages" and "day" not in rule, rule
    gone = call(text=f"the example browser stops on {soon.isoformat()}", retire=rule["cite"])
    assert gone["ok"] and "day" not in gone, gone
    assert call(text=f"The example browser stops on {soon.isoformat()}.")["day"] == note(soon.isoformat())     # the same words, told
