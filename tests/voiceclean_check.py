"""Free VA-3c checks: synthetic fixtures only, no network or real models.

PYTHONPATH=$PWD /home/johnw/projects/Jarvis/.venv/bin/python tests/voiceclean_check.py
"""
from __future__ import annotations

import contextlib
import gzip
import importlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import pkgutil
import random
import socket
import stat
import sys
import tempfile
from unittest.mock import patch

PASSED = 0
FAILED = 0


def check(name, condition):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print('  ok  ' + name)
    else:
        FAILED += 1
        print(' FAIL ' + name)


def raises(fn, kind=ValueError, contains=None):
    try:
        fn()
    except kind as exc:
        return contains is None or contains in str(exc)
    return False


class ImportFence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('jarvis.config', 'jarvis.voice', 'jarvis.stt_local', 'jarvis.tools.secrets'):
            raise AssertionError('forbidden Jarvis import')
        if fullname.split('.')[0] in ('numpy', 'kokoro_onnx', 'sherpa_onnx'):
            raise AssertionError('real model dependency imported in tests')


def no_network(*args, **kwargs):
    raise AssertionError('network attempted in free checks')


def fixture(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')


def main():
    real_home = Path.home()
    with tempfile.TemporaryDirectory(prefix='voiceclean-check-') as tmp:
        home = Path(tmp) / 'home'
        home.mkdir()
        # Some managed sandboxes place a read-only .git marker at /tmp.
        # Model a separate fixture filesystem: ignore only git markers above
        # its root. Real production checks remain untouched, and git/gitfile/
        # symlink rejection is exercised below inside this fixture filesystem.
        original_exists = Path.exists
        outer_git_markers = {p / '.git' for p in Path(tmp).parents}
        fixture_exists = lambda p, *a, **kw: False if p in outer_git_markers else original_exists(p, *a, **kw)
        exists_patch = patch.object(Path, 'exists', fixture_exists)
        exists_patch.start()
        os.environ['HOME'] = str(home)
        os.environ.pop('VOICECLEAN_DATA_DIR', None)
        os.environ.pop('OPENROUTER_API_KEY', None)
        sys.meta_path.insert(0, ImportFence())

        def audit(event, args):
            if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(args[0])).absolute()
                protected = [real_home / p for p in ('.claude', '.codex', '.config/jarvis',
                                                     '.local/share/jarvis')]
                if any(path == p or p in path.parents for p in protected):
                    raise AssertionError('real owner directory accessed')
                if any(p.startswith('.env') for p in path.parts) and home not in path.parents:
                    raise AssertionError('nonfixture .env accessed')
            if event in ('socket.connect', 'socket.getaddrinfo'):
                raise AssertionError('network attempted')
        sys.addaudithook(audit)
        socket.create_connection = no_network

        from voiceclean import (build, corrupt, data, normalize, owner_text, pair_check,
                                prose, public_text, roundtrip, storage, teachers, templates)
        import voiceclean
        import urllib.request
        urllib.request.urlopen = no_network

        def guard_checks():
            accepted = [
                ('um please check the intake', 'Please check the intake.'),
                ('meet on tuesday no wait wednesday at noon', 'Meet on Wednesday at noon.'),
                ('we need twenty five parts', 'We need 25 parts.'),
                ('in two thousand twenty six we will compete', 'In 2026 we will compete.'),
                ('we used nine hundred ninety nine thousand nine hundred ninety nine parts',
                 'We used 999999 parts.'),
                ('we need one hundred and five parts', 'We need 105 parts.'),
                ('please check comma then test period', 'Please check, then test.'),
                ('is it ready question mark', 'Is it ready?'),
                ('we do not know if i am ready', "We don't know if I'm ready."),
                ("i'm ready and don't need help", 'I am ready and do not need help.'),
                ('use twenty five percent power', 'Use 25% power.'),
                ('we have 999,999 parts', 'We have 999999 parts.'),
            ]
            for n, (messy, target) in enumerate(accepted):
                check(f'pair accepts equivalence {n}', pair_check.pair_check(messy, target)[0])
            rejected = [
                ('please test intake', 'Please test the intake.', 'added_word'),
                ('test the intake', 'Test the lift.', 'added_word'),
                ('test the intake', 'Intake the test.', 'reordered'),
                ('test the intake', 'Test\nthe intake.', 'newline'),
                ('test the intake', 'Test\rthe intake.', 'newline'),
                ('test the intake', 'Test\u2028the intake.', 'newline'),
                ('test the intake', 'Test\tthe intake.', 'control_char'),
                ('test the intake', 'Test\x00the intake.', 'control_char'),
                ('test the intake', 'Test\u200bthe intake.', 'control_char'),
                ('one two three four five six seven eight nine ten', 'One two three.', 'over_deletion'),
                ('test the intake', '', 'empty'),
                ('test the intake', '.', 'empty'),
                ('test period the intake', 'Test comma the intake.', 'added_word'),
            ]
            for n, (messy, target, reason) in enumerate(rejected):
                check(f'pair rejects {reason} {n}', pair_check.pair_check(messy, target) == (False, reason))
            check('pair rejects reorder specifically', not pair_check.pair_check('red blue green', 'Blue red green.')[0])
            check('duplicate word addition rejected', pair_check.pair_check('we can test', 'We can can test.')[1] == 'added_word')
            check('exact sixty percent deletion allowed', pair_check.pair_check('a b c d e', 'D e.')[0])
            check('number grammar does not sum adjacent words', normalize.tokens('one two twenty five six') == ['1', '2', '25', '6'])
            check('number full range endpoints', normalize.tokens('zero nine hundred ninety nine thousand nine hundred ninety nine') == ['0', '999999'])
            check('number words inverse sampled', all(normalize.tokens(normalize.number_words(n)) == [str(n)]
                  for n in [0, 1, 20, 25, 101, 999, 1000, 2026, 999999, *range(0, 999999, 997)]))
            check('leading zero time equivalence', pair_check.pair_check('meet at ten zero', 'Meet at 10:00.')[0])
            bool_rows = [{'text': 'Please test the intake.', 'origin': 'public'}] * 3
            try:
                teachers.run(bool_rows, 'openai/gpt-oss-20b', 'messify',
                             lambda m, msgs, t: ('um please test the intake', False), limit=3, max_usd=.02)
                bool_refused = False
            except ValueError:
                bool_refused = True
            check('boolean cost refused, not counted as zero', bool_refused)
            with tempfile.TemporaryDirectory() as bd:
                bdir = Path(bd) / 'data'
                bdir.mkdir(mode=0o700)
                rows = [{'messy': f'{f} please check the private intake', 'target': 'Please check the private intake.',
                         'text': 'Please check the private intake.', 'origin': 'public', 'source_id': f'id{i}'}
                        for i, f in enumerate(['um'] * 5 + ['uh'] * 5)]
                storage.write_json(bdir / 'pairs-synth-9.jsonl', rows, lines=True)
                build.build(bdir)
                splits = {name for name in ('train', 'dev', 'test')
                          if (bdir / 'dataset' / f'{name}.jsonl').exists()
                          and (bdir / 'dataset' / f'{name}.jsonl').read_text().strip()}
                check('supplied source ids cannot split one source', len(splits) == 1)
            check('source ids ignore presentation', storage.source_id('We need twenty five parts.') == storage.source_id('we need 25 parts'))

        def prose_checks():
            check('short prose kept', prose.is_prose('Please check the intake before practice.'))
            bad = ['hi there', 'word ' * 61, 'Read https://example.com please', 'Open /tmp/source now',
                   'Read config.yaml before tomorrow', 'Open README.md for details', 'Visit example.com after practice',
                   'Open C:\\Users\\source now', '```python\nprint something here\n```',
                   'Traceback (most recent call last):', '- Please check this', '# Please check this',
                   '> Please check this', '* Please check this', 'xxxxxxxx ####### $$$$$ !!!!!',
                   'ééééééééé please check this', 'Type error\x00 into text']
            check('prose rejects structural noise', all(not prose.is_prose(s) for s in bad))
            source = "  i think i'm ready. can we test PID? i’ve checked V5 Brain  "
            target = prose.tidy_target(source)
            check('target tidying is word preserving', target == "I think I'm ready. Can we test PID? I’ve checked V5 Brain.")
            check('tidied target passes guard', pair_check.pair_check(source, target)[0])
            check('quoted sentence starts capitalized', prose.tidy_target('"hello there." "please wait"') == '"Hello there." "Please wait".')

        def corruption_checks():
            sentences = [
                'Please check the V5 Brain on Wednesday at 10:30.',
                "I'm ready, but don't move the drivetrain until Tuesday.",
                'We need 25 parts in January for team 1234A.',
                'Please tune PID and odometry before the Skills Challenge.',
                'Set the intake to 50 percent and then wait.',
                'We will meet in two thousand twenty six on Monday.',
                'Can we check the inertial sensor before practice?',
            ] + [r['text'] for r in templates.generate(12, 80)]
            failures = 0
            operations = set()
            protected_failures = 0
            for n in range(5000):
                source = sentences[n % len(sentences)]
                pair = corrupt.corrupt(source, random.Random(n))
                failures += not pair_check.pair_check(pair.messy, pair.target)[0]
                operations.update(pair.ops)
                if 'V5 Brain' in source:
                    protected_failures += 'v5 brain' not in pair.messy
                if 'inertial sensor' in source:
                    protected_failures += 'inertial sensor' not in pair.messy
            check('5000 seeded pairs pass deletion-only invariant', failures == 0)
            check('protected terms never split by fillers', protected_failures == 0)
            check('all corruption operations exercised', operations >= set(corrupt.DEFAULT_PROFILE) - {'partial_punctuation', 'intensity_min'})
            check('corruption deterministic', corrupt.corrupt(sentences[0], random.Random(444)) ==
                  corrupt.corrupt(sentences[0], random.Random(444)))
            check('templates deterministic', list(templates.generate(42, 20)) == list(templates.generate(42, 20)))
            check('at least sixty VEX terms', len(templates.protected_terms()) >= 60)
            names = home / 'private-names.txt'
            names.write_text('Ada Lovelace\nGrace Hopper\n')
            rows = list(templates.generate(8, 100, names))
            check('owner names templates flagged', any(r['owner_derived'] for r in rows) and
                  all(r['owner_derived'] for r in rows if 'Ada Lovelace' in r['text'] or 'Grace Hopper' in r['text']))
            protected = corrupt.corrupt('Please ask Ada Lovelace to check the V5 Brain.', random.Random(7),
                                        {'fillers': 1, 'protected_terms': ['Ada Lovelace']})
            check('custom names never split', 'ada lovelace' in protected.messy and 'v5 brain' in protected.messy)
            only_corrections = {key: 0 for key in corrupt.DEFAULT_PROFILE} | {'self_correction': 1, 'intensity_min': 1}
            corrected = corrupt.corrupt('Please bring twenty five parts to practice.', random.Random(1), only_corrections)
            check('number-word self corrections generated', corrected.ops == ['self_correction']
                  and pair_check.pair_check(corrected.messy, corrected.target)[0])

        def owner_checks():
            desktop = {'type': 'user', 'entrypoint': 'claude-desktop', 'userType': 'external',
                       'message': {'content': 'Please check the desktop intake before practice.'}}
            entries = [desktop]
            for field, value in [('entrypoint', 'sdk-py'), ('isSidechain', True), ('isMeta', True)]:
                entries.append({**desktop, field: value})
            for content in ['<task-notification>please check this</task-notification>',
                            'This session is being continued from a previous conversation',
                            '<pasted_content>Please check this message</pasted_content>',
                            '```please check this code block```', '# Please check this heading',
                            '- first item\n- second item\n- third item',
                            'Read https://example.com for details',
                            'Please use sk-or-' + 'Abc123def456Ghi789xyz012345' + ' for testing']:
                entries.append({**desktop, 'message': {'content': content}})
            entries.append({**desktop, 'message': {'content': [{'type': 'tool_result', 'content': 'Please check this'}]}})
            entries.append({**desktop, 'message': {'content': [{'type': 'text', 'text': 'Please check the typed text block.'}]}})
            entries.append({**desktop, 'message': {'content': '  please check the desktop intake before practice.  '}})
            fixture(home / '.claude/projects/fake/chat.jsonl', entries)
            fixture(home / '.claude/projects/fake/subagents/child.jsonl', [desktop])
            for origin in ('codex_exec', 'codex-tui'):
                fixture(home / f'.codex/sessions/{origin}.jsonl', [
                    {'type': 'session_meta', 'payload': {'originator': origin}},
                    {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': f'Please check the interactive codex intake today.'}},
                    {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Please check the assistant intake today.'}}])
            fixture(home / '.local/share/jarvis/sessions/old/log.jsonl', [
                {'role': 'you', 'text': 'Please check the legacy intake before practice.'},
                {'role': 'assistant', 'text': 'Please check the legacy assistant message.'}])
            fixture(home / '.local/share/jarvis/v2/threads/new/log.jsonl', [
                {'kind': 'user', 'data': {'origin': 'owner', 'typed': 'Please check the modern intake before practice.', 'spoken': True}},
                {'kind': 'user', 'data': {'origin': 'agent', 'typed': 'Please check this agent message today.'}},
                {'kind': 'assistant', 'data': {'origin': 'owner', 'typed': 'Please check the assistant modern message.'}}])
            # Forbidden dotenv files are fixtures only; no code may read them.
            (home / '.env.fake').write_text('not a real credential')
            rows, counts = owner_text.extract()
            check('owner extractor exact source counts', counts['kept'] == {'v2': 1, 'v1': 1, 'claude': 2, 'codex': 1})
            check('owner extractor removes duplicates', counts['dropped'].get('claude:dup') == 1)
            check('owner rows marked and spoken recorded', len(rows) == 5 and all(r['origin'] == 'owner' for r in rows)
                  and sum(r['spoken'] for r in rows) == 1)
            check('codex_exec excluded', [r['session'] for r in rows if r['source'] == 'codex'] == ['codex-tui'])
            check('credential drops whole message', owner_text.clean('Please use password: hunter22 for this request') is None)
            check('system reminder removed like prototype', owner_text.clean('<system-reminder>injected context</system-reminder>Please check the intake today.') == 'Please check the intake today.')
            directory = home / 'owner-output'
            written = owner_text.write(directory)
            check('owner private output written', written['total'] == 5 and len(list(storage.read_jsonl(directory / 'owner_text.jsonl'))) == 5)

        def public_checks():
            text = 'Please check the intake before practice. '
            rows = [
                {'role': 'prompter', 'lang': 'en', 'text': text * 12, 'message_id': 'human'},
                {'role': 'assistant', 'lang': 'en', 'text': text, 'message_id': 'assistant'},
                {'role': 'prompter', 'lang': 'de', 'text': text, 'message_id': 'german'},
                {'role': 'prompter', 'lang': 'en', 'text': 'Here is code. ```Do not keep this```', 'message_id': 'code'},
            ]
            path = home / 'public.jsonl'
            fixture(path, rows)
            plain = list(public_text.read(path))
            check('public groups long prompts into short prose', len(plain) == 2 and all(prose.is_prose(r['text']) for r in plain))
            check('public provenance recorded', all(r['origin'] == 'public' and r['message_id'] == 'human'
                  and r['provenance']['file_sha256'] == storage.file_hash(path)
                  and r['provenance']['licence'] == 'Apache-2.0' for r in plain))
            compressed = home / 'public.jsonl.gz'
            with gzip.open(compressed, 'wt') as fh:
                fh.write(path.read_text())
            gz = list(public_text.read(compressed))
            check('gzip public input supported', [r['text'] for r in gz] == [r['text'] for r in plain]
                  and gz[0]['provenance']['file_sha256'] == storage.file_hash(compressed))

        def teacher_checks():
            source = {'text': 'Please test the intake.', 'messy': 'um please test the intake',
                      'target': 'Please test the intake.', 'origin': 'public', 'source_id': 'source',
                      'provenance': {'licence': 'Apache-2.0'}}
            calls = []

            def client(model, messages, temperature):
                calls.append((model, temperature))
                return 'um please test the intake', .01

            for model in ('anthropic/claude-haiku-5.5', 'openai/gpt-6.1-sol', 'other/model'):
                check('nonallowlisted teacher refused ' + model,
                      raises(lambda: teachers.run([source], model, 'messify', client)))
            check('model refusal made no client call', not calls)
            for model in teachers.ALLOWLIST:
                out, stats = teachers.run([source], model, 'messify', client)
                check('allowlisted teacher licence ' + model, len(out) == 1 and out[0]['provenance']['teachers'][0]['licence'] == teachers.ALLOWLIST[model])
            calls.clear()
            owner = {**source, 'origin': 'owner'}
            out, stats = teachers.run([owner], 'openai/gpt-oss-20b', 'messify', client)
            check('owner refusal before any client call', not out and not calls and stats['rejected'] == {'owner': 1})
            out, stats = teachers.run([{**source, 'source': 'owner'}], 'openai/gpt-oss-20b', 'messify', client)
            check('owner source marker also refused', not out and not calls and stats['rejected'] == {'owner': 1})
            out, stats = teachers.run([{**source, 'origin': 'template', 'owner_derived': True}],
                                     'openai/gpt-oss-20b', 'messify', client)
            check('owner names refused before any client call', not out and not calls and stats['rejected'] == {'owner': 1})
            out, stats = teachers.run([source] * 10, 'openai/gpt-oss-20b', 'messify', client, max_usd=.02)
            check('teacher budget stops further requests', len(calls) == 2 and stats['requests'] == 2 and stats['spend_usd'] == .02)
            check('teacher temperatures bounded', all(.7 <= t <= 1 for _, t in calls))
            out, stats = teachers.run([source], 'openai/gpt-oss-20b', 'messify', client, max_usd=0)
            check('zero budget means zero requests', stats['requests'] == 0)
            responses = iter(['Please test the lift.', 'Intake the test please.', 'Please\ntest the intake.', '', 'Please test the intake.'])
            out, stats = teachers.run([source] * 5, 'deepseek/deepseek-r1', 'clean',
                                     lambda *args: (next(responses), 0), max_usd=1)
            check('teacher rejects counted by guard reason', stats['rejected'] ==
                  {'added_word': 1, 'reordered': 1, 'newline': 1, 'empty': 1} and stats['accepted'] == 1)
            check('clean task keeps raw messy', out[0]['messy'] == source['messy'])
            out, stats = teachers.run([source], 'deepseek/deepseek-r1', 'messify',
                                     lambda *args: ('Here is the dictation: "um please test the intake"', 0))
            check('teacher wrapper removed only after guard', len(out) == 1 and out[0]['messy'] == 'um please test the intake')
            out, stats = teachers.run([source], 'deepseek/deepseek-r1', 'messify',
                                     lambda *args: ('Here is the dictation: "please move the lift"', 0))
            check('invalid wrapped teacher output rejected', not out and stats['rejected'] == {'added_word': 1})
            check('missing API key clear error', raises(teachers.openrouter_client, contains='OPENROUTER_API_KEY is unset'))
            check('invalid budget refused', raises(lambda: teachers.run([], 'deepseek/deepseek-r1', 'clean', client, max_usd=float('nan'))))
            check('invalid returned cost aborts', raises(lambda: teachers.run([source], 'deepseek/deepseek-r1', 'clean', lambda *a: ('', -1))))

            requests = []
            def http(request, timeout):
                requests.append(request)
                return io.StringIO(json.dumps({'choices': [{'message': {'content': 'Please test the intake.',
                                      'reasoning': 'must never become training text'}}], 'usage': {'cost': .001}}))
            with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'fake-test-key'}), patch('urllib.request.urlopen', http):
                client = teachers.openrouter_client()
                result, cost = client('deepseek/deepseek-r1', [], .8)
                body = json.loads(requests[0].data)
                check('HTTP adapter content only and privacy flags', result == 'Please test the intake.' and cost == .001
                      and body['usage'] == {'include': True} and body['provider'] == {'data_collection': 'deny'})
                check('HTTP adapter itself validates models', raises(lambda: client('openai/gpt-6.1-sol', [], .8)) and len(requests) == 1)
            with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'fake-test-key'}), patch('urllib.request.urlopen', side_effect=RuntimeError('fake-test-key')):
                check('HTTP errors never expose API key', raises(lambda: teachers.openrouter_client()('deepseek/deepseek-r1', [], .8), RuntimeError,
                                                                               'teacher request failed'))

        def roundtrip_checks():
            rows = [{'messy': 'um test the intake', 'target': 'Test the intake.', 'origin': 'owner', 'source_id': 'id'},
                    {'messy': 'um test the intake', 'target': 'Test the intake.', 'origin': 'public', 'source_id': 'id2'}]
            calls = []
            def tts(text, voice, speed):
                calls.append((voice, speed))
                return [0.0, .1], 24000
            results = iter(['um test the lift', 'um test the intake'])
            def stt(samples, rate):
                assert samples == [0.0, .1] and rate == 24000
                return next(results)
            out = list(roundtrip.run(rows, tts, stt, ['voice-a', 'voice-b'], random.Random(3)))
            check('roundtrip retains substitution as incompatible', out[0]['guard_compatible'] is False)
            check('roundtrip valid deletion marked compatible', out[1]['guard_compatible'] is True)
            check('roundtrip retains origin target source', all(o['origin'] == r['origin'] and o['target'] == r['target']
                  and o['source_id'] == r['source_id'] and o['kind'] == 'roundtrip' for o, r in zip(out, rows)))
            check('roundtrip voice and speed bounded', all(v in ('voice-a', 'voice-b') and .9 <= s <= 1.15 for v, s in calls))
            check('roundtrip empty voices refused', raises(lambda: list(roundtrip.run(rows, tts, stt, [], random.Random(3)))))

        def storage_build_cli_checks():
            git = home / 'fake-git'
            (git / '.git').mkdir(parents=True)
            check('data dir git worktree refused', raises(lambda: storage.data_dir(git / 'private' / 'data')))
            worktree = home / 'fake-worktree'
            worktree.mkdir()
            (worktree / '.git').write_text('gitdir: /fake/worktrees/one')
            check('data dir gitfile worktree refused', raises(lambda: storage.data_dir(worktree / 'data')))
            alias = home / 'git-alias'
            alias.symlink_to(git, target_is_directory=True)
            check('data dir symlink into git refused', raises(lambda: storage.data_dir(alias / 'data')))
            check('dotenv read inputs refused', raises(lambda: storage.safe_input(home / '.env.fake')))
            directory = storage.data_dir(home / 'private' / 'data')
            with patch.dict(os.environ, {'VOICECLEAN_DATA_DIR': str(directory)}):
                check('data dir env override', storage.data_dir() == directory)
            check('default data dir private path', storage.data_dir() == home / '.local/share/jarvis/va3-data')
            rows = []
            for n in range(250):
                source = f'Please check the intake before practice number {n}.'
                for kind in ('synthetic', 'roundtrip'):
                    rows.append({'text': source, 'source_id': storage.source_id(source),
                        'messy': ('um ' if kind == 'synthetic' else 'uh ') + source,
                        'target': source, 'origin': 'public' if n % 2 else 'owner',
                        'kind': kind, 'ops': ['fillers'], 'seed': 42,
                        'provenance': {'licence': 'Apache-2.0'}})
            storage.write_json(directory / 'pairs-a.jsonl', rows, lines=True)
            storage.write_json(directory / 'pairs-b.jsonl', [rows[0]], lines=True)
            card = build.build(directory)
            check('build dedupe and total correct', card['total'] == 500 and card['duplicates_removed'] == 1)
            check('build card origin kind and op counts', card['counts']['origin'] == {'owner': 250, 'public': 250}
                  and card['counts']['kind'] == {'synthetic': 250, 'roundtrip': 250}
                  and card['counts']['op'] == {'fillers': 500})
            check('build card metadata', card['guard_compatible'] == {'count': 500, 'share': 1.0}
                  and card['seeds'] == [42] and card['licences'] == ['Apache-2.0']
                  and card['private_note'] == build.PRIVATE_NOTE
                  and card['input_sha256']['pairs-a.jsonl'] == storage.file_hash(directory / 'pairs-a.jsonl'))
            seen = {}
            leakage = False
            total = 0
            for split in ('train', 'dev', 'test'):
                loaded = list(storage.read_jsonl(directory / 'dataset' / f'{split}.jsonl'))
                total += len(loaded)
                check('build split count ' + split, len(loaded) == card['counts']['split'][split] and len(loaded) > 0)
                for row in loaded:
                    assert build.split_for(row['source_id']) == split
                    leakage |= row['source_id'] in seen and seen[row['source_id']] != split
                    seen[row['source_id']] = split
            check('no source id appears in two splits', not leakage and total == 500)
            check('build deterministic and excludes prior outputs', build.build(directory) == card)
            check('private directory modes', all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in (directory, directory.parent, directory / 'dataset')))
            check('every output mode private', all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.rglob('*') if p.is_file()))
            target = directory / 'pairs-b.jsonl'
            target.chmod(0o644)
            storage.write_json(target, [], lines=True)
            check('existing file mode repaired', stat.S_IMODE(target.stat().st_mode) == 0o600)
            link = directory / 'linked.jsonl'
            link.symlink_to(target)
            check('symlink output refused', raises(lambda: storage.write_json(link, [], lines=True)))
            link.unlink()

            mixed = storage.data_dir(home / 'mixed')
            base = {'messy': 'um please test the intake', 'target': 'Please test the intake.',
                    'source_id': 'fixed-source', 'origin': 'public', 'kind': 'teacher', 'seed': 1,
                    'provenance': {'licence': 'Apache-2.0', 'teachers': [
                        {'model': 'deepseek/deepseek-r1', 'licence': 'MIT'}]}}
            private = {**base, 'origin': 'owner', 'seed': 2, 'provenance': {'licence': 'private-owner',
                       'roundtrip': {'tts_licence': 'Apache-2.0', 'stt_licence': 'CC-BY-4.0'}}}
            incompatible = {**base, 'messy': 'please test the lift', 'kind': 'roundtrip', 'seed': 3}
            storage.write_json(mixed / 'pairs.jsonl', [base, private, incompatible], lines=True)
            mixed_card = build.build(mixed)
            retained = [r for s in ('train', 'dev', 'test') for r in
                        storage.read_jsonl(mixed / 'dataset' / f'{s}.jsonl')]
            check('dedupe never loses private origin', mixed_card['owner_derived'] == 1 and
                  any(r['origin'] == 'owner' and r['owner_derived'] for r in retained))
            check('card retains duplicate seeds and provenance', mixed_card['seeds'] == [1, 2, 3] and
                  mixed_card['licences'] == ['Apache-2.0', 'CC-BY-4.0', 'MIT', 'private-owner'] and
                  mixed_card['teacher_models'] == ['deepseek/deepseek-r1'])
            check('card incompatible share correct', mixed_card['guard_compatible'] == {'count': 1, 'share': .5})
            literal = storage.data_dir(home / 'literal-punctuation')
            storage.write_json(literal / 'public_text.jsonl', [
                {'text': 'Please put a comma in the sentence.', 'origin': 'public'}], lines=True)
            skipped = {'sources': 0, 'skipped_sources': 0}
            check('literal punctuation words are synthesizable',
                  len(list(data.synthetic(literal, 1, 1, 0, skipped))) == 1 and
                  skipped == {'sources': 1, 'skipped_sources': 0})

            cli_dir = home / 'cli-data'
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                first = data.main(['synth', '--seed', '15', '--per-source', '2', '--template-count', '10', '--data-dir', str(cli_dir)])
                second = data.main(['--data-dir', str(cli_dir), 'build'])
            check('CLI synthetic and build steps', first == second == 0 and len(output.getvalue().splitlines()) == 2)
            check('CLI prints counts only', 'messy' not in output.getvalue() and 'Please' not in output.getvalue())
            check('synth source ids and seeds present', all(r['source_id'] and 'seed' in r and r['origin'] == 'template'
                  for r in storage.read_jsonl(cli_dir / 'pairs-synth-15.jsonl')))
            output = io.StringIO()
            statuses = []
            fake_tts = lambda text, voice, speed: ([0.0], 24000)
            fake_stt = lambda samples, rate: 'Please test the intake.'
            fake_client = lambda model, messages, temperature: (messages[-1]['content'], .001)
            with contextlib.redirect_stdout(output), patch.object(roundtrip, 'kokoro_backend', return_value=fake_tts), \
                    patch.object(roundtrip, 'parakeet_backend', return_value=fake_stt), \
                    patch.object(teachers, 'openrouter_client', return_value=fake_client):
                commands = [
                    ['owner-text'], ['public', '--oasst2', str(home / 'public.jsonl')],
                    ['synth', '--seed', '16', '--per-source', '1', '--template-count', '10'],
                    ['roundtrip', '--seed', '16', '--limit', '3'],
                    ['teach', '--model', 'openai/gpt-oss-20b', '--task', 'messify', '--limit', '2'],
                    ['teach', '--model', 'deepseek/deepseek-r1', '--task', 'clean', '--limit', '2'], ['build']]
                for command in commands:
                    statuses.append(data.main(['--data-dir', str(cli_dir), *command]))
            check('all seven CLI steps with injected backends', statuses == [0] * 7)
            summaries = [json.loads(line) for line in output.getvalue().splitlines()]
            check('full CLI teacher counters and counts-only output', len(summaries) == 7 and
                  summaries[4]['requests'] == summaries[5]['requests'] == 2 and
                  'Please' not in output.getvalue())

        for fn in (guard_checks, prose_checks, corruption_checks, owner_checks, public_checks,
                   teacher_checks, roundtrip_checks, storage_build_cli_checks):
            try:
                fn()
            except Exception as exc:
                check(fn.__name__ + ' completed (' + type(exc).__name__ + ')', False)

        for module in pkgutil.walk_packages(voiceclean.__path__, voiceclean.__name__ + '.'):
            importlib.import_module(module.name)
        check('all modules imported without jarvis.config', 'jarvis.config' not in sys.modules)
        check('no forbidden runtime or model imports', not any(m in sys.modules for m in
              ('jarvis.voice', 'jarvis.stt_local', 'jarvis.tools.secrets', 'numpy', 'kokoro_onnx', 'sherpa_onnx')))
        exists_patch.stop()
    print(f'voiceclean_check: {PASSED} passed, {FAILED} failed')
    return bool(FAILED)


if __name__ == '__main__':
    raise SystemExit(main())
