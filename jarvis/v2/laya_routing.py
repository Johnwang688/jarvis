"""Recording-only voice decisions for the offline Laya benchmark."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from numbers import Real

from windows.voicetype import JARVIS_ADDRESS, web_destination


MIN_CONFIDENCE = 0.65
MIN_MARGIN = 0.20
CHOICES = (
    "ask", "navigate", "open_app", "switch_app", "search", "read_selection", "clarify",
)

_LITERAL = re.compile(
    r"^\s*(?:(?:please|kindly)\s+)?(?:type(?: out)?|dictate|insert)\s+"
    r"(?:(?:the\s+)?(?:words|phrase|sentence|text)|literally|verbatim)(?=[: ,\s])"
    r"(?:\s*[:,]\s*|\s+)(?=\S)", re.I,
)
_LITERAL_QUOTED = re.compile(
    r"^\s*(?:(?:please|kindly)\s+)?(?:type(?: out)?|dictate|insert)"
    r"(?:\s*[:,]\s*|\s+)(?=[\"“‘])", re.I,
)

# Token boundaries adapted from the prior website validator. Paths, queries,
# credentials, ports and domains embedded in email addresses are excluded.
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_SPOKEN_DOT = re.compile(
    r"(?<=[a-z0-9])\s+dot(?:\s+|-)(?=[a-z0-9])|(?<=[a-z0-9]) +\. +(?=[a-z0-9])",
    re.I | re.ASCII,
)
_HOST_TOKEN = re.compile(
    rf"(?<![\w.@:/\\%#?&=+~-])((?:https?://)?(?:{_DNS_LABEL}\.)+[a-z]{{2,63}}/?)"
    r"(?![\w@:/\\%#&=+~-]|\?(?=\S)|\.[a-z0-9])", re.I | re.ASCII,
)
# ASR can concatenate segments without a space before a new capitalized greeting.
_FUSED_GREETING = re.compile(
    r"(\.[a-z]{2,63})(?=(?:Yo|Hey|Hi|Hello|Okay|OK)[, ]+)", re.ASCII,
)
# An entire hostname can also be repeated with no separator.
_REPEATED_HOST = re.compile(
    rf"(?<![\w.@:/\\%#?&=+~-])((?:{_DNS_LABEL}\.)+[a-z]{{2,63}}?)(?:\1)+"
    r"(?![\w@:/\\%#&=+~-]|\?(?=\S)|\.[a-z0-9])", re.I | re.ASCII,
)

_SEARCH_CUE = re.compile(
    r"\b(?:(?:search|look) (?:online|the (?:web|internet)) for|"
    r"(?:(?:do|run) a )?(?:(?:web|online) )?search for|find online results for|"
    r"look up|search the web(?: about)?(?!\s+for\b)|google)\s+", re.I,
)
_SEARCH_TRAILER = re.compile(r"(?<!\S)(?:for me|please)(?:[,\s]+(?:for me|please))*$", re.I)
_SECOND_SEARCH = re.compile(r"\b(?:and|or|then)\s+" + _SEARCH_CUE.pattern, re.I)


def _result(kind, target=""):
    return {"kind": kind, "target": target}


def _literal_payload(text):
    body = JARVIS_ADDRESS.sub("", text.lstrip(), count=1)
    match = _LITERAL.match(body) or _LITERAL_QUOTED.match(body)
    if match:
        return body[match.end():]
    return ""


def _groundable(text):
    return len(text) <= 2000 and all(
        not unicodedata.category(char).startswith("C") or char == "\t" for char in text
    )


def _website(text):
    text = _SPOKEN_DOT.sub(".", text)
    text = _FUSED_GREETING.sub(r"\1 ", text)
    text = _REPEATED_HOST.sub(r"\1", text)
    targets = {web_destination(match[1]) for match in _HOST_TOKEN.finditer(text)}
    targets.discard("")
    return next(iter(targets)) if len(targets) == 1 else ""


def _named_target(text, candidates):
    # Inventory entries with identical names are ambiguous. Nested names that
    # only occur inside a longer match refer to that longer name.
    matches = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, str) or not candidate.strip() or not _groundable(candidate):
            continue
        phrase = r"\s+".join(map(re.escape, candidate.split()))
        pattern = rf"(?<![\w+]){phrase}(?![\w+])"
        matches.extend((match.start(), match.end(), index)
                       for match in re.finditer(pattern, text, re.I))

    named = {index for start, end, index in matches
             if not any(left <= start and end <= right and (left, right) != (start, end)
                        for left, right, _ in matches)}
    return candidates[next(iter(named))] if len(named) == 1 else ""


def _search_query(text):
    text = JARVIS_ADDRESS.sub("", text.lstrip(), count=1)
    match = _SEARCH_CUE.search(text)
    if not match:
        return ""
    query = text[match.end():].strip().rstrip(".,!?").rstrip()
    # A second search request can name a different query in the same utterance.
    if query.casefold() in {"me", "us"} or _SECOND_SEARCH.search(query):
        return ""
    return _SEARCH_TRAILER.sub("", query).rstrip(" ,")


def _rank(scores):
    if not isinstance(scores, Mapping) or not 2 <= len(scores) <= len(CHOICES):
        raise ValueError("expected at least two route scores")
    if any(kind not in CHOICES or isinstance(score, bool) or not isinstance(score, Real)
           or not math.isfinite(score) or not 0 <= score <= 1
           for kind, score in scores.items()):
        raise ValueError("invalid route scores")
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)[:2]


def decide(mode, text, classify, *, apps=(), windows=()):
    """Return a recorded decision. Classify once with the original transcript.

    Classifiers return a mapping of at least two known routes to finite scores
    in [0, 1]. This adapter uses the highest score and its lead over the runner-up.
    """
    if mode == "dictation":
        return _result("dictate", text)
    if mode not in ("assistant", "wake"):
        raise ValueError(f"unknown voice mode: {mode!r}")
    if payload := _literal_payload(text):
        return _result("literal", payload)

    try:
        (kind, best), (_, runner_up) = _rank(classify(text))
    except Exception:
        return _result("unavailable")

    if best < MIN_CONFIDENCE or round(best - runner_up, 12) < MIN_MARGIN:
        return _result("clarify")
    if kind in ("ask", "read_selection", "clarify"):
        return _result(kind)
    if not _groundable(text):
        return _result("clarify")

    if kind == "navigate":
        target = _website(text)
    elif kind == "search":
        target = _search_query(text)
    else:
        target = _named_target(text, tuple(apps if kind == "open_app" else windows))
    return _result(kind, target) if target else _result("clarify")
