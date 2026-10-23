"""`yerk eval taste`: measures how well the taste judge (evidence.taste_judge), a model asked whether you would complain about a reply, agrees
with the replies you complained about and the ones you accepted, each with what its turn read. It prints precision, recall, the false alarms
per verdict and the median judge seconds, beside phase 1's (the chat-only judge of evals/taste/run.py, PR 79; this one is from PR 93, judge-2,
without its shadow thread). The bar for a gate is printed, never enforced: nothing here judges a reply as it is posted or blocks one.

The case file is private (decision 22): cases.jsonl and controls.jsonl, one JSON object a line (id, room, before, reply_id,
reply, reply_at, split, held, and kind for a case), from --cases and --controls, else the private layer's evals/taste/;
tests/fixtures holds example values only. A row's reads are its own `reads` when it carries them, else the ledger's
evidence rows of the turn that posted its reply (read only, joined by the reply's message id, cut at the reply's time); a
row no turn of the ledger posted is judged on the chat alone and counted. The ledger keeps no text a call brought back, so
a read joined from it has no first words; a row's own `reads` may carry them (`head`).
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import evidence

VERDICTS = ("missed-the-ask", "unchecked-claim", "not-his-voice", "slow-or-no-answer")
BAR_RECALL, BAR_FALSE_ALARMS = 5 / 7, 0.10          # the bar a gate would need: 5 of 7 complaints caught, under 1 in 10 false alarms
# Phase 1, the chat-only judge (claude-fable-5-1 at medium effort, 142 calls, 2026-10-21): per split, and per verdict as
# (caught, cases, false alarms of the 74 controls), its ten kinds folded as evidence.TASTE_VERDICTS folds them.
PHASE1: dict[str, Any] = {
    "all": {"cases": 68, "controls": 74, "tp": 23, "fp": 8, "precision": 0.742, "recall": 0.338, "false_alarms": 0.108},
    "heldout": {"cases": 34, "controls": 37, "tp": 5, "fp": 2, "precision": 0.714, "recall": 0.147, "false_alarms": 0.054},
    "verdicts": {"missed-the-ask": (10, 29, 2), "unchecked-claim": (8, 31, 4), "not-his-voice": (1, 2, 2), "slow-or-no-answer": (4, 6, 0)},
    "seconds_median": 5.5}


def read_rows(path: Path, control: bool) -> list[dict[str, Any]]:
    return [{**json.loads(x), "is_control": control} for x in path.read_text().splitlines() if x.strip()]


def bucket(row: dict[str, Any]) -> str:
    """A case's verdict: its kind as evidence.TASTE_VERDICTS folds it."""
    return evidence.TASTE_VERDICTS.get(str(row.get("kind") or ""), "missed-the-ask")


def reads_of(db: sqlite3.Connection, reply_id: str, before: float | None = None) -> list[dict[str, Any]] | None:
    """What the turn that posted `reply_id` read (its evidence rows up to `before`, the reply's time), or None when no
    turn of the ledger posted it."""
    keys = [r[0] for r in db.execute("SELECT DISTINCT t.turn_key FROM message_links l JOIN inbound i ON i.message_id = l.row_key "
                                     "JOIN turns t ON t.id = i.turn_id WHERE l.tbl = 'inbound' AND l.col = 'reply_ids' "
                                     "AND l.message_id = ? AND coalesce(t.turn_key, '') != ''", (str(reply_id),))]
    if not keys:
        return None
    got = db.execute(f"SELECT t, tool, target, chars FROM evidence WHERE turn IN ({','.join('?' * len(keys))}) ORDER BY t, id", keys)
    return [{"tool": str(r[1]), "target": str(r[2] or ""), "chars": int(r[3] or 0), "head": ""} for r in got
            if before is None or float(r[0] or 0) <= before + 2]


def stamp(at: Any) -> float | None:
    """A row's local "YYYY-MM-DD HH:MM:SS" as epoch seconds, else None."""
    try:
        return datetime.fromisoformat(str(at)).timestamp() if at else None
    except ValueError:
        return None


def sample(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """`n` cases taken in turn from each verdict and `n` controls in turn from each room: a stratified subset."""
    def strat(xs: list[dict[str, Any]], key: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for x in xs:
            groups.setdefault(key(x), []).append(x)
        out: list[dict[str, Any]] = []
        while len(out) < min(n, len(xs)):
            for g in groups.values():
                if g and len(out) < n:
                    out.append(g.pop(0))
        return out
    return (strat([r for r in rows if not r["is_control"]], bucket)
            + strat([r for r in rows if r["is_control"]], lambda r: str(r.get("room"))))


def score(rows: list[dict[str, Any]], split: str = "all") -> dict[str, Any]:
    """Precision, recall and false alarms of the rows of `split`: a timeout flags nothing (the judge gave no verdict at
    its cap); a failed call (a usage limit, no answer) is no judgment and is left out, counted apart."""
    every = [r for r in rows if split == "all" or r.get("split") == split]
    sel = [r for r in every if not r.get("error")]
    cases, controls = [r for r in sel if not r["is_control"]], [r for r in sel if r["is_control"]]
    tp = [r for r in cases if r.get("verdict", "ok") != "ok"]
    fp = sum(1 for r in controls if r.get("verdict", "ok") != "ok")
    secs = [float(r["seconds"]) for r in sel if "verdict" in r]
    return {"split": split, "cases": len(cases), "controls": len(controls), "tp": len(tp), "fp": fp,
            "precision": round(len(tp) / (len(tp) + fp), 3) if tp or fp else None,
            "recall": round(len(tp) / len(cases), 3) if cases else None,
            "false_alarms": round(fp / len(controls), 3) if controls else None,
            "verdict_match": round(sum(1 for r in tp if r["verdict"] == bucket(r)) / len(tp), 3) if tp else None,
            "verdicts": {v: (sum(1 for r in tp if bucket(r) == v), sum(1 for r in cases if bucket(r) == v),
                             sum(1 for r in controls if r.get("verdict") == v)) for v in VERDICTS},
            "timeouts": sum(1 for r in sel if r.get("timeout")), "errors": len(every) - len(sel),
            "first_error": next((str(r["error"]) for r in every if r.get("error")), ""),
            "seconds_median": round(statistics.median(secs), 1) if secs else None}


def f3(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:.3f}"


def table(ms: list[dict[str, Any]], chat_only: int) -> list[str]:
    """The run's lines: per split and per verdict, phase 2 beside phase 1, the seconds, and the bar."""
    out = ["taste judge: phase 2 (what the turn read) beside phase 1 (the chat alone)", "",
           f"{'split':<9} {'cases':>5} {'ctrls':>5}   {'prec':>5} {'recall':>6} {'false alarms':>12}   "
           f"{'phase 1:':>8} {'prec':>5} {'recall':>6} {'false alarms':>12}"]
    for m in ms:
        p1 = PHASE1.get(m["split"], {})
        out.append(f"{m['split']:<9} {m['cases']:>5} {m['controls']:>5}   {f3(m['precision']):>5} {f3(m['recall']):>6} "
                   f"{f3(m['false_alarms']):>12}   {'':>8} {f3(p1.get('precision')):>5} {f3(p1.get('recall')):>6} {f3(p1.get('false_alarms')):>12}")
    a, nc = ms[0], ms[0]["controls"]
    if not a["cases"] + nc:
        return out + ["", f"no numbers: every call failed ({a['errors']}), the first: {a['first_error']}"]
    out += ["", f"{'verdict (all)':<18} {'caught':>9} {'false alarms':>12}   {'phase 1:':>8} {'caught':>9} {'false alarms':>12}"]
    for v in VERDICTS:
        (c, n, fa), (c1, n1, fa1) = a["verdicts"][v], PHASE1["verdicts"][v]
        out.append(f"{v:<18} {f'{c} of {n}':>9} {f'{fa} of {nc}':>12}   {'':>8} {f'{c1} of {n1}':>9} {f'{fa1} of 74':>12}")
    out += ["", f"median judge seconds: {a['seconds_median']} (phase 1: {PHASE1['seconds_median']}); timeouts {a['timeouts']}, "
                f"errors {a['errors']}; verdict match on the catches {f3(a['verdict_match'])}; judged on the chat alone (no turn in the ledger): {chat_only}"]
    ok = a["recall"] is not None and a["recall"] >= BAR_RECALL and a["false_alarms"] is not None and a["false_alarms"] < BAR_FALSE_ALARMS
    out.append(f"bar for a gate (printed, not enforced): recall at least 5 of 7 ({BAR_RECALL:.3f}) on the cases and false alarms under "
               f"1 in 10 on the controls; this run: recall {f3(a['recall'])}, false alarms {f3(a['false_alarms'])}: "
               f"{'clears it' if ok else 'does not clear it'}")
    return out


def run(cases: Path, controls: Path, rubric: Path, ledger: Path | None, out: Path | None, model: str = "", effort: str = "medium",
        jobs: int = 4, cap: float = evidence.TASTE_CAP, n: int = 0, judge: Callable[[str, float], str] | None = None,
        claude: str = "") -> tuple[list[str], dict[str, Any]]:
    """Judge every row (or a stratified `n` of each); the lines to print and what goes to `out`."""
    rows = read_rows(cases, False) + read_rows(controls, True)
    rows = sample(rows, n) if n else rows
    chat_only = 0
    db = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) if ledger and ledger.is_file() else None
    for r in rows:
        if "reads" not in r:
            r["reads"] = reads_of(db, str(r.get("reply_id") or ""), stamp(r.get("reply_at"))) if db else None
            chat_only += r["reads"] is None
    if db:
        db.close()
    go = judge or evidence.taste_runner(rubric, model, effort, cwd=(out.parent if out else None), claude=claude)

    def one(r: dict[str, Any]) -> dict[str, Any]:
        rec = evidence.taste_judge(evidence.taste_prompt(r), go, cap)
        keep = {k: r.get(k) for k in ("id", "room", "split", "kind", "held", "is_control", "reply_id")}
        return {**keep, "reads": len(r["reads"]) if r["reads"] is not None else None,      # the judge's kind beside the label's
                **{"pred_kind" if k == "kind" else k: v for k, v in rec.items()}}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        done = list(ex.map(one, rows))
    ms = [score(done, s) for s in ("all", "heldout")]
    lines = table(ms, chat_only)
    report = {"model": model or "default", "effort": effort, "cap": cap, "rubric_chars": len(rubric.read_text()), "metrics": ms,
              "phase1": PHASE1, "bar": {"recall_at_least": round(BAR_RECALL, 3), "false_alarms_under": BAR_FALSE_ALARMS}, "rows": done}
    if out:
        out.write_text(json.dumps(report, indent=1))
    return lines, report
