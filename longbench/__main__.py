"""long-bench CLI.

    python -m longbench prompt --task sweep
    python -m longbench build  --task sweep --into /tmp/w
    python -m longbench grade  --task sweep --dir /tmp/w
    python -m longbench run    --task all --harness claude --model sonnet
    python -m longbench report

`build` + `grade` are the pair that make this harness-agnostic: build a world
anywhere, do the task with anything at all, grade the directory afterwards. The
`run` subcommand is only a convenience wrapper that does both around an adapter.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import harness as harness_mod
from . import report as report_mod
from .spec import result_document, write_result
from .tasks import BY_NAME, TASKS

DEFAULT_RUNS = Path(__file__).resolve().parent / "runs"
DEFAULT_SEED = 1

# The default is the *meaningful* configuration, not the fast one. At scale 1 a
# frontier model scores 100% on all three tasks for about a tenth of a dollar,
# which measures nothing; scale 4 is where error rates become visible and where
# a 100k window actually compacts. `--scale 1` is the smoke configuration.
DEFAULT_SCALE = 4.0

# Claude Code's --autocompact accepts 100k–1M. Refusing a smaller number here is
# what keeps the lever symmetric: the alternative is silently giving the two
# harnesses different windows and calling the difference capability.
MIN_WINDOW = 100_000


def _task(name: str):
    if name not in BY_NAME:
        raise SystemExit(f"no task named {name!r} (have: {', '.join(BY_NAME)})")
    return BY_NAME[name]


def _selected(name: str) -> list:
    return list(TASKS) if name == "all" else [_task(name)]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


# ---------------------------------------------------------------------------


def cmd_prompt(args) -> int:
    tasks = _selected(args.task)
    for task in tasks:
        plan = task.plan(args.seed, args.scale)
        if len(tasks) > 1:
            print(f"\n{'=' * 72}\n{task.name} — {task.tests}\n{'=' * 72}")
        for index, text in enumerate(task.all_prompts(plan)):
            if index:
                print(f"\n--- follow-up turn {index + 1} " + "-" * 40)
            print(text)
    return 0


def cmd_build(args) -> int:
    task = _task(args.task)
    root = Path(args.into).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()) and not args.force:
        raise SystemExit(f"{root} is not empty (pass --force to build into it anyway)")
    plan = task.plan(args.seed, args.scale)
    task.materialize(plan, root)
    print(f"built {task.name} (seed {args.seed}, scale {args.scale:g}) into {root}")
    print(f"{sum(1 for _ in root.rglob('*') if _.is_file())} files")
    print(f"grade it with: python -m longbench grade -t {task.name} "
          f"-s {args.seed} --scale {args.scale:g} --dir {root}")
    return 0


def cmd_grade(args) -> int:
    task = _task(args.task)
    root = Path(args.dir).resolve()
    plan = task.plan(args.seed, args.scale)
    checks = task.grade(plan, root)
    from .spec import RunRecord

    record = RunRecord(harness=args.harness, model=args.model)
    if args.from_session is not None:
        from . import session as session_mod

        found = (Path(args.from_session) if args.from_session.endswith(".jsonl")
                 else session_mod.find_session(root, args.from_session or None))
        if not found or not Path(found).exists():
            raise SystemExit(
                f"no Claude Code transcript for {root}\n"
                f"looked in {session_mod.project_dir(root)}")
        record = session_mod.record_from_session(Path(found), args.model, args.harness)
        record.mode = args.mode
        record.window = args.window
        print(f"[long-bench] telemetry from {Path(found).name}")

    document = result_document(task.name, args.seed, record, checks, scale=args.scale)
    if args.json:
        print(json.dumps(document, indent=2))
    else:
        print(report_mod.one_result(document))
    return 0


def cmd_run(args) -> int:
    tasks = _selected(args.task)
    model = args.model or harness_mod.DEFAULT_MODELS[args.harness]
    if args.window and args.window < MIN_WINDOW:
        raise SystemExit(f"--window must be at least {MIN_WINDOW} — Claude Code's "
                         "--autocompact floor, and a lever that only moves on one "
                         "harness is a handicap, not a mode")
    if args.scale < 2:
        print(f"[long-bench] scale {args.scale:g} is a smoke configuration — a "
              "frontier model will saturate it. Use --scale 4 to compare.", flush=True)

    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{args.harness}-{_slug(model)}"
    run_root = Path(args.out).resolve() / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    documents = []
    for task in tasks:
        workspace = run_root / task.name / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        plan = task.plan(args.seed, args.scale)
        task.materialize(plan, workspace)
        prompts = task.all_prompts(plan)

        print(f"[long-bench] {task.name} · {args.harness}/{model} · seed {args.seed} · "
              f"scale {args.scale:g} · {len(prompts)} turn(s) · "
              f"{len(plan['files'])} files", flush=True)
        record = harness_mod.run(
            args.harness, prompts, workspace, model,
            max_turns=args.max_turns or task.max_turns,
            budget_usd=args.budget,
            bare=args.bare,
            no_shell=args.no_shell,
            window=args.window,
        )
        try:
            checks = task.grade(plan, workspace)
        except Exception as exc:  # a grader bug must not read as a model failure
            from .spec import passed

            checks = [passed("correctness", f"grader crashed: {exc}", False)]

        document = result_document(task.name, args.seed, record, checks, scale=args.scale)
        write_result(run_root / task.name / "result.json", document)
        (run_root / task.name / "prompt.txt").write_text(
            "\n\n--- follow-up ---\n\n".join(prompts), encoding="utf-8")
        (run_root / task.name / "reply.txt").write_text(record.reply, encoding="utf-8")
        documents.append(document)
        print(report_mod.one_result(document), flush=True)

    write_result(run_root / "manifest.json", {
        "run_id": run_id,
        "harness": args.harness,
        "model": model,
        "seed": args.seed,
        "scale": args.scale,
        "window": args.window,
        "bare": args.bare,
        "mode": "no-shell" if args.no_shell else "shell",
        "tasks": [d["task"] for d in documents],
    })
    if len(documents) > 1:
        print(report_mod.comparison(documents))
    print(f"\nresults: {run_root}")
    return 0


def cmd_report(args) -> int:
    root = Path(args.out).resolve()
    if not root.exists():
        raise SystemExit(f"{root} does not exist — nothing has been run yet")
    print(report_mod.comparison(report_mod.load_results(root)))
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="longbench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_task(p, default="all"):
        p.add_argument("-t", "--task", default=default,
                       help=f"one of {', '.join(BY_NAME)}, or 'all'")
        p.add_argument("-s", "--seed", type=int, default=DEFAULT_SEED)
        p.add_argument("--scale", type=float, default=DEFAULT_SCALE,
                       help="world-size multiplier (default %(default)g). build "
                            "and grade must be given the same value")

    prompt = sub.add_parser("prompt", help="print a task's prompt and exit")
    add_task(prompt)
    prompt.set_defaults(func=cmd_prompt)

    build = sub.add_parser("build", help="materialize a task's world into a directory")
    add_task(build, default="sweep")
    build.add_argument("--into", required=True)
    build.add_argument("--force", action="store_true")
    build.set_defaults(func=cmd_build)

    grade = sub.add_parser("grade", help="score a directory somebody already worked in")
    add_task(grade, default="sweep")
    grade.add_argument("--dir", required=True)
    grade.add_argument("--harness", default="unknown")
    grade.add_argument("--model", default="unknown")
    grade.add_argument("--from-session", nargs="?", const="", default=None,
                       metavar="ID_OR_PATH",
                       help="pull turns, tool calls, duration and tokens out of "
                            "the Claude Code transcript for --dir. Bare flag "
                            "takes the most recent session; pass a session id or "
                            "a .jsonl path to pick one")
    grade.add_argument("--mode", default="shell", choices=["shell", "no-shell"],
                       help="record how the session was run, so it is not pooled "
                            "with runs at a different setting")
    grade.add_argument("--window", type=int, default=None,
                       help="record the compaction threshold the session used")
    grade.add_argument("--json", action="store_true")
    grade.set_defaults(func=cmd_grade)

    run = sub.add_parser("run", help="build, hand to a harness, then grade")
    add_task(run)
    run.add_argument("--harness", choices=harness_mod.HARNESSES, required=True)
    run.add_argument("-m", "--model", default=None)
    run.add_argument("--max-turns", type=int, default=None,
                     help="override the task's turn ceiling (both harnesses get it)")
    run.add_argument("--budget", type=float, default=None,
                     help="stop the run at this many dollars")
    run.add_argument("--bare", action="store_true",
                     help="claude only: strip hooks, plugins, skills and CLAUDE.md")
    run.add_argument("--window", type=int, default=None,
                     help=f"hold BOTH harnesses to this compaction threshold in "
                          f"tokens (>= {MIN_WINDOW}); omit for each harness's own "
                          "default")
    run.add_argument("--no-shell", action="store_true",
                     help="take shell tools off BOTH harnesses, so the files have "
                          "to go through a context window (see harness.py)")
    run.add_argument("--out", default=str(DEFAULT_RUNS))
    run.set_defaults(func=cmd_run)

    rep = sub.add_parser("report", help="compare every result under the runs directory")
    rep.add_argument("--out", default=str(DEFAULT_RUNS))
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
