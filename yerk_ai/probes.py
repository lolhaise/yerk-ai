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


# blocks: which setup piece goes with the first-message blocks. One piece toggled at a time
# against the test room's own setup; the same short message in a fresh session every run.
SETUPS: dict[str, dict[str, Any]] = {
    "room setup": {},
    "no context file": {"context": False},
    "default setting sources": {"drop": ["--setting-sources"], "settings": {"disableAllHooks": True}},
    "no --tools allowlist": {"drop": ["--tools"]},
    "model opus-5-5": {"model": "claude-opus-5-5"},
    "no --replay-user-messages": {"drop": ["--replay-user-messages"]},
}
TAKES_VALUE = {"--setting-sources", "--tools"}


class Variant(RoomProcess):
    drop: list[str] = []

    def argv(self) -> list[str]:
        out, skip = [], False
        for x in super().argv():
            if skip:
                skip = False
            elif x in self.drop:
                skip = x in TAKES_VALUE
            else:
                out.append(x)
        return out


def blocks(a: argparse.Namespace) -> dict[str, Any]:
    from . import context
    from .config import EXAMPLE_PRIVATE, budgets, rooms
    env = Env(a)
    room = rooms(EXAMPLE_PRIVATE)["example-room"]
    ctx = env.state / "blocks-context.md"
    ctx.write_text(context.appended_file(context.session_start(EXAMPLE_PRIVATE, room, budgets())))
    runs: list[dict[str, Any]] = []
    for rep in range(a.reps):
        for name, s in SETUPS.items():
            spec = RoomSpec(room="probe-blocks", model=s.get("model", env.model), cwd=scratch(env, "blocks"),
                            tools=list(room["tools"]), settings=s.get("settings", {}),
                            context_file=ctx if s.get("context", True) else None)
            rp = Variant(spec, from_dir(a.config_dir), env.state, ledger=env.ledger, failures=env.failures,
                         log=env.logs / f"blocks-{len(runs) + 1}.jsonl")
            rp.drop = list(s.get("drop", []))
            with rp:
                rp.start(fresh=True)
                turn = rp.ask(HELLO, timeout=180)
            runs.append({"run": len(runs) + 1, "setup": name, "rep": rep + 1, "blocked": turn.fallback_model is not None,
                         **summary(turn), "argv": [x for x in rp.argv() if not x.startswith("/")]})
    table = {n: f"{sum(r['blocked'] for r in runs if r['setup'] == n)} of {a.reps}" for n in SETUPS}
    return {"ok": True, "blocked_per_setup": table, "runs": runs}


# smoke: scripted messages into a room through `yerkd --console`, two of them written mid-turn.
# Each entry is (text, seconds after the previous message, or None to wait for its reply first).
def smoke_script(layer: str, room: str) -> list[tuple[str, float | None]]:
    return [
        ("Hi. One short line back.", None),
        ("What units does the owner prefer? Check the source file, then answer in one line.", None),
        (f"Read these three files one at a time, as separate calls: {layer}/persona.md, {layer}/rulebook.md, "
         f"{layer}/rooms/example-room.md. Then say in one line how many files you read.", None),
        ("Also, what is 7 times 6? Put the number in the same reply.", 1.5),
        (f"Use the send tool to post the word PING to room {room}, then reply with DONE.", None),
        ("Call the hold tool with wait_for test-event and reason scripted hold test, then reply in one line.", None),
        (f"Use Glob to list the .md files under {layer}/memory/daily, then Read each one as a separate call, "
         "then tell me the newest date in one line.", None),
        ("And what weekday is it now, per your clock line?", 1.5),
        ("What time is it right now, per the clock line? One line.", None),
        ("Last one: say OK.", None),
    ]


def smoke(a: argparse.Namespace) -> dict[str, Any]:
    import os
    import queue as q
    import subprocess
    import sys
    import threading
    from . import context
    from .config import private_dir, rooms
    env = Env(a)
    priv = private_dir(a.private)
    layer = str(context.layer_dir(priv, rooms(priv)[a.room]))
    argv = [sys.executable, "-m", "yerk_ai.daemon", "--console", a.room, "--private", str(priv),
            "--state", str(env.state)] + (["--config-dir", a.config_dir] if a.only_account else [])
    err = open(env.logs / "smoke-yerkd.stderr", "w")
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    replies: q.Queue[dict[str, Any]] = q.Queue()
    got: list[dict[str, Any]] = []

    def read() -> None:
        assert proc.stdout
        for line in proc.stdout:
            if line.strip().startswith("{"):
                replies.put(json.loads(line))

    threading.Thread(target=read, daemon=True).start()
    sent: list[dict[str, Any]] = []

    def covered() -> int:
        return sum(len(r.get("inbound", [])) for r in got if "reply" in r or "notice" in r)

    def wait_all(timeout: float = 240) -> None:
        end = time.time() + timeout
        while covered() < len(sent) and time.time() < end:
            try:
                got.append(replies.get(timeout=max(end - time.time(), 0.1)))
            except q.Empty:
                break

    time.sleep(1.0)
    assert proc.stdin
    for text, delay in smoke_script(layer, a.room):
        if delay is None:
            wait_all()
        else:
            time.sleep(delay)
        proc.stdin.write(text + "\n")
        proc.stdin.flush()
        sent.append({"t": round(time.time(), 3), "text": text[:80], "mid_turn": delay is not None})
    wait_all()
    proc.stdin.close()
    rc = proc.wait(120)
    err.close()
    while not replies.empty():
        got.append(replies.get())
    inbound = sorted(env.ledger.rows(50, "inbound"), key=lambda r: r["received"])[-len(sent):]
    turns = {r["id"]: r for r in env.ledger.rows(30)}
    rows = [{k: r[k] for k in ("message_id", "state", "folded", "turn_id", "reply_ids")} for r in inbound]
    answered = sum(r["state"] == "answered" for r in inbound)
    folds = [r for r, s in zip(inbound, sent) if s["mid_turn"]]
    folded_ok = [bool(r["folded"]) and r["state"] == "answered" and any(
        x["turn_id"] == r["turn_id"] and not x["folded"] for x in inbound) for r in folds]
    return {"ok": rc == 0 and answered == len(sent) and all(folded_ok), "rc": rc, "sent": sent, "replies": got,
            "inbound": rows, "answered": answered, "mid_turn_folded_into_running_turn": folded_ok,
            "turns": [{k: turns[i][k] for k in ("id", "account", "outcome", "accepted", "models", "fallback_model",
                                                "started", "first_visible", "ended", "inbound")}
                      for i in sorted({r["turn_id"] for r in inbound if r["turn_id"]})]}


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
