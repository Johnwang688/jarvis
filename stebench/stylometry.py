"""Dependency-free stylometry and partial STE-compliance scoring.

Two jobs:

1. Stylometry - the features a detector actually keys on. Burstiness
   (sentence-length SD) is the one to watch: GPTZero's public methodology
   names perplexity and burstiness, and STE caps sentence length, which
   should collapse the second one.

2. STE compliance - whether the model DID what it was told. Without this,
   a null result is ambiguous between "STE does not help" and "the model
   ignored the instruction", and those need different follow-ups.

   IMPORTANT LIMIT: this scores only the rules checkable WITHOUT the
   ASD-STE100 approved-word dictionary - sentence length, voice, one
   instruction per sentence, paragraph length, -ing constructs, noun
   clusters. Vocabulary compliance (the ~900-word approved list, which
   needs the Issue 9 PDF parsed) is NOT measured here. Anything reported
   from this module is a ceiling on true compliance, never a floor.

Passive-voice detection is a heuristic (form of "be" + past participle),
not a parse. It is fine for comparing conditions against each other and
should not be quoted as an absolute rate.
"""

import re
import statistics
from typing import Any

_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "st", "e.g", "i.e", "etc", "vs", "fig", "no"}

_BE = {"is", "are", "was", "were", "be", "been", "being", "am", "get", "gets", "got"}

# Irregular past participles common enough to matter in technical prose.
_IRREGULAR_PP = {
    "made", "done", "seen", "taken", "given", "held", "kept", "built", "sent",
    "spent", "found", "left", "lost", "met", "paid", "put", "read", "run",
    "set", "shut", "sold", "told", "understood", "worn", "written", "driven",
    "drawn", "known", "grown", "shown", "thrown", "broken", "chosen", "frozen",
    "spoken", "stolen", "woken", "hidden", "ridden", "risen", "cut", "hit",
    "let", "cost", "burst", "bent", "dealt", "felt", "meant", "swept",
}

_IMPERATIVE_VERBS = {
    "remove", "install", "attach", "detach", "connect", "disconnect", "turn",
    "push", "pull", "lift", "lower", "open", "close", "tighten", "loosen",
    "check", "make", "put", "set", "start", "stop", "hold", "release", "clean",
    "apply", "align", "insert", "remove", "replace", "examine", "measure",
    "adjust", "fill", "drain", "bleed", "press", "release", "rotate", "engage",
    "disengage", "do", "keep", "let", "use", "wait", "repeat", "continue",
}

_SUBORDINATORS = {
    "although", "because", "since", "unless", "whereas", "while", "whilst",
    "though", "if", "when", "whenever", "after", "before", "until", "which",
    "who", "whom", "whose", "that",
}


def split_sentences(text: str) -> list[str]:
    """Regex sentence splitter that survives common abbreviations."""
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return []
    parts, buf = [], []
    for tok in re.split(r"(?<=[.!?])\s+", text):
        buf.append(tok)
        stem = re.sub(r"[^a-z.]", "", tok.split()[-1].lower()) if tok.split() else ""
        if stem.rstrip(".") in _ABBREV:
            continue
        parts.append(" ".join(buf))
        buf = []
    if buf:
        parts.append(" ".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'-]*", text)


def _is_passive(sentence: str) -> bool:
    toks = [w.lower() for w in words(sentence)]
    for i, w in enumerate(toks[:-1]):
        if w not in _BE:
            continue
        for nxt in toks[i + 1 : i + 3]:  # allow one adverb between
            if nxt in _IRREGULAR_PP or (nxt.endswith("ed") and len(nxt) > 3):
                return True
    return False


def _starts_imperative(sentence: str) -> bool:
    w = words(sentence)
    return bool(w) and w[0].lower() in _IMPERATIVE_VERBS


def _max_noun_cluster(sentence: str) -> int:
    """Longest run of consecutive capitalised-or-lowercase nouns is not
    computable without a tagger. Approximate: longest run of words that are
    neither function words nor verbs, bounded by punctuation."""
    function = _SUBORDINATORS | _BE | {
        "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
        "for", "with", "from", "by", "as", "into", "onto", "over", "under",
        "this", "these", "those", "it", "its", "you", "your", "not", "no",
    }
    best = run = 0
    for tok in re.split(r"[^A-Za-z'-]+", sentence):
        if not tok:
            continue
        if tok.lower() in function or tok.lower().endswith("ly"):
            run = 0
        else:
            run += 1
            best = max(best, run)
    return best


def stylometry(text: str) -> dict[str, Any]:
    sents = split_sentences(text)
    lens = [len(words(s)) for s in sents]
    w = words(text)
    lower = [x.lower() for x in w]
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]

    return {
        "n_words": len(w),
        "n_sentences": len(sents),
        "n_paragraphs": len(paras),
        "sent_len_mean": round(statistics.fmean(lens), 2) if lens else 0.0,
        # burstiness - the feature STE should crush
        "sent_len_sd": round(statistics.pstdev(lens), 2) if len(lens) > 1 else 0.0,
        "sent_len_max": max(lens) if lens else 0,
        "ttr": round(len(set(lower)) / len(lower), 4) if lower else 0.0,
        "mean_word_len": round(statistics.fmean([len(x) for x in w]), 2) if w else 0.0,
        "comma_per_sent": round(text.count(",") / len(sents), 3) if sents else 0.0,
        "passive_rate": round(sum(_is_passive(s) for s in sents) / len(sents), 3) if sents else 0.0,
        "subordinator_rate": round(
            sum(1 for x in lower if x in _SUBORDINATORS) / len(lower), 4
        ) if lower else 0.0,
        "contraction_rate": round(len(re.findall(r"\w'(?:s|t|re|ve|ll|d|m)\b", text)) / len(w), 4)
        if w else 0.0,
    }


def ste_compliance(text: str) -> dict[str, Any]:
    """Partial STE compliance. Dictionary rules NOT included - see module docstring."""
    sents = split_sentences(text)
    if not sents:
        return {"ste_score": 0.0, "ste_measured_rules": 0}
    lens = [len(words(s)) for s in sents]
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]

    within_20 = sum(n <= 20 for n in lens) / len(lens)
    within_25 = sum(n <= 25 for n in lens) / len(lens)
    active = 1.0 - (sum(_is_passive(s) for s in sents) / len(sents))
    # one instruction per sentence: among imperative sentences, penalise
    # those chaining a second command with "and"/";"
    imper = [s for s in sents if _starts_imperative(s)]
    single_instr = (
        sum(1 for s in imper if not re.search(r"\b(and|then)\b\s+\w+|;", s.lower())) / len(imper)
        if imper else 1.0
    )
    para_ok = (
        sum(1 for p in paras if len(split_sentences(p)) <= 6) / len(paras) if paras else 1.0
    )
    # -ing as clause opener or gerund subject
    ing_bad = sum(1 for s in sents if re.match(r"^\s*\w+ing\b", s)) / len(sents)
    cluster_ok = sum(1 for s in sents if _max_noun_cluster(s) <= 3) / len(sents)

    parts = {
        "r_sent_len_20": round(within_20, 3),
        "r_sent_len_25": round(within_25, 3),
        "r_active_voice": round(active, 3),
        "r_one_instruction": round(single_instr, 3),
        "r_para_len": round(para_ok, 3),
        "r_no_ing_opener": round(1.0 - ing_bad, 3),
        "r_noun_cluster": round(cluster_ok, 3),
    }
    parts["ste_score"] = round(statistics.fmean(parts.values()), 3)
    parts["ste_measured_rules"] = 7
    parts["ste_vocab_measured"] = False
    return parts


def features(text: str) -> dict[str, Any]:
    return {**stylometry(text), **ste_compliance(text)}
