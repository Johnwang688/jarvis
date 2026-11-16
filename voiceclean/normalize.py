"""Fixed lexical equivalences shared by the offline pair checker and generator."""
from __future__ import annotations

import re

CONTRACTIONS = {
    "don't": 'do not', "doesn't": 'does not', "didn't": 'did not',
    "can't": 'can not', "cannot": 'can not', "couldn't": 'could not',
    "won't": 'will not', "wouldn't": 'would not', "shouldn't": 'should not',
    "isn't": 'is not', "aren't": 'are not', "wasn't": 'was not',
    "weren't": 'were not', "hasn't": 'has not', "haven't": 'have not',
    "hadn't": 'had not', "mustn't": 'must not', "needn't": 'need not',
    "i'm": 'i am', "i've": 'i have', "i'll": 'i will', "i'd": 'i would',
    "you're": 'you are', "you've": 'you have', "you'll": 'you will',
    "you'd": 'you would', "he's": 'he is', "he'll": 'he will',
    "she's": 'she is', "she'll": 'she will', "it's": 'it is',
    "it'll": 'it will', "we're": 'we are', "we've": 'we have',
    "we'll": 'we will', "we'd": 'we would', "they're": 'they are',
    "they've": 'they have', "they'll": 'they will', "they'd": 'they would',
    "that's": 'that is', "there's": 'there is', "here's": 'here is',
    "what's": 'what is', "who's": 'who is', "let's": 'let us',
}
_CONTRACTION = re.compile(r"\b(?:" + '|'.join(map(re.escape, CONTRACTIONS)) + r")\b")
SMALL = ('zero one two three four five six seven eight nine ten eleven twelve '
         'thirteen fourteen fifteen sixteen seventeen eighteen nineteen').split()
TENS = ('twenty thirty forty fifty sixty seventy eighty ninety').split()
VALUES = {w: n for n, w in enumerate(SMALL)} | {w: 20 + 10 * n for n, w in enumerate(TENS)}
SPOKEN = re.compile(r'\b(?:full stop|question mark|exclamation point|exclamation mark|'
                    r'comma|period|colon|semicolon)\b')
_WORDS = re.compile(r"[^\W_]+(?:'[^\W_]+)?|%", re.UNICODE)


def expand_contractions(text: str) -> str:
    return _CONTRACTION.sub(lambda m: CONTRACTIONS[m[0]], text.casefold().replace('’', "'"))


def number_words(n: int) -> str:
    if not 0 <= n <= 999999:
        raise ValueError('number out of range')
    if n < 20:
        return SMALL[n]
    if n < 100:
        return TENS[n // 10 - 2] + (' ' + SMALL[n % 10] if n % 10 else '')
    if n < 1000:
        return SMALL[n // 100] + ' hundred' + (' ' + number_words(n % 100) if n % 100 else '')
    return number_words(n // 1000) + ' thousand' + (' ' + number_words(n % 1000) if n % 1000 else '')


def _under_hundred(words, pos):
    if pos >= len(words) or words[pos] not in VALUES:
        return None
    n = VALUES[words[pos]]
    end = pos + 1
    if n >= 20 and end < len(words) and words[end] in SMALL[1:10]:
        n += VALUES[words[end]]
        end += 1
    return n, end


def _under_thousand(words, pos):
    first = _under_hundred(words, pos)
    if first is None:
        return None
    n, end = first
    if 1 <= n <= 9 and end < len(words) and words[end] == 'hundred':
        n *= 100
        end += 1
        start = end + (end < len(words) and words[end] == 'and')
        tail = _under_hundred(words, start)
        if tail is not None and tail[0] > 0:
            n += tail[0]
            end = tail[1]
    return n, end


def tokens(text: str, *, messy: bool = False) -> list[str]:
    text = expand_contractions(text)
    if messy:
        text = SPOKEN.sub(' ', text)
    # Treat grouped digits consistently; leave decimal/time parts separate.
    text = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', text)
    words = _WORDS.findall(text)
    out = []
    pos = 0
    while pos < len(words):
        parsed = _under_thousand(words, pos)
        if parsed is None:
            word = words[pos]
            out.append('percent' if word == '%' else re.sub(r'^0+(?=\d)', '', word)
                       if word.isdecimal() else word)
            pos += 1
            continue
        n, end = parsed
        if n > 0 and end < len(words) and words[end] == 'thousand':
            n *= 1000
            end += 1
            start = end + (end < len(words) and words[end] == 'and')
            tail = _under_thousand(words, start)
            if tail is not None and tail[0] > 0:
                n += tail[0]
                end = tail[1]
        out.append(str(n))
        pos = end
    return out
