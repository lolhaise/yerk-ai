"""Checks location.py, which tells an owner room where you are: the place part on an owner room's message only, fresh and at a named
place, and the day's places as one day-log line that a re-run replaces where it stands. The fixture is a made-up day in the
own shape: a fix every 20 minutes, two drives, a lunch off the office lot, a line that is no fix and one with no point."""
import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from yerk_ai import cli, context, hooks, location, memindex, memory
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms
from yerk_ai.daemon import Yerkd

FIXES = Path(__file__).parent / "fixtures" / "location.jsonl"
PLACES = Path(__file__).parent / "fixtures" / "places.json"
TZ = ZoneInfo("America/Toronto")
LAST = datetime(2026, 10, 23, 4, 30, tzinfo=timezone.utc)          # the fixture's newest fix: home, 00:30 in Toronto
DAY = ("the owner's places from the phone, first and last fix at each: 00:05 to 07:45 home (Example Town); "
       "08:40 to 12:00 the office (Example City); 12:45 to 17:05 the office (Example City); 17:40 to 23:40 home (Example Town)")
OWNER, FAMILY = {"privacy": "owner"}, {"privacy": "family", "people": [31]}


def loc(fixes=FIXES, **conf):
    return location.settings({"file": str(fixes), "places": str(PLACES), **conf}, Path("/nowhere"))


def upto(tmp_path, stamp):
    """The fixture cut after the fix at `stamp`, so that fix is the newest."""
    rows = FIXES.read_text().splitlines()
    path = tmp_path / "fixes.jsonl"
    path.write_text("\n".join(rows[:next(i for i, x in enumerate(rows) if stamp in x) + 1]) + "\n")
    return path


def test_an_owner_rooms_part_is_the_newest_fixs_place_how_long_ago_and_still(tmp_path):
    assert location.part(loc(), OWNER, LAST + timedelta(minutes=7)) == "[place] near home (Example Town), 7 min ago, stationary"
    assert location.part(loc(), {}, LAST + timedelta(seconds=20)) == "[place] near home (Example Town), just now, stationary"
    arrived = loc(upto(tmp_path, "2026-10-22T21:40:00Z"))          # the drive's fix before it is 18 km off: moving
    assert location.part(arrived, OWNER, datetime(2026, 10, 22, 21, 47, tzinfo=timezone.utc)) == \
        "[place] near home (Example Town), 7 min ago, moving"


def test_no_part_in_a_person_room_past_stale_after_at_no_named_place_or_without_the_table(tmp_path):
    assert location.part(loc(), FAMILY, LAST + timedelta(minutes=7)) == ""
    assert location.part(loc(), OWNER, LAST + timedelta(hours=3, minutes=1)) == ""                  # stale_after: 3h unset
    assert location.part(loc(stale_after="20m"), OWNER, LAST + timedelta(minutes=21)) == ""
    assert location.part(loc(stale_after="1h30m"), OWNER, LAST + timedelta(minutes=89)).startswith("[place] near home")
    road = loc(upto(tmp_path, "2026-10-22T21:25:00Z"))                # the newest fix is on the road: no coordinate is said
    assert location.part(road, OWNER, datetime(2026, 10, 22, 21, 30, tzinfo=timezone.utc)) == ""
    assert location.part(loc(fixes=tmp_path / "none.jsonl"), OWNER, LAST) == ""                     # no file yet
    assert location.settings(None, tmp_path) is None and location.settings({"places": str(PLACES)}, tmp_path) is None
    assert location.part(None, OWNER, LAST) == ""
    with pytest.raises(SystemExit, match="not a duration"):
        loc(stale_after="soon")


def test_the_tail_read_gives_the_newest_fixes_whole_and_the_places_table_drops_a_todo_row():
    every = location.fixes(FIXES)
    assert len(every) == 74 and every[-1].at == LAST                 # 76 lines: one is no fix, one has no point
    tail = location.fixes(FIXES, 400)
    assert 0 < len(tail) < 4 and tail == every[-len(tail):]
    assert [label for label, *_ in location.places(PLACES)] == ["home (Example Town)", "the office (Example City)"]


def hook(room, sender, rs):
    out = hooks.user_prompt_submit({"session_id": "sid-1", "prompt": "hi"}, Path("/nowhere"), rs, lambda r, p: (sender, None),
                                   budgets(), now=LAST + timedelta(minutes=7), room=room,
                                   place=lambda r, now: location.part(loc(), rs[r], now))
    return out["hookSpecificOutput"]["additionalContext"]


def test_the_hook_puts_the_place_after_the_metadata_in_an_owner_room_only(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "record", lambda *a, **k: None)
    rs = {**rooms(EXAMPLE_PRIVATE), "family-room": FAMILY}
    lines = hook("example-room", "owner", rs).splitlines()
    assert lines[0].startswith("[clock] ") and lines[1].startswith("[message] ")
    assert lines[2] == "[place] near home (Example Town), 7 min ago, stationary" and len(lines) == 3
    assert "[place]" not in hook("family-room", "owner", rs)                     # a person's room
    assert "[place]" not in hook("example-room", "person", rs)                   # a person's message in an owner room
    assert context.hook_parts("UserPromptSubmit", "\n".join(lines))["place"] == context.chars(lines[2])


def test_the_place_part_has_its_budget_and_the_cap_rose_by_it():
    pm = budgets()["per_message"]
    part = context.per_message("example-room", OWNER, "owner", LAST, None, budgets(), location.part(loc(), OWNER, LAST))[2]
    assert part.name == "place" and part.budget == pm["parts"]["place"] == 150 and not part.over
    assert sum(pm["parts"].values()) + sum(pm["on_hit"].values()) == pm["cap"] == 5950   # need-2026-11-09-74, need-2026-11-19-124 and need-2026-11-21-135: the recall line's 100 each


def install(tmp_path, table=""):
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "yerkd.toml").write_text('[memory]\ntimezone = "America/Toronto"\n' + table)
    (tmp_path / "state").mkdir(exist_ok=True)
    return private


TABLE = f'\n[location]\nfile = "{FIXES}"\nplaces = "{PLACES}"\nstale_after = "3h"\n'


def test_the_daemon_gives_its_owner_rooms_the_part_and_nothing_without_the_table(tmp_path):
    y = Yerkd(install(tmp_path, TABLE), tmp_path / "state")
    assert y.place_for("example-room", LAST + timedelta(minutes=7)).startswith("[place] near home (Example Town)")
    assert y.place_for("no-such-room", LAST) == ""
    bare = Yerkd(install(tmp_path / "bare"), tmp_path / "bare" / "state")
    assert bare.location is None and bare.place_for("example-room", LAST) == ""


def day_log(tmp_path):
    return tmp_path / "private" / "memory" / "daily" / "2026-10-22.md"


def run(tmp_path, capsys, *extra):
    rc = cli.main(["location", "day", "2026-10-22", "--private", str(tmp_path / "private"), "--state", str(tmp_path / "state"), *extra])
    return rc, capsys.readouterr()


def test_the_day_line_is_one_private_line_of_places_and_a_rerun_replaces_it_where_it_stands(tmp_path, capsys):
    install(tmp_path, TABLE)
    log = memory.DailyLog(day_log(tmp_path).parent, TZ)
    log.capture("example-room", "m1", "owner", "morning", when=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc))
    log.capture("example-room", "m2", "owner", "noon", when=datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc))
    rc, out = run(tmp_path, capsys)
    rows = day_log(tmp_path).read_text().splitlines()
    assert rc == 0 and out.out.strip() == rows[-1] and len(rows) == 5
    ln = memory.parse_line(rows[-1])
    assert ln and (ln.time, ln.offset, ln.label, ln.role, ln.body, ln.cls, ln.mid) == (
        "23:40:00", "-04:00", "location", "place", DAY, "private", "location-2026-10-22")
    assert memindex.LINE.match(rows[-1]) and not re.search(r"\d+\.\d{3}", rows[-1])                 # never a coordinate
    text = day_log(tmp_path).read_text()
    family = memory.Reader("family-room", "family-room", owner=False)
    assert memory.allowed_text(memory.Reader("example-room", "example-room", owner=True), day_log(tmp_path), text) == text
    assert DAY not in (memory.allowed_text(family, day_log(tmp_path), text, whole=True) or "")         # no person's room reads it

    log.capture("example-room", "m3", "owner", "evening", when=datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc))
    before = day_log(tmp_path).read_text()
    assert run(tmp_path, capsys)[0] == 0 and day_log(tmp_path).read_text() == before                 # the same day: idempotent
    later = tmp_path / "later.jsonl"                    # one more fix at home, 23:55: the line moves to it, in place
    later.write_text(FIXES.read_text() + json.dumps({"lat": 43.8001, "lon": -40.3001, "speed_mps": None, "ts_iso": "2026-10-23T03:55:00Z"}) + "\n")
    (tmp_path / "private" / "yerkd.toml").write_text('[memory]\ntimezone = "America/Toronto"\n' + TABLE.replace(str(FIXES), str(later)))
    assert run(tmp_path, capsys)[0] == 0
    after = day_log(tmp_path).read_text().splitlines()
    assert after[:4] == before.splitlines()[:4] and after[5:] == before.splitlines()[5:] and after[5].endswith("[m:m3]")
    assert after[4].startswith("- 23:55:00-04:00 location place: ") and "17:40 to 23:55 home (Example Town)" in after[4]
    assert sum("[m:location-2026-10-22]" in x for x in after) == 1


def test_a_dry_run_writes_nothing_and_no_table_or_no_named_place_writes_nothing(tmp_path, capsys):
    install(tmp_path, TABLE)
    rc, out = run(tmp_path, capsys, "--dry-run")
    assert rc == 0 and DAY in out.out and not day_log(tmp_path).exists()
    assert location.day(loc(), day_log(tmp_path).parent, TZ, date(2026, 10, 20)) == ""
    assert not (day_log(tmp_path).parent / "2026-10-20.md").exists()
    install(tmp_path / "bare")
    rc = cli.main(["location", "day", "2026-10-22", "--private", str(tmp_path / "bare" / "private"), "--state", str(tmp_path / "bare" / "state")])
    assert rc == 1 and "no [location] table" in capsys.readouterr().err
    (tmp_path / "private" / "yerkd.toml").write_text('[memory]\ntimezone = "America/Toronto"\n' + TABLE.replace(str(PLACES), str(tmp_path / "gone.json")))
    rc, out = run(tmp_path, capsys)                     # a places table that names nothing: the nightly job alerts, never a silent night
    assert rc == 1 and "names a place" in out.err and not day_log(tmp_path).exists()
