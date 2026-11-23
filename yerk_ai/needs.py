"""Missing abilities: a room that cannot do what it was asked files what it needed, and one worker builds that ability once
for every room, so every room shares the same one best way of doing each thing.

- The filing: a room that cannot do what it was asked (no tool, a refused tool, a wall) files ONE line with the `need` tool:
  what was asked, what it tried, what it needed; what it did anyway (`shadow`) rides on the line behind a `[shadow]` mark, so
  the builder sees what the room improvised. A room never builds a way of its own (mechanics.md).
- Where it lives: <state>/needs.json is the record; each need is a proposal of kind `need` in <memory>/rules-proposals/<day>.md,
  `## N. need: ABILITY`, numbered past every number on its day's page and every number a need has held, so a new need never
  takes an earlier one's (until 2026-11-11 the count restarted each day; curate.rules gives its own proposals the numbers the
  day's needs leave and keeps their blocks when it rewrites the page), and one line in the machine room.
- Folded: a filing naming the ability of an open need (`same`) is a line under it, its count + 1, never a second builder.
- `yerk needs build DAY N`: the need as a worker brief (`brief`: the asks, the tries, the repo, the shape of done) on [needs]
  account (default: [curate] account), its card in the machine room. A worker never pushes or opens a PR (gates.EXTERNAL), so
  it commits a branch; the push and the PR come from outside it and the owner's cold read merges, so one deploy gives every
  room the same way. Its end sets the state from its report's first line (`ended`).
- `yerk needs list`: each need and its state (`said`); `yerk needs done DAY N PR`: merged by a person.
- The pipeline (pipeline.py, [needs] auto): one need at a time from filed to live, the rest queued; its states are below
  (FLIGHT, CLOSED) and each one's words are `said`'s.
- Plug-ins (need 36 of 2026-11-02): an add-on whose code is a git repository of its own (`plugins`, from the rooms' mcp
  tables) is named in the brief, and a fix living there is built there on a branch of the need's name; the build's end
  records it (`ahead`), the cold read and a fix worker are handed it, the merge fast-forwards the checkout the rooms run (`land`).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import memindex, words
from .config import write_private
from .memory import Settings, daily_lock, one_line

STORE, FOLDER = "needs.json", "rules-proposals"     # FOLDER is curate.PROPOSALS: the one page a day both write
CAP = 60.0                                         # a builder's minutes unless [needs] cap_minutes says otherwise
NEAR = 0.6                                         # the share of content words two abilities hold in common to be one
STOP = {"a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "it", "its", "be", "can", "could", "that", "this", "with",
        "from", "is", "my", "me", "i", "you", "your", "able", "way"}
HEAD = re.compile(r"^## (\d+)\. ", re.M)                       # any proposal's number on the page, curate's or a need's
BLOCK = re.compile(r"\n## \d+\. need: .*?(?=\n## |\Z)", re.S)    # a need's block, to its next heading
PR = re.compile(r"\bPR\s*#?(\d+)\b")
NO_BRANCH = re.compile(r"\W*no branch\b", re.I)                   # the brief's third first line: it built nothing
BRANCH = re.compile(r"\bbranch\s+`?([\w./-]+?)`?(?=[\s,:;]|$)")
FLIGHT = ("building", "built", "pr", "fixing", "reading", "merged", "deploying")   # one need at a time is in one of these
CLOSED = ("merged", "deploying", "live")           # a filing of the same ability after these is a new need
TOKEN = re.compile(r"[^\s'\"`;,()<>]+")
TASKS = ["worktree on its own branch", "the ability, with its tests", "the full suite green", "README, CHANGELOG, abilities",
         "committed, REPORT.md"]


class Unreadable(Exception):
    """<state>/needs.json is there and cannot be read as the store: no need of it is known, so nothing writes it."""


def load(state: Path) -> list[dict[str, Any]]:
    """The needs; [] when there is no store yet. One there that cannot be read is Unreadable, never an empty store."""
    try:
        rows = json.loads((state / STORE).read_text())
        if not isinstance(rows, list):
            raise ValueError("not a list")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        raise Unreadable(words.NEEDS_UNREADABLE.format(path=state / STORE, why=type(e).__name__)) from e
    return [r for r in rows if isinstance(r, dict)]


def refused(r: dict[str, Any] | None) -> bool:
    """`file`'s or `update`'s answer when the store could not be read: nothing was written."""
    return isinstance(r, dict) and r.get("ok") is False


def find(rows: list[dict[str, Any]], day: str, n: int) -> dict[str, Any] | None:
    return next((r for r in rows if r.get("day") == day and int(r.get("n", 0)) == n), None)


def called(state: Path, r: dict[str, Any]) -> str:
    """A need's number as a line says it: N, or "N of DAY" (words.NEED_OF) when a need of another day holds N too, as needs
    filed before 2026-11-11 can, when the count restarted each day."""
    n, day = int(r["n"]), str(r["day"])
    return words.NEED_OF.format(n=n, day=day) if any(int(x.get("n", 0)) == n and x.get("day") != day for x in load(state)) else str(n)


def stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def content(x: str) -> set[str]:
    return {stem(w) for w in memindex.WORD.findall(memindex.norm(x)) if len(w) > 1} - STOP


def same(a: str, b: str) -> bool:
    """One ability said twice: the same content words, one's words all in the other's, or NEAR of them shared."""
    x, y = content(a), content(b)
    if not x or not y:
        return False
    return x == y or min(len(x), len(y)) >= 2 and (x <= y or y <= x) or len(x & y) / len(x | y) >= NEAR


def filed(a: dict[str, Any]) -> str:
    """One filing as its one line: the room, when, what was asked, tried and needed, what it did anyway behind [shadow]."""
    did = f"; did anyway: {a['shadow']} [shadow]" if a.get("shadow") else ""
    return f"- {a['room']}, {a['at']}: asked: {a['asked']}; tried: {a['tried']}; needed: {a['needed']}{did} [m:{a['mid']}]"


def said(r: dict[str, Any]) -> str:
    """Its state in words: filed, queued, building WORKER, built on branch B (no PR seen yet), PR N and its checks, fixing
    PR N (k of the cap), cold read of PR N, held: why, stuck: why, merged, deploying, live."""
    st, last, pr = str(r.get("state", "filed")), f", last build {r['last']}" if r.get("last") else "", r.get("pr")
    ran, plug = [v[0] for v in dict(r.get("checks") or {}).values()], with_plugins(r)
    return {"queued": "queued, it starts when the need in flight is live, held or stuck",
            "building": f"building {r.get('worker')}", "built": f"built on branch {r.get('branch')}{plug}",
            "pr": f"PR {pr}" + (f", checks {ran.count('success')} of {len(ran)} passed" if ran else "") + plug,
            "fixing": f"fixing PR {pr}, fix {r.get('fixes')} of {r.get('fix_cap', 2)}, {r.get('worker')}",
            "reading": f"cold read of PR {pr}{plug}, {r.get('worker')}", "held": f"held: {' '.join(str(r.get('why')).splitlines())}",
            "stuck": f"stuck: {r.get('why')}",
            "merged": f"merged, PR {pr}", "deploying": f"merged, PR {pr}, deploying", "live": f"live, PR {pr}"}.get(st, "filed" + last)


def with_plugins(r: dict[str, Any]) -> str:
    """The plug-in branches a need carries, as a clause: ", and the example plug-in's branch at abc1234"."""
    return "".join(f", and the {k} plug-in's branch at {str(v.get('head'))[:7]}" for k, v in dict(r.get("plugins") or {}).items())


def block(r: dict[str, Any]) -> str:
    k = len(r["asks"])
    return (f"\n## {r['n']}. need: {r['ability']}\n\nSTATE: {said(r)}, asked {k} time{'s' * (k != 1)}\n\n"
            + "".join(filed(a) + "\n" for a in r["asks"]) + f"\nBUILD: `yerk needs build {r['day']} {r['n']}`\n")


def part(state: Path, day: str) -> str:
    """The day's needs as the page shows them, after curate's own proposals (curate.rules)."""
    return "".join(block(r) for r in load(state) if r.get("day") == day)


def numbers(state: Path, day: str) -> set[int]:
    return {int(r["n"]) for r in load(state) if r.get("day") == day}


def around(props: list[dict[str, Any]], taken: set[int]) -> None:
    """curate's proposals numbered in their order past the numbers the day's needs hold, a promotion's `needs` with them: in
    its list and in its WHY's "once N, M are applied", the words curate.propose writes the numbers into."""
    free = (n for n in range(1, len(props) + len(taken) + 2) if n not in taken)
    new = {int(q["n"]): next(free) for q in props}
    for q in props:
        q["n"] = new[int(q["n"])]
        if isinstance(q.get("needs"), list):
            was, q["needs"] = q["needs"], [new.get(int(x), x) for x in q["needs"]]
            q["why"] = str(q.get("why", "")).replace(f" once {', '.join(map(str, was))} are applied",
                                                     f" once {', '.join(map(str, q['needs']))} are applied")


def page(s: Settings, rows: list[dict[str, Any]], day: str) -> Path:
    """The day's page with its needs' blocks as they stand now, curate's part kept (the caller holds daily_lock)."""
    p = s.root / FOLDER / f"{day}.md"
    text = p.read_text() if p.is_file() else (f"---\nclass: private\n---\n# Rule proposals, {day}\n\nNeeds the rooms filed: "
                                              "`yerk needs build DAY N` starts its builder, `yerk needs list` says each one's state.\n")
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return write_private(p, BLOCK.sub("", text).rstrip("\n") + "\n" + "".join(block(r) for r in rows if r.get("day") == day))


def file(s: Settings, state: Path, room: str, mid: str, asked: str, tried: str, needed: str, shadow: str = "",
         now: float | None = None) -> dict[str, Any]:
    """One filing: folded into the open need of the same ability (its count + 1), else a new need numbered past every
    proposal of the day and every need of any day; the page's block written. A retried call files once."""
    a = {k: one_line(v).strip() for k, v in (("asked", asked), ("tried", tried), ("needed", needed), ("shadow", shadow))}
    if not (a["asked"] and a["tried"] and a["needed"]):
        return {"ok": False, "error": "a need is three things, each as text: what was asked, what you tried, what you needed"}
    at = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).astimezone(s.tz)
    a |= {"room": room, "at": f"{at:%Y-%m-%d %H:%M}", "mid": mid}
    with daily_lock(s.daily):                          # the memory writers' lock: curate's rewrite of the page waits on it too
        try:
            rows = load(state)
        except Unreadable as e:
            return {"ok": False, "error": str(e)}
        r = next((x for x in rows if x.get("state") not in CLOSED and same(str(x["ability"]), a["needed"])), None)
        again = r is not None and any((x["room"], x["mid"], x["needed"]) == (room, mid, a["needed"]) for x in r["asks"])
        if r is None:
            day, p = at.date().isoformat(), s.root / FOLDER / f"{at.date().isoformat()}.md"
            used = {int(x) for x in HEAD.findall(p.read_text())} if p.is_file() else set()
            r = {"day": day, "n": max(used | {int(x["n"]) for x in rows}, default=0) + 1,
                 "ability": a["needed"], "state": "filed", "asks": []}
            rows.append(r)
        if not again:
            r["asks"].append(a)
            write_private(state / STORE, json.dumps(rows, indent=1, ensure_ascii=False))
            page(s, rows, str(r["day"]))
    return {"ok": True, "day": r["day"], "n": r["n"], "ability": r["ability"], "count": len(r["asks"]),
            "folded": len(r["asks"]) > 1, "again": again, "state": said(r)}


def update(s: Settings, state: Path, day: str, n: int, changes: dict[str, Any],
           when: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any] | None:
    """The need's record changed and its block written again, under the lock, so a check (`when`) and its change are one
    step; None when there is no such need or `when` does not hold; {"ok": False, "error"} when the store cannot be read."""
    with daily_lock(s.daily):
        try:
            rows = load(state)
        except Unreadable as e:
            return {"ok": False, "error": str(e)}
        if (r := find(rows, day, n)) is None or when is not None and not when(r):
            return None
        r.update(changes)
        write_private(state / STORE, json.dumps(rows, indent=1, ensure_ascii=False))
        page(s, rows, day)
    return r


def ended(s: Settings, state: Path, day: str, n: int, wid: str, how: str, first: str, clean: bool,
          plugs: dict[str, dict[str, str]] | None = None) -> dict[str, Any] | None:
    """A builder's end: PR N when its report's first line names one, else the branch it names, else filed again with how it
    ended, so it can be built again; a need marked merged meanwhile, or built by another worker, stays as it is. A line that
    starts "no branch" names neither, whatever PR or branch its why mentions. `plugs` are the plug-in branches it left
    (`ahead`), kept with a PR or a branch."""
    pr, branch = (None, None) if NO_BRANCH.match(first) else (PR.search(first), BRANCH.search(first))
    on = {"branch": branch[1] if branch else f"need-{day}-{n}", "since": round(time.time(), 3), "plugins": dict(plugs or {})}   # since: the watcher's clock
    changes: dict[str, Any] = ({"state": "pr", "pr": int(pr[1]), **on} if pr else {"state": "built", **on} if branch and clean
                               else {"state": "filed", "last": f"{wid}: {how}"})
    return update(s, state, day, n, changes, lambda r: r.get("state") == "building" and r.get("worker") == wid)


def listing(state: Path) -> list[str]:
    """`yerk needs list`: a line a need, the open ones first and the oldest first: its day and number, state, count, ability."""
    rows = sorted(load(state), key=lambda r: (r.get("state") in ("merged", "live"), str(r.get("day")), int(r.get("n", 0))))
    return [f"{r['day']} {r['n']} {said(r)}: {r['ability']} (asked {len(r['asks'])} time{'s' * (len(r['asks']) != 1)})"
            for r in rows]


def foreign(texts: list[str], own: Path, others: list[str]) -> str:
    """The first path in `texts` a person's room may not name: outside its own folder `own`, or naming another room's folder."""
    for tok in (t.rstrip(":.!?") for x in texts for t in TOKEN.findall(x)):
        if any(f"rooms/{o}" in tok for o in others):
            return tok
        if tok.startswith(("/", "~", "./", "../")) and re.search(r"\w", tok):
            p = Path(tok).expanduser()
            if not (p if p.is_absolute() else own / "work" / p).resolve().is_relative_to(own.resolve()):
                return tok
    return ""


def git(repo: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    """git in `repo`; one that could not run (no git, its 30 s gone) answers 127 with why."""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess([], 127, "", str(e))


def tidy(folder: Path) -> list[str]:
    """A need's worker ended: each git worktree it left under its folder (a linked one's .git is a file) removed when clean,
    by `git worktree remove`, never forced, its branch kept (2026-11-10: 207 left, none ever removed); one line for each
    that stays, for the worker's note."""
    kept = []
    for wt in sorted(p.parent for p in folder.glob("*/.git") if p.is_file()):
        st = git(wt, "status", "--porcelain")
        if st.returncode == 0 and not st.stdout.strip() and git(wt, "worktree", "remove", str(wt)).returncode == 0:
            continue
        kept.append(words.WORKTREE_KEPT.format(name=wt.name, why=words.WORKTREE_CHANGES if st.stdout.strip() else words.WORKTREE_REFUSED))
    return kept


def plugins(confs: dict[str, dict[str, Any]], own: Path) -> dict[str, dict[str, str]]:
    """The add-ons whose code is a local git repository of their own, where a need's fix may live: each room's mcp server
    whose command or an argument is a file in a checkout that is not Yerk's own, the home folder or the root, as {server:
    {"repo": its top, "base": the branch that checkout is on, the code the rooms run}}; a repository once, under the first
    server naming it, and a checkout on no branch never, since nothing could land there."""
    out: dict[str, dict[str, str]] = {}
    for name, srv in ((k, v) for c in confs.values() for k, v in dict(c.get("mcp") or {}).items() if isinstance(v, dict) and k not in out):
        for f in (Path(x).expanduser() for x in [srv.get("command"), *list(srv.get("args") or [])] if isinstance(x, str) and x.startswith(("/", "~"))):
            top = git(f.parent, "rev-parse", "--show-toplevel").stdout.strip() if f.is_file() else ""
            base = git(f.parent, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() if top else ""
            if base and (t := str(Path(top).resolve())) not in {str(own.resolve()), str(Path.home().resolve()), "/", *(v["repo"] for v in out.values())}:
                out[str(name)] = {"repo": t, "base": base}
                break
    return out


def ahead(found: dict[str, dict[str, str]], branch: str) -> dict[str, dict[str, str]]:
    """The plug-ins in `found` whose repository has `branch` with commits its base lacks: each with the branch and its head."""
    heads = {k: git(p["repo"], "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}").stdout.strip() for k, p in found.items()}
    return {k: {"repo": p["repo"], "base": p["base"], "branch": branch, "head": heads[k]} for k, p in found.items()
            if heads[k] and git(p["repo"], "rev-list", "--count", f"{p['base']}..{heads[k]}").stdout.strip() not in ("", "0")}


def land(name: str, p: dict[str, str], dry: bool = False) -> str:
    """A plug-in's branch onto its base in the checkout the rooms run, a fast-forward to the head the cold read saw (the sha
    guard, as on GitHub): "" when it landed or its base holds it already, else why, in words; `dry` only checks."""
    repo, base, sha, b = p["repo"], p["base"], p["head"], p["branch"]
    if (a := git(repo, "merge-base", "--is-ancestor", sha, base)).returncode in (0, 127):
        return "" if a.returncode == 0 else f"the {name} plug-in did not land: git did not run: {a.stderr}"
    why = (f"its branch {b} moved off {sha[:7]}" if git(repo, "rev-parse", "-q", "--verify", f"refs/heads/{b}").stdout.strip() != sha
           else f"its {base} moved since {b} was made, so it is no fast-forward" if git(repo, "merge-base", "--is-ancestor", base, sha).returncode
           else f"its checkout is not on {base}" if git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() != base
           else "its checkout has changes not committed" if git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
           else "" if dry else one_line((m := git(repo, "merge", "--ff-only", "--quiet", sha)).stderr or "git refused")[:160] * bool(m.returncode))
    return why and f"the {name} plug-in did not land: {why}"


def plugged(offered: dict[str, dict[str, str]], there: dict[str, dict[str, str]], branch: str) -> str:
    """The brief's plug-ins part for a builder or a fix worker: each repository a fix may live in, its branch when one is there."""
    if not (offered := {**offered, **there}):
        return ""
    each = "; ".join(f"`{k}` at {v['repo']} (its rooms run its {v['base']}" + (f"; its branch `{branch}` is at {there[k]['head'][:7]}, so "
                     "build on it" if k in there else "") + ")" for k, v in offered.items())
    return ("## Plug-ins\nAn add-on whose code is a local git repository of its own is Yerk's too: a fix that lives in its code is built "
            f"there, never described in abilities.toml as out of reach. Here: {each}. Make its worktree beside yours, `git -C <repo> "
            f"worktree add <your folder>/<plug-in>-wt -b {branch} <its base>` (no `-b` when the branch is there), run its tests as "
            f"its README says, and commit on `{branch}`, never on its base: the pipeline finds the branch when you end, the cold read "
            "reads it and the merge fast-forwards the plug-in's base, the checkout its rooms run. Yerk's branch still carries the "
            "abilities entry, the CHANGELOG and a budgets.toml line when the plug-in's tool entries grow; name the plug-in's branch "
            "and its short sha in the report.\n\n")


def carried(r: dict[str, Any]) -> str:
    """The cold read's part: each plug-in branch the need carries, read with the PR and landed by its merge."""
    return "".join(f"The fix lives in the {k} plug-in's own repository too: its branch `{v['branch']}` at {v['head'][:7]} in {v['repo']}, "
                   f"its rooms on its {v['base']}. Read `git -C {v['repo']} diff {v['base']}...{v['head']}` and run its tests in a "
                   f"worktree of that sha under your folder (`git -C {v['repo']} worktree add --detach <your folder>/{k}-wt "
                   f"{v['head']}`); a `merge` merges the PR and fast-forwards its {v['base']} to that sha.\n\n"
                   for k, v in dict(r.get("plugins") or {}).items())


def brief(r: dict[str, Any], repo: Path, check: str = "", offered: dict[str, dict[str, str]] | None = None,
          there: dict[str, dict[str, str]] | None = None) -> str:
    """The builder's brief from a need: every ask as filed, the repo and where to read, the plug-in repositories a fix may
    live in (`offered`, `plugins`; `there`, a branch an earlier build left in one), and the shape of done (a branch with
    tests, never a merge, a push or a deploy; a report; the last message the deliverable)."""
    branch, k, check = f"need-{r['day']}-{r['n']}", len(r["asks"]), checked(check)
    return (
        f"# Yerk: build \"{r['ability']}\" for every room (need {r['n']} of {r['day']})\n\n"
        f"A room could not do what it was asked and filed what it needed instead of improvising a way of its own, {k} "
        f"time{'s' * (k != 1)}:\n\n" + "".join(filed(a) + "\n" for a in r["asks"]) +
        "\nA line marked [shadow] is what a room did anyway: what it improvised, never the way to build.\n\n"
        "## Where\n"
        f"Repo {repo} (package yerk_ai, tests in tests/, `uv run pytest -q`). Work in a git worktree on a new branch `{branch}` "
        f"from main, made under your own folder: `git -C {repo} worktree add <your folder>/wt -b {branch} main`. Read first: "
        "README.md, STYLE.md and yerk_ai/words.py (every line a room or a card prints), abilities.toml (the new ability gets its "
        "entry), yerk_ai/mechanics.md (one line, only when every room must know the new way), yerk_ai/mcp_server.py and "
        "yerk_ai/daemon.py (the tools a room calls), budgets.toml (every size line, each raise with its dated reason).\n\n"
        + plugged(offered or {}, there or {}, branch) +
        "## What done looks like\n"
        "1. The ability built once, the one best way, in Yerk itself, so one deploy gives every room the same way: never a "
        "workaround for one room, a script in a room's folder, or a rule telling rooms to improvise.\n"
        "2. Tests in the existing style for what it does and for the wall the asks above hit; the full suite green (`uv run "
        "pytest -q`, its exit status read, never through `| tail`); ruff and mypy clean. Every person, place, handle and id in "
        "a test, a fixture, a doc or the CHANGELOG is a placeholder, never one from the asks above.\n"
        "3. README.md, CHANGELOG.md and the abilities.toml entry.\n"
        f"4. Committed on `{branch}`. Never merge, push, open a PR or deploy: a worker's gate refuses a push and a PR, the "
        "branch reaches its pull request from outside this run, and the owner's cold read decides the merge.\n"
        f"5. REPORT.md's first line: `PR <number>: <what it adds>` when `gh pr list --head {branch}` shows one, else `branch "
        f"{branch} at <short sha>: <what it adds>`, or `no branch: <why>`; then the files changed, the test counts and every "
        "decision you made.\n\n"
        "## Leave alone\n"
        "The owner's private folder and Yerk's state folder, every room's files, other branches, the running daemon. Surgical "
        "edits, never a whole file rewritten. Your last message is the deliverable: REPORT.md's first line and the test "
        "counts, with no tool call after it.\n" + RULES.format(check=check))


RULES = ("\n## Every commit\n"
         "- {check}\n- Never amend, squash or rebase a commit once it is made: every commit is pushed within seconds.\n"
         "- No Agent tool and no subagents: read with grep and sed yourself.\n")


def checked(tool: str) -> str:
    """The pr-check line every brief carries: [needs] pr_check names the install's checker, else the checks by hand."""
    return (f"Before your last commit run `{tool} <your worktree>` and fix until it prints ALL PASS." if tool else
            "Before your last commit run the full suite, ruff and mypy in your worktree and fix until all three are clean.")


def where(r: dict[str, Any], repo: Path) -> str:
    return (f"Repo {repo}, pull request {r.get('pr')} from branch `{r.get('branch') or ''}` at {str(r.get('head') or '')[:7]}. "
            f"Make your own worktree at that head under your folder: `git -C {repo} fetch origin` then `git -C {repo} worktree "
            f"add <your folder>/wt {r.get('branch') or r.get('head')}`.\n\n")


def read_brief(r: dict[str, Any], repo: Path, check: str) -> str:
    """The cold read: a fresh worker, never the builder, reads the pull request against main and says merge, fix first or
    unsure in its report's first line (pipeline.py acts on it)."""
    return (f"# Yerk: the cold read of PR {r.get('pr')}, need {r['n']} of {r['day']}: \"{r['ability']}\"\n\n"
            "The rooms filed it so:\n\n" + "".join(filed(a) + "\n" for a in r["asks"]) + "\n" + where(r, repo) + carried(r) +
            "Read the diff against main (`git diff origin/main...HEAD` in the worktree) as a reviewer who did not write it: does "
            "it build the ability once for every room, with tests, in the repo's style (README.md, STYLE.md), and break nothing? "
            f"{checked(check)}\n\nYour REPORT.md's first line is exactly one of `merge`, `fix first: <why>`, `unsure: <why>`, "
            "and the rest is what you read. Never merge, push, open a PR or deploy, and change no file in the repo.\n")


def fix_brief(r: dict[str, Any], repo: Path, check: str, what: str, offered: dict[str, dict[str, str]] | None = None) -> str:
    """A fix worker: the failing checks as GitHub gives them, or the cold read's why, fixed with a new commit on the branch,
    or on the plug-in's branch when the fix lives in a plug-in (`plugged`)."""
    return (f"# Yerk: fix PR {r.get('pr')}, need {r['n']} of {r['day']}: \"{r['ability']}\"\n\n" + where(r, repo) +
            plugged(offered or {}, dict(r.get("plugins") or {}), f"need-{r['day']}-{r['n']}") +
            f"What to fix:\n{what}\n\nReproduce it first (the suite, on that Python through `uv run --python X` when uv has "
            "it). Fold origin/main in first when the branch is behind it. Fix it with a NEW commit on the branch, never an amend, "
            "a squash or a rebase: every commit on the branch is pushed within seconds. Never push, open a PR, merge or deploy.\n"
            + RULES.format(check=checked(check)) + "\nYour REPORT.md's first line is `fixed: <short sha>` or `cannot: <why>`.\n")
