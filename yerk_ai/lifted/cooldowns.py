# Adapted from Hermes Agent v0.22.2, agent/credential_pool_model_cooldowns.py
# (model_cooldown_until and merge_model_cooldowns), https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
# Changes: keyed on an account name instead of a pooled credential and a model; the pool,
# provider and entitlement logic is gone.
"""Which accounts are resting, and until when."""
from __future__ import annotations

import time
from typing import Any, Mapping


def cooldown_until(cooldowns: Mapping[str, Any], key: str | None, now: float | None = None) -> float | None:
    """Active cooldown blocking `key`, or None.

    A caller that names no key stays conservative: any active cooldown blocks it.
    """
    values = cooldowns.values() if not key else (cooldowns.get(key),)
    now = time.time() if now is None else now
    active = [float(until) for until in values if isinstance(until, (int, float)) and until > now]
    return max(active) if active else None


def merge_cooldowns(*maps: Any) -> dict[str, float]:
    """Latest reset per key across snapshots; each writer only observed its own key."""
    merged: dict[str, float] = {}
    for cooldowns in maps:
        if not isinstance(cooldowns, dict):
            continue
        for key, until in cooldowns.items():
            if isinstance(until, (int, float)):
                merged[key] = max(float(until), merged.get(key, 0.0))
    return merged
