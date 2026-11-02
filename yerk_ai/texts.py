"""Sends one text for the text tool, an iMessage or else an SMS, in your name from your Mac, to one person by their phone
number or email address. Plain ssh holds no grant to drive Messages, so the Mac's bridge does it (a small app
there that holds Messages' grants and serves imsg's JSON-RPC on a socket), reached as the iMessage rooms reached it
(docs/STATUS.md, Layer 8a): ssh to [imessage] `host`, then socat to its `socket` there, or `command` whole. Each text is a
connection of its own: a `status` first, whose answer (any answer, an old bridge's unknown-method error too) says the
bridge is there, then one `send` of `to`, `text` and `service`, then the connection closes. So a text that did not go is
told apart from one that may have: no answer to the status, or an error to the send, is not sent; no answer to the send,
or the bridge's own "may have completed" (-32001), may have gone, and nothing here ever sends it again. The gate shows
the owner the text first (gates.py, the text-out rule), and the daemon sends only a call that gate released.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shlex
from typing import Any

from . import words

TOOL = "mcp__yerk__text"
HELLO = 20.0                 # seconds the status waits: the ssh connect and the bridge's first answer
SEND = 60.0                  # seconds a send waits on Messages itself ([imessage] send_timeout)
LINE = 1024 * 1024           # the longest line read from the bridge
MAYBE = -32001               # imsg: the operation may have completed or remains in flight
PHONE, EMAIL = re.compile(r"\+?[\d\s().-]+"), re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def transport(conf: dict[str, Any]) -> list[str]:
    """The command whose stdin and stdout are the bridge: `command` as given, else ssh to `host` and socat to `socket`
    there; [] when neither is set, so no text can go."""
    if c := conf.get("command"):
        return shlex.split(c) if isinstance(c, str) else [str(x) for x in c]
    if not conf.get("host") or not conf.get("socket"):
        return []
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", str(conf["host"]),
            str(conf.get("socat", "/opt/homebrew/bin/socat")), "-", f"UNIX-CONNECT:{conf['socket']}"]


def handle(to: str) -> str:
    """A person as Messages takes them: a phone number by its digits and a leading +, an email address as given; "" for
    anything else (a name, a group), which the room must look up first."""
    t = to.strip()
    if PHONE.fullmatch(t) and 5 <= len(digits := re.sub(r"\D", "", t)) <= 15:
        return ("+" if t.startswith("+") else "") + digits
    return t if EMAIL.fullmatch(t) else ""


def preview(ti: dict[str, Any]) -> str:
    """A held text as the owner reads it before the word: its first line who it goes to exactly as the send has it (handle,
    one line), never as the room wrote it or as no_paths would cut a path-shaped address to its name; then its words."""
    return f"to: {handle(str(ti.get('to') or ''))}\n{ti.get('text') or ''}"


async def ask(proc: asyncio.subprocess.Process, rid: int, method: str, params: dict[str, Any], wait: float) -> dict[str, Any] | None:
    """One request on the line and its answer, a result or an error; None when none came in `wait` or the line closed."""
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
        await proc.stdin.drain()
        async with asyncio.timeout(wait):
            while line := await proc.stdout.readline():
                with contextlib.suppress(ValueError):
                    msg = json.loads(line)
                    if isinstance(msg, dict) and msg.get("id") == rid and ("result" in msg or "error" in msg):
                        return msg
    except (OSError, TimeoutError, ValueError):        # a closed pipe, no answer in time, a line past LINE
        return None
    return None


async def send(conf: dict[str, Any], to: str, text: str) -> dict[str, Any]:
    """One text to `to`: {"ok": True, "to", "guid"} once the bridge answered the send, else {"ok": False, "error",
    "maybe_sent"}, true when it may have gone out."""
    def no(why: str) -> dict[str, Any]:
        return {"ok": False, "maybe_sent": False, "error": words.TEXT_NOT_SENT.format(why=why)}
    if not (argv := transport(conf)):
        return no(words.TEXT_NO_SETUP)
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, limit=LINE)
    except OSError as e:
        return no(e.strerror or type(e).__name__)
    params = {"to": handle(to), "text": text, "service": str(conf.get("service", "auto")),
              **({"transport": str(conf["send_transport"])} if conf.get("send_transport") else {})}
    try:
        hello = await ask(proc, 1, "status", {}, HELLO)
        answer = await ask(proc, 2, "send", params, float(conf.get("send_timeout", SEND))) if hello else None
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if proc.stdin is not None:
                proc.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
    if hello is None:                                   # the send was never written
        return no(words.TEXT_AWAY)
    err = answer.get("error") if answer else None
    code = err.get("code") if isinstance(err, dict) else None
    if answer is None or code == MAYBE:
        return {"ok": False, "maybe_sent": True, "error": words.TEXT_MAYBE}
    if err is not None:
        return no(words.TEXT_WHY.get(code, words.TEXT_NO_WHY) if isinstance(code, int) else words.TEXT_NO_WHY)
    got = answer.get("result")
    return {"ok": True, "to": params["to"], "guid": str(got.get("guid") or "") if isinstance(got, dict) else ""}
