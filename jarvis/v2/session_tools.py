"""The Jarvis tools a Claude or Codex chat session may call through jarvis-mcp.

The daemon's per-session list (plan `docs/plans/2026-10-10-terminal-read-mcp-
plan.md` §2, decisions D-3). A tool named here is offered to an owner's chat
thread on the providers it names. Task workers, reviewers and orchestrators
receive no session server or token (§1.2). Claude CLI subagents may use the
parent's D-3 tools; the foreground hook refuses terminal_read for them. A
listed tool runs **in the daemon**, through the bridge's single
`tools.dispatch` (`bridge.py`), never in the jarvis-mcp process the CLI starts.

Owner Claude and Codex chats receive `terminal_read`, the D-3 catalog,
the chat reads and `chat_send`/`chat_wait` (`tools/chats.py`), filtered by
installed tool availability.
Codex foreground-only calls are bound to the exact native parent handle/turn
before daemon dispatch. Tests may register temporary entries without widening
the production catalog.

The flags, each read by exactly one place:

- `read_only` — the tool changes nothing. Claude's `PreToolUse` hook returns
  an explicit delegation allow for it and `permissions.build_permit`'s
  `jarvis-tool` layer ALLOWs it without asking anyone (an always-ask rule
  naming it still asks); the relay listing marks it `readOnlyHint`. Codex delegates all exact
  offered entries to the daemon via native `approval_mode = "approve"`;
  this flag never mislabels a write or bypasses the daemon gate.
- `foreground_only` — the conversation itself may call it, a sub-agent may
  not. Claude's hook refuses it when the hook input carries `agent_id`,
  without asking `permit`.
- `owner_gated` — native permission delegates to the daemon Browser gate;
  it never approves execution or grants an allowlist.
- `desk_only` — usable only on a turn the owner typed at the HUD (WP-2's desk
  rule). Listed always and refused cleanly off the desk by the tool (D-4).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import ProviderName, Role
from .mcp_catalog import MCP_TOOLS, MCP_READ_ONLY

# What a Claude tool name looks like for a tool of the jarvis server:
# `mcp__<server>__<tool>`.
CLAUDE_PREFIX = "mcp__jarvis__"


@dataclass(frozen=True)
class SessionTool:
    name: str                                   # the registry name (`tools.REGISTRY`)
    providers: frozenset = field(default_factory=lambda: frozenset(
        {ProviderName.CLAUDE.value, ProviderName.CODEX.value}))
    read_only: bool = False
    foreground_only: bool = False
    desk_only: bool = False
    owner_gated: bool = False

    def __post_init__(self):
        if self.owner_gated and (self.read_only or self.foreground_only is not True
                                 or self.desk_only is not True):
            raise ValueError("an owner-gated action must be foreground-only, desk-only and not read-only")


# Both owner-chat providers share the approved catalog.
_CHAT_PROVIDERS = frozenset({ProviderName.CLAUDE.value, ProviderName.CODEX.value})
SESSION_TOOLS: dict[str, SessionTool] = {
    name: SessionTool(name, providers=_CHAT_PROVIDERS, read_only=name in MCP_READ_ONLY)
    for name in MCP_TOOLS
}
for _name in ("terminal_read", "hud_browser_view"):
    SESSION_TOOLS[_name] = SessionTool(
        _name, providers=_CHAT_PROVIDERS, read_only=True, foreground_only=True, desk_only=True)

SESSION_TOOLS["hud_browser_act"] = SessionTool(
    "hud_browser_act", providers=_CHAT_PROVIDERS, foreground_only=True,
    desk_only=True, owner_gated=True)

# Push and pull requests for Codex chats (owner decision CG-0, 2026-10-15;
# `chat_git.py`): Codex has no GitHub login, so the daemon runs git and gh
# after the owner's yes. Codex only (Claude pushes natively with the owner's
# own login), foreground only (a sub-agent cannot push), not desk-only (the
# owner's card decides, from any surface). External MCP never gets them.
_CODEX_ONLY = frozenset({ProviderName.CODEX.value})
CHAT_GIT_TOOLS = ("chat_git_push", "chat_pr_create", "chat_pr_merge", "chat_pr_close",
                  "chat_git_fetch")
for _name in CHAT_GIT_TOOLS:
    SESSION_TOOLS[_name] = SessionTool(_name, providers=_CODEX_ONLY, foreground_only=True)


# The chat reads (chat-to-chat plan C1, §4.2; decision CC-1): every owner
# Claude and Codex chat may read every other open HUD chat. Read-only — they
# change nothing — so the hook allows them and the listing says readOnlyHint;
# neither foreground- nor desk-only, as `session_*` are not. External MCP
# never gets them (not in MCP_TOOLS).
CHAT_READ_TOOLS = ("chat_list", "chat_summary", "chat_search", "chat_read")
for _name in CHAT_READ_TOOLS:
    SESSION_TOOLS[_name] = SessionTool(_name, providers=_CHAT_PROVIDERS, read_only=True)

# Messaging another chat (chat-to-chat plan C4, §4.2; decisions CC-2, CC-3,
# CC-6). Neither is read-only: the hook's explicit allow and the
# `jarvis-tool` layer never cover them, so `permit` decides (the
# `jarvis-session-delegate` layer: allowed unasked, refused for STRICT, and an
# always-ask rule naming one asks). Both foreground-only: a Claude sub-agent
# is refused by the hook before `permit`, a Codex child by `admit`.
# `chat_send` is desk-only too (a turn the owner started at the HUD in this
# chat); `chat_wait` is not, as a turn may wait on its own message from any
# surface. The bridge's `request_alive` check covers `chat_wait`, so a relay
# socket that closes ends the wait. External MCP never gets them.
CHAT_SEND_TOOLS = ("chat_send", "chat_wait")
SESSION_TOOLS["chat_send"] = SessionTool(
    "chat_send", providers=_CHAT_PROVIDERS, foreground_only=True, desk_only=True)
SESSION_TOOLS["chat_wait"] = SessionTool(
    "chat_wait", providers=_CHAT_PROVIDERS, foreground_only=True)


def register(entry: SessionTool) -> SessionTool:
    """Offer `entry` to chat sessions. Primarily for isolated test probes."""
    if not isinstance(entry, SessionTool) or not entry.name:
        raise ValueError("a session tool needs a name")
    bad = set(entry.providers) - {ProviderName.CLAUDE.value, ProviderName.CODEX.value}
    if bad:
        raise ValueError("session tools are for Claude and Codex chats only, not "
                         + ", ".join(sorted(bad)))
    if entry.owner_gated:
        from jarvis import tools
        implementation = tools.REGISTRY.get(entry.name)
        if implementation is None or implementation.dangerous:
            raise ValueError("an owner-gated action needs a registered nondangerous implementation")
    SESSION_TOOLS[entry.name] = entry
    return entry


def unregister(name: str) -> None:
    SESSION_TOOLS.pop(name, None)


def _provider_name(provider) -> str:
    return provider.value if isinstance(provider, ProviderName) else str(provider)


def for_provider(provider) -> list[SessionTool]:
    """What a chat on `provider` is offered, sorted by name. A tool the
    registry does not hold, or whose tool group is not available on this
    machine, is left out — an offer the bridge could only refuse is noise."""
    wanted = _provider_name(provider)
    if wanted not in (ProviderName.CLAUDE.value, ProviderName.CODEX.value):
        return []
    entries = [entry for entry in SESSION_TOOLS.values() if wanted in entry.providers]
    if not entries:
        return []
    from jarvis import tools
    from .tools import terminal_read, hud_browser_view, hud_browser_act, schedules, chat_git_tools, chats  # noqa: F401 — register implementations
    offered = []
    for entry in sorted(entries, key=lambda e: e.name):
        if entry.name not in tools.REGISTRY:
            continue
        if entry.owner_gated and (entry.read_only or not entry.foreground_only
                                  or not entry.desk_only or tools.REGISTRY[entry.name].dangerous):
            continue
        group = tools.group_of(entry.name)
        if group is not None and not group.is_available():
            continue
        offered.append(entry)
    return offered


def private_result(name: str) -> bool:
    """Whether a jarvis-server tool's result stays out of every summary: its
    canonical name is in `tools.PRIVATE_RESULT` (the terminal read, the
    Browser look and act, the chat reads, chat_wait). The modules that register those
    names are imported first, so the answer never depends on what this
    process happened to import already."""
    from jarvis import tools
    from .tools import terminal_read, hud_browser_view, hud_browser_act, chats  # noqa: F401
    return isinstance(name, str) and name in tools.PRIVATE_RESULT


def offered(provider, name: str) -> SessionTool | None:
    """The entry for `name` if a chat on `provider` is offered it."""
    return next((entry for entry in for_provider(provider) if entry.name == name), None)


def claude_tool(name: str) -> SessionTool | None:
    """The session tool a Claude tool name (`mcp__jarvis__<tool>`) is, or None."""
    if not isinstance(name, str) or not name.startswith(CLAUDE_PREFIX):
        return None
    return offered(ProviderName.CLAUDE, name[len(CLAUDE_PREFIX):])


def read_only_claude_names() -> frozenset[str]:
    """`mcp__jarvis__<tool>` for every read-only tool a Claude chat is offered:
    the names `build_permit`'s `jarvis-tool` layer ALLOWs."""
    return frozenset(CLAUDE_PREFIX + entry.name
                     for entry in for_provider(ProviderName.CLAUDE) if entry.read_only)


def claude_session_tool(brief, name: str) -> SessionTool | None:
    """An exact offered tool on this owner's session server, never a worker's."""
    if (brief is None or brief.role != Role.CHAT or brief.task_id is not None
            or "jarvis" not in brief.mcp_servers):
        return None
    return claude_tool(name)


def codex_session_tool(brief, name: str) -> SessionTool | None:
    """Canonical name on the daemon-provided Codex owner-chat server only.

    The caller must also check provider=Codex; Brief deliberately has no
    provider field. Incoming user briefs cannot supply the jarvis server.
    """
    if (brief is None or brief.role != Role.CHAT or brief.task_id is not None
            or "jarvis" not in brief.mcp_servers or not isinstance(name, str)):
        return None
    return offered(ProviderName.CODEX, name)
