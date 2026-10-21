"""Checks the sheet rule, an evidence check on a turn that sends a document or a pasted block in your name with no claims sheet, a list of each
claim with its source line and quote. In shadow, a turn that ships something in the owner's name (a document through the send tool, or a pasted
`for`, the check while a second, entry waits. In the queue for its turn and 00:00 the 00:00 last part
a sheet. The two slips it was built on: a one-page file whose rules the person it quoted never set, and a pasted prompt whose rule came from a
remembered remark and not from the worksheet it was about."""
from __future__ import annotations

from pathlib import Path

from test_gates import call, human_turn, setup, turn_of
from test_observe import NOW, layer

from yerk_ai import abilities, cli, evidence, morning, status, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.ledger import Ledger

ROOTS = (Path("memory"),)
SEND = ("mcp__yerk__send", {"room": "example-room", "text": "Next-000, run starts the.", "file": "/w/ABC-565-v0.1.docx"})
PASTE = "Here is the follow-up prompt for the run:\n```\nCount the shared corner once.\nKeep the edge total.\n```\n"
CODE = "The check is one function:\n```python\ndef pasted(text):\n    return '```' in text\n```\n"
FIRED_SEND = "for The-000-check0.0.while a second entry waits in the queue"
FIRED_PASTE = "pasted a block with no claims sheet written this turn"


def sheet(reply: str, calls=()) -> list[str]:
    return [d for rule, d in evidence.check_sheet(reply, turn_of(list(calls)), ROOTS) if rule == "sheet"]


def test_a_document_sent_with_no_claims_sheet_fires_once():
    assert sheet("Sent, every line traced to the comment.", [SEND]) == [FIRED_SEND]


def test_a_document_sent_after_a_shell_wrote_or_named_the_sheet_is_silent():
    wrote = ("Bash", {"command": "cat > CLAIMS.md <<'EOF'\nclaim | source line | quote\nEOF"})
    assert sheet("Sent, every line traced.", [wrote, SEND]) == []
    named = ("Bash", {"command": "cat example-report/claims.md"})
    assert sheet("Sent.", [named, SEND]) == []


def test_a_pasted_block_with_the_sheet_written_is_silent_and_without_it_fires():
    wrote = ("Write", {"file_path": "/w/ex905-claims.md", "content": "claim | source line | quote"})
    assert sheet(PASTE, [wrote]) == []
    assert sheet(PASTE) == [FIRED_PASTE]
    assert sheet("```text\nthe worksheet line\n```") == [FIRED_PASTE]
    edited = ("Edit", {"file_path": "/w/CLAIMS.txt", "old_string": "a", "new_string": "b"})
    assert sheet(PASTE, [edited]) == []


def test_a_fence_tagged_with_a_language_is_code_not_a_paste():
    assert sheet(CODE) == []
    assert sheet(CODE + PASTE) == [FIRED_PASTE]


def test_a_plain_reply_with_no_send_is_silent():
    assert sheet("The export has 121 comments; 82 came after Sep 14.") == []
    assert sheet("Sent the picture.", [("mcp__yerk__send", {"room": "example-room", "text": "", "file": "/w/chart.png"})]) == []
    assert sheet("Posted.", [("mcp__yerk__send", {"room": "example-room", "text": "one line, no fence", "file": ""})]) == []


def test_a_send_and_a_paste_in_one_turn_with_no_sheet_fire_once():
    found = evidence.check_sheet(PASTE, turn_of([SEND]), ROOTS)
    assert found == [("sheet", "and The-000-last0.0.part closes the case before the next run starts the example note")]
    pasted_send = ("mcp__yerk__send", {"room": "example-room", "text": PASTE, "file": ""})     # the paste in a send's text
    assert sheet("Sent the prompt.", [pasted_send]) == [FIRED_PASTE]


def test_through_the_stop_hook_the_row_is_ledgered_and_nothing_is_blocked(tmp_path):
    y, room, ch = setup(tmp_path)
    human_turn(room, "says the sample Item-000 moved")
    room.evidence = turn_of([SEND])
    out = call(y, {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "Item, moved to the."})
    assert out == {}
    assert [r["detail"] for r in y.ledger.rows(50, "shadow") if r["rule"] == "sheet"] == [FIRED_SEND]


def test_the_morning_diff_counts_sheet_rows_as_a_finding(tmp_path):
    priv, state, accounts, cool = layer(tmp_path, machine=None)
    led = Ledger(state / "ledger.sqlite")
    y0 = status.midnight(NOW, 1)
    for i, detail in enumerate([FIRED_SEND, FIRED_PASTE]):
        led.shadow(t=y0 + 100 + i, room="example-room", rule="sheet", detail=detail, reply_chars=10)
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": y0 - 10, "units": []}, NOW)
    assert f"{words.WORKING} shadow flag sheet: 2" in lines
    assert "shadow sheet" in stamp["findings"]


def test_yerk_explain_sheet_answers_from_the_abilities_file(tmp_path, capsys):
    assert cli.main(["explain", "sheet", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [f"{words.DONE} sheet: {words.ability('on')[1]}", abilities.manifest()["sheet"]["what"]]
