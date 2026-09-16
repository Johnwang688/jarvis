"""Pure Discord templates. Status prose comes exclusively from the runner record."""
from __future__ import annotations

import json

from ..model import Project, Report, Task


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def status_embed(task: Task, project: Project) -> dict:
    # Identity is metadata; never use brief, spec, plan or report for status.
    status = task.status
    routing = {}
    for decision in status.routing:
        routing[decision.role.value] = decision
    route = " · ".join(
        f"{role}: {d.provider.value} ({d.reason})" for role, d in routing.items())
    seconds = max(0, int(status.elapsed_s))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    values = [
        ("Phase", status.phase.value),
        ("Step", f"{status.step}/{status.steps}"),
        ("Elapsed", f"{hours:02}:{minutes:02}:{seconds:02}"),
        ("Cost", f"${status.cost_usd:.4f} · {status.tokens:,} tokens"),
        ("Last action", " · ".join(x for x in (status.last_tool, status.last_file) if x) or "—"),
        ("Open question", status.open_question or "—"),
        ("Routing", route or "—"),
    ]
    title = _cap(f"{project.name} · Task {task.id}", 256)
    budget = 6000 - len(title) - sum(len(name) for name, _ in values)
    fields = []
    for index, (name, value) in enumerate(values):
        # Reserve one character for each remaining field as empty values are invalid.
        value = _cap(value, min(1024, budget - (len(values) - index - 1)))
        fields.append({"name": name, "value": value, "inline": False})
        budget -= len(value)
    return {"title": title, "fields": fields}


def milestone(kind: str, task: Task, **ctx) -> str:
    """Short notifications; approvals must also include approval_text unabridged."""
    detail = ctx.get("text", task.status.open_question or "")
    templates = {
        "started": f"Started task {task.id}: {ctx.get('text', task.brief)}",
        "question": f"Question (blocking): {detail}" + (
            " Choices: " + " / ".join(ctx["options"]) if ctx.get("options") else ""),
        "approval": f"Approval required for {ctx.get('tool', 'tool')}: {detail} "
                    f"Reply yes/no {ctx.get('code', '')} in this thread.",
        "blocked": f"Blocked: {detail or 'Owner input required.'}",
        "verified": f"Verified: {ctx.get('text', task.report.verified if task.report else 'Checks passed.')}",
        "done": f"Done: task {task.id}. {ctx.get('text', 'Report follows.')}",
        "failed": f"Failed: {detail or 'See the task log for details.'}",
    }
    return _cap(templates[kind], 400)


class ReportText(str):
    """A <=1500-character string; overflow is the full report attachment or None."""

    overflow: str | None

    def __new__(cls, full: str):
        result = super().__new__(cls, _cap(full, 1500))
        result.overflow = full if len(full) > 1500 else None
        return result


def report_text(report: Report) -> ReportText:
    def lines(items):
        return "\n".join(items) or "—"

    return ReportText("\n".join((
        "DONE      " + lines(report.done),
        "CHANGED   " + lines(report.changed),
        "VERIFIED  " + (report.verified or "—"),
        "OPEN      " + lines(report.open),
        "NEXT      " + (report.next or "—"),
        # Report.cost does not encode units; do not invent dollars for token counts.
        "COST      " + (" · ".join(f"{k}: {v:g}" for k, v in report.cost.items()) or "—"),
    )))


def approval_text(tool: str, args, code: str, origin: str) -> str:
    """Never truncate. DiscordRest.post attaches this verbatim when >2000 chars."""
    # A shell command must be visible with literal newlines, not JSON escapes.
    command = args.get("command") if isinstance(args, dict) else args
    parts = [f"Approval required: {tool}", f"Origin: {origin}",
             f"Reply yes/no {code} in this thread."]
    if isinstance(command, str):
        parts += ["Command:", command]
    parts += ["Arguments:", json.dumps(args, ensure_ascii=False, indent=2)]
    return "\n".join(parts)
