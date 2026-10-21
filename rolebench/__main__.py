"""python -m rolebench <snapshot|review|implement|judge|report|clean>

    python -m rolebench snapshot hint-r1
    python -m rolebench review --harness claude --model haiku --case hint-r1 --runs 3
    python -m rolebench judge --run ~/.cache/jarvis-lead/rolebench/runs/<run-id>
    python -m rolebench implement --harness claude --model sonnet --case impl-279 \\
        --reviewer-harness claude --reviewer-model opus
    python -m rolebench pool --case c6-r1                       # cluster valid-new findings (one Opus call)
    python -m rolebench pool --case c6-r1 --accept p1,p3 --severity p1=MED --reject p2 --by john
    python -m rolebench report
    python -m rolebench clean --judged

The quota guard is always on. `--no-quota-guard` exists for the owner only: it
also needs ROLEBENCH_QUOTA_OVERRIDE=<reason>, and an agent never passes it
without the owner's explicit OK for that run. It lifts the 75% guard, never
the hard ceiling (ROLEBENCH_QUOTA_CEILING, default 97%): at or above it, or
with the quota unreadable, a Claude or Codex cell or a judge call is refused.

Claude, Jarvis and judge cells reach their APIs through pasta
(ROLEBENCH_NET=pasta, the default); ROLEBENCH_NET=none has no network, so
real runs of them are refused.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import cases, config, flows, judge, pool, report, snapshot

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


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{value} must be above 0")
    return value


def _deps(args) -> flows.Deps:
    if args.off_matrix:
        print("WARNING: --off-matrix: models outside the O-1 matrix may run; every run row records off_matrix: true",
              file=sys.stderr, flush=True)
    deps = flows.Deps(quota_override=quota_override(args), off_matrix=bool(args.off_matrix))
    if getattr(args, "timeout", None) is not None:
        deps.review_timeout = deps.implement_timeout = int(args.timeout)
    if getattr(args, "max_turns", None) is not None:
        deps.review_max_turns = int(args.max_turns)
    return deps


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
                 f"{run['wall_s']}s, net flags {len(run['network_flags'])}, escape flags {len(run.get('escape_hard', run['escape_flags']))} hard, "
                 f"{len(run.get('escape_cd_relative') or [])} probably cd-relative")
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
        except (judge.JudgeError, cases.CaseError) as exc:
            print(f"{d.name}: judge failed: {exc}")
            continue
        judged.append(d)
        print(f"{d.name}: {json.dumps(doc['score'])}" + (" (already judged; --force to redo)" if doc.get("reused") else ""))
    size, path = judge.spotcheck(judged)
    print(f"{path}: {size} finding(s) to check")
    return 0


def cmd_report(args) -> int:
    runs = report.load_runs(config.runs_root())
    text = report.render(runs, pool.rescore_runs(runs))
    out = Path(args.out) if args.out else config.home() / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"written to {out}")
    return 0


def _ids(values) -> list[str]:
    return [v.strip() for item in values or () for v in item.split(",") if v.strip()]


def cmd_pool(args) -> int:
    """Cluster a case's valid-new findings (one Opus call), or adjudicate the clusters."""
    if args.accept or args.reject or args.severity or args.accept_members:
        severities = {}
        for item in _ids(args.severity):
            cid, _, sev = item.partition("=")
            if not sev:
                raise SystemExit(f"--severity takes id=SEVERITY, got {item!r}")
            severities[cid.strip()] = sev.strip().upper()
        try:
            doc = pool.adjudicate(args.case, accept=_ids(args.accept), reject=_ids(args.reject),
                                  severities=severities, by=args.by or "", accept_members=_ids(args.accept_members),
                                  redefine=args.redefine)
        except (pool.PoolError, cases.CaseError) as exc:
            raise SystemExit(f"pool: {exc}")
        print(f"{pool.key_path(args.case)}: {sum(c['status'] == pool.ACCEPTED for c in doc['clusters'])} accepted cluster(s)")
    else:
        try:
            doc = pool.pool_case(args.case, quota_override=quota_override(args))
        except (pool.PoolError, cases.CaseError) as exc:
            raise SystemExit(f"pool: {exc}")
    for c in doc["clusters"]:
        print(f"{c['id']} [{c.get('severity_final') or c['severity']}] {c['status']}: {c['location']}: "
              f"{c['mechanism']} ({len(c['members'])} member(s))")
        for m in c.get("pending") or ():
            print(f"    pending (needs --accept-members {c['id']}): {m['run']} finding {m['finding']}: {m.get('title', '')}")
    print(pool.pool_path(args.case))
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
                        help=f"owner only, never routine: run Claude/Codex cells without the 75%% quota guard "
                             f"(never past ROLEBENCH_QUOTA_CEILING, default 97%%). "
                             f"Also needs {OVERRIDE_ENV}=<reason>; every cell is marked as overridden.")
    parser.add_argument("--off-matrix", action="store_true",
                        help="run a model that is not in config.MATRIX for its harness (recorded in every run row)")
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
        p.add_argument("--timeout", type=_positive, help="seconds per agent run (a smoke's cap; default the config's)")
        p.add_argument("--max-turns", type=_positive, help="reviewer steps (a smoke's cap; default the config's)")

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

    po = sub.add_parser("pool", help="cluster a case's judge-confirmed valid-new findings, or adjudicate the clusters")
    po.add_argument("--case", required=True)
    po.add_argument("--accept", action="append", help="cluster ids to accept into the pooled key (comma-separated, repeatable)")
    po.add_argument("--reject", action="append", help="cluster ids to reject")
    po.add_argument("--severity", action="append", help="id=SEVERITY for an accepted cluster (repeatable)")
    po.add_argument("--accept-members", action="append",
                    help="accepted cluster ids whose pending members (proposed by a later pool run) join the key")
    po.add_argument("--redefine", action="store_true",
                    help="allow an accepted cluster's committed mechanism or location to change")
    po.add_argument("--by", help="who adjudicated (required with --accept/--reject/--severity/--accept-members)")
    po.set_defaults(func=cmd_pool)

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
