"""Checks the hook endpoint, where yerkd answers the calls Claude Code's hooks make, and the MCP server that hands a room Yerk AI's own tools."""
import asyncio
import json
from datetime import datetime, timezone

from starlette.testclient import TestClient

from yerk_ai import context, hooks, mcp_server
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms
from yerk_ai.daemon import ConsoleChannel, Yerkd
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


def test_six_preloaded_tools_with_slim_schemas(tmp_path):
    y = daemon(tmp_path)
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert set(tools) == {"send", "hold", "recall", "read_memory", "worker", "schedule"}
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
        assert rd.status_code == 200 and "hits" in rd.json() or rd.json().get("status") in ("failure", "timeout")
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
    hook = spec.settings["hooks"]["Stop"][0]["hooks"][0]
    assert hook["type"] == "http" and hook["headers"] == {"x-yerk-secret": spec.token}
    assert f"-H 'x-yerk-secret: {spec.token}'" in spec.settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    cfg = json.loads(spec.mcp_config.read_text())
    assert cfg["mcpServers"]["yerk"]["headers"] == {"x-yerk-secret": spec.token} and "x-yerk-room" not in str(cfg)
    assert oct(spec.mcp_config.stat().st_mode & 0o777) == "0o600"
    assert y.spec_for("example-room").token != spec.token           # a token per process start
