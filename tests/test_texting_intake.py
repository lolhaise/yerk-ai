"""Checks the intake of texts sent to you: each written down once, a burst made one decision, catch-up after a restart, no person's room reading them."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from yerk_ai import memory
from yerk_ai.ledger import Ledger
from yerk_ai.texting.intake import Intake
from yerk_ai.texting.link import Link
from test_texting_link import A1, FakeBridge, listed, stamp, until

HOURS7 = 7 * 3600


@pytest.fixture
def sock():
    d = tempfile.mkdtemp(prefix="yt")             # under TMPDIR, short: a socket path has a length limit
    yield str(Path(d) / "b.sock")
    shutil.rmtree(d, ignore_errors=True)


class Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t


def intake(tmp_path: Path, led: Ledger | None = None, clock: Clock | None = None, **conf: Any) -> tuple[Intake, Ledger, Clock]:
    led = led or Ledger(tmp_path / "ledger.sqlite")
    clock = clock or Clock()
    daily = memory.DailyLog(tmp_path / "daily", timezone.utc)
    return Intake({"mode": "dry", **conf}, listed(tmp_path), led, daily.capture, clock=clock), led, clock


def row(guid: str, text: str, from_me: bool = False, ago: float = 0.0, **extra: Any) -> dict[str, Any]:
    return {"id": 1, "guid": guid, "chat_id": 1, "chat_identifier": A1, "is_from_me": from_me, "text": text,
            "created_at": stamp(ago), "attachments": [], **extra}


def states(led: Ledger) -> dict[str, str]:
    return dict(led.db.execute("SELECT message_id, state FROM inbound").fetchall())


def decisions(led: Ledger) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in led.db.execute("SELECT person, inbound, state, mode FROM texting ORDER BY id")]


def test_texting_row_links_the_texts_it_answers(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    rid = led.texting_add(person="alex", handle="+15550000101", inbound="G-1,G-2", state="deciding", mode="dry")
    assert decisions(led) == [("alex", "G-1,G-2", "deciding", "dry")]
    links = led.db.execute("SELECT message_id FROM message_links WHERE tbl = 'texting' ORDER BY pos").fetchall()
    assert [r[0] for r in links] == ["G-1", "G-2"]
    led.texting_set(rid, state="cancelled", reason="owner answered")
    assert [r["state"] for r in led.texting_rows("alex", "cancelled")] == ["cancelled"]


def test_a_row_twice_is_one_inbound_row(tmp_path):
    it, led, _ = intake(tmp_path)
    alex = it.people.people["alex"]
    r = row("G-1", "hey", attachments=[{"transfer_name": "IMG_1.heic", "mime_type": "image/heic", "total_bytes": 2048}])
    assert it.take(alex, r) == "them"
    assert it.take(alex, r) == "seen"
    assert led.db.execute("SELECT room, channel, sender FROM inbound").fetchall() == [("texting", "imessage:alex", "alex")]
    assert led.db.execute("SELECT name, type, size, state FROM attachments").fetchall() == [
        ("IMG_1.heic", "image/heic", 2048, "not fetched")]
    day = next((tmp_path / "daily").glob("*.md")).read_text()
    assert day.count("[m:G-1]") == 1 and " texting person@alex: hey [c:private] [m:G-1]" in day
    assert "  - attachment: IMG_1.heic, image/heic, 2,048 bytes" in day


def test_three_texts_in_ten_seconds_are_one_burst(tmp_path):
    it, led, clock = intake(tmp_path)
    alex = it.people.people["alex"]
    for n in range(3):
        assert it.take(alex, row(f"G-{n}", f"text {n}")) == "them"
        clock.t += 5
    assert it.tick() == [] and decisions(led) == []                # 5 s after the last: still open
    clock.t += 15
    assert it.tick() == []
    assert decisions(led) == [("alex", "G-0,G-1,G-2", "deciding", "dry")]
    assert set(states(led).values()) == {"pending"}
    for n in range(10):                                             # one every 15 s: closed at the cap, 120 s on
        it.take(alex, row(f"H-{n}", "more"))
        clock.t += 15
        it.tick()
    clock.t += 20
    it.tick()
    assert [d[1].count(",") + 1 for d in decisions(led)[1:]] == [8, 2]


def test_a_text_followed_by_the_owners_own_is_answered(tmp_path):
    it, led, clock = intake(tmp_path)
    alex = it.people.people["alex"]
    it.take(alex, row("G-1", "you around?", ago=30))
    assert it.take(alex, row("G-2", "yes", from_me=True, ago=10)) == "owner"
    clock.t += 60
    assert it.tick() == [] and decisions(led) == []
    assert states(led) == {"G-1": "answered", "G-2": "filed"}
    assert led.inbound_get("G-2")["sender"] == "owner"
    it.take(alex, row("G-3", "cool", ago=5))                        # a pending decision, then the owner answers it
    clock.t += 30
    it.tick()
    it.take(alex, row("G-4", "see you", from_me=True))
    assert decisions(led) == [("alex", "G-3", "cancelled", "dry")] and states(led)["G-3"] == "answered"


@pytest.mark.parametrize("conf, to", [({"mode": "dry", "report": "owner-room", "dry_report": "test-room"}, "test-room"),
                                      ({"mode": "dry", "report": "owner-room"}, None),
                                      ({"mode": "live", "report": "owner-room", "dry_report": "test-room"}, "owner-room")])
def test_a_text_older_than_catch_up_is_missed_and_listed_once_never_in_report_when_dry(tmp_path, conf, to):
    it, led, clock = intake(tmp_path, **conf)
    alex = it.people.people["alex"]
    it.take(alex, row("G-1", "late one", ago=HOURS7))
    it.take(alex, row("G-2", "and another", ago=HOURS7 - 60))
    clock.t += 30
    said = it.tick()
    assert [r for r, _ in said] == ([to] if to else [])
    assert all(line.startswith("missed: 2 texts from Alex since ") for _, line in said)
    assert decisions(led) == [("alex", "G-1,G-2", "missed", conf["mode"])] and set(states(led).values()) == {"missed"}
    assert it.take(alex, row("G-1", "late one", ago=HOURS7)) == "seen"
    clock.t += 30
    assert it.tick() == []


def test_a_restart_replays_from_the_cursor_with_no_row_twice(sock, tmp_path):
    async def go() -> None:
        fb = FakeBridge(sock)
        fb.chat(1, A1)
        fb.add(1, "before", ago=120)
        await fb.start()
        it, led, clock = intake(tmp_path, report="owner-room", dry_report="test-room")
        link = Link(it.people, tmp_path, it.take, unix=sock)
        task = asyncio.create_task(link.start())
        await until(lambda: link.connected)
        first = fb.add(1, "first")
        fb.push(first)
        await until(lambda: len(states(led)) == 1)
        task.cancel()                                               # a stop with the burst still open
        await asyncio.gather(task, return_exceptions=True)
        fb.add(1, "while asleep", ago=HOURS7)
        fb.add(1, "now")
        it, _, _ = intake(tmp_path, led, clock, report="owner-room", dry_report="test-room")
        it.recover()
        link = Link(it.people, tmp_path, it.take, unix=sock)
        task = asyncio.create_task(link.start())
        await until(lambda: len(states(led)) == 3)
        fb.push(first)                                              # the same row again
        await asyncio.sleep(0.1)
        clock.t += 30
        said = it.tick()
        assert led.db.execute("SELECT COUNT(*) FROM inbound").fetchone()[0] == 3
        assert decisions(led) == [("alex", "G-103", "missed", "dry"), ("alex", "G-102,G-104", "deciding", "dry")]
        assert [r for r, _ in said] == ["test-room"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fb.stop()
    asyncio.run(go())


def test_no_person_room_reads_a_texting_line(tmp_path):
    it, _, _ = intake(tmp_path)
    it.take(it.people.people["alex"], row("G-1", "a private text"))
    daily = tmp_path / "daily"
    f = next(daily.glob("*.md"))
    text = f.read_text()
    for reader in (memory.Reader("family", "family-id"), memory.Reader("family", "family-id", sealed=True),
                   memory.Reader("gym", "gym-id", owner=True, sealed=True)):
        assert memory.allowed_text(reader, f, text, daily=daily) is None
    assert "a private text" in (memory.allowed_text(memory.Reader("main", "main-id", owner=True), f, text, daily=daily) or "")
