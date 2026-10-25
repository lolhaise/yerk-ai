"""Installs and removes add-ons. An add-on is an ability in a repository of its own, whose addon.toml is an abilities.toml entry
with `repo`, `version`, `size` (its own ceiling in lines of code) and `install`; the aim is a core Yerk that still knows every
ability, where each lives on GitHub, and adds one easily. `yerk add` fetches it into <state>/addons/NAME, checks it, installs its
package, writes its server and allow lines under rooms.toml's [defaults] and its tables into yerkd.toml (marked blocks,
read back through the loaders that start yerkd), links its units and runs its fast tests, a line per step; `yerk remove`
step and the test reads it back as plain text. With one, more line for the check while a second entry
waits.in the queue for its. Turn and the last part closes the case, before the next run."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

from . import words
from .config import load_toml, rooms, table, toml_value, write_private

DIR, MANIFEST = "addons", "addon.toml"
INSTALL = ("package", "module", "mcp", "allow", "settings", "room_keys", "units", "budgets", "words", "tests")
CODE, SKIP = (".py", ".mjs", ".js"), ("tests", "node_modules", ".git", ".venv", "__pycache__", ".pytest_cache")
TOTALS = {"session_start": ("total",), "per_message": ("typical", "cap")}     # a part's raise raises its section's totals
BEGIN, END = "# add-on {name}: yerk add wrote these lines, yerk remove {name} takes them out\n", "# add-on {name}: end\n"
DEFAULTS = "\n[defaults]                    # the add-ons' tools, written by yerk add\nallow = []\n"
ALLOW = re.compile(r"^\[defaults\][^\n]*\n(?:(?!\[)[^\n]*\n)*?[ \t]*allow\s*=\s*\[", re.M)    # [defaults]' own allow = [
NAME = re.compile(r"[A-Za-z0-9_-]+")      # an add-on's name: one folder under addons/, a bare TOML key, the NAME in mcp__NAME__
Run =Callable[..., "subprocess.CompletedProcess[str]"]


def units_dir() -> Path:
    """The user's systemd units, where `systemctl --user` looks ($XDG_CONFIG_HOME/systemd/user)."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "systemd" / "user"


def entry(folder: Path) -> tuple[str, dict[str, Any]]:
    """An add-on folder's one manifest entry, (name, entry)."""
    tab = load_toml(folder / MANIFEST).get("abilities", {})
    if len(tab) != 1:
        raise ValueError(f"{MANIFEST} holds {len(tab)} [abilities.NAME] entries, not one")
    return next(iter(tab.items()))


def installed(state: Path) -> dict[str, dict[str, Any]]:
    """Every add-on under <state>/addons by its own entry; a folder whose manifest does not read is not one."""
    out = {}
    for f in sorted((state / DIR).glob(f"*/{MANIFEST}")):
        with contextlib.suppress(OSError, ValueError):
            name, ab = entry(f.parent)
            out.update({name: ab} if name == f.parent.name else {})
    return out


def importable(module: str) -> bool:
    importlib.invalidate_caches()
    return importlib.util.find_spec(module) is not None


def servers(private: Path) -> dict[str, Any]:
    with contextlib.suppress(OSError, ValueError):
        return dict(load_toml(private / "rooms.toml").get("defaults", {}).get("mcp", {}))
    return {}


def missing(name: str, ab: dict[str, Any], private: Path, state: Path) -> list[str]:
    """What of an installed add-on is not in place, read from the install: its package importable, its server under
    rooms.toml's [defaults], each unit linked; [] when it all is."""
    ins = ab.get("install", {})
    out = [] if not ins.get("module") or importable(str(ins["module"])) else ["its package"]
    out += [] if not ins.get("mcp") or name in servers(private) else ["its server"]
    return out + [f"unit {Path(u).name}" for u in ins.get("units", []) if not (units_dir() / Path(u).name).exists()]


def size(folder: Path) -> int:
    """Its code's lines as the core's ceiling counts them (each newline), its tests and fetched dependencies aside."""
    return sum(f.read_bytes().count(b"\n") for f in folder.rglob("*")
               if f.suffix in CODE and f.is_file() and not set(SKIP) & set(f.relative_to(folder).parts))


def check(name: str, ab: dict[str, Any], folder: Path, known: dict[str, Any] | None) -> str:
    """Why `yerk add` refuses this entry, "" for nothing: a name that is not one plain word, a field missing, an install key no
    step reads or a table that is not one, a tool that is not its own server's, a budget line on a total, outside the parts or
    not a whole number, a version other than the registry's, a path outside it, a words file or tests not there, its code over its own ceiling."""
    ins = ab.get("install")
    if not NAME.fullmatch(name):
        return f"its name {name} is not one plain word: letters, digits, - and _ only"
    if miss := [k for k in ("what", "tier", "needs", "free_path", "version", "size", "install") if k not in ab]:
        return f"its entry has no {', '.join(miss)}"
    if not isinstance(ins, dict) or (bad := sorted(set(ins) - set(INSTALL))):
        return f"install {', '.join(bad) if isinstance(ins, dict) else 'is not a table'}: no step reads it ({', '.join(INSTALL)})"
    if bad := [k for k in ("mcp", "settings", "budgets") if not isinstance(ins.get(k, {}), dict)]:
        return f"install {bad[0]} is not a table"
    if known is not None and "install" not in known and "repo" not in known:
        return f"{name} is a core ability"
    if bad := [t for t in ins.get("allow", []) if not str(t).startswith(f"mcp__{name}__")]:
        return f"allow {bad[0]} is not a tool of its own server (mcp__{name}__...)"
    if bad := [k for k in ins.get("budgets", {}) if k.split(".")[0] not in TOTALS or k.rsplit(".", 1)[-1] in sum(TOTALS.values(), ())]:
        return f"budget line {bad[0]}: an add-on adds session_start and per_message parts, never a total"
    if bad := [f"{k} = {n!r}" for k, n in ins.get("budgets", {}).items() if type(n) is not int or n < 0]:
        return f"budget line {bad[0]}: not a whole number"
    if known and known.get("version") and str(known["version"]) != str(ab["version"]):
        return f"it is version {ab['version']} and the registry names {known['version']}"
    if not ins.get("words") or not ins.get("tests"):
        return "install names no words file or no tests, and an add-on brings both"
    if bad := [str(p) for p in [ins["words"], *ins["tests"], *ins.get("units", [])] if not (folder / str(p)).resolve().is_relative_to(folder.resolve())]:
        return f"{bad[0]} is outside its folder"        # absolute, or out through .. or a link: remove would leave it linked
    if bad := [str(p) for p in [ins["words"], *ins["tests"]] if not (folder / str(p)).exists()]:
        return f"no {bad[0]} in it"
    if (n := size(folder)) > int(ab["size"]):
        return f"its code is {n:,} lines, over its own ceiling of {int(ab['size']):,}"
    return ""


def mark(name: str, body: str) -> str:
    return "\n" + BEGIN.format(name=name) + body + END.format(name=name)


def unmark(text: str, name: str) -> str:
    return re.sub(rf"\n?{re.escape(BEGIN.format(name=name))}.*?{re.escape(END.format(name=name))}", "", text, count=1, flags=re.S)


def tables(t: dict[str, Any], head: str = "") -> str:
    return "".join(f"[{head}{k}]\n" + "".join(f"{x} = {toml_value(y)}\n" for x, y in v.items()) for k, v in t.items())


def with_server(text: str, name: str, server: dict[str, Any], tools: list[str]) -> str:
    """rooms.toml with the add-on's server as [defaults.mcp.NAME] and its tools first in [defaults] allow (a [defaults]
    of the add-ons' own when the file has none)."""
    if not server:
        return text
    text += "" if re.search(r"^\[defaults\]", text, re.M) else DEFAULTS
    if not (m := ALLOW.search(text)):
        raise SystemExit("rooms.toml: [defaults] has no allow list: give it allow = [] and add again")
    return text[:m.end()] + "".join(f'"{t}", ' for t in tools) + text[m.end():] + mark(name, tables({name: server}, "defaults.mcp."))


def without_server(text: str, name: str, tools: list[str]) -> str:
    text = unmark(text, name)
    for t in tools:
        text = text.replace(f'"{t}", ', "", 1)
    return text.replace(DEFAULTS, "", 1) if not re.search(r"^\[defaults\.mcp\.", text, re.M) else text


def write(path: Path, text: str) -> None:
    """A private-folder file rewritten in place with its own mode; one left empty by a remove is deleted."""
    if not text:
        return path.unlink(missing_ok=True)
    write_private(path, text, stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)


def pip(verb: str, what: str) -> list[str]:
    """uv's pip when uv is on the PATH (a uv venv has no pip of its own), else this Python's pip; into this venv."""
    if shutil.which("uv"):
        return ["uv", "pip", verb, "--quiet", "--python", sys.executable, what]
    return [sys.executable, "-m", "pip", verb, "--quiet", *(["-y"] if verb == "uninstall" else []), what]


def tests(state: Path) -> list[tuple[str, list[str], Path]]:
    """(name, its fast tests, its folder) of every installed add-on, for `yerk add` and `yerk deploy`."""
    return [(n, [str(t) for t in ab.get("install", {}).get("tests", [])], state / DIR / n) for n, ab in installed(state).items()]


def with_lines(b: dict[str, Any], state: Path) -> dict[str, Any]:
    """budgets.toml as the gate reads it here: each installed add-on's own lines added (a line it names raised by its
    number, a new one made) and its section's totals raised by the same, so the parts still add up (decision 12)."""
    out = copy.deepcopy(b)
    for ab in installed(state).values():
        for key, n in ab.get("install", {}).get("budgets", {}).items():
            if (path := key.split("."))[0] not in TOTALS or len(path) < 2:
                continue                                    # check() refuses it at `yerk add`; a folder put there by hand
            *path, last = path
            t = out
            for p in path:
                t = t.setdefault(p, {})
            t[last] = t.get(last, 0) + int(n)
            for total in TOTALS[path[0]]:
                out[path[0]][total] = out[path[0]].get(total, 0) + int(n)
    return out


def room_keys(state: Path) -> frozenset[str]:
    """The rooms.toml keys the installed add-ons read: known keys to the doctor while they are installed."""
    return frozenset(str(k) for ab in installed(state).values() for k in ab.get("install", {}).get("room_keys", []))


def add(source: str, private: Path, state: Path, dry: bool = False, run: Run = subprocess.run, say: Callable[[str], Any] = print) -> int:
    """`yerk add SOURCE [--dry-run]`: 0 added (or, dry, it would be), 1 refused or its tests failed (nothing kept), 2 no
    such add-on."""
    W, reg = words, dict(load_toml(table("abilities.toml"))["abilities"])
    src = str(reg[source]["repo"]) if source in reg and reg[source].get("repo") else source
    if not Path(src).expanduser().is_dir() and not re.match(r"^(\w+://|git@)", src):
        say(f"{W.NOT_DONE} " + W.ADD_NONE.format(source=source, names=", ".join(n for n, x in reg.items() if x.get("repo")) or "none"))
        return 2
    scratch = state / DIR / f".fetch-{os.getpid()}"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    try:
        if Path(src).expanduser().is_dir():
            shutil.copytree(Path(src).expanduser(), scratch, ignore=shutil.ignore_patterns(*SKIP[1:]))
        elif (r := run(["git", "clone", "--quiet", "--depth", "1", src, str(scratch)], capture_output=True, text=True)).returncode:
            say(f"{W.NOT_DONE} " + W.NOT_ADDED.format(why=W.no_paths((r.stderr or "git clone failed").strip().splitlines()[-1])))
            return 1
        name, ab = entry(scratch)
        ins, folder = ab.get("install", {}), state / DIR / name
        say(W.step(W.ADD_STEP, "fetch", False, src=src))
        rtext = (private / "rooms.toml").read_text()
        ytext = (private / "yerkd.toml").read_text() if (private / "yerkd.toml").is_file() else ""
        server = {k: [fill(x, folder, private, state) for x in v] if isinstance(v, list) else fill(v, folder, private, state)
                  for k, v in ins.get("mcp", {}).items()} if isinstance(ins, dict) else {}
        server.update({"command": sys.executable} if server.get("command") == "python" else {})
        why = check(name, ab, scratch, reg.get(name)) or ("it is installed: yerk remove it first" if folder.exists() else "")
        why = why or next((f"yerkd.toml has [{t}]" for t in ins.get("settings", {}) if t in tomllib.loads(ytext)), "")
        why = why or (f"rooms.toml has [defaults.mcp.{name}]" if name in servers(private) else "")
        links = [(units_dir() / Path(u).name, folder / u) for u in ins.get("units", [])]
        why = why or next((f"a unit {x.name} is there already" for x, _ in links if x.exists() or x.is_symlink()), "")
        rnew = with_server(rtext, name, server, list(ins.get("allow", []))) if not why else rtext
        rooms(private, text=rnew)                                   # the loaders that start yerkd refuse it here, else nothing
        ynew = ytext + mark(name, tables(ins["settings"])) if ins.get("settings") and not why else ytext
        tomllib.loads(ynew)
    except (OSError, ValueError, KeyError, TypeError, SystemExit) as e:
        why = W.no_paths(str(e).removeprefix("rooms.toml: ")) or type(e).__name__
    if why:
        shutil.rmtree(scratch, ignore_errors=True)
        say(f"{W.NOT_DONE} " + W.NOT_ADDED.format(why=why))
        return 1
    say(W.step(W.ADD_STEP, "check", False, name=name, version=ab["version"], lines=f"{size(scratch):,}", size=f"{int(ab['size']):,}"))
    if not dry:
        scratch.rename(folder)

    def do(key: str, act: Callable[[], Any] = lambda: None, **f: Any) -> None:
        if not dry:
            act()
        say(W.step(W.ADD_STEP, key, dry, **f))
    try:
        if ins.get("package"):
            do("package", lambda: run(pip("install", str(folder)), check=True, capture_output=True, text=True), package=ins["package"])
        if server:
            do("server", lambda: write(private / "rooms.toml", rnew), name=name, n=len(ins.get("allow", [])))
        if ins.get("settings"):
            do("tables", lambda: write(private / "yerkd.toml", ynew), tables=", ".join(f"[{t}]" for t in ins["settings"]))
        for x, t in links if not dry else []:
            x.parent.mkdir(parents=True, exist_ok=True)
            x.symlink_to(t)
        if links:
            do("units", units=", ".join(x.name for x, _ in links), into=units_dir())
        if ins.get("budgets"):
            do("budgets", lines=", ".join(f"{k} +{n:,}" for k, n in ins["budgets"].items()))
        do("words", words=ins["words"])
        if not dry and (t := run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--tb=line", *ins["tests"]],
                                 cwd=folder, capture_output=True, text=True)).returncode:
            raise RuntimeError(W.ADD_TESTS_FAILED.format(said=W.no_paths((t.stdout.strip().splitlines() or ["no output"])[-1])))
        say(W.step(W.ADD_STEP, "tests", dry, said=", ".join(ins["tests"])))
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, TypeError, KeyError) as e:   # any step's: each undone
        say(f"{W.NOT_DONE} " + W.NOT_ADDED.format(why=W.no_paths(str(e))))
        remove(name, private, state, say=lambda _: None, run=run)
        return 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    say(f"{W.PLAIN} {W.ADD_DRY}" if dry else f"{W.DONE} " + W.ADDED.format(name=name, version=ab["version"]))
    return 0


def fill(v: Any, folder: Path, private: Path, state: Path) -> Any:
    """{addon}, {private} and {state} in a server's args as this install has them; {room} stays for each room's own."""
    return v.replace("{addon}", str(folder)).replace("{private}", str(private.resolve())).replace("{state}", str(state)) \
        if isinstance(v, str) else v


def remove(name: str, private: Path, state: Path, dry: bool = False, run: Run = subprocess.run, say: Callable[[str], Any] = print) -> int:
    """`yerk remove NAME [--dry-run]`: each step of `yerk add` undone, the folder to the trash; 0 removed, 2 not installed."""
    W, folder = words, state / DIR / name
    if not (folder / MANIFEST).is_file():
        say(f"{W.NOT_DONE} " + W.NOT_REMOVED.format(name=name))
        return 2
    ins, y, trash = entry(folder)[1].get("install", {}), private / "yerkd.toml", state / "trash" / time.strftime("%Y-%m-%d/%H%M%S") / DIR

    def do(key: str, act: Callable[[], Any] = lambda: None, **f: Any) -> None:
        if not dry:
            act()
        say(W.step(W.REMOVE_STEP, key, dry, **f))
    if (rnew := without_server(rtext := (private / "rooms.toml").read_text(), name, list(ins.get("allow", [])))) != rtext:
        do("server", lambda: write(private / "rooms.toml", rnew), name=name)
    if y.is_file() and (ynew := unmark(ytext := y.read_text(), name)) != ytext:
        do("tables", lambda: write(y, ynew), tables=", ".join(f"[{t}]" for t in ins.get("settings", {})))
    if links := [x for u in ins.get("units", []) if (x := units_dir() / Path(u).name).is_symlink()
                 and x.resolve().is_relative_to(folder.resolve())]:
        for x in links if not dry else []:
            x.unlink()
        do("units", units=", ".join(x.name for x in links))
    if ins.get("package"):                              # a package that will not go is said, and the rest still goes
        failed = not dry and run(pip("uninstall", str(ins["package"])), capture_output=True, text=True).returncode
        say(f"{W.NOT_DONE} " + W.NOT_UNINSTALLED.format(package=ins["package"]) if failed else W.step(W.REMOVE_STEP, "package", dry, package=ins["package"]))
    if not dry:
        trash.mkdir(parents=True, exist_ok=True)
    do("folder", lambda: shutil.move(folder, trash / name))
    say(f"{W.PLAIN} {W.ADD_DRY}" if dry else f"{W.DONE} " + W.REMOVED.format(name=name))
    return 0
