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
# Every effort word a routing entry may carry: "default", OpenRouter's ladder
# and Codex's `ultra`. Which of them a provider or model takes is checked per
# entry (`_cli_model`, `_saved_entry`): `ultra` only where a Codex ladder has it.
EFFORTS = ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
# Codex's own ladder, hardest first: OpenRouter's with `ultra` on top. `ultra`
# is Codex's alone (PR #20 review): it never joins `models.EFFORT_LADDER`, so
# the fast path and Claude can neither offer nor send it.
CODEX_EFFORT_LADDER = ("ultra", "max", "xhigh", "high", "medium", "low", "minimal", "none")
_CONFIG_LOCK = threading.RLock()
LOG = logging.getLogger(__name__)
# Each distinct note about a setting that does not run as written is logged
# once per process (routing is read on every resolve and HUD poll), bounded.
_NOTED: dict[str, None] = {}
NOTE_CAP = 300


def warn_once(text: str) -> None:
    """Log a degraded setting once per process. The text names models and
    settings only — routing and the HUD defaults hold no secrets."""
    with _CONFIG_LOCK:
        if text in _NOTED:
            return
        _NOTED[text] = None
        while len(_NOTED) > 256:
            _NOTED.pop(next(iter(_NOTED)))
    LOG.warning("routing: %s", text)


def _note(notes, text: str) -> None:
    text = text if len(text) <= NOTE_CAP else text[:NOTE_CAP - 1] + "…"
    warn_once(text)
    if notes is not None and text not in notes:
        notes.append(text)


def effort_words(provider) -> tuple[str, ...]:
    """Every effort `provider` can be asked for, hardest first: Codex's
    ladder for Codex, OpenRouter's for the fast path and Claude."""
    if getattr(provider, "value", provider) == "codex":
        return CODEX_EFFORT_LADDER
    from jarvis import models
    return models.EFFORT_LADDER


def clamp_effort(wanted: str, ladder, provider) -> str | None:
    """`wanted` as a model with `ladder` can run it: itself, else the nearest
    level the ladder offers — down first (never ask for more than was meant),
    then up. None for an empty ladder or a word that is not one of
    `provider`'s efforts."""
    ladder = tuple(ladder or ())
    if not ladder:
        return None
    if wanted in ladder:
        return wanted
    order = effort_words(provider)
    if wanted not in order:
        return None
    index = order.index(wanted)
    for level in order[index + 1:] + tuple(reversed(order[:index])):
        if level in ladder:
            return level
    return None


# The models each CLI provider can be asked for by name, with the efforts each
# one takes and whether it can see an image. One table, read by the
# capability filter below (vision), by the per-thread model check
# (`thread_model`) and by the HUD's model chip — so the router, the check and
# the list cannot disagree about what exists.
#
# Claude: Claude Code's own model ids; `--effort` is the SDK's `EffortLevel`
# (`providers/claude.py` `EFFORT_LEVELS`), and Haiku 4.5 has no effort control.
#
# Codex: the signed-in account's own catalog (`model/list`, installed by
# `set_codex_models` after a HUD read refreshes it), saved to
# `config.CODEX_CATALOG_PATH` and loaded at daemon start
# (`load_codex_catalog`). `CODEX_FALLBACK` stands until then, and whenever the
# cache is missing or corrupt; its ladders are the ones the account advertised
# on 2026-10-08. The table is replaced whole, never unioned: a model the
# account stops offering is gone from it. What was saved naming that model —
# a routing.json entry, a project's routing.models, the HUD's Codex default —
# is **never rewritten**: it degrades to the default with a note while the
# model is absent, an effort the model no longer offers is clamped, and the
# owner's choice comes back when the model does (PR #20 review).
#
# A new choice (`/route`, a project write, the chip) is held to the table as
# it is now; a model missing from it is refused by name, never guessed at.
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CODEX_EFFORTS = ("low", "medium", "high", "xhigh")
_CODEX_MAX = ("low", "medium", "high", "xhigh", "max")
_CODEX_ULTRA = ("low", "medium", "high", "xhigh", "max", "ultra")
CODEX_FALLBACK: dict[str, dict] = {
    "gpt-6.1-sol": {"name": "GPT-6.1 Sol", "efforts": _CODEX_ULTRA, "vision": True},
    "gpt-6-sol": {"name": "GPT-6 Sol", "efforts": _CODEX_ULTRA, "vision": True},
    "gpt-6-luna": {"name": "GPT-6 Luna", "efforts": _CODEX_MAX, "vision": True},
    "gpt-6-astra": {"name": "GPT-6 Astra", "efforts": _CODEX_ULTRA, "vision": True},
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
    "codex": {model: dict(entry) for model, entry in CODEX_FALLBACK.items()},
}


def set_codex_models(rows) -> dict[str, dict]:
    """Install the account's `model/list` rows as the Codex table, replacing
    it whole, save them for the next start (`config.CODEX_CATALOG_PATH`), and
    return the parsed table (`codex_catalog.parse`). A catalog with nothing
    usable raises ValueError and changes and saves nothing; a save that fails
    is logged and the installed table stands."""
    from . import codex_catalog
    parsed = codex_catalog.parse(rows)
    with _CONFIG_LOCK:
        CLI_MODELS["codex"] = parsed
        codex_catalog.save(parsed)
    return {m: dict(e) for m, e in parsed.items()}


def load_codex_catalog(path=None) -> dict[str, dict] | None:
    """At daemon start: install the last saved account catalog and return it,
    or None — no file, or one that cannot be used — leaving the built-in
    fallback in place."""
    from . import codex_catalog
    parsed = codex_catalog.load(path)
    if parsed is None:
        return None
    with _CONFIG_LOCK:
        CLI_MODELS["codex"] = parsed
    return {m: dict(e) for m, e in parsed.items()}


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


def _model(value, provider=None):
    """Split a routing entry into (model, effort or None). Syntax only: which
    efforts a provider and a model take is `_cli_model`'s and
    `_saved_entry`'s to judge (`ultra` is Codex's alone)."""
    if not isinstance(value, str) or "/" not in value:
        raise ValueError("expected model/effort")
    model, effort = value.rsplit("/", 1)
    if not model.strip() or any(c.isspace() for c in model) or effort not in EFFORTS:
        raise ValueError("invalid model/effort")
    return model, None if effort == "default" else effort


def _offered(provider) -> tuple[str, ...]:
    """Every effort some model of this CLI takes, in `EFFORTS` order: what
    `roster/<effort>` may name, since the roster's model is not known here.
    Claude Code takes low..max, so `roster/ultra` (or `minimal`) is never
    Claude's."""
    known = {e for entry in CLI_MODELS[provider].values() for e in entry["efforts"]}
    return tuple(e for e in EFFORTS if e in known)


def _cli_model(provider, value):
    """`_model`, for one CLI provider's routing entry: the model must be one
    `CLI_MODELS` names (or `roster`) and the effort one that model offers —
    for `roster`, one some model of that CLI offers. The rule for a **new**
    choice; a saved one degrades instead (`_saved_entry`)."""
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


def _show(value) -> str:
    """A saved value, quoted and capped, for a note."""
    text = repr(value)
    return text if len(text) <= 60 else text[:59] + "…"


def _saved_entry(provider, value, where, notes=None):
    """A saved routing entry as it can run on the table as it is now, or None
    for "use the default". Never raises, never writes (PR #20 review): a
    model the table lacks falls back with a note — and comes back when the
    table has it again — and an effort the model no longer offers is clamped
    down to the nearest one it does (option 1), with a note."""
    try:
        model, effort = _model(value)
    except ValueError:
        _note(notes, f"{where}: {_show(value)} is not a model/effort Jarvis can run; "
                     "using the default")
        return None
    if model == "roster":
        if effort is not None and effort not in _offered(provider):
            _note(notes, f"{where}: {provider} takes no effort {effort!r}; using the default")
            return None
        return value
    known = CLI_MODELS[provider]
    if model not in known:
        _note(notes, f"{where}: {model} is not a {provider} model the account offers now; "
                     "using the default until it does (the setting is kept as written)")
        return None
    ladder = known[model]["efforts"]
    if effort is None or effort in ladder:
        return value
    # `ultra` on a Claude ladder clamps to nothing: the model's own default.
    clamped = clamp_effort(effort, ladder, provider)
    _note(notes, f"{where}: {model} does not offer effort {effort!r} now; "
                 f"running at {clamped or 'its default'}")
    return f"{model}/{clamped or 'default'}"
def check_project_models(models):
    """A project's own `routing.models`, held to the rule a new `/route`
    choice is held to: a known role, claude/codex only, and each entry a
    model `CLI_MODELS` names (or `roster`) with an effort that model offers.
    Raises ValueError naming the entry; called when `/projects` writes a
    project's routing."""
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


def _read_saved(path, notes=None) -> dict:
    """routing.json as a dict; {} when missing, unreadable or not an object
    (with a note). Never raises."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        _note(notes, f"routing.json could not be read ({type(exc).__name__}); using the defaults")
        return {}
    try:
        saved = json.loads(text)
    except (ValueError, RecursionError):
        _note(notes, "routing.json is not valid JSON; using the defaults")
        return {}
    if not isinstance(saved, dict):
        _note(notes, "routing.json is not an object; using the defaults")
        return {}
    return saved


def _limits(saved, result, notes=None) -> None:
    """Overlay `no_new_work` and `allowances` from `saved` onto `result`,
    keeping the default for whatever is malformed."""
    if "no_new_work" in saved:
        fraction = saved["no_new_work"]
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
            _note(notes, "routing.json no_new_work must be in (0, 1]; using the default")
        else:
            result["no_new_work"] = fraction
    allowances = saved.get("allowances", {})
    if not isinstance(allowances, dict):
        _note(notes, "routing.json allowances must be an object; using the defaults")
        return
    for provider, limits in allowances.items():
        if provider not in DEFAULT_ALLOWANCES:
            _note(notes, f"routing.json allowances.{provider}: not a provider; ignored")
            continue
        metric = next(iter(DEFAULT_ALLOWANCES[provider]))
        if (not isinstance(limits, dict) or set(limits) != {metric} or
                isinstance(limits[metric], bool) or not isinstance(limits[metric], (int, float)) or
                not 0 < limits[metric] < float("inf")):
            _note(notes, f"routing.json allowances.{provider} needs a positive {metric}; "
                         "using the default")
            continue
        result["allowances"][provider] = limits


def _overlay(saved, notes=None, *, as_saved=False) -> dict:
    """`defaults()` with every well-formed part of `saved` on top.

    `as_saved=False` (reading, `load_routing`): each model entry as it can run
    now (`_saved_entry`). `as_saved=True` (writing, `configure`): a
    well-formed entry is carried verbatim, even one naming a model the table
    lacks today — it is the owner's choice, and a write of something else must
    not quietly turn it into the default."""
    result = defaults()
    for key in sorted(saved.keys() - result.keys(), key=str):
        _note(notes, f"routing.json field {_show(key)} ignored: not a routing setting")
    for key in ("chains", "models"):
        entries = saved.get(key, {})
        if not isinstance(entries, dict):
            _note(notes, f"routing.json {key} must be an object; using the defaults")
            continue
        for role, value in entries.items():
            if role not in ROLES:
                _note(notes, f"routing.json {key}.{role}: not a role; ignored")
                continue
            if key == "chains":
                try:
                    result[key][role] = _chain(value)
                except ValueError as exc:
                    _note(notes, f"routing.json chains.{role}: {exc}; using the default")
                continue
            if not isinstance(value, dict):
                _note(notes, f"routing.json models.{role} must map claude/codex to "
                             "model/effort; using the defaults")
                continue
            for provider, setting in value.items():
                if provider not in CLI_PROVIDERS:
                    _note(notes, f"routing.json models.{role}.{provider}: "
                                 "not claude/codex; ignored")
                    continue
                if as_saved:
                    try:
                        _model(setting)
                    except ValueError:
                        _note(notes, f"routing.json models.{role}.{provider}: "
                                     f"{_show(setting)} is not model/effort; dropped")
                        continue
                    entry = setting
                elif setting == result[key][role][provider]:
                    # The default, written out by an earlier save: nothing to
                    # degrade to, and nothing the owner chose to warn about.
                    entry = setting
                else:
                    entry = _saved_entry(provider, setting, f"routing.json models.{role}.{provider}", notes)
                if entry is not None:
                    result[key][role][provider] = entry
    _limits(saved, result, notes)
    return result


def load_routing(path=None, *, notes=None):
    """The routing table: the defaults, with routing.json's settings on top.

    Never raises for what the file holds (PR #20 review): a malformed part, a
    model the Codex table lacks now, or an effort a model no longer offers
    degrades — to the default, or clamped — with a note appended to `notes`
    and logged once. The file is never rewritten to match, so the owner's
    choice comes back when the model does."""
    path = path or config.ROUTING_PATH
    return _overlay(_read_saved(path, notes), notes)


def usage_settings(path=None, *, notes=None):
    """Only what `/usage` and the ledger's thresholds need from routing.json:
    `no_new_work` and `allowances`, each degrading to its default. Reads no
    chains and no models, so a routing table at odds with the model catalog
    can never stop the status column (PR #20 review)."""
    result = defaults()
    _limits(_read_saved(path or config.ROUTING_PATH, notes), result, notes)
    return {"no_new_work": result["no_new_work"], "allowances": result["allowances"]}


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


def model_settings(role, provider, project=None, settings=None, notes=None):
    """(model, effort) a fresh `role` thread on `provider` runs on: the
    project's own entry, else the routing table's. An entry naming a model the
    Codex table lacks now falls back — a project's to the table's, the
    table's to the built-in default — with a note, and an effort its model no
    longer offers is clamped, as `load_routing` does for routing.json.
    Nothing is ever written back. A table handed in rather than loaded is held
    to the same rule."""
    settings = settings or load_routing(notes=notes)
    provider = ProviderName(provider).value
    entries = project.routing.models.get(role) if project else None
    value = (entries.get(provider) if isinstance(entries, dict) else None) or None
    if value is not None:
        value = _saved_entry(provider, value, f"project {project.id} routing.models.{role}.{provider}",
                             notes)
    if value is None:
        fallback = defaults()["models"][role][provider]
        value = settings["models"][role][provider]
        if value != fallback:
            value = _saved_entry(provider, value, f"routing models.{role}.{provider}", notes) or fallback
    model, effort = _model(value)
    entry = CLI_MODELS[provider].get(model)
    if entry is not None and effort is not None and effort not in entry["efforts"]:
        # The built-in default meeting a ladder that lacks its level: clamped
        # like a saved one, silently (nobody chose it).
        effort = clamp_effort(effort, entry["efforts"], provider)
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
        # `notes`: every saved setting that does not run as written right
        # now — a model the Codex table lacks, a clamped effort, a malformed
        # part — in words the HUD shows (PR #20 review). Nothing is rewritten.
        notes: list[str] = []
        settings = load_routing(notes=notes)
        project = self._project(project_id) if project_id else None
        effective = copy.deepcopy(settings)
        if project:
            effective["chains"].update({r: c for r, c in project.routing.chains.items() if c})
            for role, models in project.routing.models.items():
                effective["models"][role].update(models)
            if project.routing.no_new_work is not None:
                effective["no_new_work"] = project.routing.no_new_work
        effective["resolved_models"] = {role: {p: dict(zip(("model", "effort"),
                                                           model_settings(role, p, project, settings, notes)))
                                              for p in CLI_PROVIDERS} for role in ROLES}
        states = {}
        for name in ProviderName:
            provider = self.providers.get(name)
            ok, reason = self.health.check(provider) if provider else (False, "not in provider roster")
            self.ledger.set_health(name, ok, reason)
            states[name.value] = self.ledger.state(name, no_new_work=effective["no_new_work"], allowances=settings["allowances"])
        decisions = [{"task_id": t.id, **to_json(d)} for t in self.stores.tasks.list()
                     if not project_id or t.project_id == project_id for d in t.status.routing]
        return {"table": effective, "states": states, "decisions": sorted(decisions, key=lambda d: d["at"])[-10:],
                "notes": notes}

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
                # Built from what the file holds, not from `load_routing`: a
                # stored table that no longer runs as written (a model the
                # Codex catalog dropped, a corrupt part) must not stop the
                # owner saving a fresh one, and an entry this write does not
                # touch is kept verbatim rather than saved as its fallback.
                settings = _overlay(_read_saved(config.ROUTING_PATH), as_saved=True)
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
