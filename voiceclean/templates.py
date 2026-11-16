"""Seeded local sentences with robotics terms, dates, times and quantities."""
from pathlib import Path
import random

from .storage import file_hash, safe_input, source_id

WEEKDAYS = 'Monday Tuesday Wednesday Thursday Friday Saturday Sunday'.split()
MONTHS = ('January February March April May June July August September October November December').split()


def protected_terms() -> list[str]:
    return Path(__file__).with_name('terms').joinpath('vex.txt').read_text(encoding='utf-8').splitlines()


def generate(seed: int, count: int = 200, names_path: Path | None = None):
    rng = random.Random(seed)
    terms = protected_terms()
    names = []
    names_hash = None
    if names_path is not None and names_path.exists():
        names_path = safe_input(names_path)
        names = [n.strip() for n in names_path.read_text(encoding='utf-8').splitlines() if n.strip()]
        names_hash = file_hash(names_path)
    for _ in range(count):
        term = rng.choice(terms)
        day, month = rng.choice(WEEKDAYS), rng.choice(MONTHS)
        number, time = rng.randrange(1, 500), f'{rng.randrange(1, 13)}:{rng.choice(("00", "15", "30", "45"))}'
        team = f'{rng.randrange(1000, 10000)}{rng.choice("ABCDEFGH")}'
        sentences = [
            f'Please check the {term} before practice on {day}.',
            f'Team {team} will test the {term} at {time} on {day}.',
            f'We need {number} parts for the {term} in {month}.',
            f'I will tune the {term} on {day} at {time}.',
            f'Can we finish {number} runs before the event in {month}?',
        ]
        if names:
            sentences.append(f'Please ask {rng.choice(names)} to check the {term} on {day}.')
        text = rng.choice(sentences)
        uses_names = bool(names) and text == sentences[-1]
        yield {'text': text, 'origin': 'template', 'source': 'protected-templates',
               'source_id': source_id(text), 'seed': seed, 'owner_derived': uses_names,
               'protected_terms': [term] + (names if uses_names else []),
               'provenance': {'licence': 'local-template', **(
                   {'names_sha256': names_hash, 'private': True} if uses_names else {})}}
