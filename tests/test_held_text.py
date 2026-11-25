"""Checks that an outside action held for your yes, such as a post or a text, shows you its exact text
before your yes is asked for: inline when it fits one message, as a file right after the line when it does
not, and a code fence inside the text cannot close the block it is shown in; a room with people is shown
never its JSON. Nothing here runs a command or posts anywhere."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from test_gates import Rec
from yerk_ai import gates, texts, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import SessionTable
from yerk_ai.texting import holds


class RecFiles(Rec):
    def __init__(self) -> None:
        super().__init__()
        self.files: list[tuple[str, str]] = []

    async def send_file(self, room, path, caption="", key=None, inbound=""):
        self.files.append((Path(path).read_text(), caption))
        return ["f1"]


def held(tmp_path: Path, command: str) -> tuple[RecFiles, dict]:
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.gates.trash = ("trash",)
    ch = RecFiles()
    y.add_room("example-room", ch)
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-g", "one")

    async def go():
        out = await y.hook({"session_id": "sid-g", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                            "tool_input": {"command": command}})
        await asyncio.sleep(0.1)                      # the line and any file go out beside the answer
        return out
    return ch, asyncio.run(go())


def test_a_short_held_post_shows_its_exact_text_in_the_line(tmp_path) -> None:
    cmd = "gh issue comment 12 --body 'Thanks, that fixed it for me too.'"
    ch, out = held(tmp_path, cmd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    exact = gates.call_key("Bash", {"command": cmd})
    assert ch.notes == [f"{words.WORKING} held: a public post\nsay approve here to let it run once\n```\n{exact}\n```"]
    assert ch.files == []


def test_a_long_held_post_says_so_and_sends_the_exact_text_as_a_file(tmp_path) -> None:
    cmd = "gh issue comment 12 --body '" + "word " * 400 + "'"
    ch, _ = held(tmp_path, cmd)
    exact = gates.call_key("Bash", {"command": cmd})
    assert len(exact) > gates.SHOW
    assert ch.notes == [f"{words.WORKING} held: a public post\nsay approve here to let it run once\n"
                        f"{words.HELD_ATTACHED}"]
    assert ch.files == [(exact + "\n", words.HELD_FILE)]


def test_a_fence_inside_the_text_cannot_close_the_block() -> None:
    _, line = gates.held("a public post", "x", "approve", exact="before ``` after")
    body = line.split("\n```\n", 1)[1]
    assert body.endswith("\n```") and "```" not in body[:-4]


def hold(state: Path, ti: dict, people: bool = False) -> tuple[Yerkd, RecFiles]:
    """A send from example-room to another room, held; `people`: the room lists a person (a placeholder id)."""
    state.mkdir()
    y = Yerkd(EXAMPLE_PRIVATE, state)
    if people:
        y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    ch = RecFiles()
    y.add_room("example-room", ch)
    SessionTable(state / "sessions.json").bind("example-room", "sid-g", "one")

    async def go() -> None:
        await y.hook({"session_id": "sid-g", "hook_event_name": "PreToolUse", "tool_name": "mcp__yerk__send", "tool_input": ti})
        await asyncio.sleep(0.1)
    asyncio.run(go())
    return y, ch


def test_a_held_send_in_a_room_with_people_shows_its_words_and_the_owners_room_still_sees_the_call(tmp_path) -> None:
    """Seen live: a person's room was shown the raw `mcp__yerk__send {...}` call on a held card. A room with
    people is shown the call's content in plain words, the room it goes to in the first line and the text below, inline
    or, when long, as the file; the exact call stays in the ledger as the receipt. The owner's room still sees the exact
    call the word releases."""
    send = {"room": "example-quiet", "text": "They asked to hear once the deals are signed.\n\nWant a check after the trip?"}
    head = f"{words.WORKING} held: a send to another room (example-quiet)\nsay approve here to let it run once\n"
    y, ch = hold(tmp_path / "people", send, people=True)
    assert ch.notes == [head + f"```\n{send['text']}\n```"] and ch.files == []
    assert "mcp__yerk__send" not in ch.notes[0] and "{" not in ch.notes[0]
    row, = y.ledger.rows(5, "approvals")
    assert row["call"] == gates.call_key("mcp__yerk__send", send)                   # the exact call: the receipt
    long = {"room": "example-quiet", "text": "word " * 400}
    _, ch = hold(tmp_path / "long", long, people=True)
    assert ch.notes == [head + words.HELD_ATTACHED] and ch.files == [(long["text"] + "\n", words.HELD_FILE)]
    _, ch = hold(tmp_path / "owner", send)                                          # the owner's room: as before
    assert ch.notes == [head + f"```\n{gates.call_key('mcp__yerk__send', send)}\n```"]
    assert gates.plain("Bash", {"command": "gh  issue comment 1 --body 'x'"}) == gates.call_key("Bash", {"command": "gh issue comment 1 --body 'x'"})
    assert gates.plain("mcp__mail__send", {"to": "the example list", "file": "/srv/x/notes.pdf", "body": ""}) == \
        "to: the example list\nfile: notes.pdf"


def test_a_text_card_shows_each_text_exactly_and_its_hold_keys_exactly_those_texts() -> None:
    """texting-5: a held text's card shows each text of the draft as it is, one a line, and its hold is keyed by exactly those
    texts and that handle, so the owner's word on the card releases the texts it showed and no other."""
    row = {"decision": "reply", "handle": "+15550000101", "parts": json.dumps(["see you ``` there", "  two spaces"])}
    assert holds.card("Alex", "09:17", [], row, False).split("\n")[2:4] == ["see you ``` there", "  two spaces"]
    assert holds.key(row) == gates.call_key(texts.TOOL, {"to": "+15550000101", "parts": ["see you ``` there", "  two spaces"]})
    assert holds.key({**row, "parts": json.dumps(["see you ``` there", " two spaces"])}) != holds.key(row)
