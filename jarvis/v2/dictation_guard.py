"""The VA-3 dictation guard: what a cleanup pass may type for the owner.

Voice anywhere (VA-D2) types dictation into any app, terminals included, and
the text typed is whatever the cleanup pass returns. A model that adds or
answers turns a sentence into keystrokes the owner never said. So a model never
decides what is typed: this module does
(docs/plans/2026-11-16-voice-anywhere-va3-local-cleanup.md, "Hard rule").

`check(raw, cleaned)` is an **allow-list of edits**, not a normaliser. Split
both texts on spaces. Every typed word must be produced, in order, from the
owner's words by exactly one of these, and anything else is refused:

1. **Keep** a raw word, letters' case changed only for ASCII letters. The
   word's own characters stay as said: a sign, a decimal point, a `%` or a
   bracket is part of the word and cannot be dropped or changed.
2. **Merge a contraction** from two raw words, or **split** one into two, from a
   fixed table ("do not" <-> "don't").
3. **Write a number run as digits**: a maximal run of number words becomes one
   digit string only when the whole run reads as one number from 0 to 999,999
   or a clock time ("two thousand twenty six" -> `2026`, "four forty five" ->
   `4:45`), optionally with a following "percent" as `%`, and a grouping comma
   (`1,000`) allowed.
4. **Join two raw words with one hyphen**, letters only ("re enable" ->
   `re-enable`).

Raw words not used are deleted. After its word, a typed word may end with one
of `, . ? ! ?! ...`, or with the raw word's own trailing punctuation. Nothing
else may be added anywhere: no leading symbols, no quotes or brackets the owner
did not say, no gap reused from elsewhere. At most `MAX_DELETION` of the raw
words may be deleted (fillers and spoken punctuation not counted), and the
result is never empty when the owner said something. Only plain spaces separate
words: no newline (VA-2's "new line" command does that), tab, control, format,
bidi or zero-width character. Identical text always passes, because it is what
the raw fallback types anyway.

`choose(raw, rules_text, model_text)` is the single decision point the
dictation path uses: the model's text when it passes, otherwise the rules text
when *it* passes, otherwise the raw transcript.

`tokens()` is a looser normaliser kept for the offline data tool (ids, dedupe,
word error rate). It is not used to decide what is typed.

Pure standard library. Importing this module must not import `jarvis.config` or
anything else: `voiceclean` calls it as its pair check, and
`tests/v2/dictation_guard_check.py` holds it to that.

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


_END = frozenset({"", ",", ".", "?", "!", "?!", "..."})
_STRIP = ",.?!"
_SPOKEN_PHRASES = (("full", "stop"), ("question", "mark"), ("exclamation", "point"),
                   ("exclamation", "mark"), ("comma",), ("period",), ("colon",), ("semicolon",))
_MAX_WORDS = 300
# A word that may take added punctuation after it: letters/digits with internal
# apostrophes, or a number. "$", "/tmp/" and other syntax may not ("$" -> "$?").
_PLAIN = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*|\d+(?:[.,:]\d+)*%?")
# Words that belong to a spoken number and go with it: kept whole or deleted
# whole, so "minus five" cannot become "5" and "twenty five" cannot become "5".
_SIGNS = frozenset({"minus", "negative"})
_APOSTROPHES = frozenset({"'", "\u2019"})


def _ascii(text: str) -> bool:
    return text.replace("\u2019", "'").isascii()


def _same(a: str, b: str) -> bool:
    """Equal, except that ASCII letters may differ in case and a straight and a
    curly apostrophe count as the same."""
    return len(a) == len(b) and all(
        x == y or (x in _APOSTROPHES and y in _APOSTROPHES)
        or (x.isascii() and y.isascii() and x.isalpha() and y.isalpha() and x.lower() == y.lower())
        for x, y in zip(a, b))


def _split_word(word: str) -> tuple[str, str]:
    core = word.rstrip(_STRIP)
    if not core:
        return word, ""
    return core, word[len(core):]


def _key(core: str) -> str:
    return core.replace("\u2019", "'").replace("\u2018", "'").lower()


def _uncounted(words: list[str]) -> list[bool]:
    """Raw words that do not count toward the deletion cap: fillers and spoken
    punctuation phrases."""
    keys = [_key(_split_word(w)[0]) for w in words]
    out = [k in FILLERS for k in keys]
    for i in range(len(keys)):
        for phrase in _SPOKEN_PHRASES:
            if tuple(keys[i:i + len(phrase)]) == phrase:
                for k in range(i, i + len(phrase)):
                    out[k] = True
    return out


class _Aligner:
    def __init__(self, raw_words: list[str], typed_words: list[str]):
        self.raw = [_split_word(w) for w in raw_words]
        self.keys = [_key(c) for c, _ in self.raw]
        self.typed = [_split_word(w) for w in typed_words]
        self.free = _uncounted(raw_words)
        self.memo: dict[tuple[int, int], int | None] = {}
        self.atom_start, self.atom_end = self._atoms()

    def _member(self, x: int) -> bool:
        return self.keys[x] in _NUMBER_WORDS or self.raw[x][0][:1].isdigit()

    def _atoms(self) -> tuple[list[int], list[int]]:
        """For each raw word, the first and one-past-last index of the number
        unit it belongs to (itself alone when it is in none). A unit is a run of
        number words or digit words, with a sign before it, "point" inside it
        and "percent" after it; punctuation after a word ends it."""
        n = len(self.raw)
        start, end = list(range(n)), [x + 1 for x in range(n)]
        x = 0
        while x < n:
            first = x
            if self.keys[x] in _SIGNS and x + 1 < n and not self.raw[x][1] and self._member(x + 1):
                x += 1
            if not self._member(x):
                x = first + 1
                continue
            last = x
            while not self.raw[last][1] and last + 1 < n:
                nxt = last + 1
                if self._member(nxt):
                    last = nxt
                elif (self.keys[nxt] == "point" and nxt + 1 < n and not self.raw[nxt][1]
                        and self._member(nxt + 1)):
                    last = nxt + 1
                elif self.keys[nxt] == "percent":
                    last = nxt
                    break
                else:
                    break
            for y in range(first, last + 1):
                start[y], end[y] = first, last + 1
            x = last + 1
        return start, end

    def _inside(self, x: int) -> bool:
        return x < len(self.raw) and self.atom_start[x] < x

    def _tail_ok(self, tail: str, raw_tail: str, core: str = "") -> bool:
        if tail == raw_tail:
            return True
        return tail in _END and bool(_PLAIN.fullmatch(core))

    def _forms(self, j: int, k: int):
        """(raw words consumed, typed words consumed) for typed[j] at raw k."""
        core, tail = self.typed[j]
        n = len(self.raw)
        raw_core, raw_tail = self.raw[k]
        # 1. keep
        if _same(core, raw_core) and self._tail_ok(tail, raw_tail, raw_core):
            yield 1, 1
        plain = _ascii(core)
        # 2a. merge a contraction from two raw words
        if plain and k + 1 < n and not raw_tail and _key(core) in CONTRACTIONS:
            if CONTRACTIONS[_key(core)].split() == [self.keys[k], self.keys[k + 1]] \
                    and _ascii(self.raw[k][0] + self.raw[k + 1][0]) \
                    and self._tail_ok(tail, self.raw[k + 1][1], core):
                yield 2, 1
        # 2b. split a raw contraction into two typed words
        if self.keys[k] in CONTRACTIONS and j + 1 < len(self.typed) and plain and not tail:
            first, second = CONTRACTIONS[self.keys[k]].split()
            core2, tail2 = self.typed[j + 1]
            if _ascii(core2) and _key(core) == first and _key(core2) == second \
                    and self._tail_ok(tail2, raw_tail, core2):
                yield 1, 2
        # 3. a whole number-word run as digits
        # Only from the start of its number unit, and only covering all of it.
        if self.keys[k] in _NUMBER_WORDS and core[:1].isdigit() and not self._inside(k):
            end = k
            while end < n and self.keys[end] in _NUMBER_WORDS:
                end += 1
                if self.raw[end - 1][1]:
                    break  # punctuation ends a run
            if all(not t for _, t in self.raw[k:end - 1]):
                form = _number_run(self.keys[k:end])
                if len(form) == 1 and form[0][:1].isdigit():
                    pct = core.endswith("%")
                    body = core[:-1] if pct else core
                    digits = (body.replace(",", "") if _GROUPED.fullmatch(body) else body) + ("%" if pct else "")
                    last_tail = self.raw[end - 1][1]
                    unit_end = self.atom_end[k]
                    if digits == form[0] and end == unit_end and self._tail_ok(tail, last_tail, core):
                        yield end - k, 1
                    if (digits == form[0] + "%" and end < n and self.keys[end] == "percent"
                            and end + 1 == unit_end and not last_tail
                            and self._tail_ok(tail, self.raw[end][1], core)):
                        yield end - k + 1, 1
        # 4. two raw words joined by one hyphen, letters only
        if plain and core.count("-") == 1 and k + 1 < n and not raw_tail:
            left, right = core.split("-")
            if left.isalpha() and right.isalpha() and _same(left, raw_core) \
                    and _same(right, self.raw[k + 1][0]) and self._tail_ok(tail, self.raw[k + 1][1], core):
                yield 2, 1

    def best(self, i: int, j: int) -> int | None:
        """Most counted raw words kept aligning typed[j:] into raw[i:], or None."""
        if j == len(self.typed):
            # The rest of the raw words are deleted: never half a number unit.
            return None if self._inside(i) else 0
        key = (i, j)
        if key in self.memo:
            return self.memo[key]
        result = None
        for k in range(i, len(self.raw)):
            # Deleting raw[i:k] may not cut a number unit at either end.
            if k > i and (self._inside(i) or self._inside(k)):
                continue
            for used, typed_used in self._forms(j, k):
                rest = self.best(k + used, j + typed_used)
                if rest is not None:
                    kept = rest + sum(1 for x in range(k, k + used) if not self.free[x])
                    if result is None or kept > result:
                        result = kept
        self.memo[key] = result
        return result

    def matchable(self, j: int) -> bool:
        return any(True for k in range(len(self.raw)) for _ in self._forms(j, k))


def check(raw: str, cleaned: str) -> Verdict:
    """May `cleaned` be typed for an owner who said `raw`? Fixed reason strings."""
    if not isinstance(raw, str) or not isinstance(cleaned, str):
        return Verdict(False, "not_text")
    if cleaned == raw:
        return Verdict(True)  # what the raw fallback types anyway
    if any(c in _BREAKS for c in cleaned):
        return Verdict(False, "newline")
    if any(unicodedata.category(c)[0] == "C" or (c.isspace() and c != " ") for c in cleaned):
        return Verdict(False, "control_char")
    if len(cleaned) > len(raw) + 16 + len(raw) // 10:
        return Verdict(False, "too_long")
    raw_words, typed_words = raw.split(), cleaned.split(" ")
    typed_words = [w for w in typed_words if w]
    if len(raw_words) > _MAX_WORDS or len(typed_words) > _MAX_WORDS:
        return Verdict(False, "too_long")
    counted = sum(1 for free in _uncounted(raw_words) if not free)
    if not typed_words:
        return Verdict(False, "empty") if counted else Verdict(True)
    aligner = _Aligner(raw_words, typed_words)
    kept = aligner.best(0, 0)
    if kept is None:
        for j, (core, _tail) in enumerate(aligner.typed):
            if not aligner.matchable(j):
                odd = (any(not (c.isalnum() or c in "'\u2019-") and c not in raw for c in core)
                       or (_tail not in _END and any(c not in raw for c in _tail)))
                return Verdict(False, "new_symbol" if odd else "added_word")
        said = Counter(aligner.keys)
        if Counter(_key(c) for c, _ in aligner.typed) - said:
            return Verdict(False, "added_word")
        return Verdict(False, "reordered")
    if counted and kept < counted * (1 - MAX_DELETION):
        return Verdict(False, "over_deletion")
    return Verdict(True)


def choose(raw: str, rules_text: str, model_text: str | None) -> tuple[str, str]:
    """The text to type and which pass produced it: `model`, `rules` or `raw`."""
    if model_text is not None and check(raw, model_text).ok:
        return model_text, "model"
    if check(raw, rules_text).ok:
        return rules_text, "rules"
    return raw, "raw"
