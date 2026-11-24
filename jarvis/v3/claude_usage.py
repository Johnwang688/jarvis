"""Read Claude Code's subscription windows without importing the v2 HUD."""

from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat
from threading import Lock
import time
from urllib.request import HTTPErrorProcessor, ProxyHandler, Request, build_opener

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_USAGE_TTL_S = 300
_FAILURE_BACKOFF_S = 30
_TIMEOUT_S = 8
_BODY_CAP = 64 * 1024
_WINDOWS = (("five_hour", "5h"), ("seven_day", "week"))

_lock = Lock()
_cached = None
_retry_at = 0.0


class _NoRedirect(HTTPErrorProcessor):
    """Return any HTTP status to the caller without forwarding authorization."""

    def http_response(self, request, response):
        return response

    https_response = http_response


def _open(request, *, timeout):
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def _access_token():
    override = os.environ.get("JARVIS_CLAUDE_CREDENTIALS")
    path = Path(override) if override else Path.home() / ".claude" / ".credentials.json"
    # Never wait on a FIFO, never read without a bound.
    with open(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC), "rb") as login:
        if not stat.S_ISREG(os.fstat(login.fileno()).st_mode):
            return None
        raw = login.read(_BODY_CAP + 1)
    if len(raw) > _BODY_CAP:
        return None
    body = json.loads(raw)
    oauth = body.get("claudeAiOauth") if isinstance(body, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return token if isinstance(token, str) and token.strip() else None


def _percent(value) -> float | None:
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        percent = float(value)
    except OverflowError:
        return None
    return percent if math.isfinite(percent) else None


def _reset_timestamp(value: str) -> float | None:
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            return moment.timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _expired(resets_at, now: float) -> bool:
    """Use the v2 reviewer's ISO, Unix seconds, and millisecond reset forms."""
    if isinstance(resets_at, str):
        seconds = _reset_timestamp(resets_at)
        return seconds is not None and seconds <= now
    seconds = _percent(resets_at)
    if seconds is not None and seconds > 1e12:
        seconds /= 1000
    return seconds is not None and 0 < seconds <= now


def _windows(body, token: str) -> list[dict] | None:
    if not isinstance(body, dict):
        return None
    windows = []
    for key, name in _WINDOWS:
        window = body.get(key)
        if window is None:
            continue
        if not isinstance(window, dict):
            return None
        used = _percent(window.get("utilization"))
        if used is None:
            return None
        resets = window.get("resets_at")
        if not isinstance(resets, str) or token in resets or _reset_timestamp(resets) is None:
            resets = ""
        windows.append({"name": name, "used_percent": used, "resets_at": resets})
    return windows or None


def _fetch(opener) -> list[dict] | None:
    try:
        token = _access_token()
        if token is None:
            return None
        from jarvis.v2.providers.claude import usage_agent_version

        request = Request(CLAUDE_USAGE_URL, headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "claude-code/" + usage_agent_version(),
        })
        with opener(request, timeout=_TIMEOUT_S) as response:
            if response.status != 200:
                return None
            raw = response.read(_BODY_CAP + 1)
        if len(raw) <= _BODY_CAP:
            return _windows(json.loads(raw), token)
    except Exception:
        # Transport and parse errors can quote request headers or raw input.
        # Drop the whole exception and return only unknown usage.
        pass
    return None


def read_windows(*, opener=None) -> list[dict] | None:
    """Read current usage, with a five-minute cache and a short failure back-off."""
    global _cached, _retry_at
    with _lock:
        # A window that reset since the reading says nothing about now: read again, early.
        reset = _cached is not None and any(_expired(w["resets_at"], time.time()) for w in _cached)
        if reset or time.monotonic() >= _retry_at:
            _cached = _fetch(_open if opener is None else opener)
            if _cached is not None and any(_expired(w["resets_at"], time.time()) for w in _cached):
                _cached = None      # the service itself answered with a past window: unknown, and no request loop
            ttl = CLAUDE_USAGE_TTL_S if _cached is not None else _FAILURE_BACKOFF_S
            _retry_at = time.monotonic() + ttl
        return [window.copy() for window in _cached] if _cached is not None else None
