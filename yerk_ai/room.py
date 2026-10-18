"""A room at run time: its Claude Code process, a message that comes in mid-turn, closing when idle, and a failed or empty turn run again.

Channel-agnostic: an adapter hands the room messages with `submit` and the room calls back the
adapter's `progress`, `reply` and `notice`. Turns run on one thread per busy room, never on the
adapter's event loop.

- A message for an idle room starts a turn; a message for a busy room goes straight to the
  process's stdin (the fold) and is answered inside the running turn.
- Each `result` answers the messages Claude Code replayed (accepted) before it. A message that
  was written but not yet accepted when a result lands gets the next result, which Claude Code
  starts by itself.
- An empty reply retries once in a fresh session. An auth or quota failure rests the account and
  continues the same turn on the next one, resuming the same session; the card stays.
- After `idle_seconds` without a turn the process closes; the next message resumes the session.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .accounts import Account, Cooldowns, pick
from .engine import RoomProcess, RoomSpec, Turn
from .ledger import Ledger
from .lifted.classify import FailureTable

log = logging.getLogger("yerk.room")


@dataclass
class Inbound:
    id: str
    sender: str
    text: str
    received: float = field(default_factory=time.time)
    prev_human: float | None = None       # when the previous human message reached this room
    attempt: int = 0


class Channel(Protocol):
    def progress(self, room: str, text: str) -> None: ...            # post or edit the room's card
    def reply(self, room: str, text: str, inbound: list[str]) -> list[str]: ...   # card off, reply on
    def notice(self, room: str, text: str, inbound: list[str]) -> list[str]: ...  # a visible failure


class Room:
    def __init__(self, name: str, conf: dict[str, Any], spec: Callable[[], RoomSpec], accounts: list[Account],
                 channel: Channel, state: Any, ledger: Ledger, failures: FailureTable, cooldowns: Cooldowns,
                 idle_seconds: float = 1800.0, turn_timeout: float = 900.0, log_dir: Any = None):
        self.name, self.conf, self.spec, self.accounts, self.channel = name, conf, spec, accounts, channel
        self.state, self.ledger, self.failures, self.cooldowns = state, ledger, failures, cooldowns
        self.idle_seconds, self.turn_timeout, self.log_dir = idle_seconds, turn_timeout, log_dir
        self.lock = threading.Lock()
        self.rp: RoomProcess | None = None
        self.busy = False
        self.reading = False                      # a turn's stream is being read: a new message folds
        self.queue: deque[Inbound] = deque()      # waiting for a process
        self.pending: deque[Inbound] = deque()    # written to stdin, not yet accepted
        self.inflight: list[Inbound] = []         # written since the last delivered result
        self.last_active = time.time()
        self.last_human: float | None = ledger.last_received(name)   # survives a daemon restart
        self.seen: dict[str, Inbound] = {}        # by text, for the hook's metadata line
        self.first_line: str | None = None
        self.answered: set[str] = set()           # sessions with at least one delivered reply

    # called by the channel adapter
    def submit(self, msg: Inbound) -> str:
        with self.lock:
            msg.prev_human, self.last_human = self.last_human, msg.received
            self.seen[msg.text.strip()] = msg
            while len(self.seen) > 200:
                self.seen.pop(next(iter(self.seen)))
            if self.reading and self.rp and self.rp.alive() and not self.queue:
                self._write(msg)
                self.ledger.inbound_set(msg.id, state="running", folded=1, attempts=msg.attempt + 1)
                return "folded"
            self.queue.append(msg)
            if self.busy:
                return "queued"
            self.busy = True
        threading.Thread(target=self._run, name=f"room-{self.name}", daemon=True).start()
        return "started"

    def meta_for(self, prompt: str) -> tuple[str, float | None]:
        """Sender and previous human message time for a prompt the hook saw."""
        with self.lock:
            m = self.seen.get(prompt.strip())
            return (m.sender, m.prev_human) if m else ("unknown", self.last_human)

    def reap_idle(self, now: float | None = None) -> bool:
        """Close the process after idle_seconds without a turn; the session stays bound."""
        now = time.time() if now is None else now
        with self.lock:
            if self.busy or not self.rp or now - self.last_active < self.idle_seconds:
                return False
            rp, self.rp = self.rp, None
        rp.close()
        log.info("room %s: idle process closed, session %s kept", self.name, rp.session_id)
        return True

    # the room's thread
    def _write(self, msg: Inbound) -> None:
        assert self.rp
        self.pending.append(msg)
        self.inflight.append(msg)
        self.rp.send(msg.text)

    def _start(self, account: Account, fresh: bool = False) -> None:
        log_path = self.log_dir / f"{self.name}.jsonl" if self.log_dir else None
        rp = RoomProcess(self.spec(), account, self.state, ledger=self.ledger, failures=self.failures, log=log_path)
        rp.start(fresh=fresh)
        self.rp = rp

    def _account(self) -> Account:
        return pick(self.accounts, self.cooldowns)

    def _run(self) -> None:
        try:
            while True:
                with self.lock:
                    if not self.queue:
                        self.busy = False
                        self.last_active = time.time()
                        return
                    batch = list(self.queue)
                    self.queue.clear()
                self._turn(batch)
        except Exception:
            log.exception("room %s: turn crashed", self.name)
            with self.lock:
                self.busy = self.reading = False

    def _on_event(self, turn: Turn, ev: dict[str, Any]) -> None:
        """The card's first visible line: the first line of text, or the tool that started."""
        if self.first_line is not None or turn.first_visible is None:
            return
        e = ev.get("event") or {}
        if e.get("type") == "content_block_start":
            self.first_line = "using " + str((e.get("content_block") or {}).get("name", "a tool"))
        elif e.get("type") == "content_block_delta":
            self.first_line = str((e.get("delta") or {}).get("text", "")).strip().split("\n")[0][:180] or None
        if self.first_line:
            self.channel.progress(self.name, self.first_line)

    def _turn(self, batch: list[Inbound]) -> None:
        self.first_line = None
        self.channel.progress(self.name, "working on it")
        fresh, retried, hops, account = False, False, 0, self._account()
        resend = batch
        while True:
            with self.lock:
                old = self.rp
                restart = old is None or not old.alive() or old.account != account or fresh
                if restart:
                    self.rp = None
            if restart:
                if old:
                    old.close()
                self._start(account, fresh=fresh)
                fresh = False
            with self.lock:
                self.pending.clear()
                self.inflight = []
                for m in resend:
                    self._write(m)
                    self.ledger.inbound_set(m.id, state="running", attempts=m.attempt + 1)
                self.reading = True
            started = time.time()
            while True:
                assert self.rp
                turn = self.rp.read_turn(started, self.turn_timeout, record=False, on_event=self._on_event)
                with self.lock:
                    n = len(turn.accepted) or (1 if turn.outcome == "ok" else 0)
                    covered = [self.pending.popleft() for _ in range(min(n, len(self.pending)))]
                ids = [m.id for m in covered]
                turn_id = self.rp.record(turn, ids)
                if turn.outcome != "ok":
                    break
                self.cooldowns.clear(account.name)
                self.answered.add(self.rp.session_id)
                reply_ids = self.channel.reply(self.name, turn.text, ids)
                now = time.time()
                for m in covered:
                    self.ledger.inbound_set(m.id, state="answered", answered=now, turn_id=turn_id,
                                            reply_ids=",".join(reply_ids))
                with self.lock:
                    self.inflight = list(self.pending)
                    if not self.pending:
                        self.reading = False      # from here a new message queues for the next turn
                        return
                started, self.first_line = time.time(), None
                self.channel.progress(self.name, "working on it")
            # the turn failed: every message written since the last delivered result goes again
            with self.lock:
                self.reading = False
                resend = list(self.inflight)
            action = turn.failure.action if turn.failure else None
            if turn.outcome == "empty" and not retried:
                log.info("room %s: empty reply, retrying once in a fresh session", self.name)
                fresh = retried = True
                continue
            if action == "hop_account" and hops < len(self.accounts) - 1:
                f = turn.failure
                assert f
                self.cooldowns.fail(account.name, lambda r: self.failures.cooldown_seconds(f.kind or "auth", r))
                nxt = self._account()
                if nxt != account:
                    log.info("room %s: %s on %s, continuing on %s", self.name, f.cls, account.name, nxt.name)
                    # a session that never answered may have no transcript to resume elsewhere
                    hops, account = hops + 1, nxt
                    fresh = not (self.rp and (self.rp.resume_arg or self.rp.session_id in self.answered))
                    continue
            why = turn.failure.cls if turn.failure else turn.outcome
            notice_ids = self.channel.notice(self.name, f"No reply: the turn ended {turn.outcome} ({why}).",
                                             [m.id for m in resend])
            for m in resend:
                self.ledger.inbound_set(m.id, state="failed", note=why, reply_ids=",".join(notice_ids))
            with self.lock:
                old, self.rp, self.inflight = self.rp, None, []
                self.pending.clear()
            if old:
                old.close()
            return
