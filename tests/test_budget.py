"""The budget test: every example room's context fits budgets.toml, and a planted file fails."""
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yerk_ai import context, memory
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms, state_dir
from yerk_ai.lifted.clock import clock_line
from yerk_ai.lifted.trim import truncate

PLANTED = Path(__file__).parent / "fixtures" / "over-budget"
NOW = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)


def build(private, name, state):
    """A room's parts as the daemon and the morning diff build them: the install's rules file included."""
    b = budgets()
    room = rooms(private)[name]
    start = context.session_start(private, room, b, rules=memory.settings(private, state).root / memory.RULES)
    msg = context.per_message(name, room, "owner", NOW, NOW - timedelta(minutes=5), b)
    return context.violations(start, msg, b), start


@pytest.mark.parametrize("name", sorted(rooms(EXAMPLE_PRIVATE)))
def test_example_rooms_fit_their_budgets(name, tmp_path):
    bad, _ = build(EXAMPLE_PRIVATE, name, tmp_path)
    assert bad == []


def test_planted_over_budget_file_fails_and_is_sent_trimmed(tmp_path):
    bad, start = build(PLANTED, "planted-room", tmp_path)
    assert any(v.startswith("persona:") for v in bad)
    persona = next(p for p in start if p.name == "persona")
    assert persona.over and persona.size <= persona.budget
    assert "[...trimmed persona: kept" in persona.text and "Omitted sections:" in persona.text


def test_the_code_stays_at_its_ceiling_which_only_goes_down():
    ceiling = budgets()["code"]["lines"]
    lines = sum(f.read_bytes().count(b"\n") for f in Path(context.__file__).parent.rglob("*.py"))
    assert lines <= ceiling, f"yerk_ai/ is {lines} lines, over the ceiling of {ceiling} in budgets.toml: cut as much as you add"
    assert lines == ceiling, f"yerk_ai/ shrank to {lines} lines: set [code] lines in budgets.toml to {lines} in this change"


@pytest.mark.skipif(not os.environ.get("YERK_PRIVATE"), reason="the real private layer exists only on the box")
def test_real_private_layer_fits():
    private, state = Path(os.environ["YERK_PRIVATE"]).expanduser(), state_dir()
    for name in rooms(private):
        bad = build(private, name, state)[0]
        assert bad == [], f"{name} {'; '.join(bad)}"      # the line `yerk deploy` puts on its card: the room, then its part


def test_the_real_layer_test_counts_the_rules_file(tmp_path, monkeypatch):
    """A room over budget only through the install's rules file fails the real-layer test, named with its part. Built
    without the rules file, as it was, the test passed such a room."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "memory" / memory.RULES).write_text("- a job: done this way.\n" + "- another job: " + "x" * 7000 + "\n")
    monkeypatch.setenv("YERK_PRIVATE", str(private))
    monkeypatch.setenv("YERK_STATE", str(tmp_path / "state"))
    with pytest.raises(AssertionError, match=rf"^example-room rules: 7,\d{{3}} > {budgets()['session_start']['appended']['rules']:,} \("):
        test_real_private_layer_fits()


def test_the_rules_file_is_a_part_of_its_own_so_a_full_rulebook_never_pushes_a_rule_out(tmp_path):
    """On 2026-10-20 two rooms' rulebooks with the rules file inside came to 12,290 and 12,326 against 12,000, and the trim
    cut five shared rules from the middle. The rules file is its own part now, after the hand-kept files (the rooms write
    it as they go); past its own budget its trim marker names it; a sealed room gets none."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "rulebook.md").write_text("- a rule of this room.\n" * 500)                        # 11,500: near its cap
    rules = private / "memory" / memory.RULES
    rules.write_text("".join(f"- job {i}: how job {i} is done in every room.\n" for i in range(120)))      # 5,300
    bad, start = build(private, "example-room", tmp_path / "state")
    part = {p.name: p for p in start}
    assert list(part) == ["mechanics", "persona", "rulebook", "room_voice", "pinned_memory", "rules", "recall_pack"]
    assert bad == [] and not part["rulebook"].over and part["rules"].raw > 5000
    assert all(f"- job {i}: " in part["rules"].text for i in range(120)) and "[...trimmed" not in context.appended_file(start)
    rules.write_text(rules.read_text() * 2)                                                     # past the part's own budget
    bad, start = build(private, "example-room", tmp_path / "state")
    part = {p.name: p for p in start}
    assert [v.split(":")[0] for v in bad] == ["rules"] and "[...trimmed rules: kept" in part["rules"].text
    assert part["rulebook"].text.endswith("- a rule of this room.") and not part["rulebook"].over
    sealed = context.session_start(private, {**rooms(private)["example-room"], "sealed": True}, budgets(), rules=rules)
    assert next(p for p in sealed if p.name == "rules").raw == 0


# Claude Code's parts as the widest live room sends them, the largest of each across the six rooms' newest transcripts
# (2026-10-20, Claude Code 2.1.301): the prompt of a session started on Fable 5.1 (4,817 on Opus 5.5), each tool entry.
LIVE_PROMPT = 10889
LIVE_TOOLS = {"Bash": 4248, "Glob": 785, "Grep": 3362, "Read": 2006, "mcp__web-ladder__web_fetch": 878,
              "mcp__web-ladder__web_search": 621, "mcp__yerk__hold": 481, "mcp__yerk__read_memory": 551, "mcp__yerk__recall": 656,
              "mcp__yerk__remember": 1220, "mcp__yerk__schedule": 1735, "mcp__yerk__send": 520, "mcp__yerk__settings": 878,
              "DeferredToolPlaceholder": 214, "mcp__yerk__worker": 1292}


def test_claude_codes_parts_as_the_rooms_send_them_fit_their_lines_and_what_is_left_goes_to_the_rules(tmp_path):
    """`yerk map --wire` on the live rooms read the other tool entries, Claude Code's deferred-loading placeholder (sent
    in every room, only when deferred tool loading is on) and web-ladder's two tools, at 1,713 against 1,500, and the
    prompt line sat 511 over the largest prompt measured. Each Claude Code part of the widest room fits its line, the
    lines set from the measurement keep at most 2% over it, and the session-start lines add to exactly the total
    (decision 12), so what they freed went to `rules`."""
    def entry(name, size):
        t = {"name": name, "description": ""}
        return {**t, "description": "x" * (size - context.chars(json.dumps(t)))}
    shape = {"system": [{"type": "text", "text": "p" * LIVE_PROMPT}, {"type": "text", "text": "# Yerk AI mechanics"}],
             "tools": [entry(n, size) for n, size in LIVE_TOOLS.items()]}
    (t := tmp_path / "sid.jsonl").write_text(json.dumps({"type": "api-request-shape", "shape": shape}) + "\n")
    b = budgets()
    out = context.render_wire("r", context.wire(t), context.chars("# Yerk AI mechanics"), b)
    rows = {m[1]: (int(m[2].replace(",", "")), int(m[3].replace(",", ""))) for m in re.finditer(
        r"^  (system prompt: Claude Code's own|built-in tools|Yerk AI's tools|other tool entries)(?: \(.*?\))?\s+([\d,]+)\s+([\d,]+)", out, re.M)}
    assert rows == {"system prompt: Claude Code's own": (LIVE_PROMPT, b["session_start"]["claude_code"]["claude_code_prompt"]),
                    "built-in tools": (10401, b["session_start"]["claude_code"]["builtin_tools"]),
                    "Yerk AI's tools": (7333, b["session_start"]["claude_code"]["yerk_tools"]),
                    "other tool entries": (1713, b["session_start"]["claude_code"]["deferred_tool_names"])}
    builtin, other = (next(x for x in out.splitlines() if x.startswith(f"  {k} (")) for k in ("built-in tools", "other tool entries"))
    assert "DeferredToolPlaceholder" in other and "DeferredToolPlaceholder" not in builtin
    assert all(sent <= line for sent, line in rows.values()), out
    assert all(line <= sent * 1.02 for name, (sent, line) in rows.items() if name != "Yerk AI's tools"), rows
    s = b["session_start"]
    assert sum(s["claude_code"].values()) + sum(s["appended"].values()) == s["total"] == 80000


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
