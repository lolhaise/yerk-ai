"""Writes Yerk AI's reference docs in docs/ from the code itself (`yerk docs own`): commands.md from the parsers of the
commands pyproject.toml installs, tools.md from the MCP server's tools as a room is handed them, settings.md from the
loaders' key tables, budgets.md from budgets.toml, hooks.md from the events a room's settings register, units.md from
units/. Nothing here reads the private layer, the state folder, the clock or the machine, so a tree writes the same bytes
test_owndocs fails the build when docs/ is not what this writes."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import re
import tomllib
from pathlib import Path
from typing import Any, cast

from .config import DEFAULT_KEYS, REPO, ROOM_WORDS, YERKD_WORDS, inherit, table

DOCS = REPO / "docs"


def head(title: str, src: str, about: str) -> list[str]:
    return [f"# {title}", "", about, "", f"Written by `yerk docs own` from {src}; change those and run it again, never this page.", ""]


def para(out: list[str], *texts: str) -> None:
    """Paragraphs after `out`, a blank line before each and after the last."""
    for t in (t for t in texts if t):
        out += ([""] if out and out[-1] else []) + [t, ""]


def usage(a: argparse.Action) -> str:
    """One argument as it is typed."""
    mv = a.metavar if a.metavar is not None else a.dest.upper()
    mv = " ".join(mv) if isinstance(mv, tuple) else str(mv) if a.option_strings else str(mv).upper()
    if a.nargs == 0:
        return ", ".join(a.option_strings)
    arg = f"[{mv} ...]" if a.nargs == "*" else f"[{mv}]" if a.nargs == "?" else mv
    return f"{', '.join(a.option_strings)} {arg}".strip()


def said(a: argparse.Action) -> str:
    bits = [str(a.help)] if a.help and a.help != argparse.SUPPRESS else []
    if a.choices is not None and not isinstance(a, argparse._SubParsersAction):
        bits.append("one of " + ", ".join(map(str, a.choices)))
    if a.nargs != 0 and a.default not in (None, "", [], 0, argparse.SUPPRESS) and "default" not in (a.help or ""):
        bits.append(f"default {a.default}")
    if isinstance(a, argparse._AppendAction):
        bits.append("repeat for each")
    return "; ".join(bits)


def parser_lines(p: argparse.ArgumentParser, depth: int) -> list[str]:
    out = [f"- `{usage(a)}`" + (f": {w}" if (w := said(a)) else "")
           for a in p._actions if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))]
    for sp in (a for a in p._actions if isinstance(a, argparse._SubParsersAction)):
        helps = {c.dest: str(c.help or "") for c in sp._choices_actions}
        for name, child in sp.choices.items():
            args = " ".join(usage(a) for a in child._actions if not a.option_strings)
            para(out, f"{'#' * (depth + 1)} {child.prog}{' ' + args if args else ''}", helps.get(name, ""))
            out += parser_lines(child, depth + 1)
    return out


def commands() -> str:
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["scripts"]
    out = head("Commands", "the parsers in " + ", ".join(f"yerk_ai/{v.split(':')[0].split('.')[-1]}.py" for v in scripts.values()),
               "The commands pyproject.toml installs, each command and flag with the help its parser gives it.")
    for name, target in scripts.items():
        p = importlib.import_module(target.split(":")[0]).parser()
        para(out, f"## {name}", str(p.description or ""))
        out += parser_lines(p, 2)
    return "\n".join(out).rstrip() + "\n"


def kind(s: dict[str, Any]) -> str:
    if "anyOf" in s:
        return " or ".join(kind(x) for x in s["anyOf"])
    return f"list of {kind(s['items'])}" if s.get("type") == "array" and "items" in s else str(s.get("type", "any"))


def tools() -> str:
    from . import mcp_server, workers

    async def none(payload: dict[str, Any], auth: dict[str, str]) -> dict[str, Any]:
        return {}
    listed = asyncio.run(mcp_server.build(cast(Any, None), 0, none).list_tools())
    out = head("Tools", "yerk_ai/mcp_server.py (build) and workers.WORKER_TOOLS",
               f"The {len(listed)} tools Yerk AI's own MCP server hands every room's Claude Code, preloaded, as mcp__yerk__NAME. Each "
               "one's description is the text the model reads, indentation dropped. A worker gets none of them unless [workers] "
               f"yerk_tools in yerkd.toml names some, and then only {', '.join(workers.WORKER_TOOLS)}, on its own server.")
    for t in listed:
        props, need = t.inputSchema.get("properties", {}), set(t.inputSchema.get("required", []))
        para(out, f"## {t.name}", "```text\n" + inspect.cleandoc(t.description or "") + "\n```")
        out += [f"- `{k}`: {kind(v)}, " + ("required" if k in need else f"default {json.dumps(v.get('default'))}") for k, v in props.items()]
    return "\n".join(out).rstrip() + "\n"


def settings() -> str:
    from . import jobs, memory, retention, words
    out = head("Settings", "yerk_ai/config.py (ROOM_WORDS, DEFAULT_KEYS, YERKD_WORDS) and the tables beside the code that reads "
               "them (words.SETTINGS, memory.described, jobs.CONF, jobs.KEYS, retention.DAYS and CAP)",
               "Every key the loaders take from the private folder's rooms.toml, yerkd.toml and jobs.toml, in plain words with its "
               "default. rooms.toml.example and yerkd.toml.example are commented copies with example values.")
    para(out, "## rooms.toml", "One `[rooms.NAME]` table a room; `yerk doctor` names any other key as a typo.")
    out += [f"- `{k}`: {w}" for k, w in sorted(ROOM_WORDS.items())]
    para(out, "### [defaults]", f"Keys: {', '.join(DEFAULT_KEYS)}. " + " ".join(inspect.cleandoc(inherit.__doc__ or "").split()))
    found: dict[str, dict[str, str]] = {}
    for r in words.described(None) + memory.described(None):
        sec, key = r["key"].split(".", 1)
        found.setdefault(sec, {})[key] = f"{r['what']} (default {r['default']}; choices: {', '.join(map(str, r['choices']))}; {r['reloads']})"
    found.setdefault("jobs", {}).update(jobs.CONF)
    days = {**retention.DAYS, **retention.CAP}
    secs = {s: {**{k: w + (f" (default {days[k]})" if s == "retention" and k in days else "") for k, w in ks.items()}, **found.pop(s, {})}
            for s, ks in YERKD_WORDS.items()}
    para(out, "## yerkd.toml", "The daemon's settings, in the private folder beside rooms.toml, read when yerkd starts.")
    for s, ks in {**secs, **found}.items():
        para(out, f"### [{s}]")
        out += [f"- `{k}`: {w}" for k, w in ks.items()]
    para(out, "## jobs.toml", "One `[jobs.NAME]` table a scheduled job; `yerk jobs sync` makes its systemd timer.")
    out += [f"- `{k}`: {w}" for k, w in jobs.KEYS.items()]
    return "\n".join(out).rstrip() + "\n"


def code_split(line: str) -> tuple[str, str]:
    """A TOML line as (its value part, its trailing comment), a # inside quotes kept."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i].rstrip(), line[i + 1:].strip()
    return line.rstrip(), ""


def budgets() -> str:
    """budgets.toml as it reads: a comment block says what follows it, a table or a key, and a dated line starts a paragraph;
    the comments before the first table are the file's own."""
    out = head("Budgets", "budgets.toml", "The size limits in budgets.toml, table by table, each line with the comments that say what it is.")
    block: list[list[str]] = []
    lines, i, first = table("budgets.toml").read_text().splitlines(), 0, True
    while i < len(lines):
        s, i = lines[i].strip(), i + 1
        if s.startswith("#"):
            text = s.lstrip("#").strip()
            if not block or not text or re.match(r"\d{4}-\d\d-\d\d", text):
                block.append([])
            block[-1] += [text] if text else []
            continue
        if not s:
            if first and block:                     # the file's own comments end at its first blank line
                para(out, *(" ".join(b) for b in block))
                block, first = [], False
            continue
        value, note = code_split(s)
        while value.count("[") > value.count("]") and i < len(lines):
            value, i = value + " " + code_split(lines[i].strip())[0], i + 1
        if s.startswith("["):
            para(out, f"## {s}")
        para(out, *(" ".join(b) for b in block))
        block = []
        if not s.startswith("["):
            out.append(f"- `{value}`" + (f": {note}" if note else ""))
    return "\n".join(out).rstrip() + "\n"


def hooks() -> str:
    from . import hooks as h
    reg = h.settings_for("URL", "HEADER")
    if set(reg) != set(h.EVENTS):
        raise ValueError(f"hooks.EVENTS and settings_for name different events: {sorted(set(reg) ^ set(h.EVENTS))}")
    out = head("Hooks", "yerk_ai/hooks.py (EVENTS, settings_for)", " ".join(inspect.cleandoc(h.settings_for.__doc__ or "").split()))
    for ev, (entry, *_) in reg.items():
        when = f", on {entry['matcher'].replace('|', ', ')}" if entry.get("matcher") else ""
        para(out, f"## {ev}", f"Timeout {entry['hooks'][0]['timeout']} s{when}. It {h.EVENTS[ev]}.")
    return "\n".join(out).rstrip() + "\n"


def unit(path: Path) -> list[str]:
    out, prose = [f"## {path.name}", ""], []
    for s in (x.strip() for x in path.read_text().splitlines()):
        if s.startswith("#"):
            prose.append(s.lstrip("#").strip())
            continue
        if prose and s:
            para(out, " ".join(prose))
            prose = []
        if s.startswith("["):
            para(out, s)
        elif "=" in s:
            k, _, v = s.partition("=")
            out.append(f"- {k}: " + (v.strip('"').partition("=")[0] if k == "Environment" else f"`{v}`"))
    para(out, " ".join(prose))
    return out


def units() -> str:
    files = sorted(p for p in (REPO / "units").iterdir() if p.suffix in (".service", ".timer"))
    timers = {p.stem: [x.strip() for x in p.read_text().splitlines() if x.startswith("On")] for p in files if p.suffix == ".timer"}
    out = head("Units", "units/", "The systemd user units Yerk AI ships, each service with when it runs, then each file line by "
               "line; an Environment line shows its variable's name only. Copy a unit to ~/.config/systemd/user/ to install it.")
    out += [f"- {p.name}: " + (f"{p.stem}.timer, {'; '.join(timers[p.stem])}" if p.stem in timers else "no timer, it runs while it is enabled")
            for p in files if p.suffix == ".service"]
    for p in files:
        out += ["", *unit(p)]
    return "\n".join(re.sub(r"\n{3,}", "\n\n", "\n".join(out)).splitlines()).rstrip() + "\n"


def pages() -> dict[str, str]:
    return {"commands.md": commands(), "tools.md": tools(), "settings.md": settings(), "budgets.md": budgets(),
            "hooks.md": hooks(), "units.md": units()}


def stale(root: Path = DOCS, got: dict[str, str] | None = None) -> list[str]:
    got = got if got is not None else pages()
    return [n for n, t in got.items() if not (root / n).is_file() or (root / n).read_text() != t]


def main(dry: bool = False, root: Path = DOCS) -> int:
    got = pages()
    old = stale(root, got)
    if dry:
        print("\n".join(f"docs/{n}: stale against the code; `yerk docs own` writes it" for n in old) or "docs: every page is what the code writes")
        return 1 if old else 0
    root.mkdir(exist_ok=True)
    for n in old:
        (root / n).write_text(got[n])
    print(f"docs: {len(old)} written, {len(got) - len(old)} the same" + "".join(f"; written: {n}" for n in old))
    return 0
