"""The watchdog beat and the stall watch (DESIGN 9, L201, L238). A loop task sets a mark every
second; a thread beside the loop sends systemd WATCHDOG=1 only when the mark moved since its last
beat, so a frozen loop stops the beat and systemd restarts the unit (units/yerkd.service; with no
WatchdogSec there is nothing to beat and nothing is sent). The same thread sees a stall while it
lasts: once the mark is more than LIMIT seconds late it writes a dump (the loop thread's stack, the
asyncio tasks, /proc for the daemon's threads and each open room's processes) under the state
folder, never under the memory tree, and when the loop runs again it writes the stall row: when it
began, how late the loop ran, what it was running. A stall inside C code that holds the GIL keeps
this thread out too: it is ledgered when it ends, with no dump."""
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
LIMIT = 2.0             # seconds late before the loop counts as stalled (Layer 7: "a stall over 2 s")
EVERY = 1.0             # the mark's cadence


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
        self.mark, self.loop_thread = time.monotonic(), 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop = threading.Event()

    async def tick(self) -> None:
        """The loop's side: READY=1 once the loop runs, then a mark every `every` seconds."""
        self.loop, self.loop_thread, self.mark = asyncio.get_running_loop(), threading.get_ident(), time.monotonic()
        self.stop.clear()
        threading.Thread(target=self.watch, name="yerk-beat", daemon=True).start()
        self.send("READY=1")
        try:
            while True:
                await asyncio.sleep(self.every)
                self.mark = time.monotonic()
        finally:
            self.stop.set()

    def watch(self) -> None:
        """The thread's side, four times a mark: beat when the mark moved; mid-stall, dump; after, the row."""
        prev, began, doing, dump = self.mark, 0.0, "", ""
        while not self.stop.wait(self.every / 4):
            try:
                mark, late = self.mark, time.monotonic() - self.mark - self.every
                if mark == prev:
                    if not began and late > self.limit:          # still stalled: the dump, while it lasts
                        began = time.time() - late
                        doing, dump = self.dump(began)
                    continue
                late = mark - prev - self.every
                if not began and late > self.limit:              # ended before this thread could run
                    began, doing = time.time() - (time.monotonic() - prev - self.every), "not seen while it ran"
                if began:
                    self.ledger.stall(began=round(began, 3), seconds=round(late, 2), doing=doing, dump=dump)
                    log.warning("stall: the event loop ran %.1f s late, in %s", late, doing)
                prev, began, doing, dump = mark, 0.0, "", ""
                if self.watchdog:
                    self.send("WATCHDOG=1")
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
        rows = [("yerkd", r) for t in sorted(tasks.iterdir()) if (r := stat(int(t.name), self.proc, tasks))] \
            if tasks.is_dir() else []
        rows += [(room, r) for room, pid in self.pids().items() for r in tree(pid, self.proc)]
        lines += ["processes (who, pid, name, state, wchan, resident, cpu):"]
        lines += [f"  {who} {r['pid']} {r['name']} {r['state']} {r['wchan']} {r['rss_mb']} MB {r['cpu_s']:.1f} s"
                  for who, r in rows]
        self.dumps.mkdir(parents=True, exist_ok=True)
        path = self.dumps / time.strftime("%Y%m%d-%H%M%S.txt", time.localtime(began))
        path.write_text("\n".join(lines) + "\n")
        return doing, str(path)
