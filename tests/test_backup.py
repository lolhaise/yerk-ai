"""Checks `yerk backup` and `yerk restore` (backup.py), which keep in one dated archive what one bad disk would take, and put it back. The cases, on a copy
of the example layer (examples/private, the fake private folder the tests use) with example values only: the archive restores into an empty folder byte for
byte with every mode kept, a secret's 0600 too; an archive with one byte changed or cut short is refused before anything is written, and without the manifest
check the changed one would restore (the mutation); the last N stay; the layer is restored over only with --force, after a backup of it, what it replaces
moved to the trash; never under a running yerkd unless --force; the card and the doctor say the newest's age. Example values only is decision 22."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest

from yerk_ai import backup, cli, doctor, status, words
from yerk_ai.config import EXAMPLE_PRIVATE

SECRET = b"example value, not a key\n"


@pytest.fixture
def layer(tmp_path, monkeypatch):
    """A copy of the example layer with a secret and an attachments folder, a state with a ledger in WAL mode, a session
    binding and the index's files, and a units folder; yerkd.toml's [backup] names the folder and the units."""
    monkeypatch.setattr(backup, "UNITS", tmp_path / "no-units")      # never the box's own units
    priv, state, units = tmp_path / "private", tmp_path / "state", tmp_path / "units"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    (priv / "secrets").mkdir(mode=0o700)
    (priv / "secrets" / "example.key").write_bytes(SECRET)
    (priv / "secrets" / "example.key").chmod(0o600)
    (priv / "example-room-attachments").mkdir()
    (priv / "example-room-attachments" / "note.txt").write_text("an example attachment\n")
    (priv / "example-room-attachments" / "note.txt").chmod(0o660)     # a group-write bit: tarfile's own filter drops it, keep_mode keeps it
    (priv / "yerkd.toml").write_text(f'[backup]\npath = "{tmp_path / "backups"}"\nunits = "{units}"\nkeep = 2\n')
    (state / "memory").mkdir(parents=True)
    db = sqlite3.connect(state / "ledger.sqlite")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)")
    db.executemany("INSERT INTO messages (text) VALUES (?)", [(f"example message {i}",) for i in range(50)])
    db.commit()                                                       # left open: its rows sit in the -wal, as under yerkd
    (state / "sessions.json").write_text(json.dumps({"example-room": {"session_id": "s1"}}))
    (state / "memory" / "index.json").write_text(json.dumps({"port": 1, "at": 1.0}))
    (state / "memory" / "index.sqlite").write_bytes(b"")             # the vector store: out unless --with-index
    units.mkdir()
    (units / "yerkd.service").write_text("[Service]\nExecStart=yerkd\n")
    (units / "other.service").write_text("[Service]\nExecStart=not-ours\n")
    for p in (priv, state):
        p.chmod(0o700)
    yield priv, state, units
    db.close()


def tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    return {str(p.relative_to(root)): (p.stat().st_mode & 0o7777, p.read_bytes() if p.is_file() else None) for p in root.rglob("*")}


def one(priv: Path) -> Path:
    arch, = (priv.parent / "backups").glob("yerk-*.tar")
    return arch


def test_a_backup_restores_into_an_empty_folder_byte_for_byte_with_its_modes_and_a_secrets_0600(layer, tmp_path, capsys):
    priv, state, units = layer
    assert cli.main(["backup", "--private", str(priv), "--state", str(state)]) == 0
    out, arch = capsys.readouterr().out, one(priv)
    assert out.startswith(f"backup: {arch}, ") and out.count("\n") == 1
    assert arch.stat().st_mode & 0o777 == 0o600 and arch.parent.stat().st_mode & 0o777 == 0o700
    with tarfile.open(arch) as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile(backup.MANIFEST).read())
    assert names[-1] == backup.MANIFEST and "units/yerkd.service" in names and "units/other.service" not in names
    assert "state/memory/index.json" in names and "state/memory/index.sqlite" not in names and "state/ledger.sqlite" in names
    assert len(manifest["files"]) == len(names) - 1 and all(len(r["sha256"]) == 64 for r in manifest["files"] if "bytes" in r)
    assert cli.main(["restore", str(arch), "--to", str(tmp_path / "back"), "--private", str(priv), "--state", str(state)]) == 0
    steps = capsys.readouterr().out.splitlines()
    assert steps[0].startswith(f"verified: {arch.name}, ") and [x.split(":")[0] for x in steps[1:]] == [
        "restore private", "restore state", "restore units"]
    assert tree(tmp_path / "back" / "private") == tree(priv)              # every byte and every mode
    assert (tmp_path / "back" / "private" / "secrets" / "example.key").stat().st_mode & 0o777 == 0o600
    rows = sqlite3.connect(tmp_path / "back" / "state" / "ledger.sqlite").execute("SELECT count(*) FROM messages").fetchone()
    assert rows == (50,) and (tmp_path / "back" / "units" / "yerkd.service").read_text() == (units / "yerkd.service").read_text()
    assert cli.main(["backup", "--with-index", "--to", str(tmp_path / "ix"), "--private", str(priv), "--state", str(state)]) == 0
    with tarfile.open(next((tmp_path / "ix").glob("yerk-*.tar"))) as tar:
        assert "state/memory/index.sqlite" in tar.getnames()


def corrupt(arch: Path, dest: Path) -> Path:
    """A copy of `arch` with one byte changed inside the secret's bytes: the tar itself still reads."""
    data = bytearray(arch.read_bytes())
    at = data.index(SECRET)
    data[at] ^= 1
    dest.write_bytes(bytes(data))
    return dest


def refused(arch: Path, priv: Path, state: Path, to: Path, capsys) -> bool:
    code = cli.main(["restore", str(arch), "--to", str(to), "--private", str(priv), "--state", str(state)])
    err = capsys.readouterr().err
    return code == 1 and err.startswith("refused: ") and not to.exists()


def test_an_archive_changed_in_one_byte_or_cut_short_is_refused_and_nothing_is_written(layer, tmp_path, capsys):
    priv, state, _ = layer
    assert backup.main(priv, state) == 0
    arch = one(priv)
    assert refused(corrupt(arch, tmp_path / "bad.tar"), priv, state, tmp_path / "to1", capsys)
    data = arch.read_bytes()
    (tmp_path / "half.tar").write_bytes(data[:len(data) // 2])
    (tmp_path / "head.tar").write_bytes(data[:data.index(b"MANIFEST.json")])       # cut before the manifest: no manifest
    for cut in ("half.tar", "head.tar"):
        assert refused(tmp_path / cut, priv, state, tmp_path / "to2", capsys)
    assert cli.main(["restore", str(arch), "--to", str(tmp_path / "to3"), "--dry-run", "--private", str(priv), "--state", str(state)]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("would restore units: 1 files") and not (tmp_path / "to3").exists()


def test_without_the_manifest_check_the_changed_archive_would_restore(layer, tmp_path, capsys, monkeypatch):
    """The mutation: the check skipped, the same archive goes in with its changed byte. The refusal above is that check."""
    priv, state, _ = layer
    assert backup.main(priv, state) == 0
    monkeypatch.setattr(backup, "differs", lambda listed, seen: [])
    assert not refused(corrupt(one(priv), tmp_path / "bad.tar"), priv, state, tmp_path / "to", capsys)
    assert (tmp_path / "to" / "private" / "secrets" / "example.key").read_bytes() != SECRET


def test_the_last_n_stay_and_a_run_the_same_second_takes_the_next(layer, tmp_path):
    priv, state, units = layer
    folder, keep, _ = backup.settings(priv, state)
    for t in (1.0e9, 1.0e9, 1.0e9 + 3600):
        backup.make(priv, state, folder, units, now=t)
    assert sorted(p.name for p in folder.iterdir())[:2] == ["yerk-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + ".tar"
                                                             for t in (1.0e9, 1.0e9 + 1)]
    assert len(backup.prune(folder, keep)) == 1 and len(list(folder.iterdir())) == keep == 2


def test_the_layer_is_restored_over_only_with_force_after_its_own_backup_and_never_under_yerkd_unless_forced(layer, tmp_path, capsys):
    priv, state, units = layer
    assert backup.main(priv, state) == 0
    arch, was = one(priv), tree(priv)
    (priv / "persona.md").write_text("changed after the backup\n")
    args = ["restore", str(arch), "--private", str(priv), "--state", str(state)]
    assert cli.main(args) == 1 and "holds files" in capsys.readouterr().err
    held = os.open(state / "yerkd.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)                     # as daemon.claim holds it
    try:
        assert backup.running(state) and cli.main(["restore", str(arch), "--to", str(tmp_path / "empty"), "--private", str(priv),
                                                   "--state", str(state)]) == 0
        assert cli.main(args + ["--force"]) == 0
    finally:
        os.close(held)
    out = capsys.readouterr().out
    assert "yerkd runs on" in out and "back up first: " in out and tree(priv) == was
    aside, = (state / "trash").glob("*/restore-*")
    assert (aside / "private" / "persona.md").read_text() == "changed after the backup\n" and (aside / "state" / "ledger.sqlite").exists()
    assert len(list((tmp_path / "backups").glob("yerk-*.tar"))) == 2      # the archive and the backup taken before the restore
    assert sqlite3.connect(state / "ledger.sqlite").execute("SELECT count(*) FROM messages").fetchone() == (50,)


def test_a_layer_whose_state_is_gone_restores_with_force_and_a_new_disk_without_its_units_written_over(layer, tmp_path, capsys,
                                                                                                        monkeypatch):
    """What a dead disk leaves: the state gone under a layer still there (--force), then both gone with the units the README
    copies still in place (no --force needed, nothing holds files: each unit moved aside, never written over)."""
    priv, state, units = layer
    assert backup.main(priv, state) == 0
    arch, was = one(priv), tree(priv)
    args = ["restore", str(arch), "--private", str(priv), "--state", str(state)]
    shutil.rmtree(state)
    assert cli.main(args + ["--force"]) == 0 and tree(priv) == was
    aside, = (state / "trash").glob("*/restore-*")
    assert (aside / "private" / "persona.md").is_file() and (aside / "units" / "yerkd.service").is_file()
    shutil.rmtree(priv)
    shutil.rmtree(state)
    monkeypatch.setattr(backup, "UNITS", units)                          # no yerkd.toml yet: the units' default folder
    (units / "yerkd.service").write_text("[Service]\nExecStart=a newer yerkd\n")
    capsys.readouterr()
    assert cli.main(args) == 0 and tree(priv) == was and "back up first" not in capsys.readouterr().out
    aside, = (state / "trash").glob("*/restore-*")
    assert (aside / "units" / "yerkd.service").read_text() == "[Service]\nExecStart=a newer yerkd\n"
    assert (units / "yerkd.service").read_text() == "[Service]\nExecStart=yerkd\n"
    assert sqlite3.connect(state / "ledger.sqlite").execute("SELECT count(*) FROM messages").fetchone() == (50,)


def test_the_card_and_the_doctor_say_the_newest_backups_age(layer, tmp_path):
    priv, state, _ = layer
    conf = (priv / "yerkd.toml").read_text()
    assert backup.newest(priv, state) is None
    assert [x for x in doctor.run(priv, state, dry=True) if x.startswith(f"{words.NOT_DONE} backup: ")] == [f"{words.NOT_DONE} backup: none in backups; `yerk backup` makes "
                                                                                         "one, yerk-backup.timer each night"]
    assert backup.main(priv, state) == 0
    b = backup.newest(priv, state)
    assert b is not None and b["bytes"] == one(priv).stat().st_size
    s = {"at": b["at"] + 7200, "running": False, "rooms": [], "units": [], "accounts": [], "workers": [], "jobs": [], "stalls": [],
         "backup": b}
    assert f"{words.PLAIN} backup: 2 h 0 min ago, {words.size(int(b['bytes']))}" in status.card(s)
    assert f"{words.NOT_DONE} {words.NO_BACKUP}" in status.card({**s, "backup": None})
    assert not [x for x in doctor.run(priv, state, dry=True) if x.startswith(f"{words.NOT_DONE} backup: ")]
    os.utime(one(priv), (b["at"] - 37 * 3600, b["at"] - 37 * 3600))
    assert [x for x in doctor.run(priv, state, dry=True) if x.startswith(f"{words.NOT_DONE} backup: ")] == [
        f"{words.NOT_DONE} backup: the newest, {one(priv).name}, is 37 h old, past 36 h; is yerk-backup.timer on?"]
    assert status.document(priv, state, s, _ledger(tmp_path))["backup_age"] >= 37 * 3600
    (priv / "yerkd.toml").write_text(conf.replace("[backup]\n", "[other]\n"))       # no [backup] table, no archive: not a finding
    one(priv).unlink()
    assert not [x for x in doctor.run(priv, state, dry=True) if x.startswith(f"{words.NOT_DONE} backup: ")]


def _ledger(tmp_path: Path):
    from yerk_ai.ledger import Ledger
    return Ledger(tmp_path / "doc.sqlite")
