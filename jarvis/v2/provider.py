"""Provider interface — design §5. Fixed; implementations live in providers/.

Every provider translates its native stream into `Event`s. Nothing above this
layer may know what a stream-json line or an app-server notification looks
like. A provider that cannot honour a Brief raises `BriefRefused` rather than
silently narrowing it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Protocol

from .model import PermissionProfile, ProviderName, Role, Thread


class BriefRefused(Exception):
    """The provider cannot run this brief as stated (tool it cannot load, profile it lacks)."""


class SessionLost(BriefRefused):
    """A model switch failed and the old session could not be restored either.

    The provider has closed the session; nothing is left to send on. The
    owner of the handle should drop it and resume the thread afresh.
    """


class SteerRefused(Exception):
    """A provider cannot take a message into the turn it is running right now.

    Raised by the optional `steer()` (see `Provider`). Never fatal: the turn
    goes on untouched. `fallback` says what the caller should do instead:

    - ``"queue"`` — the message waits and runs as its own turn when this one
      ends (no turn running yet, one ending, a provider mid-approval);
    - ``"interrupt"`` — this provider has no way to steer at all (a Codex
      without `turn/steer`), so the caller interrupts the turn and runs the
      message next, saying the previous turn was interrupted by the owner.
    """

    def __init__(self, reason: str, fallback: str = "queue"):
        super().__init__(reason)
        self.fallback = fallback if fallback in ("queue", "interrupt") else "queue"


@dataclass
class Brief:
    role: Role
    cwd: str                                    # the task worktree (or project root for chat)
    system_append: str = ""                     # role brief text, design §10.2
    profile: PermissionProfile = PermissionProfile.AUTO
    model: str | None = None
    effort: str | None = None
    allowed_tools: list[str] | None = None      # None -> the role's default set
    mcp_servers: dict[str, dict] = field(default_factory=dict)   # name -> config
    always_ask: list[str] = field(default_factory=list)          # §6 layer 2, project + global
    max_turns: int | None = None
    task_id: str | None = None                  # attribution on approvals


class EventKind(str, Enum):
    TURN_STARTED = "turn_started"
    TEXT_DELTA = "text_delta"
    TEXT = "text"
    THINKING = "thinking"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    REVIEWER_DECLINED = "reviewer_declined"     # design §6.1
    QUESTION = "question"                       # provider asked the user something (Codex requestUserInput)
    PLAN_UPDATED = "plan_updated"
    USAGE = "usage"
    TURN_FINISHED = "turn_finished"
    ERROR = "error"


@dataclass
class Event:
    kind: EventKind
    thread_id: str
    data: dict[str, Any] = field(default_factory=dict)
    # Conventions for `data`, per kind:
    #   text_delta/text/thinking: {"text": str}
    #   tool_started: {"call_id", "name", "args": dict}   tool_finished: {"call_id", "name", "ok": bool, "summary": str}
    #   approval_requested: {"req_id", "tool", "args", "command": str|None}
    #     ("the gate was consulted": the daemon logs these two as gate_requested/
    #     gate_resolved and never publishes them; owner questions are the broker's)
    #   reviewer_declined: {"tool", "args", "command": str|None, "reason": str}
    #   question: {"req_id", "text", "options": list[str]}
    #   usage: {"input", "output", "cached", "cost_usd": float|None, "provider_reported": dict}
    #   turn_finished: {"stop": "end"|"max_turns"|"interrupted"|"error"}
    #   error: {"message": str, "fatal": bool}


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None               # None when the provider does not report dollars

    @property
    def work_tokens(self) -> int:               # design §8.3: uncached input + output
        return max(0, self.input_tokens - self.cached_tokens) + self.output_tokens


@dataclass
class SessionHandle:
    thread_id: str
    provider: ProviderName
    provider_session_id: str | None
    native: Any = None                          # the provider's own object; opaque above this layer


@dataclass
class UserMessage:
    text: str
    images: list[dict] = field(default_factory=list)     # [{"b64":..., "mime":...}]
    origin: str = "owner"                                 # "owner" | "system" | "owner-ran" (§6.1)
    # A skill the owner invoked by name (`/skill`). Each provider maps it onto
    # its own mechanism: the fast path injects the body, Claude and Codex get a
    # directive to use the skill they have installed (`commands.skill_directive`).
    # Validated against `commands.invocable_skills()` before it gets here.
    skill: str | None = None
    # Where the message came from and what the owner actually wrote (PR C,
    # plan §4.3). `via` is "hud" | "discord" | "dm" | "system" | "peer"; None
    # means "hud" for an owner message and "system" for anything else.
    # `typed` is the owner's words before `assemble_turn` inlined any file
    # (None: the text is the typed words); `attachments` are file names
    # only, never contents. `spoken` marks dictation or a voice note. The two
    # Discord ids say which message ran the turn and in which channel, so a
    # reply goes back there and a duplicate delivery runs once. None of these
    # reach a provider: they are for the log, the bus and the mirror.
    via: str | None = None
    typed: str | None = None
    attachments: list[str] = field(default_factory=list)
    spoken: bool = False
    discord_message_id: str | None = None
    discord_channel_id: str | None = None


# Permission callback the daemon hands every provider: the five layers of §6
# live behind it, never in a provider. Returns a Decision; blocks while a human
# is asked. Providers call it for every tool use their native permission system
# does not settle first (auto mode allow), and MUST call it for anything on the
# brief's always_ask list regardless of what their reviewer said.
# Codex also passes `widening="SANDBOX WIDENING: …"` (keyword-only) for an
# approval that would widen its sandbox; `permissions.build_permit` asks a human
# for it every time, and a callback that does not take the keyword denies.
PermissionCallback = Callable[[str, dict, Brief], Decision]   # (tool_name, args, brief)


class Provider(Protocol):
    name: ProviderName

    def health(self) -> tuple[bool, str]:
        """(ok, reason). Version pin, login, binary present. Never spends tokens."""

    def start(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle: ...

    def resume(self, thread: Thread, brief: Brief, permit: PermissionCallback) -> SessionHandle: ...

    def send(self, h: SessionHandle, message: UserMessage) -> Iterator[Event]:
        """Blocks for the turn; yields events in order; ends with TURN_FINISHED or ERROR(fatal)."""

    def interrupt(self, h: SessionHandle) -> None: ...

    def answer(self, h: SessionHandle, req_id: str, decision: Decision | str) -> None:
        """Answer an APPROVAL_REQUESTED (Decision) or a QUESTION (str) the provider is blocked on."""

    def usage(self, h: SessionHandle) -> Usage: ...

    def close(self, h: SessionHandle) -> None: ...

    # Optional, like `set_model` (2026-10-08, steering). A provider without
    # `steer` has every message sent during a turn queued behind it.
    #
    # def steer(self, h: SessionHandle, message: UserMessage) -> None:
    #     """Deliver `message` into the turn running on `h`, at the provider's
    #     next safe point — never inside a tool batch, and never as an answer
    #     to anything the turn is waiting on. Returns once the provider has
    #     taken it; raises `SteerRefused` when it cannot. Called from a thread
    #     other than the one draining `send()`."""
    #
    # def undelivered(self, h: SessionHandle) -> list[UserMessage]:
    #     """Steered messages the provider took but that never reached the
    #     model before its turn ended (the fast path's final answer came
    #     first). The very objects passed to `steer`; the caller runs them
    #     next. Emptied by the call."""
