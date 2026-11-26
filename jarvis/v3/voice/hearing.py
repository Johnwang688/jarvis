"""Turn a stream of mono PCM frames into utterances and speculative text."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Condition, Lock, RLock, Thread
from typing import Protocol

from jarvis import config

from .engines import SAMPLE_RATE, Transcriber, _float_pcm

FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
START_SPEECH_MS = 150
START_WINDOW_MS = 200
PRE_ROLL_MS = 300
PARTIAL_MS = 500
HEAD_START_MS = 200
MAX_UTTERANCE_MS = 30_000
MAX_PENDING_FINALS = 3
END_SILENCE_MS = config.V3_END_SILENCE_MS
LOG = logging.getLogger(__name__)
_SILERO_IDLE = "energy fallback (Silero not started)"


class Detector(Protocol):
    def is_speech(self, frame) -> float: ...


class EnergyVad:
    """Track the ambient RMS of frames below the current speech threshold."""

    def __init__(self):
        self._noise = 0.003

    def is_speech(self, frame) -> float:
        import numpy as np

        rms = float(np.std(frame))
        threshold = max(0.008, 3 * self._noise)
        if rms < threshold:
            self._noise += 0.05 * (rms - self._noise)
        return min(1.0, rms / (2 * threshold))


class SileroVad:
    """Score contiguous 512-sample windows, holding the previous score between them."""

    _MODEL = "silero_vad_v5.1.2.onnx"
    _SIZE = 2_327_524
    _SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"

    def __init__(self):
        self._energy = EnergyVad()
        self._load_lock = Lock()
        self._loader = None
        self._session = None
        self._status = _SILERO_IDLE

    def warm(self) -> None:
        """Start loading the local model in the background once."""
        with self._load_lock:
            if self._loader is None:
                self._status = "energy fallback (Silero loading)"
                self._loader = Thread(target=self._initialize, name="voice-vad-load", daemon=True)
                self._loader.start()

    def status(self) -> str:
        with self._load_lock:
            return self._status

    def _initialize(self) -> None:
        try:
            session = self._load_session()
        except Exception:
            session = None
        with self._load_lock:
            self._session = session
            self._status = "energy fallback (Silero unavailable)" if session is None else "Silero VAD"

    def _load_session(self):
        import numpy as np
        import onnxruntime as ort

        model = Path(__file__).with_name(self._MODEL)
        if not model.is_file():
            model = config.REPO_ROOT / "hud" / "vendor" / "silero-vad" / self._MODEL
        with model.open("rb") as source:
            data = source.read(self._SIZE + 1)
        if len(data) != self._SIZE or hashlib.sha256(data).hexdigest() != self._SHA256:
            raise ValueError("Silero model failed its integrity check")

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(data, sess_options=options, providers=["CPUExecutionProvider"])
        self._rate = np.array(SAMPLE_RATE, dtype=np.int64)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(64, dtype=np.float32)
        self._pending = np.empty(0, dtype=np.float32)
        self._probability = 0.0
        return session

    def is_speech(self, frame) -> float:
        self.warm()
        with self._load_lock:
            session = self._session
        if session is None:
            return self._energy.is_speech(frame)

        import numpy as np

        self._pending = np.concatenate((self._pending, frame))
        if len(self._pending) >= 512:
            window, self._pending = self._pending[:512], self._pending[512:]
            audio = np.concatenate((self._context, window))[None, :]
            probability, self._state = session.run(
                ["output", "stateN"], {"input": audio, "state": self._state, "sr": self._rate},
            )
            self._context = window[-64:]
            self._probability = float(probability.item())
        return self._probability


@dataclass
class _Result:
    text: str
    decode_ms: float


@dataclass(eq=False)
class _Utterance:
    frames: list
    pre_roll_frames: int
    voice_end: int
    previous_speech: bool = True
    ended_at: float | None = None
    head_attempted: int = 0
    head_result: _Result | None = None

    @property
    def silence_ms(self) -> int:
        return (len(self.frames) - self.voice_end) * FRAME_MS

    @property
    def decode_end(self) -> int:
        return min(len(self.frames), self.voice_end + HEAD_START_MS // FRAME_MS)


@dataclass
class _Decode:
    kind: str
    utterance: _Utterance
    frames: tuple = field(init=False)
    voice_end: int = field(init=False)

    def __post_init__(self):
        self.voice_end = self.utterance.voice_end
        end = len(self.utterance.frames) if self.kind == "partial" else self.utterance.decode_end
        self.frames = tuple(self.utterance.frames[:end])


class Hearing:
    """Feed 320 samples at a time, from one capture stream.

    Callbacks take ``on_event(kind, **fields)`` and run in order on a separate
    thread. Endpoint timing uses frame counts. ``clock`` supplies monotonic
    seconds for decode duration and time from endpoint to final.
    """

    def __init__(self, transcriber: Transcriber, *, names=(), on_event, clock=time.monotonic,
                 detector: Detector | None = None, end_silence_ms: int | None = None):
        self._transcriber = transcriber
        self._names = tuple(names)
        self._on_event = on_event
        self._clock = clock
        self._detector = detector
        cutoff = END_SILENCE_MS if end_silence_ms is None else end_silence_ms
        self._end_silence_ms = max(300, min(1500, cutoff))
        self._changed = Condition()
        self._callback = RLock()
        self._closed = False
        self._before = deque(maxlen=PRE_ROLL_MS // FRAME_MS)
        self._onset = deque()
        self._active = None
        self._ended = deque()
        self._events = deque()
        self._job = None
        Thread(target=self._decode, name="voice-decode", daemon=True).start()
        Thread(target=self._notify, name="voice-heard-events", daemon=True).start()

    def speech_active(self) -> bool:
        with self._changed:
            return self._active is not None

    def _get_detector(self) -> Detector:
        if self._detector is None:
            self._detector = SileroVad()
        return self._detector

    def warm(self) -> None:
        """Optionally start the detector before capture begins."""
        with self._changed:
            if not self._closed:
                warm = getattr(self._get_detector(), "warm", None)
                if warm is not None:
                    warm()

    def detector_status(self) -> str:
        with self._changed:
            if self._detector is None:
                return _SILERO_IDLE
            status = getattr(self._detector, "status", None)
            return status() if status is not None else "custom detector"

    def feed(self, frame) -> None:
        with self._changed:
            if self._closed:
                return
            frame = _float_pcm(frame).copy()  # capture may reuse its buffer
            if len(frame) != FRAME_SAMPLES:
                raise ValueError("expected one 20 ms PCM frame")
            speech = self._get_detector().is_speech(frame) >= 0.5
            utterance = self._active
            if utterance is None:
                self._listen_for_onset(frame, speech)
                return

            utterance.frames.append(frame)
            if speech and utterance.previous_speech:
                utterance.voice_end = len(utterance.frames)
                utterance.head_result = None
            utterance.previous_speech = speech
            if (utterance.silence_ms >= self._end_silence_ms
                    or len(utterance.frames) * FRAME_MS >= MAX_UTTERANCE_MS):
                utterance.ended_at = self._clock()
                self._active = None
                self._before.extend(utterance.frames[utterance.voice_end:])
                del utterance.frames[utterance.decode_end:]
                if len(self._ended) == MAX_PENDING_FINALS:
                    # The oldest final may be decoding already. Its late result is ignored.
                    self._final(_Result("", 0), dropped=True)
                self._ended.append(utterance)
                self._schedule()
                return

            self._schedule()
            elapsed_ms = (len(utterance.frames) - utterance.pre_roll_frames) * FRAME_MS
            if speech and elapsed_ms % PARTIAL_MS == 0 and self._job is None:
                self._submit("partial", utterance)

    def _listen_for_onset(self, frame, speech):
        if len(self._onset) == START_WINDOW_MS // FRAME_MS:
            self._before.append(self._onset.popleft()[0])
        self._onset.append((frame, speech))
        if sum(detected for _, detected in self._onset) * FRAME_MS < START_SPEECH_MS:
            return

        # Keep pre-roll measured from the first detected speech in the window.
        while not self._onset[0][1]:
            self._before.append(self._onset.popleft()[0])
        frames = [*self._before, *(frame for frame, _ in self._onset)]
        self._active = _Utterance(frames, len(self._before), len(frames))
        self._before.clear()
        self._onset.clear()
        self._emit("speech_start")

    def _submit(self, kind, utterance):
        self._job = _Decode(kind, utterance)
        self._changed.notify_all()

    def _schedule(self):
        while self._ended:
            first = self._ended[0]
            if first.head_result is not None:
                self._final(first.head_result)
            else:
                if self._job is None:
                    self._submit("final", first)
                return
        active = self._active
        if (self._job is None and active is not None and active.silence_ms >= HEAD_START_MS
                and active.head_attempted != active.voice_end):
            active.head_attempted = active.voice_end
            self._submit("head", active)

    def _decode(self):
        try:
            while True:
                with self._changed:
                    self._changed.wait_for(lambda: self._closed or self._job is not None)
                    if self._closed:
                        return
                    job = self._job
                started = self._clock()
                try:
                    import numpy as np

                    text = self._transcriber.transcribe(np.concatenate(job.frames), names=self._names)
                    result = _Result(text, (self._clock() - started) * 1000)
                except Exception:
                    LOG.warning("A voice decode failed.")
                    result = None
                with self._changed:
                    self._job = None
                    if self._closed:
                        return
                    utterance = job.utterance
                    if job.kind == "partial" and utterance is self._active and result and result.text:
                        self._emit("partial", text=result.text)
                    elif (job.kind == "head" and job.voice_end == utterance.voice_end
                          and (utterance is self._active or utterance in self._ended)):
                        utterance.head_result = result
                    elif job.kind == "final" and self._ended and self._ended[0] is utterance:
                        self._final(result or _Result("", (self._clock() - started) * 1000))
                    self._schedule()
                    self._changed.notify_all()
                del job  # Release its PCM snapshot before waiting for another decode.
        finally:
            close = getattr(self._transcriber, "close", None)
            if close is not None:
                close()

    def _final(self, result, *, dropped=False):
        utterance = self._ended.popleft()
        audio_ms = len(utterance.frames) * FRAME_MS
        utterance.frames.clear()
        fields = {"dropped": True} if dropped else {}
        self._emit("final", text=result.text, audio_ms=audio_ms, decode_ms=result.decode_ms,
                   waited_ms=(self._clock() - utterance.ended_at) * 1000, **fields)
        self._emit("speech_end")

    def _emit(self, kind, **fields):
        self._events.append((kind, fields))
        self._changed.notify_all()

    def _notify(self):
        while True:
            with self._changed:
                self._changed.wait_for(lambda: self._closed or self._events)
            with self._callback:
                with self._changed:
                    if self._closed:
                        return
                    kind, fields = self._events.popleft()
                try:
                    self._on_event(kind, **fields)
                except Exception:
                    LOG.warning("A hearing event callback failed.")

    def close(self) -> None:
        """Suppress new events. Any in-flight native decode releases the engine on return."""
        with self._changed:
            self._closed = True
            self._active = None
            self._before.clear()
            self._onset.clear()
            self._ended.clear()
            self._events.clear()
            self._job = None
            self._changed.notify_all()
        with self._callback:
            pass  # A callback already entered may finish before close returns.
