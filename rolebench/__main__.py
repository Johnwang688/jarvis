"""python -m rolebench <snapshot|review|implement|judge|report>

    python -m rolebench snapshot hint-r1
    python -m rolebench review --harness claude --model haiku --case hint-r1 --runs 3
    python -m rolebench judge --run ~/.cache/jarvis-lead/rolebench/runs/<run-id>
    python -m rolebench implement --harness claude --model sonnet --case impl-279 \\
        --reviewer-harness claude --reviewer-model opus
    python -m rolebench report
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import cases, config, flows, judge, report, snapshot


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


def _deps(args) -> flows.Deps:
    return flows.Deps(use_quota=not args.no_quota_guard)


def cmd_snapshot(args) -> int:
    case = cases.load(args.case)
    run = args.run or time.strftime("%Y%m%d-%H%M%S")
    print(snapshot.build(case, run))
    return 0


def _print_run(run: dict) -> None:
    line = f"{run['run_id']}: {run['result']}"
    if run.get("reason"):
        line += f" ({run['reason']})"
    if run["kind"] == "review" and run.get("reviewer"):
        doc = run["reviewer"]
        line += (f" - {run['n_findings']} findings, {doc.get('turns')} turns, cost {doc.get('cost_usd')}, "
                 f"tokens {sum((doc.get('tokens') or {}).values())}, {run['wall_s']}s, "
                 f"net flags {len(run['network_flags'])}, escape flags {len(run['escape_flags'])}")
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


def cmd_judge(args) -> int:
    root = Path(args.run).expanduser().resolve()
    dirs = [root] if (root / "run.json").is_file() else sorted(p.parent for p in root.glob("*/run.json"))
    if not dirs:
        raise SystemExit(f"no run.json under {root}")
    judged = []
    for d in dirs:
        run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        if run.get("kind") != "review" or run.get("result") not in ("ok", "over-cap"):
            print(f"{d.name}: not judged ({run.get('result')})")
            continue
        try:
            doc = judge.judge_run(d, cases.load(run["case_id"]), use_quota=not args.no_quota_guard)
        except judge.JudgeError as exc:
            print(f"{d.name}: judge failed: {exc}")
            continue
        judged.append(d)
        print(f"{d.name}: {json.dumps(doc['score'])}")
    size = judge.spotcheck(judged, root / "spotcheck.md")
    print(f"spotcheck.md: {size} finding(s) to check")
    return 0


def cmd_report(args) -> int:
    text = report.render(report.load_runs(config.runs_root()))
    out = Path(args.out) if args.out else config.home() / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rolebench", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-quota-guard", action="store_true", help="do not read the daemon's usage (tests, offline)")
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
    j.set_defaults(func=cmd_judge)

    rep = sub.add_parser("report", help="per-model tables over every run")
    rep.add_argument("--out")
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
