"""Owner Browser pane snapshots shared by the HUD registry and BR5.

This module's public values are immutable and memory-only. They are not event
payloads, log fields or persisted browsing history. In particular, URLs and
source identities never go onto the bus; browser_viewed uses only view_id.
The registry implementation resolves a displayed slot, never a raw tab ID,
and revalidates generation plus host document identity around a capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ResolvedSource:
    """An immutable identity resolved under the dev-server registry lock.

    An unknown/legacy source is a refusal, not an owner-entered URL. A terminal
    source additionally requires current terminal readability at capture time;
    a display label or a client-supplied terminal ID never grants that access.
    """

    kind: Literal["owner", "terminal", "thread", "unknown"]
    entry_id: str | None = None
    terminal_id: str | None = None
    thread_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class PaneSnapshot:
    """The complete identity BR5 must revalidate before and after a look.

    page_identity and document_generation are None only for Direct. Remote
    navigation invalidates document_generation even on a same-URL reload and
    before the HUD sends another report. view_id is the daemon tab ID for
    Remote, or a host-issued opaque binding ID for Direct; it is never the
    temporary hidden-load ID or a local six-character tab ID.
    """

    window: str
    pane_id: int
    slot: int
    local_tab_id: str
    mode: Literal["direct", "remote"]
    tab_id: str | None
    view_id: str
    profile: str
    ctx: str | None
    url: str = field(repr=False)
    see: bool
    drive: bool
    drawn: bool
    covered: bool
    generation: int
    page_identity: int | None
    document_generation: int | None
    source: ResolvedSource


class PaneRefused(Exception):
    """Fixed safe refusal text, with no URL, title or source label."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
