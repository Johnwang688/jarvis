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


def by_model(rows: list[dict], fields: list[str], keys: tuple = ("harness", "model")) -> dict:
    """rows (one per run, with harness, model and score fields) -> {(harness, model): {field: span}}
    (or keyed by `keys`, e.g. ("harness", "model", "grader") so two graders never share a row)."""
    groups: dict = {}
    for r in rows:
        groups.setdefault(tuple(r[k] for k in keys), []).append(r)
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


def escape_cleared(run: dict) -> bool:
    """The lead cleared exactly the run's current escape flags (a later flag is never pre-cleared)."""
    c = run.get("escape_cleared") or {}
    return bool(c) and sorted(c.get("flags") or []) == sorted(run.get("escape_flags") or [])


def review_rows(runs: list[dict], rescored: dict | None = None) -> list[dict]:
    """One row per review run. With `rescored` (pool.rescore_runs) a judged run
    carries its original-key and its full-key recall as they are now (the
    legacy judge's full key is original + pooled; the rubric grader's is
    original + rubric + pooled), and a rubric-graded one its nits, unverified
    findings and precision."""
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
        full = (rs.get("full") or rs.get("pooled")) if rs else (r.get("score_full") or r.get("score_pooled") or {})
        grader = (rs or {}).get("grader") or r.get("grader") or ("legacy" if r.get("judged") else "-")
        rubric = grader not in ("legacy", "-")
        settled = rs if rs and rubric else (score if rubric else {})
        rows.append({
            "run_id": r.get("run_id"), "grader": grader,
            "harness": r["harness"], "model": r["model"], "case": r["case_id"], "result": r["result"],
            "weighted_recall": orig.get("weighted_recall"), "medplus_recall": orig.get("medplus_recall"),
            "weighted_recall_full": full.get("weighted_recall"), "medplus_recall_full": full.get("medplus_recall"),
            "false_positives": settled.get("false_positives") if rubric else score.get("false_positives"),
            "nits": settled.get("nits") if rubric else score.get("nits"),
            "unverified": settled.get("unverified") if rubric else None,
            "precision": settled.get("precision") if rubric else None,
            "ran_tests": float(flags["ran_tests"]) if "ran_tests" in flags else None,
            "tried_mutant": float(flags["tried_mutant"]) if "tried_mutant" in flags else None,
            "cost": doc.get("cost_usd"), "wall": r.get("wall_s"), "tool_calls": doc.get("tool_calls"),
            "network": len(r.get("network_flags") or []),
            # every escape flag counts until the lead clears it (clear-flags); the cd-relative
            # count is "of which", a triage hint that never discounts a flag
            "escape": 0 if escape_cleared(r) else len(r.get("escape_flags") or []),
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
                "adjudicated, human- and seed-sourced, minus any entry the rubric disputed) and the full key (the "
                "original plus, for the rubric grader, the rubric's verified entries, plus the pooled clusters the "
                "lead accepted, `rolebench pool`). The rubric comes from one model family, so the original-key "
                "columns are the ones to compare across families. Pooling bias: the pool is built from the models "
                "that ran, so a defect no model found is still missing from both keys, and pooled recall rewards "
                "finding what other models in this bench found.")
GRADER_NOTE = ("Grader: `rubric-1` is the two-stage grader (a location gate, then a text-only Sonnet matcher "
               "against the case's rubric; unmatched findings are ruled once by Opus and adjudicated by the lead); "
               "`legacy` is the Opus judge with repo access. False positives are non-issue matches (rubric-1) or "
               "invalid verdicts (legacy). Unverified findings (rubric-1: unmatched and not yet settled by an accepted "
               "ruling) are never counted as right or wrong; precision is over the settled findings only.")


def render(runs: list[dict], rescored: dict | None = None) -> str:
    out = ["# Role bench report", "", POOLING_NOTE, "", GRADER_NOTE, "",
           "Ranges are lo-hi over runs. Review scores come from the Opus judge and are blank for unjudged runs. "
           "Cost is dollars (jarvis), list-price equivalent (claude) or blank (codex: tokens only). "
           "Tokens: input excludes cached reads in every harness (Codex's input_tokens include them and are split here). "
           "Never pooled with agent-bench, long-bench or swecompare.", ""]
    rrows = review_rows(runs, rescored)
    rev = by_model(rrows, ["weighted_recall", "medplus_recall", "weighted_recall_full",
                           "medplus_recall_full", "false_positives", "nits", "unverified", "precision", "ran_tests",
                           "tried_mutant", "cost", "in_tokens", "cached_tokens", "out_tokens", "wall",
                           "tool_calls", "network", "escape", "escape_soft"], keys=("harness", "model", "grader"))
    out += ["## Reviewer bench", ""]
    if rev:
        out += ["| harness | model | grader | weighted recall (original key) | MED+ recall (original key) | "
                "weighted recall (full key) | MED+ recall (full key) | false pos | nits | unverified | precision (settled) | "
                "ran tests | tried mutant | cost | "
                "in tok | cached tok | out tok | wall s | tool calls | net/escape flags | of which probably cd-relative |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for (h, m, g), v in sorted(rev.items()):
            out.append(f"| {h} | {m} | {g} | {_fmt(v['weighted_recall'], pct=True)} | {_fmt(v['medplus_recall'], pct=True)} | "
                       f"{_fmt(v['weighted_recall_full'], pct=True)} | {_fmt(v['medplus_recall_full'], pct=True)} | "
                       f"{_fmt(v['false_positives'], digits=0)} | {_fmt(v['nits'], digits=0)} | {_fmt(v['unverified'], digits=0)} | "
                       f"{_fmt(v['precision'], pct=True)} | {_fmt(v['ran_tests'], pct=True)} | {_fmt(v['tried_mutant'], pct=True)} | "
                       f"{_fmt(v['cost'], money=True)} | {_fmt(v['in_tokens'], digits=0)} | {_fmt(v['cached_tokens'], digits=0)} | "
                       f"{_fmt(v['out_tokens'], digits=0)} | {_fmt(v['wall'], digits=0)} | "
                       f"{_fmt(v['tool_calls'], digits=0)} | {_fmt(v['network'], digits=0)}/{_fmt(v['escape'], digits=0)} | "
                       f"{_fmt(v['escape_soft'], digits=0)} |")
        judged = [r for r in rrows if r["grader"] != "-"]
        if judged:
            out += ["", "### Review runs, with the grader of each", "",
                    "| run | case | harness | model | grader | weighted recall (original) | weighted recall (full) | "
                    "false pos | nits | unverified | precision (settled) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
            one = lambda v, pct=False: "-" if v is None else (f"{v:.0%}" if pct else str(v))
            for r in sorted(judged, key=lambda r: (r["case"], r["run_id"] or "")):
                out.append(f"| {r['run_id']} | {r['case']} | {r['harness']} | {r['model']} | {r['grader']} | "
                           f"{one(r['weighted_recall'], True)} | {one(r['weighted_recall_full'], True)} | "
                           f"{one(r['false_positives'])} | {one(r['nits'])} | {one(r['unverified'])} | {one(r['precision'], True)} |")
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
        out += ["", "## Escape flags that are probably cd-relative (a triage hint: they still count until cleared)", ""] + [
            f"- {r['run_id']}: {d['flag']} (assumed cwd `{d.get('assumed_cwd')}`) in `{(d.get('command') or '')[:160]}`"
            for r, d in soft]
    cleared = [r for r in runs if escape_cleared(r)]
    if cleared:
        out += ["", "## Escape flags cleared by hand", ""] + [
            f"- {r['run_id']}: {len(r['escape_cleared']['flags'])} flag(s) cleared by {r['escape_cleared']['by']} "
            f"on {r['escape_cleared']['on']}" for r in cleared]
    skipped = [r for r in runs if r.get("result") == "skipped"]
    if skipped:
        out += ["", "## Skipped cells", ""] + [f"- {r['run_id']}: {r.get('reason', '')}" for r in skipped]
    return "\n".join(out) + "\n"
