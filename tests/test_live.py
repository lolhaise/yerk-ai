"""Contract test against a real Claude Code login: off unless YERK_LIVE=1 and YERK_LIVE_CONFIG_DIR is set."""
import argparse
import os

import pytest

from contract import probes

pytestmark = [pytest.mark.live, pytest.mark.skipif(
    os.environ.get("YERK_LIVE") != "1" or not os.environ.get("YERK_LIVE_CONFIG_DIR"),
    reason="needs a real Claude Code login")]


def test_live_hello(tmp_path):
    a = argparse.Namespace(config_dir=os.environ["YERK_LIVE_CONFIG_DIR"], model="claude-fable-5-1",
                           state=str(tmp_path), logs=None)
    out = probes.hello(a)
    assert out["ok"] and out["reply"] and out["ledger_row"]["outcome"] == "ok"
