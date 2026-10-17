"""Contract probes: real Claude Code processes, tiny prompts, raw event logs kept.

- hello: one scripted turn end to end, with its run ledger row
- fold: is a message written to stdin mid-turn answered inside the turn it lands in?
- resume: does a session started under one account resume under another?
"""
from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
from pathlib import Path
from typing import Any

from .accounts import Account, from_dir
from .config import state_dir, table
from .engine import RoomProcess, RoomSpec, SessionBusy, Turn
from .ledger import Ledger
from .lifted.classify import FailureTable

HELLO = "Say hello in five words or fewer."
FOLD_1 = ("Use the Bash tool to run `sleep 8`. When it finishes, run `sleep 8` a second time as a "
          "separate call. Then reply with the word DONE.")
FOLD_2 = "Also, what is 2+2? Put the digit in the same reply."
WORDS = ["lantern", "cobalt", "juniper", "marble", "saffron", "harbor", "quartz", "tundra", "velvet", "ember"]


class Env:
    def __init__(self, a: argparse.Namespace):
        self.state = state_dir(a.state)
        self.logs = Path(a.logs).expanduser() if a.logs else self.state / "probes"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.state / "ledger.sqlite")
        self.failures = FailureTable.load(table("failures.toml"))
        self.model = a.model

    def room(self, spec: RoomSpec, account: Account, log: str) -> RoomProcess:
        return RoomProcess(spec, account, self.state, ledger=self.ledger, failures=self.failures,
                           log=self.logs / log)


def scratch(env: "Env", tag: str) -> Path:
    """An empty working folder under the state folder, so no project files are discovered."""
    (env.state / "scratch").mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{tag}-", dir=env.state / "scratch"))


def summary(turn: Turn) -> dict[str, Any]:
    return {"outcome": turn.outcome, "reply": turn.text[:200], "first_visible_ms": turn.ms(turn.first_visible),
            "end_ms": turn.ms(turn.ended), "models": turn.models, "fallback_model": turn.fallback_model,
            "failure": turn.failure.cls if turn.failure else None, "tools": turn.tools}


def hello(a: argparse.Namespace) -> dict[str, Any]:
    env = Env(a)
    spec = RoomSpec(room="probe-hello", model=env.model, cwd=scratch(env, "hello"))
    with env.room(spec, from_dir(a.config_dir), "hello.jsonl") as rp:
        sid = rp.start(fresh=True)
        turn = rp.ask(HELLO, timeout=180)
    return {"ok": turn.outcome == "ok", "session_id": sid, **summary(turn), "ledger_row": env.ledger.rows(1)[0]}


def timeline(log: Path) -> dict[str, Any]:
    """Seconds from the first send for every send, replay, tool start, tool result and result."""
    rows = [json.loads(line) for line in log.open()]
    t0 = next(r["t"] for r in rows if "sent" in r)
    marks: list[tuple[float, str]] = []
    results: list[dict[str, Any]] = []
    for r in rows:
        t, e = round(r["t"] - t0, 2), r.get("e") or {}
        if "sent" in r:
            marks.append((t, "sent: " + r["sent"][:40]))
        elif e.get("type") == "user":
            content = (e.get("message") or {}).get("content")
            if e.get("isReplay"):
                marks.append((t, "replayed: " + (content if isinstance(content, str) else json.dumps(content))[:40]))
            elif isinstance(content, list) and any(c.get("type") == "tool_result" for c in content):
                marks.append((t, "tool result"))
            else:
                marks.append((t, "user event, synthetic=" + str(e.get("isSynthetic"))))
        elif e.get("type") == "stream_event" and (e.get("event") or {}).get("type") == "content_block_start":
            block = (e["event"].get("content_block") or {})
            if block.get("type") == "tool_use":
                marks.append((t, "tool start: " + str(block.get("name"))))
        elif e.get("type") == "system" and e.get("subtype") in ("model_refusal_fallback", "informational"):
            marks.append((t, "system " + str(e.get("subtype"))))
        elif e.get("type") == "result":
            results.append({"t": t, "text": str(e.get("result") or "")[:120], "num_turns": e.get("num_turns"),
                            "queued_turn_count": e.get("queued_turn_count"), "result_index": e.get("result_index"),
                            "models": list((e.get("modelUsage") or {}).keys())})
            marks.append((t, "RESULT " + str(e.get("result") or "")[:60].replace("\n", " ")))
    return {"marks": marks, "results": results}


def fold(a: argparse.Namespace) -> dict[str, Any]:
    env = Env(a)
    reps = []
    for i in range(a.reps):
        name = f"fold-{i + 1}.jsonl"
        spec = RoomSpec(room=f"probe-fold-{i + 1}", model=env.model, cwd=scratch(env, "fold"),
                        tools=["Bash"], allow=["Bash(sleep *)"], settings=a.fold_settings or {})
        with env.room(spec, from_dir(a.config_dir), name) as rp:
            rp.start(fresh=True)
            t1 = rp.send(FOLD_1)
            time.sleep(a.delay)
            if a.on_second:
                a.on_second(FOLD_2)
            else:
                rp.send(FOLD_2)
            first = rp.read_turn(t1, timeout=240)
            # a trailing turn, if Claude Code runs one, starts by itself: wait for it
            second = rp.read_turn(first.ended or time.time(), timeout=a.trailing_wait, record=False)
            if second.result:
                rp.record(second)
        tl = timeline(env.logs / name)
        res = tl["results"]
        answered_first = bool(res) and "4" in res[0]["text"]
        verdict = ("folded" if len(res) == 1 and answered_first else
                   "trailing" if len(res) >= 2 and not answered_first and "4" in res[1]["text"] else
                   "folded+extra" if len(res) >= 2 and answered_first else "unclear")
        reps.append({"rep": i + 1, "verdict": verdict, "log": str(env.logs / name), **tl})
    verdicts = [r["verdict"] for r in reps]
    return {"ok": all(v == "folded" for v in verdicts), "verdicts": verdicts, "reps": reps}


def ask_word(env: Env, spec: RoomSpec, account: Account, resume: str, variant: str, word: str) -> dict[str, Any]:
    with env.room(spec, account, f"resume-{variant}.jsonl") as rp:
        try:
            rp.start(resume=resume)
        except SessionBusy:
            return {"variant": variant, "ok": False, "note": "session busy"}
        turn = rp.ask("What was the code word I gave you? Reply with it only.", timeout=180)
        tail = rp.stderr_tail(400).strip()
    return {"variant": variant, "ok": word in turn.text, **summary(turn), "stderr_tail": tail[-300:]}


def resume(a: argparse.Namespace) -> dict[str, Any]:
    env = Env(a)
    word = f"{random.choice(WORDS)}-{random.randint(100, 999)}"
    src, dst = from_dir(a.config_dir), from_dir(a.to_config_dir)
    spec = RoomSpec(room="probe-resume", model=env.model, cwd=scratch(env, "resume"))
    with env.room(spec, src, "resume-0-start.jsonl") as rp:
        sid = rp.start(fresh=True)
        first = rp.ask(f"Remember this code word: {word}. Reply only OK.", timeout=180)
    found = sorted(src.config_dir.glob(f"projects/*/{sid}.jsonl"))
    out: dict[str, Any] = {"word": word, "session_id": sid, "start": summary(first),
                           "transcript_in_source": bool(found), "attempts": []}
    attempts: list[dict[str, Any]] = out["attempts"]
    attempts.append(ask_word(env, spec, dst, sid, "plain", word))
    if not attempts[-1]["ok"] and found:
        project = found[0].parent
        link = dst.config_dir / "projects" / project.name
        if link.exists() or link.is_symlink() or not link.parent.is_dir():
            attempts.append({"variant": "shared-folder", "ok": False, "note": "target folder exists or has no projects folder; not touched"})
        else:
            link.symlink_to(project, target_is_directory=True)
            try:
                attempts.append(ask_word(env, spec, dst, sid, "shared-folder", word))
            finally:
                link.unlink()
            attempts[-1]["link_removed"] = not (link.exists() or link.is_symlink())
        attempts.append(ask_word(env, spec, dst, str(found[0]), "transcript-path", word))
    out["transcript_in_target"] = bool(sorted(dst.config_dir.glob(f"projects/*/{sid}.jsonl")))
    out["ok"] = any(x["ok"] for x in attempts)
    out["works"] = [x["variant"] for x in attempts if x["ok"]]
    return out
