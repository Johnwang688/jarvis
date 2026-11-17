"""Interactive owner-only gold recording and history review; no backend loads on import.

Injected IO supplies isatty(), read(prompt, timeout=None), write(text); read
returns None on timeout and raises EOFError on EOF. A recorder supplies
start(path) and stop(); a transcriber is callable with a WAV Path.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import uuid
import wave

from jarvis.v2 import dictation_guard as guard
from jarvis.v2 import dictation_rules as rules, redact
from . import storage

MAX_SECONDS = 30
SUGGESTIONS = (
    'Message a friend about plans.',
    'Ask an AI to fix a failing test.',
    'Make a VEX note with team numbers and measurements.',
    'Change your mind mid-sentence.',
    'Ask a question.',
    'Say a long run-on with several clauses.',
    'Reschedule a meeting.',
    'Describe a bug you just found.',
    'Make a grocery reminder.',
    'Explain how practice went.',
    'Ask someone for directions.',
    'Thank a teammate for their help.',
    'Describe a weekend plan.',
    'Dictate a short email to a teacher.',
    'Ask an AI to explain an error.',
    'Leave a note about a robot mechanism.',
    'List a few tasks for tomorrow.',
    'Compare two design ideas.',
    'Give a time and date for an event.',
    'Mention a price and a quantity.',
    'Use a contraction in a short message.',
    'Start over after a false start.',
    'Use spoken punctuation.',
    'Ask a follow-up question about code.',
    'Write an update about a project.',
    'Dictate a reminder with a name.',
    'Explain a measurement in inches.',
    'Say a team number with a letter.',
    'Describe something that surprised you.',
    'Ask for help with tomorrow\'s schedule.',
)


class TerminalIO:
    def isatty(self):
        return sys.stdin.isatty() and sys.stdout.isatty()

    def write(self, text):
        print(text, flush=True)

    def read(self, prompt, timeout=None):
        print(prompt, end='', flush=True)
        if timeout is not None and not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.rstrip('\r\n')


def require_tty(io, step='record'):
    if not io.isatty():
        raise RuntimeError(f'{step} requires both stdin and stdout to be TTYs; run it interactively yourself')


def history_loop(io, data_dir, threads_dir=None):
    """Review unique, credential-free spoken owner turns, resuming saved gold."""
    require_tty(io, 'gold-from-history')
    from .owner_text import jsonl

    directory = storage.data_dir(data_dir) / 'gold'
    storage.data_dir(directory)
    seen = set()
    for path in sorted(directory.glob('*.json')):
        with storage.safe_input(path).open(encoding='utf-8') as fh:
            raw = json.load(fh).get('raw')
            if isinstance(raw, str):
                seen.add(raw)
    threads = storage.safe_input(threads_dir or Path.home() / '.local/share/jarvis/v2/threads')
    items = []
    for path in sorted(threads.glob('*/log.jsonl')):
        for row in jsonl(path):
            data = row.get('data')
            if (row.get('kind') != 'user' or not isinstance(data, dict)
                    or data.get('origin') != 'owner' or data.get('spoken') is not True):
                continue
            raw = data.get('text') or data.get('typed')
            if not isinstance(raw, str) or not raw.strip() or raw in seen:
                continue
            try:
                scrubbed = redact.labelled_values(redact.shapes(raw, mode=redact.DISPLAY))
            except Exception:
                continue  # Fail closed, as owner_text does, without displaying the row.
            if scrubbed != raw:
                continue
            seen.add(raw)
            items.append(raw)

    counts = {'saved': 0, 'accepted_as_suggested': 0, 'corrected': 0,
              'skipped': 0, 'guard_incompatible': 0}
    try:
        for index, raw in enumerate(items, 1):
            suggestion = rules.clean(raw)
            io.write(f'{index}/{len(items)}')
            io.write(raw)
            io.write(suggestion)
            answer = io.read('Target (Enter = suggestion, s = skip, q = quit): ')
            if answer == 'q':
                break
            if answer == 's':
                counts['skipped'] += 1
                continue
            accepted = answer == ''
            target = suggestion if accepted else answer
            verdict = guard.check(raw, target)
            ident = 'hist-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
            storage.write_json(directory / f'{ident}.json', {
                'id': ident, 'raw': raw, 'target': target, 'source': 'history',
                'wav': None, 'suggestion_accepted': accepted,
                'created': datetime.now(timezone.utc).isoformat(),
                'guard_compatible': verdict.ok})
            counts['saved'] += 1
            counts['accepted_as_suggested' if accepted else 'corrected'] += 1
            if not verdict.ok:
                counts['guard_incompatible'] += 1
                io.write(f'Saved guard-incompatible target: {verdict.reason}')
    except (EOFError, KeyboardInterrupt):
        pass
    io.write(json.dumps(counts, sort_keys=True))
    return counts


class ParecordRecorder:
    """The child inherits umask 077; a watchdog also enforces the hard cap."""
    def __init__(self):
        self.process = self.timer = self.path = None
        self.lock = threading.Lock()

    def start(self, path):
        with self.lock:
            self.path = path
            self.process = subprocess.Popen(
                ['parecord', '--channels=1', '--rate=16000', '--format=s16le',
                 '--file-format=wav', str(path)], umask=0o077,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.timer = threading.Timer(MAX_SECONDS, self.stop)
            self.timer.daemon = True
            self.timer.start()

    def stop(self):
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            if self.process is not None:
                process, self.process = self.process, None
                if process.poll() is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass  # It exited between poll() and terminate().
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if self.path is not None:
                path, self.path = self.path, None
                truncate_wav(path, MAX_SECONDS)


def truncate_wav(path, seconds):
    """Keep at most `seconds` of a saved WAV, so the hard cap holds for the
    file on disk too, even when a recorder outlived its stop (Sol delta review
    of #392). Rewritten in place: the file keeps its 0600 mode."""
    try:
        with wave.open(str(path), 'rb') as wav:
            params = wav.getparams()
            limit = seconds * wav.getframerate()
            if wav.getnframes() <= limit:
                return
            frames = wav.readframes(limit)
    except (OSError, EOFError, wave.Error):
        return
    with open(path, 'r+b') as fh, wave.open(fh, 'wb') as out:
        fh.truncate(0)
        out.setparams(params)
        out.writeframes(frames)


def parakeet_transcriber(model_dir=None, threads=2):
    """Load Parakeet lazily, only after the owner actually records a clip."""
    backend = None

    def transcribe(path):
        nonlocal backend
        import numpy as np
        from .roundtrip import parakeet_backend
        with wave.open(str(path), 'rb') as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
                raise ValueError('recording must be mono 16-bit PCM at 16000 Hz')
            # Never more than the cap, even if a recorder outlived its stop.
            frames = min(wav.getnframes(), MAX_SECONDS * 16000)
            samples = np.frombuffer(wav.readframes(frames), dtype='<i2').astype(np.float32) / 32768.0
            rate = wav.getframerate()
        if backend is None:
            backend = parakeet_backend(model_dir, threads)
        return backend(samples, rate)
    return transcribe


def record_loop(io, recorder, transcriber, data_dir):
    """Save reviewed clips only. Skips, redos, EOF and errors remove the WAV."""
    require_tty(io)
    directory = storage.data_dir(data_dir) / 'gold'
    storage.data_dir(directory)
    counts = {'saved': 0, 'skipped': 0, 'redos': 0}
    index = 0
    try:
        while True:
            suggestion = SUGGESTIONS[index % len(SUGGESTIONS)]
            io.write(suggestion)
            if io.read('Enter to record, q to quit: ') == 'q':
                break
            ident = uuid.uuid4().hex
            wav, metadata = directory / f'{ident}.wav', directory / f'{ident}.json'
            saved = False
            try:
                os.close(storage.create_private_file(wav))
                try:
                    recorder.start(wav)
                    io.read('Recording: Enter to stop (30 seconds maximum): ', timeout=MAX_SECONDS)
                finally:
                    recorder.stop()
                raw = transcriber(wav)
                if not isinstance(raw, str):
                    raise ValueError('transcriber must return text')
                io.write(raw)
                target = io.read('Clean target (empty = skip, r = redo, q = quit): ')
                if target == 'q':
                    break
                if target == 'r':
                    counts['redos'] += 1
                    continue
                if not target.strip():
                    counts['skipped'] += 1
                else:
                    storage.write_json(metadata, {'id': ident, 'wav': wav.name, 'raw': raw,
                        'target': target, 'suggestion': suggestion,
                        'created': datetime.now(timezone.utc).isoformat(),
                        'guard_compatible': guard.check(raw, target).ok})
                    saved = True
                    counts['saved'] += 1
                index += 1
            finally:
                if not saved:
                    wav.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
    except (EOFError, KeyboardInterrupt):
        pass
    io.write(json.dumps(counts, sort_keys=True))
    return counts
