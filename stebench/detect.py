"""Detector clients. Multi-backend behind one interface.

    score(text, backend="sapling") -> {ai_prob, predicted_class,
                                       sentence_probs, error, raw}

SAPLING is the default. Its API is usage-metered with NO subscription
($0.005/1k chars, trial keys run free without a card), which is why the
pilot moved off GPTZero - GPTZero gates API access behind a $45/mo plan.
Sapling also returns per-sentence AND per-token probabilities, which is
strictly more useful for building a detector than a document verdict.

GPTZERO is kept wired up in case access ever appears. Its client is written
defensively: the published docs are JS-rendered and could not be scraped, so
the response schema is inferred. Every backend stores the RAW json alongside
the parsed score, so a wrong schema guess costs no re-spend.

    python detect.py --selftest [--backend sapling|gptzero]
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

GPTZERO_ENDPOINT = "https://api.gptzero.me/v2/predict/text"
SAPLING_ENDPOINT = "https://api.sapling.ai/api/v1/aidetect"

# $ per 1000 characters, first 10M/month (sapling.ai/docs/api/pricing)
SAPLING_RATE_PER_1K_CHARS = 0.005

# A free trial key processes 50k chars per 24h and expires after a month.
# The whole pilot is ~116k chars, so it fits in three days at zero cost -
# which is why `detect` takes a character budget and resumes.
SAPLING_TRIAL_CHARS_PER_DAY = 50_000


def _key(names: str | tuple[str, ...], where: str) -> str:
    """First of `names` found in the environment or the repo .env.

    Sapling issues a public/private pair. The REST API wants the PRIVATE key
    (documented as 32 characters); the public one is ~92 chars and is for the
    browser SDK. Order matters here - the public key would authenticate as
    nothing and fail confusingly.
    """
    if isinstance(names, str):
        names = (names,)
    env_lines: list[str] = []
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        env_lines = env.read_text().splitlines()

    for var in names:
        key = os.environ.get(var)
        if not key:
            for line in env_lines:
                if line.startswith(f"{var}="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
                    break
        if key:
            return key

    raise SystemExit(
        f"none of {', '.join(names)} found.\n"
        f"Get one at {where}, then append to .env:\n"
        f"  printf '{names[0]}=%s\\n' 'your_key' >> .env"
    )


def _request(fn, retries: int = 3) -> dict[str, Any]:
    """Shared retry/backoff. Any failure returns an error row, never raises -
    a crashed scan must not lose the scans already paid for."""
    last = None
    for attempt in range(retries):
        try:
            r = fn()
            if r.status_code == 429:
                # Distinguish a burst limit from an exhausted quota. Sapling's
                # trial key allows 50k chars/24h and answers "Capacity used"
                # when that is gone - seconds of backoff cannot fix a daily
                # window, and retrying it per text wastes minutes for nothing.
                if "capacity" in r.text.lower():
                    return {"ok": None, "error": "QUOTA_EXHAUSTED: " + r.text[:200]}
                last = "429 rate limited"
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return {"ok": r.json(), "error": None}
        except httpx.HTTPStatusError as e:
            last = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
            if e.response.status_code < 500:
                break
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001 - network shapes vary
            last = f"{type(e).__name__}: {e}"
            time.sleep(2 * (attempt + 1))
    return {"ok": None, "error": last}


_EMPTY = {"ai_prob": None, "predicted_class": None, "sentence_probs": [],
          "n_sentences_scored": 0, "raw": None}


def score_sapling(text: str) -> dict[str, Any]:
    key = _key(
        ("SAPLING_API_PRIVATE_KEY", "SAPLING_API_KEY", "SAPLING_API_PUBLIC_KEY"),
        "https://sapling.ai/docs/api/api-access/",
    )
    res = _request(lambda: httpx.post(
        SAPLING_ENDPOINT,
        json={"key": key, "text": text, "sent_scores": True, "score_string": False},
        timeout=120,
    ))
    if res["error"]:
        return {**_EMPTY, "error": res["error"]}

    body = res["ok"]
    sents = body.get("sentence_scores") or []
    sent_probs = [s.get("score") for s in sents if isinstance(s, dict) and s.get("score") is not None]
    ai_prob = body.get("score")
    return {
        "ai_prob": ai_prob,
        # Sapling ships no label; 0.5 is the neutral cut. Threshold choice is
        # reported separately in analysis rather than baked in here.
        "predicted_class": None if ai_prob is None else ("ai" if ai_prob >= 0.5 else "human"),
        "class_probabilities": None,
        "sentence_probs": sent_probs,
        "n_sentences_scored": len(sent_probs),
        # Per-token probabilities come free with the same call and are the
        # actual raw material for building a detector - keep them.
        "token_probs": body.get("token_probs") or [],
        "tokens": body.get("tokens") or [],
        "error": None,
        "raw": body,
    }


def score_gptzero(text: str) -> dict[str, Any]:
    key = _key("GPTZERO_API_KEY", "https://app.gptzero.me/app/api")
    res = _request(lambda: httpx.post(
        GPTZERO_ENDPOINT,
        headers={"x-api-key": key, "Content-Type": "application/json",
                 "Accept": "application/json"},
        json={"document": text},
        timeout=120,
    ))
    if res["error"]:
        return {**_EMPTY, "error": res["error"]}
    return _parse_gptzero(res["ok"])


BACKENDS = {"sapling": score_sapling, "gptzero": score_gptzero}
DEFAULT_BACKEND = "sapling"


def score(text: str, backend: str = DEFAULT_BACKEND) -> dict[str, Any]:
    if backend not in BACKENDS:
        raise SystemExit(f"unknown backend {backend!r}; pick one of {list(BACKENDS)}")
    return BACKENDS[backend](text)


def estimate_cost(texts: list[str], backend: str = DEFAULT_BACKEND) -> float:
    if backend != "sapling":
        return float("nan")
    return sum(len(t) for t in texts) / 1000 * SAPLING_RATE_PER_1K_CHARS


def _parse_gptzero(body: dict) -> dict[str, Any]:
    docs = body.get("documents") or []
    doc = docs[0] if docs else body

    ai_prob = doc.get("completely_generated_prob")
    probs = doc.get("class_probabilities") or {}
    if ai_prob is None and probs:
        # newer schema: {human, ai, mixed}
        ai_prob = probs.get("ai")
        if ai_prob is not None and probs.get("mixed") is not None:
            ai_prob = probs["ai"] + 0.5 * probs["mixed"]

    sents = doc.get("sentences") or []
    sent_probs = [s.get("generated_prob") for s in sents if isinstance(s, dict)]

    return {
        "ai_prob": ai_prob,
        "predicted_class": doc.get("document_classification") or doc.get("predicted_class"),
        "class_probabilities": probs or None,
        "sentence_probs": [p for p in sent_probs if p is not None],
        "n_sentences_scored": len(sent_probs),
        "error": None,
        "raw": body,
    }


def _selftest(backend: str = DEFAULT_BACKEND) -> int:
    sample = (
        "The heat pump moves thermal energy from a cold reservoir to a warm one. "
        "It does this with a refrigerant that changes phase at two different "
        "pressures. A compressor raises the pressure of the vapour, which also "
        "raises its temperature above that of the indoor air. The hot vapour then "
        "gives up its heat in the condenser and becomes a liquid again."
    )
    print("backend: ", backend)
    print("endpoint:", SAPLING_ENDPOINT if backend == "sapling" else GPTZERO_ENDPOINT)
    try:
        res = score(sample, backend)
    except SystemExit as e:
        print(e)
        return 1
    print("key:      found")
    if res["error"]:
        print("FAILED:  ", res["error"])
        return 1

    print("ai_prob:  ", res["ai_prob"])
    print("class:    ", res["predicted_class"])
    print("sentences:", res["n_sentences_scored"])
    print("\n--- raw response keys (schema check) ---")
    raw = res["raw"]
    print(json.dumps(_shape(raw), indent=2)[:2000])
    return 0


def _shape(obj: Any, depth: int = 0) -> Any:
    """Structure of a json blob without its bulk."""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {k: _shape(v, depth + 1) for k, v in list(obj.items())[:20]}
    if isinstance(obj, list):
        return [_shape(obj[0], depth + 1), f"...x{len(obj)}"] if obj else []
    if isinstance(obj, str):
        return f"<str len={len(obj)}>"
    return obj


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        b = DEFAULT_BACKEND
        if "--backend" in sys.argv:
            b = sys.argv[sys.argv.index("--backend") + 1]
        raise SystemExit(_selftest(b))
    print(__doc__)
    print(f"usage: python detect.py --selftest [--backend {'|'.join(BACKENDS)}]")
