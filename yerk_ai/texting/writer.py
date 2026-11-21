"""The writer: one model call (`claude -p`) for each closed burst of a person's texts, deciding how to answer it with what the owner's report room knows. A
closed burst is a `texting` row in state `deciding`; the pack carries that room's recall for their texts, and the worker server gives it that room's recall
and read_memory to dig further (workers.mcp_config). It answers reply (one to three texts), react, none or held, each
statement about the owner with its source. The code holds what it cannot trust: a statement with no source the pack has,
an always-held class their sheet does not open, no clean answer, a call past `decide_seconds`, no account free. A row
older than `catch_up_hours` when it is taken is missed, never decided. In dry mode a decision is only recorded: the row and one
line of <state>/texting/dry/YYYY-MM-DD.jsonl, never the day log, never a room. In live mode the sender (sender.py) takes it on."""
from __future__ import annotations

import asyncio
import base64
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
from .bridge import BridgeDown, BridgeError, attached, is_tapback, when_of
from .people import load, sheet_text

log = logging.getLogger("yerk.texting")
PROMPT = Path(__file__).with_name("writer.md")
TOOLS = ["recall", "read_memory"]
THREAD = 30                  # the thread rows a pack carries, every chat of the person merged by time
LATE = 300.0                 # seconds between a text's own time and its arrival that mark it late
USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
KINDS = (("image/", "a photo"), ("video/", "a video"), ("audio/", "a voice message"))
PHOTOS = ("image/heic", "image/heif", "image/jpeg", "image/png")      # the photos in their texts that are looked at
LOOKS, BIG, TAKE, FETCH = 4, 25 << 20, 4 << 20, 30.0     # photos a decision, the largest one, the bytes taken back, a fetch's seconds
REMOTE = (r'''p=$(cat); case "$p" in "~/"*) p="$HOME/${p#\~/}";; esac; case "$p" in "$HOME/Library/Messages/Attachments/"*) ;; '''
          r'''*) exit 3;; esac; case "$p" in *..*) exit 3;; esac; t=$(/usr/bin/mktemp -d) || exit 4; trap '/bin/rm -rf "$t"' EXIT; '''
          r'''/usr/bin/sips -s format jpeg -s formatOptions 80 -Z 1568 "$p" --out "$t/p.jpg" >/dev/null 2>&1 || exit 5; /bin/cat "$t/p.jpg"''')
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
    said: list[dict[str, Any]] = [{"n": i, "text": str(s.get("text") or ""), "source": str(s.get("source") or "").strip()}   # by place (J109)
            for i, s in enumerate((s for s in ans.get("statements") or [] if isinstance(s, dict)), 1)]
    bare = [s["text"] for s in said if not sourced(s["source"], guids)]
    shut = [c for c in ans.get("classes") or [] if c in words.TEXTING_CLASSES and c not in open_]
    question = " ".join(str(ans.get("question") or "").split()) or (words.TEXTING_NO_SOURCE.format(what=bare[0]) if bare else "")
    decision = "held" if bare or shut else decision
    why, by = " ".join(str(ans.get("why") or "").split()), words.TEXTING_HELD_BY.format(classes=", ".join(shut)) if shut else ""
    whole = "; ".join(x for x in (question if decision == "held" else "", by, why) if x)         # the ledger's `why`, never on a card
    code = words.TEXTING_NO_SOURCE.format(what=bare[0]) if bare else by or words.TEXTING_HELD_OWN     # a draft: the code's words alone (J110)
    reason = (code if parts else question or by or why) if decision == "held" else whole           # no draft: his question stands in for it
    return {"decision": decision, "parts": parts, "react": str(ans.get("react") or "") if decision == "react" else "",
            "reply_to": str(ans.get("reply_to") or ""), "statements": said, "reason": reason, "why": whole, **rec}


def fence(name: str, body: str, tag: str) -> str:
    return f"=== {name} {tag} ===\n{body.strip() or 'none'}\n=== end {name} {tag} ==="


def taps(ledger: Ledger, person: str) -> set[str]:
    """The row ids of texting's own tapbacks to `person`, as its claims keep them (sender.Sender.claim): a history names a
    tapback by its row id alone, never its guid (J97)."""
    return {x for r in ledger.texting_rows(person, "sent") if r["decision"] == "react" for x in str(r["claimed"] or "").split(",") if x}


def kind(a: dict[str, Any]) -> str:
    """A file in plain words, by its type: a photo, a video, a voice message, a sticker, else a file."""
    t = str(a.get("mime_type") or "")
    return "a sticker" if a.get("is_sticker") else next((w for p, w in KINDS if t.startswith(p)), "a file")


def line(r: dict[str, Any], late: set[str] | None = None, auto: set[str] | None = None,
         shown: dict[tuple[str, int], int] | None = None) -> str:
    """One thread row: its guid (what a statement cites), its time, whose side (`auto` for texting's own claimed sends, else
    `owner` or `them`), the text (never the mark a file leaves in it), its tapbacks, whose and which (`auto` for texting's
    own, by row id: `taps`), and each file named by its kind: unseen, or the number the call shows it by (`shown`, by the
    row's guid and the file's place in it)."""
    when, g, seen, own = when_of(r), str(r.get("guid")), shown or {}, auto or set()
    said = ", ".join(" ".join(str(x) for x in ("auto" if str(e.get("id")) in own else "owner" if e.get("is_from_me") is True else "them",
                                                e.get("type"), e.get("emoji")) if x) for e in r.get("reactions") or [])
    return " ".join(x for x in (g, f"{when.astimezone():%m-%d %H:%M}" if when else "?",
                                ("auto" if r.get("guid") in own else "owner" if r.get("is_from_me") is True else "them") + ":",
                                str(r.get("text") or "").replace("\ufffc", "").strip(), "(a tapback)" if is_tapback(r) else "",
                                f"(tapbacks: {said})" if said else "",
                                *(f"({kind(a)}, shown below as photo {seen[g, n]})" if (g, n) in seen else f"({kind(a)}, unseen)"
                                  for n, a in enumerate(attached(r))),
                                "(arrived late)" if r.get("guid") in (late or set()) else "") if x)


async def fetch(head: list[str], a: dict[str, Any]) -> bytes | str:
    """One photo, converted on the Mac by REMOTE with its path on ssh's stdin, never a command line: its JPEG, or why not in
    the ledger's words. A file's name is its sender's choice, so its path is checked here first and reaches no log."""
    p = str(a.get("original_path") or a.get("filename") or "")
    if not p.startswith(("~/Library/Messages/Attachments/", "/Users/")) or "/Library/Messages/Attachments/" not in p \
            or ".." in p or "\n" in p:
        return "refused"
    try:
        pr = await asyncio.create_subprocess_exec(*head, REMOTE, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return "not fetched"
    try:
        async with asyncio.timeout(FETCH):
            assert pr.stdin is not None and pr.stdout is not None
            pr.stdin.write(p.encode())
            pr.stdin.close()
            try:
                await pr.stdout.readexactly(TAKE + 1)
                return "too big"
            except asyncio.IncompleteReadError as e:
                out = e.partial
            ok = await pr.wait() == 0
    except (TimeoutError, OSError):
        return "not fetched"
    finally:
        if pr.returncode is None:
            pr.kill()
            await pr.wait()
    return out if ok and out.startswith(b"\xff\xd8\xff") else "not fetched"


def streamed(out: str) -> str:
    """A stream's last `result` line, which carries what the one JSON object does; "" with none: no clean answer."""
    for x in reversed(out.splitlines()):
        try:
            if json.loads(x).get("type") == "result":
                return x
        except (ValueError, AttributeError):
            pass
    return ""


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
                 model: str = "", claude: str = "", clock: Callable[[], float] = time.time, tools: list[str] | None = None,
                 head: list[str] | None = None):
        self.file, self.state, self.ledger, self.thread, self.recall, self.place = file, state, ledger, thread, recall, place
        self.head = list(head or [])           # ssh to the Mac, where a photo is converted: with none (the replay's), none is looked at
        self.accounts, self.cooldowns, self.failures, self.clock = accounts, cooldowns, failures, clock
        self.model, self.hours = str(conf.get("model") or model), float(conf.get("catch_up_hours", 6))
        self.mode = "live" if conf.get("mode") == "live" else "dry"      # what a decision says it was made in (J44)
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
        auto = taps(self.ledger, key) | {g for r in rows if r.get("is_from_me") is True
                                         and (self.ledger.inbound_get(g := str(r.get("guid"))) or {}).get("sender") == "auto"}
        at = datetime.fromtimestamp(now).astimezone()
        text = "\n".join(str(r.get("text") or "") for r in theirs)
        memory = await asyncio.to_thread(self.recall, "\n".join([text, *(str(r.get("text") or "") for r in rows[-3:] if r not in theirs)]))
        holds = [f"{r['decision']}: {r['why'] or r['reason']}" for r in self.ledger.texting_rows(key, "held")]
        shown, pics = await self.look(theirs)
        tag = secrets.token_hex(4)
        try:
            base = (self.file.parent / "baseline.md").read_text()
        except OSError:
            base = ""
        pack = "\n\n".join([words.TEXTING_PACK.format(now=f"{at:%A %Y-%m-%d %H:%M}", bucket=bucket(at, max(mine, default=None)),
                                                      owner=who.owner, first=p.first, tag=tag),
                            fence("baseline", base, tag), fence("sheet", sheet_text(who.sheets[key]), tag),
                            fence("thread", "\n".join(line(r, auto=auto, shown=shown) for r in rows), tag),
                            fence("their texts", "\n".join(line(r, late, auto, shown) for r in theirs), tag), fence("memory", memory, tag),
                            fence("place", await asyncio.to_thread(self.place, at), tag), fence("held", "\n".join(holds), tag)])
        t0 = time.monotonic()
        d = await asyncio.to_thread(self.call, pack, {str(r.get("guid")) for r in rows}, who.sheets[key].open, pics)
        d.setdefault("seconds", round(time.monotonic() - t0, 2))
        fields = {"decision": d["decision"], "parts": json.dumps(d["parts"]), "react": d["react"], "reply_to": d["reply_to"],
                  "statements": json.dumps(d["statements"]), "reason": d["reason"], "why": d.get("why", d["reason"]), "account": d.get("account", ""),
                  "seconds": d["seconds"], "tokens": d.get("tokens", 0), "mode": self.mode}
        was = (self.ledger.texting_get(rid) or {}).get("state")                             # the owner answered while the call ran (J96): never
        first = was != "deciding" or any((self.ledger.inbound_get(g) or {}).get("state") == "answered" for g in gs)   # undone, the record only
        shut = {"state": "cancelled", "reason": "owner answered"} if was == "deciding" else {}       # texts answered, the row unmoved: closed
        self.ledger.texting_set(rid, **({**{k: v for k, v in fields.items() if k != "reason"}, **shut} if first else {"state": "decided", **fields}))
        for g in () if first else gs:
            self.ledger.inbound_set(g, state="decided")
        if self.mode == "dry":
            dry(self.state, at, {"t": now, "id": rid, "person": key, "inbound": gs, **fields, "parts": d["parts"], "statements": d["statements"],
                                 **({"answered_first": True} if first else {})})
        log.info("texting: %s decided %s (%s, %.0f s)", key, d["decision"], self.mode, d["seconds"])

    async def last(self, key: str) -> list[dict[str, Any]]:
        """The person's last THREAD rows, every chat merged by time: the thread a pack and a check are handed."""
        return sorted(await self.thread(key, THREAD), key=lambda r: w.timestamp() if (w := when_of(r)) else 0.0)[-THREAD:]

    async def look(self, theirs: list[dict[str, Any]]) -> tuple[dict[tuple[str, int], int], list[bytes]]:
        """The photos in their texts the call is shown, by number: the newest LOOKS that fit, each fetched once, its bytes
        kept for the one call and never written; each one's ledger row `seen` or why not. None with no ssh head."""
        cand = [(str(r.get("guid")), n, a) for r in theirs if r.get("is_from_me") is not True for n, a in enumerate(attached(r))
                if a.get("mime_type") in PHOTOS and not a.get("missing") and not a.get("is_sticker")]
        fit = [c for c in cand if int(c[2].get("total_bytes") or 0) <= BIG][-LOOKS:] if self.head else []
        shown: dict[tuple[str, int], int] = {}
        pics: list[bytes] = []
        for g, n, a in cand:
            got = await fetch(self.head, a) if (g, n, a) in fit else "too big" if int(a.get("total_bytes") or 0) > BIG else ""
            if isinstance(got, bytes):
                pics.append(got)
                shown[g, n] = len(pics)
            if got:
                self.ledger.attachment_state(g, n, "seen" if isinstance(got, bytes) else got)
        return shown, pics

    def call(self, pack: str, guids: set[str], open_: tuple[str, ...], pics: list[bytes] | None = None) -> dict[str, Any]:
        """Today's one JSON answer, or with photos the stream form: one line on stdin, the pack and then each photo in its
        number's order, and the stream's last `result` line read as that answer, through the same hop."""
        if not pics:
            return self.hop(self.argv, pack, self.cwd, self.cap, self.model, lambda out: parse(out, guids, open_), held)
        i = self.argv.index("--output-format")
        argv = [*self.argv[:i], "--output-format", "stream-json", "--verbose", "--input-format", "stream-json", *self.argv[i + 2:]]
        shows = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(b).decode()}} for b in pics]
        stdin = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": pack}, *shows]}}) + "\n"
        return self.hop(argv, stdin, self.cwd, self.cap, self.model, lambda out: parse(streamed(out), guids, open_), held)

    def hop(self, argv: list[str], stdin: str, cwd: Path, cap: float, model: str, read: Callable[[str], dict[str, Any]],
            hold: Callable[[str], dict[str, Any]], config: Path | None = None) -> dict[str, Any]:
        """One call (the writer's or the check's) on the first free account for `model`; a quota or rate-limit failure rests
        that account and hops, each account once; past `cap` seconds, no clean answer; no account free, the same tries on `fallback`
        when it names another model, its account then `<account> (<model>)`, and still none, a hold that says until when. With
        `config` (J28), the check's own empty folder, the call runs from it on the account's token, passing over one with none."""
        pool = [a for a in self.accounts if read_token(a)[0]] if config else self.accounts
        if config and not pool:
            return hold(words.TEXTING_NO_TOKEN)
        for m in (model, self.fallback) if self.fallback not in ("", model) else (model,):
            if m != model:                  # the second model, named in --model in place of the first, or added
                argv = [*argv[:i], m, *argv[i + 1:]] if (i := argv.index("--model") + 1 if "--model" in argv else 0) else [*argv, "--model", m]
            for _ in pool:                  # each account once a model: the second model only when EVERY one refused (J33)
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
