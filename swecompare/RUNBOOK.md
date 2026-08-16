# swecompare runbook

Everything needed to run the comparison from a cold session. Written to be
followed in order; each phase ends in a check you can read off the terminal.

## The question

**How does Jarvis + GPT-5.6 Luna compare to Claude Code + Sonnet 5 (high effort)
at agentic programming / SWE tasks?**

Two cells first. If the gap is large, a third cell (Sonnet 5 *inside the Jarvis
harness*, via OpenRouter) separates model from harness. Not before — it costs
real money and only matters if there is a gap to attribute.

`longbench/` is a **different** benchmark and does not answer this. It measures
what a harness does to a transcript under context pressure and contains no test
suite. Do not mix the two sets of numbers.

## What is already done

- Docker works from WSL (Docker Desktop → Settings → Resources → WSL Integration).
- `swebench` 4.1.0 and `datasets` installed in `.venv`.
- **Grading validated**: the gold patch resolves 1/1 through the official harness.
- **Full pipeline validated end to end** on `pallets__flask-5014` with
  claude/sonnet effort=high → 435-byte patch → **resolved 1/1**.
- Images cached locally: `pallets__flask-5014`, `django__django-11433`,
  plus the shared base/env layers (~11.7 GB so far).
- One graded run exists: `swecompare/runs/20260815-200701-claude-sonnet/`
  (single instance — a plumbing test, not a result).

## Phase 1 — fetch the images (one time, ~25-35 GB)

Both cells reuse these, so this is paid once.

    cd ~/projects/Jarvis
    .venv/bin/python -m swecompare images --size 30 --workers 2

Pulls prebuilt images from Docker Hub where published (fast), builds locally
where not (slow). Expect ~30-60 min on a decent connection.

**Check:** ends with `ready: 30/30`. If any are missing it prints their ids;
re-run the command, it skips what is already present.

**Disk:** ~4 GB per *environment*, 12 environments in this subset, layers shared.
Budget 35 GB. `docker system df` to watch it.

## Phase 2 — cell A: Claude Code + Sonnet 5, high effort

Subscription usage, not dollars.

    .venv/bin/python -m swecompare run \
      --harness claude --model sonnet --effort high --size 30

**Expect 2-4 hours.** It prints one line per instance with patch size, turns and
wall time. Safe to leave; each instance is independent and failures are recorded
rather than fatal.

**Check:** `predictions.jsonl` has 30 lines and few are empty patches. The run
prints the grade command with the right `--run-dir` at the end.

## Phase 3 — cell B: Jarvis + Luna

Real OpenRouter money — roughly $5-20 for 30 instances.

    .venv/bin/python -m swecompare run \
      --harness jarvis --model openai/gpt-5.6-luna --size 30

Needs `OPENROUTER_API_KEY` in `.env` (it is loaded by `jarvis.config`, not the
shell). Run from the repo root with `.venv/bin/python` so `jarvis` imports.

## Phase 4 — grade both

Grading is a separate step on purpose: the thing producing the score is never
the thing that produced the answer.

    .venv/bin/python -m swecompare grade --run-dir swecompare/runs/<cell-A-dir> --workers 2
    .venv/bin/python -m swecompare grade --run-dir swecompare/runs/<cell-B-dir> --workers 2

**Keep `--workers` at 2 or less.** This machine has 16 GB and parallel SWE-bench
evaluation is where memory goes.

## Phase 5 — read the result

    .venv/bin/python -m swecompare report

Prints resolved/total, rate, and a 95% Wilson interval per cell.

**How to read it.** At N=30 the interval is roughly ±18 points near 50%. If the
two intervals overlap, the honest conclusion is *no detectable difference at this
N* — not a ranking. Detecting a 10-point difference needs ~200 instances. The
report says this at the bottom for exactly this reason.

**Check the network audit line.** Both harnesses have a shell and every instance
is a merged PR that could be looked up. Web tools are disallowed and the test
container runs `--network none`, but the host shell is not sealed. `grade` prints
`NETWORK AUDIT` if any instance ran network-shaped commands. Investigate before
trusting the score; it is detection, not prevention.

## Phase 6 — only if the gap is large

    .venv/bin/python -m swecompare run \
      --harness jarvis --model anthropic/claude-sonnet-5 --size 30

Cell B vs C isolates the **model**; cell C vs A isolates the **harness**.
Sonnet through OpenRouter is the expensive one — budget accordingly.

## Gotchas already hit (do not rediscover these)

- **Unknown instance ids used to report success.** An id not in Verified yields
  an empty list, and "0 of 0 missing" printed as `built: 1/1`. It errors now, but
  the shape of that bug is worth remembering.
- **`total_instances` in the swebench report is 500**, the whole dataset — not
  what you ran. Using it made a perfect 1-instance run print `1/500 = 0%`. The
  denominator is `submitted_instances`.
- **Prebuilt image names replace `__` with `_1776_`** once a namespace is
  involved. Pulling the wrong name fails as "repository does not exist", which
  reads exactly like "nothing is published" and nearly cost hours of local builds.
- **`run_evaluation` deletes instance images when it finishes.** Fetch them with
  `swecompare images` *before* the runs, not after grading.
- **Containers run as the host user.** As root they wrote `__pycache__` into the
  bind-mounted workspace that the host could not delete, breaking the second run
  on any instance.
- **Do not grade a run in flight.** A long-bench run scored 48% at seventeen
  minutes and 95% at the end; the agent was mid-self-correction. Wait for it.

## Design notes worth not relitigating

- **The repo's own tests grade.** Nothing in `swecompare` scores anything —
  `swebench.harness.run_evaluation` does, in its own container, against
  FAIL_TO_PASS / PASS_TO_PASS.
- **The agent can run tests** via `./run_tests <pytest args>` in the workspace,
  executing inside the instance's real environment. Editing blind would measure
  something much narrower than agentic programming.
- **`run_tests` cannot contaminate a patch** — it and the test byproducts are in
  `.git/info/exclude`, which is local to the clone and never part of a diff.
- **Editing tests cannot help.** The eval script restores every test file to the
  base commit and applies the official test patch before running.
- **The subset is seeded and repo-stratified** (max 25% from any one repo).
  Django is 231 of 500 instances; unstratified, half the sample would be Django.
  Same `--seed` and `--size` reproduces the same 30 instances exactly.
