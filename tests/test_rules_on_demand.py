"""Checks rules on demand: a rule that does not fit in the rules file loads when a message names its topic, never at session start, and is never
never held in no room. Its triggers are its job's words and what its own words name, each weighted by its rarity among the
rules as recall weighs a word; it rides in the per-message rules part beside the news, best first and under that part's
budget, each once per session and again after a compaction, in the rooms its [rooms: ...] names or in every room. yerkd
moves rules-overflow.md's rules into it once; curate proposes `topic`, `gate` and a promotion from it. Every value here is
a placeholder (decision 22)."""
import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone

import pytest

from yerk_ai import context, curate, memory, words
from yerk_ai.config import EXAMPLE_PRIVATE

from test_curate_rules import NOW, tree  # noqa: F401  (the curate fixture: an example layer, its rules file, the week's evidence)
from test_rules import A, B, FAMILY, daemon, fill, rule, start

AT = "(example-room, 2026-10-21 09:00:00-04:00)"
WIDGET = f"- write a widget memo: Open a widget memo on its order number, since the buyer files by it. {AT} [m:m-7]"
HALL = f"- book the example hall: Book the Example Hall a week ahead, since it fills. {AT} [m:m-8]"


def topics(y, *rows: str) -> None:
    (y.memory.root / memory.TOPIC_RULES).write_text("\n".join([*memory.TOPIC_HEAD, *rows]) + "\n")


def said(y, sid: str, prompt: str) -> str:
    """What a message of session `sid` is handed, through the daemon's own UserPromptSubmit hook."""
    out = asyncio.run(y.hook({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": prompt}))
    return out["hookSpecificOutput"]["additionalContext"]


def test_a_topic_rule_rides_on_a_message_naming_its_topic_once_per_session_and_again_after_a_compaction(tmp_path):
    y = daemon(tmp_path)
    fill(y, 3000)                                   # the always-on rules, so a word has rules to be rare among
    topics(y, WIDGET, HALL)
    start(y, B, "sid-b")
    block = said(y, "sid-b", "draft the widget memo for order 12")
    assert block.count(f"{context.TOPICAL}\n{WIDGET}") == 1 and HALL not in block and context.NEWS not in block
    assert context.TOPICAL not in said(y, "sid-b", "the widget memo again")            # once per session
    assert context.TOPICAL not in said(y, "sid-b", "how was the weather today")        # a message on no rule's topic
    asyncio.run(y.hook({"hook_event_name": "SessionStart", "source": "compact", "session_id": "sid-b"}))
    assert WIDGET in said(y, "sid-b", "the widget memo, once more")                   # again after a compaction
    start(y, B, "sid-c")
    both = said(y, "sid-c", "book the hall and write the widget memo")                # best first: three of its words, then two
    assert both.index(WIDGET) < both.index(HALL)
    rows = [json.loads(x) for x in (tmp_path / "state" / "rooms" / B / "assembled.jsonl").read_text().splitlines()]
    assert [r["topic_rules"] for r in rows if r["event"] == "message"] == [
        ["write a widget memo"], [], [], ["write a widget memo"], ["write a widget memo", "book the example hall"]]
    assert context.rode(tmp_path / "state", B) == (5, {"write a widget memo": 3, "book the example hall": 1})
    y.budgets["per_message"]["on_hit"]["rules"] = context.chars(f"{context.TOPICAL}\n{WIDGET}") + 1
    start(y, B, "sid-d")
    assert HALL not in said(y, "sid-d", "book the hall and write the widget memo")    # each whole under the budget, the best
    assert HALL in said(y, "sid-d", "book the example hall")                          # the one left out rides when it fits


def test_a_rule_kept_to_one_room_never_rides_in_another(tmp_path):
    y = daemon(tmp_path)
    fill(y, 3000)
    topics(y, kept := memory.placed(WIDGET, [A]))
    assert kept == WIDGET.replace(f" {AT}", f" [rooms: {A}] {AT}")                   # the mark before its trailer
    for room, sid in ((A, "sid-a"), (B, "sid-b"), (FAMILY, "sid-f")):
        start(y, room, sid)
    assert context.TOPICAL not in said(y, "sid-b", "draft the widget memo") and context.TOPICAL not in said(y, "sid-f", "draft the widget memo")
    assert kept in said(y, "sid-a", "draft the widget memo")
    topics(y, WIDGET)                                                                  # no mark: every room's, her room's too
    assert WIDGET in said(y, "sid-f", "a widget memo")


def test_a_rule_filed_where_its_words_are_that_rooms_own_is_kept_to_it_and_otherwise_to_none(tmp_path):
    books = {"work": "- A widget memo goes to the buyer.", "work-too": "- A widget memo goes to the buyer.", "home": "- Dinner is at six."}
    corpus = [WIDGET, HALL, f"- cook dinner: Dinner at six, since the example cook leaves at seven. {AT} [m:m-9]"]
    assert memory.scoped(WIDGET, "work", books, corpus) == ["work", "work-too"]     # only the rulebook they read says widget
    assert memory.scoped(corpus[2], "work", books, corpus) == []                     # dinner is the home room's word
    assert memory.scoped(HALL, "work", books, corpus) == [] and memory.scoped(WIDGET, "attic", books, corpus) == []
    both = {k: f"{t} A message is short." for k, t in books.items()}                # a word every room's rulebook says
    lines = [*corpus, f"- send a message: Keep a message short. {AT} [m:m-10]", f"- reply to a message: Reply in one line. {AT} [m:m-11]",
             end := f"- write a widget message: End a widget message on its last sentence. {AT} [m:m-12]"]
    assert memory.scoped(end, "work", both, lines) == ["work", "work-too"] and memory.scoped(end, "home", both, lines) == []   # widget outweighs message
    priv = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    (priv / "rulebook-work.md").write_text("# Rulebook\n\n- A widget memo goes to the buyer by noon.\n")
    (priv / "rooms.toml").write_text((priv / "rooms.toml").read_text().replace('rulebook = "rulebook.md"', 'rulebook = "rulebook-work.md"', 1))
    y = daemon(tmp_path, priv)                                                         # its family room reads A's rulebook too
    fill(y, y.budgets["session_start"]["appended"]["rules"] - 50)
    r = rule(y, A, "Open a widget memo on its order number, since the buyer files by it.", "write a widget memo")
    assert r["topic"] and r["rooms"] == [A, FAMILY]
    assert (y.memory.root / memory.TOPIC_RULES).read_text().splitlines()[-1].startswith(
        f"- write a widget memo: Open a widget memo on its order number, since the buyer files by it. [rooms: {A},{FAMILY}] ({A}, ")


def test_the_overflow_moves_to_the_topic_rules_once_with_its_backup_and_one_log_line(tmp_path, caplog):
    y = daemon(tmp_path)
    held, old = y.memory.root / memory.OVERFLOW, "- write a widget memo: Open it on the order date. (example-room, 2026-10-20 09:00:00-04:00) [m:m-6]"
    held.parent.mkdir(parents=True, exist_ok=True)
    held.write_text(f"# Rules held: past the rules file's cap, in no room\n\n{old}\n{HALL}\n{WIDGET}\n")
    before = held.read_bytes()
    with caplog.at_level(logging.INFO, logger="yerk.memory"):
        line = y.move_held()
    assert line == f"{words.DONE} 3 rules moved to rules-topics.md\nfrom rules-overflow.md, once\neach loads when its topic comes up"
    tier = (y.memory.root / memory.TOPIC_RULES).read_text().splitlines()
    assert tier == [*memory.TOPIC_HEAD, memory.mark(old, "tree/rules-topics.md:5"), HALL, WIDGET]     # one live line a job
    assert not held.exists() and held.with_name("rules-overflow.md.moved").read_bytes() == before
    assert [r.getMessage() for r in caplog.records if r.name == "yerk.memory"] == [
        "rules: 3 rule(s) moved from rules-overflow.md to rules-topics.md, each loading when its topic comes up; the old file kept as "
        "rules-overflow.md.moved"]
    assert y.move_held() == "" and (y.memory.root / memory.TOPIC_RULES).read_text().splitlines() == tier     # once
    held.write_bytes(before)                     # a start cut off between the write and the rename: nothing moves twice
    assert y.move_held() == line and (y.memory.root / memory.TOPIC_RULES).read_text().splitlines() == tier


TILL = "- close the till: Count the till twice before closing, since one count misses a coin. (example-room, 2026-10-21 08:00:00+00:00) [m:1010]"
SHOP = "- open the shop: Unlock at nine, since the first guest comes at ten past. [rooms: example-room] (example-room, 2026-10-21 08:05:00+00:00) [m:1011]"
HALLR = "- book the example hall: Book the Example Hall a week ahead, since it fills. (example-room, 2026-10-21 09:30:00+00:00) [m:2001]"
NOTE = "- write a note: Keep a note free of em dashes, since he reads one as a machine's. (example-room, 2026-10-21 09:40:00+00:00) [m:2002]"


@pytest.fixture
def tiers(tree):  # noqa: F811
    """The curate fixture plus: the owner's messages of the day, one of them filing a rule on one topic; that rule and one a
    code check could hold in the rules file; and two topic rules, one every room's that rode in two of the week's three
    sessions, one kept to its room that rode in both."""
    priv, state, s = tree
    at = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)
    sent = ["can you book the example hall for friday", "good morning", "thanks, that works", "send me the photo", "what is for lunch",
            "is the shop open"]
    with (s.daily / "2026-10-21.md").open("a") as f:
        f.write("".join(memory.format_line(at, "example-room", "discord:100", "owner", "", x, "private", str(2001 + 10 * i)) + "\n"
                        for i, x in enumerate(sent)))
    (s.root / "rules.md").write_text((s.root / "rules.md").read_text() + f"{HALLR}\n{NOTE}\n")
    (s.root / memory.TOPIC_RULES).write_text("\n".join([*memory.TOPIC_HEAD, TILL, SHOP]) + "\n")
    (rooms := state / "rooms" / "example-room").mkdir(parents=True)
    (rooms / "assembled.jsonl").write_text("".join(json.dumps({"t": NOW - 3600 * k, "event": "message", "session_id": f"s{k}", "topic_rules": rode})
                                                   + "\n" for k, rode in ((1, ["close the till", "open the shop"]), (2, ["open the shop", "close the till"]), (3, []))))
    return priv, state, s


def test_curate_proposes_a_topic_rule_a_gate_and_a_promotion_from_the_topic_rules(tiers):
    priv, state, s = tiers
    props = curate.propose(priv, state, s, NOW)[0]
    rules, tier = str((s.root / "rules.md").resolve()), str((s.root / memory.TOPIC_RULES).resolve())
    topic = [q for q in props if q["kind"] == "topic"]
    assert [q["label"] for q in topic] == ["book the example hall"]                    # filed off a message naming it
    n = (s.root / "rules.md").read_text().splitlines().index(HALLR) + 1
    assert topic[0]["edits"] == [[rules, n, HALLR, ""], [tier, 0, "", HALLR]] and topic[0]["saved"] == len(HALLR)    # blanked
    assert topic[0]["why"] == ("one topic: the message it was filed from names it (book, hall or example), it says no every-reply topic, "
                               "and 1 of the week's 6 messages named it; on demand it loads on those, in every room")
    gate = [q for q in props if q["kind"] == "gate" and q["label"] == "write a note"]
    assert len(gate) == 1 and gate[0]["edits"] == [[rules, n + 1, NOTE, NOTE]] and gate[0]["saved"] == 0
    assert gate[0]["why"] == ("a code check could hold it on every reply: a reply holding an em dash (U+2014); curate names the check and "
                              "never builds it")
    promote = [q for q in props if q["kind"] == "promote" and q["label"] in ("close the till", "open the shop")]
    assert [q["label"] for q in promote] == ["close the till"]                          # every room's, in two of three sessions
    assert promote[0]["why"] == "it rode in 2 of the week's 3 sessions; it fits rules.md under its line"
    assert promote[0]["edits"] == [[rules, 0, "", TILL], [tier, 3, TILL, ""]]
    r = curate.rules(priv, state, s, False, NOW, post=lambda p: {"ok": True})
    page = (s.root / "rules-proposals" / "2026-10-21.md").read_text()
    assert "(unchanged: a gate would hold it)" in page and "memory/rules-topics.md: " in page and ", loaded on demand\n" in page
    k = {q["label"]: q["n"] for q in r["proposals"]}
    with pytest.raises(RuntimeError, match="is a gate"):
        curate.apply(priv, state, "2026-10-21", str(k["write a note"]), NOW)
    curate.apply(priv, state, "2026-10-21", str(k["book the example hall"]), NOW)
    assert HALLR not in (s.root / "rules.md").read_text() and (s.root / memory.TOPIC_RULES).read_text().splitlines()[-1] == HALLR
