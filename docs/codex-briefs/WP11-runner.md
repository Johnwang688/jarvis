# WP11 — TaskRunner: roles, the intake gate, the lifecycle

Work package 11 of the Jarvis v2 redesign, in a git worktree on branch
`jarvis/wp11-runner`. Claude leads and you are Claude. This is the package
that turns every merged piece into a task that runs end to end. Read first:

1. `docs/jarvis-v2-design.md` — **§10 in full**, §8.1 (proposals → tasks),
   §8.2 (every worker thread is routed), §8.4 (pinned providers, the
   orchestrator restart), §6.1, §11.3 (what the Reporter needs from you),
   §14 row WP11.
2. Fixed interfaces: `jarvis/v2/model.py` (`Task`, `Spec`, `OpenQuestion`,
   `Status`, `Report`, `TRANSITIONS`), `provider.py`, **`control.py`**
   (`TaskControl` — you implement it exactly).
3. Merged modules you compose, none of which you edit: `stores.py`
   (`TaskStore.transition` is the only way state moves), `worktrees.py`,
   `daemon.py` (`open_thread`, `send`, `interrupt`, `answer`, the bus,
   `permit_factory`), `router.py` (`resolve`, `on_event`, `ready_to_start`,
   `cancel_proposal`, `next_worker`, `orchestrator_unavailable` — read
   `WP9-notes.md` for the calls it says WP11 owes), `ledger.py`
   (`ceiling_hit`), `permissions.py` + `approvals.py` + `hatch.py`
   (`WP5-notes.md`), `discord/reporter.py` (`WP10a-notes.md`: it needs a
   `task_status_changed` bus record `{kind, task_id, project_id,
   data: to_json(task)}` after **every** task write).
4. `jarvis/goalrunner.py` (v1) — the slices/steering/verify-slice/ceiling
   design this replaces; `tests/goals_check.py` for the scripted-agent test
   style. `jarvis/agents.py` for v1's role briefs (explorer/reviewer…).

## Deliverables

### `jarvis/v2/roles.py`

One `RoleBrief` per §10.2 role: `system_append` text (the role's job, what
it must never do — the orchestrator never implements, the reviewer never
edits, the researcher returns findings only), default tool allowance
(`Brief.allowed_tools`, honouring each provider's meaning of it — see
`WP3-notes.md`), `max_turns`, and the **structured-output contract** the
runner parses:

- Orchestrator intake reply: a fenced ```json block `{goal, deliverable,
  acceptance: [..], constraints: [..], questions: [{text, blocking,
  options?}]}` — the SPEC. The brief text states the §10.1 rule: a
  question is blocking only if two reasonable answers lead to materially
  different work; assumable questions carry an `assumed` value.
- Orchestrator plan reply: ```json `{steps: [..]}` with the verification
  step last.
- Orchestrator final reply: ```json in the §10.4 REPORT shape.
- Reviewer reply: ```json `{pass: bool, findings: [..]}`.
- Implementer/researcher: free text; the runner reads their `TEXT`.

`parse_block(text, shape)` extracts the last fenced JSON block, validates
required keys, and returns `None` on failure; the runner re-asks **once**
("reply with only the JSON block") and then blocks the task with the
reason. Never guess a spec from prose.

### `jarvis/v2/runner.py` — `class TaskRunner(TaskControl)`

Constructed with the daemon, stores, router, ledger, approvals, and the
bus; one worker thread per running task, `MAX_ACTIVE` (default 3)
concurrent, the rest queued in order. The lifecycle, exactly §10.1:

- `start`: INTAKE → CLARIFYING. `router.ready_to_start`; `worktrees.ensure`;
  resolve the orchestrator via `router.resolve(role=orchestrator, …)`
  (a `RoutingBlocked` → BLOCKED with its text); open the orchestrator
  thread with the role brief, `cwd` = worktree, the project's profile and
  `always_ask`; send the intake prompt (task brief + project name/root);
  parse the SPEC; save it; journal. Blocking questions open → publish
  `task_question` on the bus (the surfaces ask), stay in CLARIFYING.
  `answer_question` records the answer and, when none block, proceeds.
  Assumable questions proceed immediately with their assumption written
  into the spec.
- CLARIFYING → PLANNED: ask for the PLAN; save `task.plan`; `status.steps`.
- PLANNED → RUNNING: open **one implementer thread** for the task (routed
  fresh, `WP9` `next_worker`), send the spec + plan + step 1; after each
  step's `TURN_FINISHED`, the orchestrator receives the implementer's
  `TEXT` as a worker report and answers with the next instruction
  (structured: ```json `{next: "step text" | null, done: bool}`); the
  runner relays. `status.step` advances per instruction; `last_tool`/
  `last_file` from `TOOL_STARTED`/`TOOL_FINISHED` data; `cost_usd`/`tokens`
  from `USAGE`; `elapsed_s` from `started`.
- RUNNING → VERIFYING: a **fresh reviewer thread** (routed; the §8.2 soft
  preference makes it the other provider when possible) gets the spec's
  acceptance criteria, the plan, and `worktrees.status` + the branch diff
  (`git diff <base>...HEAD` in the worktree, capped); parses `{pass,
  findings}`. `pass` → orchestrator asked for the REPORT → DONE, `task.report`
  saved. Fail → RUNNING with the findings as the next instruction, **once**;
  a second fail → BLOCKED with the findings as the reason.
- Steering: `steer` queues text; it is delivered to the orchestrator as
  `[owner steering] …` at the next boundary (before the next instruction is
  relayed). Spoken steering is prefixed `[voice note]`.
- Cancel: a flag checked at every boundary; running turns are interrupted
  via the daemon; → CANCELLED; the worktree is left in place.
- Ceilings: on every `USAGE` event, `ledger.ceiling_hit(task)`; a hit
  interrupts the running turn and → BLOCKED with the ceiling named and the
  spend; `resume` requeues if the ceiling was raised (re-check first).
- `router.on_event` is called synchronously for every event the runner
  handles (it consumes proposals; the daemon's bus may drop). A proposal's
  reply text is returned to the fast-path caller by the daemon — expose
  `runner.on_proposal(reply)` if the daemon needs a hook; keep daemon edits
  minimal.
- Orchestrator provider unavailable mid-task (an `ERROR{fatal}` naming
  health, or `router` reporting it): → BLOCKED with
  `router.orchestrator_unavailable(task)` text. `resume(task, provider=…)`
  opens a new orchestrator on that provider and restarts **from durable
  state** — spec, plan, status, the journal's worker reports — never the
  old transcript; journal `orchestrator_restarted`.
- After **every** `stores.tasks.save`/`transition`: publish
  `task_status_changed` as WP10a specifies. Journal every transition and
  every relayed instruction with its thread id.
- `hatch.py` already listens on the bus; do not re-implement. Verify in a
  test that a `reviewer_declined` during RUNNING does not break the
  step loop (the hatch's `owner-ran` message arrives as a user message on
  the implementer thread, and the next instruction still relays).

### Daemon wiring (`daemon.py`, minimal additive)

Routes: `POST /tasks/{id}/start | steer | cancel | resume | answer`; the
runner constructed in `main()` and started/stopped with the daemon;
`stop()` cancels nothing (tasks stay resumable) but interrupts running
turns and joins within the bound. The fast path's proposal reply text
flows back to the caller of `/threads/{id}/send` — record how.

### Tests: `tests/v2/runner_check.py`

Free. Fake providers with scripted replies keyed on the message they
receive (the `longhorizon_check` pattern), fake router/ledger where
simpler, stores on a temp root, `V2_DATA_DIR` never real. Cover the whole
lifecycle happy path with the state sequence and journal asserted; a
blocking question stopping in CLARIFYING, `answer_question` proceeding,
an assumable one proceeding with its assumption in the spec; an
unparseable spec re-asked once then BLOCKED; roles on different threads
and the reviewer on the other provider when available; verify fail loops
once then blocks, with the findings relayed; report parsed and saved in
the §10.4 shape; status fields from events; `task_status_changed` after
every write (count them against writes); steer delivered at the boundary
with the prefix, spoken prefixed `[voice note]`; cancel at a boundary
while a turn is mid-flight; a ceiling hit → BLOCKED with spend, resume
after raising; orchestrator unavailable → BLOCKED text, resume on the
other provider restarting from disk (assert the new thread's first message
carries the spec and plan, not old transcript); proposal → task → started;
`MAX_ACTIVE` queueing; `reviewer_declined` mid-run not breaking the loop;
every `TaskControl` error path raising `ControlError`. Then run every
`tests/v2/*_check.py`.

## Rules

Stay inside `jarvis/v2/roles.py`, `jarvis/v2/runner.py`, `jarvis/v2/daemon.py`
(routes + construction only), `tests/v2/`, `docs/codex-briefs/WP11-notes.md`.
Fixed interfaces untouched (`control.py` included — implement it, do not
edit it; propose in notes). No new dependencies. When green, commit with a
message starting `v2 WP11: task runner — roles, intake gate, lifecycle`
ending with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Do not
push or merge. Finish with `WP11-notes.md` (under 80 lines): what you
built, the structured-output contracts as shipped, test commands and last
lines, workarounds, proposed interface changes. Report branch, worktree
path and notes path as your final message.
