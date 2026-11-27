"""Checks that the figure rule, an evidence check that records a reply's figure with no source behind it and never
blocks the reply, counts only a real quote. A version after a name, a figure the reply marks as approximate and a round
figure with a near number read this turn are not looked up on file; an exact figure quoted with no read behind it still
is. The three cases come from one conversation on 2026-10-18 where each was ledgered against an unrelated line."""

from __future__ import annotations


from yerk_ai import evidence

FILED = lambda fig: "facts/numbers.md:3"  # noqa: E731  every figure is "on file" somewhere


def figures(reply: str, read: str = "") -> list[str]:
    ev = evidence.Evidence()
    if read:
        ev.give(read)
    return [d for rule, d in evidence.check(reply, ev, FILED) if rule == "figure"]


def test_a_version_after_a_name_is_not_a_figure() -> None:
    assert figures("The room runs on Opus 5.5 at max, Claude Code 2.1, on Python 3.12.") == []


def test_a_figure_marked_approximate_is_not_looked_up() -> None:
    assert figures("Old rooms load over 200,000 characters; the test room about 22,000.") == []
    assert figures("Roughly 1,500 lines, ~40 tests.") == []


def test_a_round_figure_is_carried_by_a_near_number_read_this_turn() -> None:
    assert figures("The test room loads 22,000 characters.", read="21,822 chars at session start") == []
    assert len(figures("The test room loads 22,000 characters.", read="a line with 30,000 in it")) == 1


def test_an_exact_figure_with_no_read_behind_it_is_still_ledgered() -> None:
    found = figures("Top six is 71.3% and the boiler holds 1.35 bar.")
    assert len(found) == 2 and all("missing read" in d for d in found)
    assert figures("Top six is 71.3%.", read="top six 71.3% on the 164") == []


def test_a_small_round_number_is_not_loosened() -> None:
    assert len(figures("It costs $500.", read="a figure of 480 somewhere")) == 1
