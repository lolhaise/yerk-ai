"""Checks the live progress card a room shows while it works, built from Claude Code's streamed events: narration as it streams,
target in plain words, ticked or crossed, the thinking line only where a room turns it on."""
import json
from types import SimpleNamespace as NS

from yerk_ai import words
from yerk_ai.card import FAILED, OK, ROW_MAX, RUNNING, START, Card
from yerk_ai.room import Room


def se(event):
    return {"type": "stream_event", "event": event}


def tool(i, tid, name, args):
    return [se({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": tid, "name": name,
                                                                             "input": {}}}),
            se({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": ""}}),
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": args}]}}]


def result(tid, error=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": error}]}}


def test_the_card_streams_narration_then_rows_ticked_and_crossed():
    c = Card()
    assert c.render() == f"{RUNNING} {START}"
    assert c.feed(se({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})) is False
    c.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Checking the "}}))
    c.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "notes first"}}))
    assert c.render() == f"{RUNNING} Checking the notes first"
    for ev in tool(1, "a", "Read", {"file_path": "/x/memory/pinned.md"}) + tool(2, "b", "Bash",
                                                                                {"command": "git status --short\nls"}):
        c.feed(ev)
    for ev in tool(3, "c", "WebFetch", {"url": "https://docs.example.org/a?b=1"}) + tool(
            4, "d", "mcp__yerk__send", {"room": "r", "file": "/w/report.pdf"}) + tool(5, "e", "Grep", {"pattern": "owner",
                                                                                                    "path": "/x/rules.md"}):
        c.feed(ev)
    c.feed(result("a"))
    c.feed(result("b", error=True))
    lines = c.render().splitlines()
    assert lines == [f"{RUNNING} Checking the notes first", f"{OK} reading the pinned notes", f"{FAILED} running a command",
                     f"{RUNNING} reading docs.example.org", f"{RUNNING} sending report.pdf",
                     f"{RUNNING} searching the rules for owner"]


def test_rows_stay_short_and_the_newest_win():
    c = Card()
    for i in range(20):
        for ev in tool(i, f"t{i}", "Bash", {"command": "echo " + "x" * 200}):
            c.feed(ev)
    lines = c.render().splitlines()
    assert all(len(x) <= ROW_MAX for x in lines) and lines[1] == "-# 8 earlier steps" and len(lines) == 14


def test_a_row_gets_its_target_from_the_streamed_input_alone():
    c = Card()
    c.feed(se({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "z", "name": "Glob"}}))
    c.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta",
                                                                    "partial_json": '{"pattern": "**/*.md"}'}}))
    c.feed(se({"type": "content_block_stop", "index": 0}))
    assert c.render().splitlines()[1] == f"{RUNNING} looking for md files"


def test_the_thinking_line_shows_only_where_the_room_turns_it_on():
    events = [se({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
              se({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "y" * 300}})]
    on, off = Card(thinking=True), Card()
    for ev in events:
        on.feed(ev)
        off.feed(ev)
    first = on.render().splitlines()[1]                                # held to phone width, like every card line
    assert first.startswith("-# thinking: yyy") and first.endswith("...") and words.width(first) <= words.PHONE
    assert off.render() == f"{RUNNING} {START}"                        # the default: no thinking line
    short = Card(thinking=True)                                        # a summary arrives in a burst: no fragment
    short.feed(events[0])
    short.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Doub"}}))
    assert short.render() == f"{RUNNING} {START}"
    short.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "le-check"}}))
    short.feed(se({"type": "content_block_stop", "index": 0}))
    assert short.render().splitlines()[1] == "-# thinking: Double-check"


def test_a_card_with_nothing_to_show_says_what_was_owed():
    c = Card()
    for ev in tool(0, "a", "Read", {"file_path": "/x/units.md"}):
        c.feed(ev)
    first = c.owed(words.why("auth", "error"), "what units does the owner prefer, check the file please")
    assert first.splitlines()[:3] == [f"{FAILED} not answered", "the account's login was refused",
                                      '-# you asked: "what units does the..."']
    assert first.splitlines()[3] == f"{RUNNING} reading units"


def test_an_image_never_read_bounces_once_at_stop(tmp_path):
    room = Room.__new__(Room)
    room.lock, room.rp, room.bounced = __import__("threading").Lock(), NS(read_paths=set()), False
    room.conf = {"privacy": "family"}            # a room that files no picture it reads (test_pictures has an owner's)
    room.to_open = {"/in/5/photo.png": "photo.png"}
    reason = room.stop_check(active=False)
    assert reason and "photo.png" in reason and "/in/5/photo.png" in reason
    assert room.stop_check(active=False) is None                       # once per result
    room.bounced = False
    assert room.stop_check(active=True) is None                        # Claude Code's own loop guard
    room.rp.read_paths.add("/in/5/photo.png")
    assert room.stop_check(active=False) is None                       # read: nothing to say


# the rows in plain words (STYLE.md rule 4; the owner, 2026-10-19: a card a person who knows nothing can read)
MEM = "/home/someone/.local/state/yerk/memory"                         # the memory tree: "your notes", never a path


def rows(*calls, failed=()):
    """A card fed `calls` of (tool name, input), each result back in turn; its rows, without the head."""
    c = Card()
    for i, (name, args) in enumerate(calls):
        for ev in tool(i, f"t{i}", name, args):
            c.feed(ev)
        c.feed(result(f"t{i}", error=i in failed))
    return c.render().splitlines()[1:]


def test_rule_1_a_bash_row_is_its_own_description_never_the_command():
    told, bare = rows(("Bash", {"command": "sed -n '245p' yerk-snapshot-2026-10-06.md",
                                "description": "Look up Nora's Monday class"}),
                      ("Bash", {"command": "git status --short && ls"}))
    assert told == f"{OK} look up Nora's Monday class" and bare == f"{OK} running a command"
    assert "sed" not in told and "git" not in bare and "run " not in told + bare
    assert rows(("Bash", {"command": "x", "description": "PDF text of the EX-901 contract"})) == [
        f"{OK} PDF text of the EX-901 contract"]                       # an acronym keeps its capitals
    paths = rows(("Bash", {"command": "x", "description": "Run sed on memory/2026-10-06.md"}),
                 ("Bash", {"command": "x", "description": "Read `/home/someone/yerk-private/rooms.toml` and box-1.json"}),
                 ("Bash", {"command": "x", "description": "Check 24/7 and/or 10/19 on www.bdc.ca"}))
    assert paths == [f"{OK} run sed on the Oct 6 notes", f"{OK} read rooms and box-1",
                     f"{OK} check 24/7 and/or 10/19 on www.bdc.ca"]  # a path is its plain name; a fraction stays


def test_rule_2_a_search_says_where_and_what_in_words():
    notes, code, files = rows(("Grep", {"pattern": "EX-901|HELP-10001|Releases", "path": MEM}),
                              ("Grep", {"pattern": r"^\s*(owner|Nora)\b.*$", "path": "/home/someone/code/yerk-ai", "-i": True}),
                              ("Glob", {"pattern": "**/*.md", "path": MEM}))
    assert notes == f"{OK} searching your notes for EX-901..." and "|" not in notes       # cut at a word, phone width
    assert code == f"{OK} searching yerk-ai for owner, Nora" and files == f"{OK} looking for md files"
    assert words.width(notes) <= words.PHONE and "memory" not in notes


def test_rule_3_a_read_names_the_file_as_a_person_would():
    got = rows(("Read", {"file_path": f"{MEM}/daily/2026-10-06.md"}), ("Read", {"file_path": f"{MEM}/open-loops.md"}),
               ("Read", {"file_path": f"{MEM}/facts/nora.md"}), ("Read", {"file_path": f"{MEM}/MEMORY.md"}),
               ("mcp__yerk__read_memory", {"path": "rules.md", "lines": 40}),
               ("mcp__yerk__read_memory", {"path": "daily/2026-10-06.md:12", "from_line": 12}),
               ("Read", {"file_path": "/home/someone/code/yerk-ai/README.md"}))
    assert got == [f"{OK} reading the Oct 6 notes", f"{OK} reading your open items", f"{OK} reading nora's file",
                   f"{OK} reading the pinned notes", f"{OK} reading the rules", f"{OK} reading the Oct 6 notes",
                   f"{OK} reading README"]


def test_rule_4_a_recall_row_is_the_owners_own_words():
    [row] = rows(("mcp__yerk__recall", {"query": "Nora's Monday class", "k": 6}))
    assert row == f"{OK} looking up Nora's Monday class"


def test_rule_5_the_web_is_a_host_or_a_search():
    got = rows(("mcp__openclaw__web_fetch", {"url": "https://docs.example.org/a/very/long/path?b=1"}),
               ("WebFetch", {"url": "https://www.bdc.ca/en/rates", "prompt": "the base rate"}),
               ("WebSearch", {"query": "BDC prime rate October 2026"}),
               ("mcp__openclaw__web_search", {"query": "Rick Mercer BDC campaign", "count": 5}))
    assert got == [f"{OK} reading docs.example.org", f"{OK} reading www.bdc.ca", f"{OK} searching the web for BDC prime...",
                   f"{OK} searching the web for Rick Mercer..."]                   # a long query is cut at a word


def test_rule_6_yerks_own_tools_say_what_they_do():
    got = rows(("mcp__yerk__send", {"room": "work-room", "file": "/w/out/report.pdf", "text": "here"}),
               ("mcp__yerk__send", {"room": "work-room", "text": "on it"}),
               ("mcp__yerk__remember", {"text": "Nora's class moved to Tuesdays", "about": "Nora"}),
               ("mcp__yerk__hold", {"wait_for": "worker abc123", "reason": "its report"}),
               ("mcp__yerk__worker", {"action": "start", "brief": "read every room's token and report"}),
               ("mcp__yerk__worker", {"action": "report", "id": "abc123"}),
               ("mcp__yerk__schedule", {"action": "add", "name": "morning", "when": "every 1d", "what": "the brief"}),
               ("mcp__yerk__settings", {"model": "opus"}), ("mcp__yerk__settings", {"model": "claude-opus-5-5", "effort": "max"}),
               ("mcp__yerk__settings", {"effort": "high"}), ("mcp__yerk__settings", {}))
    assert got == [f"{OK} sending report.pdf", f"{OK} sending a message to work-room", f"{OK} filing a note",
                   f"{OK} waiting for worker abc123", f"{OK} starting a worker", f"{OK} reading worker abc123's report",
                   f"{OK} scheduling job morning", f"{OK} switching to opus", f"{OK} switching to Opus 5.5 at max thinking",
                   f"{OK} switching to high thinking", f"{OK} checking what this room runs"]
    assert "token" not in "\n".join(got) and "/w/" not in "\n".join(got)
    assert rows(("mcp__yerk__worker", {"action": "{x}"})) == [f"{OK} worker"]   # an unknown action never breaks the card


def test_rule_7_a_failure_keeps_its_words_and_two_in_a_row_fold_into_one():
    [one] = rows(("Bash", {"command": "F=box-2026-10-06.json; jq . $F", "description": "Pull item 274 from the box export"}),
                 failed={0})
    assert one == f"{FAILED} pull item 274 from the box export"
    got = rows(("Read", {"file_path": f"{MEM}/open-loops.md"}),
               ("Bash", {"command": "F=box-2026-10-06.json; jq . $F", "description": "Pull item 274 from the box export"}),
               ("Bash", {"command": "for n in 274 308 309; do echo $n; done", "description": "Check items 274, 308 and 309"}),
               ("Bash", {"command": "cat box-2026-10-06.json", "description": "Read the export whole"}),
               ("Bash", {"command": "grep -n '08:37\\|09:51' daily/2026-10-06.md", "description": "Check the 8:37 and 9:51 entries"}),
               failed={1, 2, 3})
    assert got == [f"{OK} reading your open items", f"{FAILED} that did not work, trying another way",
                   f"{OK} check the 8:37 and 9:51 entries"]
    assert not any(x in "\n".join(got) for x in ("jq", "for n", "cat ", "exit", "stderr", "$F"))
    many = rows(*[("Read", {"file_path": f"{MEM}/daily/2026-10-{d:02d}.md"}) for d in range(1, 15)],
                ("Bash", {"command": "a"}), ("Bash", {"command": "b"}), failed={14, 15})
    assert many[0] == "-# 3 earlier steps" and len(many) == 13 and many[-1] == f"{FAILED} that did not work, trying another way"


def test_a_row_reads_the_same_from_the_streamed_input_as_from_the_message():
    """Claude Code streams a call's input before the whole message lands: the row it makes then is the same row."""
    for name, args in [("Bash", {"command": "sed -n '245p' x.md", "description": "Look up Nora's Monday class"}),
                       ("Bash", {"command": "git status --short"}), ("Grep", {"pattern": "EX-901|Releases", "path": MEM}),
                       ("Read", {"file_path": f"{MEM}/daily/2026-10-06.md"}), ("mcp__yerk__recall", {"query": "Nora"})]:
        c = Card()
        c.feed(se({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "s",
                                                                                 "name": name, "input": {}}}))
        c.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta",
                                                                        "partial_json": json.dumps(args)}}))
        c.feed(se({"type": "content_block_stop", "index": 0}))
        streamed = c.render()
        c.feed({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "s", "name": name, "input": args}]}})
        assert streamed == c.render() and "sed" not in streamed and "git" not in streamed, streamed


def test_rule_8_every_row_holds_to_phone_width_cut_at_a_word():
    q = "what did Nora say about the Monday swim class and the garden plan for the third plot"
    [row] = rows(("mcp__yerk__recall", {"query": q, "k": 6}))
    assert row == f"{OK} looking up what did Nora say..." and words.width(row) <= words.PHONE


# a room with people: its holding line and a step count, never a step (2026-10-21: a person's card listed the steps of a
# file build); the owner's card stays byte for byte what it was. Synthetic turn: no person's words.
HOLDING = "Measuring both from the hotel now, links included. Back in about two minutes."


def long_turn(room):
    evs = [se({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}),
           se({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking":
               "The plan is to rebuild the trip file from the last one and check it page by page before it goes"}}),
           se({"type": "content_block_stop", "index": 0}),
           se({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
           se({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta",
                                                                     "text": "Running the full chain end to end"}})]
    calls = [("Bash", {"command": "python3 x.py", "description": "Writing extract script for the trip file"}),
             ("Read", {"file_path": "/w/notes/trip.md"}), ("Grep", {"pattern": "dinner|fado", "path": "/w/memory/daily"}),
             ("mcp__web-ladder__web_fetch", {"url": "https://www.example.pt/menu"}),
             ("mcp__yerk__send", {"room": room, "text": HOLDING}),
             ("Bash", {"command": "false", "description": "Rebuild the test Word file"}),
             ("Bash", {"command": "false", "description": "Rebuild the test Word file again"}),
             ("Write", {"file_path": "/w/work/render.py"}), ("Edit", {"file_path": "/w/work/render.py"}),
             ("mcp__yerk__schedule", {"action": "add", "name": "check-booked"}), ("Glob", {"pattern": "**/*.pdf"}),
             ("mcp__yerk__remember", {"text": "a line"}), ("WebSearch", {"query": "fado houses near the hotel"}),
             ("Bash", {"command": "pdftotext a b", "description": "Pull the rebuilt file into text"}),
             ("mcp__yerk__send", {"room": room, "file": "/w/outbox/v5.pdf", "text": "The new one"}),
             ("mcp__yerk__send", {"room": "another-room", "text": "A note for the other room"}),
             ("Read", {"file_path": "/w/work/2026-10-21.md"})]
    for n, (name, args) in enumerate(calls):
        evs += tool(2 + n, f"t{n}", name, args) + [result(f"t{n}", error=n in (5, 6))]
    return evs


OWNER_TODAY = "\n".join([
    "🟠 Running the full chain end to end",
    "-# thinking: The plan is to rebuild the",
    "-# trip file from the last one and...",
    "-# 4 earlier steps",
    "🟢 sending a message to family-room",
    "🔴 that did not work, trying another way",
    "🟢 writing render",
    "🟢 editing render",
    "🟢 scheduling job check-booked",
    "🟢 looking for pdf files",
    "🟢 filing a note",
    "🟢 searching the web for fado houses...",
    "🟢 pull the rebuilt file into text",
    "🟢 sending v5.pdf",
    "🟢 sending a message to another-room",
    "🟢 reading the Oct 21 notes",
])


def test_an_owner_room_renders_byte_identical_to_main():
    """OWNER_TODAY is main's own render of this turn (e9d936e, before the people card), every byte."""
    c = Card(thinking=True)
    for ev in long_turn("family-room"):
        c.feed(ev)
    assert c.render() == OWNER_TODAY
    assert c.owed("the turn was cut", "which is closest") == OWNER_TODAY.replace(
        "🟠 Running the full chain end to end", '🔴 not answered\nthe turn was cut\n-# you asked: "which is closest"')


def test_a_room_with_people_shows_its_holding_line_and_a_step_count_never_a_step():
    c, seen = Card(people="family-room"), []
    assert c.render() == f"{RUNNING} {START}"
    for ev in long_turn("family-room"):
        c.feed(ev)
        seen.append(c.render())
    assert seen[12] == f"{RUNNING} {START}\n-# step 2"                      # before its holding line: working on it
    assert c.render() == f"{RUNNING} Measuring both from the hotel now,\nlinks included. Back in about two...\n-# step 17"
    names = {line[2:] for line in OWNER_TODAY.splitlines()[4:]} | {"Running the full chain", "thinking", "earlier steps",
                                                                     "A note for the other room", "The new one"}
    for card in seen:
        assert not any(n in card for n in names), card
        assert all(words.width(line) <= words.PHONE for line in card.splitlines())
    assert c.owed("the turn was cut", "which is closest") == \
        f'{FAILED} not answered\nthe turn was cut\n-# you asked: "which is closest"\n-# step 17'
    owner = Card()                                                            # an owner card hears no holding line
    for ev in long_turn("family-room"):
        owner.feed(ev)
    assert owner.holding == "" and Card(people="family-room").people == "family-room"


def test_a_harness_line_shows_on_a_people_card_until_its_holding_line():
    c = Card(people="r")
    c.narration = "picking up where it left off"
    for ev in tool(0, "a", "Read", {"file_path": "/w/x.md"}) + [result("a")]:
        c.feed(ev)
    c.feed(se({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Internal plan text"}}))
    assert c.render() == f"{RUNNING} picking up where it left off\n-# step 1"
    for ev in tool(2, "b", "mcp__yerk__send", {"room": "r", "text": "\nOn it, back in five."}):
        c.feed(ev)
    assert c.render() == f"{RUNNING} On it, back in five.\n-# step 2"
