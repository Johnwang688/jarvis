"""ClaudeProvider — Claude Code behind the v2 provider interface (design §5.3).

The SDK (`claude-agent-sdk` 0.2.153) spawns the installed `claude` binary and
inherits its subscription login, so a worker here is the owner's own Claude
Code running headless in a task worktree. Three things about that shape decide
almost every line below.

**The gate is a `PreToolUse` hook, not `can_use_tool`** (R2, answered by
`tests/spikes/r1_sdk_login.py`). Under `permission_mode="auto"` the CLI's
classifier settles every call and the permission callback is consulted for
*nothing*, while the hook fires for every tool use. So §6's five layers reach
Claude through the hook. Under `auto` an ALLOW returns `{}` rather than an
explicit allow decision: layer 3 *is* the classifier, and an explicit allow
skips it (the SDK says so, and it would also skip `can_use_tool`). Under `ask`
layer 3 is skipped by definition (§6.2), so there the same ALLOW is explicit.

**`allowed_tools` is never used for anything gated** (R1). Entries there are
auto-approved *before* any callback, which would silently unbuild the gate.
`Brief.allowed_tools` therefore maps to the SDK's `tools` option — which
restricts what exists rather than what runs unasked — and `allowed_tools` is
left empty on every request.

**The SDK is async and this interface is not.** Each handle owns a private
event loop on its own thread; `send()` runs the turn as a coroutine that pushes
`Event`s into a queue the generator drains synchronously — the fastpath
pattern, for the fastpath reason: `TEXT_DELTA` has to arrive *during* the model
call, not in a list at the end. The turn coroutine's `finally` always posts the
sentinel, so the generator cannot hang, and an SDK exception becomes
`ERROR{fatal: True}` and then stops.

A note on money: `ResultMessage.total_cost_usd` is reported even on a
subscription, where nothing is billed (R1: 0.28 for a three-tool turn). It is
passed through unchanged as an **equivalent** figure; what it means is the
ledger's decision (§8.3), not this provider's.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import queue
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage as SdkUserMessage,
)

from ...tools import secrets
from ..model import PermissionProfile, ProviderName, Thread
from ..provider import (
    Brief,
    BriefRefused,
    Decision,
    Event,
    EventKind,
    PermissionCallback,
    SessionHandle,
    Usage,
    UserMessage,
)

# major.minor is the pin; the patch moves weekly and breaks nothing we use.
# health() reports the exact version it found, because "2.1.233" in this
# constant and 2.1.273 on the machine is the sort of gap a reason string
# should state rather than hide behind an ok.
CLAUDE_PIN = "2.1.233"

# The CLI kills a hook that does not answer in time and treats it as no
# decision, so a slow owner would read as "the gate did not fire". The broker's
# own remote timeout is 10 minutes (a Discord DM to a phone), so anything under
# that turns an owner who is merely walking to their desk into a silent
# fall-through. `HookMatcher.timeout` is in **seconds** and defaults to 60
# (verified in the installed SDK's `types.py`); the SDK itself applies no
# timeout of its own to the callback — it awaits it — so this number is the
# whole budget.
HOOK_TIMEOUT_S = 660.0

# How long to wait for connect/disconnect/interrupt control requests.
CONNECT_TIMEOUT_S = 120.0
CONTROL_TIMEOUT_S = 60.0

# How much of a tool result goes into TOOL_FINISHED.summary. A surface renders
# this in a ticker; the transcript already holds the whole thing.
SUMMARY_CHARS = 200

# Kept only to enrich a fatal error. Never emitted on its own, always scrubbed.
STDERR_LINES = 40

# The exact text the CLI puts in a tool result when the auto-mode classifier
# refuses a call — §6.1's `reviewer_declined` trigger. Verified 2026-09-15
# against the installed binary (2.1.273), which holds it as a single template
# constant; the classifier's own words follow "Reason: ".
CLASSIFIER_DENIAL_PREFIX = (
    "Permission for this action was denied by the Claude Code auto mode classifier"
)
_REASON = re.compile(
    r"Reason:\s*(?P<reason>.+?)\.\s+If you have other tasks", re.DOTALL
)

# What `--effort` accepts (SDK `EffortLevel`). An off-ladder value is refused
# rather than passed through: the CLI would reject it at spawn, one turn later
# and nowhere near the brief that asked for it.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Claude Code's own prompt, CLAUDE.md and skills all hang off the preset. A
# bare string prompt would replace them, which is how a worker loses the
# project's conventions without anything being logged.
SETTING_SOURCES = ["user", "project"]

# Anything that looks like a bearer credential, redacted before a string can
# reach an event. `secrets.scrub` covers the files Jarvis owns; the CLI's own
# OAuth bundle (`~/.claude/.credentials.json`) is not one of them, and this
# provider is the first thing in the codebase whose child process holds it.
_TOKENISH = re.compile(r"\b(?:sk|oat|rt)[-_][A-Za-z0-9_\-]{12,}", re.IGNORECASE)
_JWTISH = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+")
_REDACTED = "[redacted]"


def _safe(text: Any) -> str:
    """Every string this module puts in an event, a reason or an exception.

    Two layers, because they cover different things. `secrets.scrub` knows the
    credential files Jarvis owns and the values inside them. The regexes know
    the shape of a bearer token, which is what a child process's stderr or a
    model-written argument might carry from somewhere Jarvis has never heard
    of — including the CLI's own login.
    """
    out = secrets.scrub(str(text or ""))
    out = _TOKENISH.sub(_REDACTED, out)
    return _JWTISH.sub(_REDACTED, out)


# The seam the free tests replace: everything that would spawn a `claude`
# process goes through here, so a suite never needs one.
def _client_factory(options: ClaudeAgentOptions) -> Any:
    return ClaudeSDKClient(options=options)


# --- health -----------------------------------------------------------------


def _credentials_path() -> Path:
    """The CLI's credential bundle. An env override exists for the tests only.

    Read for exactly one number. Never opened anywhere else in this module.
    """
    override = os.environ.get("JARVIS_CLAUDE_CREDENTIALS")
    return Path(override) if override else Path.home() / ".claude" / ".credentials.json"


def _login_expiry_ms() -> int | None:
    """When the login stops working, in epoch milliseconds, or None if unknown.

    **Which field, and why it matters.** The bundle carries two: `expiresAt`
    (the access token, refreshed silently roughly hourly) and
    `refreshTokenExpiresAt` (the grant itself). Judging the login by the access
    token would report "expired" for most of every hour on a perfectly healthy
    machine, and a health check that cries wolf is one nobody reads — the same
    failure as an allowlist that silently stops matching. So the refresh
    token's expiry is the login's expiry when it is present, and the access
    token's is the fallback.

    Nothing but these integers is ever read out of the file, and no value from
    it is returned, logged or put in an exception.
    """
    path = _credentials_path()
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        oauth = json.loads(raw).get("claudeAiOauth") or {}
    except ValueError:
        return None
    for key in ("refreshTokenExpiresAt", "expiresAt"):
        value = oauth.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


# --- session state ----------------------------------------------------------


@dataclass
class _Pending:
    """One outstanding permission question, answerable out of band."""

    ready: threading.Event = field(default_factory=threading.Event)
    value: Decision | None = None


@dataclass
class _Session:
    client: Any
    brief: Brief
    permit: PermissionCallback
    thread_id: str
    loop: asyncio.AbstractEventLoop
    runner: threading.Thread
    session_id: str | None = None
    handle: SessionHandle | None = None
    # One turn at a time per conversation: two concurrent sends would
    # interleave two turns into one transcript.
    sending: threading.Lock = field(default_factory=threading.Lock)
    events: queue.Queue | None = None
    pending: dict[str, _Pending] = field(default_factory=dict)
    mutex: threading.RLock = field(default_factory=threading.RLock)
    interrupted: bool = False
    closed: bool = False
    tools_seen: dict[str, tuple[str, dict]] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    stderr: deque = field(default_factory=lambda: deque(maxlen=STDERR_LINES))

    def emit(self, kind: EventKind, **data: Any) -> None:
        """Put an event on the turn's queue. Silent outside a turn by design.

        A hook can only fire inside a turn, so `events is None` means the
        consumer already stopped reading — dropping is right, blocking is not.
        """
        events = self.events
        if events is not None:
            events.put(Event(kind, self.thread_id, data))


# --- the provider -----------------------------------------------------------


class ClaudeProvider:
    """Claude Code as a v2 provider. See the module docstring."""

    name = ProviderName.CLAUDE

    # -- health

    def health(self) -> tuple[bool, str]:
        """(ok, reason). Binary, version pin, login. Never spends a token.

        The login check reads one integer out of the credential bundle and
        nothing else — see `_login_expiry_ms`. A bundle with no expiry field at
        all is reported as *unknown* rather than refused: a machine
        authenticating some other way (an `ANTHROPIC_API_KEY`, a managed
        keychain) has no bundle to read, and refusing to run because a file
        this provider does not own has changed shape would be a worse failure
        than the one it is guarding against.
        """
        binary = shutil.which("claude")
        if binary is None:
            return False, "claude is not on PATH"
        try:
            probe = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=20
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"claude --version failed ({type(exc).__name__})"
        if probe.returncode:
            return False, f"claude --version exited {probe.returncode}"
        found = (probe.stdout or "").strip().split(" ")[0]
        if _series(found) != _series(CLAUDE_PIN):
            return False, f"claude {found or '?'}, pinned {CLAUDE_PIN}"
        expiry = _login_expiry_ms()
        if expiry is None:
            if not _credentials_path().exists():
                return False, f"claude {found}: no login found; run `claude login`"
            return True, f"claude {found}, login expiry unknown"
        if expiry <= _now_ms():
            return False, f"claude {found}: login expired; run `claude login`"
        return True, f"claude {found}, subscription login"

    # -- lifecycle

    def start(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        return self._open(thread, brief, permit, resume=False)

    def resume(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        if not thread.provider_session_id:
            raise BriefRefused("Claude resume needs the thread's provider_session_id")
        return self._open(thread, brief, permit, resume=True)

    def _open(
        self, thread: Thread, brief: Brief, permit: PermissionCallback, *, resume: bool
    ) -> SessionHandle:
        session_id = thread.provider_session_id if resume else str(uuid.uuid4())
        loop = asyncio.new_event_loop()
        runner = threading.Thread(
            target=_run_loop, args=(loop,), name=f"jarvis-claude-{thread.id}", daemon=True
        )
        runner.start()
        session = _Session(
            client=None,
            brief=brief,
            permit=permit,
            thread_id=thread.id,
            loop=loop,
            runner=runner,
            session_id=session_id,
        )
        options = self._options(brief, session, resume=resume, session_id=session_id)
        try:
            session.client = _client_factory(options)
            _await(loop, session.client.connect(), CONNECT_TIMEOUT_S)
        except BaseException as exc:  # noqa: BLE001 — a failed start must not leak a thread
            _stop_loop(loop, runner)
            raise BriefRefused(f"claude session failed to start: {_safe(exc)}") from None
        handle = SessionHandle(
            thread_id=thread.id,
            provider=self.name,
            provider_session_id=session_id,
            native=session,
        )
        session.handle = handle
        return handle

    def _options(
        self, brief: Brief, session: _Session, *, resume: bool, session_id: str | None
    ) -> ClaudeAgentOptions:
        """The Brief, mapped onto the SDK. Refuses rather than narrows (§5.1)."""
        if brief.profile is PermissionProfile.STRICT:
            raise BriefRefused(
                "the strict profile is firm-style confinement (native tools off, "
                "no network, deny-all); Claude Code has no such mode — route "
                "strict tasks to the Codex provider (design §6.2)"
            )
        if brief.effort is not None and brief.effort not in EFFORT_LEVELS:
            raise BriefRefused(
                f"effort {brief.effort!r} is not one of {', '.join(EFFORT_LEVELS)}"
            )
        auto = brief.profile is PermissionProfile.AUTO
        kwargs: dict[str, Any] = {
            "cwd": brief.cwd,
            # The preset, never a bare string: a string *replaces* Claude
            # Code's own prompt, and with it CLAUDE.md and the skills listing.
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                **({"append": brief.system_append} if brief.system_append else {}),
            },
            "setting_sources": list(SETTING_SOURCES),
            "permission_mode": "auto" if auto else "default",
            "include_partial_messages": True,
            "strict_mcp_config": True,
            "mcp_servers": dict(brief.mcp_servers),
            # R1: an entry here is auto-approved before any callback runs.
            # Nothing gated may ever appear in it, and everything is gated.
            "allowed_tools": [],
            "hooks": {
                "PreToolUse": [
                    HookMatcher(
                        matcher=None,  # every tool, not a name pattern
                        hooks=[self._pre_tool_hook(session)],
                        timeout=HOOK_TIMEOUT_S,
                    )
                ]
            },
            "stderr": session.stderr.append,
        }
        if brief.model:
            kwargs["model"] = brief.model
        if brief.effort:
            kwargs["effort"] = brief.effort
        if brief.max_turns is not None:
            kwargs["max_turns"] = brief.max_turns
        if brief.allowed_tools is not None:
            # `tools` restricts what the model *has*; `allowed_tools` would
            # restrict what runs unasked. A role brief means the former.
            kwargs["tools"] = list(brief.allowed_tools)
        if not auto:
            # The SDK-level backstop for the cases the CLI's own rules send to
            # a prompt. The hook settles almost everything first; this exists
            # so a path that reaches the prompt still reaches the owner.
            kwargs["can_use_tool"] = self._can_use_tool(session)
        if resume:
            kwargs["resume"] = session_id
        else:
            # Minting the id is what lets `start()` return a handle that can
            # already be resumed: the CLI does not announce a session id until
            # the first turn (verified against 2.1.273 — no init message
            # arrives at connect, and `get_server_info()` carries no id).
            kwargs["session_id"] = session_id
        return ClaudeAgentOptions(**kwargs)

    # -- the gate

    def _pre_tool_hook(self, session: _Session):
        """§6's five layers, as the one thing that sees every tool call.

        `{}` on ALLOW under `auto` is the load-bearing detail: it is *not* an
        allow decision, so the classifier (layer 3) still runs. An explicit
        allow would skip it — and would also skip `can_use_tool`, per the SDK.
        Under `ask` layer 3 is skipped by design, so ALLOW is explicit there.
        """

        async def pre_tool(input_data, tool_use_id, context):  # noqa: ANN001
            data = input_data or {}
            name = str(data.get("tool_name") or "")
            args = data.get("tool_input")
            args = dict(args) if isinstance(args, dict) else {}
            decision = await self._decide(session, name, args)
            if decision is Decision.ALLOW and session.brief.profile is PermissionProfile.AUTO:
                return {}
            if decision is Decision.ALLOW:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": "approved by Jarvis",
                    }
                }
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    # The model reads this, so it says what happened rather
                    # than only that something did.
                    "permissionDecisionReason": _safe(
                        f"Jarvis denied {name}. The owner's rules or the owner "
                        "refused this call; do not retry it or work around it."
                    ),
                }
            }

        return pre_tool

    def _can_use_tool(self, session: _Session):
        async def can_use_tool(tool_name, input_data, context):  # noqa: ANN001
            args = dict(input_data) if isinstance(input_data, dict) else {}
            decision = await self._decide(session, str(tool_name or ""), args)
            if decision is Decision.ALLOW:
                return PermissionResultAllow()
            return PermissionResultDeny(message=_safe("Jarvis denied this call"))

        return can_use_tool

    async def _decide(self, session: _Session, name: str, args: dict) -> Decision:
        """Ask `permit`, announcing the question so a stalled turn is visible.

        `permit` may block for minutes while a human is asked, and it is a
        *synchronous* callable, so it runs on a worker thread — blocking this
        session's event loop would stop the CLI's stdout being read and make
        `interrupt()` unanswerable for the whole wait.

        An out-of-band `answer()` races it and wins, which is the one place
        this provider goes further than its Codex sibling: the abandoned
        `permit` call keeps its thread until it returns on its own (Python
        cannot cancel it), but the turn is released.
        """
        req_id = uuid.uuid4().hex[:12]
        pending = _Pending()
        with session.mutex:
            session.pending[req_id] = pending
        session.emit(
            EventKind.APPROVAL_REQUESTED,
            req_id=req_id,
            tool=name,
            args=args,
            command=_command(args),
        )
        try:
            decision = await self._race(session, pending, name, args)
        finally:
            with session.mutex:
                session.pending.pop(req_id, None)
        session.emit(EventKind.APPROVAL_RESOLVED, req_id=req_id, decision=decision.value)
        return decision

    async def _race(
        self, session: _Session, pending: _Pending, name: str, args: dict
    ) -> Decision:
        def ask() -> Decision:
            try:
                return Decision(session.permit(name, dict(args), session.brief))
            except Exception:  # noqa: BLE001 — an unreadable answer is a denial
                return Decision.DENY

        async def answered() -> Decision:
            # Polled rather than waited on in a thread: a thread parked on an
            # answer that never comes is a thread the interpreter joins at
            # exit, which turns "the owner never replied" into "the daemon
            # will not shut down".
            while not pending.ready.is_set():
                await asyncio.sleep(0.05)
            return pending.value or Decision.DENY

        asked = asyncio.ensure_future(_in_thread(session.loop, ask))
        waiter = asyncio.ensure_future(answered())
        try:
            done, _ = await asyncio.wait({asked, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not waiter.done():
                waiter.cancel()
        if pending.ready.is_set() and pending.value is not None:
            return pending.value
        if asked in done:
            try:
                return asked.result()
            except Exception:  # noqa: BLE001
                return Decision.DENY
        # The owner answered out of band and `permit` is still blocked. Python
        # cannot cancel it, so its thread is left to finish on its own — it is
        # a daemon, so it holds nothing up. (WP4 asked for a cancellable
        # request context; this is the half of it a provider can build.)
        return Decision.DENY

    def answer(self, h: SessionHandle, req_id: str, decision: Decision | str) -> None:
        """Resolve a question this session is blocked on, out of band."""
        session = _native(h)
        with session.mutex:
            pending = session.pending.get(req_id)
            if pending is None or pending.ready.is_set():
                raise ValueError(f"Unknown or resolved Claude request id: {req_id}")
            if isinstance(decision, str) and not isinstance(decision, Decision):
                decision = Decision(decision)
            pending.value = Decision(decision)
            pending.ready.set()

    # -- the turn

    def send(self, h: SessionHandle, message: UserMessage) -> Iterator[Event]:
        session = h.native
        if session is None or session.closed:
            yield Event(
                EventKind.ERROR, h.thread_id, {"message": "session closed", "fatal": True}
            )
            return
        if not session.sending.acquire(blocking=False):
            yield Event(
                EventKind.ERROR,
                h.thread_id,
                {"message": "a turn is already running on this thread", "fatal": True},
            )
            return
        try:
            yield from self._run(h, session, message)
        finally:
            session.events = None
            session.sending.release()

    def _run(self, h: SessionHandle, session: _Session, message: UserMessage) -> Iterator[Event]:
        events: queue.Queue = queue.Queue()
        session.events = events
        session.interrupted = False
        session.tools_seen.clear()
        done = object()

        async def turn() -> None:
            try:
                await session.client.query(_prompt(message))
                async for msg in session.client.receive_response():
                    for event in self._translate(session, msg):
                        events.put(event)
            except BaseException as exc:  # noqa: BLE001 — reported, never swallowed
                events.put(
                    Event(
                        EventKind.ERROR,
                        session.thread_id,
                        {"message": _fatal_message(session, exc), "fatal": True},
                    )
                )
            finally:
                # The generator must never hang, whatever happened above.
                events.put(done)

        yield Event(EventKind.TURN_STARTED, h.thread_id)
        future = asyncio.run_coroutine_threadsafe(turn(), session.loop)
        finished = False
        try:
            while True:
                item = events.get()
                if item is done:
                    break
                if item.kind in (EventKind.TURN_FINISHED, EventKind.ERROR):
                    finished = True
                yield item
        finally:
            if not finished and not future.done():
                # A consumer that abandons the iterator must not leave a model
                # editing files in the worktree.
                self.interrupt(h)
            future.cancel()

    def _translate(self, session: _Session, msg: Any) -> list[Event]:
        """One SDK message becomes zero or more §5.2 events."""
        out: list[Event] = []
        if isinstance(msg, SystemMessage):
            self._note_session_id(session, (msg.data or {}).get("session_id"))
            return out
        if isinstance(msg, StreamEvent):
            self._note_session_id(session, msg.session_id)
            text = _delta_text(msg.event)
            if text:
                out.append(Event(EventKind.TEXT_DELTA, session.thread_id, {"text": text}))
            return out
        if isinstance(msg, AssistantMessage):
            self._note_session_id(session, msg.session_id)
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if block.text:
                        out.append(
                            Event(EventKind.TEXT, session.thread_id, {"text": _safe(block.text)})
                        )
                elif isinstance(block, ThinkingBlock):
                    if block.thinking:
                        out.append(
                            Event(
                                EventKind.THINKING,
                                session.thread_id,
                                {"text": _safe(block.thinking)},
                            )
                        )
                elif isinstance(block, ToolUseBlock):
                    args = dict(block.input) if isinstance(block.input, dict) else {}
                    session.tools_seen[block.id] = (block.name, args)
                    out.append(
                        Event(
                            EventKind.TOOL_STARTED,
                            session.thread_id,
                            {"call_id": block.id, "name": block.name, "args": args},
                        )
                    )
            return out
        if isinstance(msg, SdkUserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    out.extend(self._tool_result(session, block))
            return out
        if isinstance(msg, ResultMessage):
            self._note_session_id(session, msg.session_id)
            out.extend(self._result(session, msg))
            return out
        return out

    def _tool_result(self, session: _Session, block: Any) -> list[Event]:
        if not isinstance(block, ToolResultBlock):
            return []
        name, args = session.tools_seen.get(block.tool_use_id, ("", {}))
        text = _safe(_result_text(block.content))
        declined = text.startswith(CLASSIFIER_DENIAL_PREFIX)
        out: list[Event] = []
        if declined:
            # §6.1: the owner's judgement outranks a classifier, but only
            # explicitly and per command — so the command travels with the
            # event rather than a summary of it.
            match = _REASON.search(text)
            out.append(
                Event(
                    EventKind.REVIEWER_DECLINED,
                    session.thread_id,
                    {
                        "tool": name,
                        "args": args,
                        "command": _command(args),
                        "reason": match.group("reason").strip()
                        if match
                        else text[:SUMMARY_CHARS],
                    },
                )
            )
        out.append(
            Event(
                EventKind.TOOL_FINISHED,
                session.thread_id,
                {
                    "call_id": block.tool_use_id,
                    "name": name,
                    "ok": not bool(block.is_error) and not declined,
                    "summary": text[:SUMMARY_CHARS],
                },
            )
        )
        return out

    def _result(self, session: _Session, msg: ResultMessage) -> list[Event]:
        reported = dict(msg.usage or {})
        cost = msg.total_cost_usd
        turn = Usage(
            input_tokens=int(reported.get("input_tokens") or 0),
            output_tokens=int(reported.get("output_tokens") or 0),
            cached_tokens=int(reported.get("cache_read_input_tokens") or 0),
            cost_usd=cost,
        )
        total = session.usage
        total.input_tokens += turn.input_tokens
        total.output_tokens += turn.output_tokens
        total.cached_tokens += turn.cached_tokens
        if cost is not None:
            total.cost_usd = (total.cost_usd or 0.0) + float(cost)
        return [
            Event(
                EventKind.USAGE,
                session.thread_id,
                {
                    "input": turn.input_tokens,
                    "output": turn.output_tokens,
                    "cached": turn.cached_tokens,
                    # An *equivalent* figure on a subscription, where nothing
                    # is billed (R1). Passed through unchanged; what it means
                    # is the ledger's call, not this provider's.
                    "cost_usd": cost,
                    "provider_reported": reported,
                },
            ),
            Event(
                EventKind.TURN_FINISHED,
                session.thread_id,
                {"stop": _stop_reason(session, msg)},
            ),
        ]

    def _note_session_id(self, session: _Session, session_id: Any) -> None:
        """The CLI's own id wins over the one we minted, if they ever differ."""
        if not session_id or session.session_id == session_id:
            return
        session.session_id = str(session_id)
        if session.handle is not None:
            session.handle.provider_session_id = session.session_id

    # -- control

    def interrupt(self, h: SessionHandle) -> None:
        session = h.native
        if session is None or session.closed:
            return
        session.interrupted = True
        try:
            _await(session.loop, session.client.interrupt(), CONTROL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — a failed interrupt must not raise at a surface
            pass

    def usage(self, h: SessionHandle) -> Usage:
        session = h.native
        if session is None:
            return Usage()
        total = session.usage
        return Usage(
            input_tokens=total.input_tokens,
            output_tokens=total.output_tokens,
            cached_tokens=total.cached_tokens,
            cost_usd=total.cost_usd,
        )

    def close(self, h: SessionHandle) -> None:
        """Disconnect and stop the loop thread. Idempotent."""
        session = h.native
        if session is None or session.closed:
            return
        session.closed = True
        with session.mutex:
            for pending in session.pending.values():
                if not pending.ready.is_set():
                    pending.value = Decision.DENY
                    pending.ready.set()
            session.pending.clear()
        try:
            _await(session.loop, session.client.disconnect(), CONTROL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — close must not raise
            pass
        _stop_loop(session.loop, session.runner)


# --- helpers ----------------------------------------------------------------


def _native(h: SessionHandle) -> _Session:
    if h.native is None:
        raise ValueError("this Claude session is closed")
    return h.native


def _series(version: str) -> str:
    parts = (version or "").split(".")
    return ".".join(parts[:2])


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _await(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def _in_thread(loop: asyncio.AbstractEventLoop, fn) -> Any:
    """`asyncio.to_thread`, but on a **daemon** thread we own.

    The default executor's threads are joined at interpreter exit, so a
    `permit` that blocks for ten minutes would block shutdown for ten minutes —
    and one that never returns would block it forever. The gate is allowed to
    take a long time; the process is not allowed to be held hostage by it.
    """
    future = loop.create_future()

    def run() -> None:
        try:
            value, error = fn(), None
        except BaseException as exc:  # noqa: BLE001
            value, error = None, exc

        def settle() -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

        loop.call_soon_threadsafe(settle)

    threading.Thread(target=run, name="jarvis-claude-permit", daemon=True).start()
    return future


def _stop_loop(loop: asyncio.AbstractEventLoop, runner: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    runner.join(timeout=CONTROL_TIMEOUT_S)
    try:
        loop.close()
    except RuntimeError:
        pass


def _command(args: dict) -> str | None:
    """The shell command in a tool's arguments, if it has one."""
    value = args.get("command")
    return value if isinstance(value, str) else None


def _prompt(message: UserMessage) -> Any:
    """A v2 `UserMessage` as what `client.query()` accepts.

    Text alone goes as a plain string (the SDK's own fast path); anything with
    an image becomes a streamed content-block message.
    """
    if not message.images:
        return message.text
    content: list[dict] = []
    if message.text:
        content.append({"type": "text", "text": message.text})
    for image in message.images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.get("mime") or "image/png",
                    "data": image.get("b64") or "",
                },
            }
        )

    async def one() -> Any:
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    return one()


def _delta_text(event: Any) -> str:
    """The text of a raw Anthropic stream event, or "" if it carries none."""
    if not isinstance(event, dict):
        return ""
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    return _safe(delta.get("text") or "")


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


_ABORTED = ("aborted_streaming", "aborted_tools")


def _stop_reason(session: _Session, msg: ResultMessage) -> str:
    terminal = msg.terminal_reason
    if session.interrupted or terminal in _ABORTED:
        return "interrupted"
    if msg.subtype == "error_max_turns" or terminal == "max_turns":
        return "max_turns"
    if msg.is_error:
        return "error"
    return "end"


def _fatal_message(session: _Session, exc: BaseException) -> str:
    """What the surface is told when the SDK raises.

    The child's stderr is the only place a real diagnosis usually lives, so a
    bounded tail of it rides along — scrubbed, like everything else here. It is
    never emitted on its own: stderr from a process holding the owner's login
    is not a stream to broadcast.
    """
    message = f"{type(exc).__name__}: {_safe(exc)}"
    tail = _safe("".join(session.stderr)).strip()
    if tail:
        message = f"{message}\n{tail[-1000:]}"
    return message
