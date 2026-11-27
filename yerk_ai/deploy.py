"""`yerk deploy [REF]`: moves the running daemon onto a commit and proves it is up; when the proof fails, it
moves the daemon back to the commit it ran and proves that. REF must fast-forward from the running commit and
from the branch checked out in the daemon's tree, which moves to it by `merge --ff-only` (a rollback moves it
back with `reset --keep`; a detached tree stays detached); the fast tests run there, each add-on's, then the layer's
recall controls (each through the exact half, a miss once more with the meaning half; one missed deploys nothing), each
behind a hidden worker's wall in a unit of its own (workers.checked: the home hidden, the tree, the interpreter, the layer
and the state read-only, secrets/ gone but the denylist file, the doors shut; the network stays the deploy's own, the
network slice's, not built), a wall that cannot be built deploying nothing ([deploy] wall = false runs them plain, the card
says so; `--check` runs the step alone, touching nothing); the restart waits until no person's turn runs, no room's copy (its compaction off to the side) is younger than
[deploy] copy_seconds, no room has had a message either way (a person's or its own post) for [deploy]
quiet_seconds and the memory index is not rebuilding, and gives up after cap_minutes, the checkout as it was
found. The proof: the unit active, the channel connected, /status answering under the install's secret with the
new commit and every room served before. A failed proof rolls back the same way. Each deploy and rollback is a
ledger row with the commits and the turns cut (never any); the card goes to the machine room, else to the
terminal. `--unit` moves a systemd-run daemon onto units/yerkd.service under its own name, and a failed proof
puts the transient unit back. The unit is [deploy] unit, else the user service whose main process is the running
yerkd's pid (the card names it), else nothing is touched; yerk-index's unit, found from its pid, restarts after
it and the proof wants it on the commit too. Each row is written before its restart. A fault, Ctrl-C, SIGTERM or
SIGHUP from the quiet wait to the proof is never a traceback: once the daemon was stopped or moved it is a
failed proof and the rollback runs; before that, a refusal with the daemon as it was. With no --private and no
$YERK_PRIVATE the private and state folders are the daemon's own, from its unit's command line (the card says
so), else it refuses: the fast tests never read the example layer. Its design: DESIGN 11, L153, L203, L226."""
from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import sysconfig
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterator

from . import addons, jobs, status, words, workers
from .config import EXAMPLE_PRIVATE, REPO, head, load_toml, rooms, write_private
from .ledger import Ledger
from .workers import systemctl, unit_line

FAST = ["tests/test_scrub.py", "tests/test_budget.py", "tests/test_card.py", "tests/test_style.py"]
USER_UNITS = Path.home() / ".config" / "systemd" / "user"      # where `systemctl --user` finds a unit file of the user's
FAILED = re.compile(r"^/\S+:\d+: (?:AssertionError: )?(.+)$", re.M)     # pytest --tb=line: where a test failed, and why
Ask = Callable[[Path], "dict[str, Any] | None"]
Check = Callable[..., "subprocess.CompletedProcess[str]"]   # one check's run: workers.checked, or plain


def git(tree: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=True).stdout.strip()


def ahead(tree: Path, old: str, new: str) -> bool:
    """`new` is `old` or a descendant of it."""
    return subprocess.run(["git", "-C", str(tree), "merge-base", "--is-ancestor", old, new]).returncode == 0


def branch(tree: Path) -> str:
    """The branch checked out in `tree`; "" when it is detached."""
    return subprocess.run(["git", "-C", str(tree), "symbolic-ref", "-q", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()


def move(tree: Path, on: str, commit: str) -> None:
    """The checkout onto `commit`: the branch `on` forward by fast-forward and back with reset --keep (no
    change is ever lost; the merge lane's next `git pull --ff-only` still works), else detached."""
    if not on:
        git(tree, "checkout", "--quiet", "--detach", commit)
    elif ahead(tree, head(tree), commit):
        git(tree, "merge", "--quiet", "--ff-only", commit)
    else:
        git(tree, "reset", "--quiet", "--keep", commit)


def moved_back(tree: Path, on: str, failed: str) -> list[str]:
    """The card after a rollback: where the checkout is, and what the next pull brings back."""
    if not on:
        return [words.line(f"{words.PLAIN} {words.DETACHED.format(commit=head(tree)[:7])}")]
    out = [words.named(f"{words.PLAIN} {words.MOVED_BACK}", on, commit=head(tree)[:7])]
    up = subprocess.run(["git", "-C", str(tree), "rev-parse", "--abbrev-ref", "@{upstream}"], capture_output=True, text=True).stdout.strip()
    if up and (n := int(git(tree, "rev-list", "--count", f"HEAD..{up}"))):
        out.append(words.named(f"{words.PLAIN} {words.AHEAD}", up, n=n))
        if ahead(tree, failed, up):
            out.append(words.line(f"{words.PLAIN} {words.PULL_BRINGS.format(commit=failed[:7])}"))
    return out


def unit_text(template: str, argv: list[str], cwd: str, env: list[str]) -> str:
    """units/yerkd.service with a unit's own command, folder and environment, every value exactly as
    systemd's bus holds it: % doubled (a specifier otherwise), \\ and " escaped inside the quotes, and $
    doubled in the command (variables expand only there)."""
    text = re.sub(r"(?m)^Environment=.*\n", "", template)
    quoted = ["Environment=\"" + e.replace("\\", "\\\\").replace("\"", "\\\"").replace("%", "%%") + "\"\n" for e in env]
    return re.sub(r"(?m)^ExecStart=.*$", lambda _: "".join(quoted) + f"WorkingDirectory={cwd.replace('%', '%%')}\n"
                  + "ExecStart=" + unit_line(*argv), text)


def unit_of(state: Path, proc: Path = Path("/proc"), main_pid: Callable[[str], str] | None = None, of: str = "yerkd.json",
            progs: tuple[bytes, ...] = (b"yerk_ai.daemon", b"yerkd")) -> str:
    """The unit the running yerkd is, from its pid (yerkd.json; yerk-index's with `of` its index.json and `progs` its
    module): the `.service` leaf of its cgroup (/proc/<pid>/cgroup, the unified or name=systemd line) under this
    user's manager (user@<uid>.service, the one `systemctl --user` talks to), without the suffix, and only when that
    unit's MainPID is the pid: a yerkd started inside another program's service (a tmux, a gateway) is in its
    cgroup, and a restart of that unit restarts the other program. "" when no yerkd runs on this state folder, the
    pid is another program's by now, or yerkd is not the main process of a user service."""
    try:
        pid = int(json.loads((state / of).read_text())["pid"])
        argv = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")
        groups = (proc / str(pid) / "cgroup").read_text().splitlines()
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if not any(a.rpartition(b"/")[2] in progs for a in argv):
        return ""                                       # a pid handed to another program since
    for g in groups:
        ctrl, _, path = g.partition(":")[2].partition(":")
        *above, leaf = path.split("/")
        if ctrl in ("", "name=systemd") and leaf.endswith(".service") and leaf != ".service":
            unit = leaf.removesuffix(".service")
            if f"user@{os.getuid()}.service" not in above:
                return ""                               # the system manager's, or another user's
            shown = (main_pid or (lambda u: Systemd(u).show("MainPID")))(unit)
            return unit if shown == str(pid) else ""    # yerkd is a child of the unit's own program
    return ""


def daemon_layer(state: Path, sd: Systemd | None, proc: Path, systemd: Callable[[str], Systemd]) -> tuple[Path, Path] | None:
    """The private and state folders the running yerkd serves: --private and --state on its unit's command line (`sd`,
    else the unit found from its pid; `--private=P` too, a relative one under the unit's folder), its state the folder
    it was found in when no --state is there. None with no unit, no command on the bus, or no --private in it."""
    name = "" if sd else unit_of(state, proc, lambda u: systemd(u).show("MainPID"))
    unit = sd or (systemd(name) if name else None)
    try:
        argv = unit.argv() if unit else []
    except (IndexError, ValueError, OSError, subprocess.SubprocessError):
        argv = []                                       # no ExecStart on the bus
    given: dict[str, str] = {}
    for i, a in enumerate(argv):                        # as argparse reads them: the last one wins
        k, eq, v = a.partition("=")
        if k in ("--private", "--state"):
            given[k] = v if eq else "".join(argv[i + 1:i + 2])
    if not (unit and given.get("--private")):
        return None
    at = Path(unit.show("WorkingDirectory") or ".")
    return at / Path(given["--private"]).expanduser(), at / Path(given["--state"]).expanduser() if given.get("--state") else state


def configured(private: Path) -> set[str]:
    """The rooms.toml rooms with a channel, a Discord one with its channel id (the rooms yerkd serves,
    daemon.run_discord): what the proof wants served when no /status answered before the deploy."""
    out = set()
    for name, conf in rooms(private).items():
        kind, _, cid = str(conf.get("channel") or "").partition(":")
        if kind and (kind != "discord" or cid.isdigit()):
            out.add(name)
    return out


def fault(e: BaseException) -> str:
    """'IndexError in argv': the exception's class and the innermost frame outside the standard
    library, never its message (a failed command's carries its command line and environment). A
    SIGTERM or SIGHUP is its name, at the frame it landed in ('SIGTERM in prove')."""
    std = sysconfig.get_paths()["stdlib"]
    at = [f.name for f in traceback.extract_tb(e.__traceback__) if not f.filename.startswith(std)]
    what = type(e).__name__
    if isinstance(e, KeyboardInterrupt) and at and at[-1] == interrupt.__name__:
        what, at = str(e), at[:-1]
    return words.FAULT.format(what=what, at=at[-1] if at else "?")


def interrupt(signum: int, _frame: Any) -> None:
    """SIGTERM or SIGHUP as Ctrl-C: a KeyboardInterrupt where the deploy is, so the way back runs."""
    raise KeyboardInterrupt(signal.Signals(signum).name)


@contextlib.contextmanager
def interruptible() -> Iterator[None]:
    """SIGTERM and SIGHUP raise KeyboardInterrupt inside (a stopped unit, a closed terminal), and the
    handlers before come back after; off the main thread they stay as they are."""
    before: dict[int, Any] = {}
    with contextlib.suppress(ValueError):               # signal.signal works on the main thread only
        for s in (signal.SIGTERM, signal.SIGHUP):
            before[s] = signal.signal(s, interrupt)
    try:
        yield
    finally:
        for n, h in before.items():
            signal.signal(n, signal.SIG_DFL if h is None else h)   # None: one set outside Python


def warn(text: str) -> None:
    """One line to stderr; a terminal closed under the deploy (SIGHUP) never stops it."""
    with contextlib.suppress(OSError):
        print(text, file=sys.stderr)


class Systemd:
    def __init__(self, unit: str):
        self.unit = unit

    def ctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return systemctl(*args, timeout=180)

    def restart(self) -> bool:
        return self.ctl("restart", self.unit).returncode == 0

    def active(self) -> bool:
        return self.ctl("is-active", self.unit).stdout.strip() == "active"

    def show(self, prop: str) -> str:
        return self.ctl("show", "-p", prop, "--value", self.unit).stdout.strip()

    def busctl(self, prop: str) -> str:
        """A Service property as systemd's bus prints it: each string quoted, exactly as held."""
        path = "/org/freedesktop/systemd1/unit/" + "".join(c if c.isalnum() else f"_{ord(c):02x}" for c in self.unit + ".service")
        return subprocess.run(["busctl", "--user", "get-property", "org.freedesktop.systemd1", path,
                               "org.freedesktop.systemd1.Service", prop], capture_output=True, text=True,
                              timeout=30).stdout

    def argv(self) -> list[str]:
        """The unit's command word by word, from systemd's bus (systemctl show drops the quoting)."""
        w = shlex.split(self.busctl("ExecStart"))         # a(sasbttttuii) 1 path n arg...
        return w[4:4 + int(w[3])]

    def env(self) -> list[str]:
        """The unit's environment from the bus (systemctl show escapes it, and % and $ came back changed)."""
        w = shlex.split(self.busctl("Environment"))       # as n "KEY=value"...
        return w[2:2 + int(w[1])] if len(w) > 1 else []

    def move(self, tree: Path, home: Path, was: dict[str, Any] | None = None) -> dict[str, Any]:
        """The transient unit's command, folder and environment into units/yerkd.service, written 0600
        under ~/.config/systemd/user with the unit's own name; the transient unit stopped, the file's
        started (Type=notify). Returns what `back` needs to put the transient unit back, filled in `was`
        as it goes (`file` once begun, `stopped` just before the stop), so a fault part way still says
        whether anything stopped and leaves `back` what it needs."""
        was = {} if was is None else was
        was.update(argv=self.argv(), cwd=self.show("WorkingDirectory"), env=self.env(), type=self.show("Type"),
                   watchdog=self.show("WatchdogUSec"))
        text = unit_text((tree / "units" / "yerkd.service").read_text(), was["argv"], was["cwd"], was["env"])
        path = home / f"{self.unit}.service"
        path.parent.mkdir(parents=True, exist_ok=True)
        was["file"] = str(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        was["stopped"] = True
        self.ctl("stop", self.unit)
        self.ctl("daemon-reload")
        self.ctl("start", self.unit)                    # Type=notify: back once READY=1, after the port listens
        return was

    def back(self, was: dict[str, Any]) -> bool:
        """The file unit out, the transient unit as it was (its Type and watchdog too); reset-failed
        first, since systemd-run refuses a name a failed stop left loaded."""
        self.ctl("stop", self.unit)
        Path(was["file"]).rename(was["file"] + ".failed")
        self.ctl("daemon-reload")
        self.ctl("reset-failed", self.unit)
        kept = [f"Type={was['type']}"] if was.get("type") else []
        kept += [f"WatchdogSec={was['watchdog']}"] if was.get("watchdog") not in (None, "", "0") else []
        cmd = ["systemd-run", "--user", f"--unit={self.unit}", "-p", "Restart=on-failure", "-p",
               f"WorkingDirectory={was['cwd']}", *[a for k in kept for a in ("-p", k)],
               *[f"--setenv={e}" for e in was["env"]], "--", *was["argv"]]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180).returncode == 0


def quiet(state: Path, ask: Ask, window: float, now: float, blind_ok: bool = False, copy: float = 0.0,
          over: list[str] | None = None) -> tuple[bool, str]:
    """No person's turn runs or waits, no room's copy (its compaction off to the side, which a restart drops) has run under
    `copy` seconds, and no room had a message either way (a person's, or its own post) within `window`; quiet, `over` gets
    a line for each copy older than that. A row with no `copy` (an older yerkd's) has none. A yerkd that runs and does not
    answer is not quiet (unknown), unless `blind_ok`: a rollback of a daemon whose proof failed goes ahead."""
    s = ask(state) if status.alive(state) else {"rooms": []}
    if s is None:
        return blind_ok, words.line(f"{words.PLAIN} {words.NOT_ANSWERING_WHY}")
    if s.get("rebuilding"):                     # the daemon's own restart gate keeps this rule too
        return False, words.line(f"{words.PLAIN} {words.REBUILDING}")
    for r in s["rooms"]:
        if r["busy"] or r["waiting"]:
            return False, words.named(f"{words.PLAIN} {words.TURN_RUNS}", r["room"])
    for r in s["rooms"]:                        # seen live: a deploy dropped a busy room's copy at 62 s
        if r.get("copy") is not None and r["copy"] < copy:
            return False, words.named(f"{words.PLAIN} {words.COPYING}", r["room"], ago=words.span(r["copy"]))
    ledger = Ledger(state / "ledger.sqlite")
    for r in s["rooms"]:
        if now - (last := max(ledger.last_received(r["room"]) or 0.0, ledger.last_said(r["room"]) or 0.0)) < window:
            return False, words.named(f"{words.PLAIN} {words.RECENT}", r["room"], ago=words.span(now - last))
    if over is not None:                        # quiet: a copy past `copy` holds nothing, the restart drops it
        over.extend(words.named(f"{words.NOT_DONE} {words.COPY_DROPPED}", r["room"], ago=words.span(r["copy"]))
                    for r in s["rooms"] if r.get("copy") is not None)
    return True, ""


def indexed(state: Path) -> str:
    """The commit the running yerk-index started on (its index.json says it before the vectors load); "" when none runs."""
    try:
        d = json.loads((state / "memory" / "index.json").read_text())
        os.kill(int(d["pid"]), 0)
        return str(d.get("commit") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def plain(argv: list[str], cwd: Path, tree: Path, private: Path, state: Path, timeout: float | None = None,
          reads: Any = ()) -> subprocess.CompletedProcess[str]:
    """[deploy] wall = false: a check as before the wall, a child of the deploy with its rights and its whole environment, a
    pytest run's with the layer and the tree on its path (the budget test reads the real rooms), the controls' as it is."""
    env = {**os.environ, "YERK_PRIVATE": str(private), "YERK_STATE": str(state), "PYTHONPATH": str(tree)} if argv[1:3] == ["-m", "pytest"] else None
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)


def recall_check(tree: Path, private: Path, state: Path, go: Check = plain) -> dict[str, Any]:
    """The layer's recall controls on the code under test, run in the tree as the fast tests are (memory.controls, through
    `yerk recall-bench --controls`, which writes nothing) by `go`; a check that did not run or printed no result is
    {"broke": True}, and one with no wall to run behind raises workers.NoWall."""
    try:
        t = go([sys.executable, "-m", "yerk_ai.cli", "recall-bench", "--controls", "--private", str(private), "--state", str(state)],
               cwd=tree, tree=tree, private=private, state=state, timeout=180)
        return dict(json.loads(t.stdout.splitlines()[-1]))
    except workers.NoWall:
        raise
    except (OSError, ValueError, TypeError, IndexError, subprocess.SubprocessError):
        return {"broke": True}


def transcripts(private: Path, state: Path) -> list[Path]:
    """Each room's newest Claude Code transcript (context.transcript): the one thing of the home the fast tests read (the
    budget test, Claude Code's parts as the room last sent them; unread, each part counts at its line and nothing is
    measured), so what a walled check is given to read beside the tree, the layer and the state. Only a file that lies in an
    account's projects folder; a layer that cannot be read gives none."""
    from . import context
    from .accounts import load_accounts
    with contextlib.suppress(Exception):
        homes = [a.config_dir.resolve() / "projects" for a in load_accounts(private / "accounts.toml")]
        found = (context.transcript(private, state, r) for r in rooms(private))
        return sorted({t for t in found if t and any(t.resolve().is_relative_to(h) for h in homes)})
    return []


@contextlib.contextmanager
def index_held(state: Path) -> Iterator[None]:
    """The live memory index open read-only out here while the checks run: behind the wall the state folder is read-only, and a
    WAL file's reader there needs its -shm, which a reader out here makes; with none (an index its last writer closed) every
    control would miss. A hold that fails is no hold; the checks run as they would. And the pid yerk-index is alive under,
    seen from out here, in YERK_INDEX_SEEN meanwhile: a walled check's process list is its own, so the recall there would
    read a live index as gone (memory.meaning_state), the bench skipping and every control the meaning half finds missed."""
    from . import memindex, memory
    conn, was = None, os.environ.get(memory.INDEX_SEEN)
    with contextlib.suppress(Exception):
        if (db := memindex.live(state / "memory" / "index.sqlite")).is_file():
            conn = memindex.connect(db, readonly=True)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        os.kill(pid := int(json.loads((state / "memory" / "index.json").read_text())["pid"]), 0)
        os.environ[memory.INDEX_SEEN] = str(pid)
    try:
        yield
    finally:
        os.environ.pop(memory.INDEX_SEEN, None)
        if was is not None:
            os.environ[memory.INDEX_SEEN] = was
        if conn is not None:
            conn.close()


def step(tree: Path, private: Path, state: Path, conf: dict[str, Any], go: Check | None = None,
         check: Callable[[Path, Path, Path], dict[str, Any]] | None = None) -> tuple[str, list[str]]:
    """The deploy's checks on the tree as it stands: the core's fast tests ([deploy] tests, else FAST) in the tree, each
    installed add-on's in its folder, then the recall controls, each behind the wall (workers.checked) unless [deploy] wall =
    false (plain, and the card says so) or `go` stands in, the live index held open meanwhile (index_held) and the tests
    given each room's newest transcript to read (transcripts). Returns the
    outcome a failure ledgers ("" when all held) and the card's lines; no wall to run behind runs nothing more."""
    W, walled = words, conf.get("wall", True) is not False
    go = go or (workers.checked if walled else plain)
    off, reads = [] if walled else [W.line(f"{W.PLAIN} {W.WALL_OFF}")], transcripts(private, state)
    with index_held(state):
        try:
            for name, paths, cwd in [("", list(conf.get("tests", FAST)), tree), *addons.tests(state)]:   # each add-on's too
                if (t := go([sys.executable, "-m", "pytest", "-q", "-x", "--tb=line", *paths], cwd=cwd, tree=tree,
                            private=private, state=state, reads=reads)).returncode:
                    why = [W.line(f"{W.PLAIN} {W.no_paths(x)}") for x in FAILED.findall(t.stdout)[:1]]   # the first failure, in its words
                    return "tests failed", [W.line(f"{W.PLAIN} {W.OF_ADDON.format(name=name) if name else W.TESTS_FAILED}"), *why, *off]
            held, rec = recalled((check or functools.partial(recall_check, go=go))(tree, private, state))   # the layer the daemon serves
        except workers.NoWall as e:
            return "no wall", [W.line(f"{W.PLAIN} {W.NO_WALL}"), W.line(f"{W.PLAIN} {W.no_paths(str(e))}")]
    return "" if held else "recall failed", [W.line(f"{W.DONE} {W.TESTS_PASSED}"), *off, *rec]


def check_step(private: Path, state: Path, *, sd: Systemd | None = None, systemd: Callable[[str], Systemd] = Systemd,
               proc: Path = Path("/proc"), wall: Check | None = None, check: Callable[[Path, Path, Path], dict[str, Any]] | None = None) -> int:
    """`yerk deploy --check`: the step alone, as a deploy runs it (step), on the daemon's tree and layer as a deploy finds them,
    the checkout as it stands: nothing moved, waited for or restarted, no ledger row, no card to the machine room; the step's
    card lines printed. 0 when all held, 1 when not (the example layer refused, as a deploy refuses it)."""
    taken = daemon_layer(state, sd, proc, systemd) if private.resolve() == EXAMPLE_PRIVATE.resolve() else None
    private, state = taken or (private, state)
    if private.resolve() == EXAMPLE_PRIVATE.resolve():
        print(words.line(f"{words.PLAIN} {words.NO_LAYER}"))
        return 1
    conf = dict(load_toml(private / "yerkd.toml").get("deploy", {})) if (private / "yerkd.toml").is_file() else {}
    name = str(conf.get("unit") or "") or ("" if sd else unit_of(state, proc, lambda u: systemd(u).show("MainPID")))
    unit = sd or (systemd(name) if name else None)
    outcome, lines = step(Path(conf.get("tree") or (unit.show("WorkingDirectory") if unit else "") or REPO), private, state, conf, wall, check)
    print("\n".join(lines))
    return 1 if outcome else 0


def recalled(r: dict[str, Any]) -> tuple[bool, list[str]]:
    """(held, the card's lines): every control hit, how many only through the meaning half; else the ones missed."""
    W = words
    if r.get("broke") or r.get("none"):
        return not r.get("broke"), [W.line(f"{W.PLAIN} {W.RECALL_BROKE if r.get('broke') else W.RECALL_NONE.format(why=r['none'])}")]
    said = W.RECALL.format(n=r["hit"], of=r["of"]) + (W.RECALL_BY.format(m=len(r["meaning"])) if r["meaning"] else "")
    missed = [W.named(f"{W.PLAIN} {W.MISSED}", ", ".join(r["missed"]))] if r["missed"] else []
    return not missed, [W.line(f"{W.PLAIN if missed else W.DONE} {said}"), *missed]


def prove(state: Path, sd: Systemd, ask: Ask, served: set[str], want: str, wait: float,
          sleep: Callable[[float], None], unit_file: str = "", index: bool = False) -> tuple[bool, list[str]]:
    """Checked until all hold or `wait` seconds pass; a line per check, its colour first. After the move
    onto `unit_file`, the unit runs from that file as Type=notify (a transient one left loaded fails). With
    `index`, the running line wants yerk-index on the commit too."""
    end = time.monotonic() + wait
    while True:
        s = ask(state) if status.alive(state) else None
        got = {r["room"] for r in (s or {}).get("rooms", [])}
        checks = [(sd.active(), words.PROOF_UP), (bool(s and s.get("connected")), words.PROOF_CONNECTED),
                  (s is not None, words.PROOF_STATUS), (bool(s and s.get("commit") == want) and (not index or indexed(state) == want),
                                                        words.PROOF_COMMIT.format(commit=want[:7])),
                  (served <= got, words.PROOF_ROOMS.format(n=len(served & got), of=len(served)))]
        if unit_file:
            checks.append((sd.show("FragmentPath") == unit_file and sd.show("Type") == "notify", words.PROOF_FILE))
        if (ok := all(c for c, _ in checks)) or time.monotonic() >= end:
            return ok, [words.line(f"{words.DONE if c else words.NOT_DONE} {say}") for c, say in checks]
        sleep(2.0)


def timers(tree: Path, home: Path, ctl: Callable[..., subprocess.CompletedProcess[str]] = systemctl) -> list[str]:
    """Each timer in the tree's units/ that systemd does not know (`is-enabled`: not-found) copied with its service into
    `home` as shipped (0600; a service file already there is kept), one daemon-reload, then `enable --now`: a card line
    each. Its comment once said to copy it by hand, and yerk-curate.timer never was (2026-10-24). One the owner installed
    and turned off stays off; the doctor names it."""
    names = sorted(p.name for p in (tree / "units").glob("*.timer"))
    new = [t for t, s in zip(names, ctl("is-enabled", *names).stdout.split() if names else []) if s == "not-found"]
    try:
        for f in (tree / "units" / x for t in new for x in (t, t.removesuffix(".timer") + ".service")):
            if f.is_file() and not (home / f.name).exists():
                home.mkdir(parents=True, exist_ok=True)
                write_private(home / f.name, f.read_bytes())
    except OSError as e:
        return [words.line(f"{words.NOT_DONE} timers not installed: {type(e).__name__}")]
    if new:
        ctl("daemon-reload")
    return [words.line(f"{words.NOT_DONE} {t} not enabled" if ctl("enable", "--now", t).returncode else f"{words.DONE} {t} installed, on")
            for t in new]


def cut(ledger: Ledger, opened: set[str], until: float) -> int:
    """A person's messages a restart left taken and not answered (the turns it cut): open once it
    returned at `until` and received before then, and not already open at the last quiet check (a row a
    crash or a dropped room left open long before is not this deploy's)."""
    return len([r for r in ledger.inbound_open() if r["sender"] != "yerk" and str(r["message_id"]) not in opened and r["received"] < until])


def run(private: Path, state: Path, ref: str = "", tests: bool = True, unit_file: bool = False, *,
        sd: Systemd | None = None, ask: Ask = status.ask, post: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep, home: Path = USER_UNITS,
        systemd: Callable[[str], Systemd] = Systemd, proc: Path = Path("/proc"),
        check: Callable[[Path, Path, Path], dict[str, Any]] | None = None,
        ctl: Callable[..., subprocess.CompletedProcess[str]] = systemctl, wall: Check | None = None) -> int:
    """0 deployed and proven, and then each timer in units/ not installed yet installed and on (timers); 1 rolled back or
    not deployed (the card says which and why); 2 the rollback failed or never ran, so the daemon may run a commit no proof
    passed (the morning diff says it again); 130 stopped by Ctrl-C, SIGTERM or SIGHUP, and rolled back or nothing stopped."""
    taken = daemon_layer(state, sd, proc, systemd) if private.resolve() == EXAMPLE_PRIVATE.resolve() else None
    private, state = taken or (private, state)        # no --private, no $YERK_PRIVATE: the layer the daemon serves
    conf = dict(load_toml(private / "yerkd.toml").get("deploy", {})) if (private / "yerkd.toml").is_file() else {}
    W, ledger = words, Ledger(state / "ledger.sqlite")
    named = str(conf.get("unit") or "")
    found = "" if named or sd else unit_of(state, proc, lambda u: systemd(u).show("MainPID"))
    ix = unit_of(state, proc, lambda u: systemd(u).show("MainPID"), "memory/index.json", (b"yerk_ai.memindex",))
    was = ask(state) if status.alive(state) else None
    if (example := private.resolve() == EXAMPLE_PRIVATE.resolve()) or not (sd or named or found):   # refused before anything is touched
        reason = W.line(f"{W.PLAIN} {W.NO_LAYER if example else W.NO_UNIT}")
        ledger.deploy(kind="deploy", unit="", before=str((was or {}).get("commit") or ""), after="", outcome="refused", cut=0, note=reason)
        return say(private, state, [W.line(f"{W.NOT_DONE} {W.NOT_DEPLOYED}"), reason], post)
    sd = sd or systemd(named or found)
    tree = Path(conf.get("tree") or sd.show("WorkingDirectory") or REPO)
    window, cap = float(conf.get("quiet_seconds", 240)), float(conf.get("cap_minutes", 60)) * 60
    copying = float(conf.get("copy_seconds", 300))   # a room's copy compacting holds the restart this long at most
    wait = float(conf.get("proof_seconds", 60))
    before = str((was or {}).get("commit") or head(tree))
    served = {r["room"] for r in was.get("rooms", [])} if was else configured(private)
    card: list[str] = []
    moved: dict[str, Any] = {}
    unit = [W.line(f"{W.PLAIN} {W.THE_DAEMONS}")] if taken else []
    unit += [W.named(f"{W.PLAIN} {W.UNIT_FOUND}", found)] if found else []
    row: dict[str, int] = {}                            # kind: its row, once settle wrote it before the restart
    stopped: list[BaseException] = []                   # a Ctrl-C, SIGTERM or SIGHUP caught on the way

    def tell(lines: list[str], code: int = 1) -> int:
        return say(private, state, [*lines[:1], *unit, *lines[1:]], post, code)

    def ledgered(kind: str, **cols: Any) -> list[str]:
        """The row written, or set again once settle wrote it; a failed write is a stderr line and a card
        line, never a traceback that skips the rollback or the card."""
        try:
            row[kind] = ledger.deploy(row.get(kind, 0), kind=kind, unit=sd.unit, **cols)
            return []
        except Exception as e:
            warn(f"deploy: the {kind} row was not written: {fault(e)}")
            return [W.line(f"{W.NOT_DONE} {W.NO_ROW.format(kind=kind)}")]

    def no(outcome: str, *why: str, after: str = "") -> int:
        lost = ledgered("deploy", before=before, after=after, outcome=outcome, cut=0, note="; ".join(why))
        return tell([W.line(f"{W.NOT_DONE} {W.NOT_DEPLOYED}"), *why, *lost], 130 if stopped else 1)

    def settle(kind: str, want: str, came_from: str, restart: Callable[[], Any]) -> tuple[bool, list[str], str]:
        """Wait for quiet (or the cap), then the restart and the proof: (proven, why it never restarted,
        the outcome). The row is written before the restart (outcome "begun", so even a SIGKILL leaves
        one the morning diff names; a deploy whose row cannot be written never restarts) and set again
        after the proof. A fault, Ctrl-C, SIGTERM or SIGHUP from the quiet wait to the proof is never a
        traceback: once the daemon may be stopped or moved (a restart begun, the move past its stop) it
        is the outcome "fault", its line where the checks go, and the way back runs; before that, a
        reason and nothing stopped. The cut is counted only once the restart returned."""
        end = time.monotonic() + cap
        n, ok, outcome, begun, counted = 0, False, "", False, False
        lines: list[str] = []
        lost: list[str] = []
        over: list[str] = []                            # a copy older than copy_seconds, the restart went over it
        try:
            with interruptible():
                while not (q := quiet(state, ask, window, time.time(), blind_ok=kind == "rollback", copy=copying, over=over))[0]:
                    if time.monotonic() >= end:
                        return False, [W.line(f"{W.PLAIN} {W.NOT_QUIET.format(span=W.span(cap))}"), q[1]], "gave up"
                    sleep(5.0)
                opened = {str(r["message_id"]) for r in ledger.inbound_open()}   # left open before: not this restart's
                if kind == "deploy":                    # no row, no restart
                    row[kind] = ledger.deploy(kind=kind, unit=sd.unit, before=came_from, after=want, outcome="begun")
                else:                                   # a rollback runs with or without its row
                    lost = ledgered(kind, before=came_from, after=want, outcome="begun")
                begun = True
                restart()
                if ix:                                  # yerk-index next, on the same code (it ran an older mentions.py for 4 h, 2026-10-20)
                    systemd(ix).restart()
                n, counted = cut(ledger, opened, time.time()), True
                ok, lines = prove(state, sd, ask, served, want, wait, sleep, moved.get("file", "") if kind == "deploy" else "", bool(ix))
        except BaseException as e:                      # KeyboardInterrupt too: the way back runs, then exit 130
            if not isinstance(e, Exception):
                stopped.append(e)
            if not begun or kind == "deploy" and unit_file and not moved.get("stopped"):   # nothing stopped
                if kind == "deploy":
                    if moved.get("file"):
                        with contextlib.suppress(OSError):
                            os.rename(moved["file"], moved["file"] + ".failed")
                    moved.clear()
                return False, [W.line(f"{W.PLAIN} {fault(e)}"), W.line(f"{W.PLAIN} {W.NOTHING_STOPPED}")], "fault"
            lines, outcome = [W.line(f"{W.NOT_DONE} {fault(e)}")], "fault"
        said = (W.CUT.format(n=n) if n else W.NO_CUT) if counted else W.NOT_COUNTED
        card.extend(lines + [W.line(f"{W.NOT_DONE if n or not counted else W.PLAIN} {said}")] + over)
        outcome = outcome or ("proven" if ok else "proof failed")
        card.extend(lost + ledgered(kind, before=came_from, after=want, outcome=outcome, cut=n if counted else None,
                                    proof="; ".join(lines)))
        return ok, [], outcome

    try:
        target = git(tree, "rev-parse", f"{ref or 'HEAD'}^{{commit}}")
    except subprocess.CalledProcessError:
        return no("refused", W.line(f"{W.PLAIN} {W.NO_REF.format(ref=ref)}"))
    if git(tree, "status", "--porcelain", "--untracked-files=no"):
        return no("refused", W.line(f"{W.PLAIN} {W.DIRTY}"), after=target)
    on, found = branch(tree), head(tree)              # the checkout as found, put back by every way out that deploys nothing
    if not ahead(tree, before, target):
        return no("refused", W.line(f"{W.PLAIN} {W.NO_FF.format(ref=target[:7], commit=before[:7])}"), after=target)
    if on and not ahead(tree, found, target):
        return no("refused", W.named(f"{W.PLAIN} {W.NOT_AHEAD}", on, ref=target[:7]), after=target)
    move(tree, on, target)
    outcome, tested = step(tree, private, state, conf, wall, check) if tests else ("", [W.line(f"{W.PLAIN} {W.TESTS_SKIPPED}")])
    if outcome:                                       # the fast tests, each add-on's and the live controls, behind the wall
        move(tree, on, found)
        return no(outcome, *tested, after=target)
    where = [] if on else [W.line(f"{W.PLAIN} {W.DETACHED.format(commit=target[:7])}")]
    ok, why, outcome = settle("deploy", target, before, (lambda: sd.move(tree, home, moved)) if unit_file else sd.restart)
    if why:
        move(tree, on, found)
        return no(outcome, *why, *tested, after=target)
    if ok:                                            # the proven commit's timers: one units/ has and the box does not, on
        return tell([W.line(f"{W.DONE} {W.DEPLOYED.format(commit=target[:7])}"), *tested, *card, *where, *timers(tree, home, ctl)], 0)
    first = [W.line(f"{W.NOT_DONE} {(W.BROKE_OFF if outcome == 'fault' else W.FAILED_PROOF).format(commit=target[:7])}"), *tested, *card]
    card.clear()

    def restart_before() -> bool:
        move(tree, on, before)                        # the checkout moves back only when the restart comes
        return sd.back(moved) if moved.get("stopped") else sd.restart()
    back, why, how = settle("rollback", before, target, restart_before)
    if why:                                           # never quiet, or a fault first: the daemon and the checkout stay on the target
        lost = ledgered("rollback", before=target, after=before, outcome=how, cut=0, note="; ".join(why))
        return tell([*first, W.line(f"{W.NOT_DONE} {W.ROLLBACK_NEVER.format(commit=before[:7])}"), *why, *lost, *where], 2)
    return tell([*first, W.line(f"{W.DONE if back else W.NOT_DONE} "
                                f"{(W.ROLLED_BACK if back else W.ROLLBACK_FAILED).format(commit=before[:7])}"),
                 *card, *moved_back(tree, on, target)], (130 if stopped else 1) if back else 2)


def say(private: Path, state: Path, lines: list[str], post: Callable[[dict[str, Any]], dict[str, Any]] | None, code: int = 1) -> int:
    """The card to the machine room through the running yerkd, and always to the terminal."""
    text = "\n".join(lines)
    with contextlib.suppress(OSError):                # a terminal closed under it: the machine room still gets it
        print(text)
    if (machine := [n for n, c in rooms(private).items() if c.get("role") == "machine"]):
        req = {"machine": {"room": machine[0], "text": text, "key": f"deploy-{int(time.time())}"}}
        try:
            out = (post or (lambda p: jobs.post_control(state, p, 60)))(req)
        except (OSError, ValueError) as e:
            out = {"ok": False, "error": type(e).__name__}
        if not out.get("ok"):
            warn(f"deploy: the card did not reach {machine[0]}: {out.get('error')}")
    return code
