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
    t = "(example-room, 2025-11-01 09:00:00-04:00)"
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
    assert ledger["lessons"][0]["body"] == 'It holds for the job "example ledger". Only the owner\'s go-ahead lifts it.'
    assert [d["kind"] for d in ledger["done"]] == ["scope added", "exception added", "no reason on file", "still failing"]
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
        assert f"Reason on file, from daily/{day.name}:{n}: holds my accounts" in prompt and bounced not in prompt
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
    assert ledger["lessons"][0]["gate"] == [] and not any(d["kind"] == "no reason on file" for d in ledger["done"])
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
        assert ledger["lessons"][0]["gate"] == [] and ledger["missing"] == [] and rec["lost"] == {}     # its lessons pass
        assert rec["ready"] is ready
        if not ready:
            assert rec["why"] == ["1 rule the reviewer dropped after the last round, each listed"]
            assert {"kind": "reviewer dropped", "why": "after the last round: it says more than the rule"} in ledger["done"]
            assert "- reviewer dropped (after the last round: it says more than the rule)" in path.read_text()


def test_the_accounting_reads_a_sentence_whole_across_an_abbreviation_in_brackets():
    from yerk_ai import conversion
    said = "Book the example hall (Ex. Reg. 12/34, rooms 4(b) and 5) a week ahead, since it fills (see the rota.) Post the rota."
    lesson = [{"summary": "Book the example hall a week ahead.", "body": "Do so since it fills. Post the rota."}]
    whole = "Book the example hall (Ex. Reg. 12/34, rooms 4(b) and 5) a week ahead, since it fills (see the rota.)"
    listed = [{"kind": "rewritten", "said": whole, "as": "Book the example hall a week ahead, since it fills."}]
    assert conversion.accounted({"said": said}, lesson, listed) == []        # one sentence, its brackets' stops inside it
    assert conversion.accounted({"said": said}, lesson, []) == [whole]       # and a bracket's own last stop still ends one


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
                {"summary": "Post the rota on Monday.", "body": "Do so since the team plans from it.", "reason": {"said": "the team plans from it"}},
                {"summary": "Put the week in bold.", "body": ""}],
             "rewritten": [{"said": "Post the rota on Monday, since the team plans from it; put the week in bold.", "as": "the two lessons"}]},
            {"cite": "tree/rules.md:8", "lessons": [
                {"summary": "Keep replies short.", "body": f'Do so since in his words "{said}".', "reason": {"said": said, "from": f"daily/{day.name}:{n}"}},
                {"summary": "Put the day first.", "body": "Do it since he plans from it.", "reason": {"said": "he plans from it"}}],
             "rewritten": [{"said": one, "as": "Keep replies short."}, {"said": two.split(";")[0], "as": "Put the day first."}],
             "left_out": [{"said": '"date first, always, so I can plan"', "why": "his words"}, {"said": "the last long one", "why": "how one went"}]},
            {"cite": "tree/rules.md:9", "lessons": [{"summary": "Keep the example ledger in the owner's room.", "body": "Never post it, since "
             "the bank reads it.", "reason": {"said": "the bank reads", "from": f"daily/{day.name}:{n + 2}"}}]}))
    rec = conversion.loaded(conversion.dry_run(EXAMPLE_PRIVATE, tmp_path / "state", m, NOW, model=model, read=FABLE2))
    rota, web, ledger = rec["rules"][:3]
    assert web["missing"] == [] and rota["missing"] == []          # a cited quote inside a listed sentence, a mark, the wrapper
    assert web["lessons"][0]["body"] == f"Do so since in his words the words at daily/{day.name}:{n}."
    assert rota["lessons"][0]["body"].endswith(f"<example>Bounced, daily/{day.name}:3</example>") and rota["lessons"][0]["gate"] == []
    assert any(d["kind"] == "example cited" and "its gloss of 10 words left out" in d.get("why", "") for d in rota["done"])
    assert {"kind": "no reason on file", "said": "Put the week in bold.", "why": "listed for the owner's side: the rule gives this lesson's part no "
            "reason and a reason is never invented; a row in rules-proposals/reasons.tsv supplies one"} in rota["done"]
    assert {"kind": "reason supplied", "said": "the bank reads", "from": f"daily/{day.name}:{n + 2}, named in rules-proposals/reasons.tsv"} in ledger["done"]
    assert rec["why"][0] == "1 lesson refused only for a reason no file holds, each listed (a row in rules-proposals/reasons.tsv supplies one)"
    assert len(backs) == 4 and all(x.count(" (job: ") == 1 for x in backs)   # the round's failing rules, one a call at CHUNK 1
