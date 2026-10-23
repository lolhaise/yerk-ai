"""The budget test: every example room's context fits budgets.toml, and a planted file fails."""
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yerk_ai import context, memindex, memory
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


def test_the_recall_bench_is_a_ratchet_a_drop_holds_until_a_dated_note_names_it():
    def run(top_k: int, questions: int = 164) -> dict:
        return {"top_k": top_k, "k": 6, "questions": questions}
    four = [run(116), run(117), run(116), run(116)]
    assert memory.ratchet([*four, run(116)], []) is None                                  # one under the best of four: the slack
    assert memory.ratchet([*four, run(98)], []) == ("top 6 fell to 98 of 164 from 117 in the 4 runs before, and no dated note "
                                                    "in budgets.toml [recall] drops names it")
    assert memory.ratchet([*four, run(98)], ["2026-10-22: 117 to 98, the import"]) is None
    assert memory.ratchet([*four, run(90)], ["2026-10-22: 117 to 98, the import"])         # under the floor the note names
    assert memory.ratchet([*four, run(98)], ["117 to 98, the import"])                      # a note without its date names nothing
    assert memory.ratchet([run(117), *[run(98)] * 4, run(98)], []) is None                 # the best has left the window
    assert memory.ratchet([run(29, 46), run(98)], []) is None and memory.ratchet([run(98)], []) is None    # no history
    rec = budgets()["recall"]
    assert rec["slack"] == 1 and all(re.match(r"\d{4}-\d\d-\d\d: \d+ to \d+, \S", n) for n in rec["drops"])


def test_once_the_notes_best_leaves_the_window_a_97_is_refused_and_the_next_honest_run_passes():
    """The series as the first run on this code left it: 116, then 98, 98, 99 and 104, so the four runs before the next one
    no longer hold the 116 a "116 to 98" note names. A faked 97 passed on that note while 116 was in the window; from here
    it is refused, as 102 is, and the next honest run at 103 or more passes."""
    def run(top_k: int) -> dict:
        return {"top_k": top_k, "k": 6, "questions": 164}
    series, note = [run(116), run(98), run(98), run(99), run(104)], ["2026-10-22: 116 to 98, the import"]
    assert memory.ratchet([*series[:-1], run(97)], note) is None                       # 116 in the window: the note's floor
    assert memory.ratchet([*series, run(97)], note) == ("top 6 fell to 97 of 164 from 104 in the 4 runs before, and no dated "
                                                        "note in budgets.toml [recall] drops names it")
    assert memory.ratchet([*series, run(102)], note)
    assert [memory.ratchet([*series, run(n)], note) for n in (103, 104, 105)] == [None, None, None]


@pytest.mark.skipif(not os.environ.get("YERK_PRIVATE"), reason="the real index exists only on the box")
def test_the_real_index_holds_the_recall_bench_ratchet():
    """Where the install's index, question set and series are (the box: `yerk deploy`'s fast tests), the bench runs on the code
    under test, writing nothing, and its top six is held to the series as `yerk recall-bench` holds a run; skipped elsewhere."""
    s = memory.settings(Path(os.environ["YERK_PRIVATE"]).expanduser(), state_dir())
    series = s.index_db.parent / "recall-bench.jsonl"
    if not (memindex.live(s.index_db).is_file() and s.bench.is_file() and series.is_file()) or memory.meaning_state(s) != "ok":
        pytest.skip("no index, question set, series or meaning search here")
    r = memory.bench(s)
    assert r["ok"] and not r["blind"], r.get("error") or r["blind"]
    rec = budgets()["recall"]
    rows = [json.loads(x) for x in series.read_text().splitlines() if x.strip()]
    assert (drop := memory.ratchet([*rows, r], rec["drops"], rec["slack"])) is None, drop


def test_the_real_layer_test_counts_the_rules_file(tmp_path, monkeypatch):
    """A room over budget only through the install's rules file fails the real-layer test, named with its part. Built
    without the rules file, as it was, the test passed such a room."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "memory" / memory.RULES).write_text("- a job: done this way.\n" + "- another job: " + "x" * 13000 + "\n")
    monkeypatch.setenv("YERK_PRIVATE", str(private))
    monkeypatch.setenv("YERK_STATE", str(tmp_path / "state"))
    with pytest.raises(AssertionError, match=rf"^example-room rules: 13,\d{{3}} > {budgets()['session_start']['appended']['rules']:,} \("):
        test_real_private_layer_fits()


def test_the_rules_file_is_a_part_of_its_own_so_a_full_rulebook_never_pushes_a_rule_out(tmp_path):
    """On 2026-10-20 two rooms' rulebooks with the rules file inside came to 12,290 and 12,326 against 12,000, and the trim
    cut five shared rules from the middle. The rules file is its own part now, after the hand-kept files (the rooms write
    it as they go); past its own budget its trim marker names it; a sealed room gets none."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "rulebook.md").write_text("- a rule of this room.\n" * 290)                        # 6,670: near its cap
    rules = private / "memory" / memory.RULES
    rules.write_text("".join(f"- job {i}: how job {i} is done in every room.\n" for i in range(120)))      # 5,300
    bad, start = build(private, "example-room", tmp_path / "state")
    part = {p.name: p for p in start}
    assert list(part) == ["mechanics", "persona", "rulebook", "room_voice", "pinned_memory", "rules", "recall_pack"]
    assert bad == [] and not part["rulebook"].over and part["rules"].raw > 5000
    assert all(f"- job {i}: " in part["rules"].text for i in range(120)) and "[...trimmed" not in context.appended_file(start)
    rules.write_text(rules.read_text() * 3)                                                     # past the part's own budget
    bad, start = build(private, "example-room", tmp_path / "state")
    part = {p.name: p for p in start}
    assert [v.split(":")[0] for v in bad] == ["rules"] and "[...trimmed rules: kept" in part["rules"].text
    assert part["rulebook"].text.endswith("- a rule of this room.") and not part["rulebook"].over
    sealed = context.session_start(private, {**rooms(private)["example-room"], "sealed": True}, budgets(), rules=rules)
    assert next(p for p in sealed if p.name == "rules").raw == 0


# Claude Code's parts as the widest live room sends them, the largest of each across the six rooms' newest transcripts
# (2026-10-20, Claude Code 2.1.301): the prompt of a session started on Fable 5.1 (4,817 on Opus 5.5), each tool entry.
# Yerk AI's own entries measured again 2026-10-23 (PR 97's cold read): the work room's newest shape, ten tools, plus `seen`
# as this tree sends it on the daemon's Python 3.14.
LIVE_PROMPT = 10889
LIVE_TOOLS = {"Bash": 4248, "Glob": 785, "Grep": 3362, "Read": 2006, "mcp__web-ladder__web_fetch": 878,
              "mcp__web-ladder__web_search": 621, "mcp__yerk__hold": 481, "mcp__yerk__read_memory": 551, "mcp__yerk__recall": 656,
              "mcp__yerk__remember": 1656, "mcp__yerk__schedule": 2149, "mcp__yerk__send": 564, "mcp__yerk__settings": 878,
              "mcp__yerk__transcribe": 840, "mcp__yerk__voice_note": 911, "mcp__yerk__seen": 845,
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
                    "Yerk AI's tools": (10823, b["session_start"]["claude_code"]["yerk_tools"]),
                    "other tool entries": (1713, b["session_start"]["claude_code"]["deferred_tool_names"])}
    builtin, other = (next(x for x in out.splitlines() if x.startswith(f"  {k} (")) for k in ("built-in tools", "other tool entries"))
    assert "DeferredToolPlaceholder" in other and "DeferredToolPlaceholder" not in builtin
    assert all(sent <= line for sent, line in rows.values()), out
    assert all(line <= sent * 1.02 for name, (sent, line) in rows.items() if name != "Yerk AI's tools"), rows
    s = b["session_start"]
    assert sum(s["claude_code"].values()) + sum(s["appended"].values()) == s["total"] == 91614


# The largest room rulebook, the room's own text only since PR 47, across the six rooms: the work room's, read as session
# start builds it (13,518 before a hand edit took two blocks out of it).
LIVE_RULEBOOK = 12696


def test_the_rulebook_line_keeps_about_1000_over_the_largest_room_text_and_what_it_freed_went_to_the_rules(tmp_path):
    """Once the rules file left the rulebook, its line held 12,000 for a room text of 6,019 at the most. It keeps about
    1,000 over that now and every character it freed went to `rules`: a rulebook of that size and a rules file of about
    11,000 both go out whole, where `rules` at 6,886 trimmed that file, the total holding at 80,000 (decision 12). On
    2026-10-21 the taste rules took the largest room text to 9,860: the line followed it, `rules` kept its 11,886, and the
    total rose by the rulebook's 3,850 and room_voice's 500 to 84,350; on 2026-10-22 by the mechanics' 200 (the voice_note
    line) to 84,550, then by the mechanics' 200 again (the paste line) to 84,750. That day both line-per-rule parts were
    found cut from the middle at every start since Oct 21: the work room's rulebook (13,518, then 12,696) went to 14,000,
    about 10% over it, `rules` to 12,500 for the shared file's 12,298, and the total rose by the 3,764 to 88,514; the
    doctor's fit check reads red the day a part passes its line."""
    s = budgets()["session_start"]
    assert 900 <= s["appended"]["rulebook"] - LIVE_RULEBOOK <= 1400 and s["appended"]["rules"] == 12500
    assert sum(s["claude_code"].values()) + sum(s["appended"].values()) == s["total"] == 91614
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "rulebook.md").write_text("r" * (LIVE_RULEBOOK - 1) + "\n")
    (private / "memory" / memory.RULES).write_text("".join(f"- job {i}: " + "x" * 100 + "\n" for i in range(100)))
    bad, start = build(private, "example-room", tmp_path / "state")
    part = {p.name: p for p in start}
    assert bad == [] and part["rules"].raw > 11000 and "[...trimmed" not in context.appended_file(start)


def test_the_mechanics_say_when_work_goes_to_a_worker_inside_their_budget():
    """Seen live: a 36-step job ran 15 minutes inside a room's own turn; knowing `worker` exists is not knowing
    when to use it."""
    text = context.MECHANICS.read_text().strip()
    line = next(x for x in text.splitlines() if "`worker`" in x)
    assert "five minutes" in line and "one line now" in line
    assert context.chars(text) < budgets()["session_start"]["appended"]["mechanics"]


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
