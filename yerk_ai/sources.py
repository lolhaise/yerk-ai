"""`yerk import imessage:<chat.db>` and `yerk import gmail`: copy your texts and mail into the private folder as plain
files, imports/texts/<person or group>.md and imports/mail/<YYYY-MM>.md, each line a stored message written out by code
(its speaker, the install's time, attachments and app messages as tags, a deleted chat's messages placed by ck_chat_id,
newsletter, a draft and a security mail marked, sent mail kept longer), nothing summed up. A file there carries no
class, so it reads as private: an owner room reads the two folders once [memory] also names them, and no person room
does. By id, a message's guid and a mail's UIDVALIDITY:UID, one a line in imports/texts.ids and imports/mail.ids, so a
second run adds only what is new. A security mail, a security text from a short code and every secret-shaped string are
written as their kind only. The Messages databases are opened read-only; Gmail is read with EXAMINE and BODY.PEEK only,
its app password read from the file [gmail] names and never printed. `yerk import openclaw:<workspace dir>`: a box's
notes as copies of its files, at the end."""
from __future__ import annotations

import argparse
import base64
import bisect
import email
import fcntl
import hashlib
import html
import imaplib
import os
import plistlib
import quopri
import re
import sqlite3
import sys
import time
import tomllib
import unicodedata
from collections import Counter, defaultdict
from contextlib import suppress
from datetime import datetime, timedelta, timezone, tzinfo
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterator

from . import memindex, memory
from .config import REPO, load_toml, private_dir, state_dir, toml_value, write_private

# A security mail, by the words on its subject or anywhere in its fetched text: its body is written as its kind only.
SECURITY = re.compile(r"\b(?:pass)?codes?\b|\bpasswords?\b|\bverif(?:y|ied|ies|ying|ication)\b|\breset\b"
                      r"|\b(?:sign[- ]?in|log[- ]?in|magic) link\b", re.I)
# A code or a credential in other words, on the subject and the line that would be written (an 'OTP for validation'
# mail and two self-sent gateway tokens passed the words above, 2026-10-20)
EXTRA = re.compile(r"\bOTP\b|\bone[- ]time\b|(?-i:\bPIN\b)|\b(?:2FA|MFA)\b|\btwo[- ](?:factor|step)\b|token"
                   r"|\b(?:api|secret|private|access)[ _-]?keys?\b|\bclient[ _-]?secret\b"
                   r"|(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])", re.I)
# A secret's own shape: never written, wherever it is, its kind in its place (the high-precision kinds of the
# 2026-10-21 scrub, a code said as one or after its word, a password's value and a link's token: 91, 82 and 6 lines
# from long-number senders and others passed the rest on the 2026-10-20 texts)
SECRETS = {"anthropic-key": r"sk-ant-[A-Za-z0-9_-]{20,}", "openai-key": r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}",
           "github-token": r"(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}",
           "slack-token": r"xox[abprs]-[A-Za-z0-9-]{10,}", "aws-key-id": r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])",
           "google-api-key": r"AIza[0-9A-Za-z_-]{35}", "google-oauth": r"ya29\.[0-9A-Za-z_-]{20,}",
           "discord-token": r"(?<![A-Za-z0-9])[MNO][A-Za-z\d_-]{23,27}\.[\w-]{6}\.[\w-]{27,40}",
           "jwt": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
           "presigned-url": r"X-Amz-Signature=[0-9a-f]{20,}", "stripe-key": r"(?:sk|rk)_live_[A-Za-z0-9]{20,}",
           "telegram-token": r"(?<!\d)\d{8,10}:AA[A-Za-z0-9_-]{30,}",
           "one-time-code": r"(?<![0-9])\d{4,8}(?![0-9])(?i:(?=[\"”']?\s+is your [\w ]{0,30}(?:code|verification)))",
           "code": r"(?i:\b(?:passcode|code|pin|otp)\b\W{0,3}(?:is\W{0,3})?)\d{4,8}(?!\d)",
           "password": r"(?i:\b(?:password|passwd|passcode|pwd|pw)\b\s*(?:is\s*:?|[:=-])\s*)(?=\S*[0-9!@#$%^&*_+=~-])\S{6,}",
           "link-token": r"(?i:[?&#](?:token|access_token|auth\w*|code|key|sig|signature|reset\w*|magic\w*|otp|login\w*|pwd)=)[\w.%-]{16,}"}
SECRET = re.compile("|".join(f"(?P<{k.replace('-', '_')}>{v})" for k, v in SECRETS.items()))
CODE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")          # a code's shape: in a short code's text, and in a security mail's subject
HELD_TEXT, HELD_MAIL = "[security text: body withheld]", "[security mail: body withheld]"


def scrub(text: str) -> str:
    return SECRET.sub(lambda m: f"[{str(m.lastgroup).replace('_', '-')} withheld]", text)


def put(out: Path, record: Path, files: dict[str, tuple[str, list[tuple[str, str]]]], span: bool, dry: bool) -> tuple[int, int]:
    """Each file's entries, (id, text) oldest first, whose id is not on record: each put after every entry of its minute
    in the file (nothing there moves), the title and count written again (`span`: and the first and last day), the file
    written (0600, its folders 0700), then its ids added to the record. Said a file at a time. (new, there already)"""
    if not dry:
        for d in (out.parent, out):
            d.mkdir(mode=0o700, exist_ok=True)
            d.chmod(0o700)
    lock = None if dry else os.open(record, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if lock is not None:
        fcntl.flock(lock, fcntl.LOCK_EX)            # a second import of the same kind waits, then reads the record anew
    have = set(record.read_text().split()) if record.is_file() else set()
    added = there = 0
    try:
        for name, (title, entries) in files.items():
            new = [(i, t) for i, t in entries if i not in have]
            there += len(entries) - len(new)
            if not new:
                continue
            p, rows = out / f"{name}.md", list[str]()
            if (text := p.read_text() if p.is_file() else "") and "\n\n" not in text:
                print(f"{name}.md: not as an import writes it (no blank line under its title), left as it is", flush=True)
                continue
            for ln in text.split("\n\n", 1)[1].rstrip("\n").split("\n") if text else []:
                if ln.startswith("- ") or not rows:
                    rows.append(ln)
                else:                               # a mail's body line goes with its header line
                    rows[-1] += "\n" + ln
            keys = [r[:18] for r in rows]           # "- YYYY-MM-DD HH:MM"
            for _, t in new if rows else ():
                j = bisect.bisect_right(keys, t[:18])
                rows.insert(j, t)
                keys.insert(j, t[:18])
            rows = rows or [t for _, t in new]      # a new file: the source's own order
            count = f"{len(rows)} messages" + (f", {rows[0][2:12]} to {rows[-1][2:12]}" if span else "")
            print(f"{name}.md: {len(new):,} {'to add' if dry else 'added'}, {len(entries) - len(new):,} there already", flush=True)
            added += len(new)
            if lock is not None:
                write_private(p, "\n".join([title, count, "", *rows]) + "\n")
                os.write(lock, "".join(f"{i}\n" for i, _ in new).encode())
    finally:
        if lock is not None:
            os.close(lock)
    return added, there


# texts: one file a person (their contact name, else their number or address) or group chat, as texts_export.py
# (2026-10-20) wrote them, which was checked against 108,733 rows that hold their text both ways
APPLE = 978307200                                   # 2001-01-01, where Messages' dates count from
SYSTEM = {1: "member added/removed", 2: "group renamed", 3: "group action (left, photo)", 4: "location sharing event",
          5: "other system event"}
LINK = "com.apple.messages.URLBalloonProvider"      # a link preview: its URL is the message's own text


def ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)


def norm(h: str) -> str:
    """A handle as a contact key: an address lowercased, else its last ten digits."""
    h, d = (h or "").strip(), re.sub(r"\D", "", h or "")
    return h.lower() if "@" in h or not d else d[-10:]


def slug(s: str, limit: int = 100) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())
    return s.strip("-")[:limit].strip("-")


def flat(s: str) -> str:
    """A message on one line: each run of line breaks one space."""
    return re.sub(r"\s*[\r\n\u2028\u2029]+\s*", " ", s).strip()


def body_of(blob: bytes) -> str | None:
    """An attributedBody's text: after the NSString class entry, '+', a length (one byte, or 0x81 then two bytes, or 0x82
    then four, little-endian) and that many bytes of UTF-8; None when it cannot be read."""
    for cls in (b"NSString", b"NSMutableString"):
        if (i := blob.find(cls)) < 0 or (plus := blob.find(b"+", i + len(cls), i + len(cls) + 16)) < 0:
            continue
        if (j := plus + 1) >= len(blob):
            return None
        w = {0x81: 2, 0x82: 4}.get(blob[j], 0)
        n, j = int.from_bytes(blob[j + 1:j + 1 + w], "little") if w else blob[j], j + 1 + w
        try:
            return blob[j:j + n].decode() if len(blob[j:j + n]) == n else None
        except UnicodeDecodeError:
            return None
    return None


def contacts_of(db: Path | None) -> tuple[dict[int, str], dict[str, int]]:
    """A contacts database's names by record, and the record (the lowest) each phone number and address belongs to."""
    if db is None:
        return {}, {}
    c, names, keys = ro(db), dict[int, str](), dict[str, int]()
    ent = c.execute("select Z_ENT from Z_PRIMARYKEY where Z_NAME='ABCDContact'").fetchone()[0]
    for pk, fn, ln, org, nick in c.execute("select Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME from ZABCDRECORD where Z_ENT=?", (ent,)):
        if name := " ".join(x.strip() for x in (fn, ln) if x and x.strip()) or (org or "").strip() or (nick or "").strip():
            names[pk] = flat(name)
    for owner, a in [*c.execute("select ZOWNER, ZFULLNUMBER from ZABCDPHONENUMBER"),
                     *((o, n or a) for o, a, n in c.execute("select ZOWNER, ZADDRESS, ZADDRESSNORMALIZED from ZABCDEMAILADDRESS"))]:
        if a and owner in names:
            keys[norm(a)] = min(keys.get(norm(a), owner), owner)
    c.close()
    return names, keys


def text_line(at: datetime, who: str, content: str, short: bool) -> str:
    """A text's line; from a short code (a bank's, a sign-in's), a text with a security word or a code's shape is its kind only."""
    if short and any(r.search(content) for r in (SECURITY, EXTRA, SECRET, CODE)):
        content = HELD_TEXT
    return scrub(f"- {at:%Y-%m-%d %H:%M} {who}: {content}")


def texts(db: Path, contacts: Path | None, me: str, tz: tzinfo, known: dict[str, str]) -> tuple[dict[str, tuple[str, list[tuple[datetime | None, str, str]]]], Counter[str], int]:
    """Every conversation of a Messages database, named as a file (a second chat of one name gets -2), its title and its
    messages oldest first as (time, guid, line); the rows skipped by rule, each by its reason; the rows read. A row is
    placed by its chat, else by ck_chat_id (a chat deleted on the phone: 'service;-;handle' is one person, a group id or
    its hex a group of its own), else by its sender. A conversation keeps the file `known` gives it, and no other takes
    that file, so a -2 never moves when old texts go from the phone; `known` gets every conversation's file."""
    names, keys = contacts_of(contacts)
    m = ro(db)
    handles: dict[int, str] = dict(m.execute("select ROWID, id from handle"))

    def person(raw: str) -> tuple[str, Any]:
        return ("c", keys[norm(raw)]) if norm(raw) in keys else ("h", norm(raw))

    def label(h: int) -> str:
        p = person(handles[h])
        return names[p[1]] if p[0] == "c" else handles[h]

    chats, gids, idents = dict[int, tuple[int, str, str]](), dict[str, int](), dict[str, int]()
    members, atts = defaultdict[int, list[int]](list), defaultdict[int, list[str]](list)
    for rid, style, ident, dname, gid, ogid in m.execute("select ROWID, style, chat_identifier, display_name, group_id, original_group_id from chat"):
        chats[rid] = (style, ident, (dname or "").strip())
        for g in (gid, ogid) if style == 43 else ():
            if g:
                gids.setdefault(g, rid)
        if style == 43 and ident:
            idents.setdefault(ident, rid)
    for cid, hid in m.execute("select chat_id, handle_id from chat_handle_join order by rowid"):
        members[cid].append(hid)
    for mid, tn, fn, hide in m.execute("select j.message_id, a.transfer_name, a.filename, a.hide_attachment from message_attachment_join j "
                                       "join attachment a on a.ROWID=j.attachment_id order by j.message_id, a.ROWID"):
        if not hide and not (name := (tn or os.path.basename(fn or "") or "").strip()).endswith(".pluginPayloadAttachment"):
            atts[mid].append(name)                  # a link preview's image and an app's payload are not listed
    titles = dict(m.execute("select ck_chat_id, group_title from message where item_type=2 and ck_chat_id is not null "
                            "and group_title is not null and group_title!='' order by date"))   # a deleted group's last name
    convs: dict[tuple[Any, ...], dict[str, Any]] = {}

    def conv(key: tuple[Any, ...], kind: str) -> dict[str, Any]:
        return convs.setdefault(key, {"kind": kind, "msgs": [], "raws": Counter(), "rows": set(), "senders": set()})

    def one(raw: str) -> dict[str, Any]:
        cv = conv(("p", *person(raw)), "person")
        cv["raws"][raw] += 1
        return cv

    def group(ck: str) -> str:
        if len(ck) > 36 and re.fullmatch(r"[0-9A-Fa-f]+", ck):
            with suppress(ValueError, UnicodeDecodeError):
                return bytes.fromhex(ck).decode("ascii")
        return ck

    skips: Counter[str] = Counter()
    rows = 0
    for rid, guid, text, ab, hrow, mine, date, item, assoc, balloon, ck, msi, cid in m.execute(
            "select m.ROWID, m.guid, m.text, m.attributedBody, m.handle_id, m.is_from_me, m.date, m.item_type, m.associated_message_type, "
            "m.balloon_bundle_id, m.ck_chat_id, m.message_summary_info, j.chat_id from message m "
            "left join chat_message_join j on j.message_id=m.ROWID order by m.date, m.ROWID"):
        rows, assoc = rows + 1, assoc or 0
        if why := (f"system event: {SYSTEM.get(item, f'item_type {item}')}" if item else "tapback / emoji reaction" if 2000 <= assoc < 3000
                   else "tapback removed" if 3000 <= assoc < 4000 else "sticker placed on a message (reaction)" if assoc == 1000
                   else "poll vote / poll update" if 4000 <= assoc < 5000 else ""):
            skips[why] += 1
            continue
        if cid is not None and cid in chats:
            cv = conv(("g", cid), "group") if chats[cid][0] == 43 else one(chats[cid][1])
            cv["rows"].add(cid)
        elif ck and ";-;" in ck:
            cv = one(ck.split(";-;", 1)[1])
        elif ck:
            g = ck.split(";+;", 1)[1] if ";+;" in ck else group(ck)
            crow = gids.get(g) or idents.get(g)
            cv = conv(("g", crow if crow is not None else "ck:" + g), "group")
            if crow is not None:
                cv["rows"].add(crow)
            else:
                cv["ck"] = ck
        elif hrow:
            cv = one(handles.get(hrow, str(hrow)))
        else:
            skips["no chat, no ck_chat_id and no handle: conversation unknown"] += 1
            continue
        body, failed, empty = text or None, False, False
        if body is None and ab is not None:
            body = body_of(ab)
            failed, empty = body is None, body is not None and not body.strip()
        app, files, tokens = balloon if balloon and balloon != LINK else None, iter(atts.get(rid, [])), []
        pieces = (body or "").replace("\ufffd", "").split("\ufffc")     # U+FFFC: where an attachment sits
        for k, piece in enumerate(pieces):
            if p := flat(piece):
                tokens.append(p)
            if k < len(pieces) - 1 and ((f := next(files, None)) is not None or not app):
                tokens.append(f"[attachment: {f}]" if f is not None else "[attachment]")
        tokens += [f"[attachment: {f}]" for f in files]
        tokens = ([f"[app: {app.split(':')[-1]}]"] if app else []) + ["[text not decodable]"] * failed + tokens
        if not (content := " ".join(tokens)):
            gone = False
            with suppress(Exception):
                gone = bool(msi) and "rp" in plistlib.loads(msi)
            skips["unsent by sender (retracted, no content stored)" if gone else "attributedBody holds an empty string and nothing else is stored"
                  if empty else "no text, attachment or app content stored"] += 1
            continue
        if not mine and cv["kind"] == "group" and hrow:
            cv["senders"].add(hrow)
        who = me if mine else None if cv["kind"] == "person" else (label(hrow) if hrow in handles else None) if hrow else "unknown sender"
        date = date or 0                            # nanoseconds since 2001, or seconds on old rows
        cv["msgs"].append((date, rid, datetime.fromtimestamp((date / 1e9 if date > 1e11 else float(date)) + APPLE, tz=tz), who, content, guid))
    m.close()
    plans = []
    for key, cv in convs.items():
        if not cv["msgs"]:
            continue
        cv["msgs"].sort(key=lambda x: (x[0], x[1]))
        if cv["kind"] == "person":
            raws = sorted(cv["raws"], key=lambda r: (-cv["raws"][r], r))
            lab = names[key[2]] if key[1] == "c" else raws[0]
            s = (slug(lab) if key[1] == "c" else "") or slug(raws[0].replace("@", "-at-")) or "unknown"
            plans.append((cv["msgs"][0][2], key, cv, s, f"# Texts with {lab} ({', '.join(sorted(cv['raws']))})", lab))
            continue
        hrows: list[int] = []
        for crow in sorted(cv["rows"]):
            hrows += [h for h in members.get(crow, []) if h not in hrows]
        hrows += [h for h in sorted(cv["senders"]) if h not in hrows]
        mem = sorted(((label(h), handles[h]) for h in hrows if h in handles), key=lambda x: x[0].lower())
        dname = next((chats[c][2] for c in sorted(cv["rows"]) if chats[c][2]), "") or flat(titles.get(cv.get("ck"), ""))
        lab = dname or ", ".join(x for x, _ in mem) or "unnamed group"
        s = "group-" + (slug(dname or "-".join(x.replace("@", "-at-") for x, _ in mem), 90) or "unnamed")
        mem_txt = ", ".join(x if x == raw else f"{x} {raw}" for x, raw in mem) or "members not stored"
        plans.append((cv["msgs"][0][2], key, cv, s, f"# Texts in group {lab} (members: {mem_txt})", lab))
    out: dict[str, tuple[str, list[tuple[datetime | None, str, str]]]] = {}
    taken = set(known.values())
    for _, key, cv, s, title, lab in sorted(plans, key=lambda p: (p[0], str(p[1]))):
        base, n = s, 2
        while (s in out or s in taken) and str(key) not in known:
            s, n = f"{base}-{n}", n + 1
        s = known.setdefault(str(key), s)
        short = cv["kind"] == "person" and all(len(re.sub(r"\D", "", r)) < 10 and "@" not in r for r in cv["raws"])
        out[s] = (title, [(at, guid, text_line(at, who or lab, content, short)) for _, _, at, who, content, guid in cv["msgs"]])
    return out, skips, rows


# mail: one file a month, as mail_export.py (2026-10-20) wrote them
FIELDS = ("(UID INTERNALDATE X-GM-LABELS BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT LIST-UNSUBSCRIBE CONTENT-TYPE "
          "CONTENT-TRANSFER-ENCODING)])")
WINDOWS = ((4000, 250), (32768, 100))               # bytes of a body fetched, then again for one with no text in them; a batch
SNIP, SNIP_SENT = 400, 1200                         # characters of a mail's text written, of the owner's own sent mail
UID_RX, IDATE_RX = re.compile(rb"UID (\d+)"), re.compile(rb'INTERNALDATE "([^"]+)"')
LABELS_RX, BODY_OPEN = re.compile(rb"X-GM-LABELS \((.*?)\)(?: |$|\))"), re.compile(r"<body\b", re.I)
ZW = re.compile("[\u200b-\u200f\u034f\u00ad\ufeff\u2060\u180e]")


def dec(h: Any) -> str:
    try:
        s = str(make_header(decode_header(h))) if h is not None else ""
    except Exception:
        s = str(h)
    return re.sub(r"\s+", " ", s).strip()


def addr(name: str, a: str) -> str:
    name, a = dec(name).strip('"').strip(), (a or "").strip()
    return f"{name} <{a}>" if name and a and name.lower() != a.lower() else a or name


def fetch(c: imaplib.IMAP4, uids: list[int], items: str) -> list[tuple[bytes, bytes, int | None]]:
    """UID FETCH: each message's prefix, its literal and its UID (read after the literal when it comes there)."""
    typ, data = c.uid("FETCH", ",".join(map(str, uids)), items)
    if typ != "OK":
        raise imaplib.IMAP4.error(f"FETCH said {typ}")
    out = []
    for i, d in enumerate(data):
        if isinstance(d, tuple):
            nxt = data[i + 1] if i + 1 < len(data) else None
            mu = UID_RX.search(d[0]) or (UID_RX.search(nxt) if isinstance(nxt, bytes) else None)
            out.append((d[0], d[1], int(mu.group(1)) if mu else None))
    return out


def dated(h: str | None, tz: tzinfo, now: datetime) -> datetime | None:
    try:
        dt = parsedate_to_datetime(str(h))
        dt = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(tz)
        return dt if dt.year >= 1995 and dt <= now + timedelta(days=2) else None
    except Exception:
        return None


def header(prefix: bytes, lit: bytes, me: str, tz: tzinfo, now: datetime) -> dict[str, Any]:
    """A mail's fields: its Date (else Gmail's INTERNALDATE: a Date missing or before 1995 or days ahead), From, To,
    Subject, whether it is a newsletter (List-Unsubscribe), the owner's own (Gmail's Sent label, or From the owner) or a
    draft, and the type and encoding its body is read with."""
    m, idate = email.message_from_bytes(lit or b"", policy=policy.compat32), None
    if mi := IDATE_RX.search(prefix):
        with suppress(ValueError):
            idate = datetime.strptime(mi.group(1).decode(), "%d-%b-%Y %H:%M:%S %z").astimezone(tz)
    labels = (ml.group(1) if (ml := LABELS_RX.search(prefix)) else b"").decode("utf-8", "replace")
    fname, faddr = parseaddr(m.get("From", ""))
    tos = [a for a in getaddresses(m.get_all("To", [])) if a[1] or a[0]]
    return {"dt": (dated(m.get("Date"), tz, now) if m.get("Date") else None) or idate, "frm": addr(fname, faddr) or "-",
            "to": addr(*tos[0]) if tos else dec(m.get("To", "")) or "-", "subject": dec(m.get("Subject")) or "(no subject)",
            "lu": m.get("List-Unsubscribe") is not None, "sent": bool(re.search(r"\\+Sent\b", labels)) or faddr.strip().lower() == me,
            "draft": bool(re.search(r"\\+Draft\b", labels)), "ct": re.sub(r"\r?\n[ \t]+", " ", m.get("Content-Type", "") or "").strip(),
            "cte": (m.get("Content-Transfer-Encoding", "") or "").strip()}


def cut(s: str, tag: str) -> str:
    """s less each <tag>...</tag>: an unclosed one ends at a <body> after it (a mail that opens <head> and never closes
    it), else at the end, where the partial fetch cut it."""
    o, c, out, i = re.compile(rf"<{tag}\b", re.I), re.compile(rf"</{tag}\s*>", re.I), [], 0
    while m := o.search(s, i):
        out.append(s[i:m.start()])
        if e := c.search(s, m.end()):
            i = e.end()
        elif b := BODY_OPEN.search(s, m.end()):
            i = b.start()
        else:
            return " ".join(out)
    return " ".join([*out, s[i:]])


def strip_html(s: str) -> str:
    for tag in ("head", "script", "style", "title", "noscript"):
        s = cut(s, tag)
    return html.unescape(re.sub(r"<[^>]*$", " ", re.sub(r"(?s)<[^>]*>", " ", re.sub(r"(?s)<!--.*?(?:-->|$)", " ", s))))


def part_text(part: Message) -> str:
    cte, raw = (part.get("Content-Transfer-Encoding", "") or "").strip().lower(), part.get_payload(decode=False)
    if isinstance(raw, list):
        return ""
    data = raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else raw if isinstance(raw, bytes) else b""
    if cte == "base64":                             # a partial fetch ends mid-quantum: what decodes whole is kept
        b = re.sub(rb"[^A-Za-z0-9+/=]", b"", data).rstrip(b"=")
        try:
            data = base64.b64decode(b[:len(b) - len(b) % 4])
        except ValueError:                          # binascii.Error is one
            data = b""
    elif cte == "quoted-printable":
        data = quopri.decodestring(data)
    try:
        return str(data.decode(part.get_content_charset() or "utf-8", "replace"))
    except LookupError:
        return str(data.decode("utf-8", "replace"))


def text_of(ct: str, cte: str, partial: bytes) -> str:
    """The text of a body's first bytes, read under its own type: its first text/plain part, else its first HTML part stripped."""
    head = "MIME-Version: 1.0\r\n" + (f"Content-Type: {ct}\r\n" if ct else "") + (f"Content-Transfer-Encoding: {cte}\r\n" if cte else "")
    plain = htm = None
    for part in email.message_from_bytes(head.encode("utf-8", "replace") + b"\r\n" + (partial or b""), policy=policy.compat32).walk():
        if part.is_multipart() or (part.get("Content-Disposition", "") or "").lower().startswith("attachment"):
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = part_text(part)
        elif part.get_content_type() == "text/html" and htm is None:
            htm = part_text(part)
    txt = plain if plain and plain.strip() else strip_html(htm) if htm else ""
    return re.sub(r"\s+", " ", ZW.sub("", txt).replace("\ufffd", "")).strip()


def body_line(h: dict[str, Any], partial: bytes) -> str | None:
    """A mail's body line from its first bytes: its kind only for a security mail, else its first 400 characters (1,200 of the
    owner's own); None when they hold no text."""
    try:
        txt, bad = text_of(h["ct"], h["cte"], partial), False
    except Exception:
        txt, bad = "", True
    body = txt[:SNIP_SENT if h["sent"] else SNIP]
    if SECURITY.search(txt) or SECURITY.search(h["subject"]) or any(r.search(body) or r.search(h["subject"]) for r in (EXTRA, SECRET)):
        return HELD_MAIL
    return "[body could not be parsed]" if bad else body or None


def mail(g: dict[str, Any], priv: Path, tz: tzinfo, since: datetime | None, have: set[str]) -> tuple[dict[str, tuple[str, list[tuple[datetime | None, str, str]]]], int, int, int]:
    """Gmail's All Mail since `since` (all of it without), the mail not on record fetched: its fields in batches of 500, then
    the body of each that is no newsletter. A batch that fails is said, the connection made again, and its mail left for the
    next run. (each month's file: its title and its mail oldest first as (time, UIDVALIDITY:UID, entry); mail found; read; failed)"""
    user, now = str(g["user"]), datetime.now(tz)

    def connect() -> tuple[imaplib.IMAP4, str]:
        text = (priv / Path(str(g["password_file"])).expanduser()).read_text()
        # the rest of the line after an "App password ...:" label, else a file of one line; Google's four groups are one password
        if not (m := re.search(r"(?im)^\W*app password[^:\n]*:\s*(\S[^\n]*)", text) or re.fullmatch(r"\s*(\S[^\n]*?)\s*", text)):
            raise SystemExit("gmail: no app password in [gmail] password_file (alone on its one line, or after 'App password:'); nothing sent")
        c = imaplib.IMAP4_SSL(str(g.get("host", "imap.gmail.com")), 993, timeout=180)
        try:
            c.login(user, "".join(m.group(1).split()))
        except (imaplib.IMAP4.error, UnicodeError):     # a refusal's text, or a character LOGIN cannot send, is never said
            raise SystemExit(f"gmail: Gmail said no to {user}'s login: [gmail] user and the app password in its file") from None
        boxes = [b.decode("utf-8", "replace") for b in c.list()[1] if isinstance(b, bytes) and b"\\All" in b]
        box = boxes[0].rsplit(' "/" ', 1)[-1].strip() if boxes else '"[Gmail]/All Mail"'
        if c.select(box, readonly=True)[0] != "OK":     # EXAMINE: nothing is marked read
            raise SystemExit(f"gmail: {box} would not open")
        return c, (c.response("UIDVALIDITY")[1][-1] or b"").decode()

    c, v = connect()
    if have and not any(k.startswith(f"{v}:") for k in have):
        raise SystemExit(f"gmail: All Mail's UIDVALIDITY is now {v}, so the UIDs on record name other mail; move imports/mail and its record aside")
    crit = ["SINCE", f"{since - timedelta(days=2):%d-%b-%Y}"] if since else ["ALL"]   # by arrival: two days' margin for its Date
    found = [int(x) for x in c.uid("SEARCH", *crit)[1][0].split()]
    uids = [u for u in found if f"{v}:{u}" not in have]
    hs: dict[int, dict[str, Any]] = {}
    body: dict[int, str | None] = {}
    failed: set[int] = set()
    down = False

    def batches(todo: list[int], size: int, items: str) -> Iterator[tuple[bytes, bytes, int | None]]:
        nonlocal c, down
        for i in range(0, len(todo), size):
            if down:                                    # Gmail did not come back: the rest waits for the next run too
                failed.update(todo[i:])
                return
            try:
                yield from fetch(c, todo[i:i + size], items)
            except (imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError) as e:     # its text may be a server line: masked
                print(f"gmail: {len(todo[i:i + size])} mails not read, left for the next run: {type(e).__name__}: "
                      f"{CODE.sub('[number]', scrub(str(e)))[:200]}", flush=True)
                failed.update(todo[i:i + size])
                with suppress(Exception):
                    c.logout()
                try:
                    c = connect()[0]
                except (imaplib.IMAP4.error, OSError) as again:
                    print(f"gmail: Gmail did not answer again ({type(again).__name__}); the rest is left for the next run", flush=True)
                    down = True

    for p, lit, uid in batches(uids, 500, FIELDS):
        h = header(p, lit, user.lower(), tz, now)
        if uid is not None and (not since or (h["dt"] is not None and h["dt"] >= since)):
            hs[uid] = h
    for size, n in WINDOWS:
        todo = [u for u in sorted(hs, reverse=True) if body.get(u) is None and u not in failed and not (hs[u]["lu"] and not hs[u]["sent"])]
        for _, lit, uid in batches(todo, n, f"(UID BODY.PEEK[TEXT]<0.{size}>)"):
            if uid in hs:
                body[uid] = body_line(hs[uid], lit) or (None if size < WINDOWS[-1][0] else f"[no text body in the first {size} bytes]")
    with suppress(Exception):
        c.logout()
    months: dict[str, list[tuple[str, int, datetime | None, str]]] = defaultdict(list)
    for uid, h in hs.items():
        if uid in failed:
            continue
        b = "[newsletter]" if h["lu"] and not h["sent"] else body.get(uid)
        subject = CODE.sub("[code withheld]", h["subject"]) if b == HELD_MAIL else h["subject"]
        when = f"{h['dt']:%Y-%m-%d %H:%M}" if h["dt"] else "undated"
        line = (f"- {when} From: {h['frm']} | To: {h['to']} | Subject: {subject}" + (" | [draft]" if h["draft"] else "")
                + (f"\n  {b}" if b else ""))
        months[f"{h['dt']:%Y-%m}" if h["dt"] else "undated"].append((h["dt"].isoformat() if h["dt"] else "", uid, h["dt"], scrub(line)))
    return ({k: (f"# Mail {k}", [(at, f"{v}:{uid}", t) for _, uid, at, t in sorted(es)]) for k, es in sorted(months.items())},
            len(found), len(uids), len(failed))


# a box: an OpenClaw workspace's notes as copies of its files in <private>/openclaw/<its name>/<the same path> (the memory
# copy inventory, 2026-10-24): of each part what the index reads, the box tools' own state left, and a line with a secret's
# shape as yerk doctor finds one written with its kind in its place. Named in [memory] also where the box's path was, a copied
# root keeps its cite name (memory.settings names roots by their last parts, in order); outside imports/ a note keeps its
# whole weight, and beside daily/, never in it, a box's day log never meets DailyLog.
PARTS = (("MEMORY.md", "file"), ("USER.md", "file"), ("memory", "notes"), ("wiki", "notes"), ("media/gym", "every"))
LEFT = {"health": "telemetry the box's tools write all day", "jobstate": "the job cycle's state", "finance": "the money run's data",
        "sms": "texts, one-time codes among them", "modelbench": "bench data", "forensics": "never indexed", "tft": "never indexed",
        "index": "never indexed", "archive-offindex": "overwrite backups, kept off the index by design"}    # folders of memory/
TEXTS = ("memory/transcripts/", (".csv", ".txt", ".json", ".html", ".vtt"))    # its text files too: read_memory reads any file of a root
TEXT = (".md", *TEXTS[1])                           # a copy scrubbed a line at a time (a sidecar's .txt too); a picture goes as it is
SUMS = "# yerk import openclaw:"                    # the record's first line, then a line a file: box sha, copy sha, lines scrubbed, path
# On an import the key is the tell, not the value (PR 125's cold read: `token: abc123`, a pin, a password with no digit and an
# Authorization header crossed under the doctor's 16 character floor): a key naming a secret, with `:` or `=` (quoted, starred,
# or the end of a longer key, ACCESS_TOKEN), withholds its line from the key on; prose that only says the word does not.
KEYED = re.compile(r"(?i)(?<![A-Za-z0-9])(?:token|api[ _-]?key|secret|pass(?:word|phrase|wd)|pin|bearer|authorization|cookie"
                   r"|session|private[ _-]?key)[\"'`*_\s]*[:=].*")


def withheld(data: bytes) -> tuple[bytes, list[tuple[int, str]]]:
    """A text with each line holding a shape of yerk doctor's scan (a key, a one-time code, a password or token line), or a
    key naming a secret whatever its value (KEYED, a token line; a password line too runs to the line's end), written with the
    shape's kind in its place until it holds none (a kind is no shape: a line written twice is never one again);
    (line, kind) of each line changed, by its first shape, as the doctor says one."""
    from .doctor import SECRETS
    shapes = {**SECRETS, "password line": re.compile(SECRETS["password line"].pattern + ".*"), "token line": KEYED}
    text = data.decode("utf-8", "surrogateescape")
    if not any(rx.search(text) for rx in shapes.values()):
        return data, []
    lines, hits = text.split("\n"), list[tuple[int, str]]()
    for n, line in enumerate(lines):
        while k := next((k for k, rx in shapes.items() if rx.search(line)), ""):
            hits += [] if hits and hits[-1][0] == n + 1 else [(n + 1, k)]
            line = shapes[k].sub(f"[{k.replace(' ', '-')} withheld]", line)
        lines[n] = line
    return "\n".join(lines).encode("utf-8", "surrogateescape"), hits


def box(ws: Path) -> tuple[dict[str, os.stat_result], dict[str, list[int]], list[str]]:
    """The workspace's files to copy by their path under it, each rule's files and bytes (a stat each, nothing opened), and
    the folders never entered: a dot folder (a session's cookie jar, the box ledger's blobs, git's own) and a secrets one."""
    take, said, shut = dict[str, os.stat_result](), defaultdict[str, list[int]](lambda: [0, 0]), list[str]()
    for part, how in PARTS:
        if how == "file":
            if (p := ws / part).is_file() and not p.is_symlink():
                take[part] = st = p.stat()
                said[f"copy {part}"] = [1, st.st_size]
            continue
        for d, dirs, names in os.walk(ws / part):
            shut += sorted((Path(d) / x).relative_to(ws).as_posix() for x in dirs if x.startswith(".") or x == "secrets")
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "secrets")
            for n in sorted(names):
                p = Path(d, n)
                rel, sub = p.relative_to(ws).as_posix(), p.relative_to(ws / part).parts
                try:
                    st = p.lstat()
                except OSError:                     # gone between the listing and the stat: the next run
                    continue
                rule = (f"skip memory/{sub[0]}/: {LEFT[sub[0]]}" if part == "memory" and len(sub) > 1 and sub[0] in LEFT else
                        "skip a folder the index skips (node_modules, __pycache__, overwrite-backups)" if set(sub[:-1]) & memindex.SKIP_DIRS
                        else "skip a link, as the index does" if p.is_symlink() else "skip a temp or backup copy (*.tmp, *.bak, *.pre-*)"
                        if any(fnmatch(n, x) for x in memindex.SKIP_NAMES) or "\n" in n else
                        f"copy {part}/, every file (a picture beside its seen sidecar)" if how == "every" else
                        f"copy {part}/, the kinds the index reads" if n.endswith(memindex.kinds(ws / part)) else
                        f"copy {TEXTS[0]}, its text files too ({' '.join(TEXTS[1])})" if rel.startswith(TEXTS[0]) and n.endswith(TEXTS[1])
                        else "skip a kind the index does not read")
                said[rule][0] += 1
                said[rule][1] += st.st_size
                if rule.startswith("copy"):
                    take[rel] = st
    return take, said, shut


def folder(top: Path, d: Path) -> None:
    """`d` and each folder above it from `top` down, made 0700 where missing (mkdir's parents take the umask's mode)."""
    for p in (*reversed(d.parents), d):
        if p.is_relative_to(top) and not p.is_dir():
            p.mkdir(mode=0o700)


def listed(line: str, at: int) -> tuple[list[str], str] | None:
    """The array of strings at `at` in a one-line TOML assignment, and what follows it (its comment); None for another value."""
    for j in (j for j, ch in enumerate(line) if ch == "]" and j > at):
        with suppress(tomllib.TOMLDecodeError):
            xs = tomllib.loads(f"x = {line[at:j + 1]}")["x"]
            return ([str(x) for x in xs], line[j + 1:]) if all(isinstance(x, str) for x in xs) else None
    return None


def swaps(priv: Path) -> list[str]:
    """The yerkd.toml and rooms.toml lines that read the copies, to make by hand: in an `also` line each box path with a copy
    becomes its copy in its place, so every root keeps its cite name; a `read` naming one stays while the box runs (yerkd
    refuses a `read` inside the private folder) and loses it when its room retires; a denyRead keeping a shell off a box
    keeps it off the copies too. Every box whose record is in <private>/openclaw counts, so the last run's lines are whole."""
    base = priv.resolve() / "openclaw"
    boxes = {Path(h[len(SUMS):]): f.with_suffix("") for f in sorted(base.glob("*.sums"))
             if (h := f.read_text().partition("\n")[0]).startswith(SUMS)}

    def moved(x: str) -> str:
        p = (priv / Path(x).expanduser()).resolve()
        return next((str(c / p.relative_to(b)) for b, c in boxes.items() if p.is_relative_to(b) and (c / p.relative_to(b)).exists()), x)

    out = []
    for name in ("yerkd.toml", "rooms.toml"):
        for n, ln in enumerate((priv / name).read_text().split("\n") if (priv / name).is_file() else [], 1):
            if not (m := re.match(r"\s*(also|read|denyRead)\s*=\s*\[", ln)) or (got := listed(ln, m.end() - 1)) is None:
                continue
            (xs, rest), deny = got, [x[1:] for x in got[0] if x.startswith("//")]     # a sandbox path: // and the absolute path
            new = [moved(x) for x in xs] if m[1] == "also" else xs
            at = next((i for i, x in enumerate(xs) if x.startswith("//") and any(b.is_relative_to(x[1:]) for b in boxes)), -1)
            if m[1] == "denyRead" and at >= 0 and not any(base.is_relative_to(x) for x in deny):
                new = [*xs[:at + 1], f"/{base}", *xs[at + 1:]]      # beside the box's
            if new != xs:
                out += [f"{name}:{n} now: {ln}", f"{name}:{n} to:  {ln[:m.end() - 1]}{toml_value(new)}{rest}"]
            elif m[1] == "read" and any(moved(x) != x for x in xs):
                out.append(f"{name}:{n}: its `read` stays while the box runs (yerkd refuses one inside the private folder); "
                           "its box path goes when the room retires")
    return ["the lines that read the copies, by hand (never written here), then yerk-index and yerkd restarted:", *out] if out else []


def openclaw(priv: Path, raw: str, since: datetime | None, dry: bool) -> int:
    """`yerk import openclaw:<workspace dir>`, once a workspace: its notes copied in, a rule a line, the scrubbed lines by file
    and line, then the whole and the lines that read the copies. By path and sha256 against the record beside the copy
    (<name>.sums), so a second run writes nothing, a file the box changed is written again (a private temp file renamed, so
    the index never reads half of one, its time the box's) and one it deleted leaves the copy; a part the box no longer has
    is held, not deleted. --since: the files changed from that day on; --dry-run: the inventory, nothing written."""
    t0, ws, mine = time.monotonic(), Path(raw).expanduser().resolve(), priv.resolve()
    out = mine / "openclaw" / ws.name
    record, head = out.parent / f"{ws.name}.sums", f"{SUMS}{ws}"
    old = record.read_text() if record.is_file() else ""
    first = old.partition("\n")[0]
    why = ("openclaw: takes openclaw:<an OpenClaw workspace's folder>" if not raw
           else f"openclaw: {ws} is not a folder" if not ws.is_dir()
           else "openclaw: the workspace and the private folder hold one another" if ws.is_relative_to(mine) or mine.is_relative_to(ws)
           else f"openclaw: {priv} is not a folder" if not dry and not priv.is_dir()
           else "openclaw: --private or YERK_PRIVATE, a private folder of your own: the copy never goes into the repository"
           if not dry and mine.is_relative_to(REPO)
           else f"openclaw/{ws.name}: the copy of {first[len(SUMS):]}, not of {ws}; move it aside and run this again" if old and first != head
           else f"openclaw/{ws.name}: files no import wrote (no {record.name} beside it); move them aside and run this again"
           if not old and out.is_dir() and any(out.iterdir()) else "")
    if why:
        print(why, file=sys.stderr)
        return 2
    have = {r: (b, c, s) for b, c, s, r in (x.split("\t", 3) for x in old.split("\n")[1:] if x.count("\t") >= 3)}
    files, said, shut = box(ws)
    print("\n".join([f"openclaw:{ws} into {out}{' (a dry run: nothing written)' if dry else ''}",
                     *(f"{k}: {n:,} file{'s' * (n != 1)}, {size:,} bytes" for k, (n, size) in sorted(said.items(), key=lambda kv: kv[0][:4] != "copy")),
                     *([f"skip a dot or secrets folder, never entered: {', '.join(shut[:6])}" + (f" and {len(shut) - 6} more" if shut[6:] else "")]
                       if shut else [])]), flush=True)
    keep, hits, new, changed, same, older, failed = dict[str, tuple[str, str, str]](), list[tuple[str, str]](), 0, 0, 0, 0, 0
    for rel, st in files.items():
        if since and st.st_mtime < since.timestamp():
            older += 1
            keep |= {rel: have[rel]} if rel in have else {}
            continue
        try:
            data = (ws / rel).read_bytes()
        except OSError as e:                        # gone since the walk, or not readable: its copy as it is, and the next run
            failed += not isinstance(e, FileNotFoundError)
            print(f"openclaw: {rel} not read, left for the next run: {type(e).__name__}", flush=True)
            keep |= {rel: have[rel]} if rel in have else {}
            continue
        sha, dest, was = hashlib.sha256(data).hexdigest(), out / rel, have.get(rel)
        there = dest.read_bytes() if dest.is_file() else None
        if was and was[0] == sha and there is not None and hashlib.sha256(there).hexdigest() == was[1]:
            same, keep[rel] = same + 1, was         # the same file and its copy as written: not scrubbed again
            hits += [(rel, x) for x in was[2].split(";") if x]
            continue
        body, found = withheld(data) if rel.endswith(TEXT) else (data, [])
        keep[rel] = (sha, hashlib.sha256(body).hexdigest(), ";".join(f"{n} ({k})" for n, k in found))
        hits += [(rel, f"{n} ({k})") for n, k in found]
        if there == body:
            same += 1
            continue
        new, changed = new + (there is None), changed + (there is not None)
        if not dry:
            folder(out.parent, dest.parent)
            write_private(dest, body)
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    tops = {r: next((p for p, _ in PARTS if r == p or r.startswith(p + "/")), "") for r in have if r not in files}
    held = [r for r, p in tops.items() if p and not (ws / p).exists()]
    gone, keep = [r for r in tops if r not in held], keep | {r: have[r] for r in held}
    text = "\n".join([head, *(f"{b}\t{c}\t{s}\t{r}" for r, (b, c, s) in sorted(keep.items()))]) + "\n"
    if not dry:
        folder(out.parent, out.parent)
        out.parent.chmod(0o700)
        for r in gone:
            (out / r).unlink(missing_ok=True)
        if text != old:
            write_private(record, text)
    if since:
        print(f"skip a file older than --since {since:%Y-%m-%d}, its copy as it is: {older:,} files")
    if held:
        print(f"hold {len(held):,} copies under a part the box no longer has (a folder gone is no file deleted): kept")
    print(f"scrub: {len(hits):,} lines in {len({r for r, _ in hits}):,} files, a shape of yerk doctor's written as its kind"
          + "".join(f"\n  {r}:{x}" for r, x in hits))
    print(f"openclaw:{ws.name}: {len(files):,} file{'s' * (len(files) != 1)}, {sum(s.st_size for s in files.values()):,} bytes; {new + changed:,} "
          f"{'to write' if dry else 'written'} ({new:,} new, {changed:,} changed), {same:,} the same, {len(gone):,} the box deleted "
          f"{'to take' if dry else 'taken'} out; {time.monotonic() - t0:.1f} s")
    if not dry and (lines := swaps(priv)):
        print("\n".join(lines))
    return 1 if failed else 0


def imports(a: argparse.Namespace) -> int:
    """`yerk import imessage:<chat.db> [--contacts <db>]` or `yerk import gmail`, through the same import command, --since and
    --dry-run as a Discord chat: each file said as it is written, then the whole; --dry-run reads and counts and writes nothing.
    `openclaw:<workspace dir>`: a box's notes copied in, above."""
    priv = private_dir(a.private)
    conf = load_toml(priv / "yerkd.toml") if (priv / "yerkd.toml").is_file() else {}
    tz, imp = memory.settings(priv, state_dir(a.state), dict(conf.get("memory", {}))).tz, dict(conf.get("import", {}))
    since = datetime.fromisoformat(a.since).replace(tzinfo=tz) if a.since else None
    kind, _, path = a.source.partition(":")
    if kind == "openclaw":
        return openclaw(priv, path, since, a.dry_run)
    db, contacts = Path(path).expanduser(), Path(a.contacts).expanduser() if a.contacts else None
    g, out = dict(conf.get("gmail", {})), priv / "imports" / ("texts" if kind == "imessage" else "mail")
    record, why = out.with_suffix(".ids"), ""
    if kind == "imessage" and (not db.is_file() or contacts and not contacts.is_file()):
        why = f"{'--contacts' if db.is_file() else 'imessage'}: {contacts if db.is_file() else db} is not a file"
    elif kind == "gmail" and not (g.get("user") and g.get("password_file")):
        why = "yerkd.toml [gmail]: user (the address) and password_file (its app password, a path in the private folder) first"
    elif kind == "gmail" and not (pwf := priv / Path(str(g["password_file"])).expanduser()).is_file():
        why = f"[gmail] password_file: {pwf} is not a file"
    elif not record.is_file() and any(out.glob("*.md")):
        why = f"imports/{out.name}: files no import wrote (no {record.name} beside it); move them aside and run it again"
    if why:
        print(why, file=sys.stderr)
        return 2
    have = set(record.read_text().split()) if record.is_file() else set()
    names = out.with_suffix(".names")               # imports/texts.names: each conversation's file, from one run to the next
    if kind == "imessage":
        known = {k: v for k, v in (ln.split("\t", 1) for ln in names.read_text().splitlines() if "\t" in ln)} if names.is_file() else {}
        was = dict(known)
        try:
            got, skips, rows = texts(db, contacts, str(imp.get("me", "me")), tz, known)
        except sqlite3.DatabaseError as e:          # locked, not a database, or a schema without a column read here
            print(f"imessage: {db.name}{f' or {contacts.name}' if contacts else ''} would not read: {e}", file=sys.stderr)
            return 2
        failed, found, n, what = 0, 0, 0, f"{rows:,} rows, {sum(skips.values()):,} skipped by rule ({', '.join(f'{k} {n:,}' for k, n in skips.most_common())})"
    else:
        got, found, n, failed = mail(g, priv, tz, since, have)
        what = f"{found:,} mails found, {n:,} not on record read" + (f", {failed:,} of them not" if failed else "")
    files = {k: (t, [(i, x) for at, i, x in es if not since or (at is not None and at >= since)]) for k, (t, es) in got.items()}
    new, there = put(out, record, files, kind == "imessage", a.dry_run)
    if kind == "imessage" and not a.dry_run and known != was:
        write_private(names, "".join(f"{k}\t{v}\n" for k, v in sorted(known.items())))
    there += found - n                             # mail on record is never fetched
    print(f"{kind}: {what}; {sum(1 for _, es in files.values() if es):,} files, {new:,} {'to add' if a.dry_run else 'added'}, "
          f"{there:,} there already")
    return 1 if failed else 0
