"""A room at run time: its Claude Code process, a message that comes in mid-turn, closing when idle, and a failed or empty turn run again.

Channel-agnostic: an adapter hands the room messages with `submit` and the room calls back the
adapter's `progress`, `reply` and `notice`. Turns run on one thread per busy room, never on the
adapter's event loop.

- A message for an idle room starts a turn; a message for a busy room goes straight to the
  process's stdin (the fold) and is answered inside the running turn.
- Each `result` answers the messages Claude Code replayed (accepted) before it. A message that
  was written but not yet accepted when a result lands gets the next result, which Claude Code
  starts by itself.
- A failed turn takes the action failures.toml gives it: a transient failure reruns the same
  session; an overflow compacts it and runs the turn again; an auth or quota failure rests the
  account and continues the same turn on the next one, resuming the same session under the same
  card (the ledger row names the hop); a session that cannot be resumed starts fresh with a
  one-line recap; anything else ends in a visible notice. An empty reply retries once fresh.
- A turn Claude Code answered on a fallback model after a block stands, marked on its card and
  ledger row; the room's next turn starts a fresh session on the room's own model.
- After `idle_seconds` without a turn the process closes; the next message resumes the session.
- Each turn owns one card (card.py), fed from the stream; every outbound part carries the turn's
  key. A turn that was sent an image and never read it is bounced once at Stop, naming the file;
  a working turn answering a person that ends with nothing visible in the room bounces once,
  naming what is owed (nothing that already landed under the turn's key is sent again).
- A message from the harness itself (sender `yerk`: a worker's relay) runs like any other but is
  not a human message for the clock line or the idle rule; it may cap its reply and name a line the
  reply must carry, and it hears how its turn ended.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import words
from .accounts import Account, Cooldowns, pick
from .card import Card
from .engine import RoomProcess, RoomSpec, Turn
from .evidence import Evidence
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
    files: list[dict[str, Any]] = field(default_factory=list)   # staged attachments (name, type, path, ok)
    cap: int = 0                          # a harness message: the most its reply may be (0: no cap)
    must: str = ""                        # ... a line the reply must carry (a relay's "report saved" line)
    then: Callable[[str, list[str], int], None] | None = None   # (answered or failed, message ids, chars)


HARNESS = "yerk"                          # the sender of Yerk AI's own messages (a worker's relay)


def fit(text: str, cap: int, must: list[str]) -> str:
    """A reply held to `cap` characters, cut at a line or a sentence, carrying every `must` line as a
    room reads it (words.carried: a worker's report path is "report saved" and its command); one the
    reply already has, or whose `command` it already names, is not added twice."""
    lines = [words.carried(m) for m in dict.fromkeys(must) if m]
    tail = "\n".join(m for m in lines if m not in text and not any(c in text for c in re.findall(r"`([^`]+)`", m)))
    room = cap - (len(tail) + 1 if tail else 0)
    if len(text) > room:
        cut = text[:max(room - 3, 0)]
        at = max(cut.rfind("\n"), cut.rfind(". ") + 1)
        text = (cut[:at] if at > room // 2 else cut).rstrip() + "..."
    return f"{text}\n{tail}" if tail else text


RESUME_RECAP = ("(Yerk AI: the earlier session of this room could not be resumed, so this is a new one. "
                "The last reply here began: \"{last}\")")
REFUSAL_RECAP = ("(Yerk AI: this is a new session. The previous one was moved to another model after a block, "
                 "so its messages are not in this context.)")


class Channel(Protocol):
    def open_card(self, room: str, text: str, kind: str = "turn") -> None: ...   # post the turn's card
    def progress(self, room: str, text: str) -> None: ...            # edit the room's card
    def reply(self, room: str, text: str, inbound: list[str],          # card off (or kept with `card`), reply on
              card: str | None = None, key: str | None = None) -> list[str]: ...
    def notice(self, room: str, text: str, inbound: list[str],        # a visible failure: the card kept with
               card: str | None = None, key: str | None = None) -> list[str]: ...  # `card`, else `text` posted


class Room:
    def __init__(self, name: str, conf: dict[str, Any], spec: Callable[[], RoomSpec], accounts: list[Account],
                 channel: Channel, state: Any, ledger: Ledger, failures: FailureTable, cooldowns: Cooldowns,
                 idle_seconds: float = 1800.0, turn_timeout: float = 900.0, log_dir: Any = None,
                 meters: Callable[[str, dict[str, Any]], None] | None = None):
        self.name, self.conf, self.spec, self.accounts, self.channel = name, conf, spec, accounts, channel
        self.state, self.ledger, self.failures, self.cooldowns = state, ledger, failures, cooldowns
        self.idle_seconds, self.turn_timeout, self.log_dir, self.meters = idle_seconds, turn_timeout, log_dir, meters
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
        self.last_reply = ""                      # the first line of the last reply, for a recap
        self.fresh_next: str | None = None        # a recap: the next turn starts a fresh session
        self.card = Card()
        self.turn_key = ""                        # the idempotency key of the result being produced
        self.images: dict[str, str] = {}          # path -> name: images sent in since the last result
        self.bounced = False                      # the unread-image check bounced this result already
        self.silent_bounced = False               # the silent-ending check bounced this result already
        self.evidence = Evidence(on_call=self._ledger_call)   # every tool call of the turn and what it read
        self.answering: set[str] = set()                      # ids of the batch the turn answers

    def _ledger_call(self, call: dict[str, Any]) -> None:
        self.ledger.evidence(t=round(time.time(), 3), room=self.name, session_id=self.rp.session_id if self.rp else "",
                             turn=self.turn_key, tool=call["tool"], target=call["target"], chars=call["chars"],
                             tool_use_id=call.get("id"))

    def human_turn(self) -> bool:
        """A turn is running that answers a person (not only the harness's own messages)."""
        with self.lock:
            return self.busy and any(m.sender != HARNESS for m in [*self.inflight, *self.pending])

    def current_ids(self) -> set[str]:
        """The ids of the messages this turn is answering (the topic hit leaves them out)."""
        with self.lock:
            return {m.id for m in [*self.inflight, *self.queue]} | self.answering

    def prompts(self) -> list[str]:
        with self.lock:
            return [m.text for m in self.inflight]

    def compact_now(self) -> str:
        """`yerk room compact ROOM`: the same /compact the overflow path sends, when no turn runs."""
        with self.lock:
            if self.busy:
                return "busy"
            self.busy = True
        try:
            with self.lock:
                alive = self.rp is not None and self.rp.alive()
            if not alive:
                self._start(self._account())
            assert self.rp
            t = self.rp.ask("/compact", self.turn_timeout)
            log.info("room %s: compact (asked) ended %s", self.name, t.outcome)
            return "ok" if t.outcome in ("ok", "empty") else t.outcome     # /compact answers with no text
        finally:
            with self.lock:
                self.busy, self.last_active = False, time.time()

    # called by the channel adapter
    def idle_reason(self, seconds: float, now: float | None = None) -> str:
        """Why the room is not idle for a relay ("" when it is): a turn running, or a human message
        in the last `seconds`."""
        now = time.time() if now is None else now
        with self.lock:
            if self.busy or self.queue:
                return "a turn was running"
            if self.last_human is not None and now - self.last_human < seconds:
                return f"a human message {now - self.last_human:.0f} s ago (under {seconds:g} s)"
        return ""

    def submit(self, msg: Inbound) -> str:
        with self.lock:
            msg.prev_human = self.last_human
            if msg.sender != HARNESS:
                self.last_human = msg.received
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

    def revise(self, message_id: str, text: str) -> bool:
        """An edit of a message still waiting for its turn: it runs as edited. False once written."""
        with self.lock:
            for m in self.queue:
                if m.id == message_id:
                    m.text = text
                    self.seen[text.strip()] = m
                    return True
        return False

    def withdraw(self, message_id: str) -> bool:
        """A message deleted while still waiting for its turn never runs. False once written."""
        with self.lock:
            for m in list(self.queue):
                if m.id == message_id:
                    self.queue.remove(m)
                    return True
        return False

    def inbox(self, message_id: str) -> Any:
        """The folder a message's files are staged in: inside the room's working folder, so the
        room's process can read them."""
        d = self.state / "rooms" / self.name / "work" / "inbox" / message_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def unread_images(self) -> list[tuple[str, str]]:
        """(name, path) of the images sent in this result that the process never Read."""
        with self.lock:
            read = self.rp.read_paths if self.rp else set()
            return [(n, p) for p, n in self.images.items() if p not in read]

    def stop_check(self, active: bool) -> str | None:
        """The Stop hook's rule, no judge: an image sent in and never read bounces once, naming it."""
        unread = self.unread_images()
        if not unread or active or self.bounced:
            return None
        self.bounced = True
        names = ", ".join(n for n, _ in unread)
        paths = " ".join(p for _, p in unread)
        return (f"You were sent {names} and have not opened it. Read {paths} with the Read tool, "
                "then answer with what it shows.")

    def silent_check(self, active: bool, text: str | None, landed: int) -> str | None:
        """The Stop hook's second rule, no judge: a turn that ran tools for a person and ends with
        nothing visible (no final text, nothing landed under the turn's key) bounces once, naming the
        message it owes. `text` None means the payload did not say, and that never bounces."""
        if active or text is None or text.strip() or landed or self.silent_bounced:
            return None
        with self.lock:
            asked = [m for m in self.inflight if m.sender != HARNESS]
        with self.evidence.lock:
            worked = bool(self.evidence.calls)
        if not asked or not worked:
            return None
        self.silent_bounced = True
        head = " ".join(asked[0].text.split())[:160]
        return (f"Your turn is ending with nothing visible in the room, and the room is still waiting on: \"{head}\". "
                "Reply now with the answer, or one plain line on where it stands.")

    def _new_card(self) -> None:
        privacy = str(self.conf.get("privacy", "owner"))
        self.card = Card(thinking=bool(self.conf.get("thinking_panel", False)) and privacy == "owner")
        self.turn_key, self.bounced, self.silent_bounced = uuid.uuid4().hex[:12], False, False
        self.channel.open_card(self.name, self.card.render())   # state first from the first post

    def meta_for(self, prompt: str) -> tuple[str, float | None]:
        """Sender and previous human message time for a prompt the hook saw."""
        with self.lock:
            m = self.seen.get(prompt.strip())
            return (m.sender, m.prev_human) if m else ("unknown", self.last_human)

    def reap_idle(self, now: float | None = None, force: bool = False) -> bool:
        """Close the process after idle_seconds without a turn (at once with `force`, still never
        mid-turn); the session stays bound."""
        now = time.time() if now is None else now
        with self.lock:
            if self.busy or not self.rp or (not force and now - self.last_active < self.idle_seconds):
                return False
            rp, self.rp = self.rp, None
        rp.close()
        log.info("room %s: idle process closed, session %s kept", self.name, rp.session_id)
        return True

    # the room's thread
    def _write(self, msg: Inbound, recap: str | None = None) -> None:
        assert self.rp
        self.pending.append(msg)
        self.inflight.append(msg)
        for f in msg.files:
            if f.get("ok") and str(f.get("type", "")).startswith("image/"):
                self.images[str(f["path"])] = str(f["name"])
        text = f"{recap}\n\n{msg.text}" if recap else msg.text
        if recap:
            self.seen[text.strip()] = msg
        self.rp.send(text)

    def _start(self, account: Account, fresh: bool = False) -> None:
        log_path = self.log_dir / f"{self.name}.jsonl" if self.log_dir else None
        rp = RoomProcess(self.spec(), account, self.state, ledger=self.ledger, failures=self.failures, log=log_path,
                         meters=self.meters, evidence=self.evidence)
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
        """The card from the stream: narration, thinking line, tool rows."""
        if self.card.feed(ev):
            self.first_line = self.first_line or self.card.narration or "a tool started"
            self.channel.progress(self.name, self.card.render())

    def _note(self, line: str) -> None:
        """A harness line on the card (a hop, a compaction) until the model's next words."""
        self.card.narration = line
        self.channel.progress(self.name, self.card.render())

    def _turn(self, batch: list[Inbound]) -> None:
        self.first_line = None
        with self.lock:
            self.images = {}
            self.answering = {m.id for m in batch}
        self.evidence.reset()
        ctx = self.state / "rooms" / self.name / "context.md"
        if ctx.is_file():
            self.evidence.give(ctx.read_text(errors="replace"))     # the session-start file was read too
        self._new_card()
        account = self._account()
        recap, self.fresh_next = self.fresh_next, None          # set by a refusal fallback last turn
        fresh, retried, compacted, reruns, hops, hop = bool(recap), False, False, 0, 0, None
        recap_sid = None                       # the session that already got the recap
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
            assert self.rp
            note = recap if self.rp.session_id != recap_sid else None   # a recap rides until a session has it
            with self.lock:
                self.pending.clear()
                self.inflight = []
                for i, m in enumerate(resend):
                    self._write(m, note if i == 0 else None)
                    self.ledger.inbound_set(m.id, state="running", attempts=m.attempt + 1)
                self.reading = True
            recap_sid = self.rp.session_id if note else recap_sid
            step, turn = self._results(account, hop, bool(note))
            with self.lock:
                self.reading = False
                resend = list(self.inflight)
            if step == "done":
                return
            if step == "fresh":                   # answered on a fallback model with messages still waiting
                fresh, recap, self.fresh_next = True, self.fresh_next, None
                continue
            assert turn
            f = turn.failure
            action = f.action if f else None
            if turn.outcome == "empty" and not retried:
                log.info("room %s: empty reply, retrying once in a fresh session", self.name)
                fresh = retried = True
                continue
            if action == "rerun" and (wait := self.failures.rerun_wait(reruns)) is not None:
                log.info("room %s: %s, rerun %d in %.0f s", self.name, f and f.source, reruns + 1, wait)
                reruns += 1
                time.sleep(wait)
                continue
            if action == "compact_then_rerun" and not compacted:
                compacted = True
                self._compact(account)
                continue
            if action == "fresh_with_recap" and not retried:
                log.info("room %s: the session could not be resumed, starting fresh with a recap", self.name)
                fresh, retried, recap = True, True, RESUME_RECAP.format(last=self.last_reply or "nothing yet")
                continue
            if action == "hop_account" and f and hops < len(self.accounts) - 1:
                kind = f.kind or "auth"
                self.cooldowns.fail(account.name, kind, lambda r: self.failures.cooldown_seconds(kind, r))
                nxt = self._account()
                if nxt != account:
                    log.info("room %s: %s on %s, continuing on %s", self.name, f.cls, account.name, nxt.name)
                    hop = ",".join(filter(None, [hop, f"{f.cls}:{account.name}"]))
                    hops, account, self.first_line = hops + 1, nxt, None
                    self._note(words.MOVED.format(name=nxt.name))
                    # a session that never answered may have no transcript to resume elsewhere
                    fresh = not (self.rp and (self.rp.resume_arg or self.rp.session_id in self.answered))
                    continue
            why = f.cls if f else turn.outcome
            plain = words.why(f.cls if f else None, turn.outcome)
            asked = resend[0].text if resend else ""
            notice_ids = self.channel.notice(self.name, f"{words.NOT_DONE} {words.NOT_ANSWERED}\n{plain}",
                                             [m.id for m in resend], card=self.card.owed(plain, asked),
                                             key=self.turn_key)
            for m in resend:
                self.ledger.inbound_set(m.id, state="failed", note=why, reply_ids=",".join(notice_ids))
                self._then(m, "failed", notice_ids, 0)
            with self.lock:
                old, self.rp, self.inflight = self.rp, None, []
                self.pending.clear()
            if old:
                old.close()
            return

    def _results(self, account: Account, hop: str | None, recapped: bool) -> tuple[str, Turn | None]:
        """Deliver each result of the running process until nothing waits: "done"; a failed turn:
        "failed"; a reply on a fallback model while messages still wait: "fresh"."""
        started = time.time()
        while True:
            assert self.rp
            turn = self.rp.read_turn(started, self.turn_timeout, record=False, on_event=self._on_event)
            with self.lock:
                n = len(turn.accepted) or (1 if turn.outcome == "ok" else 0)
                covered = [self.pending.popleft() for _ in range(min(n, len(self.pending)))]
            ids = [m.id for m in covered]
            turn_id = self.rp.record(turn, ids, hop=hop, recap=1 if recapped else None, turn_key=self.turn_key)
            if turn.outcome != "ok":
                return "failed", turn
            self.cooldowns.clear(account.name)
            self.answered.add(self.rp.session_id)
            card = None
            if turn.fallback_model:                # which model: the turn's ledger row
                card = f"{words.WORKING} {words.BACKUP[0]}\n{words.SUB}{words.BACKUP[1]}"
                self.fresh_next = REFUSAL_RECAP
            text = turn.text
            if covered and all(m.cap for m in covered):         # harness messages only: held to their cap
                text = fit(text, min(m.cap for m in covered), [m.must for m in covered])
            reply_ids = self.channel.reply(self.name, text, ids, card, key=self.turn_key)
            self.last_reply = text.strip().split("\n")[0][:160]
            now = time.time()
            for m in covered:
                self.ledger.inbound_set(m.id, state="answered", answered=now, turn_id=turn_id,
                                        reply_ids=",".join(reply_ids))
                self._then(m, "answered", reply_ids, len(text))
            with self.lock:
                self.inflight = list(self.pending)
                self.images = {}
                if not self.pending or self.fresh_next:
                    self.reading = False          # from here a new message queues for the next turn
                    return ("fresh" if self.pending else "done"), turn
            started, self.first_line, hop, recapped = time.time(), None, None, False
            self._new_card()

    def _then(self, m: Inbound, state: str, ids: list[str], chars: int) -> None:
        if m.then:
            try:
                m.then(state, ids, chars)
            except Exception:                    # a harness callback never stops the room
                log.exception("room %s: the callback of %s failed", self.name, m.id)

    def _compact(self, account: Account) -> None:
        """/compact in the room's session (resumed when its process is gone), before the rerun."""
        with self.lock:
            alive = self.rp is not None and self.rp.alive()
        if not alive:
            self._start(account)
        assert self.rp
        self._note(words.COMPACTING)
        t = self.rp.ask("/compact", self.turn_timeout)
        log.info("room %s: compact ended %s", self.name, t.outcome)
