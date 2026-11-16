"""VA-3a free eval/record checks: fake HOME, TTY, audio and models only."""
from __future__ import annotations

import contextlib
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
    check('gold included with dataset', with_gold['suites']['all']['n'] == 4 and with_gold['suites']['gold']['n'] == 1)
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


def isolation(home):
    code = ("import sys; import voiceclean.data, voiceclean.eval, voiceclean.record; "
            "print(sorted(m for m in sys.modules if m.startswith('jarvis')))")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=ROOT,
                         env={'HOME': str(home), 'PYTHONPATH': str(ROOT), 'PATH': '/usr/bin:/bin'})
    check('subprocess config isolation', out.returncode == 0 and 'jarvis.config' not in out.stdout and
          'jarvis.v2.dictation_guard' in out.stdout and 'jarvis.v2.dictation_rules' in out.stdout)


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
            for fn in (eval_checks, record_checks, isolation):
                try:
                    fn(home)
                except Exception as exc:
                    check(fn.__name__ + ' completed (' + type(exc).__name__ + ')', False)
        check('jarvis.config absent', 'jarvis.config' not in sys.modules)
    print(f'voiceclean_eval_check: {PASSED} passed, {FAILED} failed')
    return bool(FAILED)


if __name__ == '__main__':
    raise SystemExit(main())
