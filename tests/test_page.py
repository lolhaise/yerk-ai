"""Checks a room's live page, the room on a browser tab (channels/page.py): your typing, drawing and test runs there reach the room
as messages, mid-turn too, the tests run when the page asks with no model, and what the room says and draws shows on the page."""
import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from yerk_ai import mcp_server, words
from yerk_ai.channels import page as pagemod
from yerk_ai.channels.page import Mirror, Page, address, change, run_line, run_tests
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.room import Inbound
from fake_channel import ConsoleChannel
from test_room import Chan, env, wait_idle  # noqa: F401  (env is the fixture the room tests run their fake Claude on)

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TESTS = ("import unittest\nfrom solution import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n"
         "        self.assertEqual(add(2, 2), 4)\n\n    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n")


# a run goes in bubblewrap or not at all: where it cannot start (no bwrap, or user namespaces refused), these tests have
# nothing to run on, and test_with_no_sandbox_nothing_runs_and_the_run_says_so covers that box
boxed = pytest.mark.skipif(not pagemod.sandbox_starts(), reason="bubblewrap is missing or cannot start here")


class Taken:
    """A room that only takes messages: what the page submits, in order."""
    def __init__(self, name="r"):
        self.name, self.got = name, []

    def submit(self, msg):
        self.got.append(msg)
        return "started"


def hub(tmp_path, quiet=0.05, capture=None):
    p = Page(tmp_path, Ledger(tmp_path / "ledger.sqlite"), capture=capture, quiet=quiet)
    room = Taken()
    p.add_room(room)
    return p, room


def events(tmp_path, room="r"):
    return [json.loads(x) for x in (tmp_path / "rooms" / room / "work" / "page" / "events.jsonl").read_text().splitlines()]


@boxed
def test_the_tests_run_on_the_box_red_then_green_with_no_model(tmp_path):
    red = run_tests(BROKEN, TESTS)
    assert (red["ok"], red["ran"], red["passed"]) == (False, 2, 1)
    failed = next(t for t in red["tests"] if not t["ok"])
    assert failed["name"] == "test_add" and failed["message"] == "AssertionError: 0 != 4" and failed["test_line"] == 7
    assert run_line(red) == "tests: 1 of 2 pass; test_add fails at line 7 of the tests: AssertionError: 0 != 4"
    green = run_tests(FIXED, TESTS)
    assert (green["ok"], green["passed"]) == (True, 2) and run_line(green) == "tests: 2 of 2 pass"
    boom = run_tests("def add(a, b):\n    return a + None\n", TESTS)
    assert next(t for t in boom["tests"] if not t["ok"])["line"] == 2          # the program's own line it failed at
    said = run_tests("print('hello from the program')\n" + FIXED, TESTS)
    assert "hello from the program" in said["output"]


@boxed
def test_a_program_that_will_not_load_or_never_ends_is_said_in_words(tmp_path):
    bad = run_tests("def add(a, b:\n    return a\n", TESTS)
    assert not bad["ok"] and bad["line"] == 1 and "SyntaxError" in bad["error"] and "yerk-page-" not in bad["error"]
    assert run_line(bad).startswith("the tests did not load: ") and run_line(bad).endswith("(line 1)")
    start = time.monotonic()
    loop = run_tests("while True:\n    pass\n", TESTS, seconds=1)
    assert time.monotonic() - start < 5 and loop["error"] == words.PAGE_RAN_PAST.format(n=1) and not loop["ok"]


@boxed
def test_the_tests_run_isolated_from_the_folder_and_the_environment(tmp_path, monkeypatch):
    (tmp_path / "unittest.py").write_text("raise SystemExit('planted')\n")      # a planted module where the daemon runs
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert run_tests(FIXED, TESTS)["ok"]


WALLS = """import os, socket, subprocess, sys, unittest
class T(unittest.TestCase):
    def test_walls(self):
        said = []
        try:
            said.append("read " + open(SECRET).read().strip())
        except OSError:
            said.append("no read")
        try:
            open(OUTSIDE, "w").write("x")
            said.append("wrote")
        except OSError:
            said.append("no write")
        s = socket.socket()
        s.settimeout(2)
        try:
            s.connect(("1.1.1.1", 53))
            said.append("network")
        except OSError:
            said.append("no network")
        subprocess.Popen([sys.executable, "-c", "import os, time; os.setsid(); time.sleep(30)", MARK], start_new_session=True)
        self.fail(" / ".join(said))
"""


@boxed
def test_a_run_reads_no_file_outside_its_folder_writes_none_reaches_no_network_and_leaves_nothing_running(tmp_path):
    secret, outside, mark = tmp_path / "token.secret", tmp_path / "outside.txt", f"yerk-page-orphan-{time.time_ns()}"
    secret.write_text("FAKE-TOKEN-123\n")
    secret.chmod(0o600)
    tests = WALLS.replace("SECRET", repr(str(secret))).replace("OUTSIDE", repr(str(outside))).replace("MARK", repr(mark))
    r = run_tests(FIXED, tests)
    assert r["tests"][0]["message"] == "AssertionError: no read / no write / no network", r
    assert "FAKE-TOKEN" not in run_line(r) and not outside.exists()
    time.sleep(0.3)
    alive = [c for c in Path("/proc").glob("[0-9]*/cmdline") if mark.encode() in (c.read_bytes() if c.exists() else b"")]
    assert not alive                     # a child that left the run's session died with the run's PID namespace


def test_with_no_sandbox_nothing_runs_and_the_run_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(pagemod.shutil, "which", lambda name: None)
    ran = tmp_path / "ran"
    r = run_tests(f"open({str(ran)!r}, 'w').write('x')\n" + FIXED, TESTS)
    assert r["error"] == words.PAGE_NO_SANDBOX and not r["ok"] and not ran.exists() and not pagemod.sandbox_starts()
    assert run_line(r).startswith("the tests did not load: the tests did not run: their sandbox")


def test_a_keystroke_lands_in_the_room_file_at_once_and_the_typing_goes_as_one_message_on_a_pause(tmp_path):
    filed = []
    p, room = hub(tmp_path, capture=lambda *a, **k: filed.append(a))

    async def go():
        await p.event("r", {"kind": "edit", "pane": "code", "text": BROKEN})
        await p.event("r", {"kind": "edit", "pane": "code", "text": BROKEN.replace("a - b", "a + b")})
        assert room.got == []                                    # still typing: nothing yet
        await asyncio.sleep(0.2)
    asyncio.run(go())
    folder = tmp_path / "rooms" / "r" / "work" / "page"
    assert (folder / "solution.py").read_text() == FIXED
    rows = events(tmp_path)
    assert [r["kind"] for r in rows] == ["edit", "edit"] and rows[1] == {**rows[1], "line": 2, "col": 14, "del": "-", "ins": "+"}
    assert len(room.got) == 1
    msg = room.got[0]
    assert msg.sender == "owner" and msg.id.startswith("page-r-")
    assert msg.text.splitlines()[0] == words.PAGE_FROM and "typed in solution.py:" in msg.text and "+    return a + b" in msg.text
    row = p.ledger.inbound_get(msg.id)
    assert row["channel"] == pagemod.CHANNEL and row["sender"] == "owner" and row["room"] == "r"
    assert filed and filed[0][:3] == ("r", msg.id, "owner")    # in the day log, as a Discord message is


def test_a_stroke_a_said_line_the_timer_and_a_stall_go_at_once(tmp_path):
    p, room = hub(tmp_path, quiet=60)

    async def go():
        await p.event("r", {"kind": "stroke", "pane": "code", "lines": [6, 4], "points": [[1, 2], [3, 4], "x"]})
        await p.event("r", {"kind": "say", "text": "why is this one red?"})
        p.draw("r", {"stall": 90})
        await p.event("r", {"kind": "stall"})
        await p.event("r", {"kind": "timer"})
        return await p.event("r", {"kind": "keylogger"})
    unknown = asyncio.run(go())
    texts = [m.text.splitlines()[1] for m in room.got]
    assert texts == ["drew on lines 4 to 6 of the program (2 points)", "said on the page: why is this one red?",
                     "no typing for 90 s", "the timer ran out"]
    assert not unknown["ok"] and "keylogger" in unknown["error"]
    stroke = next(r for r in events(tmp_path) if r["kind"] == "stroke")
    assert stroke["lines"] == [4, 6] and stroke["points"] == [[1, 2], [3, 4]]


@boxed
def test_a_run_rides_in_the_next_message_only_when_it_says_something_new(tmp_path):
    p, room = hub(tmp_path)

    async def go():
        r = await p.run("r", {"code": BROKEN, "tests": TESTS})
        await asyncio.sleep(0.2)
        again = await p.run("r", {})                              # the same result: nothing new to say
        await asyncio.sleep(0.2)
        green = await p.run("r", {"code": FIXED, "tests": TESTS})
        await asyncio.sleep(0.2)
        return r, again, green
    red, again, green = asyncio.run(go())
    assert (red["passed"], again["passed"], green["passed"]) == (1, 1, 2)
    assert len(room.got) == 2
    assert "tests: 1 of 2 pass; test_add fails" in room.got[0].text and "typed in test_solution.py:" in room.got[0].text
    assert "tests: 2 of 2 pass" in room.got[1].text and "+    return a + b" in room.got[1].text
    assert [r["kind"] for r in events(tmp_path)].count("run") == 3


def test_a_page_message_folds_into_the_running_turn_and_the_reply_renders_on_the_page(env):  # noqa: F811
    """The wall the ask hit: mid-turn injection was Discord's alone. A page message is a room message, so it folds into the
    turn already running, and the reply that answers both renders on the page."""
    make, ledger, tmp_path = env
    p = Page(tmp_path, ledger, quiet=60)
    chan = Chan()
    room = make(Mirror(chan, p))
    p.add_room(room)
    ledger.inbound_add("1", "r", "c", "owner", 10)
    assert room.submit(Inbound("1", "owner", "SLOW first")) == "started"
    time.sleep(0.4)

    async def go():
        await p.event("r", {"kind": "say", "text": "second"})
    asyncio.run(go())
    wait_idle(room)
    mid = next(r["message_id"] for r in ledger.rows(10, "inbound") if r["channel"] == pagemod.CHANNEL)
    assert len(chan.replies) == 1 and chan.replies[0][1] == ["1", mid]
    assert "echo: SLOW first + [from the owner's live page]" in chan.replies[0][0]
    row = ledger.inbound_get(mid)
    assert row["folded"] == 1 and row["state"] == "answered"
    assert [x["text"] for x in p.sheets["r"].lines] == [chan.replies[0][0]]   # the reply, on the page


def test_what_the_room_draws_goes_to_every_open_tab_and_a_new_tab_gets_the_whole_state(tmp_path):
    p, _ = hub(tmp_path)

    async def go():
        tab = p.stream("r", beat=0.05)
        first = json.loads(await tab.__anext__())
        got = p.draw("r", {"code": BROKEN, "tests": TESTS, "marks": [{"line": 2, "kind": "arrow", "text": "look here"},
                                                                     {"line": 0}, {"line": 3, "kind": "blink"}],
                           "hint": "  the sign  ", "bar": {"rank": "Silver II", "LP": 40}, "timer": 1200, "stall": 90})
        drawn = json.loads(await tab.__anext__())
        p.said("r", "close: look at the operator")
        p.said("r", words.NO_REPLY)
        said = json.loads(await tab.__anext__())
        beat = await tab.__anext__()
        await tab.aclose()
        late = json.loads(await p.stream("r").__anext__())
        return first, got, drawn, said, beat, late
    first, got, drawn, said, beat, late = asyncio.run(go())
    assert first["state"]["code"] == "" and first["state"]["room"] == "r"
    assert got == {"ok": True, "tabs": 1}
    d = drawn["draw"]
    assert d["marks"] == [{"line": 2, "kind": "arrow", "text": "look here"}, {"line": 3, "kind": "highlight", "text": ""}]
    assert d["hint"] == "the sign" and d["bar"] == {"rank": "Silver II", "LP": "40"} and d["timer"]["seconds"] == 1200 and d["stall"] == 90
    assert said == {"line": {**said["line"], "text": "close: look at the operator", "kind": "reply"}} and beat == b"\n"
    s = late["state"]
    assert s["code"] == BROKEN and s["tests"] == TESTS and s["hint"] == "the sign" and [x["text"] for x in s["lines"]] == ["close: look at the operator"]
    folder = tmp_path / "rooms" / "r" / "work" / "page"
    assert (folder / "solution.py").read_text() == BROKEN and (folder / "test_solution.py").read_text() == TESTS
    assert p.sheets["r"].tabs == []                               # a closed tab leaves


def test_the_page_tool_says_what_is_wrong_in_words(tmp_path):
    p, _ = hub(tmp_path)
    assert p.draw("r", {}) == {"ok": False, "error": words.PAGE_NOTHING}
    assert p.draw("other", {"hint": "x"}) == {"ok": False, "error": words.PAGE_NONE.format(room="other")}
    assert p.draw("r", {"hint": "x"}) == {"ok": True, "tabs": 0, "note": words.PAGE_NO_TAB}
    assert asyncio.run(p.event("other", {"kind": "say", "text": "x"}))["error"] == words.PAGE_NONE.format(room="other")


def test_a_page_message_a_restart_cut_is_closed_and_what_the_page_showed_comes_back(tmp_path):
    p, room = hub(tmp_path, quiet=60)
    p.draw("r", {"code": BROKEN, "tests": TESTS})
    asyncio.run(p.event("r", {"kind": "say", "text": "cut short"}))
    mid = room.got[0].id
    again = Page(tmp_path, p.ledger)
    again.add_room(Taken())
    assert p.ledger.inbound_get(mid)["state"] == "abandoned"
    assert (again.sheets["r"].code, again.sheets["r"].tests) == (BROKEN, TESTS)
    assert again.sheets["r"].key == p.sheets["r"].key                 # the same key: the tab's address still opens it


def test_the_mirror_carries_every_call_to_the_channel_and_the_room_s_words_to_the_page(tmp_path, capsys):
    p, _ = hub(tmp_path)
    m = p.mirror(ConsoleChannel())
    assert m.reply("r", "the reply", ["1"]) and m.notice("r", "not done", ["2"])
    assert asyncio.run(m.send_text("r", "a send"))
    assert asyncio.run(m.notify("r", "a machine line"))                 # anything else: the channel's own, as before
    assert m.silent("r", ["3"], "card") == []
    assert [(x["text"], x["kind"]) for x in p.sheets["r"].lines] == [("the reply", "reply"), ("not done", "notice"), ("a send", "reply")]
    assert '"reply": "the reply"' in capsys.readouterr().out


def page_daemon(tmp_path):
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    t = (private / "rooms.toml").read_text()
    (private / "rooms.toml").write_text(t.replace('[rooms.example-room]\n', '[rooms.example-room]\npage = true\n', 1))
    y = Yerkd(private, state)
    y.add_room("example-room", ConsoleChannel())
    y.add_room("example-quiet", ConsoleChannel())
    return y, state


def test_a_room_with_page_true_gets_its_page_and_the_tool_and_no_other_room_does(tmp_path):
    y, state = page_daemon(tmp_path)
    assert isinstance(y.channels["example-room"], Mirror) and not isinstance(y.channels["example-quiet"], Mirror)
    assert y.rooms["example-room"].channel is y.channels["example-room"]
    assert asyncio.run(y.tool_page("example-room", {"hint": "read line 2"}))["ok"]
    assert asyncio.run(y.tool_page("example-quiet", {"hint": "x"}))["error"] == words.PAGE_NONE.format(room="example-quiet")
    keys = state / "secrets" / "pages"
    key = (keys / "example-room.secret").read_text().strip()
    assert (keys / "example-room.secret").stat().st_mode & 0o077 == 0 and keys.stat().st_mode & 0o077 == 0
    assert not (keys / "example-quiet.secret").exists()
    assert keys in y.workers.launcher.hidden                     # no worker sees a page's key: it would type as the owner
    assert f"Read(/{state.resolve()}/secrets/**)" in y.spec_for("example-room").settings["permissions"]["deny"]   # nor any room
    (state / "yerkd.json").write_text(json.dumps({"port": 4321}))
    assert address(state, "example-room") == f"http://127.0.0.1:4321/page#room=example-room&key={key}"
    with pytest.raises(SystemExit, match="has no live page yet"):
        address(state, "example-quiet")


def test_page_true_outside_the_owner_s_rooms_gives_no_page_no_key_and_no_tool(tmp_path, caplog):
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    t = (private / "rooms.toml").read_text()
    t = t.replace('[rooms.example-quiet]\n', '[rooms.example-quiet]\npage = true\n', 1)
    head, _, rest = t.partition("[rooms.example-quiet]")
    (private / "rooms.toml").write_text(head + "[rooms.example-quiet]" + rest.replace('privacy = "owner"', 'privacy = "family"', 1))
    y = Yerkd(private, state)
    with caplog.at_level("WARNING", logger="yerk.daemon"):
        y.add_room("example-quiet", ConsoleChannel())
    assert "room example-quiet is not an owner room; its page = true is refused" in caplog.text
    assert not isinstance(y.channels["example-quiet"], Mirror) and "example-quiet" not in y.page.sheets
    assert not (state / "secrets" / "pages" / "example-quiet.secret").exists()
    assert asyncio.run(y.tool_page("example-quiet", {"hint": "x", "code": FIXED}))["error"] == words.PAGE_OWNER_ONLY


def test_the_page_opens_with_no_key_and_its_calls_only_with_its_own_room_s_key(tmp_path):
    y, state = page_daemon(tmp_path)
    key = y.page.sheets["example-room"].key
    app = mcp_server.app(mcp_server.build(y, y.port, y.hook, y.control, y.status_now, y.page), y.check())
    with TestClient(app) as c:
        html = c.get("/page")
        assert html.status_code == 200 and "x-yerk-secret" in html.text and key not in html.text and "example-room" not in html.text
        assert c.post("/page/event", json={"kind": "say", "text": "x"}).status_code == 401                    # no key
        assert c.post("/page/event", json={"kind": "say", "text": "x"}, headers={"x-yerk-secret": y.secret}).status_code == 401
        ok = c.post("/page/event", json={"kind": "edit", "pane": "code", "text": FIXED}, headers={"x-yerk-secret": key})
        assert ok.status_code == 200 and ok.json() == {"ok": True}
        ran = c.post("/page/run", json={"tests": TESTS}, headers={"x-yerk-secret": key}).json()
        assert ran["ok"] and ran["passed"] == 2
        for path in ("/control", "/status", "/hook"):                                                       # it opens nothing else
            assert c.post(path, json={}, headers={"x-yerk-secret": key}).status_code == 401
        assert c.post("/page/run", content=b"[]", headers={"x-yerk-secret": key}).status_code == 400
    assert (state / "rooms" / "example-room" / "work" / "page" / "solution.py").read_text() == FIXED


def test_change_finds_the_one_span_a_keystroke_changed():
    assert change("", "a") == {"line": 1, "col": 1, "del": "", "ins": "a"}
    assert change("ab\ncd", "ab\nd") == {"line": 2, "col": 1, "del": "c", "ins": ""}
    assert change("aaa", "aaaa") == {"line": 1, "col": 4, "del": "", "ins": "a"}
    assert change("x = 1\n", "x = 2\n") == {"line": 1, "col": 5, "del": "1", "ins": "2"}


@boxed
def test_a_drawn_program_runs_at_once_and_the_page_s_own_run_of_it_wakes_no_turn(tmp_path):
    y, _ = page_daemon(tmp_path)
    y.page.quiet = 0.05
    taken = []
    y.rooms["example-room"].submit = taken.append                    # no Claude Code here: what would reach the room
    got = asyncio.run(y.tool_page("example-room", {"code": BROKEN, "tests": TESTS, "hint": "one operator"}))
    assert got["ok"] and got["tests"] == "tests: 1 of 2 pass; test_add fails at line 7 of the tests: AssertionError: 0 != 4"

    async def go():
        await y.page.run("example-room", {"code": BROKEN, "tests": TESTS})   # the page runs what it was handed
        await asyncio.sleep(0.2)
        await y.page.run("example-room", {"code": FIXED, "tests": TESTS})    # then the owner fixes it
        await asyncio.sleep(0.2)
    asyncio.run(go())
    assert len(taken) == 1 and "tests: 2 of 2 pass" in taken[0].text


def test_a_stop_ends_every_tab_s_feed(tmp_path):
    p, _ = hub(tmp_path)

    async def go():
        tab = p.stream("r", beat=10)
        await tab.__anext__()
        p.close()
        with pytest.raises(StopAsyncIteration):
            await tab.__anext__()
    asyncio.run(go())
    assert p.sheets["r"].tabs == []


def opener_script(tmp_path, body="printf '%s\\n' \"$@\" > \"$(dirname \"$0\")/opened\""):
    """A stand-in for the box's opener: what it was handed lands in `opened` beside it."""
    (s := tmp_path / "opener").write_text(f"#!/bin/sh\n{body}\n")
    s.chmod(0o700)
    return s


def plain_daemon(tmp_path, family=False):
    """Example rooms with no `page` in rooms.toml; `family`: example-quiet is not an owner room."""
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir(exist_ok=True)
    if family:
        head, _, rest = (private / "rooms.toml").read_text().partition("[rooms.example-quiet]")
        (private / "rooms.toml").write_text(head + "[rooms.example-quiet]" + rest.replace('privacy = "owner"', 'privacy = "family"', 1))
    y = Yerkd(private, state)
    y.add_room("example-room", ConsoleChannel())
    y.add_room("example-quiet", ConsoleChannel())
    return y, private, state


def test_the_owner_s_ask_turns_the_page_on_with_no_restart_and_opens_it_on_the_screen_at_a_one_time_address(tmp_path):
    y, private, state = plain_daemon(tmp_path)
    toml, room = (private / "rooms.toml").read_bytes(), y.rooms["example-room"]
    y.conf["page"] = {"open": [str(opener_script(tmp_path))]}
    (state / "rooms" / "example-room").mkdir(parents=True, exist_ok=True)
    (state / "rooms" / "example-room" / "override.json").write_text('{"effort": "high"}')     # the settings tool's, kept
    cut = []
    room.shutdown, room.respec = (lambda: cut.append("shutdown")), (lambda: cut.append("respec"))   # what a restart would do to the turn
    assert asyncio.run(y.tool_page("example-room", {"hint": "x"}))["error"] == words.PAGE_NONE.format(room="example-room")
    got = asyncio.run(y.tool_page("example-room", {"open": True}))
    assert got["ok"] and got["opened"] and got["line"].startswith(words.PAGE_ON) and got["line"].endswith(words.PAGE_OPENED)
    assert y.rooms["example-room"] is room and cut == []                        # the same room, its process and turn untouched
    assert isinstance(room.channel, Mirror) and room.channel is y.channels["example-room"] and "example-room" in y.page.sheets
    key = (state / "secrets" / "pages" / "example-room.secret").read_text().strip()
    url = (tmp_path / "opened").read_text().strip()                              # what the opener's command line carried
    assert key not in url and url.startswith(f"http://127.0.0.1:{y.port}/page?room=example-room&open=")
    assert (private / "rooms.toml").read_bytes() == toml                         # rooms.toml is never written
    over = state / "rooms" / "example-room" / "override.json"
    assert json.loads(over.read_text()) == {"effort": "high", "page": True} and over.stat().st_mode & 0o077 == 0
    app = mcp_server.app(mcp_server.build(y, y.port, y.hook, y.control, y.status_now, y.page), y.check())
    with TestClient(app) as c:
        first = c.post("/page/open", json={"ticket": url.rpartition("open=")[2]})
        assert first.status_code == 200 and first.json() == {"ok": True, "room": "example-room", "key": key}
        again = c.post("/page/open", json={"ticket": url.rpartition("open=")[2]})
        assert again.status_code == 401 and again.json()["error"] == words.PAGE_TICKET_SPENT     # once only
        assert c.post("/page/event", json={"kind": "say", "text": "hi"}, headers={"x-yerk-secret": key}).json() == {"ok": True}
    assert asyncio.run(y.tool_page("example-room", {"open": True}))["line"].startswith(words.PAGE_WAS_ON)
    y2 = Yerkd(private, state)                                                  # the next start keeps it on
    y2.add_room("example-room", ConsoleChannel())
    assert isinstance(y2.channels["example-room"], Mirror) and y2.page.sheets["example-room"].key == key


def test_the_page_turns_off_with_no_restart_stays_off_and_comes_back_at_the_same_address(tmp_path):
    y, state = page_daemon(tmp_path)
    y.conf["page"] = {"open": [str(opener_script(tmp_path))]}
    key, inner = y.page.sheets["example-room"].key, y.channels["example-room"].inner

    async def off_with_a_tab_open():
        tab = y.page.stream("example-room", beat=10)
        await tab.__anext__()
        got = await y.tool_page("example-room", {"off": True})
        with pytest.raises(StopAsyncIteration):                                 # the open tab's feed ends
            await tab.__anext__()
        return got
    assert asyncio.run(off_with_a_tab_open()) == {"ok": True, "line": words.PAGE_OFF}
    assert y.channels["example-room"] is inner and y.rooms["example-room"].channel is inner and "example-room" not in y.page.sheets
    assert y.page.room_of(key) is None                                          # its key opens nothing now
    y2 = Yerkd(y.private, state)                                                # rooms.toml still says page = true: off wins
    y2.add_room("example-room", ConsoleChannel())
    assert not isinstance(y2.channels["example-room"], Mirror) and "example-room" not in y2.page.sheets
    assert asyncio.run(y.tool_page("example-room", {"open": True}))["ok"] and y.page.sheets["example-room"].key == key
    assert json.loads((state / "rooms" / "example-room" / "override.json").read_text()) == {}   # back to rooms.toml's say


def test_a_ticket_opens_nothing_used_old_or_unknown_and_no_room_but_the_owner_s_switches_a_page(tmp_path, monkeypatch):
    y, _, state = plain_daemon(tmp_path, family=True)
    y.conf["page"] = {"open": [str(opener_script(tmp_path))]}
    assert asyncio.run(y.tool_page("example-quiet", {"open": True})) == {"ok": False, "error": words.PAGE_OWNER_ONLY}
    assert not (tmp_path / "opened").exists() and not (state / "rooms" / "example-quiet" / "override.json").exists()
    assert "example-quiet" not in y.page.sheets
    y.page_switch("example-room", True)
    monkeypatch.setattr(pagemod, "TICKET_SECONDS", -1)                          # a ticket past its minute
    old = y.page.ticket("example-room", y.port).rpartition("open=")[2]
    assert y.page.redeem(old) == {"ok": False, "error": words.PAGE_TICKET_SPENT}
    assert y.page.redeem("") == y.page.redeem("no-such-ticket") == {"ok": False, "error": words.PAGE_TICKET_SPENT}
    monkeypatch.setattr(pagemod, "TICKET_SECONDS", 60)
    live = y.page.ticket("example-room", y.port).rpartition("open=")[2]
    y.page_switch("example-room", False)                                        # the page went off before the tab opened
    assert y.page.redeem(live)["ok"] is False


def test_the_opener_is_the_owner_s_command_else_the_box_s_own_and_says_why_not_in_words(tmp_path, monkeypatch):
    assert pagemod.opener({"page": {"open": ["my-opener", "--new-tab"]}}) == ["my-opener", "--new-tab"]
    monkeypatch.setattr(pagemod.sys, "platform", "linux")
    monkeypatch.setattr(pagemod.os.path, "exists", lambda p: p in ("/proc/sys/fs/binfmt_misc/WSLInterop", pagemod.WSL_OPEN[0]))
    monkeypatch.setattr(pagemod.shutil, "which", lambda name: None)
    assert pagemod.opener({}) == list(pagemod.WSL_OPEN)                         # WSL: Windows' own, the address whole
    monkeypatch.setattr(pagemod.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert pagemod.opener({}) == ["/usr/bin/wslview"]
    monkeypatch.setattr(pagemod.os.path, "exists", lambda p: False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert pagemod.opener({}) == []                                             # a box with no screen
    monkeypatch.setenv("DISPLAY", ":0")
    assert pagemod.opener({}) == ["/usr/bin/xdg-open"]
    url = "http://127.0.0.1:1/page?room=r&open=secret-ticket"
    assert pagemod.open_on_screen([], url) == words.PAGE_NO_SCREEN
    failed = pagemod.open_on_screen([str(opener_script(tmp_path, 'echo "no browser for $1" >&2; exit 3'))], url)
    assert failed == words.PAGE_OPEN_FAILED.format(why="no browser for the address") and "secret-ticket" not in failed
    assert pagemod.open_on_screen([str(tmp_path / "missing")], url) == words.PAGE_OPEN_FAILED.format(why="No such file or directory")
    slow = opener_script(tmp_path, "sleep 5")
    assert pagemod.open_on_screen([str(slow)], url, seconds=0.2) == words.PAGE_OPEN_SLOW.format(n=0)


def test_a_page_turned_on_where_nothing_opens_it_is_on_and_the_room_is_told_the_way_in(tmp_path):
    y, _, _ = plain_daemon(tmp_path)
    y.conf["page"] = {"open": [str(tmp_path / "missing")]}
    got = asyncio.run(y.tool_page("example-room", {"open": True, "hint": "start at line 1"}))
    assert got["ok"] and got["opened"]["ok"] and got["opened"]["opened"] is False and "example-room" in y.page.sheets
    assert "`yerk room page example-room` on the box prints its address" in got["opened"]["line"]
    assert y.page.sheets["example-room"].hint == "start at line 1"              # what it drew in the same call is on the page
