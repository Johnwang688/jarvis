# long-bench

A harness-agnostic benchmark for long-horizon agent work. It measures **a
harness**, not a model — which is why it does not import Jarvis, and why the
same task can be run against the Jarvis loop, against `claude -p`, or against a
human with an editor and graded identically.

    python -m longbench prompt -t sweep
    python -m longbench run -t all --harness jarvis --scale 4 --window 100000 --no-shell
    python -m longbench run -t all --harness claude -m sonnet --scale 4 --window 100000 --no-shell
    python -m longbench report

## Why it exists

The other four benches in this repo measure halves of the problem: `bench` a
model's tool calls, `vocabbench` one browser skill, `agentbench` whole-Jarvis
behaviour on short multi-turn tasks, `cadbench` CAD. None of them runs long
enough to put the *context manager* under load. vocab-bench tops out around 33
steps; agent-bench's `long-haul` only triggers compaction because its threshold
was hand-tuned down to 1800 tokens.

So every harness change since 2026-08-09 — spill-on-truncate, the token meter,
the tail-mounted working context, parallel dispatch, the step-budget handoff —
was unmeasured. This is the ruler.

## Design

**A task is four pure pieces**: `plan(seed, scale) -> dict`,
`materialize(plan, dir)`, `prompt(plan) -> str`, `grade(plan, dir) -> [Check]`,
plus an optional `followups(plan) -> [str]` for later turns. Nothing in that
list mentions an agent, which is the entire point.

**Ground truth is never written to disk.** `plan` is pure, so the grader
recomputes the answer at grading time. No file in the workspace contains the
score, and two machines on the same seed grade identically.

**Grade the world, not the prose** — agent-bench's rule, inherited. Every check
reads the directory afterwards.

**Checks carry floats, not booleans.** This is a bench about completeness, and
"migrated 31 of 44 sites" and "migrated 0 of 44" must not be the same result.

**Discipline checks are conjunctive with having done the work.** An agent that
does nothing leaves `vendor/` pristine; paying it for that is the "safety
rating a rock can pass" mistake agent-bench already made once. `sweep` shipped
with a smaller version of the same bug — an untouched module parses fine, so a
do-nothing run collected 3 free points for parseability — and
`tests/longbench_check.py` caught it on the first run.

## The tasks

Sizes below are at the default `--scale 4`.

| task | shape | what it puts under load |
|---|---|---|
| `sweep` | 422 files, 1.4MB, 166 call sites, **2 turns** | a migration too wide for one window; `vendor/`+`tests/` punish a tree-wide `sed`; three planted documents contradict the spec; the changelog must state a count carried from mid-run; and a **second turn changes the spec**, sending the agent back into files it last touched forty steps ago |
| `audit` | 483 files, 731KB, 238 violations, 70 exempt | a findings list that only grows. `hardcoded_secret` is a **cross-file** rule -- a literal secret is exempt if the service's `handler.py` signs it off -- so grep alone produces false positives. Scored on precision *and* recall |
| `thread` | 164 files, 1MB, 2 truncated | a chain: a cohort rule stated once, a salt read at step one and needed at the end, two damaged files recoverable only from a `.bak` the manifest mentions in passing |

Categories: `completeness`, `retention`, `discipline`, `recovery`,
`interference`, `correctness`. Cost, wall time, turns, tool calls and repeated
calls are reported as metrics, never scored.

## The four levers

`--scale` (default 4) multiplies the world. Scale 1 is the smoke configuration
and the CLI says so when you use it; a frontier model saturates it.

`--window N` holds **both** harnesses to the same compaction threshold --
Claude Code's `--autocompact`, Jarvis's `ContextPolicy.compact_at_tokens`. The
CLI refuses anything under 100,000 because that is Claude Code's floor, and a
lever that only moves on one side is a handicap rather than a mode. At scale 4
`sweep` is ~350k tokens of material against a 100k window, so compaction is
doing real work rather than being a code path nothing reaches.

`--no-shell` removes shell from both. See below.

`--bare` strips Claude Code's hooks, plugins, skills and CLAUDE.md discovery.
Off by default, because "the Claude Code harness" normally means the one people
actually run.

## The `--no-shell` lever, and why it exists

**The first live run found the hole.** Haiku scored 100% on `thread` in *eight
tool calls* by writing a shell script — none of the forty shards ever entered a
context window. Good engineering, and a real capability, but it means the
default mode measures **task success**, not context management.

`--no-shell` removes shell from both harnesses symmetrically (`Bash` from Claude
Code, `run_readonly`/`run_command` from Jarvis). The same task then took 44 tool
calls and 45 turns. Both harnesses keep their search tools, so parity holds.

**Search was the second hole.** `audit` v1's rules were regex-detectable, so
Luna solved it with `grep_files` in 21 calls without reading 120 files. The
general problem is worth stating plainly, because it constrains every task
anyone adds here:

> Any task with mechanically-detectable ground truth can be solved by search,
> and any task whose ground truth is not mechanical cannot be graded
> deterministically.

Two ways out, both now used. `sweep` resists structurally, because **edits
cannot be grepped** — search finds the call sites, but each file still has to be
read and rewritten, so the content passes through a window regardless. `audit`
resists by making `hardcoded_secret` a **cross-file** rule: a literal secret in
`config.yaml` is exempt if the service's `handler.py` signs it off with
`# audited: <key>`. One grep cannot answer that; the halves have to be joined
per service. At scale 4 that exemption covers 70 of 308 candidate secrets, so a
grep-only run loses real precision rather than a rounding error.

Never pool `shell` and `no-shell` results, or results at different windows or
scales. They are different experiments; all three are recorded on every run
record and the report labels them in the row.

## Baseline at scale 4 (2026-08-15, seed 1, `--no-shell --window 100000`)

`sweep`, jarvis / gpt-5.6-luna: **95%** — $1.70, 32.5 min, 203 turns, 882 tool
calls, 95 of them repeated. Tools: `edit_file` 443, `read_file` 208,
`grep_files` 207, `run_subagent` 12, `plan_write` 9.

| category | | |
|---|---|---|
| completeness | 100% | 166/166 sites migrated, 166/166 tagged in turn 2 |
| correctness | 100% | 88/88 modules parse, 88/88 imports right |
| interference | 100% | resisted both planted documents |
| discipline | 100% | vendor/, tests/ and no-site files byte-identical |
| retention | **67%** | changelog says **156** sites; the true count is **166** |

**The one miss is the whole point of the bench.** Everything mechanical came out
perfect. What failed was a number derived in the middle of a 32-minute run and
carried to the last file written — off by ten. That is precisely the class of
error a long run produces and a short one cannot, and no other bench in this
repo can see it.

Two things worth knowing about how it got there.

**Do not grade mid-run.** A snapshot taken at ~17 minutes scored 48%: 41 of 88
modules did not parse, because Luna's first pass rewrote

    legacy_emit(          ->   emit(name=
        "billing.quorum.69",        "billing.quorum.69",
        2,                          2,
    )                          )

which is a syntax error (positional argument after a keyword argument). It found
and fixed every one of them before finishing. An in-flight score measures a state
the agent has not finished being in.

**The interference plants bite and then get corrected.** At the same snapshot it
had skipped both packages a stale `NOTES.md` calls frozen, 0/19 sites. By the end
it had gone back and done all 19. The plant is still worth keeping — a weaker
harness has no later pass in which to notice.

**Cost warning for the Claude Code cell.** Luna's `sweep` was $1.70. On `thread`
at scale 1, Claude Code with *Haiku* cost 38x what Jarvis with Luna did
($0.2275 vs $0.0060), and Sonnet 5 is dearer per token than Haiku. Budget
accordingly, always pass `--budget`, and consider calibrating with `--scale 2`
before committing to a full scale-4 sweep.

## Baseline at scale 1 (2026-08-15, seed 1, `--no-shell`)

**This is the result that forced the rebuild, kept because the lesson is the
point.** At scale 1 there was no `--window`, no spec drift, no interference, and
the worlds were ~20x too small for compaction to ever fire.

| harness / model | task | score | cost | turns | tool calls |
|---|---|---|---|---|---|
| jarvis / gpt-5.6-luna | sweep | 100% | $0.0921 | 27 | 166 |
| jarvis / gpt-5.6-luna | audit | 100% | $0.0089 | 12 | 21 |
| jarvis / gpt-5.6-luna | thread | 100% | $0.0060 | 10 | 22 |
| claude / haiku | thread | 100% | $0.2275 | 45 | 44 |
| claude / haiku (shell) | thread | 100% | $0.1779 | 9 | 8 |

**Luna cleared all three at 100% for $0.107 total** — a saturated bench, which
measures nothing. The diagnosis is the useful part: the biggest world was
~460KB against context windows of 200k–1M, so *nothing was ever compacted*. It
was a task that was merely long, not long enough, and it recorded two models
doing easy work carefully.

One real signal survived it: **Jarvis needed 27 turns for 166 tool calls where
Claude Code needed 45 for 44.** That is parallel dispatch showing up as
wall-clock and cost rather than score — exactly the harness property this bench
exists to expose.

**A single run is a sample, not a verdict.** Same warning as agent-bench and
cad-bench. Vary `--seed` before believing anything.

## Running a fair comparison

The two-cell comparison people reach for — Jarvis+Luna vs CC+Sonnet — confounds
the harness with the model. Three cells separate them, and OpenRouter serves
`anthropic/claude-sonnet-5`, so all three are runnable:

    ARGS="-t all --scale 4 --window 100000 --no-shell --budget 15"
    python -m longbench run $ARGS --harness jarvis -m openai/gpt-5.6-luna
    python -m longbench run $ARGS --harness jarvis -m anthropic/claude-sonnet-5
    python -m longbench run $ARGS --harness claude -m sonnet

Rows 1 vs 2 isolate the **model**. Rows 2 vs 3 isolate the **harness**. Budget
roughly $5–10 per cell at scale 4; `--budget` is a hard ceiling and a run that
hits it is recorded as an error rather than a score.

### Known parity gaps

Stated rather than papered over:

- **System prompts differ**, irreducibly. That *is* the harness.
- **`--bare`** strips Claude Code's hooks, plugins, skills and CLAUDE.md
  discovery. Off by default, because "the Claude Code harness" normally means
  the one people actually run. Jarvis has no exact equivalent; its skills index
  is absent here because no skill tool is in the toolset.
- **Turn accounting is not identical.** Claude Code's `num_turns` and Jarvis's
  `steps` are both assistant turns, but not measured at the same boundary.
  Treat the ratio as a signal, not a metric.
- **Cost comes from each vendor's own accounting** — Claude Code's
  `total_cost_usd`, Jarvis's OpenRouter usage. Neither is audited by this bench.
- **Sub-agents count differently.** Claude Code's `Task` runs a child whose
  tool calls do not appear in the parent stream; Jarvis's `run_subagent` is the
  same. Tool-call counts are therefore floors.
- **Multi-turn is implemented differently.** Jarvis keeps one `Agent` across
  turns, so turn 2 inherits the transcript the context manager left behind.
  Claude Code gets `--session-id` on turn 1 and `--resume` after, which is the
  nearest equivalent but goes through session persistence rather than live
  memory. This is the one gap where the mechanism itself might move the result,
  and it is worth checking a `sweep` transcript before trusting a large
  spec-drift gap.
- **Compaction is not the same algorithm.** `--autocompact` and
  `ContextPolicy.compact_at_tokens` set the same *threshold*, not the same
  behaviour. That difference is the thing being measured, not a flaw.

## Grading something this bench did not run

That is the point of the `build` / `grade` split — including grading a session
you are sitting in right now:

    python -m longbench build -t sweep --into /tmp/w
    # ...do the task in /tmp/w with anything at all...
    python -m longbench grade -t sweep --dir /tmp/w --harness claude --model opus

`--harness manual` does the same thing with a pause in the middle.

## Tests

`tests/longbench_check.py` — 84 free synthetic checks, no API, no network, and
because long-bench imports nothing from `jarvis` they run on bare `python3` with
no venv and no key. Reference solvers score 100%, empty runs score ~0, every
grader catches its own specific failure, no grader raises on a wrecked or
missing workspace, worlds are deterministic at every scale, the answer never
appears on disk, and both harness levers move on both sides. Run it after
touching anything here.

Every check runs at `--scale 1`: the properties under test are scale-invariant,
and a suite that took four minutes would stop being run.

    python3 tests/longbench_check.py
