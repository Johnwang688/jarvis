"""The one command registry every surface reads (slash plan §3, S1).

Discord registers it (`discord/commands.py` turns it into Discord's payload),
and the HUD's `/` menu will read the same table later (plan §5). Two rules hold
it together, and both are about who can change what the owner is offered:

* **The registry is static code.** No runtime data — no skill, project, task
  or approval — ever enters the *registered* payload. Skills, projects, tasks
  and codes reach the owner only as autocomplete suggestions at read time,
  through `completions()`. A skill an agent wrote with `skill_write` can be
  *suggested*; it can never become a command, because registration is a pure
  function of this file.
* **Autocomplete is a suggestion, never a validation.** A hand-typed value
  bypasses it, so every handler re-checks what it is given against the live
  stores (an unknown or archived project, a `jarvis-only` skill, a code asked
  in another chat). `completions()` only narrows; it authorizes nothing.

Nothing here talks to Discord or to a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# Where a command may run. A Discord channel is one of these places
# (`DiscordRouter._locate`); "other" is a guild channel Jarvis does not own.
DM, PROJECT, TASK, OTHER = "dm", "project", "task", "other"
JARVIS_PLACES = frozenset({DM, PROJECT, TASK})

# Discord: a choice name is at most 100 characters, and autocomplete returns
# at most 25 of them.
CHOICE_NAME_MAX = 100
MAX_CHOICES = 25

# The fast path injects a skill body into the turn; a body larger than this is
# refused rather than truncated, because half a set of instructions is a
# different set of instructions.
SKILL_BODY_MAX = 8000


@dataclass(frozen=True)
class Opt:
    """One option. `complete` names a completer kind (see `completions`)."""
    name: str
    kind: str                       # "string" | "integer" | "boolean"
    description: str
    required: bool = False
    complete: str | None = None
    choices: tuple[tuple[str, str], ...] = ()
    max_length: int | None = None


@dataclass(frozen=True)
class Cmd:
    name: str
    description: str
    options: tuple[Opt, ...] = ()
    places: frozenset[str] = JARVIS_PLACES
    surfaces: frozenset[str] = frozenset({"discord"})
    subcommands: tuple["Cmd", ...] = ()
    ephemeral: bool = False         # lookups answer privately; actions are public


_TASK_OPT = Opt("task", "string", "Task id (defaults to this thread's task)", complete="task")
_CODE = Opt("code", "string", "Approval code (optional when only one is open here)",
            complete="code")
_PROVIDER = (("claude", "claude"), ("codex", "codex"))

REGISTRY: tuple[Cmd, ...] = (
    Cmd("task", "Open a task (leave the brief empty for a multi-line box)", (
        Opt("brief", "string", "What to do", max_length=4000),
        Opt("project", "string", "Project (defaults to this channel's, or the Inbox)",
            complete="project"),
        Opt("skill", "string", "A skill the task should use", complete="skill"),
        Opt("provider", "string", "Orchestrator provider", choices=_PROVIDER),
    ), surfaces=frozenset({"discord", "hud"})),
    Cmd("status", "Show this task's card, or the active tasks", (
        Opt("task", "string", "Task id", complete="task"),
    ), surfaces=frozenset({"discord", "hud"}), ephemeral=True),
    Cmd("cancel", "Cancel a task (or withdraw a proposal inside its grace window)",
        (_TASK_OPT,), surfaces=frozenset({"discord", "hud"})),
    Cmd("steer", "Steer a running task at its next step", (
        Opt("text", "string", "What to tell the task", required=True, max_length=4000),
        _TASK_OPT,
    ), surfaces=frozenset({"discord", "hud"})),
    Cmd("answer", "Answer a task's open question", (
        Opt("text", "string", "Your answer", required=True, complete="answer",
            max_length=4000),
        Opt("question", "string", "Which question (defaults to the first blocking one)",
            complete="question"),
        _TASK_OPT,
    )),
    # Never on the HUD: its approval surface is the card, and a typed `/yes`
    # plus Enter would be a keyboard default on an authorization (plan §5).
    Cmd("yes", "Approve the open request asked in this chat", (_CODE,)),
    Cmd("no", "Deny the open request asked in this chat", (_CODE,)),
    Cmd("always", "Approve, and stop asking about this one", (
        Opt("code", "string", "Approval code (optional when only one is open here)",
            complete="always_code"),
    )),
    Cmd("resume", "Resume a blocked task", (
        Opt("task", "string", "Task id (defaults to this thread's task)",
            complete="blocked_task"),
        Opt("provider", "string", "Restart the orchestrator on this provider",
            choices=_PROVIDER),
    )),
    Cmd("skill", "Run one of your skills", (
        Opt("name", "string", "Skill", required=True, complete="skill"),
        Opt("request", "string", "What you want from it", max_length=4000),
    ), surfaces=frozenset({"discord", "hud"})),
    # Only `list` in S1. `new`, `link`, `unlink` and `channel` arrive with B1
    # and B2; an unregistered subcommand is better than a stub that refuses.
    Cmd("project", "Projects", subcommands=(
        Cmd("list", "List your projects", ephemeral=True),
    )),
)

BY_NAME = {cmd.name: cmd for cmd in REGISTRY}


def find(name: str, sub: str | None = None) -> Cmd | None:
    cmd = BY_NAME.get(name)
    if cmd is None or sub is None:
        return cmd if cmd is not None and not cmd.subcommands else None
    return next((s for s in cmd.subcommands if s.name == sub), None)


def for_surface(surface: str) -> list[Cmd]:
    return [cmd for cmd in REGISTRY if surface in cmd.surfaces]


# -- skills ------------------------------------------------------------------


class SkillRefused(ValueError):
    """A skill name that cannot be run from here, with a sentence why."""


def _catalogue() -> dict[str, tuple[str, bool, Any]]:
    """name -> (description, jarvis_only, path) for the repo's skills (D8)."""
    from jarvis.tools import skills

    out = {}
    for name, path in skills._paths().items():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = skills.metadata(text)
        only = str(meta.get("jarvis-only", "")).strip().lower() == "true"
        out[name] = (meta.get("description", ""), only, path)
    return out


def invocable_skills() -> list[tuple[str, str]]:
    """(name, description) for every repo skill a v2 thread can run.

    `jarvis-only` skills are excluded: each needs a v1-loop tool the v2 fast
    path refuses, or a protected file, so offering one would offer a failure.
    """
    return [(name, desc) for name, (desc, only, _) in _catalogue().items() if not only]


def check_skill(name: str) -> str:
    """The canonical name, or SkillRefused with the sentence to show."""
    name = (name or "").strip()
    entry = _catalogue().get(name)
    if entry is None:
        raise SkillRefused(f"I don't have a skill called `{_clean(name, 60)}`.")
    if entry[1]:
        raise SkillRefused(f"`{name}` only runs in the v1 loop.")
    return name


def skill_body(name: str, limit: int = SKILL_BODY_MAX) -> str:
    """The instructions of an invocable skill, refused above `limit`."""
    from jarvis.tools import skills

    name = check_skill(name)
    _, body = skills._parse(_catalogue()[name][2].read_text(encoding="utf-8"))
    if len(body) > limit:
        raise SkillRefused(f"`{name}` is {len(body)} characters, over the {limit}-character "
                           "limit for running it here.")
    return body


def skill_directive(name: str, text: str = "") -> str:
    """What Claude and Codex receive: they have the skill installed natively,
    so they are told to use it rather than handed its body (plan §2)."""
    head = f'Use the "{name}" skill for this request.'
    text = (text or "").strip()
    return f"{head}\n\n{text}" if text else head


# -- completers ----------------------------------------------------------------


@dataclass
class Context:
    """What a completer may read. Stores and the open approvals, nothing else."""
    stores: Any
    project: Any = None             # the place's project, if any
    task: Any = None                # the place's task, if any
    pending: list = field(default_factory=list)   # approvals asked in *this* chat
    options: dict = field(default_factory=dict)   # the other options typed so far


def _clean(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _filter(rows: Iterable[tuple[str, str]], prefix: str) -> list[tuple[str, str]]:
    """Case-insensitive prefix matches first, then substring; at most 25."""
    rows = list(rows)
    needle = (prefix or "").strip().casefold()
    if not needle:
        return rows[:MAX_CHOICES]
    first = [r for r in rows if r[0].casefold().startswith(needle)
             or r[1].casefold().startswith(needle)]
    rest = [r for r in rows if r not in first and (needle in r[0].casefold()
                                                   or needle in r[1].casefold())]
    return (first + rest)[:MAX_CHOICES]


def _task_row(task) -> tuple[str, str]:
    return _clean(f"{task.id} · {task.state.value} — {task.brief}", CHOICE_NAME_MAX), task.id


def _live_project_ids(stores) -> set[str]:
    return {p.id for p in stores.projects.list() if not p.archived}


def completions(kind: str, prefix: str, ctx: Context) -> list[tuple[str, str]]:
    """(name, value) suggestions for one option. Reads only; never writes."""
    from .model import TERMINAL_STATES, TaskState

    stores = ctx.stores
    if kind == "project":
        rows = [(_clean(p.name, CHOICE_NAME_MAX), p.id) for p in stores.projects.list()
                if not p.archived]
    elif kind in ("task", "blocked_task"):
        live = _live_project_ids(stores)
        tasks = [t for t in stores.tasks.list() if t.project_id in live]
        if kind == "blocked_task":
            tasks = [t for t in tasks if t.state == TaskState.BLOCKED]
        else:
            tasks = [t for t in tasks if t.state not in TERMINAL_STATES]
        if ctx.project is not None and not getattr(ctx.project, "inbox", False):
            tasks = [t for t in tasks if t.project_id == ctx.project.id] or tasks
        rows = [_task_row(t) for t in tasks]
    elif kind in ("code", "always_code"):
        requests = [r for r in ctx.pending
                    if kind == "code" or getattr(r, "allowlistable", True)]
        rows = []
        for r in requests:
            what = r.command if isinstance(r.command, str) and r.command else r.tool
            rows.append((_clean(f"{r.code} · {r.tool}: {what}", CHOICE_NAME_MAX), r.code))
    elif kind == "skill":
        rows = [(_clean(f"{name} — {desc}" if desc else name, CHOICE_NAME_MAX), name)
                for name, desc in invocable_skills()]
    elif kind in ("question", "answer"):
        task = _option_task(ctx)
        if task is None:
            return []
        questions = open_questions(task)
        if kind == "question":
            rows = [(_clean(f"{i}: {q.text}", CHOICE_NAME_MAX), str(i)) for i, q in questions]
        else:
            wanted = str(ctx.options.get("question", "")).strip()
            chosen = [q for i, q in questions if str(i) == wanted] if wanted else \
                [q for _, q in questions[:1]]
            rows = [(_clean(o, CHOICE_NAME_MAX), _clean(o, CHOICE_NAME_MAX))
                    for q in chosen for o in q.options]
    else:
        return []
    return _filter(rows, prefix)


def open_questions(task) -> list[tuple[int, Any]]:
    """(index, question) for each blocking question still unanswered — the
    runner's own `Spec.blocked_on` rule, with the index `answer_question` takes.
    Only a CLARIFYING task is waiting on one."""
    from .model import TaskState

    if task is None or task.state != TaskState.CLARIFYING:
        return []
    return [(i, q) for i, q in enumerate(task.spec.questions)
            if q.blocking and q.answer is None]


def _option_task(ctx: Context):
    wanted = str(ctx.options.get("task", "") or "").strip()
    if wanted:
        task = ctx.stores.tasks.get(wanted)
        return task if task is not None and task.project_id in _live_project_ids(ctx.stores) \
            else None
    return ctx.task


def describe(surface: str = "discord") -> list[dict]:
    """The registry as plain JSON (the HUD menu reads this later)."""
    def opt(o: Opt) -> dict:
        return {"name": o.name, "kind": o.kind, "description": o.description,
                "required": o.required, "complete": o.complete,
                "choices": [list(c) for c in o.choices]}

    def cmd(c: Cmd) -> dict:
        return {"name": c.name, "description": c.description,
                "options": [opt(o) for o in c.options], "places": sorted(c.places),
                "subcommands": [cmd(s) for s in c.subcommands], "ephemeral": c.ephemeral}

    return [cmd(c) for c in for_surface(surface)]


__all__ = ["Opt", "Cmd", "REGISTRY", "Context", "completions", "invocable_skills",
           "check_skill", "skill_body", "skill_directive", "SkillRefused", "find",
           "for_surface", "describe"]
