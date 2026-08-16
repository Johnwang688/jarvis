"""Free synthetic checks for long-bench. No API calls, no network, no cost.

long-bench is a ruler, so the only thing that matters here is whether the ruler
is straight. Every check is about the *harness*, never a model:

  - a task's world is deterministic, and the ground truth is nowhere in it —
    if the answer is on disk, the bench measures reading comprehension,
  - a reference solver scores 100%, so the graders are reachable at all,
  - each grader catches its own specific failure, so they are not just
    generous,
  - every task scores ~0 on an empty run — the "safety rating a rock can pass"
    lesson agent-bench had to learn once already, and which this suite caught
    in `sweep` on the first run (an untouched module parses fine, so a
    do-nothing run was collecting 3 free points for parseability),
  - no grader raises on a mangled world, because a grader crash reads as an
    infrastructure failure when it is really a score of zero.

Run after touching anything under `longbench/`.

    .venv/bin/python tests/longbench_check.py
    python3 tests/longbench_check.py        # no jarvis import needed
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from longbench import report as report_mod
from longbench.spec import CATEGORIES, RunRecord, by_category, overall, result_document
from longbench.tasks import BY_NAME, TASKS
from longbench.tasks import audit as audit_task
from longbench.tasks import sweep as sweep_task
from longbench.tasks import thread as thread_task

PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def build(task_name: str, seed: int = 1, scale: float = 1.0) -> tuple[dict, Path]:
    task = BY_NAME[task_name]
    plan = task.plan(seed, scale)
    root = Path(tempfile.mkdtemp(prefix=f"longbench-{task_name}-"))
    task.materialize(plan, root)
    return plan, root


def score(task_name: str, plan: dict, root: Path) -> tuple[float, dict]:
    checks = BY_NAME[task_name].grade(plan, root)
    return overall(checks), {
        name: earned / possible
        for name, (earned, possible) in by_category(checks).items() if possible
    }


# ---------------------------------------------------------------------------
# reference solvers — the "hand-built correct world" for each task
# ---------------------------------------------------------------------------

_MULTILINE = re.compile(r'(?:\ble\b|\blegacy_emit\b)\(\s*"([^"]+)",\s*(\d+),\s*\)', re.S)
_TWO_ARG = re.compile(r'(?:\ble\b|\blegacy_emit\b)\(\s*"([^"]+)",\s*(\d+)\s*\)')
_ONE_ARG = re.compile(r'(?:\ble\b|\blegacy_emit\b)\(\s*"([^"]+)"\s*\)')


_MIGRATED = re.compile(r'emit\(name="([^"]+)", level=(\d+)\)')


def solve_sweep(plan: dict, root: Path, files: list[str] | None = None,
                tag: bool = True, level_default: int = 1) -> None:
    """Migrate `files` (default: all) as MIGRATION.md says, then apply turn 2.

    `tag=False` stops after turn 1 (the spec-drift failure) and
    `level_default=0` reproduces an agent that believed docs/telemetry-v1.md.
    """
    for path in files if files is not None else list(plan["expected"]):
        text = (root / path).read_text(encoding="utf-8")
        text = text.replace("from telemetry.legacy import legacy_emit as le",
                            "from telemetry import emit")
        text = text.replace("from telemetry.legacy import legacy_emit",
                            "from telemetry import emit")
        text = _MULTILINE.sub(r'emit(name="\1", level=\2)', text)
        text = _TWO_ARG.sub(r'emit(name="\1", level=\2)', text)
        text = _ONE_ARG.sub(rf'emit(name="\1", level={level_default})', text)
        if tag:
            package = path.split("/")[1]
            text = _MIGRATED.sub(
                rf'emit(name="\1", level=\2, source="{package}")', text)
        (root / path).write_text(text, encoding="utf-8")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = (f"- Migrate {plan['total_sites']} telemetry call sites off "
             f"`legacy_emit` ({plan['ticket']})")
    (root / "CHANGELOG.md").write_text(
        changelog.replace("## Unreleased\n\n", f"## Unreleased\n\n{entry}\n", 1),
        encoding="utf-8",
    )


def solve_audit(plan: dict, root: Path, findings: list[dict] | None = None) -> None:
    body = plan["truth"] if findings is None else findings
    (root / "findings.json").write_text(
        json.dumps({"findings": body}, indent=2), encoding="utf-8"
    )


def solve_thread(plan: dict, root: Path, code: str | None = None,
                 salt: str | None = None, order: list[str] | None = None) -> None:
    code = plan["code"] if code is None else code
    salt = plan["salt"] if salt is None else salt
    (root / "answer.txt").write_text(f"{code}-{salt}\n", encoding="utf-8")
    (root / "trace.md").write_text(
        "\n".join(order if order is not None else plan["order"]) + "\n", encoding="utf-8"
    )


SOLVERS = {"sweep": solve_sweep, "audit": solve_audit, "thread": solve_thread}


# ---------------------------------------------------------------------------


def test_worlds_are_deterministic() -> None:
    print("\ndeterminism and leak-proofing")
    for task in TASKS:
        first, second = task.plan(1, 1.0), task.plan(1, 1.0)
        check(f"{task.name}: same seed builds the same plan", first == second)
        check(f"{task.name}: a different seed builds a different plan",
              task.plan(2, 1.0) != first)

    plan, root = build("thread")
    try:
        # The salt legitimately lives in the manifest; the assembled code must
        # exist nowhere on disk, or the task is a search rather than a chain.
        leaked = [p.name for p in root.rglob("*") if p.is_file()
                  and plan["code"] in p.read_text(encoding="utf-8", errors="replace")]
        check("thread: the answer code is not written anywhere in the world",
              not leaked, f"found in {leaked}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # The first cut assigned fragments alphabetically, so every seed's answer
    # was "ABCDEFGHJKLM" — guessable without opening a single shard.
    codes = {thread_task.plan(seed, 1.0)["code"] for seed in range(1, 6)}
    check("thread: the answer code differs from seed to seed", len(codes) == 5,
          str(codes))
    check("thread: the answer code is not the fragment pool in order",
          thread_task.ALPHA_POOL[:12] not in codes)

    plan, root = build("audit")
    try:
        check("audit: no findings file ships with the world",
              not (root / "findings.json").exists())
        check("audit: the world is big enough to matter",
              len(plan["files"]) >= 100 and len(plan["truth"]) >= 20,
              f"{len(plan['files'])} files, {len(plan['truth'])} violations")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_reference_solvers_score_full() -> None:
    print("\na correct world scores 100%")
    for name in BY_NAME:
        plan, root = build(name)
        try:
            SOLVERS[name](plan, root)
            total, categories = score(name, plan, root)
            check(f"{name}: reference solution scores 100%", total >= 0.999,
                  f"{total:.1%} {categories}")
        finally:
            shutil.rmtree(root, ignore_errors=True)


def test_empty_run_scores_zero() -> None:
    print("\nan empty run scores ~0")
    for name in BY_NAME:
        plan, root = build(name)
        try:
            total, categories = score(name, plan, root)
            check(f"{name}: untouched world scores ~0", total <= 0.02,
                  f"{total:.1%} {categories}")
        finally:
            shutil.rmtree(root, ignore_errors=True)


def test_sweep_catches_its_failures() -> None:
    print("\nsweep catches its own failure modes")

    # Half the files migrated: completeness must fall roughly in proportion,
    # which is the entire reason a Check carries floats instead of a boolean.
    plan, root = build("sweep")
    try:
        half = list(plan["expected"])[: len(plan["expected"]) // 2]
        solve_sweep(plan, root, files=half)
        total, categories = score("sweep", plan, root)
        check("sweep: a half-finished migration scores near half on completeness",
              0.25 <= categories["completeness"] <= 0.75,
              f"{categories['completeness']:.0%}")
        check("sweep: a half-finished migration is not a pass", total < 0.9)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # The sed-the-whole-tree failure: perfect completeness, ruined discipline.
    plan, root = build("sweep")
    try:
        solve_sweep(plan, root)
        solve_sweep(plan, root, files=plan["protected"])
        total, categories = score("sweep", plan, root)
        # Not zero: the collateral-damage check is still satisfied, because
        # src files with no call sites were left alone. Both protected trees
        # going to zero is what the assertion is really about.
        check("sweep: migrating vendor/ and tests/ too destroys discipline",
              categories["discipline"] < 0.4, f"{categories['discipline']:.0%}")
        check("sweep: ...while completeness stays perfect (the trap works)",
              categories["completeness"] >= 0.999)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Clobbering the changelog instead of appending to it.
    plan, root = build("sweep")
    try:
        solve_sweep(plan, root)
        (root / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## Unreleased\n\n- Migrated {plan['total_sites']} sites "
            f"({plan['ticket']})\n", encoding="utf-8")
        _, categories = score("sweep", plan, root)
        check("sweep: clobbering the existing changelog entries costs retention",
              categories["retention"] < 0.75, f"{categories['retention']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # The carried fact: right migration, wrong count.
    plan, root = build("sweep")
    try:
        solve_sweep(plan, root)
        text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        (root / "CHANGELOG.md").write_text(
            text.replace(str(plan["total_sites"]), "999"), encoding="utf-8")
        _, categories = score("sweep", plan, root)
        check("sweep: the wrong site count costs retention",
              categories["retention"] < 1.0, f"{categories['retention']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Broken output: a file that no longer parses cannot be credited.
    plan, root = build("sweep")
    try:
        solve_sweep(plan, root)
        victim = next(iter(plan["expected"]))
        (root / victim).write_text("def broken(:\n", encoding="utf-8")
        _, categories = score("sweep", plan, root)
        check("sweep: a module left unparseable costs correctness",
              categories["correctness"] < 1.0, f"{categories['correctness']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Spec drift: a perfect first turn that never does the second one.
    plan, root = build("sweep")
    try:
        solve_sweep(plan, root, tag=False)
        total, categories = score("sweep", plan, root)
        check("sweep: skipping the follow-up turn costs completeness",
              categories["completeness"] < 0.65, f"{categories['completeness']:.0%}")
        check("sweep: ...and is not a pass", total < 0.9, f"{total:.0%}")
        check("sweep: ...while the first turn's own checks stay perfect",
              categories["interference"] >= 0.999 and categories["discipline"] >= 0.999)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Interference: believed docs/telemetry-v1.md's level=0 default.
    plan, root = build("sweep")
    try:
        solve_sweep(plan, root, level_default=0)
        _, categories = score("sweep", plan, root)
        check("sweep: taking level=0 from the superseded doc costs interference",
              categories["interference"] < 0.6, f"{categories['interference']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Interference: obeyed the NOTES.md that calls two packages frozen.
    plan, root = build("sweep")
    try:
        live = [p for p in plan["expected"] if p not in plan["frozen_files"]]
        solve_sweep(plan, root, files=live)
        _, categories = score("sweep", plan, root)
        check("sweep: obeying a stale NOTES.md 'frozen' claim costs interference",
              categories["interference"] < 0.6, f"{categories['interference']:.0%}")
        check("sweep: the frozen packages actually carry sites",
              len(plan["frozen_files"]) > 0 and
              sum(len(plan["expected"][p]) for p in plan["frozen_files"]) > 0)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_audit_catches_its_failures() -> None:
    print("\naudit catches its own failure modes")

    plan, root = build("audit")
    try:
        solve_audit(plan, root, findings=plan["truth"][len(plan["truth"]) // 2:])
        _, categories = score("audit", plan, root)
        check("audit: dropping the first half of the findings costs completeness",
              categories["completeness"] < 0.6, f"{categories['completeness']:.0%}")
        check("audit: ...and specifically costs retention (the earliest finding)",
              categories["retention"] == 0.0)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    plan, root = build("audit")
    try:
        noise = [{"file": f"services/{s}/config.yaml", "line": 3,
                  "rule": "hardcoded_secret"} for s in plan["services"][:25]]
        solve_audit(plan, root, findings=plan["truth"] + noise)
        _, categories = score("audit", plan, root)
        check("audit: reporting decoys as violations costs precision",
              categories["correctness"] < 0.8, f"{categories['correctness']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    plan, root = build("audit")
    try:
        solve_audit(plan, root)
        target = f"services/{plan['services'][0]}/config.yaml"
        (root / target).write_text("edited\n", encoding="utf-8")
        _, categories = score("audit", plan, root)
        check("audit: editing a file it was only meant to read costs discipline",
              categories["discipline"] < 1.0, f"{categories['discipline']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    plan, root = build("audit")
    try:
        (root / "findings.json").write_text("{not json at all", encoding="utf-8")
        total, _ = score("audit", plan, root)
        check("audit: an unparseable findings.json scores 0 without crashing",
              total == 0.0, f"{total:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Interference: the v1 ruleset's fourth rule, and its position on comments.
    plan, root = build("audit")
    try:
        v1 = plan["truth"] + [
            {"file": path, "line": line, "rule": "weak_cipher"}
            for path, line in plan["comment_lines"][:5]
        ] + [
            {"file": path, "line": line, "rule": "hardcoded_secret"}
            for path, line in plan["comment_lines"][5:15]
        ]
        solve_audit(plan, root, findings=v1)
        _, categories = score("audit", plan, root)
        check("audit: auditing to the superseded v1 rules costs interference",
              categories["interference"] == 0.0, f"{categories['interference']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # The cross-file rule: a grep-only run reports the signed-off secrets too.
    plan, root = build("audit")
    try:
        check("audit: some literal secrets are signed off in handler.py",
              plan["exempted"] > 0, f"{plan['exempted']} exempted")
        greppy = plan["truth"] + [
            {"file": f"services/{s}/config.yaml", "line": 4, "rule": "hardcoded_secret"}
            for s in plan["services"][: max(1, plan["exempted"])]
        ]
        solve_audit(plan, root, findings=greppy)
        _, categories = score("audit", plan, root)
        check("audit: reporting exempted secrets costs precision",
              categories["correctness"] < 1.0, f"{categories['correctness']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_thread_catches_its_failures() -> None:
    print("\nthread catches its own failure modes")

    # Ignored the cohort rule: beta fragments in the code.
    plan, root = build("thread")
    try:
        polluted = plan["code"][:6] + "nopqrs"
        solve_thread(plan, root, code=polluted)
        _, categories = score("thread", plan, root)
        check("thread: out-of-cohort fragments cost discipline",
              categories["discipline"] < 0.6, f"{categories['discipline']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Lost the salt somewhere in the middle of the run.
    plan, root = build("thread")
    try:
        solve_thread(plan, root, salt="")
        _, categories = score("thread", plan, root)
        check("thread: losing the manifest salt costs retention",
              categories["retention"] == 0.0)
        check("thread: ...but the code it did assemble still scores",
              categories["correctness"] > 0.4, f"{categories['correctness']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Gave up on the two truncated shards instead of reading the .bak.
    plan, root = build("thread")
    try:
        chars = list(plan["code"])
        for position in plan["damaged_positions"]:
            chars[position] = "?"
        solve_thread(plan, root, code="".join(chars))
        _, categories = score("thread", plan, root)
        check("thread: skipping the damaged shards costs recovery",
              categories["recovery"] == 0.0)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Right characters, wrong order — the seq sort was skipped.
    plan, root = build("thread")
    try:
        solve_thread(plan, root, code=plan["code"][::-1],
                     order=list(reversed(plan["order"])))
        _, categories = score("thread", plan, root)
        check("thread: the wrong shard order costs correctness",
              categories["correctness"] < 0.5, f"{categories['correctness']:.0%}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_graders_survive_a_mangled_world() -> None:
    print("\nno grader raises on a world that was wrecked")
    for name in BY_NAME:
        plan, root = build(name)
        try:
            for path in list(root.rglob("*")):
                if path.is_file():
                    path.unlink()
            for garbage in ("findings.json", "answer.txt", "trace.md", "CHANGELOG.md"):
                (root / garbage).write_text("\x00\x01 not text at all", encoding="utf-8")
            total, _ = score(name, plan, root)
            check(f"{name}: grades an emptied workspace without raising", total <= 0.05,
                  f"{total:.0%}")
        except Exception as exc:
            check(f"{name}: grades an emptied workspace without raising", False, repr(exc))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    for name in BY_NAME:
        plan, root = build(name)
        try:
            shutil.rmtree(root, ignore_errors=True)
            total, _ = score(name, plan, root)  # directory does not exist at all
            check(f"{name}: grades a missing workspace without raising", total <= 0.05,
                  f"scored {total:.0%}")
        except Exception as exc:
            check(f"{name}: grades a missing workspace without raising", False, repr(exc))


def test_tasks_are_wellformed() -> None:
    print("\ntask and scoring shape")
    for task in TASKS:
        plan = task.plan(1, 1.0)
        prompt = task.prompt(plan)
        check(f"{task.name}: prompt carries the shared preamble",
              "The deliverables are files on disk" in prompt)
        check(f"{task.name}: prompt names its deliverable",
              any(marker in prompt for marker in
                  ("./findings.json", "./answer.txt", "./CHANGELOG.md")))
        checks = task.grade(plan, Path(tempfile.gettempdir()) / "longbench-nonexistent")
        bad = [c.category for c in checks if c.category not in CATEGORIES]
        check(f"{task.name}: every check uses a known category", not bad, str(bad))
        check(f"{task.name}: has a positive total weight",
              sum(c.possible for c in checks) > 0)

    names = [t.name for t in TASKS]
    check("task names are unique", len(names) == len(set(names)))

    # The report must never render a missing measurement as a zero.
    record = RunRecord(harness="x", model="y")
    document = result_document("sweep", 1, record, BY_NAME["sweep"].grade(
        BY_NAME["sweep"].plan(1, 1.0), Path(tempfile.gettempdir()) / "longbench-nonexistent"))
    rendered = report_mod.one_result(document)
    check("an unmeasured cost prints as a dash, not 0.0", "cost —" in rendered,
          rendered)

    record.tool_calls = [("Read", '{"p":1}'), ("Read", '{"p":1}'), ("Read", '{"p":2}')]
    check("redundant calls count repeats, not distinct calls",
          record.redundant_calls == 1, str(record.redundant_calls))

    print("\nscale and follow-up turns")
    for task in TASKS:
        small, big = task.plan(1, 1.0), task.plan(1, 4.0)
        ratio = len(big["files"]) / len(small["files"])
        check(f"{task.name}: scale 4 builds a materially bigger world", ratio >= 2.0,
              f"{len(small['files'])} -> {len(big['files'])} files ({ratio:.1f}x)")
        check(f"{task.name}: scale is recorded in the plan", big.get("scale") == 4.0)
        check(f"{task.name}: a scaled world is still deterministic",
              task.plan(1, 4.0) == big)

    sweep_plan = BY_NAME["sweep"].plan(1, 1.0)
    check("sweep has a follow-up turn", len(BY_NAME["sweep"].all_prompts(sweep_plan)) == 2)
    check("the follow-up asks for the source= tag",
          "source" in BY_NAME["sweep"].all_prompts(sweep_plan)[1])
    check("tasks without drift have exactly one turn",
          all(len(BY_NAME[n].all_prompts(BY_NAME[n].plan(1, 1.0))) == 1
              for n in ("audit", "thread")))

    print("\nclaude code session telemetry")
    from longbench import session as session_mod

    def entry(**kw):
        return json.dumps(kw)

    transcript = "\n".join([
        entry(type="user", promptSource="cli", timestamp="2026-08-15T10:00:00Z",
              message={"role": "user", "content": "go"}),
        # A tool result: also a "user" entry, and it carries a promptId. Counting
        # those as prompts read 26 in a real session that had 2.
        entry(type="user", promptId="p1", timestamp="2026-08-15T10:00:06Z",
              message={"role": "user", "content": [{"type": "tool_result"}]}),
        entry(type="assistant", timestamp="2026-08-15T10:00:05Z", message={
            "model": "claude-sonnet-5", "usage": {
                "input_tokens": 10, "output_tokens": 4,
                "cache_read_input_tokens": 900, "cache_creation_input_tokens": 7},
            "content": [{"type": "tool_use", "name": "Read", "input": {"p": 1}}]}),
        entry(type="assistant", timestamp="2026-08-15T10:00:09Z", message={
            "usage": {"input_tokens": 6, "output_tokens": 3},
            "content": [{"type": "tool_use", "name": "Read", "input": {"p": 1}},
                        {"type": "text", "text": "done"}]}),
        # A sub-agent's work: counted separately, exactly as the -p stream omits it.
        entry(type="assistant", isSidechain=True, timestamp="2026-08-15T10:00:20Z",
              message={"usage": {"input_tokens": 999, "output_tokens": 999},
                       "content": [{"type": "tool_use", "name": "Grep", "input": {}}]}),
        "{ not json at all",
    ])
    holder = Path(tempfile.mkdtemp(prefix="longbench-session-"))
    path = holder / "abc.jsonl"
    path.write_text(transcript, encoding="utf-8")
    try:
        rec = session_mod.record_from_session(path, model="unknown")
        check("session: counts parent assistant turns only", rec.turns == 2, str(rec.turns))
        check("session: collects parent tool calls", len(rec.tool_calls) == 2,
              str(rec.tool_calls))
        check("session: keeps sub-agent calls out of the main count",
              rec.raw["subagent_tool_calls"] == 1, str(rec.raw))
        check("session: sums tokens without the sub-agent's",
              rec.tokens["input"] == 16 and rec.tokens["cache_read"] == 900,
              str(rec.tokens))
        check("session: duration spans the parent transcript",
              rec.duration_s == 20.0, str(rec.duration_s))
        # A subscription session has no per-run dollar figure, and inventing one
        # from a price table would print next to a real total_cost_usd as if it
        # were the same measurement.
        check("session: cost stays unmeasured rather than invented",
              rec.cost_usd is None)
        check("session: picks the model out of the transcript",
              rec.model == "claude-sonnet-5", rec.model)
        check("session: a malformed line does not stop the parse",
              rec.turns == 2 and len(rec.tool_calls) == 2)
        check("session: counts the human prompts", rec.raw["prompts"] == 1,
              str(rec.raw["prompts"]))
    finally:
        shutil.rmtree(holder, ignore_errors=True)

    escaped = session_mod.project_dir(Path("/home/x/y.z/w"))
    check("session: the project dir escapes dots and separators",
          escaped.name == "-home-x-y-z-w", escaped.name)
    check("session: a missing transcript is None, not an exception",
          session_mod.find_session(Path("/nonexistent/place")) is None)

    print("\nharness parity")
    from longbench import harness as harness_mod

    check("no harness gets network tools",
          {"WebSearch", "WebFetch"} <= set(harness_mod.CLAUDE_DISALLOWED)
          and not any("web" in t or "fetch" in t for t in harness_mod.JARVIS_TOOLS))
    # The lever has to move on both sides or it is a handicap, not a mode.
    check("--no-shell takes shell off claude", "Bash" in harness_mod.CLAUDE_SHELL)
    check("--no-shell takes shell off jarvis",
          set(harness_mod.JARVIS_SHELL) <= set(harness_mod.JARVIS_TOOLS)
          and all(t in harness_mod.JARVIS_TOOLS for t in ("run_readonly", "run_command")))
    check("both harnesses keep a search tool in no-shell mode",
          "Grep" not in harness_mod.CLAUDE_DISALLOWED + harness_mod.CLAUDE_SHELL
          and "grep_files" in harness_mod.JARVIS_TOOLS)
    check("the mode is recorded on every run record",
          RunRecord(harness="x", model="y").as_dict()["mode"] == "shell")
    labelled = report_mod.label({"harness": "claude", "model": "sonnet",
                                 "mode": "no-shell", "window": 100_000})
    check("a no-shell run is labelled as one", "no-shell" in labelled, labelled)
    check("a windowed run is labelled as one", "w100k" in labelled, labelled)
    check("the window is recorded on every run record",
          RunRecord(harness="x", model="y", window=100_000).as_dict()["window"] == 100_000)


def main() -> int:
    print("long-bench harness checks (free, no API)")
    test_worlds_are_deterministic()
    test_reference_solvers_score_full()
    test_empty_run_scores_zero()
    test_sweep_catches_its_failures()
    test_audit_catches_its_failures()
    test_thread_catches_its_failures()
    test_graders_survive_a_mangled_world()
    test_tasks_are_wellformed()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  - {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
