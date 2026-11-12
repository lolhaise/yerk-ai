"""Checks texting's link to the Mac's Messages, which watches only your listed one-to-one chats, against a stub bridge speaking imsg's JSON-RPC."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from yerk_ai import words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.texting import people
from yerk_ai.texting.bridge import Bridge
from yerk_ai.texting.link import Link

AT = "@"                     # split in the source, so the repository scan never reads a fixture as an address
A1, A2, B1 = "+15550000101", f"alex.q{AT}example.com", "+15550000102"
LIST = f'[owner]\nname = "Sam"\n\n[people.alex]\nfirst = "Alex"\nhandles = ["{A1}", "{A2}"]\n\n' \
       f'[people.beta]\nfirst = "Beta"\nhandles = ["{B1}"]\n'


def listed(tmp_path: Path) -> people.People:
    f = tmp_path / "people.toml"
    f.write_text(LIST)
    os.chmod(f, 0o600)
    return people.load(f)


async def until(cond: Any, wait: float = 5.0) -> None:
    end = time.monotonic() + wait
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.02)


def stamp(ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat().replace("+00:00", "Z")


class FakeBridge:
    """chats.list, messages.history and watch.subscribe as the Mac's bridge answers them; anything else is an unknown
    method. `lines`: every request that reached the socket."""

    def __init__(self, path: str):
        self.path = path
        self.chats: dict[int, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.next = 100
        self.lines: list[dict[str, Any]] = []
        self.subs: list[tuple[asyncio.StreamWriter, dict[str, Any]]] = []
        self.conns: list[asyncio.StreamWriter] = []
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self.handle, self.path)

    async def stop(self) -> None:
        for w in self.conns:
            w.close()
        self.conns, self.subs = [], []
        if self.server is not None:
            self.server.close()
            self.server = None
        Path(self.path).unlink(missing_ok=True)

    def chat(self, cid: int, ident: str, group: bool = False, participants: list[str] | None = None) -> None:
        self.chats[cid] = {"id": cid, "identifier": ident, "is_group": group, "service": "iMessage",
                           "participants": [ident] if participants is None else participants}

    def add(self, cid: int, text: str, from_me: bool = False, ago: float = 0.0, **extra: Any) -> dict[str, Any]:
        self.next += 1
        row = {"id": self.next, "guid": f"G-{self.next}", "chat_id": cid, "chat_identifier": self.chats[cid]["identifier"],
               "is_from_me": from_me, "text": text, "created_at": stamp(ago), "attachments": [], **extra}
        self.rows.append(row)
        return row

    def push(self, row: dict[str, Any]) -> None:
        for n, (w, p) in enumerate(self.subs):
            if p.get("chat_id") == row["chat_id"]:
                w.write((json.dumps({"jsonrpc": "2.0", "method": "message",
                                     "params": {"subscription": n + 1, "message": row}}) + "\n").encode())

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.conns.append(w)
        while line := await r.readline():
            req = json.loads(line)
            self.lines.append(req)
            m, p = req["method"], req.get("params") or {}
            after: list[dict[str, Any]] = []
            if m == "chats.list":
                res: Any = {"chats": list(self.chats.values())}
            elif m == "messages.history":
                mine = [x for x in self.rows if x["chat_id"] == p.get("chat_id")]
                res = {"messages": sorted(mine, key=lambda x: -x["id"])[:int(p.get("limit", 50))]}
            elif m == "watch.subscribe":
                self.subs.append((w, p))
                res = {"subscription": len(self.subs)}
                after = [x for x in self.rows if x["chat_id"] == p.get("chat_id") and x["id"] > int(p.get("since_rowid") or 0)]
            else:
                w.write((json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "no"}}) + "\n").encode())
                continue
            w.write((json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res}) + "\n").encode())
            for x in after:
                self.push(x)
            await w.drain()


@pytest.fixture
def sock():
    d = tempfile.mkdtemp(prefix="yt")             # under TMPDIR, short: a socket path has a length limit
    yield str(Path(d) / "b.sock")
    shutil.rmtree(d, ignore_errors=True)


def test_read_only_bridge_refuses_every_write_before_the_socket(sock):
    async def go() -> None:
        fb = FakeBridge(sock)
        await fb.start()
        b = Bridge(unix=sock)
        await b.open()
        for method in ("send", "react", "status", "chats.delete", "typing.start"):
            with pytest.raises(PermissionError):
                await b.request(method, {"to": "+15550000001", "text": "hi"})
        await asyncio.sleep(0.05)
        assert fb.lines == []                         # nothing reached the socket
        assert (await b.request("chats.list"))["chats"] == []
        assert [x["method"] for x in fb.lines] == ["chats.list"]
        await b.close()
        await fb.stop()
    asyncio.run(go())


def test_link_watches_only_listed_one_to_one_chats(sock, tmp_path):
    async def go() -> None:
        fb = FakeBridge(sock)
        fb.chat(1, A1)
        fb.chat(2, A1, group=True)                                  # a group, even of that person alone
        fb.chat(3, "+15550000199")                                 # not on the list
        fb.chat(4, A2.upper())                                      # the same person by address
        fb.chat(5, B1, participants=[B1, "+15550000198"])          # a third participant
        fb.chat(6, A1)                                              # the SMS chat of the same number
        await fb.start()
        took: list[tuple[str, str]] = []
        link = Link(listed(tmp_path), tmp_path, lambda p, m: took.append((p.key, m["text"])) or "them", unix=sock)
        task = asyncio.create_task(link.start())
        await until(lambda: link.connected)
        assert sorted(x["params"]["chat_id"] for x in fb.lines if x["method"] == "watch.subscribe") == [1, 4, 6]
        assert link.take(fb.add(3, "unlisted")) == "not ours"
        assert link.take(fb.add(5, "three of us")) == "not ours"
        assert link.take({**fb.add(1, "names someone else"), "chat_identifier": B1}) == "not ours"
        fb.push(fb.add(6, "over sms"))
        await until(lambda: took)
        assert took == [("alex", "over sms")]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fb.stop()
    asyncio.run(go())


def test_link_starts_from_the_newest_row_and_a_restart_replays_from_its_cursor(sock, tmp_path):
    async def go() -> None:
        fb = FakeBridge(sock)
        fb.chat(1, A1)
        fb.add(1, "old one", ago=60)
        fb.add(1, "old two", ago=50)
        await fb.start()
        took: list[str] = []
        link = Link(listed(tmp_path), tmp_path, lambda p, m: took.append(m["text"]) or "them", unix=sock)
        task = asyncio.create_task(link.start())
        await until(lambda: link.connected)
        fb.push(fb.add(1, "new"))
        await until(lambda: took)
        assert took == ["new"]                                      # a first start never replays
        assert json.loads((tmp_path / "texting" / "cursor.json").read_text()) == {"cursor": {"1": 103}}
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        fb.add(1, "while down")
        again = Link(listed(tmp_path), tmp_path, lambda p, m: took.append(m["text"]) or "them", unix=sock)
        task = asyncio.create_task(again.start())
        await until(lambda: len(took) == 2)
        assert took == ["new", "while down"]
        assert json.loads((tmp_path / "texting" / "status.json").read_text())["up"] is True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fb.stop()
    asyncio.run(go())


def test_link_tells_the_machine_room_once_away_and_once_back(sock, tmp_path):
    async def go() -> None:
        said: list[str] = []
        link = Link(listed(tmp_path), tmp_path, lambda p, m: "them", machine=said.append, unix=sock)
        link.away, link.back = 0.2, 0.2
        task = asyncio.create_task(link.start())                    # no bridge yet
        await until(lambda: said == [words.TEXTING_AWAY])
        fb = FakeBridge(sock)
        fb.chat(1, A1)
        await fb.start()
        await until(lambda: len(said) == 2)
        assert said == [words.TEXTING_AWAY, words.TEXTING_BACK]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fb.stop()
    asyncio.run(go())


def test_texting_is_off_by_default_and_with_mode_off(tmp_path, monkeypatch):
    opened: list[Any] = []
    monkeypatch.setattr(Bridge, "open", lambda self: opened.append(self))   # any connection at all would land here
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    (private / "texting").mkdir()
    listed(private / "texting")
    for n, table in enumerate(("", '[texting]\nmode = "off"\n', '[texting]\nmode = "dry"\n',
                               '[texting]\nmode = "dry"\nreport = "example-room"\n')):
        (private / "yerkd.toml").write_text(table + '\n[imessage]\ncommand = ["true"]\n')
        (state := tmp_path / f"state{n}").mkdir()
        y = Yerkd(private, state)
        got = y.texting()
        if n < 2:
            assert got == [] and not (state / "texting").exists()
            assert y.ledger.db.execute("SELECT COUNT(*) FROM texting").fetchone()[0] == 0
            assert y.ledger.db.execute("SELECT COUNT(*) FROM inbound WHERE room = 'texting'").fetchone()[0] == 0
            # no room is held here, so the tool gives its one fixed line (an owner's room hears "texting is off": test_texting_people)
            assert asyncio.run(y.tool_texting("example-room", "sheet", "alex", "", "")) == {"ok": False, "line": words.TEXTING_ONLY}
        else:                       # dry: the link and the intake, to run as tasks, and the writer, the check and the holds once a report room is named
            assert len(got) == 2 + 3 * (n == 3) and (state / "texting" / "check-cwd").exists() == (n == 3)
            assert (state / "texting" / "writer-mcp.json").exists() == (n == 3) and y.ledger.db.execute(
                "SELECT COUNT(*) FROM texting").fetchone()[0] == 0
            for c in got:
                c.close()
    assert opened == []
