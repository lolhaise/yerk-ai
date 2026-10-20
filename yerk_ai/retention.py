"""`yerk retention`: the daily sweep that keeps the state folder from growing forever, run by units/yerk-retention.timer.
Each kind keeps a number of days, [retention] in the private yerkd.toml over DAYS: a room's stream log (logs/<room>.jsonl)
a room's stream log (logs/<room>.jsonl) and assembled log (rooms/<room>/assembled.jsonl) are gzipped whole each
day into <name>.<day of their last write>.jsonl.gz beside them, a day's file kept `logs` or `assembled` days; a
Yerk room's Claude Code transcripts are kept `transcripts` days after the session's last write, and never one
whose session sessions.json binds, one written in the last 24 hours, or any of a room with a turn in flight or
waiting; rows of the timing tables (TIMED: no reader looks back past a day) are kept `ledger` days and the WAL
is checkpointed; a finished worker's folder and its project folder are kept `workers` days; a room's scratch
(scratch(): its work folder but the person's files, and the copies of the files it sent) is kept `scratch` days,
and past `scratch_mb` a room loses its oldest files first, never one written in the last hour and never while
it has a turn in flight. What leaves moves to <state>/trash/YYYY-MM-DD/ first (a cut log's original, a table's
rows as gzipped JSON lines), and only the trash past `trash` days is deleted, so a wrong rule costs a week, not
data. Never touched: the memory tree, the private layer (yerkd.toml and accounts.toml are read), attachments in
work/inbox (their size is said), anything a process holds open or runs in (from /proc), anything else outside
the state folder."""
from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any

from . import status, words
from .accounts import load_accounts
from .config import load_toml, write_private
from .engine import project_dir
from .ledger import Ledger
from .workers import settings as worker_settings

DAY = 86400.0
DAYS = {"logs": 14, "assembled": 14, "transcripts": 30, "ledger": 90, "workers": 30, "scratch": 2, "trash": 7}
CAP = {"scratch_mb": 500}             # past this a room's scratch loses its oldest files first
TIMED = {"outbound": "sent", "hook_calls": "t", "tool_times": "t", "evidence": "t", "shadow": "t"}
SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DATED = re.compile(r"\.(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")
PROC, LAST = Path("/proc"), "retention.json"


def day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def keep(conf: dict[str, Any]) -> dict[str, float]:
    """[retention] over DAYS and CAP: a value that is not a positive number is the default."""
    got = dict(conf.get("retention", {}))
    return {k: float(v) if isinstance(v := got.get(k, d), (int, float)) and not isinstance(v, bool) and v > 0 else d
            for k, d in (DAYS | CAP).items()}


def held(proc: Path = PROC) -> set[tuple[int, int]]:
    """(device, inode) of every file a process holds open and every folder one runs in; no /proc, no guess."""
    if not (proc / "self" / "fd").is_dir():
        raise OSError(2, "no /proc here to say which files are open")
    found = set()
    for p in [*proc.glob("[0-9]*/fd/*"), *proc.glob("[0-9]*/cwd")]:
        with contextlib.suppress(OSError):
            found.add(((st := p.stat()).st_dev, st.st_ino))
    return found


def tree(path: Path) -> list[os.stat_result]:
    """`path` and everything under it, links never followed."""
    if path.is_symlink() or not path.is_dir():
        return [path.lstat()]
    return [path.lstat(), *(Path(d, f).lstat() for d, ds, fs in os.walk(path) for f in [*ds, *fs])]


def size(found: list[os.stat_result]) -> int:
    return sum(s.st_size for s in found if stat.S_ISREG(s.st_mode))


def scratch(room: Path) -> list[tuple[Path, os.stat_result]]:
    """A room's scratch, oldest first: every file under work/ but inbox/ (the person's files) and the dot entries at
    its top (Claude Code's own and its sandbox's), and under outbox/ (a copy of each file the room sent, read only to
    send it again at the start after a crash). A link is a file of its own, never followed."""
    tops = [p for p in (room / "work").glob("*") if p.name != "inbox" and not p.name.startswith(".")] + [room / "outbox"]
    found = [Path(d, f) for t in tops if t.is_dir() and not t.is_symlink() for d, _, fs in os.walk(t) for f in fs]
    return sorted(((p, p.lstat()) for p in found + [t for t in tops if t.is_symlink() or t.is_file()]), key=lambda x: x[1].st_mtime)


def in_flight(state: Path, ledger: Ledger, rooms: list[str]) -> set[str]:
    """Rooms with a turn running or waiting: yerkd's /status (every room when it runs and does not answer), and the
    ledger's open rows."""
    live = status.ask(state) if status.alive(state) else {"rooms": []}
    busy = {str(r["room"]) for r in live["rooms"] if r.get("busy") or r.get("waiting")} if live else set(rooms)
    return busy | {str(r["room"]) for r in ledger.inbound_open()}


def run(private: Path, state: Path, dry: bool = False, now: float | None = None, proc: Path = PROC) -> dict[str, Any]:
    """One sweep: {"at", "dry", "kinds": {kind: [items, bytes]}, "inbox": bytes, "error", "why"}; with `dry` nothing
    moves. A kind's bytes are what moved to the trash; the trash's, what was deleted from it."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {"at": now, "dry": dry, "kinds": {}, "inbox": 0}
    trash, busy = state / "trash" / day(now), set[tuple[int, int]]()
    rooms = sorted(p for p in (state / "rooms").glob("*") if p.is_dir())

    def tally(kind: str, n: int, nbytes: int) -> None:
        row = out["kinds"].setdefault(kind, [0, 0])
        row[0], row[1] = row[0] + n, row[1] + nbytes

    def free(found: list[os.stat_result]) -> bool:
        return not any((s.st_dev, s.st_ino) in busy for s in found)

    def move(kind: str, path: Path, rel: str) -> Path | None:
        """`path` into today's trash as `rel` (the original itself when `dry`); None when a process holds any of it."""
        if not free(found := tree(path)):
            return None
        tally(kind, 1, size(found))
        if dry:
            return path
        (dest := trash / rel).parent.mkdir(parents=True, exist_ok=True)
        return Path(shutil.move(path, dest.with_name(f"{dest.name}.{time.time_ns()}") if dest.exists() else dest))
    try:
        conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
        days, ledger, busy = keep(conf), Ledger(state / "ledger.sqlite"), held(proc)
        flight = in_flight(state, ledger, [r.name for r in rooms])
        if not dry:                                       # the trash holds transcripts and ledger rows: the owner's alone
            (state / "trash").mkdir(mode=0o700, exist_ok=True)
            (state / "trash").chmod(0o700)
        logs = [("logs", p, p.name.removesuffix(".jsonl")) for p in sorted((state / "logs").glob("*.jsonl"))]
        for kind, path, room in logs + [("assembled", r / "assembled.jsonl", r.name) for r in rooms]:
            if room in flight or not path.is_file() or not (st := path.stat()).st_size:
                continue
            if not (moved := move(kind, path, str(path.relative_to(state)))):
                continue
            dest = path.with_name(f"{path.name.removesuffix('.jsonl')}.{day(st.st_mtime)}.jsonl.gz")
            if not dry:                                                       # the original went first: the copy is cut from it
                os.close(os.open(dest, os.O_WRONLY | os.O_CREAT, st.st_mode & 0o777))   # its source's mode, never wider
                with open(moved, "rb") as f, gzip.open(dest, "ab") as g:      # a second cut the same day adds a member
                    shutil.copyfileobj(f, g)
        for kind, pattern in (("logs", "logs/*.jsonl.gz"), ("assembled", "rooms/*/assembled.*.jsonl.gz")):
            for p in sorted(state.glob(pattern)):
                if (m := DATED.search(p.name)) and m[1] < day(now - days[kind] * DAY):
                    move(kind, p, str(p.relative_to(state)))
        table = state / "sessions.json"                   # unreadable: the sweep fails, it never guesses that none is bound
        bound = {str(r.get("session_id")) for r in json.loads(table.read_text()).values()} if table.exists() else set()
        accounts = load_accounts(private / "accounts.toml") if (private / "accounts.toml").is_file() else []
        projects = sorted({(a.config_dir / "projects").resolve() for a in accounts})    # accounts that share one, once
        for folder in (r for r in rooms if r.name not in flight):
            for d in (p / project_dir((folder / "work").resolve()) for p in projects):
                sessions: dict[str, list[Path]] = {}
                for p in [*d.glob("*"), *(d / "rotated").glob("*")]:
                    if (m := SESSION.match(p.name)):
                        sessions.setdefault(m[0], []).append(p)
                for sid, paths in sessions.items():                           # 24 hours is the floor under any setting
                    if sid not in bound and max(s.st_mtime for p in paths for s in tree(p)) < now - max(days["transcripts"], 1) * DAY:
                        for p in paths:
                            move("transcripts", p, f"projects/{d.name}/{p.relative_to(d)}")
        root = worker_settings(state, dict(conf.get("workers", {}))).root.resolve()
        for w in ledger.workers(limit=10**6):
            folder = Path(str(w["staging"] or ""))
            if (w["finished"] and w["finished"] < now - days["workers"] * DAY and folder.is_absolute() and folder.is_dir()
                    and folder.parent.resolve() == root):
                gone = [p / project_dir(folder.resolve()) for p in projects]  # its own Claude Code project folder goes with it
                if move("workers", folder, f"workers/{folder.name}"):
                    for d in filter(Path.is_dir, gone):
                        move("workers", d, f"projects/{d.name}")
        for r in (r for r in rooms if r.name not in flight):              # scratch: past its days, then the oldest past the cap
            listed, emptied, work = scratch(r), set[Path](), r / "work"
            total = size([s for _, s in listed])
            for p, s in listed:                                          # one written in the last hour stays, whatever the cap
                if s.st_mtime <= now - DAY / 24 and (s.st_mtime < now - days["scratch"] * DAY or total > days["scratch_mb"] * 1e6) \
                        and move("scratch", p, str(p.relative_to(state))):
                    total -= size([s])
                    emptied.update(q for q in p.parents if q.is_relative_to(work) and len(q.relative_to(work).parts) > 1)
            for d in sorted(emptied, key=lambda q: -len(q.parts)) if not dry else []:   # the deepest first; a top folder stays
                with contextlib.suppress(OSError):
                    if free([d.lstat()]):
                        d.rmdir()                                        # empty, or it stays
        for name, col in TIMED.items():
            if (rows := ledger.older(name, col, cut := now - days["ledger"] * DAY)):
                lines = [json.dumps(r) + "\n" for r in rows]
                tally("ledger", len(rows), sum(map(len, lines)))
                if not dry:                               # a copy of the rows waits in the trash a week
                    (trash / "ledger").mkdir(parents=True, exist_ok=True)
                    with gzip.open(trash / "ledger" / f"{name}.jsonl.gz", "at") as z:
                        z.writelines(lines)
                    ledger.expire(name, col, cut)
        if not dry:
            ledger.checkpoint()
        out["inbox"] = sum(size(tree(r / "work" / "inbox")) for r in rooms if (r / "work" / "inbox").is_dir())
        for d in sorted((state / "trash").glob("????-??-??")):
            if d.name < day(now - days["trash"] * DAY) and free(found := tree(d)):
                tally("trash", 1, size(found))
                if not dry:
                    shutil.rmtree(d)
    except Exception as e:      # any failure is said, kept for the morning diff, exits 1; what moved before it stays counted
        out["error"], out["why"] = f"{type(e).__name__}: {e}", words.no_paths(getattr(e, "strerror", None) or type(e).__name__)
    return out


def main(private: Path, state: Path, dry: bool = False, now: float | None = None, proc: Path = PROC) -> int:
    """A line per kind that moved, every kind on a dry run; the run kept in <state>/retention.json (never a dry one)."""
    r = run(private, state, dry, now, proc)
    for kind in DAYS if dry else r["kinds"]:                         # a dry run names every kind, zeros too
        n, nbytes = r["kinds"].get(kind, (0, 0))
        verb = ("would be " if dry else "") + ("deleted" if kind == "trash" else "moved to the trash")
        print(f"{kind}: {n} {verb}, {words.size(nbytes)}")
    if dry or r["kinds"]:
        print(f"inbox: {words.size(r['inbox'])} of attachments, kept")
    if r.get("error"):
        print(f"retention failed: {r['error']}", file=sys.stderr)
    if not dry:
        write_private(state / LAST, json.dumps(r))
    return 1 if r.get("error") else 0


def line(state: Path, now: float) -> tuple[str, str] | None:
    """The morning diff's (finding key, line) for a sweep inside the last day: its failure, else what moved by kind,
    the biggest first; None when it moved nothing."""
    try:
        r = json.loads((state / LAST).read_text())
    except (OSError, ValueError):
        return None
    if now - float(r["at"]) > DAY or not (r.get("error") or r["kinds"]):
        return None
    if r.get("error"):
        return "retention failed", words.line(f"{words.NOT_DONE} {words.SWEEP_FAILED.format(why=r['why'])}")
    kinds = sorted(r["kinds"].items(), key=lambda kv: -kv[1][1])
    return "", words.line(f"{words.PLAIN} " + words.SWEPT.format(kinds=", ".join(f"{k} {words.size(b)}" for k, (_, b) in kinds)))


def here(private: Path) -> str:
    """`yerk explain retention`'s line for this install."""
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    k = keep(conf)
    return ("Here: " + ", ".join(f"{n} {k[n]:g} days" for n in DAYS) + f", a room's scratch at most {k['scratch_mb']:g} MB"
            " ([retention] in yerkd.toml)")
