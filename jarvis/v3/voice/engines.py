"""Lazy, local speech engines. Audio at the ASR boundary is 16 kHz mono PCM."""

from __future__ import annotations

import ctypes
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from jarvis import config

if TYPE_CHECKING:
    from numpy import ndarray

SAMPLE_RATE = 16_000
MAX_PROMPT_CHARS = 256
_WHISPER_WEIGHTS = ("config.json", "model.bin", "vocabulary.json")
_WHISPER_TEXT = ("tokenizer.json", "preprocessor_config.json")
_STOCK_TEXT = {"thank you", "thanks for watching", "thank you for watching", "you",
               "thanks for listening", "please subscribe", "bye"}


class Transcriber(Protocol):
    def transcribe(self, samples: ndarray, *, names: Sequence[str]) -> str: ...


class Voice(Protocol):
    def synthesize(self, text: str) -> tuple[ndarray, int]: ...


class EngineUnavailable(RuntimeError):
    """An engine cannot be used locally."""


def _float_pcm(samples: ndarray) -> ndarray:
    import numpy as np

    samples = np.asarray(samples)
    if samples.ndim != 1 or samples.dtype not in (np.float32, np.int16):
        raise ValueError("expected mono float32 or int16 PCM")
    if samples.dtype == np.int16:
        return samples.astype(np.float32) / 32768.0
    return samples


# Preload the same wheel libraries used by the speech spike, in dependency
# order. Changing LD_LIBRARY_PATH after process startup cannot do this on Linux.
_CUDA_WHEELS = (
    ("cuda-runtime", "cuda_runtime", ("libcudart.so.12",)),
    ("nvjitlink", "nvjitlink", ("libnvJitLink.so.12",)),
    ("cuda-nvrtc", "cuda_nvrtc", ("libnvrtc-builtins.so.12.9", "libnvrtc.so.12")),
    ("cublas", "cublas", ("libcublasLt.so.12", "libcublas.so.12")),
    ("cufft", "cufft", ("libcufft.so.11",)),
    ("curand", "curand", ("libcurand.so.10",)),
    ("cudnn", "cudnn", tuple(f"libcudnn{component}.so.9" for component in (
        "_graph", "_ops", "_adv", "_cnn", "_engines_precompiled",
        "_engines_runtime_compiled", "_engines_tensor_ir", "_heuristic", "",
    ))),
)


@lru_cache(maxsize=1)
def _preload_cuda() -> tuple:
    try:
        driver = ctypes.CDLL("libcuda.so.1", mode=ctypes.RTLD_GLOBAL)
    except OSError:
        driver = ctypes.CDLL("/usr/lib/wsl/lib/libcuda.so.1", mode=ctypes.RTLD_GLOBAL)
    handles = [driver]
    for wheel, module, libraries in _CUDA_WHEELS:
        try:
            package = distribution(f"nvidia-{wheel}-cu12")
        except PackageNotFoundError:
            continue  # A system CUDA installation can supply these libraries.
        for library in libraries:
            path = package.locate_file(f"nvidia/{module}/lib/{library}")
            handles.append(ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL))
    return tuple(handles)


class _Engine:
    def __init__(self):
        self._lock = Lock()
        self._backend = None
        self._closed = False
        self.reason = ""

    def _get_backend(self):
        # Call under the engine lock. Successful probes keep the model warm.
        if self._closed:
            raise EngineUnavailable("engine is closed")
        if self._backend is None:
            if self.reason:
                raise EngineUnavailable(self.reason)
            try:
                self._backend = self._load()
            except EngineUnavailable as exc:
                self.reason = str(exc)
            except Exception:
                self.reason = "engine could not be initialized"
            if self.reason:
                raise EngineUnavailable(self.reason)
        return self._backend

    def available(self) -> bool:
        with self._lock:
            try:
                self._get_backend()
            except EngineUnavailable:
                return False
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            close = getattr(self._backend, "close", None)
            self._backend = None
            if close is not None:
                close()


class WhisperGpu(_Engine):
    def __init__(self, model_dir: Path | None = None):
        super().__init__()
        self.model_dir = Path(model_dir or config.V3_WHISPER_MODEL_DIR).expanduser()

    def _load(self):
        # faster-whisper falls back to a hub tokenizer if the local one is
        # missing, and defaults to 80 mel bins without the feature config.
        directory = self.model_dir.resolve()
        if not all((directory / filename).is_file() for filename in (*_WHISPER_WEIGHTS, *_WHISPER_TEXT)):
            raise EngineUnavailable("local Whisper model files are missing")
        try:
            _preload_cuda()
            from faster_whisper import WhisperModel

            # Keep the tokenizer present throughout initialization, including
            # if the configured model directory is being replaced. Passing
            # just tokenizer bytes via `files` instead selects CTranslate2's
            # in-memory loader, which would also require the full weights.
            with TemporaryDirectory(prefix="jarvis-whisper-") as local:
                for name in _WHISPER_TEXT:
                    data = (directory / name).read_bytes()
                    if not data:
                        raise EngineUnavailable("local Whisper tokenizer or feature config is empty")
                    (Path(local) / name).write_bytes(data)
                for name in _WHISPER_WEIGHTS:
                    (Path(local) / name).symlink_to(directory / name)
                return WhisperModel(str(local), device="cuda", compute_type="int8_float16",
                                    cpu_threads=2, num_workers=1, local_files_only=True,
                                    use_auth_token=False)
        except ImportError:
            raise EngineUnavailable("faster-whisper could not be imported") from None
        except EngineUnavailable:
            raise
        except Exception:
            raise EngineUnavailable("Whisper CUDA model could not be initialized") from None

    def transcribe(self, samples: ndarray, *, names: Sequence[str]) -> str:
        import numpy as np

        audio = _float_pcm(samples)
        if not len(audio):
            return ""
        # Remove DC bias so an offset on a quiet input cannot pass as speech.
        rms = float(np.std(audio))
        if not np.isfinite(rms) or rms < 0.0025:
            return ""
        joined_names = ", ".join(name[:MAX_PROMPT_CHARS].strip() for name in islice(names, 64))
        prompt = f"Names and apps: {joined_names}."[:MAX_PROMPT_CHARS] if joined_names else None
        with self._lock:
            segments, _ = self._get_backend().transcribe(
                audio, language="en", beam_size=1, temperature=0, vad_filter=False,
                without_timestamps=True, condition_on_previous_text=False,
                initial_prompt=prompt,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        normalized = " ".join(re.findall(r"\w+", text.casefold()))
        if normalized in _STOCK_TEXT and (len(audio) < 2 * SAMPLE_RATE or rms < 0.012):
            return ""
        return text


class ParakeetCpu(_Engine):
    def _load(self):
        from jarvis import stt_local

        try:
            # The shared loader checks the same files and integrity marker.
            # Hearing already supplies a worker and PCM, so bypass the WAV API.
            return stt_local._default_factory(config.STT_LOCAL_THREADS)
        except stt_local.LocalSTTUnavailable as exc:
            raise EngineUnavailable(str(exc)) from None

    def transcribe(self, samples: ndarray, *, names: Sequence[str]) -> str:
        audio = _float_pcm(samples)
        if not len(audio):
            return ""
        with self._lock:
            return self._get_backend().decode(audio, SAMPLE_RATE).strip()


def _trim_edges(samples: ndarray, sample_rate: int) -> ndarray:
    import numpy as np

    if not np.any(samples):
        return samples[:0]
    window = max(1, sample_rate * 10 // 1000)
    starts = np.arange(0, len(samples), window)
    ends = np.minimum(starts + window, len(samples))
    power = np.add.reduceat(np.square(samples, dtype=np.float64), starts) / (ends - starts)
    below_floor = power < 10 ** (-55 / 10)
    audible = np.flatnonzero(~below_floor)
    if not len(audible):
        return samples  # There is no reliable speech boundary to trim against.

    first, last = audible[0], audible[-1]
    start = starts[first] + np.flatnonzero(samples[starts[first]:ends[first]])[0]
    end = starts[last] + np.flatnonzero(samples[starts[last]:ends[last]])[-1] + 1
    # Keep 60 ms beyond the nonzero edges. This also keeps both edge windows intact.
    margin = sample_rate * 60 // 1000
    return samples[max(0, start - margin):end + margin]


def _kokoro_pcm(samples: ndarray, rate: int) -> ndarray:
    import numpy as np

    samples = _trim_edges(samples, rate)
    return np.rint(np.clip(samples, -1, 1) * 32767).astype(np.int16)


def _kokoro_speed(speed: float | None) -> float:
    chosen = config.V3_KOKORO_SPEED if speed is None else float(speed)
    return max(0.8, min(2.0, chosen)) if chosen == chosen else 1.35


def cuda_voice_available() -> bool:
    try:
        import onnxruntime as ort

        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


class KokoroVoice(_Engine):
    def __init__(self, *, device: str = "auto", speed: float | None = None):
        super().__init__()
        if device not in ("auto", "cuda", "cpu"):
            raise ValueError("Kokoro device must be auto, cuda or cpu")
        self.device = device
        self.active_device = None
        self.fallback_reason = ""
        self.speed = _kokoro_speed(speed)

    def _load(self):
        model = config.KOKORO_MODEL.expanduser()
        voices = config.KOKORO_VOICES.expanduser()
        if not model.is_file() or not voices.is_file():
            raise EngineUnavailable("local Kokoro model or voices file is missing")
        try:
            import onnxruntime as ort
            from kokoro_onnx import Kokoro
        except ImportError:
            raise EngineUnavailable("Kokoro or ONNX Runtime could not be imported") from None

        if self.device != "cpu":
            try:
                kokoro = self._load_device(ort, Kokoro, model, voices, "cuda")
            except Exception:
                self.fallback_reason = "Kokoro CUDA session could not be initialized"
                if self.device == "cuda":
                    raise EngineUnavailable(self.fallback_reason) from None
            else:
                self.active_device = "cuda"
                return kokoro
        try:
            kokoro = self._load_device(ort, Kokoro, model, voices, "cpu")
        except Exception:
            reason = "Kokoro CPU session could not be initialized"
            if self.fallback_reason:
                reason = f"{self.fallback_reason}. {reason}"
            raise EngineUnavailable(reason) from None
        self.active_device = "cpu"
        return kokoro

    @staticmethod
    def _load_device(ort, kokoro, model, voices, device):
        provider = "CUDAExecutionProvider" if device == "cuda" else "CPUExecutionProvider"
        if provider not in ort.get_available_providers():
            raise EngineUnavailable("requested ONNX Runtime provider is unavailable")
        if device == "cuda":
            _preload_cuda()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        options.log_severity_level = 4
        session = ort.InferenceSession(str(model), sess_options=options, providers=[provider])
        if session.get_providers()[0] != provider:
            raise EngineUnavailable("requested ONNX Runtime provider did not initialize")
        session.disable_fallback()
        return kokoro.from_session(session, str(voices))

    def synthesize(self, text: str) -> tuple[ndarray, int]:
        import numpy as np
        from jarvis.voice import speakable

        text = speakable(text)
        if not text:
            return np.empty(0, dtype=np.int16), 24_000
        with self._lock:
            samples, rate = self._get_backend().create(
                text, voice="bm_george", speed=self.speed, lang="en-gb", trim=False,
            )
        return _kokoro_pcm(samples, rate), rate


_EngineType = TypeVar("_EngineType")


@dataclass(frozen=True)
class EngineChoice(Generic[_EngineType]):
    engine: _EngineType | None
    name: str | None
    reason: str


def pick_transcriber(setting: str | None = None) -> EngineChoice[Transcriber]:
    setting = config.V3_TRANSCRIBER if setting is None else setting
    if setting not in ("auto", "whisper-gpu", "parakeet-cpu"):
        return EngineChoice(None, None, "unknown transcriber setting")
    reasons = []
    for name, engine_class in (("whisper-gpu", WhisperGpu), ("parakeet-cpu", ParakeetCpu)):
        if setting not in ("auto", name):
            continue
        engine = engine_class()
        if engine.available():
            reasons.append(f"{name} is available" if setting == "auto" else f"{name} was requested")
            return EngineChoice(engine, name, ". ".join(reasons))
        reasons.append(engine.reason)
    return EngineChoice(None, None, ". ".join(reasons))


def pick_voice(setting: str | None = None) -> EngineChoice[Voice]:
    setting = config.V3_VOICE if setting is None else setting
    devices = {"auto": "auto", "kokoro-gpu": "cuda", "kokoro-cpu": "cpu"}
    if setting not in devices:
        return EngineChoice(None, None, "unknown voice setting")
    engine = KokoroVoice(device=devices[setting])
    if not engine.available():
        return EngineChoice(None, None, engine.reason)
    name = "kokoro-gpu" if engine.active_device == "cuda" else "kokoro-cpu"
    reason = f"{name} is available" if setting == "auto" else f"{name} was requested"
    if engine.fallback_reason:
        reason = f"{engine.fallback_reason}. {reason}"
    return EngineChoice(engine, name, reason)
