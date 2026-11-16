# Voice anywhere, amendment 1: VA-3 as a local cleanup model (plan, 2026-11-16)

Status: **proposal**. Nothing is built. Amends the VA-3 row of
[the voice-anywhere plan](2026-11-15-voice-anywhere-plan.md); every other row,
owner decision (VA-D1, VA-D2) and boundary there still holds. The owner asked for
this amendment on 2026-11-16 ("Draft VA-3 amendment").

## Why

VA-3 today: "Local rules first; an optional Haiku pass behind a setting. Haiku adds
a round trip; off by default until VA-0 says there is room." Rules handle fillers and
capitals but not the things that make dictation feel finished: punctuation across
clauses, self-corrections ("Tuesday, no wait, Wednesday"), repeated starts. Haiku
handles them but sends the owner's words to the cloud and adds a network round trip
to the step the owner called "too slow".

A small model fine-tuned for this one job, running on this machine, removes both
costs. Measured facts that make it practical here:

- The machine has an **RTX 5060 Laptop GPU, 8 GB**, visible from WSL (`nvidia-smi`,
  2026-11-16), 24 cores, 21 GB RAM. Parakeet already runs on CPU (int8,
  sherpa-onnx), so the GPU is free for this.
- Kokoro (TTS) and Parakeet (STT) are already installed. Together they produce
  realistic speech-recognition errors for training data with no new dependency.

## Hard rule: the cleaner may not add words

VA-D2 lets dictation type into **any app, terminals included**. The cleaner's output
is what gets typed. A model that "helpfully" adds or answers turns a dictated
sentence into keystrokes the owner never said, possibly in a shell. So:

1. **A deterministic guard, not the model, decides what may be typed.** After the
   model runs, `guard(raw, cleaned)` accepts only if every word in the output comes
   from the input, after normalisation (case, punctuation, a fixed table of
   number words → digits and spoken punctuation such as "comma"). Deleting words is
   allowed (fillers, false starts, the abandoned half of a self-correction); adding,
   substituting or reordering beyond that table is not. Output also may not contain
   control characters or newlines (line breaks belong to VA-2's "new line"
   command), and may not delete more than a set share of the words (default 60%).
2. **On any rejection the rules output is typed instead**, never the model's. The
   diagnostics count rejections; they never log the text.
3. **VA-2's command grammar runs on the raw transcript, before cleanup.** The cleaner
   never sees, creates or removes "send it", "new line" or "scratch that".
4. **Terminals get rules only** (VA3-D1). In a shell, a comma or a capital letter
   the model chose can change a command.
5. **The guard is authorization code.** It decides what text may be typed into any
   app, the same reason `voice_link.py` and `windows/voice_agent.py` are in
   `SELF_PROTECTED`. The guard module goes there too (VA3-D2).

The same guard is the hard gate in the training reward (below), so RL cannot learn to
add words: any output the guard rejects scores the minimum.

## Where it runs

On the daemon side, after `remote_stt.transcribe` returns text and before VA-2 types
it: raw transcript → command grammar → rules → model → guard → typed text. The model
runs under llama.cpp with CUDA (a 0.8–2B model at 4-bit is about 0.5–1.4 GB of
VRAM). Loaded on first use, like `stt_local`. Optional: if the runtime or the weights
are missing, VA-3 is rules only and says so in Settings → Voice.

The Windows agent must report the foreground app's category (terminal / other) with
each clip for rule 4. That change lands in VA-2, in the self-protected Windows files,
written by the lead, not an agent.

Latency target (estimate, to be checked against VA-0's receipts): cleanup at most
150 ms p50 and 400 ms p95 for an utterance of up to 40 words. If a prompted
off-the-shelf model already meets the quality bar inside this budget, training is
skipped.

## Licensing and provenance

- **Base model: Qwen3.5-0.8B** (owner's choice, 2026-11-16, VA3-D5), with
  Qwen3.5-2B only if it falls short (Apache 2.0,
  released 2026-03, `-Base` variants exist). This is the newest small Qwen found on
  2026-11-16; Qwen3.8 has no small sizes yet. **Gemma 4 E2B** (Apache 2.0,
  2026-04; 2.3B effective, 5.1B with embeddings) is the comparison in B2. Before
  VA-3b: read each model's LICENSE file on Hugging Face (the license was confirmed
  only from mirrors) and check that llama.cpp supports the architecture.
  Not a fit: TypeSafe's Jev is a closed, API-only decision model, not a text
  rewriter, and cannot be downloaded or fine-tuned; its open look-alikes are
  decision models too.
- **Several open-weight teachers** give the dataset variety, so it is not all
  one script's style. Each needs a training-permitting licence (Apache 2.0 or MIT),
  for example gpt-oss-120b, Qwen3.8-27B, Gemma 4 31B or DeepSeek-R1. Check each
  licence at the time of use. Teachers write two things:
  - naturalistic messy dictation from clean text;
  - alternative valid cleanups of messy text.

  Only outputs that pass the guard are kept. Teachers receive synthetic or public
  text only, never owner-written text, so the routing rule on personal data does
  not arise.
- **No outputs from Claude, GPT or any other proprietary model in training data.**
  Their terms forbid training competing models on outputs. Anthropic's Commercial
  Terms D.4(a) cover Jarvis's Haiku calls through OpenRouter BYOK. OpenAI's terms
  carry an equivalent clause for the GPT-6.x models. The owner asked on 2026-11-16 to
  include Haiku, Sonnet, Opus 5.5, GPT-6.1 Sol and GPT-6 Astra outputs for variety.
  The lead declined to build that part. The open-weight teachers above give the
  same variety. The exclusion also covers Jarvis transcripts, whose assistant turns
  are mostly Haiku 5.5. Calling Haiku at *runtime* (the existing optional pass) is
  unaffected.
- Source text is **human-written** with a licence that permits training. Many public
  "chat" datasets were generated by GPT-4 or Claude, so check each dataset card.
- Kokoro (Apache 2.0) and Parakeet (CC BY 4.0) may generate training audio and
  transcripts. Keep attribution in the data card.
- Every example records its source, so a dataset can be dropped later.

## Data

1. **Synthetic messy/clean pairs, made by a script.** Take clean human-written
   sentences in a conversational register (messages, prompts to an assistant, short
   notes) and corrupt them: insert fillers, repeat starts, add self-corrections
   ("…on Tuesday, no wait, Wednesday" with target "…on Wednesday"), strip punctuation
   and case. The script writes both sides, so the target is exact.
2. **Real recognizer errors.** Speak the corrupted text with Kokoro (several
   voices and speeds) and transcribe it with Parakeet. The pair is (Parakeet's text,
   clean target). This teaches the mistakes this pipeline actually makes.
3. **Protected-term lists.** VEX vocabulary, team numbers, project names and people's
   names appear in templates, so the model learns to leave them alone.
4. **Owner-written text** (the owner's own messages, never assistant turns) is the
   best in-domain source (VA3-D4: yes, locally). It and every pair, checkpoint and
   adapter trained on it stay on this machine and are never published or pushed.
   Extraction must keep only owner-authored turns. A Jarvis transcript's assistant
   turns are Haiku output, and an owner message that quotes an assistant reply must
   be dropped, not trimmed. No credential-bearing text: run the shared secret
   scrubber over every message before it enters a dataset.

   **First extraction, 2026-11-16:**
   - **Count:** 2,054 messages, 66k words; median 23 words per message, p90 68.
   - **By source:**
     - desktop Claude chats (`entrypoint` `claude-desktop`/`cli`, main thread
       only): 866;
     - v2 HUD/phone messages with `origin: owner`: 729, of which 25 were spoken;
     - v1 sessions, role `you`: 459;
     - interactive Codex (`codex-tui`): 0. All 101 were over the length cap,
       likely injected context. Non-interactive `codex exec` prompts are
       agent-written and always excluded.
   - **Drop rules:**
     - harness text: task notifications, command tags, compaction summaries;
     - pasted content, code fences, headings, lists of 3+ items;
     - over 1,200 characters;
     - any credential shape (`redact.shapes` + `labelled_values`, shape-only, so
       no credential file is read). The whole message is dropped, not redacted.
   - **Output:** `~/.local/share/jarvis/va3-data/owner_text.jsonl`
     (folder 0700, file 0600), with each row's source and session.
   - **Prototype extractor:** `~/.cache/jarvis-lead/va3/extract_owner_text.py`.
     It moves into the repo with VA-3c.
   - **Known noise:** some kept messages are pasted error text, not prose. VA-3c
     should add a prose filter.

**First VA-3c run (2026-11-16, live, local).**
- **Owner text:** 1,597 messages after the prose filter.
- **Synthetic set:** 5,391 pairs from those messages plus 200 templates (3 per
  source). The dataset holds 5,319 pairs after dedupe: 4,736 train, 276 dev, 307
  test.
- **Corruption density:** the first rates gave a median of 1.54 messy tokens per
  target token, with a filler every few words. They were lowered and a per-pair
  intensity added, which gives a median of 1.2 and a p90 of 1.55.
- **Round trip:** 20 clips took 62 s wall (about 3 s per clip, CPU). Only **4 of
  20** were `guard_compatible`. The words the recognizer lacked were jargon
  (`enum`, `worktree`, `drivetrain`, `dist`) and **typos in the owner's own text**
  (`soemthing`, `seperate`, `shoudl`), which Parakeet spells correctly.
- **Open follow-ups:**
  - a typo filter for owner text, using the oasst2 vocabulary once that file is
    fetched;
  - open question 2 decides whether jargon fixes are ever allowed.

## Evaluation (built before any training)

- **Gold set:** about 100 real dictations by the owner at their own mic, with a clean
  target written by the owner. Real audio is required. The plan notes that no voice
  package has yet been accepted with the owner's real voice. Recorded by a deliberate
  owner-run command into an owner-only folder outside the repo (VA3-D3). This is the
  only exception to the base plan's "nothing written to disk", and it never applies
  to everyday dictation.
- **Hard-case suites:**
  - questions and instructions to an AI ("write me a function that…"), where the
    output must stay a question or instruction, never an answer;
  - self-corrections;
  - protected terms;
  - numbers, dates and times;
  - code-like dictation.
- **Metrics:**
  - word error rate against the target;
  - guard rejection rate;
  - "answered instead of cleaned" count, which must be 0;
  - latency p50/p95 on this GPU.
- **Baselines, in order:**
  - B0: raw transcript.
  - B1: rules.
  - B2: prompted off-the-shelf Qwen3.5-0.8B / 2B and Gemma 4 E2B (thinking mode off).
  - B3: Haiku, as a quality ceiling only.

  Every trained model is reported as a gain over B0 and B1. Typing the input
  unchanged always passes the guard and scores fairly well, so "better than raw" is
  the bar that matters.

## Training

Only if B2 misses the bar.

- **SFT:** LoRA on the synthetic and round-trip pairs. A 0.8–2B QLoRA run fits in
  8 GB (Unsloth or TRL). Local, so no cost and no data leaves the machine.
- **GRPO:**
  - Reward = guard gate (rejected output gets the minimum) + similarity to the clean
    target (chrF) + a small length penalty.
  - Prompts are weighted toward the hard-case suites.
  - Watch for reward hacking: the identity output, and dropping hard words to stay
    inside the guard. That is what the deletion cap and the B0 comparison catch.
- **Re-evaluate after 4-bit quantization.** Ship only what passes as quantized.
- Runs are capped with `systemd-run --user --scope -p MemoryMax=…` like every other
  job here, and never touch the live daemon.

## Packages

| Pkg | What | Notes |
|---|---|---|
| **VA-3a** | Rules pass, the guard (added to `SELF_PROTECTED`), the eval harness (B0, B1) and the owner-run gold-set recorder. No model. | Guard has negative tests: added word, substitution, newline, control character, over-deletion. Mutation check: a guard that skips the subset test must fail them. Recorder: owner-run only, owner-only folder (0700/0600), outside the repo. |
| **VA-3b** | llama.cpp runtime in the daemon, prompted Qwen3.5 (B2), behind a Settings → Voice switch, default off. Terminals rules-only. | Needs VA-2's foreground category. Latency into VA-0's Diagnostics. |
| **VA-3c** | Data generator: synthetic pairs, Kokoro→Parakeet round trip, provenance per example. | Offline tool, not in the daemon. **Built 2026-11-16** as `voiceclean/` (`python -m voiceclean.data`); first-run numbers under *Data*. |
| **VA-3d** | SFT run and report against B0–B3. | Only if B2 misses the bar. |
| **VA-3e** | GRPO run and report. | Only if SFT leaves a measurable gap on hard cases. |
| **VA-3f** | Ship: weights fetched like Parakeet's (pinned URL, size and hash checked), switch default decided by the owner from the report. | |

Order: VA-3a first; it is useful alone and gates everything after it. VA-3b after
VA-2. VA-3c can start any time. VA-3d/e only on evidence.

Later, not in scope: the same weights can run in a browser extension through WebGPU
for in-browser dictation. A web page cannot type into other tabs or apps; an
extension can, within the browser only.

## Owner decisions (2026-11-16)

| # | Question | Answer |
|---|---|---|
| VA3-D1 | Terminals: model or rules? | **Rules only.** The model never cleans terminal dictation. |
| VA3-D2 | Guard module in `SELF_PROTECTED`? | **Yes**, like the other voice-link files. |
| VA3-D3 | Record a gold set? | **Yes, owner-run only.** About 100 clips (audio and text), recorded only by a command the owner runs, kept in an owner-only folder outside the repo, used only for evaluation. Everyday dictation still writes nothing. |
| VA3-D4 | Owner's own messages as training text? | **Yes, locally.** Owner-written messages only, never assistant turns; they and anything derived from them stay on this machine. |
| VA3-D5 | Base model | **Qwen3.5-0.8B.** |

## Open questions (defaults in use until answered)

1. Keep the optional Haiku pass as a setting once a local model ships? (default yes,
   off)
2. **Recognizer errors versus the guard.** When Parakeet mishears ("when's day" for
   "Wednesday"), the correct target holds a word the input lacks, so the guard as
   written rejects the fix. Should VA-3a's guard admit a narrow class of
   substitutions, such as near-homophones from a fixed table? (default no: the
   cleaner fixes punctuation, fillers and self-corrections only. VA-3c tags every
   round-trip pair `guard_compatible` so training can include or exclude them.)
