"""Checks `yerk room add`, which moves a Discord chat in as a new room, against a fake Discord REST client and a fixture
private layer. The cases: the channel (its overwrites copied from the room it is made like), the voice, the block (loaded and
through the people guard before anything changes), the one history import (roles, attachments, a second run only tops up),
room's own layer behind the wall; and --dry-run writing and sending nothing."""
import os
import tomllib
from datetime import datetime, timedelta, timezone

import discord
import pytest

import yerk_ai.move as move
from yerk_ai import cli, context, memory
from yerk_ai.config import REPO, rooms, toml_value

GUILD, OWNER, BOT, BEN, MEMBER_A, MEMBER_B, OLD = 100, 1, 2, 3, 5, 6, 30
PERSON = tomllib.loads((REPO / "examples" / "rooms" / "person.toml").read_text())
HOME_OWS = [{"id": str(OWNER), "type": 1, "allow": "536988752", "deny": "0"}, {"id": str(GUILD), "type": 0, "allow": "0", "deny": "1024"},
            {"id": str(BOT), "type": 1, "allow": "536988736", "deny": "0"}, {"id": str(BEN), "type": 1, "allow": "117824", "deny": "0"}]
ROOMS = f"""[rooms.home]
channel = "discord:10"
layer = "desk"
model = "claude-opus-5-5"
tools = ["Read", "Grep", "Glob", "Bash"]
privacy = "owner"
persona = "persona.md"
rulebook = "rulebook.md"
voice = "rooms/home.md"
allow = ["mcp__web__fetch"]

[rooms.home.mcp.web]
type = "stdio"
args = ["server.mjs"]
timeout = 180000

[rooms.home.sandbox.filesystem]
denyWrite = ["//elsewhere"]

[rooms.aunt-room]
channel = "discord:20"
layer = "aunt"
model = "claude-opus-5-5"
tools = ["Read", "Grep", "Glob"]
privacy = "family"
people = [{AUNT}]
persona = "persona.md"
rulebook = "rulebook.md"
voice = "rooms/aunt.md"
pinned_memory = "pinned.md"
open_loops = "open-loops.md"
"""


class FakeHTTP:
    """discord.py's REST calls the command makes, answered from fixtures; every call is recorded."""

    def __init__(self, msgs):
        self.msgs, self.calls, self.forbidden = msgs, [], set()
        self.channels = [{"id": "10", "name": "home", "type": 0}, {"id": "20", "name": "aunt-room", "type": 0}]
        self.hooks: dict[str, list] = {}

    async def get_all_guild_channels(self, guild):
        self.calls.append(("channels", guild))
        return list(self.channels)

    async def get_channel(self, cid):
        self.calls.append(("channel", cid))
        return {"id": cid, "permission_overwrites": HOME_OWS + ([{"id": str(MEMBER_A), "type": 1, "allow": "117824", "deny": "0"}]
                                                              if cid == "20" else [])}

    async def create_channel(self, guild, kind, **kw):
        self.calls.append(("create_channel", guild, kind, kw))
        self.channels.append({"id": "77", "name": kw["name"], "type": kind})
        return {"id": "77"}

    async def logs_from(self, cid, limit, before=None):
        self.calls.append(("logs", cid, before))
        if str(cid) in self.forbidden:
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": "Forbidden"})(), "Missing Access")
        older = [m for m in sorted(self.msgs, key=lambda m: -int(m["id"])) if before is None or int(m["id"]) < int(before)]
        return older[:limit]

    async def get_from_cdn(self, url):
        self.calls.append(("cdn", url))
        return f"bytes of {url}".encode()

    async def channel_webhooks(self, cid):
        self.calls.append(("webhooks", cid))
        return list(self.hooks.get(str(cid), []))

    async def create_webhook(self, cid, name):
        self.calls.append(("create_webhook", cid, name))
        self.hooks[str(cid)] = [{"id": "88", "name": name, "token": "tok"}]
        return self.hooks[str(cid)][0]

    def sent(self):
        return [c for c in self.calls if c[0].startswith("create")]


def message(i, author, text="", files=(), ref=None, at=None):
    names = {OWNER: ("owner-handle", "The Owner"), BEN: ("Ben", None), COUSIN: ("cousin-handle", "Cousin")}
    a = {"id": str(author), "username": names[author][0], "global_name": names[author][1]}
    when = at or datetime(2026, 1, 5, 9, tzinfo=timezone.utc) + timedelta(minutes=i)
    return {"id": str(1000 + i), "author": a, "content": text, "timestamp": when.isoformat(), "referenced_message": ref,
            "attachments": [{"filename": n, "size": s, "url": f"https://cdn/{1000 + i}/{k}", "content_type": "image/png"}
                            for k, (n, s) in enumerate(files)]}


@pytest.fixture
def layer(tmp_path, monkeypatch):
    priv, state = tmp_path / "private", tmp_path / "state"
    for d in (priv / "secrets", priv / "desk" / "rooms", priv / "aunt" / "rooms", state):
        d.mkdir(parents=True)
    (priv / "secrets" / "discord.token").write_text("fake-token\n")
    (priv / "yerkd.toml").write_text(f'[discord]\ntoken_file = "secrets/discord.token"\nguild = {GUILD}\nowner = {OWNER}\n'
                                     'test_senders = [9]\n\n[memory]\nroot = "memory"\ntimezone = "UTC"\n')
    (priv / "rooms.toml").write_text(ROOMS)
    for f, text in (("desk/persona.md", "# Persona\n"), ("desk/rulebook.md", "# Rulebook\n"), ("desk/rooms/home.md", "# home\n"),
                    ("aunt/persona.md", "# Persona, the family copy\n\n# About the owner\n\nThe Owner is the owner of this install "
                     "and her nephew.\n\n# Voice\nWarm, short.\n"), ("aunt/rooms/aunt.md", "# aunt\n"),
                    ("aunt/rulebook.md", f"# Rulebook\nThis room reads its own folder, {priv.resolve()}/aunt, and nothing else.\n"
                     "Answer in plain words.\n"),
                    ("aunt/pinned.md", "# aunt pinned\n"), ("aunt/open-loops.md", "# aunt loops\n")):
        (priv / f).write_text(text)
    msgs = [message(i, OWNER if i % 2 else BEN, f"line {i}") for i in range(140)]
    msgs[3] = message(3, OWNER, "photos", files=[("image.png", 10), ("image.png", 11), ("big.mov", 30_000_000)])
    msgs[4] = message(4, BEN, "nice", ref=msgs[3])
    msgs[5] = message(5, COUSIN, "hi from the cousin")
    fake = FakeHTTP(msgs)

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
    return cli.main(["room", "add", *args, "--private", str(priv), "--state", str(state)])


def tree(root):
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def day_lines(priv):
    return [x for f in sorted((priv / "memory" / "daily").glob("*.md")) for x in f.read_text().splitlines() if x.startswith("- ")]


def test_a_dry_run_reads_and_prints_and_writes_and_sends_nothing(layer, capsys):
    priv, state, fake = layer
    before = tree(priv)
    assert run(priv, state, "sports", "--from", f"discord:{OLD}", "--dry-run") == 0
    out = capsys.readouterr().out
    lock = priv / "memory" / ".daily.lock"                   # the day logs' writers' lock (memory.daily_lock): the one file a dry run may make
    assert fake.sent() == [] and {p: b for p, b in tree(priv).items() if p != lock} == before
    assert f"would make #sports-on-yerk: POST /guilds/{GUILD}/channels " in out and str(HOME_OWS).replace("'", '"') in out
    block = tomllib.loads(out.split("in backups/:\n", 1)[1].split("\nhistory:", 1)[0])["rooms"]["sports-on-yerk"]
    home = rooms(priv)["home"]
    assert block == {**home, "channel": "discord:NEW", "voice": "rooms/sports.md"} and list(block) == list(home)
    assert "history: 140 messages from discord:30, 140 to file, 0 there already; 2 files up to 25 MB, 2 to save" in out
    assert "rooms/sports.md: would write" in out and "+ test_senders = [9, \"NEW\"]" in out and "setsid nohup yerk deploy" in out


def test_an_owner_room_moves_in_and_a_second_run_only_tops_up(layer, capsys, tmp_path):
    priv, state, fake = layer
    voice = tmp_path / "voice.md"
    voice.write_text("# sports: short, numbers first\n")
    old_text = (priv / "rooms.toml").read_text()
    assert run(priv, state, "sports", "--from", f"discord:{OLD}", "--voice", str(voice)) == 0
    out = capsys.readouterr().out
    made = [c for c in fake.sent() if c[0] == "create_channel"]
    assert made == [("create_channel", GUILD, 0, {"name": "sports-on-yerk", "permission_overwrites": HOME_OWS})]
    rs = rooms(priv)
    assert rs["sports-on-yerk"] == {**rs["home"], "channel": "discord:77", "voice": "rooms/sports.md"}
    backups = list((priv / "backups").glob("rooms.toml.pre-sports-*"))
    assert len(backups) == 1 and backups[0].read_text() == old_text and (priv / "rooms.toml").read_text().startswith(old_text)
    v = priv / "desk" / "rooms" / "sports.md"
    assert v.read_text() == voice.read_text() and v.stat().st_mode & 0o777 == 0o600
    lines = day_lines(priv)
    assert len(lines) == 140 and all(" sports-on-yerk#discord:77 " in x for x in lines)
    assert sum("owner@owner-handle:" in x for x in lines) == 69 and sum(" other@Ben:" in x for x in lines) == 70
    assert any("other@cousin-handle: hi from the cousin" in x for x in lines)
    assert any('[a reply to The Owner\'s message: "photos"] / nice' in x for x in lines)
    files = priv / "desk" / "sports-attachments"
    assert files.stat().st_mode & 0o777 == 0o700 and sorted(p.name for p in files.iterdir()) == ["1003-1-image.png", "1003-image.png"]
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in files.iterdir())
    text = (priv / "memory" / "daily" / "2026-01-05.md").read_text()
    assert f"image.png (saved at {files / '1003-1-image.png'})" in text and "big.mov, image/png, 30,000,000 bytes" in text
    url = priv / "secrets" / "discord-sports-webhook.url"
    assert url.read_text() == "https://discord.com/api/webhooks/88/tok\n" and url.stat().st_mode & 0o777 == 0o600
    assert "+ test_senders = [9, 88]" in out and "history: 140 messages from discord:30, 140 filed" in out
    fake.msgs += [message(200, OWNER, "a new one"), message(201, BEN, "and its answer")]
    cdn = sum(c[0] == "cdn" for c in fake.calls)
    assert run(priv, state, "sports", "--from", f"discord:{OLD}") == 0
    again = capsys.readouterr().out
    assert len([c for c in fake.sent() if c[0] == "create_channel"]) == 1 and len(fake.hooks) == 1
    assert sum(c[0] == "cdn" for c in fake.calls) == cdn and len(day_lines(priv)) == 142
    assert "#sports-on-yerk 77 is there, kept as it is" in again and "rooms.toml: sports-on-yerk is there, kept as it is" in again
    assert "142 messages from discord:30, 2 filed, 140 there already" in again and "test webhook 88 already" not in again
    assert v.read_text() == voice.read_text() and "rooms/sports.md: there, kept as it is" in again       # no --voice now: kept
    assert again.rstrip().splitlines()[-1] == ("backfill: none, sports-on-yerk has no also (it recalls every [memory] also root) "
                                               "and a backfill takes only a folder it lists")


def test_a_room_with_an_also_lists_the_files_it_brought_and_ends_on_their_two_backfills(layer, capsys):
    """attachments-root (2026-10-24): the folder the history's files go to joins the new block's `also` ("would list" in a
    dry run), yerkd.toml's [memory] also is named by hand before the deploy (yerkd does not start on a room's also outside
    it), and the last two steps are the backfills, which then take that folder."""
    priv, state, fake = layer
    (priv / "rooms.toml").write_text(ROOMS.replace('privacy = "owner"\n', 'privacy = "owner"\nalso = ["notes"]\n', 1))
    (priv / "yerkd.toml").write_text((priv / "yerkd.toml").read_text() + 'also = ["notes"]\n')
    folder = (priv / "desk" / "sports-attachments").resolve()
    steps = [f"pictures, after the deploy: yerk pictures backfill sports-on-yerk --root {folder}",
             f"files, after the deploy: yerk files backfill sports-on-yerk --root {folder}"]
    assert run(priv, state, "sports", "--from", f"discord:{OLD}", "--dry-run") == 0
    out = capsys.readouterr().out
    block = tomllib.loads(out.split("in backups/:\n", 1)[1].split("\nalso: ", 1)[0])["rooms"]["sports-on-yerk"]
    assert block["also"] == ["notes", str(folder)] and f"also: would list {folder} in it, the folder the history's files go to" in out
    assert f'yerkd.toml [memory] also, by hand before the deploy, or yerkd does not start: add "{folder}"' in out
    assert out.rstrip().splitlines()[-2:] == steps and not folder.exists()
    assert run(priv, state, "sports", "--from", f"discord:{OLD}") == 0
    assert f"also: listing {folder} in it" in (out := capsys.readouterr().out) and out.rstrip().splitlines()[-2:] == steps
    assert rooms(priv)["sports-on-yerk"]["also"] == ["notes", str(folder)] and len(list(folder.iterdir())) == 2
    assert cli.main(["files", "backfill", "sports-on-yerk", "--root", str(folder), "--dry-run", "--private", str(priv), "--state", str(state)]) == 0
    assert cli.main(["pictures", "backfill", "sports-on-yerk", "--root", str(folder), "--dry-run", "--private", str(priv), "--state", str(state)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"2 picture(s) in {folder} with no sidecar, 1 batch(es) of 20; nothing started (--dry-run)"


def test_a_person_room_gets_a_layer_of_its_own_behind_the_wall(layer, capsys):
    priv, state, fake = layer
    assert run(priv, state, "cousin", "--from", f"discord:{OLD}", "--person", str(COUSIN)) == 0
    rs, lay = rooms(priv), (priv / "cousin").resolve()
    b = rs["cousin-on-yerk"]
    assert b["privacy"] == "family" and b["people"] == [COUSIN] and b["tools"] == ["Read", "Grep", "Glob"]
    assert b["layer"] == str(lay) and not {"read", "also", "sandbox"} & set(b) and b["pinned_memory"] == "pinned.md"
    context.walls(priv, state, memory.settings(priv, state, {"root": "memory"}).scope.roots, rs)   # the daemon's guard
    assert lay.stat().st_mode & 0o777 == 0o700 and (lay / "rooms").stat().st_mode & 0o777 == 0o700
    persona, about = (lay / "persona.md").read_text(), PERSON["about"].format(layer=lay).strip()
    assert persona == f"# Persona, the family copy\n\n# Voice\nWarm, short.\n\n{about}\n" and "nephew" not in persona
    assert "\n# About the owner\n\nOWNER is the owner of this install and PERSON's RELATION. " in persona
    assert f"This room reads its own folder, {lay}, with Read, Grep and Glob;" in persona        # the folder line, in its words
    assert (lay / "rulebook.md").read_text() == "# Rulebook\nAnswer in plain words.\n"           # the aunt's folder line went
    assert all(str(priv.resolve()) not in p.read_text().replace(str(lay), "") for p in lay.rglob("*.md"))
    assert "persona.md: OWNER, PERSON and RELATION in its last section are yours to fill in" in capsys.readouterr().out
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in lay.rglob("*") if p.is_file())
    ows = [c for c in fake.sent() if c[0] == "create_channel"][0][3]["permission_overwrites"]
    assert ows == HOME_OWS + [{"id": str(COUSIN), "type": 1, "allow": "117824", "deny": "0"}]   # the aunt is not let in
    lines = day_lines(priv)
    assert any("person@cousin-handle: hi from the cousin" in x for x in lines) and all(" [c:room] " in x for x in lines)
    assert sorted(p.name for p in (lay / "cousin-attachments").iterdir()) == ["1003-1-image.png", "1003-image.png"]


def test_a_person_room_whose_copies_reach_the_private_folder_changes_nothing(layer, capsys):
    priv, state, fake = layer
    (priv / "aunt" / "persona.md").write_text(f"# Persona\nHer notes: {priv.resolve()}/memory/facts/aunt.md\n")
    before = tree(priv)
    with pytest.raises(SystemExit, match="persona.md: a path into .* outside the room's own layer"):
        run(priv, state, "cousin", "--from", f"discord:{OLD}", "--person", str(COUSIN))
    assert fake.sent() == [] and tree(priv) == before


def test_a_block_the_people_guard_refuses_changes_nothing(layer, capsys):
    priv, state, fake = layer
    before = tree(priv)
    with pytest.raises(SystemExit, match="people needs a `layer` of its own"):    # NAME aunt: the aunt's room's own layer
        run(priv, state, "aunt", "--from", f"discord:{OLD}", "--person", str(COUSIN))
    assert fake.sent() == [] and tree(priv) == before
    assert run(priv, state, "x", "--from", f"discord:{OLD}", "--person", str(COUSIN), "--like", "home") == 2
    assert run(priv, state, "x", "--from", "imessage:30") == 2 and fake.sent() == []


def test_a_channel_the_bot_cannot_read_is_said_in_one_line_and_nothing_is_made(layer, capsys):
    priv, state, fake = layer
    fake.forbidden.add(str(OLD))
    before = tree(priv)
    for dry in (["--dry-run"], []):
        with pytest.raises(SystemExit, match="Yerk's bot cannot read discord:30 or may not act here; let it in and run this again"):
            run(priv, state, "sports", "--from", f"discord:{OLD}", *dry)
    assert fake.sent() == [] and tree(priv) == before and fake.calls == [("logs", str(OLD), None)] * 2   # read first, then nothing


def test_since_keeps_the_days_from_then(layer, capsys):
    priv, state, fake = layer
    fake.msgs[:] = [message(i, OWNER, f"day {i}", at=datetime(2026, 1, 1 + i, 12, tzinfo=timezone.utc)) for i in range(5)]
    assert run(priv, state, "sports", "--from", f"discord:{OLD}", "--since", "2026-01-03", "--dry-run") == 0
    assert "history: 3 messages from discord:30, 3 to file" in capsys.readouterr().out


def test_capture_dry_says_what_it_would_write_and_writes_nothing(tmp_path):
    log = memory.DailyLog(tmp_path / "daily", timezone.utc, {"r": ("discord:1", "private")})
    at = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
    assert log.capture("r", "m1", "owner", "hello", when=at, dry=True) is True and not list((tmp_path / "daily").glob("*.md"))
    assert log.capture("r", "m1", "owner", "hello", when=at) is True
    assert log.capture("r", "m1", "owner", "hello", when=at, dry=True) is False
    assert len((tmp_path / "daily" / "2026-01-05.md").read_text().splitlines()) == 3      # the head, a blank, the line


def test_the_templates_name_no_one_and_leave_the_person_to_the_owner():
    from test_abilities import life                 # a name and a role, a #room, an @handle, a figure, an address, a date
    texts = {p.name: p.read_text() for p in (REPO / "examples" / "rooms").glob("*.toml")}
    assert sorted(texts) == ["owner.toml", "person.toml"] and [h for t in texts.values() for h in life(t)] == []
    assert all(w in PERSON["about"] for w in ("OWNER", "PERSON", "RELATION")) and "{layer}" in PERSON["about"]


def test_a_block_is_written_the_way_it_loads():
    table = {"channel": "discord:1", "tools": ["Read", "Bash"], "on": True, "n": 3, "mcp": {"web": {"args": ["a b", 'q"x']}},
             "sandbox": {"enabled": True, "filesystem": {"denyWrite": ["//x"]}}}
    assert tomllib.loads("\n".join(move.toml_lines("rooms.r", table)))["rooms"]["r"] == table
    assert toml_value([1, "two", False, 1.5]) == '[1, "two", false, 1.5]'


def test_the_import_files_with_the_rooms_id_and_class(tmp_path):
    import asyncio
    fake = FakeHTTP([message(1, OWNER, "mine"), message(2, COUSIN, "theirs"), message(3, BEN, "the old bot")])
    log = memory.DailyLog(tmp_path / "daily", timezone.utc, {"r": ("discord:9", "room")})
    got = asyncio.run(move.history(fake, log, "r", "30", {str(OWNER): "owner", str(COUSIN): "person"}, tmp_path / "files"))
    assert got == {"messages": 3, "new": 3, "files": 0, "saved": 0, "first": 1001, "last": 1003}    # the first and last ids read
    lines = (tmp_path / "daily" / "2026-01-05.md").read_text().splitlines()[2:]
    assert [x.split(" ", 4)[2:4] for x in lines] == [["r#discord:9", "owner@owner-handle:"], ["r#discord:9", "person@cousin-handle:"],
                                                    ["r#discord:9", "other@Ben:"]]
    assert all(x.endswith(f"[c:room] [m:{1000 + i}]") for i, x in enumerate(lines, 1))
    assert os.path.exists(tmp_path / "files") is False      # nothing to save, no folder
