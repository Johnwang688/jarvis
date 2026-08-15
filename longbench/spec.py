"""What a long-bench task is, and how one is scored.

Deliberately stdlib-only and free of any `jarvis` import. long-bench grades a
*directory*, not an agent, which is what lets the same task run against the
Jarvis loop, against `claude -p`, or against a human with a text editor. The
only thing a harness has to do is take a prompt, work in a directory, and stop.

Scoring keeps agent-bench's rule — **grade the world the agent left behind, not
the prose it wrote** — with one change that matters for this bench. agent-bench
checks are booleans, and long-bench is largely a bench about *completeness*:
"migrated 31 of 34 call sites" and "migrated 0 of 34" are the same boolean and
wildly different results. So a `Check` carries `earned`/`possible` floats and a
grader may award part of one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# One rating per category, plus an overall. These are the failure modes a long
# run has that a short one does not:
#
#   completeness  did it finish the sweep, or stop when the context filled up
#   retention     did a fact from early in the run survive to the deliverable
#   discipline    did a constraint stated once still hold at step 80
#   recovery      did bad input get worked around, or did it stall
#   interference  did the authoritative spec keep winning over a stale document
#                 the agent happened to read more recently
#   correctness   is what it produced actually right
CATEGORIES = ["completeness", "retention", "discipline", "recovery",
              "interference", "correctness"]


@dataclass
class Check:
    """One graded property of the world the agent left behind."""

    category: str
    name: str
    earned: float
    possible: float
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.earned >= self.possible - 1e-9

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "name": self.name,
            "earned": round(self.earned, 4),
            "possible": round(self.possible, 4),
            "ok": self.ok,
            "detail": self.detail,
        }


def scored(category: str, name: str, ratio: float, weight: float, detail: str = "") -> Check:
    """A partial-credit check: `ratio` of `weight`, clamped to [0, 1]."""
    ratio = max(0.0, min(1.0, ratio))
    return Check(category, name, ratio * weight, weight, detail)


def passed(category: str, name: str, ok: bool, weight: float = 1.0, detail: str = "") -> Check:
    """An all-or-nothing check."""
    return Check(category, name, weight if ok else 0.0, weight, detail)


def by_category(checks: list[Check]) -> dict[str, tuple[float, float]]:
    """category -> (earned, possible). Categories with no checks are omitted."""
    totals: dict[str, list[float]] = {}
    for check in checks:
        bucket = totals.setdefault(check.category, [0.0, 0.0])
        bucket[0] += check.earned
        bucket[1] += check.possible
    return {name: (earned, possible) for name, (earned, possible) in totals.items()}


def overall(checks: list[Check]) -> float:
    possible = sum(c.possible for c in checks)
    return (sum(c.earned for c in checks) / possible) if possible else 0.0


# ---------------------------------------------------------------------------
# a task
# ---------------------------------------------------------------------------

# Identical for every harness, because anything said to one and not the other
# is a confound rather than a measurement. It states only what the harness
# cannot know: where the work goes, and that the files are the deliverable.
PREAMBLE = """\
Work in the current directory. Everything you need is already here and you need
no network access.

Ground rules:
- The deliverables are files on disk. Your reply is not graded; the files are.
- Do not modify or delete any file the task does not tell you to modify.
- Do not create files outside this directory.
- Finish the whole task before you stop.
"""


@dataclass
class Task:
    """A long-horizon task: a pure plan, a world built from it, and a grader.

    The split between `plan` and `materialize` is the load-bearing part. `plan`
    is a pure function of the seed — no filesystem — so the grader can recompute
    the ground truth at grading time instead of reading it off disk. Nothing the
    agent could open ever contains the answer, and two machines with the same
    seed grade identically.
    """

    name: str
    tests: str
    plan: Callable[[int, float], dict]
    materialize: Callable[[dict, Path], None]
    prompt: Callable[[dict], str]
    grade: Callable[[dict, Path], list[Check]]
    # A runaway guard, not a work limit — same reasoning as config.MAX_STEPS.
    # Both harnesses get the same number.
    max_turns: int = 80
    # Later turns, delivered after the first one finishes. This is the spec-drift
    # axis: a rule that changes once the work is already done forces the agent
    # back into files it last touched forty steps ago, which is exactly where a
    # transcript that has been compacted stops being enough.
    followups: Callable[[dict], list[str]] | None = None

    def all_prompts(self, plan: dict) -> list[str]:
        return [self.prompt(plan)] + (self.followups(plan) if self.followups else [])


# ---------------------------------------------------------------------------
# what a harness gives back
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    """Telemetry from one harness run. Every field is best-effort.

    A harness that cannot report something leaves it None rather than zero —
    a missing measurement and a measurement of nothing must not print the same.
    """

    harness: str
    model: str
    # "shell" or "no-shell". Recorded because the two are different experiments
    # and their numbers must never be averaged together — see harness.py.
    mode: str = "shell"
    # The compaction threshold both harnesses were held to, or None for each
    # harness's own default. Recorded for the same reason as `mode`: it changes
    # what the run measures, so results at different windows must not be pooled.
    window: int | None = None
    cost_usd: float | None = None
    duration_s: float | None = None
    turns: int | None = None
    tool_calls: list[tuple[str, str]] = field(default_factory=list)  # (name, args json)
    # Token counts, when the source reports them. A run on a subscription has
    # no per-run dollar figure, so tokens are what makes it comparable at all.
    tokens: dict[str, int] = field(default_factory=dict)
    error: str = ""
    reply: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def redundant_calls(self) -> int:
        """Identical (tool, arguments) pairs issued more than once.

        The cheapest available proxy for the failure the loop cannot see today:
        a run going in circles. Re-reading a file after editing it is legitimate,
        so this is a signal to look at, never a check to fail on.
        """
        seen: set[tuple[str, str]] = set()
        repeats = 0
        for call in self.tool_calls:
            if call in seen:
                repeats += 1
            seen.add(call)
        return repeats

    def as_dict(self) -> dict:
        return {
            "harness": self.harness,
            "model": self.model,
            "mode": self.mode,
            "window": self.window,
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
            "turns": self.turns,
            "tool_calls": len(self.tool_calls),
            "redundant_calls": self.redundant_calls,
            "tokens": self.tokens or None,
            "tools_used": _histogram(self.tool_calls),
            "error": self.error,
        }


def _histogram(calls: list[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, _ in calls:
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------------
# results on disk
# ---------------------------------------------------------------------------


def result_document(task: str, seed: int, record: RunRecord, checks: list[Check],
                    scale: float = 1.0) -> dict:
    totals = by_category(checks)
    return {
        "task": task,
        "seed": seed,
        "scale": scale,
        "run": record.as_dict(),
        "overall": round(overall(checks), 4),
        "categories": {
            name: round(earned / possible, 4)
            for name, (earned, possible) in totals.items()
            if possible
        },
        "checks": [c.as_dict() for c in checks],
    }


def write_result(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
