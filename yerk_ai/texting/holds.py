"""The holds: a text that may not go on its own waits for the owner's word as one card, a message showing it, in the owner's texting room. It is filed
as a hold in that room's name (rule `text-thread`): a `held` decision, and a `release-ready` one of a person whose standing release is off. A card shows
only in `to`, the room the daemon allowed at its start (live: `report`; dry: `dry_report`, marked DRY; an owner room with no
people, else none). The owner's word, as a reply to the card (or a bare `send` or `skip` while that card is the one text hold
open and no other hold is), sends exactly the text the card shows (`queued`; dry: `would-send`), makes a new draft (`edit
<text>`) or drops it (`skip`). The owner's own text to the person withdraws it; a newer burst edits it in place; past `hold_hours`
it expires. Standing release (live, text-out on) queues a checked draft with no card, its `standing` gates row first. Nothing
here reaches the Mac: the sender takes `queued` rows."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import gates, texts, words
from ..ledger import Ledger
from .bridge import BridgeDown, BridgeError
from .check import never, precheck
from .hands import parts as sendable
from .people import People, Person, load
from .writer import dry

log = logging.getLogger("yerk.texting")
RULE = "text-thread"
CUT = 300                    # a text of theirs on a card, in characters
WORDS = ("send", "skip", "edit")


def payload(row: dict[str, Any]) -> dict[str, Any]:
    """What a send of this row carries, as its hold keys it: the handle and the exact texts, or the tapback and its text."""
    if row.get("decision") == "react":
        return {"to": row["handle"], "react": row["react"], "reply_to": row.get("reply_to") or ""}
    return {"to": row["handle"], "parts": json.loads(row.get("parts") or "[]")}


def key(row: dict[str, Any]) -> str:
    return gates.call_key(texts.TOOL, payload(row))


def quote(text: str) -> str:
    one = " ".join(str(text).split())
    return "> " + (one if len(one) <= CUT else one[:CUT - 3] + "...")


def card(first: str, at: str, quotes: list[str], row: dict[str, Any], dry_run: bool) -> str:
    """Who, when and held; their texts, one line each; the exact draft, a text a line (or the tapback); the words. Why it waits is
    not on the card: the row's reason and verdict keep it, and the texting tool's `why` reads it back when asked."""
    draft = ([words.TEXTING_CARD_TAPBACK.format(react=row["react"])] if row.get("decision") == "react"
             else [words.TEXTING_CARD_DRAFT, *json.loads(row.get("parts") or "[]")] if sendable(row) else [])    # no draft: it only asks
    return "\n".join([words.TEXTING_CARD.format(mark=words.TEXTING_CARD_MARK["dry" if dry_run else "live"], first=first, at=at,
                                                state=words.TEXTING_SHOWS["held"]),
                      *quotes, *draft, words.TEXTING_CARD_WORDS if draft else words.TEXTING_CARD_ASKS])


WENT = ("queued", "would-send", "sent", "unseen")      # the states of a row whose texts went, are going or may have gone


def went(row: dict[str, Any]) -> list[str]:
    """The texts a row sends, a line each, or its tapback: what stays on its card once it is answered by a send."""
    return ([words.TEXTING_CARD_TAPPED.format(react=row["react"])] if row.get("decision") == "react" else
            [str(x) for x in json.loads(row.get("parts") or "[]")]) if row.get("state") in WENT else []


def restate(shown: str, state: str, sent: list[str] | None = None) -> str:
    """A card once answered: its first line with `state` in place of held, then `sent`, the texts that went for it, and nothing
    else. Their texts, a draft that did not go and the words line are gone from it: the thread and the ledger keep them."""
    return "\n".join([f"{shown.partition(chr(10))[0].rsplit(' · ', 1)[0]} · {state}", *(sent or [])])


def gs_of(row: dict[str, Any]) -> list[str]:
    return [g for g in str(row.get("inbound") or "").split(",") if g]


class Holds:
    def __init__(self, conf: dict[str, Any], file: Path, state: Path, ledger: Ledger, to: str,
                 thread: Callable[[str], Awaitable[list[dict[str, Any]]]], show: Callable[[str, str, str], Awaitable[str]],
                 out: bool = True, window_s: float = 1800.0, word: str = "approve", clock: Callable[[], float] = time.time):
        self.mode, self.to, self.hours = str(conf.get("mode") or "off"), to, float(conf.get("hold_hours", 6))
        self.names = never(conf.get("never_say"))
        self.file, self.state, self.ledger, self.thread, self.show, self.out = file, state, ledger, thread, show, out
        self.window, self.approve, self.clock = window_s, word, clock
        self.edits: list[tuple[int, str]] = []      # (texting row id, what its card says now): edited at the next pass

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except (OSError, ValueError) as e:
                log.warning("texting: the holds pass did not run: %s", e)
            await asyncio.sleep(1.0)

    async def tick(self) -> None:
        """Expire what is past `hold_hours`, show each new held or release-ready decision (or let it go on standing release),
        then edit the cards whose state moved."""
        who = await asyncio.to_thread(load, self.file)      # the reply list and the sheets as they are now (J16)
        now = self.clock()
        for k, p in who.people.items():
            for r in self.ledger.texting_rows(k, "held"):
                if self.expired(r, now):
                    self.close(r, "expired")
            new = [*self.ledger.texting_rows(k, "release-ready"), *(r for r in self.ledger.texting_rows(k, "decided") if r["decision"] == "held")]
            for r in sorted(new, key=lambda r: int(r["id"])):
                await self.take(who, p, r)
        await self.flush()

    async def flush(self) -> None:
        while self.edits:
            rid, said = self.edits.pop(0)
            r = self.ledger.texting_get(rid)
            if r and r["card"] and r["shown"] and (self.ledger.texting_card(r["card"]) or {}).get("id") == rid:
                text = restate(r["shown"], said, went(r))
                if await self.show(self.to, text, r["card"]):
                    self.ledger.texting_set(rid, shown=text)

    def expired(self, r: dict[str, Any], now: float) -> bool:
        h = self.ledger.approval(int(r["approval_id"] or 0))
        return h is None or now - float(h["held"]) > self.hours * 3600

    def answered(self, gs: list[str]) -> bool:
        return any((self.ledger.inbound_get(g) or {}).get("state") == "answered" for g in gs)

    def at(self, gs: list[str]) -> str:
        ts = [float(x.get("created") or x.get("received") or 0) for g in gs if (x := self.ledger.inbound_get(g))]
        return datetime.fromtimestamp(max(ts, default=self.clock())).strftime("%H:%M")

    def close(self, r: dict[str, Any], state: str, at: float | None = None) -> None:
        """A held text answered: its state, the gates row that says so, and its card's edit at the next flush."""
        said = words.TEXTING_SHOWS[state].format(at=datetime.fromtimestamp(at or self.clock()).strftime("%H:%M"))
        self.ledger.texting_set(int(r["id"]), state=state, reason=said)
        self.ledger.gate(room=self.to or "texting", gate="text", decision=state, tool=texts.TOOL, rule=RULE,
                         detail=f"{r['person']}, texting row {r['id']}")
        self.edits.append((int(r["id"]), said))

    async def take(self, who: People, p: Person, r: dict[str, Any]) -> None:
        if self.answered(gs_of(r)):                   # the owner texted them after these (the intake marks them)
            self.ledger.texting_set(int(r["id"]), state="cancelled", reason="owner answered")
            return
        shown = [x for x in self.ledger.texting_rows(p.key, "held") if x["card"]]
        if r["state"] == "release-ready" and p.release and not shown and (self.mode == "dry" or self.out):
            self.standing(p, r)
        elif self.to:
            await self.hold(p, r, shown[-1] if shown else None)

    def standing(self, p: Person, r: dict[str, Any]) -> None:
        """Standing release: live, the `standing` gates row first, then queued; dry, would send, and the dry file says standing."""
        rid, now = int(r["id"]), self.clock()
        if self.mode == "live":
            self.ledger.gate(room=self.to or "texting", gate="text", decision="standing", tool=texts.TOOL, rule=RULE,
                             detail=f"{p.key}, check {rid}")
        state = "queued" if self.mode == "live" else "would-send"
        self.ledger.texting_set(rid, state=state)
        self.ledger.gate(room=self.to or "texting", gate="text", decision=state, tool=texts.TOOL, rule=RULE, detail=f"{p.key}, texting row {rid}")
        if self.mode == "dry":
            dry(self.state, datetime.fromtimestamp(now).astimezone(), {"t": now, "id": rid, "person": p.key, "state": state, "standing": True})

    async def hold(self, p: Person, r: dict[str, Any], old: dict[str, Any] | None, quotes: list[str] | None = None) -> None:
        """One card and its hold. With a card open for them (`old`), that card is edited to cover both and the old decision is
        superseded; nothing new is posted."""
        rid, was = int(r["id"]), r["state"]
        if quotes is None:
            try:
                got = {str(x.get("guid")): x for x in await self.thread(p.key)}
            except (BridgeDown, BridgeError, OSError) as e:      # shown at a later pass
                log.info("texting: %s not shown yet: %s", p.key, e)
                return
            quotes = [x for x in str((old or {}).get("shown") or "").split("\n") if x.startswith("> ")]
            quotes += [quote(got[g].get("text") or "") for g in gs_of(r) if g in got]
        r = self.ledger.texting_get(rid) or {}
        if r.get("state") != was or self.answered(gs_of(r)):     # moved while the thread was read
            return
        inbound = ",".join(dict.fromkeys([*(gs_of(old) if old else []), *gs_of(r)]))
        text = card(p.first, self.at(inbound.split(",")), quotes, r, self.mode == "dry")
        if old is not None:
            self.ledger.texting_set(int(old["id"]), state="superseded", reason=words.TEXTING_NEWER)
        gate_id = self.ledger.gate(room=self.to, gate="approval", decision="hold", tool=texts.TOOL, rule=RULE, detail=f"{p.key}, texting row {rid}")
        aid = self.ledger.hold_add(self.to, RULE, words.TEXTING_HOLD_WHAT.format(first=p.first), gate_id, call=key(r))
        self.ledger.texting_set(rid, state="held", approval_id=aid, inbound=inbound)      # before any wait: a word finds it held
        if not (mid := await self.show(self.to, text, old["card"] if old else "")):
            self.ledger.texting_set(rid, state=was)          # not shown: never sendable, shown at the next pass
            if old is not None:
                self.ledger.texting_set(int(old["id"]), state="held", reason=old["reason"])
            return
        self.ledger.texting_set(rid, card=mid, shown=text)
        if self.answered(gs_of(r)) and (now := self.ledger.texting_get(rid)) and now["state"] == "held":
            self.close(now, "withdrawn")                     # the owner texted them while it was being posted

    def withdraw(self, p: Person, at: float | None) -> None:
        """The owner's own text to `p` (the intake): each card of theirs whose texts are all older is withdrawn, its card edited to
        say the owner answered at the next flush; nothing new is posted and nothing is silenced."""
        for r in self.ledger.texting_rows(p.key, "held"):
            made = [(self.ledger.inbound_get(g) or {}).get("created") for g in gs_of(r)]
            if all(at is None or c is None or c <= at for c in made):
                self.close(r, "withdrawn", at)

    async def word(self, room: str, sender: str, text: str, ref: str | None) -> bool:
        """The owner's message in the card room's own channel, asked before any turn: True when it was the owner's word on a card and
        is answered here (no turn); False leaves it an ordinary message. A reply to a card counts when its first word is
        send (or the approval word), skip or edit; a message that is no reply only when it is exactly send or skip while
        exactly one text hold is open and no other hold of any kind is open in the room."""
        if not self.to or room != self.to or sender != "owner":
            return False
        said = text.strip().split(None, 1)
        first = said[0].casefold().strip(".,!:;") if said else ""
        if ref is not None:
            r = self.ledger.texting_card(ref)
            first = "send" if first == self.approve else first
            if r is None or first not in WORDS:
                return False
        else:
            now = self.clock()
            shown = [x for x in self.ledger.texting_held() if x["card"] and not self.expired(x, now)]
            if text.strip().casefold() not in ("send", "skip") or len(shown) != 1 or \
                    any(h["rule"] != RULE for h in self.ledger.holds_open(room, now - self.window)):
                return False
            r = shown[0]
        await self.act(int(r["id"]), first, said[1].strip() if len(said) > 1 else "")
        return True

    async def act(self, rid: int, word: str, rest: str) -> None:
        who = await asyncio.to_thread(load, self.file)       # the reply list and the sheet as they are when the word is read
        r, now = self.ledger.texting_get(rid) or {}, self.clock()
        if r.get("state") == "held" and self.expired(r, now):
            self.close(r, "expired")
            r = {**r, "state": "expired"}
        p = who.people.get(str(r.get("person")))
        if r.get("state") != "held" or p is None:
            said = str(r.get("reason") or "") if r.get("state") == "withdrawn" else words.TEXTING_SHOWS.get(str(r.get("state")), str(r.get("state")))
            line = words.TEXTING_GONE.format(state=said.format(at="") if p is not None else words.TEXTING_UNLISTED)
        elif word == "skip":
            self.close(r, "skipped")
            line = ""
        elif word == "edit":
            line = await self.edit(who, p, r, rest) if rest else words.TEXTING_EDIT_EMPTY
        elif not sendable(r):                                 # a card with no draft only asks: an empty job would stop the sender for everyone
            line = words.TEXTING_NO_DRAFT
        elif self.mode == "live" and not self.out:
            line = words.TEXTING_OUT_OFF
        else:
            line = self.send(r)
        await self.flush()
        if line:
            await self.show(self.to, line, "")

    def send(self, r: dict[str, Any]) -> str:
        """The text the card shows, once: its hold approved by id and released only for the key derived again from the
        stored text, then queued (dry: would send)."""
        rid, aid = int(r["id"]), int(r["approval_id"] or 0)
        self.ledger.hold_approve(aid, "owner")
        got = self.ledger.release(self.to, RULE, key(r), None, self.hours * 3600)
        if got is None or int(got["id"]) != aid:      # the row is not the text its card showed: never sent on that card's word
            return words.TEXTING_MOVED
        self.ledger.gate(room=self.to, gate="approval", decision="approved", tool=texts.TOOL, rule=RULE, detail=f"{r['person']}, texting row {rid}")
        state = "queued" if self.mode == "live" else "would-send"
        self.close(r, state)
        if self.mode == "dry":
            now = self.clock()
            dry(self.state, datetime.fromtimestamp(now).astimezone(), {"t": now, "id": rid, "person": r["person"], "state": state, "by": "word"})
        return ""

    async def edit(self, who: People, p: Person, r: dict[str, Any], text: str) -> str:
        """The owner's own words as a new draft: a new card and a new hold, the code's pre-check kept in the row's verdict and never enforced;
        nothing sent."""
        found = precheck([text], who.sheets[p.key].open, self.names)
        rid = self.ledger.texting_add(person=p.key, handle=r["handle"], inbound=r["inbound"], decision="reply", parts=json.dumps([text]),
                                      react="", reply_to="", statements="[]", verdict=json.dumps({"by": "owner", "precheck": found}),
                                      state="decided", reason="", mode=self.mode)
        self.close(r, "superseded")
        quotes = [x for x in str(r["shown"] or "").split("\n") if x.startswith("> ")]
        await self.hold(p, self.ledger.texting_get(rid) or {}, None, quotes)
        return ""
