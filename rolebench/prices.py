"""The pre-call budget price for a Jarvis-loop model: its most expensive OpenRouter endpoint.

The catalog lists a model at its cheapest provider; a run can be routed to any
of them (and a BYOK key can be several times the catalog price, as Kimi's
was). So the worker's refuse-before-sending estimate uses the highest prompt
price and the highest completion price over every endpoint in
`/api/v1/models/<id>/endpoints`, an upper bound on whatever endpoint serves
the call. The ledger still settles on the usage OpenRouter reports.

One read-only metadata GET per model per day (no key, no model call), cached
under `prices/` in the bench's home. A failed fetch falls back to the cache at
any age; with no cache the run is refused, never estimated at zero.
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


def max_price(doc: dict) -> tuple[float, float, int]:
    """(max prompt $/M, max completion $/M, endpoints priced) from an endpoints reply."""
    data = doc.get("data") if isinstance(doc, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise PriceError("no endpoints list in the reply")
    prompt, completion, n = None, None, 0
    for ep in endpoints:
        pricing = ep.get("pricing") if isinstance(ep, dict) else None
        if not isinstance(pricing, dict):
            continue
        p, c = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
        if p is None or c is None:
            continue
        prompt, completion, n = max(prompt or 0.0, p), max(completion or 0.0, c), n + 1
    if not n:
        raise PriceError("no endpoint carries a prompt and completion price")
    return prompt, completion, n


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


def endpoint_price(model: str, fetch=None, now: float | None = None) -> tuple[float, float]:
    """($ per M input, $ per M output) at the model's most expensive endpoint."""
    key = (model or "").strip().lower()
    if not MODEL_RE.match(key):
        raise PriceError(f"{model!r} is not an OpenRouter model id")
    now = time.time() if now is None else now
    path = _cache_path(key)
    cached = _read_cache(path)
    if cached and now - cached["fetched"] < CACHE_TTL_S:
        return float(cached["prompt"]), float(cached["completion"])
    try:
        prompt, completion, n = max_price((fetch or _http_get)(ENDPOINTS_URL.format(model=urllib.parse.quote(key, safe="/:"))))
    except Exception as exc:
        if cached:
            return float(cached["prompt"]), float(cached["completion"])
        raise PriceError(f"no endpoint price for {model}: {type(exc).__name__}: {exc}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"model": key, "prompt": prompt, "completion": completion, "endpoints": n,
                               "fetched": now, "basis": "max over endpoints"}), encoding="utf-8")
    os.replace(tmp, path)
    return prompt, completion
