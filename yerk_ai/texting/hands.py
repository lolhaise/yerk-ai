"""The hands: the one step that reaches the Mac, handing it what may be sent. `job` makes the Mac runner's job for one part of a texting row; the hands
the mode chose at the start hand it on, once. Dry writes it as a file under <state>/texting/dry/ and has no way to build
or run a command. Live writes it on ssh's stdin into `[texting] outbox` with an atomic move (DESIGN Q6), only to a
handle on the reply list as it is on disk at that moment (J16). What went is seen only as a from-me row on the link."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from .. import texts
from .people import load

PUT, REFUSED, DOWN, MAYBE = "put", "refused", "down", "may have gone"   # what a hand-off came to
# what ssh says when no command can have run: no connection, no login, or hung up before its greeting (a Mac at its ssh
# session cap, seen 2026-11-13); a close that says none of these may have written the job, so it is never tried again
NO_LINK = re.compile(r"ssh: connect to host|Could not resolve hostname|Permission denied|Host key verification failed|"
                     r"kex_exchange_identification|banner exchange")


def parts(row: dict[str, Any]) -> int:
    return 1 if row.get("decision") == "react" else len(json.loads(row.get("parts") or "[]"))


def job(row: dict[str, Any], part: int, newest: float) -> dict[str, Any]:
    """Part `part` (from 1) of a decided row as the runner takes it: the row's handle, delay 0 (Yerk holds the clock),
    `queued` and `since` at `newest` (the newest answered text's own time) plus 1 s, the text or the tapback, `burst`
    on parts 2 and 3 (a plain job ends the handle's running jobs), `reply_to` on part 1 when the writer gave one."""
    out: dict[str, Any] = {"to": row["handle"], "delay": 0, "queued": newest + 1, "since": newest + 1}
    if row.get("decision") == "react":
        return {**out, "react": row["react"]}
    out["text"] = json.loads(row["parts"])[part - 1]
    if part > 1:
        out["burst"] = True
    elif row.get("reply_to"):
        out["reply_to"] = row["reply_to"]
    return out


def listed(file: Path, to: str) -> bool:
    try:
        return load(file, False).by_handle(to) is not None
    except (OSError, ValueError):
        return False


class Dry:
    """Dry's hands: the job as <state>/texting/dry/<name>.json (0600). Nothing here builds or runs a command."""

    def __init__(self, file: Path, state: Path):
        self.file, self.folder = file, state / "texting" / "dry"

    async def hand(self, name: str, job: dict[str, Any]) -> str:
        if not listed(self.file, str(job.get("to") or "")):
            return REFUSED
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(os.open(self.folder / f"{name}.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(json.dumps(job))
        return PUT


class Live:
    """Live's hands: one job on ssh's stdin, moved into the outbox whole. `argv` is ssh to the link's own host, built by
    the daemon (texts.transport's head) and never read from a key. The Mac's `queued:<name>` line is the only PUT; an
    ssh that could not connect wrote nothing (DOWN, tried again); anything else may have gone (MAYBE, never again)."""

    def __init__(self, file: Path, outbox: str, argv: list[str], timeout: float = 30.0):
        self.file, self.outbox, self.argv, self.timeout = file, outbox, list(argv), timeout

    async def hand(self, name: str, job: dict[str, Any]) -> str:
        if not texts.WHOLE.fullmatch(self.outbox) or not self.argv or not listed(self.file, str(job.get("to") or "")):
            return REFUSED
        box = self.outbox.rstrip("/")
        remote = f"umask 077; mkdir -p {box}; cat > {box}/{name}.tmp && mv {box}/{name}.tmp {box}/{name}.json && echo queued:{name}"
        try:
            proc = await asyncio.create_subprocess_exec(*self.argv, remote, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError:
            return DOWN
        try:
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(job).encode()), self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return MAYBE
        if f"queued:{name}" in out.decode(errors="replace").split():
            return PUT
        return DOWN if proc.returncode == 255 and NO_LINK.search(err.decode(errors="replace")) else MAYBE
