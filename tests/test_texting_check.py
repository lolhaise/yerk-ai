"""Checks the texting check (texting/check.py), the step between drafting a text and sending it, which holds a draft it cannot confirm and
tests/fake_claude.py; nothing here runs a real `claude`, reaches a Mac or posts anywhere."""
from __future__ import annotations

from yerk_ai import words
from yerk_ai.texting import check


def test_the_code_holds_an_assistant_word_markdown_a_long_or_fourth_text_and_a_shut_class_before_any_model():
    for parts, reason in ((["haha my assistant booked it"], words.TEXTING_CHECK_SAYS.format(word="assistant")),
                          (["that was an AUTO-REPLY lol"], words.TEXTING_CHECK_SAYS.format(word="auto reply")),
                          (["**yes**"], words.TEXTING_CHECK_MARK.format(mark="*")), (["see [this](x)"], words.TEXTING_CHECK_MARK.format(mark="](")),
                          (["ok", "- milk\n- eggs"], words.TEXTING_CHECK_MARK.format(mark="- ")),
                          (["a", "b", "c", "d"], words.TEXTING_CHECK_PARTS.format(n=3)), (["x" * 401], words.TEXTING_CHECK_LONG.format(chars=400)),
                          (["she has a girlfriend now"], words.TEXTING_HELD_BY.format(classes="dating")),
                          (["it was like $20", "and the doctor said rest"], words.TEXTING_HELD_BY.format(classes="health, money")),
                          (["owe you 20 bucks"], words.TEXTING_HELD_BY.format(classes="money"))):
        assert check.precheck(parts, ()) == reason, parts
    assert check.precheck(["she has a girlfriend now"], ("dating",)) == ""          # a class the sheet opens is the model's to test
    for fine in (["haha yes", "last week"], ["main road was slow"], ["daily", "airport"], ["x" * 400]):
        assert check.precheck(fine, ()) == "", fine                                 # whole words only: "airport" is not "ai"
