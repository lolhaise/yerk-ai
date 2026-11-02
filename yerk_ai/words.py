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


def either(ws: list[str]) -> str:
    """Words as a notice lists them: "a", "a or b", "a, b or c"."""
    return ", ".join(ws[:-1]) + " or " + ws[-1] if len(ws) > 1 else "".join(ws)


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
        "voice_note": "making a voice note", "seen": "filing a picture", "need": "filing what it needs", "watch": "watching a video on {what}",
        "todowrite": "updating the plan", "task": "handing off {what}", "agent": "handing off {what}", "toolsearch": "loading a tool"}
WORKER_STEP = {"start": "starting a worker", "list": "listing the workers", "stop": "stopping worker {id}",
               "report": "reading worker {id}'s report"}
JOB_STEP = {"add": "scheduling job {name}", "remove": "removing job {name}", "list": "listing the jobs"}
WATCH_FILE = "watching the video {what}"  # a watch call on a video file sent to the room, by its plain name
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
    "crashed": "the harness failed; it is in the log", "undelivered": "Discord did not take the reply"}
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
RELAY_ASK = ("[tell the room the outcome in your own voice{under}, ending with the line "
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
TOPIC_CLAUSE = "one clause at its end, this one: the rule loads when {triggers} come up, since the shared rules file is full"
# a filing fitted under the rules file's cap (memory.rule): each strip and a topic rule in the room's day log, a topic rule in the
# machine room (rules-on-demand: never "held", it loads when its topic comes up)
RULE_STRIPPED = "the example in rule \"{job}\" taken out so rules.md fits its cap of {cap:,}"
RULE_TOPIC_LOG = "rule \"{job}\" filed as a topic rule in {file}: rules.md is full at its cap of {cap:,}; it loads when {triggers} come up"
RULE_TOPIC = "filed as a topic rule: loads when\n{triggers} come up\n\"{job}\"\nfrom {room}\n{where}\nrules.md is full at its cap of {cap:,}"
TOPIC_WHERE = ("for every room", "for {rooms} only")
TOPICS_MOVED = "{n} rule{s} moved to rules-topics.md\nfrom rules-overflow.md, once\neach loads when its topic comes up"
PART_CUT = "{part}: {n} line{s} dropped\nto fit its {cap:,} in {room}"     # at session start: whole lines of its own, no framing
FILED = re.compile(r"^\W*(?:filed|saved|from now on,? every room)\b", re.I)   # a final reporting a filing (stub)
STUB = ("Only your final message reaches the room, and yours, \"{head}\", reports the filing, not the answer: the answer "
        "is above your last tool call, where the room never sees it, or it was never written. Write the whole answer as "
        "your final message now, first and complete, with the filing one clause at its end at most.")
# the note rule at Stop (seen live: a reply opened with the model's plan for it, and the room posted both whole)
NOTE = ("Every word of your final message is posted, and yours opens with a note to yourself (\"{head}\") before the answer. "
        "Thinking is where that goes. Write the answer alone as your final message now, from its first word.")
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
SAID_HERE = ("{name} is quoted or paraphrased here and nothing was read this turn. Read where their words are in this room's own "
             "lines (recall or read_memory: its day log, a file sent here, a recording's transcript) and cite it, or write it as: "
             "from my notes, not their words.")      # a person's room: its own sources only, never a file outside its layer
WORKER_OWN_ROOM = "a room starts workers for itself only, not for {room}"
GONE, STRAY, NONE_RUNNING = "gone, no result", "unknown worker {id} running", "workers: none running"


def outcome(state: str, **say: Any) -> tuple[str, str]:
    """(mark, words) for a worker state; `say` fills the words' blanks (cap, why, ticked, rows)."""
    mark, text = OUTCOME.get(state, (NOT_DONE, state))
    return mark, text.format(**say)


def died(result: str) -> str:
    """systemd's SERVICE_RESULT as one plain reason."""
    return DIED.get(result, "it ended with an error")


# jobs in a room: the exit code and systemd's words stay in the unit's log and in
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


# notes: a reminder, told to its room's own session when it is due
NOTE_DUE = ("[reminder {name} due] {message}\n[set here for {at}: do what it says now and tell the room in your own "
            "voice{under}]")
# where the owner is (location.py): an owner room's per-message line, and the day's places as one day-log line
PLACE = "[place] near {place}, {ago}, {state}"
PLACES_DAY = "the owner's places from the phone, first and last fix at each: {stays}"


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


def under(cap: int) -> str:
    """A harness ask's length, ", under N characters"; nothing for an uncapped install's 0 ([limits] uncapped)."""
    return f", under {cap:,} characters" if cap else ""


def clock(epoch: float, now: float | None = None) -> str:
    """A time of day as a person says it: '13:05' today, 'Mon 13:05' another day."""
    t, today = datetime.fromtimestamp(epoch), datetime.fromtimestamp(time.time() if now is None else now)
    return t.strftime("%H:%M") if t.date() == today.date() else t.strftime("%a %H:%M")


def rest(until: float | None) -> str:
    """An account's rest as /model says it: free, or resting until its end."""
    return RESTING.format(until=clock(until)) if until else FREE


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
# a video file (watch.py): the line under its [attachment] line, since Read cannot open a video
VIDEO_FILE = ("[video] {name} is a video. You see a video only as the still frames the watch tool takes from it, so call watch "
              "with `path` set to the path above before you answer, then Read its frames and answer with what they show and what "
              "its transcript says, or with the fact that it could not be watched.")
TRANSCRIBED_NEXT = ("Open your reply with what the recording says, in your own words. A segment marked (uncertain) has two "
                    "readings that disagree, so give that part as unsure or ask about it, since either reading may be wrong. A "
                    "letter before a line (A:) is a voice told apart by sound, so say which person each letter is as your guess "
                    "from what they say, unless the owner said who is who.")
TRANSCRIPT_HEAD, UNCERTAIN, NO_WORDS = "{name}, {minutes} min, {how}", "(uncertain) {first} | second pass: {second}", "(no words)"
SEEN_NOT_HERE = "{name} is not a picture sent to this room; seen files a picture from its [attachment] line"
SEEN_NO_SHOWS = "seen needs `shows`: one or two lines of what the picture shows"
# a document rendered (render.py): why no PDF or page count came back
RENDER_NOT_HERE = "{name} is outside this room's folders; render reads the files sent to it and this room's own work"
RENDER_NO_FILE = "there is no file {name} in this room's folders"
RENDER_NO_SOFFICE = "LibreOffice (soffice) is not on this machine, so no PDF could be made"
RENDER_FAILED, RENDER_NO_WHY = "LibreOffice could not make a PDF of {name}: {why}", "it gave no reason"
RENDER_TIMED_OUT = "LibreOffice did not finish {name} in {seconds} s, so it was stopped and no PDF was made"
RENDER_NOT_PDF = "{name} did not open as a PDF ({why}), so its pages could not be counted"
RENDER_DAMAGED, RENDER_LOCKED = "it is damaged or not a PDF", "it is locked with a password"
# a video watched (watch.py): why no video came back, and a result's note on what it has no frames or transcript for
WATCH_NO_ROOM = "watch puts the video in a room's work folder, and there is no room {room}"
WATCH_NOT_LINK = ("\"{given}\" is not a link: watch takes a post's web address, one that starts http:// or https://, or a video "
                  "file's saved path as `path`")
WATCH_BOTH = "watch takes a post's link or a video file's saved path, one of the two"
WATCH_NOT_HERE = "{name} is outside this room's folders; watch reads the videos sent to it and this room's own work"
WATCH_NO_FILE = "there is no file {name} in this room's folders"
WATCH_NO_YTDLP = "yt-dlp is not on this machine, so no video could be pulled"
WATCH_FAILED, WATCH_NO_WHY = "no video was pulled from that link: {why}", "it gave no reason"
WATCH_NOTHING = ("no video was pulled from that link: yt-dlp saved nothing, as it does for a video over 300 MB or a page with "
                 "no video in it")
WATCH_TIMED_OUT = "the download did not finish in {seconds} s, so it was stopped and no video was saved"
WATCH_NO_FFMPEG = "ffmpeg and ffprobe are not both on this machine, so the video could not be read"
WATCH_NOT_READ, WATCH_SLOW = "{name} did not read as a video ({why})", "it took past {seconds} s"
WATCH_BROKE = "the video could not be watched: {why}"
WATCH_LINKED = "{name} in this room's work folder is a link, and watch never writes through one, so nothing was pulled"
WATCH_NO_SOUND, WATCH_NO_PICTURE = "it has no sound, so there is no transcript", "it has no picture, so there are no frames"
WATCH_NO_TRANSCRIPT = "no transcript: {why}"
WATCH_FRAMES_LOST = "{got} of {of} frames came, the rest would not: {why}"
WATCH_FROM_CAPTIONS = "the transcript is the video's own captions, read free with their timestamps, so say the words are its captions"
WATCH_FROM_AUTO_CAPTIONS = ("the transcript is the video's auto-generated captions, read free with their timestamps, so say the words "
                            "are its auto captions: they carry no punctuation, and a name in them may be misheard")
WATCH_NO_CAPTIONS = "no captions on that page, so its sound was transcribed"
WATCH_CAPTIONS_FAILED = "its captions could not be read ({why}), so its sound was transcribed"
CAPTIONS_LIVE = "it is live now or not started yet, and a live stream has no captions to read"
# a text in the owner's name (texts.py): why one cannot go, said before the owner is asked where it can be, and how one went
TEXT_OWNER_ROOMS = "a text goes out in the owner's name, so only the owner's own rooms send one"
TEXT_NO_SETUP = "no Mac is set up for texts ([imessage] host and socket in yerkd.toml)"
TEXT_NO_HANDLE = "\"{given}\" is not a phone number or an email address: recall the person's number from the owner's texts, or ask"
TEXT_EMPTY = "there is no text to send"
TEXT_NOT_RELEASED = "not sent: a text goes only as the call the owner was shown held and said the word for, once"
TEXT_NOT_SENT, TEXT_NO_WHY = "not sent: {why}", "the Mac gave no reason"
TEXT_AWAY = "the Mac's bridge did not answer (the Mac asleep or away, or the bridge not running)"
TEXT_MAYBE = "it may have gone out, and the Mac never confirmed it: tell the owner to check Messages, and never send it again unasked"
TEXT_WHY = {-32602: "Messages would not take that number or address", -32603: "Messages on the Mac failed or lacks a permission",
            -32000: "the Mac's bridge was busy, so try again in a minute", -32004: "the Mac's bridge was busy, so try again in a minute"}
# a transcript's head (TRANSCRIPT_HEAD's `how`) when a video's own captions wrote it (captions.py)
FROM_CAPTIONS = "the video's own captions, free, their timestamps"
FROM_AUTO_CAPTIONS = "the video's auto-generated captions, free; no punctuation, and a name may be misheard"
UNSEEN = ("You read {names} and did not file what it shows. Call seen on {paths} with its words and a line "
          "of what it shows, then give your answer again, since only the text after your last tool call reaches the room.")
UNWATCHED = ("You were sent {names}, a video, and have not watched it. Call watch with `path` {paths}, since Read cannot open a "
             "video, then Read its frames and answer with what it shows.")
PICTURES_PERSON = "{room} is not an owner room: the backfill runs in owner rooms only; a picture sent there is filed by its own room"
PICTURES_NONE = "{room}: every picture in {where} has its sidecar; nothing to start"
PICTURES_NOT_LISTED = ("{folder} is not a folder {room} lists in rooms.toml (read or also) outside Yerk's own folders, so "
                       "nothing there is filed")
PICTURES_RUNNING = "{room}: a backfill runs already, one batch after another; its lines are in the machine room"
PICTURES_BATCH = "pictures backfill {room}: batch {n} of {of} {how}, {filed} of {count} filed{then}"
PICTURES_NEXT, PICTURES_STOP, PICTURES_DONE = "; batch {n} started", "; stopped, {left} left: run it again", "; done"
FILE_TEXT = "[its text, extracted by code] saved at {path}"     # under a workbook's or a PDF's [attachment] line (files.py)
ADDON_READS = "[read by an add-on] {name} is read through {tool}, an installed add-on's tool: call it on the path above"   # addons.reads
# the line above a message from the channel a room watches (rooms.toml `watch`, `watch_assistant`): who it was sent to there
SENT_THERE = ("[sent to {who} in #{channel}, not to this room: the owner is talking to {who} there. Read along and file what "
              "matters. Answer here only if the owner's words ask this room something, or you have something they would want from this "
              "room that {who} did not give; otherwise answer exactly {quiet}.]")
# ... in a room with `watch_audit` true: a verdict on that assistant's answer, every time, never NO_REPLY (the owner, 2026-10-29)
SENT_THERE_AUDIT = ("[sent to {who} in #{channel}, not to this room: {sender} is talking to {who} there. {who}'s answer follows the "
                    "message. AUDIT it and post your verdict here: check it against the files and the thread, not recall; first line "
                    + DONE + " looks good, " + WORKING + " right but missing something, or " + NOT_DONE + " wrong, then what is wrong or "
                    "missing and the fix, written out in full in plain words, because {readers} read this room; one or two lines when "
                    "it is right. If {who} had not answered within the wait, say that in one line. Never {quiet} on this turn; never "
                    "redo work {who} got right.]")
OWNER, A_PERSON = "the owner", "a person this room answers"   # who reads it, by name where the server shows one
NO_VERDICT = "no verdict: the room answered nothing"   # its card, kept under NOT_DONE, when it answers NO_REPLY all the same
OTHER_ASSISTANT = "the other assistant"              # `watch_assistant` unset
# the line under it once that assistant's answer there has settled, a line per file it attached (saved to the room's inbox as a
# person's is, or why not); else once `watch_wait` ran out
WATCH_ANSWERED, WATCH_ATTACHED = "[{who} answered at {at} in #{channel}: {text}]", '[{who} attached: "{name}", {type}, {size:,} bytes, saved at {path}]'
WATCH_NOT_KEPT = '[{who} attached: "{name}", which could not be fetched ({why})]'
WATCH_UNANSWERED = "[{who} had not answered in #{channel} within {wait} s of the owner's message]"
NO_REPLY, QUIET = "NO_REPLY", "read it, nothing to add"   # a final of exactly NO_REPLY posts nothing; its card says QUIET
# the emoji an OpenClaw progress card's tool row starts with, one per tool kind, "🧩" for any other (an MCP tool's): its
# TOOL_DISPLAY_CONFIG with the message tool's spec, /usr/lib/node_modules/openclaw/dist/tool-display-zmIG5_OB.mjs, read
# 2026-10-29; a message with one is that assistant at work, never its answer (channels/discord.py is_card)
OPENCLAW_ROWS = ("✉️ 🧩 🛠️ 🖱️ 📱 🖥️ ⌨️ 🌐 🧰 📖 ✍️ 📝 📎 🖼️ 📋 ⏰ 🎯 🗺️ ❓ 🔑 ✨ 🗑️ 🦀 🔌 📊 🔀 🔐 🗂️ 💬 📨 ↔️ 🧾 🔎 🎙️ 🧑‍🔧 ⏳ 🤖 🧭 🧠 📓 📄 🧮 "
                 "🩹 🎨 🎵 🎬 📑 ⏸️ 🔊")
FILES_PERSON = "{room} is not an owner room: a workbook or a PDF sent there is never read"
FILES_BACKFILL = "{n} workbook(s) and PDF(s) in {where} with no text beside them or an older one; {done}"
FILES_WRITTEN, FILES_DRY = "written", "nothing written (--dry-run)"
BACKFILLS = ("yerk pictures backfill {room} --root {folder}", "yerk files backfill {room} --root {folder}")   # room add's, the doctor's
MOVED_IN = ("{room}: {pics} picture(s) and {sheets} workbook(s) or PDF(s) moved in with it, in {folder}, have no sidecar, so no "
            "recall finds what is in them: {commands}")
MOVED_UNLISTED = "; list the folder in the room's also and in yerkd.toml's [memory] also first"
# missing-ability (needs.py): a room's need in the machine room, its build and its end there, and the refusals
NEED_FILED = WORKING + " need {n} from {room}: {ability}; `yerk needs build {day} {n}`"
NEED_AGAIN = WORKING + " need {n} asked again from {room}, {count} times now: {ability}"
NEED_PR, NEED_BRANCH = DONE + " need {n} built, PR {pr}: {ability}", WORKING + " need {n} built on {branch}, no PR seen: {ability}"
NEED_FAILED = NOT_DONE + " need {n}: its builder {how}, no branch; filed again: {ability}"
NEED_CLAUSE = "one clause at its end, this one: what you needed is filed for the owner to build for every room"
NEED_NOT_YOURS = "not filed: a need from this room names only its own files, and {path} is not one"
NEED_NONE = "no need {n} of {day}; `yerk needs list` shows them"
NEED_BUSY = "need {n} is {state}: one builder a need, so nothing started"
# the needs pipeline (pipeline.py): each change one line in the machine room; live also one line in each room that filed it
NEED_STARTS = WORKING + " need {n} from {room}: {ability}; its builder starts"
NEED_QUEUED = WORKING + " need {n} from {room}: {ability}; queued behind need {behind}"
NEED_PICKED_UP = WORKING + " need {n} of {day} from {room}: {ability}; filed and never built, so queued"
NEED_NO_TOKEN = WORKING + " need {n}: no GitHub token, so the merge waits for a person\nput one at secrets/github.token for the pipeline"
NEED_SEEN = WORKING + " need {n}: PR {pr} open, waiting on its checks"
NEED_FIXING = WORKING + " need {n}: PR {pr}, fix {k} of {cap} starts\n{why}"
NEED_FIXED = WORKING + " need {n}: PR {pr} fixed, its checks run again"
NEED_READING = WORKING + " need {n}: PR {pr} checks passed, its cold read starts"
NEED_HELD = NOT_DONE + " need {n} held, a person decides: {ability}\n{why}"
NEED_STUCK = NOT_DONE + " need {n} stuck: {ability}\n{why}"
NEED_MERGED = DONE + " need {n}: PR {pr} merged; the deploy is next"
NEED_DEPLOYING = WORKING + " need {n}: deploying PR {pr} onto the daemon"
NEED_LIVE = DONE + " need {n} is built and live: {ability}"
NEED_LIVE_ROOM = DONE + " need {n} is built and live: {ability}\nthis room has it from its next message, when its session starts on the new code"
CHECKED = "checked by a second pass: {n} of {of} segments uncertain"
PART_CHECKED = "checked by a second pass but for chunk {chunks}, where it failed: {n} of {of} segments uncertain"
UNCHECKED = "one pass only, so nothing in it was checked: a second pass needs a Groq key at secrets/groq.key"
# the line under a transcript's head: who talks how long, by Muse's letters reconciled across its windows (transcribe.reconcile)
SPEAKERS = "speakers: {who} (letters, not names: say who is who as a guess from what is said, unless the owner tells you)"
UNMATCHED, ONE_VOICE = ", {n} letters could not be matched across windows", "one voice"
NO_SPEAKERS, NO_META_KEY, NO_TURNS = "speakers: none (Muse: {why})", "no Meta key at secrets/meta.key", "no speech turns in its answer"
MUSE_FAILED = "window {windows} of {of} failed: {error}"
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
           "described": (NOTHING, "known, not built"), "installed": (DONE, "installed, version {version}"),
           "available": (NOTHING, "not installed: yerk add {name}"), "broken": (NOT_DONE, "installed, missing {what}")}


def ability(state: str, **say: Any) -> tuple[str, str]:
    mark, text = ABILITY[state]
    return mark, text.format(**say)


# `yerk add` and `yerk remove` (addons.py): each step as done, and as a dry run says it would be (step)
ADD_STEP = {"fetch": ("fetched", "fetch", "{src}"), "check": ("checked", "check", "{name} {version}: {lines} lines of code, its ceiling {size}"),
            "package": ("installed", "install", "its package {package}"), "server": ("wrote", "write", "[defaults.mcp.{name}] and {n} allow line(s) in rooms.toml"),
            "tables": ("wrote", "write", "{tables} in yerkd.toml"), "units": ("linked", "link", "{units} into {into}"),
            "budgets": ("counted", "count", "budget line(s) {lines} while it is installed"),
            "words": ("kept", "keep", "its words file {words} under the style test"), "tests": ("ran", "run", "its fast tests ({said})")}
REMOVE_STEP = {"server": ("took", "take", "[defaults.mcp.{name}] and its allow lines out of rooms.toml"),
               "tables": ("took", "take", "{tables} out of yerkd.toml"), "units": ("unlinked", "unlink", "{units}"),
               "package": ("uninstalled", "uninstall", "its package {package}"), "folder": ("moved", "move", "its folder to the trash")}
ADD_NONE = "no add-on {source}: name a folder, a git URL or an ability with a repo ({names})"
NOT_ADDED, ADD_TESTS_FAILED, ADD_DRY = "not added: {why}", "its fast tests failed: {said}", "dry run: nothing written"
ADDED, REMOVED, NOT_REMOVED = "added {name} {version}: the rooms get it at yerkd's next start", "removed {name}", "not removed: no add-on {name} is installed"
ADDONS, NO_ADDONS, OF_ADDON = "add-ons: {names}", "none installed", "the fast tests of add-on {name} failed"
NOT_UNINSTALLED = "not uninstalled: its package {package}; the rest is removed"


def step(steps: dict[str, tuple[str, str, str]], key: str, dry: bool, **say: Any) -> str:
    """A `yerk add` or `yerk remove` step, state first: done, or what a dry run would do."""
    past, base, rest = steps[key]
    return f"{PLAIN} would {base} {rest.format(**say)}" if dry else f"{DONE} {past} {rest.format(**say)}"


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
BACKUP_AT, NO_BACKUP = "backup: {ago} ago, {size}", "backup: none yet"
NO_JOB_DUE, NO_TIMERS = "jobs: none due within the hour", "jobs: the timer list is unreadable"
STALLS, NO_STALLS = "stalls {day}: {n}, longest {took}", "stalls {day}: none"
TODAY, YESTERDAY = "today", "yesterday"
# each account, two lines: its meters (a warning sign at HOT and over), then when its windows reset and its next bill;
# the account whose week resets soonest wears the green mark on its tag; then the next two resets of each window
HOT, WARN = 80, "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"
RESETS, BILL = "\N{CLOCKWISE OPEN CIRCLE ARROW} {parts}", "\N{CREDIT CARD} {day}"
BACK_AT, NEXT, THEN = "resting until {until}", "{label} next: {first}", " · then {tag} {when}"
FIRST_TRY = "next turn tries {tag} first"         # accounts.pick for a room on no account of its own
ROOMS_ON = "on account {account}: {rooms}"        # two rooms or more put on one account, one line
NUMBERS = ("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
           "seventeen eighteen nineteen twenty").split()


def tag(name: str) -> str:
    """An account on the card: account one (or 1) is ①, up to ⑳; any other name is itself."""
    n = NUMBERS.index(name.lower()) + 1 if name.lower() in NUMBERS else int(name) if name.isdigit() else 0
    return chr(0x2460 + n - 1) if 1 <= n <= 20 else name


def short_clock(epoch: float) -> str:
    """'5p', '5:10p', '12a': a time of day the short way."""
    t = datetime.fromtimestamp(epoch)
    return f"{t.hour % 12 or 12}{'' if t.minute == 0 else f':{t.minute:02d}'}{'a' if t.hour < 12 else 'p'}"


def short_when(epoch: float, now: float | None = None) -> str:
    """A reset: '5:10p' within a day, 'Wed 2a' within a week, else 'Nov 8'."""
    ahead = epoch - (time.time() if now is None else now)
    if ahead < 86400:
        return short_clock(epoch)
    return f"{datetime.fromtimestamp(epoch):%a} {short_clock(epoch)}" if ahead < 7 * 86400 else short_day(epoch)


def short_day(epoch: float) -> str:
    """'Nov 8': a date, never a weekday (a bill)."""
    t = datetime.fromtimestamp(epoch)
    return f"{t:%b} {t.day}"
# the morning diff (morning.py), for the machine room
MORNING_NEW, MORNING_SAME, KNOWN = "morning: {n} new since the last diff", "morning: nothing new", ", {n} known"
PAYLOAD, PART = "{name}: {size:,}, {left:,} left", "{room} {part}: {change} chars"     # as sent and its gap to the total; what moved
OVER_PART = "{name}: {size:,} > {line:,}"           # "room part", a Claude Code part past its line: the deploy's gate refuses
ANSWERED, SINCE = "{day}: {n} messages answered", " since {at}"
WAIT, FIRST_LINE = "queue wait: {mid} typical, {worst} worst", "first line: {mid} typical, {worst} worst"
SPEED = "{day}: first line {first}, turn {turn}"                     # the day's medians (latency.medians) on the status card
SPEED_WAS, SPEED_OF = "{what} {now}, day before {was}", {"first": "first line", "turn": "turn"}   # the morning's, against the day before
SHADOW, NO_SHADOW = "shadow flag {rule}: {n}", "shadow flags: none"
SPENT = "spent {day}: US${usd:.2f} on {n} calls"                     # the spend rows, one per paid call (ledger.spent)
SPEND = "spend {day} US${usd:.2f} ({n} call{s}) · week US${week:.2f}"     # an uncapped install's, every day (status.spend_line)
UNKNOWN, NOT_ANSWERING_WHY = "{what}: unknown, {why}", "yerkd not answering"
DEAD, NO_YERKD, NO_DEAD = "dead turns: {n}, {name}, last {at}", "no yerkd answers", "dead turns: none in 24h"
SWEPT, SWEEP_FAILED = "swept: {kinds}", "retention failed: {why}"     # the night's sweep (retention.py)
SWEPT_FROM, SWEPT_MORE = "; scratch: {rooms}", ", and {n} more"     # the rooms its scratch left and their top folders
SWEPT_NOTE = ("# What the nightly sweep took\n\nThe sweep of {at} moved these out of this room's scratch, each to where it waits "
              "in the state folder:\n\n{moved}\n\nThe trash keeps them {days} after that day, then a sweep deletes them for good.\n")
NEVER_SWEPT = "A folder under work/ with a file named KEEP is never swept."   # RETENTION.md's last line, in a swept room's work/
DOCTOR_OK, DOCTOR, NO_RUN = "doctor: clean", "doctor: {n} found, {first}", "no run in 24h"  # the daily check (doctor.py)
DOCS_CHANGED, SHAPE = "docs changed: {slug}", "rule shape: {counts}"         # docs, the weekly rule-shape line
CURATE_OK, CURATE = "curate: {n} landed, {m} swept", "curate: {n} to read, {first}"     # the night's curation (curate.py)
CURATE_RUNNING, CURATE_CUT = "curate: running since {at}", "curate: cut off at {at}, {n} to undo"
RULES_CARD, RULES_MORE, RULES_APPLY = "rules: {n} proposed, -{chars} chars", "and {n} more in the proposals file", "apply: yerk curate --apply {day} N"
RULES_KIND, RULES_NONE = "{kind}: {numbers}", "rules: none proposed {day}"     # the morning's list of the night's (curate.rules_lines)
# the stance check (evidence.stance): the owner's pushback opens a line with one of these words or carries one of these phrases
PUSHBACK = re.compile(r"^[^\in\n]*(?:(?:the|Next|run|starts|the|example+|note)\says[^\the\n]*)?(?:next+|step|and+(?!\the+(?:test|reads|it|back|as)\plain)"
                      r"|wrong|actually|disagree|not\s+(?:true|really|quite))\b|\b(?:you['’]?re|you\s+are|that['’]?s|that\s+is)\s+wrong\b"
                      r"|\bthat(?:['’]?s|\s+is)(?:\s+not|n['’]?t)\s+(?:true|right|correct|it|what|how)\b|\bnot\s+true\b"
                      r"|\bi\s+(?:disagree|don['’]?t\s+agree)\b", re.I | re.M)
# decision receipts (claim 19, evidence.calls): code decides that a sentence of a reply makes a call for the person the room
# answers: "I'd go with" (or "I'd" with an imperative verb: "I'd hold it until it scores"), "go with" or "pick" opening a sentence,
# "my call:", "you should", "the plan is", or a number with an imperative after it ("1. Book the 9:40", "350, don't go higher");
# a question, quotes and code are never one, nor "you should have" or "should've"; and by the had rule "I'd put" or "I'd set"
# with a past-time word later in its sentence (earlier, already, ago, yesterday, last) is "I had": "I'd put your two runs in the
# tracker an hour earlier". They are the only verbs here whose "I had" reads the same; "I'd take the earlier one" is a call
DO = (r"(?:send|book|buy|call|email|text|ask|tell|go|take|get|pick|put|move|pay|cancel|order|sign|submit|reply|schedule|set|start|"
      r"stop|use|bring|leave|offer|counter|accept|decline|sell|keep|skip|wait|push|grab|try|hold|apply|file|don['’]?t|never)")
_OPEN = r"(?:^|(?<=[.!?;:]\s))\W*(?:so\s+|just\s+|then\s+)?"
_HAD = r"(?!(?:put|set)\b(?:(?![.!?](?:\s|$)).)*?\b(?:earlier|already|ago|yesterday|last)\b)"     # the had rule, above
CALL = re.compile(rf"\bI['’]?d\s+{_HAD}(?:go\s+with|stick\s+with|choose|{DO})\b|\b(?:I\s+would|let['’]?s)\s+go\s+with\b|{_OPEN}go\s+with\b"
                  rf"|{_OPEN}pick\b(?!\s+(?:up|one|any|either|whichever|whatever)\b)|\bmy\s+(?:call|pick)\s*(?::|is\b|would\s+be\b|here\b)"
                  r"|\byou\s+should\b(?!\s*['’]ve\b|\s+(?:have|be|see|know|hear|get\s+(?:a|an|the)\s+(?:email|text|message|call|notification)))"
                  r"|\b(?:the|my|our)\s+plan\s+(?:is|would\s+be)\b|\bhere['’]?s\s+the\s+plan\b"
                  rf"|{_OPEN}(?:step\s+)?\$?\d[\d,.]*[kK%]?(?:[.):,]|\s+(?:max|tops|firm),?)?\s+(?:and\s+)?{DO}\b", re.I)
CALLS, CALLS_BARE, CALLS_NONE = "calls {day}: {n}", ", {m} with no reads", "calls {day}: none"    # the morning diff (morning.calls)
CALL_SAID, CALL_READS, NO_READS, READS_MORE = "{room}: {call}", "call {id}: {reads}", "no reads behind it", " +{n}"
CALLS_MORE, CALLS_FOR = "and {n} more calls", "{room}: {n} call{s} for its people"
CALL_FIXED, FIXED_BY = "call {id}: {source} corrected", "by {cite}"      # a fix, the morning after `yerk calls fix`
CALL_FIX = "{note} (the fix for call {id}, {room}: \"{call}\")"     # `yerk calls fix`: the correction's day-log line
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
RECALL, RECALL_BY, MISSED = "recall: {n} of {of} hit", " ({m} by meaning)", "missed: {name}"     # the live controls
RECALL_NONE, RECALL_BROKE = "recall: not checked, {why}", "the recall check did not run"
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
# bare /model under the room's line, and its suggestions as the owner types (Yerkd.model_menu, model_picks)
MENU_ACCOUNTS, MENU_ACCOUNT, MENU_MODELS = "accounts, in the order tried for {model}:", "{name}: order {order}, {state}", "models: {models}"
FREE, RESTING, PINNED_MARK, PICK_ON = "free", "resting until {until}", ", pinned", "{model} on account {name}, {state}"
PICK_DEFAULT = "default: clears the model and the account"
USUAL = "the default"                                # a room whose rooms.toml names no model
# slash commands on the rooms' own bot (Discord): the owner's only, every answer seen by the owner alone
COMMANDS = {"model": "this room's model or account, from the next reply", "effort": "this room's thinking level, from the next reply",
            "new": "a new session in this room; memory stays", "stop": "end the running turn now",
            "status": "what runs now"}
CHOICE = {"model": "an account, a model or both, the account first; default clears both", "effort": "a thinking level, or default", "account": "an account by name, or default"}
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
