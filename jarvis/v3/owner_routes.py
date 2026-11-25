"""The v3 HUD's exact routes and browser owner identity rule."""

from collections.abc import Callable
from dataclasses import dataclass, field
from email import errors as header_errors
from http.server import BaseHTTPRequestHandler

from jarvis import config


MAX_BODY = 64 * 1024
_MALFORMED_HEADERS = (
    header_errors.FirstHeaderLineIsContinuationDefect,
    header_errors.InvalidHeaderDefect,
    header_errors.MisplacedEnvelopeHeaderDefect,
    header_errors.MissingHeaderBodySeparatorDefect,
)


@dataclass(frozen=True)
class Route:
    handler: Callable[[dict | None], tuple[int, object]]
    owner: bool = field(default=True, kw_only=True)


ROUTES: dict[tuple[str, str], Route] = {
    ("GET", "/v3/owner/whoami"): Route(lambda body: (200, {"owner": True})),
    ("POST", "/v3/owner/ping"): Route(lambda body: (200, {"ok": True})),
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
