"""Tells systemd the daemon is alive only while its event loop answers, so a frozen daemon is restarted, and records each time
probe on the loop every tenth of the beat's cadence; the time a probe waits is how long the loop was
puts a probe on the loop every tenth of the beat's cadence; the time a probe waits is how long the loop was held. Only an
answered probe beats systemd's watchdog (WATCHDOG=1, at most once a cadence, and only under WatchdogSec), so a frozen loop
stops the beat and systemd restarts the unit. A probe waiting past LIMIT gets the dump while the stall lasts (the loop thread's
stack, the asyncio tasks, /proc for the daemon's threads and each room's processes, never under the memory tree); once answered,
past STALL is a log line and one past LIMIT a stall row: when it began, how long it held, what ran.
READY=1 goes out once the daemon says it can answer (its HTTP port listens)."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from .ledger import Ledger
from .lifted.systemd_notify import notify, watchdog_interval_seconds
from .status import PROC, stat, tree

log = logging.getLogger("yerk.beat")
LIMIT = 2.0             # seconds held before the loop counts as stalled (Layer 7: "a stall over 2 s")
STALL = 0.25            # seconds held before the log says so
EVERY = 1.0             # the watchdog's cadence; a probe goes out every tenth of it


def folder(state: Path, memory_root: Path) -> Path:
    """<state>/stalls, unless the state folder sits inside the memory tree, where every dump would be
    an index write (the dumps that caused the next stall, L201): then the temp folder."""
    f = state / "stalls"
    inside = f.resolve().is_relative_to(memory_root.resolve())
    return Path(tempfile.gettempdir()) / f"yerk-stalls-{os.getuid()}" if inside else f


class Beat:
    def __init__(self, ledger: Ledger, dumps: Path, pids: Callable[[], dict[str, int]] = dict,
                 send: Callable[[str], bool] = notify, limit: float = LIMIT, every: float = EVERY, proc: Path = PROC):
        self.ledger, self.dumps, self.pids, self.send, self.proc = ledger, dumps, pids, send, proc
        self.limit, self.every = limit, every
        self.watchdog = watchdog_interval_seconds() is not None      # WatchdogSec is set on the unit
        self.asked, self.held, self.loop_thread = 0.0, 0.0, 0        # the probe out (when asked), the longest wait
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop = threading.Event()

    async def tick(self, ready: Callable[[], bool] = lambda: True) -> None:
        """The loop's side: start the watch, then READY=1 once `ready()` holds, then wait to be cancelled."""
        self.loop, self.loop_thread = asyncio.get_running_loop(), threading.get_ident()
        self.stop.clear()
        self.asked = time.monotonic()
        self.loop.call_soon(self.answer)
        threading.Thread(target=self.watch, name="yerk-beat", daemon=True).start()
        try:
            while not ready():
                await asyncio.sleep(0.05)
            self.send("READY=1")
            await asyncio.Event().wait()
        finally:
            self.stop.set()

    def answer(self) -> None:
        """On the loop: the probe ran, and the time it waited is how long the loop was held."""
        self.held, self.asked = max(self.held, time.monotonic() - self.asked), 0.0

    def watch(self) -> None:
        """The thread's side: while a probe waits, nothing beats and past `limit` the dump is taken; once
        it is answered, the hold is said and ledgered, the watchdog beats, and the next probe goes out."""
        began, doing, dump, beat = 0.0, "", "", 0.0
        while not self.stop.wait(self.every / 10):
            try:
                now = time.monotonic()
                if self.asked:                                   # a probe waits: the loop is held
                    if not began and now - self.asked > self.limit:
                        began = time.time() - (now - self.asked)
                        doing, dump = self.dump(began)
                    continue
                held, self.held = self.held, 0.0
                if held > self.limit and not began:              # ended before this thread could run
                    began, doing = time.time() - held, "not seen while it ran"
                if held > STALL:
                    log.warning("stall: the event loop was blocked for %.0f ms%s", held * 1000, doing and f", in {doing}")
                if held > self.limit:
                    self.ledger.stall(began=round(began, 3), seconds=round(held, 2), doing=doing, dump=dump)
                began, doing, dump = 0.0, "", ""
                if self.watchdog and now - beat >= self.every:
                    beat = now
                    self.send("WATCHDOG=1")
                if self.loop is None or self.loop.is_closed():
                    return
                self.asked = time.monotonic()
                self.loop.call_soon_threadsafe(self.answer)
            except Exception:
                log.exception("beat: a round of the watch failed")

    def dump(self, began: float) -> tuple[str, str]:
        """What the loop thread runs, the asyncio tasks, and /proc for the daemon's threads and each
        room's processes, to <dumps>/<time>.txt. Returns (what the loop runs, the file)."""
        frame = sys._current_frames().get(self.loop_thread)
        stack = traceback.extract_stack(frame) if frame else traceback.StackSummary()
        ours = [f for f in stack if f"{os.sep}yerk_ai{os.sep}" in f.filename] or list(stack)
        doing = f"{Path(ours[-1].filename).name}:{ours[-1].lineno} in {ours[-1].name}" if ours else "unknown"
        lines = [f"stalled since {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(began))}, still running",
                 f"the loop thread is in {doing}", "", *traceback.format_list(stack[-15:]), "asyncio tasks:"]
        try:
            for t in asyncio.all_tasks(self.loop) if self.loop else set():
                f = t.get_stack(limit=1)
                at = f"{Path(f[0].f_code.co_filename).name}:{f[0].f_lineno}" if f else "not started"
                lines.append(f"  {t.get_name()}: {getattr(t.get_coro(), '__qualname__', '?')} at {at}")
        except RuntimeError as e:                        # the task set changed under the read
            lines.append(f"  unreadable: {e}")
        tasks = self.proc / str(os.getpid()) / "task"
        rows = [("yerkd", r) for t in sorted(tasks.iterdir()) if (r := stat(int(t.name), self.proc, tasks))] if tasks.is_dir() else []
        rows += [(room, r) for room, pid in self.pids().items() for r in tree(pid, self.proc)]
        lines += ["processes (who, pid, name, state, wchan, resident, cpu):"]
        lines += [f"  {who} {r['pid']} {r['name']} {r['state']} {r['wchan']} {r['rss_mb']} MB {r['cpu_s']:.1f} s" for who, r in rows]
        self.dumps.mkdir(parents=True, exist_ok=True)
        path = self.dumps / time.strftime("%Y%m%d-%H%M%S.txt", time.localtime(began))
        path.write_text("\n".join(lines) + "\n")
        return doing, str(path)
