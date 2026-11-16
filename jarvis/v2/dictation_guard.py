"""The VA-3 dictation guard: what a cleanup pass may type for the owner.

Voice anywhere (VA-D2) types dictation into any app, terminals included, and
the text typed is whatever the cleanup pass returns. A model that adds or
answers turns a sentence into keystrokes the owner never said. So a model never
decides what is typed: this module does
(docs/plans/2026-11-16-voice-anywhere-va3-local-cleanup.md, "Hard rule").

`check(raw, cleaned)` accepts a cleanup only if it **deletes** from what the owner
said and changes nothing else that matters:

- After normalisation, the cleaned words are an order-preserving subsequence of
  the raw words. Normalisation case-folds, drops punctuation, expands a fixed
  contraction table, parses English number words (0-999,999) to digits and maps
  `percent` to `%`. Nothing else counts as the same word. Spoken punctuation
  (`comma`, `question mark`, ...) is an ordinary word for matching, so turning
  it into a symbol is a deletion; it is left out of the deletion cap.
- No newline (line breaks are VA-2's "new line" command) and no control, format,
  private-use or unassigned character, bidi controls and zero-width characters
  included.
- No symbol the raw text lacks, outside a small safe punctuation set. A model
  cannot introduce `|`, `>`, `$`, a backtick and the like into any app.
- At most `MAX_DELETION` of the raw words removed, fillers not counted, and never
  empty when the owner said something.
- Not much longer than the raw text.

`choose(raw, rules_text, model_text)` is the single decision point the
dictation path uses: the model's text when it passes, otherwise the rules text
when *it* passes, otherwise the raw transcript. The rules output is checked
too; a bug there cannot type anything the owner did not say either.

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

# Share of raw words (fillers excluded) a cleanup may delete. A self-correction
# can remove a lot ("on Tuesday no wait Wednesday" -> "on Wednesday" drops 3 of
# 5), so the cap is generous; it exists to stop "Yes." standing for a paragraph.
MAX_DELETION = 0.6

# Hesitation sounds that are never content. Not "like", "so" or "you know":
# those are real words in other sentences, so they count as words here.
FILLERS = frozenset({"um", "umm", "uh", "uhh", "er", "erm", "ah", "hmm", "mm", "mhm"})

# Punctuation a cleanup may add although the owner did not say it. Anything else
# must already be in the raw text.
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
_CONTRACTION = re.compile(r"\b(?:" + "|".join(map(re.escape, CONTRACTIONS)) + r")\b")
SMALL = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_VALUES = {w: n for n, w in enumerate(SMALL)} | {w: 20 + 10 * n for n, w in enumerate(TENS)}
SPOKEN_PUNCTUATION = re.compile(r"\b(?:full stop|question mark|exclamation point|exclamation mark|"
                                r"comma|period|colon|semicolon)\b")
_WORDS = re.compile(r"[^\W_]+(?:'[^\W_]+)?|%", re.UNICODE)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


def expand_contractions(text: str) -> str:
    return _CONTRACTION.sub(lambda m: CONTRACTIONS[m[0]],
                            text.casefold().replace("’", "'").replace("‘", "'"))


def _under_hundred(words: list[str], pos: int):
    if pos >= len(words) or words[pos] not in _VALUES:
        return None
    n, end = _VALUES[words[pos]], pos + 1
    if n >= 20 and end < len(words) and words[end] in SMALL[1:10]:
        n, end = n + _VALUES[words[end]], end + 1
    return n, end


def _under_thousand(words: list[str], pos: int):
    first = _under_hundred(words, pos)
    if first is None:
        return None
    n, end = first
    if 1 <= n <= 9 and end < len(words) and words[end] == "hundred":
        n, end = n * 100, end + 1
        start = end + (end < len(words) and words[end] == "and")
        tail = _under_hundred(words, start)
        if tail is not None and tail[0] > 0:
            n, end = n + tail[0], tail[1]
    return n, end


def tokens(text: str, *, raw: bool = False) -> list[str]:
    """Normalised words of `text`. `raw=True` is the owner's side: spoken
    punctuation words are dropped there, never on the cleaned side."""
    text = expand_contractions(text)
    if raw:
        text = SPOKEN_PUNCTUATION.sub(" ", text)
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)  # 1,000 -> 1000
    words = _WORDS.findall(text)
    out: list[str] = []
    pos = 0
    while pos < len(words):
        parsed = _under_thousand(words, pos)
        if parsed is None:
            word = words[pos]
            if word == "%":
                word = "percent"
            elif word.isdecimal():
                word = word.lstrip("0") or "0"
            out.append(word)
            pos += 1
            continue
        n, end = parsed
        if n > 0 and end < len(words) and words[end] == "thousand":
            n, end = n * 1000, end + 1
            start = end + (end < len(words) and words[end] == "and")
            tail = _under_thousand(words, start)
            if tail is not None and tail[0] > 0:
                n, end = n + tail[0], tail[1]
        out.append(str(n))
        pos = end
    return out


def _subsequence(raw: list[str], cleaned: list[str]) -> bool:
    remaining = iter(raw)
    return all(any(word == candidate for candidate in remaining) for word in cleaned)


def check(raw: str, cleaned: str) -> Verdict:
    """May `cleaned` be typed for an owner who said `raw`? Fixed reason strings."""
    if not isinstance(raw, str) or not isinstance(cleaned, str):
        return Verdict(False, "not_text")
    if any(c in _BREAKS for c in cleaned):
        return Verdict(False, "newline")
    if any(unicodedata.category(c)[0] == "C" for c in cleaned):
        return Verdict(False, "control_char")
    if any(not (c.isalnum() or c == " " or c in SAFE_SYMBOLS or c in raw) for c in cleaned):
        return Verdict(False, "new_symbol")
    if len(cleaned) > len(raw) + 16 + len(raw) // 10:
        return Verdict(False, "too_long")
    # Matching uses every word the owner said, spoken punctuation included: a
    # cleanup that turns "comma" into "," simply deletes it, and one that keeps
    # a literal "grace period" keeps it. Only the deletion cap and the empty
    # test leave spoken punctuation out, as they leave out fillers.
    said, typed = tokens(raw), tokens(cleaned)
    content = [w for w in tokens(raw, raw=True) if w not in FILLERS]
    if not typed:
        return Verdict(False, "empty") if content else Verdict(True)
    if not _subsequence(said, typed):
        extra = Counter(typed) - Counter(said)  # a repeated word counts as added
        return Verdict(False, "added_word" if extra else "reordered")
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
