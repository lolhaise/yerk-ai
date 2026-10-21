"""Checks `yerk docs` (docs.py), which keeps the Claude docs Yerk AI runs on as files every room can recall. The cases: the fetcher
against a local site (no network), a kept page headed by its source, fetch time and hash, a page whose hash moved said once in the morning
diff, a failure that keeps the old page and leaves the refresh due, the docs folder as a memory root of class shared in every room (one
docs/claude/<slug>.md:line, the doctor's weekly refresh, the command, and a room asking how its rules are written."""
import hashlib
import http.server
import shutil
import threading
import time
from pathlib import Path

import pytest

from test_doctor import tree  # noqa: F401  (the doctor's healthy install: the example layer, a day log, a fresh index)
from yerk_ai import abilities, cli, docs, doctor, memory, morning, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import DOCS_PAGES, EXAMPLE_PRIVATE
from yerk_ai.ledger import Ledger

PAGE = (Path(__file__).parent / "fixtures" / "docs" / "page.md").read_bytes()
DAY = 86400.0


@pytest.fixture
def site():
    """A docs site on 127.0.0.1: /page answers Markdown, /html answers HTML, anything else 404; each request's Accept kept."""
    seen, body = list[str](), {"page": PAGE}

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(str(self.headers.get("Accept")))
            kind, data = {"/page": ("text/markdown; charset=utf-8", body["page"]), "/html": ("text/html", b"<html></html>")}.get(self.path, ("", b""))
            self.send_response(200 if kind else 404)
            self.send_header("Content-Type", kind or "text/plain")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen, body
    srv.shutdown()
    srv.server_close()


def test_a_page_is_fetched_as_markdown_headed_by_its_source_time_and_hash_and_a_moved_one_is_said_once(site, tmp_path):
    url, seen, body = site
    conf = {"pages": {"guide": f"{url}/page"}}
    assert docs.refresh(tmp_path, conf, now=1000.0)["new"] == ["guide"] and seen == ["text/markdown"]
    page = tmp_path / "docs" / "claude" / "guide.md"
    assert docs.kept(page) == {"source": f"{url}/page", "fetched": docs.kept(page)["fetched"], "sha256": hashlib.sha256(PAGE).hexdigest()}
    assert page.read_bytes().endswith(PAGE) and page.stat().st_mode & 0o777 == 0o600
    assert docs.refresh(tmp_path, conf, now=2000.0)["same"] == ["guide"] and docs.changed(tmp_path, 0) == []     # first fetch: new, not changed
    body["page"] = PAGE + b"\nA new line.\n"
    assert docs.refresh(tmp_path, conf, now=3000.0)["changed"] == ["guide"]
    key = f"docs guide {hashlib.sha256(body['page']).hexdigest()[:12]}"
    assert docs.changed(tmp_path, 2500.0) == [(key, words.line(f"{words.WORKING} docs changed: guide"))]
    assert docs.changed(tmp_path, 3000.0) == []                                       # the diff after it has said it


def test_a_page_that_fails_or_is_not_markdown_keeps_its_old_file_and_the_refresh_stays_due(site, tmp_path):
    url, _, _ = site
    docs.refresh(tmp_path, {"pages": {"guide": f"{url}/page"}}, now=1000.0)
    assert not docs.due(tmp_path, {}, 1000.0 + 6 * DAY) and docs.due(tmp_path, {}, 1000.0 + 7 * DAY)
    r = docs.refresh(tmp_path, {"pages": {"guide": f"{url}/html", "gone": f"{url}/gone", "Bad Slug": f"{url}/page"}}, now=1000.0 + 7 * DAY)
    assert r["failed"]["guide"] == "ValueError: not Markdown (text/html)" and r["failed"]["gone"].startswith("HTTPError: HTTP Error 404")
    assert r["failed"]["Bad Slug"].startswith("ValueError: a slug is") and docs.failed(r)[0].startswith("docs: 3 pages not fetched, first guide")
    assert docs.kept(tmp_path / "docs" / "claude" / "guide.md")["source"] == f"{url}/page"          # the old page kept
    assert docs.due(tmp_path, {}, 1000.0 + 8 * DAY)                                                  # tried again next run
    assert not docs.due(tmp_path, {"refresh_days": 30}, 1000.0 + 8 * DAY)


def test_the_docs_folder_is_a_memory_root_of_class_shared_in_every_room_cited_as_docs(site, tmp_path):
    url, _, _ = site
    s = memory.settings(EXAMPLE_PRIVATE, tmp_path)
    assert s.docs == (tmp_path / "docs").resolve() and s.docs in s.scope.roots and dict(s.cites)[s.docs] == "docs"
    docs.refresh(tmp_path, {"pages": {"guide": f"{url}/page"}})
    page = s.docs / "claude" / "guide.md"
    n = page.read_text().splitlines().index("Tell Claude what to do instead of what not to do, and say why.") + 1
    assert memory.file_class(page) == ("shared", "") and memory.cite(s, page) == "docs/claude/guide.md" and str(page) in s.scope.files()
    got = memory.read(s, f"docs/claude/guide.md:{n}", lines=1, reader=memory.Reader("a-stranger", "a-person-room"))
    assert got["ok"] and got["text"] == "Tell Claude what to do instead of what not to do, and say why."
    assert s.docs in s.only(EXAMPLE_PRIVATE, "a-room", []).scope.roots                      # a room with its own `also` keeps it


def test_the_default_pages_are_the_guide_the_models_prompting_pages_and_the_claude_code_pages():
    assert DOCS_PAGES["prompting-guide"].endswith("/prompt-engineering/claude-prompting-best-practices")
    assert {"prompting-opus-5-5", "prompting-sonnet-5-5", "prompting-haiku-5-5", "prompting-fable-5-1"} <= set(DOCS_PAGES)
    assert {"code-settings", "code-hooks", "code-memory", "code-sandboxing", "code-mcp", "code-cli-reference", "code-system-prompts"} <= set(DOCS_PAGES)
    assert all(docs.SLUG.fullmatch(k) and words.width(f"{words.WORKING} docs changed: {k}") <= words.PHONE for k in DOCS_PAGES)


def test_yerk_doctor_refreshes_the_docs_once_a_week_and_says_a_page_it_could_not_fetch(site, tree, capsys):  # noqa: F811
    url, seen, _ = site
    priv, state, *_ = tree
    (priv / "yerkd.toml").write_text(f'[docs.pages]\nguide = "{url}/page"\n')
    assert doctor.main(priv, state) == 0 and seen == []                    # the doctor's tests and checks: no fetch
    assert doctor.main(priv, state, refresh=True) == 0 and seen == ["text/markdown"]
    assert doctor.main(priv, state, refresh=True) == 0 and len(seen) == 1   # inside the week: nothing fetched
    (priv / "yerkd.toml").write_text(f'[docs]\nrefresh_days = 1\n[docs.pages]\nguide = "{url}/page"\ngone = "{url}/gone"\n')
    assert doctor.run(priv, state, now=time.time() + 2 * DAY, refresh=True)[0].startswith("docs: 1 page not fetched, first gone: HTTPError")
    accounts, cool, led = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"), Ledger(state / "ledger.sqlite")
    lines, _ = morning.build(priv, state, accounts, cool, led, {"started": time.time() - DAY, "units": []}, time.time() + 2 * DAY)
    assert any(x.startswith(f"{words.NOT_DONE} doctor: 1 found, docs: 1 page") for x in lines)


def test_the_morning_diff_says_a_changed_page_as_a_finding(site, tmp_path):
    url, _, body = site
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    docs.refresh(state, {"pages": {"guide": f"{url}/page"}}, now=time.time() - 60)
    body["page"] = PAGE + b"\nA new line.\n"
    docs.refresh(state, {"pages": {"guide": f"{url}/page"}})
    accounts, cool, led = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"), Ledger(state / "ledger.sqlite")
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": time.time() - DAY, "units": []}, time.time())
    assert words.line(f"{words.WORKING} docs changed: guide") in lines and [k for k in stamp["findings"] if k.startswith("docs guide ")]


def test_yerk_docs_refreshes_lists_shows_and_prints_the_shape_report(site, tmp_path, capsys):
    url, _, _ = site
    priv = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    (priv / "yerkd.toml").write_text(f'[docs.pages]\nguide = "{url}/page"\n')
    args = ["--private", str(priv), "--state", str(tmp_path / "state")]
    assert cli.main(["docs", "refresh", *args]) == 0 and capsys.readouterr().out == "docs: 1 new, 0 changed, 0 the same\n"
    assert cli.main(["docs", "list", *args]) == 0 and capsys.readouterr().out.startswith("guide: fetched ")
    assert cli.main(["docs", "show", "guide", *args]) == 0 and capsys.readouterr().out.endswith(PAGE.decode())
    assert cli.main(["docs", "show", "nope", *args]) == 2 and "pages: guide" in capsys.readouterr().err
    assert cli.main(["docs", "shape", *args]) == 0 and capsys.readouterr().out.endswith("rule shape: every count is within budgets.toml [shape]\n")
    assert cli.main(["docs", "shape", "--rules", *args]) == 0 and capsys.readouterr().out.startswith("- no why: a line holding never")


def test_a_room_asking_how_its_rules_are_written_gets_the_shape_check_and_the_docs_first(tmp_path):
    s = memory.settings(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("are your rules written the way the claude docs say?", s)
    assert sorted(e["key"] for e in found) == ["claude-docs", "rule-shape"]
    assert all("docs/" in e["text"] for e in found)


def test_a_page_cut_off_mid_read_is_a_failure_of_that_page_and_never_stops_the_doctor(site, tree, monkeypatch):  # noqa: F811
    import http.client
    url, _, _ = site
    priv, state, *_ = tree
    (priv / "yerkd.toml").write_text(f'[docs.pages]\nguide = "{url}/page"\n')

    def cut(u):
        raise http.client.IncompleteRead(b"half")
    monkeypatch.setattr(docs, "fetch", cut)
    assert doctor.run(priv, state, refresh=True)[0].startswith("docs: 1 page not fetched, first guide: IncompleteRead")
    monkeypatch.setattr(docs, "refresh", lambda *a, **k: 1 / 0)
    out = doctor.run(priv, state, refresh=True)
    assert out[0] == "docs: not refreshed, ZeroDivisionError: division by zero" and len(out) == 1
