"""Checks the zoom tool (pictures.zoom), which hands a room an enlarged part of a picture so it can read the small print, and the header
read under it (pictures.size). Read shows a room a picture at most 2000 px on its long side, so the small print of a bigger one (a time in a
calendar, a figure on a form) was lost, and ffmpeg in a room's shell waited on an approval no one gives mid-turn. A part of a picture in the
room's own folders, as fractions of its width and height, comes back as a PNG 2000 px on its long side (enlarged 4 times at most), numbered
work/zoom/<message id>/; the whole picture re-encoded is the way past a picture Read refuses. A picture's size is read off
its header (PNG, JPEG, WebP, GIF), never decoded, and an [attachment] line names a picture past 2000 px with that zoom
reads its small print. Every refusal is said in words. All pictures here are made by ffmpeg or by zlib, none real."""
from __future__ import annotations

import asyncio
import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from yerk_ai import card, context, pictures, words
from yerk_ai.channels.discord import message_text
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel

EXAMPLE_PRIVATE = Path(__file__).resolve().parents[1] / "examples" / "private"
FFMPEG = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def png(w: int, h: int) -> bytes:
    """A grey RGB PNG of w x h, made in pure Python through zlib."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\x80" * (3 * w) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def made(out: Path, size: str = "2400x1400", *more: str) -> Path:
    """ffmpeg's test picture of `size` saved as `out` (its kind by its suffix)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}", "-frames:v", "1", *more, str(out)],
                   check=True, capture_output=True)
    return out


def turned(jpeg: bytes, orientation: int) -> bytes:
    """A JPEG with an EXIF block (big-endian) whose orientation is `orientation`, put ahead of its other segments."""
    tiff = b"MM\x00\x2a" + struct.pack(">IHHHIHH", 8, 1, 0x0112, 3, 1, orientation, 0) + struct.pack(">I", 0)
    block = b"Exif\x00\x00" + tiff
    return jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(block) + 2) + block + jpeg[2:]


def room(tmp_path: Path) -> tuple[Yerkd, Path]:
    """The example room on a private folder of its own, and a message's folder in its inbox."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (state := tmp_path / "state").mkdir(parents=True)
    y = Yerkd(private, state)
    y.add_room("example-room", ConsoleChannel())
    (inbox := state / "rooms" / "example-room" / "work" / "inbox" / "1001").mkdir(parents=True)
    return y, inbox


def zoom(y: Yerkd, p: Path, *box: float, name: str = "example-room") -> dict[str, Any]:
    return asyncio.run(y.tool_zoom(name, str(p), *box))


def test_the_header_read_gives_a_pngs_size_from_its_ihdr_and_none_for_a_file_that_is_no_picture(tmp_path):
    (p := tmp_path / "a.png").write_bytes(png(2401, 3))
    (cut := tmp_path / "cut.png").write_bytes(png(5, 5)[:20])
    (text := tmp_path / "a.txt").write_text("not a picture at all, longer than thirty bytes")
    assert pictures.size(p) == (2401, 3) and pictures.size(cut) is None and pictures.size(text) is None
    assert pictures.size(tmp_path / "gone.png") is None


def test_the_header_read_gives_a_webp_extended_canvas_and_a_gif_screen_from_their_bytes(tmp_path):
    vp8x = b"VP8X" + struct.pack("<I", 10) + b"\x10\x00\x00\x00" + (2399).to_bytes(3, "little") + (1399).to_bytes(3, "little")
    (p := tmp_path / "x.webp").write_bytes(b"RIFF" + struct.pack("<I", 4 + len(vp8x)) + b"WEBP" + vp8x)
    (g := tmp_path / "g.gif").write_bytes(b"GIF89a" + struct.pack("<HH", 2600, 90) + b"\x00" * 30)
    assert pictures.size(p) == (2400, 1400) and pictures.size(g) == (2600, 90)


@FFMPEG
def test_the_header_read_matches_what_ffmpeg_wrote_for_png_jpeg_webp_and_gif(tmp_path):
    assert pictures.size(made(tmp_path / "t.png")) == (2400, 1400)
    assert pictures.size(made(tmp_path / "t.jpg")) == (2400, 1400)
    assert pictures.size(made(tmp_path / "t.webp", "2401x1399")) == (2401, 1399)                       # lossy: VP8
    assert pictures.size(made(tmp_path / "l.webp", "2401x1399", "-lossless", "1")) == (2401, 1399)    # lossless: VP8L
    assert pictures.size(made(tmp_path / "t.gif", "640x481")) == (640, 481)


@FFMPEG
def test_a_jpeg_whose_exif_turns_it_a_quarter_reads_turned_as_ffmpeg_turns_it(tmp_path):
    plain = made(tmp_path / "plain.jpg", "400x200").read_bytes()
    (six := tmp_path / "six.jpg").write_bytes(turned(plain, 6))
    (three := tmp_path / "three.jpg").write_bytes(turned(plain, 3))
    assert pictures.size(six) == (200, 400) and pictures.size(three) == (400, 200)
    y, inbox = room(tmp_path)
    shutil.copy(six, p := inbox / "six.jpg")
    r = zoom(y, p, 0, 0, 0.5, 1)
    assert r["ok"] and r["box_px"] == [0, 0, 100, 400] and pictures.size(Path(r["file"])) == (400, 1600)


@FFMPEG
def test_a_part_of_a_picture_comes_back_a_png_2000_px_on_its_long_side_numbered_per_picture(tmp_path):
    y, inbox = room(tmp_path)
    p = made(inbox / "calendar.png")
    first = zoom(y, p, 0.5, 0.5, 1, 1)
    assert first == {"ok": True, "file": first["file"], "box_px": [1200, 700, 2400, 1400], "scale": 1.67, "next": words.ZOOM_NEXT}
    out = Path(first["file"])
    assert out == tmp_path / "state" / "rooms" / "example-room" / "work" / "zoom" / "1001" / "calendar.zoom-1.png"
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and pictures.size(out) == (2000, 1167)
    second = zoom(y, p, 0.25, 0, 0.5, 0.5)
    assert second["box_px"] == [600, 0, 1200, 700] and second["scale"] == 2.86
    assert Path(second["file"]).name == "calendar.zoom-2.png" and pictures.size(Path(second["file"])) == (1714, 2000)
    other = zoom(y, made(inbox / "receipt.jpg", "800x2400"), 0, 0, 1, 0.5)
    assert Path(other["file"]).name == "receipt.zoom-1.png" and pictures.size(Path(other["file"])) == (1333, 2000)
    assert sorted(x.name for x in pictures.pending(inbox.parent)) == ["calendar.png", "receipt.jpg"]     # no zoom is a picture sent


@FFMPEG
def test_the_whole_picture_is_made_one_read_opens_and_the_enlargement_stops_at_4x(tmp_path):
    y, inbox = room(tmp_path)
    p = made(inbox / "screen.png", "2835x2004")
    whole = zoom(y, p, 0, 0, 1, 1)
    assert whole["box_px"] == [0, 0, 2835, 2004] and whole["scale"] == 0.71 and pictures.size(Path(whole["file"])) == (2000, 1414)
    small = zoom(y, p, 0.1, 0.1, 0.2, 0.15)                     # 284 x 100 px: 7x would fit 2000, 4x is the most
    assert small["box_px"] == [284, 200, 567, 301] and small["scale"] == 4.0 and pictures.size(Path(small["file"])) == (1132, 404)


@FFMPEG
def test_every_refusal_is_said_in_words_and_writes_nothing(tmp_path, monkeypatch):
    y, inbox = room(tmp_path)
    p = made(inbox / "form.png", "640x480")
    (outside := tmp_path / "elsewhere").mkdir()
    shutil.copy(p, away := outside / "form.png")
    (note := inbox / "note.txt").write_text("words")
    (fake := inbox / "fake.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    box = (0, 0, 1, 1)
    assert zoom(y, away, *box) == {"ok": False, "error": words.ZOOM_NOT_HERE.format(name="form.png")}
    assert zoom(y, p, *box, name="no-such-room") == {"ok": False, "error": words.ZOOM_NOT_HERE.format(name="form.png")}
    assert zoom(y, inbox / "gone.png", *box) == {"ok": False, "error": words.REVERSE_NO_FILE.format(name="gone.png")}
    assert zoom(y, note, *box) == {"ok": False, "error": words.ZOOM_NOT_PICTURE.format(name="note.txt")}
    for given, why in (((0, 0, 1.2, 1), "outside"), ((-0.1, 0, 1, 1), "outside"), ((0.5, 0, 0.4, 1), "inverted"),
                       ((0, 0.6, 1, 0.2), "inverted"), ((0.3, 0, 0.3, 1), "empty"), ((0, 0, 0.0001, 1), "empty"),
                       ((0, 0, float("nan"), 1), "outside")):
        r = zoom(y, p, *given)
        assert r["ok"] is False and words.ZOOM_WHY[why] in r["error"] and "fractions" in r["error"], (given, r)
    r = zoom(y, fake, *box)
    assert r["ok"] is False and r["error"].startswith("fake.png did not read as a picture (") and str(tmp_path) not in r["error"]
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert zoom(y, p, *box) == {"ok": False, "error": words.ZOOM_NO_FFMPEG.format(name="form.png")}
    assert not (tmp_path / "state" / "rooms" / "example-room" / "work" / "zoom").exists()


def test_the_attachment_line_says_a_picture_past_2000_px_is_shrunk_and_only_then(tmp_path):
    (big := tmp_path / "big.png").write_bytes(png(2461, 1440))
    (edge := tmp_path / "edge.png").write_bytes(png(2000, 1999))
    (doc := tmp_path / "big.pdf").write_bytes(png(2461, 1440))          # a picture's bytes under a name that is no picture
    files = [{"name": p.name, "type": "image/png", "size": 10, "path": str(p), "ok": True} for p in (big, edge, doc)]
    text = message_text(NS(clean_content="?", content="?"), files).splitlines()
    assert text[1] == (f"[attachment] big.png, image/png, 10 bytes, saved at {big}, 2461x1440 px, shown to you shrunk to 2000: "
                       "zoom for small print")
    assert text[2:] == [f"[attachment] edge.png, image/png, 10 bytes, saved at {edge}",
                        f"[attachment] big.pdf, image/png, 10 bytes, saved at {doc}"]


def test_the_tool_is_in_every_rooms_allow_list_on_the_card_and_in_the_mechanics(tmp_path):
    y, _ = room(tmp_path)
    assert "mcp__yerk__zoom" in y.spec_for("example-room").settings["permissions"]["allow"]
    assert card.step("mcp__yerk__zoom", {"path": "/rooms/r/work/inbox/1001/IMG_0001.png"}) == "zooming in on IMG_0001"
    line = next(x for x in context.MECHANICS.read_text().splitlines() if "`zoom`" in x)
    assert all(w in line for w in ("2000 px", "a guess", "Read the file it names", "If Read refuses a picture, zoom the whole of it"))
