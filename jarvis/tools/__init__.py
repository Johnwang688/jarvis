"""Tool registry.

A tool is a plain Python function. The `@tool` decorator derives its JSON
schema from type hints, so there is no schema to keep in sync by hand:

    @tool
    def read_file(path: Annotated[str, "Path to read"]) -> str:
        '''Read a UTF-8 text file.'''

`Annotated[T, "description"]` supplies the per-parameter description that the
model reads when deciding how to call the tool. Parameters with defaults are
optional; everything else is required.
"""

from __future__ import annotations

import inspect
import json
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable

from .secrets import scrub

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class ToolResult:
    """What a tool hands back.

    A `tool` message in the OpenAI wire format can only hold a string, so a
    tool that produces an image returns it here and the agent loop attaches it
    as a separate user message. Tools that only produce text can just return a
    plain str.
    """

    text: str
    image_b64: str | None = None
    mime: str = "image/png"

    def __str__(self) -> str:
        return self.text


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    func: Callable[..., Any]
    dangerous: bool = False

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


REGISTRY: dict[str, Tool] = {}


@dataclass(frozen=True)
class ToolGroup:
    """A set of tools that is only worth its schema cost some of the time.

    Every tool in `REGISTRY` is sent to the provider on **every** request, and
    the registry is already ~12k tokens of JSON schema. That is the right trade
    for tools any turn might need; it is the wrong one for an integration that
    is idle in most conversations and absent entirely on a machine that never
    connected it.

    So a group is the two-tier design the skills index already uses here, moved
    down a level: a cheap always-visible pointer, and the bulk loaded on demand.

      * `available()` false — the owner never connected this service — and the
        group costs *nothing*: not the schemas, not the pointer. A clone with no
        Spotify account is byte-identical to one where these tools do not exist.
      * available, and `core` is offered every turn. Keep it to the one or two
        calls that answer the common ask outright, because a round trip spent
        expanding a toolset is a round trip the owner waits through — which on
        a voice surface is the whole latency budget.
      * `extra` appears only after `load_tools(name)`, and stays for the life of
        the agent.

    The cost of expanding is a one-time prefix-cache miss (tools are part of the
    cached prefix, the same reason invariant 7 moved the volatile block to the
    tail). Once per conversation that actually uses the group, against ~1.3k
    tokens on every request that does not — which is why `core` is small rather
    than empty.
    """

    name: str
    summary: str  # one line, rendered into the working-context block
    core: tuple[str, ...]
    extra: tuple[str, ...]
    available: Callable[[], bool]

    def all_names(self) -> tuple[str, ...]:
        return self.core + self.extra

    def is_available(self) -> bool:
        """Never raises: a broken availability check hides the group rather
        than breaking every agent construction in the process."""
        try:
            return bool(self.available())
        except Exception:
            return False


GROUPS: dict[str, ToolGroup] = {}


def register_group(
    name: str,
    summary: str,
    core: tuple[str, ...],
    extra: tuple[str, ...],
    available: Callable[[], bool],
) -> None:
    GROUPS[name] = ToolGroup(name, summary, tuple(core), tuple(extra), available)


def group_of(tool_name: str) -> ToolGroup | None:
    for group in GROUPS.values():
        if tool_name in group.all_names():
            return group
    return None


def default_names() -> list[str]:
    """The toolset an agent gets when it does not ask for a specific one.

    The whole registry, minus every group that is not available at all, minus
    the deferred half of the groups that are.
    """
    hidden: set[str] = set()
    available = False
    for group in GROUPS.values():
        if group.is_available():
            available = True
            hidden.update(group.extra)
        else:
            hidden.update(group.all_names())
    # With nothing to load, the loader is noise: a tool whose every answer is
    # "there are no groups" still costs its schema on every request, which is
    # the exact cost this whole mechanism exists to avoid.
    if not available:
        hidden.add("load_tools")
    return [name for name in REGISTRY if name not in hidden]


# How many of an agent's own tool names a refusal lists. Enough to correct a
# guessed or misremembered name; the schemas themselves are already in the
# request, so listing a hundred names would only re-send the registry.
REFUSAL_LIST_CAP = 40


def _names_listing(names) -> str:
    ordered = sorted(names)
    shown = ordered[:REFUSAL_LIST_CAP]
    text = ", ".join(shown) if shown else "none"
    if len(ordered) > len(shown):
        text += f", and {len(ordered) - len(shown)} more (their schemas are in this request)"
    return text


def _toolset_refusal(name: str) -> str | None:
    """Why a registered tool must not run for the agent that called it, or None.

    **An agent's toolset is enforced here, not only by what the request offers**
    (2026-10-10). The registry is global — every tool any module registered is
    in `REGISTRY` — and dispatch used to look a call up there and nowhere else,
    so a model that simply *named* a tool it was never offered reached it. A
    dangerous tool still met the approver; a non-dangerous one ran with nothing
    in the way. That made every toolset in this codebase advisory: the v2 fast
    path's "no tool can change anything" (a scripted turn wrote a file with
    `write_file`), a workflow's "no browser" (`browser_*` are not dangerous), a
    sub-agent's intersection with its parent, an attended task's "no spawning,
    no desktop", a goal's "no desktop", a bench's pinned set. Each was only true
    of the schemas *sent*.

    What an agent holds is `runtime.current_tools()`: bound by `Agent.run_turn`
    from the agent's tool specs at the top of every turn (and put back as it
    was when the turn ends, so "bound" means "inside a turn"), re-bound by
    `Agent._sync_tools` when `load_tools` expands it, carried into each parallel
    worker by `_dispatch_calls`' per-worker `copy_context()`, and bound by
    jarvis-mcp's worker to the list it exposes. A tool an agent was handed
    explicitly (one never in `default_names()`) is in that set like any other.

    Two refusals, both text (invariant 4), both before the arguments are read:

      * a tool of a deferred group this agent **could** load — it holds
        `load_tools` and the group's core — gets the pointer to `load_tools`,
        because a model that guessed the name guessed the arguments too;
      * anything else gets "not available to this agent", with the agent's own
        tool names (capped). No pointer to `load_tools` for an agent that
        cannot call it or may not widen into that group.

    **Unbound fails open**, and that is a decision, not an oversight: nothing
    bound means no agent is calling — a test or a script calling `dispatch`
    directly — so there is no toolset to enforce. It is safe only because no
    real path dispatches unbound: the only callers of `dispatch` under
    `jarvis/` are `Agent._dispatch_one` (reached only from `_run_turn`, after
    its bind) and `v2/mcp.call_tool` (which binds its exposed list first), and
    `tests/dispatch_toolset_check.py` asserts both halves — that set of call
    sites, and a binding at every dispatch on every surface it drives. A new
    caller of `dispatch` must bind a toolset first, or it runs unconfined.
    """
    from .. import runtime

    held = runtime.current_tools()
    if held is None or name in held:
        return None
    group = group_of(name)
    if group is not None and "load_tools" in held and group in loadable(held):
        return (
            f"Error: {name} belongs to the '{group.name}' tool group, which is not "
            f"loaded in this conversation. Call load_tools('{group.name}') first — "
            "its schema comes with it, so calling it now would be guesswork."
        )
    return (
        f"Error: {name} is not available to this agent, so it was not run. "
        "Use the tools you were given, or say what you need and let the user "
        f"decide. Your tools: {_names_listing(held)}."
    )


def loadable(names: set[str] | frozenset[str]) -> list[ToolGroup]:
    """Groups an agent holding `names` may still expand.

    Gated on the agent already having the group's core tools, for the same
    reason a sub-agent's toolset is intersected with its parent's: an agent
    that was handed an explicit toolset must not be able to widen it by asking.
    """
    return [
        group
        for group in GROUPS.values()
        if group.is_available()
        and group.core
        and set(group.core) <= set(names)
        and not set(group.extra) <= set(names)
    ]

# Tools the agent loop may run **concurrently** with each other.
#
# An allowlist, not a denylist, and the same reasoning as DESKTOP_APPS: the
# question "is this safe to run beside a copy of itself" has to be answered
# once per tool by a person, because the failure mode of guessing wrong is two
# tools quietly corrupting each other's work rather than an error.
#
# Everything here is read-only and owns no shared handle. What is deliberately
# absent, and why:
#
#   browser_*      one Playwright page. Two concurrent gotos interleave into
#                  nonsense even though `_submit()` serialises them mechanically.
#   desktop_*      driving an app means holding the Windows foreground; two at
#                  once fight over it, and over the owner's keyboard.
#   run_subagent   a child may take the browser, and children would then overlap
#                  on it — the exact caveat CLAUDE.md flags as the first thing
#                  to break if sub-agents ever became concurrent.
#   run_readonly   a subprocess is only as read-only as its binary: two `git`
#                  invocations in one repo can collide on index.lock. grep_files
#                  covers the case this would have been used for.
#   write_file / edit_file / *_write / plan_write
#                  writes, and order between them is meaning.
#
# Anything `dangerous` is refused by `parallelizable()` whatever this set says:
# two approval prompts racing would ask the owner to answer a question while
# another one is still on screen.
PARALLEL_SAFE = frozenset(
    {
        "read_file", "list_dir", "find_files", "grep_files", "get_datetime",
        "memory_list", "memory_read", "memory_search",
        "skill_list", "skill_read",
        "session_list", "session_read", "session_search", "session_summary",
        "web_search", "fetch_page", "query_sqlite",
        "gmail_search", "gmail_read", "drive_search", "drive_read",
        "discord_channels", "discord_read",
        "cad_status", "cad_find_part", "cad_assembly", "cad_render",
        # Read-only Spotify views. The playback controls are deliberately
        # absent: two concurrent writes to one player are a race whose loser
        # is silent — "play X" and "volume 40" landing out of order is a
        # different song at the wrong volume, with no error anywhere.
        "spotify_status", "spotify_search", "spotify_playlists",
        "spotify_library", "spotify_artist_tracks",
        "workflow_status", "workflow_log", "task_status", "task_log",
        "avatar_list",
    }
)


def parallelizable(name: str) -> bool:
    """True if `name` may run alongside other tools in the same assistant turn."""
    entry = REGISTRY.get(name)
    return entry is not None and not entry.dangerous and name in PARALLEL_SAFE


def _json_type(annotation: Any) -> dict[str, Any]:
    origin = typing.get_origin(annotation)

    if origin is typing.Annotated:
        base, *meta = typing.get_args(annotation)
        node = _json_type(base)
        for item in meta:
            if isinstance(item, str):
                node["description"] = item
        return node

    # Optional[X] / X | None -> schema of X
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
        return {}

    if origin in (list, set, tuple):
        args = typing.get_args(annotation)
        node: dict[str, Any] = {"type": "array"}
        if args:
            node["items"] = _json_type(args[0])
        return node

    if origin is dict:
        return {"type": "object"}

    if isinstance(annotation, type) and issubclass(annotation, bool):
        return {"type": "boolean"}

    return {"type": _PY_TO_JSON.get(annotation, "string")}


def tool(func: Callable[..., Any] | None = None, *, dangerous: bool = False):
    """Register a function as a tool. `dangerous=True` requires user approval."""

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        hints = typing.get_type_hints(fn, include_extras=True)
        signature = inspect.signature(fn)

        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, param in signature.parameters.items():
            properties[name] = _json_type(hints.get(name, str))
            if param.default is inspect.Parameter.empty:
                required.append(name)

        doc = inspect.getdoc(fn) or ""
        REGISTRY[fn.__name__] = Tool(
            name=fn.__name__,
            description=doc.strip(),
            schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            func=fn,
            dangerous=dangerous,
        )
        return fn

    return wrap(func) if func else wrap


def specs(names: list[str] | None = None) -> list[dict[str, Any]]:
    tools = REGISTRY.values() if names is None else [REGISTRY[n] for n in names]
    return [t.spec() for t in tools]


def dispatch(
    name: str, raw_arguments: str, approve: Callable[[Tool, dict], bool] | None = None
) -> ToolResult:
    """Execute a tool call and return its result.

    Failures come back as text rather than raised exceptions: the model sees
    them as a tool result and gets a chance to correct itself, which is the
    whole point of an agent loop.

    Every result is scrubbed of `.env` content on the way out. This is the last
    point before a string becomes a `tool` message, so it is the only place
    that catches a leak from a tool that never names the file — a recursive
    grep, a fetched page, a browser snapshot. See tools/secrets.py.
    """
    result = _dispatch(name, raw_arguments, approve)
    result.text = scrub(result.text)
    return result


def _dispatch(
    name: str, raw_arguments: str, approve: Callable[[Tool, dict], bool] | None = None
) -> ToolResult:
    entry = REGISTRY.get(name)
    if entry is None:
        from .. import runtime

        held = runtime.current_tools()
        if held is not None:
            return ToolResult(f"Error: no tool named {name!r}. Available: {_names_listing(held)}")
        visible = sorted(default_names())
        return ToolResult(f"Error: no tool named {name!r}. Available: {', '.join(visible)}")

    # The calling agent's toolset, checked before anything about the call is
    # read: a tool this agent does not hold must not get as far as parsing its
    # arguments, let alone the approver. See _toolset_refusal.
    refused = _toolset_refusal(name)
    if refused is not None:
        return ToolResult(refused)

    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        return ToolResult(
            f"Error: arguments were not valid JSON ({exc}). Received: {raw_arguments[:200]}"
        )

    if not isinstance(arguments, dict):
        return ToolResult(f"Error: arguments must be a JSON object, got {type(arguments).__name__}")

    missing = [k for k in entry.schema["required"] if k not in arguments]
    if missing:
        return ToolResult(f"Error: missing required argument(s): {', '.join(missing)}")

    if entry.dangerous and approve is not None:
        # Three verdicts, decided here rather than inside the approver so the
        # fetch-execute review runs exactly once (it costs a network round trip
        # and a model call). See jarvis/rules.py.
        from .. import permissions

        verdict = permissions.command_verdict(entry.name, arguments)
        if verdict.decision == "deny":
            # Not approvable, by anyone — the same shape as the .env refusal.
            # Approving a command is not consent to what it does, so some
            # things must not reach the owner as a yes/no question at all.
            return ToolResult(
                f"Refused: {verdict.reason}. This is blocked outright, not "
                "pending approval — do not look for another way to run it. "
                "Say what you were trying to achieve and let the user decide."
            )
        # An ALLOW is only honoured for an approver with a person behind it.
        # A background workflow's deny-all approver must keep denying: nobody
        # is watching it, which is the entire reason it has one.
        auto = verdict.decision == "allow" and getattr(approve, "jarvis_human_backed", False)
        if not auto and not approve(entry, arguments):
            return ToolResult("The user declined to run this. Ask what they would like instead.")

    try:
        result = entry.func(**arguments)
    except TypeError as exc:
        return ToolResult(f"Error: bad arguments for {name}: {exc}")
    except Exception as exc:  # surfaced to the model, not the user
        return ToolResult(f"Error: {type(exc).__name__}: {exc}")

    if isinstance(result, ToolResult):
        return result
    if isinstance(result, str):
        return ToolResult(result)
    return ToolResult(json.dumps(result, default=str, indent=2))


from . import (  # noqa: E402,F401  (registers the tools)
    browsing,
    clock,
    contextctl,
    desktop,
    files,
    avatarctl,
    gmail,
    gitctl,
    goalctl,
    drive,
    google_workspace,
    memory,
    onshape,
    plan,
    search,
    sessions,
    shell,
    skills,
    spotify,
    toolgroups,
    sqlite,
    subagent,
    discord,
    tasks,
    voicectl,
    web,
    whiteboardctl,
    workflows,
)
