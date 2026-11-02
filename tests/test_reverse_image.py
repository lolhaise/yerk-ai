"""The reverse_image_search tool (reverse_image.py), offline: a fake Google answers in urlopen's place, so a picture sent to a
room is searched by its own bytes, the key in a header and never in the address, and Google's best guess, names, pages,
copies and look-alikes come back with one spend row; the wall the room met (no tool searched the web by picture) is now a
key the owner adds, said in one line with nothing sent; a path outside the room's folders, Google's refusal, a busy answer,
an answer with an error in it and an answer with nothing found are each said in words; a picture Google does not read as
it is, or one past what a request carries, goes as a smaller JPEG made by ffmpeg. The card and the allow list name it."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from yerk_ai import abilities, card, cli, reverse_image, words
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel

EXAMPLE_PRIVATE = Path(__file__).resolve().parents[1] / "examples" / "private"
FFMPEG = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg is not installed")
KEY = "AIza-placeholder-key-0001"
PHOTO = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(256)) * 4     # a jpeg by its first bytes, as the tool reads one
FOUND = {"responses": [{"webDetection": {
    "bestGuessLabels": [{"label": "harbour painting at dusk", "languageCode": "en"}],
    "webEntities": [{"entityId": "/m/0placeholder", "score": 0.8312, "description": "Example Painter"},
                    {"entityId": "/m/0other", "score": 0.41, "description": "Oil painting"}, {"entityId": "/m/0blank", "score": 0.2}],
    "pagesWithMatchingImages": [
        {"url": "https://gallery.example/works/harbour", "pageTitle": "<b>Harbour</b> at Dusk &amp; other works",
         "fullMatchingImages": [{"url": "https://gallery.example/img/harbour.jpg"}]},
        {"url": "https://blog.example/visit", "pageTitle": "A visit", "partialMatchingImages": [{"url": "https://blog.example/p.jpg"}]}],
    "fullMatchingImages": [{"url": "https://gallery.example/img/harbour.jpg"}],
    "partialMatchingImages": [{"url": "https://blog.example/p.jpg"}],
    "visuallySimilarImages": [{"url": "https://pics.example/1.jpg"}, {"url": "https://pics.example/2.jpg"}]}}]}


class Answer(io.BytesIO):
    def __enter__(self) -> Answer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def google(monkeypatch: pytest.MonkeyPatch, *answers: Any) -> list[urllib.request.Request]:
    """urlopen answered in order by `answers` (a dict is a 200 with that JSON, an HTTPError is raised); the requests it got."""
    got: list[urllib.request.Request] = []
    left = list(answers)

    def urlopen(req: urllib.request.Request, timeout: float = 0) -> Answer:
        got.append(req)
        a = left.pop(0)
        if isinstance(a, BaseException):
            raise a
        return Answer(json.dumps(a).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(reverse_image, "PAUSE", 0)
    return got


def refusal(code: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": code, "message": message, "status": "PERMISSION_DENIED"}}).encode()
    return urllib.error.HTTPError(reverse_image.URL, code, "refused", {}, io.BytesIO(body))   # type: ignore[arg-type]


def room(tmp_path: Path, key: str = KEY) -> tuple[Yerkd, Path]:
    """The example room on a private folder of its own (with the Vision key unless `key` is ""), and its inbox."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    if key:
        (private / "secrets").mkdir(mode=0o700)
        (private / reverse_image.KEY).write_text(key + "\n")
    (state := tmp_path / "state").mkdir(parents=True)
    y = Yerkd(private, state)
    y.add_room("example-room", ConsoleChannel())
    (inbox := state / "rooms" / "example-room" / "work" / "inbox" / "1001").mkdir(parents=True)
    return y, inbox


def ask(y: Yerkd, p: Path, name: str = "example-room") -> dict[str, Any]:
    return asyncio.run(y.tool_reverse_image_search(name, str(p)))


def spent(y: Yerkd) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in y.ledger.db.execute("SELECT room, tool, engine, usd, note FROM spend")]


def test_a_picture_sent_to_the_room_is_searched_by_its_bytes_with_the_key_in_a_header_and_one_spend_row(tmp_path, monkeypatch):
    y, inbox = room(tmp_path)
    (photo := inbox / "IMG_0001.jpg").write_bytes(PHOTO)
    got = google(monkeypatch, FOUND)
    r = ask(y, photo)
    assert r == {"ok": True, "picture": "IMG_0001.jpg", "best_guess": ["harbour painting at dusk"],
                 "names": [{"name": "Example Painter", "score": 0.83}, {"name": "Oil painting", "score": 0.41}],
                 "pages": [{"title": "Harbour at Dusk & other works", "url": "https://gallery.example/works/harbour", "shows": "the whole picture"},
                           {"title": "A visit", "url": "https://blog.example/visit", "shows": "part of it"}],
                 "copies": ["https://gallery.example/img/harbour.jpg"], "parts": ["https://blog.example/p.jpg"],
                 "look_alikes": ["https://pics.example/1.jpg", "https://pics.example/2.jpg"], "next": words.REVERSE_NEXT}
    [req] = got
    assert req.full_url == reverse_image.URL and KEY not in req.full_url and req.get_method() == "POST"
    assert req.get_header("X-goog-api-key") == KEY                             # a header: never in an address or a log
    body = json.loads(req.data)                                                # type: ignore[arg-type]
    assert body["requests"][0]["features"] == [{"type": "WEB_DETECTION", "maxResults": reverse_image.RESULTS}]
    assert base64.b64decode(body["requests"][0]["image"]["content"]) == PHOTO  # the bytes themselves: no public address made
    assert spent(y) == [("example-room", "reverse_image_search", "google-vision", 0.0035, "web detection, list price")]
    assert KEY not in json.dumps(r)


def test_with_no_key_the_room_hears_which_key_turns_it_on_and_nothing_is_sent(tmp_path, monkeypatch):
    """The wall a room met on 2026-10-26: asked who painted a photographed painting, it had only word searches, six of them
    found nothing, and no tool searched the web by picture. Now the tool is there in every room; with no key it says the
    one file that turns it on, for the owner, and sends nothing."""
    y, inbox = room(tmp_path, key="")
    (photo := inbox / "IMG_0002.jpg").write_bytes(PHOTO)
    got = google(monkeypatch)
    assert ask(y, photo) == {"ok": False, "error": words.REVERSE_NO_KEY}
    assert "secrets/google-vision.key" in words.REVERSE_NO_KEY and "yerk explain reverse-image-search" in words.REVERSE_NO_KEY
    assert got == [] and spent(y) == []
    assert "mcp__yerk__reverse_image_search" in y.spec_for("example-room").settings["permissions"]["allow"]   # every room's


def test_a_path_outside_the_rooms_folders_a_secret_or_no_file_is_refused_and_nothing_is_sent(tmp_path, monkeypatch):
    y, inbox = room(tmp_path)
    (other := tmp_path / "state" / "rooms" / "other-room" / "work").mkdir(parents=True)
    (theirs := other / "theirs.jpg").write_bytes(PHOTO)
    got = google(monkeypatch)
    assert ask(y, theirs) == {"ok": False, "error": words.REVERSE_NOT_HERE.format(name="theirs.jpg")}
    assert ask(y, tmp_path / "private" / reverse_image.KEY) == {"ok": False, "error": words.REVERSE_NOT_HERE.format(name="google-vision.key")}
    assert ask(y, inbox / "gone.jpg") == {"ok": False, "error": words.REVERSE_NO_FILE.format(name="gone.jpg")}
    assert ask(y, theirs, "no-such-room")["error"] == words.REVERSE_NOT_HERE.format(name="theirs.jpg")
    assert got == [] and spent(y) == []


def test_googles_refusal_is_said_in_its_own_words_without_the_key_tried_once_and_nothing_is_booked(tmp_path, monkeypatch):
    y, inbox = room(tmp_path)
    (photo := inbox / "IMG_0003.jpg").write_bytes(PHOTO)
    got = google(monkeypatch, refusal(403, f"Cloud Vision API has not been used in project 0000 before or it is disabled (key {KEY})."))
    r = ask(y, photo)
    assert r == {"ok": False, "error": words.REVERSE_FAILED.format(
        why="Cloud Vision API has not been used in project 0000 before or it is disabled (key <key>).")}
    assert len(got) == 1 and spent(y) == []


def test_a_busy_answer_is_tried_again_and_an_answer_with_an_error_in_it_books_nothing(tmp_path, monkeypatch):
    y, inbox = room(tmp_path)
    (photo := inbox / "IMG_0004.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
    got = google(monkeypatch, refusal(503, "The service is currently unavailable."), FOUND)
    assert ask(y, photo)["ok"] and len(got) == 2 and len(spent(y)) == 1
    google(monkeypatch, {"responses": [{"error": {"code": 3, "message": "Bad image data."}}]})
    assert ask(y, photo) == {"ok": False, "error": words.REVERSE_FAILED.format(why="Bad image data.")}
    assert len(spent(y)) == 1                                                  # the first call's row only


def test_nothing_found_is_said_with_why_a_photo_of_a_thing_finds_less(tmp_path, monkeypatch):
    y, inbox = room(tmp_path)
    (photo := inbox / "IMG_0005.jpg").write_bytes(PHOTO)
    google(monkeypatch, {"responses": [{"webDetection": {}}]})
    r = ask(y, photo)
    assert r["ok"] and r["next"] == words.REVERSE_NOTHING and r["pages"] == r["names"] == r["look_alikes"] == []
    assert len(spent(y)) == 1                                                  # Google answered: the call is billed


@FFMPEG
def test_a_picture_google_does_not_read_or_one_too_big_goes_as_a_smaller_jpeg_and_a_file_that_is_none_is_refused(tmp_path, monkeypatch):
    y, inbox = room(tmp_path)
    big = inbox / "scan.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=3000x1500", "-frames:v", "1", str(big)], check=True)
    monkeypatch.setattr(reverse_image, "MOST", 1000)                           # past what a request carries
    got = google(monkeypatch, FOUND)
    assert ask(y, big)["ok"]
    sent = tmp_path / "sent.jpg"
    sent.write_bytes(base64.b64decode(json.loads(got[0].data)["requests"][0]["image"]["content"]))   # type: ignore[arg-type]
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", str(sent)],
                           capture_output=True, text=True).stdout.split()
    assert probe == ["mjpeg,2048,1024"] and big.read_bytes()[:4] == b"\x89PNG"   # its long side at EDGE; the picture itself untouched
    (notes := inbox / "notes.heic").write_text("not a picture at all")       # not read as it is: ffmpeg is asked, and fails
    got = google(monkeypatch)
    r = ask(y, notes)
    assert not r["ok"] and r["error"].startswith("notes.heic did not read as a picture (") and got == []


def test_no_ffmpeg_for_a_picture_google_does_not_read_is_said_and_nothing_is_sent(tmp_path, monkeypatch):
    y, inbox = room(tmp_path)
    (photo := inbox / "IMG_0006.heic").write_bytes(b"\x00\x00\x00\x18ftypheic" + bytes(64))
    monkeypatch.setattr(reverse_image.shutil, "which", lambda name: None)
    got = google(monkeypatch)
    assert ask(y, photo) == {"ok": False, "error": words.REVERSE_NO_FFMPEG.format(name="IMG_0006.heic")} and got == []


def test_the_card_row_says_what_the_step_is_for_and_the_ability_is_on_with_its_key(tmp_path, capsys):
    assert card.step("mcp__yerk__reverse_image_search", {"path": "/rooms/r/work/inbox/1001/IMG_0001.jpg"}) == "finding IMG_0001 on the web"
    ab = abilities.manifest()["reverse-image-search"]
    assert ab["state"] == "built" and ab["tier"] == "key" and ab["keys"] == ["google-vision"]
    room(tmp_path / "with")                                                    # a private folder with the key
    for private, said in ((EXAMPLE_PRIVATE, words.ability("needs", provider=ab["provider"])), (tmp_path / "with" / "private", words.ability("on"))):
        assert cli.main(["explain", "reverse-image-search", "--private", str(private), "--state", str(tmp_path / "state")]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[:2] == [f"{said[0]} reverse-image-search: {said[1]}", ab["what"]]
        assert any(x.startswith("How to add the key: ") and "secrets/google-vision.key" in x for x in lines)
