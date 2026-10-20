"""Report math. (Markdown tables and the JSONL log writer: still to do.)"""

from __future__ import annotations


def span(values) -> dict | None:
    """Range and mean over runs, ignoring None; None when nothing was measured."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {"lo": min(vals), "hi": max(vals), "mean": sum(vals) / len(vals), "n": len(vals)}


def by_model(rows: list[dict], fields: list[str]) -> dict:
    """rows (one per run, with harness, model and score fields) -> {(harness, model): {field: span}}."""
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["harness"], r["model"]), []).append(r)
    return {key: {f: span(r.get(f) for r in rs) for f in fields} for key, rs in groups.items()}


# --- tables -----------------------------------------------------------------------

import json
from pathlib import Path


def load_runs(runs_root: Path) -> list[dict]:
    out = []
    for path in sorted(runs_root.glob("*/run.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _fmt(s: dict | None, money: bool = False, pct: bool = False, digits: int = 1) -> str:
    if not s:
        return "-"
    def one(v):
        if pct:
            return f"{v:.0%}"
        if money:
            return f"${v:.3f}"
        text = f"{v:.{digits}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text
    rng = one(s["lo"]) if s["lo"] == s["hi"] else f"{one(s['lo'])}-{one(s['hi'])}"
    return f"{rng} (n={s['n']})"


def review_rows(runs: list[dict]) -> list[dict]:
    rows = []
    for r in runs:
        if r.get("kind") != "review" or r.get("result") == "skipped":
            continue
        doc = r.get("reviewer") or {}
        flags = r.get("flags") or {}
        score = r.get("score") or {}
        rows.append({
            "harness": r["harness"], "model": r["model"], "case": r["case_id"], "result": r["result"],
            "weighted_recall": score.get("weighted_recall"), "medplus_recall": score.get("medplus_recall"),
            "false_positives": score.get("false_positives"),
            "ran_tests": float(flags["ran_tests"]) if "ran_tests" in flags else None,
            "tried_mutant": float(flags["tried_mutant"]) if "tried_mutant" in flags else None,
            "cost": doc.get("cost_usd"), "wall": r.get("wall_s"), "tool_calls": doc.get("tool_calls"),
            "network": len(r.get("network_flags") or []), "escape": len(r.get("escape_flags") or []),
            "tokens": sum((doc.get("tokens") or {}).values()) or None,
        })
    return rows


def implement_rows(runs: list[dict]) -> list[dict]:
    rows = []
    for r in runs:
        if r.get("kind") != "implement" or r.get("result") == "skipped":
            continue
        rows.append({"harness": r["harness"], "model": r["model"], "case": r["case_id"],
                     "merge_ready": 1.0 if r.get("merged_ready") else 0.0,
                     "rounds": r.get("rounds_to_merge_ready"), "cost_to_ready": r.get("cost_to_merge_ready"),
                     "total_cost": r.get("total_cost_usd"), "wall": r.get("wall_s")})
    return rows


def render(runs: list[dict]) -> str:
    out = ["# Role bench report", "",
           "Ranges are lo-hi over runs. Review scores come from the Opus judge and are blank for unjudged runs. "
           "Cost is dollars (jarvis), list-price equivalent (claude) or blank (codex: tokens only). "
           "Never pooled with agent-bench, long-bench or swecompare.", ""]
    rev = by_model(review_rows(runs), ["weighted_recall", "medplus_recall", "false_positives", "ran_tests",
                                      "tried_mutant", "cost", "tokens", "wall", "tool_calls", "network", "escape"])
    skipped = [r for r in runs if r.get("result") == "skipped"]
    out += ["## Reviewer bench", ""]
    if rev:
        out += ["| harness | model | weighted recall | MED+ recall | false pos | ran tests | tried mutant | cost | tokens | wall s | tool calls | net flags |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for (h, m), v in sorted(rev.items()):
            out.append(f"| {h} | {m} | {_fmt(v['weighted_recall'], pct=True)} | {_fmt(v['medplus_recall'], pct=True)} | "
                       f"{_fmt(v['false_positives'], digits=0)} | {_fmt(v['ran_tests'], pct=True)} | {_fmt(v['tried_mutant'], pct=True)} | "
                       f"{_fmt(v['cost'], money=True)} | {_fmt(v['tokens'], digits=0)} | {_fmt(v['wall'], digits=0)} | "
                       f"{_fmt(v['tool_calls'], digits=0)} | {_fmt(v['network'], digits=0)}/{_fmt(v['escape'], digits=0)} |")
    else:
        out.append("No review runs yet.")
    impl = by_model(implement_rows(runs), ["merge_ready", "rounds", "cost_to_ready", "total_cost", "wall"])
    out += ["", "## Implementer bench", ""]
    if impl:
        out += ["| harness | model | merge-ready | rounds to ready | cost to ready | total cost | wall s |", "|---|---|---|---|---|---|---|"]
        for (h, m), v in sorted(impl.items()):
            out.append(f"| {h} | {m} | {_fmt(v['merge_ready'], pct=True)} | {_fmt(v['rounds'], digits=0)} | "
                       f"{_fmt(v['cost_to_ready'], money=True)} | {_fmt(v['total_cost'], money=True)} | {_fmt(v['wall'], digits=0)} |")
    else:
        out.append("No implement runs yet.")
    if skipped:
        out += ["", "## Skipped cells", ""] + [f"- {r['run_id']}: {r.get('reason', '')}" for r in skipped]
    return "\n".join(out) + "\n"
