"""Checks how yerkd reports on itself: the status socket and the card, the watchdog beat and its stall
rows, the morning diff. /proc and systemd are faked where it matters; the stall is a real event loop held
for 3 s in a test daemon on a temp state folder with no channel adapter attached. Layer 7, run 1."""
import asyncio
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

from yerk_ai import __version__, beat, cli, context, jobs, mcp_server, morning, status, usage, words
from yerk_ai.accounts import Account, Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE, REPO, budgets
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
from yerk_ai.ledger import Ledger
from yerk_ai.meters import Meters
from yerk_ai.room import Inbound

DOTS = (words.DONE, words.WORKING, words.NOT_DONE, words.NOTHING)

TICK, PAGE = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
NOW = 1792303200.0                     # a fixed morning for the diff


def fake_proc(root, procs, uptime=1000.0):
    """A /proc holding `procs`: pid -> (name, parent, cpu seconds, started at seconds after boot, resident pages)."""
    root.mkdir(parents=True)
    (root / "uptime").write_text(f"{uptime} 0.0\n")
    for pid, (name, ppid, cpu, start, pages) in procs.items():
        (root / str(pid)).mkdir()
        f = ["S", str(ppid), *["0"] * 9, str(int(cpu * TICK)), "0", *["0"] * 6, str(int(start * TICK)), *["0"] * 5]
        (root / str(pid) / "stat").write_text(f"{pid} ({name}) {' '.join(f)}\n")
        (root / str(pid) / "statm").write_text(f"{pages * 2} {pages} 0 0 0 0 0\n")
    return root


class Timers:
    def __init__(self, timers=None):
        self.t = timers

    def timers(self):
        if self.t is None:
            raise OSError("systemctl --user list-timers exited 1")
        return self.t


def busy_room(since=None, waiting=0, pid=None):
    rp = SimpleNamespace(proc=SimpleNamespace(pid=pid), alive=lambda: True) if pid else None
    return SimpleNamespace(lock=threading.Lock(), busy=since is not None, rp=rp, pending=[], conf={},
                           inflight=[Inbound("m", "owner", "hi", received=since)] if since else [],
                           queue=[Inbound(f"q{i}", "owner", "x") for i in range(waiting)])


def test_proc_reads_a_process_its_tree_and_its_use(tmp_path):
    proc = fake_proc(tmp_path / "proc", {10: ("claude", 1, 30.0, 400.0, 25600), 11: ("node (mcp)", 10, 6.0, 500.0, 2560),
                                         12: ("bash", 11, 0.0, 900.0, 256), 13: ("other", 1, 99.0, 10.0, 256)})
    (proc / "10" / "wchan").write_text("do_epoll_wait")
    r = status.stat(10, proc)
    assert (r["name"], r["ppid"], r["cpu_s"], r["age_s"], r["wchan"]) == ("claude", 1, 30.0, 600.0, "do_epoll_wait")
    assert status.stat(11, proc)["name"] == "node (mcp)"                 # a name with parentheses in it
    assert status.stat(12, proc)["wchan"].startswith("unreadable")       # said, never shown as "-"
    assert sorted(x["pid"] for x in status.tree(10, proc)) == [10, 11, 12] and status.stat(99, proc) is None
    u = status.use("example-room", status.tree(10, proc))
    mb = sum(round(n * PAGE / 2**20, 1) for n in (25600, 2560, 256))
    assert u["procs"] == 3 and u["rss_mb"] == round(mb, 1) and u["cpu_pct"] == round(100 * 36.0 / 600.0, 2)
    assert status.use("gone", []) == {"name": "gone", "gone": True}


def test_the_card_is_phone_width_state_first_and_colours_only_what_means_something(tmp_path):
    now = time.time()
    proc = fake_proc(tmp_path / "proc", {os.getpid(): ("python", 1, 3.0, 0.0, 20480), 10: ("claude", os.getpid(), 6.0, 900.0, 25600)})
    led = Ledger(tmp_path / "ledger.sqlite")
    led.worker_add(id="abc123", room="example-room", account="one", unit="yerk-worker-abc123", started=now - 180, cap=900)
    led.worker_add(id="def456", room="example-room", account="one", unit="yerk-worker-def456", started=now - 60, cap=900)
    led.stall(began=max(now - 60, status.midnight(now)), seconds=3.2, doing="room.py:300 in _run", dump="")
    Meters(tmp_path / "meters.json").seen("one", {"five_hour": {"utilization": 0.95, "resetsAt": now + 3000}})
    cool = Cooldowns(tmp_path / "cooldowns.json")
    cool.set("two", "quota", now + 1800)
    rooms = {"example-room": busy_room(now - 120, waiting=1, pid=10), "example-quiet": busy_room(now - 900)}
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, load_accounts(EXAMPLE_PRIVATE / "accounts.toml"), cool, led, rooms,
                       now=now, proc=proc, units=lambda: {"yerk-worker-abc123"},
                       timers=Timers({f"{jobs.UNIT}backup.timer": {"next": now + 600, "last": None}}))
    lines = status.card(json.loads(json.dumps(s)), now)            # what the socket carries is plain JSON
    assert all(words.width(x) <= words.PHONE and x[:1] in (*DOTS, words.PLAIN) for x in lines), lines
    assert lines[0] == "- yerkd running: 2 rooms"                         # two names would not fit
    assert lines[1] == f"{words.WORKING} ① 5h {words.WARN}95%" and lines[2] == f"- ↻ 5h {words.short_when(now + 3000, now)}"
    assert lines[3] == f"{words.NOT_DONE} ②: no reading yet" and lines[4].startswith("- ↻ resting until ")
    assert "- example-room: busy 2 min, 1 waiting" in lines            # in flight, since when
    assert f"{words.NOT_DONE} example-quiet: busy 15 min" in lines      # past 10 minutes: a blocked room
    assert "- worker abc123: 3 min of 15" in lines and f"{words.NOT_DONE} worker def456: gone, no result" in lines
    assert "- job backup: in 10 min" in lines
    assert "- example-room: 100 MB, 6.0% cpu" in lines and any(x.startswith("- yerkd: 80 MB") for x in lines)
    assert lines[-1] == f"{words.NOT_DONE} stalls today: 1, longest 3.2 s"
    reds = [x for x in lines if x.startswith(words.NOT_DONE)]
    assert len(reds) == 5 and f"{words.NOT_DONE} {words.NO_BACKUP}" in reds  # resting, blocked, gone, no backup, stall: no other
    quiet = status.card(status.collect(EXAMPLE_PRIVATE, tmp_path / "q", [], cool, Ledger(tmp_path / "q.sqlite"),
                                       units=set, timers=Timers()), now)
    assert quiet == [f"{words.NOT_DONE} yerkd: not running", f"- yerk {__version__} at ?", "- workers: none running",
                     f"{words.NOT_DONE} jobs: the timer list is unreadable", f"{words.NOT_DONE} {words.NO_BACKUP}",
                     "- stalls today: none"]


def test_a_room_put_on_an_account_says_so_on_the_card_and_a_room_on_its_order_says_nothing_new(tmp_path):
    for room, account in (("example-room", "two"), ("example-quiet", "gone")):    # gone: off accounts.toml
        (tmp_path / "rooms" / room).mkdir(parents=True)
        (tmp_path / "rooms" / room / "override.json").write_text(json.dumps({"account": account}))
    rooms = {"example-room": busy_room(), "example-quiet": busy_room(), "third": busy_room()}
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, load_accounts(EXAMPLE_PRIVATE / "accounts.toml"), Cooldowns(None),
                       Ledger(tmp_path / "ledger.sqlite"), rooms, units=set, timers=Timers({}))
    lines = status.card(json.loads(json.dumps(s)))
    assert [r["account"] for r in s["rooms"]] == ["two", "", ""]
    assert [x for x in lines if "account" in x] == ["- example-room: on account ②"]     # the pin, and only the pin
    assert all(words.width(x) <= words.PHONE and x[:1] in (*DOTS, words.PLAIN) for x in lines), lines


FAKE = Path(__file__).with_name("fake_claude.py")
H = 3600.0
FIVE = """[accounts]
one = {{ config_dir = "{d}/one", order = 2, bill_day = 8 }}
four = {{ config_dir = "{d}/four", order = 3 }}
two = {{ config_dir = "{d}/two", order = 4, bill_day = 31 }}
three = {{ config_dir = "{d}/three", order = 5, bill_day = 0 }}
five = {{ config_dir = "{d}/five", order = 1, bill_day = 25 }}
"""


def five_accounts(tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "accounts.toml").write_text(FIVE.format(d=tmp_path))
    return load_accounts(tmp_path / "private" / "accounts.toml")


def live(five, five_in, week, week_in, fable=None, fable_in=None):
    """A live reading as usage.parse returns one: percents, and resets this many seconds after NOW."""
    r = {"five_hour": {"pct": five, "resets": NOW + five_in}, "seven_day": {"pct": week, "resets": NOW + week_in}}
    return r if fable is None else {**r, "model": {"name": "Fable", "pct": fable, "resets": NOW + (fable_in or week_in)}}


# the box card's tank as seen live, its accounts as Yerk names them
TANK = {"one": live(7, 3.3 * H, 93, 10 * H, 100), "two": live(29, 3.5 * H, 72, 108 * H, 100),
        "three": live(14, 1 * H, 93, 72 * H, 100), "four": live(100, 0.3 * H, 48, 132 * H, 76),
        "five": live(7, 4.5 * H, 1, 38 * H, 2)}


def test_live_meters_put_five_accounts_on_the_card_in_the_box_cards_shape_with_the_next_resets_in_order(tmp_path):
    accounts, asked = five_accounts(tmp_path), []
    assert [(a.name, a.bill_day) for a in accounts] == [("five", 25), ("one", 8), ("four", None), ("two", 31),
                                                         ("three", None)]      # 0 is no day of a month: left out
    cool = Cooldowns(tmp_path / "cooldowns.json")
    cool.set("one", "rate_limit", NOW + 10 * H)
    cool.set("four", "rate_limit", NOW + 0.3 * H)
    s = status.collect(tmp_path / "private", tmp_path, accounts, cool, Ledger(tmp_path / "ledger.sqlite"), now=NOW,
                       units=set, timers=Timers({}), live=True, read_one=lambda a: asked.append(a.name) or TANK[a.name])
    lines = status.card(json.loads(json.dumps(s)), NOW)
    w, d, ok = words.short_when, words.short_day, f"{words.WARN}"
    block = lines[1:lines.index("- yerk " + __version__ + " at ?")]
    assert block == [
        "- ⑤ 5h 7% · wk 1% · Fable 2%",
        f"- ↻ 5h {w(NOW + 4.5 * H, NOW)} · wk {w(NOW + 38 * H, NOW)} · 💳 {d(status.next_bill(25, NOW))}",
        f"{words.NOT_DONE} {words.DONE}① 5h 7% · wk {ok}93% · Fable {ok}100%",          # resting, and its week is next
        f"- ↻ resting until {w(NOW + 10 * H, NOW)} · 💳 {d(status.next_bill(8, NOW))}",
        f"{words.NOT_DONE} ④ 5h {ok}100% · wk 48% · Fable 76%",
        f"- ↻ resting until {w(NOW + 0.3 * H, NOW)}",                                    # no bill day: no card
        f"{words.WORKING} ② 5h 29% · wk 72% · Fable {ok}100%",                           # Fable is out, Opus is not
        f"- ↻ 5h {w(NOW + 3.5 * H, NOW)} · wk {w(NOW + 108 * H, NOW)} · 💳 {d(status.next_bill(31, NOW))}",
        f"{words.WORKING} ③ 5h 14% · wk {ok}93% · Fable {ok}100%",
        f"- ↻ 5h {w(NOW + 1 * H, NOW)} · wk {w(NOW + 72 * H, NOW)}",
        f"- wk next: {words.DONE}① {w(NOW + 10 * H, NOW)} · then ⑤ {w(NOW + 38 * H, NOW)}",
        f"- 5h next: ④ {w(NOW + 0.3 * H, NOW)} · then ③ {w(NOW + 1 * H, NOW)}",
        "- next turn tries ⑤ first"]
    assert all(words.width(x) <= words.PHONE and x[:1] in (*DOTS, words.PLAIN) for x in lines), lines
    assert sorted(asked) == sorted(TANK) and s["accounts"][0]["meter"]["source"] == "live"
    again = status.collect(tmp_path / "private", tmp_path, accounts, cool, Ledger(tmp_path / "ledger.sqlite"),
                           now=NOW + 59, units=set, timers=Timers({}), live=True, read_one=lambda a: asked.append(a.name))
    assert len(asked) == 5 and again["accounts"] == s["accounts"]           # under a minute: no second Claude Code
    cool.set("five", "rate_limit", NOW + 2 * H)                             # the first in order rests: the next free one
    later = status.collect(tmp_path / "private", tmp_path, accounts, cool, Ledger(tmp_path / "ledger.sqlite"), now=NOW + 61,
                           units=set, timers=Timers({}), live=True, read_one=lambda a: asked.append(a.name) or TANK[a.name])
    assert len(asked) == 10 and oct(os.stat(tmp_path / usage.FILE).st_mode & 0o777) == "0o600"
    assert "- next turn tries ② first" in status.card(json.loads(json.dumps(later)), NOW + 61)
    two = lambda m: status.account_lines([{"name": "two", "meter": m, "rests_until": None, "bill": None}], NOW)[0]  # noqa: E731
    assert two(live(100, H, 50, 50 * H)) == f"{words.NOT_DONE} ② 5h {words.WARN}100% · wk 50%"     # a full window, no rest: red
    assert two(live(79, H, 80, 50 * H)) == f"- ② 5h 79% · wk {words.WARN}80%"                        # the warning sign from 80


def test_a_reset_and_the_end_of_a_rest_read_the_cards_short_way_in_every_renderer(monkeypatch):
    """The owner's yes of 2026-11-10 (one way to write a reset time, the status card's): at one moment the card, `yerk
    status --full`'s meter line, the morning diff's account line and /model's resting state write a reset and the end of
    a rest the same way, words.short_when's: '5:30a' within a day, 'Tue 4a' within a week, 'Oct 27' past that. They said
    '05:30', 'Tue' and 'Tue 04:00' before; a reset not read yet still says '?'. The morning diff's account line
    (meter_short) is held to the phone width: where the resets do not fit beside the meters they take a second line,
    the card's own, so no reset is ever cut off."""
    from yerk_ai.meters import meter_line, meter_short
    monkeypatch.setattr(time, "time", lambda: NOW)                                  # words.rest reads the clock itself
    m, rest, far, w = live(7, 3.5 * H, 3, 50 * H), NOW + 10 * H, NOW + 9 * 24 * H, words.short_when
    five, week = w(m["five_hour"]["resets"], NOW), w(m["seven_day"]["resets"], NOW)
    assert re.fullmatch(r"\d+:\d\d[ap]", five) and re.fullmatch(r"[A-Z][a-z]{2} \d+[ap]", week)   # within a day, a week
    card = status.account_lines([{"name": "one", "meter": m, "rests_until": None, "bill": None}], NOW)
    resting = status.account_lines([{"name": "one", "meter": m, "rests_until": rest, "bill": None}], NOW)
    assert card[1] == f"- ↻ 5h {five} · wk {week}" and resting[1] == f"- ↻ resting until {w(rest, NOW)}"   # as it was
    assert meter_line("one", {**m, "at": NOW}, NOW) == f"one: 5h 7% ({five}) · wk 3% ({week}), seen 0 min ago"
    assert words.rest(rest) == f"resting until {w(rest, NOW)}"
    later = {"five_hour": {"pct": 7}, "seven_day": {"pct": 3, "resets": far}, "at": NOW}   # no reset read; one 9 days off
    assert re.fullmatch(r"[A-Z][a-z]{2} \d+", w(far, NOW))
    assert meter_line("one", later, NOW) == f"one: 5h 7% (?) · wk 3% ({w(far, NOW)}), seen 0 min ago"
    assert meter_short("one", {"seven_day": m["seven_day"]}, now=NOW) == f"- one: wk 3% ({week})"   # a weekly window alone
    assert meter_short("one", m, now=NOW) == f"- one: 5h 7% ({five}) · wk 3% ({week})"              # both, where they fit
    assert meter_short("one", m, rest, NOW) == f"{words.NOT_DONE} one: resting until {w(rest, NOW)}"
    assert meter_short("one", later, now=NOW) == f"- one: 5h 7% · wk 3% ({w(far, NOW)})"           # 9 days off: its date
    hot = meter_short("three", live(93, 3.5 * H, 93, 50 * H), now=NOW)                                # too wide for one line
    assert hot == f"{words.WORKING} three: 5h 93% · wk 93%\n- ↻ 5h {five} · wk {week}"              # the card's two lines
    assert all(words.width(x) <= words.PHONE for x in hot.splitlines())                             # and nothing is cut
    back = NOW + 56 * H + 59 * 60                               # Tue 10:59a: as wide as a reset inside a week is ever said
    note = words.out_of("claude-opus-5-5", "claude-fable-5-2", back, NOW)      # the card's note when every account is out
    assert re.fullmatch(r"[A-Z][a-z]{2} \d\d:\d\d[ap]", w(back, NOW)) and note == f"on Opus..., Fable 5.2 back {w(back, NOW)}"
    assert words.line(f"{words.WORKING} {note}") == f"{words.WORKING} {note}"   # the card cuts nothing: the time is whole
    assert words.out_of("claude-opus-5-5", "claude-fable-5-2", NOW + 3.5 * H, NOW) == f"on Opus 5.5, Fable 5.2 back {five}"


def test_a_failed_live_read_shows_the_streams_reading_with_its_age_and_json_says_why(tmp_path):
    accounts = five_accounts(tmp_path)[:2]                                     # five and one
    (tmp_path / "meters.json").write_text(json.dumps({"one": {"at": NOW - 1.5 * H, "source": "stream",
                                                              "five_hour": {"pct": 42.0, "resets": NOW + 2 * H},
                                                              "seven_day": {"pct": 91.0, "resets": NOW - 60}}}))
    fail = {"five": live(3, 4 * H, 20, 50 * H, 5), "one": {"error": "no answer in 15 s"}}
    s = status.collect(tmp_path / "private", tmp_path, accounts, Cooldowns(None), Ledger(tmp_path / "ledger.sqlite"),
                       now=NOW, units=set, timers=Timers({}), live=True, read_one=lambda a: fail[a.name])
    lines = status.card(json.loads(json.dumps(s)), NOW)
    i = lines.index(f"{words.WORKING} ① 5h 42% · wk {words.WARN}91%")              # the stream's numbers
    assert lines[i + 1:i + 3] == [f"- ↻ 5h {words.short_when(NOW + 2 * H, NOW)} · 💳 {words.short_day(status.next_bill(8, NOW))}",
                                  "- ①: last reading 1 h 30 min ago"]       # a passed reset is not said
    assert s["accounts"][1]["meter"]["live_error"] == "no answer in 15 s"
    assert f"- wk next: {words.DONE}⑤ {words.short_when(NOW + 50 * H, NOW)}" in lines   # one reset ahead: no "then"
    young = json.loads((tmp_path / "meters.json").read_text())
    young["one"]["at"] = NOW - 600
    (tmp_path / "meters.json").write_text(json.dumps(young))
    s = status.collect(tmp_path / "private", tmp_path, accounts, Cooldowns(None), Ledger(tmp_path / "ledger.sqlite"),
                       now=NOW + 30, units=set, timers=Timers({}), live=True, read_one=lambda a: fail[a.name])
    assert "- ①: last reading 10 min ago" in status.card(json.loads(json.dumps(s)), NOW + 30)   # live asked: any age


def said(epoch):
    """A reset the way Claude Code 2.1.304's /usage says it."""
    t = datetime.fromtimestamp(epoch, ZoneInfo("America/Toronto"))
    return f"{t:%b} {t.day}, {t.hour % 12 or 12}{f':{t.minute:02d}' if t.minute else ''}{'am' if t.hour < 12 else 'pm'} (America/Toronto)"


def test_claude_codes_own_usage_on_the_folders_login_is_read_never_the_token(tmp_path, monkeypatch):
    soon, week = (time.time() // 600) * 600 + 3 * H, (time.time() // 3600) * 3600 + 80 * H      # resets sit on 10-minute marks
    soon, week = (t + H * datetime.fromtimestamp(t, ZoneInfo("America/Toronto")).fold for t in (soon, week))  # the hour a fall-back repeats reads as its first
    (tmp_path / "usage").mkdir()
    (tmp_path / "usage" / "cfg-a.txt").write_text(
        "You are currently using your subscription to power your Claude Code usage\n\n"
        f"Current session: 30% used · resets {said(soon)}\nCurrent week (all models): 72% used · resets {said(week)}\n"
        f"Current week (Fable): 100% used · resets {said(week)}\n\nWhat's contributing to your limits usage?\n")
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_USAGE_DIR", str(tmp_path / "usage"))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "record.jsonl"))
    (tmp_path / "a.token").write_text("sk-" + "ant-oat01-" + "x" * 40)
    a = Account("a", tmp_path / "cfg-a", token_file=tmp_path / "a.token")      # a one-year token: left out of this call
    assert usage.read(a, tmp_path) == {"error": "no config folder"} and not (tmp_path / "record.jsonl").exists()  # never made
    (tmp_path / "cfg-a").mkdir()
    (tmp_path / "cfg-b").mkdir()
    r = usage.read(a, tmp_path)
    assert r == {"five_hour": {"pct": 30.0, "resets": soon}, "seven_day": {"pct": 72.0, "resets": week},
                 "model": {"name": "Fable", "pct": 100.0, "resets": week}}
    rec = json.loads((tmp_path / "record.jsonl").read_text().splitlines()[0])
    assert rec["argv"][:2] == ["-p", "/usage"] and "--no-session-persistence" in rec["argv"] and "--strict-mcp-config" in rec["argv"]
    assert rec["config_dir"] == str(tmp_path / "cfg-a") and "CLAUDE_CODE_OAUTH_TOKEN" not in rec["env_keys"]
    assert usage.read(Account("b", tmp_path / "cfg-b"), tmp_path) == {"error": "no meters in what Claude Code printed"}
    monkeypatch.setenv("FAKE_CLAUDE_USAGE_SLOW", "3")
    assert usage.read(a, tmp_path, timeout=0.5) == {"error": "no answer in 0.5 s"}


def test_the_deploys_status_ask_and_json_never_start_claude_code_and_the_card_does(tmp_path, capsys, monkeypatch):
    started, sent = [], []
    monkeypatch.setattr(usage, "read", lambda a, cwd, timeout=usage.TIMEOUT: started.append(a.name) or {"error": "x"})
    monkeypatch.setattr(jobs, "post_control", lambda state, payload, timeout, path="": sent.append((payload, timeout)) or {})
    status.ask(tmp_path)                                                   # the deploy's proof and its quiet check
    status.ask(tmp_path, payload={"live": True})                           # the card's
    assert sent == [({}, 10.0), ({"live": True}, 10.0 + 2 * usage.TIMEOUT)]
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    asyncio.run(y.status_now({}))
    assert cli.main(["status", "--json", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    assert started == []                                                   # neither started Claude Code
    asyncio.run(y.status_now({"live": True}))
    assert sorted(started) == ["one", "two"]


def test_the_card_names_a_watched_channel_by_its_name_and_by_its_id_only_when_no_name_is_known(tmp_path):
    """F2-13: a room's watched channel on the card (`yerk status` and /status, both from status_now) is its name as the
    adapter knows it; its id only with no adapter, or a channel the adapter has no name for."""
    y, ref = Yerkd(EXAMPLE_PRIVATE, tmp_path), "discord:4242"        # a real id is 17 to 19 digits; the scrub test refuses one
    y.rooms = {"example-room": busy_room()}
    y.rooms["example-room"].conf["watch"] = ref
    y.link = SimpleNamespace(channel_name=lambda r: "watched" if r == ref else "")
    lines = status.card(json.loads(json.dumps(asyncio.run(y.status_now({})))), NOW)
    assert [x for x in lines if ": watches " in x] == ["- example-room: watches #watched"], lines
    for link in (None, SimpleNamespace(channel_name=lambda r: "")):
        y.link = link
        lines = status.card(json.loads(json.dumps(asyncio.run(y.status_now({})))), NOW)
        assert [x for x in lines if ": watches " in x] == ["- example-room: watches 4242"], lines


def test_the_status_help_names_every_part_the_card_prints(tmp_path):
    """F2-02: `yerk status --help` (and so docs/commands.md) names every part of the card, from words.STATUS_PARTS in the
    card's order; the fullest card (every part present) has no line that a named part does not make, so a part added to the
    card and not to the list fails here."""
    import argparse
    W = words
    s = json.loads(json.dumps(status.collect(EXAMPLE_PRIVATE, tmp_path, load_accounts(EXAMPLE_PRIVATE / "accounts.toml"),
                                             Cooldowns(tmp_path / "c.json"), Ledger(tmp_path / "l.sqlite"), {}, now=NOW, units=set)))
    s.update({"rooms": [{"room": "example-room", "busy": True, "waiting": 1, "watch": "discord:1", "since": NOW - 120, "account": "one"}],
              "workers": [{"id": "abc123", "room": "example-room", "account": "one", "started": NOW - 180, "cap": 900, "waiting": False,
                           "queued": None, "gone": False}], "jobs": [{"name": "backup", "next": NOW + 600}],
              "units": [{"name": "yerkd", "rss_mb": 120.0, "cpu_pct": 1.5}], "speed": {"first": 2.0, "turn": 30.0},
              "spend": {"n": 2, "usd": 0.375, "week": 1.875}, "backup": {"at": NOW - 3600, "bytes": 1000}, "addons": ["hello"]})
    tags, pre, mid = {W.tag(a["name"]) for a in s["accounts"]}, (lambda t: t.split("{", 1)[0]), (lambda t: t.split("}", 1)[1].split("{", 1)[0])
    made = [lambda x: x.startswith(pre(W.RUNNING_ROOMS)), lambda x: x.partition(":")[0] in tags or x.startswith(pre(W.FIRST_TRY)),
            lambda x: mid(W.TURN_FOR) in x, lambda x: mid(W.WATCHES) in x, lambda x: mid(W.PINNED) in x or x.startswith(pre(W.ROOMS_ON)),
            lambda x: x.startswith(pre(W.VERSION_AT)), lambda x: x.startswith(pre(W.ADDONS)),
            lambda x: x.startswith(pre(W.WORKER)) or x == W.NONE_RUNNING, lambda x: x.startswith(pre(W.JOB_DUE)) or x in (W.NO_JOB_DUE, W.NO_TIMERS),
            lambda x: x.endswith(W.UNIT_USE.rsplit("}", 1)[1]), lambda x: mid(W.SPEED) in x, lambda x: x.startswith(pre(W.SPEND)),
            lambda x: x.startswith(pre(W.BACKUP_AT)), lambda x: x.startswith(pre(W.STALLS))]
    lines = [x.split(" ", 1)[1] for x in status.card(s, NOW)]
    part = [next((i for i, f in enumerate(made) if f(x)), None) for x in lines]
    assert None not in part and part == sorted(part) and set(part) == set(range(len(made))), list(zip(part, lines))
    sub = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction))
    said, parts = {c.dest: str(c.help) for c in sub._choices_actions}["status"], getattr(W, "STATUS_PARTS", ())
    assert len(parts) == len(made) and all(x in said for x in parts), (said, parts)


def test_a_status_read_that_does_not_ask_never_waits_on_a_card_that_does(tmp_path):
    accounts, gate, began, got = five_accounts(tmp_path)[:1], threading.Event(), threading.Event(), []

    def slow(a):                                                           # a live card's Claude Code, still running
        began.set()
        gate.wait(10)
        return TANK[a.name]
    card = threading.Thread(target=usage.readings, args=(accounts, tmp_path, True, NOW, slow))
    card.start()
    assert began.wait(5)
    proof = threading.Thread(target=lambda: got.append(usage.readings(accounts, tmp_path, False, NOW)))
    proof.start()
    proof.join(2)
    waited = proof.is_alive()
    gate.set()
    card.join(10)
    proof.join(10)
    assert not waited and got == [{}]                                      # answered at once, from the file as it stood
    assert usage.readings(accounts, tmp_path, False, NOW) == {"five": {**TANK["five"], "at": NOW, "source": "live"}}


def test_rooms_put_on_one_account_are_one_line_and_a_lone_room_keeps_its_own():
    def pinned(rooms):
        s = {"at": NOW, "running": True, "units": [], "workers": [], "jobs": [], "accounts": [], "stalls": [],
             "rooms": [{"room": n, "busy": False, "waiting": 0, "since": None, "account": a} for n, a in rooms]}
        return [x for x in status.card(s, NOW) if "on account" in x]
    assert pinned([("yerk-test", "four"), ("yerk-ops", "four"), ("club", "one"), ("free", "")]) == [
        "- on account ④: yerk-test, yerk-ops", "- club: on account ①"]
    six = [(f"{n}-on-yerk", "four") for n in ("harbor", "main", "family", "club", "test", "ops")]
    assert pinned(six) == ["- on account ④: 6 rooms"]                      # the names would not fit


def test_a_reset_is_read_in_its_zone_and_rolls_into_the_next_year_and_a_bill_day_is_a_date():
    eve = datetime(2026, 12, 30, 12, tzinfo=ZoneInfo("America/Toronto")).timestamp()
    assert usage.reset_at("Jan 2, 3am (America/Toronto)", eve) == datetime(2027, 1, 2, 3, tzinfo=ZoneInfo("America/Toronto")).timestamp()
    assert usage.reset_at("5:10pm (America/Toronto)", eve) == datetime(2026, 12, 30, 17, 10, tzinfo=ZoneInfo("America/Toronto")).timestamp()
    assert usage.reset_at("11am (America/Toronto)", eve) == datetime(2026, 12, 31, 11, tzinfo=ZoneInfo("America/Toronto")).timestamp()
    assert usage.reset_at("11:59pm (America/Toronto)", eve) == datetime(2026, 12, 31, tzinfo=ZoneInfo("America/Toronto")).timestamp()
    assert usage.reset_at("Jan 2, 3am (Asia/Tokyo)", eve) == datetime(2027, 1, 2, 3, tzinfo=ZoneInfo("Asia/Tokyo")).timestamp()
    assert usage.reset_at("soon", eve) is None and usage.parse("Total cost: $0.0000\n", eve) == {}
    day = lambda *t: datetime(*t).timestamp()                                  # noqa: E731
    assert status.next_bill(8, day(2026, 11, 8, 15)) == day(2026, 11, 8)        # the day itself: today
    assert status.next_bill(7, day(2026, 11, 8, 15)) == day(2026, 12, 7) and status.next_bill(None, day(2026, 11, 8)) is None
    assert status.next_bill(30, day(2027, 2, 10)) == day(2027, 2, 28) and status.next_bill(31, day(2026, 12, 31, 9)) == day(2026, 12, 31)
    assert words.short_day(day(2026, 11, 8)) == "Nov 8" and words.tag("seven") == "⑦" and words.tag("gpu-box") == "gpu-box"
    assert [words.short_clock(day(2026, 11, 8, h, m)) for h, m in ((0, 0), (14, 0), (17, 10), (12, 30))] == ["12a", "2p", "5:10p", "12:30p"]


def test_the_status_endpoint_answers_and_yerk_status_renders_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))                     # the card's /usage call, never the real one
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    y.sessions.bind("example-room", "s-1", "one", token="room-token")
    h = mcp_server.HEADER
    app = mcp_server.app(mcp_server.build(y, y.port, y.hook, y.control, y.status_now), y.check())  # as serve_http
    with TestClient(app) as c:
        refused = [c.post("/status", json={}, headers=hd).status_code
                   for hd in ({}, {h: y.worker_secret}, {h: "room-token"}, {h: "wrong"})]
        hook_by_room = c.post("/hook", json={}, headers={h: "room-token"}).status_code
        r = c.post("/status", json={}, headers={h: y.secret})
    assert refused == [401, 401, 401, 401] and hook_by_room == 200   # a room's token opens its own door only
    s = r.json()
    assert r.status_code == 200 and s["running"] and s["rooms"] == [{"room": "example-room", "busy": False,
                                                                     "waiting": 0, "watch": None, "since": None, "account": "",
                                                                     "fast": False, "head": {
        "model": "claude-fable-5-1", "effort": "", "account": "one", "context": None, "window": 250000, "compactions": 0}}]
    assert s["units"][0]["name"] == "yerkd" and s["units"][0]["rss_mb"] > 0 and s["stalls"] == [] and s["started"]
    assert [a["name"] for a in s["accounts"]] == ["one", "two"] and s["accounts"][0]["meter"]["error"]

    secret_file = tmp_path / "http.secret"
    secret_file.write_text("install-secret\n")

    class Answer(BaseHTTPRequestHandler):           # answers only the install's secret, as the Guard does
        def do_POST(self):
            ok = self.headers.get(h) == "install-secret"
            body = json.dumps(s if ok else {"ok": False}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = HTTPServer(("127.0.0.1", 0), Answer)
    threading.Thread(target=server.handle_request, daemon=True).start()
    st = {"pid": os.getpid(), "port": server.server_port, "secret_file": str(secret_file)}
    (tmp_path / "yerkd.json").write_text(json.dumps(st))
    assert cli.main(["status", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    out = capsys.readouterr().out.splitlines()
    server.server_close()
    assert out[0] == "- yerkd running: example-room" and any(x.startswith("- yerkd: ") for x in out)
    assert all(words.width(x) <= words.PHONE for x in out)
    (tmp_path / "yerkd.json").write_text(json.dumps(st))
    assert cli.main(["status", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0   # alive, silent
    assert capsys.readouterr().out.splitlines()[0] == f"{words.NOT_DONE} yerkd: running, not answering"


def test_the_beat_says_ready_and_beats_through_systemds_socket_only_with_a_watchdog(tmp_path, monkeypatch):
    folder = Path(tempfile.mkdtemp(prefix="yb-"))               # a socket path short enough for AF_UNIX
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(folder / "notify"))
    sock.settimeout(3)
    monkeypatch.setenv("NOTIFY_SOCKET", str(folder / "notify"))

    def run(b):
        async def go():
            ticker = asyncio.create_task(b.tick())
            await asyncio.sleep(0.45)
            ticker.cancel()
        asyncio.run(go())
        time.sleep(0.1)
        got = []
        sock.settimeout(0.2)
        try:
            while True:
                got.append(sock.recv(100).decode())
        except TimeoutError:
            return got
    assert run(beat.Beat(Ledger(tmp_path / "l.sqlite"), tmp_path / "stalls", every=0.1)) == ["READY=1"]   # no WatchdogSec
    monkeypatch.setenv("WATCHDOG_USEC", "4000000")
    got = run(beat.Beat(Ledger(tmp_path / "l.sqlite"), tmp_path / "stalls", every=0.1))
    assert got[0] == "READY=1" and got.count("WATCHDOG=1") >= 2 and set(got) == {"READY=1", "WATCHDOG=1"}
    sock.close()
    shutil.rmtree(folder)


def test_a_loop_held_3_s_stops_the_beat_and_leaves_a_stall_row_a_dump_and_a_red_line(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)                        # a test daemon: no channel adapter attached
    sent = []
    y.beat.send = lambda m: sent.append((time.monotonic(), m)) or True
    y.beat.watchdog = True
    held = {}

    async def go():
        ticker = asyncio.create_task(y.beat.tick())
        await asyncio.sleep(1.5)
        held["from"] = time.monotonic()
        time.sleep(3)                                           # the loop held on purpose
        held["to"] = time.monotonic()
        await asyncio.sleep(1.6)
        ticker.cancel()
    asyncio.run(go())
    rows = y.ledger.stalls(0)
    assert len(rows) == 1 and 2.0 < rows[0]["seconds"] <= 3.5 and rows[0]["doing"].endswith(" in go"), rows
    dump = Path(rows[0]["dump"])
    assert dump.parent == tmp_path / "stalls" and not dump.resolve().is_relative_to(y.memory.root.resolve())
    text = dump.read_text()
    assert "still running" in text and "time.sleep(3)" in text and "asyncio tasks:" in text and "\n  yerkd " in text
    assert time.mktime(time.strptime(dump.stem, "%Y%m%d-%H%M%S")) <= rows[0]["began"] + 1
    beats = [t for t, m in sent if m == "WATCHDOG=1"]
    assert sent[0][1] == "READY=1" and len(beats) >= 2
    assert not [t for t in beats if held["from"] + 0.5 < t < held["to"]]          # no beat while the loop was held
    lines = status.card(status.collect(EXAMPLE_PRIVATE, tmp_path, y.accounts, y.cooldowns, y.ledger, units=set,
                                       timers=Timers({})))
    assert lines[-1].startswith(f"{words.NOT_DONE} stalls today: 1, longest 2.") or \
        lines[-1].startswith(f"{words.NOT_DONE} stalls today: 1, longest 3.")


def test_dumps_never_land_in_the_memory_tree():
    with tempfile.TemporaryDirectory() as d:
        memory_root = Path(d) / "private" / "memory"
        assert beat.folder(Path(d) / "state", memory_root) == Path(d) / "state" / "stalls"
        assert not beat.folder(memory_root / "state", memory_root).is_relative_to(memory_root)


def layer(tmp_path, machine="example-quiet"):
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    state.mkdir()
    if machine:
        rt = priv / "rooms.toml"
        rt.write_text(rt.read_text().replace(f"[rooms.{machine}]\n", f"[rooms.{machine}]\nrole = \"machine\"\n"))
    return priv, state, load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json")


def test_a_planted_payload_change_shows_up_the_next_morning_in_the_machine_room_only(tmp_path, capsys):
    priv, state, accounts, cool = layer(tmp_path)
    posts = []

    def post(p):
        posts.append(p["morning"])
        return {"ok": True, "message_ids": ["m1"]}
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW) == 0
    assert re.search(r"^- example-room: [\d,]+, [\d,]+ left$", posts[0]["text"], re.M)          # as sent and its gap to the total
    (priv / "rulebook.md").write_text("x" * 500 + (priv / "rulebook.md").read_text())     # planted
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW + 86400) == 0
    text = posts[1]["text"].splitlines()
    assert text[0] == f"{words.NOT_DONE} morning: 2 new since the last diff"          # the alert opens the diff
    assert set(text[1:3]) == {f"{words.WORKING} example-room rulebook: +500 chars",
                              f"{words.WORKING} example-quiet rulebook: +500 chars"}
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW + 2 * 86400) == 0
    assert posts[2]["text"].splitlines()[0] == "- morning: nothing new, 4 known"     # the blind detectors, curate's (a doctor never run: none)
    assert {p["room"] for p in posts} == {"example-quiet"}                            # the machine room, no other
    assert all(words.width(x) <= words.PHONE for p in posts for x in p["text"].splitlines())
    assert (state / "morning" / time.strftime("%Y-%m-%d.txt", time.localtime(NOW))).is_file()


def test_the_morning_diff_reads_claude_codes_parts_as_sent_says_the_gap_and_names_a_part_that_grew_or_passed_its_line(tmp_path):
    """measured-budgets (2026-10-24): each room's line is its session start as sent, Claude Code's parts from its newest
    transcript (context.sent), and what is left under the total; a Claude Code part that grew is a finding the next
    morning, and one past its line is red while it lasts (the deploy's gate refuses it)."""
    priv, state, accounts, cool = layer(tmp_path)
    b, posts, t = budgets(), [], tmp_path / "s1.jsonl"
    line = b["session_start"]["claude_code"]["mcp__hands__"]

    def sends(n):           # the room's newest shape: a prompt of 1,000 and the plug-in's one tool at n characters as sent
        tool = {"name": "mcp__hands__browser", "description": ""}
        tool["description"] = "x" * (n - context.chars(json.dumps(tool)))
        shape = {"system": [{"type": "text", "text": "p" * 999 + "\n# Yerk AI mechanics"}], "tools": [tool]}
        t.write_text(json.dumps({"type": "api-request-shape", "shape": shape}) + "\n")
    (state / "sessions.json").write_text(json.dumps({"example-room": {"session_id": "s1", "transcript": str(t)}}))

    def post(p):
        posts.append(p["morning"]["text"].splitlines())
        return {"ok": True}
    sends(3000)
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW) == 0
    sizes = morning.sizes(priv, state)["example-room"]
    assert (sizes["claude_code_prompt"], sizes["mcp__hands__"], sizes["builtin_tools"]) == (1000, 3000, 0)
    size = context.start_size(sizes)
    assert words.named(f"{words.PLAIN} {words.PAYLOAD}", "example-room", size=size, left=b["session_start"]["total"] - size) in posts[0]
    sends(3050)
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW + 86400) == 0
    assert f"{words.WORKING} example-room mcp__hands__: +50 chars" in posts[1]
    sends(line + 1)
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW + 2 * 86400) == 0
    assert words.named(f"{words.NOT_DONE} {words.OVER_PART}", "example-room mcp__hands__", size=line + 1, line=line) in posts[2]
    assert all(words.width(x) <= words.PHONE for p in posts for x in p)


def test_a_failed_post_stamps_nothing_so_the_change_shows_again(tmp_path, capsys):
    priv, state, accounts, cool = layer(tmp_path)
    ok = lambda p: {"ok": True}                                                       # noqa: E731
    assert morning.run(priv, state, accounts, cool, post=ok, now=NOW) == 0
    (priv / "persona.md").write_text("y" * 40 + (priv / "persona.md").read_text())

    def down(p):
        raise OSError("connection refused")
    assert morning.run(priv, state, accounts, cool, post=down, now=NOW + 86400) == 1
    assert "not posted to example-quiet" in capsys.readouterr().err
    seen = []
    assert morning.run(priv, state, accounts, cool, post=lambda p: seen.append(p) or {"ok": True}, now=NOW + 86400) == 0
    assert f"{words.WORKING} example-room persona: +40 chars" in seen[0]["morning"]["text"]


def test_a_rule_filed_since_the_last_diff_is_named_as_each_rooms_rules_part(tmp_path):
    """The install's rules file is a part of its own, out of the rulebook: a rule filed overnight shows in the morning
    diff as the rules part of every room that reads the file, and no rulebook moves."""
    priv, state, accounts, cool = layer(tmp_path)
    posts = []
    post = lambda p: posts.append(p["morning"]["text"]) or {"ok": True}                 # noqa: E731
    (priv / "memory" / "rules.md").write_text("# Rules\n\n- a job: done this way.\n")
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW) == 0
    with (priv / "memory" / "rules.md").open("a") as f:
        f.write("- another job: done that way.\n")                                       # 30 characters with its line break
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW + 86400) == 0
    lines = posts[1].splitlines()
    assert {f"{words.WORKING} example-room rules: +30 chars", f"{words.WORKING} example-quiet rules: +30 chars"} <= set(lines)
    assert not any(" rulebook: " in x for x in lines)


def test_with_no_machine_room_the_diff_goes_to_a_file_and_one_line_says_where(tmp_path, capsys):
    priv, state, accounts, cool = layer(tmp_path, machine=None)
    assert morning.run(priv, state, accounts, cool, post=lambda p: 1 / 0, now=NOW) == 0     # never posted
    path = state / "morning" / time.strftime("%Y-%m-%d.txt", time.localtime(NOW))
    err = capsys.readouterr().err.strip().splitlines()
    assert err == [f"morning: no room in rooms.toml has role = \"machine\"; the diff is at {path}"]
    assert path.read_text().startswith(f"{words.NOT_DONE} morning: ") and (state / "morning" / "last.json").is_file()


def test_the_morning_diff_reads_yesterday_from_the_ledger_and_says_unknown_when_blind(tmp_path):
    priv, state, accounts, cool = layer(tmp_path, machine=None)
    led = Ledger(state / "ledger.sqlite")
    y0 = status.midnight(NOW, 1)
    for i, (wait, first) in enumerate([(0.5, 1.5), (2.5, 4.0), (0.0, 2.0)]):
        at = y0 + 3600 + i * 60
        led.inbound_add(f"m{i}", "example-room", "c", "owner", 5)
        tid = led.record(room="example-room", started=at + wait, first_visible=at + first, ended=at + 9, outcome="ok")
        led.inbound_set(f"m{i}", received=at, turn_id=tid)
    led.stall(began=y0 + 7200, seconds=3.4, doing="room.py:300 in _run", dump="")
    led.stall(began=NOW - 60, seconds=9.0, doing="today's, not yesterday's", dump="")
    led.shadow(t=y0 + 100, room="example-room", rule="unread-figure", detail="", reply_chars=10)
    live = {"started": y0 - 10, "units": [{"name": "yerkd", "rss_mb": 84.6, "cpu_pct": 0.31}]}
    lines, stamp = morning.build(priv, state, accounts, cool, led, live, NOW)
    assert "- yesterday: 3 messages answered" in lines and "- queue wait: 0.5 s typical, 2.5 s worst" in lines
    assert "- first line: 2.0 s typical, 4.0 s worst" in lines and "- yerkd: 85 MB, 0.3% cpu" in lines
    assert f"{words.NOT_DONE} stalls yesterday: 1, longest 3.4 s" in lines
    assert f"{words.WORKING} shadow flag unread-figure: 1" in lines
    assert stamp["findings"] == ["stalls", "shadow unread-figure", "unknown curate"]   # no curate run; the doctor's never ran:
    empty = Ledger(state / "empty.sqlite")
    blind, _ = morning.build(priv, state, accounts, cool, empty, None, NOW)
    assert f"{words.NOT_DONE} stalls: unknown, yerkd not answering" in blind      # a blind detector says so
    assert f"{words.NOT_DONE} memory: unknown, yerkd not answering" in blind
    partly, _ = morning.build(priv, state, accounts, cool, empty, {"started": y0 + 50000, "units": []}, NOW)
    assert f"- stalls yesterday: none since {words.clock(y0 + 50000, NOW)}" in partly


def test_the_daemon_posts_the_diff_into_a_machine_room_and_refuses_any_other(tmp_path, capsys):
    priv, state, *_ = layer(tmp_path)
    y = Yerkd(priv, state)
    y.add_room("example-room", ConsoleChannel())
    y.add_room("example-quiet", ConsoleChannel())
    no = asyncio.run(y.control({"morning": {"room": "example-room", "text": "- morning: nothing new", "key": "k1"}}))
    yes = asyncio.run(y.control({"morning": {"room": "example-quiet", "text": "- morning: nothing new", "key": "k2"}}))
    assert no == {"ok": False, "error": "no machine room 'example-room' runs here"}
    assert yes["ok"] and yes["message_ids"] and capsys.readouterr().out.count("morning: nothing new") == 1


def test_the_unit_example_runs_yerkd_under_notify_with_a_watchdog_and_says_why():
    unit = (REPO / "units" / "yerkd.service").read_text()
    assert "Type=notify" in unit and "WatchdogSec=30" in unit and "Without them" in unit
    assert "OnCalendar=*-*-* 06:30:00" in (REPO / "units" / "yerk-morning.timer").read_text()


def test_the_nightly_rules_pass_is_an_example_job_that_parses_and_the_status_card_lists_when_due(tmp_path):
    """`yerk curate --rules` as a jobs.toml job (examples/private/jobs.toml): a script at 04:30, silent, no account (the pass is
    code); due within the hour, `yerk status` names it like any job."""
    found, bad = jobs.load(EXAMPLE_PRIVATE / jobs.FILE, NOW)
    job = found["example-curate-rules"]
    assert "example-curate-rules" not in bad and (job.kind, job.when, job.spec, job.deliver, job.account) == ("script", "calendar", "*-*-* 04:30", "silent", "")
    assert job.payload.startswith("yerk curate --rules")
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, [], Cooldowns(tmp_path / "c.json"), Ledger(tmp_path / "l.sqlite"), now=NOW, units=set,
                       timers=Timers({f"{jobs.UNIT}example-curate-rules.timer": {"next": NOW + 1200, "last": None}}))
    assert "- job example-curate-rules: in 20 min" in status.card(json.loads(json.dumps(s)), NOW)
