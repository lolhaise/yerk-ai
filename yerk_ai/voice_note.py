"""The voice_note tool: a script a room writes becomes a spoken voice note, an ogg in the room's own work folder that `send`
posts as a Discord voice message. Domain tools live outside the harness as MCP servers (NON-GOALS); this one is a tool on
Yerk's own MCP server because the daemon has the network and a room's shell has none, as transcribe is. It is the box's
tools/voice-note recipe: every section on Fish Audio, and a failure on any section means no note; then loudness to -14 LUFS
and opus at 48 kHz in ogg. A speech call's success is no receipt, so before a note is handed back its first PROBE seconds
are read back by whisper-1 (transcribe's pass-1 call) and must match the script's opening; a note that does not is deleted.
English only: the voice and the check are English. The keys are files in the private folder, the first line of each, read
per call and never logged or returned: secrets/fish.key, and the install's OpenAI key for the check. Every paid call is one
spend row at the provider's list price, and a note whose estimate passes GATE_USD waits for the owner's yes (decision 32).
"""
from __future__ import annotations

import base64
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path
from typing import Any

from . import transcribe, words
from .config import write_private

# engine: (url, model, voice, list price in US$ a billed unit), read 2026-10-22: Fish bills UTF-8 bytes (its pricing page:
# s2.1-pro-free US$0)
ENGINES = {
    "fish": ("https://api.fish.audio/v1/tts", "s2.1-pro-free", "ca4ec67b92ae4d90829eb40dcd672312", 0.0),
}
KEYS = {"fish": "secrets/fish.key"}
UNIT = {"fish": "bytes"}
MAX_CHUNK = 4000                    # characters a section, the box tool's
SHORT = 50                          # a section shorter than this joins the next with a (break): a voice glues a syllable to it
SILENCE = 0.5                       # seconds between sections
LOUDNORM = "loudnorm=I=-14:TP=-1.0:LRA=11"
PROBE = 20                          # seconds of the note read back before it is handed back
MATCH = 0.75                        # the least share of the probe's words that must line up with the script's opening
FOLDER = "voice"                    # under the room's work folder
WAVE = 256                          # Discord's most waveform points, one byte each, at most one a 0.1 s
GATE_USD = transcribe.GATE_USD
# interjection tags and the box's markup: never spoken as words, so never compared as words
TAGS = frozenset("break pause long-break laughs chuckle coughs clear-throat groans breath pant inhale exhale gasps sniffs "
                 "sighs snorts burps lip-smacking humming hissing emm sneezes ha haha hah heh".split())


class Failed(Exception):
    """A step that stops the note, in words a room can say."""


def sections(text: str) -> list[str]:
    """The script as the calls it takes: `[[pause]]` alone on a line splits it, a section over MAX_CHUNK splits again at its
    paragraphs, and a section under SHORT characters joins the next one (the last, the one before) with a (break)."""
    out: list[str] = []
    for part in (p.strip() for p in re.split(r"^\s*\[\[pause\]\]\s*$", text, flags=re.M)):
        buf = ""
        for para in part.split("\n\n") if len(part) > MAX_CHUNK else [part]:
            if buf and len(buf) + len(para) + 2 > MAX_CHUNK:
                out.append(buf)
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            out.append(buf.strip())
    joined: list[str] = []
    for s in out:
        if joined and len(joined[-1]) < SHORT:
            joined[-1] = f"{joined[-1]} (break) {s}"
        else:
            joined.append(s)
    if len(joined) > 1 and len(joined[-1]) < SHORT:
        last = joined.pop()
        joined[-1] = f"{joined[-1]} (break) {last}"
    return joined


def for_fish(text: str) -> str:
    """Fish's text: a <#0.4#> pause written into a script becomes its (break); [mood] tags pass through, Fish acts on them."""
    return re.sub(r"<#[\d.]+#>", "(break)", text).strip()


def said(text: str) -> list[str]:
    """The words a listener hears, as transcribe compares them: markup, mood tags and interjections left out."""
    t = re.sub(r"\[\[pause\]\]|\[[^\]]{1,40}\]|<#[\d.]+#>|\(([a-z-]+)\)", lambda m: "" if m.group(1) in TAGS or m.group(1) is None
               else m.group(0), text)
    return [n for _, n in transcribe.tokens(t) if n not in TAGS]


def match(heard: list[str], script: list[str]) -> tuple[float, list[str]]:
    """(share, stray): the share of the probe's words that line up, in order, with the script's opening, and the words heard
    between two runs of at least two matching words that the script does not have (a syllable a voice glued on, the box's
    measured defect)."""
    if not heard:
        return 0.0, []
    ops = difflib.SequenceMatcher(None, heard, script[:len(heard) + 15], autojunk=False).get_opcodes()
    same = sum(i2 - i1 for tag, i1, i2, _, _ in ops if tag == "equal")
    stray = [w for k, (tag, i1, i2, _, _) in enumerate(ops) if tag == "delete" and 0 < k < len(ops) - 1
             and all(ops[n][0] == "equal" and ops[n][2] - ops[n][1] >= 2 for n in (k - 1, k + 1)) for w in heard[i1:i2]]
    return same / len(heard), stray


def call(url: str, body: dict[str, Any], secret: str, headers: dict[str, str] | None = None, timeout: float = 300) -> bytes:
    """One POST and its answer's bytes; any failure is Failed, the key masked."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {secret}", "Content-Type": "application/json", "User-Agent": "yerk-ai", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bytes(r.read())
    except urllib.error.HTTPError as e:
        why = f"HTTP {e.code} {e.read()[:160].decode(errors='replace')}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        why = f"{type(e).__name__}: {e}"
    raise Failed(" ".join(why.replace(secret, "<key>").split()))


def fish(secret: str, text: str, out: Path) -> int:
    """One section on Fish as mp3; the UTF-8 bytes it bills."""
    url, model, voice, _ = ENGINES["fish"]
    t = for_fish(text)
    data = call(url, {"text": t, "format": "mp3", "reference_id": voice}, secret, {"model": model})
    if len(data) < 1000:
        raise Failed("Fish answered with no audio")
    out.write_bytes(data)
    return len(t.encode())


def synth(engine: str, secret: str, secs: list[str], tmp: Path, book: transcribe.Book) -> list[Path]:
    """Every section on one engine, all or nothing: a failed section raises, and there is no note."""
    parts = []
    for i, s in enumerate(secs):
        p = tmp / f"{engine}-{i:02d}.mp3"
        n = fish(secret, s, p)
        book(f"{engine}/{ENGINES[engine][1]}", n * ENGINES[engine][3], f"section {i + 1} of {len(secs)}, {n:,} {UNIT[engine]}")
        parts.append(p)
    return parts


def ffmpeg(*args: str) -> None:
    if not (exe := shutil.which("ffmpeg")):
        raise Failed("ffmpeg is not installed on this machine")
    p = subprocess.run([exe, "-v", "error", "-y", *args], capture_output=True, text=True, timeout=900)
    if p.returncode:
        raise Failed(f"ffmpeg failed: {p.stderr.strip()[-160:]}")


def assemble(parts: list[Path], out: Path, tmp: Path) -> None:
    """The sections joined by SILENCE seconds, loudness to -14 LUFS, then opus at 48 kHz mono in ogg, as the box tool does."""
    wavs = []
    for i, p in enumerate(parts):
        wavs.append(w := tmp / f"part-{i:02d}.wav")
        ffmpeg("-i", str(p), "-ar", "48000", "-ac", "1", str(w))
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", str(SILENCE), str(sil := tmp / "silence.wav"))
    (lst := tmp / "list.txt").write_text("".join((f"file '{sil}'\n" if i else "") + f"file '{w}'\n" for i, w in enumerate(wavs)))
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined := tmp / "joined.wav"))
    ffmpeg("-i", str(joined), "-af", LOUDNORM, "-ar", "48000", "-ac", "1", "-c:a", "libopus", "-b:a", "48k", str(out))


def duration(path: Path) -> float:
    exe = shutil.which("ffprobe") or "ffprobe"
    out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, timeout=60).stdout
    try:
        return float(out.strip())
    except ValueError:
        raise Failed("ffprobe could not read the note's length") from None


def waveform(path: Path, seconds: float) -> str:
    """Discord's waveform: up to WAVE points, one a 0.1 s at most, each the mean loudness of its stretch of 8 kHz audio scaled
    so the loudest is 255, base64 (the box's own Discord send samples the same way)."""
    pcm = subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar",
                          "8000", "-"], capture_output=True, timeout=120).stdout
    x = array("h", pcm[:len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        x.byteswap()
    n = max(1, min(WAVE, int(seconds * 10)))
    step = max(1, len(x) // n)
    means = [sum(map(abs, x[i * step:(i + 1) * step])) / step for i in range(n)]
    top = max(means) or 1.0
    return base64.b64encode(bytes(min(255, round(m / top * 255)) for m in means)).decode()


def sidecar(ogg: Path) -> Path:
    return ogg.with_name(f"{ogg.stem}.voice.json")


def meta(path: Path) -> dict[str, Any] | None:
    """{duration, waveform} when `path` is a note this tool made and it is unchanged since (its sidecar's byte count), else
    None: `send` posts such a file as a Discord voice message, anything else as a plain file."""
    try:
        m = json.loads(sidecar(path).read_text())
        wave = base64.b64decode(str(m["waveform"]), validate=True)
        ok = path.suffix == ".ogg" and int(m["bytes"]) == path.stat().st_size and float(m["duration"]) > 0 and 0 < len(wave) <= WAVE
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {"duration": round(float(m["duration"]), 2), "waveform": str(m["waveform"])} if ok else None


def target(folder: Path, name: str) -> Path:
    """The note's path: `name` cut to letters, digits, dot, dash and underscore (a time stamp when none is left), never over
    a note already there."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem if name else "").strip("-.")[:60] or time.strftime("voice-%Y%m%d-%H%M%S")
    p, n = folder / f"{stem}.ogg", 1
    while p.exists():
        n += 1
        p = folder / f"{stem}-{n}.ogg"
    return p


def run(script: str, name: str, folder: Path, private: Path, openai_key: Path, book: transcribe.Book,
        owner_yes: bool = False) -> dict[str, Any]:
    """The tool's whole call, blocking (the daemon runs it off the loop): {"ok": False, "error"} on any stop, else the note's
    file, its length, the engine, the probe's words and the cost. Nothing is paid before the keys and the gate pass."""
    secs, words_said = sections(script), said(script)
    if not secs or not words_said:
        return {"ok": False, "error": words.VOICE_EMPTY}
    used = "fish"
    if not (secret := transcribe.key(private / KEYS[used])):
        return {"ok": False, "error": words.VOICE_NO_KEY}
    if not (checker := transcribe.key(openai_key)):
        return {"ok": False, "error": words.VOICE_NO_CHECK}
    size = sum(len(for_fish(s).encode()) for s in secs)
    if (est := size * ENGINES[used][3] + PROBE * transcribe.PASSES["openai"][2]) > GATE_USD and not owner_yes:
        return {"ok": False, "error": words.VOICE_OVER_GATE.format(chars=size, usd=est)}
    paid: list[float] = []

    def pay(engine: str, usd: float, note: str) -> None:
        paid.append(usd)
        book(engine, usd, note)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".voice-", dir=folder) as d:
            tmp = Path(d)
            try:
                parts = synth(used, secret, secs, tmp, pay)
            except Failed as e:
                return {"ok": False, "error": words.VOICE_NO_ENGINE.format(why=f"{used}: {e}")}
            assemble(parts, note := tmp / "note.ogg", tmp)
            seconds = duration(note)
            ffmpeg("-i", str(note), "-t", str(PROBE), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(clip := tmp / "probe.mp3"))
            heard = " ".join(s["text"] for s in transcribe.kept(transcribe.post("openai", checker, clip), 0)).strip()
            pay("openai/whisper-1", min(seconds, PROBE) * transcribe.PASSES["openai"][2], f"check, first {min(seconds, PROBE):.0f} s")
            share, stray = match(said(heard), words_said)
            if share < MATCH or stray:
                opening = " ".join(re.sub(r"\[\[pause\]\]|<#[\d.]+#>|\[[^\]]{1,40}\]|\((?:break|pause)\)", " ", script).split()[
                    :len(heard.split()) or 12])
                return {"ok": False, "error": words.VOICE_PROBE_FAILED.format(heard=heard[:240] or words.NO_WORDS, said=opening[:240]),
                        "engine": f"{used}/{ENGINES[used][1]}", "usd": round(sum(paid), 4)}
            out = target(folder, name)
            shutil.move(note, out)
        write_private(sidecar(out), json.dumps({"duration": round(seconds, 2), "waveform": waveform(out, seconds), "bytes": out.stat().st_size}))
    except (Failed, transcribe.Failed, OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e), "usd": round(sum(paid), 4)}
    return {"ok": True, "file": str(out), "duration_s": round(seconds, 1), "engine": f"{used}/{ENGINES[used][1]}", "probe": heard,
            "usd": round(sum(paid), 4), "sections": len(secs), "next": words.VOICE_NEXT}
