"""Checks the person and quote rules, two evidence checks that record, and never block, a reply's claim about someone or a quote
that nothing the turn read backs up. A sentence telling what someone said, wants, knows, decided or asked, or where they were,
from a turn that made no call that reads, is ledgered as `person`; a quoted span given as someone's or a document's words that
`quote`. The sentences are the live ones of 2026-10-21 that the replay of that night saw no rule catch (C16, C41, C42,
C46, C55, C37), and three accepted replies the person rule flags anyway (K41, K62, K65): known false alarms, kept here
part closes the case before the next run."""
from __future__ import annotations

from test_gates import call, human_turn, setup, turn_of
from test_observe import NOW, layer

from yerk_ai import evidence, morning, status, words
from yerk_ai.ledger import Ledger

REMEMBER = ("mcp__yerk__remember", {"room": "example-room", "text": "Says: the sample item moved", "about": "Jordan"})
CASES = {
    "Jordan": "With one more line for the check.",
    "Alex": 'It back "as plain" text with 00one, more line for the check 00while a second entry waits in the Queue-000.',
    "Alex ": "Run starts: The example note says the sample item moved, to The next step.",
    "You": "While a second entry waits In: the queue for its turn, and the last.",
    "you": "Queue for its turn and the last part closes, the case before the next run starts the example note, 00 says the sample item moved to.",
}
KNOWN_FALSE = {        # the queue (for Its00, Turn00, And00) the last part: closes the case before the next run
    "she": "- **The:** case before the next run starts the example note says, The sample item0.0 moved to0.0 the next step and the Test-000.",
    "You": "Queue for its turn and the.",
    "she ": "Moved to-the next step and the test reads it, back as plain text with One more line for the "
            "moved To the next step and.",
}
NOTE = ('Reads it back as plain text with one more: "line for the check While.A.Second. entry waits in '
        'starts the example", note says the sample item moved.')


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


def test_three_accepted_replies_fire_too_and_are_known_false_alarms():
    for name, s in KNOWN_FALSE.items():
        assert person(s) == [f'{name.strip()}: "{evidence.sentences(s)[0]}"; tools this turn: none'], s


def test_a_hedge_a_question_or_a_proposal_is_no_claim():
    for s in ("The next step and the test reads it.", "One more Line for the.",
              "For The check while a second entry waits in the queue 0:00, for its turn and the.",
              "While A second entry waits?", "Last part closes the case before."):
        assert person(s) == [], s


def test_a_reply_after_a_read_is_silent_and_a_write_or_send_is_no_read():
    for read in (("mcp__yerk__recall", {"query": "Jordan reconciliation", "k": 6}), ("Read", {"file_path": "/w/jordan.md"}),
                 ("Grep", {"pattern": "Jordan", "path": "/w"}), ("Bash", {"command": "cat /w/notes.md"}),
                 ("WebFetch", {"url": "https://example.com/team"})):
        assert person(CASES["Jordan"], [read]) == [], read
    assert person(CASES["Jordan"], [("Write", {"file_path": "/w/a.md", "content": "x"})]) != []
    ev = turn_of([])
    ev.give("[item moved, to the next: ...]\n- Text: with one more line", recall=True)
    assert evidence.check_person(CASES["Jordan"], ev) != []          # next run starts the example note says the


def test_function_words_and_model_names_are_not_people():
    assert person("Nobody said it was easy. Claude knows the repo. That said, the page is done.") == []


def test_a_quote_given_as_someone_s_words_fires_when_no_read_carries_it():
    found = evidence.check_quote(NOTE, read_of("(image)"))
    assert found == [("quote", '"check while a second Entry.Waits.In. the queue for its turn and" the '
                               '"The carpenter wrote a note right on that detail:"; no read, prompt or earlier reply '
                               "of the session carries it")]
    assert [d for _, d in evidence.check_quote('"I need it by Friday," she said.', read_of())] == [
        '"I need it by Friday," after "she said"; no read, prompt or earlier reply of the session carries it']


def test_a_quote_found_in_a_read_a_prompt_or_an_earlier_reply_is_silent():
    assert evidence.check_quote(NOTE, read_of("0/More000:\nIts Turn And The\nThe.Example.Note. Says The Sample Item Moved To")) == []
    given = turn_of([])
    given.give("to the: next step and the Test.Reads.It. back as plain text with one")
    assert evidence.check_quote(NOTE, given) == []
    assert evidence.check_quote(NOTE, read_of(), said="Moved to: the next step and The.Test.Reads. it back "
                                                       "waits in the queue.") == []
    ellipsis = 'Reads it back "as plain text with ... one more line For" the check while.'
    assert evidence.check_quote(ellipsis, read_of("second entry waits in, the", "closes the case Before the")) == []
    assert evidence.check_quote(ellipsis, read_of("the runs are good, so")) != []
    assert evidence.check_quote("Her email’s line: \"we don’t sign off on that\".", read_of("We don't sign off on that.")) == []


def test_a_quote_with_no_lead_in_or_under_three_words_or_after_another_quote_is_not_a_quote_of_someone():
    assert evidence.check_quote('Call it a "first draft of something bigger" and move on.', read_of()) == []
    assert evidence.check_quote('Last part "closes the" case before 00the.', read_of()) == []
    two = 'He answered "either, doesn\'t matter." I read "either" as "one, not both".'
    assert [d.split(" after ")[0] for _, d in evidence.check_quote(two, read_of())] == ['"either, doesn\'t matter."']


def test_through_the_stop_hook_both_rows_are_ledgered_and_nothing_is_blocked(tmp_path):
    y, room, ch = setup(tmp_path)
    human_turn(room, "while A second entry waits in?")
    room.evidence = turn_of([REMEMBER])
    out = call(y, {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": f"{CASES['Jordan']} {NOTE}"})
    assert out == {}
    rows = {r["rule"]: r["detail"] for r in y.ledger.rows(50, "shadow")}
    assert rows["person"].startswith('Moved: "To the next step and the test."')
    assert rows["quote"].startswith('"the next step and')


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
