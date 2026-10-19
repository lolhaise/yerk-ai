"""Checks the hook endpoint, where yerkd answers the calls Claude Code's hooks make, and the MCP server that hands a room Yerk AI's own tools."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from starlette.testclient import TestClient

from yerk_ai import context, hooks, mcp_server
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms
from yerk_ai.daemon import YERK_TOOLS, ConsoleChannel, Yerkd
from yerk_ai.engine import SessionTable


def test_user_prompt_submit_adds_the_clock_and_metadata_for_the_bound_room(tmp_path):
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-1", "one")
    rs, b = rooms(EXAMPLE_PRIVATE), budgets()
    now = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)
    meta = lambda room, prompt: ("owner", now.timestamp() - 3900)  # noqa: E731
    out = hooks.user_prompt_submit({"session_id": "sid-1", "prompt": "hi"}, tmp_path, rs, meta, b, now=now)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert ctx.splitlines() == ["[clock] Wednesday 2026-01-07 12:00 UTC, 1h 5m since the previous human message",
                                "[message] room example-room, from owner, privacy owner"]
    assert len(ctx) < b["hook"]["output_cap"]
    assert hooks.user_prompt_submit({"session_id": "other"}, tmp_path, rs, meta, b) == hooks.UNBOUND   # never a silent {}


def daemon(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    return y


def test_the_hook_endpoint_answers_over_http(tmp_path):
    y = daemon(tmp_path)
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-2", "one")
    app = mcp_server.build(y, y.port, y.hook).streamable_http_app()
    with TestClient(app) as c:
        r = c.post("/hook", json={"hook_event_name": "UserPromptSubmit", "session_id": "sid-2", "prompt": "x"})
        other = c.post("/hook", json={"hook_event_name": "Stop", "session_id": "sid-2"})
    assert r.status_code == 200 and r.json()["hookSpecificOutput"]["additionalContext"].startswith("[clock] ")
    assert other.json() == {}


def test_eight_preloaded_tools_with_slim_schemas(tmp_path):
    y = daemon(tmp_path)
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert set(tools) == {"send", "hold", "recall", "read_memory", "remember", "worker", "schedule", "settings"}
    assert list(tools["remember"].inputSchema["properties"]) == ["text", "about", "replaces"]
    assert {f"mcp__yerk__{t}" for t in tools} == set(YERK_TOOLS) and "`remember`" in context.MECHANICS.read_text()
    assert list(tools["settings"].inputSchema["properties"]) == ["model", "effort"]
    assert "the owner asks to change this room's model or thinking level" in str(tools["settings"].description).lower()
    assert list(tools["schedule"].inputSchema["properties"]) == ["action", "name", "when", "what", "kind", "tz",
                                                                "deliver", "room", "cap_minutes", "account"]
    assert list(tools["send"].inputSchema["properties"]) == ["room", "text", "file"]
    assert list(tools["hold"].inputSchema["properties"]) == ["wait_for", "reason"]
    assert list(tools["recall"].inputSchema["properties"]) == ["query", "k"]
    assert list(tools["read_memory"].inputSchema["properties"]) == ["path", "from_line", "lines"]
    assert list(tools["worker"].inputSchema["properties"]) == ["action", "brief", "id", "room", "tasks", "cap_minutes",
                                                              "account", "model"]
    cfg = json.loads(mcp_server.room_config("http://127.0.0.1:1/mcp", "example-room", tmp_path / "m.json").read_text())
    assert cfg["mcpServers"]["yerk"]["alwaysLoad"] is True and list(cfg["mcpServers"]) == ["yerk"]
    web = {"type": "stdio", "command": "/usr/bin/node", "args": ["server.mjs"], "timeout": 180000}   # a room's `mcp` table
    cfg = json.loads(mcp_server.room_config("http://127.0.0.1:1/mcp", "example-room", tmp_path / "m.json", "t",
                                            {"web-ladder": web, "yerk": {"type": "stdio", "command": "x"}}).read_text())
    assert cfg["mcpServers"]["web-ladder"] == web and cfg["mcpServers"]["yerk"]["type"] == "http"   # yerk is never replaced


def test_send_and_hold(tmp_path, capsys):
    y = daemon(tmp_path)
    ok = asyncio.run(y.tool_send("example-room", "PING", "", "example-room"))
    assert ok["ok"] and ok["message_ids"] and '"send": "PING"' in capsys.readouterr().out
    assert not asyncio.run(y.tool_send("not-a-room", "x", "", ""))["ok"]
    assert not asyncio.run(y.tool_send("example-room", "", str(tmp_path / "missing.png"), ""))["ok"]
    inside = context.layer_dir(y.private, y.room_confs["example-room"]) / "memory" / "pinned.md"
    assert asyncio.run(y.tool_send("example-room", "pinned", str(inside), "example-room"))["ok"]
    outside = tmp_path / "token.txt"
    outside.write_text("not for the room")
    capsys.readouterr()
    refused = asyncio.run(y.tool_send("example-room", "", str(outside), "example-room"))
    assert not refused["ok"] and "outside this room's folders" in refused["error"]
    said = capsys.readouterr().out                                                  # said in the room
    assert "not sent: token.txt" in said and "it is outside this room's folders" in said
    held =asyncio.run(y.tool_hold("example-room", "worker-1", "waiting on the build"))
    assert held["ok"] and y.ledger.rows(1, "holds")[0]["wait_for"] == "worker-1"


class KeyedChannel(ConsoleChannel):
    def __init__(self):
        super().__init__()
        self.keys = []

    async def send_text(self, room, text, key=None):
        self.keys.append((key, ""))
        return await super().send_text(room, text, key)

    async def send_file(self, room, path, caption="", key=None, inbound=""):
        self.keys.append((key, inbound))
        return await super().send_file(room, path, caption, key, inbound)


def test_a_send_outside_a_turn_has_a_key_of_its_own_across_restarts(tmp_path):
    keys = []
    for _ in range(2):                                                # two daemon starts, one send each
        y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
        ch = KeyedChannel()
        y.add_room("example-room", ch)
        assert asyncio.run(y.tool_send("example-room", "PING", "", "example-room"))["ok"]
        keys += ch.keys
    assert len({k for k, _ in keys}) == 2 and all(k.endswith("-s1") for k, _ in keys)   # never "send-s1" twice
    room = y.rooms["example-room"]
    room.busy, room.turn_key = True, "abc123def456"                   # inside a turn: its key and its messages
    room.inflight = [type("M", (), {"id": "501"})()]
    inside = context.layer_dir(y.private, y.room_confs["example-room"]) / "memory" / "pinned.md"
    assert asyncio.run(y.tool_send("example-room", "pinned", str(inside), "example-room"))["ok"]
    assert ch.keys[-1] == ("abc123def456-s2", "501")


# the secret header (restart test R3): every endpoint, three keys, identity from the binding
def guarded(y):
    return mcp_server.app(mcp_server.build(y, y.port, y.hook, y.control), y.check())


def test_every_endpoint_refuses_a_caller_without_the_secret_and_knows_a_room_by_its_token(tmp_path):
    y = daemon(tmp_path)
    y.sessions.bind("example-room", "sid-t", "one", token="room-token-1")
    with TestClient(guarded(y)) as c:
        for path in ("/hook", "/control", "/mcp"):
            r = c.post(path, json={"hook_event_name": "Stop", "session_id": "sid-t"})
            assert r.status_code == 401 and "x-yerk-secret" in r.json()["error"], path
        bad = c.post("/hook", json={"hook_event_name": "Stop", "session_id": "sid-t"}, headers={"x-yerk-secret": "nope"})
        assert bad.status_code == 401
        # the install's secret opens everything; a session nobody bound is answered unbound, never {}
        ok = c.post("/hook", json={"hook_event_name": "UserPromptSubmit", "session_id": "sid-t", "prompt": "x"},
                    headers={"x-yerk-secret": y.secret})
        assert ok.status_code == 200 and ok.json()["hookSpecificOutput"]["additionalContext"].startswith("[clock] ")
        unbound = c.post("/hook", json={"hook_event_name": "UserPromptSubmit", "session_id": "nobody", "prompt": "x"},
                         headers={"x-yerk-secret": y.secret})
        assert unbound.status_code == 200 and unbound.json() == {"yerk": "unbound"}
        # a room's own token: the room comes from the binding, whatever the payload says
        by_token = c.post("/hook", json={"hook_event_name": "UserPromptSubmit", "session_id": "someone-else", "prompt": "x"},
                          headers={"x-yerk-secret": "room-token-1"})
        assert by_token.status_code == 200 and "room example-room" in by_token.json()["hookSpecificOutput"]["additionalContext"]
        assert c.post("/control", json={"close": "example-room"}, headers={"x-yerk-secret": "room-token-1"}).status_code == 401
        # the worker secret: reads only, on /control only
        rd = c.post("/control", json={"recall": {"room": "example-room", "query": "kettle", "k": 2}},
                    headers={"x-yerk-secret": y.worker_secret})
        assert rd.status_code == 200 and ("hits" in rd.json() or rd.json().get("status") in ("failure", "timeout"))
        start = c.post("/control", json={"worker": {"action": "list", "room": "example-room"}},
                       headers={"x-yerk-secret": y.worker_secret})
        assert start.status_code == 200 and not start.json()["ok"] and "reads memory only" in start.json()["error"]
        job = c.post("/control", json={"job": {"action": "list", "room": "example-room"}}, headers={"x-yerk-secret": y.worker_secret})
        assert not job.json()["ok"]
        assert c.post("/hook", json={"hook_event_name": "Stop"}, headers={"x-yerk-secret": y.worker_secret}).status_code == 401
    assert oct(y.secret_file.stat().st_mode & 0o777) == "0o600" and y.secret != y.worker_secret and len(y.secret) >= 32
    assert y.secret_file.is_relative_to(tmp_path)              # the example layer in the repo is never written


def test_a_rooms_settings_and_mcp_config_carry_its_token_and_are_private(tmp_path):
    y = daemon(tmp_path)
    spec = y.spec_for("example-room")
    assert spec.token and len(spec.token) == 32
    for event, entries in spec.settings["hooks"].items():        # every event a command hook: a sandboxed
        hook = entries[0]["hooks"][0]                              # room's proxy refuses http hooks with 403
        assert hook["type"] == "command" and "http" not in hook, event
        assert f"-H 'x-yerk-secret: {spec.token}'" in hook["command"] and hook["command"].endswith("/hook"), event
    cfg = json.loads(spec.mcp_config.read_text())
    assert cfg["mcpServers"]["yerk"]["headers"] == {"x-yerk-secret": spec.token} and "x-yerk-room" not in str(cfg)
    assert oct(spec.mcp_config.stat().st_mode & 0o777) == "0o600"
    assert y.spec_for("example-room").token != spec.token           # a token per process start


def test_the_secrets_sit_outside_every_rooms_layer(tmp_path):
    """Cold read F4: a room with no `layer` key has the whole private folder in its Read rule and its
    --add-dir, so the secrets are minted under the state folder for every layer, never inside one."""
    import shutil
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)                        # a real private layer: not the one in the repo
    (tmp_path / "state").mkdir()
    y = Yerkd(private, tmp_path / "state")
    assert "layer" not in y.room_confs["example-room"]
    spec = y.spec_for("example-room")
    layer = context.layer_dir(private, y.room_confs["example-room"])
    rules = [r for r in spec.settings["permissions"]["allow"] if r.startswith("Read(") and r.endswith("/**)")]
    folders = [layer, *spec.add_dirs] + [Path("/" + r[len("Read("):-len("/**)")].lstrip("/")) for r in rules]
    assert layer == private.resolve() and rules and all(f.resolve() == layer for f in folders)
    for secret in (y.secret_file, y.worker_secret_file):
        assert secret.is_relative_to(tmp_path / "state") and oct(secret.stat().st_mode & 0o777) == "0o600"
        assert not any(secret.resolve().is_relative_to(f.resolve()) for f in folders), secret.name
    assert not (private / "secrets").exists()


def test_a_rooms_read_folders_are_added_and_never_reach_yerks_own(tmp_path):
    """`read` in rooms.toml: each folder is one more --add-dir for that room. A folder that is, holds or sits
    inside the private or the state folder stops the daemon at start, so no room can read the secrets (F4)."""
    import shutil
    import pytest
    private, state, notes = tmp_path / "private", tmp_path / "state", tmp_path / "notes"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    notes.mkdir()
    base = (private / "rooms.toml").read_text()

    def read(p: Path) -> None:
        (private / "rooms.toml").write_text(base.replace("[rooms.example-room]\n", f'[rooms.example-room]\nread = ["{p}"]\n', 1))
    read(notes)
    assert Yerkd(private, state).spec_for("example-room").add_dirs[1:] == [notes]
    assert Yerkd(private, state).spec_for("example-quiet").add_dirs[1:] == []
    for bad in (private, private / "rooms", state, state / "secrets", tmp_path):
        read(bad)
        with pytest.raises(SystemExit, match="reaches Yerk's own folders"):
            Yerkd(private, state)


def test_a_relative_read_folder_is_judged_where_claude_code_lands_it_and_handed_over_resolved(tmp_path, monkeypatch):
    """Cold read 33 R1: Claude Code lands a relative --add-dir from the room's work folder, so the start-up check resolves
    `read` from there too, whatever folder the daemon started in, and the room is handed the very folder judged:
    `../../../secrets` is the state folder's secrets and stops the daemon; a symlink goes over as the folder it was."""
    import shutil
    import pytest
    private, state, notes = tmp_path / "private", tmp_path / "state", tmp_path / "notes"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    notes.mkdir()
    (tmp_path / "link").symlink_to(notes)
    (cwd := tmp_path / "a" / "b" / "c").mkdir(parents=True)
    monkeypatch.chdir(cwd)                          # three deep, like the live unit's, so ../../.. from here is tmp_path
    base = (private / "rooms.toml").read_text()

    def read(*ps: str) -> None:
        (private / "rooms.toml").write_text(base.replace("[rooms.example-room]\n", f"[rooms.example-room]\nread = {json.dumps(ps)}\n", 1))
    for bad, lands in (("../../../secrets", state / "secrets"), ("../..", state / "rooms"),
                       ("../../../../private/memory", private / "memory")):
        read(bad)
        with pytest.raises(SystemExit) as e:
            Yerkd(private, state)
        assert f"read {lands} reaches Yerk's own folders" in str(e.value)
    read("../../../../notes", str(tmp_path / "link"))
    y = Yerkd(private, state)
    assert y.spec_for("example-room").add_dirs[1:] == y.read_dirs["example-room"] == [notes, notes]


def test_a_read_written_as_one_string_stops_the_daemon_with_one_plain_line(tmp_path):
    """Cold read 33: `read = "<folder>"`, a string where a list belongs, was read one character at a time (a relative one
    became an --add-dir per letter, an absolute one a refusal of `/`); now the daemon stops at start with one line naming
    the room and the shape it wants, and so does any other value that is not a list."""
    import shutil
    import pytest
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    base = (private / "rooms.toml").read_text()
    for toml, shown in (('"notes"', "'notes'"), (f'"{tmp_path}/notes"', repr(f"{tmp_path}/notes")), ("5", "5")):
        (private / "rooms.toml").write_text(base.replace("[rooms.example-room]\n", f"[rooms.example-room]\nread = {toml}\n", 1))
        with pytest.raises(SystemExit) as e:
            Yerkd(private, state)
        assert str(e.value) == f'room example-room: read must be a list of folders, like read = ["~/notes"], not {shown}'


def test_a_rooms_mcp_that_is_not_server_tables_or_names_like_yerk_stops_the_daemon_with_one_plain_line(tmp_path):
    """Cold read of pull request 37, B2 and B3: `mcp = "web"` raised a ValueError at the room's first spawn and an entry
    that is not a table went through to Claude Code as it was; a server named yerk-web or a__b makes tools that the map,
    the evidence rules and the card read as Yerk's own (mcp__yerk-web__*, a last segment of send). Each now stops the
    start with one line that names the room and never shows a value (an entry's env may hold a key)."""
    import shutil
    import pytest
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    base = (private / "rooms.toml").read_text()
    shape = "room example-room: mcp must be a table of servers, like [rooms.example-room.mcp.web], each one a table"
    for toml, said in (('mcp = "web"\n', shape), ('mcp = ["web"]\n', shape), ('mcp = { web = "x", ok = {} }\n', shape),
                       ('[rooms.example-room.mcp.yerk-web]\ncommand = "x"\n', "mcp server yerk-web could pass"),
                       ('[rooms.example-room.mcp.YERK]\ncommand = "x"\n', "mcp server YERK could pass"),
                       ('[rooms.example-room.mcp.a__b]\ncommand = "x"\n', "mcp server a__b could pass")):
        (private / "rooms.toml").write_text(base.replace("[rooms.example-room]\n", f"[rooms.example-room]\n{toml}", 1)
                                            if toml.startswith("mcp") else base + "\n" + toml)
        with pytest.raises(SystemExit) as e:
            Yerkd(private, state)
        assert said in str(e.value) and "\n" not in str(e.value) and '"x"' not in str(e.value)
    (private / "rooms.toml").write_text(base + '\n[rooms.example-room.mcp.web-ladder]\ncommand = "/usr/bin/node"\n')
    assert Yerkd(private, state).room_confs["example-room"]["mcp"] == {"web-ladder": {"command": "/usr/bin/node"}}


def test_a_rooms_sandbox_table_reaches_its_claude_code_settings_and_no_other_room(tmp_path):
    """`[rooms.NAME.sandbox]` is passed as Claude Code's own `sandbox` setting for that room's process, so a room
    can run a shell that writes only where the table lets it; a room without the table gets no sandbox key."""
    import shutil
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    rooms_toml = private / "rooms.toml"
    rooms_toml.write_text(rooms_toml.read_text() + '\n[rooms.example-room.sandbox]\nenabled = true\n'
                          '[rooms.example-room.sandbox.filesystem]\ndenyWrite = ["/home/someone/notes"]\n')
    y = Yerkd(private, state)
    assert y.spec_for("example-room").settings["sandbox"] == {"enabled": True, "filesystem": {"denyWrite": ["/home/someone/notes"]}}
    assert "sandbox" not in y.spec_for("example-quiet").settings


def test_the_daemon_reads_a_rooms_lines_in_that_rooms_view(tmp_path):
    """Cold read F2, the daemon's side: the topic hits and the recall pack of a room that is not an
    owner room carry the room's own view, so a former room's old lines (no id) never reach it."""
    y = daemon(tmp_path)
    y.room_confs["family-room"] = {**y.room_confs["example-room"], "privacy": "family", "id": "f-new"}
    y.memory.daily.mkdir(parents=True)
    day = datetime.now(timezone.utc).astimezone(y.memory.tz).strftime("%Y-%m-%d")
    (y.memory.daily / f"{day}.md").write_text(
        f"# {day}\n\n- 09:00:00 family-room owner: the former room decided on a teal kettle [m:1]\n"
        f"- 09:01:00+00:00 family-room#f-new owner: this room decided on a red kettle [c:room] [m:2]\n")
    assert [t["text"] for t in y.recall_for("family-room", "what did we decide on the kettle?")[1]] == [
        "this room decided on a red kettle"]
    pack = y.room_pack("family-room")[0]
    assert "red kettle" in pack and "teal" not in pack
    y.room_confs["family-room"]["privacy"] = "owner"                   # the owner view: by name too
    assert len(y.recall_for("family-room", "what did we decide on the kettle?")[1]) == 2


def test_a_room_cannot_point_its_binding_at_another_transcript(tmp_path, caplog):
    """Cold read F5: a hook call under the room's own token sets the binding's transcript only to an
    absolute `<session id>.jsonl`; any other path is logged and ignored."""
    import logging
    y = daemon(tmp_path)
    y.sessions.bind("example-room", "sid-t", "one", token="room-token-1")
    own = tmp_path / "projects" / "-work" / "sid-t.jsonl"
    own.parent.mkdir(parents=True)
    own.write_text("{}\n")
    planted = tmp_path / "not-my-transcript.jsonl"
    planted.write_text("{}\n")
    with TestClient(guarded(y)) as c, caplog.at_level(logging.WARNING, logger="yerk.daemon"):
        for path in (str(planted), "projects/-work/sid-t.jsonl", str(tmp_path / "sid-t.json"), str(tmp_path / "x" / "sid-u.jsonl")):
            r = c.post("/hook", json={"hook_event_name": "Stop", "session_id": "sid-t", "transcript_path": path},
                       headers={"x-yerk-secret": "room-token-1"})
            assert r.status_code == 200 and y.sessions.get("example-room").get("transcript", "") == "", path
        assert sum("not its session's own file" in rec.getMessage() for rec in caplog.records) == 4
        c.post("/hook", json={"hook_event_name": "Stop", "session_id": "other-sid", "transcript_path": str(own)},
               headers={"x-yerk-secret": "room-token-1"})
        assert y.sessions.get("example-room").get("transcript", "") == ""   # another session's id: ignored, as before
        c.post("/hook", json={"hook_event_name": "Stop", "session_id": "sid-t", "transcript_path": str(own)},
               headers={"x-yerk-secret": "room-token-1"})
    assert y.sessions.get("example-room")["transcript"] == str(own)
