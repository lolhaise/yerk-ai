"""Checks `yerk eval taste`, which replays past turns to the taste judge and scores it. The judge reads the owner's message, the
reply and what the turn read; the eval scores it beside phase 1 and prints the bar without enforcing it; a reply to the owner
makes no judge call (PR 93 cut to its eval half, the shadow thread left out). A fake claude stands in for the judge: it flags a
figure no read line carries, a filing for an answer, and (an example false alarm) a reply that says enjoy. No network."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from test_gates import call, human_turn, setup

from yerk_ai import cli, config, evidence, taste_eval
from yerk_ai.ledger import Ledger

FIX = Path(__file__).parent / "fixtures"
CASES, CONTROLS = FIX / "taste-judge-cases.jsonl", FIX / "taste-judge-controls.jsonl"
JUDGE = r'''
def answer(prompt):
    reply = next(x for x in prompt.splitlines() if "THE REPLY UNDER JUDGMENT" in x)
    reads = [x for x in prompt.splitlines() if x.startswith("- ") and " chars)" in x]
    fig = re.search(r"\d[\d,]*\s?sf", reply)
    if fig and not any(fig.group(0) in x for x in reads):
        return {"would_complain": True, "kind": "no check before claiming", "why": "the figure has no read", "cite": "none"}
    if "Filed" in reply:
        return {"would_complain": True, "kind": "filed instead of answering", "why": "bookkeeping for a verdict", "cite": "none"}
    if "enjoy" in reply:
        return {"would_complain": True, "kind": "wrong register", "why": "an example false alarm", "cite": "none"}
    return {"would_complain": False, "kind": "none", "why": "answered from its read", "cite": reads[0][2:40] if reads else "none"}
'''
FAKE = "#!/usr/bin/env python3\nimport json, re, sys\n" + JUDGE + r'''
prompt = sys.stdin.read()
assert "--json-schema" in sys.argv and "--system-prompt-file" in sys.argv and sys.argv[sys.argv.index("--tools") + 1] == ""
assert "--setting-sources" in sys.argv and "--no-session-persistence" in sys.argv
ans = answer(prompt)
print(json.dumps({"type": "result", "is_error": False, "duration_ms": 1500, "usage": {"input_tokens": 10, "output_tokens": 5},
                  "result": json.dumps(ans), "structured_output": ans}))
'''
ns: dict = {"re": re}
exec(JUDGE, ns)


def fake_run(prompt, cap):                     # the same judge in process, for the eval's arithmetic
    ans = ns["answer"](prompt)
    return json.dumps({"type": "result", "is_error": False, "duration_ms": 1500, "usage": {}, "result": "", "structured_output": ans})


def fake_claude(tmp_path, monkeypatch):
    (f := tmp_path / "claude").write_text(FAKE)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("YERK_CLAUDE", str(f))
    return f


def turn(*calls):
    """A turn's evidence: each (tool, input, text back) as the stream gives it."""
    ev = evidence.Evidence()
    for i, (tool, ti, text) in enumerate(calls):
        ev.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"tu{i}", "name": tool, "input": ti}]}})
        ev.observe({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"tu{i}", "content": text}]}})
    return ev


def test_a_reply_to_the_owner_makes_no_judge_call(tmp_path, monkeypatch):
    """The shadow judge was left out of PR 93's cut: a reply to the owner through the Stop hook, its figure carried by the
    turn's read, runs no claude process, calls none of the judge's functions, leaves no thread and writes no taste row,
    and no room key turns a judge on. The mutation: PR 93's taste_later back in the Stop hook goes red here."""
    marker = tmp_path / "judged"
    (f := tmp_path / "claude").write_text(f"#!/bin/sh\ntouch {marker}\n")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("YERK_CLAUDE", str(f))
    called: list[str] = []
    for fn in ("taste_prompt", "taste_runner", "taste_judge"):
        monkeypatch.setattr(evidence, fn, lambda *a, fn=fn, **k: called.append(fn))
    y, room, ch = setup(tmp_path)
    human_turn(room, "how big is the slab?")
    room.rp = SimpleNamespace(spec=SimpleNamespace(model="example-model"), account=room.accounts[0], session_id="sid-g", read_paths=set())
    room.evidence = turn(("Read", {"file_path": "notes/site.md"}, "Site notes: slab 4,015 sf, poured in March."))
    before = set(threading.enumerate())
    out = call(y, {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "The slab is 4,015 sf, from the site notes."})
    for t in set(threading.enumerate()) - before:                          # anything the hook started, ended before the check
        t.join(timeout=10)
    assert out == {} and called == [] and not marker.exists()               # the reply stands and nothing judged it
    assert not [r for r in y.ledger.rows(50, "shadow") if r["rule"] == "taste"]
    assert not hasattr(y, "taste_later") and "taste_judge" not in config.ROOM_KEYS


def test_the_judge_reading_only_the_chat_cannot_tell_a_checked_figure_from_an_unchecked_one():
    """The mutation: a judge that reads the chat alone (no reads, phase 1's shape) flags the control whose figure its read
    carries, which the judge with the reads passes; the eval below goes red the same way."""
    row = json.loads(CONTROLS.read_text().splitlines()[0])
    rec = evidence.taste_judge(evidence.taste_prompt(row), fake_run)
    assert (rec["verdict"], rec["why"], rec["cite"], rec["kind"]) == ("ok", "answered from its read", "Read file_path=notes/site.md (120 char", "none")
    assert evidence.taste_judge(evidence.taste_prompt({**row, "reads": None}), fake_run)["verdict"] == "unchecked-claim"


def test_the_prompt_carries_each_read_with_its_first_words_and_the_recall_never_his_next_message():
    reads = [{"tool": "Read", "target": "file_path=notes/site.md", "chars": 500, "head": ""},
             {"tool": "Bash", "target": "command=ls notes", "chars": 7, "head": ""},
             {"tool": "mcp__yerk__recall", "target": "query=slab", "chars": 33, "head": "slab 4,015 sf [c:notes/site.md:3]"}]
    row ={"room": "example-room", "reply_id": "r1", "reply": "The slab is 4,015 sf.", "reads": reads, "recall": "[recall] a line",
           "before": [{"author": "owner", "at": "09:00", "message_id": "m1", "text": "how big?"}]}
    p = evidence.taste_prompt(row)
    assert "[owner 09:00] how big?" in p and "THE REPLY UNDER JUDGMENT The slab is 4,015 sf." in p
    assert "- Read file_path=notes/site.md (500 chars)\n- Bash command=ls notes (7 chars)\n" in p
    assert "- mcp__yerk__recall query=slab (33 chars): slab 4,015 sf [c:notes/site.md:3]" in p and "[recall] a line" in p
    assert "- nothing: the turn made no call" in evidence.taste_prompt({**row, "reads": []})
    alone = evidence.taste_prompt({**row, "reads": None})                  # no ledger: the chat alone, never told it read nothing
    assert "What the turn read" not in alone and "was not checked" not in alone
    sent = evidence.taste_prompt({**row, "reply_at": "09:01", "reply_attachments": ["plan.pdf"]})    # the reply is not in `before`
    assert "[room 09:01]  <-- THE REPLY UNDER JUDGMENT The slab is 4,015 sf. [attachments: plan.pdf]" in sent


def test_the_eval_arithmetic_on_the_example_fixture_and_the_chat_only_mutation_goes_red(tmp_path):
    lines, rep = taste_eval.run(CASES, CONTROLS, evidence.TASTE_RUBRIC, None, tmp_path / "s.json", judge=fake_run)
    a, held = rep["metrics"]
    assert (a["cases"], a["controls"], a["tp"], a["fp"]) == (3, 3, 2, 1)
    assert (a["precision"], a["recall"], a["false_alarms"], a["seconds_median"]) == (0.667, 0.667, 0.333, 1.5)
    assert a["verdicts"] == {"missed-the-ask": (1, 1, 0), "unchecked-claim": (1, 1, 0), "not-his-voice": (0, 0, 1), "slow-or-no-answer": (0, 1, 0)}
    assert (held["cases"], held["tp"], held["fp"], held["precision"], held["recall"], held["false_alarms"]) == (2, 1, 1, 0.5, 0.5, 0.5)
    assert "heldout       2     2   0.500  0.500        0.500            0.714  0.147        0.054" in lines
    assert "unchecked-claim       1 of 1       0 of 3              8 of 31      4 of 74" in lines
    assert lines[-1].endswith("recall 0.667, false alarms 0.333: does not clear it")
    assert json.loads((tmp_path / "s.json").read_text())["rows"][0]["verdict"] == "unchecked-claim"
    chat = tmp_path / "chat.jsonl"                                          # the same controls with no reads: a false alarm more
    chat.write_text("".join(json.dumps({k: v for k, v in json.loads(x).items() if k != "reads"}) + "\n" for x in CONTROLS.read_text().splitlines()))
    _, rep = taste_eval.run(CASES, chat, evidence.TASTE_RUBRIC, None, None, judge=fake_run)
    assert rep["metrics"][0]["fp"] == 2


def test_the_eval_takes_each_turns_reads_from_the_ledger_cut_at_the_reply_and_a_stratified_sample(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    tid = led.record(room="example-room", session_id="s", turn_key="tk9", started=100.0)
    led.inbound_add("in9", "example-room", "c", "owner", 10)
    led.inbound_set("in9", turn_id=tid, reply_ids="r9")
    led.evidence(t=200.0, room="example-room", session_id="s", turn="tk9", tool="Read", target="file_path=a.md", chars=12)
    led.evidence(t=900.0, room="example-room", session_id="s", turn="tk9", tool="Grep", target="pattern=x", chars=3)
    led.close()
    import sqlite3
    db = sqlite3.connect(f"file:{tmp_path / 'ledger.sqlite'}?mode=ro", uri=True)
    assert taste_eval.reads_of(db, "r9", 300.0) == [{"tool": "Read", "target": "file_path=a.md", "chars": 12, "head": ""}]
    assert len(taste_eval.reads_of(db, "r9") or []) == 2 and taste_eval.reads_of(db, "nope") is None
    rows = taste_eval.read_rows(CASES, False) + taste_eval.read_rows(CONTROLS, True)
    picked = taste_eval.sample(rows, 2)
    assert [r["id"] for r in picked] == ["E01", "E02", "K01", "K03"]       # one case of each verdict in turn, one control a room


def test_yerk_eval_taste_prints_phase_2_beside_phase_1_and_the_bar_and_never_fails_on_the_numbers(tmp_path, monkeypatch, capsys):
    fake_claude(tmp_path, monkeypatch)
    rc = cli.main(["eval", "taste", "--cases", str(CASES), "--controls", str(CONTROLS), "--out", str(tmp_path / "s.json"),
                   "--ledger", str(tmp_path / "none.sqlite"), "--state", str(tmp_path), "--jobs", "2"])
    out = capsys.readouterr().out
    assert rc == 0 and "phase 2 (what the turn read) beside phase 1 (the chat alone)" in out
    assert "bar for a gate (printed, not enforced): recall at least 5 of 7 (0.714)" in out and "does not clear it" in out
    assert "median judge seconds: 1.5 (phase 1: 5.5)" in out and os.path.exists(tmp_path / "s.json")
    assert cli.main(["eval", "taste", "--cases", str(tmp_path / "missing.jsonl"), "--controls", str(CONTROLS), "--state", str(tmp_path)]) == 2


def test_a_failed_call_is_an_error_row_with_its_reason_and_scores_nothing(tmp_path):
    def limited(prompt, cap):                  # an account out of a model: is_error, its reason in result
        return json.dumps({"type": "result", "is_error": True, "duration_ms": 300, "result": "You've reached your usage limit."})
    assert evidence.taste_judge(evidence.taste_prompt({"room": "r", "reply": "x", "reads": []}), limited) == {
        "seconds": 0.3, "tokens": 0, "error": "You've reached your usage limit."}
    lines, rep = taste_eval.run(CASES, CONTROLS, evidence.TASTE_RUBRIC, None, None, judge=limited)
    assert (rep["metrics"][0]["cases"], rep["metrics"][0]["errors"]) == (0, 6)
    assert lines[-1] == "no numbers: every call failed (6), the first: You've reached your usage limit."


def test_a_call_past_the_cap_is_a_timeout_that_flags_nothing_and_is_counted():
    def slow(prompt, cap):                     # the judge still running at --cap
        raise subprocess.TimeoutExpired("claude", cap)
    assert evidence.taste_judge("x", slow, 0.5)["timeout"] is True
    _, rep = taste_eval.run(CASES, CONTROLS, evidence.TASTE_RUBRIC, None, None, cap=0.5, judge=slow)
    a = rep["metrics"][0]
    assert (a["cases"], a["controls"], a["tp"], a["fp"], a["timeouts"], a["errors"]) == (3, 3, 0, 0, 6, 0)
