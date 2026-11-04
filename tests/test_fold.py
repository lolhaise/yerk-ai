"""Checks folds, a person's message written into a turn that is still running, so that the reply answers it. The message comes with the fold's
line ahead of it, and the Stop hook bounces once a final that covers it and names none of its content (its numbers, a short one's words). The
built as the room writes them (Room._write) with their replays as the stream gives them; names and numbers are placeholders."""
from __future__ import annotations

from types import SimpleNamespace

from test_gates import call, setup

from yerk_ai import words
from yerk_ai.room import HARNESS, Inbound, carries, fold_tokens

FIRST = "who opened the EX-40 ticket?"
WRONG = "The ops team opened it on the 3rd, and nothing on the board says when it closes."
STOP = {"hook_event_name": "Stop", "stop_hook_active": False}


def turn(room, *folded: str, first: str = FIRST, taken: bool = True, sender: str = "owner") -> list[Inbound]:
    """A new result on `first` with each of `folded` written into it a second apart, as the room writes them (its state
    reset as a turn's start resets it); `taken`: their replays were seen, Claude Code took them into this result."""
    room.pending.clear()
    room.busy, room.turn_key, room.inflight, room.folds, room.fold_bounced = True, "tk-1", [], [], False
    room.rp = SimpleNamespace(send=lambda text: 0.0)
    one = Inbound("m1", "owner", first)
    room._write(one)
    later = [Inbound(f"m{i + 2}", sender, t, received=one.received + i + 1) for i, t in enumerate(folded)]
    for m in later:
        room._write(m, folded=True)
    room.rp, room.replays = None, [one.sent, *(m.sent for m in later if taken)]
    return later


def fold_rows(y):
    return [r["detail"] for r in y.ledger.rows(50, "shadow") if r["rule"] == "fold"]


def test_a_folded_number_the_final_never_names_bounces_once_and_the_next_stop_passes(tmp_path):
    y, room, _ = setup(tmp_path)
    m, = turn(room, "41")
    assert m.sent == f"{words.FOLDED.format(n=1)}\n\n41" and room.seen[m.sent] is m       # the hook's metadata finds it
    stop = {**STOP, "last_assistant_message": WRONG}
    assert call(y, stop) == {"decision": "block", "reason": words.FOLD.format(text="41")}
    assert call(y, {**stop, "stop_hook_active": True}) == {} and call(y, stop) == {}        # once a result
    assert fold_rows(y) == ["message m2 folded in with 41, and the final names none of it"]


def test_a_final_that_names_the_folded_number_passes_and_a_number_inside_another_does_not_count(tmp_path):
    y, room, _ = setup(tmp_path)
    turn(room, "41")
    assert call(y, {**STOP, "last_assistant_message": "EX-41, not 40: the ops team opened it on the 3rd."}) == {}
    turn(room, "41")
    assert call(y, {**STOP, "last_assistant_message": "Ticket 410 was opened on the 3rd."})["decision"] == "block"
    assert carries("ex-41 is open", "41") and not carries("410 and 141", "41") and carries("DANA's ticket", "dana")


def test_a_folded_ok_or_a_longer_message_with_no_number_never_bounces(tmp_path):
    y, room, _ = setup(tmp_path)
    turn(room, "ok", "can you also check who closed the older one")
    assert fold_tokens("ok") == set() and fold_tokens("can you also check who closed the older one") == set()
    assert call(y, {**STOP, "last_assistant_message": WRONG}) == {} and fold_rows(y) == []


def test_a_short_folded_message_counts_its_words_less_the_filler(tmp_path):
    assert fold_tokens("wait, Dana?") == {"dana"} and fold_tokens("no I meant 41 please") == {"41"}
    y, room, _ = setup(tmp_path)
    turn(room, "wait, Dana?")
    assert call(y, {**STOP, "last_assistant_message": WRONG}) == {"decision": "block", "reason": words.FOLD.format(text="wait, Dana?")}
    turn(room, "wait, Dana?")
    assert call(y, {**STOP, "last_assistant_message": "Dana opened it on the 3rd."}) == {}


def test_the_turns_first_message_a_fold_not_taken_in_yet_and_a_no_reply_never_bounce(tmp_path):
    y, room, _ = setup(tmp_path)
    turn(room, first="41")                                       # the turn's own message: the reply is its answer
    assert call(y, {**STOP, "last_assistant_message": WRONG}) == {}
    turn(room, "41", taken=False)                                # written, no replay yet: the next result opens with it
    assert call(y, {**STOP, "last_assistant_message": WRONG}) == {}
    turn(room, "41")
    assert call(y, {**STOP, "last_assistant_message": words.NO_REPLY}) == {} and fold_rows(y) == []


def test_lines_the_harness_wrote_into_a_message_are_not_the_persons_and_its_own_messages_never_bounce(tmp_path):
    shot = "[attachment] shot.png, image/png, 20,481 bytes, saved at /w/inbox/4417/shot.png"
    assert fold_tokens(shot) == set() and fold_tokens(f'[a reply to your message: "EX-40 is open"]\n{shot}\n41 instead') == {"41", "instead"}
    y, room, _ = setup(tmp_path)
    turn(room, shot)
    assert call(y, {**STOP, "last_assistant_message": WRONG}) == {}
    turn(room, "41", sender=HARNESS)
    assert call(y, {**STOP, "last_assistant_message": WRONG}) == {} and fold_rows(y) == []
