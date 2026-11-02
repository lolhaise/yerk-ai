"""The watch tool: a video in a post (an Instagram reel or post, a TikTok, an X post, a YouTube link, any page yt-dlp knows)
pulled from its link into the calling room's work folder under videos/<slug>/, with still frames spread evenly through it
and its sound transcribed, so a room answers off the video and not off the caption, the cover picture and the comments a
page gives. It is a tool on Yerk's own MCP server because the daemon has the network and a room's shell has none. yt-dlp is
the program on the daemon's PATH, never imported (Yerk's venv does not carry it), run in a clean environment in a process
group of its own, one download at a time across rooms, and killed with its group past WATCH_TIMEOUT. ffprobe reads the
length and the streams, ffmpeg takes the frames, and the transcript is the page's own captions when it has them, read
free before the download (captions.py), else transcribe.run's, on its keys, its US$1 gate and its spend rows. A post
already pulled is never pulled or transcribed again: its folder keeps the video, the transcript beside it and the link it
came from, so another post whose slug is the same gets a folder of its own. A video file sent to a room (`path`), which a
room's Read cannot open, is watched where it is (run_file): nothing pulled, the same frames under videos/, the transcript
beside it as transcribe writes one. A Discord message's link (`message`) is watched from the video attached to it, which
the daemon's bot saves to the room's inbox as a file sent to it (the adapter's pull), then run_file.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import SplitResult, parse_qs, urlsplit

from . import captions, transcribe, words
from .config import write_private

FOLDER = "videos"            # under a room's work folder: a folder a post
WATCH_TIMEOUT = 300          # seconds one download may take before yt-dlp is killed with its process group
FRAME_TIMEOUT = 60           # seconds one frame, or the probe, may take
FRAMES, MOST = 6, 12         # the frames a call takes unless asked, and the most it takes
SLUG = 48                    # the most characters of a post's own id its folder's name keeps
CAP = "300M"                 # yt-dlp's --max-filesize: a video past it is skipped, nothing saved
FORMAT = "bv*[height<=720]+ba/b[height<=720]/b"    # the best picture at 720 lines or under with the best sound, else one file
SOURCE = "url.txt"           # the link a folder's post was pulled from
MOST_BYTES = 300 << 20       # CAP in bytes: a video attached to a Discord message past it is not saved
DISCORD = re.compile(r"https://(?:(?:www|ptb|canary)\.)?discord(?:app)?\.com/channels/(?:\d+|@me)/(\d+)/(\d+)/?")
KINDS = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".3gp", ".mpg", ".mpeg", ".wmv")    # a video file as a phone or a camera saves it
ONE = threading.Lock()       # one download at a time across rooms; a caller waits its turn, never fails for it
GUARD = threading.Lock()     # held while a call picks its folder
POSTS: dict[Path, threading.Lock] = {}     # one call at a time a post, so two at once never pay twice for its transcript
Hear = Callable[[Path], dict[str, Any]]    # transcribe.run on the video, as the daemon calls it


class Failed(Exception):
    """A step that stops the call, in words a room can say."""


def is_link(url: str) -> bool:
    """An http or https address with a host."""
    try:
        u = urlsplit(url.strip())
        return u.scheme in ("http", "https") and bool(u.hostname)
    except ValueError:
        return False


def message(url: str) -> tuple[int, int] | None:
    """A Discord message's link (discord.com/channels/SERVER/CHANNEL/MESSAGE, as Copy Message Link gives it): its channel's
    id and its own, else None. The server is not read from it: the bot reads the channel's own (the adapter's pull)."""
    return (int(m[1]), int(m[2])) if (m := DISCORD.fullmatch(url.strip())) else None


def is_video(name: str, kind: str = "") -> bool:
    """A video file by its name or its type, never one transcribe takes for a recording (a .webm can be either)."""
    return (Path(name).suffix.lower() in KINDS or kind.startswith("video/")) and not transcribe.is_audio(name, kind)


def site(u: SplitResult) -> str:
    """The host's second-level label: instagram, tiktok, x, youtube."""
    labels = (u.hostname or "").split(".")
    return re.sub(r"[^A-Za-z0-9_-]", "", labels[-2] if len(labels) > 1 else labels[0])


def slug(url: str) -> str:
    """A post's folder name: its site, a hyphen, then the link's last non-empty path segment (a `v` in its query over that,
    a watch link's video) kept to letters, digits, _ and -, at most SLUG long; with none, 10 hex of the link's sha1."""
    u = urlsplit(url)
    last = parse_qs(u.query).get("v", [""])[0] or next((s for s in reversed(u.path.split("/")) if s), "")
    return f"{site(u)}-{re.sub(r'[^A-Za-z0-9_-]', '', last)[:SLUG] or hashlib.sha1(url.encode()).hexdigest()[:10]}"


def post(url: str) -> str:
    """What makes two links one post: the site, the path and a `v`; a share's tracking query is not."""
    u = urlsplit(url.strip())
    return f"{site(u)}{u.path.rstrip('/')}?{parse_qs(u.query).get('v', [''])[0]}"


def folder_for(root: Path, url: str) -> Path:
    """The post's folder, made 0700 and holding its link: slug(url), or that with 10 hex of the link's sha1 after it when
    another post's link is there already. A room's shell writes in its work folder and the daemon reads and writes as the
    owner, so a link in it is never followed: a linked folder stops the call, and a link inside the folder is removed."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = root / slug(url)
    if linked := next((p for p in (root, folder) if p.is_symlink()), None):
        raise Failed(words.WATCH_LINKED.format(name=linked.name))
    if (was := folder / SOURCE).is_file() and not was.is_symlink() and post(was.read_text()) != post(url):
        if (folder := root / f"{folder.name}-{hashlib.sha1(url.encode()).hexdigest()[:10]}").is_symlink():
            raise Failed(words.WATCH_LINKED.format(name=folder.name))
    folder.mkdir(mode=0o700, exist_ok=True)
    for p in folder.iterdir():
        if p.is_symlink():
            p.unlink()
    if not (folder / SOURCE).is_file():
        write_private(folder / SOURCE, url + "\n")
    return folder


def saved(folder: Path) -> Path | None:
    """The newest video.EXT in the folder: one dot, so never its transcript, a part file or a stream yt-dlp merges."""
    got = [p for p in folder.glob("video.*") if p.name.count(".") == 1 and p.suffix not in (".part", ".ytdl") and p.is_file()
           and not p.is_symlink()]
    return max(got, key=lambda p: p.stat().st_mtime, default=None)


def last(text: str) -> str:
    return next((x.strip() for x in reversed(text.splitlines()) if x.strip()), "")


def pull(exe: str, url: str, folder: Path) -> tuple[Path, float]:
    """One yt-dlp run into `folder`: the file it saved and the seconds it took, else Failed. Popen and not subprocess.run,
    since run kills only yt-dlp at its timeout, never the ffmpeg it started to join the picture and the sound."""
    argv = [exe, "--no-playlist", "--no-warnings", "--no-progress", "--restrict-filenames", "--max-filesize", CAP, "-f", FORMAT,
            "--merge-output-format", "mp4", "-o", str(folder / "video.%(ext)s"), "--print", "after_move:filepath", "--no-simulate", url]
    env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": os.environ.get("LANG", "C.UTF-8"), "HOME": os.environ.get("HOME", str(Path.home()))}
    t0 = time.monotonic()
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                          start_new_session=True) as p:
        try:
            out, err = p.communicate(timeout=WATCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)          # the whole group: yt-dlp and the ffmpeg it runs
            p.wait()
            raise Failed(words.WATCH_TIMED_OUT.format(seconds=WATCH_TIMEOUT))
    said = Path(last(out) or ".")                         # the path --print gave, else the newest video.* there
    got = said if said.parent == folder and said.is_file() and not said.is_symlink() else saved(folder)
    if p.returncode or got is None:
        why = re.split(r"\s+Use --|; please report this issue", last(err).removeprefix("ERROR: "), maxsplit=1)[0]   # its advice
        raise Failed(words.WATCH_FAILED.format(why=words.no_paths(why)[:300]) if why else words.WATCH_NOTHING)    # on its flags
    return got, time.monotonic() - t0


def probe(video: Path) -> tuple[float, bool, bool]:
    """The video's length in seconds, one decimal, and whether it has a sound stream and a picture stream, by ffprobe."""
    if not (exe := shutil.which("ffprobe")):
        raise Failed(words.WATCH_NO_FFMPEG)
    try:
        p = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(video)],
                           capture_output=True, text=True, timeout=FRAME_TIMEOUT)
        data = json.loads(p.stdout)
        kinds = {str(s.get("codec_type")) for s in data.get("streams") or []}
        return round(float(data["format"]["duration"]), 1), "audio" in kinds, "video" in kinds
    except subprocess.TimeoutExpired:
        why = words.WATCH_SLOW.format(seconds=FRAME_TIMEOUT)
    except (ValueError, KeyError, TypeError, AttributeError):
        why = words.no_paths(last(p.stderr))[:160] or words.WATCH_NO_WHY
    raise Failed(words.WATCH_NOT_READ.format(name=video.name, why=why))


def stills(video: Path, seconds: float, n: int, into: Path | None = None) -> tuple[list[dict[str, str]], str]:
    """`n` still pictures spread evenly through the video, at seconds * (i + 0.5) / n, in `into` (else the video's folder),
    the ones an earlier call left there removed first: each a JPEG at most 1280 wide, named by its number and its time; and
    the note for any that did not come."""
    if not (exe := shutil.which("ffmpeg")):
        raise Failed(words.WATCH_NO_FFMPEG)
    for old in (into := into or video.parent).glob("frame-*.jpg"):
        old.unlink()
    out, why = [], ""
    for i in range(n):
        t = seconds * (i + 0.5) / n
        f = into / f"frame-{i + 1:02d}-{int(t) // 60:02d}m{int(t) % 60:02d}s.jpg"
        try:
            p = subprocess.run([exe, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-vf",
                                "scale='min(1280,iw)':-2", "-q:v", "4", str(f)], capture_output=True, text=True, timeout=FRAME_TIMEOUT)
            said = last(p.stderr)
        except subprocess.TimeoutExpired:
            said = words.WATCH_SLOW.format(seconds=FRAME_TIMEOUT)
        if f.is_file():
            out.append({"at": f"{int(t) // 60}m{int(t) % 60:02d}s", "file": str(f)})
        else:
            why = words.no_paths(said)[:160] or words.WATCH_NO_WHY
    return out, words.WATCH_FRAMES_LOST.format(got=len(out), of=n, why=why) if why else ""


def heard(video: Path, hear: Hear) -> tuple[str, str, str]:
    """The transcript's text as a result carries it (at most transcribe.RESULT_CHARS, cut at a line), its file, and the
    note when there is none: the file beside the video when it is there, else transcribe.run's, which writes it."""
    kept = video.with_name(f"{video.stem}.transcript.txt")
    if not kept.is_file() or kept.is_symlink():
        r = hear(video)
        if not r.get("ok"):
            return "", "", words.WATCH_NO_TRANSCRIPT.format(why=r.get("error") or words.WATCH_NO_WHY)
        kept = Path(str(r.get("file") or kept))
    text = kept.read_text(errors="replace").strip()
    if len(text) > transcribe.RESULT_CHARS:
        cut = text.rfind("\n", 0, transcribe.RESULT_CHARS)
        text = text[:cut if cut > 0 else transcribe.RESULT_CHARS] + "\n" + words.MORE_IN_FILE
    return text, str(kept), ""


def captioned(exe: str, url: str, video: Path) -> tuple[dict[str, Any], str]:
    """The captions step, before the download and outside ONE: the page's facts by captions.probe, and its one caption
    track written as the transcript beside `video`, so heard() reads it and nothing is paid; and the words for the note,
    what came or why none did. A missing track or a step that fails never stops the call."""
    info: dict[str, Any] = {}
    try:
        info = captions.probe(exe, url, video.parent)
        kind, key = captions.track(info)
        if not kind or not key or not (said := captions.lines(captions.pull(exe, video.with_name(captions.INFO), key, video.parent))):
            return info, words.WATCH_NO_CAPTIONS
        captions.write(video, said, info, kind)
        return info, words.WATCH_FROM_CAPTIONS if kind == "manual" else words.WATCH_FROM_AUTO_CAPTIONS
    except Failed as e:
        return info, words.WATCH_CAPTIONS_FAILED.format(why=e)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return info, words.WATCH_CAPTIONS_FAILED.format(why=getattr(e, "strerror", None) or words.WATCH_NO_WHY)
    finally:
        video.with_name(captions.INFO).unlink(missing_ok=True)


def run(url: str, frames: int, root: Path, hear: Hear) -> tuple[dict[str, Any], float]:
    """The tool's whole call, blocking (the daemon runs it off the loop): {"ok": False, "error"} on any stop, else the
    video, its length, its frames, its transcript, where that came from and whether it was pulled before; and the seconds
    its download took. With no transcript beside the video, the page's captions are tried first, and a download that
    fails with a transcript there returns the transcript alone, its note saying why there is no video."""
    url, n, took, said = url.strip(), max(1, min(MOST, int(frames))), 0.0, ""
    info: dict[str, Any] = {}
    try:
        with GUARD:
            folder = folder_for(root, url)
            mine = POSTS.setdefault(folder, threading.Lock())
        with mine:
            kept = folder / "video.transcript.txt"
            if not kept.is_file() and (exe := shutil.which("yt-dlp")):
                info, said = captioned(exe, url, saved(folder) or folder / "video.mp4")
            with ONE:
                reused = (video := saved(folder)) is not None
                if video is None:
                    if not (exe := shutil.which("yt-dlp")):
                        return {"ok": False, "error": words.WATCH_NO_YTDLP}, took
                    try:
                        video, took = pull(exe, url, folder)
                    except Failed as e:
                        if not kept.is_file() or kept.is_symlink():
                            raise
                        text, file, _ = heard(folder / "video.mp4", hear)     # the file is there, so nothing is paid
                        return {"ok": True, "slug": folder.name, "folder": str(folder), "title": str(info.get("title") or ""),
                                "video": "", "seconds": round(info.get("duration") or 0.0, 1), "frames": [], "transcript": text,
                                "transcript_file": file, "source": captions.source(text), "reused": False,
                                "note": "; ".join(x for x in (str(e), said) if x)}, took
            seconds, sound, picture = probe(video)
            shots, lost = stills(video, seconds, n) if picture else ([], words.WATCH_NO_PICTURE)
            text, file, why = heard(video, hear) if sound or kept.is_file() else ("", "", words.WATCH_NO_SOUND)
    except Failed as e:
        return {"ok": False, "error": str(e)}, took
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": words.WATCH_BROKE.format(why=getattr(e, "strerror", None) or words.WATCH_NO_WHY)}, took
    return {"ok": True, "slug": folder.name, "folder": str(folder), "title": str(info.get("title") or ""), "video": str(video),
            "seconds": seconds, "frames": shots, "transcript": text, "transcript_file": file, "source": captions.source(text),
            "reused": reused, "note": "; ".join(x for x in (lost, said if text else "", why) if x)}, took


def run_file(video: Path, frames: int, root: Path, hear: Hear) -> tuple[dict[str, Any], float]:
    """The tool's call on a video file in the room's own folders (`path`, checked by the daemon), blocking, in run's shape:
    nothing pulled, so no captions and no seconds of download; its frames in a folder of their own under `root`, file-<its
    name>-<10 hex of its path's sha1>, so two files of one name never share one, and a link there stops the call as in
    folder_for; its transcript beside it, where transcribe writes one, read from that file when it is there, so a video
    transcribed or watched before is never paid for twice."""
    n = max(1, min(MOST, int(frames)))
    folder = root / f"file-{re.sub(r'[^A-Za-z0-9_-]', '', video.stem)[:SLUG] or 'video'}-{hashlib.sha1(str(video).encode()).hexdigest()[:10]}"
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if linked := next((p for p in (root, folder) if p.is_symlink()), None):
            raise Failed(words.WATCH_LINKED.format(name=linked.name))
        folder.mkdir(mode=0o700, exist_ok=True)
        with GUARD:
            mine = POSTS.setdefault(folder, threading.Lock())
        with mine:
            seconds, sound, picture = probe(video)
            shots, lost = stills(video, seconds, n, folder) if picture else ([], words.WATCH_NO_PICTURE)
            kept = video.with_name(f"{video.stem}.transcript.txt").is_file()
            text, file, why = heard(video, hear) if sound or kept else ("", "", words.WATCH_NO_SOUND)
    except Failed as e:
        return {"ok": False, "error": str(e)}, 0.0
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": words.WATCH_BROKE.format(why=getattr(e, "strerror", None) or words.WATCH_NO_WHY)}, 0.0
    return {"ok": True, "slug": folder.name, "folder": str(folder), "title": "", "video": str(video), "seconds": seconds,
            "frames": shots, "transcript": text, "transcript_file": file, "source": captions.source(text), "reused": False,
            "note": "; ".join(x for x in (lost, why) if x)}, 0.0
