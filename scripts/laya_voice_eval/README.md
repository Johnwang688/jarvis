# Offline Jarvis voice routing benchmark

**Decision on November 18, 2026. Hold integration.** The English Laya checkpoint met the 100 ms warm p95 target on the RTX 5060 Laptop GPU. It chose an accepted route on 81 of 112 held-out transcripts (72.3%). The target was at least 95%, or 107 of 112. All predictions were recorded without desktop actions, typing or chat receipts.

## Measurements

| Held-out intent | Correct out of 16 |
|---|---:|
| Answer a question | 3 |
| Navigate to a website | 13 |
| Open an app | 15 |
| Switch to an open window | 8 |
| Search the web | 11 |
| Read the selection | 15 |
| Clarify an ambiguous or unresolved request | 16 |

Two errors proposed an unintended action. For “Switch over to the running Calculator” and “Switch from here to Word,” the classifier selected opening the application. The installed deterministic router from `fbc798ec` scored 20/112 in a router-only comparison. That build treats both keyboard shortcuts as the old hotkey, so this comparison also reflects the proposed input-mode change.

The public contract suite accepted 19/23 decisions. It covers the two Jarvis inputs, explicit literal typing, Ctrl+D bypass and destination grounding. A fused utterance can navigate to the unique agreed website or ask for clarification. Both input sources missed “Yo, open youtube.Com for me” and “Could you tell me the difference between nasa.gov and noaa.gov?” Ctrl+D and explicit literal controls made zero model calls.

Two synthesized recordings were decoded by the existing local Parakeet transcriber. “Open boo uh youtube.com” proposed a YouTube browser handoff. “Yo, open youtube.com for me” was classified as an app launch and the missing matching app caused a clarification. This stage measures proposals. Native Windows handoff, profile reply cancellation, full microphone-to-screen latency and the live answer backend belong to later integration gates.

| Serial, batch size one | Held-out run |
|---|---:|
| Warm median routing | 23.4 ms |
| Warm p95 routing | 28.9 ms |
| First routing inference | 552 ms |
| Checkpoint load after imports and device initialization | 1.88 s |
| Peak CUDA memory allocated / reserved | 2.26 / 2.44 GiB |
| CPU fallbacks | 0 |

Warm measurements include classification, synchronization and target resolution. The first model turn and zero-call bypasses are excluded. CUDA peak counters measure PyTorch allocator use. Loading the checkpoint used a populated disk cache. All runs used a 3 GiB host memory limit with zero swap.

## Reproduce

Use the English root checkpoint of [convaiinnovations/laya at the measured revision](https://huggingface.co/convaiinnovations/laya/tree/e333cb7c823cd1787276228260f56d02fbf5180b). The measured isolated environment used CPython 3.12.13, Laya 0.4.37, PyTorch 2.11.0+cu128, Transformers 5.23.0 and BF16 autocast. It does not need to be installed in the Jarvis environment. The SDK reported a temperature clamp for decisions with at least 11 choices. This experiment used seven.

Run from the repository root. Set `LAYA_PYTHON` to the isolated environment's Python and `BENCH_DIR` to a writable results directory. Put the following fields in `$BENCH_DIR/benchmark-runtime.json`, with the actual local checkpoint path.

```json
{
  "repo": "convaiinnovations/laya",
  "revision": "e333cb7c823cd1787276228260f56d02fbf5180b",
  "checkpoint": "/absolute/path/to/english/checkpoint",
  "device": "cuda:0"
}
```

```bash
python3 scripts/laya_voice_eval/make_corpus.py "$BENCH_DIR"
for split in contract heldout; do
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 \
    "$LAYA_PYTHON" scripts/bench_laya_voice.py \
    --runtime "$BENCH_DIR/benchmark-runtime.json" --compare-legacy \
    --input "$BENCH_DIR/$split-input.jsonl" --output "$BENCH_DIR/$split-predictions.json"
  python3 scripts/laya_voice_eval/score.py "$BENCH_DIR" "$split" \
    "$BENCH_DIR/$split-predictions.json" --out "$BENCH_DIR/$split-score.json"
done
```

The grader writes the per-route counts and all misses with expected and proposed targets, then exits 1 for a missed gate. Add `--baseline` to the scoring command to evaluate the embedded legacy proposals. The predictions include the raw class probabilities, per-turn latency, model configuration and input checksums.

The orchestrator authored and fingerprinted the evaluation text independently of the implementation. Development compared five configurations on public examples and seven additional controls. The original seven-way question performed best. The selected confidence floor of 0.65 and lead of 0.20 over the runner-up were frozen before scoring the held-out corpus. A lower 0.50/0.10 pair gained one development example and still missed the required YouTube paraphrase. The published held-out text can serve as a regression suite; use a fresh blind set to judge another iteration.
