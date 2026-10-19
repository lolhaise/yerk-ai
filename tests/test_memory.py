"""Checks the memory, the day logs and the search index over them. The cases: the day's log (capture, dedupe),
the index (per-chunk updates, scope, freshness against the disk, timeout apart from failure), recall ranking on
recall bench and the manifest. Every value here is a placeholder."""
import json
import os
import sqlite3
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yerk_ai import memindex, memory
from yerk_ai.config import EXAMPLE_PRIVATE, REPO

UTC = timezone.utc


def tree(tmp_path: Path) -> memory.Settings:
    private = tmp_path / "private"
    (private / "memory" / "facts").mkdir(parents=True)
    (private / "memory" / "daily").mkdir(parents=True)
    (private / "memory" / "facts" / "kettle.md").write_text(
        "# Kitchen\n\nThe example kettle in an example room is painted teal.\n")
    (private / "memory" / "daily" / "2026-01-05.md").write_text(
        "# 2026-01-05\n\n- 09:00 a person: kettle kettle kettle kettle kettle\n"
        "- 09:01 a person: lunch at example.com with a person\n")
    (private / "memory" / "pinned.md").write_text("# Pinned\n\nA person prefers tea in the morning.\n")
    (private / "memory" / "open-loops.md").write_text("# Open loops\n\n- send a person the example file\n")
    return memory.settings(private, tmp_path / "state", {"timezone": "UTC"})


def build(s: memory.Settings) -> memindex.Index:
    idx = memindex.Index(s.index_db, s.scope)
    idx.sync()
    return idx


# capture
def test_capture_files_text_attachment_only_and_one_character_once_each(tmp_path):
    log = memory.DailyLog(tmp_path / "daily", UTC)
    at = datetime(2026, 1, 5, 9, 30, 15, tzinfo=UTC)
    assert log.capture("an-example-room", "101", "owner", "a plain line\nwith a second line", when=at)
    assert log.capture("an-example-room", "102", "test-webhook", "", [("photo.png", "image/png", 1234)], when=at)
    assert log.capture("an-example-room", "103", "owner", "k", when=at)
    assert not log.capture("an-example-room", "101", "owner", "a plain line\nwith a second line", when=at)
    again = memory.DailyLog(tmp_path / "daily", UTC)                 # a restart, then a replay
    assert not again.capture("an-example-room", "102", "test-webhook", "", [("photo.png", "image/png", 1234)], when=at)
    assert again.capture("an-example-room", "101", "owner", "the new text", when=at, edited=True)
    assert not again.capture("an-example-room", "101", "owner", "the new text", when=at, edited=True)
    text = (tmp_path / "daily" / "2026-01-05.md").read_text()
    assert text.splitlines() == [
        "# 2026-01-05", "",
        "- 09:30:15+00:00 an-example-room owner: a plain line / with a second line [c:private] [m:101]",
        "- 09:30:15+00:00 an-example-room test-webhook: (attachment only) [c:private] [m:102]",
        "  - attachment: photo.png, image/png, 1,234 bytes",
        "- 09:30:15+00:00 an-example-room owner: k [c:private] [m:103]",
        text.splitlines()[-1]]
    assert text.splitlines()[-1].startswith("- 09:30:15+00:00 an-example-room owner: (edited) the new text [c:private] [m:101.e")
    assert oct((tmp_path / "daily" / "2026-01-05.md").stat().st_mode & 0o777) == "0o600"


def test_the_day_is_the_messages_own_in_the_install_time_zone(tmp_path):
    log = memory.DailyLog(tmp_path / "daily", timezone(timedelta(hours=-5)))
    log.capture("an-example-room", "201", "owner", "late", when=datetime(2026, 1, 6, 4, 59, tzinfo=UTC))
    log.capture("an-example-room", "202", "owner", "replayed after midnight", when=datetime(2026, 1, 6, 4, 59, tzinfo=UTC))
    assert sorted(p.name for p in (tmp_path / "daily").iterdir()) == ["2026-01-05.md"]
    assert "- 23:59:00-05:00 an-example-room owner: late [c:private] [m:201]" in (tmp_path / "daily" / "2026-01-05.md").read_text()


# the line's shape (restart test R5): the offset per line from the zone, the room's id, the author, the class
def test_the_offset_is_the_lines_own_and_the_room_and_author_carry_an_id_and_a_handle(tmp_path):
    from zoneinfo import ZoneInfo
    log = memory.DailyLog(tmp_path / "daily", ZoneInfo("America/Toronto"),
                          rooms={"an-example-room": ("discord:1000", "room")})
    log.capture("an-example-room", "301", "owner", "winter", who="example_handle", when=datetime(2026, 1, 10, 15, 0, tzinfo=UTC))
    log.capture("an-example-room", "302", "owner", "summer", when=datetime(2026, 7, 10, 15, 0, tzinfo=UTC))
    log.capture("unlisted-room", "303", "other", "no id on file", when=datetime(2026, 7, 10, 15, 0, tzinfo=UTC))
    jan = (tmp_path / "daily" / "2026-01-10.md").read_text().splitlines()[-1]
    jul = (tmp_path / "daily" / "2026-07-10.md").read_text().splitlines()
    assert jan == "- 10:00:00-05:00 an-example-room#discord:1000 owner@example_handle: winter [c:room] [m:301]"
    assert jul[-2] == "- 11:00:00-04:00 an-example-room#discord:1000 owner: summer [c:room] [m:302]"
    assert jul[-1] == "- 11:00:00-04:00 unlisted-room other: no id on file [c:private] [m:303]"
    ln = memory.parse_line(jan)
    assert ln and (ln.offset, ln.label, ln.room_id, ln.role, ln.who, ln.cls, ln.mid) == (
        "-05:00", "an-example-room", "discord:1000", "owner", "example_handle", "room", "301")
    old = memory.parse_line("- 09:30:15 an-example-room owner: a plain line [m:7]")   # the shape before this change
    assert old and (old.offset, old.room_id, old.who, old.cls, old.body, old.mid) == ("", "", "", "", "a plain line", "7")
    assert old.of_room("an-example-room", "discord:1000") and ln.of_room("renamed-room", "discord:1000")
    assert not ln.of_room("an-example-room", "discord:2000")      # another room with this name is another room
    assert memory.system_zone()                                  # a zone name, never a fixed offset


def test_the_migration_rewrites_old_lines_once_with_a_backup_and_leaves_new_ones(tmp_path):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Toronto")
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-03-08.md").write_text(                        # the clocks change at 02:00 that day
        "# 2026-03-08\n\n- 01:30:00 an-example-room owner: before the change [m:1]\n"
        "- 03:30:00 an-example-room yerk: after the change [m:2]\n  - attachment: a.png, image/png, 10 bytes\n"
        "- 04:00:00 other-room owner: another room, no id on file [m:3]\n"
        "- 05:00:00-04:00 an-example-room#discord:1000 owner@h: already new [c:room] [m:4]\n")
    (daily / "2026-11-01.md").write_text("# 2026-11-01\n\n- 01:30:00 an-example-room owner: the repeated hour [m:5]\n")
    rooms = {"an-example-room": ("discord:1000", "room")}
    st = memory.migrate_daylogs(daily, tz, rooms, tmp_path / "backup")
    assert (st["files"], st["rewritten"], st["kept"]) == (2, 4, 1)
    lines = (daily / "2026-03-08.md").read_text().splitlines()
    assert lines[2:] == [
        "- 01:30:00-05:00 an-example-room#discord:1000 owner: before the change [c:room] [m:1]",
        "- 03:30:00-04:00 an-example-room#discord:1000 yerk: after the change [c:room] [m:2]",
        "  - attachment: a.png, image/png, 10 bytes",
        "- 04:00:00-04:00 other-room owner: another room, no id on file [c:private] [m:3]",
        "- 05:00:00-04:00 an-example-room#discord:1000 owner@h: already new [c:room] [m:4]"]
    assert (daily / "2026-11-01.md").read_text().splitlines()[-1].startswith("- 01:30:00-04:00 ")   # the first offset
    assert (tmp_path / "backup" / "2026-03-08.md").read_text().splitlines()[2].startswith("- 01:30:00 an-example-room owner")
    again = memory.migrate_daylogs(daily, tz, rooms, tmp_path / "backup")
    assert (again["rewritten"], again["kept"]) == (0, 5)
    s = memory.settings(tmp_path, tmp_path / "state", {"timezone": "America/Toronto", "root": "."})
    hits = memory.topic_hits(s, "an-example-room", "before the change?", set(), datetime(2026, 3, 9, tzinfo=UTC), rid="discord:1000")
    by = {h["cite"]: h["text"] for h in hits}                                   # read back across the migration
    assert by["daily/2026-03-08.md:3"] == "before the change" and "other room" not in str(hits)


# privacy classes (restart test R2): a room reads shared files and its own lines, an owner room everything
def test_a_room_cannot_read_another_rooms_private_lines_or_files(tmp_path):
    s = tree(tmp_path)
    (s.root / "facts" / "shared.md").write_text("---\nclass: shared\n---\n# Shared\n\nThe example kettle is shared knowledge.\n")
    (s.root / "facts" / "side.md").write_text("# Side\n\nThe example kettle line for room b only.\n")
    (s.root / "facts" / "side.md.class").write_text("room b-id\n")
    (s.root / "daily" / "2026-01-05.md").write_text(
        "# 2026-01-05\n\n- 09:00:00+00:00 room-a owner: kettle kettle kettle in room a [c:private] [m:1]\n"
        "- 09:01:00+00:00 room-b#b-id other@h: kettle kettle kettle in room b [c:room] [m:2]\n"
        "- 09:02:00 room-a owner: an old line, kettle [m:3]\n")
    build(s)
    owner, b, nobody = memory.Reader("room-a", "room-a", True), memory.Reader("room-b", "b-id", False), memory.Reader()
    paths = lambda r: sorted({h["cite"].rpartition(":")[0].rsplit("/", 1)[-1] for h in r["hits"]})  # noqa: E731
    assert paths(memory.recall(s, "kettle", 20, reader=owner)) == ["2026-01-05.md", "kettle.md", "shared.md", "side.md"]
    got = memory.recall(s, "kettle", 20, reader=b)
    assert paths(got) == ["2026-01-05.md", "shared.md", "side.md"]
    day = next(h for h in got["hits"] if "/2026-01-05.md:" in h["cite"])
    assert "in room b" in day["text"] and "in room a" not in day["text"] and "an old line" not in day["text"]
    assert paths(memory.recall(s, "kettle", 20, reader=nobody)) == ["shared.md"]
    assert not memory.read(s, "facts/kettle.md", reader=b)["ok"] and memory.read(s, "facts/kettle.md", reader=owner)["ok"]
    assert memory.read(s, "facts/side.md", reader=b)["ok"] and not memory.read(s, "facts/side.md", reader=nobody)["ok"]
    r = memory.read(s, "daily/2026-01-05.md", 3, 3, reader=b)
    assert r["ok"] and r["text"].splitlines() == ["(a line of another room)",
                                                  "- 09:01:00+00:00 room-b#b-id other@h: kettle kettle kettle in room b [c:room] [m:2]",
                                                  "(a line of another room)"]
    assert memory.file_class(s.root / "facts" / "shared.md") == ("shared", "")
    assert memory.file_class(s.root / "facts" / "side.md") == ("room", "b-id")
    assert memory.file_class(s.root / "facts" / "kettle.md") == ("private", "")
    assert memory.room_class({"privacy": "owner"}) == "private" and memory.room_class({"privacy": "family"}) == "room"
    assert memory.room_class({"privacy": "family", "class": "shared"}) == "shared"


def test_a_free_form_line_and_a_date_named_note_stay_with_the_owner(tmp_path):
    """Cold read F1: a line in a day log that is not a day-log line (a hand edit, an import) is blanked for
    a room that is not an owner room, and a date-named file outside daily/ is a plain file, class-checked."""
    s = tree(tmp_path)
    (s.root / "daily" / "2026-01-06.md").write_text(
        "# 2026-01-06\n\nA FREE-FORM NOTE beside the lines\n"
        "- 10:00:00+00:00 room-a owner: the owner's line [c:private] [m:1]\n  - attachment: owner-file.pdf, pdf, 1 bytes\n"
        "- 10:01:00+00:00 room-b#b-id other: the room's own line [c:room] [m:2]\n  - attachment: own-file.png, image/png, 2 bytes\n")
    (s.root / "2026-01-06.md").write_text("# 2026-01-06\n\nA DATE-NAMED NOTE at the root, no class\n")
    (s.root / "2026-01-09.md").write_text("---\nclass: shared\n---\n# 2026-01-09\n\nA DATE-NAMED NOTE marked shared\n")
    owner, b = memory.Reader("room-a", "room-a", True), memory.Reader("room-b", "b-id", False)
    r = memory.read(s, "daily/2026-01-06.md", 1, 40, reader=b)
    assert r["ok"] and r["text"].splitlines() == [
        "# 2026-01-06", "", "(a line of another room)", "(a line of another room)", "(a line of another room)",
        "- 10:01:00+00:00 room-b#b-id other: the room's own line [c:room] [m:2]",
        "  - attachment: own-file.png, image/png, 2 bytes"]
    assert "FREE-FORM" in memory.read(s, "daily/2026-01-06.md", 1, 40, reader=owner)["text"]
    root_note = memory.read(s, "2026-01-06.md", 1, 40, reader=b)
    assert not root_note["ok"] and "keeps it to the owner's rooms" in root_note["error"]
    assert "DATE-NAMED NOTE at the root" in memory.read(s, "2026-01-06.md", 1, 40, reader=owner)["text"]
    assert "marked shared" in memory.read(s, "2026-01-09.md", 1, 40, reader=b)["text"]   # shared by its front matter: whole
    build(s)
    for room in (b, memory.Reader()):                                    # recall: the same lines, the same files
        texts = " ".join(h["text"] for h in memory.recall(s, "FREE-FORM DATE-NAMED NOTE", 20, reader=room)["hits"])
        assert "FREE-FORM" not in texts and "at the root" not in texts and "marked shared" in texts
    assert "FREE-FORM" in " ".join(h["text"] for h in memory.recall(s, "FREE-FORM NOTE", 20, reader=owner)["hits"])


def test_a_former_rooms_name_is_not_a_non_owner_rooms_history(tmp_path):
    """Cold read F2: an old line (no id) matches a room by name for an owner room only; a room that is
    not an owner room and carries that name with another id gets no item and no topic hit from it."""
    s = tree(tmp_path)
    (s.root / "daily" / "2026-01-05.md").write_text(
        "# 2026-01-05\n\n- 09:00:00 family owner: the former family room decided on the teal kettle [m:1]\n"
        "- 09:01:00+00:00 family#f-new owner: the new family room decided on a red kettle [c:room] [m:2]\n")
    now = datetime(2026, 1, 5, 20, tzinfo=UTC)
    older = memory.room_items(s, "family", now, rid="f-new", owner=False)
    assert [i["text"] for i in older] == ["the new family room decided on a red kettle"]
    hits = memory.topic_hits(s, "family", "decided on the kettle?", set(), now, rid="f-new", owner=False)
    assert [h["cite"] for h in hits] == ["daily/2026-01-05.md:4"]
    assert memory.room_pack(s, "family", 6000, now, rid="f-new", owner=False)[1] == 1
    assert len(memory.room_items(s, "family", now, rid="f-new")) == 2                  # the owner view, by name too
    assert len(memory.topic_hits(s, "family", "decided on the kettle?", set(), now, rid="f-new")) == 2
    assert len(memory.room_items(s, "family", now, rid="family", owner=False)) == 0   # an old line never reaches a non-owner room


def test_a_line_the_daemon_files_during_the_migrations_rewrite_lands_in_the_new_file(tmp_path, monkeypatch):
    """Cold read F3: the daemon's capture and the migration's rewrite hold one lock, so an append that
    arrives between the read and the rename waits, then lands in the rewritten file."""
    import threading
    from zoneinfo import ZoneInfo
    tz, rooms = ZoneInfo("America/Toronto"), {"an-example-room": ("discord:1000", "room")}
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-10-18.md").write_text("# 2026-10-18\n\n- 10:00:00 an-example-room owner: an old line [m:1]\n")
    log = memory.DailyLog(daily, tz, rooms)
    filed = threading.Thread(target=log.capture, args=("an-example-room", "2", "owner", "filed during the rewrite"),
                             kwargs={"when": datetime(2026, 10, 18, 14, 0, 5, tzinfo=UTC)})
    real = os.replace

    def replace_while_a_message_lands(a, b):
        if Path(b) == daily / "2026-10-18.md":                  # the day file's rename, not the backup copy's
            filed.start()
            filed.join(0.5)                  # without the lock the line is in the old file by now, and lost
        return real(a, b)
    monkeypatch.setattr(os, "replace", replace_while_a_message_lands)
    st = memory.migrate_daylogs(daily, tz, rooms, tmp_path / "backup")
    filed.join(5)
    assert (st["rewritten"], st["kept"]) == (1, 0) and not filed.is_alive()
    assert (daily / "2026-10-18.md").read_text().splitlines()[2:] == [
        "- 10:00:00-04:00 an-example-room#discord:1000 owner: an old line [c:room] [m:1]",
        "- 10:00:05-04:00 an-example-room#discord:1000 owner: filed during the rewrite [c:room] [m:2]"]
    assert (tmp_path / "backup" / "2026-10-18.md").read_text().endswith("an old line [m:1]\n")
    assert memory.migrate_daylogs(daily, tz, rooms, tmp_path / "backup-2")["rewritten"] == 0
    assert not (tmp_path / "backup-2").exists()                          # nothing to rewrite: nothing copied


def test_the_example_layer_never_captures_into_the_repo(tmp_path):
    s = memory.settings(EXAMPLE_PRIVATE, tmp_path, {})
    assert s.root.is_relative_to(tmp_path) and not s.root.is_relative_to(REPO)
    assert (EXAMPLE_PRIVATE / "memory").resolve() in s.scope.roots


# the index
def test_appending_a_line_rewrites_one_chunk_and_a_touch_writes_none(tmp_path):
    s = tree(tmp_path)
    idx = build(s)
    day = s.root / "daily" / "2026-01-05.md"
    day.write_text("# 2026-01-05\n\n" + "".join(f"- 09:{i:02d} line number {i} of the log\n" for i in range(60)))
    idx.sync()
    before = dict(idx.conn.execute("SELECT id, sha FROM chunks WHERE path = ?", (str(day),)).fetchall())
    assert len(before) > 2
    with day.open("a") as f:
        f.write("- 10:00 one more line\n")
    st = idx.sync()
    assert (st["changed"], st["inserted"], st["deleted"]) == (1, 1, 1)
    after = dict(idx.conn.execute("SELECT id, sha FROM chunks WHERE path = ?", (str(day),)).fetchall())
    assert len(set(before.values()) & set(after.values())) == len(before) - 1   # only the last chunk changed
    os.utime(day, None)
    assert idx.sync()["inserted"] == 0
    day.unlink()
    st = idx.sync()
    assert st["gone"] == 1 and not idx.conn.execute("SELECT 1 FROM chunks WHERE path = ?", (str(day),)).fetchone()


def test_temp_backup_and_hidden_files_stay_out_of_the_index(tmp_path):
    s = tree(tmp_path)
    for rel in ("facts/kettle.pre-move.md", ".drafts/x.md", "overwrite-backups/pinned.md", "notes.md.tmp", "pinned.md.bak"):
        p = s.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("a copy that must not be indexed\n")
    files = s.scope.files()
    assert sorted(Path(p).relative_to(s.root).as_posix() for p in files) == [
        "daily/2026-01-05.md", "facts/kettle.md", "open-loops.md", "pinned.md"]
    with pytest.raises(ValueError):
        memindex.Scope(()).files()


def test_freshness_is_measured_against_the_disk(tmp_path):
    s = tree(tmp_path)
    assert memindex.check(s.index_db, s.scope)["status"] == "failure"          # no index yet
    idx = build(s)
    assert memindex.check(s.index_db, s.scope)["status"] == "fresh"
    pinned = s.root / "pinned.md"
    pinned.write_text("# Pinned\n\nchanged on disk\n")
    assert memindex.check(s.index_db, s.scope)["status"] == "pending"          # younger than the grace
    old = time.time() - 600
    os.utime(pinned, (old, old))
    r = memindex.check(s.index_db, s.scope)
    assert r["status"] == "stale" and r["stale"] == 1 and r["examples"] == [str(pinned)]
    idx.sync()
    assert memindex.check(s.index_db, s.scope)["status"] == "fresh"
    wider = memindex.Scope((*s.scope.roots, tmp_path / "elsewhere"))
    assert memindex.check(s.index_db, wider)["status"] == "scope-changed"
    assert memindex.check(s.index_db, s.scope, deadline_s=-1)["status"] == "timeout"
    idx.close()
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"this is not a database" * 100)
    assert memindex.check(bad, s.scope)["status"] == "failure"
    assert memindex.CHECK_EXIT["timeout"] != memindex.CHECK_EXIT["failure"]


def test_recall_ranks_the_fact_above_a_chunk_that_repeats_one_word(tmp_path):
    s = tree(tmp_path)
    build(s)
    r = memory.recall(s, "What colour is the example kettle in the example room?")
    assert r["ok"] and r["hits"][0]["cite"] == "memory/facts/kettle.md:1" and set(r["hits"][0]) == {"cite", "text"}
    assert "painted teal" in r["hits"][0]["text"]
    assert memory.recall(s, "the of and")["hits"] == []


def test_recall_says_timeout_and_failure_apart(tmp_path, monkeypatch):
    s = tree(tmp_path)
    assert memory.recall(s, "kettle")["status"] == "failure"                   # no index file
    build(s)

    def busy(*a, **k):
        raise sqlite3.OperationalError("interrupted")
    monkeypatch.setattr(memindex, "search", busy)
    r = memory.recall(s, "kettle")
    assert r["status"] == "timeout" and "not down" in r["error"]

    def broken(*a, **k):
        raise sqlite3.DatabaseError("file is not a database")
    monkeypatch.setattr(memindex, "search", broken)
    assert memory.recall(s, "kettle")["status"] == "failure"


# read_memory
def test_read_memory_reads_exact_lines_and_only_inside_the_tree(tmp_path):
    s = tree(tmp_path)
    r = memory.read(s, "facts/kettle.md", 3, 1)
    assert r["ok"] and r["text"] == "The example kettle in an example room is painted teal." and r["total_lines"] == 3
    assert memory.read(s, str(s.root / "pinned.md"), 1, 400)["ok"]
    outside = tmp_path / "private" / "secret.md"
    outside.write_text("not memory\n")
    (s.root / ".hidden").mkdir()
    (s.root / ".hidden" / "x.md").write_text("hidden\n")
    (s.root / "facts" / "link.md").symlink_to(outside)
    for path in ("../secret.md", str(outside), "facts/link.md", ".hidden/x.md", "/etc/hostname"):
        r = memory.read(s, path)
        assert not r["ok"] and ("outside the memory tree" in r["error"] or "no such file" in r["error"]), path
    assert not memory.read(s, "facts")["ok"] and not memory.read(s, "facts/missing.md")["ok"]


def test_a_cite_line_past_nine_digits_reads_as_no_such_file(tmp_path):
    s = tree(tmp_path)
    for path in ("pinned.md:" + "9" * 5000, "pinned.md:" + "1" * 10, "x" * 300 + ".md:1"):   # int() refuses 4,300 digits
        r = memory.read(s, path)
        assert not r["ok"] and "no such file" in r["error"], len(path)
    r = memory.read(s, "pinned.md:" + "9" * 9)                                # nine digits still cite: past the end
    assert r["ok"] and r["text"] == "" and r["total_lines"] == 3


def test_a_nul_in_the_path_reads_as_no_such_file(tmp_path):
    s = tree(tmp_path)
    for path in ("pin\x00ned.md:1", "pinned.md\x00", "facts/\x00", str(s.root / "pinned.md") + "\x00:2"):
        r = memory.read(s, path)
        assert not r["ok"] and "no such file" in r["error"], repr(path)
    assert memory.read(s, "pinned.md:1")["ok"]


# the recall diet: a hit is the lines that carry the match and a cite, one call has a cap
LINES = ["# Example notes", "", "- the lamp sits on the example shelf", "- an unrelated line about the weather",
         "- the kettle is painted teal and lives in the example kitchen", "- another unrelated line", "- a last line"]


def test_the_snippet_keeps_whole_lines_around_the_match_in_file_order():
    text = "\n".join(LINES)
    w = {"kettle": 3.0, "teal": 4.0}
    first, cut = memory.snippet(text, 10, w, cap=120)          # the match mid-file: the line after it first
    assert first == 14 and cut.splitlines() == [LINES[4], LINES[5]]          # whole lines, cut at line ends
    first, cut = memory.snippet(text, 10, w, cap=160)          # then the line before
    assert first == 13 and cut.splitlines() == [LINES[3], LINES[4], LINES[5]]
    first, cut = memory.snippet(text, 1, {"notes": 2.0}, cap=40)               # the match on the file's first line
    assert first == 1 and cut == LINES[0]
    first, cut = memory.snippet(text, 1, {"last": 2.0}, cap=60)                # and on its last line
    assert first == 6 and cut.splitlines() == [LINES[5], LINES[6]]
    first, cut = memory.snippet(text, 1, {"lamp": 2.0, "teal": 2.0}, cap=140)  # two matches with room for the line between
    assert first == 3 and cut.splitlines() == [LINES[2], LINES[3], LINES[4]]
    _, cut = memory.snippet(text, 1, {"lamp": 2.0, "teal": 2.0}, cap=110)      # without it: a gap reads …
    assert cut.splitlines() == [LINES[2], "…", LINES[4]]
    _, cut = memory.snippet("# Head\n\nthe kettle line", 1, {"kettle": 1.0})   # a blank line between is no gap
    assert cut == "# Head\nthe kettle line"
    _, cut = memory.snippet(text, 1, {}, near=LINES[3], cap=60)              # the meaning's line counts as a match
    assert cut == LINES[3]


def test_a_line_longer_than_the_cap_is_cut_around_its_match():
    long = " ".join(f"word{i}" for i in range(200)) + " the example kettle is teal " + " ".join(f"tail{i}" for i in range(200))
    first, cut = memory.snippet(long, 7, {"kettle": 3.0}, cap=120)
    assert first == 7 and "kettle" in cut and cut.startswith("…") and cut.endswith("…") and len(cut) <= 122
    assert cut[1:-1] in long and long[long.index(cut[1:-1]) - 1] == " "      # cut at a space, never mid-word
    _, cut = memory.snippet("kettle " + "x " * 300, 1, {"kettle": 3.0}, cap=80)   # the match at the line's start
    assert cut.startswith("kettle") and cut.endswith("…") and len(cut) <= 81


def test_recall_hands_snippets_in_rank_order_under_the_call_cap(tmp_path):
    s = tree(tmp_path)
    for i in range(12):
        (s.root / "facts" / f"k{i}.md").write_text(f"# K{i}\n\n" + "\n".join(f"- kettle note {i}.{j} " + "x" * 80
                                                                           for j in range(8)) + "\n")
    build(s)
    whole = memory.ranked(s, "kettle note", 20)["hits"]
    r = memory.recall(s, "kettle note", 20)
    assert [h["cite"].rpartition(":")[0] for h in r["hits"]] == [h["cite"].rpartition(":")[0] for h in whole]
    assert len(r["hits"]) == len(whole) > 6 and sum(len(h["text"]) for h in r["hits"][:6]) <= s.recall_chars
    assert all(len(h["text"]) <= memory.SNIPPET + 2 for h in r["hits"])
    assert all(len(h["text"]) <= memory.EXTRA for h in r["hits"][6:])         # past the sixth: a short line on top
    assert sum(len(h["text"]) for h in whole) > s.recall_chars                # the cap did the cutting
    small = memory.settings(tmp_path / "private", tmp_path / "state", {"timezone": "UTC", "recall_chars": 600})
    r = memory.recall(small, "kettle note", 6)
    assert len(r["hits"]) == 6 and sum(len(h["text"]) for h in r["hits"]) <= 600
    f, _, line = r["hits"][0]["cite"].rpartition(":")
    read = memory.read(s, r["hits"][0]["cite"])                               # the cite is what read_memory takes
    assert read["ok"] and read["from"] == int(line) and read["to"] - read["from"] + 1 <= memory.READ_LINES
    assert read["path"].endswith(f.split("/", 1)[1]) and r["hits"][0]["text"].splitlines()[0] in read["text"]
    assert memory.read(s, r["hits"][0]["cite"], 2)["from"] == 2                # a from_line past 1 wins


def test_recall_chars_is_a_described_setting_with_a_floor(tmp_path):
    s = memory.settings(tmp_path / "private", tmp_path / "state", {"timezone": "UTC"})
    assert s.recall_chars == memory.RECALL_CHARS == 6 * memory.SNIPPET
    assert memory.settings(tmp_path / "p", tmp_path / "s", {"recall_chars": 2000}).recall_chars == 2000
    assert memory.settings(tmp_path / "p", tmp_path / "s", {"recall_chars": 5}).recall_chars == 100
    row = next(r for r in memory.described({"recall_chars": 2000}) if r["key"] == "memory.recall_chars")
    assert row["value"] == "2000" and row["source"] == "yerkd.toml" and row["default"] == str(memory.RECALL_CHARS)
    assert next(r for r in memory.described({}) if r["key"] == "memory.recall_chars")["source"] == "default"


def many(s: memory.Settings, n: int = 24) -> None:
    """n kettle notes, each with a short match, a long best line (the match mid-line) and a match further down."""
    for i in range(n):
        (s.root / "facts" / f"t{i}.md").write_text(
            f"# T{i}\n\n- a short kettle line {i}\n- " + " ".join(f"w{i}x{j}" for j in range(40)) + f" the kettle note {i} "
            + " ".join(f"v{j}" for j in range(40)) + "\n- an unrelated line\n- the kettle again, a note " + "y" * (7 * i) + "\n")


def shipped(hits: list[dict], weights: dict[str, float], budget: int) -> list[dict]:
    """snippets() as pull request 30 first shipped it (ad41b75): every hit a share of the whole budget."""
    out = []
    for n, h in enumerate(hits):
        first, text = memory.snippet(str(h.get("text", "")), int(h["line"]), weights, h.get("near"),
                                     min(memory.SNIPPET, budget // (len(hits) - n)))
        budget -= len(text)
        out.append({"cite": f"{h['cite'].rpartition(':')[0]}:{first}", "text": text})
    return out


def test_a_call_of_twenty_shows_its_first_six_as_a_call_of_six_and_a_short_line_past_them(tmp_path):
    s = tree(tmp_path)
    many(s)
    build(s)
    w: dict[str, float] = {}
    hits = memory.ranked(s, "kettle note", 20, weights=w)["hits"]
    assert len(hits) == 20
    twenty = memory.snippets(hits, w, s.recall_chars)
    assert twenty[:6] == memory.snippets(hits[:6], w, s.recall_chars) and memory.recall(s, "kettle note", 20)["hits"] == twenty
    assert all(len(h["text"]) <= memory.EXTRA and "\n" not in h["text"] for h in twenty[6:])     # the best line alone
    assert all("kettle" in h["text"] and "note" in h["text"] for h in twenty[6:])            # cut around the match
    assert any(h["text"].startswith("…") and h["text"].endswith("…") for h in twenty[6:])
    for h, cut in zip(hits[6:], twenty[6:]):                                  # the cite is the best line's own
        line = int(cut["cite"].rpartition(":")[2])
        assert cut["text"].strip("…") in str(h["text"]).splitlines()[line - int(h["line"])]


def test_a_call_of_six_or_fewer_is_cut_byte_for_byte_as_shipped(tmp_path):
    s = tree(tmp_path)
    many(s, 10)
    build(s)
    for q in ("kettle note", "the kettle", "an unrelated line", "lunch with a person", "what colour is the example kettle"):
        w: dict[str, float] = {}
        hits = memory.ranked(s, q, 6, weights=w)["hits"]
        for k in range(1, len(hits) + 1):
            for budget in (100, 600, 4200, s.recall_chars):
                assert json.dumps(memory.snippets(hits[:k], w, budget)) == json.dumps(shipped(hits[:k], w, budget)), (q, k)


# the bench and the manifest
def test_the_bench_scores_top_six_and_top_one_and_flags_a_blind_control(tmp_path):
    s = tree(tmp_path)
    build(s)
    q = tmp_path / "questions.json"
    q.write_text(json.dumps({"questions": [
        {"id": "c1", "q": "what colour is the example kettle", "answer_tokens": ["teal"], "control": True},
        {"id": "q1", "q": "what does a person prefer in the morning", "answer_tokens": ["tea", "morning"]},
        {"id": "q2", "q": "who had lunch at example.com", "answer_tokens": ["lunch", "a person"]},
        {"id": "q3", "q": "what is the example lamp", "answer_tokens": ["lamp", "shelf"]}]}))
    r = memory.bench(s, q)
    assert r["ok"] and r["questions"] == 3 and r["blind"] == []
    assert (r["top_k"], r["top_1"]) == (2, 2) and r["top_k_rate"] == round(2 / 3, 3)
    q.write_text(json.dumps({"questions": [{"id": "c2", "q": "the example lamp", "answer_tokens": ["lamp"], "control": True}]}))
    assert memory.bench(s, q)["blind"] == ["c2"]


def test_the_manifest_declares_every_memory_ability_with_a_tier():
    abilities = tomllib.loads((REPO / "abilities.toml").read_text())["abilities"]
    assert {"capture", "memory-index", "recall", "read-memory", "recall-bench", "mention-recall", "topic-recall",
            "recall-pack", "evidence-ledger"} <= set(abilities)
    for name, a in abilities.items():
        assert a["tier"] in {"free", "key", "paid"} and a["what"] and a["needs"] and a["free_path"], name


def test_the_stall_line_fires_when_the_loop_is_blocked(caplog, tmp_path):
    """The control for the daemon's stall detector: a blocked loop is said, so a quiet log means quiet."""
    import asyncio
    import logging

    from yerk_ai.beat import Beat
    from yerk_ai.ledger import Ledger

    async def go():
        watch = asyncio.create_task(Beat(Ledger(tmp_path / "l.sqlite"), tmp_path / "stalls", every=0.05).tick())
        await asyncio.sleep(0.06)
        time.sleep(0.4)                                                  # the loop held, as index work on it would
        await asyncio.sleep(0.1)
        watch.cancel()
    with caplog.at_level(logging.WARNING, logger="yerk.beat"):
        asyncio.run(go())
    assert any("stall: the event loop was blocked" in r.getMessage() for r in caplog.records)
