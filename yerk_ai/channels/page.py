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
- The tests run with no model call: the program and its unittest file in a fresh folder under `python3 -I`, capped in
  CPU, memory and wall clock (RUN_SECONDS), one run at a time a room, inside bubblewrap (sandbox): no network, no home,
  state or private folder, nothing writable but the run's own folder, a PID namespace of its own that dies with the run.
  The room's `page` tool hands it code too, so it is code a model wrote: with no bwrap that starts, nothing runs and the
  run says so. The result goes back to the page at once and rides in the next message.
- What the room says renders on the page's chat, a pane of its own beside the program, beside what the owner said there
  (a line in its say box, or a # comment line typed in the program, which also reaches the room as said), each line
  time-stamped and read the way Discord reads a message (rich), the newest LINES_KEPT kept in work/page/ across a
  restart: its replies, its notices and its sends to itself (Mirror), and what the `page` tool draws: a new program or tests, marks on lines (a highlight, an arrow, each with a note), a one-line hint,
  the bar (labels and values, a countdown) and the stall wait. A tab that opens or reconnects gets the whole state first.
- The talk can live in the program instead (talk): with the page's chat closed to nothing, the room's lines also go into the
  program as #> comment lines under the owner's last # line (into), which yerkd writes in its copy and every open tab writes
  in its own text at the same line, so the owner's caret and typing since stay, no more than TEXT_MAX holds, the room's
  marks under them moved down; a # line ended with Enter goes at once, and a program whose tests are blank runs alone,
  its prints shown large and said to the room.
- On the owner's ask the room's `page` tool turns its page on with no restart (Yerkd.page_switch) and yerkd opens it on
  the owner's screen with the box's own opener (opener): the address it opens holds a one-time ticket, never the key,
  since an opener's command line is readable by every local user (/proc); the page trades the ticket once, within
  TICKET_SECONDS, for its room and key (redeem), so nobody pastes the address and no room ever holds it. With `where`
  mac it opens on the Mac instead (mac): ssh to it runs `open` there on the address as the Mac reaches yerkd,
  handed on ssh's stdin, so not even the ticket is on a command line. A tab keeps its room and key in its own session
  storage (gone when the tab closes), so a reload of a page whose address bar lost them opens it again.
- yerkd serves loopback alone unless [page] listen names the box's tailnet or LAN address (listen): then the page's
  routes, and nothing else (mcp_server.Guard), are on that address too, on yerkd's port (bind), so the owner's Mac and
  phone open it, and the address `yerk room page` prints and the Mac's open carry that host (reach), the key still only
  in the fragment.
"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import errno
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlencode, urlsplit

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
PRINTED = 1000           # characters of what a run printed, its end, that the room reads in the next message
TALK_WIDTH = 88          # columns of one #> line the room's words are wrapped to in the program (a fenced line kept whole)
TEXT_MAX = 100_000       # characters of a program or its tests the page may send, and of a chat line, either side's
DIFF_LINES = 60          # lines of the change one message carries, and # comments said; the whole program is in work/page/
LINES_KEPT = 200         # the chat's newest lines, the owner's and the room's, a tab shows when it opens
POINTS_MAX = 400         # points of one stroke kept in the events file
NOTE_MAX = 200           # characters of a mark's note or the hint
TICKET_SECONDS = 60       # a one-time address opened on the owner's screen is good this long, and once
OPEN_SECONDS = 15         # an opener's wall clock: it hands the address to the browser and ends (ssh to the Mac too)
# the Mac's opener: ssh to it, its script on stdin (as the hands plug-in's mac tool runs one), so the one-time address is on
# no command line of the box's, where every local user reads it (/proc); `open` there hands it to the Mac's default browser
MAC_SSH = ("ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
MAC_OPEN = "open {url}\n"
# [page] listen's socket binds before its address is up (tailscale at boot), so the page is there once it is: IP_FREEBIND,
# Linux's 15, which some Pythons do not name; the tailnet's own ranges, encrypted end to end, where a LAN carries it plain
FREEBIND = getattr(socket, "IP_FREEBIND", 15 if sys.platform == "linux" else 0)
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
# Windows' own opener under WSL (a Windows browser reaches the box's local address): it hands the address whole to the
# default browser, where cmd.exe's start would split it at its `&`
WSL_OPEN = ("/mnt/c/Windows/System32/rundll32.exe", "url.dll,FileProtocolHandler")
PROGRAM, TESTS, CHAT, TALK = "solution.py", "test_solution.py", "chat.json", "talk.json"
MARKS = ("highlight", "arrow")
# the bar as a scoreboard, read from the room's words alone (cell): a rank's tiers, low to high, each its colour on the
# page (.tier-NAME in page.html); the labels whose value is a rank; a division after a tier; a number and a short unit
TIERS = ("iron", "bronze", "silver", "gold", "platinum", "emerald", "diamond", "master", "grandmaster", "challenger")
RANKS = ("rank", "tier", "league", "division")
DIVISION = re.compile(r"(?:[1-5]|I{1,3}|IV|V)?", re.I)
NUM = r"(\d[\d,]*(?:\.\d+)?)"
UNIT = r"(?:\s*([^\d\s/%:.,][^\s]{0,7}))?"
METER, PERCENT = re.compile(rf"{NUM}\s*(?:/|of|out of)\s*{NUM}{UNIT}", re.I), re.compile(rf"{NUM}\s*%")
NUMBER = re.compile(rf"([+\-−]?){NUM}{UNIT}")
# a line said, read the way Discord reads a message (rich): a fenced block, else a row a line, its look from its start
# (a heading, subtext, a quote, a list item), and in a row its runs: code, an escape, a link (masked or bare), then the
# marks, each a letter of the run's look (bold, underline, italic, strike, a spoiler hidden until it is clicked). A mark's
# words, and a link's, never run past its own mark: one never closed is read only up to the next, so a row costs time in
# step with its length, never its square (a row of 100,000 characters of `*a ` took 27 s on yerkd's only loop)
FENCE = re.compile(r"```(?:([\w+#.-]*)\n)?(.*?)```", re.S)
ROW = re.compile(r"(?P<h>#{1,3}) (?=\S)|-# (?=\S)|(?P<all>>>> ?)|> |(?P<in>\s*)(?P<li>[-*•]|\d{1,3}\.) (?=\S)")
INLINE = re.compile("|".join((r"`(?P<code>[^`\n]+)`", r"\\(?P<esc>[\\*_~`|>#\[\]()-])",
                              r"\[(?P<label>[^\[\]\n]+)\]\(<?(?P<to>https?://[^\s<>()]+)>?\)", r"<?(?P<url>https?://[^\s<>]*[^\s<>.,:;!?'\")\]])>?",
                              r"\*\*\*(?P<bi>\S(?:(?:(?!\*\*\*).)*?\S)?)\*\*\*", r"\*\*(?P<b>\S(?:(?:(?!\*\*).)*?\S)?)\*\*",
                              r"__(?P<u>\S(?:(?:(?!__).)*?\S)?)__", r"\*(?P<i>[^\s*](?:[^*]*?[^\s*])?)\*", r"(?<!\w)_(?P<i_>\S(?:[^_]*?\S)?)_(?!\w)",
                              r"~~(?P<s>\S(?:(?:(?!~~).)*?\S)?)~~", r"\|\|(?P<h>.+?)\|\|")))
LOOKS = {"bi": "bi", "b": "b", "u": "u", "i": "i", "i_": "i", "s": "s", "h": "h"}
# blanks str.split() keeps that a room could pad the bar's words with (plain): fillers read as a space, zero-width ones go
BLANKS = str.maketrans("\u2800\u3164\uffa0", "   ", "\u200b\u2060\ufeff")
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
if not open(here + "/test_solution.py").read().strip():
    out["main"] = True
    try:
        import runpy
        runpy.run_path(here + "/solution.py", run_name="__main__")
    except SystemExit:
        pass
    except BaseException as e:
        out["error"] = said(type(e), e)
        out["line"] = getattr(e, "lineno", 0) if isinstance(e, SyntaxError) else at(e.__traceback__, "solution.py")
    sys.stdout.flush()
    write()
    sys.exit(0)
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


def sandbox(python: str, here: str) -> list[str] | None:
    """The bubblewrap command a run goes in: every namespace its own (no network; its own PID namespace, so whatever it
    starts dies when it ends or with yerkd's kill of it), the system's /usr and the Python's own install read-only, the
    run's folder the one thing writable, an empty /tmp, no home, state or private folder, and no environment but PATH and
    HOME (the run's folder). None: no bwrap on the box, so nothing runs (a room hands the runner code: never bare)."""
    if (bwrap := shutil.which("bwrap")) is None:
        return None
    real = os.path.realpath(python)
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--setenv", "PATH", os.defpath,
            "--setenv", "HOME", here, "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for top in ("/bin", "/sbin", "/lib", "/lib32", "/lib64"):            # a merged /usr links these; an older system has them
        if os.path.islink(top):
            args += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            args += ["--ro-bind", top, top]
    if not real.startswith("/usr/"):          # a Python outside /usr (uv's, CI's): its binary and its lib folder, read-only
        args += ["--ro-bind", real, real] + [a for lib in (str(Path(real).parents[1] / "lib"),) if os.path.isdir(lib) for a in ("--ro-bind", lib, lib)]
    return args + ["--bind", here, here, "--chdir", here, real]


def sandbox_starts(python: str = sys.executable) -> bool:
    """Whether the sandbox starts on this box (bwrap there, and user namespaces let it): said at a page room's start."""
    with tempfile.TemporaryDirectory(prefix="yerk-page-") as d:
        if (box := sandbox(python, d)) is None:
            return False
        try:
            return subprocess.run(box + ["-I", "-c", "pass"], capture_output=True, timeout=RUN_SECONDS).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def run_tests(code: str, tests: str, seconds: int = RUN_SECONDS, python: str = sys.executable) -> dict[str, Any]:
    """The program and its unittest file run in a fresh folder under `python -I`, in the sandbox (bubblewrap) and capped
    (RUN_SECONDS of wall clock, a second more of CPU behind it, and RUN_MEMORY), killed with its whole group past the cap:
    each test passed or failed with its message and the program's line it failed at, or the error that stopped the tests
    loading (a syntax error, a failed import) with its line; blank tests, the program run alone as a script (`main`), and
    the error that stopped it with its line. No sandbox that starts: nothing ran, and the error says so."""
    with tempfile.TemporaryDirectory(prefix="yerk-page-") as d:
        here = Path(d)
        (here / PROGRAM).write_text(code)
        (here / TESTS).write_text(tests)
        (here / "run.py").write_text(RUNNER)
        if (box := sandbox(python, d)) is None:
            return {"ok": False, "ran": 0, "passed": 0, "tests": [], "error": words.PAGE_NO_SANDBOX, "line": 0, "output": "", "seconds": 0}
        start = time.monotonic()
        p = subprocess.Popen(box + ["-I", str(here / "run.py"), d, str(seconds), str(RUN_MEMORY)], cwd=d, start_new_session=True,
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
            past = p.returncode in (-signal.SIGXCPU, -signal.SIGKILL, 128 + signal.SIGXCPU, 128 + signal.SIGKILL)   # the CPU cap
            boxed = printed_text.startswith("bwrap:")                    # the sandbox would not start: nothing ran in it
            return {"ok": False, "ran": 0, "passed": 0, "tests": [], "line": 0,
                    "error": words.PAGE_NO_SANDBOX if boxed else words.PAGE_RAN_PAST.format(n=seconds) if past else words.PAGE_RUN_DIED,
                    "output": printed_text, "seconds": took}
    ran, passed, main = len(out["tests"]), sum(1 for t in out["tests"] if t["ok"]), bool(out.get("main"))
    return {"ok": not out["error"] and (main or ran > 0 and passed == ran), "ran": ran, "passed": passed, "tests": out["tests"],
            "error": out["error"], "line": out.get("line", 0), "output": printed_text, "seconds": took, **({"main": True} if main else {})}


def run_line(r: dict[str, Any]) -> str:
    """A run as the room reads it: how many passed, then the first failure and the program's line it failed at, or the error
    that stopped the tests loading (the program run alone: that it ran, or what stopped it); then the end of what it printed."""
    printed = r.get("output", "").strip()[-PRINTED:]
    return run_said(r) + ("\n" + words.PAGE_PRINTED.format(output=printed) if printed else "")


def run_said(r: dict[str, Any]) -> str:
    where = f" (line {r['line']})" if r.get("line") else ""
    if r.get("main"):
        return words.PAGE_MAIN_ERROR.format(error=r["error"], line=where) if r.get("error") else words.PAGE_MAIN
    if r.get("error"):
        return words.PAGE_RUN_ERROR.format(error=r["error"], line=where)
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


def plain(said: str) -> str:
    """One of the bar's labels or values as its words alone: a run of spaces of any kind one space, none at either end, since
    the page spaces its cells itself and padding only shows as a blank in a cell, or keeps a tier word from its division."""
    return " ".join(said.translate(BLANKS).split())


def cell(label: str, value: str) -> dict[str, Any]:
    """One of the bar's labels and values as the page's scoreboard shows it, read from the words alone, since a room sets
    only the words and the look is the page's: a tier word (TIERS) with a division or none after it, or any value under a
    rank label (RANKS), a rank badge in its tier's colour; N/M, N of M, N% or a number under LP or before LP, a bar filled
    that far (LP of 100); a number, big, a signed one up or down; anything else, words."""
    out: dict[str, Any] = {"label": label, "value": value, "shape": "text"}
    said, ranked = value.strip(), label.strip().lower() in RANKS
    first, _, rest = said.partition(" ")
    if first.lower() in TIERS and (ranked or DIVISION.fullmatch(rest.strip())):
        return out | {"shape": "rank", "tier": first.lower(), "name": first, "division": rest.strip()}
    if ranked:
        return out | {"shape": "rank", "tier": "", "name": said, "division": ""}
    if m := METER.fullmatch(said) or PERCENT.fullmatch(said):
        n, of = float(m[1].replace(",", "")), float(m[2].replace(",", "")) if m.re is METER else 100.0
        unit = f"/{m[2]}" + (f" {m[3]}" if m[3] else "") if m.re is METER else "%"
        return out | {"shape": "meter", "big": m[1], "unit": unit, "fill": round(min(1.0, n / of), 3) if of > 0 else 0.0}
    if m := NUMBER.fullmatch(said):
        sign, unit = m[1], m[3] or ""
        if not sign and (label.strip().lower() == "lp" or unit.lower() == "lp"):
            return out | {"shape": "meter", "big": m[2], "unit": unit or "/100", "fill": round(min(1.0, float(m[2].replace(",", "")) / 100), 3)}
        return out | {"shape": "number", "big": sign + m[2], "unit": unit, "sign": "up" if sign == "+" else "down" if sign else ""}
    return out


def board(bar: dict[str, str]) -> list[dict[str, Any]]:
    """The bar's labels and values as the scoreboard's cells, in the room's order (cell)."""
    return [cell(k, v) for k, v in bar.items()]


def runs(text: str, looks: str = "") -> list[dict[str, str]]:
    """A row's words as runs of one look each (INLINE): `code` in code marks, a backslash's mark kept a mark, a link
    ([words](address), a bare address, or one in <>) only to http or https, and **bold**, __underline__, *italic* or
    _italic_, ~~strike~~ and ||a spoiler|| around words read in turn, so marks nest."""
    out: list[dict[str, str]] = []

    def add(text: str, looks: str, href: str = "") -> None:
        if text and out and out[-1]["looks"] == looks and out[-1].get("href", "") == href:
            out[-1]["text"] += text
        elif text:
            out.append({"text": text, "looks": looks, **({"href": href} if href else {})})
    at = 0
    for m in INLINE.finditer(text):
        add(text[at:m.start()], looks)
        at, kind = m.end(), m.lastgroup or ""
        if kind in ("code", "esc", "url"):
            add(m[kind], looks + "c" if kind == "code" else looks, m[kind] if kind == "url" else "")
        else:
            for r in runs(m["label"] if kind == "to" else m[kind], looks if kind == "to" else looks + LOOKS[kind]):
                add(r["text"], r["looks"], m["to"] if kind == "to" else r.get("href", ""))
    add(text[at:], looks)
    return out


def rich(text: str) -> list[dict[str, Any]]:
    """A line said, as the page's chat draws it, read the way Discord reads a message, since the owner reads the room
    there in place of Discord: a fenced block code in a box of its own (its language named after the fence), every other
    line a row and a blank line one gap between thoughts; a row's look from its start (ROW: #, ## or ### a heading, -#
    subtext, > a quote and >>> every row after it, a dash, a star or a number and a full stop a list item, its depth from
    its indent) and its words as runs (runs). Words only: the page puts each run in as text, never as markup."""
    out: list[dict[str, Any]] = []
    quoted, at, fences = False, 0, list(FENCE.finditer(text))
    for n, m in enumerate([*fences, None]):
        rows = text[at:m.start() if m else len(text)].split("\n")
        rows = rows[1:] if n and rows[0] == "" else rows                 # the line break after a fence
        rows = rows[:-1] if m and len(rows) > 1 and rows[-1] == "" else rows   # the one before it
        for row in rows:
            if not row.strip():
                if out and out[-1]["kind"] != "gap":
                    out.append({"kind": "gap"})
                continue
            r = ROW.match(row)
            head, quoted = r.group(0) if r else "", quoted or bool(r and r["all"])
            block: dict[str, Any] = ({"kind": "quote"} if quoted or head.startswith(">") else {"kind": "h" + str(len(r["h"]))} if r and r["h"]
                                     else {"kind": "sub"} if head.startswith("-#") else {"kind": "p"} if not (r and r["li"])
                                     else {"kind": "li", "mark": "•" if r["li"] in "-*•" else r["li"], "depth": min(len(r["in"].expandtabs(4)) // 2, 4)})
            out.append(block | {"runs": runs(row if block["kind"] == "p" or (quoted and not head.startswith(">")) else row[len(head):])})
        if m:
            out.append({"kind": "code", "lang": m[1] or "", "text": m[2].removesuffix("\n")})
            at = m.end()
    return out[:-1] if out and out[-1]["kind"] == "gap" else out


def comments(old: str, new: str) -> list[tuple[int, str]]:
    """The # comment lines the owner typed in the program since the room last heard it, the owner's talk as a said line is (a
    whole line a comment, new or changed, never a #! line, a #> line (the room's, talk) or one the program already had: so no diff is needed, whose time
    grew with the square of the lines when they repeat, and an unchanged program is not read): each run of them as its
    first line, from 1, and its words, a line each."""
    if old == new:
        return []
    had, out = {x.strip() for x in old.splitlines()}, list[tuple[int, list[str]]]()
    for j, row in enumerate(new.splitlines()):
        said = row.strip()
        if not owned(said) or said in had or not (talk := said.lstrip("#").strip()):
            continue
        if out and out[-1][0] + len(out[-1][1]) == j + 1:           # the next line of the same comment
            out[-1][1].append(talk)
        else:
            out.append((j + 1, [talk]))
    return [(at, "\n".join(said)) for at, said in out]


def owned(row: str) -> bool:
    """A whole line the owner's # comment: never a #! line, nor a #> line, the room's own (talk)."""
    said = row.strip()
    return said.startswith("#") and not said.startswith(("#!", "#>")) and bool(said.lstrip("#").strip())


def talk_lines(text: str, room: int = TEXT_MAX, indent: int = 0) -> list[str]:
    """A line the room said as the program's #> comment lines: a row each, a long one wrapped at TALK_WIDTH with its words
    whole (a link never cut) and a fenced block's rows kept whole; DIFF_LINES at most, and no more characters than `room`
    (what the program has left under TEXT_MAX) at `indent`, a line its newline too, the rest named as on the chat; none
    when not even that fits."""
    rows: list[str] = []
    fenced = False
    for row in text.replace("\r", "").strip("\n").split("\n"):
        fence = row.lstrip().startswith("```")
        rows += [row] if fenced or fence or len(row) <= TALK_WIDTH else textwrap.wrap(row, TALK_WIDTH, break_long_words=False, break_on_hyphens=False)
        fenced ^= fence
    lines = [f"#> {x}".rstrip() for x in rows]
    for n in range(min(len(lines), DIFF_LINES), 0, -1):          # all of it, else n - 1 lines and the count of the rest
        kept = lines if n == len(lines) else [*lines[:n - 1], f"#> {words.PAGE_TALK_CUT.format(n=len(lines) - n + 1)}"]
        if sum(indent + len(x) + 1 for x in kept) <= room:
            return kept
    return []


def spot(rows: list[str], asked: tuple[int, str]) -> tuple[int, str, tuple[int, str]]:
    """Where the room's lines go in the program's rows (talk): the row they go after, from 0 (-1: on top), their indent,
    and the owner's # line they go under, from 1 (0: none), and its words: the comment said last (`asked`, its line and
    words), the copy nearest its line, else the lowest # line of the owner's; under the #> lines already under it, at its
    indent; with none, after the program's last line."""
    hits = [i for i, r in enumerate(rows) if asked[1] and r.strip() == asked[1]]
    i = min(hits, key=lambda j: abs(j - asked[0] + 1)) if hits else next((j for j in range(len(rows) - 1, -1, -1) if owned(rows[j])), -1)
    anchor, indent = (i + 1, rows[i].strip()), rows[i][:len(rows[i]) - len(rows[i].lstrip())] if i >= 0 else ""
    if i < 0:                                      # no # line of the owner's: after the last line
        anchor, i = (0, ""), len(rows) - 2 if rows[-1] == "" else len(rows) - 1
    while i + 1 < len(rows) and rows[i + 1].lstrip().startswith("#>"):
        i += 1
    return i, indent, anchor


def into(code: str, asked: tuple[int, str], lines: list[str]) -> tuple[str, int, str]:
    """`lines` written into the program where the room's go (spot), and the owner's # line they went under, from 1 (0:
    none), and its words. A tab does the same in its own text with the line this found (page.html, talk), so what the owner
    typed since keeps its place, and the caret too."""
    rows = code.split("\n")
    i, indent, anchor = spot(rows, asked)
    rows[i + 1:i + 1] = [indent + x for x in lines]
    return "\n".join(rows), *anchor


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
    lines: list[dict[str, Any]] = field(default_factory=list)   # the chat's newest lines, the owner's and the room's
    said_n: int = 0                                           # the number the chat's next line gets
    run: dict[str, Any] | None = None
    run_heard: bool = True
    talk: bool = False                                        # the page's chat closed: the room's lines go in the program too
    asked: tuple[int, str] = (0, "")                          # the # line the owner said last: its line and its words
    waiting: list[str] = field(default_factory=list)          # lines for the next message: a stroke, a said line, an event
    tabs: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    quiet: asyncio.TimerHandle | None = None
    running: asyncio.Lock = field(default_factory=asyncio.Lock)

    def state(self) -> dict[str, Any]:
        """Everything a tab shows, for one that opens or reconnects."""
        return {"state": {"room": self.room.name, "code": self.code, "tests": self.tests, "marks": self.marks, "hint": self.hint,
                          "bar": self.bar, "board": board(self.bar), "timer": self.timer, "stall": self.stall,
                          "lines": [shown(x) for x in self.lines], "run": self.run, "talk": self.talk}}


def shown(line: dict[str, Any]) -> dict[str, Any]:
    """A chat line as a tab gets it: its words, and beside them their parts as Discord would show them (rich), read once and
    kept with the line (never in chat.json), so a tab that opens or reconnects reads none of the kept lines again."""
    if "parts" not in line:
        line["parts"] = rich(line["text"])
    return line


class Page:
    """Every room's page: the keys, the events in, the runs, and what the rooms draw."""

    def __init__(self, state: Path, ledger: Ledger, capture: Callable[..., Any] | None = None, quiet: float = QUIET):
        self.state, self.ledger, self.capture, self.quiet = state, ledger, capture, quiet
        self.sheets: dict[str, Sheet] = {}
        self.tickets: dict[str, tuple[str, float]] = {}     # one-time ticket -> (its room, when it runs out)
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
        with contextlib.suppress(OSError, ValueError, AttributeError):
            s.talk = json.loads((folder / TALK).read_text()).get("on") is True
        s.heard = (s.code, s.tests)
        with contextlib.suppress(OSError, ValueError, TypeError):
            s.lines = [{k: v for k, v in x.items() if k != "parts"} for x in json.loads((folder / CHAT).read_text())   # parts: rich's alone
                       if isinstance(x, dict) and isinstance(x.get("text"), str)][-LINES_KEPT:]
            s.said_n = max((x.get("n", 0) for x in s.lines if isinstance(x.get("n"), int)), default=0) + 1
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

    def drop_room(self, name: str) -> None:
        """The room's page off now: what the owner typed last goes to the room, its tabs' feeds end and its key opens
        nothing; the key file stays, so the address is the same when it comes back on."""
        if name in self.sheets:
            self.flush(name)
            self._push(self.sheets.pop(name), {"closing": True})

    def ticket(self, room: str, port: int, base: str = "") -> str:
        """A one-time address of the room's page, for the opener: a ticket good once within TICKET_SECONDS where the key
        would be, since the opener's command line is readable by every local user; a query, not a fragment, since every
        opener hands a query on whole. `base`: yerkd as another screen reaches it (mac), else the box's own address."""
        now = time.monotonic()
        self.tickets = {t: v for t, v in self.tickets.items() if v[1] > now}
        t = secrets.token_urlsafe(24)
        self.tickets[t] = (room, now + TICKET_SECONDS)
        return f"{base or f'http://127.0.0.1:{port}'}{PATH}?{urlencode({'room': room, 'open': t})}"

    def redeem(self, ticket: str) -> dict[str, Any]:
        """The page's trade of a one-time ticket for its room and key: once, within TICKET_SECONDS, while the page is on;
        a used, old or unknown ticket gets nothing."""
        found = None
        for t in list(self.tickets):
            if ticket and hmac.compare_digest(ticket, t):
                found = self.tickets.pop(t)
        if found is None or found[1] <= time.monotonic() or (s := self.sheets.get(found[0])) is None:
            log.warning("page: a one-time address was refused: used once already, past its minute, never made or its page off")
            return {"ok": False, "error": words.PAGE_TICKET_SPENT}
        return {"ok": True, "room": found[0], "key": s.key}

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
        timer's end, a stall, a # line ended with Enter, an edit with `send`, whose talk the room has not heard) or once the
        typing pauses (an edit, an Enter after a comment it heard too); the chat closed or opened (talk) rides in the next."""
        if (s := self.sheets.get(room)) is None:
            return {"ok": False, "error": words.PAGE_NONE.format(room=room)}
        self.loop = asyncio.get_running_loop()
        kind = str(ev.get("kind") or "")
        if kind == "edit":
            pane = "tests" if ev.get("pane") == "tests" else "code"
            new = str(ev.get("text") or "")[:TEXT_MAX]
            old = s.tests if pane == "tests" else s.code
            if new != old:
                s.tests, s.code = (new, s.code) if pane == "tests" else (s.tests, new)
                write_private(s.folder / (TESTS if pane == "tests" else PROGRAM), new)
                self._log(s, {"kind": "edit", "pane": pane, **change(old, new)})
            if ev.get("send") is True and comments(s.heard[0], s.code):   # a # line ended with Enter, talk the room has not heard: now
                self.flush(room)
            elif new != old:                           # else at the pause, as typing: an Enter after a comment the room heard too
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
            self._said(s, {"text": said, "kind": "me", "t": round(time.time(), 3)})
        elif kind == "timer":
            self._log(s, {"kind": "timer"})
            s.timer = {}
            s.waiting.append(words.PAGE_TIMER_DONE)
        elif kind == "stall":
            self._log(s, {"kind": "stall", "seconds": s.stall})
            s.waiting.append(words.PAGE_STALL.format(n=s.stall))
        elif kind == "talk":                           # the chat closed to nothing, or open again: said in the next message
            if (on := ev.get("on") is True) != s.talk:
                s.talk = on
                write_private(s.folder / TALK, json.dumps({"on": on}))
                self._log(s, {"kind": "talk", "on": on})
                s.waiting.append(words.PAGE_TALK_ON if on else words.PAGE_TALK_OFF)
            return {"ok": True}
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
        changed = s.run is None or [s.run.get(k) for k in ("passed", "ran", "error", "output")] != [r[k] for k in ("passed", "ran", "error", "output")]
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
        if (s := self.sheets.get(room)) is not None and not s.running.locked():   # a page turned off since: nothing
            self.flush(room)

    def message(self, s: Sheet, talk: list[tuple[int, str]] | None = None) -> str:
        """What waits to go to the room, as one message: the change to the program and its tests since it last heard them,
        each # comment line typed in the program as said (`talk`, comments, DIFF_LINES of them, the rest counted), the newest
        run when it says something new, and each stroke, said line and event in order; "" when nothing waits."""
        parts = []
        for (old, new, name) in ((s.heard[0], s.code, PROGRAM), (s.heard[1], s.tests, TESTS)):
            if old != new:
                parts.append(words.PAGE_TYPED.format(name=name, diff=diff(old, new, name)))
        parts += [words.PAGE_COMMENT.format(name=PROGRAM, line=at, text=said) for at, said in (talk or [])[:DIFF_LINES]]
        parts += [words.PAGE_COMMENT_CUT.format(n=len(talk) - DIFF_LINES, name=PROGRAM)] if talk and len(talk) > DIFF_LINES else []
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
        talk = comments(s.heard[0], s.code)
        if not (text := self.message(s, talk)):
            return ""
        s.heard, s.run_heard, s.waiting = (s.code, s.tests), True, []
        if talk:                                 # the # line said last, where the room's next lines go (into)
            at = talk[-1][0] + talk[-1][1].count("\n")
            s.asked = (at, s.code.split("\n")[at - 1].strip() if at <= s.code.count("\n") + 1 else "")
        for at, said in talk[:DIFF_LINES]:   # the owner's talk on the chat too: a comment still typed replaces its line
            last = s.lines[-1] if s.lines else {}
            again = last.get("at") == at and (said.startswith(last["text"]) or last["text"].startswith(said))
            self._said(s, {**(last if again else {}), "text": said, "kind": "me", "t": round(time.time(), 3), "at": at})
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
        padded = False
        if isinstance(req.get("bar"), dict):     # its words alone (plain), then cut; a cell left with none, a spacer, goes
            given = [(str(k), str(v)) for k, v in req["bar"].items()]
            kept = [(plain(k), plain(v)) for k, v in given]
            s.bar = op["bar"] = {k[:24]: v[:40] for k, v in [c for c in kept if any(c)][:8]}
            op["board"], padded = board(s.bar), kept != given
        if isinstance(req.get("timer"), int) and req["timer"] >= 0:
            s.timer = op["timer"] = {"seconds": req["timer"], "from": round(time.time(), 3)} if req["timer"] else {}
        if isinstance(req.get("stall"), int) and req["stall"] >= 0:
            s.stall = op["stall"] = req["stall"]
        if not op:
            return {"ok": False, "error": words.PAGE_NOTHING}
        self._log(s, {"kind": "drawn", **{k: v for k, v in op.items() if k not in ("code", "tests", "board")},
                      **{k: len(v) for k, v in op.items() if k in ("code", "tests")}})
        self._push(s, {"draw": op})
        notes = ([] if s.tabs else [words.PAGE_NO_TAB]) + ([words.PAGE_UNPADDED] if padded else [])
        return {"ok": True, "tabs": len(s.tabs), **({"note": "; ".join(notes)} if notes else {})}

    def said(self, room: str, text: str, kind: str = "reply") -> None:
        """A line the room said, from any thread, its first TEXT_MAX characters as the say box's: kept for a tab that opens
        later and shown on every open one; with the page's chat closed, written into the program too (_talk)."""
        if (s := self.sheets.get(room)) is None or not text.strip() or text.strip() == words.NO_REPLY:
            return
        line = {"text": text[:TEXT_MAX], "kind": kind, "t": round(time.time(), 3)}
        for step in (self._said, self._talk):
            if self.loop is not None and self.loop.is_running():
                self.loop.call_soon_threadsafe(step, s, line)
            else:
                step(s, line)

    def _talk(self, s: Sheet, line: dict[str, Any]) -> None:
        """A line the room said, as #> lines in the program under the owner's last # line (into) while the page's chat is
        closed: in yerkd's copy and the room's last heard one alike, so the room is never told it typed them, and to every
        open tab as that line and the words, which it writes in its own text there, the owner's caret and typing kept. No
        more than the program holds under TEXT_MAX, the rest named as on the chat and the room told once in its next
        message (past it, the next keystroke posted cut the owner's code); the room's marks under them move down with
        their lines (the tab moves its own)."""
        if not s.talk:
            return
        rows = s.code.split("\n")
        i, indent, (at, anchor) = spot(rows, s.asked)
        lines = talk_lines(line["text"], TEXT_MAX - len(s.code), len(indent))
        if lines != talk_lines(line["text"], indent=len(indent)) and (full := words.PAGE_TALK_FULL.format(n=TEXT_MAX)) not in s.waiting:
            s.waiting.append(full)
        if not lines:                                  # the program full: the chat alone has it
            return
        rows[i + 1:i + 1] = [indent + x for x in lines]
        s.code, s.heard = "\n".join(rows), (into(s.heard[0], s.asked, lines)[0], s.heard[1])
        s.marks = [{**m, "line": m["line"] + len(lines)} if m["line"] > i + 1 else m for m in s.marks]
        write_private(s.folder / PROGRAM, s.code)
        self._log(s, {"kind": "talked", "under": at, "lines": len(lines)})
        self._push(s, {"talk": {"line": at, "text": anchor, "lines": lines}})

    def _said(self, s: Sheet, line: dict[str, Any]) -> None:
        """A chat line kept, numbered, written to the room's page folder and shown on every open tab; one whose number the
        chat already has replaces that line (a comment still being typed)."""
        if "n" not in line:
            line, s.said_n = {**line, "n": s.said_n}, s.said_n + 1
        line = {**line, "parts": rich(line["text"])}           # read here once, for every tab now and each one that opens later
        s.lines = [*[x for x in s.lines if x.get("n") != line["n"]], line][-LINES_KEPT:]
        try:
            write_private(s.folder / CHAT, json.dumps([{k: v for k, v in x.items() if k != "parts"} for x in s.lines], ensure_ascii=False))
        except OSError as e:
            log.warning("page %s: the chat not written: %s", s.room.name, e)
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


def opener(conf: dict[str, Any]) -> list[str]:
    """The command that opens an address on the owner's screen, the address added last: [page] open in yerkd.toml (a list:
    the command and its arguments), else the box's own: wslview or Windows' url.dll under WSL, open on a Mac, xdg-open on a
    Linux desktop with a display; [] on a box with no screen to open it on."""
    own = dict(conf.get("page") or {}).get("open")
    if isinstance(own, list) and own and all(isinstance(x, str) and x for x in own):
        return list(own)
    if sys.platform == "darwin":
        return ["open"]
    if any(os.path.exists(f"/proc/sys/fs/binfmt_misc/{n}") for n in ("WSLInterop", "WSLInterop-late")):
        return [w] if (w := shutil.which("wslview")) else list(WSL_OPEN) if os.path.exists(WSL_OPEN[0]) else []
    x = shutil.which("xdg-open")
    return [x] if x and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) else []


def mac(conf: dict[str, Any], port: int) -> tuple[list[str], str] | str:
    """The way to the Mac for an open there (where mac): the ssh command whose stdin is the Mac's script (MAC_SSH to
    [page] mac, else [imessage] host, the text tool's way to the same Mac) and yerkd's address as the Mac reaches it
    (reach), since 127.0.0.1 there is the Mac; else why not, in words."""
    pg = dict(conf.get("page") or {})
    if not (host := str(pg.get("mac") or dict(conf.get("imessage") or {}).get("host") or "").strip()):
        return words.PAGE_NO_MAC
    try:
        base = reach(conf, port)
    except ValueError as e:
        return str(e)
    return ([*MAC_SSH, host, "/bin/bash", "-s"], base) if base else words.PAGE_NO_ADDRESS


def listen(conf: dict[str, Any]) -> str:
    """[page] listen: the box's tailnet or LAN address yerkd also serves the page's routes on, beside loopback; "" when it
    names none or loopback (the default: the page on loopback alone, as /mcp and /hook always are). ValueError in words for
    one that is not such an address: a wildcard (every network the box is on), a public one (the page on the internet in
    plain text) or a host name (it could resolve elsewhere; `tailscale ip -4` prints the address)."""
    said = str(dict(conf.get("page") or {}).get("listen") or "").strip()
    try:
        ip = ipaddress.ip_address(said.strip("[]") if said not in ("", "localhost") else "127.0.0.1")
    except ValueError:
        raise ValueError(words.PAGE_LISTEN_NAME.format(x=said[:60])) from None
    if ip.is_unspecified or ip.is_global or ip.is_multicast:
        raise ValueError((words.PAGE_LISTEN_EVERY if ip.is_unspecified else words.PAGE_LISTEN_PUBLIC).format(x=said))
    return "" if ip.is_loopback else str(ip)


def reach(conf: dict[str, Any], port: int) -> str:
    """yerkd as another screen reaches it (a Mac, a phone), the base of the page's address there: [page] address,
    the box's tailnet or LAN host (yerkd's port when it names none), else [page] listen's address on yerkd's port; "" when
    neither names one. ValueError in words for one that is not an address."""
    if not (said := str(dict(conf.get("page") or {}).get("address") or "").strip()):
        return f"http://[{ip}]:{port}" if ":" in (ip := listen(conf)) else f"http://{ip}:{port}" if ip else ""
    try:
        u = urlsplit(said if "://" in said else f"http://{said}")
        at = u.port or port
    except ValueError:                        # a port that is not one, or a bracket left open
        raise ValueError(words.PAGE_NO_ADDRESS) from None
    if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/") or u.query:
        raise ValueError(words.PAGE_NO_ADDRESS)
    return f"{u.scheme}://{f'[{u.hostname}]' if ':' in u.hostname else u.hostname}:{at}"


def bind(host: str, port: int, free: bool = False) -> tuple[socket.socket, bool]:
    """A socket of yerkd's HTTP server on host:port (SO_REUSEADDR, as uvicorn binds its own), for its serve to listen on, and
    whether the box has that address now: one it lacks yet (its tailnet still coming up at boot) is bound all the same,
    `free` (FREEBIND), so the page is served there the moment the address comes up, with no restart. OSError if it will not."""
    ip = ipaddress.ip_address(host)
    s = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if free:
            s.setsockopt(socket.SOL_IP, FREEBIND, 1)
        s.bind((str(ip), port))
    except OSError as e:
        s.close()
        if free or e.errno != errno.EADDRNOTAVAIL or not FREEBIND:
            raise
        return bind(host, port, True)
    return s, not free


def sockets(conf: dict[str, Any], port: int) -> list[socket.socket]:
    """yerkd's own sockets when [page] listen names the box's tailnet or LAN address (listen): loopback's, which serves all
    of yerkd, then that address's on the same port, which serves the page's routes alone (mcp_server.Guard's `beside`), so
    the Mac and phone reach the page and nothing else; [] (uvicorn binds loopback alone, as ever) when it names
    none, or one that is not such an address or will not bind, said in the log, the page then on loopback alone."""
    try:
        if not (host := listen(conf)):
            return []
        far, up = bind(host, port)
    except (ValueError, OSError) as e:
        log.warning("page: on loopback alone: %s", f"{host} will not take port {port}: {e.strerror or e}" if isinstance(e, OSError) else e)
        return []
    try:
        near, _ = bind("127.0.0.1", port)
    except OSError as e:
        far.close()
        raise SystemExit(f"yerkd: port {port} on 127.0.0.1 will not bind: {e.strerror or e}") from None
    log.info("page: on %s port %d too, its routes alone, beside loopback", host, port)
    if not up:
        log.warning("page: the box has no address %s yet (its tailnet still coming up, or not this box's); the page is there once it has", host)
    if not any(ipaddress.ip_address(host) in n for n in TAILNET):
        log.warning("page: %s is not a tailnet address, so the page and its key cross that network unencrypted", host)
    return [near, far]


def open_on_screen(cmd: list[str], url: str, seconds: float = OPEN_SECONDS, script: str = "") -> str:
    """`url` opened with the opener `cmd` (opener), the address added last, or, with `script` (MAC_OPEN), in that script on
    cmd's stdin (the Mac's ssh, mac), on no command line: "" when it opened, else why not, in words."""
    if not cmd:
        return words.PAGE_NO_SCREEN
    how: dict[str, Any] = {"input": script.format(url=shlex.quote(url))} if script else {"stdin": subprocess.DEVNULL}
    try:
        p = subprocess.run(cmd if script else [*cmd, url], capture_output=True, text=True, timeout=seconds, **how)
    except subprocess.TimeoutExpired:
        return words.PAGE_OPEN_SLOW.format(n=round(seconds))
    except OSError as e:
        return words.PAGE_OPEN_FAILED.format(why=e.strerror or type(e).__name__)
    said = (p.stderr or p.stdout).strip().replace(url, "the address")[-200:]
    if p.returncode == 255 and script:        # ssh's own: the Mac asleep, off the network or refusing the key
        return words.PAGE_MAC_SSH.format(why=said.splitlines()[-1] if said else words.PAGE_OPEN_SILENT)
    return "" if p.returncode == 0 else words.PAGE_OPEN_FAILED.format(why=said or words.PAGE_OPEN_SILENT)


def address(state: Path, room: str, here: bool = False) -> str:
    """The address a room's page opens at, the room and its key in the fragment: yerkd's port from its yerkd.json, on the
    host other screens reach it at when yerkd names one there (its `page`, reach), else, or `here`, the box's 127.0.0.1."""
    try:
        port = int((st := json.loads((state / "yerkd.json").read_text()))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(words.PAGE_NO_DAEMON) from None
    if not (key := read_secret(state / "secrets" / "pages" / f"{room}.secret")):
        raise SystemExit(words.PAGE_NO_KEY.format(room=room))
    base = "" if here else str(st.get("page") or "")
    return f"{base or f'http://127.0.0.1:{port}'}{PATH}#room={room}&key={key}"
