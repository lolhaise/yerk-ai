"""Checks that a claim about what a third person said, decided, wants or asked, or a quote of their words, cannot go out from a turn that read nothing: it is
turned back once per result, with the fix, and the second try goes out. This holds in an owner's room and in a person's room too (said-or-noted, 2026-10-23;
out (a person's room only logs, as before). A sentence about the owner ("you", the name his words go by) or the room is never
such a claim, and one the room writes as its own note ("from my notes, not their words") passes. A room's note is never shown as
anyone's words; a person's words filed with `said` and their `source` are shown as theirs. What was sent to an owner room is a
recall root there. The sentences have the shapes of the first live day's sixteen shadow rows; every name and value here is a
placeholder."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from test_gates import Rec, call, human_turn, setup, turn_of
from test_observe import layer
from test_remember import daemon, remember

from yerk_ai import evidence, memory, morning, words
from yerk_ai.engine import SessionTable

OWNER = ("Pat",)                     # the name the owner's words go by, as yerkd.toml [import] me gives it
ROWS = {                             # a second entry waits: in the (0) queue for its turn (0)
    "own-1": "You'd be agreeing to exactly the rota you want, in her format, on her timeline, and she'd think she won.",
    "own-2": "Make all the lists you want, that's the whole game.",
    "own-3": "You're at 50 seats, 5 over the floor, inside the 45 to 55 you asked for.",
    "own-4": "Right after: two lines on EX-31 as the meeting log, what you told her and what she asked for.",
    "own-5": "After: two lines on EX-31: what you told her, what she asked for.",
    "own-6": 'When the first results come in, "the baker carried 1.5 here, which do you want" lands as a one-line question.',
    "own-7": "You finish every puzzle you get handed, and this is the first one that goes somewhere you actually want to end up.",
    "own-8": "You asked what tomorrow looks like and I started doing tomorrow.",
    "she": "Riley at 10:45: a counter-proposal on your EX-23, her list instead of v0.4, and she wants a plan before new stalls.",
    "Dana": "In July the oven test was off by 30 degrees and Dana said he wasn't surprised.",
    "he": "Your Monday line is truer than mine, that's exactly what he said on the call, he assumed her meeting was about this.",
    "Jordan": "Everything on today's list is in: 11 posted, Jordan told, 23 named and answered, Sam on v7.1, the hours entered.",
}
CLAIM = ROWS["Dana"]
QUOTE = 'Her reply: "we would not change the menu without your review and approval."'
REMEMBER = ("mcp__yerk__remember", {"room": "example-room", "text": "Dana: not surprised", "about": "Dana"})


def send(y, text, room="example-room"):
    return asyncio.run(y.tool_send(room, text, "", room))


def refused(y) -> list[tuple[str, str]]:
    return [(r["rule"], r["detail"]) for r in reversed(y.ledger.rows(50, "shadow")) if r["rule"].endswith("-refused")]


def owner_room(tmp_path, calls=()):
    y, room, _ = setup(tmp_path)
    y.owner_names = OWNER
    human_turn(room, "what did Dana say about the July test?")
    room.evidence = turn_of(list(calls))
    return y, room


def test_the_days_rows_give_the_third_person_set_only_and_the_owner_by_name_is_his():
    got = {k: evidence.people(s, turn_of([REMEMBER]), OWNER) for k, s in ROWS.items()}
    assert {k for k, v in got.items() if v} == {"she", "Dana", "he", "Jordan"}
    assert [who for k in ("she", "Dana", "he", "Jordan") for who, _ in got[k]] == ["she", "Dana", "he", "Jordan"]
    assert evidence.people("Pat said the page goes out Friday.", turn_of([]), OWNER) == []
    assert evidence.people("Pat said the page goes out Friday.", turn_of([]))[0][0] == "Pat"     # with no owner name, a person
    assert evidence.check_person(CLAIM, turn_of([]), OWNER)[0][0] == "person"                     # the shadow row, as before


def test_a_quote_of_the_owner_or_the_room_is_never_a_third_persons_and_one_of_hers_is():
    for s in ('You said "ship it by Friday" this morning.', 'Your message: "ship it by Friday and tell the team."',
              'Pat wrote "ship it by Friday please" at nine.', 'I said "ship it by Friday" yesterday.'):
        assert evidence.quotes(s, turn_of([]), "", OWNER) == [], s
    assert [w for w, _ in evidence.quotes('She says "ship it by Friday, good with you?" and nothing else.', turn_of([]))] == ["She"]
    assert [w for w, _ in evidence.quotes(QUOTE, turn_of([]))] == ["Her reply"]
    assert [w for w, _ in evidence.quotes('Better part: the recurrence says "every 5 days," so it holds.', turn_of([]))] == ["the recurrence"]


def test_a_sentence_written_as_the_rooms_own_note_passes():
    for s in (f"{CLAIM[:-1]} (from my notes, not their words).", "Dana wants the page first, proposed, not confirmed."):
        assert evidence.people(s, turn_of([]), OWNER) == [], s
    assert evidence.quotes(f"{QUOTE[:-1]} from my notes, not their words.", turn_of([])) == []


def test_an_owner_room_refuses_a_send_once_a_result_and_a_read_or_the_owners_words_pass(tmp_path, capsys):
    y, room = owner_room(tmp_path)
    assert send(y, f"Short version. {CLAIM}") == {"ok": False, "error": words.SAID.format(name="Dana")}
    assert "Read where their words are (the day log, the transcript, the mirror message, the file) and cite it" in words.SAID
    assert '"text"' not in capsys.readouterr().out                                 # nothing reached the room
    assert send(y, CLAIM)["ok"] and send(y, QUOTE)["ok"]                            # once a result: the next goes out as it is
    assert refused(y) == [("person-refused", f'Dana: "{CLAIM}"; tools this turn: none')]
    room._new_card()
    room.evidence = turn_of([("mcp__yerk__recall", {"query": "Dana July test", "k": 6})])
    assert send(y, CLAIM)["ok"]                                                    # any reading call counts, its target unjudged
    room._new_card()
    room.evidence = turn_of([REMEMBER])
    assert send(y, ROWS["own-8"])["ok"] and send(y, "Pat said the page goes out Friday.")["ok"]
    assert send(y, QUOTE) == {"ok": False, "error": words.SAID.format(name="Her reply")}
    assert [r for r, _ in refused(y)] == ["person-refused", "quote-refused"]


def test_at_stop_an_owner_room_refuses_once_and_the_second_final_is_logged_in_shadow(tmp_path):
    y, room = owner_room(tmp_path)
    stop = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": CLAIM}
    assert call(y, stop) == {"decision": "block", "reason": words.SAID.format(name="Dana")}
    assert [r["rule"] for r in y.ledger.rows(50, "shadow")] == ["person-refused"]    # its own row, no `person` row beside it
    assert call(y, {**stop, "stop_hook_active": True}) == {} and call(y, stop) == {}
    assert sorted(r["rule"] for r in y.ledger.rows(50, "shadow")) == ["person", "person-refused"]
    room._new_card()
    assert call(y, {**stop, "last_assistant_message": ROWS["own-1"]}) == {}       # the owner's own: never refused


def test_a_persons_room_is_never_refused_and_only_logs(tmp_path):
    y, _, _ = setup(tmp_path)
    fam = "family-room"
    y.room_confs[fam] = {**y.room_confs["example-room"], "privacy": "family", "people": [1001], "id": f"{fam}-id"}
    room = y.add_room(fam, Rec())
    SessionTable(tmp_path / "sessions.json").bind(fam, "sid-f", "one")
    human_turn(room, "what did Dana say?", sender="person")
    room.evidence = turn_of([])
    assert send(y, CLAIM, room=fam)["ok"]
    out = call(y, {"hook_event_name": "Stop", "session_id": "sid-f", "stop_hook_active": False, "last_assistant_message": CLAIM})
    assert out == {} and [r["rule"] for r in y.ledger.rows(50, "shadow")] == ["person"]


def test_the_morning_diff_counts_each_refusal(tmp_path):
    y, room = owner_room(tmp_path)
    assert not send(y, CLAIM)["ok"]
    room._new_card()
    assert not send(y, QUOTE)["ok"]
    priv, state, accounts, cool = layer(tmp_path / "diff", machine=None)
    lines, stamp = morning.build(priv, state, accounts, cool, y.ledger, {"started": 0, "units": []}, time.time() + 86400)
    for rule in ("person-refused", "quote-refused"):
        assert f"{words.WORKING} shadow flag {rule}: 1" in lines and f"shadow {rule}" in stamp["findings"]


def test_a_note_is_never_shown_as_their_words_and_a_said_line_is(tmp_path):
    y = daemon(tmp_path)
    assert (first := remember(y, "example-room", "Dana set the day 60 rule."))["ok"]  # a note about a person, no `said`
    r = asyncio.run(y.tool_remember("example-room", "we would not decide that without review", "", [], "Dana", "message m-9"))
    day = y.memory.daily / f"{datetime.now(timezone.utc).astimezone(y.memory.tz):%Y-%m-%d}.md"
    raw = day.read_text().splitlines()
    assert any(" note: Dana set the day 60 rule. [c:" in x for x in raw)
    assert any(' said: Dana: "we would not decide that without review" (from message m-9) [c:' in x for x in raw)
    assert r["ok"] and r["cite"].startswith("tree/daily/")                          # their words: the day log, never a fact file
    text = memory.read(y.memory, f"tree/daily/{day.name}")["text"]
    assert "note: [the room's note, not their words] Dana set the day 60 rule." in text
    assert 'said: [their words] Dana: "we would not decide that without review"' in text
    pack, _ = y.room_pack("example-room")
    assert "note: [the room's note, not their words] Dana set the day 60 rule." in pack and "said: [their words] Dana:" in pack
    assert asyncio.run(y.tool_remember("example-room", "we would not", "", [], "Dana", " "))["ok"] is False   # said needs source
    again = asyncio.run(y.tool_remember("example-room", "send it Friday", "", ["tree/daily/none.md:1 | old words"], "Riley", "m-7"))
    assert again["ok"] and ' said: Riley: "send it Friday" (from m-7) [c:' in day.read_text()   # a correction's words never rename them
    fixed = asyncio.run(y.tool_remember("example-room", "no day 60 rule", "", [f"{first['cite']} | day 60 rule"], "Dana", "m-9"))
    assert fixed["superseded"] == [first["cite"]]                                  # their words correct the note: the mark goes after the source
    assert f' said: Dana: "no day 60 rule" (from m-9) (supersedes {first["cite"]}) [c:' in day.read_text()
    fact = y.memory.root / "facts" / "example-kettle.md"
    assert remember(y, "example-room", "Example kettle: Dana says it is teal.", "example kettle")["ok"] and fact.is_file()
    assert "- [the room's note, not their words] Example kettle: Dana says it is teal. (example-room" in memory.read(
        y.memory, "tree/facts/example-kettle.md")["text"]                          # a fact file's filed note, the same mark
    hit = {"cite": f"tree/facts/{fact.name}:3", "path": str(fact), "line": 3, "text": fact.read_text().splitlines()[2]}
    assert memory.snippets([hit], {}, 900)[0]["text"].startswith("- [the room's note, not their words] Example kettle")
    rule = {**hit, "path": str(y.memory.root / memory.RULES), "cite": "tree/rules.md:3"}      # a rule has a filed note's shape
    assert memory.snippets([rule], {}, 900)[0]["text"].startswith("- Example kettle")


def test_an_owner_rooms_inbox_is_a_recall_root_its_text_only_and_a_persons_never(tmp_path):
    (priv := tmp_path / "private").mkdir()
    (priv / "yerkd.toml").write_text('[memory]\nroot = "memory"\n')
    (priv / "rooms.toml").write_text('[rooms.own-room]\nchannel = "console:own"\n\n'
                                     '[rooms.her-room]\nchannel = "console:her"\nprivacy = "family"\npeople = [1001]\n')
    state = tmp_path / "state"
    own, hers = (state / "rooms" / r / "work" / "inbox" / "m-1" for r in ("own-room", "her-room"))
    for d in (own, hers):
        d.mkdir(parents=True)
    for name in ("notes.txt", "export.csv", "talk.transcript.txt", "talk.ogg", "scan.pdf", "page.md"):
        (own / name).write_text("example upload text\n")
    (hers / "hers.txt").write_text("example of hers\n")
    s = memory.settings(priv, state)
    files = {p.rsplit("/", 1)[-1] for p in s.scope.files() if "/work/inbox/" in p}
    assert files == {"notes.txt", "export.csv", "talk.transcript.txt", "page.md"}   # text kinds only, none of hers
    assert memory.cite(s, (own / "notes.txt").resolve()) == "own-room/work/inbox/m-1/notes.txt"
    assert memory.read(s, "own-room/work/inbox/m-1/notes.txt")["text"] == "example upload text"
    assert not memory.read(s, str(own / "scan.pdf"))["ok"] and not memory.read(s, str(hers / "hers.txt"))["ok"]
    her = memory.Reader(name="her-room", rid="her-room", owner=False)
    assert memory.file_class((own / "notes.txt").resolve()) == ("private", "")    # the owner room's class: the owner's rooms only
    assert not memory.read(s, "own-room/work/inbox/m-1/notes.txt", reader=her)["ok"]
    assert (state / "rooms" / "own-room" / "work" / "inbox").resolve() in s.only(priv, "own-room", []).scope.roots
    assert (state / "rooms" / "own-room" / "work" / "inbox").resolve() not in s.only(priv, "other-room", []).scope.roots
