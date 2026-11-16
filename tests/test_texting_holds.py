"""Checks texting's holds: a drafted text that may not go on its own is one card in your texting room, and nowhere else;
your reply `send` releases exactly that card's text (queued; dry: would send), `edit` makes a new card, `skip` drops it,
your own text withdraws it, a newer burst edits it in place, and past `hold_hours` it cannot go. Built in run 5; nothing
here reaches the Mac or Discord: the card room is the tests' fake."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import discord

from fake_channel import ConsoleChannel
from test_discord import ACK, OTHER_CH, OWNER, PERSON, ROOM_CH, Client, Msg, Room
from test_texting_link import A1, B1, stamp
from yerk_ai import gates, texts, words
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger
from yerk_ai.texting import holds, intake
from yerk_ai.texting.people import load

LIST = (f'[owner]\nname = "Sam"\n\n[people.alex]\nfirst = "Alex"\nhandles = ["{A1}"]\nsheet = "people/alex.toml"\nrelease = {{rel}}\n\n'
        f'[people.beta]\nfirst = "Beta"\nhandles = ["{B1}"]\nsheet = "people/beta.toml"\n')
SHEET = 'open = []\nreacts = ["love"]\n\n[voice]\ncard = "short, lower case"\n'
SAID = {"g-1": "wait you moved??", "g-2": "and who is the new girl", "g-9": "ok", "g-b": "gym tonight?"}


class Card:
    """The card room: each post (a new id) and each edit, by message id."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []
        self.edits: list[tuple[str, str, str]] = []

    async def show(self, room: str, text: str, mid: str = "") -> str:
        if mid:
            self.edits.append((room, mid, text))
            return mid
        self.posts.append((room, str(100 + len(self.posts)), text))
        return self.posts[-1][1]


async def thread(key: str) -> list[dict[str, Any]]:
    return [{"guid": g, "is_from_me": False, "text": t} for g, t in SAID.items()]


def setup(tmp_path: Path, mode: str = "live", release: bool = False, out: bool = True, to: str = "owner-room") -> tuple[holds.Holds, Ledger, Card]:
    priv = tmp_path / "private" / "texting"
    (priv / "people").mkdir(parents=True)
    (tmp_path / "state" / "texting").mkdir(parents=True)
    for f, text in ((priv / "people.toml", LIST.replace("{rel}", "true" if release else "false")),
                    (priv / "people" / "alex.toml", SHEET), (priv / "people" / "beta.toml", SHEET)):
        f.write_text(text)
        os.chmod(f, 0o600)
    led, card = Ledger(tmp_path / "ledger.sqlite"), Card()
    return holds.Holds({"mode": mode}, priv / "people.toml", tmp_path / "state", led, to, thread, card.show, out=out), led, card


def decided(led: Ledger, person: str = "alex", guid: str = "g-1", decision: str = "held", state: str = "decided",
            parts: tuple[str, ...] = ("yes, last week",), reason: str = "dating, your call", ago: float = 60) -> int:
    led.inbound_add(guid, "texting", f"imessage:{person}", person, len(SAID[guid]), created=time.time() - ago)
    return led.texting_add(person=person, handle=A1 if person == "alex" else B1, inbound=guid, decision=decision, parts=json.dumps(list(parts)),
                           react="", reply_to="", statements="[]", state=state, reason=reason if decision == "held" else "", mode="dry")


def row(led: Ledger, rid: int) -> dict[str, Any]:
    return led.texting_get(rid) or {}


def gates_rows(led: Ledger) -> list[tuple[str, str]]:
    return [(r["gate"], r["decision"]) for r in reversed(led.rows(50, "gates"))]


class Rec(ConsoleChannel):
    def __init__(self) -> None:
        super().__init__()
        self.got: list[tuple[str, str]] = []

    async def send_text(self, room: str, text: str, key: str | None = None, inbound: str = "") -> list[str]:
        self.got.append((room, text))
        return [f"m{len(self.got)}"]

    async def notify(self, room: str, text: str) -> list[str]:
        self.got.append((room, text))
        return [f"m{len(self.got)}"]

    async def edit_post(self, room: str, mid: str, text: str) -> None:
        self.got.append((room, f"edit {mid}: {text}"))


def test_a_card_line_or_notice_goes_only_to_an_owner_room_without_people_and_a_persons_room_is_sent_nothing(tmp_path: Path) -> None:
    """J25: a person's room named as `report` (live) or as `dry_report` (dry) is refused at the start and is sent nothing: no
    card, no edit, no word's answer, no missed list. The owner room named the same way gets them."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.room_confs["owner-room"] = dict(y.room_confs["example-room"])
    y.room_confs["person-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    y.room_confs["quiet-room"] = {**y.room_confs["example-room"], "privacy": "family"}
    chans = {n: Rec() for n in ("owner-room", "person-room", "quiet-room")}
    for n, ch in chans.items():
        y.add_room(n, ch)

    async def go(conf: dict[str, Any]) -> str:
        y.conf["texting"] = conf
        y.texting()                                   # the reply list is not there: off, but the room is settled first
        h, led, _ = setup(tmp_path / f"run{len(list(tmp_path.glob('run*')))}", mode=conf["mode"], to=y.texting_to)
        h.show = y.texting_show
        decided(led)
        await h.tick()
        for room in chans:
            await y.texting_show(room, "a card")
            await y.texting_show(room, "an edit", "m1")
            await y.texting_post(room, "missed: 1 text")
        return y.texting_to
    for refused in ("person-room", "quiet-room"):
        assert asyncio.run(go({"mode": "live", "report": refused})) == ""
        assert asyncio.run(go({"mode": "dry", "report": "owner-room", "dry_report": refused})) == ""
    assert asyncio.run(go({"mode": "dry", "report": "owner-room"})) == ""      # dry with no dry_report: nowhere, not report
    assert chans["person-room"].got == chans["quiet-room"].got == chans["owner-room"].got == []
    assert asyncio.run(go({"mode": "live", "report": "owner-room"})) == "owner-room"
    got = [t for _, t in chans["owner-room"].got]
    assert got[0].startswith("📱 Alex · ") and got[0].split("\n")[0].endswith(" · held") and got[1:] == ["a card", "edit m1: an edit", "missed: 1 text"]
    assert chans["person-room"].got == chans["quiet-room"].got == []


def test_the_card_reads_in_one_look_on_a_phone() -> None:
    r = {"decision": "reply", "parts": json.dumps(["yes, last week", "come by"]), "react": ""}
    text = holds.card("Alex", "09:17", [holds.quote("x" * 400), holds.quote("two\nlines")], r, "dating, your call", False)
    assert text.split("\n") == ["📱 Alex · 09:17 · held", "> " + "x" * 297 + "...", "> two lines", "draft:", "yes, last week", "come by",
                                "why: dating, your call", "reply: send · edit <text> · skip"]
    tap = holds.card("Alex", "09:17", ["> ok"], {"decision": "react", "react": "love", "parts": "[]"}, "it passed the check", True)
    assert tap.split("\n")[0] == "DRY Alex · 09:17 · held" and "draft: a love tapback" in tap
    for line in (text.split("\n")[0], tap.split("\n")[0], words.TEXTING_CARD_WORDS, words.TEXTING_CARD_DRAFT,
                 words.TEXTING_CARD_TAPBACK.format(react="love"), words.TEXTING_CARD_WHY.format(why=words.TEXTING_CARD_OFF.format(first="Alex")),
                 words.TEXTING_CARD_WHY.format(why=words.TEXTING_CARD_EDIT.format(found=words.TEXTING_CARD_CLEAN)),
                 *(words.TEXTING_CARD.format(mark=m, first="Alex", at="09:17", state=s.format(at="09:23"))
                   for s in words.TEXTING_SHOWS.values() for m in words.TEXTING_CARD_MARK.values())):
        assert words.width(line) <= words.PHONE, line
    assert holds.restate(text, "skipped").split("\n")[0] == "📱 Alex · 09:17 · skipped" and words.TEXTING_CARD_WORDS not in holds.restate(text, "x")


def test_a_held_decision_and_a_release_ready_one_with_release_off_get_one_card_and_hold_and_a_none_gets_nothing(tmp_path: Path) -> None:
    h, led, card = setup(tmp_path)
    a = decided(led)
    b = decided(led, "beta", "g-b", decision="reply", state="release-ready", parts=("can't tonight",))
    n = decided(led, "alex", "g-9", decision="none")
    asyncio.run(h.tick())
    asyncio.run(h.tick())                                                     # a second pass posts nothing more
    assert [(room, mid) for room, mid, _ in card.posts] == [("owner-room", "100"), ("owner-room", "101")] and card.edits == []
    assert card.posts[0][2].split("\n")[1:] == ["> wait you moved??", "draft:", "yes, last week",
                                                "why: dating, your call", "reply: send · edit <text> · skip"]
    assert "why: checked; release is off for Beta" in card.posts[1][2]
    holds_ = led._select("SELECT * FROM approvals ORDER BY id", ())
    assert [(x["room"], x["rule"], x["what"], x["call"]) for x in holds_] == [
        ("owner-room", "text-thread", "a text to Alex", gates.call_key(texts.TOOL, {"to": A1, "parts": ["yes, last week"]})),
        ("owner-room", "text-thread", "a text to Beta", gates.call_key(texts.TOOL, {"to": B1, "parts": ["can't tonight"]}))]
    assert [(row(led, x)["state"], row(led, x)["card"]) for x in (a, b, n)] == [("held", "100"), ("held", "101"), ("decided", None)]


def test_a_reply_send_releases_exactly_that_cards_text_once_and_approve_reads_as_send(tmp_path: Path) -> None:
    h, led, card = setup(tmp_path)
    a, b = decided(led), decided(led, "beta", "g-b", parts=("not tonight",))
    asyncio.run(h.tick())
    assert asyncio.run(h.word("owner-room", "owner", "send", "101")) is True          # Beta's card, by reply
    assert (row(led, a)["state"], row(led, b)["state"]) == ("held", "queued")
    assert gates_rows(led)[-2:] == [("approval", "approved"), ("text", "queued")]
    assert card.edits == [("owner-room", "101", card.posts[1][2].replace(" · held", " · sending").replace("\nreply: send · edit <text> · skip", ""))]
    hold = led.approval(int(row(led, b)["approval_id"])) or {}
    assert hold["approved_by"] == "owner" and hold["released"] and hold["call"] == holds.key(row(led, b))
    assert asyncio.run(h.word("owner-room", "owner", "send", "101")) is True          # again: said once, nothing more
    assert card.posts[-1][2] == "nothing sent: that card is sending" and row(led, b)["state"] == "queued"
    assert asyncio.run(h.word("owner-room", "owner", "Approve.", "100")) is True      # the approval word on a card is send
    assert row(led, a)["state"] == "queued"
    assert asyncio.run(h.word("owner-room", "owner", "why this?", "100")) is False   # any other reply: an ordinary message
    assert asyncio.run(h.word("owner-room", "owner", "send", "999")) is False        # a reply to a message that is no card
    assert asyncio.run(h.word("owner-room", "person", "send", "100")) is False       # not the owner
    assert asyncio.run(h.word("other-room", "owner", "send", "100")) is False        # not the card room


def test_send_never_releases_a_row_that_is_not_the_text_its_card_showed(tmp_path: Path) -> None:
    """The hold is keyed by the exact texts the card showed: a row that changed under its card is not sent on that card's word."""
    h, led, card = setup(tmp_path)
    a = decided(led)
    asyncio.run(h.tick())
    led.texting_set(a, parts=json.dumps(["a text the card never showed"]))
    assert asyncio.run(h.word("owner-room", "owner", "send", "100")) is True
    assert row(led, a)["state"] == "held" and ("text", "queued") not in gates_rows(led)
    assert card.posts[-1][2] == words.TEXTING_MOVED and not (led.approval(int(row(led, a)["approval_id"])) or {})["released"]


def test_a_word_in_a_channel_the_card_room_only_watches_is_never_read_as_his_word(tmp_path: Path) -> None:
    """Through the Discord adapter: the owner's reply `send` to a card, and a bare `send`, typed in a channel the card room
    watches leave the card held and are not filed as a word; the same reply in the room's own channel sends it."""
    h, led, _ = setup(tmp_path / "t", to="test-room")
    a = decided(led)
    client = Client()
    d = DiscordChannel("token", OWNER, [], led, client=client, state=tmp_path, word=h.word)
    d.add_room(ROOM_CH, Room(tmp_path), watch=OTHER_CH)
    asyncio.run(h.tick())

    def reply(ch: Any, mid: int) -> Msg:
        m = Msg(ch, mid, "send")
        m.type, m.reference = discord.MessageType.reply, NS(message_id=100, resolved=None)
        return m
    for m in (reply(client.chans[OTHER_CH], 7), Msg(client.chans[OTHER_CH], 8, "send")):
        asyncio.run(d.on_message(m))
    assert row(led, a)["state"] == "held" and all((led.inbound_get(i) or {}).get("state") != "filed" for i in ("7", "8"))
    asyncio.run(d.on_message(reply(client.chans[ROOM_CH], 9)))
    assert row(led, a)["state"] == "queued" and (led.inbound_get("9") or {})["state"] == "filed"


def test_dry_cards_go_marked_and_send_only_says_it_would(tmp_path: Path) -> None:
    h, led, card = setup(tmp_path, mode="dry", to="dry-room")
    a = decided(led)
    asyncio.run(h.tick())
    assert card.posts[0][0] == "dry-room" and card.posts[0][2].startswith("DRY Alex · ")
    assert asyncio.run(h.word("dry-room", "owner", "send", "100")) is True
    assert row(led, a)["state"] == "would-send" and card.edits[-1][2].split("\n")[0].endswith(" · would send")
    day = next((tmp_path / "state" / "texting" / "dry").iterdir()).read_text().splitlines()
    assert json.loads(day[-1])["state"] == "would-send" and json.loads(day[-1])["by"] == "word"


def test_a_bare_send_or_skip_resolves_a_card_only_when_it_is_the_one_hold_open_and_nothing_else_is(tmp_path: Path) -> None:
    h, led, card = setup(tmp_path)
    a, b = decided(led), decided(led, "beta", "g-b", parts=("not tonight",))
    asyncio.run(h.tick())
    assert asyncio.run(h.word("owner-room", "owner", "send", None)) is False         # two text holds open: an ordinary message
    assert asyncio.run(h.word("owner-room", "owner", "skip", "101")) is True
    assert row(led, b)["state"] == "skipped" and card.edits[-1][2].split("\n")[0].endswith(" · skipped")
    gid = led.gate(room="owner-room", gate="approval", decision="hold", tool="Bash", rule="public-post", detail="")
    led.hold_add("owner-room", "public-post", "a public post", gid, call="git push origin main")
    assert asyncio.run(h.word("owner-room", "owner", "send", None)) is False         # another hold is open in the room
    led._update("approvals", "rule", "public-post", {"approved": time.time()})
    for other in ("send it", "Send.", "approve", "edit hi", "ok send"):
        assert asyncio.run(h.word("owner-room", "owner", other, None)) is False, other
    assert row(led, a)["state"] == "held"
    assert asyncio.run(h.word("owner-room", "owner", " SEND ", None)) is True        # exactly the word, the one hold left
    assert row(led, a)["state"] == "queued"


def test_edit_makes_a_new_card_and_hold_from_his_words_shows_the_code_check_and_sends_nothing(tmp_path: Path) -> None:
    h, led, card = setup(tmp_path)
    a = decided(led)
    asyncio.run(h.tick())
    assert asyncio.run(h.word("owner-room", "owner", "edit", "100")) is True
    assert card.posts[-1][2] == words.TEXTING_EDIT_EMPTY and row(led, a)["state"] == "held"
    assert asyncio.run(h.word("owner-room", "owner", "edit yes, and her name is Beta, they are dating", "100")) is True
    new = led.texting_last("alex") or {}
    assert row(led, a)["state"] == "superseded" and new["state"] == "held" and json.loads(new["parts"]) == ["yes, and her name is Beta, they are dating"]
    assert card.posts[-1][1] == new["card"] != "100" and card.posts[-1][2].split("\n")[1:4] == ["> wait you moved??", "draft:", json.loads(new["parts"])[0]]
    assert "why: your words; dating, your call" in card.posts[-1][2]
    assert card.edits[-1][1] == "100" and card.edits[-1][2].split("\n")[0].endswith(" · replaced by your edit")
    assert [x["state"] for x in led._select("SELECT state FROM texting", ())].count("queued") == 0 and json.loads(new["verdict"])["by"] == "owner"
    assert asyncio.run(h.word("owner-room", "owner", "send", "100")) is True          # the old card: says so, sends nothing
    assert card.posts[-1][2] == "nothing sent: that card is replaced by your edit"
    assert asyncio.run(h.word("owner-room", "owner", "send", new["card"])) is True
    assert row(led, int(new["id"]))["state"] == "queued"
    c = decided(led, guid="g-9")
    asyncio.run(h.tick())
    assert asyncio.run(h.word("owner-room", "owner", "edit see you then", row(led, c)["card"])) is True     # words the code finds nothing in
    assert card.posts[-1][2].split("\n")[-2] == "why: your words; the code finds nothing"


def test_his_own_text_withdraws_the_card_edits_it_and_posts_nothing_new(tmp_path: Path) -> None:
    h, led, card = setup(tmp_path)
    took = intake.Intake({"mode": "live"}, load(tmp_path / "private" / "texting" / "people.toml"), led, lambda *a, **k: True)
    took.withdraw = h.withdraw
    a = decided(led, ago=120)
    asyncio.run(h.tick())
    p = took.people.people["alex"]
    took.take(p, {"guid": "g-own", "is_from_me": True, "text": "yes, moved", "created_at": stamp(0)})
    asyncio.run(h.tick())
    assert row(led, a)["state"] == "withdrawn" and len(card.posts) == 1
    assert card.edits[-1][2].split("\n")[0].startswith("📱 Alex · ") and " · answered by you " in card.edits[-1][2].split("\n")[0]
    assert asyncio.run(h.word("owner-room", "owner", "send", "100")) is True and row(led, a)["state"] == "withdrawn"
    assert card.posts[-1][2].startswith("nothing sent: that card is answered by you ")
    b = decided(led, guid="g-2")                                             # their next text: decided afresh, a new card
    asyncio.run(h.tick())
    assert row(led, b)["state"] == "held" and row(led, b)["card"] == card.posts[-1][1] != "100"


def test_a_newer_burst_edits_the_open_card_in_place_and_send_releases_the_new_draft(tmp_path: Path) -> None:
    h, led, card = setup(tmp_path)
    a = decided(led)
    asyncio.run(h.tick())
    b = decided(led, guid="g-2", parts=("yes, last week", "and that is not for text"))
    asyncio.run(h.tick())
    assert len(card.posts) == 1 and card.edits[-1][1] == "100"
    assert card.edits[-1][2].split("\n")[1:6] == ["> wait you moved??", "> and who is the new girl", "draft:", "yes, last week", "and that is not for text"]
    assert (row(led, a)["state"], row(led, b)["state"], row(led, b)["card"], row(led, b)["inbound"]) == ("superseded", "held", "100", "g-1,g-2")
    assert asyncio.run(h.word("owner-room", "owner", "send", "100")) is True
    assert row(led, b)["state"] == "queued" and row(led, a)["state"] == "superseded"
    released = led._select("SELECT * FROM approvals WHERE released IS NOT NULL", ())
    assert [x["call"] for x in released] == [holds.key(row(led, b))]


def test_a_hold_past_hold_hours_expires_and_can_no_longer_be_released(tmp_path: Path) -> None:
    h, led, card = setup(tmp_path)
    a, b = decided(led), decided(led, "beta", "g-b", parts=("not tonight",))
    asyncio.run(h.tick())
    led._update("approvals", "id", int(row(led, a)["approval_id"]), {"held": time.time() - 7 * 3600})
    led._update("approvals", "id", int(row(led, b)["approval_id"]), {"held": time.time() - 7 * 3600})
    assert asyncio.run(h.word("owner-room", "owner", "send", "101")) is True          # a word before the pass sweeps it
    assert row(led, b)["state"] == "expired" and card.posts[-1][2] == "nothing sent: that card is expired, not sent"
    asyncio.run(h.tick())
    assert row(led, a)["state"] == "expired" and card.edits[-1][2].split("\n")[0].endswith(" · expired, not sent")
    assert asyncio.run(h.word("owner-room", "owner", "send", "100")) is True and row(led, a)["state"] == "expired"
    assert led._select("SELECT * FROM approvals WHERE released IS NOT NULL", ()) == []


def test_standing_release_writes_the_standing_row_first_and_only_live_with_text_out_on(tmp_path: Path) -> None:
    h, led, card = setup(tmp_path / "live", release=True)
    a = decided(led, decision="reply", state="release-ready")
    asyncio.run(h.tick())
    assert row(led, a)["state"] == "queued" and card.posts == [] and gates_rows(led) == [("text", "standing"), ("text", "queued")]
    h, led, card = setup(tmp_path / "off", release=True, out=False)              # text-out off: nothing goes on its own
    a = decided(led, decision="reply", state="release-ready")
    asyncio.run(h.tick())
    assert row(led, a)["state"] == "held" and len(card.posts) == 1
    assert asyncio.run(h.word("owner-room", "owner", "send", "100")) is True
    assert row(led, a)["state"] == "held" and card.posts[-1][2] == words.TEXTING_OUT_OFF
    h, led, card = setup(tmp_path / "dry", mode="dry", release=True)
    a = decided(led, decision="reply", state="release-ready")
    asyncio.run(h.tick())
    assert row(led, a)["state"] == "would-send" and card.posts == [] and ("text", "standing") not in gates_rows(led)
    day = next((tmp_path / "dry" / "state" / "texting" / "dry").iterdir()).read_text().splitlines()
    assert json.loads(day[-1])["standing"] is True


def test_a_word_in_the_card_room_is_consumed_and_every_other_room_and_message_starts_a_turn_as_before(tmp_path: Path) -> None:
    """Through the Discord adapter: the owner's reply `send` to a card in the card room's own channel is filed (no turn, no ack)
    and releases it; an ordinary message, a reply to an ordinary message and a bare `send` in another room each start a turn
    as before, and so does a person's `send` in the card room."""
    h, led, _ = setup(tmp_path / "t", to="test-room")
    a = decided(led)
    client = Client()
    d = DiscordChannel("token", OWNER, [], led, client=client, state=tmp_path, word=h.word)
    room, other = Room(tmp_path), Room(tmp_path)
    other.name = "other-room"
    d.add_room(ROOM_CH, room, people=frozenset({PERSON}))
    d.add_room(22, other)
    asyncio.run(h.tick())

    def reply(ch: Any, mid: int, text: str, to: int, author: int = OWNER) -> Msg:
        m = Msg(ch, mid, text, author=author)
        m.type, m.reference = discord.MessageType.reply, NS(message_id=to, resolved=None)
        return m
    ours, theirs = client.chans[ROOM_CH], client.chans[22]
    for m in (Msg(theirs, 1, "hello"), reply(theirs, 2, "send", 1), Msg(theirs, 3, "send"), reply(ours, 4, "send", 100, author=PERSON)):
        asyncio.run(d.on_message(m))
    assert [(m.id, m.sender) for m in other.got] == [("1", "owner"), ("2", "owner"), ("3", "owner")]
    assert [(m.id, m.sender) for m in room.got] == [("4", "person")] and row(led, a)["state"] == "held"
    asyncio.run(d.on_message(reply(ours, 5, "send", 100)))
    assert [m.id for m in room.got] == ["4"] and row(led, a)["state"] == "queued"
    assert (led.inbound_get("5") or {})["state"] == "filed" and (5, "+", ACK) not in ours.reactions

    async def broken(*a: Any) -> bool:
        raise RuntimeError("the ledger could not be read")
    d.word = broken                               # a word that cannot be read: the message is an ordinary one, never lost
    asyncio.run(d.on_message(Msg(ours, 6, "send")))
    assert [m.id for m in room.got] == ["4", "6"]
