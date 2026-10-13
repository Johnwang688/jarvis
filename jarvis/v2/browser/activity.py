"""Short, synchronous ownership barrier for automatic Browser restarts.

The host owns these tokens. This module does no IO and makes no permission
choices. Parent activities span review/card work; child activities span actual
host-coroutine cleanup, even after a waiting Future has been canceled. Page
holds can invalidate an uncommitted restart. The broker may call commit_restart
under its lock: broker -> this lock, with no external callbacks in either.
"""
from __future__ import annotations
from dataclasses import dataclass
import threading

MAX_PROFILES = 9  # eight persistent profiles and the one Private engine
MAX_ACTIVITIES = 128
BUSY = "the Browser is changing its sound setting; try again when it finishes"
ENDED = "this Browser instance is no longer current"


class ActivityRefused(Exception):
    """Fixed safe text only."""


@dataclass(frozen=True, eq=False)
class ActivityToken:
    profile: str
    instance: int


@dataclass(frozen=True, eq=False)
class RestartToken:
    profile: str
    instance: int
    desired: int


@dataclass
class _Restart:
    token: RestartToken
    committed: bool = False


class BrowserActivity:
    """Tokens are identity capabilities, not values a caller can reconstruct.

    Never hold _lock while calling the host, broker, registry or DriveState.
    The host's async profile lock may surround these nonblocking methods, but
    a failed restart admission must release that profile lock before waiting.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._closed = False
        self._desired = 0
        self._instances: dict[str, int] = {}
        self._active: dict[ActivityToken, tuple[str, int]] = {}
        self._restarts: dict[str, _Restart] = {}
        self._cleanup: dict[str, ActivityToken] = {}

    def desired(self, generation: int) -> None:
        if type(generation) is not int or generation < 0:
            raise ValueError("invalid sound generation")
        with self._lock:
            if generation <= self._desired:
                return
            self._desired = generation
            # A committed replacement finishes before a newer request applies.
            for profile, restart in list(self._restarts.items()):
                if not restart.committed:
                    self._restarts.pop(profile, None)

    def opened(self, profile: str, instance: int, restart: RestartToken | None = None) -> bool:
        if not isinstance(profile, str) or not profile or type(instance) is not int or instance <= 0:
            raise ValueError("invalid Browser instance")
        with self._lock:
            if self._closed:
                return False
            prior = self._instances.get(profile)
            pending = self._restarts.get(profile)
            if restart is not None:
                if (pending is None or pending.token is not restart or not pending.committed
                        or prior != restart.instance or instance == prior):
                    return False
            elif pending is not None or (prior is not None and prior != instance):
                return False
            if prior is None and len(self._instances) >= MAX_PROFILES:
                return False
            # A stale canceled coroutine must settle before any new generation.
            if any(p == profile for p, _ in self._active.values()):
                return False
            self._instances[profile] = instance
            return True

    def _current(self, profile: str, instance: int) -> None:
        if self._closed or self._instances.get(profile) != instance:
            raise ActivityRefused(ENDED)

    def enter(self, profile: str, instance: int) -> ActivityToken:
        with self._lock:
            self._current(profile, instance)
            if profile in self._restarts:
                raise ActivityRefused(BUSY)
            return self._enter(profile, instance)

    def observed_hold(self, profile: str, instance: int) -> ActivityToken:
        """A current page raised a dialog/chooser before replacement committed."""
        with self._lock:
            self._current(profile, instance)
            pending = self._restarts.get(profile)
            if pending is not None:
                if pending.committed:
                    raise ActivityRefused(BUSY)
                self._restarts.pop(profile, None)
            return self._enter(profile, instance)

    def cleanup_hold(self, profile: str, instance: int) -> ActivityToken | None:
        """One reserved overflow-cleanup token per live instance (at most nine).

        Only refused dialog/download cleanup uses this escape from the ordinary
        activity cap. A committed replacement already owns old-context teardown.
        """
        with self._lock:
            try:
                self._current(profile, instance)
            except ActivityRefused:
                return None
            pending = self._restarts.get(profile)
            if pending is not None:
                if pending.committed:
                    return None
                self._restarts.pop(profile, None)
            prior = self._cleanup.get(profile)
            if prior is not None:
                return prior if prior.instance == instance else None
            token = ActivityToken(profile, instance)
            self._cleanup[profile] = token
            self._active[token] = (profile, instance)
            return token

    def _enter(self, profile: str, instance: int) -> ActivityToken:
        if len(self._active) >= MAX_ACTIVITIES:
            raise ActivityRefused("too much Browser work is still settling")
        token = ActivityToken(profile, instance)
        self._active[token] = (profile, instance)
        return token

    def leave(self, token: ActivityToken) -> None:
        with self._lock:
            self._active.pop(token, None)
            if self._cleanup.get(token.profile) is token:
                self._cleanup.pop(token.profile, None)

    def try_restart(self, profile: str, instance: int, desired: int) -> RestartToken | None:
        with self._lock:
            if (self._closed or self._instances.get(profile) != instance or desired != self._desired
                    or profile in self._restarts
                    or any(p == profile for p, _ in self._active.values())):
                return None
            token = RestartToken(profile, instance, desired)
            self._restarts[profile] = _Restart(token)
            return token

    def commit_restart(self, token: RestartToken) -> bool:
        """Called once, synchronously, under the broker's idle-commit lock."""
        with self._lock:
            found = self._restarts.get(token.profile)
            if (self._closed or found is None or found.token is not token or found.committed
                    or self._instances.get(token.profile) != token.instance
                    or token.desired != self._desired
                    or any(p == token.profile for p, _ in self._active.values())):
                return False
            found.committed = True
            return True

    def finish_restart(self, token: RestartToken) -> None:
        with self._lock:
            found = self._restarts.get(token.profile)
            if found is not None and found.token is token:
                self._restarts.pop(token.profile, None)

    def closed(self, profile: str, instance: int) -> None:
        """Owner close cancels adoption, but old activities remain until cleanup."""
        with self._lock:
            if self._instances.get(profile) != instance:
                return
            self._instances.pop(profile, None)
            self._restarts.pop(profile, None)

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            self._instances.clear()
            self._restarts.clear()
            # Retain active tokens until their actual finally releases them.

    def counts(self, profile: str) -> tuple[int, bool, bool]:
        with self._lock:
            restart = self._restarts.get(profile)
            return (sum(p == profile for p, _ in self._active.values()), restart is not None,
                    bool(restart and restart.committed))