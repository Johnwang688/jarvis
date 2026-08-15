# stebench — does ASD-STE100 help AI text evade detectors?

A pilot. One model (`openai/gpt-5.6-luna`), one detector (GPTZero), three
conditions, twelve tasks, plus a human control set.

The question behind it is **detector construction**, not evasion: if
Simplified Technical English moves AI text across a detector's boundary,
that boundary is a feature the detector should not be leaning on. And if
*human* STE gets flagged, that is a false-positive class worth designing for
independently of whether evasion works.

## Hypothesis (pre-registered before any data was collected)

**STE will make text easier to detect, not harder.** STE caps sentences at
20 words (procedural) / 25 (descriptive), forbids clause stacking, and
mandates one idea per sentence. That collapses sentence-length variance, and
burstiness is a named feature in GPTZero's public methodology.

Secondary: human-authored STE should false-positive.

Recording the direction up front so a surprising result cannot be
reinterpreted as the expected one after the fact.

## Detector choice

**Sapling is the default.** Its API is usage-metered with **no subscription**:
$0.005 per 1,000 characters (`sapling.ai/docs/api/pricing`). The `$5` on their
pricing page is a one-time minimum applied as *account credit*, not a fee.
The full 47-text pilot is **116,061 characters = $0.58**.

**A free trial key needs no credit card** and processes **50,000 chars per
24h**, expiring after a month. So the pilot also runs at **zero cost across
three days** — pass a character budget and the resumable `detect` stage picks
up where it stopped:

```bash
../.venv/bin/python run_pilot.py detect sapling 50000   # repeat daily x3
```

**GPTZero was the original target and is not viable here** — it gates API
access behind a subscription with a $45/mo floor. Its client is still in
`detect.py` and selectable with `--backend gptzero` if access ever appears.

Sapling is also the better instrument for this project: it returns `score`
0–1 **plus per-sentence scores plus per-token probabilities**, where GPTZero
returns a document verdict. Token-level output is what you would actually
build a detector on.

## Setup

```bash
# 1. Register at https://sapling.ai/user/register, then generate a key from
#    the API section of the settings dashboard.
#    See https://sapling.ai/docs/api/api-access/ - no credit card needed.
# 2. Append it to the repo .env (no quotes, no trailing space)
printf 'SAPLING_API_KEY=%s\n' 'your_key_here' >> ../.env

# 3. Confirm it works. Prints a verdict and the response schema, never the key.
../.venv/bin/python detect.py --selftest
```

`OPENROUTER_API_KEY` is already in `.env` and is read from there automatically.

## Running

Staged so a failure never costs a re-spend. `detect` skips anything already
scored, so an interrupted run resumes rather than paying twice, and it prints
an estimated cost before spending anything.

```bash
../.venv/bin/python run_pilot.py dryrun     # no network, proves the pipeline
../.venv/bin/python run_pilot.py generate   # OpenRouter, ~$0.02
../.venv/bin/python run_pilot.py human      # Wikipedia pre-2020, free
../.venv/bin/python run_pilot.py detect     # Sapling, 47 scans, ~$0.58
../.venv/bin/python run_pilot.py analyse
```

Both `detect` and `analyse` take an optional backend argument
(`detect gptzero`), and results are stored per backend in
`data/pilot_results_<backend>.json` so two detectors can be compared on the
identical corpus.

## Design

| | |
|---|---|
| Model | `openai/gpt-5.6-luna`, temperature 0.7 fixed |
| Conditions | `C0_baseline`, `C1_ste_full`, `C3_humanize` |
| Tasks | 12 — 4 procedural, 4 expository-technical, 4 argumentative |
| Length | pinned 260–460 words, out-of-band generations resampled up to 3× |
| Human control | 11 pre-2020 Wikipedia technical revisions, trimmed to the same band |
| Detector | Sapling (default), GPTZero selectable |

### Corpus as generated (verified, $0.0195)

| condition | n | words | sent len | **burstiness** | passive | STE score |
|---|---|---|---|---|---|---|
| C0 baseline | 12 | 386 | 16.7 | **6.82** | 0.09 | 0.802 |
| C1 STE-full | 12 | 387 | 9.7 | **3.45** | 0.01 | **0.905** |
| C3 humanize | 12 | 395 | 14.4 | **6.69** | 0.11 | 0.857 |
| human (wiki <2020) | 11 | 418 | 23.3 | **9.94** | 0.44 | 0.704 |

Length is matched across the three AI conditions, so style is not confounded
with length. C1 compliance of 0.905 means the treatment took — a null result
would be about STE, not about an ignored instruction.

**The predicted mechanism is already visible without a detector: STE halves
burstiness (6.82 → 3.45), moving it *away* from the human value of 9.94.**

Two generation bugs were caught here before any detector spend, both worth
keeping:

- Luna bills reasoning against `max_tokens` and returns **empty content**
  when the budget is exhausted. At 1200 this silently produced two zero-word
  texts. Now 4000.
- STE compresses *systematically*, so a single resample of an out-of-band
  generation is still biased. Now up to 3 retries, and an empty completion is
  never accepted.

### Known limitation of the human control

Wikipedia's passive rate is 0.44 against the AI texts' ~0.09. That is
encyclopedic register, not a human-vs-AI signal, and it will inflate AUROC in
a way that is not about detection. Acceptable for a pilot go/no-go; the full
study needs register-matched human sources, not merely human-authored ones.

**Why `C3_humanize` is not optional.** It is the naive evasion prompt people
already use. Without it, "STE dropped the score 12 points" has nothing to be
compared against and cannot support a conclusion.

**Why the human control is not optional.** A score drop alone cannot
distinguish "moved toward the human distribution" from "moved into an
unoccupied region that happens to score low." Those imply opposite things for
a detector. `analyse` reports AUROC against the human set for exactly this.

**Why generation goes through OpenRouter rather than Claude Code subagents.**
A subagent carries its own system prompt and tool-use framing, and its
sampling params are not under experimental control. Routing every model
through one code path keeps the only difference between cells the thing being
manipulated.

**Why temperature is pinned.** Sampling temperature is itself a known evasion
lever. Letting it vary would confound it with style.

**Why C1 and C3 are length-matched** (1280 vs 1000 chars). If they differed
by a lot, a gap between them could be "long instruction block" rather than
anything about the requested style.

## Measurement

`stylometry.py` computes, for every text including the human ones:

- **Stylometry** — sentence count, mean and SD of sentence length
  (**burstiness**, the feature STE should crush), TTR, mean word length,
  comma density, passive rate, subordinator rate, contraction rate.
- **Partial STE compliance** — a 0–1 composite over seven rules.

### The compliance checker's limit, stated plainly

It scores only rules checkable **without** the ASD-STE100 approved-word
dictionary: sentence length, voice, one-instruction-per-sentence, paragraph
length, `-ing` openers, noun clusters. **Vocabulary compliance — the ~900-word
approved list — is not measured.** Any `ste_score` here is a *ceiling* on true
compliance, never a floor.

Closing that gap means parsing Issue 9 (free download at
`asd-ste100.org/assets/files/ASD-STE100_ISSUE9.pdf`; free to obtain, **not**
free to redistribute — keep the parsed list local and out of any public repo).
That is Phase 0 of the full study, deferred here because the pilot's job is to
find out whether there is an effect worth instrumenting properly.

Validated to discriminate rather than just score everything 1.0:
hand-written STE → `1.00`, a florid non-STE control → `0.52`.

Passive detection is a heuristic (form of *be* + past participle), not a
parse. Fine for comparing conditions; do not quote the absolute rate.

## Analysis

`analyze.py` has no scipy dependency. With n=12 the normal approximation to
Wilcoxon is marginal, so the signed-rank null is **enumerated exactly** over
2^n sign assignments.

### The ceiling effect, and why everything is tested in logit space

Sapling pins **every** Luna text between 0.9998 and 0.99996. On the
probability scale the conditions differ in the 4th decimal, so a paired test
there is measuring floating-point rounding, not style. The same texts span
**8.8 to 10.4 in log-odds**, which is a scale a signed-rank test can work on.

So the paired tests run on `logit(ai_prob)` and the report shows both: the
logit delta is the sensitive measure, the raw probability is what a user of
the detector would actually see. It also reports **how many texts crossed the
0.5 verdict boundary**, because that is the only number that means "evasion" —
a 1-logit drop from 0.99996 to 0.99990 changes no decision anywhere.

Any future backend needs the same treatment. A detector that saturates is
still informative; you just cannot read it on the scale it reports.

### Sapling free-tier quota

The trial key allows 50,000 chars/24h and returns
`429 {"msg": "Rate limited. Capacity used."}` when spent. That is a *daily*
window, so seconds-scale retry backoff cannot help — `detect` recognises the
capacity message, stops the whole run immediately, and reports how many texts
remain. Re-run after the window resets; scored texts are skipped.

Reported: per-condition distributions, paired deltas vs baseline matched by
task, exact Wilcoxon with rank-biserial effect size, AUROC against the human
control, and a condition × domain breakdown.

## Reading the result

- **C1 delta positive** → hypothesis confirmed, STE is counterproductive as
  evasion. Then check whether human STE also false-positives.
- **C1 delta negative and larger than C3's** → STE beats the naive prompt.
  Worth escalating to the full four-model study.
- **C1 negative but AUROC unchanged** → the scores moved without the
  *separability* moving. A threshold artifact, not an evasion finding.
- **Low `ste_score` in C1** → the model did not comply. Fix the prompt before
  concluding anything about STE itself.

## Contrast findings (`contrast.py`, free, all 47 texts)

The single most trustworthy result, because it is not register-confounded —
C0 and C3 are the same model on the same tasks at the same length:

**The humanize prompt fixes nothing.** Of 18 features separating human from
AI at |Cliff's delta| >= 0.33, **zero** closed even 50% of the gap, and most
moved *further* from human than the baseline did.

It fails hardest at exactly what it explicitly asks for. The prompt says
"vary your sentence length a lot, mix very short sentences with long winding
ones," and:

| feature | human | C0 baseline | C3 humanize |
|---|---|---|---|
| sent_len_sd | 9.94 | 6.82 | **6.69** (worse) |
| sent_len_max | 45.82 | 30.17 | **30.08** (unchanged) |
| pct_sent_over_30 | 0.22 | 0.03 | **0.02** (worse) |

Luna says it will vary sentence length and does not. **Prompt-level
instructions do not move sentence geometry** — a humanizer would need to
merge and split sentences in post-processing, not ask.

STE moves *away* from human on all seven top features, consistent with the
pre-registered mechanism.

**Register caveat, and it is load-bearing.** The dominant separating cluster
is entirely sentence-length geometry, and encyclopedic prose is famously
long-sentenced. Wikipedia-vs-instructions probably explains much of a
"perfect separation" that looks like human-vs-AI. Do not build a detector on
these until they are confirmed against a register-matched human corpus.

## Files

| | |
|---|---|
| `prompts.py` | conditions, task set, sampling params |
| `generate.py` | OpenRouter generation + pre-2020 Wikipedia control set |
| `detect.py` | GPTZero client (defensive — stores raw JSON), `--selftest` |
| `stylometry.py` | features + partial STE compliance, dependency-free |
| `analyze.py` | exact Wilcoxon, AUROC, reporting |
| `contrast.py` | human-vs-AI feature contrast, Cliff's delta, humanize-resistance |
| `run_pilot.py` | staged orchestrator |
| `data/` | `corpus.json`, `pilot_results.json` |
