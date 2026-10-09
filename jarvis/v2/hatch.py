"""The escape hatch — design §6.1 / D7.

A classifier refusal is not the owner's decision. When a CLI reviewer declines
a command, the owner is shown **the entire command** and may run it anyway; if
they do, Jarvis runs it itself in the task worktree and feeds the result back
to the worker session as its next user message.

Four properties, each of which is the point rather than an implementation
detail:

* **DENY is unreachable through the hatch.** Layer 1 is re-checked at the
  moment of running, not only at the moment of asking, so a `sudo`, a
  credential read or a write to Jarvis's own permission state is refused even
  with the owner's yes in hand. Approving a command is not consent to what it
  does — the same rule the `.env` refusal has carried since 2026-07-30.
* **No allowlist entry is ever minted from this path.** A command the reviewer
  refused is not one to auto-approve next time, so the ask is marked
  `allowlistable=False` and an ALWAYS answer is a plain allow.
* **The owner is asked only once the turn is over.** A worker mid-turn cannot
  receive a user message, so the hatch waits for `turn_finished` on that
  thread before asking. A decline that arrives after the turn already finished
  is asked immediately.
* **The output the worker sees is scrubbed and capped**, through v1's own
  `secrets.scrub` and v1's 20,000-character shell cap — the result of the
  command goes into a model transcript exactly as any tool result would.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading

from jarvis.tools import secrets as v1_secrets
from .approvals import ApprovalRequest, PendingApprovals
from .permissions import (ApprovalRecord, denied_command, label, log_decision,
                          _safe_command)
from .provider import Decision, UserMessage

LOG = logging.getLogger(__name__)

OUTPUT_CAP = 20_000          # v1 tools/shell.py's limit
RUN_TIMEOUT_S = 600.0

# Environment variables a worker's command must not inherit from the daemon.
# The daemon holds the owner's OpenRouter key and every integration token;
# handing them to a command the *reviewer already refused* would be the worst
# possible moment to be generous.
_STRIP_PREFIXES = ("JARVIS_", "OPENROUTER_", "ANTHROPIC_", "OPENAI_",
                   "GOOGLE_", "DISCORD_", "SPOTIFY_", "ONSHAPE_", "HF_",
                   "AWS_", "GH_", "GITHUB_", "VERCEL_", "STRIPE_")
_STRIP_EXACT = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")


def worker_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name in _STRIP_EXACT or any(name.startswith(p) for p in _STRIP_PREFIXES):
            env.pop(name, None)
    return env


@dataclass
class _Declined:
    thread_id: str
    command: str
    tool: str
    args: dict
    reason: str
    task_id: str | None = None


@dataclass
class HatchResult:
    """What the hatch did, for the tests and for a surface that wants to show it."""
    decision: str
    ran: bool = False
    message: str = ""
    output: str = ""
    refusal: str = ""


class EscapeHatch:
    """Subscribes to the daemon's bus and turns a decline into the owner's call."""

    def __init__(self, daemon, approvals: PendingApprovals):
        self.daemon = daemon
        self.approvals = approvals
        self._lock = threading.Lock()
        self._waiting: dict[str, list[_Declined]] = {}
        self._running: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._subscription = None
        self.results: list[HatchResult] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._subscription = self.daemon.bus.subscribe(
            lambda record: record.get("kind") in (
                "reviewer_declined", "turn_started", "turn_finished", "shutdown"))
        self._thread = threading.Thread(target=self._pump, name="jarvis-v2-hatch",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._subscription is not None:
            self.daemon.bus.unsubscribe(self._subscription)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                record = self._subscription.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.handle(record)
            except Exception:
                LOG.exception("Escape hatch failed on %s", record.get("kind"))

    # -- the flow ----------------------------------------------------------

    def handle(self, record: dict) -> None:
        kind = record.get("kind")
        thread_id = record.get("thread_id")
        if kind == "shutdown":
            self._stop.set()
            return
        if not thread_id:
            return
        if kind == "turn_started":
            with self._lock:
                self._running.add(thread_id)
            return
        if kind == "turn_finished":
            with self._lock:
                self._running.discard(thread_id)
                queued = self._waiting.pop(thread_id, [])
            for item in queued:
                self._offer(item)
            return
        if kind != "reviewer_declined":
            return
        data = record.get("data") or {}
        command = data.get("command") or ""
        if not isinstance(command, str) or not command.strip():
            # Nothing to offer to run. A decline with no command is still worth
            # a log line, because the worker has been refused something.
            LOG.info("Reviewer declined a non-command tool on %s; no hatch",
                     thread_id)
            return
        item = _Declined(thread_id=thread_id, command=command,
                         tool=str(data.get("tool") or ""), args=dict(data.get("args") or {}),
                         reason=str(data.get("reason") or ""))
        with self._lock:
            mid_turn = thread_id in self._running
            if mid_turn:
                self._waiting.setdefault(thread_id, []).append(item)
        if not mid_turn:
            self._offer(item)

    def _offer(self, item: _Declined) -> HatchResult:
        """Ask the owner, then run or refuse. Returns what happened."""
        task = self._task_for(item.thread_id)
        item.task_id = task.id if task is not None else None
        request = ApprovalRequest(
            tool=item.tool or "Bash",
            args=dict(item.args) or {"command": item.command},
            command=item.command,
            reason=f"reviewer declined: {item.command}",
            layer="escape-hatch",
            thread_id=item.thread_id,
            task_id=item.task_id,
            origin=label(item.task_id or "", getattr(task, "brief", "")),
            # §6.1: a command the reviewer refused is never a standing rule.
            allowlistable=False,
        )
        try:
            decision = Decision(self.approvals.ask(request))
        except Exception:
            LOG.exception("Escape-hatch approval failed; denying")
            decision = Decision.DENY

        if decision is not Decision.ALLOW:
            return self._finish(item, HatchResult(
                decision="deny",
                message=f"[owner declined to run: {item.command}]"), "system",
                "reviewer-declined-owner-declined")

        # Layer 1 again, at the moment of running. The owner's yes does not
        # reach past DENY.
        refusal = denied_command(item.command)
        if refusal:
            return self._finish(item, HatchResult(
                decision="allow", refusal=refusal,
                message=f"[refused by Jarvis rules: {refusal}]"), "system",
                "reviewer-declined-refused")

        output = self._run(item, task)
        return self._finish(item, HatchResult(
            decision="allow", ran=True, output=output,
            message=f"[owner ran: {item.command}]\n{output}"), "owner-ran",
            "reviewer-declined-owner-ran")

    def _run(self, item: _Declined, task) -> str:
        cwd = self._cwd_for(item.thread_id, task)
        try:
            # `bash -c`, never `bash -lc`: a login shell sources the owner's
            # profile, which would give the command an environment this
            # function has just finished stripping.
            proc = subprocess.run(
                ["bash", "-c", item.command], cwd=cwd, env=worker_env(),
                capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
            body = (proc.stdout or "") + (proc.stderr or "")
            body = body.rstrip() + f"\n[exit {proc.returncode}]"
        except subprocess.TimeoutExpired:
            body = f"[timed out after {int(RUN_TIMEOUT_S)}s]"
        except OSError as exc:
            body = f"[could not run: {exc}]"
        body = v1_secrets.scrub(body)
        if len(body) > OUTPUT_CAP:
            body = body[:OUTPUT_CAP] + "\n[truncated]"
        return body

    def _finish(self, item: _Declined, result: HatchResult, origin: str,
                log_event: str) -> HatchResult:
        log_decision(ApprovalRecord(
            tool=item.tool or "Bash", args_digest="", layer="escape-hatch",
            decision=log_event, reason=result.refusal or item.reason,
            command=_safe_command(item.command), thread_id=item.thread_id,
            task_id=item.task_id, provider=None))
        # Through `deliver` (2026-10-08): on a chat the result steers the turn
        # running there, or waits behind it, rather than being lost because a
        # turn happened to be running when the owner answered. A task's
        # running thread still refuses (its turns are the runner's), and that
        # is logged, as before.
        deliver = getattr(self.daemon, "deliver", None)
        try:
            (deliver if callable(deliver) else self.daemon.send)(
                item.thread_id, UserMessage(text=result.message, origin=origin))
        except Exception as exc:
            LOG.warning("Cannot deliver the escape-hatch result to %s: %s",
                        item.thread_id, exc)
        self.results.append(result)
        return result

    # -- lookups -----------------------------------------------------------

    def _task_for(self, thread_id: str):
        try:
            thread = self.daemon.stores.threads.get(thread_id)
            if thread is None or not thread.task_id:
                return None
            return self.daemon.stores.tasks.get(thread.task_id)
        except Exception:
            LOG.warning("Cannot read the task for thread %s", thread_id, exc_info=True)
            return None

    def _cwd_for(self, thread_id: str, task) -> str:
        """The task worktree, or the project root, or the daemon's cwd."""
        if task is not None and task.worktree and Path(task.worktree).is_dir():
            return task.worktree
        if task is not None and task.root and Path(task.root).is_dir():
            return task.root                    # pinned when it started (decisions B5)
        try:
            thread = self.daemon.stores.threads.get(thread_id)
            if thread is not None:
                project = self.daemon.stores.projects.get(thread.project_id)
                if project is not None and Path(project.root).is_dir():
                    return project.root
        except Exception:
            LOG.warning("Cannot resolve a cwd for %s", thread_id, exc_info=True)
        return os.getcwd()
