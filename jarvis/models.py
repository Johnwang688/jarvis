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
import os
import sys
import tempfile
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
# level seen across the live catalog (2026-08-22). Codex's `ultra` is not one:
# a cold catalog sends an effort as asked, so a level OpenRouter does not take
# must not pass as a valid one here (v2 `router.CODEX_EFFORT_LADDER` has it).
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


def effort_for(model_id: str, efforts: dict[str, str] | None = None) -> str | None:
    """The reasoning effort to ask `model_id` for, or None to send nothing.

    Clamped to what the model publishes, because the ladder is not the same
    everywhere: Luna goes up to `max`, gpt-oss-20b stops at `high`. A model
    with no reasoning block gets nothing — sending an effort to a model that
    has no such control says nothing and reads, to anyone later, as though it
    did something.
    """
    global _effort_warned
    # A pin made about this model wins over the global default — it is the
    # more specific statement, and the only reason to make one. `efforts` is
    # a roster snapshot the caller already holds (`describe` builds its whole
    # payload from one), so a listing never mixes two reads of the file.
    pins = roster().efforts if efforts is None else efforts
    pinned = pins.get(model_id, "")
    wanted = (pinned or config.REASONING_EFFORT or "").strip().lower()
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


class NotOnRoster(LookupError):
    """The model named is not on the roster (a 404, and nothing else is).

    Its own class so the routes can catch exactly this: a bare LookupError
    also catches the KeyError or IndexError of a real bug, and reporting that
    as "not on the roster" would hide it.
    """


class RosterRefused(ValueError):
    """A roster edit that would leave the default unlisted, or nothing listed.

    The sentence says what to do instead ("choose another default first"),
    because the picker shows it to the owner verbatim.
    """


@dataclass
class Roster:
    models: list[str] = field(default_factory=list)
    selected: str = ""
    # Per-model reasoning effort, by model id. An id absent from here follows
    # `config.REASONING_EFFORT`; the levels are the model's own, because the
    # ladder is not the same everywhere.
    efforts: dict[str, str] = field(default_factory=dict)
    # The configured default (`config.TIERS["orchestrator"]`) the owner took
    # off the roster, or "". Without it `_load` would seed that model straight
    # back on the next read, which made the env model impossible to unpin. It
    # names the model, not just "removed", so a *different* configured default
    # (a changed JARVIS_ORCHESTRATOR) is seeded as before.
    removed_default: str = ""


_roster_lock = threading.Lock()


def _load() -> Roster:
    """The saved roster, or a fresh one seeded with what he is running now.

    Seeding matters: an empty picker teaches nothing, and the first thing an
    owner wants to see in a model list is the model they are already on.
    """
    default = config.TIERS["orchestrator"]
    try:
        payload = json.loads(config.MODELS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError *and* UnicodeDecodeError: a file
        # that is not UTF-8 must degrade to the seed like any corrupt one,
        # not raise out of effort_for on every turn.
        return Roster(models=[default], selected="")
    if not isinstance(payload, dict):
        return Roster(models=[default], selected="")

    saved = payload.get("models")
    models = [m for m in saved if isinstance(m, str) and m] if isinstance(saved, list) else []
    selected = payload.get("selected")
    selected = selected if isinstance(selected, str) and selected in models else ""
    removed = payload.get("removed_default")
    removed = removed if isinstance(removed, str) and removed == default else ""
    # The configured default is seeded unless the owner unpinned it — and even
    # then it comes back whenever nothing is selected, because with no
    # selection it *is* what the loop runs on, and the picker must list the
    # model that is answering. That one rule also keeps the roster from ever
    # being empty: a selection is always a roster member.
    if default not in models and not (removed and selected):
        models.insert(0, default)
        removed = ""

    saved_efforts = payload.get("efforts")
    efforts = {}
    if isinstance(saved_efforts, dict):
        for model_id, level in saved_efforts.items():
            # Kept only for models still on the roster and only for levels that
            # are still on the ladder — an override left behind by a removed
            # model would come back to life if that model were re-added, which
            # is a setting the owner did not make twice.
            if model_id in models and isinstance(level, str) and level in EFFORT_LADDER:
                efforts[model_id] = level
    return Roster(models=models, selected=selected, efforts=efforts,
                  removed_default=removed if default not in models else "")


def _save(roster: Roster) -> None:
    """Write the roster atomically: a temp file beside it, then os.replace.

    The v1 face and the v2 daemon are separate processes reading the same
    file, so a reader must see the old roster or the new one, never half a
    file (which `_load` would read as corrupt and re-seed). The file keeps
    the mode it had.
    """
    path = config.MODELS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        {
            "models": roster.models,
            "selected": roster.selected,
            "efforts": roster.efforts,
            "removed_default": roster.removed_default,
        },
        indent=2,
    ) + "\n"
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=".models-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
            if model_id == current.removed_default:
                current.removed_default = ""  # pinned back on: seed rules resume
            _save(current)
        return current


def removal_refusal(roster_ids: list[str], selected: str, default: str, model_id: str) -> str | None:
    """Why unpinning `model_id` is refused, or None — the rule, as data.

    Pure, so the HUD mock imports it rather than keeping a copy that drifts.
    The rule: the model a default thread runs on is always listed. So the
    last model cannot go, and neither can a removal that would leave the
    effective default (the selection, or with none the configured default)
    off the roster.
    """
    remaining = [m for m in roster_ids if m != model_id]
    if not remaining:
        return f"{model_id} is the only model on the roster; pin another one before unpinning it"
    still_selected = "" if selected == model_id else selected
    if not still_selected and default not in remaining:
        return (f"{model_id} is the default every default thread runs on; "
                "choose another default first")
    return None


def remove(model_id: str) -> Roster:
    """Take a model off the roster — the configured default included.

    Removing the selected one clears the selection back to the configured
    default rather than leaving a pointer at something no longer listed — the
    picker would show nothing selected while the loop kept running it.

    Two refusals, both about the same rule — **the model a default thread runs
    on is always listed**:

    * the last model on the roster cannot go (there would be nothing to run);
    * a removal that would leave the *effective* default off the roster is
      refused with "choose another default first": removing the configured
      default while nothing is selected, or removing the selection while the
      configured default has already been unpinned.

    The model's effort pin goes with it, so a re-add starts from the default.
    """
    with _roster_lock:
        current = _load()
        if model_id not in current.models:
            raise NotOnRoster(f"{model_id} is not on the roster")
        default = config.TIERS["orchestrator"]
        why = removal_refusal(current.models, current.selected, default, model_id)
        if why:
            raise RosterRefused(why)
        remaining = [m for m in current.models if m != model_id]
        selected = "" if current.selected == model_id else current.selected
        current.models = remaining
        current.selected = selected
        current.efforts.pop(model_id, None)
        if model_id == default:
            current.removed_default = default
        _save(current)
        return current


def select(model_id: str) -> Roster:
    """Choose the model the loop runs on; '' returns to the configured default.

    This is **the default** the owner sees: the HUD's "Set as default" lands
    here, it persists across restarts in models.json, and it beats
    `JARVIS_ORCHESTRATOR` (see `tier`). Every default-following v2 fast-path
    thread reads it at the start of each turn. '' ("Reset to config default")
    hands the decision back to the configuration.

    Only a roster member can be selected. The roster is the point of the
    feature — a shortlist the owner curated — and a select-anything endpoint
    would make it decorative.
    """
    model_id = (model_id or "").strip()
    with _roster_lock:
        current = _load()
        if model_id and model_id not in current.models:
            raise NotOnRoster(f"{model_id} is not on the roster")
        current.selected = model_id
        if not model_id:
            # Back to the configured default: it is what answers now, so it is
            # listed again even if the owner had unpinned it.
            default = config.TIERS["orchestrator"]
            if default not in current.models:
                current.models.insert(0, default)
            current.removed_default = ""
        _save(current)
        return current


def set_effort(model_id: str, level: str) -> Roster:
    """Pin how hard one model is asked to think; "" follows the global default.

    The level is checked against that model's own advertised ladder rather
    than clamped down to it. Clamping is right when a *global* default meets a
    model that cannot reach it — see effort_for — but this is a choice made
    about one named model, and silently storing something other than what was
    asked for would leave the picker showing a level nobody selected.
    """
    level = (level or "").strip().lower()
    with _roster_lock:
        current = _load()
        if model_id not in current.models:
            raise NotOnRoster(f"{model_id} is not on the roster")
        if not level:
            current.efforts.pop(model_id, None)
            _save(current)
            return current
        if level not in EFFORT_LADDER:
            raise NotEligible(f"{level!r} is not a reasoning effort")
        info = cached_info(model_id)
        if info is not None and not info.efforts:
            # No reasoning control at all. Storing a pin that effort_for would
            # then ignore is the worst of both: the picker would show a level
            # that never reaches a request.
            raise NotEligible(f"{model_id} has no reasoning effort to set")
        if info is not None and level not in info.efforts:
            raise NotEligible(
                f"{model_id} does not offer {level!r} "
                f"(it offers {', '.join(info.efforts)})"
            )
        current.efforts[model_id] = level
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
    """The roster as the HUD draws it: entries decorated from the catalog.

    Built from **one** roster read. Reading the file again per field (as
    `tier()` and `effort_for()` would) let a change landing mid-listing from
    the other process produce a payload whose `selected` and `current`
    disagree, or that lists a model the same payload's selection says was
    removed.
    """
    snap = roster()
    configured = config.TIERS["orchestrator"]
    entries = []
    for model_id in snap.models:
        info = find(model_id)
        entry = (
            info.describe()
            if info
            # Not in the catalog *right now* — an outage, or a model that was
            # retired since it was added. Listed either way: a roster that
            # silently drops rows is worse than one that shows a bare id.
            else {"id": model_id, "name": model_id, "unlisted": True}
        )
        # What this model is pinned to, and what it will actually be asked for
        # once the global default has been clamped to its ladder. The picker
        # needs both: one is the setting, the other is the consequence.
        entry["effort"] = snap.efforts.get(model_id, "")
        entry["effective_effort"] = effort_for(model_id, snap.efforts) or ""
        entries.append(entry)
    return {
        "models": entries,
        "selected": snap.selected,
        # The configured default (JARVIS_ORCHESTRATOR) — used only while
        # nothing is selected. `current` is what the loop runs on now, i.e.
        # the default the owner sees (tier()'s rule, from the same snapshot);
        # `default_source` says which one it is.
        "default": configured,
        "current": snap.selected or configured,
        "default_source": "hud" if snap.selected else "config",
        "default_effort": (config.REASONING_EFFORT or "").strip().lower(),
    }
