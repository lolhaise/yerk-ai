"""Checks yerk-index, the program that keeps the memory's search index, between its passes: it sleeps on a folder watch
and a pass costs one stat per file and no stat per path for the mention check."""
import ctypes
import errno
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from yerk_ai import memindex, memory, mentions
from yerk_ai.config import INDEX_HEADER, REPO


def tree(tmp_path: Path) -> memory.Settings:
    private = tmp_path / "private"
    (private / "memory" / "daily").mkdir(parents=True)
    (private / "memory" / "daily" / "2026-01-05.md").write_text("# 2026-01-05\n\n- 09:00 a person: tea at nine\n")
    (private / "memory" / "pinned.md").write_text("# Pinned\n\nA person prefers tea in the morning.\n")
    (private / "yerkd.toml").write_text('[memory]\ntimezone = "UTC"\n')
    return memory.settings(private, tmp_path / "state")


def synced(s: memory.Settings) -> str | None:
    try:
        conn = memindex.connect(s.index_db, readonly=True)
    except FileNotFoundError:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'synced'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return str(row[0]) if row else None


def searchable(s: memory.Settings, word: str, limit: float = 10.0, found: bool = True) -> float:
    """Seconds until the index answers `word` (or, found=False, stops answering it); limit + 1 when it never does."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        conn = memindex.connect(s.index_db, readonly=True)
        try:
            if bool(memindex.search(conn, word)) == found:
                return time.monotonic() - t0
        finally:
            conn.close()
        time.sleep(0.02)
    return limit + 1


def test_an_idle_index_makes_no_pass_and_a_change_is_searchable_inside_one_interval(tmp_path):
    s = tree(tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in ("YERK_PRIVATE", "YERK_STATE")}
    p = subprocess.Popen([sys.executable, "-m", "yerk_ai.memindex", "--private", str(s.root.parent), "--state",
                          str(tmp_path / "state"), "--interval", "1"], cwd=REPO, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # nothing to do: the time of the last pass holds still for twice --interval
        end = time.monotonic() + 30
        last, since = synced(s), time.monotonic()
        while time.monotonic() - since < 2.5:
            assert time.monotonic() < end, "yerk-index keeps walking the tree with nothing to do"
            time.sleep(0.1)
            now = synced(s)
            if now is None or now != last:
                last, since = now, time.monotonic()
        # a line appended to a day log wakes it: searchable well inside one --interval
        with (s.root / "daily" / "2026-01-05.md").open("a") as f:
            f.write("- 09:02 a person: the zebrafinch nests by the window\n")
        assert searchable(s, "zebrafinch") < 0.8
        # a file in a folder made after the start: the folder's event, then one more walk of it
        (s.root / "topics").mkdir()
        (s.root / "topics" / "birds.md").write_text("# Birds\n\nThe quetzal is green.\n")
        assert searchable(s, "quetzal") < 3.0
        (s.root / "pinned.md").unlink()
        assert searchable(s, "morning", found=False) < 3.0      # a deleted file leaves the index too
    finally:
        p.send_signal(signal.SIGTERM)
        t0 = time.monotonic()
        rc = p.wait(timeout=15)
    assert rc == 0 and time.monotonic() - t0 < 3                # the watch never holds up a stop


def test_a_running_index_names_a_token_of_its_own_in_index_json_and_answers_no_search_without_it(tmp_path):
    # the real program: each run makes its token, names it beside its port (0600) and refuses whoever does not send it
    s, seen = tree(tmp_path), []
    env = {k: v for k, v in os.environ.items() if k not in ("YERK_PRIVATE", "YERK_STATE")}
    for _ in range(2):
        s.index_status.unlink(missing_ok=True)
        p = subprocess.Popen([sys.executable, "-m", "yerk_ai.memindex", "--private", str(s.root.parent), "--state", str(tmp_path / "state"),
                              "--interval", "1"], cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            end, st = time.monotonic() + 30, {}
            while not st.get("port") and time.monotonic() < end:
                time.sleep(0.05)
                st = memory.index_status(s)
            assert len(st["token"]) >= 24 and oct(s.index_status.stat().st_mode & 0o777) == "0o600"

            def ask(headers: dict) -> int:
                req = urllib.request.Request(f"http://127.0.0.1:{st['port']}/search", data=b'{"query": "tea", "pool": 5}', headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code
            assert ask({}) == 401 and ask({INDEX_HEADER: "x" * len(st["token"])}) == 401 and ask({INDEX_HEADER: st["token"]}) == 200
            seen.append(st["token"])
        finally:
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=15)
    assert seen[0] != seen[1]                                   # a run's own


def test_the_watch_wakes_for_markdown_folders_and_a_root_file_by_name_only(tmp_path):
    w = memindex.Wake(("notes.txt",))

    def drain() -> None:
        while w.wait(0.1):
            pass
    try:
        assert w.watch(str(tmp_path)) and not w.watch(str(tmp_path))      # new once, then already watched
        assert not w.watch(str(tmp_path / "gone"))                         # gone since the walk: skipped
        assert not w.wait(0.05)
        (tmp_path / "log.jsonl").write_text("{}\n")
        assert not w.wait(0.3)                                             # not Markdown: no pass
        (tmp_path / "a.md").write_text("x\n")
        assert w.wait(1.0)
        drain()
        (tmp_path / "notes.txt").write_text("x\n")
        assert w.wait(1.0)
        drain()
        (tmp_path / "sub").mkdir()
        assert w.wait(1.0)
    finally:
        os.close(w.fd)


def test_a_root_folder_renamed_away_and_made_again_wakes_the_watch_before_the_backstop(tmp_path):
    root = tmp_path / "also"
    root.mkdir()
    w = memindex.Wake()
    try:
        assert w.watch(str(root))
        root.rename(tmp_path / "also.old")          # the watch goes with the old folder: only its MOVE_SELF tells
        root.mkdir()
        (root / "n.md").write_text("x\n")           # the new folder has no watch until the next pass
        assert w.wait(1.0)                          # that pass now, not after BACKSTOP
    finally:
        os.close(w.fd)


def test_no_inotify_says_so_once_and_the_wait_is_a_plain_sleep(tmp_path, monkeypatch, caplog):
    class NoInotify:                                # inotify_init1 fails, as at the per-user instance limit
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def inotify_init1(self, flags: int) -> int:
            ctypes.set_errno(errno.EMFILE)
            return -1
    monkeypatch.setattr(ctypes, "CDLL", NoInotify)
    with caplog.at_level(logging.WARNING, logger="yerk.index"):
        w = memindex.Wake()
    assert w.fd < 0 and caplog.text.count("no inotify (Too many open files): a pass every interval") == 1
    assert not w.watch(str(tmp_path))
    t0 = time.monotonic()
    assert not w.wait(0.1) and time.monotonic() - t0 >= 0.09     # slept, then the loop walks every interval


def test_the_mention_check_cites_only_its_sources(tmp_path, monkeypatch):
    cited: list[Path] = []
    real = mentions.short_cite
    monkeypatch.setattr(mentions, "short_cite", lambda path, roots: cited.append(path) or real(path, roots))
    b = mentions.Builder(tmp_path / "mentions.json", (tmp_path,), ("MEMORY.md",), ("facts/old-*",))
    disk = [tmp_path / "daily" / f"2026-01-{i:02d}.md" for i in range(1, 29)]
    disk += [tmp_path / "facts" / "kettle.md", tmp_path / "facts" / "old-kettle.md", tmp_path / "MEMORY.md"]
    assert [p.name for p in disk if b.is_source(p)] == ["kettle.md", "MEMORY.md"]
    assert len(cited) == 3                                    # the 28 day logs are never cited (a stat each)


def test_the_walk_names_the_folders_it_read_which_are_the_ones_the_watch_covers(tmp_path):
    root = tmp_path / "mem"
    (root / "a" / ".hidden").mkdir(parents=True)
    (root / "a" / "n.md").write_text("x\n")
    (root / "a" / ".hidden" / "h.md").write_text("hidden\n")
    (tmp_path / "MEMORY.md").write_text("# Pinned\n")
    folders: list[str] = []
    files = memindex.Scope((root, tmp_path / "MEMORY.md")).files(folders=folders)
    assert sorted(files) == sorted([str(root / "a" / "n.md"), str(tmp_path / "MEMORY.md")])
    assert sorted(folders) == sorted([str(root), str(root / "a"), str(tmp_path)])   # hidden folders are not read
