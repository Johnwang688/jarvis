"""Local Jarvis KWS and bounded in-memory utterances; no device imports on import."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import queue
import tempfile
import threading
import time

RATE, CHUNK = 16000, 1600
WAKE_PIECES = ("▁JA", "R", "VI", "S")  # hud/src/lib/wake/keywords.ts
MODEL_DIR = Path(__file__).resolve().parents[1] / "hud" / "vendor" / "kws-zipformer-gigaspeech-3.3M"


def keywords_text(pieces, name="JARVIS", boost=1.0, threshold=0.25):
    return f"{' '.join(pieces)} :{float(boost)} #{float(threshold)} @{name}\n"


def verify_model(directory):
    try:
        root = Path(directory).resolve()
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        entries = manifest["files"]
        if not isinstance(entries, list) or not entries:
            raise ValueError()
        for entry in entries:
            name, size, digest = entry["file"], entry["bytes"], entry["sha256"]
            if not isinstance(name, str) or not name or type(size) is not int or size < 0:
                raise ValueError()
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return False, "Wake model file missing or invalid"
            if path.stat().st_size != size:
                return False, "Wake model file size mismatch"
            with path.open("rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            if actual != digest:
                return False, "Wake model checksum mismatch"
    except (OSError, ValueError, KeyError, TypeError, RecursionError, RuntimeError):
        return False, "Wake model missing or invalid"
    return True, ""


class Endpointer:
    def __init__(self, rate=16000, silence_ms=800, max_ms=15000, *, grace_ms=0):
        if rate <= 0 or silence_ms <= 0 or max_ms <= 0 or grace_ms < 0:
            raise ValueError("Endpoint durations and rate must be positive")
        self.rate = rate
        self.silence_frames = round(rate * silence_ms / 1000)
        self.max_frames = round(rate * max_ms / 1000)
        self.grace_frames = round(rate * grace_ms / 1000)
        self.frames = self.quiet_frames = 0
        self.noise_floor = float("inf")
        self.spoken = False
        self.result = None

    def feed(self, rms, n):
        if self.result is not None:
            return self.result
        if n <= 0:
            return None
        level = max(0.0, float(rms))
        self.frames += n
        # Learn even when the room starts above the absolute gate. Cap the
        # floor used by the gate so continuous loud speech cannot gate itself out.
        self.noise_floor = min(self.noise_floor, level)
        threshold = max(0.01, 3 * min(self.noise_floor, 0.03))
        if self.frames > self.grace_frames and level > threshold:
            self.spoken = True
            self.quiet_frames = 0
        else:
            self.quiet_frames += n
        if self.frames >= self.max_frames:
            self.result = "max"
        elif self.spoken and self.quiet_frames >= self.silence_frames:
            self.result = "end"
        elif not self.spoken and self.frames >= self.rate * 4:
            self.result = "timeout"
        return self.result


class WakeListener:
    def __init__(self, on_utterance, on_level, busy, on_state=None, model_dir=None, threshold=0.25):
        self.on_utterance, self.on_level, self.busy = on_utterance, on_level, busy
        self.on_state = on_state or (lambda state: None)
        self.model_dir = Path(model_dir) if model_dir is not None else MODEL_DIR
        self.threshold = threshold
        self.reason = ""
        self._done = threading.Event()
        self._audio_queue = queue.Queue(maxsize=8)
        self._epoch = 0
        self._stream = self._thread = self._spotter = self._temp = None
        self._tap = None

    def tap(self, callback):
        """Lend the open wake stream to the hotkey capture.

        Windows refuses to start a second WASAPI stream on the microphone while
        this one runs (PortAudio -9999), so the capture reads these frames
        instead. Returns False when there is no live stream to lend; the caller
        then opens its own. tap(None) ends the loan.
        """
        if callback is None:
            self._tap = None
            return True
        stream = self._stream
        if stream is None or self._done.is_set() or not getattr(stream, "active", False):
            return False
        self._tap = callback
        return True

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return True
        ok, self.reason = verify_model(self.model_dir)
        if not ok:
            return False
        try:
            import sherpa_onnx
        except Exception:
            self.reason = "Wake word needs sherpa-onnx: run jarvis voice-link setup"
            return False
        try:
            import numpy as np
            import sounddevice as sd
        except Exception:
            self.reason = "Wake word needs audio dependencies: run jarvis voice-link setup"
            return False
        self._np = np
        self._done.clear()
        self._audio_queue = queue.Queue(maxsize=8)
        self._epoch = 0
        try:
            self._temp = tempfile.TemporaryDirectory(prefix="jarvis-kws-")
            keyword_file = Path(self._temp.name) / "keywords.txt"
            keyword_file.write_text(keywords_text(WAKE_PIECES, threshold=self.threshold), encoding="utf-8")
            d = self.model_dir
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(d / "tokens.txt"), encoder=str(d / "encoder.int8.onnx"),
                decoder=str(d / "decoder.int8.onnx"), joiner=str(d / "joiner.int8.onnx"),
                keywords_file=str(keyword_file), num_threads=1, sample_rate=RATE,
                keywords_score=1.0, keywords_threshold=self.threshold,
                max_active_paths=4, num_trailing_blanks=1)
        except Exception:
            self.reason = "Wake word model could not load"
            self.stop()
            return False
        try:
            wasapi = next((api for api in sd.query_hostapis() if "WASAPI" in api["name"]), None)
            if wasapi is None or wasapi["default_input_device"] < 0:
                raise RuntimeError()
            self._stream = sd.InputStream(
                device=wasapi["default_input_device"], samplerate=RATE, channels=1,
                dtype="int16", blocksize=CHUNK,
                extra_settings=sd.WasapiSettings(exclusive=False, auto_convert=True),
                callback=self._audio)
            self._stream.start()
            self.reason = ""
            self._thread = threading.Thread(target=self._run, name="voice-wake", daemon=True)
            self._thread.start()
        except Exception:
            self.reason = "Wake word microphone unavailable"
            self.stop()
            return False
        return True

    def _audio(self, indata, frames, timing, status):
        # PortAudio callback: no inference, UI work or blocking. An epoch invalidates
        # queued audio AND a partial utterance, even if the worker was in a callback.
        if self._done.is_set():
            return
        tap = self._tap
        if tap is not None:
            try:
                tap(indata, frames, timing, status)
            except Exception:
                pass
        if status or self.busy():
            self._epoch += 1
            return
        try:
            self._audio_queue.put_nowait((self._epoch, time.monotonic(), indata.tobytes()))
        except queue.Full:
            self._epoch += 1

    def _run(self):
        try:
            self._listen()
        except Exception:
            if not self._done.is_set():
                self.reason = "Wake word microphone or recognizer stopped"
                self._done.set()
                try:
                    self.on_state("error")
                except Exception:
                    pass
        finally:
            # stop() normally owns cleanup; worker failures must release the mic too.
            self._close_resources()

    def _listen(self):
        try:
            from .voicewire import wav_bytes
        except ImportError:  # Direct Windows launcher, without the repo on sys.path.
            from voicewire import wav_bytes

        spotter, np = self._spotter, self._np
        stream = spotter.create_stream()
        epoch = self._epoch
        endpoint, chunks = None, []
        while not self._done.is_set():
            try:
                stamp, captured, pcm = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._done.is_set():
                break
            if self.busy() or stamp != self._epoch or time.monotonic() - captured > 0.5:
                stream = spotter.create_stream()
                endpoint, chunks = None, []
                epoch = self._epoch
                continue
            if stamp != epoch:
                stream = spotter.create_stream()
                endpoint, chunks = None, []
                epoch = stamp
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples * samples)))
            if endpoint is None:
                stream.accept_waveform(RATE, samples)
                hit = False
                while spotter.is_ready(stream):
                    spotter.decode_stream(stream)
                    result = spotter.get_result(stream)
                    word = result if isinstance(result, str) else getattr(result, "keyword", "")
                    if word:
                        hit = True
                        spotter.reset_stream(stream)
                        break
                if not hit:
                    continue
                if self._done.is_set() or self.busy() or stamp != self._epoch:
                    stream = spotter.create_stream()
                    continue
                self.on_state("listening")
                endpoint, chunks = Endpointer(grace_ms=250), []
                # Retain audio after the hit, including any wake tail. The grace
                # counts toward timeout but cannot alone establish question speech.
                continue
            chunks.append(pcm)
            self.on_level(rms)
            # A chunk may straddle the grace boundary: classify only its suffix
            # so a loud tail confined to the grace cannot supply its RMS.
            grace = endpoint.grace_frames - endpoint.frames
            if 0 < grace < len(samples):
                endpoint.feed(rms, grace)
                samples = np.frombuffer(pcm[grace * 2:], dtype="<i2").astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(samples * samples)))
            result = endpoint.feed(rms, len(samples))
            if result:
                clip = wav_bytes(b"".join(chunks), RATE) if result != "timeout" else None
                endpoint, chunks = None, []
                if not self._done.is_set() and not self.busy() and stamp == self._epoch:
                    if clip is None:
                        self.on_state("hidden")
                    else:
                        self.on_utterance(clip)
                stream = spotter.create_stream()

    def _close_resources(self):
        # Only the worker closes after it starts; stop() joins before cleanup.
        self._tap = None
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        self._spotter = None
        temp, self._temp = self._temp, None
        if temp is not None:
            temp.cleanup()

    def stop(self):
        self._done.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        if thread is None or not thread.is_alive():
            self._thread = None
            self._close_resources()
            while True:
                try:
                    self._audio_queue.get_nowait()
                except queue.Empty:
                    break
