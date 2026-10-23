"""Missing abilities: a room that cannot do what it was asked files what it needed, and one worker builds that ability once
for every room, so every room shares the same one best way of doing each thing.

- The filing: a room that cannot do what it was asked (no tool, a refused tool, a wall) files ONE line with the `need` tool:
  what was asked, what it tried, what it needed; what it did anyway (`shadow`) rides on the line behind a `[shadow]` mark, so
  the builder sees what the room improvised. A room never builds a way of its own (mechanics.md).
- Where it lives: <state>/needs.json is the record; each need is a proposal of kind `need` in <memory>/rules-proposals/<day>.md,
  `## N. need: ABILITY`, numbered with curate's in one count a day (curate.rules gives its own proposals the numbers the day's
  needs leave and keeps their blocks when it rewrites the page), and one line in the machine room.
- Folded: a filing naming the ability of an open need (`same`) is a line under it, its count + 1, never a second builder.
- `yerk needs build DAY N`: the need as a worker brief (`brief`: the asks, the tries, the repo, the shape of done) on [needs]
  account (default: [curate] account), its card in the machine room. A worker never pushes or opens a PR (gates.EXTERNAL), so
  it commits a branch; the push and the PR come from outside it and the owner's cold read merges, so one deploy gives every
  room the same way. Its end sets the state from its report's first line (`ended`).
- `yerk needs list`: each need and its state (filed, building WORKER, PR N, merged); `yerk needs done DAY N PR`: merged.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import memindex
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
TOKEN = re.compile(r"[^\s'\"`;,()<>]+")
TASKS = ["worktree on its own branch", "the ability, with its tests", "the full suite green", "README, CHANGELOG, abilities",
         "committed, REPORT.md"]


def load(state: Path) -> list[dict[str, Any]]:
    try:
        rows = json.loads((state / STORE).read_text())
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def find(rows: list[dict[str, Any]], day: str, n: int) -> dict[str, Any] | None:
    return next((r for r in rows if r.get("day") == day and int(r.get("n", 0)) == n), None)


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
    """Its state in words: filed, building WORKER, PR N, built on branch B (no PR seen yet), merged."""
    st, last = str(r.get("state", "filed")), f", last build {r['last']}" if r.get("last") else ""
    return {"building": f"building {r.get('worker')}", "pr": f"PR {r.get('pr')}", "built": f"built on branch {r.get('branch')}",
            "merged": f"merged, PR {r.get('pr')}"}.get(st, "filed" + last)


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
    proposal and need of the day; the page's block written. A retried call files once."""
    a = {k: one_line(v).strip() for k, v in (("asked", asked), ("tried", tried), ("needed", needed), ("shadow", shadow))}
    if not (a["asked"] and a["tried"] and a["needed"]):
        return {"ok": False, "error": "a need is three things, each as text: what was asked, what you tried, what you needed"}
    at = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).astimezone(s.tz)
    a |= {"room": room, "at": f"{at:%Y-%m-%d %H:%M}", "mid": mid}
    with daily_lock(s.daily):                          # the memory writers' lock: curate's rewrite of the page waits on it too
        rows = load(state)
        r = next((x for x in rows if x.get("state") != "merged" and same(str(x["ability"]), a["needed"])), None)
        again = r is not None and any((x["room"], x["mid"], x["needed"]) == (room, mid, a["needed"]) for x in r["asks"])
        if r is None:
            day, p = at.date().isoformat(), s.root / FOLDER / f"{at.date().isoformat()}.md"
            used = {int(x) for x in HEAD.findall(p.read_text())} if p.is_file() else set()
            r = {"day": day, "n": max(used | {int(x["n"]) for x in rows if x.get("day") == day}, default=0) + 1,
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
    step; None when there is no such need or `when` does not hold."""
    with daily_lock(s.daily):
        rows = load(state)
        if (r := find(rows, day, n)) is None or when is not None and not when(r):
            return None
        r.update(changes)
        write_private(state / STORE, json.dumps(rows, indent=1, ensure_ascii=False))
        page(s, rows, day)
    return r


def ended(s: Settings, state: Path, day: str, n: int, wid: str, how: str, first: str, clean: bool) -> dict[str, Any] | None:
    """A builder's end: PR N when its report's first line names one, else the branch it names, else filed again with how it
    ended, so it can be built again; a need marked merged meanwhile, or built by another worker, stays as it is. A line that
    starts "no branch" names neither, whatever PR or branch its why mentions."""
    pr, branch = (None, None) if NO_BRANCH.match(first) else (PR.search(first), BRANCH.search(first))
    changes: dict[str, Any] = ({"state": "pr", "pr": int(pr[1])} if pr else {"state": "built", "branch": branch[1]} if branch and clean
                               else {"state": "filed", "last": f"{wid}: {how}"})
    return update(s, state, day, n, changes, lambda r: r.get("state") == "building" and r.get("worker") == wid)


def listing(state: Path) -> list[str]:
    """`yerk needs list`: a line a need, the open ones first and the oldest first: its day and number, state, count, ability."""
    rows = sorted(load(state), key=lambda r: (r.get("state") == "merged", str(r.get("day")), int(r.get("n", 0))))
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


def brief(r: dict[str, Any], repo: Path) -> str:
    """The builder's brief from a need: every ask as filed, the repo and where to read, and the shape of done (a branch with
    tests, never a merge, a push or a deploy; a report; the last message the deliverable)."""
    branch, k = f"need-{r['day']}-{r['n']}", len(r["asks"])
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
        "counts, with no tool call after it.\n")
