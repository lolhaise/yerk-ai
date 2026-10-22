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
  one-line recap; anything else ends in a visible notice, and a message that folded into the turn
  after it started runs next as its own turn. An empty reply resumes the same session once more,
  then starts fresh with the recap.
- A turn Claude Code answered on a fallback model after a block stands, marked on its card and
  ledger row; the room's next turn starts a fresh session on the room's own model.
- After `idle_seconds` without a turn the process closes; the next message resumes the session. A new
  model, effort or account (the settings tool) closes it when the running turn ends, never inside it.
- Each turn owns one card (card.py), fed from the stream; every outbound part carries the turn's
  key. A turn that was sent an image and never read it is bounced once at Stop, naming the file;
  a working turn answering a person that ends with nothing visible in the room bounces once,
  naming what is owed (nothing that already landed under the turn's key is sent again); a final
  text that only reports a filing (`remember`, `schedule`, `hold`, `settings`) bounces once (stub).
- A message from the harness itself (sender `yerk`: a worker's relay) runs like any other but is
  not a human message for the clock line or the idle rule; it may cap its reply and name a line the
  reply must carry, and it hears how its turn ended.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from . import transcribe, words
from .accounts import Account, Cooldowns, pick
from .card import Card
from .config import flag, override, pinned, write_private
from .engine import RoomProcess, RoomSpec, SessionTable, Turn
from .evidence import Evidence
from .ledger import Ledger
from .lifted.classify import Failure, FailureTable
from .meters import Meters, rest_until

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
    alone: bool = False                   # a harness turn of its own: never folded into a running turn, never
                                          # batched with another message (a relay, a watcher's event); in a
                                          # closed room it resumes the room's session, as any message does
    after: bool = False                   # it folded into a turn that failed: it runs next as its own turn, and its card says so


HARNESS = "yerk"                          # the sender of Yerk AI's own messages (a worker's relay)
FILING = ("remember", "schedule", "hold", "settings")   # Yerk AI's calls that file or set something, answering nothing
STUB_CHARS = 200                          # the stub rule's size line (Room.stub_check)


def fit(text: str, cap: int, must: list[str]) -> str:
    """A reply held to `cap` characters, cut at a line or a sentence, carrying every `must` line as a
    room reads it (words.carried: a worker's report path is "report saved" and its command); one the
    reply already has, or whose `command` it already names, is not added twice."""
    lines = [words.carried(m) for m in dict.fromkeys(must) if m]

    def missing(t: str) -> str:              # judged on the reply as cut, so a cut never drops a carried line
        return "\n".join(m for m in lines if m not in t and not any(c in t for c in re.findall(r"`([^`]+)`", m)))

    def cut(t: str, room: int) -> str:
        if len(t) <= room:
            return t
        c = t[:max(room - 3, 0)]
        at = max(c.rfind("\n"), c.rfind(". ") + 1)
        return (c[:at] if at > room // 2 else c).rstrip() + "..."
    body = cut(text, cap)
    if tail := missing(body):
        body = cut(text, cap - len(tail) - 1)
        tail = missing(body)
    return f"{body}\n{tail}" if tail else body


RESUME_RECAP = ("(Yerk AI: the earlier session of this room could not be resumed, so this is a new one; the day log "
                "holds what was said. The last reply here began: \"{last}\")")
REFUSAL_RECAP = ("(Yerk AI: this is a new session. The previous one was moved to another model after a block, "
                 "so its messages are not in this context.)")
NEW_RECAP = "(Yerk AI: the owner asked for a new session with /new, so this is a new one. The last reply here began: \"{last}\")"


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
                 meters: Meters | None = None, compact: tuple[int, float, float] = (0, 0.0, 0.0)):
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
        self.answered: set[str] = set()           # sessions with at least one delivered reply
        self.last_reply = ""                      # the first line of the last reply, for a recap
        self.fresh_next: str | None = None        # a recap: the next turn starts a fresh session
        self.card = Card()
        self.turn_key = ""                        # the idempotency key of the result being produced
        self.to_open: dict[str, str] = {}         # path -> name: images and voice notes sent in since the last result
        self.bounced = False                      # the unopened-file check bounced this result already
        self.silent_bounced = False               # the silent-ending check bounced this result already
        self.stub_bounced = False                 # the stub check bounced this result already
        self.board_bounced = False                # the board check bounced this result already
        self.evidence = Evidence(on_call=self._ledger_call)   # every tool call of the turn and what it read
        self.answering: set[str] = set()                      # ids of the batch the turn answers
        self.stale = False                        # the settings changed mid-turn: the process closes at its end
        self.ran: RoomSpec | None = None          # the spec of the last process started
        self.out_of = ""                          # the pinned model every account is out of: the turn runs on rooms.toml's
        self.stopped = False                      # /stop: the running turn ends now and is never run again
        self.halt = threading.Event()             # ... and a rerun's wait ends at once
        self.closing = False                      # the daemon is stopping (shutdown): nothing more starts or is written
        self.window, self.compact_at, self.compact_after = compact   # the idle compaction's line (idle_compact); a 0 is off
        self.context = ledger.last_context(str((SessionTable(state / "sessions.json").get(name) or {}).get("session_id") or ""))
        self.grown = True                         # the session grew since an idle compaction was last tried

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
        t = self._compact_held("asked")
        return "ok" if t.outcome in ("ok", "empty", "compacted") else t.outcome

    def idle_compact(self, now: float | None = None) -> bool:
        """`yerk room compact`, by the daemon (its reaper's minute): a room with no turn and nothing queued for `compact_after`
        seconds, whose session holds `compact_at` of its window or more, compacts in the background, so the next message finds
        it small instead of waiting minutes in its own turn for Claude Code's compaction. Once per growth: a try, kept or
        failed, waits for a turn to grow the session again. Never when its next run starts a fresh session. True: started."""
        now = time.time() if now is None else now
        fresh = self.fresh_next or (self.state / "rooms" / self.name / "fresh-next").is_file()
        with self.lock:
            if (self.busy or self.queue or self.closing or fresh or not self.grown or not self.window or not self.compact_at
                    or now - self.last_active < self.compact_after or self.context < self.compact_at * self.window):
                return False
            self.busy, self.grown = True, False
        log.info("room %s: idle %.0f min at %d of its %d tokens, compacting", self.name, (now - self.last_active) / 60, self.context, self.window)
        threading.Thread(target=self._idle_compact, name=f"compact-{self.name}", daemon=True).start()
        return True

    def _idle_compact(self) -> None:
        try:
            if (t := self._compact_held("idle")).compacted is None:     # said once: no retry until a turn grows the session
                log.warning("room %s: the idle compaction did not compact (%s); not tried again before the next turn", self.name, t.outcome)
        except Exception as e:
            log.warning("room %s: the idle compaction failed (%s); not tried again before the next turn", self.name, e)

    def _compact_held(self, why: str) -> Turn:
        """/compact in the room's session (resumed when its process is gone) while the caller holds the room busy. A message
        that came in meanwhile was queued: it runs right after, and its wait is logged."""
        started = time.time()
        try:
            with self.lock:
                alive = self.rp is not None and self.rp.alive()
            if not alive:
                self._start(self._account())
            t = self._slash_compact()
            before, self.context = self.context, t.context or self.context
            log.info("room %s: compact (%s) ended %s in %.0f s, %d to %d tokens", self.name, why, t.outcome, time.time() - started, before, self.context)
            return t
        finally:
            with self.lock:
                waiting, self.last_active = list(self.queue), time.time()
                self.busy = run = bool(waiting) and not self.closing
            if run:
                log.info("room %s: %d message(s) waited up to %.0f s for the compaction (%s), running now", self.name, len(waiting),
                         self.last_active - min(m.received for m in waiting), why)
                threading.Thread(target=self._run, name=f"room-{self.name}", daemon=True).start()

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
            if self.reading and self.rp and self.rp.alive() and not self.queue and not msg.alone:
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

    def unopened(self) -> list[tuple[str, str]]:
        """(name, path) of the images and voice notes sent in this result that the process never Read or transcribed."""
        with self.lock:
            read = self.rp.read_paths if self.rp else set()
            return [(n, p) for p, n in self.to_open.items() if p not in read]

    def stop_check(self, active: bool) -> str | None:
        """The Stop hook's rule, no judge: an image sent in and never read, or a voice note never transcribed, bounces once,
        naming it, so a room never answers a note it did not hear."""
        unread = self.unopened()
        if not unread or active or self.bounced:
            return None
        self.bounced = True
        heard = [x for x in unread if transcribe.is_audio(x[0])]
        seen = [x for x in unread if x not in heard]
        return " ".join(([f"You were sent {', '.join(n for n, _ in seen)} and have not opened it. Read {' '.join(p for _, p in seen)} "
                          "with the Read tool, then answer with what it shows."] if seen else []) +
                        ([f"You were sent {', '.join(n for n, _ in heard)}, a recording, and have not transcribed it. Call transcribe "
                          f"on {' '.join(p for _, p in heard)}, then open your answer with what it says."] if heard else []))

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

    def stub_check(self, active: bool, text: str | None) -> tuple[str, str] | None:
        """The Stop hook's third rule, no judge (2026-10-21: "Filed." and a canned rule line went out in place of answers):
        the result's last call is one of FILING and its final text, all the room sees, reports it, the answer above the call
        (a text block over STUB_CHARS and twice the final's length) or never written (a final under STUB_CHARS opening
        with words.FILED). It bounces once, quoting the final's start: (the bounce, what was found), or None."""
        with self.evidence.lock:
            trail = list(self.evidence.trail)
        calls = [i for i, (kind, _) in enumerate(trail) if kind == "tool"]
        if active or not text or self.stub_bounced or not calls or (tool := trail[calls[-1]][1].rsplit("__", 1)[-1]) not in FILING:
            return None
        n, above = len(text.strip()), max((len(t.strip()) for kind, t in trail[:calls[-1]] if kind == "text"), default=0)
        found = (f"{above} characters above the {tool} call, {n} after it" if above > STUB_CHARS and above >= 2 * n else
                 f"{n} characters after the {tool} call, opening as a filing" if n < STUB_CHARS and words.FILED.match(text) else "")
        self.stub_bounced = bool(found)
        return (words.STUB.format(head=" ".join(text.split())[:80]), found) if found else None

    def board_check(self, active: bool, text: str | None) -> tuple[str, str] | None:
        """The Stop hook's fourth rule, no judge, in a room with people only (2026-10-21: the owner rooms' status board,
        its ⛔ 🟨 items, "Unblocks: allow workers here" and ☐ lines, went out in a person's room): a final holding the
        board's marks (words.BOARD_LINE) bounces once, saying how many lines hold them: (the bounce, what was found), or None."""
        if active or not text or self.board_bounced or not self.conf.get("people"):
            return None
        n = sum(1 for x in text.splitlines() if words.BOARD_LINE.search(x))
        self.board_bounced = bool(n)
        found = f"{n} line{'s' * (n != 1)} with a checklist mark, the colour key or what unblocks it"
        return (words.BOARD.format(found=found), found) if n else None

    def _new_card(self, note: str = "") -> None:
        privacy = str(self.conf.get("privacy", "owner"))
        self.card = Card(thinking=flag(self.conf.get("thinking_panel")) and privacy == "owner",
                         people=self.name if self.conf.get("people") else "")
        self.card.narration = note                # a harness line from the first post, until the model's first words
        self.turn_key, self.bounced, self.silent_bounced, self.stub_bounced = uuid.uuid4().hex[:12], False, False, False
        self.board_bounced = False
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

    def respec(self) -> None:
        """The room's model, effort or account changed: never mid-turn. With no turn running the process closes now, else
        when the turn ends; the next message resumes the same session on the new spec (another account: as a hop does)."""
        with self.lock:
            self.stale = self.busy
            rp, self.rp = (None, self.rp) if self.busy else (self.rp, None)
        if rp:
            rp.close()

    def renew(self) -> None:
        """/new: the room's next run (the next message, or a running turn's messages still waiting) starts a
        fresh session (memory stays: its start is built as every start is) with the room's recap line. The
        line is also the daemon's own file, `fresh-next` beside the room's override, so a restart before the
        next message keeps it."""
        with self.lock:
            self.fresh_next = NEW_RECAP.format(last=self.last_reply or "nothing yet")
            (self.state / "rooms" / self.name).mkdir(parents=True, exist_ok=True)
            write_private(self.state / "rooms" / self.name / "fresh-next", self.fresh_next)

    def _take_fresh(self) -> str | None:
        """The recap the next run starts a fresh session with (a refusal fallback's, or /new's, from its file
        after a restart), taken once: the file goes with it."""
        path = self.state / "rooms" / self.name / "fresh-next"
        with self.lock:
            recap, self.fresh_next = self.fresh_next, None
            if recap is None and path.is_file():
                recap = path.read_text(errors="replace") or None
            path.unlink(missing_ok=True)
        return recap

    def stop(self) -> bool:
        """/stop: the running turn ends now and for good (L82): never rerun, hopped or retried fresh, its
        messages ledgered stopped, so a restart does not run them again. Only the live process is touched
        (L136); between two runs of the turn (a hop, a rerun's wait, a retry) no next run starts and nothing
        is written again. False when no turn runs."""
        with self.lock:
            if not self.busy:
                return False
            self.stopped, rp = True, self.rp if self.reading else None
            self.halt.set()
        if rp:
            rp.end_now()
        return True

    def shutdown(self) -> bool:
        """A daemon stop (SIGTERM): the room's process ends now, a turn mid-run cut with it. Its messages stay `running`
        in the ledger, never a `failed` row and never a notice, so the next start runs them once more, marked (the
        redeliver-once rule); nothing starts or is written after this. True when a turn was cut."""
        with self.lock:
            self.closing, rp, busy = True, self.rp, self.busy
            self.halt.set()
        if rp:
            if busy:
                rp.end_now()
            rp.close()                            # and the session's lock freed: the next start resumes it
        return busy

    # the room's thread
    def _write(self, msg: Inbound, recap: str | None = None) -> None:
        assert self.rp
        self.pending.append(msg)
        self.inflight.append(msg)
        for f in msg.files:
            if f.get("ok") and (str(f.get("type", "")).startswith("image/") or transcribe.is_audio(str(f["name"]), str(f.get("type", "")))):
                self.to_open[str(f["path"])] = str(f["name"])
        text = f"{recap}\n\n{msg.text}" if recap else msg.text
        if recap:
            self.seen[text.strip()] = msg
        self.rp.send(text)

    def _start(self, account: Account, fresh: bool = False) -> None:
        log_path = self.log_dir / f"{self.name}.jsonl" if self.log_dir else None
        spec = self.spec()
        if self.stale and self.ran:              # a hop or retry inside the turn that changed the settings
            spec.model, spec.extra_args = self.ran.model, self.ran.extra_args       # stays on the turn's own
        spec.model = str(self.conf["model"]) if self.out_of else spec.model   # every account out of the pinned model: the room's own
        self.ran = spec
        rp = RoomProcess(spec, account, self.state, ledger=self.ledger, failures=self.failures, log=log_path,
                         meters=self.meters.seen if self.meters else None, evidence=self.evidence)
        rp.start(fresh=fresh)
        self.rp = rp
        self.hooked = self.hooks_warned = False   # per process start: its first hook call proves the hooks alive; the notice went out

    def _account(self, model: str = "") -> Account:
        """The first account not resting (for `model`, when named), in the room's order with the one it is put on (config.pinned) first."""
        return pick(self.accounts, self.cooldowns, first=pinned(self.state, self.name, [a.name for a in self.accounts]), model=model)

    def _fall_back(self, model: str, account: Account) -> tuple[str, Account] | None:
        """When `account` (pick's answer for `model`, the one the room was put on) rests for it, every account does: the turn
        runs on rooms.toml's model, when that is another one, on the first account free for it, and the card says so (an owner
        room hears the models and when the first account comes back; any other the hop's plain line). None: nothing to fall to."""
        usual = str(self.conf.get("model") or "")
        if usual in ("", model) or self.cooldowns.until(account.name, model=model) is None:
            return None
        self.out_of, back = model, min(filter(None, (self.cooldowns.until(a.name, model=model) for a in self.accounts)))
        log.info("room %s: every account is out of %s until %s, the turn runs on %s", self.name, model, words.clock(back), usual)
        self._note(words.OUT_OF.format(model=words.model_name(usual), pinned=words.model_name(model), until=words.clock(back))
                   if str(self.conf.get("privacy", "owner")) == "owner" else words.MOVED_ON)
        return usual, self._account(usual)

    def _run(self) -> None:
        batch: list[Inbound] = []
        try:
            while True:
                with self.lock:
                    if not self.queue:
                        self.busy = False
                        self.last_active = time.time()
                        return
                    batch = [self.queue.popleft()]
                    while not batch[0].alone and self.queue and not self.queue[0].alone:
                        batch.append(self.queue.popleft())
                try:
                    self._turn(batch)
                finally:                         # a turn that raised has ended too
                    with self.lock:              # the settings changed in that turn: it has ended, so its
                        rp, self.rp, self.stale = (self.rp, None, False) if self.stale else (None, self.rp, False)
                    if rp:                       # process goes and the next message resumes on the new spec
                        rp.close()
                        log.info("room %s: settings changed, process closed, session %s kept", self.name, rp.session_id)
        except Exception:
            log.exception("room %s: turn crashed", self.name)
            with self.lock:
                self.busy = self.reading = False
            if batch and not self.closing:        # said in the room, its rows closed (disk-full drill c: a card left at
                with contextlib.suppress(Exception):   # "working on it" for good); when the ledger or the channel is what
                    self._end(batch, words.why(None, "crashed"), "crashed")   # failed, the rows stay open for the next start

    def _on_event(self, turn: Turn, ev: dict[str, Any]) -> None:
        """The card from the stream: narration, thinking line, tool rows."""
        if self.card.feed(ev):
            self.channel.progress(self.name, self.card.render())

    def _note(self, line: str) -> None:
        """A harness line on the card (a hop, a compaction) until the model's next words."""
        self.card.narration = line
        self.channel.progress(self.name, self.card.render())

    def _turn(self, batch: list[Inbound]) -> None:
        with self.lock:
            self.to_open, self.stopped = {}, False
            self.halt.clear()
            self.answering = {m.id for m in batch}
        self.evidence.reset()
        ctx = self.state / "rooms" / self.name / "context.md"
        if ctx.is_file():
            self.evidence.give(ctx.read_text(errors="replace"))     # the session-start file was read too
        self._new_card(words.AFTER_FAILED if any(m.after for m in batch) else "")
        self.out_of, model = "", str(override(self.state, self.name).get("model") or self.conf.get("model") or "")   # the pinned model
        on, account = self._fall_back(model, a := self._account(model)) or (model, a)   # `on`: the model the turn runs on
        recap = self._take_fresh()                              # a refusal fallback last turn, or /new
        fresh, retried, emptied, compacted, reruns, hops, hop = bool(recap), False, False, False, 0, 0, None
        recap_sid = None                       # the session that already got the recap
        resend = batch
        while True:
            if self.closing:                      # the daemon is stopping: no next run, no row, no notice (Room.shutdown)
                return
            if self.stopped:                      # /stop between two runs: no next run is started or fed
                self._end(resend, words.STOPPED, "/stop", "stopped")
                return
            with self.lock:
                old = self.rp
                if (restart := old is None or not old.alive() or old.account != account or old.spec.model != on or fresh):
                    self.rp = None
            if restart:
                if old:
                    old.close()
                self._start(account, fresh=fresh)
                fresh = False
                model = on = self.rp.spec.model if self.rp and not self.out_of else model   # the process's model is the truth: an override off the list was ignored
            assert self.rp
            note = recap if self.rp.session_id != recap_sid else None   # a recap rides until a session has it
            with self.lock:                       # Room.stop sets `stopped` under this lock: it sees `reading`
                halted = self.stopped             # and TERMs the process, or nothing is written here
                if not halted:
                    self.pending.clear()
                    self.inflight = []
                    for i, m in enumerate(resend):
                        self._write(m, note if i == 0 else None)
                        self.ledger.inbound_set(m.id, state="running", attempts=m.attempt + 1)
                    self.reading = True
            if halted:                            # it landed while the process started
                self._end(resend, words.STOPPED, "/stop", "stopped")
                return
            recap_sid = self.rp.session_id if note else recap_sid
            step, turn = self._results(account, hop, bool(note))
            with self.lock:
                self.reading = False
                resend = list(self.inflight)
            if step == "done" or self.closing:    # closing: the process was ended under the turn; its rows stay open
                return
            if self.stopped:                      # /stop: no rerun, hop or fresh retry, here or after a restart
                self._end(resend, words.STOPPED, "/stop", "stopped")
                return
            if step == "fresh":                   # answered on a fallback model (or /new) with messages still waiting
                fresh, recap = True, self._take_fresh()
                continue
            assert turn
            f = turn.failure
            action = f.action if f else None
            if turn.outcome == "empty" and not emptied:   # the same session once more, on a new process: it holds the day's context
                log.info("room %s: empty reply, resuming the same session once more", self.name)
                self._note(words.EMPTY_AGAIN)
                emptied = True
                self.rp.close()
                continue
            if action == "rerun" and (wait := self.failures.rerun_wait(reruns)) is not None:
                log.info("room %s: %s, rerun %d in %.0f s", self.name, f and f.source, reruns + 1, wait)
                reruns += 1
                self.halt.wait(wait)              # /stop ends the wait at once
                continue
            if action == "compact_then_rerun" and not compacted:
                compacted = True
                self._compact(account)
                continue
            if (action == "fresh_with_recap" or turn.outcome == "empty") and not retried:   # empty twice: fresh, through the recap
                log.info("room %s: %s, starting fresh with a recap", self.name,
                         "empty again" if turn.outcome == "empty" else "the session could not be resumed")
                self._note(words.NEW_SESSION)
                fresh, retried, recap = True, True, RESUME_RECAP.format(last=self.last_reply or "nothing yet")
                continue
            if action == "hop_account" and f:
                kind = f.kind or "auth"                       # a rate limit rests the account for this model until its reset
                self.cooldowns.fail(account.name, kind, lambda r: self.failures.cooldown_seconds(kind, r),
                                    model=on if kind == "rate_limit" else "", until=rest_until(turn.resets, self.meters, account.name, kind))
                nxt = self._account(on)
                if not self.out_of and (fell := self._fall_back(model, nxt)):   # every account is out of the pinned model now
                    (on, nxt), hops = fell, 0
                if (nxt != account and hops < len(self.accounts) - 1) or on != self.rp.spec.model:   # a model switch, once; hops capped
                    log.info("room %s: %s on %s, continuing on %s", self.name, f.cls, account.name, nxt.name)
                    hop = ",".join(filter(None, [hop, f"{f.cls}:{account.name}"]))
                    hops, account = hops + 1, nxt
                    if on == self.rp.spec.model:              # the hop's own line; a model switch said its line already
                        self._note(words.MOVED.format(name=nxt.name) if str(self.conf.get("privacy", "owner")) == "owner" else words.MOVED_ON)
                    # a session that never answered may have no transcript to resume elsewhere
                    fresh = not (self.rp and (self.rp.resume_arg or self.rp.session_id in self.answered))
                    continue
            plain = self._off_model(f) or words.why(f.cls if f else None, turn.outcome)
            self._end(resend, plain, f.cls if f else turn.outcome)
            return

    def _end(self, resend: list[Inbound], plain: str, why: str, state: str = "failed") -> None:
        """A turn that ends unanswered: its card kept saying what was owed, its messages ledgered `state`. A message
        that folded into a failed turn after it started (seen live: a greeting during a 36-step job) is no part
        of its failure: it runs next, in order, as its own turn, its card saying so; /stop stops them all."""
        own = [m for m in resend if m.id in self.answering or state != "failed"] or resend[:1]
        asked = own[0].text if own else ""
        notice_ids = self.channel.notice(self.name, f"{words.NOT_DONE} {words.NOT_ANSWERED}\n{plain}",
                                         [m.id for m in own], card=self.card.owed(plain, asked), key=self.turn_key)
        for m in own:
            self.ledger.inbound_set(m.id, state=state, note=why, reply_ids=",".join(notice_ids))
            self._then(m, "failed", notice_ids, 0)
        with self.lock:
            old, self.rp, self.inflight = self.rp, None, []
            self.pending.clear()
            self.queue.extendleft(reversed([replace(m, after=True) for m in resend if m not in own]))
        if old:
            old.close()

    def _off_model(self, f: Failure | None) -> str:
        """A turn that failed because its model was not found, on the model of the room's override: the
        override loses the model, so the next start is on rooms.toml's, and the room's line says so."""
        bad, usual = str((over := override(self.state, self.name)).get("model") or ""), str(self.conf.get("model") or "")
        if not (f and f.source in ("type:model_not_found", "pattern:model") and bad and bad != usual
                and self.rp and self.rp.spec.model == bad):
            return ""
        over.pop("model")
        write_private(self.state / "rooms" / self.name / "override.json", json.dumps(over))
        log.info("room %s: %s was not found, the override's model dropped", self.name, bad)
        return words.BACK_ON.format(model=words.model_name(bad), usual=words.model_name(usual) if usual else words.USUAL)

    def _results(self, account: Account, hop: str | None, recapped: bool) -> tuple[str, Turn | None]:
        """Deliver each result of the running process until nothing waits: "done"; a failed turn:
        "failed"; a reply on a fallback model while messages still wait: "fresh"."""
        started = time.time()
        while True:
            assert self.rp
            if self.stopped:                      # /stop came between two runs: this one ends as it starts
                self.rp.end_now()
            turn = self.rp.read_turn(started, self.turn_timeout, record=False, on_event=self._on_event)
            if turn.context:                      # the session's size now: the idle compaction's to read
                self.context, self.grown = turn.context, True
            if self.stopped and turn.outcome != "ok":
                turn.outcome, turn.failure = "stopped", None
            with self.lock:
                n = len(turn.accepted) or (1 if turn.outcome == "ok" else 0)
                covered = [self.pending.popleft() for _ in range(min(n, len(self.pending)))]
            ids = [m.id for m in covered]
            turn_id = self.rp.record(turn, ids, hop=hop, recap=1 if recapped else None, turn_key=self.turn_key, out_of=self.out_of or None)
            if turn.outcome != "ok":
                return "failed", turn
            self.cooldowns.clear(account.name, model=self.rp.spec.model)
            self.answered.add(self.rp.session_id)
            why = next((f[11:] for f in turn.flags if f.startswith("hook_error:")), "")   # Claude Code said a hook failed
            if why and not self.hooked and not self.hooks_warned:    # and none reached the daemon: the gates and the
                self.hooks_warned = True          # per-message context are off; said once per process, never per turn
                log.error("room %s: no hook call reached the daemon, turn %s: %s", self.name, self.turn_key, why)
                self.ledger.gate(room=self.name, session_id=self.rp.session_id, turn=self.turn_key, gate="hooks",
                                 decision="dead", tool="", rule="no-hook-call", detail=why[:300])
                self.channel.notice(self.name, f"{words.NOT_DONE} {words.HOOKS_DEAD}", [], key=f"{self.turn_key}:hooks")
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
                self.ledger.inbound_set(m.id, state="answered", answered=now, turn_id=turn_id, reply_ids=",".join(reply_ids))
                self._then(m, "answered", reply_ids, len(text))
            with self.lock:
                self.inflight = list(self.pending)
                self.to_open = {}
                if not self.pending or self.fresh_next:
                    self.reading = False          # from here a new message queues for the next turn
                    return ("fresh" if self.pending else "done"), turn
            started, hop, recapped = time.time(), None, False
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
        self._note(words.COMPACTING)
        log.info("room %s: compact ended %s", self.name, self._slash_compact().outcome)

    def _slash_compact(self) -> Turn:
        """/compact, both paths' one call: no text comes back, so a row that compacted says so, never a failed turn."""
        assert self.rp
        t = self.rp.read_turn(self.rp.send("/compact"), self.turn_timeout, record=False)
        t.outcome = "compacted" if t.compacted is not None and t.outcome in ("ok", "empty") else t.outcome
        self.rp.record(t)                         # read by yerk status --json's failed turns
        return t
