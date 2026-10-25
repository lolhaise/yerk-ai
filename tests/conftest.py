"""Shared setup pytest applies to every test, chiefly stand-ins so that no test reads or acts on the machine it runs on. Every test's
(a test of that check gives its own fake)."""
import subprocess

import pytest

from yerk_ai import doctor


@pytest.fixture(autouse=True)
def timers_enabled(monkeypatch):
    monkeypatch.setattr(doctor, "systemctl", lambda *a, **k: subprocess.CompletedProcess(a, 0, "enabled\n" * (len(a) - 1), ""))
