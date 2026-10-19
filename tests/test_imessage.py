"""The iMessage adapter against a fake bridge: a local Unix socket speaking imsg's JSON-RPC lines."""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from yerk_ai import jobs, morning, probes, status, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.channels import imessage as imod
from yerk_ai.config import EXAMPLE_PRIVATE
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
        self.meta: dict[int, dict[str, Any]] = {}     # a chat's is_group and participants (default: one, itself)
        self.rows: list[dict[str, Any]] = []
        self.next = 100
        self.sent: list[str] = []
        self.calls: list[str] = []
        self.subs: list[tuple[asyncio.StreamWriter, dict[str, Any]]] = []
        self.conns: list[asyncio.StreamWriter] = []
        self.opened: list[float] = []             # when each connection came in
        self.swallow = False                      # a send the pipe dies under: never reaches Messages, no answer
        self.drop_on_subscribe = False            # the link drops right after each connect
        self.send_error: dict[str, Any] | None = None
        self.late_rows = False                    # a send's row lands in chat.db after the send is answered
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self.handle, self.path)

    async def stop(self) -> None:
        """The Mac goes away: the socket closes and every connection with it."""
        for w in self.conns:
            w.close()
        await asyncio.gather(*(w.wait_closed() for w in self.conns), return_exceptions=True)
        self.conns, self.subs = [], []
        if self.server is not None:               # close() frees the socket; on 3.12 wait_closed() never returns
            self.server.close()                   # once every connection closed before it, so it is not awaited
            self.server = None
        Path(self.path).unlink(missing_ok=True)

    def drop(self) -> None:
        """The pipe dies under the link (ssh notices a dead network): every connection closes, the Mac stays."""
        for w in self.conns:
            w.close()
        self.conns, self.subs = [], []

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
        self.opened.append(time.monotonic())
        while line := await r.readline():
            req = json.loads(line)
            m, p = req["method"], req.get("params") or {}
            self.calls.append(m)
            after: list[dict[str, Any]] = []
            if m == "chats.list":
                res: Any = {"chats": [{"id": i, "identifier": c, "name": "", "service": "iMessage",
                                       **self.meta.get(i, {"is_group": False, "participants": [c]})}
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
                if self.swallow:
                    self.calls.append("swallowed")
                    continue
                if self.send_error is not None:
                    w.write((json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": self.send_error}) + "\n").encode())
                    continue
                self.sent.append(p["text"])
                if self.late_rows:                # answered first, no guid; the row a second later
                    def land(chat: int = chat, text: str = p["text"]) -> None:
                        self.push(self.add(chat, text, True))
                    asyncio.get_running_loop().call_later(1.0, land)
                    w.write((json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}) + "\n").encode())
                    continue
                row = self.add(chat, p["text"], True)
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
            if m == "watch.subscribe" and self.drop_on_subscribe:
                w.close()
                return


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
    ch = IMessageChannel({"unix": fake.path, "away_after": 0, "back_after": 0, "claim_every": 0.05, "owner": [CHAT],
                          **conf}, led, state=where / "state", machine=machine)
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
        task = await running(fake, ch)
        await asyncio.to_thread(ch.reply, "imsg-room", "Yerk's line", [])
        mine = fake.add(1, "typed on the phone", True)        # from-me, a guid the ledger never recorded
        them = fake.add(1, "not from the owner", False)       # a received copy, or anyone else's row
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
        out = await imod.probe({"imsg-room": CHAT}, {"unix": fake.path, "owner": [CHAT]}, led)
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
        await asyncio.to_thread(ch.reply, "imsg-room", "the next reply", ["G-in2"])   # the link that timed out reopens
        await until(lambda: fake.sent == ["went out, never answered", "the next reply"], t=10)
        await until(lambda: led.delivery_by_message(fake.rows[-1]["guid"]) is not None)
        await done(task, fake)
    asyncio.run(go())


async def cut_off(fake: FakeBridge, ch: IMessageChannel, text: str = "a reply cut off") -> None:
    """A send the pipe dies under before it reaches Messages: no row and no answer, then the link drops."""
    fake.swallow = True
    reply = asyncio.ensure_future(asyncio.to_thread(ch.reply, "imsg-room", text, ["G-in"]))
    await until(lambda: "swallowed" in fake.calls)
    fake.drop()
    assert await reply == []
    fake.swallow = False


def test_a_part_settled_by_its_resend_claims_nothing_so_the_owners_next_text_is_his_turn(where):
    async def go() -> None:
        fake, ch, got, _, led = setup(where, send_timeout=5)
        task = await running(fake, ch)
        await cut_off(fake, ch)
        await until(lambda: ch.connected and all(r["state"] == "sent" for r in led.deliveries("imsg-room")), t=15)
        assert fake.sent == ["a reply cut off"]   # resent on the next connect, answered with its guid
        owner = fake.add(1, "the owner, a moment later", True)
        fake.push(owner)
        await until(lambda: len(got) == 1)
        assert got[0].id == owner["guid"] and led.delivery_by_message(owner["guid"]) is None
        assert sum(len(q) for q in ch.unclaimed.values()) == 0
        await done(task, fake)
    asyncio.run(go())


def test_a_send_that_never_went_out_claims_no_text_the_owner_typed_while_the_mac_was_away(where):
    async def go() -> None:
        fake, ch, got, _, led = setup(where, send_timeout=5)
        task = await running(fake, ch)
        await cut_off(fake, ch)
        await fake.stop()                         # and the Mac goes away
        owner = fake.add(1, "you there?", True, ago=-60)      # typed a minute into the outage
        await fake.start()
        await until(lambda: ch.connected and all(r["state"] == "sent" for r in led.deliveries("imsg-room")), t=20)
        await until(lambda: len(got) == 1)
        assert fake.sent == ["a reply cut off"] and got[0].id == owner["guid"]       # the reply really went
        assert led.delivery_by_message(owner["guid"]) is None
        note = led.deliveries("imsg-room")[0]["note"] or ""
        assert note == "sent after the Mac came back"
        await done(task, fake)
    asyncio.run(go())


def test_a_claim_takes_only_a_row_made_during_its_send_attempt():
    window = (1000.0, 1030.0)
    at = lambda t: {"created_at": datetime.fromtimestamp(t, timezone.utc).isoformat()}   # noqa: E731
    assert imod.in_attempt(at(1000.0 - 9), window) and imod.in_attempt(at(1030.0 + 9), window)
    assert not imod.in_attempt(at(1000.0 - 11), window) and not imod.in_attempt(at(1030.0 + 11), window)
    assert not imod.in_attempt({"created_at": ""}, window)


def test_only_the_owners_own_thread_is_served_and_a_group_or_another_persons_chat_is_refused_at_connect(where):
    async def go() -> None:
        fake, ch, got, told, led = setup(where)
        fake.chats[3], fake.meta[3] = "family", {"is_group": True, "participants": [CHAT, "aunt"]}
        fake.chats[4] = "friend"
        ch.add_room("family", NS(name="family-room", submit=got.append))   # type: ignore[arg-type]
        ch.add_room("friend", NS(name="friend-room", submit=got.append))   # type: ignore[arg-type]
        task = await running(fake, ch)
        assert [p["chat_id"] for _, p in fake.subs] == [1]                 # only the owner's own thread is watched
        mine = fake.add(3, "the owner's own text in the group", True)
        assert ch.take(dict(mine)) == "not ours" and led.inbound_get(mine["guid"]) is None
        assert await asyncio.to_thread(ch.reply, "family-room", "never goes", []) == []
        assert await asyncio.to_thread(ch.reply, "friend-room", "never goes", []) == []
        assert fake.sent == [] and [r["state"] for r in led.deliveries("family-room") + led.deliveries("friend-room")] \
            == ["failed", "failed"]
        refused = sorted([words.line(words.IMSG_REFUSED.format(room="family-room", why=words.IMSG_GROUP)),
                          words.line(words.IMSG_REFUSED.format(room="friend-room", why=words.IMSG_OTHERS))])
        assert sorted(t for t, _ in told) == refused
        fake.drop()                               # a reconnect does not say it again
        await until(lambda: fake.subs)
        await until(lambda: ch.connected)
        assert sorted(t for t, _ in told if t in refused) == refused
        own = fake.add(1, "the owner's own thread is still served", True)
        fake.push(own)
        await until(lambda: len(got) == 1)
        assert got[0].id == own["guid"]
        await done(task, fake)
    asyncio.run(go())


def test_the_room_guard_takes_only_a_chat_of_the_owners_own_handles():
    own = {"owner-handle", "+15550001111"}
    assert imod.refusal({"identifier": "owner-handle", "participants": ["Owner-Handle"]}, own) is None
    assert imod.refusal({"identifier": "+1 555 0001111", "participants": ["+15550001111"]}, own) is None
    assert imod.refusal({"identifier": "mac-login", "account_login": "mac-login", "participants": []}, set()) is None
    assert imod.refusal({"identifier": "owner-handle", "is_group": True}, own) == words.IMSG_GROUP
    assert imod.refusal({"identifier": "friend", "participants": ["friend"]}, own) == words.IMSG_OTHERS
    assert imod.refusal({"identifier": "owner-handle", "participants": ["owner-handle", "friend"]}, own) \
        == words.IMSG_OTHERS
    assert imod.refusal({"identifier": "owner-handle", "participants": [{"handle": "owner-handle"}]}, own) \
        == words.IMSG_OTHERS                      # a participant that is not a plain handle is someone else
    assert imod.refusal({"identifier": "owner-handle"}, set()) == words.IMSG_OTHERS   # no handle set: none served


def test_a_loop_guard_trip_is_said_once_in_the_machine_room(where):
    async def go() -> None:
        fake, ch, got, told, led = setup(where, from_me_burst=3)
        task = await running(fake, ch)
        rows = [fake.add(1, f"owner text {i}", True) for i in range(6)]
        for r in rows:
            fake.push(r)
        await until(lambda: all(led.inbound_get(r["guid"]) is not None for r in rows))
        await until(lambda: len(told) >= 1)
        await asyncio.sleep(0.2)
        assert len(got) == 3 and [t for t, _ in told] == [words.line(words.IMSG_LOOP.format(room="imsg-room", mins=10))]
        await done(task, fake)
    asyncio.run(go())


def test_a_link_that_drops_right_after_each_connect_is_reopened_ever_more_slowly(where):
    async def go() -> None:
        fake, ch, _, _, _ = setup(where)
        fake.drop_on_subscribe = True             # say a replayed row crashes the watch each time
        await fake.start()
        task = asyncio.ensure_future(ch.start())
        await until(lambda: len(fake.opened) >= 3, t=10)
        gaps = [b - a for a, b in zip(fake.opened, fake.opened[1:])]
        assert 0.8 < gaps[0] < 1.6 and gaps[1] > 1.8, gaps    # 1 s, then 2 s: the reset waits for a link that held
        await done(task, fake)
    asyncio.run(go())


def test_a_tapback_is_never_a_lost_text(where):
    async def go() -> None:
        fake, _, _, _, led = setup(where)
        await fake.start()
        fake.add(1, "Loved a message", True, ago=900)["is_tapback"] = True
        fake.add(1, "Liked a message", False, ago=900)["is_reaction"] = True
        out = await imod.probe({"imsg-room": CHAT}, {"unix": fake.path, "owner": [CHAT]}, led)
        assert out["ok"] is True and out["gaps"] == [] and out["rooms"]["imsg-room"] == {"read": 2, "missing": 0}
        await fake.stop()
    asyncio.run(go())


def test_a_refused_send_is_kept_by_the_bridges_error_class_never_its_text(where, caplog):
    async def go() -> None:
        fake, ch, _, _, led = setup(where)
        fake.send_error = {"code": -32000, "message": "could not send to owner-handle-9"}
        task = await running(fake, ch)
        assert await asyncio.to_thread(ch.reply, "imsg-room", "a reply", []) == []
        r = led.deliveries("imsg-room")[0]
        assert r["state"] == "failed" and r["error"] == "bridge error -32000"
        rows = led._select("SELECT error FROM outbound", ())
        assert [x["error"] for x in rows] == ["bridge error -32000"]
        assert "owner-handle-9" not in json.dumps(led.deliveries("imsg-room")) + caplog.text
        await done(task, fake)
    asyncio.run(go())


def test_a_send_answered_with_no_guid_claims_its_row_that_lands_after_the_answer(where):
    async def go() -> None:
        fake, ch, got, _, led = setup(where, guid_in_answer=False)
        fake.late_rows = True
        task = await running(fake, ch)
        await asyncio.to_thread(ch.reply, "imsg-room", "a reply", ["G-in"])
        assert fake.rows == [] and fake.sent == ["a reply"]     # answered; its row is not in the history yet
        await until(lambda: len(fake.rows) == 1)
        row = fake.rows[0]
        await until(lambda: led.delivery_by_message(row["guid"]) is not None)
        assert got == [] and led.inbound_get(row["guid"]) is None
        await done(task, fake)
    asyncio.run(go())


def test_the_probe_is_not_ok_when_a_rooms_chat_is_not_listed_or_not_served(where):
    async def go() -> None:
        fake, _, _, _, led = setup(where)
        fake.chats[3], fake.meta[3] = "family", {"is_group": True, "participants": [CHAT, "aunt"]}
        await fake.start()
        out = await imod.probe({"imsg-room": CHAT, "gone-room": "an-old-chat"}, {"unix": fake.path, "owner": [CHAT]}, led)
        assert out["ok"] is False and out["failure"] == "chat not listed" and out["gaps"] == []
        assert out["rooms"]["gone-room"]["chat"] == "not in the chat list"
        out = await imod.probe({"imsg-room": CHAT, "family-room": "family"}, {"unix": fake.path, "owner": [CHAT]}, led)
        assert out["ok"] is False and out["failure"] == "not served" and out["gaps"] == []
        assert out["rooms"]["family-room"]["chat"] == f"not served, {words.IMSG_GROUP}"
        assert "send" not in fake.calls
        await fake.stop()
    asyncio.run(go())


@contextlib.contextmanager
def served(fake: FakeBridge) -> Any:
    """The fake bridge on its own loop in a thread, for code that runs asyncio.run itself (the probe CLI)."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    asyncio.run_coroutine_threadsafe(fake.start(), loop).result(5)
    try:
        yield loop
    finally:
        asyncio.run_coroutine_threadsafe(fake.stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)
        loop.close()


def private_layer(where: Path, unix: str) -> tuple[NS, Path]:
    priv, st = where / "private", where / "st"
    priv.mkdir()
    st.mkdir()
    (priv / "rooms.toml").write_text(f'[rooms.imsg-room]\nchannel = "imessage:{CHAT}"\n\n'
                                     '[rooms.machine-room]\nchannel = "discord:1"\nrole = "machine"\n')
    (priv / "yerkd.toml").write_text(f'[imessage]\nunix = "{unix}"\nowner = ["{CHAT}"]\n')
    return NS(private=str(priv), state=str(st), rows=20, grace=120.0), st


def test_a_text_is_lost_only_when_two_probe_runs_in_a_row_miss_it(where, monkeypatch):
    fake = FakeBridge(str(where / "b.sock"))
    a, st = private_layer(where, fake.path)
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(jobs, "post_control", lambda state, payload, timeout=120: posted.append(payload) or {"ok": True})
    with served(fake):
        fake.add(1, "typed while the Mac slept", True, ago=900)
        first = probes.imessage(a)                # not replayed yet, maybe: noted by its hash, not said
        assert first["ok"] is True and first["gaps"] == 0 and first["first_misses"] == 1 and posted == []
        second = probes.imessage(a)               # missed again: lost, said once
        assert second["ok"] is False and second["gaps"] == 1 and len(posted) == 1
        assert probes.imessage(a)["gaps"] == 1 and len(posted) == 1      # still missing: never said twice
        replayed = fake.add(1, "replayed between two runs", True, ago=900)
        assert probes.imessage(a)["first_misses"] == 1
        Ledger(st / "ledger.sqlite").inbound_add(replayed["guid"], "imsg-room", f"imessage:{CHAT}", "owner", 3)
        last = probes.imessage(a)
        assert last["gaps"] == 1 and last["first_misses"] == 0 and len(posted) == 1
        assert "send" not in fake.calls


def test_the_probe_with_the_bridge_down_is_a_run_that_did_not_reach_the_mac_with_no_gap_and_no_alert(where, monkeypatch):
    a, st = private_layer(where, str(where / "nothing.sock"))
    posted: list[Any] = []
    monkeypatch.setattr(jobs, "post_control", lambda *x, **k: posted.append(x) or {"ok": True})
    out = probes.imessage(a)
    led = Ledger(st / "ledger.sqlite")
    run = led.health_last("imessage")
    assert out["ok"] is False and "did not answer" in out["error"] and out["gaps"] == 0 and posted == []
    assert run is not None and run["ok"] == 0 and json.loads(run["note"]).get("reached") is False
    assert led.health_notes("imessage", "probe-gap") == set()


def test_the_morning_line_counts_runs_that_reached_the_mac_and_each_lost_text_once(tmp_path):
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    state.mkdir()
    led, now = Ledger(state / "ledger.sqlite"), time.time()
    t = status.midnight(now, 1) + 3600
    for i in range(3):                            # one lost text, still in the newest rows on three runs
        led.health(at=t + i * 900, account="imessage", source="probe", ok=0, failure="lost",
                   note=json.dumps({"reached": True, "lost": 1, "marks": ["a1"]}))
    for i in range(2):                            # two runs while the Mac slept
        led.health(at=t + 3600 + i * 900, account="imessage", source="probe", ok=0, failure="did not answer",
                   note=json.dumps({"reached": False, "lost": 0, "marks": []}))
    lines, _ = morning.build(priv, state, load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"),
                             led, {"units": [], "started": now - 3 * 86400}, now)
    assert [x for x in lines if "iMessage" in x and x.endswith(" 1 lost text")]   # one text, not once a run
    assert words.line(f"{words.PLAIN} {words.IMSG_PROBE.format(day=words.YESTERDAY, reached=3, runs=5)}") in lines
    assert words.line(f"{words.NOT_DONE} {words.IMSG_LOST.format(day=words.YESTERDAY, n=1, s='')}") in lines
