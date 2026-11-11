"""Checks the walls: the approval, restart and worker rules read a shell command as what would really run, whatever its shape.

G1: the approval and restart rules read a shell command the way the delete rules do, as what would really run (unwrap):
G1: the approval and restart rules read a shell command the way the delete rules do, as what would really run (unwrap): a held command stays held behind
a wrapper, in a nested shell, after a shell keyword, behind xargs or by its full path, and the control set still passes untouched. The live case: worker
need-2026-10-28-24` past the worker gate; the missing GitHub login was what stopped it.
"""
from pathlib import Path

import pytest

from yerk_ai import gates

D = gates.Settings(trash=("trash",))
PUSH_SHAPES = [                                   # one held action, `git push origin main`, in the shapes a shell runs it
    "git push origin main",
    "/usr/bin/git push origin main",
    "timeout 60 git push origin main",
    "nice -n 5 git push origin main",
    'bash -c "git push origin main"',
    "sh -c 'cd repo && git push origin main'",
    'eval "git push origin main"',
    "if true; then git push origin main; fi",
    "! git push origin main",
    "echo main | xargs git push origin",
    "echo $(git push origin main)",
    "cd wt && timeout 60 git push -u origin need-2026-10-28-24 2>&1 | tail -5",     # worker 00c67e's own line
]


@pytest.mark.parametrize("command", PUSH_SHAPES)
def test_a_push_is_held_in_every_shape_a_shell_runs_it(command):
    hit = gates.external_hit("Bash", {"command": command}, "r", D)
    assert hit is not None and hit[0] == "public-post", command


@pytest.mark.parametrize("command,rule", [
    ("/usr/bin/gh pr create -t x -b y", "public-post"),
    ("timeout 30 gh pr merge 12 --squash", "public-post"),
    ("sh -c 'gh auth login'", "login"),
    ("env -u X docker login registry.example", "login"),
    ("timeout 5 stripe charges create --amount 100", "money-cli"),
    ("/usr/local/bin/npm publish", "public-post"),
])
def test_the_other_shell_rules_read_what_really_runs(command, rule):
    hit = gates.external_hit("Bash", {"command": command}, "r", D)
    assert hit is not None and hit[0] == rule, command


@pytest.mark.parametrize("command", [             # a word of a held command in text, never run: no hold (decision 18)
    'echo "git push origin main"', "grep -rn 'git push' docs", 'git commit -m "git push comes later"', "git push --dry-run -h | cat <<'EOF'\ngh pr create\nEOF",
    "cat > notes.md <<'EOF'\ngit push origin main\nstripe charges create\nEOF", "printf '%s\\n' 'gh auth login'", "man git-push",
    "git log --format=push", "gh pr list", "git -C repo status", "ls /usr/bin/git", "which gh stripe",
])
def test_a_held_command_named_in_text_is_not_held(command):
    hit = gates.external_hit("Bash", {"command": command}, "r", D)
    assert hit is None or command.startswith("git push --dry-run"), command


def test_a_worker_is_refused_a_push_in_every_shape(tmp_path):
    spec = {"room": "r", "gate": {"trash": ["trash"], "private": str(tmp_path / "private")}}
    for command in PUSH_SHAPES:
        out = gates.worker_check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}, tmp_path, spec)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", command


RESTART_SHAPES = [
    "systemctl --user restart yerkd-test",
    "/usr/bin/systemctl --user restart yerkd-test",
    "systemctl --user --no-block restart yerkd-test",
    "systemctl --user -q restart yerkd-test.service",
    "systemctl restart --user yerkd-test",
    "bash -c 'systemctl --user restart yerkd-test'",
    "timeout 20 systemctl --user stop yerkd",
    "systemctl --user kill -s KILL yerkd-test",
    "/usr/bin/pkill -f yerkd",
    "timeout 5 killall yerkd",
    "kill $(pgrep -f yerkd)",
]


@pytest.mark.parametrize("command", RESTART_SHAPES)
def test_a_restart_of_yerkd_is_seen_in_every_shape(command, tmp_path):
    assert gates.restart_hit("Bash", {"command": command}, tmp_path / "private") == "a restart of yerkd", command


@pytest.mark.parametrize("command", [
    "systemctl --user status yerkd-test", "systemctl --user is-active yerkd-test", "systemctl --user restart yerk-index",
    "systemctl --user list-timers 'yerk-job-*'", "journalctl --user -u yerkd-test -n 50", 'echo "systemctl --user restart yerkd-test"',
    "systemctl --user show yerkd-test -p MainPID", "pgrep -f yerkd", "systemctl --user restart other-unit",
])
def test_a_look_at_yerkd_is_not_a_restart(command, tmp_path):
    assert gates.restart_hit("Bash", {"command": command}, tmp_path / "private") is None, command


def test_a_worker_is_refused_a_restart_in_every_shape(tmp_path):
    spec = {"room": "r", "gate": {"trash": ["trash"], "private": str(tmp_path / "private")}}
    for command in RESTART_SHAPES:
        out = gates.worker_check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}, tmp_path, spec)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", command


# Cleanup F5-09: the restart rule read a restart as it would run but a write to a start-up file as typed, so the write
# passed in a nested shell or by its relative name after a `cd`. It now reads the write as own_gate_hit reads one.
def _start_up_files(tmp_path: Path) -> Path:
    private = tmp_path / "private"
    private.mkdir()
    for name in gates.RESTART_FILES:
        (private / name).write_text("x")
    return private


START_UP_WRITES = [                               # {p} is the private folder; each writes one start-up file
    'bash -c "echo x >> {p}/yerkd.toml"', "cd {p} && sed -i s/a/b/ yerkd.toml", "sh -c 'cp /x/y {p}/rooms.toml'",
    "timeout 9 tee -a {p}/accounts.toml < new", "env A=1 perl -pi -e s/a/b/ {p}/yerkd.toml", "cp /x/rooms.toml {p}/",
    "cd {p}; echo x > ./accounts.toml", "echo x >> {p}/yerkd.toml", "mv new.toml {p}/yerkd.toml", "dd if=new of={p}/rooms.toml",
]


@pytest.mark.parametrize("shape", START_UP_WRITES)
def test_a_write_to_a_start_up_file_is_seen_in_every_shape(shape, tmp_path):
    private = _start_up_files(tmp_path)
    command = shape.format(p=private)
    assert (gates.restart_hit("Bash", {"command": command}, private) or "").startswith("a write to "), command


@pytest.mark.parametrize("shape", [
    "grep -n port {p}/yerkd.toml", "sed -n 1,20p {p}/yerkd.toml 2>&1 | head -5", "cp {p}/yerkd.toml /var/tmp/kept.toml",
    "diff {p}/rooms.toml rooms.toml > /dev/null", "cat {p}/accounts.toml | wc -l > count.txt", "echo x > {p}/notes.toml",
])
def test_a_read_of_a_start_up_file_is_no_write(shape, tmp_path):
    private = _start_up_files(tmp_path)
    command = shape.format(p=private)
    assert gates.restart_hit("Bash", {"command": command}, private) is None, command


def test_a_worker_is_refused_a_write_to_a_start_up_file_by_its_relative_name(tmp_path):
    private = _start_up_files(tmp_path)
    spec = {"room": "r", "gate": {"trash": ["trash"], "private": str(private)}}
    for command in ("sed -i s/a/b/ yerkd.toml", "echo x > rooms.toml", "cp new.toml accounts.toml"):
        out = gates.worker_check({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(private)}, tmp_path, spec)
        assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", command


def test_runs_reads_a_command_as_the_delete_rules_do():
    assert gates.runs("cd wt && timeout 60 /usr/bin/git push -u origin b 2>&1 | tail -5") == ["cd wt", "git push -u origin b", "tail -5"]
    assert gates.runs("echo x | xargs -n1 git push origin") == ["echo x", "git push origin"]      # behind xargs: its fixed words too
    assert gates.runs('bash -c "a; b c"') == ["a", "b c"]
    assert gates.runs("echo 'unclosed") == []                      # unreadable: the delete gate has refused it already


# G2: a worker's gate reads its settings from the worker's own folder (worker.json: the external rules, the private folder,
# the trash command; settings.json: the hook itself; mcp.json: its tools), which the worker may write. The unit now binds
# the three read-only, and the gate refuses a write to them in plain words.
def _worker(tmp_path: Path) -> tuple[Path, dict]:
    folder = tmp_path / "state" / "workers" / "w1"
    folder.mkdir(parents=True)
    spec = {"room": "r", "gate": {"trash": ["trash"], "private": str(tmp_path / "private")}}
    for name in gates.GATE_FILES:
        (folder / name).write_text("{}")
    return folder, spec


def _denied(folder: Path, spec: dict, tool: str, ti: dict, cwd: Path | None = None) -> bool:
    out = gates.worker_check({"tool_name": tool, "tool_input": ti, "cwd": str(cwd or folder)}, folder, spec)
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.mark.parametrize("name", ["worker.json", "settings.json", "mcp.json"])
def test_a_worker_may_not_write_its_own_gate_files(tmp_path, name):
    folder, spec = _worker(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    for tool, ti in (("Write", {"file_path": str(folder / name), "content": "{}"}),
                     ("Edit", {"file_path": str(folder / name), "old_string": "a", "new_string": "b"}),
                     ("Write", {"file_path": name, "content": "{}"}),                  # relative: its cwd is the folder
                     ("Bash", {"command": f"echo '{{}}' > {name}"}),
                     ("Bash", {"command": f"cp /dev/null {folder / name}"}),
                     ("Bash", {"command": f"sed -i 's/a/b/' ./{name}"}),
                     ("Bash", {"command": f"bash -c 'cat other.json > {name}'"}),
                     ("Bash", {"command": f"python3 -c \"open('{name}', 'w').write('{{}}')\""}),
                     ("Bash", {"command": f"echo x | tee -a {name}"}), ("Bash", {"command": f"mv other.json {name}"}),
                     ("Bash", {"command": f"dd if=/dev/zero of={name} count=1"}),
                     ("Bash", {"command": f"cp /tmp/x/{name} ."})):                     # a copy into its folder, under the same name
        assert _denied(folder, spec, tool, ti), (tool, ti)
    assert _denied(folder, spec, "Bash", {"command": f"cd {folder} && echo x > {name}"}, cwd=tmp_path / "elsewhere")


def test_a_worker_still_reads_its_gate_files_and_writes_its_own_work(tmp_path):
    folder, spec = _worker(tmp_path)
    for tool, ti in (("Read", {"file_path": str(folder / "worker.json")}), ("Bash", {"command": "cat worker.json"}),
                     ("Bash", {"command": "jq .room worker.json > room.txt"}), ("Bash", {"command": "sed -n 1,5p settings.json"}),
                     ("Bash", {"command": "grep -c hooks settings.json; ls -la"}), ("Write", {"file_path": "notes.md", "content": "x"}),
                     ("Write", {"file_path": str(folder / "wt" / "worker.json"), "content": "{}"}),     # a file of its work, same name
                     ("Bash", {"command": "cat > REPORT.md <<'EOF'\nworker.json holds the spec\nEOF"}),
                     ("Bash", {"command": "echo done > TASKS.md"}),
                     # naming a gate file is no write: a worker's commonest first command, and a read by a program of its own
                     # (2026-11-10: the 31 of 17,017 real worker commands that named one were all reads, and all were refused)
                     ("Bash", {"command": 'echo "--- worker.json ---" && cat worker.json && echo "--- settings.json ---" && cat settings.json'}),
                     ("Bash", {"command": "python3 -c \"import json;d=json.load(open('worker.json'));print(d.get('room'))\""}),
                     ("Bash", {"command": "cp worker.json copy.json"}), ("Bash", {"command": "find . -maxdepth 2 -name mcp.json | head"})):
        assert not _denied(folder, spec, tool, ti), (tool, ti)


def test_the_units_sandbox_binds_the_gate_files_read_only(tmp_path):
    from yerk_ai import workers
    folder, _ = _worker(tmp_path)
    (folder / "mcp.json").unlink()                     # a worker with no Yerk tools has none: a missing path is not named
    props = workers.Systemd(tmp_path / "state", ()).sandbox(folder, {"CLAUDE_CONFIG_DIR": str(tmp_path / "acct")})[1::2]
    assert f"ReadWritePaths={folder}" in props
    assert f"ReadOnlyPaths={folder / 'worker.json'}" in props and f"ReadOnlyPaths={folder / 'settings.json'}" in props
    assert not [p for p in props if "mcp.json" in p]


# G3: a gate whose hook fails must refuse. Claude Code carries an action out when a hook cannot start, times out or exits
# oddly, unless the hook says onFailure "block" (docs, hooks: "Block the action when a hook fails", v2.1.295 on). On
# 2026-10-19 one room's 170 PreToolUse calls got HTTP 403 and every action went ahead.
def test_the_gate_hook_blocks_when_it_fails_and_the_other_hooks_do_not(tmp_path):
    from yerk_ai import hooks
    s = hooks.settings_for("http://127.0.0.1:9/hook", tmp_path / "hook-header")
    assert [h.get("onFailure") for h in s["PreToolUse"][0]["hooks"]] == ["block"]
    for event in ("UserPromptSubmit", "Stop", "PostToolUse", "PostToolUseFailure", "ConfigChange", "SessionStart"):
        assert all("onFailure" not in h for h in s[event][0]["hooks"]), event     # a lost message or an endless turn is no gate


def test_the_gate_hook_as_written_refuses_when_the_daemon_does_not_answer(tmp_path):
    import socket
    import subprocess
    from yerk_ai import hooks
    with socket.socket() as sock:                     # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    (tmp_path / "hook-header").write_text("x-yerk-secret: t\n")
    s = hooks.settings_for(f"http://127.0.0.1:{port}/hook", tmp_path / "hook-header")
    gate = s["PreToolUse"][0]["hooks"][0]["command"]
    p = subprocess.run(["sh", "-c", gate], input='{"hook_event_name": "PreToolUse"}', capture_output=True, text=True, timeout=30)
    assert p.returncode == 2 and "gate" in p.stderr and "not run" in p.stderr     # exit 2: Claude Code blocks the call, the model reads why
    other = subprocess.run(["sh", "-c", s["PostToolUse"][0]["hooks"][0]["command"]], input="{}", capture_output=True, text=True, timeout=30)
    assert other.returncode not in (0, 2)             # any other hook: an error in the transcript, nothing blocked


def test_a_workers_gate_hook_blocks_when_it_fails(tmp_path):
    import json
    from yerk_ai import workers
    hook = json.loads(workers.gate_settings(tmp_path).read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook.get("onFailure") == "block"


# G6: the owner's word approves a hold they could have read: never one made after the word reached Yerk. A word folded into a
# running turn reaches the gate after the turn's next tool calls (measured: 1.0 to 2.4 s after it was written, 2 of 11 folded).
def test_the_word_never_approves_a_hold_made_after_it_was_written(tmp_path):
    import time
    from yerk_ai.ledger import Ledger
    led = Ledger(tmp_path / "ledger.sqlite")
    led.hold_add("r", "text-out", "a text in your name", 1, "text A", "/w")
    written = time.time()
    time.sleep(0.02)
    led.hold_add("r", "text-out", "a text in your name", 2, "text B", "/w")
    assert led.approve("r", "owner", 1800, before=written)["call"] == "text A"       # the one that was there to read
    assert led.approve("r", "owner", 1800, before=written) is None                    # B waits for a word of its own
    assert led.approve("r", "owner", 1800)["call"] == "text B"                        # a later word, written with B shown


# G7: a provider key pasted into a room was filed in the day log as written, then indexed, embedded at the provider and
# recalled. The imports' scrubber (sources.scrub) never saw a live message, and knew no Groq shape, though Yerk holds a Groq
# key itself. Live: one Groq-shaped string, 56 characters, memory/daily/2026-07-10.md line 264.
def test_a_provider_key_in_a_live_message_is_withheld_from_the_day_log(tmp_path):
    from zoneinfo import ZoneInfo
    from yerk_ai import memory, sources
    fakes = {"groq-key": "gsk_" + "Ab3" * 17 + "x", "github-token": "ghp_" + "Zz9" * 12, "anthropic-key": "sk-ant-" + "Qr7" * 10}
    log = memory.DailyLog(tmp_path / "daily", ZoneInfo("UTC"), {"r": ("r", "private")})
    for n, (kind, fake) in enumerate(fakes.items()):
        assert log.capture("r", f"m{n}", "owner", f"use this one {fake} for the transcripts, the door code is 4821")
    text = "".join(p.read_text() for p in (tmp_path / "daily").glob("*.md"))
    for kind, fake in fakes.items():
        assert fake not in text and f"[{kind} withheld]" in text, kind
    assert text.count("the door code is 4821") == 3            # what the owner files on purpose stays as they said it
    assert sources.scrub("gsk_" + "Ab3" * 17 + "x") == "[groq-key withheld]"      # an imported text or mail too
