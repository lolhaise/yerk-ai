"""Makes a picture sent to a room findable later, the way its uploads and recordings are, and lets a room zoom in on part of one. The room reads
turn it arrives (Read on an image is vision, so its content is in context at no extra cost) and files what it read with the
NAME.seen.txt beside the picture in <inbox>/<message id>/, its first line the header this module writes (room, message id, the local time it was
saved, its name), then every word on the picture verbatim in reading order, then one or two lines of what it shows. A password, a passphrase, a
key or a token the picture shows is written [left out] in its place, never its value (words.SEEN_LEFT_OUT, the tool's description and the
backfill's rule alike), and the daemon refuses a line that holds one by its shape (doctor.SECRETS) or a saved login's password (logins.passwords),
so no note keeps it. The index takes it as an inbox text kind (memindex.SEEN), in its room's class (memory.INBOXES: an owner room's private, a
person room's its own); recall and read_memory show its lines behind memory.SEEN_MARK, never as anyone's words. The backfill is an owner room's
only.
`yerk pictures backfill ROOM` files the pictures sent before this: a Yerk worker per BATCH pictures on the room's own account,
one at a time, one line to the machine room per batch; with `--root FOLDER`, the pictures under a folder the room lists in
rooms.toml (`read` or `also`), each sidecar beside its picture, indexed under that root as its other files are.
`zoom` (the tool): Read shows a room a picture at most EDGE pixels on its long side, so the small print on a bigger one is
lost; a part of it, as fractions of its width and height, is cut out and scaled by ffmpeg to EDGE on its long side (lanczos,
enlarged MOST_UP times at most) and written as a PNG under the room's work/ZOOM/, which no scan of the pictures sent reads
and the scratch sweep takes. size() reads a picture's width and height off its header, never a decode.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, BinaryIO, Callable

from . import words
from .config import write_private
from .context import attachments_dir
from .memindex import SEEN, SEEN_HEAD, SKIP_DIRS

KINDS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")     # a picture as Discord delivers it
BATCH = 20                   # the pictures one backfill worker reads
TEXT_MAX = 20_000            # the most of a picture's words one sidecar keeps
NONE = "(no words on it)"
SHOWS = "what it shows: "
EDGE, MOST_UP = 2000, 4.0    # pixels on the long side Read shows at most (Claude Code 2.1.316); the most a zoom enlarges
ZOOM, TIMEOUT = "zoom", 60   # under a room's work folder: zoom/<its folder's name, a message id>/STEM.zoom-N.png; seconds
SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}   # a JPEG's frame headers


def is_picture(path: Path) -> bool:
    return path.suffix.lower() in KINDS


def size(path: Path) -> tuple[int, int] | None:
    """A picture's width and height off its header, never a decode: a PNG's IHDR, a GIF's screen, a WebP's first chunk, a
    JPEG's frame header after its segments (an EXIF orientation that turns it a quarter, as ffmpeg turns it, swaps the
    two); None for anything else or a header cut short."""
    try:
        with path.open("rb") as f:
            h = f.read(30)
            if h.startswith(b"\x89PNG\r\n\x1a\n") and h[12:16] == b"IHDR" and len(h) >= 24:
                return int.from_bytes(h[16:20], "big"), int.from_bytes(h[20:24], "big")
            if h[:6] in (b"GIF87a", b"GIF89a") and len(h) >= 10:
                return int.from_bytes(h[6:8], "little"), int.from_bytes(h[8:10], "little")
            if h[:4] == b"RIFF" and h[8:12] == b"WEBP" and len(h) == 30:
                return webp(h[12:16], h[20:30])
            return jpeg(f) if h[:2] == b"\xff\xd8" else None
    except (OSError, struct.error):
        return None


def webp(kind: bytes, d: bytes) -> tuple[int, int] | None:
    """A WebP's size from its first chunk: lossy (VP8), lossless (VP8L) or extended (VP8X, its canvas)."""
    if kind == b"VP8 " and d[3:6] == b"\x9d\x01\x2a":
        return int.from_bytes(d[6:8], "little") & 0x3FFF, int.from_bytes(d[8:10], "little") & 0x3FFF
    if kind == b"VP8L" and d[:1] == b"\x2f":
        v = int.from_bytes(d[1:5], "little")
        return (v & 0x3FFF) + 1, (v >> 14 & 0x3FFF) + 1
    if kind == b"VP8X":
        return int.from_bytes(d[4:7], "little") + 1, int.from_bytes(d[7:10], "little") + 1
    return None


def jpeg(f: BinaryIO) -> tuple[int, int] | None:
    """A JPEG's size, its segments walked from the start to its frame header (an EXIF block or a colour profile comes first)."""
    f.seek(2)
    turned = False
    while len(m := f.read(2)) == 2 and m[0] == 0xFF:
        if m[1] == 0xFF:                                         # a fill byte before a marker
            f.seek(-1, 1)
        elif m[1] in (0xD9, 0xDA):                               # the end, or the picture's data, before any frame header
            return None
        elif m[1] != 0x01 and not 0xD0 <= m[1] <= 0xD7:          # those have no length
            n = struct.unpack(">H", f.read(2))[0]
            if m[1] in SOF:
                hi, wide = struct.unpack(">xHH", f.read(5))
                return (hi, wide) if turned else (wide, hi)
            if m[1] == 0xE1:
                turned = quarter(f.read(n - 2)) or turned
            else:
                f.seek(n - 2, 1)
    return None


def quarter(app1: bytes) -> bool:
    """Whether an EXIF block's orientation (5 to 8) turns the picture a quarter, so its width and height swap."""
    t, end = app1[6:], {b"II": "<", b"MM": ">"}.get(app1[6:8], "")
    if not app1.startswith(b"Exif\x00\x00") or not end:
        return False
    at = struct.unpack(end + "I", t[4:8])[0]
    for i in range(struct.unpack(end + "H", t[at:at + 2])[0]):
        tag, _, _, value = struct.unpack(end + "HHIH", t[at + 2 + 12 * i:at + 12 + 12 * i])
        if tag == 0x0112:
            return bool(value in (5, 6, 7, 8))
    return False


def zoom(picture: Path, box: tuple[float, float, float, float], into: Path) -> dict[str, Any]:
    """The `zoom` tool's call, blocking (the daemon runs it off the loop): the box, fractions of the picture's width and
    height, cut out by ffmpeg and scaled so its long side is EDGE (enlarged MOST_UP times at most), saved in `into` as
    STEM.zoom-N.png, N one past this picture's last zoom there; a picture whose header gives no size (a HEIC) is made a
    whole PNG first, so the box is in pixels ffmpeg itself counts."""
    x0, y0, x1, y1 = box
    why = ("outside" if not all(0 <= v <= 1 for v in box) else "inverted" if x1 < x0 or y1 < y0 else
           "empty" if x1 == x0 or y1 == y0 else "")
    if why:
        return {"ok": False, "error": words.ZOOM_BOX.format(box=", ".join(f"{v:g}" for v in box), why=words.ZOOM_WHY[why])}
    if not (exe := shutil.which("ffmpeg")):
        return {"ok": False, "error": words.ZOOM_NO_FFMPEG.format(name=picture.name)}
    with tempfile.TemporaryDirectory(prefix="yerk-zoom-") as d:
        src, whole = picture, Path(d) / "whole.png"
        if not (dims := size(picture)):
            if why := ffmpeg(exe, picture, "null", whole):
                return {"ok": False, "error": words.REVERSE_NOT_PICTURE.format(name=picture.name, why=why)}
            src, dims = whole, size(whole)
        w, h = dims or (0, 0)
        px = [round(x0 * w), round(y0 * h), round(x1 * w), round(y1 * h)]
        cw, ch = px[2] - px[0], px[3] - px[1]
        if cw < 1 or ch < 1:
            return {"ok": False, "error": words.ZOOM_BOX.format(box=", ".join(f"{v:g}" for v in box), why=words.ZOOM_WHY["empty"])}
        scale = min(MOST_UP, EDGE / max(cw, ch))
        into.mkdir(parents=True, exist_ok=True)
        done = [int(m[1]) for f in into.iterdir() if (m := re.fullmatch(re.escape(picture.stem) + r"\.zoom-(\d+)\.png", f.name))]
        out = into / f"{picture.stem}.zoom-{max(done, default=0) + 1}.png"
        fit = f"format=rgb24|rgba,crop={cw}:{ch}:{px[0]}:{px[1]},scale={round(cw * scale)}:{round(ch * scale)}:flags=lanczos"
        if why := ffmpeg(exe, src, fit, out):
            return {"ok": False, "error": words.REVERSE_NOT_PICTURE.format(name=picture.name, why=why)}
    return {"ok": True, "file": str(out), "box_px": px, "scale": round(scale, 2), "next": words.ZOOM_NEXT}


def ffmpeg(exe: str, src: Path, fit: str, out: Path) -> str:
    """One frame of `src` through the filters `fit` to the PNG `out`: "" once it is written, else why not, in ffmpeg's own
    last line (no path in it), and no part of a file left behind."""
    try:
        p = subprocess.run([exe, "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", fit, str(out)],
                           capture_output=True, text=True, timeout=TIMEOUT)
        why = "" if p.returncode == 0 else words.no_paths(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "")[:160]
    except subprocess.TimeoutExpired:
        why = words.WATCH_SLOW.format(seconds=TIMEOUT)
    if why or not out.is_file() or not out.stat().st_size:
        out.unlink(missing_ok=True)
        return why or words.WATCH_NO_WHY
    return ""


def sidecar(picture: Path) -> Path:
    return picture.with_name(f"{picture.stem}{SEEN}")


def header(room: str, picture: Path, tz: tzinfo, head: str = SEEN_HEAD) -> str:
    """The sidecar's first line, written by code: the room, the message id (the picture's folder in an inbox) or the folder
    (under a listed root), when it was saved, its name; a workbook's or a PDF's the same, from its own `head` (files.py)."""
    at = datetime.fromtimestamp(picture.stat().st_mtime, tz).strftime("%Y-%m-%d %H:%M %Z")
    where = f"message {picture.parent.name}" if picture.parts[-4:-2] == ("work", "inbox") else f"folder {picture.parent}"
    return f"{head}{room}, {where}, {at}: {picture.name}"     # memory.is_seen knows a root's sidecar by its start


def file(room: str, picture: Path, text: str, shows: str, tz: tzinfo) -> Path:
    """The `seen` tool's write: the header, the words as read (NONE when there are none), then at most two lines of what it shows."""
    said = "\n".join(x.rstrip() for x in text.strip().splitlines())[:TEXT_MAX] or NONE
    what = [SHOWS + x.strip().removeprefix(SHOWS) for x in shows.strip().splitlines() if x.strip()][:2]
    return write_private(sidecar(picture), f"{header(room, picture, tz)}\n{said}\n" + "\n".join(what) + "\n")


def pending(inbox: Path, root: bool = False, want: Callable[[Path], bool] | None = None) -> list[Path]:
    """The pictures with no sidecar (with `want`, the files it picks), oldest first: in a room's inbox (<inbox>/<message id>/NAME),
    or with `root` anywhere under a listed folder but a dot or skipped folder (as the index walks it); never a link."""
    if not inbox.is_dir():
        return []
    if not root:
        found = [p for p in inbox.glob("*/*") if not p.parent.name.startswith(".")]
    else:
        found = []
        for d, dirs, names in os.walk(inbox):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS)
            found += [Path(d, n) for n in names]
    found = [p for p in found if p.is_file() and not p.is_symlink() and (want(p) if want else is_picture(p) and not sidecar(p).exists())]
    return sorted(found, key=lambda p: (p.stat().st_mtime, str(p)))


def root_of(private: Path, state: Path, room: str, conf: dict[str, Any], folder: str) -> Path | None:
    """`--root FOLDER`: the folder, resolved, when the room lists it in rooms.toml (`read` from its work folder, `also` from
    the private layer, as the daemon resolves each) and it holds none of Yerk's own folders nor sits in one; else None. The
    one folder in the private layer it takes is the room's own <layer>/NAME-attachments listed in its `also`: the files
    `yerk room add` saved, nothing of Yerk's (never in the state, which a worker reads only)."""
    p, work = Path(folder).expanduser().resolve(), state / "rooms" / room / "work"
    also = [(private / Path(str(x)).expanduser()).resolve() for x in conf.get("also") or []]
    listed = [(work / Path(str(x)).expanduser()).resolve() for x in conf.get("read") or []] + also
    own = (state.resolve(),) if p in also and p == attachments_dir(private, room, conf) else (private.resolve(), state.resolve())
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
            f"- then every word visible on the picture, verbatim, in reading order, its line breaks kept, {words.SEEN_LEFT_OUT}; "
            "a screenshot of a chat keeps who said what exactly as shown; a word you cannot make out is [unreadable]; "
            f"\"{NONE}\" when it has none;\n"
            f"- then one or two lines starting \"{SHOWS}\": what the picture is, in plain words. A person in it is \"the owner\", "
            "since they sent it, unless the picture's own text says otherwise; anyone else is named only by a name the picture's "
            "own text gives them, else \"a man\", \"a woman\" or \"a person\". Never guess.\n"
            "Tick the picture's TASKS.md row once its sidecar is written. Write nothing else in the room's folders and change no "
            "picture. A picture that will not open gets no sidecar: name it in your report with why.\n"
            "REPORT.md's first line: \"N of M filed\", then one line per picture that was not.\n")
