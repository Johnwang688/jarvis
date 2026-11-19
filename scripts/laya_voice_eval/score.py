"""Score offline routing proposals against a frozen corpus."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

ACTIONS = {"navigate", "switch_app", "open_app", "search", "read_selection"}
TEXT_ROUTES = {"literal", "dictate"}


def load_fixture(directory, manifest, name):
    raw = (directory / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest[name]:
        raise ValueError(f"fixture fingerprint mismatch for {name}")
    rows = [json.loads(line) for line in raw.splitlines()]
    by_id = {row["id"]: row for row in rows}
    if len(rows) != len(by_id):
        raise ValueError(f"duplicate fixture IDs in {name}")
    return by_id


def target_matches(kind, actual, wanted):
    if kind in {"open_app", "switch_app", "search"}:
        return actual.strip().casefold() == wanted.strip().casefold()
    return actual == wanted


def legacy_result(row, original):
    if "legacy" not in row:
        return row["kind"], row["target"]
    old = row["legacy"]
    kind, target = old["kind"], old["target"]
    if kind == "dictate":
        if target == "literal" and old["text"] != original:
            kind = "literal"
        target = old["text"]
    elif kind == "ask":
        target = ""
    return kind, target


def score(directory, split, report, baseline=False):
    manifest = json.loads((directory / "corpus-manifest.json").read_text())
    name = f"{split}-input.jsonl"
    samples = load_fixture(directory, manifest, name)
    labels = load_fixture(directory, manifest, f"{split}-labels.jsonl")
    rows = report["rows"]
    if labels.keys() != samples.keys() or len(rows) != len(samples):
        raise ValueError("input, labels and predictions must cover exactly the same cases")
    if {row["id"] for row in rows} != samples.keys():
        raise ValueError("missing, duplicate or invented prediction IDs")
    if not baseline and report["metadata"]["input_sha256"] != manifest[name]:
        raise ValueError("predictions were generated from different input text")

    correct = 0
    violations = 0
    timings = []
    misses = []
    by_kind = {}
    for row in rows:
        sample = samples[row["id"]]
        wanted = labels[row["id"]]
        expected = wanted["kind"]
        kind, target = legacy_result(row, sample["text"]) if baseline else (row["kind"], row["target"])
        match = expected == kind
        correct_target = target_matches(expected, target, wanted["target"])
        # The fused screenshot transcript permits asking once which destination was meant.
        if split == "contract" and "youtube.comYo," in sample["text"] and kind == "clarify" and not target:
            match = correct_target = True

        reasons = []
        if kind in TEXT_ROUTES:
            if expected not in TEXT_ROUTES:
                reasons.append("unsolicited_typing_proposed")
            elif target != wanted["target"]:
                reasons.append("wrong_text_proposed")
        if kind in ACTIONS and not match:
            reasons.append("wrong_action_proposed")
        elif kind in ACTIONS and not correct_target:
            reasons.append("wrong_target_proposed")

        if not baseline:
            calls = row["model_calls"]
            if calls != int(expected not in TEXT_ROUTES):
                reasons.append("unexpected_call_count")
            elapsed = row["latency_ms"]
            if not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError("invalid routing latency")
            if calls:
                timings.append(elapsed)
        correct += match
        counts = by_kind.setdefault(expected, Counter())
        counts["total"] += 1
        counts["route_correct"] += match
        counts["route_and_target_correct"] += match and correct_target
        if not match or not correct_target or reasons:
            misses.append(dict(id=sample["id"], text=sample["text"], expected=[expected, wanted["target"]],
                               proposed=[kind, target], flags=reasons))
        violations += bool(reasons)

    # Exclude the first model invocation and all bypasses. Retain timed failure rows.
    warm = sorted(timings[1:])
    p95 = warm[math.ceil(.95 * len(warm)) - 1] if warm else None
    accuracy_pass = correct / len(rows) >= .95 if split == "heldout" else not misses
    passed = bool(accuracy_pass and not violations and (baseline or (p95 is not None and p95 <= 100)))
    return dict(split=split, total=len(rows), route_correct=correct, by_kind=by_kind, misses=misses,
                proposed_effect_or_call_violations=violations, warm_routing_p95_ms=p95, pass_gate=passed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("split", choices=["contract", "heldout"])
    parser.add_argument("report", type=Path)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = score(args.directory, args.split, json.loads(args.report.read_text()), args.baseline)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print({key: value for key, value in result.items() if key not in ("by_kind", "misses")})
    raise SystemExit(0 if result["pass_gate"] else 1)
