"""Merge private pair files, dedupe, and split by stable source sentence id."""
from collections import Counter
import hashlib
from pathlib import Path

from .normalize import tokens
from .pair_check import pair_check
from .storage import data_dir, file_hash, read_jsonl, source_id, write_json

PRIVATE_NOTE = 'Owner-derived rows and anything trained on them must never leave this machine.'


def pair_files(directory: Path):
    # All root-level pair files, including owner-named extensions; dataset/ is
    # deliberately excluded so rebuilds never consume their own prior outputs.
    return sorted(p for p in directory.glob('*.jsonl')
                  if p.name not in ('owner_text.jsonl', 'public_text.jsonl'))


def split_for(identifier: str) -> str:
    bucket = int(hashlib.sha256(identifier.encode()).hexdigest(), 16) % 100
    return 'train' if bucket < 90 else 'dev' if bucket < 95 else 'test'


def build(directory=None):
    directory = data_dir(directory)
    inputs = pair_files(directory)
    hashes = {path.name: file_hash(path) for path in inputs}
    unique = {}
    origins, kinds, ops, splits = Counter(), Counter(), Counter(), Counter()
    licences, seeds, models = set(), set(), set()
    compatible, duplicates = 0, 0
    for path in inputs:
        for row in read_jsonl(path):
            if 'messy' not in row or 'target' not in row:
                continue
            if row.get('origin') not in ('owner', 'public', 'template'):
                raise ValueError('pair row has missing or invalid origin')
            row = dict(row)
            row['source_id'] = row.get('source_id') or source_id(row.get('text', row['target']))
            if 'seed' in row:
                seeds.add(row['seed'])
            if 'source_seed' in row:
                seeds.add(row['source_seed'])
            key = (tuple(tokens(row['messy'], messy=True)), tuple(tokens(row['target'])))
            if key in unique:
                duplicates += 1
                previous = unique[key]
                # Privacy is monotonic even when public and owner text coincide.
                if row['origin'] == 'owner' or row.get('owner_derived'):
                    previous['owner_derived'] = True
                if row['origin'] == 'owner':
                    previous['origin'] = 'owner'
                previous.setdefault('duplicate_provenance', []).append(row.get('provenance', {}))
                continue
            unique[key] = row
    datasets = {name: [] for name in ('train', 'dev', 'test')}

    def record_provenance(provenance):
        for key, value in provenance.items():
            if key == 'licence' or key.endswith('_licence'):
                licences.add(value)
            elif key == 'teachers':
                for teacher in value:
                    models.add(teacher['model'])
                    licences.add(teacher['licence'])
            elif isinstance(value, dict):
                record_provenance(value)

    for row in unique.values():
        split = split_for(row['source_id'])
        row['guard_compatible'] = pair_check(row['messy'], row['target'])[0]
        datasets[split].append(row)
        origins[row['origin']] += 1
        kinds[row.get('kind', 'synthetic')] += 1
        ops.update(row.get('ops', []))
        splits[split] += 1
        compatible += row['guard_compatible']
        record_provenance(row.get('provenance', {}))
        for provenance in row.get('duplicate_provenance', []):
            record_provenance(provenance)
    card = {'total': len(unique), 'duplicates_removed': duplicates,
            'counts': {'origin': dict(origins), 'kind': dict(kinds), 'op': dict(ops),
                       'split': {s: splits[s] for s in datasets}},
            'guard_compatible': {'count': compatible,
                                 'share': compatible / len(unique) if unique else 0},
            'licences': sorted(licences), 'input_sha256': hashes,
            'seeds': sorted(seeds), 'teacher_models': sorted(models), 'private_note': PRIVATE_NOTE,
            'owner_derived': sum(r['origin'] == 'owner' or bool(r.get('owner_derived')) for r in unique.values()),
            'attribution': {'Kokoro': 'Apache-2.0', 'NVIDIA Parakeet TDT 0.6B v3': 'CC-BY-4.0'}}
    for split, rows in datasets.items():
        write_json(directory / 'dataset' / f'{split}.jsonl', rows, lines=True)
    write_json(directory / 'dataset' / 'data_card.json', card)
    return card
