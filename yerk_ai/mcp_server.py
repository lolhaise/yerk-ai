# The server scaffold (handlers whose docstrings are the tool descriptions, registered on the
# official Python SDK) and the send tool's shape are adapted from Hermes Agent v0.22.2,
# mcp_serve.py (create_mcp_server, messages_send), https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
"""Yerk AI's own MCP server: twenty-three tools (send, hold, recall, read_memory, remember, worker, schedule, settings, transcribe,
voice_note, seen, render, watch, need, text, email, delete, reverse_image_search, github_state, page, texts, attachments, zoom), local-only HTTP, preloaded in
every room; transcribe, voice_note, watch, text, email, reverse_image_search, github_state and attachments are here and not plug-ins because the daemon has the network
plug-ins because the daemon has the network (and github_state the token) and a room's shell has none, seen because no owner room has a Write tool, render
because LibreOffice needs a writable /tmp a room's sandbox does not give (and LaTeX a sandbox of its own, a shell or none), delete because that sandbox
folders read-only, and zoom because ffmpeg in a room's shell waits on an approval no one gives mid-turn. A worker gets none of it: by default no MCP server at all, and with
[workers] yerk_tools its own stdio server (`worker_server`) carrying recall and read_memory only.

Every endpoint (/mcp, /hook, /control) wants the `x-yerk-secret` header (restart test R3). Three
values open it: the install's secret (the CLI, the job units, the tests: everything), a room process's
own token (minted per process start and bound with its session in sessions.json: /hook and /mcp, and
the room's identity IS that binding, never a header a caller picks), and the worker secret (/control,
recall and read_memory only, so a worker holds nothing that starts a worker or schedules a job). A room's live page
(channels/page.py) is the one other caller: GET /page is the page itself, no data in it, open to a browser with no header,
and its calls under /page/ open with that room's page key only, which opens nothing else; /page/open, with no header,
trades a one-time ticket yerkd put in the address it opened on the owner's screen for that key, once. All of it is on
loopback; with [page] listen the page's routes alone are on the box's tailnet or LAN address too (Guard's `beside`).
Each room's process gets a config naming this one server with `alwaysLoad: true` and its token as the
header, passed with --strict-mcp-config. The same HTTP app answers the hooks.
"""
from __future__ import annotations

import hmac
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .memory import READ_LINES
from .shape import PINS

log = logging.getLogger("yerk.http")
HEADER = "x-yerk-secret"
PAGE = "/page"                             # a room's live page (channels/page.py); its calls under PAGE + "/"
Auth = dict[str, str]                      # {"kind": install | room | worker, "room": the bound room or ""}
RECALL = ("Search the owner's memory (daily logs, fact files, the curated file, open loops) by keywords\n"
          "and, where the install has a provider key, by meaning too, so a paraphrase finds the line.\n"
          "Each hit is the lines that carry the match and a `cite` (file:line). Cite every fact you use\n"
          "by its `cite`; read_memory(cite) reads on from it. A \"timeout\" status means busy, not down.")
# the rule shape `remember` says, in the Claude docs' own words: the pinned sentences, checked daily against the fetched page
# and, 2026-11-09, Claude Fable 5.2: in place of the 40-word sentence cap the gate no longer asks (its page: a hard limit makes
# prose choppy), the line of its "Communicating clearly" block on how a sentence reads
LESSON_SHAPE = (" ".join(PINS[k][1] for k in ("lesson", "why", "on record")) + " So a rule opens with a one-line summary, "
                "gives its reason, keeps its body brief and cites an example by its day-log line "
                "(<example>Landed, daily/YYYY-MM-DD.md:N</example>) in place of pasting it. " + PINS["plain-words"][1])
READ_MEMORY = (f"Exact lines of a memory file, `lines` of them (default {READ_LINES}; ask more, up to 400, or a later\n"
               "`from_line` to read on). `path`: a recall `cite` (its line is the start) or a memory path.")


class Daemon(Protocol):
    async def tool_send(self, room: str, text: str, file: str, caller: str) -> dict[str, Any]: ...
    async def tool_hold(self, room: str, wait_for: str, reason: str) -> dict[str, Any]: ...
    async def tool_recall(self, room: str, query: str, k: int) -> dict[str, Any]: ...
    async def tool_read_memory(self, room: str, path: str, start: int, lines: int) -> dict[str, Any]: ...
    async def tool_remember(self, room: str, text: str, about: str, replaces: list[str], said: str = "", source: str = "") -> dict[str, Any]: ...
    async def tool_rule(self, room: str, text: str, job: str, triggers: list[str] | None = None,
                        older: list[str] | None = None) -> dict[str, Any]: ...
    async def tool_retire(self, room: str, at: str, why: str) -> dict[str, Any]: ...
    async def tool_worker(self, room: str, req: dict[str, Any]) -> dict[str, Any]: ...
    async def tool_schedule(self, room: str, req: dict[str, Any]) -> dict[str, Any]: ...
    async def tool_settings(self, room: str, model: str, effort: str, account: str = "") -> dict[str, Any]: ...
    async def tool_transcribe(self, room: str, path: str, owner_yes: bool = False) -> dict[str, Any]: ...
    async def tool_voice_note(self, room: str, script: str, name: str = "", owner_yes: bool = False) -> dict[str, Any]: ...
    async def tool_render(self, room: str, path: str) -> dict[str, Any]: ...
    async def tool_watch(self, room: str, url: str, frames: int = 6, owner_yes: bool = False, path: str = "") -> dict[str, Any]: ...
    async def tool_seen(self, room: str, path: str, text: str, shows: str) -> dict[str, Any]: ...
    async def tool_need(self, room: str, asked: str, tried: str, needed: str, shadow: str = "") -> dict[str, Any]: ...
    async def tool_text(self, room: str, to: str, text: str) -> dict[str, Any]: ...
    async def tool_delete(self, room: str, paths: list[str]) -> dict[str, Any]: ...
    async def tool_email(self, room: str, ti: dict[str, Any]) -> dict[str, Any]: ...
    async def tool_reverse_image_search(self, room: str, path: str) -> dict[str, Any]: ...
    async def tool_github_state(self, room: str, branch_or_pr: str) -> dict[str, Any]: ...
    async def tool_page(self, room: str, req: dict[str, Any]) -> dict[str, Any]: ...
    async def tool_texts(self, room: str, limit: int) -> dict[str, Any]: ...
    async def tool_attachments(self, room: str, message: str, name: str) -> dict[str, Any]: ...
    async def tool_zoom(self, room: str, path: str, x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]: ...


def caller_room(ctx: Context) -> str:
    """The room a tool call comes from: the binding that holds the call's token (set by Guard)."""
    req = ctx.request_context.request
    auth = getattr(req.state, "yerk_auth", None) if req is not None else None
    return str(auth.get("room", "")) if isinstance(auth, dict) else ""


Handler = Callable[[dict[str, Any], Auth], Awaitable[dict[str, Any]]]
Check = Callable[[str, str], Auth | None]   # (path, header value) -> who this is, or None: refused


def checker(install: str, worker: str, room_of_token: Callable[[str], str | None],
            page_of_key: Callable[[str], str | None] | None = None) -> Check:
    """The one rule for every endpoint, compared in constant time; an empty secret never matches. A
    room's own token opens /hook and /mcp only: /control and /status show every room, not a room's to read.
    A page's calls (/page/...) open with that room's page key and nothing else, and the key opens nothing but them;
    /page itself, the page with no data in it, and /page/open, whose route checks its one-time ticket, open with no header."""
    def check(path: str, value: str) -> Auth | None:
        if path in (PAGE, PAGE + "/open"):          # the page itself, and its one-time ticket's trade, which its route checks
            return {"kind": "open", "room": ""}
        if path.startswith(PAGE + "/"):
            room = page_of_key(value) if value and page_of_key is not None else None
            return {"kind": "page", "room": room} if room else None
        if value and install and hmac.compare_digest(value, install):
            return {"kind": "install", "room": ""}
        if value and worker and path.startswith("/control") and hmac.compare_digest(value, worker):
            return {"kind": "worker", "room": ""}
        room = room_of_token(value) if value and not path.startswith(("/control", "/status")) else None
        return {"kind": "room", "room": room} if room else None
    return check


class Guard:
    """ASGI middleware: refuses a request without a valid x-yerk-secret (401, one warning line), else
    hands the request on with who it is in `request.state.yerk_auth`. A request that came in on one of the
    `beside` addresses, the box's tailnet or LAN one the page is also served on ([page] listen), gets the page's
    routes and nothing else: any other path is not found there (404), whatever secret it carries."""

    def __init__(self, app: Any, check: Check, beside: frozenset[str] = frozenset()):
        self.app, self.check, self.beside = app, check, beside

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] != "lifespan" and (scope.get("server") or ("",))[0] in self.beside and not (path == PAGE or path.startswith(PAGE + "/")):
            log.warning("http: refused %s %s on the page's own address: only the page is served there", scope.get("method", "?"), path)
            if scope["type"] == "http":
                await JSONResponse({"ok": False}, status_code=404)(scope, receive, send)
            else:                               # a websocket's handshake, refused before it opens
                await send({"type": "websocket.close", "code": 1008})
            return
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


def app(mcp: FastMCP, check: Check, beside: frozenset[str] = frozenset()) -> Any:
    """The HTTP app every client speaks to: the MCP server's, behind the guard; on the `beside` addresses, the page's alone."""
    return Guard(mcp.streamable_http_app(), check, beside)


def build(daemon: Daemon, port: int, hook: Handler, control: Handler | None = None, status: Handler | None = None,
          live: Any = None) -> FastMCP:
    mcp = FastMCP("yerk", host="127.0.0.1", port=port, stateless_http=True, json_response=True, log_level="WARNING")

    @mcp.tool()
    async def send(room: str, text: str = "", file: str = "", *, ctx: Context) -> str:
        """Post to a room now, before your reply: text, or a file (absolute path) with the text as its
        caption; a voice_note file goes as a voice message. `room` is a room's name; this room's own name or its
        channel id both mean this room. Returns the posted message ids."""
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

    async def remember(text: str, about: str = "", replaces: list[str] | None = None, job: str = "", triggers: list[str] | None = None,
                       said: str = "", source: str = "", older: list[str] | None = None, retire: str = "", *, ctx: Context) -> str:
        """File in memory now, before you write the answer, what you were just told: a new fact, a decision or a
        correction, one line in plain words. `about`: for a fact, the person, place or thing it is about,
        by the name the owner uses (it names the fact file). `replaces`: for a correction, each old line as
        `cite | its words` (a hit's cite, then the old line's words); the one line from that cite through the
        next 12 holding those words is marked superseded. Returns the new line's cite. `job`: when the owner
        says how a job is done from now on, or corrects how it was done, the job's short name ("open web pages",
        the rules' own name if it has one): `text` becomes its one rule in every room, the older one marked
        replaced. A rule is one lesson in the Claude docs' shape, LESSON_SHAPE; one that is not comes back with each
        fix and the docs sentence it misses, and so do live lines of other jobs on the same thing, settled in `older`:
        `cite | superseded: why` or `cite | kept: why`. `retire`: a rule's cite that turned out wrong, `text` why;
        its line is deleted and its words kept in the day log.
        `triggers`, with `job`: words or short phrases a message on the job holds (the owner's, when
        named); a topic rule loads on them and its job's words. `said`: when `text` is what a person said or
        wrote, word for word, their name, with `source` (the message id, file path or transcript line holding
        those words): it is filed as their words. Anything else about a person (what they decided or want, in
        your words) is your note, and is shown as your note, never as their words."""
        return json.dumps(await (daemon.tool_retire(caller_room(ctx), retire, text) if retire else
                                 daemon.tool_rule(caller_room(ctx), text, job, triggers, older) if job else
                                 daemon.tool_remember(caller_room(ctx), text, about, replaces or [], said, source)))
    mcp.tool(description=inspect.cleandoc(remember.__doc__ or "").replace("LESSON_SHAPE", LESSON_SHAPE))(remember)

    @mcp.tool()
    async def worker(action: str, brief: str = "", id: str = "", room: str = "", tasks: list[str] | None = None,
                     cap_minutes: float = 0, account: str = "", model: str = "", effort: str = "", *, ctx: Context) -> str:
        """Heavy work leaves the room: a worker is a headless run with its own card in the room and a
        report. action "start": `brief` is the whole job (what done looks like, where it ends, what to
        leave alone); optional `tasks` (its checklist rows), `cap_minutes` (default 65), `account`, `model`,
        `effort` (low, medium, high, xhigh, max; default the room's live level), `room` (default this room). Returns its id, folder
        and report path; when it ends, the card settles and the room hears the result, or nothing if its last message is exactly
        NO_REPLY. "list": workers and their state. "stop" / "report": by `id` (report gives the report's text, for you to tell in your own words)."""
        req = {"action": action, "brief": brief, "id": id, "room": room, "tasks": tasks or [],
               "cap_minutes": cap_minutes, "account": account, "model": model, "effort": effort}
        return json.dumps(await daemon.tool_worker(caller_room(ctx), req))

    @mcp.tool()
    async def schedule(action: str, name: str = "", when: str = "", what: str = "", kind: str = "",
                       tz: str = "", deliver: str = "announce", room: str = "", cap_minutes: float = 0,
                       account: str = "", *, ctx: Context) -> str:
        """Timed work that lands in a room, kept on systemd timers that outlive the daemon. action
        "add": `name` (lowercase letters, digits, hyphens); `when` is a date and time ("2026-03-09 09:00":
        a one-shot, removed after it fires) or a systemd calendar line, recurring ("Mon..Fri 09:00",
        "*:0/30" every half hour; never cron); `tz` an IANA zone (default: this room's); `what`, by `kind` (default "agent" in the
        owner's rooms, "note" in any other): "note", a reminder
        (when it is due this room is told the text and says it in its own voice; no worker, shell or
        account); "agent", the brief a worker gets (a headless run with a card here, its last message told
        by this room, exactly NO_REPLY for silence); "script", the shell line to run (no model call, its
        output posted as is, exit 0 with no output posts nothing, a failure alerts the ops room). Any room
        adds, lists and removes its own notes; an agent or script job runs as the owner, so only the owner's
        rooms set one. `deliver` "announce" (default) or "silent" (run and ledger, never post). Returns the
        next fire time read back from systemd. "remove": by `name`. "list": the jobs this room may see.
        <example>"remind me Saturday at 10 to call the clinic": action "add", kind "note", name
        "call-clinic", when "2026-10-24 10:00", what "Remind them to call the clinic now."</example>"""
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

    @mcp.tool()
    async def transcribe(path: str, owner_yes: bool = False, *, ctx: Context) -> str:
        """A voice note or recording as text, since you take in sound only this way: `path` is the file's saved path from
        its [attachment] line, or another audio file in this room's folders. The daemon transcribes it and returns the
        words with a timestamp and, when two or more talk, a speaker letter per segment, saved beside the audio as
        NAME.transcript.txt. A segment marked (uncertain) has two readings that disagree, so give it as unsure or ask. A
        recording past US$1 comes back refused until the owner says yes; then call again with `owner_yes` true."""
        return json.dumps(await daemon.tool_transcribe(caller_room(ctx), path, owner_yes))

    @mcp.tool()
    async def render(path: str, *, ctx: Context) -> str:
        """A document as a PDF and its page count, since LibreOffice does not run in your shell: `path` is a document in this
        room's folders (a .docx, .doc, .odt, .rtf, .xlsx, .pptx or anything LibreOffice opens). The daemon renders it to a PDF
        saved beside it as NAME.pdf (an older NAME.pdf there is replaced) and returns its page count; a .pdf path returns its
        page count alone, nothing rendered. A .tex in your work folder is compiled by LaTeX (latexmk; pdflatex unless it
        loads fontspec or has a `% !TEX program` line); a failed compile says TeX's first error and its line. Use it to check a document fits a page count before you send it, and `send` the PDF
        when the owner wants the PDF. The layout is LibreOffice's, not Word's, so a document that fits by a line in one can
        spill in the other: say a result near the edge as near the edge."""
        return json.dumps(await daemon.tool_render(caller_room(ctx), path))

    @mcp.tool()
    async def watch(url: str = "", path: str = "", frames: int = 6, owner_yes: bool = False, *, ctx: Context) -> str:
        """A video in a post, its frames and its transcript, since your shell has no network to pull it: `url` is the link to
        a post that holds a video (an Instagram reel or post, a TikTok, an X post, a YouTube link, or any page yt-dlp knows).
        The daemon pulls the video into this room's work folder under videos/, takes `frames` still pictures spread evenly
        through it (6 unless you ask for 1 to 12) and transcribes its sound, and returns the file paths and the transcript;
        open the frames with Read to see what is on screen. The transcript is the video's own captions when the page has
        them (uploaded or auto-generated; YouTube's, and any site's yt-dlp lists in json3), free and with their timestamps,
        and only a video with no captions goes to the paid transcription and its US$1 gate. Use it whenever the owner sends
        a video link and expects you to have watched it, since the caption, the cover picture and the comments are not the
        video. A page that needs a login, a private post or a page with no video comes back as that, in the site's own
        words. A transcript past US$1 comes back refused until the owner says yes; then call again with `owner_yes` true. A
        video already pulled is not pulled or transcribed again. A video file sent to you, which Read cannot open, is watched
        the same way from `path`, its saved path from its [attachment] line, in place of `url`; nothing is pulled. A video
        attached to a Discord message is watched from the message's link as `url` (discord.com/channels/SERVER/CHANNEL/MESSAGE),
        saved to this room's inbox first; a message in a channel this room does not read is refused."""
        return json.dumps(await daemon.tool_watch(caller_room(ctx), url, frames, owner_yes, path))

    @mcp.tool()
    async def seen(path: str, text: str, shows: str, *, ctx: Context) -> str:
        """After you Read a picture sent to you and before your answer: file what you read on it, so a later recall finds
        it. `path` its saved path from the [attachment] line; `text` every word on it, verbatim, in reading order (a chat
        screenshot keeps who said what as shown), "" if none; `shows` one or two lines of what it shows, a person in it "the
        owner" (in a person's room "the sender") unless its own text says otherwise, anyone else named only by that text, else "a man" or
        "a woman". Saved beside it as NAME.seen.txt."""
        return json.dumps(await daemon.tool_seen(caller_room(ctx), path, text, shows))

    @mcp.tool()
    async def need(asked: str, tried: str, needed: str, shadow: str = "", *, ctx: Context) -> str:
        """You cannot do what you were asked (no tool, a refused tool, a wall): file what you needed in place of building a
        way of your own, so one build gives every room the same way. `asked` what you were asked, `tried` what you tried and
        how it failed, `needed` the ability in a few plain words ("see a picture in a post"), `shadow` what you did anyway, if
        anything. The same ability filed again is counted on the open one."""
        return json.dumps(await daemon.tool_need(caller_room(ctx), asked, tried, needed, shadow))

    @mcp.tool()
    async def text(to: str, text: str, *, ctx: Context) -> str:
        """Texts one person as the owner when the owner asks you to: an iMessage, else an SMS, from the owner's Mac. `to` is
        their phone number or email address, never a name (recall it from the owner's texts, or ask); `text` the message
        exactly as it should arrive. It is held for the owner to read first: once they say the word, call it again the same
        and it goes once. A result with `maybe_sent` may have gone out: say so, and never send it again unasked."""
        return json.dumps(await daemon.tool_text(caller_room(ctx), to, text))

    @mcp.tool()
    async def email(to: list[str], subject: str, text: str, files: list[str] | None = None, *, ctx: Context) -> str:
        """Emails as the owner from the owner's own Gmail when the owner asks you to. `to` their email addresses, never a name
        (ask, or find it in the owner's mail); `subject` one line; `text` the email exactly as it should arrive; `files` whole
        paths of files in this room's folders to attach (a file sent to the room is in its inbox). It is held for the owner to
        read first: once they say the word, call it again the same and it goes once. A result with `maybe_sent` may have gone
        out: say so, and never send it again unasked."""
        ti = {"to": to, "subject": subject, "text": text, **({"files": files} if files else {})}
        return json.dumps(await daemon.tool_email(caller_room(ctx), ti))

    @mcp.tool()
    async def texts(limit: int = 0, *, ctx: Context) -> str:
        """The owner's own recent texts (iMessage and SMS), newest first, read from the owner's Mac: use it to answer
        whether someone has texted, or what a text said. This only reads; it never sends one (that is `text`). `limit` how
        many to return (12 unless you ask, up to 40). Each message has `who` (the other person's number or address, empty
        for one the owner sent), `from_me`, `at` (epoch seconds) and `text`. The owner's Mac sleeps when its lid is shut on
        battery, so the texts are kept as a copy on the box, refreshed whenever the Mac is awake: `as_of` says where this
        answer comes from. With `awake` true it was just read from the Mac; with it false the Mac is asleep or away and the
        answer is the last copy, whose age `as_of` gives, so say that time and do not pass a stale answer off as fresh."""
        return json.dumps(await daemon.tool_texts(caller_room(ctx), limit))

    @mcp.tool()
    async def delete(paths: list[str], *, ctx: Context) -> str:
        """Deletes files or folders outside this room's work folder when the owner orders it, since your shell deletes only
        inside it: for good, not to the trash, so the disk space comes back. `paths`: whole, real paths from /. Held for the
        owner to read first: once they say the word, call it again the same and it runs once. Returns what went, what failed
        and why, and the space freed."""
        return json.dumps(await daemon.tool_delete(caller_room(ctx), paths))

    @mcp.tool()
    async def reverse_image_search(path: str, *, ctx: Context) -> str:
        """Searches the web by picture, as Google Lens or TinEye do, since a word search cannot look for a picture: `path` is a
        picture in this room's folders (its [attachment] path, a web_image file, a watch frame). The daemon sends it to Google
        Cloud Vision's web detection and returns Google's best guess at what it is, the names it ties to it (a work and its
        maker, a place, a product), the pages that show it, and the copies and look-alikes it found. Use it when asked where a
        picture comes from, what it is or who made it; open a page that shows it before you say so, and give the best guess
        as a guess."""
        return json.dumps(await daemon.tool_reverse_image_search(caller_room(ctx), path))

    @mcp.tool()
    async def zoom(path: str, x0: float, y0: float, x1: float, y1: float, *, ctx: Context) -> str:
        """A part of a picture, enlarged, since Read shows you a picture at most 2000 px on its long side and the small print
        of a bigger one is lost: use it before you answer from a time, a figure, a name or any print you would squint at (a
        calendar, a form, a receipt, a chat's timestamps). `path` a picture in this room's folders (its [attachment] path);
        x0, y0, x1, y1 the part as fractions of its width and height from its top left (0, 0.5, 0.5, 1: its bottom left
        quarter), so look at the whole first and zoom where the answer is. Returns a PNG 2000 px on its long side (enlarged 4
        times at most): Read it, then answer. When Read refuses a picture, zoom 0, 0, 1, 1 makes the whole of it one Read opens."""
        return json.dumps(await daemon.tool_zoom(caller_room(ctx), path, x0, y0, x1, y1))

    @mcp.tool()
    async def github_state(branch_or_pr: str, *, ctx: Context) -> str:
        """A branch or a pull request of Yerk's own repository as GitHub has it now, since that repository is private and no
        room holds a GitHub login (a web fetch there gets a 404): `branch_or_pr` is a branch's name (a worker's) or a pull
        request's number. Returns the branch's head on GitHub (`github`), the same branch in Yerk's checkout against it
        (`here`: whether GitHub has its commit, how many commits each side lacks) and its pull requests: open, draft, merged
        or closed, and an open one's checks. Use it when asked whether a branch was pushed or its pull request is open,
        green or merged."""
        return json.dumps(await daemon.tool_github_state(caller_room(ctx), branch_or_pr))

    @mcp.tool()
    async def voice_note(script: str, name: str = "", owner_yes: bool = False, *, ctx: Context) -> str:
        """Speaks `script` as a voice note in English, when the owner asks for one or to say something out loud. Write it as
        it should sound: `[[pause]]` alone on a line between sections, `(break)` for a short pause. The daemon makes it (your
        shell has no network), checks that its first 20 s read back as your opening, and returns its file, length, engine,
        cost and `probe` words: `send` that file with no text and it goes as a voice message. A note past US$1 comes back
        refused until the owner says yes; then call again with `owner_yes` true."""
        return json.dumps(await daemon.tool_voice_note(caller_room(ctx), script, name, owner_yes))

    @mcp.tool()
    async def page(code: str = "", tests: str = "", marks: list[dict[str, Any]] | None = None, hint: str | None = None,
                   bar: dict[str, str] | None = None, timer: int = -1, stall: int = -1, open: bool = False, where: str = "",
                   off: bool = False, *, ctx: Context) -> str:
        """Draws on this room's live page, where the owner types, draws and runs tests in a browser; each part given replaces
        what it shows. `code` a new program (solution.py); `tests` its unittest file (it imports solution; blank: the program
        runs alone, its prints shown large), which the page runs on every pause in typing; `marks` line marks, each {"line": N, "kind": "highlight" or "arrow", "text": a note},
        [] clears; `hint` a card under the program; `bar` labels and values, a scoreboard on top ("Gold II" a rank badge,
        "12/40" or a number under LP a bar, a number big); `timer` seconds on its clock, 0 stops;
        `stall` seconds of no typing before the page tells you, 0 off. Your replies show there too, and with its chat closed
        go into the program as #> lines under the owner's last # line. What the owner types,
        draws and says there comes to you as messages, mid-turn too; each keystroke is in page/events.jsonl. `open` true
        (the owner asked for the page) turns it on, with no restart, and opens it on the owner's screen, `where` "mac" on
        their Mac (default "pc"); `off` true: off."""
        req = {k: v for k, v in {"code": code or None, "tests": tests or None, "marks": marks, "hint": hint, "bar": bar,
                                 "timer": timer if timer >= 0 else None, "stall": stall if stall >= 0 else None,
                                 "open": open or None, "where": where or None, "off": off or None}.items() if v is not None}
        return json.dumps(await daemon.tool_page(caller_room(ctx), req))

    @mcp.tool()
    async def attachments(message: str = "", name: str = "", *, ctx: Context) -> str:
        """The files an earlier Discord message carries, saved again to this room's inbox, since your shell has no network: a
        file this room posted whose copy is gone (the sweep took it), or one sent here. `message` its id (the [m:] of its
        day-log line; in this room's channel or the one it watches) or its link (discord.com/channels/SERVER/CHANNEL/MESSAGE;
        an owner's room, any channel of its server); `name` a file's name: with `message`, that file only; alone, the newest
        file this room posted under it. Returns each saved path: `send` it again, Read it or attach it."""
        return json.dumps(await daemon.tool_attachments(caller_room(ctx), message, name))

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
    if live is not None:                # the rooms' live pages (channels/page.py)
        pages(mcp, live)
    return mcp


def pages(mcp: FastMCP, page: Any) -> None:
    """A room's live page (channels/page.py): the page itself, then its feed, its events and its test runs, each under
    the room its key opens (Guard)."""
    def room_of(request: Request) -> str:
        auth = getattr(request.state, "yerk_auth", None)
        return str(auth.get("room", "")) if isinstance(auth, dict) and auth.get("kind") == "page" else ""

    @mcp.custom_route(PAGE, methods=["GET"])
    async def _page(request: Request) -> Response:
        return HTMLResponse(page.html(), headers={"cache-control": "no-store", "referrer-policy": "no-referrer"})

    @mcp.custom_route(PAGE + "/open", methods=["POST"])
    async def _open(request: Request) -> Response:
        try:                                    # the address yerkd opened on the owner's screen: its ticket, once, for the key
            payload = await request.json()
        except ValueError:
            payload = None
        r = page.redeem(str(payload.get("ticket") or "")) if isinstance(payload, dict) else {"ok": False}
        return JSONResponse(r, status_code=200 if r["ok"] else 401)

    @mcp.custom_route(PAGE + "/stream", methods=["GET"])
    async def _stream(request: Request) -> Response:
        if not (room := room_of(request)):
            return JSONResponse({"ok": False}, status_code=401)
        return StreamingResponse(page.stream(room), media_type="application/x-ndjson", headers={"cache-control": "no-store"})

    for name in ("event", "run"):
        def bind(name: str) -> None:
            @mcp.custom_route(f"{PAGE}/{name}", methods=["POST"])
            async def _call(request: Request) -> Response:
                if not (room := room_of(request)):
                    return JSONResponse({"ok": False}, status_code=401)
                try:
                    payload = await request.json()
                except ValueError:
                    return JSONResponse({}, status_code=400)
                if not isinstance(payload, dict):
                    return JSONResponse({}, status_code=400)
                return JSONResponse(await getattr(page, name)(room, payload))
        bind(name)


def room_config(url: str, room: str, path: Path, token: str = "", servers: dict[str, Any] | None = None) -> Path:
    """The per-room --mcp-config file (0600): this server, preloaded, its token as its secret header (the daemon knows the room by
    the token's binding), its `timeout` (10 min) letting a call that answers when done, as transcribe, past Claude Code's 60 s for HTTP;
    and the room's `mcp` servers from rooms.toml, never as yerk, each stdio one told its own `timeout` in YERK_TOOL_TIMEOUT (need 49)."""
    from .config import write_private
    yerk = {"type": "http", "url": url, "headers": {HEADER: token}, "alwaysLoad": True, "timeout": 600_000}
    own = {s: {**v, "env": {**v.get("env", {}), "YERK_TOOL_TIMEOUT": str(v["timeout"])}} if v.get("type", "stdio") == "stdio"
           and isinstance(v.get("timeout"), int) and isinstance(v.get("env", {}), dict) else v for s, v in dict(servers or {}).items()}
    write_private(path, json.dumps({"mcpServers": {**own, "yerk": yerk}}, indent=1))
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
