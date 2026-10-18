# Adapted from Hermes Agent v0.22.2, gateway/bot_loop_guard.py (BotLoopGuard.admit and _sweep),
# https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
# Changes: fixed settings passed in, no config reads, no enabled switch.
"""A sliding-window budget for machine-sent messages in a room, with a cooldown once it trips."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Hashable


class LoopGuard:
    def __init__(self, max_events: int = 20, window_seconds: float = 300.0, cooldown_seconds: float = 600.0,
                 clock: Callable[[], float] = time.monotonic):
        self.max_events, self.window, self.cooldown, self.clock = max_events, window_seconds, cooldown_seconds, clock
        self._lock = threading.Lock()
        self._events: dict[Hashable, deque[float]] = {}
        self._cooldown_until: dict[Hashable, float] = {}

    def admit(self, conversation: Hashable) -> tuple[bool, str]:
        """Count one machine-sent message: (allowed, state); state is ok, tripped or cooldown."""
        now = self.clock()
        with self._lock:
            if self._cooldown_until.get(conversation, 0.0) > now:
                return False, "cooldown"
            events = self._events.setdefault(conversation, deque())
            while events and events[0] <= now - self.window:
                events.popleft()
            if len(events) >= self.max_events:
                self._cooldown_until[conversation] = now + self.cooldown
                events.clear()
                return False, "tripped"
            events.append(now)
            return True, "ok"
