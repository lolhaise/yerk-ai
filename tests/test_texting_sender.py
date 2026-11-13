"""Checks texting's sender and its hands, the step that passes a text to the Mac: a text that may go waits a time drawn for it, is
checked against the thread read again at that moment, and is handed once, dry as a job file, live as one job on ssh's stdin into
the Mac runner's outbox. Built in run 6. Nothing here reaches a Mac: the thread is the tests' own, and "ssh" is a fake command
test's tmp folder and prints `queued:<id>`. Time is the tests' own clock, never a sleep."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from test_texting_link import A1, B1
from test_texting_holds import Card
from yerk_ai.ledger import Ledger
from yerk_ai.texting import hands, holds, intake, sender, writer
from yerk_ai.texting.bridge import BridgeDown
from yerk_ai.texting.intake import digest
from yerk_ai.texting.people import load

LIST = (f'[owner]\nname = "Sam"\n\n[people.alex]\nfirst = "Alex"\nhandles = ["{A1}"]\nsheet = "people/alex.toml"\n\n'
        f'[people.beta]\nfirst = "Beta"\nhandles = ["{B1}"]\nsheet = "people/beta.toml"\n')
SHEET = ('open = []\nnight = "{night}"\n\n[pacing]\nweekday_day = [2.0, 4.0]\nweekday_evening = [2.0, 4.0]\nweekend_day = [2.0, 4.0]\n'
         'weekend_evening = [2.0, 4.0]\nactive = [0.5, 1.0]\nburst_gap = [10, 35]\n\n[voice]\ncard = "short, lower case"\n')
FAKE = """import pathlib, re, sys
box, kind, cmd = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[-1]
name = re.search(r"echo queued:(\\S+)", cmd).group(1)
with open(box.parent / "calls.log", "a") as f:
    f.write(name + "\\n")
data = sys.stdin.read()
if kind == "nolink":
    sys.stderr.write("ssh: connect to host mac port 22: Connection refused\\n")
    sys.exit(255)
(box / (name + ".json")).write_text(data)
print("queued:" + name if kind == "ok" else "Connection to mac closed.")
"""


def people(tmp: Path, night: str = "hold") -> Path:
    priv = tmp / "private" / "texting"
    (priv / "people").mkdir(parents=True, exist_ok=True)
    for f, text in ((priv / "people.toml", LIST), (priv / "people" / "alex.toml", SHEET.replace("{night}", night)),
                    (priv / "people" / "beta.toml", SHEET.replace("{night}", "hold"))):
        f.write_text(text)
        os.chmod(f, 0o600)
    return priv / "people.toml"


def fake(tmp: Path, kind: str = "ok") -> list[str]:
    """The tests' "ssh": writes its stdin as <tmp>/outbox/<id>.json and answers as the Mac would (`kind` ok), or cannot
    connect (`nolink`), or connects and answers something else (`odd`); every call is a line of <tmp>/calls.log."""
    (box := tmp / "outbox").mkdir(exist_ok=True)
    (script := tmp / "fake_ssh.py").write_text(FAKE)
    return [sys.executable, str(script), str(box), kind]


def calls(tmp: Path) -> list[str]:
    return (tmp / "calls.log").read_text().split() if (tmp / "calls.log").exists() else []


def reply(*texts: str, handle: str = A1, reply_to: str = "") -> dict[str, Any]:
    return {"handle": handle, "decision": "reply", "parts": json.dumps(list(texts)), "react": "", "reply_to": reply_to}


def no_spawn(monkeypatch: Any) -> None:
    """Every way to start a process fails the test."""
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError(f"a process was started: {a[:2]}")
    for mod, name in ((asyncio, "create_subprocess_exec"), (asyncio, "create_subprocess_shell"), (subprocess, "Popen"),
                      (subprocess, "run"), (os, "system"), (os, "execv"), (os, "posix_spawn")):
        monkeypatch.setattr(mod, name, boom)


def test_a_jobs_queued_and_since_are_the_newest_answered_text_plus_one_second_and_parts_two_and_three_burst() -> None:
    jobs = [hands.job(reply("on my way", "ten min", "save me a seat", reply_to="where are you"), k, 1700000000.25) for k in (1, 2, 3)]
    assert all(j["to"] == A1 and j["delay"] == 0 and j["queued"] == j["since"] == 1700000001.25 for j in jobs)
    assert "burst" not in jobs[0] and jobs[0]["reply_to"] == "where are you" and jobs[0]["text"] == "on my way"
    assert jobs[1]["burst"] is jobs[2]["burst"] is True and "reply_to" not in jobs[1] and jobs[2]["text"] == "save me a seat"
    assert "reply_to" not in hands.job(reply("ok"), 1, 5.0)
    tap = {**reply(), "decision": "react", "react": "love", "parts": "[]"}
    assert hands.job(tap, 1, 5.0) == {"to": A1, "delay": 0, "queued": 6.0, "since": 6.0, "react": "love"} and hands.parts(tap) == 1


def test_dry_hands_write_the_job_file_and_never_start_a_process(tmp_path: Path, monkeypatch: Any) -> None:
    file = people(tmp_path)
    no_spawn(monkeypatch)
    job = hands.job(reply("on my way"), 1, 100.0)
    assert asyncio.run(hands.Dry(file, tmp_path / "state").hand("yerk-7-1", job)) == hands.PUT
    put = tmp_path / "state" / "texting" / "dry" / "yerk-7-1.json"
    assert json.loads(put.read_text()) == job and put.stat().st_mode & 0o777 == 0o600


def test_live_writes_the_job_on_stdin_and_reads_what_the_mac_answered(tmp_path: Path) -> None:
    file, job = people(tmp_path), hands.job(reply("on my way"), 1, 100.0)
    assert asyncio.run(hands.Live(file, "/Users/someone/outbox", fake(tmp_path)).hand("yerk-7-1", job)) == hands.PUT
    assert json.loads((tmp_path / "outbox" / "yerk-7-1.json").read_text()) == job
    assert asyncio.run(hands.Live(file, "/Users/someone/outbox", fake(tmp_path, "nolink")).hand("yerk-8-1", job)) == hands.DOWN
    assert asyncio.run(hands.Live(file, "/Users/someone/outbox", fake(tmp_path, "odd")).hand("yerk-9-1", job)) == hands.MAYBE
    assert calls(tmp_path) == ["yerk-7-1", "yerk-8-1", "yerk-9-1"]


def test_a_to_not_on_the_reply_list_or_an_outbox_that_is_not_a_whole_path_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    file = people(tmp_path)
    stranger = hands.job(reply("on my way", handle="+15550000199"), 1, 100.0)
    for hand in (hands.Live(file, "/Users/someone/outbox", fake(tmp_path)), hands.Dry(file, tmp_path / "state")):
        assert asyncio.run(hand.hand("yerk-7-1", stranger)) == hands.REFUSED
    ours = hands.job(reply("on my way"), 1, 100.0)
    for box in ("outbox", "/Users/someone/out box", "/Users/x;touch y"):
        assert asyncio.run(hands.Live(file, box, fake(tmp_path)).hand("yerk-7-1", ours)) == hands.REFUSED
    assert calls(tmp_path) == [] and not list((tmp_path / "outbox").iterdir()) and not (tmp_path / "state").exists()


def at(month: int, day: int, hour: int, minute: int, second: int = 0) -> float:
    return datetime(2026, month, day, hour, minute, second).timestamp()


WED = at(11, 11, 14, 0)          # a Wednesday afternoon, local time


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Thread:
    """The person's thread as the link reads it at that moment: rows the test adds; `down`, the bridge away."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.down, self.reads = False, 0

    def add(self, guid: str, t: float, text: str = "see you at six", mine: bool = False, tapback: bool = False) -> None:
        self.rows.append({"guid": guid, "is_from_me": mine, "text": text, "is_reaction": tapback, "chat_identifier": A1,
                          "created_at": datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")})

    async def __call__(self, key: str, limit: int) -> list[dict[str, Any]]:
        self.reads += 1
        if self.down:
            raise BridgeDown("the bridge is away")
        return list(self.rows)


def make(tmp: Path, mode: str = "live", night: str = "hold", t: float = WED, kind: str = "ok", posted: list[str] | None = None,
         **conf: Any) -> tuple[sender.Sender, Ledger, Clock]:
    file, clock = people(tmp, night), Clock(t)
    led, thread = Ledger(tmp / "ledger.sqlite"), Thread()
    hand: Any = hands.Live(file, "/Users/someone/outbox", fake(tmp, kind)) if mode == "live" else hands.Dry(file, tmp / "state")

    async def post(room: str, line: str) -> None:
        (posted if posted is not None else []).append(f"{room}: {line}")
    s = sender.Sender({"mode": mode, **conf}, file, led, thread, hand, post=post, to="owner-room", clock=clock)
    s.thread_rows = thread                                # type: ignore[attr-defined]
    return s, led, clock


def rows(s: sender.Sender) -> Thread:
    return s.thread_rows                                  # type: ignore[attr-defined,no-any-return]


def their(led: Ledger, guid: str, created: float, person: str = "alex", text: str = "see you at six") -> None:
    led.inbound_add(guid, "texting", f"imessage:{person}", person, 5, created=created)
    led.inbound_set(guid, digest=digest(text))


def his(led: Ledger, guid: str, created: float, side: str = "owner") -> None:
    led.inbound_add(guid, "texting", "imessage:alex", side, 5, created=created)


def waiting(led: Ledger, *guids: str, state: str = "queued", texts: tuple[str, ...] = ("on my way",), word: bool = False,
            decision: str = "reply", react: str = "", person: str = "alex") -> int:
    return led.texting_add(person=person, handle=A1 if person == "alex" else B1, inbound=",".join(guids), decision=decision,
                           parts=json.dumps(list(texts) if decision == "reply" else []), react=react, reply_to="", statements="[]",
                           state=state, reason="", mode="live" if state == "queued" else "dry", approval_id=7 if word else None)


def armed(s: sender.Sender, led: Ledger, rid: int) -> dict[str, Any]:
    asyncio.run(s.tick())
    return led.texting_get(rid) or {}


def test_his_first_shot_draws_150_to_240_s_when_his_own_text_is_newest_and_young_and_not_after_an_auto_row(tmp_path: Path) -> None:
    s, led, _ = make(tmp_path)
    his(led, "m-1", WED - 100)
    for n in range(8):
        their(led, f"t-{n}", WED - 5)
        r = armed(s, led, rid := waiting(led, f"t-{n}"))
        assert 150 <= r["delay"] <= 240 and r["fire_at"] == pytest.approx(WED - 5 + r["delay"])
        led.texting_set(rid, state="sent")
    his(led, "m-2", WED - 50, side="auto")                # texting's own send is the owner's side's newest: no first shot
    their(led, "t-9", WED - 5)
    r = armed(s, led, waiting(led, "t-9"))
    assert 30 <= r["delay"] <= 60                         # the sheet's active bucket, 0.5 to 1 minute


def test_fire_at_is_the_later_of_now_and_their_texts_own_time_plus_the_draw(tmp_path: Path) -> None:
    s, led, _ = make(tmp_path)
    their(led, "t-old", WED - 1000)
    assert armed(s, led, rid := waiting(led, "t-old"))["fire_at"] == WED
    led.texting_set(rid, state="sent")
    their(led, "t-new", WED - 10)
    r = armed(s, led, waiting(led, "t-new"))
    assert 120 <= r["delay"] <= 240 and r["fire_at"] == pytest.approx(WED - 10 + r["delay"])


def test_quiet_hours_hold_a_night_text_to_a_morning_draw_unless_the_sheet_answers_and_his_word_goes_at_once(tmp_path: Path) -> None:
    for night, when in (("hold", at(11, 11, 23, 45)), ("hold", at(11, 12, 3, 0)), ("answer", at(11, 11, 23, 45)),
                        ("answer", at(11, 12, 3, 0)), ("word", at(11, 11, 23, 45))):
        tmp = tmp_path / f"{night}-{int(when)}"
        s, led, _ = make(tmp, night="hold" if night == "word" else night, t=when)
        their(led, "t-1", when - 30)
        r = armed(s, led, waiting(led, "t-1", word=night == "word"))
        if night == "hold":
            assert at(11, 12, 7, 40) <= r["fire_at"] <= at(11, 12, 9, 0), datetime.fromtimestamp(r["fire_at"])
        elif night == "answer":
            assert 120 <= r["fire_at"] - (when - 30) <= 240
        else:
            assert when <= r["fire_at"] <= when + 30


def test_a_restart_keeps_the_stored_fire_time_and_draws_nothing_new(tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    their(led, "t-1", WED - 5)
    first = armed(s, led, rid := waiting(led, "t-1"))

    class Never(sender.random.Random):
        def uniform(self, a: float, b: float) -> float:
            raise AssertionError("drew again")
    again = sender.Sender({"mode": "live"}, s.file, led, s.thread, s.hands, clock=clock, rand=Never())
    assert armed(again, led, rid) == first and calls(tmp_path) == []
    rows(s).add("t-1", WED - 5)
    clock.t = first["fire_at"]
    assert armed(again, led, rid)["handed"] == first["fire_at"] and calls(tmp_path) == [f"yerk-{rid}-1"]


def due(s: sender.Sender, led: Ledger, clock: Clock, *guids: str, word: bool = False, state: str = "queued",
        texts: tuple[str, ...] = ("on my way",)) -> int:
    """A waiting row armed, its texts on the thread, and the clock at its fire time."""
    for n, g in enumerate(guids):
        their(led, g, WED - 30 + n)
        rows(s).add(g, WED - 30 + n)
    rid = waiting(led, *guids, word=word, state=state, texts=texts)
    clock.t = armed(s, led, rid)["fire_at"]
    return rid


def test_a_text_of_theirs_before_the_fire_time_stops_it_and_a_card_his_word_released_says_they_wrote_again(tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    shows = Card()
    h = holds.Holds({"mode": "live"}, s.file, tmp_path / "state", led, "owner-room", Thread(), shows.show)
    s.edits = h.edits
    rid = due(s, led, clock, "t-1", word=True)
    led.texting_set(rid, card="100", shown="📱 Alex · 13:59 · sending\n> see you at six\ndraft:\non my way\nwhy: checked")
    rows(s).add("t-2", WED - 2, "wait, make it seven")
    assert armed(s, led, rid)["state"] == "cancelled" and led.texting_get(rid)["reason"] == "they wrote again"
    asyncio.run(h.flush())
    assert calls(tmp_path) == [] and shows.posts == []
    assert shows.edits[-1][2].split("\n")[0] == "📱 Alex · 13:59 · not sent: they wrote again"


def test_at_fire_time_a_changed_text_is_decided_again_a_gone_one_cancels_and_an_away_bridge_waits_then_drops_once(tmp_path: Path) -> None:
    posted: list[str] = []
    s, led, clock = make(tmp_path, posted=posted)
    rid = due(s, led, clock, "t-1")
    rows(s).rows[0]["text"] = "see you at seven"
    r = armed(s, led, rid)
    assert (r["state"], r["fire_at"], r["delay"], r["handed"]) == ("deciding", None, None, None)
    assert led.inbound_get("t-1")["digest"] == digest("see you at seven")
    rid = due(s, led, clock, "t-2")
    rows(s).rows = [x for x in rows(s).rows if x["guid"] != "t-2"]
    assert armed(s, led, rid)["state"] == "cancelled" and led.texting_get(rid)["reason"] == "they took it back"
    rid = due(s, led, clock, "t-3")
    rows(s).down = True
    assert armed(s, led, rid)["state"] == "queued" and calls(tmp_path) == []
    clock.t += 30 * 60 + 1
    assert armed(s, led, rid)["state"] == "dropped" and armed(s, led, rid)["state"] == "dropped"
    assert calls(tmp_path) == [] and len(posted) == 1 and posted[0].startswith("owner-room: not sent: a text to Alex, due ")


def test_an_ssh_that_connected_and_answered_otherwise_is_never_tried_again_and_one_that_could_not_connect_is(tmp_path: Path) -> None:
    for kind, tries in (("odd", 1), ("nolink", 3)):
        s, led, clock = make(tmp_path / kind, kind=kind)
        rid = due(s, led, clock, "t-1")
        for _ in range(3):
            armed(s, led, rid)
            clock.t += 5
        assert len(calls(tmp_path / kind)) == tries
        assert (led.texting_get(rid)["handed"] is None) is (kind == "nolink")


def test_a_whole_dry_pass_writes_the_jobs_live_would_hand_and_never_starts_a_process(tmp_path: Path, monkeypatch: Any) -> None:
    s, led, clock = make(tmp_path, mode="dry")
    no_spawn(monkeypatch)
    rid = due(s, led, clock, "t-1", "t-2", state="would-send", texts=("on my way", "ten min"))
    r = armed(s, led, rid)
    assert r["state"] == "would-send" and r["handed"] == clock.t and rows(s).reads == 1
    for k in (1, 2):
        got = json.loads((tmp_path / "state" / "texting" / "dry" / f"yerk-{rid}-{k}.json").read_text())
        assert got == hands.job(r, k, WED - 29) and got["queued"] == WED - 28


def test_a_job_handed_and_never_claimed_is_unseen_after_the_window_and_never_written_again(tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    rid = due(s, led, clock, "t-1")
    assert armed(s, led, rid)["handed"] == clock.t
    for _ in range(5):
        clock.t += 60
        armed(s, led, rid)
    assert led.texting_get(rid)["state"] == "unseen" and calls(tmp_path) == [f"yerk-{rid}-1"]


def intake_for(s: sender.Sender, led: Ledger) -> tuple[intake.Intake, list[Any]]:
    took, withdrawn = intake.Intake({"mode": "live"}, load(s.file), led, lambda *a, **k: True, clock=s.clock), []
    took.claim, took.withdraw = s.claim, lambda p, at: withdrawn.append(at)
    return took, withdrawn


def test_a_from_me_row_in_the_window_claims_the_oldest_text_job_as_auto_and_answers_nothing(tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    took, withdrawn = intake_for(s, led)
    alex = load(s.file).people["alex"]
    their(led, "t-0", WED - 60)
    pending = waiting(led, "t-0", state="deciding")
    rid = due(s, led, clock, "t-1")
    handed = armed(s, led, rid)["handed"]
    rows(s).add("m-tap", handed + 5, "", mine=True, tapback=True)
    assert took.take(alex, rows(s).rows[-1]) == "tapback" and led.texting_get(rid)["claimed"] is None
    rows(s).add("m-1", handed + 20, "on my way", mine=True)
    assert took.take(alex, rows(s).rows[-1]) == "auto"
    assert (led.texting_get(rid)["state"], led.texting_get(rid)["claimed"]) == ("sent", "m-1")
    assert led.inbound_get("m-1")["sender"] == "auto" and led.texting_get(pending)["state"] == "deciding" and withdrawn == []
    assert " auto: on my way" in writer.line(rows(s).rows[-1], auto={"m-1"})
    rows(s).add("m-2", handed + 400, "running late", mine=True)
    assert took.take(alex, rows(s).rows[-1]) == "owner" and led.inbound_get("m-2")["sender"] == "owner"
    assert led.texting_get(pending)["state"] == "cancelled" and withdrawn == [pytest.approx(handed + 400, abs=1e-3)]


def test_part_two_waits_for_part_one_to_be_seen_goes_a_burst_gap_later_and_never_after_an_unseen_part_one(tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    took, _ = intake_for(s, led)
    alex = load(s.file).people["alex"]
    rid = due(s, led, clock, "t-1", texts=("on my way", "ten min"))
    handed = armed(s, led, rid)["handed"]
    for _ in range(3):
        clock.t += 20
        armed(s, led, rid)
    assert calls(tmp_path) == [f"yerk-{rid}-1"]
    rows(s).add("m-1", handed + 30, "on my way", mine=True)
    assert took.take(alex, rows(s).rows[-1]) == "auto"
    r = led.texting_get(rid)
    assert r["state"] == "queued" and r["handed"] is None and 10 <= r["fire_at"] - clock.t <= 35
    clock.t = r["fire_at"]
    armed(s, led, rid)
    assert calls(tmp_path) == [f"yerk-{rid}-1", f"yerk-{rid}-2"]          # its recheck passed over part 1's own row
    part2 = json.loads((tmp_path / "outbox" / f"yerk-{rid}-2.json").read_text())
    assert part2["burst"] is True and part2["text"] == "ten min" and part2["queued"] == WED - 29
    rows(s).add("m-2", clock.t + 8, "ten min", mine=True)
    assert took.take(alex, rows(s).rows[-1]) == "auto" and led.texting_get(rid)["state"] == "sent"
    late = due(s, led, clock, "t-2", texts=("on my way", "ten min"))
    armed(s, led, late)
    for _ in range(6):
        clock.t += 60
        armed(s, led, late)
    assert led.texting_get(late)["state"] == "unseen" and calls(tmp_path)[2:] == [f"yerk-{late}-1"]
