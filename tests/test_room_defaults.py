"""Checks the [defaults] table in rooms.toml, where the allowed tools and the MCP servers are declared once and
carried by every one of your own rooms (an owner room), so no room needs a hand edit. A room's own allow or
own mcp.<name> table replaces that default whole, allow_remove drops named tools from the allow it inherits, {room} is the
room's name in a server's args and env, a room whose privacy is not owner inherits nothing, and any other key in
[defaults] stops the start with one line. `yerk doctor` names an owner room that misses a default tool or carries its own
copy of one. The move of the box's rooms.toml is proven on a copy of its shape (fixtures/room-defaults): each room's whole
loaded table, in key order, the same before and after but for the two changes decided: yerk-ops gains the
allow and both servers, and yerk-test's allow and servers take the defaults' order."""
import json
import os
import tomllib
from pathlib import Path

import pytest
from test_doctor import tree  # noqa: F401  (the doctor's healthy install, a fixture)
from test_move import OLD, layer, run  # noqa: F401  (`yerk room add` against a fake Discord)

from yerk_ai import doctor, words
from yerk_ai.config import room_defaults, rooms

HERE = Path(__file__).resolve().parent / "fixtures" / "room-defaults"
BEFORE, AFTER = (HERE / "before.toml").read_text(), (HERE / "after.toml").read_text()
BOX = (os.environ.get("YERK_ROOMS_BEFORE", ""), os.environ.get("YERK_ROOMS_AFTER", ""))
BASE = """[defaults]
allow = ["mcp__web__fetch", "mcp__hands__browser"]

[defaults.mcp.web]
type = "stdio"
args = ["server.mjs", "--room", "{room}"]
timeout = 180000

[defaults.mcp.hands]
type = "stdio"
args = ["hands.mjs"]
env = { YERK_ROOM = "{room}", KEEP = "as is" }
"""


def room(name: str, channel: int, body: str = "", privacy: str = "owner") -> str:
    return f'\n[rooms.{name}]\nchannel = "discord:{channel}"\n' + (f'privacy = "{privacy}"\n' if privacy else "") + body


def web(name: str) -> dict:
    return {"type": "stdio", "args": ["server.mjs", "--room", name], "timeout": 180000}


def hands(name: str) -> dict:
    return {"type": "stdio", "args": ["hands.mjs"], "env": {"YERK_ROOM": name, "KEEP": "as is"}}


def proof(before: str, after: str) -> None:
    """Each room's whole loaded table before and after, in key order (json.dumps without sort_keys): every room but yerk-test
    and yerk-ops byte for byte, the person's room among them; yerk-test and yerk-ops by their exact change and nothing else.
    The other rooms are read from the files, so the box's own run names none of them here."""
    was, now, base = rooms(Path(), before), rooms(Path(), after), tomllib.loads(after)["defaults"]
    kept = [n for n in was if n not in ("yerk-test", "yerk-ops")]
    owners = [n for n in kept if was[n].get("privacy", "owner") == "owner"]
    assert list(now) == list(was) and {"yerk-test", "yerk-ops"} <= set(was) and owners and len(owners) < len(kept)
    for n in kept:
        assert json.dumps(now[n]) == json.dumps(was[n]), n
    assert all(was[n]["allow"] == base["allow"] for n in owners)          # theirs, in their order
    assert all("mcp__hands__browser" not in now[n]["allow"] and now[n]["mcp"]["web-ladder"]["timeout"] == 60000 for n in kept if n not in owners)
    t = was["yerk-test"]
    assert set(t["allow"]) == set(base["allow"]) and t["allow"] != base["allow"] and list(t["mcp"]) == ["hands", "web-ladder"]
    want = {**t, "allow": base["allow"], "mcp": {"web-ladder": t["mcp"]["web-ladder"], "hands": t["mcp"]["hands"]}}
    assert json.dumps(now["yerk-test"]) == json.dumps(want)          # the same tools and servers, in the defaults' order
    ops = was["yerk-ops"]
    assert "allow" not in ops and "mcp" not in ops and ops["role"] == "machine"
    servers = json.loads(json.dumps(was[owners[0]]["mcp"]).replace(f'"{owners[0]}"', '"yerk-ops"'))
    assert json.dumps(now["yerk-ops"]) == json.dumps({**ops, "allow": base["allow"], "mcp": servers})   # the other rooms' set, its own name
    assert now["yerk-ops"]["mcp"]["web-ladder"]["args"][-2:] == ["--room", "yerk-ops"] and now["yerk-ops"]["mcp"]["hands"]["env"] == {"YERK_ROOM": "yerk-ops"}


def test_the_box_rooms_moved_under_defaults_load_the_same_but_for_the_two_changes_decided():
    assert list(rooms(Path(), BEFORE)) == ["yerk-test", "yerk-ops", "work-on-yerk", "chat-on-yerk", "family-on-yerk", "fitness-on-yerk"]
    proof(BEFORE, AFTER)


@pytest.mark.skipif(not all(BOX), reason="the box's own rooms.toml and its proposed move exist only on the box")
def test_the_box_rooms_toml_and_its_proposed_move_load_the_same_but_for_the_two_changes_decided():
    proof(Path(BOX[0]).read_text(), Path(BOX[1]).read_text())


def test_every_owner_room_carries_the_defaults_with_its_own_name_in_args_and_env():
    rs = rooms(Path(), BASE + room("a", 1) + room("b", 2, 'also = ["notes"]\n[rooms.b.sandbox]\nenabled = true\n') + room("c", 3, privacy=""))
    for n in ("a", "b", "c"):                                       # c: privacy unset is owner, as everywhere else
        assert rs[n]["allow"] == ["mcp__web__fetch", "mcp__hands__browser"] and rs[n]["mcp"] == {"web": web(n), "hands": hands(n)}
    assert list(rs["b"]) == ["channel", "privacy", "allow", "also", "mcp", "sandbox"]       # where rooms.toml.example writes them
    rs["a"]["allow"].append("Bash")
    rs["a"]["mcp"]["web"]["args"].append("--more")
    assert rs["b"]["allow"] == ["mcp__web__fetch", "mcp__hands__browser"] and rs["b"]["mcp"]["web"] == web("b")   # each room its own copy
    assert room_defaults(tomllib.loads(BASE))["mcp"]["web"]["args"] == ["server.mjs", "--room", "{room}"]


def test_a_rooms_own_allow_or_own_server_replaces_that_default_whole_and_its_other_servers_stay():
    own = ('allow = ["Bash"]\n\n[rooms.a.mcp.web]\ntype = "http"\nurl = "https://example.org/mcp"\n\n'
           '[rooms.a.mcp.notes]\ntype = "stdio"\nargs = ["notes.mjs"]\n')
    a = rooms(Path(), BASE + room("a", 1, own))["a"]
    assert a["allow"] == ["Bash"]                                   # no merging: the default's tools are not added
    assert a["mcp"] == {"web": {"type": "http", "url": "https://example.org/mcp"}, "hands": hands("a"), "notes": {"type": "stdio", "args": ["notes.mjs"]}}
    assert list(a["mcp"]) == ["web", "hands", "notes"]             # its own server in the default's place, its others after


def test_allow_remove_drops_named_tools_from_the_allow_a_room_inherits():
    a = rooms(Path(), BASE + room("a", 1, 'allow_remove = ["mcp__hands__browser"]\n'))["a"]
    assert a["allow"] == ["mcp__web__fetch"] and a["mcp"] == {"web": web("a"), "hands": hands("a")}
    assert doctor.drift("a", rooms(Path(), BASE + room("a", 1, 'allow_remove = ["mcp__hands__browser"]\n'), own=True)["a"], room_defaults(tomllib.loads(BASE))) == ""
    mine = rooms(Path(), BASE + room("a", 1, 'allow = ["mcp__web__fetch"]\nallow_remove = ["mcp__hands__browser"]\n'), own=True)["a"]
    assert doctor.drift("a", mine, room_defaults(tomllib.loads(BASE))) == f"{words.WORKING} room a: carries a copy of the default allow"   # a tool it drops is not missed
    with pytest.raises(SystemExit, match=r"^rooms.toml: a: allow_remove must be a list of tool names, like"):
        rooms(Path(), BASE + room("a", 1, 'allow_remove = "mcp__hands__browser"\n'))


def test_a_room_whose_privacy_is_not_owner_inherits_nothing_and_keeps_its_own_tables_as_written():
    mine = 'allow = ["mcp__web__fetch"]\n\n[rooms.shared.mcp.web]\ntype = "stdio"\nargs = ["server.mjs", "--room", "{room}"]\ntimeout = 60000\n'
    text = BASE + room("fam", 1, "people = [31]\n", "family") + room("shared", 2, mine, "shared")
    rs, own = rooms(Path(), text), rooms(Path(), text, own=True)
    assert "allow" not in rs["fam"] and "mcp" not in rs["fam"]
    assert rs["shared"]["allow"] == ["mcp__web__fetch"] and rs["shared"]["mcp"] == {"web": {**web("{room}"), "timeout": 60000}}
    assert all(json.dumps(rs[n]) == json.dumps(own[n]) for n in ("fam", "shared"))


@pytest.mark.parametrize("bad, said", [
    ("[defaults]\nsandbox = { enabled = true }\n", "rooms.toml: [defaults] sandbox: a default carries allow and mcp tables only; set sandbox in each room"),
    ('[defaults]\nmodel = "claude-opus-5-5"\n', "rooms.toml: [defaults] model: a default carries allow and mcp tables only; set model in each room"),
    ('[defaults]\nallow = "mcp__web__fetch"\n', "rooms.toml: [defaults] allow must be a list of tool names, like"),
    ('[defaults.mcp]\nweb = "server.mjs"\n', "rooms.toml: [defaults] mcp must be a table of servers, like [defaults.mcp.web], each one a table"),
    ("defaults = 3\n", "rooms.toml: defaults must be a table, [defaults], holding allow and mcp tables"),
])
def test_defaults_refuses_any_key_but_allow_and_mcp_with_one_line_naming_it(bad, said):
    with pytest.raises(SystemExit) as e:
        rooms(Path(), bad + room("a", 1))
    assert str(e.value).startswith(said) and "\n" not in str(e.value)


def test_doctor_reads_todays_rooms_under_the_defaults_as_four_copies_and_the_moved_file_as_clean():
    base = room_defaults(tomllib.loads(AFTER))

    def said(text: str) -> list[str]:
        return [x for n, c in rooms(Path(), text, own=True).items() if c.get("privacy", "owner") == "owner" and (x := doctor.drift(n, c, base))]
    assert said(BEFORE) == [f"{words.WORKING} closes {n}: item moved to the next step and, the.test-reads it back.as"
                            for n in ("yerk-test", "work-on-yerk", "chat-on-yerk", "fitness-on-yerk")]      # yerk-test's allow: equal as a set
    assert said(AFTER) == []


def test_doctor_names_an_owner_room_that_misses_a_default_tool_or_carries_a_copy_one_line_each(tree, capsys):  # noqa: F811
    priv, state, *_ = tree
    path = priv / "rooms.toml"
    text = BASE + path.read_text()
    path.write_text(text.replace("recall_days = 2", 'recall_days = 2\nallow = ["mcp__web__fetch", "Bash"]', 1)
                    + '\nallow = ["mcp__hands__browser", "mcp__web__fetch"]\n\n[rooms.example-quiet.mcp.web]\ntype = "stdio"\n'
                    'args = ["server.mjs", "--room", "example-quiet"]\ntimeout = 180000\n')
    assert doctor.main(priv, state) == 1
    assert capsys.readouterr().out.splitlines() == [f"{words.NOT_DONE} room example-room: misses default tool mcp__hands__browser",
                                                    f"{words.WORKING} room example-quiet: carries a copy of the default allow and mcp.web"]
    path.write_text(text)                                           # the copies out: both rooms inherit, and all is well
    assert doctor.main(priv, state) == 0 and capsys.readouterr().out == ""


def test_a_room_made_like_another_copies_its_own_keys_and_inherits_the_defaults_under_its_own_name(layer, capsys):  # noqa: F811
    priv, state, fake = layer
    text, home = (priv / "rooms.toml").read_text(), 'allow = ["mcp__web__fetch"]\n\n[rooms.home.mcp.web]\ntype = "stdio"\nargs = ["server.mjs"]\ntimeout = 180000\n\n'
    assert home in text
    (priv / "rooms.toml").write_text('[defaults]\nallow = ["mcp__web__fetch"]\n\n[defaults.mcp.web]\ntype = "stdio"\n'
                                     'args = ["server.mjs", "--room", "{room}"]\ntimeout = 180000\n\n' + text.replace(home, ""))
    assert run(priv, state, "sports", "--from", f"discord:{OLD}") == 0
    own, rs = rooms(priv, own=True)["sports-on-yerk"], rooms(priv)["sports-on-yerk"]
    assert "allow" not in own and "mcp" not in own and own["sandbox"] == {"filesystem": {"denyWrite": ["//elsewhere"]}}
    assert rs["allow"] == ["mcp__web__fetch"] and rs["mcp"]["web"]["args"] == ["server.mjs", "--room", "sports-on-yerk"]
