"""The agent loop.

This is the whole idea, and it is deliberately small: ask the model, run any
tools it asked for, hand the results back, repeat until it stops asking. Read
`run_turn` top to bottom and you know how every agent works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, context, llm, models, runtime, tools


def _image_url(image) -> str:
    """A data: URL from either image shape run_turn accepts."""
    if isinstance(image, dict):
        return f"data:{image.get('mime') or 'image/png'};base64,{image.get('b64', '')}"
    return f"data:image/png;base64,{image}"


@dataclass
class Turn:
    """What one user message cost and did."""

    text: str = ""
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    steps: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    stopped_early: bool = False
    cancelled: bool = False
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
        should_stop: Callable[[], bool] | None = None,
        depth: int = 0,
    ):
        self.model = model or config.TIERS["orchestrator"]
        self.tool_specs = tools.specs(tool_names)
        self.max_steps = max_steps
        self.approve = approve
        self.on_event = on_event or (lambda kind, data: None)
        # Asked between steps only — see run_turn. A surface that can start a
        # turn should be able to abandon one.
        self.should_stop = should_stop or (lambda: False)
        self.policy = policy or context.ContextPolicy()
        self._base_system = system
        # How many sub-agents deep this one is; bound into runtime so a child
        # can refuse to spawn past MAX_DEPTH.
        self.depth = depth
        # This agent's working plan. Owned per-agent rather than globally: two
        # agents can be running at once (a workflow thread, the face's request
        # threads), and they must not share a checklist.
        self.plan: dict[str, str] = {"text": ""}
        # Agents with skill tools get the always-visible skills index appended
        # to their system message; agents without them (designer, benches)
        # must not see references to tools they cannot call. Same rule for the
        # plan block — an agent with no plan_write must not be told to use it.
        names = {spec["function"]["name"] for spec in self.tool_specs}
        self._has_skills = "skill_read" in names
        self._has_plan = "plan_write" in names
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    def set_model(self, name: str) -> str:
        """Point this agent at another model from the roster. Returns a report.

        Effective on the *next step*, including the rest of the turn this was
        called in — so when the switch comes from a tool call, the reply
        confirming it is already spoken by the new model, which is the fastest
        possible way to hear whether it worked.

        Scoped to this instance, never `config.TIERS`. Mutating the tier dict
        would silently re-route compaction summaries, `delegate()`, and any
        workflow started afterwards — agents nobody is watching.

        Raises ValueError if the target is off-roster or cannot call tools.
        """
        alias, entry = models.resolve(name)  # ValueError names the roster
        target = entry["id"]
        if target == self.model:
            return f"Already running {target}."

        caps = models.capabilities(target)
        # Only refuse on a *positive* "no tools" — an unknown capability means
        # the catalog was unreachable, and that must not veto the owner.
        if caps and not caps["tools"]:
            raise ValueError(
                f"{target} does not support tool calling, so it cannot run the "
                "agent loop. It would stop using tools rather than fail loudly."
            )

        notes: list[str] = []
        # The retroactive half of the vision problem: images already in the
        # transcript go out with the next request too, so a text-only target
        # needs them gone, not just avoided. evict_images swaps each for a
        # placeholder — it never deletes a message, so invariant 1 holds.
        if caps and not caps["vision"]:
            evicted = context.evict_images(self.messages, context.ContextPolicy(keep_images=0))
            if evicted:
                notes.append(f"dropped {evicted} image(s) from context — {alias} is text-only")
            else:
                notes.append(f"{alias} is text-only; screenshots will not work")

        previous = self.model
        self.model = target
        if entry.get("note"):
            notes.append(entry["note"])

        report = f"Switched from {previous} to {target}."
        return report + (" (" + "; ".join(notes) + ")" if notes else "")

    def _refresh_system(self) -> None:
        """Rebuild messages[0] from source: base prompt + skills index + plan.

        Called at the top of every *step*, not just every turn. The plan is the
        one thing that has to stay visible on step 40 of a long run, and a turn
        that runs 40 steps only passes the top of run_turn once.

        Safe by construction: index 0 is re-sent with every request anyway, and
        neither pruning nor compaction ever touches it.
        """
        blocks = []
        if self._has_skills:
            blocks.append(tools.skills.index())
        if self._has_plan:
            blocks.append(tools.plan.block())
        extra = "\n\n".join(b for b in blocks if b)
        self.messages[0]["content"] = self._base_system + ("\n\n" + extra if extra else "")

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

    def run_turn(self, user_input: str, images: list | None = None) -> Turn:
        """`images`: pictures supplied by the owner (a whiteboard sketch, a
        screenshot, an attached photo), attached to this user message so a
        vision model sees them alongside the text. Each entry is a base64 PNG
        string, or {"b64": ..., "mime": ...} for other image types. Same
        multimodal shape the context manager already evicts by age."""
        # Bind this agent's per-run state where the tools can reach it. Done
        # here rather than in __init__ because ContextVars are per-thread and
        # the thread that constructs an agent is not always the one that runs
        # it — the face keeps one persistent agent and drives it from whichever
        # request thread took the turn.
        runtime.bind(
            plan=self.plan,
            approve=self.approve if self.approve is not None else (lambda *a, **k: False),
            should_stop=self.should_stop,
            depth=self.depth,
            tool_names={spec["function"]["name"] for spec in self.tool_specs},
        )
        self._refresh_system()
        if images:
            self.messages.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_input}]
                    + [
                        {"type": "image_url", "image_url": {"url": _image_url(img)}}
                        for img in images
                    ],
                }
            )
        else:
            self.messages.append({"role": "user", "content": user_input})
        turn = Turn()

        for step in range(self.max_steps):
            # Cancellation is checked *here and nowhere else*. Between steps
            # the transcript is always whole — either nothing is pending, or
            # every assistant tool_call already has its result behind it.
            # Bailing out inside the tool loop would leave a tool_call with no
            # result, and the next request would 400 (invariant 3).
            if self.should_stop():
                turn.cancelled = True
                self.on_event("cancelled", step)
                return turn

            # Re-render the plan into messages[0] before every request, not
            # just once a turn: a long turn is exactly the case the plan exists
            # for, and a plan written at step 3 has to still be there at step 40.
            self._refresh_system()

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

            # Distinct from "text": this is what the model said on its way to
            # calling a tool, so a surface can show it without mistaking it
            # for the finished reply.
            if reply.text:
                self.on_event("interim_text", reply.text)

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
