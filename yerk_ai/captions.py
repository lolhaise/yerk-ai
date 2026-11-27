"""Uses a video's own captions as its transcript, read for free before anything pays to turn its sound into text: a step of the
watch tool, which fetches a video from a link so a room can answer about it. A page yt-dlp lists caption tracks for (YouTube's
uploaded and auto-generated ones) gives them in two small requests: `yt-dlp -J` for the page's facts and the tracks it offers,
then one track by its key, named only after -J has shown it (a pattern or a list reaches YouTube's auto-translated tracks too,
and it answers HTTP 429), saved as json3 from that same answer and read into the lines and the head transcribe.run writes, as
NAME.transcript.txt beside the video, so watch.heard reads it as it is and the US$1 gate is never reached for it. The track is
the video's own language, an uploaded one over an auto-generated one and the untranslated auto track (<lang>-orig) over its
plain one, never live chat and never a translation, in the shape of the box's tools/yt-transcript. yt-dlp is the program on the
daemon's PATH, run as watch.pull runs it; the json3 file and the -J answer are deleted once read, so the transcript is the one thing kept.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

from . import transcribe, watch, words
from .config import write_private

PROBE_TIMEOUT = 60           # seconds -J, or the one track's download, may take before yt-dlp is killed with its group
LINE_SPAN, LINE_CHARS = 15, 240      # a transcript line gathers cues for at most this many seconds and characters
INFO = ".info.json"          # the -J answer, in the post's folder while the track is fetched from it
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")      # a caption key --sub-langs can take as itself, never a pattern or a list


def ytdlp(argv: list[str]) -> tuple[int, str, str]:
    """One yt-dlp run as watch.pull runs one, in a clean environment and a process group of its own, killed with its group
    past PROBE_TIMEOUT: its exit code, what it printed and what it said."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": os.environ.get("LANG", "C.UTF-8"), "HOME": os.environ.get("HOME", str(Path.home()))}
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                          errors="replace", env=env, start_new_session=True) as p:
        try:
            out, err = p.communicate(timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            raise watch.Failed(words.WATCH_SLOW.format(seconds=PROBE_TIMEOUT))
    return p.returncode, out, err


def said(err: str) -> str:
    """Its last line as watch.pull says one: no "ERROR: ", none of its advice on its flags, no path."""
    why = re.split(r"\s+Use --|; please report this issue", watch.last(err).removeprefix("ERROR: "), maxsplit=1)[0]
    return words.no_paths(why)[:300] or words.WATCH_NO_WHY


def offered(tracks: Any) -> dict[str, list[str]]:
    """A page's caption dict (a key to its formats) as each key's file types; anything else as none."""
    return {str(k): [str(f.get("ext")) for f in v if isinstance(f, dict)] for k, v in tracks.items()
            if isinstance(v, list)} if isinstance(tracks, dict) else {}


def probe(exe: str, url: str, folder: Path) -> dict[str, Any]:
    """The page's facts by `yt-dlp -J`, nothing downloaded: its title, length, language, date, uploader, live status and
    the caption tracks it offers, kept in memory, and its whole answer written to folder/INFO for pull, which the caller
    deletes. A failed run, an answer that is not a page, or a stream live now or not started yet is Failed."""
    code, out, err = ytdlp([exe, "-J", "--no-playlist", "--no-warnings", "--skip-download", url])
    try:
        page = None if code else json.loads(out)
    except ValueError:
        page = None
    if not isinstance(page, dict):
        raise watch.Failed(said(err))
    if page.get("live_status") in ("is_live", "is_upcoming"):
        raise watch.Failed(words.CAPTIONS_LIVE)
    d = page.get("duration")
    info = {k: page.get(k) for k in ("title", "language", "upload_date", "live_status")} | {
        "duration": float(d) if isinstance(d, (int, float)) and d > 0 else None, "uploader": page.get("uploader") or page.get("channel"),
        "subtitles": offered(page.get("subtitles")), "automatic_captions": offered(page.get("automatic_captions"))}
    write_private(folder / INFO, out)
    return info


def primary(lang: str) -> str:
    return lang.split("-")[0].lower()


def track(info: dict[str, Any]) -> tuple[str | None, str | None]:
    """The one track to read, (kind, key) with kind "manual" or "auto", else (None, None). The language is the page's,
    else the one whose auto track ends -orig when there is exactly one, else en; then its primary subtag (en-US gives en).
    An uploaded track under one of those, else one whose primary subtag is the language's (never live chat), then the
    untranslated auto track, then the plain one. Only a key that offers json3 and is a key, never a pattern or a list."""
    ok = {kind: {k for k, exts in info.get(name, {}).items() if KEY.fullmatch(k) and "json3" in exts and k != "live_chat"}
          for kind, name in (("manual", "subtitles"), ("auto", "automatic_captions"))}
    origs = [k[:-5] for k in info.get("automatic_captions", {}) if k.endswith("-orig")]
    lang = str(info.get("language") or (origs[0] if len(origs) == 1 else "en"))
    cands = [lang] + ([primary(lang)] if primary(lang) != lang else [])
    for kind, keys in (("manual", [*cands, *(k for k in info.get("subtitles", {}) if primary(k) == primary(lang))]),
                       ("auto", [*(f"{c}-orig" for c in cands), *cands])):
        if key := next((k for k in keys if k in ok[kind]), None):
            return kind, key
    return None, None


def pull(exe: str, info_path: Path, key: str, folder: Path) -> Path:
    """The one track `key`, saved by yt-dlp from the -J answer at `info_path` as folder/captions.KEY.json3, else Failed in
    its words."""
    code, out, err = ytdlp([exe, "--load-info-json", str(info_path), "--skip-download", "--no-warnings", "--write-subs",
                            "--write-auto-subs", "--sub-langs", key, "--sub-format", "json3", "-o", f"{folder}/captions"])
    if not (got := folder / f"captions.{key}.json3").is_file() or got.is_symlink():
        raise watch.Failed(said(err))
    return got


def lines(path: Path) -> list[str]:
    """The json3 file's cues as transcript lines, `[HH:MM:SS -> HH:MM:SS] text`, the file deleted once read. A cue's
    pieces are joined and split at its line breaks; a line among the last two said is dropped, a cue that only grew keeps
    its new words and one said again is dropped (YouTube's rolling captions repeat the line before). Cues then gather into
    lines of at most LINE_SPAN seconds and LINE_CHARS characters, each from its first cue's start to its last cue's end."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    finally:
        path.unlink(missing_ok=True)
    cues: list[tuple[float, float, str]] = []
    recent: list[str] = []
    events = data.get("events") if isinstance(data, dict) else None
    for e in events if isinstance(events, list) else []:
        if not isinstance(e, dict) or not isinstance(segs := e.get("segs"), list):
            continue
        new = [re.sub(r"\s+", " ", x).strip() for x in "".join(str(s.get("utf8") or "") for s in segs if isinstance(s, dict)).split("\n")]
        if not (new := [x for x in new if x and x not in recent]):
            continue
        text = " ".join(new)
        if cues and text.startswith(cues[-1][2] + " "):
            text = text[len(cues[-1][2]):].strip()            # a cue that grew by a few words
        if not text or (cues and text == cues[-1][2]):
            continue
        t, d = (e[k] / 1000 if isinstance(e.get(k), (int, float)) else 0.0 for k in ("tStartMs", "dDurationMs"))
        cues.append((t, t + d, text))
        recent = (recent + new)[-2:]
    out: list[list[tuple[float, float, str]]] = []
    for c in cues:
        if not out or c[1] - out[-1][0][0] > LINE_SPAN or len(" ".join(x[2] for x in out[-1])) + 1 + len(c[2]) > LINE_CHARS:
            out.append([])
        out[-1].append(c)
    return [f"[{transcribe.hhmmss(g[0][0])} -> {transcribe.hhmmss(g[-1][1])}] {' '.join(x[2] for x in g)}" for g in out]


def write(video: Path, lines: list[str], info: dict[str, Any], kind: str) -> Path:
    """The transcript beside the video as transcribe.run writes one: the head line (its name, its minutes, 0 when the page
    gave none, and which captions), then a line a span, so a reader never has to know which engine wrote it."""
    d = info.get("duration")
    head = words.TRANSCRIPT_HEAD.format(name=video.name, minutes=round(d / 60, 1) if d else 0,
                                        how=words.FROM_CAPTIONS if kind == "manual" else words.FROM_AUTO_CAPTIONS)
    return write_private(video.with_name(f"{video.stem}.transcript.txt"), head + "\n" + "\n".join(lines) + "\n")


def source(text: str) -> str:
    """What a transcript came from, by its head line: "captions", "auto-captions", "heard" (transcribe.run's), "" for none."""
    head = text.partition("\n")[0]
    return ("captions" if head.endswith(words.FROM_CAPTIONS) else "auto-captions" if head.endswith(words.FROM_AUTO_CAPTIONS)
            else "heard" if text else "")
