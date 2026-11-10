"""Checks the rule store (lessons.py), which keeps the rules the rooms follow as lessons in the Claude docs' shape. The cases: one lesson per file
with its class, then its one-line summary, then its body; rules.md the index of their summaries; a lesson replaced in place and a
files; a lesson replaced in place and a retired one's tombstone; the index at a session start, each body where a topic rule loaded, every body on
a session from before a conversion told nothing about it. Every value here is a placeholder."""
import json

from yerk_ai import context, lessons, memory
from yerk_ai.config import EXAMPLE_PRIVATE

WEB = "Open web pages with the example browser. Since it draws what a page shows, a fetch misses parts of it."
DATES = "Take a date from a file, since a guessed one reads as fact."
HALL = "Book the example hall a week ahead. Since the hall fills by the Friday before, a later ask finds it taken."


def store(tmp_path):
    """A converted store: its rules/ folder there, nothing filed yet."""
    m = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state")
    (m.root / lessons.DIR).mkdir(parents=True)
    return m, memory.DailyLog(m.daily, m.tz)


def test_a_rule_is_one_lesson_file_its_class_first_its_summary_then_its_body_and_the_index_lists_it(tmp_path):
    m, log = store(tmp_path)
    r = memory.rule(m, "example-room", "m1", "open web pages", WEB, log=log)
    assert r["ok"] and r["cite"] == "rules/open-web-pages.md" and r["replaced"] == []
    f = m.root / "rules" / "open-web-pages.md"
    text = f.read_text()
    assert text.startswith("---\nclass: shared\njob: open web pages\nfiled: example-room, ")
    front, body = text.split("\n---\n", 1)
    assert len(front) + 5 < lessons.FRONT_MAX and memory.file_class(f) == ("shared", "")      # every room may read it
    assert body.splitlines() == ["Open web pages with the example browser.", "Since it draws what a page shows, a fetch misses parts of it."]
    assert memory.rule(m, "example-room", "m2", "dates", DATES, log=log)["cite"] == "rules/dates.md"
    rows = (m.root / memory.RULES).read_text().splitlines()
    assert rows[:3] == ["---", "class: shared", "---"] and rows[-2:] == [
        "- Open web pages with the example browser. (rules/open-web-pages.md)", f"- {DATES} (rules/dates.md)"]


def test_a_lesson_is_replaced_in_place_its_old_words_kept_in_the_day_log_and_a_retired_one_leaves_a_tombstone(tmp_path):
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "open web pages", WEB, log=log)
    memory.rule(m, "example-room", "m2", "dates", DATES, log=log)
    again = "Open web pages with the example viewer. Since it draws what a page shows, a fetch misses parts of it."
    r = memory.rule(m, "example-room", "m3", "open web pages", again, log=log)
    assert r["ok"] and r["cite"] == "rules/open-web-pages.md" and r["replaced"] == ["rules/open-web-pages.md"]
    assert [x.slug for x in lessons.store(m.root)] == ["open-web-pages", "dates"]           # its place in the index kept
    assert lessons.store(m.root)[0].summary == "Open web pages with the example viewer."
    day = "\n".join(p.read_text() for p in m.daily.glob("*.md"))
    assert 'lesson rules/open-web-pages.md "open web pages" replaced in place, it said: Open web pages with the example browser.' in day
    assert memory.rule(m, "example-room", "m3", "open web pages", again, log=log)["replaced"] == []     # a retried call
    gone = memory.retire(m, log, "example-room", "m4", "rules/dates.md", "the file had no date")
    assert gone["ok"] and gone["retired"] == "rules/dates.md" and not (m.root / "rules" / "dates.md").exists()
    assert "dates" not in (m.root / memory.RULES).read_text() and lessons.retired(m.root)["dates"][1] == ""
    assert memory.retire(m, log, "example-room", "m5", "rules/dates.md", "again")["error"].startswith("nothing retired")
    assert memory.rule(m, "example-room", "m6", "dates", DATES, log=log)["cite"] == "rules/dates-2.md"   # a slug never names two


def test_a_cite_walks_through_tombstones_and_an_old_store_cite_to_the_live_lesson(tmp_path):
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "book the hall", HALL, log=log)
    (m.root / lessons.RETIRED).write_text("old-hall\t2026-11-01\tolder-hall\nolder-hall\t2026-11-02\tbook-the-hall\nloop\t2026-11-03\tloop\n")
    (m.root / lessons.CITES).write_text("rules.md:12\told-hall\nrules-topics.md:4\tbook-the-hall\n")
    assert lessons.live_slug(m.root, "rules/old-hall.md") == "book-the-hall"
    assert lessons.live_slug(m.root, "(supersedes memory/rules.md:12)") == "book-the-hall"
    assert lessons.live_slug(m.root, "loop") == "" and lessons.live_slug(m.root, "rules.md:99") == ""
    got = memory.read(m, "rules-topics.md:4")                    # the file is gone since the conversion: the lesson it became
    assert got["ok"] and got["moved"].startswith("rules-topics.md:4 is rules/book-the-hall.md now") and HALL.split(".")[0] in got["text"]


def test_an_index_near_code_memorys_limits_is_filed_and_the_filing_says_to_shorten_it(tmp_path):
    m, log = store(tmp_path)
    for i in range(185):
        memory.rule(m, "example-room", f"m{i}", f"job number {i}", DATES)
    r = memory.rule(m, "example-room", "m-last", "one more job", DATES)
    assert r["ok"] and (m.root / "rules" / "one-more-job.md").is_file()        # a reminder, never a refusal
    assert "191 lines of 200" in r["index"] and "merging lessons" in r["index"] and "docs/claude/code-memory.md" in r["index"]


def test_an_unconverted_store_keeps_its_one_line_rules(tmp_path):
    m = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state")
    r = memory.rule(m, "example-room", "m1", "open web pages", WEB)
    assert r["cite"].endswith("rules.md:6") and not (m.root / "rules").exists()


def test_a_session_start_loads_the_index_or_every_lesson_whole_uncapped_and_a_room_sees_what_it_saw(tmp_path):
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "open web pages", WEB, log=log)
    memory.rule(m, "example-room", "m2", "book the hall", HALL, log=log)
    hall = m.root / "rules" / "book-the-hall.md"
    hall.write_text(hall.read_text().replace("job: book the hall\n", "job: book the hall\nrooms: hall-room\n"))
    rules = m.root / memory.RULES
    capped, _ = context.rules_text({}, rules, None, "example-room")
    assert capped.splitlines()[1:] == ["- Open web pages with the example browser. (rules/open-web-pages.md)"]   # kept to its room
    whole, _ = context.rules_text({}, rules, "hall-room", "hall-room")
    assert whole.splitlines()[1:] == [f"- {WEB} (rules/open-web-pages.md)", f"- {HALL} (rules/book-the-hall.md)"]
    assert context.rules_text({"sealed": True}, rules, "hall-room") == ("", "")


def test_a_lessons_body_rides_on_a_message_naming_its_words_once_and_never_when_the_start_carries_it(tmp_path):
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "open web pages", WEB, log=log)
    memory.rule(m, "example-room", "m2", "book the hall", HALL, log=log)
    told, rules = tmp_path / "state" / "rooms" / "example-room" / context.TOLD, m.root / memory.RULES
    told.parent.mkdir(parents=True)
    text, jobs = context.topics(told, "s1", "example-room", rules, "can you book the hall for friday", 4000)
    assert text.splitlines() == [context.LESSONS_TOPICAL, f"- {HALL} (rules/book-the-hall.md)"] and jobs == ["book the hall"]
    assert context.topics(told, "s1", "example-room", rules, "book the hall again", 4000) == ("", [])        # once a session
    start = context.rules_text({}, rules, "example-room")[0]
    assert context.topics(told, "s2", "example-room", rules, "book the hall", 4000, start) == ("", [])        # uncapped: at start


def test_a_session_from_before_the_conversion_hears_only_a_lesson_changed_since(tmp_path):
    m, log = store(tmp_path)
    old = f"- book the hall: {HALL} (example-room, 2026-11-01 09:00:00-04:00) [m:m0]"
    rules, rdir = m.root / memory.RULES, tmp_path / "state" / "rooms" / "example-room"
    rules.write_text("---\nclass: shared\n---\n# Rules\n\n" + old + "\n")
    seen = rdir / "rules-seen.json"
    rdir.mkdir(parents=True)
    seen.write_text(json.dumps({"session": "s1", "seen": {"mechanics": context.mechanics(), "rules": rules.read_text().strip()}}))   # its look, before
    memory.rule(m, "example-room", "m1", "book the hall", HALL, log=log)
    memory.rule(m, "example-room", "m2", "open web pages", WEB, log=log)
    hall = lessons.parse(m.root / "rules" / "book-the-hall.md")
    lessons.keep(m.root, "2026-11-10", [(hall, [old])])
    (rules.parent / lessons.RETIRED).write_text("")
    web = lessons.parse(m.root / "rules" / "open-web-pages.md")
    out = context.news(seen, "s1", EXAMPLE_PRIVATE, {}, rules, 10_000, rdir / "work" / context.CHANGED, "example-room")
    assert out.splitlines() == [context.NEWS, f"new in rules.md: {web.whole[2:]}"]            # the hall's lesson is no news
    (rdir / "starts").mkdir()
    (rdir / "starts" / "s1.md").write_text(f"[from {rules}]\n{old}\n")
    told = rdir / context.TOLD
    assert context.topics(told, "s1", "example-room", rules, "book the hall", 4000) == ("", [])    # its start holds the rule


def test_a_lesson_is_one_chunk_in_the_recall_index_from_its_summary_its_front_matter_out(tmp_path):
    from yerk_ai import memindex
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "book the hall", HALL, log=log)
    f = m.root / "rules" / "book-the-hall.md"
    assert memindex.chunks_of(f.read_text(), str(f)) == [(6, 7, HALL.replace(". Since", ".\nSince"))]
    assert all("job:" not in b for _, _, b in memindex.chunks_of(f.read_text(), str(f)))
    backup = m.root / "rules-backup-2026-11-10" / "rules.md"
    backup.parent.mkdir()
    backup.write_text("- book the hall: an original\n")
    assert not m.scope.contains(backup.resolve()) and m.scope.contains(f.resolve())     # the originals stay out of recall


def chain(m, n: int) -> list[str]:
    """A rules file whose line 6 was replaced `n` times in turn, each by the next line; the last is live."""
    rows = [f"- book the hall: version {i}, since it fills. (example-room, 2026-11-01 09:00:00-04:00) [m:m{i}]" for i in range(n + 1)]
    rows = [memory.mark(x, f"tree/rules.md:{7 + i}") for i, x in enumerate(rows[:-1])] + rows[-1:]
    (m.root / memory.RULES).parent.mkdir(parents=True, exist_ok=True)
    (m.root / memory.RULES).write_text("---\nclass: shared\n---\n# Rules\n\n" + "\n".join(rows) + "\n")
    return rows


def test_a_history_line_walks_to_its_live_end_past_five_hops_and_a_loop_ends_the_walk(tmp_path):
    m = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state")
    rows = chain(m, 10)                                          # the real store's longest chain ran ten hops
    got = memory.live_first(m, {"ok": True, "hits": [{"cite": "tree/rules.md:6", "text": rows[0]}]})["hits"]
    assert [h["cite"] for h in got] == ["tree/rules.md:16", "tree/rules.md:6"] and "version 10" in got[0]["text"]
    rows[-1] = memory.mark(rows[-1], "tree/rules.md:6")             # a loop: the walk stops when a cite comes round again
    (m.root / memory.RULES).write_text("---\nclass: shared\n---\n# Rules\n\n" + "\n".join(rows) + "\n")
    assert len(memory.live_first(m, {"ok": True, "hits": [{"cite": "tree/rules.md:6", "text": rows[0]}]})["hits"]) <= 2


def test_on_a_converted_store_a_history_line_reaches_the_live_lesson_through_an_old_cite_and_a_tombstone(tmp_path):
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "book the hall", HALL, log=log)
    (m.root / lessons.RETIRED).write_text("old-hall\t2026-11-01\tbook-the-hall\n")
    (m.root / lessons.CITES).write_text("rules.md:12\told-hall\n")
    for said in ("a note (supersedes tree/rules.md:12) on the hall", "lesson rules/old-hall.md \"hall\" replaced in place, it said: x"):
        hit = {"cite": "tree/daily/2026-11-01.md:3", "text": said}
        got = memory.live_first(m, {"ok": True, "hits": [hit]})["hits"]
        assert got[0] == {"cite": "tree/rules/book-the-hall.md:6", "text": f"- {HALL} (rules/book-the-hall.md)"} and got[1] == hit
    own = {"cite": "tree/rules/book-the-hall.md:6", "text": HALL}
    assert memory.live_first(m, {"ok": True, "hits": [own]})["hits"] == [own]       # a lesson's own hit: nothing above it


def test_the_daily_check_scores_each_lesson_file_and_the_index_and_reports_an_index_past_its_limits(tmp_path):
    from yerk_ai import shape
    from yerk_ai.config import budgets
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "book the hall", HALL, log=log)
    (m.root / "rules" / "old-way.md").write_text("---\nclass: shared\njob: old way\n---\nOpen the example tool for it.\n")
    (m.root / memory.RULES).write_text((m.root / memory.RULES).read_text() + "- Open the example tool for it. (rules/old-way.md)\n")
    r = shape.store(m, budgets()["shape"])
    assert r["lessons"] == [("tree/rules/old-way.md:5", ["no reason"])] and r["index"] == []    # a hand-made lesson with no reason
    lines = (m.root / memory.RULES).read_text() + "".join(f"- A summary of lesson {i}. (rules/lesson-{i}.md)\n" for i in range(200))
    (m.root / memory.RULES).write_text(lines)
    assert shape.store(m, budgets()["shape"])["index"] == [f"{len(lines.splitlines())} lines, past 200"]   # the daily run keeps saying it
