"""Checks the `delete` tool (deletes.py), which removes files outside a room's work folder for good, once you have seen each path and said
yes. Before it (need 23 of 2026-10-25), a room the owner asked to delete old junk outside its work folder to free the disk had its `rm`
refused by the trash gate, found the rest of the disk read-only in its sandbox, and wrote the lines for the owner to paste and run. A delete
the owner orders outside a room's work folder is made by the daemon, held first with each path shown as what it is, then run once on the
owner's word, for good, and never from a person's room, from a worker, past a rule turned off or a hook that failed open, nor on the root, a
top folder, the home folder, a mounted disk or Yerk AI's own folders. Every file deleted here is one this test made under its own tmp_path."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from test_gates import Rec, call
from yerk_ai import deletes, gates, mcp_server, words
from yerk_ai.config import EXAMPLE_PRIVATE, REPO
from yerk_ai.daemon import YERK_TOOLS, Yerkd
from yerk_ai.engine import SessionTable
from yerk_ai.room import Inbound


def junk(tmp_path: Path) -> dict[str, Path]:
    """Old junk outside every room's folders: a crash dump, a backup folder with a folder inside, and a link to a file
    that must outlive the link's delete."""
    (j := tmp_path / "junk").mkdir()
    (j / "core.4242").write_bytes(b"x" * 300_000)
    (b := j / "old-backup" / "inner").mkdir(parents=True)
    (b / "a.tar").write_bytes(b"y" * 200_000)
    (j / "old-backup" / "b.tar").write_bytes(b"z" * 100_000)
    (kept := tmp_path / "kept.txt").write_text("still here")
    (j / "to-kept").symlink_to(kept)
    return {"dump": j / "core.4242", "backup": j / "old-backup", "link": j / "to-kept", "kept": kept, "junk": j}


def setup(tmp_path: Path, people: bool = False, gate: dict | None = None) -> tuple[Yerkd, object, Rec, dict[str, Path]]:
    (tmp_path / "state").mkdir()
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path / "state")
    if gate is not None:
        y.gates = gates.settings(gate)
    y.gates.trash = ("trash",)
    if people:
        y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    ch = Rec()
    room = y.add_room("example-room", ch)
    SessionTable(tmp_path / "state" / "sessions.json").bind("example-room", "sid-g", "one")
    return y, room, ch, junk(tmp_path)


def pre(y: Yerkd, ti: dict, tool: str = deletes.TOOL) -> dict:
    return call(y, {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti})


def denied(out: dict) -> str:
    hso = out.get("hookSpecificOutput", {})
    return str(hso.get("permissionDecisionReason")) if hso.get("permissionDecision") == "deny" else ""


def delete(y: Yerkd, paths: list[str], room: str = "example-room") -> dict:
    return asyncio.run(y.tool_delete(room, paths))


def word(y: Yerkd, room) -> dict:
    room.busy, room.inflight = True, [Inbound(f"m{len(y.worded)}", "owner", "approve")]   # the owner's word, in the turn
    return call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "approve"})


def test_every_owner_room_is_handed_the_delete_tool_where_the_room_of_the_need_had_none(tmp_path):
    y, room, ch, j = setup(tmp_path)
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert list(tools["delete"].inputSchema["properties"]) == ["paths"] and deletes.TOOL in YERK_TOOLS
    assert "for good, not to the trash" in str(tools["delete"].description) and "call it again the same" in str(tools["delete"].description)
    assert deletes.TOOL in y.spec_for("example-room").settings["permissions"]["allow"]
    assert gates.external_hit(deletes.TOOL, {"paths": [str(j["dump"])]}, "example-room", y.gates)[:2] == ("delete-outside", "a delete for good")


def test_the_wall_the_shell_still_refuses_a_delete_outside_and_an_owner_room_is_told_the_way(tmp_path):
    """The need's own call, `rm -f` of a crash dump outside the work folder: still refused (the room's sandbox could not run it
    anyway), and the refusal now names the tool in an owner room, never in a person's room or a worker, which have none."""
    y, room, ch, j = setup(tmp_path)
    work = tmp_path / "state" / "rooms" / "example-room" / "work"
    rm = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(work), "tool_input": {"command": f"rm -f {j['dump']}"}}
    why = denied(call(y, rm))
    assert "outside the work folder" in why and why.endswith(words.DELETE_WAY) and "`delete`" in why and j["dump"].exists()
    y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    assert "outside the work folder" in (why := denied(call(y, rm))) and "`delete`" not in why
    spec = {"room": "example-room", "gate": {"trash": ["trash"], "external": gates.EXTERNAL}}
    out = gates.worker_check({"tool_name": "Bash", "cwd": str(work), "tool_input": rm["tool_input"]}, work, spec)
    assert "outside the work folder" in (why := denied(out)) and "`delete`" not in why and j["dump"].exists()


def test_a_delete_is_held_with_its_paths_shown_and_the_owners_word_runs_it_once_for_good(tmp_path):
    y, room, ch, j = setup(tmp_path)
    paths = [str(j["dump"]), str(j["backup"]), str(j["link"])]
    took = sum(deletes.on_disk(Path(p)) for p in paths)
    assert denied(pre(y, {"paths": paths})).startswith("Held: a delete for good")
    size = words.size(deletes.on_disk(j["dump"]))
    assert ch.notes == [f"{words.WORKING} held: a delete for good\nsay approve here to let it run once\n```\n"
                        f"{j['dump']} (a file, {size})\n{j['backup']} (a folder)\n{j['link']} (a link)\n```"]   # each path whole, what it is
    assert all(words.width(x) <= words.PHONE for x in ch.notes[0].split("```")[0].splitlines())
    assert delete(y, paths) == {"ok": False, "error": words.DELETE_NOT_RELEASED} and all(os.path.lexists(p) for p in paths)
    assert "[approval] owner said approve" in word(y, room)["hookSpecificOutput"]["additionalContext"]
    assert pre(y, {"paths": paths}) == {}                                                 # the same call, released
    assert delete(y, paths) == {"ok": True, "deleted": paths, "freed": words.size(took)}
    assert not any(os.path.lexists(p) for p in paths) and j["kept"].read_text() == "still here"   # the link went, never its file
    j["dump"].write_bytes(b"x"), j["backup"].mkdir(), j["link"].symlink_to(j["kept"])     # the same paths, made again
    assert delete(y, paths)["error"] == words.DELETE_NOT_RELEASED and all(os.path.lexists(p) for p in paths)   # once
    assert denied(pre(y, {"paths": paths})).startswith("Held: a delete for good")         # the next one is held again
    assert [(r["gate"], r["decision"]) for r in reversed(y.ledger.rows(10, "gates"))] == [
        ("approval", "hold"), ("approval", "approved"), ("approval", "release"), ("delete", "deleted"), ("approval", "hold")]
    assert y.ledger.rows(10, "gates")[1]["detail"].startswith(f"3 of 3, {words.size(took)} freed: {j['dump']}")   # the receipt


def test_an_edit_is_a_new_hold_and_the_word_runs_only_the_newest(tmp_path):
    y, room, ch, j = setup(tmp_path)
    one, both = [str(j["dump"])], [str(j["dump"]), str(j["backup"])]
    assert denied(pre(y, {"paths": both})) and denied(pre(y, {"paths": one}))
    word(y, room)
    assert denied(pre(y, {"paths": both})) and pre(y, {"paths": one}) == {}               # the word was for the list shown last
    assert delete(y, both)["error"] == words.DELETE_NOT_RELEASED and delete(y, one)["ok"]
    assert not j["dump"].exists() and j["backup"].is_dir()


def test_a_delete_that_cannot_go_is_said_before_the_owner_is_asked_for_a_word(tmp_path, monkeypatch):
    y, room, ch, j = setup(tmp_path)
    monkeypatch.setattr(deletes, "mounts", lambda: [])
    (via := tmp_path / "via").symlink_to(j["junk"])
    state = tmp_path / "state"
    (work := state / "rooms" / "example-room" / "work").mkdir(parents=True, exist_ok=True)
    (work / "notes.md").write_text("x")
    cases = [([], words.DELETE_NO_PATHS), (["junk/core.4242"], words.DELETE_NOT_WHOLE.format(given="junk/core.4242")),
             ([f"{j['junk']}/.."], words.DELETE_NOT_WHOLE.format(given=f"{j['junk']}/..")),
             ([str(via / "core.4242")], words.DELETE_NOT_REAL.format(path=via / "core.4242", real=j["dump"])),
             ([f"{j['backup']}/../core.4242"], words.DELETE_NOT_REAL.format(path=f"{j['backup']}/../core.4242", real=j["dump"])),
             ([str(j["dump"]), str(j["junk"] / "gone.txt")], words.DELETE_NOT_THERE.format(path=j["junk"] / "gone.txt")),
             (["/tmp"], words.DELETE_TOP.format(path="/tmp")), (["/"], words.DELETE_TOP.format(path="/")),
             ([str(Path.home())], words.DELETE_TOP.format(path=Path.home())),
             ([str(work / "notes.md")], words.DELETE_OWN.format(path=work / "notes.md")),
             ([str(tmp_path)], words.DELETE_OWN.format(path=tmp_path)), ([str(REPO)], words.DELETE_OWN.format(path=REPO)),
             ([str(EXAMPLE_PRIVATE / "rooms.toml")], words.DELETE_OWN.format(path=EXAMPLE_PRIVATE / "rooms.toml"))]
    for paths, why in cases:
        assert denied(pre(y, {"paths": paths})) == words.NOT_DELETED.format(why=why), paths
    (j["junk"] / "disk").mkdir()
    monkeypatch.setattr(deletes, "mounts", lambda: [j["junk"] / "disk"])               # another disk mounted in the junk
    for at in (j["junk"] / "disk", j["junk"]):
        assert denied(pre(y, {"paths": [str(at)]})) == words.NOT_DELETED.format(why=words.DELETE_MOUNTED.format(path=at))
    assert ch.notes == [] and y.ledger.rows(5, "approvals") == [] and j["dump"].exists() and (work / "notes.md").exists()
    assert {(r["gate"], r["decision"]) for r in y.ledger.rows(30, "gates")} == {("delete", "deny")}
    assert denied(pre(y, {"paths": str(j["junk"] / "gone.txt")})) == words.NOT_DELETED.format(  # a lone path is read as one
        why=words.DELETE_NOT_THERE.format(path=j["junk"] / "gone.txt"))


def test_a_persons_room_never_deletes_outside_and_nothing_of_it_is_shown_there(tmp_path):
    y, room, ch, j = setup(tmp_path, people=True)
    assert denied(pre(y, {"paths": [str(j["dump"])]})) == words.NOT_DELETED.format(why=words.DELETE_OWNER_ROOMS)
    assert ch.notes == [] and y.ledger.rows(5, "approvals") == []                         # nothing shown, nothing to approve
    word(y, room)
    assert delete(y, [str(j["dump"])])["error"] == words.NOT_DELETED.format(why=words.DELETE_OWNER_ROOMS) and j["dump"].exists()
    assert delete(y, [str(j["dump"])], room="no-such-room")["error"] == words.NOT_DELETED.format(why=words.DELETE_OWNER_ROOMS)


def test_nothing_is_deleted_with_the_rule_off_or_a_hook_that_failed_open(tmp_path):
    y, room, ch, j = setup(tmp_path, gate={"off": ["delete-outside"]})
    assert pre(y, {"paths": [str(j["dump"])]}) == {} and ch.notes == []                  # nothing held, so nothing shown
    word(y, room)
    assert delete(y, [str(j["dump"])]) == {"ok": False, "error": words.DELETE_NOT_RELEASED} and j["dump"].exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root deletes from a read-only folder")
def test_what_could_not_be_deleted_is_said_with_why_and_the_rest_still_goes(tmp_path):
    j = junk(tmp_path)
    (j["backup"] / "inner").chmod(0o500)                                                  # its file cannot be removed
    went = deletes.on_disk(j["dump"]) + deletes.on_disk(j["backup"] / "b.tar")
    try:
        r = deletes.run([str(j["backup"]), str(j["dump"])], [tmp_path / "state"], Path.home())
        assert r["ok"] is False and r["deleted"] == [str(j["dump"])] and not j["dump"].exists()
        assert r["failed"] == [{"path": str(j["backup"]), "why": words.DELETE_PARTLY.format(       # the file, its folder, the backup
            n=3, first=f"{j['backup'] / 'inner' / 'a.tar'}: Permission denied")}]
        assert (j["backup"] / "inner" / "a.tar").exists() and not (j["backup"] / "b.tar").exists()
        assert r["freed"] == words.size(went)                                             # what went, counted on disk
    finally:
        (j["backup"] / "inner").chmod(0o700)


def test_the_mount_table_and_the_sizes_a_delete_is_said_in(tmp_path):
    (table := tmp_path / "mounts").write_text("/dev/sdb / ext4 rw 0 0\nX:\\134 /mnt/x\\040drive 9p rw 0 0\nproc /proc proc rw 0 0\n")
    assert deletes.mounts(table) == [Path("/mnt/x drive"), Path("/proc")] and deletes.mounts(tmp_path / "none") == []
    assert [words.size(n) for n in (12, 900 * 1024, 25 * 1024 * 1024, 3 * 1024 ** 3, int(312.4 * 1024 ** 3))] == [
        "12 bytes", "900 KB", "25 MB", "3 GB", "312.4 GB"]
