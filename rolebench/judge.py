"""Judge output: strict validation and scoring. (The `claude -p --model opus`
call, blinding and spotcheck.md are still to do.)"""

from __future__ import annotations

from . import config

VERDICTS = ("match", "valid-new", "invalid", "nit")


class JudgeError(ValueError):
    pass


def validate(doc, finding_ids: list[str], gt_ids: list[str]) -> list[dict]:
    """Every finding exactly once; match needs a known gt id; valid-new needs a severity."""
    if not isinstance(doc, dict) or set(doc) != {"matches"} or not isinstance(doc["matches"], list):
        raise JudgeError("expected {\"matches\": [...]}")
    seen = []
    for m in doc["matches"]:
        if not isinstance(m, dict) or not {"finding", "verdict"} <= set(m) or set(m) - {"finding", "verdict", "gt", "severity", "note"}:
            raise JudgeError(f"bad entry {m!r}")
        if m["verdict"] not in VERDICTS:
            raise JudgeError(f"bad verdict {m['verdict']!r}")
        if m["finding"] not in finding_ids or m["finding"] in seen:
            raise JudgeError(f"unknown or repeated finding {m['finding']!r}")
        seen.append(m["finding"])
        if m["verdict"] == "match":
            if m.get("gt") not in gt_ids:
                raise JudgeError(f"match names unknown ground truth {m.get('gt')!r}")
        elif m.get("gt") is not None:
            raise JudgeError("only a match may name ground truth")
        if m["verdict"] == "valid-new" and m.get("severity") not in ("HIGH", "MED", "LOW"):
            raise JudgeError("valid-new needs a severity")
    if sorted(seen) != sorted(finding_ids):
        raise JudgeError("every finding needs exactly one verdict")
    return doc["matches"]


def score(matches: list[dict], ground_truth: list[dict]) -> dict:
    scored = [g for g in ground_truth if g["severity"] != "NIT"]
    caught = {m["gt"] for m in matches if m["verdict"] == "match"}
    total_w = sum(config.WEIGHTS[g["severity"]] for g in scored)
    got_w = sum(config.WEIGHTS[g["severity"]] for g in scored if g["id"] in caught)
    medplus = [g for g in scored if g["severity"] in ("HIGH", "MED")]
    return {
        "weighted_recall": got_w / total_w if total_w else None,
        "medplus_recall": (sum(g["id"] in caught for g in medplus) / len(medplus)) if medplus else None,
        "false_positives": sum(m["verdict"] == "invalid" for m in matches),
        "valid_new": sum(m["verdict"] == "valid-new" for m in matches),
        "nits": sum(m["verdict"] == "nit" for m in matches),
    }


# --- the Opus judge ---------------------------------------------------------------

import json
import random
import re
import time
from pathlib import Path

from . import findings as findings_mod
from . import quota, runlog, runners

PROMPT = """\
You are judging a code review against a known answer key. Several reviewers read the \
same change; you see one review with no indication of who wrote it.

You may read the repository in the current directory (read-only) to check a claim; it \
is the change as it was reviewed. Do not run anything.

## Answer key (ground truth)
{ground_truth}

## Findings reported by the review
{findings}

## Your job
For each reported finding decide exactly one verdict:
- "match": it describes the same defect as one ground-truth entry (same mechanism and \
place, wording may differ). Name that entry in "gt".
- "valid-new": a real defect that is not in the key. Verify it in the code. Give its \
"severity" (HIGH, MED or LOW).
- "invalid": wrong, unsupported, or not a defect in this change (a false alarm).
- "nit": true but trivial (taste, naming, formatting).

Reply with one JSON object and nothing else:
{{"matches": [{{"finding": "F1", "verdict": "match", "gt": "gt1", "note": "short reason"}}, ...]}}
Every finding must appear exactly once. "gt" only on a match; "severity" only (and \
always) on a valid-new.
"""


def blind_text(findings: list[dict]) -> tuple[str, list[str]]:
    ids = [f"F{i}" for i in range(1, len(findings) + 1)]
    lines = []
    for fid, f in zip(ids, findings):
        where = f["file"] + (f":{f['line']}" if f["line"] is not None else "")
        lines.append(f"{fid} [{f['severity']}] {where} - {f['title']}\n    scenario: {f['scenario']}")
    return "\n".join(lines), ids


def gt_text(ground_truth: list[dict]) -> str:
    return "\n".join(f"{g['id']} [{g['severity']}] {g['location']}: {g['mechanism']} (evidence: {g['evidence']})"
                     for g in ground_truth) or "(none: this change was reviewed and nothing real was found)"


def extract_json(text: str):
    text = text.strip()
    for candidate in [text] + [m for m in re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)]:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise JudgeError("the judge's reply is not JSON")


def default_judge(prompt: str, cwd: Path) -> tuple[str, float | None]:
    result = runners.run_claude(model="opus", prompt=prompt, cwd=cwd, timeout_s=900, max_turns=30,
                                read_only=True)
    if result.error and not result.text:
        raise JudgeError(f"judge run failed: {result.error}")
    return result.text, result.cost_usd


def judge_run(run_dir: Path, case: dict, judge_fn=default_judge, use_quota: bool = True,
              quota_reader=quota.daemon_reader) -> dict:
    """Judge one review run in place: writes judge.json and folds the score into run.json."""
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if run.get("kind") != "review":
        raise JudgeError(f"{run_dir.name} is not a review run")
    items = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    text, ids = blind_text(items)
    gt = case["ground_truth"]
    snap = Path(run.get("snapshot") or run_dir)
    cwd = snap if snap.is_dir() else run_dir
    if use_quota:
        skip, note = quota.guard("claude", quota_reader)
        if skip:
            raise JudgeError(f"judge skipped on quota: {note}")
    if not items:
        matches, attempts, cost = [], 0, 0.0
    else:
        prompt = PROMPT.format(ground_truth=gt_text(gt), findings=text)
        error, cost, attempts, matches = "", 0.0, 0, None
        for attempts in (1, 2):
            reply, c = judge_fn(prompt if not error else prompt + f"\n\nYour previous reply was rejected: {error}\nReply again with valid JSON only.", cwd)
            cost += c or 0.0
            try:
                matches = validate(extract_json(reply), ids, [g["id"] for g in gt])
                break
            except JudgeError as exc:
                error = str(exc)
        if matches is None:
            (run_dir / "judge.json").write_text(json.dumps({"valid": False, "error": error, "attempts": attempts}), encoding="utf-8")
            raise JudgeError(f"judge output invalid twice: {error}")
    doc = {"valid": True, "matches": matches, "score": score(matches, gt), "attempts": attempts, "cost_usd": cost,
           "finding_ids": dict(zip(ids, range(len(ids))))}
    (run_dir / "judge.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    run["score"], run["judged"] = doc["score"], True
    (run_dir / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    new = [(items[int(m["finding"][1:]) - 1], m) for m in matches if m["verdict"] == "valid-new"]
    if new:
        from . import config
        with open(config.home() / "valid_new.jsonl", "a", encoding="utf-8") as fh:
            for f, m in new:
                fh.write(json.dumps({"case": run["case_id"], "run": run["run_id"], "severity": m["severity"],
                                     "finding": f, "note": m.get("note", "")}) + "\n")
    return doc


def spotcheck(run_dirs: list[Path], out: Path, fraction: float = 0.2, seed: int = 1) -> int:
    """A 20% sample of the judge's matches for the lead to check; returns the sample size."""
    pool = []
    for d in run_dirs:
        jd = d / "judge.json"
        if not jd.is_file():
            continue
        doc = json.loads(jd.read_text(encoding="utf-8"))
        if not doc.get("valid"):
            continue
        items = json.loads((d / "findings.json").read_text(encoding="utf-8"))
        run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        for m in doc["matches"]:
            pool.append((d.name, run["case_id"], items[int(m["finding"][1:]) - 1], m))
    if not pool:
        out.write_text("# Spot-check\n\nNothing was judged.\n", encoding="utf-8")
        return 0
    k = max(1, round(len(pool) * fraction))
    sample = random.Random(seed).sample(pool, k)
    key = {}
    lines = [f"# Spot-check: {k} of {len(pool)} judged findings", "",
             "For each, mark whether the verdict is right. A match is right when the finding and the "
             "ground-truth entry are the same defect.", ""]
    for index, (run_name, case_id, f, m) in enumerate(sample, 1):
        key[f"S{index}"] = {"run": run_name, "finding": m["finding"]}
        where = f["file"] + (f":{f['line']}" if f["line"] is not None else "")
        lines += [f"## S{index} ({case_id})", f"- finding: [{f['severity']}] {where} - {f['title']}",
                  f"- scenario: {f['scenario']}", f"- verdict: **{m['verdict']}**"
                  + (f" -> {m['gt']}" if m.get("gt") else "") + (f" (severity {m['severity']})" if m.get("severity") else ""),
                  f"- judge note: {m.get('note', '')}", "- lead: [ ] agree  [ ] disagree", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    out.with_name(out.stem + "-key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")   # who wrote each: kept apart so the sample reads blind
    return k
