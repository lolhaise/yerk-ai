"""Checks that a picture sent to a room gets a text file beside it in the same turn, NAME.seen.txt, with the words on it and what it shows, so it can
be found later. The room writes it with the `seen` tool (the room's own read is the engine, the header and path are code); a person's room files its
own (person-room-parity); the index takes the sidecar as an inbox text kind in its room's class, and recall and read_memory show its lines behind
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

from yerk_ai import cli, context, mcp_server, memory, pictures, shape, workers, words
from yerk_ai.config import budgets
from fake_channel import ConsoleChannel

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


def test_a_room_files_a_picture_it_was_sent_a_persons_room_too_each_from_its_own_inbox_only(tmp_path):
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
    assert asyncio.run(y.tool_seen("her-room", str(hers), WORDS, "a chat"))["ok"]              # person-room-parity: hers, filed
    assert hers.with_name("photo.seen.txt").read_text().startswith("seen in her-room, message m-2, ")
    assert asyncio.run(y.tool_seen("her-room", str(mine), WORDS, "a chat")) == {               # its own inbox only
        "ok": False, "error": words.SEEN_NOT_HERE.format(name=mine.name)}
    (notes := mine.with_name("notes.txt")).write_text("example\n")
    loose = tmp_path / "rooms" / "example-room" / "work" / "loose.png"
    loose.write_bytes(PNG)
    for p in (hers, notes, loose):                                                          # not a picture sent to this room
        assert asyncio.run(y.tool_seen("example-room", str(p), WORDS, "x")) == {
            "ok": False, "error": words.SEEN_NOT_HERE.format(name=p.name)}
    assert asyncio.run(y.tool_seen("example-room", str(mine), WORDS, " ")) == {"ok": False, "error": words.SEEN_NO_SHOWS}
    assert "mcp__yerk__seen" in y.spec_for("example-room").settings["permissions"]["allow"]


def test_a_person_in_a_photo_the_owner_sends_in_his_own_room_is_the_owner_and_no_other_name_is_guessed(tmp_path):
    """A photo the owner sends in an owner room that shows a person shows the owner unless the picture's own text
    says otherwise, so its `shows` line says "the owner", never "a man"; anyone else is named only by the picture's text.
    One mechanics line where `seen` is, the tool's own description and the backfill brief say the same."""
    text = context.MECHANICS.read_text().strip()
    line = next(x for x in text.splitlines() if "`seen`" in x)
    assert ("`seen` a picture, its `shows` calling a person in it the owner (in a person's room, the sender) unless its own text "
            "says otherwise") in line
    assert shape.offenses(line) == [] and context.chars(text) < budgets()["session_start"]["appended"]["mechanics"]
    y = daemon(tmp_path)
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    said = " ".join(str(tools["seen"].description).split())
    assert ('a person in it "the owner" (in a person\'s room "the sender") unless its own text says otherwise, anyone else named '
            'only by that text, else "a man"') in said
    brief = " ".join(pictures.brief("example-room", [], ZoneInfo("UTC"), 1, 1).split())
    assert ('A person in it is "the owner", since they sent it, unless the picture\'s own text says otherwise; anyone else is named '
            "only by a name the picture's own text gives them" in brief and "Never guess." in brief)


def test_a_picture_read_and_not_filed_bounces_once_in_every_room(tmp_path):
    y = daemon(tmp_path, "example-room", "her-room")
    for name, mid in (("example-room", "m-1"), ("her-room", "m-2")):
        p = str(picture(tmp_path, name, mid))
        room = y.rooms[name]
        room.to_open, room.rp, room.bounced = {p: "photo.png"}, NS(read_paths={p}), False
    own, hers = y.rooms["example-room"], y.rooms["her-room"]
    reason = own.stop_check(active=False)
    assert reason and "photo.png" in reason and "Call seen on " in reason and "give your answer again" in reason
    assert own.stop_check(active=False) is None                              # once per result
    said = hers.stop_check(active=False) or ""
    assert "photo.png" in said and "Call seen on " in said and "owner" not in said             # a person's room too
    assert hers.stop_check(active=False) is None
    own.bounced = False
    pictures.file("example-room", Path(next(iter(own.to_open))), WORDS, "a chat", y.memory.tz)
    assert own.stop_check(active=False) is None                              # filed: nothing to say


def test_a_picture_read_outside_what_was_sent_never_bounces_the_hands_screenshots_included(tmp_path):
    """Cold read, 2026-10-23: an owner room Read eleven of the hands plug-in's desktop screenshots under work/hands-shots/
    in one turn. The rule counts only a picture sent to the room in this result (to_open: the inbox, from the message's
    staged files), so a Read of any other picture, filed or not, never bounces it."""
    y = daemon(tmp_path, "example-room", "her-room")
    work = tmp_path / "rooms" / "example-room" / "work"
    (shots := work / "hands-shots").mkdir(parents=True)
    hands = [shots / f"desktop-20261023-1329{i:02}.png" for i in range(11)]
    for p in [*hands, work / "loose.png", tmp_path / "elsewhere.jpg"]:
        p.write_bytes(PNG)
    older = picture(tmp_path, "example-room", "m-0")                          # an earlier message's picture, read again
    others = [str(p.resolve()) for p in [*hands, work / "loose.png", tmp_path / "elsewhere.jpg", older]]
    own = y.rooms["example-room"]
    own.to_open, own.rp, own.bounced = {}, NS(read_paths=set(others)), False
    assert own.unfiled() == [] and own.stop_check(active=False) is None and not own.bounced
    sent = str(picture(tmp_path, "example-room", "m-1"))                      # with a picture the owner sent in the same result
    own.to_open, own.rp = {sent: "photo.png"}, NS(read_paths={*others, sent})
    assert own.unfiled() == [("photo.png", sent)]
    reason = own.stop_check(active=False)
    assert reason and sent in reason and not any(p in reason for p in others)
    own.bounced = False
    pictures.file("example-room", Path(sent), WORDS, "a chat", y.memory.tz)
    assert own.stop_check(active=False) is None and not any(pictures.sidecar(Path(p)).exists() for p in others)


def test_in_a_persons_room_too_only_a_picture_sent_in_this_result_bounces(tmp_path):
    """person-room-parity cold read: the proof above in a person's room, which now files its pictures: an earlier message's
    picture and a loose one, read again and never filed, never bounce it; the one sent in this result does, once."""
    y = daemon(tmp_path, "example-room", "her-room")
    older = picture(tmp_path, "her-room", "m-0")
    (loose := tmp_path / "rooms" / "her-room" / "work" / "loose.png").write_bytes(PNG)
    others = [str(older), str(loose.resolve())]
    hers = y.rooms["her-room"]
    hers.to_open, hers.rp, hers.bounced = {}, NS(read_paths=set(others)), False
    assert hers.unfiled() == [] and hers.stop_check(active=False) is None and not hers.bounced
    sent = str(picture(tmp_path, "her-room", "m-1"))
    hers.to_open, hers.rp = {sent: "photo.png"}, NS(read_paths={*others, sent})
    reason = hers.stop_check(active=False)
    assert hers.unfiled() == [("photo.png", sent)] and reason and sent in reason and not any(p in reason for p in others)
    assert hers.stop_check(active=False) is None                             # once per result

def test_the_index_takes_a_sidecar_as_an_inbox_text_kind_an_owner_rooms_private_a_persons_class_room_hers(tmp_path):
    (priv := tmp_path / "private").mkdir()
    (priv / "yerkd.toml").write_text('[memory]\nroot = "memory"\n')
    (priv / "rooms.toml").write_text('[rooms.own-room]\nchannel = "console:own"\n\n'
                                     '[rooms.her-room]\nchannel = "console:her"\nprivacy = "family"\npeople = [1001]\n')
    state = tmp_path / "state"
    own, hers = picture(state, "own-room", "m-1"), picture(state, "her-room", "m-2")
    s = memory.settings(priv, state)
    side = pictures.file("own-room", own, "---\nclass: shared\n---\nEX-42", "a page", s.tz)     # front matter on a picture
    (theirs := hers.with_name("photo.seen.txt")).write_text("example of hers\n")
    files = sorted(p.split("/work/inbox/")[1] for p in s.scope.files() if "/work/inbox/" in p)
    assert files == ["m-1/photo.seen.txt", "m-2/photo.seen.txt"] and s.scope.contains(side) and s.scope.contains(theirs)
    assert memory.cite(s, theirs) == "her-room/work/inbox/m-2/photo.seen.txt" and memory.file_class(theirs) == ("room", "console:her")
    assert memory.cite(s, side) == "own-room/work/inbox/m-1/photo.seen.txt"
    assert memory.file_class(side) == ("private", "")          # its header comes first: no front matter can make it shared
    her = memory.Reader(name="her-room", rid="console:her", owner=False)
    assert not memory.read(s, "own-room/work/inbox/m-1/photo.seen.txt", reader=her)["ok"]
    assert memory.read(s, "her-room/work/inbox/m-2/photo.seen.txt", reader=her)["ok"]                # hers, in her room
    assert memory.read(s, str(theirs), reader=memory.Reader(name="own-room", rid="console:own", owner=True))["ok"]   # decision 44


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


def test_a_long_sidecar_line_keeps_its_mark_when_recall_or_deep_recall_cuts_it(tmp_path):
    """Cold read, 2026-10-23: the five real sidecars of the backfill proof each hold a "what it shows:" line of 249 to 410
    characters, over deep recall's 280 with the mark; _cut kept the window around the match and dropped the mark in front."""
    from yerk_ai import memindex
    (priv := tmp_path / "private").mkdir()
    (priv / "yerkd.toml").write_text('[memory]\nroot = "memory"\n')
    (priv / "rooms.toml").write_text('[rooms.own-room]\nchannel = "console:own"\n')
    state = tmp_path / "state"
    s = memory.settings(priv, state)
    shows = "a screenshot of an example sheet, " + "four columns of example figures, " * 8 + "the example label exquux at the end"
    side = pictures.file("own-room", picture(state, "own-room", "m-1"), WORDS, shows, s.tz)
    memindex.Index(s.index_db, s.scope).sync()
    deep = memory.lines_on(s, "exquux")
    assert [x["cite"] for x in deep] == ["own-room/work/inbox/m-1/photo.seen.txt:5"] and "exquux" in deep[0]["text"]
    assert deep[0]["text"].startswith(f"{memory.SEEN_MARK} …") and len(deep[0]["text"]) <= memory.ITEM_CHARS
    hit = {"cite": "own-room/work/inbox/m-1/photo.seen.txt:1", "path": str(side), "line": 1, "text": side.read_text()}
    pad = [{"cite": f"x{i}.md:1", "path": f"/x/x{i}.md", "line": 1, "text": "exquux example"} for i in range(memory.RECALL_HITS)]
    for got in (memory.snippets(pad + [hit], {"exquux": 1.0}, 4500)[-1]["text"], memory.snippets([hit], {"exquux": 1.0}, 300)[0]["text"]):
        assert got and all(x.startswith(memory.SEEN_MARK) for x in got.splitlines() if x.strip() and x != "…"), got


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


def root_pictures(root: Path) -> list[Path]:
    """Three pictures under a folder (one in a subfolder), oldest first, beside a dot folder's picture, a film and a link."""
    (root / "sub").mkdir()
    (root / ".kept").mkdir()
    pics = []
    for i, rel in enumerate(("front.jpg", "flex.PNG", "sub/side.jpeg")):
        (p := root / rel).write_bytes(PNG)
        os.utime(p, (1_760_000_000.0 + i, 1_760_000_000.0 + i))
        pics.append(p.resolve())
    (root / ".kept" / "old.png").write_bytes(PNG)
    (root / "clip.mov").write_bytes(b"\0")
    (root / "link.png").symlink_to(pics[0])
    return pics


def test_backfill_over_a_folder_the_room_lists_is_cited_and_indexed_under_it_and_any_other_folder_is_refused(tmp_path, tmp_path_factory, capsys):
    """The owner, 2026-10-23: `yerk pictures backfill ROOM --root FOLDER` over a folder the room lists in rooms.toml (`read` or
    `also`; the case: a room's `also` carrying an older room's photos). --dry-run lists what it
    would file; a folder it does not list, one inside a listed one, or one of Yerk's own is refused; a sidecar there is cited
    under that root, indexed with its class (a file with no front matter there is private) and read behind the mark."""
    root, seen_by_read = tmp_path_factory.mktemp("gym"), tmp_path_factory.mktemp("shots")
    pics = root_pictures(root)
    (seen_by_read / "d.png").write_bytes(PNG)
    assert pictures.pending(root, root=True) == pics                       # no dot folder, film or link; oldest first
    (priv := tmp_path / "private").mkdir()
    state = tmp_path / "state"
    (state / "rooms").mkdir(parents=True)
    (priv / "rooms.toml").write_text(f'[rooms.own-room]\nchannel = "console:own"\nalso = ["{root}"]\n'
                                     f'read = ["{seen_by_read}", "{state / "rooms"}"]\n')
    (priv / "yerkd.toml").write_text(f'[memory]\nroot = "memory"\nalso = ["{root}"]\n')
    base = ["--private", str(priv), "--state", str(state)]
    assert cli.main(["pictures", "backfill", "own-room", "--root", str(root), "--dry-run", *base]) == 0
    assert capsys.readouterr().out.splitlines() == [str(pictures.sidecar(p)) for p in pics] + [
        f"3 picture(s) in {root} with no sidecar, 1 batch(es) of 20; nothing started (--dry-run)"]
    assert cli.main(["pictures", "backfill", "own-room", "--root", str(seen_by_read), "--dry-run", *base]) == 0
    assert capsys.readouterr().out.splitlines()[0] == str(seen_by_read / "d.seen.txt")
    for bad in (tmp_path_factory.mktemp("unlisted"), root / "sub", state / "rooms"):
        assert cli.main(["pictures", "backfill", "own-room", "--root", str(bad), "--dry-run", *base]) == 2
        assert capsys.readouterr().err.strip() == words.PICTURES_NOT_LISTED.format(room="own-room", folder=bad)
    assert not any(pictures.sidecar(p).exists() for p in pics)
    s = memory.settings(priv, state)
    side = pictures.file("own-room", pics[0], "EX-42 TOTAL", "the owner in front of an example mirror", s.tz)
    assert side.read_text().splitlines()[0] == f"seen in own-room, folder {root}, {pictures.header('own-room', pics[0], s.tz).split(', ', 2)[2]}"
    from yerk_ai import memindex
    memindex.Index(s.index_db, s.scope).sync()
    cite = memory.cite(s, side)
    assert cite == f"{root.name}/front.seen.txt" and str(side) in s.scope.files() and memory.file_class(side) == ("private", "")
    assert all(x.startswith(memory.SEEN_MARK) for x in memory.read(s, cite)["text"].splitlines())
    hit = next(h for h in memory.recall(s, "example mirror", 6)["hits"] if h["cite"].startswith(cite))
    assert all(x.startswith(memory.SEEN_MARK) for x in hit["text"].splitlines() if x.strip())
    assert not memory.read(s, cite, reader=memory.Reader(name="her-room", rid="her-room", owner=False))["ok"]
    (root / "notes.seen.txt").write_text("a note that is no sidecar\n")
    assert memory.is_seen(side) and not memory.is_seen(root / "notes.seen.txt")


def test_backfill_over_a_listed_root_runs_the_same_worker_path_with_no_room_folder_bound(tmp_path, tmp_path_factory):
    y = daemon(tmp_path, "example-room", "her-room")
    y.room_confs["ops-room"] = {**y.room_confs["example-room"], "role": "machine", "id": "ops-room-id"}
    y.daily.rooms["ops-room"] = ("ops-room-id", "private")
    ops = Rec()
    y.add_room("ops-room", ops)
    y.workers.launcher = units = Units()
    root = tmp_path_factory.mktemp("gym")
    pics = root_pictures(root)
    y.room_confs["example-room"]["also"] = [str(root)]
    other = str(tmp_path_factory.mktemp("unlisted"))
    assert asyncio.run(y.control({"pictures": {"room": "example-room", "root": other}})) == {
        "ok": False, "error": words.PICTURES_NOT_LISTED.format(room="example-room", folder=other)}
    r = asyncio.run(y.control({"pictures": {"room": "example-room", "root": str(root)}}))
    assert r["ok"] and r["room"] == "ops-room" and (r["pictures"], r["batches"]) == (3, 1) and len(units.calls) == 1
    d = Path(r["staging"])
    spec = json.loads((d / "worker.json").read_text())
    assert spec["pictures"] == {"room": "example-room", "n": 1, "of": 1, "rest": [[str(p) for p in pics]], "root": str(root)}
    assert "bind" not in spec and spec["account"] == y.rooms["example-room"]._account().name
    props = workers.Systemd(y.state, (y.state / "rooms",)).sandbox(d, {"CLAUDE_CONFIG_DIR": str(d)})
    assert not any(p.startswith("BindPaths=") and "example-room" in p for p in props)        # no room folder of the owner's
    brief = (d / "BRIEF.md").read_text()
    assert f"3 pictures in {root}, a folder the room reads" in brief and all(
        f"Read {p}\n   Write {pictures.sidecar(p)}\n   its first line exactly: seen in example-room, folder {p.parent}, " in brief for p in pics)
    for p in pics:
        pictures.file("example-room", p, "EX-42", "a page", y.memory.tz).chmod(0o664)          # as a worker's Write leaves it
    (d / "TASKS.md").write_text((d / "TASKS.md").read_text().replace("- [ ]", "- [x]"))
    (d / "REPORT.md").write_text("3 of 3 filed\n")
    (d / "exit.json").write_text(json.dumps({"result": "success", "code": "exited", "status": "0"}))
    asyncio.run(y.workers.complete(r["id"]))
    assert ops.lines == ["pictures backfill example-room: batch 1 of 1 finished clean, 3 of 3 filed; done"]
    assert {pictures.sidecar(p).stat().st_mode & 0o777 for p in pics} == {0o600}
    assert asyncio.run(y.control({"pictures": {"room": "example-room", "root": str(root)}})) == {
        "ok": True, "room": "example-room", "pictures": 0, "line": words.PICTURES_NONE.format(room="example-room", where=root)}
