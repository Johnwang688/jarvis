"""Checks for the v2 ClaudeProvider. Free — no `claude` process, no tokens.

The whole suite runs against a fake client injected through
`claude._client_factory`, scripted with the **real** SDK dataclasses from the
installed `claude-agent-sdk`. Constructing the genuine types is the point: a
hand-rolled stand-in would keep passing after the SDK renamed a field, which is
the failure this provider is most exposed to — it is a translation layer and
almost nothing else.

Four things are under test, in descending order of how badly they fail:

  **The gate (§6, R2).** Under `auto` the `PreToolUse` hook is the only thing
  that sees every tool call, so a hook that does not fire, does not reach
  `permit`, or reports an allow where the owner said no is the whole permission
  system quietly not working. `allowed_tools` must stay empty forever, because
  an entry there is auto-approved *before* any callback (R1).

  **The escape hatch (§6.1).** A classifier refusal has to come out as
  `REVIEWER_DECLINED` carrying the exact command, *and* as a
  `TOOL_FINISHED{ok: False}` — a refused call must never render as one that
  happened.

  **The event stream (§5.2).** A surface must not be able to tell which
  provider produced an event, so the order and the `data` keys are asserted
  exactly, and the generator must terminate on every path including a raise.

  **The credential rule.** The child process holds the owner's login. No
  event, reason or exception string may carry a credential value — checked by
  grepping every string the provider produced for a planted fake token.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/claude_provider_check.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    PermissionResultAllow,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk import UserMessage as SdkUserMessage  # noqa: E402

from jarvis.v2.model import PermissionProfile, ProviderName, Role, Thread  # noqa: E402
from jarvis.v2.provider import (  # noqa: E402
    Brief,
    BriefRefused,
    Decision,
    EventKind,
    SessionLost,
    UserMessage,
)
from jarvis.v2.providers import claude  # noqa: E402

FAKE_TOKEN = "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKEFAKEFAKE0123456789"
SESSION_ID = "11111111-2222-3333-4444-555555555555"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok  {label}")
    else:
        print(f"FAIL {label}")
        _failures.append(label)


def eq(got, want, label: str) -> None:
    check(got == want, f"{label} (got {got!r}, want {want!r})")


# --- the fake client --------------------------------------------------------


class Hook:
    """A scripted point where the CLI would call the PreToolUse hook."""

    def __init__(self, tool_name: str, tool_input: dict, tool_use_id: str = "call-1"):
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.tool_use_id = tool_use_id


class Permission:
    """A scripted point where the CLI would call `can_use_tool`."""

    def __init__(self, tool_name: str, tool_input: dict):
        self.tool_name = tool_name
        self.tool_input = tool_input


class Raise:
    def __init__(self, exc: BaseException):
        self.exc = exc


class Pause:
    """Block the scripted stream until `event` is set (used for interrupt)."""

    def __init__(self, event: threading.Event):
        self.event = event


class FakeClient:
    """Everything `ClaudeSDKClient` is to this provider, and nothing more."""

    instances: list["FakeClient"] = []

    def __init__(self, options, script=None):
        self.options = options
        self.script = list(script or [])
        self.queries: list = []
        self.hook_results: list = []
        self.permission_results: list = []
        self.connected = False
        self.interrupts = 0
        self.disconnects = 0
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def query(self, prompt, session_id: str = "default"):
        if hasattr(prompt, "__aiter__"):
            self.queries.append([msg async for msg in prompt])
        else:
            self.queries.append(prompt)

    async def interrupt(self):
        self.interrupts += 1

    async def disconnect(self):
        self.disconnects += 1
        self.connected = False

    async def receive_response(self):
        script, self.script = self.script, []
        for item in script:
            if isinstance(item, Raise):
                raise item.exc
            if isinstance(item, Pause):
                # Polled, not waited on in an executor thread: a parked
                # non-daemon thread is joined at interpreter exit, and a suite
                # that hangs on shutdown is a suite nobody runs.
                while not item.event.is_set():
                    await asyncio.sleep(0.02)
                continue
            if isinstance(item, Hook):
                matcher = self.options.hooks["PreToolUse"][0]
                data = {
                    "hook_event_name": "PreToolUse",
                    "tool_name": item.tool_name,
                    "tool_input": item.tool_input,
                    "tool_use_id": item.tool_use_id,
                }
                self.hook_results.append(
                    await matcher.hooks[0](data, item.tool_use_id, {"signal": None})
                )
                continue
            if isinstance(item, Permission):
                self.permission_results.append(
                    await self.options.can_use_tool(
                        item.tool_name, item.tool_input, {"signal": None}
                    )
                )
                continue
            yield item


@contextlib.contextmanager
def fake(script=None):
    """Install the fake factory for the duration of a check."""
    original = claude._client_factory
    FakeClient.instances = []
    claude._client_factory = lambda options: FakeClient(options, script)
    try:
        yield
    finally:
        claude._client_factory = original


def thread(thread_id: str = "t1", session_id: str | None = None) -> Thread:
    return Thread(
        id=thread_id,
        project_id="p1",
        role=Role.IMPLEMENTER,
        provider=ProviderName.CLAUDE,
        provider_session_id=session_id,
    )


def brief(**kw) -> Brief:
    kw.setdefault("role", Role.IMPLEMENTER)
    kw.setdefault("cwd", "/tmp/jarvis-wp3")
    return Brief(**kw)


def allow(*_a, **_k) -> Decision:
    return Decision.ALLOW


def result_message(**kw) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id=SESSION_ID,
        total_cost_usd=0.25,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 40,
        },
    )
    base.update(kw)
    return ResultMessage(**base)


def drain(provider, handle, text: str = "hello") -> list:
    return list(provider.send(handle, UserMessage(text=text)))


def kinds(events) -> list[str]:
    return [e.kind.value for e in events]


def strings(events) -> str:
    """Every string any event carries, for the credential grep."""
    return json.dumps([[e.kind.value, e.data] for e in events], default=str)


# --- health -----------------------------------------------------------------


@contextlib.contextmanager
def credentials(payload):
    """Point the provider at a throwaway credential file, never the owner's."""
    with tempfile.TemporaryDirectory(prefix="jarvis-wp3-creds-") as tmp:
        path = Path(tmp) / ".credentials.json"
        if payload is not None:
            path.write_text(json.dumps(payload))
        previous = os.environ.get("JARVIS_CLAUDE_CREDENTIALS")
        os.environ["JARVIS_CLAUDE_CREDENTIALS"] = str(path)
        try:
            yield path
        finally:
            if previous is None:
                os.environ.pop("JARVIS_CLAUDE_CREDENTIALS", None)
            else:
                os.environ["JARVIS_CLAUDE_CREDENTIALS"] = previous


@contextlib.contextmanager
def cli(version: str | None, *, on_path: bool = True, returncode: int = 0):
    """A fake `claude` binary: no process is ever spawned."""
    which, run = claude.shutil.which, claude.subprocess.run
    claude.shutil.which = lambda name: "/fake/bin/claude" if on_path else None

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, returncode, f"{version} (Claude Code)\n", "")

    claude.subprocess.run = fake_run
    try:
        yield
    finally:
        claude.shutil.which = which
        claude.subprocess.run = run


def health_checks() -> None:
    print("\n-- health (no process, throwaway credentials)")
    provider = claude.ClaudeProvider()
    future = int((time.time() + 86_400) * 1000)
    past = int((time.time() - 86_400) * 1000)

    with cli("2.1.240", on_path=False), credentials({"claudeAiOauth": {"expiresAt": future}}):
        ok, reason = provider.health()
        check(not ok and "PATH" in reason, f"missing binary is unavailable: {reason}")

    with cli("1.9.0"), credentials({"claudeAiOauth": {"expiresAt": future}}):
        ok, reason = provider.health()
        check(
            not ok and claude.CLAUDE_PIN in reason and "1.9.0" in reason,
            f"wrong version names both: {reason}",
        )

    with cli("2.1.240"), credentials({"claudeAiOauth": {"expiresAt": future}}):
        ok, reason = provider.health()
        check(ok and "2.1.240" in reason, f"patch drift inside the pinned series is ok: {reason}")

    with cli("2.1.240"), credentials(
        {"claudeAiOauth": {"expiresAt": past, "refreshTokenExpiresAt": past}}
    ):
        ok, reason = provider.health()
        check(not ok and "expired" in reason, f"expired login is unavailable: {reason}")

    with cli("2.1.240"), credentials(
        # The access token lapses every hour and is refreshed silently; the
        # grant is what "logged in" means. A health check that cried wolf
        # hourly would be one nobody reads.
        {"claudeAiOauth": {"expiresAt": past, "refreshTokenExpiresAt": future}}
    ):
        ok, reason = provider.health()
        check(ok, f"a lapsed access token is not an expired login: {reason}")

    with cli("2.1.240"), credentials({"claudeAiOauth": {"subscriptionType": "max"}}):
        ok, reason = provider.health()
        check(ok and "unknown" in reason, f"absent expiry is unknown, not refused: {reason}")

    with cli("2.1.240"), credentials(None):
        ok, reason = provider.health()
        check(not ok and "login" in reason, f"no bundle at all is unavailable: {reason}")

    with cli("2.1.240"), credentials({"claudeAiOauth": {"expiresAt": future}}):
        ok, _ = provider.health()
        check(ok, "binary + pin + live login is available")

    # Never spends tokens: the only subprocess is `--version`.
    seen: list = []
    with cli("2.1.240"), credentials({"claudeAiOauth": {"expiresAt": future}}):
        run = claude.subprocess.run

        def spy(argv, **kw):
            seen.append(list(argv))
            return run(argv, **kw)

        claude.subprocess.run = spy
        provider.health()
    check(
        len(seen) == 1 and seen[0][1:] == ["--version"],
        f"health spawns only `claude --version`: {seen}",
    )


# --- options ----------------------------------------------------------------


def options_checks() -> None:
    print("\n-- options built from the brief")
    provider = claude.ClaudeProvider()

    with fake():
        handle = provider.start(
            thread(),
            brief(
                system_append="ROLE BRIEF",
                model="claude-opus-4-5",
                effort="high",
                max_turns=12,
                mcp_servers={"jarvis": {"type": "stdio", "command": "jarvis-mcp"}},
            ),
            allow,
        )
        options = FakeClient.instances[0].options
        eq(
            options.system_prompt,
            {"type": "preset", "preset": "claude_code", "append": "ROLE BRIEF"},
            "system prompt is the preset with the role brief appended",
        )
        eq(options.setting_sources, ["user", "project"], "setting sources load CLAUDE.md")
        eq(options.allowed_tools, [], "allowed_tools stays empty (R1: it bypasses the gate)")
        eq(options.permission_mode, "auto", "AUTO maps to permission_mode auto")
        check(options.can_use_tool is None, "AUTO installs no can_use_tool (R2: never consulted)")
        matchers = options.hooks["PreToolUse"]
        check(
            len(matchers) == 1 and matchers[0].matcher is None,
            "one PreToolUse matcher, matching every tool",
        )
        check(
            matchers[0].timeout >= 600,
            f"hook timeout is at least 600s (is {matchers[0].timeout})",
        )
        eq(options.cwd, "/tmp/jarvis-wp3", "cwd is the task worktree")
        eq(options.model, "claude-opus-4-5", "model comes from the brief")
        eq(options.effort, "high", "effort comes from the brief")
        eq(options.max_turns, 12, "max_turns comes from the brief")
        check(options.strict_mcp_config is True, "strict MCP config")
        eq(
            options.mcp_servers,
            {"jarvis": {"type": "stdio", "command": "jarvis-mcp"}},
            "MCP servers come from the brief",
        )
        check(options.include_partial_messages is True, "partial messages on, for TEXT_DELTA")
        eq(options.session_id, handle.provider_session_id, "start mints the session id it reports")
        check(options.resume is None, "start does not resume")
        provider.close(handle)

    with fake():
        handle = provider.start(thread(), brief(profile=PermissionProfile.ASK), allow)
        options = FakeClient.instances[0].options
        eq(options.permission_mode, "default", "ASK maps to permission_mode default")
        check(options.can_use_tool is not None, "ASK installs can_use_tool")
        check("PreToolUse" in (options.hooks or {}), "ASK keeps the hook: always-ask is layer 2")
        provider.close(handle)

    with fake():
        try:
            provider.start(thread(), brief(profile=PermissionProfile.STRICT), allow)
            check(False, "STRICT is refused")
        except BriefRefused as exc:
            check("strict" in str(exc).lower(), f"STRICT is refused: {exc}")

    with fake():
        try:
            provider.start(thread(), brief(effort="turbo"), allow)
            check(False, "an off-ladder effort is refused")
        except BriefRefused as exc:
            check("turbo" in str(exc), f"an off-ladder effort is refused: {exc}")

    with fake():
        handle = provider.start(thread(), brief(allowed_tools=["Bash", "Read"]), allow)
        options = FakeClient.instances[0].options
        eq(options.tools, ["Bash", "Read"], "a role toolset restricts `tools`, never `allowed_tools`")
        eq(options.allowed_tools, [], "...and still auto-approves nothing")
        provider.close(handle)

    with fake():
        handle = provider.resume(thread(session_id=SESSION_ID), brief(), allow)
        options = FakeClient.instances[0].options
        eq(options.resume, SESSION_ID, "resume passes the thread's session id")
        check(options.session_id is None, "resume does not also mint one")
        eq(handle.provider_session_id, SESSION_ID, "the handle keeps the resumed id")
        provider.close(handle)

    with fake():
        try:
            provider.resume(thread(session_id=None), brief(), allow)
            check(False, "resume without a session id is refused")
        except BriefRefused as exc:
            check("resume" in str(exc), f"resume without a session id is refused: {exc}")


# --- the event stream -------------------------------------------------------


def sequence_checks() -> None:
    print("\n-- the event sequence for one scripted turn")
    provider = claude.ClaudeProvider()
    script = [
        SystemMessage(subtype="init", data={"session_id": SESSION_ID}),
        StreamEvent(
            uuid="u1",
            session_id=SESSION_ID,
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}},
        ),
        StreamEvent(
            uuid="u2",
            session_id=SESSION_ID,
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
        ),
        StreamEvent(
            uuid="u3",
            session_id=SESSION_ID,
            event={"type": "message_start", "message": {}},
        ),
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="weighing it up", signature="sig"),
                TextBlock(text="Hello"),
                ToolUseBlock(id="call-1", name="Bash", input={"command": "echo hi"}),
            ],
            model="claude-opus-4-5",
            session_id=SESSION_ID,
        ),
        SdkUserMessage(
            content=[ToolResultBlock(tool_use_id="call-1", content="hi\n", is_error=False)]
        ),
        result_message(),
    ]
    with fake(script):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        eq(
            kinds(events),
            [
                "turn_started",
                "text_delta",
                "text_delta",
                "thinking",
                "text",
                "tool_started",
                "tool_finished",
                "usage",
                "turn_finished",
            ],
            "the exact event sequence",
        )
        by_kind = {e.kind.value: e for e in events}
        deltas = [e.data["text"] for e in events if e.kind is EventKind.TEXT_DELTA]
        eq(deltas, ["Hel", "lo"], "deltas arrive in order and carry only their text")
        check(
            len(deltas) == 2,
            "a non-text stream event (message_start) produces nothing",
        )
        eq(by_kind["text"].data, {"text": "Hello"}, "TEXT is the finished block")
        eq(
            by_kind["tool_started"].data,
            {"call_id": "call-1", "name": "Bash", "args": {"command": "echo hi"}},
            "TOOL_STARTED keys match the sibling providers",
        )
        eq(
            by_kind["tool_finished"].data,
            # "hi", not "hi\n": every string goes through the scrub, and v1's
            # `_drop_attributed_lines` rejoins on newlines. A ticker summary
            # can lose a trailing newline; a credential may not survive one.
            {"call_id": "call-1", "name": "Bash", "ok": True, "summary": "hi"},
            "TOOL_FINISHED is keyed to the call id",
        )
        usage = by_kind["usage"].data
        eq(
            {k: usage[k] for k in ("input", "output", "cached", "cost_usd")},
            {"input": 100, "output": 20, "cached": 40, "cost_usd": 0.25},
            "USAGE carries the SDK's own figures",
        )
        eq(
            usage["provider_reported"]["cache_read_input_tokens"],
            40,
            "the raw usage dict rides along",
        )
        eq(by_kind["turn_finished"].data, {"stop": "end"}, "a clean turn ends")
        check(
            all(e.thread_id == "t1" for e in events),
            "every event carries the thread id",
        )
        eq(FakeClient.instances[0].queries, ["hello"], "text-only turns go as a plain prompt")
        provider.close(handle)

    print("\n-- tool results that did not succeed")
    with fake(
        [
            AssistantMessage(
                content=[ToolUseBlock(id="c2", name="Read", input={"file_path": "/nope"})],
                model="m",
                session_id=SESSION_ID,
            ),
            SdkUserMessage(
                content=[ToolResultBlock(tool_use_id="c2", content="ENOENT", is_error=True)]
            ),
            result_message(),
        ]
    ):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        finished = [e for e in events if e.kind is EventKind.TOOL_FINISHED][0]
        check(finished.data["ok"] is False, "an is_error result is not ok")
        provider.close(handle)

    print("\n-- images ride the turn as content blocks")
    with fake([result_message()]):
        handle = provider.start(thread(), brief(), allow)
        list(
            provider.send(
                handle,
                UserMessage(text="look", images=[{"b64": "QUJD", "mime": "image/png"}]),
            )
        )
        sent = FakeClient.instances[0].queries[0][0]
        eq(
            sent["message"]["content"],
            [
                {"type": "text", "text": "look"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                },
            ],
            "text and image become one multimodal user message",
        )
        provider.close(handle)


# --- the gate ---------------------------------------------------------------


def hook_checks() -> None:
    print("\n-- the PreToolUse hook is the gate (§6, R2)")
    provider = claude.ClaudeProvider()
    seen: list = []

    def permit(name, args, b):
        seen.append((name, dict(args), b.profile))
        return Decision.ALLOW if name == "Read" else Decision.DENY

    script = [
        Hook("Read", {"file_path": "/x"}, "call-r"),
        Hook("Bash", {"command": "rm -rf /"}, "call-b"),
        result_message(),
    ]
    with fake(script):
        handle = provider.start(thread(), brief(), permit)
        events = drain(provider, handle)
        client = FakeClient.instances[0]
        eq(
            [(n, a) for n, a, _ in seen],
            [("Read", {"file_path": "/x"}), ("Bash", {"command": "rm -rf /"})],
            "permit sees the tool name and the tool input",
        )
        eq(seen[0][2], PermissionProfile.AUTO, "permit sees the brief")
        eq(client.hook_results[0], {}, "ALLOW under auto returns {} so the classifier still runs")
        denial = client.hook_results[1]["hookSpecificOutput"]
        eq(denial["hookEventName"], "PreToolUse", "the deny names its event")
        eq(denial["permissionDecision"], "deny", "DENY is an explicit deny")
        check(
            "Bash" in denial["permissionDecisionReason"],
            f"the model is told what was denied: {denial['permissionDecisionReason']}",
        )
        approval = [e for e in events if e.kind is EventKind.APPROVAL_REQUESTED]
        resolved = [e for e in events if e.kind is EventKind.APPROVAL_RESOLVED]
        eq(len(approval), 2, "each question is announced, so a stalled turn is visible")
        eq(
            [e.data["tool"] for e in approval], ["Read", "Bash"], "the announcement names the tool"
        )
        eq(
            approval[1].data["command"],
            "rm -rf /",
            "a shell call carries its whole command, never a summary",
        )
        eq([e.data["decision"] for e in resolved], ["allow", "deny"], "and each is resolved")
        eq(
            [e.data["req_id"] for e in approval],
            [e.data["req_id"] for e in resolved],
            "request and resolution share one id",
        )
        provider.close(handle)

    print("\n-- ASK: the hook allows explicitly, and can_use_tool backs it up")
    with fake([Hook("Bash", {"command": "ls"}), Permission("Bash", {"command": "ls"}), result_message()]):
        handle = provider.start(thread(), brief(profile=PermissionProfile.ASK), allow)
        drain(provider, handle)
        client = FakeClient.instances[0]
        decision = client.hook_results[0]["hookSpecificOutput"]
        eq(
            decision["permissionDecision"],
            "allow",
            "under ask layer 3 is skipped, so ALLOW is explicit",
        )
        check(
            isinstance(client.permission_results[0], PermissionResultAllow),
            "can_use_tool answers with the SDK's own allow type",
        )
        provider.close(handle)

    print("\n-- a raising permit denies")
    with fake([Hook("Bash", {"command": "ls"}), result_message()]):
        def boom(*_a, **_k):
            raise RuntimeError("broker unreachable")

        handle = provider.start(thread(), brief(), boom)
        drain(provider, handle)
        out = FakeClient.instances[0].hook_results[0]["hookSpecificOutput"]
        eq(out["permissionDecision"], "deny", "an unreachable broker denies, as everywhere")
        provider.close(handle)


def answer_checks() -> None:
    print("\n-- answer() resolves a blocked question out of band")
    provider = claude.ClaudeProvider()
    released = threading.Event()
    asked = threading.Event()

    def permit(name, args, b):
        asked.set()
        released.wait(10)
        return Decision.ALLOW

    with fake([Hook("Bash", {"command": "ls"}), result_message()]):
        handle = provider.start(thread(), brief(), permit)
        events = []
        stream = provider.send(handle, UserMessage(text="go"))
        events.append(next(stream))  # turn_started
        request = next(stream)  # approval_requested
        eq(request.kind.value, "approval_requested", "the question is announced before the wait")
        check(asked.wait(5), "permit is called on a worker thread, not the session's loop")
        provider.answer(handle, request.data["req_id"], Decision.DENY)
        rest = list(stream)
        released.set()
        resolved = [e for e in rest if e.kind is EventKind.APPROVAL_RESOLVED][0]
        eq(resolved.data["decision"], "deny", "the owner's out-of-band answer wins the race")
        out = FakeClient.instances[0].hook_results[0]["hookSpecificOutput"]
        eq(out["permissionDecision"], "deny", "...and reaches the hook")
        try:
            provider.answer(handle, request.data["req_id"], Decision.ALLOW)
            check(False, "a spent request id is dead")
        except ValueError:
            check(True, "a spent request id is dead")
        provider.close(handle)


# --- the escape hatch -------------------------------------------------------


def reviewer_declined_checks() -> None:
    print("\n-- a classifier refusal is the D7 escape hatch (§6.1)")
    provider = claude.ClaudeProvider()
    declined = (
        claude.CLASSIFIER_DENIAL_PREFIX
        + ". Reason: [Auto-Mode Bypass]. If you have other tasks that don't depend "
        "on this action, continue working on those."
    )
    script = [
        AssistantMessage(
            content=[ToolUseBlock(id="c9", name="Bash", input={"command": "vercel --prod"})],
            model="m",
            session_id=SESSION_ID,
        ),
        SdkUserMessage(
            content=[ToolResultBlock(tool_use_id="c9", content=declined, is_error=True)]
        ),
        result_message(),
    ]
    with fake(script):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        eq(
            kinds(events),
            ["turn_started", "tool_started", "reviewer_declined", "tool_finished", "usage", "turn_finished"],
            "REVIEWER_DECLINED arrives with the TOOL_FINISHED, never instead of it",
        )
        decline = [e for e in events if e.kind is EventKind.REVIEWER_DECLINED][0]
        eq(decline.data["tool"], "Bash", "the decline names the tool")
        eq(
            decline.data["command"],
            "vercel --prod",
            "the whole command travels with it — the owner must see it to approve it",
        )
        eq(decline.data["args"], {"command": "vercel --prod"}, "and its arguments")
        eq(decline.data["reason"], "[Auto-Mode Bypass]", "the classifier's own reason is extracted")
        finished = [e for e in events if e.kind is EventKind.TOOL_FINISHED][0]
        check(
            finished.data["ok"] is False,
            "a refused call must never render as one that happened",
        )
        provider.close(handle)

    print("\n-- an ordinary error is not a reviewer decline")
    with fake(
        [
            AssistantMessage(
                content=[ToolUseBlock(id="c9", name="Bash", input={"command": "false"})],
                model="m",
                session_id=SESSION_ID,
            ),
            SdkUserMessage(
                content=[ToolResultBlock(tool_use_id="c9", content="exit 1", is_error=True)]
            ),
            result_message(),
        ]
    ):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        check(
            not any(e.kind is EventKind.REVIEWER_DECLINED for e in events),
            "a failing command is not a policy decision",
        )
        provider.close(handle)


# --- control ----------------------------------------------------------------


def interrupt_checks() -> None:
    print("\n-- interrupt")
    provider = claude.ClaudeProvider()
    gate = threading.Event()
    with fake(
        [
            Pause(gate),
            result_message(subtype="success", is_error=False, terminal_reason="aborted_streaming"),
        ]
    ):
        handle = provider.start(thread(), brief(), allow)
        stream = provider.send(handle, UserMessage(text="go"))
        eq(next(stream).kind.value, "turn_started", "the turn starts")

        def cancel():
            time.sleep(0.05)
            provider.interrupt(handle)
            gate.set()

        threading.Thread(target=cancel, daemon=True).start()
        rest = list(stream)
        eq(rest[-1].data["stop"], "interrupted", "a cancelled turn finishes as interrupted")
        eq(FakeClient.instances[0].interrupts, 1, "the SDK's own interrupt is sent")
        provider.close(handle)

    print("\n-- stop reasons")
    for kw, want in (
        (dict(subtype="error_max_turns", is_error=True), "max_turns"),
        (dict(subtype="success", is_error=True), "error"),
        (dict(subtype="success", is_error=False, terminal_reason="max_turns"), "max_turns"),
        (dict(subtype="success", is_error=False), "end"),
    ):
        with fake([result_message(**kw)]):
            handle = provider.start(thread(), brief(), allow)
            events = drain(provider, handle)
            eq(events[-1].data["stop"], want, f"{kw} finishes as {want}")
            provider.close(handle)


def error_checks() -> None:
    print("\n-- a raising client")
    provider = claude.ClaudeProvider()
    with fake([Raise(RuntimeError("the CLI died"))]):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        eq(kinds(events), ["turn_started", "error"], "a raise becomes one fatal error and stops")
        check(events[-1].data["fatal"] is True, "the error is fatal")
        check("the CLI died" in events[-1].data["message"], "and says what happened")
        provider.close(handle)

    print("\n-- a closed session, and a second concurrent turn")
    with fake([result_message()]):
        handle = provider.start(thread(), brief(), allow)
        provider.close(handle)
        events = drain(provider, handle)
        eq(kinds(events), ["error"], "sending on a closed session is one fatal error")
        provider.close(handle)
        check(FakeClient.instances[0].disconnects == 1, "close is idempotent")

    gate = threading.Event()
    with fake([Pause(gate), result_message()]):
        handle = provider.start(thread(), brief(), allow)
        first = provider.send(handle, UserMessage(text="a"))
        next(first)
        second = list(provider.send(handle, UserMessage(text="b")))
        eq(kinds(second), ["error"], "a second concurrent turn on one handle is refused")
        gate.set()
        list(first)
        provider.close(handle)

    print("\n-- a failed start leaves nothing running")
    before = threading.active_count()

    def explode(options):
        raise RuntimeError("no binary")

    original = claude._client_factory
    claude._client_factory = explode
    try:
        try:
            provider.start(thread(), brief(), allow)
            check(False, "a failed connect raises BriefRefused")
        except BriefRefused:
            check(True, "a failed connect raises BriefRefused")
    finally:
        claude._client_factory = original
    time.sleep(0.2)
    check(
        threading.active_count() <= before,
        f"...and leaks no loop thread ({before} -> {threading.active_count()})",
    )


def usage_checks() -> None:
    print("\n-- usage() sums the turns since start")
    provider = claude.ClaudeProvider()
    with fake():
        handle = provider.start(thread(), brief(), allow)
        client = FakeClient.instances[0]
        eq(provider.usage(handle).cost_usd, None, "a session with no turn has no cost")
        for _ in range(2):
            client.script = [result_message()]
            drain(provider, handle)
        total = provider.usage(handle)
        eq(total.input_tokens, 200, "input tokens sum")
        eq(total.output_tokens, 40, "output tokens sum")
        eq(total.cached_tokens, 80, "cached tokens sum")
        eq(round(total.cost_usd, 4), 0.5, "the equivalent cost sums")
        eq(total.work_tokens, (200 - 80) + 40, "work_tokens is §8.3's figure")
        provider.close(handle)

    with fake([result_message(total_cost_usd=None)]):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        usage = [e for e in events if e.kind is EventKind.USAGE][0]
        check(usage.data["cost_usd"] is None, "a missing cost stays None, never 0")
        provider.close(handle)


def isolation_checks() -> None:
    print("\n-- two handles are isolated")
    provider = claude.ClaudeProvider()
    seen: list = []
    with fake():
        one = provider.start(thread("t1"), brief(cwd="/tmp/one"), lambda n, a, b: seen.append(("one", n)) or Decision.ALLOW)
        two = provider.start(thread("t2"), brief(cwd="/tmp/two"), lambda n, a, b: seen.append(("two", n)) or Decision.ALLOW)
        check(one.provider_session_id != two.provider_session_id, "each gets its own session id")
        FakeClient.instances[0].script = [Hook("Read", {}), result_message()]
        FakeClient.instances[1].script = [result_message(usage={"input_tokens": 7, "output_tokens": 1})]
        first = drain(provider, one)
        second = drain(provider, two)
        eq(seen, [("one", "Read")], "one handle's hook does not fire on the other")
        check(
            all(e.thread_id == "t1" for e in first) and all(e.thread_id == "t2" for e in second),
            "events are attributed to their own thread",
        )
        eq(provider.usage(two).input_tokens, 7, "usage is per handle")
        eq(provider.usage(one).input_tokens, 100, "...on both sides")
        provider.close(one)
        provider.close(two)


def skill_checks() -> None:
    print("\n-- a named skill (S1) reaches Claude as a directive to use it")
    provider = claude.ClaudeProvider()
    with fake():
        handle = provider.start(thread("sk"), brief(), lambda n, a, b: Decision.ALLOW)
        FakeClient.instances[0].script = [result_message()]
        list(provider.send(handle, UserMessage(text="keep it short", skill="morning-briefing")))
        eq(FakeClient.instances[0].queries[-1],
           'Use the "morning-briefing" skill for this request.\n\nkeep it short',
           "the prompt is the directive, then the request")
        FakeClient.instances[0].script = [result_message()]
        list(provider.send(handle, UserMessage(text="", skill="morning-briefing")))
        eq(FakeClient.instances[0].queries[-1],
           'Use the "morning-briefing" skill for this request.', "no request: the directive alone")
        FakeClient.instances[0].script = [result_message()]
        list(provider.send(handle, UserMessage(text="plain")))
        eq(FakeClient.instances[0].queries[-1], "plain", "no skill: the text, untouched")
        provider.close(handle)


# --- the credential rule ----------------------------------------------------


def secrets_checks() -> None:
    print("\n-- no credential value reaches any string this provider produces")
    provider = claude.ClaudeProvider()

    # Layer 1: the shape of a bearer token, wherever it came from.
    script = [
        AssistantMessage(
            content=[
                TextBlock(text=f"I found {FAKE_TOKEN} in the env"),
                ToolUseBlock(id="c1", name="Bash", input={"command": "env"}),
            ],
            model="m",
            session_id=SESSION_ID,
        ),
        SdkUserMessage(
            content=[
                ToolResultBlock(tool_use_id="c1", content=f"ANTHROPIC_KEY={FAKE_TOKEN}\n")
            ]
        ),
        StreamEvent(
            uuid="u1",
            session_id=SESSION_ID,
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": FAKE_TOKEN},
            },
        ),
        result_message(),
    ]
    with fake(script):
        handle = provider.start(thread(), brief(), allow)
        events = drain(provider, handle)
        blob = strings(events)
        check(FAKE_TOKEN not in blob, "a token-shaped value never reaches an event")
        check("[redacted]" in blob, "...and the redaction is visible rather than silent")
        provider.close(handle)

    # Layer 2: an exception path, with the child's stderr carrying it.
    with fake([Raise(RuntimeError(f"auth failed for {FAKE_TOKEN}"))]):
        handle = provider.start(thread(), brief(), allow)
        session = handle.native
        session.stderr.append(f"debug: bearer {FAKE_TOKEN}\n")
        events = drain(provider, handle)
        message = events[-1].data["message"]
        check(FAKE_TOKEN not in message, "nor an exception message")
        check("RuntimeError" in message, "...while the diagnosis survives")
        provider.close(handle)

    # Layer 3: `secrets.scrub`, which knows the files Jarvis owns.
    with tempfile.TemporaryDirectory(prefix="jarvis-wp3-env-") as tmp:
        env_value = "sh0rt-but-real-looking-credential-value-1234"
        Path(tmp, ".env").write_text(f"OPENROUTER_API_KEY={env_value}\n")
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with fake(
                [
                    AssistantMessage(
                        content=[TextBlock(text=f"the key is {env_value}")],
                        model="m",
                        session_id=SESSION_ID,
                    ),
                    result_message(),
                ]
            ):
                handle = provider.start(thread(), brief(), allow)
                events = drain(provider, handle)
                check(
                    env_value not in strings(events),
                    "a value from a protected file never reaches an event either",
                )
                provider.close(handle)
        finally:
            os.chdir(cwd)

    # And the one file this provider reads: only an integer comes out of it.
    with credentials({"claudeAiOauth": {"accessToken": FAKE_TOKEN, "expiresAt": 1}}):
        expiry = claude._login_expiry_ms()
        eq(expiry, 1, "the credential bundle yields one integer and nothing else")
        with cli("2.1.240"):
            ok, reason = claude.ClaudeProvider().health()
            check(
                FAKE_TOKEN not in reason and not ok,
                f"and the health reason carries no credential: {reason}",
            )


# --- main -------------------------------------------------------------------


def set_model_checks() -> None:
    """Decisions A1: a chat thread's model or effort changes between turns by
    resuming the same session with new options — and keeps the gate."""
    provider = claude.ClaudeProvider()
    chat = Thread(id="chat", project_id="p1", role=Role.CHAT, provider=ProviderName.CLAUDE)
    with fake([result_message()]):
        handle = provider.start(chat, brief(role=Role.CHAT, model="claude-opus-5-5", effort="high"), allow)
        first = FakeClient.instances[-1]
        eq((first.options.model, first.options.effort), ("claude-opus-5-5", "high"), "set_model: opened on the chosen pair")
        drain(provider, handle, "one")
        provider.set_model(handle, "claude-sonnet-5-5", "low")
        second = FakeClient.instances[-1]
        check(second is not first and first.disconnects == 1, "set_model: the old client is disconnected, a new one connected")
        eq((second.options.model, second.options.effort), ("claude-sonnet-5-5", "low"), "set_model: the new client runs the new pair")
        eq(second.options.resume, SESSION_ID, "set_model: it resumes the same session (the conversation is kept)")
        check(second.options.hooks["PreToolUse"][0].hooks and second.options.allowed_tools == [],
              "set_model: the PreToolUse gate travels to the new client, allowed_tools stays empty")
        eq(second.options.permission_mode, "auto", "set_model: the profile's permission mode is unchanged")
        drain(provider, handle, "two")
        eq(second.queries, ["two"], "set_model: the next turn goes to the new client")
        eq(handle.native.brief.model, "claude-sonnet-5-5", "set_model: the session's brief carries the new model")

        count = len(FakeClient.instances)
        try:
            provider.set_model(handle, "claude-sonnet-5-5", "ultra")
        except BriefRefused as exc:
            check("ultra" in str(exc), "set_model: an off-ladder effort is refused by name")
        else:
            check(False, "set_model: an off-ladder effort is refused")
        eq(len(FakeClient.instances), count, "set_model: a refused effort spawns nothing")

        handle.native.sending.acquire()
        try:
            provider.set_model(handle, "claude-opus-5-5", "high")
        except ValueError as exc:
            check("turn" in str(exc), "set_model: refused while a turn is running")
        else:
            check(False, "set_model: refused while a turn is running")
        finally:
            handle.native.sending.release()
        provider.close(handle)

    # A reconnect that fails puts the old pair back and says so.
    original = claude._client_factory

    def failing(options):
        client = FakeClient(options, [result_message()])
        if options.model == "claude-fable-5-1":
            async def refuse():
                raise RuntimeError("model not available on this plan")
            client.connect = refuse
        return client

    FakeClient.instances = []
    claude._client_factory = failing
    try:
        fresh = provider.start(Thread(id="chat2", project_id="p1", role=Role.CHAT,
                                      provider=ProviderName.CLAUDE),
                               brief(role=Role.CHAT, model="claude-opus-5-5", effort="high"), allow)
        try:
            provider.set_model(fresh, "claude-fable-5-1", "high")
        except BriefRefused as exc:
            check("claude-fable-5-1" in str(exc) and "not available" in str(exc),
                  "set_model: a failed switch is refused with the reason")
        else:
            check(False, "set_model: a failed switch raises")
        restored = FakeClient.instances[-1]
        eq((restored.options.model, restored.connected), ("claude-opus-5-5", True),
           "set_model: the old model is reconnected after a failed switch")
        eq(restored.options.resume, None, "set_model: before any turn there is nothing to resume")
        eq(restored.options.session_id, fresh.provider_session_id, "set_model: the same session id is kept")
        eq(fresh.native.brief.model, "claude-opus-5-5", "set_model: the brief keeps the old model")
        provider.close(fresh)

        # Neither the new model nor the old one connects: the old client is
        # already disconnected, so it must not be put back as if it worked.
        def nothing_connects(options):
            client = FakeClient(options, [result_message()])
            if options.model in ("claude-fable-5-1", "claude-opus-5-5") and getattr(
                    nothing_connects, "armed", False):
                async def refuse():
                    raise RuntimeError("login expired")
                client.connect = refuse
            return client

        claude._client_factory = nothing_connects
        lost = provider.start(Thread(id="chat3", project_id="p1", role=Role.CHAT,
                                     provider=ProviderName.CLAUDE),
                              brief(role=Role.CHAT, model="claude-opus-5-5", effort="high"), allow)
        old = lost.native.client
        nothing_connects.armed = True
        try:
            provider.set_model(lost, "claude-fable-5-1", "high")
        except SessionLost as exc:
            check("login expired" in str(exc) and "resumes" in str(exc),
                  "set_model: a lost session says why and that the next message resumes it")
        except BriefRefused:
            check(False, "set_model: a double failure is SessionLost, not a plain refusal")
        else:
            check(False, "set_model: a double failure raises")
        check(lost.native.closed, "set_model: a session that cannot reconnect is closed, not restored")
        check(old.disconnects >= 1 and not old.connected,
              "set_model: the disconnected old client is not treated as live")
        events = list(provider.send(lost, UserMessage("after")))
        check(len(events) == 1 and events[0].kind == EventKind.ERROR and "closed" in events[0].data["message"],
              "set_model: a send on the lost session reports it closed (the daemon resumes instead)")
        provider.close(lost)   # idempotent
    finally:
        claude._client_factory = original


def main() -> int:
    health_checks()
    options_checks()
    sequence_checks()
    hook_checks()
    answer_checks()
    reviewer_declined_checks()
    interrupt_checks()
    error_checks()
    usage_checks()
    isolation_checks()
    skill_checks()
    secrets_checks()
    set_model_checks()
    if _failures:
        print(f"\n{len(_failures)} check(s) FAILED:")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("\nall claude-provider checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
