"""The render tool: a document in a room's own folders (a .docx, .doc, .odt, .rtf, .xlsx, .pptx or anything LibreOffice
opens) becomes a PDF saved beside it as NAME.pdf, an older one there replaced, and its pages are counted by pypdf, so a
room can say whether it fits a page count and send the PDF; a .pdf has its pages counted alone, nothing run. It is a
tool on Yerk's own MCP server because LibreOffice keeps its pipe in /tmp, else /var/tmp, by a check no setting moves, and a
room's sandbox has neither writable; the daemon runs unsandboxed. Each call gets a fresh profile under <state>/render/,
removed after, so two calls never share one and none leaves a stale lock; one conversion runs at a time across rooms,
since LibreOffice is heavy, and one past RENDER_TIMEOUT is killed with its process group. Its environment is clean, HOME
the profile, with the owner's own data folder kept for fontconfig: without it a document set in Arial renders in DejaVu,
wider, and a page that fits spills (2026-10-28: one page with the owner's Liberation fonts, two without).
"""
from __future__ import annotations

import contextlib
import os
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


def run(doc: Path, state: Path) -> dict[str, Any]:
    """The tool's whole call, blocking (the daemon runs it off the loop): {"ok": False, "error"} on any stop, else the PDF,
    its pages and the seconds LibreOffice took."""
    try:
        if doc.suffix.lower() == ".pdf":
            return {"ok": True, "pdf": str(doc), "pages": pages(doc)}
        if not (exe := shutil.which("soffice") or shutil.which("libreoffice")):
            return {"ok": False, "error": words.RENDER_NO_SOFFICE}
        with ONE:
            took = convert(exe, doc, state)
        pdf = doc.with_name(f"{doc.stem}.pdf")
        return {"ok": True, "pdf": str(pdf), "pages": pages(pdf), "seconds": round(took, 1)}
    except Failed as e:
        return {"ok": False, "error": str(e)}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": words.RENDER_FAILED.format(name=doc.name, why=getattr(e, "strerror", None) or words.RENDER_NO_WHY)}
