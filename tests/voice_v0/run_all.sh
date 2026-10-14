#!/usr/bin/env bash
# Re-run every V0 probe, one at a time, each inside a memory/CPU-capped scope.
#
#   JARVIS_VOICE_V0=1 tests/voice_v0/run_all.sh [step ...]
#
# Steps (default: all, in this order): fetch fixtures special browser vadquality stt ns contention speaker kws turn
# Needs the probe venv (uv venv ~/.cache/jarvis-voice-v0/venv; uv pip install sherpa-onnx numpy scipy
# soundfile onnxruntime kokoro-onnx playwright==<repo's> sentencepiece transformers psutil) and node.
# Results land in ~/.cache/jarvis-voice-v0/results/*.json. Nothing here runs in the normal test sweep:
# every probe exits 0 with "skipped" unless JARVIS_VOICE_V0=1.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${JARVIS_VOICE_V0_PYTHON:-$HOME/.cache/jarvis-voice-v0/venv/bin/python}"
export JARVIS_VOICE_V0="${JARVIS_VOICE_V0:-}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
export PYTHONPATH="$REPO"          # fixtures use the repo's Kokoro config; harmless elsewhere

if [ "$JARVIS_VOICE_V0" != "1" ]; then
  echo "skipped: set JARVIS_VOICE_V0=1 (downloads models, uses ~4 GB disk, runs ~45 min)"; exit 0
fi

# The machine is shared: 3 GB, no swap, at most 8 cores. Use MEM=4G only for the STT steps (Parakeet peaks ~1.9 GB).
cap() { local mem="$1"; shift
  systemd-run --user --scope -q -p MemoryMax="$mem" -p MemorySwapMax=0 -p CPUQuota=800% -- "$@"; }

steps=("$@"); [ ${#steps[@]} -eq 0 ] && steps=(fetch fixtures special browser vadquality stt ns contention speaker kws turn)
for s in "${steps[@]}"; do
  echo "== $s"; case "$s" in
    fetch)      cap 3G "$PY" "$HERE/fetch_models.py" ;;
    fixtures)   cap 3G "$PY" "$HERE/make_fixtures.py" ;;
    special)    cap 3G "$PY" "$HERE/make_special_fixtures.py" ;;
    browser)    cap 3G "$PY" "$HERE/probe_browser.py" --only silero,rnnoise,kws,combo ;;
    vadquality) cap 3G "$PY" "$HERE/probe_browser.py" --only vadquality ;;
    stt)        cap 4G "$PY" "$HERE/probe_stt.py" --threads 1,2,4,8,24 --clips 96 --noisy --noisy-threads 4 ;;
    ns)         cap 4G "$PY" "$HERE/probe_stt.py" --ns --threads 4 ;;
    contention) cap 4G "$PY" "$HERE/probe_stt.py" --contention ;;
    speaker)    cap 3G "$PY" "$HERE/probe_speaker.py" --threads 1,4 ;;
    kws)        cap 3G "$PY" "$HERE/probe_kws_native.py" ;;
    turn)       cap 3G "$PY" "$HERE/probe_smart_turn.py" --threads 1,4 ;;
    *) echo "unknown step $s" >&2; exit 2 ;;
  esac
done
