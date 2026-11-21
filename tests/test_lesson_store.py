"""Checks the rule store (lessons.py), which keeps the rules the rooms follow as lessons in the Claude docs' shape. The cases: one lesson per file
under rules/, its front matter first with its class, then its one-line summary, then its body; rules.md the index, a line a job and its lessons'
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
    assert rows[:3] == ["---", "class: shared", "---"] and rows[-2:] == ["- open web pages (rules/open-web-pages.md)", "- dates (rules/dates.md)"]


def test_the_index_is_a_line_a_job_its_lessons_files_no_summary_read_back_in_its_order(tmp_path):
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "book the hall", HALL, log=log)
    memory.rule(m, "example-room", "m2", "dates", DATES, log=log)
    memory.rule_lesson(m, "example-room", "m3", "book the hall", HALL.replace("week", "month"), log, (), slug="book-the-hall-2")
    assert (m.root / memory.RULES).read_text().splitlines()[-2:] == [          # a second lesson of a job goes on its job's line
        "- book the hall (rules/book-the-hall.md, rules/book-the-hall-2.md)", "- dates (rules/dates.md)"]
    assert [x.slug for x in lessons.store(m.root)] == ["book-the-hall", "book-the-hall-2", "dates"]
    assert lessons.ROW.match("- book the hall (rules/book-the-hall.md, rules/book-the-hall-2.md)")[1] == "book the hall"
    capped, _ = context.rules_text({}, m.root / memory.RULES, None, "example-room")    # a capped start: each lesson's summary
    assert capped.splitlines()[1:] == ["- Book the example hall a week ahead. (rules/book-the-hall.md)",
                                       "- Book the example hall a month ahead. (rules/book-the-hall-2.md)", f"- {DATES} (rules/dates.md)"]
    memory.retire(m, log, "example-room", "m4", "rules/book-the-hall.md", "the hall closed")
    assert "- book the hall (rules/book-the-hall-2.md)" in (m.root / memory.RULES).read_text().splitlines()


def test_a_capped_start_carries_a_one_sentence_lesson_by_its_summary_while_the_index_on_disk_stays_a_line_a_job(tmp_path):
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "dates", DATES, log=log)            # one sentence: a summary, no body
    memory.rule(m, "example-room", "m2", "book the hall", HALL, log=log)
    hall = m.root / "rules" / "book-the-hall.md"
    hall.write_text(hall.read_text().replace("job: book the hall\n", "job: book the hall\nrooms: hall-room\n"))
    assert lessons.store(m.root)[0].body == ""
    rules = m.root / memory.RULES
    assert rules.read_text().splitlines()[-2:] == ["- dates (rules/dates.md)", "- book the hall (rules/book-the-hall.md)"]
    capped, _ = context.rules_text({}, rules, None, "example-room")
    assert capped.splitlines()[1:] == [f"- {DATES} (rules/dates.md)"]       # the lesson whole, as its summary is all of it
    whole, _ = context.rules_text({}, rules, "example-room", "example-room")
    assert "book-the-hall" not in capped and "book-the-hall" not in whole     # a lesson kept to another room is in neither
    assert whole.splitlines()[1:] == [f"- {DATES} (rules/dates.md)"]


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


def test_an_uncapped_installs_index_near_its_limits_is_a_measurement_never_a_filings_word_to_trim(tmp_path):
    import dataclasses

    from yerk_ai import shape
    from yerk_ai.config import budgets
    m, log = store(tmp_path)
    (tmp_path / "yerkd.toml").write_text("[limits]\nuncapped = true\n")
    m = dataclasses.replace(m, private=tmp_path)
    for i in range(186):
        r = memory.rule(m, "example-room", f"m{i}", f"job number {i}", DATES)
    assert r["ok"] and len((m.root / memory.RULES).read_text().splitlines()) == 191 and "index" not in r    # no word to merge or retire
    found = shape.store(m, budgets()["shape"])
    sized = [x for x in shape.store_findings(found, budgets()["shape"], whole=True) if x.startswith(shape.SIZED)]
    assert sized == [f"{shape.SIZED} rules.md is 191 lines of 200 and {(m.root / memory.RULES).stat().st_size:,} bytes of 25,000, "
                     "code-memory's limits for a capped start; a measurement, since this install is uncapped and nothing is trimmed to fit"]
    assert not [x for x in shape.store_findings(found, budgets()["shape"]) if x.startswith(shape.SIZED)]     # capped: as built


def test_a_final_restating_a_lessons_cite_is_a_filing_report():
    from yerk_ai import words
    assert words.FILING_REPORT.search("The hall is booked.\n\nThe rule is at rules/book-the-hall.md:6 now.")
    assert words.FILING_REPORT.search("The hall is booked (memory/rules/book-the-hall-2.md:1).")
    assert words.FILING_REPORT.search("Kept, rules.md:115.") and not words.FILING_REPORT.search("The rules/ folder holds the hall's.")
    assert words.FILING_REPORT.search("The hall is booked, and the rule is rules/book-the-hall.md.")   # a filing's own cite, bare
    assert not words.FILING_REPORT.search("rules.md is long") and not words.FILING_REPORT.search("rules-topics.md holds two.")


def test_a_converted_store_moves_no_held_rules_and_says_why(tmp_path):
    m, log = store(tmp_path)
    (m.root / memory.OVERFLOW).write_text("- dates: Take a date from a file. (example-room, 2026-01-02 09:00:00+00:00) [m:1]\n")
    n, why = memory.move_held(m, {})
    assert n == 0 and why.startswith("nothing moved: the store is converted") and (m.root / memory.OVERFLOW).is_file()
    assert not (m.root / memory.TOPIC_RULES).exists()


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


def test_the_morning_diff_counts_an_uncapped_rooms_lessons_as_that_room_is_sent_them(tmp_path):
    import shutil

    from yerk_ai import morning
    from yerk_ai.config import rooms
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "yerkd.toml").write_text("[limits]\nuncapped = true\n")
    m = memory.settings(private, state)
    (m.root / lessons.DIR).mkdir(parents=True)
    memory.rule(m, "example-room", "m1", "open web pages", WEB)
    memory.rule(m, "example-room", "m2", "book the hall", HALL)
    hall = m.root / "rules" / "book-the-hall.md"
    hall.write_text(hall.read_text().replace("job: book the hall\n", "job: book the hall\nrooms: example-room\n"))
    sent = context.rules_text(rooms(private)["example-room"], m.root / memory.RULES, "example-room", "example-room")[0]
    assert HALL in sent and morning.sizes(private, state)["example-room"]["rules"] == context.chars(sent)   # its own lesson, whole


def test_room_add_measures_the_new_rooms_start_with_the_installs_budgets_and_the_rooms_name(tmp_path):
    import shutil

    from yerk_ai import move
    from yerk_ai.config import load_toml, rooms
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "yerkd.toml").write_text("[limits]\nuncapped = true\n")
    m = memory.settings(private, state)
    (m.root / lessons.DIR).mkdir(parents=True)
    memory.rule(m, "example-room", "m1", "book the hall", HALL)
    hall = m.root / "rules" / "book-the-hall.md"
    hall.write_text(hall.read_text().replace("job: book the hall\n", "job: book the hall\nrooms: example-room\n"))
    start, bad = move.measure(private, rooms(private)["example-room"], "example-room", load_toml(private / "yerkd.toml"), m)
    sent = next(x.text for x in start if x.name == "rules")
    assert f"- {HALL} (rules/book-the-hall.md)" in sent.splitlines() and isinstance(bad, list)   # uncapped, kept to it: whole


def test_a_lessons_body_rides_on_a_message_naming_its_words_once_and_never_when_the_start_carries_it(tmp_path):
    m, log = store(tmp_path)
    memory.rule(m, "example-room", "m1", "open web pages", WEB, log=log)
    memory.rule(m, "example-room", "m2", "book the hall", HALL, log=log)
    told, rules = tmp_path / "state" / "rooms" / "example-room" / context.TOLD, m.root / memory.RULES
    told.parent.mkdir(parents=True)
    text, jobs = context.topics(told, "s1", "example-room", rules, "can you book the hall for friday", 4000)
    assert text.splitlines() == [context.LESSONS_TOPICAL, f"- {HALL} (rules/book-the-hall.md)"] and jobs == ["book the hall"]
    said = "\n".join(["[place] at home", text])                     # `yerk map` counts the block in the rules part, as a topic rule's
    assert context.hook_parts("UserPromptSubmit", said)["rules"] == context.chars(text)
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


# The conversion (conversion.py, `yerk curate --lessons`): a dry run, its apply from the exact file, its undo.
NOW = 1_762_790_400.0          # 2025-11-10 16:00 UTC, the dry run's clock; the day logs below sit inside its 14 days
FABLE2 = ("prompting-guide", "prompting-fable-5", "code-memory", "prompting-fable-5-2")     # a room on Claude Fable 5.2


def old_store(tmp_path):
    """An unconverted store: three live rules, one replaced line, a topic rule, an owner message that loads each."""
    from datetime import datetime, timezone
    m = memory.settings(EXAMPLE_PRIVATE, tmp_path / "state")
    m.root.mkdir(parents=True, exist_ok=True)
    t = "(example-room, 2025-11-10 09:00:00-04:00)"      # filed after the day log's lines below, which a cite may name
    rows = ["---", "class: shared", "---", "# Rules", "",
            memory.mark(f"- book the hall: Book the hall early, since it fills. {t} [m:m0]", "tree/rules.md:7"),
            f"- book the hall: {HALL} {t} [m:m1]",
            f"- open web pages: {WEB} {t} [m:m2]",
            f"- example ledger: Never post the example ledger. {t} [m:m3]"]
    (m.root / memory.RULES).write_text("\n".join(rows) + "\n")
    (m.root / memory.TOPIC_RULES).write_text("\n".join([*memory.TOPIC_HEAD, f"- dates: {DATES} [rooms: example-room] {t} [m:m4]"]) + "\n")
    m.daily.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(NOW - 86400, timezone.utc).date().isoformat()
    (m.daily / f"{day}.md").write_text(f"# {day}\n\n" + "\n".join(
        memory.format_line(datetime.fromtimestamp(NOW - 86400 + i, timezone.utc), "example-room", "r1", "owner", "", said, "private", f"o{i}")
        for i, said in enumerate(["can you book the hall for us", "open web pages for the example report", "the example ledger, post it?"])) + "\n")
    return m


def test_the_dry_run_writes_one_proposals_file_and_changes_nothing_else(tmp_path):
    from yerk_ai import conversion
    m = old_store(tmp_path)
    before = {p: p.read_bytes() for p in m.root.rglob("*") if p.is_file()}
    path = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=lambda p, tools: "", read=FABLE2)
    assert {p: p.read_bytes() for p in m.root.rglob("*") if p.is_file() and p != path} == before and path.parent.name == "rules-proposals"
    text = path.read_text()
    rec = conversion.loaded(path)
    assert [r["cite"] for r in rec["rules"]] == ["tree/rules.md:7", "tree/rules.md:8", "tree/rules.md:9", "tree/rules-topics.md:3"]
    ledger = rec["rules"][2]
    assert ledger["lessons"][0]["body"] == 'It holds for the job "example ledger".'          # no exception added: the owner gave none
    assert [d["kind"] for d in ledger["done"]] == ["scope added", "no reason on file", "still failing"]
    assert "after 2 rounds back to the model: no proposal came back for it; split as it stands" in text
    assert rec["ready"] is False and "the gate refuses" not in text and "- no reason on file (listed for the owner's side" in text
    assert rec["why"] == ["1 lesson refused only for a reason no file holds, each listed (a row in rules-proposals/reasons.tsv supplies one)"]
    assert rec["ends"]["tree/rules.md:6"] == "tree/rules.md:7"                         # the replaced line leads to its live rule
    assert "lost by the split" not in text and "1 owner message of the last 14 days load it" in text


def test_the_dry_run_reads_the_fable_5_2_page_whatever_the_rooms_run_and_is_not_ready_while_its_index_is_past_a_limit(tmp_path, monkeypatch):
    from yerk_ai import conversion, shape
    m = old_store(tmp_path)
    rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=lambda p, tools: ""))
    assert rec["read"] == list(FABLE2) and rec["index"] == [9, len(lessons.index_text([conversion.lesson_of(x) for r in rec["rules"]
                                                                                       for x in r["lessons"]]).encode()), 200, 25_000]
    assert not [x for x in rec["why"] if "index" in x]
    monkeypatch.setattr(shape, "index_limits", lambda docs=None: (6, 150))
    rec = conversion.loaded(path := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=lambda p, tools: ""))
    assert rec["ready"] is False and rec["why"][-1] == (f"the index it would write is past code-memory's limits: 9 lines, 3 past 6 and "
                                                        f"{rec['index'][1]:,} bytes, {rec['index'][1] - 150:,} past 150")
    assert f"The index it would write: 9 lines and {rec['index'][1]:,} bytes, against code-memory's 6 and 150." in path.read_text()


def test_an_apply_takes_the_exact_file_keeps_the_originals_and_an_old_cite_finds_its_lesson_and_undo_puts_them_back(tmp_path):
    from yerk_ai import conversion
    m = old_store(tmp_path)
    rows = (m.root / memory.RULES).read_text().replace("Never post the example ledger.", "Keep the example ledger to the owner, since it holds his accounts.")
    (m.root / memory.RULES).write_text(rows)
    originals = {n: (m.root / n).read_bytes() for n in (memory.RULES, memory.TOPIC_RULES)}
    path = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=lambda p, tools: "")
    assert conversion.loaded(path)["ready"] is True
    path.write_text(path.read_text().replace("Original:", "Originally:", 1))
    try:
        conversion.apply(EXAMPLE_PRIVATE, tmp_path / "state", m, path)
        raise AssertionError("an edited file applied")
    except RuntimeError as e:
        assert "not the file a dry run wrote" in str(e)
    path = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=lambda p, tools: "")
    out = conversion.apply(EXAMPLE_PRIVATE, tmp_path / "state", m, path)
    assert out[0].startswith("4 lessons from 4 rules") and lessons.converted(m.root) and not (m.root / memory.TOPIC_RULES).exists()
    day = conversion.loaded(path)["day"]
    assert (m.root / f"rules-backup-{day}" / memory.RULES).read_bytes() == originals[memory.RULES]
    assert [x.slug for x in lessons.store(m.root)] == ["book-the-hall", "open-web-pages", "example-ledger", "dates"]
    assert lessons.store(m.root)[3].rooms == ("example-room",) and lessons.store(m.root)[3].cls == "private"
    for old in ("tree/rules.md:6", "tree/rules.md:7"):                # a replaced line and its live one: the same lesson
        assert lessons.live_slug(m.root, old) == "book-the-hall"
    assert memory.read(m, "rules-topics.md:3")["moved"].startswith("rules-topics.md:3 is rules/dates.md now")
    whole, _ = context.rules_text({}, m.root / memory.RULES, "example-room", "example-room")    # uncapped: every rule it had
    assert len(whole.splitlines()) == 5 and HALL in whole and DATES in whole
    assert conversion.undo(m, day)[0].startswith("4 lessons removed")
    assert {n: (m.root / n).read_bytes() for n in originals} == originals and not lessons.converted(m.root)


def test_a_refused_or_dropped_proposal_goes_back_with_its_findings_one_still_failing_is_listed_and_no_split_loses_a_trigger(tmp_path):
    from yerk_ai import conversion
    m = old_store(tmp_path)
    rule = "Open pages in the Browserly viewer, which draws what a page shows."     # a reason to a reader, none to the gate
    (m.root / memory.RULES).write_text((m.root / memory.RULES).read_text().replace(WEB, rule))
    day = next(m.daily.glob("*.md"))
    day.write_text(day.read_text().replace("open web pages for the example report", "is Browserly up"))
    asked: list[str] = []
    again = {"cite": "tree/rules.md:8", "lessons": [{"summary": "Open pages in the example viewer.", "body": "Use it since it draws "
             "what a page shows.", "reason": {"said": "draws what a page shows", "from": "the rule's own words"}}],
             "rewritten": [{"said": rule, "as": "Open pages in the example viewer. Use it since it draws what a page shows."}]}

    def model(prompt: str, tools: str) -> str:
        asked.append(prompt := prompt.lstrip())
        if prompt.startswith("You check the lessons"):
            return "KEEP tree/rules.md:8\nDROP tree/rules.md:9 | it adds a case"
        if prompt.startswith("The gate refused"):
            return json.dumps(again) if "tree/rules.md:8 (job" in prompt else ""
        return "\n".join(json.dumps(x) for x in (
            {"cite": "tree/rules.md:8", "lessons": [{"summary": "Open pages in the example viewer.", "body": "It draws what a page shows."}],
             "rewritten": [{"said": rule, "as": "Open pages in the example viewer. It draws what a page shows."}]},
            {"cite": "tree/rules.md:9", "lessons": [{"summary": "Keep the example ledger in.", "body": ""}]}))
    path = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=model, read=FABLE2)
    rec, text = conversion.loaded(path), path.read_text()
    backs = [x for x in asked if x.startswith("The gate refused")]
    assert len(backs) == 2 and "tree/rules.md:9" in backs[1] and "tree/rules.md:8 (job" not in backs[1]       # the failing ones only
    assert "no reason given" in backs[0] and "- the reviewer dropped it: it adds a case" in backs[0]
    web, ledger = rec["rules"][1], rec["rules"][2]
    assert web["lessons"][0]["gate"] == [] and web["missing"] == [] and {"kind": "reason added", "said": "draws what a page shows",
                                                                         "from": "the rule's own words"} in web["done"]
    assert "browserly" in web["lessons"][0]["on"] and rec["lost"] == {}            # the rule's words kept: its trigger holds
    assert ledger["lessons"][0]["summary"] == "Keep the example ledger in."      # its last proposal, never split in silence
    still = next(d["why"] for d in ledger["done"] if d["kind"] == "still failing")
    assert still.startswith("after 2 rounds back to the model: ") and still.endswith(" / the reviewer dropped it: it adds a case")
    assert 'not accounted for: "Never post the example ledger."' in still and f"- still failing ({still})" in text


def test_an_example_lands_cited_a_quote_across_sentence_ends_is_one_and_a_supplied_reason_is_used_and_listed(tmp_path):
    from yerk_ai import conversion
    m = old_store(tmp_path)
    day = next(m.daily.glob("*.md"))
    bounced = "the rota came on a wednesday and nobody could plan the week"
    day.write_text(day.read_text() + "\n".join([
        memory.format_line(datetime_at(NOW - 3600), "example-room", "r1", "owner", "", bounced, "private", "o8"),
        memory.format_line(datetime_at(NOW - 3000), "example-room", "r1", "owner", "", "the example ledger holds my accounts", "private", "o9")]) + "\n")
    n = len(day.read_text().splitlines())
    quoted = 'Reply plainly when he asks "Why? What now?" about a plan, since a long answer buries it.'
    rows = (m.root / memory.RULES).read_text().replace(HALL, f'Post the rota on Monday, since the team plans from it. <example>Bounced: '
                                                         f'"{bounced}"</example>').replace(WEB, quoted)
    (m.root / memory.RULES).write_text(rows)
    (m.root / "rules-proposals").mkdir()
    (m.root / "rules-proposals" / "reasons.tsv").write_text(f"# cite, line, reason\ntree/rules.md:9\tdaily/{day.name}:{n}\tholds my accounts\n")

    def model(prompt: str, tools: str) -> str:
        if (prompt := prompt.lstrip()).startswith("You check the lessons"):
            return "KEEP tree/rules.md:8\nKEEP tree/rules.md:9"
        if prompt.startswith("The gate refused"):
            return ""
        assert f"Reason on file, from daily/{day.name}:{n}: holds my accounts" in prompt and f'Example 1: Bounced: "{bounced}"' in prompt
        return "\n".join(json.dumps(x) for x in (
            {"cite": "tree/rules.md:8", "lessons": [{"summary": "Reply plainly to a question about a plan.", "body": "Do so since a long answer buries it."}],
             "rewritten": [{"said": quoted, "as": "Reply plainly to a question about a plan."}]},
            {"cite": "tree/rules.md:9", "lessons": [{"summary": "Keep the example ledger in the owner's room.", "body": "Never post it, "
             "since it holds my accounts.", "reason": {"said": "holds my accounts", "from": f"daily/{day.name}:{n}"}}]}))
    rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=model, read=FABLE2))
    hall, web, ledger = rec["rules"][:3]
    assert hall["lessons"][0]["body"].endswith(f"<example>Bounced, daily/{day.name}:{n - 1}</example>") and hall["missing"] == []
    assert any(d["kind"] == "example cited" and d["at"] == f"daily/{day.name}:{n - 1}" for d in hall["done"])
    assert web["missing"] == [] and web["lessons"][0]["gate"] == []          # the quote's halves no longer read as two sentences
    assert not conversion.refused(ledger["lessons"][0]["gate"], False, True) and not any(d["kind"] == "no reason on file" for d in ledger["done"])
    assert {"kind": "reason supplied", "said": "holds my accounts", "from": f"daily/{day.name}:{n}, named in rules-proposals/reasons.tsv"} in ledger["done"]


def ledger_store(tmp_path):
    """old_store with a reason on file for its ledger rule, the one rule out of the shape, and a proposal for it that passes."""
    m = old_store(tmp_path)
    day = next(m.daily.glob("*.md"))
    day.write_text(day.read_text() + memory.format_line(datetime_at(NOW - 3000), "example-room", "r1", "owner", "",
                                                        "the example ledger holds my accounts", "private", "o9") + "\n")
    n = len(day.read_text().splitlines())
    (m.root / "rules-proposals").mkdir()
    (m.root / "rules-proposals" / "reasons.tsv").write_text(f"tree/rules.md:9\tdaily/{day.name}:{n}\tholds my accounts\n")
    prop = {"cite": "tree/rules.md:9", "lessons": [{"summary": "Keep the example ledger in the owner's room.", "body": "Never post it, "
            "since it holds my accounts.", "reason": {"said": "holds my accounts", "from": f"daily/{day.name}:{n}"}}],
            "rewritten": [{"said": "Never post the example ledger.", "as": "Keep the example ledger in the owner's room. Never post it."}]}
    return m, prop


def test_a_proposal_the_reviewer_still_drops_after_the_last_round_makes_the_file_not_ready_though_its_lessons_pass(tmp_path):
    from yerk_ai import conversion
    m, prop = ledger_store(tmp_path)
    for verdict, ready in (("DROP tree/rules.md:9 | it says more than the rule", False), ("KEEP tree/rules.md:9", True)):
        def model(prompt: str, tools: str, verdict=verdict) -> str:
            return verdict if prompt.lstrip().startswith("You check the lessons") else json.dumps(prop)
        rec = conversion.loaded(path := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=model, read=FABLE2))
        ledger = rec["rules"][2]
        assert not conversion.refused(ledger["lessons"][0]["gate"], False, True) and ledger["missing"] == [] and rec["lost"] == {}
        assert rec["no_exception"] == {"example-ledger": "tree/rules.md:9"}           # its lessons pass, its never as they wrote it
        assert rec["ready"] is ready
        if not ready:
            assert rec["why"] == ["1 rule the reviewer dropped after the last round, each listed"]
            assert {"kind": "reviewer dropped", "why": "after the last round: it says more than the rule"} in ledger["done"]
            assert "- reviewer dropped (after the last round: it says more than the rule)" in path.read_text()


def test_the_accounting_reads_a_sentence_whole_across_an_abbreviation_in_brackets():
    from yerk_ai import conversion
    said = "Book the example hall (E. Vol. 12, rooms 4(b) and 5) a week ahead, since it fills (see the rota.) Post the rota."
    lesson = [{"summary": "Book the example hall a week ahead.", "body": "Do so since it fills. Post the rota."}]
    whole = "Book the example hall (E. Vol. 12, rooms 4(b) and 5) a week ahead, since it fills (see the rota.)"
    listed = [{"kind": "rewritten", "said": whole, "as": "Book the example hall a week ahead, since it fills."}]
    assert conversion.accounted({"said": said}, lesson, listed) == []        # one sentence, its brackets' stops inside it
    assert conversion.accounted({"said": said}, lesson, []) == [whole]       # and a bracket's own last stop still ends one


SHOP = {"example ledger": ("post", "holds my accounts"), "example till": ("open", "holds the float"), "example rota": ("move", ""),
        "example shelf": ("empty", "holds the stock"), "example key": ("lend", "opens the hall"), "example lamp": ("dim", "")}
AT_SHOP = "(example-room, 2025-11-01 09:00:00-04:00)"


def shop_store(tmp_path):
    """old_store with five rules out of the shape at tree/rules.md:6 to :10 (never do a thing), a reason on file for four."""
    m = old_store(tmp_path)
    jobs = ["example ledger", "example till", "example rota", "example shelf", "example key"]
    (m.root / memory.RULES).write_text("\n".join(["---", "class: shared", "---", "# Rules", "", *(
        f"- {j}: Never {SHOP[j][0]} the {j}. {AT_SHOP} [m:s{i}]" for i, j in enumerate(jobs))]) + "\n")
    (m.root / "rules-proposals").mkdir()
    for i, j in enumerate(jobs):
        if SHOP[j][1]:
            reason(m, f"tree/rules.md:{6 + i}", f"the {j} {SHOP[j][1]}", SHOP[j][1])
    return m


def reason(m, cite: str, line: str, why: str) -> None:
    """An owner's day-log line, and a row of reasons.tsv naming it for `cite`."""
    day = next(m.daily.glob("*.md"))
    day.write_text(day.read_text() + memory.format_line(datetime_at(NOW - 3000), "example-room", "r1", "owner", "", line, "private", "s9") + "\n")
    with (m.root / "rules-proposals" / "reasons.tsv").open("a") as f:
        f.write(f"{cite}\tdaily/{day.name}:{len(day.read_text().splitlines())}\t{why}\n")


def shop_model(asked: set[str], drop: set[str]):
    """The model pass's stand-in: each rule's lesson in the gate's shape with its reason on file, none for a rule with none;
    the knock-down drops the cites in `drop`; `asked` gathers each cite a first proposal is asked for."""
    import re

    def model(prompt: str, tools: str) -> str:
        rules = re.findall(r"^=== (\S+) \(job: ([^)]*)\)\n(.*)$", prompt := prompt.lstrip(), re.M)
        if prompt.startswith("You check the lessons"):
            return "\n".join(f"DROP {c} | it adds a case" if c in drop else f"KEEP {c}" for c, *_ in rules)
        if prompt.startswith("The gate refused"):
            return ""
        asked.update(c for c, *_ in rules)
        return "\n".join(json.dumps({"cite": c, "lessons": [{"summary": f"Keep the {j} as it is.", "body": f"Never {SHOP[j][0]} it"
                          + (f", since it {SHOP[j][1]}." if SHOP[j][1] else "."), **({"reason": {"said": SHOP[j][1], "from": "the reason on file"}}
                          if SHOP[j][1] else {})}], "rewritten": [{"said": said, "as": "the lesson"}]}) for c, j, said in rules)
    return model


def test_a_dry_run_from_an_earlier_file_keeps_each_rule_that_passed_with_no_model_call_and_redoes_the_rest(tmp_path):
    import shutil

    from yerk_ai import conversion
    m, asked = shop_store(tmp_path), set[str]()
    first = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, {"tree/rules.md:9"}), read=FABLE2)
    (tmp_path / "elsewhere").mkdir()
    shutil.copy(first, earlier := tmp_path / "elsewhere" / "earlier.md")                 # a file from another folder
    was = {x["cite"]: x for x in conversion.loaded(earlier)["rules"]}
    assert asked == {f"tree/rules.md:{n}" for n in range(6, 11)} and was["tree/rules.md:8"]["lessons"][0]["gate"]   # no reason: refused
    assert all(not conversion.refused(x["gate"], False, True) for n in (6, 7, 9, 10) for x in was[f"tree/rules.md:{n}"]["lessons"])
    assert not was["tree/rules.md:6"]["missing"]
    assert any(d["kind"] == "reviewer dropped" for d in was["tree/rules.md:9"]["done"])
    rows = (m.root / memory.RULES).read_text().replace("- example till: Never open the example till.", "- example key: Never open the example till at night.")
    (m.root / memory.RULES).write_text(rows + f"- example lamp: Never dim the example lamp. {AT_SHOP} [m:s8]\n")    # :7 changed, :11 new
    reason(m, "tree/rules.md:6", "the example ledger is the only book", "is the only book")                       # a new row for :6
    asked.clear()
    rec = conversion.loaded(path := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, set()),
                                                       read=FABLE2, earlier=earlier))
    now = {x["cite"]: x for x in rec["rules"]}
    assert asked == {"tree/rules.md:6", "tree/rules.md:7", "tree/rules.md:8", "tree/rules.md:9", "tree/rules.md:11"}   # :10 costs no call
    assert now["tree/rules.md:10"]["lessons"] == was["tree/rules.md:10"]["lessons"] and now["tree/rules.md:10"]["lessons"][0]["slug"] == "example-key"
    assert {"kind": "kept from an earlier file", "why": "earlier.md, as it made it: its lessons word for word, no model call"} in now["tree/rules.md:10"]["done"]
    assert now["tree/rules.md:7"]["lessons"][0]["slug"] == "example-key-2"          # a redone rule takes no slug a kept one holds
    assert any(d["kind"] == "kept from an earlier file" for d in now["tree/rules-topics.md:3"]["done"])     # one in the shape too
    assert rec["from"] == {"path": str(earlier), "kept": 2, "redone": 5, "why": {"another cause": 5}} and now["tree/rules.md:6"]["reasons"][-1].endswith("\tis the only book")
    assert f"Kept from {earlier}: 2 rules word for word, with no model call; 5 redone." in path.read_text().splitlines()[:8]
    assert "1 rule the reviewer dropped after the last round, each listed" not in rec["why"]       # :9 redone, and kept this time


def test_a_none_row_carries_a_lesson_refused_for_a_reason_alone_named_and_excuses_no_other_cause(tmp_path):
    from yerk_ai import conversion
    m = old_store(tmp_path)

    def run():
        path = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=lambda p, tools: "", read=FABLE2)
        return conversion.loaded(path), path.read_text()
    assert run()[0]["why"] == ["1 lesson refused only for a reason no file holds, each listed (a row in rules-proposals/reasons.tsv supplies one)"]
    (m.root / "rules-proposals" / "reasons.tsv").write_text("# the owner's side looked: no file holds one\ntree/rules.md:9\tnone\n")
    rec, text = run()
    ledger = rec["rules"][2]
    assert rec["ready"] and rec["why"] == [] and ledger["lessons"][0]["gate"] and ledger["reasons"] == ["none"]
    assert rec["no_reason"] == {"example-ledger": "tree/rules.md:9"} and "not counted in the verdict: 1 lesson, rules/example-ledger.md." in text
    assert next(d for d in ledger["done"] if d["kind"] == "no reason on file, as the owner's side says")["said"] == ledger["lessons"][0]["summary"]
    rows = (m.root / memory.RULES).read_text()
    (m.root / memory.RULES).write_text(rows.replace("Never post the example ledger.", "Never post the example ledger. \U0001F44D"))
    rec, _ = run()                                                       # an example outside its tags: a cause a none row never excuses
    assert not rec["ready"] and rec["why"] == ["1 lesson the gate refuses"] and rec["no_reason"] == {}


def test_the_apply_lists_each_lesson_carried_with_no_reason_and_a_replacement_or_a_retire_takes_it_off(tmp_path):
    from yerk_ai import conversion
    m = old_store(tmp_path)
    (m.root / memory.RULES).write_text((m.root / memory.RULES).read_text().replace(WEB, "Open web pages with the example browser."))
    (m.root / "rules-proposals").mkdir()
    (m.root / "rules-proposals" / "reasons.tsv").write_text("tree/rules.md:8\tnone\ntree/rules.md:9\tnone\n")
    path = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=lambda p, tools: "")
    rec = conversion.loaded(path)
    assert rec["ready"] and rec["no_reason"] == {"open-web-pages": "tree/rules.md:8", "example-ledger": "tree/rules.md:9"}
    conversion.apply(EXAMPLE_PRIVATE, tmp_path / "state", m, path)
    assert lessons.no_reason(m.root) == {"open-web-pages": (rec["day"], "tree/rules.md:8"), "example-ledger": (rec["day"], "tree/rules.md:9")}
    conversion.undo(m, rec["day"])
    assert not (m.root / lessons.NO_REASON).exists()
    conversion.apply(EXAMPLE_PRIVATE, tmp_path / "state", m, path := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW,
                                                                                        model=lambda p, tools: ""))
    log = memory.DailyLog(m.daily, m.tz)
    assert memory.rule(m, "example-room", "m9", "example ledger", "Never post the example ledger.", log=log)["ok"] is False   # the write gate
    r = memory.rule(m, "example-room", "m9", "example ledger", "Keep the example ledger to the owner, since it holds his accounts.", log=log)
    assert r["ok"] and r["replaced"] == ["rules/example-ledger.md"] and list(lessons.no_reason(m.root)) == ["open-web-pages"]
    assert memory.retire(m, log, "example-room", "m10", "rules/open-web-pages.md", "the browser went")["ok"] and lessons.no_reason(m.root) == {}


def test_a_dry_run_from_an_earlier_file_redoes_each_rule_that_file_can_no_longer_vouch_for(tmp_path):
    import shutil

    from yerk_ai import conversion
    m, asked, key = shop_store(tmp_path), set[str](), "tree/rules.md:10"
    first = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, set()), read=FABLE2)
    shutil.copy(first, earlier := tmp_path / "earlier.md")

    def again(change=None) -> set[str]:
        """The cites asked of the model on a run from `earlier`, or from a copy of it whose record `change` rewrote."""
        path = earlier
        if change:
            change(rec := conversion.loaded(earlier))
            (path := tmp_path / "rewritten.md").write_text(conversion.render(rec))
        asked.clear()
        conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, set()), read=FABLE2, earlier=path)
        return set(asked)

    def its(rec: dict) -> dict:
        return next(r for r in rec["rules"] if r["cite"] == key)

    def long(rec: dict) -> None:                 # the file says its lesson passed; the gate as an apply reads it today refuses it
        its(rec)["lessons"][0]["body"] += " " + " ".join(f"Keep copy {i} of it with the owner, since it opens the hall." for i in range(12))

    def other(rec: dict) -> None:                # the file's lesson no longer holds the rule's own sentence, and lists nothing
        its(rec)["lessons"][0].update(summary="Keep the example shelf tidy.", body="Sweep it, since it holds the stock.")
        its(rec)["done"] = [d for d in its(rec)["done"] if d["kind"] != "rewritten"]

    def lost(rec: dict) -> None:                 # the file says the rule lost a trigger
        rec["lost"] = {key: ["example key"]}
    assert key not in again()                                                   # as the file stands, the rule is kept
    assert [key in again(change) for change in (long, other, lost)] == [True, True, True]
    (m.root / lessons.RETIRED).write_text("example-key\t2025-11-01\t\n")        # its lesson's slug retired since
    assert key in again()
    (m.root / lessons.RETIRED).unlink()
    rows = (m.root / memory.RULES).read_text()
    (m.root / memory.RULES).write_text(rows.replace("- example key: Never lend", "- example lamp: Never lend"))
    assert key in again() and "tree/rules.md:6" not in asked                    # its job renamed, its words the same: its line changed


def test_a_none_rows_lesson_is_not_sent_back_for_its_reason_so_a_later_dry_run_keeps_it_and_the_apply_needs_the_row(tmp_path):
    import shutil

    import pytest

    from yerk_ai import conversion
    m, prompts = old_store(tmp_path), list[str]()
    (m.root / "rules-proposals").mkdir()
    (m.root / "rules-proposals" / "reasons.tsv").write_text("tree/rules.md:9\tnone\n")
    prop = {"cite": "tree/rules.md:9", "lessons": [{"summary": "Keep the example ledger in the owner's room.", "body": "Never post it."}],
            "rewritten": [{"said": "Never post the example ledger.", "as": "Keep the example ledger in the owner's room. Never post it."}]}

    def model(prompt: str, tools: str) -> str:
        prompts.append(prompt.lstrip())
        return "KEEP tree/rules.md:9" if prompts[-1].startswith("You check the lessons") else json.dumps(prop)
    rec = conversion.loaded(first := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=model, read=FABLE2))
    ledger = rec["rules"][2]
    assert rec["ready"] and ledger["lessons"][0]["gate"] and not any(d["kind"] == "still failing" for d in ledger["done"])
    assert prompts and not any(p.startswith("The gate refused") for p in prompts)      # its missing reason is not asked for again
    shutil.copy(first, earlier := tmp_path / "earlier.md")
    prompts.clear()
    rec = conversion.loaded(path := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=model, read=FABLE2, earlier=earlier))
    assert prompts == [] and rec["from"]["kept"] == len(rec["rules"]) and rec["ready"]   # kept with the rest, no model call
    assert rec["no_reason"] == {"example-ledger": "tree/rules.md:9"}                    # and still listed
    (m.root / "rules-proposals" / "reasons.tsv").write_text("# the row was taken back\n")
    with pytest.raises(RuntimeError, match="example-ledger no longer passes the gate"):
        conversion.apply(EXAMPLE_PRIVATE, tmp_path / "state", m, path)                  # its pass holds only while the row is there
    assert not (m.root / lessons.DIR).exists()


def test_the_command_hands_its_from_file_to_the_dry_run(tmp_path, monkeypatch):
    from yerk_ai import cli, conversion
    got: list[object] = []
    monkeypatch.setattr(conversion, "dry_run", lambda private, state, s, **kw: got.append(kw) or tmp_path / "proposals.md")
    args = ["--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path / "state")]
    assert cli.main(["curate", "--lessons", "--from", str(tmp_path / "earlier.md"), *args]) == 0
    assert cli.main(["curate", "--lessons", *args]) == 0
    assert got == [{"earlier": tmp_path / "earlier.md"}, {"earlier": None}]


def datetime_at(t: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc)


def test_a_lesson_keeps_each_smallest_word_set_that_loads_its_rule_today_else_each_word_alone():
    from yerk_ai import conversion
    line = "- tidy the shelf: Keep the Quillby list by the door, since it is read there. (example-room, 2025-11-01 09:00:00-04:00)"
    assert sorted(memory.triggers(line)) == ["quill", "shelf", "tidy"]
    assert conversion.keeps(line, ({"tidy": 1.0, "shelf": 2.5, "quill": 4.0}, 4.0)) == ["quillby", "shelf tidy"]   # ride 3.0
    assert conversion.keeps(line, ({"tidy": 1.1, "shelf": 1.1, "quill": 1.1}, 4.0)) == ["tidy shelf quillby"]      # all three
    assert conversion.keeps(line, ({"tidy": 0.9, "shelf": 0.9, "quill": 0.9}, 4.0)) == []        # none loads it: build keeps each
    four = line.replace("by the door", "by the Brindle door")                                     # four words load it: each alone
    assert conversion.keeps(four, ({"tidy": 0.8, "shelf": 0.8, "quill": 0.8, "brind": 0.8}, 4.0)) == ["tidy", "shelf", "quillby", "brindle"]


def test_a_listed_sentence_holding_a_cited_quote_or_parts_that_overlap_is_accounted_for_and_a_part_with_no_reason_is_listed(tmp_path, monkeypatch):
    from yerk_ai import conversion
    monkeypatch.setattr(conversion, "CHUNK", 1)
    m = old_store(tmp_path)
    day = next(m.daily.glob("*.md"))
    said = "the long answers bury the plan every single time we talk"
    day.write_text(day.read_text() + "\n".join(
        memory.format_line(datetime_at(NOW - 3000 + i), "example-room", "r1", "owner", "", x, "private", f"o{8 + i}")
        for i, x in enumerate([said, "the example ledger holds my accounts", "and the bank reads the example ledger"])) + "\n")
    n = len(day.read_text().splitlines()) - 2
    one = f'Keep replies short, since he said "{said}".'
    two = 'Lead with the date ("date first, always, so I can plan"), since he plans from it; the last long one bounced.'
    hall = (f"Post the rota on Monday, since the team plans from it; put the week in bold. <examples><example>Bounced, daily/"
            f"{day.name}:3, a gloss of far more than six words on it</example></examples>")
    (m.root / memory.RULES).write_text((m.root / memory.RULES).read_text().replace(HALL, hall).replace(WEB, f"{one} {two}"))
    (m.root / "rules-proposals").mkdir()
    (m.root / "rules-proposals" / "reasons.tsv").write_text(
        f"tree/rules.md:9\tdaily/{day.name}:{n + 1}\tholds my accounts\ntree/rules.md:9\tdaily/{day.name}:{n + 2}\tthe bank reads\n")
    backs: list[str] = []

    def model(prompt: str, tools: str) -> str:
        if (prompt := prompt.lstrip()).startswith("You check the lessons"):
            return "KEEP tree/rules.md:7\nKEEP tree/rules.md:8\nDROP tree/rules.md:9 | so it goes back too"
        if prompt.startswith("The gate refused"):
            backs.append(prompt)
            return ""
        if "tree/rules.md:9 (job" in prompt:
            assert "holds my accounts" in prompt and f"Reason on file, from daily/{day.name}:{n + 2}: the bank reads" in prompt
        return "\n".join(json.dumps(x) for x in (
            {"cite": "tree/rules.md:7", "lessons": [
                {"summary": "Post the rota on Monday.", "body": "Do so since the team plans from it.", "reason": {"said": "the team plans from it"},
                 "examples": [1]},
                {"summary": "Put the week in bold.", "body": ""}],
             "rewritten": [{"said": "Post the rota on Monday, since the team plans from it; put the week in bold.", "as": "the two lessons"}]},
            {"cite": "tree/rules.md:8", "lessons": [
                {"summary": "Keep replies short.", "body": f'Do so since in his words "{said}".', "reason": {
                    "said": "the long answers bury the plan", "from": f"daily/{day.name}:{n}"}},
                {"summary": "Put the day first.", "body": "Do it since he plans from it.", "reason": {"said": "he plans from it"}}],
             "rewritten": [{"said": one, "as": "Keep replies short."}, {"said": two.split(";")[0], "as": "Put the day first."}],
             "left_out": [{"said": '"date first, always, so I can plan"', "why": "his words"}, {"said": "the last long one", "why": "how one went"}]},
            {"cite": "tree/rules.md:9", "lessons": [{"summary": "Keep the example ledger in the owner's room.", "body": "Never post it, since "
             "the bank reads it.", "reason": {"said": "the bank reads", "from": f"daily/{day.name}:{n + 2}"}}]}))
    rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=model, read=FABLE2))
    rota, web, ledger = rec["rules"][:3]
    assert web["missing"] == [] and rota["missing"] == []          # a cited quote inside a listed sentence, a mark, the wrapper
    assert web["lessons"][0]["body"] == f'Do so since in his words "{said}".'    # the owner's quote whole, never a pointer (S3)
    assert {"kind": "quote cited", "said": said, "as": said, "at": f"daily/{day.name}:{n}"} in web["done"]
    assert rota["lessons"][0]["body"].endswith(f"<example>Bounced, daily/{day.name}:3</example>") and rota["lessons"][0]["gate"] == []
    assert any(d["kind"] == "example cited" and "its gloss of 10 words left out" in d.get("why", "") for d in rota["done"])
    assert {"kind": "no reason on file", "said": "Put the week in bold.", "why": "listed for the owner's side: the rule gives this lesson's part no "
            "reason and a reason is never invented; a row in rules-proposals/reasons.tsv supplies one"} in rota["done"]
    assert {"kind": "reason supplied", "said": "the bank reads", "from": f"daily/{day.name}:{n + 2}, named in rules-proposals/reasons.tsv"} in ledger["done"]
    assert rec["why"][0] == "1 lesson refused only for a reason no file holds, each listed (a row in rules-proposals/reasons.tsv supplies one)"
    assert len(backs) == 4 and all(x.count(" (job: ") == 1 for x in backs)   # the round's failing rules, one a call at CHUNK 1


# Run 4 (2026-11-21): nothing the owner said is changed and nothing is added that they did not say; each row's tests, placeholder words.
FILED = "example-room, 2025-11-05 09:00:00-05:00"        # the rule's trailer: filed Nov 5 at 09:00 -05:00


def picker_logs(tmp_path):
    """A day log where a later line repeats an earlier one, another speaker's line holds an owner's words first, an example's
    words sit on its own day twice and a line written after the filing holds words no earlier line does."""
    from datetime import datetime, timedelta, timezone
    daily, tz = tmp_path / "daily", timezone(timedelta(hours=-5))
    daily.mkdir()
    rows = {"2025-11-03": [(8, 0, "yerk", "the hall key sits under the blue mat"), (8, 30, "owner", "the hall key sits under the blue mat"),
                           (9, 0, "owner", "the example rota goes up on monday for the team")],
            "2025-11-04": [(9, 0, "owner", "the example rota goes up on monday for the team"), (9, 10, "yerk", "the rota went out late again"),
                           (14, 4, "yerk", "the rota went out late again"), (16, 0, "yerk", "the rota went out late again")],
            "2025-11-05": [(8, 0, "owner", "keep the spare lamp in the store"), (10, 0, "owner", "the late shelf note only after filing")],
            "2025-11-06": [(9, 0, "owner", "a note the day after the filing")]}
    for day, said in rows.items():
        (daily / f"{day}.md").write_text(f"# {day}\n\n" + "\n".join(memory.format_line(datetime.fromisoformat(f"{day}T{h:02d}:{mi:02d}:00").replace(
            tzinfo=tz), "example-room", "r1", role, "", x, "private", f"p{day[-1]}{i}") for i, (h, mi, role, x) in enumerate(said)) + "\n")
    return daily


def test_r3_a_cite_names_the_line_it_means_or_there_is_none(tmp_path):
    from yerk_ai import conversion, shape
    daily = picker_logs(tmp_path)
    assert conversion.pick("the example rota goes up on monday", daily, FILED) == "daily/2025-11-03.md:5"     # the earliest, not the repeat
    assert conversion.pick("the hall key sits under the blue mat", daily, FILED, his=True) == "daily/2025-11-03.md:4"   # the owner's own line
    assert conversion.pick("the hall key sits under the blue mat", daily, FILED) == "daily/2025-11-03.md:3"       # else the earliest
    assert conversion.pick("the rota went out late again", daily, FILED, when="Bounced, Nov 4 14:05: the rota") == "daily/2025-11-04.md:5"
    assert conversion.pick("the rota went out late again", daily, FILED, when="Bounced, Nov 2: the rota") == ""   # none on the day it names
    assert conversion.pick("the late shelf note only after filing", daily, FILED) == ""     # written after the rule was filed: none
    assert conversion.pick("a note the day after the filing", daily, FILED) == "" and conversion.pick("a note the day after", daily) != ""
    assert shape.on_record("the example rota goes up on monday", daily) == "daily/2025-11-04.md:3"    # the gate's search, unchanged
    rule = {"said": 'Post the rota early. <example>Bounced, Nov 4 14:05: "the rota went out late again"</example> <example>Landed: '
            '"the late shelf note only after filing"</example> <example>Landed shape: "the example rota goes up on monday"</example>',
            "filed": FILED}
    cited, after, shape_ = conversion.examples(rule, daily)
    assert cited["at"] == "daily/2025-11-04.md:5" and cited["as"] == "<example>Bounced, daily/2025-11-04.md:5</example>"
    assert after["kind"] == "example kept" and after["as"] == '<example>Landed: "the late shelf note only after filing"</example>'
    assert shape_["kind"] == "example kept" and shape_["why"] == "a shape, not a message"
    quote = {"said": 'On Nov 3, 08:3x, he said "the hall key sits under the blue mat", so check it.', "filed": FILED, "job": "hall key"}
    done, faults = conversion.quoted(quote, [{"summary": 'Check the mat, since "the hall key sits under the blue mat".', "body": ""}], daily, 4)
    assert faults == [] and done == [{"kind": "quote cited", "said": "the hall key sits under the blue mat", "as": "the hall key sits under the blue mat",
                                      "at": "daily/2025-11-03.md:4"}]     # the owner's, on the day and near the time the rule names before it


def one_rule_model(prop: dict, asked: list[str] | None = None, verdict: str = "KEEP"):
    """The model pass's stand-in for one rule: `prop` as its proposal each time, `verdict` its knock-down; `asked` gathers prompts."""
    def model(prompt: str, tools: str) -> str:
        (asked if asked is not None else []).append(prompt := prompt.lstrip())
        return f"{verdict} {prop['cite']}" if prompt.startswith("You check the lessons") else json.dumps(prop)
    return model


def test_r1_no_exception_is_added_and_a_never_whose_rule_gives_none_is_listed_not_counted_and_passed_by_the_apply(tmp_path):
    from yerk_ai import conversion
    m, prop = ledger_store(tmp_path)
    asked: list[str] = []
    rec = conversion.loaded(path := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(prop, asked), read=FABLE2))
    ledger = rec["rules"][2]
    assert "go-ahead" not in json.dumps([[x["summary"], x["body"]] for x in ledger["lessons"]]) and not any(d["kind"] == "exception added" for d in ledger["done"])
    assert [g.split(":")[0] for g in ledger["lessons"][0]["gate"]] == ["an absolute rule missing its scope or exception"]
    assert rec["ready"] and rec["no_exception"] == {"example-ledger": "tree/rules.md:9"} and rec["no_reason"] == {}
    assert ("Listed as no exception, as the rule gives none, carried as he wrote it and not counted in the verdict: 1 lesson, "
            "rules/example-ledger.md.") in path.read_text().splitlines()
    assert any(d["kind"] == conversion.UNLIFTED for d in ledger["done"]) and not [p for p in asked if p.startswith("The gate refused")]
    conversion.apply(EXAMPLE_PRIVATE, tmp_path / "state", m, path)              # passed by the apply for that finding alone
    assert (m.root / lessons.NO_REASON).read_text() == f"example-ledger\t{rec['day']}\ttree/rules.md:9\tan exception\n"
    assert lessons.no_exception(m.root) == {"example-ledger": (rec["day"], "tree/rules.md:9")} and lessons.no_reason(m.root) == {}


def test_r1_a_lesson_missing_an_exception_its_rule_gives_or_an_absolute_the_rule_lacks_still_counts(tmp_path):
    from yerk_ai import conversion
    m, prop = ledger_store(tmp_path)
    rows = (m.root / memory.RULES).read_text()
    for rule, summary in (("Never post the example ledger unless the owner asks.", "Keep the example ledger in the owner's room."),
                          ("Never post the example ledger.", "Always keep the example ledger in the owner's room.")):   # "always" is not the owner's
        (m.root / memory.RULES).write_text(rows.replace("Never post the example ledger.", rule))
        p = {**prop, "lessons": [{**prop["lessons"][0], "summary": summary}],
             "rewritten": [{"said": rule, "as": f"{summary} Never post it."}]}
        rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p), read=FABLE2))
        assert not rec["ready"] and rec["why"][0] == "1 lesson the gate refuses" and rec["no_exception"] == {}, rule


def test_r1_the_daily_run_files_nothing_for_a_lesson_listed_for_its_exception_and_its_report_names_it(tmp_path, monkeypatch):
    from yerk_ai import doctor, needs, shape
    monkeypatch.setattr(doctor, "rooms", lambda private: {"example-room": {"model": "claude-fable-5-2"}, "example-machine": {"role": "machine"}})
    private, state = tmp_path / "private", tmp_path / "state"                      # a room on Fable 5.2: the absolute check binds
    __import__("shutil").copytree(EXAMPLE_PRIVATE, private)
    m = memory.settings(private, state)
    from test_lesson_shape import docs_with, every_pin
    docs_with(state, **every_pin())
    (m.root / lessons.DIR).mkdir(parents=True)
    lessons.put(m.root, lessons.Lesson("example-ledger", "example ledger", "Keep the example ledger in the owner's room.",
                                       "Never post it, since it holds the accounts."))
    (m.root / lessons.NO_REASON).write_text("example-ledger\t2026-11-21\ttree/rules.md:9\tan exception\n")
    posts: list[dict] = []
    assert doctor.lessons(private, state, m, 1000.0, post=posts.append) == [] and posts == [] and needs.load(state) == []
    at = next(a for a, *_ in shape.rows_of(m) if "/example-ledger.md" in a)
    assert (f"Listed as no exception, as its rule gives none (rules-no-reason.tsv), its missing exception no finding: 1 lesson, {at}."
            in (state / "docs" / "shape" / "lessons.md").read_text().splitlines())
    (m.root / lessons.NO_REASON).write_text("example-ledger\t2026-11-21\ttree/rules.md:9\ta reason\n")    # listed for another cause
    out = doctor.lessons(private, state, m, 1000.0, dry=True)
    assert len(out) == 1 and "absolute" in out[0] and at in out[0]


def test_r2_the_rules_caution_stays_in_a_lesson_word_for_word_or_goes_back(tmp_path):
    from yerk_ai import conversion
    m, prop = ledger_store(tmp_path)
    caution = "The example till may close early."
    rows = (m.root / memory.RULES).read_text()
    (m.root / memory.RULES).write_text(rows.replace("Never post the example ledger.", f"Never post the example ledger. {caution}"))
    whole = [{"said": f"Never post the example ledger. {caution}", "as": "Keep the example ledger in the owner's room. Never post it."}]
    for body, said, fault in ((prop["lessons"][0]["body"], caution, f'the rule\'s caution is in no lesson: "{caution}"'),
                              (prop["lessons"][0]["body"], "The till may close.", 'the caution is not the rule\'s own sentence, word for word: '
                               '"The till may close."'), (f"{prop['lessons'][0]['body']} {caution}", caution, "")):
        p = {**prop, "lessons": [{**prop["lessons"][0], "body": body}], "rewritten": whole, "caution": said}
        asked: list[str] = []
        rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p, asked), read=FABLE2))
        ledger = rec["rules"][2]
        item = next(d for d in ledger["done"] if d["kind"] == "caution to review")
        if fault:            # a rewritten item holding the caution's sentence never accounts for it
            assert ledger["faults"] == [fault] and not rec["ready"] and "1 rule with a fault the code finds, each listed" in rec["why"]
            assert any(fault in x for x in asked if x.startswith("The gate refused")) and f"- fault: {fault}" in (
                m.root / "rules-proposals" / f"lessons-{rec['day']}.md").read_text()
        else:
            assert ledger["faults"] == [] and rec["ready"] and item["why"].startswith("left in place in rules/example-ledger.md")


def test_r4_his_quote_is_kept_as_a_run_of_his_own_words_never_a_pointer(tmp_path):
    from yerk_ai import conversion
    m, prop = ledger_store(tmp_path)
    day = next(m.daily.glob("*.md"))
    said = "the example ledger holds my accounts and the bank reads it every month"
    day.write_text(day.read_text() + memory.format_line(datetime_at(NOW - 2000), "example-room", "r1", "owner", "", said, "private", "q1") + "\n")
    n = len(day.read_text().splitlines())
    rows = (m.root / memory.RULES).read_text()
    (m.root / memory.RULES).write_text(rows.replace("Never post the example ledger.", f'Never post the example ledger, since he said "{said}".'))
    rule = f'Never post the example ledger, since he said "{said}".'

    got: dict = {}

    def run(body: str) -> tuple[dict, list[str]]:
        p = {**prop, "lessons": [{**prop["lessons"][0], "body": body}], "rewritten": [{"said": rule, "as": body}]}
        asked: list[str] = []
        got["rec"] = rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p, asked), read=FABLE2))
        return rec["rules"][2], [x for x in asked if x.startswith("The gate refused")]
    ledger, backs = run('Never post it, since he said "the bank reads it every month".')     # a part of the owner's quote: cut, sent back (S3)
    assert ledger["faults"] == [f'lesson 1 cuts his quote: "the bank reads it every month" is part of "{said}", which goes in whole, word '
                                'for word'] and backs and not got["rec"]["ready"]
    ledger, backs = run(f'Never post it, since he said "{said}".')      # whole, past 8 words and on record: carried as they wrote it (S3)
    assert ledger["lessons"][0]["body"] == f'Never post it, since he said "{said}".' and not backs and got["rec"]["ready"]
    assert {"kind": "quote cited", "said": said, "as": said, "at": f"daily/{day.name}:{n}"} in ledger["done"]
    assert got["rec"]["whole"] == {"example-ledger": "tree/rules.md:9"} and any(d["kind"] == conversion.WHOLE for d in ledger["done"])
    ledger, backs = run('Never post it, since he said "holds my accounts".')         # the owner's reason on file, whole, is their words too
    assert ledger["faults"] == [] and not backs
    ledger, backs = run('Never post it, since he said "the bank reads the ledger".')       # no quote of the owner's
    assert ledger["faults"] == ['lesson 1 quotes words that are no whole quote of the rule: "the bank reads the ledger"'] and backs


def test_r5_the_model_sees_the_examples_numbered_and_each_lands_where_it_names_never_counted_against_the_body(tmp_path):
    from yerk_ai import conversion
    m, prop = ledger_store(tmp_path)
    ex = ['Bounced: "the example ledger went to the hall board"', 'Landed: "the example ledger stayed in the owner room"']
    rows = (m.root / memory.RULES).read_text()
    (m.root / memory.RULES).write_text(rows.replace("Never post the example ledger.", "Never post the example ledger. Keep its copy at home. "
                                                    + " ".join(f"<example>{x}</example>" for x in ex)))
    two = [{"summary": "Keep the example ledger in the owner's room.", "body": "Never post it, since it holds my accounts. " + "Keep it shut. " * 38,
            "reason": prop["lessons"][0]["reason"]}, {"summary": "Keep the example ledger's copy at home.", "body": "Do so since it holds my accounts."}]
    whole = [{"said": "Never post the example ledger.", "as": "the first lesson"}, {"said": "Keep its copy at home.", "as": "the second"}]

    def run(lessons_: list[dict], **more) -> tuple[dict, dict, list[str]]:
        asked: list[str] = []
        p = {**prop, "lessons": lessons_, "rewritten": whole, **more}
        rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p, asked), read=FABLE2))
        return rec, rec["rules"][2], asked
    rec, ledger, asked = run([{**two[0], "examples": [1, {"n": 2, "gloss": "a gloss"}]}, two[1]])
    assert f"Example 1: {ex[0]}\nExample 2: {ex[1]}" in asked[0] and f"Example 2: {ex[1]}" in next(x for x in asked if x.startswith("You check"))
    body = ledger["lessons"][0]["body"]
    assert len(body) > 600 and body.endswith(f"<example>{ex[0]}</example> <example>{ex[1]}</example>") and ledger["faults"] == []
    assert ledger["lessons"][0]["gate"] == [] or all(conversion.open_finding(g) for g in ledger["lessons"][0]["gate"])
    _, ledger, _ = run([{**two[0], "body": two[0]["body"] + ' <example>Bounced, "the example ledger went to the hall board."</example>'},
                        {**two[1], "examples": [2]}])                        # the model wrote one of the rule's own: taken as named
    assert ledger["faults"] == [] and ledger["lessons"][0]["body"].endswith(f"<example>{ex[0]}</example>")
    _, ledger, _ = run([{**two[0], "examples": [1, 3]}, {**two[1], "examples": [1]}])
    assert ledger["faults"] == ["lesson 1 names example 3, which the rule does not have: it has 2",
                                "example 1 is named in 2 lessons (1, 2): name it in the one whose step it shows",
                                f'example 2 of the rule is in no lesson: "{ex[1]}"']
    _, ledger, _ = run([{**two[0], "examples": [1], "body": two[0]["body"] + " <example>Landed: an example of the model's own</example>"}, two[1]],
                       left_out=[{"example": 2, "why": "it shows nothing the lesson does not say"}])
    assert ledger["faults"] == ["lesson 1 holds an example the rule does not give: \"Landed: an example of the model's own\""]
    out = next(d for d in ledger["done"] if d["kind"] == "example left out")
    assert out["said"] == ex[1] and out["why"] == "it shows nothing the lesson does not say"       # the model's why, never for length


def test_r6_r7_the_prompts_say_each_lesson_is_read_alone_a_whys_own_sentence_and_his_words_kept():
    from yerk_ai import curate, shape
    lessons_, again, down = (curate.prompt(x, {}, "") for x in ("Lessons", "Lessons again", "Knock down lessons"))
    assert "Where the rule gives no exception, write none" in lessons_ and "owner's go-ahead as its exception" not in lessons_ + again
    assert "The rule's examples are not shown to you" not in lessons_ and 'Write the words "landed" and "bounced" nowhere' not in lessons_
    assert "a room may be given one lesson of a rule and not its siblings" in lessons_.replace("A room", "a room")
    assert "is a sentence of its own in each lesson it backs, opening `Why:`" in lessons_ and "never joined to one step" in lessons_
    assert '"asks" stays "asks", three verbs stay three' in lessons_ and "never make an instruction into an example" in lessons_
    assert "no exception where the rule gives none" in again and "each lesson read alone" in again
    assert "each lesson, read alone, says what its part of the rule says, and together they say all of it" in down
    assert "a lesson that leans on a sibling for a word" in down and "a word of his reworded where his own would fit" in down
    assert "no reason" not in shape.lesson("example hall", "Book the example hall early. Why: the hall fills by the Friday before.")


def test_r8_a_findings_row_redoes_its_rule_shows_the_model_its_findings_and_a_rule_that_comes_back_with_them_is_listed_not_looped(tmp_path):
    import shutil

    from yerk_ai import conversion
    m, asked = shop_store(tmp_path), set[str]()
    first = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, set()), read=FABLE2)
    shutil.copy(first, earlier := tmp_path / "earlier.md")
    (m.root / "rules-proposals" / "findings.tsv").write_text("# cite, finding\ntree/rules.md:10\tlesson 1 leans on its sibling for a word\n")
    prompts: list[str] = []

    def model(prompt: str, tools: str) -> str:
        prompts.append(prompt.lstrip())
        return shop_model(asked, {"tree/rules.md:10"})(prompt, tools).replace("| it adds a case", "| the owner's side's finding still holds: "
                                                                               "lesson 1 leans on its sibling")
    asked.clear()
    rec = conversion.loaded(second := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=model, read=FABLE2, earlier=earlier))
    assert asked == {"tree/rules.md:8", "tree/rules.md:10"} and rec["from"]["why"]["a findings row"] == 1     # :10 never kept with a row
    assert ("The owner's side read an earlier conversion of this rule and found:\n- lesson 1 leans on its sibling for a word" in prompts[0]
            and next(p for p in prompts if p.startswith("You check")).count("leans on its sibling") == 1)
    key = next(r for r in rec["rules"] if r["cite"] == "tree/rules.md:10")
    assert key["found"] == ["lesson 1 leans on its sibling for a word"] and any(d["kind"] == "reviewer dropped" for d in key["done"])
    asked.clear()                                           # redone with its findings shown and dropped again: listed, not looped on
    rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=model, read=FABLE2, earlier=second))
    key = next(r for r in rec["rules"] if r["cite"] == "tree/rules.md:10")
    assert "tree/rules.md:10" not in asked and any(d["kind"] == conversion.LOOPED for d in key["done"]) and not rec["ready"]


def test_r8_from_redoes_a_rule_whose_lessons_hold_what_the_code_no_longer_writes(tmp_path):
    import shutil

    from yerk_ai import conversion
    m, asked, key = shop_store(tmp_path), set[str](), "tree/rules.md:10"
    shutil.copy(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, set()), read=FABLE2), earlier := tmp_path / "e.md")

    def again(change) -> tuple[set[str], dict]:
        change(rec := conversion.loaded(earlier), next(r for r in rec["rules"] if r["cite"] == key))
        (path := tmp_path / "rewritten.md").write_text(conversion.render(rec))
        asked.clear()
        out = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, set()), read=FABLE2, earlier=path))
        return set(asked), out["from"]["why"]

    def added(rec, r):
        r["done"].append({"kind": "exception added", "said": "Only the owner's go-ahead lifts it.", "from": "an earlier run"})

    def pointer(rec, r):
        r["lessons"][0]["body"] += " Since the words at daily/2025-11-09.md:3."

    def old_cite(rec, r):
        r["done"].append({"kind": "example cited", "said": "a placeholder example", "at": "daily/2025-11-09.md:3", "as": "<example>Landed, daily/2025-11-09.md:3</example>"})

    def for_length(rec, r):
        r["done"].append({"kind": "example left out", "said": "a placeholder example", "why": "the lesson's body would pass its length with it"})
    for change, why in ((added, "an exception added"), (pointer, "a quote swapped for a pointer"), (old_cite, "an example cited by the old search"),
                        (for_length, "an example left out for length")):
        got, whys = again(change)
        assert key in got and whys.get(why) == 1, why


# Run 5 (2026-11-21): the owner's words win over the gate's shape; each row's tests, placeholder words and a placeholder name only.
NAME = "Quilla"                  # a placeholder for a name the private layer gives the owner ([curate] owner_names)


def ledger_with(m, rule: str) -> None:
    """The ledger rule, tree/rules.md:9, given `rule` as its words (its trailer kept)."""
    rows = (m.root / memory.RULES).read_text()
    (m.root / memory.RULES).write_text(rows.replace("Never post the example ledger.", rule, 1))


def test_s1_a_quote_is_his_by_the_general_words_and_the_names_the_install_gives(tmp_path):
    from yerk_ai import config, conversion
    daily = picker_logs(tmp_path)
    assert conversion.given_his("Lock up, since he wrote ") and not conversion.given_his(f"Lock up, since {NAME} wrote ")
    assert conversion.given_his(f"Lock up, since {NAME} wrote ", (NAME,)) and conversion.given_his("as the owner's note says ")
    assert conversion.owner_names({"curate": {"owner_names": [NAME, " "]}}) == (NAME,) == conversion.owner_names({"curate": {"owner_names": NAME}})
    assert conversion.owner_names({}) == () and "owner_names" in config.YERKD_WORDS["curate"]
    said = "the hall key sits under the blue mat"
    for lead, his, at in (("he wrote", (), "daily/2025-11-03.md:4"), (f"{NAME} wrote", (NAME,), "daily/2025-11-03.md:4"),
                          (f"{NAME} wrote", (), "daily/2025-11-03.md:3")):      # the owner's line by the words that give it as theirs, no time named
        rule = {"said": f'Lock up, since {lead} "{said}".', "filed": FILED, "job": "hall key", "his": his}
        done, faults = conversion.quoted(rule, [{"summary": f'Lock up, since "{said}".', "body": ""}], daily, 4)
        assert faults == [] and done[0]["at"] == at, lead


def test_s2_a_never_is_read_with_its_own_sentence_and_listed_when_that_sentence_gives_no_exception(tmp_path):
    from yerk_ai import conversion
    two = "Never post the example ledger. Lock the example till unless the owner asks."
    lesson_ = "Keep the example ledger in the owner's room. Never post it, since it holds my accounts."
    rule = {"job": "example ledger", "said": two}
    assert conversion.unlifted(rule, lesson_, FABLE2)                         # another sentence's exception changes nothing
    assert not conversion.unlifted({**rule, "said": "Never post the example ledger unless the owner asks."}, lesson_, FABLE2)   # its own
    assert not conversion.unlifted(rule, "Always keep the example ledger in the owner's room.", FABLE2)       # an absolute not the owner's
    assert not conversion.unlifted({**rule, "said": 'Lock the example till. He said "never post the example ledger".'}, lesson_, FABLE2)
    both = {**rule, "said": "Never post the example ledger. Never lock the example till unless the owner asks."}   # one word, two sentences
    assert conversion.unlifted(both, lesson_, FABLE2) and not conversion.unlifted(both, "Never lock the example till, since it holds the cash.", FABLE2)
    m, prop = ledger_store(tmp_path)
    ledger_with(m, two)
    p = {**prop, "left_out": [{"said": "Lock the example till unless the owner asks.", "why": "a placeholder's other step"}]}
    rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p), read=FABLE2))
    assert rec["ready"] and rec["no_exception"] == {"example-ledger": "tree/rules.md:9"}


def test_s2b_a_lesson_giving_his_never_an_exception_or_a_seal_goes_back_and_from_never_keeps_it(tmp_path):
    from yerk_ai import conversion
    rule = {"job": "example ledger", "said": "Never post the example ledger. Lock the example till unless the owner asks."}

    def faults(text: str) -> list[str]:
        return conversion.checks(rule, [{"summary": text, "body": ""}], [], [])
    fault = 'lesson 1 gives its never an exception the rule does not: carry it as he wrote it (its words: "{}")'
    assert faults("Never post the example ledger. No case lifts it.") == [fault.format("No case lifts it")]
    assert faults("Never post the example ledger unless the owner asks.") == [fault.format("unless")]     # the owner's "unless" is another sentence's
    assert faults("Never post the example ledger, even when asked.") == [fault.format("even when asked")]
    assert faults("Never post the example ledger. Lock the example till unless the owner asks.") == []     # the owner's own sentences, kept
    m, prop = ledger_store(tmp_path)
    p = {**prop, "lessons": [{**prop["lessons"][0], "body": prop["lessons"][0]["body"] + " No case lifts it."}]}
    asked: list[str] = []
    rec = conversion.loaded(path := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p, asked), read=FABLE2))
    assert rec["rules"][2]["faults"] == [fault.format("No case lifts it")] and not rec["ready"]
    assert any(fault.format("No case lifts it") in x for x in asked if x.startswith("The gate refused"))
    asked.clear()
    rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(prop, asked), read=FABLE2,
                                               earlier=path))
    assert any("=== tree/rules.md:9 (job" in x for x in asked) and rec["from"]["why"].get("a fault the code finds") == 1 and rec["ready"]


def test_s3_his_quote_goes_in_whole_never_cut_split_or_joined_and_one_past_the_gate_is_carried_and_listed(tmp_path):
    from yerk_ai import conversion, curate
    daily = picker_logs(tmp_path)
    said = "the hall key sits under the blue mat"
    rule = {"said": f'Check the mat, since he said "{said}".', "filed": FILED, "job": "hall key"}

    def run(text: str, also: list[str] | None = None) -> tuple[list[dict], list[str]]:
        return conversion.quoted(rule, [{"summary": text, "body": ""}], daily, 4, also)
    cut = 'lesson 1 cuts his quote: "{}" is part of "' + said + '", which goes in whole, word for word'
    assert run('Check the mat, since "the hall key" "sits under the blue mat".')[1] == [cut.format("the hall key"), cut.format(
        "sits under the blue mat")]                                                         # chopped into adjacent runs
    assert run('Check the mat, since "the hall key ... the blue mat".')[1] == [cut.format("the hall key ... the blue mat")]   # an ellipsis
    assert run('Check the mat, since "the hall key sits under the mat".')[1] == [
        'lesson 1 quotes words that are no whole quote of the rule: "the hall key sits under the mat"']
    assert run(f'Check the mat, since "{said}".') == ([{"kind": "quote cited", "said": said, "as": said, "at": "daily/2025-11-03.md:4"}], [])
    assert run('Check the mat, since "the spare lamp sits in the store".', ["the spare lamp sits in the store"])[1] == []   # a reason on
    rota = "the example rota goes up on monday for the team"                    # file, whole, that the rule never quotes, is the owner's
    his = {"said": f'Post the rota, since he said "{rota}".', "job": "example rota"}
    assert conversion.recorded(his, f'Post the rota, since "{rota}".', daily) == {conversion.WHOLE}           # the owner's, whole: carried
    assert conversion.recorded({**his, "said": "Post the rota early."}, f'Post the rota, since "{rota}".', daily) == set()   # the model's
    lessons_, again = (curate.prompt(x, {}, "") for x in ("Lessons", "Lessons again"))
    assert "run of 8 or fewer" not in lessons_ + again and "two words or fewer" not in lessons_
    assert "whole, word for word, in quotes, in the sentence that carries it" in lessons_ and "never cut, split into pieces" in again


def test_s3_from_redoes_a_rule_whose_lessons_cut_his_quote(tmp_path):
    from yerk_ai import conversion
    m, prop = ledger_store(tmp_path)
    day = next(m.daily.glob("*.md"))
    said = "the example ledger holds my accounts and the bank reads it every month"
    day.write_text(day.read_text() + memory.format_line(datetime_at(NOW - 2000), "example-room", "r1", "owner", "", said, "private", "q1") + "\n")
    rule = f'Never post the example ledger, since he said "{said}".'
    ledger_with(m, rule)

    def run(body: str, earlier=None) -> tuple[dict, list[str], object]:
        asked: list[str] = []
        p = {**prop, "lessons": [{**prop["lessons"][0], "body": body}], "rewritten": [{"said": rule, "as": body}]}
        path = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p, asked), read=FABLE2, earlier=earlier)
        return conversion.loaded(path), asked, path
    rec, _, first = run('Never post it, since he said "the bank reads it every month".')
    assert not rec["ready"] and rec["rules"][2]["faults"][0].startswith("lesson 1 cuts his quote")
    rec, asked, _ = run(f'Never post it, since he said "{said}".', earlier=first)
    assert rec["from"]["why"]["a quote of his cut"] == 1 and any("=== tree/rules.md:9 (job" in x for x in asked) and rec["ready"]
    assert rec["whole"] == {"example-ledger": "tree/rules.md:9"} and ("Listed as his quote kept whole, carried as he wrote it where the gate asks "
                                                                       "a cite in its place and not counted in the verdict: 1 lesson, rules/example-ledger.md.") in conversion.render(rec)


def test_s4_his_own_sentence_holding_bounced_beside_a_quote_is_carried_and_listed_and_a_sentence_of_the_models_still_counts(tmp_path):
    from yerk_ai import conversion
    m, prop = ledger_store(tmp_path)
    his = 'It bounced when he wrote "post the example ledger on the board".'
    ledger_with(m, f"Never post the example ledger. {his}")
    whole = [{"said": "Never post the example ledger.", "as": "Keep the example ledger in the owner's room. Never post it."}]

    def run(body: str, summary: str = prop["lessons"][0]["summary"]) -> tuple[dict, list[str], str]:
        asked: list[str] = []
        p = {**prop, "lessons": [{**prop["lessons"][0], "summary": summary, "body": body}], "rewritten": whole}
        path = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p, asked), read=FABLE2)
        return conversion.loaded(path), [x for x in asked if x.startswith("The gate refused")], path.read_text()
    rec, backs, text = run(f"Never post it, since it holds my accounts. {his}")
    ledger = rec["rules"][2]
    assert any(g.startswith(conversion.UNTAGGED) for g in ledger["lessons"][0]["gate"]) and rec["ready"] and not backs
    assert rec["own"] == {"example-ledger": "tree/rules.md:9"} and any(d["kind"] == conversion.OWN for d in ledger["done"])
    assert ("Listed as his own sentence kept, carried word for word where the gate reads an exchange outside example tags and not counted in "
            "the verdict: 1 lesson, rules/example-ledger.md.") in text.splitlines()
    rec, backs, _ = run('Never post it, since it holds my accounts. He wrote "post the example ledger on the board" and it bounced.')
    assert not rec["ready"] and rec["own"] == {} and backs and "an example outside <example> tags" in backs[0]     # a sentence of its own
    rec, backs, _ = run(f"Never post it, since it holds my accounts. {his}", "Keep the example ledger in the owner's room, " + "next to the "
                        "till and the rota " * 5 + "there.")                       # the listed lesson's other finding still counts
    assert not rec["ready"] and rec["own"] == {} and backs and "no one-line summary first" in backs[0]
    assert "outside <example> tags" not in backs[0] and "an absolute rule" not in backs[0]       # the excused findings never sent


def test_s5_a_cite_the_model_refuses_is_placed_as_its_words_and_listed_and_an_example_kept_so_is_listed_not_counted(tmp_path):
    from yerk_ai import conversion, curate
    m, prop = ledger_store(tmp_path)
    day = next(m.daily.glob("*.md"))
    late = "the example ledger went up on the hall board by the door"         # written the day after the rule was filed: no line passes
    (m.daily / "2025-11-11.md").write_text("# 2025-11-11\n\n" + memory.format_line(datetime_at(NOW + 86400), "example-room", "r1", "owner", "",
                                                                                   late, "private", "q2") + "\n")
    one, two = 'Bounced: "the example ledger, post it?"', f'Bounced: "{late}"'
    ledger_with(m, f"Never post the example ledger. <example>{one}</example> <example>{two}</example>")
    why = "the line asks it, it shows no bounce"
    p = {**prop, "lessons": [{**prop["lessons"][0], "examples": [{"n": 1, "cite": False, "why": why}, 2]}]}
    asked: list[str] = []
    rec = conversion.loaded(path := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p, asked), read=FABLE2))
    ledger = rec["rules"][2]
    assert f"Example 1: {one} (code cites it as daily/{day.name}:5, which reads: the example ledger, post it?)" in asked[0]
    assert ledger["lessons"][0]["body"].endswith(f"<example>{one}</example> <example>{two}</example>") and ledger["faults"] == []
    assert {"kind": "cite refused", "said": one, "at": f"daily/{day.name}:5", "why": why} in ledger["done"]
    assert rec["ready"] and rec["uncited"] == {"example-ledger": "tree/rules.md:9"} and "Cites the model refused" in path.read_text()
    lessons_, down = curate.prompt("Lessons", {}, ""), curate.prompt("Knock down lessons", {}, "")
    assert '"cite": false' in lessons_ and "a cite kept whose line is not what the example shows" in down


def test_s5_a_day_that_is_none_cites_nothing_a_day_with_no_year_is_the_last_one_and_a_one_word_piece_is_never_searched(tmp_path):
    from yerk_ai import conversion
    daily = picker_logs(tmp_path)
    assert conversion.named("Bounced, Nov 31: the rota", None) == (conversion.NO_DAY, None)
    assert conversion.pick("the rota went out late again", daily, FILED, when="Bounced, Nov 31: the rota") == ""
    ex = {"said": 'Post the rota early. <example>Bounced, Nov 31: "the rota went out late again"</example> <example>Bounced: "late"</example>',
          "filed": FILED}
    assert [x["kind"] for x in conversion.examples(ex, daily)] == ["example kept", "example kept"]     # a one-word piece, no day named
    assert conversion.named("Bounced, Dec 30: the rota", conversion.filed_at("example-room, 2026-01-05 09:00:00-05:00")) == ("2025-12-30", None)
    assert conversion.named("Bounced, Nov 3: the rota", conversion.filed_at(FILED)) == ("2025-11-03", None)


def test_s5_a_line_stamped_inside_the_filings_120_seconds_is_its_own_and_one_past_them_is_not(tmp_path):
    from datetime import datetime, timedelta, timezone

    from yerk_ai import conversion
    daily, tz = tmp_path / "daily", timezone(timedelta(hours=-5))
    daily.mkdir()
    rows = [(9, 2, 0, "the spare lamp note inside the filing"), (9, 2, 1, "the spare lamp note past the filing")]   # FILED is 09:00:00
    (daily / "2025-11-05.md").write_text("# 2025-11-05\n\n" + "\n".join(memory.format_line(datetime(2025, 11, 5, h, mi, sec, tzinfo=tz),
                                         "example-room", "r1", "owner", "", x, "private", f"f{sec}") for h, mi, sec, x in rows) + "\n")
    assert conversion.FILING == 120 and conversion.pick("the spare lamp note inside the filing", daily, FILED) == "daily/2025-11-05.md:3"
    assert conversion.pick("the spare lamp note past the filing", daily, FILED) == ""


def test_s6_each_example_is_counted_once_by_its_place_and_two_of_one_cited_form_are_two():
    from yerk_ai import conversion
    form = "<example>Bounced, daily/2025-11-03.md:3</example>"
    shown_ = [{"kind": "example cited", "said": x, "at": "daily/2025-11-03.md:3", "as": form} for x in ("Bounced: first", "Bounced: second")]
    rule = {"job": "example hall", "said": "Book the example hall early."}

    def faults(*bodies: str) -> list[str]:
        return conversion.checks(rule, [{"summary": "Book the example hall early.", "body": b} for b in bodies], [], shown_)
    assert faults(form, form) == [] and faults(f"{form} {form}", "") == []          # one in each of two lessons, or both in one
    assert faults(form, "") == ['example 2 of the rule is in no lesson: "Bounced: second"']
    assert faults(form, f"{form} {form}") == ['lesson 2 holds an example of the rule a second time: "Bounced, daily/2025-11-03.md:3"']


def test_s7_the_apply_works_each_excuse_out_again_from_the_rules_words_and_refuses_a_list_they_do_not_bear(tmp_path):
    import pytest

    from yerk_ai import conversion
    m, prop = ledger_store(tmp_path)
    rec = conversion.loaded(path := conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(prop), read=FABLE2))
    assert rec["ready"] and rec["no_exception"] == {"example-ledger": "tree/rules.md:9"}

    def tried(change) -> str:
        change(r := conversion.loaded(path))
        (bad := tmp_path / "changed.md").write_text(conversion.render(r))
        with pytest.raises(RuntimeError) as e:
            conversion.apply(EXAMPLE_PRIVATE, tmp_path / "state", m, bad)
        return str(e.value)

    def gives(r: dict) -> None:          # the rule as the record holds it gives its never an exception: the list no longer holds
        x = next(x for x in r["rules"] if x["cite"] == "tree/rules.md:9")
        x["text"] = x["text"].replace("Never post the example ledger.", "Never post the example ledger unless the owner asks.")
    assert tried(lambda r: r["whole"].update({"example-ledger": "tree/rules.md:9"})) == (
        "example-ledger is listed as his quote kept whole, which its rule's words do not bear: run the dry run again; nothing applied")
    assert tried(gives).startswith("example-ledger is listed as no exception, as the rule gives none, which its rule's words do not bear")
    assert tried(lambda r: r.update({"no_exception": {}})) == "example-ledger no longer passes the gate: run the dry run again; nothing applied"
    assert not lessons.converted(m.root)
    conversion.apply(EXAMPLE_PRIVATE, tmp_path / "state", m, path)                # the file as the dry run wrote it
    assert lessons.no_exception(m.root) == {"example-ledger": (rec["day"], "tree/rules.md:9")}


def test_findings_never_send_a_listed_nevers_exception_finding_and_send_its_lessons_other_findings(tmp_path):
    from yerk_ai import conversion
    m = old_store(tmp_path)
    text = "Never post the example ledger in the hall."                  # its scope, no exception, no reason
    gate = memory.unshaped("example ledger", text, None, m, FABLE2)["fix"]
    sent = conversion.findings({"lessons": [{"summary": text, "body": "", "gate": gate}], "missing": [], "done": []}, False,
                               {"job": "example ledger", "said": text}, FABLE2)
    assert any(conversion.open_finding(g) for g in gate) and len(gate) > 1
    assert sent == [f'lesson 1 ("{text}"): {g}' for g in gate if not conversion.open_finding(g)]


def test_s3_s5_the_daily_run_files_nothing_for_his_quote_listed_and_keeps_an_absolute_with_no_scope_listed_for_its_exception(tmp_path, monkeypatch):
    from yerk_ai import doctor, needs, shape
    monkeypatch.setattr(doctor, "rooms", lambda private: {"example-room": {"model": "claude-fable-5-2"}, "example-machine": {"role": "machine"}})
    private, state = tmp_path / "private", tmp_path / "state"
    __import__("shutil").copytree(EXAMPLE_PRIVATE, private)
    m = memory.settings(private, state)
    from test_lesson_shape import docs_with, every_pin
    docs_with(state, **every_pin())
    said = "the example ledger holds my accounts and the bank reads it every month"
    m.daily.mkdir(parents=True, exist_ok=True)
    (m.daily / "2025-11-09.md").write_text("# 2025-11-09\n\n" + memory.format_line(datetime_at(NOW - 3000), "example-room", "r1", "owner", "",
                                                                                   said, "private", "d1") + "\n")
    (m.root / lessons.DIR).mkdir(parents=True)
    lessons.put(m.root, lessons.Lesson("example-ledger", "example ledger", "Keep the example ledger in the owner's room.", f'Do so since he said "{said}".'))
    (m.root / lessons.NO_REASON).write_text("example-ledger\t2026-11-21\ttree/rules.md:9\ta cite for his quote\n")
    posts: list[dict] = []
    assert doctor.lessons(private, state, m, 1000.0, post=posts.append) == [] and posts == [] and needs.load(state) == []
    at = next(a for a, *_ in shape.rows_of(m) if "/example-ledger.md" in a)
    assert (f"Listed as his quote kept whole, as he wrote it, the cite the gate asks in its place no finding (rules-no-reason.tsv): 1 lesson, {at}."
            in (state / "docs" / "shape" / "lessons.md").read_text().splitlines())
    (m.root / lessons.NO_REASON).write_text("example-ledger\t2026-11-21\ttree/rules.md:9\ta reason\n")      # listed for another cause
    assert any("on record" in x for x in doctor.lessons(private, state, m, 1000.0, dry=True))
    lessons.put(m.root, lessons.Lesson("example-ledger", "example ledger", "Never post the example ledger, since it holds the accounts.", ""))
    (m.root / lessons.NO_REASON).write_text("example-ledger\t2026-11-21\ttree/rules.md:9\tan exception\n")   # its absolute has no scope
    out = doctor.lessons(private, state, m, 1000.0, dry=True)
    assert len(out) == 1 and "absolute" in out[0]


def test_r8_a_rule_dropped_again_for_another_cause_than_its_findings_is_redone(tmp_path):
    import shutil

    from yerk_ai import conversion
    m, asked = shop_store(tmp_path), set[str]()
    first = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, set()), read=FABLE2)
    shutil.copy(first, earlier := tmp_path / "earlier.md")
    (m.root / "rules-proposals" / "findings.tsv").write_text("tree/rules.md:10\tlesson 1 leans on its sibling for a word\n")
    second = conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, {"tree/rules.md:10"}), read=FABLE2, earlier=earlier)
    key = next(r for r in conversion.loaded(second)["rules"] if r["cite"] == "tree/rules.md:10")
    assert any(d["kind"] == "reviewer dropped" and "it adds a case" in d["why"] for d in key["done"])     # dropped for another cause
    asked.clear()
    rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=shop_model(asked, set()), read=FABLE2, earlier=second))
    key = next(r for r in rec["rules"] if r["cite"] == "tree/rules.md:10")
    assert "tree/rules.md:10" in asked and not any(d["kind"] == conversion.LOOPED for d in key["done"])      # redone, never listed


def test_s4_the_daily_count_reads_no_exchange_in_a_lesson_listed_for_his_own_sentence(tmp_path):
    from yerk_ai import shape
    private = tmp_path / "private"
    __import__("shutil").copytree(EXAMPLE_PRIVATE, private)
    m = memory.settings(private, tmp_path / "state")
    (m.root / lessons.DIR).mkdir(parents=True)
    lessons.put(m.root, lessons.Lesson("example-ledger", "example ledger", "Keep the example ledger in the owner's room.",
                                       'Do so since it holds my accounts. It bounced when he wrote "post the example ledger on the board".'))
    assert len(shape.score(m.root / memory.RULES, rules=True)["untagged"]) == 1
    (m.root / lessons.NO_REASON).write_text("example-ledger\t2026-11-21\ttree/rules.md:9\ttags round his sentence\n")
    assert shape.score(m.root / memory.RULES, rules=True)["untagged"] == []


def test_pass_a_the_prompts_keep_his_sentence_whole_his_reason_on_file_unquoted_and_an_example_in_one_lesson(tmp_path):
    """What pass A on the copy showed: the model split the owner's sentence round its mark, quoted a long reason on file, and the
    reviewer dropped a proposal for an example the rule allows in one lesson only."""
    from yerk_ai import conversion, curate
    lessons_, down = curate.prompt("Lessons", {}, ""), curate.prompt("Knock down lessons", {}, "")
    assert "never split it, never move its quote into a sentence of its own, never reword the words round its mark" in lessons_
    assert "The reason on file goes in word for word after because, since or so that, with no quote marks round it." in lessons_
    assert "never drop a proposal because an example that shows two steps sits in one of them" in down
    m, prop = ledger_store(tmp_path)
    ledger_with(m, 'Never post the example ledger. It bounced when he wrote "post the example ledger on the board".')
    asked: list[str] = []
    p = {**prop, "lessons": [{**prop["lessons"][0], "body": 'Never post it, since it holds my accounts. He wrote "post the example ledger on the '
                              'board" and it bounced.'}], "rewritten": [{"said": "Never post the example ledger.", "as": "the lesson"}]}
    conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=one_rule_model(p, asked), read=FABLE2)
    assert conversion.OWN_HINT in next(x for x in asked if x.startswith("The gate refused"))
