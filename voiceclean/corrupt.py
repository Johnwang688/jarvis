"""Deterministic, seeded dictation corruption with a deletion-only invariant."""
from dataclasses import dataclass
import random
import re

from .normalize import SMALL, TENS, expand_contractions, number_words, tokens
from .pair_check import pair_check
from .prose import tidy_target
from .templates import MONTHS, WEEKDAYS, protected_terms

# Per-word rates at full intensity. Each pair draws an intensity in
# [intensity_min, 1], so the set spans light and heavy dictation instead of
# every sentence carrying a filler every few words (measured 2026-11-16: the
# first rates gave a median of 1.54 messy tokens per target token).
DEFAULT_PROFILE = {'fillers': .10, 'repetition': .05, 'false_start': .2,
                   'self_correction': .35, 'spoken_punctuation': .5, 'number_words': .65,
                   'partial_punctuation': .2, 'intensity_min': .2}
_SCALED = ('fillers', 'repetition', 'false_start', 'self_correction')
FILLERS = ('um', 'uh', 'like', 'you know', 'I mean', 'so')
CORRECTIONS = ('no wait', 'sorry', 'I mean', 'actually')
_ONES = '(?:' + '|'.join(SMALL[1:10]) + ')'
_LOW = '(?:(?:' + '|'.join(TENS) + ')(?:[ -]+' + _ONES + ')?|(?:' + '|'.join(SMALL) + '))'
_HUNDREDS = '(?:' + _ONES + '[ -]+hundred(?:[ -]+(?:and[ -]+)?' + _LOW + ')?|' + _LOW + ')'
_NUMBER = _HUNDREDS + '(?:[ -]+thousand(?:[ -]+(?:and[ -]+)?' + _HUNDREDS + ')?)?'


@dataclass(frozen=True)
class Pair:
    messy: str
    target: str
    ops: list[str]


def corrupt(clean: str, rng: random.Random, profile=None) -> Pair:
    profile = DEFAULT_PROFILE | (profile or {})
    intensity = rng.uniform(profile['intensity_min'], 1.0)
    profile = profile | {k: profile[k] * intensity for k in _SCALED}
    target = tidy_target(clean)
    terms = protected_terms() + list(profile.get('protected_terms', []))
    pattern = '|'.join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    units = re.findall(r'(?<!\w)(?:' + pattern + r')(?!\w)|\b\d{1,2}:\d{2}\b|\b' + _NUMBER + r'\b|'
                       r"\b\w+(?:['’]\w+)?\b|[^\w\s]", target, re.I)
    ops = []
    modified = []
    for unit in units:
        word = unit.casefold()
        if rng.random() < profile['self_correction']:
            choices = None
            if word in [d.casefold() for d in WEEKDAYS]:
                choices = WEEKDAYS
            elif word in [m.casefold() for m in MONTHS]:
                choices = MONTHS
            elif re.fullmatch(r'\d{1,2}:\d{2}', unit):
                choices = [f'{n}:30' for n in range(1, 13)]
            else:
                normalized = tokens(unit)
                if len(normalized) == 1 and normalized[0].isdecimal() and int(normalized[0]) <= 999999:
                    choices = [str((int(normalized[0]) + n) % 1000000) for n in (1, 2, 3)]
            if choices:
                modified.extend([rng.choice([c for c in choices if c.casefold() != word]),
                                 rng.choice(CORRECTIONS)])
                ops.append('self_correction')
        modified.append(unit)
        if unit[0].isalnum():
            if rng.random() < profile['repetition']:
                modified.append(unit)
                ops.append('repetition')
            if rng.random() < profile['fillers']:
                modified.append(rng.choice(FILLERS))
                ops.append('fillers')
    if rng.random() < profile['false_start']:
        prefix = []
        word_count = 0
        for unit in units:
            if not unit[0].isalnum() or word_count + len(unit.split()) > 3:
                break
            prefix.append(unit)
            word_count += len(unit.split())
        modified = prefix[:rng.randrange(1, len(prefix) + 1)] + modified if prefix else modified
        if prefix:
            ops.append('false_start')
    spoken = {',': 'comma', '.': 'period', '?': 'question mark', '!': 'exclamation point',
              ':': 'colon', ';': 'semicolon'}
    for i, unit in enumerate(modified):
        if unit in spoken and rng.random() < profile['spoken_punctuation']:
            modified[i] = spoken[unit]
            ops.append('spoken_punctuation')
        elif unit.isdigit() and int(unit) <= 999999 and rng.random() < profile['number_words']:
            modified[i] = number_words(int(unit))
            ops.append('number_words')
        elif re.fullmatch(r'\d{1,2}:\d{2}', unit) and rng.random() < profile['number_words']:
            modified[i] = ' '.join(number_words(int(part)) for part in unit.split(':'))
            ops.append('number_words')
    text = expand_contractions(' '.join(modified))
    if rng.random() >= profile['partial_punctuation']:
        text = re.sub(r'[^\w\s%]', ' ', text)
    text = ' '.join(text.split())
    if pair_check(text, target)[0]:
        return Pair(text, target, ops)
    # Number parsing is intentionally narrow; ambiguous adjacent numbers and
    # excessive disfluencies fall back to the uncorrupted source, never a bad pair.
    fallback = target.lower()
    if not pair_check(fallback, target)[0]:
        raise ValueError('source cannot produce a guard-compatible target')
    return Pair(fallback, target, [])
