"""Which OpenRouter model Jarvis runs on, and which ones the owner can pick.

Two halves that answer two different questions:

* the **catalog** — every model on OpenRouter that can actually run this loop,
  fetched from the public listing and cached on disk; and
* the **roster** — the owner's own shortlist, plus which entry is selected.

**Eligibility is a hard requirement, not a preference.** This is a tool-calling
loop: a model that cannot emit `tool_calls` cannot read a file, cannot search,
cannot approve anything — it can only talk, and every turn ends in the same
failure. So the catalog filters to models whose `supported_parameters` include
`tools`, which is the provider's own statement of capability rather than a
guess from the name. Everything else — price, vision, intelligence — is a
*badge*, because those change how well he works and not whether he works.

Intelligence numbers are Artificial Analysis's `intelligence_index`, published
inside OpenRouter's own catalog for about two thirds of the eligible models.
They are reported, never computed here: an invented score in a picker reads
exactly like a measured one, and this project has already paid for the lesson
that a number with no provenance gets believed.

The selection reaches the rest of the code through `tier()`, which every model
call resolves through instead of reading `config.TIERS` directly. A tier that
was *following* the orchestrator follows the selection too (sub-agents,
compaction, the command reviewer — "every child is the orchestrator tier"), and
a tier the owner pointed somewhere else stays where they put it.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import config, llm

# One `tools` entry is the whole eligibility test — see the module docstring.
REQUIRED_PARAM = "tools"

# `<model>:batch` is the same model addressed through the asynchronous batch
# endpoint, and it is excluded because a turn cannot wait on it: the loop calls
# and blocks, and a batch job answers when it answers. The tell is the price —
# 55 of the 60 batch variants that have a base model are listed at *exactly*
# half its completion price (checked against the live catalog 2026-08-22),
# which is the batch discount, not a cheaper model. Leaving them in would also
# double the length of the browse list with entries that duplicate their base.
EXCLUDED_SUFFIXES = (":batch",)


@dataclass
class ModelInfo:
    """One eligible model, normalized to what a picker needs."""

    id: str
    name: str
    context: int = 0
    # Dollars per million tokens, which is how every price on the OpenRouter
    # site is quoted. The raw payload is dollars per *token* — nine leading
    # zeros — and a picker that showed that would be unreadable.
    prompt_usd: float = 0.0
    completion_usd: float = 0.0
    vision: bool = False
    reasoning: bool = False
    # The reasoning levels this model publishes, highest-first as OpenRouter
    # lists them. Empty means it has no reasoning knob to turn.
    efforts: tuple[str, ...] = ()
    # Artificial Analysis, as republished by OpenRouter. None means unrated,
    # which is not the same as zero and must never be rendered as one.
    intelligence: float | None = None
    agentic: float | None = None
    description: str = ""

    @property
    def free(self) -> bool:
        return self.prompt_usd == 0.0 and self.completion_usd == 0.0

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "context": self.context,
            # Rounded on the way out: dollars-per-token times a million lands
            # on 0.19999999999999998, and the picker is the only consumer.
            "prompt_usd": round(self.prompt_usd, 6),
            "completion_usd": round(self.completion_usd, 6),
            "vision": self.vision,
            "reasoning": self.reasoning,
            "efforts": list(self.efforts),
            "intelligence": self.intelligence,
            "agentic": self.agentic,
            "free": self.free,
            "description": self.description[:280],
        }


def _price(raw: Any) -> float | None:
    """Dollars per million tokens, or None if this is not a real price.

    OpenRouter prices the `openrouter/auto` router at -1 — it is a routing
    policy, not a model, and does not know its own price until it has picked
    one. Returning None drops it from the catalog, which is the right call
    twice over: the SYSTEMS readout would name a model that is not the one
    answering, and a cost estimate against it is fiction.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if value < 0 else value * 1_000_000


def normalize(entry: dict[str, Any]) -> ModelInfo | None:
    """One raw catalog entry -> ModelInfo, or None if it cannot run the loop."""
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    if REQUIRED_PARAM not in (entry.get("supported_parameters") or []):
        return None
    if model_id.endswith(EXCLUDED_SUFFIXES):
        return None

    architecture = entry.get("architecture") or {}
    outputs = architecture.get("output_modalities") or []
    if "text" not in outputs:
        return None  # image/audio generators: not something a loop can talk to

    pricing = entry.get("pricing") or {}
    prompt_usd = _price(pricing.get("prompt"))
    completion_usd = _price(pricing.get("completion"))
    if prompt_usd is None or completion_usd is None:
        return None

    benchmarks = ((entry.get("benchmarks") or {}).get("artificial_analysis")) or {}

    def _score(key: str) -> float | None:
        value = benchmarks.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    reasoning = entry.get("reasoning") or {}
    efforts = reasoning.get("supported_efforts") or []
    top = entry.get("top_provider") or {}
    return ModelInfo(
        id=model_id,
        name=entry.get("name") or model_id,
        context=int(entry.get("context_length") or top.get("context_length") or 0),
        prompt_usd=prompt_usd,
        completion_usd=completion_usd,
        vision="image" in (architecture.get("input_modalities") or []),
        reasoning=bool(reasoning),
        efforts=tuple(e for e in efforts if isinstance(e, str)),
        intelligence=_score("intelligence_index"),
        agentic=_score("agentic_index"),
        description=(entry.get("description") or "").strip(),
    )


# ---- the catalog -----------------------------------------------------------

_catalog_lock = threading.Lock()
_cached: list[ModelInfo] = []
_cached_at: float = 0.0
# Why the last fetch did not happen, when the list being served is a stale
# cache. Surfaced to the picker: a list drawn from a two-week-old cache with no
# sign of it is how an owner ends up asking why a new model is missing.
_stale_reason: str = ""


def _sort_key(model: ModelInfo) -> tuple:
    """Best first: rated models by intelligence, then everything else by name.

    Unrated models sort last rather than at zero. Most of them are small or
    old, but some are simply new, and burying a new model under the cheapest
    rated one is a different lie from putting it at the bottom of a list the
    owner is scrolling anyway.
    """
    return (0 if model.intelligence is not None else 1,
            -(model.intelligence or 0.0),
            model.name.lower())


def _read_cache() -> tuple[list[ModelInfo], float]:
    try:
        payload = json.loads(config.MODEL_CACHE_PATH.read_text(encoding="utf-8"))
        entries = payload.get("models") or []
        fetched_at = float(payload.get("fetched_at") or 0.0)
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return [], 0.0
    models = [m for m in (normalize(e) for e in entries) if m is not None]
    return sorted(models, key=_sort_key), fetched_at


def _write_cache(entries: list[dict[str, Any]]) -> None:
    """Cache the raw payload, not the normalized one.

    Normalization is *our* rule about what can run the loop, and it changes
    when the loop does. Caching the raw entries means an eligibility fix takes
    effect on the next read rather than after a TTL nobody remembers.
    """
    try:
        config.MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.MODEL_CACHE_PATH.write_text(
            json.dumps({"fetched_at": time.time(), "models": entries}),
            encoding="utf-8",
        )
    except OSError:
        pass  # a cache that cannot be written costs a request, never a failure


def catalog(refresh: bool = False) -> list[ModelInfo]:
    """Every eligible model, freshest available copy.

    Degrades in one direction only — toward showing *something*. A failed
    fetch falls back to the disk cache however old it is, and only an empty
    cache turns into an error the picker has to render.
    """
    global _cached, _cached_at, _stale_reason
    with _catalog_lock:
        fresh_for = config.MODEL_CACHE_HOURS * 3600
        now = time.time()
        if not _cached:
            _cached, _cached_at = _read_cache()
        if _cached and not refresh and now - _cached_at < fresh_for:
            return list(_cached)

        try:
            entries = llm.catalog()
        except Exception as exc:
            _stale_reason = f"{type(exc).__name__}: {exc}"
            if _cached:
                return list(_cached)
            raise LookupError(f"model catalog unreachable ({_stale_reason})") from exc

        models = [m for m in (normalize(e) for e in entries) if m is not None]
        if not models:
            # A 200 that parses to nothing eligible is a shape change at the
            # far end, not an empty internet. Keep whatever we had.
            _stale_reason = "catalog returned no eligible models"
            if _cached:
                return list(_cached)
            raise LookupError(_stale_reason)
        _write_cache(entries)
        _cached, _cached_at, _stale_reason = sorted(models, key=_sort_key), now, ""
        return list(_cached)


def stale_reason() -> str:
    """Why the served catalog is older than it should be ('' when it is not)."""
    return _stale_reason


# OpenRouter's reasoning ladder, hardest first. A model advertises the subset
# it understands in `reasoning.supported_efforts`; the ones here are every
# level seen across the live catalog (2026-08-22).
EFFORT_LADDER = ("max", "xhigh", "high", "medium", "low", "minimal", "none")

_effort_warned = False


def cached_info(model_id: str) -> ModelInfo | None:
    """One model from whatever catalog is already in hand — **never fetches**.

    This is asked on the hot path, once per step of every turn. A model call
    must not block on OpenRouter's catalog endpoint to find out how hard to
    think, so memory and the disk cache are the only sources here.
    """
    global _cached, _cached_at
    with _catalog_lock:
        if not _cached:
            _cached, _cached_at = _read_cache()
        for model in _cached:
            if model.id == model_id:
                return model
    return None


def effort_for(model_id: str) -> str | None:
    """The reasoning effort to ask `model_id` for, or None to send nothing.

    Clamped to what the model publishes, because the ladder is not the same
    everywhere: Luna goes up to `max`, gpt-oss-20b stops at `high`. A model
    with no reasoning block gets nothing — sending an effort to a model that
    has no such control says nothing and reads, to anyone later, as though it
    did something.
    """
    global _effort_warned
    wanted = (config.REASONING_EFFORT or "").strip().lower()
    if not wanted or wanted == "default":
        return None
    if wanted not in EFFORT_LADDER:
        if not _effort_warned:
            _effort_warned = True
            print(
                f"[models] ignoring JARVIS_REASONING_EFFORT={wanted!r}: "
                f"expected one of {', '.join(EFFORT_LADDER)}",
                file=sys.stderr,
            )
        return None

    info = cached_info(model_id)
    if info is None:
        # Cold catalog, and this must not become a network call. Send it as
        # asked: an effort a model does not publish is accepted and clamped
        # upstream rather than refused — verified live 2026-08-22, `max` on
        # gpt-oss-20b (which tops out at `high`) returned 200 and spent fewer
        # reasoning tokens than `high` did. Thinking less hard because a cache
        # is cold would be the worse failure.
        return wanted
    if not info.efforts:
        return None
    for level in EFFORT_LADDER[EFFORT_LADDER.index(wanted):]:
        if level in info.efforts:
            return level
    return None


def find(model_id: str) -> ModelInfo | None:
    """One eligible model by id, or None. Never raises — callers use it to
    decorate a roster entry, and a catalog outage must not blank the roster."""
    try:
        for model in catalog():
            if model.id == model_id:
                return model
    except LookupError:
        pass
    return None


# ---- the roster ------------------------------------------------------------


class NotEligible(ValueError):
    """The requested model cannot run this loop (or could not be verified)."""


@dataclass
class Roster:
    models: list[str] = field(default_factory=list)
    selected: str = ""


_roster_lock = threading.Lock()


def _load() -> Roster:
    """The saved roster, or a fresh one seeded with what he is running now.

    Seeding matters: an empty picker teaches nothing, and the first thing an
    owner wants to see in a model list is the model they are already on.
    """
    default = config.TIERS["orchestrator"]
    try:
        payload = json.loads(config.MODELS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Roster(models=[default], selected="")
    if not isinstance(payload, dict):
        return Roster(models=[default], selected="")

    saved = payload.get("models")
    models = [m for m in saved if isinstance(m, str) and m] if isinstance(saved, list) else []
    if default not in models:
        models.insert(0, default)
    selected = payload.get("selected")
    selected = selected if isinstance(selected, str) and selected in models else ""
    return Roster(models=models, selected=selected)


def _save(roster: Roster) -> None:
    config.MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.MODELS_PATH.write_text(
        json.dumps({"models": roster.models, "selected": roster.selected}, indent=2) + "\n",
        encoding="utf-8",
    )


def roster() -> Roster:
    with _roster_lock:
        return _load()


def add(model_id: str) -> Roster:
    """Put a model on the roster, refusing anything that cannot run the loop.

    Validated against the catalog rather than trusted, and a catalog that
    cannot be reached is a refusal rather than a shrug: an id accepted here is
    one the owner can *select*, and a selection that cannot call tools breaks
    every turn afterwards with an error that points nowhere near this decision.
    """
    model_id = (model_id or "").strip()
    if not model_id:
        raise NotEligible("no model id given")
    try:
        eligible = {m.id for m in catalog()}
    except LookupError as exc:
        raise NotEligible(str(exc)) from exc
    if model_id not in eligible:
        raise NotEligible(
            f"{model_id!r} is not an OpenRouter model that supports tool calling"
        )
    with _roster_lock:
        current = _load()
        if model_id not in current.models:
            current.models.append(model_id)
            _save(current)
        return current


def remove(model_id: str) -> Roster:
    """Take a model off the roster.

    Removing the selected one clears the selection back to the configured
    default rather than leaving a pointer at something no longer listed — the
    picker would show nothing selected while the loop kept running it.
    """
    with _roster_lock:
        current = _load()
        if model_id not in current.models:
            raise LookupError(f"{model_id!r} is not on the roster")
        current.models = [m for m in current.models if m != model_id]
        if current.selected == model_id:
            current.selected = ""
        _save(current)
        return current


def select(model_id: str) -> Roster:
    """Choose the model the loop runs on; '' returns to the configured default.

    Only a roster member can be selected. The roster is the point of the
    feature — a shortlist the owner curated — and a select-anything endpoint
    would make it decorative.
    """
    model_id = (model_id or "").strip()
    with _roster_lock:
        current = _load()
        if model_id and model_id not in current.models:
            raise LookupError(f"{model_id!r} is not on the roster")
        current.selected = model_id
        _save(current)
        return current


def selected() -> str:
    """The owner's chosen model, or '' when following the configuration."""
    try:
        return roster().selected
    except Exception:
        return ""  # a corrupt roster must never take the loop down with it


def tier(name: str) -> str:
    """The model for one tier, honoring the owner's selection.

    A tier follows the selection when its configured model *is* the configured
    orchestrator — which is exactly the set of tiers that were following it
    already (subagent, compaction, review). `worker` and `cheap` point
    somewhere else on purpose and stay there, as does any tier the owner
    pinned to a different model in the environment.

    The selection also beats an explicit `JARVIS_ORCHESTRATOR`, on the same
    reasoning that makes an avatar switch beat `jarvis face -a`: an
    environment variable is a default, and a choice made in the window a
    moment ago is not. Anything that must not move takes `model=` explicitly.
    """
    base = config.TIERS[name]
    chosen = selected()
    if chosen and base == config.TIERS["orchestrator"]:
        return chosen
    return base


def describe() -> dict[str, Any]:
    """The roster as the HUD draws it: entries decorated from the catalog."""
    current = roster()
    entries = []
    for model_id in current.models:
        info = find(model_id)
        entries.append(
            info.describe()
            if info
            # Not in the catalog *right now* — an outage, or a model that was
            # retired since it was added. Listed either way: a roster that
            # silently drops rows is worse than one that shows a bare id.
            else {"id": model_id, "name": model_id, "unlisted": True}
        )
    return {
        "models": entries,
        "selected": current.selected,
        "default": config.TIERS["orchestrator"],
        "current": tier("orchestrator"),
    }
