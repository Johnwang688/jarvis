"""The Discord surface as one object (plan §1, PR A): REST, the router with its
interaction router, and the update poster — started and stopped in the one
order that loses nothing.

**Start.** `daemon.start` (the caller's), then the Reporter subscribes, then
the channel linker (B1) starts, then `runner.serve()`, then the gateway. The Reporter has to be on the bus before
the runner's recovery publishes its first snapshot, or a task re-admitted at
boot changes phase with nobody listening; the gateway comes last so no verb
reaches a runner that is not serving yet. `serve()` is idempotent, so the
daemon's own call to it afterwards is harmless.

**Stop.** The gateway first, so no verb arrives at a runner that is stopping;
then the runner, so its last snapshots are published; then the Reporter, which
drains them and sends due card edits within 2 s; then the channel linker
(B1); then REST. The hatch and the
daemon are the caller's to stop after this.

Both are idempotent: a second `start()` or `stop()` does nothing.
"""
from __future__ import annotations

import logging
import threading

LOG = logging.getLogger(__name__)


class DiscordSurface:
    def __init__(self, daemon, *, control=None, rest=None, router=None,
                 listener_factory=None, dm_channel=None, announce=None,
                 sync_commands: bool = True, reconcile: bool = True):
        from .gateway import DiscordRouter
        from .rest import DiscordRest

        self.daemon = daemon
        self.rest = rest if rest is not None else DiscordRest()
        self._own_rest = rest is None
        self._reconcile = reconcile
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False
        kwargs = {}
        if announce is not None:
            kwargs["announce"] = announce
        if dm_channel is not None:
            kwargs["dm_channel"] = dm_channel
        self.router = DiscordRouter(daemon, daemon.stores, router, daemon.approvals, control,
                                    self.rest, listener_factory,
                                    sync_commands=sync_commands, **kwargs)
        self.reporter = None
        self.linker = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "DiscordSurface":
        from .reporter import Reporter

        with self._lock:
            if self._started or self._stopped:
                return self
            self._started = True
        # 1. Subscribe before anything can publish a task snapshot.
        self.reporter = Reporter(self.daemon.stores, self.rest, self.daemon.bus,
                                 dm_channel=self.router._owner_dm,
                                 reconcile=self._reconcile)
        # 1b. The channel linker (B1), after the Reporter so the Reporter
        # hears the Inbox being linked to #ungrouped and every later link.
        from .linker import ChannelLinker
        self.linker = ChannelLinker(self.daemon, self.rest, approvals=self.daemon.approvals)
        self.linker.start()
        # B2's slash handlers (`/project`, `/channel`) reach it through the router.
        self.router.linker = self.linker
        # 2. The runner's recovery publishes now, and the Reporter hears it.
        runner = getattr(self.daemon, "runner", None)
        if runner is not None and hasattr(runner, "serve"):
            runner.serve()
        # 3. The gateway last.
        self.router.start()
        return self

    def stop(self, *, runner: bool = True) -> None:
        """`runner=False` leaves the task runner serving (a surface that failed
        to start must not take task execution down with it)."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        try:
            self.router.stop()
        except Exception as exc:
            LOG.warning("Discord gateway did not stop cleanly (%s)", type(exc).__name__)
        task_runner = getattr(self.daemon, "runner", None) if runner else None
        if task_runner is not None and hasattr(task_runner, "stop"):
            try:
                task_runner.stop()
            except Exception as exc:
                LOG.warning("Task runner did not stop cleanly (%s)", type(exc).__name__)
        if self.reporter is not None:
            self.reporter.close(flush=True)
        if self.linker is not None:
            self.linker.close()
        if self._own_rest:
            self.rest.close()

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        """For `GET /discord`: the router's (connection, commands) plus the
        Reporter's health. States, counts and times only."""
        status = dict(self.router.status())
        status["reporter"] = self.reporter.status() if self.reporter is not None else None
        if self.linker is not None:
            status.update(self.linker.status())     # guild, linker, permissions (B1)
        return status
