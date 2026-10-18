"""Reads abilities.toml, the list of every ability Yerk AI has and every one it knows how to build but does not ship, with
what each needs and costs, and says each one's state on this install. `yerk abilities`, `yerk explain` and a room message
that asks about one ("can you do
voice?") all read it here through the same explain lines, so the answer is the same everywhere
and comes from the file, never from a guess (decision 16; the list itself, decisions 23 and 26).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from . import words
from .config import load_toml, table

log = logging.getLogger("yerk.abilities")
STATES = ("built", "described")


def manifest() -> dict[str, dict[str, Any]]:
    return dict(load_toml(table("abilities.toml"))["abilities"])


def key_path(name: str, s: Any) -> Path | None:
    """Where an ability's provider key lives on this install (memory settings), if it takes one."""
    return {"embeddings": getattr(s, "embeddings_key", None)}.get(name)


def state(name: str, ab: dict[str, Any], tab: dict[str, dict[str, Any]], s: Any) -> tuple[str, str]:
    """(mark, words): on; needs <provider>; runs on the free path (the ability that lifts it is
    off); known, not built (described only, decision 26). The key file is looked at, never read."""
    if ab.get("state", "built") == "described":
        return words.ability("described")
    if ab["tier"] in ("key", "paid"):
        key = key_path(name, s)
        if key is not None and key.is_file() and key.stat().st_size > 0:
            return words.ability("on")
        return words.ability("needs", provider=ab.get("provider", "a provider key"))
    up = str(ab.get("upgrade", ""))
    if up and up in tab and state(up, tab[up], tab, s)[0] != words.DONE:
        return words.ability("free")
    return words.ability("on")


def line(name: str, ab: dict[str, Any], tab: dict[str, dict[str, Any]], s: Any) -> str:
    """`yerk abilities`: one line, state first."""
    mark, said = state(name, ab, tab, s)
    return words.line(f"{mark} {name}: {said}")


def explain(name: str, ab: dict[str, Any], tab: dict[str, dict[str, Any]], s: Any) -> list[str]:
    """`yerk explain NAME`: the state, what it is, how it would be built (a described one), what it
    needs and costs, how to add its key, its free path, and why it is not built."""
    mark, said = state(name, ab, tab, s)
    out = [f"{mark} {name}: {said}", str(ab["what"])]
    if ab.get("how"):
        out += ["", "How it would be built:", *[f"- {x}" for x in ab["how"]]]
    out += ["", f"Needs: {ab['needs']}"]
    for field, label in (("provider", "Provider"), ("model", "Model")):
        if ab.get(field):
            out.append(f"{label}: {ab[field]}")
    out.append(f"Cost: {ab.get('price') or 'nothing, it runs on the box'}")
    if ab.get("key"):
        out.append(f"How to add the key: {ab['key']}")
    out.append(f"Free path: {ab['free_path']}")
    if ab.get("why_not"):
        out.append(f"Why it is not built: {ab['why_not']}")
    return out


def brief(name: str, ab: dict[str, Any], tab: dict[str, dict[str, Any]], s: Any) -> str:
    """The explain lines as one paragraph for a room, under the per-message recall cap: each `how`
    step by its lead words (what comes before its colon); `yerk explain NAME` has the rest."""
    _, said = state(name, ab, tab, s)
    how = "; ".join(str(x).split(":", 1)[0] for x in ab.get("how", []))
    parts = [f"{name}, a Yerk AI ability ({said}): {ab['what']}", f"How it would be built: {how}." if how else "",
             f"Needs: {ab['needs']}.", f"Cost: {ab.get('price') or 'nothing, it runs on the box'}.",
             f"Free path: {ab['free_path']}.", f"Why it is not built: {ab['why_not']}" if ab.get("why_not") else ""]
    return " ".join(x for x in parts if x)


def asked(prompt: str, s: Any, tab: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The abilities a room message names by one of its `asked_as` phrases, as entries for the
    per-message recall part (`brief`), so the room answers from the manifest in its own words, never
    "I can't" and never a guess."""
    try:
        tab = manifest() if tab is None else tab
    except (OSError, ValueError, KeyError) as e:
        log.warning("abilities: the manifest could not be read: %s", e)
        return []
    low, out = prompt.lower(), []
    for name, ab in tab.items():
        if any(re.search(rf"\b{re.escape(str(p).lower())}\b", low) for p in ab.get("asked_as", [])):
            out.append({"key": name, "text": brief(name, ab, tab, s), "cite": f"yerk explain {name}"})
    return out
