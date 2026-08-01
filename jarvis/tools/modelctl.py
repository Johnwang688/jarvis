"""Model switching for the running orchestrator ("Jarvis, switch to Opus").

Built on the voicectl pattern: the live agent is bound here by whichever
surface owns it, so the tool has no import edge into the face or the CLI
(face → tools is the allowed direction, not the reverse). The face registers
on_change() to broadcast the new model to the window, so the CORE readout,
the spoken command, and `/model` at the CLI all agree.

Not `dangerous=True`. It cannot bypass the approval gate — `permissions.gate`
wraps the approver no matter which model is running — and gating it would put
an authorization card in front of something the owner just said out loud. The
containment is `config.SWITCHABLE`: an injected turn can only reach models the
owner already vetted, and every switch is announced to the window.

Only the two full-tool surfaces (chat, face) register these. Workflow agents
get SAFE_TOOLS and the designer/benches get explicit lists, so a background
agent cannot re-point itself.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable

from .. import models
from . import tool

_agent: Any = None
_on_change: Callable[[dict], None] | None = None


def bind(agent: Any) -> None:
    """Hand the tool the agent it is allowed to re-point. Last one wins."""
    global _agent
    _agent = agent


def current() -> str | None:
    """The live orchestrator's model id, or None if nothing is bound yet."""
    return getattr(_agent, "model", None)


def on_change(callback: Callable[[dict], None]) -> None:
    global _on_change
    _on_change = callback


def switch(name: str) -> str:
    """Resolve, validate, and apply. Raises ValueError with a usable message."""
    if _agent is None:
        raise ValueError("No live agent is bound, so there is nothing to switch.")
    report = _agent.set_model(name)
    if _on_change is not None:
        try:
            _on_change({"model": _agent.model, "report": report})
        except Exception:
            pass
    return report


@tool
def list_models() -> str:
    """List the models Jarvis can switch to, with their capabilities.

    Use before switching, or when the owner asks what he can run on.
    """
    return f"Currently running: {current() or 'unknown'}\n\n{models.menu()}"


@tool
def set_model(
    name: Annotated[str, "Roster alias (e.g. 'opus', 'grok', 'kimi', 'default') or full OpenRouter id"],
) -> str:
    """Switch the model running this conversation, keeping the conversation.

    Use when the owner says "switch to X", "try this on X", or asks for a
    smarter/cheaper model. The change takes effect immediately — your very
    next step runs on the new model — and lasts until the process restarts.
    Only models on the owner's roster are allowed; call list_models to see it.
    """
    return switch(name)
