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

    def __init__(self, reason: str, fallback: str = "queue", *, before_write: bool = False):
        super().__init__(reason)
        self.fallback = fallback if fallback in ("queue", "interrupt") else "queue"
        # Only the provider can prove no delivery was attempted. A generic
        # refusal/timeout after a write is ambiguous and must not restore desk.
        self.before_write = before_write is True


class SubagentStopUnsupported(Exception):
    """The provider cannot stop this one native sub-agent by itself (SV-4).

    Raised by the optional `stop_subagent()` (see `Provider`); the message is
    one plain sentence for the owner. Nothing was sent to the CLI."""


class SubagentStopDeclined(Exception):
    """The CLI answered the owner's Stop with a refusal of its own (an error
    control response, such as "no such task") (SV-4). The request reached
    it and was turned down: nothing will stop the child, so this is neither
    "not live" nor an unconfirmed send. The message is the CLI's reason."""


class SubagentNotLive(Exception):
    """The sub-agent named is not one this session still runs (SV-4): unknown
    to it, already ended, or the session is closed. Nothing was sent."""


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
    origin_tag: str | None = field(default=None, repr=False)  # AO-1b-i: run copy only, never saved


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
    # A native sub-agent of the session (SV-1, 2026-10-15): Claude Code's
    # Agent children, and Codex's child agents (SV-2). Kinds of their own,
    # never the old kinds with a tag: a consumer that does not know them
    # (Discord, the runner, titles, the ledger, the HUD's prose, voice)
    # ignores them, so a child's words can never pass for the orchestrator's.
    SUBAGENT = "subagent"                       # lifecycle
    SUBAGENT_EVENT = "subagent_event"           # one thing a sub-agent said or did
    # A line for the owner about the session itself, not from the model
    # (2026-10-16: Codex's Windows closure skipped). Shown as a system line;
    # consumers that do not know it ignore it, and the model never sees it.
    NOTICE = "notice"


@dataclass
class Event:
    kind: EventKind
    thread_id: str
    data: dict[str, Any] = field(default_factory=dict)
    # Conventions for `data`, per kind:
    #   text_delta/text/thinking: {"text": str}
    #   tool_started: {"call_id", "name", "args": dict} (+ daemon label)
    #   tool_finished: {"call_id", "name", "ok": bool, "outcome": "ok"|"failed"|"denied", "summary": str}
    #   approval_requested: {"req_id", "tool", "args", "command": str|None}
    #     ("the gate was consulted": the daemon logs these two as gate_requested/
    #     gate_resolved and never publishes them; owner questions are the broker's)
    #   reviewer_declined: {"tool", "args", "command": str|None, "reason": str}
    #   question: {"req_id", "text", "options": list[str]}
    #   usage: {"input", "output", "cached", "cost_usd": float|None, "provider_reported": dict}
    #     (Codex also: "partial_turns": int, the session's turns that ended
    #     interrupted or failed, whose usage may be under-reported; nonzero
    #     makes its token counts a lower bound)
    #     (Claude also: "cost_total": float|None, the CLI's running total its
    #     `cost_usd` was measured against, which the daemon saves on the thread)
    #   turn_finished: {"stop": "end"|"max_turns"|"interrupted"|"error"}
    #   error: {"message": str, "fatal": bool}
    #   notice: {"text": str}, a fixed sentence (never a path or file name)
    #   subagent: {"sid", "op": "started"|"progress"|"finished", "status":
    #     "running"|"backgrounded"|"done"|"failed"|"stopped", "task_id",
    #     "parent_sid", "depth", "background", "name", "kind", "last_tool",
    #     "tokens", "tool_uses", "duration_ms", "stop_reason"} (keys present
    #     when known). `sid` is "cl:<the spawning Agent call's id>" (Claude) or
    #     "cx:<the child's threadId>" (Codex, SV-2). Never a `usage`: a
    #     sub-agent's tokens ride here, outside the ledger.
    #   subagent_event: {"sid", "event": {"kind", "data"}}, the inner kind one
    #     of text, thinking, tool_started, tool_finished, reviewer_declined
    #     (each with the data above) or result {"text", "status"}: the CLI's
    #     closing summary. Never a delta.
    #   approval_requested from a sub-agent's call also carries "subagent": sid.


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
    # "owner" | "system" | "owner-ran" (§6.1) | "chat" (a message from another
    # HUD chat, chat-to-chat §5.2) | "chat-reply" (that chat's reply coming
    # back, §5.3). Both chat origins arrive with via="peer" and desk=False,
    # and neither is ever the owner's: `desk.desk_message` requires "owner".
    origin: str = "owner"
    # A skill the owner invoked by name (`/skill`). Each provider maps it onto
    # its own mechanism: the fast path injects the body, Claude and Codex get a
    # directive to use the skill they have installed (`commands.skill_directive`).
    # Validated against `commands.invocable_skills()` before it gets here.
    skill: str | None = None
    # Where the message came from and what the owner actually wrote (PR C,
    # plan §4.3). `via` is "hud" | "discord" | "dm" | "system" | "peer" |
    # "inbox" (the owner's answer to a decision, inbox B1: queued, never a
    # steer, never at the desk) | "phone" (the same answered on the phone,
    # P-4; or typed into a chat on the phone, P-5: the owner's queue, never a
    # steer, never at the desk, never Bypass); None means "hud" for an owner
    # message and "system" for anything else.
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
    # The owner is at the desk (WP-F): set only by the HUD's own send route,
    # and only when the request came through the HUD's listener with the
    # HUD's Origin (`projects.is_owner`) — never by Discord, the API
    # listener every tool's HTTP client uses, the escape hatch or a schedule.
    # The fast path and Claude session bridge bind terminal-reading authority
    # only for such a turn; the relay itself cannot grant it.
    desk: bool = False


# Permission callback the daemon hands every provider: the five layers of §6
# live behind it, never in a provider. Returns a Decision; blocks while a human
# is asked. Providers call it for every tool use their native permission system
# does not settle first (auto mode allow), and MUST call it for anything on the
# brief's always_ask list regardless of what their reviewer said.
# Codex also passes `widening="SANDBOX WIDENING: …"` (keyword-only) for an
# approval that would widen its sandbox; `permissions.build_permit` asks a human
# for it every time, and a callback that does not take the keyword denies.
# Claude passes `subagent="<label>"` (keyword-only, SV-1) for a call one of its
# Agent children makes: it names the child on the card and decides nothing. It
# is passed only to a callback that takes it, so the answer never depends on it.
# Claude also passes `shell_cwd="<dir>"` (keyword-only, 2026-10-15): its hook
# input's `cwd`, the folder its shell is in, where `merge_gate` reads the
# current branch of a push with no refspec. Only to a callback that takes it.
PermissionCallback = Callable[[str, dict, Brief], Decision]   # (tool_name, args, brief)


def takes_keyword(callback: Any, name: str) -> bool:
    """Whether `callback` takes the keyword `name` (or any keyword). A
    provider hands a permission callback an optional keyword (`subagent`,
    `subagent_sid`, `shell_cwd`, `tool_use_id`) only when it does: one that does not would raise, and a
    raise denies — such metadata must never change an answer."""
    import inspect
    try:
        params = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_KEYWORD
               or (p.name == name and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD))
               for p in params)


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
    # def steer(self, h: SessionHandle, message: UserMessage) -> bool | None:
    #     """Deliver `message` into the turn running on `h`, at the provider's
    #     next safe point — never inside a tool batch, and never as an answer
    #     to anything the turn is waiting on. Returns once the provider has
    #     taken it; raises `SteerRefused` when it cannot. An unconfirmed receipt
    #     may return False: still delivered for queue policy, but not accepted
    #     as Browser owner context. True or legacy None confirms delivery.
    #     Called from a thread
    #     other than the one draining `send()`."""
    #
    # Optional (SV-4, 2026-10-15): the owner's Stop on one native sub-agent.
    # A provider without them offers no per-child Stop.
    #
    # def subagent_stop_refusal(self, h: SessionHandle, sid: str) -> str | None:
    #     """None when `stop_subagent(h, sid)` would be sent now; otherwise one
    #     plain sentence saying why not. Cheap, sends nothing."""
    #
    # def stop_subagent(self, h: SessionHandle, sid: str) -> None:
    #     """Ask the CLI to stop that one child, and nothing else: never the
    #     orchestrator's turn, never a sibling. Returns once the request is
    #     written (and, where the CLI answers control requests, answered); the
    #     child's end arrives later as its own SUBAGENT `finished`, with
    #     `stop_reason` "stopped by the owner". Raises `SubagentNotLive` or
    #     `SubagentStopUnsupported` without sending anything. Called from a
    #     thread other than the one draining `send()`."""
    #
    # Optional (SV-6, 2026-10-16): reading between turns. A provider without
    # `watch` is read only by its turns.
    #
    # def watch(self, h: SessionHandle) -> Iterator[Event]:
    #     """For the session's life, what the provider reads while no turn
    #     runs: SUBAGENT/SUBAGENT_EVENT; APPROVAL_REQUESTED/RESOLVED of a gate
    #     question asked between turns (logged with no turn; the decision is
    #     `permit`'s); ERROR (a fatal one: the provider closed the session);
    #     and TURN_STARTED `{"self_started": origin}` when the CLI started a
    #     turn by itself. Ends when the session closes. One watcher per
    #     session, on a thread of the daemon's; never two readers of the
    #     stream at once."""
    #
    # def cli_turn_pending(self, h: SessionHandle) -> bool:
    #     """Whether a turn the CLI started by itself waits to be adopted.
    #     Cheap, non-blocking: the daemon asks under its lock."""
    #
    # def adopt(self, h: SessionHandle) -> Iterator[Event]:
    #     """That turn, read as a send's would be (TURN_STARTED, ..., USAGE,
    #     TURN_FINISHED), with no message of ours. Yields nothing when none
    #     waits. Called only when no send runs."""
    #
    # Optional (2026-10-19, the HUD's Compact button): the provider's own
    # compaction of the conversation. The transcript keeps every row; only
    # the model's context gets smaller. A provider without `compact` is
    # refused by the daemon ("This chat's provider cannot compact its
    # conversation."). Reached only from the owner's HUD route
    # (`POST /threads/{id}/compact`): no tool, bridge, Discord or CLI path.
    #
    # def compact_refusal(self, h: SessionHandle) -> str | None:
    #     """None when `compact(h)` would run now; otherwise one plain
    #     sentence saying why not (the daemon's 409 says exactly that).
    #     Cheap, sends nothing; asked under the daemon's lock."""
    #
    # def compact(self, h: SessionHandle) -> Iterator[Event]:
    #     """A turn with no message: TURN_STARTED; a TOOL_STARTED /
    #     TOOL_FINISHED pair named "compact"; any USAGE; then TURN_FINISHED
    #     {"stop": "end"|"error"|"interrupted", "compacted": bool,
    #     "reason": str | None}. `interrupt(h)` ends it ("interrupted",
    #     nothing applied). Never blocks on a running turn: busy ends it
    #     "error" at once. Called only when no send runs; a steer meanwhile
    #     is refused (the daemon queues instead). `reason` is the provider's
    #     own words, which the daemon scrubs before it logs them."""
    #
    # def undelivered(self, h: SessionHandle) -> list[UserMessage]:
    #     """Steered messages the provider took but that never reached the
    #     model before its turn ended (the fast path's final answer came
    #     first). The very objects passed to `steer`; the caller runs them
    #     next. Emptied by the call."""
