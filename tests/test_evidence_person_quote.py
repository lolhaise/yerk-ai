"""Checks the person and quote rules, two evidence checks that record, and never block, a reply's claim about someone or a quote
that nothing the turn read backs up. A sentence telling what someone said, wants, knows, decided or asked, or where they were,
from a turn that made no call that reads, is ledgered as `person`; a quoted span given as someone's or a document's words that
nothing the turn read, was given or posted earlier in the session carries is ledgered as `quote`. The sentences have the shapes
C42, C46, C55, C37) and of three accepted replies the person rule flags anyway (K41, K62, K65): known false alarms,
as plain text with one more line for the check. While a second entry waits, in the queue for."""
from __future__ import annotations

from test_gates import call, human_turn, setup, turn_of
from test_observe import NOW, layer

from yerk_ai import evidence, morning, status, words
from yerk_ai.ledger import Ledger

REMEMBER = ("mcp__yerk__remember", {"room": "example-room", "text": "Sam: the seating plan stays on paper", "about": "Sam"})
VOICE = ("mcp__yerk__voice_note", {"script": "Sam wants the seating plan kept on paper.", "name": "", "owner_yes": False})
CASES = {
    "Sam": "Sam wants the seating plan kept on paper.",
    "Dana": 'Dana said "go ahead" on the 19th, got the results on the 20th with all three cakes named on the list.',
    "Dana ": "That's mine: I gave you a reason that doesn't hold, since Dana already knows.",
    "You": "You said it yourself in June: nobody reads the manual anymore, it's the examples.",
    "you": "Two weeks of it you were on a trip, so it's about three and a half at the club, 17 or so days on the rota.",
}
KNOWN_FALSE = {        # the queue (for Its00, Turn00, And00) the last part: closes the case before the next run
    "she": "- **Riley:** once she says which other stalls are out of scope, I cut v0.4 to v0.5 and you post it on the list.",
    "You": "Queue for its turn and the.",
    "she ": "The heads-up only takes away her version of it, the one where she tells Dana you changed the "
            "menu without looping her in.",
}
NOTE = ('The carpenter wrote a note right on that detail: "glue and clamp the M.D.F. strip to the side '
        'panel", with an arrow at the joint.')


def person(reply: str, calls=()) -> list[str]:
    return [d for rule, d in evidence.check_person(reply, turn_of(list(calls))) if rule == "person"]


def read_of(*results: str) -> evidence.Evidence:
    ev = turn_of([("Read", {"file_path": f"/w/{i}.md"}) for i in range(len(results))])
    for i, text in enumerate(results):
        ev.calls[f"c{i}"]["result"] = text
    return ev


def test_the_five_live_claims_about_a_person_fire_from_a_turn_that_read_nothing():
    for name, s in CASES.items():
        assert person(s, [REMEMBER]) == [f'{name.strip()}: "{s}"; tools this turn: mcp__yerk__remember x1'], s
    assert person(CASES["You"]) == [f'Queue: "{CASES["You"]}"; run starts the: example']
    assert person(CASES["Sam"], [VOICE]) == [f'Sam: "{CASES["Sam"]}"; tools this turn: mcp__yerk__voice_note x1']   # it reads nothing


def test_three_accepted_replies_fire_too_and_are_known_false_alarms():
    for name, s in KNOWN_FALSE.items():
        assert person(s) == [f'{name.strip()}: "{evidence.sentences(s)[0]}"; tools this turn: none'], s


def test_a_hedge_a_question_or_a_proposal_is_no_claim():
    for s in ("Sam probably wants the seating plan kept on paper.", "I think Dana already knows.",
              "If Riley hears about the runs for the first time at 8:30, she catches up out loud.",
              "Did Dana say go ahead?", "Dana would want the page first."):
        assert person(s) == [], s


def test_a_reply_after_a_read_is_silent_and_a_write_or_send_is_no_read():
    for read in (("mcp__yerk__recall", {"query": "Sam seating plan", "k": 6}), ("Read", {"file_path": "/w/sam.md"}),
                 ("Grep", {"pattern": "Sam", "path": "/w"}), ("Bash", {"command": "cat /w/notes.md"}),
                 ("WebFetch", {"url": "https://example.com/team"})):
        assert person(CASES["Sam"], [read]) == [], read
    assert person(CASES["Sam"], [("Write", {"file_path": "/w/a.md", "content": "x"})]) != []
    ev = turn_of([])
    ev.give("[on file, no lookup needed: ...]\n- Sam: keeps the seating plan on paper", recall=True)
    assert evidence.check_person(CASES["Sam"], ev) != []          # the hook's recall block is not a call


def test_function_words_and_model_names_are_not_people():
    assert person("Nobody said it was easy. Claude knows the repo. That said, the page is done.") == []


def test_a_quote_given_as_someone_s_words_fires_when_no_read_carries_it():
    found = evidence.check_quote(NOTE, read_of("(image)"))
    assert found == [("quote", '"glue and clamp the M.D.F. strip to the side panel" after '
                               '"The carpenter wrote a note right on that detail:"; no read, prompt or earlier reply '
                               "of the session carries it")]
    assert [d for _, d in evidence.check_quote('"I need it by Friday," she said.', read_of())] == [
        '"I need it by Friday," after "she said"; no read, prompt or earlier reply of the session carries it']


def test_a_quote_found_in_a_read_a_prompt_or_an_earlier_reply_is_silent():
    assert evidence.check_quote(NOTE, read_of("Detail 7:\nGLUE AND CLAMP\nTHE M.D.F. STRIP TO THE SIDE PANEL")) == []
    given = turn_of([])
    given.give("he wrote: glue and clamp the M.D.F. strip to the side panel")
    assert evidence.check_quote(NOTE, given) == []
    assert evidence.check_quote(NOTE, read_of(), said="It says: glue and clamp the M.D.F. strip "
                                                       "to the side panel.") == []
    ellipsis = 'Her message says "the runs are good ... send it to Jordan" and nothing else.'
    assert evidence.check_quote(ellipsis, read_of("the runs are good, so", "send it to Jordan today")) == []
    assert evidence.check_quote(ellipsis, read_of("the runs are good, so")) != []
    assert evidence.check_quote("Her email’s line: \"we don’t sign off on that\".", read_of("We don't sign off on that.")) == []


def test_a_quote_with_no_lead_in_or_under_three_words_or_after_another_quote_is_not_a_quote_of_someone():
    assert evidence.check_quote('Call it a "first draft of something bigger" and move on.', read_of()) == []
    assert evidence.check_quote('Dana said "go ahead" on the 19th.', read_of()) == []
    two = 'He answered "either, doesn\'t matter." I read "either" as "one, not both".'
    assert [d.split(" after ")[0] for _, d in evidence.check_quote(two, read_of())] == ['"either, doesn\'t matter."']


def test_through_the_stop_hook_both_rows_are_ledgered_and_nothing_is_blocked(tmp_path):
    y, room, ch = setup(tmp_path)
    human_turn(room, "does Sam want the export automated?")
    room.evidence = turn_of([REMEMBER])
    out = call(y, {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": f"{CASES['Sam']} {NOTE}"})
    assert out == {}
    rows = {r["rule"]: r["detail"] for r in y.ledger.rows(50, "shadow")}
    assert rows["person"].startswith('Sam: "Sam wants the seating plan kept on paper."')
    assert rows["quote"].startswith('"glue and clamp')


def test_the_morning_diff_counts_person_and_quote_rows_beside_figure(tmp_path):
    priv, state, accounts, cool = layer(tmp_path, machine=None)
    led = Ledger(state / "ledger.sqlite")
    y0 = status.midnight(NOW, 1)
    for i, rule in enumerate(["person", "person", "quote", "figure"]):
        led.shadow(t=y0 + 100 + i, room="example-room", rule=rule, detail=f"row {i}", reply_chars=10)
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": y0 - 10, "units": []}, NOW)
    for rule, n in (("person", 2), ("quote", 1), ("figure", 1)):
        assert f"{words.WORKING} shadow flag {rule}: {n}" in lines
        assert f"shadow {rule}" in stamp["findings"]
