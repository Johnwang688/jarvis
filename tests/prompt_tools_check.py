"""Each surface's prompt names only tools that surface holds. Free — no API.

Invariant 7's rule is "never describe a tool the model cannot call". The
shared system prompt (`config.SYSTEM_PROMPT`) is written for the full
conversation toolset and names plan_write, run_subagent, task_start,
write_file and more — tools the v2 fast path, workflows, tasks, goals and the
benches do not hold. Before PR #34 a named tool the agent lacked still ran if
it was not dangerous; since #34 dispatch enforces the toolset, so each such
mention is a refused step (owner's answer 5, 2026-10-11: "fix it").

What must hold:

  * every surface — the CLI, Discord, the face's voice agent and designer, the
    v2 fast path, a workflow, an attended task, a goal, every sub-agent type
    under every parent that can spawn one (a type that would lose a tool its
    brief names is not sent, and says so), agent-bench's five tasks, cad-bench,
    long-bench (shell and no-shell) and vocab-bench — names in its system
    message and its working-context block only tools it holds. Each agent is
    the one that surface really builds (its own start path, `llm.chat`
    scripted), and the block is rendered with something in every index: a
    skill, a saved session, a background task, a plan and an available group;
  * the owner's named cases: the fast path names none of task_start,
    workflow_start, plan_write, run_subagent or write_file; tasks, goals and
    workflows do not name task_start;
  * `config.system_prompt` with every tool held is SYSTEM_PROMPT byte for byte
    (the wording is tuned), every paragraph that names a tool has a rule, each
    rule matches exactly one paragraph, and no version names a tool it does not
    require.

Run against origin/main (b54e84c), every surface but the four conversation
agents, the designer and vocab-bench fails, and the `config` checks fail for
want of `config.system_prompt`.

Run:  PYTHONPATH=. .venv/bin/python tests/prompt_tools_check.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_TMP = tempfile.TemporaryDirectory(prefix="jarvis-prompt-tools-")
TMP = Path(_TMP.name)
(TMP / "home").mkdir()
os.environ["HOME"] = str(TMP / "home")  # before config computes any default

from jarvis import config  # noqa: E402

config.ALLOWLIST_PATH = TMP / "allowlist.json"
config.MODELS_PATH = TMP / "models.json"
config.PROVIDER_DEFAULTS_PATH = TMP / "provider_defaults.json"
config.MODEL_CACHE_PATH = TMP / "catalog.json"
config.ROUTING_PATH = TMP / "routing.json"
config.CODEX_CATALOG_PATH = TMP / "codex_catalog.json"
config.DISCORD_GUILD_PATH = TMP / "discord_guild.json"
config.SPILL_DIR = TMP / "spill"
config.SESSIONS_DIR = TMP / "sessions"
config.MEMORY_DIR = TMP / "memory"
config.SKILLS_DIR = TMP / "skills"
config.GOALS_DIR = TMP / "goals"
config.GIT_WORKTREES_DIR = TMP / "worktrees"
config.DESIGNS_DIR = TMP / "designs"
config.VOICES_DIR = TMP / "voices"
config.AVATAR_STATE_PATH = TMP / "avatar.json"
config.AVATAR_ENV = "jarvis"
config.SPOTIFY_TOKEN_PATH = TMP / "spotify_token.json"
for _d in ("memory", "skills", "sessions", "goals", "spill", "designs"):
    (TMP / _d).mkdir(exist_ok=True)
# Something in every index, so each block renders and its wording is judged.
(TMP / "skills" / "probe").mkdir()
(TMP / "skills" / "probe" / "SKILL.md").write_text(
    "---\nname: probe\ndescription: Use when probing.\n---\nDo the probe.\n", encoding="utf-8")
# The Spotify group available, so the "more tools" pointer renders.
config.SPOTIFY_TOKEN_PATH.write_text(json.dumps({"refresh_token": "x"}), encoding="utf-8")

from jarvis import agent as agent_mod  # noqa: E402
from jarvis import agents, goals, llm, runtime, sessions, tasks, tools, workflows  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def reply(text="done"):
    return llm.Reply(message={"content": text}, finish_reason="stop", model="stub",
                     latency_s=0.0)


llm.chat = lambda *a, **k: reply()  # every agent here answers at once
llm.complete = lambda *a, **k: reply()  # titlers and summarizers, if any run

BUILT: list[agent_mod.Agent] = []
_RealAgent = agent_mod.Agent


class Recording(_RealAgent):
    """The real Agent, remembered — so each surface's own start path builds it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        BUILT.append(self)


agent_mod.Agent = Recording


def deny(*args, **kwargs) -> bool:
    return False


def built_by(start) -> agent_mod.Agent:
    before = len(BUILT)
    start()
    assert len(BUILT) > before, "the surface built no agent"
    return BUILT[before]


def wait_for(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


_NAME = {}


def mentions(text: str) -> set[str]:
    """Registered tool names the text names, as whole words."""
    found = set()
    for name in tools.REGISTRY:
        pattern = _NAME.get(name)
        if pattern is None:
            pattern = _NAME[name] = re.compile(
                r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
        if pattern.search(text):
            found.add(name)
    return found


def held_by(agent) -> set[str]:
    return {spec["function"]["name"] for spec in agent.tool_specs}


def prompt_of(agent) -> str:
    """What the agent is told: its system message and its working-context
    block, rendered with something in every index."""
    agent.plan["text"] = "- [ ] probe the prompt"
    agent._refresh_system()
    return agent_mod.durable_state(agent.messages)


# --- the surfaces -------------------------------------------------------------


def surfaces() -> dict[str, agent_mod.Agent]:
    from jarvis import __main__ as cli
    from jarvis import agentbench, cadbench, discord_agent, vocabbench
    from jarvis.face import server
    from jarvis.goalrunner import GoalRunner
    from jarvis.tools import subagent
    from jarvis.v2.model import ProviderName, Role, Thread
    from jarvis.v2.provider import Brief, Decision
    from jarvis.v2.providers.fastpath import FastPathProvider
    from longbench import harness

    class Broker:
        def approver(self, remote=False):
            return deny

    found: dict[str, agent_mod.Agent] = {}
    found["CLI chat"] = cli._make_agent(None, None)
    found["Discord agent"] = discord_agent.DiscordResponder(Broker(), None)._default_agent()
    server.set_session(sessions.new("face"))
    found["face voice agent"] = server._get_agent()
    found["face designer"] = server._get_designer()

    thread = Thread(id="pt1", project_id="p1", role=Role.CHAT, provider=ProviderName.FAST)
    handle = FastPathProvider().start(thread, Brief(role=Role.CHAT, cwd=str(TMP)),
                                      lambda name, args, brief: Decision.ALLOW)
    found["v2 fast path"] = handle.native.agent

    def workflow():
        wf = workflows.start("[prompt] a workflow")
        wait_for(lambda: wf.status != "running", "the workflow")

    def task():
        tsk = tasks.start("[prompt] a task", approve=deny)
        wait_for(lambda: tsk.status != "running", "the task")

    found["workflow"] = built_by(workflow)
    found["attended task"] = built_by(task)  # also leaves a task in the block
    runner = GoalRunner(approver=deny, notify=lambda text: None, announce=lambda text: None)
    found["goal"] = runner._default_agent(goals.create("[prompt] a goal"))

    for agent_task in agentbench.TASKS:
        found[f"agent-bench {agent_task.name}"] = built_by(
            lambda t=agent_task: agentbench.run_task("stub", t))

    saved = (cadbench.find_channel, cadbench.create_assembly, cadbench.delete_element,
             cadbench.read_assembly)
    ids = ("1" * 24, "2" * 24, "3" * 24, "JHD")  # tests/cadbench_check.py's part
    cadbench.find_channel = lambda: {
        "did": ids[0], "vid": ids[1], "eid": ids[2], "pid": ids[3],
        "name": "1 x 2 x 1 x 35 Aluminum C-Channel (276-2289)", "number": "276-2289",
        "bbox": {"lo": (-0.5, 0.0, 0.0), "hi": (0.5, 17.5, 0.55)},
        "length_in": 17.5, "width_in": 1.0, "ref": ":".join(ids),
    }
    cadbench.create_assembly = lambda name: "asm"
    cadbench.delete_element = lambda eid: None
    cadbench.read_assembly = lambda eid: []
    try:
        found["cad-bench"] = built_by(lambda: cadbench.run_task("stub", cadbench.TASKS[0]))
    finally:
        (cadbench.find_channel, cadbench.create_assembly, cadbench.delete_element,
         cadbench.read_assembly) = saved

    workspace = TMP / "longbench"
    workspace.mkdir()
    for no_shell in (False, True):
        label = "long-bench" + (" --no-shell" if no_shell else "")
        found[label] = built_by(lambda n=no_shell: harness.run_jarvis(
            ["[prompt] hello"], workspace, "stub", 3, no_shell=n))

    found["vocab-bench"] = _RealAgent(system=vocabbench.SYSTEM,
                                      tool_names=vocabbench.SNAPSHOT_TOOLS)

    # Every sub-agent type, under every parent here that can spawn one: a
    # child's toolset is its type's intersected with its parent's. A type that
    # would lose a tool its brief names is not sent at all (REFUSED).
    for parent_label in list(found):
        parent = found[parent_label]
        if not held_by(parent) & {"run_subagent", "run_fleet"}:
            continue
        for type_name in agents.TYPES:
            before = len(BUILT)
            job = agents.Job(task="[prompt] a job", type=type_name)
            with _bound(held_by(parent)):
                subagent._run_one(job)
            label = f"sub-agent {type_name} < {parent_label}"
            if len(BUILT) > before:
                found[label] = BUILT[before]
            else:
                REFUSED[label] = job.error
    return found


REFUSED: dict[str, str] = {}


class _bound:
    """A parent's toolset bound for the duration — runtime.tools_bound, spelled
    out so this file also runs on a tree without it (origin/main)."""

    def __init__(self, names):
        self.names = frozenset(names)

    def __enter__(self):
        self.token = runtime._TOOLS.set(self.names)

    def __exit__(self, *exc):
        runtime._TOOLS.reset(self.token)


def surface_checks(found) -> None:
    print("\nevery surface names only tools it holds")
    for label, agent in found.items():
        extra = sorted(mentions(prompt_of(agent)) - held_by(agent))
        check(f"{label}: names only its own tools", not extra, f"names {extra}")


def subagent_checks() -> None:
    print("\nsub-agent types")
    for name, agent_type in agents.TYPES.items():
        needs = set(getattr(agent_type, "needs", ()))
        named = mentions(agent_type.prompt())
        check(f"{name}: its brief names only the tools it needs", named <= needs,
              f"names {sorted(named - needs)}")
        check(f"{name}: it needs only tools of its own", needs <= set(agent_type.tools))
    for label, error in REFUSED.items():
        check(f"{label}: not sent, and says which tool it lacks",
              "does not hold" in error and "not sent" in error, error)
    check("a builder under a parent with no git tools is not sent",
          "sub-agent builder < long-bench" in REFUSED, str(sorted(REFUSED)))


def owner_case_checks(found) -> None:
    print("\nthe owner's named cases")
    fast = prompt_of(found["v2 fast path"])
    named = sorted(mentions(fast) & {"task_start", "workflow_start", "plan_write",
                                     "run_subagent", "write_file"})
    check("the fast path names none of task_start, workflow_start, plan_write, "
          "run_subagent, write_file", not named, f"names {named}")
    for label in ("attended task", "goal", "workflow"):
        check(f"{label} does not name task_start",
              "task_start" not in mentions(prompt_of(found[label])))
    # What the narrowing keeps, so a surface is not left without the rules
    # that still apply to it.
    check("the fast path keeps the untrusted-content rule and its fence",
          "Only the user speaks for the user." in fast
          and "fetch_page returns" in fast and "[untrusted web" in fast)
    bench = prompt_of(found["agent-bench project"])
    check("a bench with no web keeps the untrusted-content rule, without the fence",
          "Only the user speaks for the user." in bench and "[untrusted web" not in bench)
    check("cad-bench is still told about skills it can read",
          "skill_read" in prompt_of(found["cad-bench"]))


def config_checks() -> None:
    print("\nconfig.system_prompt")
    narrow = getattr(config, "system_prompt", None)
    rules = getattr(config, "PROMPT_TOOL_PARAGRAPHS", None)
    check("config.system_prompt exists", narrow is not None and rules is not None)
    if narrow is None or rules is None:
        return
    every = set(tools.REGISTRY)
    check("with every tool held it is SYSTEM_PROMPT byte for byte",
          narrow(every) == config.SYSTEM_PROMPT)
    check("with no toolset given it is SYSTEM_PROMPT", narrow(None) == config.SYSTEM_PROMPT)
    default = _RealAgent(approve=deny)
    check("the default conversation agent's system message is SYSTEM_PROMPT",
          default.messages[0]["content"] == config.SYSTEM_PROMPT)

    paragraphs = config.SYSTEM_PROMPT.split("\n\n")
    for opening in rules:
        hits = [p for p in paragraphs if p.startswith(opening)]
        check(f"rule {opening!r} matches exactly one paragraph", len(hits) == 1, str(len(hits)))
    for paragraph in paragraphs:
        named = mentions(paragraph)
        versions = config.prompt_rule(paragraph)
        if not named:
            continue
        label = paragraph[:40]
        check(f"{label!r}… names tools and has a rule", versions is not None, str(sorted(named)))
        if versions is None:
            continue
        for needs, text in versions:
            body = paragraph if text is None else text
            stray = sorted(mentions(body) - set(needs))
            check(f"{label!r}… version needing {sorted(needs)} names only those", not stray,
                  f"names {stray}")
    check("a toolset with none of them keeps every paragraph that names no tool",
          all(p in narrow(set()) for p in paragraphs if not mentions(p)
              and config.prompt_rule(p) is None))


def main() -> int:
    config_checks()
    found = surfaces()
    surface_checks(found)
    subagent_checks()
    owner_case_checks(found)
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    print("all prompt tool checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
