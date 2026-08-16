"""swecompare CLI — two harnesses, one SWE-bench Verified subset.

    python -m swecompare subset --size 30
    python -m swecompare run --harness claude --model sonnet --effort high --size 30
    python -m swecompare run --harness jarvis --model openai/gpt-5.6-luna --size 30
    python -m swecompare grade --run-dir runs/<id>
    python -m swecompare report

`run` only collects patches. Grading is a separate step that shells out to the
official `swebench` harness, so the thing producing the score is never the thing
that produced the answer.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from longbench import harness as harness_mod
from swecompare import bench as bench_mod

DEFAULT_RUNS = Path(__file__).resolve().parent / "runs"
DEFAULT_SIZE = 30
MAX_TURNS = 120


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float, float]:
    """Point estimate and 95% Wilson interval.

    Reported instead of a bare percentage because at N=30 the interval is about
    +/-18 points near 50% — wide enough that two cells can differ by a lot and
    still not differ *detectably*. Presenting one run as a verdict is the mistake
    this whole project already made once.
    """
    if not total:
        return 0.0, 0.0, 0.0
    p = successes / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denom
    return p, max(0.0, centre - margin), min(1.0, centre + margin)


def cmd_subset(args) -> int:
    chosen = bench_mod.subset(bench_mod.load_instances(), args.size, args.seed)
    counts: dict[str, int] = {}
    for inst in chosen:
        counts[inst.repo] = counts.get(inst.repo, 0) + 1
    print(f"{len(chosen)} instances (seed {args.seed})")
    for repo, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3d}  {repo}")
    if args.ids:
        for inst in chosen:
            print(inst.instance_id)
    return 0


def cmd_images(args) -> int:
    ids = args.instance or [
        i.instance_id for i in
        bench_mod.subset(bench_mod.load_instances(), args.size, args.seed)]
    instances = bench_mod.load_instances(ids)
    found = {i.instance_id for i in instances}
    # An id that is not in Verified silently yields an empty instance list, and
    # "0 of 0 missing" then prints as success — which is how a request for a
    # SWE-bench-full instance reported "built: 1/1" having done nothing at all.
    unknown = [i for i in ids if i not in found]
    if unknown:
        raise SystemExit(f"not in {bench_mod.DATASET}: {', '.join(unknown)}")

    print(f"fetching {len(instances)} instance image(s) — pulled where published, "
          f"built otherwise; both harness cells reuse them", flush=True)
    bench_mod.ensure_images(ids, args.workers)
    missing = [i.instance_id for i in instances if not bench_mod.image_exists(i)]
    print(f"ready: {len(instances) - len(missing)}/{len(instances)}")
    if missing:
        print("missing:", ", ".join(missing))
    return 1 if missing else 0


def cmd_run(args) -> int:
    model = args.model or harness_mod.DEFAULT_MODELS[args.harness]
    tag = f"{time.strftime('%Y%m%d-%H%M%S')}-{args.harness}-{_slug(model)}"
    run_root = Path(args.out).resolve() / tag
    (run_root / "workspaces").mkdir(parents=True, exist_ok=True)

    chosen = bench_mod.subset(bench_mod.load_instances(), args.size, args.seed)
    if args.instance:
        chosen = [i for i in chosen if i.instance_id in args.instance] or \
                 bench_mod.load_instances(args.instance)

    predictions = run_root / "predictions.jsonl"
    records = []
    with predictions.open("w", encoding="utf-8") as out:
        for index, inst in enumerate(chosen, 1):
            workspace = run_root / "workspaces" / inst.instance_id
            print(f"[{index}/{len(chosen)}] {inst.instance_id} ({inst.repo})", flush=True)
            container = None
            try:
                container = bench_mod.prepare(inst, workspace, tag[-8:])
            except Exception as exc:
                print(f"    prepare failed: {exc}", flush=True)
                continue

            try:
                record = harness_mod.run(
                    args.harness, [inst.prompt()], workspace, model,
                    max_turns=args.max_turns, no_shell=False, effort=args.effort,
                )
                patch = bench_mod.extract_patch(inst, workspace)
                leaks = bench_mod.audit_network(record)
                out.write(json.dumps({
                    "instance_id": inst.instance_id,
                    "model_name_or_path": f"{args.harness}-{_slug(model)}",
                    "model_patch": patch,
                }) + "\n")
                out.flush()
                records.append({
                    "instance_id": inst.instance_id, "repo": inst.repo,
                    "empty_patch": not patch.strip(),
                    "patch_bytes": len(patch),
                    "network_flags": leaks,
                    **record.as_dict(),
                })
                flag = " NETWORK?" if leaks else ""
                print(f"    {len(patch)} byte patch · {record.turns} turns · "
                      f"{record.duration_s:.0f}s{flag}", flush=True)
            finally:
                if container and not args.keep:
                    bench_mod.teardown(container)

    (run_root / "records.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    (run_root / "meta.json").write_text(json.dumps({
        "harness": args.harness, "model": model, "effort": args.effort,
        "seed": args.seed, "size": args.size, "instances": [i.instance_id for i in chosen],
    }, indent=2), encoding="utf-8")
    print(f"\npredictions: {predictions}")
    print(f"grade with: python -m swecompare grade --run-dir {run_root}")
    return 0


def cmd_grade(args) -> int:
    run_root = Path(args.run_dir).resolve()
    predictions = run_root / "predictions.jsonl"
    if not predictions.exists():
        raise SystemExit(f"no predictions at {predictions}")
    run_id = f"swecmp-{run_root.name}"
    argv = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", bench_mod.DATASET,
        "--predictions_path", str(predictions),
        "--max_workers", str(args.workers),
        "--run_id", run_id,
    ]
    print(" ".join(argv), flush=True)
    subprocess.run(argv, cwd=run_root)

    reports = sorted(run_root.glob(f"*{run_id}.json"))
    if not reports:
        print("no report produced — check the evaluation output above")
        return 1
    document = json.loads(reports[-1].read_text(encoding="utf-8"))
    (run_root / "report.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    _print_report(run_root, document)
    return 0


def _print_report(run_root: Path, document: dict) -> None:
    meta = json.loads((run_root / "meta.json").read_text(encoding="utf-8"))
    resolved = len(document.get("resolved_ids", []))
    # NOT `total_instances` — that is the size of the whole 500-instance dataset,
    # so a perfect 1-instance run printed "1/500 = 0%". The denominator is what
    # this cell was actually asked to do.
    total = (document.get("submitted_instances")
             or len(meta.get("instances", [])) or 1)
    p, low, high = wilson(resolved, total)
    label = f"{meta['harness']}/{meta['model']}" + (
        f" effort={meta['effort']}" if meta.get("effort") else "")
    print(f"\n{label}")
    print(f"  resolved {resolved}/{total} = {p:.0%}  (95% CI {low:.0%}-{high:.0%})")
    for key in ("unresolved_ids", "empty_patch_ids", "error_ids"):
        ids = document.get(key) or []
        if ids:
            print(f"  {key}: {len(ids)}")

    records = json.loads((run_root / "records.json").read_text(encoding="utf-8"))
    flagged = [r for r in records if r.get("network_flags")]
    if flagged:
        print(f"  NETWORK AUDIT: {len(flagged)} instance(s) ran shell commands that "
              f"look network-shaped — inspect before trusting the score")
        for r in flagged[:5]:
            print(f"    {r['instance_id']}: {r['network_flags'][0][:110]}")


def cmd_report(args) -> int:
    root = Path(args.out).resolve()
    rows = []
    for report in sorted(root.glob("*/report.json")):
        document = json.loads(report.read_text(encoding="utf-8"))
        meta = json.loads((report.parent / "meta.json").read_text(encoding="utf-8"))
        resolved = len(document.get("resolved_ids", []))
        total = (document.get("submitted_instances")
                 or len(meta.get("instances", [])) or 1)
        p, low, high = wilson(resolved, total)
        rows.append((f"{meta['harness']}/{meta['model']}"
                     + (f" e={meta['effort']}" if meta.get("effort") else ""),
                     f"{resolved}/{total}", f"{p:.0%}", f"{low:.0%}-{high:.0%}"))
    if not rows:
        print("no graded runs yet")
        return 0
    width = max(len(r[0]) for r in rows)
    print(f"{'cell'.ljust(width)}  resolved  rate  95% CI")
    for row in rows:
        print(f"{row[0].ljust(width)}  {row[1]:>8}  {row[2]:>4}  {row[3]}")
    print("\nOverlapping intervals mean no detectable difference at this N.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swecompare", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--size", type=int, default=DEFAULT_SIZE)
        p.add_argument("--seed", type=int, default=1)

    s = sub.add_parser("subset", help="show the stratified subset")
    common(s)
    s.add_argument("--ids", action="store_true", help="also list instance ids")
    s.set_defaults(func=cmd_subset)

    im = sub.add_parser("images", help="build the instance images once, up front")
    common(im)
    im.add_argument("--instance", action="append")
    im.add_argument("--workers", type=int, default=2)
    im.set_defaults(func=cmd_images)

    r = sub.add_parser("run", help="run a harness over the subset, collecting patches")
    common(r)
    r.add_argument("--harness", choices=harness_mod.HARNESSES, required=True)
    r.add_argument("-m", "--model", default=None)
    r.add_argument("--effort", default=None,
                   help="claude only: low/medium/high/xhigh/max")
    r.add_argument("--instance", action="append",
                   help="run only these instance ids (repeatable)")
    r.add_argument("--max-turns", type=int, default=MAX_TURNS)
    r.add_argument("--keep", action="store_true", help="leave containers running")
    r.add_argument("--out", default=str(DEFAULT_RUNS))
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("grade", help="score a run with the official swebench harness")
    g.add_argument("--run-dir", required=True)
    g.add_argument("--workers", type=int, default=2)
    g.set_defaults(func=cmd_grade)

    rep = sub.add_parser("report", help="compare every graded run")
    rep.add_argument("--out", default=str(DEFAULT_RUNS))
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
