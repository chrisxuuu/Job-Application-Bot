from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """
    Simple token-bucket rate limiter for a single resource.
    Thread-safe via asyncio lock.
    """

    def __init__(self, calls_per_minute: float) -> None:
        self.min_interval = 60.0 / calls_per_minute  # seconds between calls
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
