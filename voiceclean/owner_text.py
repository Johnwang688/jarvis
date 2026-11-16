"""Owner-only extractor ported from the VA-3 prototype, plus the prose filter.

Source selection, precedence, whole-message drop rules and dedupe match the
prototype. Nothing is read at import time; extraction is explicitly owner-run.
Only the shape-based redactor entry points are used (no credential file reads).
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re

from jarvis.v2 import redact

from .prose import is_prose
from .storage import data_dir, safe_input, source_id, write_json

REMINDER = re.compile(r'<system-reminder>.*?</system-reminder>', re.S)
HARNESS_PREFIXES = ('<', '[Request interrupted', 'Caveat:', 'This session is being continued',
                    '[owner steering]', 'Base directory for this skill')
MAX_CHARS = 1200


def agentish(text: str) -> bool:
    if '```' in text or '<pasted_content' in text:
        return True
    lines = text.splitlines()
    if any(line.lstrip().startswith('#') for line in lines):
        return True
    return sum(bool(re.match(r'\s*(?:[-*]|\d+[.)])\s', line)) for line in lines) >= 3


def clean(text) -> str | None:
    if not isinstance(text, str):
        return None
    text = REMINDER.sub('', text).strip()
    if len(text) < 3 or len(text) > MAX_CHARS or text.startswith(HARNESS_PREFIXES):
        return None
    if agentish(text):
        return None
    try:
        scrubbed = redact.labelled_values(redact.shapes(text, mode=redact.DISPLAY))
    except Exception:
        return None
    if scrubbed != text or not is_prose(text):
        return None
    return text


def jsonl(path: Path):
    with safe_input(path).open(encoding='utf-8', errors='replace') as fh:
        for line in fh:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    yield row
            except (ValueError, TypeError):
                continue


def from_claude(home: Path):
    for f in sorted((home / '.claude/projects').rglob('*.jsonl')):
        if 'subagents' in f.parts:
            continue
        for e in jsonl(f):
            if (e.get('type') != 'user' or e.get('isSidechain') or e.get('isMeta')
                    or e.get('entrypoint') not in ('claude-desktop', 'cli')
                    or e.get('userType') != 'external'):
                continue
            content = (e.get('message') or {}).get('content')
            if isinstance(content, list):
                if any(isinstance(b, dict) and b.get('type') != 'text' for b in content):
                    continue
                content = '\n'.join(b.get('text', '') for b in content if isinstance(b, dict))
            yield content, {'source': 'claude', 'session': f.stem}


def from_codex(home: Path):
    for f in sorted((home / '.codex/sessions').rglob('*.jsonl')):
        it = jsonl(f)
        first = next(it, None)
        if not first or (first.get('payload') or {}).get('originator') != 'codex-tui':
            continue
        for e in it:
            p = e.get('payload') or {}
            if e.get('type') == 'event_msg' and p.get('type') == 'user_message':
                yield p.get('message'), {'source': 'codex', 'session': f.stem}


def from_v1(home: Path):
    for f in sorted((home / '.local/share/jarvis/sessions').glob('*/log.jsonl')):
        for e in jsonl(f):
            if e.get('role') == 'you':
                yield e.get('text'), {'source': 'v1', 'session': f.parent.name}


def from_v2(home: Path):
    for f in sorted((home / '.local/share/jarvis/v2/threads').glob('*/log.jsonl')):
        for e in jsonl(f):
            d = e.get('data') or {}
            if e.get('kind') == 'user' and d.get('origin') == 'owner':
                yield d.get('typed'), {'source': 'v2', 'session': f.parent.name,
                                       'spoken': bool(d.get('spoken')), 'via': d.get('via')}


def extract():
    home = Path.home()
    seen, rows = set(), []
    kept, dropped = Counter(), Counter()
    for gen in (from_v2, from_v1, from_claude, from_codex):
        for raw, meta in gen(home):
            text = clean(raw)
            if text is None:
                dropped[meta['source']] += 1
                continue
            key = re.sub(r'\s+', ' ', text.lower())
            if key in seen:
                dropped[meta['source'] + ':dup'] += 1
                continue
            seen.add(key)
            kept[meta['source']] += 1
            rows.append({'text': text, **meta, 'spoken': meta.get('spoken', False),
                         'origin': 'owner', 'source_id': source_id(text),
                         'provenance': {'licence': 'private-owner'}})
    return rows, {'kept': dict(kept), 'dropped': dict(dropped), 'total': len(rows)}


def write(out_dir=None):
    directory = data_dir(out_dir)
    rows, counts = extract()
    write_json(directory / 'owner_text.jsonl', rows, lines=True)
    return counts
