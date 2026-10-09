"""Deterministic message placement and provider admission (design §8).

Thread.provider is never edited. WP11 calls ready_to_start before admitting a
proposal and next_worker for each NEW thread; running threads finish pinned.
Brief lacks images in the fixed interface: resolve accepts message images and
MCP requirements as keyword context; a supplied Brief supplies profile/MCP.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import json
import logging
import re
import threading
import time
from pathlib import Path

from jarvis import config
from .ledger import DEFAULT_ALLOWANCES, UsageLedger
from .model import (PermissionProfile, ProviderName, Role, RoutingDecision,
                    TaskState, to_json)
from .provider import Event
from .stores import ProjectArchived, StoreError, _write_bytes

PROPOSAL_GRACE_S = 60
HEALTH_CACHE_S = 60
ROLES = ("orchestrator", "implementer", "reviewer", "researcher")
CLI_PROVIDERS = ("claude", "codex")
EFFORTS = ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_CONFIG_LOCK = threading.RLock()
LOG = logging.getLogger(__name__)

# The models each CLI provider can be asked for by name, with the efforts each
# one takes and whether it can see an image. One table, read by the
# capability filter below (vision), by the per-thread model check
# (`thread_model`) and by the HUD's model chip — so the router, the check and
# the list cannot disagree about what exists.
#
# Claude: Claude Code's own model ids; `--effort` is the SDK's `EffortLevel`
# (`providers/claude.py` `EFFORT_LEVELS`), and Haiku 4.5 has no effort control.
#
# Codex: the **union** of `CODEX_FALLBACK` (the offline table, its ladders the
# ones the account's live catalog advertised on 2026-10-08) and every model the
# signed-in account's app-server `model/list` has offered (`set_codex_models`,
# run from a HUD read). The last catalog is saved (`config.CODEX_CATALOG_PATH`)
# and loaded at daemon start (`load_codex_catalog`), so a routing entry only
# the live catalog offers survives a restart. A model the catalog drops is
# marked `available: False` and **never removed**: routing.json, a project's
# routing.models, a pinned thread and a HUD default that name it stay valid,
# and the HUD stops offering it as a new choice. A table that shrank under
# persisted config turned every read of that config into an error (PR #20
# review). A model missing from the table is refused by name, never guessed at.
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CODEX_ULTRA = ("low", "medium", "high", "xhigh", "max", "ultra")
_CODEX_MAX = ("low", "medium", "high", "xhigh", "max")
_CODEX_EFFORTS = ("low", "medium", "high", "xhigh")
CODEX_FALLBACK: dict[str, dict] = {
    "gpt-6.1-sol": {"name": "GPT-6.1 Sol", "efforts": _CODEX_ULTRA, "vision": True},
    "gpt-6-astra": {"name": "GPT-6 Astra", "efforts": _CODEX_ULTRA, "vision": True},
    "gpt-6-sol": {"name": "GPT-6 Sol", "efforts": _CODEX_ULTRA, "vision": True},
    "gpt-6-luna": {"name": "GPT-6 Luna", "efforts": _CODEX_MAX, "vision": True},
    "gpt-5.6-sol": {"name": "GPT-5.6 Sol", "efforts": _CODEX_ULTRA, "vision": True},
    "gpt-5.6-terra": {"name": "GPT-5.6 Terra", "efforts": _CODEX_ULTRA, "vision": True},
    "gpt-5.6-luna": {"name": "GPT-5.6 Luna", "efforts": _CODEX_MAX, "vision": True},
    # Hidden from the account's listing on 2026-10-08, so its ladder is the
    # one this table always carried rather than an advertised one.
    "gpt-5.5": {"name": "GPT-5.5", "efforts": _CODEX_EFFORTS, "vision": True},
}
CLI_MODELS: dict[str, dict[str, dict]] = {
    "claude": {
        "claude-opus-5-5": {"name": "Claude Opus 5.5", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-sonnet-5-5": {"name": "Claude Sonnet 5.5", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-fable-5-1": {"name": "Claude Fable 5.1", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-opus-5": {"name": "Claude Opus 5", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-sonnet-5": {"name": "Claude Sonnet 5", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-haiku-4-5": {"name": "Claude Haiku 4.5", "efforts": (), "vision": True},
    },
    "codex": {model: dict(entry, available=True) for model, entry in CODEX_FALLBACK.items()},
}

# A Codex catalog row is app-server output, so it is held to what Jarvis can
# show and send: an id of slug characters, a name that is one cleaned, capped
# line (no control, bidi or zero-width characters), efforts from the known
# vocabulary, and a bounded number of rows.
CODEX_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
CODEX_NAME_CAP = 60
CODEX_CATALOG_CAP = 200
CODEX_RETAINED_CAP = 64
_CODEX_VOCABULARY = tuple(e for e in EFFORTS if e != "default")


def _codex_name(value, model_id: str) -> str:
    if isinstance(value, str):
        from .approvals import clean_line
        name = clean_line(value[:CODEX_NAME_CAP * 4], CODEX_NAME_CAP)
        if name.strip():
            return name.strip()
    return model_id


def _codex_efforts(advertised) -> tuple[str, ...]:
    efforts: list[str] = []
    if isinstance(advertised, list):
        for item in advertised[:len(_CODEX_VOCABULARY) * 4]:
            effort = item.get("reasoningEffort") if isinstance(item, dict) else item
            if isinstance(effort, str) and effort in _CODEX_VOCABULARY and effort not in efforts:
                efforts.append(effort)
    return tuple(efforts)


def _parse_codex_rows(rows) -> dict[str, dict]:
    """`model/list` rows as table entries; unusable rows are skipped."""
    if not isinstance(rows, list):
        raise ValueError("Codex model catalog must be a list")
    parsed: dict[str, dict] = {}
    for row in rows:
        if len(parsed) >= CODEX_CATALOG_CAP:
            break
        if not isinstance(row, dict) or row.get("hidden") is True:
            continue
        model_id = row.get("model") if isinstance(row.get("model"), str) else row.get("id")
        if not isinstance(model_id, str) or not CODEX_MODEL_ID.fullmatch(model_id) or model_id in parsed:
            continue
        modalities = row.get("inputModalities")
        if isinstance(modalities, list):
            vision = "image" in modalities
        else:
            # Silence is not sight: the offline table's answer for a model it
            # knows, else text-only, so the vision filter never sends an image
            # to a model nobody said could read it.
            vision = bool(CODEX_FALLBACK.get(model_id, {}).get("vision", False))
        parsed[model_id] = {"name": _codex_name(row.get("displayName"), model_id),
                            "efforts": _codex_efforts(row.get("supportedReasoningEfforts")),
                            "vision": vision}
    return parsed


def _install_codex(catalog: dict[str, dict] | None, retained: dict[str, dict]) -> dict[str, dict]:
    """Install the union table. With a catalog: its models (available) in its
    order, then every offline and previously offered model it does not list,
    kept and marked unavailable. Without one: the offline table, available."""
    if catalog is None:
        table = {m: dict(e, available=True) for m, e in CODEX_FALLBACK.items()}
    else:
        table = {m: dict(e, available=True) for m, e in catalog.items()}
        for model_id, entry in CODEX_FALLBACK.items():
            table.setdefault(model_id, dict(entry, available=False))
    for model_id, entry in retained.items():
        table.setdefault(model_id, dict(entry, available=False))
    with _CONFIG_LOCK:
        CLI_MODELS["codex"] = table
    return table


def _save_codex_catalog(table: dict[str, dict]) -> None:
    """Atomically; a failure is logged, never raised: the table in memory is
    already the new one, and the next good read writes it again."""
    rows = [{"id": m, "name": e["name"], "efforts": list(e["efforts"]),
             "vision": bool(e["vision"]), "available": bool(e["available"])}
            for m, e in table.items() if e["available"] or m not in CODEX_FALLBACK]
    payload = {"version": 1, "saved_at": datetime.now(timezone.utc).isoformat(), "models": rows}
    try:
        _write_bytes(Path(config.CODEX_CATALOG_PATH), (json.dumps(payload, indent=1) + "\n").encode())
    except StoreError as exc:
        LOG.warning("Codex model catalog not saved: %s", exc)


def set_codex_models(rows) -> dict[str, dict]:
    """Install and save a validated app-server model catalog; return it.

    An empty or malformed catalog is refused, so a transient protocol problem
    cannot erase anything. Models it does not list stay in the table, marked
    unavailable (the offline ones, and up to `CODEX_RETAINED_CAP` that an
    earlier catalog offered).
    """
    parsed = _parse_codex_rows(rows)
    if not parsed:
        raise ValueError("Codex model catalog has no usable models")
    with _CONFIG_LOCK:
        previous = CLI_MODELS["codex"]
        gone = [m for m in previous if m not in parsed and m not in CODEX_FALLBACK]
        # Oldest first, the ones this catalog just dropped last, so the cap
        # keeps the most recent.
        order = ([m for m in gone if not previous[m].get("available", True)] +
                 [m for m in gone if previous[m].get("available", True)])[-CODEX_RETAINED_CAP:]
        retained = {m: {k: v for k, v in previous[m].items() if k != "available"} for m in order}
        _save_codex_catalog(_install_codex(parsed, retained))
    return {m: dict(e) for m, e in parsed.items()}


def _stored_codex_row(row):
    if not isinstance(row, dict):
        return None
    model_id = row.get("id")
    efforts = row.get("efforts")
    if not isinstance(model_id, str) or not CODEX_MODEL_ID.fullmatch(model_id) or not isinstance(efforts, list):
        return None
    entry = {"name": _codex_name(row.get("name"), model_id), "efforts": _codex_efforts(efforts),
             "vision": row.get("vision") is True}
    return model_id, entry, row.get("available") is True


def load_codex_catalog(path=None) -> dict[str, dict]:
    """At daemon start: the saved catalog over the offline table.

    No file, or one that cannot be read, is the offline table alone — the
    state before any HUD read, never an error.
    """
    path = Path(path or config.CODEX_CATALOG_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = None
    except (OSError, ValueError) as exc:
        LOG.warning("Codex model catalog unreadable (%s); using the offline table", type(exc).__name__)
        payload = None
    catalog: dict[str, dict] | None = None
    retained: dict[str, dict] = {}
    rows = payload.get("models") if isinstance(payload, dict) else None
    for row in (rows[:CODEX_CATALOG_CAP + CODEX_RETAINED_CAP] if isinstance(rows, list) else ()):
        stored = _stored_codex_row(row)
        if stored is None:
            continue
        model_id, entry, available = stored
        if available:
            catalog = catalog if catalog is not None else {}
            catalog.setdefault(model_id, entry)
        elif model_id not in CODEX_FALLBACK:
            retained.setdefault(model_id, entry)
    return _install_codex(catalog, retained)


@dataclass(frozen=True)
class Incoming:
    text: str
    surface: str = "cli"
    project_id: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    intake: bool = False
    spoken: bool = False


class Destination:
    pass


@dataclass(frozen=True)
class Verb(Destination):
    name: str
    argument: str = ""


@dataclass(frozen=True)
class NewTask(Destination):
    text: str


@dataclass(frozen=True)
class Steer(Destination):
    task_id: str


@dataclass(frozen=True)
class FastPath(Destination):
    pass


@dataclass(frozen=True)
class NeedsProject(Destination):
    brief: str
    reply: str = "Which project should this task belong to?"


def classify(incoming: Incoming) -> Destination:
    text = incoming.text.strip()
    # Voice cannot authorize even when it happens to sound like a command.
    if incoming.spoken:
        return Steer(incoming.task_id) if incoming.task_id else FastPath()
    match = re.fullmatch(r"(yes|no|always)\s+(\S+)|(status|projects|tasks)|"
                         r"(cancel)(?:\s+(\S+))?|(steer|redirect):\s*(.+)|"
                         r"(resume)\s+(\S+(?:\s+on\s+\S+)?)", text,
                         flags=re.I | re.S)
    if match:
        a, b, c, d, e, f, g, h, i = match.groups()
        return Verb((a or c or d or f or h).lower(), b or e or g or i or "")
    if incoming.intake or text.lower().startswith("task:"):
        return NewTask(text[5:].strip() if text.lower().startswith("task:") else text)
    if incoming.task_id:
        return Steer(incoming.task_id)
    return FastPath()


def defaults():
    return {"chains": {role: (["claude", "codex"] if role in ("orchestrator", "reviewer")
                              else ["codex", "claude"]) for role in ROLES},
            "models": {role: {"claude": "roster/default", "codex":
                               "gpt-6-astra/xhigh" if role in ("orchestrator", "reviewer")
                               else "gpt-5.6-sol/high"} for role in ROLES},
            "no_new_work": .85, "allowances": copy.deepcopy(DEFAULT_ALLOWANCES)}


def _role(role):
    if role not in ROLES:
        raise ValueError("role must be " + ", ".join(ROLES))
    return str(role.value if isinstance(role, Role) else role)


def _chain(chain):
    if not isinstance(chain, list) or not chain or any(p not in CLI_PROVIDERS for p in chain):
        raise ValueError("chain must be a nonempty list of claude/codex")
    if len(chain) != len(set(chain)):
        raise ValueError("chain contains duplicate providers")
    return chain


def _model(value):
    if not isinstance(value, str) or "/" not in value:
        raise ValueError("expected model/effort")
    model, effort = value.rsplit("/", 1)
    if not model.strip() or any(c.isspace() for c in model) or effort not in EFFORTS:
        raise ValueError("invalid model/effort")
    return model, None if effort == "default" else effort


def _offered(provider) -> tuple[str, ...]:
    """Every effort some model of this CLI takes, in `EFFORTS` order: what
    `roster/<effort>` may name, since the roster's model is not known here."""
    known = {e for entry in CLI_MODELS[provider].values() for e in entry["efforts"]}
    return tuple(e for e in EFFORTS if e in known)


def _cli_model(provider, value):
    """`_model`, for one CLI provider's routing entry: the model must be one
    `CLI_MODELS` names (or `roster`) and the effort one that model offers —
    for `roster`, one some model of that CLI offers (Claude Code refuses an
    effort outside its own levels, so `roster/ultra` is not Claude's)."""
    model, effort = _model(value)
    if model == "roster":
        offered = _offered(provider)
        if effort is not None and effort not in offered:
            raise ValueError(f"{provider} takes no effort {effort!r} "
                             f"(it takes {', '.join(offered)}; or 'default')")
        return model, effort
    known = CLI_MODELS[provider]
    if model not in known:
        raise ValueError(f"{model} is not a {provider} model Jarvis knows "
                         f"(it knows {', '.join(known)})")
    ladder = known[model]["efforts"]
    if effort is not None and effort not in ladder:
        offered = f"it offers {', '.join(ladder)}" if ladder else "it has no effort control"
        raise ValueError(f"{model} does not offer effort {effort!r} ({offered}; or 'default')")
    return model, effort


def check_project_models(models):
    """A project's own `routing.models`, held to the rule `load_routing` holds
    `routing.json` to: a known role, claude/codex only, and each entry a model
    `CLI_MODELS` names (or `roster`) with an effort that model offers. Raises
    ValueError naming the entry; called when `/projects` writes a project."""
    if not isinstance(models, dict):
        raise ValueError("routing.models must be an object")
    for role, value in models.items():
        _role(role)
        if not isinstance(value, dict) or value.keys() - set(CLI_PROVIDERS):
            raise ValueError(f"routing.models.{role} must map claude/codex to model/effort")
        for provider, setting in value.items():
            try:
                _cli_model(provider, setting)
            except ValueError as exc:
                raise ValueError(f"routing.models.{role}.{provider}: {exc}") from exc


# Notes about routing config that could not be used. Each distinct one is
# logged once per process (the table is read on every `GET /usage`), and
# `GET /route` returns the current ones.
_NOTED: dict[str, None] = {}
NOTE_CAP = 300


def _note(notes, text):
    text = text if len(text) <= NOTE_CAP else text[:NOTE_CAP - 1] + "…"
    if notes is not None:
        notes.append(text)
    with _CONFIG_LOCK:
        if text in _NOTED:
            return
        _NOTED[text] = None
        while len(_NOTED) > 256:
            _NOTED.pop(next(iter(_NOTED)))
    LOG.warning("routing: %s", text)


def _well_formed(value) -> bool:
    try:
        _model(value)
    except ValueError:
        return False
    return True


def load_routing(path=None, *, notes=None, keep_unknown=False):
    """routing.json over the defaults, read leniently.

    A bad entry never makes the table unreadable: it falls back to that
    role's default and a note says why (`_note`; appended to `notes` when
    given). Raising here turned one stale model name into a failure of
    `GET /usage`, `GET /route`, every task's `resolve` and every default Codex
    thread, and `POST /route` could not repair it, because it reads the table
    before writing (PR #20 review).

    `keep_unknown` is `configure`'s: a well-formed model entry naming a model
    or effort this process does not know is kept as written instead of
    replaced, so a write about one role never erases the owner's choice for
    another; only what cannot be parsed at all is dropped.
    """
    path = path or config.ROUTING_PATH
    result = defaults()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return result
    except (OSError, ValueError) as exc:
        _note(notes, f"routing.json is unreadable ({type(exc).__name__}); using the built-in table")
        return result
    if not isinstance(saved, dict):
        _note(notes, "routing.json is not an object; using the built-in table")
        return result
    unknown = sorted(str(k) for k in saved.keys() - result.keys())
    if unknown:
        _note(notes, "routing.json fields ignored: " + ", ".join(unknown))
    for key in ("chains", "models"):
        entries = saved.get(key, {})
        if not isinstance(entries, dict):
            _note(notes, f"routing.json {key} must be an object; using the default {key}")
            continue
        for role, value in entries.items():
            if role not in ROLES:
                _note(notes, f"routing.json {key}.{role}: not a role; ignored")
                continue
            if key == "chains":
                try:
                    result[key][role] = _chain(value)
                except ValueError as exc:
                    _note(notes, f"routing.json chains.{role}: {exc}; using "
                                 + ",".join(result[key][role]))
                continue
            if not isinstance(value, dict):
                _note(notes, f"routing.json models.{role} must map claude/codex to model/effort; "
                             "using the defaults")
                continue
            for provider, setting in value.items():
                if provider not in CLI_PROVIDERS:
                    _note(notes, f"routing.json models.{role}.{provider}: not claude or codex; ignored")
                    continue
                try:
                    _cli_model(provider, setting)
                except ValueError as exc:
                    if not (keep_unknown and _well_formed(setting)):
                        _note(notes, f"routing.json models.{role}.{provider}: {exc}; "
                                     f"using {result[key][role][provider]}")
                        continue
                result[key][role][provider] = setting
    fraction = saved.get("no_new_work", result["no_new_work"])
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
        _note(notes, f"routing.json no_new_work must be in (0, 1]; using {result['no_new_work']}")
    else:
        result["no_new_work"] = fraction
    allowances = saved.get("allowances", {})
    if not isinstance(allowances, dict):
        _note(notes, "routing.json allowances must be an object; using the defaults")
        allowances = {}
    for provider, limits in allowances.items():
        if provider not in DEFAULT_ALLOWANCES:
            _note(notes, f"routing.json allowances.{provider}: not a provider; ignored")
            continue
        metric = next(iter(DEFAULT_ALLOWANCES[provider]))
        if (not isinstance(limits, dict) or set(limits) != {metric} or
            isinstance(limits[metric], bool) or not isinstance(limits[metric], (int, float)) or
            not 0 < limits[metric] < float("inf")):
            _note(notes, f"routing.json allowances.{provider} needs a positive {metric}; using the default")
            continue
        result["allowances"][provider] = limits
    return result


class HealthCache:
    def __init__(self, clock=time.monotonic):
        self.clock, self._values, self._lock = clock, {}, threading.RLock()

    def check(self, provider):
        with self._lock:
            key = id(provider)
            previous = self._values.get(key)
            if previous and previous[0] is provider and self.clock() - previous[1] < HEALTH_CACHE_S:
                return previous[2]
            try:
                ok, reason = provider.health()
                result = bool(ok), str(reason)
            except Exception as exc:
                result = False, f"{type(exc).__name__}: {exc}"
            self._values[key] = (provider, self.clock(), result)
            return result


HEALTH = HealthCache()


class RoutingBlocked(RuntimeError):
    def __init__(self, role, drops):
        self.role, self.drops = role, drops
        super().__init__(f"7 nothing survived: {role} blocked; " +
                         "; ".join(f"{p}: {why}" for p, why in drops.items()))


# Optional role syntax: 'use codex for reviewer'. Persist it in the journal,
# because the fixed Task.provider_override holds only a task-wide override.
_USE = re.compile(r"\buse\s+(claude|codex)\b(?:\s+for\s+(orchestrator|implementer|reviewer|researcher)\b)?", re.I)


def apply_override(task, text, stores):
    def strip(match):
        provider, role = match.group(1).lower(), match.group(2)
        if role:
            stores.tasks.journal(task.id, "owner_override", role=role.lower(), provider=provider)
        else:
            task.provider_override = ProviderName(provider)
        return ""
    return re.sub(r"[ \t]{2,}", " ", _USE.sub(strip, text)).strip(" \t\n,;")


def model_settings(role, provider, project=None, settings=None, *, notes=None):
    """(model, effort) for one role on one CLI: the project's own entry when
    it is one Jarvis can run, else the global table's (`load_routing`, which
    has already replaced what it could not use). A project saved before its
    entries were checked, or whose model has since gone, degrades the same
    way, with a note, rather than failing every task in it."""
    settings = settings or load_routing(notes=notes)
    provider = ProviderName(provider).value
    entries = project.routing.models.get(role) if project else None
    value = (entries.get(provider) if isinstance(entries, dict) else None) or None
    if value is not None:
        try:
            _cli_model(provider, value)
        except ValueError as exc:
            _note(notes, f"project {getattr(project, 'id', '?')} routing.models.{role}.{provider}: "
                         f"{exc}; using the global table")
            value = None
    if value is None:
        value = settings["models"][role][provider]
        try:
            _cli_model(provider, value)
        except ValueError as exc:
            # A table handed in rather than loaded is held to the same rule.
            fallback = defaults()["models"][role][provider]
            _note(notes, f"routing models.{role}.{provider}: {exc}; using {fallback}")
            value = fallback
    model, effort = _model(value)
    if model == "roster":
        if provider == "claude":
            # The v1 roster holds OpenRouter ids; Claude Code takes Anthropic
            # names. "roster/default" on Claude means "no model argument" — the
            # CLI's own configured default — and effort from routing.json only.
            # (Found live: the first e2e task handed deepseek to claude.)
            return None, (effort if effort and effort != "default" else None)
        from jarvis import models
        model = models.tier("orchestrator")
        effort = effort or models.effort_for(model)
    return model, effort


def resolve(role, task, project, ledger, providers, *, brief=None, images=None,
            mcp_servers=None, health_cache=None, settings=None, exclude=(), record=True):
    """Select and durably record a fresh role. No provider session is started.

    Owner overrides lead the configured chain, with normal fallback if dropped.
    Soft preferences cannot displace a surviving owner or explicit project lead.
    """
    role = _role(role)
    stores = ledger.stores
    settings = settings or load_routing()
    health_cache = health_cache or HEALTH
    if record:
        task.brief = apply_override(task, task.brief, stores)
        stores.tasks.save(task)
    overrides = [r for r in stores.tasks.read_journal(task.id)
                 if r["event"] == "owner_override" and r.get("role") == role]
    override = overrides[-1]["provider"] if overrides else task.provider_override
    project_chain = project.routing.chains.get(role)
    chain = list(_chain(project_chain) if project_chain else settings["chains"][role])
    reasons = ["1 owner override: " + (str(override.value if isinstance(override, ProviderName) else override) if override else "none"),
               "2 project table: " + (",".join(chain) if project_chain else "inherit"),
               "3 global defaults: " + ",".join(settings["chains"][role])]
    if override:
        if override not in CLI_PROVIDERS:
            raise ValueError("owner override must be claude or codex")
        chain = [override] + [p for p in chain if p != override]
    drops, survivors, states = {}, [], {}
    profile = task.profile or project.profile
    if brief is not None and profile != PermissionProfile.STRICT:
        profile = brief.profile
    images = images or getattr(brief, "images", [])
    required = mcp_servers if mcp_servers is not None else getattr(brief, "mcp_servers", {})
    for name in chain:
        name = ProviderName(name).value
        provider = providers.get(name)
        model, effort = model_settings(role, name, project, settings)
        problems = []
        if profile == PermissionProfile.STRICT and name != "codex":
            problems.append("4 capability filter: strict requires codex")
        if images:
            if name == "claude":
                from jarvis import models
                info = models.cached_info(model)
                capable = info is None or info.vision
            else:
                capable = bool(CLI_MODELS["codex"].get(model, {}).get("vision"))
            if not capable:
                problems.append(f"4 capability filter: model {model} is not known image-capable")
        # Both merged adapters accept explicit MCP configs. Future/fake adapters
        # may narrow that set using supported_mcp_servers; no probing/start().
        supported = getattr(provider, "supported_mcp_servers", None)
        if supported is not None:
            missing = set(required) - set(supported)
            if missing:
                problems.append("4 capability filter: missing MCP servers " + ",".join(sorted(missing)))
        if name in exclude:
            problems.append("5 availability filter: current pinned provider unavailable")
        if not problems:
            ok, health_reason = health_cache.check(provider) if provider else (False, "not in provider roster")
            ledger.set_health(name, ok, health_reason)
            state = ledger.state(name, no_new_work=project.routing.no_new_work if project.routing.no_new_work is not None else settings["no_new_work"], allowances=settings["allowances"])
            states[name] = state["state"]
            if state["state"] != "available":
                problems.append(f"5 availability filter: {state['state']} ({state['reason']})")
        if problems:
            drops[name] = "; ".join(problems)
        else:
            survivors.append(name)
    reasons += [f"{name}: {reason}" for name, reason in drops.items()]
    if not survivors:
        blocked = RoutingBlocked(role, drops)
        if record:
            stores.tasks.journal(task.id, "routing_blocked", milestone="blocked", role=role,
                                 reason="; ".join(reasons + ["6 fallback chain: empty", str(blocked)]), drops=drops)
        raise blocked
    reasons += ["4 capability filter: survivors satisfy requirements",
                "5 availability filter: survivors available", "6 fallback chain: " + ",".join(survivors)]
    chosen = survivors[0]
    protected = (override if override else project_chain[0] if project_chain else None)
    if chosen != protected:
        implementers = [d.provider.value for d in task.status.routing if d.role == Role.IMPLEMENTER]
        if role == "reviewer" and implementers:
            others = [p for p in survivors if p != implementers[-1]]
            if others:
                chosen = others[0]
                reasons.append("soft preference: reviewer differs from implementer")
        if "over_threshold" in states.values() and task.status.routing:
            prior = task.status.routing[-1].provider.value
            if prior in survivors:
                chosen = prior
                reasons.append("soft preference: one provider per task while another is over_threshold")
    reasons.append("selected " + chosen)
    decision = RoutingDecision(Role(role), ProviderName(chosen), "; ".join(reasons),
                               at=datetime.now(timezone.utc).isoformat())
    if record:
        task.status.routing.append(decision)
        stores.tasks.save(task)
        model, effort = model_settings(role, chosen, project, settings)
        stores.tasks.journal(task.id, "routing_decision", **to_json(decision), model=model, effort=effort)
    return decision


class Router:
    def __init__(self, stores, providers=None, ledger=None, *, clock=time.time, health_cache=None):
        self.stores = stores
        self.providers = providers or {}
        self.ledger = ledger or UsageLedger(stores, clock=clock)
        self.clock = clock
        self.health = health_cache or HealthCache()
        self._lock = threading.RLock()

    def on_event(self, event, incoming=None):
        """Runner/event-consumer hook: account usage and handle task proposals.

        Call synchronously from the durable event path: bounded EventBus queues
        may drop records and must not be the sole accounting source.
        """
        self.ledger.on_event(event)
        return self.on_turn_finished(event, incoming)

    def place(self, explicit=None, incoming=None):
        incoming = incoming or Incoming("")
        # An archived project places nothing (decisions B1), and names match
        # ignoring case and surrounding spaces, the rule the HUD numbers by.
        live = [p for p in self.stores.projects.list() if not p.archived]
        if explicit:
            wanted = explicit.strip().casefold()
            matches = [p for p in live if p.id == explicit or p.name.strip().casefold() == wanted]
            return matches[0] if len(matches) == 1 else None
        if incoming.thread_id:
            thread = self.stores.threads.get(incoming.thread_id)
            if thread:
                project = self.stores.projects.get(thread.project_id)
                if project and not project.inbox and not project.archived:
                    return project
        if incoming.project_id:
            project = self.stores.projects.get(incoming.project_id)
            if project and not project.inbox and not project.archived:
                return project
        matches = [p for p in live if p.discord_channel_id == incoming.surface]
        return matches[0] if len(matches) == 1 else None

    def on_turn_finished(self, event, incoming=None):
        event = to_json(event) if isinstance(event, Event) else event
        proposal = event.get("data", {}).get("proposal")
        if event.get("kind") != "turn_finished" or not proposal:
            return None
        thread_id = event.get("thread_id")
        thread = self.stores.threads.get(thread_id) if thread_id else None
        if thread is None or thread.provider != ProviderName.FAST:
            return None
        incoming = incoming or Incoming("", thread_id=thread_id, project_id=event.get("project_id"))
        text = proposal["brief"].strip()
        if not text:
            raise ValueError("proposal requires a brief")
        project = self.place(proposal.get("project"), incoming)
        if project is None:
            return NeedsProject(text)
        with self._lock:
            # TURN_FINISHED may be replayed by an event consumer after restart.
            turn_id = event.get("turn_id")
            if turn_id:
                for task in self.stores.tasks.list():
                    for row in self.stores.tasks.read_journal(task.id):
                        if row["event"] == "proposed_by_fastpath" and row.get("turn_id") == turn_id and row.get("thread_id") == thread_id:
                            return row["reply"]
            provider = proposal.get("provider")
            if provider and provider not in CLI_PROVIDERS:
                raise ValueError("proposal provider must be claude or codex")
            try:
                task = self.stores.tasks.create(project.id, text, provider_override=provider)
                task.brief = apply_override(task, text, self.stores)
                self.stores.tasks.save(task)
            except ProjectArchived:
                # `place` skips archived projects; this is one archived since.
                return (f"Project {project.name} was archived, so I opened nothing. "
                        "Restore it from the HUD's Archive view to work there again.")
            summary = " ".join(task.brief.split()).rstrip(".")
            reply = f"Opened task {task.id} in {project.name}: {summary}. It will ask if anything is unclear."
            self.stores.tasks.journal(task.id, "proposed_by_fastpath", brief=text,
                                     thread_id=thread_id, turn_id=turn_id, reply=reply,
                                     not_before=self.clock() + PROPOSAL_GRACE_S)
            return reply

    def ready_to_start(self, task):
        task = self.stores.tasks.get(task.id)
        if task is None:
            return False
        rows = self.stores.tasks.read_journal(task.id)
        deadlines = [r["not_before"] for r in rows if r["event"] == "proposed_by_fastpath"]
        return task.state == TaskState.INTAKE and (not deadlines or self.clock() >= max(deadlines))

    def cancel_proposal(self, task_id):
        with self._lock:
            task = self.stores.tasks.get(task_id)
            if task is None or task.state != TaskState.INTAKE:
                return False
            rows = self.stores.tasks.read_journal(task_id)
            if not any(r["event"] == "proposed_by_fastpath" and self.clock() < r["not_before"] for r in rows):
                return False
            self.stores.tasks.transition(task_id, TaskState.CANCELLED, reason="owner cancelled within proposal grace")
            return True

    def on_steer(self, task, text):
        text = apply_override(task, text, self.stores)
        self.stores.tasks.save(task)
        return text

    def next_worker(self, task, role, **context):
        with self._lock:
            return resolve(role, task, self.stores.projects.get(task.project_id), self.ledger,
                           self.providers, health_cache=self.health, **context)

    def orchestrator_unavailable(self, task, **context):
        threads = [self.stores.threads.get(i) for i in task.thread_ids]
        current = next((t.provider.value for t in threads if t and t.role == Role.ORCHESTRATOR), None)
        project = self.stores.projects.get(task.project_id)
        try:
            suggestion = resolve("orchestrator", task, project, self.ledger, self.providers,
                                 health_cache=self.health, exclude=(current,), record=False, **context)
            fallback = suggestion.provider.value
        except RoutingBlocked:
            fallback = None
        durable = {"spec": to_json(task.spec), "plan": list(task.plan), "status": to_json(task.status),
                   "worker_reports": [r for r in self.stores.tasks.read_journal(task.id)
                                      if r["event"] in ("worker_report", "report")],
                   "thread_ids": list(task.thread_ids), "report": to_json(task.report) if task.report else None}
        text = f"Task {task.id} blocked: orchestrator {current or 'unknown'} unavailable. "
        text += (f"To restart from spec, plan, status and worker reports, use resume {task.id} on {fallback}."
                 if fallback else "No fallback is available; resume when a provider recovers.")
        self.stores.tasks.journal(task.id, "orchestrator_unavailable", milestone="blocked", reason=text)
        return text, durable

    def view(self, project_id=None):
        notes = []
        settings = load_routing(notes=notes)
        project = self._project(project_id) if project_id else None
        effective = copy.deepcopy(settings)
        if project:
            effective["chains"].update({r: c for r, c in project.routing.chains.items() if c})
            for role, models in project.routing.models.items():
                if role in effective["models"] and isinstance(models, dict):
                    effective["models"][role].update(models)
            if project.routing.no_new_work is not None:
                effective["no_new_work"] = project.routing.no_new_work
        effective["resolved_models"] = {role: {p: dict(zip(("model", "effort"),
                                                           model_settings(role, p, project, settings, notes=notes)))
                                              for p in CLI_PROVIDERS} for role in ROLES}
        states = {}
        for name in ProviderName:
            provider = self.providers.get(name)
            ok, reason = self.health.check(provider) if provider else (False, "not in provider roster")
            self.ledger.set_health(name, ok, reason)
            states[name.value] = self.ledger.state(name, no_new_work=effective["no_new_work"], allowances=settings["allowances"])
        decisions = [{"task_id": t.id, **to_json(d)} for t in self.stores.tasks.list()
                     if not project_id or t.project_id == project_id for d in t.status.routing]
        # What routing.json or the project names that could not be used, and
        # what ran instead (`load_routing`): said, never a failed read.
        return {"table": effective, "states": states, "decisions": sorted(decisions, key=lambda d: d["at"])[-10:],
                "notes": list(dict.fromkeys(notes))}

    def _project(self, project_id):
        try:
            project = self.stores.projects.get(project_id)
        except StoreError as exc:
            raise ValueError(str(exc)) from exc
        if project is None:
            raise ValueError("project not found")
        return project

    def configure(self, body):
        if not isinstance(body, dict) or body.keys() - {"action", "role", "chain", "provider", "model", "project"}:
            raise ValueError("unknown route configuration fields")
        if "project" in body and (not isinstance(body["project"], str) or not body["project"]):
            raise ValueError("project must be a nonempty project id")
        action, role = body.get("action"), _role(body.get("role"))
        if action == "set":
            if set(body) - {"action", "role", "chain", "project"}:
                raise ValueError("set accepts role, chain and optional project")
            chain = body.get("chain")
            chain = chain.split(",") if isinstance(chain, str) else chain
            _chain(chain)
        elif action == "models":
            if set(body) != {"action", "role", "provider", "model"} or body.get("provider") not in CLI_PROVIDERS:
                raise ValueError("models requires role, provider and model/effort")
            _cli_model(body["provider"], body["model"])
        else:
            raise ValueError("action must be set or models")
        with _CONFIG_LOCK:
            if body.get("project"):
                project = self._project(body["project"])
                project.routing.chains[role] = chain
                self.stores.projects.save(project)
            else:
                # Lenient, so a bad file can be repaired by writing over it;
                # entries it does not touch are kept as written when they parse.
                settings = load_routing(keep_unknown=True)
                if action == "set":
                    settings["chains"][role] = chain
                else:
                    settings["models"][role][body["provider"]] = body["model"]
                _write_bytes(config.ROUTING_PATH, (json.dumps(settings, indent=2) + "\n").encode())
        return self.view(body.get("project"))


def daemon_router(daemon):
    """Lazy route service; construction has no provider subprocess or inference."""
    with daemon._lock:
        if not hasattr(daemon, "router"):
            daemon.router = Router(daemon.stores, daemon.providers)
        return daemon.router


def cli(args):
    """Keep the daemon as state owner; CLI speaks the same two JSON routes."""
    import http.client
    from urllib.parse import urlencode
    body = None
    if args.action:
        if args.action == "set" and args.model is not None:
            raise RuntimeError("route set accepts role and chain only")
        body = {"action": args.action, "role": args.role}
        if args.action == "set":
            body["chain"] = args.value
            if args.project:
                body["project"] = args.project
        else:
            body.update(provider=args.value, model=args.model)
            if args.project:
                raise RuntimeError("route models is global; --project is for route set")
    conn = http.client.HTTPConnection("127.0.0.1", config.DAEMON_PORT, timeout=30)
    try:
        path = "/route" + ("?" + urlencode({"project": args.project}) if body is None and args.project else "")
        conn.request("POST" if body else "GET", path, json.dumps(body) if body else None,
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        result = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(result.get("error", "route request failed"))
        print(json.dumps(result, indent=2))
        return 0
    except OSError as exc:
        raise RuntimeError(f"cannot reach v2 daemon: {exc}") from exc
    finally:
        conn.close()
