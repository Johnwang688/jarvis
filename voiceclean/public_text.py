"""Local OpenAssistant oasst2 prompter messages (Apache-2.0); no downloader."""
import gzip
import json
from pathlib import Path
import re

from .prose import is_prose, prose_shape
from .storage import file_hash, safe_input, source_id


def sentence_groups(text: str):
    group = []
    size = 0
    for sentence in re.split(r'(?<=[.!?])\s+', text.strip()):
        count = len(sentence.split())
        if count > 60:
            if group:
                yield ' '.join(group)
                group, size = [], 0
            continue  # do not fragment a single overlong sentence
        if size + count > 60:
            yield ' '.join(group)
            group, size = [], 0
        group.append(sentence)
        size += count
    if group:
        yield ' '.join(group)


def read(path: str | Path):
    path = safe_input(path)
    if not (path.name.endswith('.jsonl') or path.name.endswith('.jsonl.gz')):
        raise ValueError('oasst2 input must be .jsonl or .jsonl.gz')
    digest = file_hash(path)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get('role') != 'prompter' or row.get('lang') != 'en':
                continue
            text = row.get('text', '')
            if not prose_shape(text):
                continue
            for n, group in enumerate(sentence_groups(text)):
                if is_prose(group):
                    yield {'text': group, 'origin': 'public', 'source': 'oasst2',
                           'source_id': source_id(group), 'message_id': row.get('message_id'),
                           'sentence_group': n, 'provenance': {'dataset': 'OpenAssistant/oasst2',
                           'licence': 'Apache-2.0', 'file_sha256': digest}}
