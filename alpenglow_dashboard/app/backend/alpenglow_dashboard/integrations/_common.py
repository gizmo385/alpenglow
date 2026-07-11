"""Shared helpers for the B4 external integrations.

- ``env_url`` / ``env`` — config from the environment with the documented
  defaults (mirrors ``.env.example``).
- ``AsyncTTLCache`` — a tiny per-integration async cache: a single in-flight
  fetch is shared across concurrent callers and its result is reused for
  ``ttl`` seconds. ``inventory.service_enrichment()`` calls the updates fetch on
  every request, so caching here is load-bearing, not just an optimisation.
- ``ago`` — humanise a "seconds ago" delta into the compact strings the tiles
  render ("3h ago", "4d ago").

Every integration degrades on failure: helpers here swallow transport errors and
let callers substitute ``None`` fields rather than 500.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Awaitable, Callable, Optional, TypeVar

import httpx

T = TypeVar("T")

# Short, uniform timeout for every live integration call.
DEFAULT_TIMEOUT = httpx.Timeout(4.0, connect=2.0)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_url(name: str, default: str) -> str:
    """A base URL from the environment, trailing slash stripped."""
    return os.environ.get(name, default).rstrip("/")


class AsyncTTLCache:
    """Single-slot async cache with request coalescing.

    ``get(loader)`` returns the cached value while fresh; otherwise it awaits
    ``loader`` once (concurrent callers share the same in-flight task) and caches
    the result. If ``loader`` raises, the exception propagates to *this* call and
    nothing is cached, so callers can degrade to ``None`` and a later call retries.
    """

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._value: object = None
        self._expires: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, loader: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        if now < self._expires:
            return self._value  # type: ignore[return-value]
        async with self._lock:
            now = time.monotonic()
            if now < self._expires:
                return self._value  # type: ignore[return-value]
            value = await loader()
            self._value = value
            self._expires = time.monotonic() + self.ttl
            return value

    def invalidate(self) -> None:
        self._expires = 0.0


def ago(seconds: Optional[float]) -> Optional[str]:
    """Compact 'time since' label: 45s, 12m, 3h, 4d ago (None → None)."""
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"
