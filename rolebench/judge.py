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
