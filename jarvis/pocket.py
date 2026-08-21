"""Pocket TTS: the second local voice backend — clones and hybrid voices.

Kyutai's pocket-tts (github.com/kyutai-labs/pocket-tts) is a 100M-parameter
CPU TTS with zero-shot voice cloning: hand it up to 30 seconds of someone
speaking and it speaks as them. A "voice" here is the model state computed
from that audio prompt; pocket-tts exports it to a .safetensors file (it is
the model's KV-cache after reading the prompt), which reloads fast. This
module owns everything that touches the library — and therefore torch: the
model singleton, the per-process state cache, synthesis, and the custom
voice store. Install with `uv pip install -e .[pocketvoice]`.

voice.py is the only synthesis caller and owns the `pocket:` namespace;
names in here are bare. Every failure raises — voice.tts() catches and
degrades to Kokoro, so a broken store or a missing dependency still leaves
him audible, and the cloud fallback never sees a name only this backend
understands.

The store (config.VOICES_DIR, default ~/.local/share/jarvis/voices) is one
directory per voice: voice.json (name, kind, sources, weights, created),
state.safetensors, and for clones the reference audio it was built from. It
lives outside the repo — clones are audio of real people and never belong
in git (.gitignore carries a voices/ backstop anyway). Creation is
human-only (`jarvis voice clone|mix`, the `jarvis auth` pattern): the
pocket-tts license requires the speaker's lawful consent for cloning, so
making a voice is never a tool the agent holds.

Hybrids are experimental. pocket-tts has no native mixing, so mix() blends
the exported state tensors (see _blend); quality is judged by ear via
`jarvis voice say`, and the documented fallback for a bad blend is a
multi-file clone, which concatenates reference audio instead.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import wave
from datetime import datetime, timezone
from pathlib import Path

from . import avatars, config, voice

# The predefined voices the library ships (per its docs, 2026-08). A stale
# name after a library upgrade fails at prompt time and degrades audibly in
# voice.tts() — the existing rule for a voice that does not exist.
BUILTINS: tuple[str, ...] = (
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles",
    "cosette", "eponine", "eve", "fantine", "george", "jane", "javert",
    "jean", "marius", "mary", "michael", "paul", "peter_yearsley",
    "stuart_bell", "vera",
    "estelle",   # French
    "giovanni",  # Italian
    "juergen",   # German
    "lola",      # Spanish
    "rafael",    # Portuguese
)

# pocket-tts reads only the first 30 seconds of an audio prompt.
MAX_PROMPT_SECONDS = 30.0

# Reference audio is normalized to a healthy speech level before prompting.
# The model carries the prompt's loudness into everything it synthesizes —
# measured 2026-08-21: a peak-0.09 laptop-mic take cloned into a voice ~7x
# quieter than the builtins. RMS target with a peak ceiling, so one thump in
# an otherwise quiet take cannot defeat the boost or push it into clipping.
_REF_RMS = 0.08
_REF_PEAK = 0.95

_lock = threading.Lock()  # one model instance; generation is serialized on it
_model_instance = None
_states: dict[str, object] = {}  # bare name -> prompt state, for the process
_available: bool | None = None


def available() -> bool:
    """Is pocket-tts importable? Answered without importing it (or torch).

    The sys.modules check comes first so a test-injected fake counts;
    find_spec raises ValueError on such a module, not just returns None.
    """
    global _available
    if _available is None:
        if "pocket_tts" in sys.modules:
            _available = True
        else:
            try:
                _available = importlib.util.find_spec("pocket_tts") is not None
            except (ImportError, ValueError):
                _available = False
    return _available


def known(name: str) -> bool:
    """Is `name` a builtin or a voice in the store? Cheap — no model, no scan."""
    if name in BUILTINS:
        return True
    try:
        return (_voice_dir(name) / "voice.json").is_file()
    except ValueError:
        return False


def custom_voices() -> list[dict]:
    """Every voice in the store: [{name, kind, sources, weights, created}].

    Re-read per call, like avatars.active() — the store changes from the CLI
    while a face is serving, and a few stats per listing is the right price
    for never showing a stale catalog.
    """
    root = Path(config.VOICES_DIR)
    if not root.is_dir():
        return []
    out = []
    for meta_path in sorted(root.glob("*/voice.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue  # a half-written voice is not a voice
        out.append(
            {
                "name": str(meta.get("name") or meta_path.parent.name),
                "kind": str(meta.get("kind") or "clone"),
                "sources": meta.get("sources") or [],
                "weights": meta.get("weights"),
                "created": str(meta.get("created") or ""),
            }
        )
    return out


def synthesize(text: str, name: str) -> bytes:
    """Speak `text` as the pocket voice `name` -> 16-bit mono WAV bytes.

    Raises on any failure; voice.tts() owns the degradation story. The lock
    covers model, state and generation (one model instance, same rule as
    _kokoro_lock); the repad/encode tail runs outside it.
    """
    with _lock:
        model = _model()
        state = _state(name, model)
        audio = model.generate_audio(state, text)
        rate = int(model.sample_rate)
    samples = audio.detach().cpu().numpy().astype("float32")
    samples = voice._repad(samples, rate, voice._boundary(text))
    return voice._encode_wav(samples, rate)


def clone(name: str, sources: list[Path]) -> Path:
    """Build a custom voice from audio of one speaker (first 30s used).

    16-bit WAV sources are decoded, mixed to mono, resampled to one rate,
    concatenated, trimmed to the 30s the model reads, and **normalized** —
    a quiet reference clones into a quiet voice (see _REF_RMS). Anything the
    stdlib cannot decode (an mp3, an exotic WAV) is handed to pocket-tts
    untouched, at its recorded level. The store directory is built aside and
    renamed into place, so a crashed clone never leaves a half-voice the
    catalog would list.
    """
    target = _voice_dir(name)
    if name in BUILTINS or target.exists():
        raise ValueError(f"voice {name!r} already exists")
    sources = [Path(s) for s in sources]
    if not sources:
        raise ValueError("clone needs at least one audio file")
    for src in sources:
        if not src.is_file():
            raise FileNotFoundError(f"no audio file {src}")

    # mkdtemp is 0700 by construction and os.replace keeps the mode, so the
    # finished voice directory inherits the owner-only story from _store_root.
    work = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=_store_root()))
    try:
        ref = work / "ref.wav"
        if len(sources) == 1:
            try:
                _concat_wavs(sources, ref)  # decode -> mono -> normalize
            except (ValueError, wave.Error, EOFError):
                # Not a 16-bit WAV. pocket-tts decodes mp3 and friends
                # itself, so pass the file through — at its recorded level.
                ref = work / f"ref{sources[0].suffix.lower() or '.wav'}"
                shutil.copyfile(sources[0], ref)
        else:
            _concat_wavs(sources, ref)
        with _lock:
            model = _model()
            state = model.get_state_for_audio_prompt(str(ref))
            from pocket_tts import export_model_state

            export_model_state(state, str(work / "state.safetensors"))
        _write_meta(
            work, name=name, kind="clone",
            sources=[str(s.resolve()) for s in sources], weights=None,
        )
        os.replace(work, target)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return target


def mix(name: str, weights: dict[str, float]) -> Path:
    """Build a hybrid voice as a weighted blend of existing voices' states.

    Experimental — see _blend for the math and its loud failure modes.
    Components may be builtins or store voices; weights are normalized.
    """
    target = _voice_dir(name)
    if name in BUILTINS or target.exists():
        raise ValueError(f"voice {name!r} already exists")
    if len(weights) < 2:
        raise ValueError("a mix needs at least two voices")

    states = [_flat_state(component) for component in weights]
    blended = _blend(states, list(weights.values()))
    # Sliced tensors are views; safetensors wants them contiguous. numpy
    # arrays (the free suite) have no .contiguous and need none.
    blended = {
        k: t.contiguous() if hasattr(t, "contiguous") else t
        for k, t in blended.items()
    }

    work = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=_store_root()))
    try:
        _save_state(blended, work / "state.safetensors")
        _write_meta(
            work, name=name, kind="hybrid",
            sources=sorted(weights), weights=weights,
        )
        os.replace(work, target)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return target


def rename(old: str, new: str) -> Path:
    """Rename a custom voice. Builtins are not renameable, and references to
    the old name (an avatar.json, the HUD override) are not chased — they
    degrade audibly on their next use, the existing rule for a name that
    stopped existing."""
    if old in BUILTINS or new in BUILTINS:
        raise ValueError("builtin pocket voices cannot be renamed or shadowed")
    source, target = _voice_dir(old), _voice_dir(new)
    if not (source / "voice.json").is_file():
        raise LookupError(f"no pocket voice {old!r}")
    if target.exists():
        raise ValueError(f"voice {new!r} already exists")
    os.rename(source, target)
    meta_path = target / "voice.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    meta["name"] = new
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    _states.pop(old, None)  # the next synthesis must not speak as the old name
    return target


def remove(name: str) -> None:
    """Delete a custom voice. Builtins are not removable."""
    if name in BUILTINS:
        raise ValueError(f"{name!r} is a builtin pocket voice")
    target = _voice_dir(name)
    if not (target / "voice.json").is_file():
        raise LookupError(f"no pocket voice {name!r}")
    shutil.rmtree(target)
    # The next synthesis must not speak as a ghost; a face that already
    # cached the state keeps it until restart, which is documented.
    _states.pop(name, None)


# ---- internals ---------------------------------------------------------------


def _voice_dir(name: str) -> Path:
    """The store directory for `name`, with the slug rule as the gate.

    The same pattern avatars.load() uses: a name with a slash or a dot in it
    resolves to nothing, so this is never a path into the filesystem.
    """
    if not avatars.SLUG_RE.match(name):
        raise ValueError(f"{name!r} is not a usable voice name (a-z 0-9 - _)")
    return Path(config.VOICES_DIR) / name


def _store_root() -> Path:
    """The store, created owner-only. A cloned voice is reference audio of a
    real person plus the state to speak as them — credential-adjacent, so it
    gets the token-file treatment (0700, like ~/.config/jarvis bundles)."""
    root = Path(config.VOICES_DIR)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _model():
    """The TTSModel singleton. Call with _lock held; load is slow (~seconds)."""
    global _model_instance
    if _model_instance is None:
        from pocket_tts import TTSModel

        _model_instance = TTSModel.load_model()
    return _model_instance


def _state(name: str, model):
    """The cached prompt state for `name`. Call with _lock held.

    Builtins are prompted by name; store voices load their exported
    .safetensors — the documented fast path. If the live smoke ever shows
    generate_audio appending to the state it was passed, clone the tensors
    here per call; nothing else in this file would change.
    """
    if name in _states:
        return _states[name]
    if name in BUILTINS:
        state = model.get_state_for_audio_prompt(name)
    else:
        state_path = _voice_dir(name) / "state.safetensors"
        if not state_path.is_file():
            raise LookupError(f"no pocket voice {name!r}")
        state = model.get_state_for_audio_prompt(str(state_path))
    _states[name] = state
    return state


def _flat_state(name: str) -> dict:
    """The exported state for any voice name, as a flat dict of tensors.

    A store voice already has one on disk; a builtin is prompted and
    exported to a tempfile first — export_model_state is the one blessed
    serialization of the state object, and reading its file back is the one
    blessed way to get a flat dict, so the (unknown) live structure is never
    touched.
    """
    from safetensors.torch import load_file

    if name in BUILTINS:
        fd, tmp_name = tempfile.mkstemp(suffix=".safetensors")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with _lock:
                model = _model()
                state = model.get_state_for_audio_prompt(name)
                from pocket_tts import export_model_state

                export_model_state(state, str(tmp_path))
            return load_file(str(tmp_path))
        finally:
            tmp_path.unlink(missing_ok=True)
    state_path = _voice_dir(name) / "state.safetensors"
    if not state_path.is_file():
        raise LookupError(f"no pocket voice {name!r}")
    return load_file(str(state_path))


def _save_state(tensors: dict, path: Path) -> None:
    from safetensors.torch import save_file

    save_file(tensors, str(path))


def _blend(states: list[dict], weights: list[float]) -> dict:
    """Weighted per-key interpolation of exported voice states.

    The state is the model's KV-cache after the audio prompt, so tensors of
    two voices may differ along exactly one axis — the prompt-length one.
    Those are sliced to the shortest along that axis, keeping the HEAD:
    positions 0..n carry the same positional phase in every state, so
    trimming the tail leaves what remains aligned, while trimming the head
    would shift it. Anything stranger — different key sets, shapes differing
    in more than one axis — is a loud ValueError: this is an experiment, and
    a wrong blend must fail, not whisper.

    Written with plain operators (* + slicing) so the free suite runs it on
    numpy arrays while production runs it on torch tensors.
    """
    if len(states) != len(weights) or len(states) < 2:
        raise ValueError("need one weight per state, and at least two states")
    total = float(sum(weights))
    if total <= 0:
        raise ValueError("weights must sum to something positive")
    weights = [w / total for w in weights]
    keys = set(states[0])
    for st in states[1:]:
        if set(st) != keys:
            raise ValueError("voice states disagree on their tensors; cannot mix")

    out: dict = {}
    for key in states[0]:
        tensors = [st[key] for st in states]
        shapes = [tuple(t.shape) for t in tensors]
        if len(set(shapes)) > 1:
            ranks = {len(s) for s in shapes}
            if len(ranks) > 1:
                raise ValueError(f"{key}: tensor ranks differ; cannot mix")
            rank = ranks.pop()
            differing = [
                axis for axis in range(rank)
                if len({s[axis] for s in shapes}) > 1
            ]
            if len(differing) != 1:
                raise ValueError(
                    f"{key}: shapes differ in {len(differing)} axes; cannot mix"
                )
            cut = [slice(None)] * rank
            cut[differing[0]] = slice(0, min(s[differing[0]] for s in shapes))
            tensors = [t[tuple(cut)] for t in tensors]
        if "float" not in str(tensors[0].dtype):
            # Integer bookkeeping (the caches' valid-length offsets): take
            # the elementwise minimum — a blended position should only count
            # as valid if it was valid in every component, and "first wins"
            # would make the mix order-dependent. (a+b-|a-b|)//2 spells min
            # in operators numpy and torch share.
            lowest = tensors[0]
            for tensor in tensors[1:]:
                lowest = (lowest + tensor - abs(lowest - tensor)) // 2
            out[key] = lowest
            continue
        blended = tensors[0] * weights[0]
        for tensor, weight in zip(tensors[1:], weights[1:]):
            blended = blended + tensor * weight
        out[key] = blended
    return out


def _concat_wavs(sources: list[Path], out: Path) -> None:
    """Stdlib-only reference builder: decode each 16-bit WAV, mix to mono,
    resample to the first file's rate (linear interp — reference audio, not
    playback), stop once the 30s prompt window is full, and normalize to a
    healthy speech level (_REF_RMS, capped by _REF_PEAK)."""
    import numpy as np

    rate: int | None = None
    parts: list = []
    total = 0
    for src in sources:
        with wave.open(str(src), "rb") as wav_in:
            if wav_in.getsampwidth() != 2:
                raise ValueError(
                    f"{src}: only 16-bit WAV is supported for multi-file "
                    "clones (single-file clones take any format pocket-tts reads)"
                )
            frames = wav_in.readframes(wav_in.getnframes())
            channels = wav_in.getnchannels()
            src_rate = wav_in.getframerate()
        data = np.frombuffer(frames, dtype="<i2").astype("float32") / 32767.0
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        if rate is None:
            rate = src_rate
        if src_rate != rate:
            n = int(round(len(data) * rate / src_rate))
            data = np.interp(
                np.linspace(0.0, len(data) - 1, n), np.arange(len(data)), data
            ).astype("float32")
        parts.append(data)
        total += len(data)
        if total >= rate * MAX_PROMPT_SECONDS:
            break
    joined = np.concatenate(parts)[: int(rate * MAX_PROMPT_SECONDS)]
    rms = float(np.sqrt((joined**2).mean()))
    peak = float(np.abs(joined).max())
    if rms > 1e-5:  # silence stays silence; it will fail at the prompt anyway
        joined = joined * min(_REF_RMS / rms, _REF_PEAK / peak)
    out.write_bytes(voice._encode_wav(joined, rate))


def _write_meta(dirpath: Path, **meta) -> None:
    meta["created"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (dirpath / "voice.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
