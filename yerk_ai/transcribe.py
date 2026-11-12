"""The transcribe tool: a voice note or recording in a room's own folders (its inbox holds what was sent) becomes text with
a timestamp per segment, saved beside the audio as NAME.transcript.txt and returned to the room. Domain tools live outside
the harness as MCP servers (NON-GOALS); this one is a tool on Yerk's own MCP server because the daemon has the network
and a room's shell has none. It is the one transcription engine (decision 15), in the shape of the box's
tools/transcribe-2pass: ffmpeg cuts the audio into CHUNK-second mono mp3s, under both providers' 25 MB upload cap; pass 1
sends them to OpenAI's whisper-1 in parallel (verbose_json) and drops a segment only when it is near silence twice over
(DROP: whisper's "thanks for watching" over a quiet stretch); with a Groq key, pass 2 sends the same chunks to Groq's
whisper-large-v3-turbo, and a segment whose words the two passes disagree on past DIFFER is marked uncertain with both
readings, so a room says it as unsure instead of repeating a made-up word as fact; where pass 1 wrote no words and
pass 2 heard some, pass 2's lines stand in, marked (fill). With a Meta key, Meta's Muse in DIARIZATION mode says who
talks when (the box tool's third engine): WAV windows of WINDOW seconds every STEP, MUSE_PARALLEL at once, short enough
that Meta answers before it cuts a request at about 2 min; a window's letters (A, B) hold for that request only, so they
are matched across windows by the speaking time they share in each overlap (reconcile), and each pass-1 segment takes
the letter covering most of it, so the words and their times stay whisper-1's. One voice writes no letters. The keys
are files in the private folder, the first line of each, read per call and never logged or returned:
OpenAI's is the install's one ([memory] embeddings_key, default secrets/openai.key), Groq's is secrets/groq.key, Meta's
secrets/meta.key. Every paid call is one spend row at the provider's list price (decision 32), and a recording whose
estimate passes GATE_USD waits for the owner's yes.
"""
from __future__ import annotations

import difflib
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
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
BUSY, QUICK = (429, 500, 502, 503, 504), 30    # QUICK seconds: a busy answer inside it is a refusal worth another try (Groq answers a chunk in about 2 s)
PASSES = {                   # engine: (url, model, list price a second, least seconds billed a request), read 2026-10-21
    "openai": ("https://api.openai.com/v1/audio/transcriptions", "whisper-1", 0.006 / 60, 0),       # its pricing page
    "groq": ("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo", 0.04 / 3600, 10),   # its docs
}
MUSE = ("https://api.meta.ai/v1/asr/transcribe", "muse-voice-transcribe-1.0", 0.18 / 3600, 0)   # PASSES' shape: Meta's page
WINDOW, STEP = 120, 60       # seconds, Muse timed 2026-11-12: 8 requests at once took 56 to 62 s on 120 s windows, 84 to 96 s on 180 s,
                             # and Meta cuts one at about 120 s. Neighbours share 60 s, the speech reconcile matches their letters by
MUSE_PARALLEL, MUSE_WAIT = 8, 100    # Muse requests at once, as timed; seconds a window waits for its answer, under Meta's 120 s
NEAR = 3                     # seconds: a segment no turn touches takes the letter of the turn whose middle is this near its own
GROQ_KEY, META_KEY = "secrets/groq.key", "secrets/meta.key"
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


def starts(seconds: float) -> list[float]:
    """Where each Muse window starts: every STEP seconds until one reaches the end, so a recording under WINDOW is one."""
    return [i * STEP for i in range(1 + max(0, math.ceil((seconds - WINDOW) / STEP)))]


def windows(audio: Path, into: Path, at: list[float]) -> list[Path]:
    """The audio as mono 16 kHz 16-bit PCM WAVs of WINDOW seconds from each start in `at`, the one audio Muse takes, cut
    with -ss and -t, since `-f segment` cannot overlap them."""
    if not (exe := shutil.which("ffmpeg")):
        raise Failed("ffmpeg is not installed on this machine")
    out = []
    for i, start in enumerate(at):
        out.append(into / f"w{i:03d}.wav")
        p = subprocess.run([exe, "-v", "error", "-y", "-ss", str(start), "-t", str(WINDOW), "-i", str(audio), "-vn", "-ac", "1", "-ar",
                            "16000", "-c:a", "pcm_s16le", "-map_metadata", "-1", str(out[-1])], capture_output=True, text=True, timeout=900)
        if p.returncode:
            raise Failed(f"ffmpeg could not cut window {i + 1}: {p.stderr.strip()[-160:]}")
    return out


def post(engine: str, secret: str, chunk: Path, timeout: float = 600) -> dict[str, Any]:
    """One chunk to one provider, verbose_json in English as the box tool asks."""
    url, model = PASSES[engine][0], PASSES[engine][1]
    edge = uuid.uuid4().hex
    head = "".join(f'--{edge}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                   for k, v in (("model", model), ("response_format", "verbose_json"), ("language", "en")))
    body = (f'{head}--{edge}\r\nContent-Disposition: form-data; name="file"; filename="{chunk.name}"\r\n'
            "Content-Type: audio/mpeg\r\n\r\n").encode() + chunk.read_bytes() + f"\r\n--{edge}--\r\n".encode()
    return send(url, model, secret, edge, body, timeout)


def muse(secret: str, wav: Path, timeout: float = MUSE_WAIT) -> dict[str, Any]:
    """One window to Meta's Muse in DIARIZATION mode, in the one shape its live API takes (the box tool's _muse_post): a
    `request` part of JSON, an `audio` part of WAV."""
    edge, ask = uuid.uuid4().hex, json.dumps({"model": MUSE[1], "mode": "DIARIZATION", "audioEncoding": "WAV"})
    body = (f'--{edge}\r\nContent-Disposition: form-data; name="request"\r\nContent-Type: application/json\r\n\r\n{ask}\r\n'
            f'--{edge}\r\nContent-Disposition: form-data; name="audio"; filename="{wav.name}"\r\nContent-Type: audio/wav\r\n\r\n'
            ).encode() + wav.read_bytes() + f"\r\n--{edge}--\r\n".encode()
    return send(MUSE[0], MUSE[1], secret, edge, body, timeout)


def send(url: str, model: str, secret: str, edge: str, body: bytes, timeout: float) -> dict[str, Any]:
    """A multipart body to one provider, the key never in the error: three tries on a busy answer inside QUICK seconds, one
    on a request that ran long (a timeout, or an error after QUICK), and the error says its code and seconds, never HTML."""
    last = ""
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {secret}", "User-Agent": "yerk-ai",
                                                              "Content-Type": f"multipart/form-data; boundary={edge}"})
        began = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return dict(json.loads(r.read()))
        except urllib.error.HTTPError as e:
            text = e.read()[:160].decode(errors="replace").strip()
            what, said, again = f"HTTP {e.code}", "" if "<" in text else text, e.code in BUSY
        except (urllib.error.URLError, OSError, ValueError) as e:
            cause = getattr(e, "reason", e)
            what, said, again = type(e).__name__, str(cause), not isinstance(cause, TimeoutError)
        took = time.monotonic() - began
        last = f"{what} after {took:.0f} s" + (f": {said}" if said else "")
        if not again or took > QUICK:
            break
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


def fill(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One chunk's lines where pass 1 wrote no words (whisper-1 can write ♪ through minutes of a call): a pass-2 segment
    with words that shares more time with such pass-1 segments than with ones that have words takes their place, marked
    `second_only`, and a wordless pass-1 segment it overlaps goes. Returns the chunk's lines in time order and the rest of
    pass 2, for check to read against the pass-1 segments left."""
    said = [bool(tokens(f["text"])) for f in first]

    def lean(s: dict[str, Any]) -> float:          # its seconds over wordless pass-1 segments less its seconds over the others
        return sum((-1 if w else 1) * max(0.0, min(s["end"], f["end"]) - max(s["start"], f["start"])) for f, w in zip(first, said))
    took = [bool(tokens(s["text"])) and lean(s) > 0 for s in second]
    only = [dict(s, second_only=True) for s, t in zip(second, took) if t]
    left = [f for f, w in zip(first, said) if w or not any(min(f["end"], s["end"]) > max(f["start"], s["start"]) for s in only)]
    return sorted(left + only, key=lambda s: s["start"]), [s for s, t in zip(second, took) if not t]


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


def turns(data: dict[str, Any], offset: float) -> list[dict[str, Any]]:
    """A Muse answer's turns as {start, end, speaker} in seconds from the recording's start (its startMs and endMs, the
    window's offset added), one with no letter or no words left out; an answer with no list of turns fails, naming what
    it has, so a change in its shape is said, never read as silence."""
    try:
        return [{"start": float(t["startMs"]) / 1000 + offset, "end": float(t["endMs"]) / 1000 + offset, "speaker": str(t["speaker"]).strip()}
                for t in data["turns"] if str(t.get("speaker") or "").strip() and str(t.get("transcript") or "").strip()]
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise Failed(f"its answer did not read as turns ({type(e).__name__} {e}; its keys: {', '.join(sorted(map(str, data)))})") from None


def reconcile(per: list[list[dict[str, Any]]], at: list[float]) -> tuple[list[dict[str, Any]], int]:
    """Each window's turns (seconds from the recording's start, the window's own letters; the windows start at `at`) as
    one list in the recording's letters, and how many letters past window 1 got a new letter, matched by none. Window
    1's letters are the recording's. A later window's letters are matched to the ones they share the most speaking time
    with inside its overlap with the window before, both clipped to it, the pairs taken by that time, most first, one to
    one; a letter left over gets the next letter unused. A turn inside an overlap is kept from the earlier window only."""
    out, prev = list(per[0] if per else []), per[0] if per else []
    used, new = {t["speaker"] for t in out}, 0
    for i in range(1, len(per)):
        lo, hi = at[i], at[i - 1] + WINDOW
        shared = Counter[tuple[str, str]]()
        for a in prev:
            for b in per[i]:
                if (both := min(a["end"], b["end"], hi) - max(a["start"], b["start"], lo)) > 0:
                    shared[(b["speaker"], a["speaker"])] += both
        to: dict[str, str] = {}
        for (mine, theirs), _ in sorted(shared.items(), key=lambda kv: (-kv[1], kv[0])):
            if mine not in to and theirs not in to.values():
                to[mine] = theirs
        for mine in sorted({t["speaker"] for t in per[i]} - set(to)):
            to[mine] = next((c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in used), f"S{len(used) + 1}")
            used.add(to[mine])
            new += 1
        prev = [dict(t, speaker=to[t["speaker"]]) for t in per[i]]
        out += [dict(t, start=max(t["start"], hi)) for t in prev if t["end"] > hi]
    return sorted(out, key=lambda t: (t["start"], t["end"])), new


def speaker(s: dict[str, Any], heard: list[dict[str, Any]]) -> str | None:
    """The letter whose turns cover the most of a segment's span; for a segment no turn touches, the letter of the turn
    whose middle is nearest the segment's, within NEAR seconds, else none."""
    cover = Counter[str]()
    for t in heard:
        if (both := min(s["end"], t["end"]) - max(s["start"], t["start"])) > 0:
            cover[t["speaker"]] += both
    if cover:
        return cover.most_common(1)[0][0]
    mid = (s["start"] + s["end"]) / 2
    near = min(((abs((t["start"] + t["end"]) / 2 - mid), str(t["speaker"])) for t in heard), default=None)
    return near[1] if near and near[0] <= NEAR else None


def hhmmss(t: float) -> str:
    s = max(0, int(t))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def line(s: dict[str, Any]) -> str:
    said = (words.UNCERTAIN.format(first=s["text"], second=s["other"]) if "other" in s else
            words.SECOND_ONLY.format(text=s["text"]) if "second_only" in s else s["text"])
    return f"[{hhmmss(s['start'])} -> {hhmmss(s['end'])}] {s['speaker'] + ': ' if s.get('speaker') else ''}{said}"


def run(audio: Path, openai_key: Path, groq_key: Path, book: Book, owner_yes: bool = False, meta_key: Path | None = None) -> dict[str, Any]:
    """The tool's whole call, blocking (the daemon runs it off the loop): {"ok": False, "error"} on any stop, else the
    transcript's file, text and counts. Pass 2 runs when the Groq key is there; a chunk it failed on stays unchecked.
    The speakers run when the Meta key is there; a window that failed leaves every line unlabelled, so no window starts
    after it, and the file says why."""
    secrets = {e: key(p) for e, p in (("openai", openai_key), ("groq", groq_key), ("meta", meta_key)) if p}
    if not secrets["openai"]:
        return {"ok": False, "error": words.NO_SPEECH_KEY}
    engines, why = [e for e in PASSES if secrets[e]], "" if secrets.get("meta") else words.NO_META_KEY
    try:
        with tempfile.TemporaryDirectory(prefix=".transcribe-", dir=audio.parent) as tmp:
            parts = chunks(audio, Path(tmp))
            secs = [p.stat().st_size / RATE for p in parts]
            at = [] if why else starts(sum(secs))
            wsecs = [min(WINDOW, sum(secs) - s) for s in at]
            usd = {e: [max(s, PASSES[e][3]) * PASSES[e][2] for s in secs] for e in engines}
            usd["meta"] = [max(s, MUSE[3]) * MUSE[2] for s in wsecs]
            if (est := sum(map(sum, usd.values()))) > GATE_USD and not owner_yes:
                return {"ok": False, "error": words.OVER_GATE.format(minutes=round(sum(secs) / 60), usd=est)}
            wins: list[Path] = []
            try:
                wins = windows(audio, Path(tmp), at) if at else []
            except (Failed, OSError, subprocess.SubprocessError) as e:
                why = str(e)
            stop = threading.Event()                   # a Muse window failed: the letters are lost, so no window starts after it

            def one(job: tuple[str, int]) -> tuple[tuple[str, int], dict[str, Any] | Failed | None]:
                e, i = job
                if e == "meta" and stop.is_set():
                    return job, None
                try:
                    data = muse(secrets[e], wins[i]) if e == "meta" else post(e, secrets[e], parts[i])
                except Failed as err:
                    if e == "meta":
                        stop.set()
                    return job, err
                model, what, of, s = (MUSE[1], "window", len(wins), wsecs[i]) if e == "meta" else (PASSES[e][1], "chunk", len(parts), secs[i])
                book(f"{e}/{model}", usd[e][i], f"{what} {i + 1} of {of}, {s:.0f} s")
                return job, data
            with ThreadPoolExecutor(max_workers=PARALLEL) as ex, ThreadPoolExecutor(max_workers=MUSE_PARALLEL) as mx:
                meta = mx.map(one, [("meta", i) for i in range(len(wins))])
                got = dict(ex.map(one, [(e, i) for e in engines for i in range(len(parts))])) | dict(meta)
    except (Failed, OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}
    if bad := [f"chunk {i + 1}: {r}" for i in range(len(parts)) if isinstance(r := got[("openai", i)], Failed)]:
        return {"ok": False, "error": "; ".join(bad)}
    segs: list[dict[str, Any]] = []
    unchecked = []
    for i in range(len(parts)):
        first = kept(r, i * CHUNK) if isinstance(r := got[("openai", i)], dict) else []
        if isinstance(second := got.get(("groq", i)), dict):
            first, rest = fill(first, kept(second, i * CHUNK))
            check([s for s in first if "second_only" not in s], rest)
        elif "groq" in engines:
            unchecked.append(str(i + 1))
        segs += first
    if lost := [i for i in range(len(wins)) if isinstance(got[("meta", i)], Failed)]:
        unsent = sum(got[("meta", i)] is None for i in range(len(wins)))
        why = (words.MUSE_FAILED.format(windows=", ".join(str(i + 1) for i in lost), of=len(wins), error=got[("meta", lost[0])])
               + (words.MUSE_UNSENT.format(n=unsent) if unsent else ""))
    try:                                           # with no window lost, every answer is there to read
        heard, new = ([], 0) if why else reconcile([turns(r, at[i]) for i in range(len(wins)) if isinstance(r := got[("meta", i)], dict)], at)
    except Failed as e:
        heard, new, why = [], 0, str(e)
    said = Counter[str]()
    for t in heard:
        said[t["speaker"]] += t["end"] - t["start"]
    for s in segs if len(said) > 1 else []:
        if who := speaker(s, heard):
            s["speaker"] = who
    talk = (words.NO_SPEAKERS.format(why=why or words.NO_TURNS) if why or not said else words.ONE_VOICE if len(said) == 1 else
            words.SPEAKERS.format(who=", ".join(f"{k} {int(v) // 60}:{int(v) % 60:02d}" for k, v in sorted(said.items())))
            + (words.UNMATCHED.format(n=new) if new else ""))
    n, only, text = sum("other" in s for s in segs), sum("second_only" in s for s in segs), "\n".join(line(s) for s in segs)
    how = (words.UNCHECKED if "groq" not in engines else
           words.PART_CHECKED.format(chunks=", ".join(unchecked), n=n, of=len(segs) - only) if unchecked else
           words.CHECKED.format(n=n, of=len(segs) - only)) + (words.SECOND_ONLY_HEAD.format(n=only) if only else "")
    head = words.TRANSCRIPT_HEAD.format(name=audio.name, minutes=round(sum(secs) / 60, 1), how=how)
    top = "" if talk == words.ONE_VOICE else f"{talk}\n"           # one voice: the file as it was before speakers
    out = write_private(audio.with_name(f"{audio.stem}.transcript.txt"), f"{head}\n{top}{text}\n")
    shown = text if len(text) <= RESULT_CHARS else text[:text.rfind("\n", 0, RESULT_CHARS)] + "\n" + words.MORE_IN_FILE
    return {"ok": True, "file": str(out), "about": head, "uncertain": n, "segments": len(segs), "text": shown,
            "speakers": {k: round(v) for k, v in sorted(said.items())}, "speakers_how": talk, "next": words.TRANSCRIBED_NEXT}
