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
    for d in (".local/bin", ".cache/uv", ".cache/yerk/uv", ".cache/ms-playwright", ".local/share/uv", ".local/lib/py/site-packages", ".claude-acct2",
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
    # its folder, its account's token, its room's work folder, the checkouts with only the parts of .git a worktree add, a
    # commit and a fetch write, uv and the workers' own cache, python's user site-packages, Playwright's browsers, the gate
    home, reach = _home(tmp_path, monkeypatch)
    state, keys, folder, hidden = _walled(home)
    repo, plug = home / "yerk-ai", home / "plug"
    props = workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", reach).sandbox(folder, _env(home, keys))[1::2]
    assert props[:2] == ["PrivateUsers=yes", f"TemporaryFileSystem={home}:ro"]
    binds = [p for p in props if p.startswith("Bind")]
    assert binds == [
        f"BindPaths={folder}", f"BindReadOnlyPaths=-{keys / 'claude-two.token'}",
        f"BindPaths=-{state / 'rooms' / 'r' / 'work'}",
        f"BindReadOnlyPaths={repo}", *(f"BindPaths={repo / '.git' / n}" for n in workers.GIT_RW),
        f"BindReadOnlyPaths={plug}", *(f"BindPaths={plug / '.git' / n}" for n in workers.GIT_RW),
        *(f"BindReadOnlyPaths={home / '.local' / 'bin' / n}" for n in ("uv", "uvx", "trash")), f"BindPaths={home / '.cache' / 'yerk' / 'uv'}",
        f"BindReadOnlyPaths={home / '.local' / 'share' / 'uv'}", f"BindReadOnlyPaths={home / '.local' / 'lib' / 'py' / 'site-packages'}",
        f"BindReadOnlyPaths={home / '.cache' / 'ms-playwright'}", f"BindReadOnlyPaths={home / 'ws' / 'tools' / 'pr-check'}"]
    assert f"ReadOnlyPaths={folder / 'worker.json'}" in props                   # its own gate stays as the daemon wrote it
    assert f"BindPaths={repo / '.git'}" not in props and not any("hooks" in p or "/config" in p for p in props)   # .git/config read-only
    assert (repo / ".git" / "worktrees").is_dir() and (repo / ".git" / "FETCH_HEAD").is_file()   # made as git would, then bound
    for r in (repo, plug):                                          # its list of worktrees is the unit's own, empty and written
        assert f"TemporaryFileSystem={r / '.git' / 'worktrees'}:mode=0700" in props and f"BindPaths={r / '.git' / 'worktrees'}" not in props
        assert props.index(f"BindReadOnlyPaths={r}") < props.index(f"TemporaryFileSystem={r / '.git' / 'worktrees'}:mode=0700")
    named = " ".join(props)
    for gone in (".ssh", ".netrc", ".claude-acct2", ".claude-acct3", str(home / ".cache" / "uv"), "memory", "yerk-http.secret", "pages",
                 str(state / "ledger")):
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
    env = {"CLAUDE_CONFIG_DIR": str(home / ".claude-acct9")}         # an account's folder is never named, there or not
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
    assert workers.DOORS == ((Path("/run"), "empty"), (Path(f"/run/user/{__import__('os').getuid()}"), "own"), (Path("/mnt"), "empty"),
                             (Path("/dev/shm"), "own"), (Path("/var/log"), "gone"), (Path("/dev/vsock"), "gone"), (Path("/dev/fuse"), "gone"),
                             (Path("/dev/dxg"), "gone"))
    home, reach = _home(tmp_path, monkeypatch)
    state, keys, folder, hidden = _walled(home)
    run, own, mnt, log = tmp_path / "run", tmp_path / "run" / "user" / "7", tmp_path / "mnt", tmp_path / "log"
    for p in (own, mnt, log):
        p.mkdir(parents=True)
    monkeypatch.setattr(workers, "DOORS", ((run, "empty"), (own, "own"), (mnt, "empty"), (log, "gone")))
    monkeypatch.setattr(workers, "resolver", lambda: [])
    s = workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", reach)
    props = s.sandbox(folder, _env(home, keys))[1::2]
    last = list(workers.WALLS)
    assert props[-4 - len(last):] == [f"TemporaryFileSystem={run}:ro", f"TemporaryFileSystem={own}:mode=0700", f"TemporaryFileSystem={mnt}:ro",
                                      f"InaccessiblePaths={log}", *last]
    assert not any(p.startswith(("BindPaths=", "BindReadOnlyPaths=")) and str(tmp_path / "run") in p for p in props)   # none comes back
    for p in (mnt, log):
        p.rmdir()
    props = s.sandbox(folder, _env(home, keys))[1::2]
    assert props[-2 - len(last):] == [f"TemporaryFileSystem={run}:ro", f"TemporaryFileSystem={own}:mode=0700", *last]     # a box without them: not named
    monkeypatch.undo()                                                                    # the resolver's file, only when a door would hide it
    real, conf = tmp_path / "run" / "resolve" / "stub.conf", tmp_path / "resolv.conf"
    real.parent.mkdir()
    real.write_text("nameserver 127.0.0.53\n")
    conf.symlink_to(real)
    monkeypatch.setattr(workers, "DOORS", ((run, "empty"),))
    assert workers.resolver(conf) == [real]
    monkeypatch.setattr(workers, "resolver", lambda: [real])
    assert f"BindReadOnlyPaths={real}" in workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", list).sandbox(folder, _env(home, keys))[1::2]
    monkeypatch.undo()
    plain = tmp_path / "plain.conf"
    plain.write_text("nameserver 127.0.0.53\n")
    real.unlink()
    assert workers.resolver(plain) == [] and workers.resolver(conf) == [] and workers.resolver(tmp_path / "none") == []
    opened = workers.Systemd(state, hidden).sandbox(folder, _env(home, keys))[1::2]
    assert not set(last) & set(opened) and not any(str(run) in p for p in opened)       # open: as it was


def _state(state: Path, folder: Path) -> None:
    """A fake state beside the worker's folder: a ledger, a memory index, texts, logs, the web cache, the trash, the daemon's
    secrets, rooms, needs.json (need 7: its builder b11111 and this worker; need 8: c22222), a room's worker d33333."""
    for d in ("docs", "texts", "logs", "web", ".trash", "workers/b11111", "workers/c22222", "workers/d33333", "secrets", "rooms/r/work",
              "rooms/other/work", "accounts/two"):
        (state / d).mkdir(parents=True, exist_ok=True)
    for f in ("ledger.sqlite", "memory.sqlite", "doctor.json", "secrets/yerk-worker.secret", "secrets/yerk-http.secret"):
        (state / f).write_text("x")
    (state / "yerkd.json").write_text(json.dumps({"port": 1, "worker_secret_file": str(state / "secrets" / "yerk-worker.secret")}))
    (state / "workers" / "e55555").mkdir()
    (state / "needs.json").write_text(json.dumps([{"day": "2026-11-20", "n": 7, "workers": ["e55555"]},           # another day's 7
                                                  {"day": "2026-11-24", "n": 7, "workers": ["b11111", folder.name, "..", "a/b"]},
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
    mine = [f"BindPaths={folder}", f"BindReadOnlyPaths={state / 'docs'}"]                       # a need's: never a room's work folder
    room = [mine[0], f"BindPaths=-{state / 'rooms' / 'r' / 'work'}", mine[1]]                    # a room's: its own, as an open unit's
    need = {"room": "r", "need": {"day": "2026-11-24", "n": 7, "role": "read"}}
    assert of_state(need) == [*mine, f"BindReadOnlyPaths={state / 'workers' / 'b11111'}", f"ReadOnlyPaths={folder / 'worker.json'}"]
    assert of_state({**need, "yerk_tools": ["recall"]}) == [*mine, f"BindReadOnlyPaths={state / 'workers' / 'b11111'}",
                                                            f"BindReadOnlyPaths={state / 'yerkd.json'}",
                                                            f"BindReadOnlyPaths={state / 'secrets' / 'yerk-worker.secret'}",
                                                            f"ReadOnlyPaths={folder / 'worker.json'}"]
    assert of_state({"room": "r"}) == [*room, f"ReadOnlyPaths={folder / 'worker.json'}"]          # a room's worker: no kin
    assert of_state({"room": "r", "need": {"day": "2026-11-24", "n": 9}}) == [*mine, f"ReadOnlyPaths={folder / 'worker.json'}"]
    for climbs in ("..", ".", "", "../other", "r/../other", "/"):                                 # a room's name never a path of its own
        assert of_state({"room": climbs}) == [*mine, f"ReadOnlyPaths={folder / 'worker.json'}"], climbs
    inside = {"CLAUDE_CONFIG_DIR": str(state / "accounts" / "two")}                                  # an account kept in the state: not bound
    assert of_state({"room": "r"}, inside) == [*room, f"ReadOnlyPaths={folder / 'worker.json'}"]
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


def test_a_hidden_worker_has_a_config_folder_and_a_uv_cache_of_its_own_never_its_accounts_or_the_owners(tmp_path, monkeypatch):
    # 2026-11-24, the deeper check: the account's config folder held 1,276 transcripts of other rooms, the settings and the
    # wrapper other programs run; the owner's uv cache held 3,382 of the running daemon's own files (uv hard-links). Neither
    # is there for a hidden worker: it writes a config folder in its own folder and a cache only hidden workers use
    home, reach = _home(tmp_path, monkeypatch)
    state, keys, folder, hidden = _walled(home)
    env, s = _env(home, keys), workers.Systemd(state, hidden, workers.MEMORY_MAX, "hidden", reach)
    mine = s.own(folder, env)
    assert mine == {**env, "CLAUDE_CONFIG_DIR": str(folder / workers.OWN_CONFIG), "UV_CACHE_DIR": str(home / ".cache" / "yerk" / "uv")}
    for d in (folder / workers.OWN_CONFIG, home / ".cache" / "yerk" / "uv"):
        assert d.is_dir() and oct(d.stat().st_mode & 0o777) in ("0o700", "0o775", "0o755") and not d.is_symlink()
    assert oct((folder / workers.OWN_CONFIG).stat().st_mode & 0o777) == "0o700" and s.own(folder, env) == mine      # made once, kept
    monkeypatch.setenv("UV_CACHE_DIR", str(home / ".cache" / "uv"))                # whatever the daemon's own says
    assert workers.uv_cache() == home / ".cache" / "yerk" / "uv" and not any(p == home / ".cache" / "uv" for p, _ in reach())
    props = s.sandbox(folder, mine)[1::2]
    named = [p.split("=", 1)[1].lstrip("-") for p in props]
    assert str(home / ".claude-acct2") not in named and str(home / ".cache" / "uv") not in named and str(home / ".cache") not in named
    assert f"BindPaths={home / '.cache' / 'yerk' / 'uv'}" in props and f"BindPaths={folder}" in props
    assert "UV_CACHE_DIR" in workers.ENV_KEEP                                       # claude's shell gets it
    with pytest.raises(OSError, match="no token file"):                               # its only login: without it nothing starts
        s.own(folder, {"CLAUDE_CONFIG_DIR": str(home / ".claude-acct2")})


def test_a_hidden_unit_makes_no_socket_past_the_families_it_needs_and_is_walled_from_the_owners_other_processes():
    # 2026-11-24: WSL's Windows side answered any process on the virtual machine's sockets (AF_VSOCK 50001 is the drives' file
    # server), with the home, the bus and /mnt hidden; the journals gave every other program's log; an attacker run in the
    # unit saw and could signal the owner's processes, wrote its own cgroup and read the kernel's log. The list, held whole:
    # SystemCallArchitectures with the family filter, or the other ABI's socket call goes round it (systemd.exec)
    assert workers.FAMILIES.split() == ["AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"]
    assert workers.NO_CALLS == "~io_uring_setup io_uring_enter io_uring_register"
    assert workers.WALLS == (
        "PrivateTmp=yes", "PrivatePIDs=yes", "ProtectSystem=strict", "ProtectControlGroups=yes",
        "ProtectKernelTunables=yes", "ProtectKernelLogs=yes", "NoNewPrivileges=yes", "RestrictSUIDSGID=yes",
        "LockPersonality=yes", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK", "SystemCallArchitectures=native",
        "SystemCallFilter=~io_uring_setup io_uring_enter io_uring_register", "SystemCallErrorNumber=ENOSYS")


def test_a_workers_memory_server_asks_the_daemon_without_seeing_its_process(tmp_path, monkeypatch):
    # a hidden worker's list of processes is its own (PrivatePIDs): the daemon's pid is not in it, and the alive check would say
    # none runs; the install's own callers keep the check
    from yerk_ai import jobs
    (tmp_path / "s").write_text("a-secret-" + "x" * 30)
    (tmp_path / "yerkd.json").write_text(json.dumps({"pid": 2 ** 30, "port": 1, "secret_file": str(tmp_path / "s"), "worker_secret_file": str(tmp_path / "s")}))
    with pytest.raises(ProcessLookupError):
        jobs.post_control(tmp_path, {"recall": {}}, timeout=2)
    with pytest.raises(OSError) as e:
        jobs.post_control(tmp_path, {"recall": {}}, timeout=2, worker=True)
    assert not isinstance(e.value, ProcessLookupError) and jobs.not_listening(e.value)      # it asked, and nothing listens there


def test_git_runs_outside_only_on_a_worktree_its_repository_knows(tmp_path, monkeypatch):
    # 2026-11-24: the daemon runs git in a worker's worktrees (the report's branch, the tidy at a need's end). A hidden worker's
    # is in no repository's list, and its .git is a file it may write: one that named a repository of its own making would
    # have git out here run what that repository's config names (core.fsmonitor, a clean filter)
    import subprocess
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "a")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "nobody")

    def git(*a: str, cwd: Path) -> None:
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    repo, folder = home / "repo", home / "state" / "workers" / "abc123"
    repo.mkdir(parents=True)
    folder.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "a.txt").write_text("a")
    git("add", "a.txt", cwd=repo)
    git("commit", "-qm", "one", cwd=repo)
    wt = folder / "wt"
    git("worktree", "add", "-q", str(wt), "-b", "b", cwd=repo)
    assert workers.linked(wt) and workers.branch(wt)["branch"] == "b"
    ran, program = tmp_path / "ran", tmp_path / "program"
    program.write_text(f"#!/bin/sh\ntouch {ran}\nexit 1\n")
    program.chmod(0o700)

    def evil(at: Path) -> Path:
        """A repository of a worker's making at `at`, whose config runs a program on any status."""
        git("init", "-q", "-b", "main", *([] if at.name == ".git" else ["--bare"]), str(at.parent if at.name == ".git" else at), cwd=home)
        with open(at / "config", "a") as f:
            f.write(f"[core]\n\tfsmonitor = {program}\n")
        admin = at / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "HEAD").write_text("ref: refs/heads/main\n")
        (admin / "commondir").write_text("../..\n")
        (admin / "gitdir").write_text(f"{wt / '.git'}\n")
        return admin
    from yerk_ai import needs
    good = (wt / ".git").read_text()
    made = [evil(folder / "evil" / ".git"), evil(home / ".cache" / "yerk" / "uv" / "evil" / ".git"), evil(repo / ".git" / "objects" / "evil" / ".git"),
            evil(folder / "bare.git")]
    subprocess.run(["git", "-C", str(wt), f"--git-dir={made[0]}", "status", "--porcelain"], capture_output=True)
    assert ran.exists()                                                             # git by itself does run it
    ran.unlink()
    link = home / "link"
    link.symlink_to(made[0].parent)
    other = repo / ".git" / "worktrees" / "other"                                   # a record of the repository's that names another folder
    shutil.copytree(repo / ".git" / "worktrees" / "wt", other)
    (other / "gitdir").write_text(f"{home / 'elsewhere' / '.git'}\n")
    moved = repo / ".git" / "worktrees" / "moved"                                   # one whose common folder is another's
    shutil.copytree(repo / ".git" / "worktrees" / "wt", moved)
    (moved / "commondir").write_text(f"{made[0].parents[1]}\n")
    aside = repo / ".git" / "logs" / "worktrees" / "wt"                              # a record in a part of .git a hidden worker writes
    aside.mkdir(parents=True)
    for f, text in (("HEAD", "ref: refs/heads/main\n"), ("commondir", "../..\n"), ("gitdir", f"{wt / '.git'}\n")):
        (aside / f).write_text(text)
    way = home / "way"                                                                # the repository's own record, by a link
    way.symlink_to(repo / ".git")
    for admin in (*made, link / "wt", other, moved, aside, way / "worktrees" / "wt", repo / ".git" / "worktrees" / "gone", Path("../evil/.git/worktrees/wt")):
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        assert not workers.linked(wt), admin
        assert "no record of it" in workers.branch(wt)["error"] and needs.tidy(folder) == [] and wt.is_dir(), admin
    (wt / ".git").write_text(f"gitdir: {home / 'nowhere' / '.git' / 'worktrees' / 'wt'}\n")    # nothing there to run: git's own words, as before
    assert workers.branch(wt)["error"].startswith("git could not read it: fatal: not a git repository")
    (wt / ".git").write_text("not a pointer")
    assert not workers.linked(wt) and not workers.linked(folder) and not ran.exists()      # never run, on any of them
    for spelt in (good.rstrip("\n") + " \n", good.rstrip("\n") + "\n\n", " " + good, good.replace("gitdir: ", "gitdir:"), good + "x\n"):
        (wt / ".git").write_text(spelt)                                                        # not one path as git reads it: not taken
        assert not workers.linked(wt) and "no record of it" in workers.branch(wt)["error"], spelt
    for d in (other, moved):
        shutil.rmtree(d)
    (wt / ".git").write_text(good)
    assert workers.linked(wt) and needs.tidy(folder) == [] and not wt.exists()              # the repository's own: tidied as before


def test_a_state_folder_and_a_secrets_folder_outside_the_home_are_walled_like_the_home(tmp_path, monkeypatch):
    # an install with its state folder or its private folder elsewhere: the state folder is hidden whole and only the same few
    # parts come back; a hidden path outside both is hidden as an open unit has it, with the worker's own token alone in it
    home, reach = _home(tmp_path, monkeypatch)
    state, keys, folder = tmp_path / "st", tmp_path / "priv" / "secrets", tmp_path / "st" / "workers" / "abc123"
    for d in (folder, keys, state / "docs", state / "rooms" / "r" / "work"):
        d.mkdir(parents=True)
    for f in ("claude-two.token", "other.key"):
        (keys / f).write_text("x")
    (folder / "worker.json").write_text(json.dumps({"room": "r", "need": {"day": "2026-11-24", "n": 1}}))
    hidden = (state / "secrets" / "yerk-http.secret", state / "rooms", keys, state / "secrets" / "pages")
    s = workers.Systemd(state, hidden, workers.MEMORY_MAX, "needs", reach)
    props = s.sandbox(folder, _env(home, keys))[1::2]
    assert props[:3] == ["PrivateUsers=yes", f"TemporaryFileSystem={home}:ro", f"TemporaryFileSystem={state}:ro"]
    assert f"BindPaths={folder}" in props and f"BindReadOnlyPaths={state / 'docs'}" in props
    i = props.index(f"TemporaryFileSystem={keys}:ro")
    assert props[i + 1] == f"BindReadOnlyPaths=-{keys / 'claude-two.token'}" and f"InaccessiblePaths=-{keys}" not in props
    assert not any(str(state / "rooms") in p or str(state / "secrets") in p for p in props)        # hidden with the state folder
    far = s.sandbox(folder, {"YERK_TOKEN_FILE": str(home / "elsewhere.token")})[1::2]              # its token not in there: nothing back
    assert f"InaccessiblePaths=-{keys}" in far and not any(p.startswith(("Bind", "Temp")) and str(keys) in p for p in far)
    asked = s.unseen("r", "", str(keys / "claude-two.token"), [str(keys / "other.key"), str(keys / "claude-two.token"), str(state / "ledger.sqlite"),
                                                                 str(folder / "x")], {"need": {"day": "2026-11-24", "n": 1}}, folder)
    assert asked == [str(keys / "other.key"), str(state / "ledger.sqlite")]


def test_the_daemon_gives_its_launcher_the_setting_and_everything_a_job_reaches(tmp_path, monkeypatch):
    # the wall is off when the launcher is never told: yerkd.toml's word has to arrive, with the checkout, each plug-in's
    # repository, the toolchains, the trash and the gate
    from yerk_ai import needs
    from yerk_ai.config import EXAMPLE_PRIVATE, REPO
    from yerk_ai.daemon import Yerkd
    home, _ = _home(tmp_path, monkeypatch)
    private = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    conf = (private / "yerkd.toml").read_text() if (private / "yerkd.toml").is_file() else ""
    assert "[workers]" not in conf and "[needs]" not in conf
    (private / "yerkd.toml").write_text(conf + f'\n[workers]\nhome = "needs"\nmemory_max = "3G"\n\n[needs]\npr_check = "{home / "ws" / "tools" / "pr-check"}"\n')
    (tmp_path / "state").mkdir()
    y = Yerkd(private, tmp_path / "state")
    unit = y.workers.launcher
    assert unit.home == "needs" and unit.memory_max == "3G" and unit.reach == y.worker_reach
    assert unit.hides({"need": {"day": "2026-11-24", "n": 1}}) and not unit.hides({"room": "example-room"})
    monkeypatch.setattr(needs, "plugins", lambda confs, own: {"p": {"repo": str(home / "plug"), "base": "main"}} if own == REPO else {})
    y.gates.trash = ("trash",) if isinstance(y.gates.trash, tuple) else ["trash"]
    got = y.worker_reach()
    for want in ((REPO, "repo"), (home / "plug", "repo"), (home / ".local" / "bin" / "uv", "ro"), (home / ".local" / "bin" / "trash", "ro"),
                 (home / ".cache" / "yerk" / "uv", "rw"), (home / "ws" / "tools" / "pr-check", "ro")):
        assert want in got, want
    assert [how for _, how in got].count("rw") == 1                                  # one written path in the reach: the workers' uv cache
