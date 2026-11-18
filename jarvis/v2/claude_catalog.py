"""Claude Code's model catalog, validated identically from SDK and disk.

An unusable response leaves the last good table in place. The cache uses
the Codex catalog's capped, nonblocking, no-follow reader and store writer.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from jarvis import config
from .approvals import clean_line
from .codex_catalog import (
    MAX_ROWS, MAX_MODELS, MAX_NAME, MAX_EFFORTS, MAX_CACHE_BYTES, _ID, _read_capped,
)
from .stores import StoreError, _write_bytes

LOG = logging.getLogger(__name__)
CACHE_VERSION = 1
FAST_SPEED = (("fast", "Fast", "Faster output, billed as extra usage beyond your subscription"),)


def _cache_path(path=None) -> Path:
    return Path(path or config.CLAUDE_CATALOG_PATH).expanduser()


def parse(rows) -> dict[str, dict]:
    """Keep only named Claude models and their advertised effort words."""
    from .router import _CLAUDE_EFFORTS
    if not isinstance(rows, list):
        raise ValueError("Claude model catalog must be a list")
    parsed: dict[str, dict] = {}
    for row in rows[:MAX_ROWS]:
        if len(parsed) >= MAX_MODELS:
            break
        if not isinstance(row, dict) or row.get("value") == "default":
            continue
        model_id = (row.get("resolvedModel") if isinstance(row.get("resolvedModel"), str)
                    else row.get("value"))
        if (not isinstance(model_id, str) or not _ID.fullmatch(model_id)
                or not model_id.startswith("claude-") or model_id in parsed):
            continue
        display = row.get("displayName")
        name = clean_line(display[:MAX_NAME * 4], MAX_NAME) if isinstance(display, str) else ""
        if name and not name.startswith("Claude"):
            name = "Claude " + name
        efforts = []
        advertised = row.get("supportedEffortLevels")
        if row.get("supportsEffort") is True and isinstance(advertised, list):
            for effort in advertised[:MAX_EFFORTS]:
                if isinstance(effort, str) and effort in _CLAUDE_EFFORTS and effort not in efforts:
                    efforts.append(effort)
        parsed[model_id] = {
            "name": name[:MAX_NAME] or model_id,
            "efforts": tuple(efforts),
            "speeds": FAST_SPEED if row.get("supportsFastMode") is True else (),
            # Claude Code reports no input modalities; every model it lists
            # takes images. A wrong yes costs an API error, not a boundary.
            "vision": True,
        }
    if not parsed:
        raise ValueError("Claude model catalog has no usable models")
    return parsed


def rows_of(table: dict[str, dict]) -> list[dict]:
    """Cache the SDK row shape so disk reads pass through the same parser."""
    return [{"value": model, "resolvedModel": model, "displayName": None if entry["name"] == model else entry["name"],
             "supportsEffort": bool(entry["efforts"]),
             "supportsFastMode": any(t[0] == "fast" for t in entry.get("speeds", ())),
             "supportedEffortLevels": list(entry["efforts"])}
            for model, entry in table.items()]


def save(table: dict[str, dict], path=None) -> bool:
    """Atomically save a last good catalog; a write failure is only a miss."""
    target = _cache_path(path)
    body = {"version": CACHE_VERSION, "saved_at": datetime.now(timezone.utc).isoformat(),
            "models": rows_of(table)}
    try:
        _write_bytes(target, (json.dumps(body, indent=2) + "\n").encode())
    except (StoreError, OSError, TypeError, ValueError) as exc:
        LOG.warning("Claude model catalog cache not written (%s)", type(exc).__name__)
        return False
    return True


def load(path=None) -> dict[str, dict] | None:
    """Return validated cached models, or None while the fallback stands."""
    target = _cache_path(path)
    try:
        payload = json.loads(_read_capped(target).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            raise ValueError("unknown cache format")
        return parse(payload.get("models"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError, MemoryError) as exc:
        LOG.warning("Claude model catalog cache ignored (%s)", type(exc).__name__)
        return None
