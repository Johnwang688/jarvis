# VA-3c private training-data generator

This is an offline tool, separate from the daemon. It imports no Jarvis runtime
configuration. The only Jarvis dependency is the pure, shape-based scrubber in
`jarvis.v2.redact`; credential values and credential files are never read.
Python 3.14 and the standard library suffice except for real audio backends.

Data defaults to `~/.local/share/jarvis/va3-data`. Set `VOICECLEAN_DATA_DIR` or pass
`--data-dir PATH` before or after the subcommand. The directory must be outside
any git work tree (including linked worktrees with a `.git` file). Directories
are 0700 and all output files are opened with mode 0600, including replacements.
No audio is saved. Every step prints counts only. No step reads `.env` files.

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

The checker mirrors the plan and is never a runtime authorization component.
VA-3a's runtime guard is authoritative and should replace this implementation
once a side-effect-free guard is available.

Free, fake-HOME checks (network and real model imports are blocked):

```bash
PYTHONPATH=$PWD /home/johnw/projects/Jarvis/.venv/bin/python tests/voiceclean_check.py
```
