"""Owner Browser sound preference, curated launch and fixed status.

No URL, title, page text or source permission belongs here. The exact-instance
activity barrier owns admission; this coordinator never changes agent policy.
"""
from __future__ import annotations
import threading
from . import audio


class BrowserSound:
    def __init__(self, host, *, probe=audio.availability):
        self.host = host
        self.probe = probe
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._desired = host._settings.value("sound")
        self._generation = 0
        self._available = probe()
        self._queued = False
        self._last_notice = None

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
                self._generation += 1
                generation = self._generation
                self._available = available
            self.host.activity.desired(generation)
        self.wake()

    def preference(self):
        self.sync()
        with self._lock:
            return self._desired, self._generation, self._available

    def launch(self, args, env):
        self.sync()
        available = self.probe()
        with self._lock:
            self._available = available
            desired = self._desired
        return audio.launch_audio(args, env, desired, available)

    def status(self):
        desired, generation, available = self.preference()
        return {"desired": desired, "generation": generation,
                "available": available.routing,
                "reason": "unavailable" if desired and not available.routing else None}

    def instance_status(self, instance):
        desired, _, available = self.preference()
        state = instance.audio
        if state.synchronized(desired):
            phase, reason = "ready", None
        elif instance.profile == "private":
            phase, reason = "pending", "private"
        elif desired and not available.routing:
            phase, reason = "pending", "unavailable"
        else:
            phase, reason = "pending", "busy"
        return {"applied": state.applied, "generation": instance.generation,
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

    def _reconcile(self):
        with self._lock:
            self._queued = False
        if self.host._stopped:
            return
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