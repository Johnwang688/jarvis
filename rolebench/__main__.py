"""python -m rolebench <snapshot|review|implement|judge|report|clean>

    python -m rolebench snapshot hint-r1
    python -m rolebench review --harness claude --model haiku --case hint-r1 --runs 3
    python -m rolebench judge --run ~/.cache/jarvis-lead/rolebench/runs/<run-id>
    python -m rolebench implement --harness claude --model sonnet --case impl-279 \\
        --reviewer-harness claude --reviewer-model opus
    python -m rolebench report
    python -m rolebench clean --judged

The quota guard is always on. `--no-quota-guard` exists for the owner only: it
also needs ROLEBENCH_QUOTA_OVERRIDE=<reason>, and an agent never passes it
without the owner's explicit OK for that run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import cases, config, flows, judge, report, snapshot

OVERRIDE_ENV = "ROLEBENCH_QUOTA_OVERRIDE"


def _case_ids(wanted: list[str], kind: str) -> list[str]:
    ids = []
    for w in wanted:
        if w == "all":
            for path in sorted(config.cases_dir().glob("*.json")):
                if json.loads(path.read_text(encoding="utf-8")).get("kind") == kind:
                    ids.append(path.stem)
        else:
            ids.append(w)
    return ids


def quota_override(args) -> str:
    """The owner's reason when the guard is off, else ''. Exits without one."""
    if not args.no_quota_guard:
        return ""
    reason = (os.environ.get(OVERRIDE_ENV) or "").strip()
    if not reason:
        raise SystemExit(f"--no-quota-guard needs {OVERRIDE_ENV}=<reason> (the owner's explicit OK for this run)")
    bar = "!" * 72
    print(f"{bar}\nWARNING: the quota guard is OFF for this command: {reason}\n"
          f"Every cell is recorded with quota_guard \"off: {reason}\".\n{bar}", file=sys.stderr, flush=True)
    return reason


def _deps(args) -> flows.Deps:
    return flows.Deps(quota_override=quota_override(args))


def cmd_snapshot(args) -> int:
    case = cases.load(args.case)
    run = args.run or time.strftime("%Y%m%d-%H%M%S")
    snap = snapshot.build(case, run)
    print(snap)
    print(json.dumps(snapshot.shas(snap)))
    return 0


def _print_run(run: dict) -> None:
    line = f"{run['run_id']}: {run['result']}"
    if run.get("reason"):
        line += f" ({run['reason']})"
    if run.get("quota_guard", "on") != "on":
        line += f" [quota guard {run['quota_guard']}]"
    if run["kind"] == "review" and run.get("reviewer"):
        doc = run["reviewer"]
        split = doc.get("token_split") or {}
        line += (f" - {run['n_findings']} findings, {doc.get('turns')} turns, cost {doc.get('cost_usd')}, "
                 f"tokens in {split.get('input')} cached {split.get('cached')} out {split.get('output')}, "
                 f"{run['wall_s']}s, net flags {len(run['network_flags'])}, escape flags {len(run['escape_flags'])}")
        if doc.get("multi_model"):
            line += f", models {doc.get('models_used')}"
    if run["kind"] == "implement" and "merged_ready" in run:
        line += f" - merge-ready {run['merged_ready']} after {run['rounds_to_merge_ready']} fix rounds"
    print(line, flush=True)


def cmd_review(args) -> int:
    deps = _deps(args)
    cells = []
    for cid in _case_ids(args.case, "review"):
        case = cases.load(cid)
        if case["kind"] != "review":
            raise SystemExit(f"{cid} is not a review case")
        for n in range(1, args.runs + 1):
            cells.append((case, n))
    cells = flows.shuffled(cells, args.seed)
    runs = flows.run_cells([lambda c=c, n=n: flows.review_once(deps, c, harness=args.harness, model=args.model, n=n)
                            for c, n in cells], args.workers)
    for run in runs:
        _print_run(run)
    return 0


def cmd_implement(args) -> int:
    deps = _deps(args)
    cells = []
    for cid in _case_ids(args.case, "implement"):
        case = cases.load(cid)
        if case["kind"] != "implement":
            raise SystemExit(f"{cid} is not an implement case")
        for n in range(1, args.runs + 1):
            cells.append((case, n))
    cells = flows.shuffled(cells, args.seed)
    runs = flows.run_cells([lambda c=c, n=n: flows.implement_once(
        deps, c, harness=args.harness, model=args.model, reviewer_harness=args.reviewer_harness,
        reviewer_model=args.reviewer_model, n=n) for c, n in cells], args.workers)
    for run in runs:
        _print_run(run)
    return 0


def _run_dirs(root: Path) -> list[Path]:
    return [root] if (root / "run.json").is_file() else sorted(p.parent for p in root.glob("*/run.json"))


def cmd_judge(args) -> int:
    override = quota_override(args)
    root = Path(args.run).expanduser().resolve()
    dirs = _run_dirs(root)
    if not dirs:
        raise SystemExit(f"no run.json under {root}")
    judged = []
    for d in dirs:
        run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        if run.get("kind") != "review" or run.get("result") not in ("ok", "over-cap"):
            print(f"{d.name}: not judged ({run.get('result')})")
            continue
        try:
            doc = judge.judge_run(d, cases.load(run["case_id"]), quota_override=override, force=args.force)
        except judge.JudgeError as exc:
            print(f"{d.name}: judge failed: {exc}")
            continue
        judged.append(d)
        print(f"{d.name}: {json.dumps(doc['score'])}" + (" (already judged; --force to redo)" if doc.get("reused") else ""))
    size, path = judge.spotcheck(judged)
    print(f"{path}: {size} finding(s) to check")
    return 0


def cmd_report(args) -> int:
    text = report.render(report.load_runs(config.runs_root()))
    out = Path(args.out) if args.out else config.home() / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"written to {out}")
    return 0


def cmd_clean(args) -> int:
    """Remove the snapshots of runs that need them no more."""
    if not args.judged:
        raise SystemExit("say what to clean: --judged")
    snap_root = config.snap_root().resolve()
    removed = 0
    for d in _run_dirs(config.runs_root()):
        path = d / "run.json"
        run = json.loads(path.read_text(encoding="utf-8"))
        done = run.get("judged") or (run.get("kind") == "implement" and run.get("finished"))
        snap = Path(run.get("snapshot") or "")
        if not done or not run.get("snapshot") or run.get("snapshot_removed"):
            continue
        if snap.parent.resolve() != snap_root:
            print(f"{d.name}: snapshot {snap} is not under {snap_root}; left alone")
            continue
        shutil.rmtree(snap, ignore_errors=True)
        run["snapshot_removed"] = True
        path.write_text(json.dumps(run, indent=1), encoding="utf-8")
        removed += 1
    print(f"removed {removed} snapshot(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rolebench", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-quota-guard", action="store_true",
                        help=f"owner only, never routine: run Claude/Codex cells without the 75%% quota guard. "
                             f"Also needs {OVERRIDE_ENV}=<reason>; every cell is marked as overridden.")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("snapshot", help="build the isolated repo for a case")
    s.add_argument("case")
    s.add_argument("--run")
    s.set_defaults(func=cmd_snapshot)

    def cell_args(p):
        p.add_argument("--harness", choices=config.HARNESSES, required=True)
        p.add_argument("--model", required=True)
        p.add_argument("--case", action="append", required=True, help="case id (repeat), or 'all'")
        p.add_argument("--runs", type=int, default=1)
        p.add_argument("--seed", type=int, default=1, help="shuffle seed for the cell order")
        p.add_argument("--workers", type=int, default=config.MAX_CONCURRENT)

    r = sub.add_parser("review", help="run a reviewer over review cases")
    cell_args(r)
    r.set_defaults(func=cmd_review)

    i = sub.add_parser("implement", help="run an implementer with review-and-fix rounds")
    cell_args(i)
    i.add_argument("--reviewer-harness", choices=config.HARNESSES, required=True)
    i.add_argument("--reviewer-model", required=True)
    i.set_defaults(func=cmd_implement)

    j = sub.add_parser("judge", help="blind-judge a review run (or every run under a directory)")
    j.add_argument("--run", required=True)
    j.add_argument("--force", action="store_true", help="judge again a run already judged")
    j.set_defaults(func=cmd_judge)

    rep = sub.add_parser("report", help="per-model tables over every run")
    rep.add_argument("--out")
    rep.set_defaults(func=cmd_report)

    c = sub.add_parser("clean", help="remove snapshots no longer needed")
    c.add_argument("--judged", action="store_true", help="snapshots of judged review runs and finished implement runs")
    c.set_defaults(func=cmd_clean)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
