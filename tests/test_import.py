"""Checks `yerk import`, which files a Discord chat's past messages into the memory, against a fake Discord server and a fixture
private layer. The cases: one chat's whole history filed under discord:<id> with no room made (its label, its class, a second run
only tops up), --dry-run writing nothing and printing every line it would file, attachments named with their size and saved only
with --files, the server's inventory (every chat, a covered one marked, one the bot cannot read and its threads not asked for)
of every chat no room covers past one that fails, and a person's old channel filed behind her room's wall."""
import json
from datetime import datetime, timedelta, timezone

import discord
import pytest
from discord.utils import time_snowflake

import yerk_ai.move as move
from test_move import BEN, COUSIN, OWNER, UNCLE, message, tree
from yerk_ai import cli, memory

GUILD, KID = 100, 7
ROOMS = f"""[rooms.home]
channel = "discord:10"
privacy = "owner"

[rooms.cousin-room]
channel = "discord:20"
privacy = "family"
people = [{COUSIN}]
tools = ["Read", "Grep", "Glob"]

[rooms.kid-room]
channel = "discord:21"
privacy = "family"
people = [{KID}]
tools = ["Read", "Grep", "Glob"]
"""
LAST = time_snowflake(datetime(2026, 5, 1, 12, tzinfo=timezone.utc))
CHANNELS = [{"id": "1", "type": 4, "name": "Text Channels", "position": 0},
            {"id": "10", "type": 0, "name": "home", "position": 0, "parent_id": "1"},
            {"id": "30", "type": 0, "name": "old-chat", "position": 1, "parent_id": "1"},
            {"id": "40", "type": 0, "name": "secret", "position": 2, "parent_id": "1", "last_message_id": str(LAST)},
            {"id": "50", "type": 0, "name": "noise", "position": 3, "parent_id": "1"},
            {"id": "60", "type": 0, "name": "cousin-and-me", "position": 4, "parent_id": "1"},
            {"id": "70", "type": 2, "name": "Lounge", "position": 5}]
ACTIVE = [{"id": "31", "type": 11, "name": "a thread", "parent_id": "30", "message_count": 1},
          {"id": "41", "type": 11, "name": "hidden thread", "parent_id": "40", "message_count": 7}]
ARCHIVED = [{"id": f"3{i}", "type": 11, "name": f"old {i}", "parent_id": "30",       # newest archive first, as Discord pages them
             "thread_metadata": {"archive_timestamp": f"2026-04-0{9 - i}T00:00:00+00:00"}} for i in (2, 3)]


def msg(hour, author, text="", files=()):
    """A message `hour` hours into March 2026, with an id that holds that time, as a Discord id does."""
    at = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(hours=hour)
    return {**message(hour, author, text, files, at=at), "id": str(time_snowflake(at))}


def forbidden():
    return discord.Forbidden(type("R", (), {"status": 403, "reason": "Forbidden"})(), "Missing Access")


class FakeGuild:
    """The REST calls `yerk import` makes, answered from fixtures: each chat's own messages, the channels, the active
    threads and the archived ones (handed out one a page, so the paging runs), the private listing refused."""

    def __init__(self):
        self.chats = {"10": [msg(0, OWNER, "a room's own line")], "30": [msg(1, OWNER, "first"), msg(2, BEN, "second"),
                      msg(30, COUSIN, "next day")], "31": [msg(3, OWNER, "in the thread")], "32": [msg(4, BEN, "archived")],
                      "33": [msg(5, OWNER, "archived too")], "50": [msg(6, OWNER, "noise")], "70": [],
                      "60": [msg(7, OWNER, "hi"), msg(8, COUSIN, "hello from the cousin")]}
        self.forbidden, self.calls = {"40", "41"}, []

    async def get_all_guild_channels(self, guild):
        assert guild == str(GUILD)
        return [dict(c) for c in CHANNELS]

    async def get_active_threads(self, guild):
        return {"threads": [dict(t) for t in ACTIVE]}

    async def get_public_archived_threads(self, cid, before=None, limit=50):
        self.calls.append(("archived", cid, before))
        left = [dict(t) for t in ARCHIVED if t["parent_id"] == cid and (before is None or t["thread_metadata"]["archive_timestamp"] < before)]
        return {"threads": left[:1], "has_more": len(left) > 1}

    async def get_private_archived_threads(self, cid, before=None, limit=50):
        raise forbidden()

    async def get_joined_private_archived_threads(self, cid, before=None, limit=50):
        return {"threads": [], "has_more": False}

    async def get_channel(self, cid):
        return next(dict(c) for c in CHANNELS + ACTIVE + ARCHIVED if c["id"] == cid)

    async def logs_from(self, cid, limit, before=None):
        self.calls.append(("logs", cid, before))
        if cid in self.forbidden:
            raise forbidden()
        older = [m for m in sorted(self.chats.get(cid, []), key=lambda m: -int(m["id"])) if before is None or int(m["id"]) < int(before)]
        return older[:limit]

    async def get_from_cdn(self, url):
        self.calls.append(("cdn", url))
        return f"bytes of {url}".encode()


@pytest.fixture
def server(tmp_path, monkeypatch):
    priv, state = tmp_path / "private", tmp_path / "state"
    for d in (priv / "secrets", state):
        d.mkdir(parents=True)
    (priv / "secrets" / "discord.token").write_text("fake-token\n")
    (priv / "yerkd.toml").write_text(f'[discord]\ntoken_file = "secrets/discord.token"\nguild = {GUILD}\nowner = {OWNER}\n\n'
                                     '[memory]\nroot = "memory"\ntimezone = "UTC"\n\n'
                                     '[import]\nskip = ["50"]\nclasses = {"60" = "room:discord:20"}\n')
    (priv / "rooms.toml").write_text(ROOMS)
    fake = FakeGuild()

    class Client:
        def __init__(self, **kw):
            self.http = fake

        async def login(self, token):
            assert token == "fake-token"

        async def close(self):
            pass
    monkeypatch.setattr(move.discord, "Client", Client)
    return priv, state, fake


def run(priv, state, *args):
    return cli.main(["import", *args, "--private", str(priv), "--state", str(state)])


def day_lines(priv):
    return [x for f in sorted((priv / "memory" / "daily").glob("*.md")) for x in f.read_text().splitlines() if x.startswith("- ")]


def test_a_chat_is_filed_under_its_own_id_and_name_as_private_with_no_room_made_and_a_second_run_adds_nothing(server, capsys):
    priv, state, fake = server
    rooms_before = (priv / "rooms.toml").read_text()
    assert run(priv, state, "discord:30") == 0
    out = capsys.readouterr().out
    lines = day_lines(priv)
    assert [x.split(" ", 4)[2:4] for x in lines] == [["old-chat#discord:30", "owner@owner-handle:"], ["old-chat#discord:30", "other@Ben:"],
                                                    ["old-chat#discord:30", "other@cousin-handle:"]]
    assert all(" [c:private] " in x for x in lines) and (priv / "rooms.toml").read_text() == rooms_before
    assert out.startswith("#old-chat 30 (text): 3 messages, 2026-03-01 to 2026-03-02, 3 filed, 0 there already; 0 files up to 25 MB, "
                          "named only; not covered; class private\n")
    fake.chats["30"].append(msg(40, OWNER, "a new one"))
    assert run(priv, state, "discord:30") == 0
    assert "4 messages, 2026-03-01 to 2026-03-02, 1 filed, 3 there already" in capsys.readouterr().out and len(day_lines(priv)) == 4
    assert run(priv, state, "discord:30") == 0 and "0 filed, 4 there already" in capsys.readouterr().out and len(day_lines(priv)) == 4


def test_a_dry_run_writes_nothing_and_prints_every_line_it_would_file(server, capsys):
    priv, state, fake = server
    before = tree(priv)
    assert run(priv, state, "discord:30", "--dry-run") == 0
    out = capsys.readouterr().out.splitlines()
    lock = priv / "memory" / ".daily.lock"                   # the day logs' writers' lock: the one file a dry run may make
    assert {p: b for p, b in tree(priv).items() if p != lock} == before
    assert "3 messages, 2026-03-01 to 2026-03-02, 3 to file, 0 there already" in out[0]
    assert out[1:] == ["- 01:00:00+00:00 old-chat#discord:30 owner@owner-handle: first [c:private] [m:%s]" % fake.chats["30"][0]["id"],
                       "- 02:00:00+00:00 old-chat#discord:30 other@Ben: second [c:private] [m:%s]" % fake.chats["30"][1]["id"],
                       "- 06:00:00+00:00 old-chat#discord:30 other@cousin-handle: next day [c:private] [m:%s]" % fake.chats["30"][2]["id"]]
    assert run(priv, state, "discord:30", "--since", "2026-03-02", "--dry-run") == 0
    assert "1 messages, 2026-03-02 to 2026-03-02, 1 to file" in capsys.readouterr().out


def test_attachments_are_named_with_their_size_and_saved_only_with_files(server, capsys):
    priv, state, fake = server
    fake.chats["30"] = [msg(1, OWNER, "photos", files=[("photo.png", 1234), ("big.mov", 30_000_000)])]
    assert run(priv, state, "discord:30") == 0
    text = (priv / "memory" / "daily" / "2026-03-01.md").read_text()
    assert "  - attachment: photo.png, image/png, 1,234 bytes\n  - attachment: big.mov, image/png, 30,000,000 bytes" in text
    assert not any(c[0] == "cdn" for c in fake.calls) and not (priv / "attachments").exists() and "saved at" not in text
    assert "1 files up to 25 MB, named only" in capsys.readouterr().out
    fake.chats["31"] = [msg(3, OWNER, "one more", files=[("photo.png", 99)])]
    assert run(priv, state, "discord:31", "--files") == 0
    saved = priv / "attachments" / "discord-31" / f"{fake.chats['31'][0]['id']}-photo.png"
    assert saved.read_bytes() == b"bytes of https://cdn/1003/0" and saved.stat().st_mode & 0o777 == 0o600
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in (saved.parent, saved.parent.parent))
    assert f"photo.png (saved at {saved})" in (priv / "memory" / "daily" / "2026-03-01.md").read_text()
    assert "1 files up to 25 MB, 1 saved" in capsys.readouterr().out


def test_the_inventory_lists_every_chat_marks_a_covered_one_and_writes_nothing(server, capsys):
    priv, state, fake = server
    before = tree(priv)
    assert run(priv, state, f"discord-guild:{GUILD}", "--dry-run") == 1          # a chat the bot cannot read is said, and counted
    out = capsys.readouterr().out.splitlines()
    assert {p: b for p, b in tree(priv).items() if p.name != ".daily.lock"} == before
    assert [x.split(":", 1)[0] for x in out[:-1]] == ["#Lounge 70 (voice)", "#home 10 (text)", "#old-chat 30 (text)",   # no category first
                                                     "  #old 3 33 (thread in #old-chat)", "  #old 2 32 (thread in #old-chat)",
                                                     "  #a thread 31 (thread in #old-chat)", "#secret 40 (text)",
                                                     "  #hidden thread 41 (thread in #secret)", "#noise 50 (text)", "#cousin-and-me 60 (text)"]
    row = dict(x.split(": ", 1) for x in out[:-1])
    assert row["#home 10 (text)"].endswith("1 to file, 0 there already; 0 files up to 25 MB, named only; covered by home; class private")
    assert row["#old-chat 30 (text)"].endswith("; not covered; class private")
    assert row["#secret 40 (text)"] == ("not read, Yerk's bot cannot read it (403); how many unknown; made 2015-01-01, last message "
                                        "2026-05-01; not covered; class private")
    assert row["  #hidden thread 41 (thread in #secret)"].startswith("not read, its channel is not readable; about 7 messages by "
                                                                     "Discord's own thread count")
    assert ("logs", "41", None) not in fake.calls and ("archived", "30", "2026-04-07T00:00:00+00:00") in fake.calls  # one a page
    assert row["#noise 50 (text)"].endswith("in [import] skip; class private")
    assert row["#cousin-and-me 60 (text)"].endswith("not covered; class room behind cousin-room's wall")
    assert row["#Lounge 70 (voice)"].startswith("0 messages, 0 to file")
    assert out[-1] == "10 chats: 8 read, 10 messages, 10 to file; 2 not read; 1 covered by a room; 1 in [import] skip"


def test_the_server_import_files_every_chat_no_room_covers_and_carries_on_past_one_it_cannot_read(server, capsys):
    priv, state, fake = server
    assert run(priv, state, f"discord-guild:{GUILD}") == 1
    out = capsys.readouterr().out
    assert "#home 10 (text): left as it is; covered by home" in out and "#noise 50 (text): left as it is; in [import] skip" in out
    assert "#secret 40 (text): not read, Yerk's bot cannot read it (403)" in out and "#Lounge 70 (voice): 0 messages" in out
    labels = sorted({x.split(" ")[2] for x in day_lines(priv)})
    assert labels == ["a_thread#discord:31", "cousin-and-me#discord:20", "old-chat#discord:30", "old_2#discord:32", "old_3#discord:33"]
    assert out.endswith("10 chats: 6 read, 8 messages, 8 filed; 2 not read; 1 covered by a room; 1 in [import] skip\n")
    assert run(priv, state, f"discord-guild:{GUILD}") == 1 and "6 read, 8 messages, 0 filed" in capsys.readouterr().out


def test_a_persons_old_channel_is_filed_behind_her_rooms_wall(server, capsys):
    priv, state, fake = server
    assert run(priv, state, "discord:60") == 0
    assert "class room behind cousin-room's wall" in capsys.readouterr().out
    path = priv / "memory" / "daily" / "2026-03-01.md"
    lines = day_lines(priv)
    assert [x.split(" ", 4)[2:4] for x in lines] == [["cousin-and-me#discord:20", "owner@owner-handle:"],
                                                    ["cousin-and-me#discord:20", "person@cousin-handle:"]]
    assert all(" [c:room] " in x for x in lines)
    daily, text = priv / "memory" / "daily", path.read_text()
    cousin, kid = memory.Reader("cousin-room", "discord:20"), memory.Reader("kid-room", "discord:21")
    assert memory.allowed_text(cousin, path, text, daily=daily) == text.rstrip("\n")             # her room reads it
    assert memory.allowed_text(kid, path, text, daily=daily) is None                               # another person room does not
    assert memory.allowed_text(memory.Reader("home", "discord:10", owner=True), path, text, daily=daily) == text


def test_one_her_room_answers_by_asking_is_filed_as_a_person_there_too(server):
    """need-2026-11-19-126's cold read: one the owner had cousin-room answer by asking (the people tool keeps them in the
    daemon's override in the state folder, never in rooms.toml) is filed as a person in the import behind its wall, as the
    daemon files their live lines, never as other."""
    priv, state, fake = server
    (state / "rooms" / "cousin-room").mkdir(parents=True)
    (state / "rooms" / "cousin-room" / "override.json").write_text(json.dumps({"people": [{"id": str(UNCLE), "name": "uncle-handle"}]}))
    fake.chats["60"].append({**msg(9, COUSIN, "and from the uncle"), "author": {"id": str(UNCLE), "username": "uncle-handle"}})
    assert run(priv, state, "discord:60") == 0
    assert [x.split(" ", 4)[3] for x in day_lines(priv)] == ["owner@owner-handle:", "person@cousin-handle:", "person@uncle-handle:"]


def test_a_class_that_names_no_room_or_a_source_it_does_not_take_stops_before_anything_is_read(server, capsys):
    priv, state, fake = server
    conf = priv / "yerkd.toml"
    conf.write_text(conf.read_text().replace("room:discord:20", "room:discord:99"))
    assert run(priv, state, "discord:60") == 2
    assert "yerkd.toml [import] classes: 60: each is private, shared or room:<the id of a room in rooms.toml>" in capsys.readouterr().err
    conf.write_text(conf.read_text().replace("room:discord:99", "shared"))
    assert run(priv, state, "imessage:60") == 2 and run(priv, state, "discord:abc") == 2 and fake.calls == []
    assert run(priv, state, "discord:60") == 0 and all(" [c:shared] " in x and "#discord:60 " in x for x in day_lines(priv))
