# Reviewer bench: which model should review Jarvis PRs? (report, 2026-10-23)

**Question.** Jarvis's workflow has four roles: orchestrator, planner, implementer and reviewer. The orchestrator and planner are fixed at Claude Opus 5.5. This bench measures which model is the cheapest good-enough **reviewer**: how many real defects it finds in a pull request, how many of its findings are wrong, and what a review costs.

**Answer, in one line.** Codex gpt-6.1-sol gives the best precision for its cost: the highest MED+ recall on the full key (58%) at 88% precision, paid only from the Codex weekly quota, which is nearly unused. Haiku 5.5 finds the most (47% recall on the original key) but about a third of its findings are wrong. Opus never reported a wrong finding, but it found no more than the others, at about 9x Haiku's cost.

## 1. Results

### 1.1 Per model (159 reviews: 20 cases × 8 models, one rep)

| harness | model | n | recall (original key) | MED+ recall (original key) | recall (full key) | MED+ recall (full key) | true positives | false positives | nits | precision | cost per review | wall per review |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Claude Code | Opus 5.5 | 20 | 32% | 39% | 39% | 51% | 28 | 0 | 2 | **100%** | $0.757 | 267 s |
| Claude Code | Sonnet 5.5 | 20 | 33% | 39% | 37% | 46% | 27 | 11 | 2 | 71% | $0.278 | 126 s |
| Claude Code | Haiku 5.5 | 20 | **47%** | 41% | **51%** | 48% | **41** | 23 | 6 | 64% | $0.088 | 350 s |
| Codex CLI | gpt-6.1-sol | 20 | 29% | 39% | 39% | **58%** | 21 | 3 | 0 | 88% | Codex quota | 190 s |
| Codex CLI | gpt-6-luna | 20 | 20% | 30% | 17% | 23% | 5 | 4 | 0 | 56% | Codex quota | 161 s |
| Jarvis loop | Haiku 5.5 | 20 | **47%** | **50%** | 40% | 43% | 26 | 16 | 8 | 62% | **$0.065** | 385 s |
| Jarvis loop | GLM 5.3 Flash | 20 | 20% | 27% | 16% | 21% | 8 | 14 | 8 | 36% | $0.079 | 460 s |
| Jarvis loop | DeepSeek V4.1 Flash | 19 | 34% | 42% | 31% | 42% | 21 | 25 | 19 | 46% | $0.251 | 922 s |

DeepSeek has 19 runs: its seed-unreadable review failed twice, the second time at the OpenRouter spend cap.

### 1.2 What the numbers say

1. **Codex gpt-6.1-sol is the default reviewer.**
   - It found the most serious bugs (58% MED+ on the full key) with only 3 wrong findings in 20 reviews.
   - It and Jarvis Haiku are the only models that caught all four seeded bugs.
   - Its cost is Codex quota, and 40 Codex reviews moved that weekly meter by about 1%.
2. **Haiku 5.5 is the best second pass when recall matters more than noise.**
   - It has the highest recall in both harnesses, at $0.065 (Jarvis, OpenRouter BYOK) to $0.088 (Claude Code, list) per review.
   - About a third of its findings are false positives, so a lead or a second model must filter it.
3. **Opus is precise, not thorough.**
   - 28 true positives, 0 false positives and 2 nits: it reports few things, and they are right.
   - It is not worth about 9x Haiku's cost as a first-pass reviewer. It may still be worth it as a final check on boundary changes, where a wrong "looks fine" costs more than a review.
4. **Sonnet 5.5 adds nothing.** Its recall equals Opus's, its precision is lower, and it costs 3x Haiku.
5. **Drop gpt-6-luna, GLM 5.3 Flash and DeepSeek V4.1 Flash as reviewers.**
   - Luna and GLM have the lowest recall.
   - DeepSeek's recall is middling, but its precision is 46%, it produced the most nits (19), it is the slowest (15 min per review), and it is the most expensive of the cheap models ($0.25).
6. **Controls and seeds behaved.**
   - The two easy seeds (seed-origin, seed-unreadable) were caught by every model that ran them.
   - seed-privfsync (a missing `flush()` before `fsync`) separated the models. Only Codex sol, Jarvis Haiku and Jarvis DeepSeek found it. All three Claude Code models missed it.

### 1.3 How much to trust it

- **One rep, small keys.** Each case has 1–15 scored defects. One catch can move a case's recall by 20–100 points, so treat model differences under about 10 points as noise. The ranking above relies on differences larger than that, or on precision, where the counts are larger.
- **Recall is low everywhere.** The best model finds about half of the known defects. These cases are hard: real follow-up review rounds, safety-boundary code and subtle test-invisible seeds.
- **Self-grading bias.** The rubric extras and the pooled rulings were written by Opus. The **original key** (human review findings and seeds) is the unbiased measure, and it is reported separately. Opus does not lead on it.
- **Precision counts only settled findings,** and after adjudication every finding was settled: 0 unverified.

## 2. Methodology

### 2.1 Cases (20 review cases)

Each case is a real Jarvis pull request at a fixed `base..head`, with a brief that states only what the PR claims. The cases come in three kinds:

- **13 real-PR cases** from Jarvis PRs #211–#280. Their ground truth is the findings their own review rounds recorded: reviewer hand-backs and the lead's review notes. Every entry was fixed in the PR or a named follow-up.
- **3 controls:** cl3-clean, sec2-clean and sec4-clean. Their reviews found little or nothing, which tests for invented findings.
- **4 seeded cases:** the real diff plus one injected bug. All four are test-invisible: the PR's own suites pass on the seeded tree, so only reading the code finds them.
  - seed-origin: `check_origin` uses startswith.
  - seed-unreadable: PermissionError is swallowed.
  - seed-privfsync: `flush()` is missing before `fsync`.
  - seed-tornread: a cut-multibyte slice is off by one.
  - Three earlier seeds (seed-hostport, seed-privwrite and seed-readlines) were replaced because the PR's own tests caught them.

### 2.2 Models and harnesses (the O-1 matrix)

| harness | models | how it runs |
|---|---|---|
| Claude Code CLI | Haiku, Sonnet and Opus 5.5 | `claude -p`, read-only review prompt, effort high |
| Codex CLI | gpt-6.1-sol, gpt-6-luna | `codex exec`, `approval_policy=never`, effort high |
| Jarvis loop | Haiku 5.5 (via OpenRouter, Anthropic BYOK), GLM 5.3 Flash, DeepSeek V4.1 Flash | Jarvis's own tool loop, 32k output, effort high |

Kimi K3 was dropped from the matrix by the owner. Every model got the same brief, the same snapshot and the same focus list, and was asked for findings in a fixed JSON form: severity, file, line, title and scenario.

### 2.3 Isolation

Each review ran in a fresh **bwrap cell** containing a sanitized snapshot of the case head.

- The cell had a tmpfs home, an environment allowlist, and trimmed in-memory credentials. Credential files were never copied in.
- Networking went through **pasta**: DNS and TCP 443 only, with private and host ranges blocked, so the live daemon's ports were unreachable.
- A post-run **escape audit** scanned every command for paths outside the snapshot.
  - It flagged 57 runs, all from lexical matches: grep or sed patterns, heredoc code, `ls -ld /var/tmp`, in-snapshot relative paths, and Claude Code's own tool-result files.
  - The lead read the flagged commands and cleared the flags with a recorded note. None was a real attempt to leave the cell.
- A quota guard refused Claude and Codex cells at 75% of any window, and whenever the quota was unknown.

### 2.4 Grading: the two-stage rubric grader

An Opus judge with repo access costs about $0.22 per review and re-checks the same findings on every run. Instead, each case's code is verified once, and every run is graded against that record:

1. **Rubric, built once per case.**
   - Opus agents with code access, blind to every benched review, built each rubric.
   - It confirms or disputes each original finding (all 48 were confirmed).
   - It adds verified extra defects: 20, each reproduced or traced.
   - It lists 158 tempting non-issues, each with a citation. One example is the restart behaviour that dc-r1's design plan states is intended.
2. **Stage 0: location gate (free, deterministic).**
   - A finding can match a key entry only when it is on the same file, within ±15 lines.
   - In the pilot this gate kept all 12 of Opus's matches and dropped none.
3. **Stage 1: text-only matcher (Sonnet).**
   - One call per review, with no code access.
   - It labels each finding as match (a key entry among its gate candidates), non-issue, nit or unmatched.
   - Reviewer text is fenced with random nonces.
4. **Stage 2: one-time rulings.**
   - Unmatched findings are clustered across all runs of a case.
   - Opus rules once per cluster, with the code: valid, invalid, or duplicate of a key entry.
   - The lead adjudicates each ruling. Accepted rulings become the case's **pooled key**, so a later run that finds the same thing is graded for free.

**Scores.**
- **Recall** is weighted by severity (HIGH 3, MED 2, LOW 1), as a mean over cases with a scorable key. It is reported on the **original key** and on the **full key** (original + rubric extras + pooled).
- **MED+ recall** counts only MED and HIGH entries.
- **False positives** are matches to a non-issue. **Precision** is true positives over true plus false positives.

### 2.5 Grader validation

| check | result |
|---|---|
| Sonnet re-judge of the 22 pilot runs vs the Opus judge | recall identical on 22/22; per-finding labels agree 43/53 (81%). Sonnet is lenient: it accepted claims a design plan marks as intended. |
| Location-only matcher vs Opus | 12/12 Opus hits kept, 0 misses, 9 spurious. Location is necessary but not sufficient. |
| Rubric grader vs the Opus judge, 16 pilot reviews | recall on the original key identical on 16/16; after rulings, 31/44 findings get the same label (70%), and 38/44 (86%) agree on real bug vs not |
| The 6 real disagreements | 1 duplicate of a known bug that Opus missed (the rubric grader is right); 2 combining-mark claims a design rule makes intended; 1 test gap; 2 nit vs LOW |
| Matcher cost | $0.022–0.027 per review, against $0.22 for the Opus judge |

### 2.6 Adjudication in P3

- 99 clusters across the 20 cases got one Opus ruling each, from 2 clustering passes plus member rulings.
- The lead accepted:
  - **26 defects**;
  - **60 non-issues** (wrong findings);
  - **13 duplicates** of known entries: right findings that the location gate had not matched.
- Two rulings were spot-checked against the code and held:
  - the HUD relay face check returns 2 when the HUD isn't built;
  - `math.isfinite(10**400)` raises OverflowError in the probe validator.
- Two cases (monitor-r1, sv5-r1) needed a second clustering attempt. The grader's copy check had refused the first restatements.

## 3. Cost of the experiment

| item | cost |
|---|---|
| Claude Code reviews (60) | ~$22.4 list, on plan quota |
| Codex reviews (40) | ~1% of the Codex weekly quota |
| Jarvis reviews (59) | $9.87 OpenRouter (ledger $11.91 of a $12 cap, including the pilot) |
| Sonnet matcher, 159 reviews | $3.52 list |
| Opus clustering and rulings, 99 clusters (one-time) | $25.05 list |
| Claude weekly quota over the day | 13% → 20%, including other use |

The legacy Opus judge would have cost about $35 for the same 159 reviews, and would cost that again on every rep. The rulings are paid once, and a second rep would cost about $3.50 to grade.

**Incident.** While the grader was being built, its test suite made roughly 130–170 real Opus calls through a listing path that reached the real login. That is about $4–13 of quota, estimated; it wasn't measured. The test guard is now enforced in the runner itself and in `sandbox.launch`, and a launch spy confirms 0 real launches.

## 4. Side findings: bugs found in Jarvis

Building the rubric and the rulings confirmed several defects that are still on main. Each was filed as its own fix task:

- **MED** — `chat_git._scrub` lets `user:password@` through when earlier output has a backtick or the password has a `$` (from #245). Reproduced on main.
- **MED** — a v2 gate multi-field command join: an unclosed quote in one field hides a DENY command in another (from #211).
- **MED** — Codex sub-agent approval cards never use the child's provenance stamp (from #275).
- **MED** — Browser-tab error lines are cut at 300 characters, which truncates the keyring unlock hints (from #279).
- **MED** (Windows only) — PowerShell commands are judged with POSIX rules and auto-allowed (from #211).
- **LOW** — the argv-array join without quoting (from #211), and two speech-notice races (from #228).

## 5. What's next

- **P4: the implementer bench.** 8 implementation cases are ready and no runs have been made. The reviewer recommendation above is meant to be the reviewer in that bench.
- **P5: update the claude-workflows skill.** Its routing table and §9 should get the reviewer choice: Codex sol by default, plus a Haiku pass when recall matters, plus Opus for boundary PRs.
- **Optional:** a second rep, which would cost about $3.50 to grade now that the keys exist, to narrow the noise on the closer model pairs.

## 6. Data

- **Raw runs:** `~/.cache/jarvis-lead/rolebench/runs/` — run.json, findings.json, judge.json and the transcript for each review. Pool files are in `~/.cache/jarvis-lead/rolebench/pool/`.
- **Committed keys:**
  - rubrics: `rolebench/cases/rubric/`;
  - pooled keys: `rolebench/cases/pooled/`;
  - cases: `rolebench/cases/*.json`.
- **Short results note:** `docs/plans/2026-10-23-role-bench-p3-results.md`.
- **Abbreviations in the tables below:** CC = Claude Code, J = Jarvis loop, sol/luna = the Codex models. "n/a" means the run is missing; "–" means the case has no scorable entries for that key.

### 6.1 Key size per case

| case | kind | original | rubric extras | pooled defects | pooled duplicates | non-issues (rubric + pooled) |
|---|---|---|---|---|---|---|
| bpreview-docs | real PR | 5 | 1 | 2 | 1 | 9 |
| c6-fu-r1 | real PR | 2 | 2 | 2 | 0 | 8 |
| c6-r1 | real PR | 5 | 0 | 1 | 1 | 17 |
| c6-r2 | real PR | 4 | 5 | 0 | 0 | 13 |
| c6-r3 | real PR | 4 | 2 | 1 | 0 | 10 |
| cl3-clean | control | 0 | 0 | 0 | 0 | 9 |
| dc-r1 | real PR | 5 | 2 | 8 | 4 | 19 |
| hint-r1 | real PR | 4 | 1 | 0 | 1 | 9 |
| monitor-r1 | real PR | 3 | 2 | 4 | 1 | 9 |
| sec2-clean | control | 1 | 0 | 2 | 1 | 10 |
| sec3-r1 | real PR | 1 | 2 | 0 | 1 | 12 |
| sec4-clean | control | 0 | 0 | 3 | 0 | 13 |
| seed-origin | seeded | 1 | 0 | 0 | 1 | 7 |
| seed-privfsync | seeded | 1 | 0 | 1 | 0 | 14 |
| seed-tornread | seeded | 1 | 0 | 0 | 1 | 12 |
| seed-unreadable | seeded | 1 | 0 | 0 | 0 | 13 |
| store-r1 | real PR | 1 | 1 | 2 | 0 | 8 |
| sv5-r1 | real PR | 3 | 2 | 4 | 0 | 11 |
| sv6-r1 | real PR | 3 | 0 | 1 | 1 | 9 |
| vex-r1 | real PR | 3 | 0 | 3 | 0 | 10 |

### 6.2 Full-key weighted recall, case × model

| case | CC Opus | CC Sonnet | CC Haiku | Codex sol | Codex luna | J Haiku | J GLM | J DeepSeek |
|---|---|---|---|---|---|---|---|---|
| bpreview-docs | 25% | 25% | 44% | 19% | 19% | 44% | 0% | 25% |
| c6-fu-r1 | 50% | 50% | 50% | 25% | 0% | 50% | 0% | 25% |
| c6-r1 | 14% | 0% | 0% | 0% | 0% | 0% | 0% | 0% |
| c6-r2 | 31% | 8% | 8% | 31% | 0% | 8% | 8% | 0% |
| c6-r3 | 25% | 12% | 12% | 38% | 0% | 0% | 0% | 12% |
| cl3-clean | – | – | – | – | – | – | – | – |
| dc-r1 | 20% | 24% | 24% | 20% | 0% | 4% | 4% | 12% |
| hint-r1 | 57% | 71% | 71% | 43% | 0% | 43% | 0% | 57% |
| monitor-r1 | 24% | 0% | 14% | 14% | 0% | 5% | 0% | 5% |
| sec2-clean | 0% | 50% | 100% | 0% | 0% | 50% | 0% | 0% |
| sec3-r1 | 60% | 60% | 100% | 80% | 0% | 20% | 0% | 0% |
| sec4-clean | 33% | 0% | 67% | 33% | 0% | 33% | 33% | 33% |
| seed-origin | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| seed-privfsync | 0% | 0% | 25% | 75% | 0% | 75% | 0% | 100% |
| seed-tornread | 100% | 100% | 100% | 100% | 100% | 100% | 0% | 100% |
| seed-unreadable | 100% | 100% | 100% | 100% | 100% | 100% | 100% | n/a |
| store-r1 | 50% | 50% | 50% | 17% | 0% | 67% | 50% | 50% |
| sv5-r1 | 25% | 25% | 50% | 25% | 12% | 12% | 0% | 12% |
| sv6-r1 | 12% | 12% | 12% | 12% | 0% | 38% | 12% | 12% |
| vex-r1 | 20% | 20% | 40% | 0% | 0% | 20% | 0% | 20% |

### 6.3 False positives, case × model

| case | CC Opus | CC Sonnet | CC Haiku | Codex sol | Codex luna | J Haiku | J GLM | J DeepSeek |
|---|---|---|---|---|---|---|---|---|
| bpreview-docs | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 |
| c6-fu-r1 | 0 | 1 | 1 | 0 | 0 | 1 | 0 | 1 |
| c6-r1 | 0 | 3 | 2 | 0 | 0 | 1 | 0 | 3 |
| c6-r2 | 0 | 1 | 1 | 0 | 0 | 0 | 1 | 2 |
| c6-r3 | 0 | 0 | 1 | 0 | 0 | 0 | 1 | 1 |
| cl3-clean | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 |
| dc-r1 | 0 | 1 | 3 | 0 | 1 | 1 | 0 | 1 |
| hint-r1 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 1 |
| monitor-r1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| sec2-clean | 0 | 0 | 1 | 0 | 0 | 3 | 0 | 1 |
| sec3-r1 | 0 | 0 | 1 | 0 | 1 | 0 | 1 | 2 |
| sec4-clean | 0 | 0 | 2 | 0 | 0 | 1 | 3 | 3 |
| seed-origin | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| seed-privfsync | 0 | 1 | 3 | 0 | 1 | 2 | 4 | 3 |
| seed-tornread | 0 | 0 | 1 | 1 | 0 | 2 | 0 | 2 |
| seed-unreadable | 0 | 1 | 3 | 1 | 0 | 1 | 1 | n/a |
| store-r1 | 0 | 1 | 1 | 0 | 0 | 1 | 1 | 2 |
| sv5-r1 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 1 |
| sv6-r1 | 0 | 1 | 2 | 0 | 0 | 1 | 0 | 0 |
| vex-r1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 1 |

### 6.4 Every run

| run id | case | model | recall orig | recall full | MED+ full | TP | FP | nits | cost | wall s |
|---|---|---|---|---|---|---|---|---|---|---|
| 20261023-130246-bpreview-docs-claude-opus-1-ea9d | bpreview-docs | CC Opus | 23% | 25% | 25% | 2 | 0 | 0 | $0.624 | 108 |
| 20261023-122038-bpreview-docs-claude-sonnet-1-b1b3 | bpreview-docs | CC Sonnet | 31% | 25% | 25% | 2 | 0 | 0 | $0.192 | 36 |
| 20261023-102935-bpreview-docs-claude-haiku-1-6db3 | bpreview-docs | CC Haiku | 46% | 44% | 50% | 3 | 0 | 0 | $0.038 | 161 |
| 20261022-193917-bpreview-docs-codex-gpt-6-1-sol-1-c5a5 | bpreview-docs | Codex sol | 23% | 19% | 25% | 1 | 0 | 0 | – | 93 |
| 20261022-213624-bpreview-docs-codex-gpt-6-luna-1-e58d | bpreview-docs | Codex luna | 23% | 19% | 25% | 1 | 0 | 0 | – | 67 |
| 20261022-193916-bpreview-docs-jarvis-anthropic-claude-haiku-5-5-1-7973 | bpreview-docs | J Haiku | 46% | 44% | 50% | 4 | 1 | 0 | $0.079 | 456 |
| 20261022-214704-bpreview-docs-jarvis-z-ai-glm-5-3-flash-1-959a | bpreview-docs | J GLM | 0% | 0% | 0% | 0 | 1 | 0 | $0.043 | 383 |
| 20261023-001201-bpreview-docs-jarvis-deepseek-deepseek-v4-1-flash-1-6f8f | bpreview-docs | J DeepSeek | 23% | 25% | 25% | 2 | 0 | 0 | $0.290 | 377 |
| 20261023-130434-c6-fu-r1-claude-opus-1-4587 | c6-fu-r1 | CC Opus | – | 50% | – | 3 | 0 | 0 | $0.669 | 144 |
| 20261023-122114-c6-fu-r1-claude-sonnet-1-cfad | c6-fu-r1 | CC Sonnet | – | 50% | – | 3 | 1 | 0 | $0.286 | 77 |
| 20261023-103216-c6-fu-r1-claude-haiku-1-2c38 | c6-fu-r1 | CC Haiku | – | 50% | – | 3 | 1 | 0 | $0.047 | 225 |
| 20261022-194050-c6-fu-r1-codex-gpt-6-1-sol-1-c11a | c6-fu-r1 | Codex sol | – | 25% | – | 1 | 0 | 0 | – | 265 |
| 20261022-204339-c6-fu-r1-codex-gpt-6-luna-1-b5bc | c6-fu-r1 | Codex luna | – | 0% | – | 0 | 0 | 0 | – | 422 |
| 20261022-194652-c6-fu-r1-jarvis-anthropic-claude-haiku-5-5-1-1388 | c6-fu-r1 | J Haiku | – | 50% | – | 2 | 1 | 0 | $0.086 | 460 |
| 20261022-215328-c6-fu-r1-jarvis-z-ai-glm-5-3-flash-1-154c | c6-fu-r1 | J GLM | – | 0% | – | 1 | 0 | 0 | $0.025 | 1215 |
| 20261023-103928-c6-fu-r1-jarvis-deepseek-deepseek-v4-1-flash-1-3bcb | c6-fu-r1 | J DeepSeek | – | 25% | – | 1 | 1 | 2 | $0.297 | 1361 |
| 20261023-130632-c6-r1-claude-opus-1-82ea | c6-r1 | CC Opus | 0% | 14% | 0% | 1 | 0 | 0 | $1.004 | 362 |
| 20261023-122232-c6-r1-claude-sonnet-1-5b27 | c6-r1 | CC Sonnet | 0% | 0% | 0% | 0 | 3 | 0 | $0.449 | 205 |
| 20261023-103601-c6-r1-claude-haiku-1-bc5d | c6-r1 | CC Haiku | 0% | 0% | 0% | 0 | 2 | 0 | $0.257 | 520 |
| 20261022-194515-c6-r1-codex-gpt-6-1-sol-1-d43a | c6-r1 | Codex sol | 0% | 0% | 0% | 0 | 0 | 0 | – | 199 |
| 20261022-205041-c6-r1-codex-gpt-6-luna-1-37e5 | c6-r1 | Codex luna | 0% | 0% | 0% | 0 | 0 | 0 | – | 88 |
| 20261022-195432-c6-r1-jarvis-anthropic-claude-haiku-5-5-1-e989 | c6-r1 | J Haiku | 0% | 0% | 0% | 0 | 1 | 0 | $0.133 | 670 |
| 20261022-221343-c6-r1-jarvis-z-ai-glm-5-3-flash-1-2fcf | c6-r1 | J GLM | 0% | 0% | 0% | 0 | 0 | 0 | $0.064 | 323 |
| 20261023-002702-c6-r1-jarvis-deepseek-deepseek-v4-1-flash-1-9882 | c6-r1 | J DeepSeek | 0% | 0% | 0% | 0 | 3 | 1 | $0.278 | 700 |
| 20261023-130048-c6-r2-claude-opus-1-b441 | c6-r2 | CC Opus | 0% | 31% | 50% | 2 | 0 | 0 | $1.182 | 344 |
| 20261023-122558-c6-r2-claude-sonnet-1-8f3b | c6-r2 | CC Sonnet | 17% | 8% | 0% | 1 | 1 | 0 | $0.354 | 153 |
| 20261023-104442-c6-r2-claude-haiku-1-7008 | c6-r2 | CC Haiku | 0% | 8% | 0% | 1 | 1 | 0 | $0.043 | 221 |
| 20261022-194835-c6-r2-codex-gpt-6-1-sol-1-6f0a | c6-r2 | Codex sol | 17% | 31% | 50% | 2 | 0 | 0 | – | 278 |
| 20261022-205210-c6-r2-codex-gpt-6-luna-1-2b9b | c6-r2 | Codex luna | 0% | 0% | 0% | 0 | 0 | 0 | – | 107 |
| 20261022-200543-c6-r2-jarvis-anthropic-claude-haiku-5-5-1-50af | c6-r2 | J Haiku | 17% | 8% | 0% | 1 | 0 | 1 | $0.057 | 264 |
| 20261022-221906-c6-r2-jarvis-z-ai-glm-5-3-flash-1-8abf | c6-r2 | J GLM | 0% | 8% | 0% | 1 | 1 | 0 | $0.107 | 521 |
| 20261023-122549-c6-r2-jarvis-deepseek-deepseek-v4-1-flash-1-23fc | c6-r2 | J DeepSeek | 0% | 0% | 0% | 0 | 2 | 0 | $0.344 | 782 |
| 20261023-125043-c6-r3-claude-opus-1-7b8f | c6-r3 | CC Opus | 33% | 25% | 0% | 2 | 0 | 0 | $0.884 | 605 |
| 20261023-122831-c6-r3-claude-sonnet-1-1a24 | c6-r3 | CC Sonnet | 0% | 12% | 0% | 2 | 0 | 0 | $0.366 | 360 |
| 20261023-104823-c6-r3-claude-haiku-1-176f | c6-r3 | CC Haiku | 0% | 12% | 0% | 2 | 1 | 0 | $0.293 | 519 |
| 20261022-195314-c6-r3-codex-gpt-6-1-sol-1-510d | c6-r3 | Codex sol | 0% | 38% | 100% | 1 | 0 | 0 | – | 162 |
| 20261022-205357-c6-r3-codex-gpt-6-luna-1-8b66 | c6-r3 | Codex luna | 0% | 0% | 0% | 0 | 0 | 0 | – | 55 |
| 20261022-201008-c6-r3-jarvis-anthropic-claude-haiku-5-5-1-2d34 | c6-r3 | J Haiku | 0% | 0% | 0% | 1 | 0 | 0 | $0.056 | 349 |
| 20261022-222748-c6-r3-jarvis-z-ai-glm-5-3-flash-1-88c6 | c6-r3 | J GLM | 0% | 0% | 0% | 0 | 1 | 0 | $0.089 | 562 |
| 20261023-004554-c6-r3-jarvis-deepseek-deepseek-v4-1-flash-1-0f22 | c6-r3 | J DeepSeek | 33% | 12% | 0% | 1 | 1 | 1 | $0.154 | 1382 |
| 20261023-124734-cl3-clean-claude-opus-1-9582 | cl3-clean | CC Opus | – | – | – | 0 | 0 | 0 | $0.403 | 188 |
| 20261023-123431-cl3-clean-claude-sonnet-1-d643 | cl3-clean | CC Sonnet | – | – | – | 0 | 0 | 0 | $0.177 | 192 |
| 20261023-105702-cl3-clean-claude-haiku-1-2b23 | cl3-clean | CC Haiku | – | – | – | 0 | 0 | 1 | $0.039 | 531 |
| 20261022-195556-cl3-clean-codex-gpt-6-1-sol-1-46c8 | cl3-clean | Codex sol | – | – | – | 0 | 0 | 0 | – | 124 |
| 20261022-205453-cl3-clean-codex-gpt-6-luna-1-3dec | cl3-clean | Codex luna | – | – | – | 0 | 0 | 0 | – | 69 |
| 20261022-201557-cl3-clean-jarvis-anthropic-claude-haiku-5-5-1-5036 | cl3-clean | J Haiku | – | – | – | 0 | 0 | 0 | $0.061 | 412 |
| 20261022-223710-cl3-clean-jarvis-z-ai-glm-5-3-flash-1-1e72 | cl3-clean | J GLM | – | – | – | 0 | 1 | 1 | $0.030 | 379 |
| 20261023-010856-cl3-clean-jarvis-deepseek-deepseek-v4-1-flash-1-1bf4 | cl3-clean | J DeepSeek | – | – | – | 0 | 1 | 0 | $0.154 | 608 |
| 20261023-124301-dc-r1-claude-opus-1-e45c | dc-r1 | CC Opus | 11% | 20% | 20% | 3 | 0 | 0 | $1.447 | 273 |
| 20261023-123743-dc-r1-claude-sonnet-1-b465 | dc-r1 | CC Sonnet | 11% | 24% | 20% | 4 | 1 | 0 | $0.531 | 149 |
| 20261023-110554-dc-r1-claude-haiku-1-49a8 | dc-r1 | CC Haiku | 22% | 24% | 20% | 4 | 3 | 0 | $0.259 | 552 |
| 20261022-195800-dc-r1-codex-gpt-6-1-sol-1-7607 | dc-r1 | Codex sol | 11% | 20% | 20% | 3 | 0 | 0 | – | 186 |
| 20261022-205602-dc-r1-codex-gpt-6-luna-1-18cf | dc-r1 | Codex luna | 0% | 0% | 0% | 0 | 1 | 0 | – | 86 |
| 20261022-202249-dc-r1-jarvis-anthropic-claude-haiku-5-5-1-ff55 | dc-r1 | J Haiku | 0% | 4% | 0% | 1 | 1 | 0 | $0.037 | 167 |
| 20261022-224329-dc-r1-jarvis-z-ai-glm-5-3-flash-1-268d | dc-r1 | J GLM | 0% | 4% | 0% | 1 | 0 | 0 | $0.269 | 227 |
| 20261023-011905-dc-r1-jarvis-deepseek-deepseek-v4-1-flash-1-516e | dc-r1 | J DeepSeek | 0% | 12% | 20% | 1 | 1 | 0 | $0.205 | 770 |
| 20261023-124018-hint-r1-claude-opus-1-9cdb | hint-r1 | CC Opus | 25% | 57% | 100% | 2 | 0 | 0 | $0.797 | 163 |
| 20261023-124012-hint-r1-claude-sonnet-1-11ab | hint-r1 | CC Sonnet | 50% | 71% | 100% | 3 | 0 | 0 | $0.274 | 97 |
| 20261023-111507-hint-r1-claude-haiku-1-ba02 | hint-r1 | CC Haiku | 50% | 71% | 100% | 3 | 0 | 1 | $0.042 | 179 |
| 20261022-200106-hint-r1-codex-gpt-6-1-sol-1-0f6e | hint-r1 | Codex sol | 0% | 43% | 100% | 1 | 0 | 0 | – | 172 |
| 20261022-205728-hint-r1-codex-gpt-6-luna-1-3626 | hint-r1 | Codex luna | 0% | 0% | 0% | 0 | 0 | 0 | – | 226 |
| 20261022-202536-hint-r1-jarvis-anthropic-claude-haiku-5-5-1-d63f | hint-r1 | J Haiku | 75% | 43% | 0% | 3 | 1 | 0 | $0.086 | 602 |
| 20261022-224716-hint-r1-jarvis-z-ai-glm-5-3-flash-1-e767 | hint-r1 | J GLM | 0% | 0% | 0% | 0 | 0 | 1 | $0.120 | 168 |
| 20261023-013155-hint-r1-jarvis-deepseek-deepseek-v4-1-flash-1-c588 | hint-r1 | J DeepSeek | 25% | 57% | 100% | 2 | 1 | 1 | $0.203 | 1581 |
| 20261023-123704-monitor-r1-claude-opus-1-234e | monitor-r1 | CC Opus | 0% | 24% | 20% | 3 | 0 | 0 | $0.901 | 193 |
| 20261023-124149-monitor-r1-claude-sonnet-1-7fac | monitor-r1 | CC Sonnet | 0% | 0% | 0% | 0 | 0 | 1 | $0.291 | 132 |
| 20261023-111807-monitor-r1-claude-haiku-1-aee0 | monitor-r1 | CC Haiku | 11% | 14% | 0% | 3 | 1 | 1 | $0.054 | 262 |
| 20261022-200358-monitor-r1-codex-gpt-6-1-sol-1-387b | monitor-r1 | Codex sol | 0% | 14% | 20% | 1 | 0 | 0 | – | 392 |
| 20261022-210115-monitor-r1-codex-gpt-6-luna-1-b8d8 | monitor-r1 | Codex luna | 0% | 0% | 0% | 0 | 0 | 0 | – | 247 |
| 20261022-203538-monitor-r1-jarvis-anthropic-claude-haiku-5-5-1-a4b2 | monitor-r1 | J Haiku | 11% | 5% | 0% | 1 | 0 | 2 | $0.077 | 450 |
| 20261022-225003-monitor-r1-jarvis-z-ai-glm-5-3-flash-1-0ab4 | monitor-r1 | J GLM | 0% | 0% | 0% | 0 | 0 | 0 | $0.044 | 449 |
| 20261023-015816-monitor-r1-jarvis-deepseek-deepseek-v4-1-flash-1-21b5 | monitor-r1 | J DeepSeek | 11% | 5% | 0% | 1 | 0 | 3 | $0.248 | 320 |
| 20261023-123410-sec2-clean-claude-opus-1-3a1d | sec2-clean | CC Opus | – | 0% | – | 0 | 0 | 1 | $0.476 | 174 |
| 20261023-124401-sec2-clean-claude-sonnet-1-c4ae | sec2-clean | CC Sonnet | – | 50% | – | 2 | 0 | 0 | $0.258 | 118 |
| 20261023-112230-sec2-clean-claude-haiku-1-a9bd | sec2-clean | CC Haiku | – | 100% | – | 2 | 1 | 0 | $0.041 | 428 |
| 20261022-201030-sec2-clean-codex-gpt-6-1-sol-1-8517 | sec2-clean | Codex sol | – | 0% | – | 0 | 0 | 0 | – | 183 |
| 20261022-210522-sec2-clean-codex-gpt-6-luna-1-9fcb | sec2-clean | Codex luna | – | 0% | – | 0 | 0 | 0 | – | 46 |
| 20261022-204309-sec2-clean-jarvis-anthropic-claude-haiku-5-5-1-3cf0 | sec2-clean | J Haiku | – | 50% | – | 1 | 3 | 0 | $0.056 | 328 |
| 20261022-225732-sec2-clean-jarvis-z-ai-glm-5-3-flash-1-3036 | sec2-clean | J GLM | – | 0% | – | 0 | 0 | 0 | $0.083 | 198 |
| 20261023-020336-sec2-clean-jarvis-deepseek-deepseek-v4-1-flash-1-e885 | sec2-clean | J DeepSeek | – | 0% | – | 1 | 1 | 2 | $0.398 | 623 |
| 20261023-123137-sec3-r1-claude-opus-1-59f1 | sec3-r1 | CC Opus | 0% | 60% | 100% | 1 | 0 | 1 | $0.772 | 153 |
| 20261023-124559-sec3-r1-claude-sonnet-1-485f | sec3-r1 | CC Sonnet | 0% | 60% | 100% | 1 | 0 | 0 | $0.230 | 78 |
| 20261023-112938-sec3-r1-claude-haiku-1-f736 | sec3-r1 | CC Haiku | 100% | 100% | 100% | 4 | 1 | 1 | $0.164 | 285 |
| 20261022-201334-sec3-r1-codex-gpt-6-1-sol-1-7d51 | sec3-r1 | Codex sol | 0% | 80% | 100% | 2 | 0 | 0 | – | 193 |
| 20261022-210609-sec3-r1-codex-gpt-6-luna-1-fb67 | sec3-r1 | Codex luna | 0% | 0% | 0% | 0 | 1 | 0 | – | 70 |
| 20261022-204837-sec3-r1-jarvis-anthropic-claude-haiku-5-5-1-b6d7 | sec3-r1 | J Haiku | 100% | 20% | 0% | 1 | 0 | 2 | $0.059 | 244 |
| 20261022-230051-sec3-r1-jarvis-z-ai-glm-5-3-flash-1-65a5 | sec3-r1 | J GLM | 0% | 0% | 0% | 0 | 1 | 2 | $0.048 | 252 |
| 20261023-021359-sec3-r1-jarvis-deepseek-deepseek-v4-1-flash-1-da0b | sec3-r1 | J DeepSeek | 0% | 0% | 0% | 0 | 2 | 2 | $0.275 | 770 |
| 20261023-122950-sec4-clean-claude-opus-1-c31f | sec4-clean | CC Opus | – | 33% | – | 1 | 0 | 0 | $0.547 | 106 |
| 20261023-124718-sec4-clean-claude-sonnet-1-0ba4 | sec4-clean | CC Sonnet | – | 0% | – | 0 | 0 | 0 | $0.157 | 41 |
| 20261023-113423-sec4-clean-claude-haiku-1-e7f5 | sec4-clean | CC Haiku | – | 67% | – | 2 | 2 | 2 | $0.040 | 244 |
| 20261022-201648-sec4-clean-codex-gpt-6-1-sol-1-d635 | sec4-clean | Codex sol | – | 33% | – | 1 | 0 | 0 | – | 185 |
| 20261022-210719-sec4-clean-codex-gpt-6-luna-1-c365 | sec4-clean | Codex luna | – | 0% | – | 0 | 0 | 0 | – | 176 |
| 20261022-205241-sec4-clean-jarvis-anthropic-claude-haiku-5-5-1-c483 | sec4-clean | J Haiku | – | 33% | – | 1 | 1 | 0 | $0.053 | 230 |
| 20261022-230502-sec4-clean-jarvis-z-ai-glm-5-3-flash-1-ad7b | sec4-clean | J GLM | – | 33% | – | 1 | 3 | 1 | $0.102 | 284 |
| 20261023-022649-sec4-clean-jarvis-deepseek-deepseek-v4-1-flash-1-f705 | sec4-clean | J DeepSeek | – | 33% | – | 1 | 3 | 1 | $0.159 | 642 |
| 20261023-122822-seed-origin-claude-opus-1-c98a | seed-origin | CC Opus | 100% | 100% | 100% | 1 | 0 | 0 | $0.445 | 88 |
| 20261023-124759-seed-origin-claude-sonnet-1-4f61 | seed-origin | CC Sonnet | 100% | 100% | 100% | 2 | 0 | 0 | $0.217 | 69 |
| 20261023-113827-seed-origin-claude-haiku-1-abd6 | seed-origin | CC Haiku | 100% | 100% | 100% | 2 | 0 | 0 | $0.016 | 99 |
| 20261022-201953-seed-origin-codex-gpt-6-1-sol-1-266b | seed-origin | Codex sol | 100% | 100% | 100% | 1 | 0 | 0 | – | 138 |
| 20261022-211016-seed-origin-codex-gpt-6-luna-1-ffa9 | seed-origin | Codex luna | 100% | 100% | 100% | 1 | 0 | 0 | – | 90 |
| 20261023-051903-seed-origin-jarvis-anthropic-claude-haiku-5-5-1-a2cc | seed-origin | J Haiku | 100% | 100% | 100% | 2 | 0 | 0 | $0.040 | 208 |
| 20261022-230947-seed-origin-jarvis-z-ai-glm-5-3-flash-1-0f0e | seed-origin | J GLM | 100% | 100% | 100% | 1 | 0 | 0 | $0.031 | 365 |
| 20261023-112846-seed-origin-jarvis-deepseek-deepseek-v4-1-flash-1-c4d4 | seed-origin | J DeepSeek | 100% | 100% | 100% | 2 | 0 | 1 | $0.227 | 1035 |
| 20261023-122613-seed-privfsync-claude-opus-1-8aa6 | seed-privfsync | CC Opus | 0% | 0% | 0% | 0 | 0 | 0 | $0.610 | 128 |
| 20261023-124908-seed-privfsync-claude-sonnet-1-bb31 | seed-privfsync | CC Sonnet | 0% | 0% | 0% | 0 | 1 | 0 | $0.155 | 40 |
| 20261023-114007-seed-privfsync-claude-haiku-1-00b8 | seed-privfsync | CC Haiku | 0% | 25% | 0% | 1 | 3 | 0 | $0.029 | 151 |
| 20261022-202212-seed-privfsync-codex-gpt-6-1-sol-1-83e6 | seed-privfsync | Codex sol | 100% | 75% | 100% | 1 | 0 | 0 | – | 156 |
| 20261022-211146-seed-privfsync-codex-gpt-6-luna-1-b6ed | seed-privfsync | Codex luna | 0% | 0% | 0% | 0 | 1 | 0 | – | 83 |
| 20261022-205933-seed-privfsync-jarvis-anthropic-claude-haiku-5-5-1-9552 | seed-privfsync | J Haiku | 100% | 75% | 100% | 1 | 2 | 0 | $0.054 | 270 |
| 20261022-231552-seed-privfsync-jarvis-z-ai-glm-5-3-flash-1-9917 | seed-privfsync | J GLM | 0% | 0% | 0% | 0 | 4 | 0 | $0.107 | 825 |
| 20261023-025652-seed-privfsync-jarvis-deepseek-deepseek-v4-1-flash-1-e985 | seed-privfsync | J DeepSeek | 100% | 100% | 100% | 2 | 3 | 0 | $0.186 | 976 |
| 20261023-122417-seed-tornread-claude-opus-1-02f4 | seed-tornread | CC Opus | 100% | 100% | 100% | 1 | 0 | 0 | $0.456 | 116 |
| 20261023-124949-seed-tornread-claude-sonnet-1-a2fb | seed-tornread | CC Sonnet | 100% | 100% | 100% | 1 | 0 | 0 | $0.158 | 47 |
| 20261023-114238-seed-tornread-claude-haiku-1-ca7c | seed-tornread | CC Haiku | 100% | 100% | 100% | 2 | 1 | 0 | $0.038 | 643 |
| 20261022-202448-seed-tornread-codex-gpt-6-1-sol-1-03de | seed-tornread | Codex sol | 100% | 100% | 100% | 1 | 1 | 0 | – | 124 |
| 20261022-211310-seed-tornread-codex-gpt-6-luna-1-c8b9 | seed-tornread | Codex luna | 100% | 100% | 100% | 1 | 0 | 0 | – | 77 |
| 20261022-210403-seed-tornread-jarvis-anthropic-claude-haiku-5-5-1-22c9 | seed-tornread | J Haiku | 100% | 100% | 100% | 1 | 2 | 1 | $0.063 | 400 |
| 20261022-232937-seed-tornread-jarvis-z-ai-glm-5-3-flash-1-8769 | seed-tornread | J GLM | 0% | 0% | 0% | 0 | 0 | 0 | $0.008 | 478 |
| 20261023-031308-seed-tornread-jarvis-deepseek-deepseek-v4-1-flash-1-d204 | seed-tornread | J DeepSeek | 100% | 100% | 100% | 2 | 2 | 0 | $0.267 | 1591 |
| 20261023-122210-seed-unreadable-claude-opus-1-3fb9 | seed-unreadable | CC Opus | 100% | 100% | 100% | 1 | 0 | 0 | $0.534 | 127 |
| 20261023-125036-seed-unreadable-claude-sonnet-1-0d67 | seed-unreadable | CC Sonnet | 100% | 100% | 100% | 1 | 1 | 0 | $0.201 | 81 |
| 20261023-114740-seed-unreadable-claude-haiku-1-f462 | seed-unreadable | CC Haiku | 100% | 100% | 100% | 1 | 3 | 0 | $0.035 | 192 |
| 20261022-202653-seed-unreadable-codex-gpt-6-1-sol-1-6cb7 | seed-unreadable | Codex sol | 100% | 100% | 100% | 1 | 1 | 0 | – | 174 |
| 20261022-211427-seed-unreadable-codex-gpt-6-luna-1-3325 | seed-unreadable | Codex luna | 100% | 100% | 100% | 1 | 0 | 0 | – | 78 |
| 20261022-211044-seed-unreadable-jarvis-anthropic-claude-haiku-5-5-1-af60 | seed-unreadable | J Haiku | 100% | 100% | 100% | 1 | 1 | 0 | $0.066 | 390 |
| 20261022-233735-seed-unreadable-jarvis-z-ai-glm-5-3-flash-1-ece6 | seed-unreadable | J GLM | 100% | 100% | 100% | 1 | 1 | 1 | $0.049 | 783 |
| 20261023-122005-store-r1-claude-opus-1-52f6 | store-r1 | CC Opus | 100% | 50% | 100% | 1 | 0 | 0 | $0.493 | 125 |
| 20261023-125157-store-r1-claude-sonnet-1-9803 | store-r1 | CC Sonnet | 100% | 50% | 100% | 1 | 1 | 0 | $0.221 | 322 |
| 20261023-115052-store-r1-claude-haiku-1-430c | store-r1 | CC Haiku | 100% | 50% | 100% | 1 | 1 | 0 | $0.029 | 162 |
| 20261022-202947-store-r1-codex-gpt-6-1-sol-1-7dcf | store-r1 | Codex sol | 0% | 17% | 0% | 1 | 0 | 0 | – | 150 |
| 20261022-211545-store-r1-codex-gpt-6-luna-1-161b | store-r1 | Codex luna | 0% | 0% | 0% | 0 | 0 | 0 | – | 60 |
| 20261022-211714-store-r1-jarvis-anthropic-claude-haiku-5-5-1-00ad | store-r1 | J Haiku | 100% | 67% | 100% | 2 | 1 | 0 | $0.049 | 359 |
| 20261022-235038-store-r1-jarvis-z-ai-glm-5-3-flash-1-e27c | store-r1 | J GLM | 100% | 50% | 100% | 1 | 1 | 1 | $0.076 | 376 |
| 20261023-040941-store-r1-jarvis-deepseek-deepseek-v4-1-flash-1-523f | store-r1 | J DeepSeek | 100% | 50% | 100% | 1 | 2 | 0 | $0.307 | 1463 |
| 20261023-121509-sv5-r1-claude-opus-1-ac68 | sv5-r1 | CC Opus | 0% | 25% | – | 2 | 0 | 0 | $1.086 | 295 |
| 20261023-125720-sv5-r1-claude-sonnet-1-7a14 | sv5-r1 | CC Sonnet | 0% | 25% | – | 2 | 0 | 0 | $0.464 | 104 |
| 20261023-115334-sv5-r1-claude-haiku-1-3870 | sv5-r1 | CC Haiku | 50% | 50% | – | 4 | 0 | 0 | $0.138 | 322 |
| 20261022-203218-sv5-r1-codex-gpt-6-1-sol-1-fafb | sv5-r1 | Codex sol | 0% | 25% | – | 2 | 1 | 0 | – | 208 |
| 20261022-211646-sv5-r1-codex-gpt-6-luna-1-5fa1 | sv5-r1 | Codex luna | 0% | 12% | – | 1 | 1 | 0 | – | 68 |
| 20261022-212313-sv5-r1-jarvis-anthropic-claude-haiku-5-5-1-1d91 | sv5-r1 | J Haiku | 0% | 12% | – | 1 | 0 | 0 | $0.046 | 152 |
| 20261023-102936-sv5-r1-jarvis-z-ai-glm-5-3-flash-1-6499 | sv5-r1 | J GLM | 0% | 0% | – | 0 | 0 | 0 | $0.190 | 593 |
| 20261023-043404-sv5-r1-jarvis-deepseek-deepseek-v4-1-flash-1-f90b | sv5-r1 | J DeepSeek | 0% | 12% | – | 1 | 1 | 2 | $0.183 | 1343 |
| 20261023-115632-sv6-r1-claude-opus-1-a33f | sv6-r1 | CC Opus | 20% | 12% | 0% | 1 | 0 | 0 | $1.026 | 1116 |
| 20261023-125904-sv6-r1-claude-sonnet-1-57f6 | sv6-r1 | CC Sonnet | 20% | 12% | 0% | 1 | 1 | 1 | $0.310 | 117 |
| 20261023-115856-sv6-r1-claude-haiku-1-378c | sv6-r1 | CC Haiku | 20% | 12% | 0% | 1 | 2 | 0 | $0.086 | 636 |
| 20261022-203547-sv6-r1-codex-gpt-6-1-sol-1-f623 | sv6-r1 | Codex sol | 20% | 12% | 0% | 1 | 0 | 0 | – | 262 |
| 20261022-211754-sv6-r1-codex-gpt-6-luna-1-4d90 | sv6-r1 | Codex luna | 0% | 0% | 0% | 0 | 0 | 0 | – | 1022 |
| 20261022-212545-sv6-r1-jarvis-anthropic-claude-haiku-5-5-1-e600 | sv6-r1 | J Haiku | 0% | 38% | 50% | 1 | 1 | 1 | $0.086 | 862 |
| 20261022-235829-sv6-r1-jarvis-z-ai-glm-5-3-flash-1-87a8 | sv6-r1 | J GLM | 20% | 12% | 0% | 1 | 0 | 1 | $0.093 | 620 |
| 20261023-045627-sv6-r1-jarvis-deepseek-deepseek-v4-1-flash-1-842c | sv6-r1 | J DeepSeek | 20% | 12% | 0% | 2 | 0 | 2 | $0.222 | 606 |
| 20261023-114740-vex-r1-claude-opus-1-e0b5 | vex-r1 | CC Opus | 0% | 20% | – | 1 | 0 | 0 | $0.793 | 532 |
| 20261023-130101-vex-r1-claude-sonnet-1-80ac | vex-r1 | CC Sonnet | 0% | 20% | – | 1 | 1 | 0 | $0.263 | 104 |
| 20261023-120933-vex-r1-claude-haiku-1-7107 | vex-r1 | CC Haiku | 50% | 40% | – | 2 | 0 | 0 | $0.068 | 664 |
| 20261022-204010-vex-r1-codex-gpt-6-1-sol-1-b397 | vex-r1 | Codex sol | 0% | 0% | – | 0 | 0 | 0 | – | 155 |
| 20261022-213457-vex-r1-codex-gpt-6-luna-1-2e71 | vex-r1 | Codex luna | 0% | 0% | – | 0 | 0 | 0 | – | 87 |
| 20261022-214007-vex-r1-jarvis-anthropic-claude-haiku-5-5-1-9920 | vex-r1 | J Haiku | 0% | 20% | – | 1 | 0 | 1 | $0.061 | 417 |
| 20261023-000849-vex-r1-jarvis-z-ai-glm-5-3-flash-1-e1f5 | vex-r1 | J GLM | 0% | 0% | – | 0 | 0 | 0 | $0.003 | 191 |
| 20261023-121604-vex-r1-jarvis-deepseek-deepseek-v4-1-flash-1-cb9c | vex-r1 | J DeepSeek | 0% | 20% | – | 1 | 1 | 1 | $0.372 | 584 |

### 6.5 Seeded bugs caught

| seed | CC Opus | CC Sonnet | CC Haiku | Codex sol | Codex luna | J Haiku | J GLM | J DeepSeek |
|---|---|---|---|---|---|---|---|---|
| seed-origin | yes | yes | yes | yes | yes | yes | yes | yes |
| seed-privfsync | no | no | no | yes | no | yes | no | yes |
| seed-tornread | yes | yes | yes | yes | yes | yes | no | yes |
| seed-unreadable | yes | yes | yes | yes | yes | yes | yes | n/a |
