"""Allowlisted open-weight teachers; owner-derived text is never sent."""
from collections import Counter
import json
import math
import os
import random
import re
import urllib.error
import urllib.request

from .pair_check import pair_check
from .prose import tidy_target
from .storage import source_id

ALLOWLIST = {
    'openai/gpt-oss-120b': 'Apache-2.0',
    'openai/gpt-oss-20b': 'Apache-2.0',
    'google/gemma-4-31b-it': 'Apache-2.0',
    'deepseek/deepseek-r1': 'MIT',
    'qwen/qwen3.8-27b': 'Apache-2.0',
}


def validate_model(model):
    if model not in ALLOWLIST:
        raise ValueError('teacher model is not allowlisted')


def openrouter_client():
    key = os.environ.get('OPENROUTER_API_KEY')
    if not key:
        raise ValueError('OPENROUTER_API_KEY is unset; set it in the environment')

    def client(model, messages, temperature):
        validate_model(model)
        payload = {'model': model, 'messages': messages, 'temperature': temperature,
                   'usage': {'include': True}, 'provider': {'data_collection': 'deny'}}
        request = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
            data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + key,
                                                       'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.load(response)
            # Never inspect reasoning fields or expose the response in errors.
            text = result['choices'][0]['message']['content']
            cost = result['usage']['cost']
            if not isinstance(text, str) or isinstance(cost, bool) or not isinstance(cost, (int, float)):
                raise ValueError('invalid response')
            if not math.isfinite(cost) or cost < 0:
                raise ValueError('invalid cost')
            return text, float(cost)
        except Exception:
            raise RuntimeError('teacher request failed or returned invalid content/cost') from None
    return client


def _candidates(text):
    # Only use an unwrapped form if it independently passes the same guard.
    stripped = text.strip()
    if len(stripped) >= 2 and (stripped[0], stripped[-1]) in (("'", "'"), ('"', '"'), ('“', '”')):
        yield stripped[1:-1]
        return
    wrapper = re.match(r'^Here (?:is|are)\b[^:\n]{0,100}:\s*(.*)\Z', stripped, re.I | re.S)
    if wrapper:
        body = wrapper[1]
        if len(body) >= 2 and (body[0], body[-1]) in (("'", "'"), ('"', '"'), ('“', '”')):
            yield body[1:-1]
        else:
            yield body
        return
    yield text


def run(rows, model, task, client=None, *, limit=100, max_usd=.02, rng=None):
    validate_model(model)  # before client construction, iteration or any request
    if task not in ('messify', 'clean'):
        raise ValueError('teacher task must be messify or clean')
    if not math.isfinite(max_usd) or max_usd < 0 or limit < 0:
        raise ValueError('budget and limit must be nonnegative and finite')
    rng = rng or random.Random(0)
    stats = {'requests': 0, 'accepted': 0, 'rejected': Counter(), 'spend_usd': 0.0}
    output = []
    for row in rows:
        if stats['requests'] >= limit or stats['spend_usd'] >= max_usd:
            break
        if row.get('origin') == 'owner' or row.get('source') == 'owner' or row.get('owner_derived'):
            stats['rejected']['owner'] += 1
            continue
        if row.get('origin') not in ('public', 'template'):
            stats['rejected']['origin'] += 1
            continue
        if client is None:
            client = openrouter_client()
        target = tidy_target(row.get('target', row.get('text', '')))
        text = target if task == 'messify' else row['messy']
        instruction = (
            'Turn this text into natural disfluent dictation, using fillers, repeated starts '
            'and self corrections. Preserve every original word in order. Return only the dictation.'
            if task == 'messify' else
            'Clean this dictation by removing fillers, repeats and abandoned corrections. '
            'Only delete words; never add, substitute or reorder words. Adjust case and punctuation. '
            'Return only the cleaned text, on one line.')
        temperature = rng.uniform(.7, 1.0)
        result, cost = client(model, [{'role': 'system', 'content': instruction},
                                     {'role': 'user', 'content': text}], temperature)
        stats['requests'] += 1
        if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                or not math.isfinite(cost) or cost < 0):
            raise ValueError('teacher client returned invalid cost; refusing further requests')
        stats['spend_usd'] += cost
        if not isinstance(result, str):
            stats['rejected']['invalid_content'] += 1
            continue
        accepted, reason = None, 'added_word'
        for candidate in _candidates(result):
            ok, reason = pair_check(candidate, target) if task == 'messify' else pair_check(text, candidate)
            if ok:
                accepted = candidate
                break
        if accepted is None:
            stats['rejected'][reason] += 1
            continue
        provenance = dict(row.get('provenance', {}))
        provenance['teachers'] = [*provenance.get('teachers', []), {
            'model': model, 'licence': ALLOWLIST[model], 'task': task,
            'temperature': temperature, 'cost_usd': cost}]
        output.append({**row, 'messy': accepted if task == 'messify' else text,
                       'target': target if task == 'messify' else accepted,
                       'source_id': row.get('source_id') or source_id(row.get('text', target)),
                       'kind': 'teacher', 'task': task, 'guard_compatible': True,
                       'provenance': provenance})
        stats['accepted'] += 1
    stats['rejected'] = dict(stats['rejected'])
    return output, stats
