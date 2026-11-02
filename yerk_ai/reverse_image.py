"""The reverse_image_search tool: where a picture is on the web, as Google Lens or TinEye finds it, since every search a room
has takes words and a picture of a thing (a painting, a product, a place) is often found by none of them. A picture in the
calling room's own folders (its inbox holds what was sent; web_image's pictures and watch's frames are in its work folder)
goes to Google Cloud Vision's web detection, the documented API of Google's search by picture: one images:annotate call
with the picture's bytes in it, so no picture is ever put at a public address for a search to fetch. Its answer is
Google's best guess at what the picture is, the names it ties to it (a work, its maker, a place), the pages that show it,
and the copies and look-alikes it found, every one off the web and so a record. It is a tool on Yerk's own MCP server
because the daemon has the network and a room's shell has none. The key is secrets/google-vision.key in the private
folder, its first line, read per call, sent in the x-goog-api-key header and never in an address, a log or a result. A
picture Vision does not read as it is (a HEIC or an AVIF) or one past what a request carries is made a JPEG of at most
EDGE pixels a side by ffmpeg first. Each call Google answers is one spend row at the list price (decision 32).
"""
from __future__ import annotations

import base64
import html
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import transcribe, words

URL = "https://vision.googleapis.com/v1/images:annotate"
KEY = "secrets/google-vision.key"
ENGINE = "google-vision"
PRICE = 0.0035               # US$ a picture: web detection past the first 1,000 a month, Cloud Vision's pricing, read 2026-11-02
MOST = 7_000_000             # bytes of picture sent as they are: a request is 10 MB of JSON at most, and base64 writes 4 for 3
EDGE = 2048                  # pixels: the longest side of a picture made smaller, more than web detection needs
RESULTS = 20                 # the most of each list Google returns (maxResults)
TIMEOUT, PAUSE = 60, 5       # seconds one request may take; the wait before a try again on a busy answer, times the try
BUSY = (429, 500, 502, 503, 504)
OWN = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*")   # jpeg png gif bmp tiff
TAG = re.compile(r"<[^>]+>")


class Failed(Exception):
    """A step that stops the call, in words a room can say."""


def readable(head: bytes) -> bool:
    """A picture Vision reads as it is, by its own first bytes: a jpeg, png, gif, webp, bmp or tiff."""
    return head.startswith(OWN) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


def smaller(picture: Path, into: Path) -> bytes:
    """The picture's first frame as a JPEG of at most EDGE pixels a side, by ffmpeg."""
    if not (exe := shutil.which("ffmpeg")):
        raise Failed(words.REVERSE_NO_FFMPEG.format(name=picture.name))
    out, fit = into / "picture.jpg", f"scale='min({EDGE},iw)':'min({EDGE},ih)':force_original_aspect_ratio=decrease"
    try:
        p = subprocess.run([exe, "-v", "error", "-y", "-i", str(picture), "-frames:v", "1", "-vf", fit, "-q:v", "3", str(out)],
                           capture_output=True, text=True, timeout=TIMEOUT)
        why = words.no_paths(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "")[:160]
    except subprocess.TimeoutExpired:
        why = words.WATCH_SLOW.format(seconds=TIMEOUT)
    if not out.is_file() or not out.stat().st_size:
        raise Failed(words.REVERSE_NOT_PICTURE.format(name=picture.name, why=why or words.WATCH_NO_WHY))
    return out.read_bytes()


def ask(secret: str, picture: bytes) -> dict[str, Any]:
    """One web detection request, three tries on a busy answer; Google's own words when it refuses, the key never in them."""
    body = json.dumps({"requests": [{"image": {"content": base64.b64encode(picture).decode()},
                                     "features": [{"type": "WEB_DETECTION", "maxResults": RESULTS}]}]}).encode()
    last = ""
    for attempt in range(1, 4):
        req = urllib.request.Request(URL, data=body, headers={"x-goog-api-key": secret, "Content-Type": "application/json",
                                                              "User-Agent": "yerk-ai"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return dict(json.loads(r.read()))
        except urllib.error.HTTPError as e:
            raw = e.read()[:2000].decode(errors="replace")
            try:
                last = str(json.loads(raw)["error"]["message"])
            except (ValueError, KeyError, TypeError):
                last = f"HTTP {e.code} {raw[:160]}"
            if e.code not in BUSY:
                break
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < 3:
            time.sleep(PAUSE * attempt)
    raise Failed(words.REVERSE_FAILED.format(why=" ".join(last.replace(secret, "<key>").split())[:300]))


def found(data: dict[str, Any]) -> dict[str, Any]:
    """Google's answer as the result's lists: its best guess, the names with their scores, each page with its title (tags
    out) and whether it shows the whole picture or a part, then the addresses of the copies and the look-alikes."""
    r = (data.get("responses") or [{}])[0]
    if err := r.get("error"):
        raise Failed(words.REVERSE_FAILED.format(why=" ".join(str(err.get("message") or err).split())[:300]))
    web = r.get("webDetection") or {}

    def urls(k: str, of: dict[str, Any] = web) -> list[str]:
        return [str(x["url"]) for x in of.get(k) or [] if isinstance(x, dict) and x.get("url")]
    return {"best_guess": [str(x["label"]) for x in web.get("bestGuessLabels") or [] if x.get("label")],
            "names": [{"name": str(x["description"]), "score": round(float(x.get("score") or 0), 2)}
                      for x in web.get("webEntities") or [] if x.get("description")],
            "pages": [{"title": html.unescape(TAG.sub("", str(p.get("pageTitle") or ""))).strip(), "url": str(p["url"]),
                       "shows": "the whole picture" if urls("fullMatchingImages", p) else "part of it"}
                      for p in web.get("pagesWithMatchingImages") or [] if p.get("url")],
            "copies": urls("fullMatchingImages"), "parts": urls("partialMatchingImages"), "look_alikes": urls("visuallySimilarImages")}


def run(picture: Path, key_file: Path, book: transcribe.Book) -> dict[str, Any]:
    """The tool's whole call, blocking (the daemon runs it off the loop): {"ok": False, "error"} on any stop, else what
    Google found and what to do with it, the call booked once Google answers it."""
    if not (secret := transcribe.key(key_file)):
        return {"ok": False, "error": words.REVERSE_NO_KEY}
    try:
        with picture.open("rb") as f:
            head = f.read(16)
        if readable(head) and picture.stat().st_size <= MOST:
            sent = picture.read_bytes()
        else:
            with tempfile.TemporaryDirectory(prefix="yerk-reverse-") as d:
                sent = smaller(picture, Path(d))
        out = found(ask(secret, sent))
    except Failed as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": words.REVERSE_NOT_PICTURE.format(name=picture.name, why=e.strerror or words.WATCH_NO_WHY)}
    book(ENGINE, PRICE, "web detection, list price")
    none = not any(out[k] for k in ("best_guess", "names", "pages", "copies", "parts", "look_alikes"))
    return {"ok": True, "picture": picture.name, **out, "next": words.REVERSE_NOTHING if none else words.REVERSE_NEXT}
