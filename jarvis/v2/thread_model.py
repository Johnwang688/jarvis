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
  default thread follows a global change without a restart. Claude and Codex
  follow **the HUD's default for that provider** when the owner set one
  (`set_provider_default`, `config.PROVIDER_DEFAULTS_PATH`, 2026-10-08), and
  otherwise their built-in default: `claude-opus-5-5` for Claude, the routing
  default for the orchestrator role (`routing.json`) for Codex. The HUD's
  Codex default wins over routing **for chat threads only**; it never writes
  routing.json, and tasks keep routing's table.
- `Thread.effort = None` is **that model's default**: `high`, unless the
  roster pins an effort for it, clamped to the model's own ladder, and
  nothing at all for a model with no reasoning control (A4). A Codex
  catalog's advertised default effort is not used: A4 is the rule, and an
  effective effort that moved whenever the HUD refreshed the catalog would
  make one thread's turns incomparable (PR #20 review).
- An effort on a thread with `model = None` is stored **on its own**, and the
  thread keeps following the default model (A4 amendment, 2026-10-07): the
  effort is clamped to whatever the default supports at the start of each
  turn, and dropped for a default with no reasoning control. Only an
  explicit model choice pins a model to a thread.

Nothing here is a tool. The agent cannot change its own model or provider,
nor a provider's default; only the owner can, through `PATCH /threads/{id}`
and `POST /thread-models` (asserted by the suites).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from jarvis import config, models
from .model import PermissionProfile, ProviderName, Role, Thread

DEFAULT_EFFORT = "high"
# Every effort level, hardest first: OpenRouter's ladder (`models.EFFORT_LADDER`)
# with Codex's `ultra` above `max`. `ultra` is Codex's alone — OpenRouter does
# not take it, and a cold OpenRouter catalog sends an effort as asked — so it
# is a reasoning effort only for a CLI provider (`_vocabulary`), and the order
# is what a clamp walks.
EFFORT_ORDER = ("ultra",) + tuple(models.EFFORT_LADDER)
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


def _vocabulary(provider: ProviderName) -> tuple[str, ...]:
    """The words that are reasoning efforts on this provider."""
    return tuple(models.EFFORT_LADDER) if ProviderName(provider) == ProviderName.FAST else EFFORT_ORDER


def _clamp(wanted: str, ladder: tuple[str, ...]) -> str | None:
    if wanted in ladder:
        return wanted
    order = EFFORT_ORDER
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
    return default_choice(provider)[0]


def builtin_choice(provider: ProviderName) -> tuple[str | None, str | None]:
    """What a Claude or Codex default thread runs on with no HUD default:
    Opus 5.5 at its default effort for Claude; for Codex its routing default,
    model and effort together (A5). "Reset to built-in default" returns here."""
    provider = ProviderName(provider)
    if provider == ProviderName.CODEX:
        from .router import model_settings
        model, effort = model_settings("orchestrator", "codex")
        return model, effort if effort is not None else default_effort(provider, model)
    model = CLAUDE_DEFAULT if provider == ProviderName.CLAUDE else models.tier("orchestrator")
    return model, default_effort(provider, model) if model else None


def default_choice(provider: ProviderName) -> tuple[str | None, str | None]:
    """What a thread left on default runs on right now.

    Claude and Codex: the HUD's default when the owner set one (its effort,
    or the model's own default effort, within the model's ladder), else the
    built-in one. OpenRouter: the global Model picker.
    """
    provider = ProviderName(provider)
    return _choice(provider, hud_default(provider))


def _choice(provider: ProviderName, chosen) -> tuple[str | None, str | None]:
    if chosen is None:
        return builtin_choice(provider)
    model, effort = chosen
    if effort:
        return model, clamp_effort(provider, model, effort)
    if provider == ProviderName.CODEX:
        # "No effort" on the model routing already names means routing's
        # effort for it: setting routing's own model as the default must not
        # quietly drop it from xhigh to high (PR #15 review).
        try:
            routed, routed_effort = builtin_choice(provider)
        except Exception:
            routed, routed_effort = None, None
        if routed == model:
            return model, routed_effort
    return model, default_effort(provider, model)


def default_source(provider: ProviderName) -> str:
    """Where the default comes from: "hud" (chosen in the HUD), else "config"
    (OpenRouter's env model), "built-in" (Claude) or "routing" (Codex)."""
    provider = ProviderName(provider)
    if provider == ProviderName.FAST:
        return "hud" if models.selected() else "config"
    if hud_default(provider) is not None:
        return "hud"
    return "built-in" if provider == ProviderName.CLAUDE else "routing"


# ---- the HUD's default for Claude and Codex (2026-10-08) --------------------
#
# Stored in its own file (`config.PROVIDER_DEFAULTS_PATH`) as
# `{"claude": {"model", "effort"}, "codex": {...}}`; `effort: null` is that
# model's own default effort. Not in models.json, which is the OpenRouter
# roster rewritten whole by two processes; and **never in routing.json**: the
# Codex default here is a chat-thread setting, and role routing is Settings'
# (§12.1). A stored model Jarvis no longer knows is ignored (the built-in
# default applies, and `describe` says so) rather than sent to a provider.

SETTABLE = (ProviderName.CLAUDE, ProviderName.CODEX)
_defaults_lock = threading.Lock()


def _read_defaults() -> dict[str, Any]:
    try:
        payload = json.loads(config.PROVIDER_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers a non-UTF-8 file as well as bad JSON: a corrupt
        # file degrades to the built-in defaults, never to an error per turn.
        return {}
    return payload if isinstance(payload, dict) else {}


def _stored_raw(provider: ProviderName) -> tuple[str, Any] | None:
    """The stored (model, effort) exactly as the file has them."""
    entry = _read_defaults().get(ProviderName(provider).value)
    if not isinstance(entry, dict):
        return None
    model = entry.get("model")
    if not isinstance(model, str) or not model:
        return None
    return model, entry.get("effort")


def _bad_effort(effort: Any) -> bool:
    """A stored effort that is not a reasoning effort at all ("turbo", 3)."""
    if effort is None or (isinstance(effort, str) and not effort.strip()):
        return False
    return not isinstance(effort, str) or effort.strip().lower() not in EFFORT_ORDER


def _stored(provider: ProviderName) -> tuple[str, str | None] | None:
    """The stored default, its effort sanitized: one that is not a reasoning
    effort is read as None (the model's own default), never handed to the
    clamp — which would map an unknown word to the bottom of the ladder."""
    raw = _stored_raw(provider)
    if raw is None:
        return None
    model, effort = raw
    if _bad_effort(effort) or not isinstance(effort, str):
        return model, None
    return model, effort.strip().lower() or None


def hud_default(provider: ProviderName) -> tuple[str, str | None] | None:
    """The owner's HUD default for Claude or Codex as stored (model, effort),
    or None: none set, OpenRouter (its default is the Model picker's), or a
    stored model Jarvis no longer knows."""
    provider = ProviderName(provider)
    if provider not in SETTABLE:
        return None
    stored = _stored(provider)
    if stored is None or stored[0] not in _cli(provider):
        return None
    return stored


def _save_defaults(data: dict[str, Any]) -> None:
    """Atomically: a temp file beside it, then os.replace (the mode is kept),
    so a reader sees the old defaults or the new ones, never half a file."""
    path = config.PROVIDER_DEFAULTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=".provider_defaults-", suffix=".tmp", dir=str(path.parent))
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


def set_provider_default(provider: Any, model: Any, effort: Any = None) -> tuple[str, str | None] | None:
    """Set — or with `model` "" reset — the default Claude or Codex chat
    threads run on; return the stored (model, effort), or None after a reset.

    Validated like a thread's choice: a model `router.CLI_MODELS` names for
    that provider, and an effort on that model's own ladder (refused, never
    clamped: this is a choice about one named model). `effort` None or ""
    means the model's own default effort. Raises `ChoiceRefused` with the
    sentence the HUD shows. Only the HUD's route reaches this — no tool does.
    """
    try:
        provider = ProviderName(provider)
    except (TypeError, ValueError):
        raise ChoiceRefused(f"{provider!r} is not a provider (claude or codex)") from None
    if provider not in SETTABLE:
        raise ChoiceRefused("the OpenRouter default is the Model picker's (POST /model); "
                            "this sets Claude's and Codex's")
    if model is not None and not isinstance(model, str):
        raise ChoiceRefused("model must be a string ('' resets to the built-in default)")
    if effort is not None and not isinstance(effort, str):
        raise ChoiceRefused("effort must be a string or null")
    model = (model or "").strip()
    effort = (effort or "").strip().lower() or None
    if not model:
        if effort is not None:
            raise ChoiceRefused("a reset takes no effort: the built-in default brings its own")
    elif model not in _cli(provider):
        raise ChoiceRefused(
            f"{model} is not a {LABELS[provider]} model Jarvis knows "
            f"(it knows {', '.join(_cli(provider))})")
    elif effort is not None:
        ladder = efforts_of(provider, model) or ()
        if effort not in _vocabulary(provider):
            raise ChoiceRefused(f"{effort!r} is not a reasoning effort")
        if not ladder:
            raise ChoiceRefused(f"{model} has no reasoning effort to set")
        if effort not in ladder:
            raise ChoiceRefused(f"{model} does not offer {effort!r} (it offers {', '.join(ladder)})")
    with _defaults_lock:
        data = {k: v for k, v in _read_defaults().items() if k in {p.value for p in SETTABLE}}
        if not model:
            if provider.value in data:
                data.pop(provider.value)
                _save_defaults(data)
            return None
        data[provider.value] = {"model": model, "effort": effort}
        _save_defaults(data)
    return model, effort


def _stale_note(provider: ProviderName) -> str:
    """A stored default naming a model Jarvis no longer knows, or an effort
    that is not one: said, not sent."""
    if provider not in SETTABLE:
        return ""
    raw = _stored_raw(provider)
    if raw is None:
        return ""
    model, effort = raw
    if model not in _cli(provider):
        fallback = "built-in default" if provider == ProviderName.CLAUDE else "routing default"
        return (f"the HUD default {model} is not a {LABELS[provider]} model Jarvis knows "
                f"any more; using the {fallback}")
    if _bad_effort(effort):
        return (f"the HUD default's effort {effort!r} is not a reasoning effort; "
                f"{model} runs at its default effort")
    if _cli(provider)[model].get("available", True) is False:
        return (f"the HUD default {model} is no longer in your {LABELS[provider]} account's "
                "model list; it is kept, but a turn on it may be refused")
    return ""


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
        if effort not in _vocabulary(provider):
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
        # `available: false` is a Codex model the account's catalog no longer
        # lists: kept, so what names it stays valid, but not offered anew.
        rows.append({"id": model_id, "name": entry["name"], "efforts": list(entry["efforts"]),
                     "vision": entry["vision"], "available": entry.get("available", True) is not False,
                     "default_effort": default_effort(provider, model_id)})
    return rows


def describe() -> dict[str, Any]:
    """`GET /thread-models`: per provider, its default, where that default
    comes from, and the models to offer.

    Each provider's `default`, `default_source` and `hud_default` come from
    one read of the HUD's defaults, so a change landing mid-listing cannot
    make them disagree.
    """
    result = {}
    for provider in ProviderName:
        notes = []
        stored = hud_default(provider)
        try:
            model, effort = _choice(provider, stored)
        except Exception as exc:  # a broken routing file must not blank the chip
            model, effort = None, None
            notes.append(f"{type(exc).__name__}: {exc}")
        builtin = None
        if provider in SETTABLE:
            try:
                b_model, b_effort = builtin_choice(provider)
                builtin = {"model": b_model, "effort": b_effort}
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"
                if text not in notes:
                    notes.append(text)
            stale = _stale_note(provider)
            if stale:
                notes.append(stale)
        if provider == ProviderName.FAST:
            source = default_source(provider)
        else:
            source = "hud" if stored else ("built-in" if provider == ProviderName.CLAUDE else "routing")
        rows = provider_models(provider)
        for row in rows:
            row.setdefault("default_effort", default_effort(provider, row["id"]))
        result[provider.value] = {"label": LABELS[provider], "default": model,
                                  "default_effort": effort, "models": rows,
                                  "note": "; ".join(notes),
                                  "default_source": source,
                                  # Claude and Codex only: the HUD's stored
                                  # choice (effort null = the model's own), and
                                  # what "Reset to built-in default" returns to.
                                  "settable": provider in SETTABLE,
                                  "hud_default": ({"model": stored[0], "effort": stored[1]}
                                                  if stored else None),
                                  "builtin": builtin,
                                  "profiles": list(PROFILES[provider]),
                                  "always_ask": TAKES_ALWAYS_ASK[provider]}
    return {"providers": result, "effort_default": DEFAULT_EFFORT}
