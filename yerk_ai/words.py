"""The words a user reads (STYLE.md): every state mark, state word, card row and time phrase a card, a
notice or a status line uses, in one place, so a card in one room reads like a card in any other; the
commands a room may quote (`yerk status`, `yerk abilities`, `yerk explain`) take them from here too.
tests/test_style.py fails the build when a renderer spells a state mark or a state phrase itself, or
when room-bound text carries a path, an exit code, a raw timestamp or an em dash. The presentation
choices are named settings ([presentation] in yerkd.toml, decision 27), each described here in plain
words; `yerk config get presentation` prints them from this table.
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
PHONE = 40                                # a card line, in phone columns, before it wraps
SUB = "-# "                               # Discord's small grey line: a card's footer


def width(text: str) -> int:
    """Columns on a phone: a wide character (the state dots, most emoji) counts two; Discord's
    footer mark and inline code marks count nothing."""
    text = text[len(SUB):] if text.startswith(SUB) else text
    return sum(0 if unicodedata.combining(c) or c in "`️" else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


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
    if (at := text.rfind(" ")) >= len(text) // 2:                  # a word boundary that keeps most of the line
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


# the turn card: a row says what the step is FOR, never the call (STYLE.md rule 4); {what} its target, {where} a folder
START = "working on it"
STEP = {"bash": "running a command", "read": "reading {what}", "read_memory": "reading {what}", "write": "writing {what}",
        "edit": "editing {what}", "multiedit": "editing {what}", "notebookedit": "editing {what}",
        "grep": "searching {where} for {what}", "glob": "looking for {what} files", "recall": "looking up {what}",
        "webfetch": "reading {what}", "web_fetch": "reading {what}", "fetch": "reading {what}",
        "websearch": "searching the web for {what}", "web_search": "searching the web for {what}", "send": "sending {what}",
        "remember": "filing a note", "hold": "waiting for {what}", "settings": "switching to {what}", "transcribe": "transcribing {what}",
        "voice_note": "making a voice note",
        "todowrite": "updating the plan", "task": "handing off {what}", "agent": "handing off {what}", "toolsearch": "loading a tool"}
WORKER_STEP = {"start": "starting a worker", "list": "listing the workers", "stop": "stopping worker {id}",
               "report": "reading worker {id}'s report"}
JOB_STEP = {"add": "scheduling job {name}", "remove": "removing job {name}", "list": "listing the jobs"}
MESSAGE_TO, NOTES, DAY_NOTES, FACT_FILE = "a message to {room}", "your notes", "the {day} notes", "{name}'s file"
FILES = {"open-loops.md": "your open items", "MEMORY.md": "the pinned notes", "pinned.md": "the pinned notes", "rules.md": "the rules"}
ANOTHER_WAY = "that did not work, trying another way"       # two or more failed steps in a row, as one row
EARLIER, EARLIER_ROWS, ROOM_NOW = "{n} earlier steps", "{n} earlier rows", "checking what this room runs"
THINKING, NOT_ANSWERED, YOU_ASKED = "thinking: ", "not answered", 'you asked: "{asked}"'
STEP_N = "step {n}"                       # a room with people: its card counts the steps and never names one
WHY = {                                   # a failed turn, in plain words: the failure class, else the outcome
    "auth": "the account's login was refused", "quota": "out of usage on every account",
    "transient": "Claude was busy; ask again", "overflow": "the conversation is too long",
    "fatal": "Claude Code could not run it", "refusal": "the model declined",
    "timeout": "it ran past the time limit", "exited": "Claude Code stopped midway",
    "empty": "the reply came back empty", "error": "Claude Code reported an error",
    "crashed": "the harness failed; it is in the log"}
BACKUP = ("answered by a backup model", "the next message starts fresh")
MOVED, COMPACTING = "still on it, moved to account {name}", "shortening the conversation first"
MOVED_ON = "still on it, trying another way"      # the hop note in a room that is not the owner's: no account name there
OUT_OF = "on {model}, {pinned} back {until}"      # every account out of the model the room was put on: the room's own one runs it
EMPTY_AGAIN, NEW_SESSION, AFTER_FAILED = ("empty reply, asking the session again", "starting a new session, with a recap",
                                          "your message after the failed one")   # an empty reply; a fold run on its own


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
REPORT_SAVED, NO_REPORT = "report saved: `yerk worker report {id}`", "no report: `yerk worker report {id}`"
RELAY_ASK = ("[tell the room the outcome in your own voice, under {cap:,} characters, ending with the line "
             "\"{saved}\"; no file path and no exit code; {where}; do not paste it]")
# A room whose privacy is not owner hears what it can do instead and no person as the switch (seen live: a room
# turned the words for the owner's rooms into the owner's name, as the person who has to turn it on)
WORKERS_OWNER_ONLY = "workers do not run from this room; do the work here, with this room's own tools"   # start, report, list, stop
JOBS_OWNER_ONLY = "not scheduled: only this room's notes\nscripts and workers do not run from this room"   # they run as the owner
NOTES_FULL = "not scheduled: this room keeps {most} notes at most\nthe next once one is removed or has fired"   # [jobs] caps
NOTES_WAIT = "not scheduled: {what} per {every} min here\nthe next in {wait}"
NOTE_LONG = "not scheduled: a note is at most {most:,} characters\nthis one is {n:,}"
RULES_OWNER_ONLY = "not filed as a rule, since a rule reaches every room; remember it without a job and it is this room's own note"
# every remember result's last line (2026-10-21: "Filed." and a canned rule line went out as whole replies)
FILED_NEXT = ("Only the text after your last tool call reaches the room, and nothing written above this call is shown, "
              "so the answer goes there, first and whole (the result, the numbers, what you found or decided), and the "
              "filing is {clause}.")
FILED_AT_MOST = "one clause at its end at most"
RULE_CLAUSE = "one clause at its end, this one: every room now does {job} this way"
HELD_CLAUSE = "one clause at its end, this one: the rule is held for now, since the shared rules file is full"
# a filing fitted under the rules file's cap (memory.rule): each strip and a hold in the room's day log, a hold in the machine room
RULE_STRIPPED = "the example in rule \"{job}\" taken out so rules.md fits its cap of {cap:,}"
RULE_HELD_LOG = "rule \"{job}\" held in {file}: rules.md is full at its cap of {cap:,}, even with no examples"
RULE_HELD = "rule held in {file}\n\"{job}\"\nfrom {room}\nrules.md is full at its cap of {cap:,}"
PART_CUT = "session start cut {part}\n{size:,} of {cap:,} in {room}\n{n} line{s} sent broken or not at all:\n{heads}"
FILED = re.compile(r"^\W*(?:filed|saved|from now on,? every room)\b", re.I)   # a final reporting a filing (stub)
STUB = ("Only your final message reaches the room, and yours, \"{head}\", reports the filing, not the answer: the answer "
        "is above your last tool call, where the room never sees it, or it was never written. Write the whole answer as "
        "your final message now, first and complete, with the filing one clause at its end at most.")
BOARD_LINE = re.compile(r"^[\s>*-]*(?:☑|☐|⛔|🟦|🟨)|\bunblocks?:|🟦 me|🟨 you", re.I | re.M)   # the board's marks (board rule)
BOARD = ("This room has people, and your final message is the owner rooms' status board ({found}). Say where things "
         "stand in two or three plain sentences, with no checklist marks, colour key, quote rails or what unblocks it, "
         "and write that as your final message now.")
# the paste rule, in `send` and at Stop alike (2026-10-22: a block posted the evening before went out again word for word)
PASTE = ("Not posted: your message holds a fenced block this room already posted on {when}, word for word, and its dates, "
         "times and plans were that day's. Re-read it line by line against today (relative dates, clock times, who meets "
         "whom and when) and write it as it stands now, or say in one plain line why nothing in it changed.")
PASTE_FOUND = "a block of {n} line{s} first posted {when}"     # the shadow row's detail: never the block itself
# the sheet rule in an owner's room, in `send` and at Stop alike (2026-10-22: two PDFs, a crib file and pasted blocks went
# out in the owner's name in one morning with no claims sheet, each one logged and none stopped)
SHEET = ("Not {done}: {what} goes out in the owner's name, and no claims sheet was written this turn. A claims sheet is a "
         "file whose name holds claims, here {where}, with one row per claim the {thing} makes: the claim, its source line "
         "(file and line number) and the quote from that line, word for word. Write it, fix any claim it cannot source, then "
         "{again}. This is asked once a turn: the next {next} goes out as it is.")
SHEET_SEND, SHEET_STOP = ("sent", "send again", "send"), ("posted", "write your final message again", "final message")
# the person and quote rules in an owner's room, in `send` and at Stop alike (seen live: a reply told what a manager said and
# another quoted a reply, from turns that read nothing; a page for a colleague called a note about the colleague's boss the owner's rule)
SAID = ("{name} is quoted or paraphrased here and nothing was read this turn. Read where their words are (the day log, the "
        "transcript, the mirror message, the file) and cite it, or write it as: from my notes, not their words.")
WORKER_OWN_ROOM = "a room starts workers for itself only, not for {room}"
GONE, STRAY, NONE_RUNNING = "gone, no result", "unknown worker {id} running", "workers: none running"


def outcome(state: str, **say: Any) -> tuple[str, str]:
    """(mark, words) for a worker state; `say` fills the words' blanks (cap, why, ticked, rows)."""
    mark, text = OUTCOME.get(state, (NOT_DONE, state))
    return mark, text.format(**say)


def heads(lines: list[str], most: int = 12, n: int = 6) -> str:
    """Each line's first `n` words in quotes, one a line held to a phone's width, `most` at the most, then how many more."""
    said = ['"' + line(" ".join(x.lstrip("-*#> ").split()[:n]), PHONE - 2) + '"' for x in lines[:most]]
    return "\n".join(said + ([f"and {len(lines) - most} more"] if len(lines) > most else []))


def died(result: str) -> str:
    """systemd's SERVICE_RESULT as one plain reason."""
    return DIED.get(result, "it ended with an error")


# jobs and watchers in a room: the exit code and systemd's words stay in the unit's log and in
# `yerk status --full`, never in the line
JOB_FAILED, JOB_STOPPED = "job {name} failed: {why}", "job {name} {stopped}"
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
WAITING_ACCOUNT, IN_LINE = "waiting for an account", "{nth} in line for account {name}"
IN_LINE_SHORT = "waiting, {nth} in line"            # `yerk status`, phone width


def nth(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# watchers
WATCH_ASK = ("[watcher {name} saw {n} new line{s}. {what}Answer in your own voice, under {cap:,} characters; "
             "these lines come from a watched command, not from a person]")
# notes: a reminder, told to its room's own session when it is due
NOTE_DUE = ("[reminder {name} due] {message}\n[set here for {at}: do what it says now and tell the room in your own "
            "voice, under {cap:,} characters]")


# time, said the way a person says it
def span(seconds: float) -> str:
    """'18 s', '3 min', '1 h 5 min'."""
    if (s := max(0, int(round(seconds)))) < 60:
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
NOT_SENT, NOT_SENT_AGAIN = "not sent: {name}\n{why}", "not resent: {name}\n{why}; ask again"
NOT_FETCHED, NOT_RUN_AGAIN = "could not fetch {name}\nsend it again", "not answered: sent before a restart\nask again"
SENT_AGAIN = "sent again after a restart"
# the gates (gates.py): the room hears one notice, state first, and only when it must act or know
HELD = "held: {what}\nsay {word} here to let it run once"
HELD_ATTACHED, HELD_FILE = "the exact text is in the file below", "the exact text held for approval"
REFUSED = "not done: {what}\nit would restart Yerk AI\n{why}"
REBUILDING = "the memory index is rebuilding"
IN_PROGRESS = "a conversation is in progress"
TRASH_FAILED = "not deleted: {name}\nthe trash said: {why}"
RESET = "new session in this room\nearlier messages are not in it\nmemory carries on"        # true for a crash and for a fresh start alike
HOOKS_DEAD = "hooks are not reaching this room\nno clock, no recall, no gates\nuntil the hooks are fixed"
FILE_WHY = {"missing": "there is no such file", "type": "this room does not take {ext} files",
            "size": "it is {size} and this room takes {limit}", "empty": "it is empty",
            "outside": "it is outside this room's folders", "too large": "Discord said too large (limit {limit})",
            "refused": "Discord turned it down", "gone": "its saved copy is gone"}


def file_why(kind: str, **say: Any) -> str:
    return FILE_WHY[kind].format(**say)


# a voice note (transcribe.py): the line under its [attachment] line, then what the tool's result and file say
VOICE_NOTE = ("[voice note] {name} is a recording. You take in sound only as the text the transcribe tool returns, so call "
              "transcribe on the path above before you answer, then open your reply with what it says, or with the fact that it "
              "could not be transcribed. Call that step transcribing, never listening, since nothing plays the sound to you.")
TRANSCRIBED_NEXT = ("Open your reply with what the recording says, in your own words. A segment marked (uncertain) has two "
                    "readings that disagree, so give that part as unsure or ask about it, since either reading may be wrong.")
TRANSCRIPT_HEAD, UNCERTAIN, NO_WORDS = "{name}, {minutes} min, {how}", "(uncertain) {first} | second pass: {second}", "(no words)"
CHECKED = "checked by a second pass: {n} of {of} segments uncertain"
PART_CHECKED = "checked by a second pass but for chunk {chunks}, where it failed: {n} of {of} segments uncertain"
UNCHECKED = "one pass only, so nothing in it was checked: a second pass needs a Groq key at secrets/groq.key"
MORE_IN_FILE = "(the rest is in the file: Read it from this line on)"
NO_SPEECH_KEY = "no OpenAI key in the private folder (secrets/openai.key), so the recording could not be transcribed"
OVER_GATE = ("not transcribed yet, since {minutes} min of audio is about US${usd:.2f}, past the US$1 a call spends only with the "
             "owner's yes: ask, then call again with owner_yes true once the owner says yes")
# a voice note made (voice_note.py): what its result says next, why one was not made, and a send that was not a voice message
VOICE_NEXT = ("Send it now with send, file set to this path and no text, so it goes as a voice message; words of yours go in a "
              "message of their own. `probe` is its first 20 seconds read back, a check of the note: call it checking, never "
              "listening, since nothing plays the sound to you.")
VOICE_EMPTY = "the script has no words to say, so nothing was made"
VOICE_NO_KEY = "no voice key in the private folder (secrets/minimax.key or secrets/fish.key), so no voice note could be made"
VOICE_NO_CHECK = ("no OpenAI key in the private folder (secrets/openai.key), so the note could not be checked, and a note is "
                  "never handed back unchecked")
VOICE_NO_ENGINE = "neither voice made it, so there is no note: {why}"
VOICE_PROBE_FAILED = ("the note failed its check, so it was deleted: its first 20 seconds read back as \"{heard}\" where the "
                      "script opens \"{said}\"; make it once more, or answer in text")
VOICE_OVER_GATE = ("not made yet, since {chars:,} characters is about US${usd:.2f}, past the US$1 a call spends only with the "
                   "owner's yes: ask, then call again with owner_yes true once the owner says yes")
VOICE_AS_FILE = "it went as a plain audio file, not a voice message ({why}), so say so in your reply"
VOICE_PLAIN = "Discord took it without the voice message flag"


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
PLAIN, RUNNING_ROOMS = "-", "yerkd running: {rooms}"
ROOMS_N, ROOM_1 = "{n} rooms", "1 room"
VERSION_AT = "yerk {version} at {commit}"      # the package's version and the commit that runs (an upgrade's receipt)
NOT_RUNNING, NOT_ANSWERING = "yerkd: not running", "yerkd: running, not answering"
NO_READING, RESTS = "{name}: no reading yet", "{name}: resting until {until}"
OLD_READING, TURN_FOR = "{name}: last reading {ago}", "{name}: busy {took}"
QUEUED, WATCHES = ", {n} waiting", "{name}: watches {channel}"
UNIT_USE, JOB_DUE = "{name}: {mb} MB, {cpu:.1f}% cpu", "job {name}: in {left}"
NO_JOB_DUE, NO_TIMERS = "jobs: none due within the hour", "jobs: the timer list is unreadable"
STALLS, NO_STALLS = "stalls {day}: {n}, longest {took}", "stalls {day}: none"
TODAY, YESTERDAY = "today", "yesterday"
# the morning diff (morning.py), for the machine room
MORNING_NEW, MORNING_SAME, KNOWN = "morning: {n} new since the last diff", "morning: nothing new", ", {n} known"
PAYLOAD, PART = "{room}: {size:,} chars, {change}", "{room} {part}: {change} chars"
FIRST_MAP, SAME = "first map", "same"
ANSWERED, SINCE = "{day}: {n} messages answered", " since {at}"
WAIT, FIRST_LINE = "queue wait: {mid} typical, {worst} worst", "first line: {mid} typical, {worst} worst"
SHADOW, NO_SHADOW = "shadow flag {rule}: {n}", "shadow flags: none"
SPENT = "spent {day}: US${usd:.2f} on {n} calls"                     # the spend rows, one per paid call (ledger.spent)
UNKNOWN, NOT_ANSWERING_WHY = "{what}: unknown, {why}", "yerkd not answering"
DEAD, NO_YERKD, NO_DEAD = "dead turns: {n}, {name}, last {at}", "no yerkd answers", "dead turns: none in 24h"
SWEPT, SWEEP_FAILED = "swept: {kinds}", "retention failed: {why}"     # the night's sweep (retention.py)
DOCTOR_OK, DOCTOR, NO_RUN = "doctor: ok", "doctor: {n} found, {first}", "one more line 00for"     # the case before (the.next)
DOCS_CHANGED, SHAPE, SHAPE_OK = "docs changed: {slug}", "rule shape: {counts}", "waits in: the, {n} before"  # docs, shape
CURATE_OK, CURATE = "curate: {n} landed, {m} swept", "curate: {n} to read, {first}"     # the night's curation (curate.py)
CURATE_RUNNING, CURATE_CUT = "curate: running since {at}", "curate: cut off at {at}, {n} to undo"
RULES_CARD, RULES_MORE, RULES_APPLY = "rules: {n} proposed, -{chars} chars", "and {n} more in the proposals file", "apply: yerk curate --apply {day} N"
# the stance check (evidence.stance): the owner's pushback opens a line with one of these words or carries one of these phrases
PUSHBACK = re.compile(r"^[^\in\n]*(?:(?:the|Next|run|starts|the|example+|note)\says[^\the\n]*)?(?:next+|step|and+(?!\the+(?:test|reads|it|back|as)\plain)"
                      r"|wrong|actually|disagree|not\s+(?:true|really|quite))\b|\b(?:you['’]?re|you\s+are|that['’]?s|that\s+is)\s+wrong\b"
                      r"|\bthat(?:['’]?s|\s+is)(?:\s+not|n['’]?t)\s+(?:true|right|correct|it|what|how)\b|\bnot\s+true\b"
                      r"|\bi\s+(?:disagree|don['’]?t\s+agree)\b", re.I | re.M)
# recall before the answer (decision 42): code decides when a message searches the room's memory before the model starts.
# A question about the owner's past: a question word opening a sentence, or after a filler or a time word, with a past verb
# within four words ("what did I", "who did I go to lunch with", "when did", "where was that"), never with you beside the
# verb, a bare it, that or this after it (the session's own last step) or a colon (a pasted heading); "do you remember",
# "remember when"; "did" or "was" to open, never "did you", "did it" or "did this"; "did we" in a question; or, in a
# question, "last time", "last friday", "the other day", "two weeks ago", a date. Never "why": that asks what broke.
_DAY, _MONTH = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day", r"(?:jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*"
_LEAD = rf"(?:^|[.!?;(]\s*|\b(?:so|and|but|ok|okay|like|yo|bro|Alex|man|wait|hey|also|then|or|um|uh)[,\s]+|\b(?:yesterday|{_DAY}|night|week|ago)\s+)"
PAST = re.compile(rf"{_LEAD}(?:what|who|whom|when|where|which|how)(?:\s+(?!(?:it|you|u|that|this|is|are|am)\b)[\w'’]+){{0,4}}?\s+"
                  r"(?:did|was|were|had|happened|went|said|told|asked|sent|bought|met|saw|ate)\b(?!\s+(?:you|u|your|ur)\b|\s+(?:it|that|this)\s*(?:[?.!,]|$)|:)"
                  r"|\b(?:do|did|can)\s+(?:you|u)\s+(?:remember|recall)\b|\bremember\s+(?:when|the\s+(?:time|day|night))\b"
                  r"|^\W*(?:did|was|were)\s+(?!(?:you|u|it|this)\b)\w|^\W*have\s+(?:i|we)\b|\b(?:did|have)\s+(?:i|we)\b(?=[^.!\n]*\?)"
                  rf"|\b(?:last\s+(?:time|{_DAY}|week|weekend|month|year|night)"
                  r"|the\s+other\s+(?:day|night)|(?:a|one|two|three|few|couple|\d+)\s+(?:days?|weeks?|months?|years?)\s+ago"
                  rf"|on\s+(?:{_MONTH}\s+\d{{1,2}}|\d{{4}}-\d\d-\d\d))\b(?=[^.!\n]*\?)", re.I | re.M)
# a name or topic a message asks about ("who is X" with X one or two words ending the question, "what do you know about X",
# "tell me about X", "do you remember X"): X's best lines from the room's whole scope come in before the answer, or, when
# it has none, the absence line, code's word (claim 3)
ASKED = re.compile(r"\bwho(?:\s+(?:is|was)|['’]s)\s+(?P<who>[\w'’.-]+(?:\s+[\w'’.-]+)?)(?=\s*(?:(?:bro|Alex|man|dude|Alex|lol)\b\s*)?(?:[?.!,;:]|$))|\b(?:what\s+do\s+(?:you|u|we)"
                   r"\s+know\s+about|tell\s+me\s+(?:more\s+)?about|do\s+(?:you|u)\s+(?:know|remember))\s+(?P<what>[^?.!,;:\n]+)", re.I | re.M)
NOT_ASKED = frozenset("it he she they we i you u me us him them this that these those what who where when why how which there "
                      "here up out in on at off your ur his her their guy girl one person dude".split())
ABSENT = "[named, nothing on file: {what}]"


def asked(text: str) -> list[str]:
    """What a message asks about by name (ASKED), each at most four words, cut at its first "and" or "or", its leading
    article or possessive dropped; none when it opens on a pronoun, a question word or an -ing verb ("who is coming")."""
    out = []
    for m in ASKED.finditer(text):
        w = re.split(r"\s+(?:and|or|but|so|if|when|because|cuz|lol|lmao|bro|Alex|man|dude|Alex|fr|tho)\b", (m.group("who") or m.group("what")).strip(), maxsplit=1, flags=re.I)[0].split()
        w = (w[1:] if w and w[0].lower() in ("the", "a", "an", "my", "our") else w)[:4]
        if w and re.split(r"['’]", w[0].lower())[0] not in NOT_ASKED and not w[0].lower().endswith("ing"):
            out.append(" ".join(w).strip("'’\""))
    return list(dict.fromkeys(out))

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
NO_UNIT, UNIT_FOUND, NO_ROW = "no unit found: set [deploy] unit", "unit {name}, from its pid", "{kind} outcome not in the ledger"
NO_LAYER, THE_DAEMONS = "no private folder: pass --private", "private folder: the daemon's"
FAULT, NOTHING_STOPPED, BROKE_OFF = "{what} in {at}", "nothing was stopped", "{commit} broke off, not proven"
UNPROVEN, UNPROVEN_AT = "{kind} {commit}: {outcome}", ", {at}"   # the morning diff: a deploys row not proven,
UNPROVEN_CUT = ", {n} cut"                                         # or one that cut a person's turn


def secs(seconds: float) -> str:
    """'1.7 s' under ten seconds, else span's '18 s', '3 min'."""
    return f"{seconds:.1f} s" if seconds < 10 else span(seconds)


# a room's model, thinking level and account (the settings tool); {on} is ON for a room put on an account, else nothing
NEXT_REPLY, THIS_ROOM = "next reply: {model}, {effort} thinking{on}", "this room: {model}, {effort} thinking{on}"
ON, PINNED, NOT_AN_ACCOUNT = ", on account {account}", "{name}: on account {account}", "not an account: {account}\nthe accounts: {accounts}"
CARRIED, RECAPPED = "the same session goes on", "a new session starts, with a recap\nmemory stays"   # the next message, after a switch
OWNER_ONLY = "not changed: this room keeps the model, thinking level and account it has, and answers on them"   # see WORKERS_OWNER_ONLY
NOT_LISTED, NOT_A_LEVEL = "not on the list: {model}\nthe list: {models}", "not a thinking level: {effort}\nthe levels: {levels}"
BACK_ON = "{model} not found; back on {usual}"       # a switched-to model no account could run
USUAL = "the default"                                # a room whose rooms.toml names no model
# slash commands on the rooms' own bot (Discord): the owner's only, every answer seen by the owner alone
COMMANDS = {"model": "this room's model or account, from the next reply", "effort": "this room's thinking level, from the next reply",
            "new": "a new session in this room; memory stays", "stop": "end the running turn now",
            "status": "what runs now"}
CHOICE = {"model": "a model on the list, or default", "effort": "a thinking level, or default", "account": "an account by name, or default"}
NOT_OWNER, NOT_A_ROOM = "only the owner can use this", "this channel is not a Yerk room"
NO_OWNER = "set [discord] owner in yerkd.toml"       # every command, while yerkd.toml names no owner
WATCHED = "{room} answers in <#{own}>, not here"     # a channel a room watches: read, never written to
NEW_NEXT = "new session from the next message\nmemory stays"
STOPPED = "stopped with /stop"                       # the stopped turn's card
STOP_DONE, NOTHING_TO_STOP = "stopped the running turn", "nothing is running here"
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
        if (v := str((conf or {}).get(key, choices[0]))) not in choices:
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
