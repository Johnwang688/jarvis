"""Reading past conversations.

Every tool here is read-only, and none of them switches the conversation
Jarvis is currently in: which session is live is the owner's choice, made in
the HUD or on the command line. What Jarvis can do is *look* — see that a
past conversation exists (the recent-sessions index is in his context every
turn), pull its summary in when the owner refers back to it, and grep the lot.

The summary tool is the interesting one: its result lands in the transcript
as a tool message, which is exactly what "inject that session's context"
means in a chat loop — no special plumbing, just a tool that returns text.
"""

from __future__ import annotations

from typing import Annotated

from .. import sessions as store
from . import tool


@tool
def session_list(
    limit: Annotated[int, "How many recent sessions to show (default 10)"] = 10,
) -> str:
    """List recent conversations: id, title, when, and how many turns.

    Your context already names the most recent few. Use this when the owner
    asks about something older than that, or to find the id for
    session_summary / session_read.
    """
    return store.listing(max(1, min(limit, 50)))


@tool
def session_summary(
    session_id: Annotated[str, "Session id from the index or session_list"],
) -> str:
    """Pull a past conversation's summary into this one.

    Use when the owner refers back to earlier work ("what did we decide about
    the CAD sandbox?", "pick up where we left off yesterday") and the answer
    is not already in this conversation. The summary is generated once and
    cached, so asking again is free until that session gains new turns.
    """
    session = store.load(session_id.strip())
    if session is None:
        return f"Error: no session {session_id!r}. Check session_list."
    return (
        f"Summary of session {session.id} · {session.title!r} · "
        f"{store.when(session.meta.get('updated', 0))} · {session.turns} turn(s)\n\n"
        + session.summary()
    )


@tool
def session_search(
    query: Annotated[str, "Word or phrase to look for across saved conversations"],
    limit: Annotated[int, "Maximum matching lines to return (default 20)"] = 20,
    session_id: Annotated[str, "Search only this session (default: all)"] = "",
) -> str:
    """Search everything the owner and you have said in past conversations.

    Matches whole logged messages, so a hit shows the line in context with its
    session and time. Follow up with session_read or session_summary when a
    hit looks relevant.
    """
    return store.search(query, limit=max(1, min(limit, 100)), session_id=session_id.strip())


@tool
def session_read(
    session_id: Annotated[str, "Session id from the index or session_list"],
    last: Annotated[int, "How many of the most recent messages to read (default 30)"] = 30,
) -> str:
    """Read the tail of a past conversation verbatim.

    Use after session_search when a summary is too lossy and you need the
    exact wording of what was said.
    """
    session = store.load(session_id.strip())
    if session is None:
        return f"Error: no session {session_id!r}. Check session_list."
    lines = session.log_lines()[-max(1, min(last, 200)):]
    if not lines:
        return f"Session {session.id} has no recorded turns."
    body = "\n".join(f"{item.get('role', '?')}: {item.get('text', '')}" for item in lines)
    return f"Session {session.id} · {session.title!r} (last {len(lines)} message(s))\n\n{body}"
