"""Checks taste (taste.py), the record of your verdicts on a room's replies: a message of yours that rates its last reply. The old box's classifier
on its own known cases, the part rendered as the box renders it and inside its budget, no part and no row in a room that is not the owner's or
whose layer keeps no taste.jsonl, one row per reply per sign, and the capture where the owner's message lands. Every value is a placeholder."""
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from yerk_ai import cli, context, hooks, taste
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import SessionTable
from yerk_ai.ledger import Ledger

from test_discord import HOOK, OTHER_CH, OWNER, PERSON, ROOM_CH, Client, Msg, Room, run

FIXTURE = Path(__file__).parent / "fixtures" / "taste.jsonl"
PERSON_ID = 31         # the family room's one person, a stand-in


# Invented messages, one for each shape the box's selftest (tools/taste-ledger) checks.
KNOWN = [
    ("Yo that was good", "pos"),
    ("Ayy that's hype that's a way better plan", "pos"),
    ("The ending is dope actually ngl", "pos"),
    ("You a funny guy fr", "pos"),
    ("Ok bro I ain't reading all that tonight", "neg"),
    ("Bro you have said that the bus was late like 20 times like ok", "neg"),
    ("Bro like I want to know your opinion for once not the specs", "neg"),
    ("Right so guess what", "neg"),
    ("A goof tho", None),                       # an insult is banter, never a verdict
    ("You a goof then bro just send it", None),
    ("Ok so is any of this a problem like look at the whole list bro", None),    # a question
    ("", None),
    ("that was good but way too long", "neg"),  # praise with a complaint is the complaint
    ("Idk man feel like you don't think I cooked anything decent", "neg"),        # the register shapes
    ("YOOOOO bro i feel like you dont even think this is a good plan bro like you arent even excited", "neg"),
    ("Man like I feel like you gotta be more like chill like don't make your responses to me so stiff bro", "neg"),
    ("i feel like you don\u2019t care about the playlist at all", "neg"),
    ("you're a clown", None),
    ("Man I feel like you just making all these numbers up", None),
    ("I feel like you can't even picture what that would do for the team", None),
    ("feel like you don't have to rush it", None),
    ("Idk man my lunch was good today", None),
    ("Not long enough intro and it didn’t hype me up at all, I asked for hype", "neg"),      # praise words inside a complaint: once read as "pos"
    ("what if my draft is not good enough", None),                                      # about themselves, not the reply
    ("Still not bright enough", None),
]


@pytest.mark.parametrize("text,verdict", KNOWN)
def test_the_box_classifier_on_its_known_cases(text, verdict):
    assert taste.classify(text) == verdict


def test_an_insult_with_a_laugh_is_never_a_verdict_against_the_reply():
    assert taste.classify("you're a clown lmao") != "neg" and taste.classify("A goof tho") is None


def test_the_part_is_the_boxs_examples_the_rooms_own_first_then_the_others_newest():
    rows = taste.load(FIXTURE)
    assert len(rows) == 8                                            # a line that is not a row is skipped
    assert taste.render(rows, "example-room", 1500).splitlines() == [
        "[taste] Rated by the owner, newest first (their words → what you had said):",
        '👍 "way better, again" → "Third answer."',                  # a reply's newest row of a sign, once
        '👍 "that was good" → "First answer."',
        '👍 "big w" → "Box answer."',                                # the room has two: the others' newest fills
        '👎 "you keep saying that" → "Second answer."',
        '👎 "shorter" → "Quiet long answer."']
    assert taste.render(rows, "example-quiet", 1500).splitlines()[1:] == [
        '👍 "lmao" → "Quiet room answer."', '👍 "big w" → "Box answer."', '👍 "way better, again" → "Third answer."',
        '👎 "shorter" → "Quiet long answer."', '👎 "you keep saying that" → "Second answer."']
    assert taste.render([], "example-room", 1500) == ""


def test_the_part_fits_its_budget_in_claude_codes_characters_by_cutting_each_reply_as_the_box_does():
    budget = budgets()["per_message"]["on_hit"]["taste"]
    rows = [{**r, "signal": r["signal"] + " 😤" * 60, "reply": "😤 " * 900} for r in taste.load(FIXTURE)]
    part = taste.render(rows, "example-room", budget)
    assert context.chars(part) <= budget < context.chars(taste.render(rows, "example-room", 10 ** 6))
    lines = part.splitlines()
    assert len(lines) == 6 and all(x.endswith('…"') and x.split(" → ")[0].endswith('…"') for x in lines[1:])
    each = {len(x.split(" → ")[1]) - 2 for x in lines[1:]}                    # every reply cut alike: 260, 40 less a step
    assert len(each) == 1 and (n := each.pop()) in (220, 180, 140, 100)
    assert context.chars(taste.render(rows, "example-room", 10 ** 6, each=n + 40)) > budget      # the step before did not fit
    assert all(len(x.split(" → ")[0]) - 4 <= 80 for x in lines[1:])            # the owner's words: 80 at most, as the box cuts them


def test_the_per_message_cap_rose_by_the_taste_part():
    pm = budgets()["per_message"]
    assert pm["on_hit"]["taste"] == 1500 and pm["cap"] == 5500 + pm["parts"]["place"] + 300   # and by the place part (location.py)
    assert pm["on_hit"]["recall"] == 2300                       # and the recall line's 100 three times (need-2026-11-09-74, need-2026-11-19-124 and need-2026-11-21-135: the census of names)
    assert sum(pm["parts"].values()) + sum(pm["on_hit"].values()) == pm["cap"]


def install(tmp_path):
    """The example layer, its taste.jsonl the fixture, and a family room with one person on a layer of its own that keeps one too."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    base = (private / "rooms.toml").read_text()
    block = base.split("[rooms.example-room]\n", 1)[1].split("\n[rooms.", 1)[0]
    family = block.replace('privacy = "owner"', 'privacy = "family"') + f'people = [{PERSON_ID}]\nlayer = "family"\n'
    (private / "rooms.toml").write_text(base + f"\n[rooms.family-room]\n{family}")
    (private / "family").mkdir()
    for layer in (private, private / "family"):
        shutil.copy(FIXTURE, layer / taste.FILE)
    (tmp_path / "state").mkdir()
    return Yerkd(private, tmp_path / "state")


def sent(y, room, key, kind, body, mid):
    y.ledger.delivery_plan(key, room, kind, body, "in-1")
    y.ledger.delivery_set(key, state="sent", message_id=mid, sent=time.time())


def test_an_owner_rooms_message_rates_its_last_reply_once_per_sign(tmp_path):
    y = install(tmp_path)
    path, before = tmp_path / "private" / taste.FILE, (tmp_path / "private" / taste.FILE).read_text()
    y.taste_heard("example-room", "that was good")                  # no reply yet: nothing to rate
    sent(y, "example-room", "t1:0", "reply", "Part one.", "900")
    sent(y, "example-room", "t1:1", "reply", "Part two.", "901")
    sent(y, "example-room", "n1:0", "notice", "Not done.", "902")   # a notice is not a reply
    for text in ("A goof tho", "what time is the meeting tomorrow?", "that was good but way too long",
                 "way too long", "lmao", "lmao again"):
        y.taste_heard("example-room", text)
    new = [json.loads(x) for x in path.read_text()[len(before):].splitlines()]
    assert [(r["verdict"], r["signal"]) for r in new] == [("neg", "that was good but way too long"), ("pos", "lmao")]
    row = new[0]
    assert set(row) == {"ts", "channel", "verdict", "signal", "reply_id", "reply_at", "reply", "room"}
    assert (row["reply_id"], row["reply"], row["room"], row["channel"]) == ("900", "Part one.\nPart two.", "example-room", "CHANNEL_ID")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", row["ts"]) and re.fullmatch(r"\d\d:\d\d", row["reply_at"])
    assert y.taste_for("example-room").splitlines()[1] == '👍 "lmao" → "Part one. Part two."'


def test_a_room_that_is_not_the_owners_gets_no_part_and_rates_nothing(tmp_path):
    y = install(tmp_path)
    family = tmp_path / "private" / "family" / taste.FILE
    before = family.read_text()
    sent(y, "family-room", "f1:0", "reply", "Her answer.", "800")
    y.taste_heard("family-room", "that was good")
    assert family.read_text() == before
    assert y.taste_for("family-room") == "" and y.taste_for("no-such-room") == ""
    assert y.taste_for("example-room").startswith(taste.HEAD)


def test_a_layer_without_taste_jsonl_gets_no_part_and_no_file(tmp_path):
    y = install(tmp_path)
    (tmp_path / "private" / taste.FILE).unlink()
    sent(y, "example-room", "t1:0", "reply", "An answer.", "900")
    y.taste_heard("example-room", "that was good")
    assert not (tmp_path / "private" / taste.FILE).exists() and y.taste_for("example-room") == ""


def test_the_part_comes_after_recall_and_the_message_stays_under_the_cap(tmp_path):
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-1", "one")
    rs, b = rooms(EXAMPLE_PRIVATE), budgets()
    now = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)
    rows = [{**r, "reply": r["reply"] + " word" * 400} for r in taste.load(FIXTURE)]
    out = hooks.user_prompt_submit({"session_id": "sid-1", "prompt": "hi"}, tmp_path, rs, lambda r, p: ("owner", None), b,
                                   now=now, recall=lambda r, p: ([], [], [], ["the example kettle"]),
                                   taste=lambda r: taste.render(rows, r, b["per_message"]["on_hit"]["taste"]))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.index("the example kettle") < ctx.index(taste.HEAD)
    rec = json.loads((tmp_path / "rooms" / "example-room" / "assembled.jsonl").read_text().splitlines()[-1])
    assert list(rec["parts"]) == ["clock", "metadata", "recall", "taste"] and rec["parts"]["taste"] <= 1500
    assert rec["chars"] <= b["per_message"]["cap"]


def test_yerk_map_shows_an_owner_rooms_taste_part(tmp_path, capsys):
    install(tmp_path)
    private, state = tmp_path / "private", tmp_path / "state"
    assert cli.main(["map", "example-room", "--private", str(private), "--state", str(state)]) == 0
    size = context.chars(taste.render(taste.load(private / taste.FILE), "example-room", 1500))
    assert re.search(rf"^  taste +{size} +1,500  ok, on hit, under the cap  \[{re.escape(str(private / taste.FILE))}\]$",
                     capsys.readouterr().out, re.M)
    cli.main(["map", "family-room", "--private", str(private), "--state", str(state)])
    assert "  taste " not in capsys.readouterr().out
    assert cli.main(["map", "example-room", "--state", str(state)]) == 0                    # the example layer keeps none
    assert re.search(r"^  taste +0 +1,500  empty, on hit, under the cap  \[no taste\.jsonl in the layer\]$", capsys.readouterr().out, re.M)


def test_the_owners_own_words_in_the_rooms_own_channel_are_rated_nothing_else(tmp_path):
    heard = []
    d = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=(client := Client()), state=tmp_path,
                       taste=lambda room, text: heard.append((room, text)))
    room = Room(tmp_path)
    d.add_room(ROOM_CH, room, watch=OTHER_CH, people=frozenset({PERSON}))
    run(d.on_message(Msg(client.chans[ROOM_CH], 1, "lmao")))
    run(d.on_message(Msg(client.chans[ROOM_CH], 1, "lmao")))                    # a duplicate delivery
    run(d.on_message(Msg(client.chans[ROOM_CH], 2, "lmao", webhook_id=HOOK)))   # the test sender
    run(d.on_message(Msg(client.chans[ROOM_CH], 3, "lmao", author=PERSON)))     # a person of the room
    run(d.on_message(Msg(client.chans[OTHER_CH], 4, "lmao")))                   # the owner, in the channel it watches
    assert heard == [("test-room", "lmao")] and len(room.got) == 4
