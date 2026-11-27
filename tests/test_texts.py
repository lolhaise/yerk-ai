"""Checks the `text` tool (texts.py), with which a room texts a person in your name from your Mac through its bridge: the
text is held first with its words and who it goes to shown in the room, then sent once on your word, and never from a
person's room, from a worker, past a rule turned off or a hook that failed open. The case behind it (need 1 of 2026-10-24):
a room asked to text a person for the owner had no tool to send one, the Mac's Messages would not answer over plain ssh,
and the room wrote the line for the owner to paste and send. A text that did not go is told apart from one that may have. A
fake bridge here stands in for the Mac: nothing leaves the machine, and every number and address is a placeholder."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from test_gates import Rec, call
from test_page import add_on
from yerk_ai import gates, mcp_server, texts, words
from yerk_ai.channels import page
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import YERK_TOOLS, Yerkd
from yerk_ai.engine import SessionTable
from yerk_ai.room import Inbound

# the Mac's bridge as imsg serves it, one JSON-RPC line each way: every request it reads is logged, then answered by `mode`
FAKE = '''
import json, sys
mode, log = sys.argv[1], sys.argv[2]
if mode == "gone":                      # ssh could not reach the Mac: it ends before a word
    sys.exit(255)
for line in sys.stdin:
    req = json.loads(line)
    with open(log, "a") as f:
        f.write(json.dumps(req) + "\\n")
    rid, m = req["id"], req["method"]
    if m == "status":
        out = {"error": {"code": -32601, "message": "unknown method"}} if mode == "old" else {"result": {"ready": True}}
    elif mode == "refuse":
        out = {"error": {"code": -32602, "message": "invalid handle"}}
    elif mode == "maybe":
        out = {"error": {"code": -32001, "message": "may have completed", "data": {"retry_safe": False}}}
    elif mode == "silent":
        continue
    else:
        print("not json", flush=True)                                     # skipped, as a notification is
        print(json.dumps({"jsonrpc": "2.0", "method": "watch.message", "params": {}}), flush=True)
        out = {"result": {"ok": True, "id": 7, "guid": "GUID-0001"}}
    print(json.dumps({"jsonrpc": "2.0", "id": rid, **out}), flush=True)
'''
TEXT = {"to": "+1 555 0001111", "text": "Running late, there by 7.\nSave me a seat?"}
at = "@".join                                      # an address, put together here: the repository holds none
SOCK = "/Users/example/.bridge/rpc.sock"           # the Mac's bridge socket, as an add-on's mac.json names it


def mac(tmp_path: Path, mode: str = "ok", **extra: object) -> tuple[dict, Path]:
    """[imessage] for the fake bridge in `mode`, and the file its requests land in."""
    (script := tmp_path / "bridge.py").write_text(FAKE)
    log = tmp_path / f"bridge-{mode}.jsonl"
    return {"command": [sys.executable, "-I", str(script), mode, str(log)], "send_timeout": 1, **extra}, log


def asked(log: Path) -> list[tuple[str, dict]]:
    return [(r["method"], r["params"]) for r in map(json.loads, log.read_text().splitlines())] if log.is_file() else []


def setup(tmp_path: Path, mode: str = "ok", people: bool = False, gate: dict | None = None) -> tuple[Yerkd, object, Rec, Path]:
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    if gate is not None:
        y.gates = gates.settings(gate)
    if people:
        y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    y.conf["imessage"], log = mac(tmp_path, mode)
    ch = Rec()
    room = y.add_room("example-room", ch)
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-g", "one")
    return y, room, ch, log


def pre(y: Yerkd, ti: dict) -> dict:
    return call(y, {"hook_event_name": "PreToolUse", "tool_name": texts.TOOL, "tool_input": ti})


def held(out: dict) -> bool:
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def text(y: Yerkd, ti: dict, room: str = "example-room") -> dict:
    return asyncio.run(y.tool_text(room, ti["to"], ti["text"]))


def word(y: Yerkd, room) -> dict:
    room.busy, room.inflight = True, [Inbound(f"m{len(y.worded)}", "owner", "approve")]   # the owner's word, in the turn
    return call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "approve"})


def test_every_room_is_handed_the_text_tool_where_the_room_of_the_need_had_none(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", Rec())
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert list(tools["text"].inputSchema["properties"]) == ["to", "text"] and texts.TOOL in YERK_TOOLS
    assert "from the owner's Mac" in str(tools["text"].description) and "never a name" in str(tools["text"].description)
    assert texts.TOOL in y.spec_for("example-room").settings["permissions"]["allow"]
    assert gates.external_hit(texts.TOOL, TEXT, "example-room", y.gates)[:2] == ("text-out", "a text in your name")


def test_a_text_is_held_with_its_words_shown_and_the_owners_word_sends_it_once(tmp_path):
    y, room, ch, log = setup(tmp_path)
    out = pre(y, TEXT)
    assert held(out) and out["hookSpecificOutput"]["permissionDecisionReason"].startswith("Held: a text in your name")
    assert ch.notes == [f"{words.WORKING} held: a text in your name\nsay approve here to let it run once\n"
                        "```\nto: +15550001111\nRunning late, there by 7.\nSave me a seat?\n```"]   # as it reads and goes, never JSON
    assert text(y, TEXT) == {"ok": False, "maybe_sent": False, "error": words.TEXT_NOT_RELEASED} and asked(log) == []
    assert "[approval] owner said approve" in word(y, room)["hookSpecificOutput"]["additionalContext"]
    assert pre(y, TEXT) == {}                                                     # the same call, released
    assert text(y, TEXT) == {"ok": True, "to": "+15550001111", "guid": "GUID-0001"}
    assert asked(log) == [("status", {}), ("send", {"to": "+15550001111", "text": TEXT["text"], "service": "auto"})]
    assert text(y, TEXT)["error"] == words.TEXT_NOT_RELEASED and len(asked(log)) == 2     # once: never again on that word
    assert held(pre(y, TEXT))                                                     # the next one is held again
    assert [(r["gate"], r["decision"]) for r in reversed(y.ledger.rows(10, "gates"))] == [
        ("approval", "hold"), ("approval", "approved"), ("approval", "release"), ("text", "sent"), ("approval", "hold")]
    assert "to +15550001111" in y.ledger.rows(10, "gates")[1]["detail"]          # the receipt: who it went to, its id


def test_the_preview_names_who_the_text_goes_to_as_it_is_sent_never_as_the_room_wrote_it(tmp_path):
    """PR 158's cold read: an address shaped like a path was shown by its last name, so the owner said the word for one
    person and the text went to another; a number broken over two lines read as its first half too."""
    y, room, ch, log = setup(tmp_path)
    for to in ("/x/" + at(["y", "example.test"]), "+1 555\n0001111"):
        assert held(pre(y, {**TEXT, "to": to}))
        word(y, room)
        assert pre(y, {**TEXT, "to": to}) == {} and text(y, {**TEXT, "to": to})["ok"]
    shown = [n.split("```\n")[1].splitlines()[0] for n in ch.notes if "```" in n]       # the preview's first line
    assert shown == [f"to: {p['to']}" for m, p in asked(log) if m == "send"] == ["to: /x/" + at(["y", "example.test"]), "to: +15550001111"]


def test_an_edit_is_a_new_preview_and_the_word_sends_only_the_newest(tmp_path):
    y, room, ch, log = setup(tmp_path)
    edit = {**TEXT, "text": "Running late, there by 8."}
    assert held(pre(y, TEXT)) and held(pre(y, edit)) and ch.notes[1].endswith("```\nto: +15550001111\nRunning late, there by 8.\n```")
    word(y, room)
    assert held(pre(y, TEXT)) and pre(y, edit) == {}                               # the word was for the text shown last
    assert text(y, TEXT)["error"] == words.TEXT_NOT_RELEASED and text(y, edit)["ok"]
    assert [p["text"] for m, p in asked(log) if m == "send"] == ["Running late, there by 8."]


def test_the_word_sends_the_held_text_once_whatever_folder_the_shell_is_in_when_it_is_called_again(tmp_path):
    """need 78 of 2026-11-09, the wall: a text held with the room's shell in a folder under its work folder, a /compact
    before the owner's word, and the session back in the work folder; the same call matched no approved hold, the folder
    being part of the match, so it was held again on a second card and the owner said the word twice. The word is for the
    call, wherever the shell is."""
    y, room, ch, log = setup(tmp_path)
    work = tmp_path / "rooms" / "example-room" / "work"
    held_in, back_in = str(work / "notes"), str(work)

    def pre_in(cwd: str) -> dict:
        return call(y, {"hook_event_name": "PreToolUse", "tool_name": texts.TOOL, "tool_input": TEXT, "cwd": cwd})
    assert held(pre_in(held_in))
    word(y, room)
    assert pre_in(back_in) == {} and len(ch.notes) == 1               # released from the work folder: no second card
    assert text(y, TEXT)["ok"] and [m for m, _ in asked(log)].count("send") == 1
    assert held(pre_in(held_in)) and text(y, TEXT)["error"] == words.TEXT_NOT_RELEASED    # once: the next one is held again
    rows = y.ledger._select("SELECT * FROM approvals ORDER BY id", ())
    assert [(r["cwd"], bool(r["approved"]), bool(r["released"])) for r in rows] == [(held_in, True, True), (held_in, False, False)]
    assert [r["decision"] for r in y.ledger.rows(10, "gates")].count("approved") == 1      # one word


def test_a_persons_room_never_texts_as_the_owner_and_nothing_of_it_is_shown_there(tmp_path):
    y, room, ch, log = setup(tmp_path, people=True)
    out = pre(y, TEXT)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"] == words.TEXT_NOT_SENT.format(why=words.TEXT_OWNER_ROOMS)
    assert ch.notes == [] and y.ledger.rows(5, "approvals") == []                 # no preview, nothing to approve
    word(y, room)
    assert text(y, TEXT)["error"] == words.TEXT_NOT_SENT.format(why=words.TEXT_OWNER_ROOMS) and asked(log) == []
    assert text(y, TEXT, room="no-such-room")["error"] == words.TEXT_NOT_SENT.format(why=words.TEXT_OWNER_ROOMS)


def test_a_text_that_cannot_go_is_said_before_the_owner_is_asked_for_a_word(tmp_path):
    y, room, ch, log = setup(tmp_path)
    for ti, why in [({**TEXT, "to": "Kettle Person"}, words.TEXT_NO_HANDLE.format(given="Kettle Person")), ({**TEXT, "text": "  "}, words.TEXT_EMPTY)]:
        out = pre(y, ti)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"] == words.TEXT_NOT_SENT.format(why=why)
    y.conf.pop("imessage")
    assert pre(y, TEXT)["hookSpecificOutput"]["permissionDecisionReason"] == words.TEXT_NOT_SENT.format(why=words.TEXT_NO_SETUP)
    assert ch.notes == [] and y.ledger.rows(5, "approvals") == [] and asked(log) == []
    assert [(r["gate"], r["decision"]) for r in y.ledger.rows(5, "gates")] == [("text", "deny")] * 3


def test_no_text_goes_without_its_preview_with_the_rule_off_or_a_hook_that_failed_open(tmp_path):
    y, room, ch, log = setup(tmp_path, gate={"off": ["text-out"]})
    assert pre(y, TEXT) == {} and ch.notes == []                                  # nothing held, so nothing shown
    word(y, room)
    assert text(y, TEXT) == {"ok": False, "maybe_sent": False, "error": words.TEXT_NOT_RELEASED} and asked(log) == []


def test_a_text_that_did_not_go_is_told_apart_from_one_that_may_have(tmp_path, monkeypatch):
    def send(mode: str, **extra: object) -> tuple[dict, list[str]]:
        conf, log = mac(tmp_path, mode, **extra)
        return asyncio.run(texts.send(conf, TEXT["to"], TEXT["text"])), [m for m, _ in asked(log)]
    r, got = send("old", service="sms", send_transport="applescript")             # an older bridge, with no status
    assert r["ok"] and got == ["status", "send"]
    assert asked(tmp_path / "bridge-old.jsonl")[1][1] == {"to": "+15550001111", "text": TEXT["text"], "service": "sms",
                                                          "transport": "applescript"}
    assert send("refuse") == ({"ok": False, "maybe_sent": False, "error": words.TEXT_NOT_SENT.format(why=words.TEXT_WHY[-32602])},
                              ["status", "send"])
    assert send("maybe") == ({"ok": False, "maybe_sent": True, "error": words.TEXT_MAYBE}, ["status", "send"])
    assert send("silent", send_timeout=0.3) == ({"ok": False, "maybe_sent": True, "error": words.TEXT_MAYBE}, ["status", "send"])
    away = {"ok": False, "maybe_sent": False, "error": words.TEXT_NOT_SENT.format(why=words.TEXT_AWAY)}
    assert send("gone") == (away, [])                                             # the send was never written
    monkeypatch.setattr(texts, "HELLO", 0.3)
    hung = {"command": [sys.executable, "-I", "-c", "import time; time.sleep(30)"]}   # ssh that never connects
    assert asyncio.run(texts.send(hung, TEXT["to"], TEXT["text"])) == away
    gone = {"command": [str(tmp_path / "no-such-program")]}
    assert asyncio.run(texts.send(gone, TEXT["to"], TEXT["text"]))["maybe_sent"] is False


def test_a_text_that_may_have_gone_out_is_ledgered_so_and_the_room_is_told_never_to_send_it_again_unasked(tmp_path):
    y, room, ch, log = setup(tmp_path, mode="maybe")
    pre(y, TEXT)
    word(y, room)
    assert pre(y, TEXT) == {}
    assert text(y, TEXT) == {"ok": False, "maybe_sent": True, "error": words.TEXT_MAYBE} and "never send it again unasked" in words.TEXT_MAYBE
    assert y.ledger.rows(1, "gates")[0]["decision"] == "maybe sent" and len(asked(log)) == 2


def test_the_way_to_the_mac_and_who_a_text_can_go_to():
    sock = "/Users/example/Library/rpc.sock"
    assert texts.transport({"host": "mac.example", "socket": sock}) == [
        "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mac.example", "/opt/homebrew/bin/socat", "-",
        f"UNIX-CONNECT:{sock}"]
    assert texts.transport({"command": "nc -U /tmp/example.sock"}) == ["nc", "-U", "/tmp/example.sock"]
    assert texts.transport({"command": ["a", 1]}) == ["a", "1"] and texts.transport({"host": "mac.example"}) == [] == texts.transport({})
    assert [texts.handle(x) for x in ("+1 555 0001111", "(555) 0001111", f" {at(['kettle', 'example.test'])} ", "Kettle Person", "12", "",
                                      "Kettle Person and Example Co", "+1 555 0001111 2222 3333")] == [
        "+15550001111", "5550001111", at(["kettle", "example.test"]), "", "", "", "", ""]


def test_with_nothing_in_yerkd_toml_a_text_goes_by_the_mac_and_bridge_an_owner_room_s_add_on_names(tmp_path, tmp_path_factory, monkeypatch):
    """need-2026-11-08-74, the wall a room hit: the owner asked a room to send their reply to a friend as an iMessage from the
    Mac, and the gate said no Mac was set up for texts, though the box reached that Mac by ssh for the hands plug-in's mac
    tool (which cannot drive Messages) and the Mac's bridge was up there; a room cannot set yerkd.toml mid-conversation.
    With no [imessage] way, the Mac and its bridge are the ones an owner room's add-on names together in its mac.json, so
    one deploy gives every owner room the send, held for the owner's word as ever; a person's room's add-on, or one under
    the state folder (`tmp_path` here), where each room's folder is, is never read."""
    y, room, ch, log = setup(tmp_path)
    out = tmp_path_factory.mktemp("add-ons")                  # outside the state folder, as the hands plug-in's own checkout
    y.conf.pop("imessage")
    (bin_ := tmp_path / "mac-bin").mkdir()                   # ssh: its command line written down, its stdin the bridge's
    (ssh := bin_ / "ssh").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/ssh-args"\n'
                                     f'exec "{sys.executable}" -I "{tmp_path}/bridge.py" ok "{log}"\n')
    ssh.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_}:{page.os.environ['PATH']}")
    no_mac = words.TEXT_NOT_SENT.format(why=words.TEXT_NO_SETUP)
    assert pre(y, TEXT)["hookSpecificOutput"]["permissionDecisionReason"] == no_mac           # the wall, as it was
    for there in (add_on(out), add_on(tmp_path, imessage_socket=SOCK)):                      # no bridge named; a room could write it
        y.room_confs["example-room"]["mcp"] = {"hands": there}
        assert pre(y, TEXT)["hookSpecificOutput"]["permissionDecisionReason"] == no_mac
    y.room_confs["example-quiet"] = {**y.room_confs["example-quiet"], "privacy": "family",
                                     "mcp": {"hands": add_on(out, "family@family-mac", name="family", imessage_socket=SOCK)}}
    assert pre(y, TEXT)["hookSpecificOutput"]["permissionDecisionReason"] == no_mac           # an owner room's add-on only
    y.room_confs["example-room"]["mcp"] = {"hands": add_on(out, imessage_socket=SOCK)}
    assert held(pre(y, TEXT)) and ch.notes[-1].endswith("```\nto: +15550001111\nRunning late, there by 7.\nSave me a seat?\n```")
    assert text(y, TEXT)["error"] == words.TEXT_NOT_RELEASED and asked(log) == []             # held for the word as ever
    word(y, room)
    assert pre(y, TEXT) == {} and text(y, TEXT) == {"ok": True, "to": "+15550001111", "guid": "GUID-0001"}
    assert (tmp_path / "ssh-args").read_text().splitlines() == [
        "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "owner@owner-mac", "/opt/homebrew/bin/socat", "-", f"UNIX-CONNECT:{SOCK}"]
    assert asked(log) == [("status", {}), ("send", {"to": "+15550001111", "text": TEXT["text"], "service": "auto"})]
    assert y.ledger.rows(1, "gates")[0]["decision"] == "sent"


def test_the_bridge_is_yerkd_toml_s_way_else_a_host_and_socket_one_add_on_names_together_and_never_a_shell_word(tmp_path):
    """The socket rides ssh's command line to the Mac's shell, so one from mac.json is taken only as a whole path of plain
    characters and only with the host that same file names, read only where addon_file trusts it; yerkd.toml's own way
    wins, its other keys (service, send_timeout, socat) kept."""
    mac = {"ssh": "owner@owner-mac", "imessage_socket": SOCK}
    assert texts.reach({}, mac) == {"host": "owner@owner-mac", "socket": SOCK}
    assert texts.reach({"host": "other-mac", "service": "sms"}, mac) == {"host": "owner@owner-mac", "socket": SOCK, "service": "sms"}
    own, cmd = {"host": "mac.example", "socket": "/tmp/example.sock"}, {"command": "nc -U /tmp/example.sock"}
    assert texts.reach(own, mac) == own and texts.reach(cmd, mac) == cmd
    for sock in ("", "rpc.sock", "~/rpc.sock", "/a b.sock", "/a;touch x", "/a$(touch x)", "/a\n.sock", 5, None, ["/a.sock"]):
        assert texts.reach({}, {"ssh": "owner@owner-mac", "imessage_socket": sock}) == {}, sock
    assert texts.reach({}, {"imessage_socket": SOCK}) == {} and texts.reach({}, {}) == {}
    assert page.addon_file([add_on(tmp_path, imessage_socket=SOCK)]) == mac                  # the file's keys, whole
    assert page.addon_file([add_on(tmp_path, mode=0o646, name="open", imessage_socket="/other.sock"), add_on(tmp_path, name="b")]) == {
        "ssh": "owner@owner-mac"}                                                             # any user may write the first
    assert page.addon_file([add_on(tmp_path, "-oProxyCommand=x", name="c", imessage_socket=SOCK)]) == {} == page.addon_file([])


def test_a_worker_never_texts_as_the_owner(tmp_path):
    out = gates.worker_check({"tool_name": texts.TOOL, "tool_input": TEXT}, tmp_path, {"room": "example-room"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "a worker never takes one" in out["hookSpecificOutput"]["permissionDecisionReason"]
