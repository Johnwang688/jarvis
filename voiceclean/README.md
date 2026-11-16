# VA-3 offline data and evaluation tools

This is an offline tool, separate from the daemon. It imports no Jarvis runtime
configuration. Jarvis dependencies are the pure guard, rules and shape-based
scrubber (`jarvis.v2.redact`); credential values and credential files are never read.
Python 3.14 and the standard library suffice except for real audio backends.

Data defaults to `~/.local/share/jarvis/va3-data`. Set `VOICECLEAN_DATA_DIR` or pass
`--data-dir PATH` before or after the subcommand. The directory must be outside
any git work tree (including linked worktrees with a `.git` file). Directories
are 0700 and all output files are opened with mode 0600, including replacements.
Only the deliberate owner-run `record` step saves audio. Other steps print
counts and metrics only; `record` also shows suggestions, prompts and the owner's
transcript for review. No step reads `.env` files.

Run from this checkout so another installed Jarvis cannot be imported:

```bash
export PYTHONPATH="$PWD"
PY=/home/johnw/projects/Jarvis/.venv/bin/python
# Explicit owner-run command: reads only owner turns from the four transcript trees.
"$PY" -m voiceclean.data owner-text
# Supply an already downloaded oasst2 message export.
"$PY" -m voiceclean.data public --oasst2 /private/downloads/oasst2.messages.jsonl.gz
"$PY" -m voiceclean.data synth --seed 42 --per-source 5
"$PY" -m voiceclean.data roundtrip --limit 100 --threads 2 --seed 42
# Optional paid step. Set OPENROUTER_API_KEY in the environment yourself.
"$PY" -m voiceclean.data teach --model openai/gpt-oss-20b --task messify --limit 20 --max-usd 0.02
"$PY" -m voiceclean.data teach --model deepseek/deepseek-r1 --task clean --limit 20 --max-usd 0.02
"$PY" -m voiceclean.data build
"$PY" -m voiceclean.data eval --split dev --system b0
"$PY" -m voiceclean.data eval --split test --system b1 --limit 100
# Interactive owner-run command only; requires stdin AND stdout TTYs.
"$PY" -m voiceclean.data record
```

Obtain the messages export from the official
[OpenAssistant oasst2 dataset](https://huggingface.co/datasets/OpenAssistant/oasst2)
(see its dataset card and Apache-2.0 licence). Download it yourself with a browser
or your preferred dataset tools, exporting one message per JSONL line with
`role`, `lang`, `text`, `message_id`. Plain JSONL and gzip are accepted. There is
no downloader in this package. Only English human prompter messages and short
prose sentence groups are retained; assistant turns are excluded. Each row records
the exact input file's SHA-256, message id and Apache-2.0 licence.

`owner-text` ports the prototype's source precedence (v2, v1, Claude, Codex),
whole-message drop rules, shape-only credential detection and dedupe, then adds
a conservative prose filter. It reads `Path.home()` at extraction time. It never
reads assistant turns or noninteractive Codex exec prompts. Owner rows are marked
`origin: owner`; extracted text, derived rows, adapters and checkpoints must never
leave this machine. The source licence label `private-owner` is a privacy marker,
not a public licence grant.

Templates include protected VEX terms, team numbers, weekdays, months, times and
quantities. Optionally create `names.txt` in the private data directory (one name
per line, mode 0600). Names-bearing templates retain `origin: template` and are
marked `owner_derived: true`; teachers refuse them too. `local-template` identifies
locally generated sentences without asserting a third-party licence. `synth`
merges owner/public sources and templates (`--template-count`, default 200) and
records the per-pair seed, run seed and applied corruption operations. Highly
ambiguous or excessive corruptions fall back to the source. This guarantees the
deletion-only invariant without teaching an invalid correction.
Sources whose identity pair already fails the checker are skipped and counted.
In particular, literal spoken-punctuation words such as "comma" in a target
cannot be preserved under the specified asymmetric normalization.

Round trips speak synthetic pairs in memory using a random voice/speed. Install
`kokoro_onnx`, `sherpa_onnx`, `numpy` separately only for this step. Models default
to `~/.cache/jarvis-tts/{kokoro-v1.0.onnx,voices-v1.0.bin}` and
`~/.cache/jarvis-stt/parakeet-tdt-0.6b-v3-int8/` with `encoder.int8.onnx`,
`decoder.int8.onnx`, `joiner.int8.onnx`, `tokens.txt`. Override using
`--kokoro-model`, `--kokoro-voices`, `--parakeet-dir`, `--voices` and `--threads`.
Kokoro is Apache-2.0; NVIDIA Parakeet is CC BY 4.0. Attribution is retained in
provenance and the data card. Recognizer substitutions remain tagged
`guard_compatible: false`; the deletion-only guard has no homophone exception.

The exact teacher allowlist is:

| OpenRouter model id | Licence |
| --- | --- |
| openai/gpt-oss-120b | Apache-2.0 |
| openai/gpt-oss-20b | Apache-2.0 |
| google/gemma-4-31b-it | Apache-2.0 |
| deepseek/deepseek-r1 | MIT |
| qwen/qwen3.8-27b | Apache-2.0 |

These ids/licences follow the owner's supplied spec; verify the model licences
and provider ids before actual use. Every other id is refused before any request,
including proprietary Anthropic and OpenAI GPT models. Teachers receive only
public/template text, never owner-derived rows. Requests explicitly deny provider
data collection, request usage accounting, and use temperatures 0.7–1.0. Only the
message content is used; reasoning is discarded. Every accepted pair passes the
same checker. `messify` consumes synthetic pairs; `clean` also consumes round-trip
pairs. Prior teacher outputs are excluded to avoid recursive generations.
The default budget is $0.02: no further request starts once recorded
spend reaches it. One request can cross the budget because its cost is returned
after completion; missing/invalid cost aborts the run. Keep limits small. No
teacher call was made during development or tests.

`build` reads all root-level pair JSONL files, dedupes normalized messy/target
pairs, retains origin and provenance, and writes `dataset/{train,dev,test}.jsonl`
and `dataset/data_card.json`. It excludes source-only files and prior datasets.
The source sentence id, preserved through every transformation, determines the
stable 90/5/5 hash split. Source ids normalize case, punctuation, contractions and
numbers, so presentations of the same sentence stay together. Split sizes are
approximate, not quotas. The card
records counts, guard compatibility, licences, input hashes, seeds, teacher ids
and the fixed machine-local privacy note. Re-running a step with the same output
name replaces that file; use different seeds to retain separate runs.

`jarvis.v2.dictation_guard` is the authoritative, side-effect-free runtime guard;
the offline `pair_check` delegates to it. `jarvis.v2.dictation_rules.clean(raw,
app='other')` removes hesitation fillers and immediate repeats, converts narrow
spoken punctuation and fixes case and final punctuation. Terminal mode only
removes fillers and collapses whitespace. Every edit is checked; rejection
returns the original input. Neither module imports configuration. The guard
removes spoken punctuation only from the raw side, so identity pairs containing
literal `period`, `colon`, etc. can fail too; the required raw fallback cannot
guarantee a passing guard verdict for every possible input.

`eval` reads `dataset/dev.jsonl` (or `--split test`) followed by every `gold/*.json`
in filename order. A missing dataset is allowed for gold-only evaluation.
`--limit N` caps this combined stream. `b0` types raw input; `b1` uses guarded
rules. `--system MODULE:FUNC` imports a local callable `f(raw) -> str`, then
checks its result and falls back through rules to raw. Models are not bundled
or loaded by the built-in baselines. Reports contain aggregates only and are
saved as `eval/<UTC timestamp>-<system>.json`, mode 0600. The printed table gives
counts, content WER, formatting WER, exact-match and rejection rates, rejection
reason counts, and system latency in milliseconds for each suite. WER pools edit
counts and reference lengths across rows (denominator at least one); content
uses guard token normalization on both target and typed text, formatting uses
whitespace tokens with case and punctuation intact. Exact match compares strings.
Latency uses nearest-rank p50/p95 and includes rules, model call and guard choice,
excluding file IO and metric calculation. Empty suites have zero-valued metrics.

`record` presents 30 everyday suggestions; the owner may say anything. Enter
starts recording and Enter stops it, with a 30-second hard cap. After local
Parakeet transcription, type the desired clean target: empty skips, `r` redoes
the same suggestion, and `q` quits. Skipped, redone and unreviewed clips are
deleted, including on EOF or interrupted recording. Reviewed clips save a WAV
and JSON under `gold/`, both 0600 in a 0700 directory, with the guard compatibility
flag even when the target cannot pass. This is evaluation data only: `build`
does not read gold rows. The real backend requires `parecord` and the same
Parakeet/numpy installation as `roundtrip`; use `--parakeet-dir` and `--threads`
to override its defaults. Backends load only after an interactive recording.
Tests inject IO, recorder and transcriber; they never open a microphone or load
real models.

Free, fake-HOME checks (network and real model imports are blocked):

```bash
PYTHONPATH=$PWD /home/johnw/projects/Jarvis/.venv/bin/python tests/voiceclean_check.py
PYTHONPATH=$PWD /home/johnw/projects/Jarvis/.venv/bin/python tests/voiceclean_eval_check.py
PYTHONPATH=$PWD /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/dictation_guard_check.py
PYTHONPATH=$PWD /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/dictation_rules_check.py
```
