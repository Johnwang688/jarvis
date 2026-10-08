"""Which model and effort one chat thread runs on (decisions 2026-10-06, part A).

A chat thread runs on one of three providers — OpenRouter (the fast path),
Claude or Codex — chosen while composing and fixed by the first message,
because a provider session cannot change provider. Within that provider the
model and the effort can change at any time, from the next message on.

Where the choice lives, and why there:

- **On the Thread record** (`Thread.model`, `Thread.effort`). `brief.json` is
  never rewritten: it pins where a thread works and under which rules, and
  which model answers is a property of the next request, not of the
  conversation. `brief.model` only seeds the record at open.
- `Thread.model = None` is **default**. On OpenRouter that is the global
  Model picker (`models.tier("orchestrator")`, i.e. `JARVIS_ORCHESTRATOR` or
  the owner's selection), worked out at the start of *every* turn, so an open
  default thread follows a global change without a restart. Claude defaults
  to `claude-opus-5-5`; Codex to its routing default for the orchestrator
  role (`routing.json`).
- `Thread.effort = None` is **that model's default**: `high`, unless the
  roster pins an effort for it, clamped to the model's own ladder, and
  nothing at all for a model with no reasoning control (A4).
- An effort on a thread with `model = None` is stored **on its own**, and the
  thread keeps following the default model (A4 amendment, 2026-10-07): the
  effort is clamped to whatever the default supports at the start of each
  turn, and dropped for a default with no reasoning control. Only an
  explicit model choice pins a model to a thread.

Nothing here is a tool. The agent cannot change its own model or provider;
only the owner can, through `PATCH /threads/{id}` (asserted by the suites).
"""
from __future__ import annotations

from typing import Any

from jarvis import models
from .model import PermissionProfile, ProviderName, Role, Thread

DEFAULT_EFFORT = "high"
CLAUDE_DEFAULT = "claude-opus-5-5"
LABELS = {ProviderName.FAST: "OpenRouter", ProviderName.CLAUDE: "Claude", ProviderName.CODEX: "Codex"}


class ChoiceRefused(ValueError):
    """The model or effort cannot be used on this thread; the message says why."""


# What each provider can run a project's chat thread under, as data: the
# daemon refuses a thread before creating it with `refusal()`, and the HUD
# greys a provider out from the same table (`describe()` sends it), so the
# two cannot disagree. The rules are the providers' own refusals:
# - Codex (`codex_config.validate`): the auto profile only, and no always-ask
#   additions — the app-server has no pre-tool callback to enforce them;
# - Claude (`ClaudeProvider._options`): no strict profile — Claude Code has
#   no firm-style confinement (design §6.2);
# - the fast path (`FastPathProvider._toolset`): no strict profile either.
PROFILES: dict[ProviderName, tuple[str, ...]] = {
    ProviderName.FAST: ("auto", "ask"),
    ProviderName.CLAUDE: ("auto", "ask"),
    ProviderName.CODEX: ("auto",),
}
TAKES_ALWAYS_ASK: dict[ProviderName, bool] = {
    ProviderName.FAST: True, ProviderName.CLAUDE: True, ProviderName.CODEX: False,
}


def refusal(provider: ProviderName, profile: Any, always_ask: list[str] | tuple = ()) -> str | None:
    """Why `provider` cannot run a chat thread under this profile and these
    always-ask additions, or None when it can."""
    provider = ProviderName(provider)
    profile = PermissionProfile(profile).value
    name = LABELS[provider]
    if profile not in PROFILES[provider]:
        if provider == ProviderName.CODEX:
            return (f"{name} can only run a project on the auto profile, and this project "
                    f"is on {profile}; {_others(provider, profile, always_ask)}change "
                    "the project's profile")
        return (f"{name} cannot run a strict project: it has no strict confinement "
                "(design §6.2); change the project's profile to chat here")
    if always_ask and not TAKES_ALWAYS_ASK[provider]:
        return (f"{name} cannot enforce this project's always-ask commands "
                f"({', '.join(always_ask)}); {_others(provider, profile, always_ask)}"
                "remove them from the project")
    return None


def _others(provider: ProviderName, profile: str, always_ask) -> str:
    names = [LABELS[p] for p in ProviderName if p != provider
             and profile in PROFILES[p] and (TAKES_ALWAYS_ASK[p] or not always_ask)]
    return f"use {' or '.join(names)}, or " if names else ""


def is_chat(thread: Thread) -> bool:
    """Only an owner's chat thread has a choosable model. A task's threads
    run what routing gave them."""
    return thread.role == Role.CHAT and thread.task_id is None


def _cli(provider: ProviderName) -> dict[str, dict]:
    from .router import CLI_MODELS
    return CLI_MODELS[provider.value]


def efforts_of(provider: ProviderName, model: str) -> tuple[str, ...] | None:
    """The model's own effort ladder; () for none; None when unknown (a cold
    OpenRouter catalog — never a network call from here)."""
    provider = ProviderName(provider)
    if provider == ProviderName.FAST:
        info = models.cached_info(model)
        return None if info is None else tuple(info.efforts)
    entry = _cli(provider).get(model)
    return None if entry is None else tuple(entry["efforts"])


def _clamp(wanted: str, ladder: tuple[str, ...]) -> str | None:
    if wanted in ladder:
        return wanted
    order = models.EFFORT_LADDER
    if wanted in order:
        index = order.index(wanted)
        # Down the ladder first (never ask for more than was meant), then up.
        for level in order[index + 1:] + tuple(reversed(order[:index])):
            if level in ladder:
                return level
    return ladder[0] if ladder else None


def default_effort(provider: ProviderName, model: str) -> str | None:
    """A4: high, or the roster's pin for this model, within its ladder."""
    provider = ProviderName(provider)
    wanted = DEFAULT_EFFORT
    if provider == ProviderName.FAST:
        try:
            wanted = models.roster().efforts.get(model) or DEFAULT_EFFORT
        except Exception:
            wanted = DEFAULT_EFFORT
    ladder = efforts_of(provider, model)
    if ladder is None:
        # Cold catalog: sent as asked, the v1 rule (an effort a model does not
        # publish is clamped upstream, not refused).
        return wanted
    return _clamp(wanted, ladder) if ladder else None


def default_model(provider: ProviderName) -> str | None:
    provider = ProviderName(provider)
    if provider == ProviderName.FAST:
        return models.tier("orchestrator")
    if provider == ProviderName.CLAUDE:
        return CLAUDE_DEFAULT
    from .router import model_settings
    return model_settings("orchestrator", "codex")[0]


def default_choice(provider: ProviderName) -> tuple[str | None, str | None]:
    """What a thread left on default runs on right now."""
    provider = ProviderName(provider)
    if provider == ProviderName.CODEX:
        # Codex's existing routing default, model and effort together (A5).
        from .router import model_settings
        model, effort = model_settings("orchestrator", "codex")
        return model, effort if effort is not None else default_effort(provider, model)
    model = default_model(provider)
    return model, default_effort(provider, model) if model else None


def clamp_effort(provider: ProviderName, model: str, wanted: str) -> str | None:
    """A stored effort as `model` can run it: itself when the model offers
    it, else the nearest level it does offer (down first); nothing for a
    model with no reasoning control; as asked when the ladder is unknown."""
    ladder = efforts_of(provider, model)
    if ladder is None:
        return wanted
    return _clamp(wanted, ladder) if ladder else None


def effective(thread: Thread) -> tuple[str | None, str | None]:
    """(model, effort) the thread's next turn runs on.

    A default thread follows the default model every turn, and an effort the
    owner chose for it is re-clamped to whatever that model is now (A4
    amendment), so a default that moves is never sent a level it lacks.
    """
    if thread.model is None:
        model, effort = default_choice(thread.provider)
        if thread.effort is not None and model is not None:
            effort = clamp_effort(thread.provider, model, thread.effort)
        return model, effort
    effort = thread.effort if thread.effort is not None else default_effort(thread.provider, thread.model)
    return thread.model, effort


def _roster() -> list[str]:
    try:
        return list(models.roster().models)
    except Exception:
        return []


def check(provider: ProviderName, model: str | None, effort: str | None, *,
          current: str | None = None) -> tuple[str | None, str | None]:
    """Validate a choice; return it normalized, or raise `ChoiceRefused`.

    `current` is the thread's pinned model, if any: a model already pinned
    stays usable after it leaves the roster (A3) — an effort change on it is
    not a new choice of model.

    An effort with no model is checked against the default model of the
    moment (what the owner was offered), but the model stays None: the
    thread keeps following the default, and `effective` re-clamps the effort
    to it every turn.
    """
    provider = ProviderName(provider)
    if model is not None and not isinstance(model, str):
        raise ChoiceRefused("model must be a string or null")
    if effort is not None and not isinstance(effort, str):
        raise ChoiceRefused("effort must be a string or null")
    model = (model or "").strip() or None
    effort = (effort or "").strip().lower() or None
    if model is not None and model != current:
        if provider == ProviderName.FAST:
            if model not in _roster():
                info = models.find(model)
                if info is None:
                    raise ChoiceRefused(
                        f"{model} is not an OpenRouter model that supports tool calling")
                raise ChoiceRefused(f"{model} is not on your model roster; pin it to the roster first")
        elif model not in _cli(provider):
            raise ChoiceRefused(
                f"{model} is not a {LABELS[provider]} model Jarvis knows "
                f"(it knows {', '.join(_cli(provider))})")
    if effort is not None:
        target = model or default_model(provider)
        if target is None:
            raise ChoiceRefused("an effort needs a model")
        ladder = efforts_of(provider, target)
        if effort not in models.EFFORT_LADDER:
            raise ChoiceRefused(f"{effort!r} is not a reasoning effort")
        if ladder is not None and not ladder:
            raise ChoiceRefused(f"{target} has no reasoning effort to set")
        if ladder is not None and effort not in ladder:
            raise ChoiceRefused(f"{target} does not offer {effort!r} (it offers {', '.join(ladder)})")
    return model, effort


def label(model: str | None, effort: str | None) -> str:
    return (model or "default") + (f" · {effort}" if effort else "")


def provider_models(provider: ProviderName) -> list[dict[str, Any]]:
    """The models the HUD's chip lists for a provider, as text-only rows."""
    provider = ProviderName(provider)
    if provider == ProviderName.FAST:
        return models.describe()["models"]
    rows = []
    for model_id, entry in _cli(provider).items():
        rows.append({"id": model_id, "name": entry["name"], "efforts": list(entry["efforts"]),
                     "vision": entry["vision"], "default_effort": default_effort(provider, model_id)})
    return rows


def describe() -> dict[str, Any]:
    """`GET /thread-models`: per provider, its default and the models to offer."""
    result = {}
    for provider in ProviderName:
        try:
            model, effort = default_choice(provider)
        except Exception as exc:  # a broken routing file must not blank the chip
            model, effort = None, None
            note = f"{type(exc).__name__}: {exc}"
        else:
            note = ""
        rows = provider_models(provider)
        for row in rows:
            row.setdefault("default_effort", default_effort(provider, row["id"]))
        result[provider.value] = {"label": LABELS[provider], "default": model,
                                  "default_effort": effort, "models": rows, "note": note,
                                  "profiles": list(PROFILES[provider]),
                                  "always_ask": TAKES_ALWAYS_ASK[provider]}
    return {"providers": result, "effort_default": DEFAULT_EFFORT}
