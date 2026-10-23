"""`yerk map --wire` reads Claude Code's transcript rows (2.1.301): a change in them fails here."""
import json
from datetime import datetime, timezone
from pathlib import Path

from yerk_ai import context, taste

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


def test_a_line_a_crash_zeroed_or_cut_is_skipped_and_counted(tmp_path):
    """Seen live after a hard power-off: one line of a work room's transcript was 193 NUL bytes. Claude Code reads
    past such a line; `yerk map --wire` stopped on it."""
    from yerk_ai.config import budgets
    rows = FIXTURE.read_text().splitlines(keepends=True)
    damaged = tmp_path / "damaged.jsonl"
    damaged.write_text("".join(rows[:2]) + "\0" * 193 + "\n" + "".join(rows[2:]) + '{"type": "user", "mess')
    w = context.wire(damaged)
    assert w["skipped"] == 2 and w["messages"] == context.wire(FIXTURE)["messages"] and context.wire(FIXTURE)["skipped"] == 0
    assert context.render_wire("r", w, 40, budgets()).splitlines()[0].endswith("; damaged lines skipped: 2)")
    assert "skipped" not in context.render_wire("r", context.wire(FIXTURE), 40, budgets())


def test_render_wire_puts_the_parts_against_their_budgets():
    from yerk_ai.config import budgets
    out = context.render_wire("r", context.wire(FIXTURE), 40, budgets())
    assert "system prompt: Claude Code's own" in out and "skills" in out and "median of 1" in out


HOOK = "\n".join(["[clock] Friday 2026-10-23 08:00 UTC, 4 min since the previous human message",
                  "[message] room r, from owner, privacy owner", context.NEWS, "new in rules.md: say it once",
                  "[on file, no lookup needed: the people and things this message names]",    # an older wording of its head
                  "- Example Person: a made-up card (people/example.md)", taste.HEAD, '👍 "good" → "Done, filed."'])
PACK = "[from /x/memory/2026-10-22.md]\n- a filed line of the example room"
RESTORE = ("Called the Read tool with the following input: {\"file_path\": \"/x/inbox/1/message.txt\"}\n     1\t" + "long pasted text " * 60 + "\n"
           "## Bash command sandbox\nexample sandbox rules\nSessionStart hook additional context: " + PACK)


def transcript(tmp_path, hook=HOOK, days=(1, 8)):
    """Example rows (Claude Code 2.1.30x): a first message, a later one rebuilt from its attachments, and one after a
    compaction whose restore block (a file re-read, the sandbox, the recall pack) also holds the hook text, sent once."""
    def at(d):
        return datetime.fromtimestamp(NOW - d * 86400, timezone.utc).isoformat()

    def user(text, **flags):
        return {"type": "user", "message": {"role": "user", "content": text}, **flags}

    def att(kind, **a):
        return {"type": "attachment", "attachment": {"type": kind, **a}}
    reply = '[a reply to your message: "the plan"]\nok go\n[attachment] notes.txt, text/plain, 120 bytes, saved at /x/inbox/2/notes.txt'
    rows = [user("Hi."), {"type": "api-request"},
            user(reply, timestamp=at(days[0])), att("total_tokens_reminder", text="<total_tokens>9 tokens left</total_tokens>"),
            att("hook_additional_context", hookEvent="UserPromptSubmit", content=[hook]), {"type": "api-request"},
            user("This session is being continued from a previous conversation.", isCompactSummary=True), {"type": "api-request"},
            user("[Image: original 10x10, displayed at 10x10]", isMeta=True), {"type": "api-request"},
            att("hook_additional_context", hookEvent="SessionStart", content=[PACK]),
            user("next", timestamp=at(days[1])), att("total_tokens_reminder", text="<total_tokens>8 tokens left</total_tokens>"),
            att("hook_additional_context", hookEvent="UserPromptSubmit", content=[hook]),
            {"type": "api-request-blob", "message": {"role": "system", "content": [
                {"type": "text", "text": RESTORE + "\nUserPromptSubmit hook additional context: " + hook}]}}, {"type": "api-request"}]
    (p := tmp_path / "sid.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return context.wire(p)


NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc).timestamp()


def test_each_message_is_read_by_part_and_its_parts_add_up_to_what_the_map_counts(tmp_path):
    w = transcript(tmp_path)
    assert len(w["messages"]) == 3, "a compaction's summary and Claude Code's image note are nobody's message"
    _, later, restored = w["messages"]
    lines = HOOK.split("\n")
    assert {k: later["parts"][k] for k in ("clock", "metadata", "rules", "recall", "taste")} == {
        "clock": context.chars(lines[0]), "metadata": context.chars(lines[1]), "rules": context.chars("\n".join(lines[2:4])),
        "recall": context.chars("\n".join(lines[4:6])), "taste": context.chars("\n".join(lines[6:]))}
    assert later["parts"][context.TOKENS] == context.chars("<total_tokens>9 tokens left</total_tokens>")
    for m in (later, restored):
        assert sum(m["parts"].values()) == sum(m["around"])
    # the hook text inside Claude Code's restore block is sent once and counted once (the token count, not in it, rebuilt)
    assert sum(restored["around"]) == context.chars(RESTORE + "\nUserPromptSubmit hook additional context: " + HOOK) + \
        context.chars("<total_tokens>8 tokens left</total_tokens>")
    assert restored["parts"]["metadata"] == context.chars(lines[1]) and restored["parts"]["recall pack"] == context.chars(PACK)
    assert restored["parts"]["Claude Code: files re-read after compaction"] == context.chars(RESTORE.split("## Bash")[0])
    assert later["in_text"] == {"reply header": context.chars('[a reply to your message: "the plan"]'),
                                "attachment lines": context.chars(later_attachment := "[attachment] notes.txt, text/plain, "
                                                                  "120 bytes, saved at /x/inbox/2/notes.txt"),
                                context.OWN: later["text"] - context.chars('[a reply to your message: "the plan"]') - context.chars(later_attachment)}


def test_over_reads_against_each_parts_key_and_the_week_names_its_five_largest_by_the_part_that_made_them_large(tmp_path):
    from yerk_ai.config import budgets
    out = context.render_wire("r", transcript(tmp_path), 40, budgets(), "America/Toronto", NOW)
    assert "every part under its key, by part below" in out and "OVER" not in out     # the median passes 1,500, no part its key
    row = next(x for x in out.splitlines() if x.startswith("  recall pack "))
    assert row.split()[-2:] == ["6,000", "ok"]
    week = out.split("Five largest, last 7 days")[1].strip().splitlines()[1:]
    assert len(week) == 1 and week[0].startswith("  Thu 2026-10-22 08:00") and "Claude Code" not in week[0]   # the 8-day-old one is out
    over = HOOK.replace("- Example Person", "- " + "x" * 2100)
    out = context.render_wire("r", transcript(tmp_path, over, (1, 2)), 40, budgets(), "UTC", NOW)
    assert "OVER: recall on 2" in out and next(x for x in out.splitlines() if x.startswith("  recall ")).endswith("OVER on 2")
    assert next(x for x in out.splitlines() if x.startswith("  Yerk AI's hook, its parts together")).endswith("ok")
