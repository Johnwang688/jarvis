"""Offline mirror of the guard rule in the VA-3 plan, never used at runtime.

VA-3a's runtime guard is the authority. Call that guard here once it exists,
provided it can be imported without configuration or credential side effects.
"""
from collections import Counter
import unicodedata

from .normalize import tokens


def _subsequence(messy: list[str], clean: list[str]) -> bool:
    remaining = iter(messy)
    return all(any(word == candidate for candidate in remaining) for word in clean)


def pair_check(messy: str, clean: str) -> tuple[bool, str]:
    if any(c in clean for c in '\n\r\u0085\u2028\u2029'):
        return False, 'newline'
    if any(unicodedata.category(c).startswith('C') for c in clean):
        return False, 'control_char'
    raw, target = tokens(messy, messy=True), tokens(clean)
    if not target and tokens(messy):
        return False, 'empty'
    if not _subsequence(raw, target):
        if Counter(target) - Counter(raw):
            return False, 'added_word'
        return False, 'reordered'
    if len(target) * 5 < len(raw) * 2:
        return False, 'over_deletion'
    return True, ''
