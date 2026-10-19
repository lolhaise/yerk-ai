"""`yerk deploy [REF]`: moves the running daemon onto a commit and proves it is up; when the proof fails, it
to the one it ran, proven. REF is checked out detached in the daemon's tree (no branch ever moves)
only when it fast-forwards from the running commit; the fast tests run there; the restart waits until
no person's turn runs and no room has had a person's message for [deploy] quiet_seconds, and gives
up after cap_minutes. The proof: the unit active, the channel connected, /status answering under the
found. The proof: the unit active, the channel connected, /status answering under the install's secret with the
new commit and every room served before. A failed proof rolls back the same way. Each deploy and rollback is a
ledger row with the commits and the turns cut (never any); the card goes to the machine room, else to the
units/yerkd.service under its own name, and a failed proof puts the transient unit back."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import jobs, status, words
from .config import REPO, load_toml, rooms
from .ledger import Ledger

FAST = ["tests/test_scrub.py", "tests/test_budget.py", "tests/test_card.py", "tests/test_style.py"]
Ask = Callable[[Path], "dict[str, Any] | None"]


def git(tree: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=True).stdout.strip()


def head(tree: Path) -> str:
    """The commit checked out in `tree`; "" outside a git checkout."""
    try:
        return git(tree, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return ""


class Systemd:
    def __init__(self, unit: str):
        self.unit = unit

    def ctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=180)

    def restart(self) -> bool:
        return self.ctl("restart", self.unit).returncode == 0

    def active(self) -> bool:
        return self.ctl("is-active", self.unit).stdout.strip() == "active"

    def show(self, prop: str) -> str:
        return self.ctl("show", "-p", prop, "--value", self.unit).stdout.strip()

    def move(self, tree: Path, home: Path) -> dict[str, Any]:
        """The transient unit's command, folder and environment into units/yerkd.service, written 0600
        under ~/.config/systemd/user with the unit's own name; the transient unit stopped, the file's
        started (Type=notify). Returns what `back` needs to put the transient unit back."""
        was = {"argv": shlex.split(re.search(r"argv\[\]=([^;]*)", self.show("ExecStart")).group(1)),  # type: ignore[union-attr]
               "cwd": self.show("WorkingDirectory"), "env": shlex.split(self.show("Environment"))}
        text = re.sub(r"(?m)^Environment=.*\n", "", (tree / "units" / "yerkd.service").read_text())
        text = re.sub(r"(?m)^ExecStart=.*$", lambda _: "".join(f"Environment={shlex.quote(e)}\n" for e in was["env"])
                      + f"WorkingDirectory={was['cwd']}\nExecStart={shlex.join(was['argv'])}", text)
        path = home / f"{self.unit}.service"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.ctl("stop", self.unit)
        self.ctl("daemon-reload")
        self.ctl("start", self.unit)                    # Type=notify: back once READY=1, after the port listens
        return {**was, "file": str(path)}

    def back(self, was: dict[str, Any]) -> bool:
        """The file unit out, the transient unit as it was."""
        self.ctl("stop", self.unit)
        Path(was["file"]).rename(was["file"] + ".failed")
        self.ctl("daemon-reload")
        cmd = ["systemd-run", "--user", f"--unit={self.unit}", "-p", "Restart=on-failure", "-p",
               f"WorkingDirectory={was['cwd']}", *[f"--setenv={e}" for e in was["env"]], "--", *was["argv"]]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180).returncode == 0


def quiet(state: Path, ask: Ask, window: float, now: float, blind_ok: bool = False) -> tuple[bool, str]:
    """No person's turn runs or waits, and no room had a person's message within `window`. A yerkd that
    runs and does not answer is not quiet (unknown), unless `blind_ok`: a rollback of a daemon whose
    proof failed goes ahead."""
    s = ask(state) if status.alive(state) else {"rooms": []}
    if s is None:
        return blind_ok, words.line(f"{words.PLAIN} {words.NOT_ANSWERING_WHY}")
    for r in s["rooms"]:
        if r["busy"] or r["waiting"]:
            return False, words.named(f"{words.PLAIN} {words.TURN_RUNS}", r["room"])
    ledger = Ledger(state / "ledger.sqlite")
    for r in s["rooms"]:
        if now - (last := ledger.last_received(r["room"]) or 0.0) < window:
            return False, words.named(f"{words.PLAIN} {words.RECENT}", r["room"], ago=words.span(now - last))
    return True, ""


def prove(state: Path, sd: Systemd, ask: Ask, served: set[str], want: str, wait: float,
          sleep: Callable[[float], None]) -> tuple[bool, list[str]]:
    """Checked until all hold or `wait` seconds pass; a line per check, its colour first."""
    end = time.monotonic() + wait
    while True:
        s = ask(state) if status.alive(state) else None
        got = {r["room"] for r in (s or {}).get("rooms", [])}
        checks = [(sd.active(), words.PROOF_UP), (bool(s and s.get("connected")), words.PROOF_CONNECTED),
                  (s is not None, words.PROOF_STATUS), (bool(s and s.get("commit") == want),
                                                        words.PROOF_COMMIT.format(commit=want[:7])),
                  (served <= got, words.PROOF_ROOMS.format(n=len(served & got), of=len(served)))]
        ok = all(c for c, _ in checks)
        if ok or time.monotonic() >= end:
            return ok, [words.line(f"{words.DONE if c else words.NOT_DONE} {say}") for c, say in checks]
        sleep(2.0)


def cut(ledger: Ledger, at: float) -> int:
    """A person's messages a restart at `at` left taken and not answered (the turns it cut)."""
    return len([r for r in ledger.inbound_open() if r["sender"] != "yerk" and at - 3600 < r["received"] < at])


def run(private: Path, state: Path, ref: str = "", tests: bool = True, unit_file: bool = False, *,
        sd: Systemd | None = None, ask: Ask = status.ask, post: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep, home: Path = Path.home() / ".config" / "systemd" / "user") -> int:
    """0 deployed and proven; 1 rolled back or not deployed (the card says which and why)."""
    conf = dict(load_toml(private / "yerkd.toml").get("deploy", {})) if (private / "yerkd.toml").is_file() else {}
    sd = sd or Systemd(str(conf.get("unit", "yerkd")))
    tree = Path(conf.get("tree") or sd.show("WorkingDirectory") or REPO)
    window, cap = float(conf.get("quiet_seconds", 240)), float(conf.get("cap_minutes", 60)) * 60
    wait, W = float(conf.get("proof_seconds", 60)), words
    ledger = Ledger(state / "ledger.sqlite")
    was = ask(state) if status.alive(state) else None
    before = str((was or {}).get("commit") or head(tree))
    served = {r["room"] for r in (was or {}).get("rooms", [])}
    card: list[str] = []

    def no(outcome: str, *why: str, after: str = "") -> int:
        ledger.deploy(kind="deploy", unit=sd.unit, before=before, after=after, outcome=outcome, cut=0,
                      note="; ".join(why))
        return say(private, state, [W.line(f"{W.NOT_DONE} {W.NOT_DEPLOYED}"), *why], post)

    def settle(kind: str, want: str, came_from: str, restart: Callable[[], Any]) -> tuple[bool, list[str]]:
        """Wait for quiet (or the cap), then the restart and the proof: (proven, why it never restarted)."""
        end = time.monotonic() + cap
        while not (q := quiet(state, ask, window, time.time(), blind_ok=kind == "rollback"))[0]:
            if time.monotonic() >= end:
                return False, [W.line(f"{W.PLAIN} {W.NOT_QUIET.format(span=W.span(cap))}"), q[1]]
            sleep(5.0)
        at = time.time()
        restart()
        n = cut(ledger, at)
        ok, lines = prove(state, sd, ask, served, want, wait, sleep)
        card.extend(lines + [W.line(f"{W.NOT_DONE if n else W.PLAIN} " + (W.CUT.format(n=n) if n else W.NO_CUT))])
        ledger.deploy(kind=kind, unit=sd.unit, before=came_from, after=want,
                      outcome="proven" if ok else "proof failed", cut=n, proof="; ".join(lines))
        return ok, []

    try:
        target = git(tree, "rev-parse", f"{ref or 'HEAD'}^{{commit}}")
    except subprocess.CalledProcessError:
        return no("refused", W.line(f"{W.PLAIN} {W.NO_REF.format(ref=ref)}"))
    if git(tree, "status", "--porcelain", "--untracked-files=no"):
        return no("refused", W.line(f"{W.PLAIN} {W.DIRTY}"), after=target)
    if subprocess.run(["git", "-C", str(tree), "merge-base", "--is-ancestor", before, target]).returncode:
        return no("refused", W.line(f"{W.PLAIN} {W.NO_FF.format(ref=target[:7], commit=before[:7])}"), after=target)
    git(tree, "checkout", "--quiet", "--detach", target)
    if tests and subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", *conf.get("tests", FAST)], cwd=tree,
                                capture_output=True).returncode:
        git(tree, "checkout", "--quiet", "--detach", before)
        return no("tests failed", W.line(f"{W.PLAIN} {W.TESTS_FAILED}"), after=target)
    tested = W.line(f"{W.DONE if tests else W.PLAIN} {W.TESTS_PASSED if tests else W.TESTS_SKIPPED}")
    moved: dict[str, Any] = {}
    ok, why = settle("deploy", target, before, (lambda: moved.update(sd.move(tree, home))) if unit_file else sd.restart)
    if why:
        git(tree, "checkout", "--quiet", "--detach", before)
        return no("gave up", *why, tested, after=target)
    if ok:
        return say(private, state, [W.line(f"{W.DONE} {W.DEPLOYED.format(commit=target[:7])}"), tested, *card], post, 0)
    first = [W.line(f"{W.NOT_DONE} {W.FAILED_PROOF.format(commit=target[:7])}"), tested, *card]
    card.clear()
    git(tree, "checkout", "--quiet", "--detach", before)
    back, _ = settle("rollback", before, target, (lambda: sd.back(moved)) if moved else sd.restart)
    return say(private, state, [*first, W.line(f"{W.DONE if back else W.NOT_DONE} "
                                               f"{(W.ROLLED_BACK if back else W.ROLLBACK_FAILED).format(commit=before[:7])}"),
                                *card], post)


def say(private: Path, state: Path, lines: list[str], post: Callable[[dict[str, Any]], dict[str, Any]] | None,
        code: int = 1) -> int:
    """The card to the machine room through the running yerkd, and always to the terminal."""
    text = "\n".join(lines)
    print(text)
    machine = [n for n, c in rooms(private).items() if c.get("role") == "machine"]
    if machine:
        req = {"machine": {"room": machine[0], "text": text, "key": f"deploy-{int(time.time())}"}}
        try:
            out = (post or (lambda p: jobs.post_control(state, p, 60)))(req)
        except (OSError, ValueError) as e:
            out = {"ok": False, "error": type(e).__name__}
        if not out.get("ok"):
            print(f"deploy: the card did not reach {machine[0]}: {out.get('error')}", file=sys.stderr)
    return code
