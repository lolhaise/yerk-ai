"""Makes a spreadsheet or a PDF sent to one of your own rooms searchable, the way its pictures and recordings are. As it is
staged (channels/discord.stage) its text is written beside it by code, no model call: NAME.sheet.txt for an .xlsx or .xlsm,
a `## <sheet>` section a sheet, a row a line, its cells tab-separated, empty rows skipped, a formula as the value saved with
it; NAME.pdf.txt for a PDF, a `## page N` section a page. A PDF with no text on any page (a scan) gets its header alone,
naming its pages and NO_LAYER, never OCR; a file that does not open, its header and NOT_OPEN. Past budgets.toml [files]
`chars` the text is cut and its last line says how many characters were not kept. The index takes it as an inbox text kind
(a .txt) in its room's class; recall and read_memory show its lines behind memory.FILE_MARK, never as anyone's words. A
person room's files are never read here.
`yerk files backfill ROOM` writes each one missing, or older than its file, in the room's inbox, in that process; with
`--root FOLDER`, under a folder the room lists in rooms.toml (`read` or `also`), each beside its file.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

from . import pictures
from .config import budgets, write_private
from .memindex import EXTRACTED, FILE_HEAD
from .memory import system_zone

log = logging.getLogger(__name__)
SHEETS = (".xlsx", ".xlsm")          # a workbook openpyxl reads; a .csv is text already (memindex.UPLOADS)
WAIT = 30.0                          # the most a message waits on its file's text; a slower one is written when it ends
NO_TEXT = "(no text on this page)"
NO_LAYER = "no text layer, not read"
NOT_OPEN = "not read: it did not open as a {kind}"
CUT = "[cut at {cap:,} characters: {left} more not kept]"
SLOW = "[stopped after {secs:g} s of reading: the rest not read]"


def kind(path: Path) -> str:
    """What it is, a workbook or a PDF, as its name says; empty for any other file."""
    s = path.suffix.lower()
    return "workbook" if s in SHEETS else "PDF" if s == ".pdf" else ""


def sidecar(path: Path) -> Path:
    return path.with_name(path.stem + EXTRACTED[kind(path) == "PDF"])


def stale(path: Path) -> bool:
    """A workbook or a PDF with no text beside it, or one older than the file (sent again under its name)."""
    side = sidecar(path)
    return bool(kind(path)) and (not side.is_file() or side.stat().st_mtime < path.stat().st_mtime)


def pending(inbox: Path, root: bool = False) -> list[Path]:
    """The workbooks and PDFs whose text is missing or stale, oldest first, found as pictures.pending finds pictures."""
    return pictures.pending(inbox, root, stale)


def cell(v: Any) -> str:
    """A cell as text: empty as nothing, a date at midnight as its day, a tab or a line break in it as a space."""
    if v is None:
        return ""
    if isinstance(v, datetime) and v.time() == datetime.min.time():
        return v.date().isoformat()
    return re.sub(r"[\t\r\n]+", " ", str(v))


def workbook(path: Path) -> tuple[str, Iterator[str]]:
    """What it holds and its lines: `## <sheet>`, then a line for each row with a value, its cells tab-separated."""
    from openpyxl import load_workbook  # type: ignore[import-untyped]    # it ships no type stubs
    wb = load_workbook(path, read_only=True, data_only=True)    # data_only: a formula's saved value, never the formula

    def lines() -> Iterator[str]:
        try:
            for ws in wb.worksheets:
                yield f"## {ws.title}"
                ws.reset_dimensions()          # a sheet that says it spans A1:XFD1048576 is read as the cells it has
                for row in ws.iter_rows(values_only=True):
                    cells = [cell(v) for v in row]
                    while cells and not cells[-1]:
                        cells.pop()
                    if cells:
                        yield "\t".join(cells)
        finally:
            wb.close()
    n = len(wb.worksheets)
    return f"{n} sheet{'s' * (n != 1)}", lines()


def pdf(path: Path) -> tuple[str, Iterator[str]]:
    """What it holds and its lines: `## page N`, then the page's lines of text, NO_TEXT for a page with none."""
    from pypdf import PdfReader
    r = PdfReader(path)
    if r.is_encrypted and not r.decrypt(""):     # one locked only against editing opens with no password
        raise ValueError("locked with a password")

    def lines() -> Iterator[str]:
        for i, page in enumerate(r.pages, 1):
            yield f"## page {i}"
            try:
                text = page.extract_text() or ""
            except Exception as e:               # one page that does not read leaves the others
                log.warning("files: %s, page %d not read: %s", path.name, i, type(e).__name__)
                text = ""
            yield from [x.rstrip() for x in text.splitlines() if x.strip()] or [NO_TEXT]
    n = len(r.pages)
    return f"{n} page{'s' * (n != 1)}", lines()


def fit(lines: Iterable[str], cap: int, secs: float = 0) -> list[str]:
    """The lines in at most `cap` characters, the one that crosses it cut there, then CUT with how many were not kept; a file
    still being read after `secs` s (budgets.toml [files]) stops there, its last line SLOW, or CUT's "at least" once cut."""
    kept: list[str] = []
    used = left = 0
    end = time.monotonic() + secs if secs else float("inf")
    for x in lines:
        if time.monotonic() > end:
            return kept + [CUT.format(cap=cap, left=f"at least {left:,}") if left else SLOW.format(secs=secs)]
        room = cap - used - 1 if not left else -1
        if len(x) <= room:
            kept.append(x)
            used += len(x) + 1
            continue
        if room > 0:
            kept.append(x[:room])
        left += len(x) + 1 - max(room, 0)
    return kept + [CUT.format(cap=cap, left=f"{left:,}")] if left else kept


def write(room: str, path: Path, tz: tzinfo, cap: int = 0) -> Path:
    """The text beside a workbook or a PDF: its header (pictures.header from FILE_HEAD, then what it holds), then its lines
    cut at `cap` and `secs` (budgets.toml [files]); a PDF with no text on any page, or a file that does not open, its header alone."""
    sort, conf = kind(path), budgets()["files"]
    try:
        what, lines = (workbook if sort == "workbook" else pdf)(path)
        body = fit(lines, cap or int(conf["chars"]), float(conf["secs"]))
    except ImportError:                          # openpyxl or pypdf not installed (`uv sync`): the file is fine, so nothing is
        raise                                    # written and it stays missing, for the backfill once they are
    except Exception as e:                       # whatever a damaged or locked file raises: said in its header, never a stop
        log.warning("files: %s not read: %s", path.name, type(e).__name__)
        what, body = NOT_OPEN.format(kind=sort), []
    if sort == "PDF" and body and all(x.startswith("## page ") or x == NO_TEXT for x in body):
        what, body = f"{what}, {NO_LAYER}", []
    return write_private(sidecar(path), "\n".join([f"{pictures.header(room, path, tz, FILE_HEAD)}, {what}", *body]) + "\n")


async def beside(room: str, path: Path, tz: tzinfo | None = None) -> Path | None:
    """On upload: the text written beside it off the event loop, waited on at most WAIT s; a slower file's is written when
    it ends and its message goes in without it. None when it is not there yet or could not be written."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(write, room, path, tz or ZoneInfo(system_zone())), WAIT)
    except Exception as e:                       # a slow file, a full disk: the message goes in all the same
        log.warning("files: %s, its text not written yet: %s", path.name, type(e).__name__)
        return None
