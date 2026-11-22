"""Checks decision receipts, which keep each judgment call a room's reply makes for the person it answers, with what the turn read to make it.
Code reads a reply for those calls (words.CALL, no model), each call is a `calls` row with the cites its turn read or a flag that it read
nothing, the next morning diff gives each call a line and its receipt under a cap, and `yerk calls fix` files the correction beside the wrong
line the way `remember` does, never in a persona or a rule. Decision receipts are claim 19. Every value here is a placeholder (decision 22)."""
import json

import test_gates
from test_gates import call, human_turn
from test_observe import NOW, layer

from yerk_ai import cli, evidence, hooks, lessons, memory, mentions, morning, status, words
from yerk_ai.evidence import Evidence
from yerk_ai.ledger import Ledger

CALLS = ["I'd go with Plan B, it is 20 a month less.", "Pick the 2 pm slot, the morning one clashes with the standup.",
         "My call: send the deck tonight, not Monday.", "You should ask the lead before Friday.",
         "The plan is: leave at 7, fuel at the halfway stop, there by 9.", "1. Book the 9:40 to the north office.",
         "350, don't go higher.", "Go with Option B.", "I'd hold it until the second score is in.",
         "I'd put it at 60/40 the lead says yes.", "I'd take the earlier one, it lands before the standup."]
NOT_CALLS = ["I went with the blue one last time.", "You should have gotten an email from the lead by now.",
             "My call with the vendor went long.", "So you should call the lead tonight?", 'The lead said "go with the blue one" last week.',
             "2. The deck went out at 9.", "Pick up the parcel at 3.", "The plan was to leave at 7.",
             # the cold read's two real replies, their names, dates and the rest cut: a past move criticised, and "I'd" as "I had"
             "Almost nobody, and that's the point you should've led with.", "That is the point you should’ve led with.",
             "I'd put your two runs in the tracker an hour earlier, so the information was in my hands and I didn't check it against "
             "what I was about to say.", "I'd set the reminder last week.", "I'd set it up two days ago.",
             "I'd put the parcel in the hall yesterday.", "I'd already put the deck in the drive.", "You could've asked, and I would've said yes."]
REPLY = ("Checked both quotes.\n\n- **Plan B:** 20 a month less, same speed.\n\nI'd go with Plan B. Want me to book the install?\n"
         "```python\npick = 'the first one'\n```")
RECALL = f"what about the plans?\n{hooks.HEADS[1]}\n- Plan B quoted at 20 a month less (memory/daily/2026-01-05.md:4)"
PACK = "[recall pack]\n- a line from the day before (memory/daily/2026-01-04.md:7)"
STOP = {"hook_event_name": "Stop", "stop_hook_active": False}


def turn(ev: Evidence, *calls: tuple[str, dict, str]) -> Evidence:
    """A fake turn's stream: each (tool, input, what came back) as its tool_use and tool_result."""
    for i, (tool, ti, back) in enumerate(calls):
        ev.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": tool, "input": ti}]}})
        ev.observe({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": back}]}})
    return ev


def test_code_reads_each_call_shape_and_passes_the_ones_that_only_look_like_one():
    assert [evidence.calls(s) for s in CALLS] == [[s] for s in CALLS]
    assert [evidence.calls(s) for s in NOT_CALLS] == [[] for _ in NOT_CALLS]
    many = "Here it is.\n\n1. Book the 9:40.\n2. Text the lead.\n\n```\npick this\n```\nWant me to book it? Done. Pick the blue one."
    assert evidence.calls(many) == ["1. Book the 9:40.", "2. Text the lead.", "Pick the blue one."]     # no code, no question
    assert evidence.calls(REPLY) == ["I'd go with Plan B."]


def test_the_join_is_the_recall_the_turn_was_handed_and_every_line_it_opened():
    ev = Evidence()
    ev.give(PACK)                                  # the session-start file: every turn has it, so it backs no call
    ev.give(RECALL, recall=True)
    turn(ev, ("Read", {"file_path": "/layer/memory/plans.md", "offset": 12, "limit": 5}, "     12\tPlan B: 20 a month less"),
         ("mcp__yerk__recall", {"query": "plan b"}, json.dumps({"ok": True, "hits": [{"cite": "memory/daily/2026-01-06.md:9"}]})),
         ("Grep", {"pattern": "Plan B", "path": "/layer/memory"}, "/layer/memory/quotes.md:3:Plan B 45 a month"),
         ("ToolSearch", {"query": "select:send"}, "no read"),
         ("WebFetch", {"url": "https://example.org/plans"}, "a page with no cites"))
    short = lambda p: p.replace("/layer/", "")     # noqa: E731
    assert evidence.receipts(ev, short) == ["memory/daily/2026-01-05.md:4", "memory/plans.md:12", "memory/daily/2026-01-06.md:9",
                                            "memory/quotes.md:3", "https://example.org/plans"]
    rows = evidence.receipt_rows(REPLY, ev, short)
    assert [(r["call"], len(json.loads(r["reads"])), r["no_reads"]) for r in rows] == [("I'd go with Plan B.", 5, 0)]
    bare = turn(Evidence(), ("ToolSearch", {"query": "select:send"}, "no read"))       # only calls that read nothing
    assert evidence.receipt_rows(REPLY, bare) == [{"call": "I'd go with Plan B.", "reads": "[]", "no_reads": 1}]
    assert evidence.receipt_rows("Booked it for 9:40.", ev) == []


def test_a_reply_that_went_out_is_a_calls_row_a_person_rooms_for_its_people_and_a_turned_back_one_none(tmp_path):
    y, room, ch = test_gates.setup(tmp_path)
    human_turn(room, "which plan?")
    room.evidence.give(RECALL, recall=True)
    call(y, {**STOP, "last_assistant_message": REPLY})
    call(y, {**STOP, "last_assistant_message": REPLY})                         # the same call once a turn
    rows = y.ledger.rows(10, "calls")
    assert [(r["room"], r["whom"], r["turn"], r["call"], json.loads(r["reads"]), r["no_reads"]) for r in rows] == [
        ("example-room", "owner", "tk-1", "I'd go with Plan B.", ["memory/daily/2026-01-05.md:4"], 0)]
    room.evidence.reset()
    room.evidence.give(PACK)
    human_turn(room, "what did the lead want?")
    call(y, {**STOP, "last_assistant_message": "Sam said to go with Plan B. You should book it tonight."})    # said or noted: turned back
    assert len(y.ledger.rows(10, "calls")) == 1
    call(y, {**STOP, "stop_hook_active": True, "last_assistant_message": "You should book it tonight."})     # the second try goes out
    assert [(r["call"], r["reads"], r["no_reads"]) for r in y.ledger.rows(10, "calls")][0] == ("You should book it tonight.", "[]", 1)
    room.conf.update(privacy="family", people=["2"])
    room.evidence.reset()
    human_turn(room, "which one?", sender="person")
    call(y, {**STOP, "last_assistant_message": "Go with the green one."})
    assert y.ledger.rows(1, "calls")[0]["whom"] == "example-room"              # a person's room: for its people, never the owner


def receipts_layer(tmp_path):
    priv, state, accounts, cool = layer(tmp_path, machine=None)
    led, y0 = Ledger(state / "ledger.sqlite"), status.midnight(NOW, 1)
    for i, (room, whom, said, reads) in enumerate([
            ("example-room", "owner", "I'd go with Plan B, it is 20 a month less than the other quote.",
             ["memory/daily/2026-01-05.md:4", "memory/plans.md:12", "memory/quotes.md:3"]),
            ("example-quiet", "owner", "You should book it tonight.", []),
            ("example-room", "example-room", "Go with the green one.", ["memory/daily/2026-01-06.md:2"])]):
        led.call(t=y0 + 100 + i, room=room, session_id="s", turn=f"tk-{i}", whom=whom, call=said, reads=json.dumps(reads), no_reads=int(not reads))
    return priv, state, accounts, cool, led, y0


def test_the_morning_diff_gives_each_call_its_line_and_its_receipt_under_a_cap(tmp_path):
    priv, state, accounts, cool, led, y0 = receipts_layer(tmp_path)
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": y0 - 10, "units": []}, NOW)
    text = "\n".join(lines).splitlines()
    at = text.index("- calls yesterday: 2, 1 with no reads")
    assert text[at:at + 6] == ["- calls yesterday: 2, 1 with no reads", "- example-room: I'd go with Plan B...",
                               "-# call 1: memory/daily/2026-01-05.md:4 +2", f"{words.WORKING} example-quiet: You should book it...",
                               "-# call 2: no reads behind it", "- example-room: 1 call for its people"]
    assert "green one" not in "\n".join(text) and not [k for k in stamp["findings"] if "call" in k]     # receipts, never findings
    assert all(words.width(x) <= words.PHONE for x in text)
    for i in range(22):
        led.call(t=y0 + 200 + i, room="example-room", session_id="s", turn=f"tk-m{i}", whom="owner", call=f"Pick slot {i}.",
                 reads='["memory/plans.md:1"]', no_reads=0)
    shown = "\n".join(x for _, x in morning.calls(led, y0, y0 + 86400)).splitlines()
    assert shown[0] == "- calls yesterday: 24, 1 with no reads" and len([x for x in shown if x.startswith("-# call ")]) == 20
    assert "- and 4 more calls" in shown
    nothing = "\n".join(x for _, x in morning.calls(Ledger(tmp_path / "empty.sqlite"), y0, y0 + 86400))
    assert nothing == "- calls yesterday: none"                              # a quiet day says so, never silent


def test_calls_fix_files_the_correction_beside_the_wrong_line_and_never_in_a_persona_or_a_rule(tmp_path, capsys):
    priv, state, accounts, cool, led, y0 = receipts_layer(tmp_path)
    (priv / "memory" / "plans.md").write_text("# Plans\n\n" + "\n" * 9 + "- Plan B is 20 a month less (example-room, 2026-01-05) [m:m-1]\n")
    (priv / "memory" / memory.RULES).write_text("# Rules\n\n- quote the plan's monthly price (example-room, 2026-01-05 10:00) [m:m-2]\n")
    kept, days = [priv / "persona.md", priv / "rulebook.md", priv / "memory" / memory.RULES], sorted((priv / "memory" / "daily").iterdir())
    before = {p: p.read_text() for p in kept}
    args = ["--private", str(priv), "--state", str(state)]
    assert cli.main(["calls", "fix", "1", "memory/rules.md:3", "Plan B is 45 a month", *args]) == 2
    assert "rules.md is a persona or a rule file" in capsys.readouterr().err
    for src in ("persona.md:1", "rulebook.md:2", "memory/plans.md:1", "memory/plans.md", "memory/none.md:3"):
        assert cli.main(["calls", "fix", "1", src, "Plan B is 45 a month", *args]) == 2       # refused, nothing filed
    assert cli.main(["calls", "fix", "9", "memory/plans.md:12", "x", *args]) == 2 and "no call 9" in capsys.readouterr().err
    assert led.call_row(1)["fixed"] is None and sorted((priv / "memory" / "daily").iterdir()) == days
    assert cli.main(["calls", "fix", "1", "memory/plans.md:12", "Plan B is 45 a month, the 20 was the promo", *args]) == 0
    row, out = led.call_row(1), capsys.readouterr().out.splitlines()
    day = priv / row["correction"].rpartition(":")[0]
    new = day.read_text().splitlines()[int(row["correction"].rpartition(":")[2]) - 1]
    assert row["source"] == "memory/plans.md:12" and day.parent.name == "daily" and day not in days
    assert out[:3] == ["call 1: memory/plans.md:12 corrected", f"by {row['correction']}", "superseded: memory/plans.md:12"]
    assert 'Plan B is 45 a month, the 20 was the promo (the fix for call 1, example-room: "I\'d go with Plan B' in new and "(supersedes memory/plans.md:12)" in new
    wrong = (priv / "memory" / "plans.md").read_text().splitlines()[11]
    assert f"{mentions.OLD} by {row['correction']}]" in wrong and {p: p.read_text() for p in before} == before
    fixed = "\n".join(x for _, x in morning.calls(led, float(row["fixed"]) - 1, float(row["fixed"]) + 1)).splitlines()
    assert fixed[-2:] == [f"{words.DONE} call 1: memory/plans.md:12 corrected", f"-# by {row['correction']}"]     # the next morning


def test_calls_fix_never_files_a_correction_on_a_lesson_of_a_converted_store(tmp_path, capsys):
    priv, state, accounts, cool, led, y0 = receipts_layer(tmp_path)
    root = priv / "memory"
    lessons.put(root, lessons.Lesson("quote-the-plan", "quote the plan", "Quote the plan's monthly price.", "Since a yearly price reads lower.",
                                     filed="example-room, 2026-01-05 10:00:00+00:00 [m:m-2]"))
    x, days = lessons.parse(f := root / "rules" / "quote-the-plan.md"), sorted((root / "daily").iterdir())
    was, logs = f.read_bytes(), {p: p.read_bytes() for p in days}
    src = f"memory/rules/quote-the-plan.md:{x.front().count(chr(10)) + 1}"
    assert cli.main(["calls", "fix", "1", src, "Plan B is 45 a month", "--private", str(priv), "--state", str(state)]) == 2
    assert "quote-the-plan.md is a persona or a rule file" in capsys.readouterr().err
    assert led.call_row(1)["fixed"] is None and f.read_bytes() == was
    assert {p: p.read_bytes() for p in sorted((root / "daily").iterdir())} == logs
