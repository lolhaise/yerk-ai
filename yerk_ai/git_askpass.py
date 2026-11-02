"""Answers git's username and password prompts for github.com with a token read from a file, so the token never reaches a command line, the unit's
environment or a log. It is the GIT_ASKPASS helper of the needs pipeline's deploy unit (pipeline.py): git asks for the username and the password
of https://github.com, and this answers `x-access-token` and the token read from $YERK_GITHUB_TOKEN_FILE. Any other prompt gets nothing."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def answer(prompt: str, token_file: str) -> str:
    if prompt.lower().startswith("username"):
        return "x-access-token"
    if prompt.lower().startswith("password") and token_file:
        try:
            return Path(token_file).read_text().strip()
        except OSError:
            return ""
    return ""


def main(argv: list[str] | None = None) -> int:
    said = answer(" ".join((sys.argv[1:] if argv is None else argv)), os.environ.get("YERK_GITHUB_TOKEN_FILE", ""))
    if said:
        print(said)
    return 0 if said else 1


if __name__ == "__main__":
    sys.exit(main())
