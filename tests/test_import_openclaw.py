"""Checks `yerk import openclaw:`, which copies the notes of an OpenClaw workspace, another assistant's files, into the private folder with
secrets taken out, against a small OpenClaw workspace built here. The cases: each part copied or skipped by its rule, the doctor's secret shapes
and a line whose key names a secret written as their kind on the copy's own lines (whatever the value: a short token, a pin, a password with no
digit), a second run writing nothing, a file the box changed or deleted carried over and a part it no longer has held, --since and --dry-run, what
it refuses, and the yerkd.toml and rooms.toml lines that read the copies printed in place and never written. Every value here is a placeholder."""
import os
import shutil
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from yerk_ai import cli, memory, sources

CODE, PASSWORD, TOKEN = "your code is 482913", "password: placeholder-7!", "token: " + "placeholder" * 2   # the doctor's shapes
KEYED = {"token: abc123": "abc123", "password: hunter": "hunter", "pin: 123456": "123456",            # under the doctor's floor
         "Authorization: Bearer abc123def456": "abc123def456", "token: placeholder1234567": "placeholder1234567"}  # 18: over it
KEYS = ("token", "api_key", "apikey", "secret", "password", "passwd", "pin", "bearer", "authorization", "cookie", "session",
        "private_key")
FILES = {"MEMORY.md": "# the index\n- the kettle is on the shelf\n", "USER.md": "# the owner\n- tea, no sugar\n",
         "memory/2026-04-01.md": f"# 2026-04-01\n- 09:00 the morning\n- 09:05 {CODE} for the login\n- 09:06 {PASSWORD}\n"
                                 f"- 09:07 {TOKEN}\n- 09:10 the end\n",
         "memory/facts/kettle.md": "# kettle\n- blue, on the shelf\n", "memory/transcripts/call.md": "# a call\n",
         "memory/transcripts/call.vtt": "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", "memory/transcripts/call.csv": "a,b\n1,2\n",
         "memory/transcripts/deck.pptx": "a deck", "memory/state.json": "{}", "memory/notes.md.bak": "- an old copy\n",
         "memory/health/beat.md": "- a beat\n", "memory/jobstate/run.md": "- a run\n", "memory/finance/month.md": "- a sum\n",
         "memory/sms/texts.md": "- a text\n", "memory/node_modules/x.md": "- a module\n", "memory/.blobs/blob.md": "- a blob\n",
         "memory/secrets/key.md": "- a key\n", "wiki/PRIMER.md": "# the primer\n", "wiki/.git/HEAD": "ref: main\n",
         "media/gym/front.jpg": b"\xff\xd8 a picture", "media/gym/front.seen.txt": "what it shows: the shelf\n"}
COPIED = {"MEMORY.md", "USER.md", "memory/2026-04-01.md", "memory/facts/kettle.md", "memory/transcripts/call.md",
          "memory/transcripts/call.vtt", "memory/transcripts/call.csv", "wiki/PRIMER.md", "media/gym/front.jpg",
          "media/gym/front.seen.txt"}


@pytest.fixture
def box(tmp_path):
    ws = tmp_path / "box" / "workspaces" / "helper"
    for rel, body in FILES.items():
        (p := ws / rel).parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body) if isinstance(body, bytes) else p.write_text(body)
    (ws / "memory" / "link.md").symlink_to(ws / "memory" / "facts" / "kettle.md")
    (priv := tmp_path / "private").mkdir()
    return ws, priv, tmp_path / "state"


def run(ws, priv, state, *args):
    return cli.main(["import", f"openclaw:{ws}", *args, "--private", str(priv), "--state", str(state)])


def tree(d):
    return {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()}


def kept(d):
    return {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in d.rglob("*") if p.is_file()}


def test_each_part_is_copied_or_skipped_by_its_rule_and_a_secret_shape_leaves_as_its_kind(box, capsys):
    ws, priv, state = box
    assert run(ws, priv, state) == 0
    out, copy = capsys.readouterr().out, priv / "openclaw" / "helper"
    assert tree(priv) == {f"openclaw/helper/{r}" for r in COPIED} | {"openclaw/helper.sums"}
    for rel in COPIED - {"memory/2026-04-01.md"}:
        assert (copy / rel).read_bytes() == (ws / rel).read_bytes()
    assert all((copy / r).stat().st_mtime_ns == (ws / r).stat().st_mtime_ns for r in COPIED)
    day, was = (copy / "memory/2026-04-01.md").read_text().split("\n"), FILES["memory/2026-04-01.md"].split("\n")
    assert [i for i in range(len(was)) if day[i] != was[i]] == [2, 3, 4] and len(day) == len(was)
    assert day[2:5] == ["- 09:05 [one-time-code withheld] for the login", "- 09:06 [password-line withheld]",
                        "- 09:07 [token-line withheld]"]
    for line in (f"copy MEMORY.md: 1 file, {len(FILES['MEMORY.md'])} bytes", "copy memory/, the kinds the index reads: 3 files",
                 "copy memory/transcripts/, its text files too (.csv .txt .json .html .vtt): 2 files",
                 "copy wiki/, the kinds the index reads: 1 file", "copy media/gym/, every file (a picture beside its seen sidecar): 2 files",
                 "skip a kind the index does not read: 2 files", "skip memory/health/: ", "skip memory/jobstate/: ",
                 "skip memory/finance/: ", "skip memory/sms/: texts, one-time codes among them: 1 file",
                 "skip a temp or backup copy (*.tmp, *.bak, *.pre-*): 1 file", "skip a link, as the index does: 1 file",
                 "skip a folder the index skips (node_modules, __pycache__, overwrite-backups): 1 file",
                 "skip a dot or secrets folder, never entered: memory/.blobs, memory/secrets, wiki/.git",
                 "scrub: 3 lines in 1 files", "  memory/2026-04-01.md:3 (one-time code)", "  memory/2026-04-01.md:4 (password line)",
                 "  memory/2026-04-01.md:5 (token line)", "openclaw:helper: 10 files, "):
        assert line in out
    assert "10 written (10 new, 0 changed), 0 the same" in out
    files = [p for p in (priv / "openclaw").rglob("*") if p.is_file()]
    dirs = [priv / "openclaw", *(p for p in (priv / "openclaw").rglob("*") if p.is_dir())]
    assert {stat.S_IMODE(p.stat().st_mode) for p in files} == {0o600} and {stat.S_IMODE(p.stat().st_mode) for p in dirs} == {0o700}


def test_a_line_whose_key_names_a_secret_leaves_as_its_kind_whatever_its_value(box, capsys):
    ws, priv, state = box
    prose = "- the token economy, a word in a sentence"
    (ws / "memory/2026-04-02.md").write_text("\n".join(["# 2026-04-02", prose, *(f"- {x}" for x in KEYED), ""]))
    assert run(ws, priv, state) == 0
    out, copy = capsys.readouterr().out, (priv / "openclaw/helper/memory/2026-04-02.md").read_text()
    assert copy.split("\n") == ["# 2026-04-02", prose, *["- [token-line withheld]"] * len(KEYED), ""]
    assert "scrub: 8 lines in 2 files" in out and all(f"  memory/2026-04-02.md:{n} (token line)" in out for n in range(3, 8))
    landed = [p for p in priv.rglob("*") if p.is_file() for v in KEYED.values() if v.encode() in p.read_bytes()]
    assert landed == [] and not any(v in out for v in KEYED.values())


def test_on_an_import_the_key_is_the_tell_not_the_value():
    lines = [f"{k}{sep}abc" for k in (*KEYS, *(k.upper() for k in KEYS)) for sep in (": ", "=", " = ")]
    for line in [*lines, '"api_key": "abc"', "ACCESS_TOKEN=abc", "**PIN**: abc", "passphrase: abc"]:
        out, hits = sources.withheld(f"- {line}\n".encode())
        assert hits == [(1, "token line")] and b"abc" not in out and out.endswith(b"[token-line withheld]\n"), line
    for line in (b"- the token economy, a word in a sentence\n", b"- tokens: 1,200 a day\n", b"- a spin: 3 laps\n"):
        assert sources.withheld(line) == (line, [])


def test_a_second_run_writes_nothing_and_a_box_change_deletion_and_lost_part_are_carried(box, capsys):
    ws, priv, state = box
    assert run(ws, priv, state) == 0
    capsys.readouterr()
    copy, record = priv / "openclaw" / "helper", priv / "openclaw" / "helper.sums"
    before, text = kept(priv / "openclaw"), record.read_bytes()
    assert run(ws, priv, state) == 0
    out = capsys.readouterr().out
    assert "0 written (0 new, 0 changed), 10 the same, 0 the box deleted taken out" in out
    assert kept(priv / "openclaw") == before and record.read_bytes() == text
    assert "scrub: 3 lines in 1 files" in out and "  memory/2026-04-01.md:4 (password line)" in out   # said from the record
    (ws / "memory/facts/kettle.md").write_text("# kettle\n- green now\n")
    (ws / "wiki/PRIMER.md").unlink()
    shutil.rmtree(ws / "media" / "gym")
    assert run(ws, priv, state) == 0
    out = capsys.readouterr().out
    assert "1 written (0 new, 1 changed), 6 the same, 1 the box deleted taken out" in out
    assert "hold 2 copies under a part the box no longer has (a folder gone is no file deleted): kept" in out
    assert (copy / "memory/facts/kettle.md").read_text() == "# kettle\n- green now\n" and not (copy / "wiki/PRIMER.md").exists()
    assert (copy / "media/gym/front.jpg").read_bytes() == FILES["media/gym/front.jpg"] and "media/gym/front.jpg" in record.read_text()
    assert run(ws, priv, state) == 0 and "0 written (0 new, 0 changed), 7 the same" in capsys.readouterr().out


def test_dry_run_writes_nothing_and_since_takes_the_files_changed_from_that_day(box, capsys):
    ws, priv, state = box
    assert run(ws, priv, state, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "(a dry run: nothing written)" in out and "10 to write (10 new, 0 changed)" in out
    assert "  memory/2026-04-01.md:3 (one-time code)" in out and "the lines that read the copies" not in out
    assert tree(priv) == set()
    old = datetime(2026, 1, 2, tzinfo=ZoneInfo("America/Toronto")).timestamp()
    for rel in ("MEMORY.md", "USER.md"):
        os.utime(ws / rel, (old, old))
    assert run(ws, priv, state, "--since", "2026-03-01") == 0
    assert "skip a file older than --since 2026-03-01, its copy as it is: 2 files" in capsys.readouterr().out
    assert tree(priv / "openclaw" / "helper") == COPIED - {"MEMORY.md", "USER.md"}
    assert run(ws, priv, state) == 0 and "2 written (2 new, 0 changed), 8 the same" in capsys.readouterr().out


def test_what_it_refuses_before_anything_is_written(box, tmp_path, monkeypatch, capsys):
    ws, priv, state = box
    monkeypatch.delenv("YERK_PRIVATE", raising=False)
    assert run(tmp_path / "nowhere", priv, state) == 2
    assert cli.main(["import", "openclaw:", "--private", str(priv), "--state", str(state)]) == 2
    assert run(ws, ws / "memory", state) == 2                  # the private folder inside the box
    assert cli.main(["import", f"openclaw:{ws}", "--state", str(state)]) == 2     # the example layer: the repository
    err = capsys.readouterr().err
    assert "is not a folder" in err and "takes openclaw:<" in err and "hold one another" in err and "never goes into the repository" in err
    assert tree(priv) == set() and not (ws / "memory" / "openclaw").exists()
    (other := tmp_path / "elsewhere" / "helper").mkdir(parents=True)
    (other / "USER.md").write_text("# another\n")
    assert run(ws, priv, state) == 0 and run(other, priv, state) == 2
    (stray := priv / "openclaw" / "spare").mkdir()
    (stray / "note.md").write_text("- by hand\n")
    (spare := tmp_path / "box" / "workspaces" / "spare").mkdir()
    (spare / "USER.md").write_text("# spare\n")
    assert run(spare, priv, state) == 2
    err = capsys.readouterr().err
    assert f"openclaw/helper: the copy of {ws}, not of {other}" in err and "files no import wrote (no spare.sums beside it)" in err
    assert tree(priv / "openclaw" / "spare") == {"note.md"}


def test_the_lines_that_read_the_copies_are_printed_in_place_and_never_written(box, tmp_path, capsys):
    ws, priv, state = box
    (priv / "yerkd.toml").write_text(f'[memory]\nroot = "memory"\ntimezone = "America/Toronto"\n'
                                     f'also = ["{ws}/MEMORY.md", "{ws}/memory", "imports/texts", "{ws}/wiki"]   # the box\n')
    (priv / "rooms.toml").write_text(f'[rooms.desk]\nread = ["{ws}/memory", "{tmp_path}/files"]\nalso = ["{ws}/MEMORY.md", '
                                     f'"{ws}/media/gym"]\n\n[rooms.desk.sandbox.filesystem]\n'
                                     f'denyRead = ["/{ws.parent}", "//home/example/.ssh"]\n')
    conf, rooms = (priv / "yerkd.toml").read_bytes(), (priv / "rooms.toml").read_bytes()
    assert run(ws, priv, state) == 0
    out, c = capsys.readouterr().out, priv.resolve() / "openclaw" / "helper"
    also = [f"{c}/MEMORY.md", f"{c}/memory", "imports/texts", f"{c}/wiki"]
    assert "the lines that read the copies, by hand (never written here), then yerk-index and yerkd restarted:" in out
    assert f'yerkd.toml:4 to:  also = ["{c}/MEMORY.md", "{c}/memory", "imports/texts", "{c}/wiki"]   # the box' in out
    assert "rooms.toml:2: its `read` stays while the box runs" in out and "rooms.toml:2 to:" not in out
    assert f'rooms.toml:3 to:  also = ["{c}/MEMORY.md", "{c}/media/gym"]' in out
    assert f'rooms.toml:6 to:  denyRead = ["/{ws.parent}", "/{priv.resolve()}/openclaw", "//home/example/.ssh"]' in out
    assert (priv / "yerkd.toml").read_bytes() == conf and (priv / "rooms.toml").read_bytes() == rooms
    names = lambda xs: [n for _, n in memory.settings(priv, state, {"root": "memory", "also": xs}).cites]   # noqa: E731
    assert names(also) == names([f"{ws}/MEMORY.md", f"{ws}/memory", "imports/texts", f"{ws}/wiki"])
    assert run(ws, priv, state) == 0 and out.split("restarted:")[1] == capsys.readouterr().out.split("restarted:")[1]
