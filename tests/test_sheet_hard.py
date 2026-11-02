"""Checks the sheet rule as a hard stop: in one of your rooms a `send` of a document, or of a text holding a pasted block, with no
claims sheet (each claim with its source line and quote) written that turn is turned back once with what the sheet is and where it goes,
and the next send of the turn goes out as it is. A final text holding a pasted block bounces once at Stop the same way; one shadow row
`sheet` per refusal, which the morning diff counts; a person's room only logs, as before. The case behind it (2026-10-22): two PDFs, a
crib file and pasted blocks went out in the owner's name in one morning with no claims sheet, each ledgered by the shadow rule and none
And a part a session start sends trimmed is said in the machine room, once a day. Every value here is a placeholder."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

from test_gates import Rec, call, human_turn, setup, turn_of
from test_observe import layer

from yerk_ai import context, memory, morning, status, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
from yerk_ai.engine import SessionTable

PASTE = "Here is the block for the ticket:\n```\nExample line one.\nExample line two.\n```\n"
DOC = "EX-905-v0.1.pdf"
SHEET_ROW, PASTE_ROW = f"sent {DOC} with no claims sheet written this turn", "pasted a block with no claims sheet written this turn"


def work_of(tmp_path, room="example-room"):
    (work := tmp_path / "rooms" / room / "work").mkdir(parents=True, exist_ok=True)
    return work.resolve()


def send(y, text, file="", room="example-room"):
    return asyncio.run(y.tool_send(room, text, str(file), room))


def next_turn(room):
    """A new turn, begun a moment ago: its card and flags (Room._new_card) and its evidence, as Room._turn starts one."""
    room._new_card()
    room.evidence.reset()
    room.evidence.started -= 5


def rows(y):
    return [r["detail"] for r in reversed(y.ledger.rows(50, "shadow")) if r["rule"] == "sheet"]


def owner_room(tmp_path):
    y, room, _ = setup(tmp_path)
    work = work_of(tmp_path)
    (work / DOC).write_bytes(b"%PDF-1.4 example")
    human_turn(room, "send the example page")
    return y, room, work


def test_a_pdf_with_no_sheet_is_refused_once_and_goes_out_once_a_sheet_is_beside_it(tmp_path, capsys):
    y, room, work = owner_room(tmp_path)
    r = send(y, "EX-905, every line traced.", work / DOC)
    assert r == {"ok": False, "error": words.SHEET.format(done="sent", what=DOC, where=f"{work / 'EX-905-v0.1-claims.md'}, beside it",
                                                          thing="document", again="send again", next="send")}
    assert "one row per claim the document makes: the claim, its source line (file and line number) and the quote" in r["error"]
    assert '"file"' not in capsys.readouterr().out                                      # nothing reached the room
    next_turn(room)
    (sheet := work / "EX-905-v0.1-claims.md").write_text("claim | source line | quote\n")     # beside it, this turn
    assert send(y, "EX-905, every line traced.", work / DOC)["ok"] and f'"file": "{work / DOC}"' in capsys.readouterr().out
    next_turn(room)
    os.utime(sheet, (room.evidence.started - 60,) * 2)                                  # a sheet of an earlier turn is none
    room.evidence.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "w0", "name": "Write",
                                                                         "input": {"file_path": str(work / "EX-905-notes.md")}}]}})
    assert not send(y, "EX-905 again.", work / DOC)["ok"]                               # nor is a file of another name
    next_turn(room)                                                                     # a call of the turn wrote one
    sheet.unlink()
    room.evidence.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "w1", "name": "Write",
                                                                         "input": {"file_path": str(sheet), "content": "claim"}}]}})
    assert send(y, "EX-905, every line traced.", work / DOC)["ok"]
    assert rows(y) == [SHEET_ROW, SHEET_ROW]


def test_a_pasted_block_with_no_sheet_is_refused_once_and_goes_out_with_one(tmp_path, capsys):
    y, room, work = owner_room(tmp_path)
    r = send(y, PASTE)
    assert r == {"ok": False, "error": words.SHEET.format(done="sent", what="the fenced block in this message", where=str(work / "claims.md"),
                                                          thing="block", again="send again", next="send")}
    capsys.readouterr()
    next_turn(room)
    (work / "claims.md").write_text("claim | source line | quote\n")
    assert send(y, PASTE)["ok"] and "Example line one." in capsys.readouterr().out
    next_turn(room)
    (work / "claims.md").unlink()
    assert send(y, "```python\nprint('code, not a paste')\n```")["ok"] and send(y, "one plain line")["ok"]
    (work / "chart.png").write_bytes(b"example")
    assert send(y, "the chart", work / "chart.png")["ok"]                               # a picture is no document
    assert rows(y) == [PASTE_ROW]


def test_after_the_refusal_the_next_send_of_the_turn_goes_out_whatever_it_holds(tmp_path, capsys):
    y, room, work = owner_room(tmp_path)
    assert not send(y, "EX-905.", work / DOC)["ok"]
    assert send(y, "EX-905.", work / DOC)["ok"] and send(y, PASTE)["ok"]                # asked once a turn, never twice
    assert call(y, {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": PASTE}) == {}
    assert rows(y) == [SHEET_ROW]


def test_a_persons_room_is_never_refused_and_its_stop_still_only_logs(tmp_path, capsys):
    y, _, _ = setup(tmp_path)
    fam = "family-room"
    y.room_confs[fam] = {**y.room_confs["example-room"], "privacy": "family", "people": [1001], "id": f"{fam}-id"}
    room = y.add_room(fam, Rec())
    SessionTable(tmp_path / "sessions.json").bind(fam, "sid-f", "one")
    work = work_of(tmp_path, fam)
    (work / DOC).write_bytes(b"%PDF-1.4 example")
    human_turn(room, "send the example page", sender="person")
    assert send(y, PASTE, work / DOC, room=fam)["ok"] and send(y, PASTE, room=fam)["ok"]
    room.evidence = turn_of([("mcp__yerk__send", {"room": fam, "text": PASTE, "file": str(work / DOC)})])
    out = call(y, {"hook_event_name": "Stop", "session_id": "sid-f", "stop_hook_active": False, "last_assistant_message": PASTE})
    assert out == {} and rows(y) == [f"sent {DOC} and pasted a block with no claims sheet written this turn"]   # logged, as before


def test_a_final_text_with_a_pasted_block_and_no_sheet_bounces_once_at_stop(tmp_path):
    y, room, work = owner_room(tmp_path)
    stop = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": PASTE}
    assert call(y, stop) == {"decision": "block", "reason": words.SHEET.format(
        done="posted", what="the fenced block in your final message", where=str(work / "claims.md"), thing="block",
        again="write your final message again", next="final message")}
    assert call(y, {**stop, "stop_hook_active": True}) == {} and call(y, stop) == {}    # once, and no second row
    assert send(y, "EX-905.", work / DOC)["ok"]                                         # asked once a turn: a send after it goes out
    next_turn(room)
    (work / "claims.md").write_text("claim | source line | quote\n")
    assert call(y, stop) == {} and call(y, {**stop, "last_assistant_message": "One plain line."}) == {}
    assert rows(y) == [PASTE_ROW]


def test_one_shadow_row_per_refusal_and_the_morning_diff_counts_them(tmp_path):
    y, room, work = owner_room(tmp_path)
    stop = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": PASTE}
    assert [send(y, "EX-905.", work / DOC)["ok"], send(y, "EX-905.", work / DOC)["ok"], call(y, stop)] == [False, True, {}]
    next_turn(room)
    assert call(y, stop)["decision"] == "block"
    assert rows(y) == [SHEET_ROW, PASTE_ROW]                                            # two refusals, two rows
    priv, state, accounts, cool = layer(tmp_path / "diff", machine=None)
    noon = status.midnight(time.time()) + 86400 + 43200     # noon of the next LOCAL day; now + 86400 is still today at 00:xx on a fall-back night
    lines, stamp = morning.build(priv, state, accounts, cool, y.ledger, {"started": 0, "units": []}, noon)
    assert f"{words.WORKING} shadow flag sheet: 2" in lines and "shadow sheet" in stamp["findings"]


def machine_said(capsys) -> list[str]:
    return [p["notice"] for p in (json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{"))
            if p["room"] == "example-machine"]


def test_a_part_over_its_line_at_session_start_is_said_once_a_day_in_the_machine_room(tmp_path, capsys):
    (state := tmp_path / "state").mkdir()
    y = Yerkd(EXAMPLE_PRIVATE, state)
    y.room_confs["example-machine"] = {**y.room_confs["example-room"], "role": "machine", "id": "example-machine-id"}
    for name in ("example-room", "example-machine"):
        y.add_room(name, ConsoleChannel())
    rules = y.memory.root / memory.RULES
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text("".join(f"- example job {i}: done the example way, step by step.\n" for i in range(20)))
    y.spec_for("example-room")
    assert machine_said(capsys) == []                                                   # under its line: nothing said
    rules.write_text("".join(f"- example job {i}: done the example way, step by step.\n" for i in range(300)))
    y.spec_for("example-room")
    part = next(p for p in context.session_start(y.private, y.room_confs["example-room"], y.budgets, rules=rules) if p.name == "rules")
    cut, cap = list(part.cut), y.budgets["session_start"]["appended"]["rules"]
    assert part.raw > cap and len(cut) > 12 and all(x not in part.text.split("\n") for x in cut)
    assert machine_said(capsys) == [f"{words.WORKING} rules: {len(cut)} lines dropped\nto fit its {cap:,} in example-room"]
    assert cut == [f"- example job {i}: done the example way, step by step." for i in range(300 - len(cut), 300)]   # the newest, whole
    y.spec_for("example-room")
    y.spec_for("example-machine")                                                       # the same file, another room
    assert machine_said(capsys) == []                                                   # once a day per part and file
    (mark := state / "cut-said.json").write_text(json.dumps({f"rules {part.source}": "2026-01-01"}))   # a new day
    y.workers.loop = loop = asyncio.new_event_loop()                                    # and the daemon's loop running, as live
    (t := threading.Thread(target=loop.run_forever)).start()
    try:
        y.spec_for("example-room")                                                      # on this thread, as on a room's
        for _ in range(100):
            if got := machine_said(capsys):
                break
            time.sleep(0.02)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join()
        loop.close()
    assert len(got) == 1 and json.loads(mark.read_text()) == {f"rules {part.source}": time.strftime("%Y-%m-%d")}
