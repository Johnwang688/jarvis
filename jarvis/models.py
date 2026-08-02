"""The switchable model roster: which models the owner may move onto, and
whether the one they asked for can actually run the loop.

Switching is nearly free because `Agent.model` is a plain string read fresh on
every step (`agent.py`), and every model behind OpenRouter speaks the same
chat-completions shape — so there is no reconstruction, no transcript rewrite,
and no reconnect. The work is not the mutation, it is the three things that
make an unchecked mutation a bad idea:

  Policy   — a free-form model string is a way to move the transcript onto any
             model on OpenRouter. The roster (`config.SWITCHABLE`) is the whole
             point; resolution refuses anything not on it.

  Tools    — a model that cannot call tools cannot be the orchestrator. It
             would not error, it would just quietly stop using tools and look
             like Jarvis had gone stupid, which is a much worse failure.

  Vision   — this one is *retroactive*, and it is the trap. Switching to a
             text-only model does not only affect future screenshots: the
             transcript may already carry image parts from cad_render or
             browser_screenshot, and they go out with the very next request.

Capabilities come from `llm.catalog()` (live, cached) rather than a table
here, and that call fails soft — so an unreachable catalog degrades this to
"switch and hope", never to "refuse everything".
"""

from __future__ import annotations

from typing import Any

from . import config, llm

DEFAULT_ALIAS = "default"


def roster() -> dict[str, dict[str, str]]:
    """Every alias the owner can switch to, in menu order.

    The configured orchestrator is always included as `default`, even though
    it is not in `config.SWITCHABLE`. Without it the control would be one-way
    until a restart — and returning to the model the process started on
    cannot itself be a policy violation.
    """
    entries: dict[str, dict] = {
        DEFAULT_ALIAS: {"id": config.TIERS["orchestrator"], "note": "", "providers": []}
    }
    for alias, entry in config.SWITCHABLE.items():
        # A roster entry that just re-names the default would give the menu
        # two ways to say the same thing.
        if entry["id"] != config.TIERS["orchestrator"]:
            entries[alias] = dict(entry)
    return entries


def resolve(name: str) -> tuple[str, dict[str, str]]:
    """`name` (an alias or a full OpenRouter id) -> (alias, entry).

    Raises ValueError naming the roster — the message is read by the model as
    a tool result, so it has to be enough to retry from.
    """
    wanted = (name or "").strip().lower()
    available = roster()
    if not wanted:
        raise ValueError(f"No model named. Available: {', '.join(available)}.")

    for alias, entry in available.items():
        if wanted in (alias, entry["id"].lower(), entry["id"].split("/")[-1].lower()):
            return alias, entry

    raise ValueError(
        f"{name!r} is not on the switchable roster. Available: "
        + ", ".join(f"{alias} ({entry['id']})" for alias, entry in available.items())
        + ". The roster is set by the owner in config.SWITCHABLE; you cannot add to it."
    )


def capabilities(model_id: str) -> dict[str, Any] | None:
    """What OpenRouter says this model can do, or None if we could not find out."""
    return llm.catalog().get(model_id)


def describe(alias: str, entry: dict[str, str]) -> str:
    """One line for a menu: alias, id, and anything the owner should know."""
    line = f"{alias:<8} {entry['id']}"
    caps = capabilities(entry["id"])
    if caps:
        bits = [f"{caps['context_length']:,} ctx"]
        bits.append("vision" if caps["vision"] else "text-only")
        if not caps["tools"]:
            bits.append("NO TOOL CALLING")
        line += f"  [{', '.join(bits)}]"
    if entry.get("providers"):
        line += f"  via {' → '.join(entry['providers'])}"
    if entry.get("note"):
        line += f"  ⚠ {entry['note']}"
    return line


def menu() -> str:
    return "\n".join(describe(alias, entry) for alias, entry in roster().items())
