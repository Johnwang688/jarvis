"""The Windows voice helper's device adapters and streaming audio."""

from collections import deque
from contextlib import ExitStack, contextmanager
import threading
import time
from typing import ContextManager, Protocol
import warnings


DEVICE_RATE = 48000
VOICE_RATE = 16000
STEP = VOICE_RATE // 100
DEVICE_STEP = DEVICE_RATE // 100
MAX_FRAMES = 50
MAX_QUEUED_SPEECH_S = 4


class Recorder(Protocol):
    def read(self): ...


class Player(Protocol):
    def write(self, samples): ...
    def drain(self, cancelled) -> bool: ...


class Devices(Protocol):
    def recorder(self, reference: bool) -> ContextManager[Recorder]: ...
    def player(self) -> ContextManager[Player]: ...


class DeviceChanged(Exception):
    pass


@contextmanager
def _soundcard():
    import ctypes

    # Import first: SoundCard initializes COM itself on the thread that first imports it and treats
    # "already initialized" as an error, so our own call must come after (found on real Windows).
    import soundcard

    ole = ctypes.WinDLL("ole32")
    result = ole.CoInitializeEx(None, 0)
    if result not in (0, 1):
        raise OSError("Audio thread initialization failed.")
    try:
        # SoundCard installs its own warning filter when first imported.
        warnings.filterwarnings("ignore", module=r"soundcard(\.|$)")
        yield soundcard
    finally:
        ole.CoUninitialize()


class _Device:
    def __init__(self, device, get_default, stream):
        self._id = device.id
        self._get_default = get_default
        self._stream = stream
        self._check_at = time.monotonic() + 1

    def _check(self):
        now = time.monotonic()
        if now >= self._check_at:
            self._check_at = now + 1
            if self._get_default().id != self._id:
                raise DeviceChanged()

    def read(self):
        import numpy as np

        self._check()
        data = self._stream.record(numframes=DEVICE_STEP)
        return np.asarray(data, dtype=np.float32).reshape(len(data), -1).mean(axis=1)

    def write(self, samples):
        self._check()
        self._stream.play(samples.reshape(-1, 1))

    def drain(self, cancelled):
        # SoundCard.play returns when WASAPI accepts the samples. Wait for
        # that queue to empty before acknowledging playback to the daemon.
        deadline = time.monotonic() + 0.18
        while not cancelled():
            if not self._stream.currentpadding:
                return True
            if time.monotonic() >= deadline:
                raise OSError("The audio buffer did not drain.")
            time.sleep(0.001)
        return False


class SoundcardDevices:
    """Open and use each WASAPI endpoint on the same worker thread."""

    @contextmanager
    def recorder(self, reference):
        with _soundcard() as soundcard:
            get_default = soundcard.default_speaker if reference else soundcard.default_microphone
            device = get_default()
            source = soundcard.get_microphone(device.id, include_loopback=True) if reference else device
            with source.recorder(samplerate=DEVICE_RATE, channels=1, blocksize=DEVICE_STEP) as stream:
                yield _Device(device, get_default, stream)

    @contextmanager
    def player(self):
        with _soundcard() as soundcard:
            speaker = soundcard.default_speaker()
            with speaker.player(samplerate=DEVICE_RATE, channels=1, blocksize=DEVICE_STEP) as stream:
                yield _Device(speaker, soundcard.default_speaker, stream)


class Resampler:
    """A streaming FIR decimator for capture and linear interpolation for playback."""

    def __init__(self, source_rate, target_rate):
        import numpy as np

        self._source, self._target = source_rate, target_rate
        self._seen = self._next = 0
        self._last = None
        self._kernel = None
        if (source_rate, target_rate) == (DEVICE_RATE, VOICE_RATE):
            # 96 taps of history and a 7 kHz cutoff leave a transition band
            # before Nyquist at 8 kHz. Both capture streams use the same filter.
            taps = np.arange(-48, 49)
            kernel = np.sinc(taps * (14000 / DEVICE_RATE)) * np.blackman(len(taps))
            self._kernel = kernel / kernel.sum()
            self._history = np.zeros(len(taps) - 1, dtype=np.float32)

    def process(self, samples, *, final=False):
        import numpy as np

        samples = np.asarray(samples, dtype=np.float32)
        if self._source == self._target:
            return samples
        if self._kernel is not None:
            block = np.concatenate((self._history, samples))
            filtered = np.convolve(block, self._kernel, mode="valid") if len(samples) else block[:0]
            self._history = block[-len(self._history):]
            start = -self._seen % 3
            self._seen += len(samples)
            return filtered[start::3].astype(np.float32)
        if not len(samples):
            return samples
        start = self._seen
        self._seen += len(samples)
        end = self._seen if final else self._seen - 1
        limit = (end * self._target + self._source - 1) // self._source
        # Use an integer clock so separately resampled chunks share phases.
        ticks = np.arange(self._next, limit, dtype=np.int64) * self._source
        self._next = limit
        if self._last is not None:
            start -= 1
            samples = np.concatenate(([self._last], samples))
        self._last = samples[-1]
        left, fractions = np.divmod(ticks - start * self._target, self._target)
        right = np.minimum(left + 1, len(samples) - 1)
        return (samples[left] + (samples[right] - samples[left])
                * (fractions / self._target)).astype(np.float32)


def pcm16(samples):
    import numpy as np

    return (np.clip(samples, -1, 32767 / 32768) * 32768).astype("<i2")


class Canceller:
    """Keep the measured speaker-to-mic delay mostly outside WebRTC APM."""

    HISTORY_STEPS = 100
    RECHECK_S = 30
    RESIDUAL_MS = 10
    MAX_DELAY_MS = 250

    def __init__(self, rtc, *, clock=time.monotonic):
        self._rtc, self._clock = rtc, clock
        self.reset()

    def reset(self):
        import numpy as np

        self._apm = self._rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=False,
                                                    high_pass_filter=True, auto_gain_control=False)
        self.delay_ms = 60.0
        self._apm.set_stream_delay_ms(self.RESIDUAL_MS)
        self._reference = np.zeros(self.MAX_DELAY_MS * 16, dtype="<i2")
        self._near = deque(maxlen=self.HISTORY_STEPS)
        self._far = deque(maxlen=self.HISTORY_STEPS)
        self._next_check = self._clock()

    @classmethod
    def available(cls):
        try:
            from livekit import rtc
            return cls(rtc)
        except (ImportError, OSError):
            return None

    @classmethod
    def estimate(cls, near, far):
        import numpy as np

        near = np.array(near, dtype=np.float64)
        far = np.array(far, dtype=np.float64)
        if not len(near) or len(near) != len(far):
            return None
        near -= near.mean()
        far -= far.mean()
        if min(near.std(), far.std()) < 8:
            return None
        fft_size = 1 << (len(near) + len(far) - 1).bit_length()
        correlation = np.fft.irfft(np.fft.rfft(near, fft_size)
                                   * np.fft.rfft(far, fft_size).conj(), fft_size)
        max_lag = min(cls.MAX_DELAY_MS * 16, len(near) - 1)
        # Compare only physically plausible positive delays. Reject weak
        # matches during double talk, then try again when echo is present.
        values = np.abs(correlation[:max_lag + 1])
        lag = int(values.argmax())
        energy = np.linalg.norm(near[lag:]) * np.linalg.norm(far[:len(near) - lag])
        if energy and values[lag] >= 0.3 * energy:
            return lag / 16
        return None

    def _frame(self, samples):
        return self._rtc.AudioFrame(data=samples.tobytes(), sample_rate=VOICE_RATE,
                                    num_channels=1, samples_per_channel=STEP)

    def process(self, near, far):
        import numpy as np

        self._near.append(near)
        self._far.append(far)
        now = self._clock()
        if now >= self._next_check and len(self._near) == self.HISTORY_STEPS:
            delay = self.estimate(np.concatenate(self._near), np.concatenate(self._far))
            self._next_check = now + (1 if delay is None else self.RECHECK_S)
            if delay is not None:
                self.delay_ms = delay
                self._apm.set_stream_delay_ms(min(self.RESIDUAL_MS, int(delay)))
        delay = max(0, round((self.delay_ms - self.RESIDUAL_MS) * 16))
        reference = np.concatenate((self._reference, far))
        start = len(self._reference) - delay
        self._reference = reference[STEP:]
        self._apm.process_reverse_stream(self._frame(reference[start:start + STEP]))
        frame = self._frame(near)
        self._apm.process_stream(frame)
        return np.frombuffer(bytes(frame.data), dtype="<i2")


class _CapturePair:
    """The capture coordinator owns both queues under its condition lock."""

    def __init__(self):
        import numpy as np

        self.mic = deque()
        self.ref = deque()
        self._last = np.zeros(STEP, dtype="<i2")
        self._repeating = False
        self.drops = self.repeats = 0

    def next(self):
        difference = len(self.ref) - len(self.mic)
        if difference > 3:
            for _ in range(difference - 2):
                self.ref.popleft()
                self.drops += 1
            difference = 2
        if not self.mic:
            return None
        # A correction starts beyond 30 ms and brings the queues back
        # within one 20 ms output frame.
        self._repeating = difference < (-2 if self._repeating else -3)
        if self._repeating:
            self.repeats += 1
        elif self.ref:
            self._last = self.ref.popleft()
        else:
            return None
        return self.mic.popleft(), self._last


class Capture:
    def __init__(self, devices, canceller=None, *, on_error=None, on_pair=None):
        self._devices, self._canceller = devices, canceller
        self._on_error, self._on_pair = on_error, on_pair
        self._changed = threading.Condition()
        self._threads = []
        self._started_before = False
        self._stopping = True
        self._generation = 0
        self._frames = deque(maxlen=MAX_FRAMES)
        self.reason = None
        self.opened = [False, False]
        self.received = [0, 0]
        self.dropped_frames = 0
        self._pair = _CapturePair()

    @property
    def canceller(self):
        return self._canceller is not None

    @property
    def corrections(self):
        with self._changed:
            return self._pair.drops, self._pair.repeats

    def start(self):
        with self._changed:
            if self._threads:
                return
            self._generation += 1
            self._stopping = False
            self.reason = None
            self.opened = [False, False]
            self.received = [0, 0]
            self.dropped_frames = 0
            self._pair = _CapturePair()
            jobs = [(self._record, (False,)), (self._record, (True,)), (self._assemble, ())]
            self._threads = [threading.Thread(target=job, args=args, daemon=True) for job, args in jobs]
            for thread in self._threads:
                thread.start()

    def _end(self, reason):
        with self._changed:
            if self._stopping:
                return
            self.reason = reason
            self._stopping = True
            self._frames.clear()
            self._changed.notify_all()
        if self._on_error is not None:
            self._on_error(reason)

    def stop(self):
        with self._changed:
            self._stopping = True
            self._frames.clear()
            self._changed.notify_all()
            threads = self._threads
        for thread in threads:
            thread.join()
        with self._changed:
            if self._threads is threads:
                self._threads = []
                self._pair.mic.clear()
                self._pair.ref.clear()

    def restart(self):
        self.stop()
        self.start()

    def get(self, timeout=None):
        with self._changed:
            self._changed.wait_for(lambda: self._frames or self._stopping, timeout)
            return self._frames.popleft() if self._frames else None

    @property
    def generation(self):
        with self._changed:
            return self._generation

    def forward(self, generation, frame, send_audio, send_level):
        """Forward a dequeued frame only while its capture run is still active."""
        with self._changed:
            if self._stopping or generation != self._generation:
                return False
            raw, clean, level = frame
            send_audio(raw, clean)
            if level is not None:
                send_level(level)
            return True

    def _record(self, reference):
        try:
            resampler = Resampler(DEVICE_RATE, VOICE_RATE)
            with ExitStack() as stack:
                with self._changed:
                    if self._stopping:
                        return
                    recorder = stack.enter_context(self._devices.recorder(reference))
                    self.opened[reference] = True
                    target = self._pair.ref if reference else self._pair.mic
                while not self._stopping:
                    frame = pcm16(resampler.process(recorder.read()))
                    if len(frame) != STEP:
                        raise OSError("Audio recorder returned an incomplete block.")
                    with self._changed:
                        if self._stopping:
                            return
                        self.received[reference] += 1
                        if len(target) >= MAX_FRAMES * 2:
                            target.popleft()
                            if reference:
                                self._pair.drops += 1
                        target.append(frame)
                        self._changed.notify_all()
        except DeviceChanged:
            self._end("Audio device changed.")
        except Exception:
            self._end("Audio capture stopped after a device error.")

    def _assemble(self):
        import numpy as np

        raw, cancelled = [], []
        ordinal = 0
        try:
            reset = getattr(self._canceller, "reset", None)
            if self._started_before and reset is not None:
                reset()
            self._started_before = True
            while True:
                with self._changed:
                    while not self._stopping:
                        pair = self._pair.next()
                        if pair is not None:
                            break
                        self._changed.wait()
                    if self._stopping:
                        return
                near, far = pair
                clean = self._canceller.process(near, far) if self.canceller else None
                if self._on_pair is not None:
                    self._on_pair(near, far, clean)
                raw.append(near)
                if clean is not None:
                    cancelled.append(clean)
                if len(raw) == 2:
                    near = np.concatenate(raw)
                    clean = np.concatenate(cancelled) if cancelled else None
                    raw.clear()
                    cancelled.clear()
                    ordinal += 1
                    level = None
                    if ordinal % 6 == 0:
                        rms = np.sqrt(np.mean(near.astype(np.float32)**2))
                        level = min(255, round(rms * (1020 / 32768)))
                    with self._changed:
                        if self._stopping:
                            return
                        if len(self._frames) == MAX_FRAMES:
                            self.dropped_frames += 1
                        self._frames.append((near, clean, level))
                        self._changed.notify_all()
        except Exception:
            self._end("Audio processing stopped after an error.")


def tone(seconds, first, second, volume):
    import numpy as np

    t = np.arange(round(seconds * DEVICE_RATE)) / DEVICE_RATE
    envelope = np.minimum(1, np.minimum(t, seconds - t) / 0.015)
    return (volume * envelope * (np.sin(2 * np.pi * first * t)
                                + np.sin(2 * np.pi * second * t)) / 2).astype(np.float32)


class Playback:
    """A single persistent writer, including silent writes for loopback capture."""

    def __init__(self, devices, *, played, flushed=None, on_error=None):
        self._devices = devices
        self._played, self._flushed, self._on_error = played, flushed, on_error
        self._lock = threading.RLock()
        self._queue = deque()
        self._queued_samples = 0
        self._incomplete = set()
        self._generation = 0
        self._flush = False
        self._flush_callbacks = []
        self._active = self._closed = False
        self._earcon = None
        self._thread = None
        self.opened = False

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            self.opened = self._closed = False
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    @property
    def playing(self):
        with self._lock:
            return self._active or bool(self._queue) or self._earcon is not None

    def put(self, index, flags, rate, data):
        """Queue speech and return False if the chunk was dropped."""
        # Round up to keep the duration bound at any source sample rate.
        count = (len(data) // 2 * DEVICE_RATE + rate - 1) // rate
        with self._lock:
            if self._closed:
                return False
            if index in self._incomplete or self._queued_samples + count > MAX_QUEUED_SPEECH_S * DEVICE_RATE:
                # Reject the rest of a truncated sentence, including its LAST.
                if flags & 2:
                    self._incomplete.discard(index)
                else:
                    self._incomplete.add(index)
                return False
            self._queue.append((index, flags, rate, data, count))
            self._queued_samples += count
            return True

    def _clear(self):
        self._queue.clear()
        self._queued_samples = 0
        self._incomplete.clear()
        self._earcon = None

    def flush(self, *, on_silent=None):
        callback = self._flushed if on_silent is None else on_silent
        with self._lock:
            self._generation += 1
            self._clear()
            if not self._closed and self._thread is not None:
                self._flush = True
                if callback is not None:
                    self._flush_callbacks.append(callback)
                return
        if callback is not None:
            callback()

    def earcon(self, name):
        frequencies = {"start": (520, 660), "stop": (440, 330), "notice": (740, 740)}
        first, second = frequencies.get(name, (440, 554))
        sound = tone(0.12, first, second, 0.03 if name == "notice" else 0.05)
        with self._lock:
            self._earcon = sound

    def close(self):
        with self._lock:
            self._closed = True
            self._clear()
            self._flush = False
            self._flush_callbacks.clear()
            thread = self._thread
        if thread is not None:
            thread.join()
        with self._lock:
            self._thread = None
            self._active = False

    def _run(self):
        try:
            while True:
                with self._devices.player() as player:
                    self.opened = True
                    flushing = self._write(player)
                if not flushing:
                    return
                # Closing the stream stops WASAPI and releases its queued audio.
                with self._lock:
                    if self._closed:
                        return
                    callbacks, self._flush_callbacks = self._flush_callbacks, []
                    self._flush = False
                for callback in callbacks:
                    callback()
        except Exception as error:
            with self._lock:
                self._closed = True
                self._clear()
                self._flush = False
                self._flush_callbacks.clear()
                self._active = False
            if self._on_error is not None:
                reason = ("Audio device changed." if isinstance(error, DeviceChanged)
                          else "Audio playback stopped after a device error.")
                self._on_error(reason)

    def _write(self, player):
        import numpy as np

        silence = np.zeros(DEVICE_STEP, dtype=np.float32)
        resampler = None
        index = rate = None
        samples = silence[:0]
        last = False
        while True:
            with self._lock:
                if self._closed:
                    return False
                if self._flush:
                    self._active = False
                    return True
                generation = self._generation
                while not last and len(samples) < DEVICE_STEP and self._queue:
                    ident, flags, new_rate, data, count = self._queue.popleft()
                    self._queued_samples -= count
                    if ident != index or new_rate != rate or flags & 1:
                        resampler = Resampler(new_rate, DEVICE_RATE)
                        index, rate = ident, new_rate
                    last = bool(flags & 2)
                    chunk = resampler.process(np.frombuffer(data, dtype="<i2") / 32768, final=last)
                    samples = np.concatenate((samples, chunk)) if len(samples) else chunk
                self._active = bool(len(samples)) or last
                piece, samples = samples[:DEVICE_STEP], samples[DEVICE_STEP:]
                done = last and not len(samples)
                if done:
                    last = False
                if len(piece) < DEVICE_STEP:
                    piece = np.pad(piece, (0, DEVICE_STEP - len(piece))) if len(piece) else silence
                if self._earcon is not None:
                    overlay = self._earcon[:DEVICE_STEP]
                    self._earcon = self._earcon[DEVICE_STEP:] if len(self._earcon) > DEVICE_STEP else None
                    piece = piece * (1 - np.abs(overlay)) + overlay
            player.write(piece)
            if done:
                def cancelled():
                    with self._lock:
                        return self._closed or generation != self._generation

                if player.drain(cancelled) and not cancelled():
                    self._played(index)
