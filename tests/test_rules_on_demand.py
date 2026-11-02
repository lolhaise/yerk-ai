"""Checks rules on demand: a rule that does not fit in the rules file loads when a message names its topic, never at session start, and is never
left where no room gets it. Its triggers are its job's words and what its own words name, or the words it was filed to load on
(`triggers`), each weighted by its rarity among the rules as recall weighs a word, a picked one as a word only it says; it
rides in the per-message rules part beside the news, best first and under that part's
budget, each once per session and again after a compaction, in the rooms its [rooms: ...] names or in every room. yerkd
moves rules-overflow.md's rules into it once; curate proposes `topic`, `gate` and a promotion from it. Every value here is
a placeholder (decision 22)."""
import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone

import pytest

from yerk_ai import context, curate, mcp_server, memory, words
from yerk_ai.config import EXAMPLE_PRIVATE

from fake_channel import ConsoleChannel
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


def test_a_rooms_mark_typed_on_a_held_rule_is_kept_through_the_move(tmp_path):
    """The owner's word on a held rule whose words are every room's (cold read: a work room's rule kept out of a family room): a
    [rooms: ...] mark typed on its line in rules-overflow.md before yerkd's first start is the scope the move gives it, put before
    its trailer, where `scoped` would make it every room's; a line with no mark still gets `scoped`'s."""
    y = daemon(tmp_path)
    fill(y, 3000)
    held = y.memory.root / memory.OVERFLOW
    held.write_text(f"# Rules held: past the rules file's cap, in no room\n\n{WIDGET} [rooms: {A}]\n{HALL}\n")     # typed after its trailer
    assert memory.scoped(WIDGET, A, context.books(y.private, y.room_confs), [WIDGET, HALL]) == []           # its words: every room's
    assert y.move_held()
    assert (y.memory.root / memory.TOPIC_RULES).read_text().splitlines() == [*memory.TOPIC_HEAD, memory.placed(WIDGET, [A]), HALL]
    for room, sid in ((A, "sid-a"), (FAMILY, "sid-f")):
        start(y, room, sid)
    assert memory.placed(WIDGET, [A]) in said(y, "sid-a", "draft the widget memo")
    assert context.TOPICAL not in said(y, "sid-f", "draft the widget memo")           # her room reads A's rulebook, and still not


def test_a_person_room_never_reads_the_topic_rules_file_whole(tmp_path):
    """rules-topics.md is private, as rules-overflow.md was: the owner's rooms read it whole; a person's room (the family room) is
    refused it by read_memory, recall drops its lines for her (memory.allowed_text, the filter every recall hit passes), and in her
    messages only a topic rule for every room, or one kept to her room, ever rides."""
    y = daemon(tmp_path)
    fill(y, y.budgets["session_start"]["appended"]["rules"] - 50)
    assert rule(y, A, "Open a widget memo on its order number, since the buyer files by it.", "write a widget memo")["topic"]
    tier = y.memory.root / memory.TOPIC_RULES
    assert memory.file_class(tier) == ("private", "") and tier.stat().st_mode & 0o777 == 0o600
    topics(y, kept := memory.placed(WIDGET, [A]), HALL)
    whole = asyncio.run(y.tool_read_memory(A, memory.TOPIC_RULES, 1, 50))
    assert whole["ok"] and kept in whole["text"] and HALL in whole["text"]
    assert asyncio.run(y.tool_read_memory(FAMILY, memory.TOPIC_RULES, 1, 50)) == {
        "ok": False, "error": f"{memory.TOPIC_RULES} is not readable from room {FAMILY}: its class keeps it to the owner's rooms"}
    assert memory.allowed_text(y.reader(FAMILY), tier, text := tier.read_text()) is None and memory.allowed_text(y.reader(A), tier, text) == text
    start(y, FAMILY, "sid-f")
    block = said(y, "sid-f", "book the example hall and write the widget memo")
    assert HALL in block and kept not in block and "on its order number" not in block


def test_a_topic_filing_supersedes_its_jobs_older_line_in_the_rules_file(tmp_path):
    """Cold read: a correction filed as a topic rule left its job's older line live in rules.md, so every session start sent the
    old rule and a message on its topic the new one beside it, "as binding". The older line is marked superseded by the topic
    rule's cite (the docstring's "in RULES or TOPIC_RULES"), the news says it was replaced, and a retried call marks nothing."""
    y = daemon(tmp_path)
    was = "- write a widget memo: Open a widget memo on its date. (example-room, 2026-10-20 09:00:00-04:00) [m:m-6]"
    path = fill(y, y.budgets["session_start"]["appended"]["rules"] - 50, (was,))
    n, before = len(path.read_text().splitlines()), path.read_text().splitlines()
    start(y, B, "sid-b")
    body = "Open a widget memo on its order number, never its date, since the buyer files every memo by its order number and a dated one sits unread."
    r = rule(y, A, body, "write a widget memo")
    assert r["topic"] and r["cite"] == "tree/rules-topics.md:3" and r["replaced"] == [f"tree/rules.md:{n}"]
    assert path.read_text().splitlines() == [*before[:-1], memory.mark(was, "tree/rules-topics.md:3")]
    assert "on its date" not in context.rules_text(y.room_confs[A], path)[0]        # no session start sends the old rule
    block = said(y, "sid-b", "draft the widget memo")
    assert f"replaced in rules.md: {was.removeprefix('- ')}" in block and f"{context.TOPICAL}\n- write a widget memo: {body}" in block
    assert rule(y, A, body, "write a widget memo") == {**r, "replaced": []} and path.read_text().splitlines()[-1] == memory.mark(was, "tree/rules-topics.md:3")


PLAN = "Draft a widget plan from the example ledger's totals, since Nora at Example Tank reads the totals first."
MONEY = ["money", "how do I make money", "make us money"]


def remember(y, monkeypatch, **args) -> dict:
    """The room's own `remember` call, through Yerk AI's MCP server as example-room makes it."""
    monkeypatch.setattr(mcp_server, "caller_room", lambda ctx: A)
    got = asyncio.run(mcp_server.build(y, y.port, y.hook).call_tool("remember", args))
    return json.loads((got[0] if isinstance(got, tuple) else got)[0].text)


def test_a_topic_rule_loads_on_the_triggers_it_is_filed_with_and_its_older_filing_is_named_replaced(tmp_path, monkeypatch, capsys):
    """Need 22 of 2026-10-25: asked to make a topic rule load on "money" and "how do I make money", a room filed the rule again with
    those words in its text; its triggers stayed its job's words and the names its text holds, "money" (an everyday word) never one,
    and the filing said replaced: [] though it had marked the older topic line. `triggers` picks them: the rule loads on those and its
    job's words, never on a name its text holds, an [on: ...] mark before its trailer; the older line is marked and named in `replaced`,
    and the reply's clause and the machine room's notice name the words picked."""
    y = daemon(tmp_path)
    y.room_confs["example-machine"] = {**y.room_confs[A], "role": "machine", "id": "example-machine-id"}
    y.add_room("example-machine", ConsoleChannel())
    fill(y, y.budgets["session_start"]["appended"]["rules"] - 50)
    first = remember(y, monkeypatch, text=PLAN, job="write a widget plan")
    tier = y.memory.root / memory.TOPIC_RULES
    was = tier.read_text().splitlines()[-1]
    assert first["topic"] and first["cite"] == "tree/rules-topics.md:3" and "money" not in first["triggers"]
    start(y, B, "sid-a")
    assert context.TOPICAL not in said(y, "sid-a", "how do I make money this year")          # the wall: no word of it a trigger
    assert was in said(y, "sid-a", "lunch with Nora on friday")                                 # a name its text holds is one
    capsys.readouterr()
    r = remember(y, monkeypatch, text=PLAN, job="write a widget plan", triggers=MONEY)
    on = "money, how do I make money or make us money"
    assert r == {"ok": True, "job": "write a widget plan", "cite": "tree/rules-topics.md:4", "replaced": [first["cite"]], "topic": True,
                 "triggers": MONEY, "rooms": [], "next": words.FILED_NEXT.format(clause=words.TOPIC_CLAUSE.format(triggers=on))}
    new = f"- write a widget plan: {PLAN} [on: money, how do I make money, make us money] ({A}, "
    lines = tier.read_text().splitlines()
    assert lines[:3] == [*memory.TOPIC_HEAD, memory.mark(was, "tree/rules-topics.md:4")] and lines[3].startswith(new) and len(lines) == 4
    posts = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    assert [p["notice"] for p in posts if p["room"] == "example-machine"] == [f"{words.DONE} " + words.RULE_TOPIC.format(
        triggers=words.line(on, words.PHONE - 8), job="write a widget plan", room=A, where=words.TOPIC_WHERE[0],
        cap=y.budgets["session_start"]["appended"]["rules"])]
    start(y, B, "sid-b")
    assert f"{context.TOPICAL}\n{lines[3]}" in said(y, "sid-b", "can we make some money?")      # the words picked
    start(y, B, "sid-c")
    assert context.TOPICAL not in said(y, "sid-c", "lunch with Nora on friday")                 # never on a name its text holds now
    assert lines[3] in said(y, "sid-c", "the widget plan for the fall")                         # its job's words still
    start(y, B, "sid-d")
    assert lines[3] in said(y, "sid-d", "money")                                                # one word picked, alone


def test_a_picked_trigger_weighs_a_word_only_its_rule_says_and_a_phrase_rides_on_every_word_of_it():
    """A word every rule says weighs next to nothing (rarities), so a rule whose job names "money" never rides on it alone where
    every rule says money; a rule filed to load on it does, at the weight of a word only that rule says, and on a phrase it
    picked only when a message says each of that phrase's words that names something, in any order."""
    corpus = [f"- example job {i}: Count the money twice, since the till is short. {AT} [m:m-{i}]" for i in range(6)]
    job = f"- count the money: Count it at noon, since the bank closes at one. {AT} [m:m-7]"
    picked = f"- write a widget plan: Draft it from the ledger, since the buyer reads it. [on: money, break even] {AT} [m:m-8]"
    rules = [*corpus, job, picked]
    one = memory.rarities(rules)[1]
    assert memory.picked(picked) == ["money", "break even"] and memory.picked(job) == [] and "money" in memory.triggers(job).values()
    assert memory.on_topic("money?", [job, picked], rules) == [(round(one, 2), picked)]
    assert memory.on_topic("when do we break even", [picked], rules) == [(round(one, 2), picked)]
    assert memory.on_topic("even so, break the glass", [picked], rules) and not memory.on_topic("break the glass", [picked], rules)
    assert memory.cue(picked, rules) == ["money", "break even"] and memory.placed(picked, [A]).endswith(f"[on: money, break even] [rooms: {A}] {AT} [m:m-8]")


def test_a_re_filing_naming_no_triggers_keeps_the_ones_picked_and_a_trigger_naming_nothing_files_nothing(tmp_path):
    """The owner's words stay a rule's until others are picked: a correction filed with no `triggers` keeps its job's live topic
    rule's, the same call again is a retried one, and a trigger whose words all name nothing (a, the, how) is refused whole."""
    y = daemon(tmp_path)
    fill(y, y.budgets["session_start"]["appended"]["rules"] - 50)
    r = rule(y, A, PLAN, "write a widget plan", ["money", "money", "break, even"])
    assert r["triggers"] == ["money", "break even"]                                             # once each, no mark's own comma
    assert rule(y, A, PLAN, "write a widget plan", ["money", "break even"]) == r == rule(y, A, PLAN, "write a widget plan")   # retried
    fixed = rule(y, A, PLAN.replace("first", "before noon"), "write a widget plan")
    assert fixed["triggers"] == ["money", "break even"] and fixed["replaced"] == [r["cite"]]
    tier = y.memory.root / memory.TOPIC_RULES
    before = tier.read_bytes()
    assert rule(y, A, PLAN, "write a widget plan", ["how do I", "profit"]) == {"ok": False, "error": (
        "nothing filed: a trigger needs a word that names something, not only words like a, the or how: how do I")}
    assert tier.read_bytes() == before and [x for x in memory.live_rules(tier) if x.startswith("- write a widget plan: ")] == [
        tier.read_text().splitlines()[-1]]


def test_a_filing_the_rules_file_has_room_for_names_its_jobs_topic_line_replaced(tmp_path):
    """The other way round: a rule filed into rules.md once it has room again marks its job's topic line superseded, as it did,
    and names it in `replaced` too, where that said [] before; rules.md never carries the triggers, since every start loads it."""
    y = daemon(tmp_path)
    fill(y, y.budgets["session_start"]["appended"]["rules"] - 50)
    r = rule(y, A, PLAN, "write a widget plan", MONEY)
    path = fill(y, 3000)
    back = rule(y, A, PLAN, "write a widget plan", MONEY)
    assert "topic" not in back and back["replaced"] == [r["cite"]] and "[on: " not in path.read_text()
    assert memory.live_rules(y.memory.root / memory.TOPIC_RULES) == [] and path.read_text().splitlines()[-1].startswith(f"- write a widget plan: {PLAN} (")


TILL ="- close the till: Count the till twice before closing, since one count misses a coin. (example-room, 2026-10-21 08:00:00+00:00) [m:1010]"
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
