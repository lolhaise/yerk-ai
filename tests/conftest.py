"""Shared setup pytest applies to every test, chiefly stand-ins so that no test reads or acts on the machine it runs on. Every test's
systemd has every timer it is asked about enabled: the doctor's timer check never reads the box's own (a test of that check gives its
own fake). Every test's box is on no tailnet: the page's default address is never the box's own tailnet one (a test of it gives its
own). Every test's box is no Windows PC: no page is placed on the owner's real screen by Windows' own PowerShell (a test of the placing
gives its own stand-in). Every test's meter reading finds no bwrap: the example layer's first account is Claude Code's default folder,
and the box's own bwrap never runs over them (a test of the hiding gives its own stand-in)."""
import os
import subprocess
from pathlib import Path

import pytest

from yerk_ai import doctor, usage
from yerk_ai.channels import page


# a worker's hidden home ([workers] home, 2026-11-24): the default state folder is there only as the empty read-only folder
# systemd makes for the unit's binds (0755), so a command run without --state cannot make it 0700 (config.state_dir); the
# tests that run one skip there, and nowhere else (an open unit's read-only state folder is 0700 already)
STATE_DEFAULT = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "yerk"
state_read_only = pytest.mark.skipif(not os.environ.get("YERK_STATE") and STATE_DEFAULT.is_dir() and bool(STATE_DEFAULT.stat().st_mode & 0o077)
                                     and not os.access(STATE_DEFAULT, os.W_OK),
                                     reason="the default state folder is read-only and not 0700 here (a worker's hidden home)")


@pytest.fixture(autouse=True)
def timers_enabled(monkeypatch):
    monkeypatch.setattr(doctor, "systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 0, "enabled\n" * (len(a) - 1), ""))


@pytest.fixture(autouse=True)
def no_tailnet(monkeypatch):
    monkeypatch.setattr(page, "tailnet", lambda: "")


@pytest.fixture(autouse=True)
def no_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(page, "wsl", lambda: False)
    monkeypatch.setattr(page, "POWERSHELL", str(tmp_path / "no-powershell"))


@pytest.fixture(autouse=True)
def no_bwrap(monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "BWRAP", str(tmp_path / "no-bwrap"))
