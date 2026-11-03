"""A first run without yerkd.toml's [discord] table, or with a token file that is missing or empty, ends in one
plain line (SystemExit), never a traceback: the README's install block runs yerkd before the stranger has written
yerkd.toml, and every other config miss already fails this way."""
import asyncio
import shutil

import pytest

from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd


def _refusal(private, state):
    state.mkdir(parents=True, exist_ok=True)
    with pytest.raises(SystemExit) as e:
        asyncio.run(Yerkd(private, state).run_discord())
    return str(e.value)


def test_no_discord_table_is_one_plain_line_naming_the_readme(tmp_path):
    said = _refusal(EXAMPLE_PRIVATE, tmp_path / "state")           # the fake layer ships no yerkd.toml
    assert said.startswith("yerkd.toml: no [discord] token_file") and "README.md" in said and "\n" not in said


def test_a_missing_or_empty_token_file_is_named_by_its_path(tmp_path):
    priv = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    (priv / "yerkd.toml").write_text('[discord]\ntoken_file = "secrets/discord.token"\nowner = 1\n')
    assert _refusal(priv, tmp_path / "s1").startswith("yerkd.toml: [discord] token_file secrets/discord.token is not a file")
    (priv / "secrets").mkdir()
    (priv / "secrets" / "discord.token").write_text("")
    assert _refusal(priv, tmp_path / "s2").startswith("yerkd.toml: [discord] token_file secrets/discord.token is empty")
