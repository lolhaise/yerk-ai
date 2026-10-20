"""`yerk room add NAME --from discord:ID`: a Discord chat moves in as a room. Its channel, its voice, its rooms.toml
block (a template in examples/rooms/, owner.toml or person.toml, over a room of the same kind it is made like), its
history through the one import below, its test webhook; then the deploy line and the check. Discord through
discord.py's REST client and the bot token yerkd.toml names, never printed. --dry-run reads and prints, and writes
and sends nothing. It never restarts the daemon: a deploy runs detached from any room."""
from __future__ import annotations

import argparse
import json
import sys
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


async def history(http: Any, log: memory.DailyLog, room: str, channel: str, roles: dict[str, str], files: Path,
                  since: datetime = SINCE, dry: bool = False) -> dict[str, int]:
    """The one import: a channel's or thread's messages since `since`, oldest first, each filed by DailyLog.capture
    under the room's id and class, with the role `roles` gives its author (else other) and the author's handle as
    the daemon files a live line; attachments up to MAX_FILE saved into `files` (0700, each 0600) and named in the
    line. By message id and file name, so a second run only tops up."""
    msgs: list[dict[str, Any]] = []
    while page := await http.logs_from(channel, 100, before=msgs[-1]["id"] if msgs else None):
        keep = [m for m in page if datetime.fromisoformat(m["timestamp"]) >= since]
        msgs += keep
        if len(keep) < len(page):
            break
    got = {"messages": len(msgs), "new": 0, "files": 0, "saved": 0}
    for m in reversed(msgs):                        # a page runs newest first
        ref, out = m.get("referenced_message"), []
        said = " ".join(str(ref.get("content") or "").split())[:REPLY_MAX] if ref else ""
        who = (ref["author"].get("global_name") or ref["author"]["username"]) if ref else ""
        text = (f'[a reply to {who}\'s message: "{said}"]\n' if ref else "") + str(m.get("content") or "")
        for i, f in enumerate(m.get("attachments", [])):    # a name the message already used is saved as ID-i-NAME
            again = any(x["filename"] == f["filename"] for x in m["attachments"][:i])
            dest, size = files / f"{m['id']}-{f'{i}-' if again else ''}{safe_name(f['filename'])}", int(f.get("size") or 0)
            got["files"] += size <= MAX_FILE
            if size <= MAX_FILE and not dest.exists():
                got["saved"] += 1
                if not dry:
                    files.mkdir(mode=0o700, exist_ok=True)
                    write_private(dest, await http.get_from_cdn(f["url"]))
            out.append((f"{f['filename']} (saved at {dest})" if size <= MAX_FILE else f["filename"],
                        file_type(f["filename"], f.get("content_type")), size))
        got["new"] += log.capture(room, m["id"], roles.get(m["author"]["id"], "other"), text, out,
                                  when=datetime.fromisoformat(m["timestamp"]), who=m["author"]["username"], dry=dry)
    return got


async def add(a: argparse.Namespace) -> int:
    """Each step said as it is done, or as it would be with --dry-run; the block loads and passes the daemon's people
    guard before anything changes. A second run finds what the first made, keeps it, and tops the history up."""
    priv, state, dry = private_dir(a.private), state_dir(a.state), a.dry_run
    conf = load_toml(priv / "yerkd.toml")
    d, rs, s = conf["discord"], rooms(priv), memory.settings(priv, state, dict(conf.get("memory", {})))
    t = load_toml(REPO / "examples" / "rooms" / ("person.toml" if a.person else "owner.toml"))
    room = t["room"].format(name=a.room)
    kind, _, old = a.source.partition(":")
    like = a.like or next((n for n in reversed(rs) if n != room and made_like(rs[n], bool(a.person))), "")
    if kind != "discord" or not old.isdigit() or not made_like(rs.get(like, {}), bool(a.person)):
        print("--from takes discord:<channel or thread id>" if kind != "discord" or not old.isdigit() else
              f"--like: {like or 'none'} is no {'person' if a.person else 'owner'} room on Discord to make {room} like",
              file=sys.stderr)
        return 2
    client = discord.Client(intents=discord.Intents.none())
    await client.login((priv / d["token_file"]).read_text().strip())
    try:
        http: Any = client.http                     # the raw REST calls: payloads exactly as Discord takes them
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
            got = rooms(priv, text + head + "\n".join(toml_lines(f"rooms.{room}", block)))
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
        try:
            n = await history(http, log, room, old, roles, lay / f"{a.room}-attachments", since, dry)
        except discord.Forbidden:
            raise SystemExit(f"history: Yerk's bot cannot read {a.source}; let it in there and run this again, it tops up") from None
        print(f"history: {n['messages']} messages from {a.source}, {n['new']} {'to file' if dry else 'filed'}, "
              f"{n['messages'] - n['new']} there already; {n['files']} files up to 25 MB, {n['saved']} {'to save' if dry else 'saved'}")
        hooks = [w for w in await http.channel_webhooks(cid) if w["name"] == HOOK] if cid else []
        hook = hooks[0] if hooks else None if dry else await http.create_webhook(cid, name=HOOK)
        url = priv / "secrets" / f"discord-{a.room}-webhook.url"
        if hook and not dry and not url.exists():
            write_private(url, f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}\n")
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
