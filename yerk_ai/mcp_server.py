# The server scaffold (handlers whose docstrings are the tool descriptions, registered on the
# official Python SDK) and the send tool's shape are adapted from Hermes Agent v0.22.2,
# mcp_serve.py (create_mcp_server, messages_send), https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
"""Yerk AI's own MCP server: four tools (send, hold, recall, read_memory), local-only HTTP,
preloaded in every room.

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
    async def tool_recall(self, room: str, query: str, k: int) -> dict[str, Any]: ...
    async def tool_read_memory(self, room: str, path: str, start: int, lines: int) -> dict[str, Any]: ...


def caller_room(ctx: Context) -> str:
    req = ctx.request_context.request
    return str(req.headers.get(ROOM_HEADER, "")) if req is not None else ""


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def build(daemon: Daemon, port: int, hook: Handler, control: Handler | None = None) -> FastMCP:
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

    @mcp.tool()
    async def recall(query: str, k: int = 6, *, ctx: Context) -> str:
        """Search the owner's memory (daily logs, fact files, the curated file, open loops) by keywords
        and, where the install has a provider key, by meaning too, so a paraphrase finds the line.
        Returns ranked chunks, each with its file path, first line and `cite` (file:line). Cite every
        fact you use by its `cite`; read_memory gives the exact lines around a hit. A "timeout" status
        means busy, not down."""
        return json.dumps(await daemon.tool_recall(caller_room(ctx), query, k))

    @mcp.tool()
    async def read_memory(path: str, from_line: int = 1, lines: int = 40, *, ctx: Context) -> str:
        """An exact excerpt of a memory file: `lines` lines from `from_line` (1-based). `path` as recall
        returned it, or relative to the memory folder. Files outside the memory tree are refused."""
        return json.dumps(await daemon.tool_read_memory(caller_room(ctx), path, from_line, lines))

    def route(path: str, handler: Handler) -> None:
        @mcp.custom_route(path, methods=["POST"])
        async def _route(request: Request) -> Response:
            try:
                payload = await request.json()
            except ValueError:
                return JSONResponse({}, status_code=400)
            return JSONResponse(await handler(payload))

    route("/hook", hook)
    if control:
        route("/control", control)      # `yerk room close`: local only, like everything here
    return mcp


def room_config(url: str, room: str, path: Path) -> Path:
    """The per-room --mcp-config file: this server only, preloaded."""
    cfg = {"mcpServers": {"yerk": {"type": "http", "url": url, "headers": {ROOM_HEADER: room}, "alwaysLoad": True}}}
    path.write_text(json.dumps(cfg, indent=1))
    return path
