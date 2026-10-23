"""Makes a picture sent to a room findable later, the way its uploads and recordings are, and lets a room zoom in on part of one. The room reads
turn it arrives (Read on an image is vision, so its content is in context at no extra cost) and files what it read with the
NAME.seen.txt beside the picture in <inbox>/<message id>/, its first line the header this module writes (room, message id, the local time it was
saved, its name), then every word on the picture verbatim in reading order, then one or two lines of what it shows. A password, a passphrase, a
lines of what it shows. The index takes it as an inbox text kind (memindex.SEEN), in its room's class (memory.INBOXES: an
person room's its own); recall and read_memory show its lines behind memory.SEEN_MARK, never as anyone's words. The backfill is an owner room's
only.
`yerk pictures backfill ROOM` files the pictures sent before this: a Yerk worker per BATCH pictures on the room's own account,
one at a time, one line to the machine room per batch; with `--root FOLDER`, the pictures under a folder the room lists in
rooms.toml (`read` or `also`), each sidecar beside its picture, indexed under that root as its other files are.
"""
from __future__ import annotations

import os
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from .config import write_private
from .memindex import SEEN, SEEN_HEAD, SKIP_DIRS

KINDS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")     # a picture as Discord delivers it
BATCH = 20                   # the pictures one backfill worker reads
TEXT_MAX = 20_000            # the most of a picture's words one sidecar keeps
NONE = "(no words on it)"
SHOWS = "what it shows: "


def is_picture(path: Path) -> bool:
    return path.suffix.lower() in KINDS


def sidecar(picture: Path) -> Path:
    return picture.with_name(f"{picture.stem}{SEEN}")


def header(room: str, picture: Path, tz: tzinfo) -> str:
    """The sidecar's first line, written by code: the room, the message id (the picture's folder in an inbox) or the folder
    (under a listed root), when it was saved, its name."""
    at = datetime.fromtimestamp(picture.stat().st_mtime, tz).strftime("%Y-%m-%d %H:%M %Z")
    where = f"message {picture.parent.name}" if picture.parts[-4:-2] == ("work", "inbox") else f"folder {picture.parent}"
    return f"{SEEN_HEAD}{room}, {where}, {at}: {picture.name}"     # memory.is_seen knows a root's sidecar by its start


def file(room: str, picture: Path, text: str, shows: str, tz: tzinfo) -> Path:
    """The `seen` tool's write: the header, the words as read (NONE when there are none), then at most two lines of what it shows."""
    said = "\n".join(x.rstrip() for x in text.strip().splitlines())[:TEXT_MAX] or NONE
    what = [SHOWS + x.strip().removeprefix(SHOWS) for x in shows.strip().splitlines() if x.strip()][:2]
    return write_private(sidecar(picture), f"{header(room, picture, tz)}\n{said}\n" + "\n".join(what) + "\n")


def pending(inbox: Path, root: bool = False) -> list[Path]:
    """The pictures with no sidecar, oldest first: in a room's inbox (<inbox>/<message id>/NAME), or with `root` anywhere under
    a listed folder but a dot or skipped folder (as the index walks it); never a link."""
    if not inbox.is_dir():
        return []
    if not root:
        found = [p for p in inbox.glob("*/*") if not p.parent.name.startswith(".")]
    else:
        found = []
        for d, dirs, names in os.walk(inbox):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS)
            found += [Path(d, n) for n in names]
    found = [p for p in found if is_picture(p) and p.is_file() and not p.is_symlink() and not sidecar(p).exists()]
    return sorted(found, key=lambda p: (p.stat().st_mtime, str(p)))


def root_of(private: Path, state: Path, room: str, conf: dict[str, Any], folder: str) -> Path | None:
    """`--root FOLDER`: the folder, resolved, when the room lists it in rooms.toml (`read` from its work folder, `also` from
    the private layer, as the daemon resolves each) and it holds none of Yerk's own folders nor sits in one; else None."""
    p, work = Path(folder).expanduser().resolve(), state / "rooms" / room / "work"
    listed = [(work / Path(str(x)).expanduser()).resolve() for x in conf.get("read") or []] + [
        (private / Path(str(x)).expanduser()).resolve() for x in conf.get("also") or []]
    own = (private.resolve(), state.resolve())
    return p if p.is_dir() and p in listed and not any(p.is_relative_to(q) or q.is_relative_to(p) for q in own) else None


def batches(pics: list[Path]) -> list[list[str]]:
    return [[str(p) for p in pics[i:i + BATCH]] for i in range(0, len(pics), BATCH)]


def brief(room: str, pics: list[Path], tz: tzinfo, n: int, of: int, root: Path | None = None) -> str:
    """A backfill worker's brief: each picture's path, its sidecar's path and the exact first line, then how a room files one."""
    rows = "\n".join(f"{i}. Read {p}\n   Write {sidecar(p)}\n   its first line exactly: {header(room, p, tz)}"
                     for i, p in enumerate(pics, 1))
    what = f"in {root}, a folder the room reads, with nothing filed on them yet" if root else (
        "the owner sent before the room filed what it saw on them")
    return (f"Pictures backfill for room {room}, batch {n} of {of}: {len(pics)} pictures {what}. File each one the way the room "
            "now files a picture it is sent, so a later recall finds it.\n\n"
            f"{rows}\n\n"
            "For each picture, in order: Read it (the Read tool shows you the image), then Write its sidecar at the path given:\n"
            "- line 1: the first line given above, copied exactly;\n"
            "- then every word visible on the picture, verbatim, in reading order, its line breaks kept; a screenshot of a chat "
            f"keeps who said what exactly as shown; a word you cannot make out is [unreadable]; \"{NONE}\" when it has none;\n"
            f"- then one or two lines starting \"{SHOWS}\": what the picture is, in plain words. A person in it is \"the owner\", "
            "since they sent it, unless the picture's own text says otherwise; anyone else is named only by a name the picture's "
            "own text gives them, else \"a man\", \"a woman\" or \"a person\". Never guess.\n"
            "Tick the picture's TASKS.md row once its sidecar is written. Write nothing else in the room's folders and change no "
            "picture. A picture that will not open gets no sidecar: name it in your report with why.\n"
            "REPORT.md's first line: \"N of M filed\", then one line per picture that was not.\n")
