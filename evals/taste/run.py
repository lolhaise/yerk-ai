#!/usr/bin/env python3
"""The taste eval, phase 1: a judge model reads a room's reply with its context and says whether the owner would
complain about it, and of which kind; precision, recall and the false-alarm rate on a held-out half.

The case file is private and lives outside the repository: every message of the owner's rooms that was a complaint, a
correction or a push-back about the reply before it (cases.jsonl, one JSON object a line: id, room, before, reply,
kind, split, held), and at least as many replies the owner accepted (controls.jsonl, same shape, no kind). The rubric is a
Markdown file written for the judge: the kinds, each with the owner's own words as its examples. The judge never sees
what the owner wrote after the reply; that is the label.

One `claude -p` call per row, on whatever account and model the environment gives it (CLAUDE_CONFIG_DIR, --model),
hooks and settings off, no tools, no session kept, the rubric as the whole system prompt, the answer forced to JSON by
--json-schema. The table prints once; scores.json carries every row, the metrics per split and the cost per call as
`claude -p` reported it (seconds, tokens, the list price it quotes).

The bar a judge must clear before it may block (yerk_ai/evidence.py): 100 cases with under 1 in 10 false alarms. The
run prints where this judge stands against it. Phase 2, not built here: the `taste` shadow rule in the Stop check,
after the reply never before it, ledgered in `shadow` beside figure and stance, and `yerk eval taste` to re-run this.

usage: python evals/taste/run.py --cases cases.jsonl --controls controls.jsonl --rubric rubric.md --out scores.json
         [--model NAME] [--effort LEVEL] [--jobs N] [--limit N] [--split heldout|rubric|all] [--claude PATH] [--timeout S]
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

KINDS = ["wrong fact", "wrong register", "missed the ask", "too long", "no check before claiming",
         "filed instead of answering", "slow or no answer", "repeated itself", "overstepped", "other"]
SCHEMA = {"type": "object",
          "properties": {"would_complain": {"type": "boolean"}, "kind": {"type": "string", "enum": KINDS + ["none"]},
                         "why": {"type": "string"}},
          "required": ["would_complain", "kind", "why"]}
BAR_CASES = 100            # evidence.py: shadow mode until 100 cases show
BAR_FALSE_ALARMS = 0.10    # under 1 in 10 false alarms
SPLITS = ("heldout", "rubric", "all")


def read_rows(path: Path, control: bool) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            r["is_control"] = control
            rows.append(r)
    return rows


def prompt_for(row: dict[str, Any]) -> str:
    """The judge's input: the room, the messages before the owner's next message (oldest first) with the reply under
    judgment marked, and never what the owner wrote after it."""
    out = [f"Room: {row['room']}", "", "Messages before the owner's next message, oldest first:"]
    reply_id = row.get("reply_id")
    for b in row.get("before", []):
        who = "owner" if b.get("author") == "owner" else "room"
        mark = "  <-- THE REPLY UNDER JUDGMENT" if reply_id and b.get("message_id") == reply_id else ""
        atts = f" [attachments: {', '.join(b['attachments'])}]" if b.get("attachments") else ""
        out.append(f"[{who} {b.get('at', '')}]{mark} {b.get('text', '')}{atts}")
    if not any(b.get("message_id") == reply_id for b in row.get("before", [])):
        atts = f" [attachments: {', '.join(row['reply_attachments'])}]" if row.get("reply_attachments") else ""
        out.append(f"[room {row.get('reply_at', '')}]  <-- THE REPLY UNDER JUDGMENT {row.get('reply', '')}{atts}")
    out += ["", "Would the owner complain about, correct or push back on the reply marked above? Answer with the JSON."]
    return "\n".join(out)


def judge(claude: str, rubric: Path, prompt: str, model: str, effort: str, timeout: float) -> dict[str, Any]:
    argv = [claude, "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--system-prompt-file", str(rubric),
            "--tools", "", "--setting-sources", "", "--no-session-persistence"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    t0 = time.time()
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout:.0f} s", "seconds": round(time.time() - t0, 1)}
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return {"error": f"no JSON (rc={p.returncode}): {(p.stderr or p.stdout)[-200:]}", "seconds": round(time.time() - t0, 1)}
    answer = d.get("structured_output")
    if not isinstance(answer, dict):
        try:
            answer = json.loads(d.get("result") or "")
        except ValueError:
            answer = None
    usage = d.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
    rec: dict[str, Any] = {"seconds": round((d.get("duration_ms") or 0) / 1000, 2), "api_seconds": round((d.get("duration_api_ms") or 0) / 1000, 2),
                           "tokens": tokens, "output_tokens": int(usage.get("output_tokens") or 0),
                           "cost_usd": d.get("total_cost_usd"), "is_error": bool(d.get("is_error"))}
    if d.get("is_error") or not isinstance(answer, dict) or "would_complain" not in answer:
        rec["error"] = str(d.get("result"))[:200]
        return rec
    rec.update(would_complain=bool(answer["would_complain"]), pred_kind=str(answer.get("kind", "none")), why=str(answer.get("why", ""))[:400])
    return rec


def metrics(rows: list[dict[str, Any]], split: str, drop_held: bool) -> dict[str, Any]:
    sel = [r for r in rows if (split == "all" or r.get("split") == split) and "would_complain" in r]
    if drop_held:
        sel = [r for r in sel if not r.get("held")]
    cases = [r for r in sel if not r["is_control"]]
    controls = [r for r in sel if r["is_control"]]
    tp = sum(1 for r in cases if r["would_complain"])
    fn = len(cases) - tp
    fp = sum(1 for r in controls if r["would_complain"])
    kind_ok = sum(1 for r in cases if r["would_complain"] and r.get("pred_kind") == r.get("kind"))
    return {"split": split, "without_held": drop_held, "cases": len(cases), "controls": len(controls), "tp": tp, "fn": fn, "fp": fp,
            "tn": len(controls) - fp, "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / len(cases), 3) if cases else None,
            "false_alarm_rate": round(fp / len(controls), 3) if controls else None,
            "kind_match": round(kind_ok / tp, 3) if tp else None}


def table(ms: list[dict[str, Any]]) -> str:
    head = f"{'split':<18} {'cases':>5} {'ctrls':>5} {'TP':>3} {'FN':>3} {'FP':>3} {'prec':>6} {'recall':>6} {'false alarms':>12} {'kind ok':>7}"
    lines = [head, "-" * len(head)]
    def f(v: float | None) -> str:
        return "  n/a" if v is None else f"{v:.3f}"

    for m in ms:
        name = m["split"] + (" (no held)" if m["without_held"] else "")
        lines.append(f"{name:<18} {m['cases']:>5} {m['controls']:>5} {m['tp']:>3} {m['fn']:>3} {m['fp']:>3} {f(m['precision']):>6} "
                     f"{f(m['recall']):>6} {f(m['false_alarm_rate']):>12} {f(m['kind_match']):>7}")
    return "\n".join(lines)


def cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    done = [r for r in rows if "seconds" in r and not r.get("error")]
    secs = [r["seconds"] for r in done]
    toks = [r["tokens"] for r in done]
    usd = [r["cost_usd"] for r in done if isinstance(r.get("cost_usd"), (int, float))]
    return {"calls": len(done), "errors": sum(1 for r in rows if r.get("error")),
            "seconds_mean": round(statistics.mean(secs), 1) if secs else None, "seconds_median": round(statistics.median(secs), 1) if secs else None,
            "seconds_max": max(secs) if secs else None, "tokens_mean": round(statistics.mean(toks)) if toks else None,
            "tokens_total": sum(toks), "output_tokens_mean": round(statistics.mean(r["output_tokens"] for r in done)) if done else None,
            "cost_usd_total_as_reported": round(sum(usd), 4) if usd else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", required=True, type=Path)
    ap.add_argument("--controls", required=True, type=Path)
    ap.add_argument("--rubric", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--model", default="", help="the judge's model (default: the claude binary's own default)")
    ap.add_argument("--effort", default="", help="low, medium, high, xhigh or max (default: the binary's)")
    ap.add_argument("--jobs", type=int, default=4, help="calls in flight at once")
    ap.add_argument("--limit", type=int, default=0, help="only the first N rows of each file (a smoke run)")
    ap.add_argument("--split", default="all", choices=SPLITS, help="which rows to judge")
    ap.add_argument("--claude", default="claude", help="the claude binary")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds per call")
    a = ap.parse_args(argv)
    rows = read_rows(a.cases, False) + read_rows(a.controls, True)
    if a.split != "all":
        rows = [r for r in rows if r.get("split") == a.split]
    if a.limit:
        rows = [r for r in rows if not r["is_control"]][:a.limit] + [r for r in rows if r["is_control"]][:a.limit]
    print(f"judging {len(rows)} rows ({sum(1 for r in rows if not r['is_control'])} cases, {sum(1 for r in rows if r['is_control'])} controls)"
          f" with {a.jobs} in flight, model {a.model or 'default'}, effort {a.effort or 'default'}", file=sys.stderr)

    def one(r: dict[str, Any]) -> dict[str, Any]:
        rec = judge(a.claude, a.rubric, prompt_for(r), a.model, a.effort, a.timeout)
        if rec.get("error") and not rec.get("is_error"):
            rec = judge(a.claude, a.rubric, prompt_for(r), a.model, a.effort, a.timeout)       # one retry on a transport failure
        keep = {k: r.get(k) for k in ("id", "room", "at", "split", "kind", "held", "is_control", "reply_id")}
        keep.update(rec)
        return keep

    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        results = list(ex.map(one, rows))
    ms = [metrics(results, s, h) for s in SPLITS for h in (False, True)]
    c = cost(results)
    out = {"model": a.model or "default", "effort": a.effort or "default", "rubric_chars": len(a.rubric.read_text()),
           "metrics": ms, "cost_per_call": c, "bar": {"cases": BAR_CASES, "false_alarm_rate_under": BAR_FALSE_ALARMS},
           "rows": results}
    a.out.write_text(json.dumps(out, indent=1))
    print(table(ms))
    held = next(m for m in ms if m["split"] == "heldout" and not m["without_held"])
    n_cases = sum(1 for r in rows if not r["is_control"])
    far = held["false_alarm_rate"]
    print(f"\nbar: {BAR_CASES} cases with under 1 in {int(1 / BAR_FALSE_ALARMS)} false alarms; this run: {n_cases} cases"
          f" ({held['cases']} held out), held-out false alarms {far if far is not None else 'n/a'}"
          f" -> {'under' if far is not None and far < BAR_FALSE_ALARMS else 'not under'} the bar on false alarms,"
          f" {'at' if n_cases >= BAR_CASES else 'short of'} the bar on cases")
    print(f"cost per call: {c['seconds_mean']} s mean ({c['seconds_median']} s median, {c['seconds_max']} s max), "
          f"{c['tokens_mean']} tokens mean ({c['output_tokens_mean']} out), {c['errors']} errors, "
          f"list price as reported {c['cost_usd_total_as_reported']} USD for {c['calls']} calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
