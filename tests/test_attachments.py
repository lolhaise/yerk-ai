"""Checks the attachments tool, which saves the files an earlier Discord message carries into the calling room's inbox again,
through yerkd's Discord bot, so a room asked to post a file again gets it back after the nightly sweep took its outbox copy.
Before the tool (need 69 of 2026-11-06), the room's history listing gave the file's name and no address, its shell had no
network, and the copy another program kept on the box was outside what a room may read. The message is named by its id (the
room's own channel, then the one it watches), by its link (an owner's room anywhere in its own server), or with a file's name
the room posted that file in, from the ledger. Every id, name and file here is a placeholder."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from yerk_ai import gates, mcp_server, shape, watch, words
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.daemon import YERK_TOOLS, Yerkd
from fake_channel import ConsoleChannel
from test_discord import Attachment
from test_watch_video import ELSEWHERE, FAR, NEAR, OWN, SERVER, Gateway, Shelf, link

EXAMPLE_PRIVATE = Path(__file__).resolve().parents[1] / "examples" / "private"
TOOL = "mcp__yerk__attachments"
NOTES = b"line one of a placeholder prompt\nline two\n"


def on_discord(tmp_path: Path) -> tuple[Yerkd, Path, dict[int, Shelf]]:
    """The example room on the Discord adapter, its own channel OWN in SERVER beside NEAR, and FAR in another server."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    shelves = {OWN: Shelf(OWN, SERVER), NEAR: Shelf(NEAR, SERVER), FAR: Shelf(FAR, ELSEWHERE)}
    d = DiscordChannel("token", 7, [], y.ledger, client=Gateway(*shelves.values()), state=tmp_path)
    d.add_room(OWN, y.add_room("example-room", d))
    return y, tmp_path / "rooms" / "example-room" / "work", shelves


def ask(y: Yerkd, message: str = "", name: str = "") -> dict[str, Any]:
    return asyncio.run(y.tool_attachments("example-room", message, name))


def posted(y: Yerkd, mid: int, name: str, room: str = "example-room", channel: int = OWN, error: str | None = None) -> None:
    """The ledger's row for a file a room posted (send_file's): its channel, its message id and the file's name."""
    y.ledger.outbound(room=room, channel=str(channel), kind="file", message_id=None if error else str(mid), chars=0, file=name, error=error)


def test_every_room_is_handed_the_attachments_tool_where_the_room_of_the_need_had_none(tmp_path):
    """The wall: the history listing named the file and gave no address, and nothing a room holds fetched it."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    said = " ".join(str(tools["attachments"].description).split())       # as the model reads it, lines joined
    assert list(tools["attachments"].inputSchema["properties"]) == ["message", "name"] and TOOL in YERK_TOOLS
    assert all(x in said for x in ("saved again to this room's inbox", "since your shell has no network", "the sweep took it",
                                   "the [m:] of its day-log line", "discord.com/channels/SERVER/CHANNEL/MESSAGE",
                                   "the newest file this room posted under it", "`send` it again"))
    assert TOOL in y.spec_for("example-room").settings["permissions"]["allow"]
    assert gates.external_hit(TOOL, {"message": "501"}, "example-room", y.gates) is None    # a read into its own inbox: no gate


def test_a_file_this_room_posted_comes_back_from_its_message_once_its_copy_is_gone(tmp_path):
    """The ask: post again a file the room posted days ago, its outbox copy swept and the trash emptied. Its message's id
    brings the file back into the room's inbox, byte for byte, a ledger row for it, and the room may send it from there."""
    y, work, shelves = on_discord(tmp_path)
    shelves[OWN].msgs[501] = NS(id=501, attachments=[Attachment("prompt_v1.txt", NOTES, "text/plain")])
    posted(y, 501, "prompt_v1.txt")
    assert not (tmp_path / "rooms" / "example-room" / "outbox").exists()                 # the copy is gone
    r = ask(y, "501")
    saved = work / "inbox" / "501" / "prompt_v1.txt"
    assert r == {"ok": True, "message": "501", "note": "", "next": words.ATTACH_NEXT,
                 "files": [{"name": "prompt_v1.txt", "path": str(saved), "size": len(NOTES), "type": "text/plain"}]}
    assert saved.read_bytes() == NOTES and shelves[OWN].asked == [501] and shelves[NEAR].asked == []
    rows = y.ledger._select("SELECT name, state, room FROM attachments WHERE message_id = ?", ("501",))
    assert rows == [{"name": "prompt_v1.txt", "state": "staged", "room": "example-room"}]
    assert y.may_send("example-room", saved)                                             # `send` takes it from there
    assert ask(y, link(OWN, 501, server=ELSEWHERE)) == r                                 # its link, the server never read
    assert saved.read_bytes() == NOTES and sorted(p.name for p in saved.parent.iterdir()) == ["prompt_v1.txt"]


def test_a_files_name_alone_is_the_newest_message_this_room_posted_it_in(tmp_path):
    y, work, shelves = on_discord(tmp_path)
    shelves[OWN].msgs[601] = NS(id=601, attachments=[Attachment("plan.txt", b"old plan")])
    shelves[OWN].msgs[602] = NS(id=602, attachments=[Attachment("Plan.txt", b"new plan"), Attachment("other.csv", b"a,b")])
    posted(y, 601, "plan.txt")
    posted(y, 602, "Plan.txt")
    posted(y, 0, "plan.txt", error="it is 25 MB and this room takes 20 MB")              # a post that failed: no message
    posted(y, 603, "plan.txt", room="another-room")                                      # another room's post
    r = ask(y, name="PLAN.TXT")                                                          # any case
    assert r["ok"] and r["message"] == "602" and [f["name"] for f in r["files"]] == ["Plan.txt"]
    assert (work / "inbox" / "602" / "Plan.txt").read_bytes() == b"new plan" and not (work / "inbox" / "602" / "other.csv").exists()
    assert shelves[OWN].asked == [602]
    assert ask(y, name="never.txt") == {"ok": False, "error": words.ATTACH_NOT_POSTED.format(name="never.txt")}
    assert shelves[OWN].asked == [602]                                                   # nothing read for it


def test_an_id_alone_is_looked_for_in_the_rooms_own_channel_then_the_one_it_watches(tmp_path):
    y, work, shelves = on_discord(tmp_path)
    y.channels["example-room"].add_room(OWN, y.rooms["example-room"], NEAR)
    shelves[NEAR].msgs[701] = NS(id=701, attachments=[Attachment("sheet.csv", b"a,b\n1,2\n", "text/csv")])
    r = ask(y, "701")
    assert r["ok"] and r["files"][0]["path"] == str(work / "inbox" / "701" / "sheet.csv")
    assert shelves[OWN].asked == [701] and shelves[NEAR].asked == [701]                  # its own channel first
    assert ask(y, "702") == {"ok": False, "error": words.ATTACH_NOT_FOUND}
    assert ask(y, link(OWN, 702)) == {"ok": False, "error": words.WATCH_NO_MESSAGE}       # a link reads its one channel
    assert shelves[OWN].asked == [701, 702, 702] and shelves[NEAR].asked == [701, 702] and shelves[FAR].asked == []


def test_a_message_is_read_only_where_the_room_reads_and_an_owners_room_anywhere_in_its_own_server(tmp_path):
    y, work, shelves = on_discord(tmp_path)
    for shelf in shelves.values():
        shelf.msgs[7] = NS(id=7, attachments=[Attachment("notes.txt", b"x", "text/plain")])
    refused = {"ok": False, "error": words.ATTACH_NOT_ITS_CHANNEL}
    assert ask(y, link(OWN, 7))["ok"] and ask(y, link(NEAR, 7))["ok"]                    # an owner's room: its server
    assert ask(y, link(FAR, 7, server=ELSEWHERE)) == refused and shelves[FAR].asked == []  # never another server's
    y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    assert ask(y, link(NEAR, 7)) == refused and shelves[NEAR].asked == [7]               # a person's: its own channel
    assert ask(y, link(OWN, 7))["ok"] and ask(y, "7")["ok"] and shelves[NEAR].asked == [7]
    assert sorted(p.name for p in (work / "inbox").iterdir()) == ["7"]


def test_a_name_with_a_message_keeps_that_file_and_one_too_big_or_cut_off_is_named(tmp_path):
    y, work, shelves = on_discord(tmp_path)
    huge = Attachment("huge.zip", b"x", "application/zip")
    huge.size = watch.MOST_BYTES + 1
    shelves[OWN].msgs[801] = NS(id=801, attachments=[Attachment("a.txt", b"a"), Attachment("b.txt", b"b"), huge,
                                                     Attachment("cut.pdf", b"x", "application/pdf", fail=True)])
    r = ask(y, "801", "b.txt")
    assert r["ok"] and [f["name"] for f in r["files"]] == ["b.txt"] and sorted(p.name for p in (work / "inbox" / "801").iterdir()) == ["b.txt"]
    assert ask(y, "801", "c.txt") == {"ok": False, "error": words.ATTACH_NO_NAME.format(name="c.txt", files="a.txt, b.txt, huge.zip, cut.pdf")}
    r = ask(y, "801")
    assert r["ok"] and [f["name"] for f in r["files"]] == ["a.txt", "b.txt"]
    assert r["note"] == "; ".join((words.ATTACH_NOT_SAVED.format(name="cut.pdf", why="OSError: cdn said no"),
                                   words.ATTACH_TOO_BIG.format(names="huge.zip")))
    assert ask(y, "801", "huge.zip") == {"ok": False, "error": words.ATTACH_TOO_BIG.format(names="huge.zip")}
    assert not (work / "inbox" / "801" / "huge.zip").exists()


def test_each_miss_is_said_in_words_and_nothing_is_saved(tmp_path):
    y, work, shelves = on_discord(tmp_path)
    shelves[OWN].msgs[901] = NS(id=901, attachments=[])
    assert ask(y) == {"ok": False, "error": words.ATTACH_NOTHING}
    for given in ("not an id", "https://example.org/a/b", "12ab", "1" * 21):
        assert ask(y, given) == {"ok": False, "error": words.ATTACH_NOT_MESSAGE.format(given=given)}, given
    assert ask(y, "901") == {"ok": False, "error": words.ATTACH_NO_FILES}
    shelves[NEAR].shut = True
    assert ask(y, link(NEAR, 1)) == {"ok": False, "error": words.WATCH_NOT_READABLE}
    assert asyncio.run(y.tool_attachments("no-such-room", "901", "")) == {"ok": False, "error": words.ATTACH_NO_ROOM.format(room="no-such-room")}
    assert not (work / "inbox").exists()
    y.add_room("example-room", ConsoleChannel())
    assert ask(y, "901") == {"ok": False, "error": words.WATCH_NO_DISCORD}


@pytest.mark.parametrize("said", [words.ATTACH_NO_ROOM, words.ATTACH_NOTHING, words.ATTACH_NOT_MESSAGE, words.ATTACH_NOT_POSTED,
                                  words.ATTACH_NOT_FOUND, words.ATTACH_NOT_ITS_CHANNEL, words.ATTACH_NO_FILES, words.ATTACH_NO_NAME,
                                  words.ATTACH_TOO_BIG, words.ATTACH_NOT_SAVED, words.ATTACH_NEXT])
def test_the_words_are_in_the_prompting_guides_shape_with_no_path_and_no_em_dash(said):
    assert shape.offenses(said) == [] and words.no_paths(said) == said and "—" not in said
