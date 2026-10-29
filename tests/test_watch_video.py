"""Checks the watch tool (watch.py), which turns a video link or a video file into frames and a transcript a room can read. Offline, a fake yt-dlp
put first on PATH copies a real four-second video, made here by ffmpeg, where the -o template says and prints its path, so a post's link comes back
as the video in the room's work folder under videos/<slug>/ with exactly N JPEG frames and its transcript, through a fake transcribe.run that writes
(no key, no network, no money). A word, a path or an ftp link is refused and the fake never runs; no yt-dlp on the machine,
a site that wants a login, a run that saves nothing and one that hangs past the timeout are each said in words; a video
with no sound has frames and no transcript; a second call on the same post pulls nothing and pays nothing; two calls at
once both finish, one after the other. (tests/test_watch.py is the channel a room watches, rooms.toml `watch`.)"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from yerk_ai import card, context, mcp_server, transcribe, watch, words
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel

EXAMPLE_PRIVATE = Path(__file__).resolve().parents[1] / "examples" / "private"
FFMPEG = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg and ffprobe are not installed")
REEL = "https://www.instagram.com/reel/Ab1_cD-2eF/?igsh=placeholder"
SAID = "[00:00:00 -> 00:00:04] a placeholder line over the test tone"
FRAME = re.compile(r"frame-\d\d-\d\dm\d\ds\.jpg")
# the fake: what it is told comes from knobs.json beside it, since the tool hands yt-dlp a clean environment
FAKE = '''
import json, os, shutil, subprocess, sys, time
from pathlib import Path

here = Path(__file__).parent
knobs = json.loads((here / "knobs.json").read_text())
args = sys.argv[1:]
with (here / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"argv": sys.argv, "env": dict(os.environ)}) + "\\n")
if knobs.get("hang"):
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (here / "kid").write_text(str(kid.pid))
    time.sleep(60)
try:
    os.close(os.open(here / "busy", os.O_CREAT | os.O_EXCL))
except FileExistsError:
    sys.exit("two downloads at once")
time.sleep(knobs.get("sleep", 0))
os.unlink(here / "busy")
if knobs.get("code") or knobs.get("nothing"):
    sys.stderr.write(knobs.get("said", ""))
    sys.exit(knobs.get("code", 0))
out = args[args.index("-o") + 1].replace("%(ext)s", "mp4")
shutil.copy(knobs["video"], out)
print(out)
'''


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """A four-second test picture with a tone under it, and the same picture with no sound."""
    if not shutil.which("ffmpeg"):
        return {}
    d, tone = tmp_path_factory.mktemp("clips"), ["-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-shortest"]
    for name, sound in (("sound", True), ("silent", False)):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
                        *(tone if sound else []), "-c:v", "mpeg4", *(["-c:a", "aac"] if sound else []), str(d / f"{name}.mp4")],
                       check=True, capture_output=True)
    return {name: str(d / f"{name}.mp4") for name in ("sound", "silent")}


def room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clips: dict[str, str], video: str = "sound",
         **knobs: object) -> tuple[Yerkd, Path, Path]:
    """The example room on a state of its own, its work folder made, and the fake yt-dlp first on PATH."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    (work := tmp_path / "rooms" / "example-room" / "work").mkdir(parents=True, exist_ok=True)
    (bin_ := tmp_path / "bin").mkdir()
    (fake := bin_ / "yt-dlp").write_text(f"#!{sys.executable}{FAKE}")
    fake.chmod(0o755)
    (bin_ / "knobs.json").write_text(json.dumps({"video": clips.get(video, ""), **knobs}))
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ.get('PATH', os.defpath)}")
    monkeypatch.setenv("LANG", "C.UTF-8")          # so the fake's own Python adds no locale of its own to what it logs
    return y, work, bin_


def runs(bin_: Path) -> list[dict[str, Any]]:
    """What the fake was run with, a line a run; none when it never ran."""
    f = bin_ / "runs.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.is_file() else []


def hearing(monkeypatch: pytest.MonkeyPatch, error: str = "", gated: bool = False) -> list[dict[str, Any]]:
    """transcribe.run as a fake, each call kept: it books one spend row, writes video.transcript.txt beside the file and
    answers in transcribe's shape; with `error`, it answers that instead (`gated`: until the owner's yes)."""
    calls: list[dict[str, Any]] = []

    def fake(audio: Path, openai_key: Path, groq_key: Path, book: transcribe.Book, owner_yes: bool = False,
             meta_key: Path | None = None) -> dict[str, Any]:
        calls.append({"audio": audio, "keys": (openai_key, groq_key, meta_key), "owner_yes": owner_yes})
        if error and not (gated and owner_yes):
            return {"ok": False, "error": error}
        book("openai/whisper-1", 0.0004, "chunk 1 of 1, 4 s")
        head = words.TRANSCRIPT_HEAD.format(name=audio.name, minutes=0.1, how=words.UNCHECKED)
        (out := audio.with_name(f"{audio.stem}.transcript.txt")).write_text(f"{head}\n{SAID}\n")
        return {"ok": True, "file": str(out), "about": head, "uncertain": 0, "segments": 1, "text": SAID, "speakers": {},
                "speakers_how": words.ONE_VOICE, "next": words.TRANSCRIBED_NEXT}
    monkeypatch.setattr(transcribe, "run", fake)
    return calls


def ask(y: Yerkd, url: str, frames: int = 6, owner_yes: bool = False) -> dict[str, Any]:
    return asyncio.run(y.tool_watch("example-room", url, frames, owner_yes))


def frames_in(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob("frame-*.jpg"))


def test_the_tool_is_preloaded_allowed_in_every_room_and_the_mechanics_send_a_video_link_to_it(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    for name in ("example-room", "example-quiet"):
        y.add_room(name, ConsoleChannel())
        assert "mcp__yerk__watch" in y.spec_for(name).settings["permissions"]["allow"], name
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert list(tools["watch"].inputSchema["properties"]) == ["url", "frames", "owner_yes"]
    said = " ".join(str(tools["watch"].description).split())
    assert all(w in said for w in ("an Instagram reel or post, a TikTok, an X post, a YouTube link", "under videos/", "1 to 12",
                                   "open the frames with Read", "are not the video", "`owner_yes` true", "not pulled or transcribed"))
    line = next(x for x in context.MECHANICS.read_text().splitlines() if "`watch`" in x)
    assert "open them with Read" in line and "are not the video" in line and "says so" in line
    assert card.step("mcp__yerk__watch", {"url": REEL, "frames": 6}) == "watching a video on www.instagram.com"   # the host, never the link


@FFMPEG
def test_a_link_comes_back_as_its_video_its_frames_and_its_transcript_in_the_rooms_work_folder(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    calls, rows = hearing(monkeypatch), []
    monkeypatch.setattr(y.ledger, "spend", lambda **row: rows.append(row) or 1)
    monkeypatch.setenv("YERK_WATCH_PROBE", "the daemon's own")
    r = ask(y, REEL)
    folder = work / "videos" / "instagram-Ab1_cD-2eF"
    assert r == {"ok": True, "slug": "instagram-Ab1_cD-2eF", "folder": str(folder), "video": str(folder / "video.mp4"),
                 "seconds": r["seconds"], "frames": r["frames"], "transcript": r["transcript"],
                 "transcript_file": str(folder / "video.transcript.txt"), "reused": False, "note": ""}
    assert abs(r["seconds"] - 4) < 0.5 and r["seconds"] == round(r["seconds"], 1)
    assert [Path(f["file"]).name for f in r["frames"]] == frames_in(folder) and len(r["frames"]) == 6
    assert all(FRAME.fullmatch(Path(f["file"]).name) and Path(f["file"]).read_bytes()[:3] == b"\xff\xd8\xff" for f in r["frames"])
    assert [f["at"] for f in r["frames"]] == ["0m00s", "0m01s", "0m01s", "0m02s", "0m03s", "0m03s"]     # 4 s * (i + 0.5) / 6
    assert frames_in(folder)[0] == "frame-01-00m00s.jpg" and frames_in(folder)[-1] == "frame-06-00m03s.jpg"
    assert SAID in r["transcript"] and r["transcript"] == (folder / "video.transcript.txt").read_text().strip()
    assert (folder.stat().st_mode & 0o777, (folder / "url.txt").read_text()) == (0o700, REEL + "\n")
    [run] = runs(bin_)
    assert run["argv"] == [str(bin_ / "yt-dlp"), "--no-playlist", "--no-warnings", "--no-progress", "--restrict-filenames",
                           "--max-filesize", "300M", "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
                           "-o", f"{folder}/video.%(ext)s", "--print", "after_move:filepath", "--no-simulate", REEL]
    assert run["env"] == {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": os.environ["HOME"]}
    [call] = calls                                    # transcribe.run as tool_transcribe calls it, its spend rows under `watch`
    assert call == {"audio": folder / "video.mp4", "owner_yes": False,
                    "keys": (y.memory.embeddings_key, y.private / transcribe.GROQ_KEY, y.private / transcribe.META_KEY)}
    assert rows == [{"room": "example-room", "tool": "watch", "engine": "openai/whisper-1", "usd": 0.0004, "note": "chunk 1 of 1, 4 s"}]


@FFMPEG
def test_the_slug_is_the_site_and_the_posts_own_id(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    hearing(monkeypatch)
    bare = "https://example.org/"
    for url, slug in (("https://x.com/example_user/status/1234567890", "x-1234567890"),
                      (bare, "example-" + hashlib.sha1(bare.encode()).hexdigest()[:10]),
                      ("https://www.youtube.com/watch?v=AbC123xyz_0&t=30s", "youtube-AbC123xyz_0")):   # a watch link's video
        r = ask(y, url, frames=1)
        assert (r["slug"], r["video"]) == (slug, str(work / "videos" / slug / "video.mp4")), url
    assert len(runs(bin_)) == 3
    assert watch.slug("https://www.tiktok.com/v/" + "a.b-c_d" * 10) == "tiktok-" + ("ab-c_d" * 10)[:48]


@FFMPEG
def test_another_post_whose_slug_is_the_same_gets_a_folder_of_its_own(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    hearing(monkeypatch)
    first, second = "https://x.com/example_user/status/1111/video/1", "https://x.com/other_user/status/2222/video/1"
    assert ask(y, first, frames=1)["slug"] == "x-1"
    r = ask(y, second, frames=1)
    assert r["slug"] == "x-1-" + hashlib.sha1(second.encode()).hexdigest()[:10] and not r["reused"] and len(runs(bin_)) == 2
    assert ask(y, second, frames=1)["reused"] and ask(y, first + "?s=20", frames=1)["slug"] == "x-1" and len(runs(bin_)) == 2


@FFMPEG
def test_a_link_the_rooms_shell_left_in_its_folder_is_never_followed(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    calls = hearing(monkeypatch)
    (secret := tmp_path / "secret.txt").write_text("a placeholder secret\n")
    (folder := work / "videos" / "instagram-Ab1_cD-2eF").mkdir(parents=True)
    (folder / "video.mp4").symlink_to(clips["sound"])              # a video "pulled before" that is a file of somewhere else
    (folder / "video.transcript.txt").symlink_to(secret)           # and a transcript that is a file outside the room
    r = ask(y, REEL)
    assert r["ok"] and not r["reused"] and len(runs(bin_)) == 1 and len(calls) == 1
    assert SAID in r["transcript"] and "placeholder secret" not in r["transcript"]
    assert not any(p.is_symlink() for p in folder.iterdir()) and secret.read_text() == "a placeholder secret\n"
    shutil.rmtree(work / "videos")
    (elsewhere := tmp_path / "elsewhere").mkdir()
    (work / "videos").symlink_to(elsewhere)                         # the whole videos folder a link
    assert ask(y, REEL) == {"ok": False, "error": words.WATCH_LINKED.format(name="videos")}
    assert list(elsewhere.iterdir()) == [] and len(runs(bin_)) == 1


def test_not_a_link_is_refused_in_words_and_nothing_runs(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    for given, said in (("reel", "reel"), (str(work / "video.mp4"), "video.mp4"), ("ftp://example.org/clip.mp4", "ftp://example.org/clip.mp4"),
                        ("https://", "https://"), ("", "")):
        assert ask(y, given) == {"ok": False, "error": words.WATCH_NOT_LINK.format(given=said)}, given
    assert asyncio.run(y.tool_watch("no-such-room", REEL)) == {"ok": False, "error": words.WATCH_NO_ROOM.format(room="no-such-room")}
    assert runs(bin_) == [] and not (work / "videos").exists()


def test_no_yt_dlp_on_the_machine_is_said_in_words(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    (empty := tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert ask(y, REEL) == {"ok": False, "error": words.WATCH_NO_YTDLP}
    assert runs(bin_) == []


def test_a_site_that_wants_a_login_is_said_in_its_own_words(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, code=1, said="[instagram] abc: Extracting URL\nERROR: [Instagram] abc: login required\n")
    r = ask(y, REEL)
    assert r == {"ok": False, "error": words.WATCH_FAILED.format(why="[Instagram] abc: login required")}
    assert "login required" in r["error"] and "ERROR: " not in r["error"]
    (bin_ / "knobs.json").write_text(json.dumps({"code": 1, "said": "ERROR: [Instagram] abc: Requested content is not available, "
                                                 "rate-limit reached or login required. Use --cookies, --cookies-from-browser, "
                                                 "--username and --password, --netrc-cmd, or --netrc (instagram) to provide "
                                                 "account credentials\n"}))
    assert ask(y, REEL)["error"] == words.WATCH_FAILED.format(why="[Instagram] abc: Requested content is not available, "
                                                              "rate-limit reached or login required.")   # never its advice on flags
    (bin_ / "knobs.json").write_text(json.dumps({"code": 1}))
    assert ask(y, REEL) == {"ok": False, "error": words.WATCH_NOTHING}
    assert watch.saved(work / "videos" / "instagram-Ab1_cD-2eF") is None


def test_a_run_that_saves_nothing_is_said_in_words(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, nothing=True)          # what --max-filesize does to a video past it
    assert ask(y, REEL) == {"ok": False, "error": words.WATCH_NOTHING}
    assert len(runs(bin_)) == 1 and watch.saved(work / "videos" / "instagram-Ab1_cD-2eF") is None


def test_a_download_that_hangs_is_killed_with_its_group_at_the_timeout(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, hang=True)
    monkeypatch.setattr(watch, "WATCH_TIMEOUT", 2)
    t0 = time.monotonic()
    r = ask(y, REEL)
    assert r == {"ok": False, "error": words.WATCH_TIMED_OUT.format(seconds=2)} and time.monotonic() - t0 < 30
    kid = int((bin_ / "kid").read_text())                    # the fake's own child: only a group kill stops it
    for _ in range(50):
        try:
            os.kill(kid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(kid, 9)
        pytest.fail("the hanging fake's child outlived the timeout")
    assert list((work / "videos" / "instagram-Ab1_cD-2eF").glob("video.*")) == []


@FFMPEG
def test_a_video_with_no_sound_has_its_frames_and_no_transcript(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, video="silent")
    calls = hearing(monkeypatch)
    r = ask(y, REEL)
    assert r["ok"] and len(r["frames"]) == 6 and (r["transcript"], r["transcript_file"], r["note"]) == ("", "", words.WATCH_NO_SOUND)
    assert calls == []


@FFMPEG
def test_a_second_call_on_the_same_post_pulls_nothing_pays_nothing_and_makes_the_frames_again(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    calls = hearing(monkeypatch)
    first = ask(y, REEL)
    folder = Path(first["folder"])
    assert len(frames_in(folder)) == 6 and not first["reused"]
    again = ask(y, "https://instagram.com/reel/Ab1_cD-2eF?igsh=another-share", frames=3)      # the same post, shared again
    assert again["reused"] and again["transcript"] == first["transcript"] and again["transcript_file"] == first["transcript_file"]
    assert len(runs(bin_)) == 1 and len(calls) == 1
    assert [Path(f["file"]).name for f in again["frames"]] == frames_in(folder) and len(frames_in(folder)) == 3


@FFMPEG
def test_the_frames_asked_for_are_kept_to_1_to_12(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    hearing(monkeypatch)
    assert len(ask(y, REEL, frames=0)["frames"]) == 1
    r = ask(y, REEL, frames=40)
    assert len(r["frames"]) == 12 and len(frames_in(Path(r["folder"]))) == 12 and len({f["file"] for f in r["frames"]}) == 12


@FFMPEG
def test_a_transcript_that_fails_still_gives_the_frames_and_the_note_says_why(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    calls = hearing(monkeypatch, error=words.NO_SPEECH_KEY)
    r = ask(y, REEL)
    assert r["ok"] and len(r["frames"]) == 6 and r["transcript"] == "" and r["transcript_file"] == ""
    assert words.NO_SPEECH_KEY in r["note"] and r["note"] == words.WATCH_NO_TRANSCRIPT.format(why=words.NO_SPEECH_KEY) and len(calls) == 1


@FFMPEG
def test_a_transcript_past_the_gate_waits_for_the_owners_yes_and_the_video_is_not_pulled_again(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    gate = words.OVER_GATE.format(minutes=180, usd=1.08)
    calls = hearing(monkeypatch, error=gate, gated=True)
    r = ask(y, REEL)
    assert r["ok"] and r["note"] == words.WATCH_NO_TRANSCRIPT.format(why=gate) and r["transcript"] == "" and len(r["frames"]) == 6
    yes = ask(y, REEL, owner_yes=True)
    assert yes["reused"] and SAID in yes["transcript"] and yes["note"] == "" and [c["owner_yes"] for c in calls] == [False, True]
    assert len(runs(bin_)) == 1


@FFMPEG
def test_two_calls_at_once_both_finish_one_after_the_other(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, sleep=0.5)
    hearing(monkeypatch)

    async def both(urls: list[str]) -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(y.tool_watch("example-room", u, 1) for u in urls)))
    done = asyncio.run(both(["https://x.com/example_user/status/1001", "https://x.com/example_user/status/1002"]))
    assert [(r["ok"], r["slug"], r["reused"]) for r in done] == [(True, "x-1001", False), (True, "x-1002", False)]
    monkeypatch.setattr(watch, "ONE", contextlib.nullcontext())       # without the lock the fake sees the two at once
    [lost] = [r for r in asyncio.run(both(["https://x.com/example_user/status/1003", "https://x.com/example_user/status/1004"]))
              if not r["ok"]]
    assert lost["error"] == words.WATCH_FAILED.format(why="two downloads at once")
