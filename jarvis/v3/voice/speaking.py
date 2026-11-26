"""Prepare sentences ahead of a PCM sink, and hand their audio out in order."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from itertools import islice
from operator import index
from threading import Condition, Thread, current_thread
from typing import Hashable

from .engines import Voice

CHUNK_MS = 200
PREPARED_AHEAD = 2
LOG = logging.getLogger(__name__)


@dataclass(eq=False)
class _Sentence:
    id: Hashable
    text: str
    queued_at: float
    audio: tuple | None = None
    ready: bool = False


class Speaking:
    """One synthesis worker, one ordered audio delivery worker.

    ``on_audio(id, pcm, rate, first, last)`` receives one chunk at a time.
    The sink may exert backpressure by holding the callback until it can take
    another chunk. ``stop()`` waits for an audio callback on the delivery
    thread to finish. A callback can also stop its own remaining delivery.
    A chunk already handed to the sink belongs to the caller. Failed sentences
    are recorded in ``failures()`` and optionally reported through ``on_error``.
    """

    def __init__(self, voice: Voice, *, on_audio, on_done, clock=time.monotonic, on_error=None,
                 keep_history=True):
        self._voice = voice
        self._on_audio = on_audio
        self._on_done = on_done
        self._on_error = on_error
        self._clock = clock
        self._changed = Condition()
        self._handing_off = None
        self._queue = deque()
        self._closed = False
        self._keep_history = keep_history
        self._first_audio_ms = {}
        self._failed = []
        self._workers = (Thread(target=self._prepare, name="voice-synthesize", daemon=True),
                         Thread(target=self._deliver, name="voice-audio", daemon=True))
        for worker in self._workers:
            worker.start()

    def say(self, id: Hashable, text: str) -> None:
        queued_at = self._clock()
        with self._changed:
            if self._closed:
                raise RuntimeError("speaking is closed")
            self._queue.append(_Sentence(id, text, queued_at))
            self._changed.notify_all()

    def stop(self, *, wait=True) -> list[Hashable]:
        """Return the IDs dropped from the current handoff, synthesis and queue."""
        with self._changed:
            dropped = [sentence.id for sentence in self._queue]
            handing_off = self._handing_off
            self._queue.clear()
            self._changed.notify_all()
            if wait and handing_off is not None and current_thread() is not self._workers[1]:
                self._changed.wait_for(lambda: self._handing_off is not handing_off)
        return dropped

    def timings(self) -> dict[Hashable, float]:
        """Elapsed milliseconds from say() to the first audio handed out for each ID."""
        with self._changed:
            return self._first_audio_ms.copy()

    def failures(self) -> tuple[Hashable, ...]:
        with self._changed:
            return tuple(self._failed)

    def _next_unprepared(self):
        return next((item for item in islice(self._queue, PREPARED_AHEAD + 1) if not item.ready), None)

    def _prepare(self):
        import numpy as np

        try:
            while True:
                with self._changed:
                    self._changed.wait_for(lambda: self._closed or self._next_unprepared() is not None)
                    if self._closed:
                        return
                    sentence = self._next_unprepared()
                try:
                    samples, rate = self._voice.synthesize(sentence.text)
                    samples = np.asarray(samples)
                    rate = index(rate)
                    if samples.ndim != 1 or samples.dtype != np.int16 or not len(samples) or rate < 5:
                        raise ValueError("expected nonempty int16 mono PCM and a sample rate")
                    audio = samples, rate
                except Exception:
                    audio = None
                with self._changed:
                    if sentence in self._queue:
                        sentence.audio = audio
                        sentence.ready = True
                        self._changed.notify_all()
        finally:
            close = getattr(self._voice, "close", None)
            if close is not None:
                close()

    def _deliver(self):
        while True:
            with self._changed:
                self._changed.wait_for(lambda: self._closed or (self._queue and self._queue[0].ready))
                if self._closed:
                    return
                sentence = self._queue[0]
            success = sentence.audio is not None and self._play(sentence)
            with self._changed:
                if not self._queue or self._queue[0] is not sentence:
                    continue  # stop() removed it, possibly during a callback
                self._queue.popleft()
                if not success and self._keep_history:
                    self._failed.append(sentence.id)
                self._changed.notify_all()
            callback = self._on_done if success else self._on_error
            if callback is not None:
                try:
                    callback(sentence.id)
                except Exception:
                    LOG.warning("A speaking completion callback failed.")

    def _play(self, sentence):
        samples, rate = sentence.audio
        chunk_samples = rate * CHUNK_MS // 1000
        for offset in range(0, len(samples), chunk_samples):
            try:
                chunk = samples[offset:offset + chunk_samples]
            except Exception:
                return False
            with self._changed:
                if not self._queue or self._queue[0] is not sentence:
                    return False
                if offset == 0 and self._keep_history:
                    self._first_audio_ms[sentence.id] = (self._clock() - sentence.queued_at) * 1000
                self._handing_off = sentence
            try:
                self._on_audio(sentence.id, chunk, rate, offset == 0, offset + len(chunk) == len(samples))
            except Exception:
                return False
            finally:
                with self._changed:
                    self._handing_off = None
                    self._changed.notify_all()
        return True

    def close(self) -> None:
        """Discard outstanding sentences and release the voice after native work returns."""
        with self._changed:
            self._closed = True
            self._queue.clear()
            self._changed.notify_all()

    def join(self) -> None:
        for worker in self._workers:
            if worker is not current_thread():
                worker.join()
