"""The gates: rules in code that check each tool call a room or a worker makes before it runs, and hold, rewrite or refuse the
risky ones. Code decides, so they block (DESIGN 8; decisions 8 and 18). A judge-model gate never blocks until it has proven
known-good control passing).

PreToolUse, first answer wins, shell text parsed quote-aware before any pattern runs (a `rm` in
quotes, in a heredoc body or as a word in a string is not a delete; lifted/shellguard.py):
- hardline: a command that never runs (a delete of the root, home or a system folder, mkfs, dd onto
  a disk, a fork bomb, a shutdown; lifted/hardline.py) is denied and no approval releases it;
- trash: a plain `rm` or `unlink` of named paths is rewritten to the trash command with the same
  paths and runs; the room is not told. A delete in any other shape is denied with the trash line
  for the model, and one outside the work folder with the way an owner room has (the delete tool);
  a trash command that then fails is told to the room in one line;
- restart: a write to a file yerkd reads at start (yerkd.toml, rooms.toml, accounts.toml) or a
  restart of yerkd is refused while the memory index rebuilds or a human turn is running, with one
  plain line to the room;
- one reading: the approval and restart rules read a shell command as the delete rules do, as what would really run
  (runs, unwrap), so a held command is held behind a wrapper, in a nested shell or by its full path too;
- approval: an action named external (a send to another room, an owner room's line to the machine
  room excepted, a public post, a login, money, a
  message out through another tool, a text or an email in the owner's name, a delete for good outside the work folder, or a rule in
  yerkd.toml [gates.external]) is held, one plain
  line to the room, until an approver says the word in the room's running turn; then that same call
  (its normalised text, and a shell command's working folder) runs once, and any other call is held again.
Every hold, release, refusal and rewrite is a row in the ledger's `gates` table.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import unwrap, words
from .lifted import shellguard
from .lifted.hardline import hardline

RESTART_FILES = ("yerkd.toml", "rooms.toml", "accounts.toml")     # read once, when yerkd starts
RESTART_BASH = re.compile(r"^(?:systemctl\s+--user\s+(?:restart|stop|kill|try-restart|reload-or-restart)\s+yerkd"
                          r"|(?:pkill|killall)\b.*\byerk"
                          r"|kill\b.*\$\(\s*(?:pgrep|pidof)\b[^)]*yerk)", re.I)
RESTART_VERBS = frozenset({"restart", "stop", "kill", "try-restart", "reload-or-restart", "try-reload-or-restart", "condrestart",
                           "force-reload"})     # systemctl's words that end or replace a unit's process
WRITE_OPS = re.compile(r">>?|\btee\b|\bsed\s+(?:-[^\s]*\s+)*-i|\bcp\b|\bmv\b|\btruncate\b|\bdd\b|\bperl\s+-[^\s]*i")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
EXTERNAL: dict[str, dict[str, str]] = {
    "send-elsewhere": {"tool": r"^mcp__yerk__send$", "room": "other", "what": "a send to another room"},
    "public-post": {"tool": r"^Bash$", "what": "a public post",
                    "command": r"^(?:git\s+(?:(?:-C|-c|--git-dir|--work-tree)\s+\S+\s+|--?[\w-]+(?:=\S+)?\s+)*push"
                               r"|gh\s+(?:-R\s+\S+\s+)?(?:issue|pr|release|gist|repo)\s+(?:create|comment|edit|delete"
                               r"|merge|close|fork)|npm\s+publish|twine\s+upload|cargo\s+publish|docker\s+push)\b"},
    "login": {"tool": r"^Bash$", "what": "a login",
              "command": r"^(?:gh\s+auth\s+login|docker\s+login|npm\s+(?:login|adduser)|ssh-copy-id|gcloud\s+auth"
                         r"\s+login|az\s+login|aws\s+configure|claude\s+(?:login|setup-token))\b"},
    "money": {"tool": r"(?i)(?:^|_)(?:pay|payment|purchase|checkout|buy|transfer|invoice)(?:_|$)", "what": "money"},
    "money-cli": {"tool": r"^Bash$", "command": r"^stripe\b", "what": "money"},
    "message-out": {"tool": r"(?i)^mcp__(?!yerk__).*(?:send|post|publish|tweet|email|message|reply)", "what":
                    "a message to someone"},
    "text-out": {"tool": r"^mcp__yerk__text$", "what": "a text in your name"},     # the preview: off, no text goes (texts.py)
    "email-out": {"tool": r"^mcp__yerk__email$", "what": "an email in your name"},  # the same: off, no email goes (emails.py)
    "delete-outside": {"tool": r"^mcp__yerk__delete$", "what": "a delete for good"},   # its paths shown: off, none goes (deletes.py)
}


@dataclass
class Settings:
    approvers: tuple[str, ...] = ("owner",)      # senders whose word releases a hold
    word: str = "approve"
    window_s: float = 1800.0                    # a word older than this releases nothing
    trash: tuple[str, ...] = ()                 # the trash command; () finds one
    external: dict[str, dict[str, str]] = field(default_factory=lambda: dict(EXTERNAL))
    retired: tuple[str, ...] = ()               # marks a final in a room without people may not hold (Room.retired_check)


def settings(conf: dict[str, Any] | None) -> Settings:
    """yerkd.toml [gates]: approvers, word, approve_minutes, trash (a command line), off (default
    rule names to drop), [gates.external.NAME] tool, command and what, and retired_marks (none by default; each
    without its emoji variation selector, so either way of writing a mark matches)."""
    c = dict(conf or {})
    if not isinstance(marks := c.get("retired_marks", []), list) or not all(isinstance(m, str) for m in marks):
        raise SystemExit('yerkd.toml: [gates] retired_marks must be a list of marks, like retired_marks = ["☐", "🟦"]')
    ext = {k: v for k, v in EXTERNAL.items() if k not in set(c.get("off", []))}
    for name, rule in dict(c.get("external", {})).items():
        ext[str(name)] = {k: str(v) for k, v in dict(rule).items()}
    trash = tuple(shlex.split(str(c["trash"]))) if c.get("trash") else find_trash()
    return Settings(approvers=tuple(c.get("approvers", ["owner"])), word=str(c.get("word", "approve")).lower(),
                    window_s=float(c.get("approve_minutes", 30)) * 60, trash=trash, external=ext,
                    retired=tuple(x for x in (m.replace("\ufe0f", "").strip() for m in marks) if x))


def find_trash() -> tuple[str, ...]:
    if shutil.which("trash"):
        return ("trash",)
    if shutil.which("gio"):
        return ("gio", "trash")
    return ()


@dataclass
class Verdict:
    gate: str
    decision: str                      # rewrite, deny, hold, release
    reason: str = ""                   # what the model is told
    line: str = ""                     # what the room is told ("": nothing)
    detail: str = ""                   # the ledger's words
    new_input: dict[str, Any] | None = None
    rule: str = ""

    def output(self) -> dict[str, Any]:
        out: dict[str, Any] = {"hookEventName": "PreToolUse"}
        if self.decision == "rewrite":
            out["updatedInput"] = self.new_input     # no allow: the room's permission rules still decide
            out["additionalContext"] = self.reason
        elif self.decision in ("deny", "hold"):
            out["permissionDecision"] = "deny"
            out["permissionDecisionReason"] = self.reason
        else:
            return {}
        return {"hookSpecificOutput": out}


def mask(segment: str) -> str:
    """Quoted text blanked, quote marks kept: what is left is what the shell treats as syntax."""
    spans = shellguard.quoted_spans(segment)
    return "".join(" " if any(a < i < b - 1 for a, b in spans) else ch for i, ch in enumerate(segment))


def shell(command: str) -> list[tuple[str, list[str], str]]:
    """(segment, argv, normalized) for every simple command of a command line."""
    out = []
    for seg in shellguard.segments(command):
        argv = shellguard.argv_of(seg)
        out.append((seg, argv, shlex.join(argv) if argv else ""))
    return out


def runs(command: str) -> list[str]:
    """Every simple command the text would really run, one line each, read as the delete rules read it (unwrap): wrappers
    peeled, the strings a nested shell, eval, $(...) and backquotes run read again, a command behind xargs with the words
    written after it, and the program by its name alone (`/usr/bin/git` is `git`). So one reading serves every gate, and a
    held command is held behind `timeout`, in `bash -c`, after `then` or by its full path. [] when the text cannot be read
    for certain: check_shell has refused that command already."""
    return [shlex.join([os.path.basename(argv[0]), *argv[1:]]) for argv in unwrap.unwrap(command, whole=True) or [] if argv]


def restarts(argv: list[str]) -> bool:
    """One command that would stop or restart yerkd: systemctl with one of RESTART_VERBS and a unit named yerkd after it,
    wherever its options sit (`systemctl --user --no-block restart yerkd-test`, `systemctl restart --user yerkd`); pkill or
    killall with a word naming yerk."""
    name, args = (os.path.basename(argv[0]), argv[1:]) if argv else ("", [])
    if name == "systemctl":
        said = [a.lower() for a in args if not a.startswith("-")]
        verb = next((k for k, a in enumerate(said) if a in RESTART_VERBS), None)
        return verb is not None and any(a.startswith("yerkd") for a in said[verb + 1:])
    return name in ("pkill", "killall") and any("yerk" in a.lower() for a in args)


def is_delete(argv: list[str]) -> bool:
    return bool(argv) and os.path.basename(argv[0]) in ("rm", "unlink")


# Programs that run their argument as a command where the gate cannot see it (unwrap peels the plain
# wrappers; these are the rest that exist on a Linux box), the ones that move the working folder, and
# the ordinary tools whose delete, inside the work folder, runs as written.
_RUNNERS = {"taskset", "ionice", "stdbuf", "flock", "chrt", "systemd-run", "screen", "tmux", "script", "busybox",
            "watch", "unbuffer", "strace", "ltrace", "gdb", "valgrind", "chronic", "parallel", "at", "batch"}
_MOVERS = {"cd", "pushd", "popd"}
_PASS_INSIDE = {"git", "rmdir", "rsync", "zip"}


def check_shell(command: str, cwd: str, s: Settings, root: str = "", way: str = "") -> Verdict | None:
    """hardline, then the delete rules on what would really run (unwrap), for one Bash command: `cwd`
    is where it runs, `root` the room's work folder (default `cwd`) that a delete must stay under, `way`
    what the refusal of a delete outside it adds (an owner room's: the delete tool)."""
    parts = shell(command)
    whole = mask(shellguard.strip_heredocs(command))           # a redirect or a fork bomb spans separators
    for seg, argv, norm in [("", [], ""), *parts]:
        what = hardline(norm) if seg else hardline(whole, anchored=False)
        if what:
            return Verdict("hardline", "deny", f"Refused: this is {what}, which never runs here, with or without "
                                               "approval. Do not try another way.", detail=f"{what}: {(seg or command)[:200]}")
    cmds = unwrap.unwrap(command)
    if cmds is None:
        return Verdict("trash", "deny", "Refused: this command could not be read for certain (an open quote, a command "
                                        "built at run time, or nesting too deep). Run it plainly.",
                       detail=f"unreadable: {command[:200]}")
    runner = next((argv[0] for argv in cmds if argv and os.path.basename(argv[0]) in _RUNNERS), "")
    if runner:
        return Verdict("trash", "deny", f"Refused: `{runner}` runs its command where this gate cannot see it. "
                                        "Run the command without it.", detail=f"runner {runner}: {command[:200]}")
    deletes: list[tuple[list[str], list[str]]] = []
    for argv in cmds:
        targets = unwrap.delete_targets(argv, cwd)
        if targets is None:
            return Verdict("trash", "deny", "Refused: a delete here names a path this gate cannot resolve (a variable, "
                                            "a glob, a `~`, or no path at all). Run it on its own with named paths.",
                           detail=f"unresolved delete: {command[:200]}")
        if targets:
            deletes.append((argv, targets))
    if not deletes:
        return None
    if any(argv and os.path.basename(argv[0]) in _MOVERS for argv in cmds):
        return Verdict("trash", "deny", "Refused: a delete after a `cd` in the same command. Run the delete on its own, "
                                        "from the work folder.", detail=f"delete after cd: {command[:200]}")
    outside = [t for _, targets in deletes for t in targets if not unwrap.inside([t], root or cwd)]
    if outside:
        return Verdict("trash", "deny", f"Refused: it would delete outside the work folder ({', '.join(outside)[:200]}). "
                                        "Deletes here stay under the work folder; anything else is the owner's to do." + way,
                       detail=f"outside {root or cwd}: {', '.join(outside)[:200]}")
    passing = _PASS_INSIDE | ({os.path.basename(s.trash[0])} if s.trash else set())
    if all(os.path.basename(argv[0]) in passing for argv, _ in deletes):
        return None
    plain = len(parts) == 1 and len(cmds) == 1 and is_delete(parts[0][1])
    new = shellguard.delete_rewrite(command.strip(), cwd, list(s.trash)) if plain else None
    if new is None:
        how = " ".join(map(shlex.quote, s.trash)) if s.trash else "the trash command"
        return Verdict("trash", "deny", f"Deletes go to the trash here and this one could not be rewritten safely. "
                                        f"Run the delete on its own as `{how} -- PATH ...` with named paths.",
                       detail=f"not rewritable: {command[:200]}")
    cmd = str(new["command"])
    note = f"The delete was run as `{cmd}` (recoverable from the trash); the tool result is that command's. Do not repeat it."
    dropped = [str(x) for x in new["dropped"]] if isinstance(new["dropped"], list) else []
    if dropped:
        note += f" Left out, not there: {', '.join(dropped)[:200]}."
    return Verdict("trash", "rewrite", note, detail=f"{command[:160]} -> {cmd[:160]}", new_input={"command": cmd})


def restart_hit(tool: str, ti: dict[str, Any], private: Path) -> str | None:
    """Would this call restart yerkd, or write a file it reads only at start? -> what, else None."""
    targets = {str((private / f).resolve()) for f in RESTART_FILES} | {str(private / f) for f in RESTART_FILES}
    if tool in WRITE_TOOLS:
        fp = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if fp and (fp in targets or str(Path(fp).expanduser().resolve()) in targets):
            return f"a write to {Path(fp).name}"
        return None
    if tool != "Bash":
        return None
    command = str(ti.get("command") or "")
    if any(restarts(argv) for argv in unwrap.unwrap(command, whole=True) or []) or any(RESTART_BASH.search(x) for x in runs(command)):
        return "a restart of yerkd"       # what would really run: behind a wrapper, in a nested shell, by its full path
    for seg, argv, norm in shell(command):
        if RESTART_BASH.search(norm):
            return "a restart of yerkd"
        if not shellguard.search_outside_quotes(WRITE_OPS, seg):
            continue
        for tok in re.findall(r"[^\s'\"<>|;&=]+", seg):
            p = Path(os.path.expanduser(tok))
            if p.name in RESTART_FILES and p.is_absolute() and (str(p) in targets or str(p.resolve()) in targets):
                return f"a write to {p.name}"
    return None


def room_named(value: str, rooms: dict[str, dict[str, Any]]) -> str | None:
    """The room a send's `room` names, else None: a room's name, or its channel in rooms.toml (`discord:<id>`), whole or
    the id alone (the day log's room id when rooms.toml sets no `id`). The gate's "other" and the send tool both read it."""
    if value in rooms:
        return value
    for name, conf in rooms.items():
        channel = str(conf.get("channel") or "")
        if value and value in (channel, channel.partition(":")[2]):
            return name
    return None


def machine_room(frm: str, to: str, rooms: dict[str, dict[str, Any]]) -> bool:
    """An owner room's send to the machine room (rooms.toml role = "machine") stays in the in-group: the owner, their
    agents and the daemon's own cards read it, and the `need` tool already reaches it with no word. A family room's
    send there, and any send to any other room, still hold for the word."""
    return (str(rooms.get(to, {}).get("role") or "") == "machine"
            and str(rooms.get(frm, {}).get("privacy") or "owner") == "owner")


def external_hit(tool: str, ti: dict[str, Any], room: str, s: Settings,
                 rooms: dict[str, dict[str, Any]] | None = None) -> tuple[str, str, str] | None:
    """(rule, what in plain words, the call in one line) for an action named external, else None. With `rooms`
    (rooms.toml's) a send names a room by its channel too (room_named); without, by its name only."""
    parts = shell(str(ti.get("command") or "")) if tool == "Bash" else []
    read = [norm for _, _, norm in parts] + (runs(str(ti.get("command") or "")) if tool == "Bash" else [])   # as written, then as it would run
    for name, rule in s.external.items():
        if not re.search(rule.get("tool", r"^$"), tool):
            continue
        to = room_named(str(ti.get("room") or room), rooms or {room: {}}) if rule.get("room") == "other" else None
        if rule.get("room") == "other" and to == room:      # its own name or channel: never another room
            continue
        if rule.get("room") == "other" and to and machine_room(room, to, rooms or {}):   # the owner's own line to the machine room
            continue
        what = rule.get("what", name)
        if rule.get("command"):
            hit = next((norm for norm in read if re.search(rule["command"], norm)), None)
            if hit is None:
                continue
            return name, what, hit[:160]
        if ti.get("room"):
            what = f"{what} ({to or ti['room']})"
        return name, what, f"{tool} {str(ti)[:140]}"
    return None


def call_key(tool: str, ti: dict[str, Any]) -> str:
    """A call as an approval matches it: a shell command as parsed (spacing and quoting normalised, its
    description left out), else the tool and its whole input."""
    if tool == "Bash":
        return " ; ".join(norm or seg for seg, _, norm in shell(str(ti.get("command") or "")))
    return f"{tool} {json.dumps(ti, sort_keys=True, ensure_ascii=False)}"


def bound_to(tool: str, cwd: str) -> str | None:
    """The folder an approval of this call holds in: a shell command's working folder (the same words push another repository
    from another), else None, any: a tool's input is its whole call (a text, an email with its files' whole paths and sha256,
    a delete of whole paths), so a session a /compact put back in its work folder is released (need 78 of 2026-11-09)."""
    return cwd if tool == "Bash" else None


def plain(tool: str, ti: dict[str, Any]) -> str:
    """A held call as a person reads it, for the card in a room with people: a shell command as call_key has it
    (the command is the content), a send's text (the card's first line names its room), any other input one per
    line with paths as their names; never the call's JSON, which stays in the ledger as the receipt."""
    if tool == "Bash":
        return call_key(tool, ti)
    return "\n".join(str(v) if k == "text" else f"{k}: {words.no_paths(str(v))}" for k, v in ti.items()
                     if k != "room" and v not in ("", None))


SHOW = 1500        # the most of a held call's exact text one message shows; longer goes as a file (the daemon)


def held(what: str, call: str, word: str, exact: str = "") -> tuple[str, str]:
    """(for the model, for the room). The room sees the exact text the word would release, the call as
    the approval matches it (call_key), so nobody is asked for a yes on words they have not read: inline
    when it fits one message, else a line saying the file below holds it. A room with people is shown the
    call's content in plain words instead (plain); the exact call stays in the ledger."""
    line = f"{words.WORKING} {words.HELD.format(what=what, word=word)}"
    if exact:
        shown = exact.replace("```", "``​`")            # a fence inside the text cannot close the block
        line += f"\n```\n{shown}\n```" if len(exact) <= SHOW else f"\n{words.HELD_ATTACHED}"
    return (f"Held: {what} (`{call}`) is an external action and runs only after the owner says `{word}` in this "
            "room. The room has been shown its exact text; do not repeat it. End the turn with one short line, and "
            "try the same action again once the word is said.", line)


def refused(what: str, why: str) -> tuple[str, str]:
    return (f"Refused: {what} would restart Yerk AI, and {why}. The room has been told; do not repeat it, and "
            "do not try another way to change or restart it in this turn.",
            f"{words.NOT_DONE} {words.REFUSED.format(what=what, why=why)}")


def is_word(prompt: str, sender: str, s: Settings) -> bool:
    """The owner's word alone or as the first word ("approve", "Approve.", "approve the post, go"), never inside
    a sentence ("I approve", "don't approve"): a reply that leads with the word and carries a remark counts."""
    head = prompt.strip().lower().split(None, 1)
    return sender in s.approvers and bool(head) and head[0].strip(".,!:;") == s.word


def worker_check(payload: dict[str, Any], folder: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """A worker's PreToolUse (workers.py writes the hook): the room's gates on what it would run, with
    deletes held to the worker's own folder. A worker has nobody to say the word, so an external action,
    a write to Yerk AI's config or a restart of it is refused, never held."""
    g = dict(spec.get("gate") or {})
    s = Settings(trash=tuple(g.get("trash") or find_trash()),
                 external={str(k): dict(v) for k, v in dict(g.get("external") or EXTERNAL).items()})
    tool, ti = str(payload.get("tool_name") or ""), dict(payload.get("tool_input") or {})
    v = None
    if tool == "Bash":
        v = check_shell(str(ti.get("command") or ""), str(payload.get("cwd") or folder), s, root=str(folder))
        if v and v.new_input is not None:
            v.new_input = {**ti, **v.new_input}
    if v is None and (what := restart_hit(tool, ti, Path(str(g.get("private") or "/nonexistent")))):
        v = Verdict("restart", "deny", f"Refused: {what} would change or restart Yerk AI, and a worker never does "
                                       "that. Say in your report what should change; the owner does it.")
    if v is None and (hit := external_hit(tool, ti, str(spec.get("room") or ""), s)):
        _, what, call = hit
        v = Verdict("approval", "deny", f"Refused: {what} (`{call}`) is an external action, and a worker never "
                                        "takes one. Put it in your report as a draft; the owner decides.")
    return v.output() if v else {}


def worker_hook(folder: Path) -> int:
    """`python -m yerk_ai.gates worker-hook FOLDER`: the hook's payload on stdin, the answer on stdout.
    A call it cannot read is refused: a gate that fails open is no gate."""
    try:
        out = worker_check(json.loads(sys.stdin.read() or "{}"), folder, json.loads((folder / "worker.json").read_text()))
    except Exception as e:  # noqa: BLE001  fail closed on anything
        out = Verdict("worker", "deny", f"Refused: the worker gate could not read this call ({type(e).__name__}). "
                                        "Try it again in a plainer form.").output()
    if out:
        print(json.dumps(out))
    return 0


def rebuild_running(proc: Path = Path("/proc")) -> bool:
    """A full memory index rebuild (`yerk_ai.memindex --rebuild`, `yerk-index --rebuild`) is running."""
    try:
        for d in proc.iterdir():
            if not d.name.isdigit():
                continue
            try:
                argv = (d / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if b"--rebuild" in argv and any(b"memindex" in a or b"yerk-index" in a for a in argv):
                return True
    except OSError:
        return False
    return False


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "worker-hook":
        sys.exit(worker_hook(Path(sys.argv[2])))
    print("usage: python -m yerk_ai.gates worker-hook FOLDER (a worker's PreToolUse hook)", file=sys.stderr)
    sys.exit(2)
