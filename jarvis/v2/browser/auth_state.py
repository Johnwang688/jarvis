"""Owner HTTP-auth bounds and clocks. No I/O, storage, credentials or authority.

The host owns one AuthBudget per explicit owner navigation operation, retained
for later page challenges. Reconnect rotates a reply nonce elsewhere; it cannot
replace this budget. NavigationClock counts only actual non-auth intervals.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import unicodedata
from urllib.parse import urlsplit

from . import policy
from ..approvals import clean_line

AUTH_WAIT_S = 60.0
AUTH_SUBMISSIONS = 3
REQUESTS_MAX = 128
PROMPTS_MAX = 8
USERNAME_BYTES = 1024
SECRET_BYTES = 4096


class AuthRefused(ValueError):
    """Fixed words only; inputs never become exception text."""


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    @property
    def display(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return f"{self.scheme}://{host}" + (f":{self.port}" if self.port != default else "")


def origin(value, *, refused=frozenset(), challenge=False) -> Origin:
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > policy.MAX_URL:
            raise ValueError()
        # Use the normal control-port/userinfo/ambiguous-authority refusal.
        parts = urlsplit(policy.judge_url(value, refused=refused))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError()
        if challenge and (parts.path not in ("", "/") or parts.query or parts.fragment):
            raise ValueError()
        host = parts.hostname.encode("idna").decode("ascii").lower()
        return Origin(parts.scheme, host, parts.port or (443 if parts.scheme == "https" else 80))
    except (ValueError, UnicodeError):
        raise AuthRefused("the authentication origin is not supported") from None


@dataclass(frozen=True)
class Challenge:
    origin: Origin
    scheme: str
    realm: str


def challenge(request_origin: Origin, raw, *, refused=frozenset()) -> Challenge:
    if (not isinstance(raw, dict) or raw.get("source") != "Server"
            or not isinstance(raw.get("scheme"), str)
            or raw["scheme"].lower() not in ("basic", "digest")
            or not isinstance(raw.get("realm"), str) or len(raw["realm"]) > 4096):
        raise AuthRefused("this authentication challenge is not supported")
    target = origin(raw.get("origin"), refused=refused, challenge=True)
    if target != request_origin:
        raise AuthRefused("the authentication origin changed")
    return Challenge(target, raw["scheme"].lower(), clean_line(raw["realm"], 200))


def credential_fields(username, secret) -> tuple[str, str]:
    for value, cap in ((username, USERNAME_BYTES), (secret, SECRET_BYTES)):
        try:
            if (not isinstance(value, str) or len(value.encode("utf-8")) > cap
                    or any(unicodedata.category(ch).startswith("C") for ch in value)):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise AuthRefused("use bounded authentication fields without control characters") from None
    return username, secret


@dataclass
class AuthBudget:
    started: float | None = None
    submissions: int = 0

    def remaining(self, now: float) -> float:
        if not math.isfinite(now):
            return 0.0
        if self.started is None:
            return AUTH_WAIT_S
        if now < self.started:
            return 0.0
        return max(0.0, self.started + AUTH_WAIT_S - now)

    def announce(self, now: float) -> bool:
        if not math.isfinite(now):
            return False
        if self.started is None:
            self.started = now
        return self.submissions < AUTH_SUBMISSIONS and self.remaining(now) > 0

    def submit(self, now: float) -> bool:
        if self.started is None or not self.announce(now):
            return False
        self.submissions += 1
        return True


@dataclass
class NavigationClock:
    limit: float
    updated: float
    auth: AuthBudget
    spent: float = 0.0
    paused: bool = False

    def _advance(self, now: float) -> None:
        if not math.isfinite(now) or now < self.updated:
            self.spent = self.limit
            return
        if not self.paused:
            self.spent += now - self.updated
        self.updated = now

    def set_paused(self, paused: bool, now: float) -> None:
        self._advance(now)
        self.paused = paused

    def remaining(self, now: float) -> float:
        self._advance(now)
        if self.spent >= self.limit:
            return 0.0
        normal = math.inf if self.paused else self.limit - self.spent
        auth = self.auth.remaining(now) if self.auth.started is not None else math.inf
        return max(0.0, min(normal, auth))
