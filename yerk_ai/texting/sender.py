"""The sender: when a text that may go goes, and whether it still should. A row new to `queued` (dry: `would-send`) draws
its delay once and stores it with `fire_at`, so a restart re-arms from the ledger and never draws again: the owner's word on a
card goes `send_delay` after it, whatever the hour; else the owner's first shot (B9) when the owner's side's newest row is their own and
younger than `first_shot_window`; else the sheet's pacing bucket, counted from their newest text (J37); a fire time in
`quiet` for a sheet that holds at night moves to the end of quiet hours plus a `morning` draw (J38). At `fire_at` the
thread is read again on the link and the text is handed only when every text it answers is still there unchanged and
nothing newer of the owner's (texting's own claimed sends aside) or of theirs is (J39); with the bridge away it waits, and past
`stale_minutes` it is dropped and listed once, never fired late. Each job is handed once (J41). A from-me row inside a
handed job's window claims it (J42): `auto`, not the owner's answer; all parts claimed, `sent`; unclaimed after the window,
`unseen`, never handed again. Dry claims nothing: its row stays `would-send`, `handed` saying the job file was written.
A newer decision for the person cancels the older text not yet gone; a stopped text already handed and unclaimed gets
one cancel job first (live)."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import words
from ..ledger import Ledger
from .bridge import BridgeDown, BridgeError, is_tapback, when_of
from .hands import DOWN, MAYBE, REFUSED, job, parts
from .intake import digest
from .people import People, Person, Sheet, load
from .writer import THREAD, bucket

log = logging.getLogger("yerk.texting")
WAITS = {"live": "queued", "dry": "would-send"}       # the state a text that may go waits in, by the mode
SIDE = ("owner", "auto")                               # the owner's side of a thread: their own texts and texting's sends


def gs_of(r: dict[str, Any], col: str = "inbound") -> list[str]:
    return [g for g in str(r.get(col) or "").split(",") if g]


def pair(v: Any, default: tuple[float, float]) -> tuple[float, float]:
    return (float(v[0]), float(v[1])) if isinstance(v, (list, tuple)) and len(v) == 2 else default


def minute(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def quiet_end(at: datetime, quiet: tuple[int, int]) -> datetime | None:
    """The end of the quiet hours `at` (local) falls in, or None outside them."""
    start, end, m = *quiet, at.hour * 60 + at.minute
    if not (start <= m < end if start <= end else m >= start or m < end):
        return None
    day = at.date() + timedelta(days=1 if start > end and m >= start else 0)
    return datetime(day.year, day.month, day.day, end // 60, end % 60).astimezone()


class Sender:
    def __init__(self, conf: dict[str, Any], file: Path, ledger: Ledger, thread: Callable[[str, int], Awaitable[list[dict[str, Any]]]],
                 hands: Any, edits: list[tuple[int, str]] | None = None, post: Callable[[str, str], Awaitable[Any]] | None = None,
                 to: str = "", clock: Callable[[], float] = time.time, rand: random.Random | None = None):
        self.mode, self.file, self.ledger, self.clock = str(conf.get("mode") or "off"), file, ledger, clock
        self.thread, self.hands, self.post, self.to = thread, hands, post, to
        self.edits = edits if edits is not None else []     # the holds' card edits (Holds.edits), made at their next pass
        self.stale, self.claim_s = float(conf.get("stale_minutes", 30)) * 60, float(conf.get("claim_seconds", 180))
        self.wait = WAITS.get(self.mode, "queued")
        self.first_shot, self.window = pair(conf.get("first_shot"), (150.0, 240.0)), float(conf.get("first_shot_window", 300))
        self.send_delay, self.morning = pair(conf.get("send_delay"), (0.0, 30.0)), pair(conf.get("morning"), (10.0, 90.0))
        q = conf.get("quiet") or ("23:30", "07:30")
        self.quiet = (minute(q[0]), minute(q[1]))
        self.rand = rand or random.Random()
        self.who: People | None = None
        self.cancels: list[tuple[int, str, float]] = []      # (row, handle, until): a cancel job owed to the Mac

    def draw(self, span: tuple[float, float]) -> float:
        return self.rand.uniform(*span)

    def newest(self, r: dict[str, Any]) -> float:
        """The own time of the newest text the row answers."""
        got = [self.ledger.inbound_get(g) or {} for g in str(r.get("inbound") or "").split(",") if g]
        return max((float(x.get("created") or x.get("received") or 0) for x in got), default=self.clock())

    def side(self, key: str, now: float) -> tuple[float, str] | None:
        """The newest row of the owner's side of the thread in the last `first_shot_window` (and 5 minutes): when and whose."""
        rows = [r for r in self.ledger.texting_day(key, now - max(self.window, 300.0), now + 1) if r.get("sender") in SIDE]
        return max(((float(r["t"]), str(r["sender"])) for r in rows), default=None)

    def arm(self, who: People, r: dict[str, Any]) -> None:
        """A row new to the wait: its delay drawn once and stored with `fire_at`."""
        rid, key, now = int(r["id"]), str(r["person"]), self.clock()
        sheet = who.sheets.get(key) or Sheet()
        if r.get("approval_id"):                       # released by the owner's word on a card
            delay = self.draw(self.send_delay)
            self.ledger.texting_set(rid, delay=round(delay, 3), fire_at=now + delay)
            return
        last = self.side(key, now)
        if last is not None and last[1] == "owner" and now - last[0] < self.window:
            delay = self.draw(self.first_shot)
        else:
            near = datetime.fromtimestamp(last[0]).astimezone() if last else None
            lo, hi = sheet.pacing.get(bucket(datetime.fromtimestamp(now).astimezone(), near)) or (1.0, 5.0)
            delay = self.draw((lo * 60, hi * 60))
        fire = max(now, self.newest(r) + delay)
        if sheet.night == "hold" and (end := quiet_end(datetime.fromtimestamp(fire).astimezone(), self.quiet)) is not None:
            fire = end.timestamp() + self.draw(self.morning) * 60
        self.ledger.texting_set(rid, delay=round(delay, 3), fire_at=fire)

    async def tick(self) -> None:
        """One pass: the reply list as it is now (J16); each waiting row armed once, then fired when due."""
        self.who = who = await asyncio.to_thread(load, self.file)
        for new in self.ledger.texting_in(self.wait):            # a newer decision takes over (supersede)
            for old in self.ledger.texting_rows(str(new["person"]), self.wait) if new["fire_at"] is None else []:
                if int(old["id"]) < int(new["id"]) and (self.mode == "live" or old["handed"] is None):
                    self.cancel(old, "a newer decision")
        await self.cancel_jobs()
        for r in self.ledger.texting_in(self.wait):
            if r["handed"] is not None:
                if self.mode == "live" and self.clock() > float(r["handed"]) + self.claim_s:     # never seen: never again
                    self.end(r, "unseen", words.TEXTING_SENT["unseen"])
                    self.card(r, words.TEXTING_SENT["unseen"])
                continue
            if (p := who.people.get(str(r["person"]))) is None:
                self.end(r, "dropped", words.TEXTING_UNLISTED)
            elif r["fire_at"] is None:
                self.arm(who, r)
            elif float(r["fire_at"]) <= self.clock():
                await self.fire(p, r)

    async def fire(self, p: Person, r: dict[str, Any]) -> None:
        if (now := self.clock()) - float(r["fire_at"]) > self.stale:          # never late
            said = words.TEXTING_STALE.format(first=p.first, at=f"{datetime.fromtimestamp(float(r['fire_at'])):%H:%M}",
                                              minutes=f"{self.stale / 60:g}")
            self.end(r, "dropped", said.replace("\n", "; "))
            if self.post is not None and self.to:
                await self.post(self.to, said)
            return
        try:
            rows = await self.thread(p.key, THREAD)
        except (BridgeDown, BridgeError, OSError) as e:
            log.info("texting: %s waits for the bridge: %s", p.key, e)
            return
        if (why := self.recheck(r, rows)) is None:
            await self.hand(r, now)
        elif why == "they changed it" and not r["claimed"]:     # decided again on their new words
            got = {str(x.get("guid")): x for x in rows}
            for g in gs_of(r):
                self.ledger.inbound_set(g, state="pending", digest=digest(str(got[g].get("text") or "")))
            self.ledger.texting_set(int(r["id"]), state="deciding", reason=why, delay=None, fire_at=None, approval_id=None)
            self.card(r, words.TEXTING_SENT[why])
        else:
            self.cancel(r, why)

    def recheck(self, r: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
        """Why the text may not go on the thread as read just now, or None: a text it answers gone or changed, a row of the
        owner's newer than the newest of them that is not texting's own claimed send (a tapback of his too), or a text of
        theirs newer than it."""
        gs, newest, got = gs_of(r), self.newest(r), {str(x.get("guid")): x for x in rows}
        for g in gs:
            if g not in got:
                return "they took it back"
            if (d := (self.ledger.inbound_get(g) or {}).get("digest")) and digest(str(got[g].get("text") or "")) != d:
                return "they changed it"
        newer = [x for x in rows if (w := when_of(x)) is not None and w.timestamp() > newest and str(x.get("guid")) not in gs]
        if any(x.get("is_from_me") is True and (self.ledger.inbound_get(str(x.get("guid"))) or {}).get("sender") != "auto" for x in newer):
            return "owner answered"
        return "they wrote again" if any(x.get("is_from_me") is not True and not is_tapback(x) for x in newer) else None

    async def hand(self, r: dict[str, Any], now: float) -> None:
        """The next part, once (J41; dry: every part, as files): `handed` is written before the hands run, so a stop in between
        never hands twice; only an ssh that could not connect, which wrote nothing, gives it back to the next pass."""
        rid, newest, done = int(r["id"]), self.newest(r), len(gs_of(r, "claimed"))
        self.ledger.texting_set(rid, handed=now)
        for part in range(done + 1, (parts(r) if self.mode == "dry" else done + 1) + 1):
            got = await self.hands.hand(f"yerk-{rid}-{part}", job(r, part, newest))
            if got in (DOWN, REFUSED):
                self.ledger.texting_set(rid, handed=None)
                if got == REFUSED:
                    self.end(r, "dropped", words.TEXTING_REFUSED)
                return
            if got == MAYBE:
                log.warning("texting: %s, row %d part %d may have gone; never handed again", r["person"], rid, part)
        if self.mode == "dry":
            self.card(r, words.TEXTING_SENT["dry"].format(at=f"{datetime.fromtimestamp(now):%H:%M}"))

    def claim(self, p: Person, msg: dict[str, Any], at: float | None) -> bool:
        """A from-me row new on the link (the intake asks): True when it is texting's own send, claiming the oldest handed,
        unclaimed job of the thread whose window [handed - 10 s, handed + claim_seconds] holds the row's own time, a
        tapback row only a tapback job and a text row only a text job. The last part claimed: `sent`; else the next part is
        due a `burst_gap` draw from now (J40)."""
        t = self.clock() if at is None else at
        jobs = [r for r in self.ledger.texting_rows(p.key, "queued") if self.mode == "live" and r["handed"] is not None
                and (r["decision"] == "react") == is_tapback(msg) and float(r["handed"]) - 10 <= t <= float(r["handed"]) + self.claim_s]
        if not jobs:
            return False
        r = min(jobs, key=lambda r: float(r["handed"]))
        rid, got = int(r["id"]), ",".join([*gs_of(r, "claimed"), str(msg.get("guid") or "")])
        if len(got.split(",")) >= parts(r):
            self.ledger.texting_set(rid, claimed=got, state="sent", reason="")
            self.card(r, words.TEXTING_SENT["sent"].format(at=f"{datetime.fromtimestamp(t):%H:%M}"))
        else:
            sheet = (self.who.sheets.get(p.key) if self.who else None) or Sheet()
            gap = self.draw(pair(sheet.pacing.get("burst_gap"), (10.0, 35.0)))
            self.ledger.texting_set(rid, claimed=got, handed=None, delay=round(gap, 3), fire_at=self.clock() + gap)
        return True

    async def cancel_jobs(self) -> None:
        """Each cancel job owed, once: one that could not connect is tried again while its job may still be on the Mac."""
        owed, self.cancels = self.cancels, []
        for rid, to, until in owed:
            if await self.hands.hand(f"yerk-{rid}-cancel", {"to": to, "cancel": True, "queued": self.clock()}) == DOWN and self.clock() < until:
                self.cancels.append((rid, to, until))

    def cancel(self, r: dict[str, Any], why: str) -> None:
        """A text that may go, stopped: cancelled with why, and its card says so; handed and unclaimed in live, a cancel job
        for its handle is owed first (written at the start of the next pass)."""
        if self.mode == "live" and r["handed"] is not None:
            self.cancels.append((int(r["id"]), str(r["handle"]), float(r["handed"]) + self.claim_s))
        self.ledger.texting_set(int(r["id"]), state="cancelled", reason=why)
        if why == "owner answered":
            for g in gs_of(r):
                self.ledger.inbound_set(g, state="answered", note="the owner answered himself")
        self.card(r, words.TEXTING_SENT.get(why, why))

    def end(self, r: dict[str, Any], state: str, why: str) -> None:
        self.ledger.texting_set(int(r["id"]), state=state, reason=why)

    def card(self, r: dict[str, Any], said: str) -> None:
        if r.get("card"):                             # released by the owner's word: its card edited in its own room, nothing new posted
            self.edits.append((int(r["id"]), said))

    async def run(self, every: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        while True:
            try:
                await self.tick()
            except (OSError, ValueError) as e:
                log.warning("texting: the sender's pass did not run: %s", e)
            await every(1.0)
