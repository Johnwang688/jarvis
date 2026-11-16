"""In-memory Kokoro → Parakeet round trips, with injectable backends."""
from pathlib import Path

from .pair_check import pair_check
from .storage import safe_input


def run(pairs, tts, stt, voices, rng):
    voices = list(voices)
    if not voices:
        raise ValueError('at least one voice is required')
    for pair in pairs:
        voice, speed = rng.choice(voices), rng.uniform(.9, 1.15)
        samples, rate = tts(pair['messy'], voice, speed)
        text = stt(samples, rate)
        yield {**pair, 'messy': text, 'kind': 'roundtrip', 'voice': voice, 'speed': speed,
               'guard_compatible': pair_check(text, pair['target'])[0],
               'provenance': {**pair.get('provenance', {}), 'roundtrip': {
                   'tts': 'Kokoro', 'tts_licence': 'Apache-2.0',
                   'stt': 'NVIDIA Parakeet TDT 0.6B v3', 'stt_licence': 'CC-BY-4.0'}}}


def kokoro_backend(model=None, voices=None):
    from kokoro_onnx import Kokoro
    model = safe_input(model or Path.home() / '.cache/jarvis-tts/kokoro-v1.0.onnx')
    voices = safe_input(voices or Path.home() / '.cache/jarvis-tts/voices-v1.0.bin')
    engine = Kokoro(str(model), str(voices))

    def speak(text, voice, speed):
        import numpy as np
        samples, rate = engine.create(text, voice=voice, speed=speed, lang='en-us')
        return np.asarray(samples, dtype=np.float32), rate
    return speak


def parakeet_backend(model_dir=None, threads=2):
    if threads < 1:
        raise ValueError('threads must be positive')
    import sherpa_onnx
    model_dir = safe_input(model_dir or Path.home() / '.cache/jarvis-stt/parakeet-tdt-0.6b-v3-int8')
    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(model_dir / 'encoder.int8.onnx'), decoder=str(model_dir / 'decoder.int8.onnx'),
        joiner=str(model_dir / 'joiner.int8.onnx'), tokens=str(model_dir / 'tokens.txt'),
        num_threads=threads, model_type='nemo_transducer', decoding_method='greedy_search')

    def transcribe(samples, rate):
        import numpy as np
        stream = recognizer.create_stream()
        stream.accept_waveform(rate, np.asarray(samples, dtype=np.float32))
        recognizer.decode_stream(stream)
        return stream.result.text
    return transcribe
