"""Bounded cleanup only for dialogs/downloads refused by activity admission.

One batch and one overflow token per exact live instance. It does no owner or
agent action: dialogs are dismissed, downloads canceled/deleted. A cap, timeout
or failure closes only that same instance; a replacement is never a target.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field

CAP = 16
TIMEOUT_S = 2.0

@dataclass
class _Batch:
    instance: object
    token: object
    queue: list = field(default_factory=list)
    retire: bool = False
    task: object = None
    started: bool = False

class AttentionCleanup:
    def __init__(self, host):
        self.host = host
        self._batches = {}

    def refuse(self, tab, kind, value):
        host = self.host
        instance = host._instances.get(tab.profile)
        if (host._tabs.get(tab.id) is not tab or instance is None
                or instance.closing or host._stopped):
            return
        batch = self._batches.get(tab.profile)
        if batch is None:
            token = host.activity.cleanup_hold(tab.profile, instance.generation)
            if token is None:
                # Only an already committed replacement or closed admission
                # can refuse this reserved slot. Its teardown owns the page.
                return
            batch = _Batch(instance, token)
            self._batches[tab.profile] = batch
            batch.task = asyncio.create_task(self._run(batch))
            batch.task.add_done_callback(lambda task: task.cancelled() or task.exception())
        if batch.instance is not instance:
            return
        if len(batch.queue) >= CAP:
            batch.retire = True
            instance.closing = True
            host._publish_snapshot()
        else:
            batch.queue.append((kind, value))

    @staticmethod
    async def _dispose(kind, value):
        if kind == 'dialog':
            await value.dismiss()
        else:
            await value.cancel()
            await value.delete()

    async def _retire(self, batch):
        host, instance = self.host, batch.instance
        # Both before and after the async lock: no same-profile replacement
        # can be closed by a stale failure/canceled cleanup.
        if host._instances.get(instance.profile) is not instance:
            return
        instance.closing = True
        host._publish_snapshot()
        async with host._launching.setdefault(instance.profile, asyncio.Lock()):
            if host._instances.get(instance.profile) is instance:
                await host._close_instance(instance.profile)

    async def _run(self, batch):
        batch.started = True
        child = None
        try:
            while batch.queue and not batch.retire:
                kind, value = batch.queue.pop(0)
                child = asyncio.create_task(self._dispose(kind, value))
                done, _ = await asyncio.wait({child}, timeout=TIMEOUT_S)
                if not done or child.cancelled() or child.exception() is not None:
                    batch.retire = True
                    break
                child = None
        except asyncio.CancelledError:
            batch.retire = True
        finally:
            try:
                if child is not None and not child.done() and not child.cancelling():
                    child.cancel()
                if batch.retire:
                    await self._retire(batch)
            finally:
                # Neither waiter cancellation nor timeout releases the hold
                # while a child is still in its actual cleanup finally.
                if child is not None:
                    await asyncio.gather(child, return_exceptions=True)
                batch.queue.clear()
                if self._batches.get(batch.instance.profile) is batch:
                    self._batches.pop(batch.instance.profile, None)
                self.host.end_activity(batch.token)

    async def settle(self):
        batches = list(self._batches.values())
        tasks = [batch.task for batch in batches]
        for batch in batches:
            batch.retire = True
            # A task canceled before its first step cannot execute finally.
            # Let an unstarted batch enter its bounded retirement path.
            if batch.started and not batch.task.cancelling():
                batch.task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)