"""Actual coroutine ownership for Browser activity tokens.

Callers pass a lazy operation so no CDP starts before admission. A cancelled
concurrent Future does not own this lifetime: the token leaves only after the
operation's coroutine and its finally have settled on the host event loop.
"""
from __future__ import annotations
import asyncio
from .activity import BrowserActivity


class ActivityWork:
    """Event-loop-owned task registry; the underlying barrier is thread-safe."""
    def __init__(self, gate: BrowserActivity):
        self.gate = gate
        self._tasks = {}  # activity token -> actual task; nesting stays counted

    async def run(self, profile: str, instance: int, operation):
        token = self.gate.enter(profile, instance)
        task = asyncio.current_task()
        self._tasks[token] = task
        try:
            return await operation()
        finally:
            self._tasks.pop(token, None)
            self.gate.leave(token)

    async def settle(self):
        """Shutdown closes admission first, then settles actual child cleanup."""
        self.gate.stop()
        current = asyncio.current_task()
        tasks = {task for task in self._tasks.values() if task is not current}
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)