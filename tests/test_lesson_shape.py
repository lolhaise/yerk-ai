"""Checks that a rule, a standing instruction the rooms follow, is kept as one lesson in the shape the Claude docs ask for
(shape.LESSON). The cases: each check on a rule of its own, each refusal naming the docs sentence it enforces off the
fetched page, the sentences pinned to their pages (shape.PINS) and still there on the install's own fetch, the whole store
page. Every value here is a placeholder."""
import os
from pathlib import Path

import pytest

from yerk_ai import memory, shape, words
from yerk_ai.config import EXAMPLE_PRIVATE, budgets

LIMITS = budgets()["shape"]
GOOD = "Open web pages with the example browser. Since it draws what a page shows, a fetch misses parts of it."


def docs_with(tmp_path: Path, **pages: str) -> Path:
    """A docs root holding each page given (slug = text), as docs.py keeps them."""
    (root := tmp_path / "docs" / "claude").mkdir(parents=True)
    for slug, text in pages.items():
        (root / f"{slug.replace('_', '-')}.md").write_text(text)
    return tmp_path / "docs"


def every_pin() -> dict[str, str]:
    """Each pinned page's text holding every sentence pinned to it, as a fetched page would."""
    out: dict[str, str] = {}
    for slug, said in shape.PINS.values():
        out[slug.replace("-", "_")] = out.get(slug.replace("-", "_"), "# A page\n\n") + said + "\n\n"
    return out


def test_a_lesson_with_its_summary_its_reason_and_a_brief_body_passes_every_check():
    assert shape.lesson("open web pages", GOOD, None, LIMITS) == {}
    assert shape.lesson("dates", "Take a date from a file, since a guessed one reads as fact.", None, LIMITS) == {}


def test_an_enumeration_of_cases_is_one_lesson_per_case_or_one_general_rule():
    cases = ("Write it as he would, since it goes in his name. When it is to his manager, keep it short; when it is to a peer, "
             "keep it warm; if it is a client, keep it formal.")
    assert shape.lesson("a message in his name", cases, None, LIMITS)["enumerated"] == ["3 cases (When, when, if)"]
    numbered = "Write it as he would, since it goes in his name. (1) a manager short (2) a peer warm (3) a client formal."
    assert list(shape.lesson("a message in his name", numbered, None, LIMITS)) == ["enumerated"]
    assert "enumerated" not in shape.lesson("a job", "Write it as he would, since it is his. When it is long, cut it; if it is a list, keep it.", None, LIMITS)


def test_a_summary_past_its_characters_a_rule_with_no_reason_a_long_body_and_a_long_sentence_are_each_named():
    long_first = "Open web pages with the example browser " + "and the example tool " * 6 + "since it draws the page."
    assert shape.lesson("pages", long_first, None, LIMITS)["summary"] == [f"a first sentence of {len(long_first)} characters"]
    assert shape.lesson("pages", "Open web pages with the example browser.", None, LIMITS)["no reason"] == ["no because, since, so that or why"]
    body = " ".join(["Since it draws the page, it sees what a fetch misses."] * 12)
    assert shape.lesson("pages", "Open web pages with the example browser. " + body, None, LIMITS)["long"] == [f"a body of {len(body)} characters"]
    cited = "Open web pages with the example browser, since it draws it. " + " ".join(["See daily/2026-01-02.md:41."] * 30)
    assert "long" not in shape.lesson("pages", cited, None, LIMITS)                 # a cite is not counted
    dense = "Open web pages with the browser, since " + "it draws the page and " * 10 + "more."
    assert shape.lesson("pages", dense, None, LIMITS)["dense"] == [f"a sentence of {len(dense.split())} words"]


def test_a_quote_or_an_example_the_day_log_holds_is_cited_and_a_cited_example_keeps_its_gloss_short(tmp_path):
    (daily := tmp_path / "daily").mkdir()
    said = "please write it the way I write to my manager, short and plain"
    (daily / "2026-01-02.md").write_text(f"# 2026-01-02\n\n- 09:00 owner: {said} [m:1]\n")
    pasted = f'Write it short, since he writes that way. <example>Landed: "{said}"</example>'
    assert shape.lesson("his messages", pasted, daily, LIMITS)["on record"] == ["an example the day log holds at daily/2026-01-02.md:3"]
    seen: dict = {}
    assert shape.on_record(said, daily, seen) == "daily/2026-01-02.md:3" and list(seen) == [daily / "2026-01-02.md"]     # read once
    assert shape.on_record("a line no day log holds at all here", daily, seen) == "" and len(seen) == 1
    quoted = f'Write it short, since he said "{said}".'
    assert shape.lesson("his messages", quoted, daily, LIMITS)["on record"] == ["a quote the day log holds at daily/2026-01-02.md:3"]
    cite = "Write it short, since he writes that way. <example>Landed, daily/2026-01-02.md:3, short to his manager</example>"
    assert shape.lesson("his messages", cite, daily, LIMITS) == {}
    chatty = "Write it short, since he writes that way. <example>Landed, daily/2026-01-02.md:3, he liked it a lot and said so twice</example>"
    assert shape.lesson("his messages", chatty, daily, LIMITS)["on record"] == ["a gloss of 9 words beside daily/2026-01-02.md:3"]
    unlogged = 'Write it short, since he writes that way. <example>Landed: "a reply he gave in a meeting no log ever held"</example>'
    assert shape.lesson("his messages", unlogged, daily, LIMITS) == {}             # no day-log line: it stays a quote


def test_the_gate_refuses_each_check_with_its_fix_and_the_docs_sentence_read_off_the_fetched_page(tmp_path):
    m = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state")
    docs_with(tmp_path / "state", **every_pin())
    r = memory.rule(m, "example-room", "m1", "his messages", "When to a manager keep it short; when to a peer keep it warm; if a client formal.")
    assert r["ok"] is False and r["error"] == words.RULE_SHAPE and not (m.root / memory.RULES).exists()
    said = shape.sentence("brief", m.docs)
    assert said == f'"{shape.PINS["brief"][1]}" (docs/claude/prompting-fable-5.md)'
    assert r["fix"][0] == words.RULE_OFFENCE.format(name="an enumeration of cases", said="3 cases (When, when, if)",
                                                    fix=words.LESSON_FIX["enumerated"].format(cap=3, sentence=said))
    assert [x.split(":")[0] for x in r["fix"]] == ["an enumeration of cases", "no reason given"]
    assert memory.rule(m, "example-room", "m2", "open web pages", GOOD)["ok"]


def test_every_pinned_sentence_is_on_its_page_and_a_page_that_moved_names_the_sentence(tmp_path):
    docs = docs_with(tmp_path, **every_pin())
    assert shape.pinned(docs) == []
    moved = every_pin()
    moved["prompting_fable_5"] = moved["prompting_fable_5"].replace("one lesson per file", "one lesson per note")
    docs = docs_with(tmp_path / "moved", **moved)
    assert shape.pinned(docs) == [("lesson", "prompting-fable-5", shape.PINS["lesson"][1])]
    assert shape.pinned(tmp_path / "none") == []                                   # a page not fetched yet is no finding
    assert set(slug for slug, _ in shape.PINS.values()) <= set(__import__("yerk_ai.config", fromlist=["x"]).DOCS_PAGES)


def test_the_installs_own_fetched_pages_still_hold_every_pinned_sentence():
    """Where `yerk docs refresh` has run, its pages: a docs change turns this red, and the doctor's daily run files the need."""
    state = Path(os.environ.get("YERK_STATE") or Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "yerk")
    have = {slug for slug, _ in shape.PINS.values() if (state / "docs" / "claude" / f"{slug}.md").is_file()}
    if not have:
        pytest.skip("no fetched docs on this box: `yerk docs refresh` fetches them")
    assert [x for x in shape.pinned(state / "docs") if x[1] in have] == []


def test_the_index_limits_are_read_off_the_code_memory_page_and_the_store_is_scored_whole(tmp_path):
    docs = docs_with(tmp_path, **every_pin())
    assert shape.index_limits(docs) == (200, 25_000)
    moved = every_pin()
    moved["code_memory"] = moved["code_memory"].replace("200 lines", "150 lines")
    assert shape.index_limits(docs_with(tmp_path / "m", **moved)) == (200, 25_000)      # moved: the pin's own numbers, and a finding
    m = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state")
    m.root.mkdir(parents=True, exist_ok=True)
    (m.root / memory.RULES).write_text("---\nclass: shared\n---\n# Rules\n\n"
                                       f"- open web pages: {GOOD} (example-room, 2026-01-02 09:00:00+00:00) [m:1]\n"
                                       "- dates: Never guess a date. (example-room, 2026-01-02 09:00:00+00:00) [m:2]\n")
    r = shape.store(m, LIMITS, docs)
    assert r == {"lessons": [(f"{memory.cite(m, m.root / memory.RULES)}:7", ["no reason"])], "index": [], "moved": []}
    found = shape.store_findings(r, LIMITS, docs)
    assert found == [f"rule shape: 1 lesson out of the docs' shape, first {memory.cite(m, m.root / memory.RULES)}:7: no reason; "
                     f"{shape.sentence('reason', docs)}"]
    (m.root / memory.RULES).write_text("".join(f"- job {i}: Do it, since it helps. (r, 2026-01-02 09:00:00+00:00) [m:{i}]\n" for i in range(201)))
    assert shape.store(m, LIMITS, docs)["index"] == ["201 lines, past 200"]


def test_the_doctors_daily_run_files_a_need_and_says_one_line_on_a_finding_and_is_silent_on_a_green_day(tmp_path, monkeypatch):
    from yerk_ai import doctor, needs
    monkeypatch.setattr(doctor, "rooms", lambda private: {"example-room": {}, "example-machine": {"role": "machine"}})
    private, state = tmp_path / "private", tmp_path / "state"
    __import__("shutil").copytree(EXAMPLE_PRIVATE, private)
    m = memory.settings(private, state)
    docs_with(state, **every_pin())
    m.root.mkdir(parents=True, exist_ok=True)
    rules = m.root / memory.RULES
    rules.write_text(f"- open web pages: {GOOD} (example-room, 2026-01-02 09:00:00+00:00) [m:1]\n")
    posts: list[dict] = []
    assert doctor.lessons(private, state, m, 1000.0, post=posts.append) == [] and posts == [] and needs.load(state) == []
    assert "in the shape: every pinned sentence" in (state / "docs" / "shape" / "lessons.md").read_text()
    rules.write_text(rules.read_text() + "- dates: Never guess a date. (example-room, 2026-01-02 09:00:00+00:00) [m:2]\n")
    out = doctor.lessons(private, state, m, 1000.0, post=posts.append)
    assert len(out) == 1 and out[0].startswith("rule shape: 1 lesson out of the docs' shape, first ") and "no reason" in out[0]
    [need] = needs.load(state)
    assert need["ability"] == doctor.LESSON_NEED and need["asks"][0]["tried"] == out[0]
    assert [p["machine"]["room"] for p in posts] == ["example-machine"] and posts[0]["machine"]["text"].startswith(out[0])
    assert need["asks"][0]["room"] == "example-machine"
    doctor.lessons(private, state, m, 1000.0 + 86400, post=posts.append)            # the next day: the same need, its count up
    assert len(needs.load(state)) == 1 and len(needs.load(state)[0]["asks"]) == 2
    assert doctor.lessons(private, state, m, 1000.0, dry=True) == out and len(needs.load(state)[0]["asks"]) == 2


def test_a_rule_that_turned_out_wrong_is_retired_its_line_emptied_and_its_words_kept_in_the_day_log(tmp_path):
    m = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state")
    log = memory.DailyLog(m.daily, m.tz)
    assert memory.rule(m, "example-room", "m1", "open web pages", GOOD)["ok"]
    assert memory.rule(m, "example-room", "m2", "dates", "Take a date from a file, since a guessed one reads as fact.")["ok"]
    rules = m.root / memory.RULES
    at, later = f"{memory.cite(m, rules)}:6", rules.read_text().splitlines()[6]
    assert memory.retire(m, log, "example-room", "m3", at, "")["ok"] is False                     # no why: nothing retired
    r = memory.retire(m, log, "example-room", "m3", at, "the example browser was the wrong tool for it")
    assert r["ok"] and r["retired"] == at and r["cite"] == f"{memory.cite(m, m.daily)}/{r['cite'].rsplit('/', 1)[1]}"
    rows = rules.read_text().splitlines()
    assert rows[5] == "" and rows[6] == later                         # emptied, so the next rule's cite stays the same
    day = (m.daily / r["cite"].rsplit("/", 1)[1].rsplit(":", 1)[0]).read_text()
    assert f'rule "open web pages" retired from {at}, since the example browser was the wrong tool for it: open web pages: {GOOD}' in day
    assert memory.retire(m, log, "example-room", "m4", at, "again")["error"].startswith(f"nothing retired: {at} is no live rule line")
    assert memory.retire(m, log, "example-room", "m4", "facts/x.md:1", "not a rule")["ok"] is False
