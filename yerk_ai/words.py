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


# the turn card
START = "working on it"
VERBS = {"read": "read", "write": "write", "edit": "edit", "multiedit": "edit", "notebookedit": "edit",
         "bash": "run", "webfetch": "fetch", "fetch": "fetch", "websearch": "search the web for", "grep": "search",
         "glob": "list", "send": "send", "hold": "wait for", "recall": "recall", "read_memory": "read",
         "todowrite": "update the plan", "task": "hand off", "agent": "hand off", "toolsearch": "load"}
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


def nth(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# watchers
WATCH_ASK = ("[watcher {name} saw {n} new line{s}. {what}Answer in your own voice, under {cap:,} characters; "
             "these lines come from a watched command, not from a person]")
WATCH_RECAP = ("(Yerk AI: this room's process was closed, so a new session was opened for a watcher's event. "
               "The last reply here began: \"{last}\")")
RELAY_RECAP = ("(Yerk AI: this room's process was closed, so a new session was opened for a worker's result. "
               "The last reply here began: \"{last}\")")


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
FILE_WHY = {"missing": "there is no such file", "type": "this room does not take {ext} files",
            "size": "it is {size} and this room takes {limit}", "empty": "it is empty",
            "outside": "it is outside this room's folders", "too large": "Discord said too large (limit {limit})",
            "refused": "Discord turned it down", "gone": "its saved copy is gone"}


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


# status
RUNNING_ROOMS = "yerkd running: {rooms}"
NOT_RUNNING = "yerkd: not running"
NO_READING = "{name}: no reading yet"
RESTS = "{name}: resting until {until}"


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
