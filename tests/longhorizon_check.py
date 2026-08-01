"""Synthetic checks for the long-horizon machinery. Free — llm.chat is faked.

Two things are under test, both aimed at the same failure: a run that goes long
enough to forget what it was doing.

The plan (jarvis/tools/plan.py) must hold:
  1. plan_write stores through dispatch, and renders into messages[0]
  2. it is refreshed every *step*, not every turn — a 40-step turn only passes
     the top of run_turn once, and step 40 is exactly when it matters
  3. it survives compaction, because messages[0] is never pruned
  4. an agent without plan_write is never told about a plan it cannot write
  5. two agents running at once do not share a checklist

Sub-agents (jarvis/tools/subagent.py) must hold:
  6. only the final text comes back — the child's tool output never lands in
     the parent's transcript (the entire point)
  7. the human gate is inherited: a deny-all parent yields a deny-all child
  8. the child's toolset is intersected with the parent's, so a workflow (no
     browser, one shared Playwright page) cannot reach the browser via a child
  9. depth is capped
 10. cancellation reaches through to the child
 11. the parent's own plan survives the call — the child binds the same
     ContextVars, so it runs in a copied context

Run:  .venv/bin/python tests/longhorizon_check.py
"""

from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jarvis.agent as agent_mod
from jarvis import runtime, tools, workflows
from jarvis.agent import Agent


def _reply(text="", calls=None):
    message = {"content": text}
    if calls:
        message["tool_calls"] = calls
    return types.SimpleNamespace(
        message=message,
        tool_calls=calls or [],
        text=text,
        cost_usd=0.0001,
        latency_s=0.01,
    )


def _call(cid, name, **args):
    return {"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}


class Script:
    """A scripted llm.chat. Records the system message it saw on each call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.systems: list[str] = []
        self.calls = 0

    def __call__(self, model, messages, **kwargs):
        self.systems.append(messages[0]["content"])
        self.calls += 1
        if self.replies:
            step = self.replies.pop(0)
            return step(messages) if callable(step) else step
        return _reply("done")


def _run(agent, script, text="go"):
    real = agent_mod.llm.chat
    agent_mod.llm.chat = script
    try:
        return agent.run_turn(text)
    finally:
        agent_mod.llm.chat = real


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def plan_basic_checks() -> None:
    agent = Agent(tool_names=["plan_write", "get_datetime"], max_steps=4)
    script = Script(
        _reply("", [_call("c1", "plan_write", plan="- [x] read spec\n- [ ] write tests")]),
        _reply("planned"),
    )
    turn = _run(agent, script)

    assert turn.text == "planned", turn
    assert agent.plan["text"].startswith("- [x] read spec"), agent.plan
    # Step 1 saw no plan; step 2 saw it. That is the per-step refresh working.
    assert "Working plan" not in script.systems[0], "plan appeared before it was written"
    assert "Working plan" in script.systems[1], "plan never reached the system message"
    assert "write tests" in script.systems[1]
    print("ok  plan: written through dispatch and injected into messages[0]")


def plan_per_step_checks() -> None:
    """The plan must still be there on the last step of a long turn.

    Refreshing once per *turn* would pass a 2-step test and fail the real case,
    so this drives 25 steps in a single turn and checks the last one.
    """
    steps = 25
    agent = Agent(tool_names=["plan_write", "get_datetime"], max_steps=steps + 2)
    replies = [_reply("", [_call("c0", "plan_write", plan="- [ ] the one thing that matters")])]
    replies += [_reply("", [_call(f"c{i}", "get_datetime")]) for i in range(1, steps)]
    replies.append(_reply("finished"))
    script = Script(*replies)
    turn = _run(agent, script)

    assert turn.text == "finished", turn
    assert script.calls >= steps, script.calls
    last = script.systems[-1]
    assert "the one thing that matters" in last, "plan was gone by the final step"
    assert all("the one thing that matters" in s for s in script.systems[1:]), (
        "plan flickered out of the system message mid-turn"
    )
    print(f"ok  plan: still in messages[0] on step {script.calls} of one turn")


def plan_survives_compaction_checks() -> None:
    """Compaction is what eats a transcript; messages[0] is out of its reach.

    Driven across several turns rather than in one long one, because that is
    the only shape where compaction can actually fire: a cut point has to be a
    `user` message, and a single turn — however many steps it runs — contains
    none after the request that started it. Worth knowing on its own: inside one
    long turn, pruning is eviction and truncation only.
    """
    agent = Agent(tool_names=["plan_write", "get_datetime"], max_steps=8)
    agent.policy.compact_at_tokens = 2_000
    agent.policy.keep_recent_messages = 4
    agent._summarize = lambda transcript: "SUMMARY"

    first = Script(
        _reply("", [_call("c0", "plan_write", plan="- [ ] survive compaction")]),
        _reply("planned"),
    )
    _run(agent, first, text="the original request")

    for turn_no in range(6):
        script = Script(
            _reply("x" * 6000, [_call(f"t{turn_no}", "get_datetime")]),
            _reply("carrying on"),
        )
        _run(agent, script, text=f"turn {turn_no}")
        last_systems = script.systems

    compacted = any(
        isinstance(m.get("content"), str) and m["content"].startswith("[Summary of earlier")
        for m in agent.messages
    )
    assert compacted, "compaction never ran, so this proves nothing"
    assert "survive compaction" in agent.messages[0]["content"], "plan lost to compaction"
    assert "survive compaction" in last_systems[-1], "plan missing from the final request"
    assert agent.messages[1]["content"] == "the original request", (
        f"the original request was compacted away: {agent.messages[1]}"
    )
    print("ok  plan: survives compaction, and so does the original request")


def plan_absent_checks() -> None:
    """An agent without plan_write must not be told to keep a plan."""
    agent = Agent(tool_names=["get_datetime"], max_steps=2)
    script = Script(_reply("hi"))
    _run(agent, script)
    assert "Working plan" not in script.systems[0], script.systems[0]
    print("ok  plan: no plan block for agents that lack plan_write")


def plan_isolation_checks() -> None:
    """Two agents on two threads keep separate checklists."""
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def drive(tag: str) -> None:
        agent = Agent(tool_names=["plan_write", "get_datetime"], max_steps=4)

        def script(model, messages, **kwargs):
            if not agent.plan["text"]:
                return _reply("", [_call("c1", "plan_write", plan=f"- [ ] {tag}")])
            barrier.wait(timeout=5)  # both plans written before either is read
            results[tag] = messages[0]["content"]
            return _reply("done")

        _run(agent, script)

    real = agent_mod.llm.chat
    threads = [threading.Thread(target=drive, args=(t,)) for t in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    agent_mod.llm.chat = real

    assert set(results) == {"alpha", "beta"}, results
    assert "alpha" in results["alpha"] and "beta" not in results["alpha"], results["alpha"]
    assert "beta" in results["beta"] and "alpha" not in results["beta"], results["beta"]
    print("ok  plan: concurrent agents do not share a checklist")


# --------------------------------------------------------------------------
# sub-agents
# --------------------------------------------------------------------------


def subagent_isolation_checks() -> None:
    """The child's bulky tool output must not reach the parent's transcript."""
    bulk = "PAGE-DUMP-" + "q" * 5000

    def script(model, messages, **kwargs):
        system = messages[0]["content"]
        if system.startswith("You are a sub-agent"):
            if len(messages) <= 2:
                return _reply("", [_call("k1", "get_datetime")])
            # The child saw the dump; it reports only the conclusion drawn from it.
            assert bulk in json.dumps(messages), "the child never actually saw the bulk"
            return _reply("the answer is 42")
        if len(messages) <= 2:
            return _reply("", [_call("p1", "run_subagent", task="go read the thing")])
        return _reply("relayed")

    agent = Agent(tool_names=["run_subagent", "get_datetime"], max_steps=6)
    real = tools.REGISTRY["get_datetime"].func
    tools.REGISTRY["get_datetime"].func = lambda: bulk
    try:
        turn = _run(agent, script)
    finally:
        tools.REGISTRY["get_datetime"].func = real

    assert turn.text == "relayed", turn
    transcript = json.dumps(agent.messages)
    assert "PAGE-DUMP" not in transcript, "the child's tool output leaked into the parent"
    assert "the answer is 42" in transcript, "the child's answer never came back"
    assert "sub-agent: 2 step(s)" in transcript, transcript[-400:]
    print("ok  subagent: only the answer comes back, not the material")


def subagent_approval_checks() -> None:
    """The child must carry the parent's approver, and fail closed without one.

    Asserted on the wiring rather than by tripping a gate, because no tool in
    SUBAGENT_TOOLS is dangerous today — that is the first line of defence, and
    this is the second, for whenever someone adds one.
    """
    def deny(tool_entry, args) -> bool:
        return False

    captured: dict[str, object] = {}
    real_agent = agent_mod.Agent

    class Spy(real_agent):
        def __init__(self, *a, **kw):
            if kw.get("system", "").startswith("You are a sub-agent"):
                captured["approve"] = kw.get("approve")
            super().__init__(*a, **kw)

    def script(model, messages, **kwargs):
        if messages[0]["content"].startswith("You are a sub-agent"):
            return _reply("nothing to report")
        if len(messages) <= 2:
            return _reply("", [_call("p1", "run_subagent", task="have a look")])
        return _reply("done")

    agent_mod.Agent = Spy
    try:
        parent = real_agent(tool_names=["run_subagent"], max_steps=6, approve=deny)
        _run(parent, script)
    finally:
        agent_mod.Agent = real_agent

    assert "approve" in captured, "the sub-agent was never constructed"
    assert captured["approve"] is deny, "the child did not inherit the parent's approver"
    print("ok  subagent: inherits the parent's approver")

    # Belt and braces: the toolset itself contains nothing that needs a gate.
    from jarvis.tools import subagent

    dangerous = [
        n for n in subagent.SUBAGENT_TOOLS if n in tools.REGISTRY and tools.REGISTRY[n].dangerous
    ]
    assert not dangerous, f"SUBAGENT_TOOLS includes dangerous tools: {dangerous}"
    unknown = [n for n in subagent.SUBAGENT_TOOLS if n not in tools.REGISTRY]
    assert not unknown, f"SUBAGENT_TOOLS names tools that do not exist: {unknown}"

    # And with nothing bound at all, the approver denies rather than approves.
    runtime._APPROVE.set(None)
    assert runtime.approver()(None, {}) is False, "unbound approver did not fail closed"
    print("ok  subagent: no dangerous tools, and an unbound approver denies")


def subagent_toolset_checks() -> None:
    """A child can never reach a tool its parent was not given.

    The case that matters: workflows are denied browser tools because they
    share one Playwright page. Without the intersection, a workflow could spawn
    a child that has them.
    """
    from jarvis.tools import subagent

    captured: dict[str, list[str]] = {}
    real_agent = agent_mod.Agent

    class Spy(real_agent):
        def __init__(self, *a, **kw):
            if kw.get("system", "").startswith("You are a sub-agent"):
                captured["tools"] = list(kw.get("tool_names") or [])
            super().__init__(*a, **kw)

    def script(model, messages, **kwargs):
        if messages[0]["content"].startswith("You are a sub-agent"):
            return _reply("nothing to report")
        if len(messages) <= 2:
            return _reply("", [_call("p1", "run_subagent", task="look around")])
        return _reply("done")

    # A workflow-shaped parent: the real SAFE_TOOLS, which exclude the browser.
    agent_mod.Agent = Spy
    try:
        parent = real_agent(tool_names=workflows.SAFE_TOOLS, max_steps=4, approve=lambda t, a: False)
        _run(parent, script)
    finally:
        agent_mod.Agent = real_agent

    child_tools = captured.get("tools", [])
    assert child_tools, "the sub-agent was never constructed"
    browser = [t for t in child_tools if t.startswith("browser_")]
    assert not browser, f"a workflow's child got browser tools: {browser}"
    assert "read_file" in child_tools, child_tools
    assert set(child_tools) <= set(workflows.SAFE_TOOLS), (
        f"child exceeded its parent's toolset: {set(child_tools) - set(workflows.SAFE_TOOLS)}"
    )
    # A parent that does have the browser passes it down — the intersection
    # narrows, it does not blanket-ban.
    runtime.bind(tool_names=set(subagent.SUBAGENT_TOOLS))
    assert "browser_snapshot" in subagent._child_tools(), "the intersection over-narrowed"
    print(f"ok  subagent: toolset intersected with the parent ({len(child_tools)} tools, no browser)")


def subagent_depth_checks() -> None:
    runtime.bind(depth=runtime.MAX_DEPTH, approve=lambda *a: False, should_stop=lambda: False)
    out = tools.dispatch("run_subagent", json.dumps({"task": "recurse"}))
    assert "depth limit" in out.text, out.text
    runtime.bind(depth=0)
    print("ok  subagent: depth capped")


def subagent_cancel_checks() -> None:
    runtime.bind(depth=0, approve=lambda *a: False, should_stop=lambda: True)
    out = tools.dispatch("run_subagent", json.dumps({"task": "anything"}))
    assert "Cancelled" in out.text, out.text
    runtime.bind(should_stop=lambda: False)
    print("ok  subagent: a cancelled parent does not start a child")


def subagent_plan_survival_checks() -> None:
    """The parent's plan must still be in messages[0] after a sub-agent runs.

    The child binds the same ContextVars. Run it in this context rather than a
    copy and the parent comes back holding the child's empty plan — the plan
    vanishes mid-task, which is the failure this whole change exists to stop.
    """
    def script(model, messages, **kwargs):
        system = messages[0]["content"]
        if system.startswith("You are a sub-agent"):
            return _reply("child done")
        if len(messages) <= 2:
            return _reply("", [_call("p1", "plan_write", plan="- [ ] parent's own plan")])
        if len(messages) <= 5:
            return _reply("", [_call("p2", "run_subagent", task="go")])
        assert "parent's own plan" in system, "the plan was lost across the sub-agent call"
        assert runtime.depth() == 0, f"parent's depth was clobbered: {runtime.depth()}"
        return _reply("still on plan")

    agent = Agent(tool_names=["run_subagent", "plan_write"], max_steps=6)
    turn = _run(agent, script)
    assert turn.text == "still on plan", turn
    assert agent.plan["text"] == "- [ ] parent's own plan", agent.plan
    print("ok  subagent: parent's plan and depth survive the call")


def main() -> int:
    plan_basic_checks()
    plan_per_step_checks()
    plan_survives_compaction_checks()
    plan_absent_checks()
    plan_isolation_checks()
    subagent_isolation_checks()
    subagent_approval_checks()
    subagent_toolset_checks()
    subagent_depth_checks()
    subagent_cancel_checks()
    subagent_plan_survival_checks()
    print("\nall long-horizon checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
