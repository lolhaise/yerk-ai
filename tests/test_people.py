"""Checks that a room that answers other people starts answering one more person in its channel when you ask it to there (the
`people` tool). The case behind it (need-2026-11-19-126): the owner, in a person's room, asked it to answer a second person
who wrote in the same channel, whose message was filed as "other" and got no reply; the room's `people` lives in rooms.toml,
which no room reads or writes, and no tool changed who a room answers. Now the owner's own words of the turn add a person by
one of their messages there, answered from their next message on and at every start after, kept in the daemon's override
refused wherever rooms.toml's `people` is. Every value here is a placeholder (decision 22)."""
from __future__ import annotations

import asyncio
import json
import shutil
from types import SimpleNamespace as NS

import discord
import pytest
from test_discord import BOT, HOOK, OTHER_CH, OWNER, ROOM_CH, Client, Msg, run

from yerk_ai import mcp_server, watch, words
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import EXAMPLE_PRIVATE, override, people
from yerk_ai.daemon import YERK_TOOLS, Yerkd
from yerk_ai.room import Inbound
from fake_channel import ConsoleChannel

LISTED, NEWCOMER, OTHER_BOT = 31, 41, 51          # rooms.toml's person, one who wrote here unanswered, another assistant
ASK = "please answer the newcomer here too"
ROOM = ('\n[rooms.her-room]\nchannel = "discord:{ch}"\naccounts = ["one", "two"]\nmodel = "claude-fable-5-1"\ntools = {tools}\n'
        'privacy = "{privacy}"\npeople = [{listed}]\ntimezone = "UTC"\nlayer = "layers/her-room"\npersona = "persona.md"\n'
        'rulebook = "rulebook.md"\n')


def install(tmp_path, privacy="family", tools='["Read"]', listed=LISTED, watch_ids=()):
    """The example rooms and her-room, a person's room on the fake Discord channel ROOM_CH listing `listed`, with a layer of
    its own; [watch] assistants `watch_ids`."""
    priv, state = tmp_path / "private", tmp_path / "state"
    if not priv.exists():
        shutil.copytree(EXAMPLE_PRIVATE, priv)
        (lay := priv / "layers" / "her-room").mkdir(parents=True)
        for f in ("persona.md", "rulebook.md"):
            shutil.copy(priv / f, lay / f)
        with (priv / "rooms.toml").open("a") as f:
            f.write(ROOM.format(ch=ROOM_CH, tools=tools, privacy=privacy, listed=listed or ""))
        if watch_ids:
            with (priv / "yerkd.toml").open("a") as f:
                f.write(f"\n[watch]\nassistants = {list(watch_ids)}\n")
        state.mkdir()
    return priv, state


def start(tmp_path, **kw):
    """A daemon on install's folders, her-room on the adapter as run_discord adds it, its turns kept, never run."""
    priv, state = install(tmp_path, **kw)
    y, client = Yerkd(priv, state), Client()
    dc = DiscordChannel("token", OWNER, [HOOK], y.ledger, client=client, state=state, assistants=frozenset({OTHER_BOT}))
    room = y.add_room("her-room", dc)
    dc.add_room(ROOM_CH, room, None, people("her-room", y.room_confs["her-room"]))
    taken: list[Inbound] = []
    room.submit = lambda m: taken.append(m) or "started"
    ch = client.chans[ROOM_CH]
    ch.old = {501: Msg(ch, 501, "can it answer me too?", author=NEWCOMER, name="newcomer"),
              502: Msg(ch, 502, "the owner's own line", author=OWNER), 503: Msg(ch, 503, "a bot's line", author=OTHER_BOT),
              504: Msg(ch, 504, "a hook's line", webhook_id=HOOK), 505: Msg(ch, 505, "Yerk's own line", author=BOT)}
    return y, dc, ch, room, taken


def asked(room, text=ASK, sender="owner"):
    room.busy, room.inflight = True, [Inbound("m-9", sender, text)]


def tool(y, op="", person="", said=ASK):
    return asyncio.run(y.tool_people("her-room", op, person, said))


def test_the_owner_s_ask_answers_a_person_who_wrote_here_from_their_next_message_on_and_rooms_toml_is_never_written(tmp_path):
    """The wall: the newcomer's message gets no turn (filed as "other"); the owner's ask adds them by that message's id, the
    [m:ID] its day-log line ends with, and their next message is a person's turn; kept in the override, rooms.toml as it was."""
    y, dc, ch, room, taken = start(tmp_path)
    toml = (tmp_path / "private" / "rooms.toml").read_bytes()
    run(dc.on_message(Msg(ch, 600, "hello? anyone?", author=NEWCOMER, name="newcomer")))
    assert taken == [] and dc.sender_of(ch.old[501]) is None                  # the wall the room hit
    asked(room)
    got = tool(y, "add", "501")
    assert got == {"ok": True, "line": words.PEOPLE_ADDED.format(who="@newcomer")}
    run(dc.on_message(Msg(ch, 601, "thanks, so what about Friday?", author=NEWCOMER, name="newcomer")))
    assert [(m.id, m.sender) for m in taken] == [("601", "person")]
    assert NEWCOMER in people("her-room", y.room_confs["her-room"]) and LISTED in people("her-room", y.room_confs["her-room"])
    kept = override(tmp_path / "state", "her-room")["people"]
    assert [(p["id"], p["name"], p["by"]) for p in kept] == [(str(NEWCOMER), "newcomer", "m:m-9")]
    assert (tmp_path / "private" / "rooms.toml").read_bytes() == toml
    assert tool(y, "add", f"https://discord.com/channels/1/{ROOM_CH}/501") == {"ok": True, "line": words.PEOPLE_ALREADY.format(who="@newcomer")}
    line = tool(y)["line"]
    assert line == words.PEOPLE_NOW.format(also=words.PEOPLE_ONE.format(who="@newcomer", day=kept[0]["day"]) + words.PEOPLE_LISTED.format(n=1))


def test_a_room_answering_two_people_is_told_which_of_them_sent_each_message(tmp_path):
    """The cold read: with the newcomer added, the listed person's message and theirs reached the room alike, "from person"
    and the text, so a person's room (its persona, card and open items all its one person's) would answer the newcomer as
    that person. Now in a room that answers more than one, a line above each person's message names who sent it, by the
    name the server shows them by and their handle; the owner's and a one-person room's read as before."""
    y, dc, ch, room, taken = start(tmp_path)
    run(dc.on_message(Msg(ch, 600, "only me here so far", author=LISTED, name="listed")))
    asked(room)
    assert tool(y, "add", "501")["ok"]
    run(dc.on_message(Msg(ch, 601, "what did I ask you?", author=NEWCOMER, name="newcomer")))
    shown = Msg(ch, 602, "what did I ask you?", author=LISTED, name="listed")
    shown.author.display_name = "Her  Name"
    run(dc.on_message(shown))
    run(dc.on_message(Msg(ch, 603, "the owner's word", author=OWNER)))
    assert [(m.sender, m.text) for m in taken] == [
        ("person", "only me here so far"),
        ("person", f"{words.PERSON_BY.format(who='@newcomer', n=2)}\nwhat did I ask you?"),
        ("person", f"{words.PERSON_BY.format(who='Her Name (@listed)', n=2)}\nwhat did I ask you?"),
        ("owner", "the owner's word")]
    asked(room, "stop answering the newcomer here")
    assert tool(y, "drop", "@newcomer", "stop answering the newcomer")["ok"]
    run(dc.on_message(Msg(ch, 604, "just me again", author=LISTED, name="listed")))
    assert taken[-1].text == "just me again"


def test_a_restart_answers_them_still_and_a_drop_on_the_owner_s_word_ends_it(tmp_path):
    """The next start joins the added person to rooms.toml's list before the adapter takes the room; `drop` by handle ends it
    at once; one rooms.toml lists is the owner's file to edit, and an unknown one is named with who was added."""
    y, dc, ch, room, _ = start(tmp_path)
    asked(room)
    assert tool(y, "add", "501")["ok"]
    y2, dc2, ch2, room2, taken = start(tmp_path)
    assert people("her-room", y2.room_confs["her-room"]) == frozenset({LISTED, NEWCOMER})
    run(dc2.on_message(Msg(ch2, 700, "back again", author=NEWCOMER, name="newcomer")))
    assert [m.sender for m in taken] == ["person"]
    asked(room2, "stop answering the newcomer here")
    assert tool(y2, "drop", str(LISTED), "stop answering the newcomer") == {"ok": False, "line": words.PEOPLE_IN_TOML.format(who=str(LISTED))}
    assert tool(y2, "drop", "@someone", "stop answering the newcomer")["line"] == words.PEOPLE_NO_ONE.format(given="@someone", who="@newcomer")
    assert tool(y2, "drop", "@Newcomer", "stop answering the newcomer") == {"ok": True, "line": words.PEOPLE_DROPPED.format(who="@newcomer")}
    run(dc2.on_message(Msg(ch2, 701, "hello again?", author=NEWCOMER, name="newcomer")))
    assert len(taken) == 1 and people("her-room", y2.room_confs["her-room"]) == frozenset({LISTED})
    assert override(tmp_path / "state", "her-room")["people"] == []
    y3 = Yerkd(tmp_path / "private", tmp_path / "state")
    assert people("her-room", y3.room_confs["her-room"]) == frozenset({LISTED})


def test_one_added_by_asking_and_since_listed_in_rooms_toml_is_the_owner_s_to_drop_there(tmp_path):
    """The owner listed the added person in rooms.toml too: the start answers them once, and `drop` says rooms.toml still
    answers them, never that they are dropped, and keeps the override as it was."""
    y, dc, ch, room, _ = start(tmp_path)
    asked(room)
    assert tool(y, "add", "501")["ok"]
    toml = tmp_path / "private" / "rooms.toml"
    toml.write_text(toml.read_text().replace(f"people = [{LISTED}]", f"people = [{LISTED}, {NEWCOMER}]"))
    y2, dc2, ch2, room2, _ = start(tmp_path)
    assert sorted(y2.room_confs["her-room"]["people"]) == [LISTED, NEWCOMER]
    asked(room2)
    assert tool(y2, "drop", "@newcomer") == {"ok": False, "line": words.PEOPLE_IN_TOML.format(who="@newcomer")}
    kept = override(tmp_path / "state", "her-room")["people"]
    assert [p["id"] for p in kept] == [str(NEWCOMER)]
    assert tool(y2)["line"] == words.PEOPLE_NOW.format(also=words.PEOPLE_ONE.format(who="@newcomer", day=kept[0]["day"]) +
                                                       words.PEOPLE_LISTED.format(n=1))


def test_only_the_owner_s_own_words_of_this_turn_add_or_drop_anyone(tmp_path):
    """A person's message asking it, the owner's words of no message in this turn, or a scrap under three words change
    nothing: a person never widens who the room answers."""
    y, dc, ch, room, taken = start(tmp_path)
    asked(room, "add my friend so it answers them too", sender="person")
    assert tool(y, "add", "501", "add my friend so it answers them") == {"ok": False, "line": words.PEOPLE_NOT_SAID}
    asked(room)
    for said in ("answer them here too, I said", "answer", ""):
        assert tool(y, "add", "501", said) == {"ok": False, "line": words.PEOPLE_NOT_SAID}
        assert tool(y, "drop", "@newcomer", said) == {"ok": False, "line": words.PEOPLE_NOT_SAID}
    assert "people" not in override(tmp_path / "state", "her-room") and dc.sender_of(ch.old[501]) is None
    assert tool(y, "invite", "501") == {"ok": False, "line": words.PEOPLE_OPS}


def test_a_message_names_a_person_only_when_one_wrote_it_in_this_room_s_own_channel(tmp_path, monkeypatch):
    """The owner, Yerk itself, another assistant, a bot or a webhook is never added; a link to another channel, a message
    gone or a name in place of a message says so in words, and nothing is kept."""
    y, dc, ch, room, _ = start(tmp_path)
    asked(room)
    assert tool(y, "add", "502") == {"ok": False, "line": words.PEOPLE_THE_OWNER}
    for mid in ("503", "504", "505"):
        assert tool(y, "add", mid) == {"ok": False, "line": words.PEOPLE_BOT}, mid
    assert tool(y, "add", f"https://discord.com/channels/1/{OTHER_CH}/501") == {"ok": False, "line": words.PEOPLE_NOT_HERE}
    assert tool(y, "add", "newcomer") == {"ok": False, "line": words.PEOPLE_NOT_MESSAGE.format(given="newcomer")}

    async def gone(mid):
        raise discord.NotFound(NS(status=404, reason="Not Found"), {"code": 10008, "message": "Unknown Message"})
    monkeypatch.setattr(ch, "fetch_message", gone)
    assert tool(y, "add", "501") == {"ok": False, "line": words.WATCH_NO_MESSAGE}
    assert "people" not in override(tmp_path / "state", "her-room")


@pytest.mark.parametrize("kw, line", [({"privacy": "owner", "listed": 0}, words.PEOPLE_OWNER_ROOM),
                                      ({"tools": '["Read", "Bash"]', "listed": 0}, words.PEOPLE_SHELL)])
def test_a_room_rooms_toml_could_not_give_people_is_given_none_by_asking(tmp_path, kw, line):
    """Where the start refuses rooms.toml's `people` (an owner room reads the owner's whole memory; a shell or a read folder
    reads past the classes), the ask is refused in the same terms, before any message is read."""
    y, dc, ch, room, _ = start(tmp_path, **kw)
    asked(room)
    assert tool(y, "add", "501") == {"ok": False, "line": line}
    assert "people" not in override(tmp_path / "state", "her-room")
    assert tool(y) == {"ok": True, "line": words.PEOPLE_NOW.format(also="")}


def test_a_room_with_no_layer_of_its_own_is_refused_and_a_console_room_reads_no_message(tmp_path):
    """A family room whose files sit in the private folder's top (context.walls) is refused by the layer rule; a room on no
    Discord channel cannot look a message up."""
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    head, _, rest = (priv / "rooms.toml").read_text().partition("[rooms.example-quiet]")
    (priv / "rooms.toml").write_text(head + "[rooms.example-quiet]" + rest.replace('privacy = "owner"', 'privacy = "family"', 1))
    state.mkdir()
    y = Yerkd(priv, state)
    y.add_room("example-quiet", ConsoleChannel())
    asked(y.rooms["example-quiet"])
    assert asyncio.run(y.tool_people("example-quiet", "add", "501", ASK)) == {"ok": False, "line": words.PEOPLE_LAYER}
    y2, dc, ch, room, _ = start(tmp_path / "two")
    y2.channels["her-room"] = ConsoleChannel()
    asked(room)
    assert tool(y2, "add", "501") == {"ok": False, "line": words.WATCH_NO_DISCORD}


def test_a_start_never_stops_on_the_file_and_joins_no_one_where_rooms_toml_now_forbids_it(tmp_path, caplog):
    """The override is the daemon's: a room since made an owner room in rooms.toml, or a person since listed as another
    assistant, joins no one, says so in the log and starts; the file keeps them for the owner's next word."""
    y, dc, ch, room, _ = start(tmp_path)
    asked(room)
    assert tool(y, "add", "501")["ok"]
    toml = tmp_path / "private" / "rooms.toml"
    toml.write_text(toml.read_text().replace('privacy = "family"\npeople = [31]', 'privacy = "owner"\npeople = []'))
    y2 = Yerkd(tmp_path / "private", tmp_path / "state")
    assert people("her-room", y2.room_confs["her-room"]) == frozenset() and "answers none of the 1 the owner added" in caplog.text
    toml.write_text(toml.read_text().replace('privacy = "owner"\npeople = []', 'privacy = "family"\npeople = [31]'))
    with (tmp_path / "private" / "yerkd.toml").open("a") as f:
        f.write(f"\n[watch]\nassistants = [{NEWCOMER}]\n")
    caplog.clear()
    y3 = Yerkd(tmp_path / "private", tmp_path / "state")
    assert people("her-room", y3.room_confs["her-room"]) == frozenset({LISTED})
    assert [p["id"] for p in override(tmp_path / "state", "her-room")["people"]] == [str(NEWCOMER)]
    assert f"answers {NEWCOMER} no more: [watch] assistants lists them" in caplog.text      # said in the log, as the changelog says


def test_every_room_is_handed_the_tool_and_settings_keeps_the_people_it_did_not_change(tmp_path):
    """mcp__yerk__people in every room's allow list, its entry taking op, person and said; the owner's /effort there, which
    writes the same override, keeps the people."""
    y, dc, ch, room, _ = start(tmp_path)
    assert "mcp__yerk__people" in YERK_TOOLS and "mcp__yerk__people" in y.spec_for("her-room").settings["permissions"]["allow"]
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert list(tools["people"].inputSchema["properties"]) == ["op", "person", "said"]
    asked(room)
    assert tool(y, "add", "501")["ok"]
    room.respec = lambda: None                                      # no process to close here
    assert asyncio.run(y.tool_settings("her-room", "", "high", "", by_owner=True))["ok"]
    over = json.loads((tmp_path / "state" / "rooms" / "her-room" / "override.json").read_text())
    assert over["effort"] == "high" and [p["id"] for p in over["people"]] == [str(NEWCOMER)]
    assert watch.message(f"https://discord.com/channels/1/{ROOM_CH}/501") == (ROOM_CH, 501)
