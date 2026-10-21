"""Skip a Claude or Codex cell at 75% of a quota window, or when the quota is unknown (read-only).

The owner's override (`--no-quota-guard` with ROLEBENCH_QUOTA_OVERRIDE) lifts
the 75% guard only. The hard ceiling (ROLEBENCH_QUOTA_CEILING, default 97%)
is never lifted: at or above it, or with a quota that cannot be read (so the
ceiling cannot be checked), the cell or judge call is refused.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.request

from . import config

HARNESS_PROVIDER = {"claude": "claude", "codex": "codex"}


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


CEILING_ENV = "ROLEBENCH_QUOTA_CEILING"


def ceiling() -> float:
    """The hard ceiling in percent. ROLEBENCH_QUOTA_CEILING may only lower it:
    ValueError for a value above the default, at or below 0, or not a number."""
    raw = (os.environ.get(CEILING_ENV) or "").strip()
    if not raw:
        return config.QUOTA_CEILING
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{CEILING_ENV}={raw!r} is not a number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{CEILING_ENV}={raw!r} is not a percent above 0")
    if value > config.QUOTA_CEILING:
        raise ValueError(f"{CEILING_ENV}={raw!r} would raise the ceiling above {config.QUOTA_CEILING:g}%; "
                         f"it can only lower it")
    return value


def _live(quota, now: float | None = None) -> list[tuple[str, float]]:
    """(window name, used percent) for every measured window not past its reset."""
    if not isinstance(quota, dict):
        return []
    now = time.time() if now is None else now
    out = []
    for w in quota.get("windows") or ():
        used = w.get("used_percent") if isinstance(w, dict) else None
        if not _number(used):
            continue
        reset = w.get("resets_at")
        if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > 0:
            if (reset / 1000 if reset > 1e12 else reset) <= now:
                continue                      # a window past its reset is stale
        out.append((str(w.get("name") or "a window"), float(used)))
    return out


def over(quota, now: float | None = None, limit: float | None = None) -> str | None:
    """'5h at 82%' for windows at or over `limit` (the threshold by default); None when none is or unknown."""
    limit = config.QUOTA_THRESHOLD if limit is None else limit
    hits = [f"{name} at {used:g}%" for name, used in _live(quota, now) if used >= limit]
    return ", ".join(hits) or None


def daemon_reader(provider: str):
    """The HUD's usage column from the daemon: one read-only GET, or None."""
    base = os.environ.get("ROLEBENCH_DAEMON_URL") or f"http://127.0.0.1:{os.environ.get('JARVIS_DAEMON_PORT', '8405')}"
    try:
        with urllib.request.urlopen(base + "/usage", timeout=2) as r:
            return json.loads(r.read())["providers"][provider]["quota"]
    except Exception:
        return None


def guard(harness: str, reader=None, override: str = "") -> tuple[bool, str]:
    """(skip, note). Unknown quota skips (fail closed), with or without the
    override: the ceiling cannot be checked against an unknown quota. A window
    at or above the ceiling skips whatever the override; one at or above the
    75% threshold skips unless the owner's override is set. jarvis has no quota."""
    provider = HARNESS_PROVIDER.get(harness)
    if provider is None:
        return False, ""
    reader = reader or daemon_reader
    try:
        limit = ceiling()
    except ValueError as exc:
        return True, f"{exc} (fail closed)"
    try:
        quota = reader(provider)
    except Exception:
        quota = None
    windows = quota.get("windows") if isinstance(quota, dict) else None
    measured = [w for w in windows or () if isinstance(w, dict) and _number(w.get("used_percent"))]
    if not measured:
        if override:
            return True, (f"{provider} quota unknown (fail closed): the override lifts the 75% guard but the "
                          f"{limit:g}% ceiling needs a reading; start the daemon so its read-only /usage answers")
        return True, f"{provider} quota unknown (fail closed)"
    hit = over(quota, limit=limit)
    if hit:
        return True, f"{provider} {hit}: at or above the {limit:g}% ceiling (the override never lifts it)"
    if override:
        return False, ""
    hit = over(quota)
    return (True, f"{provider} {hit}") if hit else (False, "")


def readings(providers, reader=None) -> dict:
    """{provider: {window: used percent}} for the run row's quota_before /
    quota_after (None for a provider whose quota cannot be read)."""
    reader = reader or daemon_reader
    out = {}
    for provider in providers:
        try:
            quota = reader(provider)
        except Exception:
            quota = None
        live = _live(quota)
        out[provider] = {name: used for name, used in live} if live else None
    return out
