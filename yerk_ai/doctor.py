"""`yerk doctor`: checks the install once a day, and repairs the files a crash or a power cut left damaged. units/yerk-doctor.timer
runs it after retention's (Layer 11, claim 6, decision 40): one green line when all is well, a red or orange line per finding with
its fix, kept in <state>/doctor.json for the morning diff; once a day it also fetches the Claude docs from the web (docs.py),
[docs] fetch = false in yerkd.toml. A hard power-off zeroes the line a file was writing, so on yerkd's start and
log, assembled log or bound transcript with a NUL byte is copied first (a day log to backups/doctor-<day>/ beside the private
layer, the rest to <state>/trash/<day>/doctor/), then each such line keeps what still reads (the line glued on after its NULs, else
itself without them), else is blank in a day log (recall cites file:line) or dropped."""
from __future__ import annotations

import json
import os
import re
import stat
import time
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import addons, backup, deploy, docs, files, hooks, jobs, memindex, memory, pictures, retention, status, texts, words
from . import shape as rule_shape                      # the check named "rule shape"; "layout" is the private folder's
from .accounts import load_accounts, token_info
from .config import REPO, ROOM_KEYS, budgets, filled, load_toml, room_defaults, room_id, rooms, uncapped, write_private
from .context import NONE, TOPICAL, attachments_dir, chars, layer_dir, session_start
from .ledger import Ledger
from .mentions import Builder
from .texting import people as tpeople, sender as tsender
from .workers import systemctl

DAY, LAST, GROWTH_MB = 86400.0, "doctor.json", 500      # [doctor] growth_mb in yerkd.toml over GROWTH_MB
RED, ORANGE, SUNDAY = words.NOT_DONE, words.WORKING, 6  # a finding's severity, its first character; the weekly run's day
INDEX = "`systemctl --user restart yerk-index`"         # what a stale, re-scoped, failed or behind index wants
CGROUP, STUCK = Path("/sys/fs/cgroup"), 3               # where a process's memory cap is read; backfill pauses in a row that are a finding
# Secret-shaped text in memory (claim 4): the key shapes tests/test_scrub.py knows, then one-time codes and password or
# token lines. Said by file:line and kind, never the value; `yerk doctor --scrub` swaps each value (group v, else the whole
# match) for MARK, which no shape matches, and the owner rotates (a key in a log is already out).
SECRETS = {"key": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"
                             r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                             r"|[MN][A-Za-z0-9]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),
           "one-time code": re.compile(r"(?i)\b(?:(?:verification|security|one[- ]time|login|sign[- ]in|2fa|confirmation)\s+"
                                       r"(?:code|pin)|otp|passcode|your (?:code|pin) is)\b\D{0,12}(?P<v>\d{4,8})\b"),
           "password line": re.compile(r"(?i)\bpass(?:word|phrase)\s*(?:is|:|=)\s*(?=\S*[\d!#$%^&*])(?P<v>\S{6,})"),
           "token line": re.compile(r"(?i)\b(?:token|api[ _-]?key|secret|bearer)\s*[:=]\s*[\"'`]?(?P<v>[\w.-]{16,})")}
MARK = "[scrubbed]"
TEXT = (".md", ".txt", ".json", ".jsonl", ".csv", ".eml")
SPENT = 2 * DAY                                         # a one-time code is dead this long after its own line's date


def dated(text: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(text, fmt)
    except ValueError:
        return None


def used(path: Path) -> int:
    """The bytes every regular file at and under `path` holds on disk, links never followed, a file once however many links
    it has, added as the walk goes: no list of the 1.4 million entries a state folder holds (2026-11-10, the list alone was
    1.2 GB of the doctor's peak). A sparse file's holes and a second link are no growth (2026-11-10: a worker's sparse test
    files, 31 GB long and 1.7 GB on disk, read as the state folder's growth)."""
    if path.is_symlink() or not path.is_dir():
        s = path.lstat()
        return s.st_blocks * 512 if stat.S_ISREG(s.st_mode) else 0
    total, linked = 0, set[tuple[int, int]]()
    for d, _, fs in os.walk(path):
        for f in fs:
            with suppress(OSError):                         # a file gone mid-walk: a live state folder
                s = os.lstat(os.path.join(d, f))
                if not stat.S_ISREG(s.st_mode) or s.st_nlink > 1 and (s.st_dev, s.st_ino) in linked:
                    continue
                if s.st_nlink > 1:
                    linked.add((s.st_dev, s.st_ino))
                total += s.st_blocks * 512
    return total


def scan(f: Path, now: float | None = None) -> list[tuple[int, str]]:
    """(line, kind) of each secret-shaped line of `f`, by its first shape. With `now`, for an import's file: a one-time code
    is dead, and no hit, on a line whose own date is more than two days old, its entry's `- YYYY-MM-DD HH:MM` (the lines
    under an entry share it), else the end of its month file (imports/mail/YYYY-MM.md); with no date it is said. PR 66's
    cold read scanned a live tree on 2026-10-21: 194 of its 213 hits were old mail's verification codes."""
    month = dated(f.stem, "%Y-%m")
    end = (month.replace(day=28) + timedelta(days=4)).replace(day=1).timestamp() if month else None
    out, at = list[tuple[int, str]](), None
    for n, line in enumerate(f.read_bytes().decode(errors="replace").split("\n"), 1):     # by b"\n", as rewrite numbers them
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


def leaked(private: Path, m: memory.Settings, now: float) -> dict[Path, list[tuple[int, str]]]:
    """Each file with a secret-shaped line and its (line, kind) hits: the day logs, notes and fact files, and the text an
    import saved (an import's dead one-time codes are no hit)."""
    roots = [m.root] + [private / d for d in ("attachments", "imports") if (private / d).is_dir()]
    found = ((f, scan(f, now if f.is_relative_to(private / "imports") else None))
             for f in sorted(p for r in roots for p in r.rglob("*") if p.suffix in TEXT and p.is_file()))
    return {f: hits for f, hits in found if hits}


def unshaped(line: bytes) -> bytes:
    """The line with each secret-shaped value swapped for MARK until no shape is left in it, the rest of its bytes and its
    newline as they were: the file keeps its line count and a second scrub finds nothing."""
    text = line.decode(errors="surrogateescape")
    for _ in range(64):                                     # a value a pass; MARK matches no shape, so this ends
        if not (mo := next((x for rx in SECRETS.values() if (x := rx.search(text))), None)):
            break
        a, b = mo.span("v") if "v" in mo.re.groupindex else mo.span()
        text = text[:a] + MARK + text[b:]
    return text.encode(errors="surrogateescape")


def scrub(private: Path, state: Path, dry: bool = False, now: float | None = None) -> list[str]:
    """`yerk doctor --scrub`: each file the secrets check names copied first into <private>/backups/scrub-<day>/ (the
    example layer's into the state folder's, as the crash repair's), then the value on each line it named swapped for
    MARK; a line per file, by file and line, never the value; "would scrub" when `dry`, nothing written."""
    now = time.time() if now is None else now
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    m, out = memory.settings(private, state, dict(conf.get("memory", {}))), list[str]()
    keep = (state if private.resolve().is_relative_to(REPO) else private) / "backups" / f"scrub-{retention.day(now)}"
    with memory.daily_lock(m.daily) if m.daily.is_dir() else suppress():      # capture's appends wait for the rename
        for f, hits in leaked(private, m, now).items():
            at, n, kinds = {h[0] for h in hits}, iter(range(1, 1 << 62)), ", ".join(dict.fromkeys(h[1] for h in hits))
            name = f.relative_to(private) if f.is_relative_to(private) else f.name
            try:
                done = rewrite(f, keep, lambda x: unshaped(x) if next(n) in at else None, dry)
            except OSError as e:
                out.append(f"{RED} {name}: not scrubbed, {type(e).__name__}: {words.no_paths(str(e))[:80]}")
                continue
            lines = f"line{'s' * (len(done) > 1)} {', '.join(map(str, done[:8]))}{', ...' * (len(done) > 8)}"
            out.append(f"{ORANGE} {name}: would scrub {lines} ({kinds})" if dry else
                       f"{words.DONE} {name}: scrubbed {lines} ({kinds}), its copy in {keep.parent.name}/{keep.name}/")
    return out or [f"{words.DONE} scrub: no secret-shaped line"]


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
    return [f"{ORANGE} {what}: {said} line{'s' if len(n) > 1 else ''} {', '.join(map(str, n[:5]))}" for what, n in files if n]


def start(private: Path, state: Path, m: memory.Settings, flight: set[str]) -> str:
    """yerkd's crash repair before any room opens (`flight`: rooms a crash left with a message waiting); the
    morning line, for its log. A repair that cannot run is its own line and never stops yerkd's start."""
    try:
        save(state, time.time(), repair(private, state, m, flight))
        return line(state, time.time())[1]
    except Exception as e:                                  # a zeroed sessions.json, a folder it cannot write: the daily run says it
        return f"doctor: crash repair failed, {type(e).__name__}: {words.no_paths(str(e))[:120]}"


def drift(name: str, own: dict[str, Any], base: dict[str, Any]) -> str:
    """An owner room as rooms.toml writes it against [defaults] (`base`), one line, "" when it inherits clean: red for a
    default tool its own allow misses and its allow_remove does not name, orange for its own copy of a default (an allow
    equal as a set to the one it would inherit, an mcp server equal to the default filled in for it), which is how the
    rooms come apart later."""
    mine = own["allow"] if isinstance(own.get("allow"), list) else None
    inherited = [t for t in base.get("allow", []) if t not in set(own.get("allow_remove") or [])]
    missing = [t for t in inherited if t not in mine] if mine is not None else []
    copies = ["allow"] if mine is not None and "allow" in base and set(mine) == set(inherited) else []
    servers = own["mcp"] if isinstance(own.get("mcp"), dict) else {}
    copies += [f"mcp.{s}" for s, v in (base.get("mcp") or {}).items() if servers.get(s) == filled(v, name)]
    said = [f"misses default tool{'s' * (len(missing) > 1)} {', '.join(missing)}"] if missing else []
    said += [f"carries a copy of the default {' and '.join([', '.join(copies[:-1]), copies[-1]] if len(copies) > 1 else copies)}"] if copies else []
    fix = ["add each to the room's allow or name it in allow_remove"] if missing else []
    fix += ["delete the copy so the room inherits [defaults]"] if copies else []
    return f"{words.NOT_DONE if missing else words.WORKING} room {name}: {'; '.join(said)}; in rooms.toml, {', and '.join(fix)}" if said else ""


def checks(private: Path, state: Path, conf: dict[str, Any], m: memory.Settings, now: float, last: dict[str, Any],
           found: list[str], dry: bool = False) -> dict[str, Any]:
    """Every check but the crash repair, each finding a line in `found`; returns the sizes and rule shape totals to keep."""
    def layout() -> None:
        for name in ("accounts.toml", "rooms.toml"):
            try:
                load_toml(private / name)
            except (OSError, ValueError) as e:
                raise ValueError(f"{name} does not read: {e}") from e
        rs, known = rooms(private), ROOM_KEYS | addons.room_keys(state)     # an installed add-on's keys are known keys
        for n, c in rs.items():
            found.extend(f"{RED} room {n}: no {k} file; write {c[k]} in its layer folder or fix {k} in rooms.toml"
                         for k in ("persona", "rulebook", "voice") if c.get(k) and not (layer_dir(private, c) / str(c[k])).is_file())
            found.extend(f"{ORANGE} room {n}: rooms.toml key {k} is read by nothing; fix its name or delete it in rooms.toml"
                         for k in sorted(set(c) - known))
            found.extend([f"{RED} room {n}: no layer folder; `mkdir -m 700 {layer_dir(private, c)}`"] if not layer_dir(private, c).is_dir() else [])
        if base := room_defaults(load_toml(private / "rooms.toml")):     # each owner room as written, against [defaults]
            found.extend(x for n, c in rooms(private, own=True).items() if str(c.get("privacy", "owner")) == "owner" and (x := drift(n, c, base)))
        table = load_toml(private / jobs.FILE).get("jobs") if (private / jobs.FILE).is_file() else {}   # a cron line left in the jobs file
        found.extend(f"{RED} job {n}: {x}" for n, j in (table or {}).items() if isinstance(j, dict) and (x := jobs.cron_refusal(str(j.get("schedule", "")))))
        also = {(private / Path(str(x)).expanduser()).resolve() for c in rs.values() if isinstance(c.get("also"), list) for x in c["also"]}
        found.extend(f"{ORANGE} also root {r.name}: missing; put the folder back or drop it from also in rooms.toml"
                     for r in sorted(also | set(m.scope.roots[1:]) - set(m.uploads)) if not r.exists())
        named = [v for t in conf.values() if isinstance(t, dict) for k, v in t.items() if k.endswith(("_file", "_key")) and isinstance(v, str)]
        named += [str(r["token"]) for r in (load_toml(private / "accounts.toml").get("accounts") or {}).values() if r.get("token")]
        for p in (private / Path(x).expanduser() for x in named):
            mode = stat.S_IMODE(p.stat().st_mode) if p.is_file() else 0
            found.extend([] if mode == 0o600 else [f"{RED} secret {p.name}: " + (f"mode {mode:o}, not 600; `chmod 600 {p}`" if mode else
                                                                                   "missing; write it 0600 or fix its name in yerkd.toml or accounts.toml")])
        if (tx := dict(conf.get("texting") or {})) and (tmode := str(tx.get("mode") or "off")) != "off":   # texting/: on, so it must hold
            found.extend(f"{RED} [texting] {k}: not a number above 0; texting stays off" for k in (
                "burst_quiet", "burst_cap", "catch_up_hours", "hold_hours", "first_shot_window", "claim_seconds", "stale_minutes")
                         if k in tx and (isinstance(tx[k], bool) or not isinstance(tx[k], (int, float)) or tx[k] <= 0))
            for k in (k for k in ("first_shot", "send_delay", "morning", "quiet") if k in tx):    # as texting/sender.py reads them
                try:
                    tsender.quiet_of(tx[k]) if k == "quiet" else tsender.pair(tx[k], (0.0, 0.0))
                except ValueError as e:
                    found.append(f"{RED} [texting] {k}: {e}; texting stays off")
            found.extend([f"{RED} [texting] outbox: not a whole path on the Mac; live sends nothing"]
                         if tmode == "live" and not texts.WHOLE.fullmatch(str(tx.get("outbox") or "")) else [])
            found.extend([f"{RED} [texting] live with no host for the hands: no text can go; [imessage] host names the Mac (else ssh "
                          "in an owner room's add-on's mac.json), never one that begins with a dash"]
                         if tmode == "live" and not texts.head(texts.way(conf, rs, state)) else [])
            found.extend([f"{RED} [texting] mode: {tmode} is not off, dry or live"] if tmode not in ("dry", "live") else [])
            accts = load_accounts(private / "accounts.toml")                 # the check runs on an account's token alone (J28)
            found.extend([f"{RED} [texting] {words.TEXTING_NO_TOKEN}; `yerk accounts mint {accts[0].name}` makes one"]
                         if accts and not any(token_info(a, now)["token"] != "none" for a in accts) else [])
            try:
                tpeople.load(tpeople.path(private, tx))
            except (OSError, ValueError) as e:
                found.append(f"{RED} [texting] people: {e}; texting stays off")
            found.extend(f"{RED} [texting] {k}: {tx.get(k) or 'none'} is not an owner room in rooms.toml"
                         for k in ("report", "dry_report") if (k == "report" or tx.get(k)) and (
                             str(rs.get(str(tx.get(k) or ""), {"privacy": "none"}).get("privacy", "owner")) != "owner"
                             or rs.get(str(tx.get(k) or ""), {}).get("people")))
            found.extend([f"{RED} [texting] live with text-out in [gates] off: no text goes, standing release included"]
                         if tmode == "live" and "text-out" in (dict(conf.get("gates") or {}).get("off") or []) else [])
            with suppress(OSError, ValueError):                   # the sender's last pass, while yerkd runs (J50)
                last = float(json.loads((state / "texting" / "status.json").read_text()).get("pass") or now)
                found.extend([f"{RED} [texting] the sender stopped {datetime.fromtimestamp(last):%H:%M}: no text is timed or sent; "
                              "`systemctl --user restart yerkd` starts it again"] if tmode in ("dry", "live") and now - last > 60 and status.alive(state) else [])

    def classes() -> None:
        for name in m.scope.files():
            with open(name, encoding="utf-8", errors="replace") as f:
                fm = memory.FRONT.match(f.read(400))            # where file_class reads it
            if fm:
                said = [v.strip() for k, _, v in (x.partition(":") for x in fm.group(1).splitlines()) if k.strip() == "class"]
                found.extend(f"{ORANGE} {Path(name).name}: class {v} is not one of {', '.join(memory.CLASSES)}; set its class: to one"
                             for v in said if v not in memory.CLASSES)
        for side in (s for r in m.scope.roots if r.is_dir() for s in r.rglob("*.class")):
            if (side.read_text().split() or [""])[0] not in memory.CLASSES:
                found.append(f"{ORANGE} {side.name}: not one of {', '.join(memory.CLASSES)}; write one of them in it")
        for p in sorted(m.daily.glob("*.md")):
            text = p.read_text(errors="replace").split("\n")
            bad = [i + 1 for i, x in enumerate(text) if x.strip() and "\0" not in x and not reads(x.encode(), True)]
            found.extend([f"{ORANGE} day log {p.name}: no day header; make its first line `# {p.stem}`"] if not memindex.DAY_HEAD.match(text[0]) else [])
            found.extend([f"{ORANGE} day log {p.name}: line {', '.join(map(str, bad[:5]))} does not parse; rewrite it in the day-log line shape"] if bad else [])

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
                    found.append(f"{RED if t['days_left'] <= 0 else ORANGE} token {a.name}: {t['days_left']} days left; `yerk accounts mint {a.name}`")
        found.extend([f"{ORANGE} token expiry unknown: {', '.join(unknown)}; `yerk accounts mint {unknown[0]}` dates it"] if unknown else [])

    def index() -> None:            # a scope change is said once, until the index runs on the new roots (its meta key moves)
        if (r := memindex.check(m.index_db, m.scope))["status"] == "scope-changed":
            said["scope"] = m.scope.key()
        if r["status"] not in ("fresh", "pending") and not (r["status"] == "scope-changed" and last.get("scope") == said["scope"]):
            found.append(f"{RED if r['status'] == 'failure' else ORANGE} memory index: {r['status']}"
                         f"{', still on the roots before a room came or went' * (r['status'] == 'scope-changed')}; {INDEX}")
        try:
            built = float(json.loads(m.mentions.read_text())["built"])
        except (OSError, ValueError, KeyError):
            built = 0.0
        src = Builder(m.mentions, m.roots_for_mentions, m.curated, m.mentions_exclude)
        if max((t / 1e9 for p, (_, t) in m.scope.files().items() if src.is_source(Path(p))), default=0.0) > built + memindex.GRACE:
            found.append(f"{ORANGE} mention index: older than a fact file; {INDEX}")
        if (e := memory.meaning_state(m)) in ("no-index", "empty"):
            found.append(f"{ORANGE} meaning search: {e}; {INDEX}")

    def hooked() -> None:
        if (live := status.ask(state) if status.alive(state) else None) is None:
            found.append(f"{ORANGE} hooks: not checked, no yerkd answers; `systemctl --user start yerkd`")
            return
        url = f"http://127.0.0.1:{json.loads((state / 'yerkd.json').read_text())['port']}/hook"
        for n in sorted({str(u["name"]) for u in live.get("units", [])} - {"yerkd"}):
            got = json.loads((state / "rooms" / n / "settings.json").read_text()).get("hooks") or {}
            if set(got) != set(hooks.settings_for(url)) or any(h.get("type") != "command" or url not in str(h.get("command"))
                                                              for e in got.values() for x in e for h in x.get("hooks", [])):
                found.append(f"{RED} room {n}: hooks not as yerkd puts them; `systemctl --user restart yerkd` writes them again")

    def modes() -> None:             # the example layer is a public checkout: a private folder of the owner's own only
        for what, root in (("private folder", private), ("state folder", state)):
            if not (root == private and private.resolve().is_relative_to(REPO)) and (seen := exposed(root)):
                found.append(f"{RED} {what}: other users can read {len(seen)} of its files and folders, first "
                             f"{'the folder itself' if seen[0][0] == '.' else seen[0][0]} ({seen[0][1]:o}); `chmod 700 {root}`")

    def leaks() -> None:             # the day logs, notes and fact files, and the text an import saved: said, scrubbed by command
        if hits := [(f, n, kind) for f, hs in leaked(private, m, now).items() for n, kind in hs]:
            (f, n, kind), files = hits[0], len({h[0] for h in hits})
            found.append(f"{RED} secret-shaped lines: {len(hits)} in {files} file{'s' * (files > 1)}, first "
                         f"{f.relative_to(private) if f.is_relative_to(private) else f.name}:{n} ({kind}); `yerk doctor --scrub`, then rotate the key")

    now_sizes: dict[str, int] = {}

    def growth() -> None:
        sizes = {"state folder": state, "memory tree": m.root, **{f"room {r.name}": r / "work" for r in sorted((state / "rooms").glob("*"))}}
        now_sizes.update({k: used(p) for k, p in sizes.items() if p.exists()})
        mb = (conf.get("doctor") or {}).get("growth_mb", GROWTH_MB)
        mb = mb if isinstance(mb, (int, float)) and not isinstance(mb, bool) and mb > 0 else GROWTH_MB
        days = (now - float(last.get("sized", 0))) / DAY
        for k, n in now_sizes.items():
            if days >= 0.75 and k in last.get("sizes", {}) and (n - last["sizes"][k]) / days > mb * 1e6:
                found.append(f"{ORANGE} {k}: grew {words.size(n - last['sizes'][k])} in {days:.0f} day{'s' if round(days) != 1 else ''}; "
                             "`yerk retention --dry-run` says what the sweep would move")

    def high() -> None:                                         # yerk-index's memory cap against its unit's rule (F1, 2026-10-21)
        from . import embeddings                                # numpy stays out of yerkd, which imports this module
        e, f = (st := memory.index_status(m)).get("embeddings") or {}, retention.PROC / str(st.get("pid")) / "cgroup"
        cg = next((x[3:] for x in f.read_text().splitlines() if x.startswith("0::")), "") if f.is_file() else ""
        got = int(t) if cg and (p := CGROUP / cg.lstrip("/") / "memory.high").is_file() and (t := p.read_text().strip()).isdigit() else 0
        held = int((st.get("pin") or {}).get("bytes", 0))      # recall-warm: the keyword tables' pages the index holds mapped
        need = embeddings.HIGH_BASE + -(-int(e.get("vectors", 0)) // embeddings.BLOCK) * embeddings.BLOCK * (2 * int(e.get("width", 256)) + 20)
        found.extend([f"{ORANGE} memory index: memory capped at {got >> 20}M, under the {(need + held) >> 20}M its unit's rule gives "
                      f"{int(e.get('vectors', 0)):,} vectors" + (f" and {held >> 20}M of pinned keyword pages" if held else "")
                      + "; raise MemoryHigh with `systemctl --user edit yerk-index`"] if 0 < got < need + held else [])

    def backfill() -> None:                                     # the meaning backfill failing cycle after cycle (F2)
        e = memory.index_status(m).get("embeddings") or {}
        if e.get("paused") and (n := int(e.get("streak", e.get("failed", 0)))) >= STUCK:     # an index from before streak: failed
            found.append(f"{ORANGE} meaning search: backfill paused after {n} failures: "
                         f"{words.no_paths(' '.join(str(e.get('last_error')).split()))[:100]}; {INDEX} once the cause is fixed")

    def cites() -> None:                                        # two roots of one folder name and one cite: it opens only one (F3)
        named = [memory.cite(m, r) for r in m.scope.roots]
        found.extend(f"{ORANGE} memory roots: {named.count(n)} share the cite {n}; rename one of the folders"
                     for n in sorted(set(named)) if named.count(n) > 1)

    def moved() -> None:                                        # the files a moved owner room brought, with no sidecar: no recall finds them
        for n, c in rooms(private).items():
            if str(c.get("privacy", "owner")) != "owner" or not (f := attachments_dir(private, n, c)).is_dir():
                continue
            pics, sheets = len(pictures.pending(f, True)), len(files.pending(f, True))
            listed = f in m.scope.roots and f in [(private / Path(str(x)).expanduser()).resolve() for x in c.get("also") or []]
            cmds = "; ".join(x.format(room=n, folder=f) for x in words.BACKFILLS)
            found.extend([f"{words.WORKING} " + words.MOVED_IN.format(room=n, pics=pics, sheets=sheets, folder=f, commands=cmds)
                          + ("" if listed else words.MOVED_UNLISTED)] if pics or sheets else [])

    shaped, weekly, said = dict[str, int](), list[str](), {"scope": ""}

    def ruleshape() -> None:        # every room's rule files against the prompting guide: the counts a weekly line, a rule filed today daily
        rs, limits = rooms(private), dict(budgets().get("shape") or {})
        r = rule_shape.check(private, m, rs, dict(conf.get("docs") or {}), now)
        found.extend(f"{ORANGE} {x}" for x in rule_shape.findings({**r, "files": {}, "twice": {}}, limits))
        counted, t, before = rule_shape.findings({**r, "fresh": []}, limits), rule_shape.totals(r), last.get("shape") or {}
        up = {k: t[k] - int(before.get(k, 0)) for k in (*rule_shape.COUNTS, "twice") if t[k] > int(before.get(k, 0))}
        if counted and (up or time.localtime(now).tm_wday == SUNDAY):     # Sunday's run, or a day a count rose
            counts = ", ".join(f"{t[k]} {'topics said twice' if k == 'twice' else k}" + (f" (+{up[k]})" if k in up else "")
                               for k in (*rule_shape.COUNTS, "twice") if t[k])
            weekly.append(f"{ORANGE} {words.SHAPE.format(counts=counts)}; {len(counted)} past budgets.toml [shape], `yerk docs shape` names each line")
        shaped.update(t)
        at = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        for name in [] if dry else r["rooms"]:                  # each room's own report, of its class: docs/shape/<room>.md
            (out := docs.root(state) / "shape").mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(out / f"{re.sub(r'[^\w.-]', '-', name)}.md",
                          f"---\nclass: room\nroom: {room_id(name, rs[name])}\n---\n" + rule_shape.report(name, r, limits, at))

    def code() -> None:                                         # yerk-index on older code than yerkd (four hours, 2026-10-20)
        ran, ix = ((status.ask(state) or {}) if status.alive(state) else {}).get("commit"), deploy.indexed(state)
        if ran and ix and ran != ix:
            found.append(f"{ORANGE} memory index: runs {ix[:7]}, yerkd {str(ran)[:7]}; {INDEX}")

    def fits() -> None:                                         # a part past its line goes out cut (2026-10-22), a topic rule on demand
        over: dict[tuple[str, str], tuple[int, int, list[str]]] = {}      # (part, its file) -> its size, its line, the rooms
        b = budgets(load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {})   # uncapped: nothing is cut
        for n, c in rooms(private).items() if not uncapped(b) else []:
            for p in (p for p in session_start(private, c, b, rules=m.root / memory.RULES) if p.over):
                over.setdefault((p.name, p.source), (p.raw, int(p.budget or 0), []))[2].append(n)
        for (part, src), (raw, cap, names) in over.items():
            found.append(f"{RED} {Path(src).name or part}: {part} over its line by {raw - cap:,} characters ({raw:,} of {cap:,}), cut "
                         f"at every session start in {', '.join(names)}; raise its line in budgets.toml or trim the file")
        tier, most, on_file = memory.live_rules(m.root / memory.TOPIC_RULES), NONE if uncapped(b) else int(b["per_message"]["on_hit"]["rules"]), set(rooms(private))
        never = [memory.topic(x) for x in tier if chars(x) + 1 > most - chars(TOPICAL) or (sc := memory.scope(x)) is not None and not sc & on_file]
        found.extend([f"{words.WORKING} {memory.TOPIC_RULES}: {len(never)} topic rule{'s' * (len(never) != 1)} never load, first \"{never[0]}\": "
                      f"longer than the per-message rules part's {most:,} or kept to no room in rooms.toml; shorten it or fix its [rooms: ...]"] if never else [])
        held = memory.live_rules(m.root / memory.OVERFLOW)
        found.extend([f"{words.WORKING} {memory.OVERFLOW}: {len(held)} rule{'s' * (len(held) != 1)} in no room yet; yerkd moves "
                      f"{'them' if len(held) != 1 else 'it'} to {memory.TOPIC_RULES} at its next start"] if held else [])

    def timers() -> None:           # a timer in units/ the box never installed (yerk-curate.timer, 2026-10-24) or turned off
        if private.resolve().is_relative_to(REPO):
            return                  # the example layer: a public checkout's units are no box's
        names = sorted(p.name for p in (REPO / "units").glob("*.timer"))
        if len(got := systemctl("is-enabled", *names).stdout.split()) != len(names):
            raise OSError("systemctl --user is-enabled did not answer for every timer")
        found.extend(f"{ORANGE} timer {t}: {s}; " + ("`yerk deploy --timers` installs and enables it" if s == "not-found" else
                                                     f"`systemctl --user enable --now {t}`") for t, s in zip(names, got) if s != "enabled")

    def backed() -> None:                                       # one bad disk takes an install with no backup (2026-10-24)
        b = backup.newest(private, state)
        if b is None and "backup" in conf:                      # a [backup] table: the owner set it up, and nothing landed
            found.append(f"{RED} backup: none in {backup.settings(private, state)[0].name}; `yerk backup` makes one, yerk-backup.timer each night")
        elif b is not None and now - b["at"] > backup.STALE:
            found.append(f"{RED} backup: the newest, {b['name']}, is {(now - b['at']) / 3600:.0f} h old, past 36 h; is yerk-backup.timer on?")

    def lesson_shape() -> None:     # the whole store against the Claude docs' lesson shape and its pinned sentences, daily
        found.extend(f"{ORANGE} {x}" for x in lessons(private, state, m, now, dry))

    for what, check in (("layout", layout), ("classes", classes), ("tokens", tokens), ("index", index), ("hooks", hooked),
                        ("modes", modes), ("secrets", leaks), ("disk", growth), ("memory cap", high), ("backfill", backfill),
                        ("cites", cites), ("code", code), ("rule shape", ruleshape), ("lesson shape", lesson_shape), ("fit", fits),
                        ("moved in", moved), ("backup", backed),
                        ("timers", timers)):
        try:
            check()
        except (Exception, SystemExit) as e:                    # a check that cannot run is a line; the others still run
            found.append(f"{ORANGE} {what}: not checked, {type(e).__name__}: {words.no_paths(str(e))[:80]}")
    old = (now - float(last.get("sized", 0))) / DAY < 0.75     # tomorrow compares against the run a day before it
    return {"sizes": last.get("sizes", {}) if old else now_sizes, "sized": last.get("sized", 0) if old else now, "shape": shaped,
            "weekly": weekly, "scope": said["scope"]}


LESSON_NEED = "the rule store kept in the Claude docs' lesson shape"            # the need the daily run files and folds into
PIN_NEED = "the lesson checks re-pinned to the Claude docs' sentences that moved"
MODEL_NEED = "the prompting page of a model a room runs added to the pages yerk docs fetches"   # shape.unpaged


def lessons(private: Path, state: Path, m: memory.Settings, now: float, dry: bool = False,
            post: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> list[str]:
    """The daily run of the Claude docs' lesson shape (2026-11-09: it should never regress or go out of date):
    every pinned sentence against its fetched page, the whole rule store against shape.LESSON, the index against code-memory's
    limits. Its lines are the doctor's, kept in docs/shape/lessons.md; a finding files a need through needs.file (one open need of
    that ability, its count rising each day it stays) so the pipeline builds the fix, and says one line in the machine room once
    a day. A green day writes the report and says nothing. The pages read follow the rooms (2026-11-09, Claude Fable 5.2): the
    ones every room reads and the prompting page of each model a room runs (shape.pages), and a model whose page `yerk docs
    refresh` does not fetch is a line and a need naming the page to add. A moved sentence, a model with no page and the store's
    the next run starts the example."""
    from . import needs
    from .config import models
    limits, root = dict(budgets().get("shape") or {}), docs.root(state)
    try:
        conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
        by = rule_shape.run_by(rooms(private), state, models(conf))
    except (OSError, ValueError):      # rooms.toml or yerkd.toml unreadable: the pages every room reads; layout and fit say why
        conf, by = {}, {}
    r = rule_shape.store(m, limits, root, rule_shape.pages(by))
    out = rule_shape.unpaged_lines(rule_shape.unpaged(by, docs.pages(dict(conf.get("docs") or {})))) + rule_shape.store_findings(r, limits, root)
    if dry:
        return out
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    (folder := root / "shape").mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(folder / "lessons.md", "---\nclass: private\n---\n" + rule_shape.store_report(r, out, day))
    if not out:
        return out
    machine = next((n for n, c in rooms(private).items() if c.get("role") == "machine"), "")
    kinds = {PIN_NEED: [x for x in out if x.startswith("docs moved")], MODEL_NEED: [x for x in out if x.startswith("docs: no prompting")]}
    kinds[LESSON_NEED] = [x for x in out if x not in kinds[PIN_NEED] + kinds[MODEL_NEED]]
    filed = [needs.file(m, state, machine or "doctor", f"doctor-lessons-{day}", now=now, tried=said[0], needed=need,
                        asked="the owner's order of 2026-11-09: every rule written the way the Claude docs say, never out of date")
             for need, said in kinds.items() if said][0]
    if machine:
        line = words.LESSONS_LINE.format(first=out[0], n=len(out), s="s" * (len(out) != 1), need=f"{filed.get('day', day)} {filed.get('n', '')}".strip())
        with suppress(OSError, ValueError):
            (post or (lambda p: jobs.post_control(state, p, 60)))({"machine": {"room": machine, "text": line, "key": f"doctor-lessons-{day}"}})
    return out


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
    if refresh and not dry and docs.due(state, dict(conf.get("docs") or {}), now):     # daily; a failure tries again next run
        try:
            found.extend(f"{ORANGE} {x}; `yerk docs refresh` tries again" for x in docs.failed(docs.refresh(state, dict(conf.get("docs") or {}), now=now)))
        except Exception as e:                                  # a refresh that cannot run is a line; the checks still run
            found.append(f"{ORANGE} docs: not refreshed, {type(e).__name__}: {words.no_paths(str(e))[:80]}")
    kept = checks(private, state, conf, m, now, last, found, dry)
    if not dry:
        save(state, now, fixed, {"at": now, "findings": found, **kept})
    return fixed + found + kept["weekly"]


def main(private: Path, state: Path, dry: bool = False, refresh: bool = False, scrubbing: bool = False) -> int:
    """A line per repair and finding, each its severity first (red, orange), and the weekly rule-shape line on a Sunday or
    a day a count rose; one green line when there is none. Exits 1 when there was any, 2 when it could not run. With
    `scrubbing`, the scrub alone (scrub), exit 0, 1 when a file could not be scrubbed."""
    try:
        out = scrub(private, state, dry) if scrubbing else run(private, state, dry, refresh=refresh)
        code = int(any(x.startswith(RED) for x in out)) if scrubbing else 1
    except Exception as e:                                      # yerkd.toml unreadable, the state folder gone
        out, code = [f"{RED} doctor failed: {type(e).__name__}: {words.no_paths(str(e))[:120]}"], 2
        if not dry and not scrubbing:
            save(state, time.time(), [], {"at": time.time(), "findings": out})
    for x in out or [f"{words.DONE} {words.DOCTOR_OK}"]:
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
        return "", words.line(f"{words.DONE} {words.DOCTOR_OK}")
    first = found[0][2:] if found[0][:1] in (RED, ORANGE) else found[0]       # a line from before severities has none
    dot = ORANGE if all(x.startswith(ORANGE) for x in found) else RED
    return f"doctor {first}", words.line(f"{dot} " + words.DOCTOR.format(n=len(found), first=first))


def shape_line(state: Path, now: float) -> tuple[str, str] | None:
    """The morning diff's rule-shape line: the last full run's weekly line (a Sunday's, or a day a count rose); none else."""
    d = load(state)
    if now - float(d.get("at", 0)) > DAY or not (weekly := d.get("weekly")):
        return None
    return "", words.line(str(weekly[0]))
