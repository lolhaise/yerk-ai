"""The writer: one model call (`claude -p`) for each closed burst of a person's texts, deciding how to answer it with what the owner's report room knows. A
closed burst is a `texting` row in state `deciding`; the pack carries that room's recall for their texts, and the worker server gives it that room's recall
and read_memory to dig further (workers.mcp_config). It answers reply (one to three texts), react, none or held, each
statement about the owner with its source. The code holds what it cannot trust: a statement with no source the pack has,
an always-held class their sheet does not open, no clean answer, a call past `decide_seconds`, no account free. A row
older than `catch_up_hours` when it is taken is missed, never decided. In dry mode a decision is only recorded: the row and one
line of <state>/texting/dry/YYYY-MM-DD.jsonl, never the day log, never a room. In live mode the sender (sender.py) takes it on."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import words
from ..accounts import Account, Cooldowns, account_env, pick, read_token
from ..ledger import Ledger
from ..lifted.classify import FailureTable
from ..workers import mcp_config
from .bridge import BridgeDown, BridgeError, is_tapback, when_of
from .people import load, sheet_text

log = logging.getLogger("yerk.texting")
PROMPT = Path(__file__).with_name("writer.md")
TOOLS = ["recall", "read_memory"]
THREAD = 30                  # the thread rows a pack carries, every chat of the person merged by time
LATE = 300.0                 # seconds between a text's own time and its arrival that mark it late
USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
SCHEMA: dict[str, Any] = {"type": "object", "required": ["decision", "parts", "statements", "why"], "properties": {
    "decision": {"enum": ["reply", "react", "none", "held"]},
    "parts": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    "react": {"enum": ["love", "like", ""]},
    "reply_to": {"type": "string"},
    "statements": {"type": "array", "items": {"type": "object", "required": ["n", "text", "source"], "properties": {
        "n": {"type": "integer"}, "text": {"type": "string"}, "source": {"type": "string"}}}},
    "classes": {"type": "array", "items": {"enum": list(words.TEXTING_CLASSES)}},
    "question": {"type": "string"},
    "why": {"type": "string"}}}


def held(reason: str) -> dict[str, Any]:
    return {"decision": "held", "parts": [], "react": "", "reply_to": "", "statements": [], "reason": reason}


def sourced(source: str, guids: set[str]) -> bool:
    """`thread:<guid>` of a row the pack holds, `memory:<file>:<line>` or `sheet:<key>`."""
    kind, _, what = source.partition(":")
    return bool(what) and (kind == "thread" and what in guids or kind == "sheet"
                           or kind == "memory" and what.rpartition(":")[2].isdigit() and bool(what.rpartition(":")[0]))


def answer(out: str, hold: Callable[[str], dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
    """A one-shot's stdout read as taste_judge reads its judge: its structured answer and its seconds and tokens, or None and
    `hold` with no clean answer (`error` when the call failed: its reason, for the account hop)."""
    try:
        d = json.loads(out)
        assert isinstance(d, dict)
    except (ValueError, AssertionError):
        return None, hold(words.TEXTING_NO_CLEAN)
    rec = {"seconds": round(float(d.get("duration_ms") or 0) / 1000, 2), "tokens": sum(int((d.get("usage") or {}).get(k) or 0) for k in USAGE)}
    if d.get("is_error"):
        return None, {**hold(words.TEXTING_NO_CLEAN), **rec, "error": str(d.get("result") or "no answer")[:300]}
    ans = d.get("structured_output")
    if not isinstance(ans, dict):
        try:
            ans = json.loads(d.get("result") or "")
        except ValueError:
            ans = None
    return ans, rec


def parse(out: str, guids: set[str], open_: tuple[str, ...] = ()) -> dict[str, Any]:
    """A call's stdout as a decision (`answer`). The model's own decision never stands over a statement with no source or a
    class the sheet keeps shut."""
    ans, rec = answer(out, held)
    if "reason" in rec:
        return rec
    parts = [str(p).strip() for p in (ans or {}).get("parts") or [] if str(p).strip()][:3]
    if not isinstance(ans, dict) or (decision := ans.get("decision")) not in ("reply", "react", "none", "held") \
            or decision == "reply" and not parts or decision == "react" and ans.get("react") not in ("love", "like"):
        return {**held(words.TEXTING_NO_CLEAN), **rec}
    said: list[dict[str, Any]] = [{"n": int(s.get("n") or i + 1), "text": str(s.get("text") or ""), "source": str(s.get("source") or "").strip()}
            for i, s in enumerate(ans.get("statements") or []) if isinstance(s, dict)]
    bare = [s["text"] for s in said if not sourced(s["source"], guids)]
    shut = [c for c in ans.get("classes") or [] if c in words.TEXTING_CLASSES and c not in open_]
    question = " ".join(str(ans.get("question") or "").split()) or (words.TEXTING_NO_SOURCE.format(what=bare[0]) if bare else "")
    decision = "held" if bare or shut else decision
    why = " ".join(str(ans.get("why") or "").split())
    reason = "; ".join(x for x in (question if decision == "held" else "",
                                   words.TEXTING_HELD_BY.format(classes=", ".join(shut)) if shut else "", why) if x)
    return {"decision": decision, "parts": parts, "react": str(ans.get("react") or "") if decision == "react" else "",
            "reply_to": str(ans.get("reply_to") or ""), "statements": said, "reason": reason, **rec}


def fence(name: str, body: str, tag: str) -> str:
    return f"=== {name} {tag} ===\n{body.strip() or 'none'}\n=== end {name} {tag} ==="


def line(r: dict[str, Any], late: set[str] | None = None) -> str:
    """One thread row: its guid (what a statement cites), its time, whose side (until texting sends, every row of the owner's
    side is `owner`), the text, and each file named as unseen."""
    when = when_of(r)
    files = [str(a.get("mime_type") or "a file") for a in r.get("attachments") or [] if isinstance(a, dict)]
    return " ".join(x for x in (str(r.get("guid")), f"{when.astimezone():%m-%d %H:%M}" if when else "?",
                                ("owner" if r.get("is_from_me") is True else "them") + ":", str(r.get("text") or "").strip(),
                                "(a tapback)" if is_tapback(r) else "", *(f"({f}, unseen)" for f in files),
                                "(arrived late)" if r.get("guid") in (late or set()) else "") if x)


def bucket(now: datetime, owner_last: datetime | None) -> str:
    """The sheet's pacing bucket for now: `active` within 5 minutes of the owner's last text, else weekday or weekend, day
    or evening (from 17:00)."""
    if owner_last is not None and (now - owner_last).total_seconds() < 300:
        return "active"
    return ("weekend" if now.weekday() >= 5 else "weekday") + ("_evening" if now.hour >= 17 else "_day")


class Writer:
    def __init__(self, conf: dict[str, Any], file: Path, state: Path, ledger: Ledger, report: str,
                 thread: Callable[[str, int], Awaitable[list[dict[str, Any]]]], recall: Callable[[str], str],
                 place: Callable[[datetime], str], accounts: list[Account], cooldowns: Cooldowns, failures: FailureTable,
                 model: str = "", claude: str = "", clock: Callable[[], float] = time.time, tools: list[str] | None = None):
        self.file, self.state, self.ledger, self.thread, self.recall, self.place = file, state, ledger, thread, recall, place
        self.accounts, self.cooldowns, self.failures, self.clock = accounts, cooldowns, failures, clock
        self.model, self.hours = str(conf.get("model") or model), float(conf.get("catch_up_hours", 6))
        self.cap, self.parallel = float(conf.get("decide_seconds", 240)), max(1, int(conf.get("parallel", 2)))
        self.fallback = str(conf.get("fallback") or "")       # the second model, when every account refuses the first (J33)
        self.cwd = state / "texting" / "writer-cwd"
        for d in (state / "texting", self.cwd):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        from ..engine import claude_bin
        tools = TOOLS if tools is None else tools          # the replay's writer: none (J31), and no MCP at all, as the check
        mcp = ["--mcp-config", str(mcp_config(state, report, tools, state / "texting" / "writer-mcp.json")), "--strict-mcp-config",
               "--allowedTools", ",".join(f"mcp__yerk__{t}" for t in tools)] if tools else ["--strict-mcp-config"]
        self.argv = [claude or claude_bin(), "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
                     "--system-prompt-file", str(PROMPT), "--tools", "", *mcp,
                     "--setting-sources", "", "--no-session-persistence", *(["--model", self.model] if self.model else []),
                     "--effort", str(conf.get("effort") or "high")]
        self.busy: set[int] = set()
        self.slots = asyncio.Semaphore(self.parallel)

    async def run(self, state: str = "deciding", step: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                  want: Callable[[dict[str, Any]], bool] = lambda r: True) -> None:
        """Every 2 s, each `deciding` row (or the check's: a `state` row it `want`s) not already taken, at most `parallel`
        at once; a stop leaves rows where they are, and the next start takes them under the same rules."""
        while True:
            try:
                who = await asyncio.to_thread(load, self.file, False)
                for key in who.people:
                    for r in self.ledger.texting_rows(key, state):
                        if want(r) and (rid := int(r["id"])) not in self.busy:
                            self.busy.add(rid)
                            asyncio.ensure_future(self.decide(r, step))
            except (OSError, ValueError) as e:
                log.warning("texting: the reply list did not load: %s", e)
            await asyncio.sleep(2.0)

    async def decide(self, row: dict[str, Any], step: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> None:
        async with self.slots:
            try:
                await (step or self._decide)(row)
            except (BridgeDown, BridgeError, OSError) as e:      # the row stays deciding: the next pass takes it again
                log.info("texting: %s not decided yet: %s", row["person"], e)
            except Exception:                                   # a fault: never taken again before a restart, so never a call a pass
                log.exception("texting: %s could not be decided", row["person"])
                return
            self.busy.discard(int(row["id"]))

    async def _decide(self, row: dict[str, Any]) -> None:
        rid, key, now = int(row["id"]), str(row["person"]), self.clock()
        gs = [g for g in str(row["inbound"] or "").split(",") if g]
        got = {g: self.ledger.inbound_get(g) or {} for g in gs}
        if now - min([float(r.get("created") or r.get("received") or now) for r in got.values()] or [now]) > self.hours * 3600:
            self.ledger.texting_set(rid, state="missed", reason=words.TEXTING_TOO_OLD.format(hours=f"{self.hours:g}"))
            for g in gs:
                self.ledger.inbound_set(g, state="missed")
            return
        who = await asyncio.to_thread(load, self.file)          # the reply list and the sheet as they are now, never a start's copy
        if (p := who.people.get(key)) is None:
            self.ledger.texting_set(rid, state="dropped", reason=words.TEXTING_UNLISTED)
            return
        rows = await self.last(key)
        late = {g for g, r in got.items() if r.get("created") and r.get("received") and r["received"] - r["created"] > LATE}
        theirs = [r for r in rows if r.get("guid") in got]
        mine = [w for r in rows if r.get("is_from_me") is True and (w := when_of(r))]
        at = datetime.fromtimestamp(now).astimezone()
        text = "\n".join(str(r.get("text") or "") for r in theirs)
        memory = await asyncio.to_thread(self.recall, "\n".join([text, *(str(r.get("text") or "") for r in rows[-3:] if r not in theirs)]))
        holds = [f"{r['decision']}: {r['reason']}" for r in self.ledger.texting_rows(key, "held")]
        tag = secrets.token_hex(4)
        try:
            base = (self.file.parent / "baseline.md").read_text()
        except OSError:
            base = ""
        pack = "\n\n".join([words.TEXTING_PACK.format(now=f"{at:%A %Y-%m-%d %H:%M}", bucket=bucket(at, max(mine, default=None)),
                                                      owner=who.owner, first=p.first, tag=tag),
                            fence("baseline", base, tag), fence("sheet", sheet_text(who.sheets[key]), tag),
                            fence("thread", "\n".join(line(r) for r in rows), tag),
                            fence("their texts", "\n".join(line(r, late) for r in theirs), tag), fence("memory", memory, tag),
                            fence("place", await asyncio.to_thread(self.place, at), tag), fence("held", "\n".join(holds), tag)])
        t0 = time.monotonic()
        d = await asyncio.to_thread(self.call, pack, {str(r.get("guid")) for r in rows}, who.sheets[key].open)
        d.setdefault("seconds", round(time.monotonic() - t0, 2))
        fields = {"decision": d["decision"], "parts": json.dumps(d["parts"]), "react": d["react"], "reply_to": d["reply_to"],
                  "statements": json.dumps(d["statements"]), "reason": d["reason"], "account": d.get("account", ""),
                  "seconds": d["seconds"], "tokens": d.get("tokens", 0), "mode": "dry"}
        self.ledger.texting_set(rid, state="decided", **fields)
        for g in gs:
            self.ledger.inbound_set(g, state="decided")
        dry(self.state, at, {"t": now, "id": rid, "person": key, "inbound": gs, **fields, "parts": d["parts"], "statements": d["statements"]})
        log.info("texting: %s decided %s (dry, %.0f s)", key, d["decision"], d["seconds"])

    async def last(self, key: str) -> list[dict[str, Any]]:
        """The person's last THREAD rows, every chat merged by time: the thread a pack and a check are handed."""
        return sorted(await self.thread(key, THREAD), key=lambda r: w.timestamp() if (w := when_of(r)) else 0.0)[-THREAD:]

    def call(self, pack: str, guids: set[str], open_: tuple[str, ...]) -> dict[str, Any]:
        return self.hop(self.argv, pack, self.cwd, self.cap, self.model, lambda out: parse(out, guids, open_), held)

    def hop(self, argv: list[str], stdin: str, cwd: Path, cap: float, model: str, read: Callable[[str], dict[str, Any]],
            hold: Callable[[str], dict[str, Any]], config: Path | None = None) -> dict[str, Any]:
        """One call (the writer's or the check's) on the first free account for `model`; a quota or rate-limit failure rests
        that account and hops, at most twice; past `cap` seconds, no clean answer; no account free, the same tries once more
        on `fallback` when it names another model, its answer's account then `<account> (<model>)`; still none, a hold that
        says until when. With `config` (the check's own empty folder, J28) the call runs from it in place of the account's
        folder, on the account's token: an account with none is passed over, and with none anywhere the call is held, never made."""
        pool = [a for a in self.accounts if read_token(a)[0]] if config else self.accounts
        if config and not pool:
            return hold(words.TEXTING_NO_TOKEN)
        for m in (model, self.fallback) if self.fallback not in ("", model) else (model,):
            if m != model:                  # the second model, named in --model in place of the first, or added
                argv = [*argv[:i], m, *argv[i + 1:]] if (i := argv.index("--model") + 1 if "--model" in argv else 0) else [*argv, "--model", m]
            for _ in range(3):
                a = pick(pool, self.cooldowns, model=m)
                if self.cooldowns.until(a.name, model=m) is not None:
                    break
                who = a.name if m == model else f"{a.name} ({m})"
                env = {**account_env(a), "CLAUDE_CONFIG_DIR": str(config)} if config else account_env(a)
                try:
                    out = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=cap, cwd=cwd, env=env).stdout
                except subprocess.TimeoutExpired:
                    return {**hold(words.TEXTING_NO_CLEAN), "account": who, "seconds": cap}
                d = read(out)
                if (err := d.pop("error", None)) is not None and (f := self.failures.classify(text=err)).action == "hop_account":
                    kind = f.kind or "auth"
                    self.cooldowns.fail(a.name, kind, lambda n: self.failures.cooldown_seconds(kind, n),
                                        model=m if kind == "rate_limit" else "")
                    continue
                return {**d, "account": who}
        free = min([u for a in pool if (u := self.cooldowns.until(a.name, model=model)) is not None] or [self.clock()])
        return hold(words.TEXTING_NO_ACCOUNT.format(at=f"{datetime.fromtimestamp(free):%H:%M}"))


def dry(state: Path, at: datetime, entry: dict[str, Any]) -> None:
    """One line of <state>/texting/dry/YYYY-MM-DD.jsonl (0600, in a 0700 folder): a decision or a check's verdict."""
    folder = state / "texting" / "dry"
    folder.mkdir(mode=0o700, exist_ok=True)
    with os.fdopen(os.open(folder / f"{at:%Y-%m-%d}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as f:
        f.write(json.dumps(entry) + "\n")
