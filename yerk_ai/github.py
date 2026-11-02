"""Talks to GitHub for the daemon: finds each pull request of the self-upgrade loop, reads its checks and merges it, and looks up a
the sha guard. urllib only, 20 s a call, every failure as words, never a traceback. The token is <private>/secrets/github.token
(one line, 0600), read the way transcribe reads its keys; it never reaches argv, a log, a card or a test. With no token every
step here is skipped (the caller says so once) and a person merges as before."""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API, TIMEOUT = "https://api.github.com", 20.0
TOKEN = Path("secrets") / "github.token"
SLUG = re.compile(r"github\.com[:/]+([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


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
