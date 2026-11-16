"""VA-3a free eval/record checks: fake HOME, TTY, audio and models only."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import types
import wave
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PASSED = FAILED = 0


def check(name, ok):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print('  ok  ' + name)
    else:
        FAILED += 1
        print(' FAIL ' + name)


def raises(fn, kind):
    try:
        fn()
    except kind:
        return True
    return False


class Fence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'jarvis.config' or fullname.split('.')[0] in ('numpy', 'sherpa_onnx', 'kokoro_onnx'):
            raise AssertionError('forbidden configuration or real model import')


class FakeIO:
    def __init__(self, answers=(), tty=True):
        self.answers = iter(answers)
        self.tty = tty
        self.output = []
        self.reads = []

    def isatty(self):
        return self.tty

    def write(self, text):
        self.output.append(text)

    def read(self, prompt, timeout=None):
        self.reads.append((prompt, timeout))
        answer = next(self.answers, EOFError())
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeRecorder:
    def __init__(self):
        self.paths = []
        self.stops = 0

    def start(self, path):
        assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
        self.paths.append(path)
        # Fill the precreated private file with fake bytes, not real audio.
        path.write_bytes(b'fake wav')

    def stop(self):
        self.stops += 1


def eval_checks(home):
    from voiceclean import data, eval as ev, storage
    directory = storage.data_dir(home / 'evaluation')
    fixture = [
        {'messy': 'um hello world', 'target': 'Hello world.', 'origin': 'template', 'ops': ['self_correction']},
        {'messy': 'we need five parts', 'target': 'We need 5 parts.', 'origin': 'owner',
         'ops': ['number_words'], 'kind': 'roundtrip'},
        {'messy': 'is it ready', 'target': 'Is it ready?', 'origin': 'public'},
    ]
    storage.write_json(directory / 'dataset/dev.jsonl', fixture, lines=True)
    storage.write_json(directory / 'dataset/test.jsonl', fixture[:1], lines=True)
    check('Levenshtein insert delete substitute and empty',
          ev.distance(['a', 'b', 'c'], ['a', 'x', 'c', 'd']) == 2 and
          ev.distance(['a', 'b'], []) == 2 and ev.distance([], ['a']) == 1 and ev.distance([], []) == 0)
    b0 = ev.evaluate(directory, system='b0')
    all_rows = b0['suites']['all']
    check('B0 three-row hand-computed content WER', all_rows['n'] == 3 and all_rows['wer_words'] == 1 / 9)
    check('B0 three-row hand-computed formatting WER', all_rows['wer_format'] == 8 / 9)
    check('B0 exact and rejection rates', all_rows['exact_match_rate'] == all_rows['guard_rejection_rate'] == 0)
    check('suite counts by op origin question and kind',
          all(b0['suites'][s]['n'] == 1 for s in ('self_correction', 'numbers', 'questions',
              'template', 'owner', 'public', 'roundtrip')) and b0['suites']['gold']['n'] == 0)
    b1 = ev.evaluate(directory, system='b1')
    check('B1 three-row hand-computed WER', b1['suites']['all']['wer_words'] == 0 and
          b1['suites']['all']['wer_format'] == 1 / 3)
    fake = types.ModuleType('va3a_fake')
    def add(raw):
        print(raw)
        print(raw, file=sys.stderr)
        return raw + ' zebra'
    fake.add = add
    fake.bad = lambda raw: raw + '\n'
    fake.nontext = lambda raw: None
    def explode(raw):
        raise RuntimeError('private row: ' + raw)
    fake.explode = explode
    with patch.dict(sys.modules, {'va3a_fake': fake}):
        model = ev.evaluate(directory, system='va3a_fake:add')
        check('model added-word rejection and rules fallback',
              model['suites']['all']['guard_rejection_rate'] == 1 and
              model['suites']['all']['guard_rejection_reasons'] == {'added_word': 3} and
              model['suites']['all']['wer_format'] == b1['suites']['all']['wer_format'])
        check('rejection reasons counted per suite', model['suites']['numbers']['guard_rejection_reasons'] == {'added_word': 1})
        check('newline model rejection', ev.evaluate(directory, system='va3a_fake:bad')['suites']['all']
              ['guard_rejection_reasons'] == {'newline': 3})
        check('non-text model rejection', ev.evaluate(directory, system='va3a_fake:nontext')['suites']['all']
              ['guard_rejection_reasons'] == {'not_text': 3})
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = data.main(['--data-dir', str(directory), 'eval', '--system', 'va3a_fake:add'])
            failed = data.main(['eval', '--data-dir', str(directory), '--system', 'va3a_fake:explode'])
        check('CLI eval succeeds and model failure is sanitized', status == 0 and failed == 1 and
              'private row' not in errors.getvalue())
        check('CLI output contains metrics and no row text', 'wer_words' in output.getvalue() and
              all(row[key] not in output.getvalue() + errors.getvalue() for row in fixture for key in ('messy', 'target')))
    storage.write_json(directory / 'gold/example.json', {'raw': 'Ready?', 'target': 'Ready?'})
    with_gold = ev.evaluate(directory, system='b0')
    check('gold without WAV field included with dataset',
          with_gold['suites']['all']['n'] == 4 and with_gold['suites']['gold']['n'] == 1)
    check('gold exact rate and aggregate WER', with_gold['suites']['all']['exact_match_rate'] == .25 and
          with_gold['suites']['all']['wer_words'] == .1 and with_gold['suites']['all']['wer_format'] == .8)
    check('test split plus every gold row', ev.evaluate(directory, split='test')['suites']['all']['n'] == 2)
    check('limit and empty evaluation', ev.evaluate(directory, limit=1)['suites']['all']['n'] == 1 and
          ev.evaluate(directory, limit=0)['suites']['all']['n'] == 0)
    check('nearest-rank p50 p95', ev.percentile(range(1, 21), .5) == 10 and ev.percentile(range(1, 21), .95) == 19)
    check('latency metrics ordered and nonnegative', all(0 <= s['latency_p50_ms'] <= s['latency_p95_ms']
          for s in with_gold['suites'].values()))
    outputs = list((directory / 'eval').glob('*.json'))
    check('eval files private and text-free', len(outputs) >= 10 and all(
          stat.S_IMODE(p.stat().st_mode) == 0o600 and 'hello world' not in p.read_text() for p in outputs))
    check('eval directories private', all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in
          (directory, directory / 'dataset', directory / 'eval', directory / 'gold')))


def record_checks(home):
    from voiceclean import data, record
    recorder = FakeRecorder()
    no_tty_path = home / 'no-tty'
    check('no-TTY refusal before any filesystem or recorder use', raises(
          lambda: record.record_loop(FakeIO(tty=False), recorder, lambda p: '', no_tty_path), RuntimeError)
          and not no_tty_path.exists() and not recorder.paths)
    for stdin_tty, stdout_tty in ((False, True), (True, False), (False, False)):
        stdin, stdout = Mock(), Mock()
        stdin.isatty.return_value, stdout.isatty.return_value = stdin_tty, stdout_tty
        with patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', stdout):
            refused = raises(lambda: record.require_tty(record.TerminalIO()), RuntimeError)
        check(f'TTY pair {int(stdin_tty)} {int(stdout_tty)} refused', refused)
    output = io.StringIO()
    with contextlib.redirect_stderr(output), patch.object(record.TerminalIO, 'isatty', return_value=False):
        status = data.main(['record', '--data-dir', str(no_tty_path)])
    check('CLI clear no-TTY refusal with no files', status == 1 and 'stdin and stdout' in output.getvalue()
          and not no_tty_path.exists())
    terminal = FakeIO(['', '', 'Test the intake.', '', '', '', '', '', 'r',
                       '', None, 'Test the lift.', '', '', 'q'])
    transcripts = iter(['um test the intake', 'skip phrase', 'redo phrase', 'test the intake', 'quit phrase'])
    directory = home / 'recording'
    counts = record.record_loop(terminal, recorder, lambda p: next(transcripts), directory)
    check('full fake session save skip redo quit counts', counts == {'saved': 2, 'skipped': 1, 'redos': 1})
    gold = directory / 'gold'
    rows = [json.loads(p.read_text()) for p in gold.glob('*.json')]
    check('exactly saved WAVs and metadata remain', len(rows) == len(list(gold.glob('*.wav'))) == 2 and
          sum(p.exists() for p in recorder.paths) == 2)
    check('skipped redo and quit WAVs deleted', not any(recorder.paths[i].exists() for i in (1, 2, 4)))
    check('recorder always stopped and input capped', recorder.stops == 5 and
          len([t for _, t in terminal.reads if t == 30]) == 5)
    check('guard compatibility true and false retained', {r['guard_compatible'] for r in rows} == {True, False})
    check('gold schema and UTC timestamps', all(set(r) == {'id', 'wav', 'raw', 'target', 'suggestion', 'created',
          'guard_compatible'} and r['created'].endswith('+00:00') and r['wav'] == r['id'] + '.wav' for r in rows))
    check('redo keeps suggestion', terminal.output[2 * 2] == terminal.output[3 * 2])
    check('gold files and directories private', all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in gold.iterdir())
          and all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in (directory, gold)))
    for phase, answers in [('recording', ['', EOFError()]), ('target', ['', '', EOFError()])]:
        interrupted = FakeRecorder()
        record.record_loop(FakeIO(answers), interrupted, lambda p: 'test', home / phase)
        check('EOF cleans WAV during ' + phase, interrupted.stops == 1 and not interrupted.paths[0].exists())
    broken = FakeRecorder()
    check('transcriber failure cleans clip', raises(lambda: record.record_loop(FakeIO(['', '']), broken,
          lambda p: (_ for _ in ()).throw(RuntimeError('fake failure')), home / 'failure'), RuntimeError)
          and not broken.paths[0].exists())
    # Exercise the real recorder adapter through process/timer fakes only.
    process = Mock()
    process.poll.return_value = None
    with patch.object(record.subprocess, 'Popen', return_value=process) as popen, \
            patch.object(record.threading, 'Timer') as timer:
        backend = record.ParecordRecorder()
        backend.start(home / 'fake.wav')
        backend.stop()
        backend.stop()
    check('parecord adapter flags umask and watchdog', popen.call_args.args[0][:-1] ==
          ['parecord', '--channels=1', '--rate=16000', '--format=s16le', '--file-format=wav'] and
          popen.call_args.kwargs['umask'] == 0o077 and timer.call_args.args[0] == 30)
    check('parecord adapter terminate then wait exactly once', process.terminate.call_count == process.wait.call_count == 1)
    check('real transcriber construction is lazy', callable(record.parakeet_transcriber()))
    # Exercise WAV decoding and backend reuse with a tiny numpy stand-in.
    from voiceclean import roundtrip, storage
    class Samples(list):
        def astype(self, dtype):
            assert dtype == 'float32'
            return self
        def __truediv__(self, divisor):
            return [sample / divisor for sample in self]
    def frombuffer(buffer, dtype):
        assert dtype == '<i2'
        return Samples(struct.unpack('<' + 'h' * (len(buffer) // 2), buffer))
    numpy = types.SimpleNamespace(frombuffer=frombuffer, float32='float32')
    wav = directory / 'conversion.wav'
    os.close(storage.create_private_file(wav))
    with wave.open(str(wav), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(struct.pack('<hhh', -32768, 0, 16384))
    captured = []
    def stt(samples, rate):
        captured.append((samples, rate))
        return 'fake transcript'
    with patch.dict(sys.modules, {'numpy': numpy}), patch.object(roundtrip, 'parakeet_backend', return_value=stt) as factory:
        transcriber = record.parakeet_transcriber('fake-model-dir', 3)
        first, second = transcriber(wav), transcriber(wav)
    check('WAV converted to normalized float32 for reused Parakeet backend',
          first == second == 'fake transcript' and captured == [([-1.0, 0.0, .5], 16000)] * 2
          and factory.call_count == 1 and factory.call_args.args == ('fake-model-dir', 3))


def history_checks(home):
    from voiceclean import data, eval as ev, record, storage
    from jarvis.v2 import dictation_guard as guard, dictation_rules as rules

    directory = home / 'history'
    threads = home / '.local/share/jarvis/v2/threads'
    no_tty_path = home / 'history-no-tty'
    with patch.object(storage, 'data_dir', side_effect=AssertionError('storage accessed')), \
            patch.object(storage, 'safe_input', side_effect=AssertionError('source accessed')):
        check('history refuses no TTY before filesystem access', raises(
              lambda: record.history_loop(FakeIO(tty=False), no_tty_path, threads), RuntimeError))
    for stdin_tty, stdout_tty in ((False, True), (True, False), (False, False)):
        stdin, stdout = Mock(), Mock()
        stdin.isatty.return_value, stdout.isatty.return_value = stdin_tty, stdout_tty
        errors = io.StringIO()
        # Python 3.14's argparse probes the terminal for colour through
        # sys.stdout.fileno(); a Mock has none, so colour is pinned off here.
        with patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', stdout), \
                patch.dict(os.environ, {'PYTHON_COLORS': '0', 'NO_COLOR': '1'}), \
                contextlib.redirect_stderr(errors):
            status = data.main(['gold-from-history', '--data-dir', str(no_tty_path)])
        check(f'history CLI TTY pair {int(stdin_tty)} {int(stdout_tty)} refused',
              status == 1 and 'gold-from-history requires both stdin and stdout' in errors.getvalue()
              and not no_tty_path.exists())

    first, second = 'um test the intake', 'test the lift'
    typed_only, nonowner = 'owner typed message', 'someone else spoke'
    credential = 'use jmcp_abcdefgh12345678'
    def turn(text=None, *, origin='owner', spoken=True, **extra):
        return {'kind': 'user', 'data': {'origin': origin, 'spoken': spoken, **extra,
                                       **({'text': text} if text is not None else {})}}
    # Three spoken owner turns include one credential-shaped turn.
    fixture = [turn(first, typed='wrong fallback text'), turn(typed=second),
               turn(typed_only, spoken=False), turn(nonowner, origin='agent'), turn(credential)]
    storage.write_json(threads / 'one/log.jsonl', fixture, lines=True)
    terminal = FakeIO(['', 'Test the elevator.'])
    counts = record.history_loop(terminal, directory)
    check('history offers only two clean spoken owner turns from fake HOME',
          len(terminal.reads) == 2 and terminal.output[:3] == ['1/2', first, rules.clean(first)]
          and terminal.output[3:6] == ['2/2', second, rules.clean(second)]
          and all(text not in '\n'.join(terminal.output) for text in (typed_only, nonowner, credential,
                                                                    'wrong fallback text')))
    check('history accepts suggestion and saves incompatible correction counts', counts == {
          'saved': 2, 'accepted_as_suggested': 1, 'corrected': 1, 'skipped': 0, 'guard_incompatible': 1})
    gold = directory / 'gold'
    rows = {row['raw']: row for row in (json.loads(p.read_text()) for p in gold.glob('*.json'))}
    check('history accepted and corrected targets retained', rows[first]['target'] == rules.clean(first)
          and rows[first]['suggestion_accepted'] is True and rows[first]['guard_compatible'] is True
          and rows[second]['target'] == 'Test the elevator.' and rows[second]['suggestion_accepted'] is False
          and rows[second]['guard_compatible'] is False)
    check('history incompatible note includes guard reason on one line',
          terminal.output[-2] == 'Saved guard-incompatible target: ' + guard.check(second, rows[second]['target']).reason
          and '\n' not in terminal.output[-2])
    check('history deterministic filenames and JSON schema', len(rows) == 2 and all(
          row['id'] == 'hist-' + hashlib.sha256(raw.encode()).hexdigest()[:16]
          and (gold / (row['id'] + '.json')).is_file()
          and set(row) == {'id', 'raw', 'target', 'source', 'wav', 'suggestion_accepted', 'created', 'guard_compatible'}
          and row['source'] == 'history' and row['wav'] is None and row['created'].endswith('+00:00')
          for raw, row in rows.items()))
    check('history files 0600 and directories 0700',
          all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in gold.iterdir())
          and all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in (directory, gold)))
    check('history final line has counts only and no row text', json.loads(terminal.output[-1]) == counts
          and all(type(n) is int for n in json.loads(terminal.output[-1]).values())
          and all(row[key] not in terminal.output[-1] for row in rows.values() for key in ('raw', 'target')))
    before = {p.name: p.read_bytes() for p in gold.iterdir()}
    resumed = FakeIO()
    resume_counts = record.history_loop(resumed, directory, threads)
    check('history resume offers no saved rows and preserves files', not resumed.reads
          and resume_counts['saved'] == 0 and before == {p.name: p.read_bytes() for p in gold.iterdir()})
    report = ev.evaluate(directory, system='b1')
    check('history gold-only eval counts null-WAV rows including incompatible target',
          report['suites']['gold']['n'] == report['suites']['all']['n'] == 2
          and report['suites']['gold']['exact_match_rate'] == .5)

    skipped_dir = home / 'history-skip'
    skipping = FakeIO(['s', 'q'])
    skip_counts = record.history_loop(skipping, skipped_dir, threads)
    check('history skip and quit save no files', skip_counts == {
          'saved': 0, 'accepted_as_suggested': 0, 'corrected': 0, 'skipped': 1, 'guard_incompatible': 0}
          and not list((skipped_dir / 'gold').iterdir()))
    for ending in (EOFError(), KeyboardInterrupt()):
        interrupted = FakeIO(['', ending])
        end_dir = home / ('history-' + type(ending).__name__)
        ended = record.history_loop(interrupted, end_dir, threads)
        check('history preserves reviewed rows on ' + type(ending).__name__, ended['saved'] == 1
              and len(list((end_dir / 'gold').glob('*.json'))) == 1
              and json.loads(interrupted.output[-1]) == ended)

    # A separate source exercises both scrubber entry points and resuming any gold raw.
    custom_threads = home / 'custom-threads'
    custom_dir = home / 'custom-history'
    storage.write_json(custom_dir / 'gold/recorded.json', {'raw': first, 'target': rules.clean(first)})
    storage.write_json(custom_threads / 'a/log.jsonl', [turn(first), turn('password: hunter2'),
          turn('connect http://user:hunter2@localhost/path'), turn('   '), turn(typed=second),
          turn(second), {'kind': 'assistant', 'data': {'origin': 'owner', 'spoken': True, 'text': nonowner}},
          {'kind': 'user', 'data': None}], lines=True)
    # Historical logs can contain interrupted writes and non-object lines.
    with (custom_threads / 'a/log.jsonl').open('a') as fh:
        fh.write('{broken\n[]\n')
    cli_terminal = FakeIO([''])
    with patch.object(record, 'TerminalIO', return_value=cli_terminal):
        status = data.main(['--data-dir', str(custom_dir), 'gold-from-history',
                            '--threads-dir', str(custom_threads)])
    check('history CLI custom threads filters credentials empties duplicates and existing recording',
          status == 0 and cli_terminal.output == ['1/1', second, rules.clean(second), json.dumps({
              'saved': 1, 'accepted_as_suggested': 1, 'corrected': 0, 'skipped': 0, 'guard_incompatible': 0},
              sort_keys=True)])


def isolation(home):
    code = ("import sys; import voiceclean.data, voiceclean.eval, voiceclean.record; "
            "print(sorted(m for m in sys.modules if m.startswith('jarvis')))")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=ROOT,
                         env={'HOME': str(home), 'PYTHONPATH': str(ROOT), 'PATH': '/usr/bin:/bin'})
    check('subprocess config isolation', out.returncode == 0 and 'jarvis.config' not in out.stdout and
          'jarvis.v2.dictation_guard' in out.stdout and 'jarvis.v2.dictation_rules' in out.stdout)



def fd_leak_checks(home):
    """An adapter writing straight to fds 1 and 2 must print no row text."""
    adapters = home / 'adapters'
    adapters.mkdir()
    (adapters / 'leaky_adapter.py').write_text(
        'import os\n'
        'def f(raw):\n'
        '    os.write(1, ("FDLEAK1 " + raw + "\\n").encode())\n'
        '    os.write(2, ("FDLEAK2 " + raw + "\\n").encode())\n'
        '    print("FDLEAK3 " + raw)\n'
        '    return raw\n')
    data_root = home / 'fd-leak-data'
    data_root.mkdir(mode=0o700)
    (data_root / 'dataset').mkdir(mode=0o700)
    (data_root / 'dataset' / 'dev.jsonl').write_text(json.dumps(
        {'messy': 'um private marker words', 'target': 'Private marker words.',
         'origin': 'public', 'source_id': 'a'}) + '\n')
    env = {'PYTHONPATH': os.pathsep.join([str(ROOT), str(adapters)]), 'HOME': str(home),
           'PATH': '/usr/bin:/bin', 'PYTHON_COLORS': '0'}
    out = subprocess.run([sys.executable, '-m', 'voiceclean.data', 'eval', '--data-dir', str(data_root),
                          '--split', 'dev', '--system', 'leaky_adapter:f'],
                         capture_output=True, text=True, cwd=ROOT, env=env)
    check('adapter fd-level output silenced', out.returncode == 0 and 'FDLEAK' not in out.stdout + out.stderr
          and 'private marker' not in out.stdout + out.stderr)

def main():
    real_home = Path.home()
    sys.meta_path.insert(0, Fence())
    with tempfile.TemporaryDirectory(prefix='voiceclean-eval-check-') as tmp:
        home = Path(tmp) / 'home'
        home.mkdir(mode=0o700)
        original_exists = Path.exists
        outer_git_markers = {p / '.git' for p in Path(tmp).parents}
        fixture_exists = lambda p, *a, **kw: False if p in outer_git_markers else original_exists(p, *a, **kw)
        def audit(event, args):
            if event in ('socket.connect', 'socket.getaddrinfo'):
                raise AssertionError('network attempted')
            if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(args[0])).absolute()
                protected = [real_home / p for p in ('.claude', '.codex', '.config/jarvis', '.local/share/jarvis')]
                if any(path == p or p in path.parents for p in protected) or any(p.startswith('.env') for p in path.parts):
                    raise AssertionError('owner files accessed')
        sys.addaudithook(audit)
        with patch.object(Path, 'exists', fixture_exists), patch.dict(os.environ, {'HOME': str(home), 'VOICECLEAN_DATA_DIR': str(home / 'default')}):
            for fn in (eval_checks, record_checks, history_checks, fd_leak_checks, isolation):
                try:
                    fn(home)
                except Exception as exc:
                    check(fn.__name__ + ' completed (' + type(exc).__name__ + ')', False)
        check('jarvis.config absent', 'jarvis.config' not in sys.modules)
    print(f'voiceclean_eval_check: {PASSED} passed, {FAILED} failed')
    return bool(FAILED)


if __name__ == '__main__':
    raise SystemExit(main())
