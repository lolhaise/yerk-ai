"""The sender: when a text that may go goes, and whether it still should. A row new to `queued` (dry: `would-send`) draws
its delay once and stores it with `fire_at`, so a restart re-arms from the ledger and never draws again: the owner's word on a
card goes `send_delay` after it, whatever the hour; else the owner's first shot (B9) when the owner's side's newest row is their own and
younger than `first_shot_window`; else the sheet's pacing bucket, counted from their newest text (J37); a fire time in
`quiet` for a sheet that holds at night moves to the end of quiet hours plus a `morning` draw (J38)."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..ledger import Ledger
from .people import People, Sheet, load
from .writer import bucket

log = logging.getLogger("yerk.texting")
WAITS = {"live": "queued", "dry": "would-send"}       # the state a text that may go waits in, by the mode
SIDE = ("owner", "auto")                               # the owner's side of a thread: their own texts and texting's sends


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
    def __init__(self, conf: dict[str, Any], file: Path, ledger: Ledger, clock: Callable[[], float] = time.time,
                 rand: random.Random | None = None):
        self.mode, self.file, self.ledger, self.clock = str(conf.get("mode") or "off"), file, ledger, clock
        self.wait = WAITS.get(self.mode, "queued")
        self.first_shot, self.window = pair(conf.get("first_shot"), (150.0, 240.0)), float(conf.get("first_shot_window", 300))
        self.send_delay, self.morning = pair(conf.get("send_delay"), (0.0, 30.0)), pair(conf.get("morning"), (10.0, 90.0))
        q = conf.get("quiet") or ("23:30", "07:30")
        self.quiet = (minute(q[0]), minute(q[1]))
        self.rand = rand or random.Random()
        self.who: People | None = None

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
        """One pass: the reply list as it is now (J16), each waiting row armed once."""
        self.who = who = await asyncio.to_thread(load, self.file)
        for r in self.ledger.texting_in(self.wait):
            if r["fire_at"] is None and str(r["person"]) in who.people:
                self.arm(who, r)

    async def run(self, every: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        while True:
            try:
                await self.tick()
            except (OSError, ValueError) as e:
                log.warning("texting: the sender's pass did not run: %s", e)
            await every(1.0)
