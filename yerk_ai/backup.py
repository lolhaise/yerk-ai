"""`yerk backup` and `yerk restore`: everything one failed disk would lose, saved in one dated archive and put back from it. The
private layer whole (persona, rules, rooms, memory, facts, imports, the attachments folders, secrets as bytes with their
modes), the state's ledger (SQLite's own backup, whole while yerkd writes), the rooms' session bindings and small files,
the index's metadata, and the install's systemd units, as `yerk-YYYYmmdd-HHMMSS.tar`, 0600 in a 0700 folder: yerkd.toml's
`[backup] path`, else yerk-backups beside the private folder; the last `[backup] keep` (7) stay. Never the index's vector
store (`yerk-index --rebuild` makes it again from the layer) unless --with-index, never logs or a room's scratch. The last
member, MANIFEST.json, lists every other (a file's bytes, sha256 and mode, a folder's mode, a link's target), and a
restore reads the whole archive against it before it writes anything: one cut short or one byte changed is refused. A
restore goes into an empty folder, or with --force over the layer after a backup of it, what it replaces moved to
<state>/trash/<day>/restore-<time>/ first, never deleted; never under a running yerkd unless --force says so (PR 65's design)."""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Iterator

from .config import REPO, load_toml

MANIFEST, KEEP, STALE = "MANIFEST.json", 7, 36 * 3600.0       # a newest backup older than STALE is the doctor's finding
NAME = re.compile(r"yerk-\d{8}-\d{6}\.tar")
UNITS = Path("~/.config/systemd/user")                        # where the units are copied to (README), yerkd's by deploy
STATE = ("sessions.json", "cooldowns.json", "meters.json", "memory/index.json", "memory/mentions.json",
         "rooms/*/override.json", "rooms/*/fresh-next")      # beside the ledger; sessions.json binds each room to its session
TOPS = ("private", "state", "units")


class Refused(Exception):
    """A restore stopped before it wrote anything; the one line says why."""


class Tee:
    """A file read once into the archive, hashed as its bytes go by."""
    def __init__(self, f: IO[bytes]) -> None:
        self.f, self.sha = f, hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        b = self.f.read(n)
        self.sha.update(b)
        return b


def settings(private: Path, state: Path) -> tuple[Path, int, Path]:
    """yerkd.toml's [backup]: the folder (relative to the private folder; the repo's example layer's beside its state,
    never in the repo), how many archives stay, and the units folder."""
    conf = dict((load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}).get("backup") or {})
    base = state if private.resolve().is_relative_to(REPO) else private.resolve().parent
    folder = private / Path(str(conf["path"])).expanduser() if conf.get("path") else base / "yerk-backups"
    return folder, max(1, int(conf.get("keep", KEEP))), Path(str(conf.get("units") or UNITS)).expanduser()


def newest(private: Path, state: Path) -> dict[str, Any] | None:
    """The newest archive in the backup folder (its file's time, bytes, name); None when there is none or yerkd.toml does not read."""
    with suppress(OSError, ValueError, TypeError):              # the daemon's /status card is never lost to it
        folder = settings(private, state)[0]
        got = sorted((st.st_mtime, st.st_size, p.name) for p in (folder.iterdir() if folder.is_dir() else [])
                     if NAME.fullmatch(p.name) and (st := p.stat()))
        return dict(zip(("at", "bytes", "name"), got[-1])) if got else None
    return None


def row(t: tarfile.TarInfo, sha: str = "") -> dict[str, Any]:
    """A member as the manifest lists it."""
    if t.issym():
        return {"path": t.name, "link": t.linkname}
    return {"path": t.name, "mode": t.mode & 0o7777, **({"bytes": t.size, "sha256": sha} if t.isfile() else {})}


def add(tar: tarfile.TarFile, path: Path, name: str, rows: list[dict[str, Any]]) -> None:
    """A file, folder or link into the archive as `name`; a file read once, its sha256 taken from the bytes that went in."""
    t = tar.gettarinfo(path, name)
    if t is None or not (t.isfile() or t.isdir() or t.issym()):
        return                                                # a socket or a pipe: nothing to keep
    if not t.isfile():
        tar.addfile(t)
        rows.append(row(t))
        return
    with open(path, "rb") as f:
        tar.addfile(t, tee := Tee(f))
    rows.append(row(t, tee.sha.hexdigest()))


def make(private: Path, state: Path, folder: Path, units: Path, index: bool = False, now: float | None = None) -> tuple[Path, int, int]:
    """One archive in `folder`, written as a hidden .part and renamed only when whole, so a run cut short leaves no
    `yerk-*` file. Its path, its files and their bytes."""
    now = time.time() if now is None else now
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = int(now)
    while (final := folder / f"yerk-{datetime.fromtimestamp(stamp):%Y%m%d-%H%M%S}.tar").exists():
        stamp += 1                                              # never over another archive: a second run the same second takes the next
    part, copy, rows, skip = folder / f".{final.name}.part", folder / f".{final.name}.db", list[dict[str, Any]](), folder.resolve()
    try:
        with os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f, tarfile.open(fileobj=f, mode="w") as tar:
            add(tar, private, "private", rows)
            for d, dirs, names in os.walk(private):
                dirs[:] = sorted(x for x in dirs if Path(d, x).resolve() != skip)      # never a backup folder inside the layer
                for x in sorted([*dirs, *names]):
                    add(tar, Path(d, x), f"private/{Path(d, x).relative_to(private)}", rows)
            for rel in ("ledger.sqlite", *(["memory/index.sqlite"] if index else [])):
                if (state / rel).is_file():
                    os.close(os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
                    src, dst = sqlite3.connect((state / rel).resolve().as_uri() + "?mode=ro", uri=True), sqlite3.connect(copy)
                    try:
                        src.backup(dst)
                    finally:
                        src.close()
                        dst.close()
                    add(tar, copy, f"state/{rel}", rows)
            for p in [p for pat in STATE for p in sorted(state.glob(pat))]:
                add(tar, p, f"state/{p.relative_to(state)}", rows)
            for p in sorted([*units.glob("yerk*.service"), *units.glob("yerk*.timer")]):
                add(tar, p, f"units/{p.name}", rows)
            body = json.dumps({"version": 1, "made": now, "files": rows}, indent=1).encode()
            t = tarfile.TarInfo(MANIFEST)
            t.size, t.mode, t.mtime = len(body), 0o600, int(now)
            tar.addfile(t, io.BytesIO(body))
        os.rename(part, final)
    finally:
        part.unlink(missing_ok=True)
        copy.unlink(missing_ok=True)
    return final, sum("sha256" in r for r in rows), sum(int(r.get("bytes", 0)) for r in rows)


def prune(folder: Path, keep: int) -> list[str]:
    """Every archive but the newest `keep` deleted, by the time in their names; the names deleted."""
    old = sorted(p for p in folder.iterdir() if NAME.fullmatch(p.name))[:-keep]
    for p in old:
        p.unlink()
    return [p.name for p in old]


def main(private: Path, state: Path, to: str | None = None, index: bool = False) -> int:
    """`yerk backup`: one line, the archive and what it holds, else why there is none (exit 1)."""
    folder, keep, units = settings(private, state)
    folder, t0 = Path(to).expanduser() if to else folder, time.monotonic()
    try:
        path, n, size = make(private, state, folder, units, index)
    except (OSError, sqlite3.Error, tarfile.TarError) as e:
        print(f"backup failed, nothing was written: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    gone = prune(folder, keep)
    print(f"backup: {path}, {n:,} files, {size:,} bytes, {time.monotonic() - t0:.1f} s; the last {keep} kept, {len(gone)} older removed")
    return 0


def digest(f: IO[bytes]) -> str:
    sha = hashlib.sha256()
    while b := f.read(1 << 20):
        sha.update(b)
    return sha.hexdigest()


def differs(listed: dict[str, dict[str, Any]], seen: dict[str, dict[str, Any]]) -> list[str]:
    """The members the archive and its manifest do not agree on: one missing from either, or one byte or mode apart."""
    return sorted(set(listed) ^ set(seen)) or sorted(p for p in listed if listed[p] != seen[p])


def verify(tar: tarfile.TarFile, name: str) -> dict[str, Any]:
    """The manifest, once every member was read against it; Refused for an archive cut short or changed."""
    seen, manifest = dict[str, dict[str, Any]](), None
    try:
        for t in tar:
            f = tar.extractfile(t) if t.isfile() else None
            if t.name == MANIFEST:
                manifest = json.loads((f or io.BytesIO()).read())
            else:
                seen[t.name] = row(t, digest(f) if f else "")
    except (tarfile.TarError, OSError, EOFError, ValueError) as e:
        raise Refused(f"refused: {name} is partial or damaged ({type(e).__name__}: {e}); nothing was restored") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise Refused(f"refused: {name} has no manifest, so it is partial; nothing was restored")
    if bad := differs({str(r.get("path")): r for r in manifest["files"]}, seen):
        raise Refused(f"refused: {name} does not match its manifest at {bad[0]} ({len(bad)} member(s)); nothing was restored")
    if outside := [p for p in seen if p.partition("/")[0] not in TOPS or ".." in Path(p).parts]:
        raise Refused(f"refused: {name} holds {outside[0]}, outside private/, state/ and units/; nothing was restored")
    return manifest


def keep_mode(t: tarfile.TarInfo, dest: str) -> tarfile.TarInfo:
    """tarfile's own checks (no absolute name, nothing landing outside `dest`) with the mode the member was archived with."""
    return tarfile.tar_filter(t, dest).replace(mode=t.mode & 0o777, deep=False)


def running(state: Path) -> bool:
    """yerkd holds <state>/yerkd.lock for its life (daemon.claim): a lock it blocks is a daemon that runs."""
    try:
        fd = os.open(state / "yerkd.lock", os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def restore(archive: Path, private: Path, state: Path, to: Path | None, dry: bool, force: bool) -> Iterator[str]:
    """A line a step: the archive read whole against its manifest, the target checked, the layer it replaces backed up and
    moved aside (--force), then each part written; nothing written under --dry-run or when refused."""
    try:
        tar = tarfile.open(archive, "r:")
    except (tarfile.TarError, OSError) as e:
        raise Refused(f"refused: {archive.name} does not open as an archive ({type(e).__name__}); nothing was restored") from None
    with tar:
        files = [r for r in verify(tar, archive.name)["files"] if "sha256" in r]
        yield f"verified: {archive.name}, {len(files):,} files, {sum(int(r['bytes']) for r in files):,} bytes, each as its manifest says"
        folder, _, units = settings(private, state)
        if to is not None:
            if to.exists() and any(to.iterdir()):
                raise Refused(f"refused: {to} is not empty; restore into an empty folder, or over the layer with --force")
            private, state, units = to / "private", to / "state", to / "units"
        full, would = [p for p in (private, state) if p.is_dir() and any(p.iterdir())], "would " if dry else ""
        if full and not force:
            raise Refused(f"refused: {full[0]} holds files; restore --to an empty folder, or --force over it after a backup of it")
        if running(state):
            if not force:
                raise Refused(f"refused: yerkd runs on {state}; stop it first, or --force")
            yield f"yerkd runs on {state}: {would}restore under it (--force); restart it to read what was restored"
        roots = dict(zip(TOPS, (private, state, units)))
        if full:
            yield f"{would}back up first: " + (str(folder) if dry else str(make(private, state, folder, units)[0]))
        aside = state / "trash" / f"{(now := datetime.now()):%Y-%m-%d}" / f"restore-{now:%H%M%S}"
        old = [(private, aside / "private")] if private in full else []     # and every file it writes over, a unit's too
        for top, _, rest in (r["path"].partition("/") for r in files if not r["path"].startswith("private/")):
            old += [(q, aside / top / q.relative_to(roots[top])) for q in (roots[top] / rest, roots[top] / f"{rest}-wal",
                                                                         roots[top] / f"{rest}-shm") if os.path.lexists(q)]
        for p, dest in [] if dry else old:                          # a ledger's -wal goes with it: never paired with another
            (state / "trash").mkdir(mode=0o700, exist_ok=True)
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.move(p, dest)
        if old:
            yield f"{would}move aside {len(old)} path(s) it replaces, to {aside}"
        for top, root in roots.items():
            mine = [t.replace(name=t.name[len(top) + 1:], deep=False) for t in tar.getmembers() if t.name.startswith(top + "/")]
            if not dry:
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                tar.extractall(root, members=mine, filter=keep_mode)
            yield f"{would}restore {top}: {sum(t.isfile() for t in mine):,} files into {root}"


def restore_main(archive: str, private: Path, state: Path, to: str | None, dry: bool, force: bool) -> int:
    """`yerk restore`: each step a line as it happens; exit 1 when refused."""
    try:
        for line in restore(Path(archive).expanduser(), private, state, Path(to).expanduser() if to else None, dry, force):
            print(line, flush=True)
    except Refused as e:
        print(e, file=sys.stderr)
        return 1
    except (OSError, tarfile.TarError) as e:                         # after the checks: the lines above say where things went
        print(f"restore failed part way: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0
