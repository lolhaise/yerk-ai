"""Checks that a workbook or a PDF sent to a room whose privacy level is owner gets its text written beside it as it is
staged, by code with no model call (NAME.sheet.txt, NAME.pdf.txt), its arrival line naming it. A scan is named and not
read, a long one cut at budgets.toml [files] with a last line saying how much; the index takes it as an inbox text kind,
recall and read_memory show its lines behind memory.FILE_MARK; `yerk files backfill` writes the missing and stale ones; a
never read. Every value here is a placeholder (decision 22)."""
from __future__ import annotations

import io
import os
import time
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook  # type: ignore[import-untyped]
from pypdf import PdfWriter
from test_discord import OWNER, ROOM_CH, Attachment, Client, Msg, Room, run

from yerk_ai import cli, files, memindex, memory, words
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.ledger import Ledger

TZ = ZoneInfo("UTC")


def sheet_bytes() -> bytes:
    """Two sheets: a header row, a row whose Total is a formula saved with its value (2500), an empty row, a cell with a tab
    in it and a date; then a second sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Budget"
    for row in (["Item", "Qty", "Price", "Total"], ["exquux widget", 2, 1250, "=B2*C2"], [], ["see\tnote", None, None, datetime(2026, 1, 5)]):
        ws.append(row)
    wb.create_sheet("Second").append(["example vendor", "EX-42"])
    raw, out = io.BytesIO(), io.BytesIO()
    wb.save(raw)
    with zipfile.ZipFile(raw) as zin, zipfile.ZipFile(out, "w") as z:     # Excel saves a formula's value; openpyxl does not
        for item in zin.infolist():
            data = zin.read(item.filename)
            z.writestr(item, data.replace(b"<f>B2*C2</f><v />", b"<f>B2*C2</f><v>2500</v>") if item.filename.endswith("sheet1.xml") else data)
    return out.getvalue()


def pdf_bytes(pages: list[str]) -> bytes:
    """A PDF with a text layer: each page's lines in Helvetica, an empty string a page with none."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))}] /Count {len(pages)} >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        ops = ("BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({x}) Tj T*" for x in text.splitlines()) + " ET") if text else ""
        objs += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>",
                 f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream"]
    out, at = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        at.append(len(out))
        out += f"{n} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{x:010d} 00000 n \n".encode() for x in at)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def scan_bytes(pages: int = 3) -> bytes:
    """A scan as far as text goes: pages with no text layer."""
    w, out = PdfWriter(), io.BytesIO()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    w.write(out)
    return out.getvalue()


REPORT = ["Quarterly example report\nRevenue was flat", "The exquux clause renews in March\nSigned by Pat", ""]


def sent(state: Path, room: str, mid: str, name: str, data: bytes, at: float = 1_760_000_000.0) -> Path:
    (d := state / "rooms" / room / "work" / "inbox" / mid).mkdir(parents=True, exist_ok=True)
    (p := d / name).write_bytes(data)
    os.utime(p, (at, at))
    return p.resolve()


class Box(Room):
    """The Discord tests' room, staging where a real room does (<state>/rooms/<room>/work/inbox) with its rooms.toml privacy."""

    def __init__(self, tmp: Path, name: str, privacy: str = "owner") -> None:
        super().__init__(tmp)
        self.name, self.conf = name, {"privacy": privacy}

    def inbox(self, mid: str) -> Path:
        (d := self.tmp / "rooms" / self.name / "work" / "inbox" / mid).mkdir(parents=True, exist_ok=True)
        return d


def staged(tmp_path: Path, privacy: str, attachments: list[Attachment]) -> Box:
    d = DiscordChannel("token", OWNER, [], Ledger(tmp_path / "l.sqlite"), client=(client := Client()), state=tmp_path)
    d.add_room(ROOM_CH, box := Box(tmp_path, "own-room" if privacy == "owner" else "her-room", privacy))
    run(d.on_message(Msg(client.chans[ROOM_CH], 5, "the numbers", attachments=attachments)))
    return box


def test_a_workbook_and_a_pdf_sent_to_an_owner_room_get_their_text_beside_them_as_they_are_staged(tmp_path):
    box = staged(tmp_path, "owner", [Attachment("budget.xlsx", sheet_bytes()), Attachment("report.pdf", pdf_bytes(REPORT)),
                                     Attachment("photo.png", b"\x89PNG" + b"0" * 20, "image/png")])
    [got] = box.got
    inbox = tmp_path / "rooms" / "own-room" / "work" / "inbox" / "5"
    sheet, page = inbox / "budget.sheet.txt", inbox / "report.pdf.txt"
    assert sheet.is_file() and page.is_file() and sorted(p.name for p in inbox.iterdir()) == [
        "budget.sheet.txt", "budget.xlsx", "photo.png", "report.pdf", "report.pdf.txt"]     # a picture is the room's to read
    assert oct(sheet.stat().st_mode & 0o777) == "0o600"
    assert sheet.read_text().splitlines()[0].startswith("extracted in own-room, message 5, ")
    assert sheet.read_text().splitlines()[0].endswith(": budget.xlsx, 2 sheets")
    assert words.FILE_TEXT.format(path=sheet) in got.text and words.FILE_TEXT.format(path=page) in got.text   # the room is told
    assert [f.get("text") for f in got.files] == [str(sheet), str(page), None]


def test_a_file_slower_than_the_wait_goes_in_without_its_text_and_its_text_lands_when_it_is_done(tmp_path, monkeypatch):
    real = files.write

    def slow(*a, **k):
        time.sleep(0.4)
        return real(*a, **k)
    monkeypatch.setattr(files, "write", slow)
    monkeypatch.setattr(files, "WAIT", 0.05)
    box = staged(tmp_path, "owner", [Attachment("budget.xlsx", sheet_bytes())])
    [got] = box.got
    assert "[its text" not in got.text and "text" not in got.files[0] and got.files[0]["ok"]       # the message did not wait
    side = tmp_path / "rooms" / "own-room" / "work" / "inbox" / "5" / "budget.sheet.txt"
    assert side.read_text().splitlines()[3] == "exquux widget\t2\t1250\t2500"     # asyncio.run let the thread finish


def test_a_persons_room_never_reads_a_workbook_or_a_pdf_sent_to_it(tmp_path, capsys):
    box = staged(tmp_path, "family", [Attachment("budget.xlsx", sheet_bytes()), Attachment("report.pdf", pdf_bytes(REPORT))])
    [got] = box.got
    inbox = tmp_path / "rooms" / "her-room" / "work" / "inbox" / "5"
    assert sorted(p.name for p in inbox.iterdir()) == ["budget.xlsx", "report.pdf"] and "[its text" not in got.text
    (priv := tmp_path / "private").mkdir()
    (priv / "rooms.toml").write_text('[rooms.her-room]\nchannel = "console:her"\nprivacy = "family"\npeople = [1001]\n')
    assert cli.main(["files", "backfill", "her-room", "--private", str(priv), "--state", str(tmp_path)]) == 2
    assert capsys.readouterr().err.strip() == words.FILES_PERSON.format(room="her-room")
    assert sorted(p.name for p in inbox.iterdir()) == ["budget.xlsx", "report.pdf"]


def test_a_workbook_is_a_section_a_sheet_a_row_a_line_cells_tab_separated_a_formula_as_its_saved_value(tmp_path):
    side = files.write("own-room", sent(tmp_path, "own-room", "m-1", "budget.xlsx", sheet_bytes()), TZ)
    assert side.name == "budget.sheet.txt" and side.read_text().splitlines() == [
        "extracted in own-room, message m-1, 2025-10-09 08:53 UTC: budget.xlsx, 2 sheets",
        "## Budget", "Item\tQty\tPrice\tTotal", "exquux widget\t2\t1250\t2500", "see note\t\t\t2026-01-05",
        "## Second", "example vendor\tEX-42"]
    xlsm = files.write("own-room", sent(tmp_path, "own-room", "m-2", "Plan.XLSM", sheet_bytes()), TZ)
    assert xlsm.name == "Plan.sheet.txt" and xlsm.read_text().splitlines()[3] == "exquux widget\t2\t1250\t2500"


def test_a_pdf_is_a_section_a_page_a_scan_is_named_and_not_read_and_a_file_that_does_not_open_says_so(tmp_path):
    side = files.write("own-room", sent(tmp_path, "own-room", "m-1", "report.pdf", pdf_bytes(REPORT)), TZ)
    assert side.read_text().splitlines() == [
        "extracted in own-room, message m-1, 2025-10-09 08:53 UTC: report.pdf, 3 pages",
        "## page 1", "Quarterly example report", "Revenue was flat",
        "## page 2", "The exquux clause renews in March", "Signed by Pat", "## page 3", files.NO_TEXT]
    scan = files.write("own-room", sent(tmp_path, "own-room", "m-2", "scan.pdf", scan_bytes()), TZ)
    assert scan.read_text() == "extracted in own-room, message m-2, 2025-10-09 08:53 UTC: scan.pdf, 3 pages, no text layer, not read\n"
    bad = files.write("own-room", sent(tmp_path, "own-room", "m-3", "old.xlsx", b"not a workbook"), TZ)
    assert bad.read_text().splitlines() == ["extracted in own-room, message m-3, 2025-10-09 08:53 UTC: old.xlsx, "
                                            "not read: it did not open as a workbook"]


def test_past_the_cap_in_budgets_toml_the_text_is_cut_and_its_last_line_says_how_much(tmp_path, monkeypatch):
    from yerk_ai.config import budgets
    assert budgets()["files"] == {"chars": 200_000, "secs": 60}
    monkeypatch.setattr(files, "budgets", lambda: {"files": {"chars": 60, "secs": 60}})
    lines = files.write("own-room", sent(tmp_path, "own-room", "m-1", "report.pdf", pdf_bytes(REPORT)), TZ).read_text().splitlines()
    whole = ["## page 1", "Quarterly example report", "Revenue was flat", "## page 2", "The exquux clause renews in March",
             "Signed by Pat", "## page 3", files.NO_TEXT]
    kept = "\n".join(lines[1:-1]) + "\n"                                    # what is kept, with its line ends, fills the cap
    assert len(kept) == 60 and lines[1:4] == whole[:3] and lines[-2] == "## page"
    assert lines[-1] == f"[cut at 60 characters: {len(chr(10).join(whole)) + 1 - (len(kept) - 1)} more not kept]"   # the rest, to the character
    assert files.fit(["abcdef", "ghij", "klmnop"], 9) == ["abcdef", "g", "[cut at 9 characters: 11 more not kept]"]
    assert files.fit(["abc", "def"], 99) == ["abc", "def"]


def test_a_file_still_being_read_past_its_seconds_stops_there_and_its_last_line_says_so():
    """Measured 2026-10-23 on the owner rooms' real PDFs: 0.5 to 0.8 s a page, a 131-page one minutes long. The read stops
    at budgets.toml [files] secs; past the cap the count of what was not kept becomes "at least"."""
    def slow(*lines: str):
        for x in lines:
            if x == "...":
                time.sleep(0.15)
                continue
            yield x
    assert files.fit(slow("## page 1", "a line", "...", "## page 2", "more"), 1000, 0.05) == [
        "## page 1", "a line", files.SLOW.format(secs=0.05)]
    assert files.SLOW.format(secs=60) == "[stopped after 60 s of reading: the rest not read]"
    assert files.fit(slow("abcdef", "ghij", "...", "klm"), 9, 0.05) == ["abcdef", "g", "[cut at 9 characters: at least 4 more not kept]"]
    assert files.fit(slow("abcdef", "ghij", "klm"), 9, 0.05) == ["abcdef", "g", "[cut at 9 characters: 8 more not kept]"]


def test_backfill_writes_only_a_missing_or_stale_text_and_a_dry_run_writes_nothing(tmp_path, capsys):
    (priv := tmp_path / "private").mkdir()
    (priv / "rooms.toml").write_text('[rooms.own-room]\nchannel = "console:own"\n')
    state = tmp_path / "state"
    missing = sent(state, "own-room", "m-1", "budget.xlsx", sheet_bytes(), at=1_760_000_000.0)
    stale = sent(state, "own-room", "m-2", "report.pdf", pdf_bytes(REPORT), at=1_760_000_900.0)
    fresh = sent(state, "own-room", "m-3", "notes.pdf", pdf_bytes(["kept as it is"]), at=1_760_000_500.0)
    sent(state, "own-room", "m-4", "table.csv", b"a,b\n")                              # text already: indexed as it is
    for side, at in ((files.sidecar(stale), 1_760_000_800.0), (files.sidecar(fresh), 1_760_000_600.0)):
        side.write_text("an earlier text\n")
        os.utime(side, (at, at))
    base = ["--private", str(priv), "--state", str(state)]
    assert cli.main(["files", "backfill", "own-room", "--dry-run", *base]) == 0
    assert capsys.readouterr().out.splitlines() == [str(files.sidecar(missing)), str(files.sidecar(stale)),
                                                    words.FILES_BACKFILL.format(n=2, where="own-room", done=words.FILES_DRY)]
    assert not files.sidecar(missing).exists() and files.sidecar(stale).read_text() == "an earlier text\n"
    assert cli.main(["files", "backfill", "own-room", *base]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == words.FILES_BACKFILL.format(n=2, where="own-room", done=words.FILES_WRITTEN)
    assert files.sidecar(missing).read_text().splitlines()[3] == "exquux widget\t2\t1250\t2500"
    assert files.sidecar(stale).read_text().splitlines()[1:3] == ["## page 1", "Quarterly example report"]
    assert files.sidecar(fresh).read_text() == "an earlier text\n" and not (missing.parent.parent / "m-4" / "table.sheet.txt").exists()
    assert cli.main(["files", "backfill", "own-room", *base]) == 0
    assert capsys.readouterr().out.splitlines() == [words.FILES_BACKFILL.format(n=0, where="own-room", done=words.FILES_WRITTEN)]


def test_recall_finds_a_cell_and_a_phrase_on_page_two_shown_behind_the_file_mark_never_as_anyones_words(tmp_path):
    (priv := tmp_path / "private").mkdir()
    (priv / "yerkd.toml").write_text('[memory]\nroot = "memory"\n')
    (priv / "rooms.toml").write_text('[rooms.own-room]\nchannel = "console:own"\n')
    state = tmp_path / "state"
    s = memory.settings(priv, state)
    sheet = files.write("own-room", sent(state, "own-room", "m-1", "budget.xlsx", sheet_bytes()), s.tz)
    page = files.write("own-room", sent(state, "own-room", "m-2", "report.pdf", pdf_bytes(REPORT)), s.tz)
    memindex.Index(s.index_db, s.scope).sync()
    assert str(sheet) in s.scope.files() and memory.file_class(sheet) == ("private", "")
    cell = next(h for h in memory.recall(s, "exquux widget", 6)["hits"] if h["cite"].startswith("own-room/work/inbox/m-1/budget.sheet.txt"))
    assert "exquux widget\t2\t1250\t2500" in cell["text"] and all(
        x.startswith(memory.FILE_MARK) for x in cell["text"].splitlines() if x.strip() and x != "…")
    deep = memory.lines_on(s, "renews March")
    assert [x["cite"] for x in deep] == ["own-room/work/inbox/m-2/report.pdf.txt:6"]
    assert deep[0]["text"] == f"{memory.FILE_MARK} The exquux clause renews in March"
    assert page.read_text().splitlines()[4] == "## page 2"                              # line 6 is page 2's first line
    text = memory.read(s, "own-room/work/inbox/m-1/budget.sheet.txt")["text"].splitlines()
    assert len(text) == 7 and all(x.startswith(f"{memory.FILE_MARK} ") for x in text)
    assert memory.mark_of(sheet) == memory.FILE_MARK and memory.mark_of(page) == memory.FILE_MARK
    assert not memory.is_seen(sheet) and memory.mark_of(tmp_path / "work" / "x.sheet.txt") == ""
    long = f"{memory.FILE_MARK} " + "example figures, " * 30 + "exquux at the end"
    assert memory._cut(long, {"exquux": 1.0}, 200).startswith(f"{memory.FILE_MARK} …")


def test_backfill_over_a_folder_the_room_lists_writes_beside_each_file_and_the_index_takes_it_there(tmp_path, tmp_path_factory, capsys):
    root = tmp_path_factory.mktemp("statements")
    (root / "2026").mkdir()
    (pdf := root / "2026" / "march.pdf").write_bytes(pdf_bytes(["Opening balance exquux 1,250"]))
    (priv := tmp_path / "private").mkdir()
    state = tmp_path / "state"
    (priv / "rooms.toml").write_text(f'[rooms.own-room]\nchannel = "console:own"\nalso = ["{root}"]\n')
    (priv / "yerkd.toml").write_text(f'[memory]\nroot = "memory"\nalso = ["{root}"]\n')
    base = ["--private", str(priv), "--state", str(state)]
    assert cli.main(["files", "backfill", "own-room", "--root", str(tmp_path_factory.mktemp("unlisted")), *base]) == 2
    capsys.readouterr()
    assert cli.main(["files", "backfill", "own-room", "--root", str(root), *base]) == 0
    side = root / "2026" / "march.pdf.txt"
    assert capsys.readouterr().out.splitlines() == [str(side.resolve()), words.FILES_BACKFILL.format(n=1, where=root.resolve(), done=words.FILES_WRITTEN)]
    assert side.read_text().splitlines()[0].startswith(f"extracted in own-room, folder {root.resolve() / '2026'}, ")
    s = memory.settings(priv, state)
    memindex.Index(s.index_db, s.scope).sync()
    assert str(side.resolve()) in s.scope.files() and memory.mark_of(side) == memory.FILE_MARK
    (root / "plain.pdf.txt").write_text("a note that is no sidecar\n")
    assert memory.mark_of(root / "plain.pdf.txt") == "" and pdf.read_bytes().startswith(b"%PDF")
