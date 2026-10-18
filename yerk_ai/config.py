"""Where Yerk AI keeps things and how it reads them: its own tables such as budgets.toml, the private folder, the state folder."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parent
EXAMPLE_PRIVATE = REPO / "examples" / "private"


def table(name: str) -> Path:
    """budgets.toml or failures.toml: inside the installed package, else at the repo root."""
    for base in (PACKAGE, REPO):
        if (base / name).is_file():
            return base / name
    raise FileNotFoundError(name)


def load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text())


def flag(value: Any, default: bool = False) -> bool:
    """A yes-or-no setting, read strictly: a TOML true or false, or the word "true" or "false" in
    quotes (any case); any other value is the default, so a quoted "false" turns nothing on."""
    if isinstance(value, bool):
        return value
    word = value.strip().lower() if isinstance(value, str) else ""
    return word == "true" if word in ("true", "false") else default


def budgets() -> dict[str, Any]:
    return load_toml(table("budgets.toml"))


def private_dir(arg: str | None = None) -> Path:
    """--private, then $YERK_PRIVATE, then the fake example layer shipped with the repo."""
    p = arg or os.environ.get("YERK_PRIVATE")
    return Path(p).expanduser() if p else EXAMPLE_PRIVATE


def state_dir(arg: str | None = None) -> Path:
    """--state, then $YERK_STATE, then $XDG_STATE_HOME/yerk (~/.local/state/yerk)."""
    p = arg or os.environ.get("YERK_STATE")
    d = Path(p).expanduser() if p else Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "yerk"
    d.mkdir(parents=True, exist_ok=True)
    return d


def rooms(private: Path) -> dict[str, dict[str, Any]]:
    return dict(load_toml(private / "rooms.toml").get("rooms", {}))


def room_id(name: str, conf: dict[str, Any]) -> str:
    """A room's stable id in the day log: `id` in rooms.toml, else its channel (a Discord channel id
    survives a rename), else its name. The name stays on every line as a label."""
    return str(conf.get("id") or conf.get("channel") or name)


# Private files are created WITH their mode, never created then chmod-ed (after the owner's
# tools/safe_write.py): between a create and a chmod the file sits at the umask's mode, and on a box
# with umask 0002 that is a group-writable session table or token file for a moment that only has to
# be short, not zero. O_NOFOLLOW refuses a symlink planted where the file should be. The temp file is
# unique per writer (pid and a counter), so two writers never share one and os.replace is atomic.
_COUNT = 0


def write_private(path: Path, text: str, mode: int = 0o600) -> Path:
    """Write `text` to `path` through a private temp file of this writer's own, then rename."""
    global _COUNT
    _COUNT += 1
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{_COUNT}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
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
