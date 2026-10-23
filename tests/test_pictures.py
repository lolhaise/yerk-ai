"""Checks that a picture sent to a room gets a text file beside it in the same turn, NAME.seen.txt, with the words on it and what it shows, so it can
tool: the room's own read is the engine, the header and path are code), and a person's room files none; the index takes the
sidecar as an inbox text kind, class private, and recall and read_memory show its lines behind their mark, never as anyone's
their mark, never as anyone's words; `yerk pictures backfill` files the older ones through Yerk workers, a batch of 20 at a time (pictures-seen,
placeholder (decision 22)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from test_remember import daemon

from yerk_ai import cli, memory, pictures, workers, words
from yerk_ai.daemon import ConsoleChannel

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
WORDS = "EX-42 TOTAL 1,250 sf\nPat: see you at 9\nRiley: works for me"


class Rec(ConsoleChannel):
    """A room that keeps what was posted to it: its lines and the rooms its worker cards went to."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.cards: list[str] = []

    async def notify(self, room: str, text: str) -> list[str]:
        self.lines.append(text)
        return [f"n{len(self.lines)}"]

    async def worker_card(self, room: str, key: str, text: str, message_id: str | None = None) -> str | None:
        self.cards.append(room)
        return "card-1"

    async def worker_settle(self, room: str, key: str, text: str, how: str, message_id: str | None = None) -> str | None:
        self.cards.append(room)
        return "card-1"


class Units:
    """systemd-run as Workers relies on it, recorded, nothing run."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def start(self, unit, argv, env, cwd, out, err, cap, post) -> None:
        self.calls.append({"unit": unit, "cwd": Path(cwd), "env": dict(env)})

    def stop(self, unit) -> None:
        pass

    def running(self) -> set[str]:
        return {c["unit"] for c in self.calls}


def picture(state: Path, room: str, mid: str, name: str = "photo.png", at: float = 1_760_000_000.0) -> Path:
    (d := state / "rooms" / room / "work" / "inbox" / mid).mkdir(parents=True, exist_ok=True)
    (p := d / name).write_bytes(PNG)
    os.utime(p, (at, at))
    return p.resolve()


def test_an_owner_room_files_a_picture_it_was_sent_and_a_persons_room_files_none(tmp_path):
    y = daemon(tmp_path, "example-room", "her-room")
    mine, hers = picture(tmp_path, "example-room", "m-1"), picture(tmp_path, "her-room", "m-2")
    r = asyncio.run(y.tool_seen("example-room", str(mine), WORDS, "a screenshot of a chat between Pat and Riley\n"))
    side = mine.with_name("photo.seen.txt")
    assert r == {"ok": True, "file": str(side), "next": words.FILED_NEXT.format(clause=words.FILED_AT_MOST)}
    lines = side.read_text().splitlines()
    assert lines[0] == pictures.header("example-room", mine, y.memory.tz) and lines[0].startswith("seen in example-room, message m-1, ")
    assert lines[0].endswith(": photo.png") and lines[1:4] == WORDS.splitlines()          # who said what, exactly as shown
    assert lines[4:] == ["what it shows: a screenshot of a chat between Pat and Riley"] and side.stat().st_mode & 0o777 == 0o600
    assert asyncio.run(y.tool_seen("example-room", str(mine), "", "a blank page"))["ok"]         # read again: the new reading
    assert mine.with_name("photo.seen.txt").read_text().splitlines()[1:] == [pictures.NONE, "what it shows: a blank page"]
    assert asyncio.run(y.tool_seen("her-room", str(hers), WORDS, "a chat")) == {"ok": False, "error": words.SEEN_OWNER_ONLY}
    assert not hers.with_name("photo.seen.txt").exists()                                     # a person's room: never filed
    (notes := mine.with_name("notes.txt")).write_text("example\n")
    loose = tmp_path / "rooms" / "example-room" / "work" / "loose.png"
    loose.write_bytes(PNG)
    for p in (hers, notes, loose):                                                          # not a picture sent to this room
        assert asyncio.run(y.tool_seen("example-room", str(p), WORDS, "x")) == {
            "ok": False, "error": words.SEEN_NOT_HERE.format(name=p.name)}
    assert asyncio.run(y.tool_seen("example-room", str(mine), WORDS, " ")) == {"ok": False, "error": words.SEEN_NO_SHOWS}
    assert "mcp__yerk__seen" in y.spec_for("example-room").settings["permissions"]["allow"]


def test_a_picture_read_and_not_filed_bounces_once_in_an_owner_room_only(tmp_path):
    y = daemon(tmp_path, "example-room", "her-room")
    for name, mid in (("example-room", "m-1"), ("her-room", "m-2")):
        p = str(picture(tmp_path, name, mid))
        room = y.rooms[name]
        room.to_open, room.rp, room.bounced = {p: "photo.png"}, NS(read_paths={p}), False
    own, hers = y.rooms["example-room"], y.rooms["her-room"]
    reason = own.stop_check(active=False)
    assert reason and "photo.png" in reason and "Call seen on " in reason and "give your answer again" in reason
    assert own.stop_check(active=False) is None                              # once per result
    assert hers.stop_check(active=False) is None                             # a person's room files no picture
    own.bounced = False
    pictures.file("example-room", Path(next(iter(own.to_open))), WORDS, "a chat", y.memory.tz)
    assert own.stop_check(active=False) is None                              # filed: nothing to say


def test_the_index_takes_a_sidecar_as_an_owner_inbox_text_kind_class_private_and_a_persons_never(tmp_path):
    (priv := tmp_path / "private").mkdir()
    (priv / "yerkd.toml").write_text('[memory]\nroot = "memory"\n')
    (priv / "rooms.toml").write_text('[rooms.own-room]\nchannel = "console:own"\n\n'
                                     '[rooms.her-room]\nchannel = "console:her"\nprivacy = "family"\npeople = [1001]\n')
    state = tmp_path / "state"
    own, hers = picture(state, "own-room", "m-1"), picture(state, "her-room", "m-2")
    s = memory.settings(priv, state)
    side = pictures.file("own-room", own, "---\nclass: shared\n---\nEX-42", "a page", s.tz)     # front matter on a picture
    (hers.with_name("photo.seen.txt")).write_text("example of hers\n")                           # never filed, never indexed
    files = {p.rsplit("/", 1)[-1] for p in s.scope.files() if "/work/inbox/" in p}
    assert files == {"photo.seen.txt"} and s.scope.contains(side) and not s.scope.contains(hers.with_name("photo.seen.txt"))
    assert memory.cite(s, side) == "own-room/work/inbox/m-1/photo.seen.txt"
    assert memory.file_class(side) == ("private", "")          # its header comes first: no front matter can make it shared
    her = memory.Reader(name="her-room", rid="her-room", owner=False)
    assert not memory.read(s, "own-room/work/inbox/m-1/photo.seen.txt", reader=her)["ok"]
    assert not memory.read(s, str(hers.with_name("photo.seen.txt")))["ok"]


def test_recall_and_read_memory_show_a_sidecar_line_behind_its_mark_never_as_anyones_words(tmp_path):
    (priv := tmp_path / "private").mkdir()
    (priv / "yerkd.toml").write_text('[memory]\nroot = "memory"\n')
    (priv / "rooms.toml").write_text('[rooms.own-room]\nchannel = "console:own"\n')
    state = tmp_path / "state"
    own = picture(state, "own-room", "m-1")
    s = memory.settings(priv, state)
    side = pictures.file("own-room", own, WORDS, "a screenshot of a chat", s.tz)
    (talk := own.with_name("talk.transcript.txt")).write_text("[00:00:01] Pat: see you at 9\n")
    text = memory.read(s, "own-room/work/inbox/m-1/photo.seen.txt")["text"].splitlines()
    assert len(text) == 5 and all(x.startswith(f"{memory.SEEN_MARK} ") for x in text)
    assert text[2] == f"{memory.SEEN_MARK} Pat: see you at 9"                    # a person's line on it, the room's reading
    hit = {"cite": "own-room/work/inbox/m-1/photo.seen.txt:2", "path": str(side), "line": 2, "text": "\n".join(WORDS.splitlines())}
    assert all(x.startswith(memory.SEEN_MARK) for x in memory.snippets([hit], {}, 900)[0]["text"].splitlines() if x.strip())
    assert memory.read(s, str(talk))["text"] == "[00:00:01] Pat: see you at 9"   # a transcript is not a picture's sidecar
    assert memory.is_seen(side) and not memory.is_seen(talk) and not memory.is_seen(tmp_path / "work" / "x.seen.txt")


def test_backfill_skips_a_picture_that_has_one_and_dry_run_starts_nothing(tmp_path, monkeypatch, capsys):
    (priv := tmp_path / "private").mkdir()
    (priv / "rooms.toml").write_text('[rooms.own-room]\nchannel = "console:own"\n\n'
                                     '[rooms.her-room]\nchannel = "console:her"\nprivacy = "family"\npeople = [1001]\n')
    state = tmp_path / "state"
    old, new = picture(state, "own-room", "m-1", at=1_760_000_000.0), picture(state, "own-room", "m-3", "b.JPG", at=1_760_000_900.0)
    done = picture(state, "own-room", "m-2", at=1_760_000_500.0)
    pictures.file("own-room", done, "EX-42", "a page", ZoneInfo("UTC"))
    (old.parent / "scan.pdf").write_bytes(b"%PDF")
    picture(state, "own-room", ".transcribe-x")                                   # a tool's own temp folder
    assert pictures.pending(state / "rooms" / "own-room" / "work" / "inbox") == [old, new]   # oldest first, the filed one skipped
    monkeypatch.setattr(cli, "_control", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a dry run started something")))
    base = ["--private", str(priv), "--state", str(state)]
    assert cli.main(["pictures", "backfill", "own-room", "--dry-run", *base]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [str(old.with_name("photo.seen.txt")), str(new.with_name("b.seen.txt")),
                   "2 picture(s) in own-room with no sidecar, 1 batch(es) of 20; nothing started (--dry-run)"]
    assert cli.main(["pictures", "backfill", "own-room", "--dry-run", "--limit", "1", *base]) == 0
    assert capsys.readouterr().out.splitlines()[0] == str(old.with_name("photo.seen.txt"))
    assert not new.with_name("b.seen.txt").exists() and not old.with_name("photo.seen.txt").exists()
    assert cli.main(["pictures", "backfill", "her-room", "--dry-run", *base]) == 2
    assert capsys.readouterr().err.strip() == words.PICTURES_PERSON.format(room="her-room")


def test_backfill_runs_a_batch_at_a_time_on_the_rooms_account_its_card_and_line_in_the_machine_room(tmp_path):
    y = daemon(tmp_path, "example-room", "her-room")
    y.room_confs["ops-room"] = {**y.room_confs["example-room"], "role": "machine", "id": "ops-room-id"}
    y.daily.rooms["ops-room"] = ("ops-room-id", "private")
    ops = Rec()
    y.add_room("ops-room", ops)
    y.workers.launcher = units = Units()
    pics = [picture(tmp_path, "example-room", f"m-{i:02}", at=1_760_000_000.0 + i) for i in range(25)]
    pictures.file("example-room", picture(tmp_path, "example-room", "m-99", at=1_760_001_000.0), "", "a page", y.memory.tz)
    assert asyncio.run(y.control({"pictures": {"room": "her-room"}})) == {
        "ok": False, "error": words.PICTURES_PERSON.format(room="her-room")}
    r = asyncio.run(y.control({"pictures": {"room": "example-room"}}))
    assert r["ok"] and r["room"] == "ops-room" and (r["pictures"], r["batches"]) == (25, 2) and len(units.calls) == 1
    assert asyncio.run(y.control({"pictures": {"room": "example-room"}})) == {             # one chain a room at a time
        "ok": False, "error": words.PICTURES_RUNNING.format(room="example-room")}
    own = y.rooms["example-room"]._account()
    d = Path(r["staging"])
    spec = json.loads((d / "worker.json").read_text())
    assert (spec["room"], spec["bind"], spec["account"]) == ("ops-room", "example-room", own.name) and spec["id"] == r["id"]
    assert spec["pictures"] == {"room": "example-room", "n": 1, "of": 2, "rest": [[str(p) for p in pics[:20]], [str(p) for p in pics[20:]]]}
    brief = (d / "BRIEF.md").read_text()
    assert all(f"Read {p}\n   Write {p.with_name('photo.seen.txt')}\n   its first line exactly: "
               f"{pictures.header('example-room', p, y.memory.tz)}" in brief for p in pics[:20]) and str(pics[20]) not in brief
    assert (d / "TASKS.md").read_text().splitlines()[0] == "- [ ] m-00/photo.png" and set(ops.cards) == {"ops-room"}
    props = workers.Systemd(y.state, (y.state / "rooms",)).sandbox(d, {"CLAUDE_CONFIG_DIR": str(d)})
    assert f"BindPaths=-{y.state / 'rooms' / 'example-room' / 'work'}" in props                 # the owner room's pictures
    assert not any(p.startswith("BindPaths=") and "ops-room" in p for p in props)

    def finish(folder: Path, filed: list[Path]) -> None:
        for p in filed:
            pictures.file("example-room", p, "EX-42", "a page", y.memory.tz).chmod(0o664)     # as a worker's Write leaves it
        (folder / "TASKS.md").write_text((folder / "TASKS.md").read_text().replace("- [ ]", "- [x]"))
        (folder / "REPORT.md").write_text(f"{len(filed)} filed\n")
        (folder / "exit.json").write_text(json.dumps({"result": "success", "code": "exited", "status": "0"}))
    finish(d, pics[:20])
    asyncio.run(y.workers.complete(r["id"]))
    assert ops.lines == ["pictures backfill example-room: batch 1 of 2 finished clean, 20 of 20 filed; batch 2 started"]
    assert len(units.calls) == 2 and y.ledger.worker_get(r["id"])["relay"] == "skipped"       # no relay turn in any room
    assert {pictures.sidecar(p).stat().st_mode & 0o777 for p in pics[:20]} == {0o600}
    d2 = units.calls[1]["cwd"]
    assert json.loads((d2 / "worker.json").read_text())["pictures"]["n"] == 2
    finish(d2, pics[20:24])
    asyncio.run(y.workers.complete(d2.name))
    assert ops.lines[1] == "pictures backfill example-room: batch 2 of 2 finished clean, 4 of 5 filed; done"
    assert [p.name for p in pictures.pending(tmp_path / "rooms" / "example-room" / "work" / "inbox")] == ["photo.png"]
