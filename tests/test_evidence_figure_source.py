"""Checks that the line taken as the source on file for a figure in a reply is a line about the same thing, not any line that shares
its digits. The shapes come from the evidence replay of 2026-10-21 (13 of 74 accepted replies flagged, every wrong-fact hit on an
text here is made up. The lookup is memory.on_file's: a line holding the figure counts when `about` says so."""

from __future__ import annotations

from pathlib import Path

from yerk_ai import evidence

ROOTS = (Path("memory"),)
KNOWN = lambda name: None  # noqa: E731


def figures(reply: str, *lines: str, read: str = "", said: str = "") -> list[str]:
    """The figure rows the Stop check writes when `lines` are the memory tree, looked up as memory.on_file does."""
    def filed(fig: str) -> str | None:
        about = fig.about if isinstance(fig, evidence.Quoted) else None
        for n, line in enumerate(lines, 1):
            if evidence.has_figure(line, fig) and (about is None or about(line)):
                return f"daily/2026-01-01.md:{n}"
        return None
    ev = evidence.Evidence()
    if read:
        ev.give(read)
    return [d for rule, d in evidence.check(reply, ["a question"], ev, ROOTS, filed, KNOWN, said) if rule == "figure"]


def test_a_line_that_only_shares_the_digits_is_not_the_source() -> None:
    reply = "Sq ft paint by shed: Z8 6,071 and the garden fence rail 2.35 ft."
    assert figures(reply, "Best seller: a desk lamp with 6,071 reviews.", "The fund closed at $64.20, +2.35% on the day.") == []


def test_a_line_about_the_same_thing_is_still_the_source() -> None:
    found = figures("Sq ft paint by shed: Z8 6,071.", "Unrelated: a desk lamp with 6,071 reviews.",
                    "Paint count, Z8 came to 6,071 sf.")
    assert found == ["quoted 6,071, on file at daily/2026-01-01.md:2; no read this turn carries it "
                     "(missing read: recall or read_memory of that file)"]


def test_one_shared_everyday_word_is_not_enough() -> None:
    reply = "The fence line runs 1.48 ft up the post and 0.73 ft over the top of the gate."
    assert figures(reply, "Status: the gateway sat near the top at 0.73 load all day.") == []
    assert len(figures(reply, "The fence post takes 1.48 ft of paint up the side.")) == 1


def test_a_different_unit_is_a_different_thing() -> None:
    reply = "The shed door frame is 3.62 ft on the north wall and the trim edge is 0.27 ft wider."
    assert figures(reply, "The north wall beat started 3.62 s later on the shed door probe.",
                   "Fee table: the trim edge fund runs 0.27% wider than the index.") == []
    assert len(figures(reply, "North wall shed door frame: 3.62 ft as drawn.")) == 1


def test_a_dollar_figure_needs_a_dollar_amount_on_file() -> None:
    reply = "Cap the credits at $250 for the pilot run and stop it there."
    assert figures(reply, "Licensing for pilot run sites with 250 or more seats.") == []
    assert len(figures(reply, "Credits cap for the pilot run: $250 a month.")) == 1


def test_the_reply_own_record_in_the_day_log_is_not_its_source() -> None:
    reply = "Yes, both still hold. The shelf counts match the photos, just 0.09 sf apart on Z6, so nothing moves."
    copy = "- 10:00:00-04:00 room#discord:1 yerk: Yes, both still hold. / The shelf counts match the photos, " \
           "just **0.09 sf** apart on Z6, so nothing moves."
    assert figures(reply, copy) == []
    assert len(figures(reply, "Photo check: Z6 shelf counts 0.09 sf apart against the list.")) == 1


def test_a_read_of_the_same_number_without_separators_carries_it() -> None:
    assert figures("The floor area is 293,477.15 sf.", "Floor area on the sheet: 293,477.15 sf.",
                   read="cell D4: 293477.15") == []
    assert figures("The floor area is 293477.15 sf.", "Floor area on the sheet: 293477.15 sf.",
                   read="Total floor area 293,477.15") == []


def test_a_figure_with_no_label_word_beside_it_is_matched_by_its_digits_as_before() -> None:
    assert len(figures("1,321.", "Code size: jobs.py 1,321 lines.")) == 1                  # none in the reply
    assert len(figures("Shed Z8 came to 1,321 sf.", "1,321 and 5,318 here too")) == 1      # none on the line
    assert evidence.Quoted("1,321").about("anything with 1,321 in it")
