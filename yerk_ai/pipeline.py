"""The needs pipeline: a missing ability a room filed (a need, see needs.py) goes built, checked, read cold by a fresh worker,
merged, deployed and live with no person in the loop, except a change to a sensitive part, which waits for one. Yerk maintains
itself this way: one need at a time (two open PRs that both move the code ceiling or the CHANGELOG conflict), the rest queued.

- Start ([needs] auto, default true; false is the hand road of needs.py exactly): a new need starts its builder when no need
  is in flight (needs.FLIGHT), else it is queued; a need that reaches live, held or stuck starts the oldest queued one. A
  builder that fails starts once more; failed twice, the need is stuck; one cut off by a stop nobody asked for (a box
  shutdown) is queued, uncounted, and the next tick starts it; one that died for memory is held with that, never started
  again by itself (workers.for_memory: it would only run out again). A need still filed that no build ever claimed (filed
  before the pipeline, or with auto off) is queued on a tick (`pickup`); one a build came back from stays, a person's call.
  At the daemon's start a need left building with no worker (the daemon died between the claim and its start) is queued (`begin`).
- The watch (`tick`: its own task every EVERY seconds, and once each time the channel connects): built, then its PR
  (github.pr_for), then PR; a PR's head's checks all in ([needs] contexts) and green: the cold read; one red: a fix worker
  while fixes < [needs] fix_cap, else held; not all in within [needs] checks_minutes of the head: stuck, and PR again once
  they are all in and final and no other need is in flight (`late`); no PR NO_PR after the build: stuck (the box's sync is down).
- The cold read, a fresh worker and never the builder: `merge` merges with the head's sha (github.merge, the sha guard);
  `fix first: why` is a fix worker (it counts toward the cap) and the checks again; `unsure: why` is held. A fix worker:
  `fixed: sha` is PR again with its new head; `cannot: why` is held. A read or a fix cut off by a stop nobody asked for
  (workers.cut_off, a box shutdown) is no verdict: PR again, a fix's count as it was; one that died for memory is not, so with no verdict it is held with that.
- The deeper check (2026-11-17: review matched to risk, the tier set by the change and never by who built it): before a
  `merge` merges or lands anything, a change on the [needs] sensitive list (SENSITIVE unless it says otherwise; [] is the road
  above exactly) is held, its why NEED_DEEPER naming what it touches and the row's `deeper` its head, paths and plug-ins, so a
  later step finds the needs that wait (`waits`: the paths its head changes against its merge base with origin/main, read by
  git in the daemon's own tree; a change git cannot read waits too). The list is the running daemon's, never the PR's, and
  this file is on it. A person releases one as any held need: merges it and runs `yerk needs done`, and the deploy follows.
- A plug-in branch (need 36 of 2026-11-02, needs.plugins): the cold read and a fix worker are handed the need's branch in
  a plug-in's own repository at its head then (`heads`); the merge checks each lands as a fast-forward before GitHub's and
  lands it after (needs.land), in the checkout the rooms run; one that cannot land holds the need, said with why.
- The deploy: merged, with no deploy running, `yerk deploy origin/main` in a transient unit yerk-deploy-<id> (the restart
  cannot run inside the daemon), after a `git fetch origin` that authenticates through git_askpass; deploying.
- Live: a proven deploys row after the merge whose commit holds it: live, its branches dropped (`drop`), one line in the
  machine room and one in each room that filed it, each once; a deploy row that failed: held.
Every change is one line in the machine room, never a relay turn. With no GitHub token every GitHub step is skipped and one
line says so; a person merges, runs `yerk needs done` and deploys, and live still follows the proven deploy."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

from . import github, needs, words, workers
from .config import REPO, flag, write_private

log = logging.getLogger("yerk.needs")
EVERY, NO_PR, READ_CAP = 120.0, 20 * 60.0, 30.0       # the watch's tick; no PR this long after the build; a reader's and a fixer's minutes
LIVE_POLL, LIVE_CAP = 5.0, 20 * 60.0                  # a need's line to a room in a turn: looked at every 5 s, posted at 20 min at the latest
UNIT = "yerk-deploy-"
RED = ("failure", "error")
# [needs] sensitive's default, each beside what going wrong there means: a path, a folder/ (every file below it) or
# plugin:NAME (a branch in that plug-in); this file holds it, so it is on it. A change touching one waits for a deeper check.
SENSITIVE = (
    "yerk_ai/texting/",                                               # a text goes out in the owner's name, never approved
    "yerk_ai/emails.py",                                              # the same for an email
    "yerk_ai/gates.py", "yerk_ai/hooks.py", "yerk_ai/deletes.py",     # an action outside the box (money, a message, a post, a delete for good) runs with no hold
    "yerk_ai/accounts.py", "yerk_ai/git_askpass.py", "yerk_ai/github.py",   # a login or a token used where it should not be, or every room on a dead account
    "yerk_ai/deploy.py", "yerk_ai/pipeline.py", "yerk_ai/needs.py", "yerk_ai/workers.py",   # Yerk changes or ships itself wrongly, or a worker leaves its sandbox
    ".github/", "pyproject.toml", "uv.lock", "units/",                # what checks a change, what code is pulled in, what runs on the box
    "plugin:hands")                                                   # it drives the owner's own browser
BASE, SHA = "origin/main", re.compile(r"[0-9a-f]{7,64}")   # what a change is read against; a head git may be handed


class Units:
    """systemd-run --user for the deploy unit, and the yerk-deploy-* units running (unreadable is None: busy)."""

    def start(self, argv: list[str]) -> str:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        return f"systemd-run exited {p.returncode}: {p.stderr.strip()[:200]}" if p.returncode else ""

    def running(self) -> set[str] | None:
        try:
            p = workers.systemctl("list-units", f"{UNIT}*", "--all", "--plain", "--no-legend", "--no-pager", check="list-units")
        except (OSError, subprocess.SubprocessError):
            return None
        rows = [x.split() for x in p.stdout.splitlines() if x.strip()]
        return {r[0].removesuffix(".service") for r in rows if len(r) > 2 and r[2] not in ("inactive", "failed")}


def drop_local(repo: str | Path, branch: str) -> str:
    """`branch` deleted in `repo`: "" when it is gone or never was, else why it stays. Only once HEAD holds it (-d alone would
    go by an upstream the branch has), and git's own -d does it. -d also refuses a branch a worktree is on, and a worker's
    worktree stays on its branch when the worker ends, so each linked one on it is detached first, its files untouched;
    the main checkout, first in git's list, never."""
    ref = f"refs/heads/{branch}"
    if needs.git(repo, "show-ref", "--verify", "--quiet", ref).returncode == 1:
        return ""
    if (held := needs.git(repo, "merge-base", "--is-ancestor", ref, "HEAD")).returncode:
        return words.line(held.stderr or "its HEAD does not hold it", 200)
    for block in needs.git(repo, "worktree", "list", "--porcelain").stdout.split("\n\n")[1:]:
        if f"branch {ref}" in (lines := block.splitlines()):
            needs.git(lines[0].removeprefix("worktree "), "checkout", "--quiet", "--detach")
    p = needs.git(repo, "branch", "-d", branch)
    return words.line(p.stderr or "git refused", 200) if p.returncode else ""


def deeper_why(w: dict[str, Any]) -> str:
    """The why of a need that waits for the deeper check (Pipeline.waits) in lines of phone width (STYLE.md): the fixed words,
    then the first three things it touches and how many more, or what git said when it could not read the change."""
    text = words.NEED_DEEPER_UNREAD.format(why=w["unread"]) if "unread" in w else words.NEED_DEEPER.format(what=words.few(
        [words.line(p) for p in w["paths"]] + [words.NEED_DEEPER_PLUGIN.format(name=k) for k in w["plugins"]]))
    return "\n".join(textwrap.wrap(text, words.PHONE, break_long_words=False, break_on_hyphens=False))


class Pipeline:
    def __init__(self, y: Any) -> None:
        self.y, self.units, self.now, self.tree = y, Units(), time.time, REPO
        self.lock = asyncio.Lock()

    def conf(self) -> dict[str, Any]:
        return dict(self.y.conf.get("needs", {}))

    def auto(self) -> bool:
        return flag(self.conf().get("auto"), True)

    def flight(self) -> list[dict[str, Any]]:
        """The needs on the road; a merged one only when it has a merge time (one merged before the pipeline is done)."""
        return [r for r in needs.load(self.y.state) if r.get("state") in needs.FLIGHT
                and (r.get("state") != "merged" or r.get("merged_at"))]

    def room(self) -> str:
        """Where a need's worker cards and lines go: the machine room, else the owner's first room."""
        return next((r for r, c in self.y.room_confs.items() if c.get("role") == "machine" and r in self.y.channels),
                    next((r for r in self.y.rooms if self.y.owner(r)), ""))

    def account(self) -> str:
        return str(self.conf().get("account") or dict(self.y.conf.get("curate", {})).get("account") or "")

    async def set(self, r: dict[str, Any], changes: dict[str, Any], *was: str) -> dict[str, Any] | None:
        """The need changed, only while it is still in one of the states `was` (when given)."""
        return await asyncio.to_thread(needs.update, self.y.memory, self.y.state, str(r["day"]), int(r["n"]), changes,
                                       (lambda x: x.get("state") in was) if was else None)

    async def say(self, line: str) -> None:
        await self.y.jobs.alert(None, line, 0)
        log.info("%s", line)

    # the start, and the queue
    async def filed(self, r: dict[str, Any], room: str) -> None:
        """A new need (not folded, not again): its builder starts when none is in flight, else it is queued."""
        ab = words.line(str(r["ability"]), 80)
        if (busy := [x for x in self.flight() if (x["day"], x["n"]) != (r["day"], r["n"])]):
            await self.set(r, {"state": "queued"}, "filed")
            return await self.say(words.NEED_QUEUED.format(n=needs.called(self.y.state, r), room=room, ability=ab,
                                                          behind=needs.called(self.y.state, busy[0])))
        await self.say(words.NEED_STARTS.format(n=needs.called(self.y.state, r), room=room, ability=ab))
        await self.y.need_build({"day": r["day"], "n": r["n"]})

    async def next(self) -> None:
        """The oldest queued need starts when none is in flight. The pick has no await in it, so two at once pick the same
        need and need_build's claim lets one start it; never under the tick's lock, since a worker that fails to launch runs
        the hooks (and so this) inside its start."""
        if not self.auto() or self.flight():
            return
        q = sorted((r for r in needs.load(self.y.state) if r.get("state") == "queued"), key=lambda r: (str(r["day"]), int(r["n"])))
        if q and (await self.y.need_build({"day": q[0]["day"], "n": q[0]["n"]})).get("ok"):
            await self.say(words.NEED_STARTS.format(n=needs.called(self.y.state, q[0]), room=q[0]["asks"][0]["room"],
                                                    ability=words.line(str(q[0]["ability"]), 80)))

    async def pickup(self) -> None:
        """A need still filed with no `last` (no build came back) and no `worker` (need_build writes one on its claim, so no
        build ever started) is queued, oldest first, each said once; a filed one with `last` stays, a person's call, as held."""
        if not self.auto():
            return
        for r in sorted(needs.load(self.y.state), key=lambda r: (str(r["day"]), int(r["n"]))):
            if r.get("state") == "filed" and not r.get("last") and "worker" not in r and await self.set(r, {"state": "queued"}, "filed"):
                await self.say(words.NEED_PICKED_UP.format(n=r["n"], day=r["day"], room=r["asks"][0]["room"], ability=words.line(str(r["ability"]), 80)))

    def begin(self) -> None:
        """The daemon's start, before any of its tasks: a need left building with no worker (need_build's claim, the daemon
        gone before the start answered) is queued again, so the queue starts it; building is in flight, so it held the queue
        for good. One building with a worker is left alone: its end comes through workers.reattach."""
        for r in needs.load(self.y.state):
            if r.get("state") == "building" and not r.get("worker") and needs.update(
                    self.y.memory, self.y.state, str(r["day"]), int(r["n"]), {"state": "queued"},
                    lambda x: x.get("state") == "building" and not x.get("worker")):
                log.info("needs: need %s of %s was claimed and its builder never started, so it is queued again", r["n"], r["day"])

    async def stop(self, r: dict[str, Any], state: str, why: str, deeper: dict[str, Any] | None = None) -> None:
        """Held (a person decides) or stuck, with why; the next queued need starts. `deeper` is the row's own field of a need
        that waits for the deeper check (`waits`), so a program finds those; any other stop clears one an earlier hold left."""
        await self.set(r, {"state": state, "why": why, "worker": "", **({"deeper": deeper or {}} if deeper or r.get("deeper") else {})})
        await self.say((words.NEED_HELD if state == "held" else words.NEED_STUCK).format(
            n=needs.called(self.y.state, r), ability=words.line(str(r["ability"]), 80), why=why))
        await self.next()

    # the watch
    async def watch(self, every: float = EVERY) -> None:
        while True:
            await asyncio.sleep(every)
            await self.tick()

    async def tick(self) -> None:
        async with self.lock:
            try:
                await self._tick()
            except Exception:
                log.exception("needs: the pipeline's tick failed")

    async def _tick(self) -> None:
        tok = await asyncio.to_thread(github.token, self.y.private)
        repo = await asyncio.to_thread(github.slug, REPO, str(self.conf().get("repo") or "")) if tok else ""
        for r in needs.load(self.y.state):
            if r.get("state") in ("built", "pr"):
                await self.checked(r, tok, repo)
            elif r.get("state") == "stuck" and r.get("pr") and r.get("head") and tok and repo:
                await self.late(r, tok, repo)
            elif r.get("state") in ("merged", "deploying") and r.get("merged_at"):
                await self.ship(r, tok)
        await self.pickup()
        await self.next()

    async def checked(self, r: dict[str, Any], tok: str, repo: str) -> None:
        if not (tok and repo):
            if not r.get("told"):                        # said once a need: the merge is a person's, as before
                await self.set(r, {"told": 1})
                await self.say(words.NEED_NO_TOKEN.format(n=needs.called(self.y.state, r)))
            return
        now, since = self.now(), float(r.get("since") or self.now())
        if r["state"] == "built":
            p = await asyncio.to_thread(github.pr_for, repo, str(r.get("branch")), tok)
            if p["ok"] and p["number"]:
                await self.set(r, {"state": "pr", "pr": p["number"], "head": p["head"], "since": now, "checks": {}}, "built")
                await self.say(words.NEED_SEEN.format(n=needs.called(self.y.state, r), pr=p["number"]))
            elif now - since > NO_PR:
                await self.stop(r, "stuck", f"no pull request seen from {r.get('branch')} in {words.span(NO_PR)}")
            return
        p = await asyncio.to_thread(github.pull, repo, int(r["pr"]), tok)
        if not p["ok"]:
            return log.info("needs: PR %s: %s", r["pr"], p["error"])
        if p["merged"]:                                  # merged by a person meanwhile: its plug-ins land, the deploy and live follow
            if (why := await self.lands(r)):
                return await self.stop(r, "held", f"PR {r['pr']} was merged by a person; {why}")
            await self.set(r, {"state": "merged", "merged_at": now, "merge": p.get("sha") or ""}, "pr")
            return await self.say(self.merged(r))
        if p["head"] != r.get("head"):                   # a new head (a fix commit): its checks' clock starts again
            r = await self.set(r, {"head": p["head"], "since": now, "checks": {}}, "pr") or r
            since = now
        c = await asyncio.to_thread(github.checks, repo, str(r["head"]), tok)
        if not c["ok"]:
            return log.info("needs: PR %s checks: %s", r["pr"], c["error"])
        ctx: dict[str, list[str]] = c["contexts"]
        if ctx != r.get("checks"):
            r = await self.set(r, {"checks": ctx}, "pr") or r
        if (red := {k: v for k, v in ctx.items() if v[0] in RED}):
            k = int(r.get("fixes") or 0)
            await self.fix(r, "\n".join(f"- {k}: {s} ({d})" for k, (s, d) in sorted(red.items())),
                           f"checks red after {k} fix{'es' * (k != 1)}: {', '.join(sorted(red))}")
        elif len(ctx) >= int(self.conf().get("contexts") or 7) and all(v[0] == "success" for v in ctx.values()):
            await self.read(r)
        elif now - since > float(self.conf().get("checks_minutes") or 40) * 60:     # the final ones counted, the pending named
            wait = ", ".join(sorted(k for k, v in ctx.items() if v[0] not in ("success", *RED)))
            await self.stop(r, "stuck", words.NEED_CHECKS_NEVER.format(
                final=sum(v[0] in ("success", *RED) for v in ctx.values()), of=int(self.conf().get("contexts") or 7),
                span=words.span(now - since), head=str(r["head"])[:7]) + words.NEED_CHECKS_PENDING.format(names=wait) * bool(wait))

    async def late(self, r: dict[str, Any], tok: str, repo: str) -> None:
        """A stuck need with a PR whose head's checks have all come in since, each final: PR again, and the next tick reads it
        as any other, green the cold read and red a fix (2026-11-09: need 81 sat stuck with all seven green). Only when no
        other need is in flight, the queue's own test (two in flight edit the same lines of the same bookkeeping files), read
        before the checks and again after them, since a need can start while they are read; until then it stays stuck and
        says nothing more."""
        if self.flight():
            return
        c = await asyncio.to_thread(github.checks, repo, str(r["head"]), tok)
        ctx: dict[str, list[str]] = c.get("contexts") or {}
        if c["ok"] and len(ctx) >= int(self.conf().get("contexts") or 7) and all(v[0] in ("success", *RED) for v in ctx.values()) and not self.flight():
            await self.set(r, {"state": "pr", "checks": ctx}, "stuck")
            await self.say(words.NEED_LATE.format(n=needs.called(self.y.state, r), pr=r["pr"]))

    async def heads(self, r: dict[str, Any]) -> dict[str, dict[str, str]]:
        """The need's plug-in branches as they stand: need-DAY-N, the name its briefs give, in each plug-in repository the
        rooms run or the need names, ahead of its base."""
        known = {**await asyncio.to_thread(needs.plugins, self.y.room_confs, REPO), **dict(r.get("plugins") or {})}
        return await asyncio.to_thread(needs.ahead, known, f"need-{r['day']}-{r['n']}")

    async def lands(self, r: dict[str, Any], dry: bool = False) -> str:
        """Each plug-in branch the need carries landed (needs.land), or only checked; each why, else ""."""
        return "; ".join([w for k, p in dict(r.get("plugins") or {}).items() if (w := await asyncio.to_thread(needs.land, k, p, dry))])

    def waits(self, r: dict[str, Any]) -> dict[str, Any]:
        """What of the list in force the need's change touches, {} when nothing: [needs] sensitive, else SENSITIVE, the running
        daemon's and never the PR's tree, so a change cannot loosen the list that judges it ([] is none, and nothing is read).
        Every need is judged alike, whoever built it, on the head its cold read read and the merge's sha guard holds: the paths
        it changes against its merge base with origin/main, read by git in the daemon's own tree with no fetch (a rename is
        both its paths), and each branch it carries in a listed plug-in. A change git cannot read waits, never taken as safe."""
        listed = s if isinstance(s := self.conf().get("sensitive", SENSITIVE), (list, tuple)) else SENSITIVE
        if not (entries := [str(e).strip().removeprefix("./").strip("/") for e in listed]):
            return {}
        head, plugs = str(r.get("head") or ""), [k for k in dict(r.get("plugins") or {}) if f"plugin:{k}" in entries]
        p = needs.git(self.tree, "diff", "--name-only", "--no-renames", "-z", f"{BASE}...{head}", "--") if SHA.fullmatch(head) else None
        if p is None or p.returncode:                    # git's own words, each sha as its short form, so they wrap at phone width
            why = re.sub(r"[0-9a-f]{40,64}", lambda m: m[0][:7], words.no_paths(p.stderr)) if p is not None else "its PR's head is no commit"
            return {"head": head, "paths": [], "plugins": plugs, "unread": words.line(why.removeprefix("fatal: "), 120) or "git gave no reason"}
        paths = [x for x in p.stdout.split("\0") if x and any(x == e or x.startswith(f"{e}/") for e in entries if e)]
        return {"head": head, "paths": paths, "plugins": plugs} if paths or plugs else {}

    async def worker(self, r: dict[str, Any], role: str, brief: str, tasks: list[str]) -> dict[str, Any]:
        return dict(await self.y.workers.start(self.room(), brief, account=self.account(), cap_minutes=READ_CAP, tasks=tasks,
                                               more={"need": {"day": r["day"], "n": r["n"], "role": role}}))

    async def fix(self, r: dict[str, Any], what: str, held: str) -> None:
        """A fix worker for `what` while the need's fixes are under [needs] fix_cap, else held with `held`."""
        k, cap = int(r.get("fixes") or 0), int(self.conf().get("fix_cap") or 2)
        if k >= cap:
            return await self.stop(r, "held", held)
        found, plugs = await asyncio.to_thread(needs.plugins, self.y.room_confs, REPO), await self.heads(r)
        out = await self.worker(r, "fix", needs.fix_brief({**r, "plugins": plugs}, REPO, str(self.conf().get("pr_check") or ""), what, found),
                                ["reproduce it", "fix it in a new commit", "the checks green", "REPORT.md"])
        if not out.get("ok"):
            return await self.stop(r, "held", f"the fix worker did not start: {out.get('error')}")
        await self.set(r, {"state": "fixing", "fixes": k + 1, "fix_cap": cap, "worker": out["id"]})
        await self.say(words.NEED_FIXING.format(n=needs.called(self.y.state, r), pr=r["pr"], k=k + 1, cap=cap, why=words.line(what.splitlines()[0], 120)))

    async def read(self, r: dict[str, Any]) -> None:
        r = await self.set(r, {"plugins": await self.heads(r)}) or r          # what the read reads is what the merge lands
        out = await self.worker(r, "read", needs.read_brief(r, REPO, str(self.conf().get("pr_check") or "")),
                                ["worktree at the head", "the diff read against main", "the checks run", "REPORT.md: the verdict"])
        if not out.get("ok"):
            return await self.stop(r, "held", f"the cold read did not start: {out.get('error')}")
        await self.set(r, {"state": "reading", "worker": out["id"]})
        await self.say(words.NEED_READING.format(n=needs.called(self.y.state, r), pr=r["pr"]))

    async def ended(self, row: dict[str, Any], end: dict[str, Any], role: str, day: str, n: int) -> bool:
        """A cold read's or a fix worker's end (Yerkd.need_built hands it here): the need's next step from its first line. Its
        note first: no line is made when the need is gone or another worker's, and every road past that says one."""
        r = needs.find(needs.load(self.y.state), day, n)
        mine = r is not None and r.get("worker") == row["id"]
        self.y.ledger.worker_set(str(row["id"]), relay="skipped",
                                 relay_note=(words.LINE_POSTED if mine else words.LINE_NONE).format(what=f"need {role}"))
        if r is None or not mine:
            return True
        if workers.cut_off(end):                         # no verdict: PR again, a fix's count as it was, the next tick again
            back = {"fixes": max(0, int(r.get("fixes") or 0) - 1)} if role == "fix" else {}
            await self.set(r, {"state": "pr", "worker": "", **back}, "reading", "fixing")
            await self.say(words.NEED_CUT.format(n=needs.called(self.y.state, r), pr=r["pr"], what=words.NEED_CUT_WHAT[role]))
            return True
        first, how = workers.first_line(Path(row["report"])), workers.settled_state(row, workers.tasks_of(Path(row["tasks"])), end)[1]
        verdict, _, why = first.partition(":")
        verdict, why = verdict.strip().strip("`*").lower(), why.strip() or how
        if role == "read" and verdict == "merge":
            await self.merge(r)
        elif role == "read" and verdict == "fix first":
            k = int(r.get("fixes") or 0)
            await self.fix(r, why, f"the cold read said fix first after {k} fix{'es' * (k != 1)}: {why}")
        elif role == "read":
            await self.stop(r, "held", f"the cold read was unsure: {why}" if verdict == "unsure" else f"the cold read gave no verdict: {how}")
        elif verdict == "fixed":
            await self.set(r, {"state": "pr", "worker": ""}, "fixing")
            await self.say(words.NEED_FIXED.format(n=needs.called(self.y.state, r), pr=r["pr"]))
        else:
            await self.stop(r, "held", f"the fix worker could not: {why}" if verdict == "cannot" else f"the fix worker {how}")
        return True

    async def merge(self, r: dict[str, Any]) -> None:
        tok = await asyncio.to_thread(github.token, self.y.private)
        repo = await asyncio.to_thread(github.slug, REPO, str(self.conf().get("repo") or "")) if tok else ""
        if not (tok and repo):
            return await self.stop(r, "held", "the cold read said merge; no GitHub token, so a person merges")
        if (wait := await asyncio.to_thread(self.waits, r)):   # it touches the [needs] sensitive list: nothing merged or landed
            return await self.stop(r, "held", deeper_why(wait), wait)
        if (why := await self.lands(r, dry=True)):          # a plug-in that cannot land: nothing merged, a person decides
            return await self.stop(r, "held", f"the cold read said merge; {why}")
        m = await asyncio.to_thread(github.merge, repo, int(r["pr"]), str(r["head"]), tok)
        if m["ok"] and (why := await self.lands(r)):
            return await self.stop(r, "held", f"PR {r['pr']} merged; {why}")
        if m["ok"]:
            await self.set(r, {"state": "merged", "merged_at": self.now(), "merge": m["sha"], "worker": ""})
            return await self.say(self.merged(r))
        if m.get("code") == 409:                         # the head moved under the read: its checks and a read again
            await self.set(r, {"state": "pr", "worker": ""})
            return await self.say(words.NEED_FIXED.format(n=needs.called(self.y.state, r), pr=r["pr"]))
        await self.stop(r, "held", f"the merge was refused: {m['error']}")

    def merged(self, r: dict[str, Any]) -> str:
        plugs = ", ".join(dict(r.get("plugins") or {}))
        return words.NEED_MERGED.format(n=needs.called(self.y.state, r), pr=r["pr"]) + (words.NEED_LANDED.format(names=plugs) if plugs else "")

    # the deploy, and live
    def holds(self, after: str, r: dict[str, Any]) -> bool:
        """The deployed commit `after` holds the need's merge: its merge commit an ancestor, else (merged by a person, no
        sha) a merge or squash of its PR in after's history."""
        if r.get("merge"):
            return subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", str(r["merge"]), after],
                                  capture_output=True).returncode == 0
        p = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%H", "-E",
                            f"--grep=^Merge pull request #{r['pr']} from |\\(#{r['pr']}\\)$", after], capture_output=True, text=True)
        return bool(p.stdout.strip())

    async def ship(self, r: dict[str, Any], tok: str) -> None:
        rows = [d for d in self.y.ledger.deploys(float(r["merged_at"]) - 1) if d.get("kind") == "deploy"]
        for d in reversed([d for d in rows if d.get("outcome") == "proven"]):
            if await asyncio.to_thread(self.holds, str(d.get("after") or ""), r):
                return await self.live(r)
        running = await asyncio.to_thread(self.units.running)
        if r["state"] == "deploying":
            mine = [d for d in rows if float(d["at"]) >= float(r.get("deploy_at") or 0)]
            if (bad := [d for d in mine if d.get("outcome") not in ("begun", "proven")]) and bad[-1].get("outcome") == "gave up":
                await self.set(r, {"state": "merged", "unit": ""}, "deploying")      # nothing failed, nothing deployed: again
                await self.say(words.NEED_DEPLOY_AGAIN.format(n=needs.called(self.y.state, r), pr=r["pr"],
                                                                why=words.line(str(bad[-1].get("note") or ""), 160)))
            elif bad:
                await self.stop(r, "held", f"deploy failed: {words.line(str(bad[-1].get('note') or bad[-1]['outcome']), 160)}")
            elif not mine and running is not None and r.get("unit") not in running and self.now() - float(r.get("deploy_at") or 0) > 60:
                await self.stop(r, "held", "deploy failed: its unit ended with no deploy row (the fetch, or the unit itself)")
            return
        if not tok or running is None or running or any(d.get("outcome") == "begun" and self.now() - float(d["at"]) < 7200 for d in rows):
            return                                       # no token: a person deploys; one deploy at a time, never two
        uid = uuid.uuid4().hex[:6]
        if (why := await asyncio.to_thread(self.units.start, self.argv(uid, r))):
            return await self.stop(r, "held", f"deploy failed: the unit did not start: {why}")
        await self.set(r, {"state": "deploying", "unit": f"{UNIT}{uid}", "deploy_at": self.now()}, "merged")
        await self.say(words.NEED_DEPLOYING.format(n=needs.called(self.y.state, r), pr=r["pr"]))

    def argv(self, uid: str, r: dict[str, Any]) -> list[str]:
        """The deploy unit: in the daemon's tree, on its PATH, `git fetch origin` through the askpass (the token file's path in
        the environment, its value never), then `yerk deploy origin/main` on the daemon's layer. The askpass is a two-line
        wrapper in the state folder, since git runs GIT_ASKPASS as one path, never through a shell."""
        ask = write_private(self.y.state / "askpass", f"#!/bin/sh\nPYTHONPATH={REPO} exec {sys.executable} -P -m yerk_ai.git_askpass \"$@\"\n")
        ask.chmod(0o700)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_ASKPASS": str(ask), "GIT_TERMINAL_PROMPT": "0",
               "YERK_GITHUB_TOKEN_FILE": str(self.y.private / github.TOKEN)}
        return ["systemd-run", "--user", "--unit", f"{UNIT}{uid}", "--collect", "--quiet", "--working-directory", str(REPO),
                "--description", f"Yerk AI deploy, need {r['n']} of {r['day']}", *[f"--setenv={k}={v}" for k, v in env.items()],
                "--", "/bin/sh", "-c", 'git fetch --quiet origin && exec "$@"', "yerk-deploy", str(Path(sys.executable).parent / "yerk"),
                "deploy", "--private", str(self.y.private), "--state", str(self.y.state), "origin/main"]

    async def live(self, r: dict[str, Any]) -> None:
        """Live: one line in the machine room, one in each room that filed it (each once), never a relay turn; a room in a
        turn has its line after that turn (`tell`, in the background, so neither the machine room's line nor next() waits)."""
        await self.set(r, {"state": "live", "live_at": self.now(), "worker": ""})
        await self.drop(r)
        ab = words.line(str(r["ability"]), 80)
        await self.say(words.NEED_LIVE.format(n=needs.called(self.y.state, r), ability=ab))
        for room in dict.fromkeys(str(a["room"]) for a in r["asks"]):
            if (ch := self.y.channels.get(room)) is not None and room != self.room():
                said = self.tell(ch, room, words.NEED_LIVE_ROOM.format(n=needs.called(self.y.state, r), ability=ab))
                if not self.busy(room):
                    await said
                    continue
                self.y.later.add(t := asyncio.get_running_loop().create_task(said))
                t.add_done_callback(self.y.later.discard)
        await self.next()

    def busy(self, room: str) -> bool:
        return bool(getattr(self.y.rooms.get(room), "busy", False))

    async def tell(self, ch: Any, room: str, line: str) -> None:
        """`line` in `room` once its turn ends, looked at every LIVE_POLL s, at LIVE_CAP s all the same (seen live:
        need 74's line came five seconds into a turn in a busy room and read as that turn's answer)."""
        end = time.monotonic() + LIVE_CAP
        while self.busy(room) and time.monotonic() < end:
            await asyncio.sleep(LIVE_POLL)
        with contextlib.suppress(Exception):
            await ch.notify(room, line)

    async def drop(self, r: dict[str, Any]) -> None:
        """A live need's branches dropped, never in its way: one that stays, or an error, is one log line. Yerk's own goes here
        first and from GitHub only once it is gone here, since the box's sync pushes a local branch GitHub lacks and a branch
        that stays here keeps its pair (no token: GitHub's stays). A plug-in's goes in its own repository only."""
        try:
            name = f"need-{r['day']}-{r['n']}"
            for k, p in dict(r.get("plugins") or {}).items():
                if (why := await asyncio.to_thread(drop_local, p["repo"], p["branch"])):
                    log.info("needs: the %s plug-in's branch %s stays: %s", k, p["branch"], why)
            if (why := await asyncio.to_thread(drop_local, self.tree, name)):
                return log.info("needs: branch %s stays: %s", name, why)
            tok = await asyncio.to_thread(github.token, self.y.private)
            repo = await asyncio.to_thread(github.slug, REPO, str(self.conf().get("repo") or "")) if tok else ""
            if tok and repo:
                code, out = await asyncio.to_thread(github.api, "DELETE", f"/repos/{repo}/git/refs/heads/{name}", tok)
                if code != 204:
                    log.info("needs: branch %s on GitHub: %s", name, github.said(code, out))
        except Exception as e:
            log.info("needs: need %s's branches: %r", r.get("n"), e)

