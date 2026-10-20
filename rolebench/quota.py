"""Skip a Claude or Codex cell at 75% of a quota window (read-only)."""

from __future__ import annotations

import json
import math
import os
import time
import urllib.request

from . import config

HARNESS_PROVIDER = {"claude": "claude", "codex": "codex"}


def over(quota, now: float | None = None) -> str | None:
    """'5h at 82%' for windows at or over the threshold; None when none is or unknown."""
    if not isinstance(quota, dict):
        return None
    now = time.time() if now is None else now
    hits = []
    for w in quota.get("windows") or ():
        used = w.get("used_percent") if isinstance(w, dict) else None
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
            continue
        reset = w.get("resets_at")
        if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > 0:
            if (reset / 1000 if reset > 1e12 else reset) <= now:
                continue                      # a window past its reset is stale
        if used >= config.QUOTA_THRESHOLD:
            hits.append(f"{w.get('name') or 'a window'} at {used:g}%")
    return ", ".join(hits) or None


def daemon_reader(provider: str):
    """The HUD's usage column from the daemon: one read-only GET, or None."""
    base = os.environ.get("ROLEBENCH_DAEMON_URL") or f"http://127.0.0.1:{os.environ.get('JARVIS_DAEMON_PORT', '8405')}"
    try:
        with urllib.request.urlopen(base + "/usage", timeout=2) as r:
            return json.loads(r.read())["providers"][provider]["quota"]
    except Exception:
        return None


def guard(harness: str, reader=daemon_reader) -> tuple[bool, str]:
    """(skip, note). Unknown is 'proceed with a warning'; jarvis has no quota."""
    provider = HARNESS_PROVIDER.get(harness)
    if provider is None:
        return False, ""
    try:
        quota = reader(provider)
    except Exception:
        quota = None
    if quota is None:
        return False, f"warning: {provider} quota unknown, proceeding"
    hit = over(quota)
    return (True, f"{provider} {hit}") if hit else (False, "")
