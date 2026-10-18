"""`yerk map --wire` reads Claude Code's transcript rows (2.1.301): a change in them fails here."""
from pathlib import Path

from yerk_ai import context

FIXTURE = Path(__file__).parent / "fixtures" / "transcript-wire.jsonl"


def test_wire_reads_the_request_shape_and_each_message():
    w = context.wire(FIXTURE)
    assert w["system"] == 140 and list(w["tools"]) == ["Read", "mcp__yerk__send", "DeferredToolPlaceholder"]
    first, again, folded = w["messages"]
    hook = "UserPromptSubmit hook additional context: [clock] x"
    assert first["text"] == 3 and not first["folded"]
    assert sorted(first["around"]) == sorted([len("<system-reminder>\nwho</system-reminder>"), len("# Environment\nhere"),
                                             len("<total_tokens>10 tokens left</total_tokens>\n\n" + hook)])
    # no blob stored for the second message (identical blocks are stored once): rebuilt from its attachments
    assert again["around"] == [len("<total_tokens>9 tokens left</total_tokens>\n\nUserPromptSubmit hook additional context: [clock] y")]
    # a mid-turn message: only Claude Code's wrapper counts, not the message text inside it
    assert folded["folded"] and folded["around"] == [len("New message: Also this. Answer it.") - len("Also this.")]


def test_render_wire_puts_the_parts_against_their_budgets():
    from yerk_ai.config import budgets
    out = context.render_wire("r", context.wire(FIXTURE), 40, budgets())
    assert "system prompt: Claude Code's own" in out and "skills" in out and "median of 1" in out
