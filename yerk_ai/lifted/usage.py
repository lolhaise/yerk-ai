# Adapted from the author's tools/status-card (2026-09-01): fetch_usage and the five_hour and
# seven_day reading of its tank lines. Changes: one account per call, no token refresh (Yerk AI
# never writes a Claude Code config folder), the account's one-year token as the fallback
# credential, and a plain dict out instead of card text.
# Copyright (c) 2026 Eric McLaren. MIT License. See NOTICE.
"""An account's 5-hour and weekly usage, read the way Claude Code's own /usage reads it."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
WINDOWS = ("five_hour", "seven_day")


def stored_login(config_dir: Path) -> str | None:
    """The config folder's own login, when it has one that has not expired. Never printed."""
    try:
        oauth = json.loads((config_dir / ".credentials.json").read_text())["claudeAiOauth"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    tok = oauth.get("accessToken")
    return tok if tok and oauth.get("expiresAt", 0) / 1000 > time.time() else None


def parse(data: dict[str, Any]) -> dict[str, Any]:
    """{window: {"pct": percent used, "resets": epoch seconds or None}} for the two windows."""
    out: dict[str, Any] = {}
    for w in WINDOWS:
        d = data.get(w) or {}
        if d.get("utilization") is None:
            continue
        r = d.get("resets_at")
        out[w] = {"pct": float(d["utilization"]),
                  "resets": datetime.fromisoformat(r.replace("Z", "+00:00")).timestamp() if r else None}
    return out


def read_usage(config_dir: Path, token: str | None = None, timeout: float = 3.0) -> dict[str, Any]:
    """The live reading: the folder's login first, else the account's token. {"error": why} when
    neither answers; the reason never contains the credential."""
    creds = [c for c in (stored_login(config_dir), token) if c]
    if not creds:
        return {"error": "no usable login to read the meter with"}
    why = ""
    for tok in creds:
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {tok}", "User-Agent": "yerk", "Accept": "application/json",
            "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return {**parse(json.load(res)), "source": "live", "at": time.time()}
        except urllib.error.HTTPError as e:
            why = f"http {e.code}"
        except (OSError, ValueError) as e:
            why = type(e).__name__
    return {"error": why}
