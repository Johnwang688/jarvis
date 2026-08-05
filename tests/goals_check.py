"""Synthetic checks for background goals (store + runner). Free.

No network and no API: the runner takes a scripted fake agent through its
`agent_factory` seam, DMs land in a list through `notify`, and the store is
pointed at a temp directory. What matters here:

  - the store round-trips and journals append-only, runner-written;
  - a goal ends only through goal_report (the tool, through real dispatch) —
    prose never ends one;
  - steering: `steer:` queues text, interrupts the in-flight turn via
    should_stop, and lands as the next slice's `[owner steering]` message;
  - every budget ceiling parks with a DM saying what was spent;
  - cancel cancels, shutdown leaves `running` on disk, and a fresh runner
    resumes it without re-sending the statement;
  - verbs are typed-only at the daemon router, so a voice note can never
    steer or cancel.

Run:  .venv/bin/python tests/goals_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config


def _patch_goals_dir(tmp: Path):
    real = config.GOALS_DIR
    config.GOALS_DIR = tmp / "goals"
    return real


# ---- the store ---------------------------------------------------------------


def store_checks(tmp: Path) -> None:
    from jarvis import goals

    real = _patch_goals_dir(tmp)
    try:
        goal = goals.create("rank the papers", dollars=1.5)
        assert (goal.path / "goal.json").exists(), "a goal must exist on disk at once"
        loaded = goals.load(goal.id)
        assert loaded.statement == "rank the papers"
        assert loaded.budgets["dollars"] == 1.5
        assert loaded.budgets["slices"] == config.GOAL_MAX_SLICES
        assert loaded.status == "queued"

        loaded.spent_usd = 0.25
        loaded.slices = 3
        loaded.session_id = "s-123"
        loaded.set("running")
        again = goals.load(goal.id)
        assert (again.spent_usd, again.slices, again.session_id, again.status) == (
            0.25, 3, "s-123", "running",
        )

        # Journal: append-only, ordered, and it survived the status writes.
        lines = [
            json.loads(line)
            for line in (goal.path / "journal.jsonl").read_text().splitlines()
        ]
        kinds = [entry["kind"] for entry in lines]
        assert kinds[0] == "created" and "status" in kinds, kinds

        # active(): running before queued, oldest first, terminal states absent.
        second = goals.create("second")
        third = goals.create("third")
        third.set("done")
        order = [g.id for g in goals.active()]
        assert order == [goal.id, second.id], order

        # Budgets: each ceiling reports its own reason.
        assert goals.create("a", dollars=0.0).over_budget() == "budget-dollars"
        assert goals.create("b", slices=0).over_budget() == "budget-slices"
        hourly = goals.create("c", hours=0.0)
        hourly.created -= 10
        assert hourly.over_budget() == "budget-hours"
        assert goals.create("d").over_budget() is None
    finally:
        config.GOALS_DIR = real
    print("ok  store: round-trip, journal, active ordering, budget reasons")


# ---- goal_report through real dispatch ---------------------------------------


def report_tool_checks() -> None:
    from jarvis import tools
    from jarvis.tools import goalctl

    goalctl.disarm()
    outside = tools.dispatch("goal_report", '{"status": "done", "summary": "x"}')
    assert "no background goal" in outside.text
    assert goalctl.consume() is None, "an unarmed call must not set a report"

    goalctl.arm()
    bad = tools.dispatch("goal_report", '{"status": "maybe", "summary": "x"}')
    assert "must be" in bad.text and goalctl.consume() is None
    good = tools.dispatch("goal_report", '{"status": "done", "summary": "all ranked"}')
    assert "marked done" in good.text
    assert goalctl.consume() == {"status": "done", "summary": "all ranked"}
    assert goalctl.consume() is None, "a report reads once"
    goalctl.disarm()
    print("ok  goal_report: registered, armed-only, one-shot, validated")


# ---- the runner --------------------------------------------------------------


class FakeTurn:
    def __init__(self, cost=0.01, cancelled=False):
        self.text = "working"
        self.steps = 2
        self.cost_usd = cost
        self.latency_s = 0.1
        self.stopped_early = False
        self.cancelled = cancelled
        self.tool_calls = []


class ScriptedAgent:
    """One callable per slice; each receives the user message."""

    def __init__(self, script):
        self.script = list(script)
        self.messages_seen: list[str] = []
        self.plan = {"text": ""}

    def run_turn(self, message: str) -> FakeTurn:
        self.messages_seen.append(message)
        step = self.script.pop(0)
        return step(message)


def _runner(tmp: Path, script, update_minutes=999.0):
    """A runner whose worker loop is driven by hand via _run_goal."""
    from jarvis import goalrunner

    dms: list[str] = []
    agents: list[ScriptedAgent] = []

    def factory(goal):
        agent = ScriptedAgent(script)
        agents.append(agent)
        return agent

    runner = goalrunner.GoalRunner(
        notify=dms.append,
        agent_factory=factory,
        announce=lambda text: None,
        update_minutes=update_minutes,
    )
    return runner, dms, agents


def runner_done_checks(tmp: Path) -> None:
    from jarvis import goals
    from jarvis.tools import goalctl

    real = _patch_goals_dir(tmp)
    try:
        goal = goals.create("write the report")

        def plan_slice(message):
            # Slice 0 carries the statement AND asks for a plan, nothing else.
            assert "write the report" in message and "[plan first]" in message
            agents[-1].plan["text"] = "- [ ] draft\n- [ ] polish"
            return FakeTurn(cost=0.02)

        def implement_slice(message):
            assert "Implement it now" in message, message
            goalctl._report = {"status": "done", "summary": "report written"}
            return FakeTurn(cost=0.02)

        def verify_slice(message):
            # The first done buys a verification pass, not the exit.
            assert "Verification pass" in message, message
            goalctl._report = {"status": "done", "summary": "report written"}
            return FakeTurn(cost=0.01)

        runner, dms, agents = _runner(tmp, [plan_slice, implement_slice, verify_slice])
        runner._run_goal(goal)

        final = goals.load(goal.id)
        assert final.status == "done" and "report written" in final.reason
        assert abs(final.spent_usd - 0.05) < 1e-9 and final.slices == 3
        # start, the plan, the verify notice, done — and nothing else.
        assert len(dms) == 4, dms
        assert "Working on goal" in dms[0] and "steer:" in dms[0]
        assert "Plan for" in dms[1] and "draft" in dms[1]
        assert "verification pass" in dms[2]
        assert "done — report written" in dms[3] and "$0.05" in dms[3]
        assert runner.current is None
        # The tool slot is disarmed between goals: a stray call now refuses.
        from jarvis import tools

        stray = tools.dispatch("goal_report", '{"status": "done", "summary": "x"}')
        assert "no background goal" in stray.text
    finally:
        config.GOALS_DIR = real
    print("ok  runner: plan slice, implement, verify pass gates done, DMs in order")


def runner_steering_checks(tmp: Path) -> None:
    from jarvis import goals
    from jarvis.tools import goalctl

    real = _patch_goals_dir(tmp)
    try:
        goal = goals.create("compare the options")
        runner_holder = {}

        def slice1(message):
            # The owner steers mid-slice: the ack names the goal, the
            # interrupt is set so should_stop ends this turn at the next
            # step boundary — exactly the face's barge-in contract.
            ack = runner_holder["runner"].handle_dm("steer: only options A and B")
            assert goal.id in ack
            assert runner_holder["runner"]._interrupt.is_set()
            return FakeTurn(cancelled=True)

        def slice2(message):
            # Steering preempts the implement directive entirely.
            assert message == "[owner steering] only options A and B", message
            goalctl._report = {"status": "done", "summary": "A wins"}
            return FakeTurn()

        def slice3(message):
            assert "Verification pass" in message
            goalctl._report = {"status": "done", "summary": "A wins"}
            return FakeTurn()

        runner, dms, agents = _runner(tmp, [slice1, slice2, slice3])
        runner_holder["runner"] = runner
        runner._run_goal(goal)

        assert goals.load(goal.id).status == "done"
        kinds = [
            json.loads(line)["kind"]
            for line in (goal.path / "journal.jsonl").read_text().splitlines()
        ]
        assert "steering" in kinds, kinds

        # Steering with nothing running is a clear no.
        assert "No goal is running" in runner.handle_dm("steer: whatever")
        # And non-verbs fall through to conversation (None).
        assert runner.handle_dm("how was your day") is None
        assert runner.handle_dm("yes") is None  # approval replies are not verbs

        # goal resume: requeues the most recently parked goal, refuses on none.
        assert "No parked goal" in runner.handle_dm("goal resume")
        parked = goals.create("stuck thing")
        parked.set("parked", "blocked: waiting on the owner")
        ack = runner.handle_dm("goal resume")
        assert parked.id in ack and goals.load(parked.id).status == "queued"
    finally:
        config.GOALS_DIR = real
    print("ok  steering: interrupt + next-slice injection, journaled, verbs fall through")


def runner_verify_rearm_checks(tmp: Path) -> None:
    """The live-run lesson: a premature done at phase 1 must not spend the
    verification that the real completion deserves."""
    from jarvis import goals
    from jarvis.tools import goalctl

    real = _patch_goals_dir(tmp)
    try:
        goal = goals.create("seven phases")

        def plan_slice(message):
            return FakeTurn()

        def premature_done(message):
            goalctl._report = {"status": "done", "summary": "phase 1 done"}
            return FakeTurn()

        def honest_verify(message):
            # Finds phases missing, keeps working, reports nothing.
            assert "Verification pass" in message
            assert "do NOT call goal_report" in message
            return FakeTurn()

        def real_done(message):
            assert "Continue" in message
            goalctl._report = {"status": "done", "summary": "all phases done"}
            return FakeTurn()

        def second_verify(message):
            # The withdrawn done re-armed the gate: this must be a verify.
            assert "Verification pass" in message, message
            goalctl._report = {"status": "done", "summary": "all phases done"}
            return FakeTurn()

        runner, dms, agents = _runner(
            tmp, [plan_slice, premature_done, honest_verify, real_done, second_verify]
        )
        runner._run_goal(goal)
        assert goals.load(goal.id).status == "done"
        verifies = [dm for dm in dms if "verification pass" in dm]
        assert len(verifies) == 2, dms
    finally:
        config.GOALS_DIR = real
    print("ok  re-arm: a withdrawn done re-arms verification for the real completion")


def runner_budget_checks(tmp: Path) -> None:
    from jarvis import goals

    real = _patch_goals_dir(tmp)
    try:
        goal = goals.create("expensive thing", dollars=0.05)
        slices = [lambda m: FakeTurn(cost=0.03), lambda m: FakeTurn(cost=0.03)]
        runner, dms, agents = _runner(tmp, slices)
        runner._run_goal(goal)

        final = goals.load(goal.id)
        assert final.status == "parked" and final.reason == "budget-dollars"
        assert any("parked (budget-dollars)" in dm and "$0.06" in dm for dm in dms), dms

        # Slice ceiling parks the same way.
        capped = goals.create("short leash", slices=1)
        runner2, dms2, _ = _runner(tmp, [lambda m: FakeTurn()])
        runner2._run_goal(capped)
        assert goals.load(capped.id).reason == "budget-slices"
    finally:
        config.GOALS_DIR = real
    print("ok  budgets: dollar and slice ceilings park with spend in the DM")


def runner_cancel_and_resume_checks(tmp: Path) -> None:
    from jarvis import goals
    from jarvis.tools import goalctl

    real = _patch_goals_dir(tmp)
    try:
        # Cancel: requested mid-slice, honored at the boundary.
        goal = goals.create("cancel me")
        holder = {}

        def slice1(message):
            assert goal.id in holder["runner"].handle_dm("goal cancel")
            return FakeTurn(cancelled=True)

        runner, dms, _ = _runner(tmp, [slice1])
        holder["runner"] = runner
        runner._run_goal(goal)
        assert goals.load(goal.id).status == "cancelled"
        assert any("cancelled" in dm for dm in dms)

        # Shutdown: stop() mid-goal leaves it `running`; a fresh runner picks
        # it up first and continues without re-sending the statement.
        survivor = goals.create("survive a restart")

        def long_slice(message):
            holder["runner2"]._stop.set()  # shutdown lands during the slice
            return FakeTurn()

        runner2, dms2, _ = _runner(tmp, [long_slice])
        holder["runner2"] = runner2
        runner2._run_goal(survivor)
        assert goals.load(survivor.id).status == "running", "shutdown must not decide the goal"

        assert goals.active()[0].id == survivor.id, "interrupted work resumes first"

        def resume_slice(message):
            # Resumed at slices==1: the implement directive, never the
            # statement again (the transcript already holds it).
            assert "Implement it now" in message, message
            assert "survive a restart" not in message
            goalctl._report = {"status": "done", "summary": "finished after restart"}
            return FakeTurn()

        def resume_verify(message):
            assert "Verification pass" in message
            goalctl._report = {"status": "done", "summary": "finished after restart"}
            return FakeTurn()

        runner3, dms3, _ = _runner(tmp, [resume_slice, resume_verify])
        runner3._run_goal(goals.active()[0])
        assert goals.load(survivor.id).status == "done"
        assert any("Resuming" in dm for dm in dms3), dms3
    finally:
        config.GOALS_DIR = real
    print("ok  lifecycle: cancel at boundary, shutdown leaves running, restart resumes")


def runner_update_checks(tmp: Path) -> None:
    """The progress digest: sent on the interval, with cost and the plan."""
    from jarvis import goals

    real = _patch_goals_dir(tmp)
    try:
        goal = goals.create("long haul")
        from jarvis.tools import goalctl

        def slice1(message):
            return FakeTurn(cost=0.02)

        def slice2(message):
            goalctl._report = {"status": "done", "summary": "ok"}
            return FakeTurn()

        def slice3(message):
            goalctl._report = {"status": "done", "summary": "ok"}
            return FakeTurn()

        runner, dms, agents = _runner(tmp, [slice1, slice2, slice3], update_minutes=0.0)
        # update_minutes=0: every boundary is past the interval, so the digest
        # fires between slice1 and slice2.
        runner._run_goal(goal)
        time.sleep(0.3)  # digests go out on a side thread
        digests = [dm for dm in dms if "slices, $" in dm]
        assert digests, dms
        assert goal.id in digests[0] and "$0.02" in digests[0]

        # The digest carries the agent's live plan when one is written.
        goal2 = goals.create("with a plan")

        def planned_slice(message):
            agents[-1].plan["text"] = "- [x] read files\n- [ ] write summary"
            return FakeTurn()

        def finish(message):
            goalctl._report = {"status": "done", "summary": "ok"}
            return FakeTurn()

        def finish_verify(message):
            goalctl._report = {"status": "done", "summary": "ok"}
            return FakeTurn()

        runner2, dms2, agents = _runner(
            tmp, [planned_slice, finish, finish_verify], update_minutes=0.0
        )
        runner2._run_goal(goal2)
        time.sleep(0.3)  # digests go out on a side thread
        assert any("write summary" in dm for dm in dms2), dms2
    finally:
        config.GOALS_DIR = real
    print("ok  updates: interval digest with cost, includes the live plan")


def verb_router_checks(tmp: Path) -> None:
    """The daemon's _route: verbs typed-only, conversation falls through."""
    from jarvis import daemon as daemon_mod
    from jarvis import goalrunner

    real = _patch_goals_dir(tmp)
    try:
        d = daemon_mod.Daemon(port=0, announce=lambda t: None)
        d.runner = goalrunner.GoalRunner(notify=lambda t: None, announce=lambda t: None)
        seen = []
        d.responder = type(
            "R", (), {"turn": lambda self, text, ch, spoken=False: seen.append((text, spoken)) or "chat"}
        )()

        # A typed verb is handled by the runner, never reaching conversation.
        reply = d._route("goal status", "c1")
        assert "No goals" in reply and not seen

        # The same words spoken are conversation: a mishearing must not
        # touch goals, so the router only parses typed turns.
        assert d._route("goal cancel", "c1", spoken=True) == "chat"
        assert seen == [("goal cancel", True)]

        # Non-verb text falls through with spoken flag intact.
        assert d._route("hello there", "c1") == "chat"
        assert seen[-1] == ("hello there", False)
    finally:
        config.GOALS_DIR = real
    print("ok  router: typed verbs intercepted, spoken and non-verbs reach conversation")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store_checks(Path(tmp))
    report_tool_checks()
    for check in (
        runner_done_checks,
        runner_steering_checks,
        runner_verify_rearm_checks,
        runner_budget_checks,
        runner_cancel_and_resume_checks,
        runner_update_checks,
        verb_router_checks,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            check(Path(tmp))
    print("\nall goal checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
