"""`yerk doctor`: the daily check of the install's shape and the crash repair (Layer 11, claim 6, decision 40;
units/yerk-doctor.timer, after retention's), quiet when all is well, a plain line per finding, each run kept in
<state>/doctor.json for the morning diff. A hard power-off zeroes the line a file was writing, so on yerkd's start and
log, assembled log or bound transcript with a NUL byte is copied first (a day log to backups/doctor-<day>/ beside the private
beside the private layer, the rest to <state>/trash/<day>/doctor/), then each such line loses its NULs and is kept when
it still reads, else left blank in a day log (recall cites file:line) or dropped from a JSONL file."""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Any, Callable

from . import hooks, memindex, memory, retention, status, words
from .accounts import load_accounts, token_info
from .config import REPO, ROOM_KEYS, load_toml, rooms, write_private
from .context import layer_dir
from .ledger import Ledger
from .mentions import Builder

DAY, LAST, GROWTH_MB = 86400.0, "doctor.json", 500      # [doctor] growth_mb in yerkd.toml over GROWTH_MB


def reads(line: bytes, daylog: bool) -> bool:
    """A line with its NULs out still says something: a day-log line, its header or an attachment line; a JSON value."""
    try:
        text = line.decode()
        if daylog:
            return bool(memory.parse_line(text) or memindex.DAY_HEAD.match(text) or text.startswith("  - attachment: "))
        json.loads(text)
        return True
    except ValueError:                                      # not UTF-8, or not JSON
        return False


def mend(path: Path, daylog: bool, backup: Path, dry: bool) -> list[int]:
    """The lines of `path` that hold a NUL byte (from 1), mended unless `dry`, the file copied into `backup` first."""
    data = path.read_bytes()
    if b"\0" not in data:
        return []
    parts = [x + b"\n" for x in data.split(b"\n")]
    parts[-1] = parts[-1][:-1]                               # what follows the last newline, if anything
    hit = [i for i, x in enumerate(parts) if b"\0" in x]
    if not dry:
        backup.mkdir(parents=True, exist_ok=True)
        dest = backup / f"{path.parent.name}.{path.name}"
        write_private(dest.with_name(f"{dest.name}.{time.time_ns()}") if dest.exists() else dest, data)
        for i in hit:
            x = parts[i].rstrip(b"\n").replace(b"\0", b"")
            parts[i] = x + b"\n" if reads(x, daylog) else b"\n" if daylog else b""
        write_private(path, b"".join(parts), stat.S_IMODE(path.stat().st_mode))
    return [i + 1 for i in hit]


def repair(private: Path, state: Path, m: memory.Settings, flight: set[str], dry: bool = False,
           now: float | None = None, proc: Path = retention.PROC) -> list[str]:
    """A line per file with a NUL line: every day log, under the lock capture holds, then each room's assembled log
    and each transcript sessions.json binds, but those of a room in `flight` and any file a process holds open."""
    day, busy, files = retention.day(time.time() if now is None else now), retention.held(proc), []
    keep = (state if private.resolve().is_relative_to(REPO) else private) / "backups" / f"doctor-{day}"
    if m.daily.is_dir():
        with memory.daily_lock(m.daily):
            files = [(f"day log {p.name}", mend(p, True, keep, dry)) for p in sorted(m.daily.glob("*.md"))]
    bound = json.loads((state / "sessions.json").read_text()) if (state / "sessions.json").exists() else {}
    logs = [(r.name, "assembled log", r / "assembled.jsonl") for r in sorted((state / "rooms").glob("*"))]
    for room, what, p in logs + [(str(n), "transcript", Path(str(r.get("transcript") or ""))) for n, r in bound.items()]:
        if room not in flight and p.is_file() and ((st := p.stat()).st_dev, st.st_ino) not in busy:
            files.append((f"{room} {what}", mend(p, False, state / "trash" / day / "doctor", dry)))
    said = "would mend" if dry else "mended"
    return [f"{what}: {said} line{'s' if len(n) > 1 else ''} {', '.join(map(str, n[:5]))}" for what, n in files if n]


def start(private: Path, state: Path, m: memory.Settings, flight: set[str]) -> str:
    """yerkd's crash repair before any room opens (`flight`: rooms a crash left with a message waiting); the
    morning line, for its log."""
    save(state, time.time(), repair(private, state, m, flight))
    return line(state, time.time())[1]


def checks(private: Path, state: Path, conf: dict[str, Any], m: memory.Settings, now: float, last: dict[str, Any],
           found: list[str]) -> dict[str, Any]:
    """Every check but the crash repair, each finding a line in `found`; returns the sizes to keep for tomorrow."""
    def shape() -> None:
        for name in ("accounts.toml", "rooms.toml"):
            try:
                load_toml(private / name)
            except (OSError, ValueError) as e:
                raise ValueError(f"{name} does not read: {e}") from e
        rs = rooms(private)                                     # a room the loader refuses ends this check with its why
        for n, c in rs.items():
            found.extend(f"room {n}: no {k} file" for k in ("persona", "rulebook", "voice")
                         if c.get(k) and not (layer_dir(private, c) / str(c[k])).is_file())
            found.extend(f"room {n}: rooms.toml key {k} is read by nothing" for k in sorted(set(c) - ROOM_KEYS))
            found.extend([f"room {n}: no layer folder"] if not layer_dir(private, c).is_dir() else [])
        also = {(private / Path(str(x)).expanduser()).resolve() for c in rs.values() if isinstance(c.get("also"), list) for x in c["also"]}
        found.extend(f"also root {r.name}: missing" for r in sorted(also | set(m.scope.roots[1:])) if not r.exists())
        named = [v for t in conf.values() if isinstance(t, dict) for k, v in t.items() if k.endswith(("_file", "_key")) and isinstance(v, str)]
        named += [str(r["token"]) for r in (load_toml(private / "accounts.toml").get("accounts") or {}).values() if r.get("token")]
        for p in (private / Path(x).expanduser() for x in named):
            mode = stat.S_IMODE(p.stat().st_mode) if p.is_file() else 0
            found.extend([] if mode == 0o600 else [f"secret {p.name}: " + (f"mode {mode:o}, not 600" if mode else "missing")])

    def classes() -> None:
        for name in m.scope.files():
            with open(name, encoding="utf-8", errors="replace") as f:
                fm = memory.FRONT.match(f.read(400))            # where file_class reads it
            if fm:
                said = [v.strip() for k, _, v in (x.partition(":") for x in fm.group(1).splitlines()) if k.strip() == "class"]
                found.extend(f"{Path(name).name}: class {v} is not one of {', '.join(memory.CLASSES)}" for v in said if v not in memory.CLASSES)
        for side in (s for r in m.scope.roots if r.is_dir() for s in r.rglob("*.class")):
            if (side.read_text().split() or [""])[0] not in memory.CLASSES:
                found.append(f"{side.name}: not one of {', '.join(memory.CLASSES)}")
        for p in sorted(m.daily.glob("*.md")):
            text = p.read_text(errors="replace").split("\n")
            bad = [i + 1 for i, x in enumerate(text) if x.strip() and "\0" not in x and not reads(x.encode(), True)]
            found.extend([f"day log {p.name}: no day header"] if not memindex.DAY_HEAD.match(text[0]) else [])
            found.extend([f"day log {p.name}: line {', '.join(map(str, bad[:5]))} does not parse"] if bad else [])

    def tokens() -> None:
        unknown = []
        for a in load_accounts(private / "accounts.toml"):
            if a.token_file and a.token_file.is_file():        # none: the config folder's own login, refreshed by Claude Code
                try:
                    issued = isinstance(d := json.loads(a.token_file.read_text()), dict) and bool(d.get("issued"))
                except ValueError:
                    issued = False                              # a bare pasted token: its file's time is no issue date
                if not issued:
                    unknown.append(a.name)
                elif (t := token_info(a, now))["token"] != "ok":
                    found.append(f"token {a.name}: {t['days_left']} days left")
        found.extend([f"token expiry unknown: {', '.join(unknown)}"] if unknown else [])

    def index() -> None:
        if (r := memindex.check(m.index_db, m.scope))["status"] not in ("fresh", "pending"):
            found.append(f"memory index: {r['status']}")
        try:
            built = float(json.loads(m.mentions.read_text())["built"])
        except (OSError, ValueError, KeyError):
            built = 0.0
        src = Builder(m.mentions, m.roots_for_mentions, m.curated, m.mentions_exclude)
        if max((t / 1e9 for p, (_, t) in m.scope.files().items() if src.is_source(Path(p))), default=0.0) > built + memindex.GRACE:
            found.append("mention index: older than a fact file")
        if (e := memory.meaning_state(m)) in ("no-index", "empty"):
            found.append(f"meaning search: {e}")

    def hooked() -> None:
        if (live := status.ask(state) if status.alive(state) else None) is None:
            found.append("hooks: not checked, no yerkd answers")
            return
        url = f"http://127.0.0.1:{json.loads((state / 'yerkd.json').read_text())['port']}/hook"
        for n in sorted({str(u["name"]) for u in live.get("units", [])} - {"yerkd"}):
            got = json.loads((state / "rooms" / n / "settings.json").read_text()).get("hooks") or {}
            if set(got) != set(hooks.settings_for(url)) or any(h.get("type") != "command" or url not in str(h.get("command"))
                                                              for e in got.values() for x in e for h in x.get("hooks", [])):
                found.append(f"room {n}: hooks not as yerkd puts them")

    now_sizes: dict[str, int] = {}

    def growth() -> None:
        sizes = {"state folder": state, "memory tree": m.root, **{f"room {r.name}": r / "work" for r in sorted((state / "rooms").glob("*"))}}
        now_sizes.update({k: retention.size(retention.tree(p)) for k, p in sizes.items() if p.exists()})
        mb = (conf.get("doctor") or {}).get("growth_mb", GROWTH_MB)
        mb = mb if isinstance(mb, (int, float)) and not isinstance(mb, bool) and mb > 0 else GROWTH_MB
        days = (now - float(last.get("sized", 0))) / DAY
        for k, n in now_sizes.items():
            if days >= 0.75 and k in last.get("sizes", {}) and (n - last["sizes"][k]) / days > mb * 1e6:
                found.append(f"{k}: grew {words.size(n - last['sizes'][k])} in {days:.0f} day{'s' if round(days) != 1 else ''}")

    for what, check in (("shape", shape), ("classes", classes), ("tokens", tokens), ("index", index), ("hooks", hooked),
                        ("disk", growth)):
        try:
            check()
        except (Exception, SystemExit) as e:                    # a check that cannot run is a line; the others still run
            found.append(f"{what}: not checked, {type(e).__name__}: {words.no_paths(str(e))[:80]}")
    old = (now - float(last.get("sized", 0))) / DAY < 0.75     # tomorrow compares against the run a day before it
    return {"sizes": last.get("sizes", {}) if old else now_sizes, "sized": last.get("sized", 0) if old else now}


def load(state: Path) -> dict[str, Any]:
    try:
        return dict(json.loads((state / LAST).read_text()))
    except (OSError, ValueError, TypeError):
        return {}


def save(state: Path, now: float, repaired: list[str], full: dict[str, Any] | None = None) -> None:
    """doctor.json: the last full run (time, findings, sizes), and the repairs of the last day, from any run."""
    d = {**load(state), **(full or {})}
    d["repaired"] = [r for r in d.get("repaired", []) if now - r["at"] <= DAY] + [{"at": now, "line": x} for x in repaired]
    write_private(state / LAST, json.dumps(d))


def run(private: Path, state: Path, dry: bool = False, now: float | None = None, proc: Path = retention.PROC,
        flight: Callable[[Path, Ledger, list[str]], set[str]] = retention.in_flight) -> list[str]:
    """One run: the crash repair, then the checks; every line, nothing changed when `dry`, else kept."""
    now = time.time() if now is None else now
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    m, found, last = memory.settings(private, state, dict(conf.get("memory", {}))), list[str](), load(state)
    names = [r.name for r in (state / "rooms").glob("*")]
    fixed = repair(private, state, m, flight(state, Ledger(state / "ledger.sqlite"), names), dry, now, proc)
    kept = checks(private, state, conf, m, now, last, found)
    if not dry:
        save(state, now, fixed, {"at": now, "findings": found, **kept})
    return fixed + found


def main(private: Path, state: Path, dry: bool = False) -> int:
    """A line per repair and finding, nothing when all is well; exits 1 when there was any, 2 when it could not run."""
    try:
        out, code = run(private, state, dry), 1
    except Exception as e:                                      # yerkd.toml unreadable, the state folder gone
        out, code = [f"doctor failed: {type(e).__name__}: {words.no_paths(str(e))[:120]}"], 2
        if not dry:
            save(state, time.time(), [], {"at": time.time(), "findings": out})
    for x in out:
        print(x)
    return code if out else 0


def line(state: Path, now: float) -> tuple[str, str]:
    """The morning diff's (finding key, line): the last full run's findings and the last day's repairs, the count and
    the first; not run yet, a plain line, in an install's first two days with no full run; unknown after them or with none in the last day."""
    d = load(state)
    if now - float(d.get("at", 0)) > DAY:
        return "unknown doctor", words.line(f"{words.NOT_DONE} " + words.UNKNOWN.format(what="doctor", why=words.NO_RUN))
    found = [*(r["line"] for r in d.get("repaired", []) if now - r["at"] <= DAY), *d.get("findings", [])]
    if not found:
        return "", words.line(f"{words.PLAIN} {words.DOCTOR_OK}")
    return f"doctor {found[0]}", words.line(f"{words.NOT_DONE} " + words.DOCTOR.format(n=len(found), first=found[0]))
