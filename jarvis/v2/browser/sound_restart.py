"""Persistent sound replacement under the host/broker activity boundary.

All methods except status access run on the Browser event loop. Descriptors are
bounded by the existing tab cap and never persisted or emitted as events.
"""
from __future__ import annotations
import asyncio
from .sound_restore import describe, target

class SoundRestarts:
    def __init__(self, sound):
        self.sound, self.host = sound, sound.host
        self.tasks = {}
        self.descriptors = {}
        self.failed = {}  # profile -> (exact instance, attempted desired generation)
        self.restarting = set()

    def reserved(self):
        return sum(tab_id not in self.host._tabs for tab_id in self.descriptors)

    def rows(self):
        return [{'tab_id': d.tab_id, 'profile': d.profile, 'ctx': None, 'url': d.url,
                 'title': '', 'opener': d.opener, 'shown': False}
                for d in self.descriptors.values() if d.tab_id not in self.host._tabs]

    def cancel_tab(self, tab_id):
        descriptor = self.descriptors.pop(tab_id, None)
        if descriptor is None:
            return None
        return descriptor.profile

    def cancel_profile(self, profile):
        for descriptor in list(self.descriptors.values()):
            if descriptor.profile == profile:
                self.cancel_tab(descriptor.tab_id)
        self.failed.pop(profile, None)
        with self.sound._lock:
            self.sound._recoveries.pop(profile, None)
            self.sound._retirements.pop(profile, None)

    def consider(self, instance, generation):
        profile = instance.profile
        if profile == 'private' or profile in self.tasks or self.host._stopped:
            return
        failed = self.failed.get(profile)
        if failed is not None and failed[0] is instance and failed[1] == generation:
            return
        task = asyncio.create_task(self._run(instance, generation))
        self.tasks[profile] = task
        def finished(done):
            if self.tasks.get(profile) is done:
                self.tasks.pop(profile, None)
            # Consuming a busy result must not enqueue itself in a retry loop.
            if not done.cancelled():
                done.exception()
            current = self.host._instances.get(profile)
            recovery = self.sound.recovery(current) if current is not None else None
            if recovery is not None:
                if recovery[2] != generation:
                    self.sound.wake()
            self.sound.publish()
        task.add_done_callback(finished)

    async def _run(self, instance, generation):
        host, profile = self.host, instance.profile
        token = None
        committed = False
        replacement = None
        prepared = []
        async with host._launching.setdefault(profile, asyncio.Lock()):
            if host._instances.get(profile) is not instance or host._stopped:
                return
            desired, latest, available = self.sound.preference(profile)
            if latest != generation or (desired and not available.routing):
                return
            if (instance.audio.synchronized(desired) and not self.sound.retirement_current(instance)) and not any(
                    d.profile == profile for d in self.descriptors.values()):
                return
            # Sign-in is privileged owner attention, even when no modal exists.
            tabs = [tab for tab in host._tabs.values() if tab.profile == profile]
            if any(tab.signin for tab in tabs) or instance.audio.snapshot()[2]:
                return
            token = host.activity.try_restart(profile, instance.generation, generation)
            if token is None:
                return
            try:
                with host._lock:
                    leases = {port: frozenset(ids) for port, ids in host._port_owners.items()}
                prepared = [self.descriptors.get(tab.id) or describe(tab, refused=host.refused_ports(),
                    owns_port=lambda port, tid: tid in leases.get(port, ()),
                    local_servers=host._settings.value('local_servers')) for tab in tabs]
                prepared += [d for d in self.descriptors.values()
                             if d.profile == profile and d.tab_id not in host._tabs]
                broker = getattr(host._daemon, 'approvals', None)
                commit = getattr(broker, 'run_if_idle', None)
                # No fallback to a pending() snapshot: admission must be atomic.
                if commit is None or not commit(lambda: host.activity.commit_restart(token)):
                    return
                committed = True
                self.restarting.add(profile)
                self.sound.recovery_committed(instance, generation)
                self.sound.retirement_finished(instance)
                self.failed.pop(profile, None)
                # No await between commitment and detaching old page identity.
                instance.closing = True
                for descriptor in prepared:
                    self.descriptors[descriptor.tab_id] = descriptor
                for tab in tabs:
                    host._release(tab, 'sound setting changed')
                    host._tabs.pop(tab.id, None)
                host._publish_snapshot()
                self.sound.publish()
                await host._close_instance(profile, preserve_activity=True)
                if host._stopped:
                    return
                # No replacement may overlap an old owned Chrome still alive.
                from .host import chrome_pids
                if await asyncio.to_thread(chrome_pids, instance.folder):
                    raise RuntimeError('old browser cleanup is unfinished')
                replacement = await host._launch(profile, restart_token=token)
                host._next_instance_generation += 1
                replacement.generation = host._next_instance_generation
                replacement.restoring = True
                if not host.activity.opened(profile, replacement.generation, token):
                    raise RuntimeError('replacement admission changed')
                host._instances[profile] = replacement
                for descriptor in list(self.descriptors.values()):
                    if descriptor.profile != profile or not self._current(descriptor):
                        continue
                    await self._restore(replacement, descriptor, token)
                if replacement.popup_cleanup:
                    await asyncio.gather(*replacement.popup_cleanup)
                if replacement.closing:
                    raise RuntimeError('replacement popup cleanup exceeded its bound')
                # Drop only after every page succeeded; a later failure must
                # retain earlier logical tabs as bounded retry placeholders.
                for descriptor in prepared:
                    if self._current(descriptor):
                        self.descriptors.pop(descriptor.tab_id, None)
                self.failed.pop(profile, None)
            except asyncio.CancelledError:
                if committed:
                    await self._failed(instance, replacement, generation)
                raise
            except Exception:
                if committed:
                    await self._failed(instance, replacement, generation)
            finally:
                if replacement is not None:
                    # Keep refusal active until every owned cleanup settles.
                    # Page-driven popups during replacement were never adopted.
                    for page in list(replacement.unadopted):
                        if not any(tab.page is page for tab in host._tabs.values()):
                            await host._quietly(page.close())
                    replacement.unadopted.clear()
                    if replacement.popup_cleanup:
                        await asyncio.gather(*replacement.popup_cleanup, return_exceptions=True)
                    replacement.restoring = False
                if token is not None:
                    host.activity.finish_restart(token)
                self.restarting.discard(profile)
                host._publish_snapshot()
                if committed and profile not in self.failed and not host._stopped:
                    current = host._instances.get(profile)
                    if current is not None:
                        host._idle_if_empty(current)
                    # A newer preference may have arrived after commitment.
                    if self.sound.preference()[1] != generation:
                        self.sound.wake()

    def _current(self, descriptor):
        return (not self.host._stopped and self.descriptors.get(descriptor.tab_id) is descriptor)

    async def _restore(self, instance, descriptor, token):
        host = self.host
        spare = [page for page in instance.context.pages
                 if page not in instance.refused_pages
                 and not any(tab.page is page for tab in host._tabs.values())]
        if spare:
            page = spare[0]
        else:
            instance.restore_allocation = True
            try:
                page = await instance.context.new_page()
            finally:
                instance.restore_allocation = False
        if not self._current(descriptor):
            await host._quietly(page.close())
            return
        tab = host._adopt(instance, page, None, logical_id=descriptor.tab_id, restart_token=token)
        if tab is None:
            raise RuntimeError('replacement page admission changed')
        tab.opener = descriptor.opener
        url, lease, blanked = target(descriptor, refused=host.refused_ports(),
                                     local_servers=host._settings.value('local_servers'))
        tab.loopback_port = lease
        host._sync_ports(tab)
        try:
            # This is not an owner address submission and creates no lease.
            result = await host._goto(tab, url, owner=False, restart_token=token)
            if not self._current(descriptor):
                host._release(tab, 'closed')
                host._tabs.pop(tab.id, None)
                await host._quietly(page.close())
                return
            if not result.get('loaded'):
                raise RuntimeError('replacement navigation failed')
            if not tab.pane_ready:
                # A refused redirect cannot leave a document whose queued
                # ordinary guard was refused by the exclusive restart barrier.
                await page.goto('about:blank', wait_until='commit', timeout=5000)
                blanked = True
                if not self._current(descriptor):
                    host._release(tab, 'closed')
                    host._tabs.pop(tab.id, None)
                    await host._quietly(page.close())
                    return
            if blanked and page.url == 'about:blank':
                await page.set_content(host.sound_explanation(), timeout=5000)
            if not self._current(descriptor):
                host._release(tab, 'closed')
                host._tabs.pop(tab.id, None)
                await host._quietly(page.close())
        except Exception:
            # One failed logical placeholder; no usable page/stream survives.
            host._release(tab, 'sound restart failed')
            host._tabs.pop(tab.id, None)
            await host._quietly(page.close())
            raise

    async def _failed(self, old, replacement, generation):
        host = self.host
        if replacement is not None:
            # Do not target a concurrently installed same-profile instance.
            if host._instances.get(old.profile) is replacement:
                await host._close_instance(old.profile, preserve_activity=True)
            else:
                await host._quietly(replacement.context.close())
        # Preserve a bounded failed status/Retry identity without a live page.
        with self.sound._lock:
            recovery = self.sound._recoveries.get(old.profile)
        pending = (recovery is not None and recovery[2] > generation
                   and (recovery[0] is old or recovery[0] is replacement)
                   and recovery[1] == recovery[0].generation)
        prior = replacement.generation if replacement is not None else old.generation
        host.activity.closed(old.profile, prior)
        if not host._stopped and any(d.profile == old.profile for d in self.descriptors.values()):
            old.closing = True
            host._next_instance_generation += 1
            old.generation = host._next_instance_generation
            host._instances[old.profile] = old
            host.activity.opened(old.profile, old.generation)
            self.failed[old.profile] = (old, generation)
            with self.sound._lock:
                if pending and self.sound._recoveries.get(old.profile) is recovery:
                    self.sound._recoveries[old.profile] = (old, old.generation, recovery[2])

    async def settle(self):
        self.descriptors.clear()
        self.failed.clear()
        with self.sound._lock:
            self.sound._recoveries.clear()
            self.sound._retirements.clear()
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
