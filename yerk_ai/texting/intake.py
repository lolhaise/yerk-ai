"""The intake: each text or tapback the link hands over, written down once and sorted: a tapback, their text (`them`), texting's
own send (`auto`, when the sender claims it) or the owner's own (`owner`). Each goes into the inbound ledger keyed by the row's
guid, room `texting`, and the day log's private class. Their texts gather into one burst a person, closed by `burst_quiet` seconds
of quiet or `burst_cap` seconds after the first, into one pending decision (a `texting` row, `deciding`). A text older than
`catch_up_hours` when its burst closes is missed: listed once in the report room (in dry mode only in `dry_report`, never
in `report`). The owner's own text answers every text of theirs before it, open or pending. Nothing here decides or sends."""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import words
from ..ledger import Ledger
from .bridge import attached, is_tapback, when_of
from .people import People, Person

ROOM = "texting"             # the inbound rows' room and the day log's room id: unlisted there, so class private


def digest(text: str) -> str:
    """A text's words, hashed: what the sender's recheck compares with the thread at fire time."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass
class Burst:
    first: float
    last: float
    handle: str
    texts: list[tuple[str, float | None]] = field(default_factory=list)    # (guid, created)


class Intake:
    def __init__(self, conf: dict[str, Any], people: People, ledger: Ledger, capture: Callable[..., bool],
                 post: Callable[[str, str], Awaitable[Any]] | None = None, clock: Callable[[], float] = time.time):
        self.people, self.ledger, self.capture, self.post, self.clock = people, ledger, capture, post, clock
        self.mode = str(conf.get("mode") or "off")
        self.quiet, self.cap = float(conf.get("burst_quiet", 20)), float(conf.get("burst_cap", 120))
        self.hours = float(conf.get("catch_up_hours", 6))
        self.to = str(conf.get("dry_report") or "") if self.mode == "dry" else str(conf.get("report") or "")
        self.bursts: dict[str, Burst] = {}
        self.withdraw: Callable[[Person, float | None], None] | None = None   # the holds' (holds.Holds.withdraw), once made
        self.claim: Callable[[Person, dict[str, Any], float | None], bool] | None = None   # the sender's (sender.Sender.claim)
        self.stop: Callable[[dict[str, Any], str], None] | None = None     # the sender's (sender.Sender.cancel)

    def take(self, p: Person, msg: dict[str, Any]) -> str:
        """One row: what it was taken as (the link saves its cursor after this returns)."""
        guid, text, mine = str(msg.get("guid") or ""), str(msg.get("text") or ""), msg.get("is_from_me") is True
        if not guid:
            return "no guid"
        when = when_of(msg)
        at = when.timestamp() if when else None
        if not self.ledger.inbound_add(guid, ROOM, f"imessage:{p.key}", "owner" if mine else p.key, len(text), created=at):
            return "seen"
        auto = mine and self.claim is not None and self.claim(p, msg, at)     # texting's own send, seen: never his answer
        self.ledger.inbound_set(guid, digest=digest(text), **({"sender": "auto", "state": "filed", "note": "texting's own send"} if auto else {}))
        files = [(Path(str(a.get("transfer_name") or a.get("filename") or "file")).name, str(a.get("mime_type") or ""),
                  int(a.get("total_bytes") or 0)) for a in attached(msg)]
        for name, kind, size in files:            # what each is, never its bytes: nothing is fetched
            self.ledger.attachment(message_id=guid, room=ROOM, name=name, size=size, type=kind, state="not fetched")
        self.capture(ROOM, guid, "auto" if auto else "owner" if mine else "person", text, files, when=when, who="" if mine else p.key)
        if auto:
            return "auto"
        if is_tapback(msg):
            off = msg.get("is_reaction_add") is False
            self.ledger.inbound_set(guid, state="filed", note="a tapback taken away" if off else "the owner's own tapback" if mine else "tapback")
            if not mine or off:
                return "tapback"
            self.answered(p, at)                      # the owner's own, unclaimed: their answer, as their own text is (J97)
            return "owner"
        if mine:
            self.ledger.inbound_set(guid, state="filed", note="the owner's own text")
            self.answered(p, at)
            return "owner"
        now = self.clock()
        b = self.bursts.setdefault(p.key, Burst(now, now, str(msg.get("chat_identifier") or p.handles[0])))
        b.last, b.handle = now, str(msg.get("chat_identifier") or b.handle)
        b.texts.append((guid, at))
        return "them"

    def answered(self, p: Person, at: float | None) -> None:
        """The owner texted the thread: every text of theirs made before the owner's, in the open burst, a pending decision or a text
        waiting to go (dry: one whose job file is not written yet), is the owner's; a waiting one is stopped by the sender (J43)."""
        def before(c: float | None) -> bool:
            return at is None or c is None or c <= at
        if (b := self.bursts.get(p.key)) is not None:
            for g, _ in (x for x in b.texts if before(x[1])):
                self.ledger.inbound_set(g, state="answered", note="the owner answered himself")
            b.texts = [x for x in b.texts if not before(x[1])]
            if not b.texts:
                del self.bursts[p.key]
        for r in (r for state in ("deciding", "decided", "release-ready", "queued", "would-send") for r in self.ledger.texting_rows(p.key, state)):
            gs = str(r["inbound"]).split(",")
            if r["state"] == "would-send" and r["handed"] is not None:
                continue
            if all(before((self.ledger.inbound_get(g) or {}).get("created")) for g in gs):
                if r["state"] in ("queued", "would-send") and self.stop is not None:
                    self.stop(r, "owner answered")
                else:
                    self.ledger.texting_set(int(r["id"]), state="cancelled", reason="owner answered")
                for g in gs:
                    self.ledger.inbound_set(g, state="answered", note="the owner answered himself")
        if self.withdraw is not None:             # a card of theirs: withdrawn, and the card says the owner answered
            self.withdraw(p, at)

    def recover(self) -> None:
        """At a start: their texts a stop left in an open burst (received, never closed) gather again."""
        for r in self.ledger.inbound_open(ROOM):
            if (p := self.people.people.get(str(r["channel"]).removeprefix("imessage:"))) is not None and r["sender"] == p.key:
                now = self.clock()
                self.bursts.setdefault(p.key, Burst(now, now, p.handles[0])).texts.append((str(r["message_id"]), r["created"]))

    def tick(self) -> list[tuple[str, str]]:
        """Close every burst that is due; the lines to post, as (room, line)."""
        now, said = self.clock(), []
        for key, b in list(self.bursts.items()):
            if now - b.last < self.quiet and now - b.first < self.cap:
                continue
            del self.bursts[key]
            old = [x for x in b.texts if x[1] is not None and now - x[1] > self.hours * 3600]
            new = [x for x in b.texts if x not in old]
            for texts, state in ((old, "missed"), (new, "deciding")):
                if texts:
                    self.ledger.texting_add(person=key, handle=b.handle, inbound=",".join(g for g, _ in texts), state=state,
                                            mode=self.mode, reason=f"older than {self.hours:g} h" if state == "missed" else "")
                    for g, _ in texts:
                        self.ledger.inbound_set(g, state="missed" if state == "missed" else "pending")
            if old and self.to:
                at = datetime.fromtimestamp(min(c for _, c in old if c is not None)).strftime("%H:%M")
                said.append((self.to, words.TEXTING_MISSED.format(n=len(old), s="s" * (len(old) != 1), at=at,
                                                                  first=self.people.people[key].first, hours=f"{self.hours:g}")))
        return said

    async def run(self) -> None:
        self.recover()
        while True:
            for room, line in self.tick():
                if self.post is not None:
                    await self.post(room, line)
            await asyncio.sleep(1.0)
