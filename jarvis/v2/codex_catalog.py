"""The signed-in account's Codex model catalog: parsed, cached, reloaded.

`CodexProvider.account_metadata` reads `model/list` from a short-lived
app-server; `parse` turns those rows into the table `router.CLI_MODELS["codex"]`
holds (`router.set_codex_models`). The last good catalog is saved to
`config.CODEX_CATALOG_PATH`, atomically, and loaded at daemon start
(`router.load_codex_catalog`), so a restart — or a Discord-only daemon that no
HUD ever reads — offers the account's catalog instead of the built-in
fallback (PR #20 review, option 3).

Both inputs are untrusted: the rows are app-server output and the cache is a
file on disk. So both go through `parse`, under the same caps:

- an id is slug characters only (`_ID`), never a control character or space;
- a display name is one cleaned line of at most `MAX_NAME` characters;
- only Codex's own effort words survive (`router.CODEX_EFFORT_LADDER`);
- vision is claimed only by a row whose `inputModalities` lists `image` — a
  row that says nothing is text-only (fail closed);
- a hidden row is dropped, and so is anything after `MAX_MODELS` models;
- a catalog with nothing usable is refused, so a bad read never empties the
  table.

The advertised `defaultReasoningEffort` is kept as `advertised_effort`, for
information only: a thread's default effort stays `high` within the model's
ladder (decision A4), whatever Codex would pick.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re

from jarvis import config
from .approvals import clean_line
from .stores import StoreError, _write_bytes

LOG = logging.getLogger(__name__)

MAX_ROWS = 2000          # rows looked at in one catalog (the provider pages 20 × 100)
MAX_MODELS = 100         # usable models kept
MAX_NAME = 64            # display-name characters
MAX_EFFORTS = 16         # effort entries looked at per row
MAX_CACHE_BYTES = 512 * 1024
CACHE_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _cache_path(path=None) -> Path:
    return Path(path or config.CODEX_CATALOG_PATH).expanduser()


def _efforts(row) -> tuple[str, ...]:
    from .router import CODEX_EFFORT_LADDER
    advertised = row.get("supportedReasoningEfforts")
    if not isinstance(advertised, list):
        return ()
    found = []
    for item in advertised[:MAX_EFFORTS]:
        effort = item.get("reasoningEffort") if isinstance(item, dict) else item
        if isinstance(effort, str) and effort in CODEX_EFFORT_LADDER and effort not in found:
            found.append(effort)
    return tuple(found)


def parse(rows) -> dict[str, dict]:
    """The usable models in `rows`, as `{id: {name, efforts, vision,
    advertised_effort}}`. Raises ValueError when there are none."""
    if not isinstance(rows, list):
        raise ValueError("Codex model catalog must be a list")
    parsed: dict[str, dict] = {}
    for row in rows[:MAX_ROWS]:
        if len(parsed) >= MAX_MODELS:
            break
        if not isinstance(row, dict) or row.get("hidden") not in (None, False):
            continue
        model_id = row.get("model") if isinstance(row.get("model"), str) else row.get("id")
        if not isinstance(model_id, str) or not _ID.fullmatch(model_id) or model_id in parsed:
            continue
        efforts = _efforts(row)
        advertised = row.get("defaultReasoningEffort")
        modalities = row.get("inputModalities")
        display = row.get("displayName")
        name = clean_line(display[:MAX_NAME * 4], MAX_NAME) if isinstance(display, str) else ""
        parsed[model_id] = {
            "name": name or model_id,
            "efforts": efforts,
            "vision": isinstance(modalities, list) and "image" in modalities,
            "advertised_effort": advertised if isinstance(advertised, str) and advertised in efforts else None,
        }
    if not parsed:
        raise ValueError("Codex model catalog has no usable models")
    return parsed


def rows_of(table: dict[str, dict]) -> list[dict]:
    """A parsed table as app-server rows again: what the cache holds, so
    loading it is `parse` on the same shape the live response has."""
    rows = []
    for model_id, entry in table.items():
        row = {"model": model_id, "displayName": entry["name"],
               "supportedReasoningEfforts": [{"reasoningEffort": e} for e in entry["efforts"]],
               "inputModalities": ["text", "image"] if entry["vision"] else ["text"],
               "hidden": False}
        if entry.get("advertised_effort"):
            row["defaultReasoningEffort"] = entry["advertised_effort"]
        rows.append(row)
    return rows


def save(table: dict[str, dict], path=None) -> bool:
    """Write the catalog atomically (a sibling temp file, then a rename).
    False when it could not be written — a cache, so never an error."""
    target = _cache_path(path)
    body = {"version": CACHE_VERSION, "saved_at": datetime.now(timezone.utc).isoformat(),
            "models": rows_of(table)}
    try:
        _write_bytes(target, (json.dumps(body, indent=2) + "\n").encode())
    except (StoreError, OSError, TypeError, ValueError) as exc:
        LOG.warning("Codex model catalog cache not written (%s)", type(exc).__name__)
        return False
    return True


def load(path=None) -> dict[str, dict] | None:
    """The cached catalog, parsed as the live one is; None when it is
    missing, too big, unreadable or holds nothing usable (the built-in
    fallback then stands)."""
    target = _cache_path(path)
    try:
        if target.stat().st_size > MAX_CACHE_BYTES:
            raise ValueError("cache file too large")
        payload = json.loads(target.read_bytes().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            raise ValueError("unknown cache format")
        return parse(payload.get("models"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as exc:
        LOG.warning("Codex model catalog cache ignored (%s)", type(exc).__name__)
        return None
