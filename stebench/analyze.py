"""Analysis for the pilot. No scipy - exact Wilcoxon by enumeration.

With n=12 paired observations the normal approximation is marginal, so the
signed-rank null is enumerated exactly over 2^n sign assignments (4096).
"""

import itertools
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data"


def logit(p: float) -> float:
    """Log-odds.

    Required here, not decorative. Sapling pins every AI text between 0.9998
    and 0.99996, so on the probability scale the conditions differ in the 4th
    decimal and a paired test on raw probability measures rounding. The same
    texts span 8.8-10.4 in log-odds, which is a scale a signed-rank test can
    actually work on. Report BOTH: the logit delta is the sensitive measure,
    the probability is what a user of the detector would see.
    """
    p = min(max(p, 1e-12), 1 - 1e-12)
    return math.log(p / (1 - p))


def wilcoxon_signed_rank(diffs: list[float]) -> dict:
    """Exact two-sided Wilcoxon signed-rank test. Zeros dropped (Pratt off)."""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return {"n": 0, "W": None, "p": 1.0, "rank_biserial": 0.0}

    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:  # average ranks within ties on |d|
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1

    w_pos = sum(r for r, x in zip(ranks, d) if x > 0)
    total = sum(ranks)
    w_obs = min(w_pos, total - w_pos)

    if n <= 20:
        count = 0
        for signs in itertools.product([0, 1], repeat=n):
            s = sum(r for r, sg in zip(ranks, signs) if sg)
            if min(s, total - s) <= w_obs:
                count += 1
        p = count / (2 ** n)
    else:
        p = float("nan")

    return {
        "n": n,
        "W": round(w_obs, 1),
        "p": round(min(p, 1.0), 5),
        # rank-biserial: +1 means every pair moved up, -1 every pair down
        "rank_biserial": round(2 * w_pos / total - 1, 3) if total else 0.0,
    }


def describe(vals: list[float]) -> dict:
    v = [x for x in vals if x is not None]
    if not v:
        return {"n": 0}
    return {
        "n": len(v),
        "mean": round(statistics.fmean(v), 4),
        "median": round(statistics.median(v), 4),
        "sd": round(statistics.pstdev(v), 4) if len(v) > 1 else 0.0,
        "min": round(min(v), 4),
        "max": round(max(v), 4),
    }


def report(path: Path = DATA / "pilot_results.json") -> None:
    if not path.exists():
        raise SystemExit(f"no results at {path} - run `python run_pilot.py detect` first")
    rows = json.loads(path.read_text())
    if not rows:
        raise SystemExit("results file is empty - the detect stage scored nothing")
    ai = [r for r in rows if r["kind"] == "ai"]
    human = [r for r in rows if r["kind"] == "human"]

    by_cond = defaultdict(list)
    for r in ai:
        by_cond[r["condition"]].append(r)

    backend = next((r.get("backend") for r in rows if r.get("backend")), "detector")
    print("=" * 78)
    print(f"{backend}: ai_prob by condition".center(78))
    print("=" * 78)
    hdr = (f"{'condition':16s} {'n':>3s} {'mean_p':>9s} {'med_p':>9s} "
           f"{'mean_logit':>11s} {'flag@.5':>8s} {'burst':>7s} {'ste':>6s}")
    print(hdr)

    def line(label: str, rs: list) -> None:
        ps = [r["ai_prob"] for r in rs if r["ai_prob"] is not None]
        if not ps:
            print(f"{label:16s} {0:3d}")
            return
        lg = describe([logit(p) for p in ps])
        s = describe(ps)
        burst = describe([r["sent_len_sd"] for r in rs])
        ste = describe([r["ste_score"] for r in rs])
        flagged = sum(p >= 0.5 for p in ps)
        print(f"{label:16s} {s['n']:3d} {s['mean']:9.5f} {s['median']:9.5f} "
              f"{lg['mean']:11.2f} {flagged:4d}/{len(ps):<3d} "
              f"{burst.get('mean',0):7.2f} {ste.get('mean',0):6.3f}")

    for cond in sorted(by_cond):
        line(cond, by_cond[cond])
    if human:
        line("HUMAN(wiki<2020)", human)

    # paired tests against baseline, matched by task id
    print()
    print("=" * 74)
    print("Paired vs C0_baseline (same task, same model)".center(74))
    print("=" * 74)
    base = {r["task_id"]: r for r in by_cond.get("C0_baseline", [])}
    for cond in sorted(c for c in by_cond if c != "C0_baseline"):
        pairs = [(base[r["task_id"]], r) for r in by_cond[cond]
                 if r["task_id"] in base
                 and r["ai_prob"] is not None and base[r["task_id"]]["ai_prob"] is not None]
        if not pairs:
            print(f"{cond}: no comparable pairs")
            continue
        dp = [b["ai_prob"] - a["ai_prob"] for a, b in pairs]
        dl = [logit(b["ai_prob"]) - logit(a["ai_prob"]) for a, b in pairs]
        w = wilcoxon_signed_rank(dl)
        arrow = "MORE detectable" if statistics.fmean(dl) > 0 else "less detectable"
        print(f"\n{cond} vs baseline   ({arrow})")
        print(f"  delta prob    mean {statistics.fmean(dp):+.6f}   "
              f"median {statistics.median(dp):+.6f}")
        print(f"  delta logit   mean {statistics.fmean(dl):+.3f}   "
              f"median {statistics.median(dl):+.3f}   <- test scale")
        print(f"  n pairs {w['n']}   W={w['W']}   p={w['p']}   "
              f"rank-biserial={w['rank_biserial']:+.3f}")
        print(f"  moved down    {sum(1 for d in dl if d < 0)}/{len(dl)}")
        crossed = sum(1 for a, b in pairs if a["ai_prob"] >= 0.5 > b["ai_prob"])
        print(f"  crossed the 0.5 verdict boundary: {crossed}/{len(pairs)}"
              + ("   <- actual evasion" if crossed else "   (no verdict changed)"))

    # separation from human is what actually matters for a detector
    if human:
        print()
        print("=" * 74)
        print("Separation from the human control (AUROC, higher = easier to catch)".center(74))
        print("=" * 74)
        h = [r["ai_prob"] for r in human if r["ai_prob"] is not None]
        for cond in sorted(by_cond):
            a = [r["ai_prob"] for r in by_cond[cond] if r["ai_prob"] is not None]
            print(f"  {cond:16s} AUROC={auroc(a, h):.3f}   (n_ai={len(a)}, n_human={len(h)})")

    print()
    print("by domain (mean ai_prob)")
    doms = sorted({r["domain"] for r in ai})
    print(f"  {'condition':16s} " + " ".join(f"{d:>16s}" for d in doms))
    for cond in sorted(by_cond):
        cells = []
        for d in doms:
            v = [r["ai_prob"] for r in by_cond[cond]
                 if r["domain"] == d and r["ai_prob"] is not None]
            cells.append(f"{statistics.fmean(v):16.4f}" if v else f"{'-':>16s}")
        print(f"  {cond:16s} " + " ".join(cells))


def auroc(pos: list[float], neg: list[float]) -> float:
    """P(random AI text scores above random human text). Ties count half."""
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


if __name__ == "__main__":
    report()
