# Role bench: the cheapest good-enough reviewer and implementer (plan, 2026-10-20)

**Status: PLAN; owner accepted the defaults 2026-10-20 ([decisions](2026-10-20-role-bench-decisions.md)).** Section 8 lists them, defaults in
bold. Results feed the `claude-workflows` skill (section 9 of the skill, "Measured so far").

## 1. The question

The orchestrator and planner are fixed at Claude Opus 5.5 (owner, 2026-10-20). For the two
remaining roles, **which model is the cheapest one that is good enough?**

- **Reviewer:** catches the real problems in a PR, especially MED and above, without
  drowning the lead in false alarms.
- **Implementer:** turns a brief into a change that passes the hidden tests and survives
  review in few rounds.

The metric is *expected cost per merged PR* (the skill's objective), not raw pass rate. A
cheap reviewer that misses one MED in five can still win if a second cheap pass catches it.
This bench measures enough to compute that.

This is not agent-bench, long-bench or SWE-bench (`swecompare/`). Those answer generic
questions on other code, and their scores are **never pooled** with these. Everything here
is John's own repo and his own review history.

## 2. Candidates

| Harness | Models | Billing |
|---|---|---|
| Claude Code CLI (`claude -p`) | Haiku 5.5, Sonnet 5.5, Opus 5.5 (control) | Max plan quota; `--output-format json` gives a list-price `total_cost_usd` |
| Codex CLI (`codex exec`) | gpt-6.1-sol, gpt-6-luna | ChatGPT plan quota; token counts from the JSON events |
| Jarvis v1 loop via OpenRouter | Haiku 5.5 (BYOK, harness control), Kimi K3, gpt-6-luna, one or two owner picks | Dollars (OpenRouter credits, or BYOK credits) |

**Harness control:** Haiku 5.5 runs in both Claude Code and the Jarvis loop. The gap between
the two is the harness effect, and any cross-harness comparison is read through it.

**Kimi K3 provider:** OpenRouter lists $0.24–$4.50 / M input across providers. Moonshot
direct (the BYOK key) is $3 / $15, and cheap providers serve fp4 weights. Pin one provider
per run so the result names what was measured (decision O-3).

## 3. Reviewer bench (first)

### 3.1 Cases (12–15)

Each case is a PR head the lead actually reviewed, with ground truth. Ground truth means
the findings that were adjudicated real and fixed, each recorded with severity, mechanism,
file and the evidence that would show it.

| Case | Head | Ground truth (from the review record) |
|---|---|---|
| C6 round 1 | `5e9f21a` | MED: provenance follows the running turn, not the spawning turn; LOW: a timing mutant survives; LOW: duplicate titles indistinguishable; LOW: deceptive characters pass |
| C6 round 2 | `9e1dfb0` | MED: a re-tasked child shows no line; LOW: between-turn stamp race; LOW: eviction restamps; LOW: bridge cards follow the running turn |
| C6 round 3 | `4019f48` | LOW: wrong "started" for a never-seen parent; LOW: grandchild test gap; LOW: native ordering unverified |
| Direct capture round 1 | `59188dfc` | MED: the size check rejects frames at 150% scale; MED: dev-server prompt selector misspelled, so the grid can miss it; LOW ×3 |
| SV-6 buffering round 1 | `5c4cfa6` | MED: the user-message rule is unpinned (mutant survives); LOW: order test tests nothing; LOW: SDK-upgrade drop is silent |
| B-PREVIEW docs | `902eb02` | MED ×4: overclaims beyond the probes, unarchived numbers |
| Unlock hint | `1eb0f85` | LOW: the stale cap comment; LOW: the recipe is pinned only in fragments |
| Earlier PRs | mined (P0) | SEC-1..6, CL-*, FS*, C0–C5, #270, #272: review rounds with fixed findings |
| Clean controls (2–3) | mined | PRs whose review found nothing real; these measure false alarms |
| Seeded bugs (2–3) | a clean head + one injected bug | Unambiguous ground truth, for example a dropped `owner_only`, an off-by-one in a cap, a race made reachable |

### 3.2 Snapshot hygiene

A reviewer must not be able to see the fix.

- Build each case as a fresh repo: `git archive` the base, commit it, then apply the PR diff
  as one commit, so `git diff HEAD~1` is the PR.
- No remote, no future refs, no `gh` login, no GitHub token in the environment.
- Network is off where the harness allows it.
- Detect leakage the way `swecompare.audit_network` does, and report it next to the score.

### 3.3 Protocol

- One fixed review brief for every model. It is the template the lead uses: PR summary,
  focus areas from CLAUDE.md, run the relevant suites under memory caps, **try at least one
  mutant of your own**, and report findings ranked HIGH/MED/LOW/NIT with file:line and a
  failure scenario.
- The same tools for every model: read, grep and run tests in the snapshot. No pushing, no
  network.
- Runs: 3 per (model, case) for Haiku, Sonnet and the OpenRouter models; 2 for Opus.
  Cases are shuffled.

### 3.4 Scoring

- **Matching.** An Opus judge, blind to which model wrote the review, matches each reported
  finding to ground truth by mechanism and location. The lead spot-checks 20% of the
  matches.
- **Adjudicating the rest.** An unmatched finding is either valid-new (counted as a true
  positive and added to ground truth), invalid (a false positive), or a trivial NIT
  (ignored).
- **Per run:**
  - weighted recall (HIGH 5, MED 3, LOW 1);
  - MED+ recall ("caught the blocker");
  - false positives;
  - whether the model ran tests and tried a mutant;
  - cost (dollars or list-price equivalent), wall time and tool calls.
- **Decision rule.** The cheapest model whose MED+ recall is at least that of Opus minus
  10 points becomes the default first reviewer. Also report the best two-model combination
  (for example two cheap passes, or cheap then Opus on boundary PRs) by expected cost per
  caught MED+.

## 4. Implementer bench (second)

### 4.1 Tasks (6–8, sizes S/M)

Each task is a merged PR at its **base** commit, given the original brief. The PR's final
tests are kept back as hidden tests.

| Task | Size | Notes |
|---|---|---|
| #279 unlock hint | S | text plus HUD caps; hidden: password_store and BrowserProfile tests |
| #277 C6 follow-up | S | stamping rules; hidden: relay_provenance tests |
| #280 buffering bug | M | the symptom plus a *sanitized* log fixture (never the owner's real logs); hidden: idle_reader tests |
| 3–5 earlier PRs | S/M | mined in P0, preferring ones with crisp hidden tests (SEC fixes, CL cleanups) |

Large boundary builds (#275, #276) are left out: one run costs as much as the rest of the
bench.

### 4.2 Protocol and scoring

1. **Build.** The implementer builds in the snapshot and its diff is captured.
2. **Hidden tests.** The hidden tests run, alongside the touched suites as a regression
   check.
3. **Review.** A fixed reviewer, the reviewer-bench winner held constant, reviews the
   change. The implementer gets **up to 2 fix rounds** with those findings.
4. **Record:**
   - hidden-test pass after each round;
   - rounds needed to reach merge-ready (hidden tests pass and no MED+ left);
   - total cost to merge-ready, including the reviewer's share;
   - wall time;
   - diff size;
   - file-scope discipline (no edits outside the brief).
5. **Decision rule.** For each task class (mechanical, feature-in-architecture, debug from
   symptoms), pick the cheapest model with the best cost to merge-ready. A model that fails
   merge-ready on a class is not routed there.

Optional, if budget allows: Opus spec plus Haiku chunks against Sonnet alone, on one
wide-and-shallow task (skill section 3).

## 5. Harness (built once)

- **Where it lives:** a new `rolebench/` package next to `swecompare/`, reusing its harness
  runners (claude, jarvis) and adding a `codex exec` runner.
- **Case files:** JSON, one per case, holding head, base, brief, ground truth and hidden
  tests.
- **Entry points:** `python -m rolebench snapshot|review|implement|judge|report`.
- **Safety and isolation:**
  - Every run goes under `systemd-run --user --scope -p MemoryMax=3G`.
  - Snapshots live in `~/.cache/jarvis-lead/rolebench/`.
  - Never touch the live daemon or ports 8402/8403/8405.
  - Claude and Codex runs use the real CLI logins (as the L-gates do) but only ever see the
    snapshot. No credential is copied, and the review brief forbids pushing.
- **Load:** runs are serialized against lead sweeps (skill section 9, rule 6), with at most
  3 concurrent model runs.
- **Quota guard:** skip a Claude or Codex cell while that provider is at 75% or more of a
  quota window (the existing Diff-review threshold).
- **Output:** one JSONL row per run in the skill's section 6 log schema, and a
  `report.md` with per-model tables.

## 6. Phases

| Phase | What | Who | Spend |
|---|---|---|---|
| P0 | Mine transcripts and PR history for cases, ground truth and tasks; write the case files | Sonnet agent, Opus spot-check | quota only |
| P1 | Build `rolebench/` | Sonnet builder, Opus review | quota only |
| P2 | Reviewer pilot: 3 cases × every model × 1 run; check judge agreement with the lead | lead | small |
| P3 | Full reviewer bench | runner | see O-2 |
| P4 | Implementer pilot (2 tasks), then full | runner | see O-2 |
| P5 | Analysis; update the skill's routing table and section 9 | lead | none |

Stop after P2 if the judge disagrees with the lead on more than 1 in 5 matches; fix the
rubric first.

## 7. Risks

- **Small n.** About 15 review cases and 8 tasks give directions, not decimals. Report
  ranges, not point scores.
- **Judge bias.** An Opus judge scoring Opus. Mitigated by blind labels and the lead's
  spot-check.
- **Harness confound.** Mitigated by the Haiku-in-both control. Results are read as
  "model + harness".
- **Ground-truth drift.** Valid new findings join ground truth, so reruns use the updated
  set.
- **Flaky suites** (load, post-reboot environment). Hidden tests run alone with one retry;
  a failure is counted only if it reproduces.
- **Cost overrun.** Hard dollar cap per run and in total. Abort a model on its first run
  over the per-run cap. BYOK prices come from the provider's own rate, not the catalog
  (Kimi direct was about 8× the catalog price, 2026-10-20).

## 8. Owner decisions (defaults in bold)

- **O-1 Model matrix.** **Claude Haiku / Sonnet / Opus; Codex gpt-6.1-sol and gpt-6-luna;
  Jarvis loop with Haiku (control), Kimi K3 and gpt-6-luna.** Add or remove models?
- **O-2 Dollar cap** for the OpenRouter cells. **$10 total, $0.50 per run.** Claude and
  Codex cells use quota.
- **O-3 Kimi K3 provider.** **Moonshot direct (uses the $24 BYOK credit)**, or a cheaper
  US-hosted provider?
- **O-4 Chinese-hosted models.** The repo's code is not personal data under the routing
  policy, so **allowed for this bench only**; the owner's logs and chats never go to them
  (fixtures are sanitized).
- **O-5 Order.** **Reviewer bench first; implementer bench only after the reviewer winner
  is known** (the implementer bench uses it).
- **O-6 Merge rule** for the `rolebench/` build PR. **Merge on review**, the same as the
  other tooling PRs.
