"""One JSONL row per run, in the claude-workflows skill's section 6 shape
(task class, tokens and cost per role, wall time, result, rounds, merged-ready)
plus the case and run ids and whether the quota guard was on."""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path

from . import config


def role(summary: dict | None) -> dict | None:
    if not summary:
        return None
    return {k: summary.get(k) for k in ("harness", "model", "tokens", "token_split", "cost_usd", "duration_s",
                                        "tool_calls", "turns", "models_used", "multi_model")}


COST_BASIS = {"claude": "list-price equivalent", "jarvis": "usd (OpenRouter reported)", "codex": None}


def cost_basis(run: dict) -> str | None:
    """What a run's cost_usd means; implement runs name both roles' harnesses."""
    harnesses = [run.get("harness")] + ([run["reviewer_harness"]] if run.get("reviewer_harness") else [])
    bases = sorted({COST_BASIS.get(h) for h in harnesses if COST_BASIS.get(h)})
    return " + ".join(bases) or None


def row(run: dict) -> dict:
    """A log row from a run.json document."""
    roles = {name: role(run.get(name)) for name in ("reviewer", "implementer") if run.get(name)}
    if run.get("kind") == "implement":
        cost = run.get("total_cost_usd")          # every round, both roles; not just the last round
        tool_calls = sum(((r.get("implementer") or {}).get("tool_calls") or 0)
                         + ((r.get("reviewer") or {}).get("tool_calls") or 0) for r in run.get("rounds") or [])
    else:
        costs = [r["cost_usd"] for r in roles.values() if r and r.get("cost_usd") is not None]
        cost = round(sum(costs), 6) if costs else None
        tool_calls = sum((r or {}).get("tool_calls") or 0 for r in roles.values())
    return {
        "ts": run.get("finished") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": run["run_id"], "case_id": run["case_id"], "kind": run["kind"],
        "task_class": run.get("task_class") or run["kind"],
        "model": run["model"], "harness": run["harness"],
        "roles": roles,
        "cost_usd": cost, "cost_basis": cost_basis(run),
        "wall_s": run.get("wall_s"), "tool_calls": tool_calls,
        "result": run["result"], "reason": run.get("reason", ""),
        "rounds": run.get("rounds_to_merge_ready"), "merged_ready": run.get("merged_ready"),
        "findings": run.get("n_findings"), "score": run.get("score"),
        "quota_guard": run.get("quota_guard", "on"), "off_matrix": bool(run.get("off_matrix")),
        "truncated": bool(run.get("truncated")), "effort": run.get("effort"),
        "reviewer_effort": run.get("reviewer_effort"), "timeout_s": run.get("timeout_s"), "max_turns": run.get("max_turns"),
        # {provider: {window: used percent}} read before and after the cell (None = unknown)
        "quota_before": run.get("quota_before"), "quota_after": run.get("quota_after"),
        "network_flags": len(run.get("network_flags") or []), "escape_flags": len(run.get("escape_flags") or []),
    }


def append(run: dict, path: Path | None = None) -> None:
    path = path or config.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row(run), sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line)
