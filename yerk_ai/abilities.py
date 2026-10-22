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
SELF = "abilities"                  # "what can you do": its answer is every ability's name by state
HOW = {"built": "How it works:", "described": "How it would be built:"}     # its `how` lines


def manifest() -> dict[str, dict[str, Any]]:
    return dict(load_toml(table("abilities.toml"))["abilities"])


def state(name: str, ab: dict[str, Any], tab: dict[str, dict[str, Any]], s: Any) -> tuple[str, str]:
    """(mark, words): on; needs <provider>; runs on the free path (the ability that lifts it is
    off); known, not built (described only, decision 26). The key file is looked at, never read."""
    if ab.get("state", "built") == "described":
        return words.ability("described")
    if ab["tier"] in ("key", "paid"):
        key: Path | None = {"embeddings": getattr(s, "embeddings_key", None),     # the provider key's path; the install
                            "transcribe": getattr(s, "embeddings_key", None)}.get(name)   # has one OpenAI key file
        if key is not None and key.is_file() and key.stat().st_size > 0:
            return words.ability("on")
        return words.ability("needs", provider=ab.get("provider", "a provider key"))
    if (up := str(ab.get("upgrade", ""))) and up in tab and state(up, tab[up], tab, s)[0] != words.DONE:
        return words.ability("free")
    return words.ability("on")


def line(name: str, ab: dict[str, Any], tab: dict[str, dict[str, Any]], s: Any) -> str:
    """`yerk abilities`: one line, state first."""
    mark, said = state(name, ab, tab, s)
    return words.line(f"{mark} {name}: {said}")


def census(tab: dict[str, dict[str, Any]], s: Any) -> list[str]:
    """Every ability's name grouped by its state here, built ones first, a line per state: the
    answer to "what can you do", short enough to ride whole under the per-message recall cap."""
    groups: dict[str, list[str]] = {}
    for name, ab in sorted(tab.items(), key=lambda kv: kv[1].get("state", "built") == "described"):
        groups.setdefault(state(name, ab, tab, s)[1], []).append(name)
    return [f"{said} ({len(names)}): {', '.join(names)}." for said, names in groups.items()]


def built_part(ab: dict[str, Any], tab: dict[str, dict[str, Any]]) -> str:
    """A described ability's `upgrade`: the built ability that already ships part of it."""
    up = str(ab.get("upgrade", ""))
    return up if ab.get("state") == "described" and up in tab and tab[up].get("state", "built") == "built" else ""


def explain(name: str, ab: dict[str, Any], tab: dict[str, dict[str, Any]], s: Any) -> list[str]:
    """`yerk explain NAME`: the state, what it is, how it would be built (a described one), what it
    needs and costs, how to add its key, its free path, and why it is not built."""
    mark, said = state(name, ab, tab, s)
    out = [f"{mark} {name}: {said}", str(ab["what"])]
    if name == SELF:
        out += ["", *census(tab, s)]
    if ab.get("how"):
        out += ["", HOW[ab.get("state", "built")], *[f"- {x}" for x in ab["how"]]]
    out += ["", f"Needs: {ab['needs']}"]
    out += [f"{label}: {ab[field]}" for field, label in (("provider", "Provider"), ("model", "Model")) if ab.get(field)]
    out.append(f"Cost: {ab.get('price') or 'nothing, it runs on the box'}")
    if name == "embeddings" and getattr(s, "embeddings_model", None):
        out.append(f"Here: {s.embeddings_model} at {s.embeddings_dim} dimensions, {s.embeddings_width} kept in memory, "
                   f"units {s.embeddings_units} ([memory] in yerkd.toml; yerk config get memory says what each "
                   "setting buys and costs)")
    if ab.get("key"):
        out.append(f"How to add the key: {ab['key']}")
    out.append(f"Free path: {ab['free_path']}")
    if built_part(ab, tab):
        out.append(f"Built part: {built_part(ab, tab)} (yerk explain {built_part(ab, tab)})")
    if ab.get("why_not"):
        out.append(f"Why it is not built: {ab['why_not']}")
    return out


def brief(name: str, ab: dict[str, Any], tab: dict[str, dict[str, Any]], s: Any) -> str:
    """The explain lines as one paragraph for a room, under the per-message recall cap: each `how`
    step by its lead words (what comes before its colon); `yerk explain NAME` has the rest."""
    _, said = state(name, ab, tab, s)
    if name == SELF:
        return " ".join([f"{name}, a Yerk AI ability ({said}): {ab['what']}", *census(tab, s),
                         "`yerk explain NAME` says what any one does, needs and costs."])
    how = "; ".join(str(x).split(":", 1)[0] for x in ab.get("how", []))
    up = built_part(ab, tab)
    parts = [f"{name}, a Yerk AI ability ({said}): {ab['what']}", f"{HOW[ab.get('state', 'built')]} {how}." if how else "",
             f"Needs: {ab['needs']}.", f"Cost: {ab.get('price') or 'nothing, it runs on the box'}.",
             f"Free path: {ab['free_path']}.", f"Built part: {up}." if up else "",
             f"Why it is not built: {ab['why_not']}" if ab.get("why_not") else ""]
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
    low = prompt.lower()
    return [{"key": name, "text": brief(name, ab, tab, s), "cite": f"yerk explain {name}"} for name, ab in tab.items()
            if any(re.search(rf"\b{re.escape(str(p).lower())}\b", low) for p in ab.get("asked_as", []))]
