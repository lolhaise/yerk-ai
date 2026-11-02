"""Checks the github_state tool (github.look), which reads a branch or a pull request of Yerk's own GitHub repository for an owner room,
on yerkd's token. Offline, a fake GitHub answers in urlopen's place and a git repository of the test's own stands in for Yerk's checkout,
so a branch or a pull request of a private repository is read on the daemon's token, sent in a header and never in a result, with the
branch against the checkout and the pull requests' states and checks. The wall a room met (its web fetch of a private repository's pull
requests came back 404, with no GitHub login in any room) is now the daemon's token, and with none one line names the file that turns it
on and nothing is sent; a token that does not reach the repository, a pull request that is not there, another repository's address, a
each said in words. The card, the allow list and the abilities entry name it."""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from yerk_ai import abilities, card, daemon, github, words
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel

EXAMPLE_PRIVATE = Path(__file__).resolve().parents[1] / "examples" / "private"
TOKEN = "placeholder-github-token-0001"
REPO = "example-owner/example-repo"
NOW = 1_800_000_000.0                                     # the clock the times are said against


class Answer(io.BytesIO):
    status = 200

    def __enter__(self) -> Answer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def gh(monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]) -> list[urllib.request.Request]:
    """urlopen answered by the path after the API's address (a dict or a list is a 200 with that JSON, an int is that
    status with GitHub's message); the requests it got, in order."""
    got: list[urllib.request.Request] = []

    def urlopen(req: urllib.request.Request, timeout: float = 0) -> Answer:
        got.append(req)
        a = answers[req.full_url.removeprefix(github.API)]
        if isinstance(a, int):
            raise urllib.error.HTTPError(req.full_url, a, "no", {}, io.BytesIO(b'{"message": "Not Found"}'))  # type: ignore[arg-type]
        return Answer(json.dumps(a).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(github.time, "time", lambda: NOW)
    return got


def git(tree: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(tree), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *a],
                          capture_output=True, text=True, check=True).stdout.strip()


def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commits: int = 1) -> tuple[Path, list[str]]:
    """Yerk's checkout, a repository of the test's own: main, and need-example-1 with `commits` commits on it; their shas."""
    (tree := tmp_path / "checkout").mkdir()
    git(tree, "init", "-q", "-b", "main")
    git(tree, "-c", "user.name=Example", "-c", "user.email=nobody", "commit", "-q", "--allow-empty", "-m", "base")
    git(tree, "checkout", "-q", "-b", "need-example-1")
    shas = []
    for i in range(commits):
        git(tree, "-c", "user.name=Example", "-c", "user.email=nobody", "commit", "-q", "--allow-empty", "-m", f"step {i + 1}")
        shas.append(git(tree, "rev-parse", "HEAD"))
    monkeypatch.setattr(daemon, "REPO", tree)
    return tree, shas


def room(tmp_path: Path, token: str = TOKEN) -> Yerkd:
    """The example room on a private folder of its own (with the GitHub token unless `token` is ""), Yerk's repository named."""
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    if token:
        (private / "secrets").mkdir(mode=0o700)
        (private / github.TOKEN).write_text(token + "\n")
    (state := tmp_path / "state").mkdir(parents=True)
    y = Yerkd(private, state)
    y.conf["needs"] = {"repo": REPO}
    y.add_room("example-room", ConsoleChannel())
    return y


def ask(y: Yerkd, what: str, name: str = "example-room") -> dict[str, Any]:
    return asyncio.run(y.tool_github_state(name, what))


def pull(n: int, sha: str, state: str = "open", merged_at: str | None = None, draft: bool = False) -> dict[str, Any]:
    return {"number": n, "state": state, "draft": draft, "title": f"need-example-1: an example change {n}",
            "html_url": f"https://github.com/{REPO}/pull/{n}", "head": {"ref": "need-example-1", "sha": sha},
            "base": {"ref": "main"}, "created_at": "2027-01-15T06:00:00Z", "merged_at": merged_at,
            "closed_at": merged_at if state == "closed" else None}


def branch(sha: str) -> dict[str, Any]:
    return {"name": "need-example-1", "commit": {"sha": sha, "commit": {"message": "step 1\n\nthe body",
                                                                       "committer": {"date": "2027-01-15T07:00:00Z"}}}}


PULLS = f"/repos/{REPO}/pulls?head=example-owner:need-example-1&state=all&per_page=5"
BRANCH = f"/repos/{REPO}/branches/need-example-1"


def test_a_pushed_branch_with_its_pull_request_open_is_read_on_the_token_with_its_checks(tmp_path, monkeypatch):
    _, [sha] = checkout(tmp_path, monkeypatch)
    y = room(tmp_path)
    got = gh(monkeypatch, {PULLS: [pull(12, sha)], BRANCH: branch(sha), f"/repos/{REPO}/commits/{sha}/status": {
        "state": "failure", "statuses": [{"context": "tests", "state": "failure", "description": "2 failed"},
                                         {"context": "lint", "state": "success"}]}})
    r = ask(y, "need-example-1")
    assert r == {"ok": True, "repo": REPO, "branch": "need-example-1",
                 "github": {"head": sha[:7], "commit": "step 1", "committed": "60 min ago"},
                 "here": {"head": sha[:7], "same_as_github": True},
                 "pull_requests": [{"number": 12, "state": "open", "title": "need-example-1: an example change 12",
                                    "url": f"https://github.com/{REPO}/pull/12", "branch": "need-example-1", "head": sha[:7],
                                    "into": "main", "when": "opened 2 h 0 min ago", "checks": "failure", "red": ["tests"]}],
                 "next": words.GITHUB_NEXT}
    assert [q.full_url.removeprefix(github.API) for q in got] == [PULLS, BRANCH, f"/repos/{REPO}/commits/{sha}/status"]
    assert all(q.get_header("Authorization") == f"Bearer {TOKEN}" and q.get_method() == "GET" for q in got)   # a header, reads only
    assert TOKEN not in json.dumps(r) and not any(TOKEN in q.full_url for q in got)


def test_the_wall_the_room_met_is_the_daemons_token_now_and_with_none_one_line_names_it_and_nothing_is_sent(tmp_path, monkeypatch):
    """The wall a room met on 2026-10-27: asked whether a worker's committed branch had reached GitHub since, it fetched the
    repository's pull requests from GitHub's API, and a private repository answers a caller with no login 404, and no room
    holds one. Now the tool is in every owner room and the daemon asks on its own token (the test above); with no token it
    says the one file that turns it on, for the owner, and asks GitHub nothing."""
    checkout(tmp_path, monkeypatch)
    y = room(tmp_path, token="")
    got = gh(monkeypatch, {})
    assert ask(y, "need-example-1") == {"ok": False, "error": words.GITHUB_NO_TOKEN} and got == []
    assert "secrets/github.token" in words.GITHUB_NO_TOKEN and "yerk explain github-state" in words.GITHUB_NO_TOKEN


def test_a_branch_committed_here_and_not_pushed_is_not_on_github_and_one_behind_says_how_far(tmp_path, monkeypatch):
    tree, shas = checkout(tmp_path, monkeypatch, commits=3)
    y = room(tmp_path)
    gh(monkeypatch, {PULLS: [], BRANCH: 404})
    r = ask(y, "need-example-1")
    assert r["ok"] and r["github"] is None and r["pull_requests"] == []
    assert r["here"] == {"head": shas[2][:7], "same_as_github": False}            # committed, not pushed
    gh(monkeypatch, {PULLS: [], BRANCH: branch(shas[0])})                         # pushed once, two commits since
    assert ask(y, "need-example-1")["here"] == {"head": shas[2][:7], "same_as_github": False, "not_on_github": 2, "only_on_github": 0}
    gh(monkeypatch, {PULLS: [], BRANCH: branch("f" * 40)})                        # GitHub's head is a commit the checkout lacks
    assert ask(y, "need-example-1")["here"] == {"head": shas[2][:7], "same_as_github": False}
    gh(monkeypatch, {f"/repos/{REPO}/pulls?head=example-owner:only-there&state=all&per_page=5": [],
                     f"/repos/{REPO}/branches/only-there": branch(shas[0])})
    assert ask(y, "only-there")["here"] is None                                  # on GitHub, no branch of that name here
    assert git(tree, "for-each-ref", "--format=%(refname)") == "refs/heads/main\nrefs/heads/need-example-1"   # nothing written


def test_a_pull_request_by_number_merged_says_so_and_one_not_there_or_in_another_repository_is_said(tmp_path, monkeypatch):
    _, [sha] = checkout(tmp_path, monkeypatch)
    y = room(tmp_path)
    got = gh(monkeypatch, {f"/repos/{REPO}/pulls/7": pull(7, sha, "closed", merged_at="2026-12-31T06:00:00Z"),
                           f"/repos/{REPO}/pulls/8": 404, f"/repos/{REPO}": {"full_name": REPO}})
    for asked in ("7", "#7", "PR 7", f"https://github.com/{REPO}/pull/7"):
        r = ask(y, asked)
        assert r["ok"] and r["branch"] == "need-example-1" and r["here"] == {"head": sha[:7], "same_as_github": True}
        assert r["pull_requests"] == [{"number": 7, "state": "merged", "title": "need-example-1: an example change 7",
                                       "url": f"https://github.com/{REPO}/pull/7", "branch": "need-example-1", "head": sha[:7],
                                       "into": "main", "when": "merged 15 days ago"}]           # merged: no checks asked
    assert {q.full_url for q in got} == {github.API + f"/repos/{REPO}/pulls/7"}
    assert ask(y, "8") == {"ok": False, "error": words.GITHUB_NO_PR.format(n="8", repo=REPO)}
    other = ask(y, "https://github.com/someone-else/other-repo/pull/7")
    assert other == {"ok": False, "error": words.GITHUB_OTHER_REPO.format(other="someone-else/other-repo", repo=REPO)}


def test_a_token_that_does_not_reach_the_repository_a_name_that_is_no_branch_and_a_persons_room_are_each_one_line(tmp_path, monkeypatch):
    checkout(tmp_path, monkeypatch)
    y = room(tmp_path)
    got = gh(monkeypatch, {PULLS: 404})
    assert ask(y, "need-example-1") == {"ok": False, "error": words.GITHUB_NO_REACH.format(repo=REPO, why="GitHub said 404: Not Found")}
    got.clear()
    for bad in ("--upload-pack=x", "main..need-example-1", "two words", "need-example-1.lock", ""):
        assert ask(y, bad)["error"] == words.GITHUB_BAD_NAME.format(given=bad), bad
    assert got == []                                                              # nothing sent for a name that is no branch
    y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    assert ask(y, "need-example-1") == {"ok": False, "error": words.GITHUB_OWNER_ROOMS}
    assert ask(y, "need-example-1", name="no-such-room") == {"ok": False, "error": words.GITHUB_OWNER_ROOMS} and got == []
    y.conf["needs"] = {}
    monkeypatch.setattr(github, "slug", lambda tree, named="": named)            # no [needs] repo and no origin on github.com
    y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "owner"}
    assert ask(y, "need-example-1") == {"ok": False, "error": words.GITHUB_NO_REPO} and got == []


def test_github_not_answering_is_said_in_words_never_a_traceback(tmp_path, monkeypatch):
    checkout(tmp_path, monkeypatch)
    y = room(tmp_path)

    def down(req: urllib.request.Request, timeout: float = 0) -> Answer:
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    assert ask(y, "need-example-1") == {"ok": False, "error": words.GITHUB_FAILED.format(why="GitHub did not answer: URLError")}


def test_the_card_row_the_allow_list_and_the_abilitys_state_with_and_without_the_token(tmp_path):
    assert card.step("mcp__yerk__github_state", {"branch_or_pr": "need-example-1"}) == "checking need-example-1 on GitHub"
    assert "mcp__yerk__github_state" in daemon.YERK_TOOLS
    tab = abilities.manifest()
    y = room(tmp_path, token="")
    assert abilities.state("github-state", tab["github-state"], tab, y.memory) == words.ability("needs", provider=tab["github-state"]["provider"])
    (y.private / "secrets").mkdir(mode=0o700)
    (y.private / github.TOKEN).write_text(TOKEN + "\n")
    assert abilities.state("github-state", tab["github-state"], tab, y.memory) == words.ability("on")
