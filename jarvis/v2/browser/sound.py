"""Owner Browser sound preference, curated launch and fixed status.

No URL, title, page text or source permission belongs here. The exact-instance
activity barrier owns admission; this coordinator never changes agent policy.
"""
from __future__ import annotations
import asyncio
import threading
from . import audio


class BrowserSound:
    def __init__(self, host, *, probe=audio.availability):
        self.host = host
        self.probe = probe
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._desired = host._settings.value("sound")
        self._requests = set()  # owner gestures only; never persisted
        self._recoveries = {}  # profile -> exact instance, binding, owner generation
        self._retirements = {}  # revoked audible overflow -> exact instance/binding
        self._generation = 0
        self._available = probe()
        self._queued = False
        self._last_notice = None
        from .sound_restart import SoundRestarts
        self.restarts = SoundRestarts(self)
        self.pulse = audio.Pulse()
        self._mute_tasks = {}
        self._mute_reason = {}

    def sync(self, *, explicit=False):
        # Serialize preference reads and capability refreshes. A slower older
        # refresh must never publish a stale desired value over a newer one.
        with self._sync_lock:
            desired = self.host._settings.value("sound")
            with self._lock:
                changed = desired != self._desired
            if not changed and not explicit:
                return
            available = self.probe()
            with self._lock:
                self._desired = desired
                if not desired:
                    self._requests.clear()
                    self._recoveries.clear()
                self._generation += 1
                generation = self._generation
                self._available = available
            self.host.activity.desired(generation)
        self.wake()

    def preference(self, profile=None):
        self.sync()
        with self._lock:
            return (self._desired if profile is None else self._desired and profile in self._requests), self._generation, self._available

    def launch(self, args, env, *, profile=None, restart_token=None):
        """Only an exact committed sound replacement may launch audible."""
        self.sync()
        available = self.probe()
        with self._lock:
            self._available = available
            desired = bool(self._desired and profile in self._requests
                and restart_token is not None
                and getattr(restart_token, "desired", None) == self._generation)
        authorized = (desired and self.host.activity.restart_current(
            restart_token, profile, restart_token.instance))
        return audio.launch_audio(args, env, bool(authorized), available)

    def clear(self, profile):
        with self._lock:
            self._recoveries.pop(profile, None)
            if profile not in self._requests:
                return
            self._requests.discard(profile)
            self._generation += 1
            generation = self._generation
        self.host.activity.desired(generation)
        self.wake()

    async def change(self, tab_id, on):
        from .host import BrowserRefused
        if type(on) is not bool:
            raise BrowserRefused(400, "on must be a boolean")
        self.host._refuse_if_held()
        tab = self.host._tabs.get(tab_id)
        descriptor = self.restarts.descriptors.get(tab_id)
        profile = tab.profile if tab is not None else descriptor.profile if descriptor else None
        instance = self.host._instances.get(profile)
        if profile is None or instance is None:
            raise BrowserRefused(404, "no such Browser tab")
        self.sync()
        if on and profile == "private":
            raise BrowserRefused(409, "Close Private panes to allow sound")
        if on and not self.preference()[0]:
            raise BrowserRefused(409, "Browser sound is disabled in Settings")
        available = self.probe()
        self.sync()
        with self._lock:
            if on and not self._desired:
                raise BrowserRefused(409, "Browser sound is disabled in Settings")
            if on:
                self._requests.add(profile)
            else:
                self._requests.discard(profile)
            self._generation += 1
            generation = self._generation
            self._available = available
            self._recoveries[profile] = (instance, instance.generation, generation)
        self.host.activity.desired(generation)
        # Keep the failed identity visible until a real replacement commits.
        # The new desired generation admits one explicit recovery attempt.
        self.wake()
        self.publish()
        return {"on": on, "profile": profile, "generation": generation}

    def status(self):
        desired, generation, available = self.preference()
        return {"desired": desired, "generation": generation,
                "available": available.routing,
                "reason": "unavailable" if desired and not available.routing else None}

    def instance_status(self, instance):
        desired, _, available = self.preference(instance.profile)
        state = instance.audio
        if instance.profile in self.restarts.failed:
            phase, reason = "failed", "restart_failed"
        elif instance.profile in self.restarts.restarting:
            phase, reason = "restarting", None
        elif state.synchronized(desired) and not self.retirement_current(instance):
            phase, reason = "ready", None
        elif instance.profile == "private":
            phase, reason = "pending", "private"
        elif desired and not available.routing:
            phase, reason = "pending", "unavailable"
        else:
            reason = "busy"
            if not desired and state.applied:
                if not available.immediate_mute:
                    reason = "mute_unavailable"
                elif self._mute_reason.get(instance.profile) == audio.MUTE_UNCERTAIN:
                    reason = "mute_uncertain"
            phase = "pending"
        return {"requested": desired, "applied": state.applied, "generation": instance.generation,
                "phase": phase, "reason": reason}

    def wake(self):
        """Coalesce relevant changes without starting a cold host or a timer."""
        loop = self.host._loop
        if loop is None or loop.is_closed() or self.host._stopped:
            return
        with self._lock:
            if self._queued:
                return
            self._queued = True
        try:
            loop.call_soon_threadsafe(self._reconcile)
        except RuntimeError:
            with self._lock:
                self._queued = False

    def retire(self, instance):
        # Only the host's revoked-audible overflow path admits this reason.
        with self._lock:
            self._retirements[instance.profile] = (instance, instance.generation)
        self.wake()

    def retirement_current(self, instance):
        with self._lock:
            value = self._retirements.get(instance.profile)
            return (value is not None and value[0] is instance
                    and value[1] == instance.generation)

    def retirement_finished(self, instance):
        with self._lock:
            value = self._retirements.get(instance.profile)
            if (value is not None and value[0] is instance
                    and value[1] == instance.generation):
                self._retirements.pop(instance.profile, None)

    def recovery(self, instance):
        with self._lock:
            recovery = self._recoveries.get(instance.profile)
            if (recovery is not None and recovery[0] is instance
                    and recovery[1] == instance.generation):
                return recovery
        return None

    def recovery_current(self, instance):
        return self.recovery(instance) is not None

    def recovery_committed(self, instance, generation):
        with self._lock:
            recovery = self._recoveries.get(instance.profile)
            if (recovery is not None and recovery[0] is instance
                    and recovery[1] == instance.generation and recovery[2] <= generation):
                self._recoveries.pop(instance.profile, None)

    def _reconcile(self):
        with self._lock:
            self._queued = False
        if self.host._stopped:
            return
        desired, generation, available = self.preference()
        for instance in list(self.host._instances.values()):
            desired, generation, available = self.preference(instance.profile)
            if instance.profile in self.restarts.failed and not self.recovery_current(instance):
                continue
            retired = self.retirement_current(instance)
            if ((instance.audio.synchronized(desired) and instance.profile not in self.restarts.failed and not retired)
                    or (instance.closing and instance.profile not in self.restarts.failed and not retired)):
                # Failed placeholders are retried only by an explicit gesture.
                continue
            if not desired and instance.audio.applied:
                self._mute(instance, generation, available)
            if instance.profile != "private" and (not desired or available.routing):
                if instance.profile not in self._mute_tasks:
                    self.restarts.consider(instance, generation)
        self.publish()

    def publish(self):
        self.host._publish_snapshot()
        status = self.status()
        rows = tuple((i.profile, tuple(self.instance_status(i).items()))
                     for i in self.host._instances.values())
        notice = (tuple(status.items()), rows)
        if notice == self._last_notice:
            return
        self._last_notice = notice
        bus = getattr(self.host._daemon, "bus", None)
        if bus is not None:
            bus.publish({"kind": "browser_sound_changed", "data": {"generation": status["generation"]}})

    def _mute(self, instance, generation, available):
        profile = instance.profile
        if profile in self._mute_tasks or not available.immediate_mute:
            return
        def current():
            with self._lock:
                requested = (not self._desired or profile not in self._requests) and self._generation == generation
            return (requested and not self.host._stopped
                    and (not instance.closing or self.retirement_current(instance))
                    and self.host._instances.get(profile) is instance)
        async def run():
            # The actual executor future settles before structural replacement.
            # Cancellation of its waiter must not let the native child overlap.
            def work():
                from .host import chrome_pids
                try:
                    candidates = []
                    for pid in chrome_pids(instance.folder):
                        try:
                            candidates.append(audio.OwnedChrome.pin(pid, instance.folder))
                        except audio.AudioRefused:
                            pass
                    if len(candidates) != 1:
                        return audio.MuteResult(0, False, audio.MUTE_UNCERTAIN)
                    return self.pulse.mute_owned(candidates[0], instance.audio, current)
                except Exception:
                    return audio.MuteResult(0, False, audio.MUTE_UNCERTAIN)
            future = asyncio.get_running_loop().run_in_executor(None, work)
            cancelled = False
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    cancelled = True
            result = future.result()
            if current():
                self._mute_reason[profile] = result.reason
                self.restarts.consider(instance, generation)
            if cancelled:
                raise asyncio.CancelledError()
        task = asyncio.create_task(run())
        self._mute_tasks[profile] = task
        def finished(done):
            if self._mute_tasks.get(profile) is done:
                self._mute_tasks.pop(profile, None)
            if not done.cancelled():
                done.exception()
            if self.host._stopped:
                return
            desired_now, generation_now, route_now = self.preference(profile)
            if self.host._instances.get(profile) is instance and (not instance.closing or self.retirement_current(instance)):
                if instance.profile != "private" and (not desired_now or route_now.routing):
                    self.restarts.consider(instance, generation_now)
                if not desired_now and instance.audio.applied and generation_now == generation:
                    # Bounded periodic checks discover streams created while
                    # this exact ON instance waits for activity to settle.
                    asyncio.get_running_loop().call_later(1.0, self.wake)
            self.publish()
        task.add_done_callback(finished)

    async def retry(self, tab_id):
        descriptor = self.restarts.descriptors.get(tab_id)
        tab = self.host._tabs.get(tab_id)
        profile = descriptor.profile if descriptor is not None else tab.profile if tab else None
        if profile is None or profile == "private":
            from .host import BrowserRefused
            raise BrowserRefused(404, "no such persistent Browser tab")
        self.sync(explicit=True)
        instance = self.host._instances.get(profile)
        admitted = False
        if instance is not None:
            generation = self.preference()[1]
            with self._lock:
                # A later master change already withdrew this captured intent.
                if generation == self._generation:
                    self._recoveries[profile] = (instance, instance.generation, generation)
                    admitted = True
            if admitted:
                self.restarts.consider(instance, generation)
        return {"tab_id": tab_id, "retrying": admitted}

    async def settle(self):
        tasks = list(self._mute_tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.restarts.settle()
