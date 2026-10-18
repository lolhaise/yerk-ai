# The server scaffold (handlers whose docstrings are the tool descriptions, registered on the
# official Python SDK) and the send tool's shape are adapted from Hermes Agent v0.22.2,
# mcp_serve.py (create_mcp_server, messages_send), https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
"""Yerk AI's own MCP server: two tools, local-only HTTP, preloaded in every room.

Each room's process gets a config naming this one server with `alwaysLoad: true` and a header
carrying its room id, passed with --strict-mcp-config. The same HTTP app answers the hooks.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

ROOM_HEADER = "x-yerk-room"


class Daemon(Protocol):
    async def tool_send(self, room: str, text: str, file: str, caller: str) -> dict[str, Any]: ...
    async def tool_hold(self, room: str, wait_for: str, reason: str) -> dict[str, Any]: ...


def caller_room(ctx: Context) -> str:
    req = ctx.request_context.request
    return str(req.headers.get(ROOM_HEADER, "")) if req is not None else ""


def build(daemon: Daemon, port: int, hook: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> FastMCP:
    mcp = FastMCP("yerk", host="127.0.0.1", port=port, stateless_http=True, json_response=True, log_level="WARNING")

    @mcp.tool()
    async def send(room: str, text: str = "", file: str = "", *, ctx: Context) -> str:
        """Post to a room now, before your reply: text, or a file (absolute path) with the text as its
        caption. `room` is a room id from the [message] line. Returns the posted message ids."""
        return json.dumps(await daemon.tool_send(room, text, file, caller_room(ctx)))

    @mcp.tool()
    async def hold(wait_for: str, reason: str, *, ctx: Context) -> str:
        """End your turn while you wait on a named event or worker. After this call write one short
        line saying what you are waiting for, and make no more tool calls; the room hears when it happens."""
        return json.dumps(await daemon.tool_hold(caller_room(ctx), wait_for, reason))

    @mcp.custom_route("/hook", methods=["POST"])
    async def hook_route(request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({}, status_code=400)
        return JSONResponse(await hook(payload))

    return mcp


def room_config(url: str, room: str, path: Path) -> Path:
    """The per-room --mcp-config file: this server only, preloaded."""
    cfg = {"mcpServers": {"yerk": {"type": "http", "url": url, "headers": {ROOM_HEADER: room}, "alwaysLoad": True}}}
    path.write_text(json.dumps(cfg, indent=1))
    return path
