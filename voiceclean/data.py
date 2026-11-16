"""Owner-run offline CLI: python -m voiceclean.data STEP. Counts-only output."""
from __future__ import annotations

import argparse
from itertools import islice
import json
import random
import re

from . import build, corrupt, owner_text, public_text, roundtrip, teachers, templates
from .pair_check import pair_check
from .prose import tidy_target
from .storage import data_dir, read_jsonl, write_json


def nonnegative(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError('must be nonnegative')
    return n


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', default=argparse.SUPPRESS)
    steps = p.add_subparsers(dest='step', required=True)
    for name in ('owner-text', 'public', 'synth', 'roundtrip', 'teach', 'build'):
        sub = steps.add_parser(name)
        sub.add_argument('--data-dir', default=argparse.SUPPRESS)
        if name == 'public':
            sub.add_argument('--oasst2', required=True)
        if name in ('synth', 'roundtrip', 'teach'):
            sub.add_argument('--seed', type=int, default=0, required=name == 'synth')
        if name == 'synth':
            sub.add_argument('--per-source', type=nonnegative, default=5)
            sub.add_argument('--template-count', type=nonnegative, default=200)
        if name == 'roundtrip':
            sub.add_argument('--limit', type=nonnegative, required=True)
            sub.add_argument('--threads', type=int, default=2)
            sub.add_argument('--kokoro-model')
            sub.add_argument('--kokoro-voices')
            sub.add_argument('--parakeet-dir')
            sub.add_argument('--voices', nargs='+', default=['af_heart', 'am_adam', 'bf_emma'])
        if name == 'teach':
            sub.add_argument('--model', required=True)
            sub.add_argument('--task', choices=['messify', 'clean'], required=True)
            sub.add_argument('--limit', type=nonnegative, required=True)
            sub.add_argument('--max-usd', type=float, default=.02)
    return p


def sources(directory, seed, template_count):
    for name in ('owner_text.jsonl', 'public_text.jsonl'):
        path = directory / name
        if path.exists():
            yield from read_jsonl(path)
    yield from templates.generate(seed, template_count, directory / 'names.txt')


def synthetic(directory, seed, per_source=5, template_count=200, counts=None):
    rng = random.Random(seed)
    for source in sources(directory, seed, template_count):
        if counts is not None:
            counts['sources'] += 1
        # With asymmetric spoken-punctuation normalization, a literal word
        # such as "comma" in the target cannot pass even the identity pair.
        if not pair_check(source['text'], tidy_target(source['text']))[0]:
            if counts is not None:
                counts['skipped_sources'] += 1
            continue
        for _ in range(per_source):
            pair_seed = rng.getrandbits(64)
            pair = corrupt.corrupt(source['text'], random.Random(pair_seed),
                                   {'protected_terms': source.get('protected_terms', [])})
            yield {**source, 'messy': pair.messy, 'target': pair.target, 'ops': pair.ops,
                   'source_seed': seed, 'seed': pair_seed, 'kind': 'synthetic', 'guard_compatible': True}


def pairs(directory, *, synthetic_only=False):
    for path in build.pair_files(directory):
        for row in read_jsonl(path):
            if 'messy' in row and 'target' in row:
                if not synthetic_only or row.get('kind') == 'synthetic':
                    yield row


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.step == 'teach':
            teachers.validate_model(args.model)
        directory = data_dir(getattr(args, 'data_dir', None))
        if args.step == 'owner-text':
            counts = owner_text.write(directory)
        elif args.step == 'public':
            rows = list(public_text.read(args.oasst2))
            write_json(directory / 'public_text.jsonl', rows, lines=True)
            counts = {'public': len(rows)}
        elif args.step == 'synth':
            counts = {'sources': 0, 'skipped_sources': 0}
            rows = list(synthetic(directory, args.seed, args.per_source, args.template_count, counts))
            write_json(directory / f'pairs-synth-{args.seed}.jsonl', rows, lines=True)
            counts['pairs'] = len(rows)
        elif args.step == 'roundtrip':
            inputs = list(islice(pairs(directory, synthetic_only=True), args.limit))
            if inputs:
                tts = roundtrip.kokoro_backend(args.kokoro_model, args.kokoro_voices)
                stt = roundtrip.parakeet_backend(args.parakeet_dir, args.threads)
                rows = list(roundtrip.run(inputs, tts, stt, args.voices, random.Random(args.seed)))
            else:
                rows = []
            write_json(directory / f'pairs-roundtrip-{args.seed}.jsonl', rows, lines=True)
            counts = {'pairs': len(rows), 'guard_compatible': sum(r['guard_compatible'] for r in rows)}
        elif args.step == 'teach':
            inputs = (row for row in pairs(directory) if row.get('kind') != 'teacher'
                      and (args.task == 'clean' or row.get('kind') == 'synthetic'))
            rows, counts = teachers.run(inputs, args.model, args.task,
                limit=args.limit, max_usd=args.max_usd, rng=random.Random(args.seed))
            slug = re.sub(r'[^a-zA-Z0-9_-]', '-', args.model)
            write_json(directory / f'pairs-teach-{slug}-{args.task}-{args.seed}.jsonl', rows, lines=True)
        else:
            card = build.build(directory)
            counts = {'total': card['total'], **card['counts']}
        print(json.dumps(counts, sort_keys=True))
        return 0
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        # These errors have deliberately text-free messages in the pipeline.
        # OSErrors/ImportErrors may identify a missing input, but never row text.
        print(f'voiceclean: {exc}', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
