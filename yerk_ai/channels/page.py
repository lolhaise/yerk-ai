"""A live page: a room as a browser tab where you write a program and its tests, draw on them and talk with the room, served by yerkd's local web server.

- A room with `page = true` in rooms.toml has one page at /page on yerkd's port, opened at the address
  `yerk room page NAME` prints. The room and its key ride in the address's fragment, which a browser never sends, and
  every call the page makes carries the key in the x-yerk-secret header: one key a room, a 0600 file in the state
  folder's secrets/pages/, which no room reads and no worker sees. It opens that room's page routes and nothing else, and
  no other secret opens them.
- What the owner does on the page reaches the room as the owner's messages, folded into a running turn as a Discord
  message is (Room.submit): each keystroke lands at once in the room's work/page/events.jsonl (the span it changed, a
  line each) and the program and its tests in work/page/, for the room to Read mid-turn; a stroke drawn on the page, a
  line said, the timer's end or a stall goes at once as a message, and typing goes as one message once QUIET seconds pass
  with none (the change since the last message as a diff, and the tests' newest result).
- The tests run on the page's call, never the model's: the program and its unittest file in a fresh folder under
  `python3 -I`, capped in CPU, memory and wall clock (RUN_SECONDS), one run at a time a room; the result goes back to the
  page at once and rides in the next message.
- What the room says renders on the page: its replies, its notices and its sends to itself (Mirror), and what the
  `page` tool draws: a new program or tests, marks on lines (a highlight, an arrow, each with a note), a one-line hint,
  the bar (labels and values, a countdown) and the stall wait. A tab that opens or reconnects gets the whole state first.
"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import hmac
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .. import words
from ..config import read_secret, write_private
from ..ledger import Ledger
from ..room import Inbound, Room

log = logging.getLogger("yerk.page")
HTML = Path(__file__).with_name("page.html")
PATH = "/page"           # the page itself: no key, no room, no data; its calls go under PATH + "/"
CHANNEL = "page"         # the ledger's channel for a page's messages: Discord's recover never reads them
QUIET = 1.5              # seconds with no keystroke before the typing goes to the room as one message
RUN_SECONDS = 10         # a test run's wall clock, and its CPU seconds
RUN_MEMORY = 1 << 30     # a test run's address space, in bytes
OUTPUT_MAX = 4000        # characters of what a run printed that come back
TEXT_MAX = 100_000       # characters of a program or its tests the page may send
DIFF_LINES = 60          # lines of the change one message carries; the whole program is in work/page/
LINES_KEPT = 50          # the room's newest lines a tab shows when it opens
POINTS_MAX = 400         # points of one stroke kept in the events file
NOTE_MAX = 200           # characters of a mark's note or the hint
PROGRAM, TESTS = "solution.py", "test_solution.py"
MARKS = ("highlight", "arrow")
RUNNER = '''import importlib, json, os, resource, sys, traceback, unittest
here, cap, mem = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
resource.setrlimit(resource.RLIMIT_CPU, (cap + 1, cap + 1))
resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
sys.path.insert(0, here)
out = {"tests": [], "error": "", "line": 0}
def at(tb, name):
    return next((f.lineno for f in reversed(traceback.extract_tb(tb)) if os.path.basename(f.filename) == name), 0) or 0
def said(kind, e):
    return "".join(traceback.format_exception_only(kind, e)).replace(here + os.sep, "").strip()[-300:]
def write():
    with open(here + "/result.json", "w") as f:
        json.dump(out, f)
try:
    mod = importlib.import_module("test_solution")
except BaseException as e:
    out["error"], out["file"] = said(type(e), e), os.path.basename(getattr(e, "filename", "") or "")
    out["line"] = getattr(e, "lineno", 0) if isinstance(e, SyntaxError) else at(e.__traceback__, "solution.py")
    write()
    sys.exit(0)
class Result(unittest.TestResult):
    def addSuccess(self, test):
        out["tests"].append({"name": getattr(test, "_testMethodName", str(test)), "ok": True})
    def bad(self, test, err, kind):
        out["tests"].append({"name": getattr(test, "_testMethodName", str(test)), "ok": False, "kind": kind,
                             "message": said(err[0], err[1]), "line": at(err[2], "solution.py"), "test_line": at(err[2], "test_solution.py")})
    def addFailure(self, test, err):
        self.bad(test, err, "fail")
    def addError(self, test, err):
        self.bad(test, err, "error")
unittest.defaultTestLoader.loadTestsFromModule(mod).run(Result())
write()
'''


def run_tests(code: str, tests: str, seconds: int = RUN_SECONDS, python: str = sys.executable) -> dict[str, Any]:
    """The program and its unittest file run in a fresh folder under `python -I`, capped (RUN_SECONDS of wall clock, a second
    more of CPU behind it, and RUN_MEMORY), killed with its whole group past the cap: each test passed or failed with its message and the program's line
    it failed at, or the error that stopped the tests loading (a syntax error, a failed import) with its line."""
    with tempfile.TemporaryDirectory(prefix="yerk-page-") as d:
        here = Path(d)
        (here / PROGRAM).write_text(code)
        (here / TESTS).write_text(tests)
        (here / "run.py").write_text(RUNNER)
        start = time.monotonic()
        p = subprocess.Popen([python, "-I", str(here / "run.py"), d, str(seconds), str(RUN_MEMORY)], cwd=d, start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env={"PATH": os.defpath, "HOME": d})
        try:
            printed, _ = p.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            return {"ok": False, "ran": 0, "passed": 0, "tests": [], "error": words.PAGE_RAN_PAST.format(n=seconds), "line": 0,
                    "output": "", "seconds": round(time.monotonic() - start, 2)}
        took, printed_text = round(time.monotonic() - start, 2), printed.decode(errors="replace")[-OUTPUT_MAX:]
        try:
            out = json.loads((here / "result.json").read_text())
        except (OSError, ValueError):
            past = p.returncode in (-signal.SIGXCPU, -signal.SIGKILL)     # the CPU cap, the backstop behind the clock
            return {"ok": False, "ran": 0, "passed": 0, "tests": [], "line": 0,
                    "error": words.PAGE_RAN_PAST.format(n=seconds) if past else words.PAGE_RUN_DIED,
                    "output": printed_text, "seconds": took}
    ran, passed = len(out["tests"]), sum(1 for t in out["tests"] if t["ok"])
    return {"ok": not out["error"] and ran > 0 and passed == ran, "ran": ran, "passed": passed, "tests": out["tests"],
            "error": out["error"], "line": out.get("line", 0), "output": printed_text, "seconds": took}


def run_line(r: dict[str, Any]) -> str:
    """A run as the room reads it: how many passed, then the first failure and the program's line it failed at, or the error
    that stopped the tests loading."""
    if r.get("error"):
        return words.PAGE_RUN_ERROR.format(error=r["error"], line=f" (line {r['line']})" if r.get("line") else "")
    first = next((t for t in r.get("tests", []) if not t["ok"]), None)
    line = words.PAGE_RUN.format(passed=r.get("passed", 0), ran=r.get("ran", 0))
    if first is None:
        return line
    where = (f" at line {first['line']}" if first.get("line") else f" at line {first['test_line']} of the tests" if first.get("test_line")
             else "")
    return line + "; " + words.PAGE_RUN_FAILED.format(name=first["name"], line=where,
                                                       message=first["message"].splitlines()[-1] if first["message"] else first["kind"])


def change(old: str, new: str) -> dict[str, Any]:
    """The one span a keystroke changed: where it starts (its line and column, from 1) and the text it took out and put in."""
    a = 0
    while a < min(len(old), len(new)) and old[a] == new[a]:
        a += 1
    b = 0
    while b < min(len(old), len(new)) - a and old[len(old) - 1 - b] == new[len(new) - 1 - b]:
        b += 1
    head = old[:a]
    return {"line": head.count("\n") + 1, "col": a - (head.rfind("\n") + 1) + 1, "del": old[a:len(old) - b], "ins": new[a:len(new) - b]}


def diff(old: str, new: str, name: str) -> str:
    """The change since the room last heard the file, as a unified diff, DIFF_LINES at most (the rest is in the file)."""
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), name, name, lineterm="", n=1))[2:]
    if len(lines) > DIFF_LINES:
        lines = [*lines[:DIFF_LINES], words.PAGE_DIFF_CUT.format(n=len(lines) - DIFF_LINES, name=name)]
    return "\n".join(lines)


def span(lines: Any) -> tuple[int, int] | None:
    """A stroke's lines as the page measured them, [first, last] from 1, else None."""
    if isinstance(lines, list) and len(lines) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in lines):
        first, last = sorted(lines)
        return (first, last) if first >= 1 else None
    return None


@dataclass
class Sheet:
    """One room's page: what it shows, what waits to go to the room, and the tabs open on it."""
    room: Room
    folder: Path
    key: str
    code: str = ""
    tests: str = ""
    heard: tuple[str, str] = ("", "")          # the program and its tests as the room last heard them
    marks: list[dict[str, Any]] = field(default_factory=list)
    hint: str = ""
    bar: dict[str, str] = field(default_factory=dict)
    timer: dict[str, float] = field(default_factory=dict)   # {"seconds": n, "from": when it was set}, while it runs
    stall: int = 0
    lines: list[dict[str, Any]] = field(default_factory=list)   # the room's newest lines
    run: dict[str, Any] | None = None
    run_heard: bool = True
    waiting: list[str] = field(default_factory=list)          # lines for the next message: a stroke, a said line, an event
    tabs: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    quiet: asyncio.TimerHandle | None = None
    running: asyncio.Lock = field(default_factory=asyncio.Lock)

    def state(self) -> dict[str, Any]:
        """Everything a tab shows, for one that opens or reconnects."""
        return {"state": {"room": self.room.name, "code": self.code, "tests": self.tests, "marks": self.marks, "hint": self.hint,
                          "bar": self.bar, "timer": self.timer, "stall": self.stall, "lines": self.lines, "run": self.run}}


class Page:
    """Every room's page: the keys, the events in, the runs, and what the rooms draw."""

    def __init__(self, state: Path, ledger: Ledger, capture: Callable[..., Any] | None = None, quiet: float = QUIET):
        self.state, self.ledger, self.capture, self.quiet = state, ledger, capture, quiet
        self.sheets: dict[str, Sheet] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.closing = False

    def key_file(self, room: str) -> Path:
        return self.state / "secrets" / "pages" / f"{room}.secret"

    def add_room(self, room: Room) -> None:
        """The room's page, its key minted once, what it showed read back from its folder; a page message a restart left
        open is closed, since its keystrokes are in the room's file and a stale one would run as new."""
        folder = self.state / "rooms" / room.name / "work" / "page"
        folder.mkdir(parents=True, exist_ok=True)
        s = self.sheets[room.name] = Sheet(room, folder, read_secret(self.key_file(room.name), create=True))
        with contextlib.suppress(OSError):
            s.code = (folder / PROGRAM).read_text()
        with contextlib.suppress(OSError):
            s.tests = (folder / TESTS).read_text()
        s.heard = (s.code, s.tests)
        for row in self.ledger.inbound_open(room.name):
            if row["channel"] == CHANNEL:
                self.ledger.inbound_set(row["message_id"], state="abandoned", note="a page message cut by a restart; its keystrokes are in the room's file")

    def begin(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def room_of(self, key: str) -> str | None:
        """The room whose page `key` opens, compared in constant time; None: no page's."""
        found = None
        for name, s in self.sheets.items():
            if key and s.key and hmac.compare_digest(key, s.key):
                found = name
        return found

    def mirror(self, channel: Any) -> Mirror:
        return Mirror(channel, self)

    def html(self) -> str:
        """The page itself: the same for every room, no room, key or data in it."""
        return HTML.read_text()

    # what the owner does on the page
    def _log(self, s: Sheet, row: dict[str, Any]) -> None:
        try:
            with (s.folder / "events.jsonl").open("a") as f:
                f.write(json.dumps({"t": round(time.time(), 3), **row}, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("page %s: an event not written: %s", s.room.name, e)

    async def event(self, room: str, ev: dict[str, Any]) -> dict[str, Any]:
        """One thing the owner did on the page: logged at once, then a message to the room now (a stroke, a said line, the
        timer's end, a stall) or once the typing pauses (an edit)."""
        if (s := self.sheets.get(room)) is None:
            return {"ok": False, "error": words.PAGE_NONE.format(room=room)}
        self.loop = asyncio.get_running_loop()
        kind = str(ev.get("kind") or "")
        if kind == "edit":
            pane = "tests" if ev.get("pane") == "tests" else "code"
            new = str(ev.get("text") or "")[:TEXT_MAX]
            old = s.tests if pane == "tests" else s.code
            if new == old:
                return {"ok": True}
            s.tests, s.code = (new, s.code) if pane == "tests" else (s.tests, new)
            write_private(s.folder / (TESTS if pane == "tests" else PROGRAM), new)
            self._log(s, {"kind": "edit", "pane": pane, **change(old, new)})
            self._arm(s)
            return {"ok": True}
        if kind == "stroke":
            lines, pane = span(ev.get("lines")), "tests" if ev.get("pane") == "tests" else "program"
            points = [p for p in ev.get("points") or [] if isinstance(p, list) and len(p) == 2][:POINTS_MAX]
            self._log(s, {"kind": "stroke", "pane": pane, "lines": list(lines) if lines else None, "points": points})
            s.waiting.append(words.PAGE_STROKE.format(where=(words.PAGE_LINES.format(first=lines[0], last=lines[1], pane=pane)
                                                             if lines and lines[0] != lines[1] else
                                                             words.PAGE_LINE.format(line=lines[0], pane=pane) if lines else words.PAGE_NO_LINE),
                                                      n=len(points)))
        elif kind == "say":
            if not (said := str(ev.get("text") or "").strip()[:TEXT_MAX]):
                return {"ok": True}
            self._log(s, {"kind": "say", "text": said})
            s.waiting.append(words.PAGE_SAID.format(text=said))
        elif kind == "timer":
            self._log(s, {"kind": "timer"})
            s.timer = {}
            s.waiting.append(words.PAGE_TIMER_DONE)
        elif kind == "stall":
            self._log(s, {"kind": "stall", "seconds": s.stall})
            s.waiting.append(words.PAGE_STALL.format(n=s.stall))
        else:
            return {"ok": False, "error": words.PAGE_KIND.format(kind=kind[:40])}
        self.flush(room)
        return {"ok": True}

    async def run(self, room: str, req: dict[str, Any], heard: bool = False) -> dict[str, Any]:
        """The tests, run on the page's call with what it shows (one run at a time a room), the result kept for the next
        message and logged; `heard`: the room's own run of what it drew, whose result it reads in its tool call."""
        if (s := self.sheets.get(room)) is None:
            return {"ok": False, "error": words.PAGE_NONE.format(room=room)}
        self.loop = asyncio.get_running_loop()
        if isinstance(req.get("code"), str) or isinstance(req.get("tests"), str):     # what the tab shows, as it ran
            await self.event(room, {"kind": "edit", "pane": "code", "text": req.get("code", s.code)})
            await self.event(room, {"kind": "edit", "pane": "tests", "text": req.get("tests", s.tests)})
        async with s.running:
            r = await asyncio.to_thread(run_tests, s.code, s.tests)
        changed = s.run is None or (s.run.get("passed"), s.run.get("ran"), s.run.get("error")) != (r["passed"], r["ran"], r["error"])
        s.run, s.run_heard = r, heard or (s.run_heard and not changed)
        self._log(s, {"kind": "run", "passed": r["passed"], "ran": r["ran"], "error": r["error"][:300], "seconds": r["seconds"]})
        if not s.run_heard or (s.code, s.tests) != s.heard:
            self._arm(s)
        return r

    def _arm(self, s: Sheet) -> None:
        if s.quiet is not None:
            s.quiet.cancel()
        s.quiet = asyncio.get_running_loop().call_later(self.quiet, self._paused, s.room.name)

    def _paused(self, room: str) -> None:
        """The typing paused: what waits goes, unless the tests still run, whose end arms the wait again, so a pause is one
        message with its run in it."""
        if not self.sheets[room].running.locked():
            self.flush(room)

    def message(self, s: Sheet) -> str:
        """What waits to go to the room, as one message: the change to the program and its tests since it last heard them,
        the newest run when it says something new, and each stroke, said line and event in order; "" when nothing waits."""
        parts = []
        for (old, new, name) in ((s.heard[0], s.code, PROGRAM), (s.heard[1], s.tests, TESTS)):
            if old != new:
                parts.append(words.PAGE_TYPED.format(name=name, diff=diff(old, new, name)))
        if s.run is not None and not s.run_heard:
            parts.append(run_line(s.run))
        parts += s.waiting
        return "\n".join([words.PAGE_FROM, *parts]) if parts else ""

    def flush(self, room: str) -> str:
        """What waits, as the owner's message to the room now: ledgered, filed in the day log and submitted, so a busy room
        reads it inside its running turn. Its id, or "" when nothing waited."""
        s = self.sheets[room]
        if s.quiet is not None:
            s.quiet.cancel()
            s.quiet = None
        if not (text := self.message(s)):
            return ""
        s.heard, s.run_heard, s.waiting = (s.code, s.tests), True, []
        mid = f"page-{room}-{time.time_ns() // 1000}"
        self.ledger.inbound_add(mid, room, CHANNEL, "owner", len(text), created=time.time())
        if self.capture is not None:
            try:
                self.capture(room, mid, "owner", text, [])
            except OSError as e:
                log.warning("page %s: message %s not filed in the day's log: %s", room, mid, e)
        s.room.submit(Inbound(mid, "owner", text))
        return mid

    # what the room draws
    def draw(self, room: str, req: dict[str, Any]) -> dict[str, Any]:
        """The `page` tool: each part given replaces what the page shows of it, and goes to every open tab now."""
        if (s := self.sheets.get(room)) is None:
            return {"ok": False, "error": words.PAGE_NONE.format(room=room)}
        op: dict[str, Any] = {}
        for pane, name in (("code", PROGRAM), ("tests", TESTS)):
            if isinstance(req.get(pane), str):
                text = req[pane][:TEXT_MAX]
                setattr(s, pane, text)
                write_private(s.folder / name, text)
                op[pane] = text
        if "code" in op or "tests" in op:
            s.heard, s.run = (s.code, s.tests), None
        if isinstance(req.get("marks"), list):
            s.marks = op["marks"] = [{"line": int(m["line"]), "kind": m.get("kind") if m.get("kind") in MARKS else "highlight",
                                      "text": str(m.get("text") or "")[:NOTE_MAX]}
                                     for m in req["marks"] if isinstance(m, dict) and isinstance(m.get("line"), int) and m["line"] >= 1]
        if isinstance(req.get("hint"), str):
            s.hint = op["hint"] = req["hint"].strip()[:NOTE_MAX]
        if isinstance(req.get("bar"), dict):
            s.bar = op["bar"] = {str(k)[:24]: str(v)[:40] for k, v in list(req["bar"].items())[:8]}
        if isinstance(req.get("timer"), int) and req["timer"] >= 0:
            s.timer = op["timer"] = {"seconds": req["timer"], "from": round(time.time(), 3)} if req["timer"] else {}
        if isinstance(req.get("stall"), int) and req["stall"] >= 0:
            s.stall = op["stall"] = req["stall"]
        if not op:
            return {"ok": False, "error": words.PAGE_NOTHING}
        self._log(s, {"kind": "drawn", **{k: v for k, v in op.items() if k not in ("code", "tests")},
                      **{k: len(v) for k, v in op.items() if k in ("code", "tests")}})
        self._push(s, {"draw": op})
        return {"ok": True, "tabs": len(s.tabs), **({} if s.tabs else {"note": words.PAGE_NO_TAB})}

    def said(self, room: str, text: str, kind: str = "reply") -> None:
        """A line the room said, from any thread: kept for a tab that opens later and shown on every open one."""
        if (s := self.sheets.get(room)) is None or not text.strip() or text.strip() == words.NO_REPLY:
            return
        line = {"text": text, "kind": kind, "t": round(time.time(), 3)}
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._said, s, line)
        else:
            self._said(s, line)

    def _said(self, s: Sheet, line: dict[str, Any]) -> None:
        s.lines = [*s.lines, line][-LINES_KEPT:]
        self._push(s, {"line": line})

    def _push(self, s: Sheet, op: dict[str, Any]) -> None:
        for q in list(s.tabs):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(op)

    def close(self) -> None:
        """yerkd stops: every tab's feed ends, so an open tab never holds the HTTP server's stop; the tab reconnects to the
        next start."""
        self.closing = True
        for s in self.sheets.values():
            self._push(s, {"closing": True})

    async def stream(self, room: str, beat: float = 15.0) -> AsyncIterator[bytes]:
        """A tab's feed, one JSON object a line: the whole state first, then each line said and each thing drawn, and a
        blank line every `beat` seconds so a dead connection is seen; it ends when yerkd stops (close)."""
        s = self.sheets[room]
        self.loop = asyncio.get_running_loop()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        s.tabs.append(q)
        try:
            yield (json.dumps(s.state(), ensure_ascii=False) + "\n").encode()
            while not self.closing:
                try:
                    op = await asyncio.wait_for(q.get(), beat)
                except asyncio.TimeoutError:
                    yield b"\n"
                    continue
                if op.get("closing"):
                    return
                yield (json.dumps(op, ensure_ascii=False) + "\n").encode()
        finally:
            with contextlib.suppress(ValueError):
                s.tabs.remove(q)


class Mirror:
    """A room's channel with its page beside it: every call goes to the channel as before, and the room's replies, notices and
    sends to itself also render on the page."""

    def __init__(self, inner: Any, page: Page):
        self.inner, self.page = inner, page

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def reply(self, room: str, text: str, inbound: list[str], card: str | None = None, key: str | None = None) -> list[str]:
        ids: list[str] = self.inner.reply(room, text, inbound, card, key)
        self.page.said(room, text)
        return ids

    def notice(self, room: str, text: str, inbound: list[str], card: str | None = None, key: str | None = None) -> list[str]:
        ids: list[str] = self.inner.notice(room, text, inbound, card, key)
        self.page.said(room, text, "notice")
        return ids

    async def send_text(self, room: str, text: str, key: str | None = None) -> list[str]:
        ids: list[str] = await self.inner.send_text(room, text, key)
        self.page.said(room, text)
        return ids


def address(state: Path, room: str) -> str:
    """The address a room's page opens at: yerkd's port from its yerkd.json, the room and its key in the fragment."""
    try:
        port = int(json.loads((state / "yerkd.json").read_text())["port"])
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(words.PAGE_NO_DAEMON) from None
    if not (key := read_secret(state / "secrets" / "pages" / f"{room}.secret")):
        raise SystemExit(words.PAGE_NO_KEY.format(room=room))
    return f"http://127.0.0.1:{port}{PATH}#room={room}&key={key}"
