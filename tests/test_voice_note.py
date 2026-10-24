"""The voice_note tool (voice_note.py), offline: the sections and the markup each voice gets, the probe's match, a whole note
against a fake MiniMax and a fake Fish on local ports (the fallback, all or nothing, the spend rows, the gate, the keys), the
loudness and opus step on synthetic tones, the probe failing a note, the Discord voice message's payload and its plain-file
fallback, `send` taking a note as voice, and the ability's entry. One test makes a real note, behind YERK_VOICE_LIVE=1 with
keys in $YERK_PRIVATE/secrets/."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Callable

import discord
import pytest

from test_discord import HOOK, OWNER, ROOM_CH, Client, Room
from yerk_ai import abilities, cli, context, memory, shape, transcribe, voice_note, words
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import budgets
from yerk_ai.daemon import YERK_TOOLS, Yerkd
from fake_channel import ConsoleChannel
from yerk_ai.ledger import Ledger

EXAMPLE_PRIVATE = Path(__file__).resolve().parents[1] / "examples" / "private"
FFMPEG = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
SCRIPT = ("[warm] Hello from the example room.\n\nThis is only a test, (break) nothing more.\n[[pause]]\n"
          "And here is a second section, long enough to stand on its own.")
HEARD = "Hello from the example room. This is only a test, nothing more. And here is a second section"


class Fake:
    """A provider on a local port: each POST is recorded (its headers and JSON body) and answered by `answer(n, body)`."""

    def __init__(self, answer: Callable[[int, dict[str, Any]], tuple[int, bytes]]) -> None:
        self.calls: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append({"auth": self.headers.get("Authorization"), "model": self.headers.get("model"), "body": body})
                code, data = answer(len(fake.calls), body)
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a: Any) -> None:
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1/tts"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(scope="module")
def tone(tmp_path_factory) -> bytes:
    """1.5 s of a 440 Hz tone as mp3, the audio both fake voices answer with."""
    out = tmp_path_factory.mktemp("tone") / "tone.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", "-ac", "1", "-ar", "32000",
                    "-b:a", "64k", str(out)], check=True)
    return out.read_bytes()


def spoke(tone: bytes) -> Callable[[int, dict[str, Any]], tuple[int, bytes]]:
    return lambda n, body: (200, json.dumps({"data": {"audio": tone.hex()}, "extra_info": {"usage_characters": len(body["text"])},
                                             "base_resp": {"status_code": 0, "status_msg": "success"}}).encode())


def broke(code: int = 2053, msg: str = "insufficient balance") -> tuple[int, bytes]:
    return 200, json.dumps({"base_resp": {"status_code": code, "status_msg": msg}}).encode()


@pytest.fixture
def note(tmp_path, monkeypatch, request):
    """A private folder with example keys and a pronunciation file, both fakes wired in, and the probe answering `heard`
    (set it on the returned namespace); returns NS(private, folder, rows, probes, mm, fish, make)."""
    private = tmp_path / "private"
    (private / "secrets").mkdir(parents=True)
    (private / "voice").mkdir()
    (private / "secrets" / "minimax.key").write_text("mm-test-key-123456\nnotes\n")
    (private / "secrets" / "fish.key").write_text("fish-test-key-654321\n")
    (private / "secrets" / "openai.key").write_text("sk-test-openai-key-777\n")
    (private / "voice" / "pronounce.txt").write_text("# example rules\nYerk/YURK\n\nnot a rule\n")
    ns = NS(private=private, folder=tmp_path / "work" / "voice", rows=[], probes=[], heard=HEARD, mm=None, fish=None)

    def wire(mm: Fake, fish: Fake) -> None:
        ns.mm, ns.fish = mm, fish
        request.addfinalizer(mm.close)
        request.addfinalizer(fish.close)
        monkeypatch.setitem(voice_note.ENGINES, "minimax", (mm.url, *voice_note.ENGINES["minimax"][1:]))
        monkeypatch.setitem(voice_note.ENGINES, "fish", (fish.url, *voice_note.ENGINES["fish"][1:]))

    def post(engine, secret, clip, timeout=600):
        length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(clip)],
                                      capture_output=True, text=True).stdout)
        ns.probes.append((engine, secret, clip.name, length))
        return {"segments": [{"start": 0, "end": 2, "text": f" {ns.heard}", "no_speech_prob": 0.01, "avg_logprob": -0.2}]}
    monkeypatch.setattr(transcribe, "post", post)

    def make(script: str = SCRIPT, name: str = "first note", owner_yes: bool = False) -> dict[str, Any]:
        return voice_note.run(script, name, ns.folder, private, private / "secrets" / "openai.key",
                              lambda *a: ns.rows.append(a), owner_yes)
    ns.wire, ns.make = wire, make
    return ns


def probe(path: Path, entries: str) -> list[str]:
    return subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True).stdout.split()


def test_sections_split_at_pause_lines_and_long_paragraphs_and_a_short_one_joins_the_next():
    long_a, long_b = "a" * 2500, "b" * 2500
    one, two = "One section that is long enough to stand alone, at least fifty.", "And a second one that is long enough to stand alone too."
    assert voice_note.sections(f"{one}\n[[pause]]\n\n{two}") == [one, two]
    assert voice_note.sections("Alright, two things.\n[[pause]]\nThe first is the one that takes a while to say.") == [
        "Alright, two things. (break) The first is the one that takes a while to say."]   # the box's stray-syllable rule
    assert voice_note.sections(f"{long_a}\n\n{long_b}") == [long_a, long_b]               # over 4,000: split at its paragraph
    assert voice_note.sections(f"{one}\n[[pause]]\nThe end.") == [f"{one} (break) The end."]   # a short last one joins back
    assert voice_note.sections("\n[[pause]]\n  \n") == []


def test_each_voice_gets_the_markup_it_reads_and_the_check_compares_only_spoken_words():
    assert voice_note.for_minimax("(break) [warm] Hi there. (break) (pause) (laughs) Go. (break)") == "Hi there. <#0.4#> (laughs) Go."
    assert voice_note.for_fish("[warm] Hi there. <#0.6#> Go.") == "[warm] Hi there. (break) Go."
    assert voice_note.said("[warm] Hi there, (break) it's me. (laughs) <#0.4#> Say (sure) 12 times.") == [
        "hi", "there", "it's", "me", "say", "sure", "12", "times"]


def test_the_probe_matches_in_order_and_names_a_word_glued_in_between():
    script = voice_note.said("Alright, next twelve months, my odds. Here is how it goes from here on out.")
    assert voice_note.match(script[:12], script) == (1.0, [])
    share, stray = voice_note.match(voice_note.said("Alright next 12 months my odds pimp here is how it goes"), script)
    assert stray == ["pimp"] and share > 0.75                     # the box's measured defect: one syllable glued on
    assert voice_note.match(voice_note.said("Alright, next 12 months, my odds."), script) == (5 / 6, [])   # a numeral is no stray
    assert voice_note.match(voice_note.said("Completely different words were spoken here instead"), script)[0] < 0.75
    assert voice_note.match([], script) == (0.0, [])


@FFMPEG
def test_a_note_is_made_on_minimax_as_loud_opus_ogg_checked_and_booked(note, tone):
    note.wire(Fake(spoke(tone)), Fake(lambda n, body: (500, b"unused")))
    r = note.make()
    out = Path(r["file"])
    assert r["ok"] and r["engine"] == "minimax/speech-2.8-hd" and out == note.folder / "first-note.ogg" and r["sections"] == 2
    assert r["probe"] == HEARD and r["next"] == words.VOICE_NEXT and "fallback" not in r and not note.fish.calls
    first = note.mm.calls[0]
    assert first["auth"] == "Bearer mm-test-key-123456" and first["body"]["text"] == (
        "Hello from the example room.\n\nThis is only a test, <#0.4#> nothing more.")   # the mood tag dropped, (break) a pause
    assert first["body"]["voice_setting"] == {"voice_id": "English_FriendlyPerson", "speed": 1.18, "pitch": -1, "vol": 1.0}
    assert first["body"]["language_boost"] == "English" and first["body"]["pronunciation_dict"] == {"tone": ["Yerk/YURK"]}
    assert probe(out, "stream=codec_name,sample_rate,channels") == ["opus,48000,1"]
    seconds = float(probe(out, "format=duration")[0])
    assert abs(seconds - 3.5) < 0.15 and r["duration_s"] == round(seconds, 1)       # two 1.5 s sections, 0.5 s apart
    loud = subprocess.run(["ffmpeg", "-v", "info", "-i", str(out), "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True).stderr
    assert abs(float(re.findall(r"I:\s+(-?[\d.]+) LUFS", loud)[-1]) + 14) < 1.5          # loudness to -14 LUFS
    side = json.loads(voice_note.sidecar(out).read_text())
    wave = base64.b64decode(side["waveform"])
    assert voice_note.sidecar(out).stat().st_mode & 0o777 == 0o600 and side["bytes"] == out.stat().st_size
    assert abs(len(wave) - side["duration"] * 10) <= 1 and max(wave) == 255 and voice_note.meta(out) == {
        "duration": side["duration"], "waveform": side["waveform"]}
    assert note.probes == [("openai", "sk-test-openai-key-777", "probe.mp3", pytest.approx(3.5, abs=0.15))]
    chars = [len(c["body"]["text"]) for c in note.mm.calls]
    assert [(e, round(u, 6), n) for e, u, n in note.rows[:2]] == [
        ("minimax/speech-2.8-hd", round(chars[0] * 0.0001, 6), f"section 1 of 2, {chars[0]} characters"),
        ("minimax/speech-2.8-hd", round(chars[1] * 0.0001, 6), f"section 2 of 2, {chars[1]} characters")]
    assert note.rows[2][0] == "openai/whisper-1" and note.rows[2][2].startswith("check, first ") and len(note.rows) == 3
    assert r["usd"] == round(sum(u for _, u, _ in note.rows), 4) and not list(note.folder.glob(".voice-*"))
    assert note.make()["file"].endswith("first-note-2.ogg")          # never over a note already there


@FFMPEG
def test_any_minimax_failure_moves_the_whole_note_to_fish_so_it_never_changes_voice(note, tone):
    ok = spoke(tone)
    note.wire(Fake(lambda n, body: ok(n, body) if n == 1 else broke()), Fake(lambda n, body: (200, tone)))
    r = note.make()
    assert r["ok"] and r["engine"] == "fish/s2.1-pro-free" and r["fallback"] == "minimax: MiniMax 2053 insufficient balance"
    assert len(note.mm.calls) == 2 and len(note.fish.calls) == 2      # section 1 made again on Fish: all or nothing
    first = note.fish.calls[0]
    assert first["auth"] == "Bearer fish-test-key-654321" and first["model"] == "s2.1-pro-free"
    assert first["body"] == {"text": "[warm] Hello from the example room.\n\nThis is only a test, (break) nothing more.",
                             "format": "mp3", "reference_id": "ca4ec67b92ae4d90829eb40dcd672312"}   # Fish acts on the mood tag
    assert [(e, u) for e, u, _ in note.rows] == [("minimax/speech-2.8-hd", pytest.approx(len(note.mm.calls[0]["body"]["text"]) * 1e-4)),
                                                 ("fish/s2.1-pro-free", 0.0), ("fish/s2.1-pro-free", 0.0),
                                                 ("openai/whisper-1", pytest.approx(3.5 * 0.0001, abs=2e-5))]


@FFMPEG
def test_with_neither_voice_there_is_no_note_and_no_key_in_the_error(note, tone):
    note.wire(Fake(lambda n, body: (500, b"busy")), Fake(lambda n, body: (402, b'{"message": "no credit for fish-test-key-654321"}')))
    r = note.make()
    assert not r["ok"] and r["error"].startswith("neither voice made it, so there is no note: minimax: HTTP 500 busy; fish: HTTP 402")
    assert "fish-test-key" not in r["error"] and "<key>" in r["error"] and not note.rows and not note.probes
    assert list(note.folder.iterdir()) == []


@FFMPEG
def test_a_note_whose_first_words_do_not_read_back_as_the_script_is_deleted(note, tone):
    note.wire(Fake(spoke(tone)), Fake(lambda n, body: (200, tone)))
    note.heard = "Completely different words came back from this one instead"
    r = note.make()
    assert not r["ok"] and r["error"].startswith("the note failed its check, so it was deleted") and r["engine"] == "minimax/speech-2.8-hd"
    assert f'read back as "{note.heard}" where the script opens "Hello from the example room. This is only a"' in r["error"]
    assert not list(note.folder.glob("*.ogg")) and [e for e, _, _ in note.rows][-1] == "openai/whisper-1"   # paid, and booked
    note.heard = "Hello from the example room pimp. This is only a test, nothing more."
    assert note.make()["error"].startswith("the note failed its check")          # one word glued on fails it too


@FFMPEG
def test_no_key_no_check_key_and_a_note_past_the_gate_spend_nothing_until_the_owner_says_yes(note, tone):
    note.wire(Fake(spoke(tone)), Fake(lambda n, body: (200, tone)))
    (note.private / "secrets" / "openai.key").write_text("\n")
    assert note.make() == {"ok": False, "error": words.VOICE_NO_CHECK}
    (note.private / "secrets" / "openai.key").write_text("sk-test-openai-key-777\n")
    long = "\n\n".join(f"Paragraph {i} " + "word " * 100 for i in range(21))      # about 10,800 characters: about US$1.08
    size = sum(len(voice_note.for_minimax(s)) for s in voice_note.sections(long))
    r = note.make(long)
    assert not r["ok"] and r["error"] == words.VOICE_OVER_GATE.format(chars=size, usd=size * 1e-4 + 20 * 1e-4) and size > 10_000
    assert note.make("(break) [warm]") == {"ok": False, "error": words.VOICE_EMPTY} and not note.mm.calls and not note.rows
    note.heard = " ".join(long.split()[:40])
    r = note.make(long, owner_yes=True)
    assert r["ok"] and len(note.mm.calls) == len(voice_note.sections(long)) == 3
    for k in ("minimax.key", "fish.key"):
        (note.private / "secrets" / k).unlink()
    assert note.make() == {"ok": False, "error": words.VOICE_NO_KEY}


def test_a_key_never_reaches_an_error(monkeypatch):
    def refuse(req, timeout):
        assert req.get_header("Authorization") == "Bearer mm-abc-123456789"
        raise voice_note.urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"bad key mm-abc-123456789"))
    monkeypatch.setattr(voice_note.urllib.request, "urlopen", refuse)
    with pytest.raises(voice_note.Failed) as e:
        voice_note.call("https://example.invalid/v1", {"text": "hi"}, "mm-abc-123456789")
    assert str(e.value) == "HTTP 401 bad key <key>"


def test_meta_is_a_note_this_tool_made_and_nothing_changed_since(tmp_path):
    ogg = tmp_path / "hello.ogg"
    ogg.write_bytes(b"OggS" * 10)
    wave = base64.b64encode(bytes([0, 128, 255])).decode()
    voice_note.sidecar(ogg).write_text(json.dumps({"duration": 2.004, "waveform": wave, "bytes": 40}))
    assert voice_note.meta(ogg) == {"duration": 2.0, "waveform": wave}
    ogg.write_bytes(b"OggS" * 11)                                           # rewritten since: a plain file now
    assert voice_note.meta(ogg) is None and voice_note.meta(tmp_path / "other.ogg") is None
    voice_note.sidecar(ogg).write_text(json.dumps({"duration": 2.0, "waveform": base64.b64encode(bytes(257)).decode(), "bytes": 44}))
    assert voice_note.meta(ogg) is None                                     # Discord takes 256 points at most


def voice_room(tmp_path):
    client = Client()
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=client, state=tmp_path)
    d.add_room(ROOM_CH, Room(tmp_path))
    ogg = tmp_path / "rooms" / "test-room" / "work" / "voice" / "hello.ogg"
    ogg.parent.mkdir(parents=True)
    ogg.write_bytes(b"OggS" + bytes(range(200)))
    return d, client, ogg, {"duration": 3.52, "waveform": base64.b64encode(bytes(range(0, 256, 8))).decode()}


def test_a_note_goes_to_discord_as_a_voice_message_with_its_duration_and_waveform(tmp_path):
    d, client, ogg, meta = voice_room(tmp_path)
    got = []

    async def send_message(channel_id, *, params):
        payload, part = json.loads(params.multipart[0]["value"]), params.multipart[1]
        got.append((channel_id, payload, part["name"], part["content_type"], part["filename"], part["value"].read()))
        return {"id": "4242", "flags": 8192}
    client.http = NS(send_message=send_message)
    assert asyncio.run(d.send_voice("test-room", ogg, meta, key="t1-s1", inbound="5")) == (["4242"], "")
    channel, payload, name, kind, filename, data = got[0]
    assert channel == ROOM_CH and payload["flags"] == 8192 and "content" not in payload and payload["nonce"] == "t1-s1:0"
    assert payload["attachments"] == [{"id": 0, "filename": "hello.ogg", "duration_secs": 3.52, "waveform": meta["waveform"]}]
    assert (name, kind, filename, data) == ("files[0]", "audio/ogg", "hello.ogg", ogg.read_bytes())   # audio/, or Discord ignores both
    row = d.ledger.deliveries("test-room", "sent")[0]
    assert (row["key"], row["kind"], row["file"], row["message_id"]) == ("t1-s1:0", "file", "hello.ogg", "4242")
    assert len(list((tmp_path / "rooms" / "test-room" / "outbox").glob("*-t1-s1-hello.ogg"))) == 1      # sent from a snapshot


def test_a_voice_message_discord_refuses_or_takes_plain_goes_as_the_audio_file_and_says_why(tmp_path):
    d, client, ogg, meta = voice_room(tmp_path)
    ch = client.chans[ROOM_CH]

    async def refuse(channel_id, *, params):
        raise discord.HTTPException(NS(status=400, reason="Bad Request"), {"code": 50161, "message": "voice messages refused"})
    client.http = NS(send_message=refuse)
    ids, why = asyncio.run(d.send_voice("test-room", ogg, meta, key="t2-s1"))
    assert why == words.file_why("refused") and ids == [str(ch.sent[-1].id)] and ch.sent[-1].attachments[0].filename == "hello.ogg"

    async def plain(channel_id, *, params):
        return {"id": "4343", "flags": 0}
    client.http = NS(send_message=plain)
    assert asyncio.run(d.send_voice("test-room", ogg, meta, key="t3-s1")) == (["4343"], words.VOICE_PLAIN)


def test_send_takes_a_note_as_a_voice_message_and_its_words_after_it(tmp_path, capsys):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    ogg = tmp_path / "rooms" / "example-room" / "work" / "voice" / "hello.ogg"
    ogg.parent.mkdir(parents=True)
    ogg.write_bytes(b"OggS" * 10)
    voice_note.sidecar(ogg).write_text(json.dumps({"duration": 2.0, "waveform": base64.b64encode(b"\x10\x80\xff").decode(), "bytes": 40}))
    r = asyncio.run(y.tool_send("example-room", "here it is", str(ogg), "example-room"))
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert r["ok"] and r["voice_message"] is True and len(r["message_ids"]) == 2 and "note" not in r
    assert (out[0]["voice"], out[0]["duration"], out[1]["send"]) == (str(ogg), 2.0, "here it is")
    ogg.write_bytes(b"OggS" * 11)                                           # changed since it was made: a plain file
    r = asyncio.run(y.tool_send("example-room", "", str(ogg), "example-room"))
    assert r["ok"] and "voice_message" not in r and json.loads(capsys.readouterr().out)["file"] == str(ogg)


def test_the_tool_answers_a_known_room_and_says_when_no_voice_key_is_there(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    assert "no room 'nowhere'" in asyncio.run(y.tool_voice_note("nowhere", "Hello there."))["error"]
    assert asyncio.run(y.tool_voice_note("example-room", "Hello there, this is a test.")) == {"ok": False, "error": words.VOICE_NO_KEY}
    assert "mcp__yerk__voice_note" in YERK_TOOLS


def test_yerk_explain_voice_note_says_how_it_works_what_it_costs_and_that_it_is_english_only(tmp_path, capsys):
    assert cli.main(["explain", "voice_note", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path / "s")]) == 0
    out = capsys.readouterr().out.splitlines()
    tab = abilities.manifest()
    mark, said = words.ability("needs", provider="MiniMax, and Fish Audio when MiniMax fails")
    assert out[0] == f"{mark} voice_note: {said}"
    assert "How it works:" in out and [x[2:].split(":")[0] for x in out if x.startswith("- ")] == [
        "Script", "Voice", "Sound", "Check", "Send", "Spend", "English only"]
    assert any(x.startswith("Cost: US$100 a million characters on MiniMax speech-2.8-hd") for x in out)
    private = tmp_path / "p"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "secrets").mkdir()
    (private / "secrets" / "openai.key").write_text("sk-test\n")
    s = memory.settings(private, tmp_path / "s")
    assert abilities.state("voice_note", tab["voice_note"], tab, s)[1] != words.ability("on")[1]     # the check's key alone
    (private / "secrets" / "fish.key").write_text("fish-test\n")
    assert abilities.state("voice_note", tab["voice_note"], tab, s) == words.ability("on")


def test_the_room_makes_one_only_when_asked_and_every_line_it_reads_is_in_shape():
    text = context.MECHANICS.read_text().strip()
    line = next(x for x in text.splitlines() if "`voice_note`" in x)
    assert "only when the owner asks for a voice note or to say it out loud" in line and "voice message" in line
    assert context.chars(text) < budgets()["session_start"]["appended"]["mechanics"]
    for said in (line, words.VOICE_NEXT, words.VOICE_OVER_GATE, words.VOICE_PROBE_FAILED, words.VOICE_AS_FILE, words.VOICE_NO_CHECK):
        assert shape.offenses(said) == [], said
    assert words.STEP["voice_note"] == "making a voice note"


@pytest.mark.skipif(os.environ.get("YERK_VOICE_LIVE") != "1" or not os.environ.get("YERK_PRIVATE"),
                    reason="a real note: YERK_VOICE_LIVE=1 and keys in $YERK_PRIVATE/secrets/")
@FFMPEG
def test_live_a_two_sentence_note_is_made_and_checked(tmp_path):
    private = Path(os.environ["YERK_PRIVATE"]).expanduser()
    rows: list[tuple[str, float, str]] = []
    r = voice_note.run("Hello from Yerk. This is a short test of the voice note tool.", "live", tmp_path / "voice", private,
                       private / "secrets" / "openai.key", lambda *a: rows.append(a))
    assert r["ok"], r
    assert "hello" in r["probe"].lower() and rows and voice_note.meta(Path(r["file"]))
