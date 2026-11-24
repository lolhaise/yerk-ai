"""Checks the [workers] home setting: how much of your home folder a worker sees (workers.Systemd.sandbox). From the walls audit of 2026-11-09
(G9): a worker's unit read the whole home (an SSH key, a netrc, every account's login, the owner's workspaces) beside the web
pages it reads. "open" is today's unit to the letter; "hidden" an empty read-only home with only what a worker's job reaches
bound back; "needs" hidden for a need's builder, cold read and fix only."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from yerk_ai import workers


def _walled(home: Path) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    """A worker's folder in a state folder under `home`, the daemon's four hidden paths and its account's token."""
    state, keys = home / ".local" / "state" / "yerk", home / "yerk-private" / "secrets"
    folder = state / "workers" / "abc123"
    folder.mkdir(parents=True)
    (folder / "worker.json").write_text(json.dumps({"room": "r"}))
    keys.mkdir(parents=True)
    (keys / "claude-two.token").write_text("{}")
    return state, keys, folder, (state / "secrets" / "yerk-http.secret", state / "rooms", keys, state / "secrets" / "pages")


def _home(tmp_path, monkeypatch):
    """A home with what the box has: Yerk's checkout and a plug-in's, uv and the trash on PATH, uv's cache and data, python's
    user site-packages, Playwright's browsers, the gate, two accounts' folders, a key and a netrc."""
    home = tmp_path / "home"
    for d in (".local/bin", ".cache/uv", ".cache/ms-playwright", ".local/share/uv", ".local/lib/py/site-packages", ".claude-acct2",
              ".claude-acct3", ".ssh", "yerk-ai/.git/objects", "yerk-ai/.git/refs", "yerk-ai/.git/logs", "plug/.git/objects",
              "plug/.git/refs", "plug/.git/logs", "plug/.git/worktrees", "ws/tools", "ws/memory"):
        (home / d).mkdir(parents=True)
    for f in (".local/bin/uv", ".local/bin/uvx", ".local/bin/trash", "ws/tools/pr-check", ".ssh/id_ed25519", ".netrc"):
        (home / f).write_text("x")
        (home / f).chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{home / '.local' / 'bin'}:/usr/bin:/bin")
    for k in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "UV_CACHE_DIR", "PLAYWRIGHT_BROWSERS_PATH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(workers.site, "getusersitepackages", lambda: str(home / ".local" / "lib" / "py" / "site-packages"))
    monkeypatch.setattr(workers.site, "getuserbase", lambda: str(home / ".local"))

    def reach() -> list[tuple[Path, str]]:
        return workers.reach([home / "yerk-ai", home / "plug"], str(home / "ws" / "tools" / "pr-check"), ["trash"])
    return home, reach


def _env(home: Path, keys: Path) -> dict[str, str]:
    return {"CLAUDE_CONFIG_DIR": str(home / ".claude-acct2"), "YERK_TOKEN_FILE": str(keys / "claude-two.token")}


def test_open_is_todays_unit_to_the_letter_and_never_asks_what_a_job_reaches(tmp_path, monkeypatch):
    # D1: "open", the default, is the property list every unit had before the setting, the whole list held here, and it
    # never asks the daemon's reach, so nothing new runs at a launch
    home, _ = _home(tmp_path, monkeypatch)
    state, keys, folder, hidden = _walled(home)
    asked: list[int] = []
    inside = {"CLAUDE_CONFIG_DIR": str(state / "accounts" / "two")}
    today = {"outside": ["PrivateUsers=yes", f"ReadOnlyPaths={state}", f"ReadWritePaths={folder}",
                         f"ReadOnlyPaths={folder / 'worker.json'}", f"InaccessiblePaths={hidden[0]}",
                         f"TemporaryFileSystem={state / 'rooms'}:ro", f"BindPaths=-{state / 'rooms' / 'r' / 'work'}",
                         f"TemporaryFileSystem={keys}:ro", f"BindReadOnlyPaths=-{keys / 'claude-two.token'}", f"InaccessiblePaths={hidden[3]}"],
             "inside": ["PrivateUsers=yes", f"ReadOnlyPaths={state}", f"ReadWritePaths={folder}", f"ReadWritePaths={state / 'accounts' / 'two'}",
                        f"ReadOnlyPaths={folder / 'worker.json'}", f"InaccessiblePaths={hidden[0]}",
                        f"TemporaryFileSystem={state / 'rooms'}:ro", f"BindPaths=-{state / 'rooms' / 'r' / 'work'}",
                        f"InaccessiblePaths=-{keys}", f"InaccessiblePaths={hidden[3]}"]}
    for s in (workers.Systemd(state, hidden), workers.Systemd(state, hidden, workers.MEMORY_MAX, "open", lambda: asked.append(1) or [(home, "rw")])):
        for name, env in (("outside", _env(home, keys)), ("inside", inside)):
            assert s.sandbox(folder, env)[1::2] == today[name]
    (folder / "worker.json").write_text(json.dumps({"room": "r", "need": {"day": "2026-11-24", "n": 1}}))
    assert workers.Systemd(state, hidden, workers.MEMORY_MAX, "open", lambda: asked.append(1) or []).sandbox(folder, inside)[1::2] == today["inside"]
    assert asked == []


def test_the_home_setting_reads_and_a_misspelt_one_stops_yerkd(tmp_path):
    # D1: open, needs or hidden; any other value stops yerkd at its start with one line, since a wall misspelt is no wall
    assert workers.settings(tmp_path, {}).home == "open"
    assert [workers.settings(tmp_path, {"home": h}).home for h in workers.HOMES] == ["open", "needs", "hidden"]
    for bad in ("hiden", "Hidden", "", "true"):
        with pytest.raises(SystemExit, match=r"^yerkd.toml: \[workers\] home must be one of open, needs, hidden, not "):
            workers.settings(tmp_path, {"home": bad})
    from yerk_ai.config import EXAMPLE_PRIVATE, REPO, YERKD_WORDS
    from yerk_ai.daemon import Yerkd
    assert "home" in YERKD_WORDS["workers"]
    (tmp_path / "d").mkdir()
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path / "d")                       # the example install: open, and the daemon's reach wired in
    assert y.workers.launcher.home == "open" and y.workers.launcher.reach == y.worker_reach
    assert y.worker_reach()[0] == (REPO, "repo")


def test_a_hidden_home_binds_back_only_what_a_workers_job_reaches(tmp_path, monkeypatch):
    # D2: the home an empty read-only filesystem; each bind back is one a need worker's own step uses (workers.reach's counts):
    # its folder, its account and token, its room's work folder, the checkouts with only the parts of .git a worktree add, a
    # commit and a fetch write, uv and its cache, python's user site-packages, Playwright's browsers, the gate
    home, reach = _home(tmp_path, monkeypatch)
    state, keys, folder, hidden = _walled(home)
    repo, plug = home / "yerk-ai", home / "plug"
    props = workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", reach).sandbox(folder, _env(home, keys))[1::2]
    assert props[:2] == ["PrivateUsers=yes", f"TemporaryFileSystem={home}:ro"]
    binds = [p for p in props if p.startswith("Bind")]
    assert binds == [
        f"BindPaths={folder}", f"BindPaths={home / '.claude-acct2'}", f"BindReadOnlyPaths=-{keys / 'claude-two.token'}",
        f"BindPaths=-{state / 'rooms' / 'r' / 'work'}",
        f"BindReadOnlyPaths={repo}", *(f"BindPaths={repo / '.git' / n}" for n in workers.GIT_RW),
        f"BindReadOnlyPaths={plug}", *(f"BindPaths={plug / '.git' / n}" for n in workers.GIT_RW),
        *(f"BindReadOnlyPaths={home / '.local' / 'bin' / n}" for n in ("uv", "uvx", "trash")), f"BindPaths={home / '.cache' / 'uv'}",
        f"BindReadOnlyPaths={home / '.local' / 'share' / 'uv'}", f"BindReadOnlyPaths={home / '.local' / 'lib' / 'py' / 'site-packages'}",
        f"BindReadOnlyPaths={home / '.cache' / 'ms-playwright'}", f"BindReadOnlyPaths={home / 'ws' / 'tools' / 'pr-check'}"]
    assert f"ReadOnlyPaths={folder / 'worker.json'}" in props                   # its own gate stays as the daemon wrote it
    assert f"BindPaths={repo / '.git'}" not in props and not any("hooks" in p or "/config" in p for p in props)   # .git/config read-only
    assert (repo / ".git" / "worktrees").is_dir() and (repo / ".git" / "FETCH_HEAD").is_file()   # made as git would, then bound
    named = " ".join(props)
    for gone in (".ssh", ".netrc", ".claude-acct3", "memory", "yerk-http.secret", "pages", str(state / "ledger")):
        assert gone not in named                                    # never named, so the empty home leaves it out


def test_a_hidden_home_binds_each_pythons_user_site_packages_since_the_trash_runs_on_the_systems(tmp_path, monkeypatch):
    # 2026-11-24, P3: the daemon on one python, the trash-cli a delete becomes on the system's python3.14 with its user
    # site-packages; binding only the daemon's left the trash without its module
    home, reach = _home(tmp_path, monkeypatch)
    (home / ".local" / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
    got = [p for p, how in reach() if "site-packages" in str(p)]
    assert got == [home / ".local" / "lib" / "py" / "site-packages", home / ".local" / "lib" / "python3.14" / "site-packages"]
    shutil.rmtree(home / ".local" / "lib" / "py")
    state, keys, folder, hidden = _walled(home)
    props = workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", reach).sandbox(folder, _env(home, keys))[1::2]
    assert [p for p in props if "site-packages" in p] == [f"BindReadOnlyPaths={home / '.local' / 'lib' / 'python3.14' / 'site-packages'}"]


def test_a_hidden_home_names_no_path_that_is_not_there_and_none_outside_it(tmp_path, monkeypatch):
    # D8: the home is Path.home(), and each optional path is named only when it is there
    home, reach = _home(tmp_path, monkeypatch)
    state, keys, folder, hidden = _walled(home)
    for p in (home / ".cache" / "ms-playwright", home / ".local" / "bin" / "uvx", home / "plug" / ".git"):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    far = tmp_path / "srv" / "repo"                                  # a checkout outside the home: nothing hides it, nothing binds it
    (far / ".git" / "objects").mkdir(parents=True)
    env = {"CLAUDE_CONFIG_DIR": str(home / ".claude-acct9")}         # an account folder that is not there: not named either
    s = workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", lambda: [*reach(), (far, "repo"), (home / "nowhere", "ro")])
    props = s.sandbox(folder, env)[1::2]
    named = " ".join(props)
    for missing in ("ms-playwright", "uvx", "plug/.git", ".claude-acct9", "nowhere", str(far)):
        assert missing not in named
    assert f"BindReadOnlyPaths={home / 'plug'}" in props               # the plug-in's tree is still there, read-only
    assert not any("claude-two.token" in p for p in props)             # no token in its env: none bound back
    assert workers.Systemd(None, hidden, workers.MEMORY_MAX, "hidden", reach).sandbox(folder, env) == []   # no state folder: none


def test_a_hidden_home_shuts_the_other_doors_each_only_when_it_is_there(tmp_path, monkeypatch):
    # D3, measured 2026-11-24 in throwaway units with the home hidden: the user bus started a unit outside that saw ~/.ssh,
    # rootless Docker and the SSH agent answered in the owner's runtime folder, a Windows program in the worker's folder ran
    # as the owner's Windows user, /mnt/wslg/distro was the whole disk again, and tailscaled answered for the tailnet. Each is
    # shut here, the runtime folder the unit's own (the uid is the process's), each only when it is there
    assert workers.DOORS[0] == (Path(f"/run/user/{__import__('os').getuid()}"), "own")
    home, reach = _home(tmp_path, monkeypatch)
    state, keys, folder, hidden = _walled(home)
    run, wsl, ts, mnt = tmp_path / "run" / "user" / "7", tmp_path / "run" / "WSL", tmp_path / "run" / "tailscale", tmp_path / "mnt"
    for p in (run, wsl, ts, mnt):
        p.mkdir(parents=True)
    monkeypatch.setattr(workers, "DOORS", ((run, "own"), (wsl, "gone"), (ts, "gone"), (mnt, "empty")))
    s = workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", reach)
    props = s.sandbox(folder, _env(home, keys))[1::2]
    assert props[-5:] == [f"TemporaryFileSystem={run}:mode=0700", f"InaccessiblePaths={wsl}", f"InaccessiblePaths={ts}",
                          f"TemporaryFileSystem={mnt}:ro", "PrivateTmp=yes"]
    assert not any(p.startswith(("BindPaths=", "BindReadOnlyPaths=")) and str(tmp_path / "run") in p for p in props)   # none comes back
    for p in (wsl, ts, mnt):
        p.rmdir()
    props = s.sandbox(folder, _env(home, keys))[1::2]
    assert props[-2:] == [f"TemporaryFileSystem={run}:mode=0700", "PrivateTmp=yes"]   # a box without them: not named
    opened = workers.Systemd(state, hidden).sandbox(folder, _env(home, keys))[1::2]
    assert "PrivateTmp=yes" not in opened and not any(str(run) in p for p in opened)    # open: as it was


def _state(state: Path, folder: Path) -> None:
    """A fake state beside the worker's folder: a ledger, a memory index, texts, logs, the web cache, the trash, the daemon's
    secrets, rooms, needs.json (need 7: its builder b11111 and this worker; need 8: c22222), a room's worker d33333."""
    for d in ("docs", "texts", "logs", "web", ".trash", "workers/b11111", "workers/c22222", "workers/d33333", "secrets", "rooms/r/work",
              "rooms/other/work", "accounts/two"):
        (state / d).mkdir(parents=True, exist_ok=True)
    for f in ("ledger.sqlite", "memory.sqlite", "doctor.json", "secrets/yerk-worker.secret", "secrets/yerk-http.secret"):
        (state / f).write_text("x")
    (state / "yerkd.json").write_text(json.dumps({"port": 1, "worker_secret_file": str(state / "secrets" / "yerk-worker.secret")}))
    (state / "needs.json").write_text(json.dumps([{"day": "2026-11-24", "n": 7, "workers": ["b11111", folder.name, "..", "a/b"]},
                                                  {"day": "2026-11-24", "n": 8, "workers": ["c22222"]}]))


def test_a_hidden_home_gives_back_of_the_state_folder_only_docs_the_same_needs_workers_and_its_servers_two_files(tmp_path, monkeypatch):
    # D4: the state folder is not bound whole. Its own folder, its room's work folder and its account (when it sits there),
    # `docs` read-only, the same need's other workers read-only (the record's ids, never a scan), the worker secret only with
    # the tools; never the ledger, the memory, the texts, the logs, the web cache, the trash, another need's or a room's worker
    home, reach = _home(tmp_path, monkeypatch)
    state, keys, folder, hidden = _walled(home)
    _state(state, folder)
    s, env = workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", reach), _env(home, keys)

    def of_state(spec: dict, env: dict[str, str] = env) -> list[str]:
        (folder / "worker.json").write_text(json.dumps(spec))
        return [p for p in s.sandbox(folder, env)[1::2] if str(state) in p]
    mine = [f"BindPaths={folder}", f"BindPaths=-{state / 'rooms' / 'r' / 'work'}", f"BindReadOnlyPaths={state / 'docs'}"]
    need = {"room": "r", "need": {"day": "2026-11-24", "n": 7, "role": "read"}}
    assert of_state(need) == [*mine, f"BindReadOnlyPaths={state / 'workers' / 'b11111'}", f"ReadOnlyPaths={folder / 'worker.json'}"]
    assert of_state({**need, "yerk_tools": ["recall"]}) == [*mine, f"BindReadOnlyPaths={state / 'workers' / 'b11111'}",
                                                            f"BindReadOnlyPaths={state / 'yerkd.json'}",
                                                            f"BindReadOnlyPaths={state / 'secrets' / 'yerk-worker.secret'}",
                                                            f"ReadOnlyPaths={folder / 'worker.json'}"]
    assert of_state({"room": "r"}) == [*mine, f"ReadOnlyPaths={folder / 'worker.json'}"]          # a room's worker: no kin
    assert of_state({"room": "r", "need": {"day": "2026-11-24", "n": 9}}) == [*mine, f"ReadOnlyPaths={folder / 'worker.json'}"]
    inside = {"CLAUDE_CONFIG_DIR": str(state / "accounts" / "two")}                                  # an account kept in the state
    assert of_state({"room": "r"}, inside) == [mine[0], f"BindPaths={state / 'accounts' / 'two'}", *mine[1:], f"ReadOnlyPaths={folder / 'worker.json'}"]
    props = s.sandbox(folder, env)[1::2]
    named = " ".join(props)
    for gone in ("ledger.sqlite", "memory.sqlite", "texts", "logs", "web", ".trash", "workers/c22222", "workers/d33333", "needs.json",
                 "doctor.json", "secrets", "rooms/other"):
        assert str(state / gone) not in named
    assert not any(p.split("=", 1)[1].lstrip("-") == str(state) for p in props)                  # never the state folder whole
    (state / "needs.json").write_text("not json")                                                 # a record that cannot be read: no kin
    assert of_state(need) == [*mine, f"ReadOnlyPaths={folder / 'worker.json'}"]


def test_needs_hides_the_home_from_a_needs_workers_and_leaves_a_room_started_one_open(tmp_path, monkeypatch):
    # D1, P6: "needs" is the trial on the needs pipeline; a need's builder, cold read and fix carry the need in their spec
    # (Workers.start), and every other worker keeps today's unit to the letter
    home, reach = _home(tmp_path, monkeypatch)
    state, keys, folder, hidden = _walled(home)
    env = _env(home, keys)
    opened = workers.Systemd(state, hidden).sandbox(folder, env)
    needs = workers.Systemd(state, hidden, workers.MEMORY_MAX, "needs", reach)
    assert needs.sandbox(folder, env) == opened                       # a room's worker: as it was
    for role in ({}, {"role": "read"}, {"role": "fix"}):
        (folder / "worker.json").write_text(json.dumps({"room": "r", "need": {"day": "2026-11-24", "n": 1, **role}}))
        hid = workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", reach).sandbox(folder, env)
        assert needs.sandbox(folder, env) == hid and f"TemporaryFileSystem={home}:ro" in hid
