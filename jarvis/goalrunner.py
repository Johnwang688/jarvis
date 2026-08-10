"""The goal runner (away-agent phase 2): work a goal in slices until done.

A goal is a loop of turns, not a turn. The runner is one worker thread that
takes goals from the store serially — one at a time, on purpose: the browser
is a process singleton, spend stays predictable, and queued goals just wait.
Each goal gets its own Agent bound to its own session, which is what makes a
daemon restart free: the transcript comes back through the session machinery
and the next slice continues where the last one stopped.

A *slice* is one `run_turn`. Between slices — the only place the transcript
is guaranteed wire-whole — the runner checks, in order: shutdown, owner
cancel, budget ceilings (dollars / hours / slices), then feeds the next
message: pending owner steering if any, otherwise "continue".

**Steering is barge-in for goals.** A `steer:` DM lands in a queue and sets
the interrupt Event the agent's `should_stop` watches, so an in-flight turn
stops cleanly at the next step boundary (the face's cancel mechanism,
reused) and the steering text becomes the next slice's user message,
prefixed `[owner steering]`. Worst-case latency is one in-flight model
call, same as barge-in at the desk.

**Progress reaches the owner's phone.** The runner DMs on start, on every
terminal state (done / parked / failed / cancelled), and a digest on a
timer while working: slices, spend against ceiling, elapsed time, last
activity, current plan. The digest is assembled from the runner's own
records and the journal — never asked of the model, so it cannot be
confabulated.

**Ending is a tool call.** The goal agent carries `goal_report` (see
tools/goalctl.py); the runner consumes the report at the slice boundary.
Prose that claims completion without the call keeps the goal running.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from . import agent as agent_mod
from . import config, goals, sessions, tools
from .tools import goalctl

GOAL_SYSTEM = config.SYSTEM_PROMPT + (
    "\n\nYou are working a background goal for the owner while they are away "
    "from the machine. The goal statement is the first message; work it to "
    "completion across as many turns as you need, and keep a current plan "
    "with plan_write — it is how the owner's progress updates know what you "
    "are doing. Progress and cost are DMed to the owner automatically; do "
    "not send routine updates yourself (discord_dm_owner is for something "
    "genuinely urgent). A message beginning [owner steering] is the owner "
    "redirecting this goal mid-flight: the newest steering overrides "
    "anything earlier, including the original statement where they "
    "conflict. A tool that needs approval is asked in the owner's DMs — "
    "expect a wait, and if it comes back denied, adapt or report blocked "
    "rather than asking again. When the goal is complete, call "
    "goal_report(status='done', ...); if you cannot proceed without the "
    "owner, call goal_report(status='blocked', ...). Never claim completion "
    "in prose without calling goal_report — and goal_report always refers "
    "to the ENTIRE goal: never call it for a finished phase, milestone, or "
    "partial deliverable."
)

# Desk-only tools stay out: driving a desktop app steals the Windows
# foreground from whoever is at the machine, and the window controls are
# meaningless with no window. Everything else — browser included, since the
# runner is serial — is available, gated exactly as at the desk.
# set_avatar joins them for the same reason: it is a lever on a window nobody
# is watching, and a goal that renames him mid-run would confuse the owner
# reading its progress DMs.
_EXCLUDED_TOOLS = {"set_voice_mute", "whiteboard_close", "set_avatar"}

# The scripted turn directives. Slice 0 plans, slice 1 starts implementing,
# later slices continue; the first done triggers one verification pass.
PLAN_DIRECTIVE = (
    "[plan first] Do not implement anything in this turn. Write a detailed "
    "working plan with plan_write: every deliverable, the acceptance "
    "criteria you will hold each one to, and the order you will work. When "
    "the plan is written, reply with a one-paragraph summary and stop."
)
IMPLEMENT_DIRECTIVE = (
    "Your plan has been sent to the owner. Implement it now, keeping the "
    "plan current as you go. When everything is complete, call goal_report "
    "with status 'done'; if you are stuck, status 'blocked'."
)
CONTINUE_DIRECTIVE = (
    "Continue working the goal. If it is complete, call goal_report; "
    "if you are stuck, call goal_report with status 'blocked'."
)
VERIFY_DIRECTIVE = (
    "Verification pass — your 'done' is not accepted yet. Re-read every "
    "deliverable file and check it against the original statement and each "
    "acceptance criterion in your plan: no vague or placeholder "
    "implementations, no missing pieces, no stale cross-references. If "
    "everything genuinely holds up, call goal_report with status 'done' "
    "again. If anything is missing or falls short, do NOT call goal_report "
    "— fix it and keep working; the goal simply continues. 'blocked' stays "
    "reserved for things you truly cannot do without the owner."
)


def goal_tool_names() -> list[str]:
    return [
        name
        for name in tools.REGISTRY
        if not name.startswith("desktop_") and name not in _EXCLUDED_TOOLS
    ]


def _default_notify(text: str) -> None:
    from .tools.discord import dm_owner

    dm_owner(text)


class GoalRunner:
    """One worker thread; goals from the store, serially; DMs to the owner.

    `approver` is the dangerous-tool gate for goal agents (the daemon passes
    its remote broker's). `notify` and `agent_factory` are injection seams
    for the free test suite; production takes the defaults.
    """

    def __init__(
        self,
        approver: Callable | None = None,
        notify: Callable[[str], None] | None = None,
        agent_factory: Callable[[goals.Goal], agent_mod.Agent] | None = None,
        announce: Callable[[str], None] = print,
        poll_s: float = 3.0,
        update_minutes: float | None = None,
    ):
        self._approver = approver or (lambda tool, args: False)
        self._notify_impl = notify or _default_notify
        self._agent_factory = agent_factory or self._default_agent
        self._announce = announce
        self._poll_s = poll_s
        self._update_s = (
            update_minutes if update_minutes is not None else config.GOAL_UPDATE_MINUTES
        ) * 60
        self._stop = threading.Event()
        self._interrupt = threading.Event()  # the goal agent's should_stop
        self._cancel_requested = False
        self._steering: list[str] = []
        # Steering sent while a goal is parked or queued, delivered as its
        # first message when it (re)starts — one DM both unblocks and aims.
        self._pending_steering: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.current: goals.Goal | None = None
        self._last_activity = ""
        self._last_update = 0.0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._serve, name="goal-runner", daemon=True
        )
        self._thread.start()

    def stop(self, join_s: float = 10.0) -> None:
        """Shut down without deciding the goal's fate: a goal left `running`
        on disk is exactly what resume-on-boot exists for. Callers that gate
        approvals must release their broker first, or a slice blocked on an
        ask can outlive the join timeout."""
        self._stop.set()
        self._interrupt.set()
        if self._thread is not None:
            self._thread.join(join_s)
        goal = self.current
        if goal is not None:
            goal.journal("shutdown")

    # -- the owner's DM verbs ------------------------------------------------

    def handle_dm(self, text: str) -> str | None:
        """Parse a typed owner DM as a goal verb. None = not a verb (the
        caller falls through to conversation). Verbs are parsed, never
        modeled — same rule as approval replies, and the caller must never
        route spoken turns here: a mishearing must not steer or cancel."""
        stripped = text.strip()
        lowered = stripped.lower()

        if lowered.startswith("goal:"):
            statement = stripped[5:].strip()
            if not statement:
                return "Give me the goal after the colon: `goal: rank these papers …`"
            goal = goals.create(statement)
            return (
                f"Goal `{goal.id}` queued: {goal.statement}\n"
                f"Caps: ${goal.budgets['dollars']:.2f}, {goal.budgets['hours']:g}h, "
                f"{goal.budgets['slices']} slices. I'll DM progress every "
                f"{self._update_s / 60:g} minutes — `steer: …` redirects it, "
                "`goal cancel` stops it."
            )

        if lowered.startswith(("steer:", "redirect:")):
            steering = stripped.split(":", 1)[1].strip()
            if not steering:
                return "Give me the redirection after the colon: `steer: focus on X`"
            with self._lock:
                goal = self.current
                if goal is not None:
                    self._steering.append(steering)
            if goal is not None:
                self._interrupt.set()  # stop the in-flight turn at a step boundary
                goal.journal("steering", text=steering)
                return f"Steering `{goal.id}` — it takes effect within a step."
            # Nothing running: aim the steering at the newest parked or queued
            # goal instead, requeueing a parked one — a blocked goal usually
            # parks precisely because it needs the owner's answer, and that
            # answer should not require a resume-then-race-to-type second DM.
            waiting = [g for g in goals.all_goals() if g.status in ("parked", "queued")]
            if not waiting:
                return "No goal is running to steer. `goal: …` starts one."
            goal = max(waiting, key=lambda g: g.updated)
            with self._lock:
                self._pending_steering.setdefault(goal.id, []).append(steering)
            goal.journal("steering", text=steering)
            if goal.status == "parked":
                goal.set("queued")
                return f"Requeued `{goal.id}` with your steering — it resumes within seconds."
            return f"Steering queued for `{goal.id}` — it starts with it."

        if lowered in {"goal status", "goals", "goal?"}:
            return self.status_text()

        if lowered in {"goal resume", "resume goal"}:
            parked = [g for g in goals.all_goals() if g.status == "parked"]
            if not parked:
                return "No parked goal to resume."
            goal = max(parked, key=lambda g: g.updated)
            goal.set("queued")
            return (
                f"Requeued `{goal.id}` ({goal.reason or 'parked'}) — the runner "
                "picks it up within seconds. `steer: …` if it should change course."
            )

        if lowered in {"goal cancel", "cancel goal"}:
            with self._lock:
                goal = self.current
                if goal is None:
                    return "No goal is running to cancel."
                self._cancel_requested = True
            self._interrupt.set()
            return f"Cancelling `{goal.id}` at the next step boundary."

        return None

    def status_text(self) -> str:
        goal = self.current
        queued = [g for g in goals.active() if g.status == "queued"]
        if goal is None:
            if queued:
                return f"No goal running; {len(queued)} queued. Next: {queued[0].statement[:120]}"
            return "No goals running or queued. `goal: …` starts one."
        lines = [self._digest(goal)]
        if queued:
            lines.append(f"(+{len(queued)} queued)")
        return "\n".join(lines)

    # -- the worker ----------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            pending = goals.active()
            if not pending:
                self._stop.wait(self._poll_s)
                continue
            self._run_goal(pending[0])

    def _run_goal(self, goal: goals.Goal) -> None:
        with self._lock:
            self.current = goal
            self._cancel_requested = False
            self._steering[:] = self._pending_steering.pop(goal.id, [])
        self._last_activity = ""
        self._last_update = time.monotonic()
        # Per-run verification state. In-memory on purpose: a goal resumed
        # after a restart earns a fresh verification pass, which errs toward
        # checking twice rather than never.
        self._did_verify = False
        self._verify_pending = False
        resumed = goal.slices > 0
        goal.set("running")
        goal.journal("resume" if resumed else "start")
        self._notify(
            f"{'Resuming' if resumed else 'Working on'} goal `{goal.id}`: "
            f"{goal.statement}\nCaps ${goal.budgets.get('dollars', 0):.2f} / "
            f"{goal.budgets.get('hours', 0):g}h / {goal.budgets.get('slices', 0)} "
            f"slices — updates every {self._update_s / 60:g}m, `steer: …` to "
            "redirect, `goal cancel` to stop."
        )
        try:
            agent = self._agent_factory(goal)
        except Exception as exc:
            goal.set("failed", f"agent construction: {exc}")
            self._notify(f"Goal `{goal.id}` failed to start: {exc}")
            self._finish()
            return
        # The digest reads the live agent's plan slot; per-agent by design
        # (invariant 8), so this is the one reference the runner keeps.
        self._live_agent = agent

        try:
            while True:
                if self._stop.is_set():
                    goal.journal("paused-by-shutdown")
                    return  # stays `running` on disk; resume-on-boot handles it
                if self._cancel_requested:
                    goal.set("cancelled")
                    self._notify(f"Goal `{goal.id}` cancelled. {self._spent_line(goal)}")
                    return
                reason = goal.over_budget()
                if reason is not None:
                    goal.set("parked", reason)
                    self._notify(
                        f"Goal `{goal.id}` parked ({reason}). {self._spent_line(goal)}\n"
                        f"Last: {self._last_activity or 'n/a'}. Raise the cap and "
                        "requeue, or `goal cancel` it."
                    )
                    return

                message = self._next_message(goal)
                was_verify_slice = message == VERIFY_DIRECTIVE
                self._interrupt.clear()
                goalctl.arm()
                try:
                    turn = agent.run_turn(message)
                finally:
                    report = goalctl.consume()
                    goalctl.disarm()

                # A verify slice that keeps working instead of confirming has
                # withdrawn the earlier 'done' — the eventual real completion
                # must earn a fresh verification, or a premature done at
                # phase 1 of 7 would let the final done sail through unchecked.
                if was_verify_slice and report is None:
                    self._did_verify = False

                goal.spent_usd += turn.cost_usd
                goal.slices += 1
                goal.save()
                goal.journal(
                    "slice",
                    steps=turn.steps,
                    cost_usd=round(turn.cost_usd, 6),
                    cancelled=turn.cancelled,
                    stopped_early=turn.stopped_early,
                    text=(turn.text or "")[:300],
                )

                if report is not None:
                    if report["status"] == "blocked":
                        goal.set("parked", f"blocked: {report['summary'][:500]}")
                        self._notify(
                            f"Goal `{goal.id}` is blocked: {report['summary']}\n"
                            f"{self._spent_line(goal)} — `steer: …` to unblock or "
                            "`goal cancel`."
                        )
                        return
                    if self._did_verify:
                        goal.set("done", report["summary"][:500])
                        self._notify(
                            f"Goal `{goal.id}` done — {report['summary']}\n"
                            f"{self._spent_line(goal)}"
                        )
                        return
                    # A first "done" buys a verification slice, not the exit.
                    # Aimed at the one-slice victory declaration: a plan the
                    # model wrote for itself carries no acceptance pressure,
                    # so acceptance gets its own turn.
                    self._did_verify = True
                    self._verify_pending = True
                    goal.journal("verify", claimed=report["summary"][:300])
                    self._notify(
                        f"Goal `{goal.id}` reported done — running a "
                        "verification pass before accepting."
                    )
                    continue

                if goal.slices == 1:
                    plan = self._current_plan()
                    self._notify(
                        f"Plan for `{goal.id}`:\n"
                        f"{plan or '(no plan was written — proceeding anyway)'}"
                    )

                self._maybe_update(goal)
        except Exception as exc:
            # A crashed slice must never take the runner thread with it.
            goal.set("failed", f"{type(exc).__name__}: {exc}")
            goal.journal("crash", error=f"{type(exc).__name__}: {exc}")
            self._notify(f"Goal `{goal.id}` failed: {exc}. {self._spent_line(goal)}")
        finally:
            self._finish()

    def _finish(self) -> None:
        with self._lock:
            self.current = None
            self._steering.clear()
        self._live_agent = None

    def _next_message(self, goal: goals.Goal) -> str:
        with self._lock:
            steering, self._steering[:] = list(self._steering), []
        if steering:
            joined = "\n".join(steering)
            return f"[owner steering] {joined}"
        if self._verify_pending:
            self._verify_pending = False
            return VERIFY_DIRECTIVE
        if goal.slices == 0:
            return f"{goal.statement}\n\n{PLAN_DIRECTIVE}"
        if goal.slices == 1:
            return IMPLEMENT_DIRECTIVE
        # Covers both "the last slice ran out of steps" (its notice is already
        # in the transcript) and "resuming after a restart" (the session came
        # back with the whole history) — in each case the transcript itself
        # says where things stand, so the nudge only has to say "go on".
        return CONTINUE_DIRECTIVE

    # -- the default agent ---------------------------------------------------

    def _default_agent(self, goal: goals.Goal) -> agent_mod.Agent:
        session = None
        if goal.session_id:
            session = sessions.load(goal.session_id)
        if session is None:
            session = sessions.new("goal")
            goal.session_id = session.id
            goal.save()
        return agent_mod.Agent(
            system=GOAL_SYSTEM,
            tool_names=goal_tool_names(),
            approve=self._approver,
            on_event=self._on_event,
            should_stop=self._interrupt.is_set,
            session=session,
        )

    # -- progress ------------------------------------------------------------

    def _on_event(self, kind: str, data) -> None:
        if kind == "cost":
            self._check_live_spend(data)
        if kind == "tool_start":
            name, arguments = data
            self._last_activity = f"{name} {str(arguments)[:80]}"
            goal = self.current
            if goal is not None:
                goal.journal("tool", name=name, args=str(arguments)[:200])
        elif kind == "interim_text" and isinstance(data, str) and data.strip():
            self._last_activity = data.strip()[:120]
        goal = self.current
        if goal is not None:
            self._maybe_update(goal)

    def _maybe_update(self, goal: goals.Goal) -> None:
        if time.monotonic() - self._last_update < self._update_s:
            return
        self._last_update = time.monotonic()
        digest = self._digest(goal)
        goal.journal("update-dm", text=digest[:300])
        # Off the loop thread: a DM is a REST call, and progress reporting
        # must never add latency to the work it reports on. Through _notify,
        # so a Discord hiccup is announced instead of a thread dying noisily.
        threading.Thread(
            target=self._notify, args=(digest,), daemon=True, name="goal-dm"
        ).start()

    def _check_live_spend(self, turn_cost: float) -> None:
        """Stop a slice that is spending past the goal's ceiling, mid-turn.

        The dollar ceiling used to be checked only *between* slices, which
        assumed a slice costs about one slice's worth. `run_fleet` broke that
        assumption — six children, each with its own step budget, all inside a
        single turn — but the assumption was already thin: a 40-step turn that
        goes in circles overspends the same way, just more slowly.

        Setting the interrupt is enough. It is what `should_stop` reads, so the
        turn ends whole at the next step boundary (invariant 3) and the normal
        between-slices ceiling check then parks the goal and DMs the owner with
        what was spent. Nothing here has to know how parking works.
        """
        goal = self.current
        if goal is None or self._interrupt.is_set():
            return
        cap = goal.budgets.get("dollars", config.GOAL_MAX_DOLLARS)
        if goal.spent_usd + float(turn_cost or 0.0) < cap:
            return
        self._last_activity = (
            f"stopped mid-slice at ${goal.spent_usd + turn_cost:.2f} of ${cap:.2f}"
        )
        goal.journal(
            "budget",
            reason="dollar ceiling reached mid-slice",
            spent_usd=round(goal.spent_usd + turn_cost, 6),
            cap_usd=cap,
        )
        self._interrupt.set()

    def _digest(self, goal: goals.Goal) -> str:
        cap = goal.budgets.get("dollars", config.GOAL_MAX_DOLLARS)
        lines = [
            f"Goal `{goal.id}` {goal.status}: {goal.slices} slices, "
            f"${goal.spent_usd:.2f}/${cap:.2f}, {goal.elapsed_minutes():.0f}m in.",
            f"Last: {self._last_activity or 'starting up'}",
        ]
        plan = self._current_plan()
        if plan:
            lines.append(f"Plan:\n{plan[:400]}")
        return "\n".join(lines)

    def _current_plan(self) -> str:
        agent = getattr(self, "_live_agent", None)
        if agent is None:
            return ""
        return (agent.plan.get("text") or "").strip()

    def _spent_line(self, goal: goals.Goal) -> str:
        cap = goal.budgets.get("dollars", config.GOAL_MAX_DOLLARS)
        return (
            f"Spent ${goal.spent_usd:.2f}/${cap:.2f} over {goal.slices} "
            f"slices, {goal.elapsed_minutes():.0f}m."
        )

    def _notify(self, text: str) -> None:
        try:
            self._notify_impl(text)
        except Exception as exc:
            self._announce(f"[goal] DM failed: {type(exc).__name__}: {exc}")
