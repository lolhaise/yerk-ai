# Lifted from Hermes Agent v0.22.2, gateway/systemd_notify.py (commit d923a5eb),
# https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
# Changes: this header, and the SystemdWatchdog class left out: yerk_ai/beat.py uses notify() and
# watchdog_interval_seconds() and beats from a thread of its own, off the loop, because a stall dump
# has to be taken while the loop is blocked.
"""Minimal, optional systemd ``sd_notify`` support for the gateway."""

from __future__ import annotations

import math
import os
import socket


def _notify_socket() -> str:
    return os.environ.get("NOTIFY_SOCKET", "").strip() if hasattr(socket, "AF_UNIX") else ""


def notify(message: str) -> bool:
    """Send an sd_notify datagram if systemd configured it; failures never block gateway startup."""
    if not (address := _notify_socket()) or not isinstance(message, str) or not message:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)  # a full receiver buffer must not stall the event loop
            # systemd's ``@abstract`` notation -> Python's leading-NUL address form
            sender.connect("\0" + address[1:] if address.startswith("@") else address)
            sender.send(message.encode("utf-8"))
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def watchdog_interval_seconds() -> float | None:
    try:
        interval = float(os.environ.get("WATCHDOG_USEC", "") if _notify_socket() else "") / 1e6
    except (TypeError, ValueError):
        return None
    return interval if math.isfinite(interval) and interval > 0 else None
