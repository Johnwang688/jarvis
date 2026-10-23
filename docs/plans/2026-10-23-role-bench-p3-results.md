# Role bench P3: reviewer results (2026-10-23)

The full reviewer bench (plan §6, P3) is one rep of the O-1 matrix over the 20 review cases: 159 reviews. DeepSeek's seed-unreadable run is missing: it failed twice, the second time at the $12 OpenRouter cap. Every run was graded by the two-stage rubric grader (#291). The 99 clusters of unmatched findings were each ruled once by Opus and then adjudicated by the lead: 26 real defects, 60 non-issues and 13 duplicates of a known entry. Nothing is left unverified.

| harness | model | n | recall (original) | MED+ (original) | recall (full) | MED+ (full) | true pos | false pos | nits | precision | $/review | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude | opus | 20 | 32% | 39% | 39% | 51% | 28 | 0 | 2 | 100% | $0.757 | 267 |
| claude | sonnet | 20 | 33% | 39% | 37% | 46% | 27 | 11 | 2 | 71% | $0.278 | 126 |
| claude | haiku | 20 | 47% | 41% | 51% | 48% | 41 | 23 | 6 | 64% | $0.088 | 350 |
| codex | gpt-6.1-sol | 20 | 29% | 39% | 39% | 58% | 21 | 3 | 0 | 88% | quota | 190 |
| codex | gpt-6-luna | 20 | 20% | 30% | 17% | 23% | 5 | 4 | 0 | 56% | quota | 161 |
| jarvis | claude-haiku-5.5 | 20 | 47% | 50% | 40% | 43% | 26 | 16 | 8 | 62% | $0.065 | 385 |
| jarvis | glm-5.3-flash | 20 | 20% | 27% | 16% | 21% | 8 | 14 | 8 | 36% | $0.079 | 460 |
| jarvis | deepseek-v4.1-flash | 19 | 34% | 42% | 31% | 42% | 21 | 25 | 19 | 46% | $0.251 | 922 |

## Reading the table

- **The original key** holds the findings each PR's own review recorded, plus the seeds. **The full key** adds the rubric's verified extras and the pooled defects. Recall is the mean over cases with a scorable key. False positives are matches to a rubric or pooled non-issue. Precision is true positives over true plus false positives, with nits excluded.
- **Cost.** Claude costs are list-price equivalents charged to plan quota. Jarvis costs are OpenRouter dollars, with Haiku billed through BYOK. Codex costs only its own weekly quota: 40 reviews took it from 1% to 0–1%.
- **Noise.** This is one rep over 20 cases with 1–5 key entries each, so a single catch moves a case's recall a lot. Treat differences under about 10 points as noise.

## Findings

- **Codex gpt-6.1-sol** is the best value on precision and serious bugs. It has the highest full-key MED+ recall (58%), 88% precision, and costs only Codex quota, which is barely touched.
- **Haiku 5.5 has the highest recall in both harnesses** (47% original-key), but about a third of its findings are false positives. Claude Code Haiku costs $0.088 per review and Jarvis Haiku $0.065.
- **Opus is the most precise:** 28 true positives and 0 false positives. Its recall is no higher than the others' at about 9x Haiku's cost.
- **Sonnet adds nothing over Haiku and sol** at about 3x Haiku's cost.
- **Drop gpt-6-luna, GLM 5.3 Flash and DeepSeek V4.1 Flash for review.** Luna and GLM have low recall. DeepSeek has middling recall, 46% precision, 19 nits, and is slow (about 15 min) and costly here ($0.25).

## Grader cost (P3)

| step | cost (list) |
|---|---|
| Sonnet matcher, 159 reviews | $3.52 ($0.022 per review) |
| Opus clustering and rulings, 99 clusters across 20 cases (one-time) | $25.05 |

The legacy judge would have cost about $35 for the same 159 reviews, and it would cost that again on every rep. The rulings are paid once.

## Follow-ups

- Escape-audit flags were lexical on all 57 flagged runs, and the lead cleared them with a note.
- The rulings confirmed two v2 gate defects from #211: a multi-field command join (MED) and an argv join without quoting (LOW). They are filed as their own fix task.
- The copy check refused Opus's restatements twice for monitor-r1 and sv5-r1, and a retry passed both. Loosening the check on identifier-heavy findings is a follow-up.
