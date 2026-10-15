# Fast path edits directly — owner decisions (2026-10-14)

The owner asked: "can we allow the jarvis harness to make edits directly and
do stuff instead of being forced to open a task — I'm only telling it to work
when the work is pretty direct anyways."

This amends design D2 and §8.1 ("the fast path has no tool that can change
anything"). The owner chose, from the options offered:

- **F-1. The Jarvis loop (the OpenRouter fast path) gets edit tools.** It holds
  `write_file`, `edit_file`, `run_command` and `run_readonly`, so it can do
  small, direct work in place instead of proposing a task. It is not the
  "use a Claude/Codex chat thread" option, and Discord and schedules are not
  included.
- **F-2. Only the owner's own chat turns.** That means the owner's chat thread,
  on a turn the owner is present for at the HUD (typed or spoken). Discord,
  the DM, schedules, the escape hatch and task threads stay read-only, as
  they are today. A steer from anywhere else takes the turn off the desk for
  the rest of the turn, as it already does for the desk tools.
- **F-3. Approvals follow the allowlist.** The tools stay `dangerous` and go
  through the existing gate: `dispatch` → `permissions.gate` → the daemon's
  §6 permit → the broker. Allowlisted commands run without asking. Everything
  else asks the owner. Deny wins. Protected state, credential and owner-op
  refusals are unchanged.

  *Amended by review, 2026-10-14 (PR #182, the build and its independent
  review):* the route as built is not `permissions.gate`. The fast path's
  approver asks the daemon's §6 permit directly, with no v1 human-backed
  flag, because `gate` marks any approver human-backed and would let the
  allowlist and rules' ALLOW answer before the always-ask rules and behind a
  `DenyAll` asker. The permit applies the allowlist and rules' ALLOW itself
  (layer 4, human-backed asker only). Under the AUTO profile the permit is
  asked with an ASK copy of the brief, because the fast path has no reviewer
  for layer 5 to defer to. `write_file`, `edit_file` and `run_readonly` are
  not `dangerous` in v1 and are put to the same permit through
  `runtime.ask_tools()`; ordinary reads still run unasked through rules'
  ALLOW. A yes that lands after Stop, or after the turn left the desk, does
  not run.

Lead's defaults (not asked; open to amendment):

- **F-4.** A turn that holds the edit tools gets a larger step budget (30).
  Read-only turns keep 8. Running out of steps still yields a task proposal.
- **F-5.** The prompt changes from "propose a task for any edit or command"
  to "do small, direct work yourself; propose a task for large multi-file
  work, anything that wants review or a PR, or work past your budget". It is
  narrowed by `config.narrow_prompt` like every other tool paragraph.
- **F-6.** Edits land in the thread's project folder (its `cwd`), in place.
  No worktree, since the fast path has no git tools. The prompt says to keep
  the owner's uncommitted changes intact and not to commit, push or deploy
  unless asked.
- **F-7.** Still forbidden: spawning (`run_subagent`, `run_fleet`,
  `delegate`), the `git*` tools, `task_start` and the v1 task/workflow verbs,
  `load_tools`, `skill_write`, `memory_delete`, and the `browser_`/`desktop_`/
  `cad_`/`gmail_`/`discord_` prefixes.
