"""Pure Discord templates. Status prose comes exclusively from the runner record."""
from __future__ import annotations

import json

from ..approvals import clean_line
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
                    f"Answer here: tap a button, or /yes {ctx.get('code', '')} · "
                    f"/no {ctx.get('code', '')}.",
        "blocked": f"Blocked: {detail or 'Owner input required.'}",
        "verified": f"Verified: {ctx.get('text', task.report.verified if task.report else 'Checks passed.')}",
        "done": f"Done: task {task.id}. {ctx.get('text', 'Report follows.')}",
        "failed": f"Failed: {detail or 'See the task log for details.'}",
        "cancelled": "Cancelled.",
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


_MD = str.maketrans({c: "\\" + c for c in "\\*_~|>#[]()<"})


def headline_markdown(headline: str) -> str:
    """A headline as one inert Discord line: cleaned like `label()` (no
    newlines, controls or backticks; capped), markdown escaped so it cannot
    close the bold or start a quote, and `@` defused even though
    allowed_mentions already parses nothing."""
    line = clean_line(headline, 420).translate(_MD)
    return line.replace("@", "@\u200b")


class ApprovalPost(str):
    """An approval too long for one message: the headline and the answers
    inline, the whole request attached (`DiscordRest` reads `overflow`)."""

    overflow: str
    overflow_name = "approval.txt"

    def __new__(cls, inline: str, full: str):
        result = super().__new__(cls, inline)
        result.overflow = full
        return result


def approval_post(body: str, *, headline: str = "") -> str:
    """`body` as posted. Over 2000 characters DiscordRest attaches the whole
    text and shows only "Full message attached" — fine for an ordinary
    request, wrong for a sandbox widening, whose headline must be read before
    anyone answers. So a long widening keeps its first four lines (headline,
    tool, origin, answers) inline and attaches the rest."""
    if not headline or len(body) <= 2000:
        return body
    head = "\n".join(body.splitlines()[:4])
    return ApprovalPost(head + "\nThe full request is attached (unabridged); read it before answering.",
                        body)


def approval_text(tool: str, args, code: str, origin: str, *, allowlistable: bool = True,
                  headline: str = "") -> str:
    """Never truncate. DiscordRest.post attaches this verbatim when >2000 chars.

    `/always` is offered only where it can mint a standing rule: the owner is
    never shown an answer that would be refused (S1). A `headline` (a Codex
    sandbox widening) is the first line, above everything else."""
    # A shell command must be visible with literal newlines, not JSON escapes.
    command = args.get("command") if isinstance(args, dict) else args
    answers = f"`/yes {code}` · `/no {code}`" + (f" · `/always {code}`" if allowlistable else "")
    parts = [f"**{headline_markdown(headline)}**"] if headline else []
    parts += [f"Approval required: {tool}", f"Origin: {origin}",
             f"Answer here: tap a button, or {answers}."]
    if isinstance(command, str):
        parts += ["Command:", command]
    parts += ["Arguments:", json.dumps(args, ensure_ascii=False, indent=2)]
    return "\n".join(parts)


# Button custom ids carry the 4-character code, never the broker's request id:
# the request id is the HUD's one-shot handle and never needs to leave the
# machine. Whatever a press carries is untrusted anyway — `interactions.py`
# finds the request by the message the button is on and checks the code agrees.
APPROVE_PREFIX, DENY_PREFIX = "jv:a:", "jv:d:"


def approval_components(code: str) -> list[dict]:
    """Approve and Deny. Never an Always button: a standing rule is the typed
    `/always` only (decisions S-2). No default — deny is as easy as approve."""
    return [{"type": 1, "components": [
        {"type": 2, "style": 3, "label": "Approve", "custom_id": APPROVE_PREFIX + code},
        {"type": 2, "style": 4, "label": "Deny", "custom_id": DENY_PREFIX + code},
    ]}]
