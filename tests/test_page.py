"""Checks a room's live page, the room on a browser tab (channels/page.py): your typing, drawing and test runs there reach the room
as messages, mid-turn too, the tests run when the page asks with no model, and what the room says and draws shows on the page."""
import asyncio
import json
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from yerk_ai import cli, mcp_server, words
from yerk_ai.channels import page as pagemod
from yerk_ai.channels.page import Mirror, Page, address, board, cell, change, comments, plain, rich, run_line, run_tests
from yerk_ai.channels.page import tailnet as real_tailnet     # before conftest's no_tailnet puts its stand-in there
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


def asked(p, room="r"):
    """A message the room's page sent it, ledgered on the page's channel as flush ledgers one: what a line of the room's
    answers to go into the program with the chat closed (Page.answers_page)."""
    mid = f"page-{room}-{time.time_ns() // 1000}"
    p.ledger.inbound_add(mid, room, pagemod.CHANNEL, "owner", 1)
    return [mid]


def styles(css):
    """The page's CSS, each selector's declarations (a rule inside @media or @keyframes read as its own)."""
    out = {}
    for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        for sel in sels.split(","):
            out.setdefault(" ".join(sel.split()), {}).update(
                {k.strip(): v.strip() for k, _, v in (d.partition(":") for d in body.split(";")) if k.strip()})
    return out


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
    assert chan.replies[0][0].startswith("echo: SLOW first + [sent ") and "]\n\n[from the owner's live page]" in chan.replies[0][0]   # the fold's line ahead
    row = ledger.inbound_get(mid)
    assert row["folded"] == 1 and row["state"] == "answered"
    assert [(x["text"], x["kind"]) for x in p.sheets["r"].lines] == [("second", "me"), (chan.replies[0][0], "reply")]   # both, on the page


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


def test_the_bar_s_words_read_as_a_scoreboard_a_rank_badge_an_lp_bar_big_numbers_and_words():
    shapes = {k: {x: v for x, v in c.items() if x not in ("label", "value")} for k, c in
              ((c["label"], c) for c in board({"Rank": "Silver II", "LP": "40", "Streak": "3", "Solved": "12/40", "Last": "+15 LP",
                                               "Miss": "−12", "Done": "50%", "Topic": "two pointers"}))}
    assert shapes == {"Rank": {"shape": "rank", "tier": "silver", "name": "Silver", "division": "II"},
                      "LP": {"shape": "meter", "big": "40", "unit": "/100", "fill": 0.4},
                      "Streak": {"shape": "number", "big": "3", "unit": "", "sign": ""},
                      "Solved": {"shape": "meter", "big": "12", "unit": "/40", "fill": 0.3},
                      "Last": {"shape": "number", "big": "+15", "unit": "LP", "sign": "up"},
                      "Miss": {"shape": "number", "big": "−12", "unit": "", "sign": "down"},
                      "Done": {"shape": "meter", "big": "50", "unit": "%", "fill": 0.5},
                      "Topic": {"shape": "text"}}
    assert [c["label"] for c in board({"b": "1", "a": "2", "1": "3"})] == ["b", "a", "1"]          # the room's order, kept
    assert cell("medal", "gold")["tier"] == "gold" and cell("x", "Diamond 4")["division"] == "4"       # a tier word, any case
    assert cell("tier", "Unranked") == {"label": "tier", "value": "Unranked", "shape": "rank", "tier": "", "name": "Unranked", "division": ""}
    assert cell("goal", "Master recursion")["shape"] == "text"          # a tier word with words after it, outside a rank label
    assert cell("points", "75 LP")["fill"] == 0.75 and cell("LP", "130")["fill"] == 1.0 and cell("x", "0/0")["fill"] == 0.0
    assert cell("left", "3 of 4")["fill"] == 0.75 and cell("score", "1,240")["big"] == "1,240" and cell("clock", "12:30")["shape"] == "text"
    assert {c["tier"] for c in board({t: t.title() for t in pagemod.TIERS})} == set(pagemod.TIERS)


def test_the_room_sets_only_the_words_and_every_tab_gets_the_scoreboard_read_from_them(tmp_path):
    """The wall a room hit: asked to make the bar look better, it could change only its words, the look being the page's.
    The look is now the page's for every room: what the room gives is still labels and values, and the tab gets them read
    as the scoreboard's cells, with a word that is markup kept a word."""
    p, _ = hub(tmp_path)
    bar = {"Rank": "Gold I", "LP": "75", "Streak": "4", "Note": '<b style="color:red">big</b>'}

    async def go():
        tab = p.stream("r", beat=0.05)
        await tab.__anext__()
        assert p.draw("r", {"bar": bar})["ok"]
        drawn = json.loads(await tab.__anext__())
        await tab.aclose()
        return drawn["draw"], json.loads(await p.stream("r").__anext__())["state"]
    drawn, late = asyncio.run(go())
    assert drawn["bar"] == late["bar"] == bar and drawn["board"] == late["board"] == board(bar)
    assert [c["shape"] for c in drawn["board"]] == ["rank", "meter", "number", "text"]
    assert drawn["board"][0]["tier"] == "gold" and drawn["board"][1]["fill"] == 0.75
    assert drawn["board"][3]["value"] == bar["Note"]                              # words, as the room wrote them
    logged = [e for e in events(tmp_path) if e["kind"] == "drawn"][-1]
    assert logged["bar"] == bar and "board" not in logged                         # the file keeps the room's words


def test_a_line_said_reads_the_way_discord_reads_it_bold_gaps_links_code_lists_and_words_kept_words():
    reply = ("**Close.** The sign is off.\n\n\nSee https://docs.example.org/a_b_c. or [the guide](https://example.org/g) and `x - y`.\n"
             "```python\nreturn a + b\n```\n# Next\n- first *one*\n  - under it\n2. then __this__ ~~not~~ ||later||\n> quoted\n-# small print\n"
             "<b>kept</b> \\*not italic\\* [bad](javascript:alert(1)) snake_case_name ***both***")
    assert rich(reply) == [
        {"kind": "p", "runs": [{"text": "Close.", "looks": "b"}, {"text": " The sign is off.", "looks": ""}]},
        {"kind": "gap"},                                                            # blank lines between thoughts: one gap
        {"kind": "p", "runs": [{"text": "See ", "looks": ""}, {"text": "https://docs.example.org/a_b_c", "looks": "", "href": "https://docs.example.org/a_b_c"},
                               {"text": ". or ", "looks": ""}, {"text": "the guide", "looks": "", "href": "https://example.org/g"},
                               {"text": " and ", "looks": ""}, {"text": "x - y", "looks": "c"}, {"text": ".", "looks": ""}]},
        {"kind": "code", "lang": "python", "text": "return a + b"},
        {"kind": "h1", "runs": [{"text": "Next", "looks": ""}]},
        {"kind": "li", "mark": "•", "depth": 0, "runs": [{"text": "first ", "looks": ""}, {"text": "one", "looks": "i"}]},
        {"kind": "li", "mark": "•", "depth": 1, "runs": [{"text": "under it", "looks": ""}]},
        {"kind": "li", "mark": "2.", "depth": 0, "runs": [{"text": "then ", "looks": ""}, {"text": "this", "looks": "u"}, {"text": " ", "looks": ""},
                                                         {"text": "not", "looks": "s"}, {"text": " ", "looks": ""}, {"text": "later", "looks": "h"}]},
        {"kind": "quote", "runs": [{"text": "quoted", "looks": ""}]},
        {"kind": "sub", "runs": [{"text": "small print", "looks": ""}]},
        {"kind": "p", "runs": [{"text": "<b>kept</b> *not italic* [bad](javascript:alert(1)) snake_case_name ", "looks": ""},
                               {"text": "both", "looks": "bi"}]}]
    assert rich("**bold *both* bold**")[0]["runs"] == [{"text": "bold ", "looks": "b"}, {"text": "both", "looks": "bi"}, {"text": " bold", "looks": "b"}]
    assert rich(">>> all of it\n- still quoted") == [{"kind": "quote", "runs": [{"text": "all of it", "looks": ""}]},
                                                     {"kind": "quote", "runs": [{"text": "- still quoted", "looks": ""}]}]
    assert rich("```one line```") == [{"kind": "code", "lang": "", "text": "one line"}] and rich("") == []
    assert rich("a ``` never closed")[0]["runs"][0]["text"] == "a ``` never closed"
    assert all("href" not in r or r["href"].startswith(("http://", "https://")) for b in rich(reply) for r in b.get("runs", []))


def test_a_long_line_said_is_read_in_step_with_its_length_and_once_so_it_never_holds_the_owner_s_rooms(tmp_path, monkeypatch):
    """The wall the cold read hit: a mark never closed was read to the end of its row, and each one again, so one row of
    100,000 characters of `*a ` took 27 s on yerkd's only loop, every room's hooks and tools waiting, and again for all
    200 kept lines each time a tab opened. Now a mark is read only up to its own next one, and a line once."""
    took = time.perf_counter()
    for row in ("**a ", "***a ", " _a", "__a ", "~~a ", "[a"):
        rich(row * (100_000 // len(row)))
    assert rich(line := "*a " * 33_334) == [{"kind": "p", "runs": [{"text": line, "looks": ""}]}]   # never closed: words
    assert time.perf_counter() - took < 1
    assert rich("**a** and **b**")[0]["runs"] == [{"text": "a", "looks": "b"}, {"text": " and ", "looks": ""}, {"text": "b", "looks": "b"}]
    assert rich("~~a~~ _b_ __c__")[0]["runs"] == [{"text": "a", "looks": "s"}, {"text": " ", "looks": ""}, {"text": "b", "looks": "i"},
                                                  {"text": " ", "looks": ""}, {"text": "c", "looks": "u"}]   # one word each, never one run
    assert rich("**a **b**")[0]["runs"] == [{"text": "**a ", "looks": ""}, {"text": "b", "looks": "b"}]
    chat = tmp_path / "rooms" / "r" / "work" / "page" / pagemod.CHAT
    chat.parent.mkdir(parents=True)
    chat.write_text(json.dumps([{"text": "hi", "kind": "reply", "n": 0, "parts": [{"kind": "p", "runs": [{"text": "x", "href": "javascript:x"}]}]}]))
    p, _ = hub(tmp_path, quiet=60)
    assert p.sheets["r"].state()["state"]["lines"][0]["parts"] == rich("hi")                 # parts in the file are never read
    read: list[str] = []
    monkeypatch.setattr(pagemod, "rich", lambda text: read.append(text) or [])
    p.said("r", "x" * (pagemod.TEXT_MAX + 1))
    p.said("r", "two")
    for _ in range(3):                                                                       # three tabs open
        p.sheets["r"].state()
    assert read == ["x" * pagemod.TEXT_MAX, "two"]                                           # each line once, when said
    assert all("parts" not in x for x in json.loads(chat.read_text()))
    again = Page(tmp_path, p.ledger)
    again.add_room(Taken())
    for _ in range(3):
        again.sheets["r"].state()
    assert read[2:] == ["hi", "x" * pagemod.TEXT_MAX, "two"]                                 # after a restart, once, for the first tab


def test_the_chat_keeps_both_sides_numbered_time_stamped_and_read_and_comes_back_after_a_restart(tmp_path):
    """The wall the ask hit: the talk was a footer strip of plain text, the owner's own lines only in the tab that typed
    them. Now a line said on the page is the chat's on every tab, beside the room's, each with its time and its parts."""
    p, room = hub(tmp_path, quiet=60)

    async def go():
        tab = p.stream("r", beat=0.05)
        await tab.__anext__()
        await p.event("r", {"kind": "say", "text": "  why is **this** red?\nline two  "})
        mine = json.loads(await tab.__anext__())
        assert json.loads(await tab.__anext__()) == {"typing": {"pane": "chat", "line": 0}}   # the room on it, on the chat
        p.said("r", "**Close.**\n\nLook at https://example.org/x")
        theirs = json.loads(await tab.__anext__())
        await tab.aclose()
        return mine["line"], theirs["line"]
    mine, theirs = asyncio.run(go())
    assert room.got[0].text.splitlines()[1:] == ["said on the page: why is **this** red?", "line two"]
    assert {k: mine[k] for k in ("text", "kind", "n")} == {"text": "why is **this** red?\nline two", "kind": "me", "n": 0}
    assert mine["parts"][0]["runs"][1] == {"text": "this", "looks": "b"} and time.time() - mine["t"] < 60
    assert (theirs["kind"], theirs["n"]) == ("reply", 1) and [b["kind"] for b in theirs["parts"]] == ["p", "gap", "p"]
    assert theirs["parts"][2]["runs"][1]["href"] == "https://example.org/x"
    again = Page(tmp_path, p.ledger)
    again.add_room(Taken())
    late = again.sheets["r"].state()["state"]["lines"]
    assert [(x["kind"], x["n"]) for x in late] == [("me", 0), ("reply", 1)] and late[1]["parts"] == theirs["parts"]
    again.said("r", "after the restart")
    assert again.sheets["r"].lines[-1]["n"] == 2
    assert all("parts" not in x for x in json.loads((tmp_path / "rooms" / "r" / "work" / "page" / pagemod.CHAT).read_text()))
    for i in range(pagemod.LINES_KEPT + 5):
        again.said("r", f"line {i}")
    assert len(again.sheets["r"].lines) == pagemod.LINES_KEPT and again.sheets["r"].lines[-1]["text"] == f"line {pagemod.LINES_KEPT + 4}"


def test_a_comment_typed_in_the_program_is_the_owner_s_talk_on_the_chat_and_said_to_the_room(tmp_path):
    p, room = hub(tmp_path, quiet=60)
    p.draw("r", {"code": "# the room's own note\ndef add(a, b):\n    return a - b\n"})

    async def go():
        await p.event("r", {"kind": "edit", "pane": "code", "text": "# the room's own note\n# can you\ndef add(a, b):\n    return a - b\n"})
        p.flush("r")
        await p.event("r", {"kind": "edit", "pane": "code",
                            "text": "# the room's own note\n# can you explain\n# the minus?\ndef add(a, b):\n    return a - b  # here\n"})
        p.flush("r")
    asyncio.run(go())
    first, second = (m.text.splitlines() for m in room.got)
    assert "said in solution.py at line 2, as a # comment: can you" in first and "+# can you" in first
    assert second[-2:] == ["said in solution.py at line 2, as a # comment: can you explain", "the minus?"]
    lines = p.sheets["r"].lines
    assert [(x["text"], x["kind"], x["at"], x["n"]) for x in lines] == [("can you explain\nthe minus?", "me", 2, 0)]   # the same line, grown
    assert comments("x = 1\n# had\n", "#!/usr/bin/env python3\nx = 1  # trailing\n# had\n#\n  # an indented one\n") == [(5, "an indented one")]
    assert comments("", "# one\n# two\nx = 1\n# three\n") == [(1, "one\ntwo"), (4, "three")]


def test_a_program_of_repeated_lines_costs_the_owner_s_message_time_in_step_with_its_length_so_it_never_holds_the_rooms(tmp_path):
    """The wall the cold read hit: the comments were found by a diff (SequenceMatcher, autojunk off) over the whole program at
    every message, even with the program unchanged, the square of its lines when they repeat: a line said beside 8,000 blank
    lines took 5 s on yerkd's only loop, every room's hooks and tools waiting. Now a line the program had is never talk, so
    none is diffed, and an unchanged program is not read at all. And a paste of 25,000 comments was 25,000 chat lines and
    said lines, 10 s and a 1.3 MB message: now DIFF_LINES of them are said, the rest counted."""
    p, room = hub(tmp_path, quiet=60)
    p.draw("r", {"code": "\n" * 8000})

    async def go():
        took = time.perf_counter()
        await p.event("r", {"kind": "say", "text": "one"})                                 # the program unchanged
        await p.event("r", {"kind": "edit", "pane": "code", "text": "\n" * 4000 + "# two\n" + "\n" * 4000})
        p.flush("r")
        return time.perf_counter() - took
    assert asyncio.run(go()) < 1
    assert [m.text.splitlines()[-1] for m in room.got] == ["said on the page: one", "said in solution.py at line 4001, as a # comment: two"]
    assert [(x["text"], x.get("at")) for x in p.sheets["r"].lines] == [("one", None), ("two", 4001)]
    took, blank, n = time.perf_counter(), "\n" * pagemod.TEXT_MAX, pagemod.TEXT_MAX // 4
    assert comments(blank, blank) == [] and comments(blank + "x\n", "y\n" + blank) == []
    assert comments(blank, blank[:50_000] + "# three\n" + blank[50_000:]) == [(50_001, "three")]
    assert comments("", "#a\n\n" * n) == [(2 * i + 1, "a") for i in range(n)]               # a comment each
    assert comments("x\n", "x\n" + "#a\n" * n) == [(2, "\n".join(["a"] * n))]                 # one comment, grown
    assert time.perf_counter() - took < 1

    async def paste():                    # a comment each: DIFF_LINES of them said and on the chat, the rest counted
        await p.event("r", {"kind": "edit", "pane": "code", "text": "#a\n\n" * n})
        took = time.perf_counter()
        p.flush("r")
        return time.perf_counter() - took
    assert asyncio.run(paste()) < 1
    said = room.got[-1].text.splitlines()
    assert said.count("said in solution.py at line 1, as a # comment: a") == 1 and len(room.got[-1].text) < 10_000
    assert sum(x.startswith("said in solution.py at line") for x in said) == pagemod.DIFF_LINES
    assert said[-1] == words.PAGE_COMMENT_CUT.format(n=n - pagemod.DIFF_LINES, name="solution.py")
    assert [x.get("at") for x in p.sheets["r"].lines[2:]] == [2 * i + 1 for i in range(pagemod.DIFF_LINES)]


def test_the_look_is_the_page_s_own_a_scoreboard_a_clock_tiles_that_pop_a_hint_card_and_a_chat_column_dark():
    html = pagemod.HTML.read_text()
    css, body = html.split("</style>", 1)
    assert "color-scheme: dark" in css and "—" not in html
    assert all(f".tier-{t} {{" in css for t in pagemod.TIERS)                 # each tier the board reads has its colour
    for rule in (".cell.rank", ".badge", ".cell.meter", ".track i", ".cell.number.up", ".cell.number.down", ".cell .big", ".cell .value",
                 "#clock.low", "#clock.crit", ".tile.ok", ".tile.bad", ".tile.pop", "@keyframes pop", "#hint {", "#chat {", "#lines .me"):
        assert rule in css, rule
    code = body.split('data-pane="code"', 1)[1].split("</section>", 1)[0]
    assert 'id="hint"' in code and 'id="lines"' in body.split('id="chat"', 1)[1]    # the hint under the program; the lines a column
    assert 'id="clock"' in body.split('id="bar"', 1)[1].split("</header>", 1)[0]
    script = body.split("<script>", 1)[1]
    assert not [w for w in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write") if w in script]   # a word stays a word
    assert "state.board" in script and "c.tier" in script and "passedBefore" in script


def test_the_chat_is_a_pane_of_its_own_beside_the_code_wide_either_way_with_a_say_box_of_many_lines():
    """The wall the ask hit: the talk was a footer under the program, plain text over a one-line input. Now it is a
    column of its own: a grip and two buttons set how wide, its lines drawn from yerkd's parts, its box many lines."""
    html = pagemod.HTML.read_text()
    css, body = html.split("</style>", 1)
    script = body.split("<script>", 1)[1]
    assert '"main grip chat"' in css and "var(--chat, 38%)" in css and "#grip {" in css and "main.narrow" in css
    for rule in (".body strong", ".body a {", ".body pre {", ".body code", ".body .gap", ".body .quote", ".body .li", ".body .spoiler", ".who time"):
        assert rule in css, rule
    at = body.index('id="grip"')
    assert body.index("</main>") < at < body.index('id="chat"')                     # between the program and the chat
    chat = body.split('id="chat"', 1)[1].split("</aside>", 1)[0]
    assert 'id="wide-chat"' in chat and 'id="wide-code"' in chat and '<textarea id="say"' in chat and "<input" not in chat
    assert "e.shiftKey" in script and "say.focus()" in script and "localStorage" in script and "setPointerCapture" in script
    assert "l.parts" in script and 'a.rel = "noopener noreferrer"' in script and "/^https?:" in script and "toLocaleTimeString" in script
    assert 'line({ text: e.target.value, kind: "me" })' not in script                # the owner's line comes from yerkd, on every tab


def test_the_bar_s_cells_are_spaced_as_cells_each_its_own_box_in_a_row_with_its_own_gap():
    """need-2026-11-03-47, the wall a room hit: the bar's cells ran into each other, every one put inside one span, so the
    bar's gap never fell between them and only a margin on the value kept a label off its value; the room padded the
    values with no-break spaces. The page spaces them itself, for every room: each cell its own box in a row with its own
    gap, wrapping on a narrow screen, its label over its value."""
    css, body = pagemod.HTML.read_text().split("<style>", 1)[1].split("</style>", 1)
    row, box = styles(css)["#cells"], styles(css)[".cell"]
    assert row["display"] == "flex" and row["flex-wrap"] == "wrap" and int(row["gap"].removesuffix("px")) >= 8
    assert box["display"] == "flex" and box["flex-direction"] == "column" and int(box["gap"].removesuffix("px")) > 0
    assert int(box["padding"].split()[-1].removesuffix("px")) > 0 and box["border"].startswith("1px solid")   # a box
    assert '<div id="cells"></div>' in body.split('id="bar"', 1)[1].split("</header>", 1)[0]      # a row of its own
    bar = body.split("function showBar()", 1)[1].split("\nfunction ", 1)[0]
    assert 'make("div", "cell "' in bar and "cells.appendChild(d)" in bar         # each cell its own element in the row
    assert 'make("span", "label", c.label)' in bar and 'make("span", "value", c.value)' in bar


def test_padding_in_the_bar_s_words_is_dropped_so_a_padded_bar_reads_and_shows_as_the_plain_one(tmp_path):
    """need-2026-11-03-47: what the room did anyway, no-break spaces after the values, is no way now: yerkd keeps the bar's
    words alone, so a padded bar reads and shows as the plain one (no blank in a cell, and a tier word kept from its
    division by a no-break space reads as its rank again), a cell of padding alone, a spacer, goes and leaves its place to
    a cell with words, padding before the words takes none of the length cap, and the tool's answer says the page spaces
    the cells itself."""
    nb = "\u00a0"
    assert plain(f"\u3000{nb}Gold{nb}{nb}II\u2003\t\u200b") == "Gold II" and plain("\u2800two  pointers\u2800") == "two pointers"
    clean = {"Rank": "Gold II", "LP": "75", "Solved": "12/40", "Topic": "two pointers"}
    padded = {f"Rank{nb}{nb}": f"Gold{nb}II{nb * 3}", "LP": f"{nb * 2}75{nb * 3}", nb: nb * 4,
              "Solved": "12/40\u2003\u200b", "\u3000Topic\t": "\u2800two  pointers\u2800"}
    p, _ = hub(tmp_path)

    async def go():
        tab = p.stream("r", beat=0.05)
        await tab.__anext__()
        got = p.draw("r", {"bar": padded})
        drawn = json.loads(await tab.__anext__())["draw"]
        await tab.aclose()
        return got, drawn, json.loads(await p.stream("r").__anext__())["state"]
    got, drawn, late = asyncio.run(go())
    assert got == {"ok": True, "tabs": 1, "note": words.PAGE_UNPADDED}
    assert drawn["bar"] == late["bar"] == clean and drawn["board"] == late["board"] == board(clean)
    assert drawn["board"][0] == {"label": "Rank", "value": "Gold II", "shape": "rank", "tier": "gold", "name": "Gold", "division": "II"}
    assert [e for e in events(tmp_path) if e["kind"] == "drawn"][-1]["bar"] == clean
    assert p.draw("r", {"bar": clean}) == {"ok": True, "tabs": 0, "note": words.PAGE_NO_TAB}      # no padding, no word on it
    assert p.draw("r", {"bar": {"Rank": nb * 50 + "Silver I"}})["note"] == f"{words.PAGE_NO_TAB}; {words.PAGE_UNPADDED}"
    assert p.sheets["r"].bar == {"Rank": "Silver I"}                          # the cap counts the words, not the padding
    p.draw("r", {"bar": {k: v for i in range(8) for k, v in ((f"c{i}", str(i)), (nb * (i + 1), nb))}})
    assert p.sheets["r"].bar == {f"c{i}": str(i) for i in range(8)}        # a spacer takes none of the 8 cells


@pytest.mark.skipif(not shutil.which("node"), reason="node checks the page's script; it is not on this box")
def test_the_page_s_script_parses(tmp_path):
    (js := tmp_path / "page.js").write_text(pagemod.HTML.read_text().split("<script>", 1)[1].split("</script>", 1)[0])
    done = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


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
    cut, taken = [], []
    room.shutdown, room.respec = (lambda: cut.append("shutdown")), (lambda: cut.append("respec"))   # what a restart would do to the turn
    room.submit = taken.append              # no Claude Code here: a real turn outlived the test and printed into a later one's capsys
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
    assert len(taken) == 1 and words.PAGE_SAID.format(text="hi") in taken[0].text
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


def mac_box(tmp_path, monkeypatch, ssh_body='shift 6\nexec "$@"'):
    """A stand-in for the Mac: `ssh` first on PATH writes what its command line carried to `ssh-args` and its
    stdin goes on to the command after the host (/bin/bash -s, as the Mac runs it), where `open` writes what it was handed
    to `mac-opened`; `ssh_body` replaces the run of that command (a Mac that does not answer)."""
    bin_ = tmp_path / "mac-bin"
    bin_.mkdir(exist_ok=True)
    (ssh := bin_ / "ssh").write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{tmp_path}/ssh-args"\n{ssh_body}\n')
    (mac_open := bin_ / "open").write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{tmp_path}/mac-opened"\n')
    ssh.chmod(0o700)
    mac_open.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_}:{pagemod.os.environ['PATH']}")
    return tmp_path / "ssh-args", tmp_path / "mac-opened"


def test_the_owner_on_the_mac_gets_the_page_there_by_ssh_its_one_time_address_on_no_command_line(tmp_path, monkeypatch):
    """need-2026-11-03-49, the wall a room hit: the owner, on the Mac, asked for the page, and the open put it in the box's
    own browser on the PC in the other room, since the box's opener was the only way. `where` mac opens it on the Mac:
    ssh to it runs `open` on the address as the Mac reaches yerkd, the one-time ticket handed on ssh's stdin, so neither
    the key nor the ticket is on any command line, and the PC's opener is never run."""
    y, _, state = plain_daemon(tmp_path)
    args, opened = mac_box(tmp_path, monkeypatch)
    y.conf["page"] = {"open": [str(opener_script(tmp_path))], "mac": "owner-mac", "address": "box.example.ts.net:8787"}
    got = asyncio.run(y.tool_page("example-room", {"open": True, "where": "mac"}))
    assert got == {"ok": True, "opened": True, "line": f"{words.PAGE_ON}; {words.PAGE_OPENED_MAC}"}
    assert not (tmp_path / "opened").exists()                                    # the PC's opener never ran
    key = (state / "secrets" / "pages" / "example-room.secret").read_text().strip()
    assert args.read_text().splitlines() == [*pagemod.MAC_SSH[1:], "owner-mac", "/bin/bash", "-s"]   # nothing secret on it
    url = opened.read_text().strip()                                             # what `open` on the Mac was handed, whole
    assert url.startswith("http://box.example.ts.net:8787/page?room=example-room&open=") and key not in url
    app = mcp_server.app(mcp_server.build(y, y.port, y.hook, y.control, y.status_now, y.page), y.check())
    with TestClient(app) as c:
        first = c.post("/page/open", json={"ticket": url.rpartition("open=")[2]})
        assert first.status_code == 200 and first.json() == {"ok": True, "room": "example-room", "key": key}
        assert c.post("/page/open", json={"ticket": url.rpartition("open=")[2]}).status_code == 401     # once only
    y.conf["page"] = {"open": [str(opener_script(tmp_path))], "address": "https://box.example.ts.net:9443/"}
    y.conf["imessage"] = {"host": "texts-mac"}                                   # no [page] mac: the text tool's Mac
    got = asyncio.run(y.tool_page("example-room", {"where": "MAC", "hint": "line 3"}))      # a `where` alone opens too
    assert got["opened"]["line"] == f"{words.PAGE_WAS_ON}; {words.PAGE_OPENED_MAC}" and got["ok"] and got["tabs"] == 0
    assert "texts-mac" in args.read_text().splitlines() and opened.read_text().startswith("https://box.example.ts.net:9443/page?room=")
    assert asyncio.run(y.tool_page("example-room", {"open": True, "where": "pc"}))["line"].endswith(words.PAGE_OPENED)
    assert (tmp_path / "opened").read_text().startswith(f"http://127.0.0.1:{y.port}/page?room=example-room&open=")


def test_an_open_on_the_mac_says_in_words_what_it_lacks_and_mints_no_ticket_it_cannot_send(tmp_path, monkeypatch):
    y, _, _ = plain_daemon(tmp_path)
    args, _ = mac_box(tmp_path, monkeypatch, ssh_body='echo "ssh: connect to host owner-mac port 22: Operation timed out" >&2\nexit 255')
    assert asyncio.run(y.tool_page("example-room", {"open": True, "where": "phone"})) == {
        "ok": False, "error": words.PAGE_WHERE.format(where="phone")}
    assert "example-room" not in y.page.sheets                                   # a `where` it does not know switches nothing on
    got = asyncio.run(y.tool_page("example-room", {"open": True, "where": "mac"}))
    assert got == {"ok": True, "opened": False, "line": f"{words.PAGE_ON}; {words.PAGE_NOT_OPENED_MAC.format(why=words.PAGE_NO_MAC)}"}
    assert y.page.tickets == {} and not args.exists()                            # no Mac named: no ticket, no ssh
    y.conf["imessage"] = {"host": "owner-mac"}
    assert asyncio.run(y.tool_page("example-room", {"where": "mac"}))["line"].endswith(words.PAGE_NOT_OPENED_MAC.format(why=words.PAGE_NO_ADDRESS))
    y.conf["page"] = {"address": "box.example.ts.net"}
    got = asyncio.run(y.tool_page("example-room", {"where": "mac"}))
    why = words.PAGE_MAC_SSH.format(why="ssh: connect to host owner-mac port 22: Operation timed out")
    assert got["line"] == f"{words.PAGE_WAS_ON}; {words.PAGE_NOT_OPENED_MAC.format(why=why)}" and "open=" not in got["line"]
    for said in ("ftp://box.example.ts.net", "http://box.example.ts.net:port", "http://box.example.ts.net/yerk", "http://", " "):
        assert pagemod.mac({"page": {"mac": "m", "address": said}}, 1) == words.PAGE_NO_ADDRESS, said
    assert pagemod.mac({"page": {"mac": "m", "address": "box.example.ts.net"}, "imessage": {"host": "other"}}, 4321) == (
        [*pagemod.MAC_SSH, "m", "/bin/bash", "-s"], "http://box.example.ts.net:4321")    # [page] mac first; yerkd's port
    assert pagemod.mac({"page": {"address": "100.64.0.1:8787"}}, 1) == words.PAGE_NO_MAC


def add_on(tmp_path, ssh="owner@owner-mac", mode=0o644, name="addon", **more):
    """An add-on's folder as the hands plug-in keeps it: its server's script and, beside it, the Mac its mac tool reaches
    (`more`: that file's other keys, the Mac's texts bridge)."""
    (folder := tmp_path / name).mkdir(exist_ok=True)
    (folder / "server.mjs").write_text("// an add-on's MCP server\n")
    (f := folder / pagemod.MAC_FILE).write_text(json.dumps({"ssh": ssh, **more}))
    f.chmod(mode)
    return {"type": "stdio", "command": "/usr/bin/node", "args": [str(folder / "server.mjs")], "env": {"YERK_ROOM": "example-room"}}


def test_the_mac_s_open_works_with_nothing_set_in_yerkd_toml_the_add_on_s_mac_and_the_box_s_tailnet(tmp_path, monkeypatch):
    """need-2026-11-04-50, the wall a room hit: asked for the page on the Mac, it drew the page and the open came back
    "no Mac is set up for the page", then would have said the Mac had no address, though the box reached the Mac by ssh for
    the hands plug-in's mac tool and sat on the tailnet; a room cannot set yerkd.toml mid-conversation. With neither key set,
    the Mac is the one an owner room's add-on names in its mac.json and the address is the box's own tailnet one, so one
    deploy lands the page on the Mac; no family room's add-on is read, and a box on no tailnet still says so."""
    y, _, state = plain_daemon(tmp_path)
    args, opened = mac_box(tmp_path, monkeypatch)
    y.conf.pop("page", None)
    y.conf.pop("imessage", None)
    got = asyncio.run(y.tool_page("example-room", {"open": True, "where": "mac"}))
    assert got["line"] == f"{words.PAGE_ON}; {words.PAGE_NOT_OPENED_MAC.format(why=words.PAGE_NO_MAC)}" and not args.exists()
    y.room_confs["example-quiet"]["privacy"] = "family"
    y.room_confs["example-quiet"]["mcp"] = {"hands": add_on(tmp_path, "family@family-mac", name="family-addon")}
    no_mac = words.PAGE_NOT_OPENED_MAC.format(why=words.PAGE_NO_MAC)
    assert asyncio.run(y.tool_page("example-room", {"where": "mac"}))["line"].endswith(no_mac)     # an owner room's add-on only
    y.room_confs["example-room"]["mcp"] = {"hands": add_on(tmp_path)}
    got = asyncio.run(y.tool_page("example-room", {"where": "mac"}))
    assert got["line"].endswith(words.PAGE_NOT_OPENED_MAC.format(why=words.PAGE_NO_ADDRESS)) and y.page.tickets == {}   # no tailnet
    monkeypatch.setattr(pagemod, "tailnet", lambda: "100.64.0.9")
    got = asyncio.run(y.tool_page("example-room", {"where": "mac"}))
    assert got == {"ok": True, "opened": True, "line": f"{words.PAGE_WAS_ON}; {words.PAGE_OPENED_MAC}"}
    assert args.read_text().splitlines() == [*pagemod.MAC_SSH[1:], "owner@owner-mac", "/bin/bash", "-s"]
    key = (state / "secrets" / "pages" / "example-room.secret").read_text().strip()
    url = opened.read_text().strip()
    assert url.startswith(f"http://100.64.0.9:{y.port}/page?room=example-room&open=") and key not in url and key not in args.read_text()
    y.write_status("discord")
    assert json.loads((state / "yerkd.json").read_text())["page"] == f"http://100.64.0.9:{y.port}"      # `yerk room page` prints it
    y.conf["page"] = {"mac": "owner-mac", "address": "box.example.ts.net"}     # either key set wins, as before
    assert asyncio.run(y.tool_page("example-room", {"where": "mac"}))["line"].endswith(words.PAGE_OPENED_MAC)
    assert "owner-mac" in args.read_text().splitlines() and opened.read_text().startswith(f"http://box.example.ts.net:{y.port}/page?")


def test_the_page_is_on_the_box_s_own_tailnet_with_no_page_listen_and_loopback_alone_when_listen_says_so(tmp_path, monkeypatch):
    """With [page] listen unset the page's routes are on the box's own tailnet address too (as `tailscale ip -4` prints it,
    asked once a run), never a LAN address unasked, which would carry the page plain; listen naming an address wins, and
    listen naming loopback keeps the page on loopback alone."""
    assert pagemod.listen({}) == "" and pagemod.reach({}, 4321) == ""                # a box on no tailnet: loopback alone, as ever
    monkeypatch.setattr(pagemod, "tailnet", lambda: "100.64.0.9")
    for said, want in ((None, "100.64.0.9"), ("", "100.64.0.9"), ("127.0.0.1", ""), ("localhost", ""), ("::1", ""),
                       ("192.168.1.20", "192.168.1.20")):
        assert pagemod.listen({"page": {"listen": said}} if said is not None else {}) == want, said
    assert pagemod.reach({}, 4321) == "http://100.64.0.9:4321"
    assert pagemod.reach({"page": {"address": "box.example.ts.net"}}, 4321) == "http://box.example.ts.net:4321"
    lent, _ = pagemod.bind("127.0.0.1", 0)
    port = lent.getsockname()[1]
    lent.close()
    socks = pagemod.sockets({}, port)
    try:
        assert [s.getsockname()[:2] for s in socks] == [("127.0.0.1", port), ("100.64.0.9", port)]
    finally:
        for s in socks:
            s.close()
    assert pagemod.sockets({"page": {"listen": "127.0.0.1"}}, port) == []
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (ts := bin_ / "tailscale").write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/asked"\ncat "{tmp_path}/says"\n')
    ts.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    for says, want in (("100.64.0.9\n", "100.64.0.9"), ("192.168.1.20\n", ""), ("", ""), ("not an address\n", "")):
        (tmp_path / "says").write_text(says)
        assert real_tailnet.__wrapped__() == want, says
    assert (tmp_path / "asked").read_text().splitlines() == ["ip -4"] * 4
    (tmp_path / "says").write_text("100.64.0.9\n")
    real_tailnet.cache_clear()
    try:
        assert real_tailnet() == real_tailnet() == "100.64.0.9" and len((tmp_path / "asked").read_text().splitlines()) == 5   # once a run
    finally:
        real_tailnet.cache_clear()
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert real_tailnet.__wrapped__() == ""                                     # no tailscale on the box


def test_an_add_on_s_mac_is_taken_only_from_a_file_of_the_owner_s_no_one_else_writes_and_only_as_a_host(tmp_path, monkeypatch):
    """The Mac an add-on names carries the one-time address on ssh's stdin, so only a mac.json of the owner's that no other
    user may write and none under Yerk's state folder, where each room's folder is, is read, and only a host or user@host,
    never an option; yerkd.toml's keys win over it."""
    state = tmp_path / "state"
    assert pagemod.addon_mac([add_on(tmp_path)], state) == "owner@owner-mac"
    assert pagemod.addon_mac([{"command": str(tmp_path / "addon" / "server.mjs")}]) == "owner@owner-mac"      # the script as command
    assert pagemod.addon_mac(["not a table", {"command": "node", "args": "server.mjs"}, {"args": [3]}, {}]) == ""
    assert pagemod.addon_mac([{"command": "/usr/bin/node"}, add_on(tmp_path, "second@second-mac", name="b"), add_on(tmp_path)]) == "second@second-mac"
    for ssh in ("-oProxyCommand=touch x", "owner@owner-mac -p 2", "", "owner@", "@owner-mac", "owner@owner;mac"):
        assert pagemod.addon_mac([add_on(tmp_path, ssh)]) == "", ssh
    assert pagemod.addon_mac([add_on(tmp_path, mode=0o646)]) == ""              # any user may write it
    assert pagemod.addon_mac([add_on(tmp_path, ssh="mac")]) == "mac"            # an ssh config's own name for it
    (state / "rooms" / "example-room" / "work").mkdir(parents=True)
    under = add_on(state / "rooms" / "example-room" / "work")                    # a room could write this one
    assert pagemod.addon_mac([under], state) == "" and pagemod.addon_mac([under]) == "owner@owner-mac"
    (tmp_path / "addon" / pagemod.MAC_FILE).write_text("[not, json")
    assert pagemod.addon_mac([{"command": str(tmp_path / "addon" / "server.mjs")}]) == ""
    shared = add_on(tmp_path, mode=0o664)
    me = pagemod.pwd.getpwuid(pagemod.os.getuid()).pw_name
    monkeypatch.setattr(pagemod.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name=me, gr_mem=[]))
    assert pagemod.addon_mac([shared]) == "owner@owner-mac"                  # the owner's own group: only the owner writes it
    monkeypatch.setattr(pagemod.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name=me, gr_mem=["someone-else"]))
    assert pagemod.addon_mac([shared]) == ""
    monkeypatch.setattr(pagemod.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name="staff", gr_mem=[]))
    assert pagemod.addon_mac([shared]) == ""
    servers = [add_on(tmp_path)]
    assert pagemod.mac({"page": {"mac": "owner-mac", "listen": "100.64.0.1"}}, 1, servers)[0][-3] == "owner-mac"
    assert pagemod.mac({"imessage": {"host": "texts-mac"}, "page": {"listen": "100.64.0.1"}}, 1, servers)[0][-3] == "texts-mac"
    assert pagemod.mac({"page": {"listen": "100.64.0.1"}}, 1, servers) == ([*pagemod.MAC_SSH, "owner@owner-mac", "/bin/bash", "-s"], "http://100.64.0.1:1")


def page_head(**given):
    """The page script's first lines, before it touches the document, run by node with a tab's address and its session
    storage: what it read (its room, key and ticket), what its address bar was left as, and the storage after."""
    script = pagemod.HTML.read_text().split("<script>", 1)[1].split("document.title", 1)[0]
    harness = ("const tab = JSON.parse(process.argv.at(-1)), box = tab.storage;\n"
               "const store = { getItem: k => { if (box === 'none') throw new Error('denied'); return box[k] ?? null; },\n"
               "  setItem: (k, v) => { if (box === 'none') throw new Error('denied'); box[k] = v; } };\n"
               "let bar = '';\n"
               "const page = new Function('location', 'history', 'sessionStorage',\n"
               f"  {json.dumps(script)} + '\\nreturn {{ ROOM, KEY, OPEN, keepKey }};');\n"
               "const got = page({ hash: tab.hash || '', search: tab.search || '', pathname: '/page' },\n"
               "                 { replaceState: (a, b, to) => { bar = to; } }, store);\n"
               "if (tab.traded) got.keepKey(tab.traded.room, tab.traded.key);\n"
               "console.log(JSON.stringify({ room: got.ROOM, key: got.KEY, open: got.OPEN, bar, storage: box }));\n")
    done = subprocess.run(["node", "-e", harness, json.dumps({"storage": {}, **given})], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node runs the page's script; it is not on this box")
def test_a_reload_on_the_mac_keeps_the_page_its_key_in_the_tab_s_own_session_storage_never_in_the_address(tmp_path):
    """need-2026-11-03-49, the other wall: the page takes its key or ticket out of the address bar on load, by design, so
    a reload of the page opened on the Mac came back as /page alone and said it needs its address, and a line typed under
    it reached no room. The tab now keeps its room and key in its own session storage: a reload or a retyped /page in it
    opens the page again, and nothing puts the key back in the address, the history or a bookmark."""
    ticketed = page_head(search="?room=example-room&open=t1")
    assert ticketed == {"room": "example-room", "key": "", "open": "t1", "bar": "/page", "storage": {}}     # the trade gets the key
    traded = page_head(search="?room=example-room&open=t1", traded={"room": "example-room", "key": "k1"})
    kept = traded["storage"]
    assert kept == {"yerk-page-key": json.dumps({"room": "example-room", "key": "k1"}, separators=(",", ":"))}
    assert page_head(storage=kept) == {"room": "example-room", "key": "k1", "open": "", "bar": "/page", "storage": kept}   # the reload
    assert page_head(storage=kept, search="?room=example-room")["key"] == "k1"
    assert page_head(storage=kept, search="?room=other-room")["key"] == ""       # another room's key is not this page's
    assert page_head(storage=kept, search="?room=example-room&open=t2")["key"] == ""    # a new ticket wins: it is traded
    fresh = page_head(storage=kept, hash="#room=example-room&key=k2")             # `yerk room page`'s address wins and is kept
    assert fresh["key"] == "k2" and json.loads(fresh["storage"]["yerk-page-key"]) == {"room": "example-room", "key": "k2"}
    assert fresh["bar"] == "/page"
    assert page_head(storage="none") == {"room": "", "key": "", "open": "", "bar": "/page", "storage": "none"}  # no storage
    script = pagemod.HTML.read_text().split("<script>", 1)[1]
    assert "keepKey(d.room || ROOM, d.key)" in script.split('fetch("/page/open"', 1)[1]          # the trade keeps it
    assert "localStorage.setItem(KEPT" not in script and "localStorage.getItem(KEPT" not in script   # never past the tab


def test_a_tab_with_no_key_says_its_typed_line_reached_no_room_and_keeps_the_words():
    """need-2026-11-03-49: a line typed on a page with no key went to yerkd with none, which refused it in its own words
    (a header it wants); now the tab says so in plain words, keeps the words in the box and says how to open the page."""
    script = pagemod.HTML.read_text().split("<script>", 1)[1]
    send = script.split('say.addEventListener("keydown"', 1)[1].split("\n});", 1)[0]
    assert send.index("if (!KEY)") < send.index('say.value = ""') < send.index('post("event"')    # nothing posted, nothing cleared
    assert '"Not sent: no room hears this tab yet. " + STRANDED' in send
    stranded = script.split("const STRANDED = ", 1)[1].split(";\n", 1)[0]
    assert "open my page on my Mac" in stranded and "yerk room page ROOM on the box" in stranded
    assert "else if (!KEY) line({ text: STRANDED" in script


async def served(y, calls):
    """yerkd's own HTTP server as its start runs it (serve_http), on real sockets, while `calls` speaks to it; then its stop."""
    y.http = None                                                    # a server stopped before stays `started`
    task = asyncio.create_task(y.serve_http())
    for _ in range(250):
        if y.listening() or task.done():
            break
        await asyncio.sleep(0.02)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await calls(c)
    finally:
        y.http.should_exit = True
        await asyncio.wait_for(task, 10)


def test_the_page_is_on_loopback_alone_until_page_listen_names_the_box_s_address_then_its_routes_alone_are_there_too(tmp_path, monkeypatch):
    """need-2026-11-03-45, the wall a room hit: the owner, on the Mac and on the phone, could not open the live page, since
    yerkd served it on 127.0.0.1 alone. With [page] listen naming the box's tailnet or LAN address, yerkd serves the page's
    routes there too, on its own port, the key still the only way into them, and nothing else: /mcp, /hook, /control and
    /status are not found there whatever secret comes. 127.0.0.2 stands in for that address, a second one of the box's."""
    y, _ = page_daemon(tmp_path)
    key, taken = y.page.sheets["example-room"].key, []
    y.rooms["example-room"].submit = taken.append                    # no Claude Code here: what would reach the room
    near, far = f"http://127.0.0.1:{y.port}", f"http://127.0.0.2:{y.port}"

    async def loopback_alone(c):
        assert (await c.get(near + "/page")).status_code == 200
        with pytest.raises(httpx.ConnectError):                      # the default: nothing listens past loopback
            await c.get(far + "/page")
    asyncio.run(served(y, loopback_alone))

    monkeypatch.setattr(pagemod, "listen", lambda conf: "127.0.0.2")

    async def beside(c):
        shown = await c.get(far + "/page")
        assert shown.status_code == 200 and key not in shown.text and "example-room" not in shown.text
        said = await c.post(far + "/page/event", json={"kind": "say", "text": "a line from the phone"}, headers={"x-yerk-secret": key})
        assert said.status_code == 200 and said.json() == {"ok": True}
        assert (await c.post(far + "/page/event", json={"kind": "say", "text": "x"})).status_code == 401            # no key
        assert (await c.post(far + "/page/event", json={"kind": "say", "text": "x"}, headers={"x-yerk-secret": y.secret})).status_code == 401
        ticket = y.page.ticket("example-room", y.port, far).rpartition("open=")[2]
        traded = await c.post(far + "/page/open", json={"ticket": ticket})
        assert traded.status_code == 200 and traded.json() == {"ok": True, "room": "example-room", "key": key}
        async with c.stream("GET", far + "/page/stream", headers={"x-yerk-secret": key}) as feed:
            first = json.loads(await feed.aiter_lines().__anext__())
        assert first["state"]["room"] == "example-room" and first["state"]["lines"][-1]["text"] == "a line from the phone"
        for path in ("/control", "/status", "/hook", "/mcp"):                   # nothing but the page there, the install's secret or not
            assert (await c.post(far + path, json={}, headers={"x-yerk-secret": y.secret})).status_code == 404, path
        assert (await c.get(far + "/", headers={"x-yerk-secret": y.secret})).status_code == 404
        assert (await c.post(near + "/control", json={}, headers={"x-yerk-secret": y.secret})).status_code == 200   # loopback as ever
        assert (await c.get(near + "/page")).status_code == 200
    asyncio.run(served(y, beside))
    assert len(taken) == 1 and words.PAGE_SAID.format(text="a line from the phone") in taken[0].text


def test_the_address_printed_and_the_mac_s_open_carry_the_box_s_host_the_key_only_in_the_fragment(tmp_path, monkeypatch, capsys):
    """need-2026-11-03-45: the address `yerk room page` printed and the one the Mac's open carried were built on 127.0.0.1,
    which on the Mac or the phone is that device itself. With [page] listen they carry the box's host and yerkd's port, the
    key in the printed address's fragment only (a browser never sends it) and never in the opened one, which holds a
    one-time ticket; [page] address, when set, is the host both carry; --here prints the box's own."""
    y, state = page_daemon(tmp_path)
    key = y.page.sheets["example-room"].key
    y.conf["page"] = {"listen": "100.64.0.1"}
    y.write_status("discord")
    assert json.loads((state / "yerkd.json").read_text())["page"] == f"http://100.64.0.1:{y.port}"
    assert address(state, "example-room") == f"http://100.64.0.1:{y.port}/page#room=example-room&key={key}"
    assert address(state, "example-room", here=True) == f"http://127.0.0.1:{y.port}/page#room=example-room&key={key}"
    assert cli.main(["room", "page", "example-room", "--state", str(state)]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == f"http://100.64.0.1:{y.port}/page#room=example-room&key={key}" and "?" not in printed   # no query: the key rides the fragment
    assert cli.main(["room", "page", "example-room", "--here", "--state", str(state)]) == 0
    assert capsys.readouterr().out.strip().startswith(f"http://127.0.0.1:{y.port}/page#")
    args, opened = mac_box(tmp_path, monkeypatch)
    y.conf["page"] = {"listen": "100.64.0.1", "mac": "owner-mac"}               # no [page] address: listen's host, yerkd's port
    got = asyncio.run(y.tool_page("example-room", {"open": True, "where": "mac"}))
    assert got["line"].endswith(words.PAGE_OPENED_MAC)
    url = opened.read_text().strip()
    assert url.startswith(f"http://100.64.0.1:{y.port}/page?room=example-room&open=") and key not in url and key not in args.read_text()
    y.conf["page"] = {"listen": "fd7a:115c:a1e0::1"}
    assert pagemod.reach(y.conf, 8787) == "http://[fd7a:115c:a1e0::1]:8787"
    y.conf["page"] = {"listen": "100.64.0.1", "address": "box.example.ts.net"}   # the name the Mac knows the box by wins
    y.write_status("discord")
    assert address(state, "example-room") == f"http://box.example.ts.net:{y.port}/page#room=example-room&key={key}"
    y.conf["page"] = {}
    y.write_status("discord")
    assert "page" not in json.loads((state / "yerkd.json").read_text())       # the default: the box's own address alone
    assert address(state, "example-room") == f"http://127.0.0.1:{y.port}/page#room=example-room&key={key}"


def test_page_listen_takes_one_tailnet_or_lan_address_and_says_in_words_why_not_any_other(tmp_path, monkeypatch, caplog):
    """A wildcard would put the page on every network the box joins, a public address on the internet in plain text, and a
    host name could resolve elsewhere: each is refused in words, yerkd starts on loopback alone and the Mac's open says why."""
    for said, want in ((None, ""), ("", ""), ("127.0.0.1", ""), ("::1", ""), ("localhost", ""), ("100.64.0.1", "100.64.0.1"),
                       ("192.168.1.20", "192.168.1.20"), (" 10.0.0.5 ", "10.0.0.5"), ("[fd7a:115c:a1e0::1]", "fd7a:115c:a1e0::1")):
        assert pagemod.listen({"page": {"listen": said}} if said is not None else {}) == want, said
    for said, why in (("0.0.0.0", words.PAGE_LISTEN_EVERY), ("::", words.PAGE_LISTEN_EVERY), ("203.0.113.300", words.PAGE_LISTEN_NAME),
                      ("8.8.8.8", words.PAGE_LISTEN_PUBLIC), ("2606:4700::1", words.PAGE_LISTEN_PUBLIC), ("224.0.0.1", words.PAGE_LISTEN_PUBLIC),
                      ("box.example.ts.net", words.PAGE_LISTEN_NAME), (["100.64.0.1"], words.PAGE_LISTEN_NAME)):
        with pytest.raises(ValueError) as e:
            pagemod.listen({"page": {"listen": said}})
        assert str(e.value) == why.format(x=str(said).strip()[:60]), said
        with caplog.at_level("WARNING", logger="yerk.page"):
            assert pagemod.sockets({"page": {"listen": said}}, 1) == []
        assert str(e.value) in caplog.text
    y, _, state = plain_daemon(tmp_path)
    mac_box(tmp_path, monkeypatch)
    y.conf["page"] = {"listen": "0.0.0.0", "mac": "owner-mac"}
    got = asyncio.run(y.tool_page("example-room", {"open": True, "where": "mac"}))
    assert got["line"].endswith(words.PAGE_NOT_OPENED_MAC.format(why=words.PAGE_LISTEN_EVERY.format(x="0.0.0.0"))) and y.page.tickets == {}
    y.write_status("discord")
    assert "page" not in json.loads((state / "yerkd.json").read_text())


def test_an_address_the_box_has_not_got_yet_is_bound_all_the_same_so_a_tailnet_up_after_yerkd_needs_no_restart(tmp_path, caplog):
    """At boot yerkd can start before the box's tailnet is up, when its address is not yet the box's: the page's socket binds
    it all the same (IP_FREEBIND), so the page is served there the moment it comes up, and the log says it is not up yet;
    an address off the tailnet is said to carry the page and its key unencrypted. A port taken there leaves loopback alone."""
    lent, up = pagemod.bind("127.0.0.1", 0)
    assert up and lent.getsockname()[0] == "127.0.0.1"
    port = lent.getsockname()[1]
    lent.close()
    with caplog.at_level("INFO", logger="yerk.page"):
        socks = pagemod.sockets({"page": {"listen": "192.0.2.10"}}, port)    # an address of the documentation's, on no box
    try:
        assert [s.getsockname()[:2] for s in socks] == [("127.0.0.1", port), ("192.0.2.10", port)]
        assert "the box has no address 192.0.2.10 yet" in caplog.text and "192.0.2.10 is not a tailnet address" in caplog.text
        caplog.clear()
        for s in socks:
            s.listen()                                                   # as yerkd's server does with them
        with caplog.at_level("WARNING", logger="yerk.page"):
            assert pagemod.sockets({"page": {"listen": "192.0.2.10"}}, port) == []          # its port there is taken
        assert f"page: on loopback alone: 192.0.2.10 will not take port {port}" in caplog.text
    finally:
        for s in socks:
            s.close()
    six, up = pagemod.bind("2001:db8::10", 0)
    assert not up and six.family == pagemod.socket.AF_INET6
    six.close()


def test_a_comment_line_ended_with_enter_goes_at_once_not_after_the_pause(tmp_path):
    """need-2026-11-04-51: a # line the owner ends with Enter goes to the room now, not after the 1.5 s pause."""
    p, room = hub(tmp_path, quiet=60)

    async def go():
        await p.event("r", {"kind": "edit", "pane": "code", "text": "# make a snake game"})
        assert room.got == []                                    # still typing on it: waits for the pause
        await p.event("r", {"kind": "edit", "pane": "code", "text": "# make a snake game\n", "send": True})
        await p.event("r", {"kind": "edit", "pane": "code", "text": "# make a snake game\n", "send": True})   # nothing new
    asyncio.run(go())
    assert len(room.got) == 1 and room.got[0].text.splitlines()[-1] == "said in solution.py at line 1, as a # comment: make a snake game"
    assert p.sheets["r"].asked == (1, "# make a snake game") and p.sheets["r"].quiet is None
    script = pagemod.HTML.read_text().split("<script>", 1)[1]
    typed = script.split("\nfunction typed(", 1)[1].split("\nfunction ", 1)[0]
    changed = script.split("\nfunction changed(", 1)[1].split("\nfunction ", 1)[0]
    assert "owned(rows[rows.length - 2])" in changed and "send: true" in typed       # the editor's Enter (the node test runs it)
    assert 't === "insertLineBreak" || t === "insertParagraph") newline(ed, a, b)' in script


def test_an_enter_after_a_comment_the_room_has_heard_waits_for_the_pause_like_any_typing(tmp_path):
    """need-2026-11-04-51, the first cold read's third find, which 9966df9 left out: the tab marks an edit `send` whenever
    the line above the caret is a # line of the owner's, and yerkd sent on `send` alone, so an Enter at the end of a
    comment the room had already heard (a drill's header it drew, or the owner's question the pause had sent) sent the room
    a message at once whose whole content was one blank line: a room turn for nothing, the code typed next a second message,
    and the owner's question told twice. Now `send` goes at once only with talk the room has not heard (comments); else the
    edit waits for the pause like any other, and goes in one message with what is typed next."""
    p, room = hub(tmp_path)                                       # a pause of 0.05 s
    p.draw("r", {"code": "# sum two numbers\n" + FIXED})
    s = p.sheets["r"]

    async def go():
        await p.event("r", {"kind": "edit", "pane": "code", "text": "# sum two numbers\n\n" + FIXED, "send": True})
        assert room.got == [] and s.quiet is not None             # Enter after the room's own comment: nothing now, the pause armed
        await p.event("r", {"kind": "edit", "pane": "code", "text": "# sum two numbers\nimport math\n" + FIXED})
        await asyncio.sleep(0.2)                                   # the pause: the new line and its code, one message
        await p.event("r", {"kind": "edit", "pane": "code", "text": "# why math?\n# sum two numbers\nimport math\n" + FIXED})
        await asyncio.sleep(0.2)                                   # the owner's question, said at the pause
        await p.event("r", {"kind": "edit", "pane": "code", "text": "# why math?\n\n# sum two numbers\nimport math\n" + FIXED, "send": True})
        assert len(room.got) == 2 and s.quiet is not None         # Enter after it: not said again
        await p.event("r", {"kind": "edit", "pane": "code", "send": True,
                            "text": "# why math?\n# and not a plus?\n\n# sum two numbers\nimport math\n" + FIXED})
        assert len(room.got) == 3 and s.quiet is None             # a new one ended with Enter: at once, as before
    asyncio.run(go())
    first, second, third = (m.text.splitlines()[1:] for m in room.got)
    assert first == ["typed in solution.py:", "@@ -1,2 +1,3 @@", " # sum two numbers", "+import math", " def add(a, b):"]
    assert second[-1] == "said in solution.py at line 1, as a # comment: why math?"
    assert "+# and not a plus?" in third and third[-1] == "said in solution.py at line 2, as a # comment: and not a plus?"
    assert [x["text"] for x in s.lines] == ["why math?", "and not a plus?"]                # each said once on the chat


@boxed
def test_a_program_with_blank_tests_runs_alone_and_its_prints_are_shown_large_and_said(tmp_path):
    """need-2026-11-04-51: the run's print output shown large, so one surface holds code, talk, output and ink; a program
    with no tests (one the room made on a #make line) runs alone, as a script, in the same sandbox."""
    r = run_tests('print("hello")\nif __name__ == "__main__":\n    print("as a script")\n', "")
    assert r["ok"] and r["main"] and r["output"] == "hello\nas a script\n" and r["ran"] == 0
    assert run_line(r) == f"{words.PAGE_MAIN}\n{words.PAGE_PRINTED.format(output='hello' + chr(10) + 'as a script')}"
    bad = run_tests("print(1)\nprint(1 / 0)\n", "\n")
    assert not bad["ok"] and bad["line"] == 2 and run_line(bad).startswith(words.PAGE_MAIN_ERROR.format(error="ZeroDivisionError: division by zero", line=" (line 2)"))
    p, room = hub(tmp_path)

    async def go():
        await p.run("r", {"code": 'print("one")\n', "tests": ""})
        await asyncio.sleep(0.2)
        await p.run("r", {"code": 'print("two")\n'})              # a new print is news
        await asyncio.sleep(0.2)
    asyncio.run(go())
    assert [m.text.splitlines()[-1] for m in room.got] == ["one", "two"]
    css, body = pagemod.HTML.read_text().split("</style>", 1)
    out = next(v for k, v in styles(css).items() if k.endswith("*/ #out"))      # the rule under its comment
    assert int(re.search(r"(\d+)px", out["font"])[1]) >= 16 and out["white-space"] == "pre-wrap"     # large, its lines kept
    results = body.split('id="results"', 1)[1].split("</div></div>", 1)[0]
    assert results.index('id="sum"') < results.index('<pre id="out" hidden>') < results.index('id="rows"')
    show = body.split("function showRun(", 1)[1].split("\nfunction ", 1)[0]
    assert "out.textContent = (r && r.output)" in show and "r.main" in show and 'make("pre"' not in show


def test_the_chat_closes_to_nothing_and_the_page_remembers_it(tmp_path):
    """need-2026-11-04-51, the other wall: the chat's split was clamped to 15 to 80 of the width, so it could not be closed.
    Under CLOSE it is 0: the chat closed to nothing, the program the whole width, kept in the browser and by yerkd (talk),
    which every tab that opens shows; a button closes it and one on the bar brings it back."""
    css, body = pagemod.HTML.read_text().split("</style>", 1)
    rules = styles(css)
    wide = next(v for k, v in rules.items() if k.endswith("*/ body.chatless"))      # the wide screen's, under its comment
    assert wide["grid-template"] == '"bar bar" auto "main grip" minmax(0, 1fr) / minmax(0, 1fr) 10px'
    assert rules["body.chatless"]["grid-template"] == '"bar" auto "main" minmax(0, 1fr) / minmax(0, 1fr)'     # a narrow one's
    assert rules["body.chatless #chat"]["display"] == "none" and "#chat .head button:not(#close-chat)" in css
    assert 'id="close-chat"' in body.split('id="chat"', 1)[1] and 'id="open-chat"' in body.split('id="bar"', 1)[1].split("</header>", 1)[0]
    script = body.split("<script>", 1)[1]
    split = script.split("function split(", 1)[1].split("\n}\n", 1)[0]
    assert "pct < CLOSE" in split and "pct = 0" in split and 'post("event", { kind: "talk", on: chatless })' in split
    assert "keep(pct === null ? null : pct.toFixed(1))" in split and "Math.max(15, pct)" in split
    assert "if (talkOn !== chatless) split(talkOn ? 0 : opened, true, false);" in script        # the tab shows what yerkd keeps
    assert "if (op.bubble)" in script and "put(name, d[name])" in script        # a drawn program keeps the caret
    assert ".bubble {" in css and ".t-room" in css                              # the room's answers in its colour, its old #> lines too


def test_the_page_tool_closes_the_chat_and_opens_it_on_every_open_tab_and_the_page_keeps_it(tmp_path):
    """need-2026-11-09-81, the wall the ask hit: the owner, on the live page, said a second time they wanted no chat box;
    the page tool drew code, tests, marks, a hint, the bar, the timer and the stall but had no switch for the chat, which
    only the close button in the owner's browser closed, and talk.json is yerkd's own. The tool's `chat` closes it or opens
    it as the buttons do: on every open tab now, kept across a restart and shown by every tab that opens, the room's replies
    then bubbles in the editor (need-2026-11-09-82); the room is not told back what it did itself, an owner's switch it
    undoes before the room heard it is never said, and a word that is neither draws nothing."""
    p, room = hub(tmp_path, quiet=60)
    p.draw("r", {"code": "# why?\nx = 1\n"})
    kept = tmp_path / "rooms" / "r" / "work" / "page" / "talk.json"

    async def go(page, chat, said):
        tab = page.stream("r", beat=0.05)
        first = json.loads(await tab.__anext__())
        got = page.draw("r", {"chat": chat})
        ops = [json.loads(await tab.__anext__())]
        page.said("r", said, inbound=asked(page))                               # its answer to the page
        ops += [json.loads(await tab.__anext__()) for _ in range(2 if page.sheets["r"].talk else 1)]
        await tab.aclose()
        return first, got, ops
    first, got, (op, said, talk) = asyncio.run(go(p, "closed", "the room's answer"))
    s = p.sheets["r"]
    assert first["state"]["talk"] is False and got == {"ok": True, "tabs": 1} and op == {"draw": {"chat": "closed"}}
    assert s.talk and json.loads(kept.read_text())["on"] is True
    assert said["line"]["text"] == "the room's answer" and {k: talk["bubble"][k] for k in ("n", "pane", "line", "text")} == {
        "n": 1, "pane": "code", "line": 1, "text": "the room's answer"}
    assert s.code == "# why?\nx = 1\n" and [b["text"] for b in json.loads(kept.read_text())["bubbles"]] == ["the room's answer"]  # as with the button
    assert {k: v for k, v in events(tmp_path)[-2].items() if k != "t"} == {"kind": "drawn", "chat": "closed"}
    assert s.waiting == [] and p.flush("r") == "" and room.got == []              # nothing told back: the room did it
    again = Page(tmp_path, p.ledger)
    again.add_room(Taken())
    assert again.sheets["r"].state()["state"]["talk"] is True                     # a tab that opens after a restart: closed
    first, got, (op, said) = asyncio.run(go(again, " Open ", "on the chat alone"))
    assert first["state"]["talk"] is True and got == {"ok": True, "tabs": 1} and op == {"draw": {"chat": "open"}}
    assert not again.sheets["r"].talk and json.loads(kept.read_text())["on"] is False and "on the chat alone" not in again.sheets["r"].code
    assert [b["text"] for b in again.sheets["r"].bubbles] == ["the room's answer"]          # no bubble of it: the chat is open
    asyncio.run(again.event("r", {"kind": "talk", "on": True}))                  # the owner closes it; the room not told yet
    again.draw("r", {"chat": "closed"})
    assert again.sheets["r"].waiting == [words.PAGE_TALK_ON]                      # the same switch: still true, still said
    again.draw("r", {"chat": "open"})
    assert again.sheets["r"].waiting == [] and again.flush("r") == ""            # undone: never said
    assert again.draw("r", {"chat": "hidden", "hint": "x"}) == {"ok": False, "error": words.PAGE_CHAT.format(chat="hidden")}
    assert again.sheets["r"].hint == "" and not again.sheets["r"].talk            # a bad word draws nothing


def test_the_page_tool_s_chat_reaches_the_page_through_the_daemon_open_or_not(tmp_path, monkeypatch):
    """need-2026-11-09-81: the tool's `chat` reaches the room's page by the daemon, with no tab open too (the page shows it
    when one opens), beside `open` in one call, and a word that is neither is refused in words."""
    y, _ = page_daemon(tmp_path)
    server = mcp_server.build(y, y.port, y.hook)
    monkeypatch.setattr(mcp_server, "caller_room", lambda ctx: "example-room")
    got = asyncio.run(server.call_tool("page", {"chat": "closed"}))
    assert json.loads((got[0] if isinstance(got, tuple) else got)[0].text) == {"ok": True, "tabs": 0, "note": words.PAGE_NO_TAB}
    assert y.page.sheets["example-room"].state()["state"]["talk"] is True
    opened = []

    async def page_open(room, where="pc", screen=""):
        opened.append((room, where))
        return {"ok": True, "opened": True, "line": words.PAGE_OPENED}
    monkeypatch.setattr(y, "page_open", page_open)
    both = asyncio.run(y.tool_page("example-room", {"open": True, "chat": "open"}))
    assert opened == [("example-room", "pc")] and both["ok"] and both["opened"]["opened"] and not y.page.sheets["example-room"].talk
    assert asyncio.run(y.tool_page("example-room", {"chat": "gone"})) == {"ok": False, "error": words.PAGE_CHAT.format(chat="gone")}


def tab_chat(steps, width=40):
    """The page's own apply(), split() and keep() run by node on stand-ins for the document and the browser's storage: the
    chat at `width` of the screen first, then each step, an op from yerkd's feed or a press of the owner's (split's
    argument): after each, the chat closed or not, what the tab believes yerkd keeps, the body's classes, the chat button
    on the bar shown or not, the chat's width and the split the browser keeps; then what the tab posted to yerkd."""
    script = pagemod.HTML.read_text().split("<script>", 1)[1]
    fns = "".join(f"function {n}(" + script.split(f"\nfunction {n}(", 1)[1].split("\n}\n", 1)[0] + "\n}\n" for n in ("apply", "split"))
    lines = [x for x in script.splitlines() if x.startswith(("function keep(", "const SPLIT = "))]
    harness = ("const [steps, width] = JSON.parse(process.argv.at(-1)), els = {}, store = {}, posted = [];\n"
               "const classes = () => ({ on: new Set(), toggle(c, v) { if (v) this.on.add(c); else this.on.delete(c); } });\n"
               "const $ = s => els[s] || (els[s] = { hidden: true, textContent: '', classList: classes() });\n"
               "const style = { p: {}, getPropertyValue(k) { return this.p[k] || ''; }, removeProperty(k) { delete this.p[k]; },\n"
               "  setProperty(k, v) { this.p[k] = v; } };\n"
               "const document = { body: { style, classList: classes() }, activeElement: null };\n"
               "const localStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, removeItem: k => { delete store[k]; } };\n"
               "const post = (p, b) => (posted.push(b), Promise.resolve({ ok: true }));\n"
               "let KEY = 'k', talkOn = false, chatless = false, opened = null, talked = 0, timerSaid = false, state = { marks: [] };\n"
               "const panes = { code: { ta: { value: '' } } }, noop = () => {};\n"
               "const [gutter, sync, showMarks, showBar, line, showRun, put, run, talk, place, typingChat] = Array(11).fill(noop);\n"
               + "\n".join(lines) + "\n" + fns +
               "split(width, true, false);\nconst seen = steps.map(x => { if (x.op) apply(x.op); else split(x.press === 'opened' ? opened : x.press);\n"
               "  return { chatless, talkOn, body: [...document.body.classList.on], chat_button: !$('#open-chat').hidden,\n"
               "           width: style.p['--chat'] || null, kept: store['yerk-page-chat'] ?? null }; });\n"
               "console.log(JSON.stringify({ seen, posted }));\n")
    done = subprocess.run(["node", "-e", harness, json.dumps([steps, width])], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node runs the page's script; it is not on this box")
def test_a_tab_shows_the_room_s_switch_of_the_chat_and_tells_yerkd_nothing_back():
    """need-2026-11-09-81: a tab given the room's `chat` closes its chat to nothing or opens it at the width it had, as its
    own buttons do, kept in the browser, and posts nothing, since yerkd already keeps it; the owner's press after it still
    tells yerkd, once."""
    got = tab_chat([{"op": {"draw": {"chat": "closed"}}}, {"op": {"draw": {"chat": "closed"}}}, {"op": {"draw": {"chat": "open"}}},
                    {"op": {"draw": {"chat": "closed"}}}, {"press": "opened"}])
    closed = {"chatless": True, "talkOn": True, "body": ["chatless"], "chat_button": True, "width": None, "kept": "0.0"}
    back = {"chatless": False, "talkOn": False, "body": [], "chat_button": False, "width": "40.0%", "kept": "40.0"}
    assert got["seen"] == [closed, closed, back, closed, back]
    assert got["posted"] == [{"kind": "talk", "on": False}]                         # the owner's own press alone


def bubble_ops(q):
    """What a tab's feed got: each op, in order."""
    return [q.get_nowait() for _ in range(q.qsize())]


def test_with_the_chat_closed_a_reply_is_a_bubble_under_the_line_asked_from_and_never_in_the_program(tmp_path):
    """need-2026-11-09-82, the wall the ask hit: with the chat closed the room's reply was a dozen plain #> lines written into
    the owner's file under their # line, the program grown by them and nothing on screen while the room thought. Now it is a
    bubble in the editor under the line they asked from, its words read as the chat reads them, in yerkd's state for every
    tab, never in solution.py or the room's last heard copy; kept across a restart, and put away by its x on every tab and
    for good."""
    p, room = hub(tmp_path, quiet=60)
    code = "def add(a, b):\n    return a - b\n"
    p.draw("r", {"code": code})
    s = p.sheets["r"]

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        s.tabs.append(q)
        await p.event("r", {"kind": "talk", "on": True})
        typed = "# why minus?\n" + code
        await p.event("r", {"kind": "edit", "pane": "code", "text": typed, "send": True})
        p.said("r", "**Look** at line 3: it subtracts.\n\nMake it a plus.", inbound=[room.got[0].id])
        await asyncio.sleep(0)
        return typed, bubble_ops(q)
    typed, ops = asyncio.run(go())
    b = next(o["bubble"] for o in ops if "bubble" in o)
    assert {k: b[k] for k in ("n", "pane", "line", "kind")} == {"n": 1, "pane": "code", "line": 1, "kind": "reply"}
    assert b["parts"][0]["runs"][0] == {"text": "Look", "looks": "b"} and b["text"].startswith("**Look**")
    assert s.code == s.heard[0] == typed and (tmp_path / "rooms" / "r" / "work" / "page" / "solution.py").read_text() == typed
    assert "#>" not in s.code and p.flush("r") == ""             # nothing typed since: the room is never told of a bubble
    assert [x["text"] for x in s.lines][-1].startswith("**Look**")   # and the chat keeps the line too, for when it opens
    assert s.state()["state"]["bubbles"] == [b]
    again = Page(tmp_path, p.ledger)
    again.add_room(Taken())
    kept = again.sheets["r"].state()["state"]
    assert kept["talk"] is True and [(x["n"], x["line"], x["text"]) for x in kept["bubbles"]] == [(1, 1, b["text"])]
    assert kept["bubbles"][0]["parts"] == b["parts"]             # read again once, never kept in talk.json
    assert "parts" not in (tmp_path / "rooms" / "r" / "work" / "page" / "talk.json").read_text()

    async def away():
        q: asyncio.Queue = asyncio.Queue()
        again.sheets["r"].tabs.append(q)
        assert await again.event("r", {"kind": "bubble", "n": 1}) == {"ok": True}
        assert await again.event("r", {"kind": "bubble", "n": 1}) == {"ok": True}   # twice, from two tabs: once
        return bubble_ops(q)
    assert asyncio.run(away()) == [{"away": 1}]
    third = Page(tmp_path, p.ledger)
    third.add_room(Taken())
    assert third.sheets["r"].bubbles == [] and third.sheets["r"].bubble_n == 1     # gone for good, the numbering kept
    assert {"kind": "away", "n": 1} == {k: v for k, v in events(tmp_path)[-1].items() if k != "t"}


def test_the_typing_mark_shows_under_the_line_from_the_send_till_the_turn_s_reply_notice_or_silent_end(tmp_path):
    """need-2026-11-09-82: nothing showed on the page while the room thought. A typing mark goes to every tab under the line
    a message asked from when it is sent, and on the chat for a line said in the say box; the room's sends in its turn
    leave it, and the turn's reply, its failed turn's notice or its silent end (NO_REPLY) take it away."""
    p, room = hub(tmp_path, quiet=60)
    m, s = p.mirror(ConsoleChannel()), p.sheets["r"]
    p.draw("r", {"code": "x = 1\n"})

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        s.tabs.append(q)
        seen = []
        await p.event("r", {"kind": "edit", "pane": "code", "text": "x = 1\n# why one?\n", "send": True})
        seen.append(dict(s.typing))
        await m.send_text("r", "Looking.", inbound=room.got[-1].id)
        await asyncio.sleep(0)                                     # said() hands its steps to the loop
        seen.append(dict(s.typing))
        m.reply("r", "Because.", [room.got[-1].id])
        await asyncio.sleep(0)
        seen.append(dict(s.typing))
        await p.event("r", {"kind": "say", "text": "and two?"})
        seen.append(dict(s.typing))
        m.silent("r", [room.got[-1].id], "card")
        await asyncio.sleep(0)
        seen.append(dict(s.typing))
        await p.event("r", {"kind": "ask", "pane": "code", "line": 1, "text": "x = 1"})
        m.notice("r", "not answered", [room.got[-1].id])
        await asyncio.sleep(0)
        seen.append(dict(s.typing))
        return seen, [o["typing"] for o in bubble_ops(q) if "typing" in o]
    seen, ops = asyncio.run(go())
    assert seen == [{"pane": "code", "line": 2}, {"pane": "code", "line": 2}, {}, {"pane": "chat", "line": 0}, {}, {}]
    assert ops == [{"pane": "code", "line": 2}, {}, {"pane": "chat", "line": 0}, {}, {"pane": "code", "line": 1}, {}]
    assert [b["text"] for b in s.bubbles] == ["Looking.", "Because.", "not answered"]      # the chat open: line asks and # lines
    assert p.sheets["r"].state()["state"]["typing"] == {}


def test_a_line_s_number_or_ctrl_enter_says_about_line_n_and_its_words_and_its_answer_is_a_bubble_under_it(tmp_path):
    """need-2026-11-09-82: one key to ask about a line. Ctrl+Enter on a line, or a click on its number, sends "about line N"
    and its words at once, with the chat open too; the answer lands as a bubble under that line, the program untouched; a
    blank line says so, the tests pane says the tests, and a line the page does not have is refused in words."""
    p, room = hub(tmp_path, quiet=60)
    code = "def add(a, b):\n    return a - b\n\nprint(add(2, 2))\n"
    p.draw("r", {"code": code, "tests": TESTS})
    s = p.sheets["r"]

    async def go():
        assert await p.event("r", {"kind": "ask", "pane": "code", "line": 2, "text": "    return a - b"}) == {"ok": True}
        p.said("r", "It subtracts.", inbound=[room.got[-1].id])
        await p.event("r", {"kind": "ask", "pane": "code", "line": 3})
        await p.event("r", {"kind": "ask", "pane": "tests", "line": 6})
        p.said("r", "That one checks 2 + 2.", inbound=[room.got[-1].id])
        return await p.event("r", {"kind": "ask", "pane": "code", "line": 9})
    refused = asyncio.run(go())
    said = [m.text.splitlines()[-1] for m in room.got]
    assert said == [words.PAGE_ASK_LINE.format(line=2, pane="program", text="return a - b"),
                    words.PAGE_ASK_BLANK.format(line=3, pane="program"),
                    words.PAGE_ASK_LINE.format(line=6, pane="tests", text="def test_add(self):")]
    assert said[0] == "about line 2 of the program: return a - b" and room.got[0].text.splitlines()[0] == words.PAGE_FROM
    assert [(b["pane"], b["line"], b["text"]) for b in s.bubbles] == [("code", 2, "It subtracts."), ("tests", 6, "That one checks 2 + 2.")]
    assert s.code == code and not s.talk and refused == {"ok": False, "error": words.PAGE_ASK_NONE}
    assert [r["line"] for r in events(tmp_path) if r["kind"] == "ask"] == [2, 3, 6]


def test_a_red_tile_says_about_test_name_and_how_it_fails_and_its_answer_goes_under_its_def(tmp_path):
    """need-2026-11-09-82: a click on a red test's tile sends "about test NAME" with how it fails, and the answer lands under
    the test's def line in the tests; a green one's says its name alone, and a test the run never had is refused."""
    p, room = hub(tmp_path, quiet=60)
    p.draw("r", {"code": BROKEN, "tests": TESTS})
    s = p.sheets["r"]
    s.run = {"ok": False, "ran": 2, "passed": 1, "error": "", "line": 0, "output": "", "seconds": 0.1, "tests": [
        {"name": "test_add", "ok": False, "kind": "fail", "message": "Traceback\nAssertionError: 0 != 4", "line": 2, "test_line": 7},
        {"name": "test_zero", "ok": True}]}

    async def go():
        await p.event("r", {"kind": "ask", "test": "test_add"})
        p.said("r", "a - b is 0 for 2 and 2.", inbound=[room.got[-1].id])
        await p.event("r", {"kind": "ask", "test": "test_zero"})
        return await p.event("r", {"kind": "ask", "test": "test_gone"})
    refused = asyncio.run(go())
    assert [m.text.splitlines()[-1] for m in room.got] == ["about test test_add, which fails at line 2: AssertionError: 0 != 4",
                                                           "about test test_zero"]
    assert [(b["pane"], b["line"]) for b in s.bubbles] == [("tests", 6)] and TESTS.split("\n")[5].strip() == "def test_add(self):"
    assert refused == {"ok": False, "error": words.PAGE_ASK_NONE}
    assert pagemod.def_line(TESTS, "test_zero") == 9 and pagemod.def_line(TESTS, "test_ad") == 0


def test_bubbles_move_with_their_lines_as_the_owner_types_and_a_drawn_program_keeps_them_or_takes_them_with_its_lines(tmp_path):
    """need-2026-11-09-82: a bubble is the room's answer to a line, not a line of the program, so it moves with its line: lines
    typed or deleted above it move it, typing on or under it leaves it, and so does the typing mark. A program the room
    draws keeps each bubble on the line it kept and takes away the bubbles of the lines it replaced, every tab told."""
    p, room = hub(tmp_path, quiet=60)
    code = "a = 1\n# why b?\nb = 2\n# why c?\nc = 3\n"
    p.draw("r", {"code": code})
    s = p.sheets["r"]
    s.bubbles = [{"n": 1, "pane": "code", "line": 2, "text": "one", "kind": "reply", "t": 1.0},
                 {"n": 2, "pane": "code", "line": 4, "text": "two", "kind": "reply", "t": 2.0},
                 {"n": 3, "pane": "tests", "line": 2, "text": "three", "kind": "reply", "t": 3.0}]
    s.typing, s.bubble_n = {"pane": "code", "line": 4}, 3

    async def go():
        await p.event("r", {"kind": "edit", "pane": "code", "text": "import os\nimport re\n" + code})          # two above both
        await p.event("r", {"kind": "edit", "pane": "code", "text": "import os\nimport re\na = 1\n# why b?\nb = 2\nb += 1\n# why c?\nc = 3\n"})
        await p.event("r", {"kind": "edit", "pane": "code", "text": "import os\na = 1\n# why b?\nb = 2\nb += 1\n# why c?\nc = 3\n"})
    asyncio.run(go())
    assert [(b["pane"], b["line"]) for b in s.bubbles] == [("code", 3), ("code", 6), ("tests", 2)] and s.typing["line"] == 6
    q: asyncio.Queue = asyncio.Queue()
    s.tabs.append(q)
    p.draw("r", {"code": "import os\na = 1\n# why b?\nb = 2\n# a new round\n"})        # keeps line 3, replaces 6
    d = next(o["draw"] for o in bubble_ops(q) if "draw" in o)
    assert [(b["n"], b["line"]) for b in d["bubbles"]] == [(1, 3), (3, 2)] and [(b["n"], b["line"]) for b in s.bubbles] == [(1, 3), (3, 2)]
    assert [(b["n"], b["line"]) for b in json.loads((tmp_path / "rooms" / "r" / "work" / "page" / "talk.json").read_text())["bubbles"]] == [(1, 3), (3, 2)]
    p.draw("r", {"hint": "no program drawn"})
    assert "bubbles" not in next(o["draw"] for o in bubble_ops(q) if "draw" in o)


def test_a_bubble_an_edit_moved_comes_back_on_its_line_after_a_restart(tmp_path, monkeypatch):
    """PR 40's fix: lines typed above a bubble moved it in yerkd's copy alone, and talk.json still held its old line, so
    after a restart it came back beside another line of the program. An edit that moves a bubble writes talk.json at
    once; one that moves none (a line typed under it, or in the other pane) never writes it."""
    p, room = hub(tmp_path, quiet=60)
    p.draw("r", {"code": "a = 1\n# why b?\nb = 2\n", "tests": TESTS})
    s, wrote, write = p.sheets["r"], [], pagemod.write_private
    monkeypatch.setattr(pagemod, "write_private", lambda path, text, *a: (wrote.append(path.name), write(path, text, *a))[1])

    async def go():
        await p.event("r", {"kind": "ask", "pane": "code", "line": 2, "text": "# why b?"})
        p.said("r", "Because.", inbound=[room.got[-1].id])
        await asyncio.sleep(0)
        for pane, text in (("code", "a = 1\n# why b?\nb = 2\nc = 3\n"), ("tests", "\n" + TESTS),
                           ("code", "import os\nimport re\na = 1\n# why b?\nb = 2\nc = 3\n")):
            await p.event("r", {"kind": "edit", "pane": pane, "text": text})
    asyncio.run(go())
    assert [(b["pane"], b["line"]) for b in s.bubbles] == [("code", 4)]
    assert wrote.count(pagemod.TALK) == 2             # the bubble's own, then the edit that moved it: never the two that did not
    again = Page(tmp_path, p.ledger)
    again.add_room(Taken())
    back = again.sheets["r"]
    assert [(b["n"], b["pane"], b["line"], b["text"]) for b in back.bubbles] == [(1, "code", 4, "Because.")]
    assert back.code.split("\n")[3] == "# why b?"     # beside the line it answers, never "a = 1" two lines up


def test_moved_fallback_and_def_line_find_a_bubble_s_line():
    ch = change("a\n# q\nb\n", "a\nx\n# q\nb\n")                      # a line typed above
    assert pagemod.moved(2, ch) == 3 and pagemod.moved(1, ch) == 1
    assert pagemod.moved(2, change("a\n# q\nb\n", "a\n# q\n\nb\n")) == 2          # Enter at its end: the bubble stays with it
    assert pagemod.moved(2, change("a\n# q\nb\n", "a\n\n# q\nb\n")) == 3          # Enter at its start: down with it
    assert pagemod.moved(3, change("a\nx\n# q\n", "a\n# q\n")) == 2               # a line deleted above
    assert pagemod.moved(2, change("a\n# q\nb\nc\n", "a\nc\n")) == 2              # its own line deleted: where it was
    assert pagemod.moved(2, change("a\n# q\n", "a# q\n")) == 1                    # joined to the line above: there
    assert pagemod.moved(2, change("a\n# q\nb\n", "a\n# q?\nb\nz\n")) == 2        # typing on it or under it: stays
    assert pagemod.fallback("x = 1\n# why?\ny = 2\n# why?\n", (4, "# why?")) == 4 and pagemod.fallback("# why?\nx\n# why?\n", (1, "# why?")) == 1
    assert pagemod.fallback("# one\nx\n# two\ny\n", (9, "# gone")) == 3 and pagemod.fallback("x = 1\ny = 2\n", (0, "")) == 2
    assert pagemod.fallback("", (0, "")) == 1 and pagemod.fallback("x", (0, "")) == 1
    assert pagemod.kept_lines("a\nb\nc", "a\nB\nc\nd") == {1: 1, 3: 3}


EDITOR_FNS = ("owned", "lex", "join", "change", "moved", "rise", "span", "lineOf", "startOf", "edit", "undo", "newline", "indent", "changed")
EDITOR_JS = r"""
let state = { bubbles: [], typing: {} };
const told = [], draw = () => {}, select = () => {}, typed = (name, ended) => told.push(ended);
const ed = { name: "code", text: "", anchor: 0, focus: 0, rows: [], done: [], undone: [] }, out = [];
/*FNS*/
const fns = { owned, lex, change, moved, rise, edit, undo, newline, indent };
for (const [fn, ...args] of JSON.parse(process.argv.at(-1))) {
  if (fn === "state") { state = args[0]; continue; }
  if (fn === "set") { ed.text = args[0]; ed.anchor = ed.focus = args.length > 1 ? args[1] : args[0].length; continue; }
  if (fn === "sel") { ed.anchor = args[0]; ed.focus = args[1]; continue; }
  const got = ["edit", "undo", "newline", "indent"].includes(fn) ? fns[fn](ed, ...args) : fns[fn](...args);
  out.push(["edit", "undo", "newline", "indent"].includes(fn) ? { text: ed.text, caret: ed.focus, told: told.splice(0),
           bubbles: state.bubbles.map(b => b.line), typing: state.typing.line } : got);
}
console.log(JSON.stringify(out));
"""


def page_js(steps):
    """The page's own editor functions (EDITOR_FNS) run by node with no DOM, the rows drawn and yerkd told stubbed: each step
    a call on one editor or a pure function, each answer back in order."""
    script = pagemod.HTML.read_text().split("<script>", 1)[1]
    fns = "".join(f"function {n}(" + script.split(f"\nfunction {n}(", 1)[1].split("\n}\n", 1)[0] + "\n}\n" for n in EDITOR_FNS)
    fns += "".join(re.search(rf"const {n} = new Set\(.*?\);\n", script, re.S)[0] for n in ("KW", "BUILTIN"))
    done = subprocess.run(["node", "-e", EDITOR_JS.replace("/*FNS*/", fns), json.dumps(steps)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


node = pytest.mark.skipif(not shutil.which("node"), reason="node runs the page's script; it is not on this box")


@node
def test_the_page_moves_its_bubbles_as_yerkd_does_on_every_edit():
    """need-2026-11-09-82: each tab moves its bubbles itself as the owner types, and yerkd moves its own on each edit it gets,
    so a reload shows them where the tab did: the page's change and moved are page.py's, edit for edit."""
    rng = random.Random(82)
    cases = []
    for _ in range(300):
        old = "".join(rng.choice(["a", "#", " ", "\n", "\n", "x\n", "# q\n"]) for _ in range(rng.randrange(0, 14)))
        a = rng.randrange(0, len(old) + 1)
        b = rng.randrange(a, len(old) + 1)
        new = old[:a] + "".join(rng.choice(["\n", "y", "# r\n", ""]) for _ in range(rng.randrange(0, 4))) + old[b:]
        cases.append((old, new, rng.randrange(1, old.count("\n") + 2)))
    got = page_js([["change", o, n] for o, n, _ in cases] + [["moved", line, change(o, n)] for o, n, line in cases])
    assert got[:len(cases)] == [change(o, n) for o, n, _ in cases]
    assert got[len(cases):] == [pagemod.moved(line, change(o, n)) for o, n, line in cases]


@node
def test_the_editor_types_indents_undoes_and_tells_yerkd_of_a_comment_ended_with_enter():
    """need-2026-11-09-82: the coloured editor is the page's own, every edit made on its text: typing one word is one undo
    step, Enter keeps the line's indent (four more after a colon), Tab indents, a # line of the owner's ended with Enter is told to
    yerkd at once (`send`), and a bubble under a line moves down when a line is typed above it."""
    got = page_js([
        ["state", {"bubbles": [{"n": 1, "pane": "code", "line": 2}], "typing": {"pane": "code", "line": 2}}],
        ["set", "x = 1\n# why?", 0],
        ["edit", 0, 0, "y", "type"], ["edit", 1, 1, "z", "type"],                          # "yz" typed: one step
        ["newline", 2, 2],                                                                 # a line above the bubble's
        ["undo", True], ["undo", True], ["undo", False],
        ["set", "def f():", 8], ["newline", 8, 8], ["edit", 13, 13, "# go", "type"], ["newline", 17, 17],
        ["set", "a\nb", 0], ["sel", 0, 3], ["indent", False], ["indent", True],
    ])
    assert got[0]["text"] == "yx = 1\n# why?" and got[1]["text"] == "yzx = 1\n# why?" and got[1]["told"] == [False]
    assert got[2] == {"text": "yz\nx = 1\n# why?", "caret": 3, "told": [False], "bubbles": [3], "typing": 3}
    assert got[3]["text"] == "yzx = 1\n# why?" and got[3]["bubbles"] == [2]                # undo: the line and the bubble back
    assert got[4]["text"] == "x = 1\n# why?" and got[5]["text"] == "yzx = 1\n# why?"      # the whole word, then forward again
    assert got[6]["text"] == "def f():\n    " and got[6]["caret"] == 13 and got[6]["told"] == [False]
    assert got[8]["text"] == "def f():\n    # go\n    " and got[8]["told"] == [True]      # the owner's # line ended with Enter: at once
    assert got[9]["text"] == "    a\n    b" and got[10]["text"] == "a\nb"


@node
def test_the_editor_colours_python_and_his_own_lines_in_their_own_colour_with_no_outside_script():
    """need-2026-11-09-82: the editors were one-colour textareas. Both colour Python now, read by the page's own lexer, with no
    outside script: keywords, strings (a triple-quoted one across rows), numbers, a def's name, a decorator, builtins and
    comments, and a whole # line of the owner's in a colour of its own, apart from a #! line and the room's old #> lines."""
    got = page_js([["lex", 'def add(a, b):  # sums', ""], ["lex", '    """doc', ""], ["lex", 'still doc""" + f"x{1}"', '"""'],
                   ["lex", "    # why minus?", ""], ["lex", "#!/usr/bin/env python3", ""], ["lex", "#> an old reply", ""],
                   ["lex", "@cache", ""], ["lex", "print(len(x), 3.5, True)", ""], ["rise", "40", "52"], ["rise", "+3", "+1"],
                   ["rise", "1,200", "1,250.5"], ["rise", "Gold", "Gold"]])
    assert got[0] == {"runs": [["kw", "def"], ["", " "], ["fn", "add"], ["", "(a, b):  "], ["com", "# sums"]], "st": ""}
    assert got[1] == {"runs": [["", "    "], ["str", '"""doc']], "st": '"""'} and got[2]["runs"][0] == ["str", 'still doc"""']
    assert ["str", 'f"x{1}"'] in got[2]["runs"] and got[2]["st"] == ""
    assert got[3]["runs"] == [["", "    "], ["own", "# why minus?"]] and got[4]["runs"] == [["com", "#!/usr/bin/env python3"]]
    assert got[5]["runs"] == [["room", "#> an old reply"]] and got[6]["runs"] == [["dec", "@cache"]]
    assert ["bi", "print"] in got[7]["runs"] and ["num", "3.5"] in got[7]["runs"] and ["kw", "True"] in got[7]["runs"]
    assert got[8:] == [12, 0, 50.5, 0]                           # a bar number that rose pops "+N"; one that fell or is words, none
    html = pagemod.HTML.read_text()
    css, body = html.split("<style>", 1)[1].split("</style>", 1)
    rules, script = styles(css), body.split("<script>", 1)[1]
    assert "<script src" not in html and not re.search(r"<(?:script|link)[^>]+(?:src|href)=", html) and "import(" not in script
    assert "<textarea" not in body.split('id="chat"', 1)[0] and body.count('contenteditable="true"') == 2
    assert rules[".t-own"]["color"] == "var(--own)" and "--own: #7ee787" in css and rules[".t-com"]["color"] != rules[".t-own"]["color"]
    assert "var(--blue)" in rules[".bubble"]["border-left"] and rules[".bubble .body"]["max-height"] == "calc(4 * 1.45em)"
    assert rules[".bubble.open .body"]["max-height"] == "none" and "dot" in rules[".typing i"]["animation"]
    assert "rise" in next(v for k, v in rules.items() if k.endswith("*/ .plus"))["animation"] and 'make("span", "plus", "+" + rise(was.big, c.big))' in script
    assert 'if (e.key === "Enter" && mod) { e.preventDefault(); askLine(ed, 0); }' in script        # Ctrl+Enter asks
    assert "askLine(ed, Number(row.dataset.n))" in script and "ask({ test: t.name })" in script     # a number, a red tile
    assert not [w for w in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write") if w in script]


def test_with_the_chat_closed_only_the_room_s_answers_to_the_page_are_bubbles_and_its_other_lines_stay_on_the_chat(tmp_path, capsys):
    """need-2026-11-09-80, kept by need-2026-11-09-82: with the chat closed a line of the room's is a bubble only when a
    message it answers is one the page sent the room (Page.answers_page): its reply, its notice and its own send in that
    turn, and a reply to a turn that folded the page's message with another's; a Discord answer, a scheduled note's, a
    worker's end, a job's post and an answer to another room's page are on the chat alone, and each still reaches the
    room's channel as before."""
    p, room = hub(tmp_path, quiet=60)
    m, s = p.mirror(ConsoleChannel()), p.sheets["r"]
    p.ledger.inbound_add("1001", "r", "2002", "owner", 9)                     # a message in the room's Discord channel
    p.ledger.inbound_add("note-7", "r", "note", "yerk", 9)                    # a scheduled note's, as jobs ledgers it
    p.ledger.inbound_add("relay-4f2a91", "r", "worker", "yerk", 9)            # a worker's end, as workers ledgers it
    other = asked(p, "r2")                                                    # one from another room's page
    code = "def add(a, b):\n    return a - b\n"
    p.draw("r", {"code": code})

    async def go():
        q: asyncio.Queue = asyncio.Queue()
        s.tabs.append(q)
        await p.event("r", {"kind": "talk", "on": True})
        await p.event("r", {"kind": "edit", "pane": "code", "text": "# why minus?\n" + code, "send": True})
        mid = room.got[-1].id
        m.reply("r", "Sunny all day.", ["1001"])
        m.reply("r", "Call the shop at 9.", ["note-7"])
        m.reply("r", "The worker is done.", ["relay-4f2a91"])
        await m.send_text("r", "Patch notes: three fixes.", key="job-9-out")   # a job's post answers no message
        m.notice("r", "hooks are down", [])
        m.reply("r", "Not this page's.", other)
        await m.send_text("r", "On it.", inbound=mid)                          # the room's own send in the page's turn
        m.reply("r", "Make it a plus.", [mid])
        m.reply("r", "Sunny, and make it a plus.", ["1001", mid])               # a turn that folded both
        m.notice("r", "not answered", [mid])                                    # a page turn that failed
        await asyncio.sleep(0)
        return bubble_ops(q)
    ops = asyncio.run(go())
    talk = ["On it.", "Make it a plus.", "Sunny, and make it a plus.", "not answered"]
    assert [(b["line"], b["text"], b["kind"]) for b in s.bubbles] == [(1, x, "notice" if x == "not answered" else "reply") for x in talk]
    assert [o["bubble"]["text"] for o in ops if "bubble" in o] == talk and s.code == "# why minus?\n" + code
    said = ["Sunny all day.", "Call the shop at 9.", "The worker is done.", "Patch notes: three fixes.", "hooks are down", "Not this page's.",
            *talk]
    assert [x["text"] for x in s.lines] == ["why minus?", *said]                # the chat keeps every line, either way
    out = capsys.readouterr().out
    assert all(json.dumps(x, ensure_ascii=False) in out for x in said)          # the channel got every one, as before


def test_a_room_s_turn_on_a_discord_message_leaves_the_editor_be_and_its_turn_on_the_page_lands_a_bubble(env):  # noqa: F811
    """need-2026-11-09-80 and 82, on a room's own turns: with the chat closed, its reply to a message in its Discord channel is
    on the chat alone, no bubble and no typing mark; its reply to the # line the owner then ends with Enter on the page lands
    as a bubble under that line, its typing mark gone with it, the program as they left it. The messages each turn answered
    decide, as the room hands them to its reply (Room._results)."""
    make, ledger, tmp_path = env
    p = Page(tmp_path, ledger, quiet=60)
    chan = Chan()
    room = make(Mirror(chan, p))
    p.add_room(room)
    s = p.sheets["r"]
    p.draw("r", {"code": "x = 1\n"})
    asyncio.run(p.event("r", {"kind": "talk", "on": True}))
    ledger.inbound_add("1", "r", "c", "owner", 5)
    assert room.submit(Inbound("1", "owner", "hello")) == "started"
    wait_idle(room)
    assert chan.replies == [("echo: hello", ["1"])] and s.bubbles == [] and [x["text"] for x in s.lines] == ["echo: hello"]
    asyncio.run(p.event("r", {"kind": "edit", "pane": "code", "text": "# why?\nx = 1\n", "send": True}))
    wait_idle(room)
    (reply, answered) = chan.replies[-1]
    assert len(chan.replies) == 2 and ledger.inbound_get(answered[0])["channel"] == pagemod.CHANNEL and reply.startswith("echo: ")
    assert [(b["line"], b["text"]) for b in s.bubbles] == [(1, reply)] and s.code == "# why?\nx = 1\n" and s.typing == {}
    assert [x["text"] for x in s.lines] == ["echo: hello", "why?", reply]


def test_the_room_s_own_send_is_a_bubble_only_from_its_turn_on_the_page(tmp_path):
    """need-2026-11-09-80 and 82, the `send` tool: a room's send to itself carries the messages its turn answers to the page,
    so with the chat closed one from its turn on the page's message is a bubble, and one from a turn on a Discord message, or
    from no turn, stays on the chat."""
    y, _ = page_daemon(tmp_path)
    room, s = y.rooms["example-room"], y.page.sheets["example-room"]
    asyncio.run(y.page.event("example-room", {"kind": "talk", "on": True}))
    y.page.draw("example-room", {"code": "# q?\nx = 1\n"})
    y.page.sheets["example-room"].asked = (1, "# q?")
    y.ledger.inbound_add("1001", "example-room", "2002", "owner", 5)
    assert asyncio.run(y.tool_send("example-room", "from no turn", "", "example-room"))["ok"]
    room.busy, room.turn_key, room.inflight = True, "abc123def456", [SimpleNamespace(id="1001")]
    assert asyncio.run(y.tool_send("example-room", "from the Discord turn", "", "example-room"))["ok"]
    room.inflight = [SimpleNamespace(id=asked(y.page, "example-room")[0])]
    assert asyncio.run(y.tool_send("example-room", "from the page turn", "", "example-room"))["ok"]
    room.busy = False
    assert [(b["line"], b["text"]) for b in s.bubbles] == [(1, "from the page turn")] and s.code == "# q?\nx = 1\n"
    assert [x["text"] for x in s.lines] == ["from no turn", "from the Discord turn", "from the page turn"]


def windows_box(tmp_path, monkeypatch, said='{"ok": true, "n": 2, "of": 3, "x": 1920, "y": 0, "w": 2560, "h": 1440, "locked": false, "opened": true}'):
    """A stand-in for the owner's Windows PC under WSL: its PowerShell writes its command line to `ps-args`, the address,
    screen and room it was handed in its environment to `ps-env` (with WSLENV, which hands them over), and says `said`, as
    the placing script ends; the box's own opener writes what it was handed to `opened`."""
    (ps := tmp_path / "powershell.exe").write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{tmp_path}/ps-args"\n'
        f'printf \'%s\\n\' "$YERK_PAGE_URL" "$YERK_PAGE_SCREEN" "$YERK_PAGE_ROOM" "$WSLENV" > "{tmp_path}/ps-env"\n'
        f"cat <<'SAID'\n{said}\nSAID\n")
    ps.chmod(0o700)
    monkeypatch.setattr(pagemod, "POWERSHELL", str(ps))
    monkeypatch.setattr(pagemod, "wsl", lambda: True)
    monkeypatch.setattr(pagemod, "PAINT_SECONDS", 0.3)
    return tmp_path / "ps-args", tmp_path / "ps-env"


async def open_and_paint(y, req, handed_file, ticket_of=lambda url: url.rpartition("open=")[2], room="example-room"):
    """The page tool's call while the tab it opens paints: once the PC was handed the address, the tab says so (page.html's
    `shown`, with the one-time ticket it traded), as the owner's browser does when it draws a frame where the owner sees it."""
    async def tab():
        for _ in range(200):
            if handed_file.exists() and handed_file.read_text().strip():
                break
            await asyncio.sleep(0.01)
        url = handed_file.read_text().splitlines()[0]
        assert (await y.page.event(room, {"kind": "shown", "ticket": ticket_of(url)})) == {"ok": True}
    told = asyncio.create_task(tab())
    got = await y.tool_page("example-room", req)
    await told
    return got


def test_the_page_opens_maximized_on_the_screen_the_owner_names_and_the_room_hears_it_painted_before_it_looks(tmp_path, monkeypatch):
    """need-2026-11-10-82, the wall a room hit: the owner's standing order is the page on their middle monitor, and a scheduled
    check opened it with the page tool, which said "opened on the owner's screen" while the browser had put it on their left
    monitor, maximized, unpainted (the screen asleep), so a desktop screenshot showed an old frame, and only a click, which
    needs the owner's word, would wake it. A named screen (`screen`, else [page] screen) opens it in a window of its own on that
    monitor, maximized and raised, the screen woken with no click; the room is told it painted only once the tab says so,
    with the rectangle to shoot."""
    y, _, state = plain_daemon(tmp_path)
    args, handed = windows_box(tmp_path, monkeypatch)
    y.conf["page"] = {"open": [str(opener_script(tmp_path))]}
    got = asyncio.run(open_and_paint(y, {"open": True, "screen": "Middle"}, handed))
    assert got == {"ok": True, "opened": True, "line": f"{words.PAGE_ON}; {words.PAGE_OPENED_SCREEN.format(screen='middle', n=2, of=3)}; "
                                                      f"{words.PAGE_PAINTED.format(x=1920, y=0, w=2560, h=1440)}"}
    assert not (tmp_path / "opened").exists()                                    # the box's opener never ran: no tab elsewhere
    key = (state / "secrets" / "pages" / "example-room.secret").read_text().strip()
    url, screen, room, wslenv = handed.read_text().splitlines()
    assert url.startswith(f"http://127.0.0.1:{y.port}/page?room=example-room&open=") and key not in url
    assert (screen, room) == ("middle", "example-room")
    assert {"YERK_PAGE_URL/w", "YERK_PAGE_SCREEN/w", "YERK_PAGE_ROOM/w"} <= set(wslenv.split(":"))   # handed to Windows
    argv = args.read_text().splitlines()
    assert url.rpartition("open=")[2] not in args.read_text() and argv[:-1] == [
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand"]     # the ticket on no command line
    assert pagemod.base64.b64decode(argv[-1]).decode("utf-16-le") == pagemod.PLACE_PS1
    y.conf["page"] = {"open": [str(opener_script(tmp_path))], "screen": "2"}     # the owner's standing order, set once
    handed.unlink()
    got = asyncio.run(open_and_paint(y, {"open": True}, handed))
    assert got["line"].startswith(f"{words.PAGE_WAS_ON}; {words.PAGE_OPENED_SCREEN.format(screen='2', n=2, of=3)}")
    assert handed.read_text().splitlines()[1] == "2" and got["line"].endswith(words.PAGE_PAINTED.format(x=1920, y=0, w=2560, h=1440))


def test_a_tab_that_never_paints_is_said_unpainted_so_the_room_never_reads_an_old_frame_as_the_page(tmp_path, monkeypatch):
    """The wall itself: the open said done while the screen slept and the tab drew nothing. With no frame drawn by the tab
    the open made (another ticket's, or another room's, counts for nothing), the room hears the page is not painted and
    why; a locked screen is said locked."""
    y, _, _ = plain_daemon(tmp_path)
    _, handed = windows_box(tmp_path, monkeypatch)
    got = asyncio.run(y.tool_page("example-room", {"open": True, "screen": "middle"}))
    assert got["opened"] and got["line"].endswith(words.PAGE_NOT_PAINTED.format(n=0.3)) and words.PAGE_OPENED not in got["line"]
    handed.unlink()
    got = asyncio.run(open_and_paint(y, {"screen": "middle"}, handed, ticket_of=lambda url: "another-ticket"))
    assert got["line"].endswith(words.PAGE_NOT_PAINTED.format(n=0.3))
    y.page_switch("example-quiet", True)
    handed.unlink()
    got = asyncio.run(open_and_paint(y, {"screen": "middle"}, handed, room="example-quiet"))     # its key, this room's ticket
    assert got["line"].endswith(words.PAGE_NOT_PAINTED.format(n=0.3))
    windows_box(tmp_path, monkeypatch, said='{"ok": true, "n": 2, "of": 2, "x": 1920, "y": 0, "w": 2560, "h": 1440, "locked": true}')
    got = asyncio.run(y.tool_page("example-room", {"screen": "middle"}))
    assert got["line"] == f"{words.PAGE_WAS_ON}; {words.PAGE_OPENED_SCREEN.format(screen='middle', n=2, of=2)}; {words.PAGE_LOCKED}"


def test_a_screen_that_is_not_there_or_not_placed_is_said_in_words_and_the_page_opens_once_at_most(tmp_path, monkeypatch):
    y, _, _ = plain_daemon(tmp_path)
    y.conf["page"] = {"open": [str(opener_script(tmp_path))]}
    assert asyncio.run(y.tool_page("example-room", {"open": True, "screen": "top"})) == {
        "ok": False, "error": words.PAGE_SCREEN.format(screen="top")}
    assert asyncio.run(y.tool_page("example-room", {"screen": "middle", "where": "mac"})) == {"ok": False, "error": words.PAGE_SCREEN_MAC}
    assert "example-room" not in y.page.sheets                                   # neither switched anything on
    got = asyncio.run(y.tool_page("example-room", {"screen": "middle"}))        # not a Windows PC: the box's opener, said
    assert got["line"].startswith(f"{words.PAGE_ON}; {words.PAGE_NOT_PLACED.format(screen='middle', why=words.PAGE_SCREEN_NOT_HERE)}; "
                                  f"{words.PAGE_OPENED}; ") and (tmp_path / "opened").exists()
    (tmp_path / "opened").unlink()
    windows_box(tmp_path, monkeypatch, said='{"ok": false, "error": "screen", "of": 2, "opened": false}')
    got = asyncio.run(y.tool_page("example-room", {"screen": "5"}))
    why = words.PAGE_NO_SUCH_SCREEN.format(screen="5", n=2)
    assert got == {"ok": True, "opened": False, "line": f"{words.PAGE_WAS_ON}; {words.PAGE_NOT_PLACED.format(screen='5', why=why)}"}
    assert not (tmp_path / "opened").exists()                                    # no tab where the browser puts it, unasked
    windows_box(tmp_path, monkeypatch, said='{"ok": false, "error": "browser", "opened": false}')
    got = asyncio.run(y.tool_page("example-room", {"screen": "left"}))         # a browser it cannot open a window of: the opener
    assert f"{words.PAGE_NOT_PLACED.format(screen='left', why=words.PAGE_NO_BROWSER)}; {words.PAGE_OPENED}" in got["line"]
    assert (tmp_path / "opened").exists()
    (tmp_path / "opened").unlink()
    windows_box(tmp_path, monkeypatch, said='{"ok": false, "error": "window", "opened": true}')
    got = asyncio.run(y.tool_page("example-room", {"screen": "left"}))         # the browser was started: never a second tab
    assert got["opened"] and words.PAGE_NOT_PLACED.format(screen="left", why=words.PAGE_NO_WINDOW) in got["line"]
    assert not (tmp_path / "opened").exists()
    y.conf["page"] = {"open": [str(opener_script(tmp_path))], "screen": "the big one"}    # a [page] screen that names none
    got = asyncio.run(y.tool_page("example-room", {"open": True}))
    assert got["line"] == f"{words.PAGE_WAS_ON}; [page] {words.PAGE_SCREEN.format(screen='the big one')}; {words.PAGE_OPENED}"


def test_the_placing_says_powershell_s_own_failure_in_words_and_never_the_address(tmp_path, monkeypatch):
    url = "http://127.0.0.1:1/page?room=r&open=secret-ticket"
    assert pagemod.place(url, "middle", "r") == {"ok": False, "opened": False, "again": True,
                                                 "why": words.PAGE_OPEN_FAILED.format(why="No such file or directory")}
    windows_box(tmp_path, monkeypatch, said='')
    (tmp_path / "powershell.exe").write_text('#!/bin/sh\necho "cannot run for $YERK_PAGE_URL" >&2\nexit 1\n')
    got = pagemod.place(url, "middle", "r")
    assert got["why"] == words.PAGE_OPEN_FAILED.format(why="cannot run for the address") and got["again"]
    windows_box(tmp_path, monkeypatch, said='{"ok": false, "error": "failed", "why": "Access is denied", "opened": true}')
    assert pagemod.place(url, "middle", "r") == {"ok": False, "opened": True, "again": False, "locked": False,
                                                 "why": words.PAGE_OPEN_FAILED.format(why="Access is denied")}
    (tmp_path / "powershell.exe").write_text("#!/bin/sh\nsleep 5\n")
    assert pagemod.place(url, "middle", "r", seconds=0.2) == {"ok": False, "opened": True, "again": False,
                                                              "why": words.PAGE_OPEN_SLOW.format(n=0)}
    assert [pagemod.screen_name(x) for x in ("", None, " LEFT ", "middle", "right", "primary", "3")] == [
        "", "", "left", "middle", "right", "primary", "3"]
    for bad in ("0", "10", "centre", "/home/x/screen"):
        with pytest.raises(ValueError):
            pagemod.screen_name(bad)


def test_the_tab_says_it_painted_once_from_a_frame_drawn_while_it_shows_and_the_script_takes_the_owner_s_middle_one():
    """The tab's word that it painted is a frame drawn while it shows (two animation frames, which a browser never runs for
    a covered window, a screen asleep or locked), once, after its ticket's trade, with that ticket; and the placing script
    counts the screens from the left, its middle the centre one, of two the right one, so a third screen asleep, which
    Windows can drop, leaves the owner's middle one where it was."""
    script = pagemod.HTML.read_text().split("<script>", 1)[1]
    shown = script.split("function shown() {", 1)[1].split("\n}\n", 1)[0]
    assert 'document.visibilityState !== "visible"' in shown and shown.count("requestAnimationFrame") == 2
    assert 'post("event", { kind: "shown", ticket: OPEN })' in shown and "told = true" in shown
    assert 'document.addEventListener("visibilitychange", shown)' in script
    assert "listen(); shown(); }" in script.split('fetch("/page/open"', 1)[1]
    ps = pagemod.PLACE_PS1
    assert "elseif ($want -eq 'middle') { $i = [int][math]::Floor($n / 2) }" in ps
    assert "all.Sort((a, b) => a[0] != b[0] ? a[0].CompareTo(b[0]) : a[1].CompareTo(b[1]));" in ps
    assert ps.index("SetThreadExecutionState(2)") < ps.index("mouse_event(1, 0, 0, 0") < ps.index("Start-Process")   # woken first
    assert "click" not in ps.lower() and "SendKeys" not in ps and "SetCursorPos" not in ps                # no click, no key, no move
    assert "0x53" in ps and "ShowWindow($h, 3)" in ps                            # raised with no focus taken, maximized there
