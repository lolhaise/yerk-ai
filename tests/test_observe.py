"""Checks how yerkd reports on itself: the status socket and the card, the watchdog beat and its stall
rows, the morning diff. /proc and systemd are faked where it matters; the stall is a real event loop held
for 3 s in a test daemon on a temp state folder with no channel adapter attached. Layer 7, run 1."""
import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from yerk_ai import beat, cli, jobs, mcp_server, morning, status, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE, REPO
from yerk_ai.daemon import ConsoleChannel, Yerkd
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

    def active(self, unit):
        return "active"


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
    assert lines[1].startswith(f"{words.WORKING} one: 5h 95% (") and lines[2].startswith(f"{words.NOT_DONE} two: resting")
    assert "- example-room: busy 2 min, 1 waiting" in lines            # in flight, since when
    assert f"{words.NOT_DONE} example-quiet: busy 15 min" in lines      # past 10 minutes: a blocked room
    assert "- worker abc123: 3 min of 15" in lines and f"{words.NOT_DONE} worker def456: gone, no result" in lines
    assert "- job backup: in 10 min" in lines
    assert "- example-room: 100 MB, 6.0% cpu" in lines and any(x.startswith("- yerkd: 80 MB") for x in lines)
    assert lines[-1] == f"{words.NOT_DONE} stalls today: 1, longest 3.2 s"
    reds = [x for x in lines if x.startswith(words.NOT_DONE)]
    assert len(reds) == 4                                                   # resting, blocked, gone, stall: no other
    quiet = status.card(status.collect(EXAMPLE_PRIVATE, tmp_path / "q", [], cool, Ledger(tmp_path / "q.sqlite"),
                                       units=set, timers=Timers()), now)
    assert quiet == [f"{words.NOT_DONE} yerkd: not running", "- workers: none running",
                     f"{words.NOT_DONE} jobs: the timer list is unreadable", "- stalls today: none"]


def test_a_room_put_on_an_account_says_so_on_the_card_and_a_room_on_its_order_says_nothing_new(tmp_path):
    for room, account in (("example-room", "two"), ("example-quiet", "gone")):    # gone: off accounts.toml
        (tmp_path / "rooms" / room).mkdir(parents=True)
        (tmp_path / "rooms" / room / "override.json").write_text(json.dumps({"account": account}))
    rooms = {"example-room": busy_room(), "example-quiet": busy_room(), "third": busy_room()}
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, load_accounts(EXAMPLE_PRIVATE / "accounts.toml"), Cooldowns(None),
                       Ledger(tmp_path / "ledger.sqlite"), rooms, units=set, timers=Timers({}))
    lines = status.card(json.loads(json.dumps(s)))
    assert [r["account"] for r in s["rooms"]] == ["two", "", ""]
    assert [x for x in lines if "account" in x] == ["- example-room: on account two"]   # the pin, and only the pin
    assert all(words.width(x) <= words.PHONE and x[:1] in (*DOTS, words.PLAIN) for x in lines), lines


def test_the_status_endpoint_answers_and_yerk_status_renders_it(tmp_path, capsys):
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
                                                                     "waiting": 0, "watch": None, "since": None, "account": ""}]
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
    assert "- example-room: " in posts[0]["text"] and "chars, first map" in posts[0]["text"]
    (priv / "rulebook.md").write_text("x" * 500 + (priv / "rulebook.md").read_text())     # planted
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW + 86400) == 0
    text = posts[1]["text"].splitlines()
    assert text[0] == f"{words.NOT_DONE} morning: 2 new since the last diff"          # the alert opens the diff
    assert set(text[1:3]) == {f"{words.WORKING} example-room rulebook: +500 chars",
                              f"{words.WORKING} example-quiet rulebook: +500 chars"}
    assert morning.run(priv, state, accounts, cool, post=post, now=NOW + 2 * 86400) == 0
    assert posts[2]["text"].splitlines()[0] == "- morning: nothing new, 3 known"     # the three blind detectors
    assert {p["room"] for p in posts} == {"example-quiet"}                            # the machine room, no other
    assert all(words.width(x) <= words.PHONE for p in posts for x in p["text"].splitlines())
    assert (state / "morning" / time.strftime("%Y-%m-%d.txt", time.localtime(NOW))).is_file()


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
    assert stamp["findings"] == ["stalls", "shadow unread-figure"]
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
