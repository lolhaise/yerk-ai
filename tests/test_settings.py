"""Checks the settings tool, with which a room's model, thinking level and account are changed from the room
itself. Owner rooms only, listed models, levels and accounts, the daemon's override over rooms.toml, applied once
the running turn ends with the same session resumed; and the relay of a worker that left no report."""
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from yerk_ai import cli, config, words, workers
from yerk_ai.accounts import Account, Cooldowns
from yerk_ai.config import EXAMPLE_PRIVATE, load_toml, table
from yerk_ai.daemon import Yerkd
from yerk_ai.engine import RoomSpec
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.room import RESUME_RECAP, Inbound, Room, fit

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


def ask(y, model="", effort="", room=ROOM, account=""):
    return asyncio.run(y.tool_settings(room, model, effort, account))


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
                                                    "sonnet (Sonnet 5.5), haiku (Haiku 5.5), fable (Fable 5.2), fable51 (Fable 5.1)"}
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


def argvs(tmp_path):
    return [a for a in (json.loads(x)["argv"] for x in (tmp_path / "argv.jsonl").read_text().splitlines()) if "--model" in a]


def test_a_planted_override_off_the_lists_never_reaches_the_command_line(y, tmp_path, capsys, caplog, monkeypatch):
    monkeypatch.setattr(config, "_IGNORED", set(), raising=False)
    f = tmp_path / "rooms" / ROOM / "override.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"model": "not-on-any-list", "effort": "not-a-level"}))      # by hand, not by the tool
    with caplog.at_level(logging.WARNING, logger="yerk.config"):
        spec, again = y.spec_for(ROOM), y.spec_for(ROOM)                                  # two process starts
    assert spec.model == again.model == "claude-fable-5-1" and "--effort" not in spec.extra_args   # rooms.toml's
    assert [r.getMessage() for r in caplog.records if r.name == "yerk.config"] == [         # logged once
        f"room {ROOM}: the override's model 'not-on-any-list' is not on the list; rooms.toml's is used",
        f"room {ROOM}: the override's effort 'not-a-level' is not a level; rooms.toml's is used"]
    assert config_get(tmp_path, capsys) == [
        f"rooms.{ROOM}.model = claude-fable-5-1 (rooms.toml; the room's override 'not-on-any-list', not on the list: ignored)",
        f"rooms.{ROOM}.effort = unset (the model's own default; the room's override 'not-a-level', not a level: ignored)"]
    assert ask(y) == {"ok": True, "line": "this room: Fable 5.1, default thinking"}
    del y.models["haiku"]                                    # a model taken off the list after the switch
    f.write_text(json.dumps({"model": "claude-haiku-5-5", "effort": "max"}))
    spec = y.spec_for(ROOM)
    assert spec.model == "claude-fable-5-1" and spec.extra_args[:2] == ["--effort", "max"]


def test_the_models_list_ships_in_the_package_and_a_yerkd_toml_list_replaces_it(tmp_path, monkeypatch):
    four = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5",
            "fable": "claude-fable-5-2", "fable51": "claude-fable-5-1"}
    monkeypatch.setattr(config, "EXAMPLE_PRIVATE", tmp_path / "hidden")                    # no examples folder
    assert config.models({}) == four and config.models({"models": {"X": "claude-x-1"}}) == {"x": "claude-x-1"}
    site = (tmp_path / "site").resolve()          # an install: the package and pyproject's wheel tables, no repo
    shutil.copytree(config.PACKAGE, site / "yerk_ai", ignore=shutil.ignore_patterns("__pycache__"))
    wheel = load_toml(config.REPO / "pyproject.toml")["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    for src, dst in wheel.items():
        shutil.copy(config.REPO / src, site / dst)
    probe = ("import json, sys; sys.path.insert(0, sys.argv[1]); from yerk_ai import config; "
             "print(json.dumps([str(config.PACKAGE), str(config.table('models.toml')), config.models({})]))")
    out = subprocess.run([sys.executable, "-I", "-c", probe, str(site)], capture_output=True, text=True, check=True,
                         cwd=tmp_path).stdout
    assert json.loads(out) == [str(site / "yerk_ai"), str(site / "yerk_ai" / "models.toml"), four]
    assert not (site / "examples").exists()


def test_a_model_that_is_not_found_drops_the_override_and_the_next_turn_is_on_rooms_toml_s(y, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_BAD_MODEL", "claude-gone-1")
    y.models["gone"] = "claude-gone-1"                       # on the list, but no account can run it
    chan = Chan()
    room = y.add_room(ROOM, chan)
    say(y, room, "1", "hello")
    wait_idle(room)
    sid = room.rp.session_id
    assert ask(y, "gone")["line"] == "next reply: Gone 1, default thinking"
    say(y, room, "2", "and now?")
    wait_idle(room)
    assert chan.replies[-1] == f"{words.NOT_DONE} {words.NOT_ANSWERED}\nGone 1 not found; back on Fable 5.1"
    assert json.loads((tmp_path / "rooms" / ROOM / "override.json").read_text()) == {} and room.rp is None
    say(y, room, "3", "still there?")
    wait_idle(room)
    runs = argvs(tmp_path)
    assert [a[a.index("--model") + 1] for a in runs] == ["claude-fable-5-1", "claude-gone-1", "claude-fable-5-1"]
    assert runs[-1][runs[-1].index("--resume") + 1] == sid and chan.replies[-1] == "echo: still there?"
    room.rp.close()
    said = "There's an issue with the selected model (claude-gone-1)"                   # the text route names it too
    assert FailureTable.load(table("failures.toml")).classify("invalid_request", said).source == "pattern:model"


def test_a_turn_that_raises_after_the_switch_still_lands_it_at_its_end(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    now = {"model": "m-old"}
    room = Room("r", {}, lambda: RoomSpec(room="r", model=now["model"], cwd=tmp_path), [Account("one", tmp_path / "c")],
                Chan(), tmp_path, Ledger(tmp_path / "l.sqlite"), FailureTable.load(table("failures.toml")), Cooldowns(None))
    results = room._results

    def crash(*args):
        now["model"] = "m-new"                                 # the tool ran mid-turn,
        room.respec()
        raise RuntimeError("the channel broke")                # then the turn raised
    monkeypatch.setattr(room, "_results", crash)
    room.submit(Inbound("1", "owner", "hello"))
    wait_idle(room)
    assert room.rp is None and not room.stale                  # the switch landed at the crashed turn's end
    monkeypatch.setattr(room, "_results", results)
    room.submit(Inbound("2", "owner", "and now?"))
    wait_idle(room)
    assert [a[a.index("--model") + 1] for a in argvs(tmp_path)] == ["m-old", "m-new"] and room.rp.spec.model == "m-new"
    room.rp.close()


def test_an_account_puts_the_room_on_it_first_default_clears_and_a_name_off_the_list_is_refused(y, tmp_path, capsys):
    y.accounts.append(Account("two", tmp_path / "cfg2"))
    f, before = tmp_path / "rooms" / ROOM / "override.json", (EXAMPLE_PRIVATE / "rooms.toml").read_bytes()
    y.room_confs["example-quiet"] = {**y.room_confs["example-quiet"], "privacy": "family"}
    assert ask(y, account="two", room="example-quiet") == {"ok": False, "line": words.OWNER_ONLY}
    assert ask(y, account="five") == {"ok": False, "line": "not an account: five\nthe accounts: one, two, default"}
    assert ask(y, "opus", "max", account="five")["ok"] is False and not f.exists()      # a refusal writes nothing
    assert ask(y, account="Two") == {"ok": True, "line": "next reply: Fable 5.1, default thinking, on account two"}
    assert json.loads(f.read_text()) == {"account": "two"} and f.stat().st_mode & 0o777 == 0o600
    assert ask(y) == {"ok": True, "line": "this room: Fable 5.1, default thinking, on account two"}
    assert ask(y, "sonnet", "high")["line"] == "next reply: Sonnet 5.5, high thinking, on account two"   # the pin stays
    assert ask(y, account="DEFAULT") == {"ok": True, "line": "next reply: Sonnet 5.5, high thinking"}  # cleared: no word
    assert json.loads(f.read_text()) == {"model": "claude-sonnet-5-5", "effort": "high"}
    assert ask(y)["line"] == "this room: Sonnet 5.5, high thinking" and y.spec_for(ROOM).model == "claude-sonnet-5-5"
    assert config_get(tmp_path, capsys) == [f"rooms.{ROOM}.model = claude-sonnet-5-5 (the room's override)",
                                            f"rooms.{ROOM}.effort = high (the room's override)"]   # yerk sets no account
    assert (EXAMPLE_PRIVATE / "rooms.toml").read_bytes() == before                         # never written


def test_a_planted_account_off_accounts_toml_is_ignored_and_logged_once(y, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(config, "_IGNORED", set(), raising=False)
    y.accounts.append(Account("two", tmp_path / "cfg2"))
    f = tmp_path / "rooms" / ROOM / "override.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"account": "gone"}))              # an account since taken out of accounts.toml
    room = y.add_room(ROOM, Chan())
    with caplog.at_level(logging.WARNING, logger="yerk.config"):
        assert [room._account().name, room._account().name] == ["one", "one"]           # two starts: the room's order
        assert ask(y) == {"ok": True, "line": "this room: Fable 5.1, default thinking"}  # nothing said about accounts
    assert [r.getMessage() for r in caplog.records if r.name == "yerk.config"] == [
        f"room {ROOM}: the override's account 'gone' is not in accounts.toml; the room's order is used"]
    f.write_text(json.dumps({"account": "two"}))
    assert room._account().name == "two" and ask(y)["line"].endswith(", on account two")


def test_model_command_splits_an_account_from_a_model_and_only_its_default_clears_both(y, tmp_path):
    y.accounts += [Account("two", tmp_path / "cfg2", order=2), Account("five", tmp_path / "cfg5", order=5)]
    f, names = tmp_path / "rooms" / ROOM / "override.json", ["one", "two", "five"]
    assert [config.account_and_model(t, y.models, names) for t in ("5", "Five fable", "five/claude-fable-5-1", "fable", "default", "nine", "two  opus")] == [
        ("five", ""), ("five", "fable"), ("five", "claude-fable-5-1"), ("", "fable"), ("", "default"), ("", "nine"), ("two", "opus")]
    assert config.account_and_model("haiku", y.models, ["haiku"]) == ("", "haiku")                   # what reads as a model stays one,
    assert config.account_and_model("default", y.models, ["default"]) == ("", "default")             # an account of that name or not
    assert config.account_named("²", names) == "" and config.account_named("3", ["three", "3"]) == "3"   # "²".isdigit(); a name first

    def run(text, account=""):
        return asyncio.run(y.model_command(ROOM, text, account))
    assert run("5 sonnet")["line"] == "next reply: Sonnet 5.5, default thinking, on account five"
    assert ask(y, "default")["line"] == "next reply: Fable 5.1, default thinking, on account five"   # the room's tool: the model only
    assert ask(y, account="2")["line"] == "next reply: Fable 5.1, default thinking, on account two"   # the digit there too
    assert run("sonnet")["line"] == "next reply: Sonnet 5.5, default thinking, on account two"        # a model alone keeps the pin
    assert run("default") == {"ok": True, "line": "next reply: Fable 5.1, default thinking"} and json.loads(f.read_text()) == {}
    assert run("default", "five")["line"] == "next reply: Fable 5.1, default thinking, on account five"   # a typed account wins
    assert run("two", " ")["line"] == "next reply: Fable 5.1, default thinking, on account two"           # a blank one is none
    y.room_confs["example-quiet"] = {**y.room_confs["example-quiet"], "privacy": "family"}
    assert asyncio.run(y.tool_settings("example-quiet", "", "", "2")) == {"ok": False, "line": words.OWNER_ONLY}   # not an owner room


def test_an_account_switch_lands_between_turns_and_its_line_says_what_the_next_message_does(y, tmp_path, monkeypatch):
    y.accounts.append(Account("two", tmp_path / "cfg2"))
    chan = Chan()
    room = y.add_room(ROOM, chan)
    say(y, room, "1", "SLOW hello")
    time.sleep(0.4)                                                    # the turn is running
    first = room.rp
    t = tmp_path / "cfg1" / "projects" / "work" / f"{first.session_id}.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text("{}\n")
    y.learn_transcript(ROOM, {"session_id": first.session_id, "transcript_path": str(t)})   # the hooks named it
    assert ask(y, account="two")["line"] == f"next reply: Fable 5.1, default thinking, on account two\n{words.CARRIED}"
    assert room.rp is first and first.alive() and first.account.name == "one"                # never mid-turn
    wait_idle(room)
    assert room.rp is None and chan.replies == ["echo: SLOW hello"]                       # closed at its end
    say(y, room, "2", "and now?")
    wait_idle(room)
    run = [json.loads(x) for x in (tmp_path / "argv.jsonl").read_text().splitlines()][-1]
    assert run["config_dir"] == str(tmp_path / "cfg2") and run["argv"][run["argv"].index("--resume") + 1] == str(t)
    assert chan.replies[-1] == "echo: and now?" and room.rp.session_id == first.session_id   # the same session, as a hop
    t.unlink()                                                         # no path on file now: the id alone, on account one,
    monkeypatch.setenv("FAKE_CLAUDE_MISSING", "1")                     # where Claude Code finds no such session
    assert ask(y, account="default")["line"] == f"next reply: Fable 5.1, default thinking\n{words.RECAPPED}"
    assert room.rp is None                                             # idle: closed at once
    say(y, room, "3", "still there?")
    wait_idle(room)
    assert chan.replies[-1] == "echo: " + RESUME_RECAP.format(last="echo: and now?") + "\n\nstill there?"
    assert room.rp.account.name == "one" and room.rp.session_id != first.session_id
    assert ask(y, account="default")["line"] == "next reply: Fable 5.1, default thinking"   # no change: no second line
    room.rp.close()
