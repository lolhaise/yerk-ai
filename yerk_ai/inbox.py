"""The owner's recent texts, kept as a copy on the box so a room asked whether someone has texted answers even while the
owner's Mac is asleep.

The Mac sleeps on battery with the lid shut, by the owner's own rule, and ssh to it dies with it; a read of the owner's Messages
then times out, as it did the night a room was asked whether a text had come in and could not say. Nothing here keeps the
Mac awake or wakes it. A light check (daemon.refresh_inbox) reads the newest texts over ssh whenever the Mac answers, its
own short wakes included (a shut Mac still wakes itself for a minute or two to take an iMessage, and ssh answers while it
is up), and copies them to a private file under the state folder, kept 0600 like the rest of it. The `texts` tool reads
that copy: with the Mac awake it refreshes the copy first and answers live; with the Mac asleep it answers from the last
copy and says that copy's time, so a stale answer is never passed off as a fresh one.

The read is a read-only sqlite of ~/Library/Messages/chat.db over plain ssh (no Messages grant needed, unlike the send in
texts.py, only a Mac whose ssh login can read that file): the newest rows of `message` joined to `handle`, fields split on
US (0x1f) and rows on RS (0x1e), so a text carrying newlines or tabs survives the split. The command is bash on the Mac's
stdin, as the mac tool runs one, so nothing of it rides the ssh command line. ssh fails fast when the Mac is asleep (exit
255 or a timeout) and nothing here is retried, so the check costs a shut Mac one refused connection and no more.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .config import write_private

FOLDER = "texts"                 # <state>/texts/, 0700 like the rest of the state folder
FILE = "inbox.json"              # the copy: {"at": epoch copied, "host", "messages": newest first}
DB = "Library/Messages/chat.db"  # under the Mac's home
LIMIT = 40                       # messages copied, newest first
SHOW = 12                        # messages a read returns by default (the room asks for more up to LIMIT)
TEXT_MAX = 2000                  # characters kept per message, so one long text cannot bloat the copy
CONNECT = 8                      # ssh ConnectTimeout: a shut Mac refuses fast
READ_TIMEOUT = 25.0              # the whole read's wall clock, the connect included
POLL = 120.0                     # seconds between the daemon's light checks (CONF poll_seconds)
APPLE = 978307200                # seconds from the Unix epoch to Apple's (2001-01-01)
US, RS = "\x1f", "\x1e"          # the field and row separators sqlite writes between values and rows

# newest LIMIT messages: when (seconds, both the nanosecond and the older second form handled), from the owner or not, who
# (the other party's number or address, "" for the owner's own line), the text. attributedBody-only rows read as "".
Run = Callable[[list[str], bytes, float], "tuple[int | None, bytes, bool]"]


def script(limit: int = LIMIT) -> str:
    """The bash line run on the Mac's stdin: a read-only sqlite of the newest `limit` messages, fields on US and rows on
    RS. bash, so $'\\x1f', $'\\x1e' and $HOME all hold whatever the login shell is."""
    sql = ("SELECT CASE WHEN m.date > 1000000000000 THEN m.date/1000000000 ELSE m.date END + {apple}, "
           "m.is_from_me, COALESCE(h.id, ''), COALESCE(m.text, '') "
           "FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID "
           "ORDER BY m.date DESC LIMIT {limit};").format(apple=APPLE, limit=int(limit))
    return f"sqlite3 -readonly -separator $'\\x1f' -newline $'\\x1e' \"$HOME/{DB}\" \"{sql}\"\n"


def argv(host: str) -> list[str]:
    """ssh to `host`, bash reading the script from stdin (MAC_SSH in channels/page.py, the mac tool's own way)."""
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT}", host, "/bin/bash", "-s"]


def _run(cmd: list[str], stdin: bytes, timeout: float) -> tuple[int | None, bytes, bool]:
    """(exit code, stdout, timed out): None for the code on a timeout, 255 on an OSError (ssh not found and the like)."""
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, False
    except subprocess.TimeoutExpired:
        return None, b"", True
    except OSError:
        return 255, b"", False


def parse(out: str) -> list[dict[str, Any]]:
    """The sqlite output as a list of messages, newest first: each row split on US into when, from_me, who and text."""
    msgs: list[dict[str, Any]] = []
    for row in out.split(RS):
        if not row.strip("\n"):
            continue
        cols = row.split(US)
        if len(cols) < 4:
            continue
        when, from_me, who, text = cols[0], cols[1], cols[2], US.join(cols[3:])
        try:
            at = float(when)
        except ValueError:
            continue
        msgs.append({"at": at, "from_me": from_me.strip() == "1", "who": who.strip(),
                     "text": text.strip("\n")[:TEXT_MAX]})
    return msgs


def read(host: str, *, limit: int = LIMIT, run: Run | None = None) -> dict[str, Any]:
    """The newest texts off the Mac now: {"ok": True, "messages": [...]}; else {"ok": False, "asleep": True} when ssh
    could not reach it (asleep, away or refusing the key), or {"ok": False, "asleep": False, "error"} when it answered but
    the read failed (the login cannot read the database, or it was locked)."""
    if not host:
        return {"ok": False, "asleep": True}
    code, out, timed = (run or _run)(argv(host), script(limit).encode(), READ_TIMEOUT)
    if timed or code is None or code == 255:
        return {"ok": False, "asleep": True}
    if code != 0:
        return {"ok": False, "asleep": False, "error": (out.decode("utf-8", "replace").strip() or f"exit {code}")[:200]}
    return {"ok": True, "messages": parse(out.decode("utf-8", "replace"))}


def path(state: Path) -> Path:
    return state / FOLDER / FILE


def write_copy(state: Path, host: str, messages: list[dict[str, Any]], at: float | None = None) -> dict[str, Any]:
    """The copy written 0600 under <state>/texts/, its parent 0700; returns what was written."""
    data = {"at": at if at is not None else time.time(), "host": host, "messages": messages}
    p = path(state)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(p, json.dumps(data))
    return data


def read_copy(state: Path) -> dict[str, Any] | None:
    """The last copy, or None when there is none yet or it cannot be read."""
    try:
        got = json.loads(path(state).read_text())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) and isinstance(got.get("messages"), list) else None


def refresh(state: Path, host: str, *, run: Run | None = None) -> dict[str, Any] | None:
    """Read the Mac if it answers and copy the newest texts to the box; returns the copy, or None when the Mac did not
    answer or the read failed, so the last copy stands. Nothing is written on a failed read."""
    if not host:
        return None
    r = read(host, run=run)
    return write_copy(state, host, r["messages"]) if r.get("ok") else None
