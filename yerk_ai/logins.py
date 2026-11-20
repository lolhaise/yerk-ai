"""Saves a website login you hand over with the /login slash command as a secret file only yerkd writes, unseen by any room.

Before it (need 127, 2026-11-20), a room asked to keep a new site's login had no way: its shell cannot write the secrets
folder, the browser's keep saves only a key a page shows, and a password typed through a tool call lands in the room's kept
transcript. /login, the owner's own slash command, comes to the daemon as an interaction, never as a message, so no room's
session sees it: the login is written as NAME.login in the private folder's secrets/ (JSON site, user and password, 0600 in
a 0700 folder), and the browser plug-in types its user or its password by that name, on its own site only (hands, type with"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from yerk_ai import words

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")      # the browser plug-in's name rule for a kept file (hands' LOGIN_NAME)
HOST = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
MOST = 512                                          # characters of a user or a password; a longer one is a paste gone wrong


def site(raw: str) -> str:
    """The host a login is for, from what the owner typed: an address or a bare host, its scheme, path, port and a leading
    www. dropped, so a sign-in page on the site or a subdomain of it matches; "" when it is no public host name."""
    s = raw.strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s).split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".")
    s = s[4:] if s.startswith("www.") else s
    return s if HOST.fullmatch(s) else ""


def path(private: Path, name: str) -> Path:
    return private / "secrets" / f"{name}.login"


def save(private: Path, name: str, where: str, user: str, password: str) -> dict[str, Any]:
    """Write the login, the owner's own word, so replacing one of the same name is theirs to do (a changed password). The
    line back names the login, its site and its user, never the password, nor its length."""
    name, host = name.strip().lower().removesuffix(".login"), site(where)
    if not NAME.fullmatch(name):
        return {"ok": False, "line": words.LOGIN_NAME}
    if not host:
        return {"ok": False, "line": words.LOGIN_SITE}
    if not user.strip() or not password or any(len(v) > MOST or "\n" in v or "\r" in v for v in (user, password)):
        return {"ok": False, "line": words.LOGIN_EMPTY}
    folder = private / "secrets"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    file, tmp = path(private, name), folder / f".{name}.login.tmp"
    if file.is_symlink() or (file.exists() and not file.is_file()):
        return {"ok": False, "line": words.LOGIN_LINK.format(name=name)}
    again = file.exists()
    body = json.dumps({"site": host, "user": user.strip(), "password": password, "saved": date.today().isoformat()})
    tmp.unlink(missing_ok=True)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as f:
        f.write(body + "\n")
    os.replace(tmp, file)                            # whole or not at all, never a half-written login
    return {"ok": True, "again": again, "site": host, "user": user.strip(), "name": name,
            "line": (words.LOGIN_REPLACED if again else words.LOGIN_SAVED).format(name=name, site=host, user=user.strip())}
