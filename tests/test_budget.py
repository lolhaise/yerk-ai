"""The budget test: every example room's context fits budgets.toml, and a planted file fails."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yerk_ai import context
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms
from yerk_ai.lifted.clock import clock_line
from yerk_ai.lifted.trim import truncate

PLANTED = Path(__file__).parent / "fixtures" / "over-budget"
NOW = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)


def build(private, name):
    b = budgets()
    room = rooms(private)[name]
    start = context.session_start(private, room, b)
    msg = context.per_message(name, room, "owner", NOW, NOW - timedelta(minutes=5), b)
    return context.violations(start, msg, b), start


@pytest.mark.parametrize("name", sorted(rooms(EXAMPLE_PRIVATE)))
def test_example_rooms_fit_their_budgets(name):
    bad, _ = build(EXAMPLE_PRIVATE, name)
    assert bad == []


def test_planted_over_budget_file_fails_and_is_sent_trimmed():
    bad, start = build(PLANTED, "planted-room")
    assert any(v.startswith("persona:") for v in bad)
    persona = next(p for p in start if p.name == "persona")
    assert persona.over and persona.size <= persona.budget
    assert "[...trimmed persona: kept" in persona.text and "Omitted sections:" in persona.text


@pytest.mark.skipif(not os.environ.get("YERK_PRIVATE"), reason="the real private layer exists only on the box")
def test_real_private_layer_fits():
    private = Path(os.environ["YERK_PRIVATE"]).expanduser()
    for name in rooms(private):
        assert build(private, name)[0] == [], name


def test_declared_parts_fit_the_declared_total():
    s = budgets()["session_start"]
    assert sum(s["claude_code"].values()) + sum(s["appended"].values()) <= s["total"]
    pm = budgets()["per_message"]
    assert sum(pm["parts"].values()) <= pm["typical"] <= pm["cap"]
    assert sum(pm["parts"].values()) + sum(pm["on_hit"].values()) <= pm["cap"]     # a message with every hit


def test_characters_are_counted_as_claude_code_counts_them():
    assert context.chars("ab") == 2 and context.chars("a\U0001F600") == 3


def test_trim_leaves_short_text_alone():
    assert truncate("short", "x", 100) == ("short", 0)


def test_clock_line_has_weekday_zone_and_gap():
    line = clock_line(NOW, NOW - timedelta(hours=2, minutes=5))
    assert line == "[clock] Wednesday 2026-01-07 12:00 UTC, 2h 5m since the previous human message"
