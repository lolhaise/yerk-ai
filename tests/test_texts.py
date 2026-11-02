"""Checks the `text` tool (texts.py), with which a room texts a person in your name from your Mac through its bridge: the
text is held first with its words and who it goes to shown in the room, then sent once on your word, and never from a
person's room, from a worker, past a rule turned off or a hook that failed open. The case behind it (need 1 of 2026-10-24):
a room asked to text a person for the owner had no tool to send one, the Mac's Messages would not answer over plain ssh,
and the room wrote the line for the owner to paste and send. A text that did not go is told apart from one that may have. A
nothing leaves the machine, and every number and address is a placeholder."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from test_gates import Rec, call
from yerk_ai import gates, mcp_server, texts, words
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


def test_a_worker_never_texts_as_the_owner(tmp_path):
    out = gates.worker_check({"tool_name": texts.TOOL, "tool_input": TEXT}, tmp_path, {"room": "example-room"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "a worker never takes one" in out["hookSpecificOutput"]["permissionDecisionReason"]
