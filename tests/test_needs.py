"""Checks needs, the way a room that cannot do what it was asked files the missing ability for a builder to add. A room that
hits a wall files what it needed (the `need` tool: one line, a proposal of kind `need` on the day's rules-proposals page,
numbered with curate's, and one line in the machine room); the same ability filed again is folded into the open need, never
a second builder; `yerk needs build DAY N` turns it into a builder worker's brief on [needs] account; `yerk needs list` says
is a placeholder (decision 22)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from test_pictures import Rec, Units
from test_remember import daemon

from yerk_ai import cli, context, curate, mcp_server, needs, words
from yerk_ai.config import EXAMPLE_PRIVATE, REPO, budgets

ASK = ("what does the picture in this post say", "web_fetch of the post: its text and the picture's address, no way to look at it",
       "see a picture in a post")
FLYER = ("what is on this flyer", "web_fetch of the page: a login wall", "see pictures in posts")


def ops(tmp_path: Path, *rooms: str):
    """The test daemon with a machine room that keeps its lines, and systemd-run recorded, nothing run."""
    y = daemon(tmp_path, *rooms)
    y.room_confs["ops-room"] = {**y.room_confs["example-room"], "role": "machine", "id": "ops-room-id"}
    y.daily.rooms["ops-room"] = ("ops-room-id", "private")
    rec = Rec()
    y.add_room("ops-room", rec)
    y.workers.launcher = Units()
    y.conf["needs"] = {"auto": False}                  # the hand road; the pipeline's own tests are test_needs_pipeline.py
    return y, rec


def need(y, room: str, asked: str, tried: str, needed: str, shadow: str = "") -> dict:
    return asyncio.run(y.tool_need(room, asked, tried, needed, shadow))


def listed(tmp_path: Path, capsys) -> list[str]:
    assert cli.main(["needs", "list", "--state", str(tmp_path)]) == 0
    return capsys.readouterr().out.splitlines()


def test_a_room_files_one_line_on_the_days_page_and_the_machine_room_hears_it_in_one_line(tmp_path):
    y, rec = ops(tmp_path)
    r = need(y, "example-room", *ASK)
    day, page = r["day"], y.memory.root / needs.FOLDER / f"{r['day']}.md"
    assert r["ok"] and (r["n"], r["count"], r["folded"], r["state"]) == (1, 1, False, "filed")
    assert r["next"] == words.FILED_NEXT.format(clause=words.NEED_CLAUSE) and list(r)[-1] == "next"
    assert "\n## 1. need: see a picture in a post\n\nSTATE: filed, asked 1 time\n\n" in page.read_text()
    rows = [x for x in page.read_text().splitlines() if x.startswith("- ")]
    assert len(rows) == 1 and rows[0].startswith("- example-room, ") and rows[0].endswith(
        f": asked: {ASK[0]}; tried: {ASK[1]}; needed: {ASK[2]} [m:m-1]")
    assert rec.lines == [words.NEED_FILED.format(n=1, room="example-room", ability=ASK[2], day=day)]
    assert len(rec.lines[0].splitlines()) == 1 and rec.lines[0].startswith(f"{words.WORKING} need 1 from example-room")
    assert {oct(p.stat().st_mode & 0o777) for p in (page, y.state / needs.STORE)} == {"0o600"}
    # what a room did anyway rides on its own line behind [shadow]; a retried call files once and is said once
    lease = ("read the scanned lease", "Read on the PDF: no text layer", "read a scanned PDF", "asked the owner to type it out")
    assert need(y, "example-room", *lease)["n"] == 2 and need(y, "example-room", *lease)["again"]
    assert "; needed: read a scanned PDF; did anyway: asked the owner to type it out [shadow] [m:m-1]" in page.read_text()
    assert len(rec.lines) == 2 and needs.find(needs.load(y.state), day, 2)["asks"][0]["shadow"] == lease[3]
    assert need(y, "example-room", "post a reel", "", "upload a video")["ok"] is False and len(rec.lines) == 2


def test_the_need_tool_is_preloaded_and_the_mechanics_say_file_it_in_place_of_a_workaround(tmp_path):
    y = daemon(tmp_path)
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert list(tools["need"].inputSchema["properties"]) == ["asked", "tried", "needed", "shadow"]
    assert "mcp__yerk__need" in y.spec_for("example-room").settings["permissions"]["allow"]
    text = context.MECHANICS.read_text().strip()
    line = next(x for x in text.splitlines() if "`need`" in x)
    assert "never build a workaround" in line and "`shadow`" in line and "[shadow]" in line
    assert context.chars(text) < budgets()["session_start"]["appended"]["mechanics"]


def test_the_brief_from_a_need_carries_every_ask_the_repo_and_the_shape_of_done():
    row = {"day": "2026-10-23", "n": 4, "ability": ASK[2], "state": "filed", "asks": [
        {"room": "example-room", "at": "2026-10-23 09:36", "mid": "m-1", "asked": ASK[0], "tried": ASK[1], "needed": ASK[2], "shadow": ""},
        {"room": "her-room", "at": "2026-10-23 11:02", "mid": "m-7", "asked": FLYER[0], "tried": FLYER[1], "needed": FLYER[2],
         "shadow": "asked her to describe it"}]}
    b = needs.brief(row, Path("/srv/yerk-ai"))
    assert b.startswith('# Yerk: build "see a picture in a post" for every room (need 4 of 2026-10-23)\n\n')
    assert f"{needs.filed(row['asks'][0])}\n{needs.filed(row['asks'][1])}\n" in b and ", 2 times:" in b
    assert "needed: see pictures in posts; did anyway: asked her to describe it [shadow] [m:m-7]" in b
    assert "Repo /srv/yerk-ai " in b and "worktree add <your folder>/wt -b need-2026-10-23-4 main" in b and "abilities.toml" in b
    assert "Never merge, push, open a PR or deploy" in b and "`PR <number>: <what it adds>`" in b
    assert "the full suite green" in b and "no tool call after it" in b and "\N{EM DASH}" not in b
    assert "is a placeholder, never one from the asks above" in b         # a room's asks carry real names; the repo never does


def test_build_starts_one_builder_on_the_needs_account_and_its_end_sets_the_state_list_and_done_follow(tmp_path, capsys):
    y, rec = ops(tmp_path, "example-room", "her-room")
    units, names = y.workers.launcher, [a.name for a in y.accounts]
    y.conf["curate"], y.conf["needs"] = {"account": names[0]}, {"account": names[-1], "auto": False}
    day = need(y, "example-room", *ASK)["day"]
    assert listed(tmp_path, capsys) == [f"{day} 1 filed: {ASK[2]} (asked 1 time)"]
    assert asyncio.run(y.control({"needs": {"action": "build", "day": day, "n": 9}})) == {
        "ok": False, "error": words.NEED_NONE.format(day=day, n=9)}
    r = asyncio.run(y.control({"needs": {"action": "build", "day": day, "n": 1}}))
    assert r["ok"] and (r["room"], r["account"], r["cap_minutes"]) == ("ops-room", names[-1], needs.CAP) and len(units.calls) == 1
    d = Path(r["staging"])
    assert json.loads((d / "worker.json").read_text())["need"] == {"day": day, "n": 1} and set(rec.cards) == {"ops-room"}
    assert (d / "BRIEF.md").read_text() == needs.brief(needs.find(needs.load(y.state), day, 1), REPO)
    assert (d / "TASKS.md").read_text().splitlines()[0] == "- [ ] worktree on its own branch"
    assert listed(tmp_path, capsys) == [f"{day} 1 building {r['id']}: {ASK[2]} (asked 1 time)"]
    # the same ability from another room is folded into it: its count, no second builder, and build refuses a second
    f = need(y, "her-room", *FLYER)
    assert (f["n"], f["count"], f["folded"]) == (1, 2, True) and len(units.calls) == 1
    assert rec.lines[-1] == words.NEED_AGAIN.format(n=1, room="her-room", count=2, ability=ASK[2])
    assert asyncio.run(y.control({"needs": {"action": "build", "day": day, "n": 1}})) == {
        "ok": False, "error": words.NEED_BUSY.format(n=1, state=f"building {r['id']}")}
    (d / "REPORT.md").write_text(f"PR 123: web_image reads a picture in a post\nbranch need-{day}-1 at abc1234\n")
    (d / "exit.json").write_text(json.dumps({"result": "success", "code": "exited", "status": "0"}))
    asyncio.run(y.workers.complete(r["id"]))
    assert rec.lines[-1] == words.NEED_PR.format(n=1, pr=123, ability=ASK[2]) and y.ledger.worker_get(r["id"])["relay"] == "skipped"
    assert listed(tmp_path, capsys) == [f"{day} 1 PR 123: {ASK[2]} (asked 2 times)"]
    text = (y.memory.root / needs.FOLDER / f"{day}.md").read_text()
    assert "STATE: PR 123, asked 2 times" in text and text.count("\n## 1. need: ") == 1      # each change rewrites its one block
    assert cli.main(["needs", "done", day, "1", "123", "--state", str(tmp_path), "--private", str(EXAMPLE_PRIVATE)]) == 0
    assert capsys.readouterr().out.strip() == f"need 1 of {day}: merged, PR 123"
    assert listed(tmp_path, capsys) == [f"{day} 1 merged, PR 123: {ASK[2]} (asked 2 times)"]
    # a wall met again after the merge is a new need; a builder that ends with no branch files it again, to be built again
    again = need(y, "example-room", *ASK)
    assert (again["n"], again["folded"]) == (2, False)
    r2 = asyncio.run(y.control({"needs": {"action": "build", "day": day, "n": 2}}))
    (Path(r2["staging"]) / "REPORT.md").write_text("no branch: the suite would not run\n")
    (Path(r2["staging"]) / "exit.json").write_text(json.dumps({"result": "exit-code", "code": "exited", "status": "1"}))
    asyncio.run(y.workers.complete(r2["id"]))
    assert rec.lines[-1].startswith(f"{words.NOT_DONE} need 2: its builder ") and rec.lines[-1].endswith(f"; filed again: {ASK[2]}")
    assert listed(tmp_path, capsys)[0].startswith(f"{day} 2 filed, last build {r2['id']}: ")
    r3 = asyncio.run(y.control({"needs": {"action": "build", "day": day, "n": 2}}))
    assert r3["ok"] and len(units.calls) == 3
    # a report that says "no branch" built nothing, whatever PR or branch its why names, on a clean exit too
    (Path(r3["staging"]) / "REPORT.md").write_text(f"no branch: PR 98 already does it, so branch need-{day}-2 was never made\n")
    (Path(r3["staging"]) / "exit.json").write_text(json.dumps({"result": "success", "code": "exited", "status": "0"}))
    asyncio.run(y.workers.complete(r3["id"]))
    assert listed(tmp_path, capsys)[0].startswith(f"{day} 2 filed, last build {r3['id']}: ")


def test_a_persons_room_files_a_need_about_its_own_files_only_never_one_naming_another_rooms(tmp_path):
    y, rec = ops(tmp_path, "example-room", "her-room")
    other = tmp_path / "rooms" / "example-room" / "work" / "notes.md"
    for tok in (str(other), "../../example-room/work/notes.md", "rooms/example-room/work/notes.md", "~/.ssh/id_ed25519"):
        assert need(y, "her-room", "read the notes", f"Read {tok}: refused", "read another room's notes") == {
            "ok": False, "line": words.NEED_NOT_YOURS.format(path=words.no_paths(tok))}
    assert needs.load(y.state) == [] and rec.lines == [] and not (y.memory.root / needs.FOLDER).exists()
    own = tmp_path / "rooms" / "her-room" / "work" / "inbox" / "m-3" / "lease.pdf"
    assert need(y, "her-room", "read my lease", f"Read {own} and ./inbox/m-3/lease.pdf: no text layer", "read a scanned PDF")["ok"]
    theirs = tmp_path / "rooms" / "her-room" / "work" / "notes.md"
    assert need(y, "example-room", "read the notes", f"Read {theirs}: a scan", "read a scanned PDF")["folded"]   # the owner's room


def test_curates_rewrite_of_the_page_keeps_each_need_its_number_and_its_block(tmp_path, monkeypatch):
    y, rec = ops(tmp_path)
    day = need(y, "example-room", *ASK)["day"]
    fake = [{"n": i, "kind": "cut", "label": f"rule {i}", "edits": [["f", i, f"- rule {i}", ""]], "why": "w", "rows": 0,
             "evidence": "e", "saved": 10 - i} for i in (1, 2)]
    fake[1]["needs"], fake[1]["why"] = [1], "it fits once 1 are applied"   # a prerequisite moves with the number it names
    monkeypatch.setattr(curate, "propose", lambda *a: (fake, [], {}))
    took, real = [], curate.memory.daily_lock                  # the rewrite holds the needs' lock, so a need filed meanwhile waits
    monkeypatch.setattr(curate.memory, "daily_lock", lambda p: took.append(p) or real(p))
    out = curate.rules(y.private, y.state, y.memory, False, time.time(), post=lambda p: {"ok": True})
    assert [q["n"] for q in out["proposals"]] == [2, 3] and out["proposals"][1]["needs"] == [2] and took == [y.memory.daily]
    page = Path(out["file"]).read_text()
    assert "\n## 2. cut: rule 1 (-9 chars)\n" in page and "\n## 3. cut: rule 2 (-8 chars)\n" in page
    assert "\nWHY: it fits once 2 are applied\n" in page and out["proposals"][1]["why"] == "it fits once 2 are applied"
    assert f"\n## 1. need: {ASK[2]}\n\nSTATE: filed, asked 1 time\n" in page and out["day"] == day
    assert [q["n"] for q in json.loads((y.state / curate.PROPOSED).read_text())["proposals"]] == [2, 3]
    assert need(y, "example-room", "post a reel", "no upload tool", "upload a video")["n"] == 4     # past every number of the day
    page = Path(out["file"]).read_text()
    assert "\n## 2. cut: rule 1 (-9 chars)\n" in page and f"\n## 1. need: {ASK[2]}\n" in page and "\n## 4. need: upload a video\n" in page
    assert curate.PROPOSALS == needs.FOLDER


def test_one_ability_said_twice_is_one_need_and_two_abilities_are_two():
    assert needs.same("see a picture in a post", "see pictures in posts") and needs.same("read a scanned PDF", "read scanned PDFs")
    assert needs.same("see a picture in a post", "see the picture in a tweet's post")
    assert not needs.same("see a picture in a post", "upload a video") and not needs.same("read a scanned PDF", "send an email")
    assert not needs.same("", "see a picture")
    assert needs.same("read a PDF", "read a scanned PDF from an email") and not needs.same("see", "see a picture")   # inside, 2+ words
