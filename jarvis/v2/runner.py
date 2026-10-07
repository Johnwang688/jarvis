"""TaskRunner — the §10.1 lifecycle, one worker thread per running task.

This is the module that turns every merged piece into a task that runs end to
end: the router picks providers, the daemon owns sessions, the stores own
state, the ledger owns spend, `roles.py` owns the contracts. The runner owns
only the *sequence*, and four rules keep that honest.

**The state machine is driven off disk, never off a local variable.** `_drive`
reads the task's state each time round and dispatches on it, so a resume after
a daemon restart — or after an orchestrator was moved to the other provider —
re-enters exactly where it left off with no replayed transcript. Everything
the next step needs is in `task.spec`, `task.plan`, `task.status` and the
journal, by construction (§10.3).

**The runner maintains the status record; the model never writes it.** Phase,
step, elapsed, cost, last tool and last file come from the event stream. Model
prose enters at exactly four points — spec, plan, questions, report — each
through a parsed JSON shape, and a reply that does not parse is re-asked once
and then blocks the task. Nothing is inferred from prose.

**Every task write is published.** WP10a's Reporter is a bus subscriber, so a
write nobody published is a status embed that silently goes stale; `_save`,
`_transition` and every call into `worktrees.ensure` / `router.resolve` (which
write the task themselves) are followed by a `task_status_changed` snapshot.

**Cancel and ceilings land at a boundary, never inside a turn** (v1 invariant
3). Both set a flag, interrupt the running turn through the daemon, and are
acted on once the provider's `TURN_FINISHED` has come back — so no tool call
is cut in half and the transcript the next step resumes from is whole.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable

from .control import ControlError, TaskControl
from .daemon import DaemonError
from .model import (OpenQuestion, ProviderName, Report, Role, RoutingDecision, Spec,
                    Task, TaskState, TERMINAL_STATES, to_json, utcnow)
from .provider import Brief, BriefRefused, UserMessage
from .router import RoutingBlocked, model_settings
from .stores import StoreError
from . import roles, worktrees

LOG = logging.getLogger(__name__)

MAX_ACTIVE = 3                  # concurrent running tasks; the rest queue in order
MAX_STEPS = 40                  # runaway guard on the implementer loop, not a work limit
TURN_TIMEOUT = 45 * 60          # one provider turn; a hung provider must not wedge a task
SEND_TIMEOUT = 30               # waiting out someone else's turn on the same thread
DIFF_CAP = 20_000               # what the reviewer is shown of the branch diff
EXCERPT = 800                   # model text quoted into a journal or a blocked reason
_FILE_ARGS = ("file_path", "path", "notebook_path", "filename", "file")
# BLOCKED's outgoing edges that are phases to re-enter (model.TRANSITIONS).
RESUMABLE = frozenset({TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING})


class _Cancelled(Exception):
    """The owner cancelled; act at the boundary."""


class _Blocked(Exception):
    """The task cannot continue without the owner; carries the sentence they see."""


class _OrchestratorDown(Exception):
    """The orchestrator's provider failed mid-task (§8.4); the owner picks the fallback."""


@dataclass
class TurnResult:
    text: str = ""
    stop: str = "end"
    fatal: str | None = None


@dataclass
class _Run:
    """Per-task runner state. Outlives one worker thread so a steer queued while
    the task is parked is still delivered when it next runs."""

    task_id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    steers: list[tuple[str, bool]] = field(default_factory=list)
    worker: threading.Thread | None = None
    current_thread: str | None = None
    pending_instruction: str | None = None
    restart_provider: ProviderName | None = None
    ceiling: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def take_steers(self) -> list[tuple[str, bool]]:
        with self.lock:
            items, self.steers = list(self.steers), []
            return items


def _cap(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n[… {len(text) - limit} more characters]"


def _bullets(items) -> str:
    return "\n".join(f"- {item}" for item in items) or "- (none stated)"


class TaskRunner(TaskControl):
    """Implements `control.TaskControl`. Constructed with the daemon it drives."""

    def __init__(self, daemon, router, *, ledger=None, approvals=None, bus=None,
                 stores=None, max_active: int = MAX_ACTIVE,
                 turn_timeout: float = TURN_TIMEOUT, clock: Callable[[], float] = time.monotonic):
        self.daemon = daemon
        self.stores = stores if stores is not None else daemon.stores
        self.router = router
        self.ledger = ledger if ledger is not None else router.ledger
        self.approvals = approvals if approvals is not None else getattr(daemon, "approvals", None)
        self.bus = bus if bus is not None else daemon.bus
        self.max_active = max(1, int(max_active))
        self.turn_timeout = turn_timeout
        self.clock = clock
        self._lock = threading.RLock()
        self._runs: dict[str, _Run] = {}
        self._queue: deque[str] = deque()
        self._active: set[str] = set()
        self._proposed: set[str] = set()
        self._dirty: dict[str, Task] = {}
        self.proposal_replies: dict[str, str] = {}
        self._stop = threading.Event()
        self._pump_thread: threading.Thread | None = None
        self._subscription = None

    # -- service lifecycle -------------------------------------------------

    def serve(self, *, recover: bool = True) -> None:
        """Start the proposal pump. Named `serve`, not `start`: `start(task_id)`
        is a TaskControl verb and the two must not share a name."""
        with self._lock:
            if self._pump_thread is not None:
                return
            self._subscription = self.bus.subscribe(
                lambda record: record.get("kind") in ("turn_finished", "shutdown"))
            self._pump_thread = threading.Thread(target=self._pump_bus,
                                                 name="jarvis-v2-runner", daemon=True)
            self._pump_thread.start()
        if recover:
            self.recover()

    def recover(self) -> None:
        """Re-admit tasks a previous process left mid-flight. INTAKE is never
        admitted here: an unstarted task is one nobody has said go to yet."""
        try:
            tasks = self.stores.tasks.list()
        except StoreError:
            LOG.warning("Cannot list tasks to recover", exc_info=True)
            return
        for task in tasks:
            if task.state in (TaskState.PLANNED, TaskState.RUNNING, TaskState.VERIFYING) or (
                    task.state == TaskState.CLARIFYING and not task.spec.blocked_on()):
                self._journal(task.id, "recovered", phase=task.state.value)
                try:
                    self._admit(task.id)
                except ControlError:
                    return

    def stop(self, timeout: float = 5.0) -> None:
        """Interrupt running turns and join. Cancels nothing: tasks stay resumable."""
        self._stop.set()
        with self._lock:
            subscription, self._subscription = self._subscription, None
            runs = list(self._runs.values())
            pump, self._pump_thread = self._pump_thread, None
        if subscription is not None:
            self.bus.unsubscribe(subscription)
        for run in runs:
            thread_id = run.current_thread
            if thread_id:
                try:
                    self.daemon.interrupt(thread_id)
                except Exception:
                    LOG.debug("Nothing to interrupt on %s", thread_id, exc_info=True)
        deadline = time.monotonic() + timeout
        for job in [r.worker for r in runs] + [pump]:
            if job is not None:
                job.join(max(0.0, deadline - time.monotonic()))

    # -- TaskControl -------------------------------------------------------

    def start(self, task_id: str) -> Task:
        task = self._require(task_id)
        if task.state != TaskState.INTAKE:
            raise ControlError(f"Task {task_id} is already {task.state.value}; it cannot be started again.")
        if not self.router.ready_to_start(task):
            raise ControlError(f"Task {task_id} is still inside its proposal grace window; "
                               "it starts on its own, or cancel it now to withdraw it.")
        self._admit(task_id)
        return self._require(task_id)

    def steer(self, task_id: str, text: str, *, spoken: bool = False) -> None:
        task = self._require(task_id)
        if task.state in TERMINAL_STATES:
            raise ControlError(f"Task {task_id} is {task.state.value}; there is nothing to steer.")
        if not isinstance(text, str) or not text.strip():
            raise ControlError("A steer needs some text.")
        try:
            cleaned = self.router.on_steer(task, text.strip()) or text.strip()
        except Exception as exc:                        # an override parse must not lose the steer
            LOG.warning("Steer override parsing failed for %s: %s", task_id, exc)
            cleaned = text.strip()
        run = self._run(task_id)
        with run.lock:
            run.steers.append((cleaned, bool(spoken)))
        self._journal(task_id, "steer", spoken=bool(spoken), text=_cap(cleaned, EXCERPT))
        self._publish(self._require(task_id))

    def answer_question(self, task_id: str, index: int, text: str) -> Task:
        task = self._require(task_id)
        if task.state != TaskState.CLARIFYING:
            raise ControlError(f"Task {task_id} is {task.state.value}; it is not waiting on a question.")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(task.spec.questions):
            raise ControlError(f"Task {task_id} has no question {index}.")
        if not isinstance(text, str) or not text.strip():
            raise ControlError("An answer needs some text.")
        task.spec.questions[index].answer = text.strip()
        remaining = task.spec.blocked_on()
        task.status.open_question = remaining[0].text if remaining else None
        self._save(task)
        self._journal(task_id, "question_answered", index=index, text=_cap(text.strip(), EXCERPT))
        if not remaining:
            self._admit(task_id)
        return self._require(task_id)

    def cancel(self, task_id: str) -> Task:
        task = self._require(task_id)
        if task.state in TERMINAL_STATES:
            raise ControlError(f"Task {task_id} is already {task.state.value}.")
        try:
            if self.router.cancel_proposal(task_id):
                self._publish(self._require(task_id))
                return self._require(task_id)
        except Exception:
            LOG.warning("Proposal withdrawal failed for %s", task_id, exc_info=True)
        run = self._run(task_id)
        run.cancel.set()
        with self._lock:
            active = task_id in self._active
            if task_id in self._queue:
                self._queue.remove(task_id)
            thread_id = run.current_thread
        if active:
            if thread_id:
                try:
                    self.daemon.interrupt(thread_id)
                except Exception:
                    LOG.debug("No turn to interrupt on %s", thread_id, exc_info=True)
            return self._require(task_id)
        self._transition(task_id, TaskState.CANCELLED, reason="owner cancelled")
        return self._require(task_id)

    def resume(self, task_id: str, *, provider: ProviderName | None = None) -> Task:
        task = self._require(task_id)
        if task.state == TaskState.FAILED:
            raise ControlError(f"Task {task_id} failed and cannot be resumed; open a new task.")
        if task.state != TaskState.BLOCKED:
            raise ControlError(f"Task {task_id} is {task.state.value}; only a blocked task resumes.")
        hit = self.ledger.ceiling_hit(task)
        if hit:
            raise ControlError(f"Task {task_id} is still over its ceiling ({hit}); raise it before resuming.")
        run = self._run(task_id)
        run.cancel.clear()
        run.ceiling = None
        if provider is not None:
            name = ProviderName(provider)
            if name == ProviderName.FAST:
                raise ControlError("A task orchestrator cannot run on the fast path.")
            run.restart_provider = name
        self._transition(task_id, self._resume_state(task), reason="owner resumed" +
                         (f" on {run.restart_provider.value}" if run.restart_provider else ""))
        self._admit(task_id)
        return self._require(task_id)

    def status(self, task_id: str) -> Task:
        task = self._require(task_id)
        self._elapsed(task)
        return task

    def list_tasks(self, project_id: str | None = None, *, active_only: bool = True) -> list[Task]:
        try:
            tasks = self.stores.tasks.list()
        except StoreError as exc:
            raise ControlError(str(exc)) from exc
        out = [t for t in tasks
               if (project_id is None or t.project_id == project_id)
               and (not active_only or t.state not in TERMINAL_STATES)]
        for task in out:
            self._elapsed(task)
        return sorted(out, key=lambda t: t.created)

    # -- proposals (§8.1) --------------------------------------------------

    def on_proposal(self, record: dict) -> str | None:
        """Consume one `turn_finished` through the router, synchronously.

        The bus may drop, so accounting and proposal consumption cannot live
        only there — but the runner is the one durable consumer the daemon has,
        so this is where it lands. Returns the one-line reply for the fast-path
        caller (also published as `proposal_reply`), or None.
        """
        try:
            result = self.router.on_event(record)
        except Exception:
            LOG.warning("Router rejected an event", exc_info=True)
            return None
        if result is None:
            return None
        reply = result if isinstance(result, str) else getattr(result, "reply", None)
        if not isinstance(reply, str):
            return None
        turn_id = record.get("turn_id")
        if turn_id:
            self.proposal_replies[turn_id] = reply
        match = re.search(r"\btask ([0-9a-f]{8})\b", reply)
        task_id = match.group(1) if match else None
        self.bus.publish({"kind": "proposal_reply", "thread_id": record.get("thread_id"),
                          "project_id": record.get("project_id"),
                          "data": {"turn_id": turn_id, "reply": reply, "task_id": task_id}})
        if task_id:
            with self._lock:
                self._proposed.add(task_id)
        return reply

    def _admit_ready(self) -> None:
        """Start proposed tasks once their 60-second withdrawal window has passed."""
        with self._lock:
            pending = list(self._proposed)
        for task_id in pending:
            try:
                task = self.stores.tasks.get(task_id)
            except StoreError:
                task = None
            if task is None or task.state != TaskState.INTAKE:
                with self._lock:
                    self._proposed.discard(task_id)
                continue
            if self.router.ready_to_start(task):
                with self._lock:
                    self._proposed.discard(task_id)
                self._admit(task_id)

    def _pump_bus(self) -> None:
        while not self._stop.is_set():
            try:
                record = self._subscription.get(timeout=0.1)
            except (queue.Empty, AttributeError):
                self._admit_ready()
                continue
            except Exception:
                break
            if record.get("kind") == "shutdown":
                break
            if (record.get("data") or {}).get("proposal"):
                self.on_proposal(record)
            self._admit_ready()

    # -- scheduling --------------------------------------------------------

    def _run(self, task_id: str) -> _Run:
        with self._lock:
            return self._runs.setdefault(task_id, _Run(task_id))

    def _admit(self, task_id: str) -> None:
        with self._lock:
            if self._stop.is_set():
                raise ControlError("The runner is shutting down.")
            run = self._runs.setdefault(task_id, _Run(task_id))
            if task_id in self._active or task_id in self._queue:
                return
            self._queue.append(task_id)
        self._pump()

    def _pump(self) -> None:
        starting = []
        with self._lock:
            while self._queue and len(self._active) < self.max_active and not self._stop.is_set():
                task_id = self._queue.popleft()
                run = self._runs.setdefault(task_id, _Run(task_id))
                worker = threading.Thread(target=self._work, args=(task_id,),
                                          name=f"jarvis-task-{task_id}", daemon=True)
                run.worker = worker
                self._active.add(task_id)
                starting.append(worker)
        for worker in starting:
            worker.start()

    def _work(self, task_id: str) -> None:
        run = self._run(task_id)
        try:
            self._drive(run)
        except Exception:
            LOG.exception("Task runner crashed on %s", task_id)
        finally:
            with self._lock:
                self._active.discard(task_id)
                run.worker = None
                run.current_thread = None
            self._pump()

    # -- the lifecycle (§10.1) ---------------------------------------------

    def _drive(self, run: _Run) -> None:
        try:
            while not self._stop.is_set():
                task = self._require(run.task_id)
                if task.state in TERMINAL_STATES or task.state == TaskState.BLOCKED:
                    return
                self._check_cancel(run)
                if task.state == TaskState.INTAKE:
                    self._begin(run)
                elif task.state == TaskState.CLARIFYING:
                    if not self._clarify(run):
                        return                      # parked on a blocking question
                elif task.state in (TaskState.PLANNED, TaskState.RUNNING):
                    self._execute(run)
                elif task.state == TaskState.VERIFYING:
                    self._verify(run)
                else:
                    return
        except _Cancelled:
            self._finish(run, TaskState.CANCELLED, "owner cancelled")
        except _OrchestratorDown as exc:
            self._block(run, self._unavailable_text(run, str(exc)))
        except _Blocked as exc:
            self._block(run, str(exc))
        except (DaemonError, BriefRefused, RoutingBlocked, worktrees.WorktreeError, StoreError) as exc:
            self._block(run, f"{type(exc).__name__}: {exc}")
        except Exception as exc:                    # never leave a task in a phase nobody drives
            LOG.exception("Unexpected runner failure on %s", run.task_id)
            self._block(run, f"runner error: {type(exc).__name__}: {exc}")

    def _finish(self, run: _Run, state: TaskState, reason: str) -> None:
        try:
            self._transition(run.task_id, state, reason=reason)
        except StoreError:
            LOG.exception("Cannot move %s to %s", run.task_id, state.value)

    def _block(self, run: _Run, reason: str) -> None:
        self._journal(run.task_id, "blocked", milestone="blocked", reason=_cap(reason, 2000))
        self._finish(run, TaskState.BLOCKED, _cap(reason, 2000))

    def _begin(self, run: _Run) -> None:
        task = self._transition(run.task_id, TaskState.CLARIFYING, reason="intake accepted")
        project = self._project(task)
        task = worktrees.ensure(task, project, self.stores)
        self._publish(task)                         # ensure() writes the task itself
        self._orchestrator(run)

    def _clarify(self, run: _Run) -> bool:
        task = self._require(run.task_id)
        orchestrator = self._orchestrator(run)
        if not task.spec.goal and not task.spec.acceptance:
            data = self._ask(run, orchestrator, Role.ORCHESTRATOR,
                             self._intake_prompt(task), "spec")
            task = self._require(run.task_id)
            task.spec = Spec(goal=data["goal"], deliverable=data["deliverable"],
                             acceptance=data["acceptance"], constraints=data["constraints"],
                             questions=[OpenQuestion(**q) for q in data["questions"]])
            self._save(task)
            self._journal(run.task_id, "spec", goal=task.spec.goal,
                          acceptance=task.spec.acceptance,
                          questions=[to_json(q) for q in task.spec.questions])
        task = self._require(run.task_id)
        blocking = task.spec.blocked_on()
        if blocking:
            task.status.open_question = blocking[0].text
            self._save(task)
            self._journal(run.task_id, "question", milestone="question", text=blocking[0].text,
                          options=blocking[0].options)
            self.bus.publish({"kind": "task_question", "task_id": task.id,
                              "project_id": task.project_id,
                              "data": {"questions": [to_json(q) for q in blocking],
                                       "task": to_json(task)}})
            return False
        if task.status.open_question:
            task.status.open_question = None
            self._save(task)
        data = self._ask(run, orchestrator, Role.ORCHESTRATOR, self._plan_prompt(task), "plan")
        task = self._require(run.task_id)
        task.plan = data["steps"]
        task.status.steps = len(task.plan)
        task.status.step = 0
        self._save(task)
        self._journal(run.task_id, "plan", steps=task.plan)
        self._transition(run.task_id, TaskState.PLANNED, reason="plan accepted")
        return True

    def _execute(self, run: _Run) -> None:
        task = self._require(run.task_id)
        instruction = run.pending_instruction
        run.pending_instruction = None
        if task.state == TaskState.PLANNED:
            implementer = self._open_role(run, Role.IMPLEMENTER)
            task = self._require(run.task_id)
            task.status.started = task.status.started or utcnow()
            task.status.step = 1
            self._save(task)
            task = self._transition(run.task_id, TaskState.RUNNING, reason="implementer started")
            instruction = instruction or self._first_instruction(task)
        else:
            implementer = self._thread_for(task, Role.IMPLEMENTER) or self._open_role(run, Role.IMPLEMENTER)
            instruction = instruction or self._first_instruction(task)
        orchestrator = self._orchestrator(run)
        for _ in range(MAX_STEPS):
            self._check_cancel(run)
            self._deliver_steering(run, orchestrator)
            self._journal(run.task_id, "instruction", thread_id=implementer,
                          step=self._require(run.task_id).status.step,
                          text=_cap(instruction, EXCERPT))
            result = self._run_turn(run, implementer, instruction)
            report = result.text.strip() or "[the implementer returned no text]"
            self._journal(run.task_id, "worker_report", thread_id=implementer,
                          step=self._require(run.task_id).status.step, text=_cap(report, EXCERPT))
            self._deliver_steering(run, orchestrator)
            data = self._ask(run, orchestrator, Role.ORCHESTRATOR,
                             self._relay_prompt(self._require(run.task_id), report), "next")
            if data["done"] or not data["next"]:
                self._transition(run.task_id, TaskState.VERIFYING, reason="implementation reported complete")
                return
            task = self._require(run.task_id)
            task.status.step += 1
            self._save(task)
            instruction = data["next"]
        raise _Blocked(f"the implementer loop reached {MAX_STEPS} steps without the orchestrator "
                       "reporting the work complete; the plan may be wrong")

    def _verify(self, run: _Run) -> None:
        task = self._require(run.task_id)
        reviewer = self._open_role(run, Role.REVIEWER)   # always a fresh thread (§10.1)
        data = self._ask(run, reviewer, Role.REVIEWER, self._review_prompt(task, reviewer), "review")
        self._journal(run.task_id, "review", thread_id=reviewer, passed=data["pass"],
                      findings=data["findings"])
        if data["pass"]:
            orchestrator = self._orchestrator(run)
            report = self._ask(run, orchestrator, Role.ORCHESTRATOR,
                               self._report_prompt(self._require(run.task_id), reviewer, data), "report")
            task = self._require(run.task_id)
            task.report = Report(done=report["done"], changed=report["changed"],
                                 verified=report["verified"], open=report["open"],
                                 next=report["next"], cost=self._cost(task))
            task.status.open_question = None
            self._save(task)
            self._journal(run.task_id, "report", milestone="done", data=to_json(task.report))
            self._transition(run.task_id, TaskState.DONE, reason="verified")
            return
        failures = sum(1 for row in self._journal_rows(run.task_id)
                       if row.get("event") == "review" and row.get("passed") is False)
        findings = _bullets(data["findings"])
        if failures >= 2:
            raise _Blocked("verification failed twice; the reviewer's findings stand:\n" + findings)
        run.pending_instruction = ("The reviewer checked the work against the acceptance criteria "
                                   "and it did not pass. Fix exactly these findings:\n" + findings)
        self._transition(run.task_id, TaskState.RUNNING, reason="verification failed; findings relayed")

    # -- roles and threads -------------------------------------------------

    def _orchestrator(self, run: _Run) -> str:
        task = self._require(run.task_id)
        existing = self._thread_for(task, Role.ORCHESTRATOR)
        forced = run.restart_provider
        if existing is not None and forced is None:
            return existing
        if existing is not None and forced is not None:
            try:
                self.daemon.close_thread(existing)
            except Exception:
                LOG.debug("Old orchestrator %s was not open", existing, exc_info=True)
        run.restart_provider = None
        thread_id = self._open_role(run, Role.ORCHESTRATOR, provider=forced)
        task = self._require(run.task_id)
        if task.spec.goal or task.plan:
            # §8.4: a moved orchestrator restarts from durable state, never from
            # the old transcript — which is gone with its session by definition.
            self._journal(run.task_id, "orchestrator_restarted", thread_id=thread_id,
                          provider=forced.value if forced else None)
            self._run_turn(run, thread_id, self._restart_prompt(task), origin="system")
        return thread_id

    def _open_role(self, run: _Run, role: Role, *, provider: ProviderName | None = None) -> str:
        task = self._require(run.task_id)
        project = self._project(task)
        if provider is None:
            try:
                decision = self.router.next_worker(task, role.value)
            except RoutingBlocked as exc:
                if role == Role.ORCHESTRATOR:
                    raise _OrchestratorDown(str(exc)) from exc
                raise _Blocked(str(exc)) from exc
            provider = ProviderName(decision.provider)
            self._publish(self._require(run.task_id))   # resolve() writes status.routing
        else:
            task.status.routing.append(RoutingDecision(
                role, provider, f"owner resume: restart {role.value} on {provider.value}"))
            self._save(task)
            self._journal(run.task_id, "routing_forced", role=role.value, provider=provider.value)
        task = self._require(run.task_id)
        model, effort = model_settings(role.value, provider.value, project)
        role_brief = roles.brief_for(role)
        brief = Brief(role=role, cwd=task.worktree or worktrees.task_root(task, project),
                      system_append=role_brief.system_append,
                      profile=task.profile or project.profile, model=model, effort=effort,
                      allowed_tools=role_brief.tools_for(provider),
                      always_ask=list(project.always_ask), max_turns=role_brief.max_turns,
                      task_id=task.id)
        thread = self.daemon.open_thread(task.project_id, role, provider, brief)
        task = self._require(run.task_id)
        if role == Role.ORCHESTRATOR:
            task.thread_ids.insert(0, thread.id)     # "orchestrator first" survives a restart
        else:
            task.thread_ids.append(thread.id)
        self._save(task)
        self._journal(run.task_id, "thread_opened", role=role.value, provider=provider.value,
                      thread_id=thread.id, model=model, effort=effort)
        return thread.id

    def _thread_for(self, task: Task, role: Role) -> str | None:
        for thread_id in task.thread_ids:
            try:
                thread = self.stores.threads.get(thread_id)
            except StoreError:
                continue
            if thread is not None and thread.role == role:
                return thread_id
        return None

    # -- turns -------------------------------------------------------------

    def _ask(self, run: _Run, thread_id: str, role: Role, prompt: str, shape: str) -> dict:
        contract = roles.CONTRACTS[shape]
        result = self._run_turn(run, thread_id, prompt)
        data = roles.parse_block(result.text, shape)
        if data is None:
            # Exactly one re-ask (§WP11): never mine prose for a spec.
            result = self._run_turn(run, thread_id, roles.REASK + contract)
            data = roles.parse_block(result.text, shape)
        if data is None:
            raise _Blocked(f"the {role.value} did not return a valid {shape} block after two "
                           f"attempts. Its last reply began:\n{_cap(result.text.strip(), 400)}")
        return data

    def _run_turn(self, run: _Run, thread_id: str, text: str, *, origin: str = "owner") -> TurnResult:
        task = self._require(run.task_id)
        is_orchestrator = thread_id == self._thread_for(task, Role.ORCHESTRATOR)
        subscription = self.bus.subscribe(
            lambda record, wanted=thread_id: (record.get("thread_id") == wanted
                                              and record.get("kind") != "text_delta")
            or record.get("kind") == "shutdown")
        result = TurnResult()
        pieces: list[str] = []
        forwarded = False
        try:
            run.current_thread = thread_id
            turn_id = self._send(thread_id, UserMessage(text=text, origin=origin))
            deadline = time.monotonic() + self.turn_timeout
            persisted = probed = 0.0
            while True:
                if time.monotonic() > deadline:
                    raise _Blocked(f"the {'orchestrator' if is_orchestrator else 'worker'} turn "
                                   f"exceeded {self.turn_timeout:g}s with no result")
                try:
                    record = subscription.get(timeout=0.1)
                except queue.Empty:
                    # The bus is bounded; a dropped TURN_FINISHED must not wedge
                    # the task, so fall back to the thread's durable log — but
                    # not on every idle tick, because that reads the whole file.
                    if self.clock() - probed > 1.0:
                        probed = self.clock()
                        if self._finished_in_log(thread_id, turn_id):
                            result.stop = "end"
                            break
                    record = None
                if record is None:
                    pass
                elif record.get("kind") == "shutdown":
                    raise _Blocked("the daemon shut down mid-turn")
                else:
                    # Synchronous, on the durable path: the bus may drop, the
                    # ledger and the proposal consumer may not (WP9-notes).
                    try:
                        self.router.on_event(record)
                    except Exception:
                        LOG.warning("Router rejected an event on %s", thread_id, exc_info=True)
                    kind = record.get("kind")
                    data = record.get("data") or {}
                    if kind == "text":
                        pieces.append(str(data.get("text") or ""))
                    elif kind in ("tool_started", "tool_finished"):
                        self._note_tool(run.task_id, data)
                    elif kind == "usage":
                        self._note_usage(run)
                    elif kind == "error" and data.get("fatal"):
                        result.fatal = str(data.get("message") or "provider error")
                    elif kind == "turn_finished" and record.get("turn_id") == turn_id:
                        result.stop = str(data.get("stop") or "end")
                        break
                if self.clock() - persisted > 0.5:
                    persisted = self.clock()
                    self._persist_status(run.task_id)
                if run.cancel.is_set() and not forwarded:
                    forwarded = True
                    self._interrupt(thread_id)
                if run.ceiling and not forwarded:
                    forwarded = True
                    self._interrupt(thread_id)
        finally:
            run.current_thread = None
            self.bus.unsubscribe(subscription)
            self._persist_status(run.task_id)
        result.text = "\n".join(p for p in pieces if p).strip()
        self._check_cancel(run)
        if run.ceiling:
            raise _Blocked(run.ceiling)
        if result.fatal and is_orchestrator:
            raise _OrchestratorDown(result.fatal)
        if result.fatal:
            raise _Blocked(f"the worker's provider failed: {result.fatal}")
        return result

    def _send(self, thread_id: str, message: UserMessage) -> str:
        """The escape hatch (§6.1) sends its `owner-ran` result on the worker's own
        thread, so a relay can legitimately arrive while someone else's turn runs."""
        deadline = time.monotonic() + SEND_TIMEOUT
        while True:
            try:
                return self.daemon.send(thread_id, message)
            except DaemonError as exc:
                if "already running" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def _interrupt(self, thread_id: str) -> None:
        try:
            self.daemon.interrupt(thread_id)
        except Exception:
            LOG.debug("Nothing to interrupt on %s", thread_id, exc_info=True)

    def _finished_in_log(self, thread_id: str, turn_id: str) -> bool:
        """The bus is bounded and may drop; the thread log is the durable record."""
        try:
            rows = self.stores.threads.read_log(thread_id)
        except StoreError:
            return False
        return any(row.get("kind") == "turn_finished" and row.get("turn_id") == turn_id
                   for row in rows)

    def _deliver_steering(self, run: _Run, orchestrator: str) -> None:
        for text, spoken in run.take_steers():
            prefix = "[owner steering] " + ("[voice note] " if spoken else "")
            self._journal(run.task_id, "steering_delivered", thread_id=orchestrator,
                          spoken=spoken, text=_cap(prefix + text, EXCERPT))
            self._run_turn(run, orchestrator, prefix + text)

    # -- status record (§10.3) ---------------------------------------------

    def _note_tool(self, task_id: str, data: dict) -> None:
        task = self._cached(task_id)
        name = data.get("name")
        if name:
            task.status.last_tool = str(name)
        args = data.get("args")
        if isinstance(args, dict):
            for key in _FILE_ARGS:
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    task.status.last_file = value.strip()
                    break

    def _note_usage(self, run: _Run) -> None:
        task = self._cached(run.task_id)
        cost = tokens = 0.0
        for thread_id in task.thread_ids:
            try:
                thread = self.stores.threads.get(thread_id)
            except StoreError:
                thread = None
            if thread is not None:
                cost += thread.cost_usd
                tokens += thread.tokens
        task.status.cost_usd = round(cost, 10)
        task.status.tokens = int(tokens)
        self._elapsed(task)
        hit = None
        try:
            hit = self.ledger.ceiling_hit(task)
        except Exception:
            LOG.warning("Ceiling check failed for %s", run.task_id, exc_info=True)
        if hit and not run.ceiling:
            run.ceiling = (f"{hit}. Spent ${task.status.cost_usd:.4f} and "
                           f"{task.status.tokens} work tokens across "
                           f"{len(task.thread_ids)} thread(s). Raise the ceiling and resume.")
            self._journal(run.task_id, "ceiling_hit", milestone="blocked", reason=run.ceiling)

    def _elapsed(self, task: Task) -> None:
        if task.status.started:
            try:
                start = datetime.fromisoformat(task.status.started)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                task.status.elapsed_s = max(task.status.elapsed_s,
                                            (datetime.now(timezone.utc) - start).total_seconds())
            except ValueError:
                pass

    def _cached(self, task_id: str) -> Task:
        """One in-flight copy of the task per turn, so a chatty turn is not one
        JSON write per tool call; `_persist_status` flushes it."""
        with self._lock:
            task = self._dirty.get(task_id)
            if task is None:
                task = self._require(task_id)
                self._dirty[task_id] = task
            return task

    def _persist_status(self, task_id: str) -> None:
        with self._lock:
            dirty = self._dirty.pop(task_id, None)
        if dirty is None:
            return
        try:
            current = self._require(task_id)
        except ControlError:
            return
        if current.state != dirty.state:
            dirty.state = current.state
            dirty.status.phase = current.state
        self._elapsed(dirty)
        current.status = dirty.status
        try:
            self._save(current)
        except StoreError:
            LOG.warning("Cannot persist the status record for %s", task_id, exc_info=True)

    def _cost(self, task: Task) -> dict[str, float]:
        """Dollars under the provider's name; work tokens under `<provider>_tokens`
        when that provider reports no dollars. Never one number for two units."""
        out: dict[str, float] = {}
        for thread_id in task.thread_ids:
            try:
                thread = self.stores.threads.get(thread_id)
            except StoreError:
                continue
            if thread is None:
                continue
            name = thread.provider.value
            if thread.cost_usd:
                out[name] = round(out.get(name, 0.0) + thread.cost_usd, 6)
            if thread.tokens:
                out[name + "_tokens"] = out.get(name + "_tokens", 0.0) + thread.tokens
        return out

    # -- prompts -----------------------------------------------------------

    def _intake_prompt(self, task: Task) -> str:
        project = self._project(task)
        return (f"A new task has been opened in project {project.name} "
                f"(root {worktrees.task_root(task, project)}).\n"
                f"Your worktree for it is {task.worktree}.\n\n"
                f"The owner's brief, verbatim:\n---\n{task.brief}\n---\n\n"
                f"Write the SPEC.\n\n{roles.SPEC_CONTRACT}")

    def _plan_prompt(self, task: Task) -> str:
        answered = [q for q in task.spec.questions if q.answer or q.assumed]
        lines = [f"- {q.text}\n  " + (f"owner: {q.answer}" if q.answer else f"assumed: {q.assumed}")
                 for q in answered]
        return ("The spec is settled:\n"
                f"GOAL: {task.spec.goal}\nDELIVERABLE: {task.spec.deliverable}\n"
                f"ACCEPTANCE:\n{_bullets(task.spec.acceptance)}\n"
                f"CONSTRAINTS:\n{_bullets(task.spec.constraints)}\n"
                + ("RESOLVED QUESTIONS:\n" + "\n".join(lines) + "\n" if lines else "")
                + f"\nWrite the PLAN.\n\n{roles.PLAN_CONTRACT}")

    def _first_instruction(self, task: Task) -> str:
        steps = "\n".join(f"{i}. {step}" for i, step in enumerate(task.plan, 1))
        return (f"You are implementing this task in {task.worktree}"
                + (f" on branch {task.branch}" if task.branch else "") + ".\n\n"
                f"GOAL: {task.spec.goal}\nDELIVERABLE: {task.spec.deliverable}\n"
                f"ACCEPTANCE:\n{_bullets(task.spec.acceptance)}\n"
                f"CONSTRAINTS:\n{_bullets(task.spec.constraints)}\n\n"
                f"THE PLAN:\n{steps or '1. (no plan steps)'}\n\n"
                f"Do step 1 now, and only step 1:\n{task.plan[0] if task.plan else task.brief}")

    def _relay_prompt(self, task: Task, report: str) -> str:
        return (f"The implementer finished step {task.status.step} of {task.status.steps or '?'} "
                f"and reported:\n---\n{_cap(report, 6000)}\n---\n\n"
                f"THE PLAN:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(task.plan, 1)) +
                f"\n\nWhat next?\n\n{roles.NEXT_CONTRACT}")

    def _review_prompt(self, task: Task, reviewer_thread: str) -> str:
        state = ""
        try:
            status = worktrees.status(task, self.stores)
            state = (f"branch {status.branch}, {status.ahead} commit(s) ahead, "
                     f"{'dirty' if status.dirty else 'clean'}, last: {status.last_commit}")
        except Exception:
            state = "(worktree status unavailable)"
        return (f"You are reviewing task {task.id} in {task.worktree} (thread {reviewer_thread}).\n\n"
                f"GOAL: {task.spec.goal}\nDELIVERABLE: {task.spec.deliverable}\n"
                f"ACCEPTANCE CRITERIA:\n{_bullets(task.spec.acceptance)}\n"
                f"CONSTRAINTS:\n{_bullets(task.spec.constraints)}\n\n"
                f"THE PLAN:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(task.plan, 1)) +
                f"\n\nWORKTREE: {state}\n\nBRANCH DIFF:\n{self._diff(task)}\n\n"
                f"Verify the deliverable against the acceptance criteria.\n\n{roles.REVIEW_CONTRACT}")

    def _report_prompt(self, task: Task, reviewer_thread: str, review: dict) -> str:
        notes = _bullets(review["findings"]) if review["findings"] else "- (no notes)"
        return (f"The reviewer (thread {reviewer_thread}) passed the work. Its notes:\n{notes}\n\n"
                f"GOAL: {task.spec.goal}\nDELIVERABLE: {task.spec.deliverable}\n"
                f"ACCEPTANCE:\n{_bullets(task.spec.acceptance)}\n"
                f"WORKTREE: {task.worktree}" + (f" (branch {task.branch})" if task.branch else "") +
                f"\n\nWrite the REPORT. Cost is filled in by Jarvis; leave it out.\n\n"
                f"{roles.REPORT_CONTRACT}")

    def _restart_prompt(self, task: Task) -> str:
        reports = [row for row in self._journal_rows(task.id)
                   if row.get("event") in ("worker_report", "review", "steering_delivered")]
        history = "\n".join(
            f"- {row['event']}: {_cap(str(row.get('text') or row.get('findings') or ''), 500)}"
            for row in reports[-12:]) or "- (no worker reports yet)"
        return ("You are taking over as ORCHESTRATOR of a task already in progress. The previous "
                "orchestrator's session is gone; everything you need is below, from Jarvis's "
                "durable state. Do not ask for the old transcript — there is none.\n\n"
                f"TASK {task.id}, phase {task.state.value}, step {task.status.step}"
                f"/{task.status.steps}.\nWORKTREE: {task.worktree}\n\n"
                f"BRIEF:\n{task.brief}\n\n"
                f"SPEC\nGOAL: {task.spec.goal}\nDELIVERABLE: {task.spec.deliverable}\n"
                f"ACCEPTANCE:\n{_bullets(task.spec.acceptance)}\n"
                f"CONSTRAINTS:\n{_bullets(task.spec.constraints)}\n\n"
                f"PLAN:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(task.plan, 1)) +
                f"\n\nWORKER REPORTS SO FAR:\n{history}\n\n"
                "Acknowledge in one line. The next message will ask you for a decision.")

    def _unavailable_text(self, run: _Run, detail: str) -> str:
        try:
            text, _durable = self.router.orchestrator_unavailable(self._require(run.task_id))
            return f"{text} ({detail})"
        except Exception:
            LOG.warning("Cannot build the orchestrator-unavailable message", exc_info=True)
            return f"the orchestrator's provider is unavailable: {detail}"

    def _diff(self, task: Task) -> str:
        if not task.worktree or not task.branch:
            return "(no git branch for this task)"
        base = ""
        for row in self._journal_rows(task.id):
            if row.get("event") in ("worktree_created", "worktree_adopted") and row.get("base_ref"):
                base = row["base_ref"]
        if not base:
            return "(no base ref recorded)"
        parts = []
        for label, args in (("committed", ["diff", f"{base}...HEAD"]),
                            ("uncommitted", ["diff", "HEAD"])):
            try:
                done = subprocess.run(["git", *args], cwd=task.worktree, capture_output=True,
                                      text=True, timeout=60, check=False)
                body = done.stdout.strip()
            except (OSError, subprocess.SubprocessError) as exc:
                body = f"(git {args[0]} failed: {exc})"
            if body:
                parts.append(f"--- {label} ---\n{body}")
        return _cap("\n".join(parts) or "(no changes on the branch)", DIFF_CAP)

    # -- stores ------------------------------------------------------------

    def _require(self, task_id: str) -> Task:
        try:
            task = self.stores.tasks.get(task_id)
        except StoreError as exc:
            raise ControlError(str(exc)) from exc
        if task is None:
            raise ControlError(f"No task {task_id}.")
        return task

    def _project(self, task: Task):
        project = self.stores.projects.get(task.project_id)
        if project is None:
            raise _Blocked(f"project {task.project_id} is missing from the store")
        return project

    def _resume_state(self, task: Task) -> TaskState:
        previous = TaskState.CLARIFYING
        for row in self._journal_rows(task.id):
            if row.get("event") == "transition" and row.get("new_state") == TaskState.BLOCKED.value:
                previous = TaskState(row.get("old_state") or TaskState.CLARIFYING.value)
        if previous in (TaskState.INTAKE, TaskState.CLARIFYING):
            return TaskState.CLARIFYING
        if previous == TaskState.VERIFYING:
            return TaskState.RUNNING            # BLOCKED has no edge back to verifying
        return previous if previous in RESUMABLE else TaskState.CLARIFYING

    def _journal_rows(self, task_id: str) -> list[dict[str, Any]]:
        try:
            return self.stores.tasks.read_journal(task_id)
        except StoreError:
            return []

    def _journal(self, task_id: str, event: str, **fields: Any) -> None:
        try:
            self.stores.tasks.journal(task_id, event, **fields)
        except StoreError:
            LOG.warning("Cannot journal %s on %s", event, task_id, exc_info=True)

    def _save(self, task: Task) -> Task:
        self.stores.tasks.save(task)
        self._publish(task)
        return task

    def _transition(self, task_id: str, state: TaskState, *, reason: str = "") -> Task:
        task = self.stores.tasks.transition(task_id, state, reason=reason)
        self._publish(task)
        return task

    def _publish(self, task: Task) -> None:
        self.bus.publish({"kind": "task_status_changed", "task_id": task.id,
                          "project_id": task.project_id, "data": to_json(task)})

    def _check_cancel(self, run: _Run) -> None:
        if run.cancel.is_set():
            raise _Cancelled()
