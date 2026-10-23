# Role bench cases

Schema: `docs/plans/2026-10-20-role-bench-decisions.md`; validated by `rolebench/cases.py` (P1). One JSON file per case; no owner chat or log text.

| id | kind | PR | size / ground truth | control / seeded | source |
|---|---|---|---|---|---|
| bpreview-docs | review | #274 | 4 MED, 1 LOW | findings | reviewer hand-back, 2026-10-20 |
| c6-fu-r1 | review | #277 | 2 NIT | control | reviewer hand-back, 2026-10-20 |
| c6-r1 | review | #275 | 1 MED, 3 LOW, 1 NIT | findings | reviewer hand-back, 2026-10-20 |
| c6-r2 | review | #275 | 1 MED, 3 LOW | findings | reviewer hand-back, 2026-10-20 |
| c6-r3 | review | #275 | 3 LOW, 1 NIT | findings | reviewer hand-back, 2026-10-20 |
| cl3-clean | review | #253 | none | control | reviewer hand-back / lead review record, 2026-10-15..19 |
| dc-r1 | review | #276 | 2 MED, 3 LOW | findings | reviewer hand-back, 2026-10-20 |
| hint-r1 | review | #279 | 4 LOW | findings | reviewer hand-back, 2026-10-20 |
| impl-243 | implement | #243 | size S, hidden: tests/v2/approval_label_check.py | - | PR body/spec, tests and diff at merged head |
| impl-244 | implement | #244 | size M, hidden: tests/private_writes_check.py | - | PR body/spec, tests and diff at merged head |
| impl-248 | implement | #248 | size S, hidden: tests/v2/outbound_scrub_check.py | - | PR body/spec, tests and diff at merged head |
| impl-261 | implement | #261 | size M, hidden: tests/v2/redact_private_key_check.py | - | PR body/spec, tests and diff at merged head |
| impl-267 | implement | #267 | size S, hidden: tests/v2/stores_check.py | - | PR body/spec, tests and diff at merged head |
| impl-277 | implement | #277 | size S, hidden: tests/v2/relay_provenance_check.py | - | PR body/spec, tests and diff at merged head |
| impl-279 | implement | #279 | size S, hidden: tests/v2/browser_password_store_check.py, hud/src/components/BrowserProfile.test.ts | - | PR body/spec, tests and diff at merged head |
| impl-280 | implement | #280 | size M, hidden: tests/v2/idle_reader_check.py | - | PR body/spec, tests and diff at merged head |
| monitor-r1 | review | #211 | 1 HIGH, 1 MED, 1 LOW | findings | reviewer hand-back / lead review record, 2026-10-15..19 |
| sec2-clean | review | #243 | none | control | reviewer hand-back / lead review record, 2026-10-15..19 |
| sec3-r1 | review | #245 | 1 LOW | findings | reviewer hand-back / lead review record, 2026-10-15..19 |
| sec4-clean | review | #244 | none | control | reviewer hand-back / lead review record, 2026-10-15..19 |
| seed-origin | review | #253 | 1 MED | seeded (test-invisible) | injected bug on a clean head |
| seed-privfsync | review | #244 | 1 MED | seeded (test-invisible) | injected bug on a clean head |
| seed-tornread | review | #267 | 1 MED | seeded (test-invisible) | injected bug on a clean head |
| seed-unreadable | review | #267 | 1 MED | seeded (test-invisible) | injected bug on a clean head |
| store-r1 | review | #267 | 1 MED | findings | reviewer hand-back / lead review record, 2026-10-15..19 |
| sv5-r1 | review | #228 | 2 LOW, 1 NIT | findings | reviewer hand-back / lead review record, 2026-10-15..19 |
| sv6-r1 | review | #280 | 1 MED, 2 LOW | findings | reviewer hand-back, 2026-10-20 |
| vex-r1 | review | #241 | 2 LOW, 1 NIT | findings | reviewer hand-back / lead review record, 2026-10-15..19 |

## Notes and caveats

- **Review `base`/`head`.** `base` is the merge-base of `head` with the first parent of the PR's merge commit. The three `c6-*` cases share one base; each head includes the whole PR so far.
- **Controls.** `sec2-clean`, `sec4-clean` and `cl3-clean` are PRs whose review found nothing (the lead's own diff reading for the first two and `cl3-clean`; no independent reviewer report). `c6-fu-r1` (#277) found only two NITs, which are recorded but unscored.
- **Ground truth provenance.** Cases for PRs >= #274 and `monitor-r1`, `vex-r1`, `sv5-r1` come from structured reviewer hand-backs. `store-r1` and `sec3-r1` come from the lead's own review narrative in the session transcript (severity is the lead's, assigned in prose). Every entry was fixed in the PR or a named follow-up.
- **Range.** Reviews of #243-#272 were mostly the lead reading the diff and rarely produced structured findings, so `monitor-r1` (#211), `vex-r1` (#241) and `sv5-r1` (#228) were mined from earlier reviewer hand-backs (#193-#242) instead.
- **Seeded cases.** `seed-*` keep the PR's real diff and apply a small patch (checked with `git apply --check` at the head). The ground truth is the injected bug only; the underlying PR was reviewed clean. The `brief` describes the clean PR. All four seeds (`seed-origin`, `seed-privfsync`, `seed-tornread`, `seed-unreadable`) are test-invisible: their named `suites` pass on the seeded tree (checked), so only reading the code finds the bug. Three earlier seeds were removed because the PR's own suite failed on them, so they measured running the suites: `seed-hostport`, and `seed-privwrite` and `seed-readlines` (the rubric build found `tests/private_writes_check.py` failing 10 of 36 and `tests/v2/stores_check.py` torn_read failing on them). Their replacements keep the PR heads (8fa491e, d440184) with a bug the suites do not exercise: `seed-privfsync` drops the `flush()` before `fsync` in `write_private` (the suite only checks that fsync is reached), and `seed-tornread` breaks the cut-character fallback in `_read_lines` for a log with no newline yet (every torn-character case in the suite has an earlier line).
- **Implement tasks.** Hidden tests are the PR's test file(s) at the merged head (for SEC fixes, the acceptance tests committed first on the branch). `impl-277`, `impl-280` and `impl-261` briefs state the private interface their tests call; `impl-280`'s fixture is synthetic (kinds and offsets only).
- **Implement snapshots keep plan docs that existed at base** (for example `docs/plans/2026-10-18-security-fix-specs.md` in `impl-243` and `impl-244`): they are the planner's real output, which the cheaper builder receives in this workflow. The two briefs say the spec is there.
- **Hidden file is an existing suite** for `impl-248`, `impl-267`, `impl-277`, `impl-279` and `impl-280`: the implementer loses that suite from the snapshot, so it has no copy of the regression tests for the code it edits.
- **Review `focus` and `brief` are generic on purpose.** Focus lists name general review areas only; briefs state what the PR claims, never a finding.
- `impl-267`'s brief states the newline-only record rule, which the PR got from review (the first commit lacked it); the hidden test includes that case.

## Rubric (`rubric/<case>.json`)

Built once per review case on 2026-10-21 by Opus agents. They were blind to every benched review: they did not read the bench's run, pilot or judge-comparison directories. The rubric lets a grader that cannot open the code score findings, so the case's code is checked once rather than on every run.

- `original_review`: each `ground_truth` entry, `confirmed` or `disputed`, with a note. All 48 were confirmed. A `disputed` entry also names, in `non_issue`, the rubric non-issue that answers it (a match on it then scores as that non-issue). The grader shows a confirmed entry's note to its text-only matcher as "verified as".
- `entries`: further verified defects in the diff (ids `rb<n>`, the ground-truth schema plus `verified: reproduced|traced` and `fixed_later`). There are 20 across the cases, and none duplicates a ground-truth entry.
- `non_issues`: tempting claims that are not defects (ids `ni<n>`), each with a code or design-document citation. There are 158.
- `notes` (optional): suite results and observations left unclassified.

The case file is never edited. Rubric entries come from one model family, so the report should keep recall on the original key, which is human- and seed-sourced, separate from recall on the rubric.

The rubric is what the two-stage grader (`rolebench/grader.py`, the default `judge` when a rubric exists) scores against: `cases.load_rubric` validates it (head, exact `original_review` coverage, unique `rb`/`ni` ids, severities, parseable locations, `verified`), a `disputed` original entry is excluded from scoring but listed, and `tests/rolebench_grader_check.py` validates every file here. A location must name at least one `path[:line[-line]]` with a known extension, because the grader's location gate matches findings to entries by it.

## Pooled keys (`pooled/<case>.json`)

Written only by `rolebench pool` adjudication (`docs/notes/testing-benchmarks.md`, "Role bench"). `ground_truth` holds accepted clusters (`pool-p<n>`); the optional `non_issues` holds clusters an Opus ruling found invalid and the lead accepted as such (`pool-ni-p<n>`: claim, location, why_not, citation, source); `members` maps both kinds to their [run, finding] pairs; the optional `duplicates` maps a key id (original, rubric or pooled) to the findings of clusters a ruling found to be that entry. One finding is never under two ids.
