"""Checks the card a room posts when you type /status there (status.room_card): the machine card's shape, three lines
about the room it was typed in, each account's usage left at two lines each, what runs, today's speed and spend, and
(status.card) keeps its own text: the operator's machine card."""
from __future__ import annotations

import calendar
import time

import pytest
from test_idle_compact import FAKE, ROOM, Chan, say, wait_idle
from test_observe import Timers

from yerk_ai import status
from yerk_ai.accounts import Account
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd

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


def test_a_session_counts_its_compactions_from_its_start_and_a_new_session_starts_again_at_0(tmp_path, monkeypatch):
    y = daemon(tmp_path, monkeypatch)
    room = y.add_room(ROOM, Chan())
    assert room.compactions is None                          # no session started yet: nothing known
    say(y, room, "1", "CONTEXT:200000 hello")
    wait_idle(room)
    assert room.compactions == 0                             # a newly minted session
    room.compact_now()                                       # a held /compact (the reaper's, yerk room compact)
    wait_idle(room)
    assert room.compactions == 1
    say(y, room, "2", "AUTO_COMPACT and on")                 # Claude Code's own, inside a turn
    wait_idle(room)
    assert room.compactions == 2
    say(y, room, "3a", "plain")                              # a turn that compacts nothing (and a recap that quotes no trigger)
    wait_idle(room)
    assert room.compactions == 2
    room.renew()                                             # /new: the next run starts a fresh session
    say(y, room, "3", "fresh")
    wait_idle(room)
    assert room.compactions == 0
    room.rp.close()
    again = daemon(tmp_path, monkeypatch).add_room(ROOM, Chan())      # a daemon start: the session goes on, resumed
    say(y, again, "4", "back")
    wait_idle(again)
    assert again.compactions is None                         # its earlier compactions were never seen: not known
    again.rp.close()


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
