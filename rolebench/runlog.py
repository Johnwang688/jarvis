"""One JSONL row per run, in the claude-workflows skill's section 6 shape
(task class, tokens and cost per role, wall time, result, rounds, merged-ready)
plus the case and run ids."""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path

from . import config


def role(summary: dict | None) -> dict | None:
    if not summary:
        return None
    return {k: summary.get(k) for k in ("harness", "model", "tokens", "cost_usd", "duration_s", "tool_calls", "turns")}


def row(run: dict) -> dict:
    """A log row from a run.json document."""
    roles = {name: role(run.get(name)) for name in ("reviewer", "implementer") if run.get(name)}
    tool_calls = sum((r or {}).get("tool_calls") or 0 for r in roles.values())
    costs = [r["cost_usd"] for r in roles.values() if r and r.get("cost_usd") is not None]
    return {
        "ts": run.get("finished") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": run["run_id"], "case_id": run["case_id"], "kind": run["kind"],
        "task_class": run.get("task_class") or run["kind"],
        "model": run["model"], "harness": run["harness"],
        "roles": roles,
        "cost_usd": round(sum(costs), 6) if costs else None,
        "wall_s": run.get("wall_s"), "tool_calls": tool_calls,
        "result": run["result"], "reason": run.get("reason", ""),
        "rounds": run.get("rounds_to_merge_ready"), "merged_ready": run.get("merged_ready"),
        "findings": run.get("n_findings"), "score": run.get("score"),
        "network_flags": len(run.get("network_flags") or []), "escape_flags": len(run.get("escape_flags") or []),
    }


def append(run: dict, path: Path | None = None) -> None:
    path = path or config.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row(run), sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line)
