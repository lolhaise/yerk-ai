"""Talks to GitHub for the daemon: finds each pull request of the self-upgrade loop, reads its checks and merges it, and looks up a
branch or a pull request for your room. For the needs pipeline (pipeline.py): a need's pull request, its head's checks and its merge with
the sha guard; and for an owner's room, the `github_state` tool (look): a branch or a pull request of Yerk's own repository as
GitHub has it now, a red head's why with it (its checks' words, the box CI's record). urllib only, 20 s a call, every failure as words,
never a traceback. The token is <private>/secrets/github.token (one line, 0600), read the way transcribe reads its keys; it never reaches
argv, a log, a card or a test. With no token every step here is skipped (the caller says so once) and a person merges as before."""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import words

API, TIMEOUT = "https://api.github.com", 20.0
TOKEN = Path("secrets") / "github.token"
SLUG = re.compile(r"github\.com[:/]+([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
PR = re.compile(r"(?:#|pr\s*#?\s*|https?://github\.com/([\w.-]+/[\w.-]+)/pull/)?(\d+)/?", re.I)   # 12, #12, PR 12, its address
BRANCH = re.compile(r"(?!-)(?!.*\.\.)(?!.*\.lock$)[\w.-]+(?:/[\w.-]+)*")         # git's own rules, kept narrow: no option, no range
SAYS = re.compile(r"\b(?:failed|failure|errors?|timed out)\b", re.I)  # a record's line on what failed; one of its steps is "failures"
TEST = re.compile(r"[\w./-]+\.py::[\w:.]+(?:\[[^\s,\[\]]*\])?")      # a test as pytest names it; a parameter cut short is left off
LINES, WIDE = 12, 600                                                # a record's lines said, and each one's width


def token(private: Path) -> str:
    try:
        return (private / TOKEN).read_text().strip()
    except OSError:
        return ""


def slug(repo: Path, named: str = "") -> str:
    """OWNER/NAME: [needs] repo, else the tree's origin URL (https or ssh form); "" when neither says one."""
    if named:
        return named.strip("/")
    p = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True)
    return m[1] if (m := SLUG.search(p.stdout.strip())) else ""


def api(method: str, path: str, tok: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """(status, the JSON answer); status 0 and the reason in words when the call never got an answer."""
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "yerk-ai"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except (OSError, ValueError) as e:
        return 0, f"GitHub did not answer: {type(e).__name__}"


def said(code: int, out: Any) -> str:
    if code == 0:
        return str(out)
    return f"GitHub said {code}" + (f": {out['message']}" if isinstance(out, dict) and out.get("message") else "")


def pr_for(repo: str, branch: str, tok: str) -> dict[str, Any]:
    """The open pull request from `branch`: {"ok", "number", "head"}; number 0 when there is none yet."""
    code, out = api("GET", f"/repos/{repo}/pulls?head={repo.split('/')[0]}:{branch}&state=open", tok)
    if code != 200 or not isinstance(out, list):
        return {"ok": False, "error": said(code, out)}
    return {"ok": True, "number": int(out[0]["number"]), "head": str(out[0]["head"]["sha"])} if out else {"ok": True, "number": 0}


def pull(repo: str, number: int, tok: str) -> dict[str, Any]:
    """Pull request N as it is now: {"ok", "head", "merged", "sha" (its merge commit once merged)}."""
    code, out = api("GET", f"/repos/{repo}/pulls/{number}", tok)
    if code != 200 or not isinstance(out, dict):
        return {"ok": False, "error": said(code, out)}
    return {"ok": True, "head": str(out["head"]["sha"]), "merged": bool(out.get("merged")), "sha": str(out.get("merge_commit_sha") or "")}


def checks(repo: str, sha: str, tok: str) -> dict[str, Any]:
    """The head's commit statuses: {"ok", "state" (the combined one), "contexts": {name: [state, description]}}."""
    code, out = api("GET", f"/repos/{repo}/commits/{sha}/status", tok)
    if code != 200 or not isinstance(out, dict):
        return {"ok": False, "error": said(code, out)}
    return {"ok": True, "state": str(out.get("state") or "pending"),
            "contexts": {str(s["context"]): [str(s["state"]), str(s.get("description") or "")] for s in out.get("statuses") or []}}


def merge(repo: str, number: int, sha: str, tok: str) -> dict[str, Any]:
    """Pull request N merged only while its head is `sha` (the sha guard): {"ok", "sha" (the merge commit)}; a 405 (not
    mergeable) or a 409 (the head moved) is words, with its code."""
    code, out = api("PUT", f"/repos/{repo}/pulls/{number}/merge", tok, {"sha": sha, "merge_method": "merge"})
    if code == 200 and isinstance(out, dict) and out.get("merged"):
        return {"ok": True, "sha": str(out.get("sha") or "")}
    return {"ok": False, "code": code, "error": said(code, out)}


def ago(stamp: Any, now: float) -> str:
    try:
        return words.since(now - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return ""


def here(tree: Path, branch: str, theirs: str) -> dict[str, Any] | None:
    """The branch in Yerk's checkout against GitHub's head `theirs` ("" when GitHub has none): its head, whether GitHub has
    that same commit, and, when both commits are in the checkout, how many each side has that the other lacks; None when
    the checkout has no such branch. Read only: no fetch, no ref written."""
    def git(*a: str) -> str:
        p = subprocess.run(["git", "-C", str(tree), *a], capture_output=True, text=True, timeout=20)
        return p.stdout.strip() if p.returncode == 0 else ""
    if not (mine := git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")):
        return None
    out: dict[str, Any] = {"head": mine[:7], "same_as_github": mine == theirs}
    if theirs and mine != theirs and len(n := git("rev-list", "--left-right", "--count", f"{mine}...{theirs}").split()) == 2:
        out["not_on_github"], out["only_on_github"] = int(n[0]), int(n[1])
    return out


def record(folder: Path, sha: str) -> dict[str, Any] | str:
    """The box CI's own record of commit `sha`, <full sha>.txt in `folder` ([needs] ci_log), after its last line naming the
    commit whole, the newest run's header (a run again appends): the tests its lines that say what failed name, in order,
    one cut inside its parameter as the test alone, and those lines; words when there is no record or it says no failure."""
    try:
        text = (folder / f"{sha}.txt").read_text(errors="replace").splitlines() if re.fullmatch(r"[0-9a-f]{40}", sha) else []
    except OSError:
        text = []
    if not text:
        return words.GITHUB_CI_NONE.format(head=sha[:7])
    said = [x for x in text[max((i + 1 for i, x in enumerate(text) if sha in x), default=0):] if SAYS.search(x)]
    if not said:
        return words.GITHUB_CI_CLEAN.format(head=sha[:7])
    return {"failing": list(dict.fromkeys(t for x in said for t in TEST.findall(x) if not t.endswith("..."))),
            "lines": [words.line(x, WIDE) for x in said[:LINES]]}


def row(repo: str, p: dict[str, Any], tok: str, now: float, ci: Path | None = None) -> dict[str, Any]:
    """A pull request as the tool says it: open, draft, merged or closed, when, and an open one's head checks, each red one
    with its own words on GitHub and, `ci` named, the box CI's record of the head (record), else what names it."""
    state = "merged" if p.get("merged_at") else "closed" if p.get("state") == "closed" else "draft" if p.get("draft") else "open"
    sha, at = str(p["head"]["sha"]), p.get("merged_at") or p.get("closed_at") or p.get("created_at")
    out = {"number": int(p["number"]), "state": state, "title": words.line(str(p.get("title") or ""), 120),
           "url": str(p.get("html_url") or ""), "branch": str(p["head"]["ref"]), "head": sha[:7], "into": str(p["base"]["ref"]),
           "when": f"{state if state in ('merged', 'closed') else 'opened'} {ago(at, now)}".strip()}
    if state in ("open", "draft"):
        c = checks(repo, sha, tok)
        out["checks"] = (c["state"] if c["contexts"] else "none yet") if c["ok"] else c["error"]
        if red := {n: d for n, (s, d) in c.get("contexts", {}).items() if s in ("failure", "error")}:
            out["red"], out["ci"] = red, record(ci, sha) if ci else words.GITHUB_NO_CI
    return out


def look(tree: Path, repo: str, asked: str, tok: str, now: float | None = None, ci: Path | None = None) -> dict[str, Any]:
    """The `github_state` tool: pull request N (`asked` 12, #12, PR 12 or its address) or a branch by name in `repo` as GitHub
    has it now, the branch against Yerk's checkout `tree` (here), with the branch's pull requests, newest first, a red head
    read in the box CI's records in `ci` too; the token that does not reach the repository, a pull request that is not
    there and GitHub not answering, each in words."""
    now, asked = time.time() if now is None else now, asked.strip()
    if not repo:
        return {"ok": False, "error": words.GITHUB_NO_REPO}
    if m := PR.fullmatch(asked):
        if m[1] and m[1].lower() != repo.lower():
            return {"ok": False, "error": words.GITHUB_OTHER_REPO.format(other=m[1], repo=repo)}
        code, p = api("GET", f"/repos/{repo}/pulls/{m[2]}", tok)
        if code == 404 and api("GET", f"/repos/{repo}", tok)[0] == 200:
            return {"ok": False, "error": words.GITHUB_NO_PR.format(n=m[2], repo=repo)}
        if code != 200 or not isinstance(p, dict):
            return wall(repo, code, p)
        r = row(repo, p, tok, now, ci)
        return {"ok": True, "repo": repo, "branch": r["branch"], "here": here(tree, r["branch"], str(p["head"]["sha"])),
                "pull_requests": [r], "next": words.GITHUB_NEXT}
    if not BRANCH.fullmatch(asked):
        return {"ok": False, "error": words.GITHUB_BAD_NAME.format(given=words.line(words.no_paths(asked), 60))}
    head = urllib.parse.quote(f"{repo.split('/')[0]}:{asked}", safe=":/")
    code, prs = api("GET", f"/repos/{repo}/pulls?head={head}&state=all&per_page=5", tok)
    if code != 200 or not isinstance(prs, list):
        return wall(repo, code, prs)
    code, b = api("GET", f"/repos/{repo}/branches/{urllib.parse.quote(asked, safe='/')}", tok)
    if code not in (200, 404) or (code == 200 and not isinstance(b, dict)):
        return {"ok": False, "error": words.GITHUB_FAILED.format(why=said(code, b))}
    theirs = str(b["commit"]["sha"]) if code == 200 else ""
    commit = b["commit"].get("commit") or {} if code == 200 else {}
    gh = {"head": theirs[:7], "commit": words.line(str(commit.get("message") or "").partition("\n")[0], 120),
          "committed": ago((commit.get("committer") or {}).get("date"), now)} if theirs else None
    return {"ok": True, "repo": repo, "branch": asked, "github": gh, "here": here(tree, asked, theirs),
            "pull_requests": [row(repo, p, tok, now, ci) for p in prs], "next": words.GITHUB_NEXT}


def wall(repo: str, code: int, out: Any) -> dict[str, Any]:
    """GitHub's refusal: a token that does not reach the repository (401, 403, 404), else GitHub not answering."""
    return {"ok": False, "error": (words.GITHUB_NO_REACH if code in (401, 403, 404) else words.GITHUB_FAILED).format(
        repo=repo, why=said(code, out))}
