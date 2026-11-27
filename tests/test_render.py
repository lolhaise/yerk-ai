"""Checks the render tool (render.py), which turns a document in a room's work folder into a PDF beside it. Offline, a fake soffice put
first on PATH writes an N-page PDF where LibreOffice would, so a document in the room's work folder comes back as NAME.pdf beside it with
its pages, the call's own profile gone after; a path outside the room's folders is refused and a PDF is counted alone, the fake never run
either time; no LibreOffice on the machine, a LibreOffice that fails and one that hangs past the timeout are each said in words; two calls
at once both finish, one after the other. One test renders a real two-page document, when LibreOffice is on this machine. A .tex goes
to a fake latexmk, never to LibreOffice, run in the real sandbox (bubblewrap), where it finds only the document's folder,
read-only, and no network; its program comes from the file; outside the work folder, with no latexmk or no sandbox
nothing runs; a failed compile says TeX's first error and its line; a link at NAME.pdf is replaced, never written
through; a hang is killed with its sandbox. One test compiles a real two-page .tex, when TeX Live is on this machine."""
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
    assert list(tools["render"].inputSchema["properties"]) == ["path"]
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
