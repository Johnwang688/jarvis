# WP11 — TaskRunner: roles, the intake gate, the lifecycle

## Built
- `roles.py` — a `RoleBrief` per §10.2 role, the five contracts, `parse_block`.
- `runner.py` — `TaskRunner(TaskControl)`: a worker thread per running task,
  `MAX_ACTIVE=3` with the rest queued in order, the §10.1 lifecycle driven
  **off the task's state on disk** so a resume re-enters where it left off.
  Blocking questions park in CLARIFYING and publish `task_question`; assumable
  ones proceed with their assumption carried into the plan prompt. A failing
  review loops once with the findings as the next instruction, then blocks.
  Steering lands at a boundary as `[owner steering] …` (`[voice note]` when
  spoken) before the orchestrator is asked for the next instruction. Cancel and
  ceilings set a flag, interrupt through the daemon, and act after
  `TURN_FINISHED` — invariant 3. `router.on_event` runs synchronously per
  consumed event; `_publish` writes `task_status_changed` after every write.
- `daemon.py` — `self.runner = None`, `POST /tasks/{id}/start|steer|cancel|
  resume|answer` (`ControlError` → 409), and `main()` building Router +
  TaskRunner, `serve()` after start, `stop()` before the hatch. Nothing else is
  touched, so WP10b's gateway edits merge clean.

## Structured-output contracts as shipped (`roles.CONTRACTS`)
`parse_block(text, shape)` takes the **last** fenced ```json block (a bare
top-level object is the only fallback) and returns `None` on any failure; the
runner re-asks once with `roles.REASK + the contract`, then blocks the task.

| shape | required | defaulted | refused |
|---|---|---|---|
| `spec` | `goal`, `deliverable`, `acceptance` (non-empty) | `constraints` `[]`, `questions` `[]` | a question without a boolean `blocking` |
| `plan` | `steps` (non-empty) | — | an empty step list |
| `next` | `done` (bool) | `next` (str or null) | `done:false` with no `next` |
| `review` | `pass` (bool) | `findings` `[]` | `pass:false` with no findings |
| `report` | `done` (non-empty), `changed`, `verified` | `open` `[]`, `next` `""` | — |

`spec` requires the three keys that make a spec mean something: re-asking a
whole spec over a missing `"constraints": []` buys nothing. `report.cost` is
**ignored if the model sends it** — the runner fills it from the task's threads
(dollars under the provider name, work tokens under `<provider>_tokens`), §10.3.

## Tests
Run each with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python <file>`.
`tests/v2/runner_check.py` → `Ran 38 tests in 3.483s` / `OK`. All twelve other
`tests/v2/*_check.py` re-run and exit 0, unchanged last lines — the three this
package could have broken: `daemon_check` `Ran 15 tests in 1.443s`/`OK`,
`router_check` `Ran 21 tests in 0.396s`/`OK`, `permissions_check` `all v2
permissions checks passed (349 checks)`.

All free: scripted fakes, temp stores, loopback HTTP only; no `claude`/`codex`
process and no network. **Verified to bite** — six mutations each fail their own
check: no re-ask; no steering delivery; transitions unpublished; `allowed_tools`
sent to Codex verbatim; a second verify failure looping instead of blocking; a
restarted orchestrator carrying no durable state. A seventh (moving one of two
`_deliver_steering` calls) passed, exposing a weak ordering assertion — now
rewritten to assert steering precedes the next-instruction ask, and it bites.

## Workarounds and proposed interface changes
- **`Brief.allowed_tools` is resolved per provider** (`RoleBrief.tools_for`):
  the list on Claude (WP3 maps it to `tools`), `None` on Codex, which
  `codex_config` refuses outright — a read-only role sending its list to Codex
  would `BriefRefused` a task for a reason unrelated to the task. Propose the
  fixed interface say what `allowed_tools` means, or carry a capability flag.
- **A task write inside another module gets one snapshot, not one per save.**
  `worktrees.ensure` and `router.resolve` write the task themselves and the
  runner publishes after the call. Propose a store-level change hook, so
  "every write is published" is mechanical rather than a convention.
- **The proposal reply reaches the caller over the bus, not the send response.**
  `POST /threads/{id}/send` is 202 + `turn_id` and returns before the turn runs,
  so `on_proposal` publishes `proposal_reply {turn_id, reply}` (also in
  `runner.proposal_replies[turn_id]`); callers correlate on `turn_id` over
  `GET /events`. Propose a blocking send variant, or that WP12 adopt this.
- **FAILED is terminal in `model.TRANSITIONS`**, so `resume` refuses it with a
  sentence rather than letting StoreError surface — `TaskControl`'s docstring
  says "BLOCKED/FAILED"; propose changing the docstring or the table. Likewise
  BLOCKED has no edge back to VERIFYING, so a task blocked there resumes into
  RUNNING, the phase read from the journal's last transition.
- `serve()`/`stop()` are the service verbs (`start` is a TaskControl verb);
  `serve()` re-admits PLANNED/RUNNING/VERIFYING and unblocked CLARIFYING tasks,
  never INTAKE — an unstarted task is one nobody has said go to yet.
- `approvals` is stored but unused (they reach tasks through the daemon's permit
  factory and `hatch.py`'s bus subscription); kept for a per-task scope. No
  researcher is dispatched by the lifecycle — its brief exists for later
  fleet-style use — and `MAX_STEPS=40` guards the implementer loop, not work.
