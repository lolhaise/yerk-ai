"""The budget test: every example room's context fits budgets.toml, and a planted file fails."""
import json
import os
import re
import shutil
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yerk_ai import cli, context, memindex, memory
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, load_toml, rooms, state_dir, uncapped
from yerk_ai.lifted.clock import clock_line
from yerk_ai.lifted.trim import truncate

PLANTED = Path(__file__).parent / "fixtures" / "over-budget"
NOW = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)


def layer_budgets(private):
    """budgets.toml as the layer's daemon reads it, its yerkd.toml's [limits] uncapped included."""
    return budgets(load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {})


def held(private, bad, where):
    """The violations that fail the test: an uncapped install's ([limits] uncapped) are each reported, a warning, never a failure."""
    if not uncapped(layer_budgets(private)):
        return bad
    for v in bad:
        warnings.warn(f"uncapped, past its line: {where} {v}", stacklevel=2)
    return []


def build(private, name, state):
    """A room's parts as the daemon and the morning diff build them: the install's rules file included, Claude Code's parts
    as the room last sent them (context.sent, `yerk map`'s read), at their line for a room with no transcript."""
    b = layer_budgets(private)
    room = rooms(private)[name]
    start = context.session_start(private, room, b, rules=memory.settings(private, state).root / memory.RULES, room_name=name)
    msg = context.per_message(name, room, "owner", NOW, NOW - timedelta(minutes=5), b)
    return context.violations(start, msg, b, *context.sent(private, state, name, b)), start


@pytest.mark.parametrize("name", sorted(rooms(EXAMPLE_PRIVATE)))
def test_example_rooms_fit_their_budgets(name, tmp_path):
    bad, _ = build(EXAMPLE_PRIVATE, name, tmp_path)
    assert held(EXAMPLE_PRIVATE, bad, name) == []


def test_planted_over_budget_file_fails_and_is_sent_trimmed(tmp_path):
    bad, start = build(PLANTED, "planted-room", tmp_path)
    assert any(v.startswith("persona:") for v in bad)
    persona = next(p for p in start if p.name == "persona")
    assert persona.over and persona.size <= persona.budget
    assert "[...trimmed persona: kept" in persona.text and "Omitted sections:" in persona.text


def test_planted_over_budget_file_in_an_uncapped_install_is_sent_whole_and_reported_never_a_failure(tmp_path):
    """[limits] uncapped (the owner's call: every cap off, in a way that can be reverted at any point):
    the same planted persona goes out whole, its line kept as a measurement; the fit test reports it and passes."""
    shutil.copytree(PLANTED, private := tmp_path / "planted")
    (private / "yerkd.toml").write_text("[limits]\nuncapped = true\n")
    bad, start = build(private, "planted-room", tmp_path / "state")
    persona = next(p for p in start if p.name == "persona")
    assert any(v.startswith("persona:") for v in bad) and persona.over and persona.size == persona.raw > persona.budget
    assert "[...trimmed" not in context.appended_file(start) and persona.cut == ()
    with pytest.warns(UserWarning, match=r"^uncapped, past its line: planted-room ") as seen:     # its mechanics too (PAID)
        assert held(private, bad, "planted-room") == []
    assert any(str(w.message).startswith("uncapped, past its line: planted-room persona: ") for w in seen)


def test_the_code_stays_at_its_ceiling_which_only_goes_down():
    ceiling = budgets()["code"]["lines"]
    lines = sum(f.read_bytes().count(b"\n") for f in Path(context.__file__).parent.rglob("*.py"))
    assert lines <= ceiling, f"yerk_ai/ is {lines} lines, over the ceiling of {ceiling} in budgets.toml: cut as much as you add"
    assert lines == ceiling, f"yerk_ai/ shrank to {lines} lines: set [code] lines in budgets.toml to {lines} in this change"


@pytest.mark.skipif(not os.environ.get("YERK_PRIVATE"), reason="the real private layer exists only on the box")
def test_real_private_layer_fits():
    private, state = Path(os.environ["YERK_PRIVATE"]).expanduser(), state_dir()
    for name in rooms(private):
        bad = held(private, build(private, name, state)[0], name)     # an uncapped install's: reported, never a failure
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
    assert rec["slack"] == 1 and all(re.match(rf"\d{{4}}-\d\d-\d\d: (({'|'.join(memory.KINDS)}) )?\d+ to \d+, \S", n) for n in rec["drops"])


def test_the_ratchet_holds_each_kind_on_its_own_so_a_lever_that_lifts_one_and_sinks_another_is_seen():
    """A row carries each kind's numbers (cli.cmd_recall_bench); a row from before the kinds is the recall kind whole. Recall up 3
    and self down 3 leave the kinds together where they were, and the self kind's fall is still a drop, named for its kind; a
    note names another kind's drop only with the kind before BEST."""
    def run(rec: int, ep: int, me: int) -> dict:
        return {"top_k": rec + ep + me, "k": 6, "questions": 209, "kinds": {
            "recall": {"questions": 164, "top_k": rec}, "episodic": {"questions": 20, "top_k": ep}, "self": {"questions": 25, "top_k": me}}}
    four, lever = [run(100, 14, 20)] * 4, run(103, 14, 17)
    assert [memory.ratchet([*four, lever], [], 1, x) for x in ("recall", "episodic")] == [None, None]
    assert memory.ratchet([*four, lever], [], 1, "self") == ("self: top 6 fell to 17 of 25 from 20 in the 4 runs before, and no "
                                                            "dated note in budgets.toml [recall] drops names it")
    assert memory.ratchet([*four, lever], ["2026-10-25: self 20 to 17, the lever"], 1, "self") is None
    assert memory.ratchet([*four, lever], ["2026-10-25: 20 to 17, the lever"], 1, "self")              # no kind: a recall note
    assert memory.ratchet([*four, run(97, 14, 20)], ["2026-10-25: self 100 to 97, the lever"])         # a self note, not recall's
    old = [{"top_k": 100, "k": 6, "questions": 164}] * 4                                               # before the kinds
    assert memory.ratchet([*old, run(97, 14, 20)], []) and memory.ratchet([*old, run(97, 14, 17)], [], 1, "self") is None


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
    assert (drops := [d for x in r["kinds"] if (d := memory.ratchet([*rows, r], rec["drops"], rec["slack"], x))]) == [], drops


def test_the_real_layer_test_counts_the_rules_file(tmp_path, monkeypatch):
    """A room over budget only through the install's rules file fails the real-layer test, named with its part. Built
    without the rules file, as it was, the test passed such a room."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "memory" / memory.RULES).write_text("- a job: done this way.\n" + "- another job: " + "x" * 17000 + "\n")
    monkeypatch.setenv("YERK_PRIVATE", str(private))
    monkeypatch.setenv("YERK_STATE", str(tmp_path / "state"))
    with pytest.raises(AssertionError, match=rf"^example-room rules: 17,\d{{3}} > {budgets()['session_start']['appended']['rules']:,} \("):
        test_real_private_layer_fits()
    (private / "yerkd.toml").write_text("[limits]\nuncapped = true\n")         # the deploy's fast gate on an uncapped install
    with pytest.warns(UserWarning, match=r"^uncapped, past its line: example-(room|quiet) ") as seen:     # each room, reported
        test_real_private_layer_fits()
    assert any(str(w.message).startswith("uncapped, past its line: example-room rules: 17,") for w in seen)


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
    rules.write_text(rules.read_text() * 4)                                                     # 21,200: past the part's own budget (x3 was 15,986 against 16,000)
    bad, start = build(private, "example-room", tmp_path / "state")
    part = {p.name: p for p in start}
    assert [v.split(":")[0] for v in bad] == ["rules"] and "[...trimmed rules: kept" in part["rules"].text
    assert part["rulebook"].text.endswith("- a rule of this room.") and not part["rulebook"].over
    sealed = context.session_start(private, {**rooms(private)["example-room"], "sealed": True}, budgets(), rules=rules)
    assert next(p for p in sealed if p.name == "rules").raw == 0


# Claude Code's parts as the widest live room sends them, the largest of each across the six rooms' newest transcripts
# (2026-10-20, Claude Code 2.1.301): the prompt of a session started on Fable 5.1 (4,817 on Opus 5.5), each tool entry.
# Yerk AI's own entries measured again 2026-10-23 (PR 97's cold read): the work room's newest shape, ten tools, plus `seen`
# as this tree sends it on the daemon's Python 3.14; missing-ability's `need` as this tree builds it, 793 on Python 3.13,
# with the 15 `seen` gains as sent (830 built here, 845 sent).
# The web plug-in's research pair measured 2026-10-24 (web-budget, Claude Code 2.1.304) in the six rooms' newest shapes:
# web_research 1,284 and web_research_result 432 in each; web_fetch and web_search keep the bridge's 878 and 621 (a person
# room's newest shape, Oct 22), 7 and 1 over the plug-in's own.
# send-own-channel (2026-10-24): `send` as measured plus the 40 its description gains as this tree builds it (220 to 260).
# measured-budgets (2026-10-24, the same shapes read as sent): the hands plug-in's five tools in the two owner rooms that
# carry it, web_image in those and a person's room, and that room's Edit and Write beside the four built-in tools; Yerk AI's
# from an owner room's new start that evening (the tree of #117): schedule 2,180, seen 861 and need 824 as sent.
LIVE_PROMPT = 10889
LIVE_TOOLS = {"Bash": 4248, "Edit": 1037, "Glob": 785, "Grep": 3362, "Read": 2006, "Write": 694, "mcp__web-ladder__web_fetch": 878,
              "mcp__web-ladder__web_search": 621, "mcp__web-ladder__web_research": 1284, "mcp__web-ladder__web_research_result": 432,
              "mcp__web-ladder__web_image": 913, "mcp__hands__browser": 2077, "mcp__hands__desktop": 1381, "mcp__hands__discord": 1168,
              "mcp__hands__mac": 1312, "mcp__hands__mail": 1181, "mcp__yerk__hold": 481, "mcp__yerk__read_memory": 551, "mcp__yerk__recall": 656,
              "mcp__yerk__remember": 1656, "mcp__yerk__schedule": 2180, "mcp__yerk__send": 604, "mcp__yerk__settings": 878,
              "mcp__yerk__transcribe": 840, "mcp__yerk__voice_note": 911, "mcp__yerk__seen": 861, "mcp__yerk__need": 824,
              "DeferredToolPlaceholder": 214, "mcp__yerk__worker": 1292}


def entry(name, size):
    """A tool entry that Claude Code counts at `size` as sent (its JSON)."""
    t = {"name": name, "description": ""}
    return {**t, "description": "x" * (size - context.chars(json.dumps(t)))}


def test_claude_codes_parts_as_the_rooms_send_them_fit_their_lines_and_what_is_left_goes_to_the_rules(tmp_path):
    """`yerk map --wire` on the live rooms read the other tool entries, Claude Code's deferred-loading placeholder (sent
    in every room, only when deferred tool loading is on) and web-ladder's two tools, at 1,713 against 1,500, and the
    prompt line sat 511 over the largest prompt measured. Each Claude Code part of the widest room fits its line, the
    lines set from the measurement keep at most 2% over it, and the session-start lines add to exactly the total
    (decision 12), so what they freed went to `rules`. On 2026-10-24 the web plug-in's research pair (1,716) took the
    other entries to 3,429 in the six rooms' newest shapes, where the line still held 1,713 (web-budget); read as sent
    that day, the hands plug-in (7,119) and web_image (913) each got a line of their own, the built-in tools went to
    the widest room's 12,132, and Yerk AI's tools to the 11,734 a new start sent, paid by the prompt line (measured-budgets)."""
    shape = {"system": [{"type": "text", "text": "p" * LIVE_PROMPT}, {"type": "text", "text": "# Yerk AI mechanics"}],
             "tools": [entry(n, size) for n, size in LIVE_TOOLS.items()]}
    (t := tmp_path / "sid.jsonl").write_text(json.dumps({"type": "api-request-shape", "shape": shape}) + "\n")
    b = budgets()
    out = context.render_wire("r", context.wire(t), context.chars("# Yerk AI mechanics"), b)
    rows = {m[1]: (int(m[2].replace(",", "")), int(m[3].replace(",", ""))) for m in re.finditer(
        r"^  (system prompt: Claude Code's own|built-in tools|Yerk AI's tools|other tool entries|mcp__hands__|mcp__web-ladder__web_image)"
        r"(?: \(.*?\))?\s+([\d,]+)\s+([\d,]+)", out, re.M)}
    cc = b["session_start"]["claude_code"]
    assert rows == {"system prompt: Claude Code's own": (LIVE_PROMPT, cc["claude_code_prompt"]), "built-in tools": (12132, cc["builtin_tools"]),
                    "Yerk AI's tools": (11734, cc["yerk_tools"]), "other tool entries": (3429, cc["deferred_tool_names"]),
                    "mcp__hands__": (7119, cc["mcp__hands__"]), "mcp__web-ladder__web_image": (913, cc["mcp__web-ladder__web_image"])}
    builtin, other = (next(x for x in out.splitlines() if x.startswith(f"  {k} (")) for k in ("built-in tools", "other tool entries"))
    assert "DeferredToolPlaceholder" in other and "DeferredToolPlaceholder" not in builtin and "hands" not in other and "web_image" not in other
    assert all(sent <= line for sent, line in rows.values()), out
    assert all(line <= sent * 1.02 for name, (sent, line) in rows.items() if name != "Yerk AI's tools"), rows
    s = b["session_start"]
    assert sum(s["claude_code"].values()) + sum(s["appended"].values()) == s["total"] == 108093


def room_sent(tmp_path, tools, file="# Yerk AI mechanics\n- an example line"):
    """The example layer, and a state binding example-room to a transcript (example values) under the example accounts'
    ~/.claude whose newest request shape sends a prompt of LIVE_PROMPT, `file` after it, and `tools` (name: size as sent);
    an older shape before it sent nothing, and a message after it only names the row's type."""
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (t := tmp_path / "home" / ".claude" / "projects" / "p" / "s1.jsonl").parent.mkdir(parents=True)
    shapes = [{"system": [{"type": "text", "text": "p"}], "tools": []},
              {"system": [{"type": "text", "text": "p" * (LIVE_PROMPT - 1) + "\n" + file}], "tools": [entry(n, size) for n, size in tools.items()]}]
    t.write_text("".join(json.dumps({"type": "api-request-shape", "shape": s}) + "\n" for s in shapes) +
                 json.dumps({"type": "user", "message": {"role": "user", "content": "what is an api-request-shape row?"}}) + "\n")
    state.mkdir()
    (state / "sessions.json").write_text(json.dumps({"example-room": {"session_id": "s1", "transcript": str(t)}}))
    return private, state, t


SENT = {"Bash": 4000, "Read": 2000, "mcp__yerk__send": 600, "mcp__hands__browser": 2000, "mcp__hands__mail": 1000,
        "mcp__web-ladder__web_image": 900, "mcp__web-ladder__web_fetch": 800, "DeferredToolPlaceholder": 214}


def test_the_map_counts_claude_codes_parts_as_the_room_last_sent_them(tmp_path, capsys):
    """measured-budgets (2026-10-24): `yerk map` counted Claude Code's parts at their line, so rooms sent the hands plug-in,
    web_image and a person's room's Edit and Write past theirs and no check saw it. They come from the room's newest
    transcript now, its newest shape, each tool entry under its line, and the map says where it read them."""
    private, state, t = room_sent(tmp_path, SENT)
    b = budgets()
    sent, src = context.sent(private, state, "example-room", b)
    assert src == str(t) and sent == {"claude_code_prompt": LIVE_PROMPT, "builtin_tools": 6000, "yerk_tools": 600, "deferred_tool_names": 1014,
                                       "mcp__hands__": 3000, "mcp__web-ladder__web_image": 900, "skills": 0}
    assert cli.main(["map", "example-room", "--private", str(private), "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert f"Claude Code's parts as the room last sent them: {t}" in out
    assert re.search(r"^  mcp__hands__ +3,000 +7,119  ok, as sent$", out, re.M) and re.search(r"^  builtin_tools +6,000 +12,132  ok, as sent$", out, re.M)
    start = context.session_start(private, rooms(private)["example-room"], b, rules=memory.settings(private, state).root / memory.RULES)
    total = context.session_total(start, b) - sum(b["session_start"]["claude_code"].values()) + sum(sent.values())
    assert re.search(rf"^  total +{total:,} +{b['session_start']['total']:,}  ok$", out, re.M)


def test_the_gate_fails_on_a_part_the_room_sent_past_its_line_naming_the_room_the_part_and_the_transcript(tmp_path, monkeypatch):
    """The deploy's fast gate runs this file's real-layer test on the layer the daemon serves: the plug-in's entries one
    past their line fail it, named as the deploy card says it (the room, then its part); read at the line, as before this
    change, the same room passed."""
    line = budgets()["session_start"]["claude_code"]["mcp__hands__"]
    private, state, t = room_sent(tmp_path, {**SENT, "mcp__hands__browser": line - 999})        # with mail's 1,000: one past the line
    monkeypatch.setenv("YERK_PRIVATE", str(private))
    monkeypatch.setenv("YERK_STATE", str(state))
    with pytest.raises(AssertionError, match=rf"^example-room mcp__hands__: {line + 1:,} > {line:,} \(as sent, {re.escape(str(t))}\)"):
        test_real_private_layer_fits()


def test_a_room_with_no_transcript_is_counted_at_its_lines_and_the_map_says_so(tmp_path, capsys, monkeypatch):
    """A session whose path the hooks have not learnt yet is found by its id in an account's projects folder; with no
    transcript written, or no session at all, each of Claude Code's parts is at its line, the map says so, and `--wire`
    has nothing to read."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))           # the example accounts' ~/.claude: room_sent's
    private, state, t = room_sent(tmp_path, SENT)
    b = budgets()
    (state / "sessions.json").write_text(json.dumps({"example-room": {"session_id": "s1", "transcript": ""}}))
    assert context.sent(private, state, "example-room", b)[1] == str(t)
    t.unlink()
    assert context.sent(private, state, "example-room", b) == (b["session_start"]["claude_code"], "")
    (state / "sessions.json").unlink()
    assert context.sent(private, state, "example-room", b) == (b["session_start"]["claude_code"], "")
    assert cli.main(["map", "example-room", "--private", str(private), "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert "Claude Code's parts at their line: no transcript of this room yet" in out
    assert re.search(r"^  mcp__hands__ +- +7,119  counted at its line$", out, re.M)
    assert cli.main(["map", "example-room", "--wire", "--private", str(private), "--state", str(state)]) == 2
    assert "no transcript for room example-room" in capsys.readouterr().err


def test_the_wire_view_splits_the_prompt_where_that_starts_file_began_and_leaves_the_first_message_out_of_the_total(tmp_path):
    """The audit (2026-10-24): `yerk map --wire` took today's file from a prompt recorded at the room's last start, so
    Claude Code's own prompt read -6,401 in a room whose layer had changed since, and its total added the first message's
    blocks, which no line names."""
    _, _, t = room_sent(tmp_path, {"Read": 2000}, file="# Yerk AI mechanics\n" + "f" * 99)
    out = context.render_wire("example-room", context.wire(t), 5000, budgets())
    assert re.search(rf"^  system prompt: Claude Code's own +{LIVE_PROMPT:,} ", out, re.M)
    assert re.search(r"^  system prompt: Yerk AI's file +119 +`yerk map` builds 5,000 now$", out, re.M)
    assert re.search(rf"^  total +{LIVE_PROMPT + 119 + 2000:,} ", out, re.M)


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
    doctor's fit check reads red the day a part passes its line. On 2026-10-23 the mechanics' 200 (the web_image line)
    took it to 88,714, and the mechanics' 200 again (the X-links line) to 88,914. Later that day two rules sat held in
    rules-overflow.md with the shared file at 12,923 against 12,500 and curate proposing nothing to cut, so a lesson from
    two misses in one morning loaded in no room: `rules` to 15,000 and the total by the same 2,500 to 94,514; within hours four were held, so 16,000 and 95,514.
    Then the place line (200, location) took it to 95,714, and missing-ability's `need` tool (700) and its mechanics line
    (200) to 96,614, and the web plug-in's research pair (1,716, no part could pay) to 98,330. On 2026-10-24 Claude Code's
    parts read as sent (the hands plug-in, web_image, a person's room's Edit and Write: 9,763, no part could pay) to 108,093."""
    s = budgets()["session_start"]
    assert 900 <= s["appended"]["rulebook"] - LIVE_RULEBOOK <= 1400 and s["appended"]["rules"] == 16000
    assert sum(s["claude_code"].values()) + sum(s["appended"].values()) == s["total"] == 108093
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


def test_the_mechanics_say_when_to_use_the_place_unasked_in_one_line_inside_their_budget():
    """The place line is used unasked where it bears: a line used only when asked went unused."""
    text = context.MECHANICS.read_text().strip()
    lines = [x for x in text.splitlines() if "`[place]`" in x]
    assert len(lines) == 1 and all(w in lines[0] for w in ("unasked", "gym", "log the session", "work", "commute", "moving", "short"))
    assert context.chars(text) < budgets()["session_start"]["appended"]["mechanics"] == 4500


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
