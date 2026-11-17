"""Checks the card a room posts when you type /status there (status.room_card): the machine card's shape, three lines
about the room it was typed in, each account's usage left at two lines each, what runs, today's speed and spend, and
(status.card) keeps its own text: the operator's machine card."""
from __future__ import annotations

import calendar
import json
import time
from types import SimpleNamespace

import pytest
from test_idle_compact import FAKE, ROOM, Chan, say, until, wait_idle
from test_observe import Timers

from yerk_ai import config, status, usage, words
from yerk_ai.accounts import Account, Cooldowns
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import COMPACTED_IN_TURN, Ledger

NOW = float(calendar.timegm((2026, 11, 11, 13, 45, 0)))      # a Wednesday, 1:45p in the zone the tests pin
H, D = 3600.0, 86400.0


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Every time on a card is local: the tests pin one zone so the text is the same on every box."""
    monkeypatch.setenv("TZ", "UTC0")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(day: int, hour: int) -> float:
    return float(calendar.timegm((2026, 11, day, hour, 0, 0)))


def acct(name: str, wk: float, resets: float, fable: float | None = None, bill: int | None = None, rest: float | None = None,
         five: tuple[float, float] | None = None, model_resets: float | None = None, source: str = "live",
         read_at: float = NOW) -> dict:
    m: dict = {"source": source, "at": read_at, "seven_day": {"pct": wk, "resets": resets}}
    if fable is not None:
        m["model"] = {"name": "Fable", "pct": fable, "resets": model_resets or resets}
    if five is not None:
        m["five_hour"] = {"pct": five[0], "resets": five[1]}
    return {"name": name, "meter": m, "rests_until": rest, "bill": status.next_bill(bill, NOW)}


def sample_accounts() -> list[dict]:
    """The brief's five accounts, listed in an order that is not their numbers."""
    return [acct("three", 13, at(16, 14), 11, 14), acct("one", 62, at(13, 23), 100, 8, rest=at(13, 23)),
            acct("two", 19, at(18, 1), 35, 22), acct("five", 52, at(15, 2), 100, 23),
            acct("four", 67, at(12, 0), 100, 21, rest=at(12, 0) + 30)]


def machine_input() -> dict:
    """Every part of the machine card at once, with a head on each room it must ignore."""
    accounts = [*sample_accounts(), acct("six", 40, at(14, 5), 20, 3, five=(91, NOW + 2 * H), model_resets=at(17, 9),
                                         source="stream", read_at=NOW - 2 * H),
                {"name": "seven", "meter": {}, "rests_until": NOW + 3 * H, "bill": None}]
    head = {"model": "claude-fable-5-2", "effort": "max", "account": "two", "context": 212000, "window": 400000, "compactions": 0}
    rooms = [{"room": "example-room", "busy": True, "waiting": 2, "watch": "discord:123", "since": NOW - 75, "account": "two",
              "fast": True, "head": head},
             {"room": "example-quiet-room-with-a-long-name", "busy": True, "waiting": 0, "watch": None, "since": NOW - 900,
              "account": "two", "fast": False, "head": {**head, "context": None}}]
    workers = [{"id": "abc123", "room": "example-room", "account": "one", "started": NOW - 180, "cap": 900, "waiting": False,
                "queued": None, "gone": False},
               {"id": "def456", "room": "example-room", "account": "one", "started": NOW - 60, "cap": 900, "waiting": False,
                "queued": None, "gone": True},
               {"id": "aaa111", "room": "example-room", "account": "one", "started": None, "cap": 900, "waiting": True,
                "queued": NOW - 30, "gone": False}]
    return {"at": NOW, "running": True, "live": True, "rooms": rooms, "version": "9.9.9", "commit": "abcdef1234",
            "units": [{"name": "yerkd", "pid": 1, "procs": 1, "rss_mb": 80.4, "cpu_s": 1.0, "cpu_pct": 1.25},
                      {"name": "example-room", "gone": True}],
            "workers": workers, "jobs": [{"name": "backup", "next": NOW + 600}], "accounts": accounts, "addons": ["web"],
            "stalls": [{"seconds": 3.2}], "speed": {"first": 1.7, "turn": 28.0},
            "spend": {"n": 1, "usd": 12345.67, "week": 98765.43}, "backup": {"at": NOW - 3 * D, "bytes": 123456789}}


# `yerk status` on main f804ae1c for machine_input(), byte for byte: the room card shares its numbers, never its text
MACHINE = [
    '- yerkd running: 2 rooms',
    '- \u2462 wk 13% \xb7 Fable 11%',
    '- \u21bb wk Mon 2p \xb7 \U0001f4b3 Nov 14',
    '\U0001f534 \u2460 wk 62% \xb7 Fable \u26a0\ufe0f100%',
    '- \u21bb resting until Fri 11p \xb7 \U0001f4b3 Dec 8',
    '- \u2461 wk 19% \xb7 Fable 35%',
    '- \u21bb wk Wed 1a \xb7 \U0001f4b3 Nov 22',
    '\U0001f7e0 \u2464 wk 52% \xb7 Fable \u26a0\ufe0f100%',
    '- \u21bb wk Sun 2a \xb7 \U0001f4b3 Nov 23',
    '\U0001f534 \U0001f7e2\u2463 wk 67% \xb7 Fable \u26a0\ufe0f100%',
    '- \u21bb resting until 12a \xb7 \U0001f4b3 Nov 21',
    '\U0001f7e0 \u2465 5h \u26a0\ufe0f91% \xb7 wk 40% \xb7 Fable 20%',
    '- \u21bb 5h 3:45p \xb7 wk Sat 5a \xb7 \U0001f4b3 Dec 3',
    '- \u2465: last reading 2 h 0 min ago',
    '\U0001f534 \u2466: no reading yet',
    '- \u21bb resting until 4:45p',
    '- wk next: \U0001f7e2\u2463 12a \xb7 then \u2460 Fri 11p',
    '- 5h next: \u2465 3:45p',
    '- next turn tries \u2462 first',
    '- example-room: busy 1 min, 2 waiting',
    '\U0001f534 example-quiet-room-wi...: busy 15 min',
    '- example-room: watches 123',
    '- on account \u2461: 2 rooms',
    '- fast mode on: example-room',
    '- yerk 9.9.9 at abcdef1',
    '- add-ons: web',
    '- worker abc123: 3 min of 15',
    '\U0001f534 worker def456: gone, no result',
    '- worker aaa111: waiting, 1st in line',
    '- job backup: in 10 min',
    '- yerkd: 80 MB, 1.2% cpu',
    '- today: first line 1.7 s, turn 28 s',
    '- today US$12345.67 \xb7 week US$98765.43',
    '\U0001f534 backup: 72 h 0 min ago, 117.7 MB',
    '\U0001f534 stalls today: 1, longest 3.2 s',
]


def test_yerk_status_prints_the_same_text_as_on_main_for_the_same_input():
    assert status.card(machine_input()) == MACHINE


def daemon(tmp_path, monkeypatch):
    """A daemon over the example layer on the fake Claude Code, as test_idle_compact's: one account."""
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "mark"))
    d = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))
    d.budgets["session"]["precompact_within"] = 0
    return d


def test_the_broom_is_the_rooms_compactions_today_as_the_ledger_counts_them_so_a_restart_keeps_it(tmp_path, monkeypatch):
    """The number was kept in memory, so a daemon start blanked it until the room's next new session. Now the ledger counts
    the day's: a copy taken, a held /compact, and Claude Code's own inside a turn (marked on that turn's row), once each."""
    start = time.time()                                      # one day for every reading: a midnight during the test moves nothing
    y = daemon(tmp_path, monkeypatch)
    y.budgets["session"]["precompact_within"] = 0.1          # the copy's band, test_idle_compact's: 192,000 to 204,500

    def today(d):
        s = status.collect(EXAMPLE_PRIVATE, tmp_path, d.accounts, d.cooldowns, d.ledger, d.rooms, now=start, units=set, timers=Timers({}))
        return s["rooms"][0]["head"]["compactions"]
    room = y.add_room(ROOM, Chan())
    assert today(y) == 0                                     # no session yet: none today, an int all the same
    say(y, room, "1", "CONTEXT:195000 hello")                # ends in the copy's band: a copy compacted, then taken
    wait_idle(room)
    until(lambda: today(y) == 1 and room.fork is None)
    room.compact_now()                                       # a held /compact (the reaper's, yerk room compact)
    wait_idle(room)
    assert today(y) == 2
    say(y, room, "2", "AUTO_COMPACT and on")                 # Claude Code's own, inside a turn
    wait_idle(room)
    assert today(y) == 3 and y.ledger.rows(1)[0]["note"] == COMPACTED_IN_TURN
    say(y, room, "3", "plain")                               # a turn that compacts nothing
    wait_idle(room)
    y.ledger.record(room=ROOM, started=status.midnight(start) - 90, ended=status.midnight(start) - 60, outcome="compacted")
    y.ledger.record(room="another-room", started=start, ended=start, outcome="compacted")
    assert today(y) == 3                                     # yesterday's and another room's never count
    room.rp.close()
    again = daemon(tmp_path, monkeypatch)                    # a daemon start: the day's count is the ledger's, kept
    again.add_room(ROOM, Chan())
    assert today(again) == 3


def test_collect_gives_each_room_its_head_read_under_its_lock_and_answers_with_its_process_closed(tmp_path, monkeypatch):
    y = daemon(tmp_path, monkeypatch)
    room = y.add_room(ROOM, Chan())
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)

    def head():
        s = status.collect(EXAMPLE_PRIVATE, tmp_path, y.accounts, y.cooldowns, y.ledger, y.rooms, units=set, timers=Timers({}))
        return s["rooms"][0]["head"]
    assert head() == {"model": "claude-fable-5-1", "effort": "", "account": "one", "context": 200000, "window": 250000,
                      "compactions": 0}
    room.rp.close()
    room.rp = None                                           # the process gone: the account its next turn tries
    assert head()["account"] == "one" and head()["context"] == 200000
    room.renew()                                             # a /new waiting: the next reply starts a new session
    assert head()["context"] is None
    (tmp_path / "rooms" / ROOM).mkdir(parents=True, exist_ok=True)
    (tmp_path / "rooms" / ROOM / "override.json").write_text('{"model": "claude-opus-5-5", "effort": "max"}')
    assert (head()["model"], head()["effort"]) == ("claude-opus-5-5", "max")      # its /model and /effort over rooms.toml


def test_a_room_put_on_a_resting_account_names_the_account_its_next_turn_would_try(tmp_path):
    accounts = [Account("one", tmp_path / "cfg1"), Account("two", tmp_path / "cfg2")]
    cool, room = Cooldowns(tmp_path / "cooldowns.json"), SimpleNamespace(conf={"model": "claude-fable-5-2"})
    (tmp_path / "rooms" / "example-room").mkdir(parents=True)
    (tmp_path / "rooms" / "example-room" / "override.json").write_text('{"account": "two"}')

    def account() -> str:                                    # no live process: rp is None
        return status.head(tmp_path, "example-room", room, None, (0, 0, None, True), accounts, cool, config.models({}), NOW)["account"]
    assert account() == "two"                                # put on two, which is free: its own account
    cool.set("two", "limit", NOW + H)                        # two rests: the room's next turn skips it (Room._account)
    assert account() == "one"
    cool.set("one", "limit", NOW + H)                        # every account rests: pick's last one, as the turn would get
    assert account() == "one"


# the room card: the brief's sample, line for line, from room_input()
CARD = """\
🧠 claude-fable-5-2 · ②
📚 212k/400k (53%) · 🧹 0 today
⚙️ Think: max

⛽ Tank
① wk 62% · Fable ⚠️100%
↻ wk Fri 11p · 💳 Dec 8
➤ ② wk 19% · Fable 35%
↻ wk Wed 1a · 💳 Nov 22
③ wk 13% · Fable 11%
↻ wk Mon 2p · 💳 Nov 14
🟢④ wk 67% · Fable ⚠️100%
↻ wk 12a · 💳 Nov 21
⑤ wk 52% · Fable ⚠️100%
↻ wk Sun 2a · 💳 Nov 23
wk next: 🟢④ 12a · then ① Fri 11p

🔨 Quiet, nothing running
⏱ today: first line 12 s · turn 28 s
💵 today US$0.27 · week US$4.21"""
SHOWN: list[list[str]] = []                                  # every card a case renders, for the width test at the end


def room_input(**change) -> dict:
    """The brief's sample: the room on ②, live meters, nothing running, a fresh backup, an uncapped install."""
    head = {"model": "claude-fable-5-2", "effort": "max", "account": "two", "context": 212000, "window": 400000, "compactions": 0}
    s = {"at": NOW, "running": True, "live": True, "accounts": sample_accounts(), "workers": [], "jobs": [], "stalls": [],
         "rooms": [{"room": "example-room", "busy": False, "waiting": 0, "watch": None, "since": None, "account": "",
                    "fast": False, "head": head}],
         "speed": {"first": 12.0, "turn": 28.0}, "spend": {"n": 3, "usd": 0.27, "week": 4.21},
         "backup": {"at": NOW - H, "bytes": 1000}, "units": []}
    head.update(change.pop("head", {}))
    return {**s, **change}


def card(s: dict, room: str = "example-room") -> list[str]:
    SHOWN.append(lines := status.room_card(s, room))
    return lines


def test_the_room_card_reads_line_for_line_as_the_box_card_does():
    assert "\n".join(card(room_input())) == CARD


def test_the_room_is_on_the_account_its_head_names_and_the_tank_marks_it():
    lines = card(room_input(head={"account": "three"}))
    assert lines[0] == "🧠 claude-fable-5-2 · ③" and "➤ ③ wk 13% · Fable 11%" in lines and "② wk 19% · Fable 35%" in lines
    assert sum(x.startswith(words.ON_THIS) for x in lines) == 1


def test_the_room_on_the_soonest_reset_wears_both_marks():
    assert "➤ 🟢④ wk 67% · Fable ⚠️100%" in card(room_input(head={"account": "four"}))


def test_a_rest_until_other_than_the_weekly_reset_is_said_and_one_until_it_is_not():
    s = room_input()
    s["accounts"][2]["rests_until"] = NOW + 3 * H            # two rests until 4:45p, its week resets Wed 1a
    lines = card(s)
    assert "➤ ② wk 19% · Fable 35% · 💤 4:45p" in lines
    assert "① wk 62% · Fable ⚠️100%" in lines and "🟢④ wk 67% · Fable ⚠️100%" in lines      # rest until their own week
    assert not any("resting" in x for x in lines)


def test_a_rest_too_wide_for_the_first_line_gets_a_line_of_its_own():
    s = room_input(head={"account": "four"})
    s["accounts"][4] = acct("four", 7, at(12, 0), 2, 21, rest=NOW + 3 * H + 600, five=(1, NOW + 4 * H))
    lines = card(s)
    i = lines.index("➤ 🟢④ 5h 1% · wk 7% · Fable 2%")
    assert lines[i + 1:i + 3] == ["↻ 5h 5:45p · wk 12a · 💳 Nov 21", "💤 4:55p"]


def test_a_reading_that_is_not_live_says_its_age_under_its_account():
    s = room_input()
    s["accounts"][1] = acct("one", 62, at(13, 23), 100, 8, source="stream", read_at=NOW - 2 * H)
    lines = card(s)
    i = lines.index("① wk 62% · Fable ⚠️100%")
    assert lines[i + 1:i + 3] == ["↻ wk Fri 11p · 💳 Dec 8", "①: last reading 2 h 0 min ago"]


# /status's first answer, built with no live ask (status_now({}), collect's live False): account one's stream reading in
# meters.json, and its last live reading in usage.json, kept there whatever its age; the stream's is the newer
STREAM = {"at": NOW - 120, "source": "stream", "seven_day": {"pct": 44.0, "resets": at(16, 14)}}
ON_MAIN = ["🧠", "📚 new session", "", "⛽ Tank", "① wk 44%", "↻ wk Mon 2p", "", "🔨 Quiet, nothing running",
           "🔴 backup: none yet"]                         # main 61e3cf2's card for STREAM, byte for byte
LIVE_ON_MAIN = [*ON_MAIN[:4], "① 5h 12% · wk 41% · Fable ⚠️83%", "↻ 5h 3:45p · wk Mon 2p · Fable Sat 9a", *ON_MAIN[6:]]   # for kept(0)


def kept(age: float, model_resets: float | None = at(14, 9)) -> dict:
    """Account one's last live reading `age` seconds old, as usage.readings keeps it: a week and a 5-hour window older than
    the stream's, and the model's own week, which no stream reads."""
    return {"five_hour": {"pct": 12.0, "resets": NOW + 2 * H}, "seven_day": {"pct": 41.0, "resets": at(16, 14)},
            "model": {"name": "Fable", "pct": 83.0, "resets": model_resets}, "at": NOW - age, "source": "live"}


def on_hand(tmp_path, last: dict | None, stream: dict | None = STREAM, live: bool = False) -> tuple[dict, list[str]]:
    """The answer and the room's card from these files (no usage.json with `last` None, no stream reading with `stream`
    None), asked nothing; with `live`, the live card, whose /usage call reads kept(0)'s meters."""
    (tmp_path / "meters.json").write_text(json.dumps({"one": stream} if stream else {}))
    (tmp_path / usage.FILE).unlink(missing_ok=True)
    if last is not None:
        (tmp_path / usage.FILE).write_text(json.dumps({"one": last}))
    asked = {k: v for k, v in kept(0).items() if k not in ("at", "source")}
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, [Account("one", tmp_path / "cfg1")], Cooldowns(None), Ledger(tmp_path / "l.sqlite"),
                       now=NOW, units=set, timers=Timers({}), live=live, read_one=lambda a: dict(asked))
    return (s := json.loads(json.dumps(s))), card(s)


def test_the_first_answer_has_the_models_own_week_from_the_last_live_reading_beside_the_streams_newer_numbers(tmp_path):
    """(a) The first answer showed the week alone and the model's own week only with the edit: no stream reads that meter.
    It comes now from the account's last live reading, ten minutes old here, the week and the 5-hour still the stream's,
    and no age line while that reading is under an hour old. The payload (status_now, `yerk status --json`) marks the
    entry with the time of the reading it came from, and the meter stays the stream's, so it never reads as live."""
    s, lines = on_hand(tmp_path, kept(600))
    assert lines[3:7] == ["⛽ Tank", "① wk 44% · Fable ⚠️83%", "↻ wk Mon 2p · Fable Sat 9a", ""]
    assert not any("last reading" in x for x in lines)
    m = status.document(EXAMPLE_PRIVATE, tmp_path, s, Ledger(tmp_path / "l.sqlite"), NOW)["accounts"][0]["meter"]
    assert m == {**STREAM, "model": {**kept(600)["model"], "at": NOW - 600}} and m["source"] == "stream"


def test_a_kept_model_week_an_hour_old_or_more_says_so_once_on_the_cards_own_age_line(tmp_path):
    """(b) Kept two hours: the card's own age line says it, once. Where the stream's reading earns that line too, it is
    still one line, and it says the older of the two ages, so no number on the account's line is older than it says. (A
    kept reading newer than the stream's is the meter whole: kept-fresh (a) and (c) below.)"""
    _, lines = on_hand(tmp_path, kept(2 * H))
    assert lines[4:7] == ["① wk 44% · Fable ⚠️83%", "↻ wk Mon 2p · Fable Sat 9a", "①: last reading 2 h 0 min ago"]
    assert sum("last reading" in x for x in lines) == 1
    _, lines = on_hand(tmp_path, kept(3 * H), {**STREAM, "at": NOW - 2 * H})
    assert "① wk 44% · Fable ⚠️83%" in lines and [x for x in lines if "last reading" in x] == ["①: last reading 3 h 0 min ago"]


def test_a_kept_model_week_whose_reset_has_passed_or_was_never_read_is_never_shown(tmp_path):
    """(c) Its week is over, so its number is wrong: the card and the payload are as they were."""
    for resets in (NOW - 60, None):
        s, lines = on_hand(tmp_path, kept(600, resets))
        assert lines == ON_MAIN and s["accounts"][0]["meter"] == STREAM


def test_no_kept_reading_one_that_failed_or_no_stream_reading_leaves_the_card_as_it_was(tmp_path):
    """(d) No live reading kept, or the last one an error (a failed call is kept as one, under a minute old or not): exactly
    today's card. An account no stream has reported yet shows its kept live reading whole (kept-fresh), else "no reading yet"."""
    assert on_hand(tmp_path, None)[1] == ON_MAIN
    for age in (600, 30):
        assert on_hand(tmp_path, {"error": "no answer in 15 s", "at": NOW - age, "source": "live"})[1] == ON_MAIN
    assert on_hand(tmp_path, kept(600), None)[1] == LIVE_ON_MAIN and on_hand(tmp_path, None, None)[1][4] == "①: no reading yet"


def test_a_live_reading_under_a_minute_old_and_the_live_card_are_what_main_prints(tmp_path):
    """(f) A reading under a minute old is the account's whole meter, and the live card asks again whatever is kept: both
    cards byte for byte main's."""
    assert on_hand(tmp_path, kept(30))[1] == LIVE_ON_MAIN
    s, lines = on_hand(tmp_path, kept(600), live=True)
    assert lines == LIVE_ON_MAIN and s["accounts"][0]["meter"] == {**kept(0), "at": NOW}


# kept-fresh: the daemon reads each account's meters again once its kept reading is usage.EVERY old (usage.keep), so a card
# that asks nothing shows that reading whole where it is newer than the stream's, as source "kept" with its own time
ON_HAND_MAIN = [*ON_MAIN[:4], "① wk 44% · Fable ⚠️83%", "↻ wk Mon 2p · Fable Sat 9a", *ON_MAIN[6:]]   # main 714fa2b's, kept(600)
TWO_LIVE_ON_MAIN = [*LIVE_ON_MAIN[:6], "🟢② 5h 12% · wk 7% · Fable ⚠️83%", "↻ 5h 3:45p · wk Fri 2a · Fable Sat 9a",
                    "wk next: 🟢② Fri 2a · then ① Mon 2p", "5h next: ① 3:45p · then ② 3:45p", *ON_MAIN[6:]]   # main 714fa2b's


def test_kept_fresh_a_kept_reading_newer_than_the_streams_is_the_first_answers_meter_whole_and_says_no_age(tmp_path):
    """kept-fresh (a) The stream's reading 8 hours old (no turn on the account since the morning) and the kept live reading
    10 minutes old: the first answer showed the stream's week with "last reading 8 h ago" under it. Now it shows the kept
    reading's 5-hour, week and model, as the live card does, and no age line; the payload (`yerk status --json`) marks it
    "kept" with its own time, so it never reads as live."""
    s, lines = on_hand(tmp_path, kept(600), {**STREAM, "at": NOW - 8 * H})
    assert lines == LIVE_ON_MAIN and not any("last reading" in x for x in lines)
    m = status.document(EXAMPLE_PRIVATE, tmp_path, s, Ledger(tmp_path / "l.sqlite"), NOW)["accounts"][0]["meter"]
    assert m == {**kept(600), "source": "kept"} and m["at"] == NOW - 600


def test_kept_fresh_b_a_stream_reading_newer_than_the_kept_one_is_todays_card_byte_for_byte(tmp_path):
    """kept-fresh (b) The stream's reading 2 minutes old, the kept one 10: the week is the stream's, the model's own week the
    kept one's, no age line, the payload the stream's with that model entry: main's card and main's meter."""
    s, lines = on_hand(tmp_path, kept(600))
    assert lines == ON_HAND_MAIN and s["accounts"][0]["meter"] == {**STREAM, "model": {**kept(600)["model"], "at": NOW - 600}}


def test_kept_fresh_c_a_kept_reading_two_hours_old_and_newer_says_its_age_once_and_a_passed_window_is_left_out(tmp_path):
    """kept-fresh (c) Kept two hours ago (the account could not be read since) and newer than the stream's: its numbers,
    and the age line says 2 hours, once. A window of it whose reset has passed is left out: its number is over."""
    _, lines = on_hand(tmp_path, kept(2 * H), {**STREAM, "at": NOW - 3 * H})
    assert lines[4:7] == [LIVE_ON_MAIN[4], LIVE_ON_MAIN[5], "①: last reading 2 h 0 min ago"]
    assert sum("last reading" in x for x in lines) == 1
    over = {**kept(2 * H), "five_hour": {"pct": 12.0, "resets": NOW - 60}}
    s, lines = on_hand(tmp_path, over, {**STREAM, "at": NOW - 3 * H})
    assert lines[4:7] == ["① wk 41% · Fable ⚠️83%", "↻ wk Mon 2p · Fable Sat 9a", "①: last reading 2 h 0 min ago"]
    assert "five_hour" not in s["accounts"][0]["meter"]


def test_kept_fresh_j_a_live_reading_under_a_minute_old_on_every_account_is_the_card_main_prints(tmp_path):
    """kept-fresh (j) Two accounts, each read live under a minute ago, one stream's reading 8 hours old and one 2 minutes:
    the card that asks nothing is the live card, byte for byte main's, and nothing is asked."""
    accounts = [Account("one", tmp_path / "cfg1"), Account("two", tmp_path / "cfg2")]
    (tmp_path / "meters.json").write_text(json.dumps({"one": {**STREAM, "at": NOW - 8 * H}, "two": STREAM}))
    (tmp_path / usage.FILE).write_text(json.dumps({"one": kept(30), "two": {**kept(50), "seven_day": {"pct": 7.0, "resets": at(13, 2)}}}))
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, accounts, Cooldowns(None), Ledger(tmp_path / "l.sqlite"), now=NOW, units=set,
                       timers=Timers({}), read_one=lambda a: pytest.fail("asked"))
    assert card(json.loads(json.dumps(s))) == TWO_LIVE_ON_MAIN


def test_a_five_hour_window_comes_first_and_has_its_next_line():
    s = room_input()
    s["accounts"][0] = acct("three", 13, at(16, 14), 11, 14, five=(91, NOW + 2 * H))
    lines = card(s)
    i = lines.index("③ 5h ⚠️91% · wk 13% · Fable 11%")
    assert lines[i + 1] == "↻ 5h 3:45p · wk Mon 2p · 💳 Nov 14"
    assert lines[lines.index("wk next: 🟢④ 12a · then ① Fri 11p") + 1] == "5h next: ③ 3:45p"


def test_fast_mode_on_ends_the_think_line():
    s = room_input()
    s["rooms"][0]["fast"] = True
    assert card(s)[2] == "⚙️ Think: max · Fast: on"
    assert card(room_input(head={"effort": ""}))[2] == ""        # no level named and fast off: no line at all
    s["rooms"][0]["head"]["effort"] = ""
    assert card(s)[2] == "⚙️ Fast: on"


def test_a_room_with_no_session_yet_says_so():
    assert card(room_input(head={"context": None}))[1] == "📚 new session"


def test_a_part_that_cannot_be_read_is_left_out():
    assert card(room_input(head={"window": 0}))[1] == "📚 212k · 🧹 0 today"
    assert card(room_input(head={"compactions": None}))[1] == "📚 212k/400k (53%)"
    assert card(room_input(head={"context": 1049000, "window": 1000000, "compactions": 3}))[1] == "📚 1.0m/1.0m (105%) · 🧹 3 today"
    assert card(room_input(head={"model": ""}))[0] == "🧠 ②"


def test_the_count_says_it_is_todays_and_fits_its_line_at_the_longest_values():
    assert card(room_input(head={"context": 999000, "window": 1000000, "compactions": 99}))[1] == "📚 999k/1.0m (100%) · 🧹 99 today"
    assert words.width("📚 1000k/1.0m (100%)" + words.ROOM_COMPACTED.format(n=99)) == 34 <= words.PHONE    # 1000k: wider than words.tokens writes


def test_this_rooms_turn_comes_first_with_what_waits_then_other_rooms_then_workers():
    s = room_input()
    s["rooms"] = [{**s["rooms"][0], "room": "example-other", "since": NOW - 120, "head": {}},
                  {**s["rooms"][0], "since": NOW - 23, "waiting": 2}]
    s["workers"] = [{"id": "abc123", "room": "example-room", "account": "one", "started": NOW - 180, "cap": 900,
                     "waiting": False, "queued": None, "gone": False}]
    lines = card(s)
    i = lines.index("🔨 this room: turn 23 s, 2 waiting")
    assert lines[i:i + 3] == ["🔨 this room: turn 23 s, 2 waiting", "🔨 example-other: turn 2 min", "🔨 worker abc123: 3 min of 15"]
    assert "🔨 Quiet, nothing running" not in lines and lines[0] == "🧠 claude-fable-5-2 · ②"


def test_six_running_things_show_three_and_how_many_more():
    s = room_input()
    s["rooms"] = [{**s["rooms"][0], "room": f"example-{n}", "since": NOW - 60 * n} for n in range(1, 5)]
    s["workers"] = [{"id": f"w{n}", "room": "example-1", "account": "one", "started": NOW - 60, "cap": 900, "waiting": False,
                     "queued": None, "gone": False} for n in range(2)]
    lines = card(s)
    i = lines.index("🔨 example-1: turn 1 min")
    assert lines[i:i + 4] == ["🔨 example-1: turn 1 min", "🔨 example-2: turn 2 min", "🔨 example-3: turn 3 min", "🔨 and 3 more"]
    assert lines[i + 4].startswith("⏱")


def test_a_gone_worker_and_a_blocked_turn_wear_the_red_mark():
    s = room_input()
    s["rooms"] += [{**s["rooms"][0], "room": "example-quiet-room-with-a-long-name", "since": NOW - 900}]
    s["workers"] = [{"id": "def456", "room": "example-room", "account": "one", "started": NOW - 60, "cap": 900,
                     "waiting": False, "queued": None, "gone": True},
                    {"id": "aaa111", "room": "example-room", "account": "one", "started": None, "cap": 900, "waiting": True,
                     "queued": NOW - 30, "gone": False}]
    lines = card(s)
    assert f"{words.NOT_DONE} example-quiet-room-wi...: turn 15 min" in lines      # cut as words.named cuts a name
    assert f"{words.NOT_DONE} worker def456: gone, no result" in lines and "🔨 worker aaa111: waiting, 1st in line" in lines


def test_trouble_gets_a_red_line_and_all_well_gets_none():
    assert not any(x.startswith(words.NOT_DONE) for x in card(room_input()))
    lines = card(room_input(stalls=[{"seconds": 3.2}], backup=None, jobs=None))
    assert lines[-3:] == [f"{words.NOT_DONE} stalls today: 1, longest 3.2 s", f"{words.NOT_DONE} backup: none yet",
                          f"{words.NOT_DONE} jobs: the timer list is unreadable"]
    assert card(room_input(backup={"at": NOW - 3 * D, "bytes": 123456789}))[-1] == f"{words.NOT_DONE} backup: 72 h 0 min ago, 117.7 MB"


def test_a_capped_install_has_no_spend_line_and_a_day_with_no_message_yet_no_speed_line():
    s = room_input()
    del s["spend"]
    assert not any(x.startswith("💵") for x in card(s))
    assert not any(x.startswith("⏱") for x in card(room_input(speed={"first": None, "turn": None})))
    assert card(room_input(spend={"n": 9, "usd": 12345.67, "week": 98765.43}))[-1] == "💵 today US$12345.67 · week US$98765.43"


def test_one_account_has_no_green_mark_and_no_next_line():
    s = room_input(head={"account": "one"})
    s["accounts"] = [a for a in s["accounts"] if a["name"] == "one"]
    lines = card(s)
    assert lines[4:8] == ["⛽ Tank", "➤ ① wk 62% · Fable ⚠️100%", "↻ wk Fri 11p · 💳 Dec 8", ""]
    assert not any(words.DONE in x or "next" in x for x in lines)


def test_every_line_of_every_case_fits_a_phone_and_none_is_a_dash_or_carries_an_em_dash():
    SHOWN.clear()
    for name, case in list(globals().items()):              # every case above, run again here so this test stands alone
        if name.startswith("test_") and case is not test_every_line_of_every_case_fits_a_phone_and_none_is_a_dash_or_carries_an_em_dash \
                and not case.__code__.co_argcount:
            case()
    long = room_input(head={"model": "claude-sonnet-5-5", "account": "four", "context": 999999, "compactions": 12345})
    long["rooms"][0].update(since=NOW - 4000, waiting=12, fast=True)
    card(long)
    shown = [x for lines in SHOWN for x in lines]
    assert len(SHOWN) > 20 and shown
    assert all(words.width(x) <= words.PHONE and not x.startswith("- ") and "\N{EM DASH}" not in x for x in shown), [
        x for x in shown if words.width(x) > words.PHONE]
