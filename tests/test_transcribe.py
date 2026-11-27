"""Checks the transcribe tool (transcribe.py), which turns a recording into text in two passes that check each other, with who spoke
when. Offline: the chunking, the near-silence drop rule, the cross-check's marks, the speakers (Muse's windows, its turns, its
letters reconciled across windows, a letter per segment), a whole call against fake providers (its spend rows, the gate, the keys, a
failed second pass, a failed or missing Muse), the arrival line, the bounce of a note never transcribed and the morning's spend
line. One test transcribes a real 3-second fixture, behind YERK_TRANSCRIBE_LIVE=1 with a key in $YERK_PRIVATE/secrets/openai.key."""
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
import wave
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from yerk_ai import shape, transcribe, words
from yerk_ai.channels.discord import message_text
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
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


def setup(tmp_path, monkeypatch, sizes, answers, groq: str | None = "gsk_test_groq_key_123456", meta: str | None = None):
    """A private folder's keys, an audio file in an inbox, fake chunks of `sizes` bytes, fake Muse windows and fake
    provider `answers` ({(engine, chunk or window): data or an exception}); returns (audio, keys, spend rows, the calls
    made). With `meta`, secrets/meta.key holds it; a call passes `meta_key=META(tmp_path)` for the speakers."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "openai.key").write_text("sk-test-openai-key-123456\nsecond line ignored\n")
    if groq:
        (tmp_path / "secrets" / "groq.key").write_text(groq + "\n")
    if meta:
        META(tmp_path).write_text(meta + "\n")
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
    def muse(secret, wav, timeout=600):
        calls.append(("meta", secret, wav.name))
        got = answers[("meta", int(wav.stem[1:]))]
        if isinstance(got, Exception):
            raise got
        return got

    def cut(audio, into, at):
        out = [into / f"w{i:03d}.wav" for i in range(len(at))]
        for w in out:
            w.write_bytes(b"RIFF")
        return out
    monkeypatch.setattr(transcribe, "chunks", fake_chunks(sizes))
    monkeypatch.setattr(transcribe, "post", post)
    monkeypatch.setattr(transcribe, "muse", muse)
    monkeypatch.setattr(transcribe, "windows", cut)
    return audio, (tmp_path / "secrets" / "openai.key", tmp_path / "secrets" / "groq.key"), rows, calls


def META(private: Path) -> Path:
    return private / transcribe.META_KEY


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
        "speakers: none (Muse: no Meta key at secrets/meta.key)",            # no key: the missing letters are said
        "[00:00:00 -> 00:00:04] Call Nora about the rollout.",
        "[00:14:00 -> 00:14:03] (uncertain) Then email the vendor | second pass: Ten email a vendor",
        "[00:14:03 -> 00:14:05] by Friday."]
    assert r["text"] == "\n".join(saved.splitlines()[2:]) and "sk-test" not in str(r) and "gsk_" not in str(r)
    assert r["speakers"] == {} and r["speakers_how"] == saved.splitlines()[1]
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


def test_a_chunk_pass_1_wrote_as_music_comes_back_with_pass_2s_lines_marked_and_counted(tmp_path, monkeypatch):
    music = {"segments": [seg(0, 30, "♪"), seg(30, 60, "♪♪"), seg(60, 90, "♪")]}       # whisper-1 over speech it did not hear
    speech = {"segments": [seg(2, 9, "Thanks for calling, how can I help?"), seg(31, 40, "I would like to ask about a charge."),
                           seg(64, 70, "Sure, one moment please.")]}
    answers = {("openai", 0): music, ("groq", 0): speech}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 90], answers)
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a))
    assert Path(r["file"]).read_text().splitlines() == [
        "voice-message.ogg, 1.5 min, checked by a second pass: 0 of 0 segments uncertain, and 3 more heard by the second pass only, "
        "where the first heard no words",
        "speakers: none (Muse: no Meta key at secrets/meta.key)",
        "[00:00:02 -> 00:00:09] (second pass only) Thanks for calling, how can I help?",
        "[00:00:31 -> 00:00:40] (second pass only) I would like to ask about a charge.",
        "[00:01:04 -> 00:01:10] (second pass only) Sure, one moment please."]
    assert "♪" not in r["text"] and r["segments"] == 3 and r["uncertain"] == 0
    answers[("groq", 0)] = {"segments": [seg(0, 90, "♪♪")]}                # neither pass heard a word: the file as before
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a))
    assert Path(r["file"]).read_text().splitlines() == [
        "voice-message.ogg, 1.5 min, checked by a second pass: 0 of 3 segments uncertain", "speakers: none (Muse: no Meta key at secrets/meta.key)",
        "[00:00:00 -> 00:00:30] ♪", "[00:00:30 -> 00:01:00] ♪♪", "[00:01:00 -> 00:01:30] ♪"]
    answers[("groq", 0)] = speech
    keys[1].unlink()                                                         # no Groq key: one pass, as before
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a))
    assert r["about"] == f"voice-message.ogg, 1.5 min, {words.UNCHECKED}" and r["text"].splitlines() == [
        "[00:00:00 -> 00:00:30] ♪", "[00:00:30 -> 00:01:00] ♪♪", "[00:01:00 -> 00:01:30] ♪"]


def test_a_chunk_pass_1_heard_no_word_in_is_pass_2s_whether_it_wrote_a_mark_or_no_segment_at_all(tmp_path, monkeypatch):
    speech = {"segments": [seg(2, 9, "Thanks for calling, how can I help?"), seg(31, 40, "I would like to ask about a charge.")]}
    answers = {("openai", 0): {"segments": []}, ("groq", 0): speech}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 90], answers)
    for first in ({"segments": []}, {"segments": [seg(0, 5, "♪")]}):          # nothing at all; one mark, before most of the speech
        answers[("openai", 0)] = first
        r = transcribe.run(audio, *keys, lambda *a: rows.append(a))
        assert r["text"].splitlines() == ["[00:00:02 -> 00:00:09] (second pass only) Thanks for calling, how can I help?",
                                          "[00:00:31 -> 00:00:40] (second pass only) I would like to ask about a charge."], first
        assert r["about"].endswith("0 of 0 segments uncertain, and 2 more heard by the second pass only, where the first heard no words")


def test_a_run_of_music_marks_inside_speech_takes_pass_2s_lines_for_that_stretch_only(tmp_path, monkeypatch):
    one = {"segments": [seg(0, 5, "Hello, this is Alex."), seg(5, 10, "I have a question."), seg(10, 40, "♪"), seg(40, 70, "♪♪"),
                        seg(70, 75, "Thanks for holding."), seg(75, 80, "How can I help?")]}
    two = {"segments": [seg(0, 5, "Hello, this is Alex."), seg(5, 10, "I have a question."), seg(12, 20, "Please stay on the line."),
                        seg(45, 60, "Your call is important to us."), seg(70, 75, "Thanks for holding."), seg(75, 80, "How can I help?")]}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 80], {("openai", 0): one, ("groq", 0): two})
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a))
    assert r["about"].endswith("checked by a second pass: 0 of 4 segments uncertain, and 2 more heard by the second pass only, "
                               "where the first heard no words")
    assert r["text"].splitlines() == [                                       # pass 1's agreeing lines unmarked, nothing twice
        "[00:00:00 -> 00:00:05] Hello, this is Alex.",
        "[00:00:05 -> 00:00:10] I have a question.",
        "[00:00:12 -> 00:00:20] (second pass only) Please stay on the line.",
        "[00:00:45 -> 00:01:00] (second pass only) Your call is important to us.",
        "[00:01:10 -> 00:01:15] Thanks for holding.",
        "[00:01:15 -> 00:01:20] How can I help?"]


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


def turn(start: float, end: float, who: str) -> dict[str, object]:
    """One turn as transcribe.turns gives it: seconds from the recording's start and a letter."""
    return {"start": start, "end": end, "speaker": who}


def answer(*turns: tuple[float, float, str]) -> dict[str, object]:
    """A Muse answer in the live shape (probe-raw-response.json): milliseconds within its window, a placeholder session."""
    return {"sessionId": "session-0001", "transcript": "words", "audioDurationMs": 540000,
            "turns": [{"turnId": n, "startMs": int(a * 1000), "endMs": int(b * 1000), "transcript": "words", "speaker": who}
                      for n, (a, b, who) in enumerate(turns)]}


def test_muse_windows_are_120_s_every_60_s_each_sharing_a_minute_with_the_next():
    assert transcribe.starts(100) == [0] and transcribe.starts(120) == [0] and transcribe.starts(121) == [0, 60]
    for seconds, n, last in ((1692, 28, 72), (3350, 55, 110)):          # a 28-minute call and a 56-minute one
        at = transcribe.starts(seconds)
        assert at[0] == 0 and [min(transcribe.WINDOW, seconds - a) for a in at] == [120] * (n - 1) + [last]   # 2 min at most, to the end
        assert [a + transcribe.WINDOW - b for a, b in zip(at, at[1:])] == [60] * (n - 1)                       # a minute shared with the next


@pytest.fixture
def long_windows(monkeypatch):
    """Muse windows of 540 s every 450 s, the shape the letter and label tests below were written in: reconcile and the
    labels read WINDOW and STEP, never a length of their own."""
    monkeypatch.setattr(transcribe, "WINDOW", 540)
    monkeypatch.setattr(transcribe, "STEP", 450)


@FFMPEG
def test_a_window_is_mono_16_khz_16_bit_wav_cut_at_its_start_for_its_length(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "WINDOW", 2)
    monkeypatch.setattr(transcribe, "STEP", 1.5)
    note = tone(tmp_path / "call.ogg", 5)
    wins = transcribe.windows(note, tmp_path, transcribe.starts(5))
    assert [w.name for w in wins] == ["w000.wav", "w001.wav", "w002.wav"]        # from 0, 1.5 and 3 s: each shares 0.5 s
    for w in wins:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels,sample_rate:format=duration",
                                "-of", "csv=p=0", str(w)], capture_output=True, text=True).stdout.split()
        assert probe[0] == "pcm_s16le,16000,1" and abs(float(probe[1]) - 2) < 0.05
    (tmp_path / "short").mkdir()
    one = transcribe.windows(tone(tmp_path / "short.ogg", 1), tmp_path / "short", transcribe.starts(1))
    assert len(one) == 1 and abs(one[0].stat().st_size / 32000 - 1) < 0.05           # 16,000 two-byte samples a second


def test_a_muse_answer_maps_to_turns_in_seconds_from_the_recordings_start():
    got = answer((0.5, 4.25, "A"), (5, 9, "B"))
    got["turns"] += [{"turnId": 9, "startMs": 9500, "endMs": 9600, "transcript": " ", "speaker": "A"},     # no words
                     {"turnId": 10, "startMs": 9700, "endMs": 9900, "transcript": "mm", "speaker": ""},     # no letter
                     {"turnId": 11, "startMs": 9950, "endMs": 9950, "transcript": "ok", "speaker": "B"}]    # no length, as Muse sends now and then
    assert transcribe.turns(got, 450) == [turn(450.5, 454.25, "A"), turn(455, 459, "B")]
    with pytest.raises(transcribe.Failed, match=r"did not read as turns \(KeyError 'turns'; its keys: audioDurationMs, sessionId"):
        transcribe.turns({"sessionId": "session-0001", "audioDurationMs": 10080}, 0)


@pytest.mark.usefixtures("long_windows")
def test_letters_are_reconciled_by_the_time_they_share_in_each_overlap():
    at = [0, 450]
    first = [turn(0, 300, "A"), turn(300, 470, "B"), turn(470, 540, "A")]
    swapped = [turn(450, 470, "A"), turn(470, 540, "B"), turn(540, 700, "A"), turn(700, 990, "B")]   # window 2 heard B first
    out, new, _ = transcribe.reconcile([first, swapped], at)
    assert new == 0 and out == first + [turn(540, 700, "B"), turn(700, 990, "A")]   # its A is the recording's B: comes out B
    same = [turn(450, 470, "B"), turn(470, 540, "A"), turn(540, 700, "B")]
    assert transcribe.reconcile([first, same], at)[:2] == (first + [turn(540, 700, "B")], 0)    # no swap: stays as it is


@pytest.mark.usefixtures("long_windows")
def test_a_letter_with_no_time_in_the_overlap_gets_the_next_letter_and_is_counted():
    first = [turn(0, 400, "A"), turn(400, 540, "B")]
    second = [turn(450, 540, "A"), turn(540, 600, "B"), turn(600, 990, "A")]   # its B never spoke in the overlap
    out, new, _ = transcribe.reconcile([first, second], [0, 450])
    assert new == 1 and out == first + [turn(540, 600, "C"), turn(600, 990, "B")]
    third = [turn(900, 1000, "A"), turn(1000, 1100, "B")]                    # window 3 from 900: its A is window 2's A, so B
    out, new, _ = transcribe.reconcile([first, second, third], [0, 450, 900])
    assert new == 2 and out[-2:] == [turn(990, 1000, "B"), turn(1000, 1100, "D")]   # A, B and C taken: D, the second counted


@pytest.mark.usefixtures("long_windows")
def test_a_turn_inside_an_overlap_is_kept_from_the_earlier_window_only():
    first = [turn(0, 500, "A"), turn(500, 540, "B")]
    second = [turn(450, 500, "B"), turn(500, 560, "A"), turn(560, 600, "B")]   # 500-540 heard twice, 540-560 once
    out, new, _ = transcribe.reconcile([first, second], [0, 450])
    assert new == 0 and out == [turn(0, 500, "A"), turn(500, 540, "B"), turn(540, 560, "B"), turn(560, 600, "A")]
    assert sum(t["end"] - t["start"] for t in out) == 600                  # every second once


def test_a_segment_takes_the_letter_covering_most_of_it_else_the_nearest_within_3_s():
    heard = [turn(8, 13, "A"), turn(13, 20, "B"), turn(33, 35, "A")]
    assert transcribe.speaker({"start": 10, "end": 20}, heard) == "B"        # A covers 3 s of it, B 7 s
    assert transcribe.speaker({"start": 30, "end": 32}, heard) == "A"        # no turn touches it; A's middle is 3 s off
    assert transcribe.speaker({"start": 40, "end": 41}, heard) is None and transcribe.speaker({"start": 0, "end": 1}, []) is None
    assert transcribe.line({"start": 10, "end": 20, "text": "Fine.", "other": "Find.", "speaker": "B"}) == (
        "[00:00:10 -> 00:00:20] B: (uncertain) Fine. | second pass: Find.")


def test_a_voices_clip_is_its_dense_speech_nearest_the_moment_about_10_s_in_at_most_8_pieces_never_a_stretch_twice():
    def cuts(said: list[dict[str, object]], near: float) -> list[tuple[float, float]]:
        return [(round(s, 2), round(e, 2)) for s, e in transcribe.clip(said, near)]
    said = [turn(0, 20, "A"), turn(30, 33, "A"), turn(40, 52, "A"), turn(40, 52, "A"), turn(60, 61, "A"), turn(80, 84, "A")]
    assert cuts(said, 50) == [(30, 33), (40, 48)]                  # turns of 2.5 to 15 s nearest 50 first, 8 s at most: 11 s
    assert cuts(said[:1] + said[4:5], 50) == [(0, 8), (59.85, 61.15)]     # then a long turn's first 8 s, then a short one padded
    assert cuts([turn(25, 26, "A"), turn(26.1, 27, "A"), turn(0, 20, "A")], 30) == [(0, 8), (25.95, 27.15)]   # 26.1 never twice
    shorts = cuts([turn(2 * k, 2 * k + 0.8, "A") for k in range(12)] + [turn(30, 30.5, "A")], 0)
    assert len(shorts) == 8 and shorts[0] == (0, 0.95) and shorts[-1] == (13.85, 14.95)    # 8 pieces at most; under 0.7 s never


def test_a_break_is_a_window_past_the_first_voices_with_a_letter_matched_by_none_and_its_voices_are_clipped_each_side():
    at, a = [0, 60, 120, 180, 240, 300, 360], [turn(s, s + 5, "A") for s in (60, 105, 130, 200, 250, 300)]
    each = [[], [a[0], turn(65, 100, "B"), turn(100, 105, "C"), a[1], a[2]], [a[2], a[3]], [a[3], a[4]], [a[4], a[5]],
            [a[5], turn(370, 375, "D"), turn(380, 383, "D")],                   # D is new where B, silent since 100 s, may be back
            [turn(370, 375, "D"), turn(380, 383, "D"), turn(470, 471, "E")]]    # E's one second, padded, is under 1.5 s
    heard = [turn(65, 100, "B"), turn(100, 105, "C")]                           # B spoke 35 s, C 5 s
    assert transcribe.breaks(heard, each, at) == [                              # window 2's first voices are no break
        ([("A", [(250, 255), (300, 305)]), ("B", [(65, 73)])], [("D", [(370, 375), (380, 383)])])]   # A nearest 300, D nearest 360
    first = [turn(130, 140, "A"), turn(140, 150, "B")]
    assert transcribe.reconcile([[], first], [0, 60]) == (first, 0, [[], first])   # a first window that heard no one: none counted


def pcm(path: Path, seconds: float, value: int) -> Path:
    """A WAV as the windows are, every sample `value`: made up, so no test of a splice cuts real audio."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(value.to_bytes(2, "little") * round(seconds * 16000))
    return path


def test_a_splice_is_one_wav_of_each_voices_pieces_with_silence_after_each_and_its_answer_gives_each_voice_a_letter(tmp_path):
    wavs = [pcm(tmp_path / "w000.wav", 10, 1), pcm(tmp_path / "w001.wav", 5, 2)]          # windows from 0 s and from 5 s
    spans, seconds = transcribe.splice([("X", [(1, 2), (6, 6.5)]), ("Y", [(9.5, 10.5)])], wavs, [0, 5], tmp_path / "b000.wav")
    assert [(who, round(a, 2), round(b, 2)) for who, a, b in spans] == [("X", 0, 1.9), ("Y", 2.4, 3.6)] and round(seconds, 2) == 4.1
    with wave.open(str(tmp_path / "b000.wav")) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        got = w.readframes(w.getnframes())
    one, two, quiet = b"\1\0", b"\2\0", b"\0\0"
    assert got == (one * 16000 + quiet * 3200 + two * 8000 + quiet * 3200 + quiet * 8000       # X: 1 s of window 1, 0.5 s of window 2
                   + two * 8000 + quiet * 8000 + quiet * 3200 + quiet * 8000)                 # Y: past window 2's end, silence
    heard = answer((0, 1.5, "A"), (1.5, 1.9, "B"), (2.4, 3.0, "A"), (4.5, 5, "C"))
    assert transcribe.letters(spans + [("Z", 4.1, 4.4)], heard) == {"X": "A", "Y": "A", "Z": None}   # C is near Z but not on it


def test_a_minted_letter_joins_the_one_group_two_used_splices_put_it_with_and_a_join_counts_at_a_later_break():
    p, apart = [(0.0, 5.0)], [{"A", "B"}, {"C", "M"}]                         # windows heard A and B, and C and M, as two voices
    found = [([("A", p), ("B", p)], [("C", p)]), ([("A", p), ("B", p), ("C", p)], [("D", p)]), ([("A", p), ("B", p)], [("E", p)]),
             ([("A", p), ("B", p)], [("F", p)]), ([("A", p)], [("G", p)]), ([("A", p)], [("H", p)]),
             ([("A", p), ("K", p)], [("L", p)]), ([("A", p), ("B", p)], [("M", p)]), ([("A", p)], [("N", p)]),
             ([("A", p), ("B", p)], [("P", p)]), ([("A", p), ("B", p)], [("Q", p)])]
    tries = [[{"A": "s1", "B": "s2", "C": "s2"}, {"C": "s1", "A": "s2", "B": "s1"}],                  # C with B both times: joined
             [{"A": "s1", "B": "s2", "C": "s3", "D": "s3"}, {"D": "s1", "A": "s2", "B": "s1", "C": "s3"}],   # with C, then B: one group
             [{"A": "s1", "B": "s2", "E": "s1"}, {"E": "s1", "A": "s2", "B": "s1"}],                  # with A, then with B: no
             [{"A": "s1", "B": "s1", "F": "s2"}, {"F": "s1", "A": "s1", "B": "s2"}],                  # A and B one letter: not used
             [None, {"G": "s1", "A": "s1"}], [{"A": "s1", "H": None}, {"H": "s1", "A": "s1"}],          # failed; H not heard once
             [{"A": "s1", "K": "s1", "L": "s1"}, {"L": "s1", "A": "s1", "K": "s1"}],                  # with A and K, two groups: no
             [{"A": "s1", "B": "s2", "M": "s2"}, {"M": "s1", "A": "s2", "B": "s1"}],                  # with B, whose C a window heard apart from M: no
             [{"A": "s1", "N": "s2"}, {"N": "s1", "A": "s2"}],                                        # heard twice, with no one: a new voice
             [{"A": None, "B": "s1", "P": None}, {"P": "s1", "A": "s1", "B": "s2"}],                  # P and A unheard once: that splice is no vote
             [{"A": "s1", "B": "s2", "Q": "s2"}, {"Q": "s1", "A": "s1", "B": "s2"}, {"B": "s1", "A": "s2", "Q": "s1"}]]   # with B in two of three
    assert transcribe.join(found, tries, apart) == ({"C": "B", "D": "B", "Q": "B"}, ["E", "F", "G", "H", "L", "M", "P"])   # N is in neither


def bridged(tmp_path, monkeypatch, answers: dict[str, object]) -> list[str]:
    """Three made-up windows (A and B heard apart in the first, C new in the third) and a stub Muse answering each splice
    by its file's name from `answers`; returns the names it was sent. Each voice is one 5 s piece: in a splice its span is
    0 to 5.2 s, 5.7 to 10.9 s, then 11.4 to 16.6 s."""
    sent: list[str] = []

    def muse(secret, wav, timeout=600):
        sent.append(wav.name)
        if isinstance(got := answers[wav.stem], Exception):
            raise got
        return got
    monkeypatch.setattr(transcribe, "muse", muse)
    return sent


def test_a_splice_that_gives_two_voices_a_window_heard_apart_one_letter_is_not_used_and_a_third_order_decides(tmp_path, monkeypatch):
    each, at = [[turn(10, 15, "A"), turn(20, 25, "B")], [], [turn(190, 195, "C")]], [0, 60, 120]
    wavs = [pcm(tmp_path / f"w{i:03d}.wav", 1, 0) for i in range(3)]
    sent = bridged(tmp_path, monkeypatch, {
        "b000": answer((0, 10.9, "s1"), (11.4, 16.6, "s2")),                       # A, B, C: A and B one letter, not used
        "b001": answer((0, 5.2, "s1"), (5.7, 10.9, "s1"), (11.4, 16.6, "s2")),      # C, A, B: C with A
        "b002": answer((0, 5.2, "s1"), (5.7, 10.9, "s2"), (11.4, 16.6, "s2"))})     # the third, B, A, C: C with A again
    assert transcribe.bridge([], each, at, wavs, tmp_path, "meta-test-key-123456", lambda *r: None) == ({"C": "A"}, []) and sent[2:] == ["b002.wav"]


def test_a_second_letter_a_window_gave_a_voice_for_two_seconds_is_no_second_voice_and_five_seconds_is(tmp_path, monkeypatch):
    each, at = [[turn(10, 15, "A"), turn(20, 25, "B")], [turn(70, 75, "B")], [turn(130, 135, "B"), turn(140, 142, "C")]], [0, 60, 120]
    wavs = [pcm(tmp_path / f"w{i:03d}.wav", 1, 0) for i in range(3)]
    bridged(tmp_path, monkeypatch, {"b000": answer((0, 5.2, "s1"), (5.7, 19.1, "s2")),                       # A, B, C: C with B
                                    "b001": answer((0, 2.5, "s1"), (3.0, 8.2, "s2"), (8.7, 19.1, "s1"))})   # C, A, B: C with B
    assert transcribe.bridge([], each, at, wavs, tmp_path, "meta-test-key-123456", lambda *r: None) == ({"C": "B"}, [])   # C spoke 2 s beside B
    each[2][1] = turn(140, 145, "C")                                         # 5 s beside B: the window heard two voices
    sent = bridged(tmp_path, monkeypatch, {"b000": answer((0, 5.2, "s1"), (5.7, 21.8, "s2")),
                                           "b001": answer((0, 5.2, "s1"), (5.7, 10.9, "s2"), (11.4, 21.8, "s1"))})
    assert transcribe.bridge([], each, at, wavs, tmp_path, "meta-test-key-123456", lambda *r: None) == ({}, ["C"]) and len(sent) == 2   # neither used, no third


def test_each_bridge_request_answered_is_one_spend_row_and_a_bridge_that_fails_joins_nothing(tmp_path, monkeypatch):
    each, at = [[turn(10, 15, "A"), turn(20, 25, "B")], [], [turn(190, 195, "C")]], [0, 60, 120]
    wavs, rows = [pcm(tmp_path / f"w{i:03d}.wav", 1, 0) for i in range(3)], []
    late = transcribe.Failed("muse-voice-transcribe-1.0 did not answer: TimeoutError after 100 s")
    with_a = answer((0, 5.2, "s1"), (5.7, 10.9, "s2"), (11.4, 16.6, "s1"))     # A, B, C, then the third C, B, A: C with A both times
    bridged(tmp_path, monkeypatch, {"b000": with_a, "b001": late, "b002": with_a})
    assert transcribe.bridge([], each, at, wavs, tmp_path, "meta-test-key-123456", lambda *r: rows.append(r)) == ({"C": "A"}, [])
    assert [(e, round(u, 6), n) for e, u, n in rows] == [("meta/muse-voice-transcribe-1.0", 0.000855, "bridge 1 of 2, 17 s"),
                                                          ("meta/muse-voice-transcribe-1.0", 0.000855, "bridge 3 of 3, 17 s")]   # 17.1 s each
    rows.clear()
    sent = bridged(tmp_path, monkeypatch, {"b000": late, "b001": late})
    assert transcribe.bridge([], each, at, wavs, tmp_path, "meta-test-key-123456", lambda *r: rows.append(r)) == ({}, ["C"]) and rows == []
    (tmp_path / "w000.wav").write_bytes(b"RIFF")                                 # a window that cannot be read: nothing is sent
    sent.clear()
    assert transcribe.bridge([], each, at, wavs, tmp_path, "meta-test-key-123456", lambda *r: rows.append(r)) == ({}, ["C"]) and sent == []


def silent_minute(tmp_path, monkeypatch, bridges: dict[str, object], third: bool = False) -> tuple[dict[str, object], list[tuple[str, float, str]]]:
    """A made-up 5-minute call in four windows: A talks throughout, B from 25 to 50 s, then says nothing through window
    2 and is back at 200 s, where window 3 gives it a new letter, C; with `third`, a voice new at 190 s too, D. Muse
    answers the splices by file name from `bridges`. Returns the result and the spend rows."""
    z = [(70, 79, "C")] if third else []
    answers = {("openai", 0): {"segments": [seg(5, 20, "Hello there."), seg(26, 48, "Hi, good to hear you.")]
                                            + ([seg(190, 199, "Press one.")] if third else []) + [seg(200, 228, "I am back now.")]},
               ("meta", 0): answer((5, 25, "A"), (25, 50, "B"), (60, 110, "A")), ("meta", 1): answer((0, 50, "A"), (70, 110, "A")),
               ("meta", 2): answer((10, 50, "B"), *z, (80, 110, "A"), (115, 120, "B")),           # its A is B back, from 200 s
               ("meta", 3): answer(*[(a - 60, b - 60, w) for a, b, w in z], (20, 50, "B"), (55, 60, "A"), (70, 110, "A"))}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 300], answers, groq=None, meta="meta-test-key-123456")

    def muse(secret, wav, timeout=600):
        got = answers[("meta", int(wav.stem[1:]))] if wav.stem[0] == "w" else bridges[wav.stem]
        if isinstance(got, Exception):
            raise got
        return got
    monkeypatch.setattr(transcribe, "muse", muse)
    monkeypatch.setattr(transcribe, "windows", lambda audio, into, at: [pcm(into / f"w{i:03d}.wav", 0, 0) for i in range(len(at))])
    r = transcribe.run(audio, *keys, lambda e, usd, note: rows.append((e, usd, note)), meta_key=META(tmp_path))
    return r, rows


AGREE = {"b000": answer((0, 16.4, "s1"), (16.9, 33.8, "s2")),                       # A, B, C: C with B
         "b001": answer((0, 8.2, "s2"), (8.7, 25.1, "s1"), (25.6, 33.8, "s2"))}     # C, A, B: C with B
APART = ["[00:00:05 -> 00:00:20] A: Hello there.", "[00:00:26 -> 00:00:48] B: Hi, good to hear you.", "[00:03:20 -> 00:03:48] C: I am back now."]


def test_a_voice_silent_through_a_shared_minute_keeps_its_letter_and_each_bridge_request_is_a_spend_row(tmp_path, monkeypatch):
    r, rows = silent_minute(tmp_path, monkeypatch, AGREE)
    assert Path(str(r["file"])).read_text().splitlines()[1:] == [
        "speakers: A 2:35, B 0:55 (letters, not names: say who is who as a guess from what is said, unless the owner tells you)",
        "[00:00:05 -> 00:00:20] A: Hello there.", "[00:00:26 -> 00:00:48] B: Hi, good to hear you.", "[00:03:20 -> 00:03:48] B: I am back now."]
    assert r["speakers"] == {"A": 155, "B": 55} and r["speakers_joined"] == 1
    assert sorted((round(u, 6), n) for e, u, n in rows if e.startswith("meta/")) == [                  # 34.3 s each at Muse's price
        (0.001715, "bridge 1 of 2, 34 s"), (0.001715, "bridge 2 of 2, 34 s")] + [(0.006, f"window {i} of 4, 120 s") for i in (1, 2, 3, 4)]


def test_a_voice_both_splices_hear_with_no_earlier_one_is_a_new_voice_joined_to_nothing_and_not_counted(tmp_path, monkeypatch):
    r, _ = silent_minute(tmp_path, monkeypatch, {"b000": answer((0, 16.4, "s1"), (16.9, 33.8, "s2"), (34.3, 42.5, "s3")),   # A, B, C, D
                                                 "b001": answer((0, 8.2, "s2"), (8.7, 16.9, "s3"), (17.4, 33.8, "s1"), (34.3, 42.5, "s2"))},
                         third=True)                                                   # C, D, A, B: C with B, D with no one, twice
    assert r["speakers_how"] == ("speakers: A 2:35, B 0:55, D 0:09 (letters, not names: say who is who as a guess from what is said, unless the "
                                 "owner tells you)")
    assert r["speakers_joined"] == 1


def test_two_orders_that_disagree_get_a_third_and_three_that_disagree_join_nothing_and_the_letter_is_counted(tmp_path, monkeypatch):
    alone = answer((0, 8.2, "s3"), (8.7, 25.1, "s1"), (25.6, 33.8, "s2"))               # C, A, B: C with no one
    r, rows = silent_minute(tmp_path, monkeypatch, AGREE | {"b001": alone, "b002": answer((0, 16.9, "s1"), (17.4, 33.8, "s2"))})
    assert r["speakers"] == {"A": 155, "B": 55} and r["speakers_joined"] == 1           # the third, C, B, A: C with B again, two of three
    assert sorted(n for e, u, n in rows if n.startswith("bridge")) == ["bridge 1 of 2, 34 s", "bridge 2 of 2, 34 s", "bridge 3 of 3, 34 s"]
    (tmp_path / "secrets").rename(tmp_path / "secrets-1")                               # the same call again, on a fresh folder
    (tmp_path / "inbox").rename(tmp_path / "inbox-1")
    r, _ = silent_minute(tmp_path, monkeypatch, AGREE | {"b001": alone, "b002": answer((0, 8.2, "s1"), (8.7, 16.9, "s2"), (17.4, 33.8, "s1"))})   # C with A, heard apart from it: not used
    assert r["speakers"] == {"A": 155, "B": 25, "C": 30} and r["speakers_joined"] == 0 and r["speakers_how"].endswith(
        ", 1 letter could not be matched across windows, so one voice may have two letters")


def test_a_failed_bridge_leaves_the_words_the_windows_letters_and_the_count_as_before_it(tmp_path, monkeypatch):
    late = transcribe.Failed("muse-voice-transcribe-1.0 did not answer: TimeoutError after 100 s")
    r, rows = silent_minute(tmp_path, monkeypatch, {"b000": late, "b001": late})
    assert r["ok"] and r["text"].splitlines() == APART and r["speakers"] == {"A": 155, "B": 25, "C": 30} and r["speakers_joined"] == 0
    assert r["speakers_how"].endswith(", 1 letter could not be matched across windows, so one voice may have two letters")
    assert not [n for e, u, n in rows if n.startswith("bridge")]                        # a failed request is not billed


def two_chunks() -> dict[tuple[str, int], object]:
    one = {"segments": [seg(0, 4, "Let us go over the plan."), seg(4, 6, "Thanks for watching!", 0.97, -1.2)]}
    two = {"segments": [seg(0, 3, "Then email the vendor"), seg(3, 5, "by Friday.")]}
    return {("openai", 0): one, ("openai", 1): two, ("groq", 0): {"segments": [seg(0, 4, "Let us go over the plan.")]},
            ("groq", 1): {"segments": [seg(0, 5, "Ten email a vendor by Friday.")]}}


@pytest.mark.usefixtures("long_windows")
def test_two_voices_label_each_line_with_letters_matched_across_windows_and_a_spend_row_per_window(tmp_path, monkeypatch):
    answers = two_chunks() | {("meta", 0): answer((0, 4, "A"), (500, 530, "B"), (531, 539, "A")),        # 0 to 540 s
                              ("meta", 1): answer((50, 80, "A"), (81, 89, "B"), (390, 393, "A"), (393, 395, "B"))}   # from 450 s
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 840, 8000 * 5], answers, meta="meta-test-key-123456")
    r = transcribe.run(audio, *keys, lambda e, usd, note: rows.append((e, usd, note)), meta_key=META(tmp_path))
    saved = Path(r["file"]).read_text().splitlines()
    assert saved == [
        "voice-message.ogg, 14.1 min, checked by a second pass: 1 of 3 segments uncertain",
        "speakers: A 0:14, B 0:33 (letters, not names: say who is who as a guess from what is said, unless the owner tells you)",
        "[00:00:00 -> 00:00:04] A: Let us go over the plan.",
        "[00:14:00 -> 00:14:03] B: (uncertain) Then email the vendor | second pass: Ten email a vendor",   # window 2's A
        "[00:14:03 -> 00:14:05] A: by Friday."]
    assert r["speakers"] == {"A": 14, "B": 33} and r["speakers_how"] == saved[1] and r["text"] == "\n".join(saved[2:])
    assert sorted(c for c in calls if c[0] == "meta") == [("meta", "meta-test-key-123456", "w000.wav"), ("meta", "meta-test-key-123456", "w001.wav")]
    assert sorted((e, round(u, 6), n) for e, u, n in rows if e.startswith("meta/")) == [
        ("meta/muse-voice-transcribe-1.0", 0.01975, "window 2 of 2, 395 s"), ("meta/muse-voice-transcribe-1.0", 0.027, "window 1 of 2, 540 s")]
    assert len(rows) == 6 and "meta-test" not in str(r) and "meta-test" not in "\n".join(saved)


@pytest.mark.usefixtures("long_windows")
def test_one_voice_writes_no_letters_and_the_file_is_as_it_was_before_speakers(tmp_path, monkeypatch):
    answers = two_chunks() | {("meta", 0): answer((0, 4, "A"), (500, 539, "A")), ("meta", 1): answer((50, 89, "A"), (390, 395, "A"))}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 840, 8000 * 5], answers, meta="meta-test-key-123456")
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a), meta_key=META(tmp_path))
    assert Path(r["file"]).read_bytes() == (                                # the bytes the file had before speakers
        b"voice-message.ogg, 14.1 min, checked by a second pass: 1 of 3 segments uncertain\n"
        b"[00:00:00 -> 00:00:04] Let us go over the plan.\n"
        b"[00:14:00 -> 00:14:03] (uncertain) Then email the vendor | second pass: Ten email a vendor\n"
        b"[00:14:03 -> 00:14:05] by Friday.\n")
    assert r["speakers_how"] == words.ONE_VOICE and r["speakers"] == {"A": 48} and len(rows) == 6    # window 2's 500-539 s once


@pytest.mark.usefixtures("long_windows")
def test_a_failed_muse_call_or_a_missing_key_still_returns_the_words_and_says_why_there_are_no_letters(tmp_path, monkeypatch):
    answers = two_chunks() | {("meta", 0): answer((0, 4, "A"), (500, 539, "B")),
                              ("meta", 1): transcribe.Failed("muse-voice-transcribe-1.0 did not answer: HTTP 402 billing_not_configured")}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 840, 8000 * 5], answers, meta="meta-test-key-123456")
    r = transcribe.run(audio, *keys, lambda e, usd, note: rows.append((e, usd, note)), meta_key=META(tmp_path))
    said = "speakers: none (Muse: window 2 of 2 failed: muse-voice-transcribe-1.0 did not answer: HTTP 402 billing_not_configured)"
    assert r["ok"] and r["segments"] == 3 and r["speakers_how"] == said and r["speakers"] == {}
    assert Path(r["file"]).read_text().splitlines()[1] == said and r["text"].splitlines()[0] == "[00:00:00 -> 00:00:04] Let us go over the plan."
    assert [n for e, u, n in rows if e.startswith("meta/")] == ["window 1 of 2, 540 s"]     # the failed window is not billed
    answers[("meta", 1)] = {"sessionId": "session-0002", "transcript": ""}                 # an answer in another shape
    assert transcribe.run(audio, *keys, lambda *a: None, meta_key=META(tmp_path))["speakers_how"].startswith(
        "speakers: none (Muse: its answer did not read as turns (KeyError 'turns'")
    META(tmp_path).unlink()
    r = transcribe.run(audio, *keys, lambda *a: None, meta_key=META(tmp_path))
    assert r["ok"] and r["speakers_how"] == "speakers: none (Muse: no Meta key at secrets/meta.key)"


def test_a_muse_window_that_times_out_is_sent_once_and_no_window_starts_after_one_failed(tmp_path, monkeypatch):
    muse = transcribe.muse
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 840, 8000 * 5], two_chunks(), meta="meta-test-key-123456")
    sent: list[str] = []

    def silent(req, timeout):                                              # Muse never answers: each request times out
        sent.append(req.data.split(b'filename="')[1][:8].decode())
        raise TimeoutError("timed out")
    monkeypatch.setattr(transcribe, "muse", muse)
    monkeypatch.setattr(transcribe, "MUSE_PARALLEL", 4)                    # fewer at once than the 14 windows, so some never start
    monkeypatch.setattr(transcribe, "time", NS(monotonic=time.monotonic, sleep=lambda s: None))
    monkeypatch.setattr(transcribe.urllib.request, "urlopen", silent)
    r = transcribe.run(audio, *keys, lambda e, usd, note: rows.append((e, usd, note)), meta_key=META(tmp_path))
    assert r["ok"] and r["segments"] == 3 and len(sent) == len(set(sent))                  # each window sent once at most
    assert sent and set(sent) <= {f"w{i:03d}.wav" for i in range(transcribe.MUSE_PARALLEL)}   # none started after one failed
    assert r["speakers_how"] == ("speakers: none (Muse: window " + ", ".join(str(int(w[1:4]) + 1) for w in sorted(sent)) + " of 14 failed: "
                                 f"muse-voice-transcribe-1.0 did not answer: TimeoutError after 0 s: timed out, so {14 - len(sent)} more were not sent)")
    assert len(rows) == 4                                                   # the passes' rows; no window was billed


def test_the_gate_counts_the_muse_share(tmp_path, monkeypatch):
    answers = {(e, i): {"segments": []} for e in ("openai", "groq") for i in range(10)}
    audio, keys, rows, calls = setup(tmp_path, monkeypatch, [8000 * 840] * 10, answers, meta="meta-test-key-123456")
    r = transcribe.run(audio, *keys, lambda *a: rows.append(a), meta_key=META(tmp_path))   # 140 min: 139 windows, 16,680 s to Muse
    assert r == {"ok": False, "error": words.OVER_GATE.format(minutes=140, usd=0.84 + 0.093333 + 0.834)} and not calls and not rows
    assert transcribe.run(audio, *keys, lambda *a: rows.append(a))["ok"] and len(rows) == 20        # without Muse, US$0.93


def test_a_muse_request_has_the_live_shape_and_its_key_is_never_in_an_error(tmp_path, monkeypatch):
    wav = tmp_path / "w000.wav"
    wav.write_bytes(b"RIFF....WAVE")

    def refuse(req, timeout):
        assert req.full_url == "https://api.meta.ai/v1/asr/transcribe" and req.get_header("Authorization") == "Bearer meta-abc-123456789"
        assert (b'name="request"\r\nContent-Type: application/json\r\n\r\n{"model": "muse-voice-transcribe-1.0", "mode": "DIARIZATION", '
                b'"audioEncoding": "WAV"}\r\n') in req.data and b'name="audio"; filename="w000.wav"\r\nContent-Type: audio/wav\r\n\r\nRIFF' in req.data
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error": "bad key meta-abc-123456789"}'))
    monkeypatch.setattr(transcribe.urllib.request, "urlopen", refuse)
    with pytest.raises(transcribe.Failed) as e:
        transcribe.muse("meta-abc-123456789", wav)
    assert "HTTP 401" in str(e.value) and "<key>" in str(e.value) and "meta-abc" not in str(e.value)


def test_a_request_that_ran_long_is_sent_once_a_quick_refusal_three_times_and_the_reason_has_no_html(tmp_path, monkeypatch):
    wav = tmp_path / "w000.wav"
    wav.write_bytes(b"RIFF....WAVE")
    clock, plan, sent, naps = [0.0], {"code": 504, "after": 121.0}, [], []

    def answer(req, timeout):
        sent.append(timeout)
        clock[0] += plan["after"]
        if not plan["code"]:
            raise TimeoutError("timed out")
        raise urllib.error.HTTPError(req.full_url, int(plan["code"]), "busy", {}, io.BytesIO(b"<!DOCTYPE html> <html> <head> <title>Error</title>"))
    monkeypatch.setattr(transcribe, "time", NS(monotonic=lambda: clock[0], sleep=naps.append))
    monkeypatch.setattr(transcribe.urllib.request, "urlopen", answer)
    with pytest.raises(transcribe.Failed) as e:                          # Meta's edge cut it at 121 s: sent once, no page
        transcribe.muse("meta-abc-123456789", wav)
    assert str(e.value) == "muse-voice-transcribe-1.0 did not answer: HTTP 504 after 121 s" and sent == [100] and not naps   # its own wait under the edge
    plan.update(code=0, after=600.0)
    with pytest.raises(transcribe.Failed) as e:                          # a timeout: sent once
        transcribe.post("openai", "sk-abc-123456789", wav)
    assert str(e.value) == "whisper-1 did not answer: TimeoutError after 600 s: timed out" and sent == [100, 600] and not naps
    plan.update(code=503, after=0.4)
    with pytest.raises(transcribe.Failed) as e:                          # a quick refusal: three tries, 5 s and 10 s apart
        transcribe.post("groq", "gsk-abc-123456789", wav)
    assert str(e.value) == "whisper-large-v3-turbo did not answer: HTTP 503 after 0 s" and sent == [100] + [600] * 4 and naps == [5, 10]


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
    rp.proc, rp.read_paths, rp.evidence, rp.events, rp.answers = NS(stdout=[json.dumps(ev).encode()]), set(), None, queue.Queue(), queue.Queue()
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
    led.spend(room="r", tool="transcribe", engine="meta/muse-voice-transcribe-1.0", usd=0.01995, note="window 1 of 1, 399 s")
    spent = led.spent(time.time() - 60, time.time() + 1)
    assert words.SPENT.format(day=words.YESTERDAY, **spent) == "spent yesterday: US$0.06 on 3 calls"
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
