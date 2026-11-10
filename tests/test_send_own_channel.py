"""Checks that a room's own channel id is the room itself, so a `send` there is never held as a send to another room. The case behind it
(send-own-channel): a room asked to post a picture in its own chat called `send` with its channel id, the approval gate held it
as a send to another room, and the send would have answered that no room has that name. The gate's "other" and the send tool read one resolver,
room's name, its channel as rooms.toml writes it (`discord:<id>`), or the id alone. Example ids; nothing posts anywhere."""

from __future__ import annotations

import asyncio

from test_gates import Rec, call
from yerk_ai import gates, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import SessionTable
from yerk_ai.room import Inbound

OWN, QUIET = "1001", "2002"        # example channel ids, one per example room (a real one is 17 to 20 digits)


class Sent(Rec):
    """A room's notes (Rec) and the texts the send tool posted to it."""
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def send_text(self, room, text, key=None, inbound=""):
        self.sent.append(text)
        return ["s1"]


def rooms(tmp_path):
    """example-room (the caller, its session bound) and example-quiet, each on its own example channel."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.room_confs["example-room"]["channel"] = f"discord:{OWN}"
    y.room_confs["example-quiet"]["channel"] = f"discord:{QUIET}"
    own, quiet = Sent(), Sent()
    room = y.add_room("example-room", own)
    y.add_room("example-quiet", quiet)
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-g", "one")
    return y, room, own, quiet


def send(room, text="here it is"):
    return {"hook_event_name": "PreToolUse", "tool_name": "mcp__yerk__send", "tool_input": {"room": room, "text": text}}


def test_a_send_to_its_own_room_by_name_is_not_held_and_posts_there(tmp_path):
    y, _, own, quiet = rooms(tmp_path)
    assert call(y, send("example-room")) == {}
    assert asyncio.run(y.tool_send("example-room", "here it is", "", "example-room"))["ok"]
    assert own.sent == ["here it is"] and quiet.sent == [] and own.notes == [] and y.ledger.rows(10, "gates") == []


def test_its_own_channel_id_with_or_without_discord_is_the_room_itself(tmp_path):
    """The live call: its own id, bare or as rooms.toml writes it, is no other room (no hold, no word asked for) and the
    send posts in the room, where the gate held it and the send answered `no room` by that id."""
    y, _, own, quiet = rooms(tmp_path)
    for rid in (OWN, f"discord:{OWN}"):
        assert gates.room_named(rid, y.room_confs) == "example-room"
        assert call(y, send(rid)) == {}, rid
        assert asyncio.run(y.tool_send(rid, "here it is", "", "example-room"))["ok"], rid
    assert own.sent == ["here it is"] * 2 and quiet.sent == [] and own.notes == [] and y.ledger.rows(10, "gates") == []


def test_another_rooms_channel_id_is_held_as_another_room_and_after_the_word_posts_there(tmp_path):
    y, room, own, quiet = rooms(tmp_path)
    note = send(QUIET, "a note for the quiet room")
    out = call(y, note)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"].startswith(
        "Held: a send to another room (example-quiet)")                # named as the room it resolves to, not its id
    assert own.notes[0].startswith(f"{words.WORKING} held: a send to another room (example-quiet)\n")
    room.busy, room.inflight = True, [Inbound("m2", "owner", "approve")]
    call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "approve"})
    assert call(y, note) == {}                                         # released, once: the same call
    room.busy = False
    assert asyncio.run(y.tool_send(QUIET, "a note for the quiet room", "", "example-room"))["ok"]
    assert quiet.sent == ["a note for the quiet room"] and own.sent == []
    for other in (QUIET, f"discord:{QUIET}", "example-quiet"):        # by its id, its channel or its name: held again
        assert call(y, send(other))["hookSpecificOutput"]["permissionDecision"] == "deny", other


def test_a_room_nobody_has_is_refused_with_the_rooms_names(tmp_path):
    y, _, own, quiet = rooms(tmp_path)
    for bad in ("3003", "discord:3003", "no-such-room", ""):
        assert gates.room_named(bad, y.room_confs) is None, bad
        assert asyncio.run(y.tool_send(bad, "x", "", "example-room")) == {
            "ok": False, "error": f"no room {bad!r}; rooms: example-room, example-quiet"}, bad
    assert call(y, send("no-such-room"))["hookSpecificOutput"]["permissionDecision"] == "deny"     # the gate fails closed
    assert own.sent == quiet.sent == []


def test_a_person_room_still_posts_to_itself_only_its_channel_id_included(tmp_path):
    y, _, own, quiet = rooms(tmp_path)
    y.room_confs["example-quiet"] = {**y.room_confs["example-quiet"], "privacy": "family"}    # not one of the owner's rooms
    assert asyncio.run(y.tool_send(f"discord:{QUIET}", "hi", "", "example-quiet"))["ok"] and quiet.sent == ["hi"]
    for other in ("example-room", OWN, f"discord:{OWN}"):
        assert asyncio.run(y.tool_send(other, "hi", "", "example-quiet")) == {
            "ok": False, "error": "room example-quiet posts to itself only; it is not one of the owner's rooms"}, other
    assert own.sent == []
