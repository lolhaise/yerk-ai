"""The email tool: sends one email in your name from your own Gmail, once you approve it. It goes to one or more addresses, with files
from the sending room's own folders attached, over Gmail's SMTP on the app password `yerk import gmail` reads ([gmail] user
and password_file, so the owner's Gmail is named once). The gate shows the owner the email first (gates.py, the email-out
rule): who it goes to exactly as the send has them, the subject, each file by its name and size, and the words; the daemon
sends only a call that gate released. A connection or a login that failed, or a sender, recipients or message Gmail refused,
is not sent; a line that dropped once the message was under way may have gone, and nothing here ever sends it again.
"""
from __future__ import annotations

import contextlib
import mimetypes
import re
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any

from . import words

TOOL = "mcp__yerk__email"
FILES = 18 * 1024 * 1024     # the files' bytes in one email: Gmail takes 25 MB a message, and base64 makes 18 MB about 24
WAIT = 60.0                  # seconds each step waits on Gmail
SMTP = smtplib.SMTP_SSL      # the way to Gmail; the tests put a fake one here
ADDRESS = re.compile(r"[^@\s,;:<>()\[\]\"]+@[^@\s,;:<>()\[\]\"]+\.[^@\s,;:<>()\[\]\"]+")


def app_password(text: str) -> str:
    """The app password in a [gmail] password_file: the rest of the line after an "App password ...:" label, else a file of one
    line; Google's four groups are one password. "" when there is none."""
    m = re.search(r"(?im)^\W*app password[^:\n]*:\s*(\S[^\n]*)", text) or re.fullmatch(r"\s*(\S[^\n]*?)\s*", text)
    return "".join(m.group(1).split()) if m else ""


def password(g: dict[str, Any], priv: Path) -> str:
    """[gmail]'s app password, read in the daemon and never printed; "" when user, password_file or the password is missing."""
    if not g.get("user") or not g.get("password_file"):
        return ""
    try:
        return app_password((priv / Path(str(g["password_file"])).expanduser()).read_text())
    except (OSError, UnicodeError):
        return ""


def normal(ti: dict[str, Any]) -> dict[str, Any]:
    """A call as the gate holds and releases it and the tool sends it: every field, a missing one at its default."""
    return {"to": [str(x) for x in ti.get("to") or []], "subject": str(ti.get("subject") or ""), "text": str(ti.get("text") or ""),
            "files": [str(x) for x in ti.get("files") or []]}


def why(c: dict[str, Any], may: Callable[[Path], bool]) -> str:
    """Why the email `c` (normal) cannot go, "" when it can, its files judged by `may` (the room's own folders)."""
    if not c["to"]:
        return words.EMAIL_NO_TO
    if bad := next((t for t in c["to"] if not ADDRESS.fullmatch(t.strip())), None):
        return words.EMAIL_NO_ADDRESS.format(given=words.line(words.no_paths(bad), 60))
    if not c["subject"].strip() and not c["text"].strip():
        return words.EMAIL_EMPTY
    if "\n" in c["subject"] or "\r" in c["subject"]:
        return words.EMAIL_SUBJECT
    for f in c["files"]:
        p = Path(f).expanduser()
        if not p.is_absolute() or not may(p):
            return words.EMAIL_FILE_OUTSIDE.format(name=Path(f).name)
        if not p.is_file():
            return words.EMAIL_FILE_NONE.format(name=p.name)
    if (n := sum(Path(f).expanduser().stat().st_size for f in c["files"])) > FILES:
        return words.EMAIL_FILES_BIG.format(size=words.sizes(n, FILES)[0], limit=words.sizes(n, FILES)[1])
    return ""


def preview(c: dict[str, Any], sender: str) -> str:
    """A held email as the owner reads it before the word: who it is from and goes to exactly as the send has them, its subject,
    each file by its name and size (the path stays in the ledger), then its words."""
    def one(f: str) -> str:
        p = Path(f).expanduser()
        return f"{p.name} ({words.size(p.stat().st_size)})" if p.is_file() else p.name
    files = [f"attached: {', '.join(map(one, c['files']))}"] if c["files"] else []
    return "\n".join([f"from: {sender}", f"to: {', '.join(t.strip() for t in c['to'])}", f"subject: {c['subject']}", *files, "", c["text"]])


def message(g: dict[str, Any], c: dict[str, Any]) -> EmailMessage:
    """The email as it goes: from [gmail] user (under [gmail] name when set), plain text, each file attached by its name."""
    user = str(g["user"])
    msg = EmailMessage()
    msg["From"] = formataddr((str(g["name"]), user)) if g.get("name") else user
    msg["To"] = ", ".join(t.strip() for t in c["to"])
    msg["Subject"], msg["Date"] = c["subject"], formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=user.rpartition("@")[2] or None)
    msg.set_content(c["text"])
    for f in c["files"]:
        p = Path(f).expanduser()
        main, _, sub = (mimetypes.guess_type(p.name)[0] or "application/octet-stream").partition("/")
        msg.add_attachment(p.read_bytes(), maintype=main, subtype=sub, filename=p.name)
    return msg


def send(g: dict[str, Any], priv: Path, c: dict[str, Any]) -> dict[str, Any]:
    """The email `c` (normal) from [gmail] user: {"ok": True, "to", "id", "files"} once Gmail took it (with `refused`, the
    addresses it would not take, when some), else {"ok": False, "error", "maybe_sent"}, true when it may have gone out."""
    def no(why: str) -> dict[str, Any]:
        return {"ok": False, "maybe_sent": False, "error": words.EMAIL_NOT_SENT.format(why=why)}
    if not (pw := password(g, priv)):
        return no(words.EMAIL_NO_SETUP)
    try:
        msg = message(g, c)
    except OSError as e:
        return no(words.EMAIL_FILE_NONE.format(name=Path(e.filename or "").name or "a file"))
    try:
        s = SMTP(str(g.get("smtp_host", "smtp.gmail.com")), int(g.get("smtp_port", 465)), timeout=WAIT, context=ssl.create_default_context())
    except (OSError, smtplib.SMTPException):
        return no(words.EMAIL_AWAY)
    try:
        try:
            s.login(str(g["user"]), pw)
        except (smtplib.SMTPException, UnicodeError):     # its text is a server line, never said
            return no(words.EMAIL_LOGIN)
        except OSError:
            return no(words.EMAIL_AWAY)
        try:
            refused = s.send_message(msg)
        except (smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError):
            return no(words.EMAIL_REFUSED)
        except (OSError, smtplib.SMTPException):           # the line dropped once the message was under way
            return {"ok": False, "maybe_sent": True, "error": words.EMAIL_MAYBE}
    finally:
        with contextlib.suppress(OSError, smtplib.SMTPException):
            s.quit()
    return {"ok": True, "to": [t.strip() for t in c["to"]], "id": str(msg["Message-ID"]), "files": [Path(f).name for f in c["files"]],
            **({"refused": sorted(refused)} if refused else {})}
