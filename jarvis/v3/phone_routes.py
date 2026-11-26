"""Session and unlock tiers for the phone's data routes."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import Literal

from . import owner_routes, phone_auth
from .daemon import _decode_route_body


_Tier = Literal["read", "session", "unlock"]
_TierFor = Callable[[dict | None], _Tier]
_Projection = Callable[[dict], dict]
_RouteKey = tuple[str, str]

_STATUS_FIELDS = ("service", "version", "pid", "uptime_s", "jobs")
# Match the fields validated by hud3's feedApi.ts isBrief.
_BRIEF_FIELDS = (
    "id", "kind", "rank", "due", "ask", "why_now", "recommend", "options",
    "default", "context", "state", "feedback", "proposal_id",
)
_CHAT_SEND = ("POST", "/v3/owner/chat/send")


def _read(body: dict | None) -> _Tier:
    return "read"


def _session(body: dict | None) -> _Tier:
    return "session"


def _approval_answer(body: dict | None) -> _Tier:
    if (body is not None and body.keys() == {"id", "digest", "decision"}
            and body["decision"] == "deny" and all(isinstance(value, str) for value in body.values())):
        return "session"
    return "unlock"


def _feed_answer(body: dict | None) -> _Tier:
    return "session" if body is not None and body.get("action") in ("not_now", "other") else "unlock"


def _status(payload: dict) -> dict:
    return {key: payload[key] for key in _STATUS_FIELDS}


def _brief(brief: dict) -> dict:
    return {key: brief[key] for key in _BRIEF_FIELDS}


def _feed(payload: dict) -> dict:
    return {key: [_brief(brief) for brief in payload[key]] for key in ("briefs", "recent")}


def _brief_result(payload: dict) -> dict:
    return {**payload, "brief": _brief(payload["brief"])}


@dataclass(frozen=True)
class _PhoneRoute:
    owner_key: _RouteKey | None
    owner_route: owner_routes.Route | None
    tier: _TierFor
    project: _Projection | None = None


def _owner(method: str, name: str, tier: _TierFor,
           project: _Projection | None = None) -> _PhoneRoute:
    key = (method, f"/v3/owner/{name}")
    return _PhoneRoute(key, owner_routes.ROUTES[key], tier, project)


PHONE_ROUTES = {
    ("GET", "/m/api/status"): _PhoneRoute(None, None, _read, _status),
    ("GET", "/m/api/feed"): _owner("GET", "feed", _read, _feed),
    ("POST", "/m/api/feed/answer"): _owner("POST", "feed/answer", _feed_answer, _brief_result),
    ("POST", "/m/api/feed/feedback"): _owner("POST", "feed/feedback", _session, _brief_result),
    ("POST", "/m/api/proposal"): _owner("POST", "proposal", _read),
    ("GET", "/m/api/chat"): _owner("GET", "chat", _read),
    ("POST", "/m/api/chat/events"): _owner("POST", "chat/events", _read),
    ("POST", "/m/api/chat/send"): _owner("POST", "chat/send", _session),
    ("POST", "/m/api/chat/stop"): _owner("POST", "chat/stop", _session),
    ("POST", "/m/api/chat/new"): _owner("POST", "chat/new", _session),
    ("GET", "/m/api/approvals"): _owner("GET", "approvals", _read),
    ("POST", "/m/api/approvals/answer"): _owner("POST", "approvals/answer", _approval_answer),
    ("GET", "/m/api/work"): _owner("GET", "work", _read),
}


def data_route(handler, method: str, path: str, body: bytes) -> tuple[int, dict, list] | None:
    entry = PHONE_ROUTES.get((method, path))
    if entry is None:
        return None
    session = phone_auth.session_for(handler)
    if session is None:
        return 401, {"error": "unauthorized"}, []

    value = None
    if method == "POST":
        try:
            value = _decode_route_body(body)
        except (ValueError, RecursionError):
            # Invalid bodies still go through the route's unlock check.
            pass
    tier = entry.tier(value)
    if tier == "unlock":
        refusal = phone_auth.require_unlocked(handler, session)
        if refusal is not None:
            return refusal
    elif tier == "session":
        phone_auth.touch_unlock(handler, session)
    if method == "POST" and value is None:
        return 400, {"error": "bad request"}, []

    services = handler.server.services
    route = entry.owner_route
    if route is None:
        status, payload = 200, services.status()
    else:
        chat = services.chat
        if entry.owner_key == _CHAT_SEND:
            # Reuse the window's send validation and response mapping.
            chat = SimpleNamespace(send=partial(chat.send, steer=False, phone=True))
        status, payload = route.run(value, services.feed, chat, data_dir=services.data_dir,
                                    get_work=services.work, get_proposals=services.proposals)
    if 200 <= status < 300 and entry.project is not None:
        payload = entry.project(payload)
    return status, payload, []
