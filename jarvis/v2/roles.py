"""Role briefs and the structured-output contracts the runner parses (§10.2).

A role is a brief, not a model: system-prompt append, tool allowance, turn cap,
and — for the roles the runner has to *read* rather than relay — an exact JSON
shape. Three rules decide everything here.

**The separation is stated, not hoped for.** The v1 agent-bench finding was
that every non-Luna model "did it inline instead of delegating"; §10.2 makes
that structural by giving the orchestrator no edit tool at all. The prose says
the same thing twice over because a provider that cannot enforce a tool
allowance (Codex, below) has only the prose.

**`allowed_tools` means different things to different providers**, so a role
resolves it per provider (`RoleBrief.tools_for`). On Claude it maps to the
SDK's `tools` option — what exists, not what runs unasked (WP3-notes). Codex
refuses a non-None `allowed_tools` outright (`codex_config`), so there the
allowance is `None` and the role's confinement is its prose plus Codex's own
review. A role that silently sent a list to Codex would raise `BriefRefused`
and block a task for a reason that has nothing to do with the task.

**A parse failure is never a guess.** `parse_block` returns `None` and the
runner re-asks once with the shape spelled out; a second failure blocks the
task. Reading a spec out of prose is how "done" becomes unpinned — the v1 VEX
lesson (§10.1) — so the shape is a gate, not a preference.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable

from .model import ProviderName, Role

# Claude Code's own tool names; the SDK's `tools` option takes these verbatim.
READ_ONLY_TOOLS = ("Read", "Grep", "Glob", "NotebookRead", "TodoWrite")
REVIEW_TOOLS = READ_ONLY_TOOLS + ("Bash",)      # a reviewer runs tests; it never edits
RESEARCH_TOOLS = READ_ONLY_TOOLS + ("WebFetch", "WebSearch")

MAX_TURNS = {"orchestrator": 12, "implementer": 60, "reviewer": 24, "researcher": 24}


@dataclass(frozen=True)
class RoleBrief:
    """One §10.2 row. `allowed_tools=None` means the provider's full native set."""

    role: Role
    system_append: str
    allowed_tools: tuple[str, ...] | None = None
    max_turns: int | None = None
    shape: str | None = None                 # the JSON contract the runner parses, if any

    def tools_for(self, provider: ProviderName | str) -> list[str] | None:
        """Honour each provider's meaning of an allowance; never refuse a brief for it."""
        if self.allowed_tools is None:
            return None
        if ProviderName(provider) == ProviderName.CODEX:
            # Codex cannot enforce an allowlist (codex_config refuses one). The
            # role's confinement here is its prose; sending the list would only
            # trade a soft boundary for a hard BriefRefused.
            return None
        return list(self.allowed_tools)


_ORCHESTRATOR = """
You are the ORCHESTRATOR of one Jarvis task. You own the spec, the plan, the
dispatch of work to an implementer, and the final report. You are the only
role that talks to the owner.

You NEVER implement. You do not edit files, run builds, or write code, even
when the change is one line and you can see exactly what it should be — a
separate implementer worker does that in the task worktree, and you instruct
it one step at a time. Your file reads are for understanding the request well
enough to specify it, nothing more.

Every reply you send is either plain text or a single fenced ```json block, as
the message you are answering asks for. When a JSON block is asked for, reply
with the block and nothing else: no preamble, no commentary after it.

Rules for the SPEC:
- Acceptance criteria are written BEFORE any implementation. A task whose
  acceptance is unstated has no definition of done.
- A question is `blocking` only if two reasonable answers lead to materially
  different work. Everything else is assumable: mark it `blocking: false` and
  put the answer you are assuming in `assumed`, so the owner can correct it
  without being asked.
- Do not invent constraints the owner did not state, and do not pad acceptance
  with things nobody asked for.
""".strip()

_IMPLEMENTER = """
You are the IMPLEMENTER on one Jarvis task, working in a dedicated git
worktree. You receive one step at a time from the orchestrator and you do
exactly that step.

Do the work and then say what you did, in plain text: what you changed, which
files, what you ran, and anything you could not do. That text is the only
thing the orchestrator sees — it cannot read your tool output — so a step you
finished silently is a step nobody knows about.

Stay inside the worktree you were started in. Do not push, do not deploy, and
do not widen the step you were given; if the step is wrong or impossible, say
so and stop rather than doing something adjacent.
""".strip()

_REVIEWER = """
You are the REVIEWER on one Jarvis task. You verify the deliverable against
the acceptance criteria you are given, and you NEVER edit anything — not a
typo, not a missing import. A reviewer that fixes what it finds cannot report
it, and the next reviewer inherits an unverified change.

You may read the tree and run tests, builds and linters. Judge only against
the stated acceptance criteria and the plan; a thing you would have done
differently is not a failure.

Reply with a single fenced ```json block and nothing else:
{"pass": true|false, "findings": ["...", "..."]}
Each finding names the criterion it fails and what is missing, specifically
enough that the implementer can act on it without asking you. `pass: true`
means every acceptance criterion is met; `findings` may still carry notes.
""".strip()

_RESEARCHER = """
You are the RESEARCHER on one Jarvis task. You read, browse and summarize, and
you return FINDINGS ONLY: you never edit a file, run a build, or change any
state. Answer in plain text, lead with the answer, and say plainly when the
sources do not settle the question rather than filling the gap.
""".strip()


_CHAT = """
You are Jarvis, talking with the owner directly in one chat thread of a
project, as a full agent working in that project's folder. There is no task
around this conversation: no spec, no plan, no reviewer. The owner is reading
your replies as they arrive and will steer you in the next message.

Do what the owner asks in this thread, and say plainly what you changed, ran
or could not do. Your tool calls pass through the owner's permission rules for
this project; a call that is denied was denied on purpose, so do not retry it
or work around it. Do not push, deploy or send anything outward unless the
owner asked for exactly that in this conversation.
""".strip()


BRIEFS: dict[Role, RoleBrief] = {
    # A chat thread on Claude or Codex (decisions A1). The fast path does not
    # read this — it carries its own prompt — and neither does any task: the
    # runner only asks for the four task roles.
    Role.CHAT: RoleBrief(Role.CHAT, _CHAT, None, None),
    Role.ORCHESTRATOR: RoleBrief(Role.ORCHESTRATOR, _ORCHESTRATOR, READ_ONLY_TOOLS,
                                 MAX_TURNS["orchestrator"]),
    Role.IMPLEMENTER: RoleBrief(Role.IMPLEMENTER, _IMPLEMENTER, None,
                                MAX_TURNS["implementer"]),
    Role.REVIEWER: RoleBrief(Role.REVIEWER, _REVIEWER, REVIEW_TOOLS,
                             MAX_TURNS["reviewer"], shape="review"),
    Role.RESEARCHER: RoleBrief(Role.RESEARCHER, _RESEARCHER, RESEARCH_TOOLS,
                               MAX_TURNS["researcher"]),
}


def brief_for(role: Role | str) -> RoleBrief:
    try:
        return BRIEFS[Role(role)]
    except (KeyError, ValueError) as exc:
        raise KeyError(f"no role brief for {role!r}") from exc


# --- the structured-output contracts ----------------------------------------

SPEC_CONTRACT = """Reply with one fenced ```json block and nothing else:
{"goal": "one sentence",
 "deliverable": "what exists at the end",
 "acceptance": ["criterion", ...],
 "constraints": ["constraint", ...],
 "questions": [{"text": "...", "blocking": true|false,
                "options": ["..."], "assumed": "what you assume if not blocking"}]}
A question is blocking ONLY if two reasonable answers lead to materially
different work; otherwise set blocking false and fill `assumed`."""

PLAN_CONTRACT = """Reply with one fenced ```json block and nothing else:
{"steps": ["step 1", "step 2", ...]}
Numbered work steps in order, each one an instruction an implementer can carry
out on its own. The LAST step is the verification step."""

NEXT_CONTRACT = """Reply with one fenced ```json block and nothing else:
{"next": "the next instruction for the implementer, or null", "done": true|false}
`done: true` (with `next: null`) means every plan step is finished and the work
is ready for review."""

REPORT_CONTRACT = """Reply with one fenced ```json block and nothing else:
{"done": ["what was delivered, one line each"],
 "changed": ["files / branch / deploy URL"],
 "verified": "how, by whom, result",
 "open": ["anything not done, and why"],
 "next": "the one thing the owner should do now, or an empty string"}"""

REVIEW_CONTRACT = """Reply with one fenced ```json block and nothing else:
{"pass": true|false, "findings": ["...", ...]}"""

REASK = ("That reply could not be parsed. Reply with ONLY the fenced json "
         "block described below — no text before it and none after it.\n\n")


_FENCE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n(.*?)```", re.S)


def _last_json_object(text: str) -> dict | None:
    """The LAST fenced block that parses as a JSON object; a bare object as a
    fallback. Prose is never mined for values — a missing block is a failure."""
    if not isinstance(text, str):
        return None
    for body in reversed(_FENCE.findall(text)):
        try:
            value = json.loads(body)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            value = json.loads(stripped)
        except ValueError:
            return None
        if isinstance(value, dict):
            return value
    return None


def _text(value: Any, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise TypeError("expected a string")
    value = value.strip()
    if required and not value:
        raise ValueError("expected a non-empty string")
    return value


def _lines(value: Any, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise TypeError("expected a list of strings")
    out = [_text(item) for item in value]
    if required and not out:
        raise ValueError("expected a non-empty list")
    return out


def _spec(data: dict) -> dict:
    for key in ("goal", "deliverable", "acceptance"):
        if key not in data:
            raise KeyError(key)
    questions = []
    raw = data.get("questions") or []
    if not isinstance(raw, list):
        raise TypeError("questions must be a list")
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("each question must be an object")
        blocking = item.get("blocking")
        if not isinstance(blocking, bool):
            raise TypeError("question.blocking must be a boolean")
        assumed = item.get("assumed")
        questions.append({"text": _text(item.get("text")), "blocking": blocking,
                          "options": _lines(item.get("options")),
                          "assumed": None if assumed in (None, "") else _text(assumed)})
    return {"goal": _text(data["goal"]), "deliverable": _text(data["deliverable"]),
            "acceptance": _lines(data["acceptance"], required=True),
            "constraints": _lines(data.get("constraints")), "questions": questions}


def _plan(data: dict) -> dict:
    return {"steps": _lines(data["steps"], required=True)}


def _next(data: dict) -> dict:
    done = data.get("done")
    if not isinstance(done, bool):
        raise TypeError("done must be a boolean")
    nxt = data.get("next")
    if nxt is not None and not isinstance(nxt, str):
        raise TypeError("next must be a string or null")
    nxt = (nxt or "").strip()
    if not done and not nxt:
        raise ValueError("an unfinished task needs a next instruction")
    return {"next": nxt or None, "done": done}


def _review(data: dict) -> dict:
    passed = data.get("pass")
    if not isinstance(passed, bool):
        raise TypeError("pass must be a boolean")
    findings = _lines(data.get("findings"))
    if not passed and not findings:
        raise ValueError("a failing review must say what failed")
    return {"pass": passed, "findings": findings}


def _report(data: dict) -> dict:
    for key in ("done", "changed", "verified"):
        if key not in data:
            raise KeyError(key)
    return {"done": _lines(data["done"], required=True), "changed": _lines(data["changed"]),
            "verified": _text(data["verified"]), "open": _lines(data.get("open")),
            "next": _text(data.get("next"), required=False)}


SHAPES: dict[str, Callable[[dict], dict]] = {
    "spec": _spec, "plan": _plan, "next": _next, "review": _review, "report": _report,
}

CONTRACTS: dict[str, str] = {
    "spec": SPEC_CONTRACT, "plan": PLAN_CONTRACT, "next": NEXT_CONTRACT,
    "review": REVIEW_CONTRACT, "report": REPORT_CONTRACT,
}


def parse_block(text: str, shape: str) -> dict | None:
    """The last fenced JSON block, validated against `shape`; None on any failure.

    None is the only failure signal: the caller re-asks once and then blocks.
    Nothing here repairs, coerces or infers a missing value.
    """
    if shape not in SHAPES:
        raise KeyError(f"unknown shape {shape!r}")
    data = _last_json_object(text)
    if data is None:
        return None
    try:
        return SHAPES[shape](data)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
