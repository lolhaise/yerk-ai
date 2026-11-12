"""Checks the texting check (texting/check.py), the step between drafting a text and sending it, which holds a draft it cannot confirm and
tests/fake_claude.py; nothing here runs a real `claude`, reaches a Mac or posts anywhere."""
from __future__ import annotations

import json
from typing import Any

from yerk_ai import words
from yerk_ai.texting import check
from yerk_ai.texting.people import Sheet

HIS, SAID = {"g-own"}, {1: "he moved"}
KNOWN = check.keys(Sheet(open=("dating",), knows=(("He moved last week", "his order"),)))


def verdict(**ans: Any) -> str:
    return json.dumps({"type": "result", "is_error": False, "duration_ms": 900, "usage": {"input_tokens": 700, "output_tokens": 40},
                       "structured_output": {"release": True, "failed": [], "unlisted": [], "why": "all sourced",
                                             "statements": [{"n": 1, "ok": True, "basis": "thread:g-own"}], **ans}})


def test_the_model_release_stands_only_over_a_basis_the_code_confirms_on_the_owners_side():
    for basis in ("thread:g-own", "topic:g-own", "sheet:dating", "sheet:  he moved LAST week"):
        v = check.read(verdict(statements=[{"n": 1, "ok": True, "basis": basis}]), SAID, HIS, KNOWN)
        assert (v["release"], v["reason"], v["tokens"]) == (True, "", 740), basis
    for basis in ("thread:g-1", "topic:g-1", "memory:notes/day.md:12", "sheet:money", "sheet:", "none", "thread:g-gone"):
        v = check.read(verdict(statements=[{"n": 1, "ok": True, "basis": basis}]), SAID, HIS, KNOWN)   # a them row, a cite, no key
        assert (v["release"], v["reason"]) == (False, words.TEXTING_CHECK_NO_BASIS.format(what="he moved") + "; all sourced"), basis
    for ans in ({"statements": []}, {"statements": [{"n": 1, "ok": False, "basis": "thread:g-own"}]},
                {"statements": [{"n": 1, "ok": True, "basis": "thread:g-own"}, {"n": 2, "ok": False, "basis": "none"}]}):
        assert check.read(verdict(**ans), SAID, HIS, KNOWN)["release"] is False, ans      # left out, not ok, or one more not ok


def test_an_unlisted_statement_and_a_failed_test_hold_even_when_the_model_says_release():
    v = check.read(verdict(unlisted=["he is moving again"]), SAID, HIS, KNOWN)
    assert (v["release"], v["reason"]) == (False, words.TEXTING_CHECK_UNSAID.format(what="he is moving again") + "; all sourced")
    v = check.read(verdict(release=False, failed=["b"], why="see you at 7 is a time commitment"), SAID, HIS, KNOWN)   # a plan
    assert (v["release"], v["failed"]) == (False, ["b"])
    assert v["reason"] == words.TEXTING_CHECK_FAILED.format(tests="b") + "; see you at 7 is a time commitment"
    assert check.read(verdict(failed=["b"]), SAID, HIS, KNOWN)["release"] is False
    assert check.read(verdict(release=False, why=""), SAID, HIS, KNOWN)["reason"] == words.TEXTING_CHECK_HELD


def test_an_error_no_json_and_a_missing_or_wrong_field_are_holds_never_a_release():
    outs = ["not json", json.dumps({"type": "result", "result": "plain words"}), verdict(release="yes"), verdict(failed="none"),
            verdict(statements=[{"n": "one", "ok": True, "basis": "thread:g-own"}]), verdict(statements=[{"n": 1, "ok": True}])]
    outs += [json.dumps({"type": "result", "structured_output": {k: v for k, v in json.loads(verdict())["structured_output"].items() if k != k2}})
             for k2 in ("release", "failed", "statements", "unlisted", "why")]
    for out in outs:
        v = check.read(out, SAID, HIS, KNOWN)
        assert (v["release"], v["reason"]) == (False, words.TEXTING_NO_CLEAN), out
    v = check.read(json.dumps({"type": "result", "is_error": True, "result": "You've hit your limit"}), SAID, HIS, KNOWN)
    assert (v["release"], v["error"]) == (False, "You've hit your limit")


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


def test_the_four_inputs_are_fenced_with_one_tag_and_a_claim_with_no_basis_is_marked_and_a_cite_never_shown():
    rows = [{"guid": "g-own", "is_from_me": True, "text": "got the new place", "created_at": "2026-11-12T14:00:00Z"},
            {"guid": "g-1", "is_from_me": False, "text": "wait you moved??", "created_at": "2026-11-12T14:05:00Z"}]
    said = [{"n": 1, "text": "he moved", "source": "thread:g-own"}, {"n": 2, "text": "he likes it", "source": "thread:g-1"},
            {"n": 3, "text": "it has a yard", "source": "memory:notes/house.md:4"}]
    row = {"decision": "reply", "parts": json.dumps(["haha yes", "last week"]), "reply_to": "you moved", "inbound": "g-1",
           "statements": json.dumps(said)}
    got = check.inputs(row, rows, Sheet(open=("dating",), card="short, lower case"), "t4g")
    assert got.startswith(words.TEXTING_CHECK_PACK.format(tag="t4g"))
    assert [x for x in got.splitlines() if x.startswith("=== ")] == [f"=== {e}{n} t4g ===" for n in ("thread", "sheet", "their texts", "draft")
                                                                       for e in ("", "end ")]
    assert "g-own" in got and " owner: got the new place" in got and " them: wait you moved??" in got and "short, lower case" in got
    assert "text 1: haha yes\ntext 2: last week\nanswers: you moved" in got
    assert "1. he moved (source: thread:g-own)\n" in got
    assert f"2. he likes it (source: thread:g-1; {words.TEXTING_CHECK_CLAIMED})" in got
    assert f"3. it has a yard (source: memory; {words.TEXTING_CHECK_CLAIMED})" in got and "house.md" not in got
    tap = check.inputs({"decision": "react", "react": "love", "inbound": "g-1"}, rows, Sheet(), "t4g")
    assert "tapback: love\nanswers: their newest text\nstatements about the owner: none" in tap
