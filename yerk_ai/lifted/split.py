# Adapted from Hermes Agent v0.22.2, gateway/platforms/base.py (BasePlatformAdapter.truncate_message)
# and gateway/platforms/helpers.py (fence_state_after),
# https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
# Changes: a module function instead of a static method; the MarkdownV2 inline-code rule kept; the custom
# length unit (_custom_unit_to_cp, for Telegram's UTF-16 count) dropped, since no caller here passes one.
"""Split a reply into chunks under a channel's limit, never leaving a code block open."""
from __future__ import annotations


def fence_state_after(text: str, in_code: bool = False, lang: str = "") -> tuple[bool, str]:
    """Walk `text` line by line toggling on ``` lines; return the final (in_code, lang)."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            tag = stripped[3:].split()
            in_code, lang = (False, "") if in_code else (True, tag[0] if tag else "")
    return in_code, lang


def split_message(content: str, max_length: int = 2000) -> list[str]:
    """Chunks of at most `max_length`: a split inside a fence closes it at the chunk end and
    reopens it (same language tag) in the next; multi-chunk output gets (1/3) indicators."""
    if len(content) <= max_length:
        return [content]
    INDICATOR_RESERVE = 10   # room for " (XX/XX)"
    FENCE_CLOSE = "\n```"
    chunks: list[str] = []
    remaining = content
    carry_lang: str | None = None
    while remaining:
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""
        if (headroom := max_length - INDICATOR_RESERVE - len(prefix) - len(FENCE_CLOSE)) < 1:
            headroom = max(1, max_length // 2)
        if len(prefix) + len(remaining) <= max_length - INDICATOR_RESERVE:
            final_chunk = prefix + remaining
            if carry_lang is not None and fence_state_after(remaining, True, carry_lang)[0]:
                final_chunk += FENCE_CLOSE
            chunks.append(final_chunk)
            break
        region = remaining[:headroom]
        if (split_at := region.rfind("\n")) < headroom // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            split_at = max(1, headroom)
        candidate = remaining[:split_at]
        backtick_count = candidate.count("`") - candidate.count("\\`")
        if backtick_count % 2 == 1:
            last_bt = candidate.rfind("`")
            while last_bt > 0 and candidate[last_bt - 1] == "\\":
                last_bt = candidate.rfind("`", 0, last_bt)
            if last_bt > 0:
                safe_split = max(candidate.rfind(" ", 0, last_bt), candidate.rfind("\n", 0, last_bt))
                if safe_split > headroom // 4:
                    split_at = safe_split
        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip()
        full_chunk = prefix + chunk_body
        in_code, lang = fence_state_after(chunk_body, carry_lang is not None, carry_lang or "")
        carry_lang = lang if in_code else None
        chunks.append(full_chunk + FENCE_CLOSE if in_code else full_chunk)
    if len(chunks) > 1:
        chunks = [f"{chunk} ({i + 1}/{len(chunks)})" for i, chunk in enumerate(chunks)]
    return chunks
