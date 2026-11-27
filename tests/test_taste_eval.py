"""Checks the taste eval runner (evals/taste/run.py), where a judge model reads a room's reply and says whether you would
complain about it: the judge never sees what you wrote after the reply, the reply is marked in the prompt, every row gets one
call, and the table's numbers follow from the calls (a fake claude answers true when the reply carries a marker word)."""
import importlib.util
import json
import os
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE = """#!/usr/bin/env python3
import json, sys
prompt = sys.stdin.read()
hit = "MARKER" in prompt
assert "LATER_WORDS" not in prompt, "the owner's next message reached the judge"
assert "--json-schema" in sys.argv and "--system-prompt-file" in sys.argv
ans = {"would_complain": hit, "kind": "wrong fact" if hit else "none", "why": "marker" if hit else "clean"}
print(json.dumps({"type": "result", "is_error": False, "duration_ms": 1200, "duration_api_ms": 900, "num_turns": 1,
                  "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                  "cache_read_input_tokens": 0, "output_tokens": 20}, "result": json.dumps(ans), "structured_output": ans}))
"""


def load_runner():
    spec = importlib.util.spec_from_file_location("taste_run", ROOT / "evals" / "taste" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def row(i, control, reply, split, kind="wrong fact", held=False):
    before = [{"author": "owner", "at": f"2026-01-01 10:0{i}:00", "message_id": f"m{i}a", "text": "what did we decide?"},
              {"author": "room", "at": f"2026-01-01 10:0{i}:30", "message_id": f"m{i}b", "text": reply}]
    r = {"id": f"{'K' if control else 'C'}{i}", "room": "r", "at": f"2026-01-01 10:0{i}:40", "message_id": f"m{i}c",
         "reply_id": f"m{i}b", "reply": reply, "reply_at": before[1]["at"], "before": before, "message": "LATER_WORDS why", "split": split}
    if not control:
        r.update(kind=kind, held=held, right="x")
    return r


def test_the_runner_scores_a_fake_judge_and_hides_the_owners_next_message(tmp_path):
    run = load_runner()
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    cases = [row(1, False, "MARKER: a figure it never read", "heldout"), row(2, False, "a plain answer", "heldout"),
             row(3, False, "MARKER again", "rubric", held=True)]
    controls = [row(4, True, "MARKER in an accepted reply", "heldout"), row(5, True, "fine", "heldout"), row(6, True, "fine too", "rubric")]
    (tmp_path / "cases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in cases))
    (tmp_path / "controls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in controls))
    (tmp_path / "rubric.md").write_text("# rubric\n")
    p = run.prompt_for(cases[0])
    assert "THE REPLY UNDER JUDGMENT" in p and "LATER_WORDS" not in p and "MARKER" in p
    out = tmp_path / "scores.json"
    rc = run.main(["--cases", str(tmp_path / "cases.jsonl"), "--controls", str(tmp_path / "controls.jsonl"), "--rubric",
                   str(tmp_path / "rubric.md"), "--out", str(out), "--claude", str(fake), "--jobs", "2"])
    assert rc == 0
    s = json.loads(out.read_text())
    held = next(m for m in s["metrics"] if m["split"] == "heldout" and not m["without_held"])
    assert (held["cases"], held["controls"], held["tp"], held["fn"], held["fp"]) == (2, 2, 1, 1, 1)
    assert (held["precision"], held["recall"], held["false_alarm_rate"], held["kind_match"]) == (0.5, 0.5, 0.5, 1.0)
    no_held = next(m for m in s["metrics"] if m["split"] == "all" and m["without_held"])
    assert no_held["cases"] == 2 and no_held["tp"] == 1
    assert s["cost_per_call"]["calls"] == 6 and s["cost_per_call"]["errors"] == 0 and s["cost_per_call"]["tokens_mean"] == 130
    assert len(s["rows"]) == 6 and all("why" in r for r in s["rows"])
    assert "bar" in s and s["bar"]["cases"] == 100
    assert not os.path.exists(tmp_path / "nothing")


def test_an_error_result_is_counted_and_never_scored(tmp_path):
    run = load_runner()
    fake = tmp_path / "claude"
    fake.write_text('#!/usr/bin/env python3\nimport json\nprint(json.dumps({"type": "result", "is_error": True, "result": "Not logged in", "duration_ms": 5}))\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    rec = run.judge(str(fake), tmp_path / "r.md", "x", "", "", 30)
    assert rec.get("error") and "would_complain" not in rec
    m = run.metrics([{"is_control": False, "split": "heldout", **rec}], "heldout", False)
    assert m["cases"] == 0 and m["recall"] is None
