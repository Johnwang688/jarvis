"""Import v1 conversations as historical fast-path threads, leaving v1 intact."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jarvis.context import EVICTED_IMAGE, is_live_image
from . import model
from .stores import StoreError, Stores, _lock, _write_bytes


def _strip_images(value: Any) -> Any:
    """Walk nested tool content too, without dropping the first saved message."""
    if isinstance(value, list):
        return [_strip_images(item) for item in value]
    if isinstance(value, dict):
        if is_live_image(value):
            return {"type": "text", "text": EVICTED_IMAGE}
        return {key: _strip_images(item) for key, item in value.items()}
    return value


def _timestamp(value: Any) -> str:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("expected a finite v1 Unix timestamp")
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _read_session(path: Path) -> tuple[dict[str, Any], bytes, bytes]:
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    messages = json.loads((path / "messages.json").read_text(encoding="utf-8"))
    if not isinstance(meta, dict) or not isinstance(messages, list):
        raise ValueError("expected metadata object and messages array")
    if not all(isinstance(message, dict) for message in messages):
        raise ValueError("expected message objects")
    if not isinstance(meta.get("title"), str):
        raise ValueError("expected title string")
    if type(meta.get("turns")) is not int or meta["turns"] < 0:
        raise ValueError("expected nonnegative turn count")
    cost = meta.get("cost_usd")
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        raise ValueError("expected nonnegative finite cost")
    values = dict(title=meta["title"], turns=meta["turns"], cost_usd=cost,
                  created=_timestamp(meta["created"]), updated=_timestamp(meta["updated"]))
    log = (path / "log.jsonl").read_bytes()
    for line in log.decode("utf-8").splitlines():
        item = json.loads(line)
        if not isinstance(item, dict) or not isinstance(item.get("role"), str) or not isinstance(item.get("text"), str):
            raise ValueError("expected v1 log entry with role and text")
        _timestamp(item.get("t"))
    transcript = json.dumps(_strip_images(messages), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return values, log, transcript


def _import_session(path: Path, stores: Stores) -> str:
    values, log, transcript = _read_session(path)
    thread = stores.threads.create(
        stores.projects.inbox().id, model.Role.CHAT, model.ProviderName.FAST,
        provider_session_id=None, migrated_from=path.name, **values,
    )
    destination = stores.threads.path(thread.id).parent
    try:
        _write_bytes(destination / "log.jsonl", log)
        _write_bytes(destination / "v1_messages.json", transcript)
        # Publish the index entry last so a failed copy never looks migrated.
        stores.threads.save(thread)
    except Exception:
        stores.threads.delete(thread.id)
        raise
    return thread.id


def migrate_v1_sessions(v1_sessions_dir: Path, stores: Stores) -> list[str]:
    """Skip bad sessions individually; persisted migration ids make reruns safe."""
    source = Path(v1_sessions_dir)
    if stores.root.resolve().is_relative_to(source.resolve()):
        raise StoreError("The v2 destination must be outside the v1 sessions directory")
    if not source.exists():
        return []
    new_ids = []
    with _lock:
        migrated = {thread.migrated_from for thread in stores.threads.list()}
        for path in sorted(source.iterdir()):
            if not path.is_dir() or path.name in migrated:
                continue
            try:
                new_ids.append(_import_session(path, stores))
                migrated.add(path.name)
            except (OSError, ValueError, TypeError, KeyError, OverflowError, StoreError) as exc:
                print(f"Warning: skipping v1 session {path}: {exc}")
    return new_ids
