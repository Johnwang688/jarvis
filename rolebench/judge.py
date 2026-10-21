"""The blinded Opus judge: prompt, strict validation, scoring and the spot-check.

Blinding: the judge sees the answer key and the findings, shuffled and named
F1..Fn, and reads a copy of the change (the build-time change commit, exported
with no `.git`) in a directory with a random name, inside a read-only judge
cell. Nothing it can see names the model, the harness or the run. The
spot-check sample and its key live under neutral paths, never in a run
directory (whose name carries the model).
"""

from __future__ import annotations

import fcntl
import json
import random
import re
import shutil
import time
from pathlib import Path

from . import config, quota, runners, snapshot

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


PROMPT = """\
You are judging a code review against a known answer key. Several reviewers read the \
same change; you see one review with no indication of who wrote it.

You may read the repository in the current directory (read-only) to check a claim; it \
is the tree after the change. `{diff}` at its root is the change itself (a git diff). \
Do not run anything.

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


def blind(findings: list[dict], rng: random.Random) -> tuple[str, dict]:
    """(text, {"F1": index into findings, ...}) with the order shuffled."""
    order = list(range(len(findings)))
    rng.shuffle(order)
    ids = {f"F{k}": i for k, i in enumerate(order, 1)}
    lines = []
    for fid, i in ids.items():
        f = findings[i]
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
                                read_only=True, run_id=f"judge-{snapshot.new_name()}")
    if result.error and not result.text:
        raise JudgeError(f"judge run failed: {result.error}")
    return result.text, result.cost_usd


def _existing(run_dir: Path) -> dict | None:
    try:
        doc = json.loads((run_dir / "judge.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if doc.get("valid") else None


def _record_valid_new(run: dict, rows: list[dict]) -> None:
    """valid_new.jsonl holds each run's rows once, however often it is judged."""
    path = config.home() / "valid_new.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        kept = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("run") != run["run_id"]:
                        kept.append(line)
                except json.JSONDecodeError:
                    kept.append(line)
        kept += [json.dumps(r) for r in rows]
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
        tmp.replace(path)


def judge_run(run_dir: Path, case: dict, judge_fn=default_judge, quota_override: str = "",
              quota_reader=quota.daemon_reader, force: bool = False, rng: random.Random | None = None) -> dict:
    """Judge one review run in place: writes judge.json and folds the score into
    run.json. A run already judged validly is returned as it is unless `force`."""
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if run.get("kind") != "review":
        raise JudgeError(f"{run_dir.name} is not a review run")
    if not force:
        done = _existing(run_dir)
        if done is not None:
            return {**done, "reused": True}
    items = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    gt = case["ground_truth"]
    text, ids = blind(items, rng or random.Random())
    bad = (run.get("hygiene") or {}).get("after_reset")
    if bad:
        raise JudgeError(f"the snapshot was not clean after the reviewer: {bad}")
    if not quota_override:
        skip, note = quota.guard("claude", quota_reader)
        if skip:
            raise JudgeError(f"judge skipped on quota: {note}")
    if not items:
        matches, attempts, cost = [], 0, 0.0
    else:
        snap, shas = Path(run.get("snapshot") or ""), run.get("shas") or {}
        if not snap.is_dir() or not shas.get("change"):
            raise JudgeError("the run's snapshot is gone (cleaned?): it cannot be judged")
        copy = config.home() / "judge" / snapshot.new_name()
        copy.parent.mkdir(parents=True, exist_ok=True)
        try:
            snapshot.export(snap, shas["change"], copy, base=shas.get("base"))
            prompt = PROMPT.format(ground_truth=gt_text(gt), findings=text, diff=snapshot.CHANGE_DIFF)
            error, cost, attempts, matches = "", 0.0, 0, None
            for attempts in (1, 2):
                reply, c = judge_fn(prompt if not error else prompt + f"\n\nYour previous reply was rejected: {error}\nReply again with valid JSON only.", copy)
                cost += c or 0.0
                try:
                    matches = validate(extract_json(reply), list(ids), [g["id"] for g in gt])
                    break
                except JudgeError as exc:
                    error = str(exc)
        finally:
            shutil.rmtree(copy, ignore_errors=True)
        if matches is None:
            (run_dir / "judge.json").write_text(json.dumps({"valid": False, "error": error, "attempts": attempts}), encoding="utf-8")
            raise JudgeError(f"judge output invalid twice: {error}")
    doc = {"valid": True, "matches": matches, "score": score(matches, gt), "attempts": attempts, "cost_usd": cost,
           "finding_ids": ids, "quota_guard": f"off: {quota_override}" if quota_override else "on",
           "judged_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (run_dir / "judge.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    run["score"], run["judged"] = doc["score"], True
    (run_dir / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    _record_valid_new(run, [{"case": run["case_id"], "run": run["run_id"], "severity": m["severity"],
                             "finding": items[ids[m["finding"]]], "note": m.get("note", "")}
                            for m in matches if m["verdict"] == "valid-new"])
    return doc


def spotcheck_paths(stamp: str | None = None) -> tuple[Path, Path]:
    """(sample, key): neutral names, in different directories."""
    stamp = stamp or f"{time.strftime('%Y%m%d-%H%M%S')}-{snapshot.new_name()[:6]}"
    return (config.home() / "spotcheck" / stamp / "spotcheck.md",
            config.home() / "spotcheck-keys" / f"{stamp}.json")


def spotcheck(run_dirs: list[Path], out: Path | None = None, key_out: Path | None = None,
              fraction: float = 0.2, seed: int = 1) -> tuple[int, Path]:
    """A 20% sample of the judge's verdicts for the lead to check: (size, sample path)."""
    if out is None or key_out is None:
        default_out, default_key = spotcheck_paths()
        out, key_out = out or default_out, key_out or default_key
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
        ids = doc.get("finding_ids") or {}
        for m in doc["matches"]:
            index = ids.get(m["finding"], int(m["finding"][1:]) - 1)
            pool.append((d.name, run["case_id"], items[index], m))
    out.parent.mkdir(parents=True, exist_ok=True)
    key_out.parent.mkdir(parents=True, exist_ok=True)
    if not pool:
        out.write_text("# Spot-check\n\nNothing was judged.\n", encoding="utf-8")
        return 0, out
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
    key_out.write_text(json.dumps(key, indent=1), encoding="utf-8")   # who wrote each: kept apart so the sample reads blind
    return k, out
