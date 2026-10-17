# Adapted from the author's tools/token-expiry-watch (2026-09-11): the dead, due and ok
# window math only. Reading a login file is left to the caller.
# Copyright (c) 2026 Eric McLaren. MIT License. See NOTICE.
"""Is a login dead, due for renewal, or fine."""
from __future__ import annotations

DAY_MS = 86_400_000


def login_state(expires_ms: int | None, has_token: bool, now_ms: int, window_days: float = 30) -> tuple[str, float]:
    """Return (state, days_left): "dead" with no token or a past expiry, "due" inside the
    window, otherwise "ok"."""
    if not has_token or not expires_ms or expires_ms <= now_ms:
        return "dead", 0.0
    days_left = (expires_ms - now_ms) / DAY_MS
    return ("due" if days_left <= window_days else "ok"), days_left
