"""Memory-only, exact-tab restore descriptors for a committed sound change.

Private never calls this module. URLs stay off status/events/logs. An existing
lease is copied only from the exact accepted old tab, never pending navigation.
"""
from __future__ import annotations
from dataclasses import dataclass
from . import policy

BLANK = 'about:blank'

@dataclass(frozen=True, eq=False)
class Restore:
    tab_id: str
    profile: str
    old_page: int
    url: str
    port: int | None
    opener: str | None
    blanked: bool


def describe(tab, *, refused, owns_port, local_servers: bool) -> Restore:
    if tab.profile == 'private':
        raise ValueError('Private pages are never collected for restart')
    url = BLANK
    blanked = True
    if (tab.pane_ready and not tab.signin and not tab.loading
            and tab.pending_port is None and tab.location is not None
            and tab.location[0] == tab.page.url):
        try:
            judged = policy.judge_url(tab.location[0], refused=refused)
            if judged == tab.location[0]:
                url, blanked = judged, False
        except policy.URLRefused:
            pass
    port = policy.loopback_port(url)
    lease = port if port is not None and tab.loopback_port == port and owns_port(port, tab.id) else None
    if port is not None and lease is None and not local_servers:
        url, blanked = BLANK, True
    return Restore(tab.id, tab.profile, tab.page_identity, url, lease, tab.opener, blanked)


def target(descriptor: Restore, *, refused, local_servers: bool) -> tuple[str, int | None, bool]:
    """Rejudge at actual replay, never owner=True and never a pending grant."""
    try:
        url = policy.judge_url(descriptor.url, refused=refused)
        if url != descriptor.url:
            return BLANK, None, True
    except policy.URLRefused:
        return BLANK, None, True
    port = policy.loopback_port(url)
    lease = descriptor.port if port is not None and descriptor.port == port else None
    if port is not None and lease is None and not local_servers:
        return BLANK, None, True
    return url, lease, descriptor.blanked
