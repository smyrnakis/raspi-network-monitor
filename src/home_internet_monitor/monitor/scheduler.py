"""A monotonic, non-overlapping scheduler for probe rounds."""

import asyncio
from typing import Awaitable, Callable


class NonOverlappingScheduler:
    def __init__(
        self, callback: Callable[[], Awaitable[object]], interval_seconds: float
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._callback = callback
        self._interval = interval_seconds
        self._lock = asyncio.Lock()

    async def run_once(self) -> bool:
        if self._lock.locked():
            return False
        async with self._lock:
            await self._callback()
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        next_run = loop.time()
        while not stop.is_set():
            await self.run_once()
            next_run += self._interval
            if next_run < loop.time():
                next_run = loop.time()
            try:
                await asyncio.wait_for(stop.wait(), timeout=next_run - loop.time())
            except asyncio.TimeoutError:
                pass
