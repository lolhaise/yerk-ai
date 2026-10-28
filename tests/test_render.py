"""Checks the render tool (render.py), which turns a document in a room's work folder into a PDF beside it. Offline, a fake soffice put
first on PATH writes an N-page PDF where LibreOffice would, so a document in the room's work folder comes back as NAME.pdf beside it with
its pages, the call's own profile gone after; a path outside the room's folders is refused and a PDF is counted alone, the fake never run
either time; no LibreOffice on the machine, a LibreOffice that fails and one that hangs past the timeout are each said in words; two calls
finish, one after the other. One test renders a real two-page document, when LibreOffice is on this machine."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from yerk_ai import context, mcp_server, render, words
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel

EXAMPLE_PRIVATE = Path(__file__).resolve().parents[1] / "examples" / "private"
SOFFICE = pytest.mark.skipif(not (shutil.which("soffice") or shutil.which("libreoffice")), reason="LibreOffice is not installed")
# the fake: what it is told comes from knobs.json beside it, since the tool hands soffice a clean environment
FAKE = '''
import json, os, subprocess, sys, time
from pathlib import Path
from urllib.parse import unquote

here = Path(__file__).parent
knobs = json.loads((here / "knobs.json").read_text())
args = sys.argv[1:]
profile = Path(unquote(next(a for a in args if a.startswith("-env:UserInstallation=")).split("file://", 1)[1]))
with (here / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"argv": sys.argv, "env": dict(os.environ), "profile": str(profile), "there": profile.is_dir(),
                        "mode": oct(profile.stat().st_mode & 0o777)}) + "\\n")
if knobs.get("hang"):
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (here / "kid").write_text(str(kid.pid))
    time.sleep(60)
try:
    os.close(os.open(here / "busy", os.O_CREAT | os.O_EXCL))
except FileExistsError:
    sys.exit("two conversions at once")
time.sleep(knobs.get("sleep", 0))
os.unlink(here / "busy")
if knobs.get("code"):
    sys.stderr.write(knobs.get("said", ""))
    sys.exit(knobs["code"])
from pypdf import PdfWriter
src, out = Path(args[-1]), Path(args[args.index("--outdir") + 1])
w = PdfWriter()
for _ in range(knobs["pages"]):
    w.add_blank_page(612, 792)
w.write(out / f"{src.stem}.pdf")
'''


def room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **knobs: object) -> tuple[Yerkd, Path, Path]:
    """The example room on a state of its own, its work folder made, and the fake soffice first on PATH."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    (work := tmp_path / "rooms" / "example-room" / "work").mkdir(parents=True, exist_ok=True)
    (bin_ := tmp_path / "bin").mkdir()
    (fake := bin_ / "soffice").write_text(f"#!{sys.executable}{FAKE}")
    fake.chmod(0o755)
    (bin_ / "knobs.json").write_text(json.dumps({"pages": 3, **knobs}))
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ.get('PATH', os.defpath)}")
    return y, work, bin_


def runs(bin_: Path) -> list[dict[str, Any]]:
    """What the fake was run with, a line a run; none when it never ran."""
    f = bin_ / "runs.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.is_file() else []


def ask(y: Yerkd, p: Path) -> dict[str, Any]:
    return asyncio.run(y.tool_render("example-room", str(p)))


def older(p: Path, data: bytes) -> Path:
    """A file written a minute ago."""
    p.write_bytes(data)
    os.utime(p, (time.time() - 60, time.time() - 60))
    return p


def pdf_of(p: Path, n: int) -> Path:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(612, 792)
    w.write(p)
    return p


def docx(p: Path, pages: int) -> Path:
    """A .docx of `pages` pages, a page break between each, written with the standard library only."""
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    brk = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
    body = brk.join(f"<w:p><w:r><w:t>Page {i + 1} of a placeholder.</w:t></w:r></w:p>" for i in range(pages))
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                   'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/'
                   'document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                   '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>{body}'
                   "</w:body></w:document>")
    return p


def test_the_tool_is_preloaded_allowed_in_every_room_and_the_mechanics_send_a_page_count_to_it(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert list(tools["render"].inputSchema["properties"]) == ["path"]
    assert "NAME.pdf" in str(tools["render"].description) and "LibreOffice's, not Word's" in str(tools["render"].description)
    assert "mcp__yerk__render" in y.spec_for("example-room").settings["permissions"]["allow"]
    line = next(x for x in context.MECHANICS.read_text().splitlines() if "`render`" in x)
    assert "does it fit on one page" in line and "never from an estimate off the text" in line


def test_a_document_in_the_work_folder_comes_back_as_its_pdf_beside_it_with_its_pages(tmp_path, monkeypatch):
    y, work, bin_ = room(tmp_path, monkeypatch, pages=3)
    monkeypatch.setenv("YERK_RENDER_PROBE", "the daemon's own")
    doc = older(work / "report.docx", b"PK a placeholder")
    older(work / "report.pdf", b"%PDF an older one")                 # an older NAME.pdf there is replaced
    r = ask(y, doc)
    assert r == {"ok": True, "pdf": str(work / "report.pdf"), "pages": 3, "seconds": r["seconds"]}
    assert isinstance(r["seconds"], float) and r["seconds"] == round(r["seconds"], 1)
    assert render.pages(work / "report.pdf") == 3
    [run] = runs(bin_)
    profile = Path(run["profile"])
    assert run["argv"] == [str(bin_ / "soffice"), "--headless", "--norestore", "--nologo", f"-env:UserInstallation={profile.as_uri()}",
                           "--convert-to", "pdf", "--outdir", str(work), str(doc)]
    assert run["there"] and run["mode"] == "0o700" and profile.parent == tmp_path / "render"
    assert not profile.exists() and list((tmp_path / "render").iterdir()) == []      # the call's own profile, gone after
    env = run["env"]
    assert env["HOME"] == str(profile) and env["PATH"] == os.environ["PATH"] and "YERK_RENDER_PROBE" not in env


def test_a_path_outside_the_rooms_folders_is_refused_in_words_and_nothing_runs(tmp_path, monkeypatch):
    y, work, bin_ = room(tmp_path, monkeypatch)
    (other := tmp_path / "rooms" / "other-room" / "work").mkdir(parents=True)
    assert ask(y, docx(other / "report.docx", 1)) == {"ok": False, "error": words.RENDER_NOT_HERE.format(name="report.docx")}
    assert ask(y, work / "gone.docx") == {"ok": False, "error": words.RENDER_NO_FILE.format(name="gone.docx")}
    assert asyncio.run(y.tool_render("no-such-room", str(work / "gone.docx")))["error"] == words.RENDER_NOT_HERE.format(name="gone.docx")
    assert runs(bin_) == [] and not (tmp_path / "render").exists()


def test_a_pdf_is_counted_alone_and_nothing_runs(tmp_path, monkeypatch):
    y, work, bin_ = room(tmp_path, monkeypatch)
    (inbox := work / "inbox" / "7").mkdir(parents=True)
    assert ask(y, pdf_of(inbox / "scan.pdf", 2)) == {"ok": True, "pdf": str(inbox / "scan.pdf"), "pages": 2}
    (bad := inbox / "torn.pdf").write_bytes(b"%PDF-1.7 and then nothing")
    assert ask(y, bad) == {"ok": False, "error": words.RENDER_NOT_PDF.format(name="torn.pdf", why=words.RENDER_DAMAGED)}
    assert runs(bin_) == []


def test_no_libreoffice_on_the_machine_is_said_in_words(tmp_path, monkeypatch):
    y, work, bin_ = room(tmp_path, monkeypatch)
    (empty := tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert ask(y, docx(work / "report.docx", 1)) == {"ok": False, "error": words.RENDER_NO_SOFFICE}
    assert runs(bin_) == []


def test_a_libreoffice_that_fails_is_said_with_its_last_line_and_an_older_pdf_is_not_taken_for_this_one(tmp_path, monkeypatch):
    y, work, bin_ = room(tmp_path, monkeypatch, code=1, said="warn: fonts are missing\nError: source file could not be loaded\n\n")
    older(work / "report.pdf", b"%PDF an older one")
    r = ask(y, docx(work / "report.docx", 1))
    assert r == {"ok": False, "error": words.RENDER_FAILED.format(name="report.docx", why="Error: source file could not be loaded")}
    assert "report.docx" in str(r["error"]) and list((tmp_path / "render").iterdir()) == []
    (bin_ / "knobs.json").write_text(json.dumps({"code": 1}))
    assert ask(y, work / "report.docx")["error"] == words.RENDER_FAILED.format(name="report.docx", why=words.RENDER_NO_WHY)


def test_a_libreoffice_that_hangs_is_killed_with_its_group_at_the_timeout(tmp_path, monkeypatch):
    y, work, bin_ = room(tmp_path, monkeypatch, hang=True)
    monkeypatch.setattr(render, "RENDER_TIMEOUT", 2)
    t0 = time.monotonic()
    r = ask(y, docx(work / "report.docx", 1))
    assert r == {"ok": False, "error": words.RENDER_TIMED_OUT.format(name="report.docx", seconds=2)} and time.monotonic() - t0 < 30
    kid = int((bin_ / "kid").read_text())                    # the fake's own child: only a group kill stops it
    for _ in range(50):
        try:
            os.kill(kid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(kid, 9)
        pytest.fail("the hanging fake's child outlived the timeout")
    assert list((tmp_path / "render").iterdir()) == [] and not (work / "report.pdf").exists()


def test_two_calls_at_once_both_finish_one_after_the_other(tmp_path, monkeypatch):
    y, work, bin_ = room(tmp_path, monkeypatch, pages=2, sleep=0.5)
    docs = [docx(work / f"{n}.docx", 1) for n in ("first", "second")]

    async def both() -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(y.tool_render("example-room", str(d)) for d in docs)))
    assert [(r["ok"], r.get("pages")) for r in asyncio.run(both())] == [(True, 2), (True, 2)]
    assert len({x["profile"] for x in runs(bin_)}) == 2 and list((tmp_path / "render").iterdir()) == []
    monkeypatch.setattr(render, "ONE", contextlib.nullcontext())       # without the lock the fake sees the two at once
    [lost] = [r for r in asyncio.run(both()) if not r["ok"]]
    assert lost["error"].endswith(": two conversions at once")


@SOFFICE
def test_a_real_two_page_document_renders_to_two_pages(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    (work := tmp_path / "rooms" / "example-room" / "work").mkdir(parents=True)
    r = asyncio.run(y.tool_render("example-room", str(docx(work / "minutes.docx", 2))))
    assert r["ok"] and r["pages"] == 2 and r["pdf"] == str(work / "minutes.pdf"), r
    assert list((tmp_path / "render").iterdir()) == []
