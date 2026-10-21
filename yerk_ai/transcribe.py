"""The transcribe tool: a voice note or recording in a room's own folders (its inbox holds what was sent) becomes text with
a timestamp per segment, saved beside the audio as NAME.transcript.txt and returned to the room. Domain tools live outside
the harness as MCP servers (NON-GOALS); this one is a tool on Yerk's own MCP server because the daemon has the network
and a room's shell has none. It is the one transcription engine (decision 15), in the shape of the box's
tools/transcribe-2pass: ffmpeg cuts the audio into CHUNK-second mono mp3s, under both providers' 25 MB upload cap; pass 1
sends them to OpenAI's whisper-1 in parallel (verbose_json) and drops a segment only when it is near silence twice over
(DROP: whisper's "thanks for watching" over a quiet stretch); with a Groq key, pass 2 sends the same chunks to Groq's
whisper-large-v3-turbo, and a segment whose words the two passes disagree on past DIFFER is marked uncertain with both
readings, so a room says it as unsure instead of repeating a made-up word as fact. The keys are files in the private
folder, the first line of each, read per call and never logged or returned: OpenAI's is the install's one
([memory] embeddings_key, default secrets/openai.key), Groq's is secrets/groq.key. Every paid call is one spend row at
the provider's list price (decision 32), and a recording whose estimate passes GATE_USD waits for the owner's yes.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import words
from .config import write_private

EXTS = (".ogg", ".oga", ".opus", ".m4a", ".mp3", ".wav", ".webm")     # with any audio/* type: a voice note or recording
CHUNK = 840                  # seconds: 14 min of 64 kbit/s mono mp3 is 6.7 MB, under both providers' 25 MB upload cap
RATE = 8000                  # bytes a second of that mp3, so a chunk's length is read from its size
DROP = (0.9, -1.0)           # no_speech_prob over the first and avg_logprob under the second: both, or the segment stays
DIFFER = 0.3                 # a segment is uncertain when the passes disagree on more than this share of its words
GATE_USD = 1.0               # decision 32: a call estimated past this waits for the owner's yes
PARALLEL = 8
BUSY = (429, 500, 502, 503, 504)
PASSES = {                   # engine: (url, model, list price a second, least seconds billed a request), read 2026-10-21
    "openai": ("https://api.openai.com/v1/audio/transcriptions", "whisper-1", 0.006 / 60, 0),       # its pricing page
    "groq": ("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo", 0.04 / 3600, 10),   # its docs
}
GROQ_KEY = "secrets/groq.key"
RESULT_CHARS = 40_000        # the most of the text one result carries, under Claude Code's cap on a tool's answer
WORD = re.compile(r"[a-z0-9']+")
FILLERS = frozenset("um umm uh uhh uhm hmm mm erm".split())    # whisper-1 writes them, Groq's model leaves them out
Book = Callable[[str, float, str], None]     # (engine, usd, note): one spend row


class Failed(Exception):
    """A step that stops the call, in words a room can say."""


def is_audio(name: str, kind: str = "") -> bool:
    return Path(name).suffix.lower() in EXTS or kind.startswith("audio/")


def key(path: Path) -> str:
    """A key file's first line, or "" when it is missing or empty."""
    try:
        return path.read_text().split("\n", 1)[0].strip()
    except (OSError, UnicodeDecodeError):
        return ""


def chunks(audio: Path, into: Path) -> list[Path]:
    """The audio as CHUNK-second mono 16 kHz 64 kbit/s mp3s, in order, cut as tools/transcribe-2pass cuts it."""
    if not (exe := shutil.which("ffmpeg")):
        raise Failed("ffmpeg is not installed on this machine")
    p = subprocess.run([exe, "-v", "error", "-y", "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", "-f",
                        "segment", "-segment_time", str(CHUNK), str(into / "c%03d.mp3")], capture_output=True, text=True, timeout=900)
    if p.returncode or not (out := sorted(into.glob("c*.mp3"))):
        raise Failed(f"ffmpeg could not read it as audio: {p.stderr.strip()[-160:] or 'no audio in it'}")
    return out


def post(engine: str, secret: str, chunk: Path, timeout: float = 600) -> dict[str, Any]:
    """One chunk to one provider, verbose_json in English as the box tool asks, three tries on a busy answer."""
    url, model = PASSES[engine][0], PASSES[engine][1]
    edge = uuid.uuid4().hex
    head = "".join(f'--{edge}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                   for k, v in (("model", model), ("response_format", "verbose_json"), ("language", "en")))
    body = (f'{head}--{edge}\r\nContent-Disposition: form-data; name="file"; filename="{chunk.name}"\r\n'
            "Content-Type: audio/mpeg\r\n\r\n").encode() + chunk.read_bytes() + f"\r\n--{edge}--\r\n".encode()
    last = ""
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {secret}", "User-Agent": "yerk-ai",
                                                              "Content-Type": f"multipart/form-data; boundary={edge}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return dict(json.loads(r.read()))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.read()[:160].decode(errors='replace')}"
            if e.code not in BUSY:
                break
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < 3:
            time.sleep(5 * attempt)
    raise Failed(f"{model} did not answer: {' '.join(last.replace(secret, '<key>').split())}")


def kept(data: dict[str, Any], offset: float) -> list[dict[str, Any]]:
    """A pass's segments, the chunk's offset added, less the near-silence ones whisper makes up (both DROP marks)."""
    return [{"start": float(s["start"]) + offset, "end": float(s["end"]) + offset, "text": str(s.get("text") or "").strip()}
            for s in data.get("segments") or []
            if not (float(s.get("no_speech_prob") or 0) > DROP[0] and float(s.get("avg_logprob") or 0) < DROP[1])]


def tokens(text: str) -> list[tuple[str, str]]:
    """(as written, as compared) per word: compared in lower case, by its letters, digits and apostrophes, a filler never."""
    return [(raw, n) for raw in text.split() if (n := "".join(WORD.findall(raw.lower().replace("’", "'")))) and n not in FILLERS]


def check(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> None:
    """Marks each pass-1 segment of one chunk whose words pass 2 reads differently past DIFFER, with `other`, pass 2's
    reading of the same stretch. The two are aligned word by word across the whole chunk, so a segment boundary the two
    passes put in different places is no disagreement; words only pass 2 heard count against the segment before them."""
    a = [(k, n) for k, s in enumerate(first) for _, n in tokens(s["text"])]
    b = [t for s in second for t in tokens(s["text"])]
    if not a:
        return
    seg, size, differ = [k for k, _ in a], Counter(k for k, _ in a), Counter[int]()
    at = [0] * len(a) + [len(b)]                 # where each pass-1 word falls in pass 2's words
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [n for _, n in a], [n for _, n in b], autojunk=False).get_opcodes():
        for i in range(i1, i2):
            at[i] = j1 + (i - i1) * (j2 - j1) // (i2 - i1)
            differ[seg[i]] += tag != "equal"
        if (extra := (j2 - j1) - (i2 - i1)) > 0 and tag != "equal":
            differ[seg[max(i2 - 1, 0)]] += extra
    starts = {k: i for i, k in reversed(list(enumerate(seg)))}
    order = sorted(starts)
    for n, k in enumerate(order):
        if differ[k] / size[k] > DIFFER:
            lo, hi = 0 if n == 0 else at[starts[k]], at[starts[order[n + 1]]] if n + 1 < len(order) else len(b)
            first[k]["other"] = " ".join(raw for raw, _ in b[lo:hi]) or words.NO_WORDS


def hhmmss(t: float) -> str:
    s = max(0, int(t))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def line(s: dict[str, Any]) -> str:
    said = words.UNCERTAIN.format(first=s["text"], second=s["other"]) if "other" in s else s["text"]
    return f"[{hhmmss(s['start'])} -> {hhmmss(s['end'])}] {said}"


def run(audio: Path, openai_key: Path, groq_key: Path, book: Book, owner_yes: bool = False) -> dict[str, Any]:
    """The tool's whole call, blocking (the daemon runs it off the loop): {"ok": False, "error"} on any stop, else the
    transcript's file, text and counts. Pass 2 runs when the Groq key is there; a chunk it failed on stays unchecked."""
    secrets = {e: key(p) for e, p in (("openai", openai_key), ("groq", groq_key))}
    if not secrets["openai"]:
        return {"ok": False, "error": words.NO_SPEECH_KEY}
    engines = [e for e in PASSES if secrets[e]]
    try:
        with tempfile.TemporaryDirectory(prefix=".transcribe-", dir=audio.parent) as tmp:
            parts = chunks(audio, Path(tmp))
            secs = [p.stat().st_size / RATE for p in parts]
            usd = {e: [max(s, PASSES[e][3]) * PASSES[e][2] for s in secs] for e in engines}
            if (est := sum(map(sum, usd.values()))) > GATE_USD and not owner_yes:
                return {"ok": False, "error": words.OVER_GATE.format(minutes=round(sum(secs) / 60), usd=est)}

            def one(job: tuple[str, int]) -> tuple[tuple[str, int], dict[str, Any] | Failed]:
                (e, i), note = job, f"chunk {job[1] + 1} of {len(parts)}, {secs[job[1]]:.0f} s"
                try:
                    data = post(e, secrets[e], parts[i])
                except Failed as err:
                    return job, err
                book(f"{e}/{PASSES[e][1]}", usd[e][i], note)
                return job, data
            with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
                got = dict(ex.map(one, [(e, i) for e in engines for i in range(len(parts))]))
    except (Failed, OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}
    if bad := [f"chunk {i + 1}: {r}" for i in range(len(parts)) if isinstance(r := got[("openai", i)], Failed)]:
        return {"ok": False, "error": "; ".join(bad)}
    segs: list[dict[str, Any]] = []
    unchecked = []
    for i in range(len(parts)):
        first = kept(r, i * CHUNK) if isinstance(r := got[("openai", i)], dict) else []
        if isinstance(second := got.get(("groq", i)), dict):
            check(first, kept(second, i * CHUNK))
        elif "groq" in engines:
            unchecked.append(str(i + 1))
        segs += first
    n, text = sum("other" in s for s in segs), "\n".join(line(s) for s in segs)
    how = (words.UNCHECKED if "groq" not in engines else
           words.PART_CHECKED.format(chunks=", ".join(unchecked), n=n, of=len(segs)) if unchecked else
           words.CHECKED.format(n=n, of=len(segs)))
    head = words.TRANSCRIPT_HEAD.format(name=audio.name, minutes=round(sum(secs) / 60, 1), how=how)
    out = write_private(audio.with_name(f"{audio.stem}.transcript.txt"), f"{head}\n{text}\n")
    shown = text if len(text) <= RESULT_CHARS else text[:text.rfind("\n", 0, RESULT_CHARS)] + "\n" + words.MORE_IN_FILE
    return {"ok": True, "file": str(out), "about": head, "uncertain": n, "segments": len(segs), "text": shown,
            "next": words.TRANSCRIBED_NEXT}
