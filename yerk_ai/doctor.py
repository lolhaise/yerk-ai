"""`yerk doctor`: checks the install once a day, and repairs the files a crash or a power cut left damaged. units/yerk-doctor.timer
units/yerk-doctor.timer, after retention's), quiet when all is well, a plain line per finding, each run kept in
<state>/doctor.json for the morning diff; once a week it also fetches the Claude docs from the web (docs.py), unless
[docs] fetch = false in yerkd.toml. A hard power-off zeroes the line a file was writing, so on yerkd's start and
log, assembled log or bound transcript with a NUL byte is copied first (a day log to backups/doctor-<day>/ beside the private
layer, the rest to <state>/trash/<day>/doctor/), then each such line keeps what still reads (the line glued on after its NULs, else
itself without them), else is blank in a day log (recall cites file:line) or dropped."""
from __future__ import annotations

import json
import re
import stat
import time
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import deploy, docs, hooks, memindex, memory, retention, status, words
from . import shape as rule_shape                      # the check named "rule shape"; "layout" is the private folder's
from .accounts import load_accounts, token_info
from .config import REPO, ROOM_KEYS, budgets, load_toml, room_id, rooms, write_private
from .context import layer_dir
from .ledger import Ledger
from .mentions import Builder

DAY, LAST, GROWTH_MB = 86400.0, "doctor.json", 500      # [doctor] growth_mb in yerkd.toml over GROWTH_MB
CGROUP, STUCK = Path("/sys/fs/cgroup"), 3               # where a process's memory cap is read; backfill pauses in a row that are a finding
# Secret-shaped text in memory (claim 4): the key shapes tests/test_scrub.py knows, then one-time codes and password or
# token lines. Said by file:line and kind, never the value; the owner scrubs and rotates (a key in a log is already out).
SECRETS = {"key": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"
                             r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                             r"|[MN][A-Za-z0-9]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),
           "one-time code": re.compile(r"(?i)\b(?:(?:verification|security|one[- ]time|login|sign[- ]in|2fa|confirmation)\s+"
                                       r"(?:code|pin)|otp|passcode|your (?:code|pin) is)\b\D{0,12}\d{4,8}\b"),
           "password line": re.compile(r"(?i)\bpass(?:word|phrase)\s*(?:is|:|=)\s*(?=\S*[\d!#$%^&*])\S{6,}"),
           "token line": re.compile(r"(?i)\b(?:token|api[ _-]?key|secret|bearer)\s*[:=]\s*[\"'`]?[\w.-]{16,}")}
TEXT = (".md", ".txt", ".json", ".jsonl", ".csv", ".eml")
SPENT = 2 * DAY                                         # a one-time code is dead this long after its own line's date


def dated(text: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(text, fmt)
    except ValueError:
        return None


def scan(f: Path, now: float | None = None) -> list[tuple[int, str]]:
    """(line, kind) of each secret-shaped line of `f`, by its first shape. With `now`, for an import's file: a one-time code
    is dead, and no hit, on a line whose own date is more than two days old, its entry's `- YYYY-MM-DD HH:MM` (the lines
    under an entry share it), else the end of its month file (imports/mail/YYYY-MM.md); with no date it is said. PR 66's
    cold read scanned a live tree on 2026-10-21: 194 of its 213 hits were old mail's verification codes."""
    month = dated(f.stem, "%Y-%m")
    end = (month.replace(day=28) + timedelta(days=4)).replace(day=1).timestamp() if month else None
    out, at = list[tuple[int, str]](), None
    for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
        if line.startswith("- "):                           # an entry's first line: its own time, or none
            at = d.timestamp() if (d := dated(line[2:18], "%Y-%m-%d %H:%M")) else None
        dead = now is not None and now - (at or end or now) > SPENT
        if kind := next((k for k, rx in SECRETS.items() if not (dead and k == "one-time code") and rx.search(line)), ""):
            out.append((n, kind))
    return out


def reads(line: bytes, daylog: bool) -> bool:
    """A line with its NULs out still says something: a day-log line, its header or an attachment line; a JSON object."""
    try:
        text = line.decode()
        if daylog:
            return bool(memory.parse_line(text) or memindex.DAY_HEAD.match(text) or text.startswith("  - attachment: "))
        return isinstance(json.loads(text), dict)          # a JSONL row is an object; a bare value is a fragment
    except ValueError:                                      # not UTF-8, or not JSON
        return False


def rewrite(path: Path, backup: Path, fix: Callable[[bytes], bytes | None], dry: bool) -> list[int]:
    """The lines of `path` (from 1) that `fix` changes (it gets each line with its newline and answers the new bytes,
    or None to keep it), rewritten unless `dry`, the file copied into `backup` first and its mode kept."""
    data = path.read_bytes()
    parts = [x + b"\n" for x in data.split(b"\n")]
    parts[-1] = parts[-1][:-1]                               # what follows the last newline, if anything
    new = {i: y for i, x in enumerate(parts) if (y := fix(x)) is not None and y != x}
    if new and not dry:
        backup.mkdir(parents=True, exist_ok=True)
        dest = backup / f"{path.parent.name}.{path.name}"
        write_private(dest.with_name(f"{dest.name}.{time.time_ns()}") if dest.exists() else dest, data)
        write_private(path, b"".join(new.get(i, x) for i, x in enumerate(parts)), stat.S_IMODE(path.stat().st_mode))
    return [i + 1 for i in new]


def mend(path: Path, daylog: bool, backup: Path, dry: bool) -> list[int]:
    """The lines of `path` that hold a NUL byte (from 1), mended unless `dry`, the file copied into `backup` first."""
    def fix(line: bytes) -> bytes | None:                    # a long write's first page can outlive the rest: the line a
        if b"\0" not in line:                                # writer glued on after the NULs first, else the line without them
            return None
        x = line.rstrip(b"\n")
        x = next((y for y in (x.rsplit(b"\0", 1)[1], x.replace(b"\0", b"")) if reads(y, daylog)), b"")
        return x + b"\n" if x else b"\n" if daylog else b""
    return rewrite(path, backup, fix, dry)


def exposed(root: Path) -> list[tuple[str, int]]:
    """(path under `root`, mode) of each file and folder, `root` too, that a group or other user can read: its read bit
    for them, every folder above it up to `root` searchable by them. A 0700 root hides everything in it. Links: not followed."""
    out, todo = [], [(root, 0o044)]                      # 0o044: the group and other read bits still reachable
    while todo:
        p, reach = todo.pop()
        with suppress(OSError):
            st = p.lstat()
            if (mode := stat.S_IMODE(st.st_mode)) & reach and not stat.S_ISLNK(st.st_mode):
                out.append((p.relative_to(root).as_posix(), mode))
            if stat.S_ISDIR(st.st_mode) and (inner := reach & (mode << 2)):   # x for a class, shifted onto its r bit
                todo += [(c, inner) for c in p.iterdir()]
    return sorted(out)


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
    morning line, for its log. A repair that cannot run is its own line and never stops yerkd's start."""
    try:
        save(state, time.time(), repair(private, state, m, flight))
        return line(state, time.time())[1]
    except Exception as e:                                  # a zeroed sessions.json, a folder it cannot write: the daily run says it
        return f"doctor: crash repair failed, {type(e).__name__}: {words.no_paths(str(e))[:120]}"


def checks(private: Path, state: Path, conf: dict[str, Any], m: memory.Settings, now: float, last: dict[str, Any],
           found: list[str], dry: bool = False) -> dict[str, Any]:
    """Every check but the crash repair, each finding a line in `found`; returns the sizes and rule shape totals to keep."""
    def layout() -> None:
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

    def modes() -> None:             # the example layer is a public checkout: a private folder of the owner's own only
        for what, root in (("private folder", private), ("state folder", state)):
            if not (root == private and private.resolve().is_relative_to(REPO)) and (seen := exposed(root)):
                found.append(f"{what}: other users can read {len(seen)} of its files and folders, first "
                             f"{'the folder itself' if seen[0][0] == '.' else seen[0][0]} ({seen[0][1]:o}); chmod 700 it")

    def leaks() -> None:             # the day logs, notes and fact files, and the text an import saved: report, never rewrite
        roots = [m.root] + [private / d for d in ("attachments", "imports") if (private / d).is_dir()]     # an import's files
        hits = [(f, n, kind) for f in sorted(p for r in roots for p in r.rglob("*") if p.suffix in TEXT and p.is_file())
                for n, kind in scan(f, now if f.is_relative_to(private / "imports") else None)]    # an import's dead codes: not said
        if hits:
            (f, n, kind), files = hits[0], len({h[0] for h in hits})
            found.append(f"secret-shaped lines: {len(hits)} in {files} file{'s' * (files > 1)}, first "
                         f"{f.relative_to(private) if f.is_relative_to(private) else f.name}:{n} ({kind}); scrub by hand, rotate the key")

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

    def high() -> None:                                         # yerk-index's memory cap against its unit's rule (F1, 2026-10-21)
        from . import embeddings                                # numpy stays out of yerkd, which imports this module
        e, f = (st := memory.index_status(m)).get("embeddings") or {}, retention.PROC / str(st.get("pid")) / "cgroup"
        cg = next((x[3:] for x in f.read_text().splitlines() if x.startswith("0::")), "") if f.is_file() else ""
        got = int(t) if cg and (p := CGROUP / cg.lstrip("/") / "memory.high").is_file() and (t := p.read_text().strip()).isdigit() else 0
        held = int((st.get("pin") or {}).get("bytes", 0)) + int((st.get("keywords") or {}).get("cache_kib", 0)) * 1024   # recall-warm
        need = embeddings.HIGH_BASE + -(-int(e.get("vectors", 0)) // embeddings.BLOCK) * embeddings.BLOCK * (2 * int(e.get("width", 256)) + 20)
        found.extend([f"memory index: memory capped at {got >> 20}M, under the {(need + held) >> 20}M its unit's rule gives "
                      f"{int(e.get('vectors', 0)):,} vectors" + (f" and {held >> 20}M of keyword pages and cache" if held else "")]
                     if 0 < got < need + held else [])

    def backfill() -> None:                                     # the meaning backfill failing cycle after cycle (F2)
        e = memory.index_status(m).get("embeddings") or {}
        if e.get("paused") and (n := int(e.get("streak", e.get("failed", 0)))) >= STUCK:     # an index from before streak: failed
            found.append(f"meaning search: backfill paused after {n} failures: {words.no_paths(' '.join(str(e.get('last_error')).split()))[:100]}")

    def cites() -> None:                                        # two roots of one folder name and one cite: it opens only one (F3)
        named = [memory.cite(m, r) for r in m.scope.roots]
        found.extend(f"memory roots: {named.count(n)} share the cite {n}" for n in sorted(set(named)) if named.count(n) > 1)

    shaped: dict[str, int] = {}

    def ruleshape() -> None:                                    # every room's rule files against the prompting guide
        rs, limits = rooms(private), dict(budgets().get("shape") or {})
        r = rule_shape.check(private, m, rs, dict(conf.get("docs") or {}), now)
        found.extend(rule_shape.findings(r, limits))
        shaped.update(rule_shape.totals(r))
        at = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        for name in [] if dry else r["rooms"]:                  # each room's own report, of its class: docs/shape/<room>.md
            (out := docs.root(state) / "shape").mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(out / f"{re.sub(r'[^\w.-]', '-', name)}.md",
                          f"---\nclass: room\nroom: {room_id(name, rs[name])}\n---\n" + rule_shape.report(name, r, limits, at))

    def code() -> None:                                         # yerk-index on older code than yerkd (four hours, 2026-10-20)
        ran, ix = ((status.ask(state) or {}) if status.alive(state) else {}).get("commit"), deploy.indexed(state)
        if ran and ix and ran != ix:
            found.append(f"memory index: runs {ix[:7]}, yerkd {str(ran)[:7]}: restart yerk-index")

    for what, check in (("layout", layout), ("classes", classes), ("tokens", tokens), ("index", index), ("hooks", hooked),
                        ("modes", modes), ("secrets", leaks), ("disk", growth), ("memory cap", high), ("backfill", backfill),
                        ("cites", cites), ("code", code), ("rule shape", ruleshape)):
        try:
            check()
        except (Exception, SystemExit) as e:                    # a check that cannot run is a line; the others still run
            found.append(f"{what}: not checked, {type(e).__name__}: {words.no_paths(str(e))[:80]}")
    old = (now - float(last.get("sized", 0))) / DAY < 0.75     # tomorrow compares against the run a day before it
    return {"sizes": last.get("sizes", {}) if old else now_sizes, "sized": last.get("sized", 0) if old else now, "shape": shaped}


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
        flight: Callable[[Path, Ledger, list[str]], set[str]] = retention.in_flight, refresh: bool = False) -> list[str]:
    """One run: the crash repair, the docs when due (`refresh`: `yerk doctor`, the daily unit), then the checks; every line,
    nothing changed when `dry`, else kept."""
    now = time.time() if now is None else now
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    m, found, last = memory.settings(private, state, dict(conf.get("memory", {}))), list[str](), load(state)
    names, ledger = [r.name for r in (state / "rooms").glob("*")], Ledger(state / "ledger.sqlite")
    try:
        busy = flight(state, ledger, names)
    finally:
        ledger.close()              # before the checks: its -wal and -shm files go with it, never counted as the install's
    fixed = repair(private, state, m, busy, dry, now, proc)
    if refresh and not dry and docs.due(state, dict(conf.get("docs") or {}), now):     # weekly; a failure tries again tomorrow
        try:
            found.extend(docs.failed(docs.refresh(state, dict(conf.get("docs") or {}), now=now)))
        except Exception as e:                                  # a refresh that cannot run is a line; the checks still run
            found.append(f"docs: not refreshed, {type(e).__name__}: {words.no_paths(str(e))[:80]}")
    kept = checks(private, state, conf, m, now, last, found, dry)
    if not dry:
        save(state, now, fixed, {"at": now, "findings": found, **kept})
    return fixed + found


def main(private: Path, state: Path, dry: bool = False, refresh: bool = False) -> int:
    """A line per repair and finding, nothing when all is well; exits 1 when there was any, 2 when it could not run."""
    try:
        out, code = run(private, state, dry, refresh=refresh), 1
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


def shape_line(state: Path, now: float) -> tuple[str, str] | None:
    """The morning diff's rule-shape line: the last full run's totals, plain when every count is zero; none past a day."""
    d = load(state)
    if now - float(d.get("at", 0)) > DAY or not (t := d.get("shape")):
        return None
    said = ", ".join(f"{t[k]} {k}" for k in (*rule_shape.COUNTS, "twice") if t.get(k))
    return "", words.line(f"{words.WORKING} {words.SHAPE.format(counts=said)}" if said else f"{words.PLAIN} {words.SHAPE_OK.format(n=t.get('lines', 0))}")
