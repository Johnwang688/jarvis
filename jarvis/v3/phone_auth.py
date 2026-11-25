"""Passkey and session routes on the v3 phone listener."""

import json

from jarvis.v2.remote_auth import (
    ABSOLUTE_S, AuthError, Locked, RemoteAuth, auth_reply, cleared_cookie,
    locked_reply, owner_status, session_cookie,
)


_OPEN = frozenset(("login/options", "login/verify", "pair/options", "pair/verify"))
_SESSION_WRITES = frozenset(("logout", "lock", "unlock/options", "unlock"))
_PREFIX = "/m/api/"
_IDENTITY_ERRORS = {400: "untrusted host", 401: "unauthorized", 403: "cross-origin request refused"}


def session_for(handler) -> dict | None:
    """Return the live phone session for this listener and request."""
    return owner_status(handler)[1]


def touch_unlock(handler, session: dict) -> None:
    """Slide an open window for a session-only write."""
    handler.remote_auth.touch_unlock(session)


def require_unlocked(handler, session: dict) -> tuple[int, dict, list] | None:
    """Slide an open window or return the refusal before the service runs."""
    try:
        handler.remote_auth.require_unlocked(session)
    except Locked:
        return locked_reply()
    except AuthError as exc:
        return auth_reply(exc)
    return None


def _json_body(body: bytes) -> dict:
    try:
        value = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, RecursionError):
        raise AuthError(400, "bad request") from None
    if not isinstance(value, dict):
        raise AuthError(400, "bad request")
    return value


def _open_route(handler, route: str, body: bytes) -> tuple[int, dict, list]:
    auth = handler.remote_auth
    server = handler.server
    if not isinstance(auth, RemoteAuth) or auth.server is not server or not server.remote_only:
        return 401, {"error": "unauthorized"}, []
    if handler.headers.get_all("Host") != [auth.host]:
        return 400, {"error": "untrusted host"}, []
    if handler.headers.get_all("Origin") != [auth.origin]:
        return 403, {"error": "cross-origin request refused"}, []
    value = _json_body(body)
    if route == "login/options":
        return 200, auth.login_options(), []
    if route == "pair/options":
        return 200, auth.pair_options(value.get("code")), []
    credential = value.get("credential")
    if route == "pair/verify":
        return 200, auth.pair_verify(value.get("code"), credential), []
    if not isinstance(credential, dict):
        raise AuthError(400, "bad request")
    ua = (handler.headers.get_all("User-Agent") or [""])[0]
    token, _ = auth.login_verify(credential, ua=ua)
    return 200, {"signed_in": True}, [("Set-Cookie", session_cookie(token))]


def auth_route(handler, path: str, body: bytes) -> tuple[int, dict, list] | None:
    """Handle phone sign-in, pairing and unlock; leave data routes to the caller."""
    if not path.startswith(_PREFIX):
        return None
    route = path.removeprefix(_PREFIX)
    if route != "me" and route not in _OPEN and route not in _SESSION_WRITES:
        return None

    method = handler.command
    try:
        if method == "POST" and route in _OPEN:
            return _open_route(handler, route, body)
        status, session = owner_status(handler)
        if session is None:
            return status, {"error": _IDENTITY_ERRORS[status]}, []
        auth = handler.remote_auth
        if route == "me" and method in ("GET", "HEAD"):
            return 200, {
                "signed_in": True,
                "session": {"created": session["created"], "ends": session["created"] + ABSOLUTE_S},
                "unlock_s": auth.unlock_left(session),
            }, []
        if method != "POST" or route not in _SESSION_WRITES:
            return 404, {"error": "not found"}, []
        if route == "logout":
            auth.logout(session)
            return 200, {"signed_in": False}, [("Set-Cookie", cleared_cookie())]
        value = _json_body(body)
        if route == "unlock":
            if value.keys() != {"assertion"} or not isinstance(value["assertion"], dict):
                raise AuthError(400, "bad request")
            return 200, auth.unlock_verify(session, value["assertion"]), []
        if value:
            raise AuthError(400, "bad request")
        if route == "unlock/options":
            return 200, {"publicKey": auth.unlock_options(session)}, []
        return 200, auth.lock(session), []
    except Locked:
        return locked_reply()
    except AuthError as exc:
        return auth_reply(exc)
