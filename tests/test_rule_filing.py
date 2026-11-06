"""Checks that a rule a room files (how a job is done from now on, `remember` with a `job`) is checked before anything is
written. First its line in the prompting guide's shape, by the doctor's own per-line counts with its examples out
(shape.filing), then the live lines of other jobs on the same thing in the files the owner rooms load (curate.same), each
settled on the room's next call, superseded or kept with a one-clause reason in its day log; a hand-kept file's line goes
is a placeholder."""
import json
import shutil

import pytest

from fake_channel import ConsoleChannel
from test_curate_rules import DAY, NOW, kinds, tree  # noqa: F401  (curate's own install: rules, a rulebook, a machine room)
from test_rules import A, daemon, fitted, rule
from yerk_ai import curate, memory, shape, words
from yerk_ai.config import EXAMPLE_PRIVATE

MACHINE, AT = "example-machine", "2026-01-02 09:00:00+00:00"
# job A prescribes a list shape with marks and a key line; job B says marks as job A says; a rulebook line says job A's shape,
# marks included; job C, filed now, says a plan is a plain numbered list with no marks
A_SAID = ("Each item is one line opening with its mark (🟢 done, 🟠 running, ⚪ waiting), then a key line naming the marks, "
          "since a phone reads the colour before the words.")
B_SAID = "One block a morning, its items with marks as status lists says, since the owner reads both the same way."
BOOK = "- A plan goes in the status lists shape, marks included, so it reads like every other list here."
C_SAID = "A plan is a plain numbered list, with no marks, since its order is the point and a mark reads as a state."


def install(tmp_path):
    """The example layer, its rulebook holding the case's line, a machine room, and jobs A and B live in the rules file."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    shutil.rmtree(private / "memory" / "daily")                          # a day log of its own: each verdict Yerk AI writes there
    (private / "memory" / "daily").mkdir()
    (private / "rulebook.md").write_text((private / "rulebook.md").read_text() + BOOK + "\n")
    y = daemon(tmp_path, private)
    y.room_confs[MACHINE] = {**y.room_confs[A], "role": "machine", "id": f"{MACHINE}-id"}
    y.add_room(MACHINE, ConsoleChannel())
    (y.memory.root / memory.RULES).write_text("---\nclass: shared\n---\n# Rules: one live line per job, in every room\n\n"
                                              f"- status lists: {A_SAID} ({A}, {AT}) [m:m-0]\n- morning brief: {B_SAID} ({A}, {AT}) [m:m-0]\n")
    return y


def cites(y):
    rules, book = memory.cite(y.memory, y.memory.root / memory.RULES), y.private / "rulebook.md"
    return f"{rules}:6", f"{rules}:7", f"rulebook.md:{book.read_text().splitlines().index(BOOK) + 1}"


def notices(capsys) -> list[str]:
    return [p["notice"] for x in capsys.readouterr().out.splitlines() if x.startswith("{") and (p := json.loads(x))["room"] == MACHINE]


def test_a_rule_out_of_the_guides_shape_is_turned_back_naming_each_count_its_words_and_the_fix_with_nothing_written(tmp_path):
    y = daemon(tmp_path)
    r = rule(y, A, "NEVER open the old fetcher 🔴", "open web pages")
    assert r == {"ok": False, "error": words.RULE_SHAPE, "fix": [
        words.RULE_OFFENCE.format(name="a negative with no why", said='"NEVER"', fix=words.RULE_FIX["no why"]),
        words.RULE_OFFENCE.format(name="a shouted word", said='"NEVER"', fix=words.RULE_FIX["loud"]),
        words.RULE_OFFENCE.format(name="an alarm mark", said='"🔴"', fix=words.RULE_FIX["alarm"])]}
    assert r["fix"][0] == ('a negative with no why: "NEVER"; say what to do instead, or give it its reason with because, since or '
                           "so that, since a model generalises from a reason")
    assert not (y.memory.root / memory.RULES).exists() and fitted(y) == []        # nothing filed, nothing logged
    told = rule(y, A, "Answer with the first event 👍, since the day is planned from it.", "the day")
    assert told["fix"] == [words.RULE_OFFENCE.format(name="an example outside <example> tags", said='"👍"', fix=words.RULE_FIX["untagged"])]
    said = rule(y, A, 'Greet a guest by name, since it is warm: "hello Ada, come in" landed.', "greet a guest")["fix"]
    assert said == [words.RULE_OFFENCE.format(name="an example outside <example> tags", said='"hello Ada, come in", "landed"',
                                              fix=words.RULE_FIX["untagged"])]
    assert not (y.memory.root / memory.RULES).exists()


def test_the_counts_read_a_rule_with_its_examples_out_so_an_example_never_trips_one_and_never_hides_one(tmp_path):
    y = daemon(tmp_path)
    ex = ' <example>Asked "is it on", the reply was NEVER 👍, and it bounced.</example>'
    assert rule(y, A, "Open web pages with the browser, never the fetcher." + ex, "open web pages")["fix"] == [
        words.RULE_OFFENCE.format(name="a negative with no why", said='"never"', fix=words.RULE_FIX["no why"])]
    r = rule(y, A, "Open web pages with the browser, since it draws what the page shows." + ex, "open web pages")
    assert r["ok"] and r["cite"].endswith(":6")                         # the same example, inside its tags: filed
    assert shape.filing("- a job: do it, since it helps" + ex) == {} and shape.offenses("- x: never NEVER 🔴 👍", True) == list(shape.COUNTS)


def test_every_writer_of_a_rule_line_passes_the_check_the_tool_does(tmp_path):
    m = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state")
    r = memory.rule(m, A, "m1", "dates", "never guess a date")
    assert r["error"] == words.RULE_SHAPE and not (m.root / memory.RULES).exists()
    assert memory.rule(m, A, "m1", "dates", "Take a date from a file, since a guessed one reads as fact.")["ok"]
    assert memory.rule(m, A, "m2", "pages", "Say WIDGETS once", acronyms=shape.ACRONYMS | {"WIDGETS"})["ok"]      # [docs] acronyms


def test_the_case_lists_job_a_job_b_and_the_rulebook_line_and_files_nothing_on_the_first_call(tmp_path):
    y = install(tmp_path)
    before, (a, b, book) = (y.memory.root / memory.RULES).read_bytes(), cites(y)
    r = rule(y, A, C_SAID, "plans")
    assert r["ok"] is False and r["error"] == words.RULE_SAME and [x["cite"] for x in r["older"]] == [a, b, book]
    assert [x["text"] for x in r["older"]] == [f"- status lists: {A_SAID} ({A}, {AT}) [m:m-0]", f"- morning brief: {B_SAID} ({A}, {AT}) [m:m-0]", BOOK]
    assert all("mark, which this rule says no to" in x["shares"] for x in r["older"])
    assert r["error"].startswith("not filed yet: each line in `older` is live and speaks about the same thing, and one left saying ")
    assert (y.memory.root / memory.RULES).read_bytes() == before and fitted(y) == []


def test_the_second_call_settles_each_line_marks_the_rules_files_tells_the_keeper_and_logs_every_verdict(tmp_path, capsys):
    y = install(tmp_path)
    a, b, book = cites(y)
    older = [f"{a} | superseded: a plan now goes with no marks", f"{b} | kept: the morning brief is a status, not a plan",
             f"{book} | superseded, since a plan is a plain numbered list now"]
    r = rule(y, A, C_SAID, "plans", older=older)
    new = f"{a.rpartition(':')[0]}:8"
    assert r["ok"] and r["cite"] == new and r["replaced"] == [a] and r["kept"] == [b] and r["told"] == [book]
    rows = (y.memory.root / memory.RULES).read_text().splitlines()
    assert rows[5].endswith(f" [superseded by {new}] [m:m-0]") and memory.mentions.OLD not in rows[6] and rows[7].startswith(f"- plans: {C_SAID} (")
    assert fitted(y) == [words.RULE_SETTLED.format(job="plans", cite=new, old=a, verdict="superseded", why="a plan now goes with no marks"),
                         words.RULE_SETTLED.format(job="plans", cite=new, old=b, verdict="kept", why="the morning brief is a status, not a plan"),
                         words.RULE_SETTLED.format(job="plans", cite=new, old=book, verdict="superseded", why="since a plan is a plain numbered list now")]
    tell = f"{words.WORKING} " + words.RULE_TELL.format(old=book, line=BOOK, job="plans", cite=new, room=A, file="rulebook.md")
    assert notices(capsys) == [tell] and tell.splitlines()[-1] == "whoever keeps rulebook.md fixes it there"
    assert (y.private / "rulebook.md").read_text().endswith(BOOK + "\n")          # a hand-kept file: never written here
    assert rule(y, A, C_SAID, "plans", older=older)["cite"] == new                 # a retried call: nothing new, said once
    assert len(fitted(y)) == 3 and notices(capsys) == [] and (y.memory.root / memory.RULES).read_text().count("[superseded by") == 1


def test_a_second_call_missing_a_verdict_or_its_reason_is_turned_back_with_only_those_lines(tmp_path):
    y = install(tmp_path)
    a, b, book = cites(y)
    before = (y.memory.root / memory.RULES).read_bytes()
    r = rule(y, A, C_SAID, "plans", older=[f"{a} | superseded: a plan now goes with no marks", f"{b} | kept", f"{book} | gone: old"])
    assert r["error"] == words.RULE_SAME and [x["cite"] for x in r["older"]] == [b, book]
    assert (y.memory.root / memory.RULES).read_bytes() == before
    assert curate.settled([f" {a} |Superseded - the plan is plain now ", "no bar here", f"{b} | kept: x"]) == {a: ("superseded", "the plan is plain now")}


def test_a_rule_on_another_thing_files_at_once_and_a_word_more_than_five_lines_say_links_none(tmp_path):
    y = install(tmp_path)
    r = rule(y, A, "Open web pages with the example browser, since it draws what a page shows.", "open web pages")
    assert r["ok"] and "kept" not in r and "told" not in r                    # the example layer and the case: none on its thing
    rules = y.memory.root / memory.RULES
    rules.write_text(rules.read_text() + "".join(f"- report {i}: each report {i} line keeps its marks, since it is read fast. ({A}, {AT}) "
                                                 f"[m:m-{i}]\n" for i in range(curate.SAME_MOST)))
    assert rule(y, A, C_SAID, "plans")["ok"]          # "marks" said in eight lines names no one thing: what the check misses
    assert curate.same(y.private, y.memory, y.room_confs, "plans", C_SAID) == []


def test_a_line_naming_a_listed_job_is_listed_with_it_and_a_topic_of_the_twice_count_links_two_lines(tmp_path):
    y = install(tmp_path)
    rules = y.memory.root / memory.RULES
    rules.write_text(rules.read_text() + f"- weekly review: the review takes the status lists shape, since the owner knows it. ({A}, {AT}) [m:m-9]\n")
    found = curate.same(y.private, y.memory, y.room_confs, "plans", C_SAID)
    assert found[2]["cite"].endswith(":8") and found[2]["shares"] == f"it names status lists, the job of {found[0]['cite']}"
    tables = curate.same(y.private, y.memory, y.room_confs, "schedules", "A schedule goes as a table, since the hours line up.")
    assert [(x["cite"], x["shares"]) for x in tables] == [("rulebook.md:8", "the topic tables; it says no to table, which this rule names")]


def test_a_line_that_says_no_to_the_same_word_agrees_and_is_listed_only_through_another_link(tmp_path):
    y = install(tmp_path)
    rules, (a, b, book) = y.memory.root / memory.RULES, cites(y)
    agrees = "The digest is plain prose with no marks, since it is read aloud."
    rules.write_text(rules.read_text() + f"- weekly digest: {agrees} ({A}, {AT}) [m:m-8]\n")
    assert [x["cite"] for x in curate.same(y.private, y.memory, y.room_confs, "plans", C_SAID)] == [a, b, book]   # no clash on marks
    rules.write_text(rules.read_text().replace(agrees, "The digest names the week's plans in plain prose with no marks, since it is read aloud."))
    found = curate.same(y.private, y.memory, y.room_confs, "plans", C_SAID)
    assert [(x["cite"], x["shares"]) for x in found] == [(a, "it names mark, which this rule says no to"), (b, "it names mark, which this "
                                                         "rule says no to"), (f"{a[:-2]}:8", "it names this rule's job"), (book, "it names "
                                                         "mark, which this rule says no to")]


@pytest.mark.parametrize("bad", [True, False])
def test_curate_writes_no_line_a_filing_would_turn_back(tree, bad):  # noqa: F811
    priv, state, s = tree
    f = s.root / "rules.md"
    q = kinds(curate.rules(priv, state, s, False, NOW, post=lambda p: {"ok": True})["proposals"])["condense"]
    rec, was = json.loads((state / curate.PROPOSED).read_text()), f.read_bytes()
    if bad:
        edit = next(x for x in rec["proposals"] if x["n"] == q["n"])["edits"][0]
        edit[3] = f"{edit[3].split(': ', 1)[0]}: never guess a date (example-room, 2026-10-21 08:00:00+00:00) [m:1]"
        (state / curate.PROPOSED).write_text(json.dumps(rec))
        with pytest.raises(RuntimeError, match=rf"^{q['n']} would write a line with a negative with no why, which a rule's filing "):
            curate.apply(priv, state, DAY, str(q["n"]), now=NOW)
        assert f.read_bytes() == was
    else:
        assert curate.apply(priv, state, DAY, str(q["n"]), now=NOW)[0].endswith("applied") and f.read_bytes() != was
