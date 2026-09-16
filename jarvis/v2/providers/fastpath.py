"""FastPathProvider — v1's agent loop behind the v2 provider interface.

Design §2 D2, §5.5 and §8.1. v1's OpenRouter loop survives as exactly one
thing: the conversation fast path. Voice turns, Discord chat that is not a task
verb, quick questions. A CLI session costs seconds to start and subscription
quota per turn, and the voice surface's whole budget is latency — so chat keeps
the cheap loop and everything that resembles *work* is handed to a task.

Two rules make that split safe, and both are mechanical rather than prompted:

**The structural boundary (§8.1).** `FAST_TOOLS` has no tool that can change
anything — no write, no shell, no browser, no desktop, nothing dangerous, no
way to spawn. So a turn that misjudges a request as chat can only answer badly;
it cannot half-do work and leave the owner to find out. The prompt carries the
escalation *test*, but the toolset is what makes a misjudgement harmless.

**The budget rule (§8.1).** A fast-path turn that burns its eight steps with
the request unmet does not report "stopped". v1 already spends one tool-free
call at the wall to write a handoff (`Agent._handoff`); here that handoff
becomes the brief of a proposed task. The v1 exhaustion handoff, pointed at a
task instead of at the owner.

Everything else here is translation: one v1 `Agent` per handle, its `on_event`
callbacks turned into the `Event` stream every v2 surface reads.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterator

from ... import config, models, permissions, sessions, tools
from ...agent import Agent
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
from ..tools import propose as _propose  # noqa: F401  (registers task_propose)
from ..tools import schedules as _schedules  # noqa: F401  (registers schedule_*)

# Eight steps, per §8.1. Not a work limit — a *definition*: anything that needs
# more than eight rounds of read-and-think is work, and work goes to a task.
# Small enough that exhaustion happens early and cheaply, which is the point,
# because exhaustion here produces a proposal rather than a failure.
FAST_MAX_STEPS = 8

# How much of a tool result goes into TOOL_FINISHED.summary. A surface renders
# this in a ticker; the transcript already holds the whole thing.
SUMMARY_CHARS = 200

# How a v1 tool result says "this did not happen". See `_TurnState._on_tool_end`.
_NOT_OK = ("Error", "Refused", "The user declined")

FAST_PROMPT = """

## This conversation is the fast path

You are answering on Jarvis's conversation fast path: cheap, quick, and
deliberately unable to change anything. You have no tool that writes a file,
runs a command, drives a browser, or spawns anything, and you have a budget of \
%(steps)d steps for this turn. That is not a mistake in your toolset — it is the \
design.

**Propose a task** with `task_propose` when the request needs any one of:
editing a file, running a command, more than one page fetched, a browser, a
deliverable (a file, a PR, a report), or more than your step budget. Any one is
sufficient. Do not attempt such a request here and do not apologise for the
limit — say in one line what you opened and that it will ask if anything is
unclear.

**Answer directly** when the request is a question you can settle from memory,
this transcript, one read or one fetch; a control request (mute, avatar,
music); or ordinary conversation. Most turns are this.

When you are unsure, propose. A task that turns out to be small costs a little;
a real job answered as if it were chat costs the owner their trust in the
answer.
""" % {"steps": FAST_MAX_STEPS}


# --- the toolset ------------------------------------------------------------

# §8.1's structural boundary, written out by name.
#
# An allowlist, and the same reasoning as `tools.PARALLEL_SAFE` and
# `config.DESKTOP_APPS`: "can this change something" has to be answered once per
# tool by a person, because the cost of guessing wrong is a chat turn quietly
# doing half a job. A denylist would silently admit every tool added later.
FAST_TOOLS: frozenset[str] = frozenset(
    {
        # read-only files
        "read_file",
        "list_dir",
        "find_files",
        "grep_files",
        # one page, when a question needs the web
        "fetch_page",
        # memory: the fast path's whole reason to exist is answering from it
        "memory_list",
        "memory_read",
        "memory_search",
        "memory_write",
        # past conversations, read-only (v1: sessions read, never switch)
        "session_list",
        "session_read",
        "session_search",
        "session_summary",
        # clock
        "get_datetime",
        # skills, read-only halves only
        "skill_list",
        "skill_read",
        # controls: audible, immediately obvious, undone by saying so
        "set_voice_mute",
        "set_avatar",
        "avatar_list",
        "spotify_play",
        "spotify_status",
        # the door out
        "task_propose",
        "schedule_create",
        "schedule_list",
        "schedule_delete",
    }
)

# Names that must never be in FAST_TOOLS, asserted below and again in the
# suite. Kept as data rather than left to a reader's eye because the failure
# mode is silent: a mutating tool that slipped in here would not raise, it
# would simply let a chat turn edit a file.
FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "run_command",
        "run_readonly",
        "run_subagent",
        "run_fleet",
        "plan_write",
        "load_tools",
        "delegate",
        "task_start",
        "task_status",
        "task_log",
        "task_cancel",
        "workflow_start",
        "workflow_status",
        "workflow_log",
        "goal_report",
        "compact_context",
        "whiteboard_close",
        "skill_write",
        "memory_delete",
    }
)

FORBIDDEN_PREFIXES = ("browser_", "desktop_", "cad_", "gmail_")


def _validate_toolset() -> None:
    """Fail loudly at import if the boundary does not describe reality.

    Invariant 10's failure mode is the one to avoid here: a toolset that
    silently shrinks is not an error, it is the mechanism quietly not working.
    A renamed or removed v1 tool must stop the process rather than leave the
    fast path one capability short with nothing said.
    """
    missing = sorted(name for name in FAST_TOOLS if name not in tools.REGISTRY)
    if missing:
        raise RuntimeError(
            "FAST_TOOLS names tools that are not registered: "
            + ", ".join(missing)
            + ". Either the tool was renamed or its module is no longer imported."
        )
    unsafe = sorted(name for name in FAST_TOOLS if tools.REGISTRY[name].dangerous)
    if unsafe:
        raise RuntimeError(f"FAST_TOOLS contains dangerous tools: {', '.join(unsafe)}")
    leaked = sorted(
        name
        for name in FAST_TOOLS
        if name in FORBIDDEN_TOOLS or name.startswith(FORBIDDEN_PREFIXES)
    )
    if leaked:
        raise RuntimeError(f"FAST_TOOLS contains tools §8.1 excludes: {', '.join(leaked)}")


_validate_toolset()


def _available_tools(names: frozenset[str]) -> list[str]:
    """`names`, minus the tools of a deferred group this machine never connected.

    Invariant 10's first state, the one that pays: a clone with no Spotify
    account should be byte-identical to one where those tools do not exist —
    no schemas, and no pointer advertising a service that would only answer
    with a setup error. `FAST_TOOLS` stays a constant so the boundary is the
    same on every machine; what is *sent* is filtered here.
    """
    hidden: set[str] = set()
    for group in tools.GROUPS.values():
        if not group.is_available():
            hidden.update(group.all_names())
    return sorted(name for name in names if name not in hidden)


# --- the handle -------------------------------------------------------------


@dataclass
class _Native:
    """Everything one fast-path conversation owns. Opaque above the provider."""

    agent: Agent
    session: sessions.Session
    brief: Brief
    # `interrupt()` sets this; v1 reads it between steps and per streamed line,
    # which is all invariant 3 allows and all a cancel needs.
    stop: threading.Event = field(default_factory=threading.Event)
    # One turn at a time per conversation. A second concurrent `send` on one
    # handle would interleave two turns into one transcript.
    busy: threading.Lock = field(default_factory=threading.Lock)
    cost_usd: float = 0.0
    turns: int = 0
    closed: bool = False


# --- the provider -----------------------------------------------------------


class FastPathProvider:
    """v1's `Agent` as a v2 provider. See the module docstring."""

    name = ProviderName.FAST

    def health(self) -> tuple[bool, str]:
        """Can this provider run a turn at all? Never spends a token.

        The key is checked for *set-ness only* — never printed, never measured.
        """
        if not os.environ.get("OPENROUTER_API_KEY"):
            return False, "OPENROUTER_API_KEY is not set"
        try:
            model = models.tier("orchestrator")
        except Exception as exc:
            return False, f"no orchestrator model: {type(exc).__name__}: {exc}"
        if not model:
            return False, "no orchestrator model configured"
        return True, f"ready on {model}"

    # -- lifecycle

    def start(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        """Open a new conversation: a fresh v1 session and an Agent over it."""
        return self._open(thread, brief, permit, session=sessions.new(surface="fast"))

    def resume(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle:
        """Reopen `thread`'s conversation, transcript and all.

        A session id that no longer resolves degrades to a fresh one rather
        than raising: losing a transcript is bad, and refusing to talk is
        worse. The handle reports the id it actually ended up on, so a caller
        that cares can see the substitution.
        """
        existing = sessions.load(thread.provider_session_id) if thread.provider_session_id else None
        return self._open(
            thread, brief, permit, session=existing or sessions.new(surface="fast")
        )

    def _open(
        self,
        thread: Thread,
        brief: Brief,
        permit: PermissionCallback,
        session: sessions.Session,
    ) -> SessionHandle:
        tool_names = self._toolset(brief)
        native = _Native(
            agent=Agent(
                model=brief.model or models.tier("orchestrator"),
                system=config.SYSTEM_PROMPT + FAST_PROMPT + (brief.system_append or ""),
                tool_names=tool_names,
                max_steps=FAST_MAX_STEPS,
                approve=self._approver(brief, permit),
                on_event=lambda kind, data: None,  # replaced per turn by send()
                should_stop=lambda: False,  # replaced below, once `native` exists
                session=session,
            ),
            session=session,
            brief=brief,
        )
        native.agent.should_stop = native.stop.is_set
        return SessionHandle(
            thread_id=thread.id,
            provider=self.name,
            provider_session_id=session.id,
            native=native,
        )

    def _toolset(self, brief: Brief) -> list[str]:
        """The tool names this brief gets, or `BriefRefused`.

        §5.1: a provider refuses a brief it cannot honour rather than silently
        narrowing it. So a brief asking for tools outside the §8.1 boundary is
        refused — quietly handing it a read-only toolset would look like the
        request was honoured and then behave as if it were not.

        A brief asking for a *subset* is honoured: narrowing further is always
        safe, and it is how a caller pins a minimal chat agent.
        """
        if brief.profile is PermissionProfile.STRICT:
            raise BriefRefused(
                "the fast path has no confinement to be strict about; strict "
                "tasks run on the Codex provider (design §6.2)"
            )
        if brief.mcp_servers:
            raise BriefRefused("the fast path cannot load MCP servers; use a CLI provider")
        allowed = _available_tools(FAST_TOOLS)
        if brief.allowed_tools is None:
            return allowed
        asked = set(brief.allowed_tools)
        widened = sorted(asked - FAST_TOOLS)
        if widened:
            raise BriefRefused(
                "the fast path may not hold "
                + ", ".join(widened)
                + " — it has no tool that can change anything (design §8.1). "
                "Open a task instead."
            )
        return sorted(asked & set(allowed))

    @staticmethod
    def _approver(brief: Brief, permit: PermissionCallback):
        """v1's `approve(tool, args) -> bool` over v2's `permit(name, args, brief)`.

        Wrapped in `permissions.gate`, which is what carries v1's
        `jarvis_human_backed` flag — the flag `dispatch()` checks before it
        honours an ALLOW verdict from `rules.py`. Calling `permit` directly
        would strip it, and stripping it is the "second copy of an idea drifts"
        failure v1 already paid for once with attended tasks.

        In practice no `FAST_TOOLS` entry is dangerous, so `dispatch()` never
        reaches an approver here. It is built anyway because `Agent` must never
        be constructed with `approve=None` — that is the one forbidden mistake,
        and leaving a hole open because today's toolset does not walk through it
        is how tomorrow's does.
        """

        def ask(tool: Any, args: dict) -> bool:
            name = getattr(tool, "name", str(tool))
            try:
                decision = Decision(permit(name, dict(args), brief))
            except (ValueError, TypeError):
                return False  # an unreadable answer is a denial, as everywhere
            return decision is Decision.ALLOW

        return permissions.gate(ask)

    def close(self, h: SessionHandle) -> None:
        """Drop the agent and stop writing to the session. Idempotent."""
        native = h.native
        if native is None or native.closed:
            return
        native.closed = True
        native.stop.set()
        # The transcript on disk is already current — `run_turn` records after
        # every turn — so this only stops *future* writes.
        native.agent.session = None
        h.native = None

    def interrupt(self, h: SessionHandle) -> None:
        """Cancel the turn in flight. v1 lands it at the next step boundary."""
        if h.native is not None:
            h.native.stop.set()

    def answer(self, h: SessionHandle, req_id: str, decision: Decision | str) -> None:
        raise ValueError(
            "the fast path raises no approvals and asks no questions — none of "
            "its tools is dangerous (design §8.1), so there is nothing to answer"
        )

    def usage(self, h: SessionHandle) -> Usage:
        """Dollars spent on this handle since `start`. Tokens are not exposed.

        v1 reports `prompt_tokens` per reply into a `TokenMeter` that keeps only
        the latest measurement — a compaction threshold, not a running total —
        so there is no honest per-handle token count to give. 0 rather than a
        guess: §8.3's ledger denominates thresholds in this number, and the v1
        BYOK lesson is that a wrong number is worse than a missing one.
        """
        native = h.native
        return Usage(cost_usd=native.cost_usd if native is not None else 0.0)

    # -- the turn

    def send(self, h: SessionHandle, message: UserMessage) -> Iterator[Event]:
        """Run one turn, yielding events as they happen.

        v1's loop is synchronous and reports through an `on_event` callback, so
        the turn runs on a worker thread and the callback writes into a queue
        this generator drains. The queue is what makes the events *live* rather
        than a list handed back at the end — the HUD draws a reply while it is
        being written, and that only works if `TEXT_DELTA` arrives during the
        model call.

        The generator must never hang, so the worker's `finally` always posts a
        sentinel: a raise inside `run_turn` becomes `ERROR(fatal)` and a raise
        anywhere else still terminates the stream.
        """
        native = h.native
        if native is None or native.closed:
            yield Event(EventKind.ERROR, h.thread_id, {"message": "session closed", "fatal": True})
            return
        if not native.busy.acquire(blocking=False):
            yield Event(
                EventKind.ERROR,
                h.thread_id,
                {"message": "a turn is already running on this thread", "fatal": True},
            )
            return
        try:
            yield from self._run(h, native, message)
        finally:
            native.busy.release()

    def _run(self, h: SessionHandle, native: _Native, message: UserMessage) -> Iterator[Event]:
        thread_id = h.thread_id
        events: queue.Queue = queue.Queue()
        done = object()

        # A cancel aimed at the *previous* turn must not kill this one — the v1
        # face clears its event under the agent lock right before run_turn for
        # exactly this reason, and we hold `busy` here.
        native.stop.clear()

        # The proposal slot this turn writes into. Bound on the worker thread,
        # where `task_propose` will run.
        proposal: dict[str, Any] = {}

        state = _TurnState(thread_id, events)
        native.agent.on_event = state.on_event

        result: dict[str, Any] = {}

        def work() -> None:
            try:
                from ... import runtime

                runtime.bind(proposal=proposal)
                result["turn"] = native.agent.run_turn(
                    message.text, images=list(message.images) or None
                )
            except BaseException as exc:  # noqa: BLE001 — reported, never swallowed
                result["error"] = exc
            finally:
                events.put(done)

        worker = threading.Thread(
            target=work, name=f"jarvis-fast-{thread_id}", daemon=True
        )
        yield Event(EventKind.TURN_STARTED, thread_id)
        worker.start()
        while True:
            item = events.get()
            if item is done:
                break
            yield item
        worker.join()

        error = result.get("error")
        if error is not None:
            yield Event(
                EventKind.ERROR,
                thread_id,
                {"message": f"{type(error).__name__}: {error}", "fatal": True},
            )
            return

        turn = result["turn"]
        native.cost_usd += turn.cost_usd
        native.turns += 1
        yield Event(
            EventKind.TURN_FINISHED,
            thread_id,
            {"stop": _stop_reason(turn), "proposal": _proposal(proposal, turn)},
        )


def _stop_reason(turn) -> str:
    if turn.cancelled:
        return "interrupted"
    if turn.stopped_early:
        return "max_turns"
    return "end"


def _proposal(slot: dict[str, Any], turn) -> dict[str, Any] | None:
    """The task this turn wants opened, or None. Design §8.1.

    An explicit `task_propose` wins over exhaustion: the model saying what the
    task is beats the loop inferring it from where the model stopped.

    The budget rule is the other half. A turn that burned its steps with the
    request unmet must not report "stopped" — v1 already spends one tool-free
    call at the wall writing DONE/OPEN/NEXT so the work is resumable, and that
    text is exactly the brief a task needs. Exhaustion becomes a proposal.
    """
    if slot:
        return dict(slot)
    if turn.stopped_early:
        return {
            "brief": "[fast path ran out of steps] " + (turn.text or ""),
            "project": None,
            "provider": None,
        }
    return None


class _TurnState:
    """Translates one turn's v1 `on_event` callbacks into `Event`s.

    Lives per turn rather than per handle because the call-id bookkeeping is
    per turn: v1 names tools, not calls, so the ids are minted here and matched
    back by order.
    """

    def __init__(self, thread_id: str, events: queue.Queue):
        self.thread_id = thread_id
        self.events = events
        self.calls = 0
        # (call_id, name) for every tool_start not yet answered. v1 fires all
        # of a batch's starts, then all of its ends, in the same order — so
        # matching the *first* pending entry with the right name is exact, and
        # keying by name keeps it exact even if that ever stops holding.
        self.pending: deque[tuple[str, str]] = deque()
        # Cost arrives cumulative-per-turn from v1. USAGE events carry the
        # increment instead, so a consumer that sums them gets the right
        # number — the alternative is every surface knowing to take a max.
        self.cost = 0.0

    def emit(self, kind: EventKind, data: dict[str, Any]) -> None:
        self.events.put(Event(kind, self.thread_id, data))

    def on_event(self, kind: str, data: Any) -> None:
        handler = getattr(self, f"_on_{kind}", None)
        if handler is not None:
            handler(data)

    # v1 kinds. Anything not listed (context stats) is deliberately dropped:
    # it is harness bookkeeping, not conversation.

    def _on_delta(self, piece: Any) -> None:
        if piece:
            self.emit(EventKind.TEXT_DELTA, {"text": str(piece)})

    def _on_interim_text(self, text: Any) -> None:
        if text:
            self.emit(EventKind.THINKING, {"text": str(text)})

    def _on_text(self, text: Any) -> None:
        self.emit(EventKind.TEXT, {"text": str(text or "")})

    def _on_tool_start(self, data: Any) -> None:
        name, raw = data
        self.calls += 1
        call_id = f"fast-{self.calls}"
        self.pending.append((call_id, name))
        self.emit(
            EventKind.TOOL_STARTED,
            {"call_id": call_id, "name": name, "args": _args(raw)},
        )

    def _on_tool_end(self, data: Any) -> None:
        name, text = data
        call_id = self._claim(name)
        text = str(text or "")
        self.emit(
            EventKind.TOOL_FINISHED,
            {
                "call_id": call_id,
                "name": name,
                # v1's contract: a tool failure comes back as *text*, never a
                # raise (invariant 4), so this is prefix matching and cannot be
                # anything else. "Error" is the general failure, "Refused" the
                # DENY verdict, and the decline is what a denied approval
                # returns — which is a call that did not happen and must not
                # render as one that did.
                "ok": not text.startswith(_NOT_OK),
                "summary": text[:SUMMARY_CHARS],
            },
        )

    def _on_cost(self, total: Any) -> None:
        total = float(total or 0.0)
        delta, self.cost = total - self.cost, total
        self.emit(
            EventKind.USAGE,
            {
                "input": 0,
                "output": 0,
                "cached": 0,
                "cost_usd": delta,
                "provider_reported": {"turn_cost_usd": total},
            },
        )

    def _on_truncated(self, step: Any) -> None:
        self.emit(EventKind.THINKING, {"text": "[cut off at the token limit, continuing]"})

    def _on_cancelled(self, step: Any) -> None:
        # The real TURN_FINISHED is emitted by _run once run_turn returns, so
        # the stop reason is read off the Turn rather than guessed here. This
        # hook exists so the kind is handled rather than silently unknown.
        return

    def _claim(self, name: str) -> str:
        for index, (call_id, pending_name) in enumerate(self.pending):
            if pending_name == name:
                del self.pending[index]
                return call_id
        return "fast-unmatched"


def _args(raw: Any) -> dict[str, Any]:
    """v1 hands tool arguments over as the model's raw JSON string.

    A model can emit something that is not an object (or not JSON at all), and
    a surface must still be able to render the call. So an unparseable argument
    string is reported as what it is rather than dropped.
    """
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {"raw": str(raw)}
    return parsed if isinstance(parsed, dict) else {"raw": str(raw)}
