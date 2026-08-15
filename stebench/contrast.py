"""What actually separates the human texts from the generated ones.

Two questions, and the second is the useful one:

1. Which features separate human from AI? (a long list, most of it obvious)
2. Which of those does the humanize prompt ALREADY fix, and which does it
   fail to move? The ones it cannot move are the stubborn signal - what a
   detector should lean on, and what a humanizer would actually have to
   attack. A feature the prompt already fixes is not carrying detection.

REGISTER WARNING, and it limits every number below. The human corpus is
pre-2020 Wikipedia: encyclopedic, third-person, citation-shaped. The AI
corpus is instructional and argumentative writing. So a gap here is
authorship OR register, and this script cannot separate them. Features are
tagged with a guess (`reg?`) where register is the likelier explanation.
Treat untagged, large-effect, humanize-resistant features as the real
candidates and confirm them against a register-matched human corpus before
building anything on them.

Effect size is Cliff's delta (nonparametric, fine at n=11 vs n=36):
    |d| < 0.15 negligible, < 0.33 small, < 0.47 medium, else large.
"""

import json
import re
import statistics
from collections import Counter
from pathlib import Path

from stylometry import split_sentences, words, _is_passive

DATA = Path(__file__).parent / "data"

# --- lexical probes -----------------------------------------------------

DISCOURSE = {"however", "moreover", "furthermore", "additionally", "therefore",
             "consequently", "overall", "ultimately", "importantly", "notably",
             "essentially", "specifically", "similarly", "conversely"}
HEDGES = {"may", "might", "could", "generally", "typically", "often", "usually",
          "sometimes", "tends", "tend", "likely", "perhaps", "possibly", "can"}
BOOSTERS = {"very", "extremely", "highly", "significantly", "crucial", "critical",
            "essential", "vital", "key", "major", "massive", "huge", "dramatically"}
AI_TELLS = {"delve", "tapestry", "realm", "landscape", "navigate", "leverage",
            "robust", "seamless", "underscore", "pivotal", "myriad", "testament",
            "showcase", "intricate", "nuanced", "holistic", "multifaceted"}
NOMINAL = re.compile(r"\b\w+(?:tion|ment|ness|ity|ance|ence)\b", re.I)


def _rate(count: int, denom: int) -> float:
    return count / denom if denom else 0.0


def feature_vector(text: str) -> dict[str, float]:
    sents = split_sentences(text)
    w = words(text)
    lower = [x.lower() for x in w]
    nw, ns = len(w), len(sents)
    lens = [len(words(s)) for s in sents]
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    plens = [len(split_sentences(p)) for p in paras]

    first_words = [words(s)[0].lower() for s in sents if words(s)]
    counts = Counter(lower)

    f = {
        # --- shape -----------------------------------------------------
        "sent_len_mean": statistics.fmean(lens) if lens else 0,
        "sent_len_sd": statistics.pstdev(lens) if len(lens) > 1 else 0,
        "sent_len_max": max(lens) if lens else 0,
        "sent_len_min": min(lens) if lens else 0,
        "pct_sent_under_8": _rate(sum(n < 8 for n in lens), ns),
        "pct_sent_over_30": _rate(sum(n > 30 for n in lens), ns),
        "para_len_mean": statistics.fmean(plens) if plens else 0,
        "para_len_sd": statistics.pstdev(plens) if len(plens) > 1 else 0,
        "sents_per_text": ns,

        # --- punctuation ----------------------------------------------
        "comma_per_sent": _rate(text.count(","), ns),
        "semicolon_per_1k": _rate(text.count(";"), nw) * 1000,
        "colon_per_1k": _rate(text.count(":"), nw) * 1000,
        "emdash_per_1k": _rate(text.count("—") + text.count("--"), nw) * 1000,
        "paren_per_1k": _rate(text.count("("), nw) * 1000,
        "quote_per_1k": _rate(text.count('"') + text.count("“"), nw) * 1000,

        # --- lexis ------------------------------------------------------
        "ttr": _rate(len(set(lower)), nw),
        "hapax_rate": _rate(sum(1 for v in counts.values() if v == 1), nw),
        "mean_word_len": statistics.fmean([len(x) for x in w]) if w else 0,
        "pct_long_words": _rate(sum(len(x) >= 8 for x in w), nw),
        "digit_rate": _rate(len(re.findall(r"\d", text)), nw) * 100,
        "nominalization_per_1k": _rate(len(NOMINAL.findall(text)), nw) * 1000,

        # --- register markers -------------------------------------------
        "discourse_per_1k": _rate(sum(counts[x] for x in DISCOURSE), nw) * 1000,
        "hedge_per_1k": _rate(sum(counts[x] for x in HEDGES), nw) * 1000,
        "booster_per_1k": _rate(sum(counts[x] for x in BOOSTERS), nw) * 1000,
        "ai_tell_per_1k": _rate(sum(counts[x] for x in AI_TELLS), nw) * 1000,
        "contraction_rate": _rate(len(re.findall(r"\w'(?:s|t|re|ve|ll|d|m)\b", text)), nw) * 100,
        "passive_rate": _rate(sum(_is_passive(s) for s in sents), ns),
        "second_person_per_1k": _rate(counts["you"] + counts["your"], nw) * 1000,
        "first_person_per_1k": _rate(counts["i"] + counts["we"] + counts["our"], nw) * 1000,

        # --- openers and repetition --------------------------------------
        "opener_conjunction": _rate(sum(x in {"and", "but", "so", "yet", "or"}
                                        for x in first_words), ns),
        "opener_the": _rate(sum(x == "the" for x in first_words), ns),
        "opener_variety": _rate(len(set(first_words)), len(first_words)),
        "rule_of_three": _rate(len(re.findall(r"\w+, \w+,? and \w+", text)), ns),
    }
    return f


# Features where encyclopedic-vs-instructional register is at least as good
# an explanation as human-vs-AI authorship. Not excluded - just flagged.
REGISTER_SUSPECT = {
    "second_person_per_1k", "first_person_per_1k", "digit_rate",
    "paren_per_1k", "quote_per_1k", "passive_rate", "opener_the",
    "sents_per_text", "colon_per_1k", "hedge_per_1k",
}


def cliffs_delta(a: list[float], b: list[float]) -> float:
    """P(a>b) - P(a<b). Positive means group `a` scores higher."""
    if not a or not b:
        return 0.0
    gt = sum(x > y for x in a for y in b)
    lt = sum(x < y for x in a for y in b)
    return (gt - lt) / (len(a) * len(b))


def magnitude(d: float) -> str:
    ad = abs(d)
    return ("negligible" if ad < 0.15 else "small" if ad < 0.33
            else "medium" if ad < 0.47 else "LARGE")


def main() -> None:
    rows = json.loads((DATA / "corpus.json").read_text())
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["condition"], []).append(feature_vector(r["text"]))

    human = groups.get("HUMAN", [])
    c0 = groups.get("C0_baseline", [])
    c1 = groups.get("C1_ste_full", [])
    c3 = groups.get("C3_humanize", [])
    all_ai = c0 + c1 + c3
    if not human or not all_ai:
        raise SystemExit("need both human and AI texts - run generate + human first")

    keys = list(all_ai[0])

    def col(g: list[dict], k: str) -> list[float]:
        return [x[k] for x in g]

    def mean(g: list[dict], k: str) -> float:
        return statistics.fmean(col(g, k)) if g else float("nan")

    rank = sorted(keys, key=lambda k: -abs(cliffs_delta(col(all_ai, k), col(human, k))))

    print("=" * 100)
    print("HUMAN (pre-2020 Wikipedia, n=%d)  vs  ALL AI (Luna, n=%d)"
          % (len(human), len(all_ai)))
    print("=" * 100)
    print(f"{'feature':26s}{'human':>9s}{'AI':>9s}{'delta':>8s}{'size':>12s}"
          f"{'humanize?':>11s}   note")
    print("-" * 100)

    stubborn, fixed = [], []
    for k in rank:
        d = cliffs_delta(col(all_ai, k), col(human, k))
        if abs(d) < 0.33:
            continue
        # Does the humanize prompt close the gap relative to baseline?
        gap0 = abs(mean(c0, k) - mean(human, k))
        gap3 = abs(mean(c3, k) - mean(human, k))
        if gap0 == 0:
            closed = 0.0
        else:
            closed = (gap0 - gap3) / gap0
        tag = "reg?" if k in REGISTER_SUSPECT else ""
        verdict = (f"{closed*100:+.0f}%" if abs(closed) > 0.05 else "no move")
        if k not in REGISTER_SUSPECT and abs(d) >= 0.47 and closed < 0.25:
            stubborn.append((k, d, closed))
        elif closed >= 0.5:
            fixed.append((k, d, closed))
        print(f"{k:26s}{mean(human,k):9.2f}{mean(all_ai,k):9.2f}"
              f"{d:+8.2f}{magnitude(d):>12s}{verdict:>11s}   {tag}")

    print()
    print("=" * 100)
    print("THE SIGNAL WORTH BUILDING ON".center(100))
    print("=" * 100)
    print("Large separation, NOT obviously register, and the humanize prompt")
    print("closes less than a quarter of the gap:\n")
    for k, d, closed in sorted(stubborn, key=lambda x: -abs(x[1])):
        print(f"  {k:26s} delta={d:+.2f}  human={mean(human,k):8.2f}  "
              f"C0={mean(c0,k):8.2f}  C3={mean(c3,k):8.2f}  closed={closed*100:+.0f}%")
    if not stubborn:
        print("  (none)")

    print()
    print("What the humanize prompt DOES fix (closes >=50% of the gap) -")
    print("i.e. features that are cheap to defeat and weak for a detector:\n")
    for k, d, closed in sorted(fixed, key=lambda x: -x[2]):
        print(f"  {k:26s} closed={closed*100:+.0f}%  "
              f"human={mean(human,k):8.2f} C0={mean(c0,k):8.2f} C3={mean(c3,k):8.2f}")
    if not fixed:
        print("  (none)")

    print()
    print("=" * 100)
    print("PER-CONDITION MEANS (does STE move toward human or away?)".center(100))
    print("=" * 100)
    print(f"{'feature':26s}{'human':>10s}{'C0':>10s}{'C1_ste':>10s}{'C3_human':>10s}"
          f"   direction of STE")
    print("-" * 100)
    for k, _, _ in sorted(stubborn, key=lambda x: -abs(x[1]))[:14]:
        h, b, s = mean(human, k), mean(c0, k), mean(c1, k)
        toward = "toward human" if abs(s - h) < abs(b - h) else "AWAY from human"
        print(f"{k:26s}{h:10.2f}{b:10.2f}{s:10.2f}{mean(c3,k):10.2f}   {toward}")


if __name__ == "__main__":
    main()
