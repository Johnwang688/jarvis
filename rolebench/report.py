"""Report math and the markdown tables over every run."""

from __future__ import annotations

import json
from pathlib import Path


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


def _split(doc: dict, key: str):
    v = (doc.get("token_split") or {}).get(key)
    return v if isinstance(v, int) else None


def review_rows(runs: list[dict], rescored: dict | None = None) -> list[dict]:
    """One row per review run. With `rescored` (pool.rescore_runs) a judged run
    carries both its original-key and its pooled-key recall."""
    rows = []
    for r in runs:
        if r.get("kind") != "review" or r.get("result") == "skipped":
            continue
        doc = r.get("reviewer") or {}
        flags = r.get("flags") or {}
        score = r.get("score") or {}
        rs = (rescored or {}).get(r.get("run_id"))
        orig = rs["original"] if rs else {"weighted_recall": score.get("weighted_recall"),
                                          "medplus_recall": score.get("medplus_recall")}
        pooled = rs["pooled"] if rs else (r.get("score_pooled") or {})
        rows.append({
            "harness": r["harness"], "model": r["model"], "case": r["case_id"], "result": r["result"],
            "weighted_recall": orig.get("weighted_recall"), "medplus_recall": orig.get("medplus_recall"),
            "weighted_recall_pooled": pooled.get("weighted_recall"), "medplus_recall_pooled": pooled.get("medplus_recall"),
            "false_positives": score.get("false_positives"),
            "ran_tests": float(flags["ran_tests"]) if "ran_tests" in flags else None,
            "tried_mutant": float(flags["tried_mutant"]) if "tried_mutant" in flags else None,
            "cost": doc.get("cost_usd"), "wall": r.get("wall_s"), "tool_calls": doc.get("tool_calls"),
            "network": len(r.get("network_flags") or []),
            # hard flags count against the run; probable-cd-relative ones are for the lead to glance at
            "escape": len(r.get("escape_hard", r.get("escape_flags")) or []),
            "escape_soft": len(r.get("escape_cd_relative") or []),
            "in_tokens": _split(doc, "input"), "cached_tokens": _split(doc, "cached"), "out_tokens": _split(doc, "output"),
        })
    return rows


def implement_rows(runs: list[dict]) -> list[dict]:
    rows = []
    for r in runs:
        if r.get("kind") != "implement" or r.get("result") == "skipped":
            continue
        rounds = r.get("rounds") or []
        tok = {"input": 0, "cached": 0, "output": 0}
        for rd in rounds:
            for part in (rd.get("implementer"), rd.get("reviewer")):
                for k in tok:
                    tok[k] += _split(part or {}, k) or 0
        rows.append({"harness": r["harness"], "model": r["model"], "case": r["case_id"],
                     "merge_ready": 1.0 if r.get("merged_ready") else 0.0,
                     "rounds": r.get("rounds_to_merge_ready"), "cost_to_ready": r.get("cost_to_merge_ready"),
                     "total_cost": r.get("total_cost_usd"), "wall": r.get("wall_s"),
                     "in_tokens": tok["input"] if rounds else None, "cached_tokens": tok["cached"] if rounds else None,
                     "out_tokens": tok["output"] if rounds else None,
                     "out_of_scope": float(any((rd.get("diff") or {}).get("out_of_scope") for rd in rounds)) if rounds else None})
    return rows


POOLING_NOTE = ("Recall is shown against two keys: the original key (the findings the PR's own review "
                "adjudicated) and the pooled key (the original plus the clusters of judge-confirmed valid-new "
                "findings the lead accepted, `rolebench pool`). Pooling bias: the pool is built from the models "
                "that ran, so a defect no model found is still missing from both keys, and pooled recall rewards "
                "finding what other models in this bench found.")


def render(runs: list[dict], rescored: dict | None = None) -> str:
    out = ["# Role bench report", "", POOLING_NOTE, "",
           "Ranges are lo-hi over runs. Review scores come from the Opus judge and are blank for unjudged runs. "
           "Cost is dollars (jarvis), list-price equivalent (claude) or blank (codex: tokens only). "
           "Tokens: input excludes cached reads in every harness (Codex's input_tokens include them and are split here). "
           "Never pooled with agent-bench, long-bench or swecompare.", ""]
    rev = by_model(review_rows(runs, rescored), ["weighted_recall", "medplus_recall", "weighted_recall_pooled",
                                                 "medplus_recall_pooled", "false_positives", "ran_tests",
                                      "tried_mutant", "cost", "in_tokens", "cached_tokens", "out_tokens", "wall",
                                      "tool_calls", "network", "escape", "escape_soft"])
    out += ["## Reviewer bench", ""]
    if rev:
        out += ["| harness | model | weighted recall (original key) | MED+ recall (original key) | "
                "weighted recall (pooled key) | MED+ recall (pooled key) | false pos | ran tests | tried mutant | cost | "
                "in tok | cached tok | out tok | wall s | tool calls | net/escape flags (hard) | escape: probably cd-relative |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for (h, m), v in sorted(rev.items()):
            out.append(f"| {h} | {m} | {_fmt(v['weighted_recall'], pct=True)} | {_fmt(v['medplus_recall'], pct=True)} | "
                       f"{_fmt(v['weighted_recall_pooled'], pct=True)} | {_fmt(v['medplus_recall_pooled'], pct=True)} | "
                       f"{_fmt(v['false_positives'], digits=0)} | {_fmt(v['ran_tests'], pct=True)} | {_fmt(v['tried_mutant'], pct=True)} | "
                       f"{_fmt(v['cost'], money=True)} | {_fmt(v['in_tokens'], digits=0)} | {_fmt(v['cached_tokens'], digits=0)} | "
                       f"{_fmt(v['out_tokens'], digits=0)} | {_fmt(v['wall'], digits=0)} | "
                       f"{_fmt(v['tool_calls'], digits=0)} | {_fmt(v['network'], digits=0)}/{_fmt(v['escape'], digits=0)} | "
                       f"{_fmt(v['escape_soft'], digits=0)} |")
    else:
        out.append("No review runs yet.")
    impl = by_model(implement_rows(runs), ["merge_ready", "rounds", "cost_to_ready", "total_cost", "in_tokens",
                                           "cached_tokens", "out_tokens", "wall", "out_of_scope"])
    out += ["", "## Implementer bench", ""]
    if impl:
        out += ["| harness | model | merge-ready | rounds to ready | cost to ready | total cost | in tok | cached tok | "
                "out tok | wall s | out of scope |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for (h, m), v in sorted(impl.items()):
            out.append(f"| {h} | {m} | {_fmt(v['merge_ready'], pct=True)} | {_fmt(v['rounds'], digits=0)} | "
                       f"{_fmt(v['cost_to_ready'], money=True)} | {_fmt(v['total_cost'], money=True)} | "
                       f"{_fmt(v['in_tokens'], digits=0)} | {_fmt(v['cached_tokens'], digits=0)} | {_fmt(v['out_tokens'], digits=0)} | "
                       f"{_fmt(v['wall'], digits=0)} | {_fmt(v['out_of_scope'], pct=True)} |")
    else:
        out.append("No implement runs yet.")
    overridden = [r for r in runs if str(r.get("quota_guard", "on")).startswith("off")]
    if overridden:
        out += ["", "## Cells run with the quota guard OFF", ""] + [
            f"- {r['run_id']}: {r.get('quota_guard')}" for r in overridden]
    multi = [r for r in runs if (r.get("reviewer") or {}).get("multi_model") or (r.get("implementer") or {}).get("multi_model")]
    if multi:
        out += ["", "## Runs that used more than one model", ""] + [
            f"- {r['run_id']}: " + ", ".join(sorted(set((r.get("reviewer") or {}).get("models_used") or [])
                                                    | set((r.get("implementer") or {}).get("models_used") or [])))
            for r in multi]
    dirty = [r for r in runs if (r.get("hygiene") or {}).get("after_run")]
    if dirty:
        out += ["", "## Snapshots an agent changed (commits, refs or HEAD; undone before judging)", ""] + [
            f"- {r['run_id']}: {'; '.join(r['hygiene']['after_run'])}" for r in dirty]
    soft = [(r, d) for r in runs for d in r.get("escape_cd_relative") or ()]
    if soft:
        out += ["", "## Escape flags that are probably cd-relative (advisory: for the lead to glance at)", ""] + [
            f"- {r['run_id']}: {d['flag']} (assumed cwd `{d.get('assumed_cwd')}`) in `{(d.get('command') or '')[:160]}`"
            for r, d in soft]
    skipped = [r for r in runs if r.get("result") == "skipped"]
    if skipped:
        out += ["", "## Skipped cells", ""] + [f"- {r['run_id']}: {r.get('reason', '')}" for r in skipped]
    return "\n".join(out) + "\n"
