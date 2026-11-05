"""Shared setup pytest applies to every test, chiefly stand-ins so that no test reads or acts on the machine it runs on. Every test's
systemd has every timer it is asked about enabled: the doctor's timer check never reads the box's own (a test of that check gives its
box's own tailnet one (a test of it gives its own)."""
import subprocess

import pytest

from yerk_ai import doctor
from yerk_ai.channels import page


@pytest.fixture(autouse=True)
def timers_enabled(monkeypatch):
    monkeypatch.setattr(doctor, "systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 0, "enabled\n" * (len(a) - 1), ""))


@pytest.fixture(autouse=True)
def no_tailnet(monkeypatch):
    monkeypatch.setattr(page, "tailnet", lambda: "")
