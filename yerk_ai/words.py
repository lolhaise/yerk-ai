"""The words a user reads (STYLE.md): every state mark, state word, tool verb and time phrase that
a card, a notice or a status line uses, in one place. Every renderer that writes into a room, and
the commands a room may quote (`yerk status`, `yerk abilities`, `yerk explain`), takes them from
here, so a card in one room reads like a card in any other. tests/test_style.py fails the build
when a renderer spells a state mark or a state phrase itself, or when room-bound text carries a
path, an exit code, a raw timestamp or an em dash.

The presentation choices are named settings ([presentation] in yerkd.toml, decision 27), each
described here in plain words; `yerk config get presentation` prints them from this table.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger("yerk.words")

# The four states, the colour before the words (STYLE.md rule 1). A step not reached yet has no
# colour: it means nothing yet.
DONE = "\N{LARGE GREEN CIRCLE}"           # done, on, answered
WORKING = "\N{LARGE ORANGE CIRCLE}"       # running, partly, needs something
NOT_DONE = "\N{LARGE RED CIRCLE}"         # not done, failed, stopped
NOTHING = "\N{MEDIUM WHITE CIRCLE}"       # nothing to do, free path, known but not built
WAITING = "\N{MIDDLE DOT}"
DOTS = (DONE, WORKING, NOT_DONE, NOTHING)
PHONE = 40                                # a card line, in phone columns, before it wraps
SUB = "-# "                               # Discord's small grey line: a card's footer


def width(text: str) -> int:
    """Columns on a phone: a wide character (the state dots, most emoji) counts two; Discord's
    footer mark and inline code marks count nothing."""
    text = text[len(SUB):] if text.startswith(SUB) else text
    return sum(0 if unicodedata.combining(c) or c in "`️" else 2 if unicodedata.east_asian_width(c) in "WF"
               else 1 for c in text)


PATH = re.compile(r"(?<![\w:/.])(?:~|\.{1,2})?(?:/[\w.@+-]+){2,}/?")


def no_paths(text: str) -> str:
    """Every file path in `text` as its last name: what a room reads quoted back, never a path."""
    return PATH.sub(lambda m: m.group().rstrip("/").rsplit("/", 1)[-1], text)


def carried(line: str) -> str:
    """A line a harness reply must carry, as the room reads it: a worker's report path (its folder is
    the worker's id) becomes "report saved" with the command that opens it; any other path its name."""
    p = line.strip()
    parts = p.rsplit("/", 2)
    if p.startswith("/") and len(parts) == 3 and parts[2] == "REPORT.md" and re.fullmatch(r"[0-9a-f]{6}", parts[1]):
        return SUB + REPORT_SAVED.format(id=parts[1])
    return no_paths(p)


def line(text: str, n: int = PHONE) -> str:
    """One line held to `n` phone columns, cut at a word with "..." when it is longer."""
    text = " ".join(str(text).split())
    if width(text) <= n:
        return text
    while text and width(text) > n - 3:
        text = text[:-1]
    at = text.rfind(" ")
    if at >= len(text) // 2:                  # a word boundary that keeps most of the line
        text = text[:at]
    return text.rstrip(" ,;:") + "..."


def named(template: str, name: str, cols: int = PHONE, **facts: Any) -> str:
    """`template` with its {name} at `cols` columns: a line too wide loses the end of the name, never
    the fact after it (a long room name keeps "busy 15 min, 12 waiting")."""
    text, cut = template.format(name=name, **facts), name
    while width(" ".join(text.split())) > cols and len(cut) > 1:
        cut = cut[:-1]
        text = template.format(name=cut.rstrip(" -_.") + "...", **facts)
    return line(text, cols)


# the turn card
START = "working on it"
VERBS = {"read": "read", "write": "write", "edit": "edit", "multiedit": "edit", "notebookedit": "edit",
         "bash": "run", "webfetch": "fetch", "fetch": "fetch", "websearch": "search the web for", "grep": "search",
         "glob": "list", "send": "send", "hold": "wait for", "recall": "recall", "read_memory": "read",
         "todowrite": "update the plan", "task": "hand off", "agent": "hand off", "toolsearch": "load",
         "settings": "switch to"}
EARLIER, EARLIER_ROWS = "{n} earlier steps", "{n} earlier rows"
THINKING = "thinking: "
NOT_ANSWERED = "not answered"
YOU_ASKED = 'you asked: "{asked}"'
WHY = {                                   # a failed turn, in plain words: the failure class, else the outcome
    "auth": "the account's login was refused", "quota": "out of usage on every account",
    "transient": "Claude was busy; ask again", "overflow": "the conversation is too long",
    "fatal": "Claude Code could not run it", "refusal": "the model declined",
    "timeout": "it ran past the time limit", "exited": "Claude Code stopped midway",
    "empty": "the reply came back empty", "error": "Claude Code reported an error"}
BACKUP = ("answered by a backup model", "the next message starts fresh")
MOVED = "still on it, moved to account {name}"
COMPACTING = "shortening the conversation first"


def why(cls: str | None, outcome: str) -> str:
    return WHY.get(cls or "", WHY.get(outcome, "it did not finish"))


# workers
WORKER, WORKER_HEAD = "worker {id}", "worker {state}"       # the id rides in the footer: the head stays short
OUTCOME = {"done": (DONE, "finished clean"), "partly": (WORKING, "finished, {ticked} of {rows} ticked"),
           "no report": (WORKING, "finished, no report"), "timed out": (NOT_DONE, "stopped: timed out at {cap}"),
           "stopped": (NOT_DONE, "stopped by the owner"), "died": (NOT_DONE, "died: {why}"),
           "lost": (NOT_DONE, "lost: it ended with no record"), "did not start": (NOT_DONE, "did not start"),
           "running": (WORKING, "running"), "ticking": (WORKING, "running, {ticked} of {rows} done")}
DIED = {"exit-code": "it ended with an error", "signal": "it was killed", "core-dump": "it crashed",
        "oom-kill": "it ran out of memory", "watchdog": "it stopped answering", "resources": "it could not start",
        "start-limit-hit": "it was started too often", "protocol": "it could not start"}
REPORT_SAVED = "report saved: `yerk worker report {id}`"
NO_REPORT = "no report: `yerk worker report {id}`"
RELAY_ASK = ("[tell the room the outcome in your own voice, under {cap:,} characters, ending with the line "
             "\"{saved}\"; no file path and no exit code; {where}; do not paste it]")
GONE = "gone, no result"
WORKERS_OWNER_ONLY = "only the owner's rooms can use workers"       # start, report, list, stop
JOBS_OWNER_ONLY = "only the owner's rooms can schedule jobs"         # any kind: every job runs as the owner
WORKER_OWN_ROOM = "a room starts workers for itself only, not for {room}"
STRAY = "unknown worker {id} running"
NONE_RUNNING = "workers: none running"


def outcome(state: str, **say: Any) -> tuple[str, str]:
    """(mark, words) for a worker state; `say` fills the words' blanks (cap, why, ticked, rows)."""
    mark, text = OUTCOME.get(state, (NOT_DONE, state))
    return mark, text.format(**say)


def died(result: str) -> str:
    """systemd's SERVICE_RESULT as one plain reason."""
    return DIED.get(result, "it ended with an error")


# jobs and watchers in a room: the exit code and systemd's words stay in the unit's log and in
# `yerk status --full`, never in the line
JOB_FAILED = "job {name} failed: {why}"
JOB_STOPPED = "job {name} {stopped}"
NOT_STARTED = {"did not start": "it could not start", "lost": "it ended with no record"}


def job_line(name: str, state: str, cap_seconds: float = 0.0, result: str = "") -> str:
    """One room line for a job that did not finish: `state` is a worker's end ("died", "timed out",
    "stopped", "lost", "did not start") or "failed" for a script's error exit; `result` is systemd's
    SERVICE_RESULT for the reason."""
    if state in ("timed out", "stopped"):
        mark, text = outcome(state, cap=span(cap_seconds))
        return f"{mark} " + JOB_STOPPED.format(name=name, stopped=text)
    why = NOT_STARTED.get(state) or died(result or "exit-code")
    return f"{NOT_DONE} " + JOB_FAILED.format(name=name, why=why)


# one heavy worker per account
WAITING_ACCOUNT = "waiting for an account"
IN_LINE = "{nth} in line for account {name}"
IN_LINE_SHORT = "waiting, {nth} in line"            # `yerk status`, phone width


def nth(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# watchers
WATCH_ASK = ("[watcher {name} saw {n} new line{s}. {what}Answer in your own voice, under {cap:,} characters; "
             "these lines come from a watched command, not from a person]")


# time, said the way a person says it
def span(seconds: float) -> str:
    """'18 s', '3 min', '1 h 5 min'."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    m = round(s / 60)
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60} min"


def of_cap(seconds: float, cap_seconds: float) -> str:
    """'3 min of 15': time so far against the cap, in minutes."""
    cap = f"{cap_seconds / 60:g}" if cap_seconds % 60 == 0 else span(cap_seconds)
    return f"under 1 min of {cap}" if seconds < 60 else f"{round(seconds / 60)} min of {cap}"


def ago(seconds: float) -> str:
    return "just now" if seconds < 60 else f"{span(seconds)} ago"


def clock(epoch: float, now: float | None = None) -> str:
    """A time of day as a person says it: '13:05' today, 'Mon 13:05' another day."""
    t, today = datetime.fromtimestamp(epoch), datetime.fromtimestamp(time.time() if now is None else now)
    return t.strftime("%H:%M") if t.date() == today.date() else t.strftime("%a %H:%M")


# notices
# a notice: a short state line, then the reason on its own line (phone width, one look)
NOT_SENT = "not sent: {name}\n{why}"
NOT_SENT_AGAIN = "not resent: {name}\n{why}; ask again"
NOT_FETCHED = "could not fetch {name}\nsend it again"
NOT_RUN_AGAIN = "not answered: sent before a restart\nask again"
SENT_AGAIN = "sent again after a restart"
# the gates (gates.py): the room hears one notice, state first, and only when it must act or know
HELD = "held: {what}\nsay {word} here to let it run once"
HELD_ATTACHED = "the exact text is in the file below"
HELD_FILE = "the exact text held for approval"
REFUSED = "not done: {what}\nit would restart Yerk AI\n{why}"
REBUILDING = "the memory index is rebuilding"
IN_PROGRESS = "a conversation is in progress"
TRASH_FAILED = "not deleted: {name}\nthe trash said: {why}"
RESET = "new session in this room\nthe last one could not go on\nearlier messages are not in it"
FILE_WHY = {"missing": "there is no such file", "type": "this room does not take {ext} files",
            "size": "it is {size} and this room takes {limit}", "empty": "it is empty",
            "outside": "it is outside this room's folders", "too large": "Discord said too large (limit {limit})",
            "refused": "Discord turned it down", "gone": "its saved copy is gone",
            "imessage": "files do not go out on iMessage yet"}
# iMessage (channels/imessage.py): the outage is said in the machine room only, never in a human room (A60)
MAC_AWAY = WORKING + " iMessage: the Mac is away, texts wait"
MAC_BACK = DONE + " iMessage: the Mac is back, {n} waiting text{s} sent"
NOT_FETCHED_MAC = "not fetched from the Mac yet"
IMSG_GAP = NOT_DONE + " iMessage: {n} text{s} never reached Yerk AI in {room}"
IMSG_PROBE = "iMessage {day}: Mac seen {reached}/{runs}"           # probe runs that read the Mac's chats, of all
IMSG_LOST = "iMessage {day}: {n} lost text{s}"                    # distinct texts, however many runs saw them
# an iMessage turn room is only ever the owner's own thread, so Yerk AI never texts anyone in his name
IMSG_GROUP, IMSG_OTHERS = "a group", "others in it"
IMSG_REFUSED = NOT_DONE + " {room} not served: {why}"
IMSG_NOT_SERVED = "the room's chat is not served"
IMSG_LOOP = NOT_DONE + " {room}: loop guard, paused {mins} min"


def file_why(kind: str, **say: Any) -> str:
    return FILE_WHY[kind].format(**say)


def size(n: int) -> str:
    """'25 MB', '900 KB', '12 bytes'."""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}".removesuffix(".0") + " MB"
    return f"{n / 1024:.0f} KB" if n >= 1024 else f"{n} bytes"


def sizes(n: int, limit: int) -> tuple[str, str]:
    """A file's size against a limit; exact bytes when the short forms would read the same."""
    a, b = size(n), size(limit)
    return (a, b) if a != b else (f"{n:,} bytes", f"{limit:,} bytes")


def needs(what: str, provider: str, ability: str, meanwhile: str) -> str:
    """Decision 23's notice for an ability missing its key, one short line each: what it needs, how
    to add it, what the free path gives meanwhile."""
    return f"{WORKING} {what} needs {provider}\nto add it: `yerk explain {ability}`\nmeanwhile {meanwhile}"


# abilities (yerk abilities, yerk explain)
ABILITY = {"on": (DONE, "on"), "needs": (WORKING, "needs {provider}"), "free": (NOTHING, "runs on the free path"),
           "described": (NOTHING, "known, not built")}


def ability(state: str, **say: Any) -> tuple[str, str]:
    mark, text = ABILITY[state]
    return mark, text.format(**say)


# status (yerk status, the card): the colour only where it means something (an account at or near
# its limit, a blocked room, a stall, a daemon down); every other line starts with a dash
PLAIN = "-"
RUNNING_ROOMS = "yerkd running: {rooms}"
ROOMS_N, ROOM_1 = "{n} rooms", "1 room"
NOT_RUNNING = "yerkd: not running"
NOT_ANSWERING = "yerkd: running, not answering"
NO_READING = "{name}: no reading yet"
RESTS = "{name}: resting until {until}"
OLD_READING = "{name}: last reading {ago}"
TURN_FOR = "{name}: busy {took}"
QUEUED = ", {n} waiting"
UNIT_USE = "{name}: {mb} MB, {cpu:.1f}% cpu"
JOB_DUE = "job {name}: in {left}"
NO_JOB_DUE = "jobs: none due within the hour"
NO_TIMERS = "jobs: the timer list is unreadable"
STALLS = "stalls {day}: {n}, longest {took}"
NO_STALLS = "stalls {day}: none"
TODAY, YESTERDAY = "today", "yesterday"
# the morning diff (morning.py), for the machine room
MORNING_NEW = "morning: {n} new since the last diff"
MORNING_SAME = "morning: nothing new"
KNOWN = ", {n} known"
PAYLOAD = "{room}: {size:,} chars, {change}"
PART = "{room} {part}: {change} chars"
FIRST_MAP, SAME = "first map", "same"
ANSWERED = "{day}: {n} messages answered"
WAIT = "queue wait: {mid} typical, {worst} worst"
FIRST_LINE = "first line: {mid} typical, {worst} worst"
SINCE = " since {at}"
SHADOW = "shadow flag {rule}: {n}"
NO_SHADOW = "shadow flags: none"
UNKNOWN = "{what}: unknown, {why}"
NOT_ANSWERING_WHY = "yerkd not answering"
DEAD = "dead turns: {n}, {name}, last {at}"
NO_YERKD = "no yerkd answers"
NO_DEAD = "dead turns: none in 24h"

# yerk deploy's card (deploy.py), for the machine room: the state line, then each reason on its own
DEPLOYED, FAILED_PROOF = "deployed {commit}", "{commit} failed its proof"
ROLLED_BACK, ROLLBACK_FAILED = "rolled back to {commit}", "rollback to {commit} failed its proof"
NOT_DEPLOYED, NO_REF, DIRTY = "not deployed", "no commit {ref}", "the daemon's tree has changes"
NO_FF, NOT_AHEAD = "{ref} is not ahead of {commit}", "{ref} is not ahead of {name}"
ROLLBACK_NEVER, DETACHED = "rollback to {commit} never ran", "detached at {commit}"
MOVED_BACK, AHEAD = "{name} moved back to {commit}", "{name} is {n} ahead"
PULL_BRINGS = "a pull brings {commit} back to the tree"
TESTS_PASSED, TESTS_SKIPPED, TESTS_FAILED = "fast tests passed", "fast tests skipped", "the fast tests failed"
NOT_QUIET = "never quiet in {span}"
TURN_RUNS, RECENT = "{name} has a turn", "{name} had a message {ago} ago"
PROOF_UP, PROOF_CONNECTED, PROOF_STATUS = "yerkd up", "channel connected", "status answers"
PROOF_COMMIT, PROOF_ROOMS = "running {commit}", "rooms served: {n} of {of}"
PROOF_FILE = "unit runs from its file"
NO_CUT, CUT, NOT_COUNTED = "no turn cut", "turns cut: {n}", "turns cut: not counted"
NO_UNIT, UNIT_FOUND = "no unit found: set [deploy] unit", "unit {name}, from its pid"
FAULT, NOTHING_STOPPED, BROKE_OFF = "{what} in {at}", "nothing was stopped", "{commit} broke off, not proven"
NO_ROW = "{kind} outcome not in the ledger"
UNPROVEN, UNPROVEN_AT = "{kind} {commit}: {outcome}", ", {at}"   # the morning diff: a deploys row not proven,
UNPROVEN_CUT = ", {n} cut"                                         # or one that cut a person's turn


def secs(seconds: float) -> str:
    """'1.7 s' under ten seconds, else span's '18 s', '3 min'."""
    return f"{seconds:.1f} s" if seconds < 10 else span(seconds)


# a room's model and thinking level (the settings tool)
NEXT_REPLY = "next reply: {model}, {effort} thinking"
THIS_ROOM = "this room: {model}, {effort} thinking"
OWNER_ONLY = "only the owner's rooms can change this"
NOT_LISTED = "not on the list: {model}\nthe list: {models}"
NOT_A_LEVEL = "not a thinking level: {effort}\nthe levels: {levels}"
BACK_ON = "{model} not found; back on {usual}"       # a switched-to model no account could run
USUAL = "the default"                                # a room whose rooms.toml names no model
# slash commands on the rooms' own bot (Discord): the owner's only, every answer seen by the owner alone
COMMANDS = {"model": "this room's model, from the next reply", "effort": "this room's thinking level, from the next reply",
            "new": "a new session in this room; memory stays", "stop": "end the running turn now",
            "status": "what runs now"}
CHOICE = {"model": "a model on the list, or default", "effort": "a thinking level, or default"}
NOT_OWNER = "only the owner can use this"
NO_OWNER = "set [discord] owner in yerkd.toml"       # every command, while yerkd.toml names no owner
NOT_A_ROOM = "this channel is not a Yerk room"
NEW_NEXT = "new session from the next message\nmemory stays"
STOPPED = "stopped with /stop"                       # the stopped turn's card
STOP_DONE = "stopped the running turn"
NOTHING_TO_STOP = "nothing is running here"
NOT_RUN = "that did not run\nthe daemon's log says why"


def model_name(mid: str) -> str:
    """A model id as a person says it: 'claude-opus-5-5' is 'Opus 5.5'; any other id as it is."""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d))?", mid)
    return f"{m[1].title()} {m[2]}{'.' + m[3] if m[3] else ''}" if m else mid


# the presentation settings (decision 27): the first choice is the default
SETTINGS: dict[str, tuple[tuple[str, ...], str]] = {
    "footer": (("minimal", "full"),
               "What the last lines of a worker card show. minimal: how long it ran and the command that opens "
               "the report. full: also the account and the model it ran on."),
    "paths": (("never", "owner"),
              "Whether a file path may appear in a room. never: a report shows as 'report saved' with the command "
              "that opens it. owner: a worker card in a room whose privacy is owner also shows the report's path."),
    "times": (("relative", "clock"),
              "How a card says when. relative: '3 min of 15', 'done in 3 min'. clock: the time of day, "
              "'started 13:05, cap 15 min'."),
}
RELOADS = "read when yerkd starts; restart it to apply a change"


@dataclass(frozen=True)
class Look:
    footer: str = "minimal"
    paths: str = "never"
    times: str = "relative"


def look(conf: dict[str, Any] | None) -> Look:
    """[presentation] from yerkd.toml; a value that is not one of its choices falls back to the default."""
    out = {}
    for key, (choices, _) in SETTINGS.items():
        v = str((conf or {}).get(key, choices[0]))
        if v not in choices:
            log.warning("presentation.%s = %r is not one of %s; using %s", key, v, ", ".join(choices), choices[0])
            v = choices[0]
        out[key] = v
    return Look(**out)


LOOK = Look()


def configure(conf: dict[str, Any] | None) -> Look:
    """The daemon's presentation, set once at start from yerkd.toml."""
    global LOOK
    LOOK = look(conf)
    return LOOK


def described(conf: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Each setting with its value, where the value came from, its choices and its plain words."""
    cur = look(conf)
    return [{"key": f"presentation.{k}", "value": getattr(cur, k), "default": choices[0],
             "source": "yerkd.toml" if k in (conf or {}) else "default", "choices": list(choices), "what": what,
             "reloads": RELOADS} for k, (choices, what) in SETTINGS.items()]
