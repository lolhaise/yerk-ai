"""Where Yerk AI keeps things and how it reads them: its own tables such as budgets.toml, the private folder, the state folder."""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger("yerk.config")
PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parent
EXAMPLE_PRIVATE = REPO / "examples" / "private"
EFFORTS = ("low", "medium", "high", "xhigh", "max")      # Claude Code 2.1.301's --effort levels (its --help)
_IGNORED: set[tuple[str, str, str]] = set()             # (room, key, value) of an override already logged
# <state>/rooms/<room>/starts/, beside assembled.jsonl: each session's start file as it began (<id>.md) and the texts news tells
# it was built from (<id>.json), so a resumed session keeps the system prompt it started with (engine.kept_start, context.news)
STARTS = "starts"
# The pages `yerk docs` keeps (docs.py), slug = URL, [docs] pages in yerkd.toml replacing them whole: the prompting guide,
# the prompting page of each model in models.toml, and the Claude Code pages a room's harness leans on (the system prompt
# flags are on cli-reference and the Agent SDK's page). Each answered `Accept: text/markdown` with Markdown on 2026-10-21.
# 2026-11-09: prompting-fable-5-2 added the day Claude Fable 5.2 shipped, since a room's checks read its own model's page (shape.page_of).
_PE, _CC = "https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/", "https://code.claude.com/docs/en/"
DOCS_PAGES = {"prompting-guide": _PE + "claude-prompting-best-practices",
              **{f"prompting-{m}": f"{_PE}prompting-claude-{m}" for m in ("opus-5-5", "sonnet-5-5", "haiku-5-5", "fable-5-2", "fable-5-1", "fable-5")},
              **{f"code-{p.rsplit('/', 1)[-1].removeprefix('modifying-')}": _CC + p for p in
                 ("settings", "hooks", "memory", "sandboxing", "mcp", "cli-reference", "agent-sdk/modifying-system-prompts")}}


def table(name: str) -> Path:
    """budgets.toml or failures.toml: inside the installed package, else at the repo root."""
    for base in (PACKAGE, REPO):
        if (base / name).is_file():
            return base / name
    raise FileNotFoundError(name)


def head(tree: Path) -> str:
    """The commit checked out in `tree`; "" outside a git checkout. yerkd and yerk-index each keep the one they started on."""
    try:
        return subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text())


def toml_value(v: Any) -> str:
    """One value as TOML writes it: true or false, a number, a string (a DEL escaped, a lone surrogate as ?), or a list."""
    if isinstance(v, list):
        return f"[{', '.join(toml_value(x) for x in v)}]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    return json.dumps(str(v).encode("utf-8", "replace").decode(), ensure_ascii=False).replace("\x7f", "\\u007f")


def flag(value: Any, default: bool = False) -> bool:
    """A yes-or-no setting, read strictly: a TOML true or false, or the word "true" or "false" in
    quotes (any case); any other value is the default, so a quoted "false" turns nothing on."""
    if isinstance(value, bool):
        return value
    word = value.strip().lower() if isinstance(value, str) else ""
    return word == "true" if word in ("true", "false") else default


def discord_id(value: Any) -> int | None:
    """A Discord id setting, read strictly: a TOML integer or digits in quotes; None for anything else
    (missing, a bool, a float, which cannot hold an id exactly, other text, zero)."""
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def budgets(conf: dict[str, Any] | None = None) -> dict[str, Any]:
    """budgets.toml; with the private yerkd.toml's `conf` and its [limits] uncapped true, `limits` beside its tables (uncapped)."""
    b = load_toml(table("budgets.toml"))
    return {**b, "limits": {"uncapped": True}} if flag(dict((conf or {}).get("limits") or {}).get("uncapped")) else b


def uncapped(b: dict[str, Any]) -> bool:
    """budgets `b` of an uncapped install: every character budget reads as unlimited for what is sent, its number a measurement."""
    return bool(b.get("limits", {}).get("uncapped"))


def private_dir(arg: str | None = None) -> Path:
    """--private, then $YERK_PRIVATE, then the fake example layer shipped with the repo."""
    p = arg or os.environ.get("YERK_PRIVATE")
    return Path(p).expanduser() if p else EXAMPLE_PRIVATE


def state_dir(arg: str | None = None) -> Path:
    """--state, then $YERK_STATE, then $XDG_STATE_HOME/yerk (~/.local/state/yerk); made 0700, so nothing in it
    (the conversation logs, the ledger, a room's context) is another local user's to read."""
    p = arg or os.environ.get("YERK_STATE")
    d = Path(p).expanduser() if p else Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "yerk"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if d.stat().st_mode & 0o077:                            # an install from before 0700: the next start, yerkd, yerk or a deploy, closes it
        d.chmod(0o700)
    return d


# Every key a loader reads from a room in rooms.toml, in plain words; `yerk doctor` names any other as a typo (test_doctor fails on
# a read past it), and docs/settings.md is written from it (decision 16). A file's path is relative to the room's layer.
ROOM_WORDS = {
    "accounts": "the accounts the room is tried on, in this order, then the rest of accounts.toml; the settings tool or /model can put it on one first",
    "allow": "permission rules beyond Read (the tool goes in `tools` too); the gates run first. An owner room carries [defaults] allow unless it sets its own",
    "allow_remove": "tools an owner room does without, out of the allow it inherits from [defaults]",
    "also": "which of yerkd.toml's [memory] also roots reach the room's recall, read_memory and names (unset: all of them)",
    "card": "steps: a room with people shows the owner rooms' progress card, the model's newest line and one row per step, done or not done "
    "(unset or any other value: its newest holding line and a step count)",
    "channel": "the chat the room is, discord:<channel id>",
    "class": "the class the room's day-log lines carry: private, room or shared (default: private in an owner room, room in any other)",
    "compact_window": "tokens the session keeps before Claude Code compacts (unset: budgets.toml [session]; 0: the model's own)",
    "daily_notes": "the room's day-log folder (default memory/daily)",
    "effort": "passed as --effort: low, medium, high, xhigh or max (unset: the model's own default)",
    "id": "the room's id in the day logs and the ledgers (default: its channel, else its name)",
    "idle_compact_after": "minutes with no turn before a session at idle_compact_at or more is compacted (unset: budgets.toml [session])",
    "idle_compact_at": "the share of compact_window past which an idle session is compacted between turns (unset: budgets.toml [session]; 0: never)",
    "idle_compact_within": "the share of compact_window under Claude Code's own line (the window less 33,000) within which a session is "
    "compacted as soon as a turn ends with nothing waiting, not idle_compact_after later (unset: budgets.toml [session]; 0: never)",
    "layer": "the folder the room's files come from, in the private folder (default: the private folder itself)",
    "mcp": "the room's own MCP servers, one [rooms.NAME.mcp.SERVER] table each; an owner room carries each [defaults] server it does not set",
    "mention_recall": "the people and things a message names, with their best lines, in its recall part (default true)",
    "model": "the model the room runs on, an alias or id from the install's model list; also what a turn runs on while every account is "
    "out of the one the settings tool put it on",
    "open_loops": "the open commitments file, kept whole in the recall pack at a session start",
    "page": "true: a live page for the room, a browser tab at the address `yerk room page NAME` prints, where the owner's typing, drawing and "
    "test runs reach the room as the owner's messages and its replies and the page tool render (default false; the page tool's open and "
    "off switch it with no restart, kept in the room's override over this)",
    "people": "Discord user ids answered in the room besides the owner, as sender person (unset: the owner only); needs privacy family or shared",
    "persona": "the persona file, the session start's persona part",
    "pinned_memory": "the pinned memory file, the session start's pinned_memory part",
    "precompact_within": "the share of compact_window under Claude Code's own line within which a turn's end compacts a copy of the "
    "session, the room left free, and the copy becomes its session once compacted, at a turn's end (unset: budgets.toml [session]; 0: never)",
    "privacy": "owner (the default): the owner's room, which reads the whole memory; any other value walls the room: its own lines and the shared files only",
    "read": "folders the room may read besides its own, from its work folder; a room with people takes none, trusted or not",
    "recall_days": "how many of the newest day logs the recall pack reads at a session start (default 2)",
    "role": "machine: the machine room, where the morning diff, the deploy card and the ops lines land",
    "rulebook": "the rulebook file, the session start's rulebook part",
    "sandbox": "[rooms.NAME.sandbox]: Claude Code's sandbox for the room's shell (default: Yerk AI's own when a shell tool is listed); a trusted "
    "room may add filesystem.denyRead, filesystem.denyWrite and the network allowlist, no other key",
    "sealed": "true: in Yerk's own tree, the room's own lines only, no other room's lines, notes, facts or rules",
    "shell": "trusted: a room with people that runs the owner rooms' tools, its shell inside the daemon's wall (`yerk explain trusted`), Write "
    "and Edit on its layer and work folder only",
    "thinking_panel": "owner-only rooms: a thinking line on the card, about 5 s a reply (default false)",
    "timezone": "the room's IANA zone: its clock line (default UTC) and its jobs (else [memory] timezone)",
    "tools": "the built-in tools the room's Claude Code gets (--tools); a shell tool brings the sandbox",
    "topic_recall": "the room's own lines on the message's topic, and the search of a question about the past, in its recall part (default true)",
    "voice": "the room's voice file, the session start's room_voice part",
    "watch": "discord:<channel id> of a channel the room watches: [watch] assistants' messages there go in its day log, never answered",
    "watch_audit": "true: the room grades the watched assistant's answer to every watched message, a verdict line (🟢 looks good, 🟠 right "
    "but missing something, 🔴 wrong) then the fix, never silent (default false: read along, answer only when asked)",
    "watch_assistant": "the assistant the owner talks to in the watched channel, by name: a message read from there comes in marked as sent to it, "
    "not to this room (default: the other assistant)",
    "watch_wait": "seconds the room waits for the watched assistant's answer after a watched message, counted again from that assistant's "
    "progress card's last edit while the card is up, at most three times this (default 150)",
}
ROOM_KEYS = frozenset(ROOM_WORDS)
# The private yerkd.toml's keys by section, in plain words, beside those described where they are read (words.SETTINGS for
# [presentation], memory.described for the embedding and recall keys of [memory], jobs.CONF for [jobs]); every key optional
# but [discord]'s first two. docs/settings.md is written from them; a test fails on a key here the package never names.
YERKD_WORDS = {
    "discord": {"token_file": "the bot's token, a file in the private folder only you can read (0600)",
                "owner": "the owner's Discord user id, answered in every room",
                "test_senders": "webhook ids accepted as senders in test mode (`yerk setup-room` makes one a room)",
                "guild": "the Discord server's id, where `yerk setup-room` finds or makes a room's channel and its test webhook (that command only)",
                "webhook_burst": "webhook events per 5 minutes before 10 minutes off, the loop guard (default 100)",
                "commands": "the slash commands (default true)"},
    "http": {"port": "the local port of /mcp, /hook and /control (default: a free one; yerkd --port wins)"},
    "page": {"open": "the command that opens a room's live page on the owner's screen, a list, the address added last (default: the "
                     "box's own: wslview or Windows' url.dll under WSL, open on a Mac, xdg-open with a display)",
             "screen": "the PC's monitor the page tool's open puts the page on under WSL, maximized, the screen woken with no click: "
                       "left, middle, right, primary or its number from the left (default: none, where the browser puts it; the "
                       "tool's `screen` wins)",
             "mac": "the ssh host of the owner's Mac, where the page tool's open with where mac runs `open` on the page's one-time "
                    "address, handed on ssh's stdin (default: [imessage] host, else `ssh` in a mac.json beside an owner room's "
                    "add-on's server, the hands plug-in's, a file of the owner's no one else writes)",
             "address": "yerkd as the owner's Mac reaches it, the box's tailnet or LAN host and its port (http://box.example.ts.net:8787; "
                        "no port: yerkd's), the address an open on the Mac carries and `yerk room page` prints (none: listen's, else "
                        "no page opens on the Mac)",
             "listen": "the box's tailnet or LAN address (100.64.0.1; `tailscale ip -4` prints the tailnet's) where yerkd also serves "
                       "the page's routes, and nothing else, on its port, so the owner's Mac and phone open the page (default: the "
                       "box's own tailnet address, as `tailscale ip -4` prints it at yerkd's start, else loopback alone; 127.0.0.1 "
                       "keeps it on loopback alone); a wildcard, a public address or a host name is refused"},
    "style": {"dialect": "the owner's own filler words (their slang), joined after the neutral ones to the pushback, lead, trailing and name-cut word lists; the package ships none"},
    "watch": {"assistants": "other assistants' Discord user ids: in a channel a room watches, their messages go in its day log under their "
              "own label once they stop changing, never answered and never an instruction; their files, there or in the room's own "
              "channel, are saved in its work/inbox as a person's are"},
    "memory": {"root": "the memory tree, in the private folder (default: its memory/)",
               "also": "more roots the owner's rooms recall, each in the private folder or absolute (a room's own `also` narrows them)",
               "exclude": "paths the index leaves out", "interval": "the least seconds between the index's passes (default 2)",
               "timezone": "the install's IANA zone: the day logs' days, `yerk location day`, a job's default zone (default: the system's)",
               "bench": "the recall bench's question file (default evals/recall-questions.json)",
               "curated": "the curated files, as the recall reads them (default MEMORY.md)",
               "mentions_exclude": "files kept out of mention recall whole",
               "embeddings_key": "the provider key file for meaning search (default secrets/openai.key; none there: keywords only)"},
    "location": {"file": "the phone's fixes, one JSON fix a line (ts_iso, lat, lon, speed_mps), only read",
                 "places": "the named places, {\"places\": [{\"label\", \"lat\", \"lon\", \"radius_m\"}]}, only read",
                 "stale_after": "no place part when the newest fix is older than this (default 3h)"},
    "workers": {"root": "where workers' folders go (default <state>/workers)", "cap_minutes": "minutes before a worker is stopped (default 65)",
                "model": "a worker's model (default: the room's)", "account": "the account workers run on (default: the first not resting)",
                "effort": "a worker's thinking level, one of low, medium, high, xhigh, max (default: the room's live level)",
                "tools": "a worker's built-in tools (default Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, TodoWrite)",
                "permission_mode": "a worker's --permission-mode (default bypassPermissions)",
                "idle_seconds": "seconds a room goes without a human message before a worker's end is told there (default 60)",
                "per_account": "workers at once on one account (default 1)",
                "yerk_tools": "Yerk AI tools a worker gets on its own server: recall and read_memory only (default none)"},
    "gates": {"approvers": "the senders whose word releases a held action (default owner)", "word": "the word that does (default approve)",
              "approve_minutes": "a word older than this releases nothing (default 30)",
              "trash": "the trash command a delete is rewritten into (default: one found on the machine)",
              "off": "the built-in outward-action rules turned off, by name",
              "external": "[gates.external.NAME] tables, outward-action rules of your own: tool, command and what",
              "retired_marks": "marks a reply may not hold in a room without people (an owner room, the machine room), like "
                               "[\"☐\", \"🟦\"]: a final message holding one anywhere in a line outside a fenced block is "
                               "turned back once at Stop, asked for a plain numbered list instead, and the session start's "
                               "room pack quotes the room's earlier lines without them, a colour key line left out whole "
                               "(default none: nothing is turned back or changed; a room with people keeps its own board rule)"},
    "import": {"me": "the name your own texts and mail are filed under",
               "classes": "a chat id's class or room, \"shared\" or \"room:<a room's id>\" (default private)",
               "skip": "chat ids a server import leaves out"},
    "gmail": {"user": "the Gmail address `yerk import gmail` reads and the `email` tool sends from (none: no room emails)",
              "password_file": "a file holding its app password, never printed", "name": "the name an email goes out under (default none)",
              "smtp_host": "the mail server the `email` tool sends through (default smtp.gmail.com)", "smtp_port": "that server's port (default 465)"},
    "imessage": {"host": "the ssh host of the owner's Mac, the `text` tool's way to it (none, with socket: `ssh` and "
                         "`imessage_socket` in a mac.json beside an owner room's add-on's server, the hands plug-in's, else no room texts)",
                 "socket": "the Mac's bridge socket there, serving imsg's JSON-RPC", "socat": "socat on the Mac (default /opt/homebrew/bin/socat)",
                 "command": "a whole command whose stdin and stdout are the bridge, in place of host and socket",
                 "service": "imessage, sms or auto (default auto: iMessage where the person has it, else SMS)",
                 "send_transport": "imsg's own way to drive Messages, auto, bridge or applescript (default: imsg's)",
                 "send_timeout": "seconds a text waits on Messages before it may have gone out (default 60)",
                 "poll_seconds": "seconds between the light checks that copy the owner's newest texts to the box whenever the "
                                 "Mac answers, so the `texts` tool can read them while the Mac sleeps (default 120; 0 turns the "
                                 "copy off and the tool answers only when the Mac is awake)"},
    "needs": {"account": "the account `yerk needs build` runs on (default [curate] account, else the workers' pick)",
              "cap_minutes": "a builder's minutes before it is stopped (default 60)",
              "auto": "the pipeline: a filed need is built, checked, read cold, merged, deployed and live with no person, one at a "
                      "time, and one filed before it and never built is queued on its next tick (default true; false: "
                      "`yerk needs build` by hand, as before)",
              "fix_cap": "fix workers a need's PR gets for red checks or a cold read's fix first before it is held (default 2)",
              "checks_minutes": "minutes a PR's head waits for all its checks before the need is stuck (default 40)",
              "contexts": "how many CI checks a head gets before they count as all in (default 7)",
              "repo": "the GitHub OWNER/NAME of the pipeline's PRs (default: from the daemon tree's origin)",
              "pr_check": "the install's PR checker every need worker runs before its last commit (default: the suite, ruff, mypy)"},
    "curate": {"account": "the one account the night's curation runs on", "logs": "more files in the private folder read as the owner's own stream"},
    "docs": {"fetch": "the doctor's daily fetch of the Claude docs (default true)", "refresh_days": "days between fetches (default 1)",
             "pages": "the pages kept, slug = URL (default config.DOCS_PAGES)",
             "topics": "the install's own topics the rule-shape check reads, name = \"regex\", beside shape.TOPICS",
             "acronyms": "words in capitals the rule-shape check and a rule's filing read as names, not shouting"},
    "deploy": {"unit": "the systemd unit `yerk deploy` restarts (default: the one the running yerkd is in)",
               "tree": "the daemon's checkout (default: the unit's working folder)",
               "tests": "the fast tests run before a restart (default test_scrub, test_budget, test_card and test_style)",
               "quiet_seconds": "how long every room must have had no message either way, a person's or its own, before the restart (default 240)",
               "cap_minutes": "the longest a deploy waits for that quiet (default 60)", "proof_seconds": "how long the proof may take (default 60)"},
    "retention": {"logs": "days the daemon's logs keep", "assembled": "days the rooms' assembled.jsonl keep",
                  "transcripts": "days Claude Code's transcripts keep", "ledger": "days of the ledger's timing rows",
                  "workers": "days a finished worker's folder keeps",
                  "scratch": "days a room's scratch, its work folder but inbox/ and any top folder holding a file named KEEP, keeps",
                  "scratch_mb": "megabytes a room's scratch keeps, its oldest files out first", "trash": "days the trash keeps what left",
                  "scratch_trash": "days the trash keeps a room's scratch"},
    "doctor": {"growth_mb": "the disk growth a day past which the doctor says so (default 500)"},
    "limits": {"uncapped": "true: every character budget in budgets.toml reads as unlimited for what is sent, its number kept as a "
               "measurement `yerk map` reports (docs/budgets.md, Uncapped); compact_window and idle_compact_at are not touched by it (default false)"},
    "backup": {"path": "the folder `yerk backup` writes to, in the private folder or absolute (default yerk-backups beside the private folder)",
               "keep": "how many archives stay, the newest (default 7)", "units": "the systemd units folder backed up and restored (default ~/.config/systemd/user)"},
    "models": {"ALIAS": "one line a model, alias = full id (default: the package's models.toml)"},
}


# [defaults] in rooms.toml: the one tool set every owner room carries (one best way for every
# queue 0 for its"), turn and the last part closes the; case before the next run starts. The example note says the sample
# room's table before the first of the room's own keys named here for it, where rooms.toml.example writes it, else last.
DEFAULT_KEYS = ("allow", "mcp")
BEFORE = {"allow": ("also", "sealed", "thinking_panel", "mcp", "sandbox"), "mcp": ("sandbox",)}


def room_defaults(doc: dict[str, Any]) -> dict[str, Any]:
    """rooms.toml's [defaults], {} for none: allow and mcp.<name> tables only; any other key, or one of another shape,
    stops the start with one line naming it."""
    base = doc.get("defaults", {})
    if not isinstance(base, dict):
        raise SystemExit("rooms.toml: defaults must be a table, [defaults], holding allow and mcp tables")
    for k in (k for k in base if k not in DEFAULT_KEYS):
        raise SystemExit(f"rooms.toml: [defaults] {k}: a default carries allow and mcp tables only; set {k} in each room")
    if not isinstance(base.get("allow", []), list) or not all(isinstance(x, str) for x in base.get("allow", [])):
        raise SystemExit(f'rooms.toml: [defaults] allow must be a list of tool names, like allow = ["mcp__web__web_fetch"], not {base["allow"]!r}')
    if not isinstance(m := base.get("mcp", {}), dict) or not all(isinstance(v, dict) for v in m.values()):
        raise SystemExit("rooms.toml: [defaults] mcp must be a table of servers, like [defaults.mcp.web], each one a table")
    return base


def filled(server: dict[str, Any], name: str) -> dict[str, Any]:
    """A default mcp server as one room runs it: {room} in its args and env values is the room's name."""
    out, put = copy.deepcopy(server), (lambda v: v.replace("{room}", name) if isinstance(v, str) else v)
    if isinstance(out.get("args"), list):
        out["args"] = [put(x) for x in out["args"]]
    if isinstance(out.get("env"), dict):
        out["env"] = {k: put(v) for k, v in out["env"].items()}
    return out


def inherit(name: str, conf: dict[str, Any], base: dict[str, Any]) -> None:
    """An owner room under [defaults] (`base`), in place: the default allow less the room's allow_remove, unless the room
    has its own allow, and each default mcp server, filled, unless the room has its own mcp.<name> table of that name;
    a key of the room's own replaces the default whole, its other servers kept after the defaults'."""
    drop = conf.get("allow_remove", [])
    if not isinstance(drop, list):
        raise SystemExit(f'rooms.toml: {name}: allow_remove must be a list of tool names, like allow_remove = ["mcp__web__web_fetch"], not {drop!r}')
    add: dict[str, Any] = {} if "allow" in conf or "allow" not in base else {"allow": [t for t in base["allow"] if t not in drop]}
    if base.get("mcp") and isinstance(own := conf.get("mcp", {}), dict):        # an mcp of another shape: the daemon names it
        add["mcp"] = {s: own[s] if s in own else filled(v, name) for s, v in base["mcp"].items()}
        add["mcp"] |= {s: v for s, v in own.items() if s not in base["mcp"]}
    items = list(conf.items())
    for k, v in add.items():
        at = next((i for i, (x, _) in enumerate(items) if x in BEFORE[k]), len(items))
        items = [(x, v if x == k else y) for x, y in items] if k in conf else [*items[:at], (k, v), *items[at:]]
    conf.clear()
    conf.update(items)


def rooms(private: Path, text: str | None = None, own: bool = False) -> dict[str, dict[str, Any]]:
    """rooms.toml's rooms, or those of `text` not yet written, each owner room with [defaults] under it (`own`: each as
    written, nothing inherited); a `watch` that a room owns or watches is refused, naming both."""
    doc = tomllib.loads(text) if text is not None else load_toml(private / "rooms.toml")
    rs, base = dict(doc.get("rooms", {})), room_defaults(doc)
    for n, c in rs.items():
        if base and not own and str(c.get("privacy", "owner")) == "owner":
            inherit(n, c, base)
    taken = {str(c.get("channel")): f"which is {n}'s own channel" for n, c in rs.items()}
    for n, w in ((n, str(c["watch"])) for n, c in rs.items() if c.get("watch")):
        if w in taken:
            raise SystemExit(f"rooms.toml: {n} watches {w}, {taken[w]}")
        taken[w] = f"which {n} watches too"
    for n, c in rs.items():
        people(n, c)                                    # a `people` that is not a list of ids stops the start here
        if not isinstance(c.get("watch_audit", False), bool):   # so does a `watch_audit` that is not true or false
            raise SystemExit(f"rooms.toml: {n}: watch_audit must be true or false, like watch_audit = true, not {c['watch_audit']!r}")
    return rs


SHELLS = frozenset({"Bash", "Monitor", "PowerShell", "default"})   # the tools that run a shell ("default" brings Bash)
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")             # the file tools' permission rules (Claude Code consults Edit's)
# What a trusted room's own [rooms.NAME.sandbox] table may add; the rest of its sandbox is the daemon's (Yerkd.wall)
OWN_SANDBOX = {"filesystem": ("denyRead", "denyWrite"), "network": ("strictAllowlist", "allowedDomains", "deniedDomains")}


def trusted(conf: dict[str, Any]) -> bool:
    """`shell = "trusted"`: the room runs the owner rooms' tools, its shell inside the wall the daemon builds (Yerkd.wall)."""
    return conf.get("shell") == "trusted"


def unwalled(conf: dict[str, Any]) -> str:
    """What in a trusted room's rooms.toml would undo the daemon's wall, "" for nothing: a sandbox key past the denies and
    the network allowlist (turning it off, a command run outside it, a folder re-opened or made writable), or an `allow`
    rule for the file tools, which the daemon gives the room's own folders only."""
    box = conf.get("sandbox", {})
    if not isinstance(box, dict):
        return "its sandbox must be a table"
    for k, v in box.items():
        if k in OWN_SANDBOX and isinstance(v, dict):
            bad = [f"{k}.{x}" for x in v if x not in OWN_SANDBOX[k]]
        else:
            bad = [] if k == "autoAllowBashIfSandboxed" or (k == "enabled" and v is True) else [k]
        if bad:
            return f"its sandbox sets {bad[0]}, and only denyRead, denyWrite and the network allowlist are the room's to add"
    rules = [str(r) for r in conf.get("allow") or [] if str(r).split("(")[0] in EDITS]
    return f"allow names {rules[0]}, and the daemon gives the file tools the room's own folders only" if rules else ""


def people(name: str, conf: dict[str, Any]) -> frozenset[int]:
    """The Discord user ids a room answers besides the owner (`people` in rooms.toml); unset or empty, the owner only.
    Anything but a list of ids, or a list on a room whose privacy is owner (it reads the owner's whole memory and recalls the
    owner's people and things) or that has a `read` folder or Bash without shell = "trusted" (each reads past the classes),
    stops the start with one line; so does a `shell` other than "trusted", and a trusted room whose rooms.toml would undo
    the daemon's wall (unwalled)."""
    raw = conf.get("people") or []
    ids = [discord_id(x) for x in raw] if isinstance(raw, list) else []
    if not isinstance(raw, list) or None in ids:
        raise SystemExit(f"rooms.toml: {name}: people must be a list of Discord user ids, like people = [<user id>, <user id>], not {raw!r}")
    if conf.get("shell", "trusted") != "trusted":
        raise SystemExit(f'rooms.toml: {name}: shell must be "trusted" or unset, not {conf["shell"]!r}')
    if ids and str(conf.get("privacy", "owner")) == "owner":
        raise SystemExit(f'rooms.toml: {name}: people needs privacy = "family" or "shared"; a room whose privacy is owner reads the owner\'s whole memory')
    if ids and (SHELLS & set(conf.get("tools") or []) and not trusted(conf) or conf.get("read")):
        raise SystemExit(f'rooms.toml: {name}: people needs tools without "Bash" and no `read`: a shell or a read folder reads past the memory classes; '
                         'shell = "trusted" runs the shell inside the daemon\'s wall')
    if trusted(conf) and (why := unwalled(conf)):
        raise SystemExit(f'rooms.toml: {name}: shell = "trusted" needs the daemon\'s wall whole: {why}')
    return frozenset(i for i in ids if i)


def assistants(conf: dict[str, Any], humans: frozenset[int | None] = frozenset()) -> frozenset[int]:
    """Other assistants' Discord user ids (`[watch] assistants` in yerkd.toml): in a channel a room watches, their messages
    are filed as theirs and never answered; unset, none. Anything but a list of ids, or one of `humans`, stops the start."""
    raw = dict(conf.get("watch") or {}).get("assistants", [])
    ids = [discord_id(x) for x in raw] if isinstance(raw, list) else [None]
    if None in ids:
        raise SystemExit(f'yerkd.toml: [watch] assistants must be a list of Discord user ids, like assistants = ["<user id>"], not {raw!r}')
    if both := humans.intersection(ids):
        raise SystemExit(f"yerkd.toml: [watch] assistants lists {min(i for i in both if i)}, the owner or a person a room answers, who would never be answered")
    return frozenset(i for i in ids if i is not None)


def room_id(name: str, conf: dict[str, Any]) -> str:
    """A room's stable id in the day log: `id` in rooms.toml, else its channel (a Discord channel id
    survives a rename), else its name. The name stays on every line as a label."""
    return str(conf.get("id") or conf.get("channel") or name)


def models(conf: dict[str, Any]) -> dict[str, str]:
    """The models a room may be switched to (alias = full id): [models] in yerkd.toml, which replaces
    the list shipped in the package (models.toml) whole."""
    listed = conf.get("models") or load_toml(table("models.toml")).get("models", {})
    return {str(k).lower(): str(v) for k, v in listed.items()}


def override(state: Path, name: str) -> dict[str, Any]:
    """A room's override: the settings tool's file under the state folder (the daemon's own, never the
    room's; `settings.json` beside it is Claude Code's), {} when there is none."""
    try:
        return dict(json.loads((state / "rooms" / name / "override.json").read_text()))
    except (OSError, ValueError, TypeError):
        return {}


def room_settings(conf: dict[str, Any], state: Path, name: str, listed: dict[str, str]) -> dict[str, tuple[str, str]]:
    """A room's model and effort, each with where it comes from: the room's override over rooms.toml. A value
    off the models list (`listed`) or the levels is ignored, logged once: a stale or planted file never runs."""
    over, out = override(state, name), dict[str, tuple[str, str]]()
    for k, ok, why in (("model", set(listed.values()), "not on the list"), ("effort", set(EFFORTS), "not a level")):
        v = str(over.get(k) or "")
        base = (str(conf[k]), "rooms.toml") if conf.get(k) else ("", "the model's own default")
        if v and v not in ok:
            if (name, k, v) not in _IGNORED:
                _IGNORED.add((name, k, v))
                log.warning("room %s: the override's %s %r is %s; rooms.toml's is used", name, k, v[:80], why)
            base = (base[0], f"{base[1]}; the room's override {v[:80]!r}, {why}: ignored")
        out[k] = (v, "the room's override") if v and v in ok else base
    return out


def pinned(state: Path, name: str, names: list[str]) -> str:
    """The account a room is put on first (the settings tool's `account`, in its override), "" for none. A name off
    accounts.toml's `names` is ignored, logged once, as room_settings does: a stale or planted file never runs."""
    v = str(override(state, name).get("account") or "")
    if v and v not in names and (name, "account", v) not in _IGNORED:
        _IGNORED.add((name, "account", v))
        log.warning("room %s: the override's account %r is not in accounts.toml; the room's order is used", name, v[:80])
    return v if v in names else ""


DIGITS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def account_named(text: str, names: list[str]) -> str:
    """The account `text` names: its name in any case, else its one digit (5 for five, as the box says cli 5); "" for none."""
    t, by = text.strip().lower(), {n.lower(): n for n in names}
    digit = DIGITS[int(t)] if len(t) == 1 and t in "0123456789" else ""     # "²".isdigit() holds and int() refuses it
    return by.get(t) or by.get(digit, "")


def account_and_model(text: str, listed: dict[str, str], names: list[str]) -> tuple[str, str]:
    """/model's one argument as (account, model): an account by name or digit, then a model or nothing ("five", "5 fable",
    "five/claude-fable-5-1"). What reads as a model today (an alias, a full id, default) stays one: ("", text)."""
    t = text.strip()
    if t.lower() in listed or t in listed.values() or t.lower() == "default":
        return "", t
    head, _, rest = t.replace("/", " ", 1).partition(" ")
    return (acct, rest.strip()) if (acct := account_named(head, names)) else ("", t)


# Private files are created WITH their mode, never created then chmod-ed (after the owner's
# tools/safe_write.py): between a create and a chmod the file sits at the umask's mode, and on a box
# with umask 0002 that is a group-writable session table or token file for a moment that only has to
# be short, not zero. O_NOFOLLOW refuses a symlink planted where the file should be. The temp file is
# unique per writer (pid and a counter), so two writers never share one and os.replace is atomic.
_COUNT = 0


def write_private(path: Path, text: str | bytes, mode: int = 0o600) -> Path:
    """Write `text` (or bytes) to `path` through a private temp file of this writer's own, then rename."""
    global _COUNT
    _COUNT += 1
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{_COUNT}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb" if isinstance(text, bytes) else "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def read_secret(path: Path, create: bool = False) -> str:
    """The install's secret for /hook, /control and /mcp (one line, 0600, in the private layer's
    secrets folder); minted once when `create` and the file is missing. Never logged."""
    import secrets as _secrets
    try:
        return path.read_text().strip()
    except OSError:
        if not create:
            return ""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = _secrets.token_hex(24)
    write_private(path, value + "\n")
    return value
