"""Checks the render tool (render.py), which turns a document in a room's work folder into a PDF beside it. Offline, a fake soffice put
first on PATH writes an N-page PDF where LibreOffice would, so a document in the room's work folder comes back as NAME.pdf beside it with
its pages, the call's own profile gone after; a path outside the room's folders is refused and a PDF is counted alone, the fake never run
either time; no LibreOffice on the machine, a LibreOffice that fails and one that hangs past the timeout are each said in words; two calls
at once both finish, one after the other. One test renders a real two-page document, when LibreOffice is on this machine. A .tex goes
to a fake latexmk, never to LibreOffice, run in the real sandbox (bubblewrap), where it finds only the document's folder,
read-only, and no network; its program comes from the file; outside the work folder, with no latexmk or no sandbox
nothing runs; a failed compile says TeX's first error and its line; a link at NAME.pdf is replaced, never written
through; a hang is killed with its sandbox. One test compiles a real two-page .tex, when TeX Live is on this machine. An
.html or .htm goes to a fake Chromium, never to LibreOffice, run in the real sandbox, where it speaks the DevTools protocol
on its fds 3 and 4 and finds only the page's folder, read-only, and no network; the Chromium is the setting, else the PATH
names in order, else Playwright's newest shell before its full build; the box as composed holds no home, state or private
folder; with no sandbox, past a bound or outside the work folder nothing runs; a failed run leaves an older picture as it
was and a link at NAME.png is replaced; a page that never settles is drawn as it stands, a hang is killed with its
sandbox, and show journals whether a picture is beside a page. Three tests draw real pages when a Chromium is here."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import struct
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from yerk_ai import context, mcp_server, render, words
from yerk_ai.channels import page
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
    assert list(tools["render"].inputSchema["properties"]) == ["path", "width", "scale"]
    assert {k: v.get("default") for k, v in tools["render"].inputSchema["properties"].items()} == {"path": None, "width": 1200, "scale": 2}
    said = " ".join(str(tools["render"].description).split())
    assert "An .html in your work folder becomes a picture, NAME.png beside it (an older one replaced): the whole page" in said
    assert "write the page, render it, Read the PNG to see it as they will, then `send` the PNG." in said
    assert " ".join(str(tools["show"].description).split()).endswith(
        "A page meant for the chat is a picture: `render` it and `send` the PNG; their screen is for when they ask to have it up.")
    assert "NAME.pdf" in str(tools["render"].description) and "LibreOffice's, not Word's" in str(tools["render"].description)
    assert "A .tex in your work folder is compiled by LaTeX" in str(tools["render"].description)
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


# A .tex (render.latex): a fake latexmk, run in the real sandbox, reads tex.json from its own folder (bound read-only), says
# what it could read, write and reach in a comment line of the PDF it writes, since its build folder is gone after the call
FAKE_TEX = '''
import json, os, socket, sys, time
from pathlib import Path

here = Path(__file__).parent
knobs = json.loads((here / "tex.json").read_text())
args = sys.argv[1:]
out, src = Path(next(a for a in args if a.startswith("-outdir=")).split("=", 1)[1]), Path(args[-1])
seen = {"argv": args, "env": dict(os.environ), "cwd": os.getcwd(), "src": src.read_text()}
for name, path in knobs.get("read", {}).items():
    try:
        seen[name] = Path(path).read_text()
    except OSError as e:
        seen[name] = type(e).__name__
try:
    (Path.cwd() / "left.txt").write_text("x")
    seen["write"] = "written"
except OSError as e:
    seen["write"] = type(e).__name__
with socket.socket() as s:
    s.settimeout(2)
    try:
        s.connect(("192.0.2.1", 9))
        seen["net"] = "connected"
    except OSError as e:
        seen["net"] = type(e).__name__
if knobs.get("hang"):
    time.sleep(60)
if knobs.get("code"):
    (out / (src.stem + ".log")).write_text(knobs.get("log", ""))
    print(knobs.get("said", ""))
    sys.exit(knobs["code"])
objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [%s] /Count %d >>"
        % (" ".join("%d 0 R" % (3 + i) for i in range(knobs["pages"])), knobs["pages"])]
objs += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"] * knobs["pages"]
pdf, at = b"%PDF-1.4\\n%" + json.dumps(seen).encode() + b"\\n", []
for i, o in enumerate(objs, 1):
    at.append(len(pdf))
    pdf += ("%d 0 obj\\n%s\\nendobj\\n" % (i, o)).encode()
xref = len(pdf)
pdf += ("xref\\n0 %d\\n0000000000 65535 f \\n" % (len(objs) + 1)).encode() + b"".join(b"%010d 00000 n \\n" % a for a in at)
pdf += ("trailer\\n<< /Size %d /Root 1 0 R >>\\nstartxref\\n%d\\n%%%%EOF\\n" % (len(objs) + 1, xref)).encode()
(out / (src.stem + ".pdf")).write_bytes(pdf)
'''
BOXED = pytest.mark.skipif(not page.sandbox_starts() or not os.access("/usr/bin/python3", os.X_OK),
                           reason="bubblewrap is missing or cannot start here, or there is no /usr/bin/python3 for the fake")
TEX_DOC = "\\documentclass{article}\n\\begin{document}\nA placeholder.\n\\end{document}\n"


def tex_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **knobs: object) -> tuple[Yerkd, Path, Path]:
    """The example room with the fake soffice, and a fake latexmk and TeX programs beside it."""
    y, work, bin_ = room(tmp_path, monkeypatch)
    (fake := bin_ / "latexmk").write_text(f"#!/usr/bin/python3{FAKE_TEX}")
    for f in (fake, *(bin_ / p for p in render.TEX)):
        f.touch()
        f.chmod(0o755)
    (bin_ / "tex.json").write_text(json.dumps({"pages": 2, **knobs}))
    return y, work, bin_


def running(mark: str) -> list[str]:
    """The pids whose command line carries `mark`."""
    out = []
    for p in Path("/proc").iterdir():
        with contextlib.suppress(OSError):
            if p.name.isdigit() and mark.encode() in (p / "cmdline").read_bytes():
                out.append(p.name)
    return out


def seen_in(pdf: Path) -> dict[str, Any]:
    """What the fake latexmk saw, from its PDF's comment line."""
    return json.loads(pdf.read_bytes().split(b"\n")[1][1:])


@BOXED
def test_a_tex_compiles_in_the_sandbox_to_its_pdf_beside_it_and_never_goes_to_libreoffice(tmp_path, monkeypatch):
    (canary := tmp_path / "secrets" / "discord.token").parent.mkdir()
    canary.write_text("a placeholder secret")
    y, work, bin_ = tex_room(tmp_path, monkeypatch)
    (bin_ / "tex.json").write_text(json.dumps({"pages": 2, "read": {"secret": str(canary), "layer": str(EXAMPLE_PRIVATE / "rooms.toml"),
                                                                    "beside": str(work / "notes.txt")}}))
    (work / "notes.txt").write_text("the room's own")
    doc = work / "resume.tex"
    doc.write_text(TEX_DOC)
    older(work / "resume.pdf", b"%PDF an older one")               # an older NAME.pdf there is replaced
    r = ask(y, doc)
    assert r == {"ok": True, "pdf": str(work / "resume.pdf"), "pages": 2, "seconds": r["seconds"], "engine": "pdflatex"}
    assert runs(bin_) == [] and render.pages(work / "resume.pdf") == 2       # LibreOffice never ran on the .tex
    seen = seen_in(work / "resume.pdf")
    build = Path(next(a for a in seen["argv"] if a.startswith("-outdir="))[8:])
    assert seen["argv"] == ["-norc", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", "-no-shell-escape",
                            f"-outdir={build}", "resume.tex"]
    assert build.parent == tmp_path / "render" and not build.exists() and list((tmp_path / "render").iterdir()) == []
    assert seen["cwd"] == str(work) and seen["src"] == TEX_DOC and seen["beside"] == "the room's own"
    assert seen["secret"] == seen["layer"] == "FileNotFoundError"            # nothing outside the document's folder is there
    assert seen["write"] == "OSError" and not (work / "left.txt").exists()    # the document's folder is read-only
    assert seen["net"] == "OSError"                                           # no network
    assert set(seen["env"]) == {"PATH", "HOME", "LANG", "XDG_DATA_HOME", "PWD"} and seen["env"]["HOME"] == str(build)
    assert seen["env"]["PATH"].split(os.pathsep)[0] == str(bin_)
    assert [p.name for p in work.iterdir() if p.name.startswith(".")] == []  # no copy left half-way


def test_the_tex_program_is_the_files_own_mark_else_what_it_loads_else_pdflatex(tmp_path):
    def of(text: str, cls: str = "") -> str:
        (d := tmp_path / str(len(list(tmp_path.iterdir())))).mkdir()
        (d / "main.tex").write_text(text)
        if cls:
            (d / "cv.cls").write_text(cls)
        return render.engine(d / "main.tex")
    assert of(TEX_DOC) == "pdflatex" and of("\\usepackage[T1]{fontenc}\n" + TEX_DOC) == "pdflatex"
    assert of("% !TEX program = xelatex\n" + TEX_DOC) == "xelatex" and of("%!TEX TS-program = LuaLaTeX\n" + TEX_DOC) == "lualatex"
    assert of("% !TEX program = context\n\\usepackage{fontspec}\n") == "xelatex"     # a program latexmk is not told: the scan
    assert of("\\usepackage[no-math]{fontspec,xcolor}\n") == "xelatex" and of("\\usepackage{unicode-math}\n") == "xelatex"
    assert of("\\documentclass{cv}\n" + TEX_DOC, cls="\\RequirePackage{fontspec}\n") == "xelatex"   # the class beside it
    assert of("\\directlua{tex.print(1)}\n") == "lualatex" and of("% \\usepackage{fontspec}\n" + TEX_DOC) == "pdflatex"


def test_a_tex_outside_the_work_folder_no_latexmk_and_no_sandbox_are_each_said_and_nothing_runs(tmp_path, monkeypatch):
    shutil.copytree(EXAMPLE_PRIVATE, private := tmp_path / "private")
    (state := tmp_path / "state").mkdir()
    y = Yerkd(private, state)
    y.add_room("example-room", ConsoleChannel())
    (layer_doc := context.layer_dir(private, y.room_confs["example-room"]) / "resume.tex").write_text(TEX_DOC)
    assert asyncio.run(y.tool_render("example-room", str(layer_doc))) == {
        "ok": False, "error": words.RENDER_TEX_NOT_WORK.format(name="resume.tex")}
    (tmp_path / "t").mkdir()
    y, work, bin_ = tex_room(tmp_path / "t", monkeypatch)
    (doc := work / "resume.tex").write_text(TEX_DOC)
    monkeypatch.setenv("PATH", str(bin_))                                     # latexmk and pdflatex, no bwrap
    assert ask(y, doc) == {"ok": False, "error": words.RENDER_NO_TEX_BOX}
    (bin_ / "latexmk").unlink()
    assert ask(y, doc) == {"ok": False, "error": words.RENDER_NO_LATEX}
    assert runs(bin_) == [] and not (work / "resume.pdf").exists() and list((tmp_path / "t" / "render").iterdir()) == []


@BOXED
def test_a_failed_compile_says_texs_first_error_and_its_line_and_an_older_pdf_is_not_taken_for_this_one(tmp_path, monkeypatch):
    log = ("This is pdfTeX, a placeholder log\n(./resume.tex\nLaTeX2e <2025-11-01>\n"
           "./resume.tex:12: Undefined control sequence.\nl.12 \\pdfgentounicode\n                  =1\n"
           "./resume.tex:30: LaTeX Error: Environment itemz undefined.\n")
    y, work, bin_ = tex_room(tmp_path, monkeypatch, code=12, log=log, said="Latexmk: Errors, so I did not complete making targets")
    (doc := work / "resume.tex").write_text(TEX_DOC)
    older(work / "resume.pdf", b"%PDF an older one")
    why = "resume.tex:12: Undefined control sequence. (l.12 \\pdfgentounicode)"
    assert ask(y, doc) == {"ok": False, "error": words.RENDER_TEX_FAILED.format(engine="pdflatex", name="resume.tex", why=why)}
    assert (work / "resume.pdf").read_bytes() == b"%PDF an older one" and list((tmp_path / "render").iterdir()) == []
    (bin_ / "tex.json").write_text(json.dumps({"code": 12, "log": "! Emergency stop.\n<*> resume.tex\n", "said": "x"}))
    assert ask(y, doc)["error"].endswith("resume.tex: Emergency stop.")
    (bin_ / "tex.json").write_text(json.dumps({"code": 12, "said": "Latexmk: Could not find file '/a/b/resume.tex'\n"}))
    assert ask(y, doc)["error"].endswith(": Latexmk: Could not find file 'resume.tex'")       # no log: its last line, no path


@BOXED
def test_a_link_left_at_the_pdfs_name_is_replaced_never_written_through(tmp_path, monkeypatch):
    y, work, bin_ = tex_room(tmp_path, monkeypatch)
    (target := tmp_path / "elsewhere.txt").write_text("not the room's")
    (work / "resume.pdf").symlink_to(target)
    (doc := work / "resume.tex").write_text(TEX_DOC)
    assert ask(y, doc)["pages"] == 2 and not (work / "resume.pdf").is_symlink() and target.read_text() == "not the room's"


@BOXED
def test_a_tex_that_hangs_is_killed_with_its_sandbox_at_the_timeout(tmp_path, monkeypatch):
    y, work, bin_ = tex_room(tmp_path, monkeypatch, hang=True)
    monkeypatch.setattr(render, "RENDER_TIMEOUT", 2)
    (doc := work / "resume.tex").write_text(TEX_DOC)
    t0 = time.monotonic()
    assert ask(y, doc) == {"ok": False, "error": words.RENDER_TEX_TIMED_OUT.format(name="resume.tex", seconds=2)}
    assert time.monotonic() - t0 < 30 and not (work / "resume.pdf").exists() and list((tmp_path / "render").iterdir()) == []
    for _ in range(50):                                   # the fake inside, in the sandbox's PID namespace, dies with it
        if not running(str(tmp_path / "render")):
            break
        time.sleep(0.1)
    else:
        pytest.fail("the hanging fake outlived the timeout")


def test_a_tex_logs_first_error_is_found_with_its_line(tmp_path):
    (log := tmp_path / "x.log").write_text("(./x.tex\n/usr/share/texmf/tex/latex/cv/cv.cls:45: Package fontspec Error: placeholder\n"
                                           "l.45 \\setmainfont\n")
    assert render.first_error(log) == "cv.cls:45: Package fontspec Error: placeholder (l.45 \\setmainfont)"
    log.write_text("Overfull \\hbox (1.0pt too wide) in paragraph at lines 3--4\n! LaTeX Error: File `cv.sty' not found.\n")
    assert render.first_error(log) == "LaTeX Error: File `cv.sty' not found."
    log.write_text("Output written on x.pdf (1 page).\n")
    assert render.first_error(log) == "" and render.first_error(tmp_path / "none.log") == ""


@pytest.mark.skipif(not (shutil.which("latexmk") and shutil.which("pdflatex") and page.sandbox_starts()),
                    reason="TeX Live (latexmk, pdflatex) is not installed, or bubblewrap cannot start here")
def test_a_real_two_page_tex_compiles_to_two_pages(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    (work := tmp_path / "rooms" / "example-room" / "work").mkdir(parents=True)
    (doc := work / "minutes.tex").write_text("\\documentclass{article}\n\\begin{document}\nOne.\n\\newpage\nTwo.\n\\end{document}\n")
    r = asyncio.run(y.tool_render("example-room", str(doc)))
    assert r["ok"] and r["pages"] == 2 and r["pdf"] == str(work / "minutes.pdf") and r["engine"] == "pdflatex", r
    assert list((tmp_path / "render").iterdir()) == [] and sorted(p.name for p in work.iterdir()) == ["minutes.pdf", "minutes.tex"]


# A page (render.picture): a fake Chromium, run in the real sandbox, speaks the DevTools protocol on its fds 3 and 4 as the
# browser does; its knobs come from chromium.json beside it (its folder, outside /usr, is bound read-only), and what it
# saw (its argv, environment, folder, the files it could read, a write, the network and every call) rides back in its
# PNG's text chunk, since nothing it writes leaves the sandbox.
FAKE_CHROMIUM = '''
import base64, json, os, socket, struct, sys, time, zlib
from pathlib import Path

here = Path(__file__).parent
knobs = json.loads((here / "chromium.json").read_text())
seen = {"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "calls": []}
for name, path in knobs.get("read", {}).items():
    try:
        seen[name] = Path(path).read_text()
    except OSError as e:
        seen[name] = type(e).__name__
try:
    (Path.cwd() / "left.txt").write_text("x")
    seen["write"] = "written"
except OSError as e:
    seen["write"] = type(e).__name__
with socket.socket() as s:
    s.settimeout(2)
    try:
        s.connect(("192.0.2.1", 9))
        seen["net"] = "connected"
    except OSError as e:
        seen["net"] = type(e).__name__
if knobs.get("crash"):
    sys.stderr.write("[0101/000000.000000:ERROR:placeholder.cc(1)] a line before the last\\n" + knobs["crash"] + "\\n")
    sys.exit(1)
into, out = os.fdopen(3, "rb", buffering=0), os.fdopen(4, "wb", buffering=0)
buf, view = b"", {}

def say(m):
    out.write(json.dumps(m).encode() + b"\\0")

def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

def png(w, h):
    rows = b"".join(b"\\0" + b"\\x24\\x44\\x66" * w for _ in range(h))
    return (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"seen\\0" + json.dumps(seen).encode()) + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b""))

while True:
    while b"\\0" not in buf:
        got = into.read(65536)
        if not got:
            sys.exit(0)
        buf += got
    one, buf = buf.split(b"\\0", 1)
    m = json.loads(one)
    method, p, r = m["method"], m.get("params", {}), {}
    seen["calls"].append([method, p])
    if method == "Target.createTarget":
        r = {"targetId": "T1"}
    elif method == "Target.attachToTarget":
        r = {"sessionId": "S1"}
    elif method == "Emulation.setDeviceMetricsOverride":
        view = p
    elif method == "Page.navigate":
        seen["url"] = p["url"]
        if not knobs.get("noload"):
            say({"method": "Page.loadEventFired", "params": {"timestamp": 1}, "sessionId": "S1"})
    elif method == "Runtime.evaluate":
        if knobs.get("stuck") or (knobs.get("unsettled") and "fonts" in p["expression"]):
            continue                                       # a page that never settles, or whose scripts never yield
        h = knobs["h"]
        r = {"result": {"type": "object", "value": {"own": h, "sh": max(h, view["height"])}}}
    elif method == "Page.captureScreenshot":
        if knobs.get("hang"):
            time.sleep(60)
        if knobs.get("error"):
            say({"id": m["id"], "error": {"code": -32000, "message": knobs["error"]}, "sessionId": "S1"})
            continue
        data = b"GIF89a not a png" if knobs.get("notpng") else png(view["width"] * view["deviceScaleFactor"], view["height"] * view["deviceScaleFactor"])
        r = {"data": base64.b64encode(data).decode()}
    say({"id": m["id"], "result": r, **({"sessionId": m["sessionId"]} if "sessionId" in m else {})})
'''
PAGE_DOC = "<!doctype html><title>Card</title><body style='margin:0'><div style='height:300px'>A placeholder card.</div></body>"


def page_room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **knobs: object) -> tuple[Yerkd, Path, Path, Path]:
    """The example room on a state of its own (tmp_path/state) with the fake soffice first on PATH, and the fake Chromium
    in a folder outside the state, named by [render] chromium: the daemon, the work folder, soffice's folder, Chromium's."""
    (state := tmp_path / "state").mkdir()
    y, work, bin_ = room(state, monkeypatch)
    (cr := tmp_path / "cr").mkdir()
    (fake := cr / "chrome-headless-shell").write_text(f"#!/usr/bin/python3{FAKE_CHROMIUM}")
    fake.chmod(0o755)
    (cr / "chromium.json").write_text(json.dumps({"h": 300, **knobs}))
    y.conf["render"] = {"chromium": str(fake)}
    return y, work, bin_, cr


def size(png: Path) -> tuple[int, int]:
    """A PNG's width and height from its header."""
    w, h = struct.unpack(">II", png.read_bytes()[16:24])
    return w, h


def seen_by(png: Path) -> dict[str, Any]:
    """What the fake Chromium saw, from its PNG's text chunk."""
    data, i = png.read_bytes(), 8
    while i < len(data):
        n, kind = struct.unpack(">I4s", data[i:i + 8])
        if kind == b"tEXt" and data[i + 8:i + 13] == b"seen\0":
            return json.loads(data[i + 13:i + 8 + n])
        i += 12 + n
    return {}


def binds(args: list[str]) -> dict[str, list[str]]:
    """A bubblewrap command's binds by kind, each the source; the setenv pairs under "env"."""
    out: dict[str, list[str]] = {"--bind": [], "--ro-bind": [], "--ro-bind-try": [], "env": []}
    i = 1
    while i < len(args):
        a = args[i]
        if a in ("--bind", "--ro-bind", "--ro-bind-try", "--symlink", "--setenv"):
            out.setdefault(a if a != "--setenv" else "env", []).append(args[i + 1] if a != "--setenv" else f"{args[i + 1]}={args[i + 2]}")
            i += 3
        elif a in ("--proc", "--dev", "--tmpfs", "--chdir"):
            i += 2
        else:
            i += 1
    return out


def outside_of(sources: list[str], *folders: Path) -> bool:
    """No bind's source is any of `folders` or a folder above one."""
    return not any(f.resolve().is_relative_to(s) for s in sources for f in folders)


def test_a_page_goes_to_the_browser_and_a_document_a_pdf_and_a_tex_go_where_they_went(tmp_path, monkeypatch):
    y, work, bin_ = room(tmp_path, monkeypatch)
    went: list[tuple[str, str]] = []
    monkeypatch.setattr(render, "picture", lambda doc, state, width, scale, setting: went.append(("browser", doc.name)) or {"ok": True})
    monkeypatch.setattr(render, "latex", lambda doc, state: went.append(("latex", doc.name)) or {"ok": True})
    monkeypatch.setattr(render, "convert", lambda exe, doc, state: went.append(("libreoffice", doc.name)) or 0.0)
    monkeypatch.setattr(render, "pages", lambda pdf: went.append(("counted", pdf.name)) or 1)
    for name in ("card.html", "card.htm", "CARD.HTML", "report.docx", "scan.pdf", "cv.tex"):
        assert render.run(work / name, tmp_path)["ok"]
    assert went == [("browser", "card.html"), ("browser", "card.htm"), ("browser", "CARD.HTML"), ("libreoffice", "report.docx"),
                    ("counted", "report.pdf"), ("counted", "scan.pdf"), ("latex", "cv.tex")]


def test_the_chromium_is_the_setting_else_each_path_name_in_order_else_playwrights_newest_shell_then_its_full_build(tmp_path, monkeypatch):
    def exe(p: Path) -> Path:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p.resolve()
    (bin_ := tmp_path / "bin").mkdir()
    monkeypatch.setenv("PATH", str(bin_))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache := tmp_path / "cache"))
    (doc := tmp_path / "card.html").write_text(PAGE_DOC)
    assert render.find_chromium() is None
    assert render.run(doc, tmp_path / "state") == {"ok": False, "error": words.RENDER_NO_CHROMIUM}       # none: the plain line
    assert "[render] chromium" in words.RENDER_NO_CHROMIUM
    pw = cache / "ms-playwright"
    full = exe(pw / "chromium-1300" / "chrome-linux64" / "chrome")
    exe(pw / "chromium-1100" / "chrome-linux64" / "chrome")
    assert render.find_chromium() == full                                    # the newest full build
    exe(pw / "chromium_headless_shell-1100" / "chrome-headless-shell-linux64" / "chrome-headless-shell")
    shell = exe(pw / "chromium_headless_shell-1200" / "chrome-headless-shell-linux64" / "chrome-headless-shell")
    assert render.find_chromium() == shell                                   # the newest shell, before a newer full build
    for name in render.CHROMIUMS:
        exe(bin_ / name)
    for name in render.CHROMIUMS:                                            # each PATH name in its order, before the cache
        assert render.find_chromium() == (bin_ / name).resolve(), name
        (bin_ / name).unlink()
    assert render.find_chromium() == shell
    exe(bin_ / "chromium")
    named = exe(tmp_path / "opt" / "browser" / "chrome")
    assert render.find_chromium(str(named)) == named                         # the setting before everything
    assert render.find_chromium(str(tmp_path / "none")) is None and render.find_chromium("chrome") is None   # a whole path only
    assert render.run(doc, tmp_path / "state", chromium=str(tmp_path / "none")) == {"ok": False, "error": words.RENDER_CHROMIUM_NOT_ONE}
    assert not (tmp_path / "state").exists()                                 # nothing ran


def test_the_pages_box_as_composed_has_no_network_no_home_state_or_private_folder_and_the_build_folder_its_one_writable_bind(tmp_path, monkeypatch):
    (bin_ := tmp_path / "bin").mkdir()
    (fake := bin_ / "bwrap").write_text("#!/bin/sh\nexit 1\n")                # composed only: never run
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ.get('PATH', os.defpath)}")
    state = tmp_path / "state"
    doc, build = state / "rooms" / "example-room" / "work" / "cards" / "card.html", state / "render" / "abc"
    exe = tmp_path / "cr" / "chrome-headless-shell-linux64" / "chrome-headless-shell"
    args = render.page_box(exe, doc, build)
    assert args is not None and args[0] == str(fake)
    assert args[1:6] == ["--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--setenv"]   # no network, no environment
    b = binds(args)
    assert b["--bind"] == [str(build)]                                        # the one writable bind
    assert str(doc.parent) in b["--ro-bind"] and str(exe.parent) in b["--ro-bind"] and "/usr" in b["--ro-bind"]
    fonts = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "fonts"
    assert {"/etc/fonts", "/var/cache/fontconfig", str(fonts)} <= set(b["--ro-bind-try"])
    assert outside_of(b["--bind"] + b["--ro-bind"] + b["--ro-bind-try"], Path.home(), state, EXAMPLE_PRIVATE)
    assert f"HOME={build}" in b["env"] and f"PATH={os.defpath}" in b["env"] and args[args.index("--chdir") + 1] == str(doc.parent)
    usr = render.page_box(Path("/usr/lib/chromium/chromium"), doc, build)     # a Chromium under /usr: /usr is there already
    assert usr is not None and "/usr/lib/chromium" not in binds(usr)["--ro-bind"]
    assert "--no-sandbox" in render.INSIDE and "--no-sandbox" not in args    # its own sandbox off inside the box, never in the box's own words


def test_no_bwrap_starts_nothing_and_says_so(tmp_path, monkeypatch):
    y, work, bin_, cr = page_room(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(bin_))                                     # soffice, no bwrap
    monkeypatch.setattr(os, "posix_spawn", lambda *a, **k: pytest.fail("a process started with no sandbox"))
    (doc := work / "card.html").write_text(PAGE_DOC)
    assert ask(y, doc) == {"ok": False, "error": words.RENDER_NO_PAGE_BOX}
    assert not (work / "card.png").exists() and list((tmp_path / "state" / "render").iterdir()) == [] and runs(bin_) == []


def test_a_width_or_scale_outside_its_bounds_is_refused_with_its_range_and_nothing_runs(tmp_path, monkeypatch):
    y, work, bin_, cr = page_room(tmp_path, monkeypatch)
    monkeypatch.setattr(os, "posix_spawn", lambda *a, **k: pytest.fail("a run started past a bound"))
    (doc := work / "card.html").write_text(PAGE_DOC)
    for width in (199, 3001):
        assert asyncio.run(y.tool_render("example-room", str(doc), width=width)) == {
            "ok": False, "error": words.RENDER_PAGE_WIDTH.format(given=width, least=200, most=3000, default=1200)}
    for scale in (0, 4):
        assert asyncio.run(y.tool_render("example-room", str(doc), scale=scale)) == {
            "ok": False, "error": words.RENDER_PAGE_SCALE.format(given=scale, least=1, most=3, default=2)}
    assert "200 to 3000" in words.RENDER_PAGE_WIDTH.format(given=1, least=200, most=3000, default=1200)
    assert not (work / "card.png").exists() and not (tmp_path / "state" / "render").exists()


@BOXED
def test_a_page_is_drawn_whole_in_the_sandbox_to_its_png_beside_it_and_never_goes_to_libreoffice(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="yerk.daemon")
    (canary := tmp_path / "secrets" / "discord.token").parent.mkdir()
    canary.write_text("a placeholder secret")
    y, work, bin_, cr = page_room(tmp_path, monkeypatch)
    (cr / "chromium.json").write_text(json.dumps({"h": 300, "read": {
        "secret": str(canary), "layer": str(EXAMPLE_PRIVATE / "rooms.toml"), "beside": str(work / "notes.txt")}}))
    (work / "notes.txt").write_text("the room's own")
    (doc := work / "card.html").write_text(PAGE_DOC)
    older(work / "card.png", b"an older picture")                            # an older NAME.png there is replaced
    r = ask(y, doc)
    assert r == {"ok": True, "png": str(work / "card.png"), "width": 2400, "height": 600, "seconds": r["seconds"]}
    assert size(work / "card.png") == (2400, 600) and runs(bin_) == []      # 300 CSS px at scale 2; LibreOffice never ran
    seen = seen_by(work / "card.png")
    build = Path(next(a for a in seen["argv"] if a.startswith("--user-data-dir=")).split("=", 1)[1]).parent
    assert build.parent == tmp_path / "state" / "render" and not build.exists() and list(build.parent.iterdir()) == []
    assert {"--headless", "--no-sandbox", "--remote-debugging-pipe"} <= set(seen["argv"]) and seen["url"] == doc.as_uri()
    assert seen["cwd"] == str(work) and seen["beside"] == "the room's own"
    assert seen["secret"] == seen["layer"] == "FileNotFoundError"            # nothing outside the page's folder is there
    assert seen["write"] == "OSError" and not (work / "left.txt").exists()    # the page's folder is read-only
    assert seen["net"] == "OSError"                                           # no network
    assert set(seen["env"]) == {"PATH", "HOME", "LANG", "XDG_DATA_HOME", "PWD"} and seen["env"]["HOME"] == str(build)
    views = [p for m, p in seen["calls"] if m == "Emulation.setDeviceMetricsOverride"]
    assert views[0] == {"width": 1200, "height": render.VIEW, "deviceScaleFactor": 2, "mobile": False} and views[-1]["height"] == 300
    assert [p.name for p in work.iterdir() if p.name.startswith(".")] == []  # no copy left half-way
    said = [x.getMessage() for x in caplog.records if x.getMessage().startswith("render: ")]
    assert said == [f"render: example-room, card.html, picture 2400x600 px in {r['seconds']} s"]
    (cr / "chromium.json").write_text(json.dumps({"h": 2000}))
    (htm := work / "tall.htm").write_text(PAGE_DOC)
    r = asyncio.run(y.tool_render("example-room", str(htm), width=640, scale=1))   # taller than the window: all of it
    assert (r["width"], r["height"]) == size(work / "tall.png") == (640, 2000) and runs(bin_) == []


@BOXED
def test_a_page_too_tall_for_one_picture_is_refused_with_its_height_and_the_two_ways_out(tmp_path, monkeypatch):
    y, work, bin_, cr = page_room(tmp_path, monkeypatch, h=8001)
    (doc := work / "card.html").write_text(PAGE_DOC)
    older(work / "card.png", b"an older picture")
    error = words.RENDER_PAGE_TALL.format(name="card.html", height=8001, device=16002, scale=2, most=16000)
    assert ask(y, doc) == {"ok": False, "error": error} and "lower scale" in error and "page shorter" in error
    assert (work / "card.png").read_bytes() == b"an older picture" and list((tmp_path / "state" / "render").iterdir()) == []
    (cr / "chromium.json").write_text(json.dumps({"h": 8000}))
    assert asyncio.run(y.tool_render("example-room", str(doc), width=200, scale=2))["height"] == 16000   # the bound itself is drawn


@BOXED
def test_a_failed_run_writes_no_picture_and_leaves_an_older_one_as_it_was(tmp_path, monkeypatch):
    y, work, bin_, cr = page_room(tmp_path, monkeypatch, error="Unable to capture screenshot")
    (doc := work / "card.html").write_text(PAGE_DOC)
    old = older(work / "card.png", b"an older picture")
    stamp = old.stat().st_mtime
    assert ask(y, doc) == {"ok": False, "error": words.RENDER_PAGE_FAILED.format(name="card.html", why="Unable to capture screenshot")}
    (cr / "chromium.json").write_text(json.dumps({"h": 300, "notpng": True}))
    assert ask(y, doc) == {"ok": False, "error": words.RENDER_PAGE_FAILED.format(name="card.html", why=words.RENDER_NOT_PNG)}
    (cr / "chromium.json").write_text(json.dumps({"h": 300, "crash": "[0101/000000.000001:FATAL:placeholder.cc(2)] a placeholder crash"}))
    assert ask(y, doc)["error"].endswith("FATAL:placeholder.cc(2)] a placeholder crash")
    assert old.read_bytes() == b"an older picture" and old.stat().st_mtime == stamp
    assert sorted(p.name for p in work.iterdir()) == ["card.html", "card.png"] and list((tmp_path / "state" / "render").iterdir()) == []


@BOXED
def test_a_link_left_at_the_pictures_name_is_replaced_never_written_through(tmp_path, monkeypatch):
    y, work, bin_, cr = page_room(tmp_path, monkeypatch)
    (target := tmp_path / "elsewhere.png").write_bytes(b"not the room's")
    (work / "card.png").symlink_to(target)
    (doc := work / "card.html").write_text(PAGE_DOC)
    assert ask(y, doc)["ok"] and not (work / "card.png").is_symlink() and size(work / "card.png") == (2400, 600)
    assert target.read_bytes() == b"not the room's"


@BOXED
def test_a_page_that_never_settles_is_drawn_as_it_stands_and_one_whose_scripts_never_yield_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "SETTLE", 1)
    monkeypatch.setattr(render, "STUCK", 1)
    y, work, bin_, cr = page_room(tmp_path, monkeypatch, unsettled=True)
    (doc := work / "card.html").write_text(PAGE_DOC)
    r = ask(y, doc)
    assert r["ok"] and r["note"] == words.RENDER_PAGE_UNSETTLED.format(seconds=1) and size(work / "card.png") == (2400, 600)
    (cr / "chromium.json").write_text(json.dumps({"h": 300, "noload": True}))
    assert ask(y, doc)["note"] == words.RENDER_PAGE_UNSETTLED.format(seconds=1)       # its load never came: as it stands too
    (work / "card.png").unlink()
    (cr / "chromium.json").write_text(json.dumps({"h": 300, "stuck": True}))
    assert ask(y, doc) == {"ok": False, "error": words.RENDER_PAGE_STUCK.format(name="card.html", seconds=1)}
    assert not (work / "card.png").exists() and list((tmp_path / "state" / "render").iterdir()) == []


@BOXED
def test_a_page_run_that_hangs_is_killed_with_its_sandbox_at_the_timeout(tmp_path, monkeypatch):
    y, work, bin_, cr = page_room(tmp_path, monkeypatch, hang=True)
    monkeypatch.setattr(render, "RENDER_TIMEOUT", 2)
    (doc := work / "card.html").write_text(PAGE_DOC)
    older(work / "card.png", b"an older picture")
    t0 = time.monotonic()
    assert ask(y, doc) == {"ok": False, "error": words.RENDER_PAGE_TIMED_OUT.format(name="card.html", seconds=2)}
    assert time.monotonic() - t0 < 30 and (work / "card.png").read_bytes() == b"an older picture"
    for _ in range(50):                                   # the fake inside, in the sandbox's PID namespace, dies with it
        if not running(str(tmp_path / "state" / "render")):
            break
        time.sleep(0.1)
    else:
        pytest.fail("the hanging fake outlived the timeout")


def test_a_page_outside_the_work_folder_is_refused_and_nothing_runs(tmp_path, monkeypatch):
    shutil.copytree(EXAMPLE_PRIVATE, private := tmp_path / "private")
    (state := tmp_path / "state").mkdir()
    y = Yerkd(private, state)
    y.add_room("example-room", ConsoleChannel())
    monkeypatch.setattr(os, "posix_spawn", lambda *a, **k: pytest.fail("a page outside the work folder ran"))
    for name in ("card.html", "card.htm"):
        (doc := context.layer_dir(private, y.room_confs["example-room"]) / name).write_text(PAGE_DOC)
        assert asyncio.run(y.tool_render("example-room", str(doc))) == {"ok": False, "error": words.RENDER_PAGE_NOT_WORK.format(name=name)}
    assert not (state / "render").exists()


def test_show_of_a_page_journals_whether_a_picture_of_it_is_beside_it(tmp_path, monkeypatch, caplog):
    from test_page import plain_daemon
    from test_show import deck, pc
    caplog.set_level(logging.INFO, logger="yerk.daemon")
    y, _, state = plain_daemon(tmp_path)
    pc(tmp_path, monkeypatch)
    f = deck(state, "card.html")
    lines = lambda: [x.getMessage() for x in caplog.records if x.getMessage().startswith("show page: ")]   # noqa: E731
    assert asyncio.run(y.tool_show("example-room", str(f), "Right"))["ok"]
    older(f.with_name("card.png"), b"a picture from before the page changed")
    assert asyncio.run(y.tool_show("example-room", str(f), "Right"))["ok"]
    f.with_name("card.png").write_bytes(b"a picture of it")
    assert asyncio.run(y.tool_show("example-room", str(f), "Right"))["ok"]
    assert lines() == [f"show page: room example-room, card.html, picture beside it: {x}" for x in ("no", "no", "yes")]
    (pdf := f.with_name("deck.pdf")).write_bytes(b"%PDF a placeholder")
    assert asyncio.run(y.tool_show("example-room", str(pdf), "Right"))["ok"] and len(lines()) == 3   # a page's line only


# The real browser: a Chromium found as the daemon finds one, in the real sandbox; skipped, with the reason, when either is missing.
CHROMIUM = render.find_chromium()
REAL = pytest.mark.skipif(CHROMIUM is None or not page.sandbox_starts(),
                          reason="no Chromium on this machine (chrome-headless-shell, chromium, Chrome or Playwright's), or bubblewrap cannot start here")


def draws(doc: Path, state: Path, width: int = 1200, scale: int = 2) -> dict[str, Any]:
    r = render.run(doc, state, width, scale, str(CHROMIUM))
    assert r["ok"], r
    assert (r["width"], r["height"]) == size(Path(r["png"])) and list((state / "render").iterdir()) == []
    return r


@REAL
def test_a_real_page_taller_than_a_screen_and_one_a_line_tall_are_drawn_whole_with_no_band(tmp_path):
    (d := tmp_path / "work").mkdir()
    (tall := d / "tall.html").write_text("<!doctype html><body style='margin:0'><div style='height:2000px;background:#246'>top</div></body>")
    (line := d / "line.html").write_text("<!doctype html><body style='margin:0'><div style='height:40px'>one line</div></body>")
    r = draws(tall, tmp_path / "state")
    assert r["width"] == 1200 * 2 and abs(r["height"] - 2000 * 2) <= 2
    r = draws(line, tmp_path / "state", 600, 1)
    assert r["width"] == 600 and abs(r["height"] - 40) <= 2
    r = draws(line, tmp_path / "state", 333, 3)
    assert r["width"] == 999 and abs(r["height"] - 120) <= 2


@REAL
def test_a_real_page_that_asks_the_network_is_drawn_inside_30_seconds(tmp_path):
    (d := tmp_path / "work").mkdir()
    (net := d / "net.html").write_text("<!doctype html><body style='margin:0'><img src='http://192.0.2.1/a.png'><img "
                                       "src='http://placeholder.invalid/b.png'><script src='http://192.0.2.1/c.js'></script>"
                                       "<div style='height:100px'>the page</div></body>")
    t0 = time.monotonic()
    draws(net, tmp_path / "state", 400, 1)
    assert time.monotonic() - t0 < 30


@REAL
def test_a_real_page_draws_a_sibling_by_its_relative_path_and_its_box_holds_no_home_state_or_private_folder(tmp_path, monkeypatch):
    (d := tmp_path / "work").mkdir()
    (d / "tall.css").write_text("#x { height: 500px; background: #246 }")
    (sib := d / "sibling.html").write_text("<!doctype html><link rel='stylesheet' href='tall.css'><body style='margin:0'>"
                                           "<div id='x'></div><iframe src='file:///etc/hostname'></iframe></body>")
    composed: list[list[str]] = []
    real_box = render.page_box
    monkeypatch.setattr(render, "page_box", lambda *a: composed.append(b := real_box(*a)) or b)
    r = draws(sib, tmp_path / "state", 400, 1)
    assert r["width"] == 400 and r["height"] >= 500                           # the sheet beside it was read: its box is drawn
    b = binds(composed[0])
    assert b["--bind"] == [str(Path(composed[0][composed[0].index("--bind") + 1]))] and str(d) in b["--ro-bind"]
    assert outside_of(b["--bind"] + b["--ro-bind"] + b["--ro-bind-try"], Path.home(), tmp_path / "state", EXAMPLE_PRIVATE)
    assert "/etc" not in b["--ro-bind"] + b["--ro-bind-try"] and "/etc/hostname" not in b["--ro-bind"] + b["--ro-bind-try"]
