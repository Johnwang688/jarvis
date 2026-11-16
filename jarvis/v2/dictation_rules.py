"""Conservative VA-3 cleanup. Pure stdlib; the guard checks every edit.

Terminal cleanup only removes hesitation sounds and collapses whitespace
(VA3-D1). Elsewhere: spoken punctuation first (so "grace period period" keeps
its word), then immediate repeats, capitals and a final full stop. If an edit
cannot pass the guard, the original transcript is returned unchanged.
"""
from __future__ import annotations

import re

from jarvis.v2 import dictation_guard as guard

_WORD = r"[^\W_]+(?:['’][^\W_]+)?"
_FILLERS = re.compile(r"(?<![\w'’])(?:" + "|".join(sorted(guard.FILLERS)) + r")(?![\w'’])", re.I)
_REPEAT = re.compile(r"(?<![\w'’])(" + _WORD + r"(?:\s+" + _WORD + r")?)\s+\1(?![\w'’])", re.I)
_DOUBLES = frozenset({'that', 'had'})
_PUNCT = {'comma': ',', 'question mark': '?', 'exclamation point': '!',
          'exclamation mark': '!', 'full stop': '.', 'period': '.'}
_SPOKEN = re.compile(r"\b(?:question mark|exclamation point|exclamation mark|full stop|comma|period(?=\W*$))\b", re.I)


def _dedupe(match: re.Match) -> str:
    phrase = match[1]
    if len(phrase.split()) == 1 and phrase.casefold() in _DOUBLES:
        return match[0]
    return phrase


def clean(raw: str, app: str = 'other') -> str:
    """Return a guard-checked cleanup, or the original input on rejection."""
    text = ' '.join(_FILLERS.sub('', raw).split())
    if app != 'terminal':
        # Spoken punctuation before repeats: "a grace period period" must lose
        # the spoken one, not collapse into a single word and then a dot.
        text = _SPOKEN.sub(lambda m: _PUNCT[m[0].casefold()], text)
        while True:
            shorter = _REPEAT.sub(_dedupe, text)
            if shorter == text:
                break
            text = shorter
        text = re.sub(r'\s+([,?.!])', r'\1', text)
        # The guard allows one of , . ? ! ?! ... after a word: a run the owner
        # spoke as two marks ("exclamation mark full stop") keeps the first.
        text = re.sub(r'[,?.!]{2,}', lambda m: m[0] if m[0] in ('?!', '...') else m[0][0], text)
        text = re.sub(r"\bi(?:['’](?:m|ve|ll|d))?\b", lambda m: 'I' + m[0][1:], text)
        text = re.sub(r"(^|[.!?])(\s*['\"(“‘]*)([^\W\d_])",
                      lambda m: m[1] + m[2] + m[3].upper(), text)
        if text and text[-1].isalnum() and len(re.findall(_WORD, text)) >= 3:
            text += '.'
    return text if guard.check(raw, text).ok else raw
