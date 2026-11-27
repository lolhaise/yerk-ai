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

An .html or .htm is a page, drawn as a picture (`picture`): a headless Chromium ([render] chromium, else one on PATH, else
Playwright's) in the same kind of box (`walls`, shared with TeX's), its own sandbox off inside it and nowhere else, driven
over --remote-debugging-pipe (the DevTools protocol, a JSON message each way ended by a NUL byte, the standard library
only). The page is laid out `width` CSS pixels wide, given SETTLE seconds to load and settle, measured, and drawn whole at
`scale` device pixels to each, top to bottom, no taller than TALLEST; the PNG comes back over the pipe and only a clean
run's is written beside the page as NAME.png, a new file over whatever had that name.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import math
import os
import re
import select
import shutil
import signal
import struct
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


def walls(build: Path, doc: Path, path: str, there: tuple[str, ...] = (), own: Path | None = None) -> list[str] | None:
    """The bubblewrap command a TeX run and a page run go in (channels/page.py's sandbox, for them): every namespace its own
    (no network; its own PID namespace, so whatever it starts dies with it), /usr, `there` and fontconfig's and the owner's
    font folders read-only, the program's own folder `own` read-only when it lies outside /usr, the document's folder
    read-only, the build folder the one thing writable and its HOME, no home, state or private folder, and no environment
    but PATH (`path`), HOME, LANG and the fonts' XDG_DATA_HOME. None: no bwrap on the box, so nothing runs."""
    if (bwrap := shutil.which("bwrap")) is None:
        return None
    fonts = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "fonts"
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--setenv", "PATH", path,
            "--setenv", "HOME", str(build), "--setenv", "LANG", os.environ.get("LANG", "C.UTF-8"), "--setenv", "XDG_DATA_HOME",
            str(fonts.parent), "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for top in ("/bin", "/sbin", "/lib", "/lib32", "/lib64"):            # a merged /usr links these; an older system has them
        if os.path.islink(top):
            args += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            args += ["--ro-bind", top, top]
    for x in (*there, "/etc/fonts", "/etc/alternatives", "/etc/ld.so.cache", "/etc/localtime", "/var/cache/fontconfig", str(fonts)):
        args += ["--ro-bind-try", x, x]
    if own is not None and not own.is_relative_to("/usr"):
        args += ["--ro-bind", str(own), str(own)]
    return args + ["--ro-bind", str(doc.parent), str(doc.parent), "--bind", str(build), str(build), "--chdir", str(doc.parent)]


def box(tex: Path, doc: Path, build: Path) -> list[str] | None:
    """The walls a TeX run goes in, with TeX's tree and TeX's own /etc and /var folders read-only: `tex` is the folder of
    the TeX program's binary, and a TeX Live outside /usr (TinyTeX's ~/.TinyTeX, <its root>/bin/<arch>) has its root bound
    read-only. None: no bwrap on the box, so nothing runs (a .tex can name any file: never bare)."""
    root = tex.parents[1] if len(tex.parents) > 2 and (tex.parents[1] / "texmf-dist").is_dir() else tex
    return walls(build, doc, f"{tex}{os.pathsep}{os.defpath}", ("/etc/texmf", "/etc/papersize", "/var/lib/texmf"), root)


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


PAGE = (".html", ".htm")     # a page: drawn as a picture, never sent to LibreOffice
WIDTH, SCALE, TALLEST = (200, 3000, 1200), (1, 3, 2), 16_000   # CSS px wide and device px to each (least, most, default); device px tall
SETTLE = 5                   # seconds a page gets to load and settle before it is drawn as it stands
STUCK = 5                    # seconds more an unsettled page gets to be measured at all, else it is refused
VIEW = 800                   # the CSS height of the window a page is laid out in before it is measured
CHROMIUMS = ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome", "chrome")   # on PATH, in this order
PLAYWRIGHT = (("chromium_headless_shell-", ("chrome-headless-shell-linux64/chrome-headless-shell", "chrome-linux/headless_shell")),
              ("chromium-", ("chrome-linux64/chrome", "chrome-linux/chrome")))       # its cache's builds, a shell before a full one
# Chromium's switches inside the box, and only there: its own sandbox off, since the box is the sandbox (page_box)
INSIDE = ("--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--mute-audio", "--no-first-run",
          "--no-default-browser-check", "--disable-extensions", "--disable-background-networking", "--disable-component-update",
          "--disable-sync", "--remote-debugging-pipe")
MEASURE = ("(() => { const e = document.documentElement, r = e.getBoundingClientRect(); return {own: r.bottom + scrollY + "
           "parseFloat(getComputedStyle(e).marginBottom), sh: e.scrollHeight}; })()")
# settled: its fonts loaded, its finite animations ended, its DOM unchanged for a quarter second and two frames drawn
SETTLED = ("(async () => { await document.fonts.ready; await Promise.all(document.getAnimations().filter(a => a.playState === "
           "'running' && a.effect && a.effect.getComputedTiming().endTime !== Infinity).map(a => a.finished.catch(() => 0))); "
           "await new Promise(r => { let t; const done = () => { o.disconnect(); r(); }, o = new MutationObserver(() => { "
           "clearTimeout(t); t = setTimeout(done, 250); }); o.observe(document, {subtree: true, childList: true, attributes: true, "
           "characterData: true}); t = setTimeout(done, 250); }); await new Promise(r => requestAnimationFrame(() => "
           "requestAnimationFrame(r))); return " + MEASURE + "; })()")
PNG = b"\x89PNG\r\n\x1a\n"


def find_chromium(setting: str = "") -> Path | None:
    """The Chromium a page is drawn by, its real path: [render] chromium when set (a whole path), else the first of
    CHROMIUMS on PATH, else the newest headless shell and then the newest full build Playwright left in its cache folder
    ($XDG_CACHE_HOME, else ~/.cache, then ms-playwright). None: none there, or the setting names no program."""
    def runs(p: Path) -> bool:
        return p.is_file() and os.access(p, os.X_OK)
    if setting:
        p = Path(setting).expanduser()
        return p.resolve() if p.is_absolute() and runs(p) else None
    if found := next((x for name in CHROMIUMS if (x := shutil.which(name))), None):
        return Path(found).resolve()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"
    for prefix, inside in PLAYWRIGHT:
        builds = [d for d in cache.glob(prefix + "*") if d.name[len(prefix):].isdigit()]
        for d in sorted(builds, key=lambda d: int(d.name[len(prefix):]), reverse=True):
            if exe := next((d / x for x in inside if runs(d / x)), None):
                return exe.resolve()
    return None


def page_box(exe: Path, doc: Path, build: Path) -> list[str] | None:
    """The walls a page run goes in: the browser's own folder read-only when it lies outside /usr (Playwright's, Chrome's
    /opt), the page's folder read-only, the build folder its HOME and profile and the one thing writable. None: no bwrap on
    the box, so nothing runs (a page's scripts run: never bare)."""
    return walls(build, doc, os.defpath, (), exe.parent)


class Pipe:
    """The DevTools protocol over --remote-debugging-pipe: the daemon's messages on the browser's fd 3, the browser's on its
    fd 4, each a JSON object ended by a NUL byte; every read bounded by a time.monotonic() deadline (TimeoutError past it,
    EOFError when the browser is gone). Events are kept by name only, for `wait`."""

    def __init__(self, out: int, into: int) -> None:
        self.out, self.into, self.buf, self.n, self.seen = out, into, bytearray(), 0, set[str]()

    def read(self, until: float) -> dict[str, Any]:
        while (i := self.buf.find(b"\0")) < 0:
            if (left := until - time.monotonic()) <= 0 or not select.select([self.into], [], [], left)[0]:
                raise TimeoutError
            if not (chunk := os.read(self.into, 1 << 20)):
                raise EOFError
            self.buf += chunk
        msg = json.loads(bytes(self.buf[:i]))
        del self.buf[:i + 1]
        return msg if isinstance(msg, dict) else {}

    def call(self, method: str, params: dict[str, Any], until: float, session: str = "") -> dict[str, Any]:
        """One command's result; an answer the browser gives as an error raises ValueError with its message."""
        self.n += 1
        data = json.dumps({"id": self.n, "method": method, "params": params, **({"sessionId": session} if session else {})}).encode()
        try:
            left = memoryview(data + b"\0")
            while left:
                left = left[os.write(self.out, left):]
        except BrokenPipeError as e:
            raise EOFError from e
        while (m := self.read(until)).get("id") != self.n:                    # an event, or a late answer to one given up on
            self.seen.add(str(m.get("method")))
        if "error" in m:
            raise ValueError(str(m["error"].get("message") if isinstance(m["error"], dict) else m["error"]))
        return m.get("result") or {}

    def wait(self, event: str, until: float) -> bool:
        with contextlib.suppress(TimeoutError):
            while event not in self.seen:
                self.seen.add(str(self.read(until).get("method")))
        return event in self.seen

    def value(self, expression: str, until: float, session: str) -> dict[str, Any] | None:
        """What a script run in the page gives back, None when it threw or did not finish by `until`."""
        try:
            r = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True}, until, session)
        except TimeoutError:
            return None
        v = r.get("result", {}).get("value")
        return v if isinstance(v, dict) and "exceptionDetails" not in r else None


def tall(m: dict[str, Any], window: int) -> int:
    """A page's own height in CSS px from a measure in a window `window` tall: what it scrolls to when that passes the
    window, else its root's own bottom, so a short page has no band under it."""
    sh, own = int(m.get("sh") or 0), math.ceil(float(m.get("own") or 0))
    return max(1, sh if sh > window else own)


def said_last(log: Path) -> str:
    """The browser's own last word from its log: a sandbox's refusal, else a fatal line, else its last line."""
    with contextlib.suppress(OSError):
        lines = [x.strip() for x in log.read_bytes()[-LOG_READ:].decode(errors="replace").splitlines() if x.strip()]
        return next((x for x in lines if x.startswith("bwrap:")), "") or next((x for x in reversed(lines) if "FATAL" in x), "") or (lines or [""])[-1]
    return ""


def draw(exe: Path, doc: Path, state: Path, width: int, scale: int) -> tuple[bytes, float, bool]:
    """One headless Chromium run on the page in its sandbox (page_box), its build folder (HOME and profile) fresh under
    <state>/render/ and removed after whatever happens, the browser killed with its group at the end of every run: the
    PNG, the seconds it took and whether the page settled in SETTLE s (one that did not is drawn as it stands), else Failed.
    RENDER_TIMEOUT ends the whole run."""
    (root := state / "render").mkdir(mode=0o700, parents=True, exist_ok=True)
    build = Path(tempfile.mkdtemp(dir=root))              # 0700, a random name
    pid, fds = 0, list[int]()
    try:
        if (cmd := page_box(exe, doc, build)) is None:
            raise Failed(words.RENDER_NO_PAGE_BOX)
        argv = cmd + [str(exe), *INSIDE, f"--user-data-dir={build / 'profile'}", "about:blank"]
        fds += (to_r := os.pipe())[0:2]
        fds += (from_r := os.pipe())[0:2]
        far = [fcntl.fcntl(f, fcntl.F_DUPFD_CLOEXEC, 10) for f in (to_r[0], from_r[1])]   # never 3 or 4 already: each dup2 is real
        fds += far + [log := os.open(build / "browser.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)]
        t0 = time.monotonic()
        end = t0 + RENDER_TIMEOUT
        pid = os.posix_spawn(argv[0], argv, {"PATH": os.defpath}, setpgroup=0, file_actions=[
            (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0), (os.POSIX_SPAWN_DUP2, log, 1), (os.POSIX_SPAWN_DUP2, log, 2),
            (os.POSIX_SPAWN_DUP2, far[0], 3), (os.POSIX_SPAWN_DUP2, far[1], 4)])
        for f in (to_r[0], from_r[1], *far, log):          # the browser's ends: closed here, so its exit reads as an end
            os.close(f)
            fds.remove(f)
        talk = Pipe(to_r[1], from_r[0])
        try:
            target = talk.call("Target.createTarget", {"url": "about:blank"}, end)["targetId"]
            s = talk.call("Target.attachToTarget", {"targetId": target, "flatten": True}, end)["sessionId"]
            view = {"width": width, "height": VIEW, "deviceScaleFactor": scale, "mobile": False}
            talk.call("Emulation.setDeviceMetricsOverride", view, end, s)
            talk.call("Page.enable", {}, end, s)
            if err := talk.call("Page.navigate", {"url": doc.as_uri()}, end, s).get("errorText"):
                raise Failed(words.RENDER_PAGE_FAILED.format(name=doc.name, why=words.no_paths(str(err))[:160]))
            settle = min(time.monotonic() + SETTLE, end)
            m = talk.value(SETTLED, settle, s) if talk.wait("Page.loadEventFired", settle) else None
            settled = m is not None                        # not settled at the bound: measured as it stands, else refused
            if m is None and (m := talk.value(MEASURE, min(time.monotonic() + STUCK, end), s)) is None:
                raise Failed(words.RENDER_PAGE_STUCK.format(name=doc.name, seconds=SETTLE))
            h = tall(m, VIEW)
            for _ in range(3):                             # laid out at its own height, what still overflows it is the page's too
                if h * scale > TALLEST:
                    raise Failed(words.RENDER_PAGE_TALL.format(name=doc.name, height=h, device=h * scale, scale=scale, most=TALLEST))
                talk.call("Emulation.setDeviceMetricsOverride", {**view, "height": h}, end, s)
                if (grown := int((talk.value(MEASURE, min(time.monotonic() + STUCK, end), s) or {}).get("sh") or 0)) <= h:
                    break
                h = grown
            png = base64.b64decode(talk.call("Page.captureScreenshot", {"format": "png", "fromSurface": True}, end, s).get("data") or "")
        except TimeoutError:
            raise Failed(words.RENDER_PAGE_TIMED_OUT.format(name=doc.name, seconds=RENDER_TIMEOUT)) from None
        except EOFError:
            said = said_last(build / "browser.log")
            if said.startswith("bwrap:") and "execvp" not in said:
                raise Failed(words.RENDER_NO_PAGE_BOX) from None
            raise Failed(words.RENDER_PAGE_FAILED.format(name=doc.name, why=words.no_paths(said)[-160:] or words.RENDER_NO_WHY)) from None
        except (ValueError, KeyError, TypeError) as e:     # an error the browser answered, or an answer not in its shape
            raise Failed(words.RENDER_PAGE_FAILED.format(name=doc.name, why=words.no_paths(str(e))[:160] or words.RENDER_NO_WHY)) from None
        if png[:8] != PNG or png[12:16] != b"IHDR":
            raise Failed(words.RENDER_PAGE_FAILED.format(name=doc.name, why=words.RENDER_NOT_PNG))
        return png, time.monotonic() - t0, settled
    finally:
        if pid:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)            # bwrap's group; the sandbox's PID namespace dies with it
            os.waitpid(pid, 0)
        for f in fds:
            with contextlib.suppress(OSError):
                os.close(f)
        shutil.rmtree(build, ignore_errors=True)


def picture(doc: Path, state: Path, width: int, scale: int, setting: str = "") -> dict[str, Any]:
    """A page made a picture beside it (draw), one run at a time with every conversion: the PNG, its size in device pixels
    and the seconds it took, NAME.png written only after a clean run, a new file over whatever had that name (a link
    there is replaced, never written through); a width or scale outside its bounds, or no Chromium, said before anything runs."""
    if not WIDTH[0] <= width <= WIDTH[1]:
        return {"ok": False, "error": words.RENDER_PAGE_WIDTH.format(given=width, least=WIDTH[0], most=WIDTH[1], default=WIDTH[2])}
    if not SCALE[0] <= scale <= SCALE[1]:
        return {"ok": False, "error": words.RENDER_PAGE_SCALE.format(given=scale, least=SCALE[0], most=SCALE[1], default=SCALE[2])}
    if (exe := find_chromium(setting)) is None:
        return {"ok": False, "error": words.RENDER_CHROMIUM_NOT_ONE if setting else words.RENDER_NO_CHROMIUM}
    with ONE:
        png, took, settled = draw(exe, doc, state, width, scale)
    fd, tmp = tempfile.mkstemp(dir=doc.parent, prefix=f".{doc.stem}-", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(png)
        os.replace(tmp, out_ := doc.with_name(f"{doc.stem}.png"))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    w, h = struct.unpack(">II", png[16:24])
    r = {"ok": True, "png": str(out_), "width": w, "height": h, "seconds": round(took, 1)}
    return r if settled else {**r, "note": words.RENDER_PAGE_UNSETTLED.format(seconds=SETTLE)}


def run(doc: Path, state: Path, width: int = WIDTH[2], scale: int = SCALE[2], chromium: str = "") -> dict[str, Any]:
    """The tool's whole call, blocking (the daemon runs it off the loop): {"ok": False, "error"} on any stop, else the PDF,
    its pages and the seconds LibreOffice took; a page's picture (picture) with `width`, `scale` and the [render] chromium
    setting, which nothing else reads."""
    kind = doc.suffix.lower()
    try:
        if kind == ".pdf":
            return {"ok": True, "pdf": str(doc), "pages": pages(doc)}
        if kind == ".tex":
            return latex(doc, state)
        if kind in PAGE:
            return picture(doc, state, width, scale, chromium)
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
        if kind in PAGE:
            return {"ok": False, "error": words.RENDER_PAGE_FAILED.format(name=doc.name, why=why)}
        return {"ok": False, "error": words.RENDER_TEX_FAILED.format(engine="latexmk", name=doc.name, why=why) if kind == ".tex" else
                words.RENDER_FAILED.format(name=doc.name, why=why)}
