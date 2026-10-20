# Role bench cases

Schema: `docs/plans/2026-10-20-role-bench-decisions.md`. One JSON file per case; no owner chat or log text.

| id | kind | PR | size / ground truth | control / seeded | source |
|---|---|---|---|---|---|
| c6-r1 | review | #275 | 1 MED, 3 LOW, 1 NIT | findings | reviewer hand-back, 2026-10-20 |
| c6-r2 | review | #275 | 1 MED, 3 LOW | findings | reviewer hand-back, 2026-10-20 |
| c6-r3 | review | #275 | 3 LOW, 1 NIT | findings | reviewer hand-back, 2026-10-20 |
| c6-fu-r1 | review | #277 | 2 NIT | control | reviewer hand-back, 2026-10-20 |
| dc-r1 | review | #276 | 2 MED, 3 LOW | findings | reviewer hand-back, 2026-10-20 |
| sv6-r1 | review | #280 | 1 MED, 2 LOW | findings | reviewer hand-back, 2026-10-20 |
| bpreview-docs | review | #274 | 4 MED, 1 LOW | findings | reviewer hand-back, 2026-10-20 |
| hint-r1 | review | #279 | 4 LOW | findings | reviewer hand-back, 2026-10-20 |
| sec3-r1 | review | #245 | 1 LOW | findings | lead review record (transcript, 2026-10-18) |
| sec2-clean | review | #243 | none | control | lead review record (transcript, 2026-10-18) |
| sec4-clean | review | #244 | none | control | lead review record (transcript, 2026-10-18) |
| impl-279 | implement | #279 | size S | - | PR body, tests and diff at merged head |
| impl-277 | implement | #277 | size S | - | PR body, tests and diff at merged head |
| impl-280 | implement | #280 | size M | - | PR body, tests and diff at merged head |

## Notes and caveats

- `c6-fu-r1` (#277 at 94dd73c) is marked `control: true`: its review found only two NITs, which are recorded but not scored.
- `sec2-clean` and `sec4-clean` are controls: the lead's review of the diff found nothing (transcript record, 2026-10-18). `sec3-r1` records the one LOW the lead flagged, fixed in #248.
- `impl-277` and `impl-280` hidden tests call private names; the briefs state that interface. `impl-280`'s fixture is synthetic (kinds and offsets only).
- Review `base` is the merge-base of `head` with the first parent of the PR's merge commit. Review cases for #275 share one base; each head includes the whole PR so far.

## Not yet done (next session)

- 4 or more further mined review cases from #243-#272 (candidates: #267 `v2 stores` U+2028 split fix commit d440184 on 1b13630; #263 SEC-6; #265 C3; #266 C4) and one more clean control (candidate #253 CL-3).
- 2-3 seeded cases (clean head plus a small `patch`, checked with `git apply --check`).
- 3-5 further implement tasks from earlier PRs (candidates: #243, #244, #248, #261, #263, #267; their acceptance tests are the first commit of each branch).
- Ground-truth severities for `sec3-r1` come from the lead's summary, not a structured report.
