"""FastPathProvider — v1's agent loop behind the v2 provider interface.

Design §2 D2, §5.5 and §8.1. v1's OpenRouter loop survives as exactly one
thing: the conversation fast path. Voice turns, Discord chat that is not a task
verb, quick questions. A CLI session costs seconds to start and subscription
quota per turn, and the voice surface's whole budget is latency — so chat keeps
the cheap loop and everything that resembles *large* work is handed to a task.

Amended 2026-10-14 (owner, fast-path-edits decisions F-1..F-7,
docs/plans/2026-10-14-fast-path-edits-decisions.md): the owner's own chat turn
at the HUD also holds `EDIT_TOOLS` — `write_file`, `edit_file`, `run_command`
and `run_readonly` — so small, direct work is done in place, in the thread's
project folder, instead of being proposed. Every other turn is read-only, as
before.

Three rules make that split safe, and all are mechanical rather than prompted:

**The structural boundary (§8.1).** `FAST_TOOLS` has no generic write, shell,
desktop or spawn tool. Its typed Browser action exception is independently
gated by the owner's global and pane switches. Read-only Browser looks require
the live desk and pane permission. `EDIT_TOOLS` are added only for a turn
where the owner is at the desk (`_owners_chat` and `UserMessage.desk`, the
`DESK_TOOLS` rule); any other turn's request carries no schema for them and
dispatch refuses them by name (invariant 10). A steer from anywhere else takes
the turn off the desk, and the toolset with it, at the next step boundary.
Toolset and per-tool gates enforce this; prompts do not authorize an action.
The prompt carries the escalation *test*, but the toolset is what makes a
misjudgement harmless.

**The gate (§6, decision F-3).** An edit or a command goes through the
daemon's §6 permit, the one every provider is handed: layer 1 refusals
(protected state, credentials, env files, owner ops), the always-ask rules,
the owner's allowlist and rules' ALLOW for a human-backed asker (layer 4),
and otherwise the owner, through the broker. See `_approver`.

**The budget rule (§8.1).** A fast-path turn that burns its steps (eight;
thirty on a turn holding the edit tools) with the request unmet does not
report "stopped". v1 already spends one tool-free call at the wall to write a
handoff (`Agent._handoff`); here that handoff becomes the brief of a proposed
task. The v1 exhaustion handoff, pointed at a task instead of at the owner.

Everything else here is translation: one v1 `Agent` per handle, its `on_event`
callbacks turned into the `Event` stream every v2 surface reads.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from ... import config, models, sessions, tools
from ...agent import Agent
from ..desk import desk_message as _at_desk
from ..model import PermissionProfile, ProviderName, Role, Thread
from ..provider import (
    Brief,
    BriefRefused,
    Decision,
    Event,
    EventKind,
    PermissionCallback,
    SessionHandle,
    SteerRefused,
    Usage,
    UserMessage,
)
from ..tools import propose as _propose  # noqa: F401  (registers task_propose)
from ..tools import schedules as _schedules  # noqa: F401  (registers schedule_*)
from ..tools import hud_browser_view as _hud_browser_view  # noqa: F401
from ..tools import hud_browser_act as _hud_browser_act  # noqa: F401
from ..tools import terminal_read as _terminal_read  # noqa: F401  (registers terminal_read)
from ..tools import chats as _chats  # noqa: F401  (registers the chat reads)

# Eight steps, per §8.1. Not a work limit — a *definition*: anything that needs
# more than eight rounds of read-and-think is work, and work goes to a task.
# Small enough that exhaustion happens early and cheaply, which is the point,
# because exhaustion here produces a proposal rather than a failure.
FAST_MAX_STEPS = config.FAST_STEPS
# A turn holding the edit tools (decision F-4, 2026-10-14): read, edit, check
# and fix a small change takes more rounds than answering a question does.
# Exhaustion still becomes a proposal. Chosen per turn in `_run`.
FAST_EDIT_MAX_STEPS = config.FAST_EDIT_STEPS

# How much of a tool result goes into TOOL_FINISHED.summary. A surface renders
# this in a ticker; the transcript already holds the whole thing.
SUMMARY_CHARS = 200

# How a v1 tool result says "this did not happen". See `_TurnState._on_tool_end`.
_NOT_OK = ("Error", "Refused", "The user declined")
# The exact framing written by tools.dispatch and its approval gate.
_DENIAL_PREFIXES = ("Refused: ", "The user declined to run this.")
# These results carry stored/file/page content, which can start with anything.
_RAW_CONTENT_TOOLS = frozenset(("memory_read", "session_summary", "session_read",
                               "read_file", "fetch_page", "skill_read",
                               "chat_list", "chat_summary", "chat_search", "chat_read"))

# Written for the owner's desk turn holding the edit tools (decision F-5,
# 2026-10-14). `config.narrow_prompt` — which `Agent.__init__` and
# `_turn_tools` apply — turns it into the read-only wording every other turn
# had before, byte for byte, by `config.PROMPT_TOOL_PARAGRAPHS`.
FAST_PROMPT = (
    "\n\n## This conversation is the fast path\n\n"
    + config.FAST_LIMITS_PARAGRAPH + "\n\n"
    + config.FAST_DIRECT_PARAGRAPH + "\n\n"
    + config.FAST_PROPOSE_PARAGRAPH + "\n\n"
    + config.FAST_DRIVE_PARAGRAPH + "\n\n"
    # Narrowed away with the chat reads: a task's thread holds none, and its
    # prompt is then what it was before them, byte for byte.
    + config.FAST_CHATS_PARAGRAPH + "\n\n"
    + """**Answer directly** when the request is a question you can settle from memory,
this transcript, one read or one fetch; a control request (mute, avatar,
music); or ordinary conversation. Most turns are this.

When you are unsure, propose. A task that turns out to be small costs a little;
a real job answered as if it were chat costs the owner their trust in the
answer.
"""
)


# --- the toolset ------------------------------------------------------------

# §8.1's structural boundary, written out by name.
#
# An allowlist, and the same reasoning as `tools.PARALLEL_SAFE` and
# `config.DESKTOP_APPS`: "can this change something" has to be answered once per
# tool by a person, because the cost of guessing wrong is a chat turn quietly
# doing half a job. A denylist would silently admit every tool added later.
DRIVE_TOOLS: frozenset[str] = frozenset({"hud_browser_act"})

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
        # the owner's HUD terminals, read-only (WP-F, decisions W-2) — and
        # only in an owner's chat turn typed in the HUD: `DESK_TOOLS`.
        "terminal_read",
        "hud_browser_view",
    }
) | DRIVE_TOOLS

# Tools a fast-path handle holds only for a turn where the owner is present:
# an owner's chat thread (not a task's), the message typed in the HUD's own
# window (`UserMessage.desk`). Any other turn — Discord, the DM, the escape
# hatch, a schedule — runs without them in the request, and the tool itself
# refuses unless `runtime.at_desk()` (fail closed). A steer from anywhere but
# the desk takes the turn off the desk for the rest of it.
DESK_TOOLS: frozenset[str] = frozenset({"terminal_read", "hud_browser_view"}) | DRIVE_TOOLS

# The other HUD chats, read-only (chat-to-chat plan C1, §4.2; decision CC-1).
# Held on every turn of an owner's chat — Discord, the DM and schedules
# included, desk or not — and never by a task's thread or another role, so
# not in `FAST_TOOLS`, which every brief may hold. The tools refuse without a
# v2 caller and the daemon's chat service.
CHAT_TOOLS: frozenset[str] = frozenset({"chat_list", "chat_summary", "chat_search", "chat_read"})

# The tools the owner's own chat turn at the HUD gains (decisions F-1, F-2,
# 2026-10-14): small, direct work done in place. Held exactly as `DESK_TOOLS`
# are — an owner's chat brief, a message typed or spoken at the HUD, and not
# steered from anywhere else since — and never in `FAST_TOOLS`, so every other
# turn (Discord, the DM, a schedule, the escape hatch, a task's thread) stays
# read-only. The only dangerous tools the fast path may ever hold.
EDIT_TOOLS: frozenset[str] = frozenset(config.FAST_EDIT_TOOLS)

# Of those, the ones v1's registry marks safe but the fast path still puts to
# its gate (decision F-3: "the tools ... go through the existing gate"): each
# reaches the daemon's §6 permit as `run_command` does (`runtime.ask_tools`).
# `run_readonly` is here too (review of PR #182, H1): its own allowlist of
# read-only programs says nothing about *what* they read, so on a desk turn
# `cat ~/.config/gh/hosts.yml`, a grep of the Browser profile, or `cat` of a
# project symlink into ~/.ssh returned credentials with nobody asked — and
# `fetch_page` could carry them out. Through the permit, rules' ALLOW still
# runs an ordinary read (`ls`, `cat src/app.py`) unasked for a human-backed
# asker, the always-ask `credentials-path` rule asks, and the approver's desk
# check covers it.
ASKED_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "run_readonly"})

# Names that must never be in FAST_TOOLS or EDIT_TOOLS, asserted below and
# again in the suite. Kept as data rather than left to a reader's eye because
# the failure mode is silent: a tool that slipped in here would not raise, it
# would simply let a chat turn spawn, commit or push. (The four edit tools
# left this list on 2026-10-14 for `EDIT_TOOLS`, decision F-7; nothing else
# did.)
FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        "run_subagent",
        "run_fleet",
        "git",
        "git_worktree",
        "git_pull_request",
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

# "discord_": nothing on the fast path legitimately needs Discord — chat
# replies are posted by the surface, not by a tool — and the slash plan's rule
# is that no tool reaches the Discord API at all (S1). `discord_dm_owner`
# stays available to Claude/Codex through jarvis-mcp, which has its own list.
FORBIDDEN_PREFIXES = ("browser_", "desktop_", "cad_", "gmail_", "discord_")


def _validate_toolset() -> None:
    """Fail loudly at import if the boundary does not describe reality.

    Invariant 10's failure mode is the one to avoid here: a toolset that
    silently shrinks is not an error, it is the mechanism quietly not working.
    A renamed or removed v1 tool must stop the process rather than leave the
    fast path one capability short with nothing said.
    """
    every = FAST_TOOLS | EDIT_TOOLS | CHAT_TOOLS
    missing = sorted(name for name in every if name not in tools.REGISTRY)
    if missing:
        raise RuntimeError(
            "FAST_TOOLS, EDIT_TOOLS or CHAT_TOOLS names tools that are not registered: "
            + ", ".join(missing)
            + ". Either the tool was renamed or its module is no longer imported."
        )
    # A dangerous tool only by name, in EDIT_TOOLS — held only at the desk.
    unsafe = sorted(name for name in every
                    if tools.REGISTRY[name].dangerous and name not in EDIT_TOOLS)
    if unsafe:
        raise RuntimeError(f"FAST_TOOLS contains dangerous tools: {', '.join(unsafe)}")
    # The edit tools are the desk turn's additions, never the base set every
    # turn holds; and a desk turn asks for exactly the edits it holds.
    based = sorted(FAST_TOOLS & EDIT_TOOLS)
    if based:
        raise RuntimeError(f"FAST_TOOLS contains desk-only edit tools: {', '.join(based)}")
    # The chat reads are an owner's chat's, never the base set a task holds.
    based = sorted(FAST_TOOLS & CHAT_TOOLS | DESK_TOOLS & CHAT_TOOLS | EDIT_TOOLS & CHAT_TOOLS)
    if based:
        raise RuntimeError(f"CHAT_TOOLS overlaps FAST_TOOLS, DESK_TOOLS or EDIT_TOOLS: {', '.join(based)}")
    if not ASKED_TOOLS <= EDIT_TOOLS:
        raise RuntimeError("ASKED_TOOLS names tools outside EDIT_TOOLS: "
                           + ", ".join(sorted(ASKED_TOOLS - EDIT_TOOLS)))
    leaked = sorted(
        name
        for name in every
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


def _owners_chat(brief: Brief) -> bool:
    """An owner's chat thread's brief: the chat role and no task."""
    return brief.role == Role.CHAT and brief.task_id is None


# Held only on a turn where the owner is at the desk.
_DESK_ONLY: frozenset[str] = DESK_TOOLS | EDIT_TOOLS


def _project_folder(brief: Brief) -> str | None:
    """The thread's project folder, where its edits land and its commands
    start (decision F-6), or None. The daemon makes every brief's `cwd`
    absolute (`Daemon.make_brief`); one that is not, or that holds a
    character a prompt line cannot show as written, or that is not an
    existing folder, binds nothing — and a handle with no folder holds no
    edit tools (`_open`).

    The *physical* folder (`realpath`, review of PR #182, M1): the shell
    runs there, and `cd ..` from a symlinked project leaves the physical
    parent. Judged on the logical path, `cd .. && echo x > jarvis/allowlist.json`
    from a project linked into ~/.config looked harmless and overwrote the
    allowlist."""
    cwd = brief.cwd if isinstance(brief.cwd, str) else ""
    if not cwd or not os.path.isabs(cwd):
        return None
    if any(ch in cwd for ch in "\n\r\x00`") or not cwd.isprintable():
        return None
    real = os.path.realpath(cwd)
    # A folder that is not there binds nothing: an edit would create it.
    return real if os.path.isdir(real) else None


def _summary(name: str, text: str) -> str:
    """TOOL_FINISHED's summary: the result's start — except a desk tool's,
    which is fixed (2026-10-10 review). The summary rides the bus and the
    thread's `log.jsonl`, and a terminal's text must reach neither: the read's
    first line used to, inside the first 200 characters."""
    if name in tools.PRIVATE_RESULT:
        return tools.private_summary(name, text)
    return text[:SUMMARY_CHARS]



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
    # The model and effort the *next* turn runs on. `model=None` follows the
    # global picker, resolved at the start of every turn (a v2 default thread
    # must not stay on the model it was opened with). `effort=None` keeps v1's
    # per-model resolution. Written by `set_model`, read only by `_run`.
    model: str | None = None
    effort: str | None = None
    # Owner steering (2026-10-08). `steer` appends here while a turn is
    # accepting; v1's loop takes it at its next step boundary (invariant 3)
    # as an `[owner steering]` message. What a turn never took — its final
    # answer came first — moves to `leftover`, which the daemon runs next.
    inbox: list = field(default_factory=list)
    inbox_lock: threading.Lock = field(default_factory=threading.Lock)
    accepting: bool = False
    leftover: list = field(default_factory=list)
    # The tools this brief allows (DESK_TOOLS included when it may hold them),
    # and the turn's desk slot (`runtime._DESK`): {"present": bool}.
    tool_names: list = field(default_factory=list)
    desk: dict = field(default_factory=lambda: {"present": False})
    # The thread's project, for `runtime.caller()` (the peers plan's Phase 0).
    project_id: str | None = None
    pristine_system: str = ""
    # The project folder every turn works in (`runtime.working_dir()`,
    # decision F-6), or None: the daemon's own directory, as before.
    workdir: str | None = None


# --- the provider -----------------------------------------------------------


class FastPathProvider:
    """v1's `Agent` as a v2 provider. See the module docstring."""

    name = ProviderName.FAST
    # A raise inside run_turn (a 429 or 5xx that outlasted its retries) is
    # reported as ERROR(fatal), but the Agent and its in-memory transcript
    # are intact; the v1 session is saved only when a turn returns. The
    # daemon keeps the session (PR #12 review F2), so "try again" reaches
    # a model that knows what it already did. Only `close` ends it.
    keeps_session_on_error = True

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
        workdir = _project_folder(brief)
        if workdir is None:
            # Fail closed (review of PR #182, M2): with no folder bound, a
            # relative edit would land in the daemon's own directory.
            tool_names = [name for name in tool_names if name not in EDIT_TOOLS]
        # The folder is stated on every turn, desk or not: relative reads land
        # there too. It names no tool, so no narrowing touches it.
        folder = (f"\n\nYour project folder is `{workdir}`. Relative paths resolve there."
                  if workdir else "")
        pristine_system = (config.SYSTEM_PROMPT + FAST_PROMPT + folder
                           + (brief.system_append or ""))
        native = _Native(
            pristine_system=pristine_system,
            agent=Agent(
                model=brief.model or models.tier("orchestrator"),
                system=pristine_system,
                # Off the desk until a turn says otherwise (`_turn_tools`).
                tool_names=[name for name in tool_names if name not in _DESK_ONLY],
                max_steps=FAST_MAX_STEPS,
                approve=self._approver(brief, permit),
                on_event=lambda kind, data: None,  # replaced per turn by send()
                should_stop=lambda: False,  # replaced below, once `native` exists
                session=session,
            ),
            session=session,
            brief=brief,
            model=brief.model,
            effort=brief.effort,
            tool_names=list(tool_names),
            project_id=thread.project_id,
            workdir=workdir,
        )
        native.agent.should_stop = native.stop.is_set
        native.agent.take_steering = lambda: self._take_steering(native)
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

        `DESK_TOOLS` and `EDIT_TOOLS` are an owner's chat brief's only: any
        other brief naming one is refused, and one naming none never holds
        them. Which turns of an owner's chat hold them is `_turn_tools`'.
        `CHAT_TOOLS` are an owner's chat brief's only too, on every turn.
        """
        if brief.profile is PermissionProfile.STRICT:
            raise BriefRefused(
                "the fast path has no confinement to be strict about; strict "
                "tasks run on the Codex provider (design §6.2)"
            )
        if brief.mcp_servers:
            raise BriefRefused("the fast path cannot load MCP servers; use a CLI provider")
        allowed = _available_tools(FAST_TOOLS | EDIT_TOOLS | CHAT_TOOLS)
        if not _owners_chat(brief):
            # A task's thread (or any role but chat) never holds a desk tool,
            # nor an edit tool (decision F-2).
            if brief.allowed_tools is not None and DESK_TOOLS & set(brief.allowed_tools):
                raise BriefRefused(
                    "desk tools are for the owner's own chat at the HUD, never a task's thread")
            if brief.allowed_tools is not None and EDIT_TOOLS & set(brief.allowed_tools):
                raise BriefRefused(
                    "the fast path's edit tools are for the owner's own chat at the HUD, "
                    "never a task's thread or another role")
            if brief.allowed_tools is not None and CHAT_TOOLS & set(brief.allowed_tools):
                raise BriefRefused(
                    "the chat reads are for the owner's own chats, never a task's thread "
                    "or another role")
            allowed = [name for name in allowed
                       if name not in _DESK_ONLY and name not in CHAT_TOOLS]
        if brief.allowed_tools is None:
            return allowed
        asked = set(brief.allowed_tools)
        if asked & EDIT_TOOLS and not EDIT_TOOLS <= asked:
            # The edit tools come as a set: the desk wording, the 30-step
            # budget and "small direct work" describe all four, and a turn
            # holding `run_command` under "you have no tool that runs a
            # command" would be told something false (review of PR #182, L4).
            raise BriefRefused(
                "the fast path's edit tools are pinned all together or not at all: "
                + ", ".join(sorted(EDIT_TOOLS)))
        widened = sorted(asked - FAST_TOOLS - EDIT_TOOLS - CHAT_TOOLS)
        if widened:
            raise BriefRefused(
                "the fast path may not hold "
                + ", ".join(widened)
                + " — beyond the owner's desk edits it has no tool that can change "
                "anything (design §8.1). Open a task instead."
            )
        return sorted(asked & set(allowed))

    @staticmethod
    def _turn_tools(native: _Native, at_desk: bool) -> list[str]:
        """This turn's toolset: the brief's, without `DESK_TOOLS` and
        `EDIT_TOOLS` unless the owner is at the desk. Rebuilt only when it
        changes (the tools are part of the cached prefix), and the prompt is
        narrowed with it, so the edit wording follows the edit tools.
        Returns the names, which `_take_steering` binds for dispatch."""
        names = [n for n in native.tool_names if at_desk or n not in _DESK_ONLY]
        agent = native.agent
        if names != list(agent._base_tool_names):
            agent._base_tool_names = names
            agent.tool_specs = tools.specs(names)
            agent._base_system = config.narrow_prompt(native.pristine_system, set(names))
        return names

    @staticmethod
    def _approver(brief: Brief, permit: PermissionCallback):
        """v1's `approve(tool, args) -> bool` over v2's `permit(name, args, brief)`.

        Reached by `dispatch()` for `run_command` (dangerous) and for the
        `ASKED_TOOLS` edits (`runtime.ask_tools`), which only an owner's desk
        turn holds (decision F-3, 2026-10-14). Everything is decided by the
        daemon's §6 permit, the one every provider is handed — so the gate is
        the same gate and the route is the same route:

        - layer 1 refuses protected state, the allowlist, credential and env
          files, SELF_PROTECTED code and the owner ops outright (dispatch's own
          `command_verdict` DENY and v1's tool-level refusals still run too);
        - layer 2's always-ask rules (prod deploys, pushes to protected
          branches, payments, credentials) ask whatever the allowlist says;
        - layer 4 honours the owner's allowlist and rules' ALLOW **only for a
          human-backed asker** — the daemon's broker (`PendingApprovals`) is;
          a `DenyAll` is not and never auto-approves;
        - layer 5 asks the owner through the broker: a HUD card (and the
          Discord DM when attached), answered by `POST /approvals/{id}`, which
          resolves the broker itself — never `answer()`. Unanswered, timed
          out, shut down or unreadable is a denial.

        **No `jarvis_human_backed` flag here, deliberately** (until
        2026-10-14 this was `permissions.gate(ask)`). With the flag,
        `dispatch()` auto-runs a rules-ALLOW command and `gate` an allowlisted
        one *before* the permit is asked — skipping the always-ask rules, and
        auto-running for a `DenyAll` asker too, since `gate` sets the flag
        whoever stands behind it. Without it every call reaches the permit,
        which applies the allowlist and rules' ALLOW itself at layer 4 with
        the asker's real `human_backed`, judged in the thread's folder. The
        jarvis-mcp bridge's approver omits the flag for the same reason.

        **AUTO means ASK here.** Under AUTO, layer 5 answers "the provider's
        own reviewer decides" — Claude's classifier, Codex's reviewer. The
        fast path has no reviewer, so that ALLOW would run an unlisted command
        with nobody asked. Its permit is asked with an ASK copy of the brief:
        what no layer allows, the owner decides.

        **Only at the desk, and only while the turn runs.** An edit tool's
        call is denied when the turn is no longer at the desk — before the
        permit is asked, and again after, since an approval can wait out a
        steer from elsewhere. And a yes that lands after the owner pressed
        Stop is a denial (review of PR #182, L1): the card is still answered
        or times out in the broker — the permit takes no cancel — but what it
        approved never runs.

        Built for every handle, desk or not, because `Agent` must never be
        constructed with `approve=None` — `dispatch()` runs a dangerous tool
        unguarded with no approver, which is the one forbidden mistake.
        """
        from ... import runtime

        asked = (replace(brief, profile=PermissionProfile.ASK)
                 if brief.profile == PermissionProfile.AUTO else brief)

        def approve(tool: Any, args: dict) -> bool:
            name = getattr(tool, "name", str(tool))
            if name in EDIT_TOOLS and not runtime.at_desk():
                return False
            try:
                decision = Decision(permit(name, dict(args), asked))
            except Exception:  # noqa: BLE001 — an unreadable answer is a denial
                return False
            return (decision is Decision.ALLOW
                    and (name not in EDIT_TOOLS or runtime.at_desk())
                    and not runtime.should_stop()())

        return approve

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

    def set_model(self, h: SessionHandle, model: str | None, effort: str | None) -> None:
        """Change the model and effort from the next turn on (decisions A1).

        Stored only: a turn in flight keeps every one of its calls on the
        model it started with, and `_run` applies the new pair before the
        next turn's first call. Costs nothing — the transcript is the
        conversation, and any model can read it.
        """
        native = h.native
        if native is None or native.closed:
            raise ValueError("session closed")
        native.model, native.effort = model, effort

    def interrupt(self, h: SessionHandle) -> None:
        """Cancel the turn in flight. v1 lands it at the next step boundary."""
        if h.native is not None:
            h.native.stop.set()

    # -- steering (2026-10-08)

    def steer(self, h: SessionHandle, message: UserMessage) -> None:
        """Hand `message` to the running turn: v1's loop appends it as an
        `[owner steering]` user message at its next step boundary — the only
        place a message can go without splitting a tool batch (invariant 3).
        Refused (the daemon queues it) when no turn is accepting, or when a
        named skill cannot be loaded, which its own turn reports properly."""
        native = h.native
        if native is None or native.closed:
            raise SteerRefused("session closed")
        from ..commands import SkillRefused

        try:
            text = _turn_text(message)
        except SkillRefused as exc:
            raise SteerRefused(str(exc)) from None
        with native.inbox_lock:
            if not native.accepting:
                raise SteerRefused("no turn is running")
            if not _at_desk(native.brief, message):
                native.desk["present"] = False          # the rest of this turn is off the desk
            native.inbox.append((message, text, list(message.images)))

    def undelivered(self, h: SessionHandle) -> list[UserMessage]:
        """Steered messages the last turn never took (its final answer came
        first, or it stopped). Emptied by the call."""
        native = h.native
        if native is None:
            return []
        with native.inbox_lock:
            out, native.leftover = native.leftover, []
        return [message for message, _text, _images in out]

    @classmethod
    def _take_steering(cls, native: _Native) -> list:
        """v1's `Agent.take_steering`: everything steered in since the last
        step, as `(text, images)`. Called on the turn's worker thread, at the
        step boundary, before the next request is built.

        A turn a steer took off the desk loses `DESK_TOOLS` and `EDIT_TOOLS`
        here, for the rest of the turn: the next request carries neither
        their schemas nor the edit wording, and the toolset dispatch enforces
        is re-bound (invariant 10; `_sync_tools` re-binds only on a group
        load, which the fast path never makes). The step budget the turn
        started with stands — the loop's range is fixed — so the read-only
        wording may state fewer steps than the loop allows."""
        with native.inbox_lock:
            taken, native.inbox = native.inbox, []
        if (native.desk.get("present") is not True
                and _DESK_ONLY & set(native.agent._base_tool_names)):
            from ... import runtime

            names = cls._turn_tools(native, False)
            runtime.bind(tool_names=set(names), ask_tools=frozenset())
        return [(text, images or None) for _message, text, images in taken]

    def answer(self, h: SessionHandle, req_id: str, decision: Decision | str) -> None:
        raise ValueError(
            "the fast path asks no questions, and its approvals are the broker's: "
            "an edit or command it asks about is answered on the approval card "
            "(POST /approvals/{id}), which resolves the daemon's PendingApprovals "
            "directly — there is nothing for answer() to deliver"
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
            # Not fatal: this is a second send refused, not the session
            # failing. A fatal error makes the daemon drop and close the
            # session — which here is the one whose turn is still running.
            # (The daemon never sends twice on one handle; `steer` is how a
            # message reaches a running turn.)
            yield Event(
                EventKind.ERROR,
                h.thread_id,
                {"message": "a turn is already running on this thread", "fatal": False},
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

        from ..commands import SkillRefused

        try:
            text = _turn_text(message)
        except SkillRefused as exc:
            yield Event(EventKind.TURN_STARTED, thread_id)
            yield Event(EventKind.ERROR, thread_id, {"message": str(exc), "fatal": False})
            yield Event(EventKind.TURN_FINISHED, thread_id, {"stop": "error"})
            return

        # A cancel aimed at the *previous* turn must not kill this one — the v1
        # face clears its event under the agent lock right before run_turn for
        # exactly this reason, and we hold `busy` here.
        native.stop.clear()

        # The proposal slot this turn writes into. Bound on the worker thread,
        # where `task_propose` will run.
        proposal: dict[str, Any] = {}

        # Which model answers is decided here, once per turn, never mid-turn:
        # a default handle follows the global picker; a pinned one stays put.
        native.agent.model = native.model or models.tier("orchestrator")
        # Only OpenRouter's own ladder goes on the wire: an effort it lacks
        # (Codex's `ultra`) is dropped here, whatever handed it over, and the
        # model's v1 default applies (PR #20 review).
        effort = native.effort
        if effort and effort not in models.EFFORT_LADDER:
            effort = None
        native.agent.effort = effort
        state = _TurnState(thread_id, events, native.agent.model, effort)
        native.agent.on_event = state.on_event
        # Is the owner at the desk for this turn? A fresh slot per turn, so a
        # steer that took the last turn off the desk does not linger.
        model_info = models.cached_info(native.agent.model)
        desk = {"present": _at_desk(native.brief, message),
                "images": bool(model_info is not None and model_info.vision)}
        native.desk = desk
        names = self._turn_tools(native, desk["present"])
        # Decision F-4: a turn holding the edit tools gets the larger budget.
        # Set per turn, before `run_turn` reads it; exhaustion still proposes.
        native.agent.max_steps = (FAST_EDIT_MAX_STEPS if EDIT_TOOLS <= set(names)
                                  else FAST_MAX_STEPS)
        # Edits reach the gate as commands do (decision F-3), on this turn only.
        asked_tools = ASKED_TOOLS & set(names)

        result: dict[str, Any] = {}

        # Who is calling (`runtime.caller()`, the peers plan's Phase 0): this
        # thread, its project, and the daemon's turn id — which the daemon
        # bound on the thread this generator runs on; read here, before the
        # worker (a fresh context) starts. A tool reads the caller from
        # there, never from an argument the model wrote.
        from ... import runtime

        bound = runtime.caller() or {}
        caller = {"thread_id": thread_id, "project_id": native.project_id,
                  "provider": ProviderName.FAST.value,
                  "turn_id": bound.get("turn_id") if bound.get("thread_id") == thread_id else None}

        from ..browser.drive_state import DriveTurn, turn_scope as drive_scope
        from ..browser.drive import retire_turn
        driving=DriveTurn(thread_id,caller["turn_id"] or "",
                          native.brief.profile is PermissionProfile.STRICT,
                          str(native.brief.cwd or ""),message.text)

        def work() -> None:
            previous_ending=getattr(native.agent,"exhaustion_message",None)
            try:
                runtime.bind(proposal=proposal, caller=caller, ask_tools=asked_tools)
                if native.workdir is not None:
                    runtime.bind(workdir=native.workdir)
                native.agent.exhaustion_message=_drive_ending(driving,native.agent.max_steps)
                with runtime.turn_scope(desk=desk), drive_scope(driving,retire_turn):
                    result["turn"] = native.agent.run_turn(
                        text, images=list(message.images) or None
                    )
            except BaseException as exc:  # noqa: BLE001 — reported, never swallowed
                result["error"] = exc
            finally:
                native.agent.exhaustion_message=previous_ending
                events.put(done)

        worker = threading.Thread(
            target=work, name=f"jarvis-fast-{thread_id}", daemon=True
        )
        with native.inbox_lock:
            native.accepting = True
        yield Event(EventKind.TURN_STARTED, thread_id)
        worker.start()
        try:
            while True:
                item = events.get()
                if item is done:
                    break
                yield item
            worker.join()
        finally:
            # Closed before the last event, which is where the daemon stops
            # reading: a steer that arrives from here on is refused (and
            # queued by the daemon), and one the loop never took — the final
            # answer came first — is handed back through `undelivered`.
            with native.inbox_lock:
                native.accepting = False
                native.leftover.extend(native.inbox)
                native.inbox = []

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
            {"stop": _stop_reason(turn), "proposal": _proposal(proposal, turn, driving=driving.attempted)},
        )


# The driving ending (`DriveTurn.exhaustion`, BR9b) says "eight steps": true
# of every turn until a desk turn held the edit tools and their larger budget
# (decision F-4). Said in the same words for whichever budget the turn had.
_STEP_WORDS = {FAST_MAX_STEPS: "eight", FAST_EDIT_MAX_STEPS: "thirty"}


def _drive_ending(driving, steps: int):
    """`driving.exhaustion`, stating this turn's own step budget."""
    def ending():
        text = driving.exhaustion()
        if text and steps != FAST_MAX_STEPS:
            text = text.replace("after eight steps",
                                f"after {_STEP_WORDS.get(steps, str(steps))} steps", 1)
        return text
    return ending


def _turn_text(message: UserMessage) -> str:
    """What the model is given for `message`.

    A named skill's body goes into the turn, which saves a `skill_read` step
    and cannot be skipped by the model. Same trust as `skill_read` — skill
    bodies are the owner's words. A skill that is unknown, jarvis-only or
    over the cap raises `SkillRefused`, never cut.
    """
    if not message.skill:
        return message.text
    from ..commands import skill_body

    body = skill_body(message.skill)
    request = message.text.strip() or "(no further request — run the skill)"
    return (f'[The owner invoked the skill "{message.skill}". Follow it.]\n{body}'
            f"\n\n[Request]\n{request}")


def _stop_reason(turn) -> str:
    if turn.cancelled:
        return "interrupted"
    if turn.stopped_early:
        return "max_turns"
    return "end"


def _proposal(slot: dict[str, Any], turn, *, driving: bool = False) -> dict[str, Any] | None:
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
    if turn.stopped_early and not driving:
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

    def __init__(self, thread_id: str, events: queue.Queue, model: str | None = None,
                 effort: str | None = None):
        self.thread_id = thread_id
        self.events = events
        self.model = model
        self.effort = effort
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
            {"call_id": call_id, "name": name, "args": {} if name in DRIVE_TOOLS else _args(raw)},
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
                # Preserve v1's ok contract; raw content cannot establish an
                # outcome, even when v1's broad prefix check says not ok.
                "ok": not text.startswith(_NOT_OK),
                "outcome": ("ok" if name in _RAW_CONTENT_TOOLS else
                            "denied" if text.startswith(_DENIAL_PREFIXES) else
                            "failed" if text.startswith("Error:") else "ok"),
                "summary": _summary(name, text),
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
                "provider_reported": {"turn_cost_usd": total, "model": self.model,
                                      "effort": self.effort},
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
