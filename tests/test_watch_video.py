"""Checks the watch tool (watch.py), which turns a video link or a video file into frames and a transcript a room can read. Offline, a fake yt-dlp
put first on PATH copies a real four-second video, made here by ffmpeg, where the -o template says and prints its path, so a post's link comes back
as the video in the room's work folder under videos/<slug>/ with exactly N JPEG frames and its transcript, through a fake transcribe.run that writes
the file beside it (no key, no network, no money). A word, a path or an ftp link is refused and the fake never runs; no yt-dlp on the machine,
a site that wants a login, a run that saves nothing and one that hangs past the timeout are each said in words; a video
with no sound has frames and no transcript; a second call on the same post pulls nothing and pays nothing; two calls at
once both finish, one after the other. The same fake answers the captions step by its argv: `-J` prints the page knobs.json
holds, a track's run writes its json3, so a page's own captions are the transcript and nothing is paid, whichever track
is chosen, and a page with none, a failed probe or a video too big to save each go the way they should. A video file sent
to the room (`path`) comes back the same way with nothing pulled and the fake never run, its frames under videos/ and its
transcript beside it; a path outside the room's folders, a missing file or a link is refused; the [attachment] line says
to watch it, and the Stop hook bounces a turn that never did, since only a watch call sees a video. A Discord message's
link is read through the adapter on a fake gateway: its video saved to the room's inbox and watched as a file, the fake
never run, only where the room reads (an owner's room anywhere in its own server), and every miss said in words.
(tests/test_watch.py is the channel a room watches, rooms.toml `watch`.)"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import discord
import pytest

from yerk_ai import captions, card, context, mcp_server, shape, transcribe, watch, words
from yerk_ai.channels.discord import DiscordChannel, message_text
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import RoomProcess
from yerk_ai.room import Inbound, Room
from fake_channel import ConsoleChannel
from test_discord import Attachment

EXAMPLE_PRIVATE = Path(__file__).resolve().parents[1] / "examples" / "private"
FFMPEG = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg and ffprobe are not installed")
REEL = "https://www.instagram.com/reel/Ab1_cD-2eF/?igsh=placeholder"
CLIP = "https://videos.example.com/clip/abc"           # a placeholder site that is not Instagram
SAID = "[00:00:00 -> 00:00:04] a placeholder line over the test tone"
TALK = "https://www.youtube.com/watch?v=AbC123xyz_0"
TRACK = [{"ext": "json3"}, {"ext": "vtt"}]
CUES = {"events": [{"tStartMs": 0, "dDurationMs": 60000, "wpWinPosId": 1},             # a styling event, no words
                   {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "a placeholder"}, {"utf8": "  line"}]},
                   {"tStartMs": 20000, "dDurationMs": 2500, "segs": [{"utf8": "and a second one"}]}]}
LINES = ["[00:00:01 -> 00:00:03] a placeholder line", "[00:00:20 -> 00:00:22] and a second one"]
FRAME = re.compile(r"frame-\d\d-\d\dm\d\ds\.jpg")
INBOX = "m-1"                              # a placeholder message id: the inbox folder a file sent with it is saved in
# the fake: what it is told comes from knobs.json beside it, since the tool hands yt-dlp a clean environment
FAKE = '''
import json, os, shutil, subprocess, sys, time
from pathlib import Path

here = Path(__file__).parent
knobs = json.loads((here / "knobs.json").read_text())
args = sys.argv[1:]
with (here / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"argv": sys.argv, "env": dict(os.environ)}) + "\\n")
if "-J" in args:                              # the probe: the page knobs.json holds, one with no captions unless told
    print(json.dumps(knobs.get("info", {})))
    sys.stderr.write(knobs.get("info_said", ""))
    sys.exit(knobs.get("info_code", 0))
if "--write-subs" in args:                    # one track, read from the probe's own answer, which must still be there
    if json.loads(Path(args[args.index("--load-info-json") + 1]).read_text()) != knobs.get("info", {}):
        sys.exit("ERROR: not the probe's answer")
    if not knobs.get("no_track_file"):
        Path(f"{args[args.index('-o') + 1]}.{args[args.index('--sub-langs') + 1]}.json3").write_text(json.dumps(knobs["json3"]))
    print("[info] Writing video subtitles")
    sys.exit(0)
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


def kinds(bin_: Path) -> list[str]:
    """Each run of the fake by what it did: the -J probe, one caption track, or a pull of the video."""
    return ["probe" if "-J" in r["argv"] else "captions" if "--write-subs" in r["argv"] else "pull" for r in runs(bin_)]


def page(manual: tuple[str, ...] = (), auto: tuple[str, ...] = (), **more: object) -> dict[str, Any]:
    """A -J answer: a placeholder talk four seconds long, offering json3 and vtt under each caption key given."""
    return {"title": "a placeholder talk", "duration": 4, "language": "en", "live_status": "not_live",
            "subtitles": {k: TRACK for k in manual}, "automatic_captions": {k: TRACK for k in auto}, **more}


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


def sent(work: Path, clips: dict[str, str], video: str = "sound", name: str = "clip.mov", mid: str = INBOX) -> Path:
    """A video file as a room is sent one: saved in its inbox under the message's id (the adapter's stage step)."""
    (box := work / "inbox" / mid).mkdir(parents=True, exist_ok=True)
    shutil.copy(clips[video], box / name)
    return (box / name).resolve()


def look(y: Yerkd, path: Path, frames: int = 6, url: str = "") -> dict[str, Any]:
    """The watch tool on a file's saved path, as a room calls it."""
    return asyncio.run(y.tool_watch("example-room", url, frames, False, str(path)))


def frames_of(video: Path) -> str:
    """The folder a file's frames go in: its name, then 10 hex of its path's sha1."""
    return f"file-{video.stem}-{hashlib.sha1(str(video).encode()).hexdigest()[:10]}"


def test_the_tool_is_preloaded_allowed_in_every_room_and_the_mechanics_send_a_video_link_to_it(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    for name in ("example-room", "example-quiet"):
        y.add_room(name, ConsoleChannel())
        assert "mcp__yerk__watch" in y.spec_for(name).settings["permissions"]["allow"], name
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert list(tools["watch"].inputSchema["properties"]) == ["url", "path", "frames", "owner_yes"]
    assert not tools["watch"].inputSchema.get("required")                    # a link or a file's path, either one
    said = " ".join(str(tools["watch"].description).split())
    assert all(w in said for w in ("an Instagram reel or post, a TikTok, an X post, a YouTube link", "under videos/", "1 to 12",
                                   "open the frames with Read", "are not the video", "`owner_yes` true", "not pulled or transcribed",
                                   "the video's own captions when the page has them", "free and with their timestamps",
                                   "only a video with no captions goes to the paid transcription", "A video file sent to you, which "
                                   "Read cannot open, is watched the same way from `path`", "[attachment] line", "nothing is pulled"))
    line = next(x for x in context.MECHANICS.read_text().splitlines() if "`watch`" in x)
    assert "open them with Read" in line and "are not the video" in line and "says so" in line
    assert "the video's own captions when it has them, free" in line and "without captions is transcribed from its sound" in line
    assert "(a video file sent to you: on its `path`, since Read cannot open one)" in line and shape.offenses(line) == []
    assert card.step("mcp__yerk__watch", {"url": REEL, "frames": 6}) == "watching a video on www.instagram.com"   # the host, never the link
    assert card.step("mcp__yerk__watch", {"path": f"/s/inbox/{INBOX}/clip.mov"}) == "watching the video clip"   # its name, never its path


@FFMPEG
def test_a_link_comes_back_as_its_video_its_frames_and_its_transcript_in_the_rooms_work_folder(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, info=page())       # a page with no captions: its sound is transcribed
    calls, rows = hearing(monkeypatch), []
    monkeypatch.setattr(y.ledger, "spend", lambda **row: rows.append(row) or 1)
    monkeypatch.setenv("YERK_WATCH_PROBE", "the daemon's own")
    r = ask(y, REEL)
    folder = work / "videos" / "instagram-Ab1_cD-2eF"
    assert r == {"ok": True, "slug": "instagram-Ab1_cD-2eF", "folder": str(folder), "title": "a placeholder talk",
                 "video": str(folder / "video.mp4"), "seconds": r["seconds"], "frames": r["frames"], "transcript": r["transcript"],
                 "transcript_file": str(folder / "video.transcript.txt"), "source": "heard", "reused": False,
                 "note": words.WATCH_NO_CAPTIONS}
    assert abs(r["seconds"] - 4) < 0.5 and r["seconds"] == round(r["seconds"], 1)
    assert [Path(f["file"]).name for f in r["frames"]] == frames_in(folder) and len(r["frames"]) == 6
    assert all(FRAME.fullmatch(Path(f["file"]).name) and Path(f["file"]).read_bytes()[:3] == b"\xff\xd8\xff" for f in r["frames"])
    assert [f["at"] for f in r["frames"]] == ["0m00s", "0m01s", "0m01s", "0m02s", "0m03s", "0m03s"]     # 4 s * (i + 0.5) / 6
    assert frames_in(folder)[0] == "frame-01-00m00s.jpg" and frames_in(folder)[-1] == "frame-06-00m03s.jpg"
    assert SAID in r["transcript"] and r["transcript"] == (folder / "video.transcript.txt").read_text().strip()
    assert (folder.stat().st_mode & 0o777, (folder / "url.txt").read_text()) == (0o700, REEL + "\n")
    probe, run = runs(bin_)
    assert probe["argv"] == [str(bin_ / "yt-dlp"), "-J", "--no-playlist", "--no-warnings", "--skip-download", REEL]
    assert probe["env"] == {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": os.environ["HOME"]}
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
    assert kinds(bin_) == ["probe", "pull"] * 3
    assert watch.slug("https://www.tiktok.com/v/" + "a.b-c_d" * 10) == "tiktok-" + ("ab-c_d" * 10)[:48]


@FFMPEG
def test_another_post_whose_slug_is_the_same_gets_a_folder_of_its_own(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    hearing(monkeypatch)
    first, second = "https://x.com/example_user/status/1111/video/1", "https://x.com/other_user/status/2222/video/1"
    assert ask(y, first, frames=1)["slug"] == "x-1"
    r = ask(y, second, frames=1)
    assert r["slug"] == "x-1-" + hashlib.sha1(second.encode()).hexdigest()[:10] and not r["reused"] and kinds(bin_).count("pull") == 2
    assert ask(y, second, frames=1)["reused"] and ask(y, first + "?s=20", frames=1)["slug"] == "x-1" and kinds(bin_).count("pull") == 2


@FFMPEG
def test_a_link_the_rooms_shell_left_in_its_folder_is_never_followed(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    calls = hearing(monkeypatch)
    (secret := tmp_path / "secret.txt").write_text("a placeholder secret\n")
    (folder := work / "videos" / "instagram-Ab1_cD-2eF").mkdir(parents=True)
    (folder / "video.mp4").symlink_to(clips["sound"])              # a video "pulled before" that is a file of somewhere else
    (folder / "video.transcript.txt").symlink_to(secret)           # and a transcript that is a file outside the room
    r = ask(y, REEL)
    assert r["ok"] and not r["reused"] and kinds(bin_) == ["probe", "pull"] and len(calls) == 1
    assert SAID in r["transcript"] and "placeholder secret" not in r["transcript"]
    assert not any(p.is_symlink() for p in folder.iterdir()) and secret.read_text() == "a placeholder secret\n"
    shutil.rmtree(work / "videos")
    (elsewhere := tmp_path / "elsewhere").mkdir()
    (work / "videos").symlink_to(elsewhere)                         # the whole videos folder a link
    assert ask(y, REEL) == {"ok": False, "error": words.WATCH_LINKED.format(name="videos")}
    assert list(elsewhere.iterdir()) == [] and kinds(bin_) == ["probe", "pull"]


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
    """A site other than Instagram, whose posts no browser of the room's reads for watch (need 111 is Instagram's)."""
    y, work, bin_ = room(tmp_path, monkeypatch, clips, code=1, said="[example] abc: Extracting URL\nERROR: [Example] abc: login required\n")
    r = ask(y, CLIP)
    assert r == {"ok": False, "error": words.WATCH_FAILED.format(why="[Example] abc: login required")}
    assert "login required" in r["error"] and "ERROR: " not in r["error"]
    (bin_ / "knobs.json").write_text(json.dumps({"code": 1, "said": "ERROR: [Example] abc: Requested content is not available, "
                                                 "rate-limit reached or login required. Use --cookies, --cookies-from-browser, "
                                                 "--username and --password, --netrc-cmd, or --netrc (example) to provide "
                                                 "account credentials\n"}))
    assert ask(y, CLIP)["error"] == words.WATCH_FAILED.format(why="[Example] abc: Requested content is not available, "
                                                              "rate-limit reached or login required.")   # never its advice on flags
    (bin_ / "knobs.json").write_text(json.dumps({"code": 1}))
    assert ask(y, CLIP) == {"ok": False, "error": words.WATCH_NOTHING}
    assert ask(y, REEL) == {"ok": False, "error": words.WATCH_NOTHING}                    # Instagram's, with no words, as before
    assert watch.saved(work / "videos" / "example-abc") is None and watch.saved(work / "videos" / "instagram-Ab1_cD-2eF") is None


# yt-dlp's own words (2026.08.19) when Instagram shuts a post to a logged-out reader, the wall need 111 hit twice
EMPTY = ("ERROR: [Instagram] Ab1_cD-2eF: Instagram sent an empty media response. Check if this post is accessible in your browser "
         "without being logged-in. If it is not, then use --cookies-from-browser or --cookies for the authentication. See  "
         "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies. Otherwise, "
         "if the post is accessible in browser without being logged-in, please report this issue on  "
         "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template. Confirm you are on the latest "
         "version using  yt-dlp -U\n")


def test_an_instagram_post_shut_to_a_logged_out_reader_names_the_signed_in_browsers_way(tmp_path, monkeypatch, clips):
    """need-2026-11-18-111: a room sent a reel to watch met "Instagram sent an empty media response" twice (the account
    limits who sees it, and watch reads logged out, as yt-dlp does), then the signed-in browser's background tab painted a
    black frame and no sound reached the room. The wall is said with the way: a room with an add-on's browser tool hears
    its video action, which saves the post's video signed in (the hands plug-in's), then watch on that path; a room with
    none hears that the video was not seen and is never answered off its caption; yt-dlp's advice on cookies flags never
    reaches a room, and nothing is saved or retried."""
    y, work, bin_ = room(tmp_path, monkeypatch, clips, code=1, said=EMPTY, info_code=1, info_said=EMPTY)
    said = "[Instagram] Ab1_cD-2eF: Instagram sent an empty media response"
    r = ask(y, REEL)
    assert r == {"ok": False, "error": words.WATCH_SHUT.format(why=said, way=words.WATCH_SHUT_NO_BROWSER)}
    assert "cookies" not in r["error"] and "github" not in r["error"] and "not seen" in r["error"]
    assert kinds(bin_) == ["probe", "pull"] and watch.saved(work / "videos" / "instagram-Ab1_cD-2eF") is None
    # an owner room with the browser add-on (a placeholder server's browser tool in its allow and its mcp tables)
    conf = y.room_confs["example-room"]
    y.room_confs["example-room"] = {**conf, "allow": ["mcp__example-hands__browser"], "mcp": {"example-hands": {"type": "stdio", "command": "node"}}}
    r = ask(y, REEL)
    assert r == {"ok": False, "error": words.WATCH_SHUT.format(why=said, way=words.WATCH_SHUT_BROWSER)}
    assert "video action on this link" in r["error"] and "watch that path" in r["error"]
    # each of yt-dlp's other words for a post shut to it, on each form of an Instagram link
    for err in ("ERROR: [Instagram] Ab1_cD-2eF: This content is only available for registered users who follow this account. Use --cookies",
                "ERROR: [Instagram] Ab1_cD-2eF: Requested content is not available, rate-limit reached or login required. Use --cookies",
                "ERROR: [Instagram] Ab1_cD-2eF: The webpage request was redirected to the login page. You have exceeded the rate-limit",
                "ERROR: [Instagram] Ab1_cD-2eF: Restricted Video: You must be 18 years old or over to see this video. Use --cookies"):
        (bin_ / "knobs.json").write_text(json.dumps({"code": 1, "said": err + "\n", "info_code": 1}))
        for link in (REEL, "https://instagram.com/p/Ab1_cD-2eF/", "https://instagr.am/p/Ab1_cD-2eF/"):
            got = ask(y, link)
            assert got["error"].startswith("no video was pulled from that link: Instagram shuts this post to a logged-out reader, "
                                           "as watch is ([Instagram] Ab1_cD-2eF: "), (err, link, got)
            assert got["error"].endswith(words.WATCH_SHUT_BROWSER) and "--" not in got["error"] and "shut" not in got, got
    # Instagram's other words (a post with no video in it), and the same words on another site, stay as they were
    (bin_ / "knobs.json").write_text(json.dumps({"code": 1, "said": "ERROR: [Instagram] Ab1_cD-2eF: There is no video in this post\n"}))
    assert ask(y, REEL) == {"ok": False, "error": words.WATCH_FAILED.format(why="[Instagram] Ab1_cD-2eF: There is no video in this post")}
    (bin_ / "knobs.json").write_text(json.dumps({"code": 1, "said": "ERROR: [Example] abc: login required\n"}))
    assert ask(y, CLIP) == {"ok": False, "error": words.WATCH_FAILED.format(why="[Example] abc: login required")}


def test_a_room_browses_only_with_an_add_ons_browser_tool_its_allow_names_and_its_mcp_tables_hold():
    """watch.browses, the room's tools as the daemon reads them: an add-on server's browser tool named in allow, its server
    in the room's mcp tables (an owner room's [defaults] merged in); never a tool alone, a server alone or another tool."""
    hands = {"mcp": {"example-hands": {"command": "node"}}}
    assert watch.browses({**hands, "allow": ["mcp__example-hands__browser"]})
    assert watch.browses({**hands, "allow": ["mcp__example-hands__mail", "mcp__example-hands__browser"]})
    for conf in ({**hands, "allow": ["mcp__example-hands__mail"]}, {**hands, "allow": ["mcp__other__browser"]}, {"allow": ["mcp__example-hands__browser"]},
                 {**hands, "allow": ["mcp__example-hands"]}, {**hands}, {}):
        assert not watch.browses(conf), conf
    assert watch.shut(REEL, "Instagram sent an empty media response") and not watch.shut(CLIP, "Instagram sent an empty media response")
    assert not watch.shut("https://notinstagram.com/p/x/", "login required") and not watch.shut(REEL, "There is no video in this post")


def test_an_instagram_story_shut_to_a_logged_out_reader_is_said_in_yt_dlps_words_never_the_browsers_way(tmp_path, monkeypatch, clips):
    """PR 102's cold read: "This content is unreachable" and "You need to log in to access this content" are the words of
    yt-dlp's story reader (2026.08.19), for a story or a highlight, a link the hands plug-in's video action refuses (it
    reads a post, a reel or a tv link), so a room with that browser sent a story heard its video action named and was sent
    back to watch by it. Only the links that action takes are shut; a story is said in yt-dlp's own words, as before."""
    y, work, bin_ = room(tmp_path, monkeypatch, clips, code=1, info_code=1)
    conf = y.room_confs["example-room"]
    y.room_confs["example-room"] = {**conf, "allow": ["mcp__example-hands__browser"], "mcp": {"example-hands": {"type": "stdio", "command": "node"}}}
    for err in ("This content is unreachable", "You need to log in to access this content"):
        for link, name in (("https://www.instagram.com/stories/example.account/123456789/", "123456789"),
                           ("https://www.instagram.com/stories/highlights/987654321/", "987654321"),
                           ("https://instagram.com/stories/example.account", "example.account")):
            (bin_ / "knobs.json").write_text(json.dumps({"code": 1, "info_code": 1, "said": f"ERROR: [instagram:story] {name}: {err}. Use "
                                                         "--cookies-from-browser or --cookies for the authentication. See  "
                                                         "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  "
                                                         "for how to manually pass cookies\n"}))
            r = ask(y, link)
            assert r == {"ok": False, "error": words.WATCH_FAILED.format(why=f"[instagram:story] {name}: {err}.")}, (err, link, r)
            assert "browser" not in r["error"] and "--" not in r["error"] and not watch.shut(link, err)
    # a post's words on any link that is not a post, a reel or a tv link on a host hands' video action reads: not shut
    for link in ("https://www.instagram.com/example.account/", "https://www.instagram.com/explore/tags/example/", "https://l.instagram.com/?u=x",
                 "https://help.instagram.com/p/Ab1_cD-2eF/", "https://www.instagr.am/p/Ab1_cD-2eF/", "https://www.instagram.com/REEL/Ab1_cD-2eF/",
                 "https://www.instagram.com/reel/Ab1/", "https://www.instagram.com/stories/example.account/123456789/"):
        assert not watch.shut(link, "Instagram sent an empty media response"), link
    for link in (REEL, "https://instagram.com/example.account/p/Xy9-ab_CDE1/", "https://www.instagram.com/reels/Xy9-ab_CDE1",
                 "https://m.instagram.com/tv/Xy9-ab_CDE1/", "https://instagr.am/p/Ab1_cD-2eF/", "https://WWW.Instagram.com/reel/Ab1_cD-2eF"):
        assert watch.shut(link, "Instagram sent an empty media response"), link
    assert kinds(bin_).count("pull") == 6 and watch.saved(work / "videos" / "instagram-123456789") is None


def test_a_run_that_saves_nothing_is_said_in_words(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, nothing=True)          # what --max-filesize does to a video past it
    assert ask(y, REEL) == {"ok": False, "error": words.WATCH_NOTHING}                   # a page with no captions
    assert kinds(bin_) == ["probe", "pull"] and watch.saved(work / "videos" / "instagram-Ab1_cD-2eF") is None


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
    assert kinds(bin_) == ["probe", "pull"] and len(calls) == 1
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
    assert yes["reused"] and SAID in yes["transcript"] and yes["note"] == words.WATCH_NO_CAPTIONS and yes["source"] == "heard"
    assert [c["owner_yes"] for c in calls] == [False, True] and kinds(bin_) == ["probe", "pull", "probe"]   # no transcript, so -J again


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


@FFMPEG
def test_a_page_with_captions_has_them_as_its_transcript_and_its_sound_is_never_paid_for(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, info=page(manual=("en",), auto=("en",)), json3=CUES)
    calls, rows = hearing(monkeypatch), []
    monkeypatch.setattr(y.ledger, "spend", lambda **row: rows.append(row) or 1)
    r = ask(y, TALK)
    folder, exe = work / "videos" / "youtube-AbC123xyz_0", str(bin_ / "yt-dlp")
    head = words.TRANSCRIPT_HEAD.format(name="video.mp4", minutes=0.1, how=words.FROM_CAPTIONS)
    assert (folder / "video.transcript.txt").read_text() == "\n".join([head, *LINES]) + "\n"     # the uploaded track, not the auto one
    assert r["ok"] and r["transcript"] == "\n".join([head, *LINES]) and r["transcript_file"] == str(folder / "video.transcript.txt")
    assert (r["source"], r["title"], r["note"], r["reused"], len(r["frames"])) == ("captions", "a placeholder talk",
                                                                                   words.WATCH_FROM_CAPTIONS, False, 6)
    assert calls == [] and rows == []                                       # no transcribe.run, no spend row
    probe, track, pull = runs(bin_)
    assert probe["argv"] == [exe, "-J", "--no-playlist", "--no-warnings", "--skip-download", TALK]
    assert track["argv"] == [exe, "--load-info-json", str(folder / ".info.json"), "--skip-download", "--no-warnings", "--write-subs",
                             "--write-auto-subs", "--sub-langs", "en", "--sub-format", "json3", "-o", f"{folder}/captions"]
    assert pull["argv"] == [exe, "--no-playlist", "--no-warnings", "--no-progress", "--restrict-filenames", "--max-filesize", "300M",
                            "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4", "-o", f"{folder}/video.%(ext)s",
                            "--print", "after_move:filepath", "--no-simulate", TALK]
    assert all(x["env"] == {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": os.environ["HOME"]} for x in (probe, track, pull))
    assert sorted(p.name for p in folder.iterdir() if not FRAME.fullmatch(p.name)) == ["url.txt", "video.mp4", "video.transcript.txt"]


@FFMPEG
def test_auto_captions_are_the_untranslated_track_and_the_head_says_they_carry_no_punctuation(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, info=page(auto=("de", "en-orig", "en")), json3=CUES)
    calls = hearing(monkeypatch)
    r = ask(y, TALK, frames=1)
    [track] = [x["argv"] for x in runs(bin_) if "--write-subs" in x["argv"]]
    assert track[track.index("--sub-langs") + 1] == "en-orig" and calls == []
    assert (r["source"], r["note"]) == ("auto-captions", words.WATCH_FROM_AUTO_CAPTIONS)
    assert r["transcript"].splitlines() == [words.TRANSCRIPT_HEAD.format(name="video.mp4", minutes=0.1, how=words.FROM_AUTO_CAPTIONS), *LINES]


def test_the_track_is_the_videos_own_language_uploaded_first_never_live_chat_and_always_one_plain_key():
    def info(language: str | None = None, manual: tuple[str, ...] = (), auto: tuple[str, ...] = ()) -> dict[str, Any]:
        return {"language": language, "subtitles": {k: ["json3", "vtt"] for k in manual}, "automatic_captions": {k: ["json3"] for k in auto}}
    assert captions.track(info("en-US", auto=("fr", "en"))) == ("auto", "en")                  # the primary subtag is tried
    assert captions.track(info("en-US", manual=("en",), auto=("en-orig",))) == ("manual", "en")
    assert captions.track(info("en", manual=("de", "en-GB"), auto=("en-orig",))) == ("manual", "en-GB")    # one of its language
    assert captions.track(info(None, auto=("fr-orig", "fr", "en"))) == ("auto", "fr-orig")     # no language: its one -orig
    assert captions.track(info(None, manual=("de", "en"), auto=("en",))) == ("manual", "en")   # else en
    assert captions.track(info("en", auto=("de",))) == (None, None)                           # never a translation
    assert captions.track(info("en", manual=("live_chat",))) == (None, None)                  # never live chat
    assert captions.track(info("live_chat", manual=("live_chat",))) == (None, None)
    assert captions.track(info("en", manual=("live_chat",), auto=("en",))) == ("auto", "en")
    assert captions.track(info("en", manual=("en-*", "en,fr", "en.*"), auto=("en",))) == ("auto", "en")    # a key, never a pattern
    assert captions.track(info("en.*", manual=("en.*",))) == (None, None)
    assert captions.track({"language": "en", "subtitles": {"en": ["vtt"]}, "automatic_captions": {}}) == (None, None)   # json3 only


def test_the_sub_langs_value_is_never_a_pattern_or_a_list(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, nothing=True, info=page(manual=("en-*", "en,fr", "en.*"), auto=("en",)), json3=CUES)
    assert ask(y, TALK)["source"] == "auto-captions"
    langs = [x["argv"][x["argv"].index("--sub-langs") + 1] for x in runs(bin_) if "--sub-langs" in x["argv"]]
    assert langs == ["en"] and not any("*" in x or "," in x for x in langs)


@FFMPEG
def test_a_probe_that_fails_a_live_page_and_a_track_that_saves_nothing_each_go_the_paid_way_as_today(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, info_code=1,
                         info_said="[youtube] AbC123xyz_0: Extracting URL\nERROR: [youtube] AbC123xyz_0: Video unavailable\n")
    calls = hearing(monkeypatch)
    r = ask(y, TALK, frames=1)
    assert r["ok"] and (r["source"], r["title"]) == ("heard", "") and SAID in r["transcript"] and len(calls) == 1
    assert r["note"] == words.WATCH_CAPTIONS_FAILED.format(why="[youtube] AbC123xyz_0: Video unavailable") and kinds(bin_) == ["probe", "pull"]
    for n, (knobs, why) in enumerate(((page(live_status="is_live", manual=("en",)), words.CAPTIONS_LIVE), ("not a page", words.WATCH_NO_WHY))):
        (bin_ / "knobs.json").write_text(json.dumps({"video": clips["sound"], "info": knobs}))
        r = ask(y, f"https://www.youtube.com/watch?v=Pl4ceH0ld3r{n}", frames=1)
        assert r["ok"] and r["source"] == "heard" and r["note"] == words.WATCH_CAPTIONS_FAILED.format(why=why) and len(calls) == n + 2
    (bin_ / "knobs.json").write_text(json.dumps({"video": clips["sound"], "info": page(manual=("en",)), "json3": CUES, "no_track_file": True}))
    r = ask(y, "https://www.youtube.com/watch?v=Pl4ceH0ld3r9", frames=1)
    assert r["ok"] and r["source"] == "heard" and r["note"] == words.WATCH_CAPTIONS_FAILED.format(why=words.WATCH_NO_WHY) and len(calls) == 4
    assert kinds(bin_) == ["probe", "pull"] * 3 + ["probe", "captions", "pull"]
    assert not list(work.rglob("*.json3")) and not list(work.rglob(".info.json"))


@FFMPEG
def test_a_video_pulled_before_with_no_transcript_gets_its_captions_and_is_not_pulled_again(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, info=page(auto=("en",)), json3=CUES)
    calls = hearing(monkeypatch)
    (folder := work / "videos" / "youtube-AbC123xyz_0").mkdir(parents=True)
    shutil.copy(clips["sound"], folder / "video.mp4")
    r = ask(y, TALK, frames=1)
    assert r["reused"] and r["source"] == "auto-captions" and kinds(bin_) == ["probe", "captions"] and calls == []
    assert r["transcript"].splitlines()[1:] == LINES and r["video"] == str(folder / "video.mp4")


def test_a_video_too_big_to_save_still_gives_its_captions(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, nothing=True, info=page(auto=("en-orig",)), json3=CUES)
    calls = hearing(monkeypatch)
    r = ask(y, TALK)
    folder = work / "videos" / "youtube-AbC123xyz_0"
    assert r == {"ok": True, "slug": "youtube-AbC123xyz_0", "folder": str(folder), "title": "a placeholder talk", "video": "",
                 "seconds": 4.0, "frames": [], "transcript": r["transcript"], "transcript_file": str(folder / "video.transcript.txt"),
                 "source": "auto-captions", "reused": False, "note": f"{words.WATCH_NOTHING}; {words.WATCH_FROM_AUTO_CAPTIONS}"}
    assert r["transcript"].splitlines()[1:] == LINES and calls == []
    assert sorted(p.name for p in folder.iterdir()) == ["url.txt", "video.transcript.txt"]          # no json3, no -J answer
    again = ask(y, TALK)                  # the transcript is there now: only the pull is tried again, and the words still come
    assert again["ok"] and (again["note"], again["source"], again["transcript"]) == (words.WATCH_NOTHING, "auto-captions", r["transcript"])
    assert kinds(bin_) == ["probe", "captions", "pull", "pull"] and calls == []


def test_rolling_captions_collapse_to_their_new_words_and_cues_gather_into_lines_of_at_most_15_s(tmp_path):
    rolling = {"events": [{"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "we  are"}, {"utf8": " going"}]},
                          {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "we are going\nto talk about"}]},
                          {"tStartMs": 4000, "dDurationMs": 2000, "segs": [{"utf8": "to talk about\nplaceholder things"}]},
                          {"tStartMs": 6000, "dDurationMs": 2000, "segs": [{"utf8": "placeholder things\nplaceholder things"}]},
                          {"tStartMs": 7000, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
                          {"tStartMs": 8000, "dDurationMs": 2000, "segs": [{"utf8": "placeholder things today"}]}]}
    (f := tmp_path / "captions.en.json3").write_text(json.dumps(rolling))
    assert captions.lines(f) == ["[00:00:00 -> 00:00:10] we are going to talk about placeholder things today"] and not f.exists()
    f.write_text(json.dumps({"events": [{"tStartMs": i * 3000, "dDurationMs": 3000, "segs": [{"utf8": f"word{i}"}]} for i in range(20)]}))
    got = captions.lines(f)
    spans = [[sum(int(x) * k for x, k in zip(t.split(":"), (3600, 60, 1))) for t in re.findall(r"\d\d:\d\d:\d\d", g)] for g in got]
    assert len(got) == 4 and all(end - start <= captions.LINE_SPAN for start, end in spans) and spans[0] == [0, 15] and spans[-1] == [45, 60]
    assert " ".join(g.split("] ", 1)[1] for g in got) == " ".join(f"word{i}" for i in range(20))
    f.write_text(json.dumps({"events": [{"tStartMs": i * 1000, "dDurationMs": 1000, "segs": [{"utf8": chr(97 + i) * 100}]} for i in range(5)]}))
    assert [len(g.split("] ", 1)[1]) for g in captions.lines(f)] == [201, 201, 100]               # never past LINE_CHARS


@FFMPEG
def test_a_transcript_already_beside_the_video_runs_neither_yt_dlp_nor_the_paid_engine(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips, info=page(manual=("en",)), json3=CUES)
    calls = hearing(monkeypatch)
    (folder := work / "videos" / "youtube-AbC123xyz_0").mkdir(parents=True)
    shutil.copy(clips["sound"], folder / "video.mp4")
    (folder / "video.transcript.txt").write_text(f"{words.TRANSCRIPT_HEAD.format(name='video.mp4', minutes=0.1, how=words.UNCHECKED)}\n{SAID}\n")
    r = ask(y, TALK, frames=1)
    assert r["reused"] and SAID in r["transcript"] and (r["source"], r["note"]) == ("heard", "") and runs(bin_) == [] and calls == []


@FFMPEG
def test_a_video_file_sent_to_the_room_comes_back_as_its_frames_and_its_transcript_and_nothing_is_pulled(tmp_path, monkeypatch, clips):
    """Need 22 of 2026-10-24: a room sent a .mov could not see it, since Read opens no video, ffmpeg in its shell waits on an
    approval no one gives mid-turn, transcribe gives the sound alone and watch took only a link. `path` watches the file
    where it is: the frames under videos/ as a link's are, the transcript beside the file as transcribe writes one."""
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    calls, rows = hearing(monkeypatch), []
    monkeypatch.setattr(y.ledger, "spend", lambda **row: rows.append(row) or 1)
    video = sent(work, clips)
    r = look(y, video)
    folder = work / "videos" / frames_of(video)
    assert r == {"ok": True, "slug": frames_of(video), "folder": str(folder), "title": "", "video": str(video),
                 "seconds": r["seconds"], "frames": r["frames"], "transcript": r["transcript"],
                 "transcript_file": str(video.with_name("clip.transcript.txt")), "source": "heard", "reused": False, "note": ""}
    assert abs(r["seconds"] - 4) < 0.5 and runs(bin_) == []                   # no yt-dlp run: there is nothing to pull
    assert [Path(f["file"]).name for f in r["frames"]] == frames_in(folder) and len(r["frames"]) == 6
    assert all(FRAME.fullmatch(Path(f["file"]).name) and Path(f["file"]).read_bytes()[:3] == b"\xff\xd8\xff" for f in r["frames"])
    assert [f["at"] for f in r["frames"]] == ["0m00s", "0m01s", "0m01s", "0m02s", "0m03s", "0m03s"]
    assert sorted(p.name for p in video.parent.iterdir()) == ["clip.mov", "clip.transcript.txt"]     # the inbox keeps the file
    assert SAID in r["transcript"] and folder.stat().st_mode & 0o777 == 0o700 and not (folder / "url.txt").exists()
    [call] = calls                                # transcribe.run as tool_transcribe calls it, its spend rows under `watch`
    assert call == {"audio": video, "owner_yes": False,
                    "keys": (y.memory.embeddings_key, y.private / transcribe.GROQ_KEY, y.private / transcribe.META_KEY)}
    assert rows == [{"room": "example-room", "tool": "watch", "engine": "openai/whisper-1", "usd": 0.0004, "note": "chunk 1 of 1, 4 s"}]


@FFMPEG
def test_a_file_watched_or_transcribed_before_is_never_paid_again_and_two_of_one_name_never_share_their_frames(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    calls = hearing(monkeypatch)
    first, other = sent(work, clips), sent(work, clips, mid="m-2")       # one name, two messages
    a = look(y, first)
    again = look(y, first, frames=3)
    assert len(calls) == 1 and (again["transcript"], again["folder"]) == (a["transcript"], a["folder"])
    assert len(again["frames"]) == 3 and len(frames_in(Path(a["folder"]))) == 3            # the frames are made again
    b = look(y, other, frames=1)
    assert b["folder"] != a["folder"] and len(frames_in(Path(a["folder"]))) == 3 and len(calls) == 2
    memo = sent(work, clips, name="memo.mov", mid="m-3")
    head = words.TRANSCRIPT_HEAD.format(name="memo.mov", minutes=0.1, how=words.UNCHECKED)
    memo.with_name("memo.transcript.txt").write_text(f"{head}\n{SAID}\n")                 # the transcribe tool's, from before
    r = look(y, memo, frames=1)
    assert r["ok"] and r["transcript"] == f"{head}\n{SAID}" and r["source"] == "heard" and len(calls) == 2 and runs(bin_) == []


def test_a_path_outside_the_rooms_folders_a_missing_file_a_link_out_and_a_path_with_a_link_are_refused_in_words(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    calls = hearing(monkeypatch)
    (theirs := tmp_path / "rooms" / "example-quiet" / "work" / "inbox" / INBOX).mkdir(parents=True)
    (elsewhere := theirs / "clip.mov").write_bytes(b"\0")                                  # another room's file
    (box := work / "inbox" / INBOX).mkdir(parents=True)
    (box / "link.mov").symlink_to(elsewhere)                                                # a link this room's shell left
    assert look(y, elsewhere) == {"ok": False, "error": words.WATCH_NOT_HERE.format(name="clip.mov")}
    assert look(y, box / "link.mov") == {"ok": False, "error": words.WATCH_NOT_HERE.format(name="clip.mov")}
    assert look(y, box / "gone.mov") == {"ok": False, "error": words.WATCH_NO_FILE.format(name="gone.mov")}
    assert look(y, box / "link.mov", url=REEL) == {"ok": False, "error": words.WATCH_BOTH}
    assert asyncio.run(y.tool_watch("no-such-room", "", 6, False, str(box / "link.mov")))["error"] == words.WATCH_NO_ROOM.format(room="no-such-room")
    assert runs(bin_) == [] and calls == [] and not (work / "videos").exists() and elsewhere.read_bytes() == b"\0"


@FFMPEG
def test_a_link_where_a_files_frames_go_is_never_followed(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    hearing(monkeypatch)
    video, secret = sent(work, clips), tmp_path / "secret.txt"
    secret.write_text("a placeholder secret\n")
    (folder := work / "videos" / frames_of(video)).mkdir(parents=True)
    (folder / "frame-01-00m00s.jpg").symlink_to(secret)                  # a frame "made before" that is a file outside the room
    r = look(y, video)
    assert r["ok"] and len(r["frames"]) == 6 and not (folder / "frame-01-00m00s.jpg").is_symlink()
    assert secret.read_text() == "a placeholder secret\n"
    shutil.rmtree(folder)
    (elsewhere := tmp_path / "elsewhere").mkdir()
    folder.symlink_to(elsewhere)                                         # the frames' folder itself a link
    assert look(y, video) == {"ok": False, "error": words.WATCH_LINKED.format(name=folder.name)} and list(elsewhere.iterdir()) == []


@FFMPEG
def test_a_file_that_is_no_video_is_said_and_one_with_no_picture_or_no_sound_gives_what_it_has(tmp_path, monkeypatch, clips):
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    calls = hearing(monkeypatch)
    (box := work / "inbox" / INBOX).mkdir(parents=True)
    (box / "notes.mov").write_text("a placeholder line, not a video\n")
    r = look(y, box / "notes.mov")
    assert not r["ok"] and r["error"].startswith(words.WATCH_NOT_READ.split("(")[0].format(name="notes.mov")) and calls == []
    quiet = look(y, sent(work, clips, video="silent", name="quiet.mov"))
    assert quiet["ok"] and len(quiet["frames"]) == 6 and (quiet["transcript"], quiet["note"]) == ("", words.WATCH_NO_SOUND) and calls == []
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", clips["sound"], "-vn", "-c:a", "aac", str(box / "tone.m4a")], check=True,
                   capture_output=True)
    tone = look(y, box / "tone.m4a")                                     # sound alone: its words, no frames, and why
    assert tone["ok"] and tone["frames"] == [] and tone["note"] == words.WATCH_NO_PICTURE and SAID in tone["transcript"] and len(calls) == 1


def test_a_video_file_is_told_from_a_recording_and_a_picture_by_its_name_or_its_type():
    assert all(watch.is_video(n) for n in ("clip.mov", "CLIP.MOV", "clip.mp4", "clip.m4v", "clip.mkv", "clip.3gp"))
    assert watch.is_video("clip", "video/quicktime") and watch.is_video("clip.bin", "video/mp4")
    assert not any(watch.is_video(n) for n in ("photo.png", "voice-message.ogg", "memo.m4a", "notes.txt", ""))
    assert not watch.is_video("screen.webm", "video/webm") and transcribe.is_audio("screen.webm")     # a .webm stays transcribe's
    assert not watch.is_video("memo.mp4", "audio/mp4")


def test_a_video_sent_in_arrives_with_the_line_that_it_is_seen_only_through_watch():
    files = [{"name": "clip.mov", "type": "video/quicktime", "size": 7_900_000, "path": f"/s/inbox/{INBOX}/clip.mov", "ok": True},
             {"name": "photo.png", "type": "image/png", "size": 10, "path": f"/s/inbox/{INBOX}/photo.png", "ok": True}]
    text = message_text(NS(clean_content="?", content="?"), files).splitlines()
    assert text == ["?", f"[attachment] clip.mov, video/quicktime, 7,900,000 bytes, saved at /s/inbox/{INBOX}/clip.mov",
                    words.VIDEO_FILE.format(name="clip.mov"), f"[attachment] photo.png, image/png, 10 bytes, saved at /s/inbox/{INBOX}/photo.png"]
    assert "call watch with `path` set to the path above before you answer" in text[2] and "Read its frames" in text[2]
    assert context.in_text("\n".join(text))["attachment lines"] == sum(context.chars(x) for x in text[1:])    # `yerk map --wire`
    for said in (text[2], words.UNWATCHED, words.WATCH_BOTH, words.WATCH_NOT_HERE):     # in the prompting guide's shape
        assert shape.offenses(said) == [], said


def test_a_video_sent_in_and_never_watched_bounces_once_at_stop_and_only_a_watch_call_on_it_sees_it(monkeypatch):
    room = Room.__new__(Room)
    room.lock, room.pending, room.inflight, room.to_open, room.seen, room.bounced, written = threading.Lock(), [], [], {}, {}, False, []
    room.rp = NS(read_paths=set(), send=written.append)
    files = [{"name": "clip.mov", "type": "video/quicktime", "size": 10, "path": f"/in/{INBOX}/clip.mov", "ok": True},
             {"name": "gone.mov", "type": "video/quicktime", "size": 10, "ok": False, "error": "not staged"}]
    room._write(Inbound(id=INBOX, sender="owner", text="what is in this one", files=files))
    assert room.to_open == {f"/in/{INBOX}/clip.mov": "clip.mov"} and written == ["what is in this one"]
    assert room.stop_check(active=False) == words.UNWATCHED.format(names="clip.mov", paths=f"/in/{INBOX}/clip.mov")
    assert room.stop_check(active=False) is None                                 # once per result
    rp = RoomProcess.__new__(RoomProcess)
    calls = [("Read", {"file_path": f"/in/{INBOX}/clip.mov"}), ("mcp__yerk__transcribe", {"path": f"/in/{INBOX}/other.mp4"}),
             ("mcp__yerk__watch", {"path": f"/in/{INBOX}/third.mov"}), ("mcp__yerk__watch", {"path": f"/in/{INBOX}/memo.m4a"}),
             ("Read", {"file_path": f"/in/{INBOX}/photo.png"})]
    ev = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": n, "input": i} for n, i in calls]}}
    rp.proc, rp.read_paths, rp.evidence, rp.events, rp.answers = NS(stdout=[json.dumps(ev).encode()]), set(), None, queue.Queue(), queue.Queue()
    monkeypatch.setattr(rp, "_note", lambda obj: None)
    rp._read()               # a Read of a video opens nothing and transcribe hears only its sound, so both still bounce
    assert rp.read_paths == {f"/in/{INBOX}/third.mov", f"/in/{INBOX}/memo.m4a", f"/in/{INBOX}/photo.png"}
    room.bounced, room.rp.read_paths = False, {f"/in/{INBOX}/clip.mov"}         # the engine adds a watch call's path
    assert room.stop_check(active=False) is None


SERVER, ELSEWHERE, OWN, NEAR, FAR = 900, 901, 11, 12, 13    # placeholder ids: the room's server, another, and three channels


def gone(what: str) -> discord.NotFound:
    return discord.NotFound(NS(status=404, reason="Not Found"), {"code": 10008, "message": f"Unknown {what}"})


class Shelf:
    """A channel on the fake gateway: its server and its messages by id; one not there is Discord's 404, `shut` its 403."""
    def __init__(self, cid: int, server: int) -> None:
        self.id, self.guild, self.msgs, self.shut, self.asked = cid, NS(id=server), dict[int, Any](), False, list[int]()

    async def fetch_message(self, mid: int) -> Any:
        self.asked.append(mid)
        if self.shut:
            raise discord.Forbidden(NS(status=403, reason="Forbidden"), {"code": 50001, "message": "Missing Access"})
        if mid not in self.msgs:
            raise gone("Message")
        return self.msgs[mid]


class Gateway:
    """The bot's view: the channels it has in cache; any other is Discord's 404 when fetched."""
    def __init__(self, *shelves: Shelf) -> None:
        self.chans, self.user = {x.id: x for x in shelves}, NS(id=99)

    def event(self, fn: Any) -> Any:
        return fn

    def get_channel(self, cid: int) -> Shelf | None:
        return self.chans.get(cid)

    async def fetch_channel(self, cid: int) -> Shelf:
        raise gone("Channel")


def on_discord(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clips: dict[str, str]) -> tuple[Yerkd, Path, Path, dict[int, Shelf]]:
    """The example room on the Discord adapter, its own channel OWN in SERVER beside NEAR, and FAR in another server."""
    y, work, bin_ = room(tmp_path, monkeypatch, clips)
    shelves = {OWN: Shelf(OWN, SERVER), NEAR: Shelf(NEAR, SERVER), FAR: Shelf(FAR, ELSEWHERE)}
    d = DiscordChannel("token", 7, [], y.ledger, client=Gateway(*shelves.values()), state=tmp_path)
    d.add_room(OWN, y.add_room("example-room", d))
    return y, work, bin_, shelves


def link(cid: int, mid: int, server: int = SERVER) -> str:
    return f"https://discord.com/channels/{server}/{cid}/{mid}"


def test_a_discord_messages_link_is_its_channel_and_its_id_in_every_form_discord_gives():
    for host in ("discord.com", "www.discord.com", "ptb.discord.com", "canary.discord.com", "discordapp.com"):
        assert watch.message(f" https://{host}/channels/{SERVER}/{NEAR}/501/ ") == (NEAR, 501), host
    assert watch.message(f"https://discord.com/channels/@me/{NEAR}/501") == (NEAR, 501)     # the bot's own read says no
    for other in (f"https://discord.com/channels/{SERVER}/{NEAR}", f"https://discord.com.example.org/channels/{SERVER}/{NEAR}/501",
                  f"http://discord.com/channels/{SERVER}/{NEAR}/501", REEL, f"https://cdn.discordapp.com/attachments/{NEAR}/501/a.mp4"):
        assert watch.message(other) is None, other


@FFMPEG
def test_a_video_attached_to_a_discord_message_is_watched_from_the_messages_link_and_nothing_is_pulled(tmp_path, monkeypatch, clips):
    """Need 25 of 2026-10-30: a room asked to watch videos another assistant had posted as mp4 files in a Discord channel
    had their names from a history listing and no address, and watch took the message's link to yt-dlp, which said
    "Unsupported URL". The daemon's bot reads the message, saves its video to the room's inbox as a file sent to it, and
    watches it there: the same frames and transcript as `path`, paid once, saved once."""
    y, work, bin_, shelves = on_discord(tmp_path, monkeypatch, clips)
    calls, data = hearing(monkeypatch), Path(clips["sound"]).read_bytes()
    shelves[NEAR].msgs[501] = NS(id=501, attachments=[Attachment("part-one.mp4", data, "video/mp4")])
    r = ask(y, link(NEAR, 501))
    video = work / "inbox" / "501" / "part-one.mp4"
    assert r == {"ok": True, "slug": frames_of(video), "folder": str(work / "videos" / frames_of(video)), "title": "",
                 "video": str(video), "seconds": r["seconds"], "frames": r["frames"], "transcript": r["transcript"],
                 "transcript_file": str(video.with_name("part-one.transcript.txt")), "source": "heard", "reused": False, "note": ""}
    assert video.read_bytes() == data and len(r["frames"]) == 6 and SAID in r["transcript"] and abs(r["seconds"] - 4) < 0.5
    assert runs(bin_) == [] and len(calls) == 1                                   # yt-dlp never ran on the link
    rows = y.ledger._select("SELECT * FROM attachments WHERE message_id = ?", ("501",))
    assert [(x["name"], x["state"], x["room"]) for x in rows] == [("part-one.mp4", "staged", "example-room")]
    again = ask(y, link(NEAR, 501, server=ELSEWHERE), frames=2)                  # the link's server is never read
    assert again["ok"] and again["transcript"] == r["transcript"] and len(again["frames"]) == 2 and len(calls) == 1
    assert len(y.ledger._select("SELECT * FROM attachments WHERE message_id = ?", ("501",))) == 2 and runs(bin_) == []


@FFMPEG
def test_a_message_with_more_videos_watches_the_first_and_saves_the_rest_and_one_too_big_is_never_saved(tmp_path, monkeypatch, clips):
    y, work, bin_, shelves = on_discord(tmp_path, monkeypatch, clips)
    hearing(monkeypatch)
    data, huge = Path(clips["sound"]).read_bytes(), Attachment("huge.mov", b"x", "video/quicktime")
    huge.size = watch.MOST_BYTES + 1
    files = [Attachment("notes.txt", b"x", "text/plain"), Attachment("one.mp4", data, "video/mp4"), huge, Attachment("two.mov", data)]
    shelves[OWN].msgs[502] = NS(id=502, attachments=files)
    r = ask(y, link(OWN, 502), frames=1)
    box = work / "inbox" / "502"
    assert r["ok"] and r["video"] == str(box / "one.mp4") and sorted(p.name for p in box.iterdir()) == ["one.mp4", "one.transcript.txt", "two.mov"]
    assert r["note"] == "; ".join((words.WATCH_MORE.format(n=1, paths=box / "two.mov"), words.WATCH_TOO_BIG.format(names="huge.mov")))
    shelves[OWN].msgs[503] = NS(id=503, attachments=[huge])
    assert ask(y, link(OWN, 503)) == {"ok": False, "error": words.WATCH_TOO_BIG.format(names="huge.mov")}
    assert not (work / "inbox" / "503").exists() and runs(bin_) == []


def test_a_discord_message_is_read_only_where_the_room_reads_and_an_owners_room_anywhere_in_its_own_server(tmp_path, monkeypatch, clips):
    y, work, bin_, shelves = on_discord(tmp_path, monkeypatch, clips)
    calls = hearing(monkeypatch)
    for shelf in shelves.values():
        shelf.msgs[7] = NS(id=7, attachments=[Attachment("notes.txt", b"x", "text/plain")])
    none = {"ok": False, "error": words.WATCH_NO_VIDEO.format(files="notes.txt")}           # read: it has no video
    refused = {"ok": False, "error": words.WATCH_NOT_ITS_CHANNEL}
    assert ask(y, link(OWN, 7)) == ask(y, link(NEAR, 7)) == none                            # an owner's room: its server
    assert ask(y, link(FAR, 7, server=ELSEWHERE)) == refused and shelves[FAR].asked == []    # never another server's
    y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    assert ask(y, link(NEAR, 7)) == refused and shelves[NEAR].asked == [7]                  # a person's: its own channel
    assert ask(y, link(OWN, 7)) == none and shelves[OWN].asked == [7, 7]
    y.channels["example-room"].add_room(OWN, y.rooms["example-room"], NEAR)                 # and the one it watches
    assert ask(y, link(NEAR, 7)) == none and shelves[NEAR].asked == [7, 7]
    assert calls == [] and runs(bin_) == [] and not (work / "inbox").exists() and not (work / "videos").exists()


def test_a_message_not_there_a_channel_shut_to_the_bot_no_files_and_a_room_off_discord_are_each_said_in_words(tmp_path, monkeypatch, clips):
    y, work, bin_, shelves = on_discord(tmp_path, monkeypatch, clips)
    calls = hearing(monkeypatch)
    shelves[OWN].msgs[8] = NS(id=8, attachments=[])
    assert ask(y, link(OWN, 404)) == ask(y, link(77, 1)) == {"ok": False, "error": words.WATCH_NO_MESSAGE}   # a channel it lacks too
    assert ask(y, link(OWN, 8)) == {"ok": False, "error": words.WATCH_NO_FILES}
    shelves[NEAR].shut = True
    assert ask(y, link(NEAR, 1)) == {"ok": False, "error": words.WATCH_NOT_READABLE}
    shelves[OWN].msgs[9] = NS(id=9, attachments=[Attachment("cut.mp4", b"x", "video/mp4", fail=True)])
    assert ask(y, link(OWN, 9)) == {"ok": False, "error": words.WATCH_NOT_SAVED.format(name="cut.mp4", why="OSError: cdn said no")}
    y.add_room("example-room", ConsoleChannel())
    assert ask(y, link(OWN, 8)) == {"ok": False, "error": words.WATCH_NO_DISCORD}
    assert calls == [] and runs(bin_) == [] and not (work / "videos").exists()
    for said in (words.WATCH_NO_DISCORD, words.WATCH_NOT_ITS_CHANNEL, words.WATCH_NO_MESSAGE, words.WATCH_NOT_READABLE,
                 words.WATCH_DISCORD_DOWN, words.WATCH_NO_VIDEO, words.WATCH_NO_FILES, words.WATCH_TOO_BIG, words.WATCH_NOT_SAVED,
                 words.WATCH_MORE):                                                         # in the prompting guide's shape
        assert shape.offenses(said) == [], said
