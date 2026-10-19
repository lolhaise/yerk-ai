"""The iMessage adapter against a fake bridge: a local Unix socket speaking imsg's JSON-RPC lines."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from yerk_ai import words
from yerk_ai.channels import imessage as imod
from yerk_ai.channels.imessage import LIMIT, Bridge, IMessageChannel
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.split import split_message

CHAT, OTHER_CHAT = "test-thread", "someone-else"


def stamp(ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat().replace("+00:00", "Z")


class FakeBridge:
    """chats.list, messages.history, watch.subscribe and send, as the Mac's bridge answers them; a
    send's row is pushed to the watchers before the send is answered (the order the race needs)."""

    def __init__(self, path: str, guid_in_answer: bool = True):
        self.path, self.guid_in_answer = path, guid_in_answer
        self.answer_sends = True
        self.chats = {1: CHAT, 2: OTHER_CHAT}
        self.rows: list[dict[str, Any]] = []
        self.next = 100
        self.sent: list[str] = []
        self.calls: list[str] = []
        self.subs: list[tuple[asyncio.StreamWriter, dict[str, Any]]] = []
        self.conns: list[asyncio.StreamWriter] = []
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self.handle, self.path)

    async def stop(self) -> None:
        """The Mac goes away: the socket closes and every connection with it."""
        for w in self.conns:
            w.close()
        await asyncio.gather(*(w.wait_closed() for w in self.conns), return_exceptions=True)
        self.conns, self.subs = [], []
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        Path(self.path).unlink(missing_ok=True)

    def add(self, chat: int, text: str, from_me: bool, ago: float = 0.0, guid: str | None = None) -> dict[str, Any]:
        self.next += 1
        row = {"id": self.next, "guid": guid or f"G-{self.next}", "chat_id": chat, "chat_identifier": self.chats[chat],
               "is_from_me": from_me, "text": text, "created_at": stamp(ago), "sender": "x", "attachments": []}
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
            m, p = req["method"], req.get("params") or {}
            self.calls.append(m)
            after: list[dict[str, Any]] = []
            if m == "chats.list":
                res: Any = {"chats": [{"id": i, "identifier": c, "name": "", "service": "iMessage"}
                                      for i, c in self.chats.items()]}
            elif m == "messages.history":
                mine = [x for x in self.rows if x["chat_id"] == p.get("chat_id")]
                res = {"messages": sorted(mine, key=lambda x: -x["id"])[:int(p.get("limit", 50))]}
            elif m == "watch.subscribe":
                self.subs.append((w, p))
                res = {"subscription": len(self.subs)}
                after = [x for x in self.rows if x["chat_id"] == p.get("chat_id") and x["id"] > int(p.get("since_rowid") or 0)]
            elif m == "send":
                chat = int(p.get("chat_id") or next(i for i, c in self.chats.items() if c == p.get("chat_identifier")))
                row = self.add(chat, p["text"], True)
                self.sent.append(p["text"])
                self.push(row)                    # the watch sees the row before the send is answered
                if not self.answer_sends:         # the Mac sleeps between the send and its answer
                    continue
                res = {"ok": True, **({"guid": row["guid"]} if self.guid_in_answer else {})}
            else:
                w.write((json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "no"}})
                         + "\n").encode())
                continue
            w.write((json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res}) + "\n").encode())
            for x in after:
                self.push(x)
            await w.drain()


async def until(cond: Any, t: float = 8.0) -> None:
    end = time.monotonic() + t
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


@pytest.fixture
def where() -> Any:
    d = Path(tempfile.mkdtemp(prefix="yi-", dir="/tmp"))     # a Unix socket path stays short
    yield d
    shutil.rmtree(d, ignore_errors=True)


def setup(where: Path, **conf: Any) -> tuple[FakeBridge, IMessageChannel, list[Any], list[tuple[str, str]], Ledger]:
    fake = FakeBridge(str(where / "b.sock"), guid_in_answer=conf.pop("guid_in_answer", True))
    told: list[tuple[str, str]] = []
    got: list[Any] = []

    async def machine(text: str, key: str) -> list[str]:
        told.append((text, key))
        return ["m"]
    led = Ledger(where / "l.sqlite")
    (where / "state").mkdir(exist_ok=True)
    ch = IMessageChannel({"unix": fake.path, "away_after": 0, "back_after": 0, "claim_every": 0.05, **conf}, led,
                         state=where / "state", machine=machine)
    ch.add_room(CHAT, NS(name="imsg-room", submit=got.append))   # type: ignore[arg-type]
    return fake, ch, got, told, led


async def running(fake: FakeBridge, ch: IMessageChannel) -> asyncio.Task[None]:
    await fake.start()
    task = asyncio.ensure_future(ch.start())
    await until(lambda: ch.connected)
    return task


async def done(task: asyncio.Task[None], fake: FakeBridge) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await fake.stop()


def test_rows_are_deduplicated_by_guid_and_a_first_start_never_replays(where):
    async def go() -> None:
        fake, ch, got, _, led = setup(where)
        fake.add(1, "old, before Yerk AI served the room", True, ago=600)
        task = await running(fake, ch)
        row = fake.add(1, "hello", True)
        fake.push(row)
        fake.push(row)                            # the same row twice (a second notification)
        await until(lambda: len(got) == 1)
        await asyncio.sleep(0.2)
        assert [m.id for m in got] == [row["guid"]] and got[0].sender == "owner"
        assert ch.take(dict(row)) == "seen"       # and once more, as a replay would bring it
        assert led.inbound_get(row["guid"])["state"] == "received"
        await done(task, fake)
    asyncio.run(go())


def test_yerks_own_send_is_recognised_by_its_guid_and_never_answered(where):
    async def go() -> None:
        fake, ch, got, _, led = setup(where)
        task = await running(fake, ch)
        ids = await asyncio.to_thread(ch.reply, "imsg-room", "Yerk AI iMessage test, no reply needed", ["G-in"])
        await asyncio.sleep(0.3)
        assert fake.sent == ["Yerk AI iMessage test, no reply needed"] and got == []
        guid = fake.rows[-1]["guid"]
        assert ids == [guid] and led.delivery_by_message(guid)["state"] == "sent"
        assert ch.take(dict(fake.rows[-1])) == "own" and led.inbound_get(guid) is None
        await done(task, fake)
    asyncio.run(go())


def test_a_send_whose_answer_has_no_guid_claims_the_chats_next_from_me_row(where):
    async def go() -> None:
        fake, ch, got, _, led = setup(where, guid_in_answer=False)
        task = await running(fake, ch)
        ids = await asyncio.to_thread(ch.reply, "imsg-room", "a reply", ["G-in"])
        await asyncio.sleep(0.3)
        guid = fake.rows[-1]["guid"]
        assert ids == [guid] and got == [] and led.delivery_by_message(guid) is not None
        await done(task, fake)
    asyncio.run(go())


def test_the_owner_typing_in_the_thread_is_the_owner_and_other_people_are_never_answered(where):
    async def go() -> None:
        fake, ch, got, _, led = setup(where)
        fake.chats[3] = "family"
        ch.add_room("family", NS(name="family-room", submit=got.append))   # type: ignore[arg-type]
        task = await running(fake, ch)
        await asyncio.to_thread(ch.reply, "imsg-room", "Yerk's line", [])
        mine = fake.add(1, "typed on the phone", True)        # from-me, a guid the ledger never recorded
        them = fake.add(3, "from someone else", False)
        fake.push(mine)
        fake.push(them)
        await until(lambda: led.inbound_get(them["guid"]) is not None)
        await until(lambda: len(got) == 1)
        assert got[0].id == mine["guid"] and got[0].sender == "owner" and got[0].text == "typed on the phone"
        assert led.inbound_get(them["guid"])["state"] == "ignored"
        await done(task, fake)
    asyncio.run(go())


def test_parts_wait_while_the_mac_is_away_and_go_in_order_with_one_machine_line_each_way(where):
    async def go() -> None:
        fake, ch, got, told, led = setup(where)
        task = await running(fake, ch)
        await fake.stop()                         # the lid closes
        await until(lambda: not ch.connected)
        await until(lambda: len(told) == 1)
        t0 = time.monotonic()
        first = await asyncio.to_thread(ch.reply, "imsg-room", "first reply", ["G-a"])
        second = await asyncio.to_thread(ch.reply, "imsg-room", "second reply", ["G-b"])
        assert first == [] and second == [] and time.monotonic() - t0 < 2     # held, not waited on
        assert [r["state"] for r in led.deliveries("imsg-room")] == ["pending", "pending"]
        await fake.start()                        # the lid opens
        await until(lambda: len(fake.sent) == 2, t=10)
        assert fake.sent == ["first reply", "second reply"]                 # in order, nothing else in the room
        await until(lambda: len(told) == 2)
        assert [t for t, _ in told] == [words.line(words.MAC_AWAY), words.line(words.MAC_BACK.format(n=2, s="s"))]
        assert all(r["state"] == "sent" for r in led.deliveries("imsg-room")) and got == []
        await done(task, fake)
    asyncio.run(go())


def test_a_long_reply_splits_at_the_limit_with_the_shared_splitter(where):
    async def go() -> None:
        fake, ch, _, _, _ = setup(where)
        task = await running(fake, ch)
        text = "\n\n".join(f"paragraph {i} " + "word " * 300 for i in range(6))
        ids = await asyncio.to_thread(ch.reply, "imsg-room", text, [])
        assert fake.sent == split_message(text.strip(), LIMIT) and len(ids) == len(fake.sent) > 1
        assert all(len(p) <= LIMIT for p in fake.sent)
        await done(task, fake)
    asyncio.run(go())


def test_no_card_no_edit_and_a_worker_gets_one_line(where):
    async def go() -> None:
        fake, ch, _, _, _ = setup(where)
        task = await running(fake, ch)
        assert ch.open_card("imsg-room", "card") is None and ch.progress("imsg-room", "edit") is None
        mid = await ch.worker_card("imsg-room", "w1", "worker running\nline two")
        again = await ch.worker_card("imsg-room", "w1", "worker running, 1 of 3", mid)
        settled = await ch.worker_settle("imsg-room", "w1", "finished", "done", mid)
        assert fake.sent == ["worker running"] and mid == again == settled
        with pytest.raises(ValueError):
            await ch.send_file("imsg-room", where / "x.png")
        await done(task, fake)
    asyncio.run(go())


def test_the_probe_never_sends_and_finds_a_planted_gap(where):
    async def go() -> None:
        fake, _, _, _, led = setup(where)
        await fake.start()
        seen = fake.add(1, "reached Yerk AI", True, ago=900)
        led.inbound_add(seen["guid"], "imsg-room", f"imessage:{CHAT}", "owner", 3)
        lost = fake.add(1, "never reached it", False, ago=800)
        fake.add(1, "just now, maybe in flight", False, ago=5)
        out = await imod.probe({"imsg-room": CHAT}, {"unix": fake.path}, led)
        assert out["ok"] is False and out["gaps"] == [{"room": "imsg-room", "guid": lost["guid"]}]
        assert out["rooms"]["imsg-room"] == {"read": 3, "missing": 1}
        assert "send" not in fake.calls and set(fake.calls) <= {"chats.list", "messages.history"}
        b = Bridge(unix=fake.path, read_only=True)
        await b.open()
        with pytest.raises(PermissionError):
            await b.request("send", {"chat_id": 1, "text": "never"})
        await b.close()
        assert fake.sent == []
        await fake.stop()
    asyncio.run(go())


def test_the_transport_is_ssh_and_socat_to_the_socket():
    argv = imod.transport({"host": "user@mac", "socket": "/Users/x/.imsg/rpc.sock"})
    assert argv[0] == "ssh" and "user@mac" in argv and argv[-1] == "UNIX-CONNECT:/Users/x/.imsg/rpc.sock"
    assert imod.guid_of({"ok": True}) is None and imod.guid_of({"messageGuid": "AB-1"}) == "AB-1"
    assert imod.guid_of({"message_id": "42"}) == 42 and imod.guid_of({"id": 7}) == 7


def test_a_send_that_went_out_with_no_answer_is_never_sent_twice_or_taken_as_the_owner(where):
    async def go() -> None:
        fake, ch, got, _, led = setup(where, send_timeout=0.3)
        task = await running(fake, ch)
        fake.answer_sends = False
        await asyncio.to_thread(ch.reply, "imsg-room", "went out, never answered", ["G-in"])
        fake.answer_sends = True
        await until(lambda: ch.connected and all(r["state"] == "sent" for r in led.deliveries("imsg-room")))
        await asyncio.sleep(0.5)
        guid = fake.rows[-1]["guid"]
        assert fake.sent == ["went out, never answered"] and got == []
        assert led.delivery_by_message(guid)["state"] == "sent" and led.inbound_get(guid) is None
        await done(task, fake)
    asyncio.run(go())
