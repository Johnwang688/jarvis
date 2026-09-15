# WP2 — FastPathProvider: the v1 loop behind the v2 provider interface

Work package 2 of the Jarvis v2 redesign (`docs/jarvis-v2-design.md`). You
are implementing it in a git worktree on your own branch. Claude leads; this
brief is your entire scope. Read first, in this order:

1. `docs/jarvis-v2-design.md` — §2 D2, §3, §5.1, §5.2, §5.5, **§8.1 (the
   whole point of this package)**, §14 row WP2.
2. `jarvis/v2/provider.py` and `jarvis/v2/model.py` — **fixed interfaces.**
   Do not edit; propose changes in your notes.
3. `CLAUDE.md` invariants 1–10 and *Safety design* — the v1 loop you are
   wrapping has rules that fail silently when broken.
4. `jarvis/agent.py` (`Agent`, `run_turn`, `Turn`, the `on_event` kinds:
   `delta`, `interim_text`, `text`, `tool_start`, `tool_end`, `cost`,
   `cancelled`, `truncated`, `context`; `stopped_early` and `_handoff`),
   `jarvis/tools/__init__.py` (`REGISTRY`, `default_names`, `dangerous`,
   `dispatch`, `runtime` binding), `jarvis/runtime.py`, `jarvis/sessions.py`,
   `jarvis/models.py` (`tier()`), `jarvis/permissions.py` (`gate`).
5. `tests/loop_check.py` and `tests/longhorizon_check.py` — the house pattern
   for faking `llm.chat` so a suite is free and deterministic.
6. `docs/codex-briefs/WP1-notes.md` / `WP6-notes.md` — conventions Codex
   has already established in `jarvis/v2/`.

## Deliverables

### `jarvis/v2/providers/fastpath.py` — `class FastPathProvider`

Implements `provider.Provider` with `name = ProviderName.FAST` over a v1
`Agent`. Create `jarvis/v2/providers/__init__.py` (empty docstring) if
absent — WP4 is writing `codex.py` beside it on another branch, so touch
nothing else in that directory.

- `health()`: `OPENROUTER_API_KEY` is set (never print it or its length —
  memory rule: check set-ness only) and `models.tier("orchestrator")`
  resolves. Never spends tokens.
- `start(thread, brief, permit)`: build one `Agent` per handle with:
  `tool_names=FAST_TOOLS` (below), `system` = `config.SYSTEM_PROMPT` +
  `FAST_PROMPT` (the §8.1 escalation test, written into the module) +
  `brief.system_append`; `model=brief.model or models.tier("orchestrator")`;
  `max_steps=FAST_MAX_STEPS = 8`; `session=` a v1 `sessions.Session` whose id
  is stored as `provider_session_id` so `resume()` restores it; `approve=`
  an adapter that calls `permit(tool_name, args, brief)` and returns
  `decision is Decision.ALLOW` — **wrapped with `permissions.gate`** so the
  human-backed flag semantics of v1 hold; a `should_stop` reading a
  per-handle `threading.Event` for `interrupt()`. `brief.profile` is
  ignored except `STRICT` → `BriefRefused` (chat is never strict).
- `send(h, message)`: run `run_turn(message.text, images=message.images)`
  on a worker thread and **yield `Event`s as they happen** (a queue between
  the v1 callback and the generator; end with `TURN_FINISHED`). Mapping:
  `delta` → `TEXT_DELTA`; `interim_text` → `THINKING`; `text` → `TEXT`;
  `tool_start` → `TOOL_STARTED` (`call_id` minted per call, `name`, `args`
  parsed back from v1's string if JSON, else `{"raw": str}`); `tool_end` →
  `TOOL_FINISHED` (`ok` = result does not start with `Error`, `summary` =
  first 200 chars); `cost` → `USAGE` with `cost_usd` and, if the agent
  exposes them, token counts; `cancelled` → `TURN_FINISHED{stop:
  "interrupted"}`; `truncated` → `THINKING{"text": "[cut off at the token
  limit, continuing]"}`. An exception from `run_turn` → `ERROR{fatal:
  True}` then stop; the generator must never hang.
- **The proposal channel, §8.1.** `TURN_FINISHED.data` carries
  `"proposal": {"brief", "project", "provider"} | None`. It is set when
  (a) the model called `task_propose` during the turn, or (b) the turn
  ended `stopped_early` — then the brief is the v1 handoff text
  (`turn.text`) prefixed `"[fast path ran out of steps] "`, `project` and
  `provider` None. (b) is the *budget rule*: exhaustion becomes a proposal,
  never a bare "stopped".
- `interrupt`: set the handle's event (v1 checks it between steps and per
  streamed line; that is enough). `answer`: `ValueError` (the fast path
  raises no approvals or questions of its own — its tools are never
  dangerous). `usage`: summed from the `cost` events since start (dollars
  known; tokens 0 unless exposed). `close`: stop the session save and drop
  the agent; idempotent.

### `jarvis/v2/tools/propose.py` — the one tool the fast path gains

`task_propose(brief: Annotated[str, ...], project: Annotated[str, ...] = "",
provider: Annotated[str, ...] = "")` registered with the v1 `@tool`
decorator (schema from type hints, invariant 6), **not dangerous**. It writes
into a per-run slot the provider reads after the turn (the `runtime.py`
ContextVar pattern — add one ContextVar there if none fits; that is the one
v1 file you may edit, additively) and returns a short confirmation string
the model will relay. A second call in one turn replaces the first. Import
it from `fastpath.py` so registering is a side effect of using the provider.

### `FAST_TOOLS` — the structural boundary, §8.1

A frozenset in `fastpath.py`, built by name from `tools.REGISTRY`:
read-only files (`read_file`, `grep_files`, `list_dir` if it exists),
`fetch_page` (single page), memory read/write, the four session read tools,
clock, `skill_list`/`skill_read`, the control tools (`set_voice_mute`,
`set_avatar`, `avatar_list`, the spotify core), and `task_propose`.
**Excluded, and asserted excluded:** anything `dangerous`, `write_file`,
`edit_file`, `run_command`, `run_readonly`, every `browser_*`, `desktop_*`,
`cad_*`, `gmail_*`, `task_*`/`workflow_*`, `run_subagent`, `run_fleet`,
`plan_write`, `load_tools`, `delegate`. If a name in `FAST_TOOLS` is not in
the registry, fail loudly at import — a silently shrinking toolset is the
invariant-10 failure mode.

### `tests/v2/fastpath_check.py`

Free: `llm.chat` faked as in `tests/loop_check.py`; `config.SESSIONS_DIR`,
`config.ALLOWLIST_PATH`, `config.MODELS_PATH` pointed at a temp dir; no
network, no key needed (`health()` tested with the env var patched both
ways). Cover: the exact `EventKind` sequence for a scripted two-step turn
(tool call then text) and the `data` conventions; deltas arriving before
`TEXT`; `task_propose` → proposal on `TURN_FINISHED` (and a second call
replacing the first); step exhaustion → proposal with the handoff prefix,
using a script that calls tools forever; interrupt mid-turn →
`TURN_FINISHED{stop: "interrupted"}` with a wire-valid transcript (no
outstanding tool_call — assert with `agent.durable_state`/message shape);
`ERROR(fatal)` on a raising `llm.chat`, generator terminates; resume: a
second handle on the same thread sees the first's transcript; **the
boundary**: every `FAST_TOOLS` entry exists, none is dangerous, every
excluded name above is absent, and a `Brief` cannot widen it; the approver
adapter: register a throwaway dangerous tool in the test, put it in a
handle's toolset via a test-only hook, and assert `permit` is called with
the tool name and that `DENY` means the tool never runs; `STRICT` refused;
`usage()` sums costs. Two concurrent handles do not share a plan or
transcript (the `longhorizon_check` concurrency pattern — one shared script
dispatching on the calling agent's own message, since `llm.chat` is a
module global).

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/fastpath_check.py`
Then run `tests/loop_check.py`, `tests/longhorizon_check.py`,
`tests/sessions_check.py` the same way — the runtime edit must not move them.

## Rules

Stay inside `jarvis/v2/providers/fastpath.py`, `jarvis/v2/providers/__init__.py`,
`jarvis/v2/tools/` (new), `jarvis/runtime.py` (additive only), `tests/v2/`,
and `docs/codex-briefs/WP2-notes.md`. No other v1 edits, no CLAUDE.md edits,
no new dependencies. Style: the repo's — type hints, docstrings that say
*why*, small functions, no framework. When every suite named above is green,
commit on your branch with a message starting `v2 WP2: FastPathProvider`
ending with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Do not
push, do not merge. Finish with `WP2-notes.md` (under 60 lines): what you
built, the test commands and their last lines, workarounds, proposed
interface changes. Report the branch name and the notes file path as your
final message.
