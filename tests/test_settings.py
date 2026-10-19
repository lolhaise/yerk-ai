"""A room's model and thinking level from the room (the settings tool): owner rooms only, listed models
and levels, the daemon's override over rooms.toml, applied once the running turn ends with the same
session resumed; and the relay of a worker that left no report."""
import asyncio
import json
import time
from pathlib import Path

import pytest

from yerk_ai import cli, words, workers
from yerk_ai.accounts import Account, Cooldowns
from yerk_ai.config import EXAMPLE_PRIVATE, table
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import RoomSpec
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.room import Inbound, Room, fit

FAKE = Path(__file__).with_name("fake_claude.py")
ROOM = "example-room"


class Chan:
    def __init__(self):
        self.replies = []

    def open_card(self, room, text, kind="turn"):
        pass

    def progress(self, room, text):
        pass

    def reply(self, room, text, inbound, card=None, key=None):
        self.replies.append(text)
        return [f"m{len(self.replies)}"]

    def notice(self, room, text, inbound, card=None, key=None):
        self.replies.append(text)
        return ["n1"]


@pytest.fixture
def y(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_MARK", str(tmp_path / "mark"))
    return Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))


def ask(y, model="", effort="", room=ROOM):
    return asyncio.run(y.tool_settings(room, model, effort))


def say(y, room, mid, text):
    y.ledger.inbound_add(mid, ROOM, "console", "owner", len(text))
    room.submit(Inbound(mid, "owner", text))


def wait_idle(room, timeout=20):
    end = time.time() + timeout
    while (room.busy or room.queue) and time.time() < end:
        time.sleep(0.05)
    assert not room.busy


def config_get(tmp_path, capsys):
    assert cli.main(["config", "get", "room", ROOM, "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    return capsys.readouterr().out.splitlines()


def test_the_switch_waits_for_the_turn_to_end_then_the_same_session_resumes_on_the_new_model(y, tmp_path):
    chan = Chan()
    room = y.add_room(ROOM, chan)
    say(y, room, "1", "SLOW hello")
    time.sleep(0.4)                                                    # the turn is running
    first = room.rp
    assert ask(y, "sonnet", "low") == {"ok": True, "line": "next reply: Sonnet 5.5, low thinking"}
    assert room.rp is first and first.alive() and first.spec.model == "claude-fable-5-1"    # never mid-turn
    wait_idle(room)
    assert room.rp is None and not first.alive() and chan.replies == ["echo: SLOW hello"]   # closed at its end
    say(y, room, "2", "and now?")
    wait_idle(room)
    argv = [json.loads(x) for x in (tmp_path / "argv.jsonl").read_text().splitlines()][-1]["argv"]
    assert argv[argv.index("--resume") + 1] == first.session_id == room.rp.session_id      # the same session
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5" and argv[argv.index("--effort") + 1] == "low"
    assert chan.replies[-1] == "echo: and now?"
    room.rp.close()


def test_a_restart_inside_the_turn_that_changed_the_settings_stays_on_the_turn_s_model(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    now = {"model": "m-old"}
    room = Room("r", {}, lambda: RoomSpec(room="r", model=now["model"], cwd=tmp_path), [Account("one", tmp_path / "c")],
                Chan(), tmp_path, Ledger(tmp_path / "l.sqlite"), FailureTable.load(table("failures.toml")), Cooldowns(None))
    room._start(room.accounts[0])
    first = room.rp
    now["model"], room.busy = "m-new", True                          # the tool ran mid-turn
    room.respec()
    assert room.rp is first and room.stale
    room._start(room.accounts[0], fresh=True)                         # a hop or a fresh retry, same turn
    assert room.rp.spec.model == "m-old"
    room.rp.close()
    first.close()
    room.stale, room.busy = False, False                             # the turn ended: the next start is new
    room._start(room.accounts[0])
    assert room.rp.spec.model == "m-new"
    room.rp.close()


def test_owner_rooms_only_listed_models_and_levels_and_default_clears(y, tmp_path, capsys):
    y.room_confs["example-quiet"] = {**y.room_confs["example-quiet"], "privacy": "family"}
    assert ask(y, "opus", room="example-quiet") == {"ok": False, "line": words.OWNER_ONLY}
    assert ask(y, "opus", room="") == {"ok": False, "line": words.OWNER_ONLY}      # no binding, no room
    assert ask(y, "gpt-9") == {"ok": False, "line": "not on the list: gpt-9\nthe list: opus (Opus 5.5), "
                                                    "sonnet (Sonnet 5.5), haiku (Haiku 5.5), fable (Fable 5.1)"}
    assert ask(y, "", "turbo") == {"ok": False, "line": "not a thinking level: turbo\n"
                                                        "the levels: low, medium, high, xhigh, max, default"}
    assert not list(tmp_path.glob("rooms/*/override.json"))           # a refusal writes nothing
    assert config_get(tmp_path, capsys) == [f"rooms.{ROOM}.model = claude-fable-5-1 (rooms.toml)",
                                            f"rooms.{ROOM}.effort = unset (the model's own default)"]
    assert ask(y, "claude-haiku-5-5", "MAX")["line"] == "next reply: Haiku 5.5, max thinking"   # a full id
    f = tmp_path / "rooms" / ROOM / "override.json"
    assert f.stat().st_mode & 0o777 == 0o600 and json.loads(f.read_text()) == {"model": "claude-haiku-5-5",
                                                                                 "effort": "max"}
    assert config_get(tmp_path, capsys) == [f"rooms.{ROOM}.model = claude-haiku-5-5 (the room's override)",
                                            f"rooms.{ROOM}.effort = max (the room's override)"]
    assert ask(y, "", "default")["line"] == "next reply: Haiku 5.5, default thinking"
    assert ask(y, "Default")["line"] == "next reply: Fable 5.1, default thinking"
    assert ask(y) == {"ok": True, "line": "this room: Fable 5.1, default thinking"}
    spec = y.spec_for(ROOM)
    assert spec.model == "claude-fable-5-1" and "--effort" not in spec.extra_args and json.loads(f.read_text()) == {}


def test_the_override_survives_a_new_daemon_on_the_same_state(y, tmp_path):
    before = (EXAMPLE_PRIVATE / "rooms.toml").read_bytes()
    assert ask(y, "OPUS", "high")["ok"]
    spec = Yerkd(EXAMPLE_PRIVATE, tmp_path).spec_for(ROOM)            # a restart: a new daemon, same state
    assert spec.model == "claude-opus-5-5" and spec.extra_args[:2] == ["--effort", "high"]
    assert (EXAMPLE_PRIVATE / "rooms.toml").read_bytes() == before   # rooms.toml is never written


def test_a_relay_from_a_worker_that_left_no_report_says_so_never_report_saved(y, tmp_path):
    y.add_room(ROOM, Chan())
    row = {"id": "abc123", "cap": 900.0, "started": time.time() - 185, "account": "one", "model": "m", "room": ROOM,
           "report": str(tmp_path / "abc123" / "REPORT.md"), "tasks": str(tmp_path / "TASKS.md")}
    end = {"exit": "done", "finished": row["started"] + 181, "result": "no report file", "has_report": False,
           "result_code": "success", "detail": "success, exited 0"}
    none = "-# no report: `yerk worker report abc123`"
    text = workers.relay_text(row, end, 1500)
    assert "report saved" not in text and f'ending with the line "{none}"' in text
    asked = []
    y.workers.ask = lambda room, msg: asked.append(msg)
    assert asyncio.run(y.workers.relay(row, end)) == "" and asked[0].must == none
    reply = fit("It ended without writing anything down.", 1500, [asked[0].must])
    assert reply.endswith(f"\n{none}") and "report saved" not in reply
    (tmp_path / "abc123").mkdir()
    (tmp_path / "abc123" / "REPORT.md").write_text("All three done\n")
    assert workers.relay_line(row) == "-# report saved: `yerk worker report abc123`"     # a file: saved
