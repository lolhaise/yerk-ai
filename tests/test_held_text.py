"""Checks that an outside action held for your yes, such as a post or a text, shows you its exact text
before your yes is asked for: inline when it fits one message, as a file right after the line when it does
cannot close the block it is shown in. Nothing here runs a command or posts anywhere."""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_gates import Rec
from yerk_ai import gates, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import SessionTable


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
