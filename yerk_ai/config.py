"""Where Yerk AI keeps things and how it reads them: its own tables such as budgets.toml, the private folder, the state folder."""
from __future__ import annotations

import json
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger("yerk.config")
PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parent
EXAMPLE_PRIVATE = REPO / "examples" / "private"
EFFORTS = ("low", "medium", "high", "xhigh", "max")      # Claude Code 2.1.301's --effort levels (its --help)
_IGNORED: set[tuple[str, str, str]] = set()             # (room, key, value) of an override already logged


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
