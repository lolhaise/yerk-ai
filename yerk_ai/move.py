"""`yerk room add NAME --from discord:ID`: a Discord chat moves in as a room. Its channel, its voice, its rooms.toml
block (a template in examples/rooms/, owner.toml or person.toml, over a room of the same kind it is made like), its
history through the one import below, its test webhook; then the deploy line and the check. Discord through
discord.py's REST client and the bot token yerkd.toml names, never printed. --dry-run reads and prints, and writes
and sends nothing. It never restarts the daemon: a deploy runs detached from any room. `yerk import` files a chat's
history, or every chat's on the server, through the same import with no room made."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord

from . import context, memory
from .channels.discord import REPLY_MAX, file_type, safe_name
from .config import REPO, budgets, load_toml, people, private_dir, room_id, rooms, state_dir, toml_value, write_private

MAX_FILE = 25_000_000                               # bytes: a larger attachment is named in its line, not saved
HOOK = "yerk-test-sender"                           # the test webhook, named as `yerk setup-room` names it
SINCE = datetime(2015, 1, 1, tzinfo=timezone.utc)   # before Discord: the whole history
CHATS = {0: "text", 2: "voice", 5: "news", 13: "stage", 15: "forum", 16: "media", 10: "thread", 11: "thread", 12: "private thread"}
HOLD = (0, 5, 15, 16)                               # the channels threads hang off
ARCHIVED = (("get_public_archived_threads", False), ("get_private_archived_threads", False),
            ("get_joined_private_archived_threads", True))     # each paged by the last one's archive time, or its id


def made_like(c: dict[str, Any], person: bool) -> bool:
    """A room a new one may copy: on Discord, with people for a person room (its wall is reused), else an owner
    room that is not the machine room."""
    if not str(c.get("channel", "")).startswith("discord:"):
        return False
    return bool(c.get("people")) if person else str(c.get("privacy", "owner")) == "owner" and not c.get("role")


def block_of(t: dict[str, Any], like: dict[str, Any], fill: dict[str, str]) -> dict[str, Any]:
    """The room it is made like, less the template's `drop`, with the template's [set] written over it, filled in."""
    put = {k: [x.format(**fill) for x in v] if isinstance(v, list) else v.format(**fill) if isinstance(v, str) else v
           for k, v in t["set"].items()}
    return {k: v for k, v in like.items() if k not in t["drop"]} | put


def own(t: dict[str, Any], key: str, text: str, was: str, fill: dict[str, str]) -> str:
    """A copy of the other person room's file less that room's own: every line naming its folder, and the persona's
    section under the template's `about` heading (its person), which `about` takes the place of at the end."""
    lines, head = [x for x in text.split("\n") if was not in x], t["about"].split("\n", 1)[0]
    if key != "persona":
        return "\n".join(lines)
    i = lines.index(head) if head in lines else len(lines)
    j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("# ")), len(lines))
    return "\n".join(lines[:i] + lines[j:]).rstrip("\n") + "\n\n" + t["about"].format(**fill)


def toml_lines(head: str, table: dict[str, Any]) -> list[str]:
    """A table the way rooms.toml writes one: its keys, then each sub-table under its own header."""
    keys = [f"{k} = {toml_value(v)}" for k, v in table.items() if not isinstance(v, dict)]
    out = [f"[{head}]", *keys, ""] if keys else []
    for k, v in table.items():
        if isinstance(v, dict):
            out += toml_lines(f"{head}.{k}", v)
    return out


async def history(http: Any, log: memory.DailyLog, room: str, channel: str, roles: dict[str, str], files: Path | None,
                  since: datetime = SINCE, dry: bool = False) -> dict[str, int]:
    """The one import: a channel's or thread's messages since `since`, oldest first, each filed by DailyLog.capture
    under the room's id and class, with the role `roles` gives its author (else other) and the author's handle as
    the daemon files a live line; attachments up to MAX_FILE saved into `files` (0700, each 0600) and named in the
    line, or with no `files` named with their size only. By message id and file name, so a second run only tops up.
    `first` and `last` are the ids of the first and last message read (a Discord id holds its time)."""
    msgs: list[dict[str, Any]] = []
    while page := await http.logs_from(channel, 100, before=msgs[-1]["id"] if msgs else None):
        keep = [m for m in page if datetime.fromisoformat(m["timestamp"]) >= since]
        msgs += keep
        if len(keep) < len(page):
            break
    got = {"messages": len(msgs), "new": 0, "files": 0, "saved": 0, "first": int(msgs[-1]["id"]) if msgs else 0,
           "last": int(msgs[0]["id"]) if msgs else 0}
    for m in reversed(msgs):                        # a page runs newest first
        ref, out = m.get("referenced_message"), []
        said = " ".join(str(ref.get("content") or "").split())[:REPLY_MAX] if ref else ""
        who = (ref["author"].get("global_name") or ref["author"]["username"]) if ref else ""
        text = (f'[a reply to {who}\'s message: "{said}"]\n' if ref else "") + str(m.get("content") or "")
        for i, f in enumerate(m.get("attachments", [])):    # a name the message already used is saved as ID-i-NAME
            again, size = any(x["filename"] == f["filename"] for x in m["attachments"][:i]), int(f.get("size") or 0)
            dest = files / f"{m['id']}-{f'{i}-' if again else ''}{safe_name(f['filename'])}" if files and size <= MAX_FILE else None
            got["files"] += size <= MAX_FILE
            if dest and not dest.exists():
                got["saved"] += 1
                if not dry:
                    dest.parent.mkdir(mode=0o700, exist_ok=True)
                    write_private(dest, await http.get_from_cdn(f["url"]))
            out.append((f"{f['filename']} (saved at {dest})" if dest else f["filename"], file_type(f["filename"], f.get("content_type")), size))
        got["new"] += log.capture(room, m["id"], roles.get(m["author"]["id"], "other"), text, out,
                                  when=datetime.fromisoformat(m["timestamp"]), who=m["author"]["username"], dry=dry)
    return got


async def login(priv: Path, d: dict[str, Any]) -> discord.Client:
    """discord.py's REST client, logged in with the bot token yerkd.toml names, read from its file and never printed."""
    client = discord.Client(intents=discord.Intents.none())
    await client.login((priv / d["token_file"]).read_text().strip())
    return client


async def add(a: argparse.Namespace) -> int:
    """Each step said as it is done, or as it would be with --dry-run; the block loads and passes the daemon's people
    guard before anything changes. A second run finds what the first made, keeps it, and tops the history up."""
    priv, state, dry = private_dir(a.private), state_dir(a.state), a.dry_run
    conf = load_toml(priv / "yerkd.toml")
    d, s = conf["discord"], memory.settings(priv, state, dict(conf.get("memory", {})))
    rs = rooms(priv, own=True)                      # each as written: a new owner room inherits [defaults] under its own name
    t = load_toml(REPO / "examples" / "rooms" / ("person.toml" if a.person else "owner.toml"))
    room = t["room"].format(name=a.room)
    kind, _, old = a.source.partition(":")
    like = a.like or next((n for n in reversed(rs) if n != room and made_like(rs[n], bool(a.person))), "")
    if kind != "discord" or not old.isdigit() or not made_like(rs.get(like, {}), bool(a.person)):
        print("--from takes discord:<channel or thread id>" if kind != "discord" or not old.isdigit() else
              f"--like: {like or 'none'} is no {'person' if a.person else 'owner'} room on Discord to make {room} like",
              file=sys.stderr)
        return 2
    client = await login(priv, d)
    try:
        http: Any = client.http                     # the raw REST calls: payloads exactly as Discord takes them
        await http.logs_from(old, 1)                # the old chat is read first: a 403 here stops it before anything is written
        have = rs.get(room)
        found = [c["id"] for c in await http.get_all_guild_channels(d["guild"]) if c["name"] == room and c["type"] == 0]
        cid = str(have["channel"]).partition(":")[2] if have else found[0] if found else ""
        ows = [o for o in (await http.get_channel(rs[like]["channel"].partition(":")[2]))["permission_overwrites"]
               if int(o["id"]) not in people(like, rs[like])]
        ows += [{"id": str(a.person), "type": 1, "allow": str(t["allow"]), "deny": "0"}] if a.person else []
        fill = {"name": a.room, "channel": cid or "NEW", "layer": str(priv.resolve() / a.room)}
        block = have or block_of(t, rs[like], fill) | ({"people": [a.person]} if a.person else {})
        text, head = (priv / "rooms.toml").read_text(), f"\n# {room}: moved in from {a.source} by `yerk room add`\n"
        if not have:                                # the file it would write loads, with the room as built
            got = rooms(priv, text + head + "\n".join(toml_lines(f"rooms.{room}", block)), own=True)
            if got.get(room) != block:
                raise SystemExit(f"rooms.toml: {room} would not load as it was built")
            context.walls(priv, state, s.scope.roots, got)
        lay, was = context.layer_dir(priv, block), context.layer_dir(priv, rs[like])
        files = {str(block[k]): own(t, k, (was / str(rs[like][k])).read_text(), str(was), fill) for k in t.get("copy", [])}
        files |= {k.format(**fill): v.format(**fill) for k, v in t["files"].items()}
        if a.voice:
            files[str(block["voice"])] = Path(a.voice).expanduser().read_text()
        if a.person and (leak := [k for k, v in files.items() if str(priv.resolve()) in v.replace(str(lay), "")]):
            raise SystemExit(f"{', '.join(leak)}: a path into {priv} outside the room's own layer; a person room holds none")
        print(f"channel: #{room} {cid} is there, kept as it is" if cid else f"channel: {'would make' if dry else 'making'} "
              f"#{room}: POST /guilds/{d['guild']}/channels {json.dumps({'name': room, 'type': 0, 'permission_overwrites': ows})}")
        if not cid and not dry:
            cid = str((await http.create_channel(d["guild"], 0, name=room, permission_overwrites=ows))["id"])
            block["channel"] = f"discord:{cid}"
        for k, v in files.items():
            p = lay / k
            print(f"{k}: there, {'the same' if p.read_text() == v else 'kept as it is'}" if p.is_file() else
                  f"{k}: {'would write' if dry else 'written'}, {len(v):,} characters, in {lay}")
            if not p.exists() and not dry:
                lay.mkdir(mode=0o700, exist_ok=True)
                p.parent.mkdir(mode=0o700, exist_ok=True)
                write_private(p, v)
        if a.person:
            print(f"{block['persona']}: OWNER, PERSON and RELATION in its last section are yours to fill in before they are let in")
        body = "\n".join(toml_lines(f"rooms.{room}", block))
        print(f"rooms.toml: {room} is there, kept as it is" if have else
              f"rooms.toml: {'would append' if dry else 'appending'}, after a timestamped copy in backups/:{head}{body}")
        if not have and not dry:
            (priv / "backups").mkdir(mode=0o700, exist_ok=True)
            write_private(priv / "backups" / f"rooms.toml.pre-{a.room}-{datetime.now():%Y-%m-%d-%H%M%S}", text)
            write_private(priv / "rooms.toml", text + head + body)
        log = memory.DailyLog(s.daily, s.tz, {room: (room_id(room, block), memory.room_class(block))})
        since = datetime.fromisoformat(a.since).replace(tzinfo=s.tz) if a.since else SINCE
        roles = {str(d["owner"]): "owner", **({str(a.person): "person"} if a.person else {})}
        n = await history(http, log, room, old, roles, lay / f"{a.room}-attachments", since, dry)
        print(f"history: {n['messages']} messages from {a.source}, {n['new']} {'to file' if dry else 'filed'}, "
              f"{n['messages'] - n['new']} there already; {n['files']} files up to 25 MB, {n['saved']} {'to save' if dry else 'saved'}")
        hooks = [w for w in await http.channel_webhooks(cid) if w["name"] == HOOK] if cid else []
        hook = hooks[0] if hooks else None if dry else await http.create_webhook(cid, name=HOOK)
        url = priv / "secrets" / f"discord-{a.room}-webhook.url"
        if hook and not dry and not url.exists():
            write_private(url, f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}\n")
    except discord.Forbidden:
        raise SystemExit(f"Discord said no (403): Yerk's bot cannot read {a.source} or may not act here; let it in and run this again") from None
    finally:
        await client.close()
    ids, wid = list(d.get("test_senders", [])), int(hook["id"]) if hook else "NEW"
    print(f"yerkd.toml: test_senders has the test webhook {wid} already" if wid in ids else "yerkd.toml [discord], by "
          f"hand before the deploy:\n- test_senders = {toml_value(ids)}\n+ test_senders = {toml_value([*ids, wid])}")
    b, now = budgets(), datetime.now(timezone.utc)
    start = context.session_start(priv, block, b, rules=s.root / memory.RULES)
    bad = context.violations(start, context.per_message(room, block, "owner", now, now - timedelta(minutes=27), b), b)
    print(f"map: {'; '.join(bad) if bad else 'under budget'} (`yerk map {room}` after the deploy shows each part)")
    print(f"deploy, detached at a quiet moment: setsid nohup yerk deploy > {state}/deploy-{a.room}.log 2>&1 < /dev/null &")
    print(f"warm-up, after it: curl -s -H 'Content-Type: application/json' -d '{{\"content\": \"...\"}}' \"$(cat {url})\"")
    return 1 if bad else 0


async def chats_of(http: Any, guild: str) -> list[dict[str, Any]]:
    """Every chat on the server in its order, each channel's threads under it, newest first: active and archived, public
    and the private ones the bot may list; a listing Discord refuses is left out."""
    chans, found = await http.get_all_guild_channels(guild), (await http.get_active_threads(guild))["threads"]
    for c, (call, by_id) in ((c, x) for c in chans if c["type"] in HOLD for x in ARCHIVED):
        before = None
        with suppress(discord.Forbidden):
            while (page := await getattr(http, call)(c["id"], before, 100))["threads"]:
                found += page["threads"]
                if not page.get("has_more"):
                    break
                last = page["threads"][-1]
                before = last["id"] if by_id else last["thread_metadata"]["archive_timestamp"]
    cat, threads = {c["id"]: c.get("position", 0) for c in chans if c["type"] == 4}, {t["id"]: t for t in found}.values()
    top = sorted((c for c in chans if c["type"] in CHATS), key=lambda c: (cat.get(c.get("parent_id"), -1), c.get("position", 0)))
    return [x for c in top for x in [c, *sorted((t for t in threads if t.get("parent_id") == c["id"]), key=lambda t: -int(t["id"]))]]


async def imports(a: argparse.Namespace) -> int:
    """`yerk import discord:<id>`: a chat's whole history into the day logs through the one import above, with no room
    made; `discord-guild:<id>`: every chat on the server but those a room covers (its channel or watch) and those in
    [import] skip. A line is filed under discord:<id>, labelled with the chat's name, class private, unless [import]
    classes in yerkd.toml gives the id "shared" or "room:<a room's id>" (that room's id and class, so behind its wall,
    its people `person`); a chat a room covers is filed as that room files it. Each chat is said as it is done, one
    that fails said and the rest carried on. --dry-run reads and counts and writes nothing: on a server, the inventory
    (a chat it cannot read counted by Discord's own thread count where there is one); on one chat, also every line.
    `imessage:<chat.db>` and `gmail`: a person's texts and mail, as files (sources.py)."""
    if a.source.partition(":")[0] in ("imessage", "gmail"):
        from . import sources
        return sources.imports(a)
    priv, state, dry = private_dir(a.private), state_dir(a.state), a.dry_run
    conf = load_toml(priv / "yerkd.toml")
    d, rs, s, imp = conf["discord"], rooms(priv), memory.settings(priv, state, dict(conf.get("memory", {}))), conf.get("import", {})
    ids, covers = {room_id(n, c): n for n, c in rs.items()}, {str(c[k]): n for n, c in rs.items() for k in ("channel", "watch") if c.get(k)}
    classes, skip = {str(k): str(v) for k, v in imp.get("classes", {}).items()}, {str(x) for x in imp.get("skip", [])}
    wrong = [k for k, v in classes.items() if v not in ("private", "shared") and not (v.startswith("room:") and v[5:] in ids)]
    kind, _, cid = a.source.partition(":")
    if wrong or kind not in ("discord", "discord-guild") or not cid.isdigit():
        print(f"yerkd.toml [import] classes: {', '.join(wrong)}: each is private, shared or room:<the id of a room in rooms.toml>"
              if wrong else "yerk import takes discord:<channel or thread id>, discord-guild:<guild id>, imessage:<chat.db> or gmail",
              file=sys.stderr)
        return 2
    since = datetime.fromisoformat(a.since).replace(tzinfo=s.tz) if a.since else SINCE
    day = lambda i: f"{discord.utils.snowflake_time(int(i)).astimezone(s.tz):%Y-%m-%d}"     # a Discord id's day, in the install's zone
    if a.files and not dry:
        (priv / "attachments").mkdir(mode=0o700, exist_ok=True)
    walled: set[str] = set()                        # channels the bot cannot read: their threads are not asked for
    client, read, msgs, new, failed = await login(priv, d), 0, 0, 0, 0
    try:
        http: Any = client.http
        chats = await chats_of(http, cid) if kind == "discord-guild" else [await http.get_channel(cid)]
        names = {str(c["id"]): c["name"] for c in chats}
        for c in chats:
            ch, room, pid, thread = str(c["id"]), covers.get(f"discord:{c['id']}"), str(c.get("parent_id") or ""), c["type"] in (10, 11, 12)
            v = classes.get(ch, "private")
            label, rid, cls = (room, room_id(room, rs[room]), memory.room_class(rs[room])) if room else (
                c["name"], *((v[5:], "room") if v.startswith("room:") else (f"discord:{ch}", v)))
            where = f"{'  ' if thread else ''}#{c['name']} {ch} ({CHATS.get(c['type'], 'chat')}{f' in #{names.get(pid, pid)}' if thread else ''})"
            about = (f"covered by {room}" if room else "in [import] skip" if ch in skip else "not covered") + f"; class {cls}" + (
                f" behind {ids[rid]}'s wall" if cls == "room" and rid in ids else "")
            if c["type"] in (15, 16) or kind == "discord-guild" and not dry and (room or ch in skip):
                print(f"{where}: {'a forum, its posts are the threads under it' if c['type'] in (15, 16) else 'left as it is'}; {about}", flush=True)
                continue
            mates = people(ids[rid], rs[ids[rid]]) if cls == "room" and rid in ids else frozenset()
            log, n, why = memory.DailyLog(s.daily, s.tz, {label: (rid, cls)}), None, "its channel is not readable" if pid in walled else ""
            try:
                if not why:
                    n = await history(http, log, label, ch, {str(d["owner"]): "owner", **{str(p): "person" for p in mates}},
                                      priv / "attachments" / f"discord-{ch}" if a.files else None, since, dry)
            except Exception as e:                  # one chat's failure is said, and the rest go on
                status = e.status if isinstance(e, discord.HTTPException) else 0
                why = "Yerk's bot cannot read it (403)" if status == 403 else f"Discord said {status}" if status else f"{type(e).__name__}: {e}"
                walled |= {ch} if status == 403 else set()
            if n is None:
                failed += 1
                est = f"about {c['message_count']:,} messages by Discord's own thread count" if "message_count" in c else "how many unknown"
                last = f", last message {day(c['last_message_id'])}" if c.get("last_message_id") else ""
                print(f"{where}: not read, {why}; {est}; made {day(ch)}{last}; {about}", flush=True)
                continue
            read, msgs, new = read + 1, msgs + n["messages"], new + n["new"]
            span = f", {day(n['first'])} to {day(n['last'])}" if n["messages"] else ""
            saved = f"{n['saved']:,} {'to save' if dry else 'saved'}" if a.files else "named only"
            print(f"{where}: {n['messages']:,} messages{span}, {n['new']:,} {'to file' if dry else 'filed'}, "
                  f"{n['messages'] - n['new']:,} there already; {n['files']:,} files up to 25 MB, {saved}; {about}", flush=True)
            if dry and kind == "discord" and log.shown:
                print("\n".join(log.shown), flush=True)
    except discord.HTTPException as e:
        raise SystemExit(f"Discord said no (403): Yerk's bot cannot read {a.source}; let it in and run this again" if e.status == 403
                         else f"Discord said {e.status} to {a.source}: {e.text}") from None
    finally:
        await client.close()
    if kind == "discord-guild":
        cov, skipped = sum(f"discord:{c['id']}" in covers for c in chats), sum(str(c["id"]) in skip for c in chats)
        print(f"{len(chats)} chats: {read} read, {msgs:,} messages, {new:,} {'to file' if dry else 'filed'}; {failed} not read; "
              f"{cov} covered by a room; {skipped} in [import] skip")
    return 1 if failed else 0
