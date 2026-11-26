"""Turn a stream of mono PCM frames into utterances and speculative text."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Condition, Lock, Thread, current_thread
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

    def reset(self) -> None:
        with self._load_lock:
            if self._session is not None:
                self._state.fill(0)
                self._context.fill(0)
                self._pending = self._pending[:0]
                self._probability = 0.0

    def join(self) -> None:
        if self._loader is not None:
            self._loader.join()

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
    failed: bool = False
    uncancelled_echo: bool = False


@dataclass(frozen=True)
class _Frame:
    cancelled: object
    raw: object
    at: float
    context: object
    echo: bool
    has_canceller: bool


@dataclass(eq=False, frozen=True)
class Capture:
    started_at: float
    context: object
    power: float
    keyed: bool


@dataclass(eq=False)
class _Utterance:
    frames: list
    pre_roll_frames: int
    voice_end: int
    capture: Capture | None = None
    keyed_during_capture: bool = False
    echo_start: int | None = None
    speech_ended_at: float = 0
    silence_waited_ms: int = 0
    key_power: list = field(default_factory=list)
    names: tuple | None = None
    previous_speech: bool = True
    ended_at: float | None = None
    head_attempted: int = 0
    head_result: _Result | None = None
    speech_onset: _Frame | None = None

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
    prefer_cancelled: bool
    frames: tuple = field(init=False)
    voice_end: int = field(init=False)
    echo_start: int | None = field(init=False)
    uncancelled_echo: bool = field(init=False)

    def __post_init__(self):
        self.voice_end = self.utterance.voice_end
        self.echo_start = self.utterance.echo_start
        end = len(self.utterance.frames) if self.kind == "partial" else self.utterance.decode_end
        frames = self.utterance.frames[:end]
        # An answer recorded with a canceller may include unpaired frames at a stream change.
        # Keep those raw samples out of the answer decode too.
        paired_only = self.prefer_cancelled and any(frame.has_canceller for frame in frames)
        audio = []
        self.uncancelled_echo = False
        for i, frame in enumerate(frames):
            echo = frame.echo or (self.echo_start is not None and i >= self.echo_start)
            if echo and not frame.has_canceller and i >= self.utterance.pre_roll_frames:
                self.uncancelled_echo = True
            if not paired_only or frame.has_canceller:
                audio.append(frame.cancelled if echo or paired_only else frame.raw)
        self.frames = tuple(audio)


class Hearing:
    """Feed 320 samples at a time, from one capture stream.

    Callbacks take ``on_event(kind, **fields)`` and run in order on a separate
    thread. Endpoint timing uses frame counts. ``clock`` supplies monotonic
    seconds for decode duration and time from endpoint to final. A callable
    ``names`` receives the capture details once per utterance.
    """

    def __init__(self, transcriber: Transcriber, *, names=(), on_event, clock=time.monotonic,
                 detector: Detector | None = None, end_silence_ms: int | None = None,
                 capture_details: bool = False, prefer_cancelled=None):
        self._transcriber = transcriber
        self._names = names if callable(names) else tuple(names)
        self._on_event = on_event
        self._clock = clock
        self._detector = detector
        self._capture_details = capture_details
        self._prefer_cancelled = prefer_cancelled
        cutoff = END_SILENCE_MS if end_silence_ms is None else end_silence_ms
        self._end_silence_ms = max(300, min(1500, cutoff))
        self._changed = Condition()
        self._notifying = False
        self._closed = False
        self._before = deque(maxlen=PRE_ROLL_MS // FRAME_MS)
        self._onset = deque()
        self._active = None
        self._keyed = False
        self._key_down = False
        self._ended = deque()
        self._events = deque()
        self._job = None
        self._workers = (Thread(target=self._decode, name="voice-decode", daemon=True),
                         Thread(target=self._notify, name="voice-heard-events", daemon=True))
        for worker in self._workers:
            worker.start()

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

    def feed(self, frame, *, raw=None, echo=False, context=None, at=None) -> None:
        """Score the cancelled frame and retain the matching samples for decode."""
        with self._changed:
            if self._closed:
                return
            at = self._clock() if at is None else at
            frame = _float_pcm(frame).copy()  # capture may reuse its buffer
            has_canceller = raw is not None
            raw = _float_pcm(raw).copy() if has_canceller else frame
            if len(frame) != FRAME_SAMPLES or len(raw) != FRAME_SAMPLES:
                raise ValueError("expected one 20 ms PCM frame")
            pair = _Frame(frame, raw, at, context, echo, has_canceller)
            speech = self._get_detector().is_speech(frame) >= 0.5
            if self._key_down and not self._keyed:
                if self._active is None and not self._pending_onset():
                    self._start_key(context)
                elif self._active is not None and speech:
                    # Silence under the key must leave the ongoing wake capture intact.
                    self._keyed = self._active.keyed_during_capture = True
                    self._active.head_result = None
            if self._keyed:
                utterance = self._active
                self._append(utterance, pair)
                if speech:
                    if utterance.speech_onset is None:
                        utterance.speech_onset = pair
                    if len(utterance.key_power) < START_WINDOW_MS // FRAME_MS:
                        utterance.key_power.append(self._power(frame))
                utterance.voice_end = len(utterance.frames)
                if utterance.voice_end * FRAME_MS % PARTIAL_MS == 0 and self._job is None:
                    self._submit("partial", utterance)
                return
            utterance = self._active
            if utterance is None:
                self._listen_for_onset(pair, speech)
                return

            self._append(utterance, pair)
            if speech and utterance.previous_speech:
                utterance.voice_end = len(utterance.frames)
                utterance.speech_ended_at = pair.at
                utterance.head_result = None
            utterance.previous_speech = speech
            if (utterance.silence_ms >= self._end_silence_ms
                    or len(utterance.frames) * FRAME_MS >= MAX_UTTERANCE_MS):
                self._before.extend(utterance.frames[utterance.voice_end:])
                self._end(utterance)
                return

            self._schedule()
            elapsed_ms = (len(utterance.frames) - utterance.pre_roll_frames) * FRAME_MS
            if speech and elapsed_ms % PARTIAL_MS == 0 and self._job is None:
                self._submit("partial", utterance)

    @staticmethod
    def _append(utterance, frame):
        if frame.echo and utterance.echo_start is None:
            utterance.echo_start = len(utterance.frames)
            utterance.head_result = None
            utterance.head_attempted = 0
        utterance.frames.append(frame)

    def key(self, down: bool, *, context=None) -> None:
        with self._changed:
            if self._closed or self._key_down == down:
                return
            self._key_down = down
            if down:
                if self._active is None and not self._pending_onset():
                    self._start_key(context)
            elif self._keyed:
                self._keyed = False
                utterance = self._active
                utterance.speech_ended_at = self._clock()
                if utterance.frames:
                    self._end(utterance)
                else:
                    self._active = None
                    self._emit("speech_end", **self._details(utterance))

    def _pending_onset(self):
        return any(speech for _, speech in self._onset)

    def _start_key(self, context):
        self._before.clear()
        self._onset.clear()
        self._keyed = True
        capture = Capture(self._clock(), context, 0, keyed=True)
        self._active = _Utterance([], 0, 0, capture)
        self._emit("speech_start", **self._details(self._active))

    def _end(self, utterance):
        utterance.ended_at = self._clock()
        utterance.silence_waited_ms = utterance.silence_ms
        self._active = None
        del utterance.frames[utterance.decode_end:]
        if len(self._ended) == MAX_PENDING_FINALS:
            # The oldest final may be decoding already. Its late result is ignored.
            self._final(_Result("", 0), dropped=True)
        self._ended.append(utterance)
        self._schedule()

    def _listen_for_onset(self, frame, speech):
        if len(self._onset) == START_WINDOW_MS // FRAME_MS:
            self._before.append(self._onset.popleft()[0])
        self._onset.append((frame, speech))
        if sum(detected for _, detected in self._onset) * FRAME_MS < START_SPEECH_MS:
            return

        # Keep pre-roll measured from the first detected speech in the window.
        while not self._onset[0][1]:
            self._before.append(self._onset.popleft()[0])
        first = self._onset[0][0]
        frames = [*self._before, *(frame for frame, _ in self._onset)]
        capture = None
        if self._capture_details:
            voiced = [frame.cancelled for frame, speech in self._onset if speech]
            power = sum(map(self._power, voiced)) / len(voiced)
            capture = Capture(first.at, first.context, power, keyed=False)
        before = len(self._before)
        echo_start = next((i for i in range(before, len(frames)) if frames[i].echo), None)
        self._active = _Utterance(frames, before, len(frames), capture, echo_start=echo_start,
                                  speech_ended_at=frame.at, speech_onset=first)
        if self._key_down:
            self._keyed = self._active.keyed_during_capture = True
            self._active.key_power.append(self._power(frame.cancelled))
        self._before.clear()
        self._onset.clear()
        self._emit("speech_start", **self._details(self._active))

    def _details(self, utterance):
        if not self._capture_details:
            return {}
        return {"capture": utterance.capture, "keyed": utterance.capture.keyed or utterance.keyed_during_capture}

    @staticmethod
    def _power(frame):
        import numpy as np

        return float(np.var(frame, dtype=np.float64))

    def _submit(self, kind, utterance):
        prefer_cancelled = False
        if self._prefer_cancelled is not None:
            onset = utterance.speech_onset
            context = onset.context if onset is not None else utterance.capture.context
            prefer_cancelled = self._prefer_cancelled(context)
        self._job = _Decode(kind, utterance, prefer_cancelled)
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
        if (self._job is None and active is not None and not self._keyed and active.silence_ms >= HEAD_START_MS
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

                    if job.utterance.names is None:
                        names = self._names(job.utterance.capture) if callable(self._names) else self._names
                        job.utterance.names = tuple(names)
                    text = self._transcriber.transcribe(np.concatenate(job.frames), names=job.utterance.names)
                    result = _Result(text, (self._clock() - started) * 1000,
                                     uncancelled_echo=job.uncancelled_echo)
                    del text
                except Exception:
                    LOG.warning("A voice decode failed.")
                    result = _Result("", (self._clock() - started) * 1000, failed=True)
                with self._changed:
                    if self._closed:
                        return
                    if self._job is not job:
                        del job, result
                        continue  # The capture link was reset during decode.
                    self._job = None
                    utterance = job.utterance
                    current_stream = utterance.echo_start == job.echo_start
                    if (job.kind == "partial" and utterance is self._active and current_stream
                            and result.text):
                        details = self._details(utterance)
                        if details:
                            details["uncancelled_echo"] = job.uncancelled_echo
                        self._emit("partial", text=result.text, **details)
                    elif (job.kind == "head" and job.voice_end == utterance.voice_end
                          and current_stream and (utterance is self._active or utterance in self._ended)):
                        utterance.head_result = None if result.failed else result
                    elif job.kind == "final" and self._ended and self._ended[0] is utterance:
                        self._final(result)
                    self._schedule()
                    self._changed.notify_all()
                del job, result  # Release the PCM snapshot and transcript before waiting.
        finally:
            close = getattr(self._transcriber, "close", None)
            if close is not None:
                close()

    def _final(self, result, *, dropped=False):
        utterance = self._ended.popleft()
        audio_ms = len(utterance.frames) * FRAME_MS
        utterance.head_result = None
        fields = {"dropped": True} if dropped else {}
        details = self._details(utterance)
        if details:
            fields.update(details, silence_ms=utterance.silence_waited_ms,
                          speech_ended_at=utterance.speech_ended_at, failed=result.failed,
                          uncancelled_echo=result.uncancelled_echo,
                          speech_onset=self._answer_onset(utterance))
            if utterance.key_power:
                fields["power"] = sum(utterance.key_power) / len(utterance.key_power)
        utterance.frames.clear()
        self._emit("final", text=result.text, audio_ms=audio_ms, decode_ms=result.decode_ms,
                   waited_ms=(self._clock() - utterance.ended_at) * 1000, **fields)
        self._emit("speech_end", **details)

    @staticmethod
    def _answer_onset(utterance):
        onset = utterance.speech_onset
        if (onset is None or not onset.has_canceller) and any(frame.has_canceller for frame in utterance.frames):
            return None
        if onset is not None:
            return onset.at, onset.context
        # Push-to-talk still works without VAD confirmation when there is no canceller.
        if utterance.capture.keyed:
            return utterance.capture.started_at, utterance.capture.context
        return None

    def _emit(self, kind, **fields):
        self._events.append((kind, fields))
        self._changed.notify_all()

    def _notify(self):
        while True:
            with self._changed:
                self._changed.wait_for(lambda: self._closed or self._events)
                if self._closed:
                    return
                kind, fields = self._events.popleft()
                self._notifying = True
            try:
                self._on_event(kind, **fields)
            except Exception:
                LOG.warning("A hearing event callback failed.")
            finally:
                del fields
                with self._changed:
                    self._notifying = False
                    self._changed.notify_all()

    def reset(self) -> None:
        """Discard this link's capture while an in-flight decode winds down."""
        with self._changed:
            self._active = None
            self._keyed = False
            self._key_down = False
            self._before.clear()
            self._onset.clear()
            self._ended.clear()
            self._events.clear()
            self._job = None
            reset = getattr(self._detector, "reset", None)
            if reset is not None:
                reset()
            self._changed.notify_all()

    def close(self) -> None:
        """Suppress new events. Any in-flight native decode releases the engine on return."""
        with self._changed:
            self._closed = True
            self.reset()
            if current_thread() is not self._workers[1]:
                self._changed.wait_for(lambda: not self._notifying)

    def join(self) -> None:
        for worker in self._workers:
            if worker is not current_thread():
                worker.join()
        join = getattr(self._detector, "join", None)
        if join is not None:
            join()
