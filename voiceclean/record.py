"""Deliberate, interactive owner-only gold recording; no backend loads on import.

Injected IO supplies isatty(), read(prompt, timeout=None), write(text); read
returns None on timeout and raises EOFError on EOF. A recorder supplies
start(path) and stop(); a transcriber is callable with a WAV Path.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import select
import subprocess
import sys
import threading
import uuid
import wave

from jarvis.v2 import dictation_guard as guard
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


def require_tty(io):
    if not io.isatty():
        raise RuntimeError('record requires both stdin and stdout to be TTYs; run it interactively yourself')


class ParecordRecorder:
    """The child inherits umask 077; a watchdog also enforces the hard cap."""
    def __init__(self):
        self.process = self.timer = None
        self.lock = threading.Lock()

    def start(self, path):
        with self.lock:
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
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


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
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32) / 32768.0
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
