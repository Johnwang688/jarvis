"""The durable goal store (away-agent phase 2).

A goal is a statement of work that outlives any single turn: the runner
(`goalrunner.py`) works it in slices, and everything worth auditing lands
here, on disk, under `config.GOALS_DIR/<id>/`:

  goal.json      statement, status, budgets, spend, the goal's session id
  journal.jsonl  append-only: slices, costs, steering, parks, DMs sent

The journal is written by the *runner*, never by the model — "what did you
do while I was gone" must be answerable from disk, not from a transcript
that compaction has been editing. The model's own durable state lives where
it always has: the goal's session transcript.

Unlike sessions, a goal exists on disk the moment it is created — an asked-
for goal that vanished in a restart would be silently dropped work, which is
the one failure mode this store exists to prevent.

Statuses: queued -> running -> (parked | done | failed | cancelled).
`parked` carries a reason ("budget-dollars", "blocked: …") and is terminal
until the owner intervenes; `running` on disk after a crash simply resumes.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

STATUSES = {"queued", "running", "parked", "done", "failed", "cancelled"}
ACTIVE = ("running", "queued")  # resume order: running first, then queued


def root() -> Path:
    return Path(config.GOALS_DIR)


@dataclass
class Goal:
    id: str
    path: Path
    statement: str
    status: str = "queued"
    reason: str = ""
    budgets: dict = field(default_factory=dict)  # dollars / hours / slices
    spent_usd: float = 0.0
    slices: int = 0
    session_id: str = ""
    created: float = 0.0
    updated: float = 0.0

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        self.updated = time.time()
        payload = {
            "id": self.id,
            "statement": self.statement,
            "status": self.status,
            "reason": self.reason,
            "budgets": self.budgets,
            "spent_usd": round(self.spent_usd, 6),
            "slices": self.slices,
            "session_id": self.session_id,
            "created": self.created,
            "updated": self.updated,
        }
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.path / "goal.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path / "goal.json")

    def journal(self, kind: str, **fields) -> None:
        """Append one audit line. Failures never break the run — the journal
        serves the owner, not the loop."""
        entry = {"ts": time.time(), "kind": kind, **fields}
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            with open(self.path / "journal.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def set(self, status: str, reason: str = "") -> None:
        assert status in STATUSES, status
        self.status = status
        self.reason = reason
        self.save()
        self.journal("status", status=status, reason=reason)

    # -- budgets -----------------------------------------------------------

    def over_budget(self) -> str | None:
        """The reason string if a ceiling is hit, else None."""
        if self.spent_usd >= self.budgets.get("dollars", config.GOAL_MAX_DOLLARS):
            return "budget-dollars"
        hours = self.budgets.get("hours", config.GOAL_MAX_HOURS)
        if self.created and time.time() - self.created >= hours * 3600:
            return "budget-hours"
        if self.slices >= self.budgets.get("slices", config.GOAL_MAX_SLICES):
            return "budget-slices"
        return None

    def elapsed_minutes(self) -> float:
        return (time.time() - self.created) / 60 if self.created else 0.0


def create(
    statement: str,
    dollars: float | None = None,
    hours: float | None = None,
    slices: int | None = None,
) -> Goal:
    goal_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    goal = Goal(
        id=goal_id,
        path=root() / goal_id,
        statement=statement.strip(),
        budgets={
            "dollars": dollars if dollars is not None else config.GOAL_MAX_DOLLARS,
            "hours": hours if hours is not None else config.GOAL_MAX_HOURS,
            "slices": slices if slices is not None else config.GOAL_MAX_SLICES,
        },
        created=time.time(),
    )
    goal.save()
    goal.journal("created", statement=goal.statement, budgets=goal.budgets)
    return goal


def load(goal_id: str) -> Goal | None:
    path = root() / goal_id
    try:
        payload = json.loads((path / "goal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Goal(
        id=payload.get("id", goal_id),
        path=path,
        statement=payload.get("statement", ""),
        status=payload.get("status", "queued"),
        reason=payload.get("reason", ""),
        budgets=payload.get("budgets", {}),
        spent_usd=float(payload.get("spent_usd", 0.0)),
        slices=int(payload.get("slices", 0)),
        session_id=payload.get("session_id", ""),
        created=float(payload.get("created", 0.0)),
        updated=float(payload.get("updated", 0.0)),
    )


def all_goals() -> list[Goal]:
    if not root().exists():
        return []
    found = [load(p.name) for p in sorted(root().iterdir()) if p.is_dir()]
    return [g for g in found if g is not None]


def active() -> list[Goal]:
    """Goals the runner should pick up, interrupted work first: a `running`
    goal on disk is one a crash or shutdown cut short."""
    goals = [g for g in all_goals() if g.status in ACTIVE]
    return sorted(goals, key=lambda g: (g.status != "running", g.created))
