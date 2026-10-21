"""The pre-call budget price for a Jarvis-loop model: its most expensive OpenRouter endpoint.

The catalog lists a model at its cheapest provider; a run can be routed to any
of them (and a BYOK key can be several times the catalog price, as Kimi's
was). So the worker's refuse-before-sending estimate uses the highest prompt
price and the highest completion price over every endpoint in
`/api/v1/models/<id>/endpoints`, an upper bound on whatever endpoint serves
the call. The ledger still settles on the usage OpenRouter reports.

One read-only metadata GET per model per day (no key, no model call), cached
under `prices/` in the bench's home. A failed refetch falls back to a cached
price at most 7 days old, raised 25%; an older cache or none at all refuses
the model, never estimating it at zero. The highest `max_completion_tokens`
over the endpoints is kept too, as the estimate's completion allowance.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"
CACHE_TTL_S = 24 * 3600
STALE_MAX_S = 7 * 24 * 3600
STALE_MARGIN = 1.25
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*$")


class PriceError(RuntimeError):
    pass


def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "rolebench"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _per_million(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x * 1e6 if math.isfinite(x) and x >= 0 else None


def max_price(doc: dict) -> tuple[float, float, int, int | None]:
    """(max prompt $/M, max completion $/M, endpoints priced, max completion
    tokens or None) from an endpoints reply."""
    data = doc.get("data") if isinstance(doc, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise PriceError("no endpoints list in the reply")
    prompt, completion, n, max_out = None, None, 0, None
    for ep in endpoints:
        pricing = ep.get("pricing") if isinstance(ep, dict) else None
        if not isinstance(pricing, dict):
            continue
        p, c = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
        if p is None or c is None:
            continue
        prompt, completion, n = max(prompt or 0.0, p), max(completion or 0.0, c), n + 1
        mc = ep.get("max_completion_tokens")
        if isinstance(mc, int) and not isinstance(mc, bool) and mc > 0:
            max_out = max(max_out or 0, mc)
    if not n:
        raise PriceError("no endpoint carries a prompt and completion price")
    return prompt, completion, n, max_out


def _cache_path(model: str) -> Path:
    return config.prices_dir() / (re.sub(r"[^a-z0-9._-]+", "_", model) + ".json")


def _read_cache(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if all(isinstance(doc.get(k), (int, float)) and not isinstance(doc.get(k), bool) for k in ("prompt", "completion", "fetched")):
            return doc
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _cached(doc: dict, margin: float = 1.0) -> tuple[float, float, int | None]:
    mc = doc.get("max_completion")
    mc = mc if isinstance(mc, int) and not isinstance(mc, bool) and mc > 0 else None
    return float(doc["prompt"]) * margin, float(doc["completion"]) * margin, mc


def endpoint_price(model: str, fetch=None, now: float | None = None) -> tuple[float, float, int | None]:
    """($ per M input, $ per M output, max completion tokens or None) at the
    model's most expensive endpoint."""
    key = (model or "").strip().lower()
    if not MODEL_RE.match(key):
        raise PriceError(f"{model!r} is not an OpenRouter model id")
    now = time.time() if now is None else now
    path = _cache_path(key)
    cached = _read_cache(path)
    age = now - cached["fetched"] if cached else None
    if cached and 0 <= age < CACHE_TTL_S:
        return _cached(cached)
    try:
        prompt, completion, n, max_out = max_price((fetch or _http_get)(
            ENDPOINTS_URL.format(model=urllib.parse.quote(key, safe="/:"))))
    except Exception as exc:
        if cached and 0 <= age <= STALE_MAX_S:
            return _cached(cached, STALE_MARGIN)          # a stale price, raised 25%
        why = f"the cached price is {age / 86400:.1f} days old" if cached else "nothing is cached"
        raise PriceError(f"no endpoint price for {model} ({why}; refetch failed: {type(exc).__name__}: {exc})")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"model": key, "prompt": prompt, "completion": completion, "max_completion": max_out,
                               "endpoints": n, "fetched": now, "basis": "max over endpoints"}), encoding="utf-8")
    os.replace(tmp, path)
    return prompt, completion, max_out
