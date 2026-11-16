"""Offline VA-3 evaluation. Reports contain aggregate metrics, never row text."""
from __future__ import annotations

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import time

from jarvis.v2 import dictation_guard as guard, dictation_rules as rules
from .storage import data_dir, read_jsonl, safe_input, write_json

SUITES = ('all', 'self_correction', 'numbers', 'questions', 'template', 'owner',
          'public', 'roundtrip', 'gold')


def distance(reference: list[str], hypothesis: list[str]) -> int:
    """Levenshtein edit count using one row of working memory."""
    previous = list(range(len(hypothesis) + 1))
    for i, expected in enumerate(reference, 1):
        current = [i]
        for j, actual in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (expected != actual)))
        previous = current
    return previous[-1]


def percentile(values, fraction):
    """Nearest-rank percentile; an empty suite has zero latency."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else 0.0


def rows(directory: Path, split: str):
    path = directory / 'dataset' / f'{split}.jsonl'
    if path.exists():
        for row in read_jsonl(path):
            yield row, False
    for path in sorted((directory / 'gold').glob('*.json')):
        with safe_input(path).open(encoding='utf-8') as fh:
            yield json.load(fh), True


def memberships(row, gold):
    yield 'all'
    if 'self_correction' in row.get('ops', []):
        yield 'self_correction'
    if 'number_words' in row.get('ops', []):
        yield 'numbers'
    if row['target'].rstrip().endswith('?'):
        yield 'questions'
    if row.get('origin') in ('template', 'owner', 'public'):
        yield row['origin']
    if row.get('kind') == 'roundtrip':
        yield 'roundtrip'
    if gold:
        yield 'gold'


def evaluate(directory, *, split='dev', system='b1', limit=None):
    """Evaluate the selected split followed by every gold row (limit is global)."""
    if split not in ('dev', 'test') or (limit is not None and limit < 0):
        raise ValueError('invalid evaluation selection')
    directory = data_dir(directory)
    model = None
    if system not in ('b0', 'b1'):
        module, sep, name = system.partition(':')
        if not sep or not module or not name:
            raise ValueError('system must be b0, b1 or MODULE:FUNC')
        # Optional baseline adapters may print their inputs or loader output.
        # The CLI's only output is the aggregate table, even for those adapters.
        with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
            model = getattr(importlib.import_module(module), name)
        if not callable(model):
            raise ValueError('evaluation system must be callable')
    buckets = {name: [] for name in SUITES}
    for row, gold in islice(rows(directory, split), limit):
        raw, target = row.get('raw', row.get('messy')), row.get('target')
        if not isinstance(raw, str) or not isinstance(target, str):
            raise ValueError('evaluation row requires transcript and target strings')
        started = time.perf_counter_ns()
        rejected = ''
        try:
            if system == 'b0':
                typed = raw
            else:
                fallback = rules.clean(raw)
                candidate = None
                if model is not None:
                    with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
                        candidate = model(raw)
                    verdict = guard.check(raw, candidate)
                    rejected = verdict.reason if not verdict.ok else ''
                typed = guard.choose(raw, fallback, candidate)[0]
        except Exception:
            raise RuntimeError('evaluation system failed') from None
        latency = (time.perf_counter_ns() - started) / 1_000_000
        content_target, content_typed = guard.tokens(target), guard.tokens(typed)
        format_target, format_typed = target.split(), typed.split()
        sample = (distance(content_target, content_typed), len(content_target),
                  distance(format_target, format_typed), len(format_target),
                  typed == target, rejected, latency)
        for suite in memberships(row, gold):
            buckets[suite].append(sample)
    suites = {}
    for suite, samples in buckets.items():
        n = len(samples)
        reasons = Counter(s[5] for s in samples if s[5])
        suites[suite] = {
            'n': n,
            'wer_words': sum(s[0] for s in samples) / max(1, sum(s[1] for s in samples)),
            'wer_format': sum(s[2] for s in samples) / max(1, sum(s[3] for s in samples)),
            'exact_match_rate': sum(s[4] for s in samples) / n if n else 0.0,
            'guard_rejection_rate': sum(reasons.values()) / n if n else 0.0,
            'guard_rejection_reasons': dict(sorted(reasons.items())),
            'latency_p50_ms': percentile([s[6] for s in samples], .5),
            'latency_p95_ms': percentile([s[6] for s in samples], .95),
        }
    now = datetime.now(timezone.utc)
    report = {'system': system, 'split': split, 'created': now.isoformat(), 'suites': suites}
    slug = re.sub(r'[^a-zA-Z0-9_-]', '-', system)
    write_json(directory / 'eval' / f'{now:%Y%m%dT%H%M%S%fZ}-{slug}.json', report)
    return report


def print_table(report):
    print('suite n wer_words wer_format exact reject p50_ms p95_ms')
    for suite, metrics in report['suites'].items():
        print(f"{suite} {metrics['n']} " + ' '.join(f'{metrics[key]:.4f}' for key in
              ('wer_words', 'wer_format', 'exact_match_rate', 'guard_rejection_rate',
               'latency_p50_ms', 'latency_p95_ms')))
        for reason, count in metrics['guard_rejection_reasons'].items():
            print(f'{suite} {reason} {count}')
