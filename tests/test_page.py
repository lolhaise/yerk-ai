"""Checks a room's live page, the room on a browser tab (channels/page.py): your typing, drawing and test runs there reach the room
as messages, mid-turn too, the tests run when the page asks with no model, and what the room says and draws shows on the page."""
import asyncio
import json
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


def add_on(tmp_path, ssh="owner@owner-mac", mode=0o644, name="addon"):
    """An add-on's folder as the hands plug-in keeps it: its server's script and, beside it, the Mac its mac tool reaches."""
    (folder := tmp_path / name).mkdir(exist_ok=True)
    (folder / "server.mjs").write_text("// an add-on's MCP server\n")
    (f := folder / pagemod.MAC_FILE).write_text(json.dumps({"ssh": ssh}))
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
