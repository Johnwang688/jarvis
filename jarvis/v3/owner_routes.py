"""The v3 HUD's exact routes and browser owner identity rule."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from email import errors as header_errors
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING

from jarvis import config

if TYPE_CHECKING:
    from .feed import Feed


MAX_BODY = 64 * 1024
FEED_LOCK_TIMEOUT = 2.0
_MALFORMED_HEADERS = (
    header_errors.FirstHeaderLineIsContinuationDefect,
    header_errors.InvalidHeaderDefect,
    header_errors.MisplacedEnvelopeHeaderDefect,
    header_errors.MissingHeaderBodySeparatorDefect,
)


@dataclass(frozen=True)
class Route:
    handler: Callable[..., tuple[int, object]]
    owner: bool = field(default=True, kw_only=True)
    with_feed: bool = field(default=False, kw_only=True)

    def run(self, body: dict | None, get_feed: Callable[[], Feed]) -> tuple[int, object]:
        if not self.with_feed:
            return self.handler(body)
        from .feed import BriefNotFound, FeedBusy, FeedError

        try:
            return self.handler(body, get_feed())
        except FeedBusy:
            return 503, {"error": "Feed is busy."}
        except BriefNotFound:
            return 404, {"error": "Brief not found."}
        except FeedError:
            return 400, {"error": "Feed operation refused."}


def _feed_snapshot(body, feed):
    return 200, feed.snapshot()


def _feed_answer(body, feed):
    """Answer an existing brief. The lookup may create the empty private feed folder."""
    if (not {"id", "action"} <= body.keys() <= {"id", "action", "text"}
            or not all(isinstance(value, str) for value in body.values())):
        return 400, {"error": "Expected a brief ID, an action and optional answer text."}
    bid, action = body["id"], body["action"]
    brief = feed.answer(bid, action, text=body.get("text"), return_brief=True)
    result = {"brief": brief}
    if action == "approve" and brief["proposal_id"] is not None:
        result["executed"] = False
    return 200, result


def _feed_feedback(body, feed):
    if body.keys() != {"id", "tag"} or not all(isinstance(value, str) for value in body.values()):
        return 400, {"error": "Expected a brief ID and a feedback tag."}
    return 200, {"brief": feed.feedback(body["id"], body["tag"], return_brief=True)}


ROUTES: dict[tuple[str, str], Route] = {
    ("GET", "/v3/owner/whoami"): Route(lambda body: (200, {"owner": True})),
    ("POST", "/v3/owner/ping"): Route(lambda body: (200, {"ok": True})),
    ("GET", "/v3/owner/feed"): Route(_feed_snapshot, with_feed=True),
    ("POST", "/v3/owner/feed/answer"): Route(_feed_answer, with_feed=True),
    ("POST", "/v3/owner/feed/feedback"): Route(_feed_feedback, with_feed=True),
}
PUBLIC = frozenset(key for key, route in ROUTES.items() if not route.owner)


def is_owner(handler: BaseHTTPRequestHandler) -> bool:
    """Recognize the HUD page on the v3 loopback listener.

    The strict Host check refuses rebound names. Same-origin GET and HEAD
    often omit Origin, so a plain read can use browser fetch metadata instead.
    Page scripts cannot set that header. Programs on the owner's machine can
    supply these headers themselves.
    """
    port = handler.server.server_port
    if handler.server.server_address != ("127.0.0.1", port):
        return False
    # The email parser also records MIME body defects based on Content-Type.
    # Only consider defects that can hide or misinterpret HTTP headers here.
    if any(isinstance(defect, _MALFORMED_HEADERS) for defect in handler.headers.defects):
        return False
    if handler.folded_headers & {b"host", b"origin", b"sec-fetch-site"}:
        return False
    hosts = handler.headers.get_all("Host", [])
    if len(hosts) != 1 or not config.own_host_header(hosts[0], port):
        return False

    read = handler.command in ("GET", "HEAD")
    sites = handler.headers.get_all("Sec-Fetch-Site", [])
    if len(sites) > 1 or sites == ["cross-site"]:
        return False
    origins = handler.headers.get_all("Origin", [])
    if origins:
        return origins == [f"http://{hosts[0]}"] and (read or sites in ([], ["same-origin"]))
    return read and sites == ["same-origin"] and "Upgrade" not in handler.headers
