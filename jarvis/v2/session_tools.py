"""The Jarvis tools a Claude or Codex chat session may call through jarvis-mcp.

The daemon's per-session list (plan `docs/plans/2026-10-10-terminal-read-mcp-
plan.md` §2, decisions D-3). A tool named here is offered to an owner's chat
thread on the providers it names, and only there: task workers, reviewers,
orchestrators and sub-agents never get a jarvis server or a token (§1.2). A
listed tool runs **in the daemon**, through the bridge's single
`tools.dispatch` (`bridge.py`), never in the jarvis-mcp process the CLI starts.

**It ships empty.** WP-1 is plumbing with no behaviour change: with nothing
here, `for_provider` is empty for every provider, so the daemon mints no token
and no session gets a jarvis server. WP-2 adds `terminal_read` (and the D-3
tools); a test registers its own entries with `register` and removes them.

The flags, each read by exactly one place:

- `read_only` — the tool changes nothing. Claude's `PreToolUse` hook returns
  an explicit allow for it and `permissions.build_permit`'s `jarvis-tool`
  layer ALLOWs it without asking anyone (an always-ask rule naming it still
  asks); Codex's config marks it `approval_mode = "approve"` and the relay's
  listing marks it `readOnlyHint`.
- `foreground_only` — the conversation itself may call it, a sub-agent may
  not. Claude's hook refuses it when the hook input carries `agent_id`,
  without asking `permit`.
- `desk_only` — usable only on a turn the owner typed at the HUD (WP-2's desk
  rule). Listed always and refused cleanly off the desk by the tool (D-4).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import ProviderName

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


# Empty in production (WP-1). Keyed by registry name.
SESSION_TOOLS: dict[str, SessionTool] = {}


def register(entry: SessionTool) -> SessionTool:
    """Offer `entry` to chat sessions. For WP-2 and for tests."""
    if not isinstance(entry, SessionTool) or not entry.name:
        raise ValueError("a session tool needs a name")
    bad = set(entry.providers) - {ProviderName.CLAUDE.value, ProviderName.CODEX.value}
    if bad:
        raise ValueError("session tools are for Claude and Codex chats only, not "
                         + ", ".join(sorted(bad)))
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
    offered = []
    for entry in sorted(entries, key=lambda e: e.name):
        if entry.name not in tools.REGISTRY:
            continue
        group = tools.group_of(entry.name)
        if group is not None and not group.is_available():
            continue
        offered.append(entry)
    return offered


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
