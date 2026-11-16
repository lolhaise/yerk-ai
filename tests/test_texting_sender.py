"""Checks texting's sender and its hands, the step that passes a text to the Mac: a text that may go waits a time drawn for it, is
checked against the thread read again at that moment, and is handed once, dry as a job file, live as one job on ssh's stdin into
the Mac runner's outbox. Built in run 6. Nothing here reaches a Mac: the thread is the tests' own, and "ssh" is a fake command
test's tmp folder and prints `queued:<id>`. Time is the tests' own clock, never a sleep."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from test_texting_link import A1, B1, FakeBridge, listed, until
from test_texting_holds import Card
from yerk_ai import texts, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.texting import hands, holds, intake, sender, writer
from yerk_ai.texting.bridge import BridgeDown
from yerk_ai.texting.intake import digest
from yerk_ai.texting.link import Link
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
if kind in ("nolink", "cap", "closed"):
    sys.stderr.write({"nolink": "ssh: connect to host mac port 22: Connection refused", "closed": "Connection closed by mac port 22",
                      "cap": "kex_exchange_identification: read: Connection reset by peer"}[kind] + "\\n")
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
    connect (`nolink`), is hung up on before its greeting (`cap`), is closed with no such sign (`closed`), or connects and
    answers something else (`odd`); every call is a line of <tmp>/calls.log."""
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


def test_an_ssh_hung_up_on_before_its_greeting_wrote_nothing_so_it_is_down_and_a_close_with_no_such_sign_may_have_gone(tmp_path: Path) -> None:
    """A Mac at its ssh session cap hangs up before the greeting: no command ran, so the next pass may try again. Any other
    close may have written the job: a text lost can be sent again by the owner, one sent twice cannot be taken back."""
    file, job = people(tmp_path), hands.job(reply("on my way"), 1, 100.0)
    assert asyncio.run(hands.Live(file, "/Users/someone/outbox", fake(tmp_path, "cap")).hand("yerk-7-1", job)) == hands.DOWN
    assert asyncio.run(hands.Live(file, "/Users/someone/outbox", fake(tmp_path, "closed")).hand("yerk-8-1", job)) == hands.MAYBE
    assert calls(tmp_path) == ["yerk-7-1", "yerk-8-1"] and not list((tmp_path / "outbox").iterdir())


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

    def add(self, guid: str, t: float, text: str = "see you at six", mine: bool = False) -> None:
        self.rows.append({"guid": guid, "is_from_me": mine, "text": text, "chat_identifier": A1, "reactions": [],
                          "created_at": datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")})

    def tap(self, n: int, t: float, on: str = "t-1", mine: bool = True, add: bool = True) -> dict[str, Any]:
        """A tapback on row `on`, as the Mac's bridge has it (run9/probe/shapes.txt): in a history only an entry of that row's
        `reactions`, by its row id `n` and no guid; returned, the watch's row for it, which carries both."""
        at, row = datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z"), next((x for x in self.rows if x["guid"] == on), None)
        if add and row is not None:
            row["reactions"].append({"id": n, "is_from_me": mine, "type": "like", "emoji": "👍", "sender": A1, "created_at": at})
        return {"id": n, "guid": f"tap-{n}", "is_from_me": mine, "text": "Liked “see you at six”", "chat_identifier": A1, "created_at": at,
                "is_reaction": True, "is_reaction_add": add, "reaction_type": "like", "reaction_emoji": "👍", "reacted_to_guid": on}

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
    rid = due(s, led, clock, "t-2", "t-2b")
    rows(s).rows = [{**x, "text": "see you at nine"} if x["guid"] == "t-2" else x for x in rows(s).rows if x["guid"] != "t-2b"]
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
    rows(s).add("m-1", handed + 20, "on my way", mine=True)
    assert took.take(alex, rows(s).rows[-1]) == "auto"
    assert (led.texting_get(rid)["state"], led.texting_get(rid)["claimed"]) == ("sent", "m-1")
    assert led.inbound_get("m-1")["sender"] == "auto" and led.texting_get(pending)["state"] == "deciding" and withdrawn == []
    assert " auto: on my way" in writer.line(rows(s).rows[-1], auto={"m-1"})
    rows(s).add("m-2", handed + 400, "running late", mine=True)
    assert took.take(alex, rows(s).rows[-1]) == "owner" and led.inbound_get("m-2")["sender"] == "owner"
    assert led.texting_get(pending)["state"] == "cancelled" and withdrawn == [pytest.approx(handed + 400, abs=1e-3)]


def test_a_tapback_of_his_own_from_the_watch_that_claims_nothing_is_his_answer_and_one_he_takes_away_is_nothing(tmp_path: Path) -> None:
    s, led, _ = make(tmp_path)
    took, withdrawn = intake_for(s, led)
    took.stop, alex, rids = s.cancel, load(s.file).people["alex"], []
    for n, state in enumerate(("deciding", "decided", "release-ready", "queued")):
        their(led, f"t-{n}", WED - 60 + n)
        rows(s).add(f"t-{n}", WED - 60 + n)
        rids.append(waiting(led, f"t-{n}", state=state))
    assert took.take(alex, rows(s).tap(9000, WED - 10, on="t-3", add=False)) == "tapback" and withdrawn == []
    assert [led.texting_get(r)["state"] for r in rids] == ["deciding", "decided", "release-ready", "queued"]
    assert took.take(alex, rows(s).tap(9001, WED - 5, on="t-3")) == "owner" and withdrawn == [pytest.approx(WED - 5, abs=1e-3)]
    assert [(led.texting_get(r)["state"], led.texting_get(r)["reason"]) for r in rids] == [("cancelled", "owner answered")] * 4
    assert [(led.inbound_get(f"t-{n}")["state"], led.inbound_get(f"t-{n}")["note"]) for n in range(4)] == [
        ("answered", "the owner answered himself")] * 4
    assert (led.inbound_get("tap-9000")["note"], led.inbound_get("tap-9001")["note"]) == ("a tapback taken away", "the owner's own tapback")


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



def test_his_own_text_cancels_a_queued_text_and_one_handed_and_unclaimed_gets_exactly_one_cancel_job(tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    took, _ = intake_for(s, led)
    took.stop = s.cancel
    alex = load(s.file).people["alex"]
    rid = due(s, led, clock, "t-1")
    clock.t -= 20                                          # before its fire time
    rows(s).add("m-1", WED - 10, "be right there", mine=True)
    assert took.take(alex, rows(s).rows[-1]) == "owner"
    assert armed(s, led, rid)["state"] == "cancelled" and led.texting_get(rid)["reason"] == "owner answered"
    assert calls(tmp_path) == []
    tap = led.texting_add(person="alex", handle=A1, inbound="t-2", decision="react", parts="[]", react="love", reply_to="",
                          statements="[]", state="queued", reason="", mode="live")
    their(led, "t-2", clock.t - 5)
    rows(s).add("t-2", clock.t - 5)
    clock.t = armed(s, led, tap)["fire_at"]
    handed = armed(s, led, tap)["handed"]
    assert json.loads((tmp_path / "outbox" / f"yerk-{tap}-1.json").read_text())["react"] == "love"
    rows(s).add("m-2", handed + 3, "actually call me", mine=True)        # a text row never claims a tapback job: the owner's
    assert took.take(alex, rows(s).rows[-1]) == "owner" and led.texting_get(tap)["state"] == "cancelled"
    for _ in range(3):
        clock.t += 5
        armed(s, led, tap)
    assert calls(tmp_path) == [f"yerk-{tap}-1", f"yerk-{tap}-cancel"]
    sent = json.loads((tmp_path / "outbox" / f"yerk-{tap}-cancel.json").read_text())
    assert sent == {"to": A1, "cancel": True, "queued": handed + 5}


def test_a_newer_decision_for_the_person_cancels_the_older_text_not_yet_gone(tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    older = due(s, led, clock, "t-1")
    clock.t -= 20
    their(led, "t-2", clock.t - 2)
    rows(s).add("t-2", clock.t - 2)
    newer = waiting(led, "t-2")
    assert armed(s, led, newer)["fire_at"] is not None
    assert led.texting_get(older)["state"] == "cancelled" and led.texting_get(older)["reason"] == "a newer decision"
    assert calls(tmp_path) == []
    clock.t = led.texting_get(newer)["fire_at"]
    assert armed(s, led, newer)["handed"] == clock.t
    their(led, "t-3", clock.t + 1)
    rows(s).add("t-3", clock.t + 1)
    newest = waiting(led, "t-3")
    armed(s, led, newest)
    assert led.texting_get(newer)["state"] == "cancelled" and calls(tmp_path) == [f"yerk-{newer}-1", f"yerk-{newer}-cancel"]



def test_a_dry_start_drops_what_live_left_to_send_lists_it_once_in_the_card_room_and_hands_nothing(tmp_path: Path) -> None:
    posted: list[str] = []
    s, led, clock = make(tmp_path, mode="dry", posted=posted)
    their(led, "t-1", WED - 30)
    rows(s).add("t-1", WED - 30)
    left = waiting(led, "t-1")
    for _ in range(2):
        asyncio.run(s.start())
    for _ in range(3):
        clock.t += 600
        asyncio.run(s.tick())
    assert (led.texting_get(left)["state"], led.texting_get(left)["reason"]) == ("dropped", "switched off")
    assert posted == ["owner-room: " + words.TEXTING_SWITCHED.format(n=1, s="", mode="dry")]
    assert not (tmp_path / "state" / "texting" / "dry").exists() and calls(tmp_path) == []


def test_a_start_with_texting_off_drops_what_live_left_with_a_log_line_and_with_no_table_touches_nothing(tmp_path: Path, caplog: Any) -> None:
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "texting").mkdir()
    listed(private / "texting")
    for table, after in (("", "queued"), ('[texting]\nmode = "off"\n', "dropped")):
        (private / "yerkd.toml").write_text(table + '\n[imessage]\ncommand = ["true"]\n')
        (state := tmp_path / f"state-{after}").mkdir()
        y = Yerkd(private, state)
        rid = waiting(y.ledger, "t-1")
        with caplog.at_level(logging.WARNING):
            assert y.texting() == [] and y.ledger.texting_get(rid)["state"] == after
        assert ("left to send dropped" in caplog.text) == (after == "dropped")


def test_live_with_text_out_off_in_gates_drops_what_was_left_to_send_and_gives_the_hands_no_command(tmp_path: Path, caplog: Any) -> None:
    """[gates] off = ["text-out"] is the install's switch for texts: one that waited through the restart must not go after it."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "texting").mkdir()
    listed(private / "texting")
    for gate, head in (("", ["mac"]), ('\n[gates]\noff = ["text-out"]\n', [])):
        (private / "yerkd.toml").write_text('[texting]\nmode = "live"\nreport = "example-room"\noutbox = "/Users/someone/outbox"\n'
                                            '\n[imessage]\nhost = "mac"\nsocket = "/Users/someone/bridge.sock"\n' + gate)
        (state := tmp_path / f"state{len(head)}").mkdir()
        y = Yerkd(private, state)
        rid = waiting(y.ledger, "t-1")
        got = y.texting()
        send = got[-1].cr_frame.f_locals["self"]             # the sender, from its own task
        with caplog.at_level(logging.WARNING):
            asyncio.run(send.start())
        for c in got:
            c.close()
        assert send.hands.argv[-1:] == head and y.ledger.texting_get(rid)["state"] == ("queued" if head else "dropped")
        assert ("text-out is off" in caplog.text) == (not head)
    s, led, clock = make(tmp_path / "pass")                  # and a pass after such a start hands nothing
    s.out = False
    their(led, "t-1", WED - 30)
    rows(s).add("t-1", WED - 30)
    left = waiting(led, "t-1")
    asyncio.run(s.start())
    for _ in range(3):
        clock.t += 600
        asyncio.run(s.tick())
    assert led.texting_get(left)["state"] == "dropped" and calls(tmp_path / "pass") == []


def test_at_fire_time_a_text_or_a_tapback_of_his_own_that_only_the_thread_holds_stops_the_text(tmp_path: Path) -> None:
    """The watch can miss a row (the link away, a replay still to come), so the thread read at fire time is the guard that
    is left: the owner's own text after theirs stops the text, and so does a tapback of the owner's, an entry of a row's `reactions`."""
    for n, tapback in enumerate((False, True)):
        s, led, clock = make(tmp_path / str(n))
        rid = due(s, led, clock, "t-1")
        if tapback:                                                         # on the thread only: the intake never saw it
            rows(s).tap(9001, WED - 2)
        else:
            rows(s).add("m-1", WED - 2, "running late", mine=True)
        r = armed(s, led, rid)
        assert (r["state"], r["reason"], r["handed"]) == ("cancelled", "owner answered", None)
        assert calls(tmp_path / str(n)) == [] and led.inbound_get("t-1")["state"] == "answered"


def test_at_fire_time_a_tapback_of_theirs_or_texting_own_claimed_one_on_the_thread_does_not_stop_the_text(tmp_path: Path) -> None:
    for n, mine in enumerate((False, True)):
        s, led, clock = make(tmp_path / str(n))
        rid = due(s, led, clock, "t-1")
        tap = rows(s).tap(9001, WED - 2, mine=mine)
        if mine:                                                            # texting's own, seen late: claimed by its row id
            react = waiting(led, "t-0", state="unseen", decision="react", react="like")
            led.texting_set(react, handed=WED - 5)
            assert intake_for(s, led)[0].take(load(s.file).people["alex"], tap) == "auto" and led.texting_get(react)["claimed"] == "9001"
        assert armed(s, led, rid)["handed"] is not None and calls(tmp_path / str(n)) == [f"yerk-{rid}-1"]


def test_a_from_me_row_outside_a_handed_jobs_window_is_the_owners_and_claims_nothing(tmp_path: Path) -> None:
    for name, offset, side in (("early", -11.0, "owner"), ("late", 181.0, "owner"), ("inside", 179.0, "auto")):
        s, led, clock = make(tmp_path / name)
        took, _ = intake_for(s, led)
        rid = due(s, led, clock, "t-1")
        handed = armed(s, led, rid)["handed"]
        rows(s).add("m-1", handed + offset, "on my way", mine=True)
        assert took.take(load(s.file).people["alex"], rows(s).rows[-1]) == side
        assert bool(led.texting_get(rid)["claimed"]) is (side == "auto")


def test_a_send_seen_after_its_job_went_unseen_is_sent_late_and_auto_and_cancels_nothing_and_outside_every_window_is_his(tmp_path: Path) -> None:
    for name, offset, side in (("inside", 20.0, "auto"), ("outside", 400.0, "owner")):
        s, led, clock = make(tmp_path / name)
        took, withdrawn = intake_for(s, led)
        their(led, "t-0", WED - 60)
        pending = waiting(led, "t-0", state="deciding")
        rid = due(s, led, clock, "t-1")
        handed = armed(s, led, rid)["handed"]
        clock.t = handed + 300                                 # the bridge was away: the window closed with nothing seen
        assert armed(s, led, rid)["state"] == "unseen"
        rows(s).add("m-1", handed + offset, "on my way", mine=True)
        assert took.take(load(s.file).people["alex"], rows(s).rows[-1]) == side
        got, mine = led.texting_get(rid), led.inbound_get("m-1")["sender"]
        assert (got["state"], got["reason"], mine) == (("sent", words.TEXTING_LATE.format(n=1, of=1), "auto") if side == "auto"
                                                       else ("unseen", words.TEXTING_SENT["unseen"], "owner"))
        assert led.texting_get(pending)["state"] == ("deciding" if side == "auto" else "cancelled")
        assert withdrawn == ([] if side == "auto" else [pytest.approx(handed + offset, abs=1e-3)])
    s, led, clock = make(tmp_path / "two")                     # part 2 was never handed: it stays dropped, and the reason says so
    took, _ = intake_for(s, led)
    rid = due(s, led, clock, "t-1", texts=("on my way", "ten min"))
    handed = armed(s, led, rid)["handed"]
    clock.t = handed + 300
    armed(s, led, rid)
    rows(s).add("m-1", handed + 20, "on my way", mine=True)
    assert took.take(load(s.file).people["alex"], rows(s).rows[-1]) == "auto"
    clock.t += 60
    armed(s, led, rid)
    assert (led.texting_get(rid)["reason"], calls(tmp_path / "two")) == (words.TEXTING_LATE.format(n=1, of=2), [f"yerk-{rid}-1"])


def test_a_late_row_claims_a_queued_job_before_an_unseen_one_and_a_tapback_never_claims_a_text_job(tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    took, _ = intake_for(s, led)
    old, new = waiting(led, "t-1"), waiting(led, "t-2")
    led.texting_set(old, state="unseen", handed=WED, reason=words.TEXTING_SENT["unseen"])
    led.texting_set(new, handed=WED + 100)
    clock.t = WED + 400                                         # both windows closed; each row's own time is inside both
    for guid, side in (("m-1", "auto"), ("m-tap", "owner"), ("m-2", "auto")):  # the tapback, with `old` open: the owner's own answer
        if guid == "m-tap":
            row = rows(s).tap(9001, WED + 95)
        else:
            rows(s).add(guid, WED + 95, "on my way", mine=True)
            row = rows(s).rows[-1]
        assert took.take(load(s.file).people["alex"], row) == side
    assert [(r["state"], r["claimed"]) for r in map(led.texting_get, (new, old))] == [("sent", "m-1"), ("sent", "m-2")]


@pytest.fixture
def sock():
    d = tempfile.mkdtemp(prefix="yt")             # under TMPDIR, short: a socket path has a length limit
    yield str(Path(d) / "b.sock")
    shutil.rmtree(d, ignore_errors=True)


def test_live_texting_own_tapback_from_the_watch_claims_its_job_as_sent_and_auto_and_a_removal_or_a_text_claims_none(sock, tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    took, withdrawn = intake_for(s, led)
    alex, fb = load(s.file).people["alex"], FakeBridge(sock)
    fb.chat(1, A1)
    six = fb.add(1, "see you at six", ago=60)
    their(led, six["guid"], time.time() - 60)
    their(led, "t-2", time.time() - 20)
    job, pending = waiting(led, six["guid"], decision="react", react="like"), waiting(led, "t-2", state="deciding")
    led.texting_set(job, handed=time.time() - 5)

    async def go() -> dict[str, Any]:
        await fb.start()
        link = Link(load(s.file), tmp_path, took.take, unix=sock)
        task = asyncio.create_task(link.start())
        await until(lambda: link.connected)
        fb.push(off := fb.tap(1, six, from_me=True, add=False))
        await until(lambda: led.inbound_get(off["guid"]) is not None)
        assert (led.texting_get(job)["state"], led.texting_get(job)["claimed"]) == ("queued", None)     # a removal claims nothing
        fb.push(own := fb.tap(1, six, from_me=True))
        await until(lambda: led.texting_get(job)["state"] == "sent")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fb.stop()
        return own
    own = asyncio.run(go())
    assert led.texting_get(job)["claimed"] == str(own["id"]) and led.inbound_get(own["guid"])["sender"] == "auto"
    assert led.texting_get(pending)["state"] == "deciding" and withdrawn == []
    clock.t = time.time() + 400                                  # past its window: a claimed tapback is never unseen
    asyncio.run(s.tick())
    assert led.texting_get(job)["state"] == "sent"
    again = waiting(led, "t-2", decision="react", react="like")   # a text row never claims a tapback job
    led.texting_set(again, handed=time.time())
    rows(s).add("m-1", time.time(), "on my way", mine=True)
    assert took.take(alex, rows(s).rows[-1]) == "owner" and led.texting_get(again)["claimed"] is None


def test_dry_claims_nothing_not_even_a_job_a_live_run_left_handed(tmp_path: Path) -> None:
    s, led, _ = make(tmp_path, mode="dry")
    rid = waiting(led, "t-1")
    led.texting_set(rid, handed=WED)
    rows(s).add("m-1", WED + 5, "on my way", mine=True)
    assert s.claim(load(s.file).people["alex"], rows(s).rows[-1], WED + 5) is False and led.texting_get(rid)["state"] == "queued"


def test_a_stop_in_the_middle_of_a_hand_off_counts_as_handed_and_the_text_is_never_handed_again(tmp_path: Path) -> None:
    """J41: the mark is written before the hands run, so a daemon stopped between the write on the Mac and the mark can
    never write that job a second time."""
    s, led, clock = make(tmp_path)
    rid = due(s, led, clock, "t-1")
    real = s.hands

    class Stopped:
        async def hand(self, name: str, job: dict[str, Any]) -> str:
            await real.hand(name, job)                       # the job is on the Mac
            raise RuntimeError("the daemon stopped")          # and the daemon is gone before it hears so
    s.hands = Stopped()
    with pytest.raises(RuntimeError):
        asyncio.run(s.tick())
    assert led.texting_get(rid)["handed"] == clock.t and calls(tmp_path) == [f"yerk-{rid}-1"]
    again = sender.Sender({"mode": "live"}, s.file, led, s.thread, real, clock=clock)       # the next start
    for _ in range(4):
        clock.t += 60
        asyncio.run(again.tick())
    assert calls(tmp_path) == [f"yerk-{rid}-1"] and led.texting_get(rid)["state"] == "unseen"


def test_the_daemon_gives_dry_hands_that_only_write_files_and_live_ones_ssh_to_a_plain_host_or_no_command(tmp_path: Path) -> None:
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "texting").mkdir()
    listed(private / "texting")
    for n, (mode, kind, host, argv) in enumerate((("dry", hands.Dry, "mac", None), ("live", hands.Live, "mac", [*texts.SSH, "mac"]),
                                                  ("live", hands.Live, "-oProxyCommand=x", []))):
        (private / "yerkd.toml").write_text(f'[texting]\nmode = "{mode}"\nreport = "example-room"\noutbox = "/Users/someone/outbox"\n'
                                            f'\n[imessage]\nhost = "{host}"\nsocket = "/Users/someone/bridge.sock"\n')
        (state := tmp_path / f"state{n}").mkdir()
        got = Yerkd(private, state).texting()
        send = got[-1].cr_frame.f_locals["self"]             # the sender, from its own task
        wrote = next(x for c in got if isinstance(x := c.cr_frame.f_locals.get("self"), writer.Writer))
        for c in got:
            c.close()
        assert type(send.hands) is kind and getattr(send.hands, "argv", None) == argv
        assert wrote.head == ([] if host.startswith("-") else [*texts.SSH, "mac"])     # a photo is looked at over the same ssh, in dry too


def test_status_is_told_the_last_text_out_how_many_are_held_and_how_many_unseen(tmp_path: Path) -> None:
    s, led, clock = make(tmp_path)
    told: list[dict[str, Any]] = []
    s.status = told.append
    took, _ = intake_for(s, led)
    lost = due(s, led, clock, "t-1")
    armed(s, led, lost)
    led.texting_add(person="beta", handle=B1, inbound="t-b", decision="held", state="held", reason="", mode="live")
    for _ in range(4):
        clock.t += 60
        armed(s, led, lost)
    assert told[-1] == {"last_out": None, "held": 1, "unseen": 1, "pass": round(clock.t)}      # a pass a minute apart: each one told
    n = len(told)
    for _ in range(5):                                           # passes a second apart: the time is told at most every 15 s
        clock.t += 1
        armed(s, led, lost)
    assert len(told) == n
    rid = due(s, led, clock, "t-2")
    handed = armed(s, led, rid)["handed"]
    rows(s).add("m-1", handed + 4, "on my way", mine=True)
    assert took.take(load(s.file).people["alex"], rows(s).rows[-1]) == "auto"
    armed(s, led, rid)
    assert {**told[-1], "pass": 0} == {"last_out": handed, "held": 1, "unseen": 1, "pass": 0}


def test_an_error_the_pass_does_not_expect_stops_the_sender_and_says_one_line_in_the_machine_room_with_no_text_or_person(
        tmp_path: Path, monkeypatch: Any, caplog: Any) -> None:
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "texting").mkdir()
    listed(private / "texting")
    (private / "yerkd.toml").write_text('[texting]\nmode = "live"\nreport = "example-room"\noutbox = "/Users/someone/outbox"\n'
                                        '\n[imessage]\nhost = "mac"\nsocket = "/Users/someone/bridge.sock"\n')
    (state := tmp_path / "state").mkdir()
    said: list[str] = []
    monkeypatch.setattr(Yerkd, "to_machine", lambda self, text: said.append(text))
    got = Yerkd(private, state).texting()
    for c in got[:-1]:
        c.close()
    send, passes = got[-1].cr_frame.f_locals["self"], []
    assert send.day_file == state / "texting" / "daily.json"   # the daily line's day, kept where the daemon says

    async def boom() -> None:
        passes.append(1)
        raise KeyError("Alex: see you at six")
    send.tick = boom

    async def go() -> None:
        with pytest.raises(asyncio.TimeoutError):            # stopped, never ended: a task that ends ends the daemon
            await asyncio.wait_for(got[-1], 1.25)            # past the next pass's second: it never comes
    with caplog.at_level(logging.ERROR, logger="yerk.texting"):
        asyncio.run(go())
    assert said == [words.TEXTING_SENDER_DOWN.format(error="KeyError")] and passes == [1]
    assert not any(x in said[0].casefold() for x in ("alex", "see you", A1)) and "the sender stopped" in caplog.text


def test_the_daily_line_counts_a_made_up_day_from_the_ledger_says_it_once_across_a_restart_skips_none_and_names_no_one(
        tmp_path: Path, monkeypatch: Any) -> None:
    s, led, clock = make(tmp_path)
    said: list[str] = []
    s.machine, s.day_file = said.append, tmp_path / "state" / "texting" / "daily.json"
    for n, who in enumerate(("alex", "alex", "beta")):
        their(led, f"t-{n}", WED + n, person=who)
    their(led, "t-3", at(11, 12, 2, 0), person="beta")           # two in the morning: the day runs to 08:00, not to midnight
    his(led, "m-0", WED + 10)                                    # the owner's texts: one before the run, one inside it, which starts it over
    his(led, "m-1", WED + 32.5)
    alex, beta = ("reply", "sent", "", "alex"), ("reply", "sent", "", "beta")
    for n, (decision, state, reason, who) in enumerate((alex, alex, beta, alex) + (     # Beta's send between Alex's: a run is one person's
            ("react", "cancelled", "owner answered", "beta"), ("reply", "cancelled", "owner answered", "beta"),
            ("reply", "cancelled", "they wrote again", "beta"), ("reply", "cancelled", "a newer decision", "beta"),
            ("none", "decided", "", "beta"), ("held", "held", "", "beta"), ("reply", "unseen", "", "beta"))):
        with monkeypatch.context() as m:                         # a decision's own time is the ledger's clock
            m.setattr(time, "time", lambda: WED + 20 + n)
            rid = waiting(led, "t-2", decision=decision, state=state, word=decision == "held", person=who)
        led.texting_set(rid, reason=reason, handed=WED + 30 + n if state in ("sent", "unseen") else None)
    clock.t = at(11, 12, 9, 0)
    s.daily()
    line = words.TEXTING_DAY.format(mode="live", day="Thu Nov 12", got=4, kinds="1 held, 1 none, 1 react, 8 reply", held=1, sent=4,
                                    out="sent", first=2, again=2, unseen=1, run=2)
    assert said == [line]
    again = sender.Sender({"mode": "live"}, s.file, led, rows(s), s.hands, clock=clock, machine=said.append, day_file=s.day_file)
    again.daily()                                                 # a restart: that day is said no second time
    clock.t = at(11, 13, 7, 59)
    again.daily()
    assert said == [line]
    clock.t = at(11, 16, 8, 1)                                    # days on: none is skipped, a quiet one in a few words
    again.daily()
    assert said[1:] == [words.TEXTING_DAY_QUIET.format(mode="live", day=d) for d in ("Fri Nov 13", "Sat Nov 14", "Sun Nov 15", "Mon Nov 16")]
    clock.t = at(12, 20, 9, 0)                                    # a month on: the last week's days, no more
    again.daily()
    assert said[5:] == [words.TEXTING_DAY_QUIET.format(mode="live", day=f"{d} Dec {n}")
                        for d, n in zip("Mon Tue Wed Thu Fri Sat Sun".split(), range(14, 21))]
    assert not any(x in y.casefold() for y in said for x in ("alex", "beta", "5550000", "see you", "on my way"))
