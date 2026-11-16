"""The VA-3 dictation guard: what a cleanup pass may type for the owner.

Voice anywhere (VA-D2) types dictation into any app, terminals included, and
the text typed is whatever the cleanup pass returns. A model that adds or
answers turns a sentence into keystrokes the owner never said. So a model never
decides what is typed: this module does
(docs/plans/2026-11-16-voice-anywhere-va3-local-cleanup.md, "Hard rule").

`check(raw, cleaned)` accepts a cleanup only if it **deletes** from what the owner
said and changes nothing else that matters:

- **Words.** After normalisation, the cleaned words are an order-preserving
  subsequence of the raw words. Normalisation lower-cases (`str.lower`, not
  `casefold`: "Maße" is not "Masse"), expands a fixed contraction table and maps
  `%` to `percent`. Spoken punctuation (`comma`, `question mark`, ...) is an
  ordinary word for matching, so turning it into a symbol is a deletion.
- **Numbers.** A digit string is one token, decimals and separators included
  (`1.05` is not `1.5`; `10:00` is one token; only a grouping comma as in
  `1,000` is dropped). A run of number words becomes digits only when the
  **whole run** reads as exactly one number from 0 to 999,999 ("two thousand
  twenty six" -> `2026`) or as a clock time ("four forty five" -> `4:45`). Any
  other run ("twenty twenty six", "one two", "one thousand one thousand") stays
  words, so no digit form of it is accepted. Runs never cross punctuation, and
  "and" never joins numbers.
- **Characters.** No newline (line breaks are VA-2's "new line" command), no
  control, format, private-use or unassigned character (bidi and zero-width
  included), no compatibility character such as a ligature unless the raw text
  has it, and no symbol outside a small safe set unless the raw text has it.
- **Where punctuation goes.** Between words, a cleanup may add only prose
  punctuation: a comma, semicolon or colon before a space, sentence punctuation
  with optional quotes or brackets around a space, a hyphen joining two words
  with no spaces, an opening quote or bracket at the start, closing punctuation
  at the end. Any other gap must appear as a gap in the raw text. So "ls la"
  cannot become "ls -la", "five" cannot become "-5", "echo hello" cannot become
  "!!; echo hello".
- At most `MAX_DELETION` of the raw words removed, fillers and spoken
  punctuation not counted, and never empty when the owner said something. Not
  much longer than the raw text. Identical text always passes: it is what the
  raw fallback types anyway.

`choose(raw, rules_text, model_text)` is the single decision point the
dictation path uses: the model's text when it passes, otherwise the rules text
when *it* passes, otherwise the raw transcript.

Pure standard library. Importing this module must not import `jarvis.config` or
anything else: the offline data tool (`voiceclean`) calls it as its pair check,
and `tests/v2/dictation_guard_check.py` holds it to that.

This file is in `SELF_PROTECTED` (VA3-D2): it is authorization code, as
`voice_link.py` is.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import unicodedata

# Share of raw words (fillers and spoken punctuation excluded) a cleanup may
# delete. A self-correction can remove a lot ("on Tuesday no wait Wednesday" ->
# "on Wednesday" drops 3 of 5); the cap stops "Yes." standing for a paragraph.
MAX_DELETION = 0.6

# Hesitation sounds that are never content. Not "like", "so" or "you know":
# those are real words in other sentences, so they count as words here.
FILLERS = frozenset({"um", "umm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm", "mhm"})

# Characters a cleanup may add although the owner did not say them. Where they
# may go is judged separately (`_gap_ok`).
SAFE_SYMBOLS = frozenset(".,?!'\"-:;()%’‘“”")

# Line and paragraph breaks, judged before the general category test so the
# reason says what happened.
_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85  ")

CONTRACTIONS = {
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "can't": "can not", "cannot": "can not", "couldn't": "could not",
    "won't": "will not", "wouldn't": "would not", "shouldn't": "should not",
    "isn't": "is not", "aren't": "are not", "wasn't": "was not",
    "weren't": "were not", "hasn't": "has not", "haven't": "have not",
    "hadn't": "had not", "mustn't": "must not", "needn't": "need not",
    "i'm": "i am", "i've": "i have", "i'll": "i will", "i'd": "i would",
    "you're": "you are", "you've": "you have", "you'll": "you will",
    "you'd": "you would", "he's": "he is", "he'll": "he will",
    "she's": "she is", "she'll": "she will", "it's": "it is",
    "it'll": "it will", "we're": "we are", "we've": "we have",
    "we'll": "we will", "we'd": "we would", "they're": "they are",
    "they've": "they have", "they'll": "they will", "they'd": "they would",
    "that's": "that is", "there's": "there is", "here's": "here is",
    "what's": "what is", "who's": "who is", "let's": "let us",
}
_CONTRACTION = re.compile(r"(?<![^\W_])(?:" + "|".join(map(re.escape, CONTRACTIONS)) + r")(?![^\W_])")
SMALL = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_VALUES = {w: n for n, w in enumerate(SMALL)} | {w: 20 + 10 * n for n, w in enumerate(TENS)}
_NUMBER_WORDS = frozenset(_VALUES) | {"hundred", "thousand"}
SPOKEN_PUNCTUATION = re.compile(r"\b(?:full stop|question mark|exclamation point|exclamation mark|"
                                r"comma|period|colon|semicolon)\b")

# One item of text: a digit string (with decimal, time or grouping separators,
# and an optional %) that is not glued to letters, or a word with internal
# apostrophes. Everything between items is a gap.
_ITEM = re.compile(r"\d+(?:[.,:/]\d+)*(?![^\W_])%?|[^\W_]+(?:'[^\W_]+)*")
_GROUPED = re.compile(r"\d{1,3}(?:,\d{3})+")

_OPEN = "\"'“‘("
_CLOSE = "\"'”’)"
# Prose punctuation after a word: commas, stops and closing quotes/brackets in
# either order ("word.)" or "word).").
_AFTER = r"[,;:.?!]{0,3}[" + re.escape(_CLOSE) + r"]?[,;:.?!]{0,3}"
_INNER_GAP = re.compile(_AFTER + r"\s+[" + re.escape(_OPEN) + r"]?|-")
_LEAD_GAP = re.compile(r"\s*[" + re.escape(_OPEN) + r"]?")
_TRAIL_GAP = re.compile(_AFTER + r"\s*")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


def _fold(text: str) -> str:
    return text.lower().replace("’", "'").replace("‘", "'")


def expand_contractions(text: str) -> str:
    return _CONTRACTION.sub(lambda m: CONTRACTIONS[m[0]], _fold(text))


def _parse_run(words: list[str]):
    """The whole run as one number 0..999,999, else None. No "and"."""
    def under_hundred(pos):
        if pos >= len(words) or words[pos] not in _VALUES:
            return None
        n, end = _VALUES[words[pos]], pos + 1
        if 20 <= n and end < len(words) and words[end] in SMALL[1:10]:
            n, end = n + _VALUES[words[end]], end + 1
        return n, end

    def under_thousand(pos):
        first = under_hundred(pos)
        if first is None:
            return None
        n, end = first
        if 1 <= n <= 9 and end < len(words) and words[end] == "hundred":
            n, end = n * 100, end + 1
            tail = under_hundred(end)
            if tail is not None and tail[0] > 0:
                n, end = n + tail[0], tail[1]
        return n, end

    head = under_thousand(0)
    if head is None:
        return None
    n, end = head
    if n > 0 and end < len(words) and words[end] == "thousand":
        n, end = n * 1000, end + 1
        tail = under_thousand(end)
        if tail is not None and tail[0] > 0:
            n, end = n + tail[0], tail[1]
    return n if end == len(words) else None


def _number_run(words: list[str]) -> list[str]:
    n = _parse_run(words)
    if n is not None:
        return [str(n)]
    # A clock time: "four forty five" -> 4:45 (hour 1-12, minutes 10-59).
    if 2 <= len(words) <= 3 and words[0] in SMALL[1:13]:
        minutes = _parse_run(words[1:])
        if minutes is not None and 10 <= minutes <= 59:
            return [f"{_VALUES[words[0]]}:{minutes:02d}"]
    return list(words)


def _items(text: str) -> list[re.Match]:
    return list(_ITEM.finditer(text))


def tokens(text: str, *, raw: bool = False) -> list[str]:
    """Normalised words of `text`. `raw=True` also drops spoken punctuation
    words; the guard uses that only to count content, never for matching."""
    text = expand_contractions(text)
    if raw:
        text = SPOKEN_PUNCTUATION.sub(" ", text)
    out: list[str] = []
    run: list[str] = []
    last_end = 0
    for m in _items(text):
        # A number-word run continues only across plain whitespace.
        if run and text[last_end:m.start()].strip():
            out.extend(_number_run(run))
            run = []
        word = m[0]
        last_end = m.end()
        if word in _NUMBER_WORDS:
            run.append(word)
            continue
        if run:
            out.extend(_number_run(run))
            run = []
        if word[0].isdigit():
            percent = word.endswith("%")
            digits = word[:-1] if percent else word
            out.append(digits.replace(",", "") if _GROUPED.fullmatch(digits) else digits)
            if percent:
                out.append("percent")
        else:
            out.append(word)
    if run:
        out.extend(_number_run(run))
    return out


def _gaps(text: str) -> tuple[str, list[str], str] | None:
    items = _items(text)
    if not items:
        return None
    inner = [text[a.end():b.start()] for a, b in zip(items, items[1:])]
    return text[:items[0].start()], inner, text[items[-1].end():]


def _gap_ok(raw: str, cleaned: str) -> bool:
    """Punctuation may be added only where prose puts it; any other gap must
    already be a gap the owner's text has."""
    parts = _gaps(cleaned)
    if parts is None:
        return not cleaned.strip()
    raw_parts = _gaps(raw)
    said = set()
    if raw_parts is not None:
        lead, inner, trail = raw_parts
        said = {g.strip() for g in (lead, *inner, trail) if g.strip()}
    lead, inner, trail = parts

    def known(gap):
        return gap.strip() in said

    if not (_LEAD_GAP.fullmatch(lead) or known(lead)):
        return False
    if not (_TRAIL_GAP.fullmatch(trail) or known(trail)):
        return False
    return all(_INNER_GAP.fullmatch(g) or known(g) for g in inner)


def _subsequence(said: list[str], typed: list[str]) -> bool:
    remaining = iter(said)
    return all(any(word == candidate for candidate in remaining) for word in typed)


def check(raw: str, cleaned: str) -> Verdict:
    """May `cleaned` be typed for an owner who said `raw`? Fixed reason strings."""
    if not isinstance(raw, str) or not isinstance(cleaned, str):
        return Verdict(False, "not_text")
    if cleaned == raw:
        return Verdict(True)  # what the raw fallback types anyway
    if any(c in _BREAKS for c in cleaned):
        return Verdict(False, "newline")
    if any(unicodedata.category(c)[0] == "C" for c in cleaned):
        return Verdict(False, "control_char")
    for c in cleaned:
        if c in raw:
            continue
        if unicodedata.normalize("NFKC", c) != c:
            return Verdict(False, "new_symbol")  # ligatures, full-width forms, ...
        if not (c.isalnum() or c == " " or c in SAFE_SYMBOLS):
            return Verdict(False, "new_symbol")
    if len(cleaned) > len(raw) + 16 + len(raw) // 10:
        return Verdict(False, "too_long")
    said, typed = tokens(raw), tokens(cleaned)
    content = [w for w in tokens(raw, raw=True) if w not in FILLERS]
    if not typed:
        if content:
            return Verdict(False, "empty")
        return Verdict(True) if _gap_ok(raw, cleaned) else Verdict(False, "new_symbol")
    if not _subsequence(said, typed):
        extra = Counter(typed) - Counter(said)  # a repeated word counts as added
        return Verdict(False, "added_word" if extra else "reordered")
    if not _gap_ok(raw, cleaned):
        return Verdict(False, "new_symbol")
    kept = sum(1 for w in typed if w not in FILLERS)
    if content and kept < len(content) * (1 - MAX_DELETION):
        return Verdict(False, "over_deletion")
    return Verdict(True)


def choose(raw: str, rules_text: str, model_text: str | None) -> tuple[str, str]:
    """The text to type and which pass produced it: `model`, `rules` or `raw`."""
    if model_text is not None and check(raw, model_text).ok:
        return model_text, "model"
    if check(raw, rules_text).ok:
        return rules_text, "rules"
    return raw, "raw"
