"""The shared, owner-approved D-3 tool catalog. No dispatch or credentials.

Read-only is explicit: nondangerous reversible writes remain writes in MCP
annotations. External MCP and per-session MCP import this same catalog.
"""
MCP_TOOLS = frozenset({
    "memory_list", "memory_read", "memory_search", "memory_write", "memory_delete",
    "session_list", "session_read", "session_search", "session_summary",
    "get_datetime", "gmail_search", "gmail_read", "gmail_send", "discord_dm_owner",
    "spotify_play", "spotify_status", "spotify_search", "spotify_pause",
    "cad_status", "cad_find_part",
    "schedule_create", "schedule_list", "schedule_delete",
})
MCP_READ_ONLY = frozenset({
    "memory_list", "memory_read", "memory_search",
    "session_list", "session_read", "session_search", "session_summary",
    "get_datetime", "gmail_search", "gmail_read", "spotify_status", "spotify_search",
    "cad_status", "cad_find_part", "schedule_list",
})
assert MCP_READ_ONLY <= MCP_TOOLS
