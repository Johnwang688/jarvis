"""Paid probe: local Parakeet vs the cloud STT model, word error rate on synthetic speech (V13, Q6).

Plan section 7, question 6 (answered): local speech-to-text becomes the default only if its WER is
within 1 point of the cloud's. This runs the SAME synthetic Kokoro clips (V0's fixtures) through

  - the product's local engine (`jarvis/stt_local.py`, Parakeet-TDT-0.6B-v3 int8, 2 threads), and
  - OpenRouter's transcription endpoint with `nvidia/parakeet-tdt-0.6b-v3` (the model the cloud
    path calls first), sending the same 16 kHz 16-bit WAV bytes the HUD sends,

and prints each WER, their difference in points, a paired-bootstrap interval, and the verdict.

MONEY. The only paid step is the cloud request per clip. The probe

  - refuses to run without `--yes-spend` (`--dry-run` runs local only: no key, no network, no spend);
  - estimates each clip's cost before sending it (audio minutes x `--price-per-min`, default
    $0.006/min, 4x the $0.0015/min listed for Parakeet, so the estimate is an overestimate; a cost the
    response itself reports is used when it is larger), and STOPS before the running total would pass
    `--max-usd` (default and hard ceiling $0.24, under the approved $0.25);
  - makes at most one request per clip, no retries, and gives up after 5 failures in a row.

SECRETS. The key comes from the environment (`OPENROUTER_API_KEY`) and is never printed (it is
scrubbed from every message). The probe imports nothing from `jarvis` except `stt_local.py`, loaded
from its file under stub `jarvis`/`jarvis.config` modules, because `jarvis.config` reads the
credential `.env` at import. It opens no `.env*` file. There is no .env fallback: export the
variable in the shell that runs it.

RUN (the lead; ~$0.01 at the listed price for the default 96 clips, 7 minutes of audio):

  systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0 -- \\
    env OPENROUTER_API_KEY=... PYTHONPATH=<worktree> \\
    ~/.cache/jarvis-voice-v0/venv/bin/python tests/stt_compare_probe.py --yes-spend

The V0 venv is the one with sherpa-onnx. Free rehearsal first: add `--dry-run` instead of `--yes-spend`.
Fixtures: `JARVIS_VOICE_V0=1 tests/voice_v0/run_all.sh fixtures` makes ~/.cache/jarvis-voice-v0/fixtures.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "voice_v0"))

import numpy as np  # noqa: E402

import common  # noqa: E402  (tests/voice_v0/common.py: WER helpers; pure numpy/stdlib)

HARD_CAP_USD = 0.24
DEFAULT_PRICE_PER_MIN = 0.006
LISTED_PRICE_PER_MIN = 0.0015
DEFAULT_MODEL = "nvidia/parakeet-tdt-0.6b-v3"
URL = "https://openrouter.ai/api/v1/audio/transcriptions"
MAX_CONSECUTIVE_FAILURES = 5
KEY_ENV = "OPENROUTER_API_KEY"
WITHIN_POINTS = 1.0


# ---- pure parts (tests/stt_compare_probe_check.py exercises these without any model or network) -------

def select_clips(manifest: list[dict], limit: int) -> list[dict]:
    """The same spread as V0's STT probe: voice k % V, sentence (5k + k // V) mod S, Kokoro only."""
    by_voice: dict[str, dict[int, dict]] = {}
    for c in manifest:
        if c.get("engine") == "kokoro":
            by_voice.setdefault(c["voice"], {})[c["sentence"]] = c
    if not by_voice:
        return []
    voices = sorted(by_voice)
    n_sent = len(next(iter(by_voice.values())))
    picked, seen = [], set()
    for k in range(limit):
        v = voices[k % len(voices)]
        sentences = sorted(by_voice[v])
        c = by_voice[v][sentences[(5 * k + k // len(voices)) % len(sentences)]]
        if c["file"] not in seen:
            seen.add(c["file"])
            picked.append(c)
        if len(picked) >= n_sent * len(voices):
            break
    return picked


def estimate_usd(seconds: float, price_per_min: float) -> float:
    return max(0.0, seconds) / 60.0 * price_per_min


class Meter:
    """Running spend. `allow(next_cost)` is False once adding it would pass the cap."""

    def __init__(self, cap_usd: float):
        if not (0 < cap_usd <= HARD_CAP_USD):
            raise ValueError(f"--max-usd must be above 0 and at most {HARD_CAP_USD}")
        self.cap = cap_usd
        self.spent = 0.0
        self.requests = 0

    def allow(self, next_cost: float) -> bool:
        return self.spent + next_cost <= self.cap

    def charge(self, estimate: float, reported: object = None) -> float:
        cost = estimate
        if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported > estimate:
            cost = float(reported)
        self.spent += cost
        self.requests += 1
        return cost


def scrub(text: str, secret: str) -> str:
    return text.replace(secret, "<key>") if secret else text


def multipart(fields: dict[str, str], filename: str, mime: str, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                  f"Content-Type: {mime}\r\n\r\n").encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def per_clip_errors(ref: str, hyp: str) -> tuple[int, int]:
    s, d, i, n = common.edit_counts(common.norm_words(ref), common.norm_words(hyp))
    return s + d + i, n


def compare(refs: list[str], local: list[str], cloud: list[str], *, resamples: int = 2000, seed: int = 7) -> dict:
    """WER of each side on the same clips, their difference in points, and a paired bootstrap of it."""
    le = np.array([per_clip_errors(r, h)[0] for r, h in zip(refs, local, strict=True)], dtype=float)
    ce = np.array([per_clip_errors(r, h)[0] for r, h in zip(refs, cloud, strict=True)], dtype=float)
    n = np.array([per_clip_errors(r, "")[1] for r in refs], dtype=float)
    words = max(float(n.sum()), 1.0)
    local_wer, cloud_wer = 100 * le.sum() / words, 100 * ce.sum() / words
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(resamples):
        idx = rng.integers(0, len(refs), len(refs)) if len(refs) else np.array([], dtype=int)
        w = max(float(n[idx].sum()), 1.0)
        diffs.append(100 * (le[idx].sum() - ce[idx].sum()) / w)
    lo, hi = (float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))) if diffs else (0.0, 0.0)
    diff = local_wer - cloud_wer
    return {
        "clips": len(refs), "ref_words": int(words),
        "local_wer_pct": local_wer, "cloud_wer_pct": cloud_wer, "local_minus_cloud_points": diff,
        "bootstrap95_points": [lo, hi],
        "local_within_1_point": diff <= WITHIN_POINTS,
        "ci_upper_within_1_point": hi <= WITHIN_POINTS,
    }


# ---- the engine under test ---------------------------------------------------------------------------

def load_stt_local(model_dir: Path | None):
    """`jarvis/stt_local.py` loaded from its file under stub `jarvis` and `jarvis.config` modules,
    so nothing reads the credential `.env`. Returns the module."""
    cfg = types.ModuleType("jarvis.config")
    default = Path.home() / ".cache" / "jarvis-stt" / "parakeet-tdt-0.6b-v3-int8"
    cfg.STT_LOCAL_MODEL_DIR = model_dir or Path(os.environ.get("JARVIS_STT_LOCAL_MODEL", default))
    cfg.STT_LOCAL_THREADS = 2
    cfg.STT_LOCAL_IDLE_MIN = 10.0
    pkg = types.ModuleType("jarvis")
    pkg.__path__ = []
    pkg.config = cfg
    sys.modules.setdefault("jarvis", pkg)
    sys.modules["jarvis.config"] = cfg
    spec = importlib.util.spec_from_file_location("jarvis.stt_local", ROOT / "jarvis" / "stt_local.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["jarvis.stt_local"] = module
    spec.loader.exec_module(module)
    return module


def find_model_dir(arg: str | None) -> Path | None:
    if arg:
        return Path(arg)
    product = Path(os.environ.get("JARVIS_STT_LOCAL_MODEL", Path.home() / ".cache" / "jarvis-stt" / "parakeet-tdt-0.6b-v3-int8"))
    v0 = Path(os.environ.get("JARVIS_VOICE_MODELS", Path.home() / ".cache" / "jarvis-voice-models")) \
        / "parakeet-tdt-0.6b-v3-int8" / "x" / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
    for d in (product, v0):
        if all((d / f).is_file() for f in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")):
            return d
    return None


def cloud_transcribe(wav: bytes, model: str, key: str) -> tuple[str, object]:
    body, ctype = multipart({"model": model}, "audio.wav", "audio/wav", wav)
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": ctype, "X-Title": "Jarvis"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read())
    usage = data.get("usage") if isinstance(data, dict) else None
    reported = usage.get("cost") if isinstance(usage, dict) else None
    return str(data.get("text") or "").strip(), reported


# ---- main ------------------------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    arg = common.parse_args(argv)
    dry = "--dry-run" in arg
    if "--yes-spend" not in arg and not dry:
        print("refusing to run: this makes paid cloud requests (about $0.01 at the listed price, capped at "
              f"${HARD_CAP_USD}). Re-run with --yes-spend, or --dry-run for a free local-only rehearsal.")
        return 2
    try:
        cap = float(arg.get("--max-usd", HARD_CAP_USD))
        meter = Meter(cap)
        price = float(arg.get("--price-per-min", DEFAULT_PRICE_PER_MIN))
        limit = int(arg.get("--clips", 96))
    except ValueError as exc:
        print(f"bad argument: {exc}")
        return 2
    if price <= 0 or limit < 1:
        print("--price-per-min and --clips must be positive")
        return 2
    model = str(arg.get("--model", DEFAULT_MODEL))
    key = os.environ.get(KEY_ENV, "")
    if not dry and not key:
        print(f"{KEY_ENV} is not set in the environment (it is read from there only, never from a file).")
        return 2

    manifest_path = common.FIXTURES / "manifest.json"
    if not manifest_path.is_file():
        print(f"no fixtures at {common.FIXTURES}: run  JARVIS_VOICE_V0=1 tests/voice_v0/run_all.sh fixtures")
        return 2
    clips = select_clips(json.loads(manifest_path.read_text()), limit)
    model_dir = find_model_dir(arg.get("--model-dir"))
    if model_dir is None:
        print("no local model: run  python -m jarvis.stt_local fetch  (or pass --model-dir)")
        return 2
    try:
        stt_local = load_stt_local(model_dir)
        engine = stt_local.Engine(threads=2, schedule=False)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot load the local engine ({type(exc).__name__}): is sherpa-onnx installed in this Python?")
        return 2

    wavs = {c["file"]: (common.FIXTURES / c["file"]).read_bytes() for c in clips}
    seconds = {c["file"]: float(c.get("seconds") or 0.0) for c in clips}
    total_min = sum(seconds.values()) / 60
    print(f"{len(clips)} clips, {total_min:.1f} min of synthetic speech; local engine: 2 threads; cloud model: {model}")
    print(f"estimated cloud cost at ${price}/min: ${estimate_usd(sum(seconds.values()), price):.4f} "
          f"(listed ${LISTED_PRICE_PER_MIN}/min: ${estimate_usd(sum(seconds.values()), LISTED_PRICE_PER_MIN):.4f}); cap ${meter.cap}")

    # Local first: free, and it fixes which clips the cloud will be asked about.
    local_text, local_ms = {}, []
    for c in clips:
        t0 = time.perf_counter()
        local_text[c["file"]] = engine.transcribe(wavs[c["file"]], "audio/wav", timeout=120)
        local_ms.append((time.perf_counter() - t0) * 1000)
    print(f"local done: p50 {np.percentile(local_ms, 50):.0f} ms (first includes the model load)")

    cloud_text, cloud_ms, failures, consecutive, stopped = {}, [], 0, 0, ""
    if not dry:
        for c in clips:
            est = estimate_usd(seconds[c["file"]], price)
            if not meter.allow(est):
                stopped = f"stopped before the ${meter.cap} cap: next clip would take the estimate from ${meter.spent:.4f} to ${meter.spent + est:.4f}"
                break
            t0 = time.perf_counter()
            try:
                text, reported = cloud_transcribe(wavs[c["file"]], model, key)
            except urllib.error.HTTPError as exc:
                failures += 1
                consecutive += 1
                print(f"  cloud request failed: HTTP {exc.code}")
                meter.charge(0.0)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                consecutive += 1
                print("  cloud request failed: " + scrub(type(exc).__name__, key))
                meter.charge(0.0)
            else:
                consecutive = 0
                cloud_ms.append((time.perf_counter() - t0) * 1000)
                cloud_text[c["file"]] = text
                meter.charge(est, reported)
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                stopped = f"stopped after {MAX_CONSECUTIVE_FAILURES} failures in a row"
                break

    done = [c for c in clips if c["file"] in cloud_text]
    result: dict = {"model": model, "clips_selected": len(clips), "audio_min": total_min, "dry_run": dry,
                    "spent_usd_estimate": round(meter.spent, 5), "cap_usd": meter.cap, "requests": meter.requests,
                    "failures": failures, "stopped": stopped,
                    "local_latency_ms": {"p50": float(np.percentile(local_ms, 50)), "p95": float(np.percentile(local_ms, 95))}}
    refs_all = [c["text"] for c in clips]
    result["local_wer_all_selected_pct"] = 100 * common.wer(refs_all, [local_text[c["file"]] for c in clips])["wer"]
    if done:
        refs = [c["text"] for c in done]
        result["comparison"] = compare(refs, [local_text[c["file"]] for c in done], [cloud_text[c["file"]] for c in done])
        result["cloud_latency_ms"] = {"p50": float(np.percentile(cloud_ms, 50)), "p95": float(np.percentile(cloud_ms, 95))}
        result["disagreements"] = [
            {"ref": c["text"], "local": local_text[c["file"]], "cloud": cloud_text[c["file"]]}
            for c in done if common.norm_words(local_text[c["file"]]) != common.norm_words(cloud_text[c["file"]])][:15]
    common.emit("stt_compare", result)
    if stopped:
        print(stopped)
    cmp = result.get("comparison")
    if cmp:
        verdict = "YES" if cmp["local_within_1_point"] else "NO"
        print(f"\nlocal WER {cmp['local_wer_pct']:.2f}%  cloud WER {cmp['cloud_wer_pct']:.2f}%  "
              f"difference {cmp['local_minus_cloud_points']:+.2f} points "
              f"(95% bootstrap {cmp['bootstrap95_points'][0]:+.2f} .. {cmp['bootstrap95_points'][1]:+.2f}) over "
              f"{cmp['clips']} clips / {cmp['ref_words']} words")
        print(f"local within 1 point of cloud (the Q6 condition): {verdict}"
              + ("" if cmp["ci_upper_within_1_point"] else "  (interval upper end is above 1 point)"))
        print(f"estimated spend ${meter.spent:.4f} of ${meter.cap}")
        print("synthetic speech only: this bounds the models from below and says nothing about the owner's voice.")
    elif not dry:
        print("no cloud clip completed: nothing to compare")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
