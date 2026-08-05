# Roadmap: Jarvis as an always-on away-agent

Draft 2026-08-03. The goal: hand Jarvis a goal from a phone, leave the house,
and come back to finished work, parked questions, and an honest audit trail —
an OpenClaw-shaped agent, built on Jarvis's existing confinement design rather
than by trading it away.

The premise of the sequencing: three attended-desk assumptions have to be
retired **in order** — (1) every turn originates from a surface, (2) approval
blocks a thread with a human minutes away, (3) the only unattended agents are
deliberately neutered. Each phase retires part of one assumption and is
independently useful if the project stops there.

## What never changes (decided; do not relitigate mid-build)

- **Workflows keep SAFE_TOOLS + deny-all.** The goal runner is a *new*
  executor class with different supervision, not a loosened workflow.
- **The goal runner never gets mode "all".** Away-mode autonomy comes from
  scoped, expiring, goal-tagged pre-authorization (phase 3), not from
  removing the gate.
- **Triggers stay owner-only** (`should_respond` rules unchanged), consent
  and provisioning stay human-only CLI commands, credentials stay
  use-but-never-see.
- **No framework.** The runner is the same hand-rolled loop, called in
  slices.
- **Fail-stop-and-DM is the universal failure mode.** Every ceiling, error,
  or confusion ends in a parked goal and a `discord_dm_owner`, never a retry
  loop.

## Phase 0 — Foundations (existing roadmap items promoted to prerequisites)

These were "nice to have" for a desk agent; unattended multi-hour runs make
them load-bearing. Mostly already listed in CLAUDE.md's long-horizon part 1
review.

| item | why it gates away-mode |
|---|---|
| Step-budget handoff (yield, don't die) | becomes the runner's slice boundary in phase 2 |
| Spill-don't-drop truncation (full result → `traces/`, pointer in transcript) | a 6-hour run cannot afford unrecoverable truncation |
| Real `prompt_tokens` from `llm.Reply` driving compaction | the chars÷4 estimate drifts worst exactly on long runs |
| Repetition detection (same call+args k times → intervene) | unattended, a stall burns money for hours; today it is invisible to the loop |
| Durable workflow journals | template for the goal journal; restart-forgets is unacceptable once nobody restarts things by hand |

Also in phase 0: close the PENDING live-validation list in CLAUDE.md
(Playwright suites, `plan_write` live behavior, `run_subagent` economics).
Item 4 (a real run past the compaction threshold) is absorbed by long-bench
(see `docs/long-bench.md`).

Tests: each item gets a free synthetic check in the existing style
(`tests/*_check.py`), written to fail against the old code.

Exit gate: all free suites green; long-bench pilot run completes without a
wire 400.

## Phase 1 — `jarvis daemon`: a headless service (size: S)

> **Status 2026-08-03: built.** `jarvis/daemon.py` + `jarvis daemon
> [run|install]`, the Discord core shared via `jarvis/discord_agent.py`,
> face/daemon mutual exclusion (health endpoint on 8405 as the lock, face
> detaches its remote approval channel when the daemon owns the gateway),
> `tests/daemon_check.py` green. Remaining for the exit gate: the 72-hour
> soak under systemd with real host sleep/wake.

Today the Discord gateway and the approval broker start under `jarvis face`
— away-mode Jarvis requires a HUD window to nobody. Extract the always-on
core.

Build:
- `jarvis/daemon.py` + `jarvis daemon` subcommand: Discord gateway, approval
  broker with the remote (DM) channel, sessions, **no** HUD, no browser
  window, no TTS pre-warm. Refactor the startup that `face/server.py`
  currently owns into a shared runtime module both entry points use; the
  face becomes "daemon + window".
- Service-ization, human-installed: `jarvis daemon install` *prints* a
  systemd user unit (`Restart=on-failure`, `WantedBy=default.target`) and
  the `loginctl enable-linger` command for the owner to run (sudo-free).
- WSL keepalive: a Windows Scheduled Task at logon running
  `wsl -d Ubuntu --exec ...` to hold the VM open, documented alongside the
  power-plan requirement (host sleep kills everything; away-mode means the
  desktop stays awake). Document, verify, don't automate — this is the
  owner's machine policy.

Tests: `tests/daemon_check.py` — broker reachable with no face routes,
gateway response rules unchanged headless, clean shutdown stops the browser
session (the EPIPE rule), a face attaching to a running daemon does not
double-start the gateway.

Exit gate: 72-hour soak. Daemon survives host sleep/wake (gateway
fresh-IDENTIFY reconnect already exists), a `kill -9` restart resumes clean,
zero orphaned Chromium/Node processes, Discord DM round-trip works at hour 72.

## Phase 2 — Goal store + goal runner (size: L, the heart of the project)

> **Status 2026-08-03: built**, plus two items pulled forward at the
> owner's request: interval progress DMs (cost, last activity, live plan)
> and mid-goal steering (`steer: …` interrupts the in-flight turn at a
> step boundary and lands as the next slice's `[owner steering]` message).
> `goal status` / `goal cancel` verbs came with them (phase 5's `pause`
> and `log` remain). Ceilings (dollars/hours/slices) shipped with the
> runner per the interleave rule. `tests/goals_check.py` green. Remaining
> for the exit gate: the real multi-hour goal with a daemon restart
> mid-run.

A goal is not a turn; it is a loop of turns with budgets and a lifecycle.

Build:
- `jarvis/goals.py` — durable store at `~/.local/share/jarvis/goals/<id>/`
  (outside the repo, like sessions): `goal.json` (statement, status:
  queued / running / parked / blocked / done / failed / cancelled; budgets:
  $ / wall-clock / steps; session_id; scope grants once phase 3 lands),
  `journal.jsonl` (append-only: every slice, tool denial, park, resume,
  DM sent — the audit trail), created/updated stamps.
- `jarvis/goalrunner.py` — the executor. One slice = one `run_turn` on the
  goal's own session (sessions already give persistent, compaction-managed
  transcripts for free). Between slices — the only place the transcript is
  guaranteed wire-whole, same reason cancel lives there — check budgets,
  check repetition, check for owner control verbs, then continue, yield, or
  park. The phase-0 step-budget handoff is the yield mechanism: "out of
  steps" becomes "journal state, reschedule next slice", not a failure
  string.
- Toolset: the main agent's, minus desktop (v1: foreground-stealing is a
  desk feature) — browser included, guarded by a mutex so one goal browses
  at a time (SESSION is a process singleton). Approver: the remote/DM
  approver. v1 approval behavior: a denial or timeout **parks the goal**
  with a DM, rather than burning the turn — crude but safe until phase 3.
- Intake: Discord DM verb (`goal: rank these 5 papers and draft an email`),
  CLI (`jarvis goal "..."`). Confirmation echo before it queues.
- Restart-safety: `jarvis daemon` reads `goals/` on boot and resumes
  `running`/`queued` goals from their sessions.

Tests: `tests/goals_check.py` (store round-trip, restart resume, budget
accounting, journal append-only, cancelled state); runner lifecycle against
a faked `llm.chat` (slices, park-on-deny, resume-without-redo).

Exit gate: a real multi-hour, safe-tools goal (research + files + memory)
given via Discord, with the daemon deliberately restarted mid-run; the goal
completes, the journal reads coherently end to end, nothing was redone.

## Phase 3 — Away-mode approval economics (size: M, the hard design)

Two steps, shipped separately:

1. **Park-and-continue.** The runner's approver wrapper returns a distinct
   "parked pending approval" outcome: the tool result says so (invariant 4 —
   failures as text), the step is recorded in the journal, the DM goes out
   with the existing one-shot code, and the goal either continues on
   non-blocked work or yields. When the owner's reply lands, the runner
   resumes with an explicit user-visible turn ("approved: <command>") so the
   transcript, not hidden state, carries the authorization (the
   confabulation lesson: state the model can't see is state it will invent).
   The 10-minute broker timeout stops mattering — a park has no deadline.
2. **Scoped pre-authorization.** At intake, Jarvis proposes the scopes he
   expects ("needs: `git`, `uv`, writes under ~/projects/foo") and the owner
   approves the *set* once by DM code. Extend `permissions.py` with
   goal-tagged, expiring entries: same first-word command matching, dies
   with the goal, never written to the global allowlist. ALWAYS remains a
   separate, deliberate, global act.

Not built, ever: an LLM judging whether its own pending call is safe.

Tests: extend `tests/permissions_check.py` (scope expiry, goal isolation,
no leakage into the global list) and `tests/discord_approvals_check.py`
(parked resolution after hours, replay-dead codes, two parked goals not
cross-resolving).

Exit gate: a goal needing three gated actions completes overnight on one
up-front scope approval and zero mid-run DMs; the same goal with the scope
declined parks cleanly at the first gated step.

## Phase 4 — Guardrails (interleaved: ceilings land WITH phase 2, not after)

- **Spend ledger + ceiling.** `llm.py` already computes per-call cost; a
  ContextVar ledger aggregates per slice into the goal's budget. Hard stop →
  park + DM ("spent $1.40 of $1.50 ceiling; here's where I am"). Default
  ceilings conservative and per-goal overridable at intake.
- **Wall-clock ceiling** per goal, same park semantics.
- **Repetition intervention** (phase 0 detector, wired): k identical
  call+args → inject one nudge turn; still looping → park. Turns the
  gpt-oss-20b 16-round stall from invisible to self-terminating.
- **Kill verb**: `cancel <id>` from Discord takes effect at the next step
  boundary (the existing `should_stop` mechanism, invariant 3 preserved).
- Injection posture: no new mechanism yet — measure first via long-bench's
  embedded injection turn, then decide whether unattended runs need a
  stricter prompt variant.

Exit gate: an adversarial self-test — a deliberately impossible goal ("make
this repo's tests pass" where they can't) parks at a ceiling with a coherent
DM, never loops, never exceeds budget.

## Phase 5 — Control plane + observability (size: S)

- Gateway DM **verbs, parsed not modeled** (like approval replies —
  deterministic, owner-only, same-channel rules): `status`, `goals`,
  `pause <id>`, `resume <id>`, `cancel <id>`, `log <id>`.
- `status` reports per goal: state, current plan (the plan slot's second
  reader), spend vs ceiling, last journal line.
- **End-of-goal digest DM**: done / spent / skipped / parked-awaiting-you.
- CLI audit: `jarvis goals log <id>` pretty-prints the journal.

Tests: extend `tests/discord_gateway_check.py` — verbs owner-only, a verb is
never treated as a goal, `status` with zero goals, digest content assembled
from the journal (not from the model).

Exit gate: a full away-day dogfood — two goals given from a phone, digests
received, audit read afterwards, nothing surprising in it.

## Phase 6 — Benches as release gates

- **long-bench** (`docs/long-bench.md`) — the capability meter for exactly
  the runs away-mode produces. Gate: no wire-400s, recall and revision
  scores at or above the recorded Luna baseline.
- **away-bench** — the goal runner in the agent-bench sandbox, zero
  approvals granted: grade completion of the ungated subset, clean parking
  of gated steps, spend under ceiling, honest final report of what's left.
  Empty-run-scores-zero rule applies; a runner that does nothing parks
  nothing and reports nothing.
- Promotion rule: **unattended mode is enabled for a model/config only after
  it passes agent-bench `pressure`, away-bench, and long-bench.** A new
  default model re-runs all three.

## Phase 7 — Proactive layer (deliberately last)

Scheduled runs (morning-briefing on a timer — the OpenClaw heartbeat),
calendar integration on the use-but-never-see template, more desktop apps,
always-on wake word. All of it is *more autonomy on the same rails*, which
is why it waits for the rails: guardrails proven in live use, benches green,
a month of away-day dogfooding without a surprise in the audit trail.

## Sequencing rationale, in one paragraph

Phase 1 is smallest and unlocks everything (nothing else matters if Jarvis
dies with the terminal). Phase 2 is the core and is safe-by-crudeness at
first (every gated call parks). Phase 3 makes it *useful* unattended, and is
deliberately after 2 so the approval design is informed by real parked-goal
logs rather than guessed. Guardrails (4) ship interleaved with 2 because the
first unattended run — not the tenth — is the one that loops for six hours.
Observability (5) before proactive (7), because an agent that acts on its
own schedule must first be an agent you can interrogate from a phone. The
benches (6) are the graduation exam, and away-bench doubles as the safety
regression suite for every future model swap.
