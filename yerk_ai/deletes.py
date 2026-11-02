"""The delete tool: on your approval the daemon deletes paths outside a room's work folder, which a room's own shell
never can (the trash gate keeps a room's deletes to its work folder, and Claude Code's sandbox mounts the rest
read-only). The room names whole paths; the delete-outside gate holds the call with each path shown as what it is, the
owner's approval word releases that exact call once, and the daemon deletes only a call that gate released. For good,
not to the trash: what the owner orders deleted out there is to free the disk, and a trash frees nothing. Never touched,
approval or not (`why`): a path that is not whole, not there or not its own real path (a link or a `..` on its way, so
the path shown is the path deleted), the root, a top folder and the home folder, a mounted disk or a folder holding one,
and Yerk AI's own (its code, its state and private folders, the memory, an account's folder) or a folder holding one.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import words

TOOL = "mcp__yerk__delete"


def mounts(table: Path = Path("/proc/self/mounts")) -> list[Path]:
    """Every mount point but the root (another disk, a Windows drive under /mnt, /proc), its octal escapes read."""
    with contextlib.suppress(OSError):
        rows = [r.split() for r in table.read_text().splitlines()]
        return [Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), r[1])) for r in rows if len(r) > 1 and r[1] != "/"]
    return []


def why(raw: str, own: list[Path], home: Path, mounted: list[Path]) -> str:
    """Why `raw` is never deleted here, "" when it may be, once the owner says the word."""
    p = Path(raw)
    if not p.is_absolute() or p.name in (".", ".."):
        return words.DELETE_NOT_WHOLE.format(given=words.line(raw, 200))
    if (real := Path(os.path.realpath(p.parent)) / p.name) != p:
        return words.DELETE_NOT_REAL.format(path=p, real=real)
    if not os.path.lexists(p):
        return words.DELETE_NOT_THERE.format(path=p)
    if len(p.parts) <= 2 or p == home or p in home.parents:
        return words.DELETE_TOP.format(path=p)
    if any(m == p or p in m.parents for m in mounted):
        return words.DELETE_MOUNTED.format(path=p)
    mine = {q for o in own for q in (o.expanduser().absolute(), o.expanduser().resolve())}
    return words.DELETE_OWN.format(path=p) if any(o == p or o in p.parents or p in o.parents for o in mine) else ""


def on_disk(p: Path) -> int:
    """What `p` takes on disk, a folder's whole tree counted, never through a link."""
    try:
        n = p.lstat().st_blocks * 512
    except OSError:
        return 0
    if p.is_dir() and not p.is_symlink():
        for top, dirs, files in os.walk(p):
            for name in dirs + files:
                with contextlib.suppress(OSError):
                    n += os.lstat(os.path.join(top, name)).st_blocks * 512
    return n


def kind(p: Path) -> str:
    """A path as the owner reads it before the word: a link, a folder, or a file and its size on disk (a folder is never
    walked at the gate, which answers inside the hook's 10 s)."""
    if p.is_symlink():
        return words.DELETE_KIND["link"]
    return words.DELETE_KIND["folder"] if p.is_dir() else words.DELETE_KIND["file"].format(size=words.size(on_disk(p)))


def paths_of(ti: dict[str, Any]) -> list[str]:
    """A call's `paths` as the gate reads them, a lone path as a list of one."""
    v = ti.get("paths")
    return [str(x) for x in (v if isinstance(v, list) else [v] if v else [])]


def preview(ti: dict[str, Any]) -> str:
    """A held delete as the owner reads it before the word: each path whole and what it is, one a line."""
    return "\n".join(f"{p} ({kind(Path(p))})" for p in paths_of(ti))


def run(paths: list[str], own: list[Path], home: Path) -> dict[str, Any]:
    """The delete, path by path, each checked again first: a link or a file unlinked, a folder removed whole (rmtree, which
    never follows a link inside it); what each took on disk counted before and after. {ok, deleted, freed, failed}."""
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    freed, mounted = 0, mounts()
    for raw in paths:
        if no := why(raw, own, home, mounted):
            failed.append({"path": raw, "why": no})
            continue
        p, errors = Path(raw), list[str]()
        before = on_disk(p)
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, onexc=lambda f, at, e: errors.append(f"{at}: {getattr(e, 'strerror', None) or e}"))
            else:
                p.unlink()
        except OSError as e:
            errors.append(f"{p}: {e.strerror or e}")
        freed += before - (on_disk(p) if os.path.lexists(p) else 0)
        if errors:
            failed.append({"path": raw, "why": words.DELETE_PARTLY.format(n=len(errors), first=errors[0])})
        else:
            deleted.append(raw)
    return {"ok": not failed, "deleted": deleted, "freed": words.size(freed), **({"failed": failed} if failed else {})}
