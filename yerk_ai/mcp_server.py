# The server scaffold (handlers whose docstrings are the tool descriptions, registered on the
# official Python SDK) and the send tool's shape are adapted from Hermes Agent v0.22.2,
# mcp_serve.py (create_mcp_server, messages_send), https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
"""Yerk AI's own MCP server: eight tools (send, hold, recall, read_memory, remember, worker, schedule, settings), local-only
HTTP, preloaded in every room. A worker gets none of it: by default no MCP server at all, and with
[workers] yerk_tools its own stdio server (`worker_server`) carrying recall and read_memory only.

Every endpoint (/mcp, /hook, /control) wants the `x-yerk-secret` header (restart test R3). Three
values open it: the install's secret (the CLI, the job units, the tests: everything), a room process's
own token (minted per process start and bound with its session in sessions.json: /hook and /mcp, and
the room's identity IS that binding, never a header a caller picks), and the worker secret (/control,
recall and read_memory only, so a worker holds nothing that starts a worker or schedules a job).
Each room's process gets a config naming this one server with `alwaysLoad: true` and its token as the
header, passed with --strict-mcp-config. The same HTTP app answers the hooks.
"""
from __future__ import annotations

import hmac
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .memory import READ_LINES

log = logging.getLogger("yerk.http")
HEADER = "x-yerk-secret"
Auth = dict[str, str]                      # {"kind": install | room | worker, "room": the bound room or ""}
RECALL = ("Search the owner's memory (daily logs, fact files, the curated file, open loops) by keywords\n"
          "and, where the install has a provider key, by meaning too, so a paraphrase finds the line.\n"
          "Each hit is the lines that carry the match and a `cite` (file:line). Cite every fact you use\n"
          "by its `cite`; read_memory(cite) reads on from it. A \"timeout\" status means busy, not down.")
READ_MEMORY = (f"Exact lines of a memory file, `lines` of them (default {READ_LINES}; ask more, up to 400, or a later\n"
               "`from_line` to read on). `path`: a recall `cite` (its line is the start) or a memory path.")


class Daemon(Protocol):
    async def tool_send(self, room: str, text: str, file: str, caller: str) -> dict[str, Any]: ...
    async def tool_hold(self, room: str, wait_for: str, reason: str) -> dict[str, Any]: ...
    async def tool_recall(self, room: str, query: str, k: int) -> dict[str, Any]: ...
    async def tool_read_memory(self, room: str, path: str, start: int, lines: int) -> dict[str, Any]: ...
    async def tool_remember(self, room: str, text: str, about: str, replaces: list[str]) -> dict[str, Any]: ...
    async def tool_rule(self, room: str, text: str, job: str) -> dict[str, Any]: ...
    async def tool_worker(self, room: str, req: dict[str, Any]) -> dict[str, Any]: ...
    async def tool_schedule(self, room: str, req: dict[str, Any]) -> dict[str, Any]: ...
    async def tool_settings(self, room: str, model: str, effort: str, account: str = "") -> dict[str, Any]: ...


def caller_room(ctx: Context) -> str:
    """The room a tool call comes from: the binding that holds the call's token (set by Guard)."""
    req = ctx.request_context.request
    auth = getattr(req.state, "yerk_auth", None) if req is not None else None
    return str(auth.get("room", "")) if isinstance(auth, dict) else ""


Handler = Callable[[dict[str, Any], Auth], Awaitable[dict[str, Any]]]
Check = Callable[[str, str], Auth | None]   # (path, header value) -> who this is, or None: refused


def checker(install: str, worker: str, room_of_token: Callable[[str], str | None]) -> Check:
    """The one rule for every endpoint, compared in constant time; an empty secret never matches. A
    room's own token opens /hook and /mcp only: /control and /status show every room, not a room's to read."""
    def check(path: str, value: str) -> Auth | None:
        if value and install and hmac.compare_digest(value, install):
            return {"kind": "install", "room": ""}
        if value and worker and path.startswith("/control") and hmac.compare_digest(value, worker):
            return {"kind": "worker", "room": ""}
        room = room_of_token(value) if value and not path.startswith(("/control", "/status")) else None
        return {"kind": "room", "room": room} if room else None
    return check


class Guard:
    """ASGI middleware: refuses a request without a valid x-yerk-secret (401, one warning line), else
    hands the request on with who it is in `request.state.yerk_auth`."""

    def __init__(self, app: Any, check: Check):
        self.app, self.check = app, check

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
            auth = self.check(str(scope.get("path", "")), headers.get(HEADER, ""))
            if auth is None:
                log.warning("http: refused %s %s: no valid %s header (unbound or unauthenticated caller)",
                            scope.get("method", "?"), scope.get("path", "?"), HEADER)
                resp = JSONResponse({"ok": False, "error": f"refused: a valid {HEADER} header is required"}, status_code=401)
                await resp(scope, receive, send)
                return
            scope.setdefault("state", {})["yerk_auth"] = auth
        await self.app(scope, receive, send)


def app(mcp: FastMCP, check: Check) -> Any:
    """The HTTP app every client speaks to: the MCP server's, behind the guard."""
    return Guard(mcp.streamable_http_app(), check)


def build(daemon: Daemon, port: int, hook: Handler, control: Handler | None = None, status: Handler | None = None) -> FastMCP:
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
    async def read_memory(path: str, from_line: int = 1, lines: int = READ_LINES, *, ctx: Context) -> str:
        return json.dumps(await daemon.tool_read_memory(caller_room(ctx), path, from_line, lines))

    @mcp.tool()
    async def remember(text: str, about: str = "", replaces: list[str] | None = None, job: str = "", *, ctx: Context) -> str:
        """File in memory now, before you write the answer, what you were just told: a new fact, a decision or a
        correction, one line in plain words. `about`: for a fact, the person, place or thing it is about,
        by the name the owner uses (it names the fact file). `replaces`: for a correction, each old line as
        `cite | its words` (a hit's cite, then the old line's words); the one line from that cite through the
        next 12 holding those words is marked superseded. Returns the new line's cite. `job`: when the owner
        says how a job is done from now on, or corrects how it was done, the job's short name ("open web pages",
        the rules' own name if it has one): `text` becomes its one rule in every room, the older one marked
        replaced."""
        return json.dumps(await (daemon.tool_rule(caller_room(ctx), text, job) if job else
                                 daemon.tool_remember(caller_room(ctx), text, about, replaces or [])))

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

    @mcp.tool()
    async def settings(model: str = "", effort: str = "", account: str = "", *, ctx: Context) -> str:
        """The owner asks to change this room's model or thinking level, or the account it runs on ("put this room on
        Opus at max thinking on account three"): `model` an alias or full id from the install's list, `effort` low,
        medium, high, xhigh or max, `account` an account's name (tried first, the others behind it); "default" for any
        goes back to the room's own. It applies from the next reply on, in the same conversation unless the line says
        a new session starts; with none it says what the room runs now. Say its `line`."""
        return json.dumps(await daemon.tool_settings(caller_room(ctx), model, effort, account))

    def route(path: str, handler: Handler) -> None:
        @mcp.custom_route(path, methods=["POST"])
        async def _route(request: Request) -> Response:
            try:
                payload = await request.json()
            except ValueError:
                return JSONResponse({}, status_code=400)
            auth = getattr(request.state, "yerk_auth", None)
            return JSONResponse(await handler(payload, dict(auth) if isinstance(auth, dict) else {"kind": "", "room": ""}))

    route("/hook", hook)
    if control:
        route("/control", control)      # `yerk room close`, `yerk worker`, the job units, a worker's reads
    if status:
        route("/status", status)        # `yerk status`: what runs now (status.py), local only the same way
    return mcp


def room_config(url: str, room: str, path: Path, token: str = "", servers: dict[str, Any] | None = None) -> Path:
    """The per-room --mcp-config file (0600): this server, preloaded, the process's own token as its secret header
    (the daemon knows the room by the token's binding), and the room's `mcp` servers from rooms.toml, never as yerk."""
    from .config import write_private
    cfg = {"mcpServers": {**dict(servers or {}), "yerk": {"type": "http", "url": url, "headers": {HEADER: token}, "alwaysLoad": True}}}
    write_private(path, json.dumps(cfg, indent=1))
    return path


def worker_server(state: Path, room: str, tools: list[str]) -> FastMCP:
    """A worker's own MCP server (stdio, started by its claude from workers.mcp_config): only the
    read-only tools named in `tools` and in workers.WORKER_TOOLS, each one /control call to the running
    daemon for the worker's room, under the worker secret (reads only). It has no send, hold, worker
    or schedule tool to give, and no secret that would open them."""
    from .jobs import post_control                       # late: jobs imports workers, which builds this config
    from .workers import WORKER_TOOLS
    mcp = FastMCP("yerk")
    allowed = [t for t in WORKER_TOOLS if t in tools]

    async def call(payload: dict[str, Any]) -> str:
        import asyncio
        try:
            return json.dumps(await asyncio.to_thread(post_control, state, payload, 60, True))
        except (OSError, ValueError) as e:
            return json.dumps({"ok": False, "error": f"the daemon did not answer: {e}"})

    if "recall" in allowed:
        async def recall(query: str, k: int = 6) -> str:
            return await call({"recall": {"room": room, "query": query, "k": k}})
        mcp.tool(description=RECALL)(recall)
    if "read_memory" in allowed:
        async def read_memory(path: str, from_line: int = 1, lines: int = READ_LINES) -> str:
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
