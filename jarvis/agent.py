"""The agent loop.

This is the whole idea, and it is deliberately small: ask the model, run any
tools it asked for, hand the results back, repeat until it stops asking. Read
`run_turn` top to bottom and you know how every agent works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, context, llm, tools


@dataclass
class Turn:
    """What one user message cost and did."""

    text: str = ""
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    steps: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    stopped_early: bool = False
    tokens_saved: int = 0


class Agent:
    def __init__(
        self,
        model: str | None = None,
        system: str = config.SYSTEM_PROMPT,
        tool_names: list[str] | None = None,
        max_steps: int = 12,
        approve: Callable[[tools.Tool, dict], bool] | None = None,
        on_event: Callable[[str, Any], None] | None = None,
        policy: context.ContextPolicy | None = None,
    ):
        self.model = model or config.TIERS["orchestrator"]
        self.tool_specs = tools.specs(tool_names)
        self.max_steps = max_steps
        self.approve = approve
        self.on_event = on_event or (lambda kind, data: None)
        self.policy = policy or context.ContextPolicy()
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    def _summarize(self, transcript: str) -> str:
        """Compress old history using the cheap tier — this is bulk text work."""
        return llm.chat(
            config.TIERS["cheap"],
            [
                {
                    "role": "user",
                    "content": (
                        "Summarize this agent transcript for your own future reference. "
                        "Keep decisions made, facts established, files or URLs touched, "
                        "and anything still outstanding. Drop pleasantries and dead ends.\n\n"
                        + transcript
                    ),
                }
            ],
            max_tokens=1024,
        ).text

    def run_turn(self, user_input: str) -> Turn:
        self.messages.append({"role": "user", "content": user_input})
        turn = Turn()

        for step in range(self.max_steps):
            reply = llm.chat(self.model, self.messages, tools=self.tool_specs)
            turn.steps = step + 1
            turn.cost_usd += reply.cost_usd
            turn.latency_s += reply.latency_s

            # Keep the assistant turn verbatim — the tool_call ids in it are what
            # the next round of tool results are matched against.
            self.messages.append(
                {
                    "role": "assistant",
                    "content": reply.message.get("content"),
                    **({"tool_calls": reply.tool_calls} if reply.tool_calls else {}),
                }
            )

            if not reply.tool_calls:
                turn.text = reply.text
                self.on_event("text", reply.text)
                return turn

            if reply.text:
                self.on_event("text", reply.text)

            # All results from one assistant turn go back together, each keyed to
            # its call id. Splitting them across messages teaches the model to
            # stop making parallel calls.
            for call in reply.tool_calls:
                name = call.get("function", {}).get("name", "")
                arguments = call.get("function", {}).get("arguments", "") or "{}"
                self.on_event("tool_start", (name, arguments))

                result = tools.dispatch(name, arguments, approve=self.approve)

                turn.tool_calls.append((name, arguments))
                self.on_event("tool_end", (name, result.text))
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": result.text,
                    }
                )
                # A tool message can only carry a string, so an image rides
                # along in its own user message right behind it.
                if result.image_b64:
                    self.messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{result.mime};base64,{result.image_b64}"
                                    },
                                }
                            ],
                        }
                    )

            stats = context.manage(self.messages, self.policy, summarize=self._summarize)
            if stats:
                turn.tokens_saved += stats.saved
                self.on_event("context", stats)

        turn.stopped_early = True
        turn.text = f"[stopped after {self.max_steps} steps without finishing]"
        return turn


def delegate(prompt: str, tier: str = "worker", system: str | None = None) -> str:
    """Run a single-shot, tool-free request on a cheaper tier.

    This is the cost lever: the orchestrator stays on a capable model and hands
    bulk text work (drafting, summarizing, extracting) down to a cheap one.
    """
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})
    return llm.chat(config.TIERS[tier], messages, temperature=0.4).text
