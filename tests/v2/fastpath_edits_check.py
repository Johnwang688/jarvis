"""Free checks: the fast path's owner-desk edit tools (2026-10-14).

The owner: "can we allow the jarvis harness to make edits directly and do
stuff instead of being forced to open a task". The decisions are
docs/plans/2026-10-14-fast-path-edits-decisions.md (F-1..F-7); design §2 D2
and §8.1 are amended there. What must hold, in descending order of how badly
it fails:

  **Who holds them (F-1, F-2, F-7).** `write_file`, `edit_file`, `run_command`
  and `run_readonly` (`fastpath.EDIT_TOOLS`) are held only on the owner's own
  chat turn at the HUD — the `DESK_TOOLS` rule. A Discord, DM, schedule,
  escape-hatch or API turn, a task's thread and any other role never carry
  their schemas, dispatch refuses them by name there (invariant 10), and a
  steer from anywhere but the desk drops them at the next step boundary.
  Everything else F-7 lists stays forbidden.

  **The gate (F-3).** Every edit and command goes through the daemon's §6
  permit and its broker: the allowlist and rules' ALLOW run unasked only for a
  human-backed asker; everything else asks the owner; deny, a timeout and a
  `DenyAll` asker refuse; the always-ask rules ask whatever the allowlist
  says; protected state, credentials, env files and owner ops stay refused
  unasked. AUTO means ASK here: the fast path has no reviewer of its own.

  **Where (F-6).** Relative paths and commands land in the thread's project
  folder (`runtime.working_dir()`), and the gate-state check and the `.env`
  scrubber judge them there.

  **The budget and the prompt (F-4, F-5).** Thirty steps on a turn holding the
  edit tools, eight otherwise, exhaustion still a proposal at both; the edit
  wording follows the edit tools, and without them the prompt is the
  read-only text it was before, byte for byte.

Fake model (`llm.chat` scripted), temporary HOME and config, the daemon on
ephemeral loopback ports, no key, nothing of the owner's. The checks written
to bite were each shown to fail against the pre-change code or a targeted
mutation (see the PR).

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/fastpath_edits_check.py
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="jarvis-fastpath-edits-")
ROOT = Path(_TMP.name).resolve()
HOME = ROOT / "home"
HOME.mkdir()
# Before anything computes a default from HOME: every ~ path is the test's.
os.environ["HOME"] = str(HOME)
for _name in [n for n in os.environ if n.startswith("JARVIS_")]:
    os.environ.pop(_name)
os.environ["OPENROUTER_API_KEY"] = "dummy-not-a-key"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402

config.SKILLS_DIR = ROOT / "skills"
config.SKILLS_DIR.mkdir()
config.MEMORY_DIR = ROOT / "memory"
config.AVATAR_ENV = "jarvis"
for _path in (config.ALLOWLIST_PATH, config.SESSIONS_DIR, config.V2_DATA_DIR, config.MODELS_PATH,
              config.PROVIDER_DEFAULTS_PATH, config.MODEL_CACHE_PATH, config.ROUTING_PATH,
              config.V2_ALWAYS_ASK, config.HUD_DIR):
    assert ROOT in Path(_path).resolve().parents, f"{_path} is not the test's"
config.ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

from jarvis import llm, runtime, tools  # noqa: E402
from jarvis.tools import files as v1_files  # noqa: E402
from jarvis.v2.approvals import DenyAll, PendingApprovals  # noqa: E402
from jarvis.v2.model import PermissionProfile, ProviderName, Role, Thread  # noqa: E402
from jarvis.v2.permissions import PermitContext, build_permit  # noqa: E402
from jarvis.v2.provider import Brief, BriefRefused, Decision, EventKind, UserMessage  # noqa: E402
from jarvis.v2.providers import fastpath  # noqa: E402
from jarvis.v2.providers.fastpath import FastPathProvider  # noqa: E402

EDIT = {"write_file", "edit_file", "run_command", "run_readonly"}

# The fast path's own prompt as it was on main before 2026-10-14 (07a152f):
# what every turn without the edit tools must still read, byte for byte.
OLD_FAST_PROMPT = (
    "\n\n## This conversation is the fast path\n\nYou are answering on Jarvis's conversation "
    "fast path: cheap, quick, and\ndeliberately limited. You have no tool that writes a file,\n"
    "runs a command or spawns anything, and you have a budget of 8 steps for this turn. That is "
    "not a mistake in your toolset — it is the design.\n\n**Propose a task** with `task_propose` "
    "when the request needs any one of:\nediting a file, running a command, more than one page "
    "fetched, a browser task beyond the guarded desk tools, a\ndeliverable (a file, a PR, a "
    "report), or more than your step budget. Any one is\nsufficient. Do not attempt such a "
    "request here and do not apologise for the\nlimit — say in one line what you opened and that "
    "it will ask if anything is\nunclear.\n\nOn a desk turn, `hud_browser_act` can perform a "
    "short requested action in a\nvisible Remote Browser pane after a fresh look. The owner's "
    "global and pane\nswitches, Private opt-in, five quiet seconds and action gate must all "
    "permit it.\nSensitive actions ask on the owner's private card. Never drive off desk.\n\n"
    "**Answer directly** when the request is a question you can settle from memory,\nthis "
    "transcript, one read or one fetch; a control request (mute, avatar,\nmusic); or ordinary "
    "conversation. Most turns are this.\n\nWhen you are unsure, propose. A task that turns out to "
    "be small costs a little;\na real job answered as if it were chat costs the owner their "
    "trust in the\nanswer.\n")

# The list FORBIDDEN_TOOLS held before 2026-10-14, minus exactly the four
# edit tools (decision F-7: nothing else leaves it).
FORBIDDEN_AFTER = frozenset({
    "run_subagent", "run_fleet", "git", "git_worktree", "git_pull_request", "plan_write",
    "load_tools", "delegate", "task_start", "task_status", "task_log", "task_cancel",
    "workflow_start", "workflow_status", "workflow_log", "goal_report", "compact_context",
    "whiteboard_close", "skill_write", "memory_delete"})


# --- harness ------------------------------------------------------------------


def reply(text="", calls=None):
    message: dict = {"content": text or None}
    if calls:
        message["tool_calls"] = [
            {"id": f"c{i}", "type": "function",
             "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a)}}
            for i, (n, a) in enumerate(calls)]
    return llm.Reply(message=message, finish_reason="tool_calls" if calls else "stop",
                     model="stub", latency_s=0.0, cost_usd=0.0)


class Script:
    """A fake `llm.chat`: each request's tool names and system message are
    recorded; replies come from `steps` (a callable per request index, or a
    reply), then a plain "done"."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.tools: list[set[str]] = []
        self.systems: list[str] = []
        self.results: list[str] = []

    def __call__(self, model, messages, tools=None, on_delta=None, **kw):
        if tools is None:  # v1's tool-free handoff at the wall
            return reply("DONE: some. OPEN: the rest. NEXT: carry on.")
        self.tools.append({spec["function"]["name"] for spec in tools})
        self.systems.append(messages[0]["content"])
        if messages and messages[-1].get("role") == "tool":
            pass
        self.results = [m["content"] for m in messages if m.get("role") == "tool"]
        index = len(self.tools) - 1
        if index < len(self.steps):
            step = self.steps[index]
            return step() if callable(step) else step
        return reply("done")


@contextlib.contextmanager
def scripted(fake):
    real = llm.chat
    llm.chat = fake
    try:
        yield fake
    finally:
        llm.chat = real


def desk(text="go"):
    """The owner's message typed in the HUD's own window."""
    return UserMessage(text=text, desk=True)


class Asker(PendingApprovals):
    """The daemon's real broker, answered from a script as each card is
    announced. `answers` empty: the card waits out its timeout."""

    def __init__(self, *answers, timeout_s=5.0):
        self.answers = deque(answers)
        self.asked = []
        super().__init__(timeout_s=timeout_s, on_request=self._announced)

    def _announced(self, request):
        self.asked.append(request)
        if self.answers:
            self.resolve(request.req_id, self.answers.popleft())


class RecordingDenyAll(DenyAll):
    def __init__(self):
        super().__init__()
        self.asked = []

    def ask(self, request):
        self.asked.append(request)
        return super().ask(request)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        # The process sits somewhere else, so a relative path that resolved
        # against it would visibly land in the wrong folder.
        self.elsewhere = self.tmp / "elsewhere"
        self.elsewhere.mkdir()
        cwd = os.getcwd()
        os.chdir(self.elsewhere)
        self.addCleanup(os.chdir, cwd)
        config.ALLOWLIST_PATH.write_text("[]\n")
        self.addCleanup(lambda: config.ALLOWLIST_PATH.write_text("[]\n"))
        self.handles = []

    def tearDown(self):
        for provider, handle in self.handles:
            provider.close(handle)

    def allowlist(self, *entries):
        config.ALLOWLIST_PATH.write_text(json.dumps(list(entries)) + "\n")

    def open(self, asker=None, *, cwd=None, profile=PermissionProfile.AUTO, task_id=None,
             role=Role.CHAT, tid="t1", permit=None, allowed_tools=None):
        brief = Brief(role=role, cwd=str(cwd or self.project), profile=profile, task_id=task_id,
                      allowed_tools=allowed_tools)
        if permit is None:
            permit = build_permit(PermitContext(thread_id=tid, brief=brief, provider="fast"),
                                  asker if asker is not None else Asker())
        thread = Thread(id=tid, project_id="p1", role=role, provider=ProviderName.FAST)
        provider = FastPathProvider()
        handle = provider.start(thread, brief, permit)
        self.handles.append((provider, handle))
        return provider, handle

    def turn(self, provider, handle, script, message=None):
        with scripted(script):
            return list(provider.send(handle, message or desk()))

    def finished(self, events):
        return [e for e in events if e.kind is EventKind.TOOL_FINISHED]


# --- who holds them -------------------------------------------------------------


class Boundary(Base):
    def test_edit_tools_are_named_disjoint_and_the_only_dangerous_ones(self):
        self.assertEqual(fastpath.EDIT_TOOLS, frozenset(EDIT))
        self.assertEqual(fastpath.ASKED_TOOLS,
                         frozenset({"write_file", "edit_file", "run_readonly"}))
        self.assertFalse(fastpath.FAST_TOOLS & fastpath.EDIT_TOOLS, "never in the base set")
        self.assertEqual(fastpath.FORBIDDEN_TOOLS, FORBIDDEN_AFTER)
        dangerous = {n for n, t in tools.REGISTRY.items() if t.dangerous}
        self.assertEqual(dangerous & (fastpath.FAST_TOOLS | fastpath.EDIT_TOOLS), {"run_command"})
        for name in fastpath.FAST_TOOLS | fastpath.EDIT_TOOLS:
            self.assertFalse(name.startswith(fastpath.FORBIDDEN_PREFIXES), name)
        for name in ("run_subagent", "run_fleet", "delegate", "git", "git_worktree",
                     "git_pull_request", "task_start", "workflow_start", "load_tools",
                     "skill_write", "memory_delete"):
            self.assertNotIn(name, fastpath.FAST_TOOLS | fastpath.EDIT_TOOLS)

    def test_the_import_guard_still_bites(self):
        saved = (fastpath.FAST_TOOLS, fastpath.EDIT_TOOLS)
        tools.REGISTRY["fastpath_edits_danger"] = tools.Tool(
            name="fastpath_edits_danger", description="stub",
            schema={"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False},
            func=lambda: "ran", dangerous=True)
        try:
            for fast, edit, word in (
                    (saved[0] | {"fastpath_edits_danger"}, saved[1], "dangerous"),
                    (saved[0] | {"run_command"}, saved[1], "run_command"),
                    (saved[0] | {"write_file"}, saved[1], "write_file"),
                    (saved[0], saved[1] | {"run_subagent"}, "run_subagent"),
                    (saved[0], saved[1] | {"no_such_tool_here"}, "no_such_tool_here")):
                fastpath.FAST_TOOLS, fastpath.EDIT_TOOLS = fast, edit
                with self.assertRaises(RuntimeError) as caught:
                    fastpath._validate_toolset()
                self.assertIn(word, str(caught.exception))
        finally:
            fastpath.FAST_TOOLS, fastpath.EDIT_TOOLS = saved
            tools.REGISTRY.pop("fastpath_edits_danger", None)
        fastpath._validate_toolset()

    def test_only_an_owners_chat_brief_may_hold_them(self):
        provider = FastPathProvider()
        chat = Brief(role=Role.CHAT, cwd=str(self.project))
        self.assertLessEqual(EDIT, set(provider._toolset(chat)))
        others = [Brief(role=Role.CHAT, cwd=str(self.project), task_id="t1")]
        others += [Brief(role=role, cwd=str(self.project)) for role in Role if role is not Role.CHAT]
        for brief in others:
            with self.subTest(role=brief.role.value, task=brief.task_id):
                self.assertFalse(EDIT & set(provider._toolset(brief)))
                for name in sorted(EDIT):
                    with self.assertRaises(BriefRefused):
                        provider._toolset(Brief(role=brief.role, cwd=brief.cwd, task_id=brief.task_id,
                                                allowed_tools=["read_file", name]))
        # An owner's chat may pin them — all four together, or none — and
        # never anything past them. A partial pin would be told the
        # read-only wording while holding a tool it denies having (L4).
        self.assertEqual(provider._toolset(Brief(role=Role.CHAT, cwd=str(self.project),
                                                 allowed_tools=["read_file", *sorted(EDIT)])),
                         sorted({"read_file"} | EDIT))
        for partial in (["run_readonly"], ["read_file", "run_command"], ["write_file", "edit_file"]):
            with self.subTest(partial=partial), self.assertRaises(BriefRefused):
                provider._toolset(Brief(role=Role.CHAT, cwd=str(self.project), allowed_tools=partial))
        with self.assertRaises(BriefRefused):
            provider._toolset(Brief(role=Role.CHAT, cwd=str(self.project),
                                    allowed_tools=[*sorted(EDIT), "run_subagent"]))


class Turns(Base):
    def test_the_owners_desk_turn_holds_them_with_thirty_steps(self):
        provider, handle = self.open()
        script = Script()
        self.turn(provider, handle, script, desk())
        self.assertLessEqual(EDIT, script.tools[0])
        self.assertEqual(handle.native.agent.max_steps, fastpath.FAST_EDIT_MAX_STEPS)
        self.assertEqual(fastpath.FAST_EDIT_MAX_STEPS, 30)
        system = script.systems[0]
        self.assertIn("**Do small, direct work yourself**", system)
        self.assertIn("a budget of 30 steps", system)
        self.assertNotIn("You have no tool that writes a file", system)
        self.assertIn(f"Your project folder is `{self.project}`.", system)

    def test_every_other_turn_does_not_and_naming_one_is_refused(self):
        messages = {
            "the HUD API, not its window": UserMessage(text="go"),
            "Discord": UserMessage(text="go", via="discord", desk=True),
            "the DM": UserMessage(text="go", via="dm", desk=True),
            "a schedule": UserMessage(text="go", origin="system", via="system", desk=True),
            "the escape hatch": UserMessage(text="go", origin="owner-ran", desk=True),
        }
        for label, message in messages.items():
            with self.subTest(label):
                provider, handle = self.open(tid=f"t-{len(self.handles)}")
                target = self.project / "made.txt"
                script = Script(reply("", [("write_file", {"path": "made.txt", "content": "x"}),
                                           ("run_command", {"command": "touch ran.txt",
                                                            "reason": "test"})]))
                events = self.turn(provider, handle, script, message)
                self.assertFalse(EDIT & script.tools[0], script.tools[0])
                self.assertEqual(handle.native.agent.max_steps, fastpath.FAST_MAX_STEPS)
                self.assertIn("You have no tool that writes a file", script.systems[0])
                self.assertNotIn("**Do small, direct work yourself**", script.systems[0])
                for result in script.results:
                    self.assertIn("not available to this agent", result)
                self.assertFalse(target.exists())
                self.assertFalse((self.project / "ran.txt").exists())
                self.assertTrue(all(e.data["outcome"] != "ok" for e in self.finished(events)))

    def test_no_project_folder_means_no_edit_tools(self):
        # Review of PR #182, M2: a folder the prompt cannot show (a backtick,
        # a control character) binds nothing, so the handle holds no edit
        # tool — a relative edit would otherwise land in the daemon's cwd.
        # A folder that does not exist binds nothing either.
        odd = self.tmp / "my`proj"
        odd.mkdir()
        for folder in (odd, self.tmp / "missing"):
            with self.subTest(folder=folder.name):
                provider, handle = self.open(cwd=folder, tid=f"t-{len(self.handles)}")
                script = Script(reply("", [("write_file", {"path": "notes.txt", "content": "hi"})]))
                self.turn(provider, handle, script, desk())
                self.assertFalse(EDIT & script.tools[0], script.tools[0])
                self.assertEqual(handle.native.agent.max_steps, fastpath.FAST_MAX_STEPS)
                self.assertIn("not available to this agent", script.results[-1])
                self.assertFalse((self.elsewhere / "notes.txt").exists())
                self.assertFalse((folder / "notes.txt").exists())
                self.assertNotIn("Your project folder", script.systems[0])

    def test_a_task_thread_never_holds_them_even_from_the_desk(self):
        provider, handle = self.open(task_id="task1")
        script = Script()
        self.turn(provider, handle, script, desk())
        self.assertFalse(EDIT & script.tools[0])
        self.assertEqual(handle.native.agent.max_steps, fastpath.FAST_MAX_STEPS)

    def _steering_turn(self, steered: UserMessage):
        provider, handle = self.open()
        real = tools.REGISTRY["get_datetime"].func

        def steering():
            provider.steer(handle, steered)
            return "noon"

        tools.REGISTRY["get_datetime"].func = steering
        self.addCleanup(lambda: tools.REGISTRY["get_datetime"].__setattr__("func", real))
        script = Script(reply("", [("get_datetime", "{}")]),
                        reply("", [("write_file", {"path": "late.txt", "content": "x"})]))
        events = self.turn(provider, handle, script, desk())
        return provider, handle, script, events

    def test_a_steer_from_off_the_desk_drops_them_at_the_next_step(self):
        _provider, handle, script, _events = self._steering_turn(
            UserMessage(text="from my phone", via="discord"))
        self.assertLessEqual(EDIT, script.tools[0])
        self.assertFalse(EDIT & script.tools[1], "the next request carries no edit schema")
        self.assertNotIn("**Do small, direct work yourself**", script.systems[1])
        self.assertIn("You have no tool that writes a file", script.systems[1])
        self.assertIn("not available to this agent", script.results[-1])
        self.assertFalse((self.project / "late.txt").exists())
        self.assertFalse(handle.native.desk["present"])

    def test_a_steer_from_the_desk_keeps_them(self):
        _provider, _handle, script, _events = self._steering_turn(desk("and also this"))
        self.assertLessEqual(EDIT, script.tools[1])


# --- the gate -------------------------------------------------------------------


class Gate(Base):
    def run_command(self, command, asker, **kw):
        provider, handle = self.open(asker, **kw)
        script = Script(reply("", [("run_command", {"command": command, "reason": "test"})]))
        events = self.turn(provider, handle, script)
        return script.results[-1], events

    def test_an_allowlisted_command_runs_unasked(self):
        (self.project / "stale.txt").write_text("old")
        self.allowlist({"tool": "run_command", "prefix": "rm"})
        asker = Asker()
        result, _ = self.run_command("rm stale.txt", asker)
        self.assertEqual(asker.asked, [])
        self.assertIn("[exit 0]", result)
        self.assertFalse((self.project / "stale.txt").exists())

    def test_run_readonly_cannot_read_credentials_unasked(self):
        # Review of PR #182, H1: on a desk turn `run_readonly` read a gh
        # token, the Browser profile and an ssh key behind a project symlink
        # with no card. Each now asks (the owner says no here) or is refused;
        # nothing of the credential comes back.
        (HOME / ".config" / "gh").mkdir(parents=True, exist_ok=True)
        (HOME / ".config" / "gh" / "hosts.yml").write_text(
            "github.com:\n    oauth_token: gho_FAKEFAKEFAKEFAKEFAKEFAKE1234567890\n")
        profile = HOME / ".local" / "share" / "jarvis" / "browser" / "Default"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "History").write_text("visited https://bank.example/?session=SESSIONSECRET\n")
        (HOME / ".ssh").mkdir(exist_ok=True)
        (HOME / ".ssh" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nKEYMATERIAL\n")
        (self.project / "notes").symlink_to(HOME / ".ssh" / "id_ed25519")
        for command, marker in (("cat ~/.config/gh/hosts.yml", "gho_FAKE"),
                                (f"grep -ra session {profile.parent}", "SESSIONSECRET"),
                                ("cat notes", "KEYMATERIAL"),
                                ("cat note?", "KEYMATERIAL"),
                                ("grep -r oauth ~/.config", "gho_FAKE"),
                                ("head -5 ./notes", "KEYMATERIAL")):
            with self.subTest(command):
                asker = Asker(Decision.DENY)
                provider, handle = self.open(asker, tid=f"t-{len(self.handles)}")
                script = Script(reply("", [("run_readonly", {"command": command})]))
                events = self.turn(provider, handle, script)
                result = script.results[-1]
                self.assertNotIn(marker, result)
                self.assertNotEqual(self.finished(events)[0].data["outcome"], "ok", result)
                self.assertLessEqual(len(asker.asked), 1)

    def test_ordinary_reads_still_run_unasked(self):
        # `ls` and `cat` are rules' ALLOW. `git status` is rules' ASK (git is
        # not a read-only program here), so it runs unasked once the owner's
        # allowlist covers `git`, as on every other surface.
        import subprocess
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "app.py").write_text("print('hi')\n")
        self.allowlist({"tool": "run_command", "prefix": "git"})
        asker = Asker()
        provider, handle = self.open(asker)
        script = Script(reply("", [("run_readonly", {"command": "ls"})]),
                        reply("", [("run_readonly", {"command": "cat app.py"})]),
                        reply("", [("run_command", {"command": "git status --short",
                                                    "reason": "look"})]))
        self.turn(provider, handle, script)
        self.assertEqual(asker.asked, [])
        ls, cat, status = script.results[:3]
        self.assertIn("app.py", ls)
        self.assertIn("print('hi')", cat)
        self.assertIn("app.py", status)

    def test_run_readonly_judges_what_it_reads_on_every_surface(self):
        # The same judge with nothing bound (v1's surfaces): the process's
        # own folder places a relative word.
        from jarvis.tools import shell
        (HOME / ".ssh").mkdir(exist_ok=True)
        (HOME / ".ssh" / "id_ed25519").write_text("KEYMATERIAL\n")
        (self.elsewhere / "k").symlink_to(HOME / ".ssh" / "id_ed25519")
        (self.elsewhere / "plain.txt").write_text("fine\n")
        for command in ("cat ~/.ssh/id_ed25519", "cat k", "ls ~/.ssh", "grep -r x ~"):
            with self.subTest(command):
                self.assertIn("credential folder", shell.run_readonly(command))
        self.assertIn("fine", shell.run_readonly("cat plain.txt"))
        self.assertIn("fine", shell.run_readonly("grep -r fine ."))

    def _read_fixture(self):
        (HOME / ".ssh").mkdir(exist_ok=True)
        (HOME / ".ssh" / "id_test").write_text("FAKEKEY-SECRET\n")
        link = self.project / "link"
        if not link.is_symlink():
            link.symlink_to(HOME / ".ssh")
        (self.project / "a.txt").write_text("hello x\n")
        (self.project / "src").mkdir(exist_ok=True)
        (self.project / "src" / "m.py").write_text("# TODO foo\n")

    # Review 2 of PR #182: three ways past the operand judge, each a leak.
    BYPASSES = (
        # parameter expansion
        "cat $HOME/.ssh/id_test", "cat ${HOME}/.ssh/id_test", 'cat "$HOME/.ssh/id_test"',
        # grep's other spellings of recursion, over a folder holding ~/.ssh
        "grep -d recurse FAKE ~", "grep -drecurse FAKE ~", "grep -id recurse FAKE ~",
        "grep --directories=recurse FAKE ~", "grep --directories recurse FAKE ~",
        "grep --dir=recurse FAKE ~", "grep --recur FAKE ~", "grep -d rec FAKE ~",
        # following a nested symlink (`link -> ~/.ssh`)
        "grep -R FAKE .", "grep -rR FAKE .", "grep --dereference-recursive FAKE .",
        "grep --deref FAKE .", "rg -L FAKE .", "rg -iL FAKE .", "rg --follow FAKE .",
    )
    ORDINARY = (("grep -rn foo src/", "TODO foo"), ("rg TODO", "TODO foo"),
                ("grep -i 'x$' a.txt", "hello"), ("ls -la", "a.txt"), ("cat a.txt", "hello"),
                # plain recursion does not follow a symlink below what it names
                ("grep -r hello .", "hello"), ("rg hello .", "hello"))

    def test_run_readonly_bypasses_are_closed_with_nothing_bound(self):
        from jarvis.tools import shell
        self._read_fixture()
        os.chdir(self.project)
        for command in self.BYPASSES:
            with self.subTest(command):
                out = shell.run_readonly(command)
                self.assertNotIn("FAKEKEY", out)
                self.assertTrue(out.startswith("Error:"), out)
        for command, expected in self.ORDINARY:
            with self.subTest(command):
                out = shell.run_readonly(command)
                self.assertIn(expected, out)
                self.assertNotIn("FAKEKEY", out)

    def test_run_readonly_bypasses_are_closed_on_a_desk_turn(self):
        self._read_fixture()
        for command in self.BYPASSES:
            with self.subTest(command):
                asker = Asker(Decision.ALLOW)   # an owner who would say yes
                provider, handle = self.open(asker, tid=f"t-{len(self.handles)}")
                script = Script(reply("", [("run_readonly", {"command": command})]))
                self.turn(provider, handle, script)
                self.assertNotIn("FAKEKEY", script.results[-1])
        asker = Asker()
        provider, handle = self.open(asker, tid="t-ordinary")
        script = Script(*[reply("", [("run_readonly", {"command": c})]) for c, _ in self.ORDINARY])
        self.turn(provider, handle, script)
        self.assertEqual(asker.asked, [], "ordinary reads run unasked")
        for (command, expected), result in zip(self.ORDINARY, script.results):
            with self.subTest(command):
                self.assertIn(expected, result)
                self.assertNotIn("FAKEKEY", result)

    def test_the_daemons_own_keys_are_scrubbed_from_a_read(self):
        # `run_readonly env` prints the process environment, which in the
        # daemon holds its provider keys (open on v1 too before this PR).
        key = "sk-or-v1-" + "a1b2c3d4" * 8
        saved = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = key
        self.addCleanup(os.environ.__setitem__, "OPENROUTER_API_KEY", saved)
        # `env` is not rules' ALLOW, so it is put to the owner; say yes.
        asker = Asker(Decision.ALLOW)
        provider, handle = self.open(asker)
        script = Script(reply("", [("run_readonly", {"command": "env"})]))
        self.turn(provider, handle, script)
        self.assertEqual([r.command for r in asker.asked], ["env"])
        self.assertIn("OPENROUTER_API_KEY=", script.results[-1])
        self.assertNotIn(key, script.results[-1])
        self.assertIn("PATH=", script.results[-1], "ordinary settings are not withheld")

    def test_a_rules_allow_command_runs_unasked_in_the_project_folder(self):
        asker = Asker()
        result, _ = self.run_command("touch made.txt", asker)
        self.assertEqual(asker.asked, [])
        self.assertTrue((self.project / "made.txt").exists())
        self.assertFalse((self.elsewhere / "made.txt").exists())

    def test_an_unlisted_command_asks_and_the_owner_decides(self):
        target = self.project / "f.txt"
        for answer, expected in ((Decision.DENY, "a"), (Decision.ALLOW, "b")):
            with self.subTest(answer.value):
                target.write_text("a\n")
                asker = Asker(answer)
                result, events = self.run_command("sed -i s/a/b/ f.txt", asker,
                                                  tid=f"t-{answer.value}")
                self.assertEqual(len(asker.asked), 1)
                card = asker.asked[0]
                self.assertEqual((card.tool, card.command, card.thread_id),
                                 ("run_command", "sed -i s/a/b/ f.txt", f"t-{answer.value}"))
                self.assertTrue(card.allowlistable)
                self.assertEqual(target.read_text().strip(), expected)
                if answer is Decision.DENY:
                    self.assertTrue(result.startswith("The user declined"), result)
                    self.assertEqual(self.finished(events)[0].data["outcome"], "denied")

    def test_an_unanswered_approval_times_out_to_a_denial(self):
        (self.project / "f.txt").write_text("a\n")
        asker = Asker(timeout_s=0.2)
        result, _ = self.run_command("sed -i s/a/b/ f.txt", asker)
        self.assertEqual(len(asker.asked), 1)
        self.assertTrue(result.startswith("The user declined"), result)
        self.assertEqual((self.project / "f.txt").read_text(), "a\n")
        self.assertEqual(asker.decisions[-1]["resolution"], "timeout")

    def test_a_deny_all_asker_denies_even_what_rules_allow(self):
        self.allowlist({"tool": "run_command", "prefix": "rm"})
        (self.project / "stale.txt").write_text("old")
        for command, made in (("touch made.txt", "made.txt"), ("rm stale.txt", None)):
            with self.subTest(command):
                asker = RecordingDenyAll()
                result, _ = self.run_command(command, asker, tid=f"t-{len(self.handles)}")
                self.assertTrue(result.startswith("The user declined"), result)
                if made:
                    self.assertFalse((self.project / made).exists())
        self.assertTrue((self.project / "stale.txt").exists())

    def test_auto_has_no_reviewer_here_so_it_asks(self):
        (self.project / "f.txt").write_text("a\n")
        asker = Asker(Decision.DENY)
        self.run_command("sed -i s/a/b/ f.txt", asker, profile=PermissionProfile.AUTO)
        self.assertEqual(len(asker.asked), 1, "AUTO must not let an unlisted command run unasked")
        self.assertEqual((self.project / "f.txt").read_text(), "a\n")
        self.assertEqual(asker.asked[0].layer, "human")

    def test_an_always_ask_rule_asks_whatever_the_allowlist_says(self):
        self.allowlist({"tool": "run_command", "prefix": "vercel"})
        asker = Asker(Decision.DENY)
        result, _ = self.run_command("vercel --prod", asker)
        self.assertEqual(len(asker.asked), 1)
        self.assertEqual(asker.asked[0].layer, "always-ask")
        self.assertIn("vercel-prod", asker.asked[0].reason)
        self.assertTrue(result.startswith("The user declined"), result)

    def test_an_edit_asks_and_the_allowlist_can_cover_it(self):
        target = self.project / "notes" / "a.txt"
        for answer in (Decision.DENY, Decision.ALLOW):
            with self.subTest(answer.value):
                asker = Asker(answer)
                provider, handle = self.open(asker, tid=f"t-{answer.value}")
                script = Script(reply("", [("write_file", {"path": "notes/a.txt",
                                                           "content": "hello\n"})]))
                self.turn(provider, handle, script)
                self.assertEqual(len(asker.asked), 1)
                self.assertEqual(asker.asked[0].tool, "write_file")
                self.assertEqual(target.exists(), answer is Decision.ALLOW)
        # read, then edit — the edit asks too.
        asker = Asker(Decision.ALLOW)
        provider, handle = self.open(asker, tid="t-edit")
        script = Script(reply("", [("read_file", {"path": "notes/a.txt"})]),
                        reply("", [("edit_file", {"path": "notes/a.txt", "old_string": "hello",
                                                  "new_string": "bye"})]))
        self.turn(provider, handle, script)
        self.assertEqual([r.tool for r in asker.asked], ["edit_file"])
        self.assertEqual(target.read_text(), "bye\n")
        # The owner's "always" on an edit is a standing yes for the tool.
        self.allowlist({"tool": "write_file"})
        asker = Asker()
        provider, handle = self.open(asker, tid="t-listed")
        self.turn(provider, handle, Script(reply("", [("write_file", {"path": "b.txt",
                                                                      "content": "b"})])))
        self.assertEqual(asker.asked, [])
        self.assertEqual((self.project / "b.txt").read_text(), "b")

    def test_protected_state_credentials_env_files_and_owner_ops_stay_refused(self):
        self.allowlist()
        allowlist_before = config.ALLOWLIST_PATH.read_text()
        (HOME / ".ssh").mkdir(exist_ok=True)
        calls = [
            ("write_file", {"path": str(config.ALLOWLIST_PATH), "content": '[{"tool": "run_command"}]'}),
            ("write_file", {"path": ".env", "content": "API_TOKEN=x"}),
            ("write_file", {"path": ".env.local", "content": "API_TOKEN=x"}),
            ("write_file", {"path": "~/.ssh/authorized_keys", "content": "ssh-ed25519 AAAA"}),
            ("write_file", {"path": str(config.MODELS_PATH), "content": "{}"}),
            ("run_command", {"command": "echo API_TOKEN=x > .env", "reason": "t"}),
            ("run_command", {"command": f"cp /dev/null {config.ALLOWLIST_PATH}", "reason": "t"}),
            ("run_command", {"command": "jarvis hud install", "reason": "t"}),
        ]
        # Every call is put to an owner who says yes to everything: what is
        # refused is refused before anyone could be asked — except a write
        # into a credential folder, which the §6 permit puts to the owner
        # (always-ask `credentials-path`, as for Claude's Write) and v1's
        # write tool then refuses whatever the owner said.
        for name, args in calls:
            with self.subTest(name=name, args=args):
                asker = Asker(*([Decision.ALLOW] * 3))
                provider, handle = self.open(asker, tid=f"t-{len(self.handles)}")
                script = Script(reply("", [(name, args)]))
                events = self.turn(provider, handle, script)
                if ".ssh" in str(args.get("path", "")):
                    self.assertEqual([r.layer for r in asker.asked], ["always-ask"])
                    self.assertIn("credential folder", script.results[-1])
                else:
                    self.assertEqual(asker.asked, [], "refused, never asked")
                self.assertNotEqual(self.finished(events)[0].data["outcome"], "ok",
                                    script.results[-1])
        self.assertEqual(config.ALLOWLIST_PATH.read_text(), allowlist_before)
        for name in (".env", ".env.local"):
            self.assertFalse((self.project / name).exists())
        self.assertFalse((HOME / ".ssh" / "authorized_keys").exists())
        self.assertFalse(config.MODELS_PATH.exists())

    def test_a_relative_gate_file_is_judged_in_the_project_folder(self):
        # A thread whose folder holds the gate's own state: `allowlist.json`
        # written relatively is the allowlist, and is refused as one — by the
        # write tool, by `run_command`'s own check and by dispatch's verdict,
        # all judging in the folder the command runs in.
        folder = config.ALLOWLIST_PATH.parent
        before = config.ALLOWLIST_PATH.read_text()
        for name, args in (
                ("run_command", {"command": "echo '[{\"tool\": \"run_command\"}]' > allowlist.json",
                                 "reason": "t"}),
                ("write_file", {"path": "allowlist.json", "content": "[]"})):
            with self.subTest(name):
                asker = Asker(*([Decision.ALLOW] * 3))
                provider, handle = self.open(asker, cwd=folder, tid=f"t-{len(self.handles)}")
                script = Script(reply("", [(name, args)]))
                self.turn(provider, handle, script)
                self.assertEqual(asker.asked, [])
                self.assertEqual(config.ALLOWLIST_PATH.read_text(), before, script.results[-1])
        self.assertFalse((self.elsewhere / "allowlist.json").exists())

    def test_the_gate_state_check_judges_where_the_command_runs(self):
        # Beneath the permit (which judges in the brief's folder itself):
        # dispatch's own verdict and `run_command`'s own refusal read the
        # bound working folder, so even an approver that says yes to
        # everything cannot have a relative `allowlist.json` written there.
        import contextvars
        before = config.ALLOWLIST_PATH.read_text()

        def attempt():
            runtime.bind(workdir=str(config.ALLOWLIST_PATH.parent))
            with runtime.tools_bound(["run_command"]):
                return tools.dispatch(
                    "run_command",
                    json.dumps({"command": "echo '[{\"tool\": \"run_command\"}]' > allowlist.json",
                                "reason": "t"}),
                    approve=lambda tool, args: True).text

        result = contextvars.copy_context().run(attempt)
        self.assertTrue(result.startswith("Refused:"), result)
        self.assertEqual(config.ALLOWLIST_PATH.read_text(), before)

    def test_a_symlinked_project_is_judged_where_it_really_is(self):
        # Review of PR #182, M1: a project folder that is a symlink into the
        # gate's own folder. `cd ..` leaves the *physical* parent, so a line
        # judged on the logical path (`<link>/../jarvis/allowlist.json`)
        # looked harmless and overwrote the allowlist.
        cfg = config.ALLOWLIST_PATH.parent
        link = self.tmp / "linked-project"
        link.symlink_to(cfg)
        evil = self.tmp / "evil.json"
        evil.write_text('[{"tool": "run_command", "prefix": "rm"}]\n')
        before = config.ALLOWLIST_PATH.read_text()
        asker = Asker(*([Decision.ALLOW] * 3))
        provider, handle = self.open(asker, cwd=link)
        self.assertEqual(handle.native.workdir, os.path.realpath(link))
        for command in (f"cd .. && cp {evil} {cfg.name}/allowlist.json",
                        f"cd .. && echo x > {cfg.name}/allowlist.json"):
            with self.subTest(command):
                script = Script(reply("", [("run_command", {"command": command, "reason": "t"})]))
                self.turn(provider, handle, script)
                self.assertEqual(config.ALLOWLIST_PATH.read_text(), before, script.results[-1])

    def test_a_yes_after_stop_does_not_run(self):
        # Review of PR #182, L1: the owner presses Stop, then approves the
        # card. The broker's wait takes no cancel, so the card is answered;
        # what it approved must still not run.
        class StopThenAllow(PendingApprovals):
            def __init__(self):
                super().__init__(timeout_s=5, on_request=self.announced)
                self.asked = []

            def announced(self, request):
                self.asked.append(request)
                self.provider.interrupt(self.handle)
                self.resolve(request.req_id, Decision.ALLOW)

        asker = StopThenAllow()
        provider, handle = self.open(asker)
        asker.provider, asker.handle = provider, handle
        script = Script(reply("", [("run_command", {"command": "sed -i s/a/b/ f.txt",
                                                    "reason": "t"})]))
        (self.project / "f.txt").write_text("a\n")
        events = self.turn(provider, handle, script)
        self.assertEqual(len(asker.asked), 1)
        self.assertEqual((self.project / "f.txt").read_text(), "a\n")
        done = [e for e in events if e.kind is EventKind.TURN_FINISHED][0]
        self.assertEqual(done.data["stop"], "interrupted")

    def test_the_approver_denies_off_the_desk_before_asking(self):
        seen = []

        def permit(name, args, brief):
            seen.append(name)
            return Decision.ALLOW

        approve = FastPathProvider._approver(Brief(role=Role.CHAT, cwd=str(self.project)), permit)
        self.assertFalse(getattr(approve, "jarvis_human_backed", False),
                         "rules ALLOW must not stand in for the permit")
        entry = tools.REGISTRY["run_command"]
        with runtime.turn_scope(desk={"present": False}):
            self.assertFalse(approve(entry, {"command": "ls"}))
        self.assertEqual(seen, [])
        slot = {"present": True}

        def leaves(name, args, brief):
            slot["present"] = False  # a steer from elsewhere while the card waited
            return Decision.ALLOW

        approve = FastPathProvider._approver(Brief(role=Role.CHAT, cwd=str(self.project)), leaves)
        with runtime.turn_scope(desk=slot):
            self.assertFalse(approve(entry, {"command": "ls"}))
        with runtime.turn_scope(desk={"present": True}):
            approve = FastPathProvider._approver(Brief(role=Role.CHAT, cwd=str(self.project)), permit)
            self.assertTrue(approve(entry, {"command": "ls"}))

    def test_answer_still_refuses(self):
        provider, handle = self.open()
        with self.assertRaises(ValueError) as caught:
            provider.answer(handle, "r1", Decision.ALLOW)
        self.assertIn("/approvals", str(caught.exception))


# --- where ------------------------------------------------------------------------


class Workdir(Base):
    def test_relative_paths_and_commands_land_in_the_project_folder(self):
        self.allowlist({"tool": "write_file"})
        asker = Asker()
        provider, handle = self.open(asker)
        script = Script(
            reply("", [("write_file", {"path": "notes/a.txt", "content": "needle\n"})]),
            reply("", [("read_file", {"path": "notes/a.txt"})]),
            reply("", [("grep_files", {"pattern": "needle", "path": "notes", "mode": "files"})]),
            reply("", [("list_dir", {})]),
            reply("", [("run_command", {"command": "pwd", "reason": "where"})]),
            reply("", [("run_readonly", {"command": "ls notes"})]),
        )
        self.turn(provider, handle, script)
        self.assertEqual(asker.asked, [])
        self.assertEqual((self.project / "notes" / "a.txt").read_text(), "needle\n")
        self.assertFalse((self.elsewhere / "notes").exists())
        read, grep, listing, pwd, ls = script.results[1:6]
        self.assertIn("needle", read)
        self.assertIn("a.txt", grep)
        self.assertIn("notes/", listing)
        self.assertEqual(pwd.splitlines()[0], str(self.project))
        self.assertIn("a.txt", ls)

    def test_a_relative_read_of_a_credential_symlink_is_refused(self):
        # Review of PR #182, L3: the read tools place a relative path in the
        # project before judging it, so a symlink inside the project that
        # points into ~/.ssh is refused as the credential it is.
        (HOME / ".ssh").mkdir(exist_ok=True)
        (HOME / ".ssh" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nKEYMATERIAL\n")
        (self.project / "notes").symlink_to(HOME / ".ssh" / "id_ed25519")
        provider, handle = self.open()
        script = Script(reply("", [("read_file", {"path": "notes"})]))
        self.turn(provider, handle, script)
        self.assertNotIn("KEYMATERIAL", script.results[-1])
        self.assertIn("credential folder", script.results[-1])

    def test_with_nothing_bound_every_path_is_as_written(self):
        self.assertIsNone(runtime.working_dir())
        self.assertEqual(v1_files.in_working_dir("notes/a.txt"), "notes/a.txt")
        self.assertEqual(v1_files.in_working_dir("~/x"), "~/x")
        from jarvis.tools import shell
        self.assertEqual(shell._run("pwd").splitlines()[0], str(self.elsewhere))
        with runtime.turn_scope():
            runtime.bind(workdir="relative/only")
            self.assertIsNone(runtime.working_dir(), "only an absolute folder is a place")

    def test_the_scrubber_reads_the_project_folders_env(self):
        secret = "q7Hk2mPz9Xw4Rt6Yb8Nc3Vd5"
        (self.project / ".env").write_text(f"API_TOKEN={secret}\n")
        (self.project / "notes.txt").write_text(f"the token is {secret}\n")
        asker = Asker()
        provider, handle = self.open(asker)
        script = Script(reply("", [("run_command", {"command": "cat notes.txt", "reason": "t"})]))
        self.turn(provider, handle, script)
        self.assertEqual(asker.asked, [])
        self.assertIn("the token is", script.results[-1])
        self.assertNotIn(secret, script.results[-1])


# --- the budget and the prompt ------------------------------------------------------


class Budget(Base):
    def exhaust(self, message):
        provider, handle = self.open(tid=f"t-{len(self.handles)}")
        script = Script(*[reply("", [("get_datetime", "{}")])] * 40)
        events = self.turn(provider, handle, script, message)
        done = [e for e in events if e.kind is EventKind.TURN_FINISHED][0]
        return len([e for e in events if e.kind is EventKind.TOOL_STARTED]), done

    def test_thirty_at_the_desk_eight_elsewhere_and_both_propose(self):
        for message, steps in ((desk("migrate everything"), 30),
                               (UserMessage(text="migrate everything", via="discord"), 8)):
            with self.subTest(steps=steps):
                calls, done = self.exhaust(message)
                self.assertEqual(calls, steps)
                self.assertEqual(done.data["stop"], "max_turns")
                self.assertTrue(done.data["proposal"]["brief"].startswith(
                    "[fast path ran out of steps] "), done.data)

    def test_the_budget_is_chosen_per_turn(self):
        provider, handle = self.open()
        self.turn(provider, handle, Script(), desk())
        self.assertEqual(handle.native.agent.max_steps, 30)
        self.turn(provider, handle, Script(), UserMessage(text="hi", via="discord"))
        self.assertEqual(handle.native.agent.max_steps, 8)
        self.turn(provider, handle, Script(), desk())
        self.assertEqual(handle.native.agent.max_steps, 30)


class Prompt(Base):
    def test_without_the_edit_tools_it_reads_as_before_byte_for_byte(self):
        full = config.SYSTEM_PROMPT + fastpath.FAST_PROMPT
        before = config.SYSTEM_PROMPT + OLD_FAST_PROMPT
        for held in (fastpath.FAST_TOOLS, fastpath.FAST_TOOLS - fastpath.DESK_TOOLS):
            self.assertEqual(config.narrow_prompt(full, held), config.narrow_prompt(before, held))

    def test_with_them_it_says_do_small_work_and_propose_large(self):
        text = config.narrow_prompt(config.SYSTEM_PROMPT + fastpath.FAST_PROMPT,
                                    fastpath.FAST_TOOLS | fastpath.EDIT_TOOLS)
        for needle in ("**Do small, direct work yourself**", "a budget of 30 steps",
                       "Keep the owner's uncommitted changes intact",
                       "Do not\ncommit, push or deploy unless the owner asked",
                       "a large or multi-file change", "land as a pull\nrequest"):
            self.assertIn(needle, text)
        for needle in ("You have no tool that writes a file", "editing a file, running a command"):
            self.assertNotIn(needle, text)
        # Partial edit toolsets are read-only: every paragraph needs all four.
        partial = config.narrow_prompt(config.SYSTEM_PROMPT + fastpath.FAST_PROMPT,
                                       fastpath.FAST_TOOLS | {"run_readonly"})
        self.assertIn("You have no tool that writes a file", partial)
        self.assertNotIn("run_readonly", partial.split("## This conversation is the fast path")[1])

    def test_the_prompt_follows_each_desk_transition(self):
        provider, handle = self.open()
        native = handle.native
        for at in (False, True, False, True):
            provider._turn_tools(native, at)
            held = {s["function"]["name"] for s in native.agent.tool_specs}
            self.assertEqual(EDIT <= held, at)
            self.assertEqual("**Do small, direct work yourself**" in native.agent._base_system, at)
            self.assertEqual(native.agent._base_system,
                             config.narrow_prompt(native.pristine_system, held))

    def test_the_numbers_are_one_source(self):
        self.assertEqual((fastpath.FAST_MAX_STEPS, fastpath.FAST_EDIT_MAX_STEPS),
                         (config.FAST_STEPS, config.FAST_EDIT_STEPS))
        self.assertIn(f"budget of {config.FAST_STEPS} steps", config.FAST_READ_ONLY_LIMITS)
        self.assertIn(f"budget of {config.FAST_EDIT_STEPS} steps",
                      " ".join(config.FAST_LIMITS_PARAGRAPH.split()))


# --- end to end through the daemon ------------------------------------------------


class Daemon(Base):
    """The real daemon, its real §6 permit and broker, and a real fast path:
    an edit on the owner's desk turn becomes a card on the bus for that
    thread, the broker's own resolve answers it (what POST /approvals/{id}
    calls), and the edit lands in the project folder."""

    def setUp(self):
        super().setUp()
        from jarvis.v2 import daemon as mod
        from jarvis.v2.stores import Stores
        self.stores = Stores()
        self.d = mod.Daemon(self.stores, {ProviderName.FAST: FastPathProvider()}, None, 0)
        self.d.start()
        self.addCleanup(self.d.stop)
        self.proj = self.stores.projects.create("edits", str(self.project))
        self.events = self.d.bus.subscribe()
        self.addCleanup(lambda: self.d.bus.unsubscribe(self.events))

    def wait(self, predicate, what, timeout=5.0):
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"{what} did not happen")
            time.sleep(0.01)

    def test_an_edit_reaches_the_owner_through_the_broker(self):
        thread = self.d.open_thread(self.proj.id, Role.CHAT, ProviderName.FAST,
                                    Brief(Role.CHAT, str(self.project)))
        script = Script(reply("", [("write_file", {"path": "hello.txt", "content": "hi\n"})]),
                        reply("written"))
        with scripted(script):
            started = self.d.deliver(thread.id, desk("write hello.txt"))
            self.assertEqual(started["status"], "started")
            pending = self.wait(self.d.approvals.pending, "the approval card")
            self.assertEqual(len(pending), 1)
            card = pending[0]
            self.assertEqual((card.tool, card.thread_id, card.layer),
                             ("write_file", thread.id, "human"))
            self.assertFalse((self.project / "hello.txt").exists())
            seen = []

            def published():
                while True:
                    try:
                        seen.append(self.events.get_nowait())
                    except Exception:
                        return [e for e in seen if e.get("kind") == "approval_requested"]

            requested = self.wait(published, "approval_requested on the bus")
            self.assertEqual(requested[0]["thread_id"], thread.id)
            self.assertEqual(requested[0]["data"]["req_id"], card.req_id)
            self.d.approvals.resolve(card.req_id, Decision.ALLOW, hud=True)
            self.wait(lambda: (self.project / "hello.txt").exists(), "the edit")

            def idle():
                with self.d._lock:
                    session = self.d._sessions.get(thread.id)
                    return session is None or session.worker is None
            self.wait(idle, "the turn to finish")
        self.assertEqual((self.project / "hello.txt").read_text(), "hi\n")
        self.assertFalse((self.elsewhere / "hello.txt").exists())


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        _TMP.cleanup()
