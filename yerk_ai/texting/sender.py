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
one cancel job first (live). At a start in dry, what a live run left to send is dropped and listed once."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import words
from ..config import write_private
from ..ledger import Ledger
from .bridge import BridgeDown, BridgeError, is_tapback, when_of
from .hands import DOWN, MAYBE, REFUSED, job, parts
from .intake import digest
from .people import People, Person, Sheet, load
from .writer import THREAD, bucket, taps

log = logging.getLogger("yerk.texting")
WAITS = {"live": "queued", "dry": "would-send"}       # the state a text that may go waits in, by the mode
SIDE = ("owner", "auto")                               # the owner's side of a thread: their own texts and texting's sends


def gs_of(r: dict[str, Any], col: str = "inbound") -> list[str]:
    return [g for g in str(r.get(col) or "").split(",") if g]


def pair(v: Any, default: tuple[float, float]) -> tuple[float, float]:
    """[low, high], two numbers from 0 up; none set, `default`; anything else is refused (`yerk doctor` names it)."""
    if v is None:
        return default
    if not (isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
            and 0 <= v[0] <= v[1]):
        raise ValueError(f"{v!r} is not two numbers, low then high")
    return float(v[0]), float(v[1])


def switched_off(ledger: Ledger) -> list[dict[str, Any]]:
    """What a live run left to send, at a start that is not live: dropped, never handed."""
    rows = ledger.texting_in("queued")
    for r in rows:
        ledger.texting_set(int(r["id"]), state="dropped", reason="switched off")
    return rows


def quiet_of(v: Any) -> tuple[int, int]:
    """[start, end], local HH:MM, as minutes of the day; anything else is refused."""
    try:
        q = tuple(int(h) * 60 + int(m) for h, m in (str(x).split(":") for x in v))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{v!r} is not two HH:MM times") from e
    if len(q) != 2 or not all(0 <= m < 24 * 60 for m in q):
        raise ValueError(f"{v!r} is not two HH:MM times")
    return q[0], q[1]


def quiet_end(at: datetime, quiet: tuple[int, int]) -> datetime | None:
    """The end of the quiet hours `at` (local) falls in, or None outside them."""
    start, end, m = *quiet, at.hour * 60 + at.minute
    if not (start <= m < end if start <= end else m >= start or m < end):
        return None
    day = at.date() + timedelta(days=1 if start > end and m >= start else 0)
    return datetime(day.year, day.month, day.day, end // 60, end % 60).astimezone()


def eight(d: date) -> float:
    return datetime(d.year, d.month, d.day, 8).astimezone().timestamp()


def day_line(ledger: Ledger, mode: str, d: date) -> str:
    """The machine room's line for the day to `d`'s 08:00 from the one before (J61), from the ledger alone: counts, never a
    person, a thread or a text. The run: texting's own sends to one person in a row with no text of the owner's between (J60)."""
    texts, rows = ledger.texting_span(eight(d - timedelta(days=1)), eight(d))
    if not texts and not rows:
        return words.TEXTING_DAY_QUIET.format(mode=mode, day=f"{d:%a %b} {d.day}")
    sent, runs, longest = [r for r in rows if r["state"] == ("sent" if mode == "live" else "would-send") and r["handed"]], Counter[str](), 0
    for _, who, his in sorted([(float(r["handed"]), str(r["person"]), False) for r in sent]
                              + [(float(x["t"]), str(x["channel"]).removeprefix("imessage:"), True) for x in texts if x["sender"] == "owner"]):
        runs[who] = 0 if his else runs[who] + 1
        longest = max(longest, runs[who])
    why = Counter(str(r["reason"]) for r in rows if r["state"] == "cancelled")
    return words.TEXTING_DAY.format(mode=mode, day=f"{d:%a %b} {d.day}", got=sum(x["sender"] not in SIDE for x in texts),
                                    kinds=", ".join(f"{n} {k}" for k, n in sorted(Counter(str(r["decision"]) for r in rows if r["decision"]).items())) or "none",
                                    held=sum(bool(r["approval_id"]) for r in rows), sent=len(sent), out=words.TEXTING_SENT_AS[mode],
                                    first=why["owner answered"], again=why["they wrote again"] + why["a newer decision"],
                                    unseen=sum(r["state"] == "unseen" for r in rows), run=longest)


class Sender:
    def __init__(self, conf: dict[str, Any], file: Path, ledger: Ledger, thread: Callable[[str, int], Awaitable[list[dict[str, Any]]]],
                 hands: Any, edits: list[tuple[int, str]] | None = None, post: Callable[[str, str], Awaitable[Any]] | None = None,
                 to: str = "", clock: Callable[[], float] = time.time, rand: random.Random | None = None,
                 status: Callable[[dict[str, Any]], None] | None = None, out: bool = True, machine: Callable[[str], None] | None = None,
                 day_file: Path | None = None):
        self.mode, self.file, self.ledger, self.clock = str(conf.get("mode") or "off"), file, ledger, clock
        self.thread, self.hands, self.post, self.to, self.status, self.out = thread, hands, post, to, status, out
        self.machine, self.day_file = machine, day_file       # a line to the machine room (Daemon.to_machine); the last day said
        self.day: date | None = None
        self.edits = edits if edits is not None else []     # the holds' card edits (Holds.edits), made at their next pass
        self.stale, self.claim_s = float(conf.get("stale_minutes", 30)) * 60, float(conf.get("claim_seconds", 180))
        self.wait = WAITS.get(self.mode, "queued")
        self.first_shot, self.window = pair(conf.get("first_shot"), (150.0, 240.0)), float(conf.get("first_shot_window", 300))
        self.send_delay, self.morning = pair(conf.get("send_delay"), (0.0, 30.0)), pair(conf.get("morning"), (10.0, 90.0))
        self.quiet = quiet_of(conf.get("quiet", ("23:30", "07:30")))
        for k in ("first_shot_window", "claim_seconds", "stale_minutes"):
            if k in conf and not (isinstance(conf[k], (int, float)) and not isinstance(conf[k], bool) and conf[k] > 0):
                raise ValueError(f"{k} is not a number above 0")
        self.rand = rand or random.Random()
        self.who: People | None = None
        self.cancels: list[tuple[int, str, float]] = []      # (row, handle, until): a cancel job owed to the Mac
        self.said: dict[str, Any] = {}

    async def start(self) -> None:
        """At a start in dry: what a live run left to send is dropped and listed once, in the dry card room only. In live
        with text-out off in [gates] (`out`), the install's switch for texts, it is dropped too, with a log line."""
        if (self.mode == "dry" or not self.out) and (left := switched_off(self.ledger)):
            if self.mode != "dry":
                log.warning("texting: text-out is off in [gates]: %d text(s) left to send dropped, none sent", len(left))
            elif self.post is not None and self.to:
                await self.post(self.to, words.TEXTING_SWITCHED.format(n=len(left), s="s" * (len(left) != 1), mode=self.mode))

    def counts(self) -> None:
        """The last text out (live: claimed; dry: its job file written), how many held, how many unseen today, and the time
        of this pass at most every 15 s (`pass`: status and doctor say when a sender stopped, J50), to status."""
        now, outs = self.clock(), [float(r["handed"]) for r in self.ledger.texting_in("sent" if self.mode == "live" else self.wait) if r["handed"]]
        said = {"last_out": max(outs, default=None), "held": len(self.ledger.texting_in("held")),
                "unseen": sum(1 for r in self.ledger.texting_in("unseen") if float(r["handed"] or 0) > now - 86400),
                "pass": self.said["pass"] if now - self.said.get("pass", 0) < 15 else round(now)}
        if said != self.said and self.status is not None:
            self.status(said)
        self.said = said

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
        self.counts()
        self.daily()

    def daily(self) -> None:
        """From 08:00 local, the machine room's line for each day not said yet, at most a week back (J61): the day is kept
        before its line goes, so a restart never says one twice and never skips one."""
        if self.machine is None or self.day_file is None:
            return
        now = datetime.fromtimestamp(self.clock()).astimezone()
        due = now.date() - timedelta(days=now.hour < 8)
        if self.day is None:
            try:
                self.day = date.fromisoformat(json.loads(self.day_file.read_text())["day"])
            except (OSError, ValueError, KeyError, TypeError):
                self.day = due - timedelta(days=1)           # the first start: this day's line only
        while self.day < due:
            self.day = max(self.day + timedelta(days=1), due - timedelta(days=6))
            self.day_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(self.day_file, json.dumps({"day": str(self.day)}))
            self.machine(day_line(self.ledger, self.mode, self.day))

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
        owner's newer than the newest of them that is not texting's own claimed send (a tapback of the owner's too: an entry of a
        row's `reactions`, by its row id), or a text of theirs newer than it; a tapback of theirs is none of these (J49)."""
        gs, newest, got = gs_of(r), self.newest(r), {str(x.get("guid")): x for x in rows}
        if any(g not in got for g in gs):
            return "they took it back"
        if any((d := (self.ledger.inbound_get(g) or {}).get("digest")) and digest(str(got[g].get("text") or "")) != d for g in gs):
            return "they changed it"
        newer = [x for x in rows if (w := when_of(x)) is not None and w.timestamp() > newest and str(x.get("guid")) not in gs]
        own = taps(self.ledger, str(r["person"]))
        if any(x.get("is_from_me") is True and (self.ledger.inbound_get(str(x.get("guid"))) or {}).get("sender") != "auto" for x in newer) \
                or any(e.get("is_from_me") is True and str(e.get("id")) not in own and (w := when_of(e)) is not None and w.timestamp() > newest
                       for x in rows for e in x.get("reactions") or []):
            return "owner answered"
        return "they wrote again" if any(x.get("is_from_me") is not True and not is_tapback(x) for x in newer) else None

    async def hand(self, r: dict[str, Any], now: float) -> None:
        """The next part, once (J41; dry: every part, as files): `handed` is written before the hands run, so a stop in between
        never hands twice; only an ssh that could not connect, which wrote nothing, gives it back to the next pass."""
        rid, newest, done = int(r["id"]), self.newest(r), len(gs_of(r, "claimed"))
        if not parts(r):                                  # no text and no tapback: never a job (an empty one raised here and stopped the sender)
            return self.end(r, "dropped", words.TEXTING_NO_DRAFT)
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
        tapback row only a tapback job and a text row only a text job; with none, an `unseen` one the same way, seen late
        (J62: the bridge was away while the Mac sent it). The last part claimed, or one seen late: `sent` (a part never
        handed stays dropped); else the next part is due a `burst_gap` draw from now (J40). A tapback is kept by its row id,
        the one name a history gives it (`taps`)."""
        t = self.clock() if at is None else at
        jobs = [r for state in ("queued", "unseen") for r in self.ledger.texting_rows(p.key, state) if self.mode == "live"
                and r["handed"] is not None and (r["decision"] == "react") == is_tapback(msg)
                and float(r["handed"]) - 10 <= t <= float(r["handed"]) + self.claim_s]
        if not jobs:
            return False
        r = min(jobs, key=lambda r: (r["state"] != "queued", float(r["handed"])))
        rid, got = int(r["id"]), ",".join([*gs_of(r, "claimed"), str((msg.get("id") if is_tapback(msg) else msg.get("guid")) or "")])
        if (n := len(got.split(","))) >= parts(r) or r["state"] == "unseen":
            self.ledger.texting_set(rid, claimed=got, state="sent",
                                    reason="" if r["state"] == "queued" else words.TEXTING_LATE.format(n=n, of=parts(r)))
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
        """A pass a second. Any other error stops the sender and never the daemon (a task that ends ends it): logged, and
        said once in the machine room by its class alone, never a text or a person (J50); nothing more is timed or sent."""
        await self.start()
        while True:
            try:
                await self.tick()
            except (OSError, ValueError) as e:
                log.warning("texting: the sender's pass did not run: %s", e)
            except Exception as e:
                log.error("texting: the sender stopped: %s", e, exc_info=True)
                if self.machine is not None:
                    self.machine(words.TEXTING_SENDER_DOWN.format(error=type(e).__name__))
                await asyncio.Event().wait()
            await every(1.0)
