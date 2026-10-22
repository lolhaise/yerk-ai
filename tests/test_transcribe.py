"""The transcribe tool (transcribe.py), offline: the chunking, the near-silence drop rule, the cross-check's marks, a whole
call against fake providers (its spend rows, the gate, the keys, a failed second pass), the arrival line, the bounce of a
note never transcribed and the morning's spend line. One test transcribes a real 3-second fixture, behind
YERK_TRANSCRIBE_LIVE=1 with a key in $YERK_PRIVATE/secrets/openai.key."""
from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from yerk_ai import shape, transcribe, words
from yerk_ai.channels.discord import message_text
from yerk_ai.daemon import ConsoleChannel, Yerkd
from yerk_ai.engine import RoomProcess
from yerk_ai.ledger import Ledger
from yerk_ai.room import Room

EXAMPLE_PRIVATE = Path(__file__).resolve().parents[1] / "examples" / "private"
FFMPEG = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def seg(start: float, end: float, text: str, no_speech: float = 0.01, logprob: float = -0.2) -> dict[str, object]:
    return {"start": start, "end": end, "text": f" {text}", "no_speech_prob": no_speech, "avg_logprob": logprob}


def tone(path: Path, seconds: float) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", str(path)], check=True)
    return path


@FFMPEG
def test_audio_is_cut_into_mono_mp3_chunks_of_chunk_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "CHUNK", 1)
    parts = transcribe.chunks(tone(tmp_path / "note.wav", 2.5), tmp_path)
    assert [p.name for p in parts] == ["c000.mp3", "c001.mp3", "c002.mp3"]
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=channels,sample_rate", "-of", "csv=p=0", str(parts[0])],
                           capture_output=True, text=True).stdout.split()
    assert probe == ["16000,1"]                                       # mono 16 kHz, as the box tool sends it
    assert abs(sum(p.stat().st_size for p in parts) / transcribe.RATE - 2.5) < 0.5       # its length, read from its size
    with pytest.raises(transcribe.Failed, match="could not read it as audio"):
        transcribe.chunks((tmp_path / "x.ogg").resolve(), tmp_path / "none")


def test_a_segment_is_dropped_only_when_it_is_near_silence_twice_over():
    data = {"segments": [seg(0, 2, "Hello there."), seg(2, 4, "Thanks for watching!", 0.95, -1.4),
                         seg(4, 6, "quiet but sure", 0.95, -0.3), seg(6, 8, "loud but unsure", 0.2, -1.6)]}
    out = transcribe.kept(data, 840)
    assert [s["text"] for s in out] == ["Hello there.", "quiet but sure", "loud but unsure"]
    assert out[0]["start"] == 840 and out[2]["end"] == 848            # the chunk's offset added


def test_the_cross_check_marks_only_a_real_disagreement_with_both_readings():
    first = [{"start": 0, "end": 3, "text": "We start with Northdale on Monday."}, {"start": 3, "end": 6, "text": "um"},
             {"start": 6, "end": 9, "text": "Then the garden tour goes"}, {"start": 9, "end": 12, "text": "to all thirty plots, okay?"}]
    second = [{"start": 0, "end": 3, "text": "We start with Northfield on Wednesday."},          # two of six words differ
              {"start": 3, "end": 10, "text": "Then the garden tour goes to all"},          # the boundary in another place
              {"start": 10, "end": 12, "text": "thirty plots. Okay?"}]
    transcribe.check(first, second)
    assert first[0]["other"] == "We start with Northfield on Wednesday."
    assert ["other" in s for s in first[1:]] == [False, False, False]   # a filler and a moved boundary are no disagreement
    assert transcribe.line(first[0]) == ("[00:00:00 -> 00:00:03] (uncertain) We start with Northdale on Monday. | second pass: "
                                         "We start with Northfield on Wednesday.")
    alone = [{"start": 0, "end": 2, "text": "I said seventeen"}]
    transcribe.check(alone, [])
    assert alone[0]["other"] == words.NO_WORDS                         # the second pass heard nothing there


def fake_chunks(sizes: list[int]):
    def cut(audio: Path, into: Path) -> list[Path]:
        out = [into / f"c{i:03d}.mp3" for i in range(len(sizes))]
        for p, n in zip(out, sizes):
            p.write_bytes(b"\0" * n)
        return out
    return cut


def setup(tmp_path, monkeypatch, sizes, answers, groq: str | None = "gsk_test_groq_key_123456"):
    """A private folder's keys, an audio file in an inbox, fake chunks of `sizes` bytes and fake provider `answers`
    ({(engine, chunk): data or an exception}); returns (audio, keys, spend rows, the calls made)."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "openai.key").write_text("sk-test-openai-key-123456\nsecond line ignored\n")
    if groq:
        (tmp_path / "secrets" / "groq.key").write_text(groq + "\n")
    audio = tmp_path / "inbox" / "77" / "voice-message.ogg"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"OggS")
    calls: list[tuple[str, str, str]] = []
    rows: list[tuple[str, float, str]] = []

    def post(engine, secret, chunk, timeout=600):
        calls.append((engine, secret, chunk.name))
        got = answers[(engine, int(chunk.stem[1:]))]
        if isinstance(got, Exception):
            raise got
        return got
    monkeypatch.setattr(transcribe, "chunks", fake_chunks(sizes))
    monkeypatch.setattr(transcribe, "post", post)
    return audio, (tmp_path / "secrets" / "openai.key", tmp_path / "secrets" / "groq.key"), rows, calls


def test_a_call_runs_both_passes_writes_the_file_and_books_a_spend_row_per_paid_call(tmp_path, monkeypatch):
    one = {"segments": [seg(0, 4, "Call Nora about the rollout."), seg(4, 6, "Thanks for watching!", 0.97, -1.2)]}
    two = {"segments": [seg(0, 3, "Then email the vendor"), seg(3, 5, "by Friday.")]}
    answers = {("openai", 0): one, ("openai", 1): two, ("groq", 0): {"segments": [seg(0, 4, "Call Norah about the rollout.")]},
               ("groq", 1): {"segments": [seg(0, 5, "Ten email a vendor by Friday.")]}}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 840, 8000 * 5], answers)
    r = transcribe.run(audio, *keys, lambda e, usd, note: rows.append((e, usd, note)))
    assert r["ok"] and r["segments"] == 3 and r["uncertain"] == 1 and r["next"] == words.TRANSCRIBED_NEXT
    assert sorted(calls) == [("groq", "gsk_test_groq_key_123456", "c000.mp3"), ("groq", "gsk_test_groq_key_123456", "c001.mp3"),
                             ("openai", "sk-test-openai-key-123456", "c000.mp3"), ("openai", "sk-test-openai-key-123456", "c001.mp3")]
    saved = Path(r["file"]).read_text()
    assert Path(r["file"]) == audio.with_name("voice-message.transcript.txt") and Path(r["file"]).stat().st_mode & 0o777 == 0o600
    assert saved.splitlines() == [
        "voice-message.ogg, 14.1 min, checked by a second pass: 1 of 3 segments uncertain",
        "[00:00:00 -> 00:00:04] Call Nora about the rollout.",
        "[00:14:00 -> 00:14:03] (uncertain) Then email the vendor | second pass: Ten email a vendor",
        "[00:14:03 -> 00:14:05] by Friday."]
    assert r["text"] == "\n".join(saved.splitlines()[1:]) and "sk-test" not in str(r) and "gsk_" not in str(r)
    assert sorted((e, round(u, 6), n) for e, u, n in rows) == sorted([
        ("groq/whisper-large-v3-turbo", 0.009333, "chunk 1 of 2, 840 s"), ("groq/whisper-large-v3-turbo", 0.000111, "chunk 2 of 2, 5 s"),
        ("openai/whisper-1", 0.084, "chunk 1 of 2, 840 s"), ("openai/whisper-1", 0.0005, "chunk 2 of 2, 5 s")])  # Groq bills 10 s at least
    assert not list(audio.parent.glob(".transcribe-*"))                # the chunks went with the call


def test_without_a_groq_key_one_pass_runs_and_the_file_says_nothing_was_checked(tmp_path, monkeypatch):
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 30], {("openai", 0): {"segments": [seg(0, 2, "Hi.")]}}, groq=None)
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a))
    assert r["ok"] and [c[0] for c in calls] == ["openai"] and len(rows) == 1
    assert r["about"] == f"voice-message.ogg, 0.5 min, {words.UNCHECKED}"


def test_no_openai_key_and_a_call_past_the_gate_spend_nothing(tmp_path, monkeypatch):
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 840] * 11, {})
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a))       # 154 min: US$1.03 with both passes
    assert not r["ok"] and r["error"] == words.OVER_GATE.format(minutes=154, usd=1.026667) and not calls and not rows
    keys[0].write_text("\n")
    assert transcribe.run(audio, *keys, lambda *a: rows.append(a)) == {"ok": False, "error": words.NO_SPEECH_KEY}
    assert not calls and not rows


def test_the_owners_yes_releases_the_gate(tmp_path, monkeypatch):
    answers = {(e, i): {"segments": []} for e in ("openai", "groq") for i in range(11)}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 840] * 11, answers)
    assert transcribe.run(audio, *keys, lambda *a: rows.append(a), owner_yes=True)["ok"] and len(calls) == len(rows) == 22


def test_a_failed_second_pass_leaves_its_chunk_unchecked_and_a_failed_first_pass_fails_the_call(tmp_path, monkeypatch):
    answers = {("openai", 0): {"segments": [seg(0, 2, "One.")]}, ("openai", 1): {"segments": [seg(0, 2, "Two.")]},
               ("groq", 0): {"segments": [seg(0, 2, "Won.")]}, ("groq", 1): transcribe.Failed("whisper-large-v3-turbo did not answer")}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 840, 8000 * 2], answers)
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a))
    assert r["ok"] and r["about"].endswith(words.PART_CHECKED.format(chunks="2", n=1, of=2)) and len(rows) == 3
    answers[("openai", 1)] = transcribe.Failed("whisper-1 did not answer: HTTP 500")
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a))
    assert r == {"ok": False, "error": "chunk 2: whisper-1 did not answer: HTTP 500"}


def test_a_key_is_its_files_first_line_and_never_in_an_error(tmp_path, monkeypatch):
    (tmp_path / "k").write_text("  sk-abc-123456789  \nnotes\n")
    assert transcribe.key(tmp_path / "k") == "sk-abc-123456789" and transcribe.key(tmp_path / "gone") == ""
    chunk = tmp_path / "c000.mp3"
    chunk.write_bytes(b"\0" * 10)

    def refuse(req, timeout):
        assert req.get_header("Authorization") == "Bearer sk-abc-123456789" and b'name="model"\r\n\r\nwhisper-1' in req.data
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error": "bad key sk-abc-123456789"}'))
    monkeypatch.setattr(transcribe.urllib.request, "urlopen", refuse)
    with pytest.raises(transcribe.Failed) as e:
        transcribe.post("openai", "sk-abc-123456789", chunk)
    assert "HTTP 401" in str(e.value) and "<key>" in str(e.value) and "sk-abc" not in str(e.value)


def test_a_voice_note_arrives_with_the_line_that_it_is_heard_only_through_the_tool():
    files = [{"name": "voice-message.ogg", "type": "audio/ogg", "size": 1514226, "path": "/s/inbox/9/voice-message.ogg", "ok": True},
             {"name": "photo.png", "type": "image/png", "size": 10, "path": "/s/inbox/9/photo.png", "ok": True},
             {"name": "memo.m4a", "type": "audio/mp4", "size": 10, "ok": False, "error": "not staged"}]
    text = message_text(NS(clean_content="?", content="?"), files).splitlines()
    assert text == ["?", "[attachment] voice-message.ogg, audio/ogg, 1,514,226 bytes, saved at /s/inbox/9/voice-message.ogg",
                    words.VOICE_NOTE.format(name="voice-message.ogg"), "[attachment] photo.png, image/png, 10 bytes, saved at /s/inbox/9/photo.png",
                    "[attachment] memo.m4a: could not be fetched (not staged)"]
    assert "never listening" in text[2] and "transcribe" in text[2]
    for said in (text[2], words.TRANSCRIBED_NEXT, words.OVER_GATE):    # in the prompting guide's shape (shape.py's counts)
        assert shape.offenses(said) == []


def test_a_voice_note_never_transcribed_bounces_once_at_stop():
    room = Room.__new__(Room)
    room.lock, room.rp, room.bounced = threading.Lock(), NS(read_paths=set()), False
    room.to_open = {"/in/9/voice-message.ogg": "voice-message.ogg"}
    reason = room.stop_check(active=False)
    assert reason == ("You were sent voice-message.ogg, a recording, and have not transcribed it. Call transcribe on "
                      "/in/9/voice-message.ogg, then open your answer with what it says.")
    room.bounced = False
    room.rp.read_paths.add("/in/9/voice-message.ogg")                 # the engine adds a transcribe call's path
    assert room.stop_check(active=False) is None


def test_only_a_transcribe_call_counts_as_hearing_a_recording(monkeypatch):
    rp = RoomProcess.__new__(RoomProcess)
    calls = [("Read", {"file_path": "/in/9/voice-message.ogg"}), ("Read", {"file_path": "/in/9/photo.png"}),
             ("mcp__yerk__transcribe", {"path": "/in/9/memo.m4a"})]
    ev = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": n, "input": i} for n, i in calls]}}
    rp.proc, rp.read_paths, rp.evidence, rp.events = NS(stdout=[json.dumps(ev).encode()]), set(), None, queue.Queue()
    monkeypatch.setattr(rp, "_note", lambda obj: None)
    rp._read()
    assert rp.read_paths == {"/in/9/photo.png", "/in/9/memo.m4a"}     # a Read of a recording opens nothing, so it still bounces


def test_the_tool_reads_only_files_in_the_calling_rooms_own_folders(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    inbox = tmp_path / "rooms" / "example-room" / "work" / "inbox" / "5"
    inbox.mkdir(parents=True)
    (inbox / "voice-message.ogg").write_bytes(b"OggS")
    ask = lambda p: asyncio.run(y.tool_transcribe("example-room", str(p)))   # noqa: E731
    assert "outside this room's folders" in ask(tmp_path / "rooms" / "other" / "a.ogg")["error"]
    assert "there is no file gone.ogg" in ask(inbox / "gone.ogg")["error"]
    assert ask(inbox / "voice-message.ogg") == {"ok": False, "error": words.NO_SPEECH_KEY}     # the example layer has no key


def test_the_spend_rows_reach_the_morning_line(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    led.spend(room="r", tool="transcribe", engine="openai/whisper-1", usd=0.039889, note="chunk 1 of 1, 399 s")
    led.spend(room="r", tool="transcribe", engine="groq/whisper-large-v3-turbo", usd=0.004432, note="chunk 1 of 1, 399 s")
    spent = led.spent(time.time() - 60, time.time() + 1)
    assert words.SPENT.format(day=words.YESTERDAY, **spent) == "spent yesterday: US$0.04 on 2 calls"
    assert led.spent(0, time.time() - 60) == {"n": 0, "usd": 0}


@pytest.mark.skipif(os.environ.get("YERK_TRANSCRIBE_LIVE") != "1" or not os.environ.get("YERK_PRIVATE"),
                    reason="a real call: YERK_TRANSCRIBE_LIVE=1 and a key in $YERK_PRIVATE/secrets/openai.key")
@FFMPEG
def test_live_a_three_second_fixture_is_transcribed(tmp_path):
    private = Path(os.environ["YERK_PRIVATE"]).expanduser()
    if not transcribe.key(private / "secrets" / "openai.key"):
        pytest.skip("no OpenAI key in the private folder")
    note = tmp_path / "hello.wav"                                     # synthetic speech, made here: nothing personal on disk
    made = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "flite=text='Hello from Yerk. This is a test.'",
                           "-t", "3", str(note)], capture_output=True)
    if made.returncode:
        pytest.skip("this ffmpeg has no flite voice")
    rows: list[tuple[str, float, str]] = []
    r = transcribe.run(note, private / "secrets" / "openai.key", private / transcribe.GROQ_KEY, lambda *a: rows.append(a))
    assert r["ok"] and "hello" in r["text"].lower() and rows and Path(r["file"]).is_file()
