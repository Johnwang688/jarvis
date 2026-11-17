"""Lexical equivalences for the offline generator.

The normalisation itself lives in the runtime guard, `jarvis.v2.dictation_guard`,
so the data tool and the dictation path judge words identically. This module
re-exports it and adds `number_words`, which only the generator needs.
"""
from __future__ import annotations

from jarvis.v2.dictation_guard import (CONTRACTIONS, SMALL, SPOKEN_PUNCTUATION as SPOKEN, TENS,
                                       expand_contractions)
from jarvis.v2.dictation_guard import tokens as _guard_tokens

VALUES = {w: n for n, w in enumerate(SMALL)} | {w: 20 + 10 * n for n, w in enumerate(TENS)}


def tokens(text: str, *, messy: bool = False) -> list[str]:
    return _guard_tokens(text, raw=messy)


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
