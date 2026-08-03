"""The agent loop.

This is the whole idea, and it is deliberately small: ask the model, run any
tools it asked for, hand the results back, repeat until it stops asking. Read
`run_turn` top to bottom and you know how every agent works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, context, llm, runtime, sessions, tools
from .tools import contextctl


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
        # The conversation surfaces (HUD, chat, ask) take this default; every
        # other agent sets its own. 12 was too tight for real work: a
        # self-improve turn spends most of it on checkpoint + edit + test runs
        # before any misstep, and running out mid-task is expensive — the work
        # is done but unreported, and the owner sees a bare stop notice.
        max_steps: int = 30,
        approve: Callable[[tools.Tool, dict], bool] | None = None,
        on_event: Callable[[str, Any], None] | None = None,
        policy: context.ContextPolicy | None = None,
        should_stop: Callable[[], bool] | None = None,
        session: sessions.Session | None = None,
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
        # plan block — an agent with no plan_write must not be told to use it —
        # and for the recent-sessions index, whose whole point is to tell the
        # model to call session_summary.
        names = {spec["function"]["name"] for spec in self.tool_specs}
        self._has_skills = "skill_read" in names
        self._has_plan = "plan_write" in names
        self._has_sessions = "session_summary" in names
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        # A bound session makes this conversation durable: its saved transcript
        # is restored here, and every turn is written back after it completes.
        # The system message is *not* restored — it is rebuilt above, so a
        # resumed conversation gets today's prompt and today's indexes.
        self.session = session
        if session is not None:
            self.messages.extend(session.restore_messages())

    def _refresh_system(self) -> None:
        """Rebuild messages[0] from source: base prompt + skills index + plan
        + recent-session titles.

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
        if self._has_sessions:
            blocks.append(sessions.index(current=self.session.id if self.session else None))
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
        """One user message through the loop, saved to the session if bound."""
        if not images and self._is_compact_request(user_input):
            self._refresh_system()
            self.messages.append({"role": "user", "content": user_input})
            active = contextctl.ActiveContext(
                self.messages, self.policy, self._summarize, self.on_event
            )
            token = contextctl.bind(active)
            try:
                compacted = contextctl.compact_now()
            finally:
                contextctl.reset(token)
            turn = Turn(text=compacted)
            self.on_event("text", turn.text)
        else:
            turn = self._run_turn(user_input, images)
        if self.session is not None:
            try:
                self.session.record(user_input, turn, self.messages)
            except Exception as exc:  # persistence must never break a turn
                self.on_event("interim_text", f"[session not saved: {exc}]")
        return turn

    @staticmethod
    def _is_compact_request(user_input: str) -> bool:
        normalized = " ".join(user_input.strip().lower().split())
        return normalized in {
            "/compact",
            "compact context",
            "shorten the context",
            "summarize so we can continue",
        }

    def _run_turn(self, user_input: str, images: list | None = None) -> Turn:
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

                active = contextctl.ActiveContext(
                    self.messages, self.policy, self._summarize, self.on_event
                )
                token = contextctl.bind(active)
                try:
                    result = tools.dispatch(name, arguments, approve=self.approve)
                finally:
                    contextctl.reset(token)

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
        # The transcript has to say so too. Without this it ends on a tail of
        # tool results with no sign the loop was cut off, so the next turn's
        # model cannot know it ran out and will invent a reason for its own
        # silence — asked why he stopped, Jarvis once answered "there isn't a
        # twelve-step limit" while the window showed exactly that message.
        # Safe to append here: the loop only exits at a step boundary, so
        # every tool_call already has its result (invariant 3).
        self.messages.append({"role": "assistant", "content": turn.text})
        return turn


def delegate(prompt: str, tier: str = "worker", system: str | None = None) -> str:
    """Run a single-shot, tool-free request on a cheaper tier.

    This is the cost lever: the orchestrator stays on a capable model and hands
    bulk text work (drafting, summarizing, extracting) down to a cheap one.
    """
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})
    return llm.chat(config.TIERS[tier], messages, temperature=0.4).text
