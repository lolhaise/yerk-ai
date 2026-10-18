# The server scaffold (handlers whose docstrings are the tool descriptions, registered on the
# official Python SDK) and the send tool's shape are adapted from Hermes Agent v0.22.2,
# mcp_serve.py (create_mcp_server, messages_send), https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
"""Yerk AI's own MCP server: six tools (send, hold, recall, read_memory, worker, schedule), local-only
HTTP, preloaded in every room. A worker gets none of it: by default no MCP server at all, and with
[workers] yerk_tools its own stdio server (`worker_server`) carrying recall and read_memory only.

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
RECALL = ("Search the owner's memory (daily logs, fact files, the curated file, open loops) by keywords\n"
          "and, where the install has a provider key, by meaning too, so a paraphrase finds the line.\n"
          "Returns ranked chunks, each with its file path, first line and `cite` (file:line). Cite every\n"
          "fact you use by its `cite`; read_memory gives the exact lines around a hit. A \"timeout\" status\n"
          "means busy, not down.")
READ_MEMORY = ("An exact excerpt of a memory file: `lines` lines from `from_line` (1-based). `path` as recall\n"
               "returned it, or relative to the memory folder. Files outside the memory tree are refused.")


class Daemon(Protocol):
    async def tool_send(self, room: str, text: str, file: str, caller: str) -> dict[str, Any]: ...
    async def tool_hold(self, room: str, wait_for: str, reason: str) -> dict[str, Any]: ...
    async def tool_recall(self, room: str, query: str, k: int) -> dict[str, Any]: ...
    async def tool_read_memory(self, room: str, path: str, start: int, lines: int) -> dict[str, Any]: ...
    async def tool_worker(self, room: str, req: dict[str, Any]) -> dict[str, Any]: ...
    async def tool_schedule(self, room: str, req: dict[str, Any]) -> dict[str, Any]: ...


def caller_room(ctx: Context) -> str:
    req = ctx.request_context.request
    return str(req.headers.get(ROOM_HEADER, "")) if req is not None else ""


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def build(daemon: Daemon, port: int, hook: Handler, control: Handler | None = None,
          status: Handler | None = None) -> FastMCP:
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

    @mcp.tool(description=RECALL)
    async def recall(query: str, k: int = 6, *, ctx: Context) -> str:
        return json.dumps(await daemon.tool_recall(caller_room(ctx), query, k))

    @mcp.tool(description=READ_MEMORY)
    async def read_memory(path: str, from_line: int = 1, lines: int = 40, *, ctx: Context) -> str:
        return json.dumps(await daemon.tool_read_memory(caller_room(ctx), path, from_line, lines))

    @mcp.tool()
    async def worker(action: str, brief: str = "", id: str = "", room: str = "", tasks: list[str] | None = None,
                     cap_minutes: float = 0, account: str = "", model: str = "", *, ctx: Context) -> str:
        """Heavy work leaves the room: a worker is a headless run with its own card in the room and a
        report. action "start": `brief` is the whole job (what done looks like, where it ends, what to
        leave alone); optional `tasks` (its checklist rows), `cap_minutes` (default 65), `account`,
        `model`, `room` (default this room). Returns its id, folder and report path; when it ends, the
        card settles and the room hears the result. "list": workers and their state. "stop" / "report":
        by `id` (report gives the report's text, for you to tell in your own words)."""
        req = {"action": action, "brief": brief, "id": id, "room": room, "tasks": tasks or [],
               "cap_minutes": cap_minutes, "account": account, "model": model}
        return json.dumps(await daemon.tool_worker(caller_room(ctx), req))

    @mcp.tool()
    async def schedule(action: str, name: str = "", when: str = "", what: str = "", kind: str = "agent",
                       tz: str = "", deliver: str = "announce", room: str = "", cap_minutes: float = 0,
                       account: str = "", *, ctx: Context) -> str:
        """Timed work that lands in a room, kept on systemd timers that outlive the daemon. action
        "add": `name` (lowercase letters, digits, hyphens); `when` is a date and time ("2026-03-09 09:00":
        a one-shot, removed after it fires), "every 2h" (an interval), or a cron expression
        ("0 9 * * 1-5"); `tz` an IANA zone (default: this room's); `what` is the brief a worker gets
        (kind "agent": a headless run with a card here, its last message told by this room, exactly
        NO_REPLY for silence) or the shell line to run (kind "script": no model call, its output posted
        as is, exit 0 with no output posts nothing, a failure alerts the ops room; a plain reminder is
        a script that echoes the text, on the second and at no cost); `deliver` "announce" (default) or
        "silent" (run and ledger, never post). Returns the next fire time read back from systemd.
        "remove": by `name`. "list": every job with its next fire time."""
        req = {"action": action, "name": name, "when": when, "what": what, "kind": kind, "tz": tz,
               "deliver": deliver, "room": room, "cap_minutes": cap_minutes, "account": account}
        return json.dumps(await daemon.tool_schedule(caller_room(ctx), req))

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
        route("/control", control)      # `yerk room close`, `yerk worker`: local only, like everything here
    if status:
        route("/status", status)        # `yerk status`: what runs now (status.py), local only the same way
    return mcp


def room_config(url: str, room: str, path: Path) -> Path:
    """The per-room --mcp-config file: this server only, preloaded."""
    cfg = {"mcpServers": {"yerk": {"type": "http", "url": url, "headers": {ROOM_HEADER: room}, "alwaysLoad": True}}}
    path.write_text(json.dumps(cfg, indent=1))
    return path


WORKER_TOOLS = ("recall", "read_memory")    # workers.WORKER_TOOLS: what a worker's own server can carry


def worker_server(state: Path, room: str, tools: list[str]) -> FastMCP:
    """A worker's own MCP server (stdio, started by its claude from workers.mcp_config): only the
    read-only tools named in `tools` and in WORKER_TOOLS, each one /control call to the running
    daemon for the worker's room. It has no send, hold, worker or schedule tool to give."""
    from .jobs import post_control                       # late: jobs imports workers, which builds this config
    mcp = FastMCP("yerk")
    allowed = [t for t in WORKER_TOOLS if t in tools]

    async def call(payload: dict[str, Any]) -> str:
        import asyncio
        try:
            return json.dumps(await asyncio.to_thread(post_control, state, payload, 60))
        except (OSError, ValueError) as e:
            return json.dumps({"ok": False, "error": f"the daemon did not answer: {e}"})

    if "recall" in allowed:
        async def recall(query: str, k: int = 6) -> str:
            return await call({"recall": {"room": room, "query": query, "k": k}})
        mcp.tool(description=RECALL)(recall)
    if "read_memory" in allowed:
        async def read_memory(path: str, from_line: int = 1, lines: int = 40) -> str:
            return await call({"read_memory": {"room": room, "path": path, "start": from_line, "lines": lines}})
        mcp.tool(description=READ_MEMORY)(read_memory)
    return mcp


def main(argv: list[str] | None = None) -> int:
    import sys
    a = sys.argv[1:] if argv is None else argv
    if len(a) == 4 and a[0] == "worker":
        worker_server(Path(a[1]), a[2], [t for t in a[3].split(",") if t]).run("stdio")
        return 0
    print("usage: python -m yerk_ai.mcp_server worker STATE ROOM TOOLS (a worker's own server)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
