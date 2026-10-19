"""Checks that a worker's tool calls pass the same gates (the approval rules) as a room's, with deletes held to
its own folder: the hook workers.py writes into a worker's settings, and what gates.worker_check answers. Every
under pytest's tmp_path and the file to delete is delete-target; nothing here deletes anything."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_workers import Busy, ended, setup  # noqa: F401  (setup is the workers fixture)
from yerk_ai import gates, workers


@pytest.fixture
def worker(tmp_path: Path) -> tuple[Path, dict, Path]:
    folder = tmp_path / "workers" / "w1"
    folder.mkdir(parents=True)
    (folder / "delete-target").write_text("x")
    (tmp_path / "workers" / "delete-target").write_text("x")
    private = tmp_path / "private"
    private.mkdir()
    spec = {"room": "r", "gate": {"trash": ["trash"], "private": str(private)}}
    (folder / "worker.json").write_text(json.dumps(spec))
    return folder, spec, private


def ask(folder: Path, spec: dict, tool: str, **ti: str) -> dict:
    return gates.worker_check({"tool_name": tool, "tool_input": ti, "cwd": str(folder)}, folder, spec)


def denied(out: dict) -> str:
    """The reason when the hook says deny, else ""."""
    h = out.get("hookSpecificOutput") or {}
    return str(h.get("permissionDecisionReason", "")) if h.get("permissionDecision") == "deny" else ""


def test_a_plain_delete_in_the_workers_folder_goes_to_the_trash(worker) -> None:
    folder, spec, _ = worker
    out = ask(folder, spec, "Bash", command="rm delete-target", description="tidy up")
    assert out["hookSpecificOutput"]["updatedInput"] == {"command": "trash -- delete-target", "description": "tidy up"}


@pytest.mark.parametrize("command", ["rm ../delete-target", "cd .. && rm delete-target", "timeout 5 rm -r ..",
                                     "taskset -c 1 ls", "rm *", "echo 'open"])
def test_what_the_room_gate_refuses_the_worker_gate_refuses(worker, command: str) -> None:
    folder, spec, _ = worker
    assert denied(ask(folder, spec, "Bash", command=command))


@pytest.mark.parametrize("command", ["git push origin main", "gh pr create -t x -b y", "npm publish"])
def test_an_external_action_is_refused_never_held(worker, command: str) -> None:
    folder, spec, _ = worker
    why = denied(ask(folder, spec, "Bash", command=command))
    assert "external action" in why and "never takes one" in why


def test_a_write_to_yerks_config_or_a_restart_of_it_is_refused(worker) -> None:
    folder, spec, private = worker
    assert "rooms.toml" in denied(ask(folder, spec, "Write", file_path=str(private / "rooms.toml"), content="x"))
    assert "restart" in denied(ask(folder, spec, "Bash", command="systemctl --user restart yerkd"))


@pytest.mark.parametrize(("tool", "ti"), [("Bash", {"command": "ls -la"}), ("Bash", {"command": "python -m pytest -q"}),
                                          ("Bash", {"command": "grep -rn 'rm -rf' ."}), ("Read", {"file_path": "notes.md"}),
                                          ("Write", {"file_path": "notes.md", "content": "x"}),
                                          ("WebFetch", {"url": "https://example.com", "prompt": "read it"})])
def test_ordinary_calls_pass_untouched(worker, tool: str, ti: dict) -> None:
    folder, spec, _ = worker
    assert gates.worker_check({"tool_name": tool, "tool_input": ti, "cwd": str(folder)}, folder, spec) == {}


def test_the_hook_fails_closed_on_a_call_it_cannot_read(worker, monkeypatch, capsys) -> None:
    folder, _, _ = worker
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert gates.worker_hook(folder) == 0
    assert denied(json.loads(capsys.readouterr().out))


def test_the_hook_command_as_written_runs_with_claudes_bare_environment(worker) -> None:
    folder, _, _ = worker
    cmd = json.loads(workers.gate_settings(folder).read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    payload = {"tool_name": "Bash", "tool_input": {"command": "rm ../delete-target"}, "cwd": str(folder)}
    r = subprocess.run(["sh", "-c", cmd], input=json.dumps(payload), capture_output=True, text=True, cwd=folder,
                       env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, timeout=60)
    assert r.returncode == 0 and "outside the work folder" in denied(json.loads(r.stdout))
    assert (folder.parent / "delete-target").exists()


def test_a_worker_is_launched_with_the_gate_hook_on_every_tool_call(setup) -> None:  # noqa: F811
    s = setup

    async def go():
        w = s.make({"r": Busy()})
        w.gate = {"trash": ["trash"], "private": str(s.tmp / "private")}
        w.begin(asyncio.get_running_loop())
        out = await w.start("r", 'FAKE report="gated"', account="one")
        return out, await ended(s.ledger, out["id"])
    out, row = asyncio.run(go())
    folder = Path(out["staging"])
    hook = json.loads((folder / "settings.json").read_text())["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "*" and hook["hooks"][0]["type"] == "command"
    assert hook["hooks"][0]["command"].endswith(f"-m yerk_ai.gates worker-hook {folder}")
    argv = json.loads((folder / "fake-record.json").read_text())["argv"]
    assert argv[argv.index("--settings") + 1] == str(folder / "settings.json")
    assert json.loads((folder / "worker.json").read_text())["gate"]["trash"] == ["trash"]
    assert "pass Yerk AI's gates" in (folder / "PROMPT.md").read_text() and row["exit"] == "done"
