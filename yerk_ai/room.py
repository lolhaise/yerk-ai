"""A room at run time: its Claude Code process, a message that comes in mid-turn, closing when idle, and a failed or empty turn run again.

Channel-agnostic: an adapter hands the room messages with `submit` and the room calls back the
adapter's `progress`, `reply` and `notice`. Turns run on one thread per busy room, never on the
adapter's event loop.

- A message for an idle room starts a turn; a message for a busy room goes straight to the
  process's stdin (the fold) and is answered inside the running turn, a line ahead of it saying it
  came in while the room was answering and that the reply names it (words.FOLDED).
- Each `result` covers every message Claude Code replayed (accepted) before it, and one replay may
  carry several waiting messages joined with newlines (seen live on 2.1.316). A message written
  but not yet accepted when a result lands gets the next result, which Claude Code starts by itself.
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
- A session compacts between turns: at once when a turn ends within `compact_within` of Claude Code's own line (_ended),
  the room held, a message meanwhile waiting under its card. A turn that ends further out, within `precompact_within` of
  the line, and a room idle `compact_after` past `compact_at` of its window (idle_compact: with no process alive it starts
  one, the room held for that start only, and a fast room's waits for a turn) compact a copy of the session instead, the
  room left free (_precompact): a message meanwhile runs at once on the live session and the copy goes on compacting;
  once it has, it becomes the room's session in one step, at once when no turn runs, else as the running turn ends,
  before a message waiting runs, so the next message finds it small without waiting. What the live session said since
  the copy was made rides once, as text, in the first message the copy gets (_tail); while one compacts, the turn-end
  compaction waits for it as long as a turn still fits under the line ([session] copy_room). With a room's
  `precompact_midturn` on (off by default; `log` only says it would), a turn whose call reaches that band mid-turn starts
  the copy at that call (_midturn), its own exchange first in the tail, said to be one request already answered.
- Each turn owns one card (card.py), fed from the stream; every outbound part carries the turn's
  key. A turn that was sent an image and never read it is bounced once at Stop, naming the file;
  a working turn answering a person that ends with nothing visible in the room bounces once,
  naming what is owed (nothing that already landed under the turn's key is sent again); a final of exactly NO_REPLY
  posts nothing, its card kept saying the room read the message and had nothing to add, ledgered `silent`, and the
  quiet line before it goes when the room said nothing since, so one stands (a watched message's verdict owed by a
  `watch_audit` room: the card says none came, and stays); a final
  text that only reports a filing (`remember`, `schedule`, `hold`, `settings`) bounces once (stub); a final opening with
  a note to itself before the answer (its plan for the reply, a rulebook tag) bounces once (note); in a room without
  people, a final holding a mark the install retired ([gates] retired_marks) outside a fenced block bounces once
  (retired); a fenced block the
  room posted on an earlier day is turned back once, in `send` before it posts and in the final text at Stop (paste); a
  document or a pasted block an owner's room ships with no claims sheet written that turn is turned back once the same
  way (sheet); a person's message that folded into the result and was taken in, whose content (its numbers, a short
  one's words) the final never names, bounces once (fold).
- A message from the harness itself (sender `yerk`: a worker's relay) runs like any other but is
  not a human message for the clock line or the idle rule; it may cap its reply and name a line the
  reply must carry, and it hears how its turn ended.
"""
from __future__ import annotations

import calendar
import contextlib
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Protocol

from . import memory, pictures, transcribe, watch, words
from .accounts import Account, Cooldowns, pick
from .card import Card
from .config import fast_on, flag, override, pinned, write_private
from .engine import FAST_MODELS, RoomProcess, RoomSpec, SessionTable, Turn, fast_reaches
from .evidence import DOCUMENT, QUOTED, Evidence, pasted, people, quotes, read_any, sheet_written, shipped
from .ledger import COMPACTED_IN_TURN, Ledger
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
                                          # batched with another message (a relay, a note); in a
                                          # closed room it resumes the room's session, as any message does
    after: bool = False                   # it folded into a turn that failed: it runs next as its own turn, and its card says so
    sent: str = ""                        # the exact text written to stdin (a recap or the fold's line ahead of it too): what its replay carries


HARNESS = "yerk"                          # the sender of Yerk AI's own messages (a worker's relay)
RESERVE = 33000                           # Claude Code compacts at its window less this (2.1.304's output reserve and buffer)
FILING = ("remember", "schedule", "hold", "settings")   # Yerk AI's calls that file or set something, answering nothing
STUB_CHARS = 200                          # the stub rule's size line (Room.stub_check)
# the note rule's signals (Room.note_check), as measured 2026-10-29 over 2,571 finals: one hit, the reply seen live
SHAPE = re.compile(r"\b(?:in|to) (?:a|one|two|three) lines?\b|\bkeep it (?:to|short|tight|brief)\b|\b(?:one|two|three)[- ]liner\b|\bno need to\b|\b(?:and|then) move(?: on)?\b|\bstyle\b|\btone\b|\bregister\b|\bthe ask\b|\bthe one correction\b|\bnuance\b", re.I)
DIRECTIVE = re.compile(r"(?:^|[.;:!?]\s+)(?:Confirm|Keep|Say|Answer|Lead|Open|Close|Give|Tell|Name|Skip|Drop|Add|Use|Match|Mirror|Stay|Hold|Avoid|Don't|Do not|No need)\b")
YOU = re.compile(r"\b(?:you|your|you're|yours|yourself)\b", re.I)
RULEQUOTE = re.compile(r"\b(?:Landed|Bounced):")
NOTE_CHARS = 700                          # a first paragraph longer than this is never a note


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


def covered_by(replays: list[str], sent: list[str]) -> int:
    """How many of the messages written to stdin (`sent`, oldest first, as `_write` handed them) a turn's replays carry: one
    replay may carry every line waiting when a turn ends, joined with newlines (seen live on 2.1.316).
    A replay that matches nothing at the cursor counts as one, as before; a message no replay carries is never counted."""
    i = 0
    for r in replays:
        j = i
        while r and j < len(sent) and (r == sent[j] or r.startswith(sent[j] + "\n")):
            r, j = r[len(sent[j]) + 1:], j + 1      # one message taken: the rest of the replay is the next one's
        i = max(j, i + 1)                          # nothing matched at the cursor: one, today's count
    return i


def own_words(text: str) -> str:
    """A person's message without the lines the harness wrote into it ([attachment], [a reply to ...], the edit mark)."""
    return " ".join(x for x in text.splitlines() if not x.lstrip().startswith("["))


def fold_tokens(text: str) -> set[str]:
    """The fold rule's content tokens of a person's message (Room.fold_check): its numbers (41 in "EX-41") and, in a message
    of at most three words, its words of three or more letters less words.FILLER, lower case."""
    said = own_words(text)
    found = set(re.findall(r"\d+", said))
    return found | ({w.lower() for w in re.findall(r"[^\W\d_]{3,}", said)} - words.FILLER if len(said.split()) <= 3 else set())


def carries(text: str, token: str) -> bool:
    """A content token in a final text, case aside: a number as a whole number (41 in "EX-41", never in 410), a word at a word's start."""
    return re.search(rf"(?<!\d){token}(?!\d)" if token.isdigit() else rf"\b{re.escape(token)}", text, re.I) is not None


RESUME_RECAP = ("(Yerk AI: the earlier session of this room could not be resumed, so this is a new one; the day log "
                "holds what was said. The last reply here began: \"{last}\")")
REFUSAL_RECAP = ("(Yerk AI: this is a new session. The previous one was moved to another model after a block, "
                 "so its messages are not in this context.)")
NEW_RECAP = "(Yerk AI: the owner asked for a new session with /new, so this is a new one. The last reply here began: \"{last}\")"
TAIL_HEAD = ("## Said after the summary above was written\n(Yerk AI: while the summary above was being written, this room "
             "went on talking. Here is each message and the room's reply since, word for word and in order, with their times. "
             "The tool output of those turns is not included; anything they wrote to memory or to disk is there.)")
TAIL_CUT = "Earlier, named by their day-log lines to keep this short:"
TAIL_RAN = ("(Yerk AI: the first exchange below was already running when the summary above was written, which is why the "
            "summary shows it unfinished or interrupted, a tool call of it cut off. It is one request, asked once, not two "
            "attempts: that same work went on and finished on the live session, as its reply says. Nothing in it is to be done "
            "or checked again.)")
MIDTURN = ("off", "log", "on")            # [session] precompact_midturn: a copy started mid-turn (Room._midturn); off by default


class Channel(Protocol):
    def open_card(self, room: str, text: str) -> None: ...   # post the turn's card
    def progress(self, room: str, text: str) -> None: ...            # edit the room's card
    def reply(self, room: str, text: str, inbound: list[str],          # card off (or kept with `card`), reply on
              card: str | None = None, key: str | None = None) -> list[str]: ...
    def notice(self, room: str, text: str, inbound: list[str],        # a visible failure: the card kept with
               card: str | None = None, key: str | None = None) -> list[str]: ...  # `card`, else `text` posted
    def silent(self, room: str, inbound: list[str], card: str,         # a final of exactly NO_REPLY: nothing posted,
               key: str | None = None) -> list[str]: ...              # the card kept with `card`


class Room:
    def __init__(self, name: str, conf: dict[str, Any], spec: Callable[[], RoomSpec], accounts: list[Account],
                 channel: Channel, state: Any, ledger: Ledger, failures: FailureTable, cooldowns: Cooldowns,
                 idle_seconds: float = 1800.0, turn_timeout: float = 900.0, log_dir: Any = None,
                 meters: Meters | None = None, compact: tuple[int, float, float, float] = (0, 0.0, 0.0, 0.0),
                 precompact: float = 0.0, copy: tuple[int, int] = (12000, 30000), midturn: str = "off"):
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
        self.folds: list[Inbound] = []            # of those, folded into the result being read, marked (the fold rule's)
        self.replays: list[str] = []              # the replays of the result being read, as the stream gives them
        self.last_active = time.time()
        self.last_human: float | None = ledger.last_received(name)   # survives a daemon restart
        self.seen: dict[str, Inbound] = {}        # by text, for the hook's metadata line
        self.answered: set[str] = set()           # sessions with at least one delivered reply
        self.last_reply = ""                      # the first line of the last reply, for a recap
        self.fresh_next: str | None = None        # a recap: the next turn starts a fresh session
        self.card = Card()
        self.turn_key = ""                        # the idempotency key of the result being produced
        self.fast_state, self.fast_told = "", False   # fast_mode_state Claude Code last reported; a refusal said already
        self.to_open: dict[str, str] = {}         # path -> name: images, voice notes and videos sent in since the last result
        self.bounced = False                      # the unopened-file check bounced this result already
        self.silent_bounced = False               # the silent-ending check bounced this result already
        self.stub_bounced = False                 # the stub check bounced this result already
        self.note_bounced = False                 # the note check bounced this result already
        self.filing_bounced = False               # the filing check bounced this result already
        self.board_bounced = False                # the board check bounced this result already
        self.retired_bounced = False              # the retired-marks check bounced this result already
        self.paste_bounced = False                # the paste check turned back a send or a final of this result already
        self.sheet_bounced = False                # the sheet check turned back a send or a final of this result already
        self.said_bounced = False                 # the person and quote check turned back a send or a final of this result already
        self.fold_bounced = False                 # the fold check bounced this result already
        self.evidence = Evidence(on_call=self._ledger_call)   # every tool call of the turn and what it read
        self.answering: set[str] = set()                      # ids of the batch the turn answers
        self.stale = False                        # the settings changed mid-turn: the process closes at its end
        self.ran: RoomSpec | None = None          # the spec of the last process started
        self.out_of = ""                          # the pinned model every account is out of: the turn runs on rooms.toml's
        self.stopped = False                      # /stop: the running turn ends now and is never run again
        self.halt = threading.Event()             # ... and a rerun's wait ends at once
        self.closing = False                      # the daemon is stopping (shutdown): nothing more starts or is written
        self.window, self.compact_at, self.compact_after, self.compact_within = compact   # idle_compact's and _ended's; a 0 is off
        self.context = ledger.last_context(str((SessionTable(state / "sessions.json").get(name) or {}).get("session_id") or ""))
        self.grown = True                         # the session grew since an idle compaction was last tried
        self.shortening = self.early = False      # a compaction holds the room; a message waiting on it has its card up
        self.precompact_within = precompact       # _ended's copy compaction, a share of the window; 0 is off
        self.fork: RoomProcess | None = None      # the copy being compacted off to the side (_precompact), at most one
        self.runs = 0                             # bumped by anything that changes or replaces the live session: a copy
                                                  # taken before a bump is stale (Claude Code's own prefix_changed rule)
        self.copy_tail, self.copy_room = copy     # [session] copy_tail (chars) and copy_room (tokens under the line)
        self.ready: tuple[RoomProcess, Turn, float] | None = None   # a copy compacted while a turn ran: taken as it ends
        self.tail: list[dict[str, Any]] = []      # each exchange on the live session since the copy was made (_said)
        self.overtook = 0                         # the turns that ran on the live session since the copy was made
        self.dropped = ""                         # why the copy in flight was ended (_drop_fork), for its thread's line
        self.shrunk = False                       # the live session compacted since the copy was made: the copy is moot
        self.midturn = midturn if midturn in MIDTURN[1:] else "off"   # the copy started mid-turn too (_midturn): log, on, else off
        self.midturn_said, self.midturn_at = False, 0   # ... at most once a turn, and the most a call of this turn started at
        self.midturn_of = ""                      # the key of the turn the copy in flight was made in, mid-turn ("": none)
        self.exited = threading.Event()           # clear while reap_idle or respec closes the process: _start waits for it,
        self.exited.set()                         # since the closing process holds its session's lock until it is gone
        self.cite: Callable[[str, float], str] | None = None   # a message's day-log cite (the daemon's), for a long tail

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

    def compact_now(self) -> str:
        """`yerk room compact ROOM`: the same /compact the overflow path sends, when no turn runs."""
        with self.lock:
            if self.busy:
                return "busy"
            self.busy = self.shortening = True
        t = self._compact_held("asked")
        return "ok" if t.outcome in ("ok", "empty", "compacted") else t.outcome

    def idle_compact(self, now: float | None = None) -> bool:
        """The daemon's idle compaction (its reaper's minute): a room with no turn and nothing queued for `compact_after`
        seconds, whose session holds `compact_at` of its window or more, compacts in the background, so the next message finds
        it small instead of waiting minutes in its own turn for Claude Code's compaction. Once per growth: a try, kept or
        failed, waits for a turn to grow the session again. Never when its next run starts a fresh session. True: started.
        With the room's process alive a copy compacts (_fork_off), launched without fast mode, the room free, and is taken
        (_take), so nobody waits on it (seen live: held 150 s on its own session, a message 66 s
        in waited 84 s). With none alive to copy, a room whose next process runs with fast mode (usage credits) waits for a
        turn, and any other starts its process first, the room held for that start only (_idle_start)."""
        now = time.time() if now is None else now
        fresh = self.fresh_next or (self.state / "rooms" / self.name / "fresh-next").is_file()
        with self.lock:
            if (self.busy or self.queue or self.closing or fresh or not self.grown or not self.window or not self.compact_at
                    or self.fork or now - self.last_active < self.compact_after or self.context < self.compact_at * self.window):
                return False
            self.grown, idle, fast = False, (now - self.last_active) / 60, self._fast_next()
            why = f"idle {idle:.0f} min{' with fast mode on' if fast else ''}"
            if copy := fast or bool(self.rp and self.rp.alive()):
                self._fork_off(why)
                started = self.fork is not None
            else:
                self.busy = True                  # for its process's start only (_idle_start)
        if copy:
            if fast and not started:
                log.info("room %s: idle %.0f min at %d of its %d tokens with no process alive, and the next one runs with fast "
                         "mode, so the compaction waits for a turn", self.name, idle, self.context, self.window)
            return started
        log.info("room %s: idle %.0f min at %d of its %d tokens with no process alive, starting one to copy", self.name, idle,
                 self.context, self.window)
        threading.Thread(target=self._idle_start, args=(why,), name=f"compact-{self.name}", daemon=True).start()
        return True

    def _idle_start(self, why: str) -> None:
        """idle_compact with no process alive (a daemon restart): the room's process starts, the room held for that start
        only, then a copy compacts (_fork_off) and a message that came in meanwhile runs at once on the live session."""
        try:
            self._start(self._account())
        except Exception as e:
            log.warning("room %s: the idle compaction could not start the room's process (%s); not tried again before the next "
                        "turn", self.name, e)
        with self.lock:
            if not self.closing:
                self._fork_off(why)
            waiting = list(self.queue)
            self.busy = run = bool(waiting) and not self.closing
        if run:
            log.info("room %s: %d message(s) waited up to %.0f s for the room's process to start (idle), running now", self.name,
                     len(waiting), time.time() - min(m.received for m in waiting))
            threading.Thread(target=self._run, name=f"room-{self.name}", daemon=True).start()

    def _fast_next(self) -> bool:
        """Under the lock, whether the room's /compact would reach a process launched with fast mode: the live one's key,
        else the next one's, by the room's own /fast on and model as status.fast_shown reads them (spec() mints a token)."""
        if self.rp is not None and self.rp.alive():
            return fast_reaches(self.rp.spec)
        return fast_on(self.state, self.name) and str(override(self.state, self.name).get("model") or self.conf.get("model") or "") in FAST_MODELS

    def _ended(self) -> bool:
        """Under the lock, a turn ended with nothing queued: a session within `compact_within` of its window under Claude Code's
        own line (the window less RESERVE) compacts now, in the background as idle_compact does, not `compact_after` later, so
        the next message finds it small instead of waiting in its own turn for Claude Code's (seen live: a turn ended at about
        763,900 of 800,000 and the message 4 min 55 s later waited 2 min for one). Once per growth; never after /stop or the
        daemon's stop, nor when the next run starts a fresh session. True: started, the room kept busy.
        A turn that ends short of that but within `precompact_within` of the line compacts a copy instead (_precompact) and
        the room stays free: False (seen live: a turn ended 37,362 under its line of 767,000,
        and the message after it waited 126 s behind the 206 s compaction; earlier a turn had ended 61,754 under it with
        305 s of quiet after, time for a copy's compaction, then ledgered at a median 161 s). While a copy compacts, the
        turn-end compaction is the last resort: it waits for the copy as long as a turn still fits, `copy_room` or more
        under the line (seen live: it ran at 917,284, 49,716 under the line, and the message
        107 s later waited 14 s under its card)."""
        line = self.window - RESERVE
        if (self.closing or self.stopped or not self.grown or not self.window or self.fresh_next
                or (self.state / "rooms" / self.name / "fresh-next").is_file()):
            return False
        if not self.compact_within or self.context < line - self.compact_within * self.window:
            if self.precompact_within and self.context >= line - self.precompact_within * self.window:
                self._fork_off()
            return False
        if self.fork is not None and line - self.context >= self.copy_room:
            log.info("room %s: a turn ended at %d of its %d tokens, Claude Code's line %d; a copy is compacting and a turn "
                     "still fits, so the session waits for it", self.name, self.context, self.window, line)
            return False
        self.grown, self.shortening = False, True
        log.info("room %s: a turn ended at %d of its %d tokens, Claude Code's line %d, compacting", self.name, self.context, self.window, line)
        threading.Thread(target=self._idle_compact, args=("end-of-turn",), name=f"compact-{self.name}", daemon=True).start()
        return True

    def _idle_compact(self, why: str = "idle") -> None:
        try:
            if (t := self._compact_held(why)).compacted is None:     # said once: no retry until a turn grows the session
                log.warning("room %s: the %s compaction did not compact (%s); not tried again before the next turn", self.name, why, t.outcome)
        except Exception as e:
            log.warning("room %s: the %s compaction failed (%s); not tried again before the next turn", self.name, why, e)

    def _fork_off(self, why: str = "a turn ended", at: int | None = None) -> None:
        """Under the lock, a turn ended in the copy's band (_ended): the room's session copied (--fork-session) and compacted
        off to the side, on the live process's own spec and account, so the copy's requests read the prompt cache the live
        session warmed and its one hook (SessionStart compact: the recall pack added again) answers as the room's. One copy
        per growth: none while one compacts or waits to be taken; never without a live process to copy from. A quiet room's
        idle compaction comes here too (idle_compact), `why` saying so in the line, and a turn mid-turn (_midturn), `at`
        the size its call started at, for the line, which else gives the last turn's end."""
        if self.fork is not None or self.ready is not None or not (self.rp and self.rp.alive()):
            return
        live, self.tail, self.overtook, self.dropped, self.shrunk, self.midturn_of = self.rp, [], 0, "", False, ""
        self.fork = rp = RoomProcess(replace(live.spec, fast=False), live.account, self.state, ledger=self.ledger,   # never fast:
                                     failures=self.failures,                                                       # credits
                                     log=self.log_dir / f"{self.name}.jsonl" if self.log_dir else None,
                                     meters=self.meters.seen if self.meters else None)
        n = self.context if at is None else at
        log.info("room %s: %s at %d of its %d tokens, %d under Claude Code's line, compacting a copy", self.name, why,
                 n, self.window, self.window - RESERVE - n)
        threading.Thread(target=self._precompact, args=(rp, self.runs), name=f"precompact-{self.name}", daemon=True).start()

    def _precompact(self, rp: RoomProcess, runs: int) -> None:
        """The copy's /compact, the room free meanwhile: a turn that starts runs on the live session and the copy goes on.
        Compacted, it is taken in one step under the lock (_take) at once when no turn runs, else held (`ready`) and taken
        as the running turn ends, before a message waiting runs (_run). Dropped, its transcript copy with it, when its own
        compaction failed, when a change ended it (`runs`: a settings change, /new, a held compaction, the daemon's stop;
        _drop_fork), or when the next run starts a fresh session; the next turn's end may copy again. The live session never
        waits on it (seen live: a message within a minute of each answer dropped nine copies in a row
        while a copy took about two minutes, and the tenth turn's end compacted the live session with the room held)."""
        started, t, why = time.time(), None, ""
        try:
            rp.start(fork=True)
            t = rp.read_turn(rp.send("/compact"), self.turn_timeout, record=False)
            if t.compacted is not None:           # asked here, the copy idle and the room free; _take holds the lock
                t.context = rp.size() or t.context
        except Exception as e:
            why = f"its compaction failed: {e}"
        took = None
        with self.lock:
            ok = t is not None and t.compacted is not None and t.outcome in ("ok", "empty")
            why = next((w for w, hit in ((self.dropped or "a change ended it", self.runs != runs or self.fork is not rp),
                                         (why or f"its compaction failed ({t.outcome if t else 'no turn'})", not ok)) if hit), "")
            why = why or self._stale(rp)
            if self.fork is rp:
                self.fork = None
                if why:
                    self.tail, self.overtook, self.midturn_of = [], 0, ""
            if not why and t is not None:
                if self.busy or self.queue:
                    self.ready = (rp, t, started)     # _run takes it as the running turn ends
                else:
                    took = self._take(rp, t, started)
        if why or t is None:
            self._dropped(rp, t, started, why)
        elif took:
            self._took(*took)
        else:
            log.info("room %s: compacted on a copy in %.0f s while a turn runs; taken as it ends", self.name, time.time() - started)

    def _stale(self, rp: RoomProcess) -> str:
        """Under the lock, why a compacted copy cannot become the room's session ("" when it can): the daemon is stopping,
        the next run starts a fresh session, a turn's run started one (the room's bound session is no longer the one it
        copied), or the live session compacted meanwhile (Claude Code's own inside a turn, or the overflow path's)."""
        origin = str(rp.fork_of or "")
        bound = str((SessionTable(self.state / "sessions.json").get(self.name) or {}).get("session_id") or "")
        return next((w for w, hit in (("the daemon is stopping", self.closing),
                                      ("the next run starts a fresh session", bool(self.fresh_next or (self.state / "rooms" / self.name / "fresh-next").is_file())),
                                      ("a turn started a fresh session", bound != (Path(origin).stem if origin.endswith(".jsonl") else origin)),
                                      ("the live session compacted meanwhile", self.shrunk)) if hit), "")

    def _take(self, rp: RoomProcess, t: Turn, started: float) -> tuple[Any, ...]:
        """Under the lock, a compacted copy becomes the room's session in one step, no model call made (adopt: bound, the
        room's process from here); what the live session said since it was made waits for the copy's first message, once,
        in the room's folder (`copy-tail`: a restart before that message keeps it). Where fast mode reaches the spec the room
        last ran (`ran`; spec() mints a new token) the copy, launched without it, ends between turns as an idle process does,
        and the next message resumes its session on a process launched with it (_start). The rest is _took's, unlocked."""
        rp.evidence = self.evidence
        rp.adopt()
        old, self.rp, self.ready = self.rp, rp, None
        before, self.context, self.grown, self.last_active = self.context, t.context or self.context, False, time.time()
        tail, turns = self._tail(), self.overtook
        self.tail, self.overtook, self.midturn_of = [], 0, ""
        path = self.state / "rooms" / self.name / "copy-tail"
        if tail:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_private(path, json.dumps({"session_id": rp.session_id, "text": tail}))
        else:
            path.unlink(missing_ok=True)
        if end := self.ran is not None and fast_reaches(self.ran):   # last, as the idle close: nothing that can fail
            self.rp = None                        # stands between this and the close (_took), and _start waits for it:
            self.exited.clear()                   # the copy holds its session's lock
        return rp, t, started, old, before, turns, len(tail), end

    def _took(self, rp: RoomProcess, t: Turn, started: float, old: RoomProcess | None, before: int, turns: int, tail: int, end: bool) -> None:
        """A take's row and line: how long the compaction took, how long it waited for a turn, how many turns overtook the
        copy and the tail's size; the old process closed off the room's path, so a message waiting never waits on it."""
        took, waited = (t.ended or time.time()) - started, time.time() - (t.ended or time.time())
        t.outcome = "compacted"
        try:
            rp.record(t, note=f"copy taken: compacted in {took:.0f} s, taken {waited:.0f} s later, {turns} turn(s) overtook it, "
                              f"tail {tail} chars")     # the copy's row, its size after: the room's session from here
            log.info("room %s: compacted on a copy in %.0f s, %d to %d tokens, session %s now the room's (was %s), taken %.0f s "
                     "later; %d turn(s) overtook it, the tail %d chars", self.name, took, before, self.context, rp.session_id,
                     old.session_id if old else "closed", waited, turns, tail)
            if old and old is not rp:
                threading.Thread(target=old.close, name=f"precompact-old-{self.name}", daemon=True).start()
        finally:                                  # a row or a thread that failed costs its turn, never the room: the copy
            if end:                               # still ends and its session's lock is freed
                try:
                    rp.close()
                finally:
                    self.exited.set()
                log.info("room %s: the copy runs without fast mode, so it is closed; the next message resumes it with fast mode", self.name)

    def _dropped(self, rp: RoomProcess, t: Turn | None, started: float, why: str) -> None:
        """A copy the room did not take: ended, its transcript copy removed, the line and (when its /compact ran, the daemon
        not stopping) the row say why."""
        rp.discard()
        log.info("room %s: the compaction on a copy is dropped after %.0f s: %s", self.name, time.time() - started, why)
        if t is not None and not self.closing:
            t.outcome = "dropped"
            rp.record(t, note=f"copy dropped after {time.time() - started:.0f} s: {why}")

    def _drop_fork(self, why: str) -> None:
        """Under the lock, the live session is about to change (a settings change, /new, a held compaction, the daemon's
        stop): a copy being compacted, or one compacted and waiting for the running turn's end, is stale from here, so it
        ends now (a running one's thread drops it, saying `why`) instead of finishing a summary nobody takes. A turn that
        starts is no change: it runs on the live session and the copy goes on (_precompact)."""
        self.runs += 1
        if (rp := self.fork) is not None:
            self.fork, self.dropped = None, why
            threading.Thread(target=rp.end_now, name=f"precompact-end-{self.name}", daemon=True).start()
        if (ready := self.ready) is not None:
            self.ready = None
            threading.Thread(target=self._dropped, args=(ready[0], ready[1], ready[2], why), name=f"precompact-end-{self.name}",
                             daemon=True).start()
        self.tail, self.overtook, self.midturn_of = [], 0, ""

    def _said(self, covered: list[Inbound], reply: str, at: float) -> None:
        """Under the lock, a result delivered on the live session while a copy compacts or waits to be taken: its exchange,
        for the copy's tail; `ran`: the turn the copy was made in, mid-turn (_midturn), which the copy holds cut off."""
        if (self.fork is not None or self.ready is not None) and covered:
            self.tail.append({"messages": [(m.id, m.sender, m.received, m.text) for m in covered], "at": at, "reply": reply,
                              "ran": bool(self.midturn_of) and self.midturn_of == self.turn_key})

    def _tail(self) -> str:
        """Under the lock, what the live session said since the copy was made, as the copy's first message carries it: one
        heading (TAIL_HEAD), then each message and the room's reply with their times, word for word from the room's own
        record. Past `copy_tail` chars the newest exchanges stay whole and the oldest are named by their day-log lines
        (the daemon's cite), else by their times. "" when no turn overtook the copy. A copy made mid-turn (_midturn) has
        the turn it was made in first, under TAIL_RAN and whole whatever its size, counted against `copy_tail`: the copy
        holds that turn cut off, under a summary that says to pick it up, so its reply is what says it was answered."""
        def at(x: float) -> str:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x))

        def whole(x: dict[str, Any]) -> str:
            return "\n".join([*([TAIL_RAN] if x.get("ran") else []), *(f"[{at(w)}] {who}: {text}" for _, who, w, text in x["messages"]),
                              f"[{at(x['at'])}] the room: {x['reply']}"])
        if not self.tail:
            return ""
        ran = [whole(x) for x in self.tail[:1] if x.get("ran")]   # never cut: the cut starts at the exchange after it
        kept: list[str] = []
        size = len(TAIL_HEAD) + sum(len(x) + 2 for x in ran)
        for x in reversed(self.tail[len(ran):]):
            part = whole(x)
            if kept and size + len(part) + 2 > self.copy_tail:
                break
            kept.insert(0, part)
            size += len(part) + 2
        cut = self.tail[len(ran):len(self.tail) - len(kept)]
        named = [f"- [{at(w)}] {who}: {(self.cite(mid, w) if self.cite else '') or 'in the day log at that time'}"
                 for x in cut for mid, who, w, _ in x["messages"]]
        return "\n\n".join([TAIL_HEAD, *ran, *(["\n".join([TAIL_CUT, *named])] if named else []), *kept])

    def _due(self) -> tuple[RoomProcess, Turn, float] | None:
        """Under the lock, at a turn's end: the copy that compacted while it ran (`ready`), taken off it."""
        due, self.ready = self.ready, None
        return due

    def _tail_for(self, sid: str) -> tuple[str, Path] | None:
        """The tail a taken copy's first message carries (_take), for session `sid`; a file for another session is stale (a
        fresh start, /new) and goes."""
        path = self.state / "rooms" / self.name / "copy-tail"
        try:
            held = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if str(held.get("session_id")) != sid or not held.get("text"):
            path.unlink(missing_ok=True)
            return None
        return str(held["text"]), path

    def _compact_held(self, why: str) -> Turn:
        """/compact in the room's session (resumed when its process is gone) while the caller holds the room busy. A message
        that came in meanwhile was queued, its card up from then (submit): it runs right after, and its wait is logged."""
        started = time.time()
        try:
            with self.lock:
                alive = self.rp is not None and self.rp.alive()
                self._drop_fork(f"the {why} compaction of the live session started")
            if not alive:
                self._start(self._account())
            t = self._slash_compact()
            before, self.context = self.context, t.context or self.context
            log.info("room %s: compact (%s) ended %s in %.0f s, %d to %d tokens", self.name, why, t.outcome, time.time() - started, before, self.context)
            return t
        finally:
            with self.lock:
                waiting, self.last_active, self.shortening = list(self.queue), time.time(), False
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
                self._write(msg, folded=True)
                self.ledger.inbound_set(msg.id, state="running", folded=1, attempts=msg.attempt + 1)
                return "folded"
            self.queue.append(msg)
            if self.shortening and not self.early:   # waiting on a compaction: its card says so from now, its turn takes it over
                self.early = True
                self._new_card(words.COMPACTING)
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
        """(name, path) of the images, voice notes and videos sent in this result that the process never Read, transcribed or
        watched."""
        with self.lock:
            read = self.rp.read_paths if self.rp else set()
            return [(n, p) for p, n in self.to_open.items() if p not in read]

    def unfiled(self) -> list[tuple[str, str]]:
        """(name, path) of the pictures sent to this room in this result that it Read and did not file with `seen`."""
        with self.lock:
            read = self.rp.read_paths if self.rp else set()
            return [(n, p) for p, n in self.to_open.items() if p in read and pictures.is_picture(Path(p))
                    and not pictures.sidecar(Path(p)).exists()]

    def stop_check(self, active: bool) -> str | None:
        """The Stop hook's rule, no judge: an image sent in and never read, a voice note never transcribed or a video never
        watched bounces once, naming it, so a room never answers a note it did not hear or a video it did not see; so does a
        picture read and not filed."""
        unread, unfiled = self.unopened(), self.unfiled()
        if not (unread or unfiled) or active or self.bounced:
            return None
        self.bounced = True
        heard, watched = [x for x in unread if transcribe.is_audio(x[0])], [x for x in unread if watch.is_video(x[0])]
        seen = [x for x in unread if x not in heard and x not in watched]
        return " ".join(([f"You were sent {', '.join(n for n, _ in seen)} and have not opened it. Read {' '.join(p for _, p in seen)} "
                          "with the Read tool, then answer with what it shows."] if seen else []) +
                        ([f"You were sent {', '.join(n for n, _ in heard)}, a recording, and have not transcribed it. Call transcribe "
                          f"on {' '.join(p for _, p in heard)}, then open your answer with what it says."] if heard else []) +
                        ([words.UNWATCHED.format(names=", ".join(n for n, _ in watched), paths=" ".join(p for _, p in watched))]
                         if watched else []) +
                        ([words.UNSEEN.format(names=", ".join(n for n, _ in unfiled), paths=" ".join(p for _, p in unfiled))]
                         if unfiled else []))

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
        if active or not text or text.strip() == words.NO_REPLY or self.stub_bounced or not calls or (   # NO_REPLY: filed, nothing to say
                tool := trail[calls[-1]][1].rsplit("__", 1)[-1]) not in FILING:
            return None
        n, above = len(text.strip()), max((len(t.strip()) for kind, t in trail[:calls[-1]] if kind == "text"), default=0)
        found = (f"{above} characters above the {tool} call, {n} after it" if above > STUB_CHARS and above >= 2 * n else
                 f"{n} characters after the {tool} call, opening as a filing" if n < STUB_CHARS and words.FILED.match(text) else "")
        self.stub_bounced = bool(found)
        return (words.STUB.format(head=" ".join(text.split())[:80]), found) if found else None

    def filing_check(self, active: bool, text: str | None, docs: Path | None = None) -> tuple[str, str] | None:
        """The Stop hook's rule after the stub rule, no judge (2026-11-09, the owner: a rule rewritten, then "Filed: ..." told
        back): in a result that called one of FILING, a final holding a filing report (words.FILING_REPORT: a line opening Filed
        or Logged, "the rule now says", a rules file's cite restated) bounces once with the Claude docs' readability sentence,
        read off the fetched page under `docs`, the rest of the answer kept: (the bounce, what was found), or None."""
        from . import shape
        with self.evidence.lock:
            trail = list(self.evidence.trail)
        if active or not text or text.strip() == words.NO_REPLY or self.filing_bounced or self.stub_bounced or not any(
                kind == "tool" and t.rsplit("__", 1)[-1] in FILING for kind, t in trail):
            return None
        if not (hit := words.FILING_REPORT.search(text)):
            return None
        self.filing_bounced = True
        line = text[text.rfind("\n", 0, hit.end() - 1) + 1:].split("\n", 1)[0].strip()      # the line the report ends on
        return (words.FILING_TOLD.format(head=" ".join(line.split())[:80], sentence=shape.sentence("reader", docs).rsplit(" (", 1)[0]),
                f"a filing report: {' '.join(line.split())[:80]}")

    def note_check(self, active: bool, text: str | None) -> tuple[str, str] | None:
        """The Stop hook's rule after the stub rule, no judge (seen live: a turn with no thinking wrote its plan for the
        reply above the answer, and the room posted both whole): the final's first paragraph, at most NOTE_CHARS, is a note to
        itself when it holds a rulebook tag (RULEQUOTE), or, with a paragraph after it, a directive at a sentence start
        (DIRECTIVE) and a reply-shape word (SHAPE) and no second-person word (YOU), since advice to the owner on their own
        message says you or your. It bounces once, quoting the note's start: (the bounce, what was found), or None."""
        if active or not text or text.strip() == words.NO_REPLY or self.note_bounced:
            return None
        p1, *rest = re.split(r"\n\s*\n", text.strip())
        told = bool(rest) and bool(DIRECTIVE.search(p1) and SHAPE.search(p1)) and not YOU.search(p1)
        hits = [w for w, hit in (("a directive and a shape word", told), ("a rulebook tag", RULEQUOTE.search(p1))) if hit]
        if len(p1) > NOTE_CHARS or not hits:
            return None
        self.note_bounced = True
        return words.NOTE.format(head=" ".join(p1.split())[:80]), f"a {len(p1)}-character note before the answer: {', and '.join(hits)}"

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

    def retired_check(self, active: bool, text: str | None, marks: tuple[str, ...]) -> tuple[str, str] | None:
        """The rule after the board rule, no judge, in every room without people (an owner room, the machine room; a room
        with people keeps the board rule alone): a final holding one of the install's retired `marks` (yerkd.toml [gates]
        retired_marks, none by default, and then it never bounces) anywhere in a line outside a fenced block (an unclosed one
        runs to the end, as memory.fenced reads it) bounces once, saying how many lines and which marks: (the bounce, what
        was found), or None."""
        if active or not text or not marks or self.retired_bounced or self.conf.get("people"):
            return None
        plain = re.sub(f"{memory.FENCE}.*?(?:{memory.FENCE}|\\Z)", lambda m: "\n" * m[0].count("\n"), text.replace("\ufe0f", ""), flags=re.S)
        hit = [x for x in plain.splitlines() if any(m in x for m in marks)]
        self.retired_bounced = bool(hit)
        found = f"{len(hit)} line{'s' * (len(hit) != 1)} with {' '.join(m for m in marks if any(m in x for x in hit))}"
        return (words.RETIRED.format(found=found), found) if hit else None

    def paste_check(self, active: bool, text: str | None,
                    earlier: Callable[[], dict[str, tuple[str, str]]]) -> tuple[str, str] | None:
        """The paste rule, no judge, in the `send` tool before it posts and as the Stop hook's fifth rule, one flag for both
        (2026-10-22: a block posted the evening before went out again through `send`, word for word, its "tomorrow" and its
        times that day's): a fenced block that memory.fenced() makes equal to one `earlier` holds (the daemon's view of the
        room's earlier days, asked for only when the text holds three backticks) is turned back once, naming when it first
        went out: (the bounce, what was found), or None."""
        if active or not text or self.paste_bounced or memory.FENCE not in text:
            return None
        view = earlier()
        if (hit := next((b for b in memory.fenced(text) if b in view), None)) is None:
            return None
        self.paste_bounced = True
        day, at = view[hit]
        when, n = f"{calendar.month_abbr[int(day[5:7])]} {int(day[8:10])} at {at[:5]}", hit.count(" / ") + 1
        return words.PASTE.format(when=when), words.PASTE_FOUND.format(n=n, s="s" * (n != 1), when=when)

    def in_owner_name(self) -> bool:
        """What this room ships goes out in the owner's name: an owner room (rooms.toml privacy, "owner" when unset), no people."""
        return str(self.conf.get("privacy", "owner")) == "owner" and not self.conf.get("people")

    def sheet_check(self, active: bool, text: str | None, file: str = "", stop: bool = False) -> tuple[str, str] | None:
        """The sheet rule, no judge, in an owner's room (a person's room keeps the shadow row), in `send` before it posts and
        at Stop (`stop`), one flag for both (2026-10-22: eight sends in the owner's name in one morning with no claims sheet,
        each logged, none stopped): a document (evidence.DOCUMENT) or a pasted block (evidence.pasted) with no sheet written
        this turn (evidence.sheet_written, the shadow rule's test, also looking beside the file, else in the work folder) is
        turned back once, saying what the sheet is and where it goes: (the refusal, what was found), or None."""
        doc, paste = [Path(file).name] if file.lower().endswith(DOCUMENT) else [], bool(text) and pasted(str(text))
        if active or self.sheet_bounced or not self.in_owner_name() or not (doc or paste):
            return None
        folder = Path(file).expanduser().resolve().parent if file else (self.state / "rooms" / self.name / "work").resolve()
        if sheet_written(self.evidence, folder):
            return None
        self.sheet_bounced = True
        done, again, nxt = words.SHEET_STOP if stop else words.SHEET_SEND
        what = " and ".join([*doc, *([f"the fenced block in {'your final message' if stop else 'this message'}"] if paste else [])])
        where = f"{folder / (Path(file).stem + '-claims.md')}, beside it" if doc else str(folder / "claims.md")
        return (words.SHEET.format(done=done, what=what, where=where, thing="document" if doc else "block", again=again, next=nxt),
                f"{shipped(doc, paste)} with no claims sheet written this turn")

    def said_check(self, active: bool, text: str | None, said: Callable[[], str] = str,
                   owner: tuple[str, ...] = ()) -> tuple[str, str, str] | None:
        """The person and quote rules, no judge, in an owner's room and a person's (an owner's room with people keeps the
        shadow rows), in `send` before it posts and at Stop, one flag for both (2026-10-22: what a colleague said, decided or
        wrote went out from turns that read nothing): a third person's words, decision or wants (evidence.people) or a quote no
        read, prompt or earlier reply holds (evidence.quotes; `said` gives the session's earlier replies, asked for past the
        guards) from a turn with no reading call (evidence.read_any) is turned back once, naming whose: (the refusal, its shadow
        rule, what was found), or None. `owner`: the names the owner's words go by, their own only in their rooms; a person's room is
        pointed at its own sources (words.SAID_HERE), never a file outside its layer."""
        mine, person = self.in_owner_name(), str(self.conf.get("privacy", "owner")) != "owner"
        if active or self.said_bounced or not text or not (mine or person) or read_any(self.evidence):
            return None
        owner = owner if mine else ()        # in a person's room the owner is a third person: their words need a read too
        found = [(who, "person-refused", d) for who, d in people(text, self.evidence, owner)]
        found += [(who, "quote-refused", d) for who, d in quotes(text, self.evidence, said() if QUOTED.search(text) else "", owner)]
        if not found:
            return None
        self.said_bounced = True
        who, rule, detail = found[0]
        return (words.SAID if mine else words.SAID_HERE).format(name=who[:1].upper() + who[1:]), rule, detail

    def fold_check(self, active: bool, text: str | None) -> tuple[str, str] | None:
        """The Stop hook's last rule, no judge (seen live: a number fixed 0.8 s after the question it corrected folded
        into that turn, and the one reply covering both answered the first and never said the second): a person's message
        that folded into this result and was taken in (its marked text in a replay of the result being read), whose content
        tokens (fold_tokens) the final carries none of, bounces once, quoting it: (the bounce, what was found), or None. A
        fold Claude Code runs as its next result is that result's own message, never checked; nor is a final of NO_REPLY."""
        if active or not text or text.strip() == words.NO_REPLY or self.fold_bounced:
            return None
        with self.lock:
            taken = [f"\n{r}\n" for r in self.replays]
            folds = [m for m in self.folds if m.sender != HARNESS and any(f"\n{m.sent}\n" in r for r in taken)]
        for m in folds:
            if (tokens := fold_tokens(m.text)) and not any(carries(text, t) for t in tokens):
                self.fold_bounced = True
                return (words.FOLD.format(text=" ".join(own_words(m.text).split())[:60]),
                        f"message {m.id} folded in with {', '.join(sorted(tokens)[:5])}, and the final names none of it")
        return None

    def _new_card(self, note: str = "", edit: bool = False) -> None:
        privacy = str(self.conf.get("privacy", "owner"))
        self.card = Card(thinking=flag(self.conf.get("thinking_panel")) and privacy == "owner",
                         people=self.name if self.conf.get("people") and self.conf.get("card") == "count" else "")   # only "count": the short card
        self.card.narration = note                # a harness line from the first post, until the model's first words
        self.turn_key, self.bounced, self.silent_bounced, self.stub_bounced = uuid.uuid4().hex[:12], False, False, False
        self.note_bounced = self.board_bounced = self.retired_bounced = self.paste_bounced = self.sheet_bounced = self.said_bounced = False
        self.filing_bounced = False
        self.fold_bounced = False
        if edit:                                  # the card its message got while it waited on a compaction (submit)
            self.channel.progress(self.name, self.card.render())
        else:
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
            self.exited.clear()
        try:
            rp.close()
        finally:
            self.exited.set()
        log.info("room %s: idle process closed, session %s kept", self.name, rp.session_id)
        return True

    def respec(self) -> None:
        """The room's model, effort or account changed: never mid-turn. With no turn running the process closes now, else
        when the turn ends; the next message resumes the same session on the new spec (another account: as a hop does)."""
        with self.lock:
            self.stale = self.busy
            rp, self.rp = (None, self.rp) if self.busy else (self.rp, None)
            self._drop_fork("the room's model, effort or account changed")   # a copy runs on the old spec
            if rp:
                self.exited.clear()
        if rp:
            try:
                rp.close()
            finally:
                self.exited.set()

    def renew(self) -> None:
        """/new: the room's next run (the next message, or a running turn's messages still waiting) starts a
        fresh session (memory stays: its start is built as every start is) with the room's recap line. The
        line is also the daemon's own file, `fresh-next` beside the room's override, so a restart before the
        next message keeps it."""
        with self.lock:
            self._drop_fork("/new: the next run starts a fresh session")
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
            self._drop_fork("the daemon is stopping")
        if rp:
            if busy:
                rp.end_now()
            rp.close()                            # and the session's lock freed: the next start resumes it
        return busy

    # the room's thread
    def _write(self, msg: Inbound, recap: str | None = None, folded: bool = False) -> None:
        """`folded`: submit's path, a turn being read. The fold's line goes ahead of the text, decided here and never kept on
        the message, so the same message written again (a rerun, its own turn after a failed one) goes bare; a message from
        the channel the room watches says how it is answered (words.THERE) and goes bare too. Its seconds are since the
        message written before it (the person's previous one, unless the turn is a relay's)."""
        assert self.rp
        if folded and not msg.text.startswith(words.THERE):
            above = self.inflight[-1].received if self.inflight else (msg.prev_human or msg.received)
            recap = words.FOLDED.format(n=max(round(msg.received - above), 0))      # placed as a recap is
            self.folds.append(msg)
        self.pending.append(msg)
        self.inflight.append(msg)
        for f in msg.files:
            kind, name = str(f.get("type", "")), str(f.get("name", ""))
            if f.get("ok") and (kind.startswith("image/") or transcribe.is_audio(name, kind) or watch.is_video(name, kind)):
                self.to_open[str(f["path"])] = name
        text = f"{recap}\n\n{msg.text}" if recap else msg.text
        if recap:
            self.seen[text.strip()] = msg
        msg.sent = text
        self.rp.send(text)

    def _start(self, account: Account, fresh: bool = False) -> None:
        self.exited.wait(30)                      # a message that came in while the idle close still waited for the old process
        log_path = self.log_dir / f"{self.name}.jsonl" if self.log_dir else None
        spec = self.spec()
        if self.stale and self.ran:              # a hop or retry inside the turn that changed the settings
            spec.model, spec.extra_args = self.ran.model, self.ran.extra_args       # stays on the turn's own
        spec.model = str(self.conf["model"]) if self.out_of else spec.model   # every account out of the pinned model: the room's own
        self.ran = spec
        rp = RoomProcess(spec, account, self.state, ledger=self.ledger, failures=self.failures, log=log_path,
                         meters=self.meters.seen if self.meters else None, evidence=self.evidence)
        rp.start(fresh=fresh)
        if lost := rp.fast_lost:                 # its /fast on got no result: a late one would be read as the next line's reply,
            rp.close()                           # so the room runs on a process launched as on main, without fast mode
            spec.fast = False
            rp = RoomProcess(spec, account, self.state, ledger=self.ledger, failures=self.failures, log=log_path,
                             meters=self.meters.seen if self.meters else None, evidence=self.evidence)
            rp.start(fresh=fresh)
        self.rp = rp
        self.hooked = self.hooks_warned = False   # per process start: its first hook call proves the hooks alive; the notice went out
        self._fast_said(rp, words.FAST_NO_ANSWER if lost else "", at_start=True)

    def _fast_said(self, rp: RoomProcess, note: str = "", at_start: bool = False) -> None:
        """What Claude Code answered this process's own /fast on (engine.RoomProcess._fast_on), or the notice it streams once
        a turn while the account is out of usage credits (`note`): a refusal is said in the room once, in one line, until fast
        mode is on again; the reply still comes, at standard speed. Its answer is read `at_start` only: a later result's
        state (cooldown, its rate limit's pause) is no refusal."""
        self.fast_state = rp.fast_state or self.fast_state
        refused = note or (rp.fast_said if at_start and rp.fast_said and rp.fast_state != "on" else "")
        if rp.fast_said and rp.fast_state == "on" and not note:
            self.fast_told = False
        elif refused and not self.fast_told:
            self.fast_told = True
            log.info("room %s: fast mode is not running: %s", self.name, refused[:200])
            self.channel.notice(self.name, f"{words.WORKING} {words.FAST_LOST.format(why=words.fast_why(refused, rp.account.name))}",
                                [], key=f"{self.turn_key}:fast")

    def account_now(self) -> Account:
        """The account the room's process runs on, else the one its next turn would pick (/fast asks Claude Code on it)."""
        with self.lock:
            rp = self.rp
        return rp.account if rp else self._account(str(override(self.state, self.name).get("model") or self.conf.get("model") or ""))

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
        self._note(words.out_of(usual, model, back) if str(self.conf.get("privacy", "owner")) == "owner" else words.MOVED_ON)
        return usual, self._account(usual)

    def _run(self) -> None:
        batch: list[Inbound] = []
        try:
            while True:
                with self.lock:
                    due, took = self._due(), None        # a copy compacted while the turn ran: taken before anything queued
                    if due and not (stale := self._stale(due[0])):
                        took = self._take(*due)
                    elif due:
                        threading.Thread(target=self._dropped, args=(*due, stale), name=f"precompact-end-{self.name}", daemon=True).start()
                    batch = []
                    if not self.queue:
                        self.busy = self._ended()        # near Claude Code's line: compacted now, in the background
                        self.last_active = time.time()
                    else:
                        batch = [self.queue.popleft()]
                        while not batch[0].alone and self.queue and not self.queue[0].alone:
                            batch.append(self.queue.popleft())
                if took:
                    self._took(*took)
                if not batch:
                    return
                try:
                    self._turn(batch)
                finally:                         # a turn that raised has ended too
                    with self.lock:              # the settings changed in that turn: it has ended, so its
                        rp, self.rp, self.stale = (self.rp, None, False) if self.stale else (None, self.rp, False)
                    if rp:                       # process goes and the next message resumes on the new spec
                        rp.close()
                        log.info("room %s: settings changed, process closed, session %s kept", self.name, rp.session_id)
        except Exception as e:
            log.exception("room %s: turn crashed", self.name)
            with self.lock:
                self.busy = self.reading = False
            if batch and not self.closing:        # said in the room, its rows closed (disk-full drill c: a card left at
                with contextlib.suppress(Exception):   # "working on it" for good); when the ledger or the channel is what
                    from .channels.discord import DeliveryFailed     # failed, the rows stay open for the next start; a reply
                    why = "undelivered" if isinstance(e, DeliveryFailed) else None   # Discord never took is said so (imported
                    self._end(batch, words.why(why, "crashed"), "crashed")         # here: discord.py imports this module)

    def _on_event(self, turn: Turn, ev: dict[str, Any]) -> None:
        """The card from the stream: narration, thinking line, tool rows; the result's replays, for the fold rule; the size
        each of the room's calls starts at, once it is the turn's most yet, for a copy started mid-turn (_midturn)."""
        self.replays = turn.replayed
        if turn.reached > self.midturn_at and self.midturn != "off":
            try:
                self._midturn(turn)
            except Exception:                     # a copy is never worth the turn it would be made in
                log.exception("room %s: the copy mid-turn could not start", self.name)
        if self.card.feed(ev):
            self.channel.progress(self.name, self.card.render())

    def _midturn(self, turn: Turn) -> None:
        """A call of the running turn started at more tokens than any before it in the turn (`reached`, from its message_start;
        the turn's thread, unlocked). Past the start of the copy's band (_ended's own) the copy starts at that call, not at
        the turn's end, which a turn that grows across the whole band in one go reaches only for the held compaction (the
        96 hours measured: 7 of 1,189 turns grew over 100,000, one ended 7,544 under the held line). Once a turn,
        and never while a copy compacts or waits, the daemon stops, the turn was stopped, a compaction holds the room, the
        next run starts a fresh session or no process is alive to copy from; `log` says it would have and starts nothing.
        The turn copied runs on past the copy: it overtakes it, and its exchange comes first in the copy's tail (_said)."""
        with self.lock:
            self.midturn_at = n = turn.reached
            line = self.window - RESERVE
            if (self.midturn_said or not self.window or not self.precompact_within or n < line - self.precompact_within * self.window
                    or self.fork is not None or self.ready is not None or self.closing or self.stopped or self.shortening
                    or self.fresh_next or not (self.rp and self.rp.alive()) or (self.state / "rooms" / self.name / "fresh-next").is_file()):
                return
            self.midturn_said = True
            if self.midturn == "log":
                log.info("room %s: would have started a copy mid-turn at %d of its %d tokens, %d under Claude Code's line, %.0f s "
                         "into the turn", self.name, n, self.window, line - n, time.time() - turn.started)
                return
            self._fork_off("a turn crossed the copy's band mid-turn", at=n)
            if self.fork is not None:
                self.overtook, self.midturn_of = 1, self.turn_key

    def _note(self, line: str) -> None:
        """A harness line on the card (a hop, a compaction) until the model's next words."""
        self.card.narration = line
        self.channel.progress(self.name, self.card.render())

    def _turn(self, batch: list[Inbound]) -> None:
        with self.lock:
            self.to_open, self.stopped = {}, False
            self.midturn_said, self.midturn_at = False, 0   # a copy mid-turn: at most one a turn (_midturn)
            self.halt.clear()
            self.answering = {m.id for m in batch}
            self.overtook += self.fork is not None   # it runs at once on the live session; a copy compacting goes on (_precompact)
            early, self.early = self.early, False   # a card is up for it already: this turn takes it over
        self.evidence.reset()
        ctx = self.state / "rooms" / self.name / "context.md"
        if ctx.is_file():
            self.evidence.give(ctx.read_text(errors="replace"))     # the session-start file was read too
        self._new_card(words.AFTER_FAILED if any(m.after for m in batch) else "", early)
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
            tail = self._tail_for(self.rp.session_id)   # a taken copy's first message: what the live session said meanwhile
            first = "\n\n".join(x for x in (tail[0] if tail else "", note or "") if x) or None
            with self.lock:                       # Room.stop sets `stopped` under this lock: it sees `reading`
                halted = self.stopped             # and TERMs the process, or nothing is written here
                if not halted:
                    self.pending.clear()
                    self.inflight, self.folds = [], []    # written now, with no turn read: none of these is a fold
                    for i, m in enumerate(resend):
                        self._write(m, first if i == 0 else None)
                        self.ledger.inbound_set(m.id, state="running", attempts=m.attempt + 1)
                    if tail:
                        tail[1].unlink(missing_ok=True)   # given once: a rerun of this message goes without it
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
        of its failure: it runs next, in order, as its own turn, its card saying so; /stop stops them all. A copy made in
        this turn (_midturn) goes: it holds the turn cut off under a summary that says to pick it up, and no reply says
        it ended."""
        own = [m for m in resend if m.id in self.answering or state != "failed"] or resend[:1]
        asked = own[0].text if own else ""
        notice_ids = self.channel.notice(self.name, f"{words.NOT_DONE} {words.NOT_ANSWERED}\n{plain}",
                                         [m.id for m in own], card=self.card.owed(plain, asked), key=self.turn_key)
        for m in own:
            self.ledger.inbound_set(m.id, state=state, note=why, reply_ids=",".join(notice_ids))
            self._then(m, "failed", notice_ids, 0)
        with self.lock:
            old, self.rp, self.inflight, self.folds = self.rp, None, [], []
            self.pending.clear()
            self.queue.extendleft(reversed([replace(m, after=True) for m in resend if m not in own]))
            if self.midturn_of and self.midturn_of == self.turn_key:
                self._drop_fork("the turn the copy was made in ended unanswered")
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
        fast = over.pop("fast", None) is True     # off its model, fast mode goes with it, as /model's does (Yerkd.tool_settings)
        write_private(self.state / "rooms" / self.name / "override.json", json.dumps(over))
        log.info("room %s: %s was not found, the override's model dropped", self.name, bad)
        return words.BACK_ON.format(model=words.model_name(bad), usual=words.model_name(usual) if usual else words.USUAL) + (
            f"\n{words.FAST_OFF}" if fast else "")

    def _results(self, account: Account, hop: str | None, recapped: bool) -> tuple[str, Turn | None]:
        """Deliver each result of the running process until nothing waits: "done"; a failed turn:
        "failed"; a reply on a fallback model while messages still wait: "fresh"."""
        started = time.time()
        while True:
            assert self.rp
            if self.stopped:                      # /stop came between two runs: this one ends as it starts
                self.rp.end_now()
            turn = self.rp.read_turn(started, self.turn_timeout, record=False, on_event=self._on_event)
            self._fast_said(self.rp, turn.fast_note)
            if turn.context:                      # the session's size now: the idle compaction's to read
                self.context, self.grown = turn.context, True
            self.shrunk = self.shrunk or turn.compacted is not None   # Claude Code compacted it inside the turn
            if self.stopped and turn.outcome != "ok":
                turn.outcome, turn.failure = "stopped", None
            with self.lock:
                n = max(covered_by(turn.replayed, [m.sent for m in self.pending]), len(turn.accepted)) or (1 if turn.outcome == "ok" else 0)
                covered = [self.pending.popleft() for _ in range(min(n, len(self.pending)))]
                self.folds = []                   # its Stop has run: a fold it did not take opens the next result
            ids = [m.id for m in covered]
            turn_id = self.rp.record(turn, ids, hop=hop, recap=1 if recapped else None, turn_key=self.turn_key, out_of=self.out_of or None,
                                     note=COMPACTED_IN_TURN if turn.compacted is not None else None)   # the room card counts it
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
            if turn.fallback_model:                # which model: the turn's ledger row; the fresh start, failures.toml's action
                fresh = turn.failure is not None and turn.failure.action == "fresh_next_turn"
                card = f"{words.WORKING} {words.BACKUP[0]}" + (f"\n{words.SUB}{words.BACKUP[1]}" if fresh else "")
                if fresh:
                    self.fresh_next = REFUSAL_RECAP
            text = turn.text
            if covered and all(m.cap for m in covered):         # harness messages only: held to their cap
                text = fit(text, min(m.cap for m in covered), [m.must for m in covered])
            if quiet := text.strip() == words.NO_REPLY:       # read, nothing to add (a watched channel's message): no post
                head = f"{words.NOT_DONE} {words.NO_VERDICT}" if self._owed_verdict(ids) else f"{words.DONE} {words.QUIET}"
                reply_ids = self.channel.silent(self.name, ids, self.card.render(head=head), key=self.turn_key)
            else:
                reply_ids = self.channel.reply(self.name, text, ids, card, key=self.turn_key)
                self.last_reply = text.strip().split("\n")[0][:160]
            now = time.time()
            for m in covered:
                self.ledger.inbound_set(m.id, state="answered", answered=now, turn_id=turn_id, reply_ids=",".join(reply_ids),
                                        **({"note": "silent"} if quiet else {}))
                self._then(m, "answered", reply_ids, len(text))
            with self.lock:
                self._said(covered, text, now)    # a copy compacting: the exchange rides to it (_tail)
                self.inflight = list(self.pending)
                self.to_open = {}
                if not self.pending or self.fresh_next:
                    self.reading = False          # from here a new message queues for the next turn
                    return ("fresh" if self.pending else "done"), turn
            started, hop, recapped = time.time(), None, False
            self._new_card()

    def _owed_verdict(self, ids: list[str]) -> bool:
        """A `watch_audit` room's turn on a message its inbound row places in the channel the room watches: a verdict was owed,
        so a final of NO_REPLY keeps its card as NO_VERDICT, never as QUIET. A relay's row or a note's is in neither channel."""
        watched = str(self.conf.get("watch") or "").partition(":")[2]
        return self.conf.get("watch_audit") is True and any((self.ledger.inbound_get(i) or {}).get("channel") == watched for i in ids)

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
        if t.outcome == "compacted":              # the whole prompt from here, the row's and _compact_held's
            t.context = self.rp.size() or t.context
        self.shrunk = self.shrunk or t.outcome == "compacted"   # a copy made before it is moot
        self.rp.record(t)                         # read by yerk status --json's failed turns
        return t
