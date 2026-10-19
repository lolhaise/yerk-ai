"""Contract probes: real Claude Code processes, tiny prompts, raw event logs kept.

- hello: one scripted turn end to end, with its run ledger row
- fold: is a message written to stdin mid-turn answered inside the turn it lands in?
- resume: does a session started under one account resume under another?
"""
from __future__ import annotations

import argparse
import contextlib
import json
import random
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

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
    "bare: no tools, no context": {"context": False, "tools": []},
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
    setups = {n: s for n, s in SETUPS.items() if not a.only or n in a.only.split("|")}
    for rep in range(a.reps):
        for name, s in setups.items():
            spec = RoomSpec(room="probe-blocks", model=s.get("model", env.model), cwd=scratch(env, "blocks"),
                            tools=list(s.get("tools", room["tools"])), settings=s.get("settings", {}),
                            context_file=ctx if s.get("context", True) else None)
            rp = Variant(spec, from_dir(a.config_dir), env.state, ledger=env.ledger, failures=env.failures,
                         log=env.logs / f"blocks-{len(runs) + 1}.jsonl")
            rp.drop = list(s.get("drop", []))
            with rp:
                rp.start(fresh=True)
                turn = rp.ask(a.message or HELLO, timeout=180)
            runs.append({"run": len(runs) + 1, "setup": name, "rep": rep + 1, "blocked": turn.fallback_model is not None,
                         **summary(turn), "argv": [x for x in rp.argv() if not x.startswith("/")]})
    table = {n: f"{sum(r['blocked'] for r in runs if r['setup'] == n)} of {a.reps}" for n in setups}
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


DONE_STATES = ("answered", "failed", "abandoned")


def webhook_sender(url: str) -> Callable[[str], str]:
    """Post as the room's test webhook; returns the Discord message id (the inbound ledger key)."""
    import urllib.request

    def post(text: str) -> str:
        req = urllib.request.Request(url + "?wait=true", data=json.dumps({"content": text}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "yerk-smoke"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return str(json.loads(r.read())["id"])
    return post


def measure_script(layer: str, room: str) -> list[tuple[str, float | None]]:
    """50 short varied asks: 40 start a turn, 10 are sent at second 3 of a running turn. Each
    mid-turn message follows a turn of four separate tool calls, so that turn is still running."""
    L, also = layer, 3.0
    four = "as four separate calls, one at a time"
    return [
        ("Which units does the owner prefer? One line.", None),
        ("One-line opinion: paper books or e-readers?", None),
        (f"Read {L}/persona.md, {L}/rulebook.md, {L}/rooms/example-room.md and {L}/memory/open-loops.md {four}. "
         "Then say in one line which one is shortest.", None),
        ("Also, what is 7 times 6? Put it in the same reply.", also),
        (f"Use Glob to list the .md files under {L}/memory/daily, then Read the oldest one. What was packed? One line.", None),
        ("When is the weekly review? One line.", None),
        (f"Use the send tool to post the file {L}/memory/pinned.md to room {room} with the caption pinned memory, "
         "then reply SENT.", None),
        (f"Read {L}/memory/daily/2026-01-05.md, {L}/memory/daily/2026-01-06.md, {L}/memory/daily/2026-01-07.md and "
         f"{L}/memory/pinned.md {four}. Then name the daily note with the most entries, in one line.", None),
        ("Also, which day of the week is it today, per the clock line?", also),
        ("One line: coffee or tea for the morning?", None),
        ("Call the hold tool with wait_for backup-finished and reason waiting on the nightly backup, then reply in one line.",
         None),
        ("How often does the plant need water? One line.", None),
        (f"Grep for the word owner in {L}/persona.md, {L}/rulebook.md, {L}/rooms/example-room.md and {L}/memory/pinned.md "
         f"{four}. Then say in one line how many files matched.", None),
        ("And what is 12 plus 30? Same reply.", also),
        (f"Read {L}/memory/open-loops.md, then Read {L}/memory/daily/2026-01-05.md. When is the library card due? One line.",
         None),
        ("One-line opinion: is a standing desk worth it?", None),
        ("What time is it right now, per the clock line? One line.", None),
        (f"Read {L}/rooms/example-room.md, {L}/rooms/example-quiet.md, {L}/persona.md and {L}/rulebook.md {four}. "
         "Then tell me in one line which room is quieter.", None),
        ("Also: when was the plant last watered? Same reply.", also),
        ("What day of the week is the weekly review? One word.", None),
        (f"Use the send tool to post the file {L}/memory/open-loops.md to room {room} with the caption open loops, "
         "then reply SENT.", None),
        (f"Use Glob for {L}/rooms/*.md, then Read the first file it lists. One line on what that room is for.", None),
        (f"Read {L}/memory/daily/2026-01-07.md, {L}/memory/daily/2026-01-06.md, {L}/memory/daily/2026-01-05.md and "
         f"{L}/memory/open-loops.md {four}. Then say in one line what is on the shopping list.", None),
        ("Also, what is 9 times 9? Same reply.", also),
        ("One line: run in the morning or the evening?", None),
        ("Call the hold tool with wait_for report-ready and reason waiting on the weekly report, then reply in one line.",
         None),
        ("Is the owner on 12-hour or 24-hour time? One line.", None),
        (f"Grep for the word list in {L}/memory/daily/2026-01-05.md, {L}/memory/daily/2026-01-06.md, "
         f"{L}/memory/daily/2026-01-07.md and {L}/memory/open-loops.md {four}. Then one line: how many files matched?", None),
        ("And in one word: is today a weekday or the weekend?", also),
        (f"Read {L}/persona.md, then Grep {L}/rulebook.md for the word tables. One line: what does the rulebook say "
         "about tables?", None),
        ("One-line opinion: dishwasher or washing by hand?", None),
        ("How long since my previous message, per the clock line? One line.", None),
        (f"Read {L}/memory/pinned.md, {L}/memory/open-loops.md, {L}/rooms/example-room.md and "
         f"{L}/rooms/example-quiet.md {four}. Then one line: which file mentions the 20th?", None),
        ("Also, what is 100 minus 58? Same reply.", also),
        ("What did the owner ask to move this week? Check the daily notes. One line.", None),
        (f"Use the send tool to post the file {L}/rooms/example-room.md to room {room} with the caption room voice, "
         "then reply SENT.", None),
        (f"Use Glob to find the .md files under {L}/memory, then Read {L}/memory/pinned.md. One line: how many pinned "
         "facts are there?", None),
        (f"Read {L}/rulebook.md, {L}/persona.md, {L}/memory/pinned.md and {L}/memory/daily/2026-01-07.md {four}. "
         "Then one line: which file says to name the source of any number?", None),
        ("Also: which units does the owner prefer? Same reply.", also),
        ("One line: cats or dogs?", None),
        ("Call the hold tool with wait_for owner-reply and reason waiting on the owner's answer, then reply in one line.",
         None),
        ("When was the plant last watered, and when is it due next? One line.", None),
        (f"Grep for the word Sunday in {L}/memory/pinned.md, {L}/memory/daily/2026-01-07.md, {L}/rulebook.md and "
         f"{L}/persona.md {four}. Then one line: which files mention Sunday?", None),
        ("And what is 6 times 8? Same reply.", also),
        (f"Read {L}/memory/daily/2026-01-06.md, then Read {L}/memory/open-loops.md. One line: what should the owner "
         "read next, per the notes?", None),
        ("One-line opinion: is touch typing worth learning?", None),
        ("Say today's date in ISO form, per the clock line.", None),
        (f"Read {L}/rooms/example-quiet.md, {L}/rooms/example-room.md, {L}/memory/open-loops.md and {L}/memory/pinned.md "
         f"{four}. Then one line: which file is shortest?", None),
        ("Also, which day is the weekly review on? Same reply.", also),
        ("Last one: say OK.", None),
    ]


def short_script(layer: str, room: str) -> list[tuple[str, float | None]]:
    """30 short asks, one turn each, for message to first visible line: 20 answered from context
    or a line of thought, 10 that read one file."""
    L = layer
    asks = ["Hi. One short line back.", "What is 7 times 8? Just the number.", "One-line opinion: tabs or spaces?",
            "Which units does the owner prefer? One line.", "Say the weekday today, per the clock line.",
            "One word: is water wet?", "Name a colour that starts with B. One word.", "What is 144 divided by 12?",
            "One line: best time of day for a walk?", "When is the weekly review? One line.",
            f"Read {L}/memory/pinned.md and say how often the plant needs water. One line.",
            "Spell cat backwards. One word.", "One-line opinion: is breakfast the most important meal?",
            f"Read {L}/memory/open-loops.md and name the first open item. One line.", "What is 15 plus 27?",
            "One word: summer or winter?", f"Read {L}/rulebook.md and say what it says about tables. One line.",
            "How many days are in a leap year? Just the number.", "One line: what is a good name for a goldfish?",
            f"Read {L}/persona.md and describe the persona in five words.", "What is 9 squared? Just the number.",
            "One-line opinion: are standing desks worth it?", f"Read {L}/rooms/example-room.md and say what the room is "
            "for. One line.", "Say the time now, per the clock line. One line.", "One word: tea or coffee?",
            f"Read {L}/memory/daily/2026-01-05.md and name one thing in it. One line.", "What is 100 minus 37?",
            f"Read {L}/memory/daily/2026-01-06.md and name one thing in it. One line.",
            f"Read {L}/memory/daily/2026-01-07.md and name one thing in it. One line.", "Last one: say OK."]
    return [(x, None) for x in asks]


THINK_ASKS = [
    "How many weekdays (Monday to Friday) are there from 3 March 2027 to 17 April 2027 inclusive? One line.",
    "Three boxes are labelled apples, oranges and mixed, and every label is wrong. You may pick one fruit from one box. "
    "Which box do you pick from, and why? Two lines.",
    "What is the smallest positive integer that leaves remainder 1 when divided by 2, 3, 4, 5 and 6, and is divisible "
    "by 7? One line.",
    "A clock shows 3:40. What is the smaller angle between the hands, in degrees? One line.",
    "If you fold a square sheet in half twice and cut off one corner of the folded square, how many holes can appear "
    "when you unfold it, at most? One line."]


def think_script(layer: str, room: str) -> list[tuple[str, float | None]]:
    """5 asks that make the model think, for the thinking panel's comparison across settings."""
    return [(x, None) for x in THINK_ASKS]


def short(a: argparse.Namespace) -> dict[str, Any]:
    """30 short turns at a human pace: message to first visible line, the card's first edit."""
    out = drive(a, short_script, pace=[3.0, 4.0, 5.0])
    out["table"] = turn_table(Env(a), out["t0"], a.room, out["sent"])
    return out


def think(a: argparse.Namespace) -> dict[str, Any]:
    out = drive(a, think_script, pace=[3.0])
    out["table"] = turn_table(Env(a), out["t0"], a.room, out["sent"])
    return out


def smoke(a: argparse.Namespace) -> dict[str, Any]:
    """10 scripted messages, 2 of them mid-turn (`smoke_script`)."""
    out = drive(a, smoke_script, pace=[1.0])
    out["table"] = turn_table(Env(a), out["t0"], a.room, out["sent"])
    return out


def measure(a: argparse.Namespace) -> dict[str, Any]:
    """50 scripted messages, 10 of them at second 3 of a running turn, at a human pace; the
    per-turn table comes from the ledgers and the room's own event log."""
    out = drive(a, measure_script, pace=[3.0, 4.0, 5.0])
    out["table"] = turn_table(Env(a), out["t0"], a.room, out["sent"])
    return out


def drive(a: argparse.Namespace, script: Callable[[str, str], list[tuple[str, float | None]]],
          pace: list[float]) -> dict[str, Any]:
    """A script through either channel; replies are judged from the ledgers, never from what the
    channel printed. --via console: yerkd --console and stdin. --via discord: the room's test
    webhook (secrets/discord-test-webhook.url in the private folder), into a yerkd this starts,
    or with --attach into the one already running on the same state folder."""
    import os
    import subprocess
    import sys
    from . import context
    from .config import private_dir, rooms
    env = Env(a)
    priv = private_dir(a.private)
    layer = str(context.layer_dir(priv, rooms(priv)[a.room]))
    console = a.via == "console"
    attach = bool(getattr(a, "attach", False)) and not console
    proc: subprocess.Popen[str] | None = None
    errpath = env.logs / f"smoke-{a.via}-yerkd.stderr"
    if attach:
        st = json.loads((env.state / "yerkd.json").read_text())
        os.kill(int(st["pid"]), 0)                 # raises when no yerkd runs on this state folder
    else:
        argv = [sys.executable, "-m", "yerk_ai.daemon", "--private", str(priv), "--state", str(env.state)]
        argv += (["--console", a.room] if console else []) + (["--config-dir", a.config_dir] if a.only_account else [])
        err = open(errpath, "w")
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=open(env.logs / f"smoke-{a.via}-yerkd.stdout", "w"),
                                stderr=err, text=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    t0 = time.time()
    post: Callable[[str], str]
    if console:
        assert proc and proc.stdin
        stdin = proc.stdin

        def to_stdin(text: str) -> str:
            stdin.write(text + "\n")
            stdin.flush()
            return ""
        post = to_stdin
        time.sleep(1.0)
    else:
        post = webhook_sender((priv / "secrets" / "discord-test-webhook.url").read_text().strip())
        while not attach and "discord: connected as" not in errpath.read_text() and time.time() - t0 < 60:
            time.sleep(0.5)
    scripted = "owner" if console else "test-webhook"

    def mine(sender: str | None = scripted) -> list[dict[str, Any]]:
        rows = [r for r in env.ledger.rows(2000, "inbound") if r["room"] == a.room and r["received"] >= t0
                and (sender is None or r["sender"] == sender)]
        return sorted(rows, key=lambda r: r["received"])

    sent: list[dict[str, Any]] = []

    def wait_all(timeout: float = 240) -> None:
        end = time.time() + timeout
        while sum(r["state"] in DONE_STATES for r in mine()) < len(sent) and time.time() < end:
            time.sleep(0.3)

    for i, (text, delay) in enumerate(script(layer, a.room)):
        if delay is None:
            wait_all()
            if sent:
                time.sleep(pace[i % len(pace)])
        else:
            time.sleep(max(0.0, sent[-1]["t"] + delay - time.time()))
        prev = env.ledger.inbound_get(sent[-1]["id"]) if sent and sent[-1]["id"] else None
        t = round(time.time(), 3)
        sent.append({"t": t, "id": post(text), "text": text[:80], "mid_turn": delay is not None,
                     "prev_state_at_send": prev["state"] if prev else None})
    wait_all()
    rc = None
    if proc:
        if console and proc.stdin:
            proc.stdin.close()
        else:
            proc.terminate()
        rc = proc.wait(120)
    inbound = mine()
    turns = {r["id"]: r for r in env.ledger.rows(2000)}
    outbound = [r for r in env.ledger.rows(5000, "outbound") if r["room"] == a.room and r["sent"] >= t0]
    rows = [{k: r[k] for k in ("message_id", "state", "folded", "turn_id", "reply_ids")} for r in inbound]
    answered = sum(r["state"] == "answered" for r in inbound)
    folds = [(r, s) for r, s in zip(inbound, sent) if s["mid_turn"]]
    folded_ok = [bool(r["folded"]) and r["state"] == "answered" and any(
        x["turn_id"] == r["turn_id"] and not x["folded"] for x in inbound) for r, _ in folds]
    landed_mid_turn = [s["prev_state_at_send"] == "running" for _, s in folds]
    ids_match = console or [s["id"] for s in sent] == [r["message_id"] for r in inbound]
    owner = [] if console else mine("owner")
    return {"ok": answered == len(sent) == len(inbound) and all(folded_ok) and ids_match, "rc": rc, "via": a.via,
            "attach": attach, "t0": t0, "sent": sent, "inbound": rows, "answered": answered,
            "mid_turn_landed_while_running": landed_mid_turn, "mid_turn_folded_into_running_turn": folded_ok,
            "owner_messages": [{k: r[k] for k in ("message_id", "state", "folded", "turn_id")} for r in owner],
            "outbound": [{k: r[k] for k in ("kind", "message_id", "chars", "inbound", "error")} for r in outbound],
            "turns": [{k: turns[i][k] for k in ("id", "account", "outcome", "accepted", "models", "fallback_model",
                                                "started", "first_visible", "ended", "inbound")}
                      for i in sorted({r["turn_id"] for r in inbound + owner if r["turn_id"]}) if i in turns]}


def check(a: argparse.Namespace) -> dict[str, Any]:
    """Read the room back through the bot's REST, the room only: every message ledgered since
    --since carries the done mark and no eyes, every card is gone, every reply and file is there."""
    import urllib.error
    import urllib.request
    from .config import load_toml, private_dir, rooms
    env, priv = Env(a), private_dir(a.private)
    cid = str(rooms(priv)[a.room]["channel"]).partition(":")[2]
    d = load_toml(priv / "yerkd.toml")["discord"]
    token = (priv / d["token_file"]).read_text().strip()

    def get(path: str) -> Any:
        req = urllib.request.Request("https://discord.com/api/v10" + path,
                                     headers={"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (yerk-ai, 0.1)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    inbound = sorted([r for r in env.ledger.rows(5000, "inbound") if r["room"] == a.room and r["received"] >= a.since],
                     key=lambda r: int(r["message_id"]))
    outbound = [r for r in env.ledger.rows(20000, "outbound") if r["room"] == a.room and r["sent"] >= a.since]
    if not inbound:
        return {"ok": False, "error": "no inbound rows since --since"}
    seen: dict[str, Any] = {}
    after = str(int(inbound[0]["message_id"]) - 1)
    while True:
        try:
            page = get(f"/channels/{cid}/messages?limit=100&after={after}")
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"history read answered {e.code}"}
        if not page:
            break
        seen.update({m["id"]: m for m in page})
        after = max(page, key=lambda m: int(m["id"]))["id"]
        time.sleep(0.5)
    marks = {m["id"]: {r["emoji"]["name"] for r in m.get("reactions", []) if r.get("me")} for m in seen.values()}
    done, eyes = "\N{WHITE HEAVY CHECK MARK}", "\N{EYES}"
    msgs = [str(r["message_id"]) for r in inbound]
    kept = {str(o["message_id"]) for o in outbound if o["kind"] == "card-note"}    # a card kept as the notice
    cards = {str(o["message_id"]) for o in outbound if o["kind"] == "progress"} - kept
    posts = [str(o["message_id"]) for o in outbound if o["kind"] in ("reply", "notice", "send", "file") and o["message_id"]]
    edited = {str(o["message_id"]) for o in outbound if o["kind"] == "progress-edit"}
    out = {"messages": len(msgs), "with_done_mark": sum(done in marks.get(m, set()) for m in msgs),
           "with_eyes_left": sum(eyes in marks.get(m, set()) for m in msgs),
           "missing_in_room": [m for m in msgs if m not in seen], "cards": len(cards),
           "cards_edited": len(cards & edited), "cards_still_in_room": sorted(cards & set(seen)),
           "posts": len(posts), "posts_missing": [p for p in posts if p not in seen],
           "reply_ids_in_room": all(all(x in seen for x in str(r["reply_ids"] or "").split(",") if x) for r in inbound)}
    out["ok"] = (out["with_done_mark"] == len(msgs) and not out["with_eyes_left"] and not out["missing_in_room"]
                 and out["cards_edited"] == len(cards) and not out["cards_still_in_room"] and not out["posts_missing"]
                 and bool(out["reply_ids_in_room"]))
    return out


def proc_sample(pid: int) -> dict[str, Any] | None:
    """Resident memory (kB), threads and CPU seconds of one process, from /proc."""
    import os
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return None
    f = {k: v.split()[0] for k, _, v in (line.partition(":") for line in status.splitlines()) if v.strip()}
    tick = os.sysconf("SC_CLK_TCK")
    return {"pid": pid, "rss_kb": int(f.get("VmRSS", 0)), "threads": int(f.get("Threads", 0)),
            "cpu_s": (int(stat[11]) + int(stat[12])) / tick}


def descendants(pid: int) -> list[int]:
    kids: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            with contextlib.suppress(OSError, IndexError, ValueError):
                kids.setdefault(int((d / "stat").read_text().rsplit(")", 1)[1].split()[1]), []).append(int(d.name))
    out, todo = [], list(kids.get(pid, []))
    while todo:
        p = todo.pop()
        out.append(p)
        todo += kids.get(p, [])
    return out


def idle(a: argparse.Namespace) -> dict[str, Any]:
    """The idle baseline: the running yerkd and its room processes sampled every 30 s for
    --minutes, then `yerk room close`, then the daemon alone for --alone-minutes."""
    import subprocess
    import urllib.request
    env = Env(a)
    st = json.loads((env.state / "yerkd.json").read_text())
    pid = int(st["pid"])

    def unit() -> dict[str, dict[str, str]]:
        out = {}
        for u in [x for x in (a.unit, getattr(a, "compare_unit", None)) if x]:
            r = subprocess.run(["systemctl", "--user", "show", u, "-p", "MemoryCurrent", "-p", "CPUUsageNSec",
                                "-p", "TasksCurrent"], capture_output=True, text=True)
            out[u] = dict(x.split("=", 1) for x in r.stdout.split())
        return out

    def phase(name: str, minutes: float) -> dict[str, Any]:
        samples: list[dict[str, Any]] = []
        end = time.time() + minutes * 60
        while True:
            kids = [s for s in (proc_sample(p) for p in descendants(pid)) if s]
            samples.append({"t": round(time.time(), 1), "yerkd": proc_sample(pid), "children": kids, "unit": unit()})
            if time.time() >= end:
                break
            time.sleep(min(30.0, max(0.0, end - time.time())))
        first, last, wall = samples[0], samples[-1], samples[-1]["t"] - samples[0]["t"]

        def cpu(s: dict[str, Any] | None) -> float:
            return float(s["cpu_s"]) if s else 0.0
        kid_cpu = {k["pid"]: k for k in first["children"]}
        return {"phase": name, "wall_s": round(wall, 1), "samples": len(samples),
                "yerkd_rss_kb": [s["yerkd"]["rss_kb"] for s in (first, last)],
                "yerkd_threads": last["yerkd"]["threads"],
                "yerkd_cpu_s": round(cpu(last["yerkd"]) - cpu(first["yerkd"]), 3),
                "children": [{"pid": k["pid"], "rss_kb": k["rss_kb"], "threads": k["threads"],
                              "cpu_s": round(k["cpu_s"] - cpu(kid_cpu.get(k["pid"], {})), 3) if k["pid"] in kid_cpu
                              else None} for k in last["children"]],
                "unit_first": first["unit"], "unit_last": last["unit"], "raw": samples}
    out: dict[str, Any] = {"pid": pid, "room": a.room, "phases": [phase("room open, idle", a.minutes)]}
    req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/control", data=json.dumps({"close": a.room}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out["close"] = json.loads(r.read())
    time.sleep(5)
    out["phases"].append(phase("daemon alone", a.alone_minutes))
    turns = [r for r in env.ledger.rows(50) if r["room"] == a.room and (r["started"] or 0) >= out["phases"][0]["raw"][0]["t"]]
    out["turns_during"] = len(turns)
    out["ok"] = bool(out["close"].get("closed")) and not turns
    return out


def snowflake_time(mid: str) -> float | None:
    """When Discord made a message, from its id (milliseconds since 2015)."""
    return ((int(mid) >> 22) + 1420070400000) / 1000 if mid.isdigit() else None


def stream_marks(log: Path) -> list[tuple[float, str]]:
    """(time, kind) for every content block the model started, from a room's event log."""
    out = []
    if log.is_file():
        for line in log.open():
            r = json.loads(line)
            e = (r.get("e") or {}).get("event") or {}
            if e.get("type") == "content_block_start":
                out.append((float(r["t"]), str((e.get("content_block") or {}).get("type"))))
    return out


def turn_table(env: Env, t0: float, room: str, sent: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One row per message since t0: when it was sent (the probe's clock; an owner message by its
    Discord id), the turn that answered it, and from the message to: the card, its first visible
    line (the progress edit), the first content block the model started, and the reply."""
    sent_at = {s["id"]: s for s in sent or []}
    inbound = sorted([r for r in env.ledger.rows(5000, "inbound") if r["room"] == room and r["received"] >= t0],
                     key=lambda r: r["received"])
    turns = {r["id"]: r for r in env.ledger.rows(5000) if r["room"] == room}
    out_rows = sorted([r for r in env.ledger.rows(20000, "outbound") if r["room"] == room and r["sent"] >= t0],
                      key=lambda r: r["sent"])
    marks = stream_marks(env.state / "logs" / f"{room}.jsonl")
    table = []
    for n, m in enumerate(inbound, 1):
        mid, s = str(m["message_id"]), sent_at.get(str(m["message_id"]))
        t_msg = s["t"] if s else (snowflake_time(mid) or m["received"])
        tr = turns.get(m["turn_id"]) or {}
        reply = next((o for o in out_rows if o["kind"] in ("reply", "notice") and mid in str(o["inbound"] or "").split(",")),
                     None)
        end = reply["sent"] if reply else None

        def first(kind: str, rows: list[dict[str, Any]] = out_rows) -> float | None:
            o = next((o for o in rows if o["kind"] == kind and o["sent"] >= t_msg and (end is None or o["sent"] <= end)),
                     None)
            return o["sent"] if o else None
        block = next((t for t, _ in marks if t >= t_msg and (end is None or t <= end)), None)
        thinking = next((t for t, k in marks if k == "thinking" and t >= t_msg and (end is None or t <= end)), None)

        def since(t: float | None) -> float | None:
            return None if t is None else round(t - t_msg, 2)
        table.append({
            "n": n, "message_id": mid, "sender": m["sender"], "mid_turn": bool(s and s["mid_turn"]),
            "text": s["text"] if s else "(owner message)", "folded": bool(m["folded"]), "state": m["state"],
            "turn_id": m["turn_id"], "account": tr.get("account"), "models": tr.get("models"),
            "fallback_model": tr.get("fallback_model"), "output_chars": tr.get("output_chars"),
            "failure": tr.get("failure"), "accepted": tr.get("accepted"),
            "to_ledger_s": since(m["received"]), "to_card_s": since(first("progress")),
            "to_first_line_s": since(first("progress-edit")),
            "to_first_stream_visible_s": since(tr.get("first_visible")) if (tr.get("first_visible") or 0) >= t_msg else None,
            "to_first_block_s": since(block), "to_first_thinking_s": since(thinking), "to_reply_s": since(end),
            "discord_minus_local_s": round(snowflake_time(mid) - s["t"], 3) if s and snowflake_time(mid) else None,
        })
    return table


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
        with contextlib.suppress(OSError):     # an empty one is the plain try's own start (engine.py), holding nothing
            link.rmdir()
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


def imessage(a: argparse.Namespace) -> dict[str, Any]:
    """The inbound check (Layer 8): never sends anything. The newest rows of each iMessage room's chat,
    read from the bridge on a read-only connection, each looked up in the inbound and delivery
    ledgers; the result is a ledger row (health, account "imessage", source "probe") for the morning
    diff, and a lost text the machine room has not heard of yet is one line there. A row is lost only
    when two runs that read the chat in a row miss it: a Mac just awake, or a daemon just restarted,
    may not have replayed it yet. The first miss is noted by its guid's hash, the second is said."""
    import asyncio
    import hashlib
    from . import jobs, words
    from .channels.imessage import probe
    from .config import load_toml, private_dir, rooms
    priv, state = private_dir(getattr(a, "private", None)), state_dir(a.state)
    confs = rooms(priv)
    named = {n: str(c.get("channel", "")).partition(":")[2] for n, c in confs.items()
             if str(c.get("channel", "")).startswith("imessage:")}
    if not named:
        return {"ok": False, "error": 'no room has channel = "imessage:<chat identifier>" in rooms.toml'}
    conf = dict((load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}).get("imessage", {}))
    ledger = Ledger(state / "ledger.sqlite")
    out = asyncio.run(probe(named, conf, ledger, limit=int(a.rows), grace=float(a.grace)))

    def mark(g: dict[str, Any]) -> str:
        return hashlib.sha1(g["guid"].encode()).hexdigest()[:16]
    reached, misses, prev = "error" not in out, list(out.get("gaps", [])), ledger.health_last("imessage", "probe")
    try:
        before = set(json.loads(str(prev["note"] or "{}")).get("missed", [])) if prev else set()
    except (ValueError, AttributeError):
        before = set()
    lost = [g for g in misses if mark(g) in before]          # the run before missed it too
    ok = reached and not lost and not out.get("failure")
    ledger.health(account="imessage", source="probe", ok=1 if ok else 0,
                  failure=out.get("error") or ("lost" if lost else None) or out.get("failure"),
                  note=json.dumps({"rooms": out.get("rooms", {}), "reached": reached, "lost": len(lost),
                                   "marks": sorted({mark(g) for g in lost}),
                                   "missed": sorted({mark(g) for g in misses}) if reached else sorted(before)}))
    told = ledger.health_notes("imessage", "probe-gap")
    new = [g for g in lost if mark(g) not in told]
    machine = next((n for n, c in confs.items() if c.get("role") == "machine"), None)
    out["alerted"] = 0
    for room in sorted({g["room"] for g in new}):
        mine = [g for g in new if g["room"] == room]
        marks = sorted(mark(g) for g in mine)
        text = words.line(words.IMSG_GAP.format(n=len(mine), s="" if len(mine) == 1 else "s", room=room))
        if machine is None:
            print(f"probe imessage: no machine room for: {text}")
            continue
        try:
            sent = jobs.post_control(state, {"machine": {"room": machine, "text": text,
                                                         "key": "imsg-gap-" + hashlib.sha1("".join(marks).encode()).hexdigest()[:12]}}, 60)
        except (OSError, ValueError) as e:
            out["alert_error"] = f"yerkd did not answer ({type(e).__name__})"
            continue
        if sent.get("ok"):
            for m in marks:
                ledger.health(account="imessage", source="probe-gap", ok=0, note=m)
            out["alerted"] += len(mine)
    out["ok"], out["gaps"], out["first_misses"] = ok, len(lost), len(misses) - len(lost)   # counts only on stdout
    return out
