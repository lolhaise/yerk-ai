"""The render tool: a document in a room's own folders (a .docx, .doc, .odt, .rtf, .xlsx, .pptx or anything LibreOffice
opens) becomes a PDF saved beside it as NAME.pdf, an older one there replaced, and its pages are counted by pypdf, so a
room can say whether it fits a page count and send the PDF; a .pdf has its pages counted alone, nothing run. It is a
tool on Yerk's own MCP server because LibreOffice keeps its pipe in /tmp, else /var/tmp, by a check no setting moves, and a
room's sandbox has neither writable; the daemon runs unsandboxed. Each call gets a fresh profile under <state>/render/,
removed after, so two calls never share one and none leaves a stale lock; one conversion runs at a time across rooms,
since LibreOffice is heavy, and one past RENDER_TIMEOUT is killed with its process group. Its environment is clean, HOME
the profile, with the owner's own data folder kept for fontconfig: without it a document set in Arial renders in DejaVu,
wider, and a page that fits spills (2026-10-28: one page with the owner's Liberation fonts, two without).

A .tex is LaTeX's, never LibreOffice's (which typesets its source as text): latexmk (TeX Live) compiles it, reruns and
bibliography included, with the program the file asks for (`engine`). Since TeX opens any file a name points at and the
daemon reads the owner's secrets, the run goes in bubblewrap (`box`, as channels/page.py's tests do): no network, no
home, state or private folder, the document's folder read-only and a fresh build folder under <state>/render/ the one
thing writable, shell escape off; only a clean run's PDF is copied out beside the .tex, and no bwrap that starts, no run.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import words

RENDER_TIMEOUT = 120         # seconds one conversion may take before LibreOffice is killed
ONE = threading.Lock()       # one conversion at a time across rooms; a caller waits its turn, never fails for it
TEX = {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}   # latexmk's switch for each TeX program
MAGIC = re.compile(r"%\s*!\s*TEX\s+(?:TS-)?program\s*=\s*(\w+)", re.I)       # the editors' mark: % !TEX program = xelatex
LOADS = r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{[^}]*\b(?:%s)\b"
WANTS = (("lualatex", re.compile(r"\\directlua\b|" + LOADS % "luacode")), ("xelatex", re.compile(LOADS % "fontspec|unicode-math|polyglossia")))
FILE_LINE = re.compile(r"(?:\./)?([^:\s][^:]*\.(?:tex|cls|sty|bib|bbl|def|cfg|clo|ltx)):(\d+): (.+)")   # -file-line-error's
LOG_READ = 5_000_000         # bytes of a TeX log read for its first error


class Failed(Exception):
    """A step that stops the call, in words a room can say."""


def pages(pdf: Path) -> int:
    """A PDF's page count by pypdf; one locked only against editing opens with no password (files.pdf's rule)."""
    from pypdf import PdfReader
    try:
        r = PdfReader(pdf)
        locked = r.is_encrypted and not r.decrypt("")
        n = 0 if locked else len(r.pages)
    except Exception as e:                    # whatever a damaged file raises (files.py's rule): said, never a stop
        raise Failed(words.RENDER_NOT_PDF.format(name=pdf.name, why=words.RENDER_DAMAGED)) from e
    if locked:
        raise Failed(words.RENDER_NOT_PDF.format(name=pdf.name, why=words.RENDER_LOCKED))
    return n


def convert(exe: str, doc: Path, state: Path) -> float:
    """One LibreOffice run on `doc` under a fresh profile, removed after whatever happens: the seconds it took, else
    Failed. Popen and not subprocess.run, since run kills only the launcher at its timeout and keeps its pid to itself."""
    (root := state / "render").mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(dir=root))            # 0700, a random name
    argv = [exe, "--headless", "--norestore", "--nologo", f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to", "pdf", "--outdir", str(doc.parent), str(doc)]
    env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": os.environ.get("LANG", "C.UTF-8"), "HOME": str(profile),
           "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")}
    start, t0 = time.time(), time.monotonic()
    try:
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=env, start_new_session=True) as p:
            try:
                _, err = p.communicate(timeout=RENDER_TIMEOUT)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)      # the whole group: soffice.bin and its helpers, not the launcher only
                p.wait()
                raise Failed(words.RENDER_TIMED_OUT.format(name=doc.name, seconds=RENDER_TIMEOUT))
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    pdf = doc.with_name(f"{doc.stem}.pdf")
    if not pdf.is_file() or pdf.stat().st_mtime < start:  # an older NAME.pdf left there is not this call's
        said = next((x.strip() for x in reversed(err.splitlines()) if x.strip()), "")
        raise Failed(words.RENDER_FAILED.format(name=doc.name, why=words.no_paths(said)[-160:] or words.RENDER_NO_WHY))
    return time.monotonic() - t0


def engine(doc: Path) -> str:
    """The TeX program a .tex is for: its own `% !TEX program =` line in its first 20, else lualatex when it or a class or
    package file beside it runs Lua, xelatex when one loads fontspec, unicode-math or polyglossia (pdflatex stops at
    each), else pdflatex, Overleaf's default. Its comments are not read as code."""
    text = doc.read_text(errors="replace")
    for x in text.splitlines()[:20]:
        if (m := MAGIC.match(x.strip())) and m[1].lower() in TEX:
            return m[1].lower()
    near = [f for f in sorted(doc.parent.glob("*.cls")) + sorted(doc.parent.glob("*.sty")) if f.is_file() and not f.is_symlink()]
    body = re.sub(r"(?<!\\)%.*", "", "\n".join([text, *(f.read_text(errors="replace") for f in near[:20])]))
    return next((prog for prog, wants in WANTS if wants.search(body)), "pdflatex")


def box(tex: Path, doc: Path, build: Path) -> list[str] | None:
    """The bubblewrap command a TeX run goes in (channels/page.py's sandbox, for TeX): every namespace its own (no network;
    its own PID namespace, so whatever it starts dies with it), /usr, TeX's tree and TeX's, fontconfig's and the owner's
    font folders read-only, the document's folder read-only, the build folder the one thing writable and its HOME, no
    home, state or private folder, and no environment but PATH, HOME, LANG and the fonts' XDG_DATA_HOME. `tex` is the
    folder of the TeX program's binary. None: no bwrap on the box, so nothing runs (a .tex can name any file: never bare)."""
    if (bwrap := shutil.which("bwrap")) is None:
        return None
    fonts = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "fonts"
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--setenv", "PATH", f"{tex}{os.pathsep}{os.defpath}",
            "--setenv", "HOME", str(build), "--setenv", "LANG", os.environ.get("LANG", "C.UTF-8"), "--setenv", "XDG_DATA_HOME",
            str(fonts.parent), "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for top in ("/bin", "/sbin", "/lib", "/lib32", "/lib64"):            # a merged /usr links these; an older system has them
        if os.path.islink(top):
            args += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            args += ["--ro-bind", top, top]
    for there in ("/etc/texmf", "/etc/fonts", "/etc/alternatives", "/etc/ld.so.cache", "/etc/localtime", "/etc/papersize",
                  "/var/lib/texmf", "/var/cache/fontconfig", str(fonts)):
        args += ["--ro-bind-try", there, there]
    if not tex.is_relative_to("/usr"):        # a TeX Live outside /usr (TinyTeX's ~/.TinyTeX): <its root>/bin/<arch>, the root read-only
        root = tex.parents[1] if len(tex.parents) > 2 and (tex.parents[1] / "texmf-dist").is_dir() else tex
        args += ["--ro-bind", str(root), str(root)]
    return args + ["--ro-bind", str(doc.parent), str(doc.parent), "--bind", str(build), str(build), "--chdir", str(doc.parent)]


def first_error(log: Path) -> str:
    """A TeX log's first error in the words a room fixes it from: -file-line-error's `NAME.tex:12: what`, else its `! what`
    line, with the source line TeX stopped at (its `l.12 ...`); "" when the log has none or is not there."""
    try:
        lines = log.read_bytes()[:LOG_READ].decode(errors="replace").splitlines()
    except OSError:
        return ""
    for i, x in enumerate(lines):
        if (m := FILE_LINE.match(x)) or x.startswith("! "):
            said = f"{Path(m[1]).name}:{m[2]}: {m[3]}" if m else x[2:]
            at = next((y.strip() for y in lines[i + 1:i + 15] if re.match(r"l\.\d+ ", y)), "")
            return f"{said} ({at})" if at else said
    return ""


def compile_tex(prog: str, tex: Path, doc: Path, state: Path) -> float:
    """One latexmk run on `doc` in the sandbox (box), its build folder fresh under <state>/render/ and removed after whatever
    happens; only a clean run's PDF is copied out, a new file over any older NAME.pdf beside the .tex (a link left there is
    replaced, never written through): the seconds it took, else Failed with TeX's first error and its line."""
    (root := state / "render").mkdir(mode=0o700, parents=True, exist_ok=True)
    build = Path(tempfile.mkdtemp(dir=root))              # 0700, a random name
    try:
        if (cmd := box(tex, doc, build)) is None:
            raise Failed(words.RENDER_NO_TEX_BOX)
        argv = cmd + ["latexmk", "-norc", TEX[prog], "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
                      "-no-shell-escape", f"-outdir={build}", doc.name]
        t0 = time.monotonic()
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              errors="replace", env={"PATH": os.defpath}, start_new_session=True) as p:
            try:
                said, _ = p.communicate(timeout=RENDER_TIMEOUT)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGKILL)      # bwrap's group; the sandbox's PID namespace dies with it
                p.wait()
                raise Failed(words.RENDER_TEX_TIMED_OUT.format(name=doc.name, seconds=RENDER_TIMEOUT))
        if said.startswith("bwrap:"):                     # the sandbox would not start, or found no latexmk in it: nothing ran
            raise Failed(words.RENDER_NO_LATEX if "execvp" in said else words.RENDER_NO_TEX_BOX)
        pdf = build / f"{doc.stem}.pdf"
        if p.returncode or pdf.is_symlink() or not pdf.is_file():
            why = first_error(build / f"{doc.stem}.log") or next((x.strip() for x in reversed(said.splitlines()) if x.strip()), "")
            raise Failed(words.RENDER_TEX_FAILED.format(engine=prog, name=doc.name, why=words.no_paths(why)[:200] or words.RENDER_NO_WHY))
        fd, tmp = tempfile.mkstemp(dir=doc.parent, prefix=f".{doc.stem}-", suffix=".pdf")
        with os.fdopen(fd, "wb") as out, pdf.open("rb") as src:
            shutil.copyfileobj(src, out)
        os.replace(tmp, doc.with_name(f"{doc.stem}.pdf"))
        return time.monotonic() - t0
    finally:
        shutil.rmtree(build, ignore_errors=True)


def latex(doc: Path, state: Path) -> dict[str, Any]:
    """A .tex made a PDF beside it (compile_tex), one run at a time with LibreOffice's: the PDF, its pages, the seconds it
    took and the TeX program; latexmk and the program both on the daemon's PATH, else said."""
    prog = engine(doc)
    if not shutil.which("latexmk") or not (exe := shutil.which(prog)):
        return {"ok": False, "error": words.RENDER_NO_LATEX}
    with ONE:
        took = compile_tex(prog, Path(exe).resolve().parent, doc, state)
    pdf = doc.with_name(f"{doc.stem}.pdf")
    return {"ok": True, "pdf": str(pdf), "pages": pages(pdf), "seconds": round(took, 1), "engine": prog}


def run(doc: Path, state: Path) -> dict[str, Any]:
    """The tool's whole call, blocking (the daemon runs it off the loop): {"ok": False, "error"} on any stop, else the PDF,
    its pages and the seconds LibreOffice took."""
    try:
        if doc.suffix.lower() == ".pdf":
            return {"ok": True, "pdf": str(doc), "pages": pages(doc)}
        if doc.suffix.lower() == ".tex":
            return latex(doc, state)
        if not (exe := shutil.which("soffice") or shutil.which("libreoffice")):
            return {"ok": False, "error": words.RENDER_NO_SOFFICE}
        with ONE:
            took = convert(exe, doc, state)
        pdf = doc.with_name(f"{doc.stem}.pdf")
        return {"ok": True, "pdf": str(pdf), "pages": pages(pdf), "seconds": round(took, 1)}
    except Failed as e:
        return {"ok": False, "error": str(e)}
    except (OSError, subprocess.SubprocessError) as e:
        why = getattr(e, "strerror", None) or words.RENDER_NO_WHY
        tex = doc.suffix.lower() == ".tex"
        return {"ok": False, "error": words.RENDER_TEX_FAILED.format(engine="latexmk", name=doc.name, why=why) if tex else
                words.RENDER_FAILED.format(name=doc.name, why=why)}
