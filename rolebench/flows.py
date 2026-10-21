"""The review and implement flows. Everything that touches a model, the clock
or the daemon is a field of `Deps`, so the tests run them against fakes."""

from __future__ import annotations

import json
import random
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import audit, brief, config, findings, quota, runlog, runners, sandbox, snapshot, spend


@dataclass
class Deps:
    runners: dict = field(default_factory=lambda: dict(runners.REGISTRY))
    quota_reader: Callable = quota.daemon_reader
    # The owner's reason for running without the quota guard ("" = guard on).
    # Set only from `--no-quota-guard` plus ROLEBENCH_QUOTA_OVERRIDE.
    quota_override: str = ""
    ledger: spend.Ledger | None = None
    repo: Path | None = None
    suite_runner: Callable | None = None       # (grade_dir, rel) -> (ok, tail)
    review_timeout: int = config.REVIEW_TIMEOUT_S
    implement_timeout: int = config.IMPLEMENT_TIMEOUT_S
    log: bool = True

    def get_ledger(self) -> spend.Ledger:
        if self.ledger is None:
            self.ledger = spend.Ledger(config.ledger_path())
        return self.ledger

    @property
    def quota_state(self) -> str:
        return f"off: {self.quota_override}" if self.quota_override else "on"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def new_run_id(case_id: str, harness: str, model: str, n: int) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{case_id}-{harness}-{slug(model)}-{n}-{uuid.uuid4().hex[:4]}"


def _write(path: Path, doc) -> None:
    path.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")


# --- one model step: guard, budget, run, settle -----------------------------------


class Skipped(Exception):
    pass


def _guard(deps: Deps, harness: str, notes: list[str]) -> None:
    if deps.quota_override:
        return
    skip, note = quota.guard(harness, deps.quota_reader)
    if skip:
        raise Skipped(f"quota: {note}")
    if note:
        notes.append(note)


def settle_amount(result: runners.RunResult | None, budget: float, model: str) -> tuple[float, bool, str]:
    """(usd, abort the model, note). A run whose spend is unknown (timeout,
    OOM, crash, no result) is charged its whole reservation."""
    if result is None or result.timed_out or result.crashed:
        return budget, False, "spend unknown (timeout, crash or no result): charged the full reservation"
    tokens = result.tokens or {}
    est = spend.estimate(model, tokens.get("prompt_tokens", 0), tokens.get("completion_tokens", 0)) or 0.0
    usd = max(result.cost_usd or 0.0, result.spend_usd or 0.0, est)
    return usd, usd >= config.PER_RUN_CAP_USD - 1e-9, ""


def model_step(deps: Deps, *, harness: str, model: str, prompt: str, cwd: Path, run_id: str,
               out_dir: Path, timeout_s: int, max_turns: int, notes: list[str] | None = None) -> runners.RunResult:
    """One agent run: the quota guard (every call, so every implement round),
    the jarvis budget reserved before and settled after."""
    if harness in quota.HARNESS_PROVIDER:
        _guard(deps, harness, notes if notes is not None else [])
    runner = deps.runners[harness]
    budget = None
    ledger = deps.get_ledger() if harness == "jarvis" else None
    if ledger:
        try:
            config.price_override(model)
        except config.UnknownModel as exc:
            raise Skipped(f"refused: {exc}")
        try:
            budget = ledger.reserve(run_id, model)
        except spend.CapReached as exc:
            raise Skipped(f"spend cap: {exc}")
    result = None
    try:
        result = runner(model=model, prompt=prompt, cwd=cwd, timeout_s=timeout_s, max_turns=max_turns,
                        budget_usd=budget, out_dir=out_dir, run_id=run_id)
    except Exception as exc:                       # a runner that raises is an errored run, not a crash
        result = runners.RunResult(harness, model, error=f"{type(exc).__name__}: {exc}", crashed=True)
    finally:
        if ledger:
            usd, abort, note = settle_amount(result, budget, model)
            try:
                ledger.settle(run_id, model, usd, abort=abort, note=note)
            except spend.CapReached as exc:
                if result is not None:
                    result.error = (result.error + "; " if result.error else "") + f"ledger: {exc}"
            if result is not None:
                result.raw["settled_usd"] = round(usd, 6)
    return result


def review_step(deps: Deps, rcase: dict, snap: Path, *, harness: str, model: str, run_id: str,
                out_dir: Path, notes: list[str] | None = None) -> dict:
    """Run one reviewer in `snap`; returns the review part of a run document."""
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt = brief.review_prompt(rcase, snap, harness=harness)
    (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    result = model_step(deps, harness=harness, model=model, prompt=prompt, cwd=snap, run_id=run_id,
                        out_dir=out_dir, timeout_s=deps.review_timeout, max_turns=config.REVIEW_MAX_TURNS, notes=notes)
    parsed = findings.parse(result.text)
    doc = {**result.summary(), "flags": findings.flags(result.tool_calls, parsed),
           "findings": parsed.findings, "parse_errors": parsed.errors,
           "network_flags": audit.network_flags(result), "escape_flags": audit.escape_flags(result, snap),
           "settled_usd": result.raw.get("settled_usd")}
    cap = config.PER_RUN_CAP_USD
    doc["over_cap"] = harness == "jarvis" and max(result.cost_usd or 0, result.spend_usd or 0) >= cap
    (out_dir / "review.txt").write_text(result.text, encoding="utf-8")
    _write(out_dir / "findings.json", parsed.findings)
    doc["ok"] = bool(parsed.found_block) and not result.error
    doc["result"] = ("timeout" if result.timed_out else "error" if result.error and not parsed.found_block
                     else "unparsed" if not parsed.found_block else "over-cap" if doc["over_cap"] else "ok")
    return doc


def _base_run(deps: Deps, case: dict, harness: str, model: str, run_id: str) -> dict:
    return {"run_id": run_id, "case_id": case["id"], "kind": case["kind"], "harness": harness, "model": model,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "quota_guard": deps.quota_state}


def _finish(deps: Deps, run: dict, out_dir: Path, started: float) -> dict:
    run["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    run["wall_s"] = round(time.time() - started, 1)
    _write(out_dir / "run.json", run)
    if deps.log:
        runlog.append(run)
    return run


def _skipped(deps: Deps, case: dict, harness: str, model: str, run_id: str, out_dir: Path, reason: str, started: float) -> dict:
    run = {**_base_run(deps, case, harness, model, run_id), "result": "skipped", "reason": reason}
    return _finish(deps, run, out_dir, started)


# --- review -----------------------------------------------------------------------


def review_once(deps: Deps, case: dict, *, harness: str, model: str, n: int = 1) -> dict:
    started = time.time()
    run_id = new_run_id(case["id"], harness, model, n)
    out_dir = config.runs_root() / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    snap = None
    hygiene: dict = {}
    try:
        _guard(deps, harness, notes)
        snap = snapshot.build(case, run_id, repo=deps.repo)
        shas = snapshot.shas(snap)
        try:
            doc = review_step(deps, case, snap, harness=harness, model=model, run_id=run_id, out_dir=out_dir, notes=notes)
        finally:
            # What the reviewer left (commits, refs, a moved HEAD) is recorded,
            # then undone from the build-time shas, so the judge never reads a mutant.
            hygiene["after_run"] = snapshot.check(snap, case, shas)
            try:
                snapshot.reset(snap, shas["change"])
                hygiene["after_reset"] = snapshot.check(snap, case, shas)
            except snapshot.SnapshotError as exc:
                hygiene["after_reset"] = [f"reset failed: {exc}"]
    except Skipped as exc:
        if snap is not None:
            shutil.rmtree(snap, ignore_errors=True)
        return _skipped(deps, case, harness, model, run_id, out_dir, str(exc), started)
    except snapshot.SnapshotError as exc:
        run = {**_base_run(deps, case, harness, model, run_id), "result": "error", "reason": f"snapshot: {exc}"}
        return _finish(deps, run, out_dir, started)
    run = {**_base_run(deps, case, harness, model, run_id), "snapshot": str(snap), "shas": shas,
           "task_class": "review", "reviewer": doc, "result": doc["result"], "n_findings": len(doc["findings"]),
           "flags": doc["flags"], "network_flags": doc["network_flags"], "escape_flags": doc["escape_flags"],
           "hygiene": hygiene, "quota_notes": notes, "reason": doc.get("error", "")}
    return _finish(deps, run, out_dir, started)


# --- grading an implementation ----------------------------------------------------


def default_suite_runner(grade_dir: Path, rel: str) -> tuple[bool, str]:
    """One suite in a no-network grade cell: the agent's code never runs outside one."""
    cell = sandbox.Cell("grade", grade_dir, env={"PYTHONPATH": str(grade_dir)})
    if rel.endswith((".ts", ".tsx")):
        if not config.node_modules_dir().is_dir():
            return False, "the bench's hud/node_modules copy is missing: TypeScript tests cannot run"
        argv = ["npx", "--no-install", "vitest", "run", str(Path(rel).relative_to("hud")) if rel.startswith("hud/") else rel]
        cell.chdir = grade_dir / "hud"
    else:
        argv = [sandbox.PYTHON, rel]
    try:
        rc, out, err, timed_out = sandbox.launch(argv, cell=cell, run_id=f"grade-{uuid.uuid4().hex[:12]}",
                                                 timeout_s=config.GRADE_TIMEOUT_S)
    except sandbox.SandboxError as exc:
        return False, f"sandbox: {exc}"
    return (rc == 0 and not timed_out), ("timed out\n" if timed_out else "") + (out + err)[-1500:]


def grade(deps: Deps, snap: Path, case: dict) -> dict:
    """Hidden tests plus regression suites (both from the source repo, never
    the implementer's copies) on a copy; a failure counts only if it reproduces."""
    runner = deps.suite_runner or default_suite_runner
    grade_dir = snapshot.restore_hidden(snap, case, repo=deps.repo)
    try:
        out = {"hidden": {}, "regression": {}, "tails": {}}
        for bucket, rels in (("hidden", case["hidden_tests"]), ("regression", case["regression_suites"])):
            for rel in rels:
                ok, tail = runner(grade_dir, rel)
                if not ok:
                    ok, tail = runner(grade_dir, rel)      # one retry
                out[bucket][rel] = ok
                if not ok:
                    out["tails"][rel] = tail[-800:]
        out["hidden_pass"] = all(out["hidden"].values())
        out["regression_pass"] = all(out["regression"].values())
        return out
    finally:
        shutil.rmtree(grade_dir, ignore_errors=True)


TEST_PATH = re.compile(r"(^|/)(tests?|__tests__)/|_check\.py$|(^|/)test_[^/]*\.py$|\.test\.[jt]sx?$|\.spec\.[jt]sx?$")


def is_test_path(rel: str) -> bool:
    return bool(TEST_PATH.search(rel))


def _diff_stats(snap: Path, base: str, scope: list[str]) -> dict:
    """The implementer's change against the build-time base (never HEAD)."""
    snapshot.git(snap, "add", "-A")
    try:
        names = snapshot.git(snap, "diff", "--cached", "--name-only", "--no-renames", base).stdout.decode().split("\n")
        stat = snapshot.git(snap, "diff", "--cached", "--numstat", "--no-renames", base).stdout.decode().split("\n")
    finally:
        snapshot.git(snap, "reset", "-q", check=False)
    names = [n for n in names if n]
    lines = 0
    for row in stat:
        parts = row.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            lines += int(parts[0]) + int(parts[1])
    return {"files": names, "lines": lines,
            "test_files": [f for f in names if f not in scope and is_test_path(f)],
            "out_of_scope": [f for f in names if f not in scope and not is_test_path(f)]}


def implement_once(deps: Deps, case: dict, *, harness: str, model: str, reviewer_harness: str,
                   reviewer_model: str, n: int = 1, max_rounds: int = 2) -> dict:
    started = time.time()
    run_id = new_run_id(case["id"], harness, model, n)
    out_dir = config.runs_root() / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    try:
        _guard(deps, harness, notes)
        if reviewer_harness != harness:
            _guard(deps, reviewer_harness, notes)
        snap = snapshot.build(case, run_id, repo=deps.repo)
        shas = snapshot.shas(snap)
    except Skipped as exc:
        return _skipped(deps, case, harness, model, run_id, out_dir, str(exc), started)
    except snapshot.SnapshotError as exc:
        run = {**_base_run(deps, case, harness, model, run_id), "result": "error", "reason": f"snapshot: {exc}"}
        return _finish(deps, run, out_dir, started)
    base = shas["base"]

    rounds, prompt, findings_text = [], brief.implement_prompt(case, snap, harness=harness), ""
    merged_ready, reason = False, ""
    rcase = brief.reviewer_case(case)
    cumulative_cost, cumulative_known = 0.0, False
    cost_at_ready = None
    net_flags, esc_flags = [], []
    for round_no in range(max_rounds + 1):
        rdir = out_dir / f"round-{round_no}"
        rdir.mkdir(exist_ok=True)
        if round_no:
            prompt = brief.fix_prompt(case, snap, round_no, findings_text, harness=harness)
        (rdir / "implementer-prompt.txt").write_text(prompt, encoding="utf-8")
        try:
            result = model_step(deps, harness=harness, model=model, prompt=prompt, cwd=snap,
                                run_id=f"{run_id}-i{round_no}", out_dir=rdir, timeout_s=deps.implement_timeout,
                                max_turns=config.IMPLEMENT_MAX_TURNS, notes=notes)
        except Skipped as exc:
            reason = str(exc)
            break
        (rdir / "implementer-reply.txt").write_text(result.text, encoding="utf-8")
        impl_sum = result.summary()
        impl_sum["network_flags"] = audit.network_flags(result)
        impl_sum["escape_flags"] = audit.escape_flags(result, snap)
        impl_sum["settled_usd"] = result.raw.get("settled_usd")
        net_flags += [f"implementer r{round_no}: {f}" for f in impl_sum["network_flags"]]
        esc_flags += [f"implementer r{round_no}: {f}" for f in impl_sum["escape_flags"]]
        try:
            folded = snapshot.fold_commits(snap, base)        # a commit never hides the change
            hygiene = snapshot.check(snap, case, {"change": base})
        except snapshot.SnapshotError as exc:
            reason = f"snapshot unusable after round {round_no}: {exc}"
            rounds.append({"round": round_no, "implementer": impl_sum, "hygiene": [str(exc)], "merge_ready": False})
            break
        stats = _diff_stats(snap, base, case["scope"])
        graded = grade(deps, snap, case)
        review_doc = None
        rev_copy = snapshot.review_copy(snap, base, snap.parent / snapshot.new_name())
        try:
            review_doc = review_step(deps, rcase, rev_copy, harness=reviewer_harness, model=reviewer_model,
                                     run_id=f"{run_id}-r{round_no}", out_dir=rdir / "review", notes=notes)
        except Skipped as exc:
            reason = str(exc)
        finally:
            shutil.rmtree(rev_copy, ignore_errors=True)
        if review_doc:
            net_flags += [f"reviewer r{round_no}: {f}" for f in review_doc["network_flags"]]
            esc_flags += [f"reviewer r{round_no}: {f}" for f in review_doc["escape_flags"]]
        found = (review_doc or {}).get("findings", [])
        medplus = [f for f in found if f["severity"] in ("HIGH", "MED")]
        for part in (impl_sum, review_doc):
            if part and part.get("cost_usd") is not None:
                cumulative_cost += part["cost_usd"]
                cumulative_known = True
        ready = bool(review_doc and review_doc["ok"] and graded["hidden_pass"] and graded["regression_pass"]
                     and not medplus and not stats["out_of_scope"])
        rounds.append({"round": round_no, "implementer": impl_sum, "commits_folded": folded, "hygiene": hygiene,
                       "diff": stats, "grade": graded, "reviewer": review_doc, "medplus": len(medplus),
                       "merge_ready": ready,
                       "cumulative_cost_usd": round(cumulative_cost, 6) if cumulative_known else None})
        if ready:
            merged_ready, cost_at_ready = True, (round(cumulative_cost, 6) if cumulative_known else None)
            break
        if review_doc is None or round_no == max_rounds:
            break
        findings_text = findings.render(found) or (
            "(no findings were parsed; the hidden acceptance tests do not all pass)" if not graded["hidden_pass"]
            else "(no MED+ findings; files outside the expected scope: " + ", ".join(stats["out_of_scope"]) + ")")
    run = {**_base_run(deps, case, harness, model, run_id), "snapshot": str(snap), "shas": shas,
           "reviewer_harness": reviewer_harness, "reviewer_model": reviewer_model,
           "task_class": f"implement-{case['size']}", "rounds": rounds, "merged_ready": merged_ready,
           "rounds_to_merge_ready": (len(rounds) - 1) if merged_ready else None,
           "cost_to_merge_ready": cost_at_ready, "total_cost_usd": round(cumulative_cost, 6) if cumulative_known else None,
           "result": "ok" if rounds and not reason else "skipped" if not rounds else "partial", "reason": reason,
           "implementer": rounds[-1]["implementer"] if rounds else None,
           "reviewer": rounds[-1].get("reviewer") if rounds else None, "quota_notes": notes,
           "network_flags": net_flags, "escape_flags": esc_flags}
    return _finish(deps, run, out_dir, started)


# --- cells ------------------------------------------------------------------------


def run_cells(cells: list[Callable[[], dict]], workers: int = config.MAX_CONCURRENT) -> list[dict]:
    """At most MAX_CONCURRENT at once, whatever was asked."""
    workers = max(1, min(workers, config.MAX_CONCURRENT))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda cell: cell(), cells))


def shuffled(items: list, seed: int | None) -> list:
    items = list(items)
    random.Random(seed).shuffle(items)
    return items
