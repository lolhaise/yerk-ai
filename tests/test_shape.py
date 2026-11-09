"""Checks the rule-shape check (shape.py), which scores the rule files a room loads on five counts: a "do not" with no reason, a shouted word, an
example outside example tags, an alarm mark, one topic in two files. Each count on a fixture file of its own (tests/fixtures/shape/), each exemption
its rule names, the thresholds in budgets.toml [shape], the doctor's findings and each room's report of its class, a rule filed in the last day named
and the shipped mechanics and example layer clean, so a stranger's install starts in the shape."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from test_doctor import tree  # noqa: F401  (the doctor's healthy install: the example layer, a day log, a fresh index)
from yerk_ai import context, doctor, memory, morning, shape, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE, REPO, budgets, rooms
from yerk_ai.ledger import Ledger

F = Path(__file__).parent / "fixtures" / "shape"
DAY = 86400.0


def lines_of(path, **kw):
    r = shape.score(path, **kw)
    return {k: [n for n, _ in r[k]] for k in shape.COUNTS}


def test_a_negative_with_no_why_in_its_line_counts_and_code_quotes_examples_and_say_so_do_not():
    assert lines_of(F / "no-why.md")["no why"] == [3, 5, 9]          # never; "say so" is no why; an n't word


def test_a_shouted_word_counts_and_an_acronym_a_file_name_code_or_a_listed_name_does_not():
    assert lines_of(F / "loud.md")["loud"] == [3, 6, 8]
    assert lines_of(F / "loud.md", acronyms=shape.ACRONYMS | {"ZORG"})["loud"] == [3, 6]


def test_an_example_outside_example_tags_counts_and_a_tag_named_in_code_opens_nothing():
    assert lines_of(F / "untagged.md")["untagged"] == [3, 4, 5, 6, 13]      # a lead, a quote and landed, a thumbs emoji


def test_an_alarm_mark_on_an_instruction_counts_and_the_checklist_mark_does_not():
    assert lines_of(F / "alarm.md")["alarm"] == [3, 4]


def test_a_topic_in_two_files_one_room_loads_counts_once_and_yerkd_toml_adds_topics(tmp_path):
    room = {"layer": str(F / "twice"), "persona": "persona.md", "rulebook": "rulebook.md"}
    m = memory.settings(EXAMPLE_PRIVATE, tmp_path)
    r = shape.check(EXAMPLE_PRIVATE, m, {"r": room}, {}, time.time())
    files = [f"{(F / 'twice').relative_to(REPO).as_posix()}/{n}" for n in ("persona.md", "rulebook.md")]
    assert r["twice"] == {"tables": files} and shape.totals(r)["twice"] == 1
    r = shape.check(EXAMPLE_PRIVATE, m, {"r": room}, {"topics": {"figures": r"\bfigures?\b"}}, time.time())
    assert sorted(r["twice"]) == ["figures", "tables"]


def test_every_count_past_its_threshold_is_a_finding_and_a_count_under_it_is_only_in_the_report(tmp_path):
    room = {"layer": str(F), "persona": "no-why.md", "rulebook": "loud.md", "voice": "untagged.md"}
    r = shape.check(EXAMPLE_PRIVATE, memory.settings(EXAMPLE_PRIVATE, tmp_path), {"r": room}, {}, time.time())
    found = shape.findings(r, budgets()["shape"])
    assert "rule shape tests/fixtures/shape/loud.md: 1 no why, 3 loud, first line 3" in found
    assert any(x.startswith("rule shape tests/fixtures/shape/untagged.md: 5 untagged") for x in found)
    roomy = {k: 9 for k in shape.KEYS.values()}
    assert shape.findings(r, roomy) == [] and "- loud, line 6: - Write it in plain text, IMPORTANT for phones." in shape.report("r", r, roomy, "now")
    assert budgets()["shape"] == {"negatives_without_why": 0, "shouted": 0, "untagged_examples": 0, "alarm_marks": 0,
                                  "duplicated_topics": 0, "lesson_cases": 3, "lesson_summary_chars": 140, "lesson_body_chars": 600,
                                  "lesson_quote_words": 8, "lesson_gloss_words": 6, "lesson_sentence_words": 40, "lesson_findings": 0}


def test_the_mechanics_and_the_example_layer_are_in_the_shape(tmp_path):
    """What every room reads and what a stranger installs: no count, no topic said twice, under the mechanics' cap."""
    r = shape.check(EXAMPLE_PRIVATE, memory.settings(EXAMPLE_PRIVATE, tmp_path), rooms(EXAMPLE_PRIVATE), {}, time.time())
    assert shape.findings(r, budgets()["shape"]) == [] and shape.totals(r)["lines"] >= 30
    text = context.MECHANICS.read_text().strip()
    assert context.chars(text) < budgets()["session_start"]["appended"]["mechanics"]
    assert "cited in `<example>` tags, `Landed, daily/YYYY-MM-DD.md:N`" in text and "never announced" in text and "`docs/claude/` and `docs/shape/`" in text


def test_the_rule_shape_counts_are_one_weekly_line_said_on_sunday_or_the_day_one_rose_and_each_rooms_report_is_written(tree, capsys):  # noqa: F811
    """quiet-doctor (2026-10-24): the counts were six lines every day, read by no one. Now one line, on the Sunday run or
    the run a count rose since the last; the daily run and the morning diff are quiet about them otherwise."""
    priv, state, *_ = tree
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == f"{words.DONE} doctor: clean\n"
    accounts, cool, led = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"), Ledger(state / "ledger.sqlite")
    lines, _ = morning.build(priv, state, accounts, cool, led, {"started": time.time() - DAY, "units": []}, time.time())
    assert json.loads((state / "doctor.json").read_text())["shape"]["lines"] > 0 and not [x for x in lines if "rule shape" in x]
    (priv / "rulebook.md").write_text((priv / "rulebook.md").read_text() + "- NEVER guess a date.\n")
    week = "; 1 past budgets.toml [shape], `yerk docs shape` names each line"
    assert doctor.main(priv, state) == 1                                   # a count rose: said today, whatever the day
    assert capsys.readouterr().out == f"{words.WORKING} rule shape: 1 no why (+1), 1 loud (+1){week}\n"
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": time.time() - DAY, "units": []}, time.time())
    assert words.line(f"{words.WORKING} rule shape: 1 no why (+1), 1 loud (+1){week}") in lines and not [k for k in stamp["findings"] if k.startswith("doctor")]
    t = time.localtime()            # by the calendar, not k * DAY: a clock change makes a day 25 hours (Nov 1 2026, "Monday" at 00:xx was Sunday 23:xx)
    ahead = (doctor.SUNDAY - t.tm_wday) % 7 or 7
    sunday, monday = (time.mktime((t.tm_year, t.tm_mon, t.tm_mday + d, t.tm_hour, t.tm_min, t.tm_sec, 0, 0, -1)) for d in (ahead, ahead + 1))
    assert doctor.run(priv, state, now=sunday) == [f"{words.WORKING} rule shape: 1 no why, 1 loud{week}"]     # the weekly run
    assert doctor.run(priv, state, now=monday) == []                      # Monday, no count up: nothing
    report = state / "docs" / "shape" / "example-room.md"
    assert memory.file_class(report) == ("room", "discord:CHANNEL_ID") and "- loud, line 12: - NEVER guess a date." in report.read_text()
    m = memory.settings(priv, state)
    mine, other = memory.Reader("example-room", "discord:CHANNEL_ID"), memory.Reader("x", "another")
    assert memory.read(m, "docs/shape/example-room.md", reader=mine)["ok"] and not memory.read(m, "docs/shape/example-room.md", reader=other)["ok"]


def test_a_rule_line_of_the_last_day_that_misses_the_shape_is_named_by_its_cite(tree, capsys):  # noqa: F811
    """A filing out of the shape is turned back with nothing written (memory.unshaped); a line a person writes into the
    rules file by hand is read by no filing, so the doctor names it by its cite the day it lands."""
    priv, state, *_ = tree
    m = memory.settings(priv, state)
    assert not memory.rule(m, "example-room", "m1", "dates", "never guess a date")["ok"] and not (m.root / memory.RULES).exists()
    at = datetime.now(timezone.utc).astimezone(m.tz)
    (m.root / memory.RULES).write_text(f"# Rules\n\n- dates: never guess a date (example-room, {at:%Y-%m-%d %H:%M:%S%:z}) [m:m1]\n")
    filed = {"cite": f"{memory.cite(m, m.root / memory.RULES)}:3"}
    assert doctor.main(priv, state) == 1
    out = capsys.readouterr().out.splitlines()
    assert f"{words.WORKING} rule shape {filed['cite']}: filed in the last day with a negative with no why; file it again in the shape" in out
    assert not [x for x in doctor.run(priv, state, dry=True, now=time.time() + 2 * DAY) if x[2:].startswith(f"rule shape {filed['cite']}")]
    memory.rule(m, "example-room", "m2", "dates", "take a date from a file, since a guessed date reads as fact")
    assert doctor.main(priv, state) == 0                                   # the old line is superseded, so never loaded


def test_two_rooms_files_of_one_name_in_two_folders_are_each_scored_and_named_apart(tmp_path):
    for folder, text in (("a", "- Keep it short, so it reads fast.\n"), ("b", "- Never guess.\n")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "persona.md").write_text(f"# Persona\n\n{text}")
    rs = {n: {"layer": str(tmp_path / n), "persona": "persona.md"} for n in ("a", "b")}
    r = shape.check(EXAMPLE_PRIVATE, memory.settings(EXAMPLE_PRIVATE, tmp_path / "state"), rs, {}, time.time())
    assert [f["label"] for f in r["files"].values()] == ["mechanics.md", "persona.md", "b/persona.md"]
    assert shape.findings(r, budgets()["shape"]) == ["rule shape b/persona.md: 1 no why, first line 3"]
