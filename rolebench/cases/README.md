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
| seed-hostport | review | #253 | 1 MED | seeded | injected bug on a clean head |
| seed-privwrite | review | #244 | 1 MED | seeded | injected bug on a clean head |
| seed-readlines | review | #267 | 1 MED | seeded | injected bug on a clean head |
| store-r1 | review | #267 | 1 MED | findings | reviewer hand-back / lead review record, 2026-10-15..19 |
| sv5-r1 | review | #228 | 2 LOW, 1 NIT | findings | reviewer hand-back / lead review record, 2026-10-15..19 |
| sv6-r1 | review | #280 | 1 MED, 2 LOW | findings | reviewer hand-back, 2026-10-20 |
| vex-r1 | review | #241 | 2 LOW, 1 NIT | findings | reviewer hand-back / lead review record, 2026-10-15..19 |

## Notes and caveats

- **Review `base`/`head`.** `base` is the merge-base of `head` with the first parent of the PR's merge commit. The three `c6-*` cases share one base; each head includes the whole PR so far.
- **Controls.** `sec2-clean`, `sec4-clean` and `cl3-clean` are PRs whose review found nothing (the lead's own diff reading for the first two and `cl3-clean`; no independent reviewer report). `c6-fu-r1` (#277) found only two NITs, which are recorded but unscored.
- **Ground truth provenance.** Cases for PRs >= #274 and `monitor-r1`, `vex-r1`, `sv5-r1` come from structured reviewer hand-backs. `store-r1` and `sec3-r1` come from the lead's own review narrative in the session transcript (severity is the lead's, assigned in prose). Every entry was fixed in the PR or a named follow-up.
- **Range.** Reviews of #243-#272 were mostly the lead reading the diff and rarely produced structured findings, so `monitor-r1` (#211), `vex-r1` (#241) and `sv5-r1` (#228) were mined from earlier reviewer hand-backs (#193-#242) instead.
- **Seeded cases.** `seed-*` keep the PR's real diff and apply a small patch (checked with `git apply --check` at the head). The ground truth is the injected bug only; the underlying PR was reviewed clean. The `brief` describes the clean PR.
- **Implement tasks.** Hidden tests are the PR's test file(s) at the merged head (for SEC fixes, the acceptance tests committed first on the branch). `impl-277`, `impl-280` and `impl-261` briefs state the private interface their tests call; `impl-280`'s fixture is synthetic (kinds and offsets only).
- **Implement snapshots keep plan docs that existed at base** (for example `docs/plans/2026-10-18-security-fix-specs.md` in `impl-243` and `impl-244`): they are the planner's real output, which the cheaper builder receives in this workflow. The two briefs say the spec is there.
- **Hidden file is an existing suite** for `impl-248`, `impl-267`, `impl-277`, `impl-279` and `impl-280`: the implementer loses that suite from the snapshot, so it has no copy of the regression tests for the code it edits.
- **Review `focus` and `brief` are generic on purpose.** Focus lists name general review areas only; briefs state what the PR claims, never a finding.
- `impl-267`'s brief states the newline-only record rule, which the PR got from review (the first commit lacked it); the hidden test includes that case.
