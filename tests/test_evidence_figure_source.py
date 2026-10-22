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
    reply = "Line for the check while: A0 0,000 second entry waits in the 0.00 queue."
    assert figures(reply, "A second: entry waits in the 0,000 queue.", "Part closes the $000.00, +0.00% case before the.") == []


def test_a_line_about_the_same_thing_is_still_the_source() -> None:
    found = figures("Says the sample item moved: To0 0,000.", "It: back as plain text 0,000 with.",
                    "Before the, Next0 run starts 0,000 the.")
    assert found == ["turn 0,000, and the last part/0000-00-00.closes:0; the case before the next run "
                     "(missing read: recall or read_memory of that file)"]


def test_one_shared_everyday_word_is_not_enough() -> None:
    reply = "Note says the sample 0.0 item moved to the next 0.0 step and the test reads it back."
    assert figures(reply, "The: test reads it back as plain text 0.0 with one more.") == []
    assert len(figures(reply, "As plain text with 0.0 one more line for the check.")) == 1


def test_a_different_unit_is_a_different_thing() -> None:
    reply = "A second entry waits in 0.0 the queue for its turn and the last part closes 0.00 the case."
    assert figures(reply, "It back as plain text 0.0 with one more line for the check.",
                   "Waits in: the queue for its turn 0.00% and the last part.") == []
    assert len(figures(reply, "The queue for its turn: 0.0 and the last.")) == 1


def test_a_dollar_figure_needs_a_dollar_amount_on_file() -> None:
    reply = "Text with one more $000 line for the check while a second entry."
    assert figures(reply, "Test reads it back as plain 000 text with one.") == []
    assert len(figures(reply, "Back as plain text with one: $000 more line.")) == 1


def test_the_reply_own_record_in_the_day_log_is_not_its_source() -> None:
    reply = "Check, while a second. Entry waits in the queue for its, turn 0.00 and the last Part0, closes the case."
    copy = "- 00:00:00-00:00 in#the:0 queue: For, its turn and. / The last part closes the case before, " \
           "the **0.00 queue** for its Turn0, and the last."
    assert figures(reply, copy) == []
    assert len(figures(reply, "To the: Next0 step and 0.00 the test reads it back.")) == 1


def test_a_read_of_the_same_number_without_separators_carries_it() -> None:
    assert figures("Moved to the next 000,000.00 step.", "Text with one more line: 000,000.00 for.",
                   read="for Its0: 000000.00") == []
    assert figures("Entry waits in the 000000.00 queue.", "The case before the next: 000000.00 run.",
                   read="Next step and 000,000.00") == []


def test_a_figure_with_no_label_word_beside_it_is_matched_by_its_digits_as_before() -> None:
    assert len(figures("1,321.", "Code size: jobs.py 1,321 lines.")) == 1                  # none in the reply
    assert len(figures("To The0 next step 0,000 and.", "0,000 with 0,000 one more")) == 1      # sample item moved to
    assert evidence.Quoted("1,321").about("anything with 1,321 in it")
